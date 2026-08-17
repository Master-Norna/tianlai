from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from tianlai.canonical_json import canonical_json_bytes
from tianlai.onset_evidence import OnsetEvidenceError
import tianlai.score_v2_project_render as project_render_module
from tianlai.score_v2_project_render import (
    SCORE_V2_PROJECT_INPUT_CONTRACT,
    SCORE_V2_PROJECT_RENDER_COMPILATION_CONTRACT,
    SCORE_V2_PROJECT_RENDER_SCOPE,
    ScoreV2ProjectInputSnapshot,
    ScoreV2ProjectRenderCompilation,
    ScoreV2ProjectRenderError,
    capture_score_v2_project_inputs,
    compile_score_v2_project_render,
    compile_score_v2_project_render_files,
)
import tianlai.score_v2_runtime_source as runtime_source_module


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _score() -> dict[str, object]:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "project render fixture",
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
        "dynamic_profile": {"mf": _r(3, 5)},
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


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


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


def _install_runtime_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    generation = {"value": "initial"}

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
            raise OnsetEvidenceError("stale fixture generation")
        return fingerprint

    monkeypatch.setattr(runtime_source_module, "compute_runtime_fingerprint", compute)
    monkeypatch.setattr(runtime_source_module, "validate_runtime_fingerprint", validate)
    return generation


def _project_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    manifest_extra: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, Path, Path, Path]:
    project = tmp_path / "project"
    package = project / "tianlai"
    catalogue = project / "catalogue"
    instrument = catalogue / "oscillator"
    package.mkdir(parents=True)
    instrument.mkdir(parents=True)
    manifest = instrument / "乐器.json"
    manifest_document: dict[str, object] = {
        "name": "project oscillator",
        "type": "oscillator",
        "note_min": 0,
        "note_max": 127,
        "articulation_auto_default": False,
        "runtime_asset_policy": "no_external_audio_assets",
    }
    manifest_document.update(manifest_extra or {})
    _write_json(manifest, manifest_document)

    score = project / "score.json"
    roster = project / "roster.json"
    profile = project / "execution-profile.json"
    _write_json(score, _score())
    _write_json(
        roster,
        {
            "name": "project roster",
            "assignments": [
                {
                    "part": "lead",
                    "instrument": "oscillator",
                    "articulation_auto": False,
                }
            ],
        },
    )
    _write_json(profile, _profile())
    monkeypatch.setattr(project_render_module, "_PACKAGE_SOURCE_ROOT", package)
    return project, catalogue, score, roster, profile, manifest


def _capture(
    paths: tuple[Path, Path, Path, Path, Path, Path],
) -> ScoreV2ProjectInputSnapshot:
    project, catalogue, score, roster, profile, _manifest = paths
    return capture_score_v2_project_inputs(
        score,
        roster,
        profile,
        sample_rate=8_000,
        catalogue_root=catalogue,
        project_root=project,
    )


def _error_code(callable_: object) -> str:
    with pytest.raises(ScoreV2ProjectRenderError) as caught:
        callable_()  # type: ignore[operator]
    assert str(caught.value) == caught.value.code
    return caught.value.code


def test_capture_and_fixed_compilation_bind_every_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_files(tmp_path, monkeypatch)
    _install_runtime_fakes(monkeypatch)

    inputs = _capture(paths)
    compilation = compile_score_v2_project_render(inputs)

    assert type(inputs) is ScoreV2ProjectInputSnapshot
    assert inputs.to_dict()["contract"] == SCORE_V2_PROJECT_INPUT_CONTRACT
    assert inputs.canonical_bytes == canonical_json_bytes(inputs.to_dict())
    assert inputs.artifact_sha256 == hashlib.sha256(inputs.canonical_bytes).hexdigest()
    score_copy = inputs.score_document_copy()
    roster_copy = inputs.roster_document_copy()
    profile_copy = inputs.execution_profile_document_copy()
    score_copy["title"] = "detached"
    roster_copy["assignments"] = []
    profile_copy["sample_time_policy"] = "detached"
    assert inputs.score_document_copy()["title"] == "project render fixture"
    assert len(inputs.roster_document_copy()["assignments"]) == 1
    assert inputs.execution_profile_document_copy()["sample_time_policy"] == "exact"
    assert type(compilation) is ScoreV2ProjectRenderCompilation
    assert compilation.to_dict()["contract"] == (
        SCORE_V2_PROJECT_RENDER_COMPILATION_CONTRACT
    )
    assert compilation.scope == SCORE_V2_PROJECT_RENDER_SCOPE
    assert compilation.executor_id == "lead"
    assert compilation.part_id == "lead"
    assert compilation.performance_bundle.executor_count == 1
    assert compilation.to_dict()["bindings"] == {
        "project_inputs_sha256": inputs.artifact_sha256,
        "score_v2_plan_sha256": compilation.score_plan.artifact_sha256,
        "capability_source_sha256": compilation.capability_sources.artifact_sha256,
        "capability_plan_sha256": compilation.capability_plan.artifact_sha256,
        "runtime_source_sha256": compilation.runtime_sources.artifact_sha256,
        "performance_bundle_sha256": compilation.performance_bundle.artifact_sha256,
    }
    assert compilation.to_dict()["render_authority"] is False
    assert compilation.to_dict()["publish_authority"] is False
    compilation.revalidate_inputs()


