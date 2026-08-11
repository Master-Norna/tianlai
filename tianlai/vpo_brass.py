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
from .sfz import note_number
from .tuning import EqualTemperament
from .vpo_strings import parse_vpo_sfz, vpo_regions_to_manifest


_PUBLIC_ARTICULATIONS = frozenset(
    ("normal", "sustain", "slow_sustain", "staccato", "accent")
)
_ENSEMBLE_COMPONENT_MARKERS = {
    "tuba": "/tuba/",
    "horn": "/horns/",
    "trombone": "/trombone/",
    "trumpet": "/trumpet/",
}


def _with_note_id(event: PerformanceEvent, note_id: int) -> PerformanceEvent:
    return PerformanceEvent(
        sample=event.sample,
        sequence=event.sequence,
        type=event.type,
        payload={**event.payload, "note_id": note_id},
    )


def _event_midi(event: PerformanceEvent, tuning: EqualTemperament) -> float:
    if "midi_note" in event.payload:
        return float(event.payload["midi_note"])
    return 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)


def _sample_component(sample: str | Path, *, ensemble: bool) -> str:
    if not ensemble:
        return "solo"
    normalized = "/" + str(sample).replace("\\", "/").lower().strip("/") + "/"
    matches = [
        name for name, marker in _ENSEMBLE_COMPONENT_MARKERS.items() if marker in normalized
    ]
    if len(matches) != 1:
        raise ValueError(
            "all-brass SFZ sample cannot be assigned to exactly one section: "
            f"{sample}"
        )
    return matches[0]


def _optional_note(values: dict[str, str], name: str) -> float | None:
    value = values.get(name)
    return None if value is None else note_number(value)


@dataclass(frozen=True, slots=True)
class _KeyCrossfade:
    fade_in_low: float | None = None
    fade_in_high: float | None = None
    fade_out_low: float | None = None
    fade_out_high: float | None = None

    def gain(self, note: float) -> float:
        gain = 1.0
        if self.fade_in_low is not None and self.fade_in_high is not None:
            if note <= self.fade_in_low:
                gain *= 0.0
            elif note < self.fade_in_high:
                width = max(1e-9, self.fade_in_high - self.fade_in_low)
                gain *= math.sqrt((note - self.fade_in_low) / width)
        if self.fade_out_low is not None and self.fade_out_high is not None:
            if note >= self.fade_out_high:
                gain *= 0.0
            elif note > self.fade_out_low:
                width = max(1e-9, self.fade_out_high - self.fade_out_low)
                gain *= math.sqrt((self.fade_out_high - note) / width)
        return gain


@dataclass(slots=True)
class _BrassLayer:
    name: str
    note_min: float
    note_max: float
    crossfade: _KeyCrossfade
    engines: dict[str, SampleInstrument]


@dataclass(frozen=True, slots=True)
class _EngineRoute:
    engine: SampleInstrument
    note_id: int


@dataclass(slots=True)
class _ScheduledRelease:
    engine: SampleInstrument
    note_id: int
    remaining_samples: int
    release_seconds: float


