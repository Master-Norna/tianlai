from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import wave

import numpy as np
import pytest

from tianlai.capability import read_capability
from tianlai.canonical_json import canonical_json_bytes
from tianlai.events import PerformanceDocument, PerformanceEvent
from tianlai.instrument import Instrument, create_instrument as real_create_instrument
from tianlai.onset_evidence import OnsetEvidenceError
from tianlai.roster import parse_roster_document
from tianlai.score_source import snapshot_score_document
from tianlai.score_v2 import Rational
from tianlai.score_v2_capability_adapter import compile_score_v2_capability_plan
from tianlai.score_v2_capability_source import capture_score_v2_capability_sources
from tianlai.score_v2_execution_profile import parse_score_v2_execution_profile
from tianlai.score_v2_performance import compile_score_v2_performance_bundle
from tianlai.score_v2_plan import compile_score_v2_plan
from tianlai.score_v2_private_wav import (
    SCORE_V2_PRIVATE_WAV_STAGE_CONTRACT,
    ScoreV2PrivateWavError,
    stage_score_v2_executor_pcm24_wav,
)
from tianlai.score_v2_renderer import (
    ENDPOINT_EXECUTION_STATUS,
    SCORE_V2_LOCAL_EXECUTION_RECEIPT_CONTRACT,
    ScoreV2LocalExecutionReceipt,
    ScoreV2RendererError,
    render_score_v2_executor_to_private_block_sink,
)
import tianlai.score_v2_renderer as renderer_module
import tianlai.score_v2_private_wav as private_wav_module
import tianlai.score_v2_runtime_source as runtime_source_module
from tianlai.score_v2_runtime_source import (
    ScoreV2RuntimeSourceError,
    capture_score_v2_runtime_sources,
)
from tianlai.tuning import EqualTemperament
from tianlai.resource_limits import ProjectLimits, ResourceLimitError


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _note(event_id: str, offset: int, duration: int) -> dict[str, object]:
    return {
        "event_id": event_id,
        "position": {
            "measure_id": "m1",
            "offset_quarters": _r(offset),
        },
        "duration_quarters": _r(duration),
        "written_pitch": {"step": "C", "alter": _r(0), "octave": 4},
        "sounding_pitch": {"midi_note": _r(60)},
    }


def _score(notes: list[dict[str, object]]) -> dict[str, object]:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "local renderer fixture",
        "timeline": {
            "measures": [
                {"measure_id": "m1", "actual_duration_quarters": _r(4)}
            ],
            "meter_events": [
                {
                    "meter_id": "meter",
                    "at": {"measure_id": "m1", "offset_quarters": _r(0)},
                    "groups": [4],
                    "beat_unit": 4,
                }
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo",
                    "at": {"measure_id": "m1", "offset_quarters": _r(0)},
                    "quarter_bpm": _r(60),
                }
            ],
        },
        "tuning": {
            "tuning_id": "a440",
            "system": "equal_temperament",
            "divisions_per_octave": 12,
            "reference_midi_note": _r(69),
            "reference_frequency_hz": _r(440),
        },
        "parts": [{"part_id": "lead", "default_dynamic": "mf", "notes": notes}],
        "form": {"mode": "linear"},
    }


def _profile() -> dict[str, object]:
    return {
        "kind": "tianlai.score_v2_execution_profile",
        "schema_version": 1,
        "sample_time_policy": "exact",
        "dynamic_profile": {"mf": _r(3, 5)},
        "note_velocity": {
            "value_policy": "adapt",
            "semantic_policy": "approximate",
        },
        "tuning": {"value_policy": "exact", "semantic_policy": "exact"},
        "pitch": {
            "value_policy": "exact",
            "semantic_policy": "exact",
            "range_policy": "declared_hard",
        },
        "articulation": {
            "mapping_policy": "direct_only",
            "semantic_policy": "exact",
        },
        "phrase_policy": "reject",
    }


