from __future__ import annotations

from configparser import ConfigParser
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import statistics
import struct
from typing import Any

from .audio import audio_file_info
from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .sampler import SampleInstrument
from .tuning import EqualTemperament


_SUPPORTED_ARTICULATIONS = frozenset(("sustain", "legato"))
_HEADER_RE = re.compile(r"<\s*(control|global|master|group|region|curve)\s*>", re.I)
_OPCODE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_$]*)=([^\s]+)")
_DEFINE_RE = re.compile(r"^\s*#define\s+(\$[A-Za-z_][A-Za-z0-9_]*)\s+(.+?)\s*$")
_INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"\s*$')
_UNRESOLVED_MACRO_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class _ExpandedLine:
    path: Path
    line_number: int
    text: str


@dataclass(frozen=True, slots=True)
class MtgSfzDocument:
    path: Path
    regions: tuple[dict[str, str], ...]
    source_files: tuple[Path, ...]
    macros: dict[str, str]


@dataclass(frozen=True, slots=True)
class _NoteRoute:
    engine_name: str
    note_id: int


@dataclass(slots=True)
class _VibratoState:
    base_increment: float
    age_samples: int = 0


def _strip_sfz_comment(line: str) -> str:
    """Strip SFZ ``//`` comments (the MTG mappings do not quote URLs)."""

    return line.split("//", 1)[0].strip()


def _substitute_macros(text: str, macros: dict[str, str]) -> str:
    for name in sorted(macros, key=len, reverse=True):
        text = text.replace(name, macros[name])
    return text


