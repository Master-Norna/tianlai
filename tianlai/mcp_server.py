"""天籁 MCP 服务:把渲染内核做成"AI 手上的乐器"。

这不是一个胖函数("一句话→音频"),而是一小把细颗粒工具,让任何会调工具的
AI **反复演奏、边写边改**:先问调色板(能弹什么)→ 照格式写乐谱与编制 →
渲染 → 读客观仪表(峰值/平衡/削波)→ 改一处再渲。魂(确定性、可审计、分轨、
干净来源)全留着;AI 拿到的是**音频路径 + 客观测量**,而"好不好听"这一锤
始终留给人——分析是仪表,不是品味。

依赖隔离:只有本模块 import ``mcp``,核心引擎不受影响(``pip install
"tianlai-audio[mcp]"`` 才需要它)。

运行:``python -m tianlai.mcp_server``(stdio 传输,供 MCP 客户端接入)。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
from collections import Counter
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
)

from . import __version__

from .candidate import (
    CANDIDATE_MANIFEST_NAME,
    canonical_json_sha256,
    compare_candidates,
    load_candidate,
    locate_candidate,
    prepare_candidate_target,
    publish_candidate_metadata,
)
from .authoring_core import (
    build_authoring_snapshot,
    validate_project_readiness as validate_authoring_project_readiness,
)
from .authoring_project import (
    AuthoringProjectError,
    AuthoringProjectState,
    RENDERS_DIRECTORY_NAME as AUTHORING_RENDERS_DIRECTORY_NAME,
    create_authoring_project as create_authoring_project_state,
    open_authoring_project as open_authoring_project_state,
    save_authoring_project as save_authoring_project_state,
)
from .authoring_render import (
    AuthoringRenderError,
    render_project_candidate as render_authoring_project_candidate,
)
from .capability import load_capabilities
from .conductor import ExpressionSettings, build_plan
from .creative_workflow import (
    CreativeWorkflowError,
    CreativeWorkflowSnapshot,
    activate_creative_workflow as activate_creative_workflow_state,
    attach_existing_candidate_for_audit as attach_existing_candidate_for_audit_state,
    cancel_workflow_render as cancel_workflow_render_state,
    create_creative_workflow as create_creative_workflow_state,
    decide_workflow_iteration as decide_workflow_iteration_state,
    inspect_workflow_candidate_status,
    open_creative_workflow as open_creative_workflow_state,
    record_workflow_authoring_revision as record_workflow_authoring_revision_state,
    record_workflow_candidate as record_workflow_candidate_state,
    record_workflow_evidence as record_workflow_evidence_state,
    record_verified_workflow_hard_failure as record_verified_workflow_hard_failure_state,
    record_workflow_review as record_workflow_review_state,
    register_workflow_exception as register_workflow_exception_state,
    request_workflow_render as request_workflow_render_state,
    rollback_workflow as rollback_workflow_state,
    terminate_creative_workflow as terminate_creative_workflow_state,
    unresolved_workflow_hard_failures,
    verify_creative_workflow_history as verify_creative_workflow_history_state,
    workflow_render_authorization,
)
from .ensemble import render_plan
from .mcp_diagnostics import (
    build_safe_resource_restore_plan,
    collect_instrument_resource_readiness,
    collect_runtime_diagnosis,
)
from .mcp_tool_contract import bind_strict_mcp_tool
from .path_policy import (
    InputPathPolicyError,
    discover_mcp_input_policy,
)
from .plain_file import read_plain_file_bytes, revalidate_plain_file
from .portable_filename import is_windows_reserved_filename
from .preflight import roster_availability_problems
from .project_review import build_project_review_safely
from .project_import import (
    import_project as import_project_bundle,
    promote_roster as promote_imported_roster,
)
from .render_lock import (
    PlainDirectoryIdentity,
    RenderLockError,
    capture_plain_directory,
    ensure_authorized_child_directory,
    ensure_plain_directory_tree,
    revalidate_plain_directory,
)
from .render_profile import (
    RenderProfile,
    parse_render_profile,
    profile_with_overrides,
)
from .resource_limits import (
    ResourceLimitError,
    validate_render_request_resource_limits,
    validate_score_resource_limits,
)
from .roster import parse_roster_document
from .runtime_layout import discover_runtime_layout
from .score import (
    parse_score_document,
    pitch_name,
    upgrade_legacy_score_to_v1,
)
from .score_ops import (
    ScoreOpsError,
    apply_score_patch,
    compare_scores,
    slice_score,
)
from .score_time import (
    coordinate_at_position,
    coordinate_at_seconds,
    seconds_window_around,
    validate_score_time_coordinates,
)
from .self_check import (
    build_issue,
    build_review_report,
    paginate_issues,
    summarize_issues,
)
from .space import SpaceConfig
from .trust import (
    TrustPolicyError,
    load_trusted_instruments,
    load_variant_hints,
)
from .workflow_binding import validate_workflow_authorization

_RUNTIME_LAYOUT = discover_runtime_layout()
ROOT = _RUNTIME_LAYOUT.home
CATALOG = _RUNTIME_LAYOUT.catalog
ALLOWLIST_FILE = _RUNTIME_LAYOUT.allowlist
OUTPUT_DIR = _RUNTIME_LAYOUT.output / "mcp"

mcp = MCPServer(
    name="tianlai",
    title="Tianlai",
    description="Local-first deterministic music rendering and iteration runtime",
    instructions=(
        "Read the current score/roster contracts and instrument catalogue before "
        "writing music. Validate before rendering, preserve candidate immutability, "
        "and leave musical judgement and publication decisions to the creator."
    ),
    version=__version__,
)
mcp_tool = bind_strict_mcp_tool(mcp)

_READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_RENDER_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)
_AUTHORING_WRITE_TOOL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)

_caps_cache: dict[str, Any] | None = None

_ResourceSelector = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=256),
]
_ResourceSelectorList = Annotated[
    list[_ResourceSelector],
    Field(max_length=128),
]
_InstrumentQuery = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=128),
]
_AuthoringSelector = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=128),
]
_AuthoringTitle = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=1024),
]
_WorkflowText = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=4096),
]
_WorkflowShortText = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=1024),
]
_WorkflowReferenceList = Annotated[
    list[_AuthoringSelector],
    Field(max_length=128),
]
_ConstitutionClauseIdList = Annotated[
    list[_AuthoringSelector],
    Field(min_length=1, max_length=12),
]

_AUTHORING_PROJECTS_DIRECTORY_NAME = "authoring-projects"
_AUTHORING_RESULT_KIND = "tianlai.authoring_mcp_result"
_AUTHORING_RESULT_VERSION = 1
_AUTHORING_PROJECT_KEY_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$"
)
_AUTHORING_REVISION_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_AUTHORING_EVIDENCE_MAX_BYTES = 32 * 1024 * 1024
_WORKFLOW_RESULT_KIND = "tianlai.creative_workflow_mcp_result"
_WORKFLOW_RESULT_VERSION = 1
_WORKFLOW_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_OFFICIAL_CONSTITUTION = {
    "document_id": "tianlai-music-constitution",
    "version": "0.1",
    "language": "zh-CN",
    "content_sha256": "3c26f99806b2044b3fd45cbdc8ef12ffadf871d75dc119799881b0d992b75985",
}
_OFFICIAL_CONSTITUTIONS = {
    "zh-CN": _OFFICIAL_CONSTITUTION,
    "en": {
        "document_id": "tianlai-music-constitution",
        "version": "0.1",
        "language": "en",
        "content_sha256": "ca0cc236d93bca684a918f14814695835cf9aa437640294a4f02f898393903a9",
    },
}
_OFFICIAL_CONSTITUTION_FILENAMES = {
    "zh-CN": "天籁音乐宪法-v0.1.md",
    "en": "天籁音乐宪法-v0.1.en.md",
}
_CONSTITUTION_CLAUSE_LINE = re.compile(
    r"^\* \*\*(?P<clause_id>C[0-8](?:\.[A-Z])?(?:\.[0-9]{1,3}){1,2})"
    r"｜(?P<title>[^*]+)\*\*[：:]\s*(?P<text>.+?)\s*$"
)


class _McpAuthoringBoundaryError(RuntimeError):
    """One path-free semantic failure at the MCP authoring boundary."""

    def __init__(
        self,
        code: str,
        *,
        stage: str = "input",
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.stage = stage
        self.retryable = retryable
        super().__init__(code)


class _McpWorkflowBoundaryError(RuntimeError):
    """One stable, path-free failure at the creative-workflow MCP boundary."""

    def __init__(
        self,
        code: str,
        *,
        stage: str = "input",
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.stage = stage
        self.retryable = retryable
        super().__init__(code)


def _authoring_success(
    operation: str,
    project_key: str,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "kind": _AUTHORING_RESULT_KIND,
        "schema_version": _AUTHORING_RESULT_VERSION,
        "ok": True,
        "operation": operation,
        "project_key": project_key,
        **payload,
    }


def _authoring_failure(
    operation: str,
    error: BaseException,
    *,
    project_key: str | None = None,
) -> dict[str, Any]:
    safe_project_key = (
        project_key
        if isinstance(project_key, str)
        and _AUTHORING_PROJECT_KEY_PATTERN.fullmatch(project_key) is not None
        and not is_windows_reserved_filename(project_key)
        else None
    )
    source = "authoring"
    location: list[str | int] = []
    if isinstance(error, _McpAuthoringBoundaryError):
        code = error.code
        stage = error.stage
        retryable = error.retryable
        message_key = "authoringMcp." + code.replace(".", "_")
    elif isinstance(error, AuthoringProjectError):
        code = f"authoring_project.{error.code}"
        stage = "project"
        retryable = error.code == "revision_conflict"
        message_key = error.message_key
        source = error.source
        location = list(error.location_segments)
    elif isinstance(error, AuthoringRenderError):
        code = f"authoring_render.{error.code}"
        stage = error.stage
        retryable = error.retryable
        message_key = error.message_key
        source = "render"
    else:
        code = "authoring.internal_error"
        stage = "internal"
        retryable = False
        message_key = "authoringMcp.authoring_internal_error"
    result: dict[str, Any] = {
        "kind": _AUTHORING_RESULT_KIND,
        "schema_version": _AUTHORING_RESULT_VERSION,
        "ok": False,
        "operation": operation,
        "error": {
            "code": code,
            "message_key": message_key,
            "source": source,
            "stage": stage,
            "retryable": retryable,
            "location": {"segments": location},
        },
    }
    if safe_project_key is not None:
        result["project_key"] = safe_project_key
    return result


def _workflow_next_action(
    snapshot: CreativeWorkflowSnapshot,
    *,
    project_key: str,
) -> dict[str, Any] | None:
    """Translate durable core actions into the path-free high-level MCP loop."""

    document = snapshot.to_dict()
    state = document["state"]
    allowed = document["allowed_actions"]
    mapped = {
        "activate": "activate_creative_workflow",
        "record_review": "record_workflow_review",
        "record_evidence": "record_workflow_evidence",
        "register_exception": "register_workflow_exception",
        "request_render": "render_workflow_candidate",
        "record_candidate": "render_workflow_candidate",
        "cancel_render": "cancel_workflow_render",
        "attach_existing_candidate_for_audit": (
            "attach_workflow_candidate_for_audit"
        ),
        "decide": "decide_workflow_iteration",
        "record_authoring_revision": "record_workflow_authoring_revision",
        "rollback": "rollback_creative_workflow",
        "terminate": "stop_creative_workflow",
    }
    mode = state["mode"]
    alternatives = list(
        dict.fromkeys(
            mapped[item]
            for item in allowed
            if item in mapped
            and not (
                item == "attach_existing_candidate_for_audit"
                and mode != "audit"
            )
        )
    )
    status = state["status"]
    operation: str | None = None
    reason = "workflow_terminal"
    suggested_arguments: dict[str, Any] = {}
    prerequisites: list[dict[str, Any]] = []
    continuation: dict[str, Any] | None = None
    if status == "charter_pending":
        operation = "activate_creative_workflow"
        reason = "work_charter_required"
    elif status == "candidate_pending":
        operation = "render_workflow_candidate"
        reason = "reserved_render_must_be_executed_or_cancelled"
    elif status == "revision_pending":
        iteration = state["iterations"][-1]
        authoring_revision = iteration["anchor"]["authoring_revision"]
        operation = "get_authoring_snapshot"
        reason = "read_edit_save_then_bind_a_new_authoring_revision"
        suggested_arguments = {
            "project_key": project_key,
            "revision": authoring_revision,
        }
        prerequisites = [
            {
                "step": "read",
                "operation": "get_authoring_snapshot",
                "arguments": dict(suggested_arguments),
            },
            {
                "step": "edit",
                "action": "edit_complete_authoring_documents",
                "input_from": "get_authoring_snapshot.snapshot.documents",
                "constraint": "preserve_unmodified_documents_and_make_only_the_evidenced_change",
            },
            {
                "step": "save",
                "operation": "save_authoring_project",
                "arguments": {"project_key": project_key},
                "argument_sources": {
                    "expected_revision": (
                        "get_authoring_snapshot.snapshot.project.revision"
                    ),
                    "documents": "edited_complete_authoring_documents",
                },
            },
            {
                "step": "bind",
                "operation": "record_workflow_authoring_revision",
                "arguments": {
                    "project_key": project_key,
                    "workflow_id": snapshot.workflow_id,
                    "expected_revision": snapshot.revision,
                },
                "argument_sources": {
                    "authoring_revision": "save_authoring_project.project.revision"
                },
            },
        ]
        continuation = {
            "workflow_id": snapshot.workflow_id,
            "expected_revision": snapshot.revision,
        }
    elif status == "reviewing":
        iteration = state["iterations"][-1]
        candidate = iteration["anchor"]["candidate"]
        phases = {item["phase"] for item in iteration["reviews"]}
        historical_hard_failure = any(
            item["category"] == "hard_failure" for item in iteration["evidence"]
        )
        unresolved_hard_failures: list[dict[str, Any]] = []
        if historical_hard_failure:
            root = _authoring_project_root(
                project_key,
                create_namespace=False,
                require_existing=True,
            )
            try:
                unresolved_hard_failures = unresolved_workflow_hard_failures(
                    root,
                    snapshot,
                )
            except CreativeWorkflowError as exc:
                if exc.code == "workflow_revision_conflict":
                    return {
                        "operation": "open_creative_workflow",
                        "reason": "workflow_advanced_while_computing_next_action",
                        "suggested_arguments": {
                            "project_key": project_key,
                            "workflow_id": snapshot.workflow_id,
                        },
                        "alternatives": [],
                    }
                if exc.code == "trusted_validation_failed":
                    return {
                        "operation": "check_authoring_readiness",
                        "reason": "trusted_hard_failure_revalidation_unavailable",
                        "suggested_arguments": {
                            "project_key": project_key,
                            "revision": iteration["anchor"]["authoring_revision"],
                        },
                        "continuation": {
                            "operation": "open_creative_workflow",
                            "workflow_id": snapshot.workflow_id,
                        },
                        "alternatives": ["stop_creative_workflow"],
                    }
                raise
        if unresolved_hard_failures:
            operation = "decide_workflow_iteration"
            reason = "unresolved_hard_failure_requires_revision_preservation_or_stop"
            suggested_arguments = {
                "evidence_ids": [
                    item["evidence_id"] for item in unresolved_hard_failures
                ]
            }
        elif candidate is None and "intent" not in phases:
            operation = "record_workflow_review"
            reason = "intent_review_required_before_acceptance"
            suggested_arguments = {
                "phase": "intent",
                "perception_basis": "report_only",
            }
        elif candidate is None and "symbolic_structure" not in phases:
            operation = "record_workflow_review"
            reason = "symbolic_structure_review_required_before_render"
            suggested_arguments = {
                "phase": "symbolic_structure",
                "perception_basis": "report_only",
            }
        elif candidate is None and "orchestration_performance" not in phases:
            operation = "record_workflow_review"
            reason = "orchestration_review_required_before_render"
            suggested_arguments = {
                "phase": "orchestration_performance",
                "perception_basis": "report_only",
            }
        elif candidate is None:
            operation = "render_workflow_candidate"
            reason = "pre_render_reviews_complete"
        elif "intent" not in phases:
            operation = "record_workflow_review"
            reason = "intent_review_required_before_acceptance"
            suggested_arguments = {
                "phase": "intent",
                "perception_basis": "report_only",
            }
        elif "symbolic_structure" not in phases:
            operation = "record_workflow_review"
            reason = "symbolic_structure_review_required_before_acceptance"
            suggested_arguments = {
                "phase": "symbolic_structure",
                "perception_basis": "report_only",
            }
        elif "orchestration_performance" not in phases:
            operation = "record_workflow_review"
            reason = "orchestration_review_required_before_acceptance"
            suggested_arguments = {
                "phase": "orchestration_performance",
                "perception_basis": "report_only",
            }
        elif "render_report" not in phases:
            operation = "record_workflow_review"
            reason = "render_report_review_required_before_acceptance"
            suggested_arguments = {
                "phase": "render_report",
                "perception_basis": "report_only",
            }
        else:
            operation = "decide_workflow_iteration"
            reason = "evidence_and_candidate_ready_for_contextual_decision"
    if operation is None:
        return None
    result = {
        "operation": operation,
        "reason": reason,
        "expected_revision": snapshot.revision,
        "suggested_arguments": suggested_arguments,
        "alternatives": alternatives,
    }
    if prerequisites:
        result["prerequisites"] = prerequisites
    if continuation is not None:
        result["continuation"] = continuation
    return result


def _workflow_success(
    operation: str,
    project_key: str,
    snapshot: CreativeWorkflowSnapshot,
    *,
    include_next_action: bool = True,
    **payload: Any,
) -> dict[str, Any]:
    return {
        "kind": _WORKFLOW_RESULT_KIND,
        "schema_version": _WORKFLOW_RESULT_VERSION,
        "ok": True,
        "operation": operation,
        "project_key": project_key,
        "workflow": snapshot.to_dict(),
        "next_action": (
            _workflow_next_action(snapshot, project_key=project_key)
            if include_next_action
            else None
        ),
        **payload,
    }


def _workflow_failure(
    operation: str,
    error: BaseException,
    *,
    project_key: str | None = None,
    workflow_id: str | None = None,
) -> dict[str, Any]:
    safe_project_key = (
        project_key
        if isinstance(project_key, str)
        and _AUTHORING_PROJECT_KEY_PATTERN.fullmatch(project_key) is not None
        and not is_windows_reserved_filename(project_key)
        else None
    )
    safe_workflow_id = (
        workflow_id
        if isinstance(workflow_id, str)
        and _WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is not None
        else None
    )
    source = "workflow"
    location: list[str | int] = []
    if isinstance(error, _McpWorkflowBoundaryError):
        code = error.code
        stage = error.stage
        retryable = error.retryable
        message_key = "creativeWorkflowMcp." + code.replace(".", "_")
    elif isinstance(error, CreativeWorkflowError):
        code = f"creative_workflow.{error.code}"
        stage = "workflow"
        retryable = error.code in {"workflow_busy", "workflow_revision_conflict"}
        message_key = error.message_key
        source = error.source
        location = list(error.location_segments)
    elif isinstance(error, AuthoringProjectError):
        code = f"authoring_project.{error.code}"
        stage = "project"
        retryable = error.code == "revision_conflict"
        message_key = error.message_key
        source = error.source
        location = list(error.location_segments)
    elif isinstance(error, AuthoringRenderError):
        code = f"authoring_render.{error.code}"
        stage = error.stage
        retryable = error.retryable
        message_key = error.message_key
        source = "render"
    elif isinstance(error, _McpAuthoringBoundaryError):
        code = error.code
        stage = error.stage
        retryable = error.retryable
        message_key = "authoringMcp." + code.replace(".", "_")
        source = "authoring"
    else:
        code = "creative_workflow.internal_error"
        stage = "internal"
        retryable = False
        message_key = "creativeWorkflowMcp.internal_error"
    result: dict[str, Any] = {
        "kind": _WORKFLOW_RESULT_KIND,
        "schema_version": _WORKFLOW_RESULT_VERSION,
        "ok": False,
        "operation": operation,
        "error": {
            "code": code,
            "message_key": message_key,
            "source": source,
            "stage": stage,
            "retryable": retryable,
            "location": {"segments": location},
        },
        "next_action": {
            "operation": "open_creative_workflow",
            "reason": "refresh_verified_workflow_state_after_failure",
        },
    }
    if safe_project_key is not None:
        result["project_key"] = safe_project_key
    if safe_workflow_id is not None:
        result["workflow_id"] = safe_workflow_id
    return result


def _validated_workflow_id(value: str) -> str:
    if not isinstance(value, str) or _WORKFLOW_ID_PATTERN.fullmatch(value) is None:
        raise _McpWorkflowBoundaryError("creative_workflow.invalid_workflow_id")
    return value


def _validated_workflow_revision(
    value: str | None,
    *,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or _AUTHORING_REVISION_PATTERN.fullmatch(value) is None
    ):
        raise _McpWorkflowBoundaryError(
            "creative_workflow.invalid_workflow_revision"
        )
    return value


def _agent_workflow_authority(snapshot: CreativeWorkflowSnapshot) -> str:
    authority = snapshot.detached_state().get("final_authority")
    if authority != "agent":
        raise _McpWorkflowBoundaryError(
            "creative_workflow.agent_authority_required",
            stage="authority",
        )
    return "agent"


def _open_expected_workflow(
    root: Path,
    *,
    workflow_id: str,
    expected_revision: str,
) -> CreativeWorkflowSnapshot:
    snapshot = open_creative_workflow_state(root, workflow_id=workflow_id)
    if snapshot.revision != expected_revision:
        raise CreativeWorkflowError("workflow_revision_conflict")
    return snapshot


def _official_constitution_registry(language: str) -> dict[str, dict[str, str]]:
    metadata = _OFFICIAL_CONSTITUTIONS.get(language)
    filename = _OFFICIAL_CONSTITUTION_FILENAMES.get(language)
    if metadata is None or filename is None:
        raise _McpWorkflowBoundaryError(
            "creative_workflow.constitution_language_unsupported"
        )
    document = (
        Path(__file__).resolve().parent
        / "_resources"
        / "constitutions"
        / filename
    )
    if not os.path.lexists(document):
        raise _McpWorkflowBoundaryError(
            "creative_workflow.constitution_unavailable",
            stage="constitution",
        )
    try:
        identity, payload = read_plain_file_bytes(
            document,
            maximum_bytes=1024 * 1024,
        )
    except (OSError, RuntimeError) as exc:
        raise _McpWorkflowBoundaryError(
            "creative_workflow.constitution_unsafe",
            stage="constitution",
        ) from exc
    if (
        not 1 <= identity.size <= 1024 * 1024
        or hashlib.sha256(payload).hexdigest() != metadata["content_sha256"]
    ):
        raise _McpWorkflowBoundaryError(
            "creative_workflow.constitution_integrity_mismatch",
            stage="constitution",
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeError as exc:
        raise _McpWorkflowBoundaryError(
            "creative_workflow.constitution_invalid_encoding",
            stage="constitution",
        ) from exc
    registry: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        match = _CONSTITUTION_CLAUSE_LINE.fullmatch(line)
        if match is None:
            continue
        clause_id = match.group("clause_id")
        if clause_id in registry:
            raise _McpWorkflowBoundaryError(
                "creative_workflow.constitution_duplicate_clause",
                stage="constitution",
            )
        registry[clause_id] = {
            "clause_id": clause_id,
            "title": match.group("title").strip(),
            "text": match.group("text").strip(),
        }
    try:
        revalidate_plain_file(identity)
    except OSError as exc:
        raise _McpWorkflowBoundaryError(
            "creative_workflow.constitution_unsafe",
            stage="constitution",
        ) from exc
    if len(registry) < 100:
        raise _McpWorkflowBoundaryError(
            "creative_workflow.constitution_registry_incomplete",
            stage="constitution",
        )
    return registry


def _validate_mcp_constitution_activation(
    constitution: dict | None,
    active_clauses: list[dict] | None,
) -> str | None:
    if constitution is None:
        return None
    if not isinstance(constitution, dict):
        return "custom"
    document_id = constitution.get("document_id")
    if document_id != _OFFICIAL_CONSTITUTION["document_id"]:
        return "custom"
    language = constitution.get("language")
    expected = _OFFICIAL_CONSTITUTIONS.get(language)
    if expected is None or constitution != expected:
        raise _McpWorkflowBoundaryError(
            "creative_workflow.official_constitution_binding_mismatch",
            stage="constitution",
        )
    registry = _official_constitution_registry(language)
    for clause in [] if active_clauses is None else active_clauses:
        clause_id = clause.get("clause_id") if isinstance(clause, dict) else None
        if clause_id not in registry:
            raise _McpWorkflowBoundaryError(
                "creative_workflow.constitution_clause_unknown",
                stage="constitution",
            )
    return "official"


def _workflow_failure_with_current(
    operation: str,
    error: BaseException,
    *,
    project_key: str,
    workflow_id: str,
    root: Path | None,
) -> dict[str, Any]:
    result = _workflow_failure(
        operation,
        error,
        project_key=project_key,
        workflow_id=workflow_id,
    )
    if root is None or _WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is None:
        return result
    try:
        snapshot = open_creative_workflow_state(root, workflow_id=workflow_id)
    except Exception:
        return result
    result["workflow"] = snapshot.to_dict()
    result["next_action"] = _workflow_next_action(
        snapshot,
        project_key=project_key,
    )
    return result


def _validated_authoring_project_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or _AUTHORING_PROJECT_KEY_PATTERN.fullmatch(value) is None
        or is_windows_reserved_filename(value)
    ):
        raise _McpAuthoringBoundaryError("authoring_path.invalid_project_key")
    return value


def _validated_authoring_revision(
    value: str | None,
    *,
    required: bool,
) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or _AUTHORING_REVISION_PATTERN.fullmatch(value) is None
    ):
        raise _McpAuthoringBoundaryError(
            "authoring_project.invalid_revision"
        )
    return value


def _validated_candidate_segment(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value in {".", ".."}
        or Path(value).name != value
        or any(character in value for character in '<>:"/\\|?*')
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value[-1] in {" ", "."}
    ):
        raise _McpAuthoringBoundaryError(
            f"authoring_candidate.invalid_{field}"
        )
    return value


def _authoring_namespace(*, create: bool) -> Path:
    namespace = OUTPUT_DIR / _AUTHORING_PROJECTS_DIRECTORY_NAME
    try:
        if create:
            output_identity = ensure_plain_directory_tree(OUTPUT_DIR)
            namespace_identity = ensure_authorized_child_directory(
                output_identity,
                _AUTHORING_PROJECTS_DIRECTORY_NAME,
            )
        else:
            if not os.path.lexists(namespace):
                raise _McpAuthoringBoundaryError(
                    "authoring_project.not_found",
                    stage="project",
                )
            namespace_identity = capture_plain_directory(namespace)
        return revalidate_plain_directory(namespace_identity)
    except _McpAuthoringBoundaryError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _McpAuthoringBoundaryError(
            "authoring_path.namespace_unsafe",
            stage="project",
        ) from exc


def _authoring_project_root(
    project_key: str,
    *,
    create_namespace: bool,
    require_existing: bool,
) -> Path:
    key = _validated_authoring_project_key(project_key)
    namespace = _authoring_namespace(create=create_namespace)
    root = namespace / key
    if not require_existing:
        return root
    if not os.path.lexists(root):
        raise _McpAuthoringBoundaryError(
            "authoring_project.not_found",
            stage="project",
        )
    try:
        identity = capture_plain_directory(root)
        resolved = revalidate_plain_directory(identity)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _McpAuthoringBoundaryError(
            "authoring_path.project_root_unsafe",
            stage="project",
        ) from exc
    if resolved.parent != namespace or resolved.name != key:
        raise _McpAuthoringBoundaryError(
            "authoring_path.project_root_unsafe",
            stage="project",
        )
    return resolved


def _authoring_project_descriptor(
    state: AuthoringProjectState,
) -> dict[str, Any]:
    return {
        "project_id": state.project_id,
        "title": state.title,
        "created_at_utc": state.created_at_utc,
        "updated_at_utc": state.updated_at_utc,
        "revision": state.revision,
        "document_revisions": dict(state.document_revisions),
    }


def _authoring_candidate_directory(
    project_key: str,
    *,
    work_id: str,
    candidate_id: str,
) -> tuple[PlainDirectoryIdentity, AuthoringProjectState, Path]:
    checked_work_id = _validated_candidate_segment(work_id, field="work_id")
    checked_candidate_id = _validated_candidate_segment(
        candidate_id,
        field="candidate_id",
    )
    root = _authoring_project_root(
        project_key,
        create_namespace=False,
        require_existing=True,
    )
    state = open_authoring_project_state(root)
    try:
        renders_identity = capture_plain_directory(
            root / AUTHORING_RENDERS_DIRECTORY_NAME
        )
        work_identity = capture_plain_directory(
            revalidate_plain_directory(renders_identity) / checked_work_id
        )
        candidate_identity = capture_plain_directory(
            revalidate_plain_directory(work_identity) / checked_candidate_id
        )
        directory = revalidate_plain_directory(candidate_identity)
        if (
            directory.parent != revalidate_plain_directory(work_identity)
            or directory.name != checked_candidate_id
            or revalidate_plain_directory(work_identity).parent
            != revalidate_plain_directory(renders_identity)
        ):
            raise OSError("candidate directory identity mismatch")
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.not_found_or_unsafe",
            stage="candidate",
        ) from exc
    return candidate_identity, state, root


def _load_authoring_candidate(
    project_key: str,
    *,
    work_id: str,
    candidate_id: str,
) -> tuple[
    Path,
    dict[str, Any],
    AuthoringProjectState,
    PlainDirectoryIdentity,
]:
    candidate_identity, current_state, root = _authoring_candidate_directory(
        project_key,
        work_id=work_id,
        candidate_id=candidate_id,
    )
    directory = revalidate_plain_directory(candidate_identity)
    try:
        verified_directory, manifest = load_candidate(
            directory,
            verify=True,
            expected_work_id=work_id,
            expected_candidate_id=candidate_id,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.verification_failed",
            stage="candidate",
        ) from exc
    try:
        if (
            verified_directory != directory
            or revalidate_plain_directory(candidate_identity) != directory
        ):
            raise OSError("candidate directory identity mismatch")
    except OSError as exc:
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.verification_failed",
            stage="candidate",
        ) from exc
    binding = manifest.get("authoring_project")
    if (
        not isinstance(binding, dict)
        or binding.get("project_id") != current_state.project_id
    ):
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.project_binding_mismatch",
            stage="candidate",
        )
    revision = binding.get("revision")
    if (
        not isinstance(revision, str)
        or _AUTHORING_REVISION_PATTERN.fullmatch(revision) is None
    ):
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.project_binding_mismatch",
            stage="candidate",
        )
    try:
        revision_state = open_authoring_project_state(root, revision=revision)
    except AuthoringProjectError as exc:
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.revision_unavailable",
            stage="candidate",
        ) from exc
    return verified_directory, manifest, revision_state, candidate_identity


def _read_bound_candidate_json(
    directory: Path,
    directory_identity: PlainDirectoryIdentity,
    binding: object,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(binding, dict):
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.evidence_binding_invalid",
            stage="candidate",
        )
    relative = binding.get("path")
    expected_sha256 = binding.get("sha256")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(expected_sha256, str)
        or _AUTHORING_REVISION_PATTERN.fullmatch(expected_sha256) is None
    ):
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.evidence_binding_invalid",
            stage="candidate",
        )
    raw = Path(relative)
    if (
        raw.is_absolute()
        or len(raw.parts) != 1
        or raw.name in {"", ".", ".."}
    ):
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.evidence_path_unsafe",
            stage="candidate",
        )
    lexical = directory / raw
    try:
        if revalidate_plain_directory(directory_identity) != directory:
            raise OSError("candidate directory identity mismatch")
        identity, payload = read_plain_file_bytes(
            lexical,
            maximum_bytes=_AUTHORING_EVIDENCE_MAX_BYTES,
        )
        if identity.size < 1:
            raise OSError("candidate evidence is empty")
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            raise ValueError("evidence hash mismatch")
        document = json.loads(payload.decode("utf-8"))
        revalidate_plain_file(identity)
        if revalidate_plain_directory(directory_identity) != directory:
            raise OSError("candidate directory identity mismatch")
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.evidence_verification_failed",
            stage="candidate",
        ) from exc
    if not isinstance(document, dict):
        raise _McpAuthoringBoundaryError(
            "authoring_candidate.evidence_verification_failed",
            stage="candidate",
        )
    return document


def _verified_candidate_workflow_status(
    project_root: Path,
    candidate_directory: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Separate a candidate's claim, historical authority and durable outcome."""

    claim = manifest.get("authoring_workflow")
    if claim is None:
        return {
            "workflow_claim_present": False,
            "workflow_authorized": False,
            "workflow_recorded": False,
            "workflow_accepted": False,
            "workflow_managed": False,
            "authoring_workflow": None,
            "workflow_status": None,
        }
    try:
        normalized = validate_workflow_authorization(claim, allow_none=False)
        assert normalized is not None
        status = inspect_workflow_candidate_status(
            project_root,
            candidate_path=candidate_directory,
        )
    except (CreativeWorkflowError, TypeError, ValueError):
        # A shape-valid self-assertion is not authority. Keep the candidate
        # inspectable as evidence, but never label the claim as managed.
        return {
            "workflow_claim_present": True,
            "workflow_authorized": False,
            "workflow_recorded": False,
            "workflow_accepted": False,
            "workflow_managed": False,
            "authoring_workflow": None,
            "workflow_status": None,
        }
    return {
        "workflow_claim_present": True,
        "workflow_authorized": status["workflow_authorized"],
        "workflow_recorded": status["workflow_recorded"],
        "workflow_accepted": status["workflow_accepted"],
        "workflow_managed": status["workflow_authorized"],
        "authoring_workflow": normalized,
        "workflow_status": status,
    }


