from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional mcp package is not installed",
)


MUSIC_BOX = "键盘乐器/音乐盒"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workflow_mcp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from tianlai import mcp_server

    output = tmp_path / "output" / "mcp"
    output.parent.mkdir(parents=True)
    monkeypatch.setattr(mcp_server, "OUTPUT_DIR", output)
    return mcp_server, output


def _charter() -> dict[str, object]:
    return {
        "title": "Bounded MCP experiment",
        "one_sentence_promise": "Let a small motif earn one irreversible climax.",
        "target_listener_and_scene": "A focused listener in a quiet room.",
        "primary_sovereignty": ["M"],
        "identity_kernel": {
            "invariants": ["the opening three-note contour"],
            "transformable_parts": ["register", "orchestration"],
        },
        "ending_contract": "End with consequence, not merely with silence.",
    }


def _create_project(mcp_server, key: str = "workflow-project") -> dict:
    result = mcp_server.create_authoring_project(key, "Workflow project")
    assert result["ok"], result
    return result


def _create_workflow(
    mcp_server,
    *,
    key: str = "workflow-project",
    mode: str = "iterate",
    revision: str | None = None,
) -> dict:
    result = mcp_server.create_creative_workflow(
        key,
        mode,
        revision,
    )
    assert result["ok"], result
    return result


def _identity(result: dict) -> tuple[str, str]:
    identity = result["workflow"]["workflow"]
    return identity["workflow_id"], identity["revision"]


def _activate(mcp_server, created: dict, key: str = "workflow-project") -> dict:
    workflow_id, revision = _identity(created)
    result = mcp_server.activate_creative_workflow(
        key,
        workflow_id,
        revision,
        _charter(),
    )
    assert result["ok"], result
    return result


def _review(
    mcp_server,
    workflow: dict,
    phase: str,
    *,
    key: str = "workflow-project",
) -> dict:
    workflow_id, revision = _identity(workflow)
    result = mcp_server.record_workflow_review(
        key,
        workflow_id,
        revision,
        phase,
        "audio_audition" if phase == "audio_audition" else "report_only",
        f"Reviewed {phase} as bounded workflow evidence.",
    )
    assert result["ok"], result
    return result


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
        "name": "MCP workflow render",
        "assignments": [{"part": "part-1", "instrument": MUSIC_BOX}],
    }
    profile = documents["render_profile"]
    profile["name"] = "test-analysis"
    profile["expression"] = "strict"
    profile["normalize_peak_db"] = None
    profile["space"] = {"enabled": False}
    profile["collaboration_mode"] = "analyze"
    profile["write_stems"] = False
    profile["use_stem_cache"] = False
    return documents


def _assert_no_local_path(value: object, root: Path) -> None:
    encoded = json.dumps(value, ensure_ascii=False)
    assert str(root) not in encoded
    assert str(root.resolve()) not in encoded


def test_guide_supplies_activation_prerequisites_without_full_constitution(
    workflow_mcp,
) -> None:
    mcp_server, _output = workflow_mcp
    result = mcp_server.creative_workflow_guide()
    assert result["ok"] is True
    guide = result["guide"]
    constitution = guide["constitution"]
    payload = (
        ROOT / "docs" / "音乐创作参考笔记" / "天籁音乐宪法-v0.1.md"
    ).read_bytes()
    assert constitution["content_sha256"] == hashlib.sha256(payload).hexdigest()
    assert constitution["full_document_injected"] is False
    assert len(constitution["starter_clause_ids"]) <= 8
    assert guide["work_charter"]["required_fields"]
    assert guide["authority"] == {
        "mcp_final_authority": "agent",
        "trusted_human_approval_available": False,
        "note": guide["authority"]["note"],
    }
    assert "objective quality" not in json.dumps(guide).lower()

    selected = mcp_server.get_music_constitution_clauses(
        ["C0.02", "C0.25"],
    )
    assert selected["ok"] is True
    assert [item["clause_id"] for item in selected["clauses"]] == [
        "C0.02",
        "C0.25",
    ]
    assert selected["full_document_injected"] is False
    english = mcp_server.get_music_constitution_clauses(["C0.03"], "en")
    assert english["ok"] is True
    assert english["constitution"]["content_sha256"] == hashlib.sha256(
        (
            ROOT
            / "docs"
            / "音乐创作参考笔记"
            / "天籁音乐宪法-v0.1.en.md"
        ).read_bytes()
    ).hexdigest()
    unknown = mcp_server.get_music_constitution_clauses(["C8.999"])
    assert unknown["ok"] is False
    assert unknown["error"]["code"] == (
        "creative_workflow.constitution_clause_unknown"
    )

    _create_project(mcp_server, "constitution-project")
    created = _create_workflow(mcp_server, key="constitution-project")
    workflow_id, revision = _identity(created)
    invalid = mcp_server.activate_creative_workflow(
        "constitution-project",
        workflow_id,
        revision,
        _charter(),
        selected["constitution"],
        [
            {
                "clause_id": "C8.999",
                "role": "review_lens",
                "rationale": "Unknown clauses must not enter the official binding.",
                "interpretation": "Reject this activation.",
            }
        ],
    )
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == (
        "creative_workflow.constitution_clause_unknown"
    )
    activated = mcp_server.activate_creative_workflow(
        "constitution-project",
        workflow_id,
        revision,
        _charter(),
        selected["constitution"],
        [
            {
                "clause_id": "C0.02",
                "role": "review_lens",
                "rationale": "Avoid reducing the work to a single score.",
                "interpretation": "Keep review dimensions separate in this experiment.",
            }
        ],
    )
    assert activated["ok"] is True
    assert activated["constitution_source"] == "official"


