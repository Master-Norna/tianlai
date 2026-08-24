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
    MAX_AUTHORING_SAVE_SEQUENCE,
    PRIVATE_DIRECTORY_NAME,
    AuthoringProjectError,
    open_authoring_project,
    verify_authoring_revision_ancestry,
    verify_authoring_save_event_binding,
)
from .candidate import (
    CANDIDATE_MANIFEST_NAME,
    load_candidate,
    portable_slug,
)
from .charter_amendment import (
    CharterAmendmentError,
    charter_amendment_cost_acknowledgement,
    commit_charter_amendment as commit_charter_amendment_ledger,
    effective_charter_from_ledger,
    index_charter_claims,
    preflight_charter_amendment,
    verify_charter_amendment_ledger,
)
from .canonical_json import CANONICALIZATION, canonical_json_sha256
from .composition_map import (
    CompositionMapError,
    composition_map_sha256,
    inspect_composition_map,
    normalize_composition_map,
    validate_composition_map,
)
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
from .score import parse_score_document
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
DIRECT_TERMINATION_REASONS = frozenset(
    {
        "budget_exhausted",
        "no_material_improvement",
        "human_review_required",
        "external_blocker",
        "cancelled",
    }
)
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
MAX_DERIVATIONS_PER_ITERATION = 64
DEFAULT_MAX_DERIVATIONS_PER_ITERATION = 8
MAX_DERIVATION_PREMISES = 8
MAX_DERIVATION_ALTERNATIVES = 8
MAX_DERIVATION_CLAUSE_REFS = 12
MAX_DERIVATION_MATERIAL_REFS = 32
MAX_ACTIVE_CLAUSES = 60
MAX_ITERATIONS = 1 + MAX_REVISION_CYCLES + MAX_ROLLBACKS
MAX_COMPOSITION_MAPS = MAX_ITERATIONS
MAX_CHARTER_AMENDMENT_PREFLIGHTS = 128
MAX_CHARTER_AMENDMENTS = 32
MAX_REVIEW_QUESTION_ANSWERS = 128
MAX_REVIEW_CLAIM_REFERENCES = 1024
MAX_REVIEW_NODE_REFERENCES = 256
MAX_REVIEW_EVENT_REFERENCES = 128
# The durable chain must cover every legal transition admitted by the public
# budgets, plus one transition that is reserved exclusively for termination.
_BUDGETED_TRANSITIONS_PER_ITERATION = (
    MAX_REVIEWS_PER_ITERATION
    + MAX_EVIDENCE_PER_ITERATION
    + MAX_EXCEPTIONS_PER_ITERATION
    + MAX_DERIVATIONS_PER_ITERATION
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

_LEGACY_POLICY = {
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
_CLAIM_LIFECYCLE_POLICY = {
    **_LEGACY_POLICY,
    "claim_lifecycle_profile": "explicit-v1",
}
_ACCEPTANCE_GATE_POLICY = {
    **_CLAIM_LIFECYCLE_POLICY,
    "acceptance_gate_profile": "recorded-hard-failure-recheck-v1",
}
_SETTLEMENT_POLICY = {
    **_ACCEPTANCE_GATE_POLICY,
    "charter_settlement_profile": "affirmative-promise-ledger-v1",
}
_POLICY = {
    **_SETTLEMENT_POLICY,
    "revision_contract_profile": "bounded-change-and-explicit-challenger-settlement-v1",
}
_LEGACY_GOVERNANCE_POLICY = {
    **_SETTLEMENT_POLICY,
    "composition_governance_profile": "whole-work-derivation-and-bounded-amendment-v1",
}
_GOVERNANCE_POLICY = {
    **_POLICY,
    "composition_governance_profile": "whole-work-derivation-and-bounded-amendment-v1",
}

_COMPOSITION_GOVERNANCE_PROFILE = (
    "whole-work-derivation-and-bounded-amendment-v1"
)
_GOVERNANCE_REVIEW_PHASES = frozenset(
    {"intent", "symbolic_structure", "orchestration_performance"}
)

_REVISION_DOCUMENTS = frozenset(
    {"score", "authoring_roster", "render_profile"}
)
_REVISION_NOTE_FIELDS = frozenset(
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
    }
)
_WHOLE_WORK_COSTS = frozenset(
    {
        "expanded_change_surface",
        "downstream_compatibility_rework",
        "increased_topic_drift_risk",
    }
)
_REVISION_ASSESSMENT_OUTCOMES = frozenset(
    {"promote_challenger", "retain_baseline", "inconclusive"}
)

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
DERIVATION_PREMISE_KINDS = frozenset(
    {
        "declared_promise",
        "active_clause",
        "established_material",
        "render_measurement",
    }
)
_DECISION_KEYS = frozenset(
    {
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
    }
)
_DECISION_CLAIM_KEYS = frozenset({"review_ids", "evidence_dispositions"})
CHARTER_SETTLEMENT_STATUSES = frozenset({"kept", "transformed", "refused"})
# one_sentence_promise + ending_contract + every identity invariant.
MAX_SETTLEMENT_ITEMS = 64
MAX_FORKS_PER_ITERATION = 8
MAX_FORK_BRANCHES = 8
MAX_FORK_DERIVATION_REFS = 8
MAX_SETTLEMENT_BASIS_IDS = 16
_ACCEPTANCE_GATE_PROFILE = "recorded-hard-failure-recheck-v1"
_ACCEPTANCE_GATE_KIND = "tianlai.workflow_acceptance_gate"
_ACCEPTANCE_GATE_CLAIM_SCOPE = (
    "point_in_time_recorded_hard_failure_recheck_not_current_readiness_or_"
    "aesthetic_quality"
)
_EVIDENCE_DISPOSITIONS = frozenset(
    {
        "resolved",
        "contested",
        "accepted_risk",
        "excepted",
        "revision_target",
        "deferred",
    }
)
_OPEN_EVIDENCE_DISPOSITIONS = frozenset(
    {"contested", "revision_target", "deferred"}
)
MAX_DECISION_BASIS_IDS = 32


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


def _transition_timestamp(
    state: dict[str, Any], *lower_bounds: str
) -> str:
    """Return and retain a causal timestamp for one state transition.

    Wall clocks can move backwards between two compare-and-swap revisions (or
    even between the event timestamp and pointer publication).  Clamp durable
    event time to the already verified aggregate clock and any event-specific
    lower bounds, then retain it in ``updated_at_utc`` so publication cannot
    move behind the event it contains.
    """

    observed = _now()
    try:
        validate_canonical_utc_timestamp(observed)
    except ValueError as exc:
        raise CreativeWorkflowError("invalid_system_timestamp") from exc
    timestamp = max(
        observed,
        state["created_at_utc"],
        state["updated_at_utc"],
        *lower_bounds,
    )
    state["updated_at_utc"] = timestamp
    return timestamp


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
        # Workflow snapshots use recursively immutable dict/list subclasses.
        # Convert the complete graph, not only the top-level mapping, before
        # the strict authoring JSON boundary rejects arbitrary subclasses.
        payload = json_document_bytes(_thaw(value), limits=_WORKFLOW_LIMITS)
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
        # Derivations are scarce branch-closing arguments, not a quota to fill.
        # Eight leaves room for the structural hinges of a substantial work
        # without rewarding bar-by-bar procedural over-explanation.
        "max_derivations_per_iteration": DEFAULT_MAX_DERIVATIONS_PER_ITERATION,
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
        # Derivation records are an additive contract.  A zero budget
        # opts an iteration out of passage-level justification entirely.
        "max_derivations_per_iteration": (0, MAX_DERIVATIONS_PER_ITERATION),
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


def _empty_anchor(
    authoring_revision: str,
    *,
    authoring_save_sequence: int | None = None,
    authoring_save_event_sha256: str | None = None,
) -> dict[str, Any]:
    if (authoring_save_sequence is None) is not (
        authoring_save_event_sha256 is None
    ):
        raise CreativeWorkflowError("invalid_authoring_causal_anchor")
    result = {
        "authoring_revision": authoring_revision,
        "parent_candidate": None,
        "candidate": None,
    }
    if authoring_save_sequence is not None:
        if (
            isinstance(authoring_save_sequence, bool)
            or not isinstance(authoring_save_sequence, int)
            or not 1
            <= authoring_save_sequence
            <= MAX_AUTHORING_SAVE_SEQUENCE
        ):
            raise CreativeWorkflowError("invalid_authoring_causal_anchor")
        _checked_revision(
            authoring_save_event_sha256,
            code="invalid_authoring_causal_anchor",
        )
        result.update(
            {
                "authoring_save_sequence": authoring_save_sequence,
                "authoring_save_event_sha256": authoring_save_event_sha256,
            }
        )
    return result


def _empty_composition_governance(
    *, enforcement_started_iteration: int = 1
) -> dict[str, Any]:
    """Return the additive whole-work governance ledger.

    The work charter itself remains the immutable genesis document.  Maps,
    preflights and committed amendments are append-only records in this one
    ledger, so rendered candidate history remains the only version tree.
    """

    return {
        "profile": _COMPOSITION_GOVERNANCE_PROFILE,
        "enforcement_started_iteration": enforcement_started_iteration,
        "initial_charter_sha256": None,
        "composition_maps": [],
        "amendment_preflights": [],
        "amendments": [],
    }


def _new_iteration(
    number: int,
    *,
    authoring_revision: str,
    parent_candidate: dict[str, str] | None,
    candidate: dict[str, Any] | None = None,
    opened_at_utc: str,
    authoring_save_sequence: int | None = None,
    authoring_save_event_sha256: str | None = None,
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
            **_empty_anchor(
                authoring_revision,
                authoring_save_sequence=authoring_save_sequence,
                authoring_save_event_sha256=authoring_save_event_sha256,
            ),
            "parent_candidate": copy.deepcopy(parent_candidate),
            "candidate": copy.deepcopy(candidate),
        },
        "reviews": [],
        "evidence": [],
        "exceptions": [],
        "derivations": [],
        "forks": [],
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
        "open_evidence_ids": [],
        "terminated_at_utc": timestamp,
    }


def _initial_state(
    *,
    workflow_id: str,
    project_id: str,
    mode: str,
    authoring_revision: str,
    authoring_save_sequence: int | None,
    authoring_save_event_sha256: str | None,
    budget: dict[str, int],
    final_authority: str,
    timestamp: str,
    composition_governance: bool,
) -> dict[str, Any]:
    disabled = mode == "off"
    state = {
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
        "initial_anchor": _empty_anchor(
            authoring_revision,
            authoring_save_sequence=authoring_save_sequence,
            authoring_save_event_sha256=authoring_save_event_sha256,
        ),
        "budget": budget,
        "usage": {
            "revision_cycles": 0,
            "rollbacks": 0,
            "render_attempts": 0,
            "evidence_items": 0,
            "exceptions": 0,
            "reviews": 0,
            "derivations": 0,
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
        "policy": copy.deepcopy(
            _GOVERNANCE_POLICY if composition_governance else _POLICY
        ),
    }
    if composition_governance:
        state["governance"] = _empty_composition_governance()
    return state


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
    version = document.get("schema_version")
    if (
        set(document) != expected
        or document.get("kind") != WORKFLOW_KIND
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != WORKFLOW_VERSION
    ):
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
    target = _extended_windows_path(
        layout.workflow.path / WORKFLOW_MANIFEST_NAME
    )
    stage = _extended_windows_path(
        layout.workflow.path
        / f".{WORKFLOW_MANIFEST_NAME}.stage-{secrets.token_hex(16)}"
    )
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
        or isinstance(metadata.get("schema_version"), bool)
        or not isinstance(metadata.get("schema_version"), int)
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
    try:
        revalidate_plain_directory(identity)
    except OSError as exc:
        raise CreativeWorkflowError("unsafe_workflow_revision") from exc
    return state


def _publish_revision(layout: _WorkflowLayout, state: dict[str, Any]) -> str:
    _validate_state_document(state)
    _validate_state_derivation_referents(layout.project_root, state)
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


def _raise_charter_amendment_error(exc: CharterAmendmentError) -> None:
    raise CreativeWorkflowError(
        f"charter_amendment.{exc.code}",
        source=exc.source,
        location_segments=exc.location_segments,
    ) from exc


def _raise_composition_map_error(exc: CompositionMapError) -> None:
    details = exc.details if isinstance(exc.details, Mapping) else {}
    path = details.get("path")
    location = (path,) if isinstance(path, str) and path else ()
    raise CreativeWorkflowError(
        f"composition_map.{exc.code}",
        source="composition_map",
        location_segments=location,
    ) from exc


def _governance_enabled_for_iteration(
    state: Mapping[str, Any], iteration_number: int
) -> bool:
    governance = state.get("governance")
    return (
        isinstance(governance, Mapping)
        and isinstance(governance.get("enforcement_started_iteration"), int)
        and not isinstance(governance.get("enforcement_started_iteration"), bool)
        and iteration_number >= governance["enforcement_started_iteration"]
    )


def _core_amendment_entries(
    state: Mapping[str, Any], *, through_iteration: int | None = None
) -> list[dict[str, Any]]:
    governance = state.get("governance")
    if not isinstance(governance, Mapping):
        return []
    result: list[dict[str, Any]] = []
    for wrapper in governance.get("amendments", []):
        if not isinstance(wrapper, Mapping):
            continue
        effective_from = wrapper.get("effective_from_iteration")
        if through_iteration is not None and (
            isinstance(effective_from, bool)
            or not isinstance(effective_from, int)
            or effective_from > through_iteration
        ):
            continue
        entry = wrapper.get("entry")
        if isinstance(entry, Mapping):
            result.append(copy.deepcopy(dict(entry)))
    return result


def _effective_charter_for_iteration(
    state: Mapping[str, Any], iteration_number: int
) -> dict[str, Any]:
    charter = state.get("work_charter")
    if not isinstance(charter, Mapping):
        raise CreativeWorkflowError("work_charter_missing")
    try:
        effective = effective_charter_from_ledger(
            charter,
            _core_amendment_entries(state, through_iteration=iteration_number),
        )
    except CharterAmendmentError as exc:
        _raise_charter_amendment_error(exc)
    normalized = _normalize_work_charter(effective)
    if normalized != effective:
        raise CreativeWorkflowError("invalid_effective_work_charter")
    return normalized


def _current_effective_charter(state: Mapping[str, Any]) -> dict[str, Any]:
    iterations = state.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        raise CreativeWorkflowError("workflow_iteration_missing")
    number = iterations[-1].get("iteration_number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise CreativeWorkflowError("invalid_iteration_identity")
    return _effective_charter_for_iteration(state, number)


def _charter_claim_index(charter: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return index_charter_claims(charter)
    except CharterAmendmentError as exc:
        _raise_charter_amendment_error(exc)


def _charter_claim_ids(charter: Mapping[str, Any]) -> list[str]:
    return [item["claim_id"] for item in _charter_claim_index(charter)["claims"]]


def _composition_map_record(
    state: Mapping[str, Any], iteration_number: int
) -> Mapping[str, Any] | None:
    governance = state.get("governance")
    if not isinstance(governance, Mapping):
        return None
    matches = [
        item
        for item in governance.get("composition_maps", [])
        if isinstance(item, Mapping)
        and item.get("iteration_number") == iteration_number
    ]
    if len(matches) > 1:
        raise CreativeWorkflowError("duplicate_iteration_composition_map")
    return matches[0] if matches else None


def _composition_map_dependencies(
    composition_map: Mapping[str, Any],
) -> list[dict[str, Any]]:
    return [
        {
            "dependency_id": node["node_id"],
            "claim_ids": list(node["depends_on_claim_ids"]),
        }
        for node in composition_map["nodes"]
        if node["depends_on_claim_ids"]
    ]


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
        start is not None
        and end is not None
        and (start < 0.0 or end <= start)
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


def _workflow_record_identity(
    prefix: str,
    *,
    workflow_id: str,
    iteration_number: int,
    body: Mapping[str, Any],
) -> str:
    """Return one content-bound identity inside a workflow iteration."""

    return prefix + canonical_json_sha256(
        {
            "workflow_id": workflow_id,
            "iteration_number": iteration_number,
            **dict(body),
        }
    )[:20]


def _review_identity(
    *, workflow_id: str, iteration_number: int, body: Mapping[str, Any]
) -> str:
    return _workflow_record_identity(
        "review-",
        workflow_id=workflow_id,
        iteration_number=iteration_number,
        body=body,
    )


def _evidence_identity(
    *, workflow_id: str, iteration_number: int, body: Mapping[str, Any]
) -> str:
    return _workflow_record_identity(
        "evidence-",
        workflow_id=workflow_id,
        iteration_number=iteration_number,
        body=body,
    )


def _exception_identity(
    *, workflow_id: str, iteration_number: int, body: Mapping[str, Any]
) -> str:
    return _workflow_record_identity(
        "exception-",
        workflow_id=workflow_id,
        iteration_number=iteration_number,
        body=body,
    )


def _validate_review(
    value: object,
    *,
    workflow_id: str,
    iteration_number: int,
    iteration: dict[str, Any],
) -> None:
    legacy_keys = {
        "review_id",
        "phase",
        "reviewer",
        "perception_basis",
        "summary",
        "candidate_id",
        "reviewed_at_utc",
    }
    governed_keys = legacy_keys | {
        "inspection_sha256",
        "score_sha256",
        "composition_map_sha256",
        "effective_charter_sha256",
        "claim_ids",
        "question_answers",
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_keys),
        frozenset(governed_keys),
    }:
        raise CreativeWorkflowError("invalid_review_record")
    if not isinstance(value["review_id"], str) or re.fullmatch(
        r"review-[0-9a-f]{20}", value["review_id"]
    ) is None:
        raise CreativeWorkflowError("invalid_review_record")
    body = {key: item for key, item in value.items() if key != "review_id"}
    if value["review_id"] != _review_identity(
        workflow_id=workflow_id,
        iteration_number=iteration_number,
        body=body,
    ):
        raise CreativeWorkflowError("review_identity_mismatch")
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
    if "question_answers" in value:
        for field in (
            "inspection_sha256",
            "score_sha256",
            "composition_map_sha256",
            "effective_charter_sha256",
        ):
            _checked_revision(value[field], code="invalid_review_governance_binding")
        claim_ids = value["claim_ids"]
        if (
            not isinstance(claim_ids, list)
            or len(claim_ids) > MAX_REVIEW_CLAIM_REFERENCES
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"claim-[0-9a-f]{64}", item) is None
                for item in claim_ids
            )
            or claim_ids != sorted(set(claim_ids))
        ):
            raise CreativeWorkflowError("invalid_review_question_references")
        answers = value["question_answers"]
        if (
            not isinstance(answers, list)
            or not 1 <= len(answers) <= MAX_REVIEW_QUESTION_ANSWERS
        ):
            raise CreativeWorkflowError("invalid_review_question_answers")
        seen_questions: set[str] = set()
        answer_claim_ids: set[str] = set()
        for index, answer in enumerate(answers):
            if not isinstance(answer, dict) or set(answer) != {
                "question_id",
                "answer",
                "claim_ids",
                "node_ids",
                "event_ids",
            }:
                raise CreativeWorkflowError("invalid_review_question_answers")
            question_id = answer["question_id"]
            if (
                not isinstance(question_id, str)
                or re.fullmatch(
                    r"(?:question|workflow-question)-[0-9a-f]{20}", question_id
                )
                is None
                or question_id in seen_questions
            ):
                raise CreativeWorkflowError("invalid_review_question_answers")
            seen_questions.add(question_id)
            _bounded_text(
                answer["answer"],
                field=f"review.question_answers[{index}].answer",
                maximum_bytes=4096,
            )
            for field, pattern, maximum_items in (
                (
                    "claim_ids",
                    r"claim-[0-9a-f]{64}",
                    MAX_REVIEW_CLAIM_REFERENCES,
                ),
                (
                    "node_ids",
                    r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
                    MAX_REVIEW_NODE_REFERENCES,
                ),
                ("event_ids", None, MAX_REVIEW_EVENT_REFERENCES),
            ):
                references = answer[field]
                if (
                    not isinstance(references, list)
                    or len(references) > maximum_items
                    or len(set(references)) != len(references)
                    or any(
                        not isinstance(item, str)
                        or not item.strip()
                        or (pattern is not None and re.fullmatch(pattern, item) is None)
                        for item in references
                    )
                ):
                    raise CreativeWorkflowError("invalid_review_question_references")
            if not (
                answer["claim_ids"]
                or answer["node_ids"]
                or answer["event_ids"]
            ):
                raise CreativeWorkflowError("review_question_evidence_required")
            answer_claim_ids.update(answer["claim_ids"])
        if claim_ids != sorted(answer_claim_ids):
            raise CreativeWorkflowError("review_claim_coverage_mismatch")
    _canonical_timestamp(value["reviewed_at_utc"], code="invalid_review_timestamp")


