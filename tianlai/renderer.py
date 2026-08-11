from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import json
from itertools import islice
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
from typing import Any
import uuid

from .audio import (
    write_wav_pcm24 as _write_wav_pcm24,
    write_wav_pcm24_blocks as _write_wav_pcm24_blocks,
)
from .canonical_json import canonical_json_sha256
from .events import PerformanceDocument, parse_performance_document
from .instrument import (
    Instrument,
    StereoFrame,
    _EVENT_FREE_RENDER_BLOCK_CONTRACT,
    create_instrument,
)
from .license_sidecar import (
    AudioArtifact,
    InstrumentUse,
    sha256_file,
    single_render_sidecar_paths,
    write_license_sidecars,
)
from .post_render_check import (
    POST_RENDER_CHECK_NAME,
    analyze_rendered_wav,
    validate_post_render_check,
    write_post_render_check,
)
from .render_lock import acquire_render_lock


@dataclass(frozen=True, slots=True)
class RenderResult:
    sample_rate: int
    frame_count: int
    duration_seconds: float
    peak_active_voices: int
    license_sidecar_path: str | None = None
    attribution_path: str | None = None
    post_render_check_path: str | None = None
    post_render_check: dict[str, Any] | None = None
    post_render_check_summary: dict[str, Any] | None = None


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
        event_count = len(events)
        for sample_index in range(document.total_samples):
            while (
                event_index < event_count
                and events[event_index].sample == sample_index
            ):
                instrument.handle_event(events[event_index], document.tuning)
                event_index += 1
            active_voice_count = instrument.active_voice_count
            if active_voice_count > peak_voice_count[0]:
                peak_voice_count[0] = active_voice_count
            yield instrument.render_frame()

    return frames(), peak_voice_count


_DEFAULT_RENDER_BLOCK_FRAMES = 65_536
_DENSE_EVENT_GROUP_MINIMUM = 256
_DENSE_EVENT_MAXIMUM_AVERAGE_SPAN = 16


def _is_exact_audited_event_free_builtin(
    instrument_type: type[Any],
) -> bool:
    """Recognise only explicitly enumerated built-in implementation types.

    Names are used solely to avoid importing unrelated optional backends.
    Every branch finishes with an exact class-identity comparison, so a
    custom class cannot opt itself into trusted block execution by copying a
    module/name string or the private contract marker.
    """

    name = instrument_type.__name__
    if name == "OscillatorInstrument":
        from .oscillator import OscillatorInstrument

        return instrument_type is OscillatorInstrument
    if name == "SynthesizerInstrument":
        from .synthesizer import SynthesizerInstrument

        return instrument_type is SynthesizerInstrument
    if name == "SampleInstrument":
        from .sampler import SampleInstrument

        return instrument_type is SampleInstrument
    if name == "ModeledInstrument":
        from .modeled_instruments import ModeledInstrument

        return instrument_type is ModeledInstrument
    if name == "BianzhongInstrument":
        from .bianzhong import BianzhongInstrument

        return instrument_type is BianzhongInstrument
    if name == "Vsco2ViolaSectionInstrument":
        from .vsco2_viola import Vsco2ViolaSectionInstrument

        return instrument_type is Vsco2ViolaSectionInstrument
    if name in {
        "CelloInstrument",
        "ViolinInstrument",
        "FluteInstrument",
        "PianoInstrument",
    }:
        from .cello import CelloInstrument
        from .flute import FluteInstrument
        from .piano import PianoInstrument
        from .violin import ViolinInstrument

        return instrument_type in {
            CelloInstrument,
            ViolinInstrument,
            FluteInstrument,
            PianoInstrument,
        }
    if name == "DedicatedSfzInstrument":
        from .dedicated_sfz import DedicatedSfzInstrument

        return instrument_type is DedicatedSfzInstrument
    if name == "DedicatedFxInstrument":
        from .dedicated_fx import DedicatedFxInstrument

        return instrument_type is DedicatedFxInstrument
    if name == "MelodicTomsInstrument":
        from .melodic_toms import MelodicTomsInstrument

        return instrument_type is MelodicTomsInstrument
    if name == "ReversedCymbalInstrument":
        from .reversed_cymbal import ReversedCymbalInstrument

        return instrument_type is ReversedCymbalInstrument
    if name == "MtgSoloSaxInstrument":
        from .mtg_sax import MtgSoloSaxInstrument

        return instrument_type is MtgSoloSaxInstrument
    if name == "VpoBrassInstrument":
        from .vpo_brass import VpoBrassInstrument

        return instrument_type is VpoBrassInstrument
    if name == "VpoSoloWoodwindInstrument":
        from .vpo_woodwinds import VpoSoloWoodwindInstrument

        return instrument_type is VpoSoloWoodwindInstrument
    if name == "VpoPercussionInstrument":
        from .vpo_percussion import VpoPercussionInstrument

        return instrument_type is VpoPercussionInstrument
    if name in {
        "VpoSoloStringInstrument",
        "VpoStringSectionInstrument",
        "VpoHarpInstrument",
    }:
        from .vpo_strings import (
            VpoHarpInstrument,
            VpoSoloStringInstrument,
            VpoStringSectionInstrument,
        )

        return instrument_type in {
            VpoSoloStringInstrument,
            VpoStringSectionInstrument,
            VpoHarpInstrument,
        }
    if name in {
        "VpoCelestaInstrument",
        "VpoMixedChoirInstrument",
        "VpoCowbellInstrument",
        "VpoOrchestralHitInstrument",
    }:
        from .vpo_specials import (
            VpoCelestaInstrument,
            VpoCowbellInstrument,
            VpoMixedChoirInstrument,
            VpoOrchestralHitInstrument,
        )

        return instrument_type in {
            VpoCelestaInstrument,
            VpoMixedChoirInstrument,
            VpoCowbellInstrument,
            VpoOrchestralHitInstrument,
        }
    return False


