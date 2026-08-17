"""Sealed creator-consent policy for future Score-v2 execution.

This module is intentionally only a data boundary.  A profile records which
sample-grid, value, and semantic adaptations a creator has authorized; it
does not choose instruments, claim that a roster can honor the policy, or
grant render authority.

Both entry points capture one bounded JSON generation before semantic
validation.  The returned object contains tuple-backed values and retains a
canonical JSON identity, so caller-owned containers and deliberately bypassed
``frozen=True`` attributes cannot silently change the artifact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, NamedTuple

from .authoring_json import (
    AuthoringJsonError,
    AuthoringJsonLimits,
    bounded_canonical_json_bytes,
    strict_json_loads,
)
from .canonical_json import canonical_json_bytes
from .score_v2 import Rational


SCORE_V2_EXECUTION_PROFILE_KIND = "tianlai.score_v2_execution_profile"
SCORE_V2_EXECUTION_PROFILE_SCHEMA_VERSION = 1

DEFAULT_MAX_EXECUTION_PROFILE_JSON_BYTES = 1024 * 1024
HARD_MAX_EXECUTION_PROFILE_JSON_BYTES = 4 * 1024 * 1024

SAMPLE_TIME_POLICIES = frozenset(("exact", "adapt"))
VALUE_POLICIES = frozenset(("exact", "adapt"))
SEMANTIC_POLICIES = frozenset(("exact", "approximate"))
PITCH_RANGE_POLICIES = frozenset(
    ("declared_hard", "verified_high_quality")
)
ARTICULATION_MAPPING_POLICIES = frozenset(
    ("direct_only", "allow_roster_mapping")
)
PHRASE_POLICIES = frozenset(("reject",))

_DYNAMIC_MARK_ORDER = ("ppp", "pp", "p", "mp", "mf", "f", "ff", "fff")
_DYNAMIC_MARKS = frozenset(_DYNAMIC_MARK_ORDER)


class ScoreV2ExecutionProfileError(ValueError):
    """A stable, non-reflective execution-profile boundary failure."""

    def __init__(
        self,
        code: str,
        *,
        actual: int | None = None,
        limit: int | None = None,
    ) -> None:
        self.code = code
        self.message_key = f"scoreV2ExecutionProfile.{code.replace('.', '_')}"
        self.actual = actual
        self.limit = limit
        # Do not include input values, object keys, JSON snippets, or parser
        # exception messages in the human-readable exception boundary.
        super().__init__(code)


class ScoreV2ProfileRational(NamedTuple):
    """An immutable, normalized projection of :class:`score_v2.Rational`."""

    numerator: int
    denominator: int

    def to_dict(self) -> dict[str, int]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
        }

    def as_rational(self) -> Rational:
        return Rational(self.numerator, self.denominator)


class ScoreV2DynamicLevel(NamedTuple):
    mark: str
    value: ScoreV2ProfileRational

    def to_dict(self) -> dict[str, int]:
        return self.value.to_dict()


class ScoreV2NoteVelocityPolicy(NamedTuple):
    value_policy: str
    semantic_policy: str

    def to_dict(self) -> dict[str, str]:
        return {
            "value_policy": self.value_policy,
            "semantic_policy": self.semantic_policy,
        }


class ScoreV2TuningPolicy(NamedTuple):
    """Consent for adapting the score-wide tuning reference and semantics."""

    value_policy: str
    semantic_policy: str

    def to_dict(self) -> dict[str, str]:
        return {
            "value_policy": self.value_policy,
            "semantic_policy": self.semantic_policy,
        }


class ScoreV2PitchPolicy(NamedTuple):
    value_policy: str
    semantic_policy: str
    range_policy: str

    def to_dict(self) -> dict[str, str]:
        return {
            "value_policy": self.value_policy,
            "semantic_policy": self.semantic_policy,
            "range_policy": self.range_policy,
        }


class ScoreV2ArticulationPolicy(NamedTuple):
    mapping_policy: str
    semantic_policy: str

    def to_dict(self) -> dict[str, str]:
        return {
            "mapping_policy": self.mapping_policy,
            "semantic_policy": self.semantic_policy,
        }


def _document_from_values(
    *,
    kind: str,
    schema_version: int,
    sample_time_policy: str,
    dynamic_profile: tuple[ScoreV2DynamicLevel, ...],
    note_velocity: ScoreV2NoteVelocityPolicy,
    tuning: ScoreV2TuningPolicy,
    pitch: ScoreV2PitchPolicy,
    articulation: ScoreV2ArticulationPolicy,
    phrase_policy: str,
) -> dict[str, object]:
    return {
        "kind": kind,
        "schema_version": schema_version,
        "sample_time_policy": sample_time_policy,
        "dynamic_profile": {
            level.mark: level.value.to_dict() for level in dynamic_profile
        },
        "note_velocity": note_velocity.to_dict(),
        "tuning": tuning.to_dict(),
        "pitch": pitch.to_dict(),
        "articulation": articulation.to_dict(),
        "phrase_policy": phrase_policy,
    }


@dataclass(frozen=True, slots=True, init=False)
class ScoreV2ExecutionProfile:
    """One sealed, content-addressed Score-v2 execution-consent profile."""

    kind: str
    schema_version: int
    sample_time_policy: str
    dynamic_profile: tuple[ScoreV2DynamicLevel, ...]
    note_velocity: ScoreV2NoteVelocityPolicy
    tuning: ScoreV2TuningPolicy
    pitch: ScoreV2PitchPolicy
    articulation: ScoreV2ArticulationPolicy
    phrase_policy: str
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _identity_seal: tuple[object, ...] = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2ExecutionProfile cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "ScoreV2ExecutionProfile must be created by "
            "parse_score_v2_execution_profile"
        )

    def _trusted_artifact_bytes(self) -> bytes:
        try:
            identity_seal = self._identity_seal
        except AttributeError as exc:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            ) from exc
        if type(identity_seal) is not tuple or len(identity_seal) != 11:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            )
        try:
            (
                kind,
                schema_version,
                sample_time_policy,
                dynamic_profile,
                note_velocity,
                tuning,
                pitch,
                articulation,
                phrase_policy,
                canonical_bytes_value,
                artifact_sha256,
            ) = identity_seal
        except (TypeError, ValueError) as exc:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            ) from exc

        if (
            type(self.kind) is not str
            or self.kind != kind
            or type(self.schema_version) is not int
            or self.schema_version != schema_version
            or type(self.sample_time_policy) is not str
            or self.sample_time_policy != sample_time_policy
            or self.dynamic_profile is not dynamic_profile
            or self.note_velocity is not note_velocity
            or self.tuning is not tuning
            or self.pitch is not pitch
            or self.articulation is not articulation
            or type(self.phrase_policy) is not str
            or self.phrase_policy != phrase_policy
            or self._canonical_bytes is not canonical_bytes_value
            or type(canonical_bytes_value) is not bytes
            or type(self._artifact_sha256) is not str
            or type(artifact_sha256) is not str
            or self._artifact_sha256 != artifact_sha256
            or hashlib.sha256(canonical_bytes_value).hexdigest()
            != artifact_sha256
        ):
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            )

        try:
            reconstructed = _document_from_values(
                kind=self.kind,
                schema_version=self.schema_version,
                sample_time_policy=self.sample_time_policy,
                dynamic_profile=self.dynamic_profile,
                note_velocity=self.note_velocity,
                tuning=self.tuning,
                pitch=self.pitch,
                articulation=self.articulation,
                phrase_policy=self.phrase_policy,
            )
            matches_values = (
                canonical_json_bytes(reconstructed) == canonical_bytes_value
            )
            parsed_values = _parse_semantics(reconstructed)
            matches_semantics = parsed_values == (
                self.kind,
                self.schema_version,
                self.sample_time_policy,
                self.dynamic_profile,
                self.note_velocity,
                self.tuning,
                self.pitch,
                self.articulation,
                self.phrase_policy,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            ) from exc
        if not matches_values or not matches_semantics:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            )
        return canonical_bytes_value

    @property
    def canonical_bytes(self) -> bytes:
        return self._trusted_artifact_bytes()

    @property
    def canonical_json_bytes(self) -> bytes:
        """Alias that makes the serialized representation explicit."""

        return self._trusted_artifact_bytes()

    @property
    def canonical_json_bytes_size(self) -> int:
        return len(self._trusted_artifact_bytes())

    @property
    def artifact_sha256(self) -> str:
        self._trusted_artifact_bytes()
        return self._artifact_sha256

    def to_dict(self) -> dict[str, object]:
        try:
            value = json.loads(self._trusted_artifact_bytes())
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            ) from exc
        if type(value) is not dict:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.integrity_mismatch"
            )
        return value


def _active_json_limits(max_document_bytes: object) -> AuthoringJsonLimits:
    if (
        type(max_document_bytes) is not int
        or max_document_bytes < 1
        or max_document_bytes > HARD_MAX_EXECUTION_PROFILE_JSON_BYTES
    ):
        raise ScoreV2ExecutionProfileError(
            "execution_profile.invalid_resource_limit",
            limit=HARD_MAX_EXECUTION_PROFILE_JSON_BYTES,
        )
    return AuthoringJsonLimits(
        max_document_bytes=max_document_bytes,
        max_depth=16,
        max_nodes=256,
        max_string_bytes=min(max_document_bytes, 4096),
        max_array_items=16,
        max_object_members=64,
    )


def _translate_json_error(
    error: AuthoringJsonError,
) -> ScoreV2ExecutionProfileError:
    return ScoreV2ExecutionProfileError(
        f"execution_profile.json.{error.code}",
        actual=error.actual,
        limit=error.limit,
    )


def _capture_document(
    data: object,
    *,
    limits: AuthoringJsonLimits,
) -> dict[str, Any]:
    if type(data) is dict:
        try:
            payload = bounded_canonical_json_bytes(
                data,
                limits=limits,
                require_object=True,
                require_js_safe_integers=True,
            )
        except AuthoringJsonError as exc:
            raise _translate_json_error(exc) from exc
        except (
            IndexError,
            KeyError,
            RecursionError,
            RuntimeError,
            TypeError,
            UnicodeError,
            ValueError,
        ) as exc:
            # A caller may mutate an exact built-in container concurrently
            # with the preliminary traversal.  Never leak that race's parser
            # or container exception; a later retry can capture a new whole
            # generation.
            raise ScoreV2ExecutionProfileError(
                "execution_profile.capture_failed"
            ) from exc
    elif type(data) is bytes:
        # Perform the byte gate here, before UTF-8 decoding or JSON object
        # materialization.  strict_json_loads independently enforces it too.
        if len(data) > limits.max_document_bytes:
            raise ScoreV2ExecutionProfileError(
                "execution_profile.json.document_too_large",
                actual=len(data),
                limit=limits.max_document_bytes,
            )
        payload = data
    else:
        raise ScoreV2ExecutionProfileError(
            "execution_profile.input_must_be_plain_dict_or_bytes"
        )

    if type(payload) is not bytes:
        raise ScoreV2ExecutionProfileError(
            "execution_profile.capture_failed"
        )
    try:
        detached = strict_json_loads(
            payload,
            limits=limits,
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise _translate_json_error(exc) from exc
    if type(detached) is not dict:
        raise ScoreV2ExecutionProfileError(
            "execution_profile.top_level_object_required"
        )
    return detached


def _require_exact_members(
    value: object,
    expected: frozenset[str],
    *,
    code: str,
) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != expected:
        raise ScoreV2ExecutionProfileError(code)
    return value


def _require_enum(value: object, allowed: frozenset[str], *, code: str) -> str:
    if type(value) is not str or value not in allowed:
        raise ScoreV2ExecutionProfileError(code)
    return value


def _integer(value: object, *, code: str) -> int:
    """Follow Draft 2020-12's mathematical-integer JSON semantics."""

    if type(value) is int:
        return value
    if type(value) is float and value.is_integer():
        return int(value)
    raise ScoreV2ExecutionProfileError(code)


