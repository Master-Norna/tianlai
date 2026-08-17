from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tianlai.canonical_json import canonical_json_bytes, canonical_json_sha256
from tianlai.capability import read_capability
from tianlai.onset_evidence import OnsetEvidenceError
from tianlai.resource_limits import ProjectLimits, ResourceLimitError
from tianlai.roster import parse_roster_document
from tianlai.score_source import snapshot_score_document
from tianlai.score_v2 import Rational
from tianlai.score_v2_capability_adapter import compile_score_v2_capability_plan
from tianlai.score_v2_capability_source import capture_score_v2_capability_sources
from tianlai.score_v2_execution_profile import parse_score_v2_execution_profile
from tianlai.score_v2_performance import (
    ENDPOINT_DISPATCH_STATUS,
    SCORE_V2_PERFORMANCE_CONTRACT,
    ScoreV2PerformanceBundle,
    ScoreV2PerformanceError,
    compile_score_v2_performance_bundle,
)
from tianlai.score_v2_plan import compile_score_v2_plan
import tianlai.score_v2_runtime_source as runtime_source_module
from tianlai.score_v2_runtime_source import capture_score_v2_runtime_sources


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _note(
    event_id: str,
    offset: int,
    duration: int,
    pitch: int,
    *,
    articulation: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "event_id": event_id,
        "position": {
            "measure_id": "m1",
            "offset_quarters": _r(offset),
        },
        "duration_quarters": _r(duration),
        "written_pitch": {"step": "C", "alter": _r(0), "octave": 4},
        "sounding_pitch": {"midi_note": _r(pitch)},
    }
    if articulation is not None:
        value["articulations"] = [articulation]
    return value


