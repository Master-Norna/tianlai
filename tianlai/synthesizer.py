from __future__ import annotations

from dataclasses import dataclass, fields, replace
import math
from typing import Any

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .tuning import EqualTemperament


ENGINE_VERSION = "1.0.0"
_UINT32_MASK = 0xFFFFFFFF


@dataclass(frozen=True, slots=True)
class PatchProfile:
    """Versioned parameters for one deterministic synthesis topology."""

    oscillator: str
    unison_voices: int
    detune_cents: float
    stereo_width: float
    attack_seconds: float
    decay_seconds: float
    sustain_level: float
    release_seconds: float
    gain: float
    velocity_exponent: float
    cutoff_hz: float
    resonance: float
    filter_keytrack: float
    filter_env_octaves: float
    filter_env_decay_seconds: float
    filter_lfo_octaves: float
    lfo_rate_hz: float
    vibrato_cents: float
    pulse_width: float
    noise_amount: float
    fm_ratio: float
    fm_index: float
    drive: float


# These are deliberately separate instruments, not aliases with different names.
# Besides their envelopes and modulation ranges, every entry chooses a different
# source topology in _source_sample().  Values are part of ENGINE_VERSION.
PATCH_PROFILES: dict[str, PatchProfile] = {
    "halo_pad": PatchProfile(
        "halo", 7, 14.0, 1.0, 1.8, 1.4, 0.88, 4.2, 0.115, 1.15,
        5200.0, 0.18, 0.28, 0.35, 1.8, 0.55, 0.11, 7.0, 0.5, 0.0, 2.0, 0.45, 0.25,
    ),
    "choir_pad": PatchProfile(
        "choir", 3, 7.0, 0.72, 0.46, 0.75, 0.86, 2.8, 0.105, 1.05,
        6800.0, 0.28, 0.18, 0.25, 1.2, 0.18, 5.1, 8.0, 0.5, 0.012, 2.0, 0.35, 0.18,
    ),
    "synth_bass": PatchProfile(
        "bass", 2, 3.5, 0.24, 0.006, 0.18, 0.62, 0.24, 0.19, 1.55,
        260.0, 0.68, 0.72, 3.4, 0.16, 0.08, 4.8, 4.0, 0.34, 0.0, 1.0, 0.0, 1.75,
    ),
    "broad_pad": PatchProfile(
        "broad_pad", 6, 11.0, 0.92, 0.82, 1.15, 0.82, 3.1, 0.105, 1.28,
        3100.0, 0.30, 0.34, 0.7, 1.6, 0.48, 0.18, 8.0, 0.46, 0.0, 2.0, 0.45, 0.42,
    ),
    "synth_lead": PatchProfile(
        "lead", 3, 5.0, 0.38, 0.008, 0.16, 0.76, 0.30, 0.145, 1.38,
        3300.0, 0.62, 0.42, 1.55, 0.18, 0.25, 5.6, 16.0, 0.29, 0.0, 2.0, 1.15, 0.72,
    ),
    "synth_brass": PatchProfile(
        "brass", 4, 6.5, 0.52, 0.018, 0.22, 0.68, 0.42, 0.13, 1.48,
        930.0, 0.56, 0.50, 3.0, 0.21, 0.16, 5.3, 9.0, 0.43, 0.008, 2.0, 0.2, 1.25,
    ),
    "synth_strings": PatchProfile(
        "strings", 7, 9.0, 0.94, 0.15, 0.48, 0.84, 1.25, 0.105, 1.24,
        4300.0, 0.24, 0.32, 0.45, 0.9, 0.22, 5.8, 13.0, 0.47, 0.026, 2.0, 0.25, 0.32,
    ),
    "metallic_pad": PatchProfile(
        "metallic", 5, 8.0, 0.88, 1.15, 0.9, 0.78, 3.7, 0.095, 1.10,
        7200.0, 0.38, 0.12, 0.2, 1.1, 0.38, 0.13, 5.0, 0.5, 0.0, 1.41421356, 3.15, 0.28,
    ),
    "sweep_pad": PatchProfile(
        "sweep", 6, 12.5, 0.96, 2.35, 1.35, 0.90, 4.6, 0.10, 1.08,
        1150.0, 0.72, 0.25, 0.65, 2.4, 3.1, 0.087, 5.0, 0.51, 0.07, 1.61803399, 0.55, 0.44,
    ),
    "warm_pad": PatchProfile(
        "warm", 5, 7.5, 0.78, 0.62, 0.95, 0.87, 2.9, 0.12, 1.20,
        1850.0, 0.22, 0.46, 0.5, 1.4, 0.32, 0.21, 6.0, 0.54, 0.0, 2.0, 0.18, 0.84,
    ),
}