def test_create_activate_cas_history_and_next_action_are_path_free(
    workflow_mcp,
) -> None:
    mcp_server, output = workflow_mcp
    _create_project(mcp_server)
    created = _create_workflow(mcp_server)
    workflow_id, created_revision = _identity(created)
    assert created["workflow"]["state"]["final_authority"] == "agent"
    assert created["next_action"]["operation"] == "activate_creative_workflow"

    active = _activate(mcp_server, created)
    assert active["next_action"]["operation"] == "record_workflow_review"
    assert active["next_action"]["suggested_arguments"]["phase"] == "intent"
    assert (
        "attach_workflow_candidate_for_audit"
        not in active["next_action"]["alternatives"]
    )
    stale = mcp_server.activate_creative_workflow(
        "workflow-project",
        workflow_id,
        created_revision,
        _charter(),
    )
    assert stale["ok"] is False
    assert stale["error"]["code"] == "creative_workflow.workflow_revision_conflict"
    assert stale["error"]["retryable"] is True

    opened = mcp_server.open_creative_workflow("workflow-project", workflow_id)
    assert opened["ok"] is True
    assert opened["historical_read_only"] is False
    assert "history" not in opened
    historical = mcp_server.open_creative_workflow(
        "workflow-project",
        workflow_id,
        created_revision,
    )
    assert historical["ok"] is True
    assert historical["historical_read_only"] is True
    assert historical["next_action"] == {
        "operation": "open_creative_workflow",
        "reason": "historical_revision_is_read_only_refresh_current",
        "suggested_arguments": {
            "project_key": "workflow-project",
            "workflow_id": workflow_id,
        },
        "alternatives": [],
    }
    history = mcp_server.verify_creative_workflow_history(
        "workflow-project",
        workflow_id,
    )
    assert history["ok"] is True
    assert history["history"]["complete"] is True
    assert history["history"]["verified_revision_count"] == 2

    unactivated = _create_workflow(mcp_server, mode="audit")
    unactivated_id, unactivated_revision = _identity(unactivated)
    assert "stop_creative_workflow" in unactivated["next_action"]["alternatives"]
    cancelled = mcp_server.stop_creative_workflow(
        "workflow-project",
        unactivated_id,
        unactivated_revision,
        "cancelled",
        "Cancel before freezing a charter.",
    )
    assert cancelled["ok"], cancelled
    assert cancelled["workflow"]["state"]["status"] == "stopped"
    assert cancelled["workflow"]["state"]["work_charter"] is None
    assert cancelled["next_action"] is None

    for result in (
        created,
        active,
        stale,
        opened,
        historical,
        history,
        unactivated,
        cancelled,
    ):
        _assert_no_local_path(result, output.parent.parent)