def _score(notes: list[dict[str, object]]) -> dict[str, object]:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "performance transport fixture",
        "timeline": {
            "measures": [
                {"measure_id": "m1", "actual_duration_quarters": _r(4)}
            ],
            "meter_events": [
                {
                    "meter_id": "meter-1",
                    "at": {"measure_id": "m1", "offset_quarters": _r(0)},
                    "groups": [4],
                    "beat_unit": 4,
                }
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo-60",
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
        "parts": [
            {"part_id": "lead", "default_dynamic": "mf", "notes": notes}
        ],
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
        "runtime_dependencies": {"python": {"version": "test"}, "generation": generation},
        "local_implementation": {"path": None, "sha256": None},
        "resource_verification": {"path": None, "sha256": None},
        "pitch_calibration": {"path": None, "sha256": None},
        "runtime_asset_graph": {
            "algorithm": "constructed-runtime-asset-graph-v1",
            "sample_rate_hz": sample_rate,
            "file_count": 1,
            "total_bytes": 1,
            "region_count": 1,
            "sha256": hashlib.sha256(f"asset:{generation}".encode()).hexdigest(),
        },
    }


def _context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    notes: list[dict[str, object]] | None = None,
    instrument_type: str = "oscillator",
    articulations: tuple[str, ...] = (),
):
    generation = {"value": "initial"}
    instrument_dir = tmp_path / "instrument"
    instrument_dir.mkdir(parents=True)
    manifest = instrument_dir / "instrument.json"
    manifest_document: dict[str, object] = {
        "name": "performance fixture",
        "type": instrument_type,
        "note_min": 0,
        "note_max": 127,
        "articulation_auto_default": False,
    }
    if articulations:
        manifest_document["allowed_articulations"] = list(articulations)
    manifest.write_text(json.dumps(manifest_document), encoding="utf-8")

    def compute(
        project_root: Path,
        manifest_path: str,
        *,
        effective_manifest: dict[str, object],
        sample_rate_hz: int,
    ) -> dict[str, object]:
        del effective_manifest
        return _fingerprint(
            Path(project_root),
            Path(manifest_path),
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
    capability = read_capability(manifest, root=tmp_path)
    roster = parse_roster_document(
        {
            "name": "performance roster",
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
        _score(notes or [_note("n1", 0, 1, 60)])
    )
    profile = parse_score_v2_execution_profile(_profile())
    dynamic_profile = {
        level.mark: Rational(level.value.numerator, level.value.denominator)
        for level in profile.dynamic_profile
    }
    score_plan = compile_score_v2_plan(
        source,
        sample_rate=8_000,
        sample_time_policy=profile.sample_time_policy,  # type: ignore[arg-type]
        dynamic_profile=dynamic_profile,
    )
    capability_sources = capture_score_v2_capability_sources(
        roster, catalogue_root=tmp_path
    )
    capability_plan = compile_score_v2_capability_plan(
        source, score_plan, profile, roster, capability_sources
    )
    runtime_sources = capture_score_v2_runtime_sources(
        capability_plan, capability_sources, project_root=tmp_path
    )
    return score_plan, capability_plan, runtime_sources, generation


def _compile(context, *, limits: ProjectLimits | None = None):
    score_plan, capability_plan, runtime_sources, _generation = context
    return compile_score_v2_performance_bundle(
        score_plan, capability_plan, runtime_sources, limits=limits
    )


def _reseal_bundle_document(bundle: object, document: dict[str, object]) -> None:
    document["executors_sha256"] = canonical_json_sha256(
        document["executors"]
    )
    payload = canonical_json_bytes(document)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    seal = list(bundle._identity_seal)
    seal[14] = document["executors_sha256"]
    seal[-2] = payload
    seal[-1] = artifact_hash
    object.__setattr__(bundle, "_canonical_bytes", payload)
    object.__setattr__(bundle, "_artifact_sha256", artifact_hash)
    object.__setattr__(bundle, "_identity_seal", tuple(seal))


def test_compiles_sealed_non_render_authority_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _compile(_context(tmp_path, monkeypatch))
    raw = bundle.to_dict()
    executor = raw["executors"][0]
    performance = executor["performance"]

    assert type(bundle) is ScoreV2PerformanceBundle
    assert raw["contract"] == SCORE_V2_PERFORMANCE_CONTRACT
    assert raw["render_authority"] is False
    assert raw["endpoint_dispatch_status"] == ENDPOINT_DISPATCH_STATUS
    assert performance["sample_rate"] == 8_000
    assert performance["channels"] == 2
    assert performance["tuning"] == {"temperament": "equal", "a4_hz": 440.0}
    assert performance["tail_seconds"] == 0.0
    assert bundle.canonical_bytes == canonical_json_bytes(raw)
    assert bundle.artifact_sha256 == hashlib.sha256(bundle.canonical_bytes).hexdigest()


def test_uses_resolved_values_and_sidecar_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _compile(_context(tmp_path, monkeypatch)).to_dict()
    executor = raw["executors"][0]
    events = executor["performance"]["events"]
    sidecar = executor["event_sidecar"]

    assert [event["type"] for event in events] == ["note_on", "note_off"]
    assert events[0]["pitch_hz"] == pytest.approx(261.6255653005986)
    assert events[0]["velocity"] == pytest.approx(0.6)
    assert [item["sequence"] for item in sidecar] == [0, 1]
    assert {item["occurrence_id"] for item in sidecar} == {"n1"}
    assert [item["role"] for item in sidecar] == ["note_on", "note_off"]
    assert sidecar[0]["note_id"] == sidecar[1]["note_id"] == 1


def test_final_frame_note_off_is_preserved_without_hidden_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(
        tmp_path,
        monkeypatch,
        notes=[_note("whole", 0, 4, 60)],
    )
    raw = _compile(context).to_dict()
    performance = raw["executors"][0]["performance"]
    sidecar = raw["executors"][0]["event_sidecar"]

    assert raw["frame_count"] == 32_000
    assert performance["duration_seconds"] == 4.0
    assert sidecar[-1]["expected_sample"] == raw["frame_count"]
    assert raw["frame_count_endpoint_event_count"] == 1
    assert raw["endpoint_dispatch_status"] == "pending_v2_renderer"


def test_equal_exact_boundary_orders_old_off_before_new_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(
        tmp_path,
        monkeypatch,
        notes=[_note("old", 0, 1, 60), _note("new", 1, 1, 64)],
    )
    sidecar = _compile(context).to_dict()["executors"][0]["event_sidecar"]
    at_boundary = [item for item in sidecar if item["expected_sample"] == 8_000]
    assert [(item["occurrence_id"], item["role"]) for item in at_boundary] == [
        ("old", "note_off"),
        ("new", "note_on"),
    ]


def test_runtime_generation_is_revalidated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path, monkeypatch)
    context[3]["value"] = "changed"
    with pytest.raises(ScoreV2PerformanceError) as caught:
        _compile(context)
    assert caught.value.code == "performance.runtime_generation_changed"


@pytest.mark.parametrize(
    "mutation",
    ["score_sample_rate", "score_occurrences", "runtime_bindings"],
)
def test_revalidation_callback_cannot_mix_a_live_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    context = _context(tmp_path, monkeypatch)
    score_plan, _capability_plan, runtime_sources, _generation = context
    snapshot_type = type(runtime_sources)
    original = snapshot_type.revalidate_runtime_sources
    calls = 0

    def mutating_revalidation(self: object) -> None:
        nonlocal calls
        original(self)  # type: ignore[arg-type]
        calls += 1
        if calls != 1:
            return
        if mutation == "score_sample_rate":
            object.__setattr__(score_plan, "sample_rate", 16_000)
        elif mutation == "score_occurrences":
            object.__setattr__(score_plan, "occurrences", ())
        else:
            object.__setattr__(runtime_sources, "executor_bindings", ())

    monkeypatch.setattr(
        snapshot_type,
        "revalidate_runtime_sources",
        mutating_revalidation,
    )
    with pytest.raises(ScoreV2PerformanceError) as caught:
        _compile(context)
    assert caught.value.code == "performance.runtime_generation_changed"


def test_capability_and_runtime_from_different_generations_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _context(tmp_path / "first", monkeypatch)
    second = _context(tmp_path / "second", monkeypatch)
    with pytest.raises(ScoreV2PerformanceError) as caught:
        compile_score_v2_performance_bundle(first[0], first[1], second[2])
    assert caught.value.code == "performance.binding_mismatch"


def test_bundle_identity_seal_rejects_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _compile(_context(tmp_path, monkeypatch))
    object.__setattr__(bundle, "event_count", bundle.event_count + 1)
    with pytest.raises(ScoreV2PerformanceError) as caught:
        bundle.to_dict()
    assert caught.value.code == "performance.integrity_mismatch"


def test_bundle_missing_slot_is_normalized_to_integrity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _compile(_context(tmp_path, monkeypatch))
    object.__delattr__(bundle, "event_count")
    with pytest.raises(ScoreV2PerformanceError) as caught:
        bundle.to_dict()
    assert caught.value.code == "performance.integrity_mismatch"


def test_nested_payload_reseal_cannot_replace_independent_executor_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _compile(_context(tmp_path, monkeypatch))
    forged = json.loads(bundle.canonical_bytes)
    executor = forged["executors"][0]
    executor["event_sidecar"][0]["expected_sample"] += 1
    executor["event_sidecar_sha256"] = canonical_json_sha256(
        executor["event_sidecar"]
    )
    forged["executors_sha256"] = canonical_json_sha256(forged["executors"])
    payload = canonical_json_bytes(forged)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    seal = list(bundle._identity_seal)
    seal[-2] = payload
    seal[-1] = artifact_hash
    object.__setattr__(bundle, "_canonical_bytes", payload)
    object.__setattr__(bundle, "_artifact_sha256", artifact_hash)
    object.__setattr__(bundle, "_identity_seal", tuple(seal))

    with pytest.raises(ScoreV2PerformanceError) as caught:
        bundle.to_dict()
    assert caught.value.code == "performance.integrity_mismatch"


@pytest.mark.parametrize("mutation", ["velocity", "missing_velocity", "sidecar"])
def test_consistent_reseal_cannot_diverge_from_retained_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    bundle = _compile(_context(tmp_path, monkeypatch))
    forged = json.loads(bundle.canonical_bytes)
    executor = forged["executors"][0]
    note_on = next(
        event
        for event in executor["performance"]["events"]
        if event["type"] == "note_on"
    )
    if mutation == "velocity":
        note_on["velocity"] = 0.5
    elif mutation == "missing_velocity":
        del note_on["velocity"]
    else:
        executor["event_sidecar"][0]["unexpected"] = "not-authorized"
        executor["event_sidecar_sha256"] = canonical_json_sha256(
            executor["event_sidecar"]
        )
    performance_payload = canonical_json_bytes(executor["performance"])
    executor["performance_canonical_json_bytes"] = len(performance_payload)
    executor["performance_sha256"] = hashlib.sha256(
        performance_payload
    ).hexdigest()
    _reseal_bundle_document(bundle, forged)

    with pytest.raises(ScoreV2PerformanceError) as caught:
        bundle.to_dict()
    assert caught.value.code == "performance.integrity_mismatch"


def test_consistent_reseal_cannot_rebind_retained_execution_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _compile(_context(tmp_path, monkeypatch))
    forged = json.loads(bundle.canonical_bytes)
    replacement = "0" * 64
    forged["bindings"]["execution_profile_sha256"] = replacement
    payload = canonical_json_bytes(forged)
    artifact_hash = hashlib.sha256(payload).hexdigest()
    seal = list(bundle._identity_seal)
    seal[5] = replacement
    seal[-2] = payload
    seal[-1] = artifact_hash
    object.__setattr__(bundle, "_canonical_bytes", payload)
    object.__setattr__(bundle, "_artifact_sha256", artifact_hash)
    object.__setattr__(bundle, "_identity_seal", tuple(seal))

    with pytest.raises(ScoreV2PerformanceError) as caught:
        bundle.to_dict()
    assert caught.value.code == "performance.integrity_mismatch"


@pytest.mark.parametrize("gate", ["occurrences", "executors"])
def test_cheap_count_gate_runs_before_runtime_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
) -> None:
    context = _context(tmp_path, monkeypatch)
    score_plan, capability_plan, runtime_sources, _generation = context

    def forbidden_runtime_io(self: object) -> None:
        raise AssertionError("runtime I/O must not run before cheap count gates")

    monkeypatch.setattr(
        type(runtime_sources),
        "revalidate_runtime_sources",
        forbidden_runtime_io,
    )
    if gate == "occurrences":
        object.__setattr__(capability_plan, "occurrence_count", 2)
        limits = ProjectLimits(max_notes=1)
    else:
        object.__setattr__(
            runtime_sources,
            "executor_bindings",
            runtime_sources.executor_bindings * 2,
        )
        limits = ProjectLimits(max_executors=1)
    with pytest.raises(ResourceLimitError):
        compile_score_v2_performance_bundle(
            score_plan,
            capability_plan,
            runtime_sources,
            limits=limits,
        )


def test_plan_budget_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path, monkeypatch)
    limits = ProjectLimits(max_plan_json_bytes=512)
    with pytest.raises(ResourceLimitError):
        _compile(context, limits=limits)


def test_minimum_performance_budget_precedes_runtime_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = _context(tmp_path, monkeypatch)
    runtime_sources = context[2]

    def forbidden_runtime_io(self: object) -> None:
        raise AssertionError("performance framing must be rejected first")

    monkeypatch.setattr(
        type(runtime_sources),
        "revalidate_runtime_sources",
        forbidden_runtime_io,
    )
    with pytest.raises(ResourceLimitError) as caught:
        _compile(context, limits=ProjectLimits(max_score_json_bytes=1))
    assert caught.value.code == "performance.performance_document_too_large"


def test_wrong_input_types_are_rejected() -> None:
    with pytest.raises(TypeError):
        compile_score_v2_performance_bundle(None, None, None)  # type: ignore[arg-type]