def _exact_builtin_render_block(instrument: Instrument) -> Any | None:
    """Return a proven event-free block method or conservatively decline.

    Class-dictionary and implementation-identity checks are intentional.  A
    local subclass, instance monkeypatch or test double must continue through
    ``render_frame`` even if it inherits from an accelerated built-in class.
    """

    instrument_type = type(instrument)
    if not _is_exact_audited_event_free_builtin(instrument_type):
        return None
    namespace = instrument_type.__dict__
    if (
        namespace.get("_tianlai_render_block_contract")
        != _EVENT_FREE_RENDER_BLOCK_CONTRACT
        or namespace.get("handle_event")
        is not namespace.get("_tianlai_original_handle_event")
        or namespace.get("render_frame")
        is not namespace.get("_tianlai_original_render_frame")
        or namespace.get("render_block")
        is not namespace.get("_tianlai_original_render_block")
        or namespace.get("active_voice_count")
        is not namespace.get("_tianlai_original_active_voice_count")
    ):
        return None
    try:
        instance_namespace = vars(instrument)
    except TypeError:
        return None
    if any(
        name in instance_namespace
        for name in (
            "handle_event",
            "render_frame",
            "render_block",
            "active_voice_count",
        )
    ):
        return None
    provenance = instance_namespace.get("_tianlai_factory_provenance")
    if provenance is not None and (
        not isinstance(provenance, dict)
        or provenance.get("factory_route")
        != "builtin_manifest_dispatch_no_implementation"
    ):
        return None
    method = namespace.get("render_block")
    if not callable(method):
        return None
    return method.__get__(instrument, instrument_type)