def _parse_dynamic_value(value: object) -> ScoreV2ProfileRational:
    document = _require_exact_members(
        value,
        frozenset(("numerator", "denominator")),
        code="execution_profile.invalid_dynamic_value",
    )
    numerator = _integer(
        document["numerator"],
        code="execution_profile.invalid_dynamic_value",
    )
    denominator = _integer(
        document["denominator"],
        code="execution_profile.invalid_dynamic_value",
    )
    try:
        normalized = Rational(numerator, denominator)
    except (TypeError, ValueError) as exc:
        raise ScoreV2ExecutionProfileError(
            "execution_profile.invalid_dynamic_value"
        ) from exc
    if normalized.numerator <= 0 or normalized.numerator > normalized.denominator:
        raise ScoreV2ExecutionProfileError(
            "execution_profile.invalid_dynamic_value"
        )
    return ScoreV2ProfileRational(
        normalized.numerator,
        normalized.denominator,
    )


def _parse_dynamic_profile(value: object) -> tuple[ScoreV2DynamicLevel, ...]:
    if type(value) is not dict or not value:
        raise ScoreV2ExecutionProfileError(
            "execution_profile.invalid_dynamic_profile"
        )
    if len(value) > len(_DYNAMIC_MARKS):
        raise ScoreV2ExecutionProfileError(
            "execution_profile.invalid_dynamic_profile"
        )
    if any(type(mark) is not str or mark not in _DYNAMIC_MARKS for mark in value):
        raise ScoreV2ExecutionProfileError(
            "execution_profile.invalid_dynamic_profile"
        )
    return tuple(
        ScoreV2DynamicLevel(mark, _parse_dynamic_value(value[mark]))
        for mark in _DYNAMIC_MARK_ORDER
        if mark in value
    )


