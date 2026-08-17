from __future__ import annotations

from collections.abc import Callable, Iterator
import copy
from dataclasses import dataclass
import hashlib
import json
from itertools import islice
import math
import os
from pathlib import Path
import stat
import sys
from typing import Any
import warnings

from .audio import (
    _write_wav_pcm24_blocks_file,
    _write_wav_pcm24_file,
    write_wav_pcm24 as _write_wav_pcm24,
)
from .atomic_publish import (
    _PrivateFileClaim,
    _SealedPrivateFileClaim,
    _open_private_file_claim,
    _relocate_sealed_private_file,
    _rename_noreplace,
    _reserve_private_file,
    _retire_private_file,
    _seal_private_file_claim,
)
from .authoring_json import AuthoringJsonLimits, strict_json_loads
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
from .plain_file import (
    PlainFileIdentity,
    read_plain_file_bytes,
    revalidate_plain_file,
    sha256_plain_file,
)
from .render_lock import acquire_render_lock, bind_plain_sibling_path
from .resource_limits import (
    ProjectLimits,
    validate_performance_document_resource_limits,
    validate_single_render_resource_limits,
)


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


@dataclass(frozen=True, slots=True)
class _JsonObjectSnapshot:
    path: Path
    identity: PlainFileIdentity
    payload: bytes
    document: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class _SingleRenderInputs:
    manifest: _JsonObjectSnapshot
    performance: _JsonObjectSnapshot
    parsed_performance: PerformanceDocument
    limits: ProjectLimits