@dataclass(frozen=True, slots=True)
class _ProjectCompilation:
    """One read-only score/roster compilation shared by MCP inspection tools."""

    score: Any | None
    roster: Any | None
    settings: ExpressionSettings | None
    plan: Any | None
    checks: dict[str, dict[str, Any]]
    issues: tuple[dict[str, Any], ...]
    project: dict[str, str | None]
    project_review: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.plan is not None and not any(
            issue.get("blocking", issue.get("severity") == "error") is True
            for issue in self.issues
        )


def _caps() -> dict[str, Any]:
    global _caps_cache
    if _caps_cache is None:
        _caps_cache = load_capabilities(CATALOG)
    return _caps_cache


def _trusted_set() -> set[str]:
    """Load and validate the curated palette; never silently fail open."""

    try:
        return set(load_trusted_instruments(ALLOWLIST_FILE, _caps()))
    except TrustPolicyError as exc:
        raise TrustPolicyError(
            f"{exc};trusted_only=true 已按 fail-closed 拒绝"
        ) from exc


def _formal_set() -> set[str]:
    """Return every public formal entry without conflating it with curation."""

    return {
        path
        for path, capability in _caps().items()
        if capability.quality_tier == "formal"
        and capability.license_status in {"approved", "grandfathered"}
        and capability.implementation_type != "soundfont"
    }


def _resolve_mcp_instrument_scope(
    instrument_scope: str | None,
    trusted_only: bool | None,
) -> tuple[str, set[str]]:
    """Resolve the new explicit scope and the legacy boolean alias.

    ``formal`` is the MCP default and means every public ``formal`` sound
    entry.  ``curated`` is the smaller creator-maintained palette.  The old
    ``trusted_only`` argument remains accepted so existing clients do not
    silently change meaning: true maps to curated, false maps to formal.
    """

    if trusted_only is not None and not isinstance(trusted_only, bool):
        raise TypeError("trusted_only 必须是布尔值或 null")
    legacy_scope = (
        None
        if trusted_only is None
        else "curated" if trusted_only else "formal"
    )
    if instrument_scope is not None:
        if not isinstance(instrument_scope, str):
            raise TypeError("instrument_scope 必须是字符串或 null")
        instrument_scope = instrument_scope.strip()
        if instrument_scope not in {"formal", "curated"}:
            raise ValueError(
                "instrument_scope 必须是 'formal' 或 'curated'"
            )
    if (
        legacy_scope is not None
        and instrument_scope is not None
        and legacy_scope != instrument_scope
    ):
        raise ValueError(
            "instrument_scope 与兼容参数 trusted_only 表达了冲突的范围"
        )
    resolved = instrument_scope or legacy_scope or "formal"
    allowed = _trusted_set() if resolved == "curated" else _formal_set()
    if not allowed:
        raise TrustPolicyError(f"MCP {resolved} 乐器范围为空;已拒绝继续")
    return resolved, allowed


def _variant_hints() -> dict[str, str]:
    """Return curated per-instrument usage hints from the same allowlist."""

    try:
        return load_variant_hints(ALLOWLIST_FILE)
    except TrustPolicyError:
        return {}


def _resolve_mcp_input(value: str) -> Path:
    """Authorise a local source path without granting ambient disk access."""

    return discover_mcp_input_policy(
        layout=_RUNTIME_LAYOUT,
    ).resolve_file(value)


def _articulation_range_contracts(cap: Any) -> dict[str, dict[str, Any]]:
    """Resolve every articulation to an Agent-friendly score-writing range.

    The legacy fields intentionally distinguish explicit declarations from
    inherited ranges.  That is useful for audits but easy for a score-writing
    Agent to misread.  This view resolves the inheritance while retaining its
    source, and pairs exact MIDI spans with readable pitch names.
    """

    explicit = dict(cap.articulation_playable_ranges)
    contracts: dict[str, dict[str, Any]] = {}
    for articulation in cap.articulations:
        ranges = cap.ranges_for(articulation)
        if articulation in explicit:
            source = "articulation_override"
        elif cap.playable_ranges:
            source = "instrument_playable_ranges"
        elif cap.note_min is not None and cap.note_max is not None:
            source = "instrument_note_bounds"
        else:
            source = "unspecified"
        contracts[articulation] = {
            "midi_ranges": [[low, high] for low, high in ranges],
            "note_ranges": [
                f"{pitch_name(low)}~{pitch_name(high)}"
                for low, high in ranges
            ],
            "source": source,
        }
    return contracts


def _range_diagnostic_summary(plan: Any) -> dict[str, Any]:
    """Compress per-note range contracts into an Agent-sized render receipt."""

    statuses: Counter[str] = Counter()
    attention: list[dict[str, Any]] = []
    by_executor_statuses: dict[str, Counter[str]] = {}
    by_executor_attention: dict[str, list[dict[str, Any]]] = {}
    by_executor_attention_count: Counter[str] = Counter()
    by_executor_contract_count: Counter[str] = Counter()
    attention_statuses = {
        "outside_hard_playable_range",
        "outside_candidate_high_quality",
        "profile_not_found",
        "quality_pending",
        "quality_rejected",
    }
    for part in plan.parts:
        executor_id = str(part.executor.executor_id)
        executor_statuses = by_executor_statuses.setdefault(
            executor_id,
            Counter(),
        )
        executor_attention = by_executor_attention.setdefault(
            executor_id,
            [],
        )
        for trace in part.trace:
            derivation = trace.get("推导")
            if not isinstance(derivation, dict):
                continue
            contract = derivation.get("音域合同")
            if not isinstance(contract, dict):
                continue
            status = str(contract.get("status", "unknown"))
            statuses[status] += 1
            executor_statuses[status] += 1
            by_executor_contract_count[executor_id] += 1
            if status not in attention_statuses:
                continue
            example = {
                "executor": executor_id,
                "bar": trace.get("小节"),
                "beat": trace.get("拍"),
                "pitch": trace.get("音"),
                "status": status,
                "profile_id": contract.get("profile_id"),
                "legacy_covered": contract.get("legacy_covered"),
            }
            by_executor_attention_count[executor_id] += 1
            if len(executor_attention) < 8:
                executor_attention.append(example)
            if len(attention) < 16:
                attention.append(example)
    by_executor = {
        executor_id: {
            "contract_count": by_executor_contract_count[executor_id],
            "status_counts": dict(
                sorted(by_executor_statuses[executor_id].items())
            ),
            "attention_count": by_executor_attention_count[executor_id],
            "attention_examples": by_executor_attention[executor_id],
            "attention_examples_truncated": (
                by_executor_attention_count[executor_id]
                > len(by_executor_attention[executor_id])
            ),
        }
        for executor_id in sorted(by_executor_statuses)
    }
    return {
        "mode": plan.expression.range_mode,
        "status_counts": dict(sorted(statuses.items())),
        "attention_count": sum(
            count
            for status, count in statuses.items()
            if status in attention_statuses
        ),
        "attention_examples": attention,
        "by_executor": by_executor,
        "semantics": (
            "compatibility 会保留旧可演奏范围并报告风险；strict_hq 对缺失、"
            "未批准、配置不匹配或超出当前高质量范围的音符直接拒绝。"
        ),
    }


def _assignment_instruments(assignment: dict) -> list[str]:
    """一个 assignment 涉及的所有乐器相对路径。

    普通声部走顶层 ``instrument``;**鼓组用 ``kit`` 把不同音符路由到不同打击
    乐器,没有顶层 instrument**。两种都要收齐,预检才不会把 kit 声部误判成
    "不可用乐器 None"。kit 的值可以是乐器路径字符串,或带 ``instrument`` 键的对象。
    """
    paths: list[str] = []
    top = assignment.get("instrument")
    if isinstance(top, str):
        paths.append(top)
    kit = assignment.get("kit")
    if isinstance(kit, dict):
        for value in kit.values():
            if isinstance(value, str):
                paths.append(value)
            elif isinstance(value, dict) and isinstance(value.get("instrument"), str):
                paths.append(value["instrument"])
    return paths


