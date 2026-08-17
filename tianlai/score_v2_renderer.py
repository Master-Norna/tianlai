"""Short-lived local execution for the narrow Score-v2 oscillator subset.

This module intentionally does not publish audio, use render workers, or
consult stem caches.  It synchronously consumes one sealed executor transport
into a caller-owned private block sink, observes every event delivered to the
backend, dispatches exclusive-endpoint events without rendering a hidden
frame, and returns a sealed receipt that is explicitly not publish authority.

Runtime-source validation remains a sequence of observations.  The current
runtime fingerprint cannot freeze unrelated files, prevent ABA replacement,
bind already-imported Python objects to the bytes on disk, or hold lazy assets
by descriptor.  The first executable subset is therefore restricted to the
built-in oscillator manifest route with an explicit no-external-audio-assets
contract.  This does not bind the already-loaded Python object generation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable
import weakref

from .canonical_json import canonical_json_bytes
from .events import PerformanceDocument, PerformanceEvent, parse_performance_document
from .instrument import Instrument, create_instrument, factory_manifest_sha256
from .oscillator import OscillatorInstrument
from .renderer import render_document_blocks
from .score_v2_performance import (
    ScoreV2PerformanceBundle,
    ScoreV2PerformanceError,
)
from .score_v2_runtime_source import (
    NO_EXTERNAL_ASSET_INVENTORY_STATUS,
    ScoreV2RuntimeSourceError,
)


SCORE_V2_LOCAL_EXECUTION_RECEIPT_KIND = (
    "tianlai.score_v2_local_execution_receipt"
)
SCORE_V2_LOCAL_EXECUTION_RECEIPT_SCHEMA_VERSION = 1
SCORE_V2_LOCAL_EXECUTION_RECEIPT_CONTRACT = (
    "score-v2-local-execution-receipt-v1-not-publish-authority"
)
OSCILLATOR_EXECUTION_SCOPE = (
    "builtin_oscillator_manifest_route_declared_no_external_audio_assets"
)
ENDPOINT_EXECUTION_STATUS = (
    "exclusive_endpoint_events_dispatched_without_output_frame"
)
RUNTIME_GENERATION_STATUS = (
    "sequential_pre_factory_post_factory_post_render_observations"
)
SINK_STATUS = "synchronously_consumed_caller_private_block_sink"

_HEX = frozenset("0123456789abcdef")
_MAXIMUM_BLOCK_FRAMES = 65_536
_FLOAT64_AUDIO_STREAM_HASH_DOMAIN = (
    b"tianlai-score-v2-local-float64-stereo-v1\0"
)
_RECEIPT_GENERATIONS: dict[
    int,
    tuple[weakref.ReferenceType[object], bytes, str],
] = {}
_RECEIPT_LIMITATIONS = {
    "publish_authority": "not_granted",
    "sink_privacy": "caller_contract_not_independently_verified",
    "runtime_generation_atomicity": "not_atomic",
    "runtime_generation_aba_resistance": "not_claimed",
    "loaded_python_object_generation": "not_bound_to_disk_closure",
    "lazy_asset_descriptor_generation": "not_available",
    "backend_scope": OSCILLATOR_EXECUTION_SCOPE,
    "release_tail": "transport_frame_count_only_no_implicit_tail",
    "acoustic_onset_alignment": "not_captured",
}


class ScoreV2RendererError(ValueError):
    """A stable failure at the non-publishing local execution boundary."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.message_key = f"scoreV2Renderer.{code.replace('.', '_')}"
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class _BundleGeneration:
    canonical_bytes: bytes
    artifact_sha256: str
    runtime_source_sha256: str
    sample_rate: int
    frame_count: int
    executor_count: int


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in _HEX for character in value)
    )


def _add_note_safely(error: BaseException, note: str) -> None:
    try:
        error.add_note(note)
    except BaseException:
        pass


def _new_audio_stream_hasher(*, sample_rate: int, frame_count: int) -> Any:
    digest = hashlib.sha256()
    digest.update(
        _FLOAT64_AUDIO_STREAM_HASH_DOMAIN
        + canonical_json_bytes(
            {
                "sample_rate": sample_rate,
                "frame_count": frame_count,
                "channels": 2,
            }
        )
    )
    return digest


