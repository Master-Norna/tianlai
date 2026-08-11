"""天籁编钟：无外部采样的受信内置确定性双音钟模态模型。

这是面向乐谱渲染的代表性声学模型，不是对某一套出土编钟的测量复刻。
``zhenggu`` 与 ``cegu`` 分别使用正面击、侧面击所启发的非谐模态族；
无论选择哪种奏法，输入 MIDI note 都是最终听到的目标音高。换句话说，
引擎会为目标音高选择相应的“假想钟”，不会把侧鼓暗中升高小三度。

运行时只依赖 Python 标准库和天籁的乐器接口，不读取任何外部音频资源。
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from tianlai._event_free_blocks import audited_event_free_blocks
from tianlai.events import PerformanceEvent, event_pitch_hz
from tianlai.instrument import Instrument, StereoFrame
from tianlai.provenance import project_authored_dsp_provenance
from tianlai.tuning import EqualTemperament


ENGINE_VERSION = "1.0.0"
_ARTICULATIONS = ("zhenggu", "cegu")
_CONTROLS = ("expression", "modulation")
_NOTE_MIN = 36
_NOTE_MAX = 98
_SILENCE_GAIN = 1.0e-5  # -100 dB amplitude
_MAX_VOICE_SECONDS = 30.0
_TAIL_FADE_SECONDS = 0.020
_BANDLIMIT_START_RATIO = 0.34
_BANDLIMIT_STOP_RATIO = 0.45

# (frequency ratio, relative amplitude, fundamental-T60 multiplier, weak family)
#
# The weak opposite-strike family is intentionally both quiet and short.  It
# supplies the characteristic double-tone bell ancestry without synthesising
# two strong, nearly coincident oscillators (the failure mode that made the old
# steel-pan model sound like electrical beating).
_MODE_BANKS: dict[str, tuple[tuple[float, float, float, bool], ...]] = {
    "zhenggu": (
        (1.0000, 1.000, 1.00, False),
        (2.8320, 0.540, 0.48, False),
        (3.3826, 0.290, 0.40, False),
        (3.8178, 0.230, 0.36, False),
        (5.3158, 0.180, 0.28, False),
        (5.7692, 0.130, 0.24, False),
        (6.6194, 0.100, 0.20, False),
        (7.6417, 0.070, 0.16, False),
        (8.3300, 0.045, 0.13, False),
        (8.7611, 0.032, 0.10, False),
        (1.1883, 0.070, 0.13, True),
        (3.1073, 0.035, 0.08, True),
        (5.4757, 0.020, 0.05, True),
    ),
    "cegu": (
        (1.0000, 1.000, 1.00, False),
        (2.6150, 0.420, 0.44, False),
        (3.1806, 0.300, 0.36, False),
        (4.6082, 0.230, 0.28, False),
        (5.4140, 0.160, 0.23, False),
        (5.7598, 0.120, 0.20, False),
        (6.5656, 0.090, 0.16, False),
        (7.1516, 0.060, 0.13, False),
        (7.5656, 0.040, 0.10, False),
        (0.8416, 0.060, 0.12, True),
        (2.3833, 0.035, 0.08, True),
        (4.4736, 0.020, 0.05, True),
    ),
}


def _finite(value: object, name: str) -> float:
    number = float(value)  # type: ignore[arg-type]
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _unit(value: object, name: str) -> float:
    number = _finite(value, name)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return number


def _bandlimit_gain(frequency_hz: float, sample_rate: int) -> float:
    """Continuously fade a sinusoidal mode before it approaches Nyquist."""

    start = sample_rate * _BANDLIMIT_START_RATIO
    stop = sample_rate * _BANDLIMIT_STOP_RATIO
    if frequency_hz <= start:
        return 1.0
    if frequency_hz >= stop:
        return 0.0
    progress = (frequency_hz - start) / (stop - start)
    return 0.5 * (1.0 + math.cos(math.pi * progress))


def _fundamental_t60(frequency_hz: float) -> float:
    """Representative radiation-loss curve, clamped to the modeled range."""

    return min(
        22.0,
        max(3.7, 22.0 * (frequency_hz / 65.406) ** -0.514),
    )


def _soft_guard(value: float) -> float:
    """Transparent below the safety knee, smoothly bounded above it."""

    magnitude = abs(value)
    knee = 0.78
    ceiling = 0.97
    if magnitude <= knee:
        return value
    guarded = knee + (ceiling - knee) * math.tanh(
        (magnitude - knee) / (ceiling - knee)
    )
    return math.copysign(guarded, value)


def _mix_seed(seed: int, note_id: int, midi_millisteps: int, articulation: str) -> int:
    """Stable 64-bit mixing; deliberately avoids Python's salted ``hash``."""

    value = seed & 0xFFFFFFFFFFFFFFFF
    value ^= (note_id * 0x9E3779B185EBCA87) & 0xFFFFFFFFFFFFFFFF
    value ^= (midi_millisteps * 0xC2B2AE3D27D4EB4F) & 0xFFFFFFFFFFFFFFFF
    value ^= 0xA24BAED4963EE407 if articulation == "cegu" else 0x9FB21C651E98DF25
    value ^= value >> 30
    value = (value * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    value ^= value >> 27
    value = (value * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    return (value ^ (value >> 31)) or 0xD1B54A32D192ED03


class _Random:
    __slots__ = ("state",)

    def __init__(self, seed: int) -> None:
        self.state = seed & 0xFFFFFFFFFFFFFFFF

    def unit(self) -> float:
        state = self.state
        state ^= (state >> 12) & 0xFFFFFFFFFFFFFFFF
        state ^= (state << 25) & 0xFFFFFFFFFFFFFFFF
        state ^= (state >> 27) & 0xFFFFFFFFFFFFFFFF
        self.state = state & 0xFFFFFFFFFFFFFFFF
        bits = (state * 0x2545F4914F6CDD1D) & 0xFFFFFFFFFFFFFFFF
        return bits / 2.0**64


class _Mode:
    __slots__ = (
        "sine",
        "cosine",
        "step_sine",
        "step_cosine",
        "envelope",
        "decay",
        "amplitude",
        "pan",
        "brightness_weight",
    )

    def __init__(
        self,
        *,
        frequency_hz: float,
        sample_rate: int,
        amplitude: float,
        t60_seconds: float,
        pan: float,
        brightness_weight: float,
        phase: float,
    ) -> None:
        step = 2.0 * math.pi * frequency_hz / sample_rate
        self.sine = math.sin(phase)
        self.cosine = math.cos(phase)
        self.step_sine = math.sin(step)
        self.step_cosine = math.cos(step)
        self.envelope = 1.0
        self.decay = math.exp(math.log(0.001) / (t60_seconds * sample_rate))
        self.amplitude = amplitude
        self.pan = pan
        self.brightness_weight = brightness_weight

    def render(self, modulation: float) -> tuple[float, float]:
        brightness = 1.0 + 0.22 * modulation * self.brightness_weight
        value = self.sine * self.envelope * self.amplitude * brightness
        sine = self.sine * self.step_cosine + self.cosine * self.step_sine
        cosine = self.cosine * self.step_cosine - self.sine * self.step_sine
        self.sine = sine
        self.cosine = cosine
        self.envelope *= self.decay
        return value * (1.0 - self.pan), value * (1.0 + self.pan)


class _Voice:
    __slots__ = (
        "note_id",
        "modes",
        "age",
        "attack_samples",
        "maximum_samples",
        "fade_samples",
        "transient_samples",
        "transient_phases",
        "transient_steps",
        "transient_amplitude",
        "finished",
    )

    def __init__(
        self,
        *,
        note_id: int,
        midi_note: float,
        frequency_hz: float,
        velocity: float,
        articulation: str,
        sample_rate: int,
        gain: float,
        velocity_exponent: float,
        seed: int,
    ) -> None:
        self.note_id = note_id
        self.age = 0
        self.finished = False
        random = _Random(
            _mix_seed(seed, note_id, round(midi_note * 1000.0), articulation)
        )
        bank = _MODE_BANKS[articulation]
        fundamental_t60 = _fundamental_t60(frequency_hz) * (0.90 + 0.10 * velocity)
        level = velocity**velocity_exponent

        # Equalise the two strike banks by their ideal modal energy
        # sum(amplitude² * T60 multiplier), not by a peak-normalising limiter.
        energy = sum(amplitude * amplitude * t60 for _, amplitude, t60, _ in bank)
        energy_normalization = 1.0 / math.sqrt(energy)
        mode_rows: list[
            tuple[float, float, float, float, float, float]
        ] = []
        for index, (ratio, relative_amplitude, t60_scale, weak_family) in enumerate(bank):
            modal_frequency = frequency_hz * ratio
            band_gain = _bandlimit_gain(modal_frequency, sample_rate)
            if band_gain <= 0.0:
                continue
            high_order = math.log2(max(1.0, ratio))
            velocity_brightness = (
                0.55 + 0.45 * velocity**0.7
            ) ** high_order
            # A stable per-note strike variation changes colour but never
            # frequency.  Its tiny range avoids round-robin-like loudness jumps.
            colour = 0.985 + 0.030 * random.unit()
            amplitude = (
                gain
                * level
                * energy_normalization
                * relative_amplitude
                * velocity_brightness
                * band_gain
                * colour
            )
            if amplitude <= 1.0e-12:
                continue
            t60 = max(0.050, fundamental_t60 * t60_scale)
            # Weak opposite-strike remnants remain short even on the lowest bell.
            if weak_family:
                t60 = min(t60, 0.80)
            # The sounding fundamental is strictly centred.  Width comes only
            # from the radiating higher modes, without delay or detuning.
            pan_extent = 0.0 if index == 0 else min(0.10, 0.035 + 0.010 * high_order)
            pan_sign = -1.0 if index % 2 else 1.0
            pan_jitter = 0.90 + 0.20 * random.unit()
            pan = pan_sign * pan_extent * pan_jitter
            phase = 0.0 if not weak_family else (random.unit() - 0.5) * 0.35
            mode_rows.append(
                (
                    modal_frequency,
                    amplitude,
                    t60,
                    pan,
                    min(1.0, high_order / 3.2),
                    phase,
                )
            )

        self.modes = [
            _Mode(
                frequency_hz=frequency,
                sample_rate=sample_rate,
                amplitude=amplitude,
                t60_seconds=t60,
                pan=pan,
                brightness_weight=brightness,
                phase=phase,
            )
            for frequency, amplitude, t60, pan, brightness, phase in mode_rows
        ]
        # A stronger strike has a faster contact and a brighter, still-windowed
        # deterministic mallet/bronze impulse.  No unwindowed white noise is used.
        attack_seconds = 0.0024 - 0.0012 * velocity
        if articulation == "cegu":
            attack_seconds *= 0.86
        self.attack_samples = max(2, round(attack_seconds * sample_rate))
        self.transient_samples = max(
            3,
            round((0.0022 if articulation == "zhenggu" else 0.0016) * sample_rate),
        )
        transient_ratios = (9.73, 13.17, 17.61)
        transient_rows: list[tuple[float, float]] = []
        for ratio in transient_ratios:
            frequency = frequency_hz * ratio
            band_gain = _bandlimit_gain(frequency, sample_rate)
            if band_gain <= 0.0:
                continue
            phase = (random.unit() - 0.5) * math.pi
            transient_rows.append(
                (2.0 * math.pi * frequency / sample_rate, phase)
            )
        self.transient_steps = tuple(row[0] for row in transient_rows)
        self.transient_phases = tuple(row[1] for row in transient_rows)
        self.transient_amplitude = gain * 0.065 * velocity**1.55

        longest_t60 = max((row[2] for row in mode_rows), default=0.05)
        natural_end = longest_t60 * (100.0 / 60.0)
        self.maximum_samples = max(
            self.attack_samples + 1,
            round(min(_MAX_VOICE_SECONDS, natural_end) * sample_rate),
        )
        self.fade_samples = max(2, round(_TAIL_FADE_SECONDS * sample_rate))

    def render(self, modulation: float) -> tuple[float, float]:
        if self.finished:
            return 0.0, 0.0
        if self.age >= self.maximum_samples:
            self.finished = True
            return 0.0, 0.0

        attack_progress = min(1.0, self.age / self.attack_samples)
        attack = attack_progress * attack_progress * (3.0 - 2.0 * attack_progress)
        remaining = self.maximum_samples - self.age
        tail = 1.0
        if remaining <= self.fade_samples:
            progress = remaining / self.fade_samples
            tail = progress * progress * (3.0 - 2.0 * progress)

        left = 0.0
        right = 0.0
        for mode in self.modes:
            mode_left, mode_right = mode.render(modulation)
            left += mode_left
            right += mode_right

        if self.age < self.transient_samples and self.transient_steps:
            position = self.age / (self.transient_samples - 1)
            window = math.sin(math.pi * position) ** 2
            transient = 0.0
            for step, phase in zip(self.transient_steps, self.transient_phases):
                transient += math.sin(step * self.age + phase)
            transient *= (
                window * self.transient_amplitude / len(self.transient_steps)
            )
            # The impulse is almost mono; a 2% polarity-preserving width avoids
            # a hard centre dot without delaying or detuning either channel.
            left += transient * 0.98
            right += transient * 1.02

        self.age += 1
        return left * attack * tail, right * attack * tail


@audited_event_free_blocks(silence_safe=False)
class BianzhongInstrument(Instrument):
    """Deterministic, polyphonic, naturally decaying bianzhong backend."""

    def __init__(
        self,
        sample_rate: int,
        manifest: dict[str, Any],
        base_directory: str,
    ) -> None:
        del base_directory  # The model intentionally has no external assets.
        super().__init__(sample_rate)
        if not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 384_000:
            raise ValueError("sample_rate must be an integer between 8000 and 384000")
        if manifest.get("type") != "modeled_bianzhong":
            raise ValueError("bianzhong manifest type must be 'modeled_bianzhong'")
        if "implementation" in manifest:
            raise ValueError(
                "builtin bianzhong manifest must not name a local implementation"
            )
        if manifest.get("engine_version") != ENGINE_VERSION:
            raise ValueError(
                f"bianzhong engine_version must be exactly {ENGINE_VERSION}"
            )
        if manifest.get("fallback_policy") != "explicit_only_no_silent_gm":
            raise ValueError(
                "bianzhong fallback_policy must be explicit_only_no_silent_gm"
            )

        self.note_min = int(manifest["note_min"])
        self.note_max = int(manifest["note_max"])
        if (self.note_min, self.note_max) != (_NOTE_MIN, _NOTE_MAX):
            raise ValueError(
                f"bianzhong modeled range must be MIDI {_NOTE_MIN}..{_NOTE_MAX}"
            )
        allowed = tuple(str(item) for item in manifest["allowed_articulations"])
        if allowed != _ARTICULATIONS:
            raise ValueError(
                "allowed_articulations must be ['zhenggu', 'cegu'] in that order"
            )
        supported_controls = tuple(
            str(item) for item in manifest["supported_controls"]
        )
        if supported_controls != _CONTROLS:
            raise ValueError(
                "supported_controls must be ['expression', 'modulation']"
            )
        default = str(manifest.get("default_articulation", "zhenggu"))
        if default not in _ARTICULATIONS:
            raise ValueError("default_articulation must be zhenggu or cegu")
        self.articulation = default

        self.seed = int(manifest["seed"])
        if not 0 <= self.seed <= 0xFFFFFFFF:
            raise ValueError("seed must be between 0 and 4294967295")
        self.gain = _finite(manifest.get("gain", 0.145), "gain")
        if not 0.0 < self.gain <= 0.15:
            raise ValueError("gain must be greater than 0 and at most 0.15")
        self.velocity_exponent = _finite(
            manifest.get("velocity_exponent", 0.78),
            "velocity_exponent",
        )
        if not 0.5 <= self.velocity_exponent <= 1.2:
            raise ValueError("velocity_exponent must be between 0.5 and 1.2")

        expression_seconds = _finite(
            manifest.get("expression_smoothing_seconds", 0.015),
            "expression_smoothing_seconds",
        )
        modulation_seconds = _finite(
            manifest.get("modulation_smoothing_seconds", 0.025),
            "modulation_smoothing_seconds",
        )
        if not 0.001 <= expression_seconds <= 0.1:
            raise ValueError(
                "expression_smoothing_seconds must be between 0.001 and 0.1"
            )
        if not 0.001 <= modulation_seconds <= 0.1:
            raise ValueError(
                "modulation_smoothing_seconds must be between 0.001 and 0.1"
            )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (expression_seconds * sample_rate)
        )
        self._modulation_coefficient = 1.0 - math.exp(
            -1.0 / (modulation_seconds * sample_rate)
        )
        self.expression = 1.0
        self.expression_target = 1.0
        self.modulation = 0.0
        self.modulation_target = 0.0
        self._voices: list[_Voice] = []
        self._active_notes: dict[int, _Voice | None] = {}

    def _event_note(
        self,
        event: PerformanceEvent,
        tuning: EqualTemperament,
    ) -> tuple[float, float]:
        if "midi_note" in event.payload:
            midi_note = _finite(event.payload["midi_note"], "midi_note")
            frequency_hz = tuning.note_to_hz(midi_note)
        else:
            frequency_hz = _finite(event_pitch_hz(event, tuning), "pitch_hz")
            if frequency_hz <= 0.0:
                raise ValueError("pitch_hz must be positive")
            midi_note = 69.0 + 12.0 * math.log2(frequency_hz / tuning.a4_hz)
        if not self.note_min <= midi_note <= self.note_max:
            raise ValueError(
                f"bianzhong note {midi_note:.3f} is outside MIDI "
                f"{self.note_min}..{self.note_max}"
            )
        if frequency_hz >= self.sample_rate * _BANDLIMIT_STOP_RATIO:
            raise ValueError(
                "requested bianzhong fundamental is above the engine bandlimit"
            )
        return midi_note, frequency_hz

    def handle_event(
        self,
        event: PerformanceEvent,
        tuning: EqualTemperament,
    ) -> None:
        if event.type == "articulation":
            name = str(event.payload.get("name", ""))
            if name not in _ARTICULATIONS:
                choices = ", ".join(_ARTICULATIONS)
                raise ValueError(
                    f"unsupported bianzhong articulation {name!r}; choose {choices}"
                )
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload.get("name", ""))
            if name not in _CONTROLS:
                choices = ", ".join(_CONTROLS)
                raise ValueError(
                    f"unsupported bianzhong control {name!r}; choose {choices}"
                )
            value = _unit(event.payload.get("value"), f"{name} control value")
            if name == "expression":
                self.expression_target = value**1.2
            else:
                self.modulation_target = value
            return

        if event.type == "note_on":
            note_id_value = event.payload.get("note_id")
            if isinstance(note_id_value, bool):
                raise ValueError("note_id must be an integer")
            note_id = int(note_id_value)
            if note_id < 0:
                raise ValueError("note_id must not be negative")
            if note_id in self._active_notes:
                raise ValueError(f"note_id {note_id} is already active")
            midi_note, frequency_hz = self._event_note(event, tuning)
            velocity = _unit(event.payload.get("velocity", 0.8), "velocity")
            if velocity > 0.0:
                voice = _Voice(
                    note_id=note_id,
                    midi_note=midi_note,
                    frequency_hz=frequency_hz,
                    velocity=velocity,
                    articulation=self.articulation,
                    sample_rate=self.sample_rate,
                    gain=self.gain,
                    velocity_exponent=self.velocity_exponent,
                    seed=self.seed,
                )
                self._voices.append(voice)
                self._active_notes[note_id] = voice
            else:
                self._active_notes[note_id] = None
            return

        if event.type == "note_off":
            note_id_value = event.payload.get("note_id")
            if isinstance(note_id_value, bool):
                raise ValueError("note_id must be an integer")
            note_id = int(note_id_value)
            # One-shot semantics: release the public ID for future reuse, but
            # leave the already-struck bell voice to decay naturally.
            self._active_notes.pop(note_id, None)
            return

        raise ValueError(f"unsupported bianzhong event type: {event.type!r}")

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        self.modulation += (
            self.modulation_target - self.modulation
        ) * self._modulation_coefficient

        left = 0.0
        right = 0.0
        surviving: list[_Voice] = []
        for voice in self._voices:
            voice_left, voice_right = voice.render(self.modulation)
            left += voice_left
            right += voice_right
            if voice.finished:
                # A released one-shot ID may already have been reused by a new
                # voice while this old tail rings.  Remove the ID only if the
                # mapping still points to this exact voice.
                if self._active_notes.get(voice.note_id) is voice:
                    self._active_notes.pop(voice.note_id, None)
            else:
                surviving.append(voice)
        self._voices = surviving

        # Do not normalise by active voice count: long, almost inaudible tails
        # must never pull down the next attack.  The modal gain has static
        # headroom; this guard is exactly transparent below -2.15 dBFS and
        # approaches 0.97 smoothly only for exceptional dense coincidences.
        left = _soft_guard(left * self.expression)
        right = _soft_guard(right * self.expression)
        if not math.isfinite(left) or not math.isfinite(right):
            raise RuntimeError("bianzhong synthesis produced a non-finite frame")
        return left, right

    @property
    def active_voice_count(self) -> int:
        return len(self._voices)


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return BianzhongInstrument(sample_rate, manifest, base_directory)


