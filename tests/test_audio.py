from __future__ import annotations

import hashlib
from io import BytesIO
import os
from pathlib import Path
import struct
import wave
from unittest.mock import patch

import numpy as np
import pytest

from tianlai.audio import (
    _PCM24_NUMPY_CHUNK_FRAMES,
    _PCM24_SCALE,
    _SequentialDigestWriter,
    _pcm24,
    revalidate_wav_file_evidence,
    verify_wav_file_evidence_bytes,
    wav_loop_points,
    write_wav_pcm24,
    write_wav_pcm24_blocks_with_evidence,
    write_wav_pcm24_with_evidence,
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


def test_evidenced_pcm24_is_byte_identical_and_binds_writer_descriptor(
    tmp_path: Path,
) -> None:
    source = np.linspace(-0.75, 0.75, 2_002, dtype=np.float64).reshape(-1, 2)
    established = tmp_path / "established.wav"
    evidenced = tmp_path / "evidenced.wav"

    assert write_wav_pcm24(established, source, 48_000) == len(source)
    result = write_wav_pcm24_with_evidence(
        evidenced,
        source,
        48_000,
        expected_frame_count=len(source),
    )

    assert result.frame_count == len(source)
    assert result.evidence is not None
    assert evidenced.read_bytes() == established.read_bytes()
    assert result.evidence.size_bytes == evidenced.stat().st_size
    assert result.evidence.sha256 == hashlib.sha256(
        evidenced.read_bytes()
    ).hexdigest()
    assert (
        revalidate_wav_file_evidence(evidenced, result.evidence)
        == result.evidence.sha256
    )


def test_evidenced_block_writer_is_byte_identical_across_block_shapes(
    tmp_path: Path,
) -> None:
    source = np.linspace(-0.5, 0.5, 514, dtype=np.float32).reshape(-1, 2)
    established = tmp_path / "established-blocks.wav"
    evidenced = tmp_path / "evidenced-blocks.wav"
    blocks = (source[:1], source[1:129], source[129:])

    assert write_wav_pcm24(established, source, 44_100) == len(source)
    result = write_wav_pcm24_blocks_with_evidence(
        evidenced,
        blocks,
        44_100,
        expected_frame_count=len(source),
    )

    assert result.frame_count == len(source)
    assert result.evidence is not None
    assert evidenced.read_bytes() == established.read_bytes()
    assert result.evidence.sha256 == hashlib.sha256(
        evidenced.read_bytes()
    ).hexdigest()


def test_evidenced_writer_rejects_a_wrong_declared_frame_count(
    tmp_path: Path,
) -> None:
    source = np.zeros((8, 2), dtype=np.float32)

    with pytest.raises(ValueError, match="frame count mismatch"):
        write_wav_pcm24_with_evidence(
            tmp_path / "wrong-size.wav",
            source,
            48_000,
            expected_frame_count=7,
        )


def test_evidence_digest_writer_rejects_a_short_write() -> None:
    class ShortWriter(BytesIO):
        def write(self, payload: bytes) -> int:
            return super().write(payload[:-1])

    tracked = _SequentialDigestWriter(ShortWriter())

    with pytest.raises(OSError, match="short or invalid write"):
        tracked.write(b"final WAV bytes")
    assert not tracked.sequential


def test_evidenced_zero_frame_wav_has_exact_standard_size(
    tmp_path: Path,
) -> None:
    target = tmp_path / "empty.wav"
    result = write_wav_pcm24_with_evidence(
        target,
        np.empty((0, 2), dtype=np.float32),
        48_000,
        expected_frame_count=0,
    )

    assert result.frame_count == 0
    assert result.evidence is not None
    assert result.evidence.size_bytes == 44
    assert target.stat().st_size == 44
    with wave.open(str(target), "rb") as source:
        assert source.getnframes() == 0
        assert source.getnchannels() == 2
        assert source.getsampwidth() == 3


def test_evidenced_digest_rejects_same_name_replacement(tmp_path: Path) -> None:
    target = tmp_path / "bound.wav"
    result = write_wav_pcm24_with_evidence(
        target,
        np.zeros((8, 2), dtype=np.float32),
        48_000,
        expected_frame_count=8,
    )
    assert result.evidence is not None
    previous = tmp_path / "previous.wav"
    target.replace(previous)
    target.write_bytes(b"x" * result.evidence.size_bytes)

    with pytest.raises(OSError):
        revalidate_wav_file_evidence(target, result.evidence)


def test_evidenced_writer_keeps_render_valid_when_change_token_is_unavailable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fallback.wav"
    with patch("tianlai.audio._descriptor_change_token", return_value=None):
        result = write_wav_pcm24_with_evidence(
            target,
            np.zeros((8, 2), dtype=np.float32),
            48_000,
            expected_frame_count=8,
        )

    assert result.frame_count == 8
    assert result.evidence is None
    assert target.is_file()


def test_evidence_revalidation_requests_full_hash_when_token_becomes_unavailable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "revalidation-fallback.wav"
    result = write_wav_pcm24_with_evidence(
        target,
        np.zeros((8, 2), dtype=np.float32),
        48_000,
        expected_frame_count=8,
    )
    assert result.evidence is not None

    with patch("tianlai.audio._descriptor_change_token", return_value=None):
        assert (
            revalidate_wav_file_evidence(target, result.evidence)
            == result.evidence.sha256
        )


@pytest.mark.skipif(os.name != "nt", reason="requires Windows FileBasicInfo")
def test_windows_change_time_rejects_same_size_edit_with_restored_mtime(
    tmp_path: Path,
) -> None:
    target = tmp_path / "change-time.wav"
    result = write_wav_pcm24_with_evidence(
        target,
        np.zeros((32, 2), dtype=np.float32),
        48_000,
        expected_frame_count=32,
    )
    evidence = result.evidence
    assert evidence is not None
    assert evidence.change_token_kind == "windows-file-basic-change-time-100ns"
    captured = target.stat()

    with target.open("r+b") as output:
        output.seek(60)
        original = output.read(1)
        assert len(original) == 1
        output.seek(60)
        output.write(bytes((original[0] ^ 1,)))
        output.flush()
        os.fsync(output.fileno())
    os.utime(
        target,
        ns=(captured.st_atime_ns, evidence.identity.modified_ns),
    )

    current = target.stat()
    assert current.st_size == evidence.size_bytes
    assert current.st_mtime_ns == evidence.identity.modified_ns
    with pytest.raises(OSError, match="changed"):
        revalidate_wav_file_evidence(target, evidence)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows mtime semantics")
def test_unavailable_change_token_still_rejects_same_size_byte_tamper(
    tmp_path: Path,
) -> None:
    target = tmp_path / "fallback-tamper.wav"
    result = write_wav_pcm24_with_evidence(
        target,
        np.zeros((32, 2), dtype=np.float32),
        48_000,
        expected_frame_count=32,
    )
    evidence = result.evidence
    assert evidence is not None
    captured = target.stat()

    with target.open("r+b") as output:
        output.seek(60)
        original = output.read(1)
        assert len(original) == 1
        output.seek(60)
        output.write(bytes((original[0] ^ 1,)))
        output.flush()
        os.fsync(output.fileno())
    os.utime(
        target,
        ns=(captured.st_atime_ns, evidence.identity.modified_ns),
    )

    with patch("tianlai.audio._descriptor_change_token", return_value=None):
        # Windows/Python combinations may expose the edit through identity
        # metadata before the fallback digest comparison.  Either rejection is
        # the intended fail-closed result for the same-size byte tamper.
        with pytest.raises(OSError, match="changed"):
            revalidate_wav_file_evidence(target, evidence)


def test_durable_byte_verifier_rejects_tamper_when_token_is_replayed(
    tmp_path: Path,
) -> None:
    target = tmp_path / "replayed-token-tamper.wav"
    result = write_wav_pcm24_with_evidence(
        target,
        np.zeros((32, 2), dtype=np.float32),
        48_000,
        expected_frame_count=32,
    )
    evidence = result.evidence
    assert evidence is not None
    captured = target.stat()

    with target.open("r+b") as output:
        output.seek(60)
        original = output.read(1)
        assert len(original) == 1
        output.seek(60)
        output.write(bytes((original[0] ^ 1,)))
        output.flush()
        os.fsync(output.fileno())
    os.utime(
        target,
        ns=(captured.st_atime_ns, evidence.identity.modified_ns),
    )

    with patch(
        "tianlai.audio._descriptor_change_token",
        return_value=(evidence.change_token_kind, evidence.change_token),
    ):
        # POSIX ctime independently exposes this edit before the mandatory
        # digest pass; Windows reaches the digest mismatch when its mocked
        # change token is replayed.  Both are fail-closed tamper detection.
        with pytest.raises(OSError, match="changed"):
            verify_wav_file_evidence_bytes(target, evidence)


@pytest.mark.skipif(os.name != "nt", reason="requires Windows share modes")
def test_durable_byte_verifier_excludes_writers_through_its_postcheck(
    tmp_path: Path,
) -> None:
    target = tmp_path / "writer-exclusion.wav"
    result = write_wav_pcm24_with_evidence(
        target,
        np.zeros((32, 2), dtype=np.float32),
        48_000,
        expected_frame_count=32,
    )
    evidence = result.evidence
    assert evidence is not None
    real_read = os.read
    write_was_excluded = False

    def attempt_write_after_hash(descriptor: int, count: int) -> bytes:
        nonlocal write_was_excluded
        payload = real_read(descriptor, count)
        if payload or write_was_excluded:
            return payload
        with pytest.raises(OSError):
            with target.open("r+b") as output:
                output.seek(60)
                output.write(b"x")
        write_was_excluded = True
        return payload

    with patch("tianlai.audio.os.read", side_effect=attempt_write_after_hash):
        assert (
            verify_wav_file_evidence_bytes(target, evidence)
            == evidence.sha256
        )

    assert write_was_excluded is True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == evidence.sha256


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