def _roster_instrument_problems(
    roster: dict,
    trusted_only: bool | None = None,
    instrument_scope: str | None = None,
) -> list[str]:
    """校验路径及 MCP 乐器范围；返回问题清单（空=通过）。

    默认 ``formal`` 范围允许全部公开正式声音入口；``curated`` 只允许作者
    策展子集。兼容参数 ``trusted_only`` 仍按 true=curated、false=formal 解析。
    两种范围都不会放开非 formal 测试工具、``quarantined`` 或本机 SoundFont。
    """
    # 保留这个原始 JSON 兼容入口供 MCP 与既有测试使用，但真正的策略只对
    # parse_roster_document 已解析出的 capability 执行。这样完整路径和唯一
    # 短名经过同一个 resolve_capability，不会出现核心认得、MCP 却说不存在。
    problems: list[str] = []
    assignments = roster.get("assignments", [])
    if not isinstance(assignments, list):
        return ["assignments must be a non-empty array"]
    for a in assignments:
        if not isinstance(a, dict):
            continue
        paths = _assignment_instruments(a)
        if not paths:
            problems.append(f"{a.get('executor_id', '?')}(既无 instrument 也无 kit)")
    if problems:
        return problems
    # 旧的兼容辅助函数只做“涉及哪些乐器”的预检，部分调用方测试数据没有
    # part（正式 render 随后仍会严格解析并拒绝）。为保持该入口的既有契约，
    # 仅在这份临时副本里补一个不会碰撞的声部 id，再交给统一解析/策略路径。
    normalized = dict(roster)
    normalized["assignments"] = [
        (
            {**assignment, "part": f"__availability_preflight_{position}"}
            if isinstance(assignment, dict) and not str(assignment.get("part", "")).strip()
            else assignment
        )
        for position, assignment in enumerate(assignments)
    ]
    try:
        parsed = parse_roster_document(normalized, _caps())
    except Exception as exc:
        # MCP 工具返回可修正的结构化错误，不让无效编制把服务调用本身打断。
        return [str(exc)]
    try:
        _scope, allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return [f"乐器范围配置错误: {exc}"]
    non_formal = sorted(
        {
            executor.capability.relative_path
            for executor in parsed.executors
            if executor.capability.quality_tier != "formal"
        }
    )
    if non_formal:
        return [
            f"{path}(不是 MCP 公开 formal 正式声音入口)"
            for path in non_formal
        ]
    return list(
        roster_availability_problems(
            parsed,
            trusted_only=True,
            trusted_instruments=allowed,
        )
    )


def _collaboration_warnings(roster: Any) -> list[str]:
    """Return non-blocking mix/context warnings for an already parsed roster."""

    executors = tuple(roster.executors)
    warnings: list[str] = []
    if len(executors) <= 1:
        return warnings
    if any(
        executor.capability.relative_path == "世界乐器/西塔琴"
        for executor in executors
    ):
        warnings.append(
            "西塔琴在既有组合试听中电平偏轻；请按当前作品角色试听分轨与总线，"
            "再决定是否使用 gain_db 或自动化。"
        )
    if any(
        executor.capability.relative_path == "管弦乐/弦乐组/大提琴"
        and getattr(executor, "role", None) is not None
        and getattr(executor.role, "prominence", None) == "background"
        for executor in executors
    ):
        warnings.append(
            "背景大提琴在既有试听中出现过长尾与低中频遮蔽候选；"
            "请按当前作品试听分轨、目标前景声部与总线后决定是否调整。"
        )
    return warnings


def _canonical_json_sha256(value: object) -> str | None:
    """Hash JSON data without accepting non-portable NaN/Infinity values."""

    try:
        return canonical_json_sha256(value)
    except (TypeError, ValueError):
        return None


def _is_lower_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _issue(
    *,
    severity: str,
    code: str,
    stage: str,
    message: object,
    **details: Any,
) -> dict[str, Any]:
    return build_issue(
        severity=severity,
        code=code,
        stage=stage,
        message=message,
        **details,
    )


def _resolve_mcp_render_profile(
    *,
    render_profile: dict | None,
    seed: int | None,
    expression: str | None,
    range_mode: str | None,
    normalize_peak_db: float | None,
    hall: bool | None,
    master_gain_db: float | None,
    space_config: dict | None,
    collaboration_mode: str | None,
    write_stems: bool | None,
    use_stem_cache: bool | None,
    refresh_stem_cache: bool | None,
) -> RenderProfile:
    """Resolve the exact profile shared by MCP preflight and rendering."""

    if hall is not None and space_config is not None:
        raise ValueError("hall 与 space_config 不能同时覆盖 render_profile")
    explicit_space: SpaceConfig | bool | None
    if space_config is not None:
        explicit_space = SpaceConfig.from_dict(space_config)
        # ``SpaceConfig.from_dict`` accepts an explicit disabled object.  The
        # profile override API uses ``False`` to preserve that distinction.
        if explicit_space is None:
            explicit_space = False
    elif hall is not None:
        explicit_space = SpaceConfig() if hall else False
    else:
        explicit_space = None
    return profile_with_overrides(
        parse_render_profile(render_profile),
        seed=seed,
        expression=expression,
        range_mode=range_mode,
        normalize_peak_db=normalize_peak_db,
        master_gain_db=master_gain_db,
        space=explicit_space,
        collaboration_mode=collaboration_mode,
        write_stems=write_stems,
        use_stem_cache=use_stem_cache,
        refresh_stem_cache=refresh_stem_cache,
    )


def _safe_project_review(
    plan: Any | None,
    roster: Any | None,
    *,
    binding: dict[str, Any],
) -> dict[str, Any]:
    """Run creator review without allowing a diagnostic failure to block."""

    if plan is None or roster is None:
        report = build_review_report([], binding=binding)
        report["diagnostics"] = {"status": "not_run"}
        return report
    return build_project_review_safely(plan, roster, binding=binding)


