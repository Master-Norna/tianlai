from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from .events import PerformanceEvent, event_pitch_hz
from ._event_free_blocks import audited_event_free_blocks
from .instrument import Instrument, StereoFrame
from .sampler import SampleInstrument
from .tuning import EqualTemperament
from .vpo_percussion import vpo_percussion_regions
from .vpo_strings import (
    VpoStringSectionInstrument,
    vpo_regions_to_manifest,
)

_MIXED_CHOIR_ARTICULATIONS = frozenset(("normal", "sustain"))


def _with_note_id(event: PerformanceEvent, note_id: int) -> PerformanceEvent:
    return PerformanceEvent(
        event.sample,
        event.sequence,
        event.type,
        {**event.payload, "note_id": note_id},
    )


def _event_midi(event: PerformanceEvent, tuning: EqualTemperament) -> float:
    if "midi_note" in event.payload:
        return float(event.payload["midi_note"])
    return 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)


def _load_calibration(base_directory: str, manifest: dict[str, Any]) -> dict[str, Any]:
    name = manifest.get("pitch_calibration")
    if name is None:
        return {}
    path = Path(base_directory) / str(name)
    if not path.is_file():
        raise ValueError(f"VPO pitch calibration does not exist: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    samples = document.get("samples")
    if not isinstance(samples, dict):
        raise ValueError("VPO pitch calibration samples must be an object")
    return samples


def _apply_calibration(
    regions: list[dict[str, Any]], asset_root: Path, calibration: dict[str, Any]
) -> None:
    for region in regions:
        relative = Path(region["sample"]).relative_to(asset_root).as_posix()
        item = calibration.get(relative)
        if isinstance(item, dict) and isinstance(
            item.get("detune_cents"), (int, float)
        ):
            region["measured_tuning_cents"] = float(item["detune_cents"])


@dataclass(frozen=True, slots=True)
class _VelocityFade:
    mode: str
    low: float
    high: float

    def gain(self, velocity: float) -> float:
        value = velocity * 127.0
        if self.mode == "in":
            if value <= self.low:
                return 0.0
            if value >= self.high:
                return 1.0
            return math.sqrt((value - self.low) / max(1e-9, self.high - self.low))
        if self.mode == "out":
            if value <= self.low:
                return 1.0
            if value >= self.high:
                return 0.0
            return math.sqrt((self.high - value) / max(1e-9, self.high - self.low))
        raise ValueError(f"unknown velocity fade mode: {self.mode}")


@dataclass(frozen=True, slots=True)
class _EngineRoute:
    engine: SampleInstrument
    note_id: int


@dataclass(slots=True)
class _ChoirContour:
    engine: SampleInstrument
    note_id: int
    base_amplitude: float
    age_samples: int = 0


@audited_event_free_blocks(silence_safe=False)
class VpoCelestaInstrument(Instrument):
    """Real VPO celesta with its two recorded velocity layers crossfaded."""

    def __init__(
        self, sample_rate: int, manifest: dict[str, Any], base_directory: str
    ) -> None:
        super().__init__(sample_rate)
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        sfz_path = asset_root / "Keys" / "celesta.sfz"
        if not sfz_path.is_file():
            raise ValueError(f"celesta VPO mapping is missing: {sfz_path}")
        calibration = _load_calibration(base_directory, manifest)
        regions = vpo_regions_to_manifest(sfz_path, use_embedded_loops=False)
        _apply_calibration(regions, asset_root, calibration)
        layers: dict[str, list[dict[str, Any]]] = {"soft": [], "hard": []}
        for region in regions:
            is_hard = region.get("_vpo_xfin_lovel") is not None
            item = dict(region)
            item["velocity_min"] = 0.0
            item["velocity_max"] = 1.0
            layers["hard" if is_hard else "soft"].append(item)
        if [len(layers["soft"]), len(layers["hard"])] != [11, 10]:
            raise ValueError("celesta must contain 11 soft and 10 hard regions")
        shared_cache: dict[Path, Any] = {}
        gain = float(manifest.get("gain", 0.42))
        self.engines = {
            name: SampleInstrument.from_manifest(
                {
                    "regions": layer_regions,
                    "reference_a4_hz": 440.0,
                    "gain": gain,
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.74)),
                    "release_seconds": float(manifest.get("release_seconds", 2.0)),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )
            for name, layer_regions in layers.items()
        }
        self.fades = {
            "soft": _VelocityFade("out", 0.0, 95.0),
            "hard": _VelocityFade("in", 63.0, 127.0),
        }
        self.routes: dict[int, tuple[_EngineRoute, ...]] = {}
        self._internal_id = int(manifest.get("auxiliary_note_id_base", 1_800_000_000))
        self.expression = 1.0
        self.expression_target = 1.0
        seconds = max(0.001, float(manifest.get("expression_smoothing_seconds", 0.01)))
        self._expression_coefficient = 1.0 - math.exp(-1.0 / (seconds * sample_rate))

    def _next_id(self) -> int:
        self._internal_id += 1
        return self._internal_id

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} control must be between 0 and 1")
            if name == "expression":
                self.expression_target = value**1.15
            elif name == "sustain_pedal":
                for engine in self.engines.values():
                    engine.handle_event(event, tuning)
            return
        if event.type == "note_on":
            note = _event_midi(event, tuning)
            if not self.note_min <= note <= self.note_max:
                raise ValueError(
                    f"celesta note {note:.3f} is outside sampled range "
                    f"{self.note_min:g}..{self.note_max:g}"
                )
            velocity = float(event.payload["velocity"])
            routes: list[_EngineRoute] = []
            for name, engine in self.engines.items():
                weight = self.fades[name].gain(velocity)
                if weight <= 1e-9:
                    continue
                note_id = self._next_id()
                engine.handle_event(_with_note_id(event, note_id), tuning)
                engine.voices[note_id].amplitude *= weight
                routes.append(_EngineRoute(engine, note_id))
            self.routes[int(event.payload["note_id"])] = tuple(routes)
            return
        if event.type == "note_off":
            for route in self.routes.pop(int(event.payload["note_id"]), ()):
                route.engine.handle_event(_with_note_id(event, route.note_id), tuning)

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        left = right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


