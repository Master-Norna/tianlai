"""Strict score-time validation and reversible musical coordinates.

The score parser deliberately keeps notation separate from performance.  This
module supplies the time contract shared by future parser, MCP and revision
tools without teaching the score model about any renderer:

* bar/beat positions use the half-open interval
  ``[1, beats_per_bar + 1)``;
* ``seconds`` and ``absolute_quarter`` describe logical score time, never a
  conductor-adjusted or humanized note-on;
* an exact bar-line boundary belongs to the next bar at beat 1.

The implementation imports score types only while type-checking.  Consequently
``tianlai.score`` may call these validators later without creating a runtime
import cycle.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import math
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from .score import ScoreDocument, TempoEntry, TempoMap


_QUARTER_BOUNDARY_TOLERANCE = 1e-10


class ScoreTimeError(ValueError):
    """A score position or logical-time query violates the time contract."""


@dataclass(frozen=True, slots=True)
class MusicalCoordinate:
    """One logical score location expressed in both notation and clock time."""

    bar: int
    beat: float
    absolute_quarter: float
    seconds: float

    def to_dict(self) -> dict[str, int | float]:
        return {
            "bar": self.bar,
            "beat": self.beat,
            "absolute_quarter": self.absolute_quarter,
            "seconds": self.seconds,
        }


@dataclass(frozen=True, slots=True)
class SecondsRange:
    """A clipped, half-open ``[start_seconds, end_seconds)`` query window."""

    start_seconds: float
    end_seconds: float

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.start_seconds)
            or not math.isfinite(self.end_seconds)
            or self.start_seconds < 0.0
            or self.end_seconds < self.start_seconds
        ):
            raise ScoreTimeError(
                "seconds range must be finite, non-negative and ordered"
            )

    @property
    def duration_seconds(self) -> float:
        return self.end_seconds - self.start_seconds

    def to_dict(self) -> dict[str, float]:
        return {
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "duration_seconds": self.duration_seconds,
        }


def _finite_number(value: object, *, path: str) -> float:
    if isinstance(value, bool):
        raise ScoreTimeError(f"{path} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ScoreTimeError(f"{path} must be a finite number") from exc
    if not math.isfinite(number):
        raise ScoreTimeError(f"{path} must be a finite number")
    return number


def _positive_bar(value: object, *, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ScoreTimeError(f"{path} must be an integer starting at 1")
    return value


def _meter_entry_at_bar(tempo_map: TempoMap, bar: int) -> TempoEntry:
    """Return the downbeat entry whose meter governs ``bar``."""

    if not tempo_map.entries:
        raise ScoreTimeError("score.tempo_map must contain at least one entry")
    chosen = None
    for entry in tempo_map.entries:
        if entry.bar > bar:
            break
        if entry.changes_meter:
            chosen = entry
    if chosen is None:
        raise ScoreTimeError(
            f"score.tempo_map does not define a meter for bar {bar}"
        )
    if chosen.beats_per_bar < 1 or chosen.beat_unit < 1:
        raise ScoreTimeError(
            f"score.tempo_map has an invalid meter at bar {chosen.bar}"
        )
    return chosen


def validate_bar_beat(
    tempo_map: TempoMap,
    bar: int,
    beat: float,
    *,
    path: str = "position",
) -> None:
    """Validate one bar/beat pair against the meter governing that bar.

    Beat positions are strict and half-open.  For example, valid positions in
    4/4 satisfy ``1 <= beat < 5``; the following downbeat must be written as
    ``bar + 1, beat 1``.
    """

    checked_bar = _positive_bar(bar, path=f"{path}.bar")
    checked_beat = _finite_number(beat, path=f"{path}.beat")
    meter = _meter_entry_at_bar(tempo_map, checked_bar)
    upper = float(meter.beats_per_bar) + 1.0
    if checked_beat < 1.0 or checked_beat >= upper:
        raise ScoreTimeError(
            f"{path}.beat={checked_beat:g} is outside [1, {upper:g}) "
            f"for bar {checked_bar} in {meter.beats_per_bar}/{meter.beat_unit}"
        )


def validate_optional_bar_beat(
    tempo_map: TempoMap,
    bar: int | None,
    beat: float | None = None,
    *,
    path: str = "position",
) -> None:
    """Validate an optional location.

    ``bar=None, beat=None`` means the location is absent.  If a bar is present,
    an omitted beat means its downbeat.  A beat without a bar is ambiguous and
    rejected.
    """

    if bar is None:
        if beat is not None:
            raise ScoreTimeError(f"{path}.beat is present but {path}.bar is absent")
        return
    validate_bar_beat(
        tempo_map,
        bar,
        1.0 if beat is None else beat,
        path=path,
    )


def validate_tempo_map_coordinates(tempo_map: TempoMap) -> None:
    """Validate every tempo/meter marker and its ordering."""

    if not tempo_map.entries:
        raise ScoreTimeError("score.tempo_map must contain at least one entry")
    first = tempo_map.entries[0]
    if first.bar != 1 or first.beat != 1.0:
        raise ScoreTimeError(
            "score.tempo_map[0] must start at bar 1 beat 1"
        )
    previous: tuple[int, float] | None = None
    previous_quarter: float | None = None
    for index, entry in enumerate(tempo_map.entries):
        path = f"score.tempo_map[{index}]"
        validate_bar_beat(tempo_map, entry.bar, entry.beat, path=path)
        current = (entry.bar, entry.beat)
        if previous is not None and current <= previous:
            raise ScoreTimeError(
                f"{path} must follow score.tempo_map[{index - 1}]"
            )
        quarter = tempo_map.quarter_at(entry.bar, entry.beat)
        if previous_quarter is not None and quarter <= previous_quarter:
            raise ScoreTimeError(
                f"{path} does not advance logical score time"
            )
        if not math.isfinite(entry.bpm) or entry.bpm <= 0.0:
            raise ScoreTimeError(f"{path}.bpm must be a positive finite number")
        previous = current
        previous_quarter = quarter


def validate_score_time_coordinates(score: ScoreDocument) -> None:
    """Strictly validate all tempo, note and phrase positions in ``score``."""

    validate_tempo_map_coordinates(score.tempo_map)
    if not math.isfinite(score.tail_seconds) or score.tail_seconds < 0.0:
        raise ScoreTimeError(
            "score.tail_seconds must be a finite non-negative number"
        )
    for part_index, part in enumerate(score.parts):
        part_path = f"score.parts[{part_index}]"
        for note in part.notes:
            # ``index`` preserves the note's source-array position after the
            # parser sorts notes into musical order.
            validate_bar_beat(
                score.tempo_map,
                note.bar,
                note.beat,
                path=f"{part_path}.notes[{note.index}]",
            )
            if (
                not math.isfinite(note.duration_beats)
                or note.duration_beats <= 0.0
            ):
                raise ScoreTimeError(
                    f"{part_path}.notes[{note.index}].duration_beats "
                    "must be a positive finite number"
                )
        for phrase_index, phrase in enumerate(part.phrases):
            phrase_path = f"{part_path}.phrases[{phrase_index}]"
            validate_bar_beat(
                score.tempo_map,
                phrase.start_bar,
                phrase.start_beat,
                path=f"{phrase_path}.start",
            )
            validate_bar_beat(
                score.tempo_map,
                phrase.end_bar,
                phrase.end_beat,
                path=f"{phrase_path}.end",
            )
            start_quarter = score.tempo_map.quarter_at(
                phrase.start_bar,
                phrase.start_beat,
            )
            end_quarter = score.tempo_map.quarter_at(
                phrase.end_bar,
                phrase.end_beat,
            )
            if end_quarter < start_quarter:
                raise ScoreTimeError(
                    f"{phrase_path}.end must not precede {phrase_path}.start"
                )


def _tempo_boundaries(
    tempo_map: TempoMap,
) -> tuple[list[float], list[float]]:
    """Return parallel logical-quarter and second starts for tempo entries."""

    quarters = [
        tempo_map.quarter_at(entry.bar, entry.beat)
        for entry in tempo_map.entries
    ]
    seconds = [0.0]
    for index in range(1, len(quarters)):
        span = quarters[index] - quarters[index - 1]
        seconds.append(
            seconds[-1] + span * 60.0 / tempo_map.entries[index - 1].bpm
        )
    return quarters, seconds


def _snap_quarter(value: float, boundaries: Iterable[float]) -> float:
    for boundary in boundaries:
        if math.isclose(
            value,
            boundary,
            rel_tol=0.0,
            abs_tol=_QUARTER_BOUNDARY_TOLERANCE,
        ):
            return boundary
    return value


def _coordinate_at_quarter(
    tempo_map: TempoMap,
    quarter: float,
    *,
    seconds: float,
) -> MusicalCoordinate:
    meter_entries = [
        entry for entry in tempo_map.entries if entry.changes_meter
    ]
    meter_quarters = [
        tempo_map.quarter_at(entry.bar, 1.0) for entry in meter_entries
    ]
    quarter = _snap_quarter(quarter, meter_quarters)
    meter_index = bisect_right(meter_quarters, quarter) - 1
    meter = meter_entries[meter_index]
    relative_quarters = quarter - meter_quarters[meter_index]
    quarters_per_bar = meter.quarters_per_bar

    bar_fraction = relative_quarters / quarters_per_bar
    nearest_bar = round(bar_fraction)
    if math.isclose(
        relative_quarters,
        nearest_bar * quarters_per_bar,
        rel_tol=0.0,
        abs_tol=_QUARTER_BOUNDARY_TOLERANCE,
    ):
        bar_offset = int(nearest_bar)
        within_bar = 0.0
    else:
        bar_offset = math.floor(bar_fraction)
        within_bar = relative_quarters - bar_offset * quarters_per_bar

    beat = 1.0 + within_bar / meter.quarters_per_beat
    nearest_beat = round(beat)
    if math.isclose(
        beat,
        nearest_beat,
        rel_tol=0.0,
        abs_tol=_QUARTER_BOUNDARY_TOLERANCE,
    ):
        beat = float(nearest_beat)
    return MusicalCoordinate(
        bar=meter.bar + int(bar_offset),
        beat=beat,
        absolute_quarter=quarter,
        seconds=seconds,
    )


def coordinate_at_position(
    tempo_map: TempoMap,
    bar: int,
    beat: float = 1.0,
) -> MusicalCoordinate:
    """Resolve one valid notation position to logical quarter and seconds."""

    validate_tempo_map_coordinates(tempo_map)
    validate_bar_beat(tempo_map, bar, beat)
    quarter = tempo_map.quarter_at(bar, beat)
    return MusicalCoordinate(
        bar=bar,
        beat=float(beat),
        absolute_quarter=quarter,
        seconds=tempo_map.seconds_at_quarter(quarter),
    )


def coordinate_at_seconds(
    tempo_map: TempoMap,
    seconds: float,
) -> MusicalCoordinate:
    """Invert logical seconds to a notation coordinate.

    The tempo curve is piecewise linear in absolute quarter notes.  At an exact
    tempo or meter boundary, ``bisect_right`` selects the segment beginning at
    that boundary; at an exact bar line the result is therefore the next bar's
    beat 1, never the previous bar's excluded end beat.
    """

    checked_seconds = _finite_number(seconds, path="seconds")
    if checked_seconds < 0.0:
        raise ScoreTimeError("seconds must not be negative")
    validate_tempo_map_coordinates(tempo_map)
    tempo_quarters, tempo_seconds = _tempo_boundaries(tempo_map)
    tempo_index = bisect_right(tempo_seconds, checked_seconds) - 1
    entry = tempo_map.entries[tempo_index]
    quarter = tempo_quarters[tempo_index] + (
        checked_seconds - tempo_seconds[tempo_index]
    ) * entry.bpm / 60.0
    quarter = _snap_quarter(quarter, tempo_quarters)
    return _coordinate_at_quarter(
        tempo_map,
        quarter,
        seconds=checked_seconds,
    )


def seconds_window_around(
    center_seconds: float,
    *,
    before_seconds: float = 5.0,
    after_seconds: float = 5.0,
    maximum_seconds: float | None = None,
) -> SecondsRange:
    """Build a non-negative listening/query window around one clock time.

    The start is clipped to zero.  When ``maximum_seconds`` is supplied, both
    the center and resulting end must lie within (or are clipped to) that known
    media duration.
    """

    center = _finite_number(center_seconds, path="center_seconds")
    before = _finite_number(before_seconds, path="before_seconds")
    after = _finite_number(after_seconds, path="after_seconds")
    if center < 0.0:
        raise ScoreTimeError("center_seconds must not be negative")
    if before < 0.0 or after < 0.0:
        raise ScoreTimeError("window padding must not be negative")
    maximum = None
    if maximum_seconds is not None:
        maximum = _finite_number(maximum_seconds, path="maximum_seconds")
        if maximum < 0.0:
            raise ScoreTimeError("maximum_seconds must not be negative")
        if center > maximum:
            raise ScoreTimeError(
                "center_seconds must not exceed maximum_seconds"
            )
    start = max(0.0, center - before)
    end = center + after
    if maximum is not None:
        start = min(start, maximum)
        end = min(end, maximum)
    return SecondsRange(start_seconds=start, end_seconds=end)


def seconds_range_for_positions(
    tempo_map: TempoMap,
    *,
    start_bar: int,
    start_beat: float = 1.0,
    end_bar: int,
    end_beat: float = 1.0,
    pre_roll_seconds: float = 0.0,
    post_roll_seconds: float = 0.0,
    maximum_seconds: float | None = None,
) -> SecondsRange:
    """Convert a musical region to a padded, clipped logical-second window."""

    start = coordinate_at_position(tempo_map, start_bar, start_beat)
    end = coordinate_at_position(tempo_map, end_bar, end_beat)
    if end.absolute_quarter < start.absolute_quarter:
        raise ScoreTimeError("musical window end must not precede its start")
    before = _finite_number(pre_roll_seconds, path="pre_roll_seconds")
    after = _finite_number(post_roll_seconds, path="post_roll_seconds")
    if before < 0.0 or after < 0.0:
        raise ScoreTimeError("window padding must not be negative")
    start_seconds = max(0.0, start.seconds - before)
    end_seconds = end.seconds + after
    if maximum_seconds is not None:
        maximum = _finite_number(maximum_seconds, path="maximum_seconds")
        if maximum < 0.0:
            raise ScoreTimeError("maximum_seconds must not be negative")
        start_seconds = min(start_seconds, maximum)
        end_seconds = min(end_seconds, maximum)
    return SecondsRange(
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )


__all__ = [
    "MusicalCoordinate",
    "ScoreTimeError",
    "SecondsRange",
    "coordinate_at_position",
    "coordinate_at_seconds",
    "seconds_range_for_positions",
    "seconds_window_around",
    "validate_bar_beat",
    "validate_optional_bar_beat",
    "validate_score_time_coordinates",
    "validate_tempo_map_coordinates",
]