def _compile_project(
    score: dict,
    roster: dict,
    *,
    expression: str,
    seed: int,
    range_mode: str,
    instrument_scope: str,
    write_stems: bool = True,
    space: SpaceConfig | None = None,
    collaboration_mode: str | None = None,
    stem_cache_enabled: bool = False,
) -> _ProjectCompilation:
    """Compile a project entirely in memory and retain every independent issue.

    This is deliberately not a light wrapper around :func:`render`: it never
    creates an output directory, opens an audio asset, instantiates an
    instrument backend, or calls ``render_plan``.
    """

    checks: dict[str, dict[str, Any]] = {
        "settings": {"status": "not_run"},
        "score_document": {"status": "not_run"},
        "score_time_coordinates": {"status": "not_run"},
        "resource_limits": {"status": "not_run"},
        "roster_document": {"status": "not_run"},
        "availability_policy": {"status": "not_run"},
        "cross_document": {"status": "not_run"},
        "performance_plan": {"status": "not_run"},
        "resources": {
            "status": "not_run",
            "level": "catalog_only",
            "ready_to_render": None,
            "reason_code": "audio_assets_not_opened",
        },
    }
    issues: list[dict[str, Any]] = []
    score_document = None
    roster_document = None
    settings = None
    plan = None
    normalized_seed: int | None = None
    try:
        resolved_scope, allowed_instruments = (
            _resolve_mcp_instrument_scope(instrument_scope, None)
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        resolved_scope = instrument_scope
        allowed_instruments = set()
        checks["availability_policy"] = {"status": "failed"}
        issues.append(
            _issue(
                severity="error",
                code="instrument.scope_invalid",
                stage="availability_policy",
                message=exc,
            )
        )

    try:
        normalized_seed = int(seed)
        settings = ExpressionSettings.from_dict(
            {
                "mode": expression,
                "range_mode": range_mode,
                "humanize": {"seed": normalized_seed},
            }
        )
    except Exception as exc:
        checks["settings"] = {"status": "failed"}
        issues.append(
            _issue(
                severity="error",
                code="settings.invalid",
                stage="settings",
                message=exc,
            )
        )
    else:
        checks["settings"] = {"status": "passed"}

    try:
        score_document = parse_score_document(score)
    except Exception as exc:
        checks["score_document"] = {"status": "failed"}
        checks["score_time_coordinates"] = {
            "status": "skipped",
            "blocked_by": ["score_document"],
        }
        issues.append(
            _issue(
                severity="error",
                code="score.parse_failed",
                stage="score_document",
                message=exc,
            )
        )
    else:
        checks["score_document"] = {"status": "passed"}
        try:
            validate_score_time_coordinates(score_document)
        except Exception as exc:
            checks["score_time_coordinates"] = {"status": "failed"}
            issues.append(
                _issue(
                    severity="error",
                    code="score.time_coordinate_invalid",
                    stage="score_time_coordinates",
                    message=exc,
                )
            )
        else:
            checks["score_time_coordinates"] = {"status": "passed"}
            try:
                score_resource_summary = validate_score_resource_limits(
                    score,
                    score_document,
                )
            except Exception as exc:
                checks["resource_limits"] = {"status": "failed"}
                issues.append(
                    _issue(
                        severity="error",
                        code=getattr(
                            exc,
                            "code",
                            "limits.score_invalid",
                        ),
                        stage="resource_limits",
                        message=exc,
                        actual=getattr(exc, "actual", None),
                        limit=getattr(exc, "limit", None),
                    )
                )
            else:
                checks["resource_limits"] = {
                    "status": "passed",
                    **score_resource_summary,
                }

    try:
        roster_document = parse_roster_document(roster, _caps())
    except Exception as exc:
        checks["roster_document"] = {"status": "failed"}
        checks["availability_policy"] = {
            "status": "skipped",
            "blocked_by": ["roster_document"],
        }
        issues.append(
            _issue(
                severity="error",
                code="roster.parse_failed",
                stage="roster_document",
                message=exc,
            )
        )
    else:
        checks["roster_document"] = {"status": "passed"}
        try:
            availability = roster_availability_problems(
                roster_document,
                trusted_only=True,
                trusted_instruments=allowed_instruments,
            )
        except Exception as exc:
            availability = (str(exc),)
        if availability:
            checks["availability_policy"] = {"status": "failed"}
            issues.extend(
                _issue(
                    severity="error",
                    code="instrument.unavailable",
                    stage="availability_policy",
                    message=problem,
                )
                for problem in availability
            )
        else:
            checks["availability_policy"] = {"status": "passed"}

    cross_errors: list[dict[str, Any]] = []
    if score_document is None or roster_document is None:
        blocked_by = []
        if score_document is None:
            blocked_by.append("score_document")
        if roster_document is None:
            blocked_by.append("roster_document")
        checks["cross_document"] = {
            "status": "skipped",
            "blocked_by": blocked_by,
        }
    else:
        score_parts = {part.id for part in score_document.parts}
        assigned_parts = {
            executor.part_id for executor in roster_document.executors
        }
        dropped_parts = set(roster_document.dropped_parts)
        for part_id in sorted(assigned_parts - score_parts):
            cross_errors.append(
                _issue(
                    severity="error",
                    code="cross.unknown_assigned_part",
                    stage="cross_document",
                    message=f"编制引用了总谱中不存在的声部 {part_id!r}",
                    part_id=part_id,
                )
            )
        for part_id in sorted(dropped_parts - score_parts):
            cross_errors.append(
                _issue(
                    severity="error",
                    code="cross.unknown_dropped_part",
                    stage="cross_document",
                    message=f"drop_parts 引用了总谱中不存在的声部 {part_id!r}",
                    part_id=part_id,
                )
            )
        for part_id in sorted(
            score_parts - assigned_parts - dropped_parts
        ):
            cross_errors.append(
                _issue(
                    severity="error",
                    code="cross.unassigned_part",
                    stage="cross_document",
                    message=f"总谱声部 {part_id!r} 既未指派乐器也未显式丢弃",
                    part_id=part_id,
                )
            )
        issues.extend(cross_errors)
        checks["cross_document"] = {
            "status": "failed" if cross_errors else "passed"
        }

    blocking_stages = [
        name
        for name in (
            "settings",
            "score_document",
            "score_time_coordinates",
            "resource_limits",
            "roster_document",
            "availability_policy",
            "cross_document",
        )
        if checks[name]["status"] != "passed"
    ]
    if blocking_stages:
        checks["performance_plan"] = {
            "status": "skipped",
            "blocked_by": blocking_stages,
        }
    else:
        try:
            plan = build_plan(
                score_document,
                roster_document,
                settings,
            )
        except Exception as exc:
            checks["performance_plan"] = {"status": "failed"}
            issues.append(
                _issue(
                    severity="error",
                    code="performance.compile_failed",
                    stage="performance_plan",
                    message=exc,
                )
            )
        else:
            try:
                render_resource_summary = (
                    validate_render_request_resource_limits(
                        plan,
                        write_stems=write_stems,
                        space=space,
                        collaboration_mode=collaboration_mode,
                        stem_cache_enabled=stem_cache_enabled,
                    )
                )
            except Exception as exc:
                preflight = getattr(exc, "preflight", None)
                checks["resource_limits"] = {
                    **checks["resource_limits"],
                    **(preflight if isinstance(preflight, dict) else {}),
                    "status": "failed",
                }
                checks["performance_plan"] = {"status": "passed"}
                issues.append(
                    _issue(
                        severity="error",
                        code=getattr(
                            exc,
                            "code",
                            "limits.plan_invalid",
                        ),
                        stage="resource_limits",
                        message=exc,
                        actual=getattr(exc, "actual", None),
                        limit=getattr(exc, "limit", None),
                    )
                )
            else:
                checks["performance_plan"] = {"status": "passed"}
                checks["resource_limits"] = {
                    **checks["resource_limits"],
                    **render_resource_summary,
                }
    settings_binding = {
        "expression": expression,
        "seed": normalized_seed if normalized_seed is not None else seed,
        "range_mode": range_mode,
        "trusted_only": resolved_scope == "curated",
        "instrument_scope": resolved_scope,
    }
    project_binding = {
        "score": score,
        "roster": roster,
        "settings": settings_binding,
    }
    project: dict[str, str | None] = {
        "score_sha256": _canonical_json_sha256(score),
        "roster_sha256": _canonical_json_sha256(roster),
        "plan_input_sha256": _canonical_json_sha256(project_binding),
        "performance_plan_sha256": (
            _canonical_json_sha256(plan.to_dict())
            if plan is not None
            else None
        ),
    }
    project_review = _safe_project_review(
        plan,
        roster_document,
        binding=project,
    )
    stage_order = {
        name: index
        for index, name in enumerate(
            (
                "settings",
                "score_document",
                "score_time_coordinates",
                "resource_limits",
                "roster_document",
                "availability_policy",
                "cross_document",
                "performance_plan",
            )
        )
    }
    issues.sort(
        key=lambda item: (
            stage_order.get(str(item.get("stage")), 999),
            str(item.get("code", "")),
            str(item.get("part_id", "")),
            str(item.get("message", "")),
        )
    )
    return _ProjectCompilation(
        score=score_document,
        roster=roster_document,
        settings=settings,
        plan=plan,
        checks=checks,
        issues=tuple(issues),
        project=project,
        project_review=project_review,
    )


def _bounded_limit(value: int, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer between 1 and 256")
    limit = int(value)
    if not 1 <= limit <= 256:
        raise ValueError(f"{field_name} must be between 1 and 256")
    return limit


def _validation_summary(compilation: _ProjectCompilation) -> dict[str, Any] | None:
    score = compilation.score
    roster = compilation.roster
    plan = compilation.plan
    if score is None or roster is None or plan is None:
        return None

    logical_end = 0.0
    for part in score.parts:
        for note in part.notes:
            meter = score.tempo_map.entry_at_bar(note.bar)
            end_quarter = (
                score.tempo_map.quarter_at(note.bar, note.beat)
                + note.duration_beats * meter.quarters_per_beat
            )
            logical_end = max(
                logical_end,
                score.tempo_map.seconds_at_quarter(end_quarter),
            )
    scheduled_gate_end = 0.0
    for part in plan.parts:
        for event in part.performance.get("events", []):
            if event.get("type") == "note_off":
                scheduled_gate_end = max(
                    scheduled_gate_end,
                    float(event.get("time", 0.0)),
                )
    return {
        "score_schema_version": score.schema_version,
        "score_part_count": len(score.parts),
        "assigned_part_count": len(
            {executor.part_id for executor in roster.executors}
        ),
        "dropped_part_count": len(roster.dropped_parts),
        "executor_count": len(roster.executors),
        "source_event_count": sum(
            len(part.notes) for part in score.parts
        ),
        "planned_note_count": sum(
            len(part.trace) for part in plan.parts
        ),
        "logical_music_end_seconds": round(logical_end, 9),
        "scheduled_gate_end_seconds": round(scheduled_gate_end, 9),
        "tail_seconds": score.tail_seconds,
        "total_plan_seconds": round(plan.duration_seconds, 9),
    }


def _render_preflight_summary(
    compilation: _ProjectCompilation,
) -> dict[str, Any]:
    resource_check = compilation.checks["resource_limits"]
    render_parameters = resource_check.get("render_parameters")
    if not isinstance(render_parameters, dict):
        return {
            "status": "not_run",
            "passed": None,
            "reason_code": "performance_plan_not_available",
        }
    # Keep this document byte-for-byte shape-compatible with the report
    # returned by the common preflight gate.  ``resource_limits`` also holds
    # score-document counters, which deliberately stay outside this render
    # report.
    fields = (
        "duration_seconds",
        "sample_rate",
        "executor_count",
        "frame_count",
        "estimated_audio_memory_bytes",
        "estimated_primary_output_bytes",
        "status",
        "passed",
        "render_parameters",
        "memory_model",
        "limits",
        "gates",
    )
    return {
        field: resource_check[field]
        for field in fields
        if field in resource_check
    }


def _instrument_policy_summary(
    compilation: _ProjectCompilation,
) -> list[dict[str, Any]]:
    roster = compilation.roster
    if roster is None:
        return []
    try:
        trusted = _trusted_set()
    except TrustPolicyError:
        trusted = None
    rows: dict[str, dict[str, Any]] = {}
    for executor in roster.executors:
        capability = executor.capability
        path = capability.relative_path
        if path in rows:
            continue
        rows[path] = {
            "instrument": path,
            "implementation_type": capability.implementation_type,
            "license_status": capability.license_status,
            "trusted": None if trusted is None else path in trusted,
            "collaboration_review_status": (
                capability.collaboration_review_status
            ),
            "decision": (
                "allowed"
                if compilation.checks["availability_policy"]["status"]
                == "passed"
                else "see_issues"
            ),
        }
    return [rows[path] for path in sorted(rows)]


def _issue_page(
    issues: tuple[dict[str, Any], ...],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    return paginate_issues(issues, limit)


def _effective_pitch_mode(capability: Any) -> str:
    return (
        capability.pitch_mode
        if capability.pitch_mode is not None
        else "pitched" if capability.pitched else "unspecified"
    )


def _instrument_catalog_item(
    capability: Any,
    *,
    curated: set[str] | None,
    variant_hint: str | None,
    detail_level: str,
) -> dict[str, Any]:
    lo = (
        pitch_name(capability.note_min)
        if capability.note_min is not None
        else None
    )
    hi = (
        pitch_name(capability.note_max)
        if capability.note_max is not None
        else None
    )
    item: dict[str, Any] = {
        "instrument": capability.relative_path,
        "category": capability.relative_path.split("/")[0],
        "name": capability.name,
        "implementation_type": capability.implementation_type,
        "routing_class": capability.routing_class,
        "pitched": capability.pitched,
        "pitch_mode": _effective_pitch_mode(capability),
        "range": f"{lo}~{hi}" if lo and hi else None,
        "articulations": list(capability.articulations),
        "default_articulation": capability.default_articulation,
        "quality_tier": capability.quality_tier,
        "collaboration_review_status": (
            capability.collaboration_review_status
        ),
        "license_status": capability.license_status,
        "curated": (
            None
            if curated is None
            else capability.relative_path in curated
        ),
        "resource_state": "catalog_only",
        "readiness_tool": "check_project_readiness",
        "variant_hint": variant_hint,
    }
    if detail_level == "summary":
        return item
    capability_document = capability.to_dict()
    item.update(
        {
            "pitch_mode_declared": capability.pitch_mode is not None,
            "fixed_midi_note": capability.fixed_midi_note,
            "fixed_note": (
                pitch_name(capability.fixed_midi_note)
                if capability.fixed_midi_note is not None
                else None
            ),
            "ignores_pitch": capability.ignores_pitch,
            "note_min": capability.note_min,
            "note_max": capability.note_max,
            "playable_ranges": [
                [low, high] for low, high in capability.playable_ranges
            ],
            "duration_articulation_rules": capability_document[
                "duration_articulation_rules"
            ],
            "articulation_playable_ranges": {
                name: [[low, high] for low, high in ranges]
                for name, ranges in capability.articulation_playable_ranges
            },
            "articulation_range_contracts": (
                _articulation_range_contracts(capability)
            ),
            "range_contract_status": capability_document[
                "range_contract_status"
            ],
            "range_base_runtime_configuration": capability_document[
                "range_base_runtime_configuration"
            ],
            "range_profiles": capability_document["range_profiles"],
        }
    )
    return item


@mcp_tool(title="List instruments", annotations=_READ_ONLY_TOOL)
def list_instruments(
    trusted_only: StrictBool | None = None,
    pitched_only: StrictBool = False,
    instrument_scope: Literal["formal", "curated"] | None = None,
    category: _InstrumentQuery | None = None,
    routing_class: Literal["instrument", "percussion", "effect"] | None = None,
    articulation: _InstrumentQuery | None = None,
    pitch_mode: Literal["pitched", "ignore", "fixed", "unspecified"] | None = None,
    query: _InstrumentQuery | None = None,
    detail_level: Literal["summary", "full"] = "summary",
    offset: StrictInt = 0,
    limit: StrictInt = 32,
) -> dict[str, Any]:
    """列出 MCP 可调用乐器(配器前先调这个,照音域/奏法写编制)。

    默认 ``instrument_scope=formal`` 端出全部正式声音入口；``curated``
    返回作者策展子集。兼容参数 trusted_only 仍可使用：true=curated，
    false=formal。每项的 ``curated`` 字段独立标明是否属于策展子集。
    ``license_status=quarantined`` 与 ``type=soundfont`` 的本机兼容入口无论此
    参数为何值都不会列出。
    无参数调用返回 32 件摘要和下一页游标；可按条件缩小范围，选定乐器后再用
    ``detail_level=full`` 读取完整音域合同。每件给出音域(音名)、奏法、实现类型、
    质量层和可选的 ``variant_hint``。
    ``playable_ranges`` 是
    显式声明的全局分段音域，``articulation_playable_ranges`` 只列显式的
    奏法覆盖；未列出的奏法继承全局分段，未声明分段时继承 note_min/note_max。
    写谱时必须先选奏法，再直接读取
    ``articulation_range_contracts[奏法].midi_ranges``；这里已经把继承解析完，
    并附有可读的 ``note_ranges``。顶层 ``range`` 仅是整件乐器的总包络。
    ``range_profiles`` 若存在，会进一步区分物理范围、升调扩展与当前高质量
    候选范围；不存在时 ``range_contract_status`` 明确为 unmigrated。
    ``duration_articulation_rules`` 只列乐器明确授权的无记号短音替换合同；
    空列表表示不能凭奏法名字自动猜测。
    ``pitch_mode=pitched`` 按谱面音高发声；``ignore`` 用声明键区内的谱面键位
    选择既有样本/变体但不做十二平均律移调；``fixed`` 则任意谱面音高都触发
    ``fixed_midi_note``，此时 ``ignores_pitch=true``。
    """
    try:
        resolved_scope, allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.instrument_list",
            "schema_version": 1,
            "ok": False,
            "count": 0,
            "instruments": [],
            "issues": [
                _issue(
                    severity="error",
                    code="instrument.scope_invalid",
                    stage="availability_policy",
                    message=exc,
                )
            ],
        }
    try:
        curated = _trusted_set()
        curation_state = "available"
    except TrustPolicyError:
        curated = None
        curation_state = "unavailable"
    try:
        page_limit = _bounded_limit(limit, "limit")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset 必须是非负整数")
        normalized_filters = {
            "category": None if category is None else category.strip(),
            "articulation": (
                None if articulation is None else articulation.strip()
            ),
            "query": None if query is None else query.strip(),
        }
        for name, value in normalized_filters.items():
            if value == "":
                raise ValueError(f"{name} 不能是空白字符串")
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.instrument_list",
            "schema_version": 1,
            "ok": False,
            "instrument_scope": resolved_scope,
            "count": 0,
            "instruments": [],
            "issues": [
                _issue(
                    severity="error",
                    code="instrument_catalog.query_invalid",
                    stage="catalog_query",
                    message=exc,
                )
            ],
        }
    variant_hints = _variant_hints()
    matching = []
    for cap in _caps().values():
        if cap.relative_path not in allowed:
            continue
        if pitched_only and not cap.pitched:
            continue
        if (
            normalized_filters["category"] is not None
            and cap.relative_path.split("/")[0]
            != normalized_filters["category"]
        ):
            continue
        if routing_class is not None and cap.routing_class != routing_class:
            continue
        if (
            normalized_filters["articulation"] is not None
            and not cap.supports(normalized_filters["articulation"])
        ):
            continue
        if pitch_mode is not None and _effective_pitch_mode(cap) != pitch_mode:
            continue
        if normalized_filters["query"] is not None:
            needle = normalized_filters["query"].casefold()
            haystacks = (
                cap.relative_path,
                cap.name,
                cap.implementation_type,
                *cap.articulations,
            )
            if not any(needle in value.casefold() for value in haystacks):
                continue
        matching.append(cap)
    matching.sort(key=lambda cap: cap.relative_path)
    page = matching[offset : offset + page_limit]
    items = [
        _instrument_catalog_item(
            cap,
            curated=curated,
            variant_hint=variant_hints.get(cap.relative_path),
            detail_level=detail_level,
        )
        for cap in page
    ]
    next_offset = (
        offset + len(items)
        if offset + len(items) < len(matching)
        else None
    )
    note = (
        "当前为作者策展子集；使用 instrument_scope=formal 可查看全部正式入口"
        if resolved_scope == "curated"
        else (
            "当前为全部 formal 正式声音入口；formal=单音色独立测试通过；"
            "curated 字段标明作者策展子集"
        )
    )
    return {
        "kind": "tianlai.instrument_list",
        "schema_version": 1,
        "ok": True,
        "instrument_scope": resolved_scope,
        "detail_level": detail_level,
        "curation_state": curation_state,
        "curated_count": None if curated is None else len(curated),
        "catalog_count": len(allowed),
        "matched_count": len(matching),
        "count": len(items),
        "offset": offset,
        "limit": page_limit,
        "next_offset": next_offset,
        "has_more": next_offset is not None,
        "filters": {
            **normalized_filters,
            "routing_class": routing_class,
            "pitch_mode": pitch_mode,
            "pitched_only": pitched_only,
        },
        "note": note,
        "agent_writing_rule": (
            "先选择 articulation，再按该乐器 articulation_range_contracts"
            "[articulation].midi_ranges 写音符；note_ranges 供人阅读。"
            "这是 compatibility 的基础可演奏范围；顶层 range 只是整件乐器的"
            "总包络，不能代替具体奏法音域。strict_hq 还须匹配 range_profiles。"
        ),
        "range_semantics": (
            "playable_ranges 仅含显式全局分段；"
            "articulation_playable_ranges 仅含显式奏法覆盖；未列奏法继承全局分段，"
            "没有显式分段时继承 note_min/note_max；range_profiles 若存在，"
            "会把物理/兼容扩展与当前高质量候选范围分开；"
            "articulation_range_contracts 已为每个奏法解析上述继承，"
            "应作为 AI 首次写谱的直接入口；duration_articulation_rules "
            "为空时不得凭 accent/normal 名字推断自动短音替换"
        ),
        "pitch_mode_semantics": {
            "pitched": "按谱面音高移调或选择对应音高样本",
            "ignore": (
                "谱面键位选择既有打击样本/变体且须落在声明键区；kit 可用 "
                "transpose 进入该键区，后端不按十二平均律移调"
            ),
            "fixed": (
                "任意谱面音高都触发 fixed_midi_note；"
                "ignores_pitch=true"
            ),
        },
        "routing_class_semantics": {
            "instrument": "普通乐器入口，可直接用于单件 assignment",
            "percussion": "打击乐入口，可单件试听或在 kit 中按谱面符头逐键路由",
            "effect": "环境与拟音入口，可作为 ambience 或 effect 声部使用",
        },
        "instruments": items,
    }


@mcp_tool(title="Diagnose Tianlai runtime", annotations=_READ_ONLY_TOOL)
def diagnose_runtime(
    check_level: Literal["quick", "references"] = "quick",
    max_issues: StrictInt = 32,
) -> dict[str, Any]:
    """检查当前 MCP 运行时、平台、目录、资源汇总与可选能力。

    ``quick`` 检查清单显式引用；``references`` 还会展开专用 SFZ 的样本引用。
    两种模式都严格被动：不加载原生库、不启动外部程序、不创建临时文件，也不
    联网、下载、安装或返回任何本机绝对路径。目录可写性只是无写入权限估计。
    """

    try:
        limit = _bounded_limit(max_issues, "max_issues")
        return collect_runtime_diagnosis(
            _RUNTIME_LAYOUT,
            check_level=check_level,
            max_issues=limit,
        )
    except ValueError:
        return {
            "kind": "tianlai.runtime_diagnosis_result",
            "schema_version": 1,
            "ok": False,
            "status": "error",
            "issues": [
                _issue(
                    severity="error",
                    code="diagnosis.invalid_request",
                    stage="settings",
                    message="Runtime diagnosis settings are invalid.",
                )
            ],
        }
    except Exception:
        return {
            "kind": "tianlai.runtime_diagnosis_result",
            "schema_version": 1,
            "ok": False,
            "status": "error",
            "issues": [
                _issue(
                    severity="error",
                    code="diagnosis.failed",
                    stage="runtime",
                    message=(
                        "Runtime diagnosis failed without exposing local path "
                        "or loader details. Run tianlai-doctor locally for the "
                        "operator-only report."
                    ),
                )
            ],
        }


@mcp_tool(title="Plan resource restore", annotations=_READ_ONLY_TOOL)
def plan_resource_restore(
    instrument_ids: _ResourceSelectorList | None = None,
    family_ids: _ResourceSelectorList | None = None,
    groups: _ResourceSelectorList | None = None,
    max_items: StrictInt = 64,
) -> dict[str, Any]:
    """只读规划资源恢复；不联网、不下载、不解压、不安装。

    可按项目乐器 ID、冻结资源族或资源组选择，三类选择取并集；全部省略时返回
    15 个冻结资源族的完整计划。结果保留体积、缓存状态和许可证义务，但不包含
    上游 URL、commit、本机路径或可直接执行的 shell 命令。
    """

    try:
        limit = _bounded_limit(max_items, "max_items")
        return build_safe_resource_restore_plan(
            _RUNTIME_LAYOUT,
            instrument_ids=instrument_ids,
            family_ids=family_ids,
            groups=groups,
            max_items=limit,
        )
    except ValueError:
        return {
            "kind": "tianlai.resource_restore_plan_result",
            "schema_version": 1,
            "ok": False,
            "status": "blocked",
            "network": False,
            "persistent_writes": False,
            "downloads_started": False,
            "restore_started": False,
            "issues": [
                _issue(
                    severity="error",
                    code="restore_plan.invalid_request",
                    stage="settings",
                    message="Resource restore planning settings are invalid.",
                )
            ],
        }
    except Exception:
        return {
            "kind": "tianlai.resource_restore_plan_result",
            "schema_version": 1,
            "ok": False,
            "status": "blocked",
            "network": False,
            "persistent_writes": False,
            "downloads_started": False,
            "restore_started": False,
            "issues": [
                _issue(
                    severity="error",
                    code="restore_plan.failed",
                    stage="manifest",
                    message=(
                        "The frozen restore manifest could not be planned. Run "
                        "tianlai-doctor locally for operator-only details."
                    ),
                )
            ],
        }


@mcp_tool(title="Get score and roster format", annotations=_READ_ONLY_TOOL)
def score_and_roster_format() -> dict[str, Any]:
    """返回乐谱与编制的写法说明 + 一个最小可用示例。

    你(AI)据此直接写出 score 与 roster 两个 JSON 对象,再交给 render。
    铁律:bpm 恒数四分音符;beat/duration_beats 用拍号的拍单位(6/8 里一拍=八分)。
    velocity 在 (0,1];pitch 用科学音名如 "C4"/"F#3" 或 MIDI 数字。
    """
    example_score = {
        "schema_version": 1,
        "title": "示例", "sample_rate": 48000, "tail_seconds": 3.0,
        "tempo_map": [{"bar": 1, "beat": 1.0, "bpm": 72.0,
                       "beats_per_bar": 4, "beat_unit": 4}],
        "parts": [
            {"id": "Piano", "name": "Piano", "notes": [
                {"event_id": "piano-0001", "bar": 1, "beat": 1.0,
                 "duration_beats": 1.0, "pitch": "C4", "velocity": 0.5},
                {"event_id": "piano-0002", "bar": 1, "beat": 2.0,
                 "duration_beats": 1.0, "pitch": "E4", "velocity": 0.5},
                {"event_id": "piano-0003", "bar": 1, "beat": 3.0,
                 "duration_beats": 2.0, "pitch": "G4", "velocity": 0.6}]},
            {"id": "Flute", "name": "Flute", "notes": [
                {"event_id": "flute-0001", "bar": 1, "beat": 1.0,
                 "duration_beats": 1.0, "pitch": "C5", "velocity": 0.55,
                 "articulation": "short"}]},
        ],
    }
    example_roster = {
        "name": "示例编制",
        "collaboration": {
            "mode": "analyze",
            "analysis": {
                "metric": "overlap_active_rms",
                "window_ms": 400,
                "hop_ms": 100,
                "gate_dbfs": -60,
            },
            "balance_relations": [
                {
                    "subject": "Piano",
                    "reference": "Flute",
                    "target_offset_db": -4.0,
                    "tolerance_db": 2.0,
                    "max_suggestion_db": 3.0,
                }
            ],
        },
        "assignments": [
            {"part": "Piano", "executor_id": "1_钢琴", "instrument": "键盘乐器/钢琴",
             "gain_db": -4.0,
             "role": {"function": "harmony", "prominence": "midground"},
             "seat": {"azimuth_deg": -3, "distance_m": 2.5}},
            {"part": "Flute", "executor_id": "2_长笛", "instrument": "管弦乐/木管组/长笛",
             "gain_db": -6.0,
             "articulation_auto": False,
             "articulation_map": {"short": "staccato"},
             "role": {"function": "lead", "prominence": "foreground"},
             "gain_automation": [
                 {"bar": 1, "beat": 1.0, "offset_db": 0.0},
                 {"bar": 1, "beat": 3.0, "offset_db": 1.5},
             ],
             "seat": {"azimuth_deg": -18, "distance_m": 4.0}},
        ],
    }
    example_kit_score = {
        "schema_version": 1,
        "title": "鼓组逐键路由示例",
        "sample_rate": 48000,
        "tail_seconds": 2.0,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 100.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "Drums",
                "name": "Drums",
                "notes": [
                    {
                        "event_id": "drums-0001",
                        "bar": 1,
                        "beat": 1.0,
                        "duration_beats": 0.5,
                        "pitch": "C2",
                        "velocity": 0.75,
                    },
                    {
                        "event_id": "drums-0002",
                        "bar": 1,
                        "beat": 2.0,
                        "duration_beats": 0.5,
                        "pitch": "D2",
                        "velocity": 0.7,
                    },
                    {
                        "event_id": "drums-0003",
                        "bar": 1,
                        "beat": 3.0,
                        "duration_beats": 0.5,
                        "pitch": "F#2",
                        "velocity": 0.6,
                    },
                    {
                        "event_id": "drums-0004",
                        "bar": 1,
                        "beat": 4.0,
                        "duration_beats": 0.5,
                        "pitch": "D3",
                        "velocity": 0.65,
                    },
                ],
            }
        ],
    }
    example_kit_roster = {
        "name": "鼓组逐键路由示例",
        "assignments": [
            {
                "part": "Drums",
                "kit": {
                    "C2": "现代鼓组/底鼓",
                    "D2": "现代鼓组/边击军鼓",
                    "F#2": "现代鼓组/闭合踩镲",
                    "D3": {
                        "instrument": "现代鼓组/高音通鼓",
                        "transpose": 10,
                    },
                },
                "gain_db": -6.0,
                "role": {
                    "function": "rhythm",
                    "prominence": "midground",
                },
            }
        ],
    }
    return {
        "score_fields": {
            "schema_version": "新谱写 1；此时每个音符必须带全谱唯一的稳定 event_id。"
                              "旧谱可继续渲染，但局部编辑前应先调用 upgrade_score",
            "tempo_map": "至少一条,首条须在 bar1 beat1 且带 bpm/beats_per_bar/beat_unit;"
                         "后续条目可只带 bpm 做变速(rubato),小节中途变速用 beat!=1(不可带拍号)",
            "parts[].id": "声部标识,须与 roster 的 assignment.part 对应",
            "parts[].notes[]": "event_id(v1 必填且全谱唯一)、bar(1起)、beat(1起,拍单位)、"
                               "duration_beats、pitch、"
                               "可选 velocity(0,1]、可选 dynamic 记号、可选 articulation",
        },
        "roster_fields": {
            "assignments[].part": "对应 score 里的 part id",
            "assignments[].instrument": "来自 list_instruments 的 instrument 相对路径",
            "assignments[].kit": "打击声部的谱面符头 → 乐器路由；值可直接写乐器路径，"
                                 "也可写 {instrument,transpose}",
            "assignments[].transpose": "整个 assignment 的整数半音移调；普通乐器直接"
                                       "使用，kit 条目未单独设置时继承它",
            "assignments[].kit.*.transpose": "kit 先按原谱面符头选中路由，再把条目内"
                                             "整数半音加到后端演奏键位；它覆盖顶层值",
            "gain_db": "该声部电平(负值);seat.distance_m 越大在厅堂里越靠里",
            "gain_automation": "可选声部推子包络:[{bar,beat,offset_db},...];"
                               "首点必须 bar1 beat1,相邻点按真实时间在 dB 域线性插值。"
                               "它只改混音电平,不会像 velocity 一样改变音色",
            "role": "可选编制意图:{function,prominence,label?};function 可写 lead/"
                    "countermelody/harmony/pad/bass/rhythm/accent/texture/"
                    "ambience/effect/other，prominence 为 foreground/midground/"
                    "background。角色本身绝不自动改增益",
            "collaboration": "可选协奏诊断:{mode:manual|analyze|suggest,analysis,"
                             "part_groups?,balance_relations};关系端点可为 part id，"
                             "或创作者显式声明的非嵌套 part group。不得按名称猜组；"
                             "组只做求和分析，不是渲染总线。关系还须写目标相对 dB、"
                             "容差和建议上限，suggest 也不改音频",
            "dynamic_compression": "可选 0..1，把演奏 velocity 向 0.78 收拢；"
                                   "这是力度映射，不是音频压缩器",
            "duration_scale": "可选 0.1..2，缩放该声部音符发声时长，"
                              "可用于控制密集段落的尾音堆积",
            "articulation_auto": "可选布尔值；false 表示只使用谱面明确写入或"
                                 "articulation_map 映射后的奏法",
            "articulation_map": "可选的谱面记号 → 乐器奏法精确映射，例如 "
                                "{short:staccato}；音域检查使用映射后的奏法",
            "overrides": "可选受控乐器参数。release_seconds 缩短主释音；"
                         "大提琴密集旋律可用 release_tail_gain=0..1 缩放或"
                         "关闭独立离弦尾采样；sample_variant 只选已审定变体",
            "pan_and_seat": "pan 是 -1..1 静态平衡；不写时由 seat.azimuth_deg "
                            "决定。seat.distance_m 当前只影响共享厅堂送出",
        },
        "rules": [
            "bpm 恒数四分音符,与拍号无关",
            "beat/duration_beats 用拍号的拍单位(6/8:一拍=八分音符)",
            "小节内 beat 使用半开区间；4/4 只能写 1<=beat<5，下一小节写 bar+1 beat1",
            "移动、改音高、改力度或改时值时保留原 event_id；新音符分配新的唯一 ID",
            "普通声部一件乐器一个 assignment；鼓组用一个 kit assignment 按"
            "符头展开执行器；同一乐器不要重复开实例",
            "先 list_instruments 确认音域,别写出乐器够不到的音",
            "fixed 会把任意谱面音高送到 fixed_midi_note；ignore 以键位选择原生"
            "样本/变体，必要时在 kit 条目中用 transpose 进入声明键区",
            "不要从乐器名猜主次；在 roster.role 和 balance_relations 中显式声明",
            "不要从 Piano L/R、乐器名或轨道顺序猜组合端点；只有 roster 明确写入"
            " collaboration.part_groups 才可按组分析",
        ],
        "example_score": example_score,
        "example_roster": example_roster,
        "example_kit_score": example_kit_score,
        "example_kit_roster": example_kit_roster,
    }


