from __future__ import annotations

import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sys

import pytest

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional mcp package is not installed",
)


MUSIC_BOX = "\u952e\u76d8\u4e50\u5668/\u97f3\u4e50\u76d2"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def authoring_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tianlai import mcp_server

    output = tmp_path / "output" / "mcp"
    output.parent.mkdir(parents=True)
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", output)
    return mcp_server, output


def _create(mcp_server, key: str = "test-project") -> dict:
    result = mcp_server.create_authoring_project(key, "Test project")
    assert result["ok"], result
    return result


def _snapshot(mcp_server, key: str = "test-project") -> dict:
    result = mcp_server.get_authoring_snapshot(key)
    assert result["ok"], result
    return result["snapshot"]


def _renderable_documents(snapshot: dict) -> dict:
    documents = copy.deepcopy(snapshot["documents"])
    score = documents["score"]
    score["tail_seconds"] = 0.05
    score["tempo_map"][0]["bpm"] = 600.0
    score["parts"][0]["notes"] = [
        {
            "event_id": "event-1",
            "bar": 1,
            "beat": 1.0,
            "duration_beats": 0.1,
            "pitch": 84,
            "dynamic": "mf",
        }
    ]
    documents["authoring_roster"] = {
        "kind": "tianlai.authoring_roster",
        "schema_version": 1,
        "name": "MCP authoring test",
        "assignments": [
            {"part": "part-1", "instrument": MUSIC_BOX},
        ],
    }
    profile = documents["render_profile"]
    profile["name"] = "test-dry"
    profile["expression"] = "strict"
    profile["normalize_peak_db"] = None
    profile["space"] = {"enabled": False}
    profile["write_stems"] = False
    profile["use_stem_cache"] = False
    return documents


def _assert_no_local_path(value: object, path: Path) -> None:
    encoded = json.dumps(value, ensure_ascii=False)
    assert str(path) not in encoded
    assert str(path.resolve()) not in encoded


def test_create_open_snapshot_cas_and_historical_revision_are_path_free(
    authoring_mcp,
) -> None:
    mcp_server, output = authoring_mcp
    created = _create(mcp_server)
    initial_revision = created["project"]["revision"]
    snapshot = _snapshot(mcp_server)
    initial_documents = copy.deepcopy(snapshot["documents"])
    initial_documents["score"]["title"] = "Edited score"

    saved = mcp_server.save_authoring_project(
        "test-project",
        initial_revision,
        initial_documents,
    )
    assert saved["ok"], saved
    assert saved["project"]["revision"] != initial_revision

    stale = mcp_server.save_authoring_project(
        "test-project",
        initial_revision,
        initial_documents,
    )
    assert stale["ok"] is False
    assert stale["error"]["code"] == "authoring_project.revision_conflict"
    assert stale["error"]["retryable"] is True

    historical = mcp_server.get_authoring_snapshot(
        "test-project",
        initial_revision,
    )
    assert historical["ok"], historical
    assert historical["snapshot"]["project"]["revision"] == initial_revision
    assert historical["snapshot"]["documents"]["score"]["title"] == (
        "Test project"
    )
    current = mcp_server.open_authoring_project("test-project")
    assert current["project"]["revision"] == saved["project"]["revision"]

    for result in (created, saved, stale, historical, current):
        _assert_no_local_path(result, output.parent.parent)


def test_concurrent_cas_allows_exactly_one_revision_advance(authoring_mcp) -> None:
    mcp_server, _output = authoring_mcp
    created = _create(mcp_server)
    revision = created["project"]["revision"]
    documents = _snapshot(mcp_server)["documents"]
    first = copy.deepcopy(documents)
    second = copy.deepcopy(documents)
    first["score"]["title"] = "First writer"
    second["score"]["title"] = "Second writer"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda value: mcp_server.save_authoring_project(
                    "test-project",
                    revision,
                    value,
                ),
                (first, second),
            )
        )

    assert sum(result["ok"] is True for result in results) == 1
    failure = next(result for result in results if result["ok"] is False)
    assert failure["error"]["code"] == "authoring_project.revision_conflict"
    winning_revision = next(
        result["project"]["revision"] for result in results if result["ok"]
    )
    assert mcp_server.open_authoring_project("test-project")["project"][
        "revision"
    ] == winning_revision