def _prefer_dense_synth_frame_path(
    instrument: Instrument,
    document: PerformanceDocument,
) -> bool:
    """Keep the established dense path when a synth is mostly sounding.

    Sparse documents benefit from event-free zero blocks.  A continuously
    active synthesizer is DSP-bound, and materialising intermediate blocks
    cannot repay its extra transport.  The estimate is deliberately
    conservative: sustain or unusual hand-built payloads select the old path.
    """

    from .synthesizer import SynthesizerInstrument

    if (
        type(instrument) is not SynthesizerInstrument
        or _exact_builtin_render_block(instrument) is None
    ):
        return False
    total_samples = document.total_samples
    if total_samples <= 0:
        return True

    release_samples = max(0, int(instrument.release_samples))
    active_intervals: dict[int, int] = {}
    # Append at note_on time so intervals remain ordered by start sample and
    # the union can be measured linearly.  The previous implementation sorted
    # completed intervals, adding O(events log events) startup to huge scores.
    intervals: list[list[int]] = []
    previous_sample = -1
    for event in document.events:
        if type(event.payload) is not dict:
            return True
        if event.sample < previous_sample:
            return True
        previous_sample = event.sample
        if event.type == "control" and event.payload.get("name") == "sustain_pedal":
            return True
        if event.type == "note_on":
            note_id = event.payload.get("note_id")
            if type(note_id) is not int or note_id in active_intervals:
                return True
            active_intervals[note_id] = len(intervals)
            intervals.append([max(0, event.sample), total_samples])
        elif event.type == "note_off":
            note_id = event.payload.get("note_id")
            if type(note_id) is not int:
                return True
            interval_index = active_intervals.pop(note_id, None)
            if interval_index is not None:
                intervals[interval_index][1] = min(
                    total_samples,
                    event.sample + release_samples,
                )
    covered = 0
    merged_stop = 0
    for start, stop in intervals:
        start = min(total_samples, max(0, start))
        stop = min(total_samples, max(start, stop))
        if stop <= merged_stop:
            continue
        if start >= merged_stop:
            covered += stop - start
        else:
            covered += stop - merged_stop
        merged_stop = stop
    # Two thirds sounding (including the declared release) is dense enough
    # that the original direct stream is the stable performance winner.
    return covered * 3 >= total_samples * 2


def _prefer_dense_event_frame_path(
    document: PerformanceDocument,
) -> bool:
    """Avoid fragmenting transport into thousands of tiny native blocks.

    Only distinct in-range event samples matter: several events at one sample
    are handled as one ordered group before the same output frame.  The scan
    is linear and constant-space, and the deliberately narrow threshold is
    reserved for sample-rate automation rather than ordinary score events.
    """

    total_samples = document.total_samples
    if total_samples <= 0:
        return False

    unique_event_groups = 0
    previous_sample = -1
    previous_in_range_sample: int | None = None
    for event in document.events:
        event_sample = event.sample
        if event_sample < previous_sample:
            # Hand-built, unsorted documents stay on the established stream;
            # parsed production documents are already ordered.
            return True
        previous_sample = event_sample
        if not 0 <= event_sample < total_samples:
            continue
        if event_sample == previous_in_range_sample:
            continue
        previous_in_range_sample = event_sample
        unique_event_groups += 1
        if (
            unique_event_groups >= _DENSE_EVENT_GROUP_MINIMUM
            and unique_event_groups
            * _DENSE_EVENT_MAXIMUM_AVERAGE_SPAN
            >= total_samples
        ):
            return True
    return False


def _prefer_frame_stream_path(
    instrument: Instrument,
    document: PerformanceDocument,
) -> bool:
    """Preserve the established stream for custom or dense instruments.

    Block transport is an internal optimisation for exact, audited built-ins.
    Custom implementations, subclasses and instance-modified instruments keep
    the original lazy frame semantics, including first-error ordering.
    """

    if _exact_builtin_render_block(instrument) is None:
        return True
    return _prefer_dense_event_frame_path(
        document
    ) or _prefer_dense_synth_frame_path(instrument, document)


