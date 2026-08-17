"""Exact-time compilation for a trusted score-v2 source snapshot.

This module is deliberately isolated from Tianlai's legacy conductor and
renderer.  It resolves score-v2's rational musical coordinates into an
immutable exact-time index and sample-grid evidence; it does *not* claim that
the score can be rendered.

The public compiler accepts :class:`~tianlai.score_source.ScoreSourceSnapshot`
rather than a detached ``ScoreV2Document``.  That keeps every compiled time
and the source-document hash bound to one validated source generation.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from fractions import Fraction
import hashlib
import json
from types import MappingProxyType
from typing import Literal, Mapping, NamedTuple, Sequence

from .canonical_json import canonical_json_bytes
from .resource_limits import ProjectLimits
from .score_source import ScoreSourceSnapshot, snapshot_score_bytes
from .score_v2 import (
    MAX_NOTES,
    MAX_PARTS,
    MAX_SAFE_INTEGER,
    MAX_TEMPO_EVENTS,
    MAX_TIMELINE_CUMULATIVE_POSITION_BITS,
    Rational,
    SCORE_V2_IDENTITY_CONTRACT,
    SCORE_V2_TIME_CONTRACT,
    ScorePosition,
    ScoreV2Document,
)


MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 384_000
DEFAULT_MAX_TIME_INDEX_JSON_BYTES = 32 * 1024 * 1024
MAX_TIME_INDEX_JSON_BYTES = 64 * 1024 * 1024
TIME_INDEX_KIND = "tianlai.score-v2-time-index"
TIME_INDEX_SCHEMA_VERSION = 1
SAMPLE_ROUNDING_MODE = "nearest-ties-to-even"

_EVENT_PRIORITY = MappingProxyType(
    {
        "meter": 0,
        "tempo": 1,
        "note_end": 2,
        "note_start": 3,
    }
)


class ScoreV2TimeCompileError(ValueError):
    """A stable, fail-closed error raised by exact-time compilation."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str | None = None,
    ) -> None:
        self.code = code
        self.path = path
        location = f" at {path}" if path is not None else ""
        super().__init__(f"{code}{location}: {message}")


@dataclass(frozen=True, slots=True)
class ScoreV2TimeLimits:
    """Local budgets for the isolated exact-time compiler.

    These are intentionally separate from ``ProjectLimits`` until a v2
    conductor owns an end-to-end render contract.  The sample-rate range is
    nevertheless identical to the existing project/render contract so the
    time compiler cannot bless a configuration that the rest of Tianlai must
    reject.
    """

    max_fraction_bits: int = MAX_TIMELINE_CUMULATIVE_POSITION_BITS
    max_tempo_segments: int = MAX_TEMPO_EVENTS
    max_output_seconds: int = 2 * 60 * 60
    max_sample_index: int = MAX_SAFE_INTEGER
    max_index_json_bytes: int = DEFAULT_MAX_TIME_INDEX_JSON_BYTES

    def __post_init__(self) -> None:
        for name in (
            "max_fraction_bits",
            "max_tempo_segments",
            "max_output_seconds",
            "max_sample_index",
            "max_index_json_bytes",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_fraction_bits > MAX_TIMELINE_CUMULATIVE_POSITION_BITS:
            raise ValueError(
                "max_fraction_bits exceeds the compiler hard bound"
            )
        if self.max_tempo_segments > MAX_TEMPO_EVENTS:
            raise ValueError(
                "max_tempo_segments exceeds the score-v2 hard bound"
            )
        if self.max_output_seconds > MAX_SAFE_INTEGER:
            raise ValueError(
                "max_output_seconds exceeds the JSON safe-integer bound"
            )
        if self.max_sample_index > MAX_SAFE_INTEGER:
            raise ValueError(
                "max_sample_index must not exceed the JSON safe-integer bound"
            )
        if self.max_index_json_bytes > MAX_TIME_INDEX_JSON_BYTES:
            raise ValueError(
                "max_index_json_bytes exceeds the compiler hard bound"
            )


class ExactFraction(tuple):
    """A normalized exact rational whose public state cannot be reassigned.

    ``fractions.Fraction`` is mathematically immutable through its normal API,
    but Python's public ``object.__setattr__`` can replace its private
    numerator.  Public evidence records use this tuple-backed value instead;
    arithmetic remains internal and converts through :meth:`as_fraction`.
    """

    __slots__ = ()

    def __new__(
        cls,
        numerator: int | Fraction | "ExactFraction",
        denominator: int = 1,
    ) -> "ExactFraction":
        if type(denominator) is not int or isinstance(denominator, bool):
            raise TypeError("exact fraction denominator must be an integer")
        if type(numerator) is ExactFraction:
            if denominator != 1:
                raise TypeError(
                    "an ExactFraction cannot be combined with a denominator"
                )
            return numerator
        if type(numerator) is Fraction:
            if denominator != 1:
                raise TypeError("a Fraction cannot be combined with a denominator")
            value = numerator
        elif type(numerator) is int and not isinstance(numerator, bool):
            value = Fraction(numerator, denominator)
        else:
            raise TypeError("exact fraction numerator must be an integer or Fraction")
        return tuple.__new__(cls, (value.numerator, value.denominator))

    @property
    def numerator(self) -> int:
        return self[0]

    @property
    def denominator(self) -> int:
        return self[1]

    def as_fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)

    @staticmethod
    def _other_fraction(other: object) -> Fraction | None:
        if type(other) is ExactFraction:
            return other.as_fraction()
        if type(other) is Fraction:
            return other
        if type(other) is int and not isinstance(other, bool):
            return Fraction(other, 1)
        return None

    def __eq__(self, other: object) -> bool:
        candidate = self._other_fraction(other)
        if candidate is None:
            # Do not delegate to tuple's structural equality: this value uses
            # Fraction-compatible hashing, so equality with ``(n, d)`` would
            # violate Python's equal-values-have-equal-hashes contract.
            return False
        return self.as_fraction() == candidate

    def __ne__(self, other: object) -> bool:
        equal = self.__eq__(other)
        return not equal

    def __lt__(self, other: object) -> bool:
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction ordering requires an exact numeric operand"
            )
        return self.as_fraction() < candidate

    def __le__(self, other: object) -> bool:
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction ordering requires an exact numeric operand"
            )
        return self.as_fraction() <= candidate

    def __gt__(self, other: object) -> bool:
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction ordering requires an exact numeric operand"
            )
        return self.as_fraction() > candidate

    def __ge__(self, other: object) -> bool:
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction ordering requires an exact numeric operand"
            )
        return self.as_fraction() >= candidate

    def __bool__(self) -> bool:
        return self.numerator != 0

    def __add__(self, other: object) -> "ExactFraction":
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction arithmetic requires an exact numeric operand"
            )
        return ExactFraction(self.as_fraction() + candidate)

    def __radd__(self, other: object) -> "ExactFraction":
        return self.__add__(other)

    def __sub__(self, other: object) -> "ExactFraction":
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction arithmetic requires an exact numeric operand"
            )
        return ExactFraction(self.as_fraction() - candidate)

    def __rsub__(self, other: object) -> "ExactFraction":
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction arithmetic requires an exact numeric operand"
            )
        return ExactFraction(candidate - self.as_fraction())

    def __mul__(self, other: object) -> "ExactFraction":
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction arithmetic requires an exact numeric operand"
            )
        return ExactFraction(self.as_fraction() * candidate)

    def __rmul__(self, other: object) -> "ExactFraction":
        return self.__mul__(other)

    def __truediv__(self, other: object) -> "ExactFraction":
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction arithmetic requires an exact numeric operand"
            )
        return ExactFraction(self.as_fraction() / candidate)

    def __rtruediv__(self, other: object) -> "ExactFraction":
        candidate = self._other_fraction(other)
        if candidate is None:
            raise TypeError(
                "ExactFraction arithmetic requires an exact numeric operand"
            )
        return ExactFraction(candidate / self.as_fraction())

    def __neg__(self) -> "ExactFraction":
        return ExactFraction(-self.as_fraction())

    def __pos__(self) -> "ExactFraction":
        return self

    def __abs__(self) -> "ExactFraction":
        return ExactFraction(abs(self.as_fraction()))

    def __hash__(self) -> int:
        return hash(self.as_fraction())

    def __float__(self) -> float:
        return float(self.as_fraction())

    def __repr__(self) -> str:
        return f"ExactFraction({self.numerator}, {self.denominator})"

    def __str__(self) -> str:
        return str(self.as_fraction())

    def __getnewargs__(self) -> tuple[int, int]:
        return (self.numerator, self.denominator)