@pytest.mark.parametrize(
    "project_key",
    (
        "../escape",
        "a/b",
        "a\\b",
        "/absolute",
        "C:\\absolute",
        "UPPER",
        "con",
    ),
)
def test_project_key_cannot_escape_dedicated_namespace(
    authoring_mcp,
    project_key: str,
) -> None:
    mcp_server, output = authoring_mcp
    result = mcp_server.create_authoring_project(project_key, "Unsafe")
    assert result["ok"] is False
    assert result["error"]["code"] == "authoring_path.invalid_project_key"
    assert not (output.parent.parent / "escape").exists()
    assert "project_key" not in result


def test_linked_authoring_namespace_is_rejected(
    authoring_mcp,
    tmp_path: Path,
) -> None:
    mcp_server, output = authoring_mcp
    output.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    namespace = output / "authoring-projects"
    try:
        namespace.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory links are unavailable on this host")

    result = mcp_server.create_authoring_project("safe-key", "Unsafe root")
    assert result["ok"] is False
    assert result["error"]["code"] == "authoring_path.namespace_unsafe"
    assert list(outside.iterdir()) == []


def test_candidate_selectors_cannot_be_used_as_paths(authoring_mcp) -> None:
    mcp_server, output = authoring_mcp
    _create(mcp_server)
    result = mcp_server.inspect_authoring_candidate(
        "test-project",
        "../outside",
        "candidate-id",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "authoring_candidate.invalid_work_id"
    _assert_no_local_path(result, output.parent.parent)


def test_readiness_keeps_advisory_review_nonblocking(
    authoring_mcp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_server, _output = authoring_mcp
    _create(mcp_server)
    advisory = {
        "status": "review",
        "render_allowed": True,
        "summary": {},
        "issues": [
            {
                "code": "project_review.flat_energy",
                "severity": "warning",
                "decision": "review",
                "source": "project_review",
                "location": {"segments": []},
            }
        ],
        "issues_truncated": False,
    }
    monkeypatch.setattr(
        mcp_server,
        "validate_authoring_project_readiness",
        lambda *_args, **_kwargs: advisory,
    )

    result = mcp_server.check_authoring_readiness("test-project")
    assert result["ok"] is True
    assert result["readiness"] == advisory
    assert result["readiness"]["render_allowed"] is True
    assert result["readiness"]["issues"][0]["decision"] == "review"


def test_render_tool_passes_the_requested_immutable_revision(
    authoring_mcp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_server, output = authoring_mcp
    created = _create(mcp_server)
    old_revision = created["project"]["revision"]
    documents = _snapshot(mcp_server)["documents"]
    documents["score"]["title"] = "New current revision"
    saved = mcp_server.save_authoring_project(
        "test-project",
        old_revision,
        documents,
    )
    assert saved["ok"] is True
    observed: dict[str, object] = {}

    def fake_render(root, *, expected_revision, **_kwargs):
        observed["root"] = Path(root)
        observed["revision"] = expected_revision
        return {
            "status": "completed",
            "project_id": created["project"]["project_id"],
            "revision": expected_revision,
            "workflow_managed": False,
            "reused_existing": False,
            "candidate": {
                "work_id": "work-id",
                "candidate_id": "candidate-id",
            },
        }

    monkeypatch.setattr(
        mcp_server,
        "render_authoring_project_candidate",
        fake_render,
    )
    result = mcp_server.render_authoring_revision(
        "test-project",
        old_revision,
    )
    assert result["ok"] is True
    assert observed["revision"] == old_revision
    assert observed["root"] == (
        output / "authoring-projects" / "test-project"
    ).resolve()
    assert result["render"]["revision"] == old_revision
    _assert_no_local_path(result, output.parent.parent)


def test_render_inspect_locate_and_compare_use_only_candidate_ids(
    authoring_mcp,
) -> None:
    mcp_server, output = authoring_mcp
    created = _create(mcp_server)
    snapshot = _snapshot(mcp_server)
    saved = mcp_server.save_authoring_project(
        "test-project",
        created["project"]["revision"],
        _renderable_documents(snapshot),
    )
    assert saved["ok"], saved
    revision = saved["project"]["revision"]

    first = mcp_server.render_authoring_revision("test-project", revision)
    second = mcp_server.render_authoring_revision("test-project", revision)
    assert first["ok"], first
    assert second["ok"], second
    assert first["render"]["workflow_managed"] is False
    assert first["render"]["reused_existing"] is False
    first_id = first["render"]["candidate"]
    second_id = second["render"]["candidate"]

    advanced_documents = copy.deepcopy(
        mcp_server.get_authoring_snapshot("test-project")["snapshot"][
            "documents"
        ]
    )
    advanced_documents["score"]["title"] = "Current pointer advanced"
    advanced = mcp_server.save_authoring_project(
        "test-project",
        revision,
        advanced_documents,
    )
    assert advanced["ok"], advanced
    assert advanced["project"]["revision"] != revision

    inspected = mcp_server.inspect_authoring_candidate(
        "test-project",
        first_id["work_id"],
        first_id["candidate_id"],
    )
    assert inspected["ok"], inspected
    assert inspected["project"]["revision"] == revision
    assert inspected["candidate"]["authoring_project"]["revision"] == revision
    assert inspected["candidate"]["workflow_managed"] is False
    assert inspected["candidate"]["workflow_authorized"] is False
    assert inspected["candidate"]["workflow_recorded"] is False
    assert inspected["candidate"]["workflow_accepted"] is False
    assert inspected["candidate"]["authoring_workflow"] is None
    assert inspected["render_evidence"]["post_render_check"]["summary"][
        "can_proceed"
    ] is True

    located = mcp_server.locate_authoring_candidate(
        "test-project",
        first_id["work_id"],
        first_id["candidate_id"],
        0.0,
    )
    assert located["ok"], located
    assert located["location"]["candidate_id"] == first_id["candidate_id"]
    assert "candidate_directory" not in located["location"]

    compared = mcp_server.compare_authoring_candidates(
        "test-project",
        first_id["work_id"],
        first_id["candidate_id"],
        second_id["work_id"],
        second_id["candidate_id"],
    )
    assert compared["ok"], compared
    assert compared["comparison"]["score"]["changed"] is False

    for result in (
        first,
        second,
        inspected,
        located,
        compared,
    ):
        _assert_no_local_path(result, output.parent.parent)


def test_real_stdio_authoring_lifecycle_has_structured_cas_error(
    tmp_path: Path,
) -> None:
    asyncio.run(_run_stdio_authoring_lifecycle(tmp_path))


async def _run_stdio_authoring_lifecycle(tmp_path: Path) -> None:
    from mcp import Client, StdioServerParameters
    from mcp.client.stdio import stdio_client

    output = tmp_path / "stdio-output"
    resources = tmp_path / "resources"
    runtime = tmp_path / "runtime"
    for directory in (output, resources, runtime):
        directory.mkdir()
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "tianlai.mcp_server"],
        cwd=runtime,
        env={
            "TIANLAI_HOME": str(ROOT),
            "TIANLAI_OUTPUT_DIR": str(output),
            "TIANLAI_RESOURCE_DIR": str(resources),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
        encoding="utf-8",
        encoding_error_handler="strict",
    )
    async with Client(
        stdio_client(server),
        mode="auto",
        raise_exceptions=False,
        read_timeout_seconds=30,
    ) as session:
        created_result = await session.call_tool(
            "create_authoring_project",
            {"project_key": "stdio-project", "title": "Stdio project"},
        )
        assert created_result.is_error is False
        created = created_result.structured_content
        assert created["ok"] is True
        snapshot_result = await session.call_tool(
            "get_authoring_snapshot",
            {"project_key": "stdio-project"},
        )
        snapshot = snapshot_result.structured_content
        documents = snapshot["snapshot"]["documents"]
        documents["score"]["title"] = "Stdio edit"
        save_arguments = {
            "project_key": "stdio-project",
            "expected_revision": created["project"]["revision"],
            "documents": documents,
        }
        saved_result = await session.call_tool(
            "save_authoring_project",
            save_arguments,
        )
        assert saved_result.structured_content["ok"] is True
        stale_result = await session.call_tool(
            "save_authoring_project",
            save_arguments,
        )
        assert stale_result.is_error is False
        stale = stale_result.structured_content
        assert stale["ok"] is False
        assert stale["error"]["code"] == "authoring_project.revision_conflict"
        _assert_no_local_path(stale, tmp_path)

        guide_result = await session.call_tool("creative_workflow_guide", {})
        assert guide_result.is_error is False
        guide = guide_result.structured_content
        assert guide["guide"]["authority"]["mcp_final_authority"] == "agent"
        assert guide["guide"]["constitution"]["full_document_injected"] is False
        clauses_result = await session.call_tool(
            "get_music_constitution_clauses",
            {"clause_ids": ["C0.06", "C4.1.16"]},
        )
        assert clauses_result.is_error is False
        clauses = clauses_result.structured_content
        assert [item["clause_id"] for item in clauses["clauses"]] == [
            "C0.06",
            "C4.1.16",
        ]

        workflow_result = await session.call_tool(
            "create_creative_workflow",
            {
                "project_key": "stdio-project",
                "mode": "iterate",
                "base_authoring_revision": saved_result.structured_content[
                    "project"
                ]["revision"],
            },
        )
        assert workflow_result.is_error is False
        workflow = workflow_result.structured_content
        assert workflow["ok"] is True
        assert workflow["workflow"]["state"]["final_authority"] == "agent"
        workflow_id = workflow["workflow"]["workflow"]["workflow_id"]
        workflow_revision = workflow["workflow"]["workflow"]["revision"]

        activated_result = await session.call_tool(
            "activate_creative_workflow",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
                "expected_revision": workflow_revision,
                "work_charter": {
                    "title": "Stdio bounded charter",
                    "one_sentence_promise": "Preserve one traceable motif.",
                    "target_listener_and_scene": "A focused local listener.",
                    "primary_sovereignty": ["M"],
                    "identity_kernel": {
                        "invariants": ["opening contour"],
                        "transformable_parts": ["register"],
                    },
                    "ending_contract": "Answer the opening without merely stopping.",
                },
            },
        )
        activated = activated_result.structured_content
        assert activated["ok"] is True
        assert activated["next_action"]["operation"] == (
            "record_workflow_composition_map"
        )
        workflow_revision = activated["workflow"]["workflow"]["revision"]

        legacy_workflow_result = await session.call_tool(
            "create_creative_workflow",
            {
                "project_key": "stdio-project",
                "mode": "iterate",
                "base_authoring_revision": saved_result.structured_content[
                    "project"
                ]["revision"],
                "composition_governance": False,
            },
        )
        assert legacy_workflow_result.is_error is False
        legacy_workflow = legacy_workflow_result.structured_content
        assert legacy_workflow["ok"] is True
        legacy_workflow_id = legacy_workflow["workflow"]["workflow"]["workflow_id"]
        legacy_workflow_revision = legacy_workflow["workflow"]["workflow"][
            "revision"
        ]
        legacy_activated_result = await session.call_tool(
            "activate_creative_workflow",
            {
                "project_key": "stdio-project",
                "workflow_id": legacy_workflow_id,
                "expected_revision": legacy_workflow_revision,
                "work_charter": {
                    "title": "Explicit legacy-flow opt-out",
                    "one_sentence_promise": "Preserve one traceable motif.",
                    "target_listener_and_scene": "A focused local listener.",
                    "primary_sovereignty": ["M"],
                    "identity_kernel": {
                        "invariants": ["opening contour"],
                        "transformable_parts": ["register"],
                    },
                    "ending_contract": "Answer the opening without merely stopping.",
                },
            },
        )
        assert legacy_activated_result.is_error is False
        legacy_activated = legacy_activated_result.structured_content
        assert legacy_activated["ok"] is True
        assert legacy_activated["next_action"]["operation"] == (
            "record_workflow_review"
        )
        assert legacy_activated["next_action"]["suggested_arguments"]["phase"] == (
            "intent"
        )

        composition_result = await session.call_tool(
            "inspect_workflow_composition",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
            },
        )
        composition = composition_result.structured_content
        assert composition["ok"] is True
        context = composition["inspection"]
        ending_claim = next(
            item["claim_id"]
            for item in context["charter_claim_index"]["claims"]
            if item["field_path"] == ["ending_contract"]
        )
        composition_map = {
            "kind": "tianlai.composition_map",
            "schema_version": 1,
            "nodes": [
                {
                    "node_id": "whole-work",
                    "label": "Whole work",
                    "function": "Carry the current motif through one sequence.",
                    "depends_on_claim_ids": [ending_claim],
                    "ending_response": "Answer the opening with consequence.",
                }
            ],
        }
        draft_result = await session.call_tool(
            "inspect_workflow_composition",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
                "composition_map": composition_map,
            },
        )
        draft = draft_result.structured_content
        assert draft["ok"] is True
        assert draft["next_action"]["operation"] == (
            "record_workflow_composition_map"
        )
        mapped_result = await session.call_tool(
            "record_workflow_composition_map",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
                "expected_revision": workflow_revision,
                "composition_map": composition_map,
            },
        )
        mapped = mapped_result.structured_content
        assert mapped["ok"] is True
        workflow_revision = mapped["workflow"]["workflow"]["revision"]
        reviewed_context_result = await session.call_tool(
            "inspect_workflow_composition",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
            },
        )
        reviewed_context = reviewed_context_result.structured_content["inspection"]
        question_answers = [
            {
                "question_id": question["question_id"],
                "answer": "The cited claim and map node support this answer.",
                "claim_ids": [ending_claim],
                "node_ids": ["whole-work"],
                "event_ids": [],
            }
            for question in reviewed_context["review_questions"]["intent"]
        ]

        review_result = await session.call_tool(
            "record_workflow_review",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
                "expected_revision": workflow_revision,
                "phase": "intent",
                "perception_basis": "report_only",
                "summary": "Intent was reviewed without claiming human audition.",
                "question_answers": question_answers,
            },
        )
        review = review_result.structured_content
        assert review["ok"] is True
        workflow_revision = review["workflow"]["workflow"]["revision"]

        evidence_result = await session.call_tool(
            "record_workflow_evidence",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
                "expected_revision": workflow_revision,
                "category": "aesthetic_risk",
                "code": "structure.test_risk",
                "basis_kind": "diagnostic_hypothesis",
                "basis_reference": "stdio contract test",
                "perception_basis": "report_only",
                "summary": "A bounded risk hypothesis.",
                "observation": "The current edit has not been rendered.",
                "interpretation": "No positive quality claim follows.",
                "confidence": "low",
            },
        )
        evidence = evidence_result.structured_content
        assert evidence["ok"] is True
        assert evidence["workflow"]["state"]["iterations"][-1]["evidence"][0][
            "reporter"
        ] == "agent"
        workflow_revision = evidence["workflow"]["workflow"]["revision"]

        stopped_result = await session.call_tool(
            "stop_creative_workflow",
            {
                "project_key": "stdio-project",
                "workflow_id": workflow_id,
                "expected_revision": workflow_revision,
                "reason": "cancelled",
                "summary": "End the stdio contract exercise.",
            },
        )
        stopped = stopped_result.structured_content
        assert stopped["ok"] is True
        assert stopped["workflow"]["state"]["status"] == "stopped"
        for result in (
            guide,
            clauses,
            workflow,
            activated,
            review,
            evidence,
            stopped,
        ):
            _assert_no_local_path(result, tmp_path)
