from __future__ import annotations

from collections.abc import Iterable
from itertools import islice
import math
from pathlib import Path
import struct
import wave
from typing import Any

from .instrument import StereoFrame


_PCM24_SCALE = 8_388_607.0
_PCM24_NUMPY_CHUNK_FRAMES = 65_536


def _pcm24(value: float) -> bytes:
    integer = round(max(-1.0, min(1.0, value)) * _PCM24_SCALE)
    if integer < 0:
        integer += 1 << 24
    return bytes((integer & 0xFF, (integer >> 8) & 0xFF, (integer >> 16) & 0xFF))


def _validate_pcm24_frame(
    left: object,
    right: object,
    frame_index: int,
) -> tuple[float, float]:
    left_value = float(left)
    right_value = float(right)
    if not math.isfinite(left_value) or not math.isfinite(right_value):
        raise ValueError(
            "单乐器渲染产生了非有限样本："
            f"第 {frame_index} 帧 left={left_value!r}, right={right_value!r}"
        )
    frame_peak = max(abs(left_value), abs(right_value))
    if frame_peak > 1.0:
        excess_db = 20.0 * math.log10(frame_peak)
        raise ValueError(
            "单乐器渲染过载："
            f"第 {frame_index} 帧量化前峰值 {frame_peak:.6f}"
            f"（超出 {excess_db:+.2f} dB）。"
            "写盘会被静默削平，因此拒绝输出；"
            f"请将乐器或演奏增益降低至少 {excess_db:.2f} dB"
        )
    return left_value, right_value


def _validate_pcm24_samples(samples: Any, frame_offset: int) -> None:
    """Reject the first non-finite or clipping stereo frame in one batch."""

    import numpy as np

    finite_frames = np.isfinite(samples).all(axis=1)
    overloaded = (
        (samples[:, 0] > 1.0)
        | (samples[:, 0] < -1.0)
        | (samples[:, 1] > 1.0)
        | (samples[:, 1] < -1.0)
    )
    nonfinite_indices = np.flatnonzero(~finite_frames)
    overload_indices = np.flatnonzero(overloaded)
    first_nonfinite = (
        None if nonfinite_indices.size == 0 else int(nonfinite_indices[0])
    )
    first_overload = (
        None if overload_indices.size == 0 else int(overload_indices[0])
    )
    if first_nonfinite is not None and (
        first_overload is None or first_nonfinite <= first_overload
    ):
        local_index = first_nonfinite
        _validate_pcm24_frame(
            samples[local_index, 0],
            samples[local_index, 1],
            frame_offset + local_index,
        )
    if first_overload is not None:
        local_index = first_overload
        _validate_pcm24_frame(
            samples[local_index, 0],
            samples[local_index, 1],
            frame_offset + local_index,
        )


def _write_numpy_pcm24(
    output: Any,
    frames: object,
    *,
    reject_out_of_range: bool = False,
    frame_offset: int = 0,
) -> int | None:
    """Write a numeric ``(frames, 2)`` ndarray in bounded vector chunks.

    Return ``None`` when the value is not eligible, allowing the public writer
    to retain its generic iterable behaviour.  Conversion intentionally keeps
    the scalar writer's clipping, bankers-rounding and NaN/Infinity semantics.
    """

    import numpy as np

    if (
        not isinstance(frames, np.ndarray)
        or frames.ndim != 2
        or frames.shape[1] != 2
        or not (
            np.issubdtype(frames.dtype, np.integer)
            or (
                np.issubdtype(frames.dtype, np.floating)
                and frames.dtype.itemsize <= 8
            )
        )
    ):
        return None
    frame_count = int(frames.shape[0])
    for start in range(0, frame_count, _PCM24_NUMPY_CHUNK_FRAMES):
        samples = np.array(
            frames[start : start + _PCM24_NUMPY_CHUNK_FRAMES],
            dtype=np.float64,
            order="C",
            copy=True,
        )
        if reject_out_of_range:
            _validate_pcm24_samples(samples, frame_offset + start)
        np.nan_to_num(
            samples,
            copy=False,
            nan=1.0,
            posinf=1.0,
            neginf=-1.0,
        )
        np.clip(samples, -1.0, 1.0, out=samples)
        samples *= _PCM24_SCALE
        np.rint(samples, out=samples)
        integers = samples.astype("<i4")
        packed = integers.view(np.uint8).reshape(-1, 4)[:, :3]
        output.writeframesraw(packed.tobytes(order="C"))
    return frame_count


