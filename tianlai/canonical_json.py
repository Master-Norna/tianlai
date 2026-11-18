"""Project-wide canonical JSON document identity.

Byte hashes are appropriate for release archives and opaque assets. Structured
JSON evidence instead uses this representation so that line endings, indentation
and object-key order do not invalidate the same JSON document value.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


HASH_ALGORITHM = "SHA-256"
CANONICALIZATION = "tianlai-json-v1"


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        document[key] = value
    return document


def canonical_json_bytes(document: Any) -> bytes:
    """Return the canonical UTF-8 representation of a JSON-compatible value."""

    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_json_sha256(document: Any) -> str:
    """Return the stable canonical-document SHA-256 of a JSON value.

    This identity deliberately ignores source formatting and object-key order,
    but it is not a cross-schema or default-aware semantic equivalence proof.
    Callers that need a render projection or another domain-specific identity
    must define and version that projection separately.
    """

    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def canonical_json_file_sha256(path: str | Path) -> str:
    """Parse a JSON file and hash its document rather than its source bytes."""

    document = json.loads(
        Path(path).read_text(encoding="utf-8"),
        object_pairs_hook=_duplicate_safe_object,
    )
    return canonical_json_sha256(document)


def json_values_equal(before: Any, after: Any) -> bool:
    """Compare two JSON values by meaning instead of by serialization.

    ``json.dumps`` writes ``1`` and ``1.0`` as different bytes, and ``0.0`` and
    ``-0.0`` as well, so a client that round-trips a document through a JSON
    library that widens integers re-serializes every number differently while
    the document still asserts exactly the same value.  A canonical hash cannot
    see that; comparing through Python ``==`` is not the answer either, because
    ``True == 1`` and the caller would then accept a real type change as "no
    edit".  This predicate requires the same JSON kind and then compares the
    value, so both directions stay honest.
    """

    if isinstance(before, bool) or isinstance(after, bool):
        return (
            isinstance(before, bool)
            and isinstance(after, bool)
            and before is after
        )
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return before == after
    if isinstance(before, dict) and isinstance(after, dict):
        if set(before) != set(after):
            return False
        return all(json_values_equal(before[key], after[key]) for key in before)
    if isinstance(before, (list, tuple)) and isinstance(after, (list, tuple)):
        if len(before) != len(after):
            return False
        return all(
            json_values_equal(left, right)
            for left, right in zip(before, after)
        )
    if type(before) is not type(after):
        return False
    return before == after


__all__ = [
    "CANONICALIZATION",
    "HASH_ALGORITHM",
    "canonical_json_bytes",
    "canonical_json_file_sha256",
    "canonical_json_sha256",
    "json_values_equal",
]
