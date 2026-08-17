from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
from unittest import mock

import pytest
from jsonschema import Draft202012Validator, FormatChecker

import tianlai.candidate as candidate_module
from tianlai.candidate import (
    CANDIDATE_MANIFEST_NAME,
    canonical_json_sha256,
    load_candidate,
    sha256_file,
)
from tianlai.candidate_integrity import (
    CandidateIntegrityError,
    verify_candidate_integrity,
)
from tianlai.cli import main as cli_main


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "乐器"


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _score() -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "realization entrypoint contract",
        "sample_rate": 8_000,
        "tail_seconds": 0.05,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 240,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "lead",
                "notes": [
                    {
                        "event_id": "note-1",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 0.25,
                        "pitch": "C4",
                        "velocity": 0.5,
                    }
                ],
            }
        ],
    }


def _roster() -> dict[str, object]:
    return {
        "name": "realization entrypoint roster",
        "assignments": [
            {
                "part": "lead",
                "instrument": "测试工具/参考振荡器",
            }
        ],
    }


def _profile() -> dict[str, object]:
    return {
        "kind": "tianlai.render_profile",
        "schema_version": 1,
        "name": "realization-entrypoint-dry",
        "expression": "strict",
        "range_mode": "compatibility",
        "seed": 0,
        "master_gain_db": 0.0,
        "normalize_peak_db": None,
        "space": {"enabled": False},
        "collaboration_mode": None,
        "write_stems": False,
        "use_stem_cache": False,
        "refresh_stem_cache": False,
    }


def _realization(
    score: dict[str, object],
    *,
    active: bool,
) -> dict[str, object]:
    note_overrides: list[dict[str, object]] = []
    if active:
        note_overrides.append(
            {
                "event_id": "note-1",
                "timing_offset_ms": {
                    "strategy": "replace",
                    "value": 1.0,
                    "value_policy": "exact",
                    "semantic_policy": "exact",
                },
            }
        )
    return {
        "kind": "tianlai.realization",
        "schema_version": 1,
        "score_sha256": canonical_json_sha256(score),
        "defaults_profile": "tianlai.realization-defaults-v1",
        "mode": "captured",
        "note_overrides": note_overrides,
        "control_lanes": [],
    }


def _write_project_inputs(
    base: Path,
    *,
    active: bool,
) -> tuple[Path, Path, Path, Path, dict[str, object]]:
    score = _score()
    realization = _realization(score, active=active)
    score_path = base / "score.json"
    roster_path = base / "roster.json"
    profile_path = base / "profile.json"
    realization_path = base / "realization.json"
    _write_json(score_path, score)
    _write_json(roster_path, _roster())
    _write_json(profile_path, _profile())
    _write_json(realization_path, realization)
    return (
        score_path,
        roster_path,
        profile_path,
        realization_path,
        realization,
    )