def render_document_blocks(
    instrument: Instrument,
    document: PerformanceDocument,
    *,
    maximum_block_frames: int = _DEFAULT_RENDER_BLOCK_FRAMES,
    sample_dtype: Any = "float64",
) -> tuple[Iterator[Any], list[int]]:
    """Return bounded render blocks and a mutable peak counter.

    This is the bulk counterpart to :func:`render_document`.  Audited
    event-free spans stop at event samples.  Dense synthesizer spans may carry
    events inside a block, but dispatch them immediately before their original
    output frame through the established per-frame loop.  Subclasses, custom
    instruments and altered classes also execute ``render_frame`` once per
    frame.

    ``sample_dtype`` is an internal transport choice: PCM-24 writing keeps
    float64 equivalence with the established writer, while existing stem
    paths can request float32 directly and avoid a second conversion.  The
    original frame-stream API remains independently lazy and unchanged.  This
    API is intended for internal consumers which already process complete
    bounded chunks (WAV encoding, stem buffers and worker transport).
    """

    if (
        isinstance(maximum_block_frames, bool)
        or not isinstance(maximum_block_frames, int)
        or maximum_block_frames <= 0
    ):
        raise ValueError("maximum_block_frames must be a positive integer")

    import numpy as np

    try:
        dtype = np.dtype(sample_dtype)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "sample_dtype must be float32 or float64"
        ) from error
    if dtype not in (np.dtype(np.float32), np.dtype(np.float64)):
        raise ValueError("sample_dtype must be float32 or float64")

    if _prefer_frame_stream_path(instrument, document):
        frames, peak_voice_count = render_document(instrument, document)

        def stream_blocks() -> Iterator[Any]:
            iterator = iter(frames)
            remaining = document.total_samples
            while remaining > 0:
                frame_count = min(maximum_block_frames, remaining)
                yield np.fromiter(
                    (
                        sample
                        for frame in islice(iterator, frame_count)
                        for sample in frame
                    ),
                    dtype=dtype,
                    count=frame_count * 2,
                ).reshape(frame_count, 2)
                remaining -= frame_count

        return stream_blocks(), peak_voice_count

    peak_voice_count = [0]

    def blocks() -> Iterator[Any]:
        from .synthesizer import SynthesizerInstrument

        event_index = 0
        sample_index = 0
        events = document.events
        event_count = len(events)

        while sample_index < document.total_samples:
            while (
                event_index < event_count
                and events[event_index].sample == sample_index
            ):
                instrument.handle_event(events[event_index], document.tuning)
                event_index += 1

            # Events are allowed to mutate a Python object.  Recheck exact
            # built-in admission after every event group so a local factory or
            # dynamic method installation cannot retain the fast path.
            native_render_block = _exact_builtin_render_block(instrument)
            dense_synth = (
                native_render_block is not None
                and type(instrument) is SynthesizerInstrument
                and instrument.active_voice_count > 0
            )
            dense_frame_path = dense_synth or native_render_block is None

            stop = min(
                document.total_samples,
                sample_index + maximum_block_frames,
            )
            if not dense_frame_path and event_index < event_count:
                next_event_sample = events[event_index].sample
                if sample_index < next_event_sample < stop:
                    stop = next_event_sample
            frame_count = stop - sample_index
            if frame_count <= 0:
                raise RuntimeError("render block scheduler made no progress")

            if dense_frame_path:
                next_event_index = event_index
                while (
                    next_event_index < event_count
                    and events[next_event_index].sample < stop
                ):
                    next_event_index += 1

                def dense_scalars(
                    first_event_index: int = event_index,
                    first_sample: int = sample_index,
                    last_sample: int = stop,
                ) -> Any:
                    block_event_index = first_event_index
                    for current_sample in range(first_sample, last_sample):
                        while (
                            block_event_index < event_count
                            and events[block_event_index].sample
                            == current_sample
                        ):
                            instrument.handle_event(
                                events[block_event_index],
                                document.tuning,
                            )
                            block_event_index += 1
                        active_voice_count = instrument.active_voice_count
                        if active_voice_count > peak_voice_count[0]:
                            peak_voice_count[0] = active_voice_count
                        left, right = instrument.render_frame()
                        yield left
                        yield right

                block = np.fromiter(
                    dense_scalars(),
                    dtype=dtype,
                    count=frame_count * 2,
                ).reshape(frame_count, 2)
                event_index = next_event_index
            elif native_render_block is not None:
                active_voice_count = instrument.active_voice_count
                if active_voice_count > peak_voice_count[0]:
                    peak_voice_count[0] = active_voice_count
                block = native_render_block(
                    frame_count,
                    sample_dtype=dtype,
                )
                if (
                    not isinstance(block, np.ndarray)
                    or block.dtype != dtype
                    or block.shape != (frame_count, 2)
                    or not block.flags.c_contiguous
                ):
                    raise RuntimeError(
                        "built-in instrument returned an invalid render block"
                    )
            sample_index = stop
            yield block

    return blocks(), peak_voice_count


