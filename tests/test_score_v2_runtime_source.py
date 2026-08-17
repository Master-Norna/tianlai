from __future__ import annotations

from dataclasses import replace
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
from tianlai.score_v2_capability_adapter import (
    compile_score_v2_capability_plan,
)
from tianlai.score_v2_capability_source import (
    capture_score_v2_capability_sources,
)
from tianlai.score_v2_execution_profile import (
    parse_score_v2_execution_profile,
)
from tianlai.score_v2_plan import compile_score_v2_plan
import tianlai.score_v2_runtime_source as runtime_source_module
from tianlai.score_v2_runtime_source import (
    ASSET_DESCRIPTOR_STATUS,
    NO_EXTERNAL_ASSET_INVENTORY_STATUS,
    RUNTIME_FINGERPRINT_STATUS,
    SCORE_V2_RUNTIME_SOURCE_CONTRACT,
    ScoreV2RuntimeSourceError,
    ScoreV2RuntimeSourceSnapshot,
    capture_score_v2_runtime_sources,
)


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _score() -> dict[str, object]:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "runtime source fixture",
        "timeline": {
            "measures": [
                {
                    "measure_id": "m1",
                    "actual_duration_quarters": _r(4),
                }
            ],
            "meter_events": [
                {
                    "meter_id": "meter-1",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
                    "groups": [4],
                    "beat_unit": 4,
                }
            ],
            "tempo_events": [
                {
                    "tempo_id": "tempo-60",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
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
            {
                "part_id": "lead",
                "default_dynamic": "mf",
                "notes": [
                    {
                        "event_id": "n1",
                        "position": {
                            "measure_id": "m1",
                            "offset_quarters": _r(0),
                        },
                        "duration_quarters": _r(1),
                        "written_pitch": {
                            "step": "C",
                            "alter": _r(0),
                            "octave": 4,
                        },
                        "sounding_pitch": {"midi_note": _r(60)},
                    }
                ],
            }
        ],
        "form": {"mode": "linear"},
    }


def _profile() -> dict[str, object]:
    return {
        "kind": "tianlai.score_v2_execution_profile",
        "schema_version": 1,
        "sample_time_policy": "exact",
        "dynamic_profile": {
            "mf": {"numerator": 3, "denominator": 5},
        },
        "note_velocity": {
            "value_policy": "adapt",
            "semantic_policy": "approximate",
        },
        "tuning": {
            "value_policy": "exact",
            "semantic_policy": "exact",
        },
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


def _context(
    tmp_path: Path,
    *,
    manifest_extra: dict[str, object] | None = None,
    executor_count: int = 1,
):
    instrument_dir = tmp_path / "instrument"
    instrument_dir.mkdir(parents=True)
    manifest_path = instrument_dir / "instrument.json"
    manifest_document: dict[str, object] = {
        "name": "runtime fixture",
        "type": "oscillator",
        "note_min": 0,
        "note_max": 127,
        "articulation_auto_default": False,
    }
    manifest_document.update(manifest_extra or {})
    manifest_path.write_text(
        json.dumps(
            manifest_document,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    capability = read_capability(manifest_path, root=tmp_path)
    if executor_count not in (1, 2):
        raise ValueError("test fixture supports one or two executors")
    score_document = _score()
    assignments = [
        {
            "part": "lead",
            "instrument": capability.relative_path,
            "articulation_auto": False,
        }
    ]
    if executor_count == 2:
        parts = score_document["parts"]
        assert type(parts) is list and type(parts[0]) is dict
        second_part = json.loads(json.dumps(parts[0]))
        second_part["part_id"] = "support"
        second_part["notes"][0]["event_id"] = "n2"
        parts.append(second_part)
        assignments.append(
            {
                "part": "support",
                "instrument": capability.relative_path,
                "articulation_auto": False,
            }
        )
    roster = parse_roster_document(
        {
            "name": "runtime roster",
            "assignments": assignments,
        },
        {capability.relative_path: capability},
    )
    score_source = snapshot_score_document(score_document)
    profile = parse_score_v2_execution_profile(_profile())
    dynamic_profile = {
        level.mark: Rational(level.value.numerator, level.value.denominator)
        for level in profile.dynamic_profile
    }
    score_plan = compile_score_v2_plan(
        score_source,
        sample_rate=8_000,
        sample_time_policy=profile.sample_time_policy,  # type: ignore[arg-type]
        dynamic_profile=dynamic_profile,
    )
    capability_sources = capture_score_v2_capability_sources(
        roster,
        catalogue_root=tmp_path,
    )
    capability_plan = compile_score_v2_capability_plan(
        score_source,
        score_plan,
        profile,
        roster,
        capability_sources,
    )
    return capability_plan, capability_sources, manifest_path


def _fingerprint(
    root: Path,
    manifest_path: Path,
    *,
    sample_rate: int = 8_000,
    generation: str = "initial",
    file_count: int = 1,
) -> dict[str, object]:
    empty = hashlib.sha256(b"").hexdigest()
    closure = {
        "algorithm": "test-render-closure-v1",
        "entry_modules": ["tianlai.renderer"],
        "file_count": 1,
        "files": [{"path": "tianlai/renderer.py", "sha256": empty}],
        "sha256": hashlib.sha256(generation.encode("utf-8")).hexdigest(),
    }
    dependencies = {
        "python": {"version": "test"},
        "generation": generation,
    }
    graph = {
        "algorithm": "constructed-runtime-asset-graph-v1",
        "sample_rate_hz": sample_rate,
        "file_count": file_count,
        "total_bytes": 1 if file_count else 0,
        "region_count": 1 if file_count else 0,
        "sha256": hashlib.sha256(
            f"asset:{generation}:{file_count}".encode("utf-8")
        ).hexdigest(),
    }
    return {
        "algorithm": "sha256-path-content-v1",
        "manifest": {
            "path": manifest_path.resolve().relative_to(root.resolve()).as_posix(),
            "sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
        "render_python_closure": closure,
        "runtime_dependencies": dependencies,
        "local_implementation": {"path": None, "sha256": None},
        "resource_verification": {"path": None, "sha256": None},
        "pitch_calibration": {"path": None, "sha256": None},
        "runtime_asset_graph": graph,
    }


def _install_runtime_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    generation: dict[str, str] | None = None,
    file_count: int = 1,
) -> list[tuple[dict[str, object], int]]:
    active = generation or {"value": "initial"}
    calls: list[tuple[dict[str, object], int]] = []

    def compute(
        project_root: Path,
        manifest_path: str,
        *,
        effective_manifest: dict[str, object],
        sample_rate_hz: int,
    ) -> dict[str, object]:
        calls.append((dict(effective_manifest), sample_rate_hz))
        return _fingerprint(
            Path(project_root),
            Path(manifest_path),
            sample_rate=sample_rate_hz,
            generation=active["value"],
            file_count=file_count,
        )

    def validate(
        fingerprint: dict[str, object],
        *,
        project_root: Path | str,
        manifest_path: str,
        effective_manifest: dict[str, object],
        sample_rate_hz: int,
    ) -> dict[str, object]:
        current = _fingerprint(
            Path(project_root),
            Path(manifest_path),
            sample_rate=sample_rate_hz,
            generation=active["value"],
            file_count=file_count,
        )
        if fingerprint != current:
            raise OnsetEvidenceError("stale test runtime")
        return fingerprint

    monkeypatch.setattr(runtime_source_module, "compute_runtime_fingerprint", compute)
    monkeypatch.setattr(runtime_source_module, "validate_runtime_fingerprint", validate)
    return calls


def _error_code(callable_: object) -> str:
    with pytest.raises(ScoreV2RuntimeSourceError) as caught:
        callable_()  # type: ignore[operator]
    assert str(caught.value) == caught.value.code
    return caught.value.code


def test_capture_seals_per_executor_legacy_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    calls = _install_runtime_fakes(monkeypatch)

    snapshot = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )
    document = snapshot.to_dict()
    executor = document["executors"][0]

    assert type(snapshot) is ScoreV2RuntimeSourceSnapshot
    assert document["contract"] == SCORE_V2_RUNTIME_SOURCE_CONTRACT
    assert document["render_authority"] is False
    assert document["limitations"]["asset_descriptor_status"] == (
        ASSET_DESCRIPTOR_STATUS
    )
    assert document["limitations"]["malicious_aba_resistance"] == (
        "not_claimed"
    )
    assert document["limitations"]["runtime_generation_set_atomicity"] == (
        "sequential_observations_not_atomic"
    )
    assert document["limitations"]["ordinary_generation_replacement"] == (
        "rejected_when_observed"
    )
    assert document["limitations"]["onset_evidence_status"] == "not_captured"
    assert document["limitations"]["lazy_asset_generation"] == (
        "legacy_path_reopen"
    )
    assert document["limitations"]["factory_instance_generation"] == (
        "not_captured"
    )
    assert document["limitations"]["factory_provenance_status"] == (
        "pending_render_transaction"
    )
    assert executor["runtime_fingerprint_status"] == RUNTIME_FINGERPRINT_STATUS
    assert executor["capability_plan_sha256"] == capability_plan.artifact_sha256
    assert executor["capability_source_sha256"] == (
        capability_sources.artifact_sha256
    )
    assert executor["sample_rate"] == 8_000
    assert executor["runtime_evidence"]["runtime_asset_graph"]["file_count"] == 1
    assert executor["runtime_evidence"]["asset_descriptor_status"] == (
        ASSET_DESCRIPTOR_STATUS
    )
    assert executor["runtime_evidence"]["render_python_closure_sha256"] == (
        canonical_json_sha256(
            executor["legacy_runtime_fingerprint"]["render_python_closure"]
        )
    )
    assert snapshot.canonical_bytes == canonical_json_bytes(document)
    assert snapshot.artifact_sha256 == hashlib.sha256(
        snapshot.canonical_bytes
    ).hexdigest()
    assert len(calls) == 1
    assert calls[0][1] == 8_000


def test_revalidation_rejects_runtime_generation_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    generation = {"value": "initial"}
    _install_runtime_fakes(monkeypatch, generation=generation)
    snapshot = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )

    snapshot.revalidate_runtime_sources()
    generation["value"] = "replacement"
    assert _error_code(snapshot.revalidate_runtime_sources) == (
        "runtime_source.runtime_generation_changed"
    )


def test_revalidation_rejects_manifest_generation_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, manifest = _context(tmp_path)
    _install_runtime_fakes(monkeypatch)
    snapshot = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )
    manifest.write_text('{"name":"replacement","type":"oscillator"}', encoding="utf-8")

    assert _error_code(snapshot.revalidate_runtime_sources) == (
        "runtime_source.capability_source_changed"
    )


def test_empty_runtime_asset_graph_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    _install_runtime_fakes(monkeypatch, file_count=0)

    assert _error_code(
        lambda: capture_score_v2_runtime_sources(
            capability_plan,
            capability_sources,
            project_root=tmp_path,
        )
    ) == "runtime_source.empty_runtime_asset_graph"


@pytest.mark.parametrize(
    "declaration",
    [
        {"runtime_asset_policy": "no_external_audio_assets"},
        {
            "provenance_kind": "project_authored_dsp",
            "external_audio_assets": [],
        },
    ],
)
def test_explicit_asset_free_dsp_accepts_and_records_empty_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declaration: dict[str, object],
) -> None:
    capability_plan, capability_sources, _manifest = _context(
        tmp_path,
        manifest_extra=declaration,
    )
    _install_runtime_fakes(monkeypatch, file_count=0)

    snapshot = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )
    executor = snapshot.to_dict()["executors"][0]

    assert executor["asset_inventory_status"] == (
        NO_EXTERNAL_ASSET_INVENTORY_STATUS
    )
    assert executor["runtime_evidence"]["asset_inventory_status"] == (
        NO_EXTERNAL_ASSET_INVENTORY_STATUS
    )
    assert executor["runtime_evidence"]["runtime_asset_graph"]["file_count"] == 0
    snapshot.revalidate_runtime_sources()


