from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Literal

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .sampler import SampleInstrument
from .sfz import note_number
from .tuning import EqualTemperament
from .vpo_strings import parse_vpo_sfz


_PUBLIC_ARTICULATIONS = frozenset(
    ("sustain", "slow_sustain", "staccato", "accent")
)
_SUSTAIN_ENGINES = frozenset(("sustain", "slow_sustain", "accent_sustain"))
_Component = Literal["sustain", "one_shot"]


def _velocity_limits(values: dict[str, str]) -> tuple[float, float]:
    low = max(0.0, (float(values.get("lovel", 0.0)) - 0.5) / 127.0)
    high = min(1.0, (float(values.get("hivel", 127.0)) + 0.5) / 127.0)
    if "xfin_lovel" in values and "xfin_hivel" in values:
        midpoint = (float(values["xfin_lovel"]) + float(values["xfin_hivel"])) / 2.0
        low = max(low, midpoint / 127.0)
    if "xfout_lovel" in values and "xfout_hivel" in values:
        midpoint = (
            float(values["xfout_lovel"]) + float(values["xfout_hivel"])
        ) / 2.0
        high = min(high, midpoint / 127.0)
    return low, high


def _region_component(values: dict[str, str]) -> _Component:
    """Classify VPO's layered woodwind mappings by their amplitude envelope.

    Several VPO ``normal`` and ``accent`` files concatenate a manufactured
    staccato attack (``ampeg_sustain=0``) and a looped sustained component.
    A generic region selector would choose just one of those simultaneous
    layers.  The dedicated adapter separates them so both parts of an accent
    are rendered as the upstream mapping intended.
    """

    try:
        sustain_level = float(values.get("ampeg_sustain", 100.0))
    except ValueError as exc:
        raise ValueError(
            f"invalid VPO ampeg_sustain value: {values.get('ampeg_sustain')!r}"
        ) from exc
    return "one_shot" if sustain_level <= 0.0 else "sustain"


def vpo_woodwind_regions_to_manifest(
    sfz_path: str | Path,
    *,
    use_embedded_loops: bool,
    component: _Component | None = None,
) -> list[dict[str, Any]]:
    """Convert VPO SOLO woodwind regions, optionally selecting one layer.

    VPO paths contain both spaces and mixed slash styles, so this uses the
    permissive VPO parser.  Random pitch/gain/delay opcodes are intentionally
    omitted: candidate renders must remain byte deterministic.
    """

    source_path = Path(sfz_path).resolve()
    converted: list[dict[str, Any]] = []
    for index, values in enumerate(parse_vpo_sfz(source_path)):
        if values.get("trigger", "attack").lower() != "attack":
            continue
        if component is not None and _region_component(values) != component:
            continue
        sample_name = values.get("sample")
        if not sample_name:
            continue
        root_value = values.get("pitch_keycenter", values.get("key"))
        if root_value is None:
            raise ValueError(
                f"SFZ region {index} has no pitch_keycenter: {source_path}"
            )
        sample_path = (source_path.parent / sample_name.replace("\\", "/")).resolve()
        root_midi = note_number(root_value)
        key_min = note_number(values.get("lokey", values.get("key", root_value)))
        key_max = note_number(values.get("hikey", values.get("key", root_value)))
        velocity_min, velocity_max = _velocity_limits(values)
        item: dict[str, Any] = {
            "sample": str(sample_path),
            "root_midi": root_midi,
            "measured_tuning_cents": -float(values.get("tune", 0.0)),
            "key_min": key_min,
            "key_max": key_max,
            "velocity_min": velocity_min,
            "velocity_max": velocity_max,
            "gain_db": float(values.get("volume", 0.0)),
            "delay_seconds": float(values.get("delay", 0.0)),
            "attack_seconds": float(values.get("ampeg_attack", 0.0)),
            "release_seconds": float(values.get("ampeg_release", 0.25)),
            "offset_frames": int(float(values.get("offset", 0.0))),
        }
        if use_embedded_loops:
            item["use_embedded_loop"] = True
        converted.append(item)
    if not converted:
        suffix = f" ({component})" if component is not None else ""
        raise ValueError(f"VPO SFZ contains no playable regions{suffix}: {source_path}")
    return converted


