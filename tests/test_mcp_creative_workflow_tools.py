from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError


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


def test_revision_scope_typed_contract_enforces_exact_bounded_authority() -> None:
    from tianlai import mcp_server

    adapter = TypeAdapter(mcp_server._WorkflowRevisionScopeInput)
    bounded = {
        "change_scale": "bounded",
        "documents": ["score"],
        "allowed_document_paths": {"score": ["/tail_seconds"]},
        "score": {
            "part_ids": [],
            "event_ids": [],
            "bar_ranges": [],
            "allowed_note_fields": [],
            "allow_event_additions": False,
            "allow_event_deletions": False,
            "allow_reordering": False,
        },
        "whole_work_cost": None,
    }
    assert adapter.validate_python(bounded) == bounded

    invalid_values = []
    reversed_range = copy.deepcopy(bounded)
    reversed_range["score"]["bar_ranges"] = [{"start": 3, "end": 2}]
    invalid_values.append(reversed_range)
    broad_reordering = copy.deepcopy(bounded)
    broad_reordering["score"]["allow_reordering"] = True
    invalid_values.append(broad_reordering)
    note_without_event = copy.deepcopy(bounded)
    note_without_event["score"]["allowed_note_fields"] = ["pitch"]
    invalid_values.append(note_without_event)
    note_pointer = copy.deepcopy(bounded)
    note_pointer["allowed_document_paths"] = {
        "score": ["/parts/0/notes/0/pitch"]
    }
    invalid_values.append(note_pointer)
    invalid_escape = copy.deepcopy(bounded)
    invalid_escape["allowed_document_paths"] = {"score": ["/bad~2escape"]}
    invalid_values.append(invalid_escape)
    oversized_utf8 = copy.deepcopy(bounded)
    oversized_utf8["allowed_document_paths"] = {"score": ["/" + "界" * 512]}
    invalid_values.append(oversized_utf8)

    for value in invalid_values:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


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
    return _ensure_composition_map(mcp_server, result, key=key)


def _ensure_composition_map(
    mcp_server,
    workflow: dict,
    *,
    key: str = "workflow-project",
) -> dict:
    if "governance" not in workflow["workflow"]["state"]:
        return workflow
    workflow_id, revision = _identity(workflow)
    inspected = mcp_server.inspect_workflow_composition(key, workflow_id)
    assert inspected["ok"], inspected
    context = inspected["inspection"]
    if context["composition_map_source"] == "recorded":
        return workflow
    ending_claim = next(
        (
            claim["claim_id"]
            for claim in context["charter_claim_index"]["claims"]
            if claim["field_path"] == ["ending_contract"]
        ),
        context["charter_claim_index"]["claims"][0]["claim_id"],
    )
    composition_map = {
        "kind": "tianlai.composition_map",
        "schema_version": 1,
        "nodes": [
            {
                "node_id": "whole-work",
                "label": "Whole work",
                "function": (
                    "Carry the current work's own identity through one complete "
                    "sequence."
                ),
                "depends_on_claim_ids": [ending_claim],
                "ending_response": (
                    "Return the established identity with audible consequence."
                ),
            }
        ],
    }
    draft = mcp_server.inspect_workflow_composition(
        key,
        workflow_id,
        composition_map=composition_map,
    )
    assert draft["ok"], draft
    assert draft["inspection"]["composition_map_source"] == "draft"
    assert draft["next_action"]["operation"] == "record_workflow_composition_map"
    recorded = mcp_server.record_workflow_composition_map(
        key,
        workflow_id,
        revision,
        composition_map,
    )
    assert recorded["ok"], recorded
    return recorded