def _expand_sfz(
    path: Path,
    *,
    root: Path,
    macros: dict[str, str],
    source_files: set[Path],
    stack: tuple[Path, ...],
) -> list[_ExpandedLine]:
    source = path.resolve()
    root = root.resolve()
    if not source.is_relative_to(root):
        raise ValueError(f"SFZ include escapes the library root: {source}")
    if source in stack:
        chain = " -> ".join(str(item) for item in (*stack, source))
        raise ValueError(f"cyclic SFZ include: {chain}")
    if not source.is_file():
        raise ValueError(f"MTG Solo Sax SFZ include is missing: {source}")

    source_files.add(source)
    expanded: list[_ExpandedLine] = []
    for line_number, raw_line in enumerate(
        source.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        line = _strip_sfz_comment(raw_line)
        if not line:
            continue
        define = _DEFINE_RE.match(line)
        if define:
            macros[define.group(1)] = _substitute_macros(
                define.group(2).strip(), macros
            )
            continue
        include = _INCLUDE_RE.match(line)
        if include:
            include_name = _substitute_macros(include.group(1), macros).replace(
                "\\", "/"
            )
            # sfzinstruments/MTG.SoloSax writes Data/foo includes from files
            # already inside Data/.  ARIA resolves these from the top-level SFZ
            # directory, so prefer that interpretation and retain a local-file
            # fallback for ordinary SFZ include trees.
            candidates = (root / include_name, source.parent / include_name)
            include_path = next(
                (candidate for candidate in candidates if candidate.is_file()),
                candidates[0],
            )
            expanded.extend(
                _expand_sfz(
                    include_path,
                    root=root,
                    macros=macros,
                    source_files=source_files,
                    stack=(*stack, source),
                )
            )
            continue
        substituted = _substitute_macros(line, macros)
        unresolved = _UNRESOLVED_MACRO_RE.search(substituted)
        if unresolved:
            raise ValueError(
                f"unresolved SFZ macro {unresolved.group(0)} at "
                f"{source}:{line_number}"
            )
        expanded.append(_ExpandedLine(source, line_number, substituted))
    return expanded


def parse_mtg_sfz(path: str | Path) -> MtgSfzDocument:
    """Expand and parse the include/macro dialect used by MTG Solo Sax.

    This is deliberately independent from Tianlai's general SFZ adapters.  It
    implements the inheritance needed by this library (global/master/group /
    region), records the originating include file, and fails on missing or
    escaping includes instead of silently producing a partial instrument.
    """

    source_path = Path(path).resolve()
    macros: dict[str, str] = {}
    source_files: set[Path] = set()
    lines = _expand_sfz(
        source_path,
        root=source_path.parent,
        macros=macros,
        source_files=source_files,
        stack=(),
    )

    control: dict[str, str] = {}
    global_values: dict[str, str] = {}
    master: dict[str, str] = {}
    group: dict[str, str] = {}
    current_kind = ""
    current: dict[str, str] = {}
    current_source: _ExpandedLine | None = None
    regions: list[dict[str, str]] = []

    def finish_region() -> None:
        nonlocal current, current_source
        if current_kind != "region" or current_source is None:
            return
        merged = {
            **control,
            **global_values,
            **master,
            **group,
            **current,
            "_source_file": str(current_source.path),
            "_source_line": str(current_source.line_number),
        }
        regions.append(merged)
        current = {}
        current_source = None

    for expanded_line in lines:
        header = _HEADER_RE.search(expanded_line.text)
        opcode_text = expanded_line.text
        if header:
            finish_region()
            current_kind = header.group(1).lower()
            opcode_text = expanded_line.text[header.end() :]
            if current_kind == "control":
                current = control
            elif current_kind == "global":
                global_values = {}
                master = {}
                group = {}
                current = global_values
            elif current_kind == "master":
                master = {}
                group = {}
                current = master
            elif current_kind == "group":
                group = {}
                current = group
            elif current_kind == "region":
                current = {}
                current_source = expanded_line
            else:  # curve
                current = {}
        elif current_kind == "region" and current_source is None:
            current_source = expanded_line

        for opcode in _OPCODE_RE.finditer(opcode_text):
            current[opcode.group(1).lower()] = opcode.group(2)

    finish_region()
    if not regions:
        raise ValueError(f"MTG Solo Sax SFZ contains no regions: {source_path}")
    return MtgSfzDocument(
        source_path,
        tuple(regions),
        tuple(sorted(source_files, key=lambda item: item.as_posix())),
        dict(macros),
    )


def flac_loop_points(path: str | Path) -> tuple[int, int] | None:
    """Read a WAV ``smpl`` loop preserved in a FLAC ``riff`` block.

    The upstream conversion retained loop metadata as FLAC APPLICATION blocks.
    libsndfile decodes the audio but does not expose those RIFF chunks, so this
    small metadata reader is necessary for genuine sustained notes.
    """

    source = Path(path)
    with source.open("rb") as stream:
        if stream.read(4) != b"fLaC":
            return None
        last = False
        while not last:
            header = stream.read(4)
            if len(header) != 4:
                return None
            encoded = int.from_bytes(header, "big")
            last = bool(encoded & 0x80000000)
            block_type = (encoded >> 24) & 0x7F
            block_size = encoded & 0xFFFFFF
            payload = stream.read(block_size)
            if len(payload) != block_size:
                return None
            if block_type != 2 or not payload.startswith(b"riffsmpl"):
                continue
            if len(payload) < 12:
                continue
            chunk_size = struct.unpack_from("<I", payload, 8)[0]
            chunk = payload[12 : 12 + chunk_size]
            if len(chunk) < 60:
                continue
            loop_count = struct.unpack_from("<I", chunk, 28)[0]
            if loop_count < 1:
                continue
            loop_type, start, inclusive_end = struct.unpack_from("<III", chunk, 40)
            if loop_type != 0 or inclusive_end < start:
                continue
            return int(start), int(inclusive_end + 1)
    return None


def _velocity_limits(values: dict[str, str]) -> tuple[float, float]:
    low = max(0.0, (float(values.get("lovel", 0.0)) - 0.5) / 127.0)
    high = min(1.0, (float(values.get("hivel", 127.0)) + 0.5) / 127.0)
    return low, high


def _asset_paths(
    manifest: dict[str, Any], base_directory: str | Path
) -> tuple[Path, Path]:
    asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
    sfz_path = (asset_root / str(manifest["sfz_file"])).resolve()
    if not asset_root.is_dir():
        raise ValueError(
            f"{manifest.get('display_name', '萨克斯')} MTG Solo Sax 音源不存在："
            f"{asset_root}。请按 来源.md 安装固定版本资源。"
        )
    if not sfz_path.is_file():
        raise ValueError(f"MTG Solo Sax 主映射不存在：{sfz_path}")
    if not sfz_path.is_relative_to(asset_root):
        raise ValueError(f"MTG Solo Sax 主映射越出资源根目录：{sfz_path}")
    return asset_root, sfz_path


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _source_raw_midi(values: dict[str, str]) -> float:
    root = float(values.get("pitch_keycenter", values["key"]))
    tune = float(values.get("tune", 0.0))
    return root - tune / 100.0


def _pitch_regions(
    document: MtgSfzDocument,
    *,
    asset_root: Path,
    calibration: dict[str, Any] | None,
    attack_seconds: float,
    offset_frames: int = 0,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    calibration_samples = (
        calibration.get("samples") if isinstance(calibration, dict) else None
    )
    for index, values in enumerate(document.regions):
        if values.get("master_label") != "attack":
            continue
        sample_name = values.get("sample")
        if not sample_name or "key" not in values:
            continue
        default_path = values.get("default_path", "")
        sample_path = (
            document.path.parent
            / default_path.replace("\\", "/")
            / sample_name.replace("\\", "/")
        ).resolve()
        if not sample_path.is_relative_to(asset_root):
            raise ValueError(f"MTG Solo Sax sample escapes the asset root: {sample_path}")
        if not sample_path.is_file():
            raise ValueError(f"MTG Solo Sax sample is missing: {sample_path}")
        loop = flac_loop_points(sample_path)
        if loop is None:
            raise ValueError(f"pitched MTG Solo Sax FLAC has no smpl loop: {sample_path}")

        relative = _relative(sample_path, asset_root)
        inferred_raw_midi = _source_raw_midi(values)
        if not isinstance(calibration_samples, dict):
            raw_midi = inferred_raw_midi
        else:
            evidence = calibration_samples.get(relative)
            if not isinstance(evidence, dict) or "source_raw_midi" not in evidence:
                raise ValueError(f"pitch calibration is missing MTG sample: {relative}")
            raw_midi = float(evidence["source_raw_midi"])
            if not math.isclose(raw_midi, inferred_raw_midi, abs_tol=0.0002):
                raise ValueError(
                    f"pitch calibration/source tune mismatch for {relative}: "
                    f"{raw_midi:.6f} vs {inferred_raw_midi:.6f} MIDI"
                )

        source_rate, frame_count, _ = audio_file_info(sample_path)
        start = min(max(0, int(offset_frames)), frame_count - 2)
        velocity_min, velocity_max = _velocity_limits(values)
        key = float(values["key"])
        rr_position = int(values.get("seq_position", 1))
        rr_length = int(values.get("seq_length", 1))
        converted.append(
            {
                "sample": str(sample_path),
                "root_pitch_hz": 440.0 * (2.0 ** ((raw_midi - 69.0) / 12.0)),
                "key_min": key,
                "key_max": key,
                "velocity_min": velocity_min,
                "velocity_max": velocity_max,
                "gain_db": float(values.get("volume", 0.0)),
                "attack_seconds": attack_seconds,
                "offset_frames": start,
                "loop_start": loop[0],
                "loop_end": loop[1],
                "loop_mode": "loop_sustain",
                "stable_key": relative,
                "round_robin_position": rr_position,
                "round_robin_length": rr_length,
            }
        )
    if not converted:
        raise ValueError(f"MTG Solo Sax mapping has no attack pitch regions: {document.path}")
    return converted


def _noise_regions(
    document: MtgSfzDocument,
    *,
    asset_root: Path,
    group_label: str,
) -> list[dict[str, Any]]:
    selected: list[tuple[dict[str, str], Path, str]] = []
    for values in document.regions:
        if values.get("master_label") != "noises":
            continue
        if values.get("group_label") != group_label:
            continue
        sample_name = values.get("sample")
        if not sample_name:
            continue
        sample_path = (
            document.path.parent
            / values.get("default_path", "").replace("\\", "/")
            / sample_name.replace("\\", "/")
        ).resolve()
        if not sample_path.is_relative_to(asset_root) or not sample_path.is_file():
            raise ValueError(f"MTG Solo Sax noise sample is missing: {sample_path}")
        selected.append((values, sample_path, _relative(sample_path, asset_root)))
    if not selected:
        raise ValueError(
            f"MTG Solo Sax mapping has no {group_label!r} noise regions: {document.path}"
        )

    # ARIA uses four sequence groups plus random slices inside each group.  The
    # Tianlai candidate flattens that pool to a deterministic cycle, preserving
    # every real recording without introducing process-dependent randomness.
    selected.sort(
        key=lambda item: (
            int(item[0].get("seq_position", 1)),
            float(item[0].get("lorand", 0.0)),
            item[2],
        )
    )
    length = len(selected)
    return [
        {
            "sample": str(sample_path),
            "root_pitch_hz": 440.0,
            "velocity_min": 0.0,
            "velocity_max": 1.0,
            "gain_db": float(values.get("volume", 0.0)),
            "loop_mode": "one_shot",
            "stable_key": relative,
            "round_robin_position": position,
            "round_robin_length": length,
        }
        for position, (values, sample_path, relative) in enumerate(selected, start=1)
    ]


def mtg_sax_source_inventory(
    manifest: dict[str, Any], *, base_directory: str | Path
) -> dict[str, Any]:
    """Return the fully expanded source inventory without decoding every FLAC."""

    asset_root, sfz_path = _asset_paths(manifest, base_directory)
    document = parse_mtg_sfz(sfz_path)
    pitch = _pitch_regions(
        document,
        asset_root=asset_root,
        calibration=None,
        attack_seconds=float(manifest.get("fast_attack_seconds", 0.0)),
    )
    breath = _noise_regions(document, asset_root=asset_root, group_label="breath")
    keys = _noise_regions(document, asset_root=asset_root, group_label="key-clicks")
    return {
        "asset_root": asset_root,
        "document": document,
        "pitch_regions": pitch,
        "breath_regions": breath,
        "key_click_regions": keys,
    }


class MtgSoloSaxInstrument(Instrument):
    """Dedicated deterministic runtime for sfzinstruments/MTG.SoloSax."""

    def __init__(
        self, sample_rate: int, manifest: dict[str, Any], base_directory: str
    ) -> None:
        super().__init__(sample_rate)
        self.display_name = str(manifest.get("display_name", manifest["name"]))
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        self.sampled_range = str(manifest["sampled_range"])
        self.written_note_min = float(manifest["written_note_min"])
        self.written_note_max = float(manifest["written_note_max"])
        self.written_range = str(manifest["written_range"])
        self.written_to_sounding_semitones = float(
            manifest["written_to_sounding_semitones"]
        )
        if self.note_min > self.note_max:
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
            raise ValueError("MTG Solo Sax pitch_input must be 'sounding'")

        asset_root, sfz_path = _asset_paths(manifest, base_directory)
        calibration_path = (
            Path(base_directory) / str(manifest["pitch_calibration"])
        ).resolve()
        if not calibration_path.is_file():
            raise ValueError(
                f"{self.display_name} 音准校准表不存在：{calibration_path}。"
                "请运行同目录的 校准音准.py。"
            )
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        if calibration.get("upstream_commit") != manifest.get("upstream_commit"):
            raise ValueError(
                f"{self.display_name} calibration is not for the frozen upstream commit"
            )

        document = parse_mtg_sfz(sfz_path)
        attack_regions = _pitch_regions(
            document,
            asset_root=asset_root,
            calibration=calibration,
            attack_seconds=max(0.0, float(manifest.get("fast_attack_seconds", 0.0))),
        )
        legato_regions = _pitch_regions(
            document,
            asset_root=asset_root,
            calibration=calibration,
            attack_seconds=max(0.0, float(manifest.get("legato_attack_seconds", 0.05))),
            offset_frames=max(0, int(manifest.get("legato_offset_frames", 20_000))),
        )
        self._validate_pitch_coverage(attack_regions)
        breath_regions = _noise_regions(
            document, asset_root=asset_root, group_label="breath"
        )
        key_click_regions = _noise_regions(
            document, asset_root=asset_root, group_label="key-clicks"
        )

        shared_cache: dict[Path, Any] = {}
        engine_data = {
            "reference_a4_hz": 440.0,
            "gain": float(manifest.get("gain", 0.42)),
            "velocity_exponent": float(manifest.get("velocity_exponent", 0.7)),
            "release_seconds": float(manifest.get("release_seconds", 0.4)),
            "resampling_quality": str(
                manifest.get("resampling_quality", "linear")
            ),
        }
        self.engines = {
            "sustain": SampleInstrument.from_manifest(
                {**engine_data, "regions": attack_regions},
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            ),
            "legato": SampleInstrument.from_manifest(
                {**engine_data, "regions": legato_regions},
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            ),
        }
        noise_data = {
            "reference_a4_hz": 440.0,
            "gain": float(manifest.get("noise_gain", 0.035)),
            "velocity_exponent": 0.55,
            "release_seconds": 0.04,
            "resampling_quality": str(
                manifest.get("resampling_quality", "linear")
            ),
        }
        self.noise_engines = {
            "breath": SampleInstrument.from_manifest(
                {**noise_data, "regions": breath_regions},
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            ),
            "key_clicks": SampleInstrument.from_manifest(
                {**noise_data, "regions": key_click_regions},
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            ),
        }

        articulation = str(manifest.get("default_articulation", "sustain"))
        if articulation not in _SUPPORTED_ARTICULATIONS:
            raise ValueError(f"unsupported {self.display_name} articulation: {articulation}")
        self.articulation = articulation
        self.note_routes: dict[int, _NoteRoute] = {}
        self.held_notes: set[int] = set()
        self._auxiliary_note_id = int(
            manifest.get("auxiliary_note_id_base", 1_520_000_000)
        )
        self.legato_release_seconds = max(
            0.001, float(manifest.get("legato_release_seconds", 0.05))
        )

        self.expression = self.expression_target = 1.0
        self.breath = self.breath_target = 1.0
        self.modulation = self.modulation_target = 0.0
        self.noise = self.noise_target = min(
            1.0, max(0.0, float(manifest.get("noise_default", 0.22)))
        )
        self._expression_coefficient = self._smoothing_coefficient(
            float(manifest.get("expression_smoothing_seconds", 0.014))
        )
        self._breath_coefficient = self._smoothing_coefficient(
            float(manifest.get("breath_smoothing_seconds", 0.024))
        )
        self._modulation_coefficient = self._smoothing_coefficient(
            float(manifest.get("modulation_smoothing_seconds", 0.025))
        )
        self._noise_coefficient = self._smoothing_coefficient(
            float(manifest.get("noise_smoothing_seconds", 0.02))
        )
        self.vibrato_depth_cents = max(
            0.0, float(manifest.get("vibrato_depth_cents", 50.0))
        )
        self.vibrato_rate_hz = max(
            0.01, float(manifest.get("vibrato_rate_hz", 5.0))
        )
        self.vibrato_fade_samples = max(
            1, round(float(manifest.get("vibrato_fade_seconds", 2.0)) * sample_rate)
        )
        self._vibrato_phase = 0.0
        self._vibrato_states: dict[tuple[str, int], _VibratoState] = {}

    def _smoothing_coefficient(self, seconds: float) -> float:
        seconds = max(0.001, seconds)
        return 1.0 - math.exp(-1.0 / (seconds * self.sample_rate))

    def _validate_pitch_coverage(self, regions: list[dict[str, Any]]) -> None:
        minimum = min(float(item["key_min"]) for item in regions)
        maximum = max(float(item["key_max"]) for item in regions)
        if minimum != self.note_min or maximum != self.note_max:
            raise ValueError(
                f"{self.display_name} SFZ covers MIDI {minimum:g}-{maximum:g}, "
                f"not declared {self.note_min:g}-{self.note_max:g}"
            )

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _note_number(
        self, event: PerformanceEvent, tuning: EqualTemperament
    ) -> float:
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

    def _start_noise(
        self,
        engine_name: str,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        velocity: float,
    ) -> None:
        note_id = self._next_auxiliary_id()
        self.noise_engines[engine_name].handle_event(
            PerformanceEvent(
                event.sample,
                event.sequence,
                "note_on",
                {
                    "note_id": note_id,
                    "pitch_hz": 440.0,
                    "velocity": min(1.0, max(0.0, velocity)),
                },
            ),
            tuning,
        )

    def _release_for_legato(self) -> None:
        for engine_name, engine in self.engines.items():
            for note_id in tuple(engine.voices):
                engine.release_note(
                    note_id, release_seconds=self.legato_release_seconds
                )
                self._vibrato_states.pop((engine_name, note_id), None)

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _SUPPORTED_ARTICULATIONS:
                choices = ", ".join(sorted(_SUPPORTED_ARTICULATIONS))
                raise ValueError(
                    f"unsupported {self.display_name} articulation {name!r}; "
                    f"choose from {choices}"
                )
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload["name"])
            value = min(1.0, max(0.0, float(event.payload["value"])))
            if name == "expression":
                self.expression_target = value**1.3
            elif name == "breath":
                self.breath_target = value**1.08
            elif name == "modulation":
                self.modulation_target = value
            elif name == "noise":
                self.noise_target = value
            elif name == "sustain_pedal":
                for engine in self.engines.values():
                    engine.handle_event(event, tuning)
            return

        if event.type == "note_on":
            self._note_number(event, tuning)
            public_id = int(event.payload["note_id"])
            if public_id in self.note_routes:
                raise ValueError(f"{self.display_name} note_id {public_id} is already active")
            is_legato_continuation = (
                self.articulation == "legato" and bool(self.held_notes)
            )
            if is_legato_continuation:
                self._release_for_legato()
            engine_name = "legato" if is_legato_continuation else "sustain"
            note_id = self._next_auxiliary_id()
            routed = PerformanceEvent(
                event.sample,
                event.sequence,
                "note_on",
                {**event.payload, "note_id": note_id},
            )
            engine = self.engines[engine_name]
            engine.handle_event(routed, tuning)
            voice = engine.voices[note_id]
            self._vibrato_states[(engine_name, note_id)] = _VibratoState(
                voice.increment
            )
            self.note_routes[public_id] = _NoteRoute(engine_name, note_id)
            self.held_notes.add(public_id)
            if not is_legato_continuation:
                # One breath transient belongs to the phrase onset.  Replaying
                # it on every overlapped legato note produces an artificial
                # inhale inside a continuous air stream.
                self._start_noise(
                    "breath",
                    event,
                    tuning,
                    float(event.payload.get("velocity", 0.8)),
                )
            return

        if event.type == "note_off":
            public_id = int(event.payload["note_id"])
            self.held_notes.discard(public_id)
            route = self.note_routes.pop(public_id, None)
            if route is not None:
                self.engines[route.engine_name].handle_event(
                    PerformanceEvent(
                        event.sample,
                        event.sequence,
                        "note_off",
                        {**event.payload, "note_id": route.note_id},
                    ),
                    tuning,
                )
            self._start_noise(
                "key_clicks",
                event,
                tuning,
                float(event.payload.get("release_velocity", 0.5)),
            )

    def _apply_vibrato(self) -> None:
        sine = math.sin(self._vibrato_phase)
        active: set[tuple[str, int]] = set()
        for engine_name, engine in self.engines.items():
            for note_id, voice in engine.voices.items():
                key = (engine_name, note_id)
                active.add(key)
                state = self._vibrato_states.get(key)
                if state is None:
                    state = _VibratoState(voice.increment)
                    self._vibrato_states[key] = state
                fade = min(1.0, state.age_samples / self.vibrato_fade_samples)
                cents = self.vibrato_depth_cents * self.modulation * fade * sine
                voice.increment = state.base_increment * (2.0 ** (cents / 1200.0))
                state.age_samples += 1
        for key in tuple(self._vibrato_states):
            if key not in active:
                del self._vibrato_states[key]
        self._vibrato_phase = math.fmod(
            self._vibrato_phase
            + (2.0 * math.pi * self.vibrato_rate_hz / self.sample_rate),
            2.0 * math.pi,
        )

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        self.breath += (self.breath_target - self.breath) * self._breath_coefficient
        self.modulation += (
            self.modulation_target - self.modulation
        ) * self._modulation_coefficient
        self.noise += (self.noise_target - self.noise) * self._noise_coefficient
        self._apply_vibrato()

        pitched_left = 0.0
        pitched_right = 0.0
        for engine in self.engines.values():
            left, right = engine.render_frame()
            pitched_left += left
            pitched_right += right
        noise_left = 0.0
        noise_right = 0.0
        for engine in self.noise_engines.values():
            left, right = engine.render_frame()
            noise_left += left
            noise_right += right

        amplitude = self.expression * self.breath
        noise_amplitude = self.expression * (0.35 + 0.65 * self.breath) * self.noise
        return (
            pitched_left * amplitude + noise_left * noise_amplitude,
            pitched_right * amplitude + noise_right * noise_amplitude,
        )

    @property
    def active_voice_count(self) -> int:
        return sum(
            engine.active_voice_count
            for engine in (*self.engines.values(), *self.noise_engines.values())
        )


def create_mtg_sax(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return MtgSoloSaxInstrument(sample_rate, manifest, base_directory)


def _canonical_pitch_samples(
    manifest: dict[str, Any], base_directory: str | Path
) -> tuple[Path, MtgSfzDocument, dict[Path, dict[str, Any]]]:
    asset_root, sfz_path = _asset_paths(manifest, base_directory)
    document = parse_mtg_sfz(sfz_path)
    mappings: dict[Path, list[dict[str, str]]] = {}
    for values in document.regions:
        if values.get("master_label") != "attack" or "sample" not in values:
            continue
        sample_path = (
            document.path.parent
            / values.get("default_path", "").replace("\\", "/")
            / values["sample"].replace("\\", "/")
        ).resolve()
        mappings.setdefault(sample_path, []).append(values)

    canonical: dict[Path, dict[str, Any]] = {}
    for sample_path, values_list in mappings.items():
        if not sample_path.is_file():
            raise ValueError(f"MTG Solo Sax sample is missing: {sample_path}")
        inferred = [_source_raw_midi(values) for values in values_list]
        if max(inferred) - min(inferred) > 0.0002:
            raise ValueError(
                f"inconsistent SFZ tune/root mapping for {sample_path}: {inferred}"
            )
        chosen = min(values_list, key=lambda item: abs(float(item.get("tune", 0.0))))
        loop = flac_loop_points(sample_path)
        if loop is None:
            raise ValueError(f"pitched MTG Solo Sax FLAC has no smpl loop: {sample_path}")
        canonical[sample_path] = {
            "source_raw_midi": statistics.median(inferred),
            "canonical_root_midi": float(
                chosen.get("pitch_keycenter", chosen["key"])
            ),
            "canonical_tune_cents": float(chosen.get("tune", 0.0)),
            "mapping_count": len(values_list),
            "loop_start": loop[0],
            "loop_end": loop[1],
        }
    if not canonical:
        raise ValueError(f"MTG Solo Sax mapping has no pitched samples: {sfz_path}")
    return asset_root, document, canonical


def generate_mtg_sax_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
    measure_audio: bool = True,
) -> dict[str, Any]:
    """Freeze source ``tune`` calibration and optionally audit every FLAC.

    Playback follows the upstream author's root+``tune`` absolute pitch.  The
    FFT measurement is diagnostic: sustained saxophone loops contain natural
    vibrato, so applying a single-window residual would make tuning less, not
    more, reliable.  The report keeps both values and states that distinction.
    """

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    asset_root, document, canonical = _canonical_pitch_samples(
        manifest, source_manifest.parent
    )
    samples: dict[str, dict[str, Any]] = {}
    residuals: list[float] = []
    for sample_path, evidence in sorted(
        canonical.items(), key=lambda item: _relative(item[0], asset_root)
    ):
        raw_midi = float(evidence["source_raw_midi"])
        expected_hz = 440.0 * (2.0 ** ((raw_midi - 69.0) / 12.0))
        source_rate, frame_count, channels = audio_file_info(sample_path)
        item: dict[str, Any] = {
            "source_raw_midi": round(raw_midi, 6),
            "source_expected_hz": round(expected_hz, 6),
            "canonical_root_midi": evidence["canonical_root_midi"],
            "canonical_tune_cents": evidence["canonical_tune_cents"],
            "mapping_count": evidence["mapping_count"],
            "loop_start": evidence["loop_start"],
            "loop_end": evidence["loop_end"],
            "sample_rate": source_rate,
            "frame_count": frame_count,
            "channels": channels,
        }
        if measure_audio:
            from .analysis import analyze_file_pitch

            loop_length = int(evidence["loop_end"]) - int(evidence["loop_start"])
            start_frame = int(evidence["loop_start"]) + min(
                max(2_400, loop_length // 8), max(0, loop_length - 4_096)
            )
            maximum_frames = max(4_096, min(65_536, frame_count - start_frame))
            measurement = analyze_file_pitch(
                sample_path,
                expected_hz,
                start_seconds=start_frame / source_rate,
                maximum_frames=maximum_frames,
                search_cents=90.0,
            )
            residual = float(measurement.detune_cents)
            residuals.append(residual)
            item.update(
                {
                    "measured_hz": round(measurement.measured_hz, 6),
                    "measurement_minus_source_cents": round(residual, 6),
                    "measurement_window_start_frame": start_frame,
                    "measurement_maximum_frames": maximum_frames,
                }
            )
        samples[_relative(sample_path, asset_root)] = item

    summary: dict[str, Any] = {
        "sample_count": len(samples),
        "source_region_count": sum(
            1
            for values in document.regions
            if values.get("master_label") == "attack" and "sample" in values
        ),
        "looped_sample_count": sum(
            1 for item in samples.values() if item["loop_end"] > item["loop_start"]
        ),
    }
    if residuals:
        summary.update(
            {
                "median_measurement_minus_source_cents": round(
                    statistics.median(residuals), 6
                ),
                "maximum_absolute_measurement_minus_source_cents": round(
                    max(map(abs, residuals)), 6
                ),
            }
        )
    document_out: dict[str, Any] = {
        "upstream": "sfzinstruments/MTG.SoloSax",
        "upstream_commit": str(manifest["upstream_commit"]),
        "reference_a4_hz": 440.0,
        "playback_calibration": "upstream pitch_keycenter/key plus tune opcode",
        "measurement_role": (
            "diagnostic only; natural sax vibrato makes a single FFT window "
            "unsuitable as a replacement for the upstream tune map"
        ),
        "measurement_algorithm": (
            "windowed FFT near source-tuned pitch, within the embedded sustain loop, "
            "search +/-90 cents"
            if measure_audio
            else "not run"
        ),
        "summary": summary,
        "samples": samples,
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / str(manifest["pitch_calibration"])
    )
    destination.write_text(
        json.dumps(document_out, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document_out


def _git_commit(repository: Path) -> str | None:
    git_dir = repository / ".git"
    head = git_dir / "HEAD"
    if not head.is_file():
        return None
    value = head.read_text(encoding="utf-8").strip()
    if not value.startswith("ref: "):
        return value
    reference = value[5:]
    loose = git_dir / reference
    if loose.is_file():
        return loose.read_text(encoding="utf-8").strip()
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1] == reference:
                return parts[0]
    return None


def _git_origin(repository: Path) -> str | None:
    config_path = repository / ".git" / "config"
    if not config_path.is_file():
        return None
    parser = ConfigParser()
    parser.read(config_path, encoding="utf-8")
    section = 'remote "origin"'
    return parser.get(section, "url", fallback=None)


def generate_mtg_sax_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Hash every expanded SFZ source, used FLAC and licence evidence file."""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    inventory = mtg_sax_source_inventory(manifest, base_directory=source_manifest.parent)
    asset_root: Path = inventory["asset_root"]
    document: MtgSfzDocument = inventory["document"]
    sample_paths = sorted(
        {
            Path(item["sample"])
            for key in ("pitch_regions", "breath_regions", "key_click_regions")
            for item in inventory[key]
        },
        key=lambda item: _relative(item, asset_root),
    )
    sample_lines: list[str] = []
    sample_bytes = 0
    formats: dict[str, int] = {}
    for sample_path in sample_paths:
        relative = _relative(sample_path, asset_root)
        digest = hashlib.sha256(sample_path.read_bytes()).hexdigest()
        sample_lines.append(f"{digest}  {relative}\n")
        sample_bytes += sample_path.stat().st_size
        sample_rate, _, channels = audio_file_info(sample_path)
        key = f"{sample_path.suffix.lower()}:{sample_rate}Hz:{channels}ch"
        formats[key] = formats.get(key, 0) + 1

    source_hashes = {
        _relative(path, asset_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in document.source_files
    }
    evidence_hashes: dict[str, str] = {}
    for relative in ("LICENSE", "README.md"):
        path = asset_root / relative
        if not path.is_file():
            raise ValueError(f"MTG Solo Sax evidence file is missing: {path}")
        evidence_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()

    installed_commit = _git_commit(asset_root)
    expected_commit = str(manifest["upstream_commit"])
    if installed_commit is not None and installed_commit != expected_commit:
        raise ValueError(
            f"MTG Solo Sax commit mismatch: {installed_commit} != {expected_commit}"
        )
    report: dict[str, Any] = {
        "upstream": "sfzinstruments/MTG.SoloSax",
        "origin": _git_origin(asset_root)
        or "https://github.com/sfzinstruments/MTG.SoloSax.git",
        "upstream_commit": expected_commit,
        "installed_git_commit": installed_commit,
        "license": "CC-BY-4.0",
        "source_file_count": len(source_hashes),
        "source_file_sha256": source_hashes,
        "evidence_sha256": evidence_hashes,
        "sample_count": len(sample_paths),
        "sample_bytes": sample_bytes,
        "sample_formats": formats,
        "pitched_region_count": len(inventory["pitch_regions"]),
        "unique_pitched_sample_count": len(
            {Path(item["sample"]) for item in inventory["pitch_regions"]}
        ),
        "looped_pitched_region_count": sum(
            1
            for item in inventory["pitch_regions"]
            if "loop_start" in item and "loop_end" in item
        ),
        "breath_sample_count": len(inventory["breath_regions"]),
        "key_click_sample_count": len(inventory["key_click_regions"]),
        "sample_set_sha256": hashlib.sha256(
            "".join(sample_lines).encode("utf-8")
        ).hexdigest(),
        "sample_set_hash_algorithm": (
            "sort unique asset-root-relative UTF-8 paths; concatenate lowercase "
            "'<sha256>  <path>\\n>'; SHA-256 the UTF-8 bytes"
        ),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / str(manifest["resource_verification"])
    )
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def generate_mtg_sax_audition_verification(
    manifest_path: str | Path,
    events_path: str | Path,
    wav_path: str | Path,
    *,
    output_path: str | Path,
    coverage: list[str],
) -> dict[str, Any]:
    """Render one fixed audition and record objective WAV/hash evidence."""

    import numpy as np
    import soundfile as sf

    from .renderer import render_to_wav

    manifest_path = Path(manifest_path).resolve()
    events_path = Path(events_path).resolve()
    wav_path = Path(wav_path).resolve()
    result = render_to_wav(manifest_path, events_path, wav_path)
    audio, sample_rate = sf.read(str(wav_path), dtype="float32", always_2d=True)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64)))) if audio.size else 0.0
    clipped = int(np.count_nonzero(np.abs(audio) >= 1.0))
    workspace_root = manifest_path.parents[3]
    try:
        wav_label = wav_path.relative_to(workspace_root).as_posix()
    except ValueError:
        wav_label = str(wav_path)
    report: dict[str, Any] = {
        "status": "machine_pass_human_pending",
        "rendered_at": "2026-07-22",
        "platform": f"{platform.system()} Chinese-path workspace",
        "sample_rate": int(sample_rate),
        "channels": int(audio.shape[1]),
        "frame_count": int(audio.shape[0]),
        "duration_seconds": round(audio.shape[0] / sample_rate, 6),
        "peak_active_voices": int(result.peak_active_voices),
        "peak": round(peak, 6),
        "rms": round(rms, 6),
        "clipped_samples": clipped,
        "wav": wav_label,
        "wav_sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(),
        "coverage": coverage,
        "human_review": "pending",
    }
    Path(output_path).resolve().write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