def woodwind_source_regions(
    asset_root: str | Path,
    sfz_prefix: str,
) -> dict[str, list[dict[str, Any]]]:
    """Load the four public articulation mappings used by a candidate."""

    sfz_root = Path(asset_root).resolve() / "Woodwinds"
    paths = {
        "normal": sfz_root / f"{sfz_prefix}-SOLO-normal-mod-wheel.sfz",
        "slow_sustain": sfz_root / f"{sfz_prefix}-SOLO-sustain.sfz",
        "staccato": sfz_root / f"{sfz_prefix}-SOLO-staccato.sfz",
        "accent": sfz_root / f"{sfz_prefix}-SOLO-accent.sfz",
    }
    missing = [path for path in paths.values() if not path.is_file()]
    if missing:
        joined = ", ".join(str(path) for path in missing)
        raise ValueError(f"VPO woodwind articulation mapping is missing: {joined}")
    return {
        "sustain": vpo_woodwind_regions_to_manifest(
            paths["normal"], use_embedded_loops=True, component="sustain"
        ),
        "slow_sustain": vpo_woodwind_regions_to_manifest(
            paths["slow_sustain"], use_embedded_loops=True, component="sustain"
        ),
        "staccato": vpo_woodwind_regions_to_manifest(
            paths["staccato"], use_embedded_loops=False, component="one_shot"
        ),
        "accent_attack": vpo_woodwind_regions_to_manifest(
            paths["accent"], use_embedded_loops=False, component="one_shot"
        ),
        "accent_sustain": vpo_woodwind_regions_to_manifest(
            paths["accent"], use_embedded_loops=True, component="sustain"
        ),
    }


def _with_note_id(event: PerformanceEvent, note_id: int) -> PerformanceEvent:
    return PerformanceEvent(
        sample=event.sample,
        sequence=event.sequence,
        type=event.type,
        payload={**event.payload, "note_id": note_id},
    )


@dataclass(frozen=True, slots=True)
class _VoiceRoute:
    engine_name: str
    note_id: int
    sustained: bool


@dataclass(slots=True)
class _NoteRoute:
    voices: tuple[_VoiceRoute, ...]


@dataclass(slots=True)
class _ScheduledRelease:
    engine_name: str
    note_id: int
    remaining_samples: int
    release_seconds: float