def _read_manifest(path: str | Path) -> tuple[Path, dict[str, Any]]:
    manifest_path = Path(path).resolve()
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("bianzhong manifest must be a JSON object")
    return manifest_path, document


def _destination(
    manifest_path: Path,
    manifest: dict[str, Any],
    output_path: str | Path | None,
    field: str,
    default: str,
) -> Path:
    return (
        Path(output_path).resolve()
        if output_path is not None
        else manifest_path.parent / str(manifest.get(field, default))
    )


def generate_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Write hash-locked evidence that this instrument has no sampled assets."""

    source, manifest = _read_manifest(manifest_path)
    BianzhongInstrument(48_000, manifest, str(source.parent))
    engine_path = Path(__file__).resolve()
    report: dict[str, Any] = {
        "schema_version": 1,
        "implementation": "Tianlai project-authored deterministic procedural DSP",
        "model_family": "representative double-tone bell nonharmonic modal bank",
        "engine": "tianlai/bianzhong.py",
        "engine_version": ENGINE_VERSION,
        "engine_sha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
        "seed": int(manifest["seed"]),
        "external_assets": [],
        "external_asset_bytes": 0,
        "project_authored": True,
        **project_authored_dsp_provenance(),
        "deterministic": True,
        "acoustic_assumptions": [
            "each articulation uses a distinct nonharmonic modal bank anchored to the requested sounding pitch",
            "weak opposite-strike modal remnants are deliberately short and quiet",
            "radiation loss is approximated by a frequency-dependent T60 curve",
            "stereo width is modal amplitude panning only; there is no delay or inter-channel detuning",
        ],
        "limitations": [
            "representative synthesis model, not measurements of a complete historical bell set",
            "does not reproduce a specific bell's dimensions, alloy, suspension, mallet, microphone, or room",
            "same-MIDI articulation changes select different hypothetical bells so the requested sounding pitch remains invariant",
            "standalone timbre is approved; ensemble and mix review remain pending",
        ],
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = _destination(
        source,
        manifest,
        output_path,
        "resource_verification",
        "资源核验.json",
    )
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _render_probe(
    manifest: dict[str, Any],
    base_directory: Path,
    midi_note: int,
    articulation: str,
    sample_rate: int,
) -> list[float]:
    instrument = BianzhongInstrument(sample_rate, manifest, str(base_directory))
    if articulation != manifest["default_articulation"]:
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": articulation}),
            EqualTemperament(440.0),
        )
    instrument.handle_event(
        PerformanceEvent(
            0,
            1,
            "note_on",
            {"note_id": 1, "midi_note": midi_note, "velocity": 0.82},
        ),
        EqualTemperament(440.0),
    )
    # Late analysis lets the exact target fundamental dominate the shorter
    # high modes while retaining enough cycles at C2 for a stable estimate.
    frames = round(2.4 * sample_rate)
    return [
        0.5 * sum(instrument.render_frame())
        for _ in range(frames)
    ]


def _measure_fundamental(
    signal: list[float],
    sample_rate: int,
    expected_hz: float,
) -> float:
    """Estimate a near-known fundamental with two Hann-windowed lock-in phases."""

    window_frames = round(0.55 * sample_rate)
    first_start = round(0.75 * sample_rate)
    second_start = round(1.55 * sample_rate)

    def phase_at(start: int) -> float:
        real = 0.0
        imag = 0.0
        denominator = max(1, window_frames - 1)
        for offset in range(window_frames):
            window = math.sin(math.pi * offset / denominator) ** 2
            angle = 2.0 * math.pi * expected_hz * (start + offset) / sample_rate
            sample = signal[start + offset] * window
            real += sample * math.cos(angle)
            imag -= sample * math.sin(angle)
        if real == 0.0 and imag == 0.0:
            raise ValueError("silent bianzhong pitch probe")
        return math.atan2(imag, real)

    phase1 = phase_at(first_start)
    phase2 = phase_at(second_start)
    delta = phase2 - phase1
    while delta > math.pi:
        delta -= 2.0 * math.pi
    while delta < -math.pi:
        delta += 2.0 * math.pi
    separation_seconds = (second_start - first_start) / sample_rate
    # Demodulation uses exp(-j*w*t), hence residual phase has a negative sign.
    return expected_hz - delta / (2.0 * math.pi * separation_seconds)


def generate_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
    probe_notes: tuple[int, ...] | None = None,
    sample_rate: int = 48_000,
) -> dict[str, Any]:
    """Render both strike banks and write measured sounding-pitch evidence."""

    source, manifest = _read_manifest(manifest_path)
    if probe_notes is None:
        low = int(manifest["note_min"])
        high = int(manifest["note_max"])
        probe_notes = tuple(sorted({low, (low + high) // 2, high}))
    if not probe_notes:
        raise ValueError("probe_notes must not be empty")

    engine_path = Path(__file__).resolve()
    probes: dict[str, dict[str, Any]] = {}
    errors: list[float] = []
    for articulation in _ARTICULATIONS:
        articulation_probes: dict[str, Any] = {}
        for midi_note in probe_notes:
            if not int(manifest["note_min"]) <= midi_note <= int(manifest["note_max"]):
                raise ValueError(f"pitch probe {midi_note} is outside manifest range")
            expected_hz = 440.0 * 2.0 ** ((midi_note - 69.0) / 12.0)
            signal = _render_probe(
                manifest,
                source.parent,
                midi_note,
                articulation,
                sample_rate,
            )
            measured_hz = _measure_fundamental(signal, sample_rate, expected_hz)
            error_cents = 1200.0 * math.log2(measured_hz / expected_hz)
            errors.append(error_cents)
            articulation_probes[str(midi_note)] = {
                "expected_hz": round(expected_hz, 6),
                "measured_hz": round(measured_hz, 6),
                "error_cents": round(error_cents, 6),
            }
        probes[articulation] = articulation_probes

    report: dict[str, Any] = {
        "schema_version": 1,
        "applicable": True,
        "method": (
            "render each probe for both strike banks; estimate the dominant "
            "fundamental with two Hann-windowed quadrature lock-in phases"
        ),
        "engine_version": ENGINE_VERSION,
        "engine_sha256": hashlib.sha256(engine_path.read_bytes()).hexdigest(),
        "reference_a4_hz": 440.0,
        "pitch_semantics": (
            "MIDI note is the final sounding target for both zhenggu and cegu; "
            "articulation changes modal colour, not concert pitch"
        ),
        "summary": {
            "articulation_count": len(_ARTICULATIONS),
            "probe_count": len(errors),
            "maximum_absolute_error_cents": round(
                max(abs(item) for item in errors),
                6,
            ),
            "median_error_cents": round(statistics.median(errors), 6),
        },
        "probes": probes,
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = _destination(
        source,
        manifest,
        output_path,
        "pitch_calibration",
        "音准校准.json",
    )
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report
