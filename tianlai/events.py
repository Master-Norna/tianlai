from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .tuning import EqualTemperament


_PERFORMANCE_FIELDS = frozenset(
    {
        "sample_rate",
        "channels",
        "duration_seconds",
        "tail_seconds",
        "tuning",
        "events",
    }
)
_TUNING_FIELDS = frozenset({"temperament", "a4_hz"})
_EVENT_FIELDS = {
    "note_on": frozenset(
        {
            "time",
            "type",
            "note_id",
            "midi_note",
            "pitch_hz",
            "velocity",
            "source_event_id",
        }
    ),
    "note_off": frozenset(
        {"time", "type", "note_id", "release_velocity", "source_event_id"}
    ),
    "control": frozenset({"time", "type", "name", "value"}),
    "articulation": frozenset({"time", "type", "name"}),
}


@dataclass(frozen=True, slots=True)
class PerformanceEvent:
    sample: int
    sequence: int
    type: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PerformanceDocument:
    sample_rate: int
    channels: int
    total_samples: int
    events: tuple[PerformanceEvent, ...]
    tuning: EqualTemperament


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    return number


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def _required_integer(
    value: dict[str, Any],
    key: str,
    field: str,
) -> int:
    if key not in value:
        raise ValueError(f"{field} is required")
    return _integer(value[key], field)


def _unit_float(value: object, field: str) -> float:
    number = _finite_float(value, field)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{field} must be between 0 and 1")
    return number


def _reject_unknown_fields(
    value: dict[str, Any],
    allowed: frozenset[str],
    field: str,
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"{field} contains unknown fields: {', '.join(unknown)}")


def parse_performance_document(data: dict[str, Any]) -> PerformanceDocument:
    if not isinstance(data, dict):
        raise ValueError("performance document must be an object")
    _reject_unknown_fields(data, _PERFORMANCE_FIELDS, "performance document")

    sample_rate = _integer(data.get("sample_rate", 48_000), "sample_rate")
    channels = _integer(data.get("channels", 2), "channels")
    if sample_rate < 8_000 or sample_rate > 384_000:
        raise ValueError("sample_rate must be between 8000 and 384000")
    if channels != 2:
        raise ValueError("the current renderer supports exactly 2 output channels")

    from .tuning import tuning_from_document

    raw_tuning = data.get("tuning")
    if raw_tuning is not None:
        if not isinstance(raw_tuning, dict):
            raise ValueError("tuning must be an object")
        _reject_unknown_fields(raw_tuning, _TUNING_FIELDS, "tuning")
        raw_tuning = dict(raw_tuning)
        if "a4_hz" in raw_tuning:
            raw_tuning["a4_hz"] = _finite_float(
                raw_tuning["a4_hz"],
                "tuning.a4_hz",
            )
    tuning = tuning_from_document(raw_tuning)
    raw_events = data.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("events must be an array")

    events: list[PerformanceEvent] = []
    active_note_sources: dict[int, str | None] = {}
    last_time = 0.0
    previous_time = 0.0
    for sequence, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            raise ValueError(f"events[{sequence}] must be an object")
        event_type = str(raw.get("type", ""))
        allowed_fields = _EVENT_FIELDS.get(event_type)
        if allowed_fields is None:
            raise ValueError(f"unsupported event type: {event_type!r}")
        _reject_unknown_fields(raw, allowed_fields, f"events[{sequence}]")
        time = _finite_float(raw.get("time", 0.0), f"events[{sequence}].time")
        if time < 0.0:
            raise ValueError(f"events[{sequence}].time must not be negative")
        if time < previous_time:
            raise ValueError("events must be ordered by non-decreasing time")
        previous_time = time
        last_time = max(last_time, time)
        payload = dict(raw)
        payload.pop("time", None)
        payload.pop("type", None)
        if "source_event_id" in payload:
            source_event_id = payload["source_event_id"]
            if not isinstance(source_event_id, str) or not source_event_id.strip():
                raise ValueError(
                    f"events[{sequence}].source_event_id must be a non-empty string"
                )

        if event_type == "note_on":
            note_id = _required_integer(
                payload,
                "note_id",
                f"events[{sequence}].note_id",
            )
            payload["note_id"] = note_id
            if note_id in active_note_sources:
                raise ValueError(f"note_id {note_id} is already active")
            active_note_sources[note_id] = payload.get("source_event_id")
            has_pitch_hz = "pitch_hz" in payload
            has_midi_note = "midi_note" in payload
            if has_pitch_hz == has_midi_note:
                raise ValueError("note_on requires exactly one of pitch_hz or midi_note")
            if "pitch_hz" in payload and _finite_float(payload["pitch_hz"], "pitch_hz") <= 0.0:
                raise ValueError("pitch_hz must be positive")
            if "midi_note" in payload:
                payload["midi_note"] = _finite_float(payload["midi_note"], "midi_note")
            payload["velocity"] = _unit_float(payload.get("velocity", 0.8), "velocity")
        elif event_type == "note_off":
            note_id = _required_integer(
                payload,
                "note_id",
                f"events[{sequence}].note_id",
            )
            payload["note_id"] = note_id
            if note_id not in active_note_sources:
                raise ValueError(f"note_id {note_id} is not active")
            if (
                payload.get("source_event_id")
                != active_note_sources[note_id]
            ):
                raise ValueError(
                    "note_off source_event_id must match the corresponding "
                    f"note_on for note_id {note_id}"
                )
            del active_note_sources[note_id]
            # release_velocity is optional by protocol.  Missing means that
            # the score/performance did not describe key-release speed; it
            # must remain distinguishable from an explicit neutral 0.5 so
            # each backend can apply its own documented fallback.  In
            # particular, SFZ trigger=release follows the corresponding
            # note-on velocity rather than MIDI note-off velocity.
            if "release_velocity" in payload:
                payload["release_velocity"] = _unit_float(
                    payload["release_velocity"], "release_velocity"
                )
        elif event_type == "control":
            name = str(payload.get("name", ""))
            if not name:
                raise ValueError("control requires a non-empty name")
            payload["name"] = name
            payload["value"] = _unit_float(payload.get("value"), "control value")
        elif event_type == "articulation":
            name = str(payload.get("name", ""))
            if not name:
                raise ValueError("articulation requires a non-empty name")
            payload["name"] = name
        events.append(
            PerformanceEvent(
                sample=round(time * sample_rate),
                sequence=sequence,
                type=event_type,
                payload=payload,
            )
        )

    events.sort(key=lambda event: (event.sample, event.sequence))
    tail_seconds = _finite_float(data.get("tail_seconds", 1.0), "tail_seconds")
    if tail_seconds < 0.0:
        raise ValueError("tail_seconds must not be negative")
    natural_duration = last_time + tail_seconds
    duration_seconds = _finite_float(
        data.get("duration_seconds", natural_duration), "duration_seconds"
    )
    if duration_seconds < last_time:
        raise ValueError("duration_seconds must not end before the final event")
    total_samples = max(1, round(duration_seconds * sample_rate))

    return PerformanceDocument(
        sample_rate=sample_rate,
        channels=channels,
        total_samples=total_samples,
        events=tuple(events),
        tuning=tuning,
    )


def event_pitch_hz(event: PerformanceEvent, tuning: EqualTemperament) -> float:
    if "pitch_hz" in event.payload:
        return float(event.payload["pitch_hz"])
    return tuning.note_to_hz(float(event.payload["midi_note"]))