class VpoSoloWoodwindInstrument(Instrument):
    """Deterministic, monophonic VPO SOLO woodwind candidate."""

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        self.instrument_name = str(manifest["instrument_name"])
        self.display_name = str(manifest.get("display_name", self.instrument_name))
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        self.sampled_range = str(manifest["sampled_range"])
        self.written_note_min = float(manifest["written_note_min"])
        self.written_note_max = float(manifest["written_note_max"])
        self.written_range = str(manifest["written_range"])
        self.written_to_sounding_semitones = float(
            manifest["written_to_sounding_semitones"]
        )
        if self.note_min > self.note_max or self.written_note_min > self.written_note_max:
            raise ValueError(f"{self.display_name} has an invalid sampled range")
        if not math.isclose(
            self.written_note_min + self.written_to_sounding_semitones,
            self.note_min,
            abs_tol=1e-9,
        ) or not math.isclose(
            self.written_note_max + self.written_to_sounding_semitones,
            self.note_max,
            abs_tol=1e-9,
        ):
            raise ValueError(
                f"{self.display_name} written/sounding range metadata is inconsistent"
            )
        if str(manifest.get("pitch_input", "sounding")) != "sounding":
            raise ValueError("VPO woodwind pitch_input must be 'sounding'")

        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        sfz_root = asset_root / "Woodwinds"
        if not sfz_root.is_dir():
            raise ValueError(
                f"{self.display_name} VPO 木管音源不存在：{sfz_root}。"
                "请按 来源.md 安装 Virtual Playing Orchestra Standard 3.3 / Wave 3.2。"
            )

        calibration_path = Path(base_directory) / str(manifest["pitch_calibration"])
        if not calibration_path.is_file():
            raise ValueError(
                f"{self.display_name} 音准校准表不存在：{calibration_path}。"
                "请运行同目录的 校准音准.py。"
            )
        calibration_document = json.loads(calibration_path.read_text(encoding="utf-8"))
        calibration = calibration_document.get("samples")
        if not isinstance(calibration, dict) or not calibration:
            raise ValueError(f"{self.display_name} pitch calibration samples must be an object")

        sfz_prefix = str(manifest["sfz_prefix"])
        region_sets = woodwind_source_regions(asset_root, sfz_prefix)
        fast_attack = max(0.0, float(manifest.get("fast_attack_seconds", 0.035)))
        for region in region_sets["sustain"]:
            region["attack_seconds"] = fast_attack

        self._apply_calibration(region_sets, asset_root, calibration)
        self._validate_coverage(region_sets)

        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")
        default_gain = float(manifest.get("gain", 0.35))
        shared_cache: dict[Path, Any] = {}
        self.engines: dict[str, SampleInstrument] = {}
        for name, regions in region_sets.items():
            public_name = "accent" if name.startswith("accent_") else name
            self.engines[name] = SampleInstrument.from_manifest(
                {
                    "regions": regions,
                    "reference_a4_hz": 440.0,
                    "gain": default_gain
                    * float(articulation_gain.get(public_name, 1.0)),
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.68)),
                    "release_seconds": float(manifest.get("release_seconds", 0.62)),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )

        default_articulation = str(manifest.get("default_articulation", "sustain"))
        if default_articulation not in _PUBLIC_ARTICULATIONS:
            raise ValueError(
                f"unsupported default {self.display_name} articulation: "
                f"{default_articulation!r}"
            )
        self.articulation = default_articulation
        self.note_routes: dict[int, _NoteRoute] = {}
        self._auxiliary_note_id = int(manifest.get("auxiliary_note_id_base", 1_400_000_000))
        self._scheduled_releases: list[_ScheduledRelease] = []
        self._short_gate_samples = max(
            1, round(float(manifest.get("short_gate_seconds", 0.16)) * sample_rate)
        )
        self._short_release_seconds = max(
            0.001, float(manifest.get("short_release_seconds", 0.12))
        )
        self.legato_release_seconds = max(
            0.001, float(manifest.get("legato_release_seconds", 0.05))
        )
        self.tongue_choke_seconds = max(
            0.001, float(manifest.get("tongue_choke_seconds", 0.018))
        )

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

    @staticmethod
    def _apply_calibration(
        region_sets: dict[str, list[dict[str, Any]]],
        asset_root: Path,
        calibration: dict[str, Any],
    ) -> None:
        missing: set[str] = set()
        for regions in region_sets.values():
            for region in regions:
                relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                measured = calibration.get(relative)
                if not isinstance(measured, dict) or "detune_cents" not in measured:
                    missing.add(relative)
                    continue
                region["measured_tuning_cents"] = float(measured["detune_cents"])
        if missing:
            preview = ", ".join(sorted(missing)[:3])
            suffix = " ..." if len(missing) > 3 else ""
            raise ValueError(
                f"woodwind pitch calibration is missing {len(missing)} samples: "
                f"{preview}{suffix}"
            )

    def _validate_coverage(
        self, region_sets: dict[str, list[dict[str, Any]]]
    ) -> None:
        for name, regions in region_sets.items():
            minimum = min(float(region["key_min"]) for region in regions)
            maximum = max(float(region["key_max"]) for region in regions)
            if minimum > self.note_min or maximum < self.note_max:
                raise ValueError(
                    f"{self.display_name} {name} SFZ covers MIDI {minimum:g}-{maximum:g}, "
                    f"not the declared {self.note_min:g}-{self.note_max:g} range"
                )

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _note_number(self, event: PerformanceEvent, tuning: EqualTemperament) -> float:
        if "midi_note" in event.payload:
            note = float(event.payload["midi_note"])
        else:
            note = 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)
        if not self.note_min <= note <= self.note_max:
            raise ValueError(
                f"{self.display_name} note {note:.3f} is outside the sampled sounding "
                f"{self.sampled_range} range"
            )
        return note

    def _start_voice(
        self,
        engine_name: str,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        sustained: bool,
        velocity_scale: float = 1.0,
        schedule_short_release: bool = False,
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
        if schedule_short_release:
            self._scheduled_releases.append(
                _ScheduledRelease(
                    engine_name,
                    note_id,
                    self._short_gate_samples,
                    self._short_release_seconds,
                )
            )
        return _VoiceRoute(engine_name, note_id, sustained)

    def _choke_existing_voices(self) -> None:
        for name, engine in self.engines.items():
            release = (
                self.legato_release_seconds
                if name in _SUSTAIN_ENGINES
                else self.tongue_choke_seconds
            )
            for note_id in tuple(engine.voices):
                engine.release_note(note_id, release_seconds=release)
        self._scheduled_releases.clear()
        for route in self.note_routes.values():
            route.voices = ()

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _PUBLIC_ARTICULATIONS:
                choices = ", ".join(sorted(_PUBLIC_ARTICULATIONS))
                raise ValueError(
                    f"unsupported {self.display_name} articulation {name!r}; "
                    f"choose from {choices}"
                )
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
            self._note_number(event, tuning)
            self._choke_existing_voices()
            if self.articulation == "staccato":
                voices = (
                    self._start_voice(
                        "staccato",
                        event,
                        tuning,
                        sustained=False,
                        schedule_short_release=True,
                    ),
                )
            elif self.articulation == "accent":
                voices = (
                    self._start_voice(
                        "accent_attack",
                        event,
                        tuning,
                        sustained=False,
                        velocity_scale=1.04,
                        schedule_short_release=True,
                    ),
                    self._start_voice(
                        "accent_sustain", event, tuning, sustained=True
                    ),
                )
            else:
                voices = (
                    self._start_voice(
                        self.articulation, event, tuning, sustained=True
                    ),
                )
            self.note_routes[int(event.payload["note_id"])] = _NoteRoute(voices)
            return

        if event.type == "note_off":
            route = self.note_routes.pop(int(event.payload["note_id"]), None)
            if route is None:
                return
            for voice in route.voices:
                if voice.sustained:
                    self.engines[voice.engine_name].handle_event(
                        _with_note_id(event, voice.note_id), tuning
                    )

    def render_frame(self) -> StereoFrame:
        pending: list[_ScheduledRelease] = []
        for scheduled in self._scheduled_releases:
            scheduled.remaining_samples -= 1
            if scheduled.remaining_samples <= 0:
                self.engines[scheduled.engine_name].release_note(
                    scheduled.note_id,
                    release_seconds=scheduled.release_seconds,
                )
            else:
                pending.append(scheduled)
        self._scheduled_releases = pending

        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        self.breath += (
            self.breath_target - self.breath
        ) * self._breath_coefficient
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


def create_vpo_solo_woodwind(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoSoloWoodwindInstrument(sample_rate, manifest, base_directory)


def generate_woodwind_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Measure every unique WAV used by one VPO woodwind candidate."""

    from .analysis import analyze_file_pitch

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    region_sets = woodwind_source_regions(asset_root, str(manifest["sfz_prefix"]))
    roots: dict[Path, float] = {}
    for regions in region_sets.values():
        for region in regions:
            path = Path(region["sample"])
            root_midi = float(region["root_midi"])
            previous = roots.setdefault(path, root_midi)
            if not math.isclose(previous, root_midi, abs_tol=1e-9):
                raise ValueError(
                    f"one VPO sample is mapped to inconsistent roots: {path}"
                )

    samples: dict[str, dict[str, float]] = {}
    for path, root_midi in sorted(roots.items(), key=lambda item: item[0].as_posix()):
        expected_hz = 440.0 * (2.0 ** ((root_midi - 69.0) / 12.0))
        measurement = analyze_file_pitch(
            path,
            expected_hz,
            start_seconds=0.18,
            maximum_frames=131_072,
            search_cents=180.0,
        )
        relative = path.relative_to(asset_root).as_posix()
        samples[relative] = {
            "root_midi": root_midi,
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(measurement.detune_cents, 6),
        }

    detunes = [item["detune_cents"] for item in samples.values()]
    document: dict[str, Any] = {
        "description": (
            f"FFT measurement of raw {manifest['instrument_name']} samples; A4=440 Hz"
        ),
        "sfz_prefix": str(manifest["sfz_prefix"]),
        "summary": {
            "sample_count": len(samples),
            "median_detune_cents": round(statistics.median(detunes), 6),
            "maximum_absolute_detune_cents": round(max(map(abs, detunes)), 6),
        },
        "samples": samples,
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / "音准校准.json"
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def generate_woodwind_resource_verification(
    manifest_path: str | Path,
    *,
    license_files: tuple[str, ...],
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze exact SFZ, selected WAV, licence and version evidence hashes."""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    prefix = str(manifest["sfz_prefix"])
    sfz_relatives = tuple(
        f"Woodwinds/{prefix}-SOLO-{suffix}.sfz"
        for suffix in ("normal-mod-wheel", "sustain", "staccato", "accent")
    )
    region_sets = woodwind_source_regions(asset_root, prefix)
    sample_paths = sorted(
        {
            Path(region["sample"])
            for regions in region_sets.values()
            for region in regions
        },
        key=lambda path: path.relative_to(asset_root).as_posix(),
    )
    for path in sample_paths:
        if not path.is_file():
            raise ValueError(f"VPO sample file is missing: {path}")

    source_sfz_sha256 = {
        relative: hashlib.sha256((asset_root / relative).read_bytes()).hexdigest()
        for relative in sfz_relatives
    }
    sample_lines: list[str] = []
    sample_bytes = 0
    for path in sample_paths:
        relative = path.relative_to(asset_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sample_lines.append(f"{digest}  {relative}\n")
        sample_bytes += path.stat().st_size
    sample_set_sha256 = hashlib.sha256(
        "".join(sample_lines).encode("utf-8")
    ).hexdigest()

    def hash_relatives(relatives: tuple[str, ...]) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in relatives:
            path = asset_root / relative
            if not path.is_file():
                raise ValueError(f"VPO evidence file is missing: {path}")
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    document: dict[str, Any] = {
        "upstream": "Virtual Playing Orchestra",
        "sfz_version": "Standard Orchestra 3.3 (2026-06-27)",
        "wave_version": "Wave Files 3.2 (2026-06-27)",
        "source_sfz_sha256": source_sfz_sha256,
        "sample_count": len(sample_paths),
        "sample_bytes": sample_bytes,
        "sample_set_sha256": sample_set_sha256,
        "sample_set_algorithm": (
            "Sort unique VPO-relative UTF-8 paths; for each write "
            "'<lowercase file sha256>  <path>\\n'; SHA-256 the concatenated UTF-8 bytes."
        ),
        "license_file_sha256": hash_relatives(license_files),
        "version_evidence_sha256": hash_relatives(
            (
                "Documentation/change-log-Standard-Orchestra.txt",
                "Documentation/change-log-Wave-Files.txt",
            )
        ),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / "资源核验.json"
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document