@dataclass(slots=True)
class _FilterState:
    ic1eq: float = 0.0
    ic2eq: float = 0.0


@dataclass(slots=True)
class _Voice:
    frequency: float
    velocity: float
    amplitude: float
    phases: list[float]
    mod_phases: list[float]
    detune_ratios: list[float]
    pans: list[float]
    rng_state: int
    envelope: float = 0.0
    stage: str = "attack"
    release_step: float = 0.0
    pending_release: bool = False
    lfo_phase: float = 0.0
    age_samples: int = 0
    filter_left: _FilterState | None = None
    filter_right: _FilterState | None = None
    formants_left: list[_FilterState] | None = None
    formants_right: list[_FilterState] | None = None


def _poly_blep(phase: float, phase_step: float) -> float:
    """Polynomial band-limit correction for a unit discontinuity."""

    dt = min(0.499999, max(1.0e-12, phase_step))
    if phase < dt:
        x = phase / dt
        return x + x - x * x - 1.0
    if phase > 1.0 - dt:
        x = (phase - 1.0) / dt
        return x * x + x + x + 1.0
    return 0.0


def _saw(phase: float, phase_step: float) -> float:
    return 2.0 * phase - 1.0 - _poly_blep(phase, phase_step)


def _pulse(phase: float, phase_step: float, width: float) -> float:
    value = 1.0 if phase < width else -1.0
    value += _poly_blep(phase, phase_step)
    value -= _poly_blep((phase - width) % 1.0, phase_step)
    return value


def _filter_coefficients(
    cutoff_hz: float, resonance: float, sample_rate: int
) -> tuple[float, float, float, float]:
    cutoff = min(sample_rate * 0.45, max(18.0, cutoff_hz))
    g = math.tan(math.pi * cutoff / sample_rate)
    # k=2 is critically damped; approaching zero increases resonance.  The
    # explicit floor keeps the topology-preserving state-variable filter stable.
    k = max(0.06, 2.0 * (1.0 - min(0.97, max(0.0, resonance))))
    a1 = 1.0 / (1.0 + g * (g + k))
    a2 = g * a1
    a3 = g * a2
    return a1, a2, a3, k


def _filter_process(
    state: _FilterState,
    value: float,
    coefficients: tuple[float, float, float, float],
) -> tuple[float, float]:
    a1, a2, a3, _ = coefficients
    v3 = value - state.ic2eq
    band = a1 * state.ic1eq + a2 * v3
    low = state.ic2eq + a2 * state.ic1eq + a3 * v3
    state.ic1eq = 2.0 * band - state.ic1eq
    state.ic2eq = 2.0 * low - state.ic2eq
    return low, band


def _xorshift_bipolar(voice: _Voice) -> float:
    value = voice.rng_state or 0xA341316C
    value ^= (value << 13) & _UINT32_MASK
    value ^= value >> 17
    value ^= (value << 5) & _UINT32_MASK
    voice.rng_state = value & _UINT32_MASK
    return voice.rng_state / 2147483647.5 - 1.0