@mcp_tool(title="Import MIDI", annotations=_READ_ONLY_TOOL)
def import_midi(midi_path: str) -> dict[str, Any]:
    """把标准 MIDI 解析成 score 与待创作者确认的 roster 草稿。

    草稿保留 Program Change、CC7、CC10、CC11；不会自动选择天籁乐器或把
    MIDI 控制器猜成 dB。不产生音频。
    """
    from .midi_import import (  # 延迟导入,避免拉高启动
        build_roster_draft,
        read_midi,
    )
    try:
        path = _resolve_mcp_input(midi_path)
        score_doc, report = read_midi(path)
        parsed = parse_score_document(score_doc)
        roster_draft = build_roster_draft(score_doc, report)
    except InputPathPolicyError as exc:
        result = exc.to_result(stage="source_import")
        result["kind"] = "tianlai.midi_import_result"
        result["audio_rendered"] = False
        return result
    except (OSError, TypeError, ValueError, KeyError) as exc:
        return {"error": f"MIDI 导入失败:{exc}"}
    parts = []
    for p in parsed.parts:
        ps = [n.midi for n in p.notes]
        parts.append({"id": p.id, "notes": len(p.notes),
                      "range": f"{pitch_name(min(ps))}~{pitch_name(max(ps))}" if ps else None})
    return {
        "score": score_doc,
        "roster_draft": roster_draft,
        "parts": parts,
        "warnings": list(report.warnings),
        "report": report.to_dict(),
    }


@mcp_tool(title="Import MusicXML", annotations=_READ_ONLY_TOOL)
def import_musicxml(musicxml_path: str) -> dict[str, Any]:
    """把 MusicXML 总谱解析成天籁乐谱，支持 .musicxml/.xml 与压缩 .mxl。

    保留谱面声部、和弦、多声部时序、拍号、速度、力度、常见奏法、连音线和
    移调乐器的实音；返回警告会明确列出当前没有展开的谱面语义。不产生音频。
    """
    from .musicxml_import import read_musicxml  # 延迟导入，保持 MCP 冷启动轻量

    try:
        path = _resolve_mcp_input(musicxml_path)
        score_doc, report = read_musicxml(path)
        parsed = parse_score_document(score_doc)
    except InputPathPolicyError as exc:
        result = exc.to_result(stage="source_import")
        result["kind"] = "tianlai.musicxml_import_result"
        result["audio_rendered"] = False
        return result
    except (OSError, ValueError, KeyError) as exc:
        return {"error": f"MusicXML 导入失败:{exc}"}
    parts = []
    for part in parsed.parts:
        pitches = [note.midi for note in part.notes]
        parts.append(
            {
                "id": part.id,
                "name": part.name,
                "notes": len(part.notes),
                "range": (
                    f"{pitch_name(min(pitches))}~{pitch_name(max(pitches))}"
                    if pitches
                    else None
                ),
            }
        )
    return {
        "score": score_doc,
        "parts": parts,
        "warnings": list(report.warnings),
        "report": report.to_dict(),
    }


@mcp_tool(title="Import score project", annotations=_READ_ONLY_TOOL)
def import_score_project(
    source_path: str,
    trusted_only: StrictBool | None = None,
    candidate_limit: StrictInt = 8,
    instrument_scope: Literal["formal", "curated"] | None = None,
) -> dict[str, Any]:
    """统一导入 MIDI/MusicXML/MXL，返回有 Hash 绑定的三文档工程包。

    返回 score、可持久化的 import_report 与明确 ``executable=false`` 的
    roster_draft。候选乐器只作有界提示，不会自动写入正式编制，也不会产生
    文件或音频。
    """

    try:
        resolved_scope, allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.project_import_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="instrument.scope_invalid",
                    stage="availability_policy",
                    message=exc,
                )
            ],
        }
    try:
        path = _resolve_mcp_input(source_path)
        bundle = import_project_bundle(
            path,
            capabilities=_caps(),
            trusted_only=True,
            trusted_instruments=allowed,
            candidate_limit=candidate_limit,
        )
        routing_hints = bundle.get("roster_draft", {}).get("routing_hints")
        if isinstance(routing_hints, dict):
            # The lower-level importer uses ``trusted_only=True`` to enforce
            # the exact supplied allow-set.  Translate that implementation
            # detail back to the public MCP scope vocabulary before returning
            # the bundle so legacy true still means curated to clients.
            routing_hints["instrument_scope"] = resolved_scope
            routing_hints["trusted_only"] = resolved_scope == "curated"
    except InputPathPolicyError as exc:
        return {
            "kind": "tianlai.project_import_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [exc.to_issue(stage="source_import")],
        }
    except Exception as exc:
        return {
            "kind": "tianlai.project_import_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="project_import.failed",
                    stage="source_import",
                    message=exc,
                )
            ],
        }
    return {
        "kind": "tianlai.project_import_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "instrument_scope": resolved_scope,
        "bundle": bundle,
    }


@mcp_tool(title="Confirm instrument roster", annotations=_READ_ONLY_TOOL)
def confirm_roster(
    score: dict,
    roster_draft: dict,
    assignments: list[dict],
    trusted_only: StrictBool | None = None,
    name: str | None = None,
    collaboration: dict | None = None,
    instrument_scope: Literal["formal", "curated"] | None = None,
) -> dict[str, Any]:
    """把导入草稿提升为正式 roster；每个声部必须由创作者显式选择。

    普通声部提交 instrument，打击声部提交逐键 kit。工具会重验
    score/draft Hash、完整覆盖、乐器存在性、许可隔离与可信策略；候选提示、
    MIDI Program Change 或轨道名永远不会自动取得执行权限。
    """

    try:
        resolved_scope, allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.roster_confirmation_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="instrument.scope_invalid",
                    stage="availability_policy",
                    message=exc,
                )
            ],
        }
    try:
        roster = promote_imported_roster(
            roster_draft,
            score,
            assignments,
            _caps(),
            trusted_only=True,
            trusted_instruments=allowed,
            name=name,
            collaboration=collaboration,
        )
    except Exception as exc:
        return {
            "kind": "tianlai.roster_confirmation_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="roster_confirmation.failed",
                    stage="roster_confirmation",
                    message=exc,
                )
            ],
        }
    return {
        "kind": "tianlai.roster_confirmation_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "instrument_scope": resolved_scope,
        "roster": roster,
        "assignment_count": len(roster["assignments"]),
    }


@mcp_tool(title="Upgrade score", annotations=_READ_ONLY_TOOL)
def upgrade_score(score: dict) -> dict[str, Any]:
    """把 legacy score 升级为带稳定 event_id 的 score v1；不写文件、不渲染音频。

    已经是合法 v1 的输入会原样深拷贝返回。legacy 输入按原始数组遍历顺序分配
    ``event-000001`` 等稳定身份；保存升级结果后，后续移动、改音高、改力度或
    改时值时都应保留对应 ``event_id``。
    """

    before_version = (
        score.get("schema_version") if isinstance(score, dict) else None
    )
    try:
        upgraded = upgrade_legacy_score_to_v1(score)
        parsed = parse_score_document(upgraded)
        validate_score_time_coordinates(parsed)
    except Exception as exc:
        return {
            "kind": "tianlai.upgrade_score_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="score.upgrade_failed",
                    stage="score_document",
                    message=exc,
                )
            ],
        }
    changed = before_version != 1
    return {
        "kind": "tianlai.upgrade_score_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "changed": changed,
        "from_schema_version": before_version,
        "to_schema_version": 1,
        "score_sha256": _canonical_json_sha256(upgraded),
        "event_count": sum(len(part.notes) for part in parsed.parts),
        "score": upgraded,
        "warnings": (
            [
                "legacy 的人性化身份原先绑定数组下标；升级后绑定稳定 event_id，"
                "因此首次升级不承诺与 legacy 音频逐字节相同。保存 v1 后，"
                "未编辑事件在后续局部修改中会保持自己的随机身份。"
            ]
            if changed
            else []
        ),
    }


@mcp_tool(title="Read score slice", annotations=_READ_ONLY_TOOL)
def get_score_slice(score: dict, query: dict) -> dict[str, Any]:
    """按声部、event_id 或小节读取有界乐谱片段，不产生文件或音频。

    ``query`` 必须使用 ``kind=tianlai.score_slice_query``、
    ``schema_version=1``；可给 ``part_ids``、``event_ids``、
    ``bar_range={start,end}`` 和 ``max_notes``。匹配项过多时只返回有界摘要，
    不会把一个被截断的对象伪装成完整乐谱。
    """

    try:
        return slice_score(score, query)
    except ScoreOpsError as exc:
        return exc.to_dict()


@mcp_tool(title="Patch score", annotations=_READ_ONLY_TOOL)
def patch_score(score: dict, patch: dict) -> dict[str, Any]:
    """用稳定 event_id 原子修改 score-v1，返回新乐谱、Hash 与结构化差异。

    Patch 必须绑定 ``base_score_sha256``，支持 ``update_note``、
    ``delete_note`` 与 ``add_note``。更新和删除可用 ``expect`` 声明旧值前置
    条件；任何 Hash/旧值冲突都会整批拒绝，不会部分套用。新增 event_id 由
    引擎确定性分配，现有 event_id 不可修改。
    """

    try:
        return apply_score_patch(score, patch)
    except ScoreOpsError as exc:
        return exc.to_dict()


@mcp_tool(title="Compare score versions", annotations=_READ_ONLY_TOOL)
def compare_score_versions(
    before: dict,
    after: dict,
    max_changes: StrictInt = 256,
) -> dict[str, Any]:
    """按稳定 event_id 比较两份 score-v1；返回完整计数和有界差异样例。"""

    try:
        return compare_scores(
            before,
            after,
            max_changes=max_changes,
        )
    except ScoreOpsError as exc:
        return exc.to_dict()


def _validation_result_from_compilation(
    compilation: _ProjectCompilation,
    *,
    profile: RenderProfile,
    instrument_scope: str,
    issue_limit: int,
    kind: str = "tianlai.validate_project_result",
) -> dict[str, Any]:
    """Project one in-memory compilation into the shared validation contract."""

    issues, counts, truncated = _issue_page(
        compilation.issues,
        issue_limit,
    )
    resolved_profile = profile.to_dict()
    profile_sha256 = canonical_json_sha256(resolved_profile)
    return {
        "kind": kind,
        "schema_version": 1,
        "ok": compilation.ok,
        "audio_rendered": False,
        "project": compilation.project,
        "settings": {
            "expression": profile.expression,
            "seed": profile.seed,
            "range_mode": profile.range_mode,
            "instrument_scope": instrument_scope,
            "trusted_only": instrument_scope == "curated",
            "render_profile": resolved_profile,
            "render_profile_canonical_sha256": profile_sha256,
        },
        "render_handoff": {
            "render_profile": resolved_profile,
            "expected_render_profile_sha256": profile_sha256,
            "instrument_scope": instrument_scope,
        },
        "checks": compilation.checks,
        "render_preflight": _render_preflight_summary(compilation),
        "summary": _validation_summary(compilation),
        "instrument_policy": _instrument_policy_summary(compilation),
        "range_diagnostics": (
            _range_diagnostic_summary(compilation.plan)
            if compilation.plan is not None
            else None
        ),
        "self_check": summarize_issues(compilation.issues),
        "project_review": compilation.project_review,
        "issues": issues,
        "issue_counts": counts,
        "issues_truncated": truncated,
    }


@mcp_tool(title="Validate project", annotations=_READ_ONLY_TOOL)
def validate_project(
    score: dict,
    roster: dict,
    expression: str | None = None,
    seed: StrictInt | None = None,
    range_mode: str | None = None,
    trusted_only: StrictBool | None = None,
    max_issues: StrictInt = 64,
    render_profile: dict | None = None,
    normalize_peak_db: StrictFloat | None = None,
    hall: StrictBool | None = None,
    master_gain_db: StrictFloat | None = None,
    space_config: dict | None = None,
    collaboration_mode: str | None = None,
    write_stems: StrictBool | None = None,
    use_stem_cache: StrictBool | None = None,
    refresh_stem_cache: StrictBool | None = None,
    instrument_scope: Literal["formal", "curated"] | None = None,
) -> dict[str, Any]:
    """只编译并检查 score+roster，不实例化乐器、不产生音频或 output 文件。

    该入口检查文档结构、严格小节/拍坐标、许可与可信策略、跨文档声部路由以及
    指挥计划。资源状态明确标成 ``catalog_only``：本调用没有打开外部 WAV/SFZ，
    因而不会谎称资源已经 ready_to_render。资源预算按与 ``render`` 完全相同的
    render profile（包括共享厅堂、分轨、协奏分析和缓存设置）估算并明确报告
    当前参数是否过门。
    """

    try:
        limit = _bounded_limit(max_issues, "max_issues")
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.validate_project_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="query.invalid_limit",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    try:
        resolved_scope, _allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.validate_project_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="instrument.scope_invalid",
                    stage="availability_policy",
                    message=exc,
                )
            ],
        }
    try:
        profile = _resolve_mcp_render_profile(
            render_profile=render_profile,
            seed=seed,
            expression=expression,
            range_mode=range_mode,
            normalize_peak_db=normalize_peak_db,
            hall=hall,
            master_gain_db=master_gain_db,
            space_config=space_config,
            collaboration_mode=collaboration_mode,
            write_stems=write_stems,
            use_stem_cache=use_stem_cache,
            refresh_stem_cache=refresh_stem_cache,
        )
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.validate_project_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="settings.invalid",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    compilation = _compile_project(
        score,
        roster,
        expression=profile.expression,
        seed=profile.seed,
        range_mode=profile.range_mode,
        instrument_scope=resolved_scope,
        write_stems=profile.write_stems,
        space=profile.space,
        collaboration_mode=profile.collaboration_mode,
        stem_cache_enabled=profile.use_stem_cache,
    )
    return _validation_result_from_compilation(
        compilation,
        profile=profile,
        instrument_scope=resolved_scope,
        issue_limit=limit,
    )


@mcp_tool(title="Check project render readiness", annotations=_READ_ONLY_TOOL)
def check_project_readiness(
    score: dict,
    roster: dict,
    expression: str | None = None,
    seed: StrictInt | None = None,
    range_mode: str | None = None,
    trusted_only: StrictBool | None = None,
    max_issues: StrictInt = 64,
    verify_references: StrictBool = True,
    render_profile: dict | None = None,
    normalize_peak_db: StrictFloat | None = None,
    hall: StrictBool | None = None,
    master_gain_db: StrictFloat | None = None,
    space_config: dict | None = None,
    collaboration_mode: str | None = None,
    write_stems: StrictBool | None = None,
    use_stem_cache: StrictBool | None = None,
    refresh_stem_cache: StrictBool | None = None,
    instrument_scope: Literal["formal", "curated"] | None = None,
) -> dict[str, Any]:
    """检查项目合同及其实际引用乐器的资源引用，不渲染或解码音频。

    与 ``validate_project`` 的 ``catalog_only`` 检查不同，本工具只针对当前 roster
    实际使用的乐器检查 manifest/SFZ 引用。结果叫 ``ready_for_render_attempt``，
    因为它仍不会实例化乐器或声称音频后端已成功运行。无关乐器缺资源不会阻断
    当前项目；缺失资源会提供可直接交给 ``plan_resource_restore`` 的 handoff。
    平台与输出目录只做被动兼容/权限估计；实际写入和音频仍由 render 验证。
    """

    try:
        limit = _bounded_limit(max_issues, "max_issues")
    except (TypeError, ValueError):
        return {
            "kind": "tianlai.project_readiness_result",
            "schema_version": 1,
            "ok": False,
            "status": "blocked",
            "validation_ok": False,
            "resource_references_ready": False,
            "render_environment_ready": False,
            "ready_for_render_attempt": False,
            "audio_probe_performed": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="readiness.invalid_limit",
                    stage="settings",
                    message="Project readiness max_issues is invalid.",
                )
            ],
        }
    try:
        resolved_scope, _allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.project_readiness_result",
            "schema_version": 1,
            "ok": False,
            "status": "blocked",
            "validation_ok": False,
            "resource_references_ready": False,
            "render_environment_ready": False,
            "ready_for_render_attempt": False,
            "audio_probe_performed": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="instrument.scope_invalid",
                    stage="availability_policy",
                    message=exc,
                )
            ],
        }
    try:
        profile = _resolve_mcp_render_profile(
            render_profile=render_profile,
            seed=seed,
            expression=expression,
            range_mode=range_mode,
            normalize_peak_db=normalize_peak_db,
            hall=hall,
            master_gain_db=master_gain_db,
            space_config=space_config,
            collaboration_mode=collaboration_mode,
            write_stems=write_stems,
            use_stem_cache=use_stem_cache,
            refresh_stem_cache=refresh_stem_cache,
        )
    except (TypeError, ValueError):
        return {
            "kind": "tianlai.project_readiness_result",
            "schema_version": 1,
            "ok": False,
            "status": "blocked",
            "validation_ok": False,
            "resource_references_ready": False,
            "render_environment_ready": False,
            "ready_for_render_attempt": False,
            "audio_probe_performed": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="readiness.invalid_settings",
                    stage="settings",
                    message="Project readiness render settings are invalid.",
                )
            ],
        }

    compilation = _compile_project(
        score,
        roster,
        expression=profile.expression,
        seed=profile.seed,
        range_mode=profile.range_mode,
        instrument_scope=resolved_scope,
        write_stems=profile.write_stems,
        space=profile.space,
        collaboration_mode=profile.collaboration_mode,
        stem_cache_enabled=profile.use_stem_cache,
    )
    result = _validation_result_from_compilation(
        compilation,
        profile=profile,
        instrument_scope=resolved_scope,
        issue_limit=limit,
        kind="tianlai.project_readiness_result",
    )

    if compilation.roster is None:
        resources = {
            "kind": "tianlai.instrument_resource_readiness_result",
            "schema_version": 1,
            "ok": False,
            "status": "blocked",
            "resource_references_ready": False,
            "render_environment_ready": False,
            "verify_references": verify_references,
            "summary": {
                "required_count": 0,
                "ready_count": 0,
                "missing_count": 0,
                "invalid_count": 0,
                "unlisted_count": 0,
            },
            "instruments": [],
            "restore_plan_handoff": {"instrument_ids": []},
            "issues": [],
            "issue_counts": {},
            "issues_truncated": False,
            "blocked_by": ["roster_document"],
        }
    else:
        instrument_ids = sorted(
            {
                str(executor.capability.relative_path)
                for executor in compilation.roster.executors
            }
        )
        try:
            resources = collect_instrument_resource_readiness(
                _RUNTIME_LAYOUT,
                instrument_ids,
                verify_references=verify_references,
                max_issues=limit,
            )
        except Exception:
            resources = {
                "kind": "tianlai.instrument_resource_readiness_result",
                "schema_version": 1,
                "ok": False,
                "status": "invalid",
                "resource_references_ready": False,
                "render_environment_ready": False,
                "verify_references": verify_references,
                "summary": {
                    "required_count": len(instrument_ids),
                    "ready_count": 0,
                    "missing_count": 0,
                    "invalid_count": len(instrument_ids),
                    "unlisted_count": 0,
                },
                "instruments": [],
                "restore_plan_handoff": {"instrument_ids": []},
                "issues": [
                    _issue(
                        severity="error",
                        code="resource.diagnosis_failed",
                        stage="resources",
                        message=(
                            "Project resource diagnosis failed without exposing "
                            "local filesystem details."
                        ),
                    )
                ],
                "issue_counts": {"error": 1},
                "issues_truncated": False,
            }

    resource_issues = list(resources.get("issues", []))
    combined_issues = [*compilation.issues, *resource_issues]
    issues, _, issues_truncated = _issue_page(
        tuple(combined_issues),
        limit,
    )
    complete_issue_counts = Counter(
        str(issue.get("severity", "unknown")) for issue in compilation.issues
    )
    raw_resource_counts = resources.get("issue_counts")
    if isinstance(raw_resource_counts, dict):
        for severity, count in raw_resource_counts.items():
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                complete_issue_counts[str(severity)] += count
    else:
        complete_issue_counts.update(
            str(issue.get("severity", "unknown")) for issue in resource_issues
        )
    issue_counts = dict(sorted(complete_issue_counts.items()))
    combined_self_check = summarize_issues(combined_issues)
    complete_blocking_count = issue_counts.get("error", 0)
    combined_self_check["severity_counts"] = issue_counts
    combined_self_check["blocking_count"] = complete_blocking_count
    combined_self_check["can_proceed"] = complete_blocking_count == 0
    if complete_blocking_count:
        combined_self_check["status"] = "blocked"
    resource_ready = resources.get("resource_references_ready") is True
    environment_ready = resources.get("render_environment_ready") is True
    ready = compilation.ok and resource_ready and environment_ready
    resource_levels = sorted(
        {
            str(item.get("check_level"))
            for item in resources.get("instruments", [])
            if isinstance(item, dict) and item.get("check_level")
        }
    )
    checks = dict(result["checks"])
    checks["resources"] = {
        "status": "passed" if resource_ready else "failed",
        "level": "project_resource_references",
        "check_levels": resource_levels,
        "resource_references_ready": resource_ready,
        "audio_probe_performed": False,
    }
    checks["render_environment"] = {
        "status": "passed" if environment_ready else "failed",
        "level": "passive_platform_and_output_estimate",
        "render_environment_ready": environment_ready,
        "active_write_probe_performed": False,
    }
    resource_details = dict(resources)
    resource_details.pop("issues", None)
    resource_details["issues_reported_at"] = "$.issues"
    result.update(
        {
            "ok": ready,
            "status": "ready" if ready else "blocked",
            "validation_ok": compilation.ok,
            "resource_references_ready": resource_ready,
            "render_environment_ready": environment_ready,
            "ready_for_render_attempt": ready,
            "audio_probe_performed": False,
            "network": False,
            "persistent_writes": False,
            "active_probes": {
                "native_library_probe": False,
                "external_program_probe": False,
                "ephemeral_writability_probe": False,
            },
            "passive_checks": {
                "filesystem_metadata": compilation.roster is not None,
                "selected_instrument_reference_scan": (
                    compilation.roster is not None and verify_references
                ),
            },
            "checks": checks,
            "resources": resource_details,
            "restore_plan_handoff": resources.get(
                "restore_plan_handoff",
                {"instrument_ids": []},
            ),
            "self_check": combined_self_check,
            "issues": issues,
            "issue_counts": issue_counts,
            "issues_truncated": (
                issues_truncated
                or resources.get("issues_truncated") is True
            ),
        }
    )
    return result


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return number