def load_json_object(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as source:
        data = json.load(source)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return data


def _render_json_limits(limits: ProjectLimits) -> AuthoringJsonLimits:
    event_limit = limits.max_notes * 4
    return AuthoringJsonLimits(
        max_document_bytes=limits.max_score_json_bytes,
        max_depth=128,
        max_nodes=max(2_000_000, event_limit * 10 + 1024),
        max_string_bytes=4 * 1024 * 1024,
        max_array_items=max(250_000, event_limit),
        max_object_members=65_536,
    )


def _read_json_object_snapshot(
    path: str | Path,
    *,
    limits: ProjectLimits,
    label: str,
) -> _JsonObjectSnapshot:
    """Read one bounded strict JSON object through a single verified handle."""

    try:
        identity, payload = read_plain_file_bytes(
            path,
            maximum_bytes=limits.max_score_json_bytes,
        )
    except OSError as exc:
        raise ValueError(
            f"{label} 必须是可安全读取且不超过 "
            f"{limits.max_score_json_bytes} 字节的普通文件"
        ) from exc
    try:
        document = strict_json_loads(
            payload,
            limits=_render_json_limits(limits),
            require_object=True,
            require_js_safe_integers=False,
        )
    except ValueError as exc:
        raise ValueError(f"{label} 必须是严格且有界的 JSON 对象") from exc
    assert isinstance(document, dict)
    return _JsonObjectSnapshot(
        path=identity.path,
        identity=identity,
        payload=payload,
        document=document,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _capture_single_render_inputs(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    *,
    manifest_validator: Callable[[dict[str, Any]], None] | None = None,
) -> _SingleRenderInputs:
    """Capture and validate the exact immutable inputs used by one render."""

    # Freeze both relative names together before the policy callback.  A
    # callback is allowed to run arbitrary user code, including changing CWD;
    # that must not bind the manifest and performance to different roots.
    frozen_manifest_path = Path(
        os.path.abspath(os.fspath(instrument_manifest_path))
    )
    frozen_performance_path = Path(
        os.path.abspath(os.fspath(performance_path))
    )
    limits = ProjectLimits.from_environment()
    manifest = _read_json_object_snapshot(
        frozen_manifest_path,
        limits=limits,
        label="乐器清单",
    )
    if manifest_validator is not None:
        # Policy callbacks may retain or mutate their argument.  Keep the
        # render snapshot detached so the instrument and licence sidecar stay
        # bound to the exact bytes captured above.
        manifest_validator(copy.deepcopy(manifest.document))
    performance = _read_json_object_snapshot(
        frozen_performance_path,
        limits=limits,
        label="演奏事件文档",
    )
    validate_performance_document_resource_limits(
        performance.document,
        limits,
    )
    parsed_performance = parse_performance_document(performance.document)
    validate_single_render_resource_limits(parsed_performance, limits)
    return _SingleRenderInputs(
        manifest=manifest,
        performance=performance,
        parsed_performance=parsed_performance,
        limits=limits,
    )


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


def _write_claimed_wav_pcm24(
    claim: _PrivateFileClaim,
    frames: Iterator[StereoFrame],
    sample_rate: int,
) -> int:
    """Write a strict frame stream through one identity-bound claim."""

    with _open_private_file_claim(claim, truncate=True) as raw_audio:
        return _write_wav_pcm24_file(
            raw_audio,
            frames,
            sample_rate,
            reject_out_of_range=True,
        )


def _write_wav_pcm24_blocks(
    claim: _PrivateFileClaim,
    blocks: Iterator[Any],
    sample_rate: int,
    *,
    reject_out_of_range: bool = True,
) -> int:
    """Testable seam for a claim-bound block WAV writer."""

    with _open_private_file_claim(claim, truncate=True) as raw_audio:
        return _write_wav_pcm24_blocks_file(
            raw_audio,
            blocks,
            sample_rate,
            reject_out_of_range=reject_out_of_range,
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


def _reserve_sibling_staging_file(
    directory: Path,
    suffix: str,
) -> _PrivateFileClaim:
    """Reserve one identity-bound hidden staging file beside the output."""

    return _reserve_private_file(
        directory,
        prefix=".tianlai-render-",
        suffix=suffix,
    )


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
    if (
        not stat.S_ISREG(status.st_mode)
        or bool(getattr(status, "st_file_attributes", 0) & 0x400)
        or int(status.st_ino) == 0
        or int(status.st_nlink) < 1
    ):
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


@dataclass(frozen=True, slots=True)
class _OutputTargetSnapshot:
    identity: PlainFileIdentity
    sha256: str


def _capture_output_target(
    target: Path,
) -> _OutputTargetSnapshot | None:
    """Capture one existing public generation through a verified descriptor."""

    initial = _require_regular_output_target(target)
    if initial is None:
        return None
    try:
        identity, digest = sha256_plain_file(target)
    except OSError as error:
        raise OSError(
            "render output changed while its generation was captured"
        ) from error
    if (
        int(initial.st_dev) != identity.device
        or int(initial.st_ino) != identity.inode
        or int(initial.st_size) != identity.size
        or int(initial.st_mtime_ns) != identity.modified_ns
    ):
        raise OSError("render output changed while its generation was captured")
    return _OutputTargetSnapshot(identity=identity, sha256=digest)


def _output_snapshot_matches_path(
    snapshot: _OutputTargetSnapshot,
    path: Path,
) -> bool:
    """Return whether ``path`` still names one captured public generation."""

    try:
        current_identity, current_digest = sha256_plain_file(path)
    except OSError:
        return False
    identity = snapshot.identity
    return (
        current_identity.device == identity.device
        and current_identity.inode == identity.inode
        and current_identity.size == identity.size
        and current_identity.modified_ns == identity.modified_ns
        and current_digest == snapshot.sha256
    )


def _output_snapshot_metadata_matches_path(
    snapshot: _OutputTargetSnapshot,
    path: Path,
) -> bool:
    """Cheaply reject a changed target before move; digest is checked after."""

    try:
        status = path.lstat()
    except OSError:
        return False
    identity = snapshot.identity
    return (
        stat.S_ISREG(status.st_mode)
        and not stat.S_ISLNK(status.st_mode)
        and not bool(getattr(status, "st_file_attributes", 0) & 0x400)
        and int(status.st_nlink) == 1
        and int(status.st_dev) == identity.device
        and int(status.st_ino) == identity.inode
        and int(status.st_size) == identity.size
        and int(status.st_mtime_ns) == identity.modified_ns
    )


def _transaction_private_path(target: Path, *, label: str) -> Path:
    """Choose an unpredictable same-directory transaction pathname."""

    return target.parent / (
        f".{target.name.lstrip('.')}.{label}-{os.urandom(16).hex()}"
    )


def _restore_moved_racer(
    moved: Path,
    target: Path,
    primary_error: BaseException,
) -> None:
    """Best-effort restore of an unowned entry moved from a public name."""

    if not os.path.lexists(moved):
        return
    if os.path.lexists(target):
        primary_error.add_note(
            f"the concurrently moved entry was retained at {moved} because "
            "its public pathname is occupied"
        )
        return
    try:
        _rename_noreplace(moved, target)
    except BaseException as restore_error:
        primary_error.add_note(
            "a concurrent output was retained at "
            f"{moved} because its public name could not be restored: "
            f"{restore_error}"
        )


def _isolate_output_target(
    target: Path,
    snapshot: _OutputTargetSnapshot,
) -> _SealedPrivateFileClaim:
    """Move an exact old public generation into a sealed rollback entry."""

    identity = snapshot.identity
    requested = Path(os.path.abspath(os.fspath(target)))
    bound_target = bind_plain_sibling_path(
        requested,
        identity.parent_identity,
        message="render output snapshot is bound to another pathname",
    )
    if (
        identity.path.parent != bound_target.parent
        or not _output_snapshot_metadata_matches_path(snapshot, requested)
    ):
        raise OSError("render output snapshot is bound to another pathname")
    # Continue only through the canonical path captured with the verified
    # descriptor.  This accepts an ordinary Windows 8.3 spelling without
    # leaving a caller-spelled parent available for a later junction swap.
    target = identity.path
    if not _output_snapshot_metadata_matches_path(snapshot, target):
        raise OSError("render output changed before rollback isolation")

    for _ in range(32):
        backup = _transaction_private_path(target, label="tianlai-backup")
        reported_error: BaseException | None = None
        try:
            _rename_noreplace(target, backup)
        except FileExistsError:
            # A wrapper can report FileExists after the old generation was
            # moved and a concurrent writer recreated the public name.  An
            # existing fresh backup must be inspected before choosing another
            # pathname, otherwise the authentic old generation is orphaned.
            if not os.path.lexists(backup) and os.path.lexists(target):
                continue
            reported_error = FileExistsError(
                "rollback destination appeared after a committed move"
            )
        except BaseException as error:
            if os.path.lexists(target) and not os.path.lexists(backup):
                raise
            reported_error = error

        try:
            moved_identity, moved_digest = sha256_plain_file(backup)
        except BaseException as error:
            _restore_moved_racer(backup, target, error)
            if reported_error is not None:
                error.add_note(
                    "the no-replace isolation operation also reported: "
                    f"{reported_error}"
                )
            raise
        if not (
            moved_identity.device == identity.device
            and moved_identity.inode == identity.inode
            and moved_identity.size == identity.size
            and moved_identity.modified_ns == identity.modified_ns
            and moved_digest == snapshot.sha256
        ):
            mismatch = OSError(
                "render output changed while being isolated for rollback"
            )
            _restore_moved_racer(backup, target, mismatch)
            raise mismatch from reported_error

        claim = _PrivateFileClaim(
            path=moved_identity.path,
            file_key=(moved_identity.device, moved_identity.inode),
            parent_identity=moved_identity.parent_identity,
        )
        try:
            return _seal_private_file_claim(
                claim,
                expected_sha256=snapshot.sha256,
            )
        except BaseException as error:
            # A seal helper can fail after the exact old generation was
            # already isolated.  Restore that verified generation while the
            # public name is still vacant; never strand an ordinary handled
            # failure solely under a private backup name.
            if (
                _output_snapshot_matches_path(snapshot, backup)
                and not os.path.lexists(target)
            ):
                try:
                    _rename_noreplace(backup, target)
                except BaseException as restore_error:
                    if not (
                        not os.path.lexists(backup)
                        and _output_snapshot_matches_path(snapshot, target)
                    ):
                        error.add_note(
                            "the isolated old output could not be restored: "
                            f"{restore_error}"
                        )
                else:
                    if not _output_snapshot_matches_path(snapshot, target):
                        error.add_note(
                            "the isolated old output changed while it was "
                            "being restored; the public entry was left untouched"
                        )
            if os.path.lexists(backup):
                error.add_note(
                    "the isolated entry could not be sealed and was retained "
                    f"at {backup}"
                )
            raise
    raise RuntimeError("could not allocate a private rollback pathname")


def _retire_render_claims(
    claims: tuple[_PrivateFileClaim | None, ...],
) -> None:
    """Retire every stage without masking an active render failure."""

    primary_error = sys.exception()
    cleanup_errors: list[BaseException] = []
    for claim in claims:
        if claim is None:
            continue
        try:
            _retire_private_file(claim)
        except BaseException as cleanup_error:
            if primary_error is not None:
                primary_error.add_note(
                    "render staging cleanup was not completed: "
                    f"{cleanup_error}"
                )
            else:
                cleanup_errors.append(cleanup_error)
    if cleanup_errors:
        first = cleanup_errors[0]
        for cleanup_error in cleanup_errors[1:]:
            first.add_note(f"additional staging cleanup error: {cleanup_error}")
        raise first


def _sealed_file_matches_path(
    sealed: _SealedPrivateFileClaim,
    path: Path,
) -> bool:
    try:
        identity, digest = sha256_plain_file(path)
    except OSError:
        return False
    expected = sealed.identity
    return (
        identity.device == expected.device
        and identity.inode == expected.inode
        and identity.size == expected.size
        and identity.modified_ns == expected.modified_ns
        and digest == sealed.sha256
    )


def _revalidate_sealed_staging_file(
    sealed: _SealedPrivateFileClaim,
) -> Path:
    path = sealed.claim.path
    if not _sealed_file_matches_path(sealed, path):
        raise OSError(
            "verified render staging file changed before publication"
        )
    return path


def _preserve_transaction_file(
    sealed: _SealedPrivateFileClaim,
    path: Path,
    *,
    label: str,
) -> Path | None:
    """Move aside exactly one sealed public generation without clobbering."""

    path = Path(os.path.abspath(os.fspath(path)))
    if not os.path.lexists(path):
        return None
    parent = path.parent
    for _ in range(16):
        preserved = parent / (
            f".{path.name.lstrip('.')}.{label}-preserved-"
            f"{os.urandom(16).hex()}"
        )
        try:
            _rename_noreplace(path, preserved)
        except BaseException as move_error:
            # Native rename can commit before a wrapper reports any exception,
            # and the public name can be recreated immediately afterwards.
            # The random candidate's sealed identity is the only reliable
            # postcondition; source existence alone is not evidence either way.
            if _sealed_file_matches_path(sealed, preserved):
                return preserved
            if isinstance(move_error, FileExistsError) and (
                _sealed_file_matches_path(sealed, path)
            ):
                # The expected source is still public, so the fresh candidate
                # was merely occupied and another random name is safe to try.
                continue
            if (
                isinstance(move_error, FileNotFoundError)
                and not os.path.lexists(path)
                and not os.path.lexists(preserved)
            ):
                return None
            raise
        if _sealed_file_matches_path(sealed, preserved):
            return preserved
        mismatch = OSError(
            "public render artifact changed while being preserved for rollback"
        )
        _restore_moved_racer(preserved, path, mismatch)
        raise mismatch
    raise RuntimeError(f"cannot preserve render transaction file: {path}")


def _replace_published_file(
    sealed: _SealedPrivateFileClaim,
    target: Path,
) -> None:
    """Install one staged entry only while the public name remains absent."""

    staged = _revalidate_sealed_staging_file(sealed)
    _rename_noreplace(staged, target)


def _preserve_published_generation(
    sealed: _SealedPrivateFileClaim,
    target: Path,
) -> Path:
    """Move aside only the public generation owned by this transaction."""

    if not _sealed_file_matches_path(sealed, target):
        raise OSError("public render artifact is no longer owned by transaction")
    preserved = _preserve_transaction_file(
        sealed,
        target,
        label="rollback",
    )
    if preserved is None:
        raise OSError("published render artifact disappeared during rollback")
    return preserved


def _retire_withdrawn_published_generation(
    sealed: _SealedPrivateFileClaim,
    preserved: Path,
) -> None:
    """Best-effort retire an exact failed generation after public rollback."""

    try:
        identity, digest = sha256_plain_file(preserved)
        expected = sealed.identity
        if not (
            identity.device == expected.device
            and identity.inode == expected.inode
            and identity.size == expected.size
            and identity.modified_ns == expected.modified_ns
            and digest == sealed.sha256
        ):
            raise OSError(
                "withdrawn render generation changed before cleanup"
            )
        claim = _PrivateFileClaim(
            path=identity.path,
            file_key=(identity.device, identity.inode),
            parent_identity=identity.parent_identity,
        )
        recovery = _seal_private_file_claim(
            claim,
            expected_sha256=sealed.sha256,
        )
        _retire_private_file(recovery.claim)
    except BaseException as cleanup_error:
        try:
            warnings.warn(
                "withdrawn failed render was retained because identity-bound "
                f"cleanup was not completed at {preserved}: {cleanup_error}",
                RuntimeWarning,
                stacklevel=2,
            )
        except BaseException:
            pass


def _restore_published_file(
    backup: _SealedPrivateFileClaim,
    target: Path,
) -> None:
    """Restore one sealed old target without clobbering another writer."""

    source = _revalidate_sealed_staging_file(backup)
    if os.path.lexists(target):
        raise FileExistsError(
            "cannot restore an old render over a concurrent public target"
        )
    reported_error: BaseException | None = None
    try:
        _rename_noreplace(source, target)
    except BaseException as error:
        # The native move may have committed before a wrapper reported an
        # error.  When the source disappeared, always inspect the public
        # postcondition below; an unverified installed entry must be moved out
        # of the public namespace before the error is propagated.
        if os.path.lexists(source):
            raise
        reported_error = error
    if _sealed_file_matches_path(backup, target):
        return

    mismatch = OSError(
        "restored render output does not match its sealed backup; the public "
        "entry was left untouched"
    )
    raise mismatch from reported_error


def _restore_withdrawn_published_generation(
    sealed: _SealedPrivateFileClaim,
    preserved: Path,
    target: Path,
) -> None:
    """Reinstall a withdrawn new generation if old rollback cannot proceed."""

    if not _sealed_file_matches_path(sealed, preserved):
        raise OSError("withdrawn render generation changed before fallback")
    if os.path.lexists(target):
        raise FileExistsError(
            "cannot restore withdrawn render over a public replacement"
        )
    reported_error: BaseException | None = None
    try:
        _rename_noreplace(preserved, target)
    except BaseException as error:
        if os.path.lexists(preserved):
            raise
        reported_error = error
    if _sealed_file_matches_path(sealed, target):
        return
    raise OSError(
        "withdrawn render fallback does not own the public target; the public "
        "entry was left untouched"
    ) from reported_error


def _retire_output_backups(
    backups: tuple[_SealedPrivateFileClaim, ...],
) -> None:
    """Retire every unprotected rollback entry without masking an error."""

    primary_error = sys.exception()
    cleanup_errors: list[BaseException] = []
    for backup in backups:
        try:
            _retire_private_file(backup.claim)
        except BaseException as cleanup_error:
            if primary_error is not None:
                primary_error.add_note(
                    "render rollback cleanup was not completed: "
                    f"{cleanup_error}"
                )
            else:
                cleanup_errors.append(cleanup_error)
    if cleanup_errors:
        first = cleanup_errors[0]
        for cleanup_error in cleanup_errors[1:]:
            first.add_note(f"additional rollback cleanup error: {cleanup_error}")
        raise first


def _publish_staged_artifacts(
    staged_targets: tuple[
        tuple[_SealedPrivateFileClaim, Path],
        ...,
    ],
) -> None:
    """Publish a prepared artifact set and roll back ordinary exceptions.

    Each old target is moved to a sealed private entry before its replacement.
    Both removal and installation use no-replace renames.  The caller orders
    every sidecar first and the WAV last, making the WAV installation the
    successful transaction's commit marker.  Multiple filesystem entries
    cannot be made crash-atomic; handled failures are rolled back only while
    each public generation is still demonstrably owned by this transaction.
    """

    snapshots: dict[Path, _OutputTargetSnapshot | None] = {}
    backups: dict[Path, _SealedPrivateFileClaim | None] = {}
    published: dict[Path, _SealedPrivateFileClaim] = {}
    ambiguous_targets: set[Path] = set()
    protected_backups: set[Path] = set()
    try:
        # Validate every staged generation before touching any public target.
        for sealed, _ in staged_targets:
            _revalidate_sealed_staging_file(sealed)
        requested_targets = tuple(
            (
                sealed,
                Path(os.path.abspath(os.fspath(target))),
            )
            for sealed, target in staged_targets
        )
        _validate_output_targets(
            tuple(target for _, target in requested_targets)
        )
        bound_targets: list[tuple[_SealedPrivateFileClaim, Path]] = []
        for sealed, requested in requested_targets:
            bound = bind_plain_sibling_path(
                requested,
                sealed.claim.parent_identity,
                message=(
                    "render output target escaped its verified parent"
                ),
            )
            snapshot = _capture_output_target(requested)
            target = bound
            if snapshot is not None:
                snapshot_parent = snapshot.identity.parent_identity
                claim_parent = sealed.claim.parent_identity
                if (
                    snapshot.identity.path.parent != bound.parent
                    or snapshot_parent.device != claim_parent.device
                    or snapshot_parent.inode != claim_parent.inode
                ):
                    raise OSError(
                        "render output target escaped its verified parent"
                    )
                # Existing 8.3 aliases, including an aliased final component,
                # are upgraded to the exact canonical path captured through
                # the descriptor.  Every later move, install, and rollback
                # therefore stays below the authorised directory spelling.
                target = snapshot.identity.path
            if target in snapshots:
                raise ValueError(
                    "render transaction contains duplicate output targets"
                )
            snapshots[target] = snapshot
            bound_targets.append((sealed, target))
        staged_targets = tuple(bound_targets)

        try:
            for sealed, target in staged_targets:
                _revalidate_sealed_staging_file(sealed)
                snapshot = snapshots[target]
                if snapshot is None:
                    if os.path.lexists(target):
                        raise OSError(
                            "render output target appeared before publication"
                        )
                    backup = None
                else:
                    backup = _isolate_output_target(target, snapshot)
                backups[target] = backup
                try:
                    _replace_published_file(sealed, target)
                except BaseException:
                    # A native rename can commit before a wrapper reports an
                    # error.  Only an exact sealed match counts as ours.
                    if _sealed_file_matches_path(sealed, target):
                        published[target] = sealed
                    elif (
                        not os.path.lexists(sealed.claim.path)
                        and os.path.lexists(target)
                    ):
                        ambiguous_targets.add(target)
                    raise
                if not _sealed_file_matches_path(sealed, target):
                    # A correct install can be replaced immediately by a
                    # non-cooperating public writer.  Path-only postconditions
                    # cannot distinguish that from a same-user attack against
                    # the unpredictable private source name.  Preserve the
                    # public owner; private-name attacks are outside the local
                    # cooperative-writer threat boundary documented by the
                    # atomic publication primitives.
                    ambiguous_targets.add(target)
                    raise OSError(
                        "published render artifact differs from its verified "
                        "stage; the public entry was left untouched"
                    )
                published[target] = sealed
        except BaseException as publication_error:
            rollback_errors: list[str] = []
            for _, target in reversed(staged_targets):
                if target not in backups:
                    continue
                backup = backups[target]
                owned_public = published.get(target)
                withdrawn: Path | None = None
                try:
                    if target in ambiguous_targets:
                        if backup is not None:
                            protected_backups.add(backup.claim.path)
                        raise OSError(
                            "publication ownership became ambiguous; the public "
                            "entry was left untouched"
                        )
                    if owned_public is not None:
                        if not _sealed_file_matches_path(
                            owned_public,
                            target,
                        ):
                            if backup is not None:
                                protected_backups.add(backup.claim.path)
                            raise OSError(
                                "public target was replaced by another writer; "
                                "it was left untouched"
                            )
                        if backup is not None:
                            # Never withdraw the current verified public
                            # generation until the old generation is still
                            # demonstrably restorable.
                            _revalidate_sealed_staging_file(backup)
                        withdrawn = _preserve_published_generation(
                            owned_public,
                            target,
                        )
                    elif os.path.lexists(target):
                        if backup is None:
                            # The transaction never owned this new target and
                            # there is no old generation to restore.
                            continue
                        protected_backups.add(backup.claim.path)
                        raise OSError(
                            "a concurrent public target prevents restoration; "
                            "it was left untouched"
                        )
                    if backup is not None:
                        _restore_published_file(backup, target)
                    if withdrawn is not None:
                        # Cleanup follows successful public rollback.  It is
                        # best-effort and rebinds to the original sealed
                        # identity, so a replacement at the recovery name is
                        # never adopted or allowed to block restoration.
                        _retire_withdrawn_published_generation(
                            owned_public,
                            withdrawn,
                        )
                except BaseException as rollback_error:
                    if (
                        withdrawn is not None
                        and owned_public is not None
                        and not os.path.lexists(target)
                    ):
                        try:
                            _restore_withdrawn_published_generation(
                                owned_public,
                                withdrawn,
                                target,
                            )
                        except BaseException as fallback_error:
                            rollback_error.add_note(
                                "the withdrawn new generation could not be "
                                f"restored after rollback failure: {fallback_error}"
                            )
                    if backup is not None:
                        protected_backups.add(backup.claim.path)
                    rollback_errors.append(f"{target}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    "渲染输出发布失败，且四件套回滚不完整；"
                    "保留了可用的 .tianlai-backup 文件："
                    + "; ".join(rollback_errors)
                ) from publication_error
            raise
    finally:
        _retire_output_backups(
            tuple(
                backup
                for backup in backups.values()
                if backup is not None
                and backup.claim.path not in protected_backups
            )
        )


def _render_to_wav_atomic_locked(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    output_path: str | Path,
    *,
    _inputs: _SingleRenderInputs,
) -> RenderResult:
    """Render and publish one strict WAV with its three matching sidecars.

    All four files are prepared and verified beside the destination before
    publication.  Handled publication failures restore the previous four-file
    state; this does not claim crash atomicity across four filesystem entries.
    """

    del instrument_manifest_path, performance_path
    manifest_path = revalidate_plain_file(_inputs.manifest.identity)
    revalidate_plain_file(_inputs.performance.identity)
    manifest = _inputs.manifest.document
    manifest_sha256 = _inputs.manifest.sha256
    performance_document = _inputs.performance.document
    limits = _inputs.limits
    validate_performance_document_resource_limits(
        performance_document,
        limits,
    )
    performance = _inputs.parsed_performance
    validate_single_render_resource_limits(performance, limits)
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
    temporary_claim = _reserve_private_file(
        audio_path.parent,
        prefix=f".{audio_path.name}.",
        suffix=".tianlai-part",
    )
    temporary = temporary_claim.path
    staged_sidecar_claim: _PrivateFileClaim | None = None
    staged_attribution_claim: _PrivateFileClaim | None = None
    staged_post_render_check_claim: _PrivateFileClaim | None = None

    try:
        instrument = create_instrument(
            manifest,
            performance.sample_rate,
            base_directory=str(manifest_path.parent),
        )
        try:
            if _prefer_frame_stream_path(instrument, performance):
                frames, peak = render_document(instrument, performance)
                frame_count = _write_claimed_wav_pcm24(
                    temporary_claim,
                    frames,
                    performance.sample_rate,
                )
            else:
                blocks, peak = render_document_blocks(
                    instrument,
                    performance,
                )
                frame_count = _write_wav_pcm24_blocks(
                    temporary_claim,
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

        staged_sidecar_claim = _reserve_sibling_staging_file(
            audio_path.parent,
            ".许可与署名.json.tianlai-stage",
        )
        staged_attribution_claim = _reserve_sibling_staging_file(
            audio_path.parent,
            ".许可与署名.txt.tianlai-stage",
        )
        staged_post_render_check_claim = _reserve_sibling_staging_file(
            audio_path.parent,
            ".渲染后自检.json.tianlai-stage",
        )
        staged_sidecar_path = staged_sidecar_claim.path
        staged_attribution_path = staged_attribution_claim.path
        staged_post_render_check_path = staged_post_render_check_claim.path
        sidecars = write_license_sidecars(
            staged_sidecar_path,
            staged_attribution_path,
            instrument_uses=(
                InstrumentUse(
                    manifest_path=manifest_path,
                    used_by=("single_instrument_render",),
                    expected_sha256=manifest_sha256,
                    manifest_bytes=_inputs.manifest.payload,
                ),
            ),
            audio_artifacts=(
                AudioArtifact(
                    role="render",
                    path=temporary,
                    label=audio_path.name,
                ),
            ),
            _json_claim=staged_sidecar_claim,
            _text_claim=staged_attribution_claim,
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
        expected_post_render_check_sha256 = hashlib.sha256(
            (
                json.dumps(
                    post_render_check,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        ).hexdigest()
        write_post_render_check(
            staged_post_render_check_path,
            post_render_check,
            _claim=staged_post_render_check_claim,
        )
        _verify_staged_post_render_check(
            staged_post_render_check_path,
            expected_report=post_render_check,
        )
        staged_sidecar = _seal_private_file_claim(
            staged_sidecar_claim,
            expected_sha256=sidecars.json_sha256,
        )
        staged_attribution = _seal_private_file_claim(
            staged_attribution_claim,
            expected_sha256=sidecars.text_sha256,
        )
        staged_post_render_check = _seal_private_file_claim(
            staged_post_render_check_claim,
            expected_sha256=expected_post_render_check_sha256,
        )
        staged_audio = _seal_private_file_claim(
            temporary_claim,
            expected_sha256=str(
                post_render_check["artifact"]["sha256"]
            ),
        )
        # Move each verified generation away from its earlier, observable
        # staging name before entering the public namespace transaction.  The
        # new claims must replace the old generations so failure cleanup stays
        # identity-bound even when only part of this sequence completes.
        staged_sidecar = _relocate_sealed_private_file(
            staged_sidecar,
            stem=f"{sidecar_path.name}.publish-transfer",
        )
        staged_sidecar_claim = staged_sidecar.claim
        staged_attribution = _relocate_sealed_private_file(
            staged_attribution,
            stem=f"{attribution_path.name}.publish-transfer",
        )
        staged_attribution_claim = staged_attribution.claim
        staged_post_render_check = _relocate_sealed_private_file(
            staged_post_render_check,
            stem=f"{post_render_check_path.name}.publish-transfer",
        )
        staged_post_render_check_claim = staged_post_render_check.claim
        staged_audio = _relocate_sealed_private_file(
            staged_audio,
            stem=f"{audio_path.name}.publish-transfer",
        )
        temporary_claim = staged_audio.claim
        _publish_staged_artifacts(
            (
                (staged_sidecar, sidecar_path),
                (staged_attribution, attribution_path),
                (
                    staged_post_render_check,
                    post_render_check_path,
                ),
                (staged_audio, audio_path),
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
        # remove only entries whose creation-time identities still match.
        # A replacement or unverifiable entry is retained with a warning.
        _retire_render_claims(
            (
                temporary_claim,
                staged_sidecar_claim,
                staged_attribution_claim,
                staged_post_render_check_claim,
            )
        )


def render_to_wav_atomic(
    instrument_manifest_path: str | Path,
    performance_path: str | Path,
    output_path: str | Path,
    *,
    _manifest_validator: Callable[[dict[str, Any]], None] | None = None,
) -> RenderResult:
    """Render and publish one four-artifact set under exclusive ownership.

    Both JSON inputs must be stable, single-link plain files below plain local
    directories.  Symlinks, hard links and reparse points are rejected so a
    pathname cannot silently change generations during a render.
    """

    # Preserve the public target-validation contract before asking the lock
    # layer to bind the WAV pathname.  The locked implementation repeats this
    # check, so a target swapped between these two points still fails closed.
    # Freeze the output namespace before reading inputs or invoking any
    # validator/instrument callback.  A relative path must not be reinterpreted
    # if user-provided code changes the process CWD between validation, lock
    # acquisition, staging, and publication.  ``abspath`` does not resolve or
    # follow the final target, whose link policy is checked separately.
    audio_path = Path(os.path.abspath(os.fspath(output_path)))
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

    # Capture both inputs once through verified, size-bounded descriptors.
    # The immutable payloads are the exact objects rendered and attributed;
    # revalidation after locking rejects a pathname replacement while waiting.
    inputs = _capture_single_render_inputs(
        instrument_manifest_path,
        performance_path,
        manifest_validator=_manifest_validator,
    )

    # Keep one operating-system lock from the first target check through the
    # final WAV commit marker and every rollback path.  Without it, two CLI
    # renders can interleave their four os.replace operations and publish a WAV
    # whose license or post-check belongs to the other render.
    with acquire_render_lock(audio_path, existing_target_kind="file"):
        return _render_to_wav_atomic_locked(
            inputs.manifest.path,
            inputs.performance.path,
            audio_path,
            _inputs=inputs,
        )