def _finite_float(value: object, name: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class SynthesizerInstrument(Instrument):
    """Deterministic polyphonic synthesizer with ten versioned topologies."""

    def __init__(
        self,
        sample_rate: int,
        *,
        patch_name: str,
        profile: PatchProfile,
        note_min: float,
        note_max: float,
        seed: int,
        expression_smoothing_seconds: float = 0.012,
    ) -> None:
        super().__init__(sample_rate)
        self.patch_name = patch_name
        self.profile = profile
        self.note_min = note_min
        self.note_max = note_max
        self.seed = seed & _UINT32_MASK
        self.attack_samples = max(1, round(profile.attack_seconds * sample_rate))
        self.decay_samples = max(1, round(profile.decay_seconds * sample_rate))
        self.release_samples = max(1, round(profile.release_seconds * sample_rate))
        self.expression_smoothing_samples = max(
            1, round(expression_smoothing_seconds * sample_rate)
        )
        self.expression = 1.0
        self.expression_target = 1.0
        self.modulation = 0.0
        self.modulation_target = 0.0
        self.sustain_pedal = 0.0
        self.voices: dict[int, _Voice] = {}
        self._formant_coefficients = tuple(
            _filter_coefficients(frequency, 0.91, sample_rate)
            for frequency in (690.0, 1170.0, 2680.0)
        )

    @classmethod
    def from_manifest(
        cls, data: dict[str, Any], sample_rate: int
    ) -> "SynthesizerInstrument":
        engine_version = str(data.get("engine_version", ""))
        if engine_version != ENGINE_VERSION:
            raise ValueError(
                f"synthesizer engine_version must be {ENGINE_VERSION!r}, got {engine_version!r}"
            )
        patch_name = str(data.get("patch", ""))
        if patch_name not in PATCH_PROFILES:
            choices = ", ".join(sorted(PATCH_PROFILES))
            raise ValueError(f"unknown synthesizer patch {patch_name!r}; choose from {choices}")

        raw_parameters = data.get("parameters", {})
        if not isinstance(raw_parameters, dict):
            raise ValueError("synthesizer parameters must be an object")
        allowed = {field.name for field in fields(PatchProfile)} - {"oscillator"}
        unknown = set(raw_parameters) - allowed
        if unknown:
            raise ValueError(
                "unknown synthesizer parameter(s): " + ", ".join(sorted(unknown))
            )
        profile = replace(PATCH_PROFILES[patch_name], **raw_parameters)
        cls._validate_profile(profile)

        note_min = _finite_float(data.get("note_min", 0.0), "note_min")
        note_max = _finite_float(data.get("note_max", 127.0), "note_max")
        if not 0.0 <= note_min <= note_max <= 127.0:
            raise ValueError("synthesizer note range must satisfy 0 <= note_min <= note_max <= 127")
        seed = int(data.get("seed", 0))
        if not 0 <= seed <= _UINT32_MASK:
            raise ValueError("synthesizer seed must be between 0 and 4294967295")
        smoothing = _finite_float(
            data.get("expression_smoothing_seconds", 0.012),
            "expression_smoothing_seconds",
        )
        if smoothing <= 0.0:
            raise ValueError("expression_smoothing_seconds must be positive")
        return cls(
            sample_rate,
            patch_name=patch_name,
            profile=profile,
            note_min=note_min,
            note_max=note_max,
            seed=seed,
            expression_smoothing_seconds=smoothing,
        )

    @staticmethod
    def _validate_profile(profile: PatchProfile) -> None:
        numeric_values = {
            field.name: getattr(profile, field.name)
            for field in fields(PatchProfile)
            if field.name != "oscillator"
        }
        for name, value in numeric_values.items():
            if not math.isfinite(float(value)):
                raise ValueError(f"synthesizer parameter {name} must be finite")
        if not 1 <= int(profile.unison_voices) <= 9:
            raise ValueError("unison_voices must be between 1 and 9")
        if int(profile.unison_voices) != profile.unison_voices:
            raise ValueError("unison_voices must be an integer")
        if profile.detune_cents < 0.0:
            raise ValueError("detune_cents must not be negative")
        if not 0.0 <= profile.stereo_width <= 1.0:
            raise ValueError("stereo_width must be between 0 and 1")
        if min(
            profile.attack_seconds,
            profile.decay_seconds,
            profile.release_seconds,
            profile.cutoff_hz,
            profile.filter_env_decay_seconds,
            profile.lfo_rate_hz,
            profile.fm_ratio,
        ) <= 0.0:
            raise ValueError("synthesizer time, cutoff, LFO and FM ratio values must be positive")
        if not 0.0 <= profile.sustain_level <= 1.0:
            raise ValueError("sustain_level must be between 0 and 1")
        if profile.gain <= 0.0 or profile.velocity_exponent <= 0.0:
            raise ValueError("gain and velocity_exponent must be positive")
        if not 0.0 <= profile.resonance <= 0.97:
            raise ValueError("resonance must be between 0 and 0.97")
        if not 0.02 <= profile.pulse_width <= 0.98:
            raise ValueError("pulse_width must be between 0.02 and 0.98")
        if not 0.0 <= profile.noise_amount <= 1.0:
            raise ValueError("noise_amount must be between 0 and 1")
        if profile.drive < 0.0:
            raise ValueError("drive must not be negative")

    def _pitch_as_midi(self, frequency: float, tuning: EqualTemperament) -> float:
        return 69.0 + 12.0 * math.log2(frequency / tuning.a4_hz)

    def _new_voice(self, note_id: int, frequency: float, velocity: float) -> _Voice:
        count = int(self.profile.unison_voices)
        if count == 1:
            positions = [0.0]
        else:
            positions = [-1.0 + 2.0 * index / (count - 1) for index in range(count)]
        seed = (
            self.seed
            ^ ((note_id * 0x9E3779B1) & _UINT32_MASK)
            ^ (round(frequency * 1000.0) & _UINT32_MASK)
        ) or 0xA341316C
        phases: list[float] = []
        state = seed
        for _ in positions:
            state ^= (state << 13) & _UINT32_MASK
            state ^= state >> 17
            state ^= (state << 5) & _UINT32_MASK
            state &= _UINT32_MASK
            phases.append(state / 4294967296.0)
        voice = _Voice(
            frequency=frequency,
            velocity=velocity,
            amplitude=self.profile.gain * velocity**self.profile.velocity_exponent,
            phases=phases,
            mod_phases=[(phase * 0.61803398875) % 1.0 for phase in phases],
            detune_ratios=[
                2.0 ** (position * self.profile.detune_cents / 1200.0)
                for position in positions
            ],
            pans=[position * self.profile.stereo_width for position in positions],
            rng_state=state,
            filter_left=_FilterState(),
            filter_right=_FilterState(),
        )
        if self.profile.oscillator == "choir":
            voice.formants_left = [_FilterState() for _ in self._formant_coefficients]
            voice.formants_right = [_FilterState() for _ in self._formant_coefficients]
        return voice

    def _begin_release(self, voice: _Voice) -> None:
        if voice.stage != "release":
            voice.stage = "release"
            voice.pending_release = False
            voice.release_step = max(voice.envelope, 1.0e-9) / self.release_samples

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            note_id = int(event.payload["note_id"])
            if note_id in self.voices:
                raise ValueError(f"synthesizer note_id {note_id} is already active")
            frequency = event_pitch_hz(event, tuning)
            if not math.isfinite(frequency) or frequency <= 0.0:
                raise ValueError("synthesizer pitch must be finite and positive")
            midi_note = self._pitch_as_midi(frequency, tuning)
            if midi_note < self.note_min - 1.0e-9 or midi_note > self.note_max + 1.0e-9:
                raise ValueError(
                    f"synthesizer note {midi_note:.4f} is outside calibrated range "
                    f"{self.note_min:g}..{self.note_max:g}"
                )
            if frequency >= self.sample_rate * 0.45:
                raise ValueError(
                    f"synthesizer pitch {frequency:.3f} Hz exceeds the safe limit at "
                    f"{self.sample_rate} Hz sample rate"
                )
            velocity = _finite_float(event.payload.get("velocity", 0.8), "velocity")
            if not 0.0 <= velocity <= 1.0:
                raise ValueError("velocity must be between 0 and 1")
            self.voices[note_id] = self._new_voice(note_id, frequency, velocity)
            return

        if event.type == "note_off":
            voice = self.voices.get(int(event.payload["note_id"]))
            if voice is not None:
                if self.sustain_pedal >= 0.5:
                    voice.pending_release = True
                else:
                    self._begin_release(voice)
            return

        if event.type != "control":
            return
        name = str(event.payload["name"])
        value = _finite_float(event.payload["value"], f"control {name}")
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"control {name} must be between 0 and 1")
        if name == "expression":
            self.expression_target = value
        elif name == "modulation":
            self.modulation_target = value
        elif name == "sustain_pedal":
            previous = self.sustain_pedal
            self.sustain_pedal = value
            if previous >= 0.5 and value < 0.5:
                for voice in self.voices.values():
                    if voice.pending_release:
                        self._begin_release(voice)

    def _advance_envelope(self, voice: _Voice) -> float:
        if voice.stage == "attack":
            voice.envelope += 1.0 / self.attack_samples
            if voice.envelope >= 1.0:
                voice.envelope = 1.0
                voice.stage = "decay"
        elif voice.stage == "decay":
            voice.envelope -= (1.0 - self.profile.sustain_level) / self.decay_samples
            if voice.envelope <= self.profile.sustain_level:
                voice.envelope = self.profile.sustain_level
                voice.stage = "sustain"
        elif voice.stage == "release":
            voice.envelope = max(0.0, voice.envelope - voice.release_step)
        return voice.envelope

    def _source_sample(
        self, voice: _Voice, index: int, frequency: float
    ) -> float:
        phase = voice.phases[index]
        mod_phase = voice.mod_phases[index]
        step = min(0.499999, frequency / self.sample_rate)
        sine = math.sin(math.tau * phase)
        saw = _saw(phase, step)
        pulse = _pulse(phase, step, self.profile.pulse_width)
        mode = self.profile.oscillator
        safe_band = self.sample_rate * 0.45

        if mode == "halo":
            harmonic_2 = (
                math.sin(math.tau * phase * 2.0)
                if frequency * 2.0 < safe_band
                else 0.0
            )
            harmonic_3 = (
                math.sin(
                    math.tau
                    * (phase * 3.0 + 0.08 * math.sin(math.tau * mod_phase))
                )
                if frequency * 3.0 < safe_band
                else 0.0
            )
            source = (
                0.64 * sine
                + 0.23 * harmonic_2
                + 0.13 * harmonic_3
            )
        elif mode == "choir":
            source = 0.70 * saw + 0.30 * sine
        elif mode == "bass":
            source = 0.46 * saw + 0.31 * pulse + 0.23 * sine
        elif mode == "broad_pad":
            harmonic_2 = (
                math.sin(math.tau * phase * 2.0)
                if frequency * 2.0 < safe_band
                else 0.0
            )
            source = 0.58 * saw + 0.30 * sine + 0.12 * harmonic_2
        elif mode == "lead":
            mod_frequency = frequency * self.profile.fm_ratio
            safe_index = max(
                0.0,
                safe_band / max(mod_frequency, 1.0e-12)
                - frequency / max(mod_frequency, 1.0e-12)
                - 1.0,
            )
            phase_modulation = min(self.profile.fm_index, safe_index) * math.sin(
                math.tau * mod_phase
            )
            source = (
                0.33 * pulse
                + 0.27 * saw
                + 0.40 * math.sin(math.tau * phase + phase_modulation)
            )
        elif mode == "brass":
            source = math.tanh(1.25 * (0.68 * saw + 0.32 * pulse))
        elif mode == "strings":
            bow_noise = self.profile.noise_amount * _xorshift_bipolar(voice)
            source = 0.67 * saw + 0.23 * pulse + 0.10 * sine + bow_noise
        elif mode == "metallic":
            mod_frequency = frequency * self.profile.fm_ratio
            modulator = (
                math.sin(math.tau * mod_phase)
                if mod_frequency < safe_band
                else 0.0
            )
            safe_index = max(
                0.0,
                safe_band / max(mod_frequency, 1.0e-12)
                - frequency / max(mod_frequency, 1.0e-12)
                - 1.0,
            )
            carrier = math.sin(
                math.tau * phase + min(self.profile.fm_index, safe_index) * modulator
            )
            ring = sine * modulator if frequency + mod_frequency < safe_band else 0.0
            upper_modulator = (
                math.sin(math.tau * mod_phase * 2.0)
                if mod_frequency * 2.0 < safe_band
                else 0.0
            )
            source = 0.62 * carrier + 0.25 * ring + 0.13 * upper_modulator
        elif mode == "sweep":
            noise = self.profile.noise_amount * _xorshift_bipolar(voice)
            source = 0.52 * saw + 0.31 * sine + noise
        elif mode == "warm":
            harmonic_2 = (
                math.sin(math.tau * phase * 2.0)
                if frequency * 2.0 < safe_band
                else 0.0
            )
            source = 0.60 * sine + 0.26 * saw + 0.14 * harmonic_2
        else:  # guarded by the versioned profile table
            raise RuntimeError(f"unsupported oscillator topology: {mode}")

        if self.profile.drive > 0.0:
            source = math.tanh(source * (1.0 + self.profile.drive)) / math.tanh(
                1.0 + self.profile.drive
            )
        voice.phases[index] = (phase + step) % 1.0
        voice.mod_phases[index] = (
            mod_phase + step * self.profile.fm_ratio
        ) % 1.0
        return source

    def _apply_formants(
        self, value: float, states: list[_FilterState] | None
    ) -> float:
        if states is None:
            return value
        bands = [
            _filter_process(state, value, coefficients)[1]
            for state, coefficients in zip(states, self._formant_coefficients, strict=True)
        ]
        return 0.16 * value + 0.82 * bands[0] + 0.55 * bands[1] + 0.28 * bands[2]

    def _render_voice(self, voice: _Voice) -> StereoFrame:
        envelope = self._advance_envelope(voice)
        lfo = math.sin(math.tau * voice.lfo_phase)
        modulation_scale = 0.22 + 0.78 * self.modulation
        pitch_cents = self.profile.vibrato_cents * modulation_scale * lfo
        if self.profile.oscillator == "brass":
            pitch_cents += 8.0 * math.exp(
                -voice.age_samples / max(1.0, self.sample_rate * 0.045)
            )
        pitch_ratio = 2.0 ** (pitch_cents / 1200.0)

        left = 0.0
        right = 0.0
        for index, (detune, pan) in enumerate(zip(voice.detune_ratios, voice.pans, strict=True)):
            frequency = voice.frequency * detune * pitch_ratio
            sample = self._source_sample(voice, index, frequency)
            angle = (pan + 1.0) * math.pi / 4.0
            left += sample * math.cos(angle)
            right += sample * math.sin(angle)
        normalizer = float(len(voice.phases))
        left /= normalizer
        right /= normalizer

        if self.profile.oscillator == "choir":
            left = self._apply_formants(left, voice.formants_left)
            right = self._apply_formants(right, voice.formants_right)

        age_seconds = voice.age_samples / self.sample_rate
        filter_envelope = math.exp(
            -age_seconds / self.profile.filter_env_decay_seconds
        )
        cutoff = self.profile.cutoff_hz
        cutoff *= (voice.frequency / 440.0) ** self.profile.filter_keytrack
        cutoff *= 2.0 ** (
            self.profile.filter_env_octaves
            * filter_envelope
            * (0.45 + 0.55 * voice.velocity)
            + self.profile.filter_lfo_octaves * modulation_scale * lfo
        )
        coefficients = _filter_coefficients(
            cutoff, self.profile.resonance, self.sample_rate
        )
        assert voice.filter_left is not None and voice.filter_right is not None
        left = _filter_process(voice.filter_left, left, coefficients)[0]
        right = _filter_process(voice.filter_right, right, coefficients)[0]

        amplitude = voice.amplitude * envelope * self.expression
        voice.lfo_phase = (
            voice.lfo_phase + self.profile.lfo_rate_hz / self.sample_rate
        ) % 1.0
        voice.age_samples += 1
        return left * amplitude, right * amplitude

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) / self.expression_smoothing_samples
        self.modulation += (
            self.modulation_target - self.modulation
        ) / self.expression_smoothing_samples

        left = 0.0
        right = 0.0
        finished: list[int] = []
        for note_id, voice in self.voices.items():
            if voice.stage == "release" and voice.envelope <= 0.0:
                finished.append(note_id)
                continue
            voice_left, voice_right = self._render_voice(voice)
            left += voice_left
            right += voice_right
            if voice.stage == "release" and voice.envelope <= 0.0:
                finished.append(note_id)

        for note_id in finished:
            del self.voices[note_id]

        # This is a smooth safety stage, not hard clipping.  It protects files
        # from accidental full-scale overload while preserving finite output.
        return 0.97 * math.tanh(left), 0.97 * math.tanh(right)

    @property
    def active_voice_count(self) -> int:
        return len(self.voices)


__all__ = [
    "ENGINE_VERSION",
    "PATCH_PROFILES",
    "PatchProfile",
    "SynthesizerInstrument",
]