def _question_references(
    context: dict,
    question: dict,
    *,
    available_events: set[str],
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
            if value in available_events:
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


def _review(
    mcp_server,
    workflow: dict,
    phase: str,
    *,
    key: str = "workflow-project",
) -> dict:
    workflow = _ensure_composition_map(mcp_server, workflow, key=key)
    workflow_id, revision = _identity(workflow)
    inspected = mcp_server.inspect_workflow_composition(key, workflow_id)
    assert inspected["ok"], inspected
    context = inspected["inspection"]
    questions = context["review_questions"].get(phase, [])
    authoring = mcp_server.get_authoring_snapshot(
        key,
        context["authoring_revision"],
    )
    assert authoring["ok"], authoring
    available_events = {
        note["event_id"]
        for part in authoring["snapshot"]["documents"]["score"]["parts"]
        for note in part["notes"]
        if isinstance(note.get("event_id"), str)
    }
    question_answers = [
        {
            "question_id": question["question_id"],
            "answer": (
                "The cited current-work claim, map node and any located score "
                "events support this whole-work answer."
            ),
            "claim_ids": references[0],
            "node_ids": references[1],
            "event_ids": references[2],
        }
        for question in questions
        for references in [
            _question_references(
                context,
                question,
                available_events=available_events,
            )
        ]
    ]
    result = mcp_server.record_workflow_review(
        key,
        workflow_id,
        revision,
        phase,
        "audio_audition" if phase == "audio_audition" else "report_only",
        f"Reviewed {phase} as bounded workflow evidence.",
        question_answers=question_answers or None,
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
        ROOT / "docs" / "音乐创作参考笔记" / "天籁音乐宪法-v0.2.md"
    ).read_bytes()
    assert constitution["version"] == "0.2"
    assert constitution["content_sha256"] == hashlib.sha256(payload).hexdigest()
    assert constitution["full_document_injected"] is False
    assert len(constitution["starter_clause_ids"]) <= 8
    assert guide["work_charter"]["required_fields"]
    assert guide["authority"] == {
        "mcp_final_authority": "agent",
        "trusted_human_approval_available": False,
        "note": guide["authority"]["note"],
    }
    boundary = guide["boundary"]
    assert boundary["recorded_hard_failure_is_permanent_lock"] is False
    assert boundary["nonhard_claims_require_explicit_disposition"] is True
    assert boundary["aesthetic_risk_may_be_accepted_risk"] is True
    assert boundary["promise_conflict_may_be_accepted_risk"] is False
    assert guide["render_prerequisites"][:4] == [
        "current composition map bound to the full score and effective charter",
        "question-complete intent review",
        "question-complete symbolic_structure review",
        "question-complete orchestration_performance review",
    ]
    decisions = guide["decisions"]
    assert any(
        "trusted hard_failure still reproduced" in requirement
        for requirement in decisions["accept_requires"]
    )
    assert any(
        "no promise_conflict" in requirement
        for requirement in decisions["accept_requires"]
    )
    assert decisions["claim_lifecycle"] == {
        "review_ids_frozen_on_new_decisions": True,
        "nonhard_evidence_coverage": "exactly_once_per_current_iteration",
        "evidence_dispositions": [
            "resolved",
            "accepted_risk",
            "excepted",
            "deferred",
            "revision_target",
            "contested",
        ],
        "legacy_missing_fields_remain_readable": True,
        "acceptance_gate": {
            "new_accepts_freeze": "point_in_time_recorded_hard_failure_recheck",
            "binds": [
                "authoring_revision",
                "candidate_manifest_sha256",
                "checked_hard_failure_evidence_ids",
                "readiness_result_sha256",
            ],
            "does_not_prove": [
                "current_readiness",
                "unrecorded_issues_absent",
                "aesthetic_quality",
            ],
            "legacy_terminal_without_gate": "legacy_unfrozen_readable",
            "adds_workflow_step": False,
        },
            "charter_settlement": {
            "targets": [
                "one_sentence_promise",
                "identity_kernel.invariants[i] for each invariant",
                "ending_contract",
            ],
            "statuses": ["kept", "transformed", "refused"],
            "rules": [
                "every target settled at most once; acceptance requires full coverage",
                    "every settlement item cites non-empty basis among the decision's selected review, evidence, exception or derivation record ids",
                "transformed requires a derivation basis; refused requires an exception or derivation basis",
                "prohibited shortcuts are not settlement targets; violating one still requires a charter exception",
                    "legacy accepts without settlement remain readable",
                ],
            },
            "revision_contract": {
                "revise_requires": [
                    "a frozen bounded or explicitly cost-acknowledged whole-work scope",
                    "a withdrawal condition stated before authoring changes",
                ],
                "scope_enforcement": (
                    "record_workflow_authoring_revision compares the saved documents "
                    "with the contract baseline and rejects undeclared change surface"
                ),
                "challenger_settlement": [
                    "promote_challenger",
                    "retain_baseline",
                    "inconclusive",
                ],
                "boundary": (
                    "same workflow and same authoring-project chain only; no global "
                    "incumbent, cross-project inheritance or parent-version tree"
                ),
                "does_not_prove": [
                    "melodic quality",
                    "layering quality",
                    "aesthetic superiority",
                ],
            },
        }
    assert "objective quality" not in json.dumps(guide).lower()

    qiyun = guide["qiyun_space"]
    assert qiyun["no_quota"] is True
    assert qiyun["zero_additions_valid"] is True
    assert qiyun["derivation_required"] is False
    assert "subtraction or preserved silence" in qiyun["possible_forms"]
    assert "without actual audition" in qiyun["evidence_boundary"]
    assert "do not add a scored question" in qiyun["workflow_contract"]
    assert "not whether the creative thought was insightful" in qiyun[
        "workflow_contract"
    ]
    assert "not required for reversible qiyun details" in guide["derivation"][
        "note"
    ]

    selected = mcp_server.get_music_constitution_clauses(
        ["C0.06", "C4.1.16"],
    )
    assert selected["ok"] is True
    assert [item["clause_id"] for item in selected["clauses"]] == [
        "C0.06",
        "C4.1.16",
    ]
    assert selected["full_document_injected"] is False
    english = mcp_server.get_music_constitution_clauses(["C8.3.01"], "en")
    assert english["ok"] is True
    assert english["constitution"]["content_sha256"] == hashlib.sha256(
        (
            ROOT
            / "docs"
            / "音乐创作参考笔记"
            / "天籁音乐宪法-v0.2.en.md"
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
                "clause_id": "C0.06",
                "role": "review_lens",
                "rationale": "Protect a seed that precedes its verbal reason.",
                "interpretation": "Do not delete it merely because its role is not yet named.",
            }
        ],
    )
    assert activated["ok"] is True
    assert activated["constitution_source"] == "official"


def test_v01_official_constitution_binding_fails_closed(workflow_mcp) -> None:
    mcp_server, _output = workflow_mcp
    _create_project(mcp_server, "legacy-constitution-project")
    created = _create_workflow(
        mcp_server,
        key="legacy-constitution-project",
    )
    workflow_id, revision = _identity(created)
    legacy_official_binding = {
        "document_id": "tianlai-music-constitution",
        "version": "0.1",
        "language": "zh-CN",
        "content_sha256": (
            "3c26f99806b2044b3fd45cbdc8ef12ff"
            "adf871d75dc119799881b0d992b75985"
        ),
    }
    rejected = mcp_server.activate_creative_workflow(
        "legacy-constitution-project",
        workflow_id,
        revision,
        _charter(),
        legacy_official_binding,
        [
            {
                "clause_id": "C0.02",
                "role": "review_lens",
                "rationale": "Exercise fail-closed official version binding.",
                "interpretation": "An obsolete official hash cannot activate as v0.2.",
            }
        ],
    )
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == (
        "creative_workflow.official_constitution_binding_mismatch"
    )