def _gain_at_seconds(part: Any, seconds: float) -> dict[str, float]:
    points = tuple(part.gain_envelope)
    if not points:
        offset = 0.0
    elif seconds <= points[0].time_seconds:
        offset = points[0].offset_db
    elif seconds >= points[-1].time_seconds:
        offset = points[-1].offset_db
    else:
        offset = points[-1].offset_db
        for left, right in zip(points, points[1:]):
            if left.time_seconds <= seconds <= right.time_seconds:
                span = right.time_seconds - left.time_seconds
                ratio = (seconds - left.time_seconds) / span
                offset = left.offset_db + ratio * (
                    right.offset_db - left.offset_db
                )
                break
    static = float(part.executor.gain_db)
    return {
        "static_db": round(static, 6),
        "automation_offset_db": round(float(offset), 6),
        "effective_db": round(static + float(offset), 6),
    }


def _located_events(
    compilation: _ProjectCompilation,
    *,
    at_seconds: float,
    window: Any,
    selected_parts: set[str] | None,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, int]]]:
    """Collect scheduled gate spans; acoustic release and hall tails stay out."""

    score = compilation.score
    plan = compilation.plan
    if score is None or plan is None:
        return [], {}
    source_notes = {
        note.source_event_id: (part.id, note)
        for part in score.parts
        for note in part.notes
        if note.source_event_id is not None
    }
    rows: list[dict[str, Any]] = []
    executor_counts: dict[str, dict[str, int]] = {}
    point_query = math.isclose(
        window.start_seconds,
        window.end_seconds,
        rel_tol=0.0,
        abs_tol=1e-12,
    )
    for part in plan.parts:
        executor = part.executor
        if (
            selected_parts is not None
            and executor.part_id not in selected_parts
        ):
            continue
        note_offs = {
            int(event["note_id"]): event
            for event in part.performance.get("events", [])
            if event.get("type") == "note_off"
        }
        traces_by_source = {
            trace.get("source_event_id"): trace
            for trace in part.trace
            if trace.get("source_event_id") is not None
        }
        for event in part.performance.get("events", []):
            if event.get("type") != "note_on":
                continue
            note_id = int(event["note_id"])
            release = note_offs.get(note_id)
            if release is None:
                continue
            scheduled_start = float(event["time"])
            scheduled_release = float(release["time"])
            active_at_anchor = (
                scheduled_start <= at_seconds < scheduled_release
            )
            overlaps = (
                active_at_anchor
                if point_query
                else (
                    scheduled_start < window.end_seconds
                    and scheduled_release > window.start_seconds
                )
            )
            if not overlaps:
                continue
            source_event_id = event.get("source_event_id")
            trace = (
                traces_by_source.get(source_event_id)
                if source_event_id is not None
                else (
                    part.trace[note_id - 1]
                    if 0 < note_id <= len(part.trace)
                    else {}
                )
            )
            if trace is None:
                trace = {}
            source = source_notes.get(source_event_id)
            written_note = source[1] if source is not None else None
            bar = int(trace.get("小节", written_note.bar if written_note else 1))
            beat = float(
                trace.get("拍", written_note.beat if written_note else 1.0)
            )
            logical = coordinate_at_position(
                score.tempo_map,
                bar,
                beat,
            )
            derivation = trace.get("推导")
            range_contract = (
                derivation.get("音域合同")
                if isinstance(derivation, dict)
                else None
            )
            sounding_midi = float(event["midi_note"])
            row = {
                "source": {
                    "part_id": executor.part_id,
                    "event_id": source_event_id,
                    "stable_identity": source_event_id is not None,
                },
                "executor_id": executor.executor_id,
                "instrument": executor.capability.relative_path,
                "note_id": note_id,
                "pitch": {
                    "written_midi": (
                        written_note.midi
                        if written_note is not None
                        else None
                    ),
                    "written_name": (
                        pitch_name(written_note.midi)
                        if written_note is not None
                        else None
                    ),
                    "sounding_midi": sounding_midi,
                    "sounding_name": pitch_name(sounding_midi),
                },
                "articulation": {
                    "written": (
                        written_note.articulation
                        if written_note is not None
                        else None
                    ),
                    "resolved": trace.get("奏法"),
                },
                "velocity": float(event.get("velocity", 0.0)),
                "logical": logical.to_dict(),
                "scheduled": {
                    "start_seconds": scheduled_start,
                    "release_seconds": scheduled_release,
                    "gate_duration_seconds": round(
                        scheduled_release - scheduled_start,
                        9,
                    ),
                    "delta_from_logical_ms": round(
                        (scheduled_start - logical.seconds) * 1000.0,
                        6,
                    ),
                },
                "relation": {
                    "active_at_anchor": active_at_anchor,
                    "starts_in_window": (
                        window.start_seconds
                        <= scheduled_start
                        < window.end_seconds
                    ),
                    "ends_in_window": (
                        window.start_seconds
                        < scheduled_release
                        <= window.end_seconds
                    ),
                },
                "range_status": (
                    range_contract.get("status")
                    if isinstance(range_contract, dict)
                    else None
                ),
            }
            rows.append(row)
            counts = executor_counts.setdefault(
                executor.executor_id,
                {"matched_event_count": 0, "active_event_count": 0},
            )
            counts["matched_event_count"] += 1
            if active_at_anchor:
                counts["active_event_count"] += 1
    rows.sort(
        key=lambda row: (
            row["scheduled"]["start_seconds"],
            row["source"]["part_id"],
            row["executor_id"],
            row["source"]["event_id"] or "",
            row["note_id"],
        )
    )
    return rows, executor_counts


@mcp_tool(title="Locate score events", annotations=_READ_ONLY_TOOL)
def locate(
    score: dict,
    roster: dict,
    at_seconds: StrictFloat,
    before_seconds: StrictFloat = 2.0,
    after_seconds: StrictFloat = 2.0,
    part_ids: list[str] | None = None,
    expression: str = "ensemble",
    seed: StrictInt = 0,
    range_mode: str = "compatibility",
    trusted_only: StrictBool | None = None,
    max_events: StrictInt = 64,
    instrument_scope: Literal["formal", "curated"] | None = None,
) -> dict[str, Any]:
    """按最终演奏计划的秒数窗口定位音符；只读，不渲染音频。

    返回的 ``scheduled`` 是 note_on 到 note_off 的门控区间；采样自身 release、
    共鸣和厅堂尾声不在其中。``logical`` 是同一谱面位置经 TempoMap 得到的纯谱面
    时间，不含结构表情、人性化或发音补偿。
    """

    try:
        limit = _bounded_limit(max_events, "max_events")
        anchor = _finite_nonnegative(at_seconds, "at_seconds")
        before = _finite_nonnegative(before_seconds, "before_seconds")
        after = _finite_nonnegative(after_seconds, "after_seconds")
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.locate_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="query.invalid",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    try:
        resolved_scope, _allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.locate_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "issues": [
                _issue(
                    severity="error",
                    code="instrument.scope_invalid",
                    stage="availability_policy",
                    message=exc,
                )
            ],
        }
    compilation = _compile_project(
        score,
        roster,
        expression=expression,
        seed=seed,
        range_mode=range_mode,
        instrument_scope=resolved_scope,
    )
    if not compilation.ok:
        issues, counts, truncated = _issue_page(
            compilation.issues,
            limit,
        )
        return {
            "kind": "tianlai.locate_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "instrument_scope": resolved_scope,
            "project": compilation.project,
            "checks": compilation.checks,
            "issues": issues,
            "issue_counts": counts,
            "issues_truncated": truncated,
        }
    score_document = compilation.score
    roster_document = compilation.roster
    plan = compilation.plan
    if score_document is None or roster_document is None or plan is None:
        raise RuntimeError("successful project compilation lost its documents")

    selected_parts = None
    if part_ids is not None:
        selected_parts = {
            str(part_id).strip()
            for part_id in part_ids
            if str(part_id).strip()
        }
        known_parts = {part.id for part in score_document.parts}
        unknown_parts = sorted(selected_parts - known_parts)
        if unknown_parts:
            return {
                "kind": "tianlai.locate_result",
                "schema_version": 1,
                "ok": False,
                "audio_rendered": False,
                "instrument_scope": resolved_scope,
                "project": compilation.project,
                "issues": [
                    _issue(
                        severity="error",
                        code="query.unknown_part",
                        stage="settings",
                        message=(
                            "part_ids 含总谱中不存在的声部: "
                            + ", ".join(unknown_parts)
                        ),
                    )
                ],
            }
    try:
        window = seconds_window_around(
            anchor,
            before_seconds=before,
            after_seconds=after,
            maximum_seconds=plan.duration_seconds,
        )
        logical_anchor = coordinate_at_seconds(
            score_document.tempo_map,
            anchor,
        )
    except Exception as exc:
        return {
            "kind": "tianlai.locate_result",
            "schema_version": 1,
            "ok": False,
            "audio_rendered": False,
            "instrument_scope": resolved_scope,
            "project": compilation.project,
            "issues": [
                _issue(
                    severity="error",
                    code="query.time_out_of_range",
                    stage="settings",
                    message=exc,
                )
            ],
        }
    all_rows, executor_counts = _located_events(
        compilation,
        at_seconds=anchor,
        window=window,
        selected_parts=selected_parts,
    )
    rows = all_rows[:limit]
    returned_executors = {
        row["executor_id"] for row in rows
    }
    executors = []
    for part in plan.parts:
        executor = part.executor
        if executor.executor_id not in returned_executors:
            continue
        counts = executor_counts.get(executor.executor_id, {})
        executors.append(
            {
                "executor_id": executor.executor_id,
                "part_id": executor.part_id,
                "instrument": executor.capability.relative_path,
                "matched_event_count": counts.get(
                    "matched_event_count", 0
                ),
                "active_event_count": counts.get(
                    "active_event_count", 0
                ),
                "gain_at_anchor": _gain_at_seconds(part, anchor),
                "gain_semantics": (
                    "编制推子与自动化状态，不是实际音频响度预测"
                ),
                "pan": executor.pan,
                "role": (
                    executor.role.to_dict()
                    if executor.role is not None
                    else None
                ),
            }
        )
    legacy = score_document.schema_version is None
    non_error_issues = [
        issue
        for issue in compilation.issues
        if issue.get("severity") != "error"
    ]
    if legacy:
        non_error_issues.append(
            _issue(
                severity="warning",
                code="score.legacy_identity",
                stage="score_document",
                message=(
                    "该谱没有稳定 event_id；秒数仍可定位，但局部编辑前应先调用 "
                    "upgrade_score，之后才能跨修订可靠指向同一个音符。"
                ),
            )
        )
    return {
        "kind": "tianlai.locate_result",
        "schema_version": 1,
        "ok": True,
        "audio_rendered": False,
        "instrument_scope": resolved_scope,
        "project": compilation.project,
        "time_semantics": {
            "logical": (
                "纯谱面位置经 TempoMap 换算，不含结构表情、人性化和发音补偿"
            ),
            "scheduled": "最终送入后端的 note_on 到 note_off 门控时间",
            "audible_tail_included": False,
        },
        "anchor": {
            "basis": "scheduled",
            "seconds": anchor,
            "nominal_logical_coordinate": logical_anchor.to_dict(),
            "warning": (
                "scheduled 秒数只能给出同一时钟位置的名义谱面坐标；"
                "具体事件以 events 内各自 logical/scheduled 字段为准"
            ),
        },
        "window": {
            "basis": "scheduled",
            **window.to_dict(),
        },
        "summary": {
            "matched_event_count": len(all_rows),
            "returned_event_count": len(rows),
            "truncated": len(all_rows) > limit,
        },
        "events": rows,
        "executors": executors,
        "issues": non_error_issues,
    }


def _candidate_output_path(value: str) -> Path:
    """Resolve one MCP render candidate without allowing output-root escape."""

    raw = Path(value).expanduser()
    candidate = (
        raw.resolve()
        if raw.is_absolute()
        else (OUTPUT_DIR / raw).resolve()
    )
    try:
        candidate.relative_to(OUTPUT_DIR.resolve())
    except ValueError as exc:
        raise ValueError(
            "候选必须位于当前天籁 MCP 输出目录内"
        ) from exc
    return candidate


