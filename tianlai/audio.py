from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
import struct
import wave
from typing import Any

from .instrument import StereoFrame


def _pcm24(value: float) -> bytes:
    integer = round(max(-1.0, min(1.0, value)) * 8_388_607.0)
    if integer < 0:
        integer += 1 << 24
    return bytes((integer & 0xFF, (integer >> 8) & 0xFF, (integer >> 16) & 0xFF))


def write_wav_pcm24(path: str | Path, frames: Iterable[StereoFrame], sample_rate: int) -> int:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame_count = 0
    buffer = bytearray()
    with wave.open(str(output_path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        for left, right in frames:
            buffer.extend(_pcm24(left))
            buffer.extend(_pcm24(right))
            frame_count += 1
            if len(buffer) >= 24_576:
                output.writeframesraw(buffer)
                buffer.clear()
        if buffer:
            output.writeframesraw(buffer)
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
                payload = source.read(chunk_size)
                loop_count = struct.unpack_from("<I", payload, 28)[0]
                if loop_count < 1 or len(payload) < 60:
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