def test_files_convenience_api_runs_the_same_closed_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, catalogue, score, roster, profile, _manifest = _project_files(
        tmp_path, monkeypatch
    )
    _install_runtime_fakes(monkeypatch)

    result = compile_score_v2_project_render_files(
        score,
        roster,
        profile,
        sample_rate=8_000,
        catalogue_root=catalogue,
        project_root=project,
    )

    assert result.performance_bundle.executor_count == 1
    assert result.sample_rate == 8_000


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        (
            lambda value: {
                "kind": "tianlai.score_v2_migration",
                "schema_version": 1,
                "score_v2": value,
            },
            "project_render_v2.migration_bundle_not_supported",
        ),
        (
            lambda value: {**value, "schema_version": 1},
            "project_render_v2.direct_score_v2_required",
        ),
        (
            lambda value: {**value, "tail_seconds": 1},
            "project_render_v2.tail_not_supported",
        ),
        (
            lambda value: {**value, "performance_facts": {}},
            "project_render_v2.performance_facts_not_supported",
        ),
        (
            lambda value: {**value, "render_settings": {}},
            "project_render_v2.render_settings_not_supported",
        ),
    ],
)
def test_score_scope_is_direct_v2_only_with_stable_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
    expected: str,
) -> None:
    paths = _project_files(tmp_path, monkeypatch)
    score_path = paths[2]
    _write_json(score_path, mutation(_score()))  # type: ignore[operator]

    assert _error_code(lambda: _capture(paths)) == expected


def test_capture_rejects_a_catalogue_outside_the_project_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_files(tmp_path, monkeypatch)
    project, _catalogue, score, roster, profile, _manifest = paths
    outside_catalogue = tmp_path / "outside-catalogue"
    outside_catalogue.mkdir()

    assert _error_code(
        lambda: capture_score_v2_project_inputs(
            score,
            roster,
            profile,
            sample_rate=8_000,
            catalogue_root=outside_catalogue,
            project_root=project,
        )
    ) == "project_render_v2.runtime_layout_unsupported"


def test_input_generation_replacement_is_rejected_during_compilation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_files(tmp_path, monkeypatch)
    _project, _catalogue, _score_path, roster_path, _profile, _manifest = paths
    inputs = _capture(paths)
    original = project_render_module.compile_score_v2_plan

    def compile_then_replace(*args: object, **kwargs: object):
        plan = original(*args, **kwargs)
        replacement = roster_path.with_suffix(".replacement")
        _write_json(
            replacement,
            {
                "name": "replacement roster",
                "assignments": [
                    {
                        "part": "lead",
                        "instrument": "oscillator",
                        "articulation_auto": False,
                    }
                ],
            },
        )
        os.replace(replacement, roster_path)
        return plan

    monkeypatch.setattr(project_render_module, "compile_score_v2_plan", compile_then_replace)

    assert _error_code(
        lambda: compile_score_v2_project_render(inputs)
    ) == "project_render_v2.input_generation_changed"


def test_compilation_has_a_final_generation_checkpoint_before_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_files(tmp_path, monkeypatch)
    _project, _catalogue, score_path, _roster, _profile, _manifest = paths
    _install_runtime_fakes(monkeypatch)
    inputs = _capture(paths)
    original = project_render_module._register_compilation

    def register_then_replace(result: object, generation: object) -> None:
        original(result, generation)  # type: ignore[arg-type]
        replacement = score_path.with_suffix(".replacement")
        _write_json(replacement, _score())
        os.replace(replacement, score_path)

    monkeypatch.setattr(
        project_render_module,
        "_register_compilation",
        register_then_replace,
    )

    assert _error_code(
        lambda: compile_score_v2_project_render(inputs)
    ) == "project_render_v2.input_generation_changed"


def test_compilation_scope_rejects_non_oscillator_and_external_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_paths = _project_files(
        tmp_path / "backend", monkeypatch, manifest_extra={"type": "sample"}
    )
    backend_inputs = _capture(backend_paths)
    assert _error_code(
        lambda: compile_score_v2_project_render(backend_inputs)
    ) == "project_render_v2.backend_scope_unsupported"

    asset_paths = _project_files(
        tmp_path / "asset",
        monkeypatch,
        manifest_extra={"external_audio_assets": ["tone.wav"]},
    )
    asset_inputs = _capture(asset_paths)
    assert _error_code(
        lambda: compile_score_v2_project_render(asset_inputs)
    ) == "project_render_v2.external_assets_not_supported"


def test_external_registry_rejects_consistent_in_object_reseal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_files(tmp_path, monkeypatch)
    _install_runtime_fakes(monkeypatch)
    inputs = _capture(paths)
    compilation = compile_score_v2_project_render(inputs)

    forged = canonical_json_bytes({"forged": True})
    object.__setattr__(compilation, "_canonical_bytes", forged)
    object.__setattr__(
        compilation,
        "_artifact_sha256",
        hashlib.sha256(forged).hexdigest(),
    )

    assert _error_code(
        lambda: compilation.to_dict()
    ) == "project_render_v2.compilation_integrity_mismatch"


def test_snapshot_registry_seals_its_internal_resource_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _capture(_project_files(tmp_path, monkeypatch))
    object.__setattr__(inputs.limits, "max_notes", inputs.limits.max_notes + 1)

    assert _error_code(
        lambda: inputs.to_dict()
    ) == "project_render_v2.input_artifact_integrity_mismatch"


def test_runtime_generation_is_revalidated_by_compilation_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _project_files(tmp_path, monkeypatch)
    generation = _install_runtime_fakes(monkeypatch)
    compilation = compile_score_v2_project_render(_capture(paths))
    generation["value"] = "replaced"

    assert _error_code(
        lambda: compilation.revalidate_inputs()
    ) == "project_render_v2.input_generation_changed"