def _fingerprint(
    root: Path,
    manifest: Path,
    *,
    sample_rate: int,
    generation: str,
) -> dict[str, object]:
    empty = hashlib.sha256(b"").hexdigest()
    return {
        "algorithm": "sha256-path-content-v1",
        "manifest": {
            "path": manifest.resolve().relative_to(root.resolve()).as_posix(),
            "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        },
        "render_python_closure": {
            "algorithm": "test-render-closure-v1",
            "entry_modules": ["tianlai.renderer"],
            "file_count": 1,
            "files": [{"path": "tianlai/renderer.py", "sha256": empty}],
            "sha256": hashlib.sha256(generation.encode()).hexdigest(),
        },
        "runtime_dependencies": {
            "python": {"version": "test"},
            "generation": generation,
        },
        "local_implementation": {"path": None, "sha256": None},
        "resource_verification": {"path": None, "sha256": None},
        "pitch_calibration": {"path": None, "sha256": None},
        "runtime_asset_graph": {
            "algorithm": "constructed-runtime-asset-graph-v1",
            "sample_rate_hz": sample_rate,
            "file_count": 0,
            "total_bytes": 0,
            "region_count": 0,
            "sha256": empty,
        },
    }


def _bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    notes: list[dict[str, object]] | None = None,
    instrument_type: str = "oscillator",
    manifest_overrides: dict[str, object] | None = None,
):
    generation = {"value": "initial"}
    instrument_dir = tmp_path / "instrument"
    instrument_dir.mkdir(parents=True)
    manifest_path = instrument_dir / "instrument.json"
    manifest = {
        "name": "local renderer fixture",
        "type": instrument_type,
        "note_min": 0,
        "note_max": 127,
        "articulation_auto_default": False,
        "runtime_asset_policy": "no_external_audio_assets",
    }
    if manifest_overrides is not None:
        manifest.update(manifest_overrides)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def compute(
        project_root: Path,
        manifest_path_value: str,
        *,
        effective_manifest: dict[str, object],
        sample_rate_hz: int,
    ) -> dict[str, object]:
        del effective_manifest
        return _fingerprint(
            Path(project_root),
            Path(manifest_path_value),
            sample_rate=sample_rate_hz,
            generation=generation["value"],
        )

    def validate(
        fingerprint: dict[str, object],
        *,
        project_root: Path | str,
        manifest_path: str,
        effective_manifest: dict[str, object],
        sample_rate_hz: int,
    ) -> dict[str, object]:
        del effective_manifest
        current = _fingerprint(
            Path(project_root),
            Path(manifest_path),
            sample_rate=sample_rate_hz,
            generation=generation["value"],
        )
        if fingerprint != current:
            raise OnsetEvidenceError("stale runtime")
        return fingerprint

    monkeypatch.setattr(runtime_source_module, "compute_runtime_fingerprint", compute)
    monkeypatch.setattr(runtime_source_module, "validate_runtime_fingerprint", validate)
    capability = read_capability(manifest_path, root=tmp_path)
    roster = parse_roster_document(
        {
            "name": "local renderer roster",
            "assignments": [
                {
                    "part": "lead",
                    "instrument": capability.relative_path,
                    "articulation_auto": False,
                }
            ],
        },
        {capability.relative_path: capability},
    )
    source = snapshot_score_document(
        _score(notes or [_note("n1", 0, 1)])
    )
    profile = parse_score_v2_execution_profile(_profile())
    dynamics = {
        item.mark: Rational(item.value.numerator, item.value.denominator)
        for item in profile.dynamic_profile
    }
    score_plan = compile_score_v2_plan(
        source,
        sample_rate=8_000,
        sample_time_policy=profile.sample_time_policy,  # type: ignore[arg-type]
        dynamic_profile=dynamics,
    )
    sources = capture_score_v2_capability_sources(
        roster, catalogue_root=tmp_path
    )
    capability_plan = compile_score_v2_capability_plan(
        source, score_plan, profile, roster, sources
    )
    runtime = capture_score_v2_runtime_sources(
        capability_plan, sources, project_root=tmp_path
    )
    bundle = compile_score_v2_performance_bundle(
        score_plan, capability_plan, runtime
    )
    executor_id = bundle.to_dict()["executors"][0]["executor_id"]
    return bundle, executor_id, generation


def _collecting_sink():
    blocks: list[np.ndarray] = []
    offsets: list[int] = []

    def sink(block: np.ndarray, offset: int) -> None:
        assert block.flags.writeable is False
        blocks.append(block.copy())
        offsets.append(offset)

    return sink, blocks, offsets