def test_candidate_inspection_never_treats_shape_only_claim_as_authority(
    workflow_mcp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tianlai.creative_workflow import CreativeWorkflowError

    mcp_server, output = workflow_mcp
    authorization = {
        "workflow_id": "1" * 32,
        "project_id": "2" * 32,
        "reservation_revision": "3" * 64,
        "iteration_number": 1,
        "operation_id": "4" * 32,
        "authoring_revision": "5" * 64,
        "candidate_work_id": "work-id",
        "candidate_id": "candidate-id",
        "parent_work_id": None,
        "parent_candidate_id": None,
        "parent_manifest_sha256": None,
    }

    def reject_history(*_args, **_kwargs):
        raise CreativeWorkflowError("render_reservation_not_in_current_history")

    monkeypatch.setattr(
        mcp_server,
        "inspect_workflow_candidate_status",
        reject_history,
    )
    status = mcp_server._verified_candidate_workflow_status(
        output,
        output / "non-authoritative-candidate",
        {"authoring_workflow": authorization},
    )
    assert status["workflow_claim_present"] is True
    assert status["workflow_authorized"] is False
    assert status["workflow_recorded"] is False
    assert status["workflow_accepted"] is False
    assert status["workflow_managed"] is False
    assert status["authoring_workflow"] is None


def test_only_trusted_readiness_can_create_hard_failure(workflow_mcp) -> None:
    mcp_server, output = workflow_mcp
    _create_project(mcp_server)
    active = _activate(mcp_server, _create_workflow(mcp_server))
    workflow_id, revision = _identity(active)

    forged = mcp_server.record_workflow_evidence(
        "workflow-project",
        workflow_id,
        revision,
        "hard_failure",
        "authoring_roster.unassigned_part",
        "engine_contract",
        "caller assertion",
        "report_only",
        "Forged hard failure",
        "No trusted validation was run.",
        "This must not enter the blocking channel.",
        "high",
    )
    assert forged["ok"] is False
    assert forged["error"]["code"] == (
        "creative_workflow.hard_failure_requires_trusted_boundary"
    )

    verified = mcp_server.record_verified_workflow_hard_failure(
        "workflow-project",
        workflow_id,
        revision,
        "authoring_roster.unassigned_part",
    )
    assert verified["ok"] is True
    evidence = verified["workflow"]["state"]["iterations"][-1]["evidence"][0]
    assert evidence["category"] == "hard_failure"
    assert evidence["reporter"] == "validator"
    assert evidence["blocking"] is True

    rejected_exception = mcp_server.register_workflow_exception(
        "workflow-project",
        workflow_id,
        _identity(verified)[1],
        "work_charter",
        "identity_kernel",
        "Try to waive a hard contract",
        "This iteration",
        "Aesthetic intent",
        "Unsafe output",
        "None",
        [evidence["evidence_id"]],
    )
    assert rejected_exception["ok"] is False
    assert rejected_exception["error"]["code"] == (
        "creative_workflow.hard_failure_cannot_be_excepted"
    )
    stopped = mcp_server.stop_creative_workflow(
        "workflow-project",
        workflow_id,
        _identity(verified)[1],
        "external_blocker",
        "Stop because the trusted hard contract remains unresolved.",
    )
    assert stopped["ok"] is True
    assert stopped["workflow"]["state"]["status"] == "stopped"
    _assert_no_local_path(stopped, output.parent.parent)


def test_recovered_hard_failure_returns_next_action_to_normal_render_path(
    workflow_mcp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import tianlai.creative_workflow as workflow_core

    mcp_server, output = workflow_mcp
    _create_project(mcp_server)
    active = _activate(mcp_server, _create_workflow(mcp_server))
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        active = _review(mcp_server, active, phase)
    workflow_id, revision = _identity(active)
    blocked = {
        "status": "blocked",
        "render_allowed": False,
        "issues": [{"code": "output.not_writable", "decision": "block"}],
        "issues_truncated": False,
    }
    ready = {
        "status": "ready",
        "render_allowed": True,
        "issues": [],
        "issues_truncated": False,
    }
    monkeypatch.setattr(
        workflow_core,
        "validate_project_readiness",
        lambda *_args, **_kwargs: blocked,
    )
    recorded = mcp_server.record_verified_workflow_hard_failure(
        "workflow-project",
        workflow_id,
        revision,
        "output.not_writable",
    )
    assert recorded["ok"], recorded
    assert recorded["next_action"]["operation"] == "decide_workflow_iteration"
    assert recorded["next_action"]["reason"].startswith("unresolved_hard_failure")

    monkeypatch.setattr(
        workflow_core,
        "validate_project_readiness",
        lambda *_args, **_kwargs: ready,
    )
    recovered = mcp_server.open_creative_workflow(
        "workflow-project",
        workflow_id,
    )
    assert recovered["ok"], recovered
    assert recovered["next_action"]["operation"] == "render_workflow_candidate"
    evidence = recovered["workflow"]["state"]["iterations"][-1]["evidence"]
    assert evidence[0]["category"] == "hard_failure"
    assert evidence[0]["blocking"] is True
    _assert_no_local_path(recovered, output.parent.parent)


def test_render_failure_leaves_retryable_reservation_that_can_be_cancelled(
    workflow_mcp,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tianlai.authoring_render import AuthoringRenderError

    mcp_server, output = workflow_mcp
    _create_project(mcp_server)
    active = _activate(mcp_server, _create_workflow(mcp_server))
    active = _review(mcp_server, active, "intent")
    active = _review(mcp_server, active, "symbolic_structure")
    active = _review(mcp_server, active, "orchestration_performance")
    workflow_id, revision = _identity(active)

    def fail_render(*_args, **_kwargs):
        raise AuthoringRenderError("test.injected", stage="render", retryable=True)

    monkeypatch.setattr(mcp_server, "render_authoring_project_candidate", fail_render)
    failed = mcp_server.render_workflow_candidate(
        "workflow-project",
        workflow_id,
        revision,
    )
    assert failed["ok"] is False
    assert failed["error"]["code"] == "authoring_render.test.injected"
    assert failed["error"]["retryable"] is True
    assert failed["workflow"]["state"]["status"] == "candidate_pending"
    assert failed["next_action"]["operation"] == "render_workflow_candidate"

    cancelled = mcp_server.cancel_workflow_render(
        "workflow-project",
        workflow_id,
        failed["workflow"]["workflow"]["revision"],
    )
    assert cancelled["ok"] is True
    assert cancelled["workflow"]["state"]["status"] == "reviewing"
    _assert_no_local_path(cancelled, output.parent.parent)


def test_managed_render_revision_rollback_accept_and_audit_attachment(
    workflow_mcp,
) -> None:
    mcp_server, output = workflow_mcp
    project = _create_project(mcp_server)
    snapshot = mcp_server.get_authoring_snapshot("workflow-project")["snapshot"]
    saved = mcp_server.save_authoring_project(
        "workflow-project",
        project["project"]["revision"],
        _renderable_documents(snapshot),
    )
    assert saved["ok"], saved
    authoring_revision = saved["project"]["revision"]

    active = _activate(
        mcp_server,
        _create_workflow(mcp_server, revision=authoring_revision),
    )
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        active = _review(mcp_server, active, phase)
    workflow_id, revision = _identity(active)
    rendered = mcp_server.render_workflow_candidate(
        "workflow-project",
        workflow_id,
        revision,
    )
    assert rendered["ok"], rendered
    assert rendered["render"]["workflow_managed"] is True
    candidate = rendered["render"]["candidate"]
    inspected = mcp_server.inspect_authoring_candidate(
        "workflow-project",
        candidate["work_id"],
        candidate["candidate_id"],
    )
    assert inspected["ok"], inspected
    assert inspected["candidate"]["workflow_authorized"] is True
    assert inspected["candidate"]["workflow_recorded"] is True
    assert inspected["candidate"]["workflow_accepted"] is False

    reviewed = _review(mcp_server, rendered, "render_report")
    workflow_id, revision = _identity(reviewed)
    evidence = mcp_server.record_workflow_evidence(
        "workflow-project",
        workflow_id,
        revision,
        "aesthetic_risk",
        "structure.motive_evaporation",
        "diagnostic_hypothesis",
        "symbolic self-review",
        "report_only",
        "The motif may disappear rather than transform.",
        "No later event presently recalls its contour.",
        "A bounded revision can test one transformed recurrence.",
        "medium",
    )
    assert evidence["ok"], evidence
    evidence_item = evidence["workflow"]["state"]["iterations"][-1]["evidence"][-1]
    workflow_id, revision = _identity(evidence)
    exception = mcp_server.register_workflow_exception(
        "workflow-project",
        workflow_id,
        revision,
        "work_charter",
        "identity_kernel",
        "Preserve the unmodified first candidate while testing a recurrence.",
        "The next authoring revision only.",
        "Unknown but potentially valuable roughness.",
        "The recurrence may become too explicit.",
        "Rollback to the immutable first candidate.",
        [evidence_item["evidence_id"]],
    )
    assert exception["ok"], exception
    workflow_id, revision = _identity(exception)
    revision_pending = mcp_server.decide_workflow_iteration(
        "workflow-project",
        workflow_id,
        revision,
        "revise",
        "Test one bounded recurrence.",
        "The recorded risk is contextual and falsifiable.",
        "report_only",
        evidence_ids=[evidence_item["evidence_id"]],
        exception_ids=[
            exception["workflow"]["state"]["iterations"][-1]["exceptions"][-1][
                "exception_id"
            ]
        ],
        expected_audible_change="The opening contour returns once in a new register.",
    )
    assert revision_pending["ok"], revision_pending
    assert revision_pending["workflow"]["state"]["status"] == "revision_pending"
    revision_handoff = revision_pending["next_action"]
    assert revision_handoff["operation"] == "get_authoring_snapshot"
    assert revision_handoff["suggested_arguments"] == {
        "project_key": "workflow-project",
        "revision": authoring_revision,
    }
    assert [
        step.get("operation", step.get("action"))
        for step in revision_handoff["prerequisites"]
    ] == [
        "get_authoring_snapshot",
        "edit_complete_authoring_documents",
        "save_authoring_project",
        "record_workflow_authoring_revision",
    ]
    assert revision_handoff["continuation"] == {
        "workflow_id": workflow_id,
        "expected_revision": _identity(revision_pending)[1],
    }

    authoring_snapshot = mcp_server.get_authoring_snapshot("workflow-project")[
        "snapshot"
    ]
    changed_documents = copy.deepcopy(authoring_snapshot["documents"])
    changed_documents["score"]["tail_seconds"] = 0.06
    advanced = mcp_server.save_authoring_project(
        "workflow-project",
        authoring_snapshot["project"]["revision"],
        changed_documents,
    )
    assert advanced["ok"], advanced
    workflow_id, revision = _identity(revision_pending)
    second_iteration = mcp_server.record_workflow_authoring_revision(
        "workflow-project",
        workflow_id,
        revision,
        advanced["project"]["revision"],
    )
    assert second_iteration["ok"], second_iteration
    second_iteration = _review(mcp_server, second_iteration, "intent")
    workflow_id, revision = _identity(second_iteration)
    rolled_back = mcp_server.rollback_creative_workflow(
        "workflow-project",
        workflow_id,
        revision,
        1,
        "Return to the stronger immutable candidate.",
        "The bounded experiment did not justify replacing it.",
        "report_only",
    )
    assert rolled_back["ok"], rolled_back
    assert len(rolled_back["workflow"]["state"]["iterations"]) == 3

    for phase in ("intent", "symbolic_structure", "orchestration_performance", "render_report"):
        rolled_back = _review(mcp_server, rolled_back, phase)
    workflow_id, revision = _identity(rolled_back)
    accepted = mcp_server.decide_workflow_iteration(
        "workflow-project",
        workflow_id,
        revision,
        "accept",
        "Accept this candidate under the frozen charter.",
        "All required review phases are recorded; this is contextual, not a quality guarantee.",
        "report_only",
    )
    assert accepted["ok"], accepted
    assert accepted["workflow"]["state"]["status"] == "completed"
    accepted_inspection = mcp_server.inspect_authoring_candidate(
        "workflow-project",
        candidate["work_id"],
        candidate["candidate_id"],
    )
    assert accepted_inspection["candidate"]["workflow_authorized"] is True
    assert accepted_inspection["candidate"]["workflow_recorded"] is True
    assert accepted_inspection["candidate"]["workflow_accepted"] is True

    audit = _activate(
        mcp_server,
        _create_workflow(
            mcp_server,
            mode="audit",
            revision=authoring_revision,
        ),
    )
    assert (
        "attach_workflow_candidate_for_audit"
        in audit["next_action"]["alternatives"]
    )
    audit_id, audit_revision = _identity(audit)
    attached = mcp_server.attach_workflow_candidate_for_audit(
        "workflow-project",
        audit_id,
        audit_revision,
        candidate["work_id"],
        candidate["candidate_id"],
    )
    assert attached["ok"], attached
    assert attached["workflow"]["state"]["iterations"][-1]["anchor"][
        "candidate"
    ]["workflow_managed"] is False

    history = mcp_server.verify_creative_workflow_history(
        "workflow-project",
        workflow_id,
    )
    assert history["ok"], history
    assert history["history"]["complete"] is True
    for result in (rendered, inspected, accepted, accepted_inspection, attached, history):
        _assert_no_local_path(result, output.parent.parent)