def test_orchestration_next_action_exposes_lightweight_qiyun_scan(
    workflow_mcp,
) -> None:
    mcp_server, _output = workflow_mcp
    _create_project(mcp_server, "qiyun-next-action-project")
    workflow = _activate(
        mcp_server,
        _create_workflow(mcp_server, key="qiyun-next-action-project"),
        key="qiyun-next-action-project",
    )
    workflow = _review(
        mcp_server,
        workflow,
        "intent",
        key="qiyun-next-action-project",
    )
    workflow = _review(
        mcp_server,
        workflow,
        "symbolic_structure",
        key="qiyun-next-action-project",
    )

    next_action = workflow["next_action"]
    assert next_action["operation"] == "record_workflow_review"
    assert next_action["suggested_arguments"]["phase"] == (
        "orchestration_performance"
    )
    qiyun_scan = next(
        item
        for item in next_action["prerequisites"]
        if item.get("step") == "qiyun_location_scan"
    )
    assert "zero_additions_is_a_valid_answer" in qiyun_scan["constraints"]
    assert (
        "no_quantity_quota_and_no_derivation_required_for_qiyun_details"
        in qiyun_scan["constraints"]
    )
    assert any("stay empty" in question for question in qiyun_scan["questions"])
    assert "existing_orchestration_performance_answer" in qiyun_scan["recording"]
    assert "separate_qiyun_question_or_ledger" in qiyun_scan["recording"]
    assert any(
        "cannot_prove_creative_thought" in constraint
        for constraint in qiyun_scan["constraints"]
    )


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
    assert history["history"]["verified_revision_count"] == 3

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


def test_composition_governance_defaults_on_and_can_be_explicitly_disabled(
    workflow_mcp,
) -> None:
    mcp_server, _output = workflow_mcp
    _create_project(mcp_server, "governance-default-project")

    governed = mcp_server.create_creative_workflow(
        "governance-default-project",
        "iterate",
    )
    assert governed["ok"], governed
    governed_state = governed["workflow"]["state"]
    assert governed_state["policy"]["composition_governance_profile"] == (
        "whole-work-derivation-and-bounded-amendment-v1"
    )
    assert "governance" in governed_state
    governed_id, governed_revision = _identity(governed)
    governed_active = mcp_server.activate_creative_workflow(
        "governance-default-project",
        governed_id,
        governed_revision,
        _charter(),
    )
    assert governed_active["ok"], governed_active
    assert governed_active["next_action"]["operation"] == (
        "record_workflow_composition_map"
    )

    legacy = mcp_server.create_creative_workflow(
        "governance-default-project",
        "iterate",
        composition_governance=False,
    )
    assert legacy["ok"], legacy
    legacy_state = legacy["workflow"]["state"]
    assert "composition_governance_profile" not in legacy_state["policy"]
    assert "governance" not in legacy_state
    legacy_id, legacy_revision = _identity(legacy)
    legacy_active = mcp_server.activate_creative_workflow(
        "governance-default-project",
        legacy_id,
        legacy_revision,
        _charter(),
    )
    assert legacy_active["ok"], legacy_active
    assert legacy_active["next_action"]["operation"] == "record_workflow_review"
    assert legacy_active["next_action"]["suggested_arguments"]["phase"] == "intent"
    legacy_active = _review(
        mcp_server,
        legacy_active,
        "intent",
        key="governance-default-project",
    )
    legacy_active = _review(
        mcp_server,
        legacy_active,
        "symbolic_structure",
        key="governance-default-project",
    )
    assert legacy_active["next_action"]["suggested_arguments"]["phase"] == (
        "orchestration_performance"
    )
    assert all(
        item.get("step") != "qiyun_location_scan"
        for item in legacy_active["next_action"].get("prerequisites", [])
    )

    guide = mcp_server.creative_workflow_guide()["guide"]
    assert guide["composition_governance"]["default_when_created_via_mcp"] is True
    assert guide["composition_governance"]["opt_out_parameter"] == (
        "composition_governance=false"
    )
    assert guide["composition_governance"]["raw_model_baseline"] == (
        "do_not_connect_the_mcp_server"
    )