def _fraction_dict(value: Fraction | ExactFraction) -> dict[str, str]:
    """Serialize arbitrary-size exact results without unsafe JSON integers."""

    return {
        "numerator": str(value.numerator),
        "denominator": str(value.denominator),
    }


class SampleResolution(NamedTuple):
    """Evidence for resolving one exact requested time to a sample grid.

    ``error_seconds`` is signed ``resolved_seconds - requested_seconds``;
    no endpoint is silently clamped to a neighbouring sample.
    """

    requested_seconds: ExactFraction
    requested_sample: ExactFraction
    sample_rate: int
    resolved_sample: int
    resolved_seconds: ExactFraction
    error_seconds: ExactFraction
    fidelity: Literal["exact", "rounded"]
    rounding_mode: str = SAMPLE_ROUNDING_MODE

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_seconds": _fraction_dict(self.requested_seconds),
            "requested_sample": _fraction_dict(self.requested_sample),
            "sample_rate": self.sample_rate,
            "resolved_sample": self.resolved_sample,
            "resolved_seconds": _fraction_dict(self.resolved_seconds),
            "error_seconds": _fraction_dict(self.error_seconds),
            "fidelity": self.fidelity,
            "rounding_mode": self.rounding_mode,
        }


class ExactTimePoint(NamedTuple):
    """One absolute musical position and its exact/sample-grid time."""

    quarter: ExactFraction
    sample: SampleResolution

    @property
    def seconds(self) -> ExactFraction:
        return self.sample.requested_seconds

    def to_dict(self) -> dict[str, object]:
        return {
            "quarter": _fraction_dict(self.quarter),
            "sample": self.sample.to_dict(),
        }


class CompiledMeasureTime(NamedTuple):
    measure_id: str
    measure_index: int
    start: ExactTimePoint
    end: ExactTimePoint

    def to_dict(self) -> dict[str, object]:
        return {
            "measure_id": self.measure_id,
            "measure_index": self.measure_index,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
        }


class TempoSegment(NamedTuple):
    tempo_id: str
    segment_index: int
    quarter_bpm: ExactFraction
    seconds_per_quarter: ExactFraction
    start: ExactTimePoint
    end: ExactTimePoint

    def to_dict(self) -> dict[str, object]:
        return {
            "tempo_id": self.tempo_id,
            "segment_index": self.segment_index,
            "quarter_bpm": _fraction_dict(self.quarter_bpm),
            "seconds_per_quarter": _fraction_dict(
                self.seconds_per_quarter
            ),
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
        }


TimelineEventKind = Literal["meter", "tempo", "note_end", "note_start"]


class CompiledTimelineEvent(NamedTuple):
    """A deterministically ordered boundary on the requested sample grid."""

    kind: TimelineEventKind
    subject_id: str
    at: ExactTimePoint
    source_order: int
    part_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "subject_id": self.subject_id,
            "at": self.at.to_dict(),
            "source_order": self.source_order,
        }
        if self.part_id is not None:
            result["part_id"] = self.part_id
        return result


class CompiledNoteTime(NamedTuple):
    part_id: str
    event_id: str
    start: ExactTimePoint
    end: ExactTimePoint
    source_order: int

    def to_dict(self) -> dict[str, object]:
        return {
            "part_id": self.part_id,
            "event_id": self.event_id,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
            "source_order": self.source_order,
        }


def _time_index_document(
    *,
    source_document_sha256: str,
    sample_rate: int,
    score_duration: ExactTimePoint,
    measures: Sequence[CompiledMeasureTime],
    tempo_segments: Sequence[TempoSegment],
    meter_events: Sequence[CompiledTimelineEvent],
    tempo_events: Sequence[CompiledTimelineEvent],
    notes: Sequence[CompiledNoteTime],
    events: Sequence[CompiledTimelineEvent],
) -> dict[str, object]:
    return {
        "kind": TIME_INDEX_KIND,
        "schema_version": TIME_INDEX_SCHEMA_VERSION,
        "contract": "exact-time-foundation-not-render-plan",
        "source_document_sha256": source_document_sha256,
        "source_identity_contract": SCORE_V2_IDENTITY_CONTRACT,
        "source_time_contract": SCORE_V2_TIME_CONTRACT,
        "sample_rate": sample_rate,
        "sample_rounding_mode": SAMPLE_ROUNDING_MODE,
        "same_sample_order": [
            "requested_seconds",
            "meter",
            "tempo",
            "note_end",
            "note_start",
            "source_order",
            "subject_id",
        ],
        "score_duration": score_duration.to_dict(),
        "measures": [item.to_dict() for item in measures],
        "tempo_segments": [item.to_dict() for item in tempo_segments],
        "meter_events": [item.to_dict() for item in meter_events],
        "tempo_events": [item.to_dict() for item in tempo_events],
        "notes": [item.to_dict() for item in notes],
        "events": [item.to_dict() for item in events],
    }


