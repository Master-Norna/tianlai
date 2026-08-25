from __future__ import annotations

import importlib.util

import pytest


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional mcp package is not installed",
)


LEGACY_CONSTITUTION = {
    "document_id": "tianlai-music-constitution",
    "version": "0.1",
    "language": "zh-CN",
    "content_sha256": (
        "3c26f99806b2044b3fd45cbdc8ef12ff"
        "adf871d75dc119799881b0d992b75985"
    ),
}

LEGACY_ACTIVE_CLAUSES = [
    {
        "clause_id": "C3.M.01",
        "role": "review_lens",
        "rationale": "Retain the frozen historical reference without reviving it.",
        "interpretation": (
            "This work-specific interpretation remains part of the immutable workflow."
        ),
    }
]


@pytest.fixture
def workflow_mcp(tmp_path, monkeypatch):
    from tianlai import mcp_server

    output = tmp_path / "output" / "mcp"
    output.parent.mkdir(parents=True)
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", output)
    return mcp_server, output


def _charter() -> dict[str, object]:
    return {
        "title": "Optional constitution boundary",
        "one_sentence_promise": "Let one small identity change with consequence.",
        "target_listener_and_scene": "A focused listener in a quiet room.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["one traceable contour"],
            "transformable_parts": ["register", "orchestration"],
        },
        "ending_contract": "Return the identity with a clear consequence.",
    }


def _identity(result: dict) -> tuple[str, str]:
    identity = result["workflow"]["workflow"]
    return identity["workflow_id"], identity["revision"]


def _create_project_and_workflow(
    mcp_server,
    *,
    key: str,
    composition_governance: bool,
) -> dict:
    project = mcp_server.create_authoring_project(key, "Constitution boundary")
    assert project["ok"], project
    workflow = mcp_server.create_creative_workflow(
        key,
        "iterate",
        composition_governance=composition_governance,
    )
    assert workflow["ok"], workflow
    return workflow


def _forbid_registry(monkeypatch, mcp_server) -> list[str]:
    calls: list[str] = []

    def forbidden(language: str):
        calls.append(language)
        raise AssertionError("the official constitution registry is out of scope")

    monkeypatch.setattr(mcp_server, "_official_constitution_registry", forbidden)
    return calls


def _publish_legacy_binding(workflow_core, *, project_root, snapshot):
    """Seal an old binding shape without keeping a production write bypass."""

    state = snapshot.detached_state()
    state["constitution"] = LEGACY_CONSTITUTION
    state["active_clauses"] = LEGACY_ACTIVE_CLAUSES
    layout = workflow_core._existing_layout(project_root, snapshot.workflow_id)
    revision = workflow_core._publish_revision(layout, state)
    workflow_core._replace_manifest(
        layout,
        workflow_core._manifest_document(
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            created_at_utc=state["created_at_utc"],
            updated_at_utc=state["updated_at_utc"],
            revision=revision,
            sequence=state["sequence"],
        ),
    )
    return workflow_core.open_creative_workflow(
        project_root, workflow_id=snapshot.workflow_id, revision=revision
    )


def _composition_map(context: dict) -> dict:
    ending_claim = next(
        (
            claim["claim_id"]
            for claim in context["charter_claim_index"]["claims"]
            if claim["field_path"] == ["ending_contract"]
        ),
        context["charter_claim_index"]["claims"][0]["claim_id"],
    )
    return {
        "kind": "tianlai.composition_map",
        "schema_version": 1,
        "nodes": [
            {
                "node_id": "whole-work",
                "label": "Whole work",
                "function": "Carry this work's own identity through one sequence.",
                "depends_on_claim_ids": [ending_claim],
                "ending_response": "Return the identity with audible consequence.",
            }
        ],
    }


