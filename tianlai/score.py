"""Score document: the instrument-neutral musical layer.

A score states what the music *is* — bars, beats, pitches, dynamic marks and
articulation marks — and deliberately knows nothing about which instrument
plays it.  Binding parts to instruments happens in :mod:`tianlai.roster`, and
turning marks into concrete velocities and onset times happens in
:mod:`tianlai.conductor`.

Keeping this layer instrument-free is what lets one score be rendered by
different ensembles, and what lets a reviewer diff the music without wading
through performance detail.

Two conventions are fixed here because leaving them implicit causes silent
misreads later:

* ``bpm`` always counts **quarter notes** per minute, whatever the meter is.
* ``beat`` and ``duration_beats`` are counted in the **meter's beat unit**, so
  in 6/8 a ``duration_beats`` of 3 is a dotted quarter.
"""

from __future__ import annotations

from bisect import bisect_right
import copy
from dataclasses import dataclass, field
import math
from typing import Any


_DYNAMIC_MARKS = ("ppp", "pp", "p", "mp", "mf", "f", "ff", "fff")
_NOTE_OFFSETS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SCORE_KEYS = frozenset(
    (
        "schema_version",
        "title",
        "sample_rate",
        "tail_seconds",
        "tuning",
        "tempo_map",
        "parts",
    )
)
_TEMPO_KEYS = frozenset(("bar", "beat", "bpm", "beats_per_bar", "beat_unit"))
_PART_KEYS = frozenset(
    (
        "id",
        "name",
        "notes",
        "phrases",
        "default_dynamic",
        "default_articulation",
    )
)
_NOTE_KEYS = frozenset(
    (
        "bar",
        "beat",
        "duration_beats",
        "pitch",
        "dynamic",
        "velocity",
        "articulation",
        "tie",
        "staff",
        "voice",
        "event_id",
    )
)
_PHRASE_KEYS = frozenset(("start_bar", "start_beat", "end_bar", "end_beat"))
_TUNING_KEYS = frozenset(("temperament", "a4_hz"))


def _reject_unknown_keys(
    mapping: dict[Any, Any], allowed: frozenset[str], path: str
) -> None:
    """Reject misspelled document fields with their complete JSON path."""

    locations: list[str] = []
    unknown_count = 0
    for key in mapping:
        if key in allowed:
            continue
        unknown_count += 1
        if len(locations) >= 8:
            continue
        if type(key) is str:
            preview = key if len(key) <= 80 else f"{key[:77]}..."
        else:
            preview = f"<{type(key).__name__}>"
        locations.append(f"{path}.{preview}")
    if unknown_count:
        suffix = (
            ""
            if unknown_count == len(locations)
            else f"，另有 {unknown_count - len(locations)} 项"
        )
        raise ValueError(
            f"{path} 包含未知字段: {', '.join(locations)}{suffix}"
        )


def parse_pitch(value: object) -> float:
    """Accept ``60``, ``60.5`` or scientific names such as ``C4`` / ``Bb3``."""

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("pitch must be finite")
        return number
    text = str(value).strip()
    if not text:
        raise ValueError("pitch must not be empty")
    letter = text[0].upper()
    if letter not in _NOTE_OFFSETS:
        raise ValueError(f"unknown pitch name: {value!r}")
    index = 1
    accidental = 0
    while index < len(text) and text[index] in "#b♯♭":
        accidental += 1 if text[index] in "#♯" else -1
        index += 1
    octave_text = text[index:]
    if not octave_text.lstrip("-").isdigit():
        raise ValueError(f"pitch {value!r} is missing an octave number")
    octave = int(octave_text)
    # 科学音高记号法:C4 = 中央 C = MIDI 60。
    return float((octave + 1) * 12 + _NOTE_OFFSETS[letter] + accidental)


def pitch_name(midi: float) -> str:
    """Render a MIDI number back to a readable name for diagnostics."""

    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    rounded = int(round(midi))
    return f"{names[rounded % 12]}{rounded // 12 - 1}"


