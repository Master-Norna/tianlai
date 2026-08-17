"""Authoritative PCM-24 execution for the first formal Score-v2 slice.

This module consumes a live :class:`ScoreV2OscillatorRuntimeAuthority`; it
never reconstructs authority from JSON.  The output directory is expected to
be a private Candidate-v3 staging generation.  Audio is written through an
identity-bound private claim and installed at the fixed mix name only after
the exact stream has been sealed.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any
import wave
import weakref

from .atomic_publish import (
    _close_private_file_claim,
    _capture_file,
    _install_sealed_private_file,
    _open_private_file_claim,
    _reserve_private_file,
    _retire_private_file,
    _retire_sealed_private_file,
    _seal_private_file_claim,
    _same_file,
    _SealedPrivateFileClaim,
)
from .audio import _SequentialDigestWriter, _write_numpy_pcm24
from .canonical_json import canonical_json_bytes
from .resource_limits import (
    ProjectLimits,
    ResourceLimitError,
    performance_event_limit,
)
from .score_v2_candidate import SCORE_V2_MIX_NAME
from .score_v2_performance import ScoreV2PerformanceBundle
from .score_v2_renderer import (
    _decoded_execution_documents,
    _new_audio_stream_hasher,
)
from .score_v2_runtime_source import NO_EXTERNAL_ASSET_INVENTORY_STATUS
from .score_v2_runtime_authority import (
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT,
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_KIND,
    SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_SCHEMA_VERSION,
    SCORE_V2_RUNTIME_AUTHORITY_CONTRACT,
    SCORE_V2_RUNTIME_AUTHORITY_KIND,
    SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION,
    ScoreV2OscillatorRuntimeAuthority,
    ScoreV2RuntimeAuthorityError,
)


SCORE_V2_FORMAL_RENDER_CONTRACT = (
    "score-v2-formal-render-single-oscillator-pcm24-v1"
)
SCORE_V2_POST_RENDER_CHECK_KIND = "tianlai.score_v2_post_render_check"
SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION = 1
SCORE_V2_POST_RENDER_CHECK_CONTRACT = (
    "score-v2-post-render-check-pcm24-stream-v1"
)

_PCM24_HEADER_BYTES = 44
_PCM24_STEREO_BYTES_PER_FRAME = 6
_RIFF_MAX_CHUNK_SIZE = (1 << 32) - 1
_MAX_BLOCK_FRAMES = 65_536
_STREAMING_BYTES_PER_FRAME = 64


class ScoreV2FormalRenderError(ValueError):
    """A stable failure in the authoritative Score-v2 PCM transaction."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message_key = f"scoreV2FormalRender.{code.replace('.', '_')}"
        super().__init__(code)


def _add_note_safely(error: BaseException, note: str) -> None:
    try:
        error.add_note(note)
    except BaseException:
        pass


@dataclass(frozen=True, slots=True)
class _FormalGeneration:
    mix_path: Path
    sealed_mix: _SealedPrivateFileClaim
    authority: ScoreV2OscillatorRuntimeAuthority
    executor_id: str
    part_id: str
    performance_bundle_sha256: str
    runtime_source_sha256: str
    mix_sha256: str
    mix_size_bytes: int
    sample_rate: int
    frame_count: int
    block_count: int
    event_count: int
    endpoint_event_count: int
    peak_active_voices: int
    peak: float
    active_sample_count: int
    float_stream_sha256: str
    performance_sha256: str
    event_sidecar_sha256: str
    effective_manifest_sha256: str
    factory_generation_sha256: str
    runtime_authority_acquisition_canonical_bytes: bytes
    runtime_authority_acquisition_sha256: str
    runtime_authority_canonical_bytes: bytes
    runtime_authority_sha256: str
    runtime_manifest_bytes: bytes
    runtime_manifest_sha256: str
    post_render_check_canonical_bytes: bytes
    post_render_check_sha256: str


_FORMAL_GENERATIONS: dict[
    int,
    tuple[weakref.ReferenceType[object], _FormalGeneration],
] = {}