def test_executes_sealed_oscillator_to_private_sink_and_seals_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    sink, blocks, offsets = _collecting_sink()
    receipt = render_score_v2_executor_to_private_block_sink(
        bundle, executor_id, sink, maximum_block_frames=5_000
    )
    raw = receipt.to_dict()

    assert type(receipt) is ScoreV2LocalExecutionReceipt
    assert raw["contract"] == SCORE_V2_LOCAL_EXECUTION_RECEIPT_CONTRACT
    assert raw["render_authority"] is False
    assert raw["publish_authority"] is False
    assert raw["frame_count"] == sum(len(block) for block in blocks)
    assert offsets == [0, 5_000, 10_000, 15_000, 20_000, 25_000, 30_000]
    assert raw["block_count"] == len(blocks) == 7
    assert receipt.canonical_bytes == canonical_json_bytes(raw)
    assert receipt.artifact_sha256 == hashlib.sha256(receipt.canonical_bytes).hexdigest()


def test_endpoint_note_off_is_after_exactly_n_frames_without_hidden_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(
        tmp_path, monkeypatch, notes=[_note("whole", 0, 4)]
    )
    calls: list[tuple[str, int]] = []

    def create(*args, **kwargs):
        instrument = real_create_instrument(*args, **kwargs)
        real_handle = instrument.handle_event
        real_frame = instrument.render_frame
        frames = 0

        def handle(event, tuning):
            calls.append((event.type, frames))
            return real_handle(event, tuning)

        def render_frame():
            nonlocal frames
            frames += 1
            return real_frame()

        instrument.handle_event = handle
        instrument.render_frame = render_frame
        return instrument

    monkeypatch.setattr(renderer_module, "create_instrument", create)
    sink, _blocks, _offsets = _collecting_sink()
    receipt = render_score_v2_executor_to_private_block_sink(
        bundle, executor_id, sink
    )

    assert calls[0] == ("note_on", 0)
    assert calls[-1] == ("note_off", bundle.frame_count)
    assert receipt.endpoint_event_count == 1
    assert receipt.to_dict()["endpoint_execution_status"] == ENDPOINT_EXECUTION_STATUS


def test_revalidates_before_factory_after_factory_and_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    calls: list[str] = []
    original_revalidate = type(bundle).revalidate_runtime_sources
    original_create = renderer_module.create_instrument

    def revalidate(self):
        calls.append("revalidate")
        return original_revalidate(self)

    def create(*args, **kwargs):
        calls.append("factory")
        instrument = original_create(*args, **kwargs)
        instrument.close = lambda: calls.append("close")
        return instrument

    monkeypatch.setattr(type(bundle), "revalidate_runtime_sources", revalidate)
    monkeypatch.setattr(renderer_module, "create_instrument", create)
    sink, _blocks, _offsets = _collecting_sink()
    render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)

    assert calls == [
        "revalidate",
        "factory",
        "revalidate",
        "close",
        "revalidate",
    ]


def test_post_factory_runtime_change_closes_once_and_writes_no_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, generation = _bundle(tmp_path, monkeypatch)
    closed = 0
    revalidations = 0
    original_revalidate = type(bundle).revalidate_runtime_sources
    original_create = renderer_module.create_instrument

    def revalidate(self):
        nonlocal revalidations
        revalidations += 1
        if revalidations == 2:
            generation["value"] = "changed"
        return original_revalidate(self)

    def create(*args, **kwargs):
        instrument = original_create(*args, **kwargs)

        def close():
            nonlocal closed
            closed += 1

        instrument.close = close
        return instrument

    monkeypatch.setattr(type(bundle), "revalidate_runtime_sources", revalidate)
    monkeypatch.setattr(renderer_module, "create_instrument", create)
    sink, blocks, _offsets = _collecting_sink()
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)
    assert caught.value.code == "renderer.runtime_generation_changed"
    assert closed == 1
    assert blocks == []


def test_final_runtime_change_rejects_receipt_after_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, generation = _bundle(tmp_path, monkeypatch)
    closed = 0
    original_create = renderer_module.create_instrument

    def create(*args, **kwargs):
        instrument = original_create(*args, **kwargs)

        def close():
            nonlocal closed
            closed += 1
            generation["value"] = "changed-during-close"

        instrument.close = close
        return instrument

    monkeypatch.setattr(renderer_module, "create_instrument", create)
    sink, blocks, _offsets = _collecting_sink()
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)
    assert caught.value.code == "renderer.runtime_generation_changed"
    assert closed == 1
    assert sum(len(block) for block in blocks) == bundle.frame_count