def _parse_note_velocity(value: object) -> ScoreV2NoteVelocityPolicy:
    document = _require_exact_members(
        value,
        frozenset(("value_policy", "semantic_policy")),
        code="execution_profile.invalid_note_velocity",
    )
    return ScoreV2NoteVelocityPolicy(
        _require_enum(
            document["value_policy"],
            VALUE_POLICIES,
            code="execution_profile.invalid_note_velocity",
        ),
        _require_enum(
            document["semantic_policy"],
            SEMANTIC_POLICIES,
            code="execution_profile.invalid_note_velocity",
        ),
    )


def _parse_tuning(value: object) -> ScoreV2TuningPolicy:
    document = _require_exact_members(
        value,
        frozenset(("value_policy", "semantic_policy")),
        code="execution_profile.invalid_tuning",
    )
    return ScoreV2TuningPolicy(
        _require_enum(
            document["value_policy"],
            VALUE_POLICIES,
            code="execution_profile.invalid_tuning",
        ),
        _require_enum(
            document["semantic_policy"],
            SEMANTIC_POLICIES,
            code="execution_profile.invalid_tuning",
        ),
    )


def _parse_pitch(value: object) -> ScoreV2PitchPolicy:
    document = _require_exact_members(
        value,
        frozenset(("value_policy", "semantic_policy", "range_policy")),
        code="execution_profile.invalid_pitch",
    )
    return ScoreV2PitchPolicy(
        _require_enum(
            document["value_policy"],
            VALUE_POLICIES,
            code="execution_profile.invalid_pitch",
        ),
        _require_enum(
            document["semantic_policy"],
            SEMANTIC_POLICIES,
            code="execution_profile.invalid_pitch",
        ),
        _require_enum(
            document["range_policy"],
            PITCH_RANGE_POLICIES,
            code="execution_profile.invalid_pitch",
        ),
    )