class ScoreV2FormalRenderGeneration:
    """Unforgeable in-process handle for one completed formal generation."""

    __slots__ = ("__weakref__",)

    def __init_subclass__(cls, **_kwargs: object) -> None:
        raise TypeError("ScoreV2FormalRenderGeneration cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2FormalRenderGeneration is created only by the formal renderer"
        )

    def _trusted_generation(self) -> _FormalGeneration:
        try:
            registered = _FORMAL_GENERATIONS.get(id(self))
            if registered is None or registered[0]() is not self:
                raise ValueError
            generation = registered[1]
            if (
                hashlib.sha256(
                    generation.runtime_authority_acquisition_canonical_bytes
                ).hexdigest()
                != generation.runtime_authority_acquisition_sha256
                or hashlib.sha256(
                    generation.runtime_authority_canonical_bytes
                ).hexdigest()
                != generation.runtime_authority_sha256
                or hashlib.sha256(generation.runtime_manifest_bytes).hexdigest()
                != generation.runtime_manifest_sha256
                or hashlib.sha256(
                    generation.post_render_check_canonical_bytes
                ).hexdigest()
                != generation.post_render_check_sha256
                or generation.mix_sha256 != generation.sealed_mix.sha256
                or generation.mix_size_bytes != generation.sealed_mix.identity.size
                or generation.mix_path != generation.sealed_mix.identity.path
                or generation.mix_path != generation.sealed_mix.claim.path
            ):
                raise ValueError
            _validate_retained_evidence(generation)
            return generation
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2FormalRenderError(
                "render.evidence_integrity_mismatch"
            ) from exc

    @property
    def contract(self) -> str:
        self._trusted_generation()
        return SCORE_V2_FORMAL_RENDER_CONTRACT

    @property
    def mix_path(self) -> str:
        return str(self._trusted_generation().mix_path)

    def revalidate_mix(self) -> None:
        generation = self._trusted_generation()
        try:
            identity, digest = _capture_file(generation.mix_path)
            if not _same_file(
                generation.sealed_mix.identity,
                identity,
                left_sha256=generation.sealed_mix.sha256,
                right_sha256=digest,
            ):
                raise ValueError
        except (OSError, TypeError, ValueError) as exc:
            raise ScoreV2FormalRenderError(
                "render.mix_generation_changed"
            ) from exc

    def revalidate_generation(self) -> None:
        """Revalidate the live lease, retained evidence and installed WAV."""

        generation = self._trusted_generation()
        try:
            generation.authority.checkpoint(full_sources=True)
            if (
                generation.authority.executor_id != generation.executor_id
                or generation.authority.part_id != generation.part_id
                or generation.authority.sample_rate != generation.sample_rate
                or generation.authority.frame_count != generation.frame_count
                or generation.authority.performance_bundle_sha256
                != generation.performance_bundle_sha256
                or generation.authority.runtime_source_sha256
                != generation.runtime_source_sha256
                or generation.authority.effective_manifest_sha256
                != generation.effective_manifest_sha256
                or generation.authority.manifest_raw_sha256
                != generation.runtime_manifest_sha256
                or generation.authority.manifest_bytes
                != generation.runtime_manifest_bytes
                or generation.authority.factory_generation_sha256
                != generation.factory_generation_sha256
                or generation.authority.acquisition_canonical_bytes
                != generation.runtime_authority_acquisition_canonical_bytes
                or generation.authority.acquisition_sha256
                != generation.runtime_authority_acquisition_sha256
                or generation.authority.consumed_canonical_bytes
                != generation.runtime_authority_canonical_bytes
                or generation.authority.consumed_sha256
                != generation.runtime_authority_sha256
            ):
                raise ValueError
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ScoreV2FormalRenderError(
                "render.runtime_authority_inactive"
            ) from exc
        self.revalidate_mix()

    def post_render_check(self) -> dict[str, object]:
        value = json.loads(
            self._trusted_generation().post_render_check_canonical_bytes
        )
        if type(value) is not dict:
            raise ScoreV2FormalRenderError("render.evidence_integrity_mismatch")
        return value

    def runtime_authority(self) -> dict[str, object]:
        value = json.loads(
            self._trusted_generation().runtime_authority_canonical_bytes
        )
        if type(value) is not dict:
            raise ScoreV2FormalRenderError("render.evidence_integrity_mismatch")
        return value

    def runtime_authority_acquisition(self) -> dict[str, object]:
        value = json.loads(
            self._trusted_generation().runtime_authority_acquisition_canonical_bytes
        )
        if type(value) is not dict:
            raise ScoreV2FormalRenderError("render.evidence_integrity_mismatch")
        return value


