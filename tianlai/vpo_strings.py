from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import statistics
from typing import Any

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .sampler import SampleInstrument
from .sfz import note_number
from .tuning import EqualTemperament


_HEADER = re.compile(r"<(control|global|master|group|region)>", re.IGNORECASE)
_OPCODE_START = re.compile(r"(?<!\S)([A-Za-z_][A-Za-z0-9_]*)=")
_ONE_SHOTS = frozenset(("staccato", "pizzicato"))
_PUBLIC_ARTICULATIONS = frozenset(
    ("sustain", "slow_sustain", "staccato", "pizzicato", "accent")
)


def _read_sample_gain_db_overrides(
    manifest: dict[str, Any],
    sample_variant: str,
) -> dict[str, float]:
    """Read exact gain corrections scoped to the active sample variant.

    Upstream SFZ ``volume`` values are authoring choices rather than a
    perceptual-loudness guarantee.  A small number of independently recorded
    zones can therefore remain audibly discontinuous after the upstream
    mapping is applied.  Corrections live in the tracked instrument manifest
    instead of modifying ignored WAV/SFZ assets.  Each correction names its
    ``SOLO`` or ``SEC`` family explicitly, so selecting another legal family
    neither applies the wrong correction nor fails because the other family's
    path is absent.  Exact paths still fail closed inside the selected family.
    """

    raw = manifest.get("sample_gain_db_overrides")
    if raw is None:
        return {}
    if not isinstance(raw, list) or not raw:
        raise ValueError("sample_gain_db_overrides must be a non-empty array")

    overrides: dict[str, float] = {}
    seen: set[tuple[str, str]] = set()
    required = {"sample_variant", "sample", "gain_db"}
    for index, specification in enumerate(raw):
        if not isinstance(specification, dict):
            raise ValueError(
                f"sample_gain_db_overrides[{index}] must be an object"
            )
        unknown = sorted(set(specification) - required)
        if unknown:
            raise ValueError(
                f"sample_gain_db_overrides[{index}] contains unknown fields: "
                + ", ".join(unknown)
            )
        if set(specification) != required:
            raise ValueError(
                f"sample_gain_db_overrides[{index}] must declare "
                "sample_variant, sample, and gain_db"
            )

        raw_variant = specification["sample_variant"]
        if raw_variant not in ("SOLO", "SEC"):
            raise ValueError(
                f"sample_gain_db_overrides[{index}].sample_variant must be "
                f"SOLO or SEC, got {raw_variant!r}"
            )

        raw_path = specification["sample"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(
                f"sample_gain_db_overrides[{index}].sample must be a "
                "non-empty relative path"
            )
        relative = PurePosixPath(raw_path)
        if (
            relative.is_absolute()
            or relative.as_posix() != raw_path
            or any(part in (".", "..") for part in relative.parts)
        ):
            raise ValueError(
                "sample_gain_db_overrides keys must be canonical asset-relative "
                f"POSIX paths, got {raw_path!r}"
            )

        identity = (raw_variant, raw_path)
        if identity in seen:
            raise ValueError(
                "duplicate sample_gain_db_overrides entry for "
                f"{raw_variant} {raw_path!r}"
            )
        seen.add(identity)

        raw_gain = specification["gain_db"]
        if (
            isinstance(raw_gain, bool)
            or not isinstance(raw_gain, (int, float))
            or not math.isfinite(float(raw_gain))
            or not -24.0 <= float(raw_gain) <= 24.0
        ):
            raise ValueError(
                "sample_gain_db_overrides values must be finite dB corrections "
                f"between -24 and +24, got {raw_gain!r} for {raw_path!r}"
            )
        if raw_variant == sample_variant:
            overrides[raw_path] = float(raw_gain)
    return overrides


def _parse_opcodes(text: str) -> dict[str, str]:
    """Parse one SFZ fragment while preserving unquoted sample paths with spaces.

    VPO 3.3 contains valid, widely supported SFZ mappings such as
    ``sample=.../Solo Contrabass/...``.  The conservative project-wide parser
    intentionally treats whitespace as the end of every value, so the VPO
    string adapter needs this slightly more permissive reader.
    """

    matches = list(_OPCODE_START.finditer(text))
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        values[match.group(1).lower()] = value
    return values


def parse_vpo_sfz(path: str | Path) -> tuple[dict[str, str], ...]:
    """Return inherited SFZ region opcodes for a VPO mapping.

    This is deliberately local to VPO rather than broadening the core SFZ
    contract.  It is platform-neutral and handles both slash styles.
    """

    source_path = Path(path)
    text = source_path.read_text(encoding="utf-8-sig", errors="replace")
    global_values: dict[str, str] = {}
    group_values: dict[str, str] = {}
    current_kind = ""
    current_values: dict[str, str] | None = None
    regions: list[dict[str, str]] = []

    def finish_region() -> None:
        nonlocal current_values
        if current_kind == "region" and current_values is not None:
            regions.append(dict(current_values))
            current_values = None

    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        headers = list(_HEADER.finditer(line))
        pieces: list[tuple[str | None, str]] = []
        if not headers:
            pieces.append((None, line))
        else:
            if headers[0].start() > 0:
                pieces.append((None, line[: headers[0].start()]))
            for index, header in enumerate(headers):
                end = headers[index + 1].start() if index + 1 < len(headers) else len(line)
                pieces.append((header.group(1).lower(), line[header.end() : end]))

        for header_kind, opcode_text in pieces:
            if header_kind is not None:
                finish_region()
                current_kind = header_kind
                if header_kind == "global":
                    current_values = global_values
                elif header_kind in ("master", "group"):
                    group_values = dict(global_values)
                    current_values = group_values
                elif header_kind == "region":
                    current_values = dict(group_values or global_values)
                else:
                    current_values = {}
            if current_values is not None:
                current_values.update(_parse_opcodes(opcode_text))

    finish_region()
    return tuple(regions)


def _velocity_limits(values: dict[str, str]) -> tuple[float, float]:
    low = max(0.0, (float(values.get("lovel", 0.0)) - 0.5) / 127.0)
    high = min(1.0, (float(values.get("hivel", 127.0)) + 0.5) / 127.0)

    # SampleInstrument currently selects discrete layers.  Preserve VPO's two
    # cross-fade layers at their shared midpoint instead of treating them as
    # round-robin copies with identical velocity ranges.
    if "xfin_lovel" in values and "xfin_hivel" in values:
        midpoint = (float(values["xfin_lovel"]) + float(values["xfin_hivel"])) / 2.0
        low = max(low, midpoint / 127.0)
    if "xfout_lovel" in values and "xfout_hivel" in values:
        midpoint = (float(values["xfout_lovel"]) + float(values["xfout_hivel"])) / 2.0
        high = min(high, midpoint / 127.0)
    return low, high


def vpo_regions_to_manifest(
    sfz_path: str | Path,
    *,
    use_embedded_loops: bool,
    trigger: str | None = "attack",
    component: str | None = None,
) -> list[dict[str, Any]]:
    """Convert VPO SFZ regions without losing paths containing spaces.

    ``component`` is used only for VPO's layered accent mappings.  Their
    attack component carries fixed round-robin opcodes; the sustained
    component does not.  Splitting the two lets Tianlai render both layers,
    including VPO's per-note sustain delays, instead of choosing just one.
    """

    sfz_path = Path(sfz_path).resolve()
    converted: list[dict[str, Any]] = []
    for index, values in enumerate(parse_vpo_sfz(sfz_path)):
        region_trigger = values.get("trigger", "attack").lower()
        if trigger is not None and region_trigger != trigger.lower():
            continue
        sample_name = values.get("sample")
        if not sample_name:
            continue
        # Some VPO all-string bass mappings mark the first RR group only with
        # seq_length=2 (position 1 is implicit), while later groups spell out
        # seq_position.  Either opcode identifies the transient layer.
        is_round_robin_attack = (
            "seq_position" in values or "seq_length" in values
        )
        if component == "accent_attack" and not is_round_robin_attack:
            continue
        if component == "accent_sustain" and is_round_robin_attack:
            continue
        if component not in (None, "accent_attack", "accent_sustain"):
            raise ValueError(f"unsupported VPO SFZ component: {component!r}")

        sample_path = (sfz_path.parent / sample_name.replace("\\", "/")).resolve()
        root_value = values.get("pitch_keycenter", values.get("key"))
        if root_value is None:
            # Three VPO all-strings regions omit pitch_keycenter even though
            # their symmetric B3-C#4 key zone unambiguously identifies C4.
            # Use the zone midpoint instead of dropping a real sample or
            # silently substituting a GM patch.
            low_value = values.get("lokey")
            high_value = values.get("hikey")
            if low_value is None or high_value is None:
                raise ValueError(
                    f"SFZ region {index} has no pitch_keycenter or key zone: {sfz_path}"
                )
            key_min = note_number(low_value)
            key_max = note_number(high_value)
            root_midi = (key_min + key_max) / 2.0
        else:
            root_midi = note_number(root_value)
            key_min = note_number(values.get("lokey", values.get("key", root_value)))
            key_max = note_number(values.get("hikey", values.get("key", root_value)))
        velocity_min, velocity_max = _velocity_limits(values)
        tune_correction = float(values.get("tune", 0.0))
        transpose = float(values.get("transpose", 0.0))
        item: dict[str, Any] = {
            "sample": str(sample_path),
            "root_midi": root_midi,
            # SFZ transpose/tune alter playback rather than the source file.
            # Folding them into the represented source root preserves the
            # same concert pitch in SampleInstrument.
            "measured_tuning_cents": -tune_correction - 100.0 * transpose,
            "key_min": key_min,
            "key_max": key_max,
            "velocity_min": velocity_min,
            "velocity_max": velocity_max,
            "gain_db": float(values.get("volume", 0.0)),
            "pan": max(-1.0, min(1.0, float(values.get("pan", 0.0)) / 100.0)),
            "delay_seconds": float(values.get("delay", 0.0)),
            "attack_seconds": float(values.get("ampeg_attack", 0.0)),
            "release_seconds": float(values.get("ampeg_release", 0.25)),
            "offset_frames": int(float(values.get("offset", 0.0))),
            "_vpo_xfin_lokey": values.get("xfin_lokey"),
            "_vpo_xfin_hikey": values.get("xfin_hikey"),
            "_vpo_xfout_lokey": values.get("xfout_lokey"),
            "_vpo_xfout_hikey": values.get("xfout_hikey"),
            "_vpo_xfin_lovel": values.get("xfin_lovel"),
            "_vpo_xfin_hivel": values.get("xfin_hivel"),
            "_vpo_xfout_lovel": values.get("xfout_lovel"),
            "_vpo_xfout_hivel": values.get("xfout_hivel"),
            "_vpo_seq_position": values.get("seq_position"),
            "_vpo_seq_length": values.get("seq_length"),
            "_vpo_ampeg_hold": values.get("ampeg_hold"),
            "_vpo_ampeg_decay": values.get("ampeg_decay"),
            "_vpo_ampeg_sustain": values.get("ampeg_sustain"),
            "_vpo_ampeg_vel2attack": values.get("ampeg_vel2attack"),
            "_vpo_ampeg_attackcc1": values.get("ampeg_attackcc1"),
            "_vpo_loop_mode": values.get("loop_mode"),
        }
        if use_embedded_loops:
            item["use_embedded_loop"] = True
        converted.append(item)
    if not converted:
        raise ValueError(f"VPO SFZ contains no playable regions: {sfz_path}")
    return converted


def _apply_gated_release_seconds(
    regions: list[dict[str, Any]],
    release_seconds: float,
) -> None:
    """Make the effective manifest release win for note-off-gated regions.

    VPO's SFZ files carry long authoring releases (often 1.6--1.9 s).  The
    sampler correctly gives region values precedence in the general case, but
    these dedicated string adapters expose ``release_seconds`` as a reviewed
    per-performance control.  Copying the effective value into gated regions
    is what makes both the instrument manifest and roster override audible.
    One-shot attacks/pizzicato/staccato keep their authored envelopes.
    """

    if not math.isfinite(release_seconds) or release_seconds < 0.0:
        raise ValueError("release_seconds must be a finite non-negative number")
    for region in regions:
        region["release_seconds"] = release_seconds


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
    engine_name: str | None = None
    engine_note_id: int | None = None


@dataclass(slots=True)
class _ScheduledRelease:
    engine_name: str
    note_id: int
    remaining_samples: int
    release_seconds: float


class VpoSoloStringInstrument(Instrument):
    """Deterministic VPO solo-string candidate shared by viola and bass."""

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        self.instrument_name = str(manifest["instrument_name"])
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        self.sampled_range = str(manifest["sampled_range"])
        self.sfz_prefix = str(manifest["sfz_prefix"])
        # SOLO 是单人独奏采样(NoBudgetOrch),SEC 是声部齐奏采样(SSO);后者高音
        # 区直接录到 E6 以上、多人齐奏更厚,适合合奏中的声部行。默认 SOLO 以保持
        # 中提琴/大提琴等既有独奏乐器不变。
        variant = str(manifest.get("sample_variant", "SOLO")).upper()
        if variant not in ("SOLO", "SEC"):
            raise ValueError(f"sample_variant must be SOLO or SEC, got {variant!r}")

        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        sfz_root = asset_root / "Strings"
        if not sfz_root.is_dir():
            raise ValueError(
                f"{self.instrument_name} SFZ 不存在：{sfz_root}。"
                "请按来源.md 安装 Virtual Playing Orchestra。"
            )

        calibration_path = Path(base_directory) / str(
            manifest.get("pitch_calibration", "音准校准.json")
        )
        calibration: dict[str, Any] = {}
        if calibration_path.is_file():
            calibration_document = json.loads(calibration_path.read_text(encoding="utf-8"))
            calibration = calibration_document.get("samples", {})
            if not isinstance(calibration, dict):
                raise ValueError("string pitch calibration samples must be an object")

        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")
        default_gain = float(manifest.get("gain", 0.5))
        release_seconds = float(manifest.get("release_seconds", 0.7))
        shared_cache: dict[Path, Any] = {}

        sustain_path = sfz_root / f"{self.sfz_prefix}-{variant}-sustain.sfz"
        staccato_path = sfz_root / f"{self.sfz_prefix}-{variant}-staccato.sfz"
        pizzicato_path = sfz_root / f"{self.sfz_prefix}-{variant}-pizzicato.sfz"
        accent_path = sfz_root / f"{self.sfz_prefix}-{variant}-accent.sfz"
        for path in (sustain_path, staccato_path, pizzicato_path, accent_path):
            if not path.is_file():
                raise ValueError(f"{self.instrument_name} 奏法映射不存在：{path}")

        sustain_regions = vpo_regions_to_manifest(
            sustain_path, use_embedded_loops=True
        )
        self._apply_calibration(sustain_regions, asset_root, calibration)
        fast_regions = [dict(region) for region in sustain_regions]
        fast_attack = float(manifest.get("fast_attack_seconds", 0.04))
        for region in fast_regions:
            region["attack_seconds"] = fast_attack

        region_sets = {
            "sustain": fast_regions,
            "slow_sustain": sustain_regions,
            "staccato": vpo_regions_to_manifest(
                staccato_path, use_embedded_loops=False
            ),
            "pizzicato": vpo_regions_to_manifest(
                pizzicato_path, use_embedded_loops=False
            ),
            "accent_attack": vpo_regions_to_manifest(
                accent_path,
                use_embedded_loops=False,
                component="accent_attack",
            ),
            "accent_sustain": vpo_regions_to_manifest(
                accent_path,
                use_embedded_loops=True,
                component="accent_sustain",
            ),
        }
        self._apply_calibration(region_sets["accent_sustain"], asset_root, calibration)
        self._apply_sample_gain_db_overrides(
            region_sets,
            asset_root,
            _read_sample_gain_db_overrides(manifest, variant),
        )

        self.engines: dict[str, SampleInstrument] = {}
        for name, regions in region_sets.items():
            public_name = "accent" if name.startswith("accent_") else name
            if name not in ("staccato", "pizzicato", "accent_attack"):
                _apply_gated_release_seconds(regions, release_seconds)
            self.engines[name] = SampleInstrument.from_manifest(
                {
                    "regions": regions,
                    "reference_a4_hz": 440.0,
                    "gain": default_gain * float(articulation_gain.get(public_name, 1.0)),
                    "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
                    "release_seconds": release_seconds,
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )

        default_articulation = str(manifest.get("default_articulation", "sustain"))
        if default_articulation not in _PUBLIC_ARTICULATIONS:
            raise ValueError(
                f"unsupported default {self.instrument_name} articulation: "
                f"{default_articulation!r}"
            )
        self.articulation = default_articulation
        self.note_routes: dict[int, _NoteRoute] = {}
        self._auxiliary_note_id = int(manifest.get("auxiliary_note_id_base", 1_300_000_000))
        self._scheduled_releases: list[_ScheduledRelease] = []
        self._short_gate_samples = max(
            1, round(float(manifest.get("short_gate_seconds", 0.18)) * sample_rate)
        )
        self._short_release_seconds = max(
            0.001, float(manifest.get("short_release_seconds", 0.18))
        )
        self.expression = 1.0
        self.expression_target = 1.0
        smoothing_seconds = max(
            0.001, float(manifest.get("expression_smoothing_seconds", 0.014))
        )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (smoothing_seconds * sample_rate)
        )

    @staticmethod
    def _apply_calibration(
        regions: list[dict[str, Any]],
        asset_root: Path,
        calibration: dict[str, Any],
    ) -> None:
        for region in regions:
            relative = Path(region["sample"]).relative_to(asset_root).as_posix()
            measured = calibration.get(relative)
            if isinstance(measured, dict) and "detune_cents" in measured:
                region["measured_tuning_cents"] = float(measured["detune_cents"])

    @staticmethod
    def _apply_sample_gain_db_overrides(
        region_sets: dict[str, list[dict[str, Any]]],
        asset_root: Path,
        overrides: dict[str, float],
    ) -> None:
        if not overrides:
            return

        matched: set[str] = set()
        for regions in region_sets.values():
            for region in regions:
                relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                correction = overrides.get(relative)
                if correction is None:
                    continue
                region["gain_db"] = float(region.get("gain_db", 0.0)) + correction
                matched.add(relative)

        missing = sorted(set(overrides) - matched)
        if missing:
            raise ValueError(
                "sample_gain_db_overrides did not match loaded VPO regions: "
                + ", ".join(missing)
            )

    def _next_auxiliary_id(self) -> int:
        self._auxiliary_note_id += 1
        return self._auxiliary_note_id

    def _check_range(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if "midi_note" in event.payload:
            note = float(event.payload["midi_note"])
        else:
            note = 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)
        if not self.note_min <= note <= self.note_max:
            raise ValueError(
                f"{self.instrument_name} note {note:.3f} is outside the sampled "
                f"{self.sampled_range} range"
            )

    def _trigger_one_shot(
        self,
        engine_name: str,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        schedule_short_release: bool,
    ) -> int:
        note_id = self._next_auxiliary_id()
        self.engines[engine_name].handle_event(_with_note_id(event, note_id), tuning)
        if schedule_short_release:
            self._scheduled_releases.append(
                _ScheduledRelease(
                    engine_name,
                    note_id,
                    self._short_gate_samples,
                    self._short_release_seconds,
                )
            )
        return note_id

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
            if name == "expression":
                self.expression_target = float(event.payload["value"]) ** 1.35
            elif name == "sustain_pedal":
                for engine_name in ("sustain", "slow_sustain", "accent_sustain"):
                    self.engines[engine_name].handle_event(event, tuning)
            return

        if event.type == "note_on":
            self._check_range(event, tuning)
            public_note_id = int(event.payload["note_id"])
            name = self.articulation
            if name in _ONE_SHOTS:
                self._trigger_one_shot(
                    name,
                    event,
                    tuning,
                    schedule_short_release=name == "staccato",
                )
                self.note_routes[public_note_id] = _NoteRoute(name)
                return
            if name == "accent":
                self._trigger_one_shot(
                    "accent_attack",
                    event,
                    tuning,
                    schedule_short_release=True,
                )
                sustained_id = self._next_auxiliary_id()
                self.engines["accent_sustain"].handle_event(
                    _with_note_id(event, sustained_id), tuning
                )
                self.note_routes[public_note_id] = _NoteRoute(
                    name, "accent_sustain", sustained_id
                )
                return
            self.engines[name].handle_event(event, tuning)
            self.note_routes[public_note_id] = _NoteRoute(name, name, public_note_id)
            return

        if event.type == "note_off":
            public_note_id = int(event.payload["note_id"])
            route = self.note_routes.pop(public_note_id, None)
            if route is None or route.engine_name is None or route.engine_note_id is None:
                return
            self.engines[route.engine_name].handle_event(
                _with_note_id(event, route.engine_note_id), tuning
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
        left = 0.0
        right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


def create_vpo_solo_string(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoSoloStringInstrument(sample_rate, manifest, base_directory)


_SECTION_ARTICULATIONS = frozenset(
    ("sustain", "staccato", "pizzicato", "tremolo", "accent")
)
_HARP_ARTICULATIONS = frozenset(("open", "sustain", "dampened"))


def _event_midi(event: PerformanceEvent, tuning: EqualTemperament) -> float:
    if "midi_note" in event.payload:
        return float(event.payload["midi_note"])
    return 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / 440.0)


def _optional_sfz_note(value: Any) -> float | None:
    if value is None:
        return None
    return note_number(value)


@dataclass(frozen=True, slots=True)
class _StringKeyCrossfade:
    fade_in_low: float | None
    fade_in_high: float | None
    fade_out_low: float | None
    fade_out_high: float | None

    def gain(self, note: float) -> float:
        """Return VPO's equal-power section hand-off gain for one real note."""

        gain = 1.0
        if self.fade_in_low is not None and self.fade_in_high is not None:
            if note <= self.fade_in_low:
                return 0.0
            if note < self.fade_in_high:
                width = max(1e-9, self.fade_in_high - self.fade_in_low)
                gain *= math.sqrt((note - self.fade_in_low) / width)
        if self.fade_out_low is not None and self.fade_out_high is not None:
            if note >= self.fade_out_high:
                return 0.0
            if note > self.fade_out_low:
                width = max(1e-9, self.fade_out_high - self.fade_out_low)
                gain *= math.sqrt((self.fade_out_high - note) / width)
        return gain


_STRING_SECTION_BY_XFADE = {
    (None, None, 36.0, 47.0): "bass",
    (36.0, 47.0, 48.0, 53.0): "cello",
    (48.0, 53.0, 55.0, 84.0): "viola",
    (55.0, 84.0, None, None): "violin",
}


def _section_and_crossfade(
    region: dict[str, Any],
) -> tuple[str, _StringKeyCrossfade]:
    settings = (
        _optional_sfz_note(region.get("_vpo_xfin_lokey")),
        _optional_sfz_note(region.get("_vpo_xfin_hikey")),
        _optional_sfz_note(region.get("_vpo_xfout_lokey")),
        _optional_sfz_note(region.get("_vpo_xfout_hikey")),
    )
    name = _STRING_SECTION_BY_XFADE.get(settings)
    if name is None:
        raise ValueError(f"unknown VPO all-strings section crossfade: {settings!r}")
    return name, _StringKeyCrossfade(*settings)


@dataclass(slots=True)
class _SectionSourceEngine:
    source_name: str
    note_min: float
    note_max: float
    engine: SampleInstrument


@dataclass(slots=True)
class _StringSection:
    name: str
    crossfade: _StringKeyCrossfade
    engines: dict[str, tuple[_SectionSourceEngine, ...]]


@dataclass(frozen=True, slots=True)
class _SectionEngineRoute:
    engine: SampleInstrument
    note_id: int


@dataclass(slots=True)
class _SectionScheduledRelease:
    engine: SampleInstrument
    note_id: int
    remaining_samples: int
    release_seconds: float


def _source_family(region: dict[str, Any], asset_root: Path) -> str:
    path = Path(region["sample"])
    relative = path.relative_to(asset_root)
    # A source folder is a simultaneous SFZ layer; sequence-position samples
    # within that folder remain in one engine and are selected as deterministic
    # round robin.  This distinction prevents two independently recorded
    # section layers from being mistaken for RR alternatives.
    return relative.parent.as_posix()


def _load_pitch_calibration(
    base_directory: str, manifest: dict[str, Any]
) -> dict[str, Any]:
    relative = manifest.get("pitch_calibration")
    if relative is None:
        return {}
    path = Path(base_directory) / str(relative)
    if not path.is_file():
        raise ValueError(f"VPO string pitch calibration does not exist: {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    samples = document.get("samples")
    if not isinstance(samples, dict):
        raise ValueError("VPO string pitch calibration samples must be an object")
    return samples


def _apply_path_calibration(
    regions: list[dict[str, Any]], asset_root: Path, calibration: dict[str, Any]
) -> None:
    for region in regions:
        relative = Path(region["sample"]).relative_to(asset_root).as_posix()
        measurement = calibration.get(relative)
        if isinstance(measurement, dict) and "detune_cents" in measurement:
            region["measured_tuning_cents"] = float(measurement["detune_cents"])


def _harp_sfz_paths(
    manifest: dict[str, Any],
    asset_root: Path,
) -> dict[str, Path]:
    source_sfz = manifest.get("source_sfz")
    if source_sfz is None:
        string_root = asset_root / "Strings"
        return {
            "open": string_root / "harp-sustain.sfz",
            "dampened": string_root / "harp-dampened.sfz",
        }
    relative = Path(str(source_sfz))
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ValueError(f"concert harp source_sfz must be asset-root-relative: {source_sfz!r}")
    source = (asset_root / relative).resolve()
    try:
        source.relative_to(asset_root)
    except ValueError as error:
        raise ValueError(
            f"concert harp source_sfz escapes asset root: {source_sfz!r}"
        ) from error
    return {"open": source, "dampened": source}


def _prepare_harp_regions(
    sfz_path: Path,
    *,
    articulation: str,
    asset_root: Path,
    manifest: dict[str, Any],
    calibration: dict[str, Any],
) -> list[dict[str, Any]]:
    regions = vpo_regions_to_manifest(sfz_path, use_embedded_loops=False)

    # SampleInstrument is intentionally a discrete-layer sampler.  Split the
    # shared midpoint by one floating-point step so the exact boundary cannot
    # be misinterpreted as a two-item round robin.
    lower_boundaries = {
        float(region["velocity_min"])
        for region in regions
        if float(region["velocity_min"]) > 0.0
    }
    for region in regions:
        upper = float(region["velocity_max"])
        if upper < 1.0 and upper in lower_boundaries:
            region["velocity_max"] = math.nextafter(upper, 0.0)

    # VCSL Concert Harp has one deliberate-looking but sampler-ambiguous
    # bridge: D4_mf covers notes 61-63 through velocity 100 while the B3/F4
    # forte zones also cover edge notes 61/63 from the crossfade midpoint.
    # An SFZ engine would layer those overlaps; SampleInstrument would instead
    # rotate equal-score candidates as if they were RR.  Split the bridge into
    # one centre zone and two low-velocity edge zones so all integer note /
    # velocity pairs choose deterministically without inventing new audio.
    if manifest.get("source_sfz") is not None:
        bridge_relative = (
            "Chordophones/Composite Chordophones/Concert Harp/"
            "KSHarp_D4_mf1.wav"
        )
        bridge_regions = [
            region
            for region in regions
            if Path(region["sample"]).relative_to(asset_root).as_posix()
            == bridge_relative
        ]
        if len(bridge_regions) != 1:
            raise ValueError(
                "VCSL Concert Harp D4 bridge region is missing or duplicated"
            )
        bridge = bridge_regions[0]
        if (
            float(bridge["root_midi"]) != 62.0
            or float(bridge["key_min"]) != 61.0
            or float(bridge["key_max"]) != 63.0
        ):
            raise ValueError("VCSL Concert Harp D4 bridge shape changed")
        ordinary_layer_boundaries = sorted(
            boundary
            for boundary in lower_boundaries
            if boundary < float(bridge["velocity_max"])
        )
        if not ordinary_layer_boundaries:
            raise ValueError("VCSL Concert Harp velocity boundary is missing")
        edge_velocity_max = math.nextafter(
            ordinary_layer_boundaries[-1],
            0.0,
        )
        bridge["key_min"] = 62
        bridge["key_max"] = 62
        for edge_note in (61, 63):
            edge = dict(bridge)
            edge["key_min"] = edge_note
            edge["key_max"] = edge_note
            edge["velocity_max"] = edge_velocity_max
            regions.append(edge)

    raw_overrides = manifest.get("offset_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise ValueError("concert harp offset_overrides must be an object")
    overrides: dict[str, int] = {}
    for relative, raw_offset in raw_overrides.items():
        relative_path = Path(str(relative))
        if (
            relative_path.is_absolute()
            or any(part in ("", ".", "..") for part in relative_path.parts)
            or not isinstance(raw_offset, int)
            or isinstance(raw_offset, bool)
            or raw_offset < 0
        ):
            raise ValueError(
                f"invalid concert harp offset override: {relative!r}={raw_offset!r}"
            )
        overrides[relative_path.as_posix()] = raw_offset
    seen: set[str] = set()
    release_seconds = float(
        manifest.get(
            "open_release_seconds"
            if articulation == "open"
            else "dampened_release_seconds",
            30.0 if articulation == "open" else 0.35,
        )
    )
    if release_seconds < 0.0:
        raise ValueError("concert harp release seconds must be non-negative")
    for region in regions:
        path = Path(region["sample"]).resolve()
        try:
            relative = path.relative_to(asset_root).as_posix()
        except ValueError as error:
            raise ValueError(f"concert harp sample escapes asset root: {path}") from error
        if relative in overrides:
            region["offset_frames"] = overrides[relative]
            seen.add(relative)
        region["release_seconds"] = release_seconds
    missing = set(overrides) - seen
    if missing:
        raise ValueError(
            "concert harp offset overrides did not match SFZ regions: "
            + ", ".join(sorted(missing))
        )
    _apply_path_calibration(regions, asset_root, calibration)
    return regions


def harp_source_regions(
    manifest_path: str | Path,
) -> dict[str, list[dict[str, Any]]]:
    """Return the exact project-layer regions loaded by one harp manifest."""

    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    asset_root = (path.parent / str(manifest["asset_root"])).resolve()
    calibration = _load_pitch_calibration(str(path.parent), manifest)
    source_paths = _harp_sfz_paths(manifest, asset_root)
    return {
        name: _prepare_harp_regions(
            source,
            articulation=name,
            asset_root=asset_root,
            manifest=manifest,
            calibration=calibration,
        )
        for name, source in source_paths.items()
    }


class VpoStringSectionInstrument(Instrument):
    """VPO all-string section candidate with real articulation routing.

    The upstream all-strings mappings intentionally overlap bass, cello,
    viola and violin sections.  Each section is rendered independently with
    its SFZ key crossfade.  Independently recorded layers are summed, while
    sequence variants within one source family remain deterministic RR.
    """

    def __init__(
        self, sample_rate: int, manifest: dict[str, Any], base_directory: str
    ) -> None:
        super().__init__(sample_rate)
        self.instrument_name = str(manifest["instrument_name"])
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        self.sampled_range = str(manifest["sampled_range"])
        raw_allowed = manifest.get("allowed_articulations")
        if not isinstance(raw_allowed, list) or not raw_allowed:
            raise ValueError("allowed_articulations must be a non-empty array")
        self.allowed_articulations = frozenset(str(item) for item in raw_allowed)
        if not self.allowed_articulations <= _SECTION_ARTICULATIONS:
            unknown = sorted(self.allowed_articulations - _SECTION_ARTICULATIONS)
            raise ValueError(f"unsupported VPO string section articulations: {unknown}")
        default = str(manifest.get("default_articulation", "sustain"))
        if default not in self.allowed_articulations:
            raise ValueError("default_articulation must be one of allowed_articulations")
        self.articulation = default

        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        string_root = asset_root / "Strings"
        if not string_root.is_dir():
            raise ValueError(
                f"{self.instrument_name} VPO Strings directory does not exist: {string_root}"
            )
        paths = {
            name: string_root / f"all-strings-SEC-{name}.sfz"
            for name in self.allowed_articulations
        }
        for path in paths.values():
            if not path.is_file():
                raise ValueError(
                    f"{self.instrument_name} VPO articulation mapping is missing: {path}"
                )

        calibration = _load_pitch_calibration(base_directory, manifest)
        articulation_regions: dict[str, list[dict[str, Any]]] = {}
        for name, path in paths.items():
            if name == "accent":
                attack = vpo_regions_to_manifest(
                    path,
                    use_embedded_loops=False,
                    component="accent_attack",
                )
                sustained = vpo_regions_to_manifest(
                    path,
                    use_embedded_loops=True,
                    component="accent_sustain",
                )
                _apply_path_calibration(sustained, asset_root, calibration)
                articulation_regions["accent_attack"] = attack
                articulation_regions["accent_sustain"] = sustained
            else:
                regions = vpo_regions_to_manifest(
                    path,
                    use_embedded_loops=name in ("sustain", "tremolo"),
                )
                _apply_path_calibration(regions, asset_root, calibration)
                articulation_regions[name] = regions

        grouped: dict[
            str, dict[str, dict[str, list[dict[str, Any]]]]
        ] = {}
        crossfades: dict[str, _StringKeyCrossfade] = {}
        for articulation, regions in articulation_regions.items():
            for region in regions:
                section_name, crossfade = _section_and_crossfade(region)
                previous = crossfades.setdefault(section_name, crossfade)
                if previous != crossfade:
                    raise ValueError(
                        f"inconsistent VPO string crossfade for {section_name}"
                    )
                family = _source_family(region, asset_root)
                grouped.setdefault(section_name, {}).setdefault(
                    articulation, {}
                ).setdefault(family, []).append(region)

        expected_sections = set(_STRING_SECTION_BY_XFADE.values())
        if set(grouped) != expected_sections:
            raise ValueError(
                f"{self.instrument_name} must map all four string sections; "
                f"found {sorted(grouped)}"
            )
        gain = float(manifest.get("gain", 0.22))
        velocity_exponent = float(manifest.get("velocity_exponent", 0.72))
        release_seconds = float(manifest.get("release_seconds", 0.8))
        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")
        shared_cache: dict[Path, Any] = {}
        self.sections: list[_StringSection] = []
        for section_name in ("bass", "cello", "viola", "violin"):
            engine_sets: dict[str, tuple[_SectionSourceEngine, ...]] = {}
            for articulation, families in grouped[section_name].items():
                public_name = "accent" if articulation.startswith("accent_") else articulation
                source_engines: list[_SectionSourceEngine] = []
                for family in sorted(families):
                    regions = families[family]
                    if articulation not in (
                        "staccato",
                        "pizzicato",
                        "accent_attack",
                    ):
                        _apply_gated_release_seconds(
                            regions,
                            release_seconds,
                        )
                    engine = SampleInstrument.from_manifest(
                        {
                            "regions": regions,
                            "reference_a4_hz": 440.0,
                            "gain": gain * float(articulation_gain.get(public_name, 1.0)),
                            "velocity_exponent": velocity_exponent,
                            "release_seconds": release_seconds,
                        },
                        sample_rate,
                        base_directory=base_directory,
                        sample_cache=shared_cache,
                    )
                    source_engines.append(
                        _SectionSourceEngine(
                            family,
                            min(float(item["key_min"]) for item in regions),
                            max(float(item["key_max"]) for item in regions),
                            engine,
                        )
                    )
                engine_sets[articulation] = tuple(source_engines)
            self.sections.append(
                _StringSection(section_name, crossfades[section_name], engine_sets)
            )

        self.note_routes: dict[int, tuple[_SectionEngineRoute, ...]] = {}
        self._auxiliary_note_id = int(
            manifest.get("auxiliary_note_id_base", 1_600_000_000)
        )
        self._scheduled_releases: list[_SectionScheduledRelease] = []
        self._short_gate_samples = max(
            1, round(float(manifest.get("short_gate_seconds", 0.17)) * sample_rate)
        )
        self._short_release_seconds = max(
            0.001, float(manifest.get("short_release_seconds", 0.18))
        )
        self.expression = 1.0
        self.expression_target = 1.0
        smoothing = max(
            0.001, float(manifest.get("expression_smoothing_seconds", 0.014))
        )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (smoothing * sample_rate)
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
        engine_name: str,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        note: float,
        *,
        schedule_release: bool = False,
    ) -> tuple[_SectionEngineRoute, ...]:
        routes: list[_SectionEngineRoute] = []
        for section in self.sections:
            gain = section.crossfade.gain(note)
            if gain <= 1e-9:
                continue
            for source in section.engines.get(engine_name, ()):
                if not source.note_min <= note <= source.note_max:
                    continue
                note_id = self._next_auxiliary_id()
                source.engine.handle_event(_with_note_id(event, note_id), tuning)
                source.engine.voices[note_id].amplitude *= gain
                route = _SectionEngineRoute(source.engine, note_id)
                routes.append(route)
                if schedule_release:
                    self._scheduled_releases.append(
                        _SectionScheduledRelease(
                            engine=source.engine,
                            note_id=note_id,
                            remaining_samples=self._short_gate_samples,
                            release_seconds=self._short_release_seconds,
                        )
                    )
        return tuple(routes)

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in self.allowed_articulations:
                choices = ", ".join(sorted(self.allowed_articulations))
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
                self.expression_target = value**1.3
            elif name == "sustain_pedal":
                for section in self.sections:
                    for engine_name, sources in section.engines.items():
                        if engine_name in ("sustain", "tremolo", "accent_sustain"):
                            for source in sources:
                                source.engine.handle_event(event, tuning)
            return
        if event.type == "note_on":
            note = _event_midi(event, tuning)
            self._check_range(note)
            public_id = int(event.payload["note_id"])
            articulation = self.articulation
            if articulation == "accent":
                self._trigger(
                    "accent_attack", event, tuning, note, schedule_release=True
                )
                self.note_routes[public_id] = self._trigger(
                    "accent_sustain", event, tuning, note
                )
            elif articulation in ("staccato", "pizzicato"):
                self._trigger(
                    articulation,
                    event,
                    tuning,
                    note,
                    schedule_release=articulation == "staccato",
                )
                self.note_routes[public_id] = ()
            else:
                self.note_routes[public_id] = self._trigger(
                    articulation, event, tuning, note
                )
            return
        if event.type == "note_off":
            for route in self.note_routes.pop(int(event.payload["note_id"]), ()):
                route.engine.handle_event(_with_note_id(event, route.note_id), tuning)

    def render_frame(self) -> StereoFrame:
        pending: list[_SectionScheduledRelease] = []
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
        left = 0.0
        right = 0.0
        for section in self.sections:
            for sources in section.engines.values():
                for source in sources:
                    engine_left, engine_right = source.engine.render_frame()
                    left += engine_left
                    right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(
            source.engine.active_voice_count
            for section in self.sections
            for sources in section.engines.values()
            for source in sources
        )


class VpoHarpInstrument(Instrument):
    """Mapped concert-harp candidate with explicit ringing/dampened releases."""

    def __init__(
        self, sample_rate: int, manifest: dict[str, Any], base_directory: str
    ) -> None:
        super().__init__(sample_rate)
        self.instrument_name = str(manifest["instrument_name"])
        self.note_min = float(manifest["note_min"])
        self.note_max = float(manifest["note_max"])
        self.sampled_range = str(manifest["sampled_range"])
        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        paths = _harp_sfz_paths(manifest, asset_root)
        for path in paths.values():
            if not path.is_file():
                raise ValueError(f"concert harp mapping is missing: {path}")
        calibration = _load_pitch_calibration(base_directory, manifest)
        gain = float(manifest.get("gain", 0.55))
        velocity_exponent = float(manifest.get("velocity_exponent", 0.78))
        shared_cache: dict[Path, Any] = {}
        self.engines: dict[str, SampleInstrument] = {}
        for name, path in paths.items():
            regions = _prepare_harp_regions(
                path,
                articulation=name,
                asset_root=asset_root,
                manifest=manifest,
                calibration=calibration,
            )
            self.engines[name] = SampleInstrument.from_manifest(
                {
                    "regions": regions,
                    "reference_a4_hz": 440.0,
                    "gain": gain,
                    "velocity_exponent": velocity_exponent,
                    "release_seconds": float(
                        manifest.get(
                            "open_release_seconds" if name == "open" else "dampened_release_seconds",
                            6.0 if name == "open" else 1.2,
                        )
                    ),
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )
        default = str(manifest.get("default_articulation", "open"))
        if default not in _HARP_ARTICULATIONS:
            raise ValueError(f"unsupported concert harp default articulation: {default!r}")
        self.articulation = default
        self.note_routes: dict[int, tuple[str, int]] = {}
        self.expression = 1.0
        self.expression_target = 1.0
        smoothing = max(
            0.001, float(manifest.get("expression_smoothing_seconds", 0.012))
        )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (smoothing * sample_rate)
        )

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in _HARP_ARTICULATIONS:
                choices = ", ".join(sorted(_HARP_ARTICULATIONS))
                raise ValueError(
                    f"unsupported concert harp articulation {name!r}; choose from {choices}"
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
            elif name == "sustain_pedal":
                # This is a score-level damping abstraction: pedal down lets
                # released strings ring, pedal up damps pending releases.  It
                # is deliberately not the concert harp's seven pitch pedals.
                for engine in self.engines.values():
                    engine.handle_event(event, tuning)
            return
        if event.type == "note_on":
            note = _event_midi(event, tuning)
            if not self.note_min <= note <= self.note_max:
                raise ValueError(
                    f"concert harp note {note:.3f} is outside the sampled "
                    f"{self.sampled_range} concert-pitch range"
                )
            public_id = int(event.payload["note_id"])
            engine_name = "dampened" if self.articulation == "dampened" else "open"
            self.engines[engine_name].handle_event(event, tuning)
            self.note_routes[public_id] = (engine_name, public_id)
            return
        if event.type == "note_off":
            route = self.note_routes.pop(int(event.payload["note_id"]), None)
            if route is not None:
                engine_name, note_id = route
                self.engines[engine_name].handle_event(
                    _with_note_id(event, note_id), tuning
                )

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        left = 0.0
        right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(engine.active_voice_count for engine in self.engines.values())


def create_vpo_string_section(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoStringSectionInstrument(sample_rate, manifest, base_directory)


def create_vpo_harp(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoHarpInstrument(sample_rate, manifest, base_directory)


def _candidate_source_paths(
    manifest_path: str | Path,
) -> tuple[dict[str, Any], Path, Path, tuple[Path, ...], Path]:
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset_root = (manifest_path.parent / str(manifest["asset_root"])).resolve()
    instrument_type = str(manifest["type"])
    if instrument_type == "vpo_harp":
        paths = _harp_sfz_paths(manifest, asset_root)
        sfz_paths = tuple(dict.fromkeys(paths.values()))
        calibration_path = paths["open"]
    elif instrument_type == "vpo_string_section":
        string_root = asset_root / "Strings"
        allowed = tuple(str(item) for item in manifest["allowed_articulations"])
        sfz_paths = tuple(
            string_root / f"all-strings-SEC-{name}.sfz" for name in allowed
        )
        calibration_name = str(manifest["calibration_articulation"])
        calibration_path = string_root / f"all-strings-SEC-{calibration_name}.sfz"
    elif instrument_type == "vpo_solo_string":
        string_root = asset_root / "Strings"
        # 独奏(SOLO)与声部齐奏(SEC)两个变体共用一套 {prefix}-{variant}-*.sfz
        # 命名;冻结时按乐器实际加载的四个奏法 SFZ 取证。
        prefix = str(manifest["sfz_prefix"])
        variant = str(manifest.get("sample_variant", "SOLO")).upper()
        sfz_paths = tuple(
            string_root / f"{prefix}-{variant}-{name}.sfz"
            for name in ("sustain", "staccato", "pizzicato", "accent")
        )
        calibration_path = sfz_paths[0]
    else:
        raise ValueError(f"unsupported VPO string candidate type: {instrument_type}")
    return manifest, manifest_path, asset_root, sfz_paths, calibration_path


def generate_string_pitch_calibration(
    manifest_path: str | Path, output_path: str | Path
) -> dict[str, Any]:
    """Measure distinct roots used by a section entry's default real mapping."""

    from .analysis import analyze_file_harmonic_pitch

    manifest, _, asset_root, _, sfz_path = _candidate_source_paths(manifest_path)
    regions = vpo_regions_to_manifest(sfz_path, use_embedded_loops=False)
    is_harp = str(manifest["type"]) == "vpo_harp"
    samples: dict[str, dict[str, float]] = {}
    for region in regions:
        path = Path(region["sample"])
        relative = path.relative_to(asset_root).as_posix()
        if relative in samples:
            continue
        root_midi = float(region["root_midi"])
        expected_hz = 440.0 * (2.0 ** ((root_midi - 69.0) / 12.0))
        # Sustains/tremolos need a stable interior window; plucks use an
        # earlier window so their decaying fundamental remains measurable.
        articulation = str(manifest.get("calibration_articulation", "sustain"))
        start_seconds = 0.04 if is_harp or articulation == "pizzicato" else 0.24
        measurement = analyze_file_harmonic_pitch(
            path,
            expected_hz,
            start_seconds=start_seconds,
            maximum_frames=131_072,
            search_cents=180.0,
            harmonic_count=10,
        )
        item = {
            "root_midi": root_midi,
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(measurement.detune_cents, 6),
        }
        if is_harp:
            sfz_tune = -float(region["measured_tuning_cents"])
            item.update(
                {
                    "sfz_tune_cents": sfz_tune,
                    "upstream_mapped_residual_cents": round(
                        measurement.detune_cents + sfz_tune,
                        6,
                    ),
                    "playback_correction_cents": round(
                        -measurement.detune_cents,
                        6,
                    ),
                }
            )
        samples[relative] = item
    detunes = [item["detune_cents"] for item in samples.values()]
    summary = {
        "sample_count": len(samples),
        "median_detune_cents": round(statistics.median(detunes), 6),
        "maximum_absolute_detune_cents": round(max(map(abs, detunes)), 6),
    }
    if is_harp:
        upstream_residuals = [
            item["upstream_mapped_residual_cents"] for item in samples.values()
        ]
        summary.update(
            {
                "unique_root_count": len(
                    {float(item["root_midi"]) for item in samples.values()}
                ),
                "upstream_mapping_median_residual_cents": round(
                    statistics.median(upstream_residuals),
                    6,
                ),
                "upstream_mapping_maximum_absolute_residual_cents": round(
                    max(map(abs, upstream_residuals)),
                    6,
                ),
                "project_calibration_target_residual_cents": 0.0,
            }
        )
    document = {
        "description": (
            f"FFT measurement of raw {manifest['instrument_name']} samples; A4=440 Hz"
        ),
        "source_sfz": sfz_path.relative_to(asset_root).as_posix(),
        "method": (
            "harmonic FFT of each raw pluck from 0.04 s; runtime represents "
            "each source root at its measured frequency"
            if is_harp
            else "harmonic FFT of each raw source; runtime uses measured roots"
        ),
        "summary": summary,
        "samples": samples,
    }
    Path(output_path).write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_string_resource_audit(
    manifest_path: str | Path,
    output_path: str | Path,
    *,
    license_files: tuple[str, ...],
) -> dict[str, Any]:
    """Freeze exact string-candidate mappings, samples, licences and versions."""

    manifest, _, asset_root, sfz_paths, _ = _candidate_source_paths(manifest_path)
    samples: dict[str, Path] = {}
    for sfz_path in sfz_paths:
        looped = sfz_path.stem.endswith(("sustain", "tremolo"))
        for region in vpo_regions_to_manifest(sfz_path, use_embedded_loops=looped):
            path = Path(region["sample"])
            samples[path.relative_to(asset_root).as_posix()] = path
    aggregate_lines: list[str] = []
    sample_hashes: dict[str, str] = {}
    total_bytes = 0
    for relative in sorted(samples):
        path = samples[relative]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sample_hashes[relative] = digest
        aggregate_lines.append(f"{digest}  {relative}\n")
        total_bytes += path.stat().st_size
    sample_set_sha256 = hashlib.sha256(
        "".join(aggregate_lines).encode("utf-8")
    ).hexdigest()

    def hash_relative(relative: str) -> str:
        path = asset_root / relative
        if not path.is_file():
            raise ValueError(f"string resource evidence is missing: {path}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    source_sfz_sha256 = {
        path.relative_to(asset_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sfz_paths
    }
    is_vcsl_harp = (
        str(manifest["type"]) == "vpo_harp"
        and manifest.get("source_sfz") is not None
    )
    if is_vcsl_harp:
        evidence_sha256 = {
            relative: hash_relative(relative) for relative in license_files
        }
        actual_shape = {
            "sample_count": len(samples),
            "sample_bytes": total_bytes,
            "sample_set_sha256": sample_set_sha256,
            "source_sfz_sha256": source_sfz_sha256,
            "evidence_sha256": evidence_sha256,
        }
        expected_shape = {
            "sample_count": 45,
            "sample_bytes": 76_694_972,
            "sample_set_sha256": (
                "fde6a8543ff0dac04989deb96ead10ede341e6bdae50240a123cef5bb6c497d7"
            ),
            "source_sfz_sha256": {
                "Chordophones/Composite Chordophones/Concert Harp.sfz": (
                    "7d202064c7d264edfc14a0d2d0a56e47c7689c0c5eb485bec677bd7281d643e7"
                )
            },
            "evidence_sha256": {
                "README.md": (
                    "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b"
                )
            },
        }
        if actual_shape != expected_shape:
            raise ValueError(
                "VCSL Concert Harp does not match the frozen v1.2.2-RC "
                f"resource shape: {actual_shape}"
            )

        from .audio import wav_loop_points

        import soundfile as sf

        formats: dict[str, int] = {}
        durations: list[float] = []
        tail_rms_dbfs: list[float] = []
        sample_peaks: dict[Path, float] = {}
        clipped_samples = 0
        silent_samples = 0
        embedded_loops = 0
        harmful_relative = (
            "Chordophones/Composite Chordophones/Concert Harp/"
            "KSHarp_D4_f1.wav"
        )
        harmful_discarded_energy_percent: float | None = None
        harmful_attack_peak_ms: float | None = None
        for relative, path in sorted(samples.items()):
            info = sf.info(path)
            format_key = (
                f"{path.suffix.lower()}:{info.samplerate}Hz:"
                f"{info.channels}ch:{info.subtype}"
            )
            formats[format_key] = formats.get(format_key, 0) + 1
            durations.append(float(info.duration))
            embedded_loops += int(wav_loop_points(path) is not None)
            audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
            peak = float(abs(audio).max()) if audio.size else 0.0
            sample_peaks[path.resolve()] = peak
            clipped_samples += int(peak >= 1.0)
            silent_samples += int(peak <= 1e-6)
            tail_frames = min(len(audio), round(float(sample_rate) * 0.1))
            tail = audio[-tail_frames:]
            tail_rms = (
                math.sqrt(float((tail * tail).mean()))
                if tail_frames and tail.size
                else 0.0
            )
            tail_rms_dbfs.append(20.0 * math.log10(max(tail_rms, 1e-12)))
            if relative == harmful_relative:
                offset_frames = 3744
                onset_window = audio[: min(len(audio), round(sample_rate * 0.2))]
                total_energy = float((onset_window * onset_window).sum())
                discarded = onset_window[:offset_frames]
                discarded_energy = float((discarded * discarded).sum())
                harmful_discarded_energy_percent = (
                    100.0 * discarded_energy / total_energy
                    if total_energy > 0.0
                    else 0.0
                )
                frame_peaks = abs(onset_window).max(axis=1)
                harmful_attack_peak_ms = (
                    1000.0 * float(frame_peaks.argmax()) / float(sample_rate)
                )

        raw_regions = vpo_regions_to_manifest(
            sfz_paths[0], use_embedded_loops=False
        )
        open_regions = _prepare_harp_regions(
            sfz_paths[0],
            articulation="open",
            asset_root=asset_root,
            manifest=manifest,
            calibration={},
        )
        dampened_regions = _prepare_harp_regions(
            sfz_paths[0],
            articulation="dampened",
            asset_root=asset_root,
            manifest=manifest,
            calibration={},
        )
        roots = sorted({int(region["root_midi"]) for region in raw_regions})
        root_layer_counts = {
            root: sum(int(region["root_midi"]) == root for region in raw_regions)
            for root in roots
        }
        layer_count_distribution: dict[str, int] = {}
        for count in root_layer_counts.values():
            key = str(count)
            layer_count_distribution[key] = layer_count_distribution.get(key, 0) + 1
        coverage_min = min(int(region["key_min"]) for region in open_regions)
        coverage_max = max(int(region["key_max"]) for region in open_regions)
        maximum_stretch = max(
            max(
                abs(float(region["key_min"]) - float(region["root_midi"])),
                abs(float(region["key_max"]) - float(region["root_midi"])),
            )
            for region in open_regions
        )
        selection_failures: list[list[int]] = []
        for note in range(coverage_min, coverage_max + 1):
            for velocity_127 in range(128):
                velocity = velocity_127 / 127.0
                matches = [
                    region
                    for region in open_regions
                    if float(region["key_min"]) <= note <= float(region["key_max"])
                    and float(region["velocity_min"])
                    <= velocity
                    <= float(region["velocity_max"])
                ]
                if len(matches) != 1:
                    selection_failures.append([note, velocity_127, len(matches)])
        if selection_failures:
            raise ValueError(
                "VCSL Concert Harp discrete mapping has ambiguous or missing "
                f"integer selections: {selection_failures[:8]}"
            )

        raw_offsets = {
            Path(region["sample"]).relative_to(asset_root).as_posix(): int(
                region["offset_frames"]
            )
            for region in raw_regions
            if int(region["offset_frames"]) != 0
        }
        project_offsets = {
            Path(region["sample"]).relative_to(asset_root).as_posix(): int(
                region["offset_frames"]
            )
            for region in open_regions
        }
        overrides = {
            relative: {
                "upstream": raw_offsets.get(relative, 0),
                "project": project_offsets[relative],
            }
            for relative in manifest.get("offset_overrides", {})
        }
        preserved_nonzero_offsets = sum(
            project_offsets[relative] == raw_offset
            for relative, raw_offset in raw_offsets.items()
            if relative not in overrides
        )
        upstream_peak = max(
            sample_peaks[Path(region["sample"]).resolve()]
            * (10.0 ** (float(region["gain_db"]) / 20.0))
            for region in open_regions
        )
        project_gain = float(manifest.get("gain", 1.0))
        project_peak = upstream_peak * project_gain
        headroom_db = (
            -20.0 * math.log10(project_peak)
            if project_peak > 0.0
            else float("inf")
        )
        if headroom_db < 6.0:
            raise ValueError(
                "VCSL Concert Harp project gain leaves only "
                f"{headroom_db:.3f} dB headroom"
            )

        document = {
            "upstream": manifest["upstream"],
            "origin": manifest["origin"],
            "upstream_version": manifest["upstream_version"],
            "upstream_commit": manifest.get("upstream_commit"),
            "license": manifest["license"],
            "profile": "vcsl_concert_harp_strict_cc0",
            "source_sfz_sha256": source_sfz_sha256,
            "evidence_sha256": evidence_sha256,
            "sample_count": len(samples),
            "sample_bytes": total_bytes,
            "sample_sha256": sample_hashes,
            "sample_set_sha256": sample_set_sha256,
            "sample_set_algorithm": (
                "Sort unique VCSL-relative UTF-8 paths; for each write "
                "'<lowercase file sha256>  <path>\\n'; SHA-256 the "
                "concatenated UTF-8 bytes."
            ),
            "sample_formats": formats,
            "mapping": {
                "source_region_count": len(raw_regions),
                "project_region_count": len(open_regions),
                "derived_bridge_region_count": len(open_regions) - len(raw_regions),
                "unique_root_count": len(roots),
                "root_midi_notes": roots,
                "recordings_per_root": {
                    str(root): count for root, count in root_layer_counts.items()
                },
                "recording_count_distribution": layer_count_distribution,
                "maximum_recorded_velocity_layers_per_root": max(
                    root_layer_counts.values()
                ),
                "single_recording_root_midi_notes": [
                    root for root, count in root_layer_counts.items() if count == 1
                ],
                "velocity_strategy": (
                    "SFZ crossfades are represented by one deterministic "
                    "midpoint layer switch; no continuous crossfade"
                ),
                "integer_note_velocity_selections_checked": (
                    (coverage_max - coverage_min + 1) * 128
                ),
                "ambiguous_or_missing_integer_selections": len(selection_failures),
                "round_robin_count": 0,
                "coverage_midi": [coverage_min, coverage_max],
                "maximum_stretch_semitones": maximum_stretch,
                "embedded_loop_count": embedded_loops,
                "stereo_sample_count": sum(
                    count
                    for format_key, count in formats.items()
                    if ":2ch:" in format_key
                ),
                "open_region_count": len(open_regions),
                "dampened_region_count": len(dampened_regions),
                "dampened_source": (
                    "same recordings; project 350 ms release envelope"
                ),
            },
            "project_overrides": {
                "upstream_sfz_unchanged": True,
                "upstream_nonzero_offset_count": len(raw_offsets),
                "preserved_nonzero_offset_count": preserved_nonzero_offsets,
                "offset_frames": overrides,
                "harmful_offset_diagnostic": {
                    "sample": harmful_relative,
                    "upstream_offset_frames": 3744,
                    "upstream_offset_ms_at_44100_hz": round(
                        1000.0 * 3744.0 / 44_100.0, 6
                    ),
                    "attack_peak_ms": round(harmful_attack_peak_ms or 0.0, 6),
                    "first_200ms_energy_discarded_percent": round(
                        harmful_discarded_energy_percent or 0.0, 6
                    ),
                },
            },
            "audio_integrity": {
                "source_clipped_samples": clipped_samples,
                "silent_samples": silent_samples,
                "duration_seconds": {
                    "minimum": round(min(durations), 6),
                    "median": round(statistics.median(durations), 6),
                    "maximum": round(max(durations), 6),
                },
                "final_100ms_rms_dbfs": {
                    "minimum": round(min(tail_rms_dbfs), 6),
                    "median": round(statistics.median(tail_rms_dbfs), 6),
                    "maximum": round(max(tail_rms_dbfs), 6),
                    "samples_above_minus_60_dbfs": sum(
                        value > -60.0 for value in tail_rms_dbfs
                    ),
                },
                "maximum_upstream_region_peak_dbfs": round(
                    20.0 * math.log10(upstream_peak),
                    6,
                ),
                "project_gain": project_gain,
                "maximum_project_peak_dbfs": round(
                    20.0 * math.log10(project_peak),
                    6,
                ),
                "minimum_headroom_db": round(headroom_db, 6),
            },
        }
    else:
        document = {
            "upstream": "Virtual Playing Orchestra",
            "sfz_version": "Standard Orchestra 3.3 (2026-06-27)",
            "wave_version": "Wave Files 3.2 (2026-06-27)",
            "source_sfz_sha256": source_sfz_sha256,
            "sample_count": len(samples),
            "sample_bytes": total_bytes,
            "sample_set_sha256": sample_set_sha256,
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