def test_factory_provenance_mismatch_fails_before_audio_and_closes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    closed = 0
    original_create = renderer_module.create_instrument

    def create(*args, **kwargs):
        instrument = original_create(*args, **kwargs)
        instrument._tianlai_factory_provenance["sample_rate_hz"] = 16_000

        def close():
            nonlocal closed
            closed += 1

        instrument.close = close
        return instrument

    monkeypatch.setattr(renderer_module, "create_instrument", create)
    sink, blocks, _offsets = _collecting_sink()
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)
    assert caught.value.code == "renderer.factory_provenance_mismatch"
    assert closed == 1
    assert blocks == []


def test_sink_failure_closes_once_and_never_returns_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    closed = 0
    original_create = renderer_module.create_instrument

    def create(*args, **kwargs):
        instrument = original_create(*args, **kwargs)

        def close():
            nonlocal closed
            closed += 1

        instrument.close = close
        return instrument

    def sink(_block, _offset):
        raise RuntimeError("private sink failed")

    monkeypatch.setattr(renderer_module, "create_instrument", create)
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)
    assert caught.value.code == "renderer.sink_failed"
    assert closed == 1


def test_sink_failure_is_not_masked_when_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    original_create = renderer_module.create_instrument

    def create(*args, **kwargs):
        instrument = original_create(*args, **kwargs)

        def close():
            raise RuntimeError("close failed too")

        instrument.close = close
        return instrument

    def sink(_block, _offset):
        raise RuntimeError("private sink failed first")

    monkeypatch.setattr(renderer_module, "create_instrument", create)
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)
    assert caught.value.code == "renderer.sink_failed"
    assert getattr(caught.value, "__notes__", []) == [
        "instrument close also failed"
    ]


def test_sink_failure_is_not_masked_by_close_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    original_create = renderer_module.create_instrument

    def create(*args, **kwargs):
        instrument = original_create(*args, **kwargs)

        def close():
            raise KeyboardInterrupt("close interrupted")

        instrument.close = close
        return instrument

    def sink(_block, _offset):
        raise RuntimeError("private sink failed first")

    monkeypatch.setattr(renderer_module, "create_instrument", create)
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(
            bundle, executor_id, sink
        )
    assert caught.value.code == "renderer.sink_failed"
    assert getattr(caught.value, "__notes__", []) == [
        "instrument close also failed"
    ]


def test_block_request_has_a_fixed_memory_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    sink, blocks, _offsets = _collecting_sink()
    with pytest.raises(ValueError, match="between 1 and 65536"):
        render_score_v2_executor_to_private_block_sink(
            bundle,
            executor_id,
            sink,
            maximum_block_frames=65_537,
        )
    assert blocks == []


def test_sink_callback_cannot_mix_mutated_live_bundle_fields_into_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    original_frame_count = bundle.frame_count
    blocks: list[np.ndarray] = []

    def sink(block: np.ndarray, _offset: int) -> None:
        blocks.append(block.copy())
        object.__setattr__(bundle, "frame_count", original_frame_count + 1)

    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)
    assert caught.value.code == "renderer.runtime_generation_changed"
    assert sum(len(block) for block in blocks) == original_frame_count


def test_sink_cannot_reenable_writes_on_the_delivered_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    blocks: list[np.ndarray] = []

    def sink(block: np.ndarray, _offset: int) -> None:
        with pytest.raises(ValueError):
            block.setflags(write=True)
        blocks.append(block.copy())

    receipt = render_score_v2_executor_to_private_block_sink(
        bundle, executor_id, sink
    )
    assert sum(len(block) for block in blocks) == receipt.frame_count


def test_actual_block_size_cannot_exceed_the_requested_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    sink_calls = 0

    def oversized_blocks(*_args, **_kwargs):
        return iter((np.zeros((2, 2), dtype=np.float64),)), (0, 0)

    def sink(_block: np.ndarray, _offset: int) -> None:
        nonlocal sink_calls
        sink_calls += 1

    monkeypatch.setattr(
        renderer_module,
        "render_document_blocks",
        oversized_blocks,
    )
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(
            bundle,
            executor_id,
            sink,
            maximum_block_frames=1,
        )
    assert caught.value.code == "renderer.audio_block_invalid"
    assert sink_calls == 0