def _write_iterable_pcm24(
    output: Any,
    frames: Iterable[StereoFrame],
    *,
    reject_out_of_range: bool = False,
) -> int:
    """Encode a generic frame iterator in bounded, vectorizable batches.

    Numeric stereo batches take the same NumPy encoder as ndarray callers.
    Unusual iterable values retain the original scalar unpacking and encoding
    behaviour instead of becoming a narrower API as a side effect of the fast
    path.
    """

    import numpy as np

    iterator = iter(frames)
    frame_count = 0
    scalar_buffer = bytearray()
    while True:
        batch = list(islice(iterator, _PCM24_NUMPY_CHUNK_FRAMES))
        if not batch:
            break
        try:
            numeric_batch: object = np.asarray(batch)
        except (TypeError, ValueError, OverflowError):
            numeric_batch = None
        written = _write_numpy_pcm24(
            output,
            numeric_batch,
            reject_out_of_range=reject_out_of_range,
            frame_offset=frame_count,
        )
        if written is not None:
            frame_count += written
            continue
        for left, right in batch:
            if reject_out_of_range:
                left, right = _validate_pcm24_frame(left, right, frame_count)
            scalar_buffer.extend(_pcm24(left))
            scalar_buffer.extend(_pcm24(right))
            frame_count += 1
            if len(scalar_buffer) >= 24_576:
                output.writeframesraw(scalar_buffer)
                scalar_buffer.clear()
    if scalar_buffer:
        output.writeframesraw(scalar_buffer)
    return frame_count


def write_wav_pcm24(
    path: str | Path,
    frames: Iterable[StereoFrame],
    sample_rate: int,
    *,
    reject_out_of_range: bool = False,
) -> int:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        numpy_frame_count = _write_numpy_pcm24(
            output,
            frames,
            reject_out_of_range=reject_out_of_range,
        )
        if numpy_frame_count is not None:
            return numpy_frame_count
        return _write_iterable_pcm24(
            output,
            frames,
            reject_out_of_range=reject_out_of_range,
        )


def write_wav_pcm24_blocks(
    path: str | Path,
    blocks: Iterable[Any],
    sample_rate: int,
    *,
    reject_out_of_range: bool = False,
) -> int:
    """Write bounded stereo blocks without flattening them into frame tuples.

    Numeric arrays go directly to the existing bit-compatible vector encoder.
    Generic blocks retain the scalar fallback, which keeps custom instrument
    behaviour no narrower than :func:`write_wav_pcm24`.
    """

    import numpy as np

    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output_path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        frame_count = 0
        scalar_buffer = bytearray()
        for block in blocks:
            if isinstance(block, np.ndarray):
                batch: Any = block
            else:
                batch = block if isinstance(block, (list, tuple)) else list(block)
                try:
                    batch = np.asarray(batch)
                except (TypeError, ValueError, OverflowError):
                    pass
            # The numeric encoder writes immediately.  Flush any preceding
            # scalar fallback first so heterogeneous block streams cannot be
            # reordered on disk.
            if scalar_buffer:
                output.writeframesraw(scalar_buffer)
                scalar_buffer.clear()
            written = _write_numpy_pcm24(
                output,
                batch,
                reject_out_of_range=reject_out_of_range,
                frame_offset=frame_count,
            )
            if written is not None:
                frame_count += written
                continue

            for left, right in batch:
                if reject_out_of_range:
                    left, right = _validate_pcm24_frame(
                        left,
                        right,
                        frame_count,
                    )
                scalar_buffer.extend(_pcm24(left))
                scalar_buffer.extend(_pcm24(right))
                frame_count += 1
                if len(scalar_buffer) >= 24_576:
                    output.writeframesraw(scalar_buffer)
                    scalar_buffer.clear()
        if scalar_buffer:
            output.writeframesraw(scalar_buffer)
        return frame_count