@mcp_tool(title="Locate rendered candidate", annotations=_READ_ONLY_TOOL)
def locate_rendered_candidate(
    candidate_directory: str,
    at_seconds: StrictFloat,
    tail_lookback_seconds: StrictFloat = 5.0,
    upcoming_seconds: StrictFloat = 2.0,
    max_events: StrictInt = 128,
) -> dict[str, Any]:
    """从已保存候选的回执和演奏计划定位实际听到的秒数。

    与 ``locate`` 重新编译当前 score/roster 不同，本工具校验候选中的
    score、roster、render-profile、演奏计划和渲染回执 Hash，再报告该候选
    在指定秒数的活动事件、可能仍有释音/厅堂贡献的近期事件和即将到来的事件。
    """

    try:
        return locate_candidate(
            _candidate_output_path(candidate_directory),
            at_seconds=at_seconds,
            tail_lookback_seconds=tail_lookback_seconds,
            upcoming_seconds=upcoming_seconds,
            max_events=max_events,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {
            "kind": "tianlai.candidate_locate_result",
            "schema_version": 1,
            "ok": False,
            "issues": [
                _issue(
                    severity="error",
                    code="candidate.locate_failed",
                    stage="candidate_receipt",
                    message=exc,
                )
            ],
        }


@mcp_tool(title="Compare rendered candidates", annotations=_READ_ONLY_TOOL)
def compare_rendered_candidates(
    before_candidate_directory: str,
    after_candidate_directory: str,
    max_changes: StrictInt = 256,
) -> dict[str, Any]:
    """比较两个不可变候选的乐谱、编制、配置、演奏计划和混音身份。"""

    try:
        return compare_candidates(
            _candidate_output_path(before_candidate_directory),
            _candidate_output_path(after_candidate_directory),
            max_changes=max_changes,
        )
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return {
            "kind": "tianlai.candidate_compare_result",
            "schema_version": 1,
            "ok": False,
            "issues": [
                _issue(
                    severity="error",
                    code="candidate.compare_failed",
                    stage="candidate_receipt",
                    message=exc,
                )
            ],
        }


@mcp_tool(title="Get creative workflow guide", annotations=_READ_ONLY_TOOL)
def creative_workflow_guide() -> dict[str, Any]:
    """Return the bounded v0.7 workflow contract without loading the full constitution."""

    return {
        "kind": _WORKFLOW_RESULT_KIND,
        "schema_version": _WORKFLOW_RESULT_VERSION,
        "ok": True,
        "operation": "creative_workflow_guide",
        "guide": {
            "modes": {
                "off": "Record an explicit opt-out; no charter or iteration is run.",
                "audit": (
                    "Review one immutable authoring revision or existing candidate; "
                    "may recommend revision but does not mutate the score."
                ),
                "iterate": (
                    "Reserve, render, review and bind explicit new authoring revisions "
                    "within bounded iteration and rollback budgets."
                ),
            },
            "boundary": {
                "hard_failures_may_block": True,
                "promise_conflicts_block_automatically": False,
                "aesthetic_risks_block_automatically": False,
                "single_aesthetic_objective": False,
                "automatic_score_or_audio_changes": False,
                "acceptance_claim": "contextual_decision_not_objective_quality",
                "unknown_or_intentional_roughness_requires_preservation_or_exception": True,
            },
            "authority": {
                "mcp_final_authority": "agent",
                "trusted_human_approval_available": False,
                "note": (
                    "This stdio boundary never records creator/listener authority. "
                    "A trusted host or direct core API is required for a human-final workflow."
                ),
            },
            "work_charter": {
                "required_fields": [
                    "title",
                    "one_sentence_promise",
                    "target_listener_and_scene",
                    "primary_sovereignty",
                    "identity_kernel",
                    "ending_contract",
                ],
                "template": {
                    "title": "one work-specific charter title",
                    "one_sentence_promise": "what this work promises in this context",
                    "target_listener_and_scene": "listener, scene, medium and listening context",
                    "primary_sovereignty": ["M"],
                    "secondary_sovereignties": [],
                    "style_recipe": "probabilistic recipe, not a genre checklist",
                    "identity_kernel": {
                        "invariants": ["one traceable identity invariant"],
                        "transformable_parts": ["one dimension allowed to change"],
                    },
                    "ending_contract": "how the ending answers, rewrites or refuses the promise",
                },
                "optional_fields": [
                    "secondary_sovereignties",
                    "style_recipe",
                    "affect_vector",
                    "motion_vector",
                    "dramatic_question",
                    "energy_curve",
                    "tension_curve",
                    "memory_landmarks",
                    "scarce_resources",
                    "climax_privileges",
                    "prohibited_shortcuts",
                    "uncertainties",
                    "final_review_dimensions",
                ],
                "sovereignty_codes": [
                    "M",
                    "G",
                    "H",
                    "P",
                    "T",
                    "O",
                    "N",
                    "L",
                    "R",
                    "C",
                    "X",
                    "I",
                ],
            },
            "review_phases": [
                "intent",
                "symbolic_structure",
                "orchestration_performance",
                "render_report",
                "audio_audition",
            ],
            "render_prerequisites": [
                "symbolic_structure review",
                "orchestration_performance review",
                "no recorded hard_failure",
            ],
            "evidence_categories": {
                "hard_failure": (
                    "Only an engine_contract reported by engine or validator; the "
                    "historical record is never exceptable, while render and acceptance "
                    "are blocked only when the trusted boundary reproduces it again."
                ),
                "promise_conflict": (
                    "A declared_promise or active_clause conflict; nonblocking until "
                    "the frozen final authority decides."
                ),
                "aesthetic_risk": (
                    "A diagnostic, measurement or audition hypothesis; nonblocking and "
                    "never an automatic edit."
                ),
            },
            "decisions": {
                "iterate": ["accept", "revise", "preserve", "stop"],
                "audit": ["accept", "recommend_revision", "preserve", "stop"],
                "accept_requires": [
                    "workflow-authorized and recorded candidate",
                    "complete render review artifacts",
                    "intent, symbolic_structure, orchestration_performance and render_report reviews",
                    "no hard_failure",
                    "a review proving the agent's declared perception basis",
                ],
            },
            "constitution": {
                **_OFFICIAL_CONSTITUTION,
                "optional": True,
                "full_document_injected": False,
                "activation_rule": (
                    "Bind the exact document hash and activate only a small, work-relevant "
                    "clause set; do not inject or activate all 901 lines each iteration."
                ),
                "starter_clause_ids": [
                    "C0.02",
                    "C0.03",
                    "C0.16",
                    "C0.21",
                    "C0.25",
                ],
                "active_clause_shape": {
                    "clause_id": "C0.02",
                    "role": "review_lens",
                    "rationale": "why this clause matters for this work",
                    "interpretation": "bounded, work-specific interpretation",
                },
            },
        },
        "next_action": {
            "operation": "create_creative_workflow",
            "reason": "after_an_authoring_project_and_charter_inputs_exist",
        },
    }


@mcp_tool(title="Get music constitution clauses", annotations=_READ_ONLY_TOOL)
def get_music_constitution_clauses(
    clause_ids: _ConstitutionClauseIdList,
    language: Literal["zh-CN", "en"] = "zh-CN",
) -> dict[str, Any]:
    """Return at most twelve exact registered clauses after verifying the full document."""

    operation = "get_music_constitution_clauses"
    try:
        if len(set(clause_ids)) != len(clause_ids):
            raise _McpWorkflowBoundaryError(
                "creative_workflow.constitution_duplicate_clause_id"
            )
        registry = _official_constitution_registry(language)
        unknown = [clause_id for clause_id in clause_ids if clause_id not in registry]
        if unknown:
            raise _McpWorkflowBoundaryError(
                "creative_workflow.constitution_clause_unknown",
                stage="constitution",
            )
        return {
            "kind": _WORKFLOW_RESULT_KIND,
            "schema_version": _WORKFLOW_RESULT_VERSION,
            "ok": True,
            "operation": operation,
            "constitution": dict(_OFFICIAL_CONSTITUTIONS[language]),
            "clauses": [registry[clause_id] for clause_id in clause_ids],
            "bounded": True,
            "full_document_injected": False,
            "next_action": {
                "operation": "activate_creative_workflow",
                "reason": "interpret_only_the_selected_work_relevant_clauses",
            },
        }
    except _McpWorkflowBoundaryError as exc:
        return _workflow_failure(operation, exc)
    except Exception as exc:
        return _workflow_failure(operation, exc)


@mcp_tool(title="Create creative workflow", annotations=_AUTHORING_WRITE_TOOL)
def create_creative_workflow(
    project_key: _AuthoringSelector,
    mode: Literal["off", "audit", "iterate"],
    base_authoring_revision: _AuthoringSelector | None = None,
    budget: dict | None = None,
) -> dict[str, Any]:
    """Create an optional workflow whose final authority is truthfully fixed to agent."""

    operation = "create_creative_workflow"
    try:
        checked_base = _validated_authoring_revision(
            base_authoring_revision,
            required=False,
        )
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = create_creative_workflow_state(
            root,
            mode=mode,
            final_authority="agent",
            base_authoring_revision=checked_base,
            budget=budget,
        )
        history = verify_creative_workflow_history_state(
            root,
            workflow_id=snapshot.workflow_id,
        )
        return _workflow_success(
            operation,
            project_key,
            snapshot,
            history=history,
            authority={
                "final_authority": "agent",
                "trusted_human_approval": False,
            },
        )
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(operation, exc, project_key=project_key)
    except Exception as exc:
        return _workflow_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Open creative workflow", annotations=_READ_ONLY_TOOL)
def open_creative_workflow(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    revision: _AuthoringSelector | None = None,
) -> dict[str, Any]:
    """Verify and open one current or historical workflow revision without paths."""

    operation = "open_creative_workflow"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(revision, required=False)
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = open_creative_workflow_state(
            root,
            workflow_id=checked_id,
            revision=checked_revision,
        )
        current = (
            snapshot
            if checked_revision is None
            else open_creative_workflow_state(root, workflow_id=checked_id)
        )
        historical_read_only = snapshot.revision != current.revision
        result = _workflow_success(
            operation,
            project_key,
            snapshot,
            include_next_action=not historical_read_only,
        )
        result["historical_read_only"] = historical_read_only
        if historical_read_only:
            result["next_action"] = {
                "operation": "open_creative_workflow",
                "reason": "historical_revision_is_read_only_refresh_current",
                "suggested_arguments": {
                    "project_key": project_key,
                    "workflow_id": checked_id,
                },
                "alternatives": [],
            }
        return result
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Verify creative workflow history", annotations=_READ_ONLY_TOOL)
def verify_creative_workflow_history(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    maximum_revisions: StrictInt = 4096,
) -> dict[str, Any]:
    """Explicitly audit the bounded immutable parent chain from current to genesis."""

    operation = "verify_creative_workflow_history"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        history = verify_creative_workflow_history_state(
            root,
            workflow_id=checked_id,
            maximum_revisions=maximum_revisions,
        )
        snapshot = open_creative_workflow_state(root, workflow_id=checked_id)
        return _workflow_success(
            operation,
            project_key,
            snapshot,
            history=history,
        )
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Activate creative workflow", annotations=_AUTHORING_WRITE_TOOL)
def activate_creative_workflow(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    work_charter: dict,
    constitution: dict | None = None,
    active_clauses: list[dict] | None = None,
) -> dict[str, Any]:
    """Freeze the work charter and optional small constitution clause set."""

    operation = "activate_creative_workflow"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        constitution_source = _validate_mcp_constitution_activation(
            constitution,
            active_clauses,
        )
        snapshot = activate_creative_workflow_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            work_charter=work_charter,
            constitution=constitution,
            active_clauses=[] if active_clauses is None else active_clauses,
        )
        return _workflow_success(
            operation,
            project_key,
            snapshot,
            constitution_source=constitution_source,
        )
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Record workflow review", annotations=_AUTHORING_WRITE_TOOL)
def record_workflow_review(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    phase: Literal[
        "intent",
        "symbolic_structure",
        "orchestration_performance",
        "render_report",
        "audio_audition",
    ],
    perception_basis: Literal["report_only", "audio_audition"],
    summary: _WorkflowText,
) -> dict[str, Any]:
    """Record one agent phase review without claiming human or trusted-validator identity."""

    operation = "record_workflow_review"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = record_workflow_review_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            phase=phase,
            reviewer="agent",
            perception_basis=perception_basis,
            summary=summary,
        )
        return _workflow_success(operation, project_key, snapshot)
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Record workflow evidence", annotations=_AUTHORING_WRITE_TOOL)
def record_workflow_evidence(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    category: Literal["promise_conflict", "aesthetic_risk"],
    code: _AuthoringSelector,
    basis_kind: Literal[
        "declared_promise",
        "active_clause",
        "diagnostic_hypothesis",
        "render_measurement",
        "audio_audition",
    ],
    basis_reference: _WorkflowShortText,
    perception_basis: Literal["report_only", "audio_audition"],
    summary: _WorkflowText,
    observation: _WorkflowText,
    interpretation: _WorkflowText,
    confidence: Literal["low", "medium", "high"],
    scope: dict | None = None,
    artifact_sha256: _AuthoringSelector | None = None,
    artifact_role: Literal[
        "render_receipt",
        "post_render_check",
        "mix_report",
    ]
    | None = None,
) -> dict[str, Any]:
    """Record bounded negative or advisory evidence without changing score/audio."""

    operation = "record_workflow_evidence"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        checked_artifact = _validated_workflow_revision(
            artifact_sha256,
            required=False,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = record_workflow_evidence_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            category=category,
            code=code,
            basis_kind=basis_kind,
            basis_reference=basis_reference,
            reporter="agent",
            perception_basis=perception_basis,
            summary=summary,
            observation=observation,
            interpretation=interpretation,
            confidence=confidence,
            scope=scope,
            artifact_sha256=checked_artifact,
            artifact_role=artifact_role,
        )
        return _workflow_success(operation, project_key, snapshot)
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Record verified workflow hard failure", annotations=_AUTHORING_WRITE_TOOL)
def record_verified_workflow_hard_failure(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    issue_code: _AuthoringSelector,
) -> dict[str, Any]:
    """Re-run trusted readiness and record only an exact blocking issue code."""

    operation = "record_verified_workflow_hard_failure"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = record_verified_workflow_hard_failure_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            issue_code=issue_code,
        )
        return _workflow_success(operation, project_key, snapshot)
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Register workflow exception", annotations=_AUTHORING_WRITE_TOOL)
def register_workflow_exception(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    target_type: Literal["active_clause", "work_charter"],
    target_ref: _WorkflowShortText,
    purpose: _WorkflowText,
    scope: _WorkflowText,
    higher_value: _WorkflowText,
    cost: _WorkflowText,
    recovery: _WorkflowText,
    evidence_ids: _WorkflowReferenceList,
    reusable: StrictBool = False,
) -> dict[str, Any]:
    """Register a bounded exception; hard failures are never eligible."""

    operation = "register_workflow_exception"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = register_workflow_exception_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            target_type=target_type,
            target_ref=target_ref,
            purpose=purpose,
            scope=scope,
            higher_value=higher_value,
            cost=cost,
            recovery=recovery,
            evidence_ids=evidence_ids,
            reusable=reusable,
        )
        return _workflow_success(operation, project_key, snapshot)
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Create authoring project", annotations=_AUTHORING_WRITE_TOOL)
def create_authoring_project(
    project_key: _AuthoringSelector,
    title: _AuthoringTitle,
) -> dict[str, Any]:
    """Create one project below the dedicated MCP authoring namespace."""

    operation = "create_authoring_project"
    try:
        root = _authoring_project_root(
            project_key,
            create_namespace=True,
            require_existing=False,
        )
        state = create_authoring_project_state(root, title=title)
        return _authoring_success(
            operation,
            project_key,
            project=_authoring_project_descriptor(state),
        )
    except (AuthoringProjectError, _McpAuthoringBoundaryError) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Open authoring project", annotations=_READ_ONLY_TOOL)
def open_authoring_project(
    project_key: _AuthoringSelector,
    revision: _AuthoringSelector | None = None,
) -> dict[str, Any]:
    """Open current or historical project metadata without returning documents."""

    operation = "open_authoring_project"
    try:
        checked_revision = _validated_authoring_revision(
            revision,
            required=False,
        )
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        state = open_authoring_project_state(root, revision=checked_revision)
        return _authoring_success(
            operation,
            project_key,
            project=_authoring_project_descriptor(state),
        )
    except (AuthoringProjectError, _McpAuthoringBoundaryError) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Get authoring snapshot", annotations=_READ_ONLY_TOOL)
def get_authoring_snapshot(
    project_key: _AuthoringSelector,
    revision: _AuthoringSelector | None = None,
) -> dict[str, Any]:
    """Return one immutable three-document snapshot plus bounded readiness."""

    operation = "get_authoring_snapshot"
    try:
        checked_revision = _validated_authoring_revision(
            revision,
            required=False,
        )
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        state = open_authoring_project_state(root, revision=checked_revision)
        snapshot = build_authoring_snapshot(state, project_root=root)
        return _authoring_success(
            operation,
            project_key,
            snapshot=snapshot,
        )
    except (AuthoringProjectError, _McpAuthoringBoundaryError) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Save authoring project", annotations=_AUTHORING_WRITE_TOOL)
def save_authoring_project(
    project_key: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    documents: dict,
) -> dict[str, Any]:
    """CAS-save a complete document set as a new immutable revision."""

    operation = "save_authoring_project"
    try:
        checked_revision = _validated_authoring_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        state = save_authoring_project_state(
            root,
            expected_revision=checked_revision,
            documents=documents,
        )
        return _authoring_success(
            operation,
            project_key,
            project=_authoring_project_descriptor(state),
        )
    except (AuthoringProjectError, _McpAuthoringBoundaryError) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Check authoring readiness", annotations=_READ_ONLY_TOOL)
def check_authoring_readiness(
    project_key: _AuthoringSelector,
    revision: _AuthoringSelector | None = None,
) -> dict[str, Any]:
    """Check hard render contracts and retain advisory evidence as review only."""

    operation = "check_authoring_readiness"
    try:
        checked_revision = _validated_authoring_revision(
            revision,
            required=False,
        )
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        state = open_authoring_project_state(root, revision=checked_revision)
        readiness = validate_authoring_project_readiness(
            state,
            project_root=root,
        )
        return _authoring_success(
            operation,
            project_key,
            project=_authoring_project_descriptor(state),
            readiness=readiness,
        )
    except (AuthoringProjectError, _McpAuthoringBoundaryError) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Render authoring revision", annotations=_AUTHORING_WRITE_TOOL)
def render_authoring_revision(
    project_key: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
) -> dict[str, Any]:
    """Render exactly one immutable revision; never follow the current pointer."""

    operation = "render_authoring_revision"
    try:
        checked_revision = _validated_authoring_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        selected_state = open_authoring_project_state(
            root,
            revision=checked_revision,
        )
        rendered = render_authoring_project_candidate(
            root,
            expected_revision=checked_revision,
        )
        candidate = rendered.get("candidate")
        if (
            rendered.get("status") != "completed"
            or rendered.get("project_id") != selected_state.project_id
            or rendered.get("revision") != selected_state.revision
            or rendered.get("workflow_managed") is not False
            or rendered.get("reused_existing") is not False
            or not isinstance(candidate, dict)
            or not isinstance(candidate.get("work_id"), str)
            or not isinstance(candidate.get("candidate_id"), str)
        ):
            raise _McpAuthoringBoundaryError(
                "authoring_render.result_identity_mismatch",
                stage="render",
            )
        try:
            _validated_candidate_segment(
                candidate["work_id"],
                field="work_id",
            )
            _validated_candidate_segment(
                candidate["candidate_id"],
                field="candidate_id",
            )
        except _McpAuthoringBoundaryError as exc:
            raise _McpAuthoringBoundaryError(
                "authoring_render.result_identity_mismatch",
                stage="render",
            ) from exc
        return _authoring_success(
            operation,
            project_key,
            render=rendered,
        )
    except (
        AuthoringProjectError,
        AuthoringRenderError,
        _McpAuthoringBoundaryError,
    ) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Inspect authoring candidate", annotations=_READ_ONLY_TOOL)
def inspect_authoring_candidate(
    project_key: _AuthoringSelector,
    work_id: _AuthoringSelector,
    candidate_id: _AuthoringSelector,
) -> dict[str, Any]:
    """Verify an authoring candidate and return path-free render evidence."""

    operation = "inspect_authoring_candidate"
    try:
        directory, manifest, state, candidate_identity = _load_authoring_candidate(
            project_key,
            work_id=work_id,
            candidate_id=candidate_id,
        )
        receipt = _read_bound_candidate_json(
            directory,
            candidate_identity,
            manifest.get("render_receipt"),
            label="render_receipt",
        )
        post_render_check = _read_bound_candidate_json(
            directory,
            candidate_identity,
            receipt.get("post_render_check"),
            label="post_render_check",
        )
        mix_report = None
        if receipt.get("mix_report") is not None:
            mix_report = _read_bound_candidate_json(
                directory,
                candidate_identity,
                receipt.get("mix_report"),
                label="mix_report",
            )
        project_binding = manifest["project"]
        authoring_binding = manifest["authoring_project"]
        workflow_status = _verified_candidate_workflow_status(
            _authoring_project_root(
                project_key,
                create_namespace=False,
                require_existing=True,
            ),
            directory,
            manifest,
        )
        mix_binding = receipt.get("mix")
        if not isinstance(mix_binding, dict):
            raise _McpAuthoringBoundaryError(
                "authoring_candidate.evidence_binding_invalid",
                stage="candidate",
            )
        return _authoring_success(
            operation,
            project_key,
            project=_authoring_project_descriptor(state),
            candidate={
                "candidate_id": manifest["candidate_id"],
                "work_id": manifest["work_id"],
                "title": manifest["title"],
                "created_at_utc": manifest["created_at_utc"],
                "parent_candidate_id": manifest.get("parent_candidate_id"),
                **workflow_status,
                "authoring_project": {
                    "project_id": authoring_binding["project_id"],
                    "revision": authoring_binding["revision"],
                    "authoring_roster_canonical_sha256": authoring_binding[
                        "authoring_roster"
                    ]["canonical_sha256"],
                },
                "bindings": {
                    "score_sha256": project_binding["score"][
                        "canonical_sha256"
                    ],
                    "roster_sha256": project_binding["roster"][
                        "canonical_sha256"
                    ],
                    "render_profile_sha256": project_binding[
                        "render_profile"
                    ]["canonical_sha256"],
                    "performance_plan_sha256": project_binding[
                        "performance_plan_sha256"
                    ],
                    "render_receipt_sha256": manifest["render_receipt"][
                        "sha256"
                    ],
                    "mix_sha256": mix_binding.get("sha256"),
                },
            },
            render_evidence={
                "audio_format": receipt.get("audio_format"),
                "master_gain_db": receipt.get("master_gain_db"),
                "normalize": receipt.get("normalize"),
                "collaboration": receipt.get("collaboration"),
                "space": receipt.get("space"),
                "mix": {
                    "sha256": mix_binding.get("sha256"),
                    "peak": mix_binding.get("peak"),
                    "frame_count": mix_binding.get("frame_count"),
                },
                "post_render_check": post_render_check,
                "mix_report": mix_report,
            },
        )
    except (
        AuthoringProjectError,
        _McpAuthoringBoundaryError,
    ) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Locate authoring candidate", annotations=_READ_ONLY_TOOL)