def test_non_oscillator_backend_is_rejected_before_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(
        tmp_path, monkeypatch, instrument_type="synthesizer"
    )
    factory_calls = 0

    def create(*_args, **_kwargs):
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError

    monkeypatch.setattr(renderer_module, "create_instrument", create)
    sink, _blocks, _offsets = _collecting_sink()
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(bundle, executor_id, sink)
    assert caught.value.code == "renderer.backend_scope_unsupported"
    assert factory_calls == 0


@pytest.mark.parametrize(
    "manifest_overrides",
    (
        {"external_audio_assets": "not-a-list"},
        {"asset_root": None},
        {"asset_root": ""},
        {"soundfont": None},
        {"soundfont": "   "},
        {"sample": None},
        {"sample": ""},
    ),
)
def test_malformed_asset_declaration_is_rejected_during_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manifest_overrides: dict[str, object],
) -> None:
    with pytest.raises(ScoreV2RuntimeSourceError) as caught:
        _bundle(
            tmp_path,
            monkeypatch,
            manifest_overrides=manifest_overrides,
        )
    assert caught.value.code == (
        "runtime_source.asset_inventory_declaration_mismatch"
    )


def test_asset_field_presence_is_outside_the_oscillator_renderer_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(
        tmp_path,
        monkeypatch,
        manifest_overrides={"regions": []},
    )
    sink, blocks, _offsets = _collecting_sink()
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(
            bundle, executor_id, sink
        )
    assert caught.value.code == "renderer.backend_scope_unsupported"
    assert blocks == []


def test_wrong_factory_type_remains_primary_when_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)

    class WrongInstrument(Instrument):
        def handle_event(self, event, tuning) -> None:
            del event, tuning

        def render_frame(self) -> tuple[float, float]:
            return 0.0, 0.0

        @property
        def active_voice_count(self) -> int:
            return 0

        def close(self) -> None:
            raise RuntimeError("close failed too")

    monkeypatch.setattr(
        renderer_module,
        "create_instrument",
        lambda *_args, **_kwargs: WrongInstrument(8_000),
    )
    sink, _blocks, _offsets = _collecting_sink()
    with pytest.raises(ScoreV2RendererError) as caught:
        render_score_v2_executor_to_private_block_sink(
            bundle, executor_id, sink
        )
    assert caught.value.code == "renderer.backend_scope_unsupported"
    assert getattr(caught.value, "__notes__", []) == [
        "instrument close also failed"
    ]


def test_receipt_detects_in_memory_field_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    sink, _blocks, _offsets = _collecting_sink()
    receipt = render_score_v2_executor_to_private_block_sink(
        bundle, executor_id, sink
    )
    object.__setattr__(receipt, "frame_count", receipt.frame_count + 1)
    with pytest.raises(ScoreV2RendererError) as caught:
        receipt.to_dict()
    assert caught.value.code == "renderer.receipt_integrity_mismatch"


def test_receipt_rejects_consistent_in_object_reseal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    sink, _blocks, _offsets = _collecting_sink()
    receipt = render_score_v2_executor_to_private_block_sink(
        bundle, executor_id, sink
    )
    forged = receipt.to_dict()
    forged["audio_stream_sha256"] = "0" * 64
    payload = canonical_json_bytes(forged)
    digest = hashlib.sha256(payload).hexdigest()
    seal = list(receipt._identity_seal)
    seal[6] = payload
    seal[7] = digest
    object.__setattr__(receipt, "_canonical_bytes", payload)
    object.__setattr__(receipt, "_artifact_sha256", digest)
    object.__setattr__(receipt, "_identity_seal", tuple(seal))

    with pytest.raises(ScoreV2RendererError) as caught:
        receipt.to_dict()
    assert caught.value.code == "renderer.receipt_integrity_mismatch"


class _EndpointProbe(Instrument):
    def __init__(self) -> None:
        super().__init__(8_000)
        self.events: list[str] = []
        self.frames = 0

    def handle_event(self, event, tuning) -> None:
        del tuning
        self.events.append(event.type)

    def render_frame(self) -> tuple[float, float]:
        self.frames += 1
        return 0.0, 0.0

    @property
    def active_voice_count(self) -> int:
        return 0


