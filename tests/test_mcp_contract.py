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
        properties={"project_key", "mode", "base_authoring_revision", "budget"},
        required={"project_key", "mode"},
        defaults={"base_authoring_revision": None, "budget": None},
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
    "record_workflow_review": _contract(
        properties={
            "project_key",
            "workflow_id",
            "expected_revision",
            "phase",
            "perception_basis",
            "summary",
        },
        required={
            "project_key",
            "workflow_id",
            "expected_revision",
            "phase",
            "perception_basis",
            "summary",
        },
        defaults={},
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
            "expected_audible_change",
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
            "expected_audible_change": None,
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
        defaults={},
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
    "record_workflow_review",
    "record_workflow_evidence",
    "record_verified_workflow_hard_failure",
    "register_workflow_exception",
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

                strict_type_cases = (
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
