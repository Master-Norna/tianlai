from __future__ import annotations

import json
from pathlib import Path

import pytest

import tianlai.mcp_server as mcp_server
from tianlai.path_policy import InputPathPolicy, InputPathPolicyError
from tianlai.resource_limits import (
    ProjectLimits,
    precheck_score_document_resource_limits,
)
from tianlai.resource_limits import ResourceLimitError


@pytest.fixture()
def output_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "mcp"
    root.mkdir()
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", root)
    return root


def _serialized(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False)


def test_missing_candidate_failure_never_names_a_local_path(
    output_root: Path,
) -> None:
    result = mcp_server.locate_rendered_candidate("no-such-candidate", 1.0)

    assert result["ok"] is False
    issue = result["issues"][0]
    assert issue["code"] == "candidate.locate_failed"
    assert issue["reason"] == "missing"
    assert str(output_root) not in _serialized(result)


def test_compare_outside_the_output_root_is_reported_without_the_path(
    output_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    result = mcp_server.compare_rendered_candidates(str(outside), str(outside))

    assert result["ok"] is False
    assert result["issues"][0]["reason"] == "outside_output_root"
    assert str(outside) not in _serialized(result)


def test_successful_locate_drops_the_absolute_candidate_directory(
    output_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (output_root / "cand").mkdir()
    monkeypatch.setattr(
        mcp_server,
        "locate_candidate",
        lambda *_args, **_kwargs: {
            "kind": "tianlai.candidate_locate_result",
            "schema_version": 1,
            "ok": True,
            "candidate_id": "cand",
            "candidate_directory": "C:/absolute/private/candidate",
            "at_seconds": 1.0,
        },
    )

    result = mcp_server.locate_rendered_candidate("cand", 1.0)

    assert result["ok"] is True
    assert "candidate_directory" not in result
    assert result["candidate_id"] == "cand"


def test_manifest_file_form_is_accepted_and_forwarded(
    output_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = output_root / "cand"
    candidate.mkdir()
    manifest = candidate / mcp_server.CANDIDATE_MANIFEST_NAME
    manifest.write_text("{}", encoding="utf-8")
    seen: list[Path] = []

    def _locate(path: Path, **_kwargs: object) -> dict[str, object]:
        seen.append(path)
        return {
            "kind": "tianlai.candidate_locate_result",
            "schema_version": 1,
            "ok": True,
            "candidate_id": "cand",
        }

    monkeypatch.setattr(mcp_server, "locate_candidate", _locate)

    result = mcp_server.locate_rendered_candidate("cand/候选.json", 1.0)

    # load_candidate accepts the manifest file as well as the directory; the
    # boundary must not narrow that to directories alone.
    assert result["ok"] is True
    assert seen == [manifest]


def test_unsearchable_candidate_path_stays_a_structured_failure(
    output_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = output_root / "cand"
    candidate.mkdir()
    real_is_dir = Path.is_dir

    def _is_dir(self: Path) -> bool:
        if self == candidate:
            raise PermissionError(13, "Access is denied", str(self))
        return real_is_dir(self)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "is_dir", _is_dir)
        result = mcp_server.locate_rendered_candidate("cand", 1.0)

    # Path.is_dir re-raises EACCES instead of reporting False, so this used to
    # escape the tool as an uncaught PermissionError carrying the absolute path.
    assert result["ok"] is False
    assert result["issues"][0]["reason"] == "unreadable"
    assert str(output_root) not in _serialized(result)


def test_hard_linked_input_reports_the_real_reason(tmp_path: Path) -> None:
    source = tmp_path / "events.json"
    source.write_text("{}", encoding="utf-8")
    linked = tmp_path / "linked.json"
    try:
        linked.hardlink_to(source)
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        pytest.skip("hard links are unavailable on this filesystem")

    policy = InputPathPolicy.from_roots([tmp_path])

    with pytest.raises(InputPathPolicyError) as caught:
        policy.read_file(linked, maximum_bytes=1024)

    assert caught.value.code == "input_path.multiple_links"
    assert "hard link" in caught.value.message


def test_precheck_rejects_note_fan_out_before_parsing() -> None:
    limits = ProjectLimits(max_notes=2, max_parts=4)
    score = {"parts": [{"notes": [{}, {}, {}]}]}

    with pytest.raises(ResourceLimitError) as caught:
        precheck_score_document_resource_limits(score, limits)

    assert caught.value.code == "score.too_many_notes"


def test_precheck_rejects_part_fan_out_before_parsing() -> None:
    limits = ProjectLimits(max_notes=8, max_parts=1)
    score = {"parts": [{"notes": []}, {"notes": []}]}

    with pytest.raises(ResourceLimitError) as caught:
        precheck_score_document_resource_limits(score, limits)

    assert caught.value.code == "score.too_many_parts"


def test_precheck_accepts_a_score_within_the_limits() -> None:
    limits = ProjectLimits(max_notes=4, max_parts=2)
    precheck_score_document_resource_limits(
        {"parts": [{"notes": [{}, {}]}, {"notes": [{}, {}]}]}, limits
    )