def test_mismatched_capability_plan_and_source_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, _first_sources, _manifest = _context(tmp_path / "first")
    _other_plan, other_sources, _other_manifest = _context(tmp_path / "second")
    calls = _install_runtime_fakes(monkeypatch)

    assert _error_code(
        lambda: capture_score_v2_runtime_sources(
            capability_plan,
            other_sources,
            project_root=tmp_path,
        )
    ) == "runtime_source.capability_binding_mismatch"
    assert calls == []


def test_custom_and_wrong_eligibility_bindings_fail_closed(
    tmp_path: Path,
) -> None:
    _plan, sources, _manifest = _context(tmp_path)
    source = sources.manifest_generations[0]
    binding = sources.executor_bindings[0]

    custom = binding._replace(custom_implementation_blocked=True)
    assert _error_code(
        lambda: runtime_source_module._effective_manifest(source, custom)
    ) == "runtime_source.custom_implementation_blocked"

    ineligible = binding._replace(execution_eligibility="ready")
    assert _error_code(
        lambda: runtime_source_module._effective_manifest(source, ineligible)
    ) == "runtime_source.execution_eligibility_mismatch"


def test_capture_rejects_manifest_change_during_runtime_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, manifest = _context(tmp_path)
    original = _fingerprint(tmp_path, manifest)

    def compute(*_args: object, **_kwargs: object) -> dict[str, object]:
        manifest.write_text(
            '{"name":"changed during callback","type":"oscillator"}',
            encoding="utf-8",
        )
        return original

    monkeypatch.setattr(runtime_source_module, "compute_runtime_fingerprint", compute)
    monkeypatch.setattr(
        runtime_source_module,
        "validate_runtime_fingerprint",
        lambda fingerprint, **_kwargs: fingerprint,
    )

    assert _error_code(
        lambda: capture_score_v2_runtime_sources(
            capability_plan,
            capability_sources,
            project_root=tmp_path,
        )
    ) == "runtime_source.capability_source_changed"


