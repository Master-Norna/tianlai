"""Virtual Playing Orchestra 独奏大提琴的受信内置演奏层。"""

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


_SFZ_FILES = {
    "sustain": "cello-SOLO-sustain.sfz",
    "slow_sustain": "cello-SOLO-sustain.sfz",
    "staccato": "cello-SOLO-staccato.sfz",
    "pizzicato": "cello-SOLO-pizzicato.sfz",
}
_ONE_SHOTS = frozenset(("staccato", "pizzicato"))
_PUBLIC_ARTICULATIONS = frozenset((*_SFZ_FILES, "accent"))


def _with_note_id(event: PerformanceEvent, note_id: int) -> PerformanceEvent:
    return PerformanceEvent(
        sample=event.sample,
        sequence=event.sequence,
        type=event.type,
        payload={**event.payload, "note_id": note_id},
    )


@dataclass(frozen=True, slots=True)
class _NoteRoute:
    articulation: str
    midi_note: float
    pitch_hz: float
    velocity: float
    sustained_note_id: int | None = None


@audited_event_free_blocks(silence_safe=False)
class CelloInstrument(Instrument):
    """A deterministic solo cello with independent release-tail samples."""

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        sfz_root = asset_root / "Strings"
        if not sfz_root.is_dir():
            raise ValueError(
                f"大提琴 SFZ 不存在：{sfz_root}。请按 来源.md 获取 Virtual Playing Orchestra。"
            )

        calibration_path = Path(base_directory) / str(
            manifest.get("pitch_calibration", "音准校准.json")
        )
        calibration: dict[str, Any] = {}
        if calibration_path.is_file():
            calibration_document = json.loads(calibration_path.read_text(encoding="utf-8"))
            calibration = calibration_document.get("samples", {})
            if not isinstance(calibration, dict):
                raise ValueError("cello pitch calibration samples must be an object")

        shared_cache: dict[Path, Any] = {}
        default_gain = float(manifest.get("gain", 0.62))
        effective_release_seconds = float(manifest.get("release_seconds", 0.72))
        if (
            not math.isfinite(effective_release_seconds)
            or effective_release_seconds < 0.0
        ):
            raise ValueError("cello release_seconds must be finite and non-negative")
        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")

        self.engines: dict[str, SampleInstrument] = {}
        for name, sfz_name in _SFZ_FILES.items():
            sfz_path = sfz_root / sfz_name
            if not sfz_path.is_file():
                raise ValueError(f"大提琴奏法映射不存在：{sfz_path}")
            regions = regions_to_manifest(
                sfz_path,
                use_embedded_loops=name not in _ONE_SHOTS,
            )
            if name in ("sustain", "slow_sustain"):
                for region in regions:
                    relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                    measured = calibration.get(relative)
                    if isinstance(measured, dict) and "detune_cents" in measured:
                        region["measured_tuning_cents"] = float(measured["detune_cents"])
                    if name == "sustain":
                        region["attack_seconds"] = float(manifest.get("fast_attack_seconds", 0.03))
            if name not in _ONE_SHOTS:
                for region in regions:
                    region["release_seconds"] = effective_release_seconds
            engine_manifest = {
                "regions": regions,
                "reference_a4_hz": 440.0,
                "gain": default_gain * float(articulation_gain.get(name, 1.0)),
                "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
                "release_seconds": effective_release_seconds,
            }
            self.engines[name] = SampleInstrument.from_manifest(
                engine_manifest,
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )

        release_tail_gain = float(manifest.get("release_tail_gain", 0.52))
        if (
            not math.isfinite(release_tail_gain)
            or not 0.0 <= release_tail_gain <= 1.0
        ):
            raise ValueError("cello release_tail_gain must be between 0 and 1")
        self.release_tails: list[tuple[float, float, SampleInstrument]] = []
        if release_tail_gain > 0.0:
            sustain_sfz = sfz_root / "cello-SOLO-sustain.sfz"
            release_regions = regions_to_manifest(
                sustain_sfz,
                use_embedded_loops=False,
                trigger="release",
            )
            for region in release_regions:
                engine = SampleInstrument.from_manifest(
                    {
                        "regions": [region],
                        "reference_a4_hz": 440.0,
                        "gain": default_gain * release_tail_gain,
                        "velocity_exponent": 0.8,
                        "release_seconds": 0.15,
                    },
                    sample_rate,
                    base_directory=base_directory,
                    sample_cache=shared_cache,
                )
                self.release_tails.append(
                    (float(region["key_min"]), float(region["key_max"]), engine)
                )

        default_articulation = str(manifest.get("default_articulation", "sustain"))
        if default_articulation not in _PUBLIC_ARTICULATIONS:
            raise ValueError(f"unsupported default cello articulation: {default_articulation!r}")
        self.articulation = default_articulation
        self.note_routes: dict[int, _NoteRoute] = {}
        self._auxiliary_note_id = 1_200_000_000
        self.expression = 1.0
        self.expression_target = 1.0
        smoothing_seconds = max(0.001, float(manifest.get("expression_smoothing_seconds", 0.014)))
        self._expression_coefficient = 1.0 - math.exp(-1.0 / (smoothing_seconds * sample_rate))

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _note_number(self, event: PerformanceEvent, tuning: EqualTemperament) -> float:
        if "midi_note" in event.payload:
            note = float(event.payload["midi_note"])
        else:
            note = 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)
        if not 36.0 <= note <= 81.0:
            raise ValueError(f"cello note {note:.3f} is outside the sampled C2-A5 range")
        return note

    def _trigger_one_shot(
        self,
        engine: SampleInstrument,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        pitch_hz: float,
        velocity: float,
    ) -> None:
        engine.handle_event(
            PerformanceEvent(
                sample=event.sample,
                sequence=event.sequence,
                type="note_on",
                payload={
                    "note_id": self._next_auxiliary_id(),
                    "pitch_hz": pitch_hz,
                    "velocity": max(0.0, min(1.0, velocity)),
                },
            ),
            tuning,
        )

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _PUBLIC_ARTICULATIONS:
                choices = ", ".join(sorted(_PUBLIC_ARTICULATIONS))
                raise ValueError(f"unsupported cello articulation {name!r}; choose from {choices}")
            self.articulation = name
            return

        if event.type == "control":
            if event.payload["name"] == "expression":
                self.expression_target = float(event.payload["value"]) ** 1.35
            return

        if event.type == "note_on":
            midi_note = self._note_number(event, tuning)
            note_id = int(event.payload["note_id"])
            pitch_hz = event_pitch_hz(event, tuning)
            velocity = float(event.payload["velocity"])
            name = self.articulation
            if name in _ONE_SHOTS:
                self._trigger_one_shot(
                    self.engines[name], event, tuning, pitch_hz=pitch_hz, velocity=velocity
                )
                self.note_routes[note_id] = _NoteRoute(name, midi_note, pitch_hz, velocity)
                return
            if name == "accent":
                self._trigger_one_shot(
                    self.engines["staccato"],
                    event,
                    tuning,
                    pitch_hz=pitch_hz,
                    velocity=min(1.0, velocity * 1.12),
                )
                sustained_id = self._next_auxiliary_id()
                self.engines["sustain"].handle_event(_with_note_id(event, sustained_id), tuning)
                self.note_routes[note_id] = _NoteRoute(
                    name, midi_note, pitch_hz, velocity, sustained_id
                )
                return
            self.engines[name].handle_event(event, tuning)
            self.note_routes[note_id] = _NoteRoute(
                name, midi_note, pitch_hz, velocity, note_id
            )
            return

        if event.type == "note_off":
            note_id = int(event.payload["note_id"])
            route = self.note_routes.pop(note_id, None)
            if route is None or route.sustained_note_id is None:
                return
            engine_name = "sustain" if route.articulation == "accent" else route.articulation
            self.engines[engine_name].handle_event(
                _with_note_id(event, route.sustained_note_id), tuning
            )
            # Upstream marks these as SFZ trigger=release samples, whose
            # layer/level follows the originating note-on velocity.  Preserve
            # the project's already-auditioned compressed tail curve, but do
            # not let an optional MIDI note-off velocity rewrite it.
            release_tail_velocity = 0.5 * (
                0.55 + 0.45 * route.velocity
            )
            for key_min, key_max, engine in self.release_tails:
                if key_min <= route.midi_note <= key_max:
                    self._trigger_one_shot(
                        engine,
                        event,
                        tuning,
                        pitch_hz=route.pitch_hz,
                        velocity=max(
                            0.18,
                            release_tail_velocity,
                        ),
                    )

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        left = 0.0
        right = 0.0
        for engine in (
            *self.engines.values(),
            *(item[2] for item in self.release_tails),
        ):
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values()) + sum(
            item[2].active_voice_count for item in self.release_tails
        )


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return CelloInstrument(sample_rate, manifest, base_directory)