def test_charter_amendment_preflight_is_bound_before_authoring_change(
    workflow_mcp,
) -> None:
    mcp_server, output = workflow_mcp
    _create_project(mcp_server)
    active = _review(
        mcp_server,
        _activate(mcp_server, _create_workflow(mcp_server)),
        "intent",
    )
    workflow_id, revision = _identity(active)
    review_id = active["workflow"]["state"]["iterations"][-1]["reviews"][-1][
        "review_id"
    ]
    evidence = mcp_server.record_workflow_evidence(
        "workflow-project",
        workflow_id,
        revision,
        "promise_conflict",
        "ending.contract_conflict",
        "declared_promise",
        "ending_contract",
        "report_only",
        "The current ending obligation conflicts with the discovered response.",
        "Whole-work review found a different necessary ending consequence.",
        "A bounded amendment may be more honest than rationalizing the conflict.",
        "high",
    )
    assert evidence["ok"], evidence
    evidence_item = evidence["workflow"]["state"]["iterations"][-1]["evidence"][-1]
    workflow_id, revision = _identity(evidence)
    inspected = mcp_server.inspect_workflow_composition(
        "workflow-project",
        workflow_id,
    )
    assert inspected["ok"], inspected
    assert inspected["kind"] == "tianlai.creative_workflow_mcp_result"
    ending_claim = next(
        item["claim_id"]
        for item in inspected["inspection"]["charter_claim_index"]["claims"]
        if item["field_path"] == ["ending_contract"]
    )
    proposal = {
        "summary": "Change only the ending obligation.",
        "why_score_revision_is_insufficient": (
            "The old ending standard itself rejects the newly discovered consequence."
        ),
        "why_bounded_exception_is_insufficient": (
            "An exception would preserve two contradictory ending standards."
        ),
        "expected_gain": "Let the ending answer the opening under one coherent rule.",
        "accepted_costs": ["Rebuild and review the complete sequence."],
        "replacement_constraints": ["Keep the opening contour invariant."],
        "failure_conditions": ["Reject if the contour becomes unrecognizable."],
        "basis_ids": [evidence_item["evidence_id"]],
        "operations": [
            {
                "op": "replace",
                "claim_id": ending_claim,
                "value": (
                    "The ending must transform the opening contour into a "
                    "necessary consequence."
                ),
            }
        ],
    }
    preflight = mcp_server.preflight_workflow_charter_amendment(
        "workflow-project",
        workflow_id,
        proposal,
    )
    assert preflight["ok"], preflight
    assert preflight["amendment_preflight"]["read_only"] is True
    assert preflight["amendment_preflight"]["active"] is False
    assert preflight["next_action"]["operation"] == (
        "commit_workflow_charter_amendment"
    )
    assert any(
        step["step"] == "decide_revision"
        for step in preflight["next_action"]["prerequisites"]
    )

    revision_pending = mcp_server.decide_workflow_iteration(
        "workflow-project",
        workflow_id,
        revision,
        "revise",
        "Test the bounded ending amendment.",
        "The conflict is evidenced and the reconstruction cost will be explicit.",
        "report_only",
        evidence_ids=[evidence_item["evidence_id"]],
        review_ids=[review_id],
        evidence_dispositions=[
            {
                "evidence_id": evidence_item["evidence_id"],
                "disposition": "revision_target",
                "rationale": "The bounded amendment directly targets this conflict.",
                "basis_ids": [review_id],
            }
        ],
        expected_audible_change="The ending answers the opening under one rule.",
        revision_scope={
            "change_scale": "bounded",
            "documents": ["score"],
            "allowed_document_paths": {"score": ["/tail_seconds"]},
            "score": {
                "part_ids": [],
                "event_ids": [],
                "bar_ranges": [],
                "allowed_note_fields": [],
                "allow_event_additions": False,
                "allow_event_deletions": False,
                "allow_reordering": False,
            },
            "whole_work_cost": None,
        },
        withdrawal_condition=(
            "Withdraw the challenger if the ending still does not answer the opening."
        ),
    )
    assert revision_pending["ok"], revision_pending
    assert revision_pending["next_action"]["operation"] == (
        "commit_workflow_charter_amendment"
    )
    assert revision_pending["next_action"]["prerequisites"][1]["operation"] == (
        "preflight_workflow_charter_amendment"
    )
    workflow_id, revision = _identity(revision_pending)
    pending_preflight = mcp_server.preflight_workflow_charter_amendment(
        "workflow-project",
        workflow_id,
        proposal,
        revision,
    )
    assert pending_preflight["ok"], pending_preflight
    commit_arguments = pending_preflight["next_action"]["suggested_arguments"]
    assert commit_arguments["expected_revision"] == revision
    assert all(
        step["step"] != "decide_revision"
        for step in pending_preflight["next_action"]["prerequisites"]
    )
    wrong_cost = dict(commit_arguments["cost_acknowledgement"])
    wrong_cost["operation_count"] += 1
    rejected = mcp_server.commit_workflow_charter_amendment(
        "workflow-project",
        workflow_id,
        revision,
        commit_arguments["proposal"],
        commit_arguments["expected_preflight_sha256"],
        wrong_cost,
    )
    assert rejected["ok"] is False
    assert rejected["error"]["code"] == (
        "creative_workflow.charter_amendment_cost_not_acknowledged"
    )
    committed = mcp_server.commit_workflow_charter_amendment(
        "workflow-project",
        workflow_id,
        revision,
        commit_arguments["proposal"],
        commit_arguments["expected_preflight_sha256"],
        commit_arguments["cost_acknowledgement"],
    )
    assert committed["ok"], committed
    amendment = committed["workflow"]["state"]["governance"]["amendments"][-1]
    assert amendment["effective_from_iteration"] == 2
    assert amendment["entry"]["proposal"]["operations"][0]["claim_id"] == ending_claim
    assert committed["next_action"]["operation"] == "get_authoring_snapshot"
    assert "cost_acknowledged" in committed["next_action"]["reason"]
    history = mcp_server.verify_creative_workflow_history(
        "workflow-project",
        workflow_id,
    )
    assert history["ok"], history
    assert history["history"]["complete"] is True
    for result in (
        inspected,
        preflight,
        pending_preflight,
        rejected,
        committed,
        history,
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
    current_iteration = exception["workflow"]["state"]["iterations"][-1]
    review_ids = [item["review_id"] for item in current_iteration["reviews"]]
    exception_item = current_iteration["exceptions"][-1]
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
        exception_ids=[exception_item["exception_id"]],
        review_ids=review_ids,
        evidence_dispositions=[
            {
                "evidence_id": evidence_item["evidence_id"],
                "disposition": "revision_target",
                "rationale": (
                    "The bounded recurrence directly tests this recorded risk."
                ),
                "basis_ids": [review_ids[-1]],
            }
        ],
        expected_audible_change="The opening contour returns once in a new register.",
        revision_scope={
            "change_scale": "bounded",
            "documents": ["score"],
            "allowed_document_paths": {"score": ["/tail_seconds"]},
            "score": {
                "part_ids": [],
                "event_ids": [],
                "bar_ranges": [],
                "allowed_note_fields": [],
                "allow_event_additions": False,
                "allow_event_deletions": False,
                "allow_reordering": False,
            },
            "whole_work_cost": None,
        },
        withdrawal_condition=(
            "Return to the baseline if the recurrence is not established by review."
        ),
    )
    assert revision_pending["ok"], revision_pending
    assert revision_pending["workflow"]["state"]["status"] == "revision_pending"
    revision_handoff = revision_pending["next_action"]
    assert revision_handoff["operation"] == "commit_workflow_charter_amendment"
    assert revision_handoff["suggested_arguments"] == {
        "project_key": "workflow-project",
        "workflow_id": workflow_id,
        "expected_revision": _identity(revision_pending)[1],
    }
    assert [
        step["step"] for step in revision_handoff["prerequisites"]
    ] == [
        "charter_change_gate",
        "preflight_before_authoring",
        "commit_exact_preflight",
    ]
    assert revision_handoff["continuation"]["score_only_path"] == {
        "operation": "get_authoring_snapshot",
        "arguments": {
            "project_key": "workflow-project",
            "revision": authoring_revision,
        },
        "constraint": "effective_charter_must_remain_unchanged",
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
    prior_contract = revision_pending["workflow"]["state"]["iterations"][-1][
        "decision"
    ]["revision_contract"]
    early_withdrawal = second_iteration["next_action"]["continuation"][
        "early_withdrawal"
    ]
    assert early_withdrawal["withdrawal_condition"] == (
        "Return to the baseline if the recurrence is not established by review."
    )
    assert early_withdrawal["contract_sha256"] == prior_contract[
        "contract_sha256"
    ]
    assert early_withdrawal["baseline_target_iteration_number"] == 1
    assert early_withdrawal["candidate_id"] is None
    assert early_withdrawal["claim_scope"] == (
        "pre_render_contextual_withdrawal_not_audio_audition"
    )
    assert early_withdrawal["review_requirement"] == (
        "record_one_current_report_only_review_candidate_id_is_null"
    )
    rollback_sources = early_withdrawal["if_triggered"]["rollback"][
        "argument_sources"
    ]
    assert rollback_sources[
        "prior_revision_assessment.contract_sha256"
    ] == prior_contract["contract_sha256"]
    assert rollback_sources[
        "prior_revision_assessment.outcome_options"
    ] == ["retain_baseline", "inconclusive"]
    assert rollback_sources["prior_revision_assessment.basis_ids"] == (
        "record_one_current_report_only_review_then_use_its_review_id"
    )
    assert "without_claiming_the_revision_was_heard" in early_withdrawal[
        "if_triggered"
    ]["terminate"]["effect"]
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        second_iteration = _review(mcp_server, second_iteration, phase)
    workflow_id, revision = _identity(second_iteration)
    challenger = mcp_server.render_workflow_candidate(
        "workflow-project",
        workflow_id,
        revision,
    )
    assert challenger["ok"], challenger
    second_iteration = _review(mcp_server, challenger, "render_report")
    assessment_basis = second_iteration["workflow"]["state"]["iterations"][-1][
        "reviews"
    ][-1]["review_id"]
    workflow_id, revision = _identity(second_iteration)
    rolled_back = mcp_server.rollback_creative_workflow(
        "workflow-project",
        workflow_id,
        revision,
        1,
        "Return to the stronger immutable candidate.",
        "The bounded experiment did not justify replacing it.",
        "report_only",
        prior_revision_assessment={
            "contract_sha256": prior_contract["contract_sha256"],
            "outcome": "retain_baseline",
            "rationale": (
                "The selected review did not establish the declared recurrence."
            ),
            "basis_ids": [assessment_basis],
        },
    )
    assert rolled_back["ok"], rolled_back
    assert len(rolled_back["workflow"]["state"]["iterations"]) == 3

    for phase in ("intent", "symbolic_structure", "orchestration_performance", "render_report"):
        rolled_back = _review(mcp_server, rolled_back, phase)
    accepted_iteration = rolled_back["workflow"]["state"]["iterations"][-1]
    accept_review_ids = [
        item["review_id"] for item in accepted_iteration["reviews"]
    ]
    accept_evidence_ids = [
        item["evidence_id"] for item in accepted_iteration["evidence"]
    ]
    workflow_id, revision = _identity(rolled_back)
    accepted = mcp_server.decide_workflow_iteration(
        "workflow-project",
        workflow_id,
        revision,
        "accept",
        "Accept this candidate under the frozen charter.",
        "All required review phases are recorded; this is contextual, not a quality guarantee.",
        "report_only",
        evidence_ids=accept_evidence_ids,
        review_ids=accept_review_ids,
        evidence_dispositions=[
            {
                "evidence_id": evidence_id,
                "disposition": "accepted_risk",
                "rationale": "The final authority knowingly accepts this bounded risk.",
                "basis_ids": accept_review_ids,
            }
            for evidence_id in accept_evidence_ids
        ],
        charter_settlement=[
            {
                "target": target,
                "status": "kept",
                "rationale": "The accepted candidate keeps this charter promise.",
                "basis_ids": accept_review_ids,
                "event_ids": ["event-1"],
            }
            for target in (
                "one_sentence_promise",
                "identity_kernel.invariants[0]",
                "ending_contract",
            )
        ],
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


def test_post_rollback_revision_reads_baseline_and_saves_against_causal_head(
    workflow_mcp,
) -> None:
    mcp_server, _output = workflow_mcp
    project = _create_project(mcp_server)
    initial = mcp_server.get_authoring_snapshot("workflow-project")["snapshot"]
    saved = mcp_server.save_authoring_project(
        "workflow-project",
        project["project"]["revision"],
        _renderable_documents(initial),
    )
    assert saved["ok"], saved
    baseline_authoring_revision = saved["project"]["revision"]

    created = mcp_server.create_creative_workflow(
        "workflow-project",
        "iterate",
        baseline_authoring_revision,
        composition_governance=False,
    )
    active = _activate(mcp_server, created)
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        active = _review(mcp_server, active, phase)
    workflow_id, revision = _identity(active)
    baseline = mcp_server.render_workflow_candidate(
        "workflow-project", workflow_id, revision
    )
    assert baseline["ok"], baseline
    baseline = _review(mcp_server, baseline, "render_report")
    workflow_id, revision = _identity(baseline)
    evidence = mcp_server.record_workflow_evidence(
        "workflow-project",
        workflow_id,
        revision,
        "aesthetic_risk",
        "structure.first_bounded_test",
        "diagnostic_hypothesis",
        "symbolic self-review",
        "report_only",
        "The current recurrence may be too weak.",
        "The report does not establish the intended return.",
        "A bounded metadata edit tests the revision path.",
        "medium",
    )
    assert evidence["ok"], evidence
    current = evidence["workflow"]["state"]["iterations"][-1]
    evidence_id = current["evidence"][-1]["evidence_id"]
    review_ids = [item["review_id"] for item in current["reviews"]]
    workflow_id, revision = _identity(evidence)
    first_revision = mcp_server.decide_workflow_iteration(
        "workflow-project",
        workflow_id,
        revision,
        "revise",
        "Run the first bounded revision.",
        "The selected report makes the experiment falsifiable.",
        "report_only",
        evidence_ids=[evidence_id],
        review_ids=review_ids,
        evidence_dispositions=[
            {
                "evidence_id": evidence_id,
                "disposition": "revision_target",
                "rationale": "The bounded edit directly tests the recorded risk.",
                "basis_ids": [review_ids[-1]],
            }
        ],
        expected_audible_change="The recurrence becomes explicit once.",
        revision_scope={
            "change_scale": "bounded",
            "documents": ["score"],
            "allowed_document_paths": {"score": ["/tail_seconds"]},
            "score": {
                "part_ids": [],
                "event_ids": [],
                "bar_ranges": [],
                "allowed_note_fields": [],
                "allow_event_additions": False,
                "allow_event_deletions": False,
                "allow_reordering": False,
            },
            "whole_work_cost": None,
        },
        withdrawal_condition="Retain the baseline if the report does not establish the return.",
    )
    assert first_revision["ok"], first_revision
    baseline_content = mcp_server.get_authoring_snapshot(
        "workflow-project", baseline_authoring_revision
    )["snapshot"]
    first_documents = copy.deepcopy(baseline_content["documents"])
    first_documents["score"]["tail_seconds"] = 0.06
    first_saved = mcp_server.save_authoring_project(
        "workflow-project", baseline_authoring_revision, first_documents
    )
    assert first_saved["ok"], first_saved
    challenger_authoring_revision = first_saved["project"]["revision"]
    workflow_id, revision = _identity(first_revision)
    challenger_iteration = mcp_server.record_workflow_authoring_revision(
        "workflow-project",
        workflow_id,
        revision,
        challenger_authoring_revision,
    )
    assert challenger_iteration["ok"], challenger_iteration
    for phase in ("intent", "symbolic_structure", "orchestration_performance"):
        challenger_iteration = _review(mcp_server, challenger_iteration, phase)
    workflow_id, revision = _identity(challenger_iteration)
    challenger = mcp_server.render_workflow_candidate(
        "workflow-project", workflow_id, revision
    )
    assert challenger["ok"], challenger
    challenger = _review(mcp_server, challenger, "render_report")
    first_contract = first_revision["workflow"]["state"]["iterations"][-1][
        "decision"
    ]["revision_contract"]
    assessment_review_id = challenger["workflow"]["state"]["iterations"][-1][
        "reviews"
    ][-1]["review_id"]
    workflow_id, revision = _identity(challenger)
    rolled_back = mcp_server.rollback_creative_workflow(
        "workflow-project",
        workflow_id,
        revision,
        1,
        "Retain the frozen baseline.",
        "The first bounded revision did not establish its declared change.",
        "report_only",
        prior_revision_assessment={
            "contract_sha256": first_contract["contract_sha256"],
            "outcome": "retain_baseline",
            "rationale": "The cited current review did not establish the return.",
            "basis_ids": [assessment_review_id],
        },
    )
    assert rolled_back["ok"], rolled_back

    for phase in ("intent", "symbolic_structure", "orchestration_performance", "render_report"):
        rolled_back = _review(mcp_server, rolled_back, phase)
    workflow_id, revision = _identity(rolled_back)
    evidence = mcp_server.record_workflow_evidence(
        "workflow-project",
        workflow_id,
        revision,
        "aesthetic_risk",
        "structure.second_bounded_test",
        "diagnostic_hypothesis",
        "symbolic self-review",
        "report_only",
        "A second bounded test remains possible.",
        "The retained baseline supplies the content source.",
        "Save must still extend the current causal head.",
        "medium",
    )
    assert evidence["ok"], evidence
    current = evidence["workflow"]["state"]["iterations"][-1]
    evidence_id = current["evidence"][-1]["evidence_id"]
    review_ids = [item["review_id"] for item in current["reviews"]]
    workflow_id, revision = _identity(evidence)
    second_revision = mcp_server.decide_workflow_iteration(
        "workflow-project",
        workflow_id,
        revision,
        "revise",
        "Run a second bounded revision from the retained baseline.",
        "The contract must separate content provenance from causal save order.",
        "report_only",
        evidence_ids=[evidence_id],
        review_ids=review_ids,
        evidence_dispositions=[
            {
                "evidence_id": evidence_id,
                "disposition": "revision_target",
                "rationale": "The second bounded edit directly tests this risk.",
                "basis_ids": [review_ids[-1]],
            }
        ],
        expected_audible_change="The retained baseline receives one bounded change.",
        revision_scope={
            "change_scale": "bounded",
            "documents": ["score"],
            "allowed_document_paths": {"score": ["/tail_seconds"]},
            "score": {
                "part_ids": [],
                "event_ids": [],
                "bar_ranges": [],
                "allowed_note_fields": [],
                "allow_event_additions": False,
                "allow_event_deletions": False,
                "allow_reordering": False,
            },
            "whole_work_cost": None,
        },
        withdrawal_condition="Retain the baseline if the second change remains unestablished.",
    )
    assert second_revision["ok"], second_revision
    handoff = second_revision["next_action"]
    assert handoff["operation"] == "get_authoring_snapshot"
    assert handoff["continuation"]["content_source_revision"] == (
        baseline_authoring_revision
    )
    assert handoff["continuation"]["causal_save_parent_revision"] == (
        challenger_authoring_revision
    )
    assert baseline_authoring_revision != challenger_authoring_revision
    steps = {item["step"]: item for item in handoff["prerequisites"]}
    assert steps["read_content_baseline"]["arguments"]["revision"] == (
        baseline_authoring_revision
    )
    assert steps["verify_causal_save_parent"]["arguments"] == {
        "project_key": "workflow-project"
    }
    assert steps["edit"]["input_from"] == (
        "read_content_baseline.snapshot.documents"
    )
    assert steps["save"]["arguments"]["expected_revision"] == (
        challenger_authoring_revision
    )
    assert steps["save"]["argument_sources"] == {
        "documents": "edited_complete_authoring_documents"
    }

    current_head = mcp_server.get_authoring_snapshot("workflow-project")
    assert current_head["snapshot"]["project"]["revision"] == (
        challenger_authoring_revision
    )
    content = mcp_server.get_authoring_snapshot(
        "workflow-project", baseline_authoring_revision
    )["snapshot"]
    second_documents = copy.deepcopy(content["documents"])
    second_documents["score"]["tail_seconds"] = 0.07
    second_saved = mcp_server.save_authoring_project(
        "workflow-project",
        steps["save"]["arguments"]["expected_revision"],
        second_documents,
    )
    assert second_saved["ok"], second_saved
    workflow_id, revision = _identity(second_revision)
    bound = mcp_server.record_workflow_authoring_revision(
        "workflow-project",
        workflow_id,
        revision,
        second_saved["project"]["revision"],
    )
    assert bound["ok"], bound
    assert bound["workflow"]["state"]["iterations"][-1]["anchor"][
        "authoring_revision"
    ] == second_saved["project"]["revision"]


def test_record_workflow_derivation_bounds_necessity_claims(workflow_mcp) -> None:
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

    guide = mcp_server.creative_workflow_guide()
    derivation_guide = guide["guide"]["derivation"]
    assert derivation_guide["excluded_alternatives_required"] is True
    assert "established_material" in derivation_guide["premise_kinds"]

    active = _activate(
        mcp_server,
        _create_workflow(mcp_server, revision=authoring_revision),
    )
    active = _review(mcp_server, active, "intent")
    workflow_id, revision = _identity(active)
    assert "record_workflow_derivation" in active["next_action"]["alternatives"]
    inspected = mcp_server.inspect_workflow_composition(
        "workflow-project",
        workflow_id,
    )
    assert inspected["ok"], inspected
    context = inspected["inspection"]
    charter_claim_ids = [
        next(
            item["claim_id"]
            for item in context["charter_claim_index"]["claims"]
            if item["field_path"] == ["ending_contract"]
        )
    ]
    composition_map_node_ids = [context["composition_map"]["nodes"][0]["node_id"]]
    question_ids = [
        answer["question_id"]
        for answer in active["workflow"]["state"]["iterations"][-1]["reviews"][-1][
            "question_answers"
        ]
    ]

    unrelated = mcp_server.record_workflow_derivation(
        "workflow-project",
        workflow_id,
        revision,
        "An unrelated charter premise must not satisfy the governance bindings.",
        [
            {
                "kind": "declared_promise",
                "reference": "one_sentence_promise",
                "event_ids": [],
                "artifact_sha256": None,
                "artifact_role": None,
            }
        ],
        [
            {
                "alternative": "Treat three unrelated IDs as a derivation.",
                "failure": "The premise, map dependency and answered question do not form one argument.",
                "premise_indexes": [0],
            }
        ],
        ["event-1"],
        charter_claim_ids=charter_claim_ids,
        composition_map_node_ids=composition_map_node_ids,
        question_ids=question_ids,
    )
    assert unrelated["ok"] is False
    assert unrelated["error"]["code"] == (
        "creative_workflow.derivation_governance_reference_scope_mismatch"
    )

    recorded = mcp_server.record_workflow_derivation(
        "workflow-project",
        workflow_id,
        revision,
        "The single event must stand alone because the ending contract requires a consequence.",
        [
            {
                "kind": "declared_promise",
                "reference": "ending_contract",
                "event_ids": [],
                "artifact_sha256": None,
                "artifact_role": None,
            },
        ],
        [
            {
                "alternative": "Add a second voice before the climax.",
                "failure": "It would spend the climax privilege early and break the promise.",
                "premise_indexes": [0],
            }
        ],
        ["event-1"],
        charter_claim_ids=charter_claim_ids,
        composition_map_node_ids=composition_map_node_ids,
        question_ids=question_ids,
    )
    assert recorded["ok"], recorded
    derivations = recorded["workflow"]["state"]["iterations"][-1]["derivations"]
    assert len(derivations) == 1
    assert derivations[0]["anchor"]["authoring_revision"] == authoring_revision
    assert derivations[0]["anchor"]["score_sha256"]
    assert recorded["workflow"]["state"]["usage"]["derivations"] == 1

    missing_event = mcp_server.record_workflow_derivation(
        "workflow-project",
        workflow_id,
        recorded["workflow"]["workflow"]["revision"],
        "A claim anchored to a note that does not exist must fail.",
        [
            {
                "kind": "declared_promise",
                "reference": "one_sentence_promise",
                "event_ids": [],
                "artifact_sha256": None,
                "artifact_role": None,
            }
        ],
        [
            {
                "alternative": "Keep the phantom anchor.",
                "failure": "Referential integrity must hold.",
                "premise_indexes": [0],
            }
        ],
        ["event-999"],
        charter_claim_ids=charter_claim_ids,
        composition_map_node_ids=composition_map_node_ids,
        question_ids=question_ids,
    )
    assert missing_event["ok"] is False
    assert missing_event["error"]["code"] == (
        "creative_workflow.derivation_event_not_found"
    )

    no_alternatives = mcp_server.record_workflow_derivation(
        "workflow-project",
        workflow_id,
        recorded["workflow"]["workflow"]["revision"],
        "A necessity claim without excluded alternatives is only a preference.",
        [
            {
                "kind": "declared_promise",
                "reference": "one_sentence_promise",
                "event_ids": [],
                "artifact_sha256": None,
                "artifact_role": None,
            }
        ],
        [],
        ["event-1"],
        charter_claim_ids=charter_claim_ids,
        composition_map_node_ids=composition_map_node_ids,
        question_ids=question_ids,
    )
    assert no_alternatives["ok"] is False
    assert no_alternatives["error"]["code"] == (
        "creative_workflow.derivation_alternatives_required"
    )

    history = mcp_server.verify_creative_workflow_history(
        "workflow-project", workflow_id
    )
    assert history["ok"], history
    assert history["history"]["complete"] is True
    for result in (recorded, missing_event, no_alternatives, history):
        _assert_no_local_path(result, output.parent.parent)


def test_settlement_and_fork_boundaries_through_mcp(workflow_mcp) -> None:
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

    guide = mcp_server.creative_workflow_guide()
    decisions_guide = guide["guide"]["decisions"]
    assert any(
        "charter promise settled" in item
        for item in decisions_guide["accept_requires"]
    )
    assert "charter_settlement" in decisions_guide["claim_lifecycle"]
    fork_guide = decisions_guide["fork"]
    assert "whole" in fork_guide["purpose"]

    active = _activate(
        mcp_server,
        _create_workflow(mcp_server, revision=authoring_revision),
    )
    workflow_id, revision = _identity(active)
    assert "record_workflow_fork" in active["next_action"]["alternatives"]

    bad_basis = mcp_server.decide_workflow_iteration(
        "workflow-project",
        workflow_id,
        revision,
        "stop",
        "Stop with an unsettled basis.",
        "Settlement items must cite selected claim ids.",
        "report_only",
        charter_settlement=[
            {
                "target": "one_sentence_promise",
                "status": "kept",
                "rationale": "No basis is selected.",
                "basis_ids": [],
                "event_ids": [],
            }
        ],
    )
    assert bad_basis["ok"] is False
    assert bad_basis["error"]["code"] == (
        "creative_workflow.invalid_charter_settlement_basis"
    )

    branches = [
        {
            "candidate": {
                "work_id": "work-a",
                "candidate_id": "candidate-a",
                "manifest_sha256": "1" * 64,
            },
            "stance": "The motive stays low.",
            "derivation_ids": [],
        },
        {
            "candidate": {
                "work_id": "work-a",
                "candidate_id": "candidate-b",
                "manifest_sha256": "2" * 64,
            },
            "stance": "The motive rises.",
            "derivation_ids": [],
        },
    ]
    empty_anchor = mcp_server.record_workflow_fork(
        "workflow-project",
        workflow_id,
        revision,
        branches,
        [0],
    )
    assert empty_anchor["ok"] is False
    assert empty_anchor["error"]["code"] == (
        "creative_workflow.fork_anchor_empty"
    )

    partial_range = mcp_server.record_workflow_fork(
        "workflow-project",
        workflow_id,
        revision,
        branches,
        [0],
        start_bar=1,
    )
    assert partial_range["ok"] is False
    assert partial_range["error"]["code"] == (
        "creative_workflow.invalid_fork_bar_range"
    )

    for result in (bad_basis, empty_anchor, partial_range):
        _assert_no_local_path(result, output.parent.parent)