@audited_event_free_blocks(silence_safe=False)
class VpoMixedChoirInstrument(Instrument):
    """VPO SSO male/female mixed Ah choir with real looped samples."""

    def __init__(
        self, sample_rate: int, manifest: dict[str, Any], base_directory: str
    ) -> None:
        super().__init__(sample_rate)
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        vocal_root = asset_root / "Vocals"
        paths = {
            "sustain": vocal_root / "choir-MIXED-sustain.sfz",
            "normal": vocal_root / "choir-MIXED-normal-mod-wheel.sfz",
        }
        if frozenset(paths) != _MIXED_CHOIR_ARTICULATIONS:
            raise ValueError("mixed choir articulation declaration is out of date")
        for path in paths.values():
            if not path.is_file():
                raise ValueError(f"mixed choir VPO mapping is missing: {path}")
        calibration = _load_calibration(base_directory, manifest)
        shared_cache: dict[Path, Any] = {}
        self.engines: dict[str, SampleInstrument] = {}
        contour_values: set[tuple[float, float, float]] = set()
        for name, path in paths.items():
            regions = vpo_regions_to_manifest(path, use_embedded_loops=True)
            for region in regions:
                contour_values.add(
                    (
                        float(region.get("_vpo_ampeg_hold") or 0.0),
                        float(region.get("_vpo_ampeg_decay") or 0.0),
                        float(region.get("_vpo_ampeg_sustain") or 100.0),
                    )
                )
            _apply_calibration(regions, asset_root, calibration)
            self.engines[name] = SampleInstrument.from_manifest(
                {
                    "regions": regions,
                    "reference_a4_hz": 440.0,
                    "gain": float(manifest.get("gain", 0.22)),
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.7)),
                    "release_seconds": float(manifest.get("release_seconds", 1.25)),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )
        if contour_values != {(0.84, 22.0, 70.0)}:
            raise ValueError(
                f"mixed choir envelope changed upstream: {sorted(contour_values)}"
            )
        self._hold_samples = round(0.84 * sample_rate)
        self._decay_samples = round(22.0 * sample_rate)
        self._sustain_level = 0.7
        default = str(manifest.get("default_articulation", "normal"))
        if default not in self.engines:
            raise ValueError(f"unsupported mixed choir default articulation: {default}")
        self.articulation = default
        self.modulation = 0.0
        self.routes: dict[int, _EngineRoute] = {}
        self.contours: dict[int, _ChoirContour] = {}
        self._internal_id = int(manifest.get("auxiliary_note_id_base", 1_810_000_000))
        self.expression = self.expression_target = 1.0
        self.breath = self.breath_target = 1.0
        expression_seconds = max(
            0.001, float(manifest.get("expression_smoothing_seconds", 0.018))
        )
        breath_seconds = max(0.001, float(manifest.get("breath_smoothing_seconds", 0.025)))
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (expression_seconds * sample_rate)
        )
        self._breath_coefficient = 1.0 - math.exp(
            -1.0 / (breath_seconds * sample_rate)
        )

    def _next_id(self) -> int:
        self._internal_id += 1
        return self._internal_id

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in self.engines:
                raise ValueError(
                    f"unsupported mixed choir articulation {name!r}; choose normal or sustain"
                )
            self.articulation = name
            return
        if event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} control must be between 0 and 1")
            if name == "expression":
                self.expression_target = value**1.2
            elif name == "breath":
                self.breath_target = value**1.1
            elif name == "modulation":
                self.modulation = value
            elif name == "sustain_pedal":
                for engine in self.engines.values():
                    engine.handle_event(event, tuning)
            return
        if event.type == "note_on":
            note = _event_midi(event, tuning)
            if not self.note_min <= note <= self.note_max:
                raise ValueError(
                    f"mixed choir note {note:.3f} is outside sampled range "
                    f"{self.note_min:g}..{self.note_max:g}"
                )
            note_id = self._next_id()
            engine = self.engines[self.articulation]
            engine.handle_event(_with_note_id(event, note_id), tuning)
            voice = engine.voices[note_id]
            velocity = float(event.payload["velocity"])
            attack = 0.625 * (1.0 - velocity)
            if self.articulation == "normal":
                attack += self.modulation
            voice.attack_samples = max(0, round(attack * self.sample_rate))
            voice.envelope = 0.0 if voice.attack_samples else 1.0
            self.routes[int(event.payload["note_id"])] = _EngineRoute(engine, note_id)
            self.contours[note_id] = _ChoirContour(
                engine=engine,
                note_id=note_id,
                base_amplitude=voice.amplitude,
            )
            return
        if event.type == "note_off":
            route = self.routes.pop(int(event.payload["note_id"]), None)
            if route is not None:
                route.engine.handle_event(_with_note_id(event, route.note_id), tuning)

    def render_frame(self) -> StereoFrame:
        finished: list[int] = []
        for note_id, contour in self.contours.items():
            voice = contour.engine.voices.get(note_id)
            if voice is None:
                finished.append(note_id)
                continue
            if contour.age_samples <= self._hold_samples:
                level = 1.0
            else:
                progress = min(
                    1.0,
                    (contour.age_samples - self._hold_samples)
                    / max(1, self._decay_samples),
                )
                level = 1.0 + (self._sustain_level - 1.0) * progress
            voice.amplitude = contour.base_amplitude * level
            contour.age_samples += 1
        for note_id in finished:
            del self.contours[note_id]
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        self.breath += (self.breath_target - self.breath) * self._breath_coefficient
        left = right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        gain = self.expression * self.breath
        return left * gain, right * gain

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


