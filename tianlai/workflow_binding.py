"""Dependency-light identity contract for managed authoring renders.

The authoring workflow, render engine, and immutable-candidate verifier all
need to agree on the exact authorization that permitted one expensive render.
Keeping the shape in this small standard-library-only module avoids circular
imports and, more importantly, prevents any layer from accepting a looser
look-alike object.

The binding is deliberately procedural.  It proves which workflow transition
authorized a render; it says nothing about whether the resulting music is
good, preferred, or approved by a human listener.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .portable_filename import is_windows_reserved_filename


WORKFLOW_AUTHORIZATION_FIELDS = frozenset(
    {
        "workflow_id",
        "project_id",
        "reservation_revision",
        "iteration_number",
        "operation_id",
        "authoring_revision",
        "candidate_work_id",
        "candidate_id",
        "parent_work_id",
        "parent_candidate_id",
        "parent_manifest_sha256",
    }
)
MAX_WORKFLOW_ITERATIONS = 256
MAX_CANDIDATE_ID_BYTES = 512
MAX_CANDIDATE_ID_CHARACTERS = 128

_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


def _lower_hex(value: object, *, length: int, label: str) -> str:
    pattern = _HEX_32 if length == 32 else _HEX_64
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(
            f"workflow authorization {label} must be {length} lowercase "
            "hexadecimal characters"
        )
    return value


def _portable_segment(value: object, *, nullable: bool, label: str) -> str | None:
    if nullable and value is None:
        return None
    try:
        encoded_size = (
            len(value.encode("utf-8", errors="strict"))
            if isinstance(value, str)
            else 0
        )
    except UnicodeEncodeError as exc:
        raise ValueError(f"workflow authorization {label} is invalid") from exc
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or len(value) > MAX_CANDIDATE_ID_CHARACTERS
        or encoded_size > MAX_CANDIDATE_ID_BYTES
        or any(character in value for character in '<>:"/\\|?*')
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or value[-1] in {" ", "."}
        or is_windows_reserved_filename(value)
    ):
        raise ValueError(f"workflow authorization {label} is invalid")
    return value


def validate_workflow_authorization(
    value: object,
    *,
    allow_none: bool = True,
) -> dict[str, Any] | None:
    """Return one detached exact-shape authorization or reject it.

    ``None`` is retained for legacy and explicitly unmanaged renders.  A
    managed candidate must pass ``allow_none=False`` at the workflow boundary.
    Unknown fields fail closed so future versions cannot be silently
    interpreted with weaker semantics.
    """

    if value is None:
        if allow_none:
            return None
        raise ValueError("workflow authorization is required")
    if not isinstance(value, Mapping) or set(value) != WORKFLOW_AUTHORIZATION_FIELDS:
        raise ValueError("workflow authorization has an invalid shape")

    iteration_number = value.get("iteration_number")
    if (
        isinstance(iteration_number, bool)
        or not isinstance(iteration_number, int)
        or not 1 <= iteration_number <= MAX_WORKFLOW_ITERATIONS
    ):
        raise ValueError("workflow authorization iteration_number is invalid")

    candidate_work_id = _portable_segment(
        value.get("candidate_work_id"),
        nullable=False,
        label="candidate_work_id",
    )
    candidate_id = _portable_segment(
        value.get("candidate_id"), nullable=False, label="candidate_id"
    )
    parent_work_id = _portable_segment(
        value.get("parent_work_id"), nullable=True, label="parent_work_id"
    )
    parent_candidate_id = _portable_segment(
        value.get("parent_candidate_id"),
        nullable=True,
        label="parent_candidate_id",
    )
    raw_parent_manifest = value.get("parent_manifest_sha256")
    parent_manifest_sha256 = (
        None
        if raw_parent_manifest is None
        else _lower_hex(
            raw_parent_manifest,
            length=64,
            label="parent_manifest_sha256",
        )
    )
    parent_values = (
        parent_work_id,
        parent_candidate_id,
        parent_manifest_sha256,
    )
    if any(item is None for item in parent_values) and any(
        item is not None for item in parent_values
    ):
        raise ValueError(
            "workflow authorization parent locator must be entirely null or complete"
        )

    return {
        "workflow_id": _lower_hex(
            value.get("workflow_id"), length=32, label="workflow_id"
        ),
        "project_id": _lower_hex(
            value.get("project_id"), length=32, label="project_id"
        ),
        "reservation_revision": _lower_hex(
            value.get("reservation_revision"),
            length=64,
            label="reservation_revision",
        ),
        "iteration_number": iteration_number,
        "operation_id": _lower_hex(
            value.get("operation_id"), length=32, label="operation_id"
        ),
        "authoring_revision": _lower_hex(
            value.get("authoring_revision"),
            length=64,
            label="authoring_revision",
        ),
        "candidate_work_id": candidate_work_id,
        "candidate_id": candidate_id,
        "parent_work_id": parent_work_id,
        "parent_candidate_id": parent_candidate_id,
        "parent_manifest_sha256": parent_manifest_sha256,
    }


__all__ = [
    "MAX_CANDIDATE_ID_BYTES",
    "MAX_CANDIDATE_ID_CHARACTERS",
    "MAX_WORKFLOW_ITERATIONS",
    "WORKFLOW_AUTHORIZATION_FIELDS",
    "validate_workflow_authorization",
]