def locate_authoring_candidate(
    project_key: _AuthoringSelector,
    work_id: _AuthoringSelector,
    candidate_id: _AuthoringSelector,
    at_seconds: StrictFloat,
    tail_lookback_seconds: StrictFloat = 5.0,
    upcoming_seconds: StrictFloat = 2.0,
    max_events: StrictInt = 128,
) -> dict[str, Any]:
    """Locate rendered events using only project and immutable candidate IDs."""

    operation = "locate_authoring_candidate"
    try:
        directory, manifest, _state, candidate_identity = (
            _load_authoring_candidate(
                project_key,
                work_id=work_id,
                candidate_id=candidate_id,
            )
        )
        try:
            located = locate_candidate(
                directory,
                at_seconds=at_seconds,
                tail_lookback_seconds=tail_lookback_seconds,
                upcoming_seconds=upcoming_seconds,
                max_events=max_events,
            )
        except (
            OSError,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            raise _McpAuthoringBoundaryError(
                "authoring_candidate.locate_failed",
                stage="candidate",
            ) from exc
        try:
            if revalidate_plain_directory(candidate_identity) != directory:
                raise OSError("candidate directory identity mismatch")
        except OSError as exc:
            raise _McpAuthoringBoundaryError(
                "authoring_candidate.locate_failed",
                stage="candidate",
            ) from exc
        located.pop("candidate_directory", None)
        if located.get("candidate_id") != manifest.get("candidate_id"):
            raise _McpAuthoringBoundaryError(
                "authoring_candidate.identity_mismatch",
                stage="candidate",
            )
        return _authoring_success(
            operation,
            project_key,
            work_id=work_id,
            candidate_id=candidate_id,
            location=located,
        )
    except (
        AuthoringProjectError,
        _McpAuthoringBoundaryError,
    ) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Compare authoring candidates", annotations=_READ_ONLY_TOOL)
def compare_authoring_candidates(
    project_key: _AuthoringSelector,
    before_work_id: _AuthoringSelector,
    before_candidate_id: _AuthoringSelector,
    after_work_id: _AuthoringSelector,
    after_candidate_id: _AuthoringSelector,
    max_changes: StrictInt = 256,
) -> dict[str, Any]:
    """Compare two verified candidates within one authoring project."""

    operation = "compare_authoring_candidates"
    try:
        (
            before_directory,
            _before,
            before_state,
            before_identity,
        ) = _load_authoring_candidate(
            project_key,
            work_id=before_work_id,
            candidate_id=before_candidate_id,
        )
        (
            after_directory,
            _after,
            after_state,
            after_identity,
        ) = _load_authoring_candidate(
            project_key,
            work_id=after_work_id,
            candidate_id=after_candidate_id,
        )
        if before_state.project_id != after_state.project_id:
            raise _McpAuthoringBoundaryError(
                "authoring_candidate.project_binding_mismatch",
                stage="candidate",
            )
        try:
            comparison = compare_candidates(
                before_directory,
                after_directory,
                max_changes=max_changes,
            )
        except (
            OSError,
            TypeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            raise _McpAuthoringBoundaryError(
                "authoring_candidate.compare_failed",
                stage="candidate",
            ) from exc
        try:
            if (
                revalidate_plain_directory(before_identity) != before_directory
                or revalidate_plain_directory(after_identity) != after_directory
            ):
                raise OSError("candidate directory identity mismatch")
        except OSError as exc:
            raise _McpAuthoringBoundaryError(
                "authoring_candidate.compare_failed",
                stage="candidate",
            ) from exc
        return _authoring_success(
            operation,
            project_key,
            before={
                "work_id": before_work_id,
                "candidate_id": before_candidate_id,
            },
            after={
                "work_id": after_work_id,
                "candidate_id": after_candidate_id,
            },
            comparison=comparison,
        )
    except (
        AuthoringProjectError,
        _McpAuthoringBoundaryError,
    ) as exc:
        return _authoring_failure(
            operation,
            exc,
            project_key=project_key,
        )
    except Exception as exc:
        return _authoring_failure(operation, exc, project_key=project_key)


@mcp_tool(title="Render workflow candidate", annotations=_RENDER_TOOL)
def render_workflow_candidate(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
) -> dict[str, Any]:
    """Reserve, render and record one managed candidate without accepting auth or paths."""

    operation = "render_workflow_candidate"
    root: Path | None = None
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        current = _open_expected_workflow(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
        )
        status = current.detached_state()["status"]
        if status == "reviewing":
            reservation = request_workflow_render_state(
                root,
                workflow_id=checked_id,
                expected_revision=checked_revision,
            )
        elif status == "candidate_pending":
            # A failed or disconnected prior call may have already published the
            # immutable reservation. Reuse that exact current operation.
            reservation = current
        else:
            raise CreativeWorkflowError("illegal_workflow_transition")
        authorization = workflow_render_authorization(reservation)
        rendered = render_authoring_project_candidate(
            root,
            expected_revision=authorization["authoring_revision"],
            workflow_authorization=authorization,
        )
        candidate = rendered.get("candidate")
        if (
            rendered.get("status") != "completed"
            or rendered.get("project_id") != authorization["project_id"]
            or rendered.get("revision") != authorization["authoring_revision"]
            or rendered.get("workflow_managed") is not True
            or not isinstance(rendered.get("reused_existing"), bool)
            or not isinstance(candidate, dict)
            or candidate.get("work_id") != authorization["candidate_work_id"]
            or candidate.get("candidate_id") != authorization["candidate_id"]
        ):
            raise _McpWorkflowBoundaryError(
                "creative_workflow.render_result_identity_mismatch",
                stage="render",
            )
        candidate_identity, candidate_project, candidate_root = (
            _authoring_candidate_directory(
                project_key,
                work_id=candidate["work_id"],
                candidate_id=candidate["candidate_id"],
            )
        )
        if (
            candidate_root != root
            or candidate_project.project_id != authorization["project_id"]
        ):
            raise _McpWorkflowBoundaryError(
                "creative_workflow.render_result_identity_mismatch",
                stage="candidate",
            )
        recorded = record_workflow_candidate_state(
            root,
            workflow_id=checked_id,
            expected_revision=reservation.revision,
            candidate_path=revalidate_plain_directory(candidate_identity),
        )
        return _workflow_success(
            operation,
            project_key,
            recorded,
            render=rendered,
            reservation_revision=reservation.revision,
        )
    except (
        AuthoringProjectError,
        AuthoringRenderError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure_with_current(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
            root=root,
        )
    except Exception as exc:
        return _workflow_failure_with_current(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
            root=root,
        )


@mcp_tool(title="Attach workflow candidate for audit", annotations=_AUTHORING_WRITE_TOOL)
def attach_workflow_candidate_for_audit(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    work_id: _AuthoringSelector,
    candidate_id: _AuthoringSelector,
) -> dict[str, Any]:
    """Attach an existing verified candidate by ID; it remains unmanaged and unacceptible."""

    operation = "attach_workflow_candidate_for_audit"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        current = _open_expected_workflow(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
        )
        if current.detached_state()["mode"] != "audit":
            raise _McpWorkflowBoundaryError(
                "creative_workflow.audit_mode_required",
                stage="workflow",
            )
        candidate_identity, candidate_project, candidate_root = (
            _authoring_candidate_directory(
                project_key,
                work_id=work_id,
                candidate_id=candidate_id,
            )
        )
        if candidate_root != root or candidate_project.project_id != current.project_id:
            raise _McpWorkflowBoundaryError(
                "creative_workflow.candidate_project_mismatch",
                stage="candidate",
            )
        snapshot = attach_existing_candidate_for_audit_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            candidate_path=revalidate_plain_directory(candidate_identity),
        )
        return _workflow_success(
            operation,
            project_key,
            snapshot,
            candidate={"work_id": work_id, "candidate_id": candidate_id},
        )
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Decide workflow iteration", annotations=_AUTHORING_WRITE_TOOL)
def decide_workflow_iteration(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    disposition: Literal["accept", "revise", "recommend_revision", "preserve", "stop"],
    summary: _WorkflowText,
    rationale: _WorkflowText,
    perception_basis: Literal["report_only", "audio_audition"],
    protected_values: _WorkflowReferenceList | None = None,
    sacrificed_values: _WorkflowReferenceList | None = None,
    evidence_ids: _WorkflowReferenceList | None = None,
    exception_ids: _WorkflowReferenceList | None = None,
    expected_audible_change: _WorkflowText | None = None,
) -> dict[str, Any]:
    """Make an agent-authority contextual decision; never claim objective quality."""

    operation = "decide_workflow_iteration"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        current = _open_expected_workflow(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
        )
        authority = _agent_workflow_authority(current)
        candidate_path: Path | None = None
        if disposition == "accept":
            state = current.detached_state()
            iterations = state.get("iterations")
            candidate = (
                iterations[-1].get("anchor", {}).get("candidate")
                if isinstance(iterations, list) and iterations
                else None
            )
            if not isinstance(candidate, dict):
                raise CreativeWorkflowError("verified_candidate_required_for_acceptance")
            identity, candidate_project, candidate_root = _authoring_candidate_directory(
                project_key,
                work_id=candidate.get("work_id"),
                candidate_id=candidate.get("candidate_id"),
            )
            if candidate_root != root or candidate_project.project_id != current.project_id:
                raise _McpWorkflowBoundaryError(
                    "creative_workflow.candidate_project_mismatch",
                    stage="candidate",
                )
            candidate_path = revalidate_plain_directory(identity)
        snapshot = decide_workflow_iteration_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            disposition=disposition,
            summary=summary,
            rationale=rationale,
            final_authority=authority,
            perception_basis=perception_basis,
            protected_values=[] if protected_values is None else protected_values,
            sacrificed_values=[] if sacrificed_values is None else sacrificed_values,
            evidence_ids=[] if evidence_ids is None else evidence_ids,
            exception_ids=[] if exception_ids is None else exception_ids,
            expected_audible_change=expected_audible_change,
            candidate_path=candidate_path,
        )
        return _workflow_success(
            operation,
            project_key,
            snapshot,
            decision_authority=authority,
        )
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Record workflow authoring revision", annotations=_AUTHORING_WRITE_TOOL)
def record_workflow_authoring_revision(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    authoring_revision: _AuthoringSelector,
) -> dict[str, Any]:
    """Bind a separately CAS-saved immutable authoring revision as the next iteration."""

    operation = "record_workflow_authoring_revision"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        checked_authoring = _validated_authoring_revision(
            authoring_revision,
            required=True,
        )
        assert checked_revision is not None and checked_authoring is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = record_workflow_authoring_revision_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            authoring_revision=checked_authoring,
        )
        return _workflow_success(operation, project_key, snapshot)
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Rollback creative workflow", annotations=_AUTHORING_WRITE_TOOL)
def rollback_creative_workflow(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    target_iteration_number: StrictInt,
    summary: _WorkflowText,
    rationale: _WorkflowText,
    perception_basis: Literal["report_only", "audio_audition"],
) -> dict[str, Any]:
    """Select an earlier immutable anchor without overwriting revisions or candidates."""

    operation = "rollback_creative_workflow"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        current = _open_expected_workflow(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
        )
        authority = _agent_workflow_authority(current)
        snapshot = rollback_workflow_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            target_iteration_number=target_iteration_number,
            summary=summary,
            rationale=rationale,
            final_authority=authority,
            perception_basis=perception_basis,
        )
        return _workflow_success(
            operation,
            project_key,
            snapshot,
            decision_authority=authority,
        )
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Cancel workflow render", annotations=_AUTHORING_WRITE_TOOL)
def cancel_workflow_render(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
) -> dict[str, Any]:
    """Cancel the sole current reservation without deleting any published candidate."""

    operation = "cancel_workflow_render"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        snapshot = cancel_workflow_render_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
        )
        return _workflow_success(operation, project_key, snapshot)
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Stop creative workflow", annotations=_AUTHORING_WRITE_TOOL)
def stop_creative_workflow(
    project_key: _AuthoringSelector,
    workflow_id: _AuthoringSelector,
    expected_revision: _AuthoringSelector,
    reason: Literal[
        "budget_exhausted",
        "no_material_improvement",
        "human_review_required",
        "external_blocker",
        "cancelled",
    ],
    summary: _WorkflowText,
    perception_basis: Literal["report_only", "audio_audition"] = "report_only",
) -> dict[str, Any]:
    """Stop under the frozen agent authority; never impersonate creator approval."""

    operation = "stop_creative_workflow"
    try:
        checked_id = _validated_workflow_id(workflow_id)
        checked_revision = _validated_workflow_revision(
            expected_revision,
            required=True,
        )
        assert checked_revision is not None
        root = _authoring_project_root(
            project_key,
            create_namespace=False,
            require_existing=True,
        )
        current = _open_expected_workflow(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
        )
        authority = _agent_workflow_authority(current)
        snapshot = terminate_creative_workflow_state(
            root,
            workflow_id=checked_id,
            expected_revision=checked_revision,
            reason=reason,
            summary=summary,
            final_authority=authority,
            perception_basis=perception_basis,
        )
        return _workflow_success(
            operation,
            project_key,
            snapshot,
            decision_authority=authority,
        )
    except (
        AuthoringProjectError,
        CreativeWorkflowError,
        _McpAuthoringBoundaryError,
        _McpWorkflowBoundaryError,
    ) as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )
    except Exception as exc:
        return _workflow_failure(
            operation,
            exc,
            project_key=project_key,
            workflow_id=workflow_id,
        )


@mcp_tool(title="Render candidate", annotations=_RENDER_TOOL)
def render(
    score: dict,
    roster: dict,
    title: str = "untitled",
    seed: StrictInt | None = None,
    expression: str | None = None,
    range_mode: str | None = None,
    normalize_peak_db: StrictFloat | None = None,
    hall: StrictBool | None = None,
    master_gain_db: StrictFloat | None = None,
    space_config: dict | None = None,
    collaboration_mode: str | None = None,
    write_stems: StrictBool | None = None,
    use_stem_cache: StrictBool | None = None,
    refresh_stem_cache: StrictBool | None = None,
    trusted_only: StrictBool | None = None,
    render_profile: dict | None = None,
    output_id: str | None = None,
    parent_candidate_id: str | None = None,
    overwrite: StrictBool = False,
    expected_receipt_sha256: str | None = None,
    expected_render_profile_sha256: str | None = None,
    instrument_scope: Literal["formal", "curated"] | None = None,
) -> dict[str, Any]:
    """把 score+roster 渲成 24bit 立体声 WAV(合奏 + 可选分轨),返回路径与客观仪表。

    仪表包含每声部峰值/复音、总线峰值、归一增益、时长和削波状态。显式选择
    collaboration_mode=analyze/suggest 时还会返回门控 active RMS、频带/立体声
    指标，以及 roster 明确声明的 balance_relations；suggest 只给有界建议，
    不会改动音频。它是机器排查，不代替人耳。厅堂默认开。
    默认 formal 范围允许全部正式声音入口；instrument_scope=curated 可只用
    作者策展子集。兼容参数 trusted_only 仍映射到这两个范围。
    许可证据为 quarantined 的入口始终拒绝，不能用该质量开关绕过。
    range_mode="compatibility" 保持旧可演奏范围并返回逐音风险摘要；
    "strict_hq" 对未获严格高质量证据或超出核心范围的音符直接拒绝。
    use_stem_cache=True 时会校验并复用增益前的原始乐器分轨；修改 gain、pan、
    厅堂或 master 只重新混音。refresh_stem_cache=True 强制重算原始分轨。
    validate_project 返回的 render_handoff 可原样传入；预期 profile Hash
    不一致时会在创建候选前拒绝，避免把不同配置的预检当成正式渲染依据。
    """
    try:
        resolved_scope, _allowed = _resolve_mcp_instrument_scope(
            instrument_scope,
            trusted_only,
        )
    except (TrustPolicyError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "code": "instrument.scope_invalid",
            "error": str(exc),
        }
    try:
        profile = _resolve_mcp_render_profile(
            render_profile=render_profile,
            seed=seed,
            expression=expression,
            range_mode=range_mode,
            normalize_peak_db=normalize_peak_db,
            hall=hall,
            master_gain_db=master_gain_db,
            space_config=space_config,
            collaboration_mode=collaboration_mode,
            write_stems=write_stems,
            use_stem_cache=use_stem_cache,
            refresh_stem_cache=refresh_stem_cache,
        )
    except (TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "error": f"render_profile 无效: {exc}",
        }
    resolved_profile = profile.to_dict()
    profile_sha256 = canonical_json_sha256(resolved_profile)
    if expected_render_profile_sha256 is not None:
        if not _is_lower_sha256(expected_render_profile_sha256):
            return {
                "kind": "tianlai.render_result",
                "schema_version": 2,
                "ok": False,
                "code": "render_profile.invalid_expected_sha256",
                "error": (
                    "expected_render_profile_sha256 必须是 64 位小写 "
                    "SHA-256"
                ),
                "render_profile_sha256": profile_sha256,
            }
        if expected_render_profile_sha256 != profile_sha256:
            return {
                "kind": "tianlai.render_result",
                "schema_version": 2,
                "ok": False,
                "code": "render_profile.preflight_mismatch",
                "error": (
                    "正式渲染解析出的 render profile 与预检交接 Hash "
                    "不一致；请原样复用 validate_project.render_handoff"
                ),
                "expected_render_profile_sha256": (
                    expected_render_profile_sha256
                ),
                "render_profile_sha256": profile_sha256,
                "resolved_render_profile": resolved_profile,
            }
    seed = profile.seed
    expression = profile.expression
    range_mode = profile.range_mode
    normalize_peak_db = profile.normalize_peak_db
    master_gain_db = profile.master_gain_db
    collaboration_mode = profile.collaboration_mode
    write_stems = profile.write_stems
    use_stem_cache = profile.use_stem_cache
    refresh_stem_cache = profile.refresh_stem_cache

    # 前置校验:普通声部与鼓组 kit 涉及的乐器是否都存在、可信(见 _assignment_instruments)
    bad = _roster_instrument_problems(
        roster,
        instrument_scope=resolved_scope,
    )
    if bad:
        return {"error": "编制里有不可用乐器", "offenders": bad}
    caps = _caps()

    try:
        score_doc = parse_score_document(score)
        validate_score_resource_limits(score, score_doc)
        roster_doc = parse_roster_document(roster, caps)
        settings = ExpressionSettings.from_dict(
            {
                "mode": expression,
                "range_mode": range_mode,
                "humanize": {"seed": int(seed)},
            }
        )
        plan = build_plan(score_doc, roster_doc, settings)
    except Exception as exc:  # 把校验错误如实回给 AI,让它改
        return {"error": f"乐谱/编制解析失败:{exc}"}

    try:
        resource_preflight = validate_render_request_resource_limits(
            plan,
            write_stems=profile.write_stems,
            space=profile.space,
            collaboration_mode=profile.collaboration_mode,
            stem_cache_enabled=profile.use_stem_cache,
        )
    except ResourceLimitError as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "error": str(exc),
            "render_preflight": exc.preflight,
            "render_profile_sha256": profile_sha256,
        }

    if not isinstance(overwrite, bool):
        return {"error": "overwrite 必须是布尔值"}
    plan_sha256 = canonical_json_sha256(plan.to_dict())
    review_settings = {
        "expression": expression,
        "seed": int(seed),
        "range_mode": range_mode,
        "trusted_only": resolved_scope == "curated",
        "instrument_scope": resolved_scope,
    }
    project_binding: dict[str, Any] = {
        "score_sha256": _canonical_json_sha256(score),
        "roster_sha256": _canonical_json_sha256(roster),
        "plan_input_sha256": _canonical_json_sha256(
            {
                "score": score,
                "roster": roster,
                "settings": review_settings,
            }
        ),
        "performance_plan_sha256": plan_sha256,
    }
    project_review = _safe_project_review(
        plan,
        roster_doc,
        binding=project_binding,
    )
    try:
        candidate_target = prepare_candidate_target(
            OUTPUT_DIR,
            title,
            plan_sha256=plan_sha256,
            output_id=output_id,
            overwrite=overwrite,
            expected_receipt_sha256=expected_receipt_sha256,
        )
    except (OSError, TypeError, ValueError) as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "error": str(exc),
        }
    work_id = candidate_target.work_id
    candidate_id = candidate_target.candidate_id
    directory = candidate_target.directory
    try:
        space = profile.space
        result = render_plan(
            plan,
            directory,
            write_stems=write_stems,
            master_gain_db=master_gain_db,
            normalize_peak_db=normalize_peak_db,
            space=space,
            collaboration_mode=collaboration_mode,
            stem_cache_directory=(
                OUTPUT_DIR.parent / ".tianlai-cache" / "stems"
                if use_stem_cache
                else None
            ),
            refresh_stem_cache=refresh_stem_cache,
            analysis_cache_directory=(
                OUTPUT_DIR.parent / ".tianlai-cache" / "analysis"
                if use_stem_cache
                else None
            ),
        )
    except (OSError, RuntimeError, ValueError, RenderLockError) as exc:
        return {
            "kind": "tianlai.render_result",
            "schema_version": 2,
            "ok": False,
            "error": str(exc),
        }

    mix_report = getattr(result, "mix_report", None)
    effective_collaboration_mode = getattr(
        result,
        "collaboration_mode",
        None,
    )
    if effective_collaboration_mode is None:
        effective_collaboration_mode = (
            collaboration_mode
            or (mix_report or {}).get("mode")
            or getattr(
                getattr(plan, "collaboration", None),
                "mode",
                "manual",
            )
        )
    metrics_by_executor = {
        row["executor_id"]: row["metrics"]
        for row in (mix_report or {}).get("stems", [])
    }
    candidate_manifest_path = directory / CANDIDATE_MANIFEST_NAME
    candidate_manifest: dict[str, Any] | None = None
    published_receipt = Path(result.receipt_path)
    if published_receipt.is_file():
        try:
            candidate_manifest = publish_candidate_metadata(
                candidate_target,
                title=title,
                score=score,
                roster=roster,
                render_profile=resolved_profile,
                receipt_path=published_receipt,
                plan_sha256=plan_sha256,
                parent_candidate_id=parent_candidate_id,
            )
        except (OSError, TypeError, ValueError) as exc:
            return {
                "kind": "tianlai.render_result",
                "schema_version": 2,
                "ok": False,
                "error": f"音频已渲染，但候选元数据写入失败: {exc}",
                "candidate_id": candidate_id,
                "candidate_directory": str(directory),
                "render_receipt": result.receipt_path,
                "post_render_check_path": getattr(
                    result,
                    "post_render_check_path",
                    None,
                ),
                "post_render_check": getattr(
                    result,
                    "post_render_check",
                    None,
                ),
                "post_render_check_summary": getattr(
                    result,
                    "post_render_check_summary",
                    None,
                ),
            }

    meter = {
        "kind": "tianlai.render_result",
        "schema_version": 2,
        "ok": True,
        "instrument_scope": resolved_scope,
        "trusted_only": resolved_scope == "curated",
        "candidate_id": candidate_id,
        "parent_candidate_id": parent_candidate_id,
        "candidate_directory": str(directory),
        "candidate_manifest": (
            str(candidate_manifest_path)
            if candidate_manifest is not None
            else None
        ),
        "mix_wav": str(directory / "合奏.wav"),
        "performance_plan": getattr(result, "plan_path", None),
        "render_receipt": result.receipt_path,
        "post_render_check_path": getattr(
            result,
            "post_render_check_path",
            None,
        ),
        "post_render_check": getattr(
            result,
            "post_render_check",
            None,
        ),
        "post_render_check_summary": getattr(
            result,
            "post_render_check_summary",
            None,
        ),
        "mix_report_path": getattr(result, "mix_report_path", None),
        "mix_report": mix_report,
        "license_sidecar": result.license_sidecar_path,
        "attribution_notice": result.attribution_path,
        "stems_dir": str(directory / "分轨") if write_stems else None,
        "duration_seconds": round(result.duration_seconds, 2),
        "mix_peak": round(result.mix_peak, 4),
        "clipped": result.mix_peak > 1.0,
        "normalize_gain_db": round(result.normalize_gain_db, 2),
        "hall": space.to_dict() if space else None,
        "render_profile_sha256": profile_sha256,
        "render_preflight": resource_preflight,
        "parts": [{"executor": s.executor_id, "peak": round(s.peak, 4),
                   "peak_voices": s.peak_voices,
                   "mix_metrics": metrics_by_executor.get(s.executor_id)}
                  for s in result.stems],
        "resolved_render_options": {
            "render_profile": resolved_profile,
            "expression": expression,
            "range_mode": range_mode,
            "master_gain_db": master_gain_db,
            "normalize_peak_db": normalize_peak_db,
            "write_stems": write_stems,
            "use_stem_cache": use_stem_cache,
            "refresh_stem_cache": refresh_stem_cache,
            "space": space.to_dict() if space else None,
            "collaboration_mode": effective_collaboration_mode,
        },
        "collaboration_warnings": _collaboration_warnings(roster_doc),
        "range_diagnostics": _range_diagnostic_summary(plan),
        "project_review": project_review,
        "stem_cache": getattr(result, "stem_cache", None),
        "analysis_cache": getattr(result, "analysis_cache", None),
        "cache_telemetry": getattr(
            result,
            "cache_telemetry_path",
            None,
        ),
        "note": (
            "这里只报告已测技术指标，不保证无缺陷、乐器真实性或作品质量；"
            "请以人耳判断为准。"
        ),
    }
    return meter


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
