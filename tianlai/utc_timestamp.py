"""Canonical UTC timestamp contract for durable Tianlai metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import re


CANONICAL_UTC_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-(?:0[1-9]|1[0-2])-"
    r"(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\."
    r"[0-9]{3}Z$"
)
_CANONICAL_UTC_TIMESTAMP = re.compile(CANONICAL_UTC_TIMESTAMP_PATTERN)


def canonical_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def validate_canonical_utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or _CANONICAL_UTC_TIMESTAMP.fullmatch(value) is None:
        raise ValueError("timestamp must be canonical UTC with millisecond precision")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise ValueError("timestamp is not a real UTC date-time") from exc
    canonical = parsed.replace(tzinfo=timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    if canonical != value:
        raise ValueError("timestamp is not canonical UTC")
    return value


__all__ = (
    "CANONICAL_UTC_TIMESTAMP_PATTERN",
    "canonical_utc_now",
    "validate_canonical_utc_timestamp",
)
