from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from tianlai.cli import main as cli_main
from tianlai.candidate import portable_directory_name, portable_slug
from tianlai.score_v2_candidate import (
    SCORE_V2_RENDER_RECEIPT_NAME,
)


ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "乐器"
OSCILLATOR_ID = "测试工具/参考振荡器"


def _r(numerator: int, denominator: int = 1) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _score() -> dict[str, object]:
    return {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": "CLI Score v2",
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
                    "tempo_id": "tempo-240",
                    "at": {
                        "measure_id": "m1",
                        "offset_quarters": _r(0),
                    },
                    "quarter_bpm": _r(240),
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


def _roster(instrument: str = OSCILLATOR_ID) -> dict[str, object]:
    return {
        "name": "CLI Score-v2 roster",
        "assignments": [
            {
                "part": "lead",
                "instrument": instrument,
                "articulation_auto": False,
            }
        ],
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


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _project_inputs(
    tmp_path: Path,
    *,
    score: dict[str, object] | None = None,
    roster: dict[str, object] | None = None,
    profile: dict[str, object] | None = None,
) -> tuple[Path, Path, Path]:
    source = tmp_path / "inputs"
    score_path = source / "score-v2.json"
    roster_path = source / "roster.json"
    profile_path = source / "execution-profile.json"
    _write_json(score_path, _score() if score is None else score)
    _write_json(roster_path, _roster() if roster is None else roster)
    _write_json(profile_path, _profile() if profile is None else profile)
    return score_path, roster_path, profile_path


def _arguments(
    paths: tuple[Path, Path, Path],
    output_root: Path,
    *,
    catalogue: Path = CATALOGUE,
    output_id: str = "cli-v3",
    extra: tuple[str, ...] = (),
) -> list[str]:
    score, roster, profile = paths
    return [
        "project-render-v2",
        "--score",
        str(score),
        "--roster",
        str(roster),
        "--execution-profile",
        str(profile),
        "--sample-rate",
        "8000",
        "--root",
        str(catalogue),
        "--output-root",
        str(output_root),
        "--output-id",
        output_id,
        *extra,
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _active_transaction_directories(output_root: Path) -> tuple[Path, ...]:
    if not output_root.exists():
        return ()
    return tuple(
        path
        for path in output_root.rglob("*")
        if path.is_dir()
        and path.name.endswith((".staging", ".previous"))
    )


def test_project_render_v2_help_and_sample_rate_are_explicit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as help_exit:
        cli_main(["project-render-v2", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "direct tianlai.score schema-version 2 JSON" in help_text
    assert "--execution-profile" in help_text
    assert "--sample-rate" in help_text
    assert "exactly one oscillator" in help_text

    with pytest.raises(SystemExit) as missing_exit:
        cli_main(
            [
                "project-render-v2",
                "--score",
                "score.json",
                "--roster",
                "roster.json",
                "--execution-profile",
                "profile.json",
            ]
        )
    assert missing_exit.value.code == 2
    assert "--sample-rate" in capsys.readouterr().err


def test_project_render_v2_end_to_end_publishes_candidate_v3_and_verifies(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _project_inputs(tmp_path)
    output_root = tmp_path / "output"

    assert cli_main(_arguments(paths, output_root)) == 0
    result = json.loads(capsys.readouterr().out)
    candidate = Path(result["candidate_directory"])
    assert candidate.is_dir()
    assert result["candidate_version"] == 3
    assert result["scope"] == (
        "single_executor_builtin_oscillator_declared_"
        "no_external_audio_assets_v1"
    )
    assert result["mix_sha256"] == _sha256(candidate / "合奏.wav")
    manifest = json.loads(
        (candidate / "候选.json").read_text(encoding="utf-8")
    )
    assert candidate.parent.name == portable_directory_name("CLI Score v2")
    assert manifest["work_id"] == portable_slug("CLI Score v2")
    assert manifest["work_id"] != candidate.parent.name

    assert cli_main(
        ["candidate-verify", "--candidate", str(candidate)]
    ) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["ok"] is True
    assert verification["integrity_verified"] is True
    assert verification["candidate"]["version"] == 3
    assert verification["candidate"]["pipeline"] == "score_v2"
    assert verification["integrity"][
        "runtime_authority_document_reusable"
    ] is False


def test_score_v2_candidate_v3_verifies_from_legacy_hash_parent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _project_inputs(tmp_path)
    output_root = tmp_path / "output"

    assert cli_main(
        _arguments(paths, output_root, output_id="legacy-v3")
    ) == 0
    clean_candidate = Path(
        json.loads(capsys.readouterr().out)["candidate_directory"]
    )
    manifest = json.loads(
        (clean_candidate / "候选.json").read_text(encoding="utf-8")
    )
    legacy_parent = output_root / manifest["work_id"]

    os.replace(clean_candidate.parent, legacy_parent)
    legacy_candidate = legacy_parent / clean_candidate.name

    assert not clean_candidate.parent.exists()
    assert cli_main(
        ["candidate-verify", "--candidate", str(legacy_candidate)]
    ) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["ok"] is True
    assert verification["integrity_verified"] is True
    assert verification["candidate"]["version"] == 3
    assert verification["candidate"]["work_id"] == manifest["work_id"]


@pytest.mark.parametrize(
    ("mutation", "diagnostic"),
    [
        (
            lambda score: {**score, "schema_version": 1},
            "project_render_v2.direct_score_v2_required",
        ),
        (
            lambda score: {
                "kind": "tianlai.score_v2_migration",
                "schema_version": 1,
                "score_v2": score,
            },
            "project_render_v2.migration_bundle_not_supported",
        ),
        (
            lambda score: {**score, "tail_seconds": 1},
            "project_render_v2.tail_not_supported",
        ),
        (
            lambda score: {**score, "performance_facts": {}},
            "project_render_v2.performance_facts_not_supported",
        ),
    ],
)
def test_project_render_v2_rejects_non_direct_scope_before_any_output(
    mutation: object,
    diagnostic: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    score = mutation(_score())  # type: ignore[operator]
    paths = _project_inputs(tmp_path, score=score)
    output_root = tmp_path / "must-not-exist"

    assert cli_main(_arguments(paths, output_root)) == 2
    assert diagnostic in capsys.readouterr().err
    assert not output_root.exists()


def test_project_render_v2_rejects_multiple_executors_before_any_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    roster = _roster()
    roster["assignments"] = [
        *roster["assignments"],  # type: ignore[misc]
        {
            "part": "second",
            "instrument": OSCILLATOR_ID,
            "articulation_auto": False,
        },
    ]
    paths = _project_inputs(tmp_path, roster=roster)
    output_root = tmp_path / "must-not-exist"

    assert cli_main(_arguments(paths, output_root)) == 2
    assert "project_render_v2.single_executor_required" in (
        capsys.readouterr().err
    )
    assert not output_root.exists()


def test_project_render_v2_rejects_oscillator_external_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import tianlai.score_v2_project_render as project_render

    project = tmp_path / "asset-project"
    package = project / "tianlai"
    catalogue = project / "catalogue"
    manifest = catalogue / "asset-oscillator" / "乐器.json"
    package.mkdir(parents=True)
    _write_json(
        manifest,
        {
            "name": "asset oscillator",
            "type": "oscillator",
            "note_min": 0,
            "note_max": 127,
            "runtime_asset_policy": "no_external_audio_assets",
            "external_audio_assets": ["tone.wav"],
        },
    )
    monkeypatch.setattr(project_render, "_PACKAGE_SOURCE_ROOT", package)
    paths = _project_inputs(
        tmp_path / "asset-inputs",
        roster=_roster("asset-oscillator"),
    )
    output_root = tmp_path / "must-not-exist"

    assert cli_main(
        _arguments(paths, output_root, catalogue=catalogue)
    ) == 2
    assert "project_render_v2.external_assets_not_supported" in (
        capsys.readouterr().err
    )
    assert not output_root.exists()


def test_expected_receipt_argument_requires_overwrite_before_compilation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _project_inputs(tmp_path)
    output_root = tmp_path / "must-not-exist"

    assert cli_main(
        _arguments(
            paths,
            output_root,
            extra=("--expected-receipt-sha256", "0" * 64),
        )
    ) == 2
    assert "valid only with --overwrite" in capsys.readouterr().err
    assert not output_root.exists()


def test_existing_candidate_overwrite_requires_and_checks_old_receipt(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _project_inputs(tmp_path)
    output_root = tmp_path / "output"
    arguments = _arguments(paths, output_root, output_id="replace-me")
    assert cli_main(arguments) == 0
    candidate = Path(json.loads(capsys.readouterr().out)["candidate_directory"])
    receipt = candidate / SCORE_V2_RENDER_RECEIPT_NAME
    expected_receipt = _sha256(receipt)
    original_manifest = (candidate / "候选.json").read_bytes()

    assert cli_main([*arguments, "--overwrite"]) == 2
    assert "expected_receipt_sha256" in capsys.readouterr().err
    assert (candidate / "候选.json").read_bytes() == original_manifest

    assert cli_main(
        [
            *arguments,
            "--overwrite",
            "--expected-receipt-sha256",
            "0" * 64,
        ]
    ) == 2
    assert "Hash" in capsys.readouterr().err
    assert (candidate / "候选.json").read_bytes() == original_manifest

    assert cli_main(
        [
            *arguments,
            "--overwrite",
            "--expected-receipt-sha256",
            expected_receipt,
        ]
    ) == 0
    replaced = Path(json.loads(capsys.readouterr().out)["candidate_directory"])
    assert replaced == candidate
    assert cli_main(
        ["candidate-verify", "--candidate", str(replaced)]
    ) == 0
    assert json.loads(capsys.readouterr().out)["integrity_verified"] is True
    assert _active_transaction_directories(output_root) == ()


def test_input_generation_replacement_leaves_no_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import tianlai.score_v2_project_render as project_render

    paths = _project_inputs(tmp_path)
    score_path = paths[0]
    output_root = tmp_path / "must-not-exist"
    original_compile = project_render.compile_score_v2_plan

    def compile_then_replace(*args: object, **kwargs: object):
        plan = original_compile(*args, **kwargs)
        replacement = score_path.with_suffix(".replacement")
        replacement_score = copy.deepcopy(_score())
        replacement_score["title"] = "replacement generation"
        _write_json(replacement, replacement_score)
        os.replace(replacement, score_path)
        return plan

    monkeypatch.setattr(
        project_render,
        "compile_score_v2_plan",
        compile_then_replace,
    )

    assert cli_main(_arguments(paths, output_root)) == 2
    assert "project_render_v2.input_generation_changed" in (
        capsys.readouterr().err
    )
    assert not output_root.exists()


def test_formal_publication_failure_removes_private_stage_and_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import tianlai.candidate_integrity as candidate_integrity

    paths = _project_inputs(tmp_path)
    output_root = tmp_path / "output"

    def reject_formal_candidate(*args: object, **kwargs: object) -> object:
        raise ValueError("injected Candidate-v3 formal publication failure")

    monkeypatch.setattr(
        candidate_integrity,
        "verify_candidate_integrity",
        reject_formal_candidate,
    )

    assert cli_main(_arguments(paths, output_root)) == 2
    assert "injected Candidate-v3 formal publication failure" in (
        capsys.readouterr().err
    )
    final_candidate = (
        output_root
        / portable_directory_name("CLI Score v2")
        / portable_slug("cli-v3", maximum_length=96)
    )
    assert not final_candidate.exists()
    assert _active_transaction_directories(output_root) == ()