def test_final_whole_set_pass_rejects_post_capture_runtime_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, manifest = _context(tmp_path)
    initial = _fingerprint(tmp_path, manifest)
    validations = 0

    monkeypatch.setattr(
        runtime_source_module,
        "compute_runtime_fingerprint",
        lambda *_args, **_kwargs: initial,
    )

    def validate(
        fingerprint: dict[str, object],
        **_kwargs: object,
    ) -> dict[str, object]:
        nonlocal validations
        validations += 1
        if validations > 1:
            raise OnsetEvidenceError("changed after first executor pass")
        return fingerprint

    monkeypatch.setattr(
        runtime_source_module,
        "validate_runtime_fingerprint",
        validate,
    )

    assert _error_code(
        lambda: capture_score_v2_runtime_sources(
            capability_plan,
            capability_sources,
            project_root=tmp_path,
        )
    ) == "runtime_source.runtime_generation_changed"
    assert validations == 2


def test_artifact_budget_is_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    calls = 0

    def compute(*_args: object, **_kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        raise AssertionError("fixed artifact framing must be rejected first")

    monkeypatch.setattr(
        runtime_source_module,
        "compute_runtime_fingerprint",
        compute,
    )
    limits = replace(ProjectLimits(), max_plan_json_bytes=256)

    with pytest.raises(ResourceLimitError) as caught:
        capture_score_v2_runtime_sources(
            capability_plan,
            capability_sources,
            project_root=tmp_path,
            limits=limits,
        )
    assert caught.value.code == "runtime_source.document_too_large"
    assert calls == 0


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("max_notes", "runtime_source.too_many_occurrences"),
        ("max_executors", "runtime_source.too_many_executors"),
    ],
)
def test_count_limits_precede_external_revalidation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    code: str,
) -> None:
    capability_plan, capability_sources, _manifest = _context(
        tmp_path,
        executor_count=2,
    )

    def unexpected_revalidation(_self: object) -> None:
        raise AssertionError("count ceilings must precede external I/O")

    monkeypatch.setattr(
        type(capability_sources),
        "revalidate_sources",
        unexpected_revalidation,
    )
    limits = replace(ProjectLimits(), **{field: 1})

    with pytest.raises(ResourceLimitError) as caught:
        capture_score_v2_runtime_sources(
            capability_plan,
            capability_sources,
            project_root=tmp_path,
            limits=limits,
        )
    assert caught.value.code == code


