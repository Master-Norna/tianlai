"""A sealed, exact Score-v2 performance-intent plan foundation.

This module deliberately stops before instrument routing and rendering.  It
turns a trusted Score-v2 source generation into deterministic note
occurrences whose musical time remains exact and whose sample-grid adaptation
is explicitly authorized.  The legacy float conductor is never involved.

The resulting artifact is *not* render authority.  A later adapter must still
bind a roster, executor capabilities, pitch/velocity/articulation fidelity,
and the creator's semantic-approximation consent before it may emit renderer
events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
from typing import Any, Literal, NamedTuple

from .canonical_json import canonical_json_bytes, canonical_json_sha256
from .resource_limits import (
    ProjectLimits,
    ResourceLimitError,
)
from .score_source import ScoreSourceSnapshot, snapshot_score_bytes
from .score_v2 import (
    Rational,
    SCORE_V2_IDENTITY_CONTRACT,
    SCORE_V2_TIME_CONTRACT,
    ScoreNoteV2,
    ScorePartV2,
    ScoreV2Document,
    score_render_projection_sha256,
)
from .score_v2_time import (
    DEFAULT_MAX_TIME_INDEX_JSON_BYTES,
    ExactFraction,
    ExactTimePoint,
    SAMPLE_ROUNDING_MODE,
    SampleResolution,
    ScoreV2TimeIndex,
    ScoreV2TimeLimits,
    compile_score_v2_time,
)


SCORE_V2_PLAN_KIND = "tianlai.score_v2_plan"
SCORE_V2_PLAN_SCHEMA_VERSION = 1
SCORE_V2_PLAN_CONTRACT = "score-v2-plan-foundation-not-render-authority"
SAMPLE_TIME_POLICIES = frozenset(("exact", "adapt"))
MAX_V2_PLAN_SECONDS = 2 * 60 * 60
MAX_V2_PLAN_SAMPLE_INDEX = MAX_V2_PLAN_SECONDS * 384_000
_DYNAMIC_MARKS = frozenset(("ppp", "pp", "p", "mp", "mf", "f", "ff", "fff"))


def _is_sha256(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


class ScoreV2PlanError(ValueError):
    """A stable fail-closed diagnostic from Score-v2 plan compilation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def _fraction_dict(value: Fraction | ExactFraction) -> dict[str, str]:
    return {
        "numerator": str(value.numerator),
        "denominator": str(value.denominator),
    }


class ScoreV2EndpointEvidence(NamedTuple):
    requested_seconds: ExactFraction
    requested_sample: ExactFraction
    resolved_sample: int
    resolved_seconds: ExactFraction
    error_seconds: ExactFraction
    fidelity: Literal["exact", "rounded"]
    rounding_mode: str

    @classmethod
    def from_resolution(
        cls,
        resolution: SampleResolution,
    ) -> "ScoreV2EndpointEvidence":
        if type(resolution) is not SampleResolution:
            raise TypeError("endpoint resolution must be SampleResolution")
        return cls(
            requested_seconds=resolution.requested_seconds,
            requested_sample=resolution.requested_sample,
            resolved_sample=resolution.resolved_sample,
            resolved_seconds=resolution.resolved_seconds,
            error_seconds=resolution.error_seconds,
            fidelity=resolution.fidelity,
            rounding_mode=resolution.rounding_mode,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_seconds": _fraction_dict(self.requested_seconds),
            "requested_sample": _fraction_dict(self.requested_sample),
            "resolved_sample": self.resolved_sample,
            "resolved_seconds": _fraction_dict(self.resolved_seconds),
            "error_seconds": _fraction_dict(self.error_seconds),
            "fidelity": self.fidelity,
            "rounding_mode": self.rounding_mode,
        }