def _validate_evidence(
    value: object,
    *,
    workflow_id: str,
    iteration_number: int,
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
    body = {key: item for key, item in value.items() if key != "evidence_id"}
    if value["evidence_id"] != _evidence_identity(
        workflow_id=workflow_id,
        iteration_number=iteration_number,
        body=body,
    ):
        raise CreativeWorkflowError("evidence_identity_mismatch")
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
    if basis["kind"] == "render_measurement" and (
        _candidate_id(iteration) is None
        or scope["candidate_id"] != _candidate_id(iteration)
    ):
        raise CreativeWorkflowError("render_measurement_requires_current_candidate_scope")
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
    workflow_id: str,
    iteration_number: int,
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
    body = {key: item for key, item in value.items() if key != "exception_id"}
    if value["exception_id"] != _exception_identity(
        workflow_id=workflow_id,
        iteration_number=iteration_number,
        body=body,
    ):
        raise CreativeWorkflowError("exception_identity_mismatch")
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
        basis_kind = evidence["basis"]["kind"]
        if basis_kind == "declared_promise" and (
            value["target_type"] != "work_charter"
            or target_ref != evidence["basis"]["reference"]
        ):
            raise CreativeWorkflowError("exception_target_evidence_mismatch")
        if basis_kind == "active_clause" and (
            value["target_type"] != "active_clause"
            or target_ref != evidence["basis"]["reference"]
        ):
            raise CreativeWorkflowError("exception_target_evidence_mismatch")
    if not isinstance(value["reusable"], bool):
        raise CreativeWorkflowError("invalid_exception_record")
    _canonical_timestamp(value["registered_at_utc"], code="invalid_exception_timestamp")


def _derivation_identity(
    *, workflow_id: str, iteration_number: int, body: Mapping[str, Any]
) -> str:
    """Return the content identity of one passage-level derivation."""

    return "derivation-" + canonical_json_sha256(
        {
            "workflow_id": workflow_id,
            "iteration_number": iteration_number,
            **dict(body),
        }
    )[:20]


def _derivation_score_range(
    anchor: Mapping[str, Any],
    *,
    code: str = "invalid_derivation_bar_range",
    field_prefix: str = "derivation",
) -> tuple[int, float, int, float] | None:
    """Validate and return the end-exclusive score range in *anchor*."""

    fields = ("start_bar", "start_beat", "end_bar", "end_beat")
    values = tuple(anchor[field] for field in fields)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise CreativeWorkflowError(code)
    start_bar, start_beat, end_bar, end_beat = values
    for field, value in (("start_bar", start_bar), ("end_bar", end_bar)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise CreativeWorkflowError(
                code,
                location_segments=("anchor", field),
            )
    checked_start_beat = _finite_optional_number(
        start_beat, field=f"{field_prefix}.anchor.start_beat"
    )
    checked_end_beat = _finite_optional_number(
        end_beat, field=f"{field_prefix}.anchor.end_beat"
    )
    if (
        checked_start_beat is None
        or checked_end_beat is None
        or checked_start_beat < 1.0
        or checked_end_beat < 1.0
        or (end_bar, checked_end_beat) <= (start_bar, checked_start_beat)
    ):
        raise CreativeWorkflowError(code)
    return start_bar, checked_start_beat, end_bar, checked_end_beat


def _validate_derivation_premise(
    value: object,
    *,
    index: int,
    iteration: dict[str, Any],
    active_clause_ids: set[str],
    charter_fields: set[str],
) -> None:
    """Structural premise checks; score referents are verified at record time."""

    field = f"derivation.premises[{index}]"
    if not isinstance(value, dict) or set(value) != {
        "kind",
        "reference",
        "event_ids",
        "artifact_sha256",
        "artifact_role",
    }:
        raise CreativeWorkflowError("invalid_derivation_premise")
    kind = value["kind"]
    if kind not in DERIVATION_PREMISE_KINDS:
        raise CreativeWorkflowError("invalid_derivation_premise")
    reference = value["reference"]
    event_ids = value["event_ids"]
    artifact_hash = value["artifact_sha256"]
    artifact_role = value["artifact_role"]
    if not isinstance(event_ids, list) or len(event_ids) > MAX_DERIVATION_MATERIAL_REFS:
        raise CreativeWorkflowError("invalid_derivation_premise")
    for item_index, event_id in enumerate(event_ids):
        if not isinstance(event_id, str) or not event_id.strip():
            raise CreativeWorkflowError("invalid_derivation_premise")
        _bounded_text(
            event_id,
            field=f"{field}.event_ids[{item_index}]",
            maximum_bytes=256,
        )
    if len(set(event_ids)) != len(event_ids):
        raise CreativeWorkflowError("duplicate_derivation_material_reference")
    if (artifact_hash is None) is not (artifact_role is None):
        raise CreativeWorkflowError("derivation_artifact_role_mismatch")
    if artifact_hash is not None:
        if artifact_role not in _MEASUREMENT_ARTIFACT_ROLES:
            raise CreativeWorkflowError("invalid_derivation_artifact_role")
        _checked_revision(artifact_hash, code="invalid_derivation_artifact_hash")
        candidate = iteration["anchor"].get("candidate")
        if not isinstance(candidate, dict):
            raise CreativeWorkflowError("derivation_artifact_requires_candidate")
        expected_hash = candidate[_EVIDENCE_ARTIFACT_FIELDS[artifact_role]]
        if expected_hash is None or artifact_hash != expected_hash:
            raise CreativeWorkflowError("derivation_artifact_binding_mismatch")
    if kind == "declared_promise":
        if reference is None or event_ids or artifact_hash is not None:
            raise CreativeWorkflowError("invalid_derivation_premise")
        _bounded_text(reference, field=f"{field}.reference", maximum_bytes=1024)
        if reference not in charter_fields:
            raise CreativeWorkflowError("derivation_promise_reference_invalid")
    elif kind == "active_clause":
        if reference is None or event_ids or artifact_hash is not None:
            raise CreativeWorkflowError("invalid_derivation_premise")
        _bounded_text(reference, field=f"{field}.reference", maximum_bytes=1024)
        if reference not in active_clause_ids:
            raise CreativeWorkflowError("derivation_clause_not_active")
    elif kind == "established_material":
        if reference is not None or not event_ids or artifact_hash is not None:
            raise CreativeWorkflowError("invalid_derivation_premise")
    else:  # render_measurement
        if reference is None or event_ids or artifact_hash is None:
            raise CreativeWorkflowError("invalid_derivation_premise")
        _bounded_text(reference, field=f"{field}.reference", maximum_bytes=1024)


def _validate_derivation(
    value: object,
    *,
    workflow_id: str,
    iteration_number: int,
    iteration: dict[str, Any],
    active_clause_ids: set[str],
    charter_fields: set[str],
) -> None:
    """Structural derivation checks.

    A derivation is an affirmative, passage-level justification: why the
    anchored material had to be written this way under the charter and the
    active clauses.  Referential checks that need the anchored score
    (event/part identity, score hash) run at record time; this validator
    re-checks shape, bounds and in-state references on every read.
    """

    legacy_keys = {
        "derivation_id",
        "anchor",
        "claim",
        "premises",
        "clause_ids",
        "excluded_alternatives",
        "sacrificed_values",
        "recorded_at_utc",
    }
    governed_keys = legacy_keys | {
        "effective_charter_sha256",
        "charter_claim_ids",
        "composition_map_sha256",
        "composition_map_node_ids",
        "question_ids",
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(legacy_keys),
        frozenset(governed_keys),
    }:
        raise CreativeWorkflowError("invalid_derivation_record")
    if not isinstance(value["derivation_id"], str) or re.fullmatch(
        r"derivation-[0-9a-f]{20}", value["derivation_id"]
    ) is None:
        raise CreativeWorkflowError("invalid_derivation_record")
    body = {key: item for key, item in value.items() if key != "derivation_id"}
    if value["derivation_id"] != _derivation_identity(
        workflow_id=workflow_id,
        iteration_number=iteration_number,
        body=body,
    ):
        raise CreativeWorkflowError("derivation_identity_mismatch")
    anchor = value["anchor"]
    if not isinstance(anchor, dict) or set(anchor) != {
        "authoring_revision",
        "candidate_id",
        "score_sha256",
        "event_ids",
        "part_ids",
        "start_bar",
        "start_beat",
        "end_bar",
        "end_beat",
        "start_seconds",
        "end_seconds",
    }:
        raise CreativeWorkflowError("invalid_derivation_anchor")
    if anchor["authoring_revision"] != iteration["anchor"]["authoring_revision"]:
        raise CreativeWorkflowError("derivation_revision_mismatch")
    if anchor["candidate_id"] not in {None, _candidate_id(iteration)}:
        raise CreativeWorkflowError("derivation_candidate_mismatch")
    _checked_revision(anchor["score_sha256"], code="invalid_derivation_score_hash")
    if not isinstance(anchor["event_ids"], list) or len(anchor["event_ids"]) > 128:
        raise CreativeWorkflowError("invalid_derivation_anchor")
    for item_index, event_id in enumerate(anchor["event_ids"]):
        if not isinstance(event_id, str) or not event_id.strip():
            raise CreativeWorkflowError("invalid_derivation_anchor")
        _bounded_text(
            event_id,
            field=f"derivation.anchor.event_ids[{item_index}]",
            maximum_bytes=256,
        )
    if len(set(anchor["event_ids"])) != len(anchor["event_ids"]):
        raise CreativeWorkflowError("duplicate_derivation_anchor_reference")
    if not isinstance(anchor["part_ids"], list) or len(anchor["part_ids"]) > 64:
        raise CreativeWorkflowError("invalid_derivation_anchor")
    for item_index, part_id in enumerate(anchor["part_ids"]):
        if not isinstance(part_id, str) or not part_id.strip():
            raise CreativeWorkflowError("invalid_derivation_anchor")
        _bounded_text(
            part_id,
            field=f"derivation.anchor.part_ids[{item_index}]",
            maximum_bytes=256,
        )
    if len(set(anchor["part_ids"])) != len(anchor["part_ids"]):
        raise CreativeWorkflowError("duplicate_derivation_anchor_reference")
    score_range = _derivation_score_range(anchor)
    start = _finite_optional_number(
        anchor["start_seconds"], field="derivation.anchor.start_seconds"
    )
    end = _finite_optional_number(
        anchor["end_seconds"], field="derivation.anchor.end_seconds"
    )
    if (start is None) != (end is None) or (
        start is not None and end is not None and end <= start
    ) or (start is not None and start < 0.0
    ):
        raise CreativeWorkflowError("invalid_derivation_time_range")
    if start is not None and anchor["candidate_id"] is None:
        raise CreativeWorkflowError("derivation_time_scope_requires_candidate")
    # A part identifies a voice, not a passage.  It may refine event/range
    # scope, but cannot make an entire part masquerade as a local derivation.
    if not anchor["event_ids"] and score_range is None:
        raise CreativeWorkflowError(
            "derivation_anchor_requires_event_or_bar_range"
        )
    _bounded_text(value["claim"], field="derivation.claim", maximum_bytes=4096)
    premises = value["premises"]
    if (
        not isinstance(premises, list)
        or not premises
        or len(premises) > MAX_DERIVATION_PREMISES
    ):
        raise CreativeWorkflowError("derivation_premise_required")
    for index, premise in enumerate(premises):
        _validate_derivation_premise(
            premise,
            index=index,
            iteration=iteration,
            active_clause_ids=active_clause_ids,
            charter_fields=charter_fields,
        )
    clause_ids = value["clause_ids"]
    if (
        not isinstance(clause_ids, list)
        or len(clause_ids) > MAX_DERIVATION_CLAUSE_REFS
    ):
        raise CreativeWorkflowError("invalid_derivation_clause_references")
    if len(set(clause_ids)) != len(clause_ids):
        raise CreativeWorkflowError("duplicate_derivation_clause_reference")
    for clause_id in clause_ids:
        if (
            not isinstance(clause_id, str)
            or _CLAUSE_ID.fullmatch(clause_id) is None
            or clause_id not in active_clause_ids
        ):
            raise CreativeWorkflowError("derivation_clause_not_active")
    premise_clause_ids = [
        premise["reference"]
        for premise in premises
        if premise["kind"] == "active_clause"
    ]
    if premise_clause_ids != clause_ids:
        raise CreativeWorkflowError("derivation_clause_reference_mismatch")
    alternatives = value["excluded_alternatives"]
    if (
        not isinstance(alternatives, list)
        or not alternatives
        or len(alternatives) > MAX_DERIVATION_ALTERNATIVES
    ):
        raise CreativeWorkflowError("derivation_alternatives_required")
    for index, alternative in enumerate(alternatives):
        if not isinstance(alternative, dict) or set(alternative) != {
            "alternative",
            "failure",
            "premise_indexes",
        }:
            raise CreativeWorkflowError("invalid_derivation_alternative")
        _bounded_text(
            alternative["alternative"],
            field=f"derivation.excluded_alternatives[{index}].alternative",
            maximum_bytes=2048,
        )
        _bounded_text(
            alternative["failure"],
            field=f"derivation.excluded_alternatives[{index}].failure",
            maximum_bytes=2048,
        )
        premise_indexes = alternative["premise_indexes"]
        if not isinstance(premise_indexes, list) or not premise_indexes:
            raise CreativeWorkflowError(
                "derivation_alternative_premise_required"
            )
        if len(set(premise_indexes)) != len(premise_indexes):
            raise CreativeWorkflowError(
                "duplicate_derivation_alternative_premise_reference"
            )
        if len(premise_indexes) > len(premises) or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 0 <= item < len(premises)
            for item in premise_indexes
        ):
            raise CreativeWorkflowError(
                "derivation_alternative_premise_not_found"
            )
    _bounded_text_list(
        value["sacrificed_values"],
        field="derivation.sacrificed_values",
        maximum_items=32,
    )
    if "effective_charter_sha256" in value:
        _checked_revision(
            value["effective_charter_sha256"],
            code="invalid_derivation_charter_hash",
        )
        _checked_revision(
            value["composition_map_sha256"],
            code="invalid_derivation_composition_map_hash",
        )
        for field, pattern, maximum in (
            ("charter_claim_ids", r"claim-[0-9a-f]{64}", 128),
            (
                "composition_map_node_ids",
                r"[a-z0-9]+(?:[._-][a-z0-9]+)*",
                128,
            ),
            (
                "question_ids",
                r"(?:question|workflow-question)-[0-9a-f]{20}",
                MAX_REVIEW_QUESTION_ANSWERS,
            ),
        ):
            references = value[field]
            if (
                not isinstance(references, list)
                or not references
                or len(references) > maximum
                or len(set(references)) != len(references)
                or any(
                    not isinstance(item, str)
                    or re.fullmatch(pattern, item) is None
                    for item in references
                )
            ):
                raise CreativeWorkflowError("invalid_derivation_governance_references")
    _canonical_timestamp(value["recorded_at_utc"], code="invalid_derivation_timestamp")


@dataclass(frozen=True, slots=True)
class _DerivationScoreIndex:
    score_sha256: str
    score: Any
    part_ids: frozenset[str]
    event_positions: Mapping[str, tuple[str, int, float, float]]
    score_end_quarter: float


def _build_derivation_score_index(
    score_document: Mapping[str, Any],
) -> _DerivationScoreIndex:
    try:
        score = parse_score_document(score_document)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise CreativeWorkflowError("derivation_score_unavailable") from exc
    event_positions: dict[str, tuple[str, int, float, float]] = {}
    score_end = 0.0
    for part in score.parts:
        for note in part.notes:
            start = score.tempo_map.quarter_at(note.bar, note.beat)
            meter = score.tempo_map.meter_entry_at_bar(note.bar)
            end = start + note.duration_beats * meter.quarters_per_beat
            score_end = max(score_end, end)
            if note.source_event_id is not None:
                event_positions[note.source_event_id] = (
                    part.id,
                    note.bar,
                    note.beat,
                    start,
                )
    return _DerivationScoreIndex(
        score_sha256=canonical_json_sha256(score_document),
        score=score,
        part_ids=frozenset(part.id for part in score.parts),
        event_positions=event_positions,
        score_end_quarter=score_end,
    )


def _score_range_quarters(
    index: _DerivationScoreIndex,
    score_range: tuple[int, float, int, float],
    *,
    bar_range_code: str = "invalid_derivation_bar_range",
    bounds_code: str = "derivation_score_range_out_of_bounds",
) -> tuple[float, float]:
    start_bar, start_beat, end_bar, end_beat = score_range
    for bar, beat in ((start_bar, start_beat), (end_bar, end_beat)):
        try:
            meter = index.score.tempo_map.meter_entry_at_bar(bar)
        except (IndexError, ValueError) as exc:
            raise CreativeWorkflowError(bar_range_code) from exc
        # A bar boundary is expressed as the following bar at beat 1, rather
        # than as a non-canonical beats_per_bar + 1 spelling.
        if beat >= float(meter.beats_per_bar) + 1.0:
            raise CreativeWorkflowError(bar_range_code)
    try:
        start = index.score.tempo_map.quarter_at(start_bar, start_beat)
        end = index.score.tempo_map.quarter_at(end_bar, end_beat)
    except (IndexError, ValueError) as exc:
        raise CreativeWorkflowError(bar_range_code) from exc
    if end <= start or end > index.score_end_quarter + 1e-9:
        raise CreativeWorkflowError(bounds_code)
    return start, end


def _validate_derivation_score_referents(
    derivation: Mapping[str, Any],
    *,
    index: _DerivationScoreIndex,
) -> None:
    anchor = derivation["anchor"]
    if anchor["score_sha256"] != index.score_sha256:
        raise CreativeWorkflowError("derivation_score_hash_mismatch")
    selected_parts = set(anchor["part_ids"])
    if not selected_parts.issubset(index.part_ids):
        raise CreativeWorkflowError("derivation_part_not_found")
    score_range = _derivation_score_range(anchor)
    range_quarters = (
        None if score_range is None else _score_range_quarters(index, score_range)
    )
    anchor_positions: list[float] = []
    for event_id in anchor["event_ids"]:
        event = index.event_positions.get(event_id)
        if event is None:
            raise CreativeWorkflowError("derivation_event_not_found")
        part_id, _bar, _beat, position = event
        if selected_parts and part_id not in selected_parts:
            raise CreativeWorkflowError("derivation_event_part_mismatch")
        if range_quarters is not None and not (
            range_quarters[0] <= position < range_quarters[1]
        ):
            raise CreativeWorkflowError("derivation_event_range_mismatch")
        anchor_positions.append(position)
    target_start = (
        range_quarters[0]
        if range_quarters is not None
        else min(anchor_positions)
    )
    for premise in derivation["premises"]:
        if premise["kind"] != "established_material":
            continue
        for event_id in premise["event_ids"]:
            event = index.event_positions.get(event_id)
            if event is None:
                raise CreativeWorkflowError(
                    "derivation_material_event_not_found"
                )
            if not event[3] < target_start:
                raise CreativeWorkflowError(
                    "derivation_material_not_preceding_anchor"
                )


def _validate_evidence_score_referents(
    evidence: Mapping[str, Any],
    *,
    index: _DerivationScoreIndex,
) -> None:
    """Re-prove score-local evidence scope against its immutable revision."""

    scope = evidence["scope"]
    selected_parts = set(scope["part_ids"])
    if not selected_parts.issubset(index.part_ids):
        raise CreativeWorkflowError("evidence_part_not_found")
    for event_id in scope["event_ids"]:
        event = index.event_positions.get(event_id)
        if event is None:
            raise CreativeWorkflowError("evidence_event_not_found")
        if selected_parts and event[0] not in selected_parts:
            raise CreativeWorkflowError("evidence_event_part_mismatch")


def _validate_state_derivation_referents(
    project_root: Path,
    state: Mapping[str, Any],
    *,
    score_cache: dict[str, _DerivationScoreIndex] | None = None,
    score_document_cache: dict[str, Mapping[str, Any]] | None = None,
    validated_derivations: dict[str, str] | None = None,
    validated_evidence: dict[str, str] | None = None,
    validated_forks: dict[str, str] | None = None,
) -> None:
    """Re-open immutable authoring revisions and re-prove score referents."""

    _validate_state_revision_contract_referents(project_root, state)

    cache = {} if score_cache is None else score_cache
    documents = {} if score_document_cache is None else score_document_cache
    seen = {} if validated_derivations is None else validated_derivations
    seen_evidence = {} if validated_evidence is None else validated_evidence
    seen_forks = {} if validated_forks is None else validated_forks
    referenced_revisions = {state["initial_anchor"]["authoring_revision"]}
    for iteration in state["iterations"]:
        referenced_revisions.add(iteration["anchor"]["authoring_revision"])
        if iteration["next_authoring_revision"] is not None:
            referenced_revisions.add(iteration["next_authoring_revision"])
    for authoring_revision in referenced_revisions:
        if authoring_revision in documents:
            continue
        try:
            authoring = open_authoring_project(
                project_root, revision=authoring_revision
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError(
                "workflow_authoring_revision_unavailable"
            ) from exc
        if authoring.project_id != state["project_id"]:
            raise CreativeWorkflowError("authoring_project_identity_changed")
        documents[authoring_revision] = authoring.documents["score"]
    causal_anchors = [state["initial_anchor"]]
    causal_anchors.extend(
        iteration["anchor"] for iteration in state["iterations"]
    )
    for anchor in causal_anchors:
        if "authoring_save_event_sha256" not in anchor:
            continue
        try:
            verify_authoring_save_event_binding(
                project_root,
                event_sha256=anchor["authoring_save_event_sha256"],
                project_id=state["project_id"],
                revision=anchor["authoring_revision"],
                save_sequence=anchor["authoring_save_sequence"],
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError(
                "workflow_authoring_anchor_causality_invalid"
            ) from exc
    governance = state.get("governance")
    amendment_records = (
        governance.get("amendments", [])
        if isinstance(governance, Mapping)
        else []
    )
    for amendment in amendment_records:
        causal_fence = amendment.get("authoring_causal_fence")
        if not isinstance(causal_fence, Mapping):
            # This is a read-compatibility path for histories committed before
            # authoring causal fences existed.  New transitions cannot append
            # such a wrapper and cannot advance it to a revised score.
            continue
        if causal_fence["save_sequence"] > 0:
            try:
                verify_authoring_save_event_binding(
                    project_root,
                    event_sha256=causal_fence[
                        "anchor_save_event_sha256"
                    ],
                    project_id=state["project_id"],
                    revision=causal_fence["anchor_revision"],
                    save_sequence=causal_fence["save_sequence"],
                )
            except AuthoringProjectError as exc:
                raise CreativeWorkflowError(
                    "workflow_authoring_revision_causality_invalid"
                ) from exc
        committed_in_iteration = amendment["committed_in_iteration"]
        if (
            isinstance(committed_in_iteration, bool)
            or not isinstance(committed_in_iteration, int)
            or committed_in_iteration < 1
            or committed_in_iteration > len(state["iterations"])
        ):
            raise CreativeWorkflowError("invalid_charter_amendment_ledger")
        revised_to = state["iterations"][committed_in_iteration - 1][
            "next_authoring_revision"
        ]
        if revised_to is None:
            continue
        if committed_in_iteration >= len(state["iterations"]):
            raise CreativeWorkflowError(
                "workflow_authoring_revision_causality_invalid"
            )
        next_anchor = state["iterations"][committed_in_iteration]["anchor"]
        if (
            next_anchor["authoring_revision"] != revised_to
            or "authoring_save_sequence" not in next_anchor
            or "authoring_save_event_sha256" not in next_anchor
        ):
            raise CreativeWorkflowError(
                "workflow_authoring_revision_causality_invalid"
            )
        try:
            revised_authoring = open_authoring_project(
                project_root,
                revision=revised_to,
            )
            if (
                revised_authoring.revision_first_save_sequence is None
                or revised_authoring.revision_first_save_sequence
                <= causal_fence["save_sequence"]
            ):
                raise CreativeWorkflowError(
                    "workflow_authoring_revision_causality_invalid"
                )
            ancestry = verify_authoring_revision_ancestry(
                project_root,
                descendant_revision=revised_to,
                ancestor_revision=causal_fence["anchor_revision"],
                descendant_save_event_sha256=next_anchor[
                    "authoring_save_event_sha256"
                ],
                ancestor_save_event_sha256=causal_fence[
                    "anchor_save_event_sha256"
                ],
                minimum_exclusive_save_sequence=causal_fence[
                    "save_sequence"
                ],
                require_current_head=False,
            )
            if (
                ancestry["descendant_save_sequence"]
                != next_anchor["authoring_save_sequence"]
            ):
                raise CreativeWorkflowError(
                    "workflow_authoring_revision_causality_invalid"
                )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError(
                "workflow_authoring_revision_causality_invalid"
            ) from exc
    recorded_candidates: set[tuple[str, str, str]] = set()
    for iteration in state["iterations"]:
        authoring_revision = iteration["anchor"]["authoring_revision"]
        number = iteration["iteration_number"]
        map_record = _composition_map_record(state, number)
        if map_record is not None:
            charter = _effective_charter_for_iteration(state, number)
            try:
                inspection = inspect_composition_map(
                    documents[authoring_revision],
                    map_record["document"],
                    _charter_claim_ids(charter),
                )
            except CompositionMapError as exc:
                _raise_composition_map_error(exc)
            if (
                inspection["score_sha256"] != map_record["score_sha256"]
                or inspection["composition_map_sha256"]
                != map_record["composition_map_sha256"]
            ):
                raise CreativeWorkflowError(
                    "composition_map_score_binding_mismatch"
                )
            for review in iteration["reviews"]:
                if "question_answers" not in review:
                    if (
                        _governance_enabled_for_iteration(state, number)
                        and review["phase"] in _GOVERNANCE_REVIEW_PHASES
                    ):
                        raise CreativeWorkflowError(
                            "governed_review_questions_missing"
                        )
                    continue
                context = _governed_review_context(
                    state,
                    iteration=iteration,
                    score_document=documents[authoring_revision],
                    phase=review["phase"],
                )
                normalized_answers, covered_claim_ids = (
                    _normalize_review_question_answers(
                        review["question_answers"],
                        context=context,
                    )
                )
                if (
                    review["inspection_sha256"]
                    != context["inspection_sha256"]
                    or review["score_sha256"] != context["score_sha256"]
                    or review["composition_map_sha256"]
                    != context["composition_map_sha256"]
                    or review["effective_charter_sha256"]
                    != context["effective_charter_sha256"]
                    or review["question_answers"] != normalized_answers
                    or review["claim_ids"] != covered_claim_ids
                ):
                    raise CreativeWorkflowError(
                        "review_governance_binding_mismatch"
                    )
        elif _governance_enabled_for_iteration(state, number) and (
            any(
                review["phase"] in _GOVERNANCE_REVIEW_PHASES
                for review in iteration["reviews"]
            )
            or iteration["render_attempts"]
        ):
            raise CreativeWorkflowError("composition_map_required_for_iteration_work")
        scoped_evidence = [
            item
            for item in iteration["evidence"]
            if item["scope"]["event_ids"] or item["scope"]["part_ids"]
        ]
        if scoped_evidence:
            index = cache.get(authoring_revision)
            if index is None:
                index = _build_derivation_score_index(
                    documents[authoring_revision]
                )
                cache[authoring_revision] = index
            for evidence in scoped_evidence:
                evidence_id = evidence["evidence_id"]
                body_hash = canonical_json_sha256(evidence)
                previous = seen_evidence.get(evidence_id)
                if previous is not None:
                    if previous != body_hash:
                        raise CreativeWorkflowError("evidence_identity_collision")
                    continue
                _validate_evidence_score_referents(evidence, index=index)
                seen_evidence[evidence_id] = body_hash
        for derivation in iteration.get("derivations", []):
            derivation_id = derivation["derivation_id"]
            body_hash = canonical_json_sha256(derivation)
            previous = seen.get(derivation_id)
            if previous is not None:
                if previous != body_hash:
                    raise CreativeWorkflowError("derivation_identity_collision")
                continue
            authoring_revision = derivation["anchor"]["authoring_revision"]
            index = cache.get(authoring_revision)
            if index is None:
                index = _build_derivation_score_index(
                    documents[authoring_revision]
                )
                cache[authoring_revision] = index
            _validate_derivation_score_referents(derivation, index=index)
            if _governance_enabled_for_iteration(state, number):
                if "effective_charter_sha256" not in derivation:
                    raise CreativeWorkflowError(
                        "governed_derivation_binding_missing"
                    )
                charter = _effective_charter_for_iteration(state, number)
                map_record = _composition_map_record(state, number)
                if map_record is None:
                    raise CreativeWorkflowError(
                        "composition_map_required_for_iteration_work"
                    )
                available_questions = {
                    answer["question_id"]
                    for review in iteration["reviews"]
                    if "question_answers" in review
                    for answer in review["question_answers"]
                }
                if (
                    derivation["effective_charter_sha256"]
                    != canonical_json_sha256(charter)
                    or derivation["composition_map_sha256"]
                    != map_record["composition_map_sha256"]
                    or not set(derivation["charter_claim_ids"]).issubset(
                        _charter_claim_ids(charter)
                    )
                    or not set(
                        derivation["composition_map_node_ids"]
                    ).issubset(
                        {
                            node["node_id"]
                            for node in map_record["document"]["nodes"]
                        }
                    )
                    or not set(derivation["question_ids"]).issubset(
                        available_questions
                    )
                ):
                    raise CreativeWorkflowError(
                        "derivation_governance_binding_mismatch"
                    )
                _validate_derivation_governance_reference_scope(
                    derivation,
                    charter=charter,
                    composition_map=map_record["document"],
                    iteration=iteration,
                )
            seen[derivation_id] = body_hash

        candidate = iteration["anchor"].get("candidate")
        current_locator: tuple[str, str, str] | None = None
        if isinstance(candidate, dict):
            locator = _anchor_locator(candidate)
            assert locator is not None
            current_locator = (
                locator["work_id"],
                locator["candidate_id"],
                locator["manifest_sha256"],
            )
            recorded_candidates.add(current_locator)

        for fork in iteration.get("forks", []):
            _validate_fork_candidate_referents(
                fork,
                recorded_candidates=recorded_candidates,
                current_candidate=current_locator,
            )
            fork_id = fork["fork_id"]
            body_hash = canonical_json_sha256(fork)
            previous = seen_forks.get(fork_id)
            if previous is not None:
                if previous != body_hash:
                    raise CreativeWorkflowError("fork_identity_collision")
                continue
            index = cache.get(authoring_revision)
            if index is None:
                index = _build_derivation_score_index(
                    documents[authoring_revision]
                )
                cache[authoring_revision] = index
            _validate_fork_score_referents(fork, index=index)
            seen_forks[fork_id] = body_hash

        decision = iteration.get("decision")
        if isinstance(decision, dict) and decision.get("charter_settlement"):
            index = cache.get(authoring_revision)
            if index is None:
                index = _build_derivation_score_index(
                    documents[authoring_revision]
                )
                cache[authoring_revision] = index
            _validate_charter_settlement_score_referents(
                decision, index=index
            )


def _fork_identity(
    *, workflow_id: str, iteration_number: int, body: Mapping[str, Any]
) -> str:
    """Return the content identity of one whole-work fork declaration."""

    return "fork-" + canonical_json_sha256(
        {
            "workflow_id": workflow_id,
            "iteration_number": iteration_number,
            **dict(body),
        }
    )[:20]


def _validate_fork_score_referents(
    fork: Mapping[str, Any],
    *,
    index: _DerivationScoreIndex,
) -> None:
    """Re-prove a fork anchor against its immutable authoring score."""

    anchor = fork["anchor"]
    if anchor["score_sha256"] != index.score_sha256:
        raise CreativeWorkflowError("fork_score_hash_mismatch")
    selected_parts = set(anchor["part_ids"])
    if not selected_parts.issubset(index.part_ids):
        raise CreativeWorkflowError("fork_part_not_found")
    score_range = _derivation_score_range(
        anchor, code="invalid_fork_bar_range", field_prefix="fork"
    )
    range_quarters = (
        None
        if score_range is None
        else _score_range_quarters(
            index,
            score_range,
            bar_range_code="invalid_fork_bar_range",
            bounds_code="fork_score_range_out_of_bounds",
        )
    )
    if anchor["event_ids"] and not index.score.has_stable_event_identity:
        raise CreativeWorkflowError("fork_requires_stable_event_identity")
    for event_id in anchor["event_ids"]:
        event = index.event_positions.get(event_id)
        if event is None:
            raise CreativeWorkflowError("fork_event_not_found")
        if selected_parts and event[0] not in selected_parts:
            raise CreativeWorkflowError("fork_event_part_mismatch")
        if range_quarters is not None and not (
            range_quarters[0] <= event[3] < range_quarters[1]
        ):
            raise CreativeWorkflowError("fork_event_range_mismatch")


def _validate_fork_candidate_referents(
    fork: Mapping[str, Any],
    *,
    recorded_candidates: set[tuple[str, str, str]],
    current_candidate: tuple[str, str, str] | None,
) -> None:
    """Bind every fork world to candidates already recorded in this chain."""

    branch_candidates = {
        (
            branch["candidate"]["work_id"],
            branch["candidate"]["candidate_id"],
            branch["candidate"]["manifest_sha256"],
        )
        for branch in fork["branches"]
    }
    if not branch_candidates.issubset(recorded_candidates):
        raise CreativeWorkflowError("fork_branch_candidate_not_recorded")
    if current_candidate is None or current_candidate not in branch_candidates:
        raise CreativeWorkflowError("fork_current_candidate_required")


def _validate_charter_settlement_score_referents(
    decision: Mapping[str, Any],
    *,
    index: _DerivationScoreIndex,
) -> None:
    """Re-prove settlement event references against the decided revision."""

    event_ids = [
        event_id
        for item in decision.get("charter_settlement", [])
        for event_id in item["event_ids"]
    ]
    if event_ids and not index.score.has_stable_event_identity:
        raise CreativeWorkflowError(
            "charter_settlement_requires_stable_event_identity"
        )
    for event_id in event_ids:
        if event_id not in index.event_positions:
            raise CreativeWorkflowError("charter_settlement_event_not_found")


def _validate_fork(
    value: object,
    *,
    workflow_id: str,
    iteration: dict[str, Any],
    derivation_ids_available: set[str],
    invariant_count: int | None,
) -> None:
    """Structural fork checks.

    A fork declares that two or more complete candidates are variant worlds
    of the same work: one possibility is always one whole piece, never a
    swappable fragment.  Score referents and recorded-candidate matching run
    at record time; this validator re-checks shape, bounds and in-state
    references on every read.
    """

    if not isinstance(value, dict) or set(value) != {
        "fork_id",
        "anchor",
        "invariant_indexes",
        "branches",
        "note",
        "recorded_at_utc",
    }:
        raise CreativeWorkflowError("invalid_fork_record")
    if not isinstance(value["fork_id"], str) or re.fullmatch(
        r"fork-[0-9a-f]{20}", value["fork_id"]
    ) is None:
        raise CreativeWorkflowError("invalid_fork_record")
    body = {key: item for key, item in value.items() if key != "fork_id"}
    if value["fork_id"] != _fork_identity(
        workflow_id=workflow_id,
        iteration_number=iteration["iteration_number"],
        body=body,
    ):
        raise CreativeWorkflowError("fork_identity_mismatch")
    anchor = value["anchor"]
    if not isinstance(anchor, dict) or set(anchor) != {
        "authoring_revision",
        "score_sha256",
        "event_ids",
        "part_ids",
        "start_bar",
        "start_beat",
        "end_bar",
        "end_beat",
    }:
        raise CreativeWorkflowError("invalid_fork_anchor")
    if anchor["authoring_revision"] != iteration["anchor"]["authoring_revision"]:
        raise CreativeWorkflowError("fork_revision_mismatch")
    _checked_revision(anchor["score_sha256"], code="invalid_fork_score_hash")
    if not isinstance(anchor["event_ids"], list) or len(anchor["event_ids"]) > 128:
        raise CreativeWorkflowError("invalid_fork_anchor")
    for item_index, event_id in enumerate(anchor["event_ids"]):
        if not isinstance(event_id, str) or not event_id.strip():
            raise CreativeWorkflowError("invalid_fork_anchor")
        _bounded_text(
            event_id,
            field=f"fork.anchor.event_ids[{item_index}]",
            maximum_bytes=256,
        )
    if len(set(anchor["event_ids"])) != len(anchor["event_ids"]):
        raise CreativeWorkflowError("duplicate_fork_anchor_reference")
    if not isinstance(anchor["part_ids"], list) or len(anchor["part_ids"]) > 64:
        raise CreativeWorkflowError("invalid_fork_anchor")
    for item_index, part_id in enumerate(anchor["part_ids"]):
        if not isinstance(part_id, str) or not part_id.strip():
            raise CreativeWorkflowError("invalid_fork_anchor")
        _bounded_text(
            part_id,
            field=f"fork.anchor.part_ids[{item_index}]",
            maximum_bytes=256,
        )
    if len(set(anchor["part_ids"])) != len(anchor["part_ids"]):
        raise CreativeWorkflowError("duplicate_fork_anchor_reference")
    score_range = _derivation_score_range(
        anchor, code="invalid_fork_bar_range", field_prefix="fork"
    )
    if not anchor["event_ids"] and score_range is None:
        raise CreativeWorkflowError("fork_anchor_empty")
    indexes = value["invariant_indexes"]
    if not isinstance(indexes, list) or not indexes or len(indexes) > 16:
        raise CreativeWorkflowError("fork_invariants_required")
    for index in indexes:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise CreativeWorkflowError("invalid_fork_invariant_index")
        if invariant_count is not None and index >= invariant_count:
            raise CreativeWorkflowError("fork_invariant_index_out_of_range")
    if len(set(indexes)) != len(indexes):
        raise CreativeWorkflowError("duplicate_fork_invariant_index")
    branches = value["branches"]
    if (
        not isinstance(branches, list)
        or len(branches) < 2
        or len(branches) > MAX_FORK_BRANCHES
    ):
        raise CreativeWorkflowError("fork_branches_required")
    seen_candidates: set[tuple[str, str, str]] = set()
    for index, branch in enumerate(branches):
        field = f"fork.branches[{index}]"
        if not isinstance(branch, dict) or set(branch) != {
            "candidate",
            "stance",
            "derivation_ids",
        }:
            raise CreativeWorkflowError("invalid_fork_branch")
        _validate_candidate_locator(branch["candidate"])
        locator = (
            branch["candidate"]["work_id"],
            branch["candidate"]["candidate_id"],
            branch["candidate"]["manifest_sha256"],
        )
        if locator in seen_candidates:
            raise CreativeWorkflowError("duplicate_fork_branch_candidate")
        seen_candidates.add(locator)
        _bounded_text(branch["stance"], field=f"{field}.stance", maximum_bytes=2048)
        branch_derivations = branch["derivation_ids"]
        if (
            not isinstance(branch_derivations, list)
            or len(branch_derivations) > MAX_FORK_DERIVATION_REFS
            or any(not isinstance(item, str) for item in branch_derivations)
            or len(set(branch_derivations)) != len(branch_derivations)
            or not set(branch_derivations).issubset(derivation_ids_available)
        ):
            raise CreativeWorkflowError("fork_derivation_not_found")
    note = value["note"]
    if note is not None:
        _bounded_text(note, field="fork.note", maximum_bytes=4096)
    _canonical_timestamp(value["recorded_at_utc"], code="invalid_fork_timestamp")


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
    requested_at = _canonical_timestamp(
        value["requested_at_utc"], code="invalid_render_timestamp"
    )
    if requested_at < iteration["opened_at_utc"]:
        raise CreativeWorkflowError("invalid_render_timestamp")
    if value["finished_at_utc"] is not None:
        finished_at = _canonical_timestamp(
            value["finished_at_utc"], code="invalid_render_timestamp"
        )
        if finished_at < requested_at:
            raise CreativeWorkflowError("invalid_render_timestamp")


def _validate_revision_json_pointer(
    value: str, *, document_name: str
) -> None:
    if (
        not value
        or not value.startswith("/")
        or len(value.encode("utf-8")) > 1024
        or re.search(r"~(?:[^01]|$)", value) is not None
    ):
        raise CreativeWorkflowError("invalid_revision_document_path")
    tokens = [
        token.replace("~1", "/").replace("~0", "~")
        for token in value.split("/")[1:]
    ]
    if (
        document_name == "score"
        and len(tokens) >= 3
        and tokens[0] == "parts"
        and tokens[1].isdigit()
        and tokens[2] == "notes"
    ):
        raise CreativeWorkflowError("score_note_path_must_use_event_scope")


def _normalize_revision_scope(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "change_scale",
        "documents",
        "allowed_document_paths",
        "score",
        "whole_work_cost",
    }:
        raise CreativeWorkflowError("invalid_revision_scope")
    change_scale = value["change_scale"]
    if change_scale not in {"bounded", "whole_work"}:
        raise CreativeWorkflowError("invalid_revision_scope")
    documents = value["documents"]
    if (
        not isinstance(documents, (list, tuple))
        or not documents
        or any(not isinstance(item, str) for item in documents)
        or len(set(documents)) != len(documents)
        or not set(documents).issubset(_REVISION_DOCUMENTS)
    ):
        raise CreativeWorkflowError("invalid_revision_scope")
    checked_documents = sorted(documents)
    score_scope = value["score"]
    whole_work_cost = value["whole_work_cost"]
    allowed_document_paths = value["allowed_document_paths"]
    if change_scale == "whole_work":
        if score_scope is not None or allowed_document_paths is not None:
            raise CreativeWorkflowError("invalid_revision_scope")
        if not isinstance(whole_work_cost, Mapping) or set(whole_work_cost) != {
            "accepted_costs", "rationale"
        }:
            raise CreativeWorkflowError("whole_work_cost_acknowledgement_required")
        accepted = whole_work_cost["accepted_costs"]
        if (
            not isinstance(accepted, (list, tuple))
            or any(not isinstance(item, str) for item in accepted)
            or set(accepted) != _WHOLE_WORK_COSTS
            or len(accepted) != len(_WHOLE_WORK_COSTS)
        ):
            raise CreativeWorkflowError("whole_work_cost_acknowledgement_required")
        return {
            "change_scale": change_scale,
            "documents": checked_documents,
            "allowed_document_paths": None,
            "score": None,
            "whole_work_cost": {
                "accepted_costs": sorted(accepted),
                "rationale": _bounded_text(
                    whole_work_cost["rationale"],
                    field="revision_scope.whole_work_cost.rationale",
                    maximum_bytes=4096,
                ),
            },
        }
    if whole_work_cost is not None:
        raise CreativeWorkflowError("invalid_revision_scope")
    if (
        not isinstance(allowed_document_paths, Mapping)
        or set(allowed_document_paths) != set(checked_documents)
    ):
        raise CreativeWorkflowError("invalid_revision_document_paths")
    checked_document_paths: dict[str, list[str]] = {}
    for document_name in checked_documents:
        raw_paths = allowed_document_paths[document_name]
        if (
            not isinstance(raw_paths, (list, tuple))
            or len(raw_paths) > 1024
            or any(not isinstance(item, str) for item in raw_paths)
            or len(set(raw_paths)) != len(raw_paths)
        ):
            raise CreativeWorkflowError("invalid_revision_document_paths")
        checked_paths: list[str] = []
        for pointer in raw_paths:
            _validate_revision_json_pointer(
                pointer,
                document_name=document_name,
            )
            checked_paths.append(pointer)
        checked_document_paths[document_name] = sorted(checked_paths)
    if "score" not in checked_documents:
        if score_scope is not None:
            raise CreativeWorkflowError("invalid_revision_scope")
        if not any(checked_document_paths.values()):
            raise CreativeWorkflowError("empty_revision_scope")
        return {
            "change_scale": change_scale,
            "documents": checked_documents,
            "allowed_document_paths": checked_document_paths,
            "score": None,
            "whole_work_cost": None,
        }
    score_keys = {
        "part_ids", "event_ids", "bar_ranges", "allowed_note_fields",
        "allow_event_additions", "allow_event_deletions", "allow_reordering",
    }
    if not isinstance(score_scope, Mapping) or set(score_scope) != score_keys:
        raise CreativeWorkflowError("invalid_revision_score_scope")
    if not isinstance(score_scope["part_ids"], (list, tuple)) or not isinstance(
        score_scope["event_ids"], (list, tuple)
    ):
        raise CreativeWorkflowError("invalid_revision_score_scope")
    raw_part_ids = list(score_scope["part_ids"])
    raw_event_ids = list(score_scope["event_ids"])
    if (
        any(not isinstance(item, str) for item in raw_part_ids + raw_event_ids)
        or len(set(raw_part_ids)) != len(raw_part_ids)
        or len(set(raw_event_ids)) != len(raw_event_ids)
    ):
        raise CreativeWorkflowError("invalid_revision_score_scope")
    part_ids = _bounded_text_list(
        raw_part_ids, field="revision_scope.score.part_ids",
        maximum_items=128, item_bytes=128,
    )
    event_ids = _bounded_text_list(
        raw_event_ids, field="revision_scope.score.event_ids",
        maximum_items=1024, item_bytes=256,
    )
    allowed_fields = score_scope["allowed_note_fields"]
    if (
        not isinstance(allowed_fields, (list, tuple))
        or any(not isinstance(item, str) for item in allowed_fields)
        or len(set(allowed_fields)) != len(allowed_fields)
        or not set(allowed_fields).issubset(_REVISION_NOTE_FIELDS)
    ):
        raise CreativeWorkflowError("invalid_revision_score_scope")
    ranges = score_scope["bar_ranges"]
    if not isinstance(ranges, (list, tuple)) or len(ranges) > 128:
        raise CreativeWorkflowError("invalid_revision_score_scope")
    checked_ranges: list[dict[str, int]] = []
    for item in ranges:
        if not isinstance(item, Mapping) or set(item) != {"start", "end"}:
            raise CreativeWorkflowError("invalid_revision_score_scope")
        start_bar, end_bar = item["start"], item["end"]
        if (
            isinstance(start_bar, bool) or not isinstance(start_bar, int)
            or isinstance(end_bar, bool) or not isinstance(end_bar, int)
            or start_bar < 1 or end_bar < start_bar
        ):
            raise CreativeWorkflowError("invalid_revision_score_scope")
        checked_ranges.append({"start": start_bar, "end": end_bar})
    bool_fields = (
        "allow_event_additions", "allow_event_deletions", "allow_reordering",
    )
    if any(not isinstance(score_scope[field], bool) for field in bool_fields):
        raise CreativeWorkflowError("invalid_revision_score_scope")
    has_event_operation = bool(allowed_fields) or any(
        score_scope[field]
        for field in ("allow_event_additions", "allow_event_deletions", "allow_reordering")
    )
    if score_scope["allow_reordering"]:
        raise CreativeWorkflowError("bounded_revision_reordering_forbidden")
    if has_event_operation and not event_ids:
        raise CreativeWorkflowError("revision_event_ids_required")
    if not has_event_operation and not any(checked_document_paths.values()):
        raise CreativeWorkflowError("empty_revision_scope")
    return {
        "change_scale": change_scale,
        "documents": checked_documents,
        "allowed_document_paths": checked_document_paths,
        "score": {
            "part_ids": sorted(part_ids),
            "event_ids": sorted(event_ids),
            "bar_ranges": sorted(
                checked_ranges, key=lambda item: (item["start"], item["end"])
            ),
            "allowed_note_fields": sorted(allowed_fields),
            **{field: score_scope[field] for field in bool_fields},
        },
        "whole_work_cost": None,
    }


def _revision_contract_hash(
    contract: Mapping[str, Any], *, decision: Mapping[str, Any]
) -> str:
    payload = copy.deepcopy(dict(contract))
    payload.pop("contract_sha256", None)
    payload["protected_values"] = copy.deepcopy(decision["protected_values"])
    payload["sacrificed_values"] = copy.deepcopy(decision["sacrificed_values"])
    payload["expected_audible_change"] = decision["expected_audible_change"]
    return canonical_json_sha256(payload)


def _validate_revision_contract(
    value: object, *, iteration: Mapping[str, Any], decision: Mapping[str, Any]
) -> None:
    expected_keys = {
        "kind", "schema_version", "contract_id", "baseline",
        "revision_target_evidence_ids", "revision_scope", "withdrawal_condition",
        "authoring_causal_fence", "contract_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected_keys:
        raise CreativeWorkflowError("invalid_revision_contract")
    if (
        value["kind"] != "tianlai.workflow_revision_contract"
        or isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != 1
        or value["contract_id"] != f"revision-contract-{iteration['iteration_number']:04d}"
    ):
        raise CreativeWorkflowError("invalid_revision_contract")
    baseline = value["baseline"]
    if not isinstance(baseline, Mapping) or set(baseline) != {
        "authoring_revision", "document_sha256", "candidate",
        "candidate_source_iteration_number",
    }:
        raise CreativeWorkflowError("invalid_revision_contract")
    if baseline["authoring_revision"] != iteration["anchor"]["authoring_revision"]:
        raise CreativeWorkflowError("revision_contract_baseline_mismatch")
    hashes = baseline["document_sha256"]
    if not isinstance(hashes, Mapping) or set(hashes) != _REVISION_DOCUMENTS:
        raise CreativeWorkflowError("invalid_revision_contract")
    for digest in hashes.values():
        _checked_revision(digest, code="invalid_revision_contract")
    _validate_candidate_locator(baseline["candidate"])
    expected_candidate = (
        _anchor_locator(iteration["anchor"].get("candidate"))
        if isinstance(iteration["anchor"].get("candidate"), Mapping)
        else iteration["anchor"].get("parent_candidate")
    )
    if baseline["candidate"] != expected_candidate:
        raise CreativeWorkflowError("revision_contract_baseline_candidate_mismatch")
    source_number = baseline["candidate_source_iteration_number"]
    if (baseline["candidate"] is None) is not (source_number is None):
        raise CreativeWorkflowError("invalid_revision_contract")
    if source_number is not None:
        _strict_governance_integer(
            source_number, code="invalid_revision_contract", minimum=1,
            maximum=iteration["iteration_number"],
        )
    targets = value["revision_target_evidence_ids"]
    if (
        not isinstance(targets, list) or not targets
        or len(set(targets)) != len(targets)
        or any(not isinstance(item, str) for item in targets)
    ):
        raise CreativeWorkflowError("invalid_revision_contract")
    dispositions = {
        item["evidence_id"]: item["disposition"]
        for item in decision.get("evidence_dispositions", [])
    }
    expected_targets = sorted(
        evidence_id for evidence_id, disposition in dispositions.items()
        if disposition == "revision_target"
    )
    if targets != expected_targets:
        raise CreativeWorkflowError("revision_contract_target_mismatch")
    if _normalize_revision_scope(value["revision_scope"]) != value["revision_scope"]:
        raise CreativeWorkflowError("invalid_revision_scope")
    _bounded_text(
        value["withdrawal_condition"], field="revision_contract.withdrawal_condition",
        maximum_bytes=4096,
    )
    fence = value["authoring_causal_fence"]
    if not isinstance(fence, Mapping) or set(fence) != {
        "anchor_revision", "save_sequence", "anchor_save_event_sha256"
    }:
        raise CreativeWorkflowError("invalid_revision_contract")
    _checked_authoring_revision(fence["anchor_revision"])
    _strict_governance_integer(
        fence["save_sequence"], code="invalid_revision_contract", minimum=1,
        maximum=MAX_AUTHORING_SAVE_SEQUENCE,
    )
    _checked_revision(fence["anchor_save_event_sha256"], code="invalid_revision_contract")
    _checked_revision(value["contract_sha256"], code="invalid_revision_contract")
    if value["contract_sha256"] != _revision_contract_hash(value, decision=decision):
        raise CreativeWorkflowError("revision_contract_hash_mismatch")


def _validate_prior_revision_assessment(
    value: object,
    *, contract: Mapping[str, Any], iteration: Mapping[str, Any],
    decision: Mapping[str, Any], selected_basis_ids: set[str],
) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "contract_sha256", "outcome", "rationale", "basis_ids"
    }:
        raise CreativeWorkflowError("invalid_prior_revision_assessment")
    if value["contract_sha256"] != contract["contract_sha256"]:
        raise CreativeWorkflowError("revision_assessment_contract_mismatch")
    if value["outcome"] not in _REVISION_ASSESSMENT_OUTCOMES:
        raise CreativeWorkflowError("invalid_prior_revision_assessment")
    _bounded_text(
        value["rationale"], field="prior_revision_assessment.rationale",
        maximum_bytes=4096,
    )
    basis_ids = value["basis_ids"]
    if (
        not isinstance(basis_ids, list) or not basis_ids
        or len(basis_ids) > MAX_SETTLEMENT_BASIS_IDS
        or len(set(basis_ids)) != len(basis_ids)
        or any(not isinstance(item, str) for item in basis_ids)
        or not set(basis_ids).issubset(selected_basis_ids)
    ):
        raise CreativeWorkflowError("revision_assessment_basis_not_selected")
    candidate = iteration["anchor"].get("candidate")
    if value["outcome"] == "promote_challenger" and not isinstance(
        candidate, Mapping
    ):
        raise CreativeWorkflowError("revision_assessment_requires_challenger")
    expected_candidate_id = (
        candidate["candidate_id"] if isinstance(candidate, Mapping) else None
    )
    if not any(
        review["review_id"] in basis_ids
        and review["candidate_id"] == expected_candidate_id
        and review["reviewer"] == decision["final_authority"]
        and review["perception_basis"] == decision["perception_basis"]
        for review in iteration["reviews"]
    ):
        raise CreativeWorkflowError("revision_assessment_candidate_review_required")


def _validate_decision(value: object, *, iteration: dict[str, Any]) -> None:
    optional_keys = {
        "derivation_ids",
        "charter_settlement",
        "revision_contract",
        "prior_revision_assessment",
    }
    if (
        not isinstance(value, dict)
        or not _DECISION_KEYS.issubset(value)
        or set(value) - _DECISION_KEYS - _DECISION_CLAIM_KEYS - optional_keys
        or ("review_ids" in value) is not ("evidence_dispositions" in value)
    ):
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

    review_by_id = {item["review_id"]: item for item in iteration["reviews"]}
    evidence_by_id = {item["evidence_id"]: item for item in iteration["evidence"]}
    exception_by_id = {
        item["exception_id"]: item for item in iteration["exceptions"]
    }
    derivation_by_id = {
        item["derivation_id"]: item for item in iteration.get("derivations", [])
    }

    def checked_references(
        field: str, available: Mapping[str, Any], *, maximum_items: int
    ) -> set[str]:
        raw = value[field]
        if (
            not isinstance(raw, list)
            or len(raw) > maximum_items
            or any(not isinstance(item, str) for item in raw)
            or len(set(raw)) != len(raw)
            or not set(raw).issubset(available)
        ):
            raise CreativeWorkflowError(f"decision_{field[:-4]}_not_found")
        return set(raw)

    selected_evidence = checked_references(
        "evidence_ids", evidence_by_id, maximum_items=MAX_EVIDENCE_PER_ITERATION
    )
    selected_exceptions = checked_references(
        "exception_ids", exception_by_id, maximum_items=MAX_EXCEPTIONS_PER_ITERATION
    )
    if not selected_evidence.issubset(evidence_by_id):
        raise CreativeWorkflowError("decision_evidence_not_found")
    selected_reviews: set[str] = set()
    if "review_ids" in value:
        selected_reviews = checked_references(
            "review_ids", review_by_id, maximum_items=MAX_REVIEWS_PER_ITERATION
        )
        if value["disposition"] == "accept":
            selected_review_phases = {
                review_by_id[review_id]["phase"] for review_id in selected_reviews
            }
            required_review_phases = {
                "intent",
                "symbolic_structure",
                "orchestration_performance",
                "render_report",
            }
            if not required_review_phases.issubset(selected_review_phases):
                raise CreativeWorkflowError("acceptance_review_ids_incomplete")
        if value["perception_basis"] == "audio_audition" and not any(
            review_by_id[review_id]["reviewer"] == value["final_authority"]
            and review_by_id[review_id]["phase"] == "audio_audition"
            and review_by_id[review_id]["perception_basis"] == "audio_audition"
            for review_id in selected_reviews
        ):
            raise CreativeWorkflowError("audio_audition_review_required")
        if value["disposition"] != "stop" and not any(
            review_by_id[review_id]["reviewer"] == value["final_authority"]
            and review_by_id[review_id]["perception_basis"]
            == value["perception_basis"]
            for review_id in selected_reviews
        ):
            raise CreativeWorkflowError("decision_perception_basis_unproven")
    selected_derivations: set[str] = set()
    if "derivation_ids" in value:
        selected_derivations = checked_references(
            "derivation_ids",
            derivation_by_id,
            maximum_items=MAX_DERIVATIONS_PER_ITERATION,
        )

    if "evidence_dispositions" in value:
        raw_dispositions = value["evidence_dispositions"]
        if not isinstance(raw_dispositions, list) or len(
            raw_dispositions
        ) > MAX_EVIDENCE_PER_ITERATION:
            raise CreativeWorkflowError("invalid_evidence_disposition")
        disposition_by_evidence: dict[str, str] = {}
        resolved_evidence_dependencies: dict[str, set[str]] = {}
        for index, item in enumerate(raw_dispositions):
            if not isinstance(item, dict) or set(item) != {
                "evidence_id",
                "disposition",
                "rationale",
                "basis_ids",
            }:
                raise CreativeWorkflowError("invalid_evidence_disposition")
            evidence_id = item["evidence_id"]
            if (
                not isinstance(evidence_id, str)
                or evidence_id not in evidence_by_id
                or evidence_id in disposition_by_evidence
                or evidence_id not in selected_evidence
            ):
                raise CreativeWorkflowError("invalid_evidence_disposition")
            disposition = item["disposition"]
            if disposition not in _EVIDENCE_DISPOSITIONS:
                raise CreativeWorkflowError("invalid_evidence_disposition")
            _bounded_text(
                item["rationale"],
                field=f"decision.evidence_dispositions[{index}].rationale",
                maximum_bytes=4096,
            )
            basis_ids = item["basis_ids"]
            if (
                not isinstance(basis_ids, list)
                or len(basis_ids) > MAX_DECISION_BASIS_IDS
                or any(not isinstance(basis_id, str) for basis_id in basis_ids)
                or len(set(basis_ids)) != len(basis_ids)
            ):
                raise CreativeWorkflowError("invalid_evidence_disposition_basis")
            for basis_id in basis_ids:
                if basis_id == evidence_id:
                    raise CreativeWorkflowError(
                        "evidence_disposition_basis_self_reference"
                    )
                if basis_id in review_by_id:
                    if basis_id not in selected_reviews:
                        raise CreativeWorkflowError(
                            "evidence_disposition_basis_not_selected"
                        )
                elif basis_id in evidence_by_id:
                    if basis_id not in selected_evidence:
                        raise CreativeWorkflowError(
                            "evidence_disposition_basis_not_selected"
                        )
                elif basis_id in exception_by_id:
                    if basis_id not in selected_exceptions:
                        raise CreativeWorkflowError(
                            "evidence_disposition_basis_not_selected"
                        )
                elif basis_id in derivation_by_id:
                    if basis_id not in selected_derivations:
                        raise CreativeWorkflowError(
                            "evidence_disposition_basis_not_selected"
                        )
                else:
                    raise CreativeWorkflowError(
                        "evidence_disposition_basis_not_found"
                    )
            if disposition in {"resolved", "contested"} and not basis_ids:
                raise CreativeWorkflowError("evidence_disposition_basis_required")
            if disposition == "contested" and not any(
                basis_id in evidence_by_id and basis_id != evidence_id
                for basis_id in basis_ids
            ):
                raise CreativeWorkflowError("contested_evidence_basis_required")
            if disposition == "excepted" and not any(
                basis_id in exception_by_id
                and evidence_id in exception_by_id[basis_id]["evidence_ids"]
                for basis_id in basis_ids
            ):
                raise CreativeWorkflowError("evidence_disposition_exception_required")
            if disposition == "resolved":
                resolved_evidence_dependencies[evidence_id] = {
                    basis_id
                    for basis_id in basis_ids
                    if basis_id in evidence_by_id
                }
            evidence = evidence_by_id[evidence_id]
            if (
                evidence["category"] == "promise_conflict"
                and disposition == "accepted_risk"
            ):
                raise CreativeWorkflowError(
                    "promise_conflict_cannot_be_accepted_as_risk"
                )
            if value["disposition"] == "accept" and disposition not in {
                "resolved",
                "accepted_risk",
                "excepted",
            }:
                raise CreativeWorkflowError("acceptance_evidence_still_open")
            if disposition == "revision_target" and value["disposition"] not in {
                "revise",
                "recommend_revision",
            }:
                raise CreativeWorkflowError("invalid_revision_target")
            disposition_by_evidence[evidence_id] = disposition
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit_resolved_evidence(evidence_id: str) -> None:
            if evidence_id in visiting:
                raise CreativeWorkflowError("evidence_resolution_cycle")
            if evidence_id in visited:
                return
            visiting.add(evidence_id)
            for dependency_id in resolved_evidence_dependencies.get(
                evidence_id, set()
            ):
                if dependency_id in resolved_evidence_dependencies:
                    visit_resolved_evidence(dependency_id)
            visiting.remove(evidence_id)
            visited.add(evidence_id)

        for evidence_id in resolved_evidence_dependencies:
            visit_resolved_evidence(evidence_id)
        expected_nonhard = {
            evidence_id
            for evidence_id, evidence in evidence_by_id.items()
            if evidence["category"] != "hard_failure"
        }
        if set(disposition_by_evidence) != expected_nonhard:
            raise CreativeWorkflowError("evidence_disposition_incomplete")
        if value["disposition"] in {"revise", "recommend_revision"} and not any(
            disposition == "revision_target"
            for disposition in disposition_by_evidence.values()
        ):
            raise CreativeWorkflowError("revision_target_required")
    if "charter_settlement" in value:
        if "review_ids" not in value or "evidence_dispositions" not in value:
            raise CreativeWorkflowError("invalid_iteration_decision")
        selected_basis_pool = (
            selected_reviews | selected_evidence | selected_exceptions
        )
        settlement = value["charter_settlement"]
        if not isinstance(settlement, list) or len(settlement) > MAX_SETTLEMENT_ITEMS:
            raise CreativeWorkflowError("invalid_charter_settlement")
        seen_targets: set[str] = set()
        for index, item in enumerate(settlement):
            field = f"decision.charter_settlement[{index}]"
            if not isinstance(item, dict) or set(item) != {
                "target",
                "status",
                "rationale",
                "basis_ids",
                "event_ids",
            }:
                raise CreativeWorkflowError("invalid_charter_settlement")
            target = item["target"]
            if (
                not isinstance(target, str)
                or _SETTLEMENT_TARGET.fullmatch(target) is None
            ):
                raise CreativeWorkflowError("invalid_charter_settlement_target")
            if target in seen_targets:
                raise CreativeWorkflowError("duplicate_charter_settlement_target")
            seen_targets.add(target)
            if item["status"] not in CHARTER_SETTLEMENT_STATUSES:
                raise CreativeWorkflowError("invalid_charter_settlement_status")
            _bounded_text(item["rationale"], field=f"{field}.rationale", maximum_bytes=4096)
            basis_ids = item["basis_ids"]
            if (
                not isinstance(basis_ids, list)
                or not basis_ids
                or len(basis_ids) > MAX_SETTLEMENT_BASIS_IDS
                or any(not isinstance(basis_id, str) for basis_id in basis_ids)
                or len(set(basis_ids)) != len(basis_ids)
            ):
                raise CreativeWorkflowError("invalid_charter_settlement_basis")
            for basis_id in basis_ids:
                if basis_id not in selected_basis_pool | selected_derivations:
                    raise CreativeWorkflowError(
                        "charter_settlement_basis_not_selected"
                    )
            if item["status"] == "transformed" and not any(
                basis_id in selected_derivations for basis_id in basis_ids
            ):
                raise CreativeWorkflowError(
                    "charter_settlement_transformation_requires_derivation"
                )
            if item["status"] == "refused" and not any(
                basis_id in selected_exceptions or basis_id in selected_derivations
                for basis_id in basis_ids
            ):
                raise CreativeWorkflowError(
                    "charter_settlement_refusal_requires_declaration"
                )
            event_ids = item["event_ids"]
            if not isinstance(event_ids, list) or len(event_ids) > MAX_DERIVATION_MATERIAL_REFS:
                raise CreativeWorkflowError("invalid_charter_settlement")
            for event_index, event_id in enumerate(event_ids):
                if not isinstance(event_id, str) or not event_id.strip():
                    raise CreativeWorkflowError("invalid_charter_settlement")
                _bounded_text(
                    event_id,
                    field=f"{field}.event_ids[{event_index}]",
                    maximum_bytes=256,
                )
            if len(set(event_ids)) != len(event_ids):
                raise CreativeWorkflowError(
                    "duplicate_charter_settlement_event_reference"
                )
    expected_change = value["expected_audible_change"]
    if expected_change is not None:
        _bounded_text(expected_change, field="decision.expected_audible_change", maximum_bytes=4096)
    if value["disposition"] == "revise" and expected_change is None:
        raise CreativeWorkflowError("revision_hypothesis_required")
    if "revision_contract" in value:
        if value["disposition"] != "revise":
            raise CreativeWorkflowError("revision_contract_not_applicable")
        _validate_revision_contract(
            value["revision_contract"], iteration=iteration, decision=value
        )
    if value["final_authority"] not in FINAL_AUTHORITIES or value[
        "perception_basis"
    ] not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_decision_authority")
    if value["claim_scope"] != "contextual_workflow_decision_not_objective_quality":
        raise CreativeWorkflowError("invalid_decision_claim_scope")
    _canonical_timestamp(value["decided_at_utc"], code="invalid_decision_timestamp")


def _open_evidence_ids_from_decision(decision: Mapping[str, Any]) -> list[str]:
    dispositions = decision.get("evidence_dispositions")
    if not isinstance(dispositions, list):
        return []
    return [
        item["evidence_id"]
        for item in dispositions
        if item["disposition"] in _OPEN_EVIDENCE_DISPOSITIONS
    ]


_SETTLEMENT_TARGET = re.compile(
    r"^one_sentence_promise$|^identity_kernel\.invariants\[[0-9]+\]$|^ending_contract$"
)


def _charter_invariant_count(charter: Mapping[str, Any] | None) -> int:
    if not isinstance(charter, Mapping):
        return 0
    kernel = charter.get("identity_kernel")
    invariants = kernel.get("invariants", []) if isinstance(kernel, Mapping) else []
    return len(invariants) if isinstance(invariants, list) else 0


def _charter_settlement_targets(charter: Mapping[str, Any] | None) -> list[str]:
    """Enumerate the affirmative promises a settlement must account for.

    Prohibited shortcuts are deliberately not settlement targets: they are
    negative constraints whose violation already requires a charter-targeted
    exception.  Settlement keeps the positive promise ledger only.
    """

    if not isinstance(charter, Mapping):
        return []
    return [
        "one_sentence_promise",
        *(
            f"identity_kernel.invariants[{index}]"
            for index in range(_charter_invariant_count(charter))
        ),
        "ending_contract",
    ]


def _validate_charter_settlement_completeness(
    decision: Mapping[str, Any],
    *,
    charter: Mapping[str, Any] | None,
    require_for_acceptance: bool = False,
) -> None:
    """Check settlement targets against the frozen charter.

    Structural settlement rules live in ``_validate_decision``; this
    cross-check needs the charter and therefore runs at record time and in
    the state validator, where the charter is available.  Historical accepts
    recorded before settlement existed keep validating; the acceptance
    requirement is enforced at record time for new decisions.
    """

    settlement = decision.get("charter_settlement")
    if settlement is None:
        if require_for_acceptance and decision["disposition"] == "accept":
            raise CreativeWorkflowError("acceptance_charter_settlement_missing")
        return
    targets = _charter_settlement_targets(charter)
    seen: set[str] = set()
    for item in settlement:
        target = item["target"]
        if target not in targets:
            raise CreativeWorkflowError("charter_settlement_target_unknown")
        if target in seen:
            raise CreativeWorkflowError("duplicate_charter_settlement_target")
        seen.add(target)
    if decision["disposition"] == "accept" and seen != set(targets):
        raise CreativeWorkflowError("acceptance_charter_settlement_incomplete")


def _validate_acceptance_gate(value: object) -> None:
    expected = {
        "kind",
        "schema_version",
        "profile",
        "authoring_revision",
        "candidate_manifest_sha256",
        "checked_hard_failure_evidence_ids",
        "unresolved_hard_failure_evidence_ids",
        "readiness_result_sha256",
        "recorded_at_utc",
        "claim_scope",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CreativeWorkflowError("invalid_acceptance_gate")
    if (
        value["kind"] != _ACCEPTANCE_GATE_KIND
        or isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != WORKFLOW_VERSION
        or value["profile"] != _ACCEPTANCE_GATE_PROFILE
        or value["claim_scope"] != _ACCEPTANCE_GATE_CLAIM_SCOPE
    ):
        raise CreativeWorkflowError("invalid_acceptance_gate")
    _checked_authoring_revision(value["authoring_revision"])
    _checked_revision(
        value["candidate_manifest_sha256"],
        code="invalid_acceptance_gate",
    )
    for field in (
        "checked_hard_failure_evidence_ids",
        "unresolved_hard_failure_evidence_ids",
    ):
        evidence_ids = value[field]
        if (
            not isinstance(evidence_ids, list)
            or len(evidence_ids) > MAX_EVIDENCE_PER_ITERATION
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"evidence-[0-9a-f]{20}", item) is None
                for item in evidence_ids
            )
            or len(set(evidence_ids)) != len(evidence_ids)
        ):
            raise CreativeWorkflowError("invalid_acceptance_gate")
    readiness_hash = value["readiness_result_sha256"]
    if readiness_hash is not None:
        _checked_revision(readiness_hash, code="invalid_acceptance_gate")
    if (readiness_hash is None) is not (
        not value["checked_hard_failure_evidence_ids"]
    ):
        raise CreativeWorkflowError("invalid_acceptance_gate")
    _canonical_timestamp(
        value["recorded_at_utc"], code="invalid_acceptance_gate"
    )


def _validate_termination(value: object) -> None:
    base_keys = {
        "reason",
        "summary",
        "final_authority",
        "perception_basis",
        "selected_candidate",
        "terminated_at_utc",
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(base_keys),
        frozenset(base_keys) | {"open_evidence_ids"},
        frozenset(base_keys) | {"open_evidence_ids", "acceptance_gate"},
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
    if "open_evidence_ids" in value:
        open_ids = value["open_evidence_ids"]
        if (
            not isinstance(open_ids, list)
            or len(open_ids) > MAX_EVIDENCE_PER_ITERATION
            or any(
                not isinstance(item, str)
                or re.fullmatch(r"evidence-[0-9a-f]{20}", item) is None
                for item in open_ids
            )
            or len(set(open_ids)) != len(open_ids)
        ):
            raise CreativeWorkflowError("invalid_open_evidence_ids")
    if "acceptance_gate" in value:
        _validate_acceptance_gate(value["acceptance_gate"])
    _canonical_timestamp(value["terminated_at_utc"], code="invalid_termination_timestamp")


def _candidate_anchors_match(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> bool:
    return all(
        first.get(key) == second.get(key)
        for key in first
        if key != "verified_at_utc"
    )


def _challenger_source_contract(
    state: Mapping[str, Any], iteration_number: int
) -> Mapping[str, Any] | None:
    if iteration_number <= 1:
        return None
    previous = state["iterations"][iteration_number - 2]
    decision = previous.get("decision")
    if (
        previous.get("outcome") != "revised"
        or not isinstance(decision, Mapping)
        or not isinstance(decision.get("revision_contract"), Mapping)
    ):
        return None
    return decision["revision_contract"]


def _legacy_challenger_baseline_candidate(
    state: Mapping[str, Any], iteration_number: int
) -> tuple[bool, Mapping[str, Any] | None]:
    """Resolve the conservative baseline of one pre-contract revision.

    Historical ledgers cannot honestly acquire a retrospective contract or
    assessment.  They still carry enough immutable lineage to prevent a later
    challenger from becoming the terminal selection merely because it is
    current.
    """

    if iteration_number <= 1:
        return False, None
    previous = state["iterations"][iteration_number - 2]
    decision = previous.get("decision")
    if (
        previous.get("outcome") != "revised"
        or not isinstance(decision, Mapping)
        or decision.get("disposition") != "revise"
        or "revision_contract" in decision
    ):
        return False, None
    candidate = previous["anchor"].get("candidate")
    if isinstance(candidate, Mapping):
        return True, candidate
    locator = previous["anchor"].get("parent_candidate")
    if locator is None:
        return True, None
    for source in reversed(state["iterations"][: iteration_number - 1]):
        source_candidate = source["anchor"].get("candidate")
        if (
            isinstance(source_candidate, Mapping)
            and _anchor_locator(source_candidate) == locator
        ):
            return True, source_candidate
    raise CreativeWorkflowError("legacy_revision_baseline_candidate_not_found")


def _terminal_candidate_for_iteration(
    state: Mapping[str, Any], iteration: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    candidate = iteration["anchor"].get("candidate")
    contract = _challenger_source_contract(
        state, iteration["iteration_number"]
    )
    if contract is None:
        legacy_challenger, legacy_baseline = (
            _legacy_challenger_baseline_candidate(
                state, iteration["iteration_number"]
            )
        )
        decision = iteration.get("decision")
        if legacy_challenger and not (
            isinstance(decision, Mapping)
            and decision.get("disposition") == "accept"
        ):
            return legacy_baseline
        return candidate
    if contract["baseline"]["candidate"] is None:
        return candidate
    decision = iteration.get("decision")
    assessment = (
        decision.get("prior_revision_assessment")
        if isinstance(decision, Mapping)
        else None
    )
    if (
        isinstance(assessment, Mapping)
        and assessment.get("outcome") == "promote_challenger"
    ):
        return candidate
    baseline_locator = contract["baseline"]["candidate"]
    source_number = contract["baseline"]["candidate_source_iteration_number"]
    if not isinstance(source_number, int):
        return None
    source_candidate = state["iterations"][source_number - 1]["anchor"].get(
        "candidate"
    )
    if (
        isinstance(source_candidate, Mapping)
        and _anchor_locator(source_candidate) == baseline_locator
    ):
        return source_candidate
    raise CreativeWorkflowError("revision_contract_baseline_candidate_mismatch")


def _validate_terminal_contract(state: Mapping[str, Any]) -> None:
    """Bind terminal projection to the actual final iteration and decision."""

    termination = state["termination"]
    if not isinstance(termination, dict):
        return
    status = state["status"]
    iterations = state["iterations"]
    reason = termination["reason"]
    if (
        reason != "accepted_under_charter"
        and "acceptance_gate" in termination
    ):
        raise CreativeWorkflowError("acceptance_gate_not_applicable")
    if status == "disabled":
        if reason != "mode_off" or iterations or termination["selected_candidate"] is not None:
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        if "open_evidence_ids" in termination and termination["open_evidence_ids"]:
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        return
    if status == "completed":
        if reason != "accepted_under_charter" or not iterations:
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
    elif status == "stopped":
        if reason in {"mode_off", "accepted_under_charter"}:
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
    else:
        raise CreativeWorkflowError("workflow_terminal_contract_mismatch")

    if not iterations:
        if reason not in DIRECT_TERMINATION_REASONS:
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        if termination["selected_candidate"] is not None:
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        if "open_evidence_ids" in termination and termination["open_evidence_ids"]:
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        return

    last = iterations[-1]
    if termination["terminated_at_utc"] != last["closed_at_utc"]:
        raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
    expected_candidate = _terminal_candidate_for_iteration(state, last)
    selected_candidate = termination["selected_candidate"]
    if isinstance(expected_candidate, dict):
        if not isinstance(selected_candidate, dict) or not _candidate_anchors_match(
            expected_candidate, selected_candidate
        ):
            raise CreativeWorkflowError("termination_candidate_mismatch")
    elif selected_candidate is not None:
        raise CreativeWorkflowError("termination_candidate_mismatch")

    decision = last["decision"]
    terminal_map = {
        "accepted_under_charter": ("accept", "accepted"),
        "revision_recommended": ("recommend_revision", "revision_recommended"),
        "preserved_without_acceptance": ("preserve", "preserved"),
        "creator_stopped": ("stop", "stopped"),
        "agent_stopped": ("stop", "stopped"),
    }
    if reason in terminal_map:
        expected_disposition, expected_outcome = terminal_map[reason]
        if (
            not isinstance(decision, dict)
            or decision["disposition"] != expected_disposition
            or last["outcome"] != expected_outcome
            or termination["summary"] != decision["summary"]
            or termination["final_authority"] != decision["final_authority"]
            or termination["perception_basis"] != decision["perception_basis"]
        ):
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        if (
            decision["decided_at_utc"] != last["closed_at_utc"]
            or decision["decided_at_utc"] != termination["terminated_at_utc"]
        ):
            raise CreativeWorkflowError(
                "workflow_terminal_decision_timestamp_mismatch"
            )
        if reason == "creator_stopped" and decision["final_authority"] != "creator":
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        if reason == "agent_stopped" and decision["final_authority"] != "agent":
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        expected_open_ids = _open_evidence_ids_from_decision(decision)
    else:
        if reason not in DIRECT_TERMINATION_REASONS or last["outcome"] != "stopped":
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        if isinstance(decision, dict) and decision["disposition"] != "revise":
            raise CreativeWorkflowError("workflow_terminal_contract_mismatch")
        expected_open_ids = [
            item["evidence_id"]
            for item in last["evidence"]
            if item["category"] != "hard_failure"
        ]
    claim_lifecycle_decision = isinstance(decision, dict) and _DECISION_CLAIM_KEYS.issubset(
        decision
    )
    if (
        reason in terminal_map
        and not claim_lifecycle_decision
        and "open_evidence_ids" in termination
    ):
        raise CreativeWorkflowError("workflow_claim_lifecycle_hybrid")
    if "open_evidence_ids" not in termination:
        if claim_lifecycle_decision:
            raise CreativeWorkflowError("termination_open_evidence_missing")
    elif termination["open_evidence_ids"] != expected_open_ids:
        raise CreativeWorkflowError("termination_open_evidence_mismatch")

    acceptance_gate = termination.get("acceptance_gate")
    if reason != "accepted_under_charter":
        return
    if acceptance_gate is None:
        if _policy_level(state["policy"]) >= 2:
            raise CreativeWorkflowError("acceptance_gate_missing")
        return
    if _policy_level(state["policy"]) < 2:
        raise CreativeWorkflowError("workflow_acceptance_gate_hybrid")
    assert isinstance(decision, dict)
    assert isinstance(selected_candidate, dict)
    checked_hard_failure_ids = [
        item["evidence_id"]
        for item in last["evidence"]
        if item["category"] == "hard_failure"
    ]
    if (
        acceptance_gate["authoring_revision"]
        != last["anchor"]["authoring_revision"]
        or acceptance_gate["candidate_manifest_sha256"]
        != selected_candidate["candidate_manifest_sha256"]
        or acceptance_gate["checked_hard_failure_evidence_ids"]
        != checked_hard_failure_ids
        or acceptance_gate["recorded_at_utc"]
        != termination["terminated_at_utc"]
    ):
        raise CreativeWorkflowError("acceptance_gate_binding_mismatch")
    if acceptance_gate["unresolved_hard_failure_evidence_ids"]:
        raise CreativeWorkflowError(
            "acceptance_gate_unresolved_hard_failure"
        )


_LEGACY_ITERATION_KEYS = frozenset(
    {
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
    }
)
_ITERATION_KEYS = _LEGACY_ITERATION_KEYS | {"derivations"}
_ITERATION_KEY_SHAPES = {
    _LEGACY_ITERATION_KEYS,
    _LEGACY_ITERATION_KEYS | {"derivations"},
    _LEGACY_ITERATION_KEYS | {"forks"},
    _LEGACY_ITERATION_KEYS | {"derivations", "forks"},
}


def _validate_iteration(
    value: object,
    *,
    workflow_id: str,
    project_id: str,
    expected_number: int,
    active_clause_ids: set[str],
    charter_fields: set[str],
    charter_invariant_count: int | None = None,
) -> None:
    if not isinstance(value, dict) or frozenset(value) not in _ITERATION_KEY_SHAPES:
        raise CreativeWorkflowError("invalid_iteration_record")
    if value["iteration_number"] != expected_number or value["iteration_id"] != f"iteration-{expected_number:04d}":
        raise CreativeWorkflowError("invalid_iteration_identity")
    if value["status"] not in {"reviewing", "candidate_pending", "revision_pending", "closed"}:
        raise CreativeWorkflowError("invalid_iteration_status")
    opened_at = _canonical_timestamp(
        value["opened_at_utc"], code="invalid_iteration_timestamp"
    )
    if value["closed_at_utc"] is not None:
        closed_at = _canonical_timestamp(
            value["closed_at_utc"], code="invalid_iteration_timestamp"
        )
        if closed_at < opened_at:
            raise CreativeWorkflowError("invalid_iteration_timestamp")
    if (value["status"] == "closed") is not (value["closed_at_utc"] is not None):
        raise CreativeWorkflowError("invalid_iteration_status")
    anchor = value["anchor"]
    legacy_anchor_keys = {
        "authoring_revision",
        "parent_candidate",
        "candidate",
    }
    if not isinstance(anchor, dict) or set(anchor) not in (
        legacy_anchor_keys,
        legacy_anchor_keys
        | {
            "authoring_save_sequence",
            "authoring_save_event_sha256",
        },
    ):
        raise CreativeWorkflowError("invalid_iteration_anchor")
    _checked_authoring_revision(anchor["authoring_revision"])
    if "authoring_save_sequence" in anchor:
        _strict_governance_integer(
            anchor["authoring_save_sequence"],
            code="invalid_authoring_causal_anchor",
            minimum=1,
            maximum=MAX_AUTHORING_SAVE_SEQUENCE,
        )
        _checked_revision(
            anchor["authoring_save_event_sha256"],
            code="invalid_authoring_causal_anchor",
        )
    _validate_candidate_locator(anchor["parent_candidate"])
    if anchor["candidate"] is not None:
        _validate_candidate_anchor(anchor["candidate"])
        if anchor["candidate"]["authoring_revision"] != anchor["authoring_revision"]:
            raise CreativeWorkflowError("candidate_revision_mismatch")
        authorization = anchor["candidate"]["workflow_authorization"]
        if isinstance(authorization, dict):
            parent = anchor["parent_candidate"]
            expected_parent = (
                (None, None, None)
                if parent is None
                else (
                    parent["work_id"],
                    parent["candidate_id"],
                    parent["manifest_sha256"],
                )
            )
            if (
                authorization["workflow_id"] != workflow_id
                or authorization["project_id"] != project_id
                or authorization["authoring_revision"]
                != anchor["authoring_revision"]
                or authorization["candidate_work_id"]
                != anchor["candidate"]["work_id"]
                or authorization["candidate_id"]
                != anchor["candidate"]["candidate_id"]
                or (
                    authorization["parent_work_id"],
                    authorization["parent_candidate_id"],
                    authorization["parent_manifest_sha256"],
                )
                != expected_parent
            ):
                raise CreativeWorkflowError("candidate_workflow_binding_mismatch")
    if not isinstance(value["reviews"], list) or len(value["reviews"]) > MAX_REVIEWS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_reviews")
    for review in value["reviews"]:
        _validate_review(
            review,
            workflow_id=workflow_id,
            iteration_number=expected_number,
            iteration=value,
        )
        reviewed_at = review["reviewed_at_utc"]
        if reviewed_at < opened_at or (
            value["closed_at_utc"] is not None
            and reviewed_at > value["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_review_timestamp")
    if len({item["review_id"] for item in value["reviews"]}) != len(value["reviews"]):
        raise CreativeWorkflowError("duplicate_review_record")
    if not isinstance(value["evidence"], list) or len(value["evidence"]) > MAX_EVIDENCE_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_evidence_items")
    for evidence in value["evidence"]:
        _validate_evidence(
            evidence,
            workflow_id=workflow_id,
            iteration_number=expected_number,
            iteration=value,
            active_clause_ids=active_clause_ids,
            charter_fields=charter_fields,
            trusted_hard_failure=True,
        )
        recorded_at = evidence["recorded_at_utc"]
        if recorded_at < opened_at or (
            value["closed_at_utc"] is not None
            and recorded_at > value["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_evidence_timestamp")
    if len({item["evidence_id"] for item in value["evidence"]}) != len(value["evidence"]):
        raise CreativeWorkflowError("duplicate_evidence_record")
    evidence_by_id = {item["evidence_id"]: item for item in value["evidence"]}
    if not isinstance(value["exceptions"], list) or len(value["exceptions"]) > MAX_EXCEPTIONS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_exceptions")
    for exception in value["exceptions"]:
        _validate_exception(
            exception,
            workflow_id=workflow_id,
            iteration_number=expected_number,
            evidence_by_id=evidence_by_id,
            active_clause_ids=active_clause_ids,
        )
        registered_at = exception["registered_at_utc"]
        if registered_at < opened_at or (
            value["closed_at_utc"] is not None
            and registered_at > value["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_exception_timestamp")
    if len({item["exception_id"] for item in value["exceptions"]}) != len(value["exceptions"]):
        raise CreativeWorkflowError("duplicate_exception_record")
    derivations = value.get("derivations", [])
    if not isinstance(derivations, list) or len(derivations) > MAX_DERIVATIONS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_derivations")
    for derivation in derivations:
        _validate_derivation(
            derivation,
            workflow_id=workflow_id,
            iteration_number=expected_number,
            iteration=value,
            active_clause_ids=active_clause_ids,
            charter_fields=charter_fields,
        )
        recorded_at = derivation["recorded_at_utc"]
        if recorded_at < opened_at or (
            value["closed_at_utc"] is not None
            and recorded_at > value["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_derivation_timestamp")
    if len({item["derivation_id"] for item in derivations}) != len(derivations):
        raise CreativeWorkflowError("duplicate_derivation_record")
    forks = value.get("forks", [])
    if not isinstance(forks, list) or len(forks) > MAX_FORKS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_iteration_forks")
    derivation_ids_available = {item["derivation_id"] for item in derivations}
    for fork in forks:
        _validate_fork(
            fork,
            workflow_id=workflow_id,
            iteration=value,
            derivation_ids_available=derivation_ids_available,
            invariant_count=charter_invariant_count,
        )
        recorded_at = fork["recorded_at_utc"]
        if recorded_at < opened_at or (
            value["closed_at_utc"] is not None
            and recorded_at > value["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_fork_timestamp")
    if len({item["fork_id"] for item in forks}) != len(forks):
        raise CreativeWorkflowError("duplicate_fork_record")
    if not isinstance(value["render_attempts"], list) or len(value["render_attempts"]) > MAX_RENDER_ATTEMPTS_PER_ITERATION:
        raise CreativeWorkflowError("too_many_render_attempts")
    for index, attempt in enumerate(value["render_attempts"], start=1):
        _validate_render_attempt(attempt, iteration=value)
        if attempt["attempt_number"] != index:
            raise CreativeWorkflowError("invalid_render_attempt_identity")
        if (
            value["closed_at_utc"] is not None
            and (
                attempt["requested_at_utc"] > value["closed_at_utc"]
                or (
                    attempt["finished_at_utc"] is not None
                    and attempt["finished_at_utc"] > value["closed_at_utc"]
                )
            )
        ):
            raise CreativeWorkflowError("invalid_render_timestamp")
    pending = [item for item in value["render_attempts"] if item["status"] == "pending"]
    if len(pending) > 1 or (value["status"] == "candidate_pending") is not bool(pending):
        raise CreativeWorkflowError("invalid_pending_render_state")
    if value["decision"] is not None:
        _validate_decision(value["decision"], iteration=value)
        decision = value["decision"]
        if decision["decided_at_utc"] < opened_at or (
            value["closed_at_utc"] is not None
            and decision["decided_at_utc"] > value["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_decision_timestamp")
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
    if value["status"] in {"reviewing", "candidate_pending"} and value[
        "decision"
    ] is not None:
        raise CreativeWorkflowError("invalid_iteration_decision_state")
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


def _strict_governance_integer(
    value: object, *, code: str, minimum: int = 0, maximum: int | None = None
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise CreativeWorkflowError(code)
    return value


def _validate_authoring_causal_fence(
    value: object,
    *,
    project_id: str,
    anchor_revision: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "kind",
        "schema_version",
        "project_id",
        "anchor_revision",
        "save_sequence",
        "anchor_save_event_sha256",
    }:
        raise CreativeWorkflowError("invalid_authoring_causal_fence")
    _strict_governance_integer(
        value["schema_version"],
        code="invalid_authoring_causal_fence",
        minimum=WORKFLOW_VERSION,
        maximum=WORKFLOW_VERSION,
    )
    if (
        value["kind"] != "tianlai.authoring_causal_fence"
        or value["project_id"] != project_id
        or value["anchor_revision"] != anchor_revision
        or _WORKFLOW_ID.fullmatch(value["project_id"]) is None
        or _SHA256.fullmatch(value["anchor_revision"]) is None
    ):
        raise CreativeWorkflowError("invalid_authoring_causal_fence")
    _strict_governance_integer(
        value["save_sequence"],
        code="invalid_authoring_causal_fence",
        minimum=0,
        maximum=MAX_AUTHORING_SAVE_SEQUENCE,
    )
    if value["save_sequence"] == 0:
        if value["anchor_save_event_sha256"] is not None:
            raise CreativeWorkflowError("invalid_authoring_causal_fence")
    else:
        _checked_revision(
            value["anchor_save_event_sha256"],
            code="invalid_authoring_causal_fence",
        )
    return copy.deepcopy(dict(value))


def _validate_composition_governance(
    state: Mapping[str, Any],
    *,
    charter: Mapping[str, Any] | None,
    iterations: Sequence[Mapping[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Validate the additive map/preflight/amendment ledger.

    This pass is deliberately self-contained.  Score-local referents are
    re-proved later against immutable authoring revisions, while every charter
    hash, patch, cost sheet and append-only ledger link is proved here.
    """

    governance = state.get("governance")
    expected_keys = {
        "profile",
        "enforcement_started_iteration",
        "initial_charter_sha256",
        "composition_maps",
        "amendment_preflights",
        "amendments",
    }
    if not isinstance(governance, Mapping) or set(governance) != expected_keys:
        raise CreativeWorkflowError("invalid_composition_governance")
    if governance["profile"] != _COMPOSITION_GOVERNANCE_PROFILE:
        raise CreativeWorkflowError("invalid_composition_governance")
    if any(not isinstance(item, Mapping) for item in iterations):
        raise CreativeWorkflowError("invalid_iteration_record")
    enforcement_start = _strict_governance_integer(
        governance["enforcement_started_iteration"],
        code="invalid_composition_governance",
        minimum=1,
        maximum=MAX_ITERATIONS + 1,
    )
    initial_hash = governance["initial_charter_sha256"]
    if charter is None:
        if initial_hash is not None:
            raise CreativeWorkflowError("composition_governance_charter_mismatch")
        if any(
            governance[field]
            for field in (
                "composition_maps",
                "amendment_preflights",
                "amendments",
            )
        ):
            raise CreativeWorkflowError("composition_governance_before_activation")
        return {}
    if initial_hash != canonical_json_sha256(charter):
        raise CreativeWorkflowError("composition_governance_charter_mismatch")

    amendment_wrappers = governance["amendments"]
    if (
        not isinstance(amendment_wrappers, list)
        or len(amendment_wrappers) > MAX_CHARTER_AMENDMENTS
    ):
        raise CreativeWorkflowError("invalid_charter_amendment_ledger")
    core_entries: list[dict[str, Any]] = []
    committed_iterations: list[int] = []
    for offset, wrapper in enumerate(amendment_wrappers):
        legacy_wrapper_keys = {
            "committed_in_iteration",
            "effective_from_iteration",
            "authoring_revision",
            "preflight_sha256",
            "entry",
            "committed_at_utc",
        }
        if not isinstance(wrapper, Mapping) or set(wrapper) not in (
            legacy_wrapper_keys,
            legacy_wrapper_keys | {"authoring_causal_fence"},
        ):
            raise CreativeWorkflowError("invalid_charter_amendment_ledger")
        committed = _strict_governance_integer(
            wrapper["committed_in_iteration"],
            code="invalid_charter_amendment_ledger",
            minimum=1,
            maximum=max(1, len(iterations)),
        )
        effective_from = _strict_governance_integer(
            wrapper["effective_from_iteration"],
            code="invalid_charter_amendment_ledger",
            minimum=2,
            maximum=MAX_ITERATIONS + 1,
        )
        if effective_from != committed + 1 or committed < enforcement_start:
            raise CreativeWorkflowError("invalid_charter_amendment_effective_iteration")
        if committed_iterations and committed <= committed_iterations[-1]:
            raise CreativeWorkflowError("invalid_charter_amendment_sequence")
        committed_iterations.append(committed)
        if committed > len(iterations):
            raise CreativeWorkflowError("charter_amendment_iteration_not_found")
        iteration = iterations[committed - 1]
        anchor = iteration.get("anchor")
        if not isinstance(anchor, Mapping) or wrapper[
            "authoring_revision"
        ] != anchor.get("authoring_revision"):
            raise CreativeWorkflowError("charter_amendment_iteration_mismatch")
        if "authoring_causal_fence" in wrapper:
            _validate_authoring_causal_fence(
                wrapper["authoring_causal_fence"],
                project_id=state["project_id"],
                anchor_revision=wrapper["authoring_revision"],
            )
        decision = iteration.get("decision")
        if not isinstance(decision, Mapping) or decision.get("disposition") != "revise":
            raise CreativeWorkflowError("charter_amendment_requires_revision_decision")
        committed_at = _canonical_timestamp(
            wrapper["committed_at_utc"], code="invalid_charter_amendment_timestamp"
        )
        if committed_at < iteration.get("opened_at_utc", committed_at) or (
            iteration.get("closed_at_utc") is not None
            and committed_at > iteration["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_charter_amendment_timestamp")
        entry = wrapper["entry"]
        if not isinstance(entry, Mapping):
            raise CreativeWorkflowError("invalid_charter_amendment_ledger")
        if entry.get("sequence") != offset + 1 or wrapper[
            "preflight_sha256"
        ] != entry.get("preflight_sha256"):
            raise CreativeWorkflowError("invalid_charter_amendment_sequence")
        core_entries.append(copy.deepcopy(dict(entry)))

    try:
        verification = verify_charter_amendment_ledger(charter, core_entries)
    except CharterAmendmentError as exc:
        _raise_charter_amendment_error(exc)
    final_effective = _normalize_work_charter(verification["effective_charter"])
    if final_effective != verification["effective_charter"]:
        raise CreativeWorkflowError("invalid_effective_work_charter")

    effective_by_iteration: dict[int, dict[str, Any]] = {}
    for number in range(1, len(iterations) + 1):
        try:
            effective = effective_charter_from_ledger(
                charter,
                [
                    entry
                    for wrapper, entry in zip(amendment_wrappers, core_entries)
                    if wrapper["effective_from_iteration"] <= number
                ],
            )
        except CharterAmendmentError as exc:
            _raise_charter_amendment_error(exc)
        normalized = _normalize_work_charter(effective)
        if normalized != effective:
            raise CreativeWorkflowError("invalid_effective_work_charter")
        effective_by_iteration[number] = normalized

    map_records = governance["composition_maps"]
    if not isinstance(map_records, list) or len(map_records) > MAX_COMPOSITION_MAPS:
        raise CreativeWorkflowError("invalid_composition_map_ledger")
    maps_by_iteration: dict[int, Mapping[str, Any]] = {}
    previous_map_iteration = 0
    for record in map_records:
        if not isinstance(record, Mapping) or set(record) != {
            "iteration_number",
            "authoring_revision",
            "effective_charter_sha256",
            "score_sha256",
            "composition_map_sha256",
            "document",
            "recorded_at_utc",
        }:
            raise CreativeWorkflowError("invalid_composition_map_record")
        number = _strict_governance_integer(
            record["iteration_number"],
            code="invalid_composition_map_record",
            minimum=1,
            maximum=max(1, len(iterations)),
        )
        if number <= previous_map_iteration or number in maps_by_iteration:
            raise CreativeWorkflowError("duplicate_iteration_composition_map")
        previous_map_iteration = number
        if number > len(iterations) or number < enforcement_start:
            raise CreativeWorkflowError("invalid_composition_map_iteration")
        iteration = iterations[number - 1]
        anchor = iteration.get("anchor")
        if not isinstance(anchor, Mapping) or record[
            "authoring_revision"
        ] != anchor.get("authoring_revision"):
            raise CreativeWorkflowError("composition_map_authoring_mismatch")
        effective = effective_by_iteration[number]
        if record["effective_charter_sha256"] != canonical_json_sha256(effective):
            raise CreativeWorkflowError("composition_map_charter_mismatch")
        try:
            normalized_map = validate_composition_map(
                record["document"],
                charter_claim_ids=_charter_claim_ids(effective),
            )
        except CompositionMapError as exc:
            _raise_composition_map_error(exc)
        if normalized_map != record["document"]:
            raise CreativeWorkflowError("composition_map_not_normalized")
        if record["composition_map_sha256"] != composition_map_sha256(
            normalized_map
        ):
            raise CreativeWorkflowError("composition_map_identity_mismatch")
        _checked_revision(record["score_sha256"], code="invalid_composition_map_score_hash")
        recorded_at = _canonical_timestamp(
            record["recorded_at_utc"], code="invalid_composition_map_timestamp"
        )
        if recorded_at < iteration.get("opened_at_utc", recorded_at) or (
            iteration.get("closed_at_utc") is not None
            and recorded_at > iteration["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_composition_map_timestamp")
        maps_by_iteration[number] = record

    for number, iteration in enumerate(iterations, start=1):
        if number < enforcement_start or number in maps_by_iteration:
            continue
        if any(
            iteration.get(field)
            for field in (
                "reviews",
                "evidence",
                "exceptions",
                "derivations",
                "forks",
                "render_attempts",
            )
        ):
            raise CreativeWorkflowError(
                "composition_map_required_for_iteration_work"
            )

    preflight_records = governance["amendment_preflights"]
    if (
        not isinstance(preflight_records, list)
        or len(preflight_records) > MAX_CHARTER_AMENDMENT_PREFLIGHTS
    ):
        raise CreativeWorkflowError("invalid_charter_amendment_preflight_ledger")
    preflight_by_sha256: dict[str, Mapping[str, Any]] = {}
    for record in preflight_records:
        if not isinstance(record, Mapping) or set(record) != {
            "iteration_number",
            "authoring_revision",
            "effective_charter_sha256",
            "composition_map_sha256",
            "input_counts",
            "preflight",
            "recorded_at_utc",
        }:
            raise CreativeWorkflowError("invalid_charter_amendment_preflight_record")
        number = _strict_governance_integer(
            record["iteration_number"],
            code="invalid_charter_amendment_preflight_record",
            minimum=1,
            maximum=max(1, len(iterations)),
        )
        if number > len(iterations) or number < enforcement_start:
            raise CreativeWorkflowError("invalid_charter_amendment_preflight_record")
        iteration = iterations[number - 1]
        map_record = maps_by_iteration.get(number)
        if map_record is None:
            raise CreativeWorkflowError("composition_map_required_for_amendment")
        effective = effective_by_iteration[number]
        anchor = iteration.get("anchor")
        if (
            not isinstance(anchor, Mapping)
            or record["authoring_revision"]
            != anchor.get("authoring_revision")
            or record["effective_charter_sha256"]
            != canonical_json_sha256(effective)
            or record["composition_map_sha256"]
            != map_record["composition_map_sha256"]
        ):
            raise CreativeWorkflowError("charter_amendment_preflight_binding_mismatch")
        counts = record["input_counts"]
        if not isinstance(counts, Mapping) or set(counts) != {
            "derivations",
            "reviews",
            "evidence",
        }:
            raise CreativeWorkflowError("invalid_charter_amendment_preflight_inputs")
        if any(
            not isinstance(iteration.get(field), list)
            for field in ("derivations", "reviews", "evidence")
        ):
            raise CreativeWorkflowError("invalid_charter_amendment_preflight_inputs")
        for field in ("derivations", "reviews", "evidence"):
            count = _strict_governance_integer(
                counts[field],
                code="invalid_charter_amendment_preflight_inputs",
                minimum=0,
                maximum=len(iteration.get(field, [])),
            )
            if count > len(iteration.get(field, [])):
                raise CreativeWorkflowError("invalid_charter_amendment_preflight_inputs")
        preflight = record["preflight"]
        if not isinstance(preflight, Mapping):
            raise CreativeWorkflowError("invalid_charter_amendment_preflight_record")
        try:
            expected_preflight = preflight_charter_amendment(
                effective,
                preflight.get("proposal", {}),
                composition_map_dependencies=map_record["document"],
                derivations=iteration.get("derivations", [])[
                    : counts["derivations"]
                ],
                reviews=iteration.get("reviews", [])[: counts["reviews"]],
                evidence=iteration.get("evidence", [])[: counts["evidence"]],
            )
        except CharterAmendmentError as exc:
            _raise_charter_amendment_error(exc)
        if expected_preflight != preflight:
            raise CreativeWorkflowError("charter_amendment_preflight_replay_mismatch")
        preflight_sha256 = preflight.get("preflight_sha256")
        if (
            not isinstance(preflight_sha256, str)
            or preflight_sha256 in preflight_by_sha256
        ):
            raise CreativeWorkflowError("duplicate_charter_amendment_preflight")
        recorded_at = _canonical_timestamp(
            record["recorded_at_utc"],
            code="invalid_charter_amendment_preflight_timestamp",
        )
        if recorded_at < iteration.get("opened_at_utc", recorded_at) or (
            iteration.get("closed_at_utc") is not None
            and recorded_at > iteration["closed_at_utc"]
        ):
            raise CreativeWorkflowError("invalid_charter_amendment_preflight_timestamp")
        preflight_by_sha256[preflight_sha256] = record

    for wrapper in amendment_wrappers:
        recorded = preflight_by_sha256.get(wrapper["preflight_sha256"])
        if (
            recorded is None
            or recorded["iteration_number"] != wrapper["committed_in_iteration"]
            or recorded["preflight"] != wrapper["entry"].get("preflight")
        ):
            raise CreativeWorkflowError("charter_amendment_preflight_not_found")
        iteration = iterations[wrapper["committed_in_iteration"] - 1]
        if recorded["input_counts"] != {
            "derivations": len(iteration.get("derivations", [])),
            "reviews": len(iteration.get("reviews", [])),
            "evidence": len(iteration.get("evidence", [])),
        }:
            raise CreativeWorkflowError("charter_amendment_preflight_stale")
    if len(preflight_by_sha256) != len(amendment_wrappers):
        raise CreativeWorkflowError("orphan_charter_amendment_preflight")

    return effective_by_iteration


def _validate_state_document(state: dict[str, Any]) -> None:
    base_expected = {
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
    allowed_shapes = {frozenset(base_expected), frozenset(base_expected | {"governance"})}
    version = state.get("schema_version")
    if (
        frozenset(state) not in allowed_shapes
        or state.get("kind") != WORKFLOW_STATE_KIND
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version != WORKFLOW_VERSION
    ):
        raise CreativeWorkflowError("invalid_workflow_state")
    policy_level = _policy_level(state["policy"])
    # Exact policy shapes distinguish historical governance level 4 from the
    # new non-governance revision-contract level 4.
    governance_policy = state["policy"] in (
        _LEGACY_GOVERNANCE_POLICY,
        _GOVERNANCE_POLICY,
    )
    if governance_policy is not ("governance" in state):
        raise CreativeWorkflowError("composition_governance_policy_mismatch")
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
    if not isinstance(initial, dict):
        raise CreativeWorkflowError("invalid_initial_anchor")
    initial_revision = _checked_authoring_revision(
        initial.get("authoring_revision")
    )
    if initial != _empty_anchor(
        initial_revision,
        authoring_save_sequence=initial.get("authoring_save_sequence"),
        authoring_save_event_sha256=initial.get(
            "authoring_save_event_sha256"
        ),
    ):
        raise CreativeWorkflowError("invalid_initial_anchor")
    # Pre-derivation-contract workflow revisions were published before the derivation
    # budget existed.  Admit the legacy shape by closing it with the
    # default; every newly published budget carries the full key set.
    closed_budget = dict(state["budget"]) if isinstance(state["budget"], dict) else state["budget"]
    if isinstance(closed_budget, dict):
        closed_budget.setdefault(
            "max_derivations_per_iteration",
            DEFAULT_MAX_DERIVATIONS_PER_ITERATION,
        )
    budget = _normalize_budget(mode, closed_budget)
    if budget != closed_budget:
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
    effective_by_iteration = (
        _validate_composition_governance(
            state,
            charter=charter,
            iterations=iterations,
        )
        if "governance" in state
        else {}
    )
    active_ids = {item["clause_id"] for item in clauses}
    for index, iteration in enumerate(iterations, start=1):
        iteration_charter = effective_by_iteration.get(index, charter)
        charter_fields = (
            set(iteration_charter)
            if isinstance(iteration_charter, Mapping)
            else set()
        )
        _validate_iteration(
            iteration,
            workflow_id=state["workflow_id"],
            project_id=state["project_id"],
            expected_number=index,
            active_clause_ids=active_ids,
            charter_fields=charter_fields,
            charter_invariant_count=(
                _charter_invariant_count(iteration_charter)
                if iteration_charter is not None
                else None
            ),
        )
        decision = iteration.get("decision")
        source_contract = _challenger_source_contract(state, index)
        if isinstance(decision, Mapping):
            assessment = decision.get("prior_revision_assessment")
            if assessment is not None:
                if (
                    source_contract is None
                    or source_contract["baseline"]["candidate"] is None
                ):
                    raise CreativeWorkflowError(
                        "revision_assessment_without_challenger_contract"
                    )
                selected_basis_ids = set(decision.get("review_ids", []))
                selected_basis_ids.update(decision.get("evidence_ids", []))
                selected_basis_ids.update(decision.get("exception_ids", []))
                selected_basis_ids.update(decision.get("derivation_ids", []))
                _validate_prior_revision_assessment(
                    assessment,
                    contract=source_contract,
                    iteration=iteration,
                    decision=decision,
                    selected_basis_ids=selected_basis_ids,
                )
            if (
                source_contract is not None
                and source_contract["baseline"]["candidate"] is not None
                and decision["disposition"]
                in {"accept", "revise", "preserve", "stop", "rollback"}
            ):
                if not isinstance(assessment, Mapping):
                    raise CreativeWorkflowError("prior_revision_assessment_required")
                outcome = assessment["outcome"]
                if (
                    decision["disposition"] in {"accept", "revise", "preserve"}
                    and outcome != "promote_challenger"
                ):
                    raise CreativeWorkflowError("challenger_promotion_required")
                if (
                    decision["disposition"] == "rollback"
                    and outcome not in {"retain_baseline", "inconclusive"}
                ):
                    raise CreativeWorkflowError("invalid_rollback_assessment")
        if isinstance(iteration["decision"], dict) and (
            "charter_settlement" in iteration["decision"]
            or iteration["decision"]["disposition"] == "accept"
        ):
            _validate_charter_settlement_completeness(
                iteration["decision"],
                charter=iteration_charter,
                require_for_acceptance=policy_level >= 3,
            )
        per_iteration_usage = {
            "max_render_attempts_per_iteration": len(iteration["render_attempts"]),
            "max_evidence_items_per_iteration": len(iteration["evidence"]),
            "max_exceptions_per_iteration": len(iteration["exceptions"]),
            "max_reviews_per_iteration": len(iteration["reviews"]),
            "max_derivations_per_iteration": len(
                iteration.get("derivations", [])
            ),
        }
        if any(
            used > budget[field]
            for field, used in per_iteration_usage.items()
        ):
            raise CreativeWorkflowError("workflow_budget_exceeded")
        if index == 1:
            if (
                iteration["anchor"]["authoring_revision"]
                != initial["authoring_revision"]
                or iteration["anchor"]["parent_candidate"] is not None
            ):
                raise CreativeWorkflowError("workflow_iteration_lineage_mismatch")
        else:
            previous = iterations[index - 2]
            if previous["outcome"] == "revised":
                previous_candidate = previous["anchor"].get("candidate")
                expected_parent = (
                    _anchor_locator(previous_candidate)
                    if isinstance(previous_candidate, dict)
                    else previous["anchor"]["parent_candidate"]
                )
                if (
                    previous["next_authoring_revision"]
                    == previous["anchor"]["authoring_revision"]
                    or iteration["anchor"]["authoring_revision"]
                    != previous["next_authoring_revision"]
                    or iteration["anchor"]["parent_candidate"] != expected_parent
                ):
                    raise CreativeWorkflowError(
                        "workflow_iteration_lineage_mismatch"
                    )
            elif previous["outcome"] == "rolled_back":
                if not any(
                    isinstance(target["anchor"].get("candidate"), dict)
                    and iteration["anchor"] == target["anchor"]
                    for target in iterations[: index - 2]
                ):
                    raise CreativeWorkflowError(
                        "workflow_iteration_lineage_mismatch"
                    )
            else:
                raise CreativeWorkflowError("workflow_iteration_lineage_mismatch")
        if iteration["opened_at_utc"] > updated or (
            iteration["closed_at_utc"] is not None
            and iteration["closed_at_utc"] > updated
        ):
            raise CreativeWorkflowError("invalid_workflow_timestamp")
        event_timestamps = [
            *(item["reviewed_at_utc"] for item in iteration["reviews"]),
            *(item["recorded_at_utc"] for item in iteration["evidence"]),
            *(item["registered_at_utc"] for item in iteration["exceptions"]),
            *(item["recorded_at_utc"] for item in iteration.get("derivations", [])),
            *(item["recorded_at_utc"] for item in iteration.get("forks", [])),
            *(item["requested_at_utc"] for item in iteration["render_attempts"]),
            *(
                item["finished_at_utc"]
                for item in iteration["render_attempts"]
                if item["finished_at_utc"] is not None
            ),
        ]
        if isinstance(iteration["decision"], dict):
            event_timestamps.append(iteration["decision"]["decided_at_utc"])
        if isinstance(iteration["anchor"].get("candidate"), dict):
            event_timestamps.append(
                iteration["anchor"]["candidate"]["verified_at_utc"]
            )
        if any(timestamp > updated for timestamp in event_timestamps):
            raise CreativeWorkflowError("invalid_workflow_timestamp")
        if index > 1:
            previous_closed = iterations[index - 2]["closed_at_utc"]
            if (
                previous_closed is None
                or iteration["opened_at_utc"] < previous_closed
            ):
                raise CreativeWorkflowError("invalid_iteration_timestamp")
        if (
            isinstance(iteration["decision"], dict)
            and iteration["decision"]["final_authority"]
            != state["final_authority"]
        ):
            raise CreativeWorkflowError("decision_authority_mismatch")
        if iteration["status"] == "closed":
            decision = iteration["decision"]
            expected_outcome = (
                None
                if not isinstance(decision, dict)
                else {
                    "accept": "accepted",
                    "revise": "revised",
                    "recommend_revision": "revision_recommended",
                    "preserve": "preserved",
                    "stop": "stopped",
                    "rollback": "rolled_back",
                }[decision["disposition"]]
            )
            direct_termination = (
                index == len(iterations)
                and status == "stopped"
                and isinstance(state["termination"], dict)
                and state["termination"].get("reason")
                in DIRECT_TERMINATION_REASONS
                and iteration["outcome"] == "stopped"
                and (
                    decision is None
                    or decision["disposition"] == "revise"
                )
            )
            if not direct_termination and iteration["outcome"] != expected_outcome:
                raise CreativeWorkflowError(
                    "iteration_outcome_decision_mismatch"
                )
            if isinstance(decision, dict) and decision["disposition"] == "accept":
                candidate = iteration["anchor"].get("candidate")
                required_review_phases = {
                    "intent",
                    "symbolic_structure",
                    "orchestration_performance",
                    "render_report",
                }
                if (
                    not isinstance(candidate, dict)
                    or not candidate["workflow_managed"]
                    or not required_review_phases.issubset(
                        {review["phase"] for review in iteration["reviews"]}
                    )
                    or not _governed_review_phases_complete(
                        state,
                        iteration,
                        set(_GOVERNANCE_REVIEW_PHASES),
                    )
                ):
                    raise CreativeWorkflowError(
                        "workflow_acceptance_contract_mismatch"
                    )
    for iteration in iterations:
        candidate = iteration["anchor"].get("candidate")
        if not isinstance(candidate, dict):
            continue
        authorization = candidate.get("workflow_authorization")
        if not isinstance(authorization, dict):
            continue
        source_number = authorization["iteration_number"]
        if not 1 <= source_number <= len(iterations):
            raise CreativeWorkflowError("candidate_workflow_binding_mismatch")
        source_candidate = iterations[source_number - 1]["anchor"].get(
            "candidate"
        )
        if not isinstance(source_candidate, dict) or source_candidate != candidate:
            raise CreativeWorkflowError("candidate_workflow_binding_mismatch")
        parent_locator = iterations[source_number - 1]["anchor"][
            "parent_candidate"
        ]
        matching_attempts = [
            attempt
            for attempt in iterations[source_number - 1]["render_attempts"]
            if attempt["status"] == "completed"
            and attempt["reservation_revision"]
            == authorization["reservation_revision"]
            and attempt["operation_id"] == authorization["operation_id"]
            and attempt["authoring_revision"]
            == authorization["authoring_revision"]
            and attempt["expected_work_id"]
            == authorization["candidate_work_id"]
            and attempt["expected_candidate_id"] == authorization["candidate_id"]
            and attempt["parent_candidate"] == parent_locator
        ]
        if len(matching_attempts) != 1:
            raise CreativeWorkflowError("candidate_workflow_binding_mismatch")
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
        terminated_at = state["termination"]["terminated_at_utc"]
        if terminated_at > updated or terminated_at < created:
            raise CreativeWorkflowError("invalid_workflow_timestamp")
        if iterations and terminated_at < iterations[-1]["closed_at_utc"]:
            raise CreativeWorkflowError("invalid_workflow_timestamp")
        selected_candidate = state["termination"]["selected_candidate"]
        if (
            isinstance(selected_candidate, dict)
            and selected_candidate["verified_at_utc"] > updated
        ):
            raise CreativeWorkflowError("invalid_workflow_timestamp")
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
        _validate_terminal_contract(state)
    usage = state["usage"]
    legacy_usage_keys = {
        "revision_cycles",
        "rollbacks",
        "render_attempts",
        "evidence_items",
        "exceptions",
        "reviews",
    }
    if not isinstance(usage, dict) or frozenset(usage) not in {
        frozenset(legacy_usage_keys),
        frozenset(legacy_usage_keys) | {"derivations"},
    }:
        raise CreativeWorkflowError("invalid_workflow_usage")
    computed = {
        "revision_cycles": sum(item["outcome"] == "revised" for item in iterations),
        "rollbacks": sum(item["outcome"] == "rolled_back" for item in iterations),
        "render_attempts": sum(len(item["render_attempts"]) for item in iterations),
        "evidence_items": sum(len(item["evidence"]) for item in iterations),
        "exceptions": sum(len(item["exceptions"]) for item in iterations),
        "reviews": sum(len(item["reviews"]) for item in iterations),
        "derivations": sum(len(item.get("derivations", [])) for item in iterations),
    }
    if set(usage) == legacy_usage_keys:
        legacy_computed = {
            key: value for key, value in computed.items() if key in legacy_usage_keys
        }
        if usage != legacy_computed or computed["derivations"] != 0:
            raise CreativeWorkflowError("workflow_usage_mismatch")
    elif usage != computed:
        raise CreativeWorkflowError("workflow_usage_mismatch")
    if computed["revision_cycles"] > budget["max_revision_cycles"] or computed[
        "rollbacks"
    ] > budget["max_rollbacks"]:
        raise CreativeWorkflowError("workflow_budget_exceeded")
    if (
        policy_level >= 2
        and isinstance(state["termination"], dict)
        and "open_evidence_ids" not in state["termination"]
    ):
        raise CreativeWorkflowError("termination_open_evidence_missing")


def _claim_lifecycle_is_explicit(state: Mapping[str, Any]) -> bool:
    """Return whether a state carries the additive Claim Lifecycle contract."""

    if state.get("policy") in (
        _CLAIM_LIFECYCLE_POLICY,
        _ACCEPTANCE_GATE_POLICY,
        _SETTLEMENT_POLICY,
        _POLICY,
        _LEGACY_GOVERNANCE_POLICY,
        _GOVERNANCE_POLICY,
    ):
        return True
    for iteration in state.get("iterations", []):
        decision = iteration.get("decision")
        if isinstance(decision, dict) and _DECISION_CLAIM_KEYS.issubset(decision):
            return True
    termination = state.get("termination")
    return isinstance(termination, dict) and "open_evidence_ids" in termination


def _policy_level(policy: object) -> int:
    if policy == _LEGACY_POLICY:
        return 0
    if policy == _CLAIM_LIFECYCLE_POLICY:
        return 1
    if policy == _ACCEPTANCE_GATE_POLICY:
        return 2
    if policy == _SETTLEMENT_POLICY:
        return 3
    if policy == _POLICY:
        return 4
    if policy == _LEGACY_GOVERNANCE_POLICY:
        return 4
    if policy == _GOVERNANCE_POLICY:
        return 5
    raise CreativeWorkflowError("workflow_policy_mismatch")


def _closed_legacy_iteration(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize only the additive record fields used by old revisions."""

    result = _thaw(value)
    assert isinstance(result, dict)
    result.setdefault("derivations", [])
    result.setdefault("forks", [])
    decision = result.get("decision")
    if isinstance(decision, dict):
        decision.setdefault("derivation_ids", [])
    return result


def _validate_render_attempt_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    parent_revision: str,
) -> None:
    if previous == current:
        return
    immutable = {
        "attempt_number",
        "operation_id",
        "expected_work_id",
        "expected_candidate_id",
        "authoring_revision",
        "parent_candidate",
        "requested_at_utc",
    }
    if (
        previous.get("status") != "pending"
        or current.get("status") not in {"completed", "cancelled"}
        or any(previous.get(key) != current.get(key) for key in immutable)
        or previous.get("reservation_revision") is not None
        or previous.get("finished_at_utc") is not None
        or current.get("reservation_revision") != parent_revision
        or current.get("finished_at_utc") is None
    ):
        raise CreativeWorkflowError("workflow_history_render_attempt_rewritten")


def _validate_iteration_transition(
    previous_value: Mapping[str, Any],
    current_value: Mapping[str, Any],
    *,
    parent_revision: str,
    mode: str,
) -> None:
    previous = _closed_legacy_iteration(previous_value)
    current = _closed_legacy_iteration(current_value)
    for key in ("iteration_number", "iteration_id", "opened_at_utc"):
        if previous[key] != current[key]:
            raise CreativeWorkflowError("workflow_history_iteration_rewritten")

    previous_anchor = previous["anchor"]
    current_anchor = current["anchor"]
    for key in (
        "authoring_revision",
        "parent_candidate",
        "authoring_save_sequence",
        "authoring_save_event_sha256",
    ):
        if previous_anchor.get(key) != current_anchor.get(key):
            raise CreativeWorkflowError("workflow_history_iteration_rewritten")
    if previous_anchor["candidate"] is not None and (
        previous_anchor["candidate"] != current_anchor["candidate"]
    ):
        raise CreativeWorkflowError("workflow_history_iteration_rewritten")
    if previous_anchor["candidate"] is None and isinstance(
        current_anchor["candidate"], dict
    ):
        candidate = current_anchor["candidate"]
        managed_completion = (
            candidate["workflow_managed"]
            and previous["status"] == "candidate_pending"
            and current["status"] == "reviewing"
        )
        audit_attachment = (
            not candidate["workflow_managed"]
            and mode == "audit"
            and previous["status"] == "reviewing"
            and current["status"] == "reviewing"
            and not previous["render_attempts"]
            and not current["render_attempts"]
        )
        if not managed_completion and not audit_attachment:
            raise CreativeWorkflowError("workflow_history_candidate_injected")

    for field in ("reviews", "evidence", "exceptions"):
        old_records = previous[field]
        new_records = current[field]
        if len(new_records) < len(old_records) or new_records[: len(old_records)] != old_records:
            raise CreativeWorkflowError("workflow_history_claim_record_rewritten")
    old_derivations = previous["derivations"]
    new_derivations = current["derivations"]
    if (
        len(new_derivations) < len(old_derivations)
        or new_derivations[: len(old_derivations)] != old_derivations
    ):
        raise CreativeWorkflowError("workflow_history_derivation_rewritten")

    old_forks = previous["forks"]
    new_forks = current["forks"]
    if (
        len(new_forks) < len(old_forks)
        or len(new_forks) > len(old_forks) + 1
        or new_forks[: len(old_forks)] != old_forks
    ):
        raise CreativeWorkflowError("workflow_history_fork_rewritten")

    old_attempts = previous["render_attempts"]
    new_attempts = current["render_attempts"]
    if not len(old_attempts) <= len(new_attempts) <= len(old_attempts) + 1:
        raise CreativeWorkflowError("workflow_history_render_attempt_rewritten")
    for old_attempt, new_attempt in zip(old_attempts, new_attempts):
        _validate_render_attempt_transition(
            old_attempt,
            new_attempt,
            parent_revision=parent_revision,
        )
    if len(new_attempts) == len(old_attempts) + 1:
        appended_attempt = new_attempts[-1]
        if (
            appended_attempt.get("status") != "pending"
            or appended_attempt.get("reservation_revision") is not None
            or appended_attempt.get("finished_at_utc") is not None
            or old_attempts != new_attempts[:-1]
        ):
            raise CreativeWorkflowError(
                "workflow_history_render_attempt_rewritten"
            )

    old_decision = previous["decision"]
    new_decision = current["decision"]
    if old_decision is not None and old_decision != new_decision:
        raise CreativeWorkflowError("workflow_history_decision_rewritten")
    if old_decision is not None and (
        any(previous[field] != current[field] for field in (
            "reviews",
            "evidence",
            "exceptions",
            "derivations",
            "forks",
            "render_attempts",
        ))
        or previous_anchor["candidate"] != current_anchor["candidate"]
    ):
        raise CreativeWorkflowError("workflow_history_after_decision_rewritten")
    if (
        old_decision is None
        and isinstance(new_decision, dict)
        and new_decision["disposition"] != "rollback"
        and (
            any(
                previous[field] != current[field]
                for field in (
                    "reviews",
                    "evidence",
                    "exceptions",
                    "derivations",
                    "forks",
                    "render_attempts",
                )
            )
            or previous_anchor["candidate"] != current_anchor["candidate"]
        )
    ):
        raise CreativeWorkflowError("workflow_history_decision_inputs_rewritten")


def _validate_composition_governance_transition(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> None:
    old = previous.get("governance")
    new = current.get("governance")
    if old is None:
        if new is None:
            return
        # A legacy state may acquire the additive contract once.  Its start
        # marker keeps already-active historical work readable.
        if not isinstance(new, Mapping):
            raise CreativeWorkflowError("invalid_composition_governance")
        previous_iterations = previous.get("iterations", [])
        expected_start = 1
        if previous_iterations:
            last = previous_iterations[-1]
            has_activity = any(
                last.get(field)
                for field in (
                    "reviews",
                    "evidence",
                    "exceptions",
                    "derivations",
                    "forks",
                    "render_attempts",
                )
            ) or last.get("decision") is not None or last.get("anchor", {}).get(
                "candidate"
            ) is not None
            expected_start = len(previous_iterations) + (1 if has_activity else 0)
        expected_initial_hash = (
            None
            if not isinstance(previous.get("work_charter"), Mapping)
            else canonical_json_sha256(previous["work_charter"])
        )
        if (
            new.get("profile") != _COMPOSITION_GOVERNANCE_PROFILE
            or new.get("enforcement_started_iteration")
            != max(1, expected_start)
            or new.get("initial_charter_sha256") != expected_initial_hash
        ):
            raise CreativeWorkflowError("invalid_composition_governance_upgrade")
        if new["amendments"] or new["amendment_preflights"]:
            raise CreativeWorkflowError("invalid_composition_governance_upgrade")
        if len(new["composition_maps"]) > 1:
            raise CreativeWorkflowError("invalid_composition_governance_upgrade")
        if new["composition_maps"]:
            if (
                previous["status"] != "reviewing"
                or current["status"] != "reviewing"
                or current["iterations"] != previous["iterations"]
                or not previous_iterations
                or new["composition_maps"][0]["iteration_number"]
                != previous_iterations[-1]["iteration_number"]
            ):
                raise CreativeWorkflowError("invalid_composition_governance_upgrade")
        elif (
            current["status"] == "reviewing"
            and current.get("iterations")
            and _governance_enabled_for_iteration(
                current, current["iterations"][-1]["iteration_number"]
            )
            and any(
                current["iterations"][-1].get(field)
                for field in (
                    "reviews",
                    "evidence",
                    "exceptions",
                    "derivations",
                    "forks",
                    "render_attempts",
                )
            )
        ):
            raise CreativeWorkflowError("composition_map_required_for_iteration_work")
        return
    if not isinstance(old, Mapping) or not isinstance(new, Mapping):
        raise CreativeWorkflowError("composition_governance_downgrade")
    for field in (
        "profile",
        "enforcement_started_iteration",
        "initial_charter_sha256",
    ):
        if old[field] != new[field]:
            # Initial hash is populated only by activation.
            activation_hash = (
                field == "initial_charter_sha256"
                and old[field] is None
                and previous["status"] == "charter_pending"
                and isinstance(new[field], str)
            )
            if not activation_hash:
                raise CreativeWorkflowError("workflow_history_governance_rewritten")
    append_counts: dict[str, int] = {}
    for field in ("composition_maps", "amendment_preflights", "amendments"):
        old_items = old[field]
        new_items = new[field]
        if (
            len(new_items) < len(old_items)
            or len(new_items) > len(old_items) + 1
            or new_items[: len(old_items)] != old_items
        ):
            raise CreativeWorkflowError("workflow_history_governance_rewritten")
        append_counts[field] = len(new_items) - len(old_items)
    if append_counts["composition_maps"]:
        if (
            previous["status"] != "reviewing"
            or current["status"] != "reviewing"
            or previous["iterations"] != current["iterations"]
            or append_counts["amendment_preflights"]
            or append_counts["amendments"]
        ):
            raise CreativeWorkflowError("invalid_composition_map_transition")
        current_number = previous["iterations"][-1]["iteration_number"]
        if new["composition_maps"][-1]["iteration_number"] != current_number:
            raise CreativeWorkflowError("invalid_composition_map_transition")
    if append_counts["amendment_preflights"] != append_counts["amendments"]:
        raise CreativeWorkflowError("charter_amendment_commit_not_atomic")
    if append_counts["amendments"]:
        if (
            previous["status"] != "revision_pending"
            or current["status"] != "revision_pending"
            or append_counts["composition_maps"]
        ):
            raise CreativeWorkflowError("invalid_charter_amendment_transition")
        current_number = previous["iterations"][-1]["iteration_number"]
        appended_amendment = new["amendments"][-1]
        if (
            appended_amendment["committed_in_iteration"] != current_number
            or "authoring_causal_fence" not in appended_amendment
        ):
            raise CreativeWorkflowError("invalid_charter_amendment_transition")
    if not any(append_counts.values()) and old != new:
        comparable_new = copy.deepcopy(dict(new))
        if (
            old["initial_charter_sha256"] is None
            and previous["status"] == "charter_pending"
        ):
            comparable_new["initial_charter_sha256"] = None
        if dict(old) != comparable_new:
            raise CreativeWorkflowError("workflow_history_governance_rewritten")
    if (
        previous["status"] == "reviewing"
        and previous["iterations"]
        and _governance_enabled_for_iteration(
            previous, previous["iterations"][-1]["iteration_number"]
        )
        and _composition_map_record(
            previous, previous["iterations"][-1]["iteration_number"]
        )
        is None
        and current["status"] not in TERMINAL_WORKFLOW_STATUSES
        and not append_counts["composition_maps"]
    ):
        raise CreativeWorkflowError("composition_map_required_for_iteration_work")


def _validate_state_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    parent_revision: str,
) -> None:
    """Validate the semantic delta between two immutable full-state revisions."""

    if _claim_lifecycle_is_explicit(previous) and not _claim_lifecycle_is_explicit(
        current
    ):
        raise CreativeWorkflowError("workflow_claim_lifecycle_downgrade")
    if previous["status"] in TERMINAL_WORKFLOW_STATUSES:
        raise CreativeWorkflowError("workflow_transition_from_terminal")
    if (
        current["parent_revision"] != parent_revision
        or current["sequence"] != previous["sequence"] + 1
    ):
        raise CreativeWorkflowError("workflow_revision_lineage_mismatch")
    immutable_fields = {
        "kind",
        "schema_version",
        "workflow_id",
        "project_id",
        "mode",
        "final_authority",
        "created_at_utc",
        "initial_anchor",
    }
    if any(previous[field] != current[field] for field in immutable_fields):
        raise CreativeWorkflowError("workflow_history_state_rewritten")
    if current["updated_at_utc"] < previous["updated_at_utc"]:
        raise CreativeWorkflowError("invalid_workflow_timestamp")
    if _policy_level(current["policy"]) < _policy_level(previous["policy"]):
        raise CreativeWorkflowError("workflow_claim_lifecycle_downgrade")
    _validate_composition_governance_transition(previous, current)

    previous_budget = dict(previous["budget"])
    current_budget = dict(current["budget"])
    previous_budget.setdefault(
        "max_derivations_per_iteration",
        DEFAULT_MAX_DERIVATIONS_PER_ITERATION,
    )
    current_budget.setdefault(
        "max_derivations_per_iteration",
        DEFAULT_MAX_DERIVATIONS_PER_ITERATION,
    )
    if previous_budget != current_budget:
        raise CreativeWorkflowError("workflow_history_budget_mismatch")

    for field in ("constitution", "work_charter", "active_clauses"):
        if previous[field] not in (None, []) and previous[field] != current[field]:
            raise CreativeWorkflowError("workflow_history_state_rewritten")

    previous_iterations = previous["iterations"]
    current_iterations = current["iterations"]
    if not len(previous_iterations) <= len(current_iterations) <= len(
        previous_iterations
    ) + 1:
        raise CreativeWorkflowError("workflow_history_iteration_rewritten")
    for index, old_iteration in enumerate(previous_iterations):
        new_iteration = current_iterations[index]
        if index < len(previous_iterations) - 1:
            if _closed_legacy_iteration(old_iteration) != _closed_legacy_iteration(
                new_iteration
            ):
                raise CreativeWorkflowError("workflow_history_iteration_rewritten")
        else:
            _validate_iteration_transition(
                old_iteration,
                new_iteration,
                parent_revision=parent_revision,
                mode=current["mode"],
            )
            old_decision = old_iteration.get("decision")
            new_decision = new_iteration.get("decision")
            if old_decision is None and isinstance(new_decision, dict):
                if (
                    _policy_level(current["policy"]) >= 1
                    and not _DECISION_CLAIM_KEYS.issubset(new_decision)
                ):
                    raise CreativeWorkflowError(
                        "workflow_claim_lifecycle_downgrade"
                    )
                if _policy_level(current["policy"]) >= 3 and (
                    "derivation_ids" not in new_decision
                    or "charter_settlement" not in new_decision
                ):
                    raise CreativeWorkflowError(
                        "workflow_settlement_contract_downgrade"
                    )
                if (
                    "revision_contract_profile" in current["policy"]
                    and new_decision["disposition"] == "revise"
                    and "revision_contract" not in new_decision
                ):
                    raise CreativeWorkflowError("revision_contract_required")

    if len(current_iterations) == len(previous_iterations) + 1:
        appended = _closed_legacy_iteration(current_iterations[-1])
        if (
            current["status"] != "reviewing"
            or appended["opened_at_utc"] < previous["updated_at_utc"]
        ):
            raise CreativeWorkflowError("workflow_history_iteration_appended_invalid")

        if not previous_iterations:
            if previous["status"] != "charter_pending":
                raise CreativeWorkflowError(
                    "workflow_history_iteration_appended_invalid"
                )
            expected_authoring_revision = previous["initial_anchor"][
                "authoring_revision"
            ]
            expected_parent_candidate = None
            expected_candidate = None
        else:
            predecessor = _closed_legacy_iteration(current_iterations[-2])
            if predecessor["outcome"] == "revised":
                if previous["status"] != "revision_pending":
                    raise CreativeWorkflowError(
                        "workflow_history_iteration_appended_invalid"
                    )
                expected_authoring_revision = predecessor[
                    "next_authoring_revision"
                ]
                predecessor_candidate = predecessor["anchor"]["candidate"]
                expected_parent_candidate = (
                    _anchor_locator(predecessor_candidate)
                    if isinstance(predecessor_candidate, dict)
                    else copy.deepcopy(
                        predecessor["anchor"]["parent_candidate"]
                    )
                )
                expected_candidate = None
            elif predecessor["outcome"] == "rolled_back":
                if previous["status"] not in {"reviewing", "candidate_pending"}:
                    raise CreativeWorkflowError(
                        "workflow_history_iteration_appended_invalid"
                    )
                expected_authoring_revision = appended["anchor"][
                    "authoring_revision"
                ]
                expected_parent_candidate = appended["anchor"][
                    "parent_candidate"
                ]
                expected_candidate = appended["anchor"]["candidate"]
                if not isinstance(expected_candidate, dict):
                    raise CreativeWorkflowError(
                        "workflow_history_iteration_appended_invalid"
                    )
            else:
                raise CreativeWorkflowError(
                    "workflow_history_iteration_appended_invalid"
                )

        expected_iteration = _new_iteration(
            len(current_iterations),
            authoring_revision=expected_authoring_revision,
            authoring_save_sequence=appended["anchor"].get(
                "authoring_save_sequence"
            ),
            authoring_save_event_sha256=appended["anchor"].get(
                "authoring_save_event_sha256"
            ),
            parent_candidate=expected_parent_candidate,
            candidate=expected_candidate,
            opened_at_utc=appended["opened_at_utc"],
        )
        if appended != expected_iteration:
            raise CreativeWorkflowError("workflow_history_iteration_appended_invalid")

    previous_termination = previous["termination"]
    if previous_termination is not None and previous_termination != current["termination"]:
        raise CreativeWorkflowError("workflow_history_state_rewritten")
    if previous_termination is None and current["termination"] is not None:
        if (
            _policy_level(current["policy"]) >= 3
            and current["termination"]["reason"] == "budget_exhausted"
            and not _workflow_budget_is_exhausted(previous)
        ):
            raise CreativeWorkflowError("workflow_budget_not_exhausted")
        if previous_iterations and current_iterations:
            old_last = _closed_legacy_iteration(previous_iterations[-1])
            new_last = _closed_legacy_iteration(
                current_iterations[len(previous_iterations) - 1]
            )
            if (
                any(
                    old_last[field] != new_last[field]
                    for field in (
                        "reviews",
                        "evidence",
                        "exceptions",
                        "derivations",
                        "forks",
                    )
                )
                or old_last["anchor"]["candidate"]
                != new_last["anchor"]["candidate"]
                or len(old_last["render_attempts"])
                != len(new_last["render_attempts"])
            ):
                raise CreativeWorkflowError(
                    "workflow_history_termination_inputs_rewritten"
                )


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
        actions = ["record_authoring_revision", "terminate"]
        current = state["iterations"][-1]
        if (
            _governance_enabled_for_iteration(
                state, current["iteration_number"]
            )
            and _composition_map_record(
                state, current["iteration_number"]
            )
            is not None
            and not any(
                item["committed_in_iteration"]
                == current["iteration_number"]
                for item in state["governance"]["amendments"]
            )
        ):
            actions.insert(0, "commit_charter_amendment")
            actions.insert(0, "preflight_charter_amendment")
        return actions
    if status == "reviewing":
        current = state["iterations"][-1]
        if (
            _governance_enabled_for_iteration(
                state, current["iteration_number"]
            )
            and _composition_map_record(state, current["iteration_number"])
            is None
        ):
            return ["record_composition_map", "inspect_composition", "terminate"]
        actions = [
            "record_review",
            "record_evidence",
            "register_exception",
            "record_derivation",
            "record_fork",
            "decide",
            "terminate",
        ]
        if "governance" in state:
            actions.insert(0, "inspect_composition")
            actions.insert(-1, "preflight_charter_amendment")
        if current["anchor"]["candidate"] is None:
            actions.append("request_render")
            if state["mode"] == "audit" and not current["render_attempts"]:
                actions.append("attach_existing_candidate_for_audit")
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
    parent_revision = state["parent_revision"]
    if parent_revision is not None:
        parent_state = _validate_revision_directory(
            _revision_path(layout, parent_revision),
            workflow_id=manifest["workflow_id"],
            project_id=manifest["project_id"],
            revision=parent_revision,
        )
        _validate_state_transition(
            parent_state,
            state,
            parent_revision=parent_revision,
        )
    if state["created_at_utc"] != manifest["created_at_utc"]:
        raise CreativeWorkflowError("workflow_timestamp_mismatch")
    if revision is None and state["updated_at_utc"] != manifest["updated_at_utc"]:
        raise CreativeWorkflowError("workflow_timestamp_mismatch")
    if revision is None and state["sequence"] != manifest["current_sequence"]:
        raise CreativeWorkflowError("workflow_lineage_pointer_mismatch")
    _validate_state_derivation_referents(layout.project_root, state)
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
    composition_governance: bool = False,
) -> CreativeWorkflowSnapshot:
    """Create a new optional workflow bound to an immutable authoring revision."""

    if mode not in WORKFLOW_MODES:
        raise CreativeWorkflowError("invalid_workflow_mode")
    if final_authority not in FINAL_AUTHORITIES:
        raise CreativeWorkflowError("invalid_final_authority")
    if not isinstance(composition_governance, bool):
        raise CreativeWorkflowError("invalid_composition_governance_option")
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
            authoring_save_sequence=authoring.save_sequence,
            authoring_save_event_sha256=authoring.save_event_sha256,
            budget=checked_budget,
            final_authority=final_authority,
            timestamp=timestamp,
            composition_governance=composition_governance,
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
        "derivations": sum(len(item.get("derivations", [])) for item in iterations),
    }


def _workflow_budget_is_exhausted(state: Mapping[str, Any]) -> bool:
    """Return whether at least one usable workflow budget is actually spent."""

    budget = state["budget"]
    usage = state["usage"]
    aggregate_pairs = (
        ("revision_cycles", "max_revision_cycles"),
        ("rollbacks", "max_rollbacks"),
    )
    if any(
        budget[limit] > 0 and usage[used] >= budget[limit]
        for used, limit in aggregate_pairs
    ):
        return True
    iterations = state.get("iterations", [])
    if iterations:
        current = iterations[-1]
        per_iteration_pairs = (
            (len(current["render_attempts"]), "max_render_attempts_per_iteration"),
            (len(current["evidence"]), "max_evidence_items_per_iteration"),
            (len(current["exceptions"]), "max_exceptions_per_iteration"),
            (len(current["reviews"]), "max_reviews_per_iteration"),
            (
                len(current.get("derivations", [])),
                "max_derivations_per_iteration",
            ),
        )
        if any(
            budget[limit] > 0 and used >= budget[limit]
            for used, limit in per_iteration_pairs
        ):
            return True
        if len(current.get("forks", [])) >= MAX_FORKS_PER_ITERATION:
            return True
    return state["sequence"] >= MAX_WORKFLOW_HISTORY - 1


def _upgrade_legacy_derivation_shape_for_transition(
    state: dict[str, Any],
) -> None:
    """Close additive workflow fields before publishing a new revision."""

    state["budget"].setdefault(
        "max_derivations_per_iteration",
        DEFAULT_MAX_DERIVATIONS_PER_ITERATION,
    )
    for iteration in state["iterations"]:
        iteration.setdefault("derivations", [])
        iteration.setdefault("forks", [])
        decision = iteration.get("decision")
        if isinstance(decision, dict):
            decision.setdefault("derivation_ids", [])
    state["usage"].setdefault("derivations", 0)
    current = state["iterations"][-1] if state["iterations"] else None
    current_decision = (
        current.get("decision") if isinstance(current, Mapping) else None
    )
    if (
        state.get("status") == "revision_pending"
        and isinstance(current_decision, Mapping)
        and current_decision.get("disposition") == "revise"
        and "revision_contract" not in current_decision
        and "revision_contract_profile" not in state["policy"]
    ):
        # A historical pending revision cannot retroactively acquire a
        # pre-edit contract.  Let it complete exactly once under its recorded
        # policy; the newly opened iteration upgrades on its next transition.
        return
    target_policy = (
        _GOVERNANCE_POLICY if "governance" in state else _POLICY
    )
    if _policy_level(state["policy"]) < _policy_level(target_policy):
        state["policy"] = copy.deepcopy(target_policy)


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
            _upgrade_legacy_derivation_shape_for_transition(state)
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
                state["updated_at_utc"],
                state["created_at_utc"],
                current.updated_at_utc,
            )
            _refresh_usage(state)
            _validate_state_document(state)
            _validate_state_transition(
                current.state,
                state,
                parent_revision=current.revision,
            )
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
        governance = state.get("governance")
        if isinstance(governance, dict):
            governance["initial_charter_sha256"] = canonical_json_sha256(charter)
        timestamp = _transition_timestamp(state)
        state["iterations"] = [
            _new_iteration(
                1,
                authoring_revision=state["initial_anchor"]["authoring_revision"],
                authoring_save_sequence=state["initial_anchor"].get(
                    "authoring_save_sequence"
                ),
                authoring_save_event_sha256=state["initial_anchor"].get(
                    "authoring_save_event_sha256"
                ),
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


def inspect_workflow_composition(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    revision: str | None = None,
    composition_map: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the current charter claim index and a read-only whole-score mirror.

    A draft map may be supplied for inspection before it is recorded.  No map,
    score, workflow revision or candidate is changed by this operation.
    """

    snapshot = open_creative_workflow(
        project_root, workflow_id=workflow_id, revision=revision
    )
    state = snapshot.state
    if state["status"] in {"disabled", "charter_pending"}:
        raise CreativeWorkflowError("creative_workflow_not_activated")
    iteration = state["iterations"][-1]
    number = iteration["iteration_number"]
    charter = _effective_charter_for_iteration(state, number)
    claim_index = _charter_claim_index(charter)
    stored = _composition_map_record(state, number)
    selected_map: Mapping[str, Any] | None
    source: str
    if composition_map is None:
        selected_map = None if stored is None else stored["document"]
        source = "none" if stored is None else "recorded"
    else:
        try:
            # Draft inspection must be able to expose stale charter bindings
            # as questions.  The durable record boundary below performs the
            # strict current-claim check before a map can govern work.
            selected_map = normalize_composition_map(composition_map)
        except CompositionMapError as exc:
            _raise_composition_map_error(exc)
        source = "draft"
    try:
        authoring = open_authoring_project(
            project_root,
            revision=iteration["anchor"]["authoring_revision"],
        )
    except AuthoringProjectError as exc:
        raise CreativeWorkflowError("workflow_authoring_revision_unavailable") from exc
    inspection: dict[str, Any] | None = None
    phase_questions: dict[str, list[dict[str, Any]]] = {}
    if selected_map is not None:
        try:
            inspection = inspect_composition_map(
                authoring.documents["score"],
                selected_map,
                [item["claim_id"] for item in claim_index["claims"]],
            )
        except CompositionMapError as exc:
            _raise_composition_map_error(exc)
        charter_sha256 = canonical_json_sha256(charter)
        phase_questions = {
            phase: _phase_review_questions(
                inspection,
                phase=phase,
                effective_charter_sha256=charter_sha256,
            )
            for phase in sorted(_GOVERNANCE_REVIEW_PHASES)
        }
    return {
        "kind": "tianlai.workflow_composition_inspection",
        "schema_version": WORKFLOW_VERSION,
        "ok": True,
        "read_only": True,
        "workflow_id": snapshot.workflow_id,
        "workflow_revision": snapshot.revision,
        "iteration_number": number,
        "authoring_revision": iteration["anchor"]["authoring_revision"],
        "effective_work_charter": copy.deepcopy(charter),
        "effective_charter_sha256": canonical_json_sha256(charter),
        "charter_claim_index": claim_index,
        "composition_map_source": source,
        "composition_map": (
            None if selected_map is None else copy.deepcopy(dict(selected_map))
        ),
        "inspection": inspection,
        "inspection_sha256": (
            None if inspection is None else canonical_json_sha256(inspection)
        ),
        "review_questions": phase_questions,
        "authority_boundary": {
            "aesthetic_score": False,
            "automatic_edit": False,
            "audio_audition": False,
            "facts_are_not_acceptance": True,
        },
    }


def record_workflow_composition_map(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    composition_map: Mapping[str, Any],
) -> CreativeWorkflowSnapshot:
    """Freeze exactly one current-work sequence map for the active iteration."""

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        number = iteration["iteration_number"]
        if "governance" not in state:
            governance = _empty_composition_governance(
                enforcement_started_iteration=number
            )
            governance["initial_charter_sha256"] = canonical_json_sha256(
                state["work_charter"]
            )
            state["governance"] = governance
            state["policy"] = copy.deepcopy(_GOVERNANCE_POLICY)
        if not _governance_enabled_for_iteration(state, number):
            raise CreativeWorkflowError("composition_governance_begins_next_iteration")
        if _composition_map_record(state, number) is not None:
            raise CreativeWorkflowError("composition_map_already_recorded")
        if (
            any(
                iteration.get(field)
                for field in (
                    "reviews",
                    "evidence",
                    "exceptions",
                    "derivations",
                    "forks",
                    "render_attempts",
                )
            )
            or iteration["decision"] is not None
        ):
            raise CreativeWorkflowError("composition_map_must_precede_iteration_work")
        charter = _effective_charter_for_iteration(state, number)
        try:
            normalized = validate_composition_map(
                composition_map,
                charter_claim_ids=_charter_claim_ids(charter),
            )
        except CompositionMapError as exc:
            _raise_composition_map_error(exc)
        try:
            authoring = open_authoring_project(
                layout.project_root,
                revision=iteration["anchor"]["authoring_revision"],
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError(
                "workflow_authoring_revision_unavailable"
            ) from exc
        try:
            inspection = inspect_composition_map(
                authoring.documents["score"],
                normalized,
                _charter_claim_ids(charter),
            )
        except CompositionMapError as exc:
            _raise_composition_map_error(exc)
        missing_event_ids = sorted(
            {
                event_id
                for node in inspection["node_facts"]
                for event_id in node["established_material"][
                    "missing_event_ids"
                ]
            }
        )
        missing_part_ids = sorted(
            {
                part_id
                for node in inspection["node_facts"]
                for part_id in node["role_part_coverage"]["missing_part_ids"]
            }
        )
        if missing_event_ids or missing_part_ids:
            _raise_composition_map_error(
                CompositionMapError(
                    "score_referent_not_found",
                    (
                        "a recorded composition map may only cite event and part "
                        "identities present in its bound score"
                    ),
                    details={
                        "event_ids": missing_event_ids,
                        "part_ids": missing_part_ids,
                    },
                )
            )
        charter_sha256 = canonical_json_sha256(charter)
        if any(
            len(
                _phase_review_questions(
                    inspection,
                    phase=phase,
                    effective_charter_sha256=charter_sha256,
                )
            )
            > MAX_REVIEW_QUESTION_ANSWERS
            for phase in _GOVERNANCE_REVIEW_PHASES
        ):
            raise CreativeWorkflowError("review_question_budget_exceeded")
        timestamp = _transition_timestamp(state, iteration["opened_at_utc"])
        governance = state["governance"]
        governance["composition_maps"].append(
            {
                "iteration_number": number,
                "authoring_revision": iteration["anchor"]["authoring_revision"],
                "effective_charter_sha256": charter_sha256,
                "score_sha256": inspection["score_sha256"],
                "composition_map_sha256": composition_map_sha256(normalized),
                "document": normalized,
                "recorded_at_utc": timestamp,
            }
        )

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def _require_authoring_anchor_unchanged(
    authoring: Any,
    *,
    state: Mapping[str, Any],
    iteration: Mapping[str, Any],
) -> None:
    anchor = iteration["anchor"]
    if (
        authoring.project_id != state["project_id"]
        or authoring.revision != anchor["authoring_revision"]
        or authoring.save_sequence != anchor.get("authoring_save_sequence")
        or authoring.save_event_sha256
        != anchor.get("authoring_save_event_sha256")
    ):
        raise CreativeWorkflowError(
            "charter_amendment_must_precede_authoring_change"
        )


def _computed_charter_amendment_preflight(
    state: Mapping[str, Any],
    *,
    iteration: Mapping[str, Any],
    proposal: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    number = iteration["iteration_number"]
    map_record = _composition_map_record(state, number)
    if map_record is None:
        raise CreativeWorkflowError("composition_map_required_for_amendment")
    charter = _effective_charter_for_iteration(state, number)
    try:
        preflight = preflight_charter_amendment(
            charter,
            proposal,
            composition_map_dependencies=map_record["document"],
            derivations=iteration.get("derivations", []),
            reviews=iteration.get("reviews", []),
            evidence=iteration.get("evidence", []),
        )
        acknowledgement = charter_amendment_cost_acknowledgement(preflight)
        hypothetical = commit_charter_amendment_ledger(
            state["work_charter"],
            _core_amendment_entries(state),
            proposal=preflight["proposal"],
            preflight=preflight,
            cost_acknowledgement=acknowledgement,
        )
    except CharterAmendmentError as exc:
        _raise_charter_amendment_error(exc)
    effective = _normalize_work_charter(hypothetical["effective_charter"])
    if effective != hypothetical["effective_charter"]:
        raise CreativeWorkflowError("invalid_effective_work_charter")
    return preflight, acknowledgement, effective


def preflight_workflow_charter_amendment(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    revision: str | None = None,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    """Compute, but do not activate, the exact cost of one bounded amendment."""

    snapshot = open_creative_workflow(
        project_root, workflow_id=workflow_id, revision=revision
    )
    state = snapshot.state
    if state["status"] not in {"reviewing", "revision_pending"}:
        raise CreativeWorkflowError(
            "charter_amendment_preflight_requires_reviewing_or_revision_pending"
        )
    iteration = state["iterations"][-1]
    number = iteration["iteration_number"]
    if not _governance_enabled_for_iteration(state, number):
        raise CreativeWorkflowError("composition_governance_begins_next_iteration")
    if any(
        item["committed_in_iteration"] == number
        for item in state["governance"]["amendments"]
    ):
        raise CreativeWorkflowError("charter_amendment_already_committed")
    try:
        current_authoring = open_authoring_project(project_root)
    except AuthoringProjectError as exc:
        raise CreativeWorkflowError("authoring_project_unavailable") from exc
    _require_authoring_anchor_unchanged(
        current_authoring,
        state=state,
        iteration=iteration,
    )
    preflight, acknowledgement, effective = _computed_charter_amendment_preflight(
        state,
        iteration=iteration,
        proposal=proposal,
    )
    return {
        "kind": "tianlai.workflow_charter_amendment_preflight_result",
        "schema_version": WORKFLOW_VERSION,
        "ok": True,
        "read_only": True,
        "active": False,
        "workflow_id": snapshot.workflow_id,
        "workflow_revision": snapshot.revision,
        "iteration_number": number,
        "authoring_revision": iteration["anchor"]["authoring_revision"],
        "preflight": preflight,
        "cost_acknowledgement_required_for_commit": acknowledgement,
        "hypothetical_effective_charter": effective,
        "hypothetical_effective_charter_sha256": canonical_json_sha256(effective),
        "next_step": (
            "Narrow or abandon the proposal if the cost is disproportionate; "
            "otherwise make a revise decision, then echo this exact cost and "
            "preflight hash before changing the score."
        ),
    }


def commit_workflow_charter_amendment(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    proposal: Mapping[str, Any],
    expected_preflight_sha256: str,
    cost_acknowledgement: Mapping[str, Any],
) -> CreativeWorkflowSnapshot:
    """Append one exact preflight-bound amendment for the next iteration."""

    checked_preflight_sha256 = _checked_revision(
        expected_preflight_sha256,
        code="invalid_charter_amendment_preflight_hash",
    )

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "revision_pending")
        iteration = _current_iteration(state)
        number = iteration["iteration_number"]
        if not _governance_enabled_for_iteration(state, number):
            raise CreativeWorkflowError("composition_governance_begins_next_iteration")
        if any(
            item["committed_in_iteration"] == number
            for item in state["governance"]["amendments"]
        ):
            raise CreativeWorkflowError("charter_amendment_already_committed")
        # A charter change must be acknowledged before any replacement score is
        # saved.  This closes the large-rewrite-then-rationalize escape hatch.
        try:
            current_authoring = open_authoring_project(layout.project_root)
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError("authoring_project_unavailable") from exc
        _require_authoring_anchor_unchanged(
            current_authoring,
            state=state,
            iteration=iteration,
        )
        if current_authoring.save_sequence is None:
            if current_authoring.save_event_sha256 is not None:
                raise CreativeWorkflowError(
                    "authoring_causal_provenance_unavailable"
                )
            causal_fence_sequence = 0
        else:
            if current_authoring.save_event_sha256 is None:
                raise CreativeWorkflowError(
                    "authoring_causal_provenance_unavailable"
                )
            causal_fence_sequence = current_authoring.save_sequence
        authoring_causal_fence = {
            "kind": "tianlai.authoring_causal_fence",
            "schema_version": WORKFLOW_VERSION,
            "project_id": current_authoring.project_id,
            "anchor_revision": current_authoring.revision,
            "save_sequence": causal_fence_sequence,
            "anchor_save_event_sha256": (
                current_authoring.save_event_sha256
            ),
        }
        preflight, required_acknowledgement, effective = (
            _computed_charter_amendment_preflight(
                state,
                iteration=iteration,
                proposal=proposal,
            )
        )
        if preflight["preflight_sha256"] != checked_preflight_sha256:
            raise CreativeWorkflowError("charter_amendment_preflight_stale")
        if dict(cost_acknowledgement) != required_acknowledgement:
            raise CreativeWorkflowError("charter_amendment_cost_not_acknowledged")
        try:
            committed = commit_charter_amendment_ledger(
                state["work_charter"],
                _core_amendment_entries(state),
                proposal=preflight["proposal"],
                preflight=preflight,
                cost_acknowledgement=cost_acknowledgement,
                actual_effective_charter=effective,
            )
        except CharterAmendmentError as exc:
            _raise_charter_amendment_error(exc)
        normalized_effective = _normalize_work_charter(
            committed["effective_charter"]
        )
        if normalized_effective != committed["effective_charter"]:
            raise CreativeWorkflowError("invalid_effective_work_charter")
        timestamp = _transition_timestamp(
            state,
            iteration["opened_at_utc"],
            iteration["decision"]["decided_at_utc"],
        )
        map_record = _composition_map_record(state, number)
        assert map_record is not None
        preflight_record = {
            "iteration_number": number,
            "authoring_revision": iteration["anchor"]["authoring_revision"],
            "effective_charter_sha256": preflight["base_charter_sha256"],
            "composition_map_sha256": map_record["composition_map_sha256"],
            "input_counts": {
                "derivations": len(iteration.get("derivations", [])),
                "reviews": len(iteration["reviews"]),
                "evidence": len(iteration["evidence"]),
            },
            "preflight": preflight,
            "recorded_at_utc": timestamp,
        }
        state["governance"]["amendment_preflights"].append(preflight_record)
        entry = committed["ledger_entry"]
        state["governance"]["amendments"].append(
            {
                "committed_in_iteration": number,
                "effective_from_iteration": number + 1,
                "authoring_revision": iteration["anchor"]["authoring_revision"],
                "preflight_sha256": preflight["preflight_sha256"],
                "entry": entry,
                "committed_at_utc": timestamp,
                "authoring_causal_fence": authoring_causal_fence,
            }
        )

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
        resolved_root = directory.resolve(strict=True)
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("candidate_artifact_path_escape", source=label) from exc
    try:
        root_identity = capture_plain_directory(directory)
        parent_identity = capture_plain_directory(lexical.parent)
        parent_identity.path.relative_to(root_identity.path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CreativeWorkflowError("unsafe_candidate_artifact", source=label) from exc
    if resolved.parent != parent_identity.path:
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
    try:
        revalidate_plain_directory(identity)
    except OSError as exc:
        raise CreativeWorkflowError(
            "candidate_changed_during_verification"
        ) from exc
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
    score_cache: dict[str, _DerivationScoreIndex] = {}
    score_document_cache: dict[str, Mapping[str, Any]] = {}
    validated_derivations: dict[str, str] = {}
    validated_evidence: dict[str, str] = {}
    validated_forks: dict[str, str] = {}
    frozen_budget: dict[str, int] | None = None
    newer_state: dict[str, Any] | None = None
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
        if newer_state is not None:
            _validate_state_transition(
                state,
                newer_state,
                parent_revision=revision,
            )
        newer_state = state
        _validate_state_derivation_referents(
            layout.project_root,
            state,
            score_cache=score_cache,
            score_document_cache=score_document_cache,
            validated_derivations=validated_derivations,
            validated_evidence=validated_evidence,
            validated_forks=validated_forks,
        )
        observed_budget = dict(state["budget"])
        observed_budget.setdefault(
            "max_derivations_per_iteration",
            DEFAULT_MAX_DERIVATIONS_PER_ITERATION,
        )
        if frozen_budget is None:
            frozen_budget = observed_budget
        elif observed_budget != frozen_budget:
            raise CreativeWorkflowError("workflow_history_budget_mismatch")
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
    try:
        revalidate_plain_directory(identity)
    except OSError as exc:
        raise CreativeWorkflowError(
            "candidate_changed_during_verification"
        ) from exc
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


_PHASE_REVIEW_PROMPTS: dict[str, tuple[tuple[str, str], ...]] = {
    "intent": (
        (
            "whole_work_promise",
            "Which composition-map nodes carry the work's promise from its opening state to its ending response, and what current-work evidence supports that reading?",
        ),
        (
            "identity_cost",
            "Which tempting change is deliberately refused in this iteration because it would weaken an identity invariant, scarce resource, or unresolved dramatic question?",
        ),
    ),
    "symbolic_structure": (
        (
            "material_causality",
            "How does each decisive node grow from material already established by this work rather than introducing an unrelated replacement idea?",
        ),
        (
            "whole_work_dependency",
            "If one decisive node were removed or exchanged, where would the whole-work causal chain break, and which charter claim would lose support?",
        ),
    ),
    "orchestration_performance": (
        (
            "role_trajectory",
            "How do instrumental roles change across the complete sequence, and why do those changes serve the work rather than only a locally attractive moment?",
        ),
        (
            "scarcity_and_ending",
            "Where are scarce resources and ending privileges withheld, spent, or transformed, and how does the ending answer what the opening established?",
        ),
    ),
}


def _phase_review_questions(
    inspection: Mapping[str, Any],
    *,
    phase: str,
    effective_charter_sha256: str,
) -> list[dict[str, Any]]:
    """Return deterministic whole-work questions for one reasoning phase."""

    prompts = _PHASE_REVIEW_PROMPTS.get(phase, ())
    questions: list[dict[str, Any]] = []
    for question_kind, prompt in prompts:
        body = {
            "question_kind": question_kind,
            "prompt": prompt,
            "basis": {"source": "whole_work_governance"},
            "location": {
                "score_sha256": inspection["score_sha256"],
                "node_id": None,
                "bar_range": None,
                "event_ids": [],
                "event_ids_truncated": False,
                "part_ids": [],
            },
        }
        identity = canonical_json_sha256(
            {
                "profile": _COMPOSITION_GOVERNANCE_PROFILE,
                "phase": phase,
                "effective_charter_sha256": effective_charter_sha256,
                "composition_map_sha256": inspection["composition_map_sha256"],
                **body,
            }
        )
        questions.append(
            {"question_id": f"workflow-question-{identity[:20]}", **body}
        )
    # The mirror's fact-triggered questions belong to symbolic review.  They
    # are questions, never automatic defect labels or aesthetic scores.
    if phase == "symbolic_structure":
        questions.extend(copy.deepcopy(inspection["questions"]))
    return questions


def _governed_review_context(
    state: Mapping[str, Any],
    *,
    iteration: Mapping[str, Any],
    score_document: Mapping[str, Any],
    phase: str,
) -> dict[str, Any]:
    number = iteration["iteration_number"]
    map_record = _composition_map_record(state, number)
    if map_record is None:
        raise CreativeWorkflowError("composition_map_required_for_review")
    charter = _effective_charter_for_iteration(state, number)
    try:
        inspection = inspect_composition_map(
            dict(score_document),
            map_record["document"],
            _charter_claim_ids(charter),
        )
    except CompositionMapError as exc:
        _raise_composition_map_error(exc)
    if (
        inspection["score_sha256"] != map_record["score_sha256"]
        or inspection["composition_map_sha256"]
        != map_record["composition_map_sha256"]
    ):
        raise CreativeWorkflowError("composition_map_score_binding_mismatch")
    charter_sha256 = canonical_json_sha256(charter)
    score_index = _build_derivation_score_index(score_document)
    questions = _phase_review_questions(
        inspection,
        phase=phase,
        effective_charter_sha256=charter_sha256,
    )
    return {
        "effective_charter": charter,
        "effective_charter_sha256": charter_sha256,
        "charter_claim_index": _charter_claim_index(charter),
        "composition_map": copy.deepcopy(map_record["document"]),
        "composition_map_sha256": map_record["composition_map_sha256"],
        "score_sha256": inspection["score_sha256"],
        "score_event_ids": sorted(score_index.event_positions),
        "inspection": inspection,
        "inspection_sha256": canonical_json_sha256(inspection),
        "questions": questions,
    }


def _review_question_reference_hints(
    question: Mapping[str, Any],
    *,
    available_claims: set[str],
    available_nodes: set[str],
    available_events: set[str],
) -> tuple[set[str], set[str], set[str]]:
    """Recover current-work references named by one deterministic question.

    Inspector questions sometimes name references in ``basis`` (for example,
    both sides of an overlapping node pair) and sometimes in ``location``.
    Missing/stale references are intentionally ignored here: they are the
    subject of the question and therefore cannot be cited as current evidence.
    """

    claims: set[str] = set()
    nodes: set[str] = set()
    events: set[str] = set()

    def collect(value: object, *, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                if isinstance(child_key, str):
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
            if value in available_nodes:
                nodes.add(value)
        elif key in {"event_id", "event_ids"} or key.endswith("_event_ids"):
            if value in available_events:
                events.add(value)

    collect(question.get("basis"))
    collect(question.get("location"))
    return claims, nodes, events


def _normalize_review_question_answers(
    value: Sequence[Mapping[str, Any]],
    *,
    context: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise CreativeWorkflowError("invalid_review_question_answers")
    expected_questions = context["questions"]
    expected_ids = [item["question_id"] for item in expected_questions]
    raw_answers = list(value)
    if len(raw_answers) != len(expected_ids):
        raise CreativeWorkflowError("review_question_coverage_incomplete")
    answers_by_id: dict[str, dict[str, Any]] = {}
    available_claims = {
        item["claim_id"] for item in context["charter_claim_index"]["claims"]
    }
    available_nodes = {
        item["node_id"] for item in context["composition_map"]["nodes"]
    }
    available_events = set(context["score_event_ids"])
    expected_by_id = {
        item["question_id"]: item for item in expected_questions
    }
    node_claims = {
        item["node_id"]: set(item["depends_on_claim_ids"])
        for item in context["composition_map"]["nodes"]
    }
    for index, answer_value in enumerate(raw_answers):
        if not isinstance(answer_value, Mapping) or set(answer_value) != {
            "question_id",
            "answer",
            "claim_ids",
            "node_ids",
            "event_ids",
        }:
            raise CreativeWorkflowError("invalid_review_question_answers")
        question_id = answer_value["question_id"]
        if not isinstance(question_id, str) or question_id in answers_by_id:
            raise CreativeWorkflowError("invalid_review_question_answers")
        claim_ids = list(answer_value["claim_ids"]) if isinstance(
            answer_value["claim_ids"], list
        ) else None
        node_ids = list(answer_value["node_ids"]) if isinstance(
            answer_value["node_ids"], list
        ) else None
        event_ids = list(answer_value["event_ids"]) if isinstance(
            answer_value["event_ids"], list
        ) else None
        if claim_ids is None or node_ids is None or event_ids is None:
            raise CreativeWorkflowError("invalid_review_question_references")
        if (
            any(not isinstance(item, str) for item in claim_ids + node_ids + event_ids)
            or
            len(set(claim_ids)) != len(claim_ids)
            or len(set(node_ids)) != len(node_ids)
            or len(set(event_ids)) != len(event_ids)
            or not set(claim_ids).issubset(available_claims)
            or not set(node_ids).issubset(available_nodes)
            or not set(event_ids).issubset(available_events)
            or not (claim_ids or node_ids or event_ids)
        ):
            raise CreativeWorkflowError("invalid_review_question_references")
        question = expected_by_id.get(question_id)
        if question is not None:
            hinted_claims, hinted_nodes, hinted_events = (
                _review_question_reference_hints(
                    question,
                    available_claims=available_claims,
                    available_nodes=available_nodes,
                    available_events=available_events,
                )
            )
            whole_work_question = question.get("basis") == {
                "source": "whole_work_governance"
            }
            if (
                (whole_work_question and (not claim_ids or not node_ids))
                or (hinted_claims and not hinted_claims.issubset(claim_ids))
                or (hinted_nodes and not hinted_nodes.issubset(node_ids))
                or (hinted_events and not hinted_events.issubset(event_ids))
            ):
                raise CreativeWorkflowError(
                    "review_question_reference_scope_mismatch"
                )
            selected_node_claims = {
                claim_id
                for node_id in node_ids
                for claim_id in node_claims[node_id]
                if claim_id in available_claims
            }
            if (
                (whole_work_question or hinted_nodes)
                and selected_node_claims
                and not selected_node_claims.issubset(claim_ids)
            ):
                raise CreativeWorkflowError(
                    "review_question_reference_scope_mismatch"
                )
        answers_by_id[question_id] = {
            "question_id": question_id,
            "answer": _bounded_text(
                answer_value["answer"],
                field=f"review.question_answers[{index}].answer",
                maximum_bytes=4096,
            ),
            "claim_ids": claim_ids,
            "node_ids": node_ids,
            "event_ids": event_ids,
        }
    if set(answers_by_id) != set(expected_ids):
        raise CreativeWorkflowError("review_question_set_mismatch")
    ordered = [answers_by_id[question_id] for question_id in expected_ids]
    return ordered, sorted(
        {claim_id for answer in ordered for claim_id in answer["claim_ids"]}
    )


def record_workflow_review(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    phase: str,
    reviewer: str,
    perception_basis: str,
    summary: str,
    question_answers: Sequence[Mapping[str, Any]] = (),
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

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        limit = state["budget"]["max_reviews_per_iteration"]
        if len(iteration["reviews"]) >= limit:
            raise CreativeWorkflowError("review_budget_exhausted")
        candidate_id = _candidate_id(iteration)
        if phase in {"render_report", "audio_audition"} and candidate_id is None:
            raise CreativeWorkflowError("candidate_required_for_review")
        timestamp = _transition_timestamp(
            state, iteration["opened_at_utc"]
        )
        governance_fields: dict[str, Any] = {}
        if _governance_enabled_for_iteration(
            state, iteration["iteration_number"]
        ) and phase in _GOVERNANCE_REVIEW_PHASES:
            try:
                authoring = open_authoring_project(
                    layout.project_root,
                    revision=iteration["anchor"]["authoring_revision"],
                )
            except AuthoringProjectError as exc:
                raise CreativeWorkflowError(
                    "workflow_authoring_revision_unavailable"
                ) from exc
            context = _governed_review_context(
                state,
                iteration=iteration,
                score_document=authoring.documents["score"],
                phase=phase,
            )
            normalized_answers, covered_claim_ids = (
                _normalize_review_question_answers(
                    question_answers,
                    context=context,
                )
            )
            governance_fields = {
                "inspection_sha256": context["inspection_sha256"],
                "score_sha256": context["score_sha256"],
                "composition_map_sha256": context[
                    "composition_map_sha256"
                ],
                "effective_charter_sha256": context[
                    "effective_charter_sha256"
                ],
                "claim_ids": covered_claim_ids,
                "question_answers": normalized_answers,
            }
        elif question_answers:
            raise CreativeWorkflowError("review_questions_not_applicable")
        body = {
            "phase": phase,
            "reviewer": reviewer,
            "perception_basis": perception_basis,
            "summary": checked_summary,
            "candidate_id": candidate_id,
            "reviewed_at_utc": timestamp,
            **governance_fields,
        }
        review_id = _review_identity(
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            body=body,
        )
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
        timestamp = _transition_timestamp(
            state, iteration["opened_at_utc"]
        )
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
        evidence_id = _evidence_identity(
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            body=body,
        )
        record = {"evidence_id": evidence_id, **body}
        _validate_evidence(
            record,
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            iteration=iteration,
            active_clause_ids={
                item["clause_id"] for item in state["active_clauses"]
            },
            charter_fields=set(_current_effective_charter(state)),
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
        timestamp = _transition_timestamp(
            state, iteration["opened_at_utc"]
        )
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
        evidence_id = _evidence_identity(
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            body=body,
        )
        record = {"evidence_id": evidence_id, **body}
        _validate_evidence(
            record,
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            iteration=iteration,
            active_clause_ids={
                item["clause_id"] for item in state["active_clauses"]
            },
            charter_fields=set(_current_effective_charter(state)),
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


def _trusted_hard_failure_recheck(
    iteration: dict[str, Any], *, project_root: Path
) -> tuple[list[dict[str, Any]], str | None]:
    """Recheck recorded hard failures and return a point-in-time result hash."""

    historical = [
        item
        for item in iteration["evidence"]
        if item["category"] == "hard_failure"
    ]
    if not historical:
        return [], None
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
        return historical, canonical_json_sha256(readiness)
    return (
        [item for item in historical if item["code"] in current_codes],
        canonical_json_sha256(readiness),
    )


def _unresolved_trusted_hard_failures(
    iteration: dict[str, Any], *, project_root: Path
) -> list[dict[str, Any]]:
    """Reproduce historical hard evidence at the current trusted boundary."""

    unresolved, _readiness_result_sha256 = _trusted_hard_failure_recheck(
        iteration, project_root=project_root
    )
    return unresolved


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
        timestamp = _transition_timestamp(
            state, iteration["opened_at_utc"]
        )
        body = {
            "target_type": target_type,
            **checked,
            "evidence_ids": ids,
            "reusable": reusable,
            "registered_at_utc": timestamp,
        }
        exception_id = _exception_identity(
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            body=body,
        )
        record = {"exception_id": exception_id, **body}
        _validate_exception(
            record,
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
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


def _validate_derivation_governance_reference_scope(
    derivation: Mapping[str, Any],
    *,
    charter: Mapping[str, Any],
    composition_map: Mapping[str, Any],
    iteration: Mapping[str, Any],
) -> None:
    """Require one connected current-work argument, not three ID checkboxes."""

    selected_claims = set(derivation["charter_claim_ids"])
    selected_nodes = set(derivation["composition_map_node_ids"])
    claim_roots = {
        item["claim_id"]: item["field_path"][0]
        for item in _charter_claim_index(charter)["claims"]
    }
    promise_roots = {
        premise["reference"]
        for premise in derivation["premises"]
        if premise["kind"] == "declared_promise"
    }
    premise_claims = {
        claim_id
        for claim_id in selected_claims
        if claim_roots.get(claim_id) in promise_roots
    }
    if promise_roots and {
        claim_roots.get(claim_id) for claim_id in premise_claims
    } != promise_roots:
        raise CreativeWorkflowError(
            "derivation_governance_reference_scope_mismatch"
        )

    nodes_by_id = {
        node["node_id"]: node for node in composition_map["nodes"]
    }
    selected_node_dependencies: set[str] = set()
    for node_id in selected_nodes:
        dependencies = set(nodes_by_id[node_id]["depends_on_claim_ids"])
        if not dependencies.intersection(selected_claims):
            raise CreativeWorkflowError(
                "derivation_governance_reference_scope_mismatch"
            )
        selected_node_dependencies.update(dependencies)
    if promise_roots and not selected_node_dependencies.intersection(
        premise_claims
    ):
        raise CreativeWorkflowError(
            "derivation_governance_reference_scope_mismatch"
        )

    answers_by_id = {
        answer["question_id"]: answer
        for review in iteration["reviews"]
        if "question_answers" in review
        for answer in review["question_answers"]
    }
    derivation_event_ids = set(derivation["anchor"]["event_ids"])
    derivation_event_ids.update(
        event_id
        for premise in derivation["premises"]
        for event_id in premise["event_ids"]
    )
    for question_id in derivation["question_ids"]:
        answer = answers_by_id[question_id]
        if not (
            selected_claims.intersection(answer["claim_ids"])
            or selected_nodes.intersection(answer["node_ids"])
            or derivation_event_ids.intersection(answer["event_ids"])
        ):
            raise CreativeWorkflowError(
                "derivation_governance_reference_scope_mismatch"
            )


def record_workflow_derivation(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    claim: str,
    premises: Sequence[Mapping[str, Any]],
    excluded_alternatives: Sequence[Mapping[str, Any]],
    event_ids: Sequence[str] = (),
    part_ids: Sequence[str] = (),
    start_bar: int | None = None,
    start_beat: float | None = None,
    end_bar: int | None = None,
    end_beat: float | None = None,
    start_seconds: float | None = None,
    end_seconds: float | None = None,
    clause_ids: Sequence[str] = (),
    sacrificed_values: Sequence[str] = (),
    charter_claim_ids: Sequence[str] = (),
    composition_map_node_ids: Sequence[str] = (),
    question_ids: Sequence[str] = (),
) -> CreativeWorkflowSnapshot:
    """Record a passage-level justification for why material had to be this way.

    A derivation is affirmative necessity, not a problem report: it anchors a
    passage of the current authoring revision and states the premises under
    which the excluded alternatives fail.  Event and part references are
    verified against the anchored score at record time; the score's canonical
    hash is embedded so the structural validator can bind the claim later.
    Derivations never block, never trigger an edit and never replace the
    ear's final judgement.
    """

    checked_claim = _bounded_text(claim, field="derivation.claim", maximum_bytes=4096)
    anchor_event_ids = _bounded_text_list(
        list(event_ids), field="derivation.anchor.event_ids", maximum_items=128, item_bytes=256
    )
    anchor_part_ids = _bounded_text_list(
        list(part_ids), field="derivation.anchor.part_ids", maximum_items=64, item_bytes=256
    )
    checked_score_range = _derivation_score_range(
        {
            "start_bar": start_bar,
            "start_beat": start_beat,
            "end_bar": end_bar,
            "end_beat": end_beat,
        }
    )
    if not anchor_event_ids and checked_score_range is None:
        raise CreativeWorkflowError(
            "derivation_anchor_requires_event_or_bar_range"
        )
    checked_premises: list[dict[str, Any]] = []
    for index, premise in enumerate(premises):
        raw = _json_detach(premise, field=f"derivation.premises[{index}]")
        if set(raw) != {"kind", "reference", "event_ids", "artifact_sha256", "artifact_role"}:
            raise CreativeWorkflowError("invalid_derivation_premise")
        checked_premises.append(
            {
                "kind": raw["kind"],
                "reference": raw["reference"],
                "event_ids": _bounded_text_list(
                    raw["event_ids"],
                    field=f"derivation.premises[{index}].event_ids",
                    maximum_items=MAX_DERIVATION_MATERIAL_REFS,
                    item_bytes=256,
                ),
                "artifact_sha256": raw["artifact_sha256"],
                "artifact_role": raw["artifact_role"],
            }
        )
    checked_alternatives: list[dict[str, Any]] = []
    for index, alternative in enumerate(excluded_alternatives):
        raw = _json_detach(
            alternative, field=f"derivation.excluded_alternatives[{index}]"
        )
        if set(raw) != {"alternative", "failure", "premise_indexes"}:
            raise CreativeWorkflowError("invalid_derivation_alternative")
        checked_alternatives.append(
            {
                "alternative": _bounded_text(
                    raw["alternative"],
                    field=f"derivation.excluded_alternatives[{index}].alternative",
                    maximum_bytes=2048,
                ),
                "failure": _bounded_text(
                    raw["failure"],
                    field=f"derivation.excluded_alternatives[{index}].failure",
                    maximum_bytes=2048,
                ),
                "premise_indexes": copy.deepcopy(raw["premise_indexes"]),
            }
        )
    checked_clause_ids = list(clause_ids)
    checked_sacrificed = _bounded_text_list(
        list(sacrificed_values),
        field="derivation.sacrificed_values",
        maximum_items=32,
    )
    checked_charter_claim_ids = list(charter_claim_ids)
    checked_map_node_ids = list(composition_map_node_ids)
    checked_question_ids = list(question_ids)

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        iteration.setdefault("derivations", [])
        limit = state["budget"].get(
            "max_derivations_per_iteration",
            DEFAULT_MAX_DERIVATIONS_PER_ITERATION,
        )
        if len(iteration["derivations"]) >= limit:
            raise CreativeWorkflowError("derivation_budget_exhausted")
        authoring_revision = iteration["anchor"]["authoring_revision"]
        try:
            authoring = open_authoring_project(
                layout.project_root, revision=authoring_revision
            )
            score_document = authoring.documents["score"]
        except (AuthoringProjectError, RuntimeError, ValueError) as exc:
            raise CreativeWorkflowError("derivation_score_unavailable") from exc
        score_index = _build_derivation_score_index(score_document)
        if (
            anchor_event_ids
            or any(premise["event_ids"] for premise in checked_premises)
        ) and not score_index.score.has_stable_event_identity:
            raise CreativeWorkflowError(
                "derivation_requires_stable_event_identity"
            )
        timestamp = _transition_timestamp(state, iteration["opened_at_utc"])
        governance_fields: dict[str, Any] = {}
        if _governance_enabled_for_iteration(
            state, iteration["iteration_number"]
        ):
            map_record = _composition_map_record(
                state, iteration["iteration_number"]
            )
            if map_record is None:
                raise CreativeWorkflowError(
                    "composition_map_required_for_iteration_work"
                )
            charter = _current_effective_charter(state)
            available_claim_ids = set(_charter_claim_ids(charter))
            available_node_ids = {
                node["node_id"] for node in map_record["document"]["nodes"]
            }
            answered_question_ids = {
                answer["question_id"]
                for review in iteration["reviews"]
                if "question_answers" in review
                for answer in review["question_answers"]
            }
            if (
                not checked_charter_claim_ids
                or not checked_map_node_ids
                or not checked_question_ids
                or len(set(checked_charter_claim_ids))
                != len(checked_charter_claim_ids)
                or len(set(checked_map_node_ids)) != len(checked_map_node_ids)
                or len(set(checked_question_ids)) != len(checked_question_ids)
                or not set(checked_charter_claim_ids).issubset(
                    available_claim_ids
                )
                or not set(checked_map_node_ids).issubset(available_node_ids)
                or not set(checked_question_ids).issubset(
                    answered_question_ids
                )
            ):
                raise CreativeWorkflowError(
                    "invalid_derivation_governance_references"
                )
            governance_fields = {
                "effective_charter_sha256": canonical_json_sha256(charter),
                "charter_claim_ids": checked_charter_claim_ids,
                "composition_map_sha256": map_record[
                    "composition_map_sha256"
                ],
                "composition_map_node_ids": checked_map_node_ids,
                "question_ids": checked_question_ids,
            }
        elif checked_charter_claim_ids or checked_map_node_ids or checked_question_ids:
            raise CreativeWorkflowError("derivation_governance_not_applicable")
        body = {
            "anchor": {
                "authoring_revision": authoring_revision,
                "candidate_id": _candidate_id(iteration),
                "score_sha256": score_index.score_sha256,
                "event_ids": anchor_event_ids,
                "part_ids": anchor_part_ids,
                "start_bar": (
                    None if checked_score_range is None else checked_score_range[0]
                ),
                "start_beat": (
                    None if checked_score_range is None else checked_score_range[1]
                ),
                "end_bar": (
                    None if checked_score_range is None else checked_score_range[2]
                ),
                "end_beat": (
                    None if checked_score_range is None else checked_score_range[3]
                ),
                "start_seconds": (
                    None
                    if start_seconds is None
                    else _finite_optional_number(
                        start_seconds, field="derivation.anchor.start_seconds"
                    )
                ),
                "end_seconds": (
                    None
                    if end_seconds is None
                    else _finite_optional_number(
                        end_seconds, field="derivation.anchor.end_seconds"
                    )
                ),
            },
            "claim": checked_claim,
            "premises": checked_premises,
            "clause_ids": checked_clause_ids,
            "excluded_alternatives": checked_alternatives,
            "sacrificed_values": checked_sacrificed,
            "recorded_at_utc": timestamp,
            **governance_fields,
        }
        derivation_id = _derivation_identity(
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            body=body,
        )
        record = {"derivation_id": derivation_id, **body}
        _validate_derivation(
            record,
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            iteration=iteration,
            active_clause_ids={
                item["clause_id"] for item in state["active_clauses"]
            },
            charter_fields=set(_current_effective_charter(state)),
        )
        _validate_derivation_score_referents(record, index=score_index)
        if governance_fields:
            _validate_derivation_governance_reference_scope(
                record,
                charter=charter,
                composition_map=map_record["document"],
                iteration=iteration,
            )
        if any(
            item["derivation_id"] == derivation_id
            for item in iteration["derivations"]
        ):
            raise CreativeWorkflowError("duplicate_derivation_record")
        iteration["derivations"].append(record)

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def record_workflow_fork(
    project_root: str | os.PathLike[str],
    *,
    workflow_id: str,
    expected_revision: str,
    branches: Sequence[Mapping[str, Any]],
    invariant_indexes: Sequence[int],
    event_ids: Sequence[str] = (),
    part_ids: Sequence[str] = (),
    start_bar: int | None = None,
    start_beat: float | None = None,
    end_bar: int | None = None,
    end_beat: float | None = None,
    note: str | None = None,
) -> CreativeWorkflowSnapshot:
    """Declare complete-candidate variant worlds of one work.

    A fork never describes replaceable fragments: each branch is a whole
    rendered candidate, because every possibility deserves to be re-observed
    inside a complete sequence.  The anchor names where the worlds diverge
    on the current authoring revision; ``invariant_indexes`` claim which
    charter identity invariants hold across every branch.
    """

    anchor_event_ids = _bounded_text_list(
        list(event_ids), field="fork.anchor.event_ids", maximum_items=128, item_bytes=256
    )
    anchor_part_ids = _bounded_text_list(
        list(part_ids), field="fork.anchor.part_ids", maximum_items=64, item_bytes=256
    )
    checked_range = _derivation_score_range(
        {
            "start_bar": start_bar,
            "start_beat": start_beat,
            "end_bar": end_bar,
            "end_beat": end_beat,
        },
        code="invalid_fork_bar_range",
        field_prefix="fork",
    )
    if not anchor_event_ids and checked_range is None:
        raise CreativeWorkflowError("fork_anchor_empty")
    checked_indexes = list(invariant_indexes)
    if (
        not checked_indexes
        or any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in checked_indexes
        )
        or len(set(checked_indexes)) != len(checked_indexes)
    ):
        raise CreativeWorkflowError("fork_invariants_required")
    checked_branches: list[dict[str, Any]] = []
    for index, branch in enumerate(branches):
        raw = _json_detach(branch, field=f"fork.branches[{index}]")
        if set(raw) != {"candidate", "stance", "derivation_ids"}:
            raise CreativeWorkflowError("invalid_fork_branch")
        _validate_candidate_locator(raw["candidate"])
        derivation_refs = raw["derivation_ids"]
        if (
            not isinstance(derivation_refs, list)
            or any(not isinstance(item, str) for item in derivation_refs)
        ):
            raise CreativeWorkflowError("fork_derivation_not_found")
        checked_branches.append(
            {
                "candidate": copy.deepcopy(raw["candidate"]),
                "stance": _bounded_text(
                    raw["stance"],
                    field=f"fork.branches[{index}].stance",
                    maximum_bytes=2048,
                ),
                "derivation_ids": list(derivation_refs),
            }
        )
    checked_note = (
        None
        if note is None
        else _bounded_text(note, field="fork.note", maximum_bytes=4096)
    )

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        iteration.setdefault("forks", [])
        if len(iteration["forks"]) >= MAX_FORKS_PER_ITERATION:
            raise CreativeWorkflowError("fork_budget_exhausted")
        invariant_count = _charter_invariant_count(
            _effective_charter_for_iteration(
                state, iteration["iteration_number"]
            )
        )
        if invariant_count <= 0:
            raise CreativeWorkflowError("fork_invariants_unavailable")
        for index in checked_indexes:
            if index >= invariant_count:
                raise CreativeWorkflowError("fork_invariant_index_out_of_range")
        authoring_revision = iteration["anchor"]["authoring_revision"]
        try:
            authoring = open_authoring_project(
                layout.project_root, revision=authoring_revision
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError("fork_score_unavailable") from exc
        score_index = _build_derivation_score_index(authoring.documents["score"])
        if anchor_event_ids and not score_index.score.has_stable_event_identity:
            raise CreativeWorkflowError("fork_requires_stable_event_identity")
        recorded_candidates: set[tuple[str, str, str]] = set()
        for item in state["iterations"]:
            candidate = item["anchor"].get("candidate")
            if isinstance(candidate, dict):
                locator = _anchor_locator(candidate)
                assert locator is not None
                recorded_candidates.add(
                    (
                        locator["work_id"],
                        locator["candidate_id"],
                        locator["manifest_sha256"],
                    )
                )
        derivation_ids_available = {
            item["derivation_id"] for item in iteration.get("derivations", [])
        }
        for branch in checked_branches:
            candidate = branch["candidate"]
            locator = (
                candidate["work_id"],
                candidate["candidate_id"],
                candidate["manifest_sha256"],
            )
            if locator not in recorded_candidates:
                raise CreativeWorkflowError("fork_branch_candidate_not_recorded")
            if not set(branch["derivation_ids"]).issubset(derivation_ids_available):
                raise CreativeWorkflowError("fork_derivation_not_found")
        timestamp = _transition_timestamp(state, iteration["opened_at_utc"])
        body = {
            "anchor": {
                "authoring_revision": authoring_revision,
                "score_sha256": score_index.score_sha256,
                "event_ids": anchor_event_ids,
                "part_ids": anchor_part_ids,
                "start_bar": None if checked_range is None else checked_range[0],
                "start_beat": None if checked_range is None else checked_range[1],
                "end_bar": None if checked_range is None else checked_range[2],
                "end_beat": None if checked_range is None else checked_range[3],
            },
            "invariant_indexes": checked_indexes,
            "branches": checked_branches,
            "note": checked_note,
            "recorded_at_utc": timestamp,
        }
        fork_id = _fork_identity(
            workflow_id=state["workflow_id"],
            iteration_number=iteration["iteration_number"],
            body=body,
        )
        record = {"fork_id": fork_id, **body}
        _validate_fork(
            record,
            workflow_id=state["workflow_id"],
            iteration=iteration,
            derivation_ids_available=derivation_ids_available,
            invariant_count=invariant_count,
        )
        _validate_fork_score_referents(record, index=score_index)
        current_candidate = iteration["anchor"].get("candidate")
        current_locator: tuple[str, str, str] | None = None
        if isinstance(current_candidate, dict):
            current = _anchor_locator(current_candidate)
            assert current is not None
            current_locator = (
                current["work_id"],
                current["candidate_id"],
                current["manifest_sha256"],
            )
        _validate_fork_candidate_referents(
            record,
            recorded_candidates=recorded_candidates,
            current_candidate=current_locator,
        )
        if any(item["fork_id"] == fork_id for item in iteration["forks"]):
            raise CreativeWorkflowError("duplicate_fork_record")
        iteration["forks"].append(record)

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def _governed_review_phases_complete(
    state: Mapping[str, Any],
    iteration: Mapping[str, Any],
    phases: set[str],
) -> bool:
    number = iteration["iteration_number"]
    if not _governance_enabled_for_iteration(state, number):
        return phases.issubset({item["phase"] for item in iteration["reviews"]})
    map_record = _composition_map_record(state, number)
    if map_record is None:
        return False
    charter_sha256 = canonical_json_sha256(
        _effective_charter_for_iteration(state, number)
    )
    for phase in phases:
        if not any(
            review["phase"] == phase
            and "question_answers" in review
            and review["composition_map_sha256"]
            == map_record["composition_map_sha256"]
            and review["score_sha256"] == map_record["score_sha256"]
            and review["effective_charter_sha256"] == charter_sha256
            for review in iteration["reviews"]
        ):
            return False
    return True


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
        required_phases = (
            set(_GOVERNANCE_REVIEW_PHASES)
            if _governance_enabled_for_iteration(
                state, iteration["iteration_number"]
            )
            else {"symbolic_structure", "orchestration_performance"}
        )
        if not _governed_review_phases_complete(
            state, iteration, required_phases
        ):
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
        requested_at = _transition_timestamp(
            state, iteration["opened_at_utc"]
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
                "requested_at_utc": requested_at,
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
        timestamp = _transition_timestamp(
            state,
            iteration["opened_at_utc"],
            pending[0]["requested_at_utc"],
        )
        pending[0]["reservation_revision"] = expected
        pending[0]["status"] = "cancelled"
        pending[0]["finished_at_utc"] = timestamp
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
        if state["mode"] != "audit":
            raise CreativeWorkflowError("candidate_attachment_requires_audit_mode")
        iteration = _current_iteration(state)
        if iteration["anchor"]["candidate"] is not None or iteration["render_attempts"]:
            raise CreativeWorkflowError("iteration_candidate_already_bound")
        anchor = _verified_candidate_anchor(
            candidate_path,
            project_id=state["project_id"],
            authoring_revision=iteration["anchor"]["authoring_revision"],
            expected_authorization=None,
        )
        anchor["verified_at_utc"] = _transition_timestamp(
            state, iteration["opened_at_utc"], anchor["verified_at_utc"]
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
        timestamp = _transition_timestamp(
            state,
            iteration["opened_at_utc"],
            attempt["requested_at_utc"],
            anchor["verified_at_utc"],
        )
        anchor["verified_at_utc"] = timestamp
        attempt["reservation_revision"] = expected
        attempt["status"] = "completed"
        attempt["finished_at_utc"] = timestamp
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


def _baseline_candidate_for_contract(
    state: Mapping[str, Any], iteration: Mapping[str, Any]
) -> tuple[dict[str, str] | None, int | None]:
    candidate = iteration["anchor"].get("candidate")
    if isinstance(candidate, Mapping):
        return _anchor_locator(candidate), iteration["iteration_number"]
    locator = iteration["anchor"].get("parent_candidate")
    if locator is None:
        return None, None
    for source in reversed(state["iterations"][: iteration["iteration_number"]]):
        source_candidate = source["anchor"].get("candidate")
        if (
            isinstance(source_candidate, Mapping)
            and _anchor_locator(source_candidate) == locator
        ):
            return copy.deepcopy(locator), source["iteration_number"]
    raise CreativeWorkflowError("revision_baseline_candidate_not_found")


def _build_revision_contract(
    *,
    state: Mapping[str, Any],
    iteration: Mapping[str, Any],
    project_root: Path,
    decision: Mapping[str, Any],
    revision_scope: object,
    withdrawal_condition: object,
) -> dict[str, Any]:
    if revision_scope is None or withdrawal_condition is None:
        raise CreativeWorkflowError("revision_contract_required")
    normalized_scope = _normalize_revision_scope(revision_scope)
    checked_withdrawal = _bounded_text(
        withdrawal_condition,
        field="revision_contract.withdrawal_condition",
        maximum_bytes=4096,
    )
    try:
        baseline_authoring = open_authoring_project(
            project_root, revision=iteration["anchor"]["authoring_revision"]
        )
        causal_head = open_authoring_project(project_root)
    except AuthoringProjectError as exc:
        raise CreativeWorkflowError("authoring_revision_unavailable") from exc
    if (
        baseline_authoring.project_id != state["project_id"]
        or causal_head.project_id != state["project_id"]
    ):
        raise CreativeWorkflowError("authoring_project_identity_changed")
    expected_causal_head = baseline_authoring.revision
    if iteration["iteration_number"] > 1:
        previous = state["iterations"][iteration["iteration_number"] - 2]
        if previous.get("outcome") == "rolled_back":
            expected_causal_head = previous["anchor"]["authoring_revision"]
    if causal_head.revision != expected_causal_head:
        raise CreativeWorkflowError("authoring_changed_before_revision_contract")
    if causal_head.save_sequence is None or causal_head.save_event_sha256 is None:
        raise CreativeWorkflowError("authoring_causal_provenance_unavailable")
    candidate, source_number = _baseline_candidate_for_contract(state, iteration)
    target_ids = sorted(
        item["evidence_id"]
        for item in decision.get("evidence_dispositions", [])
        if item["disposition"] == "revision_target"
    )
    contract = {
        "kind": "tianlai.workflow_revision_contract",
        "schema_version": 1,
        "contract_id": f"revision-contract-{iteration['iteration_number']:04d}",
        "baseline": {
            "authoring_revision": baseline_authoring.revision,
            "document_sha256": {
                name: baseline_authoring.document_revisions[name]
                for name in sorted(_REVISION_DOCUMENTS)
            },
            "candidate": candidate,
            "candidate_source_iteration_number": source_number,
        },
        "revision_target_evidence_ids": target_ids,
        "revision_scope": normalized_scope,
        "withdrawal_condition": checked_withdrawal,
        "authoring_causal_fence": {
            "anchor_revision": causal_head.revision,
            "save_sequence": causal_head.save_sequence,
            "anchor_save_event_sha256": causal_head.save_event_sha256,
        },
        "contract_sha256": "0" * 64,
    }
    contract["contract_sha256"] = _revision_contract_hash(
        contract, decision=decision
    )
    return contract


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
    derivation_ids: Sequence[str] = (),
    review_ids: Sequence[str] = (),
    evidence_dispositions: Sequence[Mapping[str, Any]] = (),
    charter_settlement: Sequence[Mapping[str, Any]] = (),
    revision_contract: Mapping[str, Any] | None = None,
    prior_revision_assessment: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if final_authority not in FINAL_AUTHORITIES or perception_basis not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_decision_authority")
    checked_settlement: list[dict[str, Any]] = []
    if not isinstance(charter_settlement, (list, tuple)):
        raise CreativeWorkflowError("invalid_charter_settlement")
    for index, settlement_item in enumerate(charter_settlement):
        raw = _json_detach(
            settlement_item, field=f"decision.charter_settlement[{index}]"
        )
        if set(raw) != {"target", "status", "rationale", "basis_ids", "event_ids"}:
            raise CreativeWorkflowError("invalid_charter_settlement")
        target = raw["target"]
        if not isinstance(target, str) or _SETTLEMENT_TARGET.fullmatch(target) is None:
            raise CreativeWorkflowError("invalid_charter_settlement_target")
        status = raw["status"]
        if status not in CHARTER_SETTLEMENT_STATUSES:
            raise CreativeWorkflowError("invalid_charter_settlement_status")
        basis_ids = raw["basis_ids"]
        event_ids = raw["event_ids"]
        if not isinstance(basis_ids, list) or not isinstance(event_ids, list):
            raise CreativeWorkflowError("invalid_charter_settlement_basis")
        checked_settlement.append(
            {
                "target": target,
                "status": status,
                "rationale": _bounded_text(
                    raw["rationale"],
                    field=f"decision.charter_settlement[{index}].rationale",
                    maximum_bytes=4096,
                ),
                "basis_ids": copy.deepcopy(basis_ids),
                "event_ids": _bounded_text_list(
                    event_ids,
                    field=f"decision.charter_settlement[{index}].event_ids",
                    maximum_items=MAX_DERIVATION_MATERIAL_REFS,
                    item_bytes=256,
                ),
            }
        )
    checked_dispositions: list[dict[str, Any]] = []
    if not isinstance(evidence_dispositions, (list, tuple)):
        raise CreativeWorkflowError("invalid_evidence_disposition")
    for index, disposition_item in enumerate(evidence_dispositions):
        raw = _json_detach(
            disposition_item, field=f"decision.evidence_dispositions[{index}]"
        )
        if set(raw) != {
            "evidence_id",
            "disposition",
            "rationale",
            "basis_ids",
        }:
            raise CreativeWorkflowError("invalid_evidence_disposition")
        basis_ids = raw["basis_ids"]
        if not isinstance(basis_ids, list):
            raise CreativeWorkflowError("invalid_evidence_disposition_basis")
        checked_dispositions.append(
            {
                "evidence_id": raw["evidence_id"],
                "disposition": raw["disposition"],
                "rationale": _bounded_text(
                    raw["rationale"],
                    field=f"decision.evidence_dispositions[{index}].rationale",
                    maximum_bytes=4096,
                ),
                "basis_ids": copy.deepcopy(basis_ids),
            }
        )
    record = {
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
        "review_ids": list(review_ids),
        "evidence_dispositions": checked_dispositions,
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
        # Derivation references make the accepted passage-level
        # justifications part of the decision's durable claim.  The list is
        # always present on new decisions; historical pre-derivation-contract decisions may
        # omit it.
        "derivation_ids": list(derivation_ids),
        # Settlement accounts for every charter promise.  New decisions
        # always carry the list; acceptance requires it to be complete.
        "charter_settlement": checked_settlement,
    }
    if revision_contract is not None:
        record["revision_contract"] = _json_detach(
            revision_contract, field="decision.revision_contract"
        )
    if prior_revision_assessment is not None:
        assessment = _json_detach(
            prior_revision_assessment,
            field="decision.prior_revision_assessment",
        )
        if set(assessment) != {
            "contract_sha256", "outcome", "rationale", "basis_ids"
        }:
            raise CreativeWorkflowError("invalid_prior_revision_assessment")
        basis_ids = assessment["basis_ids"]
        if not isinstance(basis_ids, list):
            raise CreativeWorkflowError("revision_assessment_basis_not_selected")
        record["prior_revision_assessment"] = {
            "contract_sha256": assessment["contract_sha256"],
            "outcome": assessment["outcome"],
            "rationale": _bounded_text(
                assessment["rationale"],
                field="prior_revision_assessment.rationale",
                maximum_bytes=4096,
            ),
            "basis_ids": copy.deepcopy(basis_ids),
        }
    return record


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
    derivation_ids: Sequence[str] = (),
    review_ids: Sequence[str] = (),
    evidence_dispositions: Sequence[Mapping[str, Any]] = (),
    charter_settlement: Sequence[Mapping[str, Any]] = (),
    expected_audible_change: str | None = None,
    revision_scope: Mapping[str, Any] | None = None,
    withdrawal_condition: str | None = None,
    prior_revision_assessment: Mapping[str, Any] | None = None,
    candidate_path: str | os.PathLike[str] | None = None,
) -> CreativeWorkflowSnapshot:
    allowed = {"accept", "revise", "recommend_revision", "preserve", "stop"}
    if disposition not in allowed:
        raise CreativeWorkflowError("invalid_iteration_disposition")
    ids = list(evidence_ids)
    exception_refs = list(exception_ids)
    derivation_refs = list(derivation_ids)
    review_refs = list(review_ids)
    if (
        any(
            not isinstance(item, str)
            for item in ids + exception_refs + derivation_refs + review_refs
        )
        or len(set(ids)) != len(ids)
        or len(set(exception_refs)) != len(exception_refs)
        or len(set(derivation_refs)) != len(derivation_refs)
        or len(set(review_refs)) != len(review_refs)
    ):
        raise CreativeWorkflowError("invalid_decision_references")

    def mutate(state: dict[str, Any], layout: _WorkflowLayout, _expected: str) -> None:
        _require_status(state, "reviewing")
        iteration = _current_iteration(state)
        if final_authority != state["final_authority"]:
            raise CreativeWorkflowError("decision_authority_mismatch")
        timestamp = _transition_timestamp(
            state, iteration["opened_at_utc"]
        )
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
            derivation_ids=derivation_refs,
            review_ids=review_refs,
            evidence_dispositions=evidence_dispositions,
            charter_settlement=charter_settlement,
            prior_revision_assessment=prior_revision_assessment,
        )
        if disposition == "revise":
            decision["revision_contract"] = _build_revision_contract(
                state=state,
                iteration=iteration,
                project_root=layout.project_root,
                decision=decision,
                revision_scope=revision_scope,
                withdrawal_condition=withdrawal_condition,
            )
        elif revision_scope is not None or withdrawal_condition is not None:
            raise CreativeWorkflowError("revision_contract_not_applicable")
        _validate_decision(decision, iteration=iteration)
        _validate_charter_settlement_completeness(
            decision,
            charter=_effective_charter_for_iteration(
                state, iteration["iteration_number"]
            ),
            require_for_acceptance=True,
        )
        settlement_event_ids = [
            event_id
            for item in decision.get("charter_settlement", [])
            for event_id in item["event_ids"]
        ]
        if settlement_event_ids:
            try:
                authoring = open_authoring_project(
                    layout.project_root,
                    revision=iteration["anchor"]["authoring_revision"],
                )
            except AuthoringProjectError as exc:
                raise CreativeWorkflowError(
                    "charter_settlement_score_unavailable"
                ) from exc
            score_index = _build_derivation_score_index(
                authoring.documents["score"]
            )
            if not score_index.score.has_stable_event_identity:
                raise CreativeWorkflowError(
                    "charter_settlement_requires_stable_event_identity"
                )
            for event_id in settlement_event_ids:
                if event_id not in score_index.event_positions:
                    raise CreativeWorkflowError(
                        "charter_settlement_event_not_found"
                    )
        if (
            disposition != "stop" or perception_basis == "audio_audition"
        ) and not _authority_has_basis(
            iteration,
            authority=final_authority,
            perception_basis=perception_basis,
        ):
            raise CreativeWorkflowError("decision_perception_basis_unproven")

        candidate = iteration["anchor"]["candidate"]
        hard_failures, readiness_result_sha256 = _trusted_hard_failure_recheck(
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
            if not _governed_review_phases_complete(
                state,
                iteration,
                set(_GOVERNANCE_REVIEW_PHASES),
            ):
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
            timestamp = _transition_timestamp(
                state, timestamp, observed["verified_at_utc"]
            )
            observed["verified_at_utc"] = timestamp
            decision["decided_at_utc"] = timestamp
            iteration["decision"] = decision
            _close_iteration(iteration, outcome="accepted", timestamp=timestamp)
            state["status"] = "completed"
            state["termination"] = {
                "reason": "accepted_under_charter",
                "summary": decision["summary"],
                "final_authority": final_authority,
                "perception_basis": perception_basis,
                "selected_candidate": copy.deepcopy(observed),
                "open_evidence_ids": _open_evidence_ids_from_decision(decision),
                "acceptance_gate": {
                    "kind": _ACCEPTANCE_GATE_KIND,
                    "schema_version": WORKFLOW_VERSION,
                    "profile": _ACCEPTANCE_GATE_PROFILE,
                    "authoring_revision": iteration["anchor"][
                        "authoring_revision"
                    ],
                    "candidate_manifest_sha256": observed[
                        "candidate_manifest_sha256"
                    ],
                    "checked_hard_failure_evidence_ids": [
                        item["evidence_id"]
                        for item in iteration["evidence"]
                        if item["category"] == "hard_failure"
                    ],
                    "unresolved_hard_failure_evidence_ids": [],
                    "readiness_result_sha256": readiness_result_sha256,
                    "recorded_at_utc": timestamp,
                    "claim_scope": _ACCEPTANCE_GATE_CLAIM_SCOPE,
                },
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
                "open_evidence_ids": _open_evidence_ids_from_decision(decision),
                "terminated_at_utc": timestamp,
            }
            return

        iteration["decision"] = decision
        selected_candidate = copy.deepcopy(
            _terminal_candidate_for_iteration(state, iteration)
        )
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
            "selected_candidate": selected_candidate,
            "open_evidence_ids": _open_evidence_ids_from_decision(decision),
            "terminated_at_utc": timestamp,
        }

    return _transition(
        project_root,
        workflow_id=workflow_id,
        expected_revision=expected_revision,
        mutate=mutate,
    )


def _validate_state_revision_contract_referents(
    project_root: Path, state: Mapping[str, Any]
) -> None:
    for index, iteration in enumerate(state["iterations"]):
        decision = iteration.get("decision")
        contract = (
            decision.get("revision_contract")
            if isinstance(decision, Mapping)
            else None
        )
        if contract is None:
            continue
        try:
            baseline = open_authoring_project(
                project_root, revision=contract["baseline"]["authoring_revision"]
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError("authoring_revision_unavailable") from exc
        if baseline.project_id != state["project_id"]:
            raise CreativeWorkflowError("authoring_project_identity_changed")
        if dict(baseline.document_revisions) != contract["baseline"][
            "document_sha256"
        ]:
            raise CreativeWorkflowError("revision_contract_baseline_hash_mismatch")
        candidate_locator = contract["baseline"]["candidate"]
        source_number = contract["baseline"]["candidate_source_iteration_number"]
        if candidate_locator is not None:
            if not isinstance(source_number, int) or source_number > len(
                state["iterations"]
            ):
                raise CreativeWorkflowError(
                    "revision_contract_baseline_candidate_mismatch"
                )
            source_candidate = state["iterations"][source_number - 1]["anchor"].get(
                "candidate"
            )
            if (
                not isinstance(source_candidate, Mapping)
                or _anchor_locator(source_candidate) != candidate_locator
            ):
                raise CreativeWorkflowError(
                    "revision_contract_baseline_candidate_mismatch"
                )
        fence = contract["authoring_causal_fence"]
        expected_fence_revision = iteration["anchor"]["authoring_revision"]
        if index > 0 and state["iterations"][index - 1].get("outcome") == "rolled_back":
            expected_fence_revision = state["iterations"][index - 1]["anchor"][
                "authoring_revision"
            ]
        if fence["anchor_revision"] != expected_fence_revision:
            raise CreativeWorkflowError("revision_contract_causal_fence_mismatch")
        try:
            verify_authoring_save_event_binding(
                project_root,
                event_sha256=fence["anchor_save_event_sha256"],
                project_id=state["project_id"],
                revision=fence["anchor_revision"],
                save_sequence=fence["save_sequence"],
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError("revision_contract_causal_fence_mismatch") from exc
        revised_to = iteration.get("next_authoring_revision")
        if revised_to is None:
            continue
        if index + 1 >= len(state["iterations"]):
            raise CreativeWorkflowError("workflow_iteration_lineage_mismatch")
        next_anchor = state["iterations"][index + 1]["anchor"]
        try:
            revised = open_authoring_project(project_root, revision=revised_to)
            if (
                revised.revision_first_save_sequence is None
                or revised.revision_first_save_sequence <= fence["save_sequence"]
            ):
                raise CreativeWorkflowError("authoring_revision_causality_invalid")
            verify_authoring_revision_ancestry(
                project_root,
                descendant_revision=revised_to,
                ancestor_revision=fence["anchor_revision"],
                descendant_save_event_sha256=next_anchor[
                    "authoring_save_event_sha256"
                ],
                ancestor_save_event_sha256=fence["anchor_save_event_sha256"],
                minimum_exclusive_save_sequence=fence["save_sequence"],
                require_current_head=False,
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError("authoring_revision_causality_invalid") from exc
        _enforce_revision_contract_save_chain(
            project_root,
            project_id=state["project_id"],
            contract=contract,
            baseline_documents=baseline.detached_documents(),
            descendant_revision=revised_to,
            descendant_save_sequence=next_anchor["authoring_save_sequence"],
            descendant_save_event_sha256=next_anchor[
                "authoring_save_event_sha256"
            ],
        )


def _score_revision_note_index(
    score: Mapping[str, Any],
) -> tuple[dict[str, tuple[str, Mapping[str, Any]]], dict[str, list[str]]]:
    notes: dict[str, tuple[str, Mapping[str, Any]]] = {}
    orders: dict[str, list[str]] = {}
    for part in score["parts"]:
        part_id = part["id"]
        order: list[str] = []
        for note in part["notes"]:
            event_id = note.get("event_id")
            if not isinstance(event_id, str) or not event_id or event_id in notes:
                raise CreativeWorkflowError(
                    "bounded_revision_requires_stable_event_identity"
                )
            notes[event_id] = (part_id, note)
            order.append(event_id)
        orders[part_id] = order
    return notes, orders


def _revision_note_in_scope(
    *, event_id: str, part_id: str, note: Mapping[str, Any], scope: Mapping[str, Any]
) -> bool:
    part_ids = scope["part_ids"]
    event_ids = scope["event_ids"]
    bar_ranges = scope["bar_ranges"]
    return (
        (not part_ids or part_id in part_ids)
        and (not event_ids or event_id in event_ids)
        and (
            not bar_ranges
            or any(
                item["start"] <= note["bar"] <= item["end"]
                for item in bar_ranges
            )
        )
    )


def _score_metadata_without_notes(score: Mapping[str, Any]) -> dict[str, Any]:
    shell = _thaw(score)
    assert isinstance(shell, dict)
    for part in shell["parts"]:
        part["notes"] = []
    return shell


def _json_pointer_token(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


def _json_leaf_pointers(value: object, *, path: str) -> set[str]:
    if isinstance(value, Mapping):
        if not value:
            return {path}
        result: set[str] = set()
        for key, item in value.items():
            result.update(
                _json_leaf_pointers(
                    item,
                    path=f"{path}/{_json_pointer_token(key)}",
                )
            )
        return result
    if isinstance(value, (list, tuple)):
        if not value:
            return {path}
        result = set()
        for index, item in enumerate(value):
            result.update(_json_leaf_pointers(item, path=f"{path}/{index}"))
        return result
    return {path}


def _changed_json_leaf_pointers(
    before: object, after: object, *, path: str = ""
) -> set[str]:
    if canonical_json_sha256(before) == canonical_json_sha256(after):
        return set()
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        result: set[str] = set()
        if not before or not after:
            if not before:
                result.add(path)
            if not after:
                result.add(path)
        for key in set(before) | set(after):
            child_path = f"{path}/{_json_pointer_token(key)}"
            if key not in before:
                result.update(_json_leaf_pointers(after[key], path=child_path))
            elif key not in after:
                result.update(_json_leaf_pointers(before[key], path=child_path))
            else:
                result.update(
                    _changed_json_leaf_pointers(
                        before[key], after[key], path=child_path
                    )
                )
        return result
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        result = set()
        if not before or not after:
            if not before:
                result.add(path)
            if not after:
                result.add(path)
        common = min(len(before), len(after))
        for index in range(common):
            result.update(
                _changed_json_leaf_pointers(
                    before[index], after[index], path=f"{path}/{index}"
                )
            )
        for index in range(common, len(before)):
            result.update(_json_leaf_pointers(before[index], path=f"{path}/{index}"))
        for index in range(common, len(after)):
            result.update(_json_leaf_pointers(after[index], path=f"{path}/{index}"))
        return result
    return _json_leaf_pointers(before, path=path) | _json_leaf_pointers(
        after, path=path
    )


def _enforce_document_path_scope(
    before: object,
    after: object,
    *,
    allowed_paths: Sequence[str],
) -> None:
    changed_paths = _changed_json_leaf_pointers(before, after)
    if not changed_paths.issubset(allowed_paths):
        raise CreativeWorkflowError("revision_scope_document_path_overreach")


def _enforce_bounded_score_revision(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    scope: Mapping[str, Any],
    *,
    allowed_paths: Sequence[str],
) -> None:
    before_notes, before_orders = _score_revision_note_index(before)
    after_notes, after_orders = _score_revision_note_index(after)
    _enforce_document_path_scope(
        _score_metadata_without_notes(before),
        _score_metadata_without_notes(after),
        allowed_paths=allowed_paths,
    )
    before_ids = set(before_notes)
    after_ids = set(after_notes)
    for event_id in sorted(before_ids - after_ids):
        part_id, note = before_notes[event_id]
        if not scope["allow_event_deletions"] or not _revision_note_in_scope(
            event_id=event_id, part_id=part_id, note=note, scope=scope
        ):
            raise CreativeWorkflowError("revision_scope_event_deletion_overreach")
    for event_id in sorted(after_ids - before_ids):
        part_id, note = after_notes[event_id]
        if not scope["allow_event_additions"] or not _revision_note_in_scope(
            event_id=event_id, part_id=part_id, note=note, scope=scope
        ):
            raise CreativeWorkflowError("revision_scope_event_addition_overreach")
    for event_id in sorted(before_ids & after_ids):
        before_part, before_note = before_notes[event_id]
        after_part, after_note = after_notes[event_id]
        changed_fields = {
            field
            for field in set(before_note) | set(after_note)
            if field != "event_id"
            and canonical_json_sha256(
                {"present": field in before_note, "value": before_note.get(field)}
            )
            != canonical_json_sha256(
                {"present": field in after_note, "value": after_note.get(field)}
            )
        }
        if before_part != after_part:
            changed_fields.add("part_id")
        if not changed_fields:
            continue
        if (
            not _revision_note_in_scope(
                event_id=event_id, part_id=before_part, note=before_note, scope=scope
            )
            or not _revision_note_in_scope(
                event_id=event_id, part_id=after_part, note=after_note, scope=scope
            )
            or not changed_fields.issubset(scope["allowed_note_fields"])
        ):
            raise CreativeWorkflowError("revision_scope_event_update_overreach")
    for part_id in sorted(set(before_orders) & set(after_orders)):
        stable_ids = {
            event_id
            for event_id in before_ids & after_ids
            if before_notes[event_id][0] == part_id
            and after_notes[event_id][0] == part_id
        }
        before_order = [item for item in before_orders[part_id] if item in stable_ids]
        after_order = [item for item in after_orders[part_id] if item in stable_ids]
        if before_order != after_order and (
            not scope["allow_reordering"] or part_id not in scope["part_ids"]
        ):
            raise CreativeWorkflowError("revision_scope_reordering_overreach")


def _enforce_revision_contract_documents(
    *, contract: Mapping[str, Any], before: Mapping[str, Any], after: Mapping[str, Any]
) -> None:
    scope = contract["revision_scope"]
    allowed_documents = set(scope["documents"])
    changed = {
        name
        for name in _REVISION_DOCUMENTS
        if canonical_json_sha256(before[name]) != canonical_json_sha256(after[name])
    }
    if not changed.issubset(allowed_documents):
        raise CreativeWorkflowError("revision_scope_document_overreach")
    if (
        scope["change_scale"] == "bounded"
        and changed
    ):
        allowed_paths = scope["allowed_document_paths"]
        for document_name in sorted(changed):
            if document_name == "score":
                assert scope["score"] is not None
                _enforce_bounded_score_revision(
                    before["score"],
                    after["score"],
                    scope["score"],
                    allowed_paths=allowed_paths["score"],
                )
            else:
                _enforce_document_path_scope(
                    before[document_name],
                    after[document_name],
                    allowed_paths=allowed_paths[document_name],
                )


def _enforce_revision_contract_save_chain(
    project_root: Path,
    *,
    project_id: str,
    contract: Mapping[str, Any],
    baseline_documents: Mapping[str, Any],
    descendant_revision: object,
    descendant_save_sequence: object,
    descendant_save_event_sha256: object,
) -> None:
    """Reject any saved post-contract state that exceeds the declared scope."""

    error_code = "authoring_revision_causality_invalid"
    fence = contract["authoring_causal_fence"]
    current_revision = _checked_revision(descendant_revision, code=error_code)
    current_sequence = _strict_governance_integer(
        descendant_save_sequence,
        code=error_code,
        minimum=1,
        maximum=MAX_AUTHORING_SAVE_SEQUENCE,
    )
    current_event_sha256 = _checked_revision(
        descendant_save_event_sha256, code=error_code
    )
    fence_revision = _checked_revision(fence["anchor_revision"], code=error_code)
    fence_sequence = _strict_governance_integer(
        fence["save_sequence"],
        code=error_code,
        minimum=1,
        maximum=MAX_AUTHORING_SAVE_SEQUENCE,
    )
    fence_event_sha256 = _checked_revision(
        fence["anchor_save_event_sha256"], code=error_code
    )
    if current_sequence <= fence_sequence:
        raise CreativeWorkflowError(error_code)

    while current_sequence > fence_sequence:
        try:
            event = verify_authoring_save_event_binding(
                project_root,
                event_sha256=current_event_sha256,
                project_id=project_id,
                revision=current_revision,
                save_sequence=current_sequence,
            )
            saved_authoring = open_authoring_project(
                project_root, revision=current_revision
            )
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError(error_code) from exc
        if saved_authoring.project_id != project_id:
            raise CreativeWorkflowError("authoring_project_identity_changed")
        _enforce_revision_contract_documents(
            contract=contract,
            before=baseline_documents,
            after=saved_authoring.detached_documents(),
        )

        parent_revision = event["parent_revision"]
        parent_event_sha256 = event["parent_event_sha256"]
        if parent_revision is None or parent_event_sha256 is None:
            raise CreativeWorkflowError(error_code)
        current_revision = _checked_revision(parent_revision, code=error_code)
        current_event_sha256 = _checked_revision(
            parent_event_sha256, code=error_code
        )
        current_sequence -= 1

    if (
        current_revision != fence_revision
        or current_sequence != fence_sequence
        or current_event_sha256 != fence_event_sha256
    ):
        raise CreativeWorkflowError(error_code)


def _verify_revision_contract_causality(
    project_root: Path,
    *, project_id: str, contract: Mapping[str, Any], revised_authoring: Any,
    require_current_head: bool,
) -> None:
    fence = contract["authoring_causal_fence"]
    if (
        revised_authoring.revision_first_save_sequence is None
        or revised_authoring.revision_first_save_sequence <= fence["save_sequence"]
        or revised_authoring.save_event_sha256 is None
    ):
        raise CreativeWorkflowError("authoring_revision_causality_invalid")
    try:
        verify_authoring_revision_ancestry(
            project_root,
            descendant_revision=revised_authoring.revision,
            ancestor_revision=fence["anchor_revision"],
            descendant_save_event_sha256=revised_authoring.save_event_sha256,
            ancestor_save_event_sha256=fence["anchor_save_event_sha256"],
            minimum_exclusive_save_sequence=fence["save_sequence"],
            require_current_head=require_current_head,
        )
    except AuthoringProjectError as exc:
        raise CreativeWorkflowError("authoring_revision_causality_invalid") from exc


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
        try:
            authoring = open_authoring_project(layout.project_root)
        except AuthoringProjectError as exc:
            raise CreativeWorkflowError("authoring_revision_unavailable") from exc
        if authoring.revision != checked_revision:
            raise CreativeWorkflowError("authoring_revision_not_current_head")
        if authoring.project_id != state["project_id"]:
            raise CreativeWorkflowError("authoring_project_identity_changed")
        if checked_revision == iteration["anchor"]["authoring_revision"]:
            raise CreativeWorkflowError("authoring_revision_unchanged")
        decision = iteration.get("decision")
        contract = (
            decision.get("revision_contract")
            if isinstance(decision, Mapping)
            else None
        )
        if "revision_contract_profile" in state["policy"]:
            if not isinstance(contract, Mapping):
                raise CreativeWorkflowError("revision_contract_required")
            try:
                baseline_authoring = open_authoring_project(
                    layout.project_root,
                    revision=contract["baseline"]["authoring_revision"],
                )
            except AuthoringProjectError as exc:
                raise CreativeWorkflowError("authoring_revision_unavailable") from exc
            if baseline_authoring.project_id != state["project_id"]:
                raise CreativeWorkflowError("authoring_project_identity_changed")
            if dict(baseline_authoring.document_revisions) != contract["baseline"][
                "document_sha256"
            ]:
                raise CreativeWorkflowError("revision_contract_baseline_hash_mismatch")
            _verify_revision_contract_causality(
                layout.project_root,
                project_id=state["project_id"],
                contract=contract,
                revised_authoring=authoring,
                require_current_head=True,
            )
            _enforce_revision_contract_save_chain(
                layout.project_root,
                project_id=state["project_id"],
                contract=contract,
                baseline_documents=baseline_authoring.detached_documents(),
                descendant_revision=authoring.revision,
                descendant_save_sequence=authoring.save_sequence,
                descendant_save_event_sha256=authoring.save_event_sha256,
            )
        governance = state.get("governance")
        amendment_records = (
            governance.get("amendments", [])
            if isinstance(governance, Mapping)
            else []
        )
        amendment = next(
            (
                item
                for item in amendment_records
                if item.get("committed_in_iteration")
                == iteration["iteration_number"]
            ),
            None,
        )
        if amendment is not None:
            causal_fence = amendment.get("authoring_causal_fence")
            if causal_fence is None:
                # Historical ledgers remain readable, but cannot honestly
                # prove that a later score was first authored after an old
                # amendment whose causal boundary was never recorded.
                raise CreativeWorkflowError(
                    "authoring_causal_provenance_unavailable"
                )
            if (
                authoring.save_sequence is None
                or authoring.save_event_sha256 is None
                or authoring.revision_first_save_sequence is None
                or authoring.revision_first_save_sequence
                <= causal_fence["save_sequence"]
            ):
                raise CreativeWorkflowError(
                    "authoring_revision_causality_invalid"
                )
            try:
                verify_authoring_revision_ancestry(
                    layout.project_root,
                    descendant_revision=checked_revision,
                    ancestor_revision=causal_fence["anchor_revision"],
                    descendant_save_event_sha256=(
                        authoring.save_event_sha256
                    ),
                    ancestor_save_event_sha256=causal_fence[
                        "anchor_save_event_sha256"
                    ],
                    minimum_exclusive_save_sequence=causal_fence[
                        "save_sequence"
                    ],
                    require_current_head=True,
                )
            except AuthoringProjectError as exc:
                if exc.code == "revision_not_current_head":
                    raise CreativeWorkflowError(
                        "authoring_revision_not_current_head"
                    ) from exc
                raise CreativeWorkflowError(
                    "authoring_revision_causality_invalid"
                ) from exc
        if state["usage"]["revision_cycles"] >= state["budget"]["max_revision_cycles"]:
            raise CreativeWorkflowError("revision_budget_exhausted")
        if len(state["iterations"]) >= MAX_ITERATIONS:
            raise CreativeWorkflowError("iteration_limit_exceeded")
        timestamp = _transition_timestamp(
            state, iteration["opened_at_utc"]
        )
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
                authoring_save_sequence=authoring.save_sequence,
                authoring_save_event_sha256=authoring.save_event_sha256,
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
    prior_revision_assessment: Mapping[str, Any] | None = None,
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
        source_contract = _challenger_source_contract(
            state, current["iteration_number"]
        )
        if (
            source_contract is not None
            and source_contract["baseline"]["candidate"] is not None
        ):
            if prior_revision_assessment is None:
                raise CreativeWorkflowError("prior_revision_assessment_required")
            if target_iteration_number != source_contract["baseline"][
                "candidate_source_iteration_number"
            ]:
                raise CreativeWorkflowError("rollback_must_select_revision_baseline")
        elif prior_revision_assessment is not None:
            raise CreativeWorkflowError(
                "revision_assessment_without_challenger_contract"
            )
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
        timestamp = _transition_timestamp(
            state, current["opened_at_utc"]
        )
        nonhard_evidence_ids = [
            item["evidence_id"]
            for item in current["evidence"]
            if item["category"] != "hard_failure"
        ]
        decision = _decision_record(
            disposition="rollback",
            summary=summary,
            rationale=rationale,
            protected_values=(),
            sacrificed_values=(),
            evidence_ids=nonhard_evidence_ids,
            exception_ids=(),
            expected_audible_change=None,
            final_authority=final_authority,
            perception_basis=perception_basis,
            timestamp=timestamp,
            review_ids=[item["review_id"] for item in current["reviews"]],
            evidence_dispositions=[
                {
                    "evidence_id": evidence_id,
                    "disposition": "deferred",
                    "rationale": "Rollback preserves this non-hard claim for later review.",
                    "basis_ids": [],
                }
                for evidence_id in nonhard_evidence_ids
            ],
            prior_revision_assessment=prior_revision_assessment,
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
            timestamp = _transition_timestamp(
                state, timestamp, pending[0]["requested_at_utc"]
            )
            decision["decided_at_utc"] = timestamp
            pending[0]["reservation_revision"] = expected
            pending[0]["status"] = "cancelled"
            pending[0]["finished_at_utc"] = timestamp
        current["decision"] = decision
        _close_iteration(current, outcome="rolled_back", timestamp=timestamp)
        state["iterations"].append(
            _new_iteration(
                len(state["iterations"]) + 1,
                authoring_revision=target["anchor"]["authoring_revision"],
                authoring_save_sequence=target["anchor"].get(
                    "authoring_save_sequence"
                ),
                authoring_save_event_sha256=target["anchor"].get(
                    "authoring_save_event_sha256"
                ),
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
    if reason not in DIRECT_TERMINATION_REASONS:
        raise CreativeWorkflowError("invalid_termination_reason")
    checked_summary = _bounded_text(
        summary, field="termination.summary", maximum_bytes=4096
    )
    if final_authority not in FINAL_AUTHORITIES or perception_basis not in PERCEPTION_BASES:
        raise CreativeWorkflowError("invalid_workflow_termination")

    def mutate(state: dict[str, Any], _layout: _WorkflowLayout, expected: str) -> None:
        if state["status"] in TERMINAL_WORKFLOW_STATUSES:
            raise CreativeWorkflowError("workflow_already_terminal")
        if reason == "budget_exhausted" and not _workflow_budget_is_exhausted(
            state
        ):
            raise CreativeWorkflowError("workflow_budget_not_exhausted")
        if final_authority != state["final_authority"]:
            raise CreativeWorkflowError("termination_authority_mismatch")
        timestamp = _transition_timestamp(state)
        selected: dict[str, Any] | None = None
        open_evidence_ids: list[str] = []
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
                timestamp = _transition_timestamp(
                    state,
                    timestamp,
                    iteration["opened_at_utc"],
                    pending[0]["requested_at_utc"],
                )
                pending[0]["reservation_revision"] = expected
                pending[0]["status"] = "cancelled"
                pending[0]["finished_at_utc"] = timestamp
            timestamp = _transition_timestamp(
                state, timestamp, iteration["opened_at_utc"]
            )
            selected = copy.deepcopy(
                _terminal_candidate_for_iteration(state, iteration)
            )
            open_evidence_ids = [
                item["evidence_id"]
                for item in iteration["evidence"]
                if item["category"] != "hard_failure"
            ]
            _close_iteration(iteration, outcome="stopped", timestamp=timestamp)
        state["status"] = "stopped"
        state["termination"] = {
            "reason": reason,
            "summary": checked_summary,
            "final_authority": final_authority,
            "perception_basis": perception_basis,
            "selected_candidate": selected,
            "open_evidence_ids": open_evidence_ids,
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
    "commit_workflow_charter_amendment",
    "create_creative_workflow",
    "decide_workflow_iteration",
    "inspect_workflow_composition",
    "inspect_workflow_candidate_status",
    "open_creative_workflow",
    "record_verified_workflow_hard_failure",
    "record_workflow_authoring_revision",
    "record_workflow_candidate",
    "record_workflow_composition_map",
    "record_workflow_derivation",
    "record_workflow_evidence",
    "record_workflow_fork",
    "record_workflow_review",
    "register_workflow_exception",
    "preflight_workflow_charter_amendment",
    "request_workflow_render",
    "rollback_workflow",
    "terminate_creative_workflow",
    "unresolved_workflow_hard_failures",
    "verify_active_render_reservation",
    "verify_creative_workflow_history",
    "verify_render_reservation_history",
    "workflow_render_authorization",
]
