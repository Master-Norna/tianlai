from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from .audio import write_wav_pcm24
from .events import PerformanceDocument, parse_performance_document
from .instrument import Instrument, StereoFrame, create_instrument
from .license_sidecar import (
    AudioArtifact,
    InstrumentUse,
    sha256_file,
    single_render_sidecar_paths,
    write_license_sidecars,
)


@dataclass(frozen=True, slots=True)
class RenderResult:
    sample_rate: int
    frame_count: int
    duration_seconds: float
    peak_active_voices: int
    license_sidecar_path: str | None = None
    attribution_path: str | None = None


def load_json_object(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def render_document(
    instrument: Instrument, document: PerformanceDocument
) -> tuple[Iterator[StereoFrame], list[int]]:
    """Return a lazy frame stream and a mutable one-item peak voice counter."""

    peak_voice_count = [0]

    def frames() -> Iterator[StereoFrame]:
        event_index = 0
        events = document.events
        for sample_index in range(document.total_samples):
            while event_index < len(events) and events[event_index].sample == sample_index:
                instrument.handle_event(events[event_index], document.tuning)
                event_index += 1
            peak_voice_count[0] = max(peak_voice_count[0], instrument.active_voice_count)
            yield instrument.render_frame()

    return frames(), peak_voice_count


def render_to_wav(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    output_path: str | Path,
) -> RenderResult:
    manifest_path = Path(instrument_manifest_path).resolve()
    manifest = load_json_object(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    performance = parse_performance_document(load_json_object(performance_path))
    instrument = create_instrument(
        manifest,
        performance.sample_rate,
        base_directory=str(manifest_path.parent),
    )
    try:
        frames, peak = render_document(instrument, performance)
        audio_path = Path(output_path)
        frame_count = write_wav_pcm24(
            audio_path,
            frames,
            performance.sample_rate,
        )
        sidecar_path, attribution_path = single_render_sidecar_paths(
            audio_path
        )
        sidecars = write_license_sidecars(
            sidecar_path,
            attribution_path,
            instrument_uses=(
                InstrumentUse(
                    manifest_path=manifest_path,
                    used_by=("single_instrument_render",),
                    expected_sha256=manifest_sha256,
                ),
            ),
            audio_artifacts=(
                AudioArtifact(
                    role="render",
                    path=audio_path,
                    label=audio_path.name,
                ),
            ),
        )
        return RenderResult(
            sample_rate=performance.sample_rate,
            frame_count=frame_count,
            duration_seconds=frame_count / performance.sample_rate,
            peak_active_voices=peak[0],
            license_sidecar_path=sidecars.json_path,
            attribution_path=sidecars.text_path,
        )
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()


def _verify_completed_wav(
    path: Path,
    *,
    expected_sample_rate: int,
    expected_frame_count: int,
) -> None:
    """Fail closed unless ``path`` is the complete WAV we just rendered."""

    try:
        import soundfile as sf

        with sf.SoundFile(str(path), mode="r") as audio:
            sample_rate = int(audio.samplerate)
            channels = int(audio.channels)
            frame_count = int(audio.frames)
            audio_format = str(audio.format)
            subtype = str(audio.subtype)
            # Opening only the header is insufficient for a truncated PCM file.
            # Probe both data boundaries without loading a potentially large
            # render back into memory.
            if frame_count > 0:
                if len(audio.read(1, dtype="float32", always_2d=True)) != 1:
                    raise ValueError("WAV 首帧不可读")
                audio.seek(frame_count - 1)
                if len(audio.read(1, dtype="float32", always_2d=True)) != 1:
                    raise ValueError("WAV 末帧不可读")
    except (OSError, RuntimeError, ValueError) as error:
        raise ValueError(f"临时 WAV 无法由 soundfile 完整读取：{path}") from error

    if audio_format != "WAV":
        raise ValueError(f"临时输出不是 WAV：{audio_format}")
    if subtype != "PCM_24":
        raise ValueError(f"临时 WAV 不是 PCM_24：{subtype}")
    if sample_rate != expected_sample_rate:
        raise ValueError(
            "临时 WAV 采样率不一致："
            f"期望 {expected_sample_rate}，实际 {sample_rate}"
        )
    if channels != 2:
        raise ValueError(f"临时 WAV 必须为双声道，实际 {channels}")
    if expected_frame_count <= 0 or frame_count != expected_frame_count:
        raise ValueError(
            "临时 WAV 帧数不合理："
            f"期望 {expected_frame_count}，实际 {frame_count}"
        )


def render_to_wav_atomic(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    output_path: str | Path,
) -> RenderResult:
    """Render one CLI WAV completely before atomically replacing its target.

    This deliberately remains separate from :func:`render_to_wav`: audition
    tooling and immutable project candidates have their own publication
    contracts.  A force-killed process can leave only an untrusted
    ``*.tianlai-part`` sibling; it never exposes that partial file as the final
    WAV.
    """

    manifest_path = Path(instrument_manifest_path).resolve()
    manifest = load_json_object(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    performance = parse_performance_document(load_json_object(performance_path))
    audio_path = Path(output_path)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=audio_path.parent,
        prefix=f".{audio_path.name}.",
        suffix=".tianlai-part",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)

    try:
        instrument = create_instrument(
            manifest,
            performance.sample_rate,
            base_directory=str(manifest_path.parent),
        )
        try:
            frames, peak = render_document(instrument, performance)
            frame_count = write_wav_pcm24(
                temporary,
                frames,
                performance.sample_rate,
            )
        finally:
            close = getattr(instrument, "close", None)
            if callable(close):
                close()

        if frame_count != performance.total_samples:
            raise ValueError(
                "渲染器写出帧数与事件时长不一致："
                f"期望 {performance.total_samples}，实际 {frame_count}"
            )
        _verify_completed_wav(
            temporary,
            expected_sample_rate=performance.sample_rate,
            expected_frame_count=frame_count,
        )
        os.replace(temporary, audio_path)

        sidecar_path, attribution_path = single_render_sidecar_paths(
            audio_path
        )
        sidecars = write_license_sidecars(
            sidecar_path,
            attribution_path,
            instrument_uses=(
                InstrumentUse(
                    manifest_path=manifest_path,
                    used_by=("single_instrument_render",),
                    expected_sha256=manifest_sha256,
                ),
            ),
            audio_artifacts=(
                AudioArtifact(
                    role="render",
                    path=audio_path,
                    label=audio_path.name,
                ),
            ),
        )
        return RenderResult(
            sample_rate=performance.sample_rate,
            frame_count=frame_count,
            duration_seconds=frame_count / performance.sample_rate,
            peak_active_voices=peak[0],
            license_sidecar_path=sidecars.json_path,
            attribution_path=sidecars.text_path,
        )
    finally:
        temporary.unlink(missing_ok=True)