@audited_event_free_blocks(silence_safe=False)
class VpoCowbellInstrument(Instrument):
    """The four real VPO cowbell samples: two RR by two velocity layers."""

    def __init__(
        self, sample_rate: int, manifest: dict[str, Any], base_directory: str
    ) -> None:
        super().__init__(sample_rate)
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        sfz_path = asset_root / "Percussion" / "misc.sfz"
        if not sfz_path.is_file():
            raise ValueError(f"cowbell VPO mapping is missing: {sfz_path}")
        regions = vpo_percussion_regions(
            sfz_path, source_key_min=56, source_key_max=56
        )
        if len(regions) != 4 or any("cowbell" not in Path(r["sample"]).name.lower() for r in regions):
            raise ValueError("misc.sfz key 56 must contain exactly four real cowbell regions")
        shared_cache: dict[Path, Any] = {}
        gain = float(manifest.get("gain", 0.24))
        self.engines: dict[tuple[str, int], SampleInstrument] = {}
        for region in regions:
            name = Path(region["sample"]).name.lower()
            layer = "soft" if "_v2_" in name else "hard"
            rr = int(region.get("round_robin_position", 0))
            if rr not in (1, 2):
                raise ValueError("cowbell regions must declare RR positions 1 and 2")
            item = dict(region)
            item["velocity_min"] = 0.0
            item["velocity_max"] = 1.0
            self.engines[(layer, rr)] = SampleInstrument.from_manifest(
                {
                    "regions": [item],
                    "reference_a4_hz": 440.0,
                    "gain": gain,
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
                    "release_seconds": float(manifest.get("release_seconds", 0.3)),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )
        if len(self.engines) != 4:
            raise ValueError("cowbell must provide both layers at both RR positions")
        self.fades = {
            "soft": _VelocityFade("out", 54.0, 104.0),
            "hard": _VelocityFade("in", 54.0, 104.0),
        }
        self._rr_counter = 0
        self._internal_id = int(manifest.get("auxiliary_note_id_base", 1_820_000_000))
        self.expression = self.expression_target = 1.0
        seconds = max(0.001, float(manifest.get("expression_smoothing_seconds", 0.008)))
        self._expression_coefficient = 1.0 - math.exp(-1.0 / (seconds * sample_rate))

    def _next_id(self) -> int:
        self._internal_id += 1
        return self._internal_id

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            if str(event.payload["name"]) != "hit":
                raise ValueError("cowbell only supports the hit articulation")
            return
        if event.type == "control" and str(event.payload["name"]) == "expression":
            value = float(event.payload["value"])
            if not 0.0 <= value <= 1.0:
                raise ValueError("expression control must be between 0 and 1")
            self.expression_target = value**1.15
            return
        if event.type == "note_on":
            rr = self._rr_counter % 2 + 1
            self._rr_counter += 1
            velocity = float(event.payload["velocity"])
            fixed_payload = dict(event.payload)
            fixed_payload.pop("pitch_hz", None)
            fixed_payload["midi_note"] = 56
            fixed_event = PerformanceEvent(
                event.sample,
                event.sequence,
                "note_on",
                fixed_payload,
            )
            for layer in ("soft", "hard"):
                weight = self.fades[layer].gain(velocity)
                if weight <= 1e-9:
                    continue
                engine = self.engines[(layer, rr)]
                note_id = self._next_id()
                engine.handle_event(_with_note_id(fixed_event, note_id), EqualTemperament())
                engine.voices[note_id].amplitude *= weight
            return
        # Cowbell is a one-shot: note_off does not truncate the metal decay.

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        left = right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


class _LayeredFixedHit(Instrument):
    """One fixed percussion key with explicit global RR and velocity layers."""

    def __init__(
        self,
        sample_rate: int,
        regions: list[dict[str, Any]],
        base_directory: str,
        *,
        fixed_midi: float,
        gain: float,
        velocity_exponent: float,
        velocity_mode: str,
        note_id_base: int,
    ) -> None:
        super().__init__(sample_rate)
        if velocity_mode not in {"crossfade", "split"}:
            raise ValueError("fixed hit velocity mode must be crossfade or split")
        self.fixed_midi = fixed_midi
        self.velocity_mode = velocity_mode
        self._internal_id = note_id_base
        self._rr_counter = 0
        shared_cache: dict[Path, Any] = {}
        self.engines: dict[tuple[str, int], SampleInstrument] = {}
        for region in regions:
            rr = int(region.get("round_robin_position", 0))
            if rr not in (1, 2):
                raise ValueError("fixed orchestral-hit percussion must provide RR1/RR2")
            layer = "low" if float(region["velocity_max"]) < 1.0 else "high"
            item = dict(region)
            item["velocity_min"] = 0.0
            item["velocity_max"] = 1.0
            # This adapter chooses the RR position globally across both layers.
            # A one-region engine must therefore not advance its own RR state.
            item.pop("round_robin_position", None)
            item.pop("round_robin_length", None)
            self.engines[(layer, rr)] = SampleInstrument.from_manifest(
                {
                    "regions": [item],
                    "reference_a4_hz": 440.0,
                    "gain": gain,
                    "velocity_exponent": velocity_exponent,
                    "release_seconds": float(item.get("release_seconds", 1.0)),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )
        if set(self.engines) != {
            ("low", 1),
            ("low", 2),
            ("high", 1),
            ("high", 2),
        }:
            raise ValueError("fixed orchestral-hit percussion needs 2 layers x 2 RR")
        self.fades = {
            "low": _VelocityFade("out", 54.0, 104.0),
            "high": _VelocityFade("in", 54.0, 104.0),
        }

    def _next_id(self) -> int:
        self._internal_id += 1
        return self._internal_id

    def trigger(self, event: PerformanceEvent) -> None:
        rr = self._rr_counter % 2 + 1
        self._rr_counter += 1
        velocity = float(event.payload["velocity"])
        payload = dict(event.payload)
        payload.pop("pitch_hz", None)
        payload["midi_note"] = self.fixed_midi
        fixed_event = PerformanceEvent(event.sample, event.sequence, "note_on", payload)
        if self.velocity_mode == "split":
            weights = {
                "low": 1.0 if velocity * 127.0 < 80.0 else 0.0,
                "high": 1.0 if velocity * 127.0 >= 80.0 else 0.0,
            }
        else:
            weights = {
                name: fade.gain(velocity) for name, fade in self.fades.items()
            }
        for layer, weight in weights.items():
            if weight <= 1e-9:
                continue
            engine = self.engines[(layer, rr)]
            note_id = self._next_id()
            engine.handle_event(
                _with_note_id(fixed_event, note_id), EqualTemperament()
            )
            engine.voices[note_id].amplitude *= weight

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            self.trigger(event)
        # Both selected percussion sounds are true one-shots.

    def render_frame(self) -> StereoFrame:
        left = right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left, right

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


@dataclass(frozen=True, slots=True)
class _KeyCrossfade:
    fade_in_low: float | None
    fade_in_high: float | None
    fade_out_low: float | None
    fade_out_high: float | None

    def gain(self, note: float) -> float:
        gain = 1.0
        if self.fade_in_low is not None and self.fade_in_high is not None:
            if note <= self.fade_in_low:
                return 0.0
            if note < self.fade_in_high:
                gain *= math.sqrt(
                    (note - self.fade_in_low) / (self.fade_in_high - self.fade_in_low)
                )
        if self.fade_out_low is not None and self.fade_out_high is not None:
            if note >= self.fade_out_high:
                return 0.0
            if note > self.fade_out_low:
                gain *= math.sqrt(
                    (self.fade_out_high - note) / (self.fade_out_high - self.fade_out_low)
                )
        return gain


_BRASS_SECTION_BY_MARKER = {
    "/tuba/": "tuba",
    "/horns/": "horn",
    "/trombone/": "trombone",
    "/trumpet/": "trumpet",
}


def _brass_section(region: dict[str, Any]) -> str:
    normalized = "/" + str(region["sample"]).replace("\\", "/").lower() + "/"
    matches = [name for marker, name in _BRASS_SECTION_BY_MARKER.items() if marker in normalized]
    if len(matches) != 1:
        raise ValueError(f"cannot classify all-brass accent sample: {region['sample']}")
    return matches[0]


def _sfz_note_or_none(value: Any) -> float | None:
    if value is None:
        return None
    from .sfz import note_number

    return note_number(value)


def _is_attack_component(region: dict[str, Any]) -> bool:
    return (
        str(region.get("_vpo_ampeg_sustain", "")) in ("0", "0.0")
        or str(region.get("_vpo_loop_mode", "")) == "one_shot"
    )


class _VpoBrassAccentLayer(Instrument):
    def __init__(
        self,
        sample_rate: int,
        manifest: dict[str, Any],
        base_directory: str,
        asset_root: Path,
        calibration: dict[str, Any],
    ) -> None:
        super().__init__(sample_rate)
        path = asset_root / "Brass" / "all-brass-SEC-accent.sfz"
        if not path.is_file():
            raise ValueError(f"all-brass accent VPO mapping is missing: {path}")
        regions = vpo_regions_to_manifest(path, use_embedded_loops=True)
        _apply_calibration(regions, asset_root, calibration)
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
        crossfades: dict[str, _KeyCrossfade] = {}
        for region in regions:
            name = _brass_section(region)
            is_attack = _is_attack_component(region)
            component = "attack" if is_attack else "sustain"
            item = dict(region)
            if is_attack:
                item.pop("use_embedded_loop", None)
                item["loop_mode"] = "no_loop"
            else:
                item["loop_mode"] = "loop_sustain"
            grouped.setdefault(name, {}).setdefault(component, []).append(item)
            crossfade = _KeyCrossfade(
                _sfz_note_or_none(region.get("_vpo_xfin_lokey")),
                _sfz_note_or_none(region.get("_vpo_xfin_hikey")),
                _sfz_note_or_none(region.get("_vpo_xfout_lokey")),
                _sfz_note_or_none(region.get("_vpo_xfout_hikey")),
            )
            previous = crossfades.setdefault(name, crossfade)
            if previous != crossfade:
                raise ValueError(f"inconsistent all-brass crossfade for {name}")
        if set(grouped) != {"tuba", "horn", "trombone", "trumpet"}:
            raise ValueError("all-brass accent must contain four real sections")
        shared_cache: dict[Path, Any] = {}
        gain = float(manifest.get("brass_gain", 0.115))
        self.engines: dict[tuple[str, str], SampleInstrument] = {}
        self.ranges: dict[tuple[str, str], tuple[float, float]] = {}
        self.crossfades = crossfades
        for name, components in grouped.items():
            if set(components) != {"attack", "sustain"}:
                raise ValueError(f"all-brass accent {name} must contain attack and sustain")
            for component, section_regions in components.items():
                key = (name, component)
                self.engines[key] = SampleInstrument.from_manifest(
                    {
                        "regions": section_regions,
                        "reference_a4_hz": 440.0,
                        "gain": gain,
                        "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
                        "release_seconds": float(manifest.get("release_seconds", 0.65)),
                    },
                    sample_rate,
                    base_directory=base_directory,
                    sample_cache=shared_cache,
                )
                self.ranges[key] = (
                    min(float(r["key_min"]) for r in section_regions),
                    max(float(r["key_max"]) for r in section_regions),
                )
        self.routes: dict[int, tuple[_EngineRoute, ...]] = {}
        self.attack_routes: dict[int, tuple[_EngineRoute, ...]] = {}
        self._internal_id = int(manifest.get("brass_auxiliary_note_id_base", 1_831_000_000))

    def _next_id(self) -> int:
        self._internal_id += 1
        return self._internal_id

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            note = _event_midi(event, tuning)
            routes: list[_EngineRoute] = []
            attack_routes: list[_EngineRoute] = []
            for (name, component), engine in self.engines.items():
                low, high = self.ranges[(name, component)]
                weight = self.crossfades[name].gain(note)
                if not low <= note <= high or weight <= 1e-9:
                    continue
                note_id = self._next_id()
                engine.handle_event(_with_note_id(event, note_id), tuning)
                engine.voices[note_id].amplitude *= weight
                route = _EngineRoute(engine, note_id)
                routes.append(route)
                if component == "attack":
                    attack_routes.append(route)
            self.routes[int(event.payload["note_id"])] = tuple(routes)
            self.attack_routes[int(event.payload["note_id"])] = tuple(attack_routes)
        elif event.type == "note_off":
            self.attack_routes.pop(int(event.payload["note_id"]), None)
            for route in self.routes.pop(int(event.payload["note_id"]), ()):
                route.engine.handle_event(_with_note_id(event, route.note_id), tuning)

    def release_transients(self, note_id: int, tuning: EqualTemperament) -> None:
        event = PerformanceEvent(0, 0, "note_off", {"note_id": note_id})
        for route in self.attack_routes.pop(note_id, ()):
            route.engine.handle_event(_with_note_id(event, route.note_id), tuning)

    def render_frame(self) -> StereoFrame:
        left = right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left, right

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


@dataclass(slots=True)
class _ScheduledHitRelease:
    note_id: int
    transient_samples: int
    remaining_samples: int
    tuning: EqualTemperament


@audited_event_free_blocks(silence_safe=False)
class VpoOrchestralHitInstrument(Instrument):
    """A real one-shot orchestral tutti: strings, brass, bass drum and cymbal."""

    def __init__(
        self, sample_rate: int, manifest: dict[str, Any], base_directory: str
    ) -> None:
        super().__init__(sample_rate)
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        if not (asset_root / "Strings").is_dir():
            raise ValueError(f"orchestral-hit VPO resource tree is missing: {asset_root}")
        calibration = _load_calibration(base_directory, manifest)
        string_manifest = {
            "instrument_name": "orchestral-hit string section",
            "note_min": self.note_min,
            "note_max": self.note_max,
            "sampled_range": f"{self.note_min:g}..{self.note_max:g}",
            "allowed_articulations": ["accent"],
            "default_articulation": "accent",
            "asset_root": manifest["asset_root"],
            "pitch_calibration": manifest["pitch_calibration"],
            "gain": float(manifest.get("strings_gain", 0.15)),
            "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
            "release_seconds": float(manifest.get("release_seconds", 0.65)),
            "short_gate_seconds": float(manifest.get("transient_gate_seconds", 0.16)),
            "short_release_seconds": 0.16,
            "articulation_gain": {"accent": 1.0},
            "auxiliary_note_id_base": 1_830_000_000,
        }
        self.strings = VpoStringSectionInstrument(
            sample_rate, string_manifest, base_directory
        )
        self.brass = _VpoBrassAccentLayer(
            sample_rate, manifest, base_directory, asset_root, calibration
        )
        percussion_root = asset_root / "Percussion"
        bass_regions = vpo_percussion_regions(
            percussion_root / "bassdrum.sfz",
            source_key_min=38,
            source_key_max=38,
        )
        cymbal_regions = vpo_percussion_regions(
            percussion_root / "cymbals.sfz",
            source_key_min=66,
            source_key_max=66,
        )
        velocity_exponent = float(manifest.get("velocity_exponent", 0.72))
        self.bass_drum = _LayeredFixedHit(
            sample_rate,
            bass_regions,
            base_directory,
            fixed_midi=38.0,
            gain=float(manifest.get("bass_drum_gain", 0.19)),
            velocity_exponent=velocity_exponent,
            velocity_mode="crossfade",
            note_id_base=1_832_000_000,
        )
        self.cymbal = _LayeredFixedHit(
            sample_rate,
            cymbal_regions,
            base_directory,
            fixed_midi=66.0,
            gain=float(manifest.get("cymbal_gain", 0.12)),
            velocity_exponent=velocity_exponent,
            velocity_mode="split",
            note_id_base=1_833_000_000,
        )
        self._gate_samples = max(
            1, round(float(manifest.get("gate_seconds", 0.55)) * sample_rate)
        )
        self._transient_samples = max(
            1,
            round(float(manifest.get("transient_gate_seconds", 0.16)) * sample_rate),
        )
        self._layer_note_id = int(
            manifest.get("layer_auxiliary_note_id_base", 1_834_000_000)
        )
        self._scheduled: list[_ScheduledHitRelease] = []
        self.expression = self.expression_target = 1.0
        seconds = max(0.001, float(manifest.get("expression_smoothing_seconds", 0.01)))
        self._expression_coefficient = 1.0 - math.exp(-1.0 / (seconds * sample_rate))

    def _next_layer_id(self) -> int:
        self._layer_note_id += 1
        return self._layer_note_id

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            if str(event.payload["name"]) != "hit":
                raise ValueError("orchestral hit only supports the hit articulation")
            return
        if event.type == "control" and str(event.payload["name"]) == "expression":
            value = float(event.payload["value"])
            if not 0.0 <= value <= 1.0:
                raise ValueError("expression control must be between 0 and 1")
            self.expression_target = value**1.15
            return
        if event.type == "note_on":
            note = _event_midi(event, tuning)
            if not self.note_min <= note <= self.note_max:
                raise ValueError(
                    f"orchestral hit note {note:.3f} is outside layered range "
                    f"{self.note_min:g}..{self.note_max:g}"
                )
            layer_note_id = self._next_layer_id()
            tonal_event = _with_note_id(event, layer_note_id)
            self.strings.handle_event(tonal_event, tuning)
            self.brass.handle_event(tonal_event, tuning)
            self.bass_drum.handle_event(event, tuning)
            self.cymbal.handle_event(event, tuning)
            self._scheduled.append(
                _ScheduledHitRelease(
                    layer_note_id,
                    self._transient_samples,
                    self._gate_samples,
                    tuning,
                )
            )
            return
        # External note_off is intentionally ignored: every hit uses the same
        # fixed one-shot gate, independent of score note length.

    def render_frame(self) -> StereoFrame:
        pending: list[_ScheduledHitRelease] = []
        for scheduled in self._scheduled:
            scheduled.transient_samples -= 1
            scheduled.remaining_samples -= 1
            if scheduled.transient_samples == 0:
                self.brass.release_transients(scheduled.note_id, scheduled.tuning)
            if scheduled.remaining_samples <= 0:
                event = PerformanceEvent(
                    0, 0, "note_off", {"note_id": scheduled.note_id}
                )
                self.strings.handle_event(event, scheduled.tuning)
                self.brass.handle_event(event, scheduled.tuning)
            else:
                pending.append(scheduled)
        self._scheduled = pending
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        frames = (
            self.strings.render_frame(),
            self.brass.render_frame(),
            self.bass_drum.render_frame(),
            self.cymbal.render_frame(),
        )
        return (
            sum(frame[0] for frame in frames) * self.expression,
            sum(frame[1] for frame in frames) * self.expression,
        )

    @property
    def active_voice_count(self) -> int:
        return (
            self.strings.active_voice_count
            + self.brass.active_voice_count
            + self.bass_drum.active_voice_count
            + self.cymbal.active_voice_count
        )


def create_vpo_celesta(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoCelestaInstrument(sample_rate, manifest, base_directory)


def create_vpo_mixed_choir(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoMixedChoirInstrument(sample_rate, manifest, base_directory)


def create_vpo_cowbell(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoCowbellInstrument(sample_rate, manifest, base_directory)


def create_vpo_orchestral_hit(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoOrchestralHitInstrument(sample_rate, manifest, base_directory)


def _special_sources(
    manifest_path: str | Path,
    *,
    calibration_only: bool = False,
) -> tuple[dict[str, Any], Path, tuple[Path, ...], list[dict[str, Any]]]:
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    asset_root = (path.parent / str(manifest["asset_root"])).resolve()
    profile = str(manifest["profile"])
    if profile == "celesta":
        sfz_paths = (asset_root / "Keys" / "celesta.sfz",)
        regions = vpo_regions_to_manifest(sfz_paths[0], use_embedded_loops=False)
    elif profile == "mixed_choir":
        sustain = asset_root / "Vocals" / "choir-MIXED-sustain.sfz"
        normal = asset_root / "Vocals" / "choir-MIXED-normal-mod-wheel.sfz"
        sfz_paths = (sustain, normal)
        selected = (sustain,) if calibration_only else sfz_paths
        regions = [
            region
            for sfz in selected
            for region in vpo_regions_to_manifest(sfz, use_embedded_loops=True)
        ]
    elif profile == "cowbell":
        sfz_paths = (asset_root / "Percussion" / "misc.sfz",)
        regions = vpo_percussion_regions(
            sfz_paths[0], source_key_min=56, source_key_max=56
        )
    elif profile == "orchestral_hit":
        string_path = asset_root / "Strings" / "all-strings-SEC-accent.sfz"
        brass_path = asset_root / "Brass" / "all-brass-SEC-accent.sfz"
        bass_path = asset_root / "Percussion" / "bassdrum.sfz"
        cymbal_path = asset_root / "Percussion" / "cymbals.sfz"
        sfz_paths = (string_path, brass_path, bass_path, cymbal_path)
        if calibration_only:
            string_regions = vpo_regions_to_manifest(
                string_path,
                use_embedded_loops=True,
                component="accent_sustain",
            )
            brass_regions = vpo_regions_to_manifest(
                brass_path, use_embedded_loops=True
            )
            # Short brass attacks are intentionally excluded from pitch
            # measurement: their noisy transient can make even a constrained
            # FFT lock to the adjacent semitone. Shared attack/sustain WAVs are
            # still calibrated through the stable looped sustain mapping.
            regions = string_regions + [
                region for region in brass_regions if not _is_attack_component(region)
            ]
        else:
            regions = (
                vpo_regions_to_manifest(string_path, use_embedded_loops=True)
                + vpo_regions_to_manifest(brass_path, use_embedded_loops=True)
                + vpo_percussion_regions(
                    bass_path, source_key_min=38, source_key_max=38
                )
                + vpo_percussion_regions(
                    cymbal_path, source_key_min=66, source_key_max=66
                )
            )
    else:
        raise ValueError(f"unknown VPO special profile: {profile}")
    for source in sfz_paths:
        if not source.is_file():
            raise ValueError(f"VPO special source mapping is missing: {source}")
    return manifest, asset_root, sfz_paths, regions


def generate_special_pitch_calibration(
    manifest_path: str | Path, output_path: str | Path
) -> dict[str, Any]:
    manifest, asset_root, sfz_paths, regions = _special_sources(
        manifest_path, calibration_only=True
    )
    profile = str(manifest["profile"])
    destination = Path(output_path)
    if profile == "cowbell":
        document: dict[str, Any] = {
            "applicable": False,
            "profile": "cowbell",
            "reason": "独立牛铃为无固定音高金属打击；MIDI 56 仅是 SFZ 键位，不伪造十二平均律音高。",
            "samples": {},
        }
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return document

    from .analysis import analyze_file_harmonic_pitch

    roots: dict[Path, float] = {}
    for region in regions:
        sample = Path(region["sample"])
        root = float(region["root_midi"])
        previous = roots.setdefault(sample, root)
        if not math.isclose(previous, root, abs_tol=1e-9):
            raise ValueError(f"one VPO sample maps to inconsistent roots: {sample}")
    samples: dict[str, dict[str, float]] = {}
    start = 0.04 if profile == "celesta" else 0.22
    for sample, root in sorted(roots.items(), key=lambda item: item[0].as_posix()):
        expected = 440.0 * (2.0 ** ((root - 69.0) / 12.0))
        measurement = analyze_file_harmonic_pitch(
            sample,
            expected,
            start_seconds=start,
            maximum_frames=131_072,
            search_cents=180.0,
            harmonic_count=10,
        )
        samples[sample.relative_to(asset_root).as_posix()] = {
            "root_midi": root,
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(measurement.detune_cents, 6),
        }
    detunes = [item["detune_cents"] for item in samples.values()]
    document = {
        "applicable": True,
        "profile": profile,
        "method": "harmonic-constrained FFT of raw source WAV; A4=440 Hz",
        "source_sfz": [path.relative_to(asset_root).as_posix() for path in sfz_paths],
        "summary": {
            "sample_count": len(samples),
            "median_detune_cents": round(statistics.median(detunes), 6),
            "maximum_absolute_detune_cents": round(max(map(abs, detunes)), 6),
        },
        "samples": samples,
    }
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_special_resource_audit(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    license_files: tuple[str, ...],
) -> dict[str, Any]:
    manifest, asset_root, sfz_paths, regions = _special_sources(manifest_path)
    sample_paths = sorted(
        {Path(region["sample"]) for region in regions},
        key=lambda sample: sample.relative_to(asset_root).as_posix(),
    )
    lines: list[str] = []
    total_bytes = 0
    for sample in sample_paths:
        relative = sample.relative_to(asset_root).as_posix()
        digest = hashlib.sha256(sample.read_bytes()).hexdigest()
        lines.append(f"{digest}  {relative}\n")
        total_bytes += sample.stat().st_size

    def hashes(relatives: tuple[str, ...]) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in relatives:
            source = asset_root / relative
            if not source.is_file():
                raise ValueError(f"VPO evidence file is missing: {source}")
            result[relative] = hashlib.sha256(source.read_bytes()).hexdigest()
        return result

    document = {
        "upstream": "Virtual Playing Orchestra",
        "sfz_version": "Standard Orchestra 3.3 (2026-06-27)",
        "wave_version": "Wave Files 3.2 (2026-06-27)",
        "profile": manifest["profile"],
        "source_sfz_sha256": {
            source.relative_to(asset_root).as_posix(): hashlib.sha256(
                source.read_bytes()
            ).hexdigest()
            for source in sfz_paths
        },
        "sample_count": len(sample_paths),
        "sample_bytes": total_bytes,
        "sample_set_sha256": hashlib.sha256("".join(lines).encode("utf-8")).hexdigest(),
        "sample_set_algorithm": (
            "Sort unique VPO-relative UTF-8 paths; for each write "
            "'<lowercase file sha256>  <path>\\n'; SHA-256 the concatenated UTF-8 bytes."
        ),
        "license_file_sha256": hashes(license_files),
        "version_evidence_sha256": hashes(
            (
                "Documentation/change-log-Standard-Orchestra.txt",
                "Documentation/change-log-Wave-Files.txt",
            )
        ),
    }
    Path(output_path).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


__all__ = [
    "VpoCelestaInstrument",
    "VpoCowbellInstrument",
    "VpoMixedChoirInstrument",
    "VpoOrchestralHitInstrument",
    "create_vpo_celesta",
    "create_vpo_cowbell",
    "create_vpo_mixed_choir",
    "create_vpo_orchestral_hit",
    "generate_special_pitch_calibration",
    "generate_special_resource_audit",
]