class _MeasureSpan(NamedTuple):
    measure_id: str
    measure_index: int
    start_quarter: Fraction
    end_quarter: Fraction


class _TempoSpan(NamedTuple):
    tempo_id: str
    segment_index: int
    start_quarter: Fraction
    end_quarter: Fraction
    quarter_bpm: Fraction
    seconds_per_quarter: Fraction
    start_seconds: Fraction
    end_seconds: Fraction


class _ResolverMeasureSpan(NamedTuple):
    measure_id: str
    measure_index: int
    start_quarter: ExactFraction
    end_quarter: ExactFraction


class _ResolverTempoSpan(NamedTuple):
    tempo_id: str
    segment_index: int
    start_quarter: ExactFraction
    end_quarter: ExactFraction
    quarter_bpm: ExactFraction
    seconds_per_quarter: ExactFraction
    start_seconds: ExactFraction
    end_seconds: ExactFraction


@dataclass(frozen=True, slots=True)
class ScoreV2TimeIndex:
    """Immutable exact-time lookup bound to one score source generation.

    Public record nodes are tuple-backed, so ``object.__setattr__`` cannot
    bypass their immutability.  ``to_dict`` returns a detached parse of the
    canonical artifact captured at compilation, while position lookup checks
    an O(1) identity seal around its private immutable resolver structures.

    ``events`` is sorted by resolved sample, then exact requested seconds,
    then ``meter < tempo < note_end < note_start``, then source order and ID.
    Consequently, two boundaries rounded to the same sample retain their
    exact chronological order.  At one exact instant a note end precedes a
    note start; a positive-duration note whose two endpoints quantize to the
    same sample still remains start-before-end because its requested start is
    earlier.
    """

    source_document_sha256: str
    sample_rate: int
    score_duration: ExactTimePoint
    measures: tuple[CompiledMeasureTime, ...]
    tempo_segments: tuple[TempoSegment, ...]
    meter_events: tuple[CompiledTimelineEvent, ...]
    tempo_events: tuple[CompiledTimelineEvent, ...]
    notes: tuple[CompiledNoteTime, ...]
    events: tuple[CompiledTimelineEvent, ...]
    _measure_lookup: Mapping[str, _ResolverMeasureSpan] = field(
        repr=False,
        compare=False,
    )
    _tempo_spans: tuple[_ResolverTempoSpan, ...] = field(
        repr=False,
        compare=False,
    )
    _tempo_starts: tuple[ExactFraction, ...] = field(
        repr=False,
        compare=False,
    )
    _limits: ScoreV2TimeLimits = field(repr=False, compare=False)
    _canonical_bytes: bytes = field(repr=False, compare=False)
    _artifact_sha256: str = field(repr=False, compare=False)
    _resolver_identity: tuple[object, ...] = field(
        repr=False,
        compare=False,
    )

    def _trusted_resolver_state(
        self,
    ) -> tuple[
        Mapping[str, _ResolverMeasureSpan],
        tuple[_ResolverTempoSpan, ...],
        tuple[ExactFraction, ...],
        Fraction,
        int,
        ScoreV2TimeLimits,
    ]:
        """Validate the O(1) identity seal around immutable resolver state."""

        try:
            (
                measure_lookup,
                tempo_spans,
                tempo_starts,
                limits_object,
                limits_signature,
                source_hash,
                sample_rate,
                score_duration,
                measures,
                tempo_segments,
                meter_events,
                tempo_events,
                notes,
                events,
                canonical_bytes,
                artifact_sha256,
            ) = self._resolver_identity
            current_limits_signature = (
                self._limits.max_fraction_bits,
                self._limits.max_tempo_segments,
                self._limits.max_output_seconds,
                self._limits.max_sample_index,
                self._limits.max_index_json_bytes,
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2TimeCompileError(
                "time.index_integrity_mismatch",
                "compiled time-index state no longer matches its identity seal",
            ) from exc
        if (
            self._measure_lookup is not measure_lookup
            or self._tempo_spans is not tempo_spans
            or self._tempo_starts is not tempo_starts
            or self._limits is not limits_object
            or type(self._limits) is not ScoreV2TimeLimits
            or current_limits_signature != limits_signature
            or self.source_document_sha256 != source_hash
            or self.sample_rate != sample_rate
            or self.score_duration is not score_duration
            or self.measures is not measures
            or self.tempo_segments is not tempo_segments
            or self.meter_events is not meter_events
            or self.tempo_events is not tempo_events
            or self.notes is not notes
            or self.events is not events
            or self._canonical_bytes is not canonical_bytes
            or self._artifact_sha256 != artifact_sha256
        ):
            raise ScoreV2TimeCompileError(
                "time.index_integrity_mismatch",
                "compiled time-index state no longer matches its identity seal",
            )
        try:
            active_limits = ScoreV2TimeLimits(
                max_fraction_bits=limits_signature[0],
                max_tempo_segments=limits_signature[1],
                max_output_seconds=limits_signature[2],
                max_sample_index=limits_signature[3],
                max_index_json_bytes=limits_signature[4],
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise ScoreV2TimeCompileError(
                "time.index_integrity_mismatch",
                "compiled time-index limits no longer match their identity seal",
            ) from exc
        return (
            measure_lookup,
            tempo_spans,
            tempo_starts,
            score_duration.quarter.as_fraction(),
            sample_rate,
            active_limits,
        )

    def resolve_position(self, position: ScorePosition) -> ExactTimePoint:
        """Resolve a canonical measure/offset coordinate.

        The final measure's exact end is accepted as the timeline endpoint;
        an intermediate measure-end spelling is rejected as ambiguous and
        must be written as the next measure at offset zero.
        """

        (
            measure_lookup,
            tempo_spans,
            tempo_starts,
            score_duration_quarter,
            sample_rate,
            active_limits,
        ) = self._trusted_resolver_state()
        if type(position) is not ScorePosition:
            raise ScoreV2TimeCompileError(
                "time.invalid_position",
                "position must be an exact ScorePosition",
            )
        try:
            clean = ScorePosition(
                measure_id=position.measure_id,
                offset_quarters=Rational(
                    position.offset_quarters.numerator,
                    position.offset_quarters.denominator,
                ),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ScoreV2TimeCompileError(
                "time.invalid_position",
                "position was not a valid score-v2 coordinate",
            ) from exc
        span = measure_lookup.get(clean.measure_id)
        if span is None:
            raise ScoreV2TimeCompileError(
                "time.unknown_measure",
                f"unknown measure_id {clean.measure_id!r}",
            )
        offset = _from_rational(
            clean.offset_quarters,
            limits=active_limits,
            path="position.offset_quarters",
        )
        span_start = span.start_quarter.as_fraction()
        span_end = span.end_quarter.as_fraction()
        duration = _checked_sub(
            span_end,
            span_start,
            limits=active_limits,
            path="position measure duration",
        )
        if offset > duration:
            raise ScoreV2TimeCompileError(
                "time.position_out_of_range",
                "offset exceeds the referenced measure duration",
            )
        if offset == duration and span_end != score_duration_quarter:
            raise ScoreV2TimeCompileError(
                "time.ambiguous_measure_end",
                "intermediate measure-end must use the next measure at zero",
            )
        quarter = _checked_add(
            span_start,
            offset,
            limits=active_limits,
            path="resolved position quarter",
        )
        return _time_at_quarter(
            quarter,
            spans=tempo_spans,
            starts=tempo_starts,
            score_duration=score_duration_quarter,
            sample_rate=sample_rate,
            limits=active_limits,
            path="position",
        )

    def _trusted_artifact_bytes(self) -> bytes:
        """Return the sealed canonical artifact after an integrity check."""

        self._trusted_resolver_state()
        if (
            type(self._canonical_bytes) is not bytes
            or type(self._artifact_sha256) is not str
            or hashlib.sha256(self._canonical_bytes).hexdigest()
            != self._artifact_sha256
        ):
            raise ScoreV2TimeCompileError(
                "time.index_integrity_mismatch",
                "compiled time-index bytes no longer match their identity seal",
            )
        return self._canonical_bytes

    @property
    def artifact_sha256(self) -> str:
        """SHA-256 identity of the sealed canonical time-index artifact."""

        self._trusted_artifact_bytes()
        return self._artifact_sha256

    @property
    def canonical_json_bytes_size(self) -> int:
        """Byte size of the sealed canonical time-index artifact."""

        return len(self._trusted_artifact_bytes())

    def to_dict(self) -> dict[str, object]:
        canonical_bytes = self._trusted_artifact_bytes()
        try:
            document = json.loads(canonical_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise ScoreV2TimeCompileError(
                "time.index_integrity_mismatch",
                "compiled time-index bytes are not valid JSON",
            ) from exc
        if type(document) is not dict:
            raise ScoreV2TimeCompileError(
                "time.index_integrity_mismatch",
                "compiled time-index artifact is not a JSON object",
            )
        return document


def _raise_complexity(path: str) -> None:
    raise ScoreV2TimeCompileError(
        "time.rational_complexity",
        "exact rational complexity exceeds the configured bit bound",
        path=path,
    )


def _check_fraction(
    value: Fraction,
    *,
    limits: ScoreV2TimeLimits,
    path: str,
) -> Fraction:
    if type(value) is not Fraction:
        raise ScoreV2TimeCompileError(
            "time.invalid_fraction",
            "internal exact time must be a Fraction",
            path=path,
        )
    if (
        abs(value.numerator).bit_length() > limits.max_fraction_bits
        or value.denominator.bit_length() > limits.max_fraction_bits
    ):
        _raise_complexity(path)
    return value


def _product_fits(left: int, right: int, maximum_bits: int) -> bool:
    if left == 0 or right == 0:
        return True
    maximum_magnitude = (1 << maximum_bits) - 1
    return abs(left) <= maximum_magnitude // abs(right)


def _checked_add(
    left: Fraction,
    right: Fraction,
    *,
    limits: ScoreV2TimeLimits,
    path: str,
) -> Fraction:
    left = _check_fraction(left, limits=limits, path=f"{path} left operand")
    right = _check_fraction(right, limits=limits, path=f"{path} right operand")
    if left.numerator == 0:
        return right
    if right.numerator == 0:
        return left
    if (
        left.denominator == right.denominator
        and left.numerator == -right.numerator
    ):
        return Fraction(0, 1)
    # Fraction addition uses the denominator GCD.  Preflight each reduced
    # multiplication and the possible carry before constructing the result.
    from math import gcd

    divisor = gcd(left.denominator, right.denominator)
    left_factor = right.denominator // divisor
    right_factor = left.denominator // divisor
    if not _product_fits(
        left.numerator,
        left_factor,
        limits.max_fraction_bits,
    ) or not _product_fits(
        right.numerator,
        right_factor,
        limits.max_fraction_bits,
    ):
        _raise_complexity(path)
    if not _product_fits(
        left.denominator,
        left_factor,
        limits.max_fraction_bits,
    ):
        _raise_complexity(path)
    # The products are now known to be within budget.  Compute each once, and
    # preflight a same-sign sum against the bit ceiling.  Opposite signs can
    # only reduce the magnitude.
    left_term = left.numerator * left_factor
    right_term = right.numerator * right_factor
    maximum_magnitude = (1 << limits.max_fraction_bits) - 1
    if (left_term < 0) == (right_term < 0) and (
        abs(left_term) > maximum_magnitude - abs(right_term)
    ):
        _raise_complexity(path)
    numerator = left_term + right_term
    denominator = left.denominator * left_factor
    result = Fraction(numerator, denominator)
    return _check_fraction(result, limits=limits, path=f"{path} result")


def _checked_sub(
    left: Fraction,
    right: Fraction,
    *,
    limits: ScoreV2TimeLimits,
    path: str,
) -> Fraction:
    right = _check_fraction(right, limits=limits, path=f"{path} right operand")
    # Negation cannot increase numerator bit length.
    negative = Fraction(-right.numerator, right.denominator)
    _check_fraction(negative, limits=limits, path=f"{path} negated operand")
    return _checked_add(left, negative, limits=limits, path=path)


def _checked_mul(
    left: Fraction,
    right: Fraction,
    *,
    limits: ScoreV2TimeLimits,
    path: str,
) -> Fraction:
    from math import gcd

    left = _check_fraction(left, limits=limits, path=f"{path} left operand")
    right = _check_fraction(right, limits=limits, path=f"{path} right operand")
    cancel_left = gcd(abs(left.numerator), right.denominator)
    cancel_right = gcd(abs(right.numerator), left.denominator)
    left_numerator = left.numerator // cancel_left
    right_denominator = right.denominator // cancel_left
    right_numerator = right.numerator // cancel_right
    left_denominator = left.denominator // cancel_right
    if not _product_fits(
        left_numerator,
        right_numerator,
        limits.max_fraction_bits,
    ) or not _product_fits(
        left_denominator,
        right_denominator,
        limits.max_fraction_bits,
    ):
        _raise_complexity(path)
    result = left * right
    return _check_fraction(result, limits=limits, path=f"{path} result")


def _checked_div(
    left: Fraction,
    right: Fraction,
    *,
    limits: ScoreV2TimeLimits,
    path: str,
) -> Fraction:
    right = _check_fraction(right, limits=limits, path=f"{path} divisor")
    if right.numerator == 0:
        raise ScoreV2TimeCompileError(
            "time.zero_divisor",
            "exact time division by zero",
            path=path,
        )
    reciprocal = Fraction(right.denominator, right.numerator)
    reciprocal = _check_fraction(
        reciprocal,
        limits=limits,
        path=f"{path} reciprocal",
    )
    return _checked_mul(left, reciprocal, limits=limits, path=path)


def _from_rational(
    value: Rational,
    *,
    limits: ScoreV2TimeLimits,
    path: str,
) -> Fraction:
    if type(value) is not Rational:
        raise ScoreV2TimeCompileError(
            "time.invalid_document",
            "expected a score-v2 Rational",
            path=path,
        )
    try:
        result = Fraction(value.numerator, value.denominator)
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise ScoreV2TimeCompileError(
            "time.invalid_document",
            "invalid score-v2 Rational",
            path=path,
        ) from exc
    return _check_fraction(result, limits=limits, path=path)


def _nearest_ties_to_even(value: Fraction) -> int:
    """Round one non-negative fraction without floating-point conversion."""

    if value < 0:
        raise ValueError("sample position must not be negative")
    quotient, remainder = divmod(value.numerator, value.denominator)
    half, denominator_is_odd = divmod(value.denominator, 2)
    if remainder > half:
        return quotient + 1
    if not denominator_is_odd and remainder == half and quotient % 2:
        return quotient + 1
    return quotient


def _resolve_sample(
    seconds: Fraction,
    *,
    sample_rate: int,
    limits: ScoreV2TimeLimits,
    path: str,
) -> SampleResolution:
    seconds = _check_fraction(seconds, limits=limits, path=f"{path} seconds")
    if seconds < 0:
        raise ScoreV2TimeCompileError(
            "time.negative_time",
            "requested time must not be negative",
            path=path,
        )
    requested_sample = _checked_mul(
        seconds,
        Fraction(sample_rate, 1),
        limits=limits,
        path=f"{path} requested sample",
    )
    resolved_sample = _nearest_ties_to_even(requested_sample)
    if resolved_sample > limits.max_sample_index:
        raise ScoreV2TimeCompileError(
            "time.sample_index_overflow",
            "resolved sample exceeds the configured bound",
            path=path,
        )
    resolved_seconds = _check_fraction(
        Fraction(resolved_sample, sample_rate),
        limits=limits,
        path=f"{path} resolved seconds",
    )
    error_seconds = _checked_sub(
        resolved_seconds,
        seconds,
        limits=limits,
        path=f"{path} sample error",
    )
    fidelity: Literal["exact", "rounded"] = (
        "exact" if requested_sample.denominator == 1 else "rounded"
    )
    return SampleResolution(
        requested_seconds=ExactFraction(seconds),
        requested_sample=ExactFraction(requested_sample),
        sample_rate=sample_rate,
        resolved_sample=resolved_sample,
        resolved_seconds=ExactFraction(resolved_seconds),
        error_seconds=ExactFraction(error_seconds),
        fidelity=fidelity,
    )


def _point(
    quarter: Fraction,
    seconds: Fraction,
    *,
    sample_rate: int,
    limits: ScoreV2TimeLimits,
    path: str,
) -> ExactTimePoint:
    quarter = _check_fraction(quarter, limits=limits, path=f"{path} quarter")
    return ExactTimePoint(
        quarter=ExactFraction(quarter),
        sample=_resolve_sample(
            seconds,
            sample_rate=sample_rate,
            limits=limits,
            path=path,
        ),
    )


def _time_at_quarter(
    quarter: Fraction,
    *,
    spans: tuple[_TempoSpan, ...] | tuple[_ResolverTempoSpan, ...],
    starts: tuple[Fraction, ...] | tuple[ExactFraction, ...],
    score_duration: Fraction,
    sample_rate: int,
    limits: ScoreV2TimeLimits,
    path: str,
) -> ExactTimePoint:
    quarter = _check_fraction(quarter, limits=limits, path=f"{path} quarter")
    if quarter < 0 or quarter > score_duration:
        raise ScoreV2TimeCompileError(
            "time.position_out_of_range",
            "absolute quarter lies outside the score timeline",
            path=path,
        )
    index = bisect_right(starts, quarter) - 1
    if index < 0:
        raise ScoreV2TimeCompileError(
            "time.missing_origin_tempo",
            "tempo map does not cover the score origin",
            path=path,
        )
    span = spans[index]
    span_start_quarter = (
        span.start_quarter.as_fraction()
        if type(span.start_quarter) is ExactFraction
        else span.start_quarter
    )
    seconds_per_quarter = (
        span.seconds_per_quarter.as_fraction()
        if type(span.seconds_per_quarter) is ExactFraction
        else span.seconds_per_quarter
    )
    span_start_seconds = (
        span.start_seconds.as_fraction()
        if type(span.start_seconds) is ExactFraction
        else span.start_seconds
    )
    delta_quarters = _checked_sub(
        quarter,
        span_start_quarter,
        limits=limits,
        path=f"{path} tempo-relative quarter",
    )
    delta_seconds = _checked_mul(
        delta_quarters,
        seconds_per_quarter,
        limits=limits,
        path=f"{path} tempo-relative seconds",
    )
    seconds = _checked_add(
        span_start_seconds,
        delta_seconds,
        limits=limits,
        path=f"{path} absolute seconds",
    )
    if seconds > limits.max_output_seconds:
        raise ScoreV2TimeCompileError(
            "time.duration_too_long",
            "resolved time exceeds the configured output duration",
            path=path,
        )
    return _point(
        quarter,
        seconds,
        sample_rate=sample_rate,
        limits=limits,
        path=path,
    )


def _position_quarter(
    position: ScorePosition,
    *,
    measures: Mapping[str, _MeasureSpan],
    limits: ScoreV2TimeLimits,
    path: str,
    allow_final_end: bool = False,
    score_duration: Fraction,
) -> Fraction:
    if type(position) is not ScorePosition:
        raise ScoreV2TimeCompileError(
            "time.invalid_document",
            "expected a score-v2 position",
            path=path,
        )
    span = measures.get(position.measure_id)
    if span is None:
        raise ScoreV2TimeCompileError(
            "time.unknown_measure",
            f"unknown measure_id {position.measure_id!r}",
            path=path,
        )
    offset = _from_rational(
        position.offset_quarters,
        limits=limits,
        path=f"{path}.offset_quarters",
    )
    duration = _checked_sub(
        span.end_quarter,
        span.start_quarter,
        limits=limits,
        path=f"{path} measure duration",
    )
    if offset > duration:
        raise ScoreV2TimeCompileError(
            "time.position_out_of_range",
            "offset exceeds the referenced measure duration",
            path=path,
        )
    if offset == duration and not (
        allow_final_end and span.end_quarter == score_duration
    ):
        raise ScoreV2TimeCompileError(
            "time.ambiguous_measure_end",
            "measure-end must use the next measure at zero",
            path=path,
        )
    return _checked_add(
        span.start_quarter,
        offset,
        limits=limits,
        path=f"{path} absolute quarter",
    )


def _validated_snapshot_score(
    snapshot: ScoreSourceSnapshot,
) -> tuple[ScoreV2Document, str]:
    if type(snapshot) is not ScoreSourceSnapshot:
        raise ScoreV2TimeCompileError(
            "time.untrusted_source",
            "compile_score_v2_time requires a ScoreSourceSnapshot",
        )
    try:
        recorded_bytes = snapshot.canonical_bytes
        recorded_hash = snapshot.document_sha256
        unchanged = (
            snapshot.canonical_bytes is recorded_bytes
            and snapshot.document_sha256 == recorded_hash
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise ScoreV2TimeCompileError(
            "time.invalid_source_snapshot",
            "score source snapshot could not be read safely",
        ) from exc
    if (
        type(recorded_bytes) is not bytes
        or type(recorded_hash) is not str
        or not unchanged
    ):
        raise ScoreV2TimeCompileError(
            "time.source_identity_mismatch",
            "snapshot source generation changed while it was captured",
        )
    digest = hashlib.sha256(recorded_bytes).hexdigest()
    if digest != recorded_hash:
        raise ScoreV2TimeCompileError(
            "time.source_identity_mismatch",
            "snapshot canonical bytes and document hash do not match",
        )
    try:
        # This compiler's source contract is intentionally independent of
        # ambient ProjectLimits.  The generation was already accepted by its
        # source factory; rebinding uses the Score-v2 model's hard structural
        # caps and the exact immutable byte length instead of consulting an
        # environment which may have changed afterwards.
        trusted = snapshot_score_bytes(
            recorded_bytes,
            ProjectLimits(
                max_score_json_bytes=max(1, len(recorded_bytes)),
                max_parts=MAX_PARTS,
                max_notes=MAX_NOTES,
            ),
        )
        score = trusted.score
    except (TypeError, ValueError) as exc:
        raise ScoreV2TimeCompileError(
            "time.invalid_score_v2_source",
            "snapshot is not a valid score-v2 document",
        ) from exc
    if (
        trusted.canonical_bytes != recorded_bytes
        or trusted.document_sha256 != digest
    ):
        raise ScoreV2TimeCompileError(
            "time.source_identity_mismatch",
            "snapshot bytes are not one canonical score generation",
        )
    if type(score) is not ScoreV2Document:
        raise ScoreV2TimeCompileError(
            "time.invalid_score_v2_source",
            "snapshot is not a valid score-v2 document",
        )
    if (
        score.identity_contract != SCORE_V2_IDENTITY_CONTRACT
        or score.time_contract != SCORE_V2_TIME_CONTRACT
        or not score.has_stable_event_identity
    ):
        raise ScoreV2TimeCompileError(
            "time.unsupported_score_contract",
            "snapshot lacks the required stable-event-v2 exact-time contract",
        )
    return score, digest


class _TimeIndexJsonBudget:
    """Exact incremental bound for the canonical public index document."""

    __slots__ = ("maximum", "used", "_array_counts")

    def __init__(
        self,
        *,
        maximum: int,
        base_document: dict[str, object],
    ) -> None:
        self.maximum = maximum
        self.used = len(canonical_json_bytes(base_document))
        self._array_counts = {
            "measures": 0,
            "tempo_segments": 0,
            "meter_events": 0,
            "tempo_events": 0,
            "notes": 0,
            "events": 0,
        }
        if self.used > self.maximum:
            self._raise("time index metadata")

    def _raise(self, path: str) -> None:
        raise ScoreV2TimeCompileError(
            "time.index_too_large",
            "canonical time-index output exceeds the configured byte bound",
            path=path,
        )

    def add(
        self,
        array_name: str,
        document: dict[str, object],
        *,
        path: str,
    ) -> None:
        try:
            count = self._array_counts[array_name]
        except KeyError as exc:
            raise RuntimeError("unknown time-index output array") from exc
        size = len(canonical_json_bytes(document)) + (1 if count else 0)
        if size > self.maximum - self.used:
            self._raise(path)
        self.used += size
        self._array_counts[array_name] = count + 1


def _compile_detached_score_v2_time(
    score: ScoreV2Document,
    *,
    source_document_sha256: str,
    sample_rate: int,
    limits: ScoreV2TimeLimits,
) -> ScoreV2TimeIndex:
    """Compile a freshly parsed score; callers must bind source identity."""

    measure_spans: list[_MeasureSpan] = []
    measure_lookup: dict[str, _MeasureSpan] = {}
    score_quarters = Fraction(0, 1)
    for index, measure in enumerate(score.timeline.measures):
        duration = _from_rational(
            measure.actual_duration_quarters,
            limits=limits,
            path=f"timeline.measures[{index}].actual_duration_quarters",
        )
        end = _checked_add(
            score_quarters,
            duration,
            limits=limits,
            path=f"timeline.measures[{index}] end",
        )
        span = _MeasureSpan(measure.measure_id, index, score_quarters, end)
        measure_spans.append(span)
        measure_lookup[measure.measure_id] = span
        score_quarters = end
    frozen_measure_lookup = MappingProxyType(dict(measure_lookup))

    tempo_events = score.timeline.tempo_events
    if len(tempo_events) > limits.max_tempo_segments:
        raise ScoreV2TimeCompileError(
            "time.too_many_tempo_segments",
            "tempo segment count exceeds the configured bound",
            path="timeline.tempo_events",
        )
    tempo_quarters: list[Fraction] = []
    for index, event in enumerate(tempo_events):
        tempo_quarters.append(
            _position_quarter(
                event.at,
                measures=frozen_measure_lookup,
                limits=limits,
                path=f"timeline.tempo_events[{index}].at",
                score_duration=score_quarters,
            )
        )

    spans: list[_TempoSpan] = []
    seconds = Fraction(0, 1)
    sixty = Fraction(60, 1)
    for index, event in enumerate(tempo_events):
        start_quarter = tempo_quarters[index]
        end_quarter = (
            tempo_quarters[index + 1]
            if index + 1 < len(tempo_quarters)
            else score_quarters
        )
        quarter_bpm = _from_rational(
            event.quarter_bpm,
            limits=limits,
            path=f"timeline.tempo_events[{index}].quarter_bpm",
        )
        seconds_per_quarter = _checked_div(
            sixty,
            quarter_bpm,
            limits=limits,
            path=f"tempo segment {index} seconds_per_quarter",
        )
        quarter_length = _checked_sub(
            end_quarter,
            start_quarter,
            limits=limits,
            path=f"tempo segment {index} quarter length",
        )
        segment_seconds = _checked_mul(
            quarter_length,
            seconds_per_quarter,
            limits=limits,
            path=f"tempo segment {index} duration",
        )
        end_seconds = _checked_add(
            seconds,
            segment_seconds,
            limits=limits,
            path=f"tempo segment {index} cumulative seconds",
        )
        if end_seconds > limits.max_output_seconds:
            raise ScoreV2TimeCompileError(
                "time.duration_too_long",
                "score duration exceeds the configured output duration",
                path=f"timeline.tempo_events[{index}]",
            )
        spans.append(
            _TempoSpan(
                tempo_id=event.tempo_id,
                segment_index=index,
                start_quarter=start_quarter,
                end_quarter=end_quarter,
                quarter_bpm=quarter_bpm,
                seconds_per_quarter=seconds_per_quarter,
                start_seconds=seconds,
                end_seconds=end_seconds,
            )
        )
        seconds = end_seconds
    frozen_spans = tuple(spans)
    tempo_starts = tuple(span.start_quarter for span in frozen_spans)

    def at_quarter(quarter: Fraction, *, path: str) -> ExactTimePoint:
        return _time_at_quarter(
            quarter,
            spans=frozen_spans,
            starts=tempo_starts,
            score_duration=score_quarters,
            sample_rate=sample_rate,
            limits=limits,
            path=path,
        )

    score_duration = _point(
        score_quarters,
        seconds,
        sample_rate=sample_rate,
        limits=limits,
        path="score duration",
    )
    output_budget = _TimeIndexJsonBudget(
        maximum=limits.max_index_json_bytes,
        base_document=_time_index_document(
            source_document_sha256=source_document_sha256,
            sample_rate=sample_rate,
            score_duration=score_duration,
            measures=(),
            tempo_segments=(),
            meter_events=(),
            tempo_events=(),
            notes=(),
            events=(),
        ),
    )

    compiled_measure_items: list[CompiledMeasureTime] = []
    for span in measure_spans:
        item = CompiledMeasureTime(
            measure_id=span.measure_id,
            measure_index=span.measure_index,
            start=at_quarter(
                span.start_quarter,
                path=f"compiled measures[{span.measure_index}].start",
            ),
            end=at_quarter(
                span.end_quarter,
                path=f"compiled measures[{span.measure_index}].end",
            ),
        )
        output_budget.add(
            "measures",
            item.to_dict(),
            path=f"measures[{span.measure_index}]",
        )
        compiled_measure_items.append(item)
    compiled_measures = tuple(compiled_measure_items)

    compiled_segment_items: list[TempoSegment] = []
    for span in frozen_spans:
        item = TempoSegment(
            tempo_id=span.tempo_id,
            segment_index=span.segment_index,
            quarter_bpm=ExactFraction(span.quarter_bpm),
            seconds_per_quarter=ExactFraction(span.seconds_per_quarter),
            start=_point(
                span.start_quarter,
                span.start_seconds,
                sample_rate=sample_rate,
                limits=limits,
                path=f"compiled tempo_segments[{span.segment_index}].start",
            ),
            end=_point(
                span.end_quarter,
                span.end_seconds,
                sample_rate=sample_rate,
                limits=limits,
                path=f"compiled tempo_segments[{span.segment_index}].end",
            ),
        )
        output_budget.add(
            "tempo_segments",
            item.to_dict(),
            path=f"tempo_segments[{span.segment_index}]",
        )
        compiled_segment_items.append(item)
    compiled_segments = tuple(compiled_segment_items)

    compiled_meter_events: list[CompiledTimelineEvent] = []
    for index, event in enumerate(score.timeline.meter_events):
        quarter = _position_quarter(
            event.at,
            measures=frozen_measure_lookup,
            limits=limits,
            path=f"timeline.meter_events[{index}].at",
            score_duration=score_quarters,
        )
        item = CompiledTimelineEvent(
            kind="meter",
            subject_id=event.meter_id,
            at=at_quarter(
                quarter,
                path=f"compiled meter_events[{index}]",
            ),
            source_order=index,
        )
        output_budget.add(
            "meter_events",
            item.to_dict(),
            path=f"meter_events[{index}]",
        )
        output_budget.add(
            "events",
            item.to_dict(),
            path=f"events meter[{index}]",
        )
        compiled_meter_events.append(item)

    compiled_tempo_events: list[CompiledTimelineEvent] = []
    for index, (event, span) in enumerate(zip(tempo_events, frozen_spans)):
        item = CompiledTimelineEvent(
            kind="tempo",
            subject_id=event.tempo_id,
            at=_point(
                span.start_quarter,
                span.start_seconds,
                sample_rate=sample_rate,
                limits=limits,
                path=f"compiled tempo_events[{index}]",
            ),
            source_order=index,
        )
        output_budget.add(
            "tempo_events",
            item.to_dict(),
            path=f"tempo_events[{index}]",
        )
        output_budget.add(
            "events",
            item.to_dict(),
            path=f"events tempo[{index}]",
        )
        compiled_tempo_events.append(item)

    compiled_notes: list[CompiledNoteTime] = []
    note_boundaries: list[CompiledTimelineEvent] = []
    source_order = 0
    for part_index, part in enumerate(score.parts):
        for note_index, note in enumerate(part.notes):
            start_quarter = _position_quarter(
                note.position,
                measures=frozen_measure_lookup,
                limits=limits,
                path=f"parts[{part_index}].notes[{note_index}].position",
                score_duration=score_quarters,
            )
            duration = _from_rational(
                note.duration_quarters,
                limits=limits,
                path=f"parts[{part_index}].notes[{note_index}].duration_quarters",
            )
            end_quarter = _checked_add(
                start_quarter,
                duration,
                limits=limits,
                path=f"note {note.event_id!r} end quarter",
            )
            if end_quarter > score_quarters:
                raise ScoreV2TimeCompileError(
                    "time.note_out_of_range",
                    "note extends beyond the score timeline",
                    path=f"parts[{part_index}].notes[{note_index}]",
                )
            start = at_quarter(
                start_quarter,
                path=f"compiled note {note.event_id!r} start",
            )
            end = at_quarter(
                end_quarter,
                path=f"compiled note {note.event_id!r} end",
            )
            compiled_note = CompiledNoteTime(
                part_id=part.part_id,
                event_id=note.event_id,
                start=start,
                end=end,
                source_order=source_order,
            )
            start_boundary = CompiledTimelineEvent(
                kind="note_start",
                subject_id=note.event_id,
                part_id=part.part_id,
                at=start,
                source_order=source_order,
            )
            end_boundary = CompiledTimelineEvent(
                kind="note_end",
                subject_id=note.event_id,
                part_id=part.part_id,
                at=end,
                source_order=source_order,
            )
            output_budget.add(
                "notes",
                compiled_note.to_dict(),
                path=f"notes[{source_order}]",
            )
            output_budget.add(
                "events",
                start_boundary.to_dict(),
                path=f"events note_start[{source_order}]",
            )
            output_budget.add(
                "events",
                end_boundary.to_dict(),
                path=f"events note_end[{source_order}]",
            )
            compiled_notes.append(compiled_note)
            note_boundaries.extend((start_boundary, end_boundary))
            source_order += 1

    all_events = (
        compiled_meter_events + compiled_tempo_events + note_boundaries
    )
    all_events.sort(
        key=lambda event: (
            event.at.sample.resolved_sample,
            event.at.seconds,
            _EVENT_PRIORITY[event.kind],
            event.source_order,
            event.subject_id,
        )
    )
    meter_event_tuple = tuple(compiled_meter_events)
    tempo_event_tuple = tuple(compiled_tempo_events)
    note_tuple = tuple(compiled_notes)
    event_tuple = tuple(all_events)
    artifact_document = _time_index_document(
        source_document_sha256=source_document_sha256,
        sample_rate=sample_rate,
        score_duration=score_duration,
        measures=compiled_measures,
        tempo_segments=compiled_segments,
        meter_events=meter_event_tuple,
        tempo_events=tempo_event_tuple,
        notes=note_tuple,
        events=event_tuple,
    )
    artifact_bytes = canonical_json_bytes(artifact_document)
    if len(artifact_bytes) != output_budget.used:
        raise RuntimeError("time-index canonical byte accounting mismatch")
    artifact_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    resolver_measure_lookup = MappingProxyType(
        {
            span.measure_id: _ResolverMeasureSpan(
                measure_id=span.measure_id,
                measure_index=span.measure_index,
                start_quarter=ExactFraction(span.start_quarter),
                end_quarter=ExactFraction(span.end_quarter),
            )
            for span in measure_spans
        }
    )
    resolver_spans = tuple(
        _ResolverTempoSpan(
            tempo_id=span.tempo_id,
            segment_index=span.segment_index,
            start_quarter=ExactFraction(span.start_quarter),
            end_quarter=ExactFraction(span.end_quarter),
            quarter_bpm=ExactFraction(span.quarter_bpm),
            seconds_per_quarter=ExactFraction(span.seconds_per_quarter),
            start_seconds=ExactFraction(span.start_seconds),
            end_seconds=ExactFraction(span.end_seconds),
        )
        for span in frozen_spans
    )
    resolver_starts = tuple(span.start_quarter for span in resolver_spans)
    limits_signature = (
        limits.max_fraction_bits,
        limits.max_tempo_segments,
        limits.max_output_seconds,
        limits.max_sample_index,
        limits.max_index_json_bytes,
    )
    resolver_identity: tuple[object, ...] = (
        resolver_measure_lookup,
        resolver_spans,
        resolver_starts,
        limits,
        limits_signature,
        source_document_sha256,
        sample_rate,
        score_duration,
        compiled_measures,
        compiled_segments,
        meter_event_tuple,
        tempo_event_tuple,
        note_tuple,
        event_tuple,
        artifact_bytes,
        artifact_sha256,
    )
    return ScoreV2TimeIndex(
        source_document_sha256=source_document_sha256,
        sample_rate=sample_rate,
        score_duration=score_duration,
        measures=compiled_measures,
        tempo_segments=compiled_segments,
        meter_events=meter_event_tuple,
        tempo_events=tempo_event_tuple,
        notes=note_tuple,
        events=event_tuple,
        _measure_lookup=resolver_measure_lookup,
        _tempo_spans=resolver_spans,
        _tempo_starts=resolver_starts,
        _limits=limits,
        _canonical_bytes=artifact_bytes,
        _artifact_sha256=artifact_sha256,
        _resolver_identity=resolver_identity,
    )


def compile_score_v2_time(
    snapshot: ScoreSourceSnapshot,
    *,
    sample_rate: int,
    limits: ScoreV2TimeLimits | None = None,
) -> ScoreV2TimeIndex:
    """Compile one trusted score-v2 snapshot into an exact-time index.

    This is a coordinate/timing compilation primitive only.  It does not
    execute form, apply performance realization, build a conductor plan, or
    authorize rendering.  Canonical index output has an independent byte
    budget because each source note produces a note record and two boundary
    events, all carrying explicit exact/sample-grid evidence.
    """

    if limits is None:
        active_limits = ScoreV2TimeLimits()
    elif type(limits) is not ScoreV2TimeLimits:
        raise TypeError("limits must be a ScoreV2TimeLimits")
    else:
        # Reconstruct rather than trusting frozen-dataclass convention: a
        # caller can deliberately bypass it with object.__setattr__.
        active_limits = ScoreV2TimeLimits(
            max_fraction_bits=limits.max_fraction_bits,
            max_tempo_segments=limits.max_tempo_segments,
            max_output_seconds=limits.max_output_seconds,
            max_sample_index=limits.max_sample_index,
            max_index_json_bytes=limits.max_index_json_bytes,
        )
    if (
        type(sample_rate) is not int
        or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
    ):
        raise ScoreV2TimeCompileError(
            "time.invalid_sample_rate",
            f"sample_rate must be an integer in {MIN_SAMPLE_RATE}..{MAX_SAMPLE_RATE}",
        )
    score, source_hash = _validated_snapshot_score(snapshot)
    return _compile_detached_score_v2_time(
        score,
        source_document_sha256=source_hash,
        sample_rate=sample_rate,
        limits=active_limits,
    )


__all__ = [
    "DEFAULT_MAX_TIME_INDEX_JSON_BYTES",
    "MAX_TIME_INDEX_JSON_BYTES",
    "MAX_SAMPLE_RATE",
    "MIN_SAMPLE_RATE",
    "SAMPLE_ROUNDING_MODE",
    "TIME_INDEX_KIND",
    "TIME_INDEX_SCHEMA_VERSION",
    "CompiledMeasureTime",
    "CompiledNoteTime",
    "CompiledTimelineEvent",
    "ExactFraction",
    "ExactTimePoint",
    "SampleResolution",
    "ScoreV2TimeCompileError",
    "ScoreV2TimeIndex",
    "ScoreV2TimeLimits",
    "TempoSegment",
    "compile_score_v2_time",
]
