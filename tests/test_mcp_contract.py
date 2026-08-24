"""Public MCP tool contracts advertised and enforced by the real stdio server."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
_HAS_MCP = importlib.util.find_spec("mcp") is not None
_UNEXPECTED_FIELD = "__unexpected_tianlai_contract__"

_COMPOSITION_MAP_MINIMAL = {
    "kind": "tianlai.composition_map",
    "schema_version": 1,
    "nodes": [
        {
            "node_id": "whole-work",
            "label": "Whole work",
            "function": "Carry the current work through one complete sequence.",
        }
    ],
}
_AMENDMENT_PROPOSAL_MINIMAL = {
    "summary": "Replace one bounded charter claim.",
    "why_score_revision_is_insufficient": "The governing claim itself is at issue.",
    "why_bounded_exception_is_insufficient": "An exception would preserve a contradiction.",
    "expected_gain": "Restore one coherent obligation.",
    "accepted_costs": ["Revalidate the affected sequence."],
    "replacement_constraints": ["Preserve unaffected charter claims."],
    "failure_conditions": ["Reject if the work loses its identity."],
    "basis_ids": ["evidence-" + "0" * 20],
    "operations": [
        {
            "op": "replace",
            "claim_id": "claim-" + "0" * 64,
            "value": "A bounded replacement claim.",
        }
    ],
}
_AMENDMENT_COST_MINIMAL = {
    "preflight_sha256": "0" * 64,
    "minimum_reconstruction_scope": "bounded",
    "operation_count": 1,
    "affected_claim_count": 1,
    "affected_root_field_count": 1,
    "composition_dependencies_to_revalidate": 0,
    "derivations_to_revalidate": 0,
    "reviews_to_revalidate": 0,
    "evidence_interpretations_to_revalidate": 0,
    "observations_preserved": 0,
    "hard_failures_preserved": 0,
    "whole_work_consistency_review_required": True,
}


def _contract(
    *,
    properties: set[str],
    required: set[str],
    defaults: dict[str, object],
    minimal: dict[str, object],
) -> dict[str, object]:
    return {
        "properties": properties,
        "required": required,
        "defaults": defaults,
        "minimal": minimal,
    }


TOOL_CONTRACTS = {
    "list_instruments": _contract(
        properties={
            "trusted_only",
            "pitched_only",
            "instrument_scope",
            "category",
            "routing_class",
            "articulation",
            "pitch_mode",
            "query",
            "detail_level",
            "offset",
            "limit",
        },
        required=set(),
        defaults={
            "trusted_only": None,
            "pitched_only": False,
            "instrument_scope": None,
            "category": None,
            "routing_class": None,
            "articulation": None,
            "pitch_mode": None,
            "query": None,
            "detail_level": "summary",
            "offset": 0,
            "limit": 32,
        },
        minimal={},
    ),
    "diagnose_runtime": _contract(
        properties={"check_level", "max_issues"},
        required=set(),
        defaults={"check_level": "quick", "max_issues": 32},
        minimal={},
    ),
    "plan_resource_restore": _contract(
        properties={"instrument_ids", "family_ids", "groups", "max_items"},
        required=set(),
        defaults={
            "instrument_ids": None,
            "family_ids": None,
            "groups": None,
            "max_items": 64,
        },
        minimal={},
    ),
    "score_and_roster_format": _contract(
        properties=set(),
        required=set(),
        defaults={},
        minimal={},
    ),
    "import_midi": _contract(
        properties={"midi_path"},
        required={"midi_path"},
        defaults={},
        minimal={"midi_path": "contract.mid"},
    ),
    "import_musicxml": _contract(
        properties={"musicxml_path"},
        required={"musicxml_path"},
        defaults={},
        minimal={"musicxml_path": "contract.musicxml"},
    ),
    "import_score_project": _contract(
        properties={
            "source_path",
            "trusted_only",
            "candidate_limit",
            "instrument_scope",
        },
        required={"source_path"},
        defaults={
            "trusted_only": None,
            "candidate_limit": 8,
            "instrument_scope": None,
        },
        minimal={"source_path": "contract.musicxml"},
    ),
    "confirm_roster": _contract(
        properties={
            "score",
            "roster_draft",
            "assignments",
            "trusted_only",
            "name",
            "collaboration",
            "instrument_scope",
        },
        required={"score", "roster_draft", "assignments"},
        defaults={
            "trusted_only": None,
            "name": None,
            "collaboration": None,
            "instrument_scope": None,
        },
        minimal={"score": {}, "roster_draft": {}, "assignments": []},
    ),
    "upgrade_score": _contract(
        properties={"score"},
        required={"score"},
        defaults={},
        minimal={"score": {}},
    ),
    "get_score_slice": _contract(
        properties={"score", "query"},
        required={"score", "query"},
        defaults={},
        minimal={"score": {}, "query": {}},
    ),
    "patch_score": _contract(
        properties={"score", "patch"},
        required={"score", "patch"},
        defaults={},
        minimal={"score": {}, "patch": {}},
    ),
    "compare_score_versions": _contract(
        properties={"before", "after", "max_changes"},
        required={"before", "after"},
        defaults={"max_changes": 256},
        minimal={"before": {}, "after": {}},
    ),
    "validate_project": _contract(
        properties={
            "score",
            "roster",
            "expression",
            "seed",
            "range_mode",
            "trusted_only",
            "max_issues",
            "render_profile",
            "normalize_peak_db",
            "hall",
            "master_gain_db",
            "space_config",
            "collaboration_mode",
            "write_stems",
            "use_stem_cache",
            "refresh_stem_cache",
            "instrument_scope",
        },
        required={"score", "roster"},
        defaults={
            "expression": None,
            "seed": None,
            "range_mode": None,
            "trusted_only": None,
            "max_issues": 64,
            "render_profile": None,
            "normalize_peak_db": None,
            "hall": None,
            "master_gain_db": None,
            "space_config": None,
            "collaboration_mode": None,
            "write_stems": None,
            "use_stem_cache": None,
            "refresh_stem_cache": None,
            "instrument_scope": None,
        },
        minimal={"score": {}, "roster": {}},
    ),
    "check_project_readiness": _contract(
        properties={
            "score",
            "roster",
            "expression",
            "seed",
            "range_mode",
            "trusted_only",
            "max_issues",
            "verify_references",
            "render_profile",
            "normalize_peak_db",
            "hall",
            "master_gain_db",
            "space_config",
            "collaboration_mode",
            "write_stems",
            "use_stem_cache",
            "refresh_stem_cache",
            "instrument_scope",
        },
        required={"score", "roster"},
        defaults={
            "expression": None,
            "seed": None,
            "range_mode": None,
            "trusted_only": None,
            "max_issues": 64,
            "verify_references": True,
            "render_profile": None,
            "normalize_peak_db": None,
            "hall": None,
            "master_gain_db": None,
            "space_config": None,
            "collaboration_mode": None,
            "write_stems": None,
            "use_stem_cache": None,
            "refresh_stem_cache": None,
            "instrument_scope": None,
        },
        minimal={"score": {}, "roster": {}},
    ),
    "locate": _contract(
        properties={
            "score",
            "roster",
            "at_seconds",
            "before_seconds",
            "after_seconds",
            "part_ids",
            "expression",
            "seed",
            "range_mode",
            "trusted_only",
            "max_events",
            "instrument_scope",
        },
        required={"score", "roster", "at_seconds"},
        defaults={
            "before_seconds": 2.0,
            "after_seconds": 2.0,
            "part_ids": None,
            "expression": "ensemble",
            "seed": 0,
            "range_mode": "compatibility",
            "trusted_only": None,
            "max_events": 64,
            "instrument_scope": None,
        },
        minimal={"score": {}, "roster": {}, "at_seconds": 0.0},
    ),
    "locate_rendered_candidate": _contract(
        properties={
            "candidate_directory",
            "at_seconds",
            "tail_lookback_seconds",
            "upcoming_seconds",
            "max_events",
        },
        required={"candidate_directory", "at_seconds"},
        defaults={
            "tail_lookback_seconds": 5.0,
            "upcoming_seconds": 2.0,
            "max_events": 128,
        },
        minimal={"candidate_directory": "contract-candidate", "at_seconds": 0.0},
    ),
    "compare_rendered_candidates": _contract(
        properties={
            "before_candidate_directory",
            "after_candidate_directory",
            "max_changes",
        },
        required={
            "before_candidate_directory",
            "after_candidate_directory",
        },
        defaults={"max_changes": 256},
        minimal={
            "before_candidate_directory": "contract-before",
            "after_candidate_directory": "contract-after",
        },
    ),
    "create_authoring_project": _contract(
        properties={"project_key", "title"},
        required={"project_key", "title"},
        defaults={},
        minimal={"project_key": "contract-project", "title": "Contract"},
    ),
    "open_authoring_project": _contract(
        properties={"project_key", "revision"},
        required={"project_key"},
        defaults={"revision": None},
        minimal={"project_key": "contract-project"},
    ),
    "get_authoring_snapshot": _contract(
        properties={"project_key", "revision"},
        required={"project_key"},
        defaults={"revision": None},
        minimal={"project_key": "contract-project"},
    ),
    "save_authoring_project": _contract(
        properties={"project_key", "expected_revision", "documents"},
        required={"project_key", "expected_revision", "documents"},
        defaults={},
        minimal={
            "project_key": "contract-project",
            "expected_revision": "0" * 64,
            "documents": {},
        },
    ),
    "check_authoring_readiness": _contract(
        properties={"project_key", "revision"},
        required={"project_key"},
        defaults={"revision": None},
        minimal={"project_key": "contract-project"},
    ),
    "render_authoring_revision": _contract(
        properties={"project_key", "expected_revision"},
        required={"project_key", "expected_revision"},
        defaults={},
        minimal={
            "project_key": "contract-project",
            "expected_revision": "0" * 64,
        },
    ),
    "inspect_authoring_candidate": _contract(
        properties={"project_key", "work_id", "candidate_id"},
        required={"project_key", "work_id", "candidate_id"},
        defaults={},
        minimal={
            "project_key": "contract-project",
            "work_id": "contract-work",
            "candidate_id": "contract-candidate",
        },
    ),
    "locate_authoring_candidate": _contract(
        properties={
            "project_key",
            "work_id",
            "candidate_id",
            "at_seconds",
            "tail_lookback_seconds",
            "upcoming_seconds",
            "max_events",
        },
        required={"project_key", "work_id", "candidate_id", "at_seconds"},
        defaults={
            "tail_lookback_seconds": 5.0,
            "upcoming_seconds": 2.0,
            "max_events": 128,
        },
        minimal={
            "project_key": "contract-project",
            "work_id": "contract-work",
            "candidate_id": "contract-candidate",
            "at_seconds": 0.0,
        },
    ),
    "compare_authoring_candidates": _contract(
        properties={
            "project_key",
            "before_work_id",
            "before_candidate_id",
            "after_work_id",
            "after_candidate_id",
            "max_changes",
        },
        required={
            "project_key",
            "before_work_id",
            "before_candidate_id",
            "after_work_id",
            "after_candidate_id",
        },
        defaults={"max_changes": 256},
        minimal={
            "project_key": "contract-project",
            "before_work_id": "contract-before-work",
            "before_candidate_id": "contract-before-candidate",
            "after_work_id": "contract-after-work",
            "after_candidate_id": "contract-after-candidate",
        },
    ),
    "creative_workflow_guide": _contract(
        properties=set(),
        required=set(),
        defaults={},
        minimal={},
    ),
    "get_music_constitution_clauses": _contract(
        properties={"clause_ids", "language"},
        required={"clause_ids"},
        defaults={"language": "zh-CN"},
        minimal={"clause_ids": ["C0.02"]},
    ),
    "create_creative_workflow": _contract(
        properties={
            "project_key",
            "mode",
            "base_authoring_revision",
            "budget",
            "composition_governance",
        },
        required={"project_key", "mode"},
        defaults={
            "base_authoring_revision": None,
            "budget": None,
            "composition_governance": True,
        },
        minimal={"project_key": "contract-project", "mode": "iterate"},
    ),
    "open_creative_workflow": _contract(
        properties={"project_key", "workflow_id", "revision"},
        required={"project_key", "workflow_id"},
        defaults={"revision": None},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
        },
    ),
    "verify_creative_workflow_history": _contract(
        properties={"project_key", "workflow_id", "maximum_revisions"},
        required={"project_key", "workflow_id"},
        defaults={"maximum_revisions": 4096},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
        },
    ),
    "activate_creative_workflow": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "work_charter",
            "constitution",
            "active_clauses",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "work_charter",
        },
        defaults={"constitution": None, "active_clauses": None},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "work_charter": {},
        },
    ),
    "inspect_workflow_composition": _contract(
        properties={
            "project_key",
            "workflow_id",
            "revision",
            "composition_map",
        },
        required={"project_key", "workflow_id"},
        defaults={"revision": None, "composition_map": None},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
        },
    ),
    "record_workflow_composition_map": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "composition_map",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "composition_map",
        },
        defaults={},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "composition_map": _COMPOSITION_MAP_MINIMAL,
        },
    ),
    "preflight_workflow_charter_amendment": _contract(
        properties={"project_key", "workflow_id", "proposal", "revision"},
        required={"project_key", "workflow_id", "proposal"},
        defaults={"revision": None},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "proposal": _AMENDMENT_PROPOSAL_MINIMAL,
        },
    ),
    "commit_workflow_charter_amendment": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "proposal",
            "expected_preflight_sha256",
            "cost_acknowledgement",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "proposal",
            "expected_preflight_sha256",
            "cost_acknowledgement",
        },
        defaults={},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "proposal": _AMENDMENT_PROPOSAL_MINIMAL,
            "expected_preflight_sha256": "0" * 64,
            "cost_acknowledgement": _AMENDMENT_COST_MINIMAL,
        },
    ),
    "record_workflow_review": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "phase",
            "perception_basis",
            "summary",
            "question_answers",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "phase",
            "perception_basis",
            "summary",
        },
        defaults={"question_answers": None},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "phase": "intent",
            "perception_basis": "report_only",
            "summary": "contract",
        },
    ),
    "record_workflow_evidence": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "category",
            "code",
            "basis_kind",
            "basis_reference",
            "perception_basis",
            "summary",
            "observation",
            "interpretation",
            "confidence",
            "scope",
            "artifact_sha256",
            "artifact_role",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "category",
            "code",
            "basis_kind",
            "basis_reference",
            "perception_basis",
            "summary",
            "observation",
            "interpretation",
            "confidence",
        },
        defaults={
            "scope": None,
            "artifact_sha256": None,
            "artifact_role": None,
        },
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "category": "aesthetic_risk",
            "code": "contract.risk",
            "basis_kind": "diagnostic_hypothesis",
            "basis_reference": "contract",
            "perception_basis": "report_only",
            "summary": "contract",
            "observation": "contract",
            "interpretation": "contract",
            "confidence": "low",
        },
    ),
    "record_verified_workflow_hard_failure": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "issue_code",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "issue_code",
        },
        defaults={},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "issue_code": "score.invalid",
        },
    ),
    "register_workflow_exception": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "target_type",
            "target_ref",
            "purpose",
            "scope",
            "higher_value",
            "cost",
            "recovery",
            "evidence_ids",
            "reusable",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "target_type",
            "target_ref",
            "purpose",
            "scope",
            "higher_value",
            "cost",
            "recovery",
            "evidence_ids",
        },
        defaults={"reusable": False},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "target_type": "work_charter",
            "target_ref": "identity_kernel",
            "purpose": "contract",
            "scope": "contract",
            "higher_value": "contract",
            "cost": "contract",
            "recovery": "contract",
            "evidence_ids": ["evidence-00000000000000000000"],
        },
    ),
    "record_workflow_derivation": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "claim",
            "premises",
            "excluded_alternatives",
            "event_ids",
            "part_ids",
            "start_seconds",
            "end_seconds",
            "start_bar",
            "start_beat",
            "end_bar",
            "end_beat",
            "clause_ids",
            "sacrificed_values",
            "charter_claim_ids",
            "composition_map_node_ids",
            "question_ids",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "claim",
            "premises",
            "excluded_alternatives",
        },
        defaults={
            "event_ids": None,
            "part_ids": None,
            "start_seconds": None,
            "end_seconds": None,
            "start_bar": None,
            "start_beat": None,
            "end_bar": None,
            "end_beat": None,
            "clause_ids": None,
            "sacrificed_values": None,
            "charter_claim_ids": None,
            "composition_map_node_ids": None,
            "question_ids": None,
        },
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "claim": "contract",
            "premises": [
                {
                    "kind": "declared_promise",
                    "reference": "one_sentence_promise",
                    "event_ids": [],
                    "artifact_sha256": None,
                    "artifact_role": None,
                }
            ],
            "excluded_alternatives": [
                {
                    "alternative": "contract",
                    "failure": "contract",
                    "premise_indexes": [0],
                }
            ],
        },
    ),
    "record_workflow_fork": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "branches",
            "invariant_indexes",
            "event_ids",
            "part_ids",
            "start_bar",
            "start_beat",
            "end_bar",
            "end_beat",
            "note",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "branches",
            "invariant_indexes",
        },
        defaults={
            "event_ids": None,
            "part_ids": None,
            "start_bar": None,
            "start_beat": None,
            "end_bar": None,
            "end_beat": None,
            "note": None,
        },
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "branches": [
                {
                    "candidate": {
                        "work_id": "contract-work",
                        "candidate_id": "contract-candidate",
                        "manifest_sha256": "0" * 64,
                    },
                    "stance": "contract",
                    "derivation_ids": [],
                },
                {
                    "candidate": {
                        "work_id": "contract-work",
                        "candidate_id": "contract-candidate-b",
                        "manifest_sha256": "1" * 64,
                    },
                    "stance": "contract",
                    "derivation_ids": [],
                },
            ],
            "invariant_indexes": [0],
        },
    ),
    "render_workflow_candidate": _contract(
        properties={"project_key", "workflow_id", "expected_revision"},
        required={"project_key", "workflow_id", "expected_revision"},
        defaults={},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
        },
    ),
    "attach_workflow_candidate_for_audit": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "work_id",
            "candidate_id",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "work_id",
            "candidate_id",
        },
        defaults={},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "work_id": "contract-work",
            "candidate_id": "contract-candidate",
        },
    ),
    "decide_workflow_iteration": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "disposition",
            "summary",
            "rationale",
            "perception_basis",
            "protected_values",
            "sacrificed_values",
            "evidence_ids",
            "exception_ids",
            "derivation_ids",
            "review_ids",
            "evidence_dispositions",
            "charter_settlement",
            "expected_audible_change",
            "revision_scope",
            "withdrawal_condition",
            "prior_revision_assessment",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "disposition",
            "summary",
            "rationale",
            "perception_basis",
        },
        defaults={
            "protected_values": None,
            "sacrificed_values": None,
            "evidence_ids": None,
            "exception_ids": None,
            "derivation_ids": None,
            "review_ids": None,
            "evidence_dispositions": None,
            "charter_settlement": None,
            "expected_audible_change": None,
            "revision_scope": None,
            "withdrawal_condition": None,
            "prior_revision_assessment": None,
        },
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "disposition": "stop",
            "summary": "contract",
            "rationale": "contract",
            "perception_basis": "report_only",
        },
    ),
    "record_workflow_authoring_revision": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "authoring_revision",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "authoring_revision",
        },
        defaults={},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "authoring_revision": "0" * 64,
        },
    ),
    "rollback_creative_workflow": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "target_iteration_number",
            "summary",
            "rationale",
            "perception_basis",
            "prior_revision_assessment",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "target_iteration_number",
            "summary",
            "rationale",
            "perception_basis",
        },
        defaults={"prior_revision_assessment": None},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "target_iteration_number": 1,
            "summary": "contract",
            "rationale": "contract",
            "perception_basis": "report_only",
        },
    ),
    "cancel_workflow_render": _contract(
        properties={"project_key", "workflow_id", "expected_revision"},
        required={"project_key", "workflow_id", "expected_revision"},
        defaults={},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
        },
    ),
    "stop_creative_workflow": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "reason",
            "summary",
            "perception_basis",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "reason",
            "summary",
        },
        defaults={"perception_basis": "report_only"},
        minimal={
            "project_key": "contract-project",
            "workflow_id": "0" * 32,
            "expected_revision": "0" * 64,
            "reason": "cancelled",
            "summary": "contract",
        },
    ),
    "render": _contract(
        properties={
            "score",
            "roster",
            "title",
            "seed",
            "expression",
            "range_mode",
            "normalize_peak_db",
            "hall",
            "master_gain_db",
            "space_config",
            "collaboration_mode",
            "write_stems",
            "use_stem_cache",
            "refresh_stem_cache",
            "trusted_only",
            "render_profile",
            "output_id",
            "parent_candidate_id",
            "overwrite",
            "expected_receipt_sha256",
            "expected_render_profile_sha256",
            "instrument_scope",
        },
        required={"score", "roster"},
        defaults={
            "title": "untitled",
            "seed": None,
            "expression": None,
            "range_mode": None,
            "normalize_peak_db": None,
            "hall": None,
            "master_gain_db": None,
            "space_config": None,
            "collaboration_mode": None,
            "write_stems": None,
            "use_stem_cache": None,
            "refresh_stem_cache": None,
            "trusted_only": None,
            "render_profile": None,
            "output_id": None,
            "parent_candidate_id": None,
            "overwrite": False,
            "expected_receipt_sha256": None,
            "expected_render_profile_sha256": None,
            "instrument_scope": None,
        },
        minimal={"score": {}, "roster": {}},
    ),
}


READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
RENDER_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}
AUTHORING_WRITE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
AUTHORING_WRITE_TOOLS = {
    "create_authoring_project",
    "save_authoring_project",
    "render_authoring_revision",
    "create_creative_workflow",
    "activate_creative_workflow",
    "record_workflow_composition_map",
    "commit_workflow_charter_amendment",
    "record_workflow_review",
    "record_workflow_evidence",
    "record_verified_workflow_hard_failure",
    "register_workflow_exception",
    "record_workflow_derivation",
    "record_workflow_fork",
    "attach_workflow_candidate_for_audit",
    "decide_workflow_iteration",
    "record_workflow_authoring_revision",
    "rollback_creative_workflow",
    "cancel_workflow_render",
    "stop_creative_workflow",
}
RENDER_TOOLS = {"render", "render_workflow_candidate"}


@unittest.skipUnless(_HAS_MCP, "optional mcp package is not installed")
class McpAdvertisedContractTests(unittest.TestCase):
    def test_all_tool_schemas_defaults_outputs_and_annotations_are_fixed(self) -> None:
        from tianlai.mcp_server import mcp

        listed = asyncio.run(mcp.list_tools())
        tools = {tool.name: tool for tool in listed}
        self.assertEqual(set(tools), set(TOOL_CONTRACTS))

        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(tool=name):
                tool = tools[name]
                input_schema = tool.input_schema
                properties = input_schema.get("properties", {})
                required = set(input_schema.get("required", []))
                defaults = {
                    field: schema["default"]
                    for field, schema in properties.items()
                    if "default" in schema
                }

                self.assertEqual(input_schema.get("type"), "object")
                self.assertIs(input_schema.get("additionalProperties"), False)
                self.assertEqual(set(properties), contract["properties"])
                self.assertEqual(required, contract["required"])
                self.assertEqual(defaults, contract["defaults"])
                self.assertEqual(
                    set(properties) - required,
                    set(contract["defaults"]),
                )

                self.assertIsInstance(tool.output_schema, dict)
                self.assertEqual(tool.output_schema.get("type"), "object")
                self.assertIsInstance(tool.title, str)
                self.assertTrue(tool.title.strip())
                self.assertIsNotNone(tool.annotations)
                expected_annotations = (
                    RENDER_ANNOTATIONS
                    if name in RENDER_TOOLS
                    else AUTHORING_WRITE_ANNOTATIONS
                    if name in AUTHORING_WRITE_TOOLS
                    else READ_ONLY_ANNOTATIONS
                )
                self.assertEqual(
                    tool.annotations.model_dump(
                        by_alias=True,
                        exclude_none=True,
                    ),
                    expected_annotations,
                )

        restore_properties = tools["plan_resource_restore"].input_schema[
            "properties"
        ]
        for selector in ("instrument_ids", "family_ids", "groups"):
            array_schema = next(
                option
                for option in restore_properties[selector]["anyOf"]
                if option.get("type") == "array"
            )
            self.assertEqual(array_schema["maxItems"], 128)
            self.assertEqual(array_schema["items"]["minLength"], 1)
            self.assertEqual(array_schema["items"]["maxLength"], 256)

        governance_option = tools["create_creative_workflow"].input_schema[
            "properties"
        ]["composition_governance"]
        self.assertEqual(governance_option["type"], "boolean")
        self.assertIs(governance_option["default"], True)

        for tool_name in (
            "inspect_workflow_composition",
            "record_workflow_composition_map",
        ):
            map_schema = tools[tool_name].input_schema
            map_property = map_schema["properties"]["composition_map"]
            map_reference = (
                next(
                    option["$ref"]
                    for option in map_property["anyOf"]
                    if "$ref" in option
                )
                if "anyOf" in map_property
                else map_property["$ref"]
            )
            map_definition = map_schema["$defs"][map_reference.rsplit("/", 1)[-1]]
            self.assertIs(map_definition["additionalProperties"], False)
            self.assertEqual(
                set(map_definition["required"]),
                {"kind", "schema_version", "nodes"},
            )
            schema_version = map_definition["properties"]["schema_version"]
            self.assertEqual(schema_version["type"], "integer")
            self.assertEqual(schema_version["minimum"], 1)
            self.assertEqual(schema_version["maximum"], 1)
            nodes = map_definition["properties"]["nodes"]
            self.assertEqual(nodes["minItems"], 1)
            self.assertEqual(nodes["maxItems"], 256)
            node_definition = map_schema["$defs"][
                nodes["items"]["$ref"].rsplit("/", 1)[-1]
            ]
            self.assertIs(node_definition["additionalProperties"], False)
            self.assertEqual(
                set(node_definition["required"]),
                {"node_id", "label", "function"},
            )

        for tool_name in (
            "preflight_workflow_charter_amendment",
            "commit_workflow_charter_amendment",
        ):
            amendment_schema = tools[tool_name].input_schema
            proposal_reference = amendment_schema["properties"]["proposal"]["$ref"]
            proposal_definition = amendment_schema["$defs"][
                proposal_reference.rsplit("/", 1)[-1]
            ]
            self.assertIs(proposal_definition["additionalProperties"], False)
            self.assertEqual(
                set(proposal_definition["required"]),
                {
                    "summary",
                    "why_score_revision_is_insufficient",
                    "why_bounded_exception_is_insufficient",
                    "expected_gain",
                    "accepted_costs",
                    "replacement_constraints",
                    "failure_conditions",
                    "basis_ids",
                    "operations",
                },
            )
            operations = proposal_definition["properties"]["operations"]
            self.assertEqual(operations["minItems"], 1)
            self.assertEqual(operations["maxItems"], 32)
            self.assertEqual(
                operations["items"]["discriminator"]["propertyName"],
                "op",
            )
            for operation_variant in operations["items"]["oneOf"]:
                operation_definition = amendment_schema["$defs"][
                    operation_variant["$ref"].rsplit("/", 1)[-1]
                ]
                self.assertIs(operation_definition["additionalProperties"], False)

        commit_schema = tools["commit_workflow_charter_amendment"].input_schema
        cost_reference = commit_schema["properties"]["cost_acknowledgement"][
            "$ref"
        ]
        cost_definition = commit_schema["$defs"][cost_reference.rsplit("/", 1)[-1]]
        self.assertIs(cost_definition["additionalProperties"], False)
        self.assertEqual(
            set(cost_definition["required"]),
            set(_AMENDMENT_COST_MINIMAL),
        )
        for field in (
            "operation_count",
            "affected_claim_count",
            "affected_root_field_count",
            "composition_dependencies_to_revalidate",
            "derivations_to_revalidate",
            "reviews_to_revalidate",
            "evidence_interpretations_to_revalidate",
            "observations_preserved",
            "hard_failures_preserved",
        ):
            self.assertEqual(cost_definition["properties"][field]["type"], "integer")
            self.assertEqual(cost_definition["properties"][field]["minimum"], 0)

        review_schema = tools["record_workflow_review"].input_schema
        question_answers = next(
            option
            for option in review_schema["properties"]["question_answers"]["anyOf"]
            if option.get("type") == "array"
        )
        self.assertEqual(question_answers["minItems"], 1)
        self.assertEqual(question_answers["maxItems"], 128)
        self.assertIs(question_answers["uniqueItems"], True)
        answer_definition = review_schema["$defs"][
            question_answers["items"]["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertIs(answer_definition["additionalProperties"], False)
        self.assertEqual(
            set(answer_definition["required"]),
            {"question_id", "answer", "claim_ids", "node_ids", "event_ids"},
        )
        self.assertEqual(
            answer_definition["properties"]["claim_ids"]["maxItems"], 1024
        )
        self.assertEqual(
            answer_definition["properties"]["node_ids"]["maxItems"], 256
        )
        self.assertEqual(
            answer_definition["properties"]["event_ids"]["maxItems"], 128
        )

        derivation_schema = tools["record_workflow_derivation"].input_schema
        derivation_properties = derivation_schema["properties"]
        premise_schema = derivation_properties["premises"]
        self.assertEqual(premise_schema["minItems"], 1)
        self.assertEqual(premise_schema["maxItems"], 8)
        premise_variants = premise_schema["items"]["oneOf"]
        self.assertEqual(len(premise_variants), 4)
        for variant in premise_variants:
            definition = derivation_schema["$defs"][
                variant["$ref"].rsplit("/", 1)[-1]
            ]
            self.assertIs(definition["additionalProperties"], False)
            self.assertEqual(
                set(definition["required"]),
                {
                    "kind",
                    "reference",
                    "event_ids",
                    "artifact_sha256",
                    "artifact_role",
                },
            )
        alternative_schema = derivation_schema["$defs"][
            derivation_properties["excluded_alternatives"]["items"]["$ref"].rsplit(
                "/", 1
            )[-1]
        ]
        self.assertIs(alternative_schema["additionalProperties"], False)
        self.assertEqual(
            set(alternative_schema["required"]),
            {"alternative", "failure", "premise_indexes"},
        )
        premise_indexes_schema = alternative_schema["properties"][
            "premise_indexes"
        ]
        self.assertEqual(premise_indexes_schema["minItems"], 1)
        self.assertEqual(premise_indexes_schema["maxItems"], 8)
        self.assertIs(premise_indexes_schema["uniqueItems"], True)
        self.assertEqual(premise_indexes_schema["items"]["minimum"], 0)
        self.assertEqual(premise_indexes_schema["items"]["maximum"], 7)
        for field in (
            "charter_claim_ids",
            "composition_map_node_ids",
            "question_ids",
        ):
            reference_list = next(
                option
                for option in derivation_properties[field]["anyOf"]
                if option.get("type") == "array"
            )
            self.assertEqual(reference_list["minItems"], 1)
            self.assertEqual(reference_list["maxItems"], 128)
            self.assertIs(reference_list["uniqueItems"], True)

        decision_schema = tools["decide_workflow_iteration"].input_schema
        decision_properties = decision_schema["properties"]
        review_ids_schema = next(
            option
            for option in decision_properties["review_ids"]["anyOf"]
            if option.get("type") == "array"
        )
        self.assertEqual(review_ids_schema["maxItems"], 32)
        self.assertIs(review_ids_schema["uniqueItems"], True)
        self.assertEqual(
            review_ids_schema["items"]["pattern"],
            r"^review-[0-9a-f]{20}$",
        )

        dispositions_schema = next(
            option
            for option in decision_properties["evidence_dispositions"]["anyOf"]
            if option.get("type") == "array"
        )
        self.assertEqual(dispositions_schema["maxItems"], 128)
        self.assertIs(dispositions_schema["uniqueItems"], True)
        disposition_definition = decision_schema["$defs"][
            dispositions_schema["items"]["$ref"].rsplit("/", 1)[-1]
        ]
        self.assertIs(disposition_definition["additionalProperties"], False)
        self.assertEqual(
            set(disposition_definition["required"]),
            {"evidence_id", "disposition", "rationale", "basis_ids"},
        )
        self.assertEqual(
            disposition_definition["properties"]["evidence_id"]["pattern"],
            r"^evidence-[0-9a-f]{20}$",
        )
        self.assertEqual(
            set(disposition_definition["properties"]["disposition"]["enum"]),
            {
                "resolved",
                "accepted_risk",
                "excepted",
                "deferred",
                "revision_target",
                "contested",
            },
        )
        basis_ids_schema = disposition_definition["properties"]["basis_ids"]
        self.assertEqual(basis_ids_schema["maxItems"], 32)
        self.assertIs(basis_ids_schema["uniqueItems"], True)
        self.assertEqual(
            basis_ids_schema["items"]["pattern"],
            r"^(?:review|evidence|exception|derivation)-[0-9a-f]{20}$",
        )

        settlement_schema = next(
            option
            for option in decision_properties["charter_settlement"]["anyOf"]
            if option.get("type") == "array"
        )
        settlement_definition = decision_schema["$defs"][
            settlement_schema["items"]["$ref"].rsplit("/", 1)[-1]
        ]
        settlement_basis_ids = settlement_definition["properties"]["basis_ids"]
        self.assertEqual(settlement_basis_ids["minItems"], 1)
        self.assertEqual(settlement_basis_ids["maxItems"], 16)
        self.assertIs(settlement_basis_ids["uniqueItems"], True)
        self.assertEqual(
            settlement_basis_ids["items"]["pattern"],
            r"^(?:review|evidence|exception|derivation)-[0-9a-f]{20}$",
        )
        settlement_event_ids = settlement_definition["properties"]["event_ids"]
        self.assertEqual(settlement_event_ids["maxItems"], 32)
        self.assertIs(settlement_event_ids["uniqueItems"], True)

        revision_scope_ref = next(
            option["$ref"]
            for option in decision_properties["revision_scope"]["anyOf"]
            if "$ref" in option
        )
        revision_scope_definition = decision_schema["$defs"][
            revision_scope_ref.rsplit("/", 1)[-1]
        ]
        self.assertIs(revision_scope_definition["additionalProperties"], False)
        self.assertEqual(
            set(revision_scope_definition["required"]),
            {
                "change_scale",
                "documents",
                "allowed_document_paths",
                "score",
                "whole_work_cost",
            },
        )
        self.assertEqual(
            set(
                revision_scope_definition["properties"]["documents"]["items"][
                    "enum"
                ]
            ),
            {"score", "authoring_roster", "render_profile"},
        )
        paths_schema = next(
            option
            for option in revision_scope_definition["properties"][
                "allowed_document_paths"
            ]["anyOf"]
            if option.get("type") == "object"
        )
        self.assertEqual(paths_schema["maxProperties"], 3)
        self.assertEqual(
            set(paths_schema["additionalProperties"]["items"]),
            {"type", "minLength", "maxLength"},
        )
        self.assertEqual(paths_schema["additionalProperties"]["maxItems"], 1024)
        self.assertIs(paths_schema["additionalProperties"]["uniqueItems"], True)
        score_ref = next(
            option["$ref"]
            for option in revision_scope_definition["properties"]["score"][
                "anyOf"
            ]
            if "$ref" in option
        )
        score_scope = decision_schema["$defs"][score_ref.rsplit("/", 1)[-1]]
        self.assertEqual(score_scope["properties"]["bar_ranges"]["maxItems"], 128)
        self.assertEqual(
            set(score_scope["properties"]["allowed_note_fields"]["items"]["enum"]),
            {
                "bar",
                "beat",
                "duration_beats",
                "pitch",
                "dynamic",
                "velocity",
                "articulation",
                "tie",
                "staff",
                "voice",
                "part_id",
            },
        )
        self.assertNotIn(
            "event_id",
            score_scope["properties"]["allowed_note_fields"]["items"]["enum"],
        )
        self.assertNotIn("allow_metadata_changes", score_scope["properties"])
        self.assertEqual(score_scope["properties"]["allow_reordering"]["type"], "boolean")
        whole_work_ref = next(
            option["$ref"]
            for option in revision_scope_definition["properties"][
                "whole_work_cost"
            ]["anyOf"]
            if "$ref" in option
        )
        whole_work_cost = decision_schema["$defs"][
            whole_work_ref.rsplit("/", 1)[-1]
        ]
        accepted_costs = whole_work_cost["properties"]["accepted_costs"]
        self.assertEqual(accepted_costs["minItems"], 3)
        self.assertEqual(accepted_costs["maxItems"], 3)
        self.assertIs(accepted_costs["uniqueItems"], True)
        self.assertEqual(
            set(accepted_costs["items"]["enum"]),
            {
                "expanded_change_surface",
                "downstream_compatibility_rework",
                "increased_topic_drift_risk",
            },
        )

        assessment_ref = next(
            option["$ref"]
            for option in decision_properties["prior_revision_assessment"][
                "anyOf"
            ]
            if "$ref" in option
        )
        assessment = decision_schema["$defs"][
            assessment_ref.rsplit("/", 1)[-1]
        ]
        self.assertEqual(
            set(assessment["properties"]["outcome"]["enum"]),
            {"promote_challenger", "retain_baseline", "inconclusive"},
        )
        self.assertEqual(assessment["properties"]["basis_ids"]["minItems"], 1)

        rollback_schema = tools["rollback_creative_workflow"].input_schema
        rollback_assessment_ref = next(
            option["$ref"]
            for option in rollback_schema["properties"][
                "prior_revision_assessment"
            ]["anyOf"]
            if "$ref" in option
        )
        self.assertEqual(
            rollback_assessment_ref.rsplit("/", 1)[-1],
            "_WorkflowPriorRevisionAssessment",
        )

        fork_schema = tools["record_workflow_fork"].input_schema
        fork_properties = fork_schema["properties"]
        fork_event_ids = next(
            option
            for option in fork_properties["event_ids"]["anyOf"]
            if option.get("type") == "array"
        )
        self.assertEqual(fork_event_ids["maxItems"], 128)
        self.assertIs(fork_event_ids["uniqueItems"], True)
        fork_part_ids = next(
            option
            for option in fork_properties["part_ids"]["anyOf"]
            if option.get("type") == "array"
        )
        self.assertEqual(fork_part_ids["maxItems"], 64)
        self.assertIs(fork_part_ids["uniqueItems"], True)


@unittest.skipUnless(_HAS_MCP, "optional mcp package is not installed")
class McpStdioArgumentBoundaryTests(unittest.TestCase):
    def test_real_stdio_rejects_unknown_and_missing_required_arguments(self) -> None:
        asyncio.run(self._exercise_argument_boundary())

    async def _exercise_argument_boundary(self) -> None:
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(
            prefix="Tianlai MCP contract ",
        ) as temporary:
            sandbox = Path(temporary)
            runtime = sandbox / "Unicode 运行目录"
            input_root = sandbox / "Input root"
            output_root = sandbox / "Output root"
            resource_root = sandbox / "Empty resources"
            for directory in (
                runtime,
                input_root,
                output_root,
                resource_root,
            ):
                directory.mkdir(parents=True)

            server = StdioServerParameters(
                command=sys.executable,
                args=["-m", "tianlai.mcp_server"],
                cwd=runtime,
                env={
                    "TIANLAI_HOME": str(ROOT),
                    "TIANLAI_INPUT_ROOTS": str(input_root),
                    "TIANLAI_OUTPUT_DIR": str(output_root),
                    "TIANLAI_RESOURCE_DIR": str(resource_root),
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
                listed = await session.list_tools()
                self.assertEqual(
                    {tool.name for tool in listed.tools},
                    set(TOOL_CONTRACTS),
                )

                for name, contract in TOOL_CONTRACTS.items():
                    unexpected = dict(contract["minimal"])
                    unexpected[_UNEXPECTED_FIELD] = True
                    with self.subTest(tool=name, case="unknown"):
                        result = await session.call_tool(name, unexpected)
                        self._assert_protocol_argument_error(result)

                    for field in sorted(contract["required"]):
                        missing = dict(contract["minimal"])
                        del missing[field]
                        with self.subTest(
                            tool=name,
                            case="missing",
                            field=field,
                        ):
                            result = await session.call_tool(name, missing)
                            self._assert_protocol_argument_error(result)

                derivation_minimal = dict(
                    TOOL_CONTRACTS["record_workflow_derivation"]["minimal"]
                )
                derivation_premise = dict(derivation_minimal["premises"][0])
                derivation_alternative = dict(
                    derivation_minimal["excluded_alternatives"][0]
                )
                decision_minimal = dict(
                    TOOL_CONTRACTS["decide_workflow_iteration"]["minimal"]
                )
                settlement_item = {
                    "target": "one_sentence_promise",
                    "status": "kept",
                    "rationale": "contract",
                    "basis_ids": ["review-" + "0" * 20],
                    "event_ids": [],
                }
                fork_minimal = dict(
                    TOOL_CONTRACTS["record_workflow_fork"]["minimal"]
                )
                map_minimal = dict(_COMPOSITION_MAP_MINIMAL)
                map_node = dict(_COMPOSITION_MAP_MINIMAL["nodes"][0])
                amendment_add_operation = {
                    "op": "add",
                    "collection_id": "collection-" + "0" * 64,
                    "position": 0,
                    "value": "contract",
                }
                review_answer = {
                    "question_id": "question-" + "0" * 20,
                    "answer": "contract",
                    "claim_ids": ["claim-" + "0" * 64],
                    "node_ids": ["whole-work"],
                    "event_ids": [],
                }
                strict_type_cases = (
                    (
                        "create_creative_workflow",
                        {
                            **TOOL_CONTRACTS["create_creative_workflow"]["minimal"],
                            "composition_governance": "true",
                        },
                    ),
                    (
                        "create_creative_workflow",
                        {
                            **TOOL_CONTRACTS["create_creative_workflow"]["minimal"],
                            "composition_governance": 1,
                        },
                    ),
                    (
                        "render",
                        {"score": {}, "roster": {}, "overwrite": "true"},
                    ),
                    (
                        "render",
                        {"score": {}, "roster": {}, "overwrite": 1},
                    ),
                    (
                        "validate_project",
                        {"score": {}, "roster": {}, "trusted_only": "false"},
                    ),
                    (
                        "validate_project",
                        {"score": {}, "roster": {}, "seed": True},
                    ),
                    (
                        "locate",
                        {"score": {}, "roster": {}, "at_seconds": True},
                    ),
                    (
                        "plan_resource_restore",
                        {"instrument_ids": ["x"] * 129},
                    ),
                    (
                        "plan_resource_restore",
                        {"family_ids": ["x" * 257]},
                    ),
                    (
                        "get_music_constitution_clauses",
                        {"clause_ids": ["C0.02"] * 13},
                    ),
                    (
                        "record_workflow_composition_map",
                        {
                            **TOOL_CONTRACTS[
                                "record_workflow_composition_map"
                            ]["minimal"],
                            "composition_map": {
                                **map_minimal,
                                "schema_version": True,
                            },
                        },
                    ),
                    (
                        "record_workflow_composition_map",
                        {
                            **TOOL_CONTRACTS[
                                "record_workflow_composition_map"
                            ]["minimal"],
                            "composition_map": {
                                **map_minimal,
                                "schema_version": 1.0,
                            },
                        },
                    ),
                    (
                        "inspect_workflow_composition",
                        {
                            **TOOL_CONTRACTS[
                                "inspect_workflow_composition"
                            ]["minimal"],
                            "composition_map": {
                                **map_minimal,
                                "nodes": [
                                    {**map_node, _UNEXPECTED_FIELD: True}
                                ],
                            },
                        },
                    ),
                    (
                        "preflight_workflow_charter_amendment",
                        {
                            **TOOL_CONTRACTS[
                                "preflight_workflow_charter_amendment"
                            ]["minimal"],
                            "proposal": {
                                **_AMENDMENT_PROPOSAL_MINIMAL,
                                "operations": [
                                    {
                                        **amendment_add_operation,
                                        "position": True,
                                    }
                                ],
                            },
                        },
                    ),
                    (
                        "commit_workflow_charter_amendment",
                        {
                            **TOOL_CONTRACTS[
                                "commit_workflow_charter_amendment"
                            ]["minimal"],
                            "cost_acknowledgement": {
                                **_AMENDMENT_COST_MINIMAL,
                                "operation_count": True,
                            },
                        },
                    ),
                    (
                        "commit_workflow_charter_amendment",
                        {
                            **TOOL_CONTRACTS[
                                "commit_workflow_charter_amendment"
                            ]["minimal"],
                            "cost_acknowledgement": {
                                **_AMENDMENT_COST_MINIMAL,
                                "operation_count": 1.0,
                            },
                        },
                    ),
                    (
                        "record_workflow_review",
                        {
                            **TOOL_CONTRACTS["record_workflow_review"]["minimal"],
                            "question_answers": [],
                        },
                    ),
                    (
                        "record_workflow_review",
                        {
                            **TOOL_CONTRACTS["record_workflow_review"]["minimal"],
                            "question_answers": [review_answer, review_answer],
                        },
                    ),
                    (
                        "record_workflow_derivation",
                        {**derivation_minimal, "premises": []},
                    ),
                    (
                        "record_workflow_derivation",
                        {**derivation_minimal, "excluded_alternatives": []},
                    ),
                    (
                        "record_workflow_derivation",
                        {
                            **derivation_minimal,
                            "premises": [
                                {
                                    **derivation_premise,
                                    _UNEXPECTED_FIELD: True,
                                }
                            ],
                        },
                    ),
                    (
                        "record_workflow_derivation",
                        {
                            **derivation_minimal,
                            "excluded_alternatives": [
                                {
                                    **derivation_alternative,
                                    _UNEXPECTED_FIELD: True,
                                }
                            ],
                        },
                    ),
                    (
                        "record_workflow_derivation",
                        {
                            **derivation_minimal,
                            "excluded_alternatives": [
                                {
                                    **derivation_alternative,
                                    "premise_indexes": [0, 0],
                                }
                            ],
                        },
                    ),
                    (
                        "decide_workflow_iteration",
                        {
                            **decision_minimal,
                            "charter_settlement": [
                                {**settlement_item, "basis_ids": []}
                            ],
                        },
                    ),
                    (
                        "decide_workflow_iteration",
                        {
                            **decision_minimal,
                            "charter_settlement": [
                                {
                                    **settlement_item,
                                    "basis_ids": ["review-" + "0" * 20] * 17,
                                }
                            ],
                        },
                    ),
                    (
                        "decide_workflow_iteration",
                        {
                            **decision_minimal,
                            "charter_settlement": [
                                {**settlement_item, "basis_ids": ["not-an-id"]}
                            ],
                        },
                    ),
                    (
                        "decide_workflow_iteration",
                        {
                            **decision_minimal,
                            "charter_settlement": [
                                {**settlement_item, "event_ids": ["event-1"] * 2}
                            ],
                        },
                    ),
                    (
                        "decide_workflow_iteration",
                        {
                            **decision_minimal,
                            "charter_settlement": [
                                {
                                    **settlement_item,
                                    "event_ids": [
                                        f"event-{index}" for index in range(33)
                                    ],
                                }
                            ],
                        },
                    ),
                    (
                        "record_workflow_fork",
                        {**fork_minimal, "event_ids": ["event-1"] * 2},
                    ),
                    (
                        "record_workflow_fork",
                        {
                            **fork_minimal,
                            "event_ids": [
                                f"event-{index}" for index in range(129)
                            ],
                        },
                    ),
                    (
                        "record_workflow_fork",
                        {**fork_minimal, "part_ids": ["part-1"] * 2},
                    ),
                    (
                        "record_workflow_fork",
                        {
                            **fork_minimal,
                            "part_ids": [f"part-{index}" for index in range(65)],
                        },
                    ),
                )
                for name, arguments in strict_type_cases:
                    with self.subTest(tool=name, case="strict_type"):
                        result = await session.call_tool(name, arguments)
                        self._assert_protocol_argument_error(result)

    def _assert_protocol_argument_error(self, result) -> None:
        self.assertTrue(result.is_error, result.content)
        self.assertIsNone(result.structured_content)


if __name__ == "__main__":
    unittest.main()
