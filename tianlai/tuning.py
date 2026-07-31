from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True, slots=True)
class EqualTemperament:
    """Twelve-tone equal temperament with a configurable A4 reference."""

    a4_hz: float = 440.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.a4_hz) or self.a4_hz <= 0.0:
            raise ValueError("a4_hz must be a positive finite number")

    def note_to_hz(self, midi_note: float) -> float:
        if not math.isfinite(midi_note):
            raise ValueError("midi_note must be finite")
        return self.a4_hz * (2.0 ** ((midi_note - 69.0) / 12.0))

    def cents_between(self, lower_hz: float, upper_hz: float) -> float:
        if lower_hz <= 0.0 or upper_hz <= 0.0:
            raise ValueError("frequencies must be positive")
        return 1200.0 * math.log2(upper_hz / lower_hz)


def tuning_from_document(data: dict[str, object] | None) -> EqualTemperament:
    data = data or {}
    temperament = str(data.get("temperament", "equal"))
    if temperament != "equal":
        raise ValueError(f"unsupported temperament: {temperament!r}")
    return EqualTemperament(float(data.get("a4_hz", 440.0)))