def _validated_pcm24_frames(frames: Iterator[StereoFrame]) -> Iterator[StereoFrame]:
    """Reject samples which PCM-24 writing would otherwise silently clamp."""

    for frame_index, (left, right) in enumerate(frames):
        left = float(left)
        right = float(right)
        if not math.isfinite(left) or not math.isfinite(right):
            raise ValueError(
                "单乐器渲染产生了非有限样本："
                f"第 {frame_index} 帧 left={left!r}, right={right!r}"
            )
        frame_peak = max(abs(left), abs(right))
        if frame_peak > 1.0:
            excess_db = 20.0 * math.log10(frame_peak)
            raise ValueError(
                "单乐器渲染过载："
                f"第 {frame_index} 帧量化前峰值 {frame_peak:.6f}"
                f"（超出 {excess_db:+.2f} dB）。"
                "写盘会被静默削平，因此拒绝输出；"
                f"请将乐器或演奏增益降低至少 {excess_db:.2f} dB"
            )
        yield left, right


def write_wav_pcm24(
    path: str | Path,
    frames: Iterator[StereoFrame],
    sample_rate: int,
) -> int:
    """Write one strict single-instrument stream with batched validation."""

    return _write_wav_pcm24(
        path,
        frames,
        sample_rate,
        reject_out_of_range=True,
    )


def render_to_wav(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    output_path: str | Path,
) -> RenderResult:
    """Render through the same strict, atomic path used by the CLI."""

    return render_to_wav_atomic(
        instrument_manifest_path,
        performance_path,
        output_path,
    )


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


def _reserve_sibling_staging_path(directory: Path, suffix: str) -> Path:
    """Reserve one hidden staging path on the output filesystem."""

    descriptor, temporary_name = tempfile.mkstemp(
        dir=directory,
        prefix=".tianlai-render-",
        suffix=suffix,
    )
    os.close(descriptor)
    return Path(temporary_name)


def _single_render_post_check_path(audio_path: Path) -> Path:
    """Return the public QA sidecar path for one rendered WAV."""

    return Path(f"{audio_path}.{POST_RENDER_CHECK_NAME}")


def _require_regular_output_target(target: Path) -> os.stat_result | None:
    """Reject links and non-regular existing outputs without following them."""

    try:
        status = target.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f"无法安全检查渲染输出目标：{target}") from error

    if stat.S_ISLNK(status.st_mode):
        raise ValueError(f"拒绝将渲染输出写入符号链接：{target}")
    if not stat.S_ISREG(status.st_mode):
        raise ValueError(f"拒绝将渲染输出写入非普通文件：{target}")
    return status


def _validate_output_targets(targets: tuple[Path, ...]) -> None:
    for target in targets:
        _require_regular_output_target(target)