def test_legacy_renderer_endpoint_behavior_remains_unchanged() -> None:
    from tianlai.renderer import render_document

    probe = _EndpointProbe()
    document = PerformanceDocument(
        sample_rate=8_000,
        channels=2,
        total_samples=1,
        events=(
            PerformanceEvent(1, 0, "note_off", {"note_id": 1}),
        ),
        tuning=EqualTemperament(),
    )
    frames, _peak = render_document(probe, document)
    assert list(frames) == [(0.0, 0.0)]
    assert probe.frames == 1
    assert probe.events == []


def test_private_pcm24_stage_is_bound_and_auto_retired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "本地试听暂存"
    staging.mkdir()

    with stage_score_v2_executor_pcm24_wav(
        bundle,
        executor_id,
        staging_directory=staging,
        maximum_block_frames=5_000,
    ) as stage:
        path = stage.path
        document = stage.to_dict()
        assert path.parent == staging.resolve()
        assert path.name.startswith(".tianlai-score-v2-private-")
        assert path.is_file()
        assert stage.active is True
        stage.revalidate_private_wav()
        assert document["contract"] == SCORE_V2_PRIVATE_WAV_STAGE_CONTRACT
        assert document["render_authority"] is False
        assert document["publish_authority"] is False
        assert document["candidate_authority"] is False
        assert stage.wav_size_bytes == 44 + 6 * stage.frame_count
        payload = path.read_bytes()
        assert len(payload) == stage.wav_size_bytes
        assert hashlib.sha256(payload).hexdigest() == stage.wav_sha256
        with wave.open(str(path), "rb") as source:
            assert source.getnchannels() == 2
            assert source.getsampwidth() == 3
            assert source.getframerate() == stage.sample_rate
            assert source.getnframes() == stage.frame_count
        assert document["bindings"]["local_execution_receipt_sha256"] == (
            stage.local_execution_receipt.artifact_sha256
        )
        assert document["float_stream_sha256"] == (
            stage.local_execution_receipt.to_dict()["audio_stream_sha256"]
        )

    assert path.exists() is False
    assert stage.active is False
    with pytest.raises(ScoreV2PrivateWavError) as caught:
        stage.revalidate_private_wav()
    assert caught.value.code == "stage.retired"


def test_private_pcm24_stage_is_deterministic_for_the_same_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()
    hashes: list[tuple[str, str]] = []

    for _ in range(2):
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
        ) as stage:
            hashes.append((stage.wav_sha256, stage.artifact_sha256))

    assert hashes[0] == hashes[1]
    assert list(staging.iterdir()) == []


def test_private_pcm24_output_budget_fails_before_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()
    limits = ProjectLimits(max_primary_output_bytes=43)

    with pytest.raises(ResourceLimitError) as caught:
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
            limits=limits,
        ):
            raise AssertionError("unreachable")
    assert caught.value.code == "render.output_budget_exceeded"
    assert list(staging.iterdir()) == []


def test_private_pcm24_stage_detects_wav_generation_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ScoreV2PrivateWavError) as cleanup:
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
        ) as stage:
            with stage.path.open("r+b") as output:
                output.seek(44)
                original = output.read(1)
                output.seek(44)
                output.write(bytes((original[0] ^ 1,)))
                output.flush()
            with pytest.raises(ScoreV2PrivateWavError) as caught:
                stage.revalidate_private_wav()
            assert caught.value.code == "stage.private_wav_generation_changed"
    assert cleanup.value.code == "stage.cleanup_failed"
    assert len(list(staging.iterdir())) == 1


def test_private_pcm24_stage_rejects_consistent_evidence_reseal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()

    with stage_score_v2_executor_pcm24_wav(
        bundle,
        executor_id,
        staging_directory=staging,
    ) as stage:
        forged = stage.to_dict()
        forged["wav"]["sha256"] = "0" * 64
        payload = canonical_json_bytes(forged)
        digest = hashlib.sha256(payload).hexdigest()
        object.__setattr__(stage, "wav_sha256", "0" * 64)
        object.__setattr__(stage, "_canonical_bytes", payload)
        object.__setattr__(stage, "_artifact_sha256", digest)
        with pytest.raises(ScoreV2PrivateWavError) as caught:
            stage.to_dict()
        assert caught.value.code == "stage.evidence_integrity_mismatch"