class ScoreV2WrittenPitchEvidence(NamedTuple):
    step: str
    alter: ExactFraction
    octave: int
    accidental: str | None

    @classmethod
    def from_note(cls, note: ScoreNoteV2) -> "ScoreV2WrittenPitchEvidence":
        pitch = note.written_pitch
        return cls(
            step=pitch.step,
            alter=ExactFraction(pitch.alter.as_fraction()),
            octave=pitch.octave,
            accidental=pitch.accidental,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "step": self.step,
            "alter": _fraction_dict(self.alter),
            "octave": self.octave,
        }
        if self.accidental is not None:
            result["accidental"] = self.accidental
        return result


class ScoreV2SourceNoteEvidence(NamedTuple):
    event_id: str
    written_pitch: ScoreV2WrittenPitchEvidence
    staff: int | None
    voice: str | None

    @classmethod
    def from_note(cls, note: ScoreNoteV2) -> "ScoreV2SourceNoteEvidence":
        return cls(
            event_id=note.event_id,
            written_pitch=ScoreV2WrittenPitchEvidence.from_note(note),
            staff=note.staff,
            voice=note.voice,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "event_id": self.event_id,
            "written_pitch": self.written_pitch.to_dict(),
        }
        if self.staff is not None:
            result["staff"] = self.staff
        if self.voice is not None:
            result["voice"] = self.voice
        return result


class ScoreV2Occurrence(NamedTuple):
    occurrence_id: str
    part_id: str
    source_event_ids: tuple[str, ...]
    source_tie_ids: tuple[str, ...]
    source_order: int
    start: ScoreV2EndpointEvidence
    end: ScoreV2EndpointEvidence
    sounding_midi_note: ExactFraction
    source_notes: tuple[ScoreV2SourceNoteEvidence, ...]
    dynamic: str
    velocity: ExactFraction
    articulation: str | None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "occurrence_id": self.occurrence_id,
            "part_id": self.part_id,
            "source_event_ids": list(self.source_event_ids),
            "source_tie_ids": list(self.source_tie_ids),
            "source_order": self.source_order,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "sounding_pitch": {
                "midi_note": _fraction_dict(self.sounding_midi_note),
            },
            "source_notes": [item.to_dict() for item in self.source_notes],
            "dynamic": self.dynamic,
            "velocity": _fraction_dict(self.velocity),
        }
        if self.articulation is not None:
            result["articulation"] = self.articulation
        return result