def _question_references(
    context: dict,
    question: dict,
) -> tuple[list[str], list[str], list[str]]:
    available_claims = {
        item["claim_id"] for item in context["charter_claim_index"]["claims"]
    }
    node_dependencies = {
        item["node_id"]: item["depends_on_claim_ids"]
        for item in context["composition_map"]["nodes"]
    }
    claims: set[str] = set()
    nodes: set[str] = set()
    events: set[str] = set()

    def collect(value: object, *, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                collect(child, key=child_key)
            return
        if isinstance(value, list):
            for child in value:
                collect(child, key=key)
            return
        if not isinstance(value, str) or key is None:
            return
        if key in {"claim_id", "claim_ids"} or key.endswith("_claim_ids"):
            if value in available_claims:
                claims.add(value)
        elif key in {"node_id", "node_ids"} or key.endswith("_node_ids"):
            if value in node_dependencies:
                nodes.add(value)
        elif key in {"event_id", "event_ids"} or key.endswith("_event_ids"):
            events.add(value)

    collect(question["basis"])
    collect(question["location"])
    if question["basis"] == {"source": "whole_work_governance"}:
        nodes.add(next(iter(node_dependencies)))
    for node_id in nodes:
        claims.update(node_dependencies[node_id])
    if not claims and not nodes and not events:
        claims.add(next(iter(available_claims)))
    return sorted(claims), sorted(nodes), sorted(events)


def _intent_answers(context: dict) -> list[dict[str, object]]:
    return [
        {
            "question_id": question["question_id"],
            "answer": "The cited current-work claim and map node support this answer.",
            "claim_ids": references[0],
            "node_ids": references[1],
            "event_ids": references[2],
        }
        for question in context["review_questions"]["intent"]
        for references in [_question_references(context, question)]
    ]


def test_workflow_without_constitution_never_reads_registry_and_reaches_map_review(
    workflow_mcp,
    monkeypatch,
) -> None:
    mcp_server, _output = workflow_mcp
    registry_calls = _forbid_registry(monkeypatch, mcp_server)
    created = _create_project_and_workflow(
        mcp_server,
        key="no-constitution",
        composition_governance=True,
    )
    workflow_id, revision = _identity(created)

    activated = mcp_server.activate_creative_workflow(
        "no-constitution",
        workflow_id,
        revision,
        _charter(),
    )
    assert activated["ok"], activated
    assert activated["constitution_source"] is None
    assert activated["workflow"]["state"]["constitution"] is None
    assert activated["workflow"]["state"]["active_clauses"] == []
    assert activated["constitution_context"] == {
        "status": "unbound",
        "recorded_binding_preserved": False,
        "provenance_only": False,
        "clause_lookup_required": False,
        "clause_mapping_allowed": False,
        "generation_constraint": False,
        "acceptance_gate": False,
        "continuation_gate": False,
        "new_decision_reference_allowed": False,
    }

    inspected = mcp_server.inspect_workflow_composition(
        "no-constitution",
        workflow_id,
    )
    assert inspected["ok"], inspected
    composition_map = _composition_map(inspected["inspection"])
    draft = mcp_server.inspect_workflow_composition(
        "no-constitution",
        workflow_id,
        composition_map=composition_map,
    )
    assert draft["ok"], draft
    assert draft["inspection"]["composition_map_source"] == "draft"

    recorded = mcp_server.record_workflow_composition_map(
        "no-constitution",
        workflow_id,
        _identity(activated)[1],
        composition_map,
    )
    assert recorded["ok"], recorded
    review_context = mcp_server.inspect_workflow_composition(
        "no-constitution",
        workflow_id,
    )
    assert review_context["ok"], review_context
    reviewed = mcp_server.record_workflow_review(
        "no-constitution",
        workflow_id,
        _identity(recorded)[1],
        "intent",
        "report_only",
        "Review the work's own promise without an external constitution.",
        question_answers=_intent_answers(review_context["inspection"]),
    )
    assert reviewed["ok"], reviewed
    assert reviewed["workflow"]["state"]["iterations"][-1]["reviews"][-1][
        "phase"
    ] == "intent"
    assert reviewed["workflow"]["state"]["constitution"] is None
    assert registry_calls == []


def test_legacy_v01_binding_opens_verifies_and_continues_without_registry_or_remapping(
    workflow_mcp,
    monkeypatch,
) -> None:
    from tianlai import creative_workflow as workflow_core

    mcp_server, output = workflow_mcp
    created = _create_project_and_workflow(
        mcp_server,
        key="legacy-v01",
        composition_governance=False,
    )
    workflow_id, revision = _identity(created)
    project_root = output / "authoring-projects" / "legacy-v01"
    activated = workflow_core.activate_creative_workflow(
        project_root,
        workflow_id=workflow_id,
        expected_revision=revision,
        work_charter=_charter(),
    )
    historical = _publish_legacy_binding(
        workflow_core, project_root=project_root, snapshot=activated
    )
    registry_calls = _forbid_registry(monkeypatch, mcp_server)

    opened = mcp_server.open_creative_workflow("legacy-v01", workflow_id)
    assert opened["ok"], opened
    assert opened["workflow"]["state"]["constitution"] == LEGACY_CONSTITUTION
    assert opened["workflow"]["state"]["active_clauses"] == LEGACY_ACTIVE_CLAUSES
    assert opened["constitution_context"] == {
        "status": "retired_provenance_only",
        "recorded_binding_preserved": True,
        "provenance_only": True,
        "clause_lookup_required": False,
        "clause_mapping_allowed": False,
        "generation_constraint": False,
        "acceptance_gate": False,
        "continuation_gate": False,
        "new_decision_reference_allowed": False,
    }
    history = mcp_server.verify_creative_workflow_history(
        "legacy-v01",
        workflow_id,
    )
    assert history["ok"], history
    assert history["history"]["complete"] is True

    reviewed = mcp_server.record_workflow_review(
        "legacy-v01",
        workflow_id,
        historical.revision,
        "intent",
        "report_only",
        "Continue from the frozen work charter without consulting retired text.",
    )
    assert reviewed["ok"], reviewed
    assert reviewed["workflow"]["state"]["constitution"] == LEGACY_CONSTITUTION
    assert reviewed["workflow"]["state"]["active_clauses"] == LEGACY_ACTIVE_CLAUSES
    assert reviewed["workflow"]["workflow"]["sequence"] == historical.to_dict()[
        "workflow"
    ]["sequence"] + 1
    continued_history = mcp_server.verify_creative_workflow_history(
        "legacy-v01",
        workflow_id,
    )
    assert continued_history["ok"], continued_history
    assert continued_history["history"]["complete"] is True
    assert registry_calls == []


def test_legacy_clause_references_cannot_create_new_workflow_claims(
    workflow_mcp,
) -> None:
    from tianlai import creative_workflow as workflow_core

    mcp_server, output = workflow_mcp
    created = _create_project_and_workflow(
        mcp_server,
        key="legacy-write-boundary",
        composition_governance=False,
    )
    workflow_id, revision = _identity(created)
    project_root = output / "authoring-projects" / "legacy-write-boundary"
    activated = workflow_core.activate_creative_workflow(
        project_root,
        workflow_id=workflow_id,
        expected_revision=revision,
        work_charter=_charter(),
    )
    historical = _publish_legacy_binding(
        workflow_core, project_root=project_root, snapshot=activated
    )

    evidence = mcp_server.record_workflow_evidence(
        "legacy-write-boundary",
        workflow_id,
        historical.revision,
        "promise_conflict",
        "constitution.retired_reference",
        "active_clause",
        "C3.M.01",
        "report_only",
        "A retired clause cannot become a new claim.",
        "The historical ID remains in provenance.",
        "Continue reasoning from the work charter instead.",
        "high",
    )
    assert evidence["ok"] is False
    assert evidence["error"]["code"] == (
        "creative_workflow.active_clause_provenance_only"
    )

    exception = mcp_server.register_workflow_exception(
        "legacy-write-boundary",
        workflow_id,
        historical.revision,
        "active_clause",
        "C3.M.01",
        "Do not turn provenance into a current exception.",
        "This workflow.",
        "The work's own judgment.",
        "No current cost applies.",
        "Return to the work charter.",
        ["evidence-00000000000000000000"],
    )
    assert exception["ok"] is False
    assert exception["error"]["code"] == (
        "creative_workflow.active_clause_provenance_only"
    )

    derivation = mcp_server.record_workflow_derivation(
        "legacy-write-boundary",
        workflow_id,
        historical.revision,
        "Do not derive new music from a retired clause.",
        [
            {
                "kind": "active_clause",
                "reference": "C3.M.01",
                "event_ids": [],
                "artifact_sha256": None,
                "artifact_role": None,
            }
        ],
        [
            {
                "alternative": "Use the work's own material.",
                "failure": "The retired clause supplies no current authority.",
                "premise_indexes": [0],
            }
        ],
        event_ids=["event-1"],
        clause_ids=["C3.M.01"],
    )
    assert derivation["ok"] is False
    assert derivation["error"]["code"] == (
        "creative_workflow.active_clause_provenance_only"
    )

    reopened = mcp_server.open_creative_workflow(
        "legacy-write-boundary",
        workflow_id,
    )
    assert reopened["ok"], reopened
    assert _identity(reopened)[1] == historical.revision


def test_current_constitution_getter_rejects_legacy_clause_ids(workflow_mcp) -> None:
    mcp_server, _output = workflow_mcp

    result = mcp_server.get_music_constitution_clauses(
        ["C3.M.01", "C5.2.10"],
    )

    assert result["ok"] is False
    assert result["error"]["code"] == (
        "creative_workflow.constitution_clause_unknown"
    )
