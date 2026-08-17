"""Trusted, immutable input snapshots for Tianlai score documents.

The score parser accepts ordinary Python dictionaries because most of the
render core operates on trusted values.  Entry points which bind identities
need a stronger boundary: the bytes that are hashed, the document that is
validated, and the parsed score must all describe one generation even when a
caller retains and mutates its input dictionary.

This module supports the existing unversioned (legacy) and schema-version-1
score formats, plus the isolated exact-time score-v2 model.  Parsing v2 here
does not make it executable by the legacy conductor: this boundary only binds
one immutable source generation to its parsed representation and identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable

from .authoring_json import (
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_bytes
from .plain_file import PlainFileIdentity, read_plain_file_bytes
from .resource_limits import (
    ProjectLimits,
    ResourceLimitError,
    validate_score_resource_limits,
)
from .score import ScoreDocument, parse_score_document
from .score_v2 import ScoreV2Document, parse_score_v2_document


_LEGACY_SCORE_VERSION = object()
_SCORE_V1_VERSION = object()
_SCORE_V2_VERSION = object()
ParsedScore = ScoreDocument | ScoreV2Document


def _freeze_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        # Wrap a fresh dictionary, never the caller's mapping.  With no
        # reachable reference to the backing dictionary, MappingProxyType
        # cannot be bypassed through ``dict.__setitem__`` or pickle/reduce.
        return MappingProxyType(
            {
                key: _freeze_json_value(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(item) for item in value]
    return value


def _parse_legacy_score(document: dict[str, Any]) -> ScoreDocument:
    return parse_score_document(document)


def _parse_score_v1(document: dict[str, Any]) -> ScoreDocument:
    return parse_score_document(document)


def _parse_score_v2(document: dict[str, Any]) -> ScoreV2Document:
    return parse_score_v2_document(document)


# Deliberately do not use ``dict.get(..., None)`` here: a missing version is a
# supported legacy contract, while an explicit null is an unsupported version.
# Private sentinel keys also prevent Python's ``bool``/``int``/``float`` key
# equality from silently widening either version contract.
_SCORE_VERSION_DISPATCH: dict[
    object, Callable[[dict[str, Any]], ParsedScore]
] = {
    _LEGACY_SCORE_VERSION: _parse_legacy_score,
    _SCORE_V1_VERSION: _parse_score_v1,
    _SCORE_V2_VERSION: _parse_score_v2,
}


def _score_json_limits(limits: ProjectLimits) -> AuthoringJsonLimits:
    """Translate project budgets into the strict score JSON boundary."""

    defaults = AuthoringJsonLimits()
    # A fully populated note contributes its object, keys, and scalar values
    # to the authoring node count.  Scale that structural budget with the
    # configured semantic score budget while retaining a useful small-score
    # floor and the independent canonical byte ceiling.
    max_nodes = max(
        4096,
        limits.max_notes * 32 + limits.max_parts * 64,
    )
    return AuthoringJsonLimits(
        max_document_bytes=limits.max_score_json_bytes,
        max_depth=defaults.max_depth,
        max_nodes=max_nodes,
        max_string_bytes=defaults.max_string_bytes,
        max_array_items=max(
            defaults.max_array_items,
            limits.max_notes,
            limits.max_parts,
        ),
        max_object_members=defaults.max_object_members,
    )


def _score_parser_for(document: dict[str, Any]) -> Callable[
    [dict[str, Any]], ParsedScore
]:
    if "schema_version" not in document:
        version_key = _LEGACY_SCORE_VERSION
    else:
        version = document["schema_version"]
        if type(version) is int and version == 1:
            version_key = _SCORE_V1_VERSION
        elif (
            (type(version) is int and version == 2)
            or (type(version) is float and version == 2.0)
        ):
            # JSON Schema's ``integer`` type includes mathematically integral
            # JSON numbers such as 2.0.  Score-v2 deliberately follows that
            # rule and its typed parser normalizes the value to integer 2.
            version_key = _SCORE_V2_VERSION
        else:
            raise ValueError(
                "score.schema_version must be integer 1, or score-v2 "
                "integer-valued number 2"
            )
    try:
        return _SCORE_VERSION_DISPATCH[version_key]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "only legacy scores and score.schema_version 1 or 2 are supported"
        ) from exc


def _freeze_parsed_score(score: ParsedScore) -> ParsedScore:
    if isinstance(score, ScoreV2Document):
        # Score-v2 is a recursively frozen typed graph, including opaque
        # extension payloads, so retaining the parsed value is safe.
        return score
    tuning = _freeze_json_value(score.tuning)
    assert isinstance(tuning, Mapping)
    return replace(score, tuning=tuning)


def _preflight_raw_score_counts(
    document: dict[str, Any],
    limits: ProjectLimits,
) -> None:
    """Apply configured fan-out limits before building a typed score graph."""

    raw_parts = document.get("parts")
    if type(raw_parts) is not list:
        return
    part_count = len(raw_parts)
    if part_count > limits.max_parts:
        raise ResourceLimitError(
            "score.too_many_parts",
            f"score part count {part_count:g} exceeds limit "
            f"{limits.max_parts:g}; raise TIANLAI_MAX_PARTS deliberately "
            "if this project is trusted",
            actual=part_count,
            limit=limits.max_parts,
        )
    note_count = 0
    for raw_part in raw_parts:
        if type(raw_part) is not dict:
            continue
        raw_notes = raw_part.get("notes")
        if type(raw_notes) is not list:
            continue
        note_count += len(raw_notes)
        if note_count > limits.max_notes:
            raise ResourceLimitError(
                "score.too_many_notes",
                f"score note count {note_count:g} exceeds limit "
                f"{limits.max_notes:g}; raise TIANLAI_MAX_NOTES deliberately "
                "if this project is trusted",
                actual=note_count,
                limit=limits.max_notes,
            )


@dataclass(frozen=True, slots=True, init=False)
class ScoreSourceSnapshot:
    """One immutable, canonicalized generation of a trusted score source."""

    canonical_bytes: bytes
    document_sha256: str
    document: Mapping[str, Any]
    _score: ParsedScore = field(repr=False)
    file_identity: PlainFileIdentity | None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("ScoreSourceSnapshot cannot be subclassed")

    def __init__(
        self,
        canonical_bytes: bytes,
        document_sha256: str,
        document: Mapping[str, Any],
        score: ParsedScore,
        file_identity: PlainFileIdentity | None = None,
    ) -> None:
        # A public constructor with the familiar signature gives callers one
        # deterministic failure instead of exposing a forgeable secret token.
        # The factory allocates the frozen slots directly after deriving every
        # field from one validated generation.
        raise TypeError(
            "ScoreSourceSnapshot must be created by a score-source factory"
        )

    @property
    def score(self) -> ParsedScore:
        """Return the bound typed score without exposing mutable state.

        Python's public ``object.__setattr__`` can deliberately bypass a
        frozen dataclass.  Re-parse the retained source document on every
        access so neither legacy/v1 nested objects nor score-v2's typed graph
        share a node with the identity-bearing generation held here.
        """

        document = self.document_copy()
        if isinstance(self._score, ScoreV2Document):
            return parse_score_v2_document(document)
        return parse_score_document(document)

    @property
    def identity_contract(self) -> str:
        return self._score.identity_contract

    @property
    def time_contract(self) -> str:
        return self._score.time_contract

    def document_copy(self) -> dict[str, Any]:
        """Return a fully detached, mutable copy of the bound document."""

        result = _thaw_json_value(self.document)
        if type(result) is not dict:
            raise TypeError("score snapshot document is not a JSON object")
        return result


def _bind_detached_document(
    document: dict[str, Any],
    canonical_bytes: bytes,
    *,
    limits: ProjectLimits,
    file_identity: PlainFileIdentity | None,
) -> ScoreSourceSnapshot:
    """Parse and validate one already-detached canonical generation."""

    if not isinstance(canonical_bytes, bytes):
        raise TypeError("canonical_bytes must be bytes")
    if not isinstance(document, dict):
        raise TypeError("document must be a JSON object")
    if file_identity is not None and not isinstance(
        file_identity,
        PlainFileIdentity,
    ):
        raise TypeError("file_identity must be a PlainFileIdentity or None")
    document_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
    parser = _score_parser_for(document)
    _preflight_raw_score_counts(document, limits)
    score = parser(document)
    if not isinstance(score, (ScoreDocument, ScoreV2Document)):
        raise TypeError(
            "score parser must return a ScoreDocument or ScoreV2Document"
        )
    validate_score_resource_limits(document, score, limits)
    # Internal parsing and validation are expected to be read-only.  This
    # comparison also keeps the private binder incapable of manufacturing a
    # snapshot from mismatched document/byte arguments.
    if canonical_json_bytes(document) != canonical_bytes:
        raise RuntimeError(
            "score parser mutated its source document or canonical bytes "
            "did not match"
        )
    frozen_document = _freeze_json_value(document)
    assert isinstance(frozen_document, Mapping)
    snapshot = object.__new__(ScoreSourceSnapshot)
    object.__setattr__(snapshot, "canonical_bytes", canonical_bytes)
    object.__setattr__(snapshot, "document_sha256", document_sha256)
    object.__setattr__(snapshot, "document", frozen_document)
    object.__setattr__(snapshot, "_score", _freeze_parsed_score(score))
    object.__setattr__(snapshot, "file_identity", file_identity)
    return snapshot


def snapshot_score_document(
    document: dict[str, Any],
    limits: ProjectLimits | None = None,
) -> ScoreSourceSnapshot:
    """Bind an in-memory score without retaining any caller-owned value.

    The caller's object first receives an iterative, bounded JSON-value
    preflight and is then traversed to produce canonical bytes.  Those exact
    bytes are strict-parsed into the detached value used for every subsequent
    operation, including version dispatch, semantic parsing, resource
    validation, and the returned snapshot.
    """

    if not isinstance(document, dict):
        raise TypeError("score document must be a dictionary")
    active_limits = limits or ProjectLimits.from_environment()
    json_limits = _score_json_limits(active_limits)
    # The canonical materializer performs the iterative value preflight and
    # independently enforces the byte ceiling while encoding.  The second
    # bound matters because an in-memory caller may retain and mutate its
    # containers between traversals.
    payload = bounded_canonical_json_bytes(
        document,
        limits=json_limits,
        require_object=True,
        require_js_safe_integers=True,
    )
    detached = strict_json_loads(
        payload,
        limits=json_limits,
        require_object=True,
        require_js_safe_integers=True,
    )
    assert isinstance(detached, dict)
    return _bind_detached_document(
        detached,
        payload,
        limits=active_limits,
        file_identity=None,
    )


def snapshot_score_bytes(
    source_bytes: bytes,
    limits: ProjectLimits | None = None,
) -> ScoreSourceSnapshot:
    """Bind one strict JSON byte generation without consulting live objects.

    This is the byte-oriented counterpart to :func:`snapshot_score_document`.
    It is useful when a downstream compiler needs to detach itself from a
    previously captured snapshot: the local generation is parsed, validated,
    canonically re-encoded, and hashed entirely from the one immutable bytes
    value supplied here.
    """

    if type(source_bytes) is not bytes:
        raise TypeError("score source bytes must be exact bytes")
    active_limits = limits or ProjectLimits.from_environment()
    json_limits = _score_json_limits(active_limits)
    detached = strict_json_loads(
        source_bytes,
        limits=json_limits,
        require_object=True,
        require_js_safe_integers=True,
    )
    assert isinstance(detached, dict)
    payload = bounded_canonical_json_bytes(
        detached,
        limits=json_limits,
        require_object=True,
        require_js_safe_integers=True,
    )
    return _bind_detached_document(
        detached,
        payload,
        limits=active_limits,
        file_identity=None,
    )


def read_score_snapshot(
    path: str | Path,
    limits: ProjectLimits | None = None,
) -> ScoreSourceSnapshot:
    """Read and bind one strict score JSON file through a single descriptor."""

    active_limits = limits or ProjectLimits.from_environment()
    identity, source_bytes = read_plain_file_bytes(
        path,
        maximum_bytes=active_limits.max_score_json_bytes,
    )
    detached = strict_json_loads(
        source_bytes,
        limits=_score_json_limits(active_limits),
        require_object=True,
        require_js_safe_integers=True,
    )
    assert isinstance(detached, dict)
    payload = canonical_json_bytes(detached)
    return _bind_detached_document(
        detached,
        payload,
        limits=active_limits,
        file_identity=identity,
    )


__all__ = [
    "ScoreSourceSnapshot",
    "read_score_snapshot",
    "snapshot_score_bytes",
    "snapshot_score_document",
]