@dataclass(frozen=True, slots=True)
class TempoEntry:
    """One tempo and/or meter change.

    Meter may only change at a downbeat, which is a musical fact.  Tempo may
    change anywhere, which matters for imported performances: a rubato passage
    speeds up and slows down mid-bar, and snapping those changes to bar lines
    would visibly rewrite the timing.
    """

    bar: int
    bpm: float
    beats_per_bar: int
    beat_unit: int
    beat: float = 1.0

    @property
    def changes_meter(self) -> bool:
        return math.isclose(self.beat, 1.0, abs_tol=1e-9)

    @property
    def quarters_per_beat(self) -> float:
        return 4.0 / float(self.beat_unit)

    @property
    def quarters_per_bar(self) -> float:
        return float(self.beats_per_bar) * self.quarters_per_beat


@dataclass(frozen=True, slots=True)
class TempoMap:
    entries: tuple[TempoEntry, ...]
    _entry_bars: tuple[int, ...] = field(
        init=False, repr=False, compare=False
    )
    _meter_bars: tuple[int, ...] = field(
        init=False, repr=False, compare=False
    )
    _meter_entries: tuple[TempoEntry, ...] = field(
        init=False, repr=False, compare=False
    )
    _meter_quarters: tuple[float, ...] = field(
        init=False, repr=False, compare=False
    )
    _tempo_quarters: tuple[float, ...] = field(
        init=False, repr=False, compare=False
    )
    _tempo_seconds: tuple[float, ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Build the immutable time index once, not once per note query.

        A dense imported tempo curve can contain thousands of mid-bar tempo
        changes.  Rebuilding every boundary through ``quarter_at`` made one
        seconds lookup quadratic in that curve.  These parallel arrays keep
        public lookup semantics while making bar, quarter and seconds queries
        logarithmic after one linear construction pass.
        """

        object.__setattr__(
            self,
            "_entry_bars",
            tuple(entry.bar for entry in self.entries),
        )
        if not self.entries:
            object.__setattr__(self, "_meter_bars", ())
            object.__setattr__(self, "_meter_entries", ())
            object.__setattr__(self, "_meter_quarters", ())
            object.__setattr__(self, "_tempo_quarters", ())
            object.__setattr__(self, "_tempo_seconds", ())
            return

        # ``quarter_at`` historically treats the first entry as the meter at
        # bar 1.  The parser separately requires that entry to be bar 1 beat 1;
        # retaining the convention here keeps manually constructed invalid
        # maps inspectable by the dedicated score-time validator.
        meter_bars = [1]
        meter_entries = [self.entries[0]]
        meter_quarters = [0.0]
        for entry in self.entries[1:]:
            if not entry.changes_meter:
                continue
            previous = meter_entries[-1]
            try:
                quarter = meter_quarters[-1] + (
                    entry.bar - meter_bars[-1]
                ) * previous.quarters_per_bar
            except OverflowError as exc:
                raise ValueError(
                    "tempo map exceeds the finite score-time range"
                ) from exc
            if not math.isfinite(quarter):
                raise ValueError(
                    "tempo map exceeds the finite score-time range"
                )
            meter_bars.append(entry.bar)
            meter_entries.append(entry)
            meter_quarters.append(quarter)

        tempo_quarters: list[float] = []
        meter_position = 0
        for entry in self.entries:
            while (
                meter_position + 1 < len(meter_bars)
                and meter_bars[meter_position + 1] <= entry.bar
            ):
                meter_position += 1
            meter = meter_entries[meter_position]
            try:
                quarter = (
                    meter_quarters[meter_position]
                    + (entry.bar - meter_bars[meter_position])
                    * meter.quarters_per_bar
                    + (entry.beat - 1.0) * meter.quarters_per_beat
                )
            except OverflowError as exc:
                raise ValueError(
                    "tempo map exceeds the finite score-time range"
                ) from exc
            if not math.isfinite(quarter):
                raise ValueError(
                    "tempo map exceeds the finite score-time range"
                )
            tempo_quarters.append(quarter)
        tempo_seconds = [0.0]
        for position in range(1, len(self.entries)):
            span = tempo_quarters[position] - tempo_quarters[position - 1]
            seconds = (
                tempo_seconds[-1]
                + span * 60.0 / self.entries[position - 1].bpm
            )
            if not math.isfinite(seconds):
                raise ValueError(
                    "tempo map exceeds the finite score-time range"
                )
            tempo_seconds.append(seconds)

        object.__setattr__(self, "_meter_bars", tuple(meter_bars))
        object.__setattr__(self, "_meter_entries", tuple(meter_entries))
        object.__setattr__(self, "_meter_quarters", tuple(meter_quarters))
        object.__setattr__(self, "_tempo_quarters", tuple(tempo_quarters))
        object.__setattr__(self, "_tempo_seconds", tuple(tempo_seconds))

    def entry_at_bar(self, bar: int) -> TempoEntry:
        if not self.entries:
            raise IndexError("tempo map has no entries")
        position = bisect_right(self._entry_bars, bar) - 1
        return self.entries[max(0, position)]

    def meter_entry_at_bar(self, bar: int) -> TempoEntry:
        """Return the latest downbeat meter declaration governing ``bar``."""

        if not self.entries:
            raise IndexError("tempo map has no entries")
        position = bisect_right(self._meter_bars, bar) - 1
        return self._meter_entries[max(0, position)]

    def quarter_at(self, bar: int, beat: float) -> float:
        """Absolute position in quarter notes from the start of the piece."""

        if bar < 1:
            raise ValueError("bar numbers start at 1")
        if beat < 1.0:
            raise ValueError("beat numbers start at 1")
        if not self.entries:
            raise IndexError("tempo map has no entries")
        position = bisect_right(self._meter_bars, bar) - 1
        position = max(0, position)
        current = self._meter_entries[position]
        quarters = self._meter_quarters[position] + (
            bar - self._meter_bars[position]
        ) * current.quarters_per_bar
        return quarters + (beat - 1.0) * current.quarters_per_beat

    def seconds_at_quarter(self, quarter: float) -> float:
        """Convert a quarter-note position to seconds through the tempo map."""

        if quarter < 0.0:
            raise ValueError("quarter position must not be negative")
        if not self.entries:
            return 0.0
        position = bisect_right(self._tempo_quarters, quarter) - 1
        if position < 0:
            return 0.0
        return self._tempo_seconds[position] + (
            quarter - self._tempo_quarters[position]
        ) * 60.0 / self.entries[position].bpm

    def seconds_at(self, bar: int, beat: float) -> float:
        return self.seconds_at_quarter(self.quarter_at(bar, beat))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [
                {
                    "bar": entry.bar,
                    "beat": entry.beat,
                    "bpm": entry.bpm,
                    "beats_per_bar": entry.beats_per_bar,
                    "beat_unit": entry.beat_unit,
                }
                for entry in self.entries
            ]
        }


@dataclass(frozen=True, slots=True)
class ScoreNote:
    """One notated note.  Dynamics stay symbolic on purpose.

    ``dynamic`` is a mark such as ``mf``, not a number, because deciding how
    loud ``mf`` is on this instrument in this passage is the conductor's job.
    Storing a number here would freeze an interpretation into the score.

    ``velocity`` is the deliberate exception, and exists for imported
    performances.  A MIDI file records what a player actually did, note by
    note; folding that into eight dynamic marks and letting the conductor
    re-expand it would discard real information and invent different
    information in its place.  When present it replaces the mark's base
    velocity, and the conductor's other tiers still apply on top.
    """

    index: int
    bar: int
    beat: float
    duration_beats: float
    midi: float
    dynamic: str | None
    articulation: str | None
    tie: bool
    staff: int | None = None
    voice: str | None = None
    velocity: float | None = None
    source_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Keep conventional integer pitches readable, but never use the
        # diagnostic ``pitch_name`` rounding for a microtonal score value.
        # A score note is an editable source object, so even a very small
        # fractional offset must survive parse -> serialise -> parse.
        pitch: str | float = (
            pitch_name(self.midi) if self.midi.is_integer() else self.midi
        )
        data: dict[str, Any] = {
            "bar": self.bar,
            "beat": self.beat,
            "duration_beats": self.duration_beats,
            "pitch": pitch,
        }
        if self.source_event_id is not None:
            data["event_id"] = self.source_event_id
        if self.dynamic is not None:
            data["dynamic"] = self.dynamic
        if self.articulation is not None:
            data["articulation"] = self.articulation
        if self.tie:
            data["tie"] = True
        if self.staff is not None:
            data["staff"] = self.staff
        if self.voice is not None:
            data["voice"] = self.voice
        if self.velocity is not None:
            data["velocity"] = self.velocity
        return data


@dataclass(frozen=True, slots=True)
class Phrase:
    start_bar: int
    start_beat: float
    end_bar: int
    end_beat: float


@dataclass(frozen=True, slots=True)
class ScorePart:
    id: str
    name: str
    notes: tuple[ScoreNote, ...]
    phrases: tuple[Phrase, ...] = field(default=())
    default_dynamic: str = "mf"
    default_articulation: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreDocument:
    title: str
    sample_rate: int
    tempo_map: TempoMap
    parts: tuple[ScorePart, ...]
    tuning: dict[str, Any]
    tail_seconds: float
    schema_version: int | None = None

    @property
    def identity_contract(self) -> str:
        """Return the score's source-event identity contract.

        Consumers must branch on this capability rather than inferring it
        from a schema version.  Keeping the legacy and v1 values explicit
        also makes a future score version choose its identity semantics at
        the parser boundary instead of accidentally falling back to legacy
        positional identity.
        """

        if self.schema_version is None:
            return "legacy-position-v0"
        if self.schema_version == 1:
            return "stable-event-v1"
        raise ValueError(
            "score identity contract is undefined for schema_version "
            f"{self.schema_version!r}"
        )

    @property
    def has_stable_event_identity(self) -> bool:
        """Whether score events carry stable document-local identities."""

        return self.identity_contract == "stable-event-v1"

    @property
    def time_contract(self) -> str:
        """Return the logical score-time contract used by this document."""

        if self.schema_version is None:
            return "legacy-float-bar-beat-v0"
        if self.schema_version == 1:
            return "float-bar-beat-v1"
        raise ValueError(
            "score time contract is undefined for schema_version "
            f"{self.schema_version!r}"
        )

    def part(self, part_id: str) -> ScorePart:
        for candidate in self.parts:
            if candidate.id == part_id:
                return candidate
        raise ValueError(f"score has no part {part_id!r}")


def _positive_int(value: object, field_name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
    ):
        raise ValueError(f"{field_name} must be at least 1")
    return value


def _finite_float(value: object, field_name: str) -> float:
    """Parse one JSON number without accepting booleans, NaN or infinity."""

    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be a finite number")
    return number


def _parse_tempo_map(raw: object) -> TempoMap:
    if not isinstance(raw, list) or not raw:
        raise ValueError("tempo_map must be a non-empty array")
    entries: list[TempoEntry] = []
    bpm = 0.0
    beats_per_bar = 0
    beat_unit = 0
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"tempo_map[{position}] must be an object")
        _reject_unknown_keys(item, _TEMPO_KEYS, f"score.tempo_map[{position}]")
        bar = _positive_int(item.get("bar", 1), f"tempo_map[{position}].bar")
        beat = _finite_float(
            item.get("beat", 1.0),
            f"tempo_map[{position}].beat",
        )
        if beat < 1.0:
            raise ValueError(f"tempo_map[{position}].beat must be at least 1")
        if position == 0 and (bar != 1 or beat != 1.0):
            raise ValueError("the first tempo_map entry must start at bar 1 beat 1")
        if beat != 1.0 and ("beats_per_bar" in item or "beat_unit" in item):
            raise ValueError(
                f"tempo_map[{position}] 想在小节中途改拍号;拍号只能在小节线上改变"
            )
        if "bpm" in item:
            bpm = _finite_float(
                item["bpm"],
                f"tempo_map[{position}].bpm",
            )
            if not 1.0 <= bpm <= 600.0:
                raise ValueError(f"tempo_map[{position}].bpm must be between 1 and 600")
        elif position == 0:
            raise ValueError("the first tempo_map entry must declare bpm")
        if "beats_per_bar" in item:
            beats_per_bar = _positive_int(
                item["beats_per_bar"], f"tempo_map[{position}].beats_per_bar"
            )
        elif position == 0:
            raise ValueError("the first tempo_map entry must declare beats_per_bar")
        if "beat_unit" in item:
            beat_unit = _positive_int(item["beat_unit"], f"tempo_map[{position}].beat_unit")
            if beat_unit not in (1, 2, 4, 8, 16, 32):
                raise ValueError(f"tempo_map[{position}].beat_unit must be a power of two")
        elif position == 0:
            raise ValueError("the first tempo_map entry must declare beat_unit")
        if entries and (bar, beat) <= (entries[-1].bar, entries[-1].beat):
            raise ValueError(
                "tempo_map entries must be ordered by increasing bar and beat"
            )
        entries.append(
            TempoEntry(
                bar=bar,
                bpm=bpm,
                beats_per_bar=beats_per_bar,
                beat_unit=beat_unit,
                beat=beat,
            )
        )
    return TempoMap(entries=tuple(entries))


def _parse_note(
    raw: object,
    index: int,
    part_id: str,
    path: str,
    schema_version: int | None,
) -> ScoreNote:
    if not isinstance(raw, dict):
        raise ValueError(f"part {part_id!r} note {index} must be an object")
    _reject_unknown_keys(raw, _NOTE_KEYS, path)
    bar = _positive_int(raw.get("bar", 1), f"part {part_id!r} note {index} bar")
    beat = _finite_float(
        raw.get("beat", 1.0),
        f"part {part_id!r} note {index} beat",
    )
    if beat < 1.0:
        raise ValueError(f"part {part_id!r} note {index} beat must be at least 1")
    duration = _finite_float(
        raw.get("duration_beats", 1.0),
        f"part {part_id!r} note {index} duration_beats",
    )
    if not duration > 0.0:
        raise ValueError(f"part {part_id!r} note {index} duration_beats must be positive")
    if "pitch" not in raw:
        raise ValueError(f"part {part_id!r} note {index} requires a pitch")
    dynamic = raw.get("dynamic")
    if dynamic is not None and str(dynamic) not in _DYNAMIC_MARKS:
        raise ValueError(
            f"part {part_id!r} note {index} has unknown dynamic {dynamic!r}; "
            f"expected one of {', '.join(_DYNAMIC_MARKS)}"
        )
    articulation = raw.get("articulation")
    if articulation is not None and not isinstance(articulation, str):
        raise ValueError(
            f"part {part_id!r} note {index} articulation must be a string"
        )
    raw_tie = raw.get("tie", False)
    if not isinstance(raw_tie, bool):
        raise ValueError(
            f"part {part_id!r} note {index} tie must be boolean"
        )
    raw_staff = raw.get("staff")
    staff = (
        None
        if raw_staff is None
        else _positive_int(
            raw_staff,
            f"part {part_id!r} note {index} staff",
        )
    )
    raw_voice = raw.get("voice")
    if raw_voice is None:
        voice = None
    elif not isinstance(raw_voice, str) or not raw_voice.strip():
        raise ValueError(
            f"part {part_id!r} note {index} voice must be a non-empty string"
        )
    else:
        voice = raw_voice.strip()
    velocity = raw.get("velocity")
    if velocity is not None:
        velocity = _finite_float(
            velocity,
            f"part {part_id!r} note {index} velocity",
        )
        if not 0.0 < velocity <= 1.0:
            raise ValueError(
                f"part {part_id!r} note {index} velocity must be within (0, 1]"
            )
    raw_event_id = raw.get("event_id")
    if schema_version == 1:
        if not isinstance(raw_event_id, str) or not raw_event_id.strip():
            raise ValueError(f"{path}.event_id must be a non-empty string in score v1")
        source_event_id = raw_event_id
    else:
        if "event_id" in raw:
            raise ValueError(f"{path}.event_id requires score.schema_version 1")
        source_event_id = None
    return ScoreNote(
        index=index,
        bar=bar,
        beat=beat,
        duration_beats=duration,
        midi=parse_pitch(raw["pitch"]),
        dynamic=None if dynamic is None else str(dynamic),
        articulation=None if articulation is None else str(articulation),
        tie=raw_tie,
        staff=staff,
        voice=voice,
        velocity=velocity,
        source_event_id=source_event_id,
    )


def _parse_phrases(
    raw: object, part_id: str, path: str
) -> tuple[Phrase, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"part {part_id!r} phrases must be an array")
    phrases: list[Phrase] = []
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"part {part_id!r} phrases[{position}] must be an object")
        _reject_unknown_keys(
            item,
            _PHRASE_KEYS,
            f"{path}[{position}]",
        )
        phrases.append(
            Phrase(
                start_bar=_positive_int(item["start_bar"], "start_bar"),
                start_beat=_finite_float(
                    item.get("start_beat", 1.0),
                    f"{path}[{position}].start_beat",
                ),
                end_bar=_positive_int(item["end_bar"], "end_bar"),
                end_beat=_finite_float(
                    item.get("end_beat", 1.0),
                    f"{path}[{position}].end_beat",
                ),
            )
        )
    return tuple(phrases)


def parse_score_document(data: dict[str, Any]) -> ScoreDocument:
    """Validate and load a score document, failing loudly on any ambiguity."""

    if not isinstance(data, dict):
        raise ValueError("score must be an object")
    _reject_unknown_keys(data, _SCORE_KEYS, "score")
    if "schema_version" not in data:
        schema_version = None
    else:
        raw_schema_version = data["schema_version"]
        if (
            not isinstance(raw_schema_version, int)
            or isinstance(raw_schema_version, bool)
            or raw_schema_version != 1
        ):
            raise ValueError("score.schema_version must be 1 when present")
        schema_version = raw_schema_version
    raw_sample_rate = data.get("sample_rate", 48_000)
    if (
        isinstance(raw_sample_rate, bool)
        or not isinstance(raw_sample_rate, int)
    ):
        raise ValueError("sample_rate must be an integer")
    sample_rate = raw_sample_rate
    if sample_rate < 8_000 or sample_rate > 384_000:
        raise ValueError("sample_rate must be between 8000 and 384000")
    tempo_map = _parse_tempo_map(data.get("tempo_map"))

    raw_parts = data.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError("parts must be a non-empty array")
    parts: list[ScorePart] = []
    seen: set[str] = set()
    seen_event_ids: set[str] = set()
    for position, raw_part in enumerate(raw_parts):
        if not isinstance(raw_part, dict):
            raise ValueError(f"parts[{position}] must be an object")
        part_path = f"score.parts[{position}]"
        _reject_unknown_keys(raw_part, _PART_KEYS, part_path)
        part_id = str(raw_part.get("id", "")).strip()
        if not part_id:
            raise ValueError(f"parts[{position}] requires a non-empty id")
        if part_id in seen:
            raise ValueError(f"duplicate part id: {part_id}")
        seen.add(part_id)
        raw_notes = raw_part.get("notes")
        if not isinstance(raw_notes, list):
            raise ValueError(f"part {part_id!r} notes must be an array")
        parsed_notes: list[ScoreNote] = []
        for index, raw in enumerate(raw_notes):
            note = _parse_note(
                raw,
                index,
                part_id,
                f"{part_path}.notes[{index}]",
                schema_version,
            )
            if note.source_event_id is not None:
                if note.source_event_id in seen_event_ids:
                    raise ValueError(
                        f"duplicate event_id in score: {note.source_event_id!r}"
                    )
                seen_event_ids.add(note.source_event_id)
            parsed_notes.append(note)
        notes = tuple(parsed_notes)
        # 同一声部内按小节/拍排序,让后续所有层都能假定时间有序;
        # 保留原始下标作为并列时的稳定次序,保证结果可复现。
        notes = tuple(
            sorted(notes, key=lambda note: (note.bar, note.beat, note.midi, note.index))
        )
        default_dynamic = str(raw_part.get("default_dynamic", "mf"))
        if default_dynamic not in _DYNAMIC_MARKS:
            raise ValueError(
                f"part {part_id!r} has unknown default_dynamic {default_dynamic!r}"
            )
        default_articulation = raw_part.get("default_articulation")
        parts.append(
            ScorePart(
                id=part_id,
                name=str(raw_part.get("name", part_id)),
                notes=notes,
                phrases=_parse_phrases(
                    raw_part.get("phrases"),
                    part_id,
                    f"{part_path}.phrases",
                ),
                default_dynamic=default_dynamic,
                default_articulation=(
                    None if default_articulation is None else str(default_articulation)
                ),
            )
        )

    tail_seconds = _finite_float(
        data.get("tail_seconds", 2.0),
        "tail_seconds",
    )
    if tail_seconds < 0.0:
        raise ValueError("tail_seconds must not be negative")
    raw_tuning = data.get("tuning")
    if raw_tuning is not None:
        if not isinstance(raw_tuning, dict):
            raise ValueError("score.tuning must be an object")
        _reject_unknown_keys(raw_tuning, _TUNING_KEYS, "score.tuning")
        # Validate the effective tuning at the score boundary.  Deferring this
        # until rendering would let validation tools approve an unusable score.
        from .tuning import tuning_from_document

        tuning_from_document(raw_tuning)
    return ScoreDocument(
        title=str(data.get("title", "未命名总谱")),
        sample_rate=sample_rate,
        tempo_map=tempo_map,
        parts=tuple(parts),
        tuning=dict(raw_tuning or {"temperament": "equal", "a4_hz": 440.0}),
        tail_seconds=tail_seconds,
        schema_version=schema_version,
    )


def upgrade_legacy_score_to_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Return a validated, detached score-v1 document with stable event IDs.

    Legacy notes receive deterministic document-local IDs in their original
    array traversal order.  The IDs deliberately do not contain or hash
    editable pitch, bar, beat, duration, dynamics, or articulation data.
    Once this returned document is saved, editors must preserve its event IDs
    across musical edits.

    Migrating changes the residual-humanisation identity from the legacy note
    array index to ``event_id``.  Therefore a migrated v1 score remains
    deterministic, but is not promised to render byte-identically to its
    legacy input.  Passing an already valid v1 document is idempotent and
    preserves all existing IDs.
    """

    if not isinstance(data, dict):
        raise ValueError("score must be an object")
    from .score_time import validate_score_time_coordinates

    versioned = "schema_version" in data
    parsed = parse_score_document(data)
    validate_score_time_coordinates(parsed)
    if versioned and parsed.schema_version != 1:
        raise ValueError("only legacy or score v1 documents can be upgraded")

    # Validate before allocating identities so callers never receive a
    # superficially migrated but otherwise invalid score.  The semantic parser
    # intentionally accepts three legacy note shorthands, but score-v1's public
    # JSON Schema requires those fields to be materialised.  Do that for both a
    # legacy input and a parser-valid v1 input so the upgrader is idempotent and
    # never emits a document that only the more permissive internal parser can
    # read.
    upgraded = copy.deepcopy(data)
    upgraded["schema_version"] = 1
    sequence = 1
    for part in upgraded["parts"]:
        for note in part["notes"]:
            note.setdefault("bar", 1)
            note.setdefault("beat", 1.0)
            note.setdefault("duration_beats", 1.0)
            if not versioned:
                note["event_id"] = f"event-{sequence:06d}"
            sequence += 1
    validate_score_time_coordinates(parse_score_document(upgraded))
    return upgraded


def dynamic_marks() -> tuple[str, ...]:
    return _DYNAMIC_MARKS
