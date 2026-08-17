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
            if type(value) is not int or value < 1:
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
    # Every Unicode code point contributes at least one UTF-8 byte.  Reject a
    # newly swapped-in giant string before allocating an equally giant encoded
    # temporary merely to discover that it exceeds the limit.
    if len(value) > limits.max_string_bytes:
        raise AuthoringJsonError(
            "string_too_large",
            location_segments=location,
            limit=limits.max_string_bytes,
        )
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


def _canonical_string_size(value: str, utf8_size: int) -> int:
    """Return the exact UTF-8 size of ``value`` in canonical JSON quotes."""

    size = utf8_size + 2
    for character in value:
        codepoint = ord(character)
        if character in ('"', "\\") or character in "\b\f\n\r\t":
            size += 1
        elif codepoint < 0x20:
            # Other C0 controls use the six-byte ``\\u00xx`` spelling.
            size += 5
    return size


def validate_json_value(
    value: object,
    *,
    limits: AuthoringJsonLimits | None = None,
    require_object: bool = True,
    require_js_safe_integers: bool = True,
) -> dict[str, Any] | object:
    """Validate a detached JSON value without recursive Python calls.

    The node count includes containers, object keys, and scalar values.  This
    makes the budget conservative and stable across the parser and in-memory
    entrypoints.  Object keys are represented in locations only after they
    have passed the bounded-string gate.
    """

    active_limits = limits or AuthoringJsonLimits()
    if require_object and type(value) is not dict:
        raise AuthoringJsonError("top_level_object_required")

    stack: list[tuple[object, tuple[str | int, ...], int]] = [(value, (), 1)]
    nodes = 0
    canonical_size = 0

    def charge_canonical_bytes(
        count: int,
        *,
        location: tuple[str | int, ...],
    ) -> None:
        nonlocal canonical_size
        canonical_size += count
        if canonical_size > active_limits.max_document_bytes:
            raise AuthoringJsonError(
                "document_too_large",
                location_segments=location,
                actual=canonical_size,
                limit=active_limits.max_document_bytes,
            )

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

        if current is None:
            charge_canonical_bytes(4, location=location)
            continue
        if type(current) is bool:
            charge_canonical_bytes(4 if current else 5, location=location)
            continue
        if type(current) is int:
            if require_js_safe_integers and not (
                -JS_SAFE_INTEGER <= current <= JS_SAFE_INTEGER
            ):
                raise AuthoringJsonError(
                    "integer_outside_js_safe_range",
                    location_segments=location,
                )
            try:
                encoded_integer_size = len(str(current))
            except ValueError as exc:
                raise AuthoringJsonError(
                    "invalid_json_syntax",
                    location_segments=location,
                ) from exc
            charge_canonical_bytes(encoded_integer_size, location=location)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise AuthoringJsonError(
                    "non_finite_number",
                    location_segments=location,
                )
            charge_canonical_bytes(
                len(json.dumps(current, allow_nan=False)),
                location=location,
            )
            continue
        if type(current) is str:
            utf8_size = _checked_utf8_size(
                current,
                location=location,
                limits=active_limits,
            )
            charge_canonical_bytes(
                _canonical_string_size(current, utf8_size),
                location=location,
            )
            continue
        if type(current) is list:
            count = len(current)
            if count > active_limits.max_array_items:
                raise AuthoringJsonError(
                    "array_too_large",
                    location_segments=location,
                    actual=count,
                    limit=active_limits.max_array_items,
                )
            charge_canonical_bytes(
                2 + max(0, count - 1),
                location=location,
            )
            for index in range(count - 1, -1, -1):
                stack.append((current[index], (*location, index), depth + 1))
            continue
        if type(current) is dict:
            count = len(current)
            if count > active_limits.max_object_members:
                raise AuthoringJsonError(
                    "object_too_large",
                    location_segments=location,
                    actual=count,
                    limit=active_limits.max_object_members,
                )
            charge_canonical_bytes(
                2 + max(0, count - 1),
                location=location,
            )
            children: list[tuple[object, tuple[str | int, ...], int]] = []
            for key, child in current.items():
                if type(key) is not str:
                    raise AuthoringJsonError(
                        "non_string_object_key",
                        location_segments=location,
                    )
                key_utf8_size = _checked_utf8_size(
                    key,
                    location=location,
                    limits=active_limits,
                )
                charge_canonical_bytes(
                    _canonical_string_size(key, key_utf8_size) + 1,
                    location=location,
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


def bounded_canonical_json_bytes(
    value: object,
    *,
    limits: AuthoringJsonLimits | None = None,
    require_object: bool = True,
    require_js_safe_integers: bool = True,
) -> bytes:
    """Return canonical UTF-8 without trusting a separate size preflight.

    ``validate_json_value`` remains useful for early, precise diagnostics, but
    an in-memory caller can retain and mutate its containers after that first
    traversal.  The authoritative materialization therefore consumes the
    encoder incrementally and stops before retaining more than the document
    budget.  Callers which need a stable generation should strict-parse the
    returned bytes and use that detached value from then on.
    """

    active_limits = limits or AuthoringJsonLimits()
    validate_json_value(
        value,
        limits=active_limits,
        require_object=require_object,
        require_js_safe_integers=require_js_safe_integers,
    )
    payload = bytearray()

    def emit(chunk: bytes, *, location: tuple[str | int, ...]) -> None:
        resulting_size = len(payload) + len(chunk)
        if resulting_size > active_limits.max_document_bytes:
            raise AuthoringJsonError(
                "document_too_large",
                location_segments=location,
                actual=resulting_size,
                limit=active_limits.max_document_bytes,
            )
        payload.extend(chunk)

    # ``JSONEncoder.iterencode`` bounds repeated small values, but it still
    # creates one complete encoded chunk for a single string.  Walk exact JSON
    # built-ins ourselves so a value swapped in after the preflight receives
    # the string/container/number gates before any large encoded allocation.
    # Each container is copied through its exact built-in descriptor while the
    # GIL is held, giving this traversal a stable local generation without
    # invoking caller-defined iteration methods.
    stack: list[
        tuple[str, object, tuple[str | int, ...], int]
    ] = [("value", value, (), 1)]
    nodes = 0
    try:
        while stack:
            operation, current, location, depth = stack.pop()
            if operation == "emit":
                assert type(current) is bytes
                emit(current, location=location)
                continue
            if operation == "array_items":
                items, index = current  # type: ignore[misc]
                assert type(items) is list and type(index) is int
                if index >= len(items):
                    emit(b"]", location=location)
                    continue
                child_location = (*location, index)
                if index:
                    emit(b",", location=child_location)
                stack.append(
                    ("array_items", (items, index + 1), location, depth)
                )
                stack.append(
                    ("value", items[index], child_location, depth + 1)
                )
                continue
            if operation == "object_items":
                items, index = current  # type: ignore[misc]
                assert type(items) is list and type(index) is int
                if index >= len(items):
                    emit(b"}", location=location)
                    continue
                key, child = items[index]
                assert type(key) is str
                child_location = (*location, key)
                if index:
                    emit(b",", location=child_location)
                key_utf8_size = _checked_utf8_size(
                    key,
                    location=location,
                    limits=active_limits,
                )
                key_canonical_size = _canonical_string_size(
                    key,
                    key_utf8_size,
                )
                if (
                    len(payload) + key_canonical_size
                    > active_limits.max_document_bytes
                ):
                    raise AuthoringJsonError(
                        "document_too_large",
                        location_segments=child_location,
                        actual=len(payload) + key_canonical_size,
                        limit=active_limits.max_document_bytes,
                    )
                emit(
                    json.dumps(
                        key,
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8", errors="strict"),
                    location=child_location,
                )
                emit(b":", location=child_location)
                stack.append(
                    ("object_items", (items, index + 1), location, depth)
                )
                stack.append(
                    ("value", child, child_location, depth + 1)
                )
                continue

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

            if current is None:
                emit(b"null", location=location)
            elif type(current) is bool:
                emit(b"true" if current else b"false", location=location)
            elif type(current) is int:
                if require_js_safe_integers and not (
                    -JS_SAFE_INTEGER <= current <= JS_SAFE_INTEGER
                ):
                    raise AuthoringJsonError(
                        "integer_outside_js_safe_range",
                        location_segments=location,
                    )
                emit(str(current).encode("ascii"), location=location)
            elif type(current) is float:
                if not math.isfinite(current):
                    raise AuthoringJsonError(
                        "non_finite_number",
                        location_segments=location,
                    )
                emit(
                    json.dumps(current, allow_nan=False).encode("ascii"),
                    location=location,
                )
            elif type(current) is str:
                string_utf8_size = _checked_utf8_size(
                    current,
                    location=location,
                    limits=active_limits,
                )
                string_canonical_size = _canonical_string_size(
                    current,
                    string_utf8_size,
                )
                if (
                    len(payload) + string_canonical_size
                    > active_limits.max_document_bytes
                ):
                    raise AuthoringJsonError(
                        "document_too_large",
                        location_segments=location,
                        actual=len(payload) + string_canonical_size,
                        limit=active_limits.max_document_bytes,
                    )
                emit(
                    json.dumps(
                        current,
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8", errors="strict"),
                    location=location,
                )
            elif type(current) is list:
                items = list.copy(current)
                count = len(items)
                if count > active_limits.max_array_items:
                    raise AuthoringJsonError(
                        "array_too_large",
                        location_segments=location,
                        actual=count,
                        limit=active_limits.max_array_items,
                    )
                emit(b"[", location=location)
                stack.append(("array_items", (items, 0), location, depth))
            elif type(current) is dict:
                local = dict.copy(current)
                count = len(local)
                if count > active_limits.max_object_members:
                    raise AuthoringJsonError(
                        "object_too_large",
                        location_segments=location,
                        actual=count,
                        limit=active_limits.max_object_members,
                    )
                for key in local:
                    if type(key) is not str:
                        raise AuthoringJsonError(
                            "non_string_object_key",
                            location_segments=location,
                        )
                nodes += count
                if nodes > active_limits.max_nodes:
                    raise AuthoringJsonError(
                        "too_many_nodes",
                        location_segments=location,
                        actual=nodes,
                        limit=active_limits.max_nodes,
                    )
                items = sorted(local.items(), key=lambda item: item[0])
                emit(b"{", location=location)
                stack.append(("object_items", (items, 0), location, depth))
            else:
                raise AuthoringJsonError(
                    "unsupported_value_type",
                    location_segments=location,
                )
    except AuthoringJsonError:
        raise
    except (TypeError, ValueError, UnicodeError, RecursionError, RuntimeError) as exc:
        raise AuthoringJsonError("serialization_failed") from exc
    return bytes(payload)


def strict_json_loads(
    payload: str | bytes,
    *,
    limits: AuthoringJsonLimits | None = None,
    require_object: bool = True,
    require_js_safe_integers: bool = True,
) -> dict[str, Any] | object:
    """Decode one bounded UTF-8 JSON value, rejecting duplicate members."""

    active_limits = limits or AuthoringJsonLimits()
    if type(payload) is bytes:
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
    elif type(payload) is str:
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
        require_js_safe_integers=require_js_safe_integers,
    )


def json_document_bytes(
    document: Mapping[str, Any],
    *,
    limits: AuthoringJsonLimits | None = None,
) -> bytes:
    """Return bounded, deterministic, human-readable UTF-8."""

    active_limits = limits or AuthoringJsonLimits()
    # First capture one bounded compact generation.  Pretty-printing a strict
    # detached parse prevents a caller from swapping in a giant string or a
    # custom container between the value gate and JSONEncoder's next chunk.
    compact = bounded_canonical_json_bytes(
        document,
        limits=active_limits,
        require_object=True,
        require_js_safe_integers=True,
    )
    detached = strict_json_loads(
        compact,
        limits=active_limits,
        require_object=True,
        require_js_safe_integers=True,
    )
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    payload = bytearray()
    try:
        for text_chunk in encoder.iterencode(detached):
            chunk = text_chunk.encode("utf-8", errors="strict")
            # The public representation always includes its final newline.
            resulting_size = len(payload) + len(chunk) + 1
            if resulting_size > active_limits.max_document_bytes:
                raise AuthoringJsonError(
                    "document_too_large",
                    actual=resulting_size,
                    limit=active_limits.max_document_bytes,
                )
            payload.extend(chunk)
        payload.extend(b"\n")
    except AuthoringJsonError:
        raise
    except (
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        RuntimeError,
    ) as exc:
        raise AuthoringJsonError("serialization_failed") from exc
    return bytes(payload)


def validate_request_size(
    documents: Mapping[str, Mapping[str, Any]],
    *,
    maximum_bytes: int = MAX_AUTHORING_REQUEST_BYTES,
    limits: AuthoringJsonLimits | None = None,
) -> int:
    """Bound the combined serialized document payload for one save request."""

    if (
        type(maximum_bytes) is not int or maximum_bytes < 1
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
    "bounded_canonical_json_bytes",
    "json_document_bytes",
    "strict_json_loads",
    "validate_json_value",
    "validate_request_size",
]