def _formal_property(name: str):
    def read(self: ScoreV2FormalRenderGeneration):
        return getattr(self._trusted_generation(), name)

    return property(read)


for _property_name in (
    "mix_sha256",
    "mix_size_bytes",
    "sample_rate",
    "frame_count",
    "block_count",
    "event_count",
    "endpoint_event_count",
    "peak_active_voices",
    "peak",
    "active_sample_count",
    "float_stream_sha256",
    "performance_sha256",
    "event_sidecar_sha256",
    "effective_manifest_sha256",
    "factory_generation_sha256",
    "runtime_authority_acquisition_canonical_bytes",
    "runtime_authority_acquisition_sha256",
    "runtime_authority_canonical_bytes",
    "runtime_authority_sha256",
    "runtime_manifest_bytes",
    "runtime_manifest_sha256",
    "post_render_check_canonical_bytes",
    "post_render_check_sha256",
):
    setattr(ScoreV2FormalRenderGeneration, _property_name, _formal_property(_property_name))


def _register_formal_generation(
    handle: ScoreV2FormalRenderGeneration,
    generation: _FormalGeneration,
) -> None:
    handle_id = id(handle)

    def remove(
        reference: weakref.ReferenceType[object],
        *,
        expected_id: int = handle_id,
    ) -> None:
        current = _FORMAL_GENERATIONS.get(expected_id)
        if current is not None and current[0] is reference:
            _FORMAL_GENERATIONS.pop(expected_id, None)

    reference = weakref.ref(handle, remove)
    _FORMAL_GENERATIONS[handle_id] = (reference, generation)


def _active_limits(limits: ProjectLimits | None) -> ProjectLimits:
    if limits is None:
        return ProjectLimits.from_environment()
    if type(limits) is not ProjectLimits:
        raise TypeError("limits must be ProjectLimits")
    for name in ProjectLimits.__dataclass_fields__:
        value = getattr(limits, name)
        if type(value) is not int or value < 1:
            raise ValueError("ProjectLimits fields must be positive integers")
    return limits