def audio_file_info(path: str | Path) -> tuple[int, int, int]:
    """Return sample rate, frame count and channels without decoding the file."""

    audio_path = Path(path)
    try:
        import soundfile as sf

        info = sf.info(str(audio_path))
        if info.channels not in (1, 2):
            raise ValueError(f"unsupported audio channel count: {info.channels}")
        return int(info.samplerate), int(info.frames), int(info.channels)
    except ImportError:
        if audio_path.suffix.lower() != ".wav":
            raise ValueError("FLAC samples require the soundfile dependency") from None
        with wave.open(str(audio_path), "rb") as source:
            channels = source.getnchannels()
            if channels not in (1, 2):
                raise ValueError(f"unsupported WAV channel count: {channels}")
            return source.getframerate(), source.getnframes(), channels


def wav_loop_points(path: str | Path) -> tuple[int, int] | None:
    """Read the first forward loop from a WAV `smpl` chunk, if present."""

    audio_path = Path(path)
    if audio_path.suffix.lower() != ".wav":
        return None
    with audio_path.open("rb") as source:
        if source.read(4) != b"RIFF":
            return None
        source.seek(4, 1)
        if source.read(4) != b"WAVE":
            return None
        while True:
            chunk_id = source.read(4)
            if len(chunk_id) < 4:
                return None
            raw_size = source.read(4)
            if len(raw_size) < 4:
                return None
            chunk_size = struct.unpack("<I", raw_size)[0]
            if chunk_id == b"smpl" and chunk_size >= 60:
                # Only the fixed header and first loop record are needed.
                # Do not trust an unbounded chunk length from a third-party
                # sample, and check the bounded read before unpacking it.
                payload = source.read(60)
                if len(payload) < 60:
                    return None
                loop_count = struct.unpack_from("<I", payload, 28)[0]
                if loop_count < 1:
                    return None
                loop_type, start, end = struct.unpack_from("<III", payload, 40)[0:3]
                if loop_type != 0 or end < start:
                    return None
                return int(start), int(end + 1)
            source.seek(chunk_size + (chunk_size & 1), 1)


def read_audio_float(path: str | Path) -> tuple[int, Any]:
    """Decode WAV/FLAC into memory-efficient normalized stereo frames."""

    audio_path = Path(path)
    try:
        import soundfile as sf

        frames, sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
        if frames.shape[1] == 1:
            import numpy as np

            frames = np.repeat(frames, 2, axis=1)
        elif frames.shape[1] != 2:
            raise ValueError(f"unsupported audio channel count: {frames.shape[1]}")
        return int(sample_rate), frames
    except ImportError:
        if audio_path.suffix.lower() != ".wav":
            raise ValueError("FLAC samples require the soundfile dependency") from None

    return read_wav_float(audio_path)


def read_wav_float(path: str | Path) -> tuple[int, tuple[tuple[float, float], ...]]:
    """Standard-library PCM WAV fallback."""

    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        sample_rate = source.getframerate()
        frame_count = source.getnframes()
        if channels not in (1, 2):
            raise ValueError(f"unsupported WAV channel count: {channels}")
        if width not in (1, 2, 3, 4):
            raise ValueError(f"unsupported WAV sample width: {width}")
        raw = source.readframes(frame_count)

    def decode(offset: int) -> float:
        if width == 1:
            return (raw[offset] - 128) / 128.0
        if width == 2:
            return struct.unpack_from("<h", raw, offset)[0] / 32768.0
        if width == 3:
            value = raw[offset] | (raw[offset + 1] << 8) | (raw[offset + 2] << 16)
            if value & 0x800000:
                value -= 1 << 24
            return value / 8_388_608.0
        return struct.unpack_from("<i", raw, offset)[0] / 2_147_483_648.0

    stride = channels * width
    frames: list[tuple[float, float]] = []
    for frame_index in range(frame_count):
        offset = frame_index * stride
        left = decode(offset)
        right = left if channels == 1 else decode(offset + width)
        frames.append((left, right))
    return sample_rate, tuple(frames)
