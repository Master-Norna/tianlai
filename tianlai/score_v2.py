"""Exact, identity-stable Tianlai score-v2 document model.

This module is deliberately isolated from the legacy floating-point score
model.  Score-v2 JSON uses :class:`Rational` values for musical time, tempo,
and sounding pitch so parsing never loses authorial precision.

``to_dict()`` emits a canonical typed representation: explicitly empty
optional arrays are normalized to omission.  Raw source-document identity,
including such presence distinctions, belongs to the source snapshot/hash
layer rather than this semantic model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import total_ordering
from fractions import Fraction
from math import gcd, isfinite
from typing import Any, TypeAlias, TypeVar

from .canonical_json import canonical_json_bytes


MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_RATIONAL_DENOMINATOR = 1_000_000
MAX_ID_CHARACTERS = 256
MAX_ID_UTF8_BYTES = MAX_ID_CHARACTERS * 4
MAX_TEXT_CHARACTERS = 4_096
MAX_TEXT_UTF8_BYTES = 16_384
MAX_MEASURES = 100_000
MAX_PARTS = 256
MAX_NOTES = 250_000
MAX_RELATIONS = 250_000
MAX_METER_EVENTS = 100_000
MAX_METER_GROUPS = 250_000
MAX_TEMPO_EVENTS = 100_000
MAX_ARTICULATIONS_PER_NOTE = 256
MAX_ARTICULATIONS = 250_000
MAX_EXTENSIONS = 4_096
MAX_EXTENSION_PAYLOAD_DEPTH = 32
MAX_EXTENSION_PAYLOAD_NODES = 100_000
MAX_EXTENSION_PAYLOAD_CONTAINER_ITEMS = 10_000
MAX_EXTENSION_PAYLOAD_UTF8_BYTES = 1_000_000
MAX_TIMELINE_COMMON_DENOMINATOR_BITS = 4_096
MAX_TIMELINE_CUMULATIVE_POSITION_BITS = (
    MAX_TIMELINE_COMMON_DENOMINATOR_BITS + 128
)

SCORE_RENDER_PROJECTION_VERSION = 1
SCORE_RENDER_PROJECTION_DOMAIN = b"tianlai.score-render-projection-v1\0"
SCORE_V2_IDENTITY_CONTRACT = "stable-event-v2"
SCORE_V2_TIME_CONTRACT = "rational-measure-offset-v2"

# No audible score-v2 extensions are implemented by this isolated core yet.
# The tuple shape leaves a deliberately explicit compatibility point for a
# future namespace/version implementation.
SUPPORTED_SCORE_V2_EXTENSIONS: frozenset[tuple[str, int]] = frozenset()
_DYNAMIC_MARKS = frozenset(("ppp", "pp", "p", "mp", "mf", "f", "ff", "fff"))


def _integral_component(value: object, *, name: str) -> int:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, float) and (not isfinite(value) or not value.is_integer()):
        raise ValueError(f"{name} must be an integer")
    return int(value)


def _bounded_utf8(
    value: object,
    *,
    name: str,
    maximum: int,
    maximum_characters: int,
    nonblank: bool = False,
) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    if nonblank and not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    if len(value) > maximum_characters:
        raise ValueError(
            f"{name} exceeds the {maximum_characters}-character bound"
        )
    # Every Unicode code point needs at least one UTF-8 byte, so this rejects
    # obviously oversized strings without first allocating another huge bytes
    # object.  Encoding below still rejects lone surrogate code points.
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds the {maximum}-byte UTF-8 bound")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must contain valid Unicode") from exc
    if size > maximum:
        raise ValueError(f"{name} exceeds the {maximum}-byte UTF-8 bound")
    return value


def _identifier(value: object, *, name: str) -> str:
    return _bounded_utf8(
        value,
        name=name,
        maximum=MAX_ID_UTF8_BYTES,
        maximum_characters=MAX_ID_CHARACTERS,
        nonblank=True,
    )


def _bounded_text(
    value: object,
    *,
    name: str,
    nonblank: bool = False,
) -> str:
    return _bounded_utf8(
        value,
        name=name,
        maximum=MAX_TEXT_UTF8_BYTES,
        maximum_characters=MAX_TEXT_CHARACTERS,
        nonblank=nonblank,
    )


def _notation_text(value: object, *, name: str) -> str:
    return _bounded_utf8(
        value,
        name=name,
        maximum=MAX_ID_UTF8_BYTES,
        maximum_characters=MAX_ID_CHARACTERS,
        nonblank=True,
    )


def _dynamic(value: object, *, name: str) -> str:
    mark = _notation_text(value, name=name)
    if mark not in _DYNAMIC_MARKS:
        raise ValueError(f"{name} must be a supported dynamic mark")
    return mark


def _positive_integer(value: object, *, name: str) -> int:
    number = _integral_component(value, name=name)
    if abs(number) > MAX_SAFE_INTEGER:
        raise ValueError(f"{name} exceeds the JSON safe-integer bound")
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _safe_integer(value: object, *, name: str) -> int:
    number = _integral_component(value, name=name)
    if abs(number) > MAX_SAFE_INTEGER:
        raise ValueError(f"{name} exceeds the JSON safe-integer bound")
    return number


@total_ordering
@dataclass(frozen=True, slots=True)
class Rational:
    numerator: int
    denominator: int = 1

    def __post_init__(self) -> None:
        numerator = _integral_component(self.numerator, name="numerator")
        denominator = _integral_component(self.denominator, name="denominator")
        if abs(numerator) > MAX_SAFE_INTEGER:
            raise ValueError("numerator exceeds the JSON safe-integer bound")
        if abs(denominator) > MAX_SAFE_INTEGER:
            raise ValueError("denominator exceeds the JSON safe-integer bound")
        if denominator <= 0:
            raise ValueError("denominator must be positive")
        if denominator > MAX_RATIONAL_DENOMINATOR:
            raise ValueError("denominator exceeds the supported bound")
        divisor = gcd(abs(numerator), denominator)
        object.__setattr__(self, "numerator", numerator // divisor)
        object.__setattr__(self, "denominator", denominator // divisor)

    def to_dict(self) -> dict[str, int]:
        return {"numerator": self.numerator, "denominator": self.denominator}

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Rational):
            return NotImplemented
        return (
            self.numerator * other.denominator
            < other.numerator * self.denominator
        )

    def as_fraction(self) -> Fraction:
        return Fraction(self.numerator, self.denominator)


@dataclass(frozen=True, slots=True)
class ScorePosition:
    measure_id: str
    offset_quarters: Rational

    def __post_init__(self) -> None:
        _identifier(self.measure_id, name="measure_id")
        if type(self.offset_quarters) is not Rational:
            raise ValueError("offset_quarters must be a Rational")
        if self.offset_quarters.numerator < 0:
            raise ValueError("offset_quarters must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "measure_id": self.measure_id,
            "offset_quarters": self.offset_quarters.to_dict(),
        }


JSONPrimitive: TypeAlias = None | bool | int | float | str


@dataclass(frozen=True, slots=True)
class FrozenJSONArray:
    values: tuple["FrozenJSONValue", ...]

    def __post_init__(self) -> None:
        if type(self.values) is not tuple:
            raise ValueError("JSON array values must be an immutable tuple")
        if len(self.values) > MAX_EXTENSION_PAYLOAD_CONTAINER_ITEMS:
            raise ValueError("JSON array exceeds the extension payload bound")
        for value in self.values:
            _validate_frozen_json_scalar_or_container(value)


@dataclass(frozen=True, slots=True)
class FrozenJSONObject:
    members: tuple[tuple[str, "FrozenJSONValue"], ...]

    def __post_init__(self) -> None:
        if type(self.members) is not tuple:
            raise ValueError("JSON object members must be an immutable tuple")
        if len(self.members) > MAX_EXTENSION_PAYLOAD_CONTAINER_ITEMS:
            raise ValueError("JSON object exceeds the extension payload bound")
        seen: set[str] = set()
        normalized: list[tuple[str, FrozenJSONValue]] = []
        for member in self.members:
            if type(member) is not tuple or len(member) != 2:
                raise ValueError("JSON object members must be key/value pairs")
            key, value = member
            key = _bounded_text(key, name="extension payload object key")
            if key in seen:
                raise ValueError(f"duplicate extension payload key: {key!r}")
            seen.add(key)
            _validate_frozen_json_scalar_or_container(value)
            normalized.append((key, value))
        normalized.sort(key=lambda item: item[0])
        object.__setattr__(self, "members", tuple(normalized))


FrozenJSONValue: TypeAlias = JSONPrimitive | FrozenJSONArray | FrozenJSONObject
MutableJSONValue: TypeAlias = (
    JSONPrimitive | list["MutableJSONValue"] | dict[str, "MutableJSONValue"]
)


def _validate_frozen_json_scalar_or_container(value: object) -> None:
    if value is None or type(value) in (bool, int, str):
        if type(value) is int and abs(value) > MAX_SAFE_INTEGER:
            raise ValueError("extension payload integer exceeds the JSON safe bound")
        if type(value) is str:
            _bounded_text(value, name="extension payload string")
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("extension payload numbers must be finite JSON numbers")
        if value.is_integer() and abs(value) > MAX_SAFE_INTEGER:
            raise ValueError(
                "extension payload integral number exceeds the JSON safe bound"
            )
        return
    if type(value) in (FrozenJSONArray, FrozenJSONObject):
        return
    raise ValueError("extension payload must contain only JSON values")


@dataclass(slots=True)
class _ExtensionPayloadBudget:
    nodes: int = 0
    utf8_bytes: int = 0
    canonical_bytes: int = 0


def _freeze_json_payload(
    value: object,
    *,
    budget: _ExtensionPayloadBudget | None = None,
) -> FrozenJSONValue:
    aggregate = budget if budget is not None else _ExtensionPayloadBudget()
    local_nodes = 0
    local_utf8_bytes = 0
    active_containers: set[int] = set()

    def consume_utf8(size: int) -> None:
        nonlocal local_utf8_bytes
        local_utf8_bytes += size
        aggregate.utf8_bytes += size
        if (
            local_utf8_bytes > MAX_EXTENSION_PAYLOAD_UTF8_BYTES
            or aggregate.utf8_bytes > MAX_EXTENSION_PAYLOAD_UTF8_BYTES
        ):
            raise ValueError("extension payload exceeds the UTF-8 size bound")

    def freeze(candidate: object, *, depth: int) -> FrozenJSONValue:
        nonlocal local_nodes
        local_nodes += 1
        aggregate.nodes += 1
        if (
            local_nodes > MAX_EXTENSION_PAYLOAD_NODES
            or aggregate.nodes > MAX_EXTENSION_PAYLOAD_NODES
        ):
            raise ValueError("extension payload exceeds the JSON node bound")
        if depth > MAX_EXTENSION_PAYLOAD_DEPTH:
            raise ValueError("extension payload exceeds the JSON depth bound")
        if candidate is None or type(candidate) in (bool, int, float, str):
            _validate_frozen_json_scalar_or_container(candidate)
            if type(candidate) is str:
                consume_utf8(len(candidate.encode("utf-8")))
            return candidate  # type: ignore[return-value]
        if type(candidate) is list:
            if len(candidate) > MAX_EXTENSION_PAYLOAD_CONTAINER_ITEMS:
                raise ValueError("extension payload JSON array is too large")
            identity = id(candidate)
            if identity in active_containers:
                raise ValueError("extension payload must not contain cycles")
            active_containers.add(identity)
            try:
                return FrozenJSONArray(
                    tuple(freeze(item, depth=depth + 1) for item in candidate)
                )
            finally:
                active_containers.remove(identity)
        if type(candidate) is dict:
            if len(candidate) > MAX_EXTENSION_PAYLOAD_CONTAINER_ITEMS:
                raise ValueError("extension payload JSON object is too large")
            identity = id(candidate)
            if identity in active_containers:
                raise ValueError("extension payload must not contain cycles")
            active_containers.add(identity)
            try:
                members: list[tuple[str, FrozenJSONValue]] = []
                for key, item in candidate.items():
                    key = _bounded_text(
                        key,
                        name="extension payload object key",
                    )
                    consume_utf8(len(key.encode("utf-8")))
                    members.append((key, freeze(item, depth=depth + 1)))
                return FrozenJSONObject(tuple(members))
            finally:
                active_containers.remove(identity)
        raise ValueError("extension payload must contain only JSON values")

    return freeze(value, depth=0)


def _thaw_json_payload(value: FrozenJSONValue) -> MutableJSONValue:
    if type(value) is FrozenJSONArray:
        return [_thaw_json_payload(item) for item in value.values]
    if type(value) is FrozenJSONObject:
        return {
            key: _thaw_json_payload(item)
            for key, item in value.members
        }
    return value


T = TypeVar("T")


def _typed_tuple(
    value: object,
    expected_type: type[T],
    *,
    name: str,
    maximum: int,
    minimum: int = 0,
) -> tuple[T, ...]:
    if type(value) is not tuple:
        raise ValueError(f"{name} must be an immutable tuple")
    if len(value) < minimum:
        raise ValueError(f"{name} must contain at least {minimum} item(s)")
    if len(value) > maximum:
        raise ValueError(f"{name} exceeds the {maximum}-item bound")
    for index, item in enumerate(value):
        if type(item) is not expected_type:
            raise ValueError(
                f"{name}[{index}] must be a {expected_type.__name__}"
            )
    return value


def _validate_timeline_rational_complexity(
    measures: tuple["ScoreMeasure", ...],
) -> None:
    """Bound the LCM needed by cumulative exact measure positions.

    Individual rational bounds do not prevent many pairwise-coprime measure
    denominators from making every cumulative ``Fraction`` retain a rapidly
    growing big integer.  Check the common denominator before constructing any
    of those prefix sums so a syntactically small timeline cannot amplify into
    a multi-gigabyte in-memory index.
    """

    common_denominator = 1
    for index, measure in enumerate(measures):
        denominator = measure.actual_duration_quarters.denominator
        new_factor = denominator // gcd(common_denominator, denominator)
        # Multiplication of values at this bound is cheap.  The preliminary
        # bit check avoids ever constructing a clearly over-budget product;
        # the exact check handles the one-bit ambiguity in bit-length sums.
        if (
            common_denominator.bit_length() + new_factor.bit_length() - 1
            > MAX_TIMELINE_COMMON_DENOMINATOR_BITS
        ):
            raise ValueError(
                "timeline measure denominators exceed the aggregate exact "
                f"rational complexity bound at measures[{index}]"
            )
        candidate = common_denominator * new_factor
        if candidate.bit_length() > MAX_TIMELINE_COMMON_DENOMINATOR_BITS:
            raise ValueError(
                "timeline measure denominators exceed the aggregate exact "
                f"rational complexity bound at measures[{index}]"
            )
        common_denominator = candidate


def _bounded_timeline_fraction(value: Fraction, *, path: str) -> Fraction:
    if (
        abs(value.numerator).bit_length()
        > MAX_TIMELINE_CUMULATIVE_POSITION_BITS
        or value.denominator.bit_length()
        > MAX_TIMELINE_CUMULATIVE_POSITION_BITS
    ):
        raise ValueError(
            f"{path} exceeds the aggregate exact rational complexity bound"
        )
    return value


@dataclass(frozen=True, slots=True)
class ScoreMeasure:
    measure_id: str
    actual_duration_quarters: Rational

    def __post_init__(self) -> None:
        _identifier(self.measure_id, name="measure_id")
        if type(self.actual_duration_quarters) is not Rational:
            raise ValueError("actual_duration_quarters must be a Rational")
        if self.actual_duration_quarters.numerator <= 0:
            raise ValueError("actual_duration_quarters must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "measure_id": self.measure_id,
            "actual_duration_quarters": self.actual_duration_quarters.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MeterEvent:
    meter_id: str
    at: ScorePosition
    groups: tuple[int, ...]
    beat_unit: int

    def __post_init__(self) -> None:
        _identifier(self.meter_id, name="meter_id")
        if type(self.at) is not ScorePosition:
            raise ValueError("meter event at must be a ScorePosition")
        if type(self.groups) is not tuple or not self.groups:
            raise ValueError("meter groups must be a non-empty immutable tuple")
        if len(self.groups) > 64:
            raise ValueError("meter groups exceed the supported bound")
        groups = tuple(
            _positive_integer(group, name="meter group") for group in self.groups
        )
        beat_unit = _positive_integer(self.beat_unit, name="beat_unit")
        if beat_unit > 64 or beat_unit & (beat_unit - 1):
            raise ValueError("beat_unit must be a supported power of two")
        object.__setattr__(self, "groups", groups)
        object.__setattr__(self, "beat_unit", beat_unit)

    def to_dict(self) -> dict[str, object]:
        return {
            "meter_id": self.meter_id,
            "at": self.at.to_dict(),
            "groups": list(self.groups),
            "beat_unit": self.beat_unit,
        }


@dataclass(frozen=True, slots=True)
class TempoEvent:
    tempo_id: str
    at: ScorePosition
    quarter_bpm: Rational

    def __post_init__(self) -> None:
        _identifier(self.tempo_id, name="tempo_id")
        if type(self.at) is not ScorePosition:
            raise ValueError("tempo event at must be a ScorePosition")
        if type(self.quarter_bpm) is not Rational:
            raise ValueError("quarter_bpm must be a Rational")
        if self.quarter_bpm.numerator <= 0:
            raise ValueError("quarter_bpm must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "tempo_id": self.tempo_id,
            "at": self.at.to_dict(),
            "quarter_bpm": self.quarter_bpm.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ScoreTimeline:
    measures: tuple[ScoreMeasure, ...]
    meter_events: tuple[MeterEvent, ...]
    tempo_events: tuple[TempoEvent, ...]

    def __post_init__(self) -> None:
        measures = _typed_tuple(
            self.measures,
            ScoreMeasure,
            name="timeline.measures",
            maximum=MAX_MEASURES,
            minimum=1,
        )
        meter_events = _typed_tuple(
            self.meter_events,
            MeterEvent,
            name="timeline.meter_events",
            maximum=MAX_METER_EVENTS,
            minimum=1,
        )
        tempo_events = _typed_tuple(
            self.tempo_events,
            TempoEvent,
            name="timeline.tempo_events",
            maximum=MAX_TEMPO_EVENTS,
            minimum=1,
        )
        if sum(len(event.groups) for event in meter_events) > MAX_METER_GROUPS:
            raise ValueError("timeline meter groups exceed the aggregate bound")
        _validate_timeline_rational_complexity(measures)
        measure_lookup: dict[str, tuple[int, Fraction, Fraction]] = {}
        start = Fraction(0)
        for index, measure in enumerate(measures):
            if measure.measure_id in measure_lookup:
                raise ValueError(f"duplicate measure_id: {measure.measure_id!r}")
            duration = measure.actual_duration_quarters.as_fraction()
            measure_lookup[measure.measure_id] = (index, start, duration)
            start = _bounded_timeline_fraction(
                start + duration,
                path=f"timeline cumulative position after measures[{index}]",
            )

        for label, events, id_name in (
            ("meter_events", meter_events, "meter_id"),
            ("tempo_events", tempo_events, "tempo_id"),
        ):
            seen_ids: set[str] = set()
            previous: Fraction | None = None
            for index, event in enumerate(events):
                event_id = getattr(event, id_name)
                if event_id in seen_ids:
                    raise ValueError(f"duplicate {id_name}: {event_id!r}")
                seen_ids.add(event_id)
                absolute = _absolute_position(
                    event.at,
                    measure_lookup,
                    path=f"timeline.{label}[{index}].at",
                )
                if index == 0 and absolute != 0:
                    raise ValueError(
                        f"timeline.{label}[0] must begin at the first measure "
                        "with zero offset"
                    )
                if (
                    label == "meter_events"
                    and event.at.offset_quarters != Rational(0)
                ):
                    raise ValueError(
                        f"timeline.{label}[{index}] must occur at a measure "
                        "boundary with zero offset"
                    )
                if previous is not None and absolute <= previous:
                    raise ValueError(
                        f"timeline.{label} must be in exact increasing position order"
                    )
                previous = absolute

    def to_dict(self) -> dict[str, object]:
        return {
            "measures": [measure.to_dict() for measure in self.measures],
            "meter_events": [event.to_dict() for event in self.meter_events],
            "tempo_events": [event.to_dict() for event in self.tempo_events],
        }


@dataclass(frozen=True, slots=True)
class ScoreTuning:
    tuning_id: str
    system: str
    divisions_per_octave: int
    reference_midi_note: Rational
    reference_frequency_hz: Rational

    def __post_init__(self) -> None:
        _identifier(self.tuning_id, name="tuning_id")
        _bounded_text(self.system, name="tuning system", nonblank=True)
        if self.system != "equal_temperament":
            raise ValueError("tuning system must be 'equal_temperament'")
        divisions = _positive_integer(
            self.divisions_per_octave,
            name="divisions_per_octave",
        )
        if divisions != 12:
            raise ValueError("divisions_per_octave must be 12")
        object.__setattr__(self, "divisions_per_octave", divisions)
        if type(self.reference_midi_note) is not Rational:
            raise ValueError("reference_midi_note must be a Rational")
        if type(self.reference_frequency_hz) is not Rational:
            raise ValueError("reference_frequency_hz must be a Rational")
        if self.reference_frequency_hz.numerator <= 0:
            raise ValueError("reference_frequency_hz must be positive")

    def to_dict(self) -> dict[str, object]:
        return {
            "tuning_id": self.tuning_id,
            "system": self.system,
            "divisions_per_octave": self.divisions_per_octave,
            "reference_midi_note": self.reference_midi_note.to_dict(),
            "reference_frequency_hz": self.reference_frequency_hz.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class WrittenPitch:
    step: str
    alter: Rational
    octave: int
    accidental: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.step, name="written pitch step", nonblank=True)
        if self.step not in frozenset("ABCDEFG"):
            raise ValueError("written pitch step must be one of A-G")
        if type(self.alter) is not Rational:
            raise ValueError("written pitch alter must be a Rational")
        object.__setattr__(
            self,
            "octave",
            _safe_integer(self.octave, name="written pitch octave"),
        )
        if self.accidental is not None:
            _notation_text(
                self.accidental,
                name="written pitch accidental",
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "step": self.step,
            "alter": self.alter.to_dict(),
            "octave": self.octave,
        }
        if self.accidental is not None:
            result["accidental"] = self.accidental
        return result


@dataclass(frozen=True, slots=True)
class SoundingPitch:
    midi_note: Rational

    def __post_init__(self) -> None:
        if type(self.midi_note) is not Rational:
            raise ValueError("sounding pitch midi_note must be a Rational")

    def to_dict(self) -> dict[str, object]:
        return {"midi_note": self.midi_note.to_dict()}


@dataclass(frozen=True, slots=True)
class ScoreNoteV2:
    event_id: str
    position: ScorePosition
    duration_quarters: Rational
    written_pitch: WrittenPitch
    sounding_pitch: SoundingPitch
    dynamic: str | None = None
    articulations: tuple[str, ...] = ()
    staff: int | None = None
    voice: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.event_id, name="event_id")
        if type(self.position) is not ScorePosition:
            raise ValueError("note position must be a ScorePosition")
        if type(self.duration_quarters) is not Rational:
            raise ValueError("duration_quarters must be a Rational")
        if self.duration_quarters.numerator <= 0:
            raise ValueError("duration_quarters must be positive")
        if type(self.written_pitch) is not WrittenPitch:
            raise ValueError("written_pitch must be a WrittenPitch")
        if type(self.sounding_pitch) is not SoundingPitch:
            raise ValueError("sounding_pitch must be a SoundingPitch")
        if self.dynamic is not None:
            _dynamic(self.dynamic, name="note dynamic")
        if type(self.articulations) is not tuple:
            raise ValueError("articulations must be an immutable tuple")
        if len(self.articulations) > MAX_ARTICULATIONS_PER_NOTE:
            raise ValueError("articulations exceed the supported bound")
        for index, articulation in enumerate(self.articulations):
            _notation_text(
                articulation,
                name=f"articulations[{index}]",
            )
        if len(set(self.articulations)) != len(self.articulations):
            raise ValueError("articulations must not contain duplicates")
        if self.staff is not None:
            object.__setattr__(
                self,
                "staff",
                _positive_integer(self.staff, name="note staff"),
            )
        if self.voice is not None:
            _notation_text(self.voice, name="note voice")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "event_id": self.event_id,
            "position": self.position.to_dict(),
            "duration_quarters": self.duration_quarters.to_dict(),
            "written_pitch": self.written_pitch.to_dict(),
            "sounding_pitch": self.sounding_pitch.to_dict(),
        }
        if self.dynamic is not None:
            result["dynamic"] = self.dynamic
        if self.articulations:
            result["articulations"] = list(self.articulations)
        if self.staff is not None:
            result["staff"] = self.staff
        if self.voice is not None:
            result["voice"] = self.voice
        return result


@dataclass(frozen=True, slots=True)
class ScorePartV2:
    part_id: str
    notes: tuple[ScoreNoteV2, ...]
    name: str | None = None
    default_dynamic: str | None = None
    default_articulation: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.part_id, name="part_id")
        _typed_tuple(
            self.notes,
            ScoreNoteV2,
            name=f"part {self.part_id!r} notes",
            maximum=MAX_NOTES,
        )
        if self.name is not None:
            _bounded_text(self.name, name="part name")
        if self.default_dynamic is not None:
            _dynamic(
                self.default_dynamic,
                name="part default_dynamic",
            )
        if self.default_articulation is not None:
            _notation_text(
                self.default_articulation,
                name="part default_articulation",
            )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"part_id": self.part_id}
        if self.name is not None:
            result["name"] = self.name
        if self.default_dynamic is not None:
            result["default_dynamic"] = self.default_dynamic
        if self.default_articulation is not None:
            result["default_articulation"] = self.default_articulation
        result["notes"] = [note.to_dict() for note in self.notes]
        return result


@dataclass(frozen=True, slots=True)
class ScoreTie:
    tie_id: str
    from_event_id: str
    to_event_id: str

    def __post_init__(self) -> None:
        _identifier(self.tie_id, name="tie_id")
        _identifier(self.from_event_id, name="from_event_id")
        _identifier(self.to_event_id, name="to_event_id")
        if self.from_event_id == self.to_event_id:
            raise ValueError("a tie cannot reference the same event twice")

    def to_dict(self) -> dict[str, str]:
        return {
            "tie_id": self.tie_id,
            "from_event_id": self.from_event_id,
            "to_event_id": self.to_event_id,
        }


@dataclass(frozen=True, slots=True)
class ScorePhrase:
    phrase_id: str
    part_id: str
    start: ScorePosition
    end: ScorePosition

    def __post_init__(self) -> None:
        _identifier(self.phrase_id, name="phrase_id")
        _identifier(self.part_id, name="phrase part_id")
        if type(self.start) is not ScorePosition:
            raise ValueError("phrase start must be a ScorePosition")
        if type(self.end) is not ScorePosition:
            raise ValueError("phrase end must be a ScorePosition")

    def to_dict(self) -> dict[str, object]:
        return {
            "phrase_id": self.phrase_id,
            "part_id": self.part_id,
            "start": self.start.to_dict(),
            "end": self.end.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ScoreForm:
    mode: str = "linear"

    def __post_init__(self) -> None:
        _bounded_text(self.mode, name="score form mode", nonblank=True)
        if self.mode != "linear":
            raise ValueError("score form mode must be 'linear'")

    def to_dict(self) -> dict[str, str]:
        return {"mode": "linear"}


@dataclass(frozen=True, slots=True)
class ScoreExtension:
    namespace: str
    version: int
    required: bool
    audible: bool
    payload: FrozenJSONValue
    _payload_nodes: int = field(init=False, repr=False, compare=False)
    _payload_canonical_bytes: int = field(
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        _identifier(self.namespace, name="extension namespace")
        version = _positive_integer(self.version, name="extension version")
        object.__setattr__(self, "version", version)
        if type(self.required) is not bool:
            raise ValueError("extension required must be a boolean")
        if type(self.audible) is not bool:
            raise ValueError("extension audible must be a boolean")
        if (
            (self.namespace, self.version) not in SUPPORTED_SCORE_V2_EXTENSIONS
            and (self.required or self.audible)
        ):
            raise ValueError(
                "unknown required or audible score extension is not supported"
            )
        payload_nodes, payload_bytes = _validate_complete_frozen_payload(
            self.payload
        )
        object.__setattr__(self, "_payload_nodes", payload_nodes)
        object.__setattr__(self, "_payload_canonical_bytes", payload_bytes)

    def to_dict(self) -> dict[str, object]:
        return {
            "namespace": self.namespace,
            "version": self.version,
            "required": self.required,
            "audible": self.audible,
            "payload": _thaw_json_payload(self.payload),
        }


def _validate_complete_frozen_payload(
    value: FrozenJSONValue,
) -> tuple[int, int]:
    nodes = 0
    utf8_bytes = 0
    active_containers: set[int] = set()

    def walk(candidate: FrozenJSONValue, *, depth: int) -> None:
        nonlocal nodes, utf8_bytes
        nodes += 1
        if nodes > MAX_EXTENSION_PAYLOAD_NODES:
            raise ValueError("extension payload exceeds the JSON node bound")
        if depth > MAX_EXTENSION_PAYLOAD_DEPTH:
            raise ValueError("extension payload exceeds the JSON depth bound")
        _validate_frozen_json_scalar_or_container(candidate)
        if type(candidate) is str:
            utf8_bytes += len(candidate.encode("utf-8"))
            if utf8_bytes > MAX_EXTENSION_PAYLOAD_UTF8_BYTES:
                raise ValueError("extension payload exceeds the UTF-8 size bound")
        if type(candidate) in (FrozenJSONArray, FrozenJSONObject):
            identity = id(candidate)
            if identity in active_containers:
                raise ValueError("extension payload must not contain cycles")
            active_containers.add(identity)
            try:
                if type(candidate) is FrozenJSONArray:
                    for item in candidate.values:
                        walk(item, depth=depth + 1)
                else:
                    for key, item in candidate.members:
                        utf8_bytes += len(key.encode("utf-8"))
                        if utf8_bytes > MAX_EXTENSION_PAYLOAD_UTF8_BYTES:
                            raise ValueError(
                                "extension payload exceeds the UTF-8 size bound"
                            )
                        walk(item, depth=depth + 1)
            finally:
                active_containers.remove(identity)

    walk(value, depth=0)
    try:
        payload_bytes = canonical_json_bytes(_thaw_json_payload(value))
    except (TypeError, ValueError) as exc:
        raise ValueError("extension payload must be finite JSON") from exc
    if len(payload_bytes) > MAX_EXTENSION_PAYLOAD_UTF8_BYTES:
        raise ValueError("extension payload exceeds the canonical JSON size bound")
    return nodes, len(payload_bytes)


def _absolute_position(
    position: ScorePosition,
    measure_lookup: dict[str, tuple[int, Fraction, Fraction]],
    *,
    path: str,
    allow_measure_end: bool = False,
) -> Fraction:
    info = measure_lookup.get(position.measure_id)
    if info is None:
        raise ValueError(
            f"{path} references unknown measure {position.measure_id!r}"
        )
    measure_index, start, duration = info
    offset = position.offset_quarters.as_fraction()
    if offset > duration:
        raise ValueError(
            f"{path} offset exceeds measure {position.measure_id!r} duration"
        )
    if offset == duration and (
        not allow_measure_end or measure_index != len(measure_lookup) - 1
    ):
        raise ValueError(
            f"{path} uses an ambiguous or disallowed measure-end position"
        )
    return _bounded_timeline_fraction(
        start + offset,
        path=path,
    )


@dataclass(frozen=True, slots=True)
class ScoreV2Document:
    kind: str
    schema_version: int
    title: str
    timeline: ScoreTimeline
    tuning: ScoreTuning
    parts: tuple[ScorePartV2, ...]
    ties: tuple[ScoreTie, ...] = ()
    phrases: tuple[ScorePhrase, ...] = ()
    form: ScoreForm | None = None
    extensions: tuple[ScoreExtension, ...] = ()

    def __post_init__(self) -> None:
        _bounded_text(self.kind, name="kind", nonblank=True)
        if self.kind != "tianlai.score":
            raise ValueError("kind must be 'tianlai.score'")
        schema_version = _integral_component(
            self.schema_version,
            name="schema_version",
        )
        if schema_version != 2:
            raise ValueError("schema_version must be 2")
        object.__setattr__(self, "schema_version", schema_version)
        _bounded_text(self.title, name="title")
        if type(self.timeline) is not ScoreTimeline:
            raise ValueError("timeline must be a ScoreTimeline")
        if type(self.tuning) is not ScoreTuning:
            raise ValueError("tuning must be a ScoreTuning")
        parts = _typed_tuple(
            self.parts,
            ScorePartV2,
            name="score.parts",
            maximum=MAX_PARTS,
            minimum=1,
        )
        ties = _typed_tuple(
            self.ties,
            ScoreTie,
            name="score.ties",
            maximum=MAX_RELATIONS,
        )
        phrases = _typed_tuple(
            self.phrases,
            ScorePhrase,
            name="score.phrases",
            maximum=MAX_RELATIONS,
        )
        if len(ties) + len(phrases) > MAX_RELATIONS:
            raise ValueError("score relations exceed the supported bound")
        if self.form is not None and type(self.form) is not ScoreForm:
            raise ValueError("form must be a ScoreForm or absent")
        extensions = _typed_tuple(
            self.extensions,
            ScoreExtension,
            name="score.extensions",
            maximum=MAX_EXTENSIONS,
        )
        extension_nodes = 0
        extension_bytes = 0
        extension_keys: set[tuple[str, int]] = set()
        for extension in extensions:
            extension_key = (extension.namespace, extension.version)
            if extension_key in extension_keys:
                raise ValueError(
                    "duplicate score extension namespace/version: "
                    f"{extension_key!r}"
                )
            extension_keys.add(extension_key)
            extension_nodes += extension._payload_nodes
            extension_bytes += extension._payload_canonical_bytes
            if extension_nodes > MAX_EXTENSION_PAYLOAD_NODES:
                raise ValueError(
                    "score extension payloads exceed the aggregate JSON node bound"
                )
            if extension_bytes > MAX_EXTENSION_PAYLOAD_UTF8_BYTES:
                raise ValueError(
                    "score extension payloads exceed the aggregate canonical "
                    "JSON size bound"
                )

        measure_lookup: dict[str, tuple[int, Fraction, Fraction]] = {}
        score_duration = Fraction(0)
        for index, measure in enumerate(self.timeline.measures):
            duration = measure.actual_duration_quarters.as_fraction()
            measure_lookup[measure.measure_id] = (
                index,
                score_duration,
                duration,
            )
            score_duration = _bounded_timeline_fraction(
                score_duration + duration,
                path=f"timeline cumulative position after measures[{index}]",
            )

        part_ids: set[str] = set()
        event_lookup: dict[
            str,
            tuple[str, ScoreNoteV2, Fraction, Fraction],
        ] = {}
        total_notes = 0
        total_articulations = 0
        for part_index, part in enumerate(parts):
            if part.part_id in part_ids:
                raise ValueError(f"duplicate part_id: {part.part_id!r}")
            part_ids.add(part.part_id)
            total_notes += len(part.notes)
            if total_notes > MAX_NOTES:
                raise ValueError("score notes exceed the supported bound")
            total_articulations += sum(
                len(note.articulations) for note in part.notes
            )
            if total_articulations > MAX_ARTICULATIONS:
                raise ValueError(
                    "score note articulations exceed the aggregate supported bound"
                )
            previous_start: Fraction | None = None
            for note_index, note in enumerate(part.notes):
                if note.event_id in event_lookup:
                    raise ValueError(f"duplicate event_id: {note.event_id!r}")
                start = _absolute_position(
                    note.position,
                    measure_lookup,
                    path=(
                        f"parts[{part_index}].notes[{note_index}].position"
                    ),
                )
                if previous_start is not None and start < previous_start:
                    raise ValueError(
                        f"parts[{part_index}].notes must be in exact "
                        "nondecreasing position order"
                    )
                previous_start = start
                end = _bounded_timeline_fraction(
                    start + note.duration_quarters.as_fraction(),
                    path=f"note {note.event_id!r} end",
                )
                if end > score_duration:
                    raise ValueError(
                        f"note {note.event_id!r} extends beyond the timeline"
                    )
                event_lookup[note.event_id] = (part.part_id, note, start, end)

        tie_ids: set[str] = set()
        tied_from: set[str] = set()
        tied_to: set[str] = set()
        for index, tie in enumerate(ties):
            if tie.tie_id in tie_ids:
                raise ValueError(f"duplicate tie_id: {tie.tie_id!r}")
            tie_ids.add(tie.tie_id)
            source = event_lookup.get(tie.from_event_id)
            target = event_lookup.get(tie.to_event_id)
            if source is None:
                raise ValueError(
                    f"ties[{index}] references missing from_event_id "
                    f"{tie.from_event_id!r}"
                )
            if target is None:
                raise ValueError(
                    f"ties[{index}] references missing to_event_id "
                    f"{tie.to_event_id!r}"
                )
            if source[0] != target[0]:
                raise ValueError(f"tie {tie.tie_id!r} must stay within one part")
            if source[1].sounding_pitch != target[1].sounding_pitch:
                raise ValueError(f"tie {tie.tie_id!r} must join the same pitch")
            if source[3] != target[2]:
                raise ValueError(
                    f"tie {tie.tie_id!r} events must be exactly contiguous"
                )
            if tie.from_event_id in tied_from:
                raise ValueError(
                    f"event {tie.from_event_id!r} has more than one outgoing tie"
                )
            if tie.to_event_id in tied_to:
                raise ValueError(
                    f"event {tie.to_event_id!r} has more than one incoming tie"
                )
            tied_from.add(tie.from_event_id)
            tied_to.add(tie.to_event_id)

        phrase_ids: set[str] = set()
        for index, phrase in enumerate(phrases):
            if phrase.phrase_id in phrase_ids:
                raise ValueError(f"duplicate phrase_id: {phrase.phrase_id!r}")
            phrase_ids.add(phrase.phrase_id)
            if phrase.part_id not in part_ids:
                raise ValueError(
                    f"phrase {phrase.phrase_id!r} references missing part_id "
                    f"{phrase.part_id!r}"
                )
            start = _absolute_position(
                phrase.start,
                measure_lookup,
                path=f"phrases[{index}].start",
            )
            end = _absolute_position(
                phrase.end,
                measure_lookup,
                path=f"phrases[{index}].end",
                allow_measure_end=True,
            )
            if end <= start:
                raise ValueError(
                    f"phrase {phrase.phrase_id!r} must end after its start"
                )

    @property
    def identity_contract(self) -> str:
        return SCORE_V2_IDENTITY_CONTRACT

    @property
    def has_stable_event_identity(self) -> bool:
        return True

    @property
    def time_contract(self) -> str:
        return SCORE_V2_TIME_CONTRACT

    def part(self, part_id: str) -> ScorePartV2:
        for part in self.parts:
            if part.part_id == part_id:
                return part
        raise ValueError(f"score has no part {part_id!r}")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "title": self.title,
            "timeline": self.timeline.to_dict(),
            "tuning": self.tuning.to_dict(),
            "parts": [part.to_dict() for part in self.parts],
        }
        if self.ties:
            result["ties"] = [tie.to_dict() for tie in self.ties]
        if self.phrases:
            result["phrases"] = [phrase.to_dict() for phrase in self.phrases]
        if self.form is not None:
            result["form"] = self.form.to_dict()
        if self.extensions:
            result["extensions"] = [
                extension.to_dict() for extension in self.extensions
            ]
        return result


def _object_fields(
    value: object,
    *,
    path: str,
    allowed: frozenset[str],
    required: frozenset[str],
) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError(f"{path} must be an object")
    for key in value:
        if type(key) is not str:
            raise ValueError(f"{path} object keys must be strings")
        if key not in allowed:
            preview = key if len(key) <= 80 else f"{key[:77]}..."
            raise ValueError(
                f"{path} contains unknown field: {preview!r}"
            )
    missing = sorted(key for key in required if key not in value)
    if missing:
        fields = ", ".join(f"{path}.{key}" for key in missing)
        raise ValueError(f"{path} is missing required field(s): {fields}")
    return value


def _array(
    value: object,
    *,
    path: str,
    maximum: int,
    minimum: int = 0,
) -> list[object]:
    if type(value) is not list:
        raise ValueError(f"{path} must be an array")
    if len(value) < minimum:
        raise ValueError(f"{path} must contain at least {minimum} item(s)")
    if len(value) > maximum:
        raise ValueError(f"{path} exceeds the {maximum}-item bound")
    return value


def _parse_rational(value: object, *, path: str) -> Rational:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("numerator", "denominator")),
        required=frozenset(("numerator", "denominator")),
    )
    return Rational(
        raw["numerator"],  # type: ignore[arg-type]
        raw["denominator"],  # type: ignore[arg-type]
    )


def _parse_position(value: object, *, path: str) -> ScorePosition:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("measure_id", "offset_quarters")),
        required=frozenset(("measure_id", "offset_quarters")),
    )
    return ScorePosition(
        measure_id=_identifier(raw["measure_id"], name=f"{path}.measure_id"),
        offset_quarters=_parse_rational(
            raw["offset_quarters"],
            path=f"{path}.offset_quarters",
        ),
    )


def _parse_measure(value: object, *, path: str) -> ScoreMeasure:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("measure_id", "actual_duration_quarters")),
        required=frozenset(("measure_id", "actual_duration_quarters")),
    )
    return ScoreMeasure(
        measure_id=_identifier(raw["measure_id"], name=f"{path}.measure_id"),
        actual_duration_quarters=_parse_rational(
            raw["actual_duration_quarters"],
            path=f"{path}.actual_duration_quarters",
        ),
    )


_METER_EVENT_FIELDS = frozenset(("meter_id", "at", "groups", "beat_unit"))


def _parse_meter_event(value: object, *, path: str) -> MeterEvent:
    raw = _object_fields(
        value,
        path=path,
        allowed=_METER_EVENT_FIELDS,
        required=_METER_EVENT_FIELDS,
    )
    groups_raw = _array(
        raw["groups"],
        path=f"{path}.groups",
        maximum=64,
        minimum=1,
    )
    groups = tuple(
        _positive_integer(group, name=f"{path}.groups[{index}]")
        for index, group in enumerate(groups_raw)
    )
    return MeterEvent(
        meter_id=_identifier(raw["meter_id"], name=f"{path}.meter_id"),
        at=_parse_position(raw["at"], path=f"{path}.at"),
        groups=groups,
        beat_unit=_positive_integer(raw["beat_unit"], name=f"{path}.beat_unit"),
    )


def _parse_tempo_event(value: object, *, path: str) -> TempoEvent:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("tempo_id", "at", "quarter_bpm")),
        required=frozenset(("tempo_id", "at", "quarter_bpm")),
    )
    return TempoEvent(
        tempo_id=_identifier(raw["tempo_id"], name=f"{path}.tempo_id"),
        at=_parse_position(raw["at"], path=f"{path}.at"),
        quarter_bpm=_parse_rational(
            raw["quarter_bpm"],
            path=f"{path}.quarter_bpm",
        ),
    )


def _parse_timeline(value: object, *, path: str) -> ScoreTimeline:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("measures", "meter_events", "tempo_events")),
        required=frozenset(("measures", "meter_events", "tempo_events")),
    )
    measures_raw = _array(
        raw["measures"],
        path=f"{path}.measures",
        maximum=MAX_MEASURES,
        minimum=1,
    )
    meter_raw = _array(
        raw["meter_events"],
        path=f"{path}.meter_events",
        maximum=MAX_METER_EVENTS,
        minimum=1,
    )
    tempo_raw = _array(
        raw["tempo_events"],
        path=f"{path}.tempo_events",
        maximum=MAX_TEMPO_EVENTS,
        minimum=1,
    )
    total_meter_groups = 0
    for index, item in enumerate(meter_raw):
        meter_path = f"{path}.meter_events[{index}]"
        meter = _object_fields(
            item,
            path=meter_path,
            allowed=_METER_EVENT_FIELDS,
            required=_METER_EVENT_FIELDS,
        )
        groups = _array(
            meter["groups"],
            path=f"{meter_path}.groups",
            maximum=64,
            minimum=1,
        )
        total_meter_groups += len(groups)
        if total_meter_groups > MAX_METER_GROUPS:
            raise ValueError("timeline meter groups exceed the aggregate bound")
    return ScoreTimeline(
        measures=tuple(
            _parse_measure(item, path=f"{path}.measures[{index}]")
            for index, item in enumerate(measures_raw)
        ),
        meter_events=tuple(
            _parse_meter_event(item, path=f"{path}.meter_events[{index}]")
            for index, item in enumerate(meter_raw)
        ),
        tempo_events=tuple(
            _parse_tempo_event(item, path=f"{path}.tempo_events[{index}]")
            for index, item in enumerate(tempo_raw)
        ),
    )


def _parse_tuning(value: object, *, path: str) -> ScoreTuning:
    fields = frozenset(
        (
            "tuning_id",
            "system",
            "divisions_per_octave",
            "reference_midi_note",
            "reference_frequency_hz",
        )
    )
    raw = _object_fields(
        value,
        path=path,
        allowed=fields,
        required=fields,
    )
    system = _bounded_text(raw["system"], name=f"{path}.system")
    return ScoreTuning(
        tuning_id=_identifier(raw["tuning_id"], name=f"{path}.tuning_id"),
        system=system,
        divisions_per_octave=_positive_integer(
            raw["divisions_per_octave"],
            name=f"{path}.divisions_per_octave",
        ),
        reference_midi_note=_parse_rational(
            raw["reference_midi_note"],
            path=f"{path}.reference_midi_note",
        ),
        reference_frequency_hz=_parse_rational(
            raw["reference_frequency_hz"],
            path=f"{path}.reference_frequency_hz",
        ),
    )


def _parse_written_pitch(value: object, *, path: str) -> WrittenPitch:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("step", "alter", "octave", "accidental")),
        required=frozenset(("step", "alter", "octave")),
    )
    step = _bounded_text(raw["step"], name=f"{path}.step", nonblank=True)
    accidental: str | None = None
    if "accidental" in raw:
        accidental = _notation_text(
            raw["accidental"],
            name=f"{path}.accidental",
        )
    return WrittenPitch(
        step=step,
        alter=_parse_rational(raw["alter"], path=f"{path}.alter"),
        octave=_safe_integer(raw["octave"], name=f"{path}.octave"),
        accidental=accidental,
    )


def _parse_sounding_pitch(value: object, *, path: str) -> SoundingPitch:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("midi_note",)),
        required=frozenset(("midi_note",)),
    )
    return SoundingPitch(
        midi_note=_parse_rational(raw["midi_note"], path=f"{path}.midi_note")
    )


_NOTE_FIELDS = frozenset(
    (
        "event_id",
        "position",
        "duration_quarters",
        "written_pitch",
        "sounding_pitch",
        "dynamic",
        "articulations",
        "staff",
        "voice",
    )
)
_NOTE_REQUIRED_FIELDS = frozenset(
    (
        "event_id",
        "position",
        "duration_quarters",
        "written_pitch",
        "sounding_pitch",
    )
)


def _parse_note(value: object, *, path: str) -> ScoreNoteV2:
    raw = _object_fields(
        value,
        path=path,
        allowed=_NOTE_FIELDS,
        required=_NOTE_REQUIRED_FIELDS,
    )
    dynamic: str | None = None
    if "dynamic" in raw:
        dynamic = _dynamic(
            raw["dynamic"],
            name=f"{path}.dynamic",
        )
    articulations: tuple[str, ...] = ()
    if "articulations" in raw:
        articulation_raw = _array(
            raw["articulations"],
            path=f"{path}.articulations",
            maximum=MAX_ARTICULATIONS_PER_NOTE,
        )
        articulations = tuple(
            _notation_text(
                item,
                name=f"{path}.articulations[{index}]",
            )
            for index, item in enumerate(articulation_raw)
        )
        if len(set(articulations)) != len(articulations):
            raise ValueError(f"{path}.articulations must contain unique strings")
    staff: int | None = None
    if "staff" in raw:
        staff = _positive_integer(raw["staff"], name=f"{path}.staff")
    voice: str | None = None
    if "voice" in raw:
        voice = _notation_text(
            raw["voice"],
            name=f"{path}.voice",
        )
    return ScoreNoteV2(
        event_id=_identifier(raw["event_id"], name=f"{path}.event_id"),
        position=_parse_position(raw["position"], path=f"{path}.position"),
        duration_quarters=_parse_rational(
            raw["duration_quarters"],
            path=f"{path}.duration_quarters",
        ),
        written_pitch=_parse_written_pitch(
            raw["written_pitch"],
            path=f"{path}.written_pitch",
        ),
        sounding_pitch=_parse_sounding_pitch(
            raw["sounding_pitch"],
            path=f"{path}.sounding_pitch",
        ),
        dynamic=dynamic,
        articulations=articulations,
        staff=staff,
        voice=voice,
    )


_PART_FIELDS = frozenset(
    (
        "part_id",
        "name",
        "default_dynamic",
        "default_articulation",
        "notes",
    )
)


def _preflight_parts(value: object, *, path: str) -> list[dict[str, object]]:
    parts_raw = _array(value, path=path, maximum=MAX_PARTS, minimum=1)
    result: list[dict[str, object]] = []
    total_notes = 0
    total_articulations = 0
    for index, item in enumerate(parts_raw):
        part_path = f"{path}[{index}]"
        raw = _object_fields(
            item,
            path=part_path,
            allowed=_PART_FIELDS,
            required=frozenset(("part_id", "notes")),
        )
        notes = _array(
            raw["notes"],
            path=f"{part_path}.notes",
            maximum=MAX_NOTES,
        )
        total_notes += len(notes)
        if total_notes > MAX_NOTES:
            raise ValueError("score.parts notes exceed the supported bound")
        for note_index, note in enumerate(notes):
            note_path = f"{part_path}.notes[{note_index}]"
            note_raw = _object_fields(
                note,
                path=note_path,
                allowed=_NOTE_FIELDS,
                required=_NOTE_REQUIRED_FIELDS,
            )
            if "articulations" in note_raw:
                articulations = _array(
                    note_raw["articulations"],
                    path=f"{note_path}.articulations",
                    maximum=MAX_ARTICULATIONS_PER_NOTE,
                )
                total_articulations += len(articulations)
                if total_articulations > MAX_ARTICULATIONS:
                    raise ValueError(
                        "score note articulations exceed the aggregate "
                        "supported bound"
                    )
        result.append(raw)
    return result


def _parse_part(raw: dict[str, object], *, path: str) -> ScorePartV2:
    name: str | None = None
    if "name" in raw:
        name = _bounded_text(raw["name"], name=f"{path}.name")
    default_dynamic: str | None = None
    if "default_dynamic" in raw:
        default_dynamic = _dynamic(
            raw["default_dynamic"],
            name=f"{path}.default_dynamic",
        )
    default_articulation: str | None = None
    if "default_articulation" in raw:
        default_articulation = _notation_text(
            raw["default_articulation"],
            name=f"{path}.default_articulation",
        )
    notes_raw = _array(
        raw["notes"],
        path=f"{path}.notes",
        maximum=MAX_NOTES,
    )
    return ScorePartV2(
        part_id=_identifier(raw["part_id"], name=f"{path}.part_id"),
        name=name,
        default_dynamic=default_dynamic,
        default_articulation=default_articulation,
        notes=tuple(
            _parse_note(item, path=f"{path}.notes[{index}]")
            for index, item in enumerate(notes_raw)
        ),
    )


def _parse_tie(value: object, *, path: str) -> ScoreTie:
    fields = frozenset(("tie_id", "from_event_id", "to_event_id"))
    raw = _object_fields(
        value,
        path=path,
        allowed=fields,
        required=fields,
    )
    return ScoreTie(
        tie_id=_identifier(raw["tie_id"], name=f"{path}.tie_id"),
        from_event_id=_identifier(
            raw["from_event_id"],
            name=f"{path}.from_event_id",
        ),
        to_event_id=_identifier(
            raw["to_event_id"],
            name=f"{path}.to_event_id",
        ),
    )


def _parse_phrase(value: object, *, path: str) -> ScorePhrase:
    fields = frozenset(("phrase_id", "part_id", "start", "end"))
    raw = _object_fields(
        value,
        path=path,
        allowed=fields,
        required=fields,
    )
    return ScorePhrase(
        phrase_id=_identifier(raw["phrase_id"], name=f"{path}.phrase_id"),
        part_id=_identifier(raw["part_id"], name=f"{path}.part_id"),
        start=_parse_position(raw["start"], path=f"{path}.start"),
        end=_parse_position(raw["end"], path=f"{path}.end"),
    )


def _parse_form(value: object, *, path: str) -> ScoreForm:
    raw = _object_fields(
        value,
        path=path,
        allowed=frozenset(("mode",)),
        required=frozenset(("mode",)),
    )
    mode = _bounded_text(raw["mode"], name=f"{path}.mode", nonblank=True)
    if mode != "linear":
        raise ValueError(f"{path}.mode must be 'linear'")
    return ScoreForm(mode=mode)


def _parse_extension(
    value: object,
    *,
    path: str,
    budget: _ExtensionPayloadBudget | None = None,
) -> ScoreExtension:
    fields = frozenset(
        ("namespace", "version", "required", "audible", "payload")
    )
    raw = _object_fields(
        value,
        path=path,
        allowed=fields,
        required=fields,
    )
    if type(raw["required"]) is not bool:
        raise ValueError(f"{path}.required must be a boolean")
    if type(raw["audible"]) is not bool:
        raise ValueError(f"{path}.audible must be a boolean")
    namespace = _identifier(
        raw["namespace"],
        name=f"{path}.namespace",
    )
    version = _positive_integer(raw["version"], name=f"{path}.version")
    if (
        (namespace, version) not in SUPPORTED_SCORE_V2_EXTENSIONS
        and (raw["required"] or raw["audible"])
    ):
        raise ValueError(
            f"{path} is an unknown required or audible score extension"
        )
    aggregate = budget if budget is not None else _ExtensionPayloadBudget()
    extension = ScoreExtension(
        namespace=namespace,
        version=version,
        required=raw["required"],
        audible=raw["audible"],
        payload=_freeze_json_payload(raw["payload"], budget=aggregate),
    )
    aggregate.canonical_bytes += extension._payload_canonical_bytes
    if aggregate.canonical_bytes > MAX_EXTENSION_PAYLOAD_UTF8_BYTES:
        raise ValueError(
            "score extension payloads exceed the aggregate canonical JSON size bound"
        )
    return extension


def parse_score_v2_document(raw: dict[str, Any]) -> ScoreV2Document:
    """Parse, normalize and completely validate one score-v2 JSON value."""

    top_fields = frozenset(
        (
            "kind",
            "schema_version",
            "title",
            "timeline",
            "tuning",
            "parts",
            "ties",
            "phrases",
            "form",
            "extensions",
        )
    )
    document = _object_fields(
        raw,
        path="score",
        allowed=top_fields,
        required=frozenset(
            ("kind", "schema_version", "title", "timeline", "tuning", "parts")
        ),
    )
    kind = _bounded_text(document["kind"], name="score.kind", nonblank=True)
    schema_version = _integral_component(
        document["schema_version"],
        name="score.schema_version",
    )
    title = _bounded_text(document["title"], name="score.title")
    if kind != "tianlai.score":
        raise ValueError("score.kind must be 'tianlai.score'")
    if schema_version != 2:
        raise ValueError("score.schema_version must be 2")

    # Preflight every potentially large array before constructing its domain
    # objects.  This keeps adversarial JSON from forcing a partial 250k-object
    # graph only to fail a later aggregate limit.
    parts_raw = _preflight_parts(document["parts"], path="score.parts")
    ties_raw = _array(
        document.get("ties", []),
        path="score.ties",
        maximum=MAX_RELATIONS,
    )
    phrases_raw = _array(
        document.get("phrases", []),
        path="score.phrases",
        maximum=MAX_RELATIONS,
    )
    if len(ties_raw) + len(phrases_raw) > MAX_RELATIONS:
        raise ValueError("score relations exceed the supported bound")
    extensions_raw = _array(
        document.get("extensions", []),
        path="score.extensions",
        maximum=MAX_EXTENSIONS,
    )
    # Extensions are the only recursively shaped, opaque inputs in this core.
    # Freeze and budget them before materializing the potentially much larger
    # timeline/note graph, and enforce one aggregate document budget rather
    # than allowing MAX_EXTENSIONS independent maximum-sized payloads.
    extension_budget = _ExtensionPayloadBudget()
    extension_items: list[ScoreExtension] = []
    extension_keys: set[tuple[str, int]] = set()
    for index, item in enumerate(extensions_raw):
        extension = _parse_extension(
            item,
            path=f"score.extensions[{index}]",
            budget=extension_budget,
        )
        extension_key = (extension.namespace, extension.version)
        if extension_key in extension_keys:
            raise ValueError(
                "duplicate score extension namespace/version: "
                f"{extension_key!r}"
            )
        extension_keys.add(extension_key)
        extension_items.append(extension)
    extensions = tuple(extension_items)

    return ScoreV2Document(
        kind=kind,
        schema_version=schema_version,
        title=title,
        timeline=_parse_timeline(document["timeline"], path="score.timeline"),
        tuning=_parse_tuning(document["tuning"], path="score.tuning"),
        parts=tuple(
            _parse_part(item, path=f"score.parts[{index}]")
            for index, item in enumerate(parts_raw)
        ),
        ties=tuple(
            _parse_tie(item, path=f"score.ties[{index}]")
            for index, item in enumerate(ties_raw)
        ),
        phrases=tuple(
            _parse_phrase(item, path=f"score.phrases[{index}]")
            for index, item in enumerate(phrases_raw)
        ),
        form=(
            _parse_form(document["form"], path="score.form")
            if "form" in document
            else None
        ),
        extensions=extensions,
    )


def score_render_projection(document: ScoreV2Document) -> dict[str, Any]:
    """Return the versioned render-relevant JSON projection."""

    if type(document) is not ScoreV2Document:
        raise TypeError("document must be a ScoreV2Document")
    parts: list[dict[str, object]] = []
    for part in document.parts:
        notes: list[dict[str, object]] = []
        for note in part.notes:
            notes.append(
                {
                    "event_id": note.event_id,
                    "position": note.position.to_dict(),
                    "duration_quarters": note.duration_quarters.to_dict(),
                    "sounding_pitch": note.sounding_pitch.to_dict(),
                    "dynamic": note.dynamic,
                    "articulations": list(note.articulations),
                    "staff": note.staff,
                    "voice": note.voice,
                }
            )
        parts.append(
            {
                "part_id": part.part_id,
                "default_dynamic": part.default_dynamic,
                "default_articulation": part.default_articulation,
                "notes": notes,
            }
        )
    return {
        "kind": "tianlai.score_render_projection",
        "projection_version": SCORE_RENDER_PROJECTION_VERSION,
        "timeline": document.timeline.to_dict(),
        "tuning": document.tuning.to_dict(),
        "parts": parts,
        "ties": [tie.to_dict() for tie in document.ties],
        "phrases": [phrase.to_dict() for phrase in document.phrases],
        # The core supports no nonlinear form, so omission and an explicit
        # linear form are deliberately the same render semantics.
        "form": {"mode": "linear"},
    }


def score_render_projection_sha256(document: ScoreV2Document) -> str:
    """Return a domain-separated SHA-256 for the render projection."""

    import hashlib

    payload = canonical_json_bytes(score_render_projection(document))
    return hashlib.sha256(SCORE_RENDER_PROJECTION_DOMAIN + payload).hexdigest()


__all__ = [
    "MAX_ARTICULATIONS",
    "MAX_ARTICULATIONS_PER_NOTE",
    "MAX_EXTENSION_PAYLOAD_CONTAINER_ITEMS",
    "MAX_EXTENSION_PAYLOAD_DEPTH",
    "MAX_EXTENSION_PAYLOAD_NODES",
    "MAX_EXTENSION_PAYLOAD_UTF8_BYTES",
    "MAX_EXTENSIONS",
    "MAX_ID_CHARACTERS",
    "MAX_ID_UTF8_BYTES",
    "MAX_MEASURES",
    "MAX_METER_EVENTS",
    "MAX_METER_GROUPS",
    "MAX_NOTES",
    "MAX_PARTS",
    "MAX_RATIONAL_DENOMINATOR",
    "MAX_RELATIONS",
    "MAX_SAFE_INTEGER",
    "MAX_TEMPO_EVENTS",
    "MAX_TEXT_CHARACTERS",
    "MAX_TEXT_UTF8_BYTES",
    "MAX_TIMELINE_COMMON_DENOMINATOR_BITS",
    "MAX_TIMELINE_CUMULATIVE_POSITION_BITS",
    "MeterEvent",
    "Rational",
    "SCORE_RENDER_PROJECTION_DOMAIN",
    "SCORE_RENDER_PROJECTION_VERSION",
    "SCORE_V2_IDENTITY_CONTRACT",
    "SCORE_V2_TIME_CONTRACT",
    "SUPPORTED_SCORE_V2_EXTENSIONS",
    "ScoreExtension",
    "ScoreForm",
    "ScoreMeasure",
    "ScoreNoteV2",
    "ScorePartV2",
    "ScorePhrase",
    "ScorePosition",
    "ScoreTie",
    "ScoreTimeline",
    "ScoreTuning",
    "ScoreV2Document",
    "SoundingPitch",
    "TempoEvent",
    "WrittenPitch",
    "parse_score_v2_document",
    "score_render_projection",
    "score_render_projection_sha256",
]