def _validate_retained_evidence(generation: _FormalGeneration) -> None:
    """Reject hash-consistent evidence spliced from a different generation."""

    acquisition = json.loads(
        generation.runtime_authority_acquisition_canonical_bytes
    )
    consumed = json.loads(generation.runtime_authority_canonical_bytes)
    postcheck = json.loads(generation.post_render_check_canonical_bytes)
    if not all(type(value) is dict for value in (acquisition, consumed, postcheck)):
        raise ValueError
    if (
        canonical_json_bytes(acquisition)
        != generation.runtime_authority_acquisition_canonical_bytes
        or canonical_json_bytes(consumed)
        != generation.runtime_authority_canonical_bytes
        or canonical_json_bytes(postcheck)
        != generation.post_render_check_canonical_bytes
    ):
        raise ValueError
    acquisition_bindings = acquisition.get("bindings")
    consumed_bindings = consumed.get("bindings")
    acquisition_executor = acquisition.get("executor")
    consumed_executor = consumed.get("executor")
    acquisition_loaded = acquisition.get("loaded_python_generation")
    consumed_loaded = consumed.get("loaded_python_generation")
    held_sources = acquisition.get("held_sources")
    lifecycle = consumed.get("lifecycle")
    if not all(
        type(value) is dict
        for value in (
            acquisition_bindings,
            consumed_bindings,
            acquisition_executor,
            consumed_executor,
            acquisition_loaded,
            consumed_loaded,
            lifecycle,
        )
    ) or type(held_sources) is not list:
        raise ValueError
    common_bindings = {
        "performance_bundle_sha256": generation.performance_bundle_sha256,
        "runtime_source_sha256": generation.runtime_source_sha256,
        "effective_manifest_sha256": generation.effective_manifest_sha256,
        "manifest_raw_sha256": generation.runtime_manifest_sha256,
        "sample_rate": generation.sample_rate,
    }
    if any(
        acquisition_bindings.get(name) != value
        or consumed_bindings.get(name) != value
        for name, value in common_bindings.items()
    ):
        raise ValueError
    for name in (
        "capability_plan_sha256",
        "capability_source_sha256",
        "roster_projection_sha256",
    ):
        value = acquisition_bindings.get(name)
        if consumed_bindings.get(name) != value:
            raise ValueError
    expected_executor = {
        "executor_order": 0,
        "executor_id": generation.executor_id,
        "part_id": generation.part_id,
    }
    expected_assets = {
        "policy": "no_external_audio_assets",
        "descriptor_count": 0,
        "descriptors": [],
        "inventory_status": NO_EXTERNAL_ASSET_INVENTORY_STATUS,
    }
    if (
        acquisition.get("kind")
        != SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_KIND
        or acquisition.get("schema_version")
        != SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_SCHEMA_VERSION
        or acquisition.get("contract")
        != SCORE_V2_RUNTIME_AUTHORITY_ACQUISITION_CONTRACT
        or consumed.get("kind") != SCORE_V2_RUNTIME_AUTHORITY_KIND
        or consumed.get("schema_version")
        != SCORE_V2_RUNTIME_AUTHORITY_SCHEMA_VERSION
        or consumed.get("contract") != SCORE_V2_RUNTIME_AUTHORITY_CONTRACT
        or consumed.get("status") != "consumed"
        or acquisition_executor != expected_executor
        or consumed_executor != expected_executor
        or acquisition.get("assets") != expected_assets
        or consumed.get("assets") != expected_assets
        or acquisition.get("factory_generation_sha256")
        != generation.factory_generation_sha256
        or consumed.get("factory_generation_sha256")
        != generation.factory_generation_sha256
        or consumed_bindings.get("acquisition_sha256")
        != generation.runtime_authority_acquisition_sha256
        or acquisition_loaded.get("projection_sha256")
        != consumed_loaded.get("projection_sha256")
        or consumed_loaded.get("held_source_count") != len(held_sources)
        or lifecycle.get("lease_consumed_once") is not True
        or lifecycle.get("execution_retired_before_receipt") is not True
        or lifecycle.get("source_descriptors_held_until_context_exit") is not True
        or lifecycle.get("dispatched_event_count") != generation.event_count
        or lifecycle.get("rendered_frame_count") != generation.frame_count
    ):
        raise ValueError
    expected_postcheck = _post_render_check_document(
        mix_sha256=generation.mix_sha256,
        mix_size_bytes=generation.mix_size_bytes,
        performance_bundle_sha256=generation.performance_bundle_sha256,
        runtime_authority_sha256=generation.runtime_authority_sha256,
        sample_rate=generation.sample_rate,
        frame_count=generation.frame_count,
        peak=generation.peak,
        active_sample_count=generation.active_sample_count,
        event_count=generation.event_count,
        endpoint_event_count=generation.endpoint_event_count,
    )
    summary = postcheck.get("summary")
    if (
        postcheck != expected_postcheck
        or type(summary) is not dict
        or summary.get("can_proceed") is not True
    ):
        raise ValueError