def _verify_staged_sidecars(
    json_path: Path,
    text_path: Path,
    audio_path: Path,
    *,
    final_audio_name: str,
    expected_document: dict[str, Any],
    expected_json_sha256: str,
    expected_text_sha256: str,
) -> None:
    """Verify staged sidecars bind the staged bytes to the final WAV name."""

    try:
        json_bytes = json_path.read_bytes()
        text_bytes = text_path.read_bytes()
        document = json.loads(json_bytes.decode("utf-8"))
        human_text = text_bytes.decode("utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("暂存的许可证 sidecar 无法完整读取") from error

    if not isinstance(document, dict) or document != expected_document:
        raise ValueError("暂存的许可证 JSON 与已构建文档不一致")
    if sha256_file(json_path) != expected_json_sha256:
        raise ValueError("暂存的许可证 JSON 摘要不一致")
    if sha256_file(text_path) != expected_text_sha256:
        raise ValueError("暂存的许可证文本摘要不一致")
    if not human_text.strip():
        raise ValueError("暂存的许可证文本为空")

    expected_artifact = {
        "role": "render",
        "path": Path(final_audio_name).as_posix(),
        "sha256": sha256_file(audio_path),
    }
    if document.get("audio_artifacts") != [expected_artifact]:
        raise ValueError(
            "暂存的许可证 sidecar 未绑定最终 WAV 文件名与实际暂存字节"
        )


def _verify_staged_post_render_check(
    path: Path,
    *,
    expected_report: dict[str, Any],
) -> None:
    """Reject a missing, malformed or mutated staged QA report."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("暂存的渲染后自检报告无法完整读取") from error
    if not isinstance(document, dict) or document != expected_report:
        raise ValueError("暂存的渲染后自检报告与已分析结果不一致")


def _verify_post_render_check_binding(
    report: object,
    audio_path: Path,
    *,
    final_audio_name: str,
    expected_sample_rate: int,
    expected_frame_count: int,
    expected_plan_sha256: str,
    expected_activity: bool,
) -> dict[str, Any]:
    """Fail closed unless the report binds this exact staged generation."""

    if not isinstance(report, dict):
        raise RuntimeError("渲染后自检分析器必须返回对象")
    try:
        validate_post_render_check(report)
    except ValueError as error:
        raise RuntimeError("渲染后自检报告合同无效") from error

    artifact = report.get("artifact")
    if (
        not isinstance(artifact, dict)
        or artifact.get("path") != Path(final_audio_name).as_posix()
        or artifact.get("sha256") != sha256_file(audio_path)
        or artifact.get("size_bytes") != audio_path.stat().st_size
    ):
        raise RuntimeError("渲染后自检没有绑定当前单乐器 WAV")

    performance_plan = report.get("performance_plan")
    if (
        not isinstance(performance_plan, dict)
        or performance_plan.get("sha256") != expected_plan_sha256
    ):
        raise RuntimeError("渲染后自检没有绑定当前演奏事件计划")

    audio_format = report.get("audio_format")
    expected_audio_format = {
        "container": "WAV",
        "encoding": "PCM",
        "bits_per_sample": 24,
        "channels": 2,
        "sample_rate": expected_sample_rate,
        "frame_count": expected_frame_count,
    }
    if not isinstance(audio_format, dict) or any(
        audio_format.get(field) != value
        for field, value in expected_audio_format.items()
    ):
        raise RuntimeError("渲染后自检记录的音频格式或帧数不一致")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        raise RuntimeError("渲染后自检缺少 summary 对象")
    if summary.get("expected_activity") is not expected_activity:
        raise RuntimeError("渲染后自检的活动内容结论与演奏事件计划不一致")
    if summary.get("can_proceed") is not True:
        raise RuntimeError("渲染后自检未通过，拒绝发布单乐器渲染产物")
    return dict(summary)


def _copy_existing_output_to_backup(target: Path) -> Path | None:
    """Copy an existing output to a durable same-directory rollback file."""

    if _require_regular_output_target(target) is None:
        return None

    descriptor, backup_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tianlai-backup",
    )
    backup = Path(backup_name)
    try:
        try:
            source = target.open("rb")
        except BaseException:
            os.close(descriptor)
            raise
        with source, os.fdopen(descriptor, "wb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())
        return backup
    except BaseException:
        # The random backup is recoverable.  Deleting its mutable pathname on
        # an exception could remove a file installed by a concurrent writer.
        raise


def _preserve_transaction_file(path: Path, *, label: str) -> Path | None:
    """Move one transaction entry aside without deleting a pathname."""

    if not os.path.lexists(path):
        return None
    parent = path.parent
    for _ in range(16):
        preserved = parent / (
            f".{path.name.lstrip('.')}.{label}-preserved-{uuid.uuid4().hex}"
        )
        if os.path.lexists(preserved):
            continue
        try:
            os.rename(path, preserved)
        except FileExistsError:
            continue
        except FileNotFoundError:
            # A concurrent writer may have removed the active entry after
            # ``lexists``.  Treat a genuinely absent source as already
            # retired; do not turn cleanup into a destructive recovery step.
            if not os.path.lexists(path):
                return None
            raise
        return preserved
    raise RuntimeError(f"cannot preserve render transaction file: {path}")


def _replace_published_file(staged: Path, target: Path) -> None:
    """Publication seam kept separate so every replace can be fault-injected."""

    os.replace(staged, target)


def _restore_published_file(backup: Path, target: Path) -> None:
    """Restore one old target without clobbering a concurrent replacement.

    The newly published entry is first moved into the recoverable namespace.
    A hard link then installs the already durable same-filesystem backup with
    create-if-absent semantics.  On Windows, where ``rename`` itself refuses
    an existing destination, it is also a safe fallback for filesystems which
    do not support hard links.  On POSIX an unavailable hard-link operation is
    reported as an incomplete rollback instead of falling back to a rename
    which could overwrite a racing writer.
    """

    _preserve_transaction_file(target, label="rollback")
    try:
        os.link(backup, target, follow_symlinks=False)
        return
    except TypeError:
        # Some Python/filesystem combinations do not accept
        # ``follow_symlinks``.  The source is a renderer-created regular file.
        try:
            os.link(backup, target)
            return
        except (AttributeError, NotImplementedError, OSError) as error:
            link_error = error
    except (AttributeError, NotImplementedError, OSError) as error:
        link_error = error

    if os.name == "nt":
        try:
            # Unlike POSIX rename(), Windows rename refuses to replace an
            # existing destination, retaining the same no-clobber guarantee.
            os.rename(backup, target)
            return
        except OSError as rename_error:
            raise rename_error from link_error
    raise link_error


def _publish_staged_artifacts(
    staged_targets: tuple[tuple[Path, Path], ...],
) -> None:
    """Publish a prepared artifact set and roll back ordinary exceptions.

    Each old target is copied before any visible file is changed.  The caller
    orders every sidecar first and the WAV last, making the WAV replacement the
    successful transaction's commit marker.  Multiple filesystem entries
    cannot be made crash-atomic; this routine guarantees rollback only while
    Python remains able to handle the publication error.
    """

    backups: dict[Path, Path | None] = {}
    published: set[Path] = set()
    protected_backups: set[Path] = set()
    try:
        _validate_output_targets(
            tuple(target for _, target in staged_targets)
        )
        for _, target in staged_targets:
            backups[target] = _copy_existing_output_to_backup(target)

        try:
            for staged, target in staged_targets:
                _replace_published_file(staged, target)
                published.add(target)
        except BaseException as publication_error:
            rollback_errors: list[str] = []
            for _, target in reversed(staged_targets):
                if target not in published:
                    continue
                backup = backups[target]
                try:
                    if backup is not None:
                        _restore_published_file(backup, target)
                    else:
                        _preserve_transaction_file(
                            target,
                            label="rollback",
                        )
                except BaseException as rollback_error:
                    if backup is not None:
                        protected_backups.add(backup)
                    rollback_errors.append(f"{target}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    "渲染输出发布失败，且四件套回滚不完整；"
                    "保留了可用的 .tianlai-backup 文件："
                    + "; ".join(rollback_errors)
                ) from publication_error
            raise
    finally:
        for backup in backups.values():
            if backup is not None and backup not in protected_backups:
                _preserve_transaction_file(
                    backup,
                    label="cleanup",
                )


def _render_to_wav_atomic_locked(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    output_path: str | Path,
) -> RenderResult:
    """Render and publish one strict WAV with its three matching sidecars.

    All four files are prepared and verified beside the destination before
    publication.  Handled publication failures restore the previous four-file
    state; this does not claim crash atomicity across four filesystem entries.
    """

    manifest_path = Path(instrument_manifest_path).resolve()
    manifest = load_json_object(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    performance_document = load_json_object(performance_path)
    performance = parse_performance_document(performance_document)
    performance_sha256 = canonical_json_sha256(performance_document)
    expected_activity = any(
        event.type == "note_on"
        and float(event.payload.get("velocity", 0.0)) > 0.0
        for event in performance.events
    )
    audio_path = Path(output_path)
    if audio_path.suffix.casefold() != ".wav":
        raise ValueError("单乐器渲染输出必须使用 .wav 扩展名")
    sidecar_path, attribution_path = single_render_sidecar_paths(audio_path)
    post_render_check_path = _single_render_post_check_path(audio_path)
    _validate_output_targets(
        (
            audio_path,
            sidecar_path,
            attribution_path,
            post_render_check_path,
        )
    )
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=audio_path.parent,
        prefix=f".{audio_path.name}.",
        suffix=".tianlai-part",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    staged_sidecar_path: Path | None = None
    staged_attribution_path: Path | None = None
    staged_post_render_check_path: Path | None = None

    try:
        instrument = create_instrument(
            manifest,
            performance.sample_rate,
            base_directory=str(manifest_path.parent),
        )
        try:
            if _prefer_frame_stream_path(instrument, performance):
                frames, peak = render_document(instrument, performance)
                frame_count = write_wav_pcm24(
                    temporary,
                    frames,
                    performance.sample_rate,
                )
            else:
                blocks, peak = render_document_blocks(instrument, performance)
                frame_count = _write_wav_pcm24_blocks(
                    temporary,
                    blocks,
                    performance.sample_rate,
                    reject_out_of_range=True,
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
        post_render_check = analyze_rendered_wav(
            temporary,
            artifact_path=audio_path.name,
            expected_sample_rate=performance.sample_rate,
            expected_frame_count=frame_count,
            expected_activity=expected_activity,
            plan_sha256=performance_sha256,
        )
        post_render_check_summary = _verify_post_render_check_binding(
            post_render_check,
            temporary,
            final_audio_name=audio_path.name,
            expected_sample_rate=performance.sample_rate,
            expected_frame_count=frame_count,
            expected_plan_sha256=performance_sha256,
            expected_activity=expected_activity,
        )

        staged_sidecar_path = _reserve_sibling_staging_path(
            audio_path.parent,
            ".许可与署名.json.tianlai-stage",
        )
        staged_attribution_path = _reserve_sibling_staging_path(
            audio_path.parent,
            ".许可与署名.txt.tianlai-stage",
        )
        staged_post_render_check_path = _reserve_sibling_staging_path(
            audio_path.parent,
            ".渲染后自检.json.tianlai-stage",
        )
        sidecars = write_license_sidecars(
            staged_sidecar_path,
            staged_attribution_path,
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
                    path=temporary,
                    label=audio_path.name,
                ),
            ),
        )
        _verify_staged_sidecars(
            staged_sidecar_path,
            staged_attribution_path,
            temporary,
            final_audio_name=audio_path.name,
            expected_document=sidecars.document,
            expected_json_sha256=sidecars.json_sha256,
            expected_text_sha256=sidecars.text_sha256,
        )
        write_post_render_check(
            staged_post_render_check_path,
            post_render_check,
        )
        _verify_staged_post_render_check(
            staged_post_render_check_path,
            expected_report=post_render_check,
        )
        _publish_staged_artifacts(
            (
                (staged_sidecar_path, sidecar_path),
                (staged_attribution_path, attribution_path),
                (
                    staged_post_render_check_path,
                    post_render_check_path,
                ),
                (temporary, audio_path),
            )
        )
        return RenderResult(
            sample_rate=performance.sample_rate,
            frame_count=frame_count,
            duration_seconds=frame_count / performance.sample_rate,
            peak_active_voices=peak[0],
            license_sidecar_path=str(sidecar_path),
            attribution_path=str(attribution_path),
            post_render_check_path=str(post_render_check_path),
            post_render_check=post_render_check,
            post_render_check_summary=post_render_check_summary,
        )
    finally:
        # Successful publication consumes every staged name.  On failure,
        # move random private files out of the active staging namespace for
        # recovery instead of unlinking a pathname that may have been
        # replaced after validation.
        for staged in (
            temporary,
            staged_sidecar_path,
            staged_attribution_path,
            staged_post_render_check_path,
        ):
            if staged is not None and os.path.lexists(staged):
                _preserve_transaction_file(staged, label="cleanup")


def render_to_wav_atomic(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    output_path: str | Path,
) -> RenderResult:
    """Render and publish one four-artifact set under exclusive ownership."""

    # Preserve the public target-validation contract before asking the lock
    # layer to bind the WAV pathname.  The locked implementation repeats this
    # check, so a target swapped between these two points still fails closed.
    audio_path = Path(output_path)
    if audio_path.suffix.casefold() != ".wav":
        # The transaction owns the WAV plus three paths derived by appending
        # fixed suffixes.  Requiring a WAV name makes two valid transactions'
        # target sets disjoint, so locking the primary target covers all four
        # artifacts without a multi-lock deadlock surface.
        raise ValueError("单乐器渲染输出必须使用 .wav 扩展名")
    sidecar_path, attribution_path = single_render_sidecar_paths(audio_path)
    _validate_output_targets(
        (
            audio_path,
            sidecar_path,
            attribution_path,
            _single_render_post_check_path(audio_path),
        )
    )

    # Keep one operating-system lock from the first target check through the
    # final WAV commit marker and every rollback path.  Without it, two CLI
    # renders can interleave their four os.replace operations and publish a WAV
    # whose license or post-check belongs to the other render.
    with acquire_render_lock(output_path, existing_target_kind="file"):
        return _render_to_wav_atomic_locked(
            instrument_manifest_path,
            performance_path,
            output_path,
        )
