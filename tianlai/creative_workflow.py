"""Optional, evidence-bound creative workflow governance for Tianlai.

This module is deliberately transport independent.  MCP can expose these
operations, but choosing MCP does not make the workflow mandatory and choosing
the workflow does not turn aesthetic advice into an engine contract.

The durable aggregate is compare-and-swap and append-only at the revision
layer.  Every transition publishes a complete immutable state revision before
atomically moving the small current pointer.  A workflow revision may become
orphaned after a crash; a partially published current state cannot.

The central authority boundary is intentionally asymmetric:

* verified engine-contract failures may block a render or acceptance;
* conflicts with a work charter never block automatically;
* aesthetic risks never block automatically and never trigger an edit;
* only a workflow-reserved, receipt-bound render can be accepted as complete.

Rollback selects an earlier immutable anchor.  It never overwrites an
authoring project revision or deletes a candidate.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
from typing import Any, Callable, Iterable, Mapping, Sequence

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    json_document_bytes,
    strict_json_loads,
)
from .authoring_core import validate_project_readiness
from .authoring_project import (
    PRIVATE_DIRECTORY_NAME,
    AuthoringProjectError,
    open_authoring_project,
)
from .candidate import (
    CANDIDATE_MANIFEST_NAME,
    load_candidate,
    portable_slug,
)
from .canonical_json import CANONICALIZATION, canonical_json_sha256
from .plain_file import (
    read_plain_file_bytes,
    revalidate_plain_file,
    sha256_plain_file,
)
from .render_lock import (
    PlainDirectoryIdentity,
    RenderLockError,
    acquire_render_lock,
    capture_plain_directory,
    ensure_authorized_child_directory,
    revalidate_plain_directory,
)
from .utc_timestamp import canonical_utc_now, validate_canonical_utc_timestamp
from .workflow_binding import validate_workflow_authorization


WORKFLOW_KIND = "tianlai.creative_workflow"
WORKFLOW_STATE_KIND = "tianlai.creative_workflow_state"
WORKFLOW_REVISION_KIND = "tianlai.creative_workflow_revision"
WORKFLOW_SNAPSHOT_KIND = "tianlai.creative_workflow_snapshot"
WORKFLOW_VERSION = 1
WORKFLOW_MANIFEST_NAME = "workflow.json"
WORKFLOW_STATE_NAME = "workflow-state.json"
WORKFLOW_REVISION_MANIFEST_NAME = "revision.json"
WORKFLOWS_DIRECTORY_NAME = "workflows"
WORKFLOW_REVISIONS_DIRECTORY_NAME = "revisions"
WORKFLOW_DIRECTORY_PREFIX = "workflow-"

WORKFLOW_MODES = frozenset({"off", "audit", "iterate"})
WORKFLOW_STATUSES = frozenset(
    {
        "disabled",
        "charter_pending",
        "reviewing",
        "candidate_pending",
        "revision_pending",
        "completed",
        "stopped",
    }
)
TERMINAL_WORKFLOW_STATUSES = frozenset({"disabled", "completed", "stopped"})
EVIDENCE_CATEGORIES = frozenset(
    {"hard_failure", "promise_conflict", "aesthetic_risk"}
)
REVIEW_PHASES = frozenset(
    {
        "intent",
        "symbolic_structure",
        "orchestration_performance",
        "render_report",
        "audio_audition",
    }
)
REVIEWERS = frozenset({"engine", "validator", "agent", "creator", "listener"})
PERCEPTION_BASES = frozenset({"report_only", "audio_audition"})
FINAL_AUTHORITIES = frozenset({"creator", "agent"})
SOVEREIGNTIES = frozenset({"M", "G", "H", "P", "T", "O", "N", "L", "R", "C", "X", "I"})

MAX_REVISION_CYCLES = 32
MAX_ROLLBACKS = 8
MAX_RENDER_ATTEMPTS_PER_ITERATION = 8
MAX_EVIDENCE_PER_ITERATION = 128
MAX_EXCEPTIONS_PER_ITERATION = 32
MAX_REVIEWS_PER_ITERATION = 32
MAX_ACTIVE_CLAUSES = 60
MAX_ITERATIONS = 1 + MAX_REVISION_CYCLES + MAX_ROLLBACKS
# The durable chain must cover every legal transition admitted by the public
# budgets, plus one transition that is reserved exclusively for termination.
_BUDGETED_TRANSITIONS_PER_ITERATION = (
    MAX_REVIEWS_PER_ITERATION
    + MAX_EVIDENCE_PER_ITERATION
    + MAX_EXCEPTIONS_PER_ITERATION
    + (2 * MAX_RENDER_ATTEMPTS_PER_ITERATION)
    + 4
)
MAX_WORKFLOW_HISTORY = max(
    16_384,
    2 + (MAX_ITERATIONS * _BUDGETED_TRANSITIONS_PER_ITERATION),
)
MAX_WORKFLOW_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_WORKFLOW_TEXT_BYTES = 16 * 1024

_WORKFLOW_ID = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CODE = re.compile(r"^[a-z0-9_]+(?:[._][a-z0-9_]+)*$")
_CLAUSE_ID = re.compile(r"^C[0-8](?:\.[A-Z])?(?:\.[0-9]{1,3}){1,2}$")
_PORTABLE_ID = re.compile(r"^[^/\\:\x00-\x1f\x7f]{1,128}$")
_REPARSE_POINT = 0x400
_WORKFLOW_LIMITS = AuthoringJsonLimits(
    max_document_bytes=MAX_WORKFLOW_DOCUMENT_BYTES,
    max_depth=64,
    max_nodes=100_000,
    max_string_bytes=MAX_WORKFLOW_TEXT_BYTES,
    max_array_items=4096,
    max_object_members=256,
)

_POLICY = {
    "hard_failures_may_block": True,
    "promise_conflicts_block_automatically": False,
    "aesthetic_risks_block_automatically": False,
    "automatic_score_changes": False,
    "automatic_audio_changes": False,
    "single_aesthetic_objective": False,
    "unresolved_candidates_preserved": True,
    "rollback_is_selection_not_overwrite": True,
    "acceptance_is_contextual_not_objective_quality": True,
}

_EVIDENCE_ARTIFACT_FIELDS = {
    "candidate_manifest": "candidate_manifest_sha256",
    "render_receipt": "render_receipt_sha256",
    "performance_plan": "performance_plan_sha256",
    "performance_plan_file": "performance_plan_file_sha256",
    "mix": "mix_sha256",
    "post_render_check": "post_render_check_sha256",
    "mix_report": "mix_report_sha256",
}
_MEASUREMENT_ARTIFACT_ROLES = frozenset(
    {"render_receipt", "post_render_check", "mix_report"}
)


class CreativeWorkflowError(RuntimeError):
    """Stable, path-free workflow failure suitable for a public transport."""

    def __init__(
        self,
        code: str,
        *,
        source: str = "workflow",
        location_segments: Iterable[str | int] = (),
    ) -> None:
        self.code = code
        self.message_key = f"creativeWorkflow.{code.replace('.', '_')}"
        self.source = source
        self.location_segments = tuple(location_segments)
        super().__init__(code)

    def to_issue(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message_key": self.message_key,
            "source": self.source,
            "severity": "error",
            "decision": "block",
            "location": {"segments": list(self.location_segments)},
        }


class _FrozenObject(dict[str, Any]):
    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("creative workflow snapshots are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __copy__(self) -> _FrozenObject:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenObject:
        return self


class _FrozenArray(list[Any]):
    @staticmethod
    def _immutable(*_args: object, **_kwargs: object) -> None:
        raise TypeError("creative workflow snapshots are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable

    def __copy__(self) -> _FrozenArray:
        return self

    def __deepcopy__(self, _memo: dict[int, Any]) -> _FrozenArray:
        return self


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return _FrozenObject((key, _freeze(item)) for key, item in value.items())
    if isinstance(value, list):
        return _FrozenArray(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class CreativeWorkflowSnapshot:
    workflow_id: str
    project_id: str
    revision: str
    created_at_utc: str
    updated_at_utc: str
    state: dict[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _freeze(copy.deepcopy(self.state)))

    def detached_state(self) -> dict[str, Any]:
        result = _thaw(self.state)
        assert isinstance(result, dict)
        return result

    def to_dict(self) -> dict[str, Any]:
        state = self.detached_state()
        return {
            "kind": WORKFLOW_SNAPSHOT_KIND,
            "schema_version": WORKFLOW_VERSION,
            "workflow": {
                "workflow_id": self.workflow_id,
                "project_id": self.project_id,
                "revision": self.revision,
                "created_at_utc": self.created_at_utc,
                "updated_at_utc": self.updated_at_utc,
                "sequence": state["sequence"],
                "parent_revision": state["parent_revision"],
                "mode": state["mode"],
                "status": state["status"],
            },
            "state": state,
            "allowed_actions": _allowed_actions(state),
        }


@dataclass(frozen=True, slots=True)
class _WorkflowLayout:
    project_root: Path
    project_id: str
    workflows: PlainDirectoryIdentity
    workflow: PlainDirectoryIdentity
    revisions: PlainDirectoryIdentity


def _extended_windows_path(path: Path) -> Path:
    """Address private revision files beyond the legacy Windows MAX_PATH."""

    if os.name != "nt":
        return path
    text = str(path.absolute())
    if text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def _revision_path(layout: _WorkflowLayout, name: str) -> Path:
    return _extended_windows_path(layout.revisions.path / name)


def _now() -> str:
    return canonical_utc_now()


def _plain_file_status(path: Path, *, code: str) -> os.stat_result:
    try:
        status = os.lstat(path)
    except OSError as exc:
        raise CreativeWorkflowError(code) from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or stat.S_ISLNK(status.st_mode)
        or bool(getattr(status, "st_file_attributes", 0) & _REPARSE_POINT)
        or status.st_nlink != 1
    ):
        raise CreativeWorkflowError(code)
    return status


def _require_hex(value: object, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise CreativeWorkflowError(code)
    return value


def _checked_workflow_id(value: object) -> str:
    return _require_hex(value, _WORKFLOW_ID, "invalid_workflow_id")


def _checked_revision(value: object, code: str = "invalid_workflow_revision") -> str:
    return _require_hex(value, _SHA256, code)


def _checked_authoring_revision(value: object) -> str:
    return _require_hex(value, _SHA256, "invalid_authoring_revision")


def _bounded_text(
    value: object,
    *,
    field: str,
    maximum_bytes: int = 4096,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise CreativeWorkflowError("invalid_text", location_segments=(field,))
    text = value.strip()
    try:
        size = len(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise CreativeWorkflowError(
            "invalid_text", location_segments=(field,)
        ) from exc
    if (not text and not allow_empty) or size > maximum_bytes:
        raise CreativeWorkflowError("invalid_text", location_segments=(field,))
    return text


def _bounded_text_list(
    value: object,
    *,
    field: str,
    maximum_items: int,
    item_bytes: int = 2048,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        raise CreativeWorkflowError("invalid_text_list", location_segments=(field,))
    result = [
        _bounded_text(item, field=f"{field}[{index}]", maximum_bytes=item_bytes)
        for index, item in enumerate(value)
    ]
    if not allow_empty and not result:
        raise CreativeWorkflowError("invalid_text_list", location_segments=(field,))
    if len(set(result)) != len(result):
        raise CreativeWorkflowError("duplicate_list_item", location_segments=(field,))
    return result


def _json_detach(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CreativeWorkflowError("object_required", location_segments=(field,))
    try:
        payload = json_document_bytes(dict(value), limits=_WORKFLOW_LIMITS)
        detached = strict_json_loads(payload, limits=_WORKFLOW_LIMITS)
    except AuthoringJsonError as exc:
        raise CreativeWorkflowError(
            f"json.{exc.code}", location_segments=(field, *exc.location_segments)
        ) from exc
    assert isinstance(detached, dict)
    return detached


def _finite_optional_number(value: object, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CreativeWorkflowError("finite_number_required", location_segments=(field,))
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise CreativeWorkflowError("finite_number_required", location_segments=(field,))
    return number


def _portable_identifier(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _PORTABLE_ID.fullmatch(value) is None:
        raise CreativeWorkflowError("invalid_identifier", location_segments=(field,))
    return value


def _canonical_timestamp(value: object, *, code: str) -> str:
    try:
        return validate_canonical_utc_timestamp(value)
    except ValueError as exc:
        raise CreativeWorkflowError(code) from exc


def _file_sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _workflow_revision_identity(
    *,
    workflow_id: str,
    project_id: str,
    sequence: int,
    parent_revision: str | None,
    state_sha256: str,
) -> str:
    return canonical_json_sha256(
        {
            "kind": "tianlai.creative_workflow_revision_binding",
            "schema_version": WORKFLOW_VERSION,
            "workflow_id": workflow_id,
            "project_id": project_id,
            "sequence": sequence,
            "parent_revision": parent_revision,
            "state_sha256": state_sha256,
        }
    )


def _write_new_file(path: Path, payload: bytes) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CreativeWorkflowError("workflow_write_failed") from exc


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _read_json_file(
    path: Path, *, source: str, limits: AuthoringJsonLimits = _WORKFLOW_LIMITS
) -> tuple[dict[str, Any], bytes]:
    try:
        identity, payload = read_plain_file_bytes(
            path, maximum_bytes=limits.max_document_bytes
        )
        document = strict_json_loads(payload, limits=limits)
        revalidate_plain_file(identity)
    except (OSError, AuthoringJsonError) as exc:
        raise CreativeWorkflowError("invalid_workflow_file", source=source) from exc
    assert isinstance(document, dict)
    return document, payload


def _project_workflows_layout(
    project_root: str | os.PathLike[str], *, create: bool
) -> tuple[Path, str, PlainDirectoryIdentity]:
    try:
        root_identity = capture_plain_directory(project_root)
        root = revalidate_plain_directory(root_identity)
        authoring = open_authoring_project(root)
        private_identity = capture_plain_directory(root / PRIVATE_DIRECTORY_NAME)
        revalidate_plain_directory(root_identity)
        if create:
            workflows = ensure_authorized_child_directory(
                private_identity, WORKFLOWS_DIRECTORY_NAME
            )
        else:
            workflows = capture_plain_directory(
                private_identity.path / WORKFLOWS_DIRECTORY_NAME
            )
        revalidate_plain_directory(private_identity)
        return root, authoring.project_id, workflows
    except AuthoringProjectError as exc:
        raise CreativeWorkflowError("authoring_project_unavailable") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("unsafe_workflow_root") from exc


def _existing_layout(
    project_root: str | os.PathLike[str], workflow_id: str
) -> _WorkflowLayout:
    checked_id = _checked_workflow_id(workflow_id)
    root, project_id, workflows = _project_workflows_layout(project_root, create=False)
    try:
        workflow = capture_plain_directory(
            workflows.path / f"{WORKFLOW_DIRECTORY_PREFIX}{checked_id}"
        )
        revisions = capture_plain_directory(
            workflow.path / WORKFLOW_REVISIONS_DIRECTORY_NAME
        )
        revalidate_plain_directory(workflows)
    except OSError as exc:
        raise CreativeWorkflowError("workflow_not_found") from exc
    return _WorkflowLayout(root, project_id, workflows, workflow, revisions)


def _create_layout(
    project_root: str | os.PathLike[str], workflow_id: str
) -> _WorkflowLayout:
    root, project_id, workflows = _project_workflows_layout(project_root, create=True)
    name = f"{WORKFLOW_DIRECTORY_PREFIX}{workflow_id}"
    path = workflows.path / name
    if os.path.lexists(path):
        raise CreativeWorkflowError("workflow_id_collision")
    try:
        workflow = ensure_authorized_child_directory(workflows, name)
        revisions = ensure_authorized_child_directory(
            workflow, WORKFLOW_REVISIONS_DIRECTORY_NAME
        )
        revalidate_plain_directory(workflows)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("workflow_create_failed") from exc
    return _WorkflowLayout(root, project_id, workflows, workflow, revisions)


def _cleanup_failed_layout(_layout: _WorkflowLayout) -> None:
    # A hostile process could replace a path between the final identity check
    # and recursive deletion.  Failed workflow creation therefore preserves a
    # private random orphan for inspection instead of risking deletion through
    # a swapped directory entry.
    return


def _default_budget(mode: str) -> dict[str, int]:
    return {
        "max_revision_cycles": 4 if mode == "iterate" else 0,
        "max_rollbacks": 2 if mode == "iterate" else 0,
        "max_render_attempts_per_iteration": 3,
        "max_evidence_items_per_iteration": 64,
        "max_exceptions_per_iteration": 16,
        "max_reviews_per_iteration": 16,
    }


def _normalize_budget(mode: str, value: Mapping[str, Any] | None) -> dict[str, int]:
    result = _default_budget(mode)
    if value is not None:
        raw = _json_detach(value, field="budget")
        if set(raw) - set(result):
            raise CreativeWorkflowError("unknown_budget_field")
        result.update(raw)
    limits = {
        "max_revision_cycles": (0, MAX_REVISION_CYCLES),
        "max_rollbacks": (0, MAX_ROLLBACKS),
        "max_render_attempts_per_iteration": (1, MAX_RENDER_ATTEMPTS_PER_ITERATION),
        "max_evidence_items_per_iteration": (1, MAX_EVIDENCE_PER_ITERATION),
        "max_exceptions_per_iteration": (0, MAX_EXCEPTIONS_PER_ITERATION),
        "max_reviews_per_iteration": (1, MAX_REVIEWS_PER_ITERATION),
    }
    for field, (minimum, maximum) in limits.items():
        item = result[field]
        if isinstance(item, bool) or not isinstance(item, int) or not minimum <= item <= maximum:
            raise CreativeWorkflowError("invalid_budget", location_segments=(field,))
    if mode != "iterate" and (
        result["max_revision_cycles"] != 0 or result["max_rollbacks"] != 0
    ):
        raise CreativeWorkflowError("mode_budget_conflict")
    return result


def _empty_anchor(authoring_revision: str) -> dict[str, Any]:
    return {
        "authoring_revision": authoring_revision,
        "parent_candidate": None,
        "candidate": None,
    }


def _new_iteration(
    number: int,
    *,
    authoring_revision: str,
    parent_candidate: dict[str, str] | None,
    candidate: dict[str, Any] | None = None,
    opened_at_utc: str,
) -> dict[str, Any]:
    if not 1 <= number <= MAX_ITERATIONS:
        raise CreativeWorkflowError("iteration_limit_exceeded")
    return {
        "iteration_number": number,
        "iteration_id": f"iteration-{number:04d}",
        "status": "reviewing",
        "opened_at_utc": opened_at_utc,
        "closed_at_utc": None,
        "anchor": {
            "authoring_revision": authoring_revision,
            "parent_candidate": copy.deepcopy(parent_candidate),
            "candidate": copy.deepcopy(candidate),
        },
        "reviews": [],
        "evidence": [],
        "exceptions": [],
        "render_attempts": [],
        "decision": None,
        "outcome": None,
        "next_authoring_revision": None,
    }


def _disabled_termination(
    timestamp: str, *, final_authority: str
) -> dict[str, Any]:
    return {
        "reason": "mode_off",
        "summary": "Optional creative workflow was disabled by configuration.",
        "final_authority": final_authority,
        "perception_basis": "report_only",
        "selected_candidate": None,
        "terminated_at_utc": timestamp,
    }


def _initial_state(
    *,
    workflow_id: str,
    project_id: str,
    mode: str,
    authoring_revision: str,
    budget: dict[str, int],
    final_authority: str,
    timestamp: str,
) -> dict[str, Any]:
    disabled = mode == "off"
    return {
        "kind": WORKFLOW_STATE_KIND,
        "schema_version": WORKFLOW_VERSION,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "mode": mode,
        "status": "disabled" if disabled else "charter_pending",
        "sequence": 1,
        "parent_revision": None,
        "final_authority": final_authority,
        "created_at_utc": timestamp,
        "updated_at_utc": timestamp,
        "initial_anchor": _empty_anchor(authoring_revision),
        "budget": budget,
        "usage": {
            "revision_cycles": 0,
            "rollbacks": 0,
            "render_attempts": 0,
            "evidence_items": 0,
            "exceptions": 0,
            "reviews": 0,
        },
        "constitution": None,
        "work_charter": None,
        "active_clauses": [],
        "iterations": [],
        "termination": (
            _disabled_termination(timestamp, final_authority=final_authority)
            if disabled
            else None
        ),
        "policy": copy.deepcopy(_POLICY),
    }


def _manifest_document(
    *,
    workflow_id: str,
    project_id: str,
    created_at_utc: str,
    updated_at_utc: str,
    revision: str,
    sequence: int,
) -> dict[str, Any]:
    return {
        "kind": WORKFLOW_KIND,
        "schema_version": WORKFLOW_VERSION,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "created_at_utc": created_at_utc,
        "updated_at_utc": updated_at_utc,
        "current_revision": revision,
        "current_sequence": sequence,
    }


def _validate_manifest(document: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "kind",
        "schema_version",
        "workflow_id",
        "project_id",
        "created_at_utc",
        "updated_at_utc",
        "current_revision",
        "current_sequence",
    }
    if set(document) != expected or document.get("kind") != WORKFLOW_KIND or document.get(
        "schema_version"
    ) != WORKFLOW_VERSION:
        raise CreativeWorkflowError("invalid_workflow_manifest")
    workflow_id = _checked_workflow_id(document.get("workflow_id"))
    project_id = _require_hex(document.get("project_id"), _WORKFLOW_ID, "invalid_project_id")
    revision = _checked_revision(document.get("current_revision"))
    sequence = document.get("current_sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= MAX_WORKFLOW_HISTORY
    ):
        raise CreativeWorkflowError("invalid_workflow_manifest")
    created = _canonical_timestamp(document.get("created_at_utc"), code="invalid_workflow_manifest")
    updated = _canonical_timestamp(document.get("updated_at_utc"), code="invalid_workflow_manifest")
    if updated < created:
        raise CreativeWorkflowError("invalid_workflow_manifest")
    return {
        **document,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "current_revision": revision,
    }


def _read_manifest(layout: _WorkflowLayout) -> dict[str, Any]:
    document, _ = _read_json_file(
        layout.workflow.path / WORKFLOW_MANIFEST_NAME, source="workflow"
    )
    manifest = _validate_manifest(document)
    if manifest["project_id"] != layout.project_id:
        raise CreativeWorkflowError("workflow_project_mismatch")
    if manifest["workflow_id"] != layout.workflow.path.name.removeprefix(
        WORKFLOW_DIRECTORY_PREFIX
    ):
        raise CreativeWorkflowError("workflow_identity_mismatch")
    return manifest


def _replace_manifest(layout: _WorkflowLayout, manifest: dict[str, Any]) -> None:
    target = layout.workflow.path / WORKFLOW_MANIFEST_NAME
    stage = layout.workflow.path / f".{WORKFLOW_MANIFEST_NAME}.stage-{secrets.token_hex(16)}"
    try:
        if os.path.lexists(target):
            _plain_file_status(target, code="unsafe_workflow_manifest")
        _write_new_file(stage, json_document_bytes(manifest, limits=_WORKFLOW_LIMITS))
        staged, _ = _read_json_file(stage, source="workflow")
        if _validate_manifest(staged) != manifest:
            raise CreativeWorkflowError("workflow_manifest_staging_mismatch")
        revalidate_plain_directory(layout.workflow)
        os.replace(stage, target)
        _fsync_directory(layout.workflow.path)
        if _read_manifest(layout) != manifest:
            raise CreativeWorkflowError("workflow_manifest_publish_mismatch")
    except CreativeWorkflowError:
        raise
    except OSError as exc:
        raise CreativeWorkflowError("workflow_manifest_publish_failed") from exc
    # A failed, randomly named private stage is retained.  Deleting it through
    # the pathname after a publication error would reintroduce the same
    # validation-to-unlink race that workflow directory cleanup avoids.


def _revision_manifest(
    *,
    workflow_id: str,
    project_id: str,
    revision: str,
    sequence: int,
    parent_revision: str | None,
    created_at_utc: str,
    state: dict[str, Any],
    payload: bytes,
) -> dict[str, Any]:
    return {
        "kind": WORKFLOW_REVISION_KIND,
        "schema_version": WORKFLOW_VERSION,
        "workflow_id": workflow_id,
        "project_id": project_id,
        "revision": revision,
        "sequence": sequence,
        "parent_revision": parent_revision,
        "created_at_utc": created_at_utc,
        "canonicalization": CANONICALIZATION,
        "state": {
            "filename": WORKFLOW_STATE_NAME,
            "canonical_sha256": canonical_json_sha256(state),
            "file_sha256": _file_sha256(payload),
            "byte_length": len(payload),
        },
    }


def _validate_revision_directory(
    directory: Path,
    *,
    workflow_id: str,
    project_id: str,
    revision: str,
) -> dict[str, Any]:
    try:
        identity = capture_plain_directory(directory)
        entries = {entry.name for entry in os.scandir(identity.path)}
    except OSError as exc:
        raise CreativeWorkflowError("unsafe_workflow_revision") from exc
    if entries != {WORKFLOW_STATE_NAME, WORKFLOW_REVISION_MANIFEST_NAME}:
        raise CreativeWorkflowError("invalid_workflow_revision_shape")
    metadata, _ = _read_json_file(
        directory / WORKFLOW_REVISION_MANIFEST_NAME, source="workflow_revision"
    )
    expected_metadata = {
        "kind",
        "schema_version",
        "workflow_id",
        "project_id",
        "revision",
        "sequence",
        "parent_revision",
        "created_at_utc",
        "canonicalization",
        "state",
    }
    if (
        set(metadata) != expected_metadata
        or metadata.get("kind") != WORKFLOW_REVISION_KIND
        or metadata.get("schema_version") != WORKFLOW_VERSION
        or metadata.get("workflow_id") != workflow_id
        or metadata.get("project_id") != project_id
        or metadata.get("revision") != revision
        or metadata.get("canonicalization") != CANONICALIZATION
    ):
        raise CreativeWorkflowError("invalid_workflow_revision_manifest")
    sequence = metadata.get("sequence")
    parent_revision = metadata.get("parent_revision")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= MAX_WORKFLOW_HISTORY
        or (sequence == 1) is not (parent_revision is None)
    ):
        raise CreativeWorkflowError("invalid_workflow_revision_manifest")
    if parent_revision is not None:
        _checked_revision(
            parent_revision, code="invalid_workflow_revision_manifest"
        )
    _canonical_timestamp(
        metadata.get("created_at_utc"), code="invalid_workflow_revision_manifest"
    )
    binding = metadata.get("state")
    if not isinstance(binding, dict) or set(binding) != {
        "filename",
        "canonical_sha256",
        "file_sha256",
        "byte_length",
    }:
        raise CreativeWorkflowError("invalid_workflow_revision_manifest")
    if binding.get("filename") != WORKFLOW_STATE_NAME:
        raise CreativeWorkflowError("invalid_workflow_revision_manifest")
    canonical_hash = _checked_revision(
        binding.get("canonical_sha256"), code="invalid_workflow_revision_manifest"
    )
    file_hash = _checked_revision(
        binding.get("file_sha256"), code="invalid_workflow_revision_manifest"
    )
    byte_length = binding.get("byte_length")
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or not 1 <= byte_length <= MAX_WORKFLOW_DOCUMENT_BYTES
    ):
        raise CreativeWorkflowError("invalid_workflow_revision_manifest")
    state, payload = _read_json_file(directory / WORKFLOW_STATE_NAME, source="workflow_state")
    if (
        len(payload) != byte_length
        or _file_sha256(payload) != file_hash
        or canonical_json_sha256(state) != canonical_hash
    ):
        raise CreativeWorkflowError("workflow_revision_tampered")
    if _workflow_revision_identity(
        workflow_id=workflow_id,
        project_id=project_id,
        sequence=sequence,
        parent_revision=parent_revision,
        state_sha256=canonical_hash,
    ) != revision:
        raise CreativeWorkflowError("workflow_revision_identity_mismatch")
    _validate_state_document(state)
    if state["workflow_id"] != workflow_id or state["project_id"] != project_id:
        raise CreativeWorkflowError("workflow_state_identity_mismatch")
    if state["sequence"] != sequence or state["parent_revision"] != parent_revision:
        raise CreativeWorkflowError("workflow_revision_lineage_mismatch")
    revalidate_plain_directory(identity)
    return state


def _publish_revision(layout: _WorkflowLayout, state: dict[str, Any]) -> str:
    _validate_state_document(state)
    payload = json_document_bytes(state, limits=_WORKFLOW_LIMITS)
    state_hash = canonical_json_sha256(state)
    revision = _workflow_revision_identity(
        workflow_id=state["workflow_id"],
        project_id=state["project_id"],
        sequence=state["sequence"],
        parent_revision=state["parent_revision"],
        state_sha256=state_hash,
    )
    final = _revision_path(layout, revision)
    if os.path.lexists(final):
        existing = _validate_revision_directory(
            final,
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            revision=revision,
        )
        if existing != state:
            raise CreativeWorkflowError("workflow_revision_collision")
        return revision
    stage = _revision_path(
        layout, f".revision-stage-{secrets.token_hex(16)}"
    )
    try:
        os.mkdir(stage)
        stage_identity = capture_plain_directory(stage)
        _write_new_file(stage / WORKFLOW_STATE_NAME, payload)
        metadata = _revision_manifest(
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            revision=revision,
            sequence=state["sequence"],
            parent_revision=state["parent_revision"],
            created_at_utc=state["updated_at_utc"],
            state=state,
            payload=payload,
        )
        _write_new_file(
            stage / WORKFLOW_REVISION_MANIFEST_NAME,
            json_document_bytes(metadata, limits=_WORKFLOW_LIMITS),
        )
        _fsync_directory(stage)
        _validate_revision_directory(
            stage,
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            revision=revision,
        )
        revalidate_plain_directory(layout.revisions)
        revalidate_plain_directory(stage_identity)
        if os.path.lexists(final):
            raise CreativeWorkflowError("workflow_revision_publish_conflict")
        os.replace(stage, final)
        _fsync_directory(layout.revisions.path)
        _validate_revision_directory(
            final,
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            revision=revision,
        )
        return revision
    except CreativeWorkflowError:
        raise
    except OSError as exc:
        raise CreativeWorkflowError("workflow_revision_publish_failed") from exc
    # A failed private stage is deliberately preserved.  Recursive cleanup
    # cannot be made race-free against a hostile directory-entry replacement
    # with the portable primitives used here.


def _normalize_vector_value(value: object, *, field: str) -> str | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise CreativeWorkflowError("invalid_vector_value", location_segments=(field,))
    if isinstance(value, (int, float)):
        number = float(value)
        if math.isfinite(number) and -1.0 <= number <= 1.0:
            return number
        raise CreativeWorkflowError("invalid_vector_value", location_segments=(field,))
    return _bounded_text(value, field=field, maximum_bytes=512)


def _normalize_named_vector(
    value: object,
    *,
    field: str,
    dimensions: Sequence[str],
) -> dict[str, str | float | None]:
    if value is None:
        raw: dict[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = _json_detach(value, field=field)
    else:
        raise CreativeWorkflowError("object_required", location_segments=(field,))
    if set(raw) - set(dimensions):
        raise CreativeWorkflowError("unknown_vector_dimension", location_segments=(field,))
    return {
        dimension: _normalize_vector_value(
            raw.get(dimension), field=f"{field}.{dimension}"
        )
        for dimension in dimensions
    }


def _normalize_curve(value: object, *, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 64:
        raise CreativeWorkflowError("invalid_curve", location_segments=(field,))
    result: list[dict[str, Any]] = []
    previous = -1.0
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise CreativeWorkflowError(
                "invalid_curve", location_segments=(field, index)
            )
        row = _json_detach(item, field=f"{field}[{index}]")
        if set(row) - {"position", "label", "intent"} or not {
            "position",
            "intent",
        }.issubset(row):
            raise CreativeWorkflowError(
                "invalid_curve", location_segments=(field, index)
            )
        position = row["position"]
        if (
            isinstance(position, bool)
            or not isinstance(position, (int, float))
            or not math.isfinite(float(position))
            or not 0.0 <= float(position) <= 1.0
            or float(position) <= previous
        ):
            raise CreativeWorkflowError(
                "invalid_curve_position", location_segments=(field, index)
            )
        previous = float(position)
        label = row.get("label")
        result.append(
            {
                "position": previous,
                "label": (
                    None
                    if label is None
                    else _bounded_text(
                        label,
                        field=f"{field}[{index}].label",
                        maximum_bytes=256,
                    )
                ),
                "intent": _bounded_text(
                    row["intent"],
                    field=f"{field}[{index}].intent",
                    maximum_bytes=1024,
                ),
            }
        )
    return result


def _normalize_work_charter(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = _json_detach(value, field="work_charter")
    allowed = {
        "title",
        "one_sentence_promise",
        "target_listener_and_scene",
        "primary_sovereignty",
        "secondary_sovereignties",
        "style_recipe",
        "affect_vector",
        "motion_vector",
        "identity_kernel",
        "dramatic_question",
        "energy_curve",
        "tension_curve",
        "memory_landmarks",
        "scarce_resources",
        "climax_privileges",
        "prohibited_shortcuts",
        "ending_contract",
        "uncertainties",
        "final_review_dimensions",
    }
    required = {
        "title",
        "one_sentence_promise",
        "target_listener_and_scene",
        "primary_sovereignty",
        "identity_kernel",
        "ending_contract",
    }
    if set(raw) - allowed or not required.issubset(raw):
        raise CreativeWorkflowError("invalid_work_charter_shape")
    primary = raw["primary_sovereignty"]
    if not isinstance(primary, list) or not 1 <= len(primary) <= 3:
        raise CreativeWorkflowError(
            "invalid_primary_sovereignty",
            location_segments=("work_charter", "primary_sovereignty"),
        )
    if any(item not in SOVEREIGNTIES for item in primary) or len(set(primary)) != len(
        primary
    ):
        raise CreativeWorkflowError("invalid_primary_sovereignty")
    secondary = raw.get("secondary_sovereignties", [])
    if not isinstance(secondary, list) or len(secondary) > len(SOVEREIGNTIES):
        raise CreativeWorkflowError("invalid_secondary_sovereignties")
    if (
        any(item not in SOVEREIGNTIES for item in secondary)
        or len(set(secondary)) != len(secondary)
        or set(primary) & set(secondary)
    ):
        raise CreativeWorkflowError("invalid_secondary_sovereignties")
    kernel = raw["identity_kernel"]
    if not isinstance(kernel, Mapping):
        raise CreativeWorkflowError("invalid_identity_kernel")
    kernel_raw = _json_detach(kernel, field="work_charter.identity_kernel")
    if set(kernel_raw) != {"invariants", "transformable_parts"}:
        raise CreativeWorkflowError("invalid_identity_kernel")
    dramatic = raw.get("dramatic_question")
    return {
        "title": _bounded_text(
            raw["title"], field="work_charter.title", maximum_bytes=1024
        ),
        "one_sentence_promise": _bounded_text(
            raw["one_sentence_promise"],
            field="work_charter.one_sentence_promise",
            maximum_bytes=4096,
        ),
        "target_listener_and_scene": _bounded_text(
            raw["target_listener_and_scene"],
            field="work_charter.target_listener_and_scene",
            maximum_bytes=4096,
        ),
        "primary_sovereignty": list(primary),
        "secondary_sovereignties": list(secondary),
        "style_recipe": _bounded_text(
            raw.get("style_recipe", "unspecified"),
            field="work_charter.style_recipe",
            maximum_bytes=4096,
        ),
        "affect_vector": _normalize_named_vector(
            raw.get("affect_vector"),
            field="work_charter.affect_vector",
            dimensions=(
                "valence",
                "arousal",
                "motion",
                "tension",
                "agency",
                "intimacy",
                "scale",
                "time_orientation",
                "closure",
            ),
        ),
        "motion_vector": _normalize_named_vector(
            raw.get("motion_vector"),
            field="work_charter.motion_vector",
            dimensions=(
                "perceived_pulse",
                "onset_density",
                "subdivision",
                "harmonic_rhythm",
                "pitch_motion",
                "envelope",
                "textural_motion",
            ),
        ),
        "identity_kernel": {
            "invariants": _bounded_text_list(
                kernel_raw["invariants"],
                field="work_charter.identity_kernel.invariants",
                maximum_items=32,
            ),
            "transformable_parts": _bounded_text_list(
                kernel_raw["transformable_parts"],
                field="work_charter.identity_kernel.transformable_parts",
                maximum_items=32,
            ),
        },
        "dramatic_question": (
            None
            if dramatic is None
            else _bounded_text(
                dramatic,
                field="work_charter.dramatic_question",
                maximum_bytes=2048,
            )
        ),
        "energy_curve": _normalize_curve(
            raw.get("energy_curve"), field="work_charter.energy_curve"
        ),
        "tension_curve": _normalize_curve(
            raw.get("tension_curve"), field="work_charter.tension_curve"
        ),
        "memory_landmarks": _bounded_text_list(
            raw.get("memory_landmarks", []),
            field="work_charter.memory_landmarks",
            maximum_items=32,
        ),
        "scarce_resources": _bounded_text_list(
            raw.get("scarce_resources", []),
            field="work_charter.scarce_resources",
            maximum_items=32,
        ),
        "climax_privileges": _bounded_text_list(
            raw.get("climax_privileges", []),
            field="work_charter.climax_privileges",
            maximum_items=32,
        ),
        "prohibited_shortcuts": _bounded_text_list(
            raw.get("prohibited_shortcuts", []),
            field="work_charter.prohibited_shortcuts",
            maximum_items=32,
        ),
        "ending_contract": _bounded_text(
            raw["ending_contract"],
            field="work_charter.ending_contract",
            maximum_bytes=2048,
        ),
        "uncertainties": _bounded_text_list(
            raw.get("uncertainties", []),
            field="work_charter.uncertainties",
            maximum_items=32,
        ),
        "final_review_dimensions": _bounded_text_list(
            raw.get("final_review_dimensions", []),
            field="work_charter.final_review_dimensions",
            maximum_items=32,
        ),
    }


def _normalize_constitution(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    raw = _json_detach(value, field="constitution")
    if set(raw) != {"document_id", "version", "language", "content_sha256"}:
        raise CreativeWorkflowError("invalid_constitution_binding")
    return {
        "document_id": _bounded_text(
            raw["document_id"], field="constitution.document_id", maximum_bytes=256
        ),
        "version": _bounded_text(
            raw["version"], field="constitution.version", maximum_bytes=64
        ),
        "language": _bounded_text(
            raw["language"], field="constitution.language", maximum_bytes=32
        ),
        "content_sha256": _checked_revision(
            raw["content_sha256"], code="invalid_constitution_hash"
        ),
    }


def _normalize_active_clauses(value: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)) or len(value) > MAX_ACTIVE_CLAUSES:
        raise CreativeWorkflowError("invalid_active_clauses")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        raw = _json_detach(item, field=f"active_clauses[{index}]")
        if set(raw) != {"clause_id", "role", "rationale", "interpretation"}:
            raise CreativeWorkflowError("invalid_active_clause")
        clause_id = raw["clause_id"]
        if not isinstance(clause_id, str) or _CLAUSE_ID.fullmatch(clause_id) is None:
            raise CreativeWorkflowError("invalid_clause_id")
        if clause_id in seen:
            raise CreativeWorkflowError("duplicate_active_clause")
        seen.add(clause_id)
        role = raw["role"]
        if role not in {"question", "constraint", "review_lens"}:
            raise CreativeWorkflowError("invalid_clause_role")
        result.append(
            {
                "clause_id": clause_id,
                "role": role,
                "rationale": _bounded_text(
                    raw["rationale"],
                    field=f"active_clauses[{index}].rationale",
                    maximum_bytes=2048,
                ),
                "interpretation": _bounded_text(
                    raw["interpretation"],
                    field=f"active_clauses[{index}].interpretation",
                    maximum_bytes=4096,
                ),
            }
        )
    return result


def _normalize_scope(
    value: Mapping[str, Any] | None,
    *,
    authoring_revision: str,
    candidate_id: str | None,
) -> dict[str, Any]:
    raw = {} if value is None else _json_detach(value, field="scope")
    if set(raw) - {"start_seconds", "end_seconds", "event_ids", "part_ids"}:
        raise CreativeWorkflowError("invalid_evidence_scope")
    start = _finite_optional_number(raw.get("start_seconds"), field="scope.start_seconds")
    end = _finite_optional_number(raw.get("end_seconds"), field="scope.end_seconds")
    if (start is None) != (end is None) or (
        start is not None and end is not None and end <= start
    ):
        raise CreativeWorkflowError("invalid_evidence_time_range")
    if start is not None and candidate_id is None:
        raise CreativeWorkflowError("candidate_required_for_time_scope")
    return {
        "authoring_revision": authoring_revision,
        "candidate_id": candidate_id,
        "start_seconds": start,
        "end_seconds": end,
        "event_ids": _bounded_text_list(
            raw.get("event_ids", []), field="scope.event_ids", maximum_items=128, item_bytes=256
        ),
        "part_ids": _bounded_text_list(
            raw.get("part_ids", []), field="scope.part_ids", maximum_items=64, item_bytes=256
        ),
    }


def _current_iteration(state: dict[str, Any]) -> dict[str, Any]:
    iterations = state.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        raise CreativeWorkflowError("workflow_has_no_active_iteration")
    current = iterations[-1]
    if not isinstance(current, dict) or current.get("closed_at_utc") is not None:
        raise CreativeWorkflowError("workflow_has_no_active_iteration")
    return current


def _candidate_id(iteration: dict[str, Any]) -> str | None:
    candidate = iteration["anchor"].get("candidate")
    return candidate.get("candidate_id") if isinstance(candidate, dict) else None


def _validate_candidate_anchor(value: object) -> None:
    if not isinstance(value, dict):
        raise CreativeWorkflowError("invalid_candidate_anchor")
    expected = {
        "candidate_id",
        "work_id",
        "authoring_revision",
        "candidate_manifest_sha256",
        "render_receipt_sha256",
        "performance_plan_sha256",
        "performance_plan_file_sha256",
        "mix_sha256",
        "post_render_check_sha256",
        "mix_report_sha256",
        "workflow_managed",
        "workflow_authorization",
        "complete_review_artifacts",
        "verified_at_utc",
    }
    if set(value) != expected:
        raise CreativeWorkflowError("invalid_candidate_anchor")
    _portable_identifier(value["candidate_id"], field="candidate_id")
    _portable_identifier(value["work_id"], field="work_id")
    _checked_authoring_revision(value["authoring_revision"])
    for field in (
        "candidate_manifest_sha256",
        "render_receipt_sha256",
        "performance_plan_sha256",
        "performance_plan_file_sha256",
        "mix_sha256",
        "post_render_check_sha256",
    ):
        _checked_revision(value[field], code="invalid_candidate_anchor_hash")
    mix_report = value["mix_report_sha256"]
    if mix_report is not None:
        _checked_revision(mix_report, code="invalid_candidate_anchor_hash")
    if not isinstance(value["workflow_managed"], bool) or not isinstance(
        value["complete_review_artifacts"], bool
    ):
        raise CreativeWorkflowError("invalid_candidate_anchor")
    authorization = value["workflow_authorization"]
    if authorization is not None:
        try:
            normalized = validate_workflow_authorization(
                authorization, allow_none=False
            )
        except (TypeError, ValueError) as exc:
            raise CreativeWorkflowError("invalid_workflow_authorization") from exc
        if normalized != authorization:
            raise CreativeWorkflowError("invalid_workflow_authorization")
    if value["workflow_managed"] is not (authorization is not None):
        raise CreativeWorkflowError("invalid_candidate_management_status")
    # The renderer's mandatory receipt, performance plan, mix and
    # post-render check are the complete baseline review artifact set.
    # A mix report is an optional higher-level collaboration artifact and may
    # not permanently close acceptance for the default/manual workflow.
    if value["complete_review_artifacts"] is not True:
        raise CreativeWorkflowError("invalid_candidate_review_status")
    _canonical_timestamp(value["verified_at_utc"], code="invalid_candidate_verified_at")


def _validate_review(value: object, *, iteration: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "review_id",
        "phase",
        "reviewer",
        "perception_basis",
        "summary",
        "candidate_id",
        "reviewed_at_utc",
    }:
        raise CreativeWorkflowError("invalid_review_record")
    if not isinstance(value["review_id"], str) or re.fullmatch(
        r"review-[0-9a-f]{20}", value["review_id"]
    ) is None:
        raise CreativeWorkflowError("invalid_review_record")
    if value["phase"] not in REVIEW_PHASES or value["reviewer"] not in REVIEWERS:
        raise CreativeWorkflowError("invalid_review_record")
    if value["perception_basis"] not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_review_record")
    if (value["phase"] == "audio_audition") is not (
        value["perception_basis"] == "audio_audition"
    ):
        raise CreativeWorkflowError("review_perception_mismatch")
    if value["reviewer"] in {"engine", "validator"} and value[
        "perception_basis"
    ] != "report_only":
        raise CreativeWorkflowError("machine_cannot_claim_audio_audition")
    candidate_id = _candidate_id(iteration)
    if value["phase"] in {"render_report", "audio_audition"}:
        if candidate_id is None or value["candidate_id"] != candidate_id:
            raise CreativeWorkflowError("review_candidate_mismatch")
    elif value["candidate_id"] is not None and value["candidate_id"] != candidate_id:
        raise CreativeWorkflowError("review_candidate_mismatch")
    _bounded_text(value["summary"], field="review.summary", maximum_bytes=4096)
    _canonical_timestamp(value["reviewed_at_utc"], code="invalid_review_timestamp")


def _validate_evidence(
    value: object,
    *,
    iteration: dict[str, Any],
    active_clause_ids: set[str],
    charter_fields: set[str],
    trusted_hard_failure: bool = False,
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "evidence_id",
        "category",
        "code",
        "basis",
        "reporter",
        "perception_basis",
        "summary",
        "observation",
        "interpretation",
        "confidence",
        "scope",
        "blocking",
        "automatic_change",
        "recorded_at_utc",
    }:
        raise CreativeWorkflowError("invalid_evidence_record")
    if not isinstance(value["evidence_id"], str) or re.fullmatch(
        r"evidence-[0-9a-f]{20}", value["evidence_id"]
    ) is None:
        raise CreativeWorkflowError("invalid_evidence_record")
    category = value["category"]
    if category not in EVIDENCE_CATEGORIES:
        raise CreativeWorkflowError("invalid_evidence_category")
    if category == "hard_failure" and not trusted_hard_failure:
        raise CreativeWorkflowError("hard_failure_requires_trusted_boundary")
    if not isinstance(value["code"], str) or _CODE.fullmatch(value["code"]) is None:
        raise CreativeWorkflowError("invalid_evidence_code")
    basis = value["basis"]
    if not isinstance(basis, dict) or set(basis) != {
        "kind",
        "reference",
        "artifact_sha256",
        "artifact_role",
    }:
        raise CreativeWorkflowError("invalid_evidence_basis")
    allowed_basis = {
        "engine_contract",
        "declared_promise",
        "active_clause",
        "diagnostic_hypothesis",
        "render_measurement",
        "audio_audition",
    }
    if basis["kind"] not in allowed_basis:
        raise CreativeWorkflowError("invalid_evidence_basis")
    _bounded_text(basis["reference"], field="evidence.basis.reference", maximum_bytes=1024)
    artifact_hash = basis["artifact_sha256"]
    artifact_role = basis["artifact_role"]
    if (artifact_hash is None) is not (artifact_role is None):
        raise CreativeWorkflowError("evidence_artifact_role_mismatch")
    if artifact_hash is not None:
        if artifact_role not in _EVIDENCE_ARTIFACT_FIELDS:
            raise CreativeWorkflowError("invalid_evidence_artifact_role")
        _checked_revision(basis["artifact_sha256"], code="invalid_evidence_artifact_hash")
        candidate = iteration["anchor"].get("candidate")
        if not isinstance(candidate, dict):
            raise CreativeWorkflowError("evidence_artifact_requires_candidate")
        expected_hash = candidate[_EVIDENCE_ARTIFACT_FIELDS[artifact_role]]
        if expected_hash is None or artifact_hash != expected_hash:
            raise CreativeWorkflowError("evidence_artifact_role_binding_mismatch")
    reporter = value["reporter"]
    perception = value["perception_basis"]
    if reporter not in REVIEWERS or perception not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_evidence_reporter")
    if reporter in {"engine", "validator"} and perception != "report_only":
        raise CreativeWorkflowError("machine_cannot_claim_audio_audition")
    if basis["kind"] == "audio_audition" and perception != "audio_audition":
        raise CreativeWorkflowError("evidence_perception_mismatch")
    if basis["kind"] != "audio_audition" and perception != "report_only":
        raise CreativeWorkflowError("evidence_perception_mismatch")
    if category == "hard_failure":
        if basis["kind"] != "engine_contract" or reporter not in {"engine", "validator"}:
            raise CreativeWorkflowError("hard_failure_requires_engine_contract")
    elif category == "promise_conflict":
        if basis["kind"] not in {"declared_promise", "active_clause"}:
            raise CreativeWorkflowError("promise_conflict_requires_declared_basis")
    elif basis["kind"] == "engine_contract":
        raise CreativeWorkflowError("aesthetic_risk_cannot_claim_hard_contract")
    if basis["kind"] == "render_measurement":
        if (
            iteration["anchor"].get("candidate") is None
            or artifact_hash is None
            or artifact_role not in _MEASUREMENT_ARTIFACT_ROLES
        ):
            raise CreativeWorkflowError("render_measurement_requires_bound_report")
    if basis["kind"] == "active_clause" and basis["reference"] not in active_clause_ids:
        raise CreativeWorkflowError("evidence_clause_not_active")
    if basis["kind"] == "declared_promise" and basis["reference"] not in charter_fields:
        raise CreativeWorkflowError("evidence_promise_reference_invalid")
    if value["blocking"] is not (category == "hard_failure"):
        raise CreativeWorkflowError("evidence_blocking_policy_violation")
    if value["automatic_change"] is not False:
        raise CreativeWorkflowError("automatic_aesthetic_change_forbidden")
    for field in ("summary", "observation", "interpretation"):
        _bounded_text(value[field], field=f"evidence.{field}", maximum_bytes=4096)
    if value["confidence"] not in {"low", "medium", "high"}:
        raise CreativeWorkflowError("invalid_evidence_confidence")
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != {
        "authoring_revision",
        "candidate_id",
        "start_seconds",
        "end_seconds",
        "event_ids",
        "part_ids",
    }:
        raise CreativeWorkflowError("invalid_evidence_scope")
    if scope["authoring_revision"] != iteration["anchor"]["authoring_revision"]:
        raise CreativeWorkflowError("evidence_revision_mismatch")
    if scope["candidate_id"] not in {None, _candidate_id(iteration)}:
        raise CreativeWorkflowError("evidence_candidate_mismatch")
    if basis["kind"] == "audio_audition" and (
        _candidate_id(iteration) is None
        or scope["candidate_id"] != _candidate_id(iteration)
    ):
        raise CreativeWorkflowError("audio_audition_requires_current_candidate")
    _normalize_scope(
        {
            "start_seconds": scope["start_seconds"],
            "end_seconds": scope["end_seconds"],
            "event_ids": scope["event_ids"],
            "part_ids": scope["part_ids"],
        },
        authoring_revision=scope["authoring_revision"],
        candidate_id=scope["candidate_id"],
    )
    _canonical_timestamp(value["recorded_at_utc"], code="invalid_evidence_timestamp")


def _validate_exception(
    value: object,
    *,
    evidence_by_id: Mapping[str, dict[str, Any]],
    active_clause_ids: set[str],
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "exception_id",
        "target_type",
        "target_ref",
        "purpose",
        "scope",
        "higher_value",
        "cost",
        "recovery",
        "evidence_ids",
        "reusable",
        "registered_at_utc",
    }:
        raise CreativeWorkflowError("invalid_exception_record")
    if not isinstance(value["exception_id"], str) or re.fullmatch(
        r"exception-[0-9a-f]{20}", value["exception_id"]
    ) is None:
        raise CreativeWorkflowError("invalid_exception_record")
    if value["target_type"] not in {"active_clause", "work_charter"}:
        raise CreativeWorkflowError("invalid_exception_target")
    target_ref = _bounded_text(value["target_ref"], field="exception.target_ref", maximum_bytes=256)
    if value["target_type"] == "active_clause" and target_ref not in active_clause_ids:
        raise CreativeWorkflowError("exception_clause_not_active")
    if value["target_type"] == "work_charter" and target_ref not in {
        "one_sentence_promise",
        "identity_kernel",
        "ending_contract",
        "prohibited_shortcuts",
        "primary_sovereignty",
    }:
        raise CreativeWorkflowError("invalid_exception_target")
    for field in ("purpose", "scope", "higher_value", "cost", "recovery"):
        _bounded_text(value[field], field=f"exception.{field}", maximum_bytes=4096)
    ids = value["evidence_ids"]
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise CreativeWorkflowError("exception_evidence_required")
    for evidence_id in ids:
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise CreativeWorkflowError("exception_evidence_not_found")
        if evidence["category"] == "hard_failure":
            raise CreativeWorkflowError("hard_failure_cannot_be_excepted")
    if not isinstance(value["reusable"], bool):
        raise CreativeWorkflowError("invalid_exception_record")
    _canonical_timestamp(value["registered_at_utc"], code="invalid_exception_timestamp")


def _validate_candidate_locator(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or set(value) != {
        "work_id",
        "candidate_id",
        "manifest_sha256",
    }:
        raise CreativeWorkflowError("invalid_candidate_locator")
    _portable_identifier(value["work_id"], field="candidate_locator.work_id")
    _portable_identifier(
        value["candidate_id"], field="candidate_locator.candidate_id"
    )
    _checked_revision(
        value["manifest_sha256"], code="invalid_candidate_locator_hash"
    )


def _anchor_locator(candidate: Mapping[str, Any] | None) -> dict[str, str] | None:
    if candidate is None:
        return None
    locator = {
        "work_id": str(candidate["work_id"]),
        "candidate_id": str(candidate["candidate_id"]),
        "manifest_sha256": str(candidate["candidate_manifest_sha256"]),
    }
    _validate_candidate_locator(locator)
    return locator


def _validate_render_attempt(value: object, *, iteration: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "attempt_number",
        "operation_id",
        "expected_work_id",
        "expected_candidate_id",
        "reservation_revision",
        "authoring_revision",
        "parent_candidate",
        "status",
        "requested_at_utc",
        "finished_at_utc",
    }:
        raise CreativeWorkflowError("invalid_render_attempt")
    if (
        isinstance(value["attempt_number"], bool)
        or not isinstance(value["attempt_number"], int)
        or not 1 <= value["attempt_number"] <= MAX_RENDER_ATTEMPTS_PER_ITERATION
    ):
        raise CreativeWorkflowError("invalid_render_attempt")
    _require_hex(value["operation_id"], _WORKFLOW_ID, "invalid_operation_id")
    expected_candidate = portable_slug(
        f"workflow-{value['operation_id']}", maximum_length=96
    )
    if value["expected_candidate_id"] != expected_candidate:
        raise CreativeWorkflowError("render_candidate_identity_mismatch")
    _portable_identifier(value["expected_work_id"], field="expected_work_id")
    # The reservation revision is filled only after the CAS publication.  A
    # pending state stores null; snapshots project the owning revision to the
    # renderer and candidate authorization.  Historical attempts are sealed
    # with the actual reservation revision when their result is recorded.
    reservation = value["reservation_revision"]
    if reservation is not None:
        _checked_revision(reservation, code="invalid_render_reservation_revision")
    if (value["status"] == "pending") is not (reservation is None):
        raise CreativeWorkflowError("invalid_render_reservation_revision")
    if value["authoring_revision"] != iteration["anchor"]["authoring_revision"]:
        raise CreativeWorkflowError("render_revision_mismatch")
    _validate_candidate_locator(value["parent_candidate"])
    if value["parent_candidate"] != iteration["anchor"]["parent_candidate"]:
        raise CreativeWorkflowError("render_parent_mismatch")
    if value["status"] not in {"pending", "completed", "cancelled"}:
        raise CreativeWorkflowError("invalid_render_attempt")
    if (value["status"] == "pending") is not (value["finished_at_utc"] is None):
        raise CreativeWorkflowError("invalid_render_attempt")
    _canonical_timestamp(value["requested_at_utc"], code="invalid_render_timestamp")
    if value["finished_at_utc"] is not None:
        _canonical_timestamp(value["finished_at_utc"], code="invalid_render_timestamp")


def _validate_decision(value: object, *, iteration: dict[str, Any]) -> None:
    if not isinstance(value, dict) or set(value) != {
        "disposition",
        "summary",
        "rationale",
        "protected_values",
        "sacrificed_values",
        "evidence_ids",
        "exception_ids",
        "expected_audible_change",
        "final_authority",
        "perception_basis",
        "claim_scope",
        "decided_at_utc",
    }:
        raise CreativeWorkflowError("invalid_iteration_decision")
    if value["disposition"] not in {
        "accept",
        "revise",
        "recommend_revision",
        "preserve",
        "stop",
        "rollback",
    }:
        raise CreativeWorkflowError("invalid_iteration_decision")
    for field in ("summary", "rationale"):
        _bounded_text(value[field], field=f"decision.{field}", maximum_bytes=4096)
    _bounded_text_list(value["protected_values"], field="decision.protected_values", maximum_items=32)
    _bounded_text_list(value["sacrificed_values"], field="decision.sacrificed_values", maximum_items=32)
    evidence_ids = {item["evidence_id"] for item in iteration["evidence"]}
    exception_ids = {item["exception_id"] for item in iteration["exceptions"]}
    if not isinstance(value["evidence_ids"], list) or not set(value["evidence_ids"]).issubset(evidence_ids):
        raise CreativeWorkflowError("decision_evidence_not_found")
    if not isinstance(value["exception_ids"], list) or not set(value["exception_ids"]).issubset(exception_ids):
        raise CreativeWorkflowError("decision_exception_not_found")
    expected_change = value["expected_audible_change"]
    if expected_change is not None:
        _bounded_text(expected_change, field="decision.expected_audible_change", maximum_bytes=4096)
    if value["disposition"] == "revise" and expected_change is None:
        raise CreativeWorkflowError("revision_hypothesis_required")
    if value["final_authority"] not in FINAL_AUTHORITIES or value[
        "perception_basis"
    ] not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_decision_authority")
    if value["claim_scope"] != "contextual_workflow_decision_not_objective_quality":
        raise CreativeWorkflowError("invalid_decision_claim_scope")
    _canonical_timestamp(value["decided_at_utc"], code="invalid_decision_timestamp")


def _validate_termination(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "reason",
        "summary",
        "final_authority",
        "perception_basis",
        "selected_candidate",
        "terminated_at_utc",
    }:
        raise CreativeWorkflowError("invalid_workflow_termination")
    if value["reason"] not in {
        "mode_off",
        "accepted_under_charter",
        "revision_recommended",
        "preserved_without_acceptance",
        "creator_stopped",
        "agent_stopped",
        "budget_exhausted",
        "no_material_improvement",
        "human_review_required",
        "external_blocker",
        "cancelled",
    }:
        raise CreativeWorkflowError("invalid_workflow_termination")
    _bounded_text(value["summary"], field="termination.summary", maximum_bytes=4096)
    if value["final_authority"] not in FINAL_AUTHORITIES or value[
        "perception_basis"
    ] not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_workflow_termination")
    selected = value["selected_candidate"]
    if selected is not None:
        _validate_candidate_anchor(selected)
    _canonical_timestamp(value["terminated_at_utc"], code="invalid_termination_timestamp")


def _validate_iteration(
    value: object,
    *,
    expected_number: int,
    active_clause_ids: set[str],
    charter_fields: set[str],
) -> None:
    if not isinstance(value, dict) or set(value) != {
        "iteration_number",
        "iteration_id",
        "status",
        "opened_at_utc",
        "closed_at_utc",
        "anchor",
        "reviews",
        "evidence",
        "exceptions",
        "render_attempts",
        "decision",
        "outcome",
        "next_authoring_revision",
    }:
        raise CreativeWorkflowError("invalid_iteration_record")
    if value["iteration_number"] != expected_number or value["iteration_id"] != f"iteration-{expected_number:04d}":
        raise CreativeWorkflowError("invalid_iteration_identity")
    if value["status"] not in {"reviewing", "candidate_pending", "revision_pending", "closed"}:
        raise CreativeWorkflowError("invalid_iteration_status")
    _canonical_timestamp(value["opened_at_utc"], code="invalid_iteration_timestamp")
    if value["closed_at_utc"] is not None:
        _canonical_timestamp(value["closed_at_utc"], code="invalid_iteration_timestamp")
    if (value["status"] == "closed") is not (value["closed_at_utc"] is not None):
        raise CreativeWorkflowError("invalid_iteration_status")
    anchor = value["anchor"]
    if not isinstance(anchor, dict) or set(anchor) != {
        "authoring_revision",
        "parent_candidate",
        "candidate",
    }:
        raise CreativeWorkflowError("invalid_iteration_anchor")
    _checked_authoring_revision(anchor["authoring_revision"])
    _validate_candidate_locator(anchor["parent_candidate"])
    if anchor["candidate"] is not None:
        _validate_candidate_anchor(anchor["candidate"])
        if anchor["candidate"]["authoring_revision"] != anchor["authoring_revision"]:
            raise CreativeWorkflowError("candidate_revision_mismatch")
    if not isinstance(value["reviews"], list) or len(value["reviews"]) > MAX_REVIEWS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_reviews")
    for review in value["reviews"]:
        _validate_review(review, iteration=value)
    if len({item["review_id"] for item in value["reviews"]}) != len(value["reviews"]):
        raise CreativeWorkflowError("duplicate_review_record")
    if not isinstance(value["evidence"], list) or len(value["evidence"]) > MAX_EVIDENCE_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_evidence_items")
    for evidence in value["evidence"]:
        _validate_evidence(
            evidence,
            iteration=value,
            active_clause_ids=active_clause_ids,
            charter_fields=charter_fields,
            trusted_hard_failure=True,
        )
    if len({item["evidence_id"] for item in value["evidence"]}) != len(value["evidence"]):
        raise CreativeWorkflowError("duplicate_evidence_record")
    evidence_by_id = {item["evidence_id"]: item for item in value["evidence"]}
    if not isinstance(value["exceptions"], list) or len(value["exceptions"]) > MAX_EXCEPTIONS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_exceptions")
    for exception in value["exceptions"]:
        _validate_exception(
            exception,
            evidence_by_id=evidence_by_id,
            active_clause_ids=active_clause_ids,
        )
    if len({item["exception_id"] for item in value["exceptions"]}) != len(value["exceptions"]):
        raise CreativeWorkflowError("duplicate_exception_record")
    if not isinstance(value["render_attempts"], list) or len(value["render_attempts"]) > MAX_RENDER_ATTEMPTS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_render_attempts")
    for index, attempt in enumerate(value["render_attempts"], start=1):
        _validate_render_attempt(attempt, iteration=value)
        if attempt["attempt_number"] != index:
            raise CreativeWorkflowError("invalid_render_attempt_identity")
    pending = [item for item in value["render_attempts"] if item["status"] == "pending"]
    if len(pending) > 1 or (value["status"] == "candidate_pending") is not bool(pending):
        raise CreativeWorkflowError("invalid_pending_render_state")
    if value["decision"] is not None:
        _validate_decision(value["decision"], iteration=value)
        decision = value["decision"]
        if (
            decision["disposition"] != "stop"
            or decision["perception_basis"] == "audio_audition"
        ) and not _authority_has_basis(
            value,
            authority=decision["final_authority"],
            perception_basis=decision["perception_basis"],
        ):
            raise CreativeWorkflowError("decision_perception_basis_unproven")
    if value["status"] == "revision_pending" and (
        not isinstance(value["decision"], dict) or value["decision"]["disposition"] != "revise"
    ):
        raise CreativeWorkflowError("revision_pending_without_decision")
    if value["outcome"] not in {
        None,
        "accepted",
        "revision_recommended",
        "preserved",
        "stopped",
        "revised",
        "rolled_back",
    }:
        raise CreativeWorkflowError("invalid_iteration_outcome")
    if (value["status"] == "closed") is not (value["outcome"] is not None):
        raise CreativeWorkflowError("invalid_iteration_outcome")
    next_revision = value["next_authoring_revision"]
    if next_revision is not None:
        _checked_authoring_revision(next_revision)
    if (value["outcome"] == "revised") is not (next_revision is not None):
        raise CreativeWorkflowError("invalid_iteration_revision_outcome")


def _validate_state_document(state: dict[str, Any]) -> None:
    expected = {
        "kind",
        "schema_version",
        "workflow_id",
        "project_id",
        "mode",
        "status",
        "sequence",
        "parent_revision",
        "final_authority",
        "created_at_utc",
        "updated_at_utc",
        "initial_anchor",
        "budget",
        "usage",
        "constitution",
        "work_charter",
        "active_clauses",
        "iterations",
        "termination",
        "policy",
    }
    if set(state) != expected or state.get("kind") != WORKFLOW_STATE_KIND or state.get(
        "schema_version"
    ) != WORKFLOW_VERSION:
        raise CreativeWorkflowError("invalid_workflow_state")
    _checked_workflow_id(state["workflow_id"])
    _require_hex(state["project_id"], _WORKFLOW_ID, "invalid_project_id")
    mode = state["mode"]
    status = state["status"]
    if mode not in WORKFLOW_MODES or status not in WORKFLOW_STATUSES:
        raise CreativeWorkflowError("invalid_workflow_state")
    sequence = state["sequence"]
    parent_revision = state["parent_revision"]
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= MAX_WORKFLOW_HISTORY
        or (sequence == 1) is not (parent_revision is None)
    ):
        raise CreativeWorkflowError("invalid_workflow_lineage")
    if parent_revision is not None:
        _checked_revision(parent_revision, code="invalid_workflow_lineage")
    if state["final_authority"] not in FINAL_AUTHORITIES:
        raise CreativeWorkflowError("invalid_final_authority")
    created = _canonical_timestamp(state["created_at_utc"], code="invalid_workflow_timestamp")
    updated = _canonical_timestamp(state["updated_at_utc"], code="invalid_workflow_timestamp")
    if updated < created:
        raise CreativeWorkflowError("invalid_workflow_timestamp")
    initial = state["initial_anchor"]
    if not isinstance(initial, dict) or initial != _empty_anchor(
        _checked_authoring_revision(initial.get("authoring_revision") if isinstance(initial, dict) else None)
    ):
        raise CreativeWorkflowError("invalid_initial_anchor")
    budget = _normalize_budget(mode, state["budget"])
    if budget != state["budget"]:
        raise CreativeWorkflowError("invalid_budget")
    constitution = _normalize_constitution(state["constitution"])
    if constitution != state["constitution"]:
        raise CreativeWorkflowError("invalid_constitution_binding")
    clauses = _normalize_active_clauses(state["active_clauses"])
    if clauses != state["active_clauses"]:
        raise CreativeWorkflowError("invalid_active_clauses")
    if (constitution is None) != (not clauses):
        raise CreativeWorkflowError("constitution_clause_binding_mismatch")
    charter = state["work_charter"]
    if charter is not None and _normalize_work_charter(charter) != charter:
        raise CreativeWorkflowError("invalid_work_charter")
    iterations = state["iterations"]
    if not isinstance(iterations, list) or len(iterations) > MAX_ITERATIONS:
        raise CreativeWorkflowError("invalid_workflow_iterations")
    active_ids = {item["clause_id"] for item in clauses}
    charter_fields = set(charter) if isinstance(charter, dict) else set()
    for index, iteration in enumerate(iterations, start=1):
        _validate_iteration(
            iteration,
            expected_number=index,
            active_clause_ids=active_ids,
            charter_fields=charter_fields,
        )
        if (
            isinstance(iteration["decision"], dict)
            and iteration["decision"]["final_authority"]
            != state["final_authority"]
        ):
            raise CreativeWorkflowError("decision_authority_mismatch")
    unactivated_stopped = (
        status == "stopped" and not iterations and charter is None
    )
    if iterations:
        for iteration in iterations[:-1]:
            if iteration["status"] != "closed":
                raise CreativeWorkflowError("historical_iteration_not_closed")
        last = iterations[-1]
        expected_top_status = (
            status if status in TERMINAL_WORKFLOW_STATUSES else last["status"]
        )
        if status not in TERMINAL_WORKFLOW_STATUSES and expected_top_status != status:
            raise CreativeWorkflowError("workflow_iteration_status_mismatch")
        if status in TERMINAL_WORKFLOW_STATUSES and last["status"] != "closed":
            raise CreativeWorkflowError("terminal_iteration_not_closed")
    elif status not in {"disabled", "charter_pending"} and not unactivated_stopped:
        raise CreativeWorkflowError("workflow_iteration_missing")
    if mode == "off" and (status != "disabled" or charter is not None or iterations):
        raise CreativeWorkflowError("off_mode_state_conflict")
    if mode != "off" and status == "disabled":
        raise CreativeWorkflowError("off_mode_state_conflict")
    if status == "charter_pending" and (charter is not None or iterations):
        raise CreativeWorkflowError("charter_pending_state_conflict")
    if (
        status not in {"charter_pending", "disabled"}
        and charter is None
        and not unactivated_stopped
    ):
        raise CreativeWorkflowError("work_charter_missing")
    terminal = status in TERMINAL_WORKFLOW_STATUSES
    if terminal is not (state["termination"] is not None):
        raise CreativeWorkflowError("workflow_termination_state_mismatch")
    if state["termination"] is not None:
        _validate_termination(state["termination"])
        if state["termination"]["final_authority"] != state["final_authority"]:
            raise CreativeWorkflowError("termination_authority_mismatch")
        if state["termination"]["perception_basis"] == "audio_audition" and (
            not iterations
            or not _authority_has_basis(
                iterations[-1],
                authority=state["termination"]["final_authority"],
                perception_basis="audio_audition",
            )
        ):
            raise CreativeWorkflowError("decision_perception_basis_unproven")
    usage = state["usage"]
    if not isinstance(usage, dict) or set(usage) != {
        "revision_cycles",
        "rollbacks",
        "render_attempts",
        "evidence_items",
        "exceptions",
        "reviews",
    }:
        raise CreativeWorkflowError("invalid_workflow_usage")
    computed = {
        "revision_cycles": sum(item["outcome"] == "revised" for item in iterations),
        "rollbacks": sum(item["outcome"] == "rolled_back" for item in iterations),
        "render_attempts": sum(len(item["render_attempts"]) for item in iterations),
        "evidence_items": sum(len(item["evidence"]) for item in iterations),
        "exceptions": sum(len(item["exceptions"]) for item in iterations),
        "reviews": sum(len(item["reviews"]) for item in iterations),
    }
    if usage != computed:
        raise CreativeWorkflowError("workflow_usage_mismatch")
    if computed["revision_cycles"] > budget["max_revision_cycles"] or computed[
        "rollbacks"
    ] > budget["max_rollbacks"]:
        raise CreativeWorkflowError("workflow_budget_exceeded")
    if state["policy"] != _POLICY:
        raise CreativeWorkflowError("workflow_policy_mismatch")


def _allowed_actions(state: dict[str, Any]) -> list[str]:
    status = state["status"]
    if status in TERMINAL_WORKFLOW_STATUSES:
        return []
    if status == "charter_pending":
        return ["activate", "terminate"]
    if status == "candidate_pending":
        actions = ["record_candidate", "cancel_render", "terminate"]
        if any(
            isinstance(item["anchor"].get("candidate"), dict)
            for item in state["iterations"][:-1]
        ):
            actions.append("rollback")
        return actions
    if status == "revision_pending":
        return ["record_authoring_revision", "terminate"]
    if status == "reviewing":
        current = state["iterations"][-1]
        actions = ["record_review", "record_evidence", "register_exception", "decide", "terminate"]
        if current["anchor"]["candidate"] is None:
            actions.extend(["request_render", "attach_existing_candidate_for_audit"])
        if any(
            isinstance(item["anchor"].get("candidate"), dict)
            for item in state["iterations"][:-1]
        ):
            actions.append("rollback")
        return actions
    return []


def _snapshot_from_layout(
    layout: _WorkflowLayout, *, revision: str | None = None
) -> CreativeWorkflowSnapshot:
    manifest = _read_manifest(layout)
    selected = (
        manifest["current_revision"]
        if revision is None
        else _checked_revision(revision)
    )
    state = _validate_revision_directory(
        _revision_path(layout, selected),
        workflow_id=manifest["workflow_id"],
        project_id=manifest["project_id"],
        revision=selected,
    )
    if state["created_at_utc"] != manifest["created_at_utc"]:
        raise CreativeWorkflowError("workflow_timestamp_mismatch")
    if revision is None and state["updated_at_utc"] != manifest["updated_at_utc"]:
        raise CreativeWorkflowError("workflow_timestamp_mismatch")
    if revision is None and state["sequence"] != manifest["current_sequence"]:
        raise CreativeWorkflowError("workflow_lineage_pointer_mismatch")
    return CreativeWorkflowSnapshot(
        workflow_id=manifest["workflow_id"],
        project_id=manifest["project_id"],
        revision=selected,
        created_at_utc=state["created_at_utc"],
        updated_at_utc=state["updated_at_utc"],
        state=state,
    )


def open_creative_workflow(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    revision: str | None = None,
) -> CreativeWorkflowSnapshot:
    """Open and fully verify one current or historical workflow revision."""

    layout = _existing_layout(project_root, workflow_id)
    return _snapshot_from_layout(layout, revision=revision)


def create_creative_workflow(
    project_root: str | os.PathLike[str],
    *,
    mode: str,
    final_authority: str,
    base_authoring_revision: str | None = None,
    budget: Mapping[str, Any] | None = None,
) -> CreativeWorkflowSnapshot:
    """Create a new optional workflow bound to an immutable authoring revision."""

    if mode not in WORKFLOW_MODES:
        raise CreativeWorkflowError("invalid_workflow_mode")
    if final_authority not in FINAL_AUTHORITIES:
        raise CreativeWorkflowError("invalid_final_authority")
    root, project_id, _workflows = _project_workflows_layout(project_root, create=True)
    try:
        authoring = open_authoring_project(root, revision=base_authoring_revision)
    except AuthoringProjectError as exc:
        raise CreativeWorkflowError("authoring_revision_unavailable") from exc
    if authoring.project_id != project_id:
        raise CreativeWorkflowError("authoring_project_identity_changed")
    checked_budget = _normalize_budget(mode, budget)
    layout: _WorkflowLayout | None = None
    for _ in range(8):
        workflow_id = secrets.token_hex(16)
        try:
            layout = _create_layout(root, workflow_id)
            break
        except CreativeWorkflowError as exc:
            if exc.code != "workflow_id_collision":
                raise
    if layout is None:
        raise CreativeWorkflowError("workflow_id_generation_failed")
    timestamp = _now()
    try:
        state = _initial_state(
            workflow_id=workflow_id,
            project_id=project_id,
            mode=mode,
            authoring_revision=authoring.revision,
            budget=checked_budget,
            final_authority=final_authority,
            timestamp=timestamp,
        )
        _validate_state_document(state)
        with acquire_render_lock(
            layout.workflow.path, parent_identity=layout.workflows
        ):
            revision = _publish_revision(layout, state)
            manifest = _manifest_document(
                workflow_id=workflow_id,
                project_id=project_id,
                created_at_utc=timestamp,
                updated_at_utc=timestamp,
                revision=revision,
                sequence=state["sequence"],
            )
            _replace_manifest(layout, manifest)
        return _snapshot_from_layout(layout)
    except RenderLockError as exc:
        _cleanup_failed_layout(layout)
        raise CreativeWorkflowError("workflow_busy") from exc
    except Exception:
        _cleanup_failed_layout(layout)
        raise


def _refresh_usage(state: dict[str, Any]) -> None:
    iterations = state["iterations"]
    state["usage"] = {
        "revision_cycles": sum(item["outcome"] == "revised" for item in iterations),
        "rollbacks": sum(item["outcome"] == "rolled_back" for item in iterations),
        "render_attempts": sum(len(item["render_attempts"]) for item in iterations),
        "evidence_items": sum(len(item["evidence"]) for item in iterations),
        "exceptions": sum(len(item["exceptions"]) for item in iterations),
        "reviews": sum(len(item["reviews"]) for item in iterations),
    }


def _transition(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    mutate: Callable[[dict[str, Any], _WorkflowLayout, str], None],
    allow_reserved_termination_transition: bool = False,
) -> CreativeWorkflowSnapshot:
    layout = _existing_layout(project_root, workflow_id)
    expected = _checked_revision(expected_revision, code="invalid_expected_workflow_revision")
    try:
        with acquire_render_lock(
            layout.workflow.path, parent_identity=layout.workflows
        ):
            current = _snapshot_from_layout(layout)
            if current.revision != expected:
                raise CreativeWorkflowError("workflow_revision_conflict")
            state = current.detached_state()
            mutate(state, layout, expected)
            transition_ceiling = MAX_WORKFLOW_HISTORY - (
                0 if allow_reserved_termination_transition else 1
            )
            if state["sequence"] >= transition_ceiling:
                raise CreativeWorkflowError("workflow_history_limit_exceeded")
            state["parent_revision"] = current.revision
            state["sequence"] = state["sequence"] + 1
            observed = _now()
            try:
                validate_canonical_utc_timestamp(observed)
            except ValueError as exc:
                raise CreativeWorkflowError("invalid_system_timestamp") from exc
            state["updated_at_utc"] = max(
                observed,
                state["created_at_utc"],
                current.updated_at_utc,
            )
            _refresh_usage(state)
            _validate_state_document(state)
            revision = _publish_revision(layout, state)
            if revision == current.revision:
                raise CreativeWorkflowError("workflow_transition_no_change")
            manifest = _manifest_document(
                workflow_id=state["workflow_id"],
                project_id=state["project_id"],
                created_at_utc=state["created_at_utc"],
                updated_at_utc=state["updated_at_utc"],
                revision=revision,
                sequence=state["sequence"],
            )
            _replace_manifest(layout, manifest)
        return _snapshot_from_layout(layout)
    except RenderLockError as exc:
        raise CreativeWorkflowError("workflow_busy") from exc


def _require_status(state: dict[str, Any], *statuses: str) -> None:
    if state["status"] not in statuses:
        raise CreativeWorkflowError("illegal_workflow_transition")


def activate_creative_workflow(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    work_charter: Mapping[str, Any],
    constitution: Mapping[str, Any] | None = None,
    active_clauses: Sequence[Mapping[str, Any]] = (),
) -> CreativeWorkflowSnapshot:
    charter = _normalize_work_charter(work_charter)
    constitution_binding = _normalize_constitution(constitution)
    clauses = _normalize_active_clauses(active_clauses)
    if (constitution_binding is None) != (not clauses):
        raise CreativeWorkflowError("constitution_clause_binding_mismatch")

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "charter_pending")
        if state["mode"] == "off":
            raise CreativeWorkflowError("workflow_disabled")
        state["constitution"] = copy.deepcopy(constitution_binding)
        state["work_charter"] = copy.deepcopy(charter)
        state["active_clauses"] = copy.deepcopy(clauses)
        timestamp = max(_now(), state["created_at_utc"])
        state["iterations"] = [
            _new_iteration(
                1,
                authoring_revision=state["initial_anchor"]["authoring_revision"],
                parent_candidate=None,
                opened_at_utc=timestamp,
            )
        ]
        state["status"] = "reviewing"

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def _plain_bound_artifact(
    directory: Path,
    relative_path: object,
    *,
    expected_sha256: object,
    label: str,
) -> Path:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
    ):
        raise CreativeWorkflowError("invalid_candidate_artifact", source=label)
    portable = PurePosixPath(relative_path)
    if portable.is_absolute() or any(part in {"", ".", ".."} for part in portable.parts):
        raise CreativeWorkflowError("candidate_artifact_path_escape", source=label)
    lexical = directory.joinpath(*portable.parts)
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(directory)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("candidate_artifact_path_escape", source=label) from exc
    if resolved != lexical.absolute():
        raise CreativeWorkflowError("unsafe_candidate_artifact", source=label)
    expected = _checked_revision(expected_sha256, code="invalid_candidate_artifact_hash")
    try:
        identity, observed = sha256_plain_file(lexical)
        revalidate_plain_file(identity)
    except OSError as exc:
        raise CreativeWorkflowError(
            "unsafe_candidate_artifact", source=label
        ) from exc
    if identity.size < 1 or observed != expected:
        raise CreativeWorkflowError("candidate_artifact_hash_mismatch", source=label)
    return lexical


def _read_candidate_json(
    path: Path,
    *,
    label: str,
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    try:
        identity, payload = read_plain_file_bytes(
            path, maximum_bytes=MAX_WORKFLOW_DOCUMENT_BYTES
        )
        observed_sha256 = hashlib.sha256(payload).hexdigest()
        if expected_sha256 is not None and observed_sha256 != expected_sha256:
            raise OSError("candidate JSON hash changed")
        document = strict_json_loads(payload, limits=_WORKFLOW_LIMITS)
        revalidate_plain_file(identity)
    except (OSError, AuthoringJsonError) as exc:
        raise CreativeWorkflowError("invalid_candidate_json", source=label) from exc
    assert isinstance(document, dict)
    return document, observed_sha256


def _verified_candidate_anchor(
    candidate_path: str | os.PathLike[str],
    *,
    project_id: str,
    authoring_revision: str,
    expected_authorization: dict[str, Any] | None,
) -> dict[str, Any]:
    requested = Path(candidate_path)
    if not requested.is_absolute():
        raise CreativeWorkflowError("candidate_path_must_be_absolute")
    try:
        identity = capture_plain_directory(requested)
        directory = revalidate_plain_directory(identity)
        loaded_directory, candidate = load_candidate(directory, verify=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("candidate_verification_failed") from exc
    if loaded_directory != directory:
        raise CreativeWorkflowError("candidate_path_identity_mismatch")
    manifest_path = directory / CANDIDATE_MANIFEST_NAME
    manifest_document, manifest_sha256 = _read_candidate_json(
        manifest_path,
        label="candidate_manifest",
    )
    if manifest_document != candidate:
        raise CreativeWorkflowError("candidate_manifest_changed_during_verification")
    candidate = manifest_document
    authoring = candidate.get("authoring_project")
    if (
        not isinstance(authoring, dict)
        or authoring.get("project_id") != project_id
        or authoring.get("revision") != authoring_revision
    ):
        raise CreativeWorkflowError("candidate_authoring_binding_mismatch")
    receipt_binding = candidate.get("render_receipt")
    project = candidate.get("project")
    if not isinstance(receipt_binding, dict) or not isinstance(project, dict):
        raise CreativeWorkflowError("candidate_binding_incomplete")
    receipt_path = _plain_bound_artifact(
        directory,
        receipt_binding.get("path"),
        expected_sha256=receipt_binding.get("sha256"),
        label="render_receipt",
    )
    receipt, _receipt_sha256 = _read_candidate_json(
        receipt_path,
        label="render_receipt",
        expected_sha256=receipt_binding.get("sha256"),
    )
    if receipt.get("authoring_project", {}).get("project_id") != project_id or receipt.get(
        "authoring_project", {}
    ).get("revision") != authoring_revision:
        raise CreativeWorkflowError("candidate_authoring_binding_mismatch")
    plan_binding = receipt.get("performance_plan")
    if not isinstance(plan_binding, dict):
        raise CreativeWorkflowError("candidate_binding_incomplete")
    _plain_bound_artifact(
        directory,
        plan_binding.get("path"),
        expected_sha256=plan_binding.get("file_sha256"),
        label="performance_plan",
    )
    plan_sha256 = _checked_revision(
        plan_binding.get("sha256"), code="candidate_binding_incomplete"
    )
    if project.get("performance_plan_sha256") != plan_sha256:
        raise CreativeWorkflowError("candidate_plan_binding_mismatch")
    mix_binding = receipt.get("mix")
    post_binding = receipt.get("post_render_check")
    if not isinstance(mix_binding, dict) or not isinstance(post_binding, dict):
        raise CreativeWorkflowError("candidate_review_artifacts_incomplete")
    _plain_bound_artifact(
        directory,
        mix_binding.get("path"),
        expected_sha256=mix_binding.get("sha256"),
        label="mix",
    )
    _plain_bound_artifact(
        directory,
        post_binding.get("path"),
        expected_sha256=post_binding.get("sha256"),
        label="post_render_check",
    )
    mix_report_binding = receipt.get("mix_report")
    mix_report_sha256: str | None = None
    if mix_report_binding is not None:
        if not isinstance(mix_report_binding, dict):
            raise CreativeWorkflowError("candidate_review_artifacts_incomplete")
        _plain_bound_artifact(
            directory,
            mix_report_binding.get("path"),
            expected_sha256=mix_report_binding.get("sha256"),
            label="mix_report",
        )
        mix_report_sha256 = _checked_revision(
            mix_report_binding.get("sha256"), code="candidate_binding_incomplete"
        )
    manifest_authorization = candidate.get("authoring_workflow")
    receipt_authorization = receipt.get("authoring_workflow")
    if manifest_authorization is None and receipt_authorization is None:
        actual_authorization = None
    else:
        try:
            manifest_checked = validate_workflow_authorization(
                manifest_authorization, allow_none=False
            )
            receipt_checked = validate_workflow_authorization(
                receipt_authorization, allow_none=False
            )
        except (TypeError, ValueError) as exc:
            raise CreativeWorkflowError("invalid_workflow_authorization") from exc
        if manifest_checked != receipt_checked:
            raise CreativeWorkflowError("candidate_workflow_authorization_mismatch")
        actual_authorization = manifest_checked
    if expected_authorization is not None:
        try:
            expected_checked = validate_workflow_authorization(
                expected_authorization, allow_none=False
            )
        except (TypeError, ValueError) as exc:
            raise CreativeWorkflowError("invalid_expected_workflow_authorization") from exc
        if actual_authorization != expected_checked:
            raise CreativeWorkflowError("candidate_not_authorized_by_workflow")
        if candidate.get("candidate_id") != expected_checked["candidate_id"]:
            raise CreativeWorkflowError("candidate_workflow_authorization_mismatch")
        if candidate.get("work_id") != expected_checked["candidate_work_id"]:
            raise CreativeWorkflowError("candidate_workflow_authorization_mismatch")
        if candidate.get("parent_candidate_id") != expected_checked[
            "parent_candidate_id"
        ]:
            raise CreativeWorkflowError("candidate_workflow_authorization_mismatch")
        stored_authorization = expected_checked
        workflow_managed = True
    else:
        # Existing candidates may be inspected, including candidates managed
        # by another workflow, but they cannot be retroactively claimed by
        # this one.  Do not persist a foreign authorization as local authority.
        stored_authorization = None
        workflow_managed = False
    manifest_recheck, _ = _read_candidate_json(
        manifest_path,
        label="candidate_manifest",
        expected_sha256=manifest_sha256,
    )
    receipt_recheck, _ = _read_candidate_json(
        receipt_path,
        label="render_receipt",
        expected_sha256=receipt_binding.get("sha256"),
    )
    if manifest_recheck != candidate or receipt_recheck != receipt:
        raise CreativeWorkflowError("candidate_changed_during_verification")
    revalidate_plain_directory(identity)
    return {
        "candidate_id": _portable_identifier(
            candidate.get("candidate_id"), field="candidate_id"
        ),
        "work_id": _portable_identifier(candidate.get("work_id"), field="work_id"),
        "authoring_revision": authoring_revision,
        "candidate_manifest_sha256": manifest_sha256,
        "render_receipt_sha256": _checked_revision(
            receipt_binding.get("sha256"), code="candidate_binding_incomplete"
        ),
        "performance_plan_sha256": plan_sha256,
        "performance_plan_file_sha256": _checked_revision(
            plan_binding.get("file_sha256"), code="candidate_binding_incomplete"
        ),
        "mix_sha256": _checked_revision(
            mix_binding.get("sha256"), code="candidate_binding_incomplete"
        ),
        "post_render_check_sha256": _checked_revision(
            post_binding.get("sha256"), code="candidate_binding_incomplete"
        ),
        "mix_report_sha256": mix_report_sha256,
        "workflow_managed": workflow_managed,
        "workflow_authorization": copy.deepcopy(stored_authorization),
        "complete_review_artifacts": True,
        "verified_at_utc": _now(),
    }


def workflow_render_authorization(
    snapshot: CreativeWorkflowSnapshot,
) -> dict[str, Any]:
    """Return the exact authorization to embed in a pending render.

    ``reservation_revision`` is the immutable snapshot revision that owns the
    pending operation.  It is intentionally not self-embedded in the state
    document whose hash creates that revision.
    """

    if not isinstance(snapshot, CreativeWorkflowSnapshot):
        raise CreativeWorkflowError("workflow_snapshot_required")
    state = snapshot.detached_state()
    _validate_state_document(state)
    if state["status"] != "candidate_pending":
        raise CreativeWorkflowError("no_pending_render_reservation")
    iteration = _current_iteration(state)
    pending = [
        item for item in iteration["render_attempts"] if item["status"] == "pending"
    ]
    if len(pending) != 1:
        raise CreativeWorkflowError("no_pending_render_reservation")
    attempt = pending[0]
    parent = attempt["parent_candidate"]
    authorization = {
        "workflow_id": state["workflow_id"],
        "project_id": state["project_id"],
        "reservation_revision": snapshot.revision,
        "iteration_number": iteration["iteration_number"],
        "operation_id": attempt["operation_id"],
        "authoring_revision": attempt["authoring_revision"],
        "candidate_work_id": attempt["expected_work_id"],
        "candidate_id": attempt["expected_candidate_id"],
        "parent_work_id": None if parent is None else parent["work_id"],
        "parent_candidate_id": None if parent is None else parent["candidate_id"],
        "parent_manifest_sha256": (
            None if parent is None else parent["manifest_sha256"]
        ),
    }
    try:
        checked = validate_workflow_authorization(authorization, allow_none=False)
        assert checked is not None
        return checked
    except (TypeError, ValueError) as exc:
        raise CreativeWorkflowError("invalid_render_authorization") from exc


def _authoring_candidate_work_id(
    project_root: Path, *, authoring_revision: str
) -> str:
    try:
        state = open_authoring_project(project_root, revision=authoring_revision)
    except AuthoringProjectError as exc:
        raise CreativeWorkflowError("authoring_revision_unavailable") from exc
    title_value = state.documents["score"].get("title", "")
    title = title_value.strip() if isinstance(title_value, str) else ""
    return portable_slug(title or state.title)


def _verify_authorization_against_snapshot(
    project_root: Path,
    snapshot: CreativeWorkflowSnapshot,
    authorization: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        checked = validate_workflow_authorization(authorization, allow_none=False)
    except (TypeError, ValueError) as exc:
        raise CreativeWorkflowError("invalid_render_authorization") from exc
    state = snapshot.detached_state()
    if (
        checked["workflow_id"] != snapshot.workflow_id
        or checked["project_id"] != snapshot.project_id
        or checked["reservation_revision"] != snapshot.revision
        or state["status"] != "candidate_pending"
    ):
        raise CreativeWorkflowError("render_reservation_not_active")
    iteration = _current_iteration(state)
    pending = [
        item for item in iteration["render_attempts"] if item["status"] == "pending"
    ]
    if len(pending) != 1:
        raise CreativeWorkflowError("render_reservation_not_active")
    attempt = pending[0]
    parent = attempt["parent_candidate"]
    expected = {
        "workflow_id": state["workflow_id"],
        "project_id": state["project_id"],
        "reservation_revision": snapshot.revision,
        "iteration_number": iteration["iteration_number"],
        "operation_id": attempt["operation_id"],
        "authoring_revision": attempt["authoring_revision"],
        "candidate_work_id": attempt["expected_work_id"],
        "candidate_id": attempt["expected_candidate_id"],
        "parent_work_id": None if parent is None else parent["work_id"],
        "parent_candidate_id": None if parent is None else parent["candidate_id"],
        "parent_manifest_sha256": None if parent is None else parent["manifest_sha256"],
    }
    try:
        expected = validate_workflow_authorization(expected, allow_none=False)
    except (TypeError, ValueError) as exc:  # internal invariant defence
        raise CreativeWorkflowError("invalid_stored_render_reservation") from exc
    if checked != expected:
        raise CreativeWorkflowError("render_reservation_mismatch")
    actual_work_id = _authoring_candidate_work_id(
        project_root, authoring_revision=checked["authoring_revision"]
    )
    if actual_work_id != checked["candidate_work_id"]:
        raise CreativeWorkflowError("render_reservation_work_mismatch")
    return checked


def verify_active_render_reservation(
    project_root: str | os.PathLike[str],
    workflow_authorization: Mapping[str, Any],
) -> CreativeWorkflowSnapshot:
    """Verify that a render authorization is current, exact and unconsumed.

    Renderers must call this immediately before expensive work.  A copied or
    caller-invented authorization is insufficient: the immutable reservation
    revision must still be the workflow's current pointer and its sole pending
    operation must match every authoring, candidate and parent locator field.
    """

    try:
        checked = validate_workflow_authorization(
            workflow_authorization, allow_none=False
        )
    except (TypeError, ValueError) as exc:
        raise CreativeWorkflowError("invalid_render_authorization") from exc
    layout = _existing_layout(project_root, checked["workflow_id"])
    snapshot = _snapshot_from_layout(layout)
    _verify_authorization_against_snapshot(
        layout.project_root, snapshot, checked
    )
    return snapshot


def verify_render_reservation_history(
    project_root: str | os.PathLike[str],
    workflow_authorization: Mapping[str, Any],
) -> CreativeWorkflowSnapshot:
    """Prove that an immutable historical revision really reserved a render.

    Unlike :func:`verify_active_render_reservation`, this does not require the
    reservation to remain current.  It is suitable for inspecting a completed
    candidate whose operation was consumed by a later CAS transition.
    """

    try:
        checked = validate_workflow_authorization(
            workflow_authorization, allow_none=False
        )
    except (TypeError, ValueError) as exc:
        raise CreativeWorkflowError("invalid_render_authorization") from exc
    assert checked is not None
    layout = _existing_layout(project_root, checked["workflow_id"])
    manifest = _read_manifest(layout)
    revision = manifest["current_revision"]
    expected_sequence = manifest["current_sequence"]
    seen: set[str] = set()
    while True:
        if revision in seen:
            raise CreativeWorkflowError("workflow_history_cycle")
        seen.add(revision)
        state = _validate_revision_directory(
            _revision_path(layout, revision),
            workflow_id=manifest["workflow_id"],
            project_id=manifest["project_id"],
            revision=revision,
        )
        if state["sequence"] != expected_sequence:
            raise CreativeWorkflowError("workflow_history_sequence_mismatch")
        if revision == checked["reservation_revision"]:
            break
        if len(seen) >= MAX_WORKFLOW_HISTORY:
            raise CreativeWorkflowError("workflow_history_limit_exceeded")
        parent = state["parent_revision"]
        if expected_sequence == 1 or parent is None:
            raise CreativeWorkflowError("render_reservation_not_in_current_history")
        revision = parent
        expected_sequence -= 1
    snapshot = _snapshot_from_layout(
        layout, revision=checked["reservation_revision"]
    )
    _verify_authorization_against_snapshot(
        layout.project_root, snapshot, checked
    )
    return snapshot


def verify_creative_workflow_history(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    maximum_revisions: int = MAX_WORKFLOW_HISTORY,
) -> dict[str, Any]:
    """Verify the bounded parent chain from the current pointer to genesis."""

    if (
        isinstance(maximum_revisions, bool)
        or not isinstance(maximum_revisions, int)
        or not 1 <= maximum_revisions <= MAX_WORKFLOW_HISTORY
    ):
        raise CreativeWorkflowError("invalid_history_limit")
    layout = _existing_layout(project_root, workflow_id)
    manifest = _read_manifest(layout)
    revision = manifest["current_revision"]
    expected_sequence = manifest["current_sequence"]
    if expected_sequence > maximum_revisions:
        raise CreativeWorkflowError("workflow_history_limit_exceeded")
    seen: set[str] = set()
    verified = 0
    current_revision = revision
    genesis_revision: str | None = None
    while True:
        if revision in seen:
            raise CreativeWorkflowError("workflow_history_cycle")
        seen.add(revision)
        state = _validate_revision_directory(
            _revision_path(layout, revision),
            workflow_id=manifest["workflow_id"],
            project_id=manifest["project_id"],
            revision=revision,
        )
        if state["sequence"] != expected_sequence:
            raise CreativeWorkflowError("workflow_history_sequence_mismatch")
        verified += 1
        if verified > maximum_revisions:
            raise CreativeWorkflowError("workflow_history_limit_exceeded")
        parent = state["parent_revision"]
        if expected_sequence == 1:
            if parent is not None:
                raise CreativeWorkflowError("workflow_history_genesis_mismatch")
            genesis_revision = revision
            break
        if parent is None:
            raise CreativeWorkflowError("workflow_history_broken")
        revision = parent
        expected_sequence -= 1
    return {
        "kind": "tianlai.creative_workflow_history_verification",
        "schema_version": WORKFLOW_VERSION,
        "workflow_id": manifest["workflow_id"],
        "project_id": manifest["project_id"],
        "current_revision": current_revision,
        "current_sequence": manifest["current_sequence"],
        "genesis_revision": genesis_revision,
        "verified_revision_count": verified,
        "complete": True,
    }


def inspect_workflow_candidate_status(
    project_root: str | os.PathLike[str],
    *,
    candidate_path: str | os.PathLike[str],
) -> dict[str, Any]:
    """Distinguish authorization, workflow recording, and final acceptance."""

    requested = Path(candidate_path)
    if not requested.is_absolute():
        raise CreativeWorkflowError("candidate_path_must_be_absolute")
    try:
        identity = capture_plain_directory(requested)
        directory = revalidate_plain_directory(identity)
        _loaded, candidate = load_candidate(directory, verify=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("candidate_verification_failed") from exc
    authoring = candidate.get("authoring_project")
    if not isinstance(authoring, dict):
        raise CreativeWorkflowError("candidate_authoring_binding_mismatch")
    project_id = _require_hex(
        authoring.get("project_id"), _WORKFLOW_ID, "candidate_authoring_binding_mismatch"
    )
    authoring_revision = _checked_authoring_revision(authoring.get("revision"))
    raw_authorization = candidate.get("authoring_workflow")
    if raw_authorization is None:
        unmanaged = _verified_candidate_anchor(
            directory,
            project_id=project_id,
            authoring_revision=authoring_revision,
            expected_authorization=None,
        )
        return {
            "kind": "tianlai.workflow_candidate_status",
            "schema_version": WORKFLOW_VERSION,
            "candidate_id": unmanaged["candidate_id"],
            "work_id": unmanaged["work_id"],
            "candidate_manifest_sha256": unmanaged["candidate_manifest_sha256"],
            "workflow_id": None,
            "workflow_authorized": False,
            "workflow_recorded": False,
            "workflow_accepted": False,
            "recorded_iteration_numbers": [],
            "final_authority": None,
            "perception_basis": None,
        }
    try:
        authorization = validate_workflow_authorization(
            raw_authorization, allow_none=False
        )
    except (TypeError, ValueError) as exc:
        raise CreativeWorkflowError("invalid_workflow_authorization") from exc
    assert authorization is not None
    historical = verify_render_reservation_history(
        project_root, authorization
    )
    anchor = _verified_candidate_anchor(
        directory,
        project_id=project_id,
        authoring_revision=authoring_revision,
        expected_authorization=authorization,
    )
    current = open_creative_workflow(
        project_root, workflow_id=authorization["workflow_id"]
    )
    state = current.detached_state()
    recorded_iterations = [
        item["iteration_number"]
        for item in state["iterations"]
        if isinstance(item["anchor"].get("candidate"), dict)
        and _same_verified_candidate(item["anchor"]["candidate"], anchor)
    ]
    termination = state.get("termination")
    selected = termination.get("selected_candidate") if isinstance(termination, dict) else None
    accepted = bool(
        isinstance(termination, dict)
        and termination.get("reason") == "accepted_under_charter"
        and isinstance(selected, dict)
        and _same_verified_candidate(selected, anchor)
    )
    revalidate_plain_directory(identity)
    return {
        "kind": "tianlai.workflow_candidate_status",
        "schema_version": WORKFLOW_VERSION,
        "candidate_id": anchor["candidate_id"],
        "work_id": anchor["work_id"],
        "candidate_manifest_sha256": anchor["candidate_manifest_sha256"],
        "workflow_id": historical.workflow_id,
        "workflow_authorized": True,
        "workflow_recorded": bool(recorded_iterations),
        "workflow_accepted": accepted,
        "recorded_iteration_numbers": recorded_iterations,
        "final_authority": (
            termination.get("final_authority") if accepted else None
        ),
        "perception_basis": (
            termination.get("perception_basis") if accepted else None
        ),
    }


def record_workflow_review(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    phase: str,
    reviewer: str,
    perception_basis: str,
    summary: str,
) -> CreativeWorkflowSnapshot:
    if phase not in REVIEW_PHASES or reviewer not in REVIEWERS:
        raise CreativeWorkflowError("invalid_review_record")
    if perception_basis not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_review_record")
    if (phase == "audio_audition") is not (
        perception_basis == "audio_audition"
    ):
        raise CreativeWorkflowError("review_perception_mismatch")
    if reviewer in {"engine", "validator"} and perception_basis != "report_only":
        raise CreativeWorkflowError("machine_cannot_claim_audio_audition")
    checked_summary = _bounded_text(
        summary, field="review.summary", maximum_bytes=4096
    )

    def mutate(state: dict[str, Any], _layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        limit = state["budget"]["max_reviews_per_iteration"]
        if len(iteration["reviews"]) >= limit:
            raise CreativeWorkflowError("review_budget_exhausted")
        candidate_id = _candidate_id(iteration)
        if phase in {"render_report", "audio_audition"} and candidate_id is None:
            raise CreativeWorkflowError("candidate_required_for_review")
        timestamp = _now()
        body = {
            "phase": phase,
            "reviewer": reviewer,
            "perception_basis": perception_basis,
            "summary": checked_summary,
            "candidate_id": candidate_id,
            "reviewed_at_utc": timestamp,
        }
        review_id = "review-" + canonical_json_sha256(
            {
                "workflow_id": state["workflow_id"],
                "iteration_number": iteration["iteration_number"],
                **body,
            }
        )[:20]
        if any(item["review_id"] == review_id for item in iteration["reviews"]):
            raise CreativeWorkflowError("duplicate_review_record")
        iteration["reviews"].append({"review_id": review_id, **body})

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def record_workflow_evidence(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    category: str,
    code: str,
    basis_kind: str,
    basis_reference: str,
    reporter: str,
    perception_basis: str,
    summary: str,
    observation: str,
    interpretation: str,
    confidence: str,
    scope: Mapping[str, Any] | None = None,
    artifact_sha256: str | None = None,
    artifact_role: str | None = None,
) -> CreativeWorkflowSnapshot:
    if category not in EVIDENCE_CATEGORIES:
        raise CreativeWorkflowError("invalid_evidence_category")
    if category == "hard_failure":
        raise CreativeWorkflowError("hard_failure_requires_trusted_boundary")
    if reporter in {"engine", "validator"}:
        raise CreativeWorkflowError("trusted_reporter_requires_internal_boundary")
    if not isinstance(code, str) or _CODE.fullmatch(code) is None:
        raise CreativeWorkflowError("invalid_evidence_code")
    basis_ref = _bounded_text(
        basis_reference, field="evidence.basis.reference", maximum_bytes=1024
    )
    if artifact_sha256 is not None:
        artifact_sha256 = _checked_revision(
            artifact_sha256, code="invalid_evidence_artifact_hash"
        )
    if (artifact_sha256 is None) is not (artifact_role is None):
        raise CreativeWorkflowError("evidence_artifact_role_mismatch")
    if artifact_role is not None and artifact_role not in _EVIDENCE_ARTIFACT_FIELDS:
        raise CreativeWorkflowError("invalid_evidence_artifact_role")
    checked_text = {
        field: _bounded_text(value, field=f"evidence.{field}", maximum_bytes=4096)
        for field, value in {
            "summary": summary,
            "observation": observation,
            "interpretation": interpretation,
        }.items()
    }
    if confidence not in {"low", "medium", "high"}:
        raise CreativeWorkflowError("invalid_evidence_confidence")

    def mutate(state: dict[str, Any], _layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        limit = state["budget"]["max_evidence_items_per_iteration"]
        if len(iteration["evidence"]) >= limit:
            raise CreativeWorkflowError("evidence_budget_exhausted")
        normalized_scope = _normalize_scope(
            scope,
            authoring_revision=iteration["anchor"]["authoring_revision"],
            candidate_id=_candidate_id(iteration),
        )
        timestamp = _now()
        body = {
            "category": category,
            "code": code,
            "basis": {
                "kind": basis_kind,
                "reference": basis_ref,
                "artifact_sha256": artifact_sha256,
                "artifact_role": artifact_role,
            },
            "reporter": reporter,
            "perception_basis": perception_basis,
            **checked_text,
            "confidence": confidence,
            "scope": normalized_scope,
            "blocking": category == "hard_failure",
            "automatic_change": False,
            "recorded_at_utc": timestamp,
        }
        evidence_id = "evidence-" + canonical_json_sha256(
            {
                "workflow_id": state["workflow_id"],
                "iteration_number": iteration["iteration_number"],
                **body,
            }
        )[:20]
        record = {"evidence_id": evidence_id, **body}
        _validate_evidence(
            record,
            iteration=iteration,
            active_clause_ids={
                item["clause_id"] for item in state["active_clauses"]
            },
            charter_fields=set(state["work_charter"]),
        )
        if any(
            item["evidence_id"] == evidence_id for item in iteration["evidence"]
        ):
            raise CreativeWorkflowError("duplicate_evidence_record")
        iteration["evidence"].append(record)

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def record_verified_workflow_hard_failure(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    issue_code: str,
) -> CreativeWorkflowSnapshot:
    """Record only a hard failure reproduced by the trusted readiness core."""

    if not isinstance(issue_code, str) or _CODE.fullmatch(issue_code) is None:
        raise CreativeWorkflowError("invalid_evidence_code")

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        if len(iteration["evidence"]) >= state["budget"]["max_evidence_items_per_iteration"]:
            raise CreativeWorkflowError("evidence_budget_exhausted")
        try:
            authoring = open_authoring_project(
                layout.project_root,
                revision=iteration["anchor"]["authoring_revision"],
            )
            readiness = validate_project_readiness(
                authoring, project_root=layout.project_root
            )
        except (AuthoringProjectError, RuntimeError, ValueError) as exc:
            raise CreativeWorkflowError("trusted_validation_failed") from exc
        matching = [
            item
            for item in readiness.get("issues", [])
            if isinstance(item, dict)
            and item.get("decision") == "block"
            and item.get("code") == issue_code
        ]
        if not matching:
            raise CreativeWorkflowError("hard_failure_not_reproduced")
        timestamp = _now()
        body = {
            "category": "hard_failure",
            "code": issue_code,
            "basis": {
                "kind": "engine_contract",
                "reference": issue_code,
                "artifact_sha256": None,
                "artifact_role": None,
            },
            "reporter": "validator",
            "perception_basis": "report_only",
            "summary": f"Verified engine-contract failure: {issue_code}",
            "observation": "The trusted authoring readiness boundary reproduced this blocking issue.",
            "interpretation": "Rendering or acceptance remains blocked until a later revision no longer reproduces it.",
            "confidence": "high",
            "scope": _normalize_scope(
                None,
                authoring_revision=iteration["anchor"]["authoring_revision"],
                candidate_id=_candidate_id(iteration),
            ),
            "blocking": True,
            "automatic_change": False,
            "recorded_at_utc": timestamp,
        }
        evidence_id = "evidence-" + canonical_json_sha256(
            {
                "workflow_id": state["workflow_id"],
                "iteration_number": iteration["iteration_number"],
                **body,
            }
        )[:20]
        record = {"evidence_id": evidence_id, **body}
        _validate_evidence(
            record,
            iteration=iteration,
            active_clause_ids={
                item["clause_id"] for item in state["active_clauses"]
            },
            charter_fields=set(state["work_charter"]),
            trusted_hard_failure=True,
        )
        if any(item["evidence_id"] == evidence_id for item in iteration["evidence"]):
            raise CreativeWorkflowError("duplicate_evidence_record")
        iteration["evidence"].append(record)

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def _unresolved_trusted_hard_failures(
    iteration: dict[str, Any], *, project_root: Path
) -> list[dict[str, Any]]:
    """Reproduce historical hard evidence at the current trusted boundary."""

    historical = [
        item
        for item in iteration["evidence"]
        if item["category"] == "hard_failure"
    ]
    if not historical:
        return []
    try:
        authoring = open_authoring_project(
            project_root,
            revision=iteration["anchor"]["authoring_revision"],
        )
        readiness = validate_project_readiness(
            authoring, project_root=project_root
        )
    except (AuthoringProjectError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("trusted_validation_failed") from exc
    current_codes = {
        item.get("code")
        for item in readiness.get("issues", [])
        if isinstance(item, dict)
        and item.get("decision") == "block"
        and isinstance(item.get("code"), str)
    }
    if readiness.get("issues_truncated") is True and readiness.get(
        "render_allowed"
    ) is False:
        # A bounded readiness result cannot prove that an omitted historical
        # code disappeared.  Keep those items unresolved rather than guessing.
        return historical
    return [item for item in historical if item["code"] in current_codes]


def unresolved_workflow_hard_failures(
    project_root: str | os.PathLike[str],
    snapshot: CreativeWorkflowSnapshot,
) -> list[dict[str, Any]]:
    """Return trusted hard failures that still reproduce for the current revision.

    The supplied snapshot is used as a compare-and-swap-style freshness token;
    its detached state is never trusted as validation input.  The current
    durable snapshot is reopened and must have the same revision before the
    authoring readiness boundary is evaluated.
    """

    if not isinstance(snapshot, CreativeWorkflowSnapshot):
        raise CreativeWorkflowError("invalid_workflow_snapshot")
    current = open_creative_workflow(
        project_root,
        workflow_id=snapshot.workflow_id,
    )
    if current.project_id != snapshot.project_id:
        raise CreativeWorkflowError("workflow_project_mismatch")
    if current.revision != snapshot.revision:
        raise CreativeWorkflowError("workflow_revision_conflict")
    state = current.detached_state()
    if not state["iterations"]:
        return []
    return copy.deepcopy(
        _unresolved_trusted_hard_failures(
            _current_iteration(state),
            project_root=Path(project_root).resolve(),
        )
    )


def register_workflow_exception(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    target_type: str,
    target_ref: str,
    purpose: str,
    scope: str,
    higher_value: str,
    cost: str,
    recovery: str,
    evidence_ids: Sequence[str],
    reusable: bool = False,
) -> CreativeWorkflowSnapshot:
    checked = {
        field: _bounded_text(value, field=f"exception.{field}", maximum_bytes=4096)
        for field, value in {
            "target_ref": target_ref,
            "purpose": purpose,
            "scope": scope,
            "higher_value": higher_value,
            "cost": cost,
            "recovery": recovery,
        }.items()
    }
    if target_type not in {"active_clause", "work_charter"} or not isinstance(
        reusable, bool
    ):
        raise CreativeWorkflowError("invalid_exception_record")
    ids = list(evidence_ids) if isinstance(evidence_ids, (list, tuple)) else []
    if not ids or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids):
        raise CreativeWorkflowError("exception_evidence_required")

    def mutate(state: dict[str, Any], _layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        if len(iteration["exceptions"]) >= state["budget"]["max_exceptions_per_iteration"]:
            raise CreativeWorkflowError("exception_budget_exhausted")
        timestamp = _now()
        body = {
            "target_type": target_type,
            **checked,
            "evidence_ids": ids,
            "reusable": reusable,
            "registered_at_utc": timestamp,
        }
        exception_id = "exception-" + canonical_json_sha256(
            {
                "workflow_id": state["workflow_id"],
                "iteration_number": iteration["iteration_number"],
                **body,
            }
        )[:20]
        record = {"exception_id": exception_id, **body}
        _validate_exception(
            record,
            evidence_by_id={
                item["evidence_id"]: item for item in iteration["evidence"]
            },
            active_clause_ids={item["clause_id"] for item in state["active_clauses"]},
        )
        if any(
            item["exception_id"] == exception_id for item in iteration["exceptions"]
        ):
            raise CreativeWorkflowError("duplicate_exception_record")
        iteration["exceptions"].append(record)

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def request_workflow_render(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
) -> CreativeWorkflowSnapshot:
    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        if iteration["anchor"]["candidate"] is not None:
            raise CreativeWorkflowError("iteration_candidate_already_bound")
        phases = {item["phase"] for item in iteration["reviews"]}
        if not {"symbolic_structure", "orchestration_performance"}.issubset(phases):
            raise CreativeWorkflowError("pre_render_review_incomplete")
        if _unresolved_trusted_hard_failures(
            iteration, project_root=layout.project_root
        ):
            raise CreativeWorkflowError("hard_failure_blocks_render")
        attempts = iteration["render_attempts"]
        if len(attempts) >= state["budget"]["max_render_attempts_per_iteration"]:
            raise CreativeWorkflowError("render_attempt_budget_exhausted")
        operation_id = secrets.token_hex(16)
        expected_candidate_id = portable_slug(
            f"workflow-{operation_id}", maximum_length=96
        )
        expected_work_id = _authoring_candidate_work_id(
            layout.project_root,
            authoring_revision=iteration["anchor"]["authoring_revision"],
        )
        attempts.append(
            {
                "attempt_number": len(attempts) + 1,
                "operation_id": operation_id,
                "expected_work_id": expected_work_id,
                "expected_candidate_id": expected_candidate_id,
                "reservation_revision": None,
                "authoring_revision": iteration["anchor"]["authoring_revision"],
                "parent_candidate": copy.deepcopy(
                    iteration["anchor"]["parent_candidate"]
                ),
                "status": "pending",
                "requested_at_utc": _now(),
                "finished_at_utc": None,
            }
        )
        iteration["status"] = "candidate_pending"
        state["status"] = "candidate_pending"

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def cancel_workflow_render(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
) -> CreativeWorkflowSnapshot:
    def mutate(state: dict[str, Any], _layout: _WorkflowLayout, expected: str) -> None:
        _require_status(state, "candidate_pending")
        iteration = _current_iteration(state)
        pending = [
            item for item in iteration["render_attempts"] if item["status"] == "pending"
        ]
        if len(pending) != 1:
            raise CreativeWorkflowError("no_pending_render_reservation")
        pending[0]["reservation_revision"] = expected
        pending[0]["status"] = "cancelled"
        pending[0]["finished_at_utc"] = _now()
        iteration["status"] = "reviewing"
        state["status"] = "reviewing"

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def attach_existing_candidate_for_audit(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    candidate_path: str | os.PathLike[str],
) -> CreativeWorkflowSnapshot:
    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        if iteration["anchor"]["candidate"] is not None or iteration["render_attempts"]:
            raise CreativeWorkflowError("iteration_candidate_already_bound")
        anchor = _verified_candidate_anchor(
            candidate_path,
            project_id=state["project_id"],
            authoring_revision=iteration["anchor"]["authoring_revision"],
            expected_authorization=None,
        )
        iteration["anchor"]["candidate"] = anchor

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def record_workflow_candidate(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    candidate_path: str | os.PathLike[str],
) -> CreativeWorkflowSnapshot:
    def mutate(state: dict[str, Any], layout: _WorkflowLayout, expected: str) -> None:
        _require_status(state, "candidate_pending")
        iteration = _current_iteration(state)
        pending = [
            item for item in iteration["render_attempts"] if item["status"] == "pending"
        ]
        if len(pending) != 1:
            raise CreativeWorkflowError("no_pending_render_reservation")
        attempt = pending[0]
        parent = attempt["parent_candidate"]
        authorization = validate_workflow_authorization(
            {
                "workflow_id": state["workflow_id"],
                "project_id": state["project_id"],
                "reservation_revision": expected,
                "iteration_number": iteration["iteration_number"],
                "operation_id": attempt["operation_id"],
                "authoring_revision": attempt["authoring_revision"],
                "candidate_work_id": attempt["expected_work_id"],
                "candidate_id": attempt["expected_candidate_id"],
                "parent_work_id": None if parent is None else parent["work_id"],
                "parent_candidate_id": None if parent is None else parent["candidate_id"],
                "parent_manifest_sha256": (
                    None if parent is None else parent["manifest_sha256"]
                ),
            },
            allow_none=False,
        )
        current_snapshot = CreativeWorkflowSnapshot(
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            revision=expected,
            created_at_utc=state["created_at_utc"],
            updated_at_utc=state["updated_at_utc"],
            state=state,
        )
        _verify_authorization_against_snapshot(
            layout.project_root, current_snapshot, authorization
        )
        anchor = _verified_candidate_anchor(
            candidate_path,
            project_id=state["project_id"],
            authoring_revision=attempt["authoring_revision"],
            expected_authorization=authorization,
        )
        attempt["reservation_revision"] = expected
        attempt["status"] = "completed"
        attempt["finished_at_utc"] = _now()
        iteration["anchor"]["candidate"] = anchor
        iteration["status"] = "reviewing"
        state["status"] = "reviewing"

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def _authority_has_basis(
    iteration: dict[str, Any], *, authority: str, perception_basis: str
) -> bool:
    if perception_basis == "audio_audition" and not isinstance(
        iteration["anchor"].get("candidate"), dict
    ):
        return False
    reviewer = authority
    return any(
        item["reviewer"] == reviewer
        and item["perception_basis"] == perception_basis
        and (
            perception_basis != "audio_audition"
            or item["phase"] == "audio_audition"
        )
        for item in iteration["reviews"]
    )


def _decision_record(
    *,
    disposition: str,
    summary: str,
    rationale: str,
    protected_values: Sequence[str],
    sacrificed_values: Sequence[str],
    evidence_ids: Sequence[str],
    exception_ids: Sequence[str],
    expected_audible_change: str | None,
    final_authority: str,
    perception_basis: str,
    timestamp: str,
) -> dict[str, Any]:
    if final_authority not in FINAL_AUTHORITIES or perception_basis not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_decision_authority")
    return {
        "disposition": disposition,
        "summary": _bounded_text(summary, field="decision.summary", maximum_bytes=4096),
        "rationale": _bounded_text(
            rationale, field="decision.rationale", maximum_bytes=4096
        ),
        "protected_values": _bounded_text_list(
            list(protected_values),
            field="decision.protected_values",
            maximum_items=32,
        ),
        "sacrificed_values": _bounded_text_list(
            list(sacrificed_values),
            field="decision.sacrificed_values",
            maximum_items=32,
        ),
        "evidence_ids": list(evidence_ids),
        "exception_ids": list(exception_ids),
        "expected_audible_change": (
            None
            if expected_audible_change is None
            else _bounded_text(
                expected_audible_change,
                field="decision.expected_audible_change",
                maximum_bytes=4096,
            )
        ),
        "final_authority": final_authority,
        "perception_basis": perception_basis,
        "claim_scope": "contextual_workflow_decision_not_objective_quality",
        "decided_at_utc": timestamp,
    }


def _close_iteration(
    iteration: dict[str, Any], *, outcome: str, timestamp: str
) -> None:
    iteration["status"] = "closed"
    iteration["outcome"] = outcome
    iteration["closed_at_utc"] = timestamp


def _same_verified_candidate(
    stored: Mapping[str, Any], observed: Mapping[str, Any]
) -> bool:
    return all(
        stored.get(key) == observed.get(key)
        for key in stored
        if key != "verified_at_utc"
    )


def decide_workflow_iteration(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    disposition: str,
    summary: str,
    rationale: str,
    final_authority: str,
    perception_basis: str,
    protected_values: Sequence[str] = (),
    sacrificed_values: Sequence[str] = (),
    evidence_ids: Sequence[str] = (),
    exception_ids: Sequence[str] = (),
    expected_audible_change: str | None = None,
    candidate_path: str | os.PathLike[str] | None = None,
) -> CreativeWorkflowSnapshot:
    allowed = {"accept", "revise", "recommend_revision", "preserve", "stop"}
    if disposition not in allowed:
        raise CreativeWorkflowError("invalid_iteration_disposition")
    ids = list(evidence_ids)
    exception_refs = list(exception_ids)
    if (
        any(not isinstance(item, str) for item in ids + exception_refs)
        or len(set(ids)) != len(ids)
        or len(set(exception_refs)) != len(exception_refs)
    ):
        raise CreativeWorkflowError("invalid_decision_references")

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        if final_authority != state["final_authority"]:
            raise CreativeWorkflowError("decision_authority_mismatch")
        timestamp = _now()
        decision = _decision_record(
            disposition=disposition,
            summary=summary,
            rationale=rationale,
            protected_values=protected_values,
            sacrificed_values=sacrificed_values,
            evidence_ids=ids,
            exception_ids=exception_refs,
            expected_audible_change=expected_audible_change,
            final_authority=final_authority,
            perception_basis=perception_basis,
            timestamp=timestamp,
        )
        _validate_decision(decision, iteration=iteration)
        if (
            disposition != "stop" or perception_basis == "audio_audition"
        ) and not _authority_has_basis(
            iteration,
            authority=final_authority,
            perception_basis=perception_basis,
        ):
            raise CreativeWorkflowError("decision_perception_basis_unproven")

        candidate = iteration["anchor"]["candidate"]
        hard_failures = _unresolved_trusted_hard_failures(
            iteration, project_root=layout.project_root
        )
        if disposition == "accept":
            if not isinstance(candidate, dict):
                raise CreativeWorkflowError("verified_candidate_required_for_acceptance")
            if not candidate["workflow_managed"]:
                raise CreativeWorkflowError("unmanaged_candidate_cannot_be_accepted")
            if not candidate["complete_review_artifacts"]:
                raise CreativeWorkflowError("candidate_review_artifacts_incomplete")
            if hard_failures:
                raise CreativeWorkflowError("hard_failure_blocks_acceptance")
            completed_review_phases = {
                item["phase"] for item in iteration["reviews"]
            }
            required_review_phases = {
                "intent",
                "symbolic_structure",
                "orchestration_performance",
                "render_report",
            }
            if not required_review_phases.issubset(completed_review_phases):
                raise CreativeWorkflowError("acceptance_review_sequence_incomplete")
            if (
                perception_basis == "audio_audition"
                and "audio_audition" not in completed_review_phases
            ):
                raise CreativeWorkflowError("audio_audition_review_required")
            if candidate_path is None:
                raise CreativeWorkflowError("candidate_reverification_required")
            verify_render_reservation_history(
                layout.project_root, candidate["workflow_authorization"]
            )
            observed = _verified_candidate_anchor(
                candidate_path,
                project_id=state["project_id"],
                authoring_revision=iteration["anchor"]["authoring_revision"],
                expected_authorization=candidate["workflow_authorization"],
            )
            if not _same_verified_candidate(candidate, observed):
                raise CreativeWorkflowError("candidate_changed_since_recording")
            iteration["decision"] = decision
            _close_iteration(iteration, outcome="accepted", timestamp=timestamp)
            state["status"] = "completed"
            state["termination"] = {
                "reason": "accepted_under_charter",
                "summary": decision["summary"],
                "final_authority": final_authority,
                "perception_basis": perception_basis,
                "selected_candidate": copy.deepcopy(observed),
                "terminated_at_utc": timestamp,
            }
            return

        if disposition == "revise":
            if state["mode"] != "iterate":
                raise CreativeWorkflowError("revision_not_allowed_in_mode")
            if state["usage"]["revision_cycles"] >= state["budget"]["max_revision_cycles"]:
                raise CreativeWorkflowError("revision_budget_exhausted")
            if not ids:
                raise CreativeWorkflowError("revision_evidence_required")
            iteration["decision"] = decision
            iteration["status"] = "revision_pending"
            state["status"] = "revision_pending"
            return

        if disposition == "recommend_revision":
            if state["mode"] != "audit" or not ids:
                raise CreativeWorkflowError("revision_recommendation_not_allowed")
            iteration["decision"] = decision
            _close_iteration(
                iteration, outcome="revision_recommended", timestamp=timestamp
            )
            state["status"] = "stopped"
            state["termination"] = {
                "reason": "revision_recommended",
                "summary": decision["summary"],
                "final_authority": final_authority,
                "perception_basis": perception_basis,
                "selected_candidate": copy.deepcopy(candidate),
                "terminated_at_utc": timestamp,
            }
            return

        iteration["decision"] = decision
        outcome = "preserved" if disposition == "preserve" else "stopped"
        _close_iteration(iteration, outcome=outcome, timestamp=timestamp)
        state["status"] = "stopped"
        state["termination"] = {
            "reason": (
                "preserved_without_acceptance"
                if disposition == "preserve"
                else "creator_stopped" if final_authority == "creator" else "agent_stopped"
            ),
            "summary": decision["summary"],
            "final_authority": final_authority,
            "perception_basis": perception_basis,
            "selected_candidate": copy.deepcopy(candidate),
            "terminated_at_utc": timestamp,
        }

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def record_workflow_authoring_revision(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    authoring_revision: str,
) -> CreativeWorkflowSnapshot:
    checked_revision = _checked_authoring_revision(authoring_revision)

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "revision_pending")
        if state["mode"] != "iterate":
            raise CreativeWorkflowError("revision_not_allowed_in_mode")
        iteration = _current_iteration(state)
        if checked_revision == iteration["anchor"]["authoring_revision"]:
            raise CreativeWorkflowError("authoring_revision_unchanged")
        try:
            authoring = open_authoring_project(
                layout.project_root, revision=checked_revision
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError("authoring_revision_unavailable") from exc
        if authoring.project_id != state["project_id"]:
            raise CreativeWorkflowError("authoring_project_identity_changed")
        if state["usage"]["revision_cycles"] >= state["budget"]["max_revision_cycles"]:
            raise CreativeWorkflowError("revision_budget_exhausted")
        if len(state["iterations"]) >= MAX_ITERATIONS:
            raise CreativeWorkflowError("iteration_limit_exceeded")
        timestamp = _now()
        candidate = iteration["anchor"]["candidate"]
        parent = (
            _anchor_locator(candidate)
            if isinstance(candidate, dict)
            else copy.deepcopy(iteration["anchor"]["parent_candidate"])
        )
        iteration["next_authoring_revision"] = checked_revision
        _close_iteration(iteration, outcome="revised", timestamp=timestamp)
        state["iterations"].append(
            _new_iteration(
                len(state["iterations"]) + 1,
                authoring_revision=checked_revision,
                parent_candidate=parent,
                opened_at_utc=timestamp,
            )
        )
        state["status"] = "reviewing"

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def rollback_workflow(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    target_iteration_number: int,
    summary: str,
    rationale: str,
    final_authority: str,
    perception_basis: str,
) -> CreativeWorkflowSnapshot:
    if (
        isinstance(target_iteration_number, bool)
        or not isinstance(target_iteration_number, int)
        or target_iteration_number < 1
    ):
        raise CreativeWorkflowError("invalid_rollback_target")

    def mutate(state: dict[str, Any], _layout: _WorkflowLayout, expected: str) -> None:
        _require_status(
            state, "reviewing", "candidate_pending"
        )
        if state["mode"] != "iterate":
            raise CreativeWorkflowError("rollback_not_allowed_in_mode")
        if final_authority != state["final_authority"]:
            raise CreativeWorkflowError("decision_authority_mismatch")
        current = _current_iteration(state)
        if not target_iteration_number < current["iteration_number"]:
            raise CreativeWorkflowError("invalid_rollback_target")
        if state["usage"]["rollbacks"] >= state["budget"]["max_rollbacks"]:
            raise CreativeWorkflowError("rollback_budget_exhausted")
        if len(state["iterations"]) >= MAX_ITERATIONS:
            raise CreativeWorkflowError("iteration_limit_exceeded")
        target = state["iterations"][target_iteration_number - 1]
        candidate = target["anchor"].get("candidate")
        if not isinstance(candidate, dict):
            raise CreativeWorkflowError("rollback_target_has_no_candidate")
        if not _authority_has_basis(
            current, authority=final_authority, perception_basis=perception_basis
        ):
            raise CreativeWorkflowError("decision_perception_basis_unproven")
        timestamp = _now()
        decision = _decision_record(
            disposition="rollback",
            summary=summary,
            rationale=rationale,
            protected_values=(),
            sacrificed_values=(),
            evidence_ids=(),
            exception_ids=(),
            expected_audible_change=None,
            final_authority=final_authority,
            perception_basis=perception_basis,
            timestamp=timestamp,
        )
        _validate_decision(decision, iteration=current)
        if state["status"] == "candidate_pending":
            pending = [
                item
                for item in current["render_attempts"]
                if item["status"] == "pending"
            ]
            if len(pending) != 1:
                raise CreativeWorkflowError("no_pending_render_reservation")
            pending[0]["reservation_revision"] = expected
            pending[0]["status"] = "cancelled"
            pending[0]["finished_at_utc"] = timestamp
        current["decision"] = decision
        _close_iteration(current, outcome="rolled_back", timestamp=timestamp)
        state["iterations"].append(
            _new_iteration(
                len(state["iterations"]) + 1,
                authoring_revision=target["anchor"]["authoring_revision"],
                parent_candidate=copy.deepcopy(target["anchor"]["parent_candidate"]),
                candidate=copy.deepcopy(candidate),
                opened_at_utc=timestamp,
            )
        )
        state["status"] = "reviewing"

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def terminate_creative_workflow(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    reason: str,
    summary: str,
    final_authority: str,
    perception_basis: str = "report_only",
) -> CreativeWorkflowSnapshot:
    allowed_reasons = {
        "budget_exhausted",
        "no_material_improvement",
        "human_review_required",
        "external_blocker",
        "cancelled",
    }
    if reason not in allowed_reasons:
        raise CreativeWorkflowError("invalid_termination_reason")
    checked_summary = _bounded_text(
        summary, field="termination.summary", maximum_bytes=4096
    )
    if final_authority not in FINAL_AUTHORITIES or perception_basis not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_workflow_termination")

    def mutate(state: dict[str, Any], _layout: _WorkflowLayout, expected: str) -> None:
        if state["status"] in TERMINAL_WORKFLOW_STATUSES:
            raise CreativeWorkflowError("workflow_already_terminal")
        if final_authority != state["final_authority"]:
            raise CreativeWorkflowError("termination_authority_mismatch")
        timestamp = _now()
        selected: dict[str, Any] | None = None
        if perception_basis == "audio_audition" and not state["iterations"]:
            raise CreativeWorkflowError("decision_perception_basis_unproven")
        if state["iterations"]:
            iteration = _current_iteration(state)
            if perception_basis == "audio_audition" and not _authority_has_basis(
                iteration,
                authority=final_authority,
                perception_basis=perception_basis,
            ):
                raise CreativeWorkflowError("decision_perception_basis_unproven")
            if state["status"] == "candidate_pending":
                pending = [
                    item
                    for item in iteration["render_attempts"]
                    if item["status"] == "pending"
                ]
                if len(pending) != 1:
                    raise CreativeWorkflowError("no_pending_render_reservation")
                pending[0]["reservation_revision"] = expected
                pending[0]["status"] = "cancelled"
                pending[0]["finished_at_utc"] = timestamp
            selected = copy.deepcopy(iteration["anchor"].get("candidate"))
            _close_iteration(iteration, outcome="stopped", timestamp=timestamp)
        state["status"] = "stopped"
        state["termination"] = {
            "reason": reason,
            "summary": checked_summary,
            "final_authority": final_authority,
            "perception_basis": perception_basis,
            "selected_candidate": selected,
            "terminated_at_utc": timestamp,
        }

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
        allow_reserved_termination_transition=True,
    )


__all__ = [
    "CreativeWorkflowError",
    "CreativeWorkflowSnapshot",
    "WORKFLOW_MODES",
    "WORKFLOW_VERSION",
    "activate_creative_workflow",
    "attach_existing_candidate_for_audit",
    "cancel_workflow_render",
    "create_creative_workflow",
    "decide_workflow_iteration",
    "inspect_workflow_candidate_status",
    "open_creative_workflow",
    "record_verified_workflow_hard_failure",
    "record_workflow_authoring_revision",
    "record_workflow_candidate",
    "record_workflow_evidence",
    "record_workflow_review",
    "register_workflow_exception",
    "request_workflow_render",
    "rollback_workflow",
    "terminate_creative_workflow",
    "unresolved_workflow_hard_failures",
    "verify_active_render_reservation",
    "verify_creative_workflow_history",
    "verify_render_reservation_history",
    "workflow_render_authorization",
]