def _parse_articulation(value: object) -> ScoreV2ArticulationPolicy:
    document = _require_exact_members(
        value,
        frozenset(("mapping_policy", "semantic_policy")),
        code="execution_profile.invalid_articulation",
    )
    return ScoreV2ArticulationPolicy(
        _require_enum(
            document["mapping_policy"],
            ARTICULATION_MAPPING_POLICIES,
            code="execution_profile.invalid_articulation",
        ),
        _require_enum(
            document["semantic_policy"],
            SEMANTIC_POLICIES,
            code="execution_profile.invalid_articulation",
        ),
    )


def _parse_semantics(document: dict[str, Any]) -> tuple[object, ...]:
    root = _require_exact_members(
        document,
        frozenset(
            (
                "kind",
                "schema_version",
                "sample_time_policy",
                "dynamic_profile",
                "note_velocity",
                "tuning",
                "pitch",
                "articulation",
                "phrase_policy",
            )
        ),
        code="execution_profile.invalid_document_shape",
    )
    if root["kind"] != SCORE_V2_EXECUTION_PROFILE_KIND:
        raise ScoreV2ExecutionProfileError("execution_profile.invalid_kind")
    schema_version = _integer(
        root["schema_version"],
        code="execution_profile.unsupported_schema_version",
    )
    if schema_version != SCORE_V2_EXECUTION_PROFILE_SCHEMA_VERSION:
        raise ScoreV2ExecutionProfileError(
            "execution_profile.unsupported_schema_version"
        )
    return (
        SCORE_V2_EXECUTION_PROFILE_KIND,
        schema_version,
        _require_enum(
            root["sample_time_policy"],
            SAMPLE_TIME_POLICIES,
            code="execution_profile.invalid_sample_time_policy",
        ),
        _parse_dynamic_profile(root["dynamic_profile"]),
        _parse_note_velocity(root["note_velocity"]),
        _parse_tuning(root["tuning"]),
        _parse_pitch(root["pitch"]),
        _parse_articulation(root["articulation"]),
        _require_enum(
            root["phrase_policy"],
            PHRASE_POLICIES,
            code="execution_profile.invalid_phrase_policy",
        ),
    )


