from __future__ import annotations

from io import BytesIO
from pathlib import Path
import struct
import wave
from unittest.mock import patch

import numpy as np
import pytest

from tianlai.audio import (
    _PCM24_NUMPY_CHUNK_FRAMES,
    _PCM24_SCALE,
    _pcm24,
    wav_loop_points,
    write_wav_pcm24,
)


def _smpl_payload(*, start: int = 100, end: int = 199) -> bytes:
    payload = bytearray(60)
    struct.pack_into("<I", payload, 28, 1)
    struct.pack_into("<III", payload, 40, 0, start, end)
    return bytes(payload)


def _wav_with_smpl(payload: bytes, *, declared_size: int = 60) -> bytes:
    return (
        b"RIFF"
        + struct.pack("<I", 4 + 8 + len(payload))
        + b"WAVE"
        + b"smpl"
        + struct.pack("<I", declared_size)
        + payload
    )


def test_wav_loop_points_rejects_a_truncated_smpl_chunk(tmp_path: Path) -> None:
    path = tmp_path / "truncated.wav"
    path.write_bytes(_wav_with_smpl(b"bad!"))

    assert wav_loop_points(path) is None


def test_wav_loop_points_bounds_the_untrusted_chunk_read() -> None:
    class GuardedReader(BytesIO):
        def read(self, size: int = -1) -> bytes:
            assert size <= 60
            return super().read(size)

    source = GuardedReader(
        _wav_with_smpl(_smpl_payload(), declared_size=0xFFFF_FFF0)
    )
    with patch.object(Path, "open", return_value=source):
        assert wav_loop_points("bounded.wav") == (100, 200)


def test_numpy_pcm24_fast_path_is_byte_identical_and_does_not_mutate(
    tmp_path: Path,
) -> None:
    source = np.array(
        [
            [-2.0, -1.0],
            [-1.5 / _PCM24_SCALE, -0.5 / _PCM24_SCALE],
            [0.0, 0.5 / _PCM24_SCALE],
            [1.5 / _PCM24_SCALE, 1.0],
            [2.0, np.inf],
            [-np.inf, np.nan],
        ],
        dtype=np.float64,
    )
    before = source.copy()
    fast = tmp_path / "fast.wav"
    scalar = tmp_path / "scalar.wav"

    assert write_wav_pcm24(fast, source, 48_000) == len(source)
    assert write_wav_pcm24(
        scalar,
        ((float(left), float(right)) for left, right in source),
        48_000,
    ) == len(source)

    assert fast.read_bytes() == scalar.read_bytes()
    assert np.array_equal(source, before, equal_nan=True)


def test_numpy_pcm24_fast_path_accepts_non_contiguous_float32(
    tmp_path: Path,
) -> None:
    base = np.linspace(-1.25, 1.25, 4_000, dtype=np.float32).reshape(-1, 4)
    source = base[:, ::2]
    assert not source.flags.c_contiguous
    fast = tmp_path / "non-contiguous-fast.wav"
    scalar = tmp_path / "non-contiguous-scalar.wav"

    write_wav_pcm24(fast, source, 44_100)
    write_wav_pcm24(
        scalar,
        ((float(left), float(right)) for left, right in source),
        44_100,
    )

    assert fast.read_bytes() == scalar.read_bytes()


def test_numpy_pcm24_fast_path_keeps_raw_writes_bounded(tmp_path: Path) -> None:
    class WaveSink:
        def __init__(self) -> None:
            self.payload_sizes: list[int] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def setnchannels(self, _value: int) -> None:
            return None

        def setsampwidth(self, _value: int) -> None:
            return None

        def setframerate(self, _value: int) -> None:
            return None

        def writeframesraw(self, payload: bytes) -> None:
            self.payload_sizes.append(len(payload))

    sink = WaveSink()
    frames = np.zeros(
        (_PCM24_NUMPY_CHUNK_FRAMES * 2 + 1, 2),
        dtype=np.float32,
    )
    with patch("tianlai.audio.wave.open", return_value=sink):
        assert write_wav_pcm24(tmp_path / "bounded.wav", frames, 48_000) == len(
            frames
        )

    assert sink.payload_sizes == [
        _PCM24_NUMPY_CHUNK_FRAMES * 6,
        _PCM24_NUMPY_CHUNK_FRAMES * 6,
        6,
    ]


def test_iterable_pcm24_batches_are_scalar_identical(tmp_path: Path) -> None:
    source = [
        (-2.0, -1.0),
        (-1.5 / _PCM24_SCALE, -0.5 / _PCM24_SCALE),
        (0.0, 0.5 / _PCM24_SCALE),
        (1.5 / _PCM24_SCALE, 1.0),
        (2.0, np.inf),
        (-np.inf, np.nan),
    ]
    destination = tmp_path / "iterable.wav"

    assert write_wav_pcm24(destination, iter(source), 48_000) == len(source)

    with wave.open(str(destination), "rb") as decoded:
        payload = decoded.readframes(decoded.getnframes())
    expected = b"".join(
        _pcm24(left) + _pcm24(right) for left, right in source
    )
    assert payload == expected


def test_iterable_pcm24_fast_path_keeps_batches_bounded(tmp_path: Path) -> None:
    class WaveSink:
        def __init__(self) -> None:
            self.payload_sizes: list[int] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def setnchannels(self, _value: int) -> None:
            return None

        def setsampwidth(self, _value: int) -> None:
            return None

        def setframerate(self, _value: int) -> None:
            return None

        def writeframesraw(self, payload: bytes) -> None:
            self.payload_sizes.append(len(payload))

    sink = WaveSink()
    frames = ((float(index), -float(index)) for index in range(9))
    with (
        patch("tianlai.audio._PCM24_NUMPY_CHUNK_FRAMES", 4),
        patch("tianlai.audio.wave.open", return_value=sink),
    ):
        assert write_wav_pcm24(tmp_path / "bounded-iterable.wav", frames, 48_000) == 9

    assert sink.payload_sizes == [24, 24, 6]


def test_strict_iterable_pcm24_reports_the_global_frame_index(
    tmp_path: Path,
) -> None:
    frames = iter([(0.0, 0.0)] * 5 + [(1.000_001, 0.0)])
    with (
        patch("tianlai.audio._PCM24_NUMPY_CHUNK_FRAMES", 4),
        pytest.raises(ValueError, match="第 5 帧.*过载|过载.*第 5 帧"),
    ):
        write_wav_pcm24(
            tmp_path / "strict.wav",
            frames,
            48_000,
            reject_out_of_range=True,
        )


@pytest.mark.parametrize(
    ("frames", "message"),
    (
        ([(1.000_001, 0.0), (np.nan, 0.0)], "过载：第 0 帧"),
        ([(np.nan, 0.0), (1.000_001, 0.0)], "非有限样本：第 0 帧"),
        ([(np.inf, 0.0)], "非有限样本：第 0 帧"),
    ),
)
def test_strict_pcm24_reports_the_first_failure_in_stream_order(
    tmp_path: Path,
    frames: list[tuple[float, float]],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        write_wav_pcm24(
            tmp_path / "failure-order.wav",
            iter(frames),
            48_000,
            reject_out_of_range=True,
        )
