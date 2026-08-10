"""Strict, bounded JSON utilities for the Tianlai authoring boundary.

The normal render core accepts trusted Python dictionaries.  Authoring input
is a different boundary: caller-supplied documents may be replaced as one
complete JSON value, and must not be able to exhaust the process before
musical validation starts.  This module therefore validates the JSON value
model itself before any score, roster, or render-profile parser sees it.

Errors deliberately contain only stable codes, message keys, bounded numeric
facts, and structural location segments.  They never embed values, local
paths, parser exceptions, or arbitrary object keys in human-readable text.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from typing import Any, Iterable, Mapping, Sequence


MAX_AUTHORING_REQUEST_BYTES = 16 * 1024 * 1024
MAX_AUTHORING_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_AUTHORING_DEPTH = 128
MAX_AUTHORING_NODES = 1_000_000
MAX_AUTHORING_STRING_BYTES = 1024 * 1024
MAX_AUTHORING_ARRAY_ITEMS = 250_000
MAX_AUTHORING_OBJECT_MEMBERS = 65_536
JS_SAFE_INTEGER = 9_007_199_254_740_991


@dataclass(frozen=True, slots=True)
class AuthoringJsonLimits:
    max_document_bytes: int = MAX_AUTHORING_DOCUMENT_BYTES
    max_depth: int = MAX_AUTHORING_DEPTH
    max_nodes: int = MAX_AUTHORING_NODES
    max_string_bytes: int = MAX_AUTHORING_STRING_BYTES
    max_array_items: int = MAX_AUTHORING_ARRAY_ITEMS
    max_object_members: int = MAX_AUTHORING_OBJECT_MEMBERS

    def __post_init__(self) -> None:
        for field in self.__dataclass_fields__:
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{field} must be a positive integer")


class AuthoringJsonError(ValueError):
    """One safe JSON-boundary failure suitable for structured projection."""

    def __init__(
        self,
        code: str,
        *,
        location_segments: Iterable[str | int] = (),
        actual: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.code = code
        self.message_key = f"authoringJson.{code.replace('.', '_')}"
        self.location_segments = tuple(location_segments)
        self.actual = actual
        self.limit = limit
        super().__init__(code)

    def to_issue(self, *, source: str = "project") -> dict[str, Any]:
        issue: dict[str, Any] = {
            "code": self.code,
            "message_key": self.message_key,
            "source": source,
            "severity": "error",
            "decision": "block",
            "location": {"segments": list(self.location_segments)},
        }
        if self.actual is not None:
            issue["actual"] = self.actual
        if self.limit is not None:
            issue["limit"] = self.limit
        return issue


class _DuplicateMember(ValueError):
    pass


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateMember
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise AuthoringJsonError("non_finite_number")


def _checked_utf8_size(
    value: str,
    *,
    location: Sequence[str | int],
    limits: AuthoringJsonLimits,
) -> int:
    try:
        size = len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as exc:
        raise AuthoringJsonError(
            "invalid_unicode",
            location_segments=location,
        ) from exc
    if size > limits.max_string_bytes:
        raise AuthoringJsonError(
            "string_too_large",
            location_segments=location,
            actual=size,
            limit=limits.max_string_bytes,
        )
    return size


def validate_json_value(
    value: object,
    *,
    limits: AuthoringJsonLimits | None = None,
    require_object: bool = True,
) -> dict[str, Any] | object:
    """Validate a detached JSON value without recursive Python calls.

    The node count includes containers, object keys, and scalar values.  This
    makes the budget conservative and stable across the parser and in-memory
    entrypoints.  Object keys are represented in locations only after they
    have passed the bounded-string gate.
    """

    active_limits = limits or AuthoringJsonLimits()
    if require_object and not isinstance(value, dict):
        raise AuthoringJsonError("top_level_object_required")

    stack: list[tuple[object, tuple[str | int, ...], int]] = [(value, (), 1)]
    nodes = 0
    while stack:
        current, location, depth = stack.pop()
        nodes += 1
        if nodes > active_limits.max_nodes:
            raise AuthoringJsonError(
                "too_many_nodes",
                location_segments=location,
                actual=nodes,
                limit=active_limits.max_nodes,
            )
        if depth > active_limits.max_depth:
            raise AuthoringJsonError(
                "too_deep",
                location_segments=location,
                actual=depth,
                limit=active_limits.max_depth,
            )

        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, int):
            if not -JS_SAFE_INTEGER <= current <= JS_SAFE_INTEGER:
                raise AuthoringJsonError(
                    "integer_outside_js_safe_range",
                    location_segments=location,
                )
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise AuthoringJsonError(
                    "non_finite_number",
                    location_segments=location,
                )
            continue
        if isinstance(current, str):
            _checked_utf8_size(
                current,
                location=location,
                limits=active_limits,
            )
            continue
        if isinstance(current, list):
            count = len(current)
            if count > active_limits.max_array_items:
                raise AuthoringJsonError(
                    "array_too_large",
                    location_segments=location,
                    actual=count,
                    limit=active_limits.max_array_items,
                )
            for index in range(count - 1, -1, -1):
                stack.append((current[index], (*location, index), depth + 1))
            continue
        if isinstance(current, dict):
            count = len(current)
            if count > active_limits.max_object_members:
                raise AuthoringJsonError(
                    "object_too_large",
                    location_segments=location,
                    actual=count,
                    limit=active_limits.max_object_members,
                )
            children: list[tuple[object, tuple[str | int, ...], int]] = []
            for key, child in current.items():
                if not isinstance(key, str):
                    raise AuthoringJsonError(
                        "non_string_object_key",
                        location_segments=location,
                    )
                _checked_utf8_size(
                    key,
                    location=location,
                    limits=active_limits,
                )
                nodes += 1
                if nodes > active_limits.max_nodes:
                    raise AuthoringJsonError(
                        "too_many_nodes",
                        location_segments=location,
                        actual=nodes,
                        limit=active_limits.max_nodes,
                    )
                children.append((child, (*location, key), depth + 1))
            stack.extend(reversed(children))
            continue
        raise AuthoringJsonError(
            "unsupported_value_type",
            location_segments=location,
        )

    return value


def strict_json_loads(
    payload: str | bytes,
    *,
    limits: AuthoringJsonLimits | None = None,
    require_object: bool = True,
) -> dict[str, Any] | object:
    """Decode one bounded UTF-8 JSON value, rejecting duplicate members."""

    active_limits = limits or AuthoringJsonLimits()
    if isinstance(payload, bytes):
        size = len(payload)
        if size > active_limits.max_document_bytes:
            raise AuthoringJsonError(
                "document_too_large",
                actual=size,
                limit=active_limits.max_document_bytes,
            )
        try:
            text = payload.decode("utf-8-sig", errors="strict")
        except UnicodeDecodeError as exc:
            raise AuthoringJsonError("invalid_utf8") from exc
    elif isinstance(payload, str):
        try:
            size = len(payload.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise AuthoringJsonError("invalid_unicode") from exc
        if size > active_limits.max_document_bytes:
            raise AuthoringJsonError(
                "document_too_large",
                actual=size,
                limit=active_limits.max_document_bytes,
            )
        text = payload.removeprefix("\ufeff")
    else:
        raise AuthoringJsonError("text_or_bytes_required")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_constant,
        )
    except AuthoringJsonError:
        raise
    except _DuplicateMember as exc:
        raise AuthoringJsonError("duplicate_object_member") from exc
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise AuthoringJsonError("invalid_json_syntax") from exc
    return validate_json_value(
        value,
        limits=active_limits,
        require_object=require_object,
    )


def json_document_bytes(
    document: Mapping[str, Any],
    *,
    limits: AuthoringJsonLimits | None = None,
) -> bytes:
    """Return deterministic, human-readable UTF-8 after both value gates."""

    active_limits = limits or AuthoringJsonLimits()
    validate_json_value(document, limits=active_limits, require_object=True)
    try:
        payload = (
            json.dumps(
                document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise AuthoringJsonError("serialization_failed") from exc
    if len(payload) > active_limits.max_document_bytes:
        raise AuthoringJsonError(
            "document_too_large",
            actual=len(payload),
            limit=active_limits.max_document_bytes,
        )
    return payload


def validate_request_size(
    documents: Mapping[str, Mapping[str, Any]],
    *,
    maximum_bytes: int = MAX_AUTHORING_REQUEST_BYTES,
    limits: AuthoringJsonLimits | None = None,
) -> int:
    """Bound the combined serialized document payload for one save request."""

    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes < 1
    ):
        raise ValueError("maximum_bytes must be a positive integer")
    total = 0
    for document in documents.values():
        total += len(json_document_bytes(document, limits=limits))
        if total > maximum_bytes:
            raise AuthoringJsonError(
                "request_too_large",
                actual=total,
                limit=maximum_bytes,
            )
    return total


__all__ = [
    "AuthoringJsonError",
    "AuthoringJsonLimits",
    "JS_SAFE_INTEGER",
    "MAX_AUTHORING_ARRAY_ITEMS",
    "MAX_AUTHORING_DEPTH",
    "MAX_AUTHORING_DOCUMENT_BYTES",
    "MAX_AUTHORING_NODES",
    "MAX_AUTHORING_OBJECT_MEMBERS",
    "MAX_AUTHORING_REQUEST_BYTES",
    "MAX_AUTHORING_STRING_BYTES",
    "json_document_bytes",
    "strict_json_loads",
    "validate_json_value",
    "validate_request_size",
]