def _run_cli(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        status = cli_main(arguments)
    return status, stdout.getvalue(), stderr.getvalue()


def _project_render(
    base: Path,
    *,
    active: bool,
    output_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    (
        score_path,
        roster_path,
        profile_path,
        realization_path,
        realization,
    ) = _write_project_inputs(base, active=active)
    status, stdout, stderr = _run_cli(
        [
            "project-render",
            "--score",
            str(score_path),
            "--roster",
            str(roster_path),
            "--render-profile",
            str(profile_path),
            "--realization",
            str(realization_path),
            "--output-root",
            str(base / "candidates"),
            "--output-id",
            output_id,
            "--root",
            str(CATALOG),
        ]
    )
    assert status == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    return json.loads(stdout), realization


def _legacy_max_path_render_base(tmp_path: Path) -> Path:
    """Keep the public candidate ordinary while nesting private paths >260."""

    minimum_base_length = 128
    prefix = "deep-"
    padding = max(
        8,
        minimum_base_length
        - len(str(tmp_path.resolve()))
        - 1
        - len(prefix),
    )
    base = tmp_path / f"{prefix}{'x' * padding}"
    base.mkdir()
    assert len(str(base.resolve())) >= minimum_base_length
    return base


def _candidate_documents(
    directory: Path,
) -> tuple[Path, dict[str, object], Path, dict[str, object], Path, dict[str, object]]:
    manifest_path = directory / CANDIDATE_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt_path = directory / manifest["render_receipt"]["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    plan_path = directory / receipt["performance_plan"]["path"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    return manifest_path, manifest, receipt_path, receipt, plan_path, plan


def _snapshot(directory: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def _restore(directory: Path, snapshot: dict[Path, bytes]) -> None:
    for relative, payload in snapshot.items():
        (directory / relative).write_bytes(payload)


def test_ensemble_plan_only_preserves_and_binds_realization(
    tmp_path: Path,
) -> None:
    (
        score_path,
        roster_path,
        profile_path,
        realization_path,
        realization,
    ) = _write_project_inputs(tmp_path, active=True)
    output = tmp_path / "plan-only"

    status, stdout, stderr = _run_cli(
        [
            "ensemble",
            "--score",
            str(score_path),
            "--roster",
            str(roster_path),
            "--render-profile",
            str(profile_path),
            "--realization",
            str(realization_path),
            "--output",
            str(output),
            "--root",
            str(CATALOG),
            "--plan-only",
        ]
    )

    assert status == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    assert json.loads(
        (output / "演奏实现.json").read_text(encoding="utf-8")
    ) == realization
    plan = json.loads(
        (output / "演奏计划.json").read_text(encoding="utf-8")
    )
    assert plan["realization"] == {
        "kind": realization["kind"],
        "schema_version": realization["schema_version"],
        "score_sha256": realization["score_sha256"],
        "canonical_sha256": canonical_json_sha256(realization),
        "defaults_profile": realization["defaults_profile"],
        "mode": realization["mode"],
    }
    review = json.loads(
        (output / "创作自检.json").read_text(encoding="utf-8")
    )
    assert review["binding"]["realization_sha256"] == canonical_json_sha256(
        realization
    )

    published = _snapshot(output)
    status, _stdout, stderr = _run_cli(
        [
            "ensemble",
            "--score",
            str(score_path),
            "--roster",
            str(roster_path),
            "--render-profile",
            str(profile_path),
            "--output",
            str(output),
            "--root",
            str(CATALOG),
            "--plan-only",
        ]
    )
    assert status == 2
    assert "stale optional document" in stderr
    assert _snapshot(output) == published


@pytest.mark.parametrize(
    ("payload", "error_fragment"),
    [
        ('{"kind":"first","kind":"second"}', "duplicate_object_member"),
        ('{"kind":NaN}', "non_finite_number"),
    ],
)
@pytest.mark.parametrize("command", ["ensemble", "project-render"])
def test_realization_cli_entrypoints_reject_non_strict_json_before_publication(
    tmp_path: Path,
    payload: str,
    error_fragment: str,
    command: str,
) -> None:
    score_path, roster_path, profile_path, realization_path, _ = (
        _write_project_inputs(tmp_path, active=True)
    )
    realization_path.write_text(payload, encoding="utf-8")
    output = tmp_path / "rejected"
    common = [
        command,
        "--score",
        str(score_path),
        "--roster",
        str(roster_path),
        "--render-profile",
        str(profile_path),
        "--realization",
        str(realization_path),
        "--root",
        str(CATALOG),
    ]
    if command == "ensemble":
        common.extend(
            [
                "--output",
                str(output),
                "--plan-only",
            ]
        )
    else:
        common.extend(
            [
                "--output-root",
                str(output),
                "--output-id",
                "strict-json-rejected",
            ]
        )

    status, _stdout, stderr = _run_cli(common)

    assert status == 2
    assert error_fragment in stderr
    assert not output.exists()


def test_project_render_uses_candidate_limit_while_ensemble_uses_realization_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score_path, roster_path, profile_path, realization_path, _realization = (
        _write_project_inputs(tmp_path, active=True)
    )
    monkeypatch.setattr(
        candidate_module,
        "MAX_CANDIDATE_JSON_BYTES",
        256,
    )
    assert realization_path.stat().st_size > 256

    ensemble_output = tmp_path / "ensemble-large"
    ensemble_status, _ensemble_stdout, ensemble_stderr = _run_cli(
        [
            "ensemble",
            "--score",
            str(score_path),
            "--roster",
            str(roster_path),
            "--render-profile",
            str(profile_path),
            "--realization",
            str(realization_path),
            "--output",
            str(ensemble_output),
            "--root",
            str(CATALOG),
            "--plan-only",
        ]
    )
    assert ensemble_status == 0, ensemble_stderr
    assert (ensemble_output / "演奏实现.json").is_file()

    output_root = tmp_path / "candidates"

    status, _stdout, stderr = _run_cli(
        [
            "project-render",
            "--score",
            str(score_path),
            "--roster",
            str(roster_path),
            "--render-profile",
            str(profile_path),
            "--realization",
            str(realization_path),
            "--output-root",
            str(output_root),
            "--output-id",
            "oversized",
            "--root",
            str(CATALOG),
        ]
    )

    assert status == 2
    assert "candidate realization" in stderr
    assert "no larger than 256 bytes" in stderr
    assert not output_root.exists()


def test_project_render_rejects_candidate_oversized_plan_before_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score_path, roster_path, profile_path, realization_path, realization = (
        _write_project_inputs(tmp_path, active=True)
    )
    monkeypatch.setenv("TIANLAI_MAX_PLAN_MIB", "512")
    monkeypatch.setattr(
        candidate_module,
        "MAX_CANDIDATE_JSON_BYTES",
        1_024,
    )
    assert candidate_module.validate_candidate_json_size(
        realization,
        label="candidate realization",
    ) < 1_024
    output_root = tmp_path / "candidates"

    status, _stdout, stderr = _run_cli(
        [
            "project-render",
            "--score",
            str(score_path),
            "--roster",
            str(roster_path),
            "--render-profile",
            str(profile_path),
            "--realization",
            str(realization_path),
            "--output-root",
            str(output_root),
            "--output-id",
            "oversized-plan",
            "--root",
            str(CATALOG),
        ]
    )

    assert status == 2
    assert "candidate performance plan published JSON size" in stderr
    assert "candidate limit 1024 bytes" in stderr
    assert not output_root.exists()


def test_project_render_candidate_binds_active_realization_and_fails_closed(
    tmp_path: Path,
) -> None:
    base = _legacy_max_path_render_base(tmp_path)
    result, realization = _project_render(
        base,
        active=True,
        output_id="active-realization",
    )
    for field in (
        "candidate_directory",
        "candidate_manifest",
        "mix_wav",
        "render_receipt",
        "post_render_check",
    ):
        assert not str(result[field]).startswith("\\\\?\\")
    directory = Path(str(result["candidate_directory"]))
    loaded_directory, manifest = load_candidate(directory, verify=True)

    assert loaded_directory == directory.resolve()
    candidate_schema = json.loads(
        (ROOT / "schemas" / "candidate.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(
        candidate_schema,
        format_checker=FormatChecker(),
    ).validate(manifest)
    assert result["realization_sha256"] == canonical_json_sha256(realization)
    realization_binding = manifest["project"]["realization"]
    assert realization_binding["canonical_sha256"] == canonical_json_sha256(
        realization
    )
    bound_realization = directory / realization_binding["path"]
    assert json.loads(bound_realization.read_text(encoding="utf-8")) == realization
    _, _, _, _, _, plan = _candidate_documents(directory)
    assert plan["realization"]["canonical_sha256"] == canonical_json_sha256(
        realization
    )
    integrity = verify_candidate_integrity(directory)
    assert integrity["integrity_verified"] is True
    assert integrity["integrity"]["optional_artifacts"]["realization"] is True

    original = _snapshot(directory)
    tampered = copy.deepcopy(realization)
    tampered["mode"] = "interpreted"
    _write_json(bound_realization, tampered)
    with pytest.raises(ValueError, match="realization file hash mismatch"):
        load_candidate(directory, verify=True)
    _restore(directory, original)

    manifest_path, manifest, _, _, _, _ = _candidate_documents(directory)
    del manifest["project"]["realization"]
    _write_json(manifest_path, manifest)
    with pytest.raises(
        ValueError,
        match="realization exists without a project binding",
    ):
        load_candidate(directory, verify=True)
    with pytest.raises(CandidateIntegrityError) as unbound:
        verify_candidate_integrity(directory)
    assert unbound.value.code == "closed_world_violation"
    _restore(directory, original)

    (
        manifest_path,
        manifest,
        receipt_path,
        receipt,
        plan_path,
        plan,
    ) = _candidate_documents(directory)
    plan["realization"]["mode"] = "interpreted"
    _write_json(plan_path, plan)
    plan_sha256 = canonical_json_sha256(plan)
    receipt["performance_plan"].update(
        {
            "file_sha256": sha256_file(plan_path),
            "sha256": plan_sha256,
        }
    )
    _write_json(receipt_path, receipt)
    manifest["project"]["performance_plan_sha256"] = plan_sha256
    manifest["render_receipt"]["sha256"] = sha256_file(receipt_path)
    _write_json(manifest_path, manifest)
    with mock.patch.object(
        candidate_module,
        "verify_render_generation",
        return_value=None,
    ):
        with pytest.raises(ValueError, match="disagrees with realization.json"):
            load_candidate(directory, verify=True)
    with pytest.raises(CandidateIntegrityError) as disagreement:
        verify_candidate_integrity(directory)
    assert disagreement.value.code == "identity_mismatch"


def test_project_render_candidate_preserves_noop_realization_without_plan_delta(
    tmp_path: Path,
) -> None:
    result, realization = _project_render(
        tmp_path,
        active=False,
        output_id="noop-realization",
    )
    directory = Path(str(result["candidate_directory"]))
    _, manifest = load_candidate(directory, verify=True)
    binding = manifest["project"]["realization"]

    assert result["realization_sha256"] == canonical_json_sha256(realization)
    assert json.loads(
        (directory / binding["path"]).read_text(encoding="utf-8")
    ) == realization
    _, _, _, _, _, plan = _candidate_documents(directory)
    assert "realization" not in plan
    integrity = verify_candidate_integrity(directory)
    assert integrity["integrity"]["optional_artifacts"]["realization"] is True
