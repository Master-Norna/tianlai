from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .tuning import EqualTemperament


@dataclass(slots=True)
class _Voice:
    frequency: float
    amplitude: float
    phase: float = 0.0
    envelope: float = 0.0
    released: bool = False
    pending_release: bool = False
    release_step: float = 0.0


class OscillatorInstrument(Instrument):
    """A precisely tuned additive instrument used to validate the engine."""

    def __init__(
        self,
        sample_rate: int,
        *,
        harmonics: tuple[float, ...] = (1.0,),
        attack_seconds: float = 0.005,
        release_seconds: float = 0.2,
        gain: float = 0.2,
        velocity_exponent: float = 1.5,
        pan: float = 0.0,
    ) -> None:
        super().__init__(sample_rate)
        if not harmonics or sum(abs(value) for value in harmonics) == 0.0:
            raise ValueError("harmonics must contain audible values")
        self.harmonics = harmonics
        self.harmonic_normalizer = sum(abs(value) for value in harmonics)
        self.attack_samples = max(1, round(attack_seconds * sample_rate))
        self.release_samples = max(1, round(release_seconds * sample_rate))
        self.gain = gain
        self.velocity_exponent = velocity_exponent
        self.pan = min(1.0, max(-1.0, pan))
        self.sustain_pedal = 0.0
        self.voices: dict[int, _Voice] = {}

    @classmethod
    def from_manifest(cls, data: dict[str, Any], sample_rate: int) -> "OscillatorInstrument":
        return cls(
            sample_rate,
            harmonics=tuple(float(value) for value in data.get("harmonics", [1.0])),
            attack_seconds=float(data.get("attack_seconds", 0.005)),
            release_seconds=float(data.get("release_seconds", 0.2)),
            gain=float(data.get("gain", 0.2)),
            velocity_exponent=float(data.get("velocity_exponent", 1.5)),
            pan=float(data.get("pan", 0.0)),
        )

    def _begin_release(self, voice: _Voice) -> None:
        if not voice.released:
            voice.released = True
            voice.pending_release = False
            voice.release_step = max(voice.envelope, 1.0 / self.release_samples) / self.release_samples

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            note_id = int(event.payload["note_id"])
            velocity = float(event.payload["velocity"])
            self.voices[note_id] = _Voice(
                frequency=event_pitch_hz(event, tuning),
                amplitude=self.gain * (velocity**self.velocity_exponent),
            )
        elif event.type == "note_off":
            voice = self.voices.get(int(event.payload["note_id"]))
            if voice is not None:
                if self.sustain_pedal >= 0.5:
                    voice.pending_release = True
                else:
                    self._begin_release(voice)
        elif event.type == "control" and event.payload["name"] == "sustain_pedal":
            previous = self.sustain_pedal
            self.sustain_pedal = float(event.payload["value"])
            if previous >= 0.5 and self.sustain_pedal < 0.5:
                for voice in self.voices.values():
                    if voice.pending_release:
                        self._begin_release(voice)

    def render_frame(self) -> StereoFrame:
        mono = 0.0
        finished: list[int] = []
        for note_id, voice in self.voices.items():
            if voice.released:
                voice.envelope = max(0.0, voice.envelope - voice.release_step)
            else:
                voice.envelope = min(1.0, voice.envelope + 1.0 / self.attack_samples)

            if voice.envelope <= 0.0 and voice.released:
                finished.append(note_id)
                continue

            partials = 0.0
            for harmonic_number, weight in enumerate(self.harmonics, start=1):
                partials += weight * math.sin(voice.phase * harmonic_number)
            mono += voice.amplitude * voice.envelope * partials / self.harmonic_normalizer
            voice.phase = (voice.phase + math.tau * voice.frequency / self.sample_rate) % math.tau

        for note_id in finished:
            del self.voices[note_id]

        angle = (self.pan + 1.0) * math.pi / 4.0
        return mono * math.cos(angle), mono * math.sin(angle)

    @property
    def active_voice_count(self) -> int:
        return len(self.voices)

    def runtime_variant_contract(self) -> dict[str, Any]:
        """Prove that this exact built-in backend has no audio-choice domain."""

        return {
            "schema_version": 1,
            "kind": "top_level_runtime_variant_contract",
            "backend": "builtin_oscillator",
            "audio_selection_model": "code_deterministic_no_runtime_choices",
            "capture_completeness": "no_audio_selection_components",
            "expected_component_sha256s": [],
            "expected_selection_count": 0,
        }
