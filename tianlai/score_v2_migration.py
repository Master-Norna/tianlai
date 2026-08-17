"""Auditable, loss-aware migration from Tianlai score v1 to score v2.

The migration boundary deliberately accepts the source JSON document rather
than an already parsed ``ScoreDocument``.  It binds one trusted canonical
generation through :func:`snapshot_score_document`, so the source hash,
source spelling and parsed semantics cannot come from different revisions.

This module does not make score v2 renderable by the legacy conductor.  It
only separates and preserves the information that used to share the v1 score
document: notation in score v2, render settings, imported performance facts,
and an immutable migration receipt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import math
import re
from typing import Any

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_bytes
from .resource_limits import (
    ProjectLimits,
    ResourceLimitError,
    validate_score_resource_limits,
)
from .score import ScoreDocument, parse_score_document
from .score_source import ScoreSourceSnapshot, snapshot_score_document
from .score_time import ScoreTimeError, validate_score_time_coordinates
from .score_v2 import (
    MAX_METER_EVENTS,
    MAX_MEASURES,
    MAX_NOTES,
    MAX_PARTS,
    MAX_RATIONAL_DENOMINATOR,
    MAX_RELATIONS,
    MAX_SAFE_INTEGER,
    MAX_TEMPO_EVENTS,
    Rational,
    ScoreV2Document,
    parse_score_v2_document,
    score_render_projection_sha256,
)


MIGRATION_SCHEMA_VERSION = 1
MIGRATION_RECEIPT_DOMAIN = b"tianlai.score-v1-to-v2-receipt-v1\0"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NAMED_PITCH_RE = re.compile(r"^([A-Ga-g])(.*?)(-?\d+)$")
_NATURAL_PITCH_CLASSES = {
    "C": 0,
    "D": 2,
    "E": 4,
    "F": 5,
    "G": 7,
    "A": 9,
    "B": 11,
}
_SHARP_SPELLINGS: tuple[tuple[str, int], ...] = (
    ("C", 0),
    ("C", 1),
    ("D", 0),
    ("D", 1),
    ("E", 0),
    ("F", 0),
    ("F", 1),
    ("G", 0),
    ("G", 1),
    ("A", 0),
    ("A", 1),
    ("B", 0),
)
_ISSUE_SEVERITIES = frozenset(("info", "warning"))
_ISSUE_CATEGORIES = frozenset(
    ("notation_default", "derived_notation", "author_intent")
)
_MAX_RETAINED_MIGRATION_ISSUES = 4_096
_MIGRATION_POLICIES = {
    "number_conversion": "v1-parsed-number-decimal-text-exact-no-limiting",
    "meter_grouping": "single-group-from-v1-numerator",
    "tie_pairing": "next-same-part-pitch-staff-voice-exact-contiguous",
    "form": "explicit-linear",
    "velocity": "separated-as-score-bound-performance-fact",
    "timing": "v1-score-coordinates-preserved-no-provenance-inference",
    "implicit_defaults": "materialized-and-summarized-in-receipt",
}


class MigrationError(ValueError):
    """One source value cannot be represented under the score-v2 contract."""

    __slots__ = ("code", "location", "detail")

    def __init__(self, code: str, location: str, detail: str) -> None:
        self.code = _checked_nonblank_text(code, name="migration error code")
        self.location = _checked_nonblank_text(
            location,
            name="migration error location",
        )
        self.detail = _checked_nonblank_text(
            detail,
            name="migration error detail",
        )
        super().__init__(f"{self.code} at {self.location}: {self.detail}")

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "location": self.location,
            "detail": self.detail,
        }


def _checked_nonblank_text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a nonblank string")
    # A code-point count is a lower bound on UTF-8 bytes.  Apply it before
    # ``strip`` or ``encode`` so a forged frozen field cannot force a large
    # temporary allocation merely to be rejected.
    if len(value) > 16_384:
        raise ValueError(f"{name} is too long")
    if not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must contain valid Unicode") from exc
    if len(encoded) > 65_536:
        raise ValueError(f"{name} is too long")
    return value


def _bounded_diagnostic(error: BaseException) -> str:
    """Keep wrapped parser failures useful without reflecting huge inputs."""

    text = str(error)
    maximum = 4_096
    if len(text) > maximum:
        marker = "... [truncated]"
        text = text[: maximum - len(marker)] + marker
    text = text.strip() or type(error).__name__
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = ascii(text)
    if len(text) > maximum:
        marker = "... [truncated]"
        text = text[: maximum - len(marker)] + marker
    return text


def _checked_identifier(value: object, *, name: str) -> str:
    text = _checked_nonblank_text(value, name=name)
    if len(text) > 256 or len(text.encode("utf-8")) > 1_024:
        raise ValueError(f"{name} exceeds the score-v2 identifier bound")
    return text


def _ensure_v2_identifier(value: object, *, location: str) -> str:
    try:
        return _checked_identifier(value, name=location)
    except ValueError as exc:
        raise MigrationError(
            "target.identifier_not_representable",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _ensure_v2_text(value: object, *, location: str) -> str:
    if type(value) is not str:
        raise MigrationError(
            "target.text_not_representable",
            location,
            "the v1 parsed value is not text",
        )
    if len(value) > 4_096:
        raise MigrationError(
            "target.text_not_representable",
            location,
            "the text exceeds the score-v2 text bound",
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise MigrationError(
            "target.text_not_representable",
            location,
            "the text does not contain valid Unicode",
        ) from exc
    if len(encoded) > 16_384:
        raise MigrationError(
            "target.text_not_representable",
            location,
            "the text exceeds the score-v2 text bound",
        )
    return value


def _checked_sha256(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or _SHA256_RE.fullmatch(value) is None
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _validate_bound_rational(value: object, *, name: str) -> Rational:
    """Revalidate a frozen Rational before hashing retained artifacts.

    ``object.__setattr__`` can deliberately bypass a frozen dataclass.  The
    migration bundle is content-addressed, so hashing must never be the first
    operation which discovers a forged component or a non-normalized value.
    """

    if type(value) is not Rational:
        raise ValueError(f"{name} must be a Rational")
    if type(value.numerator) is not int or type(value.denominator) is not int:
        raise ValueError(f"{name} must retain integer components")
    try:
        normalized = Rational(value.numerator, value.denominator)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is not a representable Rational") from exc
    if (
        normalized.numerator != value.numerator
        or normalized.denominator != value.denominator
    ):
        raise ValueError(f"{name} must be normalized")
    return value


@dataclass(frozen=True, slots=True)
class MigrationIssue:
    """A non-audible default or unresolved v1 authoring intention."""

    code: str
    severity: str
    category: str
    location: str
    message: str
    audible: bool = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("MigrationIssue cannot be subclassed")

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _checked_nonblank_text(self.code, name="migration issue code")
        _checked_nonblank_text(self.location, name="migration issue location")
        _checked_nonblank_text(self.message, name="migration issue message")
        if type(self.severity) is not str or self.severity not in _ISSUE_SEVERITIES:
            raise ValueError("migration issue severity is invalid")
        if type(self.category) is not str or self.category not in _ISSUE_CATEGORIES:
            raise ValueError("migration issue category is invalid")
        if type(self.audible) is not bool:
            raise ValueError("migration issue audible must be a boolean")
        if self.audible:
            raise ValueError(
                "audible migration loss must be raised as MigrationError, "
                "not retained as an issue"
            )

    def to_dict(self) -> dict[str, str | bool]:
        self._validate()
        return {
            "code": self.code,
            "severity": self.severity,
            "category": self.category,
            "location": self.location,
            "message": self.message,
            "audible": self.audible,
        }


class _IssueCollector:
    """Retain bounded detail while making every omitted issue auditable."""

    __slots__ = ("_items", "_suppressed")

    def __init__(self) -> None:
        self._items: list[MigrationIssue] = []
        self._suppressed: dict[str, int] = {}

    def append(self, issue: MigrationIssue) -> None:
        if type(issue) is not MigrationIssue:
            raise TypeError("migration issue collector requires MigrationIssue")
        # Reserve one slot for the deterministic suppression summary.
        if len(self._items) < _MAX_RETAINED_MIGRATION_ISSUES - 1:
            self._items.append(issue)
            return
        self._suppressed[issue.code] = self._suppressed.get(issue.code, 0) + 1

    def freeze(self) -> tuple[MigrationIssue, ...]:
        if not self._suppressed:
            return tuple(self._items)
        counts = ", ".join(
            f"{code}={count}"
            for code, count in sorted(self._suppressed.items())
        )
        return (
            *self._items,
            MigrationIssue(
                code="audit.issue_details_summarized",
                severity="warning",
                category="author_intent",
                location="score",
                message=(
                    "migration issue detail exceeded the bounded receipt "
                    f"budget; suppressed counts: {counts}"
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class MigratedRenderSettings:
    """Renderer-owned v1 settings deliberately removed from score v2."""

    sample_rate: int
    tail_seconds: Rational

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("MigratedRenderSettings cannot be subclassed")

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if (
            type(self.sample_rate) is not int
            or self.sample_rate < 8_000
            or self.sample_rate > 384_000
        ):
            raise ValueError("sample_rate must be an integer from 8000 to 384000")
        tail_seconds = _validate_bound_rational(
            self.tail_seconds,
            name="tail_seconds",
        )
        if tail_seconds.numerator < 0:
            raise ValueError("tail_seconds must not be negative")

    def to_dict(self) -> dict[str, object]:
        self._validate()
        return {
            "kind": "tianlai.render_settings",
            "schema_version": 1,
            "sample_rate": self.sample_rate,
            "tail_seconds": self.tail_seconds.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class EventPerformanceFact:
    """One imported performance fact keyed by stable score-v2 event ID."""

    part_id: str
    event_id: str
    velocity: Rational

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("EventPerformanceFact cannot be subclassed")

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _checked_identifier(self.part_id, name="performance fact part_id")
        _checked_identifier(self.event_id, name="performance fact event_id")
        velocity = _validate_bound_rational(
            self.velocity,
            name="performance fact velocity",
        )
        value = velocity.as_fraction()
        if value <= 0 or value > 1:
            raise ValueError("performance fact velocity must be within (0, 1]")

    def to_dict(self) -> dict[str, object]:
        self._validate()
        return {
            "part_id": self.part_id,
            "event_id": self.event_id,
            "velocity": self.velocity.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MigratedPerformanceFacts:
    """Performance observations separated from the notation document."""

    score_document_sha256: str
    events: tuple[EventPerformanceFact, ...] = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("MigratedPerformanceFacts cannot be subclassed")

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _checked_sha256(
            self.score_document_sha256,
            name="performance facts score_document_sha256",
        )
        if type(self.events) is not tuple:
            raise ValueError("performance facts events must be an immutable tuple")
        if len(self.events) > MAX_NOTES:
            raise ValueError(
                f"performance facts support at most {MAX_NOTES} events"
            )
        seen: set[str] = set()
        for index, fact in enumerate(self.events):
            if type(fact) is not EventPerformanceFact:
                raise ValueError(
                    f"performance facts events[{index}] has an invalid type"
                )
            fact._validate()
            if fact.event_id in seen:
                raise ValueError(
                    f"duplicate performance fact event_id: {fact.event_id!r}"
                )
            seen.add(fact.event_id)

    def to_dict(self) -> dict[str, object]:
        self._validate()
        return {
            "kind": "tianlai.score_performance_facts",
            "schema_version": 1,
            "score_document_sha256": self.score_document_sha256,
            "events": [fact.to_dict() for fact in self.events],
        }


@dataclass(frozen=True, slots=True)
class ScoreV2MigrationReceipt:
    """Content-addressed proof of one deterministic migration decision."""

    source_document_sha256: str
    target_document_sha256: str
    target_render_projection_sha256: str
    render_settings_sha256: str
    performance_facts_sha256: str
    issues: tuple[MigrationIssue, ...] = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2MigrationReceipt cannot be subclassed")

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        _checked_sha256(
            self.source_document_sha256,
            name="receipt source_document_sha256",
        )
        _checked_sha256(
            self.target_document_sha256,
            name="receipt target_document_sha256",
        )
        _checked_sha256(
            self.target_render_projection_sha256,
            name="receipt target_render_projection_sha256",
        )
        _checked_sha256(
            self.render_settings_sha256,
            name="receipt render_settings_sha256",
        )
        _checked_sha256(
            self.performance_facts_sha256,
            name="receipt performance_facts_sha256",
        )
        if type(self.issues) is not tuple:
            raise ValueError("receipt issues must be an immutable tuple")
        if len(self.issues) > _MAX_RETAINED_MIGRATION_ISSUES:
            raise ValueError(
                "receipt issues exceed the bounded migration audit budget"
            )
        for index, issue in enumerate(self.issues):
            if type(issue) is not MigrationIssue:
                raise ValueError(f"receipt issues[{index}] has an invalid type")
            issue._validate()

    def to_dict(self) -> dict[str, object]:
        self._validate()
        return {
            "kind": "tianlai.score_v2_migration_receipt",
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "source": {
                "schema_version": 1,
                "identity_contract": "stable-event-v1",
                "document_sha256": self.source_document_sha256,
            },
            "target": {
                "schema_version": 2,
                "identity_contract": "stable-event-v2",
                "document_sha256": self.target_document_sha256,
                "render_projection_sha256": (
                    self.target_render_projection_sha256
                ),
            },
            "separated_artifacts": {
                "render_settings_sha256": self.render_settings_sha256,
                "performance_facts_sha256": self.performance_facts_sha256,
            },
            "policies": dict(_MIGRATION_POLICIES),
            "issues": [issue.to_dict() for issue in self.issues],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            MIGRATION_RECEIPT_DOMAIN + canonical_json_bytes(self.to_dict())
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ScoreV2Migration:
    """Complete, immutable result of a v1-to-v2 migration."""

    score: ScoreV2Document
    render_settings: MigratedRenderSettings
    performance_facts: MigratedPerformanceFacts
    receipt: ScoreV2MigrationReceipt
    _bound_receipt_sha256: str = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2Migration cannot be subclassed")

    def __post_init__(self) -> None:
        self._validate_bindings()
        object.__setattr__(
            self,
            "_bound_receipt_sha256",
            self.receipt.sha256,
        )

    def _validate_bindings(self) -> None:
        if type(self.score) is not ScoreV2Document:
            raise ValueError("migration score must be a ScoreV2Document")
        if type(self.render_settings) is not MigratedRenderSettings:
            raise ValueError("migration render_settings has an invalid type")
        if type(self.performance_facts) is not MigratedPerformanceFacts:
            raise ValueError("migration performance_facts has an invalid type")
        if type(self.receipt) is not ScoreV2MigrationReceipt:
            raise ValueError("migration receipt has an invalid type")
        try:
            bound_receipt_hash = self._bound_receipt_sha256
        except AttributeError:
            bound_receipt_hash = None
        current_receipt_hash = self.receipt.sha256
        if (
            bound_receipt_hash is not None
            and current_receipt_hash != bound_receipt_hash
        ):
            raise ValueError("migration receipt content changed after binding")

        document_hash = _score_document_sha256(self.score)
        projection_hash = score_render_projection_sha256(self.score)
        if self.receipt.target_document_sha256 != document_hash:
            raise ValueError("receipt target document hash does not match score")
        if self.receipt.target_render_projection_sha256 != projection_hash:
            raise ValueError("receipt render projection hash does not match score")
        render_settings_hash = _canonical_document_sha256(
            self.render_settings.to_dict()
        )
        if self.receipt.render_settings_sha256 != render_settings_hash:
            raise ValueError("receipt render settings hash does not match artifact")
        performance_facts_hash = _canonical_document_sha256(
            self.performance_facts.to_dict()
        )
        if self.receipt.performance_facts_sha256 != performance_facts_hash:
            raise ValueError("receipt performance facts hash does not match artifact")
        if self.performance_facts.score_document_sha256 != document_hash:
            raise ValueError("performance facts are not bound to the target score")

        event_owners = {
            note.event_id: part.part_id
            for part in self.score.parts
            for note in part.notes
        }
        for fact in self.performance_facts.events:
            if event_owners.get(fact.event_id) != fact.part_id:
                raise ValueError(
                    f"performance fact references the wrong or missing event "
                    f"{fact.event_id!r}"
                )

    @property
    def receipt_sha256(self) -> str:
        self._validate_bindings()
        return self._bound_receipt_sha256

    def to_dict(self) -> dict[str, object]:
        self._validate_bindings()
        return {
            "kind": "tianlai.score_v2_migration",
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "score": self.score.to_dict(),
            "render_settings": self.render_settings.to_dict(),
            "performance_facts": self.performance_facts.to_dict(),
            "receipt": self.receipt.to_dict(),
            "receipt_sha256": self.receipt_sha256,
        }


def _migration_json_limits(limits: ProjectLimits) -> AuthoringJsonLimits:
    """Return one explicit whole-bundle budget for external migration JSON."""

    defaults = AuthoringJsonLimits()
    return AuthoringJsonLimits(
        # A migration is a publication artifact, not a performance plan.  Its
        # score, facts and receipt share the score-source byte budget instead
        # of each receiving an independent allowance.
        max_document_bytes=limits.max_score_json_bytes,
        max_depth=defaults.max_depth,
        max_nodes=max(
            4_096,
            limits.max_notes * 48
            + limits.max_parts * 128
            + _MAX_RETAINED_MIGRATION_ISSUES * 16,
        ),
        max_string_bytes=defaults.max_string_bytes,
        max_array_items=max(
            defaults.max_array_items,
            limits.max_notes,
            limits.max_parts,
            _MAX_RETAINED_MIGRATION_ISSUES,
        ),
        max_object_members=defaults.max_object_members,
    )


def _bundle_error(code: str, location: str, detail: str) -> MigrationError:
    return MigrationError(code, location, detail)


def _bundle_object(
    value: object,
    *,
    location: str,
    allowed: frozenset[str],
    required: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise _bundle_error(
            "bundle.invalid_type",
            location,
            "must be a JSON object",
        )
    unknown_count = sum(1 for key in value if key not in allowed)
    if unknown_count:
        raise _bundle_error(
            "bundle.unknown_field",
            location,
            f"contains {unknown_count} unsupported field(s)",
        )
    missing_count = sum(1 for key in required if key not in value)
    if missing_count:
        raise _bundle_error(
            "bundle.missing_field",
            location,
            f"is missing {missing_count} required field(s)",
        )
    return value


def _bundle_array(
    value: object,
    *,
    location: str,
    maximum: int,
) -> list[Any]:
    if type(value) is not list:
        raise _bundle_error(
            "bundle.invalid_type",
            location,
            "must be a JSON array",
        )
    if len(value) > maximum:
        raise _bundle_error(
            "bundle.array_too_large",
            location,
            f"contains {len(value)} items; the limit is {maximum}",
        )
    return value


def _bundle_integral(
    value: object,
    *,
    location: str,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if type(value) not in (int, float):
        raise _bundle_error(
            "bundle.invalid_integer",
            location,
            "must be an integer-valued JSON number",
        )
    if type(value) is float and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise _bundle_error(
            "bundle.invalid_integer",
            location,
            "must be an integer-valued JSON number",
        )
    number = int(value)
    if abs(number) > MAX_SAFE_INTEGER:
        raise _bundle_error(
            "bundle.integer_outside_js_safe_range",
            location,
            "exceeds the JSON safe-integer range",
        )
    if minimum is not None and number < minimum:
        raise _bundle_error(
            "bundle.integer_out_of_range",
            location,
            f"must be at least {minimum}",
        )
    if maximum is not None and number > maximum:
        raise _bundle_error(
            "bundle.integer_out_of_range",
            location,
            f"must be at most {maximum}",
        )
    return number


def _bundle_literal(
    value: object,
    expected: object,
    *,
    location: str,
) -> None:
    if type(expected) is int:
        actual = _bundle_integral(value, location=location)
        matches = actual == expected
    else:
        matches = type(value) is type(expected) and value == expected
    if not matches:
        raise _bundle_error(
            "bundle.invalid_constant",
            location,
            "does not match the migration contract",
        )


def _bundle_sha256(value: object, *, location: str) -> str:
    try:
        return _checked_sha256(value, name=location)
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_sha256",
            location,
            "must be a lowercase SHA-256 digest",
        ) from exc


def _bundle_rational(value: object, *, location: str) -> Rational:
    raw = _bundle_object(
        value,
        location=location,
        allowed=frozenset(("numerator", "denominator")),
        required=frozenset(("numerator", "denominator")),
    )
    numerator = _bundle_integral(
        raw["numerator"],
        location=f"{location}.numerator",
    )
    denominator = _bundle_integral(
        raw["denominator"],
        location=f"{location}.denominator",
        minimum=1,
        maximum=MAX_RATIONAL_DENOMINATOR,
    )
    try:
        return Rational(numerator, denominator)
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_rational",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _bundle_identifier(value: object, *, location: str) -> str:
    try:
        return _checked_identifier(value, name=location)
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_identifier",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _preflight_migration_counts(
    document: dict[str, Any],
    limits: ProjectLimits,
) -> None:
    """Reject configured fan-out before constructing any typed note graph."""

    score = document.get("score")
    if type(score) is dict:
        parts = score.get("parts")
        if type(parts) is list:
            if len(parts) > limits.max_parts:
                raise _bundle_error(
                    "bundle.too_many_parts",
                    "migration.score.parts",
                    f"contains {len(parts)} parts; the limit is {limits.max_parts}",
                )
            note_count = 0
            for part in parts:
                if type(part) is not dict:
                    continue
                notes = part.get("notes")
                if type(notes) is not list:
                    continue
                note_count += len(notes)
                if note_count > limits.max_notes:
                    raise _bundle_error(
                        "bundle.too_many_notes",
                        "migration.score.parts",
                        f"contains more than {limits.max_notes} notes",
                    )

    facts = document.get("performance_facts")
    if type(facts) is dict:
        events = facts.get("events")
        if type(events) is list and len(events) > limits.max_notes:
            raise _bundle_error(
                "bundle.too_many_performance_facts",
                "migration.performance_facts.events",
                f"contains {len(events)} events; the limit is {limits.max_notes}",
            )

    receipt = document.get("receipt")
    if type(receipt) is dict:
        issues = receipt.get("issues")
        if (
            type(issues) is list
            and len(issues) > _MAX_RETAINED_MIGRATION_ISSUES
        ):
            raise _bundle_error(
                "bundle.too_many_issues",
                "migration.receipt.issues",
                "exceeds the bounded migration audit budget",
            )


def _parse_migrated_render_settings(value: object) -> MigratedRenderSettings:
    location = "migration.render_settings"
    raw = _bundle_object(
        value,
        location=location,
        allowed=frozenset(
            ("kind", "schema_version", "sample_rate", "tail_seconds")
        ),
        required=frozenset(
            ("kind", "schema_version", "sample_rate", "tail_seconds")
        ),
    )
    _bundle_literal(raw["kind"], "tianlai.render_settings", location=f"{location}.kind")
    _bundle_literal(raw["schema_version"], 1, location=f"{location}.schema_version")
    sample_rate = _bundle_integral(
        raw["sample_rate"],
        location=f"{location}.sample_rate",
        minimum=8_000,
        maximum=384_000,
    )
    try:
        return MigratedRenderSettings(
            sample_rate=sample_rate,
            tail_seconds=_bundle_rational(
                raw["tail_seconds"],
                location=f"{location}.tail_seconds",
            ),
        )
    except MigrationError:
        raise
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_render_settings",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _parse_performance_fact(value: object, *, index: int) -> EventPerformanceFact:
    location = f"migration.performance_facts.events[{index}]"
    raw = _bundle_object(
        value,
        location=location,
        allowed=frozenset(("part_id", "event_id", "velocity")),
        required=frozenset(("part_id", "event_id", "velocity")),
    )
    try:
        return EventPerformanceFact(
            part_id=_bundle_identifier(
                raw["part_id"], location=f"{location}.part_id"
            ),
            event_id=_bundle_identifier(
                raw["event_id"], location=f"{location}.event_id"
            ),
            velocity=_bundle_rational(
                raw["velocity"], location=f"{location}.velocity"
            ),
        )
    except MigrationError:
        raise
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_performance_fact",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _parse_migrated_performance_facts(
    value: object,
    *,
    maximum_events: int,
) -> MigratedPerformanceFacts:
    location = "migration.performance_facts"
    raw = _bundle_object(
        value,
        location=location,
        allowed=frozenset(
            ("kind", "schema_version", "score_document_sha256", "events")
        ),
        required=frozenset(
            ("kind", "schema_version", "score_document_sha256", "events")
        ),
    )
    _bundle_literal(
        raw["kind"],
        "tianlai.score_performance_facts",
        location=f"{location}.kind",
    )
    _bundle_literal(raw["schema_version"], 1, location=f"{location}.schema_version")
    events = _bundle_array(
        raw["events"],
        location=f"{location}.events",
        maximum=min(MAX_NOTES, maximum_events),
    )
    try:
        return MigratedPerformanceFacts(
            score_document_sha256=_bundle_sha256(
                raw["score_document_sha256"],
                location=f"{location}.score_document_sha256",
            ),
            events=tuple(
                _parse_performance_fact(item, index=index)
                for index, item in enumerate(events)
            ),
        )
    except MigrationError:
        raise
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_performance_facts",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _parse_migration_issue(value: object, *, index: int) -> MigrationIssue:
    location = f"migration.receipt.issues[{index}]"
    raw = _bundle_object(
        value,
        location=location,
        allowed=frozenset(
            ("code", "severity", "category", "location", "message", "audible")
        ),
        required=frozenset(
            ("code", "severity", "category", "location", "message", "audible")
        ),
    )
    try:
        return MigrationIssue(
            code=raw["code"],
            severity=raw["severity"],
            category=raw["category"],
            location=raw["location"],
            message=raw["message"],
            audible=raw["audible"],
        )
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_issue",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _parse_migration_receipt(value: object) -> ScoreV2MigrationReceipt:
    location = "migration.receipt"
    raw = _bundle_object(
        value,
        location=location,
        allowed=frozenset(
            (
                "kind",
                "schema_version",
                "source",
                "target",
                "separated_artifacts",
                "policies",
                "issues",
            )
        ),
        required=frozenset(
            (
                "kind",
                "schema_version",
                "source",
                "target",
                "separated_artifacts",
                "policies",
                "issues",
            )
        ),
    )
    _bundle_literal(
        raw["kind"],
        "tianlai.score_v2_migration_receipt",
        location=f"{location}.kind",
    )
    _bundle_literal(raw["schema_version"], 1, location=f"{location}.schema_version")

    source = _bundle_object(
        raw["source"],
        location=f"{location}.source",
        allowed=frozenset(
            ("schema_version", "identity_contract", "document_sha256")
        ),
        required=frozenset(
            ("schema_version", "identity_contract", "document_sha256")
        ),
    )
    _bundle_literal(
        source["schema_version"], 1, location=f"{location}.source.schema_version"
    )
    _bundle_literal(
        source["identity_contract"],
        "stable-event-v1",
        location=f"{location}.source.identity_contract",
    )

    target = _bundle_object(
        raw["target"],
        location=f"{location}.target",
        allowed=frozenset(
            (
                "schema_version",
                "identity_contract",
                "document_sha256",
                "render_projection_sha256",
            )
        ),
        required=frozenset(
            (
                "schema_version",
                "identity_contract",
                "document_sha256",
                "render_projection_sha256",
            )
        ),
    )
    _bundle_literal(
        target["schema_version"], 2, location=f"{location}.target.schema_version"
    )
    _bundle_literal(
        target["identity_contract"],
        "stable-event-v2",
        location=f"{location}.target.identity_contract",
    )

    artifacts = _bundle_object(
        raw["separated_artifacts"],
        location=f"{location}.separated_artifacts",
        allowed=frozenset(
            ("render_settings_sha256", "performance_facts_sha256")
        ),
        required=frozenset(
            ("render_settings_sha256", "performance_facts_sha256")
        ),
    )
    policies = _bundle_object(
        raw["policies"],
        location=f"{location}.policies",
        allowed=frozenset(_MIGRATION_POLICIES),
        required=frozenset(_MIGRATION_POLICIES),
    )
    for key, expected in _MIGRATION_POLICIES.items():
        _bundle_literal(
            policies[key],
            expected,
            location=f"{location}.policies.{key}",
        )
    issues = _bundle_array(
        raw["issues"],
        location=f"{location}.issues",
        maximum=_MAX_RETAINED_MIGRATION_ISSUES,
    )
    try:
        return ScoreV2MigrationReceipt(
            source_document_sha256=_bundle_sha256(
                source["document_sha256"],
                location=f"{location}.source.document_sha256",
            ),
            target_document_sha256=_bundle_sha256(
                target["document_sha256"],
                location=f"{location}.target.document_sha256",
            ),
            target_render_projection_sha256=_bundle_sha256(
                target["render_projection_sha256"],
                location=f"{location}.target.render_projection_sha256",
            ),
            render_settings_sha256=_bundle_sha256(
                artifacts["render_settings_sha256"],
                location=(
                    f"{location}.separated_artifacts.render_settings_sha256"
                ),
            ),
            performance_facts_sha256=_bundle_sha256(
                artifacts["performance_facts_sha256"],
                location=(
                    f"{location}.separated_artifacts.performance_facts_sha256"
                ),
            ),
            issues=tuple(
                _parse_migration_issue(item, index=index)
                for index, item in enumerate(issues)
            ),
        )
    except MigrationError:
        raise
    except ValueError as exc:
        raise _bundle_error(
            "bundle.invalid_receipt",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _detach_migration_document(
    raw: dict[str, Any] | bytes,
    *,
    limits: AuthoringJsonLimits,
) -> dict[str, Any]:
    try:
        if type(raw) is bytes:
            detached = strict_json_loads(
                raw,
                limits=limits,
                require_object=True,
                require_js_safe_integers=True,
            )
        elif type(raw) is dict:
            # Reparse a canonical copy so neither the caller's mappings nor
            # aliased nested lists are retained by the immutable result.
            detached = strict_json_loads(
                bounded_canonical_json_bytes(
                    raw,
                    limits=limits,
                    require_object=True,
                    require_js_safe_integers=True,
                ),
                limits=limits,
                require_object=True,
                require_js_safe_integers=True,
            )
        else:
            raise _bundle_error(
                "bundle.invalid_input_type",
                "migration",
                "must be supplied as a dictionary or UTF-8 JSON bytes",
            )
    except MigrationError:
        raise
    except AuthoringJsonError as exc:
        raise _bundle_error(
            "bundle.invalid_json",
            "migration",
            f"strict JSON boundary rejected the document ({exc.code})",
        ) from exc
    except (TypeError, ValueError, RecursionError) as exc:
        raise _bundle_error(
            "bundle.invalid_json",
            "migration",
            _bounded_diagnostic(exc),
        ) from exc
    assert type(detached) is dict
    return detached


def parse_score_v2_migration_document(
    raw: dict[str, Any] | bytes,
    *,
    limits: ProjectLimits | None = None,
) -> ScoreV2Migration:
    """Strict-parse and cryptographically cross-check one migration bundle.

    This entrypoint proves that the embedded score and separated artifacts
    match every target/artifact digest in the receipt, and independently
    recomputes the domain-separated receipt digest.  The receipt's source
    digest is a claim until :func:`verify_score_v2_migration_document` is
    given the trusted v1 source snapshot.
    """

    active_limits = limits or ProjectLimits.from_environment()
    # In-memory callers may hand us an already enormous fan-out while asking
    # for a much smaller semantic budget.  Reject the cheap, exact built-in
    # counts before copying/encoding that graph, then repeat the same gate on
    # the detached generation below so a concurrent mutation cannot bypass it.
    if type(raw) is dict:
        _preflight_migration_counts(raw, active_limits)
    document = _detach_migration_document(
        raw,
        limits=_migration_json_limits(active_limits),
    )
    _preflight_migration_counts(document, active_limits)
    top = _bundle_object(
        document,
        location="migration",
        allowed=frozenset(
            (
                "kind",
                "schema_version",
                "score",
                "render_settings",
                "performance_facts",
                "receipt",
                "receipt_sha256",
            )
        ),
        required=frozenset(
            (
                "kind",
                "schema_version",
                "score",
                "render_settings",
                "performance_facts",
                "receipt",
                "receipt_sha256",
            )
        ),
    )
    _bundle_literal(
        top["kind"],
        "tianlai.score_v2_migration",
        location="migration.kind",
    )
    _bundle_literal(
        top["schema_version"],
        MIGRATION_SCHEMA_VERSION,
        location="migration.schema_version",
    )
    if type(top["score"]) is not dict:
        raise _bundle_error(
            "bundle.invalid_type",
            "migration.score",
            "must be a JSON object",
        )
    try:
        score = parse_score_v2_document(top["score"])
        validate_score_resource_limits(top["score"], score, active_limits)
    except ResourceLimitError as exc:
        raise _bundle_error(
            "bundle.score_resource_limit",
            "migration.score",
            f"score resource gate rejected the document ({exc.code})",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise _bundle_error(
            "bundle.invalid_score",
            "migration.score",
            _bounded_diagnostic(exc),
        ) from exc

    render_settings = _parse_migrated_render_settings(top["render_settings"])
    performance_facts = _parse_migrated_performance_facts(
        top["performance_facts"],
        maximum_events=active_limits.max_notes,
    )
    receipt = _parse_migration_receipt(top["receipt"])
    claimed_receipt_hash = _bundle_sha256(
        top["receipt_sha256"],
        location="migration.receipt_sha256",
    )
    actual_receipt_hash = receipt.sha256
    if claimed_receipt_hash != actual_receipt_hash:
        raise _bundle_error(
            "bundle.receipt_hash_mismatch",
            "migration.receipt_sha256",
            "does not match the recomputed domain-separated receipt digest",
        )
    try:
        return ScoreV2Migration(
            score=score,
            render_settings=render_settings,
            performance_facts=performance_facts,
            receipt=receipt,
        )
    except ValueError as exc:
        raise _bundle_error(
            "bundle.binding_mismatch",
            "migration",
            _bounded_diagnostic(exc),
        ) from exc


def verify_score_v2_migration_document(
    raw: dict[str, Any] | bytes,
    source_snapshot: ScoreSourceSnapshot,
    *,
    limits: ProjectLimits | None = None,
) -> ScoreV2Migration:
    """Verify a bundle against one trusted v1 source by replaying migration.

    Parsing alone can prove every output-side digest, but no self-contained
    receipt can prove that its source digest names the caller's intended
    source generation.  This verifier rebinds the supplied snapshot under the
    current limits, compares the source digest, and reruns the deterministic
    v1-to-v2 migration before accepting the bundle.
    """

    if type(source_snapshot) is not ScoreSourceSnapshot:
        raise TypeError("source_snapshot must be a trusted ScoreSourceSnapshot")
    active_limits = limits or ProjectLimits.from_environment()
    migration = parse_score_v2_migration_document(raw, limits=active_limits)
    try:
        rebound = snapshot_score_document(
            source_snapshot.document_copy(),
            limits=active_limits,
        )
    except (TypeError, ValueError) as exc:
        raise _bundle_error(
            "bundle.invalid_source_snapshot",
            "source",
            _bounded_diagnostic(exc),
        ) from exc
    if (
        type(source_snapshot.canonical_bytes) is not bytes
        or source_snapshot.canonical_bytes != rebound.canonical_bytes
        or source_snapshot.document_sha256 != rebound.document_sha256
    ):
        raise _bundle_error(
            "bundle.invalid_source_snapshot",
            "source",
            "snapshot document, bytes and digest are not one generation",
        )
    if migration.receipt.source_document_sha256 != rebound.document_sha256:
        raise _bundle_error(
            "bundle.source_hash_mismatch",
            "migration.receipt.source.document_sha256",
            "does not match the recomputed source document digest",
        )
    try:
        expected = migrate_score_v1_snapshot(rebound)
    except MigrationError as exc:
        raise _bundle_error(
            "bundle.source_migration_failed",
            "source",
            _bounded_diagnostic(exc),
        ) from exc
    if migration != expected:
        raise _bundle_error(
            "bundle.transformation_mismatch",
            "migration",
            "does not match deterministic migration of the bound source",
        )
    return migration


def score_v2_migration_json_bytes(
    migration: ScoreV2Migration,
    *,
    limits: ProjectLimits | None = None,
) -> bytes:
    """Serialize one validated bundle under the same limit as its parser."""

    if type(migration) is not ScoreV2Migration:
        raise TypeError("migration must be a ScoreV2Migration")
    active_limits = limits or ProjectLimits.from_environment()
    try:
        return bounded_canonical_json_bytes(
            migration.to_dict(),
            limits=_migration_json_limits(active_limits),
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise _bundle_error(
            "bundle.serialization_limit",
            "migration",
            f"strict JSON boundary rejected the document ({exc.code})",
        ) from exc
    except (TypeError, ValueError, RecursionError) as exc:
        raise _bundle_error(
            "bundle.serialization_failed",
            "migration",
            _bounded_diagnostic(exc),
        ) from exc


def _rational_from_number(value: object, *, location: str) -> Rational:
    if type(value) not in (int, float):
        raise MigrationError(
            "numeric.invalid_source_number",
            location,
            "the v1 value is not an integer or floating-point number",
        )
    if type(value) is float and not math.isfinite(value):
        raise MigrationError(
            "numeric.invalid_source_number",
            location,
            "the v1 value is not finite",
        )
    try:
        exact = Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise MigrationError(
            "numeric.invalid_source_number",
            location,
            "the v1 value cannot be converted from decimal text",
        ) from exc
    return _rational_from_fraction(exact, location=location)


def _rational_from_fraction(value: Fraction, *, location: str) -> Rational:
    if value.denominator > MAX_RATIONAL_DENOMINATOR:
        raise MigrationError(
            "numeric.denominator_exceeds_v2_limit",
            location,
            (
                f"exact denominator {value.denominator} exceeds "
                f"{MAX_RATIONAL_DENOMINATOR}; migration will not approximate it"
            ),
        )
    if (
        abs(value.numerator) > MAX_SAFE_INTEGER
        or value.denominator > MAX_SAFE_INTEGER
    ):
        raise MigrationError(
            "numeric.safe_integer_exceeded",
            location,
            "the exact rational components exceed the JSON safe-integer bound",
        )
    try:
        return Rational(value.numerator, value.denominator)
    except ValueError as exc:
        raise MigrationError(
            "numeric.not_representable_in_score_v2",
            location,
            _bounded_diagnostic(exc),
        ) from exc


def _score_document_sha256(score: ScoreV2Document) -> str:
    return _canonical_document_sha256(score.to_dict())


def _canonical_document_sha256(document: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _position_dict(measure_id: str, offset: Rational) -> dict[str, object]:
    return {
        "measure_id": measure_id,
        "offset_quarters": offset.to_dict(),
    }


def _measure_id(bar: int) -> str:
    return f"measure-v1-{bar:06d}"


def _position_rational(
    *,
    bar: int,
    beat: float,
    beat_unit: int,
    location: str,
) -> Rational:
    beat_value = _rational_from_number(beat, location=f"{location}.beat")
    offset = (beat_value.as_fraction() - 1) * Fraction(4, beat_unit)
    return _rational_from_fraction(
        offset,
        location=f"{location}.offset_quarters",
    )


def _duration_rational(
    *,
    duration_beats: float,
    beat_unit: int,
    location: str,
) -> Rational:
    beats = _rational_from_number(
        duration_beats,
        location=f"{location}.duration_beats",
    )
    return _rational_from_fraction(
        beats.as_fraction() * Fraction(4, beat_unit),
        location=f"{location}.duration_quarters",
    )


def _named_written_pitch(
    raw_pitch: str,
    sounding: Rational,
    *,
    location: str,
) -> dict[str, object]:
    text = raw_pitch.strip()
    match = _NAMED_PITCH_RE.fullmatch(text)
    if match is None:
        # The trusted v1 parser accepted this spelling, but this migrator
        # cannot split it into score-v2's explicit written-pitch components.
        raise MigrationError(
            "pitch.named_spelling_not_decomposable",
            location,
            f"cannot decompose the accepted v1 pitch spelling {raw_pitch!r}",
        )
    step = match.group(1).upper()
    accidental = match.group(2)
    octave = int(match.group(3))
    natural_midi = (octave + 1) * 12 + _NATURAL_PITCH_CLASSES[step]
    alter = _rational_from_fraction(
        sounding.as_fraction() - natural_midi,
        location=f"{location}.written_pitch.alter",
    )
    result: dict[str, object] = {
        "step": step,
        "alter": alter.to_dict(),
        "octave": octave,
    }
    if accidental:
        # Preserve the exact accidental token that v1 accepted.  The numeric
        # alter remains authoritative, so glyph naming never changes pitch.
        result["accidental"] = _ensure_v2_identifier(
            accidental,
            location=f"{location}.written_pitch.accidental",
        )
    return result


def _derived_written_pitch(sounding: Rational) -> dict[str, object]:
    exact = sounding.as_fraction()
    lower_semitone = exact.numerator // exact.denominator
    pitch_class = lower_semitone % 12
    step, _ = _SHARP_SPELLINGS[pitch_class]
    octave = lower_semitone // 12 - 1
    natural_midi = (octave + 1) * 12 + _NATURAL_PITCH_CLASSES[step]
    alter = Rational(
        (exact - natural_midi).numerator,
        (exact - natural_midi).denominator,
    )
    # ``alter`` can only have the already-validated sounding denominator.
    return {
        "step": step,
        "alter": alter.to_dict(),
        "octave": octave,
    }


@dataclass(frozen=True, slots=True)
class _MigratedNote:
    event_id: str
    source_location: str
    start_absolute: Fraction
    end_absolute: Fraction
    legacy_start: float
    legacy_duration: float
    sounding: Rational
    staff: int | None
    voice: str | None
    starts_tie: bool

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("_MigratedNote cannot be subclassed")

    def __post_init__(self) -> None:
        _checked_identifier(self.event_id, name="migrated event_id")
        _checked_nonblank_text(
            self.source_location,
            name="migrated note source location",
        )
        if type(self.start_absolute) is not Fraction:
            raise ValueError("migrated note start_absolute must be a Fraction")
        if type(self.end_absolute) is not Fraction:
            raise ValueError("migrated note end_absolute must be a Fraction")
        if self.start_absolute < 0 or self.end_absolute <= self.start_absolute:
            raise ValueError("migrated note exact extent is invalid")
        if (
            type(self.legacy_start) is not float
            or type(self.legacy_duration) is not float
            or not math.isfinite(self.legacy_start)
            or not math.isfinite(self.legacy_duration)
            or self.legacy_start < 0
            or self.legacy_duration <= 0
        ):
            raise ValueError("migrated note legacy extent is invalid")
        if type(self.sounding) is not Rational:
            raise ValueError("migrated note sounding pitch must be a Rational")
        if self.staff is not None and (
            type(self.staff) is not int or self.staff < 1
        ):
            raise ValueError("migrated note staff must be a positive integer")
        if self.voice is not None:
            _checked_nonblank_text(self.voice, name="migrated note voice")
        if type(self.starts_tie) is not bool:
            raise ValueError("migrated note starts_tie must be a boolean")


def _tie_documents(
    notes_by_part: tuple[tuple[_MigratedNote, ...], ...],
    issues: _IssueCollector,
) -> list[dict[str, str]]:
    ties: list[dict[str, str]] = []
    for part_notes in notes_by_part:
        pending: dict[
            tuple[Rational, int | None, str | None],
            tuple[_MigratedNote, float, float],
        ] = {}
        for note in part_notes:
            key = (note.sounding, note.staff, note.voice)
            held = pending.pop(key, None)
            if held is not None:
                source, legacy_chain_start, legacy_chain_duration = held
                exact_contiguous = source.end_absolute == note.start_absolute
                legacy_contiguous = math.isclose(
                    legacy_chain_start + legacy_chain_duration,
                    note.legacy_start,
                    rel_tol=1e-9,
                    abs_tol=1e-6,
                )
                if exact_contiguous and legacy_contiguous:
                    ties.append(
                        {
                            "tie_id": f"tie-v1-{len(ties) + 1:06d}",
                            "from_event_id": source.event_id,
                            "to_event_id": note.event_id,
                        }
                    )
                    if note.starts_tie:
                        pending[key] = (
                            note,
                            legacy_chain_start,
                            legacy_chain_duration + note.legacy_duration,
                        )
                    continue
                if exact_contiguous:
                    raise MigrationError(
                        "tie.legacy_chain_cannot_be_preserved",
                        f"{source.source_location}.tie",
                        (
                            "score v2 would join this exact edge, but the "
                            "legacy conductor's chain-accumulated floating "
                            "extent does not join it"
                        ),
                    )
                if legacy_contiguous:
                    raise MigrationError(
                        "tie.float_tolerance_cannot_be_preserved",
                        f"{source.source_location}.tie",
                        (
                            "the legacy conductor joins this tie only by "
                            "floating-point tolerance, while score v2 "
                            "requires exact contiguity"
                        ),
                    )
                issues.append(
                    MigrationIssue(
                        code="tie.intent_not_contiguous",
                        severity="warning",
                        category="author_intent",
                        location=f"{source.source_location}.tie",
                        message=(
                            "v1 tie=true has no exactly contiguous next "
                            "note with the same pitch, staff and voice"
                        ),
                    )
                )
            if note.starts_tie:
                pending[key] = (
                    note,
                    note.legacy_start,
                    note.legacy_duration,
                )

        for source, _legacy_start, _legacy_duration in pending.values():
            issues.append(
                MigrationIssue(
                    code="tie.intent_dangling",
                    severity="warning",
                    category="author_intent",
                    location=f"{source.source_location}.tie",
                    message=(
                        "v1 tie=true has no following note with the same "
                        "pitch, staff and voice"
                    ),
                )
            )
    return ties


def migrate_score_v1_snapshot(
    snapshot: ScoreSourceSnapshot,
) -> ScoreV2Migration:
    """Migrate one already bound, trusted score-v1 source snapshot.

    Legacy documents are intentionally rejected.  Call
    :func:`tianlai.score.upgrade_legacy_score_to_v1`, save the resulting
    stable event IDs, and then migrate that explicit v1 generation.

    File-oriented callers should obtain ``snapshot`` through
    :func:`tianlai.score_source.read_score_snapshot`.  This keeps the bytes
    that were descriptor-read, hashed and parsed bound to one generation.
    """

    if type(snapshot) is not ScoreSourceSnapshot:
        raise TypeError("snapshot must be a trusted ScoreSourceSnapshot")
    # Frozen dataclasses can still be deliberately altered with
    # ``object.__setattr__``.  Rebind all identity-bearing fields here so a
    # cross-generation snapshot cannot yield a plausible migration receipt.
    try:
        source_document = snapshot.document_copy()
        source_canonical = canonical_json_bytes(source_document)
    except (TypeError, ValueError, RecursionError) as exc:
        raise MigrationError(
            "source.snapshot_binding_invalid",
            "score",
            "the retained source snapshot is not canonical portable JSON",
        ) from exc
    if (
        type(snapshot.canonical_bytes) is not bytes
        or snapshot.canonical_bytes != source_canonical
        or snapshot.document_sha256
        != hashlib.sha256(source_canonical).hexdigest()
    ):
        raise MigrationError(
            "source.snapshot_binding_invalid",
            "score",
            "snapshot document, canonical bytes and identity are not one generation",
        )
    source_schema_version = source_document.get("schema_version")
    if (
        (type(source_schema_version) is int and source_schema_version == 2)
        or (
            type(source_schema_version) is float
            and source_schema_version == 2.0
        )
    ):
        raise MigrationError(
            "source.unsupported_score_model",
            "score.schema_version",
            "the source must parse as a score-v1 ScoreDocument",
        )
    try:
        source_score = parse_score_document(source_document)
    except (TypeError, ValueError) as exc:
        raise MigrationError(
            "source.snapshot_binding_invalid",
            "score",
            "the retained snapshot document no longer parses as its bound score",
        ) from exc
    if type(source_score) is not ScoreDocument:
        raise MigrationError(
            "source.unsupported_score_model",
            "score.schema_version",
            "the source must parse as a score-v1 ScoreDocument",
        )
    if (
        source_score.schema_version != 1
        or not source_score.has_stable_event_identity
        or source_score.identity_contract != "stable-event-v1"
    ):
        raise MigrationError(
            "source.explicit_v1_required",
            "score.schema_version",
            "upgrade legacy scores to schema_version 1 before migration",
        )
    try:
        validate_score_time_coordinates(source_score)
    except (ScoreTimeError, ValueError) as exc:
        raise MigrationError(
            "source.invalid_time_coordinates",
            "score",
            _bounded_diagnostic(exc),
        ) from exc

    raw_tempo = source_document["tempo_map"]
    raw_parts = source_document["parts"]
    assert isinstance(raw_tempo, list)
    assert isinstance(raw_parts, list)

    if len(source_score.parts) > MAX_PARTS:
        raise MigrationError(
            "target.part_count_exceeds_v2_limit",
            "score.parts",
            f"score v2 supports at most {MAX_PARTS} parts",
        )
    note_count = sum(len(part.notes) for part in source_score.parts)
    if note_count > MAX_NOTES:
        raise MigrationError(
            "target.note_count_exceeds_v2_limit",
            "score.parts",
            f"score v2 supports at most {MAX_NOTES} notes",
        )
    phrase_count = sum(len(part.phrases) for part in source_score.parts)
    if phrase_count > MAX_RELATIONS:
        raise MigrationError(
            "target.relation_count_exceeds_v2_limit",
            "score.parts",
            f"score v2 supports at most {MAX_RELATIONS} relations",
        )
    meter_event_count = sum(
        1
        for index, entry in enumerate(raw_tempo)
        if index == 0
        or "beats_per_bar" in entry
        or "beat_unit" in entry
    )
    tempo_event_count = sum(
        1
        for index, entry in enumerate(raw_tempo)
        if index == 0 or "bpm" in entry
    )
    if meter_event_count > MAX_METER_EVENTS:
        raise MigrationError(
            "target.meter_event_count_exceeds_v2_limit",
            "score.tempo_map",
            f"score v2 supports at most {MAX_METER_EVENTS} meter events",
        )
    if tempo_event_count > MAX_TEMPO_EVENTS:
        raise MigrationError(
            "target.tempo_event_count_exceeds_v2_limit",
            "score.tempo_map",
            f"score v2 supports at most {MAX_TEMPO_EVENTS} tempo events",
        )

    issues = _IssueCollector()
    implicit_defaults: dict[str, int] = {}

    def count_default(name: str, count: int | bool = 1) -> None:
        amount = int(count)
        if amount:
            implicit_defaults[name] = implicit_defaults.get(name, 0) + amount

    count_default("score.title", "title" not in source_document)
    count_default("score.sample_rate", "sample_rate" not in source_document)
    count_default("score.tail_seconds", "tail_seconds" not in source_document)
    tuning_for_defaults = source_document.get("tuning")
    if tuning_for_defaults is None:
        count_default(
            (
                "score.tuning(null/default)"
                if "tuning" in source_document
                else "score.tuning"
            )
        )
    else:
        assert isinstance(tuning_for_defaults, dict)
        count_default(
            "score.tuning.temperament",
            "temperament" not in tuning_for_defaults,
        )
        count_default(
            "score.tuning.a4_hz",
            "a4_hz" not in tuning_for_defaults,
        )
    count_default(
        "score.tempo_map[].bar",
        sum("bar" not in entry for entry in raw_tempo),
    )
    count_default(
        "score.tempo_map[].beat",
        sum("beat" not in entry for entry in raw_tempo),
    )
    for raw_part in raw_parts:
        assert isinstance(raw_part, dict)
        count_default("score.parts[].name", "name" not in raw_part)
        count_default(
            "score.parts[].default_dynamic",
            "default_dynamic" not in raw_part,
        )
        raw_notes_for_defaults = raw_part["notes"]
        assert isinstance(raw_notes_for_defaults, list)
        count_default(
            "score.parts[].notes[].bar",
            sum("bar" not in note for note in raw_notes_for_defaults),
        )
        count_default(
            "score.parts[].notes[].beat",
            sum("beat" not in note for note in raw_notes_for_defaults),
        )
        count_default(
            "score.parts[].notes[].duration_beats",
            sum(
                "duration_beats" not in note
                for note in raw_notes_for_defaults
            ),
        )
        raw_phrases_for_defaults_value = raw_part.get("phrases")
        raw_phrases_for_defaults = (
            []
            if raw_phrases_for_defaults_value is None
            else raw_phrases_for_defaults_value
        )
        assert isinstance(raw_phrases_for_defaults, list)
        count_default(
            "score.parts[].phrases[].start_beat",
            sum(
                "start_beat" not in phrase
                for phrase in raw_phrases_for_defaults
            ),
        )
        count_default(
            "score.parts[].phrases[].end_beat",
            sum(
                "end_beat" not in phrase
                for phrase in raw_phrases_for_defaults
            ),
        )
    if implicit_defaults:
        summary = ", ".join(
            f"{name}={count}"
            for name, count in sorted(implicit_defaults.items())
        )
        issues.append(
            MigrationIssue(
                code="source.implicit_defaults_materialized",
                severity="info",
                category="notation_default",
                location="score",
                message=(
                    "v1 implicit/defaulted values were materialized explicitly "
                    "in the "
                    f"separated v2 artifacts; counts: {summary}"
                ),
            )
        )

    # V1 admitted longer/coerced authoring strings than the v2 core.  Reject
    # those before materializing measures and target note dictionaries so a
    # tiny incompatible identifier cannot trigger large output amplification.
    _ensure_v2_text(source_score.title, location="score.title")
    for part_index, part in enumerate(source_score.parts):
        part_path = f"score.parts[{part_index}]"
        _ensure_v2_identifier(part.id, location=f"{part_path}.id")
        _ensure_v2_text(part.name, location=f"{part_path}.name")
        if part.default_articulation is not None:
            _ensure_v2_identifier(
                part.default_articulation,
                location=f"{part_path}.default_articulation",
            )
        for note in part.notes:
            note_path = f"{part_path}.notes[{note.index}]"
            assert note.source_event_id is not None
            _ensure_v2_identifier(
                note.source_event_id,
                location=f"{note_path}.event_id",
            )
            if note.articulation is not None:
                _ensure_v2_identifier(
                    note.articulation,
                    location=f"{note_path}.articulation",
                )
            if note.voice is not None:
                _ensure_v2_identifier(
                    note.voice,
                    location=f"{note_path}.voice",
                )
    maximum_referenced_bar = max(
        entry.bar for entry in source_score.tempo_map.entries
    )
    for part in source_score.parts:
        for note in part.notes:
            maximum_referenced_bar = max(maximum_referenced_bar, note.bar)
        for phrase in part.phrases:
            maximum_referenced_bar = max(
                maximum_referenced_bar,
                phrase.start_bar,
                phrase.end_bar,
            )
    if maximum_referenced_bar > MAX_MEASURES:
        raise MigrationError(
            "timeline.measure_count_exceeds_v2_limit",
            "score",
            f"migration requires at least {maximum_referenced_bar} measures",
        )

    # First materialize every referenced bar.  Notes may extend past their
    # starting bar; additional inherited-meter measures are appended below.
    measure_durations: list[Rational] = []
    measure_starts: list[Fraction] = [Fraction(0)]
    for bar in range(1, maximum_referenced_bar + 1):
        meter = source_score.tempo_map.meter_entry_at_bar(bar)
        duration = _rational_from_fraction(
            Fraction(meter.beats_per_bar * 4, meter.beat_unit),
            location=f"score.timeline.measures[{bar - 1}].actual_duration_quarters",
        )
        measure_durations.append(duration)
        measure_starts.append(measure_starts[-1] + duration.as_fraction())

    # Prepare exact note timing before target materialization.  The original
    # source index survives the v1 parser's stable musical sort and locates the
    # spelling/velocity in the bound source generation.
    prepared: list[list[tuple[Any, ...]]] = []
    maximum_note_end = Fraction(0)
    for part_index, part in enumerate(source_score.parts):
        part_prepared: list[tuple[Any, ...]] = []
        raw_part = raw_parts[part_index]
        assert isinstance(raw_part, dict)
        raw_notes = raw_part["notes"]
        assert isinstance(raw_notes, list)
        for note in part.notes:
            location = f"score.parts[{part_index}].notes[{note.index}]"
            raw_note = raw_notes[note.index]
            assert isinstance(raw_note, dict)
            meter = source_score.tempo_map.meter_entry_at_bar(note.bar)
            offset = _position_rational(
                bar=note.bar,
                beat=note.beat,
                beat_unit=meter.beat_unit,
                location=location,
            )
            duration = _duration_rational(
                duration_beats=note.duration_beats,
                beat_unit=meter.beat_unit,
                location=location,
            )
            sounding_source = (
                raw_note["pitch"]
                if type(raw_note.get("pitch")) in (int, float)
                else note.midi
            )
            sounding = _rational_from_number(
                sounding_source,
                location=f"{location}.pitch",
            )
            start_absolute = measure_starts[note.bar - 1] + offset.as_fraction()
            end_absolute = start_absolute + duration.as_fraction()
            maximum_note_end = max(maximum_note_end, end_absolute)
            legacy_start = source_score.tempo_map.quarter_at(note.bar, note.beat)
            legacy_duration = note.duration_beats * meter.quarters_per_beat
            part_prepared.append(
                (
                    note,
                    raw_note,
                    location,
                    offset,
                    duration,
                    sounding,
                    start_absolute,
                    end_absolute,
                    legacy_start,
                    legacy_duration,
                )
            )
        prepared.append(part_prepared)

    while measure_starts[-1] < maximum_note_end:
        next_bar = len(measure_durations) + 1
        if next_bar > MAX_MEASURES:
            raise MigrationError(
                "timeline.measure_count_exceeds_v2_limit",
                "score.parts",
                "note duration requires more score-v2 measures than supported",
            )
        meter = source_score.tempo_map.meter_entry_at_bar(next_bar)
        duration = _rational_from_fraction(
            Fraction(meter.beats_per_bar * 4, meter.beat_unit),
            location=(
                f"score.timeline.measures[{next_bar - 1}]"
                ".actual_duration_quarters"
            ),
        )
        measure_durations.append(duration)
        measure_starts.append(measure_starts[-1] + duration.as_fraction())

    measures = [
        {
            "measure_id": _measure_id(bar),
            "actual_duration_quarters": duration.to_dict(),
        }
        for bar, duration in enumerate(measure_durations, start=1)
    ]

    meter_events: list[dict[str, object]] = []
    tempo_events: list[dict[str, object]] = []
    meter_grouping_default_count = 0
    for source_index, (entry, raw_entry) in enumerate(
        zip(source_score.tempo_map.entries, raw_tempo, strict=True)
    ):
        assert isinstance(raw_entry, dict)
        location = f"score.tempo_map[{source_index}]"
        meter = source_score.tempo_map.meter_entry_at_bar(entry.bar)
        offset = _position_rational(
            bar=entry.bar,
            beat=entry.beat,
            beat_unit=meter.beat_unit,
            location=location,
        )
        if source_index == 0 or (
            "beats_per_bar" in raw_entry or "beat_unit" in raw_entry
        ):
            meter_events.append(
                {
                    "meter_id": f"meter-v1-{source_index + 1:06d}",
                    "at": _position_dict(_measure_id(entry.bar), offset),
                    "groups": [entry.beats_per_bar],
                    "beat_unit": entry.beat_unit,
                }
            )
            meter_grouping_default_count += 1
        if source_index == 0 or "bpm" in raw_entry:
            tempo_events.append(
                {
                    "tempo_id": f"tempo-v1-{source_index + 1:06d}",
                    "at": _position_dict(_measure_id(entry.bar), offset),
                    "quarter_bpm": _rational_from_number(
                        entry.bpm,
                        location=f"{location}.bpm",
                    ).to_dict(),
                }
            )

    if meter_grouping_default_count:
        issues.append(
            MigrationIssue(
                code="meter.additive_grouping_defaulted",
                severity="info",
                category="notation_default",
                location="score.tempo_map",
                message=(
                    "v1 stored only meter numerators; score v2 uses one "
                    f"group for each of {meter_grouping_default_count} "
                    "materialized meter events"
                ),
            )
        )

    tuning_document = source_document.get("tuning")
    assert tuning_document is None or isinstance(tuning_document, dict)
    a4_source: object = 440.0
    if tuning_document is not None:
        a4_source = tuning_document.get("a4_hz", 440.0)
    tuning = {
        "tuning_id": "tuning-v1-concert-a",
        "system": "equal_temperament",
        "divisions_per_octave": 12,
        "reference_midi_note": Rational(69).to_dict(),
        "reference_frequency_hz": _rational_from_number(
            float(a4_source),
            location="score.tuning.a4_hz",
        ).to_dict(),
    }

    target_parts: list[dict[str, object]] = []
    migrated_by_part: list[tuple[_MigratedNote, ...]] = []
    performance_fact_values: list[tuple[str, str, Rational]] = []
    for part_index, (part, part_prepared) in enumerate(
        zip(source_score.parts, prepared, strict=True)
    ):
        target_notes: list[dict[str, object]] = []
        migrated_notes: list[_MigratedNote] = []
        for prepared_note in part_prepared:
            (
                note,
                raw_note,
                location,
                offset,
                duration,
                sounding,
                start_absolute,
                end_absolute,
                legacy_start,
                legacy_duration,
            ) = prepared_note
            assert note.source_event_id is not None
            raw_pitch = raw_note["pitch"]
            if type(raw_pitch) is str:
                written_pitch = _named_written_pitch(
                    raw_pitch,
                    sounding,
                    location=f"{location}.pitch",
                )
            else:
                written_pitch = _derived_written_pitch(sounding)
                issues.append(
                    MigrationIssue(
                        code="pitch.written_spelling_derived",
                        severity="info",
                        category="derived_notation",
                        location=f"{location}.pitch",
                        message=(
                            "numeric v1 pitch had no written spelling; a "
                            "deterministic sharp-biased spelling was derived"
                        ),
                    )
                )
            target_note: dict[str, object] = {
                "event_id": note.source_event_id,
                "position": _position_dict(_measure_id(note.bar), offset),
                "duration_quarters": duration.to_dict(),
                "written_pitch": written_pitch,
                "sounding_pitch": {"midi_note": sounding.to_dict()},
            }
            if note.dynamic is not None:
                target_note["dynamic"] = note.dynamic
            if note.articulation is not None:
                target_note["articulations"] = [note.articulation]
            if note.staff is not None:
                target_note["staff"] = note.staff
            if note.voice is not None:
                target_note["voice"] = note.voice
            target_notes.append(target_note)

            if note.velocity is not None:
                performance_fact_values.append(
                    (
                        part.id,
                        note.source_event_id,
                        _rational_from_number(
                            note.velocity,
                            location=f"{location}.velocity",
                        ),
                    )
                )
            migrated_notes.append(
                _MigratedNote(
                    event_id=note.source_event_id,
                    source_location=location,
                    start_absolute=start_absolute,
                    end_absolute=end_absolute,
                    legacy_start=legacy_start,
                    legacy_duration=legacy_duration,
                    sounding=sounding,
                    staff=note.staff,
                    voice=note.voice,
                    starts_tie=note.tie,
                )
            )

        target_part: dict[str, object] = {
            "part_id": part.id,
            "name": part.name,
            "default_dynamic": part.default_dynamic,
            "notes": target_notes,
        }
        if part.default_articulation is not None:
            target_part["default_articulation"] = part.default_articulation
        target_parts.append(target_part)
        migrated_by_part.append(tuple(migrated_notes))

    ties = _tie_documents(tuple(migrated_by_part), issues)

    phrases: list[dict[str, object]] = []
    phrase_number = 0
    for part_index, part in enumerate(source_score.parts):
        for phrase_index, phrase in enumerate(part.phrases):
            phrase_number += 1
            location = f"score.parts[{part_index}].phrases[{phrase_index}]"
            start_meter = source_score.tempo_map.meter_entry_at_bar(
                phrase.start_bar
            )
            end_meter = source_score.tempo_map.meter_entry_at_bar(phrase.end_bar)
            start_offset = _position_rational(
                bar=phrase.start_bar,
                beat=phrase.start_beat,
                beat_unit=start_meter.beat_unit,
                location=f"{location}.start",
            )
            end_offset = _position_rational(
                bar=phrase.end_bar,
                beat=phrase.end_beat,
                beat_unit=end_meter.beat_unit,
                location=f"{location}.end",
            )
            start_absolute = (
                measure_starts[phrase.start_bar - 1]
                + start_offset.as_fraction()
            )
            end_absolute = (
                measure_starts[phrase.end_bar - 1]
                + end_offset.as_fraction()
            )
            if end_absolute <= start_absolute:
                raise MigrationError(
                    "phrase.nonpositive_extent_not_supported",
                    location,
                    "score v2 phrases must end strictly after they start",
                )
            phrases.append(
                {
                    "phrase_id": f"phrase-v1-{phrase_number:06d}",
                    "part_id": part.id,
                    "start": _position_dict(
                        _measure_id(phrase.start_bar),
                        start_offset,
                    ),
                    "end": _position_dict(
                        _measure_id(phrase.end_bar),
                        end_offset,
                    ),
                }
            )

    if len(ties) + len(phrases) > MAX_RELATIONS:
        raise MigrationError(
            "target.relation_count_exceeds_v2_limit",
            "score",
            f"score v2 supports at most {MAX_RELATIONS} ties plus phrases",
        )

    target_document: dict[str, Any] = {
        "kind": "tianlai.score",
        "schema_version": 2,
        "title": source_score.title,
        "timeline": {
            "measures": measures,
            "meter_events": meter_events,
            "tempo_events": tempo_events,
        },
        "tuning": tuning,
        "parts": target_parts,
        "ties": ties,
        "phrases": phrases,
        "form": {"mode": "linear"},
    }
    try:
        target_score = parse_score_v2_document(target_document)
    except (TypeError, ValueError) as exc:
        raise MigrationError(
            "target.score_v2_validation_failed",
            "score",
            _bounded_diagnostic(exc),
        ) from exc

    target_document_sha256 = _score_document_sha256(target_score)
    target_projection_sha256 = score_render_projection_sha256(target_score)
    render_settings = MigratedRenderSettings(
        sample_rate=source_score.sample_rate,
        tail_seconds=_rational_from_number(
            source_score.tail_seconds,
            location="score.tail_seconds",
        ),
    )
    performance_facts = MigratedPerformanceFacts(
        score_document_sha256=target_document_sha256,
        events=tuple(
            EventPerformanceFact(
                part_id=part_id,
                event_id=event_id,
                velocity=velocity,
            )
            for part_id, event_id, velocity in performance_fact_values
        ),
    )
    receipt = ScoreV2MigrationReceipt(
        source_document_sha256=hashlib.sha256(source_canonical).hexdigest(),
        target_document_sha256=target_document_sha256,
        target_render_projection_sha256=target_projection_sha256,
        render_settings_sha256=_canonical_document_sha256(
            render_settings.to_dict()
        ),
        performance_facts_sha256=_canonical_document_sha256(
            performance_facts.to_dict()
        ),
        issues=issues.freeze(),
    )
    return ScoreV2Migration(
        score=target_score,
        render_settings=render_settings,
        performance_facts=performance_facts,
        receipt=receipt,
    )


def migrate_score_v1_to_v2(
    document: dict[str, Any],
    *,
    limits: ProjectLimits | None = None,
) -> ScoreV2Migration:
    """Snapshot and migrate one in-memory score-v1 JSON document."""

    try:
        snapshot = snapshot_score_document(document, limits=limits)
    except (TypeError, ValueError) as exc:
        raise MigrationError(
            "source.invalid_score",
            "score",
            _bounded_diagnostic(exc),
        ) from exc
    return migrate_score_v1_snapshot(snapshot)


def migrate_v1_score_to_v2(
    document: dict[str, Any],
    *,
    limits: ProjectLimits | None = None,
) -> ScoreV2Migration:
    """Compatibility spelling for :func:`migrate_score_v1_to_v2`."""

    return migrate_score_v1_to_v2(document, limits=limits)


__all__ = [
    "EventPerformanceFact",
    "MIGRATION_RECEIPT_DOMAIN",
    "MIGRATION_SCHEMA_VERSION",
    "MigratedPerformanceFacts",
    "MigratedRenderSettings",
    "MigrationError",
    "MigrationIssue",
    "ScoreV2Migration",
    "ScoreV2MigrationReceipt",
    "migrate_score_v1_snapshot",
    "migrate_score_v1_to_v2",
    "migrate_v1_score_to_v2",
    "parse_score_v2_migration_document",
    "score_v2_migration_json_bytes",
    "verify_score_v2_migration_document",
]