def parse_score_v2_execution_profile(
    data: dict[str, Any] | bytes,
    *,
    max_document_bytes: int = DEFAULT_MAX_EXECUTION_PROFILE_JSON_BYTES,
) -> ScoreV2ExecutionProfile:
    """Capture, validate, normalize, and seal one consent profile.

    ``max_document_bytes`` may be deliberately raised for a trusted transport,
    but never beyond :data:`HARD_MAX_EXECUTION_PROFILE_JSON_BYTES`.
    """

    limits = _active_json_limits(max_document_bytes)
    detached = _capture_document(data, limits=limits)
    (
        kind,
        schema_version,
        sample_time_policy,
        dynamic_profile,
        note_velocity,
        tuning,
        pitch,
        articulation,
        phrase_policy,
    ) = _parse_semantics(detached)

    assert type(kind) is str
    assert type(schema_version) is int
    assert type(sample_time_policy) is str
    assert type(dynamic_profile) is tuple
    assert type(note_velocity) is ScoreV2NoteVelocityPolicy
    assert type(tuning) is ScoreV2TuningPolicy
    assert type(pitch) is ScoreV2PitchPolicy
    assert type(articulation) is ScoreV2ArticulationPolicy
    assert type(phrase_policy) is str

    normalized_document = _document_from_values(
        kind=kind,
        schema_version=schema_version,
        sample_time_policy=sample_time_policy,
        dynamic_profile=dynamic_profile,
        note_velocity=note_velocity,
        tuning=tuning,
        pitch=pitch,
        articulation=articulation,
        phrase_policy=phrase_policy,
    )
    try:
        artifact_bytes = bounded_canonical_json_bytes(
            normalized_document,
            limits=limits,
            require_object=True,
            require_js_safe_integers=True,
        )
    except AuthoringJsonError as exc:
        raise _translate_json_error(exc) from exc
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()

    profile = object.__new__(ScoreV2ExecutionProfile)
    object.__setattr__(profile, "kind", kind)
    object.__setattr__(profile, "schema_version", schema_version)
    object.__setattr__(profile, "sample_time_policy", sample_time_policy)
    object.__setattr__(profile, "dynamic_profile", dynamic_profile)
    object.__setattr__(profile, "note_velocity", note_velocity)
    object.__setattr__(profile, "tuning", tuning)
    object.__setattr__(profile, "pitch", pitch)
    object.__setattr__(profile, "articulation", articulation)
    object.__setattr__(profile, "phrase_policy", phrase_policy)
    object.__setattr__(profile, "_canonical_bytes", artifact_bytes)
    object.__setattr__(profile, "_artifact_sha256", artifact_sha256)
    object.__setattr__(
        profile,
        "_identity_seal",
        (
            kind,
            schema_version,
            sample_time_policy,
            dynamic_profile,
            note_velocity,
            tuning,
            pitch,
            articulation,
            phrase_policy,
            artifact_bytes,
            artifact_sha256,
        ),
    )
    # Exercise the complete integrity path before returning the artifact.
    profile._trusted_artifact_bytes()
    return profile


__all__ = [
    "ARTICULATION_MAPPING_POLICIES",
    "DEFAULT_MAX_EXECUTION_PROFILE_JSON_BYTES",
    "HARD_MAX_EXECUTION_PROFILE_JSON_BYTES",
    "PHRASE_POLICIES",
    "PITCH_RANGE_POLICIES",
    "SAMPLE_TIME_POLICIES",
    "SCORE_V2_EXECUTION_PROFILE_KIND",
    "SCORE_V2_EXECUTION_PROFILE_SCHEMA_VERSION",
    "SEMANTIC_POLICIES",
    "ScoreV2ArticulationPolicy",
    "ScoreV2DynamicLevel",
    "ScoreV2ExecutionProfile",
    "ScoreV2ExecutionProfileError",
    "ScoreV2NoteVelocityPolicy",
    "ScoreV2PitchPolicy",
    "ScoreV2ProfileRational",
    "ScoreV2TuningPolicy",
    "VALUE_POLICIES",
    "parse_score_v2_execution_profile",
]