def _post_render_check_document(
    *,
    mix_sha256: str,
    mix_size_bytes: int,
    performance_bundle_sha256: str,
    runtime_authority_sha256: str,
    sample_rate: int,
    frame_count: int,
    peak: float,
    active_sample_count: int,
    event_count: int,
    endpoint_event_count: int,
) -> dict[str, object]:
    expected_activity = event_count > 0
    observed_activity = active_sample_count > 0
    can_proceed = (not expected_activity) or observed_activity
    return {
        "kind": SCORE_V2_POST_RENDER_CHECK_KIND,
        "schema_version": SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION,
        "contract": SCORE_V2_POST_RENDER_CHECK_CONTRACT,
        "status": "pass" if can_proceed else "fail",
        "bindings": {
            "performance_bundle_sha256": performance_bundle_sha256,
            "runtime_authority_sha256": runtime_authority_sha256,
        },
        "artifact": {
            "path": SCORE_V2_MIX_NAME,
            "sha256": mix_sha256,
            "size_bytes": mix_size_bytes,
        },
        "audio_format": {
            "container": "WAV",
            "encoding": "PCM",
            "bits_per_sample": 24,
            "channels": 2,
            "sample_rate": sample_rate,
            "frame_count": frame_count,
        },
        "observations": {
            "peak": peak,
            "active_sample_count": active_sample_count,
            "event_count": event_count,
            "endpoint_event_count": endpoint_event_count,
        },
        "summary": {
            "can_proceed": can_proceed,
            "expected_activity": expected_activity,
            "observed_activity": observed_activity,
        },
        "limitations": {
            "loudness_standard_measurement": "not_performed",
            "true_peak_measurement": "not_performed",
            "release_tail": "not_present",
            "source": "same_descriptor_stream_evidence",
        },
    }