def _revalidate_bundle(bundle: ScoreV2PerformanceBundle) -> None:
    try:
        bundle.revalidate_runtime_sources()
    except (
        ScoreV2PerformanceError,
        ScoreV2RuntimeSourceError,
        AttributeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ScoreV2RendererError(
            "renderer.runtime_generation_changed"
        ) from exc


def _freeze_bundle_generation(
    bundle: ScoreV2PerformanceBundle,
) -> _BundleGeneration:
    try:
        generation = _BundleGeneration(
            canonical_bytes=bundle.canonical_bytes,
            artifact_sha256=bundle.artifact_sha256,
            runtime_source_sha256=bundle.runtime_source_sha256,
            sample_rate=bundle.sample_rate,
            frame_count=bundle.frame_count,
            executor_count=bundle.executor_count,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2RendererError(
            "renderer.execution_input_invalid"
        ) from exc
    if (
        type(generation.canonical_bytes) is not bytes
        or not generation.canonical_bytes
        or not _is_sha256(generation.artifact_sha256)
        or hashlib.sha256(generation.canonical_bytes).hexdigest()
        != generation.artifact_sha256
        or not _is_sha256(generation.runtime_source_sha256)
        or type(generation.sample_rate) is not int
        or not 8_000 <= generation.sample_rate <= 384_000
        or type(generation.frame_count) is not int
        or generation.frame_count < 1
        or type(generation.executor_count) is not int
        or generation.executor_count < 1
    ):
        raise ScoreV2RendererError("renderer.execution_input_invalid")
    return generation


def _require_bundle_generation(
    bundle: ScoreV2PerformanceBundle,
    expected: _BundleGeneration,
) -> None:
    current = _freeze_bundle_generation(bundle)
    if current != expected:
        raise ScoreV2RendererError(
            "renderer.bundle_generation_changed"
        )


def _safe_oscillator_manifest(
    manifest: dict[str, Any],
    *,
    asset_inventory_status: str,
) -> None:
    external_assets = manifest.get("external_audio_assets")
    malformed_asset_fields = (
        (
            "external_audio_assets" in manifest
            and type(external_assets) is not list
        )
        or any(
            key in manifest
            for key in ("asset_root", "soundfont", "sample", "regions")
        )
    )
    declared_asset_free = (
        manifest.get("runtime_asset_policy") == "no_external_audio_assets"
        or (
            manifest.get("provenance_kind") == "project_authored_dsp"
            and type(external_assets) is list
            and external_assets == []
        )
    )
    explicit_asset_fields = type(external_assets) is list and bool(
        external_assets
    )
    if (
        manifest.get("type") != "oscillator"
        or manifest.get("implementation") is not None
        or malformed_asset_fields
        or not declared_asset_free
        or explicit_asset_fields
        or asset_inventory_status != NO_EXTERNAL_ASSET_INVENTORY_STATUS
    ):
        raise ScoreV2RendererError("renderer.backend_scope_unsupported")


def _validate_factory_provenance(
    instrument: OscillatorInstrument,
    manifest: dict[str, Any],
    *,
    sample_rate: int,
    expected_manifest_sha256: str,
) -> None:
    provenance = getattr(instrument, "_tianlai_factory_provenance", None)
    if (
        type(provenance) is not dict
        or set(provenance)
        != {
            "schema_version",
            "manifest_sha256",
            "sample_rate_hz",
            "factory_route",
        }
        or provenance.get("schema_version") != 1
        or provenance.get("factory_route")
        != "builtin_manifest_dispatch_no_implementation"
        or type(provenance.get("sample_rate_hz")) is not int
        or provenance.get("sample_rate_hz") != sample_rate
        or type(getattr(instrument, "sample_rate", None)) is not int
        or instrument.sample_rate != sample_rate
        or not _is_sha256(provenance.get("manifest_sha256"))
        or provenance.get("manifest_sha256") != expected_manifest_sha256
        or factory_manifest_sha256(manifest) != expected_manifest_sha256
    ):
        raise ScoreV2RendererError(
            "renderer.factory_provenance_mismatch"
        )


def _decoded_execution_documents(
    performance_bytes: bytes,
    sidecar_bytes: bytes,
    *,
    sample_rate: int,
    frame_count: int,
) -> tuple[PerformanceDocument, tuple[dict[str, object], ...]]:
    try:
        raw_performance = json.loads(performance_bytes)
        raw_sidecar = json.loads(sidecar_bytes)
        if (
            type(raw_performance) is not dict
            or type(raw_sidecar) is not list
            or canonical_json_bytes(raw_performance) != performance_bytes
            or canonical_json_bytes(raw_sidecar) != sidecar_bytes
        ):
            raise ValueError
        performance = parse_performance_document(raw_performance)
    except (
        TypeError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise ScoreV2RendererError(
            "renderer.execution_transport_invalid"
        ) from exc
    if (
        performance.sample_rate != sample_rate
        or performance.channels != 2
        or performance.total_samples != frame_count
        or len(performance.events) != len(raw_sidecar)
    ):
        raise ScoreV2RendererError("renderer.execution_transport_invalid")
    checked: list[dict[str, object]] = []
    for sequence, (event, raw) in enumerate(
        zip(performance.events, raw_sidecar, strict=True)
    ):
        if (
            type(raw) is not dict
            or set(raw)
            != {
                "sequence",
                "occurrence_id",
                "role",
                "note_id",
                "expected_sample",
            }
            or raw.get("sequence") != sequence
            or raw.get("role") != event.type
            or raw.get("expected_sample") != event.sample
            or type(raw.get("occurrence_id")) is not str
            or not raw["occurrence_id"]
            or type(raw.get("note_id")) is not int
            or event.sample < 0
            or event.sample > frame_count
        ):
            raise ScoreV2RendererError(
                "renderer.event_sidecar_mismatch"
            )
        if event.type in {"note_on", "note_off"} and (
            event.payload.get("note_id") != raw["note_id"]
            or event.payload.get("source_event_id")
            != raw["occurrence_id"]
        ):
            raise ScoreV2RendererError(
                "renderer.event_sidecar_mismatch"
            )
        checked.append(dict(raw))
    return performance, tuple(checked)


class _ObservedOscillator(Instrument):
    """Conservative frame-path adapter that proves actual event delivery."""

    def __init__(
        self,
        instrument: OscillatorInstrument,
        expected_events: tuple[PerformanceEvent, ...],
    ) -> None:
        super().__init__(instrument.sample_rate)
        self.instrument = instrument
        self.expected_events = expected_events
        self.event_index = 0
        self.render_frame_calls = 0

    def handle_event(self, event: PerformanceEvent, tuning: Any) -> None:
        if (
            self.event_index >= len(self.expected_events)
            or event is not self.expected_events[self.event_index]
        ):
            raise ScoreV2RendererError(
                "renderer.event_dispatch_mismatch"
            )
        self.instrument.handle_event(event, tuning)
        self.event_index += 1

    def render_frame(self) -> tuple[float, float]:
        self.render_frame_calls += 1
        return self.instrument.render_frame()

    @property
    def active_voice_count(self) -> int:
        return self.instrument.active_voice_count


def _receipt_document(
    *,
    bundle_sha256: str,
    runtime_source_sha256: str,
    executor_id: str,
    part_id: str,
    performance_sha256: str,
    sidecar_sha256: str,
    effective_manifest_sha256: str,
    runtime_fingerprint_sha256: str,
    sample_rate: int,
    frame_count: int,
    block_count: int,
    event_count: int,
    endpoint_event_count: int,
    peak_active_voices: int,
    audio_stream_sha256: str,
) -> dict[str, object]:
    return {
        "kind": SCORE_V2_LOCAL_EXECUTION_RECEIPT_KIND,
        "schema_version": SCORE_V2_LOCAL_EXECUTION_RECEIPT_SCHEMA_VERSION,
        "contract": SCORE_V2_LOCAL_EXECUTION_RECEIPT_CONTRACT,
        "render_authority": False,
        "publish_authority": False,
        "backend_scope": OSCILLATOR_EXECUTION_SCOPE,
        "endpoint_execution_status": ENDPOINT_EXECUTION_STATUS,
        "runtime_generation_status": RUNTIME_GENERATION_STATUS,
        "sink_status": SINK_STATUS,
        "bindings": {
            "performance_bundle_sha256": bundle_sha256,
            "runtime_source_sha256": runtime_source_sha256,
            "performance_sha256": performance_sha256,
            "event_sidecar_sha256": sidecar_sha256,
            "effective_manifest_sha256": effective_manifest_sha256,
            "legacy_runtime_fingerprint_sha256": (
                runtime_fingerprint_sha256
            ),
        },
        "executor_id": executor_id,
        "part_id": part_id,
        "sample_rate": sample_rate,
        "channels": 2,
        "frame_count": frame_count,
        "block_count": block_count,
        "event_count": event_count,
        "endpoint_event_count": endpoint_event_count,
        "peak_active_voices": peak_active_voices,
        "audio_stream_encoding": "little_endian_float64_stereo_interleaved",
        "audio_stream_sha256": audio_stream_sha256,
        "limitations": dict(_RECEIPT_LIMITATIONS),
    }


@dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
class ScoreV2LocalExecutionReceipt:
    """Sealed evidence of one local execution, never publication authority."""

    executor_id: str
    sample_rate: int
    frame_count: int
    block_count: int
    event_count: int
    endpoint_event_count: int
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _identity_seal: tuple[object, ...] = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2LocalExecutionReceipt cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2LocalExecutionReceipt must be created by the local renderer"
        )

    def _trusted_bytes(self) -> bytes:
        try:
            registered = _RECEIPT_GENERATIONS.get(id(self))
            if (
                registered is None
                or registered[0]() is not self
                or registered[1] is not self._canonical_bytes
                or registered[2] != self._artifact_sha256
            ):
                raise ValueError
            seal = self._identity_seal
            if type(seal) is not tuple or len(seal) != 9:
                raise ValueError
            (
                executor_id,
                sample_rate,
                frame_count,
                block_count,
                event_count,
                endpoint_event_count,
                payload,
                digest,
                contract,
            ) = seal
            value = json.loads(payload)
            bindings = value.get("bindings") if type(value) is dict else None
            if (
                type(executor_id) is not str
                or not executor_id
                or any(
                    type(item) is not int or item < 0
                    for item in (
                        sample_rate,
                        frame_count,
                        block_count,
                        event_count,
                        endpoint_event_count,
                    )
                )
                or sample_rate < 8_000
                or frame_count < 1
                or self.executor_id != executor_id
                or self.sample_rate != sample_rate
                or self.frame_count != frame_count
                or self.block_count != block_count
                or self.event_count != event_count
                or self.endpoint_event_count != endpoint_event_count
                or self._canonical_bytes is not payload
                or self._artifact_sha256 != digest
                or contract != SCORE_V2_LOCAL_EXECUTION_RECEIPT_CONTRACT
                or type(payload) is not bytes
                or not _is_sha256(digest)
                or hashlib.sha256(payload).hexdigest() != digest
                or type(value) is not dict
                or set(value)
                != {
                    "kind",
                    "schema_version",
                    "contract",
                    "render_authority",
                    "publish_authority",
                    "backend_scope",
                    "endpoint_execution_status",
                    "runtime_generation_status",
                    "sink_status",
                    "bindings",
                    "executor_id",
                    "part_id",
                    "sample_rate",
                    "channels",
                    "frame_count",
                    "block_count",
                    "event_count",
                    "endpoint_event_count",
                    "peak_active_voices",
                    "audio_stream_encoding",
                    "audio_stream_sha256",
                    "limitations",
                }
                or value.get("kind")
                != SCORE_V2_LOCAL_EXECUTION_RECEIPT_KIND
                or value.get("schema_version")
                != SCORE_V2_LOCAL_EXECUTION_RECEIPT_SCHEMA_VERSION
                or value.get("contract") != contract
                or value.get("render_authority") is not False
                or value.get("publish_authority") is not False
                or value.get("backend_scope") != OSCILLATOR_EXECUTION_SCOPE
                or value.get("endpoint_execution_status")
                != ENDPOINT_EXECUTION_STATUS
                or value.get("runtime_generation_status")
                != RUNTIME_GENERATION_STATUS
                or value.get("sink_status") != SINK_STATUS
                or type(bindings) is not dict
                or set(bindings)
                != {
                    "performance_bundle_sha256",
                    "runtime_source_sha256",
                    "performance_sha256",
                    "event_sidecar_sha256",
                    "effective_manifest_sha256",
                    "legacy_runtime_fingerprint_sha256",
                }
                or any(not _is_sha256(item) for item in bindings.values())
                or value.get("executor_id") != executor_id
                or type(value.get("part_id")) is not str
                or not value["part_id"]
                or value.get("sample_rate") != sample_rate
                or value.get("channels") != 2
                or value.get("frame_count") != frame_count
                or value.get("block_count") != block_count
                or value.get("event_count") != event_count
                or value.get("endpoint_event_count") != endpoint_event_count
                or type(value.get("peak_active_voices")) is not int
                or value["peak_active_voices"] < 0
                or value.get("audio_stream_encoding")
                != "little_endian_float64_stereo_interleaved"
                or not _is_sha256(value.get("audio_stream_sha256"))
                or value.get("limitations") != _RECEIPT_LIMITATIONS
                or canonical_json_bytes(value) != payload
            ):
                raise ValueError
            return payload
        except (
            AttributeError,
            TypeError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise ScoreV2RendererError(
                "renderer.receipt_integrity_mismatch"
            ) from exc

    @property
    def canonical_bytes(self) -> bytes:
        return self._trusted_bytes()

    @property
    def artifact_sha256(self) -> str:
        self._trusted_bytes()
        return self._artifact_sha256

    def to_dict(self) -> dict[str, object]:
        value = json.loads(self._trusted_bytes())
        if type(value) is not dict:
            raise ScoreV2RendererError(
                "renderer.receipt_integrity_mismatch"
            )
        return value


def render_score_v2_executor_to_private_block_sink(
    bundle: ScoreV2PerformanceBundle,
    executor_id: str,
    private_block_sink: Callable[[Any, int], object],
    *,
    maximum_block_frames: int = _MAXIMUM_BLOCK_FRAMES,
) -> ScoreV2LocalExecutionReceipt:
    """Synchronously execute the one-executor oscillator subset.

    ``private_block_sink`` is called as ``sink(read_only_block, start_sample)``.
    It must synchronously consume the block and return ``None``.  The renderer
    cannot inspect or authorize the sink's eventual destination, so the
    resulting receipt can never authorize publication.
    """

    if type(bundle) is not ScoreV2PerformanceBundle:
        raise TypeError("bundle must be ScoreV2PerformanceBundle")
    if type(executor_id) is not str or not executor_id:
        raise ValueError("executor_id must be a non-empty string")
    if not callable(private_block_sink):
        raise TypeError("private_block_sink must be callable")
    if (
        type(maximum_block_frames) is not int
        or maximum_block_frames <= 0
        or maximum_block_frames > _MAXIMUM_BLOCK_FRAMES
    ):
        raise ValueError(
            "maximum_block_frames must be an integer between 1 and 65536"
        )
    generation = _freeze_bundle_generation(bundle)
    if generation.executor_count != 1:
        raise ScoreV2RendererError(
            "renderer.single_executor_subset_required"
        )
    try:
        execution = bundle._local_execution_input_for_executor(executor_id)
    except (ScoreV2PerformanceError, ScoreV2RuntimeSourceError) as exc:
        raise ScoreV2RendererError(
            "renderer.execution_input_invalid"
        ) from exc
    _require_bundle_generation(bundle, generation)
    manifest = execution.runtime.manifest_copy()
    runtime_binding = execution.runtime.runtime_binding
    fingerprint = runtime_binding.fingerprint_copy()
    asset_graph = fingerprint.get("runtime_asset_graph")
    if (
        type(asset_graph) is not dict
        or asset_graph.get("file_count") != 0
        or asset_graph.get("total_bytes") != 0
    ):
        raise ScoreV2RendererError("renderer.backend_scope_unsupported")
    _safe_oscillator_manifest(
        manifest,
        asset_inventory_status=runtime_binding.asset_inventory_status,
    )
    try:
        instrument = create_instrument(
            manifest,
            generation.sample_rate,
            base_directory=str(Path(execution.runtime.manifest_path).parent),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ScoreV2RendererError("renderer.factory_failed") from exc
    if type(instrument) is not OscillatorInstrument:
        primary = ScoreV2RendererError(
            "renderer.backend_scope_unsupported"
        )
        close = getattr(instrument, "close", None)
        if callable(close):
            try:
                close()
            except BaseException:
                _add_note_safely(primary, "instrument close also failed")
        raise primary

    close = getattr(instrument, "close", None)
    closed = False

    def close_once() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        if callable(close):
            try:
                close()
            except Exception as exc:
                raise ScoreV2RendererError("renderer.close_failed") from exc

    try:
        _validate_factory_provenance(
            instrument,
            manifest,
            sample_rate=generation.sample_rate,
            expected_manifest_sha256=execution.runtime.effective_manifest_sha256,
        )
        _revalidate_bundle(bundle)
        _require_bundle_generation(bundle, generation)
        performance, sidecars = _decoded_execution_documents(
            execution.performance_canonical_bytes,
            execution.event_sidecar_canonical_bytes,
            sample_rate=generation.sample_rate,
            frame_count=generation.frame_count,
        )
        observer = _ObservedOscillator(instrument, performance.events)
        blocks, peak = render_document_blocks(
            observer,
            performance,
            maximum_block_frames=maximum_block_frames,
            sample_dtype="float64",
        )
        import numpy as np

        stream_hash = _new_audio_stream_hasher(
            sample_rate=generation.sample_rate,
            frame_count=generation.frame_count,
        )
        offset = 0
        block_count = 0
        for raw_block in blocks:
            block = np.ascontiguousarray(raw_block, dtype="<f8")
            frame_count = int(block.shape[0]) if block.ndim == 2 else -1
            if (
                block.shape != (frame_count, 2)
                or frame_count <= 0
                or frame_count > maximum_block_frames
                or offset + frame_count > generation.frame_count
                or not bool(np.isfinite(block).all())
                or float(np.max(np.abs(block), initial=0.0)) > 1.0
            ):
                raise ScoreV2RendererError("renderer.audio_block_invalid")
            block_payload = block.tobytes(order="C")
            stream_hash.update(block_payload)
            immutable_block = np.frombuffer(
                block_payload,
                dtype="<f8",
            ).reshape(frame_count, 2)
            try:
                result = private_block_sink(immutable_block, offset)
            except Exception as exc:
                raise ScoreV2RendererError("renderer.sink_failed") from exc
            if result is not None:
                raise ScoreV2RendererError("renderer.sink_result_invalid")
            offset += frame_count
            block_count += 1
        in_range_event_count = sum(
            event.sample < generation.frame_count
            for event in performance.events
        )
        if (
            offset != generation.frame_count
            or observer.render_frame_calls != generation.frame_count
            or observer.event_index != in_range_event_count
        ):
            raise ScoreV2RendererError(
                "renderer.in_range_execution_mismatch"
            )
        endpoint_events = tuple(
            event
            for event in performance.events
            if event.sample == generation.frame_count
        )
        endpoint_sidecars = tuple(
            sidecar
            for sidecar in sidecars
            if sidecar["expected_sample"] == generation.frame_count
        )
        if len(endpoint_events) != len(endpoint_sidecars):
            raise ScoreV2RendererError(
                "renderer.endpoint_execution_mismatch"
            )
        for event, sidecar in zip(
            endpoint_events, endpoint_sidecars, strict=True
        ):
            if (
                event.sequence != sidecar["sequence"]
                or event.type != sidecar["role"]
            ):
                raise ScoreV2RendererError(
                    "renderer.endpoint_execution_mismatch"
                )
            observer.handle_event(event, performance.tuning)
        if (
            observer.event_index != len(performance.events)
            or observer.render_frame_calls != generation.frame_count
        ):
            raise ScoreV2RendererError(
                "renderer.endpoint_execution_mismatch"
            )
        _validate_factory_provenance(
            instrument,
            manifest,
            sample_rate=generation.sample_rate,
            expected_manifest_sha256=execution.runtime.effective_manifest_sha256,
        )
    except ScoreV2RendererError as primary:
        try:
            close_once()
        except BaseException:
            _add_note_safely(primary, "instrument close also failed")
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        primary = ScoreV2RendererError("renderer.render_failed")
        try:
            close_once()
        except BaseException:
            _add_note_safely(primary, "instrument close also failed")
        raise primary from exc
    except BaseException as primary:
        try:
            close_once()
        except BaseException:
            _add_note_safely(primary, "instrument close also failed")
        raise
    else:
        close_once()

    _revalidate_bundle(bundle)
    _require_bundle_generation(bundle, generation)
    document = _receipt_document(
        bundle_sha256=generation.artifact_sha256,
        runtime_source_sha256=generation.runtime_source_sha256,
        executor_id=execution.executor_id,
        part_id=execution.part_id,
        performance_sha256=execution.performance_sha256,
        sidecar_sha256=execution.event_sidecar_sha256,
        effective_manifest_sha256=execution.runtime.effective_manifest_sha256,
        runtime_fingerprint_sha256=(
            runtime_binding.legacy_runtime_fingerprint_sha256
        ),
        sample_rate=generation.sample_rate,
        frame_count=generation.frame_count,
        block_count=block_count,
        event_count=len(performance.events),
        endpoint_event_count=len(endpoint_events),
        peak_active_voices=peak[0],
        audio_stream_sha256=stream_hash.hexdigest(),
    )
    payload = canonical_json_bytes(document)
    digest = hashlib.sha256(payload).hexdigest()
    receipt = object.__new__(ScoreV2LocalExecutionReceipt)
    for name, value in (
        ("executor_id", execution.executor_id),
        ("sample_rate", generation.sample_rate),
        ("frame_count", generation.frame_count),
        ("block_count", block_count),
        ("event_count", len(performance.events)),
        ("endpoint_event_count", len(endpoint_events)),
        ("_canonical_bytes", payload),
        ("_artifact_sha256", digest),
    ):
        object.__setattr__(receipt, name, value)
    object.__setattr__(
        receipt,
        "_identity_seal",
        (
            execution.executor_id,
            generation.sample_rate,
            generation.frame_count,
            block_count,
            len(performance.events),
            len(endpoint_events),
            payload,
            digest,
            SCORE_V2_LOCAL_EXECUTION_RECEIPT_CONTRACT,
        ),
    )
    receipt_id = id(receipt)

    def retire_receipt_generation(
        reference: weakref.ReferenceType[object],
        *,
        expected_id: int = receipt_id,
    ) -> None:
        current = _RECEIPT_GENERATIONS.get(expected_id)
        if current is not None and current[0] is reference:
            _RECEIPT_GENERATIONS.pop(expected_id, None)

    receipt_reference = weakref.ref(receipt, retire_receipt_generation)
    _RECEIPT_GENERATIONS[receipt_id] = (
        receipt_reference,
        payload,
        digest,
    )
    receipt._trusted_bytes()
    return receipt


__all__ = [
    "ENDPOINT_EXECUTION_STATUS",
    "OSCILLATOR_EXECUTION_SCOPE",
    "RUNTIME_GENERATION_STATUS",
    "SCORE_V2_LOCAL_EXECUTION_RECEIPT_CONTRACT",
    "SCORE_V2_LOCAL_EXECUTION_RECEIPT_KIND",
    "SCORE_V2_LOCAL_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "ScoreV2LocalExecutionReceipt",
    "ScoreV2RendererError",
    "render_score_v2_executor_to_private_block_sink",
]