def test_private_pcm24_writer_failure_retires_claimed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()

    def fail_write(*_args, **_kwargs):
        raise OSError("injected PCM write failure")

    monkeypatch.setattr(private_wav_module, "_write_numpy_pcm24", fail_write)
    with pytest.raises(ScoreV2PrivateWavError) as caught:
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
        ):
            raise AssertionError("unreachable")
    assert caught.value.code == "stage.local_execution_failed"
    assert list(staging.iterdir()) == []


@pytest.mark.parametrize(
    ("limits", "expected_code"),
    (
        (
            ProjectLimits(
                max_plan_seconds=1,
                max_primary_output_bytes=1_000_000,
            ),
            "render.duration_too_long",
        ),
        (
            ProjectLimits(
                max_audio_memory_bytes=1,
                max_primary_output_bytes=1_000_000,
            ),
            "render.memory_budget_exceeded",
        ),
    ),
)
def test_private_pcm24_stage_honors_duration_and_memory_limits_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limits: ProjectLimits,
    expected_code: str,
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(ResourceLimitError) as caught:
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
            limits=limits,
        ):
            raise AssertionError("unreachable")
    assert caught.value.code == expected_code
    assert list(staging.iterdir()) == []


def test_private_pcm24_stage_enforces_actual_block_ceiling_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()
    sink, _blocks, _offsets = _collecting_sink()
    genuine_receipt = render_score_v2_executor_to_private_block_sink(
        bundle, executor_id, sink
    )

    def oversized_renderer(
        _bundle_value,
        _executor_id,
        private_sink,
        *,
        maximum_block_frames,
    ):
        assert maximum_block_frames == 1
        private_sink(
            np.zeros((bundle.frame_count, 2), dtype="<f8"),
            0,
        )
        return genuine_receipt

    monkeypatch.setattr(
        private_wav_module,
        "render_score_v2_executor_to_private_block_sink",
        oversized_renderer,
    )
    with pytest.raises(ScoreV2PrivateWavError) as caught:
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
            maximum_block_frames=1,
        ):
            raise AssertionError("unreachable")
    assert caught.value.code == "stage.audio_block_invalid"
    assert list(staging.iterdir()) == []


def test_private_pcm24_stage_reports_relocated_claim_as_cleanup_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()
    moved = staging / "moved.wav"

    with pytest.raises(ScoreV2PrivateWavError) as caught:
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
        ) as stage:
            os.rename(stage.path, moved)
    assert caught.value.code == "stage.cleanup_failed"
    assert moved.is_file()


def test_private_pcm24_preyield_integrity_failure_still_retires_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()
    real_prepare = private_wav_module._prepare_private_stage

    def corrupt_registration(*args, **kwargs):
        stage, claim = real_prepare(*args, **kwargs)
        private_wav_module._STAGE_GENERATIONS.pop(id(stage))
        return stage, claim

    monkeypatch.setattr(
        private_wav_module,
        "_prepare_private_stage",
        corrupt_registration,
    )
    with pytest.raises(ScoreV2PrivateWavError) as caught:
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
        ):
            raise AssertionError("unreachable")
    assert caught.value.code == "stage.evidence_integrity_mismatch"
    assert list(staging.iterdir()) == []


def test_private_pcm24_cleanup_note_failure_never_masks_body_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, executor_id, _generation = _bundle(tmp_path, monkeypatch)
    staging = tmp_path / "staging"
    staging.mkdir()

    class BodyError(Exception):
        def add_note(self, note: str) -> None:
            del note
            raise RuntimeError("hostile add_note")

    retire_called = False

    def fail_retire(*_args, **_kwargs):
        nonlocal retire_called
        retire_called = True
        raise OSError("injected retire failure")

    monkeypatch.setattr(
        private_wav_module,
        "_retire_sealed_private_file",
        fail_retire,
    )
    with pytest.raises(BodyError, match="body failed"):
        with stage_score_v2_executor_pcm24_wav(
            bundle,
            executor_id,
            staging_directory=staging,
        ):
            raise BodyError("body failed")
    assert retire_called is True