def _group_regions(
    regions: list[dict[str, Any]], *, ensemble: bool
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for region in regions:
        name = _sample_component(region["sample"], ensemble=ensemble)
        grouped.setdefault(name, []).append(region)
    return grouped


def _extract_crossfades(
    sfz_path: Path, *, ensemble: bool
) -> dict[str, _KeyCrossfade]:
    grouped: dict[str, set[tuple[float | None, ...]]] = {}
    for values in parse_vpo_sfz(sfz_path):
        sample = values.get("sample")
        if not sample:
            continue
        name = _sample_component(sample, ensemble=ensemble)
        grouped.setdefault(name, set()).add(
            (
                _optional_note(values, "xfin_lokey"),
                _optional_note(values, "xfin_hikey"),
                _optional_note(values, "xfout_lokey"),
                _optional_note(values, "xfout_hikey"),
            )
        )
    result: dict[str, _KeyCrossfade] = {}
    for name, settings in grouped.items():
        if len(settings) != 1:
            raise ValueError(
                f"VPO brass layer {name!r} has inconsistent key crossfades: {sfz_path}"
            )
        result[name] = _KeyCrossfade(*next(iter(settings)))
    return result


def _load_calibration(
    base_directory: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    relative = manifest.get("pitch_calibration")
    if relative is None:
        return {}
    path = Path(base_directory) / str(relative)
    if not path.is_file():
        raise ValueError(f"VPO brass pitch calibration does not exist: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    samples = document.get("samples")
    if not isinstance(samples, dict):
        raise ValueError("VPO brass pitch calibration samples must be an object")
    return samples


def _apply_calibration(
    regions: list[dict[str, Any]], asset_root: Path, calibration: dict[str, Any]
) -> None:
    for region in regions:
        relative = Path(region["sample"]).relative_to(asset_root).as_posix()
        measurement = calibration.get(relative)
        if isinstance(measurement, dict) and "detune_cents" in measurement:
            region["measured_tuning_cents"] = float(measurement["detune_cents"])


@audited_event_free_blocks(silence_safe=False)
class VpoBrassInstrument(Instrument):
    """Deterministic VPO brass candidate with explicit articulation routing.

    VPO's ``normal-mod-wheel`` convention uses CC1 to lengthen the attack.  This
    adapter applies that convention to the clean sustain mapping through
    deterministic attack bins (the tuba normal file also layers a transient,
    so reading it as one flat region map would be incorrect).  The
    all-brass mapping is split into its four real sections and rendered with
    the SFZ key crossfades, rather than incorrectly choosing only one region.
    """

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        self.instrument_name = str(manifest["instrument_name"])
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        self.sampled_range = str(manifest["sampled_range"])
        self.sfz_prefix = str(manifest["sfz_prefix"])
        self.ensemble_scope = str(manifest.get("ensemble_scope", "SOLO")).upper()
        if self.ensemble_scope not in ("SOLO", "SEC"):
            raise ValueError("ensemble_scope must be SOLO or SEC")
        ensemble = self.sfz_prefix == "all-brass"
        if ensemble != (self.ensemble_scope == "SEC"):
            raise ValueError(
                "all-brass must use SEC and dedicated brass instruments must use SOLO"
            )

        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        brass_root = asset_root / "Brass"
        if not brass_root.is_dir():
            raise ValueError(
                f"{self.instrument_name} VPO Brass directory does not exist: {brass_root}"
            )
        normal_path = brass_root / f"{self.sfz_prefix}-{self.ensemble_scope}-sustain.sfz"
        staccato_path = brass_root / (
            f"{self.sfz_prefix}-{self.ensemble_scope}-staccato.sfz"
        )
        for path in (normal_path, staccato_path):
            if not path.is_file():
                raise ValueError(
                    f"{self.instrument_name} required VPO articulation mapping is missing: {path}"
                )

        normal_regions = vpo_regions_to_manifest(normal_path, use_embedded_loops=True)
        staccato_regions = vpo_regions_to_manifest(
            staccato_path, use_embedded_loops=False
        )
        calibration = _load_calibration(base_directory, manifest)
        _apply_calibration(normal_regions, asset_root, calibration)
        _apply_calibration(staccato_regions, asset_root, calibration)
        normal_groups = _group_regions(normal_regions, ensemble=ensemble)
        staccato_groups = _group_regions(staccato_regions, ensemble=ensemble)
        if normal_groups.keys() != staccato_groups.keys():
            raise ValueError(
                f"{self.instrument_name} normal/staccato section sets do not match"
            )
        crossfades = _extract_crossfades(normal_path, ensemble=ensemble)

        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")
        default_gain = float(manifest.get("gain", 0.3))
        velocity_exponent = float(manifest.get("velocity_exponent", 0.72))
        release_seconds = float(manifest.get("release_seconds", 0.65))
        attack_bins = int(manifest.get("modulation_attack_bins", 9))
        if attack_bins < 2 or attack_bins > 33:
            raise ValueError("modulation_attack_bins must be between 2 and 33")
        attack_extension = float(manifest.get("modulation_attack_extension_seconds", 0.5))
        if attack_extension < 0.0:
            raise ValueError("modulation_attack_extension_seconds must not be negative")
        accent_delay = float(manifest.get("accent_delay_seconds", 0.12))
        if accent_delay < 0.0:
            raise ValueError("accent_delay_seconds must not be negative")

        shared_cache: dict[Path, Any] = {}
        self.layers: list[_BrassLayer] = []
        for component_name in sorted(normal_groups):
            component_normal = normal_groups[component_name]
            component_staccato = staccato_groups[component_name]
            engines: dict[str, SampleInstrument] = {}

            def make_engine(
                regions: list[dict[str, Any]], articulation: str
            ) -> SampleInstrument:
                return SampleInstrument.from_manifest(
                    {
                        "regions": regions,
                        "reference_a4_hz": 440.0,
                        "gain": default_gain
                        * float(articulation_gain.get(articulation, 1.0)),
                        "velocity_exponent": velocity_exponent,
                        "release_seconds": release_seconds,
                    },
                    sample_rate,
                    base_directory=base_directory,
                    sample_cache=shared_cache,
                )

            for bin_index in range(attack_bins):
                ratio = bin_index / (attack_bins - 1)
                regions = [dict(region) for region in component_normal]
                for region in regions:
                    region["attack_seconds"] = float(
                        region.get("attack_seconds", 0.03)
                    ) + attack_extension * ratio
                engines[f"sustain_{bin_index}"] = make_engine(regions, "sustain")

            engines["staccato"] = make_engine(
                [dict(region) for region in component_staccato], "staccato"
            )
            engines["accent_attack"] = make_engine(
                [dict(region) for region in component_staccato], "accent"
            )
            accent_regions = [dict(region) for region in component_normal]
            for region in accent_regions:
                region["delay_seconds"] = (
                    float(region.get("delay_seconds", 0.0)) + accent_delay
                )
                region["attack_seconds"] = 0.0
            engines["accent_sustain"] = make_engine(accent_regions, "accent")

            note_min = min(float(region["key_min"]) for region in component_normal)
            note_max = max(float(region["key_max"]) for region in component_normal)
            self.layers.append(
                _BrassLayer(
                    component_name,
                    note_min,
                    note_max,
                    crossfades[component_name],
                    engines,
                )
            )

        default_articulation = str(manifest.get("default_articulation", "sustain"))
        if default_articulation not in _PUBLIC_ARTICULATIONS:
            raise ValueError(
                f"unsupported default {self.instrument_name} articulation: "
                f"{default_articulation!r}"
            )
        self.articulation = default_articulation
        self.modulation = 0.0
        self._attack_bins = attack_bins
        self._auxiliary_note_id = int(
            manifest.get("auxiliary_note_id_base", 1_500_000_000)
        )
        self.note_routes: dict[int, tuple[_EngineRoute, ...]] = {}
        self._scheduled_releases: list[_ScheduledRelease] = []
        self._short_gate_samples = max(
            1, round(float(manifest.get("short_gate_seconds", 0.18)) * sample_rate)
        )
        self._short_release_seconds = max(
            0.001, float(manifest.get("short_release_seconds", 0.2))
        )
        self._slow_sustain_gain = float(articulation_gain.get("slow_sustain", 1.0)) / max(
            1e-12, float(articulation_gain.get("sustain", 1.0))
        )

        self.expression = 1.0
        self.expression_target = 1.0
        self.breath = 1.0
        self.breath_target = 1.0
        expression_seconds = max(
            0.001, float(manifest.get("expression_smoothing_seconds", 0.014))
        )
        breath_seconds = max(
            0.001, float(manifest.get("breath_smoothing_seconds", 0.02))
        )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (expression_seconds * sample_rate)
        )
        self._breath_coefficient = 1.0 - math.exp(
            -1.0 / (breath_seconds * sample_rate)
        )

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _check_range(self, note: float) -> None:
        if not self.note_min <= note <= self.note_max:
            raise ValueError(
                f"{self.instrument_name} note {note:.3f} is outside the sampled "
                f"{self.sampled_range} concert-pitch range"
            )

    def _trigger(
        self,
        layer: _BrassLayer,
        engine_name: str,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        note: float,
        *,
        gain_scale: float = 1.0,
        schedule_release: bool = False,
    ) -> _EngineRoute | None:
        if not layer.note_min <= note <= layer.note_max:
            return None
        crossfade_gain = layer.crossfade.gain(note)
        if crossfade_gain <= 1e-9:
            return None
        engine = layer.engines[engine_name]
        note_id = self._next_auxiliary_id()
        engine.handle_event(_with_note_id(event, note_id), tuning)
        voice = engine.voices[note_id]
        voice.amplitude *= crossfade_gain * gain_scale
        if schedule_release:
            self._scheduled_releases.append(
                _ScheduledRelease(
                    engine,
                    note_id,
                    self._short_gate_samples,
                    self._short_release_seconds,
                )
            )
        return _EngineRoute(engine, note_id)

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _PUBLIC_ARTICULATIONS:
                choices = ", ".join(sorted(_PUBLIC_ARTICULATIONS))
                raise ValueError(
                    f"unsupported {self.instrument_name} articulation {name!r}; "
                    f"choose from {choices}"
                )
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} control must be between 0 and 1")
            if name == "expression":
                self.expression_target = value**1.25
            elif name == "breath":
                self.breath_target = value**1.15
            elif name == "modulation":
                self.modulation = value
            elif name == "sustain_pedal":
                for layer in self.layers:
                    for engine_name, engine in layer.engines.items():
                        if engine_name.startswith("sustain_") or engine_name == "accent_sustain":
                            engine.handle_event(event, tuning)
            return

        if event.type == "note_on":
            note = _event_midi(event, tuning)
            self._check_range(note)
            public_note_id = int(event.payload["note_id"])
            routes: list[_EngineRoute] = []
            if self.articulation == "staccato":
                for layer in self.layers:
                    route = self._trigger(
                        layer,
                        "staccato",
                        event,
                        tuning,
                        note,
                        schedule_release=True,
                    )
                    if route is not None:
                        routes.append(route)
                self.note_routes[public_note_id] = ()
                return
            if self.articulation == "accent":
                sustained: list[_EngineRoute] = []
                for layer in self.layers:
                    self._trigger(
                        layer,
                        "accent_attack",
                        event,
                        tuning,
                        note,
                        schedule_release=True,
                    )
                    route = self._trigger(
                        layer, "accent_sustain", event, tuning, note
                    )
                    if route is not None:
                        sustained.append(route)
                self.note_routes[public_note_id] = tuple(sustained)
                return

            if self.articulation == "slow_sustain":
                bin_index = self._attack_bins - 1
                gain_scale = self._slow_sustain_gain
            else:
                bin_index = round(self.modulation * (self._attack_bins - 1))
                gain_scale = 1.0
            engine_name = f"sustain_{bin_index}"
            for layer in self.layers:
                route = self._trigger(
                    layer,
                    engine_name,
                    event,
                    tuning,
                    note,
                    gain_scale=gain_scale,
                )
                if route is not None:
                    routes.append(route)
            self.note_routes[public_note_id] = tuple(routes)
            return

        if event.type == "note_off":
            routes = self.note_routes.pop(int(event.payload["note_id"]), ())
            for route in routes:
                route.engine.handle_event(_with_note_id(event, route.note_id), tuning)

    def render_frame(self) -> StereoFrame:
        pending: list[_ScheduledRelease] = []
        for scheduled in self._scheduled_releases:
            scheduled.remaining_samples -= 1
            if scheduled.remaining_samples <= 0:
                scheduled.engine.release_note(
                    scheduled.note_id, release_seconds=scheduled.release_seconds
                )
            else:
                pending.append(scheduled)
        self._scheduled_releases = pending

        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        self.breath += (self.breath_target - self.breath) * self._breath_coefficient
        gain = self.expression * self.breath
        left = 0.0
        right = 0.0
        for layer in self.layers:
            for engine in layer.engines.values():
                engine_left, engine_right = engine.render_frame()
                left += engine_left
                right += engine_right
        return left * gain, right * gain

    @property
    def active_voice_count(self) -> int:
        return sum(
            engine.active_voice_count
            for layer in self.layers
            for engine in layer.engines.values()
        )


def create_vpo_brass(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoBrassInstrument(sample_rate, manifest, base_directory)


def _source_paths(
    manifest_path: str | Path,
) -> tuple[dict[str, Any], Path, Path, tuple[Path, Path]]:
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset_root = (manifest_path.parent / str(manifest["asset_root"])).resolve()
    prefix = str(manifest["sfz_prefix"])
    scope = str(manifest.get("ensemble_scope", "SOLO")).upper()
    brass_root = asset_root / "Brass"
    sfz_paths = (
        brass_root / f"{prefix}-{scope}-sustain.sfz",
        brass_root / f"{prefix}-{scope}-staccato.sfz",
    )
    return manifest, manifest_path, asset_root, sfz_paths


def generate_pitch_calibration(
    manifest_path: str | Path, output_path: str | Path
) -> dict[str, Any]:
    """Measure every distinct sustained root sample and write reproducible JSON."""

    from .analysis import analyze_file_pitch

    manifest, _, asset_root, sfz_paths = _source_paths(manifest_path)
    regions = vpo_regions_to_manifest(sfz_paths[0], use_embedded_loops=False)
    samples: dict[str, dict[str, float]] = {}
    for region in regions:
        path = Path(region["sample"])
        relative = path.relative_to(asset_root).as_posix()
        if relative in samples:
            continue
        root_midi = float(region["root_midi"])
        expected_hz = 440.0 * (2.0 ** ((root_midi - 69.0) / 12.0))
        measurement = analyze_file_pitch(
            path,
            expected_hz,
            start_seconds=0.25,
            maximum_frames=131_072,
            search_cents=220.0,
        )
        samples[relative] = {
            "root_midi": root_midi,
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(measurement.detune_cents, 6),
        }
    detunes = [item["detune_cents"] for item in samples.values()]
    document = {
        "description": (
            f"FFT measurement of raw sustained {manifest['instrument_name']} samples; "
            "A4=440 Hz"
        ),
        "source_sfz": sfz_paths[0].relative_to(asset_root).as_posix(),
        "summary": {
            "sample_count": len(samples),
            "median_detune_cents": round(statistics.median(detunes), 6),
            "maximum_absolute_detune_cents": round(max(map(abs, detunes)), 6),
        },
        "samples": samples,
    }
    Path(output_path).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_resource_audit(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    license_files: tuple[str, ...],
) -> dict[str, Any]:
    """Freeze the exact SFZ, sample set, licence and VPO version evidence."""

    _, _, asset_root, sfz_paths = _source_paths(manifest_path)
    samples: dict[str, Path] = {}
    for sfz_path, looped in ((sfz_paths[0], True), (sfz_paths[1], False)):
        for region in vpo_regions_to_manifest(sfz_path, use_embedded_loops=looped):
            path = Path(region["sample"])
            samples[path.relative_to(asset_root).as_posix()] = path

    total_bytes = 0
    aggregate_lines: list[str] = []
    for relative in sorted(samples):
        path = samples[relative]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        total_bytes += path.stat().st_size
        aggregate_lines.append(f"{digest}  {relative}\n")
    aggregate = hashlib.sha256("".join(aggregate_lines).encode("utf-8")).hexdigest()

    def hash_relative(relative: str) -> str:
        return hashlib.sha256((asset_root / relative).read_bytes()).hexdigest()

    document = {
        "upstream": "Virtual Playing Orchestra",
        "sfz_version": "Standard Orchestra 3.3 (2026-06-27)",
        "wave_version": "Wave Files 3.2 (2026-06-27)",
        "source_sfz_sha256": {
            path.relative_to(asset_root).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sfz_paths
        },
        "sample_count": len(samples),
        "sample_bytes": total_bytes,
        "sample_set_sha256": aggregate,
        "sample_set_algorithm": (
            "Sort unique VPO-relative UTF-8 paths; for each write "
            "'<lowercase file sha256>  <path>\\n'; SHA-256 the concatenated UTF-8 bytes."
        ),
        "license_file_sha256": {
            relative: hash_relative(relative) for relative in license_files
        },
        "version_evidence_sha256": {
            relative: hash_relative(relative)
            for relative in (
                "Documentation/change-log-Standard-Orchestra.txt",
                "Documentation/change-log-Wave-Files.txt",
            )
        },
    }
    Path(output_path).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document
