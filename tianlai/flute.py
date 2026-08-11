"""Virtual Playing Orchestra 独奏长笛的受信内置演奏层。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from tianlai._event_free_blocks import audited_event_free_blocks
from tianlai.events import PerformanceEvent, event_pitch_hz
from tianlai.instrument import Instrument, StereoFrame
from tianlai.sampler import SampleInstrument
from tianlai.sfz import regions_to_manifest
from tianlai.tuning import EqualTemperament


_PUBLIC_ARTICULATIONS = frozenset(
    ("sustain", "slow_sustain", "legato", "staccato", "accent")
)


@dataclass(frozen=True, slots=True)
class _VoiceRoute:
    engine_name: str
    note_id: int
    sustained: bool


@dataclass(slots=True)
class _NoteRoute:
    voices: tuple[_VoiceRoute, ...]


@audited_event_free_blocks(silence_safe=False)
class FluteInstrument(Instrument):
    """A deterministic, breath-controlled monophonic solo flute."""

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        sfz_root = asset_root / "Woodwinds"
        if not sfz_root.is_dir():
            raise ValueError(
                f"长笛 SFZ 不存在：{sfz_root}。请按 来源.md 获取 Virtual Playing Orchestra。"
            )

        calibration_path = Path(base_directory) / str(
            manifest.get("pitch_calibration", "音准校准.json")
        )
        calibration: dict[str, Any] = {}
        if calibration_path.is_file():
            calibration_document = json.loads(calibration_path.read_text(encoding="utf-8"))
            calibration = calibration_document.get("samples", {})
            if not isinstance(calibration, dict):
                raise ValueError("flute pitch calibration samples must be an object")

        def calibrated(regions: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for region in regions:
                relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                measured = calibration.get(relative)
                if isinstance(measured, dict) and "detune_cents" in measured:
                    region["measured_tuning_cents"] = float(measured["detune_cents"])
            return regions

        fast_regions = calibrated(
            regions_to_manifest(
                sfz_root / "flute-SOLO-normal-mod-wheel.sfz",
                use_embedded_loops=True,
            )
        )
        slow_regions = calibrated(
            regions_to_manifest(
                sfz_root / "flute-SOLO-sustain.sfz",
                use_embedded_loops=True,
            )
        )
        legato_regions = [dict(region) for region in fast_regions]
        for region in legato_regions:
            region["attack_seconds"] = float(manifest.get("legato_attack_seconds", 0.008))

        staccato_regions = regions_to_manifest(
            sfz_root / "flute-SOLO-staccato.sfz",
            use_embedded_loops=False,
        )
        raw_accent = calibrated(
            regions_to_manifest(
                sfz_root / "flute-SOLO-accent.sfz",
                use_embedded_loops=True,
            )
        )
        accent_sustain_regions = [
            region
            for region in raw_accent
            if "susvib" in Path(region["sample"]).name.lower()
        ]
        if len(accent_sustain_regions) != 10:
            raise ValueError("flute accent mapping must contain 10 delayed sustain regions")

        shared_cache: dict[Path, Any] = {}
        default_gain = float(manifest.get("gain", 0.5))
        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")

        region_sets = {
            "sustain": fast_regions,
            "slow_sustain": slow_regions,
            "legato": legato_regions,
            "accent_sustain": accent_sustain_regions,
        }
        self.engines: dict[str, SampleInstrument] = {}
        for name, regions in region_sets.items():
            public_gain_name = "accent" if name == "accent_sustain" else name
            self.engines[name] = SampleInstrument.from_manifest(
                {
                    "regions": regions,
                    "reference_a4_hz": 440.0,
                    "gain": default_gain * float(articulation_gain.get(public_gain_name, 1.0)),
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.62)),
                    "release_seconds": float(manifest.get("release_seconds", 0.7)),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )

        self.staccato_layers: list[tuple[float, float, str]] = []
        for index, region in enumerate(staccato_regions):
            engine_name = f"staccato_{index}"
            self.engines[engine_name] = SampleInstrument.from_manifest(
                {
                    "regions": [region],
                    "reference_a4_hz": 440.0,
                    "gain": default_gain * float(articulation_gain.get("staccato", 1.0)),
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.62)),
                    "release_seconds": float(manifest.get("release_seconds", 0.7)),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )
            self.staccato_layers.append(
                (float(region["key_min"]), float(region["key_max"]), engine_name)
            )

        default_articulation = str(manifest.get("default_articulation", "sustain"))
        if default_articulation not in _PUBLIC_ARTICULATIONS:
            raise ValueError(f"unsupported default flute articulation: {default_articulation!r}")
        self.articulation = default_articulation
        self.note_routes: dict[int, _NoteRoute] = {}
        self._orphaned_voices: list[_VoiceRoute] = []
        self._auxiliary_note_id = 1_300_000_000
        self.legato_release_seconds = float(manifest.get("legato_release_seconds", 0.055))
        self.tongue_choke_seconds = float(manifest.get("tongue_choke_seconds", 0.018))

        self.expression = 1.0
        self.expression_target = 1.0
        self.breath = 1.0
        self.breath_target = 1.0
        expression_smoothing = max(
            0.001, float(manifest.get("expression_smoothing_seconds", 0.014))
        )
        breath_smoothing = max(
            0.001, float(manifest.get("breath_smoothing_seconds", 0.024))
        )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (expression_smoothing * sample_rate)
        )
        self._breath_coefficient = 1.0 - math.exp(
            -1.0 / (breath_smoothing * sample_rate)
        )

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _note_number(self, event: PerformanceEvent, tuning: EqualTemperament) -> float:
        if "midi_note" in event.payload:
            note = float(event.payload["midi_note"])
        else:
            note = 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)
        if not 60.0 <= note <= 98.0:
            raise ValueError(f"flute note {note:.3f} is outside the sampled C4-D7 range")
        return note

    def _start_voice(
        self,
        engine_name: str,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        velocity_scale: float = 1.0,
        sustained: bool,
    ) -> _VoiceRoute:
        note_id = self._next_auxiliary_id()
        velocity = min(1.0, float(event.payload["velocity"]) * velocity_scale)
        self.engines[engine_name].handle_event(
            PerformanceEvent(
                sample=event.sample,
                sequence=event.sequence,
                type="note_on",
                payload={**event.payload, "note_id": note_id, "velocity": velocity},
            ),
            tuning,
        )
        return _VoiceRoute(engine_name, note_id, sustained)

    def _start_staccato_voices(
        self,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        midi_note: float,
        velocity_scale: float = 1.0,
    ) -> tuple[_VoiceRoute, ...]:
        voices = tuple(
            self._start_voice(
                engine_name,
                event,
                tuning,
                velocity_scale=velocity_scale,
                sustained=False,
            )
            for key_min, key_max, engine_name in self.staccato_layers
            if key_min <= midi_note <= key_max
        )
        if not voices:
            raise ValueError(f"no flute staccato region covers MIDI note {midi_note:.3f}")
        return voices

    def _choke_held_notes(self) -> None:
        for voice in self._orphaned_voices:
            self.engines[voice.engine_name].release_note(
                voice.note_id,
                release_seconds=(
                    self.legato_release_seconds
                    if voice.sustained
                    else self.tongue_choke_seconds
                ),
            )
        self._orphaned_voices.clear()
        for route in self.note_routes.values():
            for voice in route.voices:
                release = (
                    self.legato_release_seconds
                    if voice.sustained
                    else self.tongue_choke_seconds
                )
                self.engines[voice.engine_name].release_note(
                    voice.note_id,
                    release_seconds=release,
                )
            route.voices = ()

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _PUBLIC_ARTICULATIONS:
                choices = ", ".join(sorted(_PUBLIC_ARTICULATIONS))
                raise ValueError(f"unsupported flute articulation {name!r}; choose from {choices}")
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            if name == "expression":
                self.expression_target = value**1.3
            elif name == "breath":
                self.breath_target = value**1.08
            return

        if event.type == "note_on":
            midi_note = self._note_number(event, tuning)
            has_sustained_note = any(
                voice.sustained
                for route in self.note_routes.values()
                for voice in route.voices
            )
            selected_articulation = self.articulation
            if has_sustained_note and selected_articulation in (
                "sustain",
                "slow_sustain",
                "legato",
            ):
                selected_articulation = "legato"
            self._choke_held_notes()
            note_id = int(event.payload["note_id"])
            if selected_articulation == "accent":
                voices = self._start_staccato_voices(
                    event,
                    tuning,
                    midi_note=midi_note,
                    velocity_scale=1.08,
                ) + (
                    self._start_voice(
                        "accent_sustain", event, tuning, sustained=True
                    ),
                )
            elif selected_articulation == "staccato":
                voices = self._start_staccato_voices(
                    event,
                    tuning,
                    midi_note=midi_note,
                )
            else:
                voices = (
                    self._start_voice(
                        selected_articulation,
                        event,
                        tuning,
                        sustained=True,
                    ),
                )
            self.note_routes[note_id] = _NoteRoute(voices)
            return

        if event.type == "note_off":
            route = self.note_routes.pop(int(event.payload["note_id"]), None)
            if route is None:
                return
            for voice in route.voices:
                if not voice.sustained:
                    self._orphaned_voices.append(voice)
                    continue
                self.engines[voice.engine_name].handle_event(
                    PerformanceEvent(
                        sample=event.sample,
                        sequence=event.sequence,
                        type="note_off",
                        payload={**event.payload, "note_id": voice.note_id},
                    ),
                    tuning,
                )
                self._orphaned_voices.append(voice)

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        self.breath += (self.breath_target - self.breath) * self._breath_coefficient
        left = 0.0
        right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        amplitude = self.expression * self.breath
        return left * amplitude, right * amplitude

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return FluteInstrument(sample_rate, manifest, base_directory)