def test_artifact_budget_accepts_exact_final_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    _install_runtime_fakes(monkeypatch)
    baseline = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )
    exact_size = len(baseline.canonical_bytes)

    rebuilt = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
        limits=replace(ProjectLimits(), max_plan_json_bytes=exact_size),
    )

    assert len(rebuilt.canonical_bytes) == exact_size


def test_runtime_revalidation_normalizes_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    _install_runtime_fakes(monkeypatch)
    snapshot = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )
    monkeypatch.setattr(
        runtime_source_module,
        "validate_runtime_fingerprint",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("runtime callback changed")
        ),
    )

    assert _error_code(snapshot.revalidate_runtime_sources) == (
        "runtime_source.runtime_generation_changed"
    )


def test_snapshot_field_tamper_breaks_identity_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    _install_runtime_fakes(monkeypatch)
    snapshot = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )

    object.__setattr__(snapshot, "sample_rate", 44_100)
    assert _error_code(snapshot.to_dict) == "runtime_source.integrity_mismatch"


def test_snapshot_missing_field_has_stable_integrity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(tmp_path)
    _install_runtime_fakes(monkeypatch)
    snapshot = capture_score_v2_runtime_sources(
        capability_plan,
        capability_sources,
        project_root=tmp_path,
    )

    object.__delattr__(snapshot, "sample_rate")
    assert _error_code(snapshot.to_dict) == "runtime_source.integrity_mismatch"


def test_catalogue_must_be_within_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability_plan, capability_sources, _manifest = _context(
        tmp_path / "catalogue"
    )
    _install_runtime_fakes(monkeypatch)

    assert _error_code(
        lambda: capture_score_v2_runtime_sources(
            capability_plan,
            capability_sources,
            project_root=tmp_path / "another-root",
        )
    ) == "runtime_source.project_root_unavailable"