def render_score_v2_formal_pcm24_generation(
    bundle: ScoreV2PerformanceBundle,
    authority: ScoreV2OscillatorRuntimeAuthority,
    *,
    output_directory: str | os.PathLike[str],
    limits: ProjectLimits | None = None,
    maximum_block_frames: int = _MAX_BLOCK_FRAMES,
) -> ScoreV2FormalRenderGeneration:
    """Consume one live authority and install a sealed Candidate-v3 mix WAV."""

    if type(bundle) is not ScoreV2PerformanceBundle:
        raise TypeError("bundle must be ScoreV2PerformanceBundle")
    if type(authority) is not ScoreV2OscillatorRuntimeAuthority:
        raise TypeError("authority must be ScoreV2OscillatorRuntimeAuthority")
    if type(maximum_block_frames) is not int or not 1 <= maximum_block_frames <= _MAX_BLOCK_FRAMES:
        raise ValueError("maximum_block_frames must be an integer from 1 to 65536")
    active_limits = _active_limits(limits)
    try:
        authority.checkpoint(full_sources=True)
        executor_id = authority.executor_id
        local = bundle._local_execution_input_for_executor(executor_id)
        if (
            bundle.artifact_sha256 != authority.performance_bundle_sha256
            or bundle.runtime_source_sha256 != authority.runtime_source_sha256
            or bundle.sample_rate != authority.sample_rate
            or bundle.frame_count != authority.frame_count
            or local.runtime.effective_manifest_sha256
            != authority.effective_manifest_sha256
        ):
            raise ScoreV2FormalRenderError("render.authority_binding_mismatch")
        acquisition_bytes = authority.acquisition_canonical_bytes
        acquisition_hash = authority.acquisition_sha256
        if hashlib.sha256(acquisition_bytes).hexdigest() != acquisition_hash:
            raise ScoreV2FormalRenderError("render.authority_evidence_mismatch")
    except ScoreV2FormalRenderError:
        raise
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2FormalRenderError("render.execution_input_invalid") from exc

    sample_rate = bundle.sample_rate
    frame_count = bundle.frame_count
    expected_size = _PCM24_HEADER_BYTES + _PCM24_STEREO_BYTES_PER_FRAME * frame_count
    if _PCM24_STEREO_BYTES_PER_FRAME * frame_count + 36 > _RIFF_MAX_CHUNK_SIZE:
        raise ScoreV2FormalRenderError("render.riff_size_limit_exceeded")
    if expected_size > active_limits.max_primary_output_bytes:
        raise ResourceLimitError(
            "render.output_budget_exceeded",
            "formal Score-v2 WAV exceeds max_primary_output_bytes",
            actual=expected_size,
            limit=active_limits.max_primary_output_bytes,
        )
    if frame_count > sample_rate * active_limits.max_plan_seconds:
        raise ResourceLimitError(
            "render.duration_too_long",
            "formal Score-v2 render exceeds max_plan_seconds",
            actual=frame_count / sample_rate,
            limit=active_limits.max_plan_seconds,
        )
    affordable = active_limits.max_audio_memory_bytes // _STREAMING_BYTES_PER_FRAME
    if affordable < 1:
        raise ResourceLimitError(
            "render.memory_budget_exceeded",
            "formal Score-v2 streaming buffer exceeds max_audio_memory_bytes",
            actual=_STREAMING_BYTES_PER_FRAME,
            limit=active_limits.max_audio_memory_bytes,
        )
    event_limit = performance_event_limit(active_limits)
    if bundle.event_count > event_limit:
        raise ResourceLimitError(
            "render.too_many_events",
            "formal Score-v2 event count exceeds the event budget",
            actual=bundle.event_count,
            limit=event_limit,
        )
    if len(local.performance_canonical_bytes) > active_limits.max_score_json_bytes:
        raise ResourceLimitError(
            "render.performance_document_too_large",
            "formal Score-v2 performance transport exceeds the score JSON budget",
            actual=len(local.performance_canonical_bytes),
            limit=active_limits.max_score_json_bytes,
        )
    for payload in (
        local.event_sidecar_canonical_bytes,
        bundle.canonical_bytes,
    ):
        if len(payload) > active_limits.max_plan_json_bytes:
            raise ResourceLimitError(
                "render.execution_bundle_too_large",
                "formal Score-v2 execution evidence exceeds the plan JSON budget",
                actual=len(payload),
                limit=active_limits.max_plan_json_bytes,
            )
    try:
        performance, sidecar = _decoded_execution_documents(
            local.performance_canonical_bytes,
            local.event_sidecar_canonical_bytes,
            sample_rate=sample_rate,
            frame_count=frame_count,
        )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2FormalRenderError("render.execution_input_invalid") from exc
    block_limit = min(maximum_block_frames, affordable, frame_count)
    directory = Path(os.path.abspath(os.fspath(output_directory)))
    target = directory / SCORE_V2_MIX_NAME
    claim = _reserve_private_file(
        directory,
        prefix=".tianlai-score-v2-formal-",
        suffix=".pcm24.wav",
    )
    active_sealed = None
    try:
        float_hash = _new_audio_stream_hasher(
            sample_rate=sample_rate,
            frame_count=frame_count,
        )
        block_count = 0
        event_count = 0
        endpoint_count = 0
        peak_active_voices = 0
        peak = 0.0
        active_sample_count = 0
        cursor = 0
        event_index = 0
        with _open_private_file_claim(claim, truncate=True) as raw:
            tracked = _SequentialDigestWriter(raw)
            output = wave.open(tracked, "wb")
            primary: BaseException | None = None
            try:
                output.setnchannels(2)
                output.setsampwidth(3)
                output.setframerate(sample_rate)
                output.setnframes(frame_count)
                while event_index < len(performance.events):
                    event_sample = performance.events[event_index].sample
                    if event_sample < cursor or event_sample > frame_count:
                        raise ScoreV2FormalRenderError("render.event_order_invalid")
                    while cursor < event_sample:
                        take = min(block_limit, event_sample - cursor)
                        block = authority.render_block(take)
                        import numpy as np

                        if (
                            type(block) is not np.ndarray
                            or block.shape != (take, 2)
                            or block.dtype != np.dtype("<f8")
                            or not block.flags.c_contiguous
                            or block.flags.writeable
                            or not bool(np.isfinite(block).all())
                        ):
                            raise ScoreV2FormalRenderError("render.audio_block_invalid")
                        local_peak = float(np.max(np.abs(block), initial=0.0))
                        if not math.isfinite(local_peak) or local_peak > 1.0:
                            raise ScoreV2FormalRenderError("render.audio_out_of_range")
                        peak = max(peak, local_peak)
                        active_sample_count += int(np.count_nonzero(block))
                        float_hash.update(block.tobytes(order="C"))
                        written = _write_numpy_pcm24(
                            output,
                            block,
                            reject_out_of_range=True,
                            frame_offset=cursor,
                        )
                        if written != take:
                            raise ScoreV2FormalRenderError("render.pcm24_write_failed")
                        cursor += take
                        block_count += 1
                        peak_active_voices = max(
                            peak_active_voices,
                            authority.active_voice_count(),
                        )
                    while (
                        event_index < len(performance.events)
                        and performance.events[event_index].sample == event_sample
                    ):
                        event = performance.events[event_index]
                        expected = sidecar[event_index]
                        if (
                            expected.get("sequence") != event_index
                            or expected.get("expected_sample") != event.sample
                            or expected.get("role") != event.type
                        ):
                            raise ScoreV2FormalRenderError("render.sidecar_mismatch")
                        authority.dispatch_event(event)
                        event_count += 1
                        if event.sample == frame_count:
                            endpoint_count += 1
                        peak_active_voices = max(
                            peak_active_voices,
                            authority.active_voice_count(),
                        )
                        event_index += 1
                while cursor < frame_count:
                    take = min(block_limit, frame_count - cursor)
                    block = authority.render_block(take)
                    import numpy as np

                    if (
                        type(block) is not np.ndarray
                        or block.shape != (take, 2)
                        or block.dtype != np.dtype("<f8")
                        or not block.flags.c_contiguous
                        or block.flags.writeable
                        or not bool(np.isfinite(block).all())
                    ):
                        raise ScoreV2FormalRenderError("render.audio_block_invalid")
                    local_peak = float(np.max(np.abs(block), initial=0.0))
                    if not math.isfinite(local_peak) or local_peak > 1.0:
                        raise ScoreV2FormalRenderError("render.audio_out_of_range")
                    peak = max(peak, local_peak)
                    active_sample_count += int(np.count_nonzero(block))
                    float_hash.update(block.tobytes(order="C"))
                    written = _write_numpy_pcm24(
                        output,
                        block,
                        reject_out_of_range=True,
                        frame_offset=cursor,
                    )
                    if written != take:
                        raise ScoreV2FormalRenderError("render.pcm24_write_failed")
                    cursor += take
                    block_count += 1
                    peak_active_voices = max(
                        peak_active_voices,
                        authority.active_voice_count(),
                    )
                if (
                    event_count != len(performance.events)
                    or event_count != bundle.event_count
                ):
                    raise ScoreV2FormalRenderError("render.event_count_mismatch")
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    output.close()
                except BaseException as close_error:
                    if primary is not None:
                        _add_note_safely(primary, f"formal WAV close also failed: {close_error}")
                    elif not isinstance(close_error, Exception):
                        raise
                    else:
                        raise ScoreV2FormalRenderError("render.wav_close_failed") from close_error
            if (
                cursor != frame_count
                or not tracked.sequential
                or tracked.byte_count != expected_size
                or raw.tell() != expected_size
            ):
                raise ScoreV2FormalRenderError("render.wav_stream_mismatch")
            wav_sha256 = tracked.digest.hexdigest()

        authority.checkpoint(full_sources=True)
        consumed_document = authority.finish_execution()
        consumed_bytes = authority.consumed_canonical_bytes
        consumed_hash = authority.consumed_sha256
        if canonical_json_bytes(consumed_document) != consumed_bytes:
            raise ScoreV2FormalRenderError("render.authority_evidence_mismatch")
        sealed = _seal_private_file_claim(claim, expected_sha256=wav_sha256)
        active_sealed = sealed
        post_document = _post_render_check_document(
            mix_sha256=wav_sha256,
            mix_size_bytes=expected_size,
            performance_bundle_sha256=bundle.artifact_sha256,
            runtime_authority_sha256=consumed_hash,
            sample_rate=sample_rate,
            frame_count=frame_count,
            peak=peak,
            active_sample_count=active_sample_count,
            event_count=event_count,
            endpoint_event_count=endpoint_count,
        )
        if post_document["summary"]["can_proceed"] is not True:
            raise ScoreV2FormalRenderError("render.post_check_failed")
        post_bytes = canonical_json_bytes(post_document)
        result_values = dict(
            executor_id=authority.executor_id,
            part_id=authority.part_id,
            performance_bundle_sha256=bundle.artifact_sha256,
            runtime_source_sha256=bundle.runtime_source_sha256,
            mix_sha256=wav_sha256,
            mix_size_bytes=expected_size,
            sample_rate=sample_rate,
            frame_count=frame_count,
            block_count=block_count,
            event_count=event_count,
            endpoint_event_count=endpoint_count,
            peak_active_voices=peak_active_voices,
            peak=peak,
            active_sample_count=active_sample_count,
            float_stream_sha256=float_hash.hexdigest(),
            performance_sha256=local.performance_sha256,
            event_sidecar_sha256=local.event_sidecar_sha256,
            effective_manifest_sha256=authority.effective_manifest_sha256,
            factory_generation_sha256=authority.factory_generation_sha256,
            runtime_authority_acquisition_canonical_bytes=acquisition_bytes,
            runtime_authority_acquisition_sha256=acquisition_hash,
            runtime_authority_canonical_bytes=consumed_bytes,
            runtime_authority_sha256=consumed_hash,
            runtime_manifest_bytes=authority.manifest_bytes,
            runtime_manifest_sha256=authority.manifest_raw_sha256,
            post_render_check_canonical_bytes=post_bytes,
            post_render_check_sha256=hashlib.sha256(post_bytes).hexdigest(),
        )
        # Installing the sealed inode under its fixed candidate filename is
        # deliberately the final fallible publication step.  Every byte and
        # every piece of returned evidence is complete before the pathname
        # becomes visible, so an earlier failure can still retire the private
        # generation without leaving a half-described mix behind.
        installed = _install_sealed_private_file(sealed, target)
        active_sealed = installed
        generation = _FormalGeneration(
            mix_path=installed.identity.path,
            sealed_mix=installed,
            authority=authority,
            **result_values,
        )
        result = object.__new__(ScoreV2FormalRenderGeneration)
        _register_formal_generation(result, generation)
        result._trusted_generation()
        _close_private_file_claim(installed.claim)
        active_sealed = None
        return result
    except BaseException as exc:
        if active_sealed is not None:
            try:
                _retire_sealed_private_file(active_sealed, require_present=False)
            except BaseException as cleanup_error:
                _add_note_safely(exc, f"formal Score-v2 WAV cleanup failed: {cleanup_error}")
        else:
            try:
                preserved = _retire_private_file(
                    claim,
                    require_present=False,
                )
                if preserved is not None:
                    _add_note_safely(
                        exc,
                        "formal Score-v2 private claim was preserved for "
                        f"recovery at {preserved}",
                    )
            except BaseException as cleanup_error:
                _add_note_safely(exc, f"formal Score-v2 private claim cleanup failed: {cleanup_error}")
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, (ScoreV2FormalRenderError, ResourceLimitError)):
            raise
        if isinstance(exc, ScoreV2RuntimeAuthorityError):
            raise ScoreV2FormalRenderError("render.runtime_authority_failed") from exc
        raise ScoreV2FormalRenderError("render.failed") from exc


__all__ = [
    "SCORE_V2_FORMAL_RENDER_CONTRACT",
    "SCORE_V2_POST_RENDER_CHECK_CONTRACT",
    "SCORE_V2_POST_RENDER_CHECK_KIND",
    "SCORE_V2_POST_RENDER_CHECK_SCHEMA_VERSION",
    "ScoreV2FormalRenderError",
    "ScoreV2FormalRenderGeneration",
    "render_score_v2_formal_pcm24_generation",
]