@dataclass(frozen=True, slots=True, init=False)
class ScoreV2Plan:
    """One sealed, JSON-addressed Score-v2 plan foundation."""

    source_document_sha256: str
    score_render_projection_sha256: str
    time_index_sha256: str
    dynamic_profile_sha256: str
    sample_rate: int
    sample_time_policy: str
    score_duration: ExactTimePoint
    occurrences: tuple[ScoreV2Occurrence, ...]
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _identity_seal: tuple[object, ...] = field(repr=False, compare=False)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("ScoreV2Plan cannot be subclassed")

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("ScoreV2Plan must be created by compile_score_v2_plan")

    def _trusted_artifact_bytes(self) -> bytes:
        try:
            (
                source_hash,
                projection_hash,
                time_hash,
                profile_hash,
                sample_rate,
                sample_policy,
                score_duration,
                occurrences,
                canonical_bytes,
                artifact_hash,
            ) = self._identity_seal
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2PlanError(
                "plan.integrity_mismatch",
                "plan state no longer matches its identity seal",
            ) from exc
        if (
            not _is_sha256(source_hash)
            or not _is_sha256(projection_hash)
            or not _is_sha256(time_hash)
            or not _is_sha256(profile_hash)
            or type(sample_rate) is not int
            or sample_rate < 1
            or type(sample_policy) is not str
            or sample_policy not in SAMPLE_TIME_POLICIES
            or type(score_duration) is not ExactTimePoint
            or type(occurrences) is not tuple
            or any(type(item) is not ScoreV2Occurrence for item in occurrences)
            or not _is_sha256(self.source_document_sha256)
            or not _is_sha256(self.score_render_projection_sha256)
            or not _is_sha256(self.time_index_sha256)
            or not _is_sha256(self.dynamic_profile_sha256)
            or type(self.sample_rate) is not int
            or type(self.sample_time_policy) is not str
            or self.source_document_sha256 != source_hash
            or self.score_render_projection_sha256 != projection_hash
            or self.time_index_sha256 != time_hash
            or self.dynamic_profile_sha256 != profile_hash
            or self.sample_rate != sample_rate
            or self.sample_time_policy != sample_policy
            or self.score_duration is not score_duration
            or self.occurrences is not occurrences
            or self._canonical_bytes is not canonical_bytes
            or self._artifact_sha256 != artifact_hash
            or type(canonical_bytes) is not bytes
            or not _is_sha256(self._artifact_sha256)
            or not _is_sha256(artifact_hash)
            or hashlib.sha256(canonical_bytes).hexdigest() != artifact_hash
        ):
            raise ScoreV2PlanError(
                "plan.integrity_mismatch",
                "plan state no longer matches its identity seal",
            )
        try:
            document = json.loads(canonical_bytes)
            bindings = document.get("bindings")
            raw_occurrences = document.get("occurrences")
            reconstructed_occurrences = [
                occurrence.to_dict() for occurrence in occurrences
            ]
            reconstructed_duration = score_duration.to_dict()
        except (
            AttributeError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise ScoreV2PlanError(
                "plan.integrity_mismatch",
                "sealed plan artifact does not match its typed generation",
            ) from exc
        if (
            type(document) is not dict
            or set(document)
            != {
                "kind",
                "schema_version",
                "contract",
                "bindings",
                "sample_rate",
                "sample_time_policy",
                "sample_time_policy_scope",
                "sample_rounding_mode",
                "occurrence_order",
                "dynamic_profile",
                "time_index_canonical_json_bytes",
                "score_duration",
                "occurrence_count",
                "occurrences",
            }
            or document.get("kind") != SCORE_V2_PLAN_KIND
            or type(document.get("schema_version")) is not int
            or document.get("schema_version")
            != SCORE_V2_PLAN_SCHEMA_VERSION
            or document.get("contract") != SCORE_V2_PLAN_CONTRACT
            or type(bindings) is not dict
            or set(bindings)
            != {
                "source_document_sha256",
                "score_render_projection_sha256",
                "time_index_sha256",
                "dynamic_profile_sha256",
            }
            or bindings.get("source_document_sha256") != source_hash
            or bindings.get("score_render_projection_sha256")
            != projection_hash
            or bindings.get("time_index_sha256") != time_hash
            or bindings.get("dynamic_profile_sha256") != profile_hash
            or document.get("sample_rate") != sample_rate
            or document.get("sample_time_policy") != sample_policy
            or document.get("sample_time_policy_scope")
            != "occurrence_endpoints"
            or document.get("sample_rounding_mode") != SAMPLE_ROUNDING_MODE
            or document.get("occurrence_order")
            != [
                "resolved_start_sample",
                "requested_start_seconds",
                "source_order",
                "occurrence_id",
            ]
            or type(document.get("dynamic_profile")) is not dict
            or canonical_json_sha256(document["dynamic_profile"])
            != profile_hash
            or type(document.get("time_index_canonical_json_bytes"))
            is not int
            or document["time_index_canonical_json_bytes"] < 1
            or document.get("score_duration") != reconstructed_duration
            or type(document.get("occurrence_count")) is not int
            or document.get("occurrence_count") != len(occurrences)
            or type(raw_occurrences) is not list
            or raw_occurrences != reconstructed_occurrences
            or canonical_json_bytes(document) != canonical_bytes
        ):
            raise ScoreV2PlanError(
                "plan.integrity_mismatch",
                "sealed plan artifact does not match its typed generation",
            )
        return canonical_bytes

    @property
    def artifact_sha256(self) -> str:
        self._trusted_artifact_bytes()
        return self._artifact_sha256

    @property
    def canonical_bytes(self) -> bytes:
        return self._trusted_artifact_bytes()

    @property
    def canonical_json_bytes_size(self) -> int:
        return len(self._trusted_artifact_bytes())

    def to_dict(self) -> dict[str, object]:
        try:
            value = json.loads(self._trusted_artifact_bytes())
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScoreV2PlanError(
                "plan.integrity_mismatch",
                "sealed plan artifact is not canonical JSON",
            ) from exc
        if type(value) is not dict:
            raise ScoreV2PlanError(
                "plan.integrity_mismatch",
                "sealed plan artifact is not an object",
            )
        return value


def _dynamic_profile_snapshot(
    dynamic_profile: object,
) -> tuple[tuple[tuple[str, Fraction], ...], dict[str, object], str]:
    if type(dynamic_profile) is not dict:
        raise ScoreV2PlanError(
            "plan.invalid_dynamic_profile",
            "dynamic_profile must be a plain dictionary of Rational values",
        )
    raw = dynamic_profile.copy()
    normalized: list[tuple[str, Fraction]] = []
    for mark, value in raw.items():
        if type(mark) is not str or mark not in _DYNAMIC_MARKS:
            raise ScoreV2PlanError(
                "plan.invalid_dynamic_profile",
                "dynamic_profile contains an unsupported dynamic mark",
            )
        if type(value) is not Rational:
            raise ScoreV2PlanError(
                "plan.invalid_dynamic_profile",
                f"dynamic_profile[{mark!r}] must be a Rational",
            )
        try:
            fraction = Rational(value.numerator, value.denominator).as_fraction()
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2PlanError(
                "plan.invalid_dynamic_profile",
                f"dynamic_profile[{mark!r}] is not a valid Rational",
            ) from exc
        if not Fraction(0) < fraction <= Fraction(1):
            raise ScoreV2PlanError(
                "plan.invalid_dynamic_profile",
                f"dynamic_profile[{mark!r}] must be within (0, 1]",
            )
        normalized.append((mark, fraction))
    if not normalized:
        raise ScoreV2PlanError(
            "plan.invalid_dynamic_profile",
            "dynamic_profile must not be empty",
        )
    normalized.sort(key=lambda item: item[0])
    profile_document: dict[str, object] = {
        "kind": "tianlai.score_v2_dynamic_profile",
        "schema_version": 1,
        "velocities": {
            mark: {
                "numerator": value.numerator,
                "denominator": value.denominator,
            }
            for mark, value in normalized
        },
    }
    return (
        tuple(normalized),
        profile_document,
        canonical_json_sha256(profile_document),
    )


def _capture_score_generation(
    snapshot: ScoreSourceSnapshot,
    *,
    limits: ProjectLimits,
) -> ScoreSourceSnapshot:
    """Detach one self-consistent source generation before any callbacks."""

    try:
        source_bytes = snapshot.canonical_bytes
        source_hash = snapshot.document_sha256
        unchanged = (
            snapshot.canonical_bytes is source_bytes
            and snapshot.document_sha256 == source_hash
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2PlanError(
            "plan.source_snapshot_mismatch",
            "score snapshot could not provide one stable source generation",
        ) from exc
    if type(source_bytes) is not bytes or type(source_hash) is not str or not unchanged:
        raise ScoreV2PlanError(
            "plan.source_snapshot_mismatch",
            "score snapshot changed while its source generation was captured",
        )
    if len(source_bytes) > limits.max_score_json_bytes:
        raise ResourceLimitError(
            "score.document_too_large",
            f"score JSON bytes {len(source_bytes):g} exceeds limit "
            f"{limits.max_score_json_bytes:g}; raise TIANLAI_MAX_SCORE_MIB "
            "deliberately if this project is trusted",
            actual=len(source_bytes),
            limit=limits.max_score_json_bytes,
        )
    if hashlib.sha256(source_bytes).hexdigest() != source_hash:
        raise ScoreV2PlanError(
            "plan.source_snapshot_mismatch",
            "score snapshot canonical bytes and hash disagree",
        )
    try:
        trusted = snapshot_score_bytes(source_bytes, limits)
    except ResourceLimitError:
        raise
    except (TypeError, ValueError) as exc:
        raise ScoreV2PlanError(
            "plan.source_snapshot_mismatch",
            "score snapshot canonical bytes are not one valid score generation",
        ) from exc
    if (
        trusted.canonical_bytes != source_bytes
        or trusted.document_sha256 != source_hash
    ):
        raise ScoreV2PlanError(
            "plan.source_snapshot_mismatch",
            "score snapshot document, canonical bytes, and hash disagree",
        )
    return trusted


def _effective_dynamic(part: ScorePartV2, note: ScoreNoteV2) -> str:
    mark = note.dynamic if note.dynamic is not None else part.default_dynamic
    if mark is None:
        raise ScoreV2PlanError(
            "plan.dynamic_unresolved",
            f"note {note.event_id!r} has no note or part default dynamic",
        )
    return mark


def _effective_articulation(
    part: ScorePartV2,
    note: ScoreNoteV2,
) -> str | None:
    if len(note.articulations) > 1:
        raise ScoreV2PlanError(
            "plan.multiple_articulations_unsupported",
            f"note {note.event_id!r} has multiple articulations",
        )
    if note.articulations:
        return note.articulations[0]
    return part.default_articulation


def _endpoint(
    resolution: SampleResolution,
    *,
    policy: str,
    event_id: str,
    label: str,
) -> ScoreV2EndpointEvidence:
    if resolution.fidelity != "exact" and policy == "exact":
        raise ScoreV2PlanError(
            "plan.sample_adaptation_not_authorized",
            f"note {event_id!r} {label} does not land exactly on the sample grid",
        )
    return ScoreV2EndpointEvidence.from_resolution(resolution)


@dataclass(slots=True)
class _PlanJsonBudget:
    maximum: int
    base_document: dict[str, object]
    used: int = field(init=False)
    occurrence_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if type(self.maximum) is not int or self.maximum <= 0:
            raise ValueError("plan JSON budget must be a positive integer")
        self.used = len(canonical_json_bytes(self.base_document))
        self._check(self.used)

    def _check(self, prospective: int) -> None:
        if prospective > self.maximum:
            raise ResourceLimitError(
                "plan.document_too_large",
                f"performance plan JSON bytes {prospective:g} exceeds limit "
                f"{self.maximum:g}; raise TIANLAI_MAX_PLAN_MIB deliberately "
                "if this project is trusted",
                actual=prospective,
                limit=self.maximum,
            )

    def add_occurrence(self, document: dict[str, object]) -> None:
        delta = len(canonical_json_bytes(document))
        if self.occurrence_count:
            delta += 1
        prospective = self.used + delta
        self._check(prospective)
        self.used = prospective
        self.occurrence_count += 1


def _build_occurrences(
    score: ScoreV2Document,
    time_index: ScoreV2TimeIndex,
    *,
    dynamic_values: dict[str, Fraction],
    sample_time_policy: str,
    budget: _PlanJsonBudget,
) -> tuple[ScoreV2Occurrence, ...]:
    time_by_event = {item.event_id: item for item in time_index.notes}
    outgoing = {
        tie.from_event_id: (tie.to_event_id, tie.tie_id)
        for tie in score.ties
    }
    incoming = {tie.to_event_id for tie in score.ties}
    note_by_event: dict[str, tuple[ScorePartV2, ScoreNoteV2, int]] = {}
    source_order = 0
    for part in score.parts:
        for note in part.notes:
            note_by_event[note.event_id] = (part, note, source_order)
            source_order += 1

    retained: list[ScoreV2Occurrence] = []
    visited: set[str] = set()
    for part in score.parts:
        for head in part.notes:
            if head.event_id in incoming:
                continue
            chain_notes: list[ScoreNoteV2] = []
            tie_ids: list[str] = []
            current = head
            while True:
                if current.event_id in visited:
                    raise ScoreV2PlanError(
                        "plan.tie_chain_invalid",
                        "tie chain revisits an event",
                    )
                visited.add(current.event_id)
                chain_notes.append(current)
                edge = outgoing.get(current.event_id)
                if edge is None:
                    break
                target_id, tie_id = edge
                target_info = note_by_event.get(target_id)
                if target_info is None:
                    raise ScoreV2PlanError(
                        "plan.tie_chain_invalid",
                        "tie chain references a missing event",
                    )
                target_part, target, _target_order = target_info
                if target_part.part_id != part.part_id:
                    raise ScoreV2PlanError(
                        "plan.tie_chain_invalid",
                        "tie chain crosses score parts",
                    )
                tie_ids.append(tie_id)
                current = target

            dynamics = tuple(_effective_dynamic(part, note) for note in chain_notes)
            articulations = tuple(
                _effective_articulation(part, note) for note in chain_notes
            )
            if len(set(dynamics)) != 1:
                raise ScoreV2PlanError(
                    "plan.tie_dynamic_conflict",
                    f"tie chain headed by {head.event_id!r} changes dynamic",
                )
            if len(set(articulations)) != 1:
                raise ScoreV2PlanError(
                    "plan.tie_articulation_conflict",
                    f"tie chain headed by {head.event_id!r} changes articulation",
                )
            dynamic = dynamics[0]
            velocity = dynamic_values.get(dynamic)
            if velocity is None:
                raise ScoreV2PlanError(
                    "plan.dynamic_unmapped",
                    f"effective dynamic {dynamic!r} has no profile value",
                )
            head_time = time_by_event.get(head.event_id)
            tail_time = time_by_event.get(chain_notes[-1].event_id)
            if head_time is None or tail_time is None:
                raise ScoreV2PlanError(
                    "plan.time_index_incomplete",
                    "exact-time index is missing a score event",
                )
            start = _endpoint(
                head_time.start.sample,
                policy=sample_time_policy,
                event_id=head.event_id,
                label="start",
            )
            end = _endpoint(
                tail_time.end.sample,
                policy=sample_time_policy,
                event_id=chain_notes[-1].event_id,
                label="end",
            )
            if end.resolved_sample <= start.resolved_sample:
                raise ScoreV2PlanError(
                    "plan.zero_sample_duration",
                    f"occurrence {head.event_id!r} has no positive sample duration",
                )
            occurrence = ScoreV2Occurrence(
                occurrence_id=head.event_id,
                part_id=part.part_id,
                source_event_ids=tuple(note.event_id for note in chain_notes),
                source_tie_ids=tuple(tie_ids),
                source_order=note_by_event[head.event_id][2],
                start=start,
                end=end,
                sounding_midi_note=ExactFraction(
                    head.sounding_pitch.midi_note.as_fraction()
                ),
                source_notes=tuple(
                    ScoreV2SourceNoteEvidence.from_note(note)
                    for note in chain_notes
                ),
                dynamic=dynamic,
                velocity=ExactFraction(velocity),
                articulation=articulations[0],
            )
            budget.add_occurrence(occurrence.to_dict())
            retained.append(occurrence)

    if len(visited) != len(note_by_event):
        raise ScoreV2PlanError(
            "plan.tie_chain_invalid",
            "one or more score events are not reachable from a tie-chain head",
        )
    retained.sort(
        key=lambda item: (
            item.start.resolved_sample,
            item.start.requested_seconds,
            item.source_order,
            item.occurrence_id,
        )
    )
    return tuple(retained)


def _make_plan(
    *,
    canonical_bytes: bytes,
    source_hash: str,
    projection_hash: str,
    time_hash: str,
    profile_hash: str,
    sample_rate: int,
    sample_policy: str,
    score_duration: ExactTimePoint,
    occurrences: tuple[ScoreV2Occurrence, ...],
) -> ScoreV2Plan:
    artifact_hash = hashlib.sha256(canonical_bytes).hexdigest()
    plan = object.__new__(ScoreV2Plan)
    object.__setattr__(plan, "source_document_sha256", source_hash)
    object.__setattr__(plan, "score_render_projection_sha256", projection_hash)
    object.__setattr__(plan, "time_index_sha256", time_hash)
    object.__setattr__(plan, "dynamic_profile_sha256", profile_hash)
    object.__setattr__(plan, "sample_rate", sample_rate)
    object.__setattr__(plan, "sample_time_policy", sample_policy)
    object.__setattr__(plan, "score_duration", score_duration)
    object.__setattr__(plan, "occurrences", occurrences)
    object.__setattr__(plan, "_canonical_bytes", canonical_bytes)
    object.__setattr__(plan, "_artifact_sha256", artifact_hash)
    seal = (
        source_hash,
        projection_hash,
        time_hash,
        profile_hash,
        sample_rate,
        sample_policy,
        score_duration,
        occurrences,
        canonical_bytes,
        artifact_hash,
    )
    object.__setattr__(plan, "_identity_seal", seal)
    return plan


def compile_score_v2_plan(
    snapshot: ScoreSourceSnapshot,
    *,
    sample_rate: int,
    sample_time_policy: Literal["exact", "adapt"],
    dynamic_profile: dict[str, Rational],
    limits: ProjectLimits | None = None,
) -> ScoreV2Plan:
    """Compile a trusted Score-v2 generation into a sealed plan foundation."""

    if type(snapshot) is not ScoreSourceSnapshot:
        raise TypeError("compile_score_v2_plan requires a ScoreSourceSnapshot")
    if (
        type(sample_time_policy) is not str
        or sample_time_policy not in SAMPLE_TIME_POLICIES
    ):
        raise ScoreV2PlanError(
            "plan.invalid_sample_time_policy",
            "sample_time_policy must be 'exact' or 'adapt'",
        )
    if limits is None:
        active_limits = ProjectLimits.from_environment()
    elif type(limits) is not ProjectLimits:
        raise TypeError("limits must be ProjectLimits or None")
    else:
        copied_limits = {
            name: getattr(limits, name)
            for name in ProjectLimits.__dataclass_fields__
        }
        if any(
            type(value) is not int or value <= 0
            for value in copied_limits.values()
        ):
            raise ValueError("ProjectLimits fields must retain positive integers")
        active_limits = ProjectLimits(**copied_limits)
    trusted_snapshot = _capture_score_generation(
        snapshot,
        limits=active_limits,
    )
    if (
        trusted_snapshot.identity_contract != SCORE_V2_IDENTITY_CONTRACT
        or trusted_snapshot.time_contract != SCORE_V2_TIME_CONTRACT
    ):
        raise ScoreV2PlanError(
            "plan.unsupported_score_contract",
            "compile_score_v2_plan requires a Score-v2 snapshot",
        )
    score = trusted_snapshot.score
    if type(score) is not ScoreV2Document:
        raise ScoreV2PlanError(
            "plan.unsupported_score_contract",
            "snapshot did not contain a Score-v2 document",
        )
    if score.phrases:
        raise ScoreV2PlanError(
            "plan.phrases_unsupported",
            "the first Score-v2 plan contract has no phrase execution policy",
        )
    if score.extensions:
        raise ScoreV2PlanError(
            "plan.extensions_unsupported",
            "the first Score-v2 plan contract does not consume extensions",
        )
    dynamic_items, profile_document, profile_hash = _dynamic_profile_snapshot(
        dynamic_profile
    )
    dynamic_values = dict(dynamic_items)
    # Projection hashing is independent of the time index.  Complete it
    # before retaining the potentially large index graph and canonical
    # artifact so their peak allocations do not overlap.
    projection_hash = score_render_projection_sha256(score)
    time_index = compile_score_v2_time(
        trusted_snapshot,
        sample_rate=sample_rate,
        limits=ScoreV2TimeLimits(
            max_output_seconds=min(
                active_limits.max_plan_seconds,
                MAX_V2_PLAN_SECONDS,
            ),
            max_sample_index=MAX_V2_PLAN_SAMPLE_INDEX,
            max_index_json_bytes=DEFAULT_MAX_TIME_INDEX_JSON_BYTES,
        ),
    )
    if time_index.source_document_sha256 != trusted_snapshot.document_sha256:
        raise ScoreV2PlanError(
            "plan.time_index_binding_mismatch",
            "exact-time index is bound to another score generation",
        )
    expected_occurrence_count = sum(len(part.notes) for part in score.parts) - len(
        score.ties
    )
    base = {
        "kind": SCORE_V2_PLAN_KIND,
        "schema_version": SCORE_V2_PLAN_SCHEMA_VERSION,
        "contract": SCORE_V2_PLAN_CONTRACT,
        "bindings": {
            "source_document_sha256": trusted_snapshot.document_sha256,
            "score_render_projection_sha256": projection_hash,
            "time_index_sha256": time_index.artifact_sha256,
            "dynamic_profile_sha256": profile_hash,
        },
        "sample_rate": sample_rate,
        "sample_time_policy": sample_time_policy,
        "sample_time_policy_scope": "occurrence_endpoints",
        "sample_rounding_mode": SAMPLE_ROUNDING_MODE,
        "occurrence_order": [
            "resolved_start_sample",
            "requested_start_seconds",
            "source_order",
            "occurrence_id",
        ],
        "dynamic_profile": profile_document,
        "time_index_canonical_json_bytes": time_index.canonical_json_bytes_size,
        "score_duration": time_index.score_duration.to_dict(),
        "occurrence_count": expected_occurrence_count,
        "occurrences": [],
    }
    budget = _PlanJsonBudget(
        maximum=active_limits.max_plan_json_bytes,
        base_document=base,
    )
    occurrences = _build_occurrences(
        score,
        time_index,
        dynamic_values=dynamic_values,
        sample_time_policy=sample_time_policy,
        budget=budget,
    )
    document: dict[str, object] = {
        **base,
        "occurrences": [item.to_dict() for item in occurrences],
    }
    if len(occurrences) != expected_occurrence_count:
        raise RuntimeError("score-v2 occurrence count accounting mismatch")
    canonical_bytes = canonical_json_bytes(document)
    if len(canonical_bytes) != budget.used:
        raise RuntimeError("score-v2 plan canonical byte accounting mismatch")
    return _make_plan(
        canonical_bytes=canonical_bytes,
        source_hash=trusted_snapshot.document_sha256,
        projection_hash=projection_hash,
        time_hash=time_index.artifact_sha256,
        profile_hash=profile_hash,
        sample_rate=sample_rate,
        sample_policy=sample_time_policy,
        score_duration=time_index.score_duration,
        occurrences=occurrences,
    )


__all__ = [
    "SAMPLE_TIME_POLICIES",
    "MAX_V2_PLAN_SAMPLE_INDEX",
    "MAX_V2_PLAN_SECONDS",
    "SCORE_V2_PLAN_CONTRACT",
    "SCORE_V2_PLAN_KIND",
    "SCORE_V2_PLAN_SCHEMA_VERSION",
    "ScoreV2EndpointEvidence",
    "ScoreV2Occurrence",
    "ScoreV2Plan",
    "ScoreV2PlanError",
    "ScoreV2SourceNoteEvidence",
    "ScoreV2WrittenPitchEvidence",
    "compile_score_v2_plan",
]
