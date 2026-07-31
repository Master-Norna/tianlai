"""Auditable adapter for dedicated SFZ sample libraries.

The adapter deliberately implements a bounded, tested SFZ subset instead of
pretending to be a complete sampler.  It supports inherited regions, safe
includes/macros, velocity layers and crossfades, round robin, ADSR, loops,
release triggers, choke groups, articulation routing and three pitch modes.
Unsupported keyswitch mappings fail at load time; callers should map their
standalone articulation SFZ files explicitly.  No General MIDI fallback exists.

``preprocess_sfz`` is the audit entry point: it returns the exact flattened SFZ
text used by the parser after confined include and macro expansion.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any
import zlib

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .runtime_variants import (
    RuntimeVariantError,
    current_runtime_variant_capture,
    stable_variant_sha256,
)
from .sampler import SampleInstrument
from .sfz import note_number
from .tuning import EqualTemperament


_HEADER = re.compile(r"<([A-Za-z_][A-Za-z0-9_]*)>")
_OPCODE_START = re.compile(r"(?<!\S)([A-Za-z_][A-Za-z0-9_]*)=")
_MACRO = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_DEFINE = re.compile(r"^#define\s+(\$[A-Za-z_][A-Za-z0-9_]*)\s+(.+?)\s*$")
_INCLUDE = re.compile(r'#include\s+(?:"([^"]+)"|<([^>]+)>)')
_TRIGGERS = frozenset(("attack", "release"))
_LOOP_MODES = frozenset(("no_loop", "one_shot", "loop_continuous", "loop_sustain"))
_PITCH_MODES = frozenset(("pitched", "fixed", "ignore"))


@dataclass(frozen=True, slots=True)
class DedicatedSfzRegion:
    """One fully inherited SFZ region after safe preprocessing."""

    values: dict[str, str]
    index: int


@dataclass(frozen=True, slots=True)
class DedicatedSfzDocument:
    """Preprocessed control settings, regions and their audited source files."""

    control: dict[str, str]
    regions: tuple[DedicatedSfzRegion, ...]
    source_files: tuple[Path, ...]


@dataclass(slots=True)
class _PreprocessState:
    root: Path
    entry_directory: Path
    macros: dict[str, str]
    source_files: list[Path]
    source_file_set: set[Path]
    total_characters: int = 0


@dataclass(frozen=True, slots=True)
class DedicatedSfzRegionMetadata:
    """Runtime-relevant choke metadata retained from one converted region."""

    group: str | None
    off_by: str | None
    off_time: float | None
    velocity_fade_in: tuple[float, float] | None
    velocity_fade_out: tuple[float, float] | None
    rt_decay_db_per_second: float | None

    def velocity_gain(self, velocity: float) -> float:
        midi_velocity = min(127.0, max(0.0, velocity * 127.0))
        gain = 1.0
        if self.velocity_fade_in is not None:
            low, high = self.velocity_fade_in
            if high == low:
                gain *= 1.0 if midi_velocity >= high else 0.0
            else:
                gain *= min(1.0, max(0.0, (midi_velocity - low) / (high - low)))
        if self.velocity_fade_out is not None:
            low, high = self.velocity_fade_out
            if high == low:
                gain *= 1.0 if midi_velocity <= low else 0.0
            else:
                gain *= min(1.0, max(0.0, (high - midi_velocity) / (high - low)))
        return gain


@dataclass(slots=True)
class _EngineLayer:
    engine: SampleInstrument
    region_runtime: dict[str, DedicatedSfzRegionMetadata]


@dataclass(slots=True)
class _ArticulationRuntime:
    attack_layers: tuple[_EngineLayer, ...]
    release_layers: tuple[_EngineLayer, ...]
    release_override_seconds: float | None = None

    @property
    def attack(self) -> SampleInstrument:
        return self.attack_layers[0].engine

    @property
    def attacks(self) -> tuple[SampleInstrument, ...]:
        return tuple(layer.engine for layer in self.attack_layers)

    @property
    def release(self) -> SampleInstrument | None:
        return self.release_layers[0].engine if self.release_layers else None

    @property
    def releases(self) -> tuple[SampleInstrument, ...]:
        return tuple(layer.engine for layer in self.release_layers)


@dataclass(frozen=True, slots=True)
class _RoutedVoice:
    layer_index: int
    internal_note_id: int
    one_shot: bool


@dataclass(slots=True)
class _NoteRoute:
    articulation: str
    voices: tuple[_RoutedVoice, ...]
    playback_payload: dict[str, Any]
    velocity: float
    started_sample: int
    pending_release_velocity: float | None = None

    @property
    def internal_note_id(self) -> int:
        return self.voices[0].internal_note_id

    @property
    def one_shot(self) -> bool:
        return all(voice.one_shot for voice in self.voices)


def _strip_comment(line: str) -> str:
    """Remove // comments without treating slashes inside quotes as comments."""

    quote = ""
    index = 0
    while index + 1 < len(line):
        character = line[index]
        if character in ('"', "'"):
            if not quote:
                quote = character
            elif quote == character:
                quote = ""
        if not quote and line[index : index + 2] == "//":
            return line[:index]
        index += 1
    return line


def _expand_macros(text: str, macros: dict[str, str], *, context: Path) -> str:
    expanded = text
    for _ in range(32):
        names = _MACRO.findall(expanded)
        if not names:
            return expanded
        missing = sorted({name for name in names if name not in macros})
        if missing:
            raise ValueError(
                f"undefined SFZ macro {', '.join(missing)} while reading {context}"
            )
        replaced = _MACRO.sub(lambda match: macros[match.group(0)], expanded)
        if replaced == expanded:
            break
        expanded = replaced
    unresolved = sorted(set(_MACRO.findall(expanded)))
    if unresolved:
        raise ValueError(
            f"recursive SFZ macro expansion {', '.join(unresolved)} in {context}"
        )
    return expanded


def _safe_relative_path(root: Path, base: Path, raw: str, *, kind: str) -> Path:
    normalized = raw.strip().strip('"').replace("\\", "/")
    windows_path = PureWindowsPath(normalized)
    if not normalized or windows_path.is_absolute() or windows_path.drive:
        raise ValueError(f"{kind} path must be relative: {raw!r}")
    candidate = (base / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"{kind} path escapes asset_root: {raw!r}") from None
    return candidate


def _preprocess_file(
    path: Path,
    state: _PreprocessState,
    *,
    stack: tuple[Path, ...],
) -> list[str]:
    if path in stack:
        cycle = " -> ".join(item.name for item in (*stack, path))
        raise ValueError(f"cyclic SFZ include: {cycle}")
    if len(stack) >= 32:
        raise ValueError(f"SFZ include depth exceeds 32 files: {path}")
    if not path.is_file():
        raise ValueError(f"SFZ source file does not exist: {path}")
    if path not in state.source_file_set:
        if len(state.source_files) >= 2048:
            raise ValueError("SFZ include graph exceeds 2048 unique files")
        state.source_file_set.add(path)
        state.source_files.append(path)
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    state.total_characters += len(text)
    if state.total_characters > 16 * 1024 * 1024:
        raise ValueError("SFZ include graph exceeds 16 MiB of source text")

    output: list[str] = []
    next_stack = (*stack, path)
    for raw_line in text.splitlines():
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        define = _DEFINE.match(line)
        if define is not None:
            name, value = define.groups()
            state.macros[name] = _expand_macros(value, state.macros, context=path)
            continue
        includes = list(_INCLUDE.finditer(line))
        if includes:
            # SFZ include paths are relative to the entry SFZ.  This also
            # matches MTG Solo Sax, whose included Data/group_*.txt files in
            # turn include Data/*_rr*.txt rather than paths relative to their
            # own directory.  Includes may appear mid-line after a header
            # (``<master> #include "..."``) and may carry trailing opcodes
            # that refine whatever the included file defined last; emitting
            # the pieces in order preserves both layouts.
            cursor = 0
            for include in includes:
                before = line[cursor : include.start()].strip()
                if before:
                    output.append(_expand_macros(before, state.macros, context=path))
                include_name = _expand_macros(
                    include.group(1) or include.group(2), state.macros, context=path
                )
                include_path = _safe_relative_path(
                    state.root,
                    state.entry_directory,
                    include_name,
                    kind="SFZ include",
                )
                output.extend(_preprocess_file(include_path, state, stack=next_stack))
                cursor = include.end()
            remainder = line[cursor:].strip()
            if remainder:
                output.append(_expand_macros(remainder, state.macros, context=path))
            continue
        if line.startswith("#"):
            raise ValueError(f"unsupported SFZ preprocessor directive in {path}: {line}")
        output.append(_expand_macros(line, state.macros, context=path))
    return output


def _preprocess(
    path: str | Path,
    *,
    asset_root: str | Path,
) -> tuple[str, tuple[Path, ...]]:
    root = Path(asset_root).resolve()
    if not root.is_dir():
        raise ValueError(f"dedicated SFZ asset_root does not exist: {root}")
    source = Path(path).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        raise ValueError(f"SFZ entry file is outside asset_root: {source}") from None
    state = _PreprocessState(root, source.parent, {}, [], set())
    lines = _preprocess_file(source, state, stack=())
    return "\n".join(lines), tuple(state.source_files)


def preprocess_sfz(path: str | Path, *, asset_root: str | Path) -> str:
    """Safely flatten relative includes and expand simple ``$MACRO`` values.

    Includes are confined to ``asset_root``, resolved relative to the entry
    SFZ, capped by depth/size, and checked for cycles.  Missing or unsupported
    preprocessor input is an error; it is never silently discarded.
    """

    return _preprocess(path, asset_root=asset_root)[0]


def _parse_opcodes(text: str) -> dict[str, str]:
    matches = list(_OPCODE_START.finditer(text))
    values: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[match.group(1).lower()] = value
    return values


def parse_dedicated_sfz(
    path: str | Path,
    *,
    asset_root: str | Path,
) -> DedicatedSfzDocument:
    """Parse global/master/group/region inheritance after safe preprocessing."""

    text, source_files = _preprocess(path, asset_root=asset_root)
    control: dict[str, str] = {}
    global_values: dict[str, str] = {}
    master_values: dict[str, str] | None = None
    group_values: dict[str, str] | None = None
    current_kind = ""
    current_values: dict[str, str] | None = None
    regions: list[DedicatedSfzRegion] = []

    def finish_region() -> None:
        nonlocal current_values
        if current_kind == "region" and current_values is not None:
            regions.append(DedicatedSfzRegion(dict(current_values), len(regions)))
            current_values = None

    for line in text.splitlines():
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
                if header_kind == "control":
                    current_values = control
                elif header_kind == "global":
                    global_values = {}
                    master_values = None
                    group_values = None
                    current_values = global_values
                elif header_kind == "master":
                    master_values = dict(global_values)
                    group_values = None
                    current_values = master_values
                elif header_kind == "group":
                    group_values = dict(
                        master_values if master_values is not None else global_values
                    )
                    current_values = group_values
                elif header_kind == "region":
                    inherited = (
                        group_values
                        if group_values is not None
                        else master_values
                        if master_values is not None
                        else global_values
                    )
                    current_values = dict(inherited)
                else:
                    current_values = None
            if current_values is not None:
                current_values.update(_parse_opcodes(opcode_text))

    finish_region()
    if not regions:
        raise ValueError(f"SFZ contains no regions: {Path(path).resolve()}")
    return DedicatedSfzDocument(control, tuple(regions), source_files)


def _normalized_group(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return value
    if number == 0.0:
        return None
    return str(int(number)) if number.is_integer() else str(number)


def _velocity_fade(
    values: dict[str, str],
    low_name: str,
    high_name: str,
    *,
    region_index: int,
) -> tuple[float, float] | None:
    has_low = low_name in values
    has_high = high_name in values
    if has_low != has_high:
        raise ValueError(
            f"SFZ region {region_index} must define {low_name} and {high_name} together"
        )
    if not has_low:
        return None
    low = float(values[low_name])
    high = float(values[high_name])
    if not 0.0 <= low <= high <= 127.0:
        raise ValueError(
            f"SFZ region {region_index} has an invalid {low_name}/{high_name} range"
        )
    return low, high


_DEFAULT_PAN_CC = 64.0  # MIDI 规定 CC10(声像)的默认值为 64,即居中


def _effective_pan(
    values: dict[str, str], cc_values: dict[int, float]
) -> float:
    """解析 SFZ 的 CC10 声像惯用法,按 MIDI 默认 CC 值求实际声像。

    许多库把声像写成 ``pan=-100`` 加 ``pan_oncc10=200``:基准硬左,再由
    CC10 调制回中间。只读 ``pan`` 就会把整件乐器停在硬左——Karoryfer
    Meatbass 正是如此。这里按 CC10 的 MIDI 默认值 64 求出静态声像。
    ``pan_curvecc10=1`` 表示双极曲线(64 为中点),其余按线性 0..1 处理。
    """

    pan = float(values.get("pan", 0.0))
    modulation = re.compile(r"pan_oncc(\d+)\Z")
    for opcode, raw_depth in values.items():
        match = modulation.fullmatch(opcode)
        if match is None:
            continue
        controller = int(match.group(1))
        controller_value = cc_values.get(
            controller, _DEFAULT_PAN_CC if controller == 10 else 0.0
        )
        curve = str(values.get(f"pan_curvecc{controller}", "0")).strip()
        if curve == "1":
            factor = (controller_value - 64.0) / 63.0
        else:
            factor = controller_value / 127.0
        pan += float(raw_depth) * factor
    return pan


_STATIC_KEYSWITCH_OPCODES = frozenset(
    ("sw_lolast", "sw_hilast", "sw_last", "sw_default", "sw_lokey", "sw_hikey", "sw_label")
)
_SET_CC = re.compile(r"set_cc(\d+)\Z")
_CC_CONDITION = re.compile(r"(lo|hi)cc(\d+)\Z")


def _initial_cc_values(
    control: dict[str, str], *, source: Path
) -> dict[int, float]:
    """Return the SFZ ``<control> set_ccN`` startup controller state."""

    values: dict[int, float] = {}
    for opcode, raw_value in control.items():
        if not opcode.startswith("set_cc"):
            continue
        match = _SET_CC.fullmatch(opcode)
        if match is None:
            raise ValueError(
                f"unsupported SFZ controller initialization {opcode!r}: {source}"
            )
        controller = int(match.group(1))
        value = float(raw_value)
        if not 0.0 <= value <= 127.0:
            raise ValueError(
                f"SFZ {opcode} must be between 0 and 127: {source}"
            )
        values[controller] = value
    return values


def _cc_conditions_match(
    values: dict[str, str],
    cc_values: dict[int, float],
    *,
    region_index: int,
    source: Path,
) -> bool:
    """Resolve static ``loccN``/``hiccN`` region routing.

    This adapter does not expose arbitrary MIDI CC events yet, so an SFZ is
    pinned to its declared ``set_ccN`` startup state.  An unset controller has
    the SFZ startup value zero.  Resolving the conditions before layer
    partitioning is essential: otherwise mutually exclusive maps are rendered
    simultaneously.
    """

    bounds: dict[int, list[float]] = {}
    for opcode, raw_value in values.items():
        match = _CC_CONDITION.fullmatch(opcode)
        if match is not None:
            controller = int(match.group(2))
            low_high = bounds.setdefault(controller, [0.0, 127.0])
            low_high[0 if match.group(1) == "lo" else 1] = float(raw_value)
            continue
        # High-definition and vendor-specific controller conditions are also
        # layer selectors.  Silently ignoring one can recreate the many-voice
        # stacking bug, so fail before any samples are loaded.
        if "cc" in opcode and opcode.startswith(("lo", "hi")):
            raise ValueError(
                f"unsupported SFZ dynamic routing opcode {opcode!r} in "
                f"region {region_index}: {source}"
            )

    for controller, (low, high) in bounds.items():
        if not 0.0 <= low <= high <= 127.0:
            raise ValueError(
                f"SFZ region {region_index} has an invalid locc{controller}/"
                f"hicc{controller} range: {source}"
            )
        if not low <= cc_values.get(controller, 0.0) <= high:
            return False
    return True


def _effective_cc_modulated_value(
    values: dict[str, str],
    name: str,
    default: float,
    cc_values: dict[int, float],
) -> float:
    """Apply unipolar ``NAME_onccN`` modulation at the static CC state."""

    result = float(values.get(name, default))
    modulation = re.compile(rf"{re.escape(name)}_oncc(\d+)\Z")
    for opcode, raw_depth in values.items():
        match = modulation.fullmatch(opcode)
        if match is None:
            continue
        controller = int(match.group(1))
        result += float(raw_depth) * cc_values.get(controller, 0.0) / 127.0
    return result


def dedicated_regions_to_manifest(
    sfz_path: str | Path,
    *,
    asset_root: str | Path,
    trigger: str = "attack",
    use_embedded_loops: bool = True,
    stable_prefix: str = "sfz",
    root_midi_fallback: float | None = None,
    keyswitch_select: float | None = None,
) -> tuple[list[dict[str, Any]], dict[str, DedicatedSfzRegionMetadata]]:
    """Convert one SFZ trigger set to SampleInstrument-compatible regions.

    ``keyswitch_select`` resolves last-keyswitch layers statically: regions
    guarded by ``sw_lolast``/``sw_hilast``/``sw_last`` are kept only when the
    selected note (or the mapping's own ``sw_default``) falls inside their
    switch range, so one manifest articulation pins one keyswitch layer.
    """

    requested_trigger = trigger.lower()
    if requested_trigger not in _TRIGGERS:
        raise ValueError(f"unsupported dedicated SFZ trigger: {trigger!r}")
    root = Path(asset_root).resolve()
    source = Path(sfz_path).resolve()
    document = parse_dedicated_sfz(source, asset_root=root)
    cc_values = _initial_cc_values(document.control, source=source)
    default_path = document.control.get("default_path", "")
    if default_path:
        sample_base = _safe_relative_path(
            root, source.parent, default_path, kind="SFZ default_path"
        )
    else:
        sample_base = source.parent

    converted: list[dict[str, Any]] = []
    runtime: dict[str, DedicatedSfzRegionMetadata] = {}
    for region in document.regions:
        values = region.values
        if not _cc_conditions_match(
            values,
            cc_values,
            region_index=region.index,
            source=source,
        ):
            continue
        dynamic_keyswitches = sorted(
            key
            for key in values
            if key.startswith("sw_") and key not in _STATIC_KEYSWITCH_OPCODES
        )
        if dynamic_keyswitches:
            raise ValueError(
                f"SFZ dynamic keyswitch opcodes are unsupported "
                f"({', '.join(dynamic_keyswitches)}): {source}"
            )
        switch_low = values.get("sw_lolast", values.get("sw_last"))
        switch_high = values.get("sw_hilast", values.get("sw_last"))
        if switch_low is not None or switch_high is not None:
            selected = keyswitch_select
            if selected is None and "sw_default" in values:
                selected = note_number(values["sw_default"])
            if selected is None:
                raise ValueError(
                    f"SFZ region {region.index} requires a keyswitch but neither "
                    f"keyswitch_select nor sw_default is available: {source}"
                )
            low = note_number(switch_low if switch_low is not None else switch_high)
            high = note_number(switch_high if switch_high is not None else switch_low)
            if not low <= float(selected) <= high:
                continue
        region_trigger = values.get("trigger", "attack").lower()
        if region_trigger != requested_trigger:
            continue
        sample_name = values.get("sample")
        if not sample_name:
            continue
        sample_path = _safe_relative_path(
            root, sample_base, sample_name, kind="SFZ sample"
        )
        if not sample_path.is_file():
            raise ValueError(f"SFZ sample file does not exist: {sample_path}")

        root_value = values.get("pitch_keycenter", values.get("key"))
        if root_value is not None:
            root_midi = note_number(root_value)
        elif "lokey" in values and "hikey" in values:
            root_midi = (note_number(values["lokey"]) + note_number(values["hikey"])) / 2.0
        elif root_midi_fallback is not None:
            root_midi = float(root_midi_fallback)
        else:
            raise ValueError(
                f"SFZ region {region.index} has no key or pitch_keycenter: {source}"
            )
        key_min = note_number(values.get("lokey", values.get("key", root_midi)))
        key_max = note_number(values.get("hikey", values.get("key", root_midi)))
        if key_min > key_max:
            raise ValueError(f"SFZ region {region.index} has an invalid key range: {source}")
        loop_mode = values.get(
            "loop_mode", "loop_sustain" if use_embedded_loops else "no_loop"
        ).lower()
        if requested_trigger == "release":
            loop_mode = "one_shot"
        if loop_mode not in _LOOP_MODES:
            raise ValueError(
                f"SFZ region {region.index} has unsupported loop_mode {loop_mode!r}: {source}"
            )
        tune = _effective_cc_modulated_value(values, "tune", 0.0, cc_values)
        transpose = _effective_cc_modulated_value(
            values, "transpose", 0.0, cc_values
        )
        attack_seconds = _effective_cc_modulated_value(
            values, "ampeg_attack", 0.0, cc_values
        )
        decay_seconds = _effective_cc_modulated_value(
            values, "ampeg_decay", 0.0, cc_values
        )
        sustain_percent = _effective_cc_modulated_value(
            values, "ampeg_sustain", 100.0, cc_values
        )
        release_seconds = _effective_cc_modulated_value(
            values, "ampeg_release", 0.25, cc_values
        )
        if min(attack_seconds, decay_seconds, release_seconds) < 0.0:
            raise ValueError(
                f"SFZ region {region.index} has a negative envelope time: {source}"
            )
        if not 0.0 <= sustain_percent <= 100.0:
            raise ValueError(
                f"SFZ region {region.index} has invalid ampeg_sustain: {source}"
            )
        delay_seconds = _effective_cc_modulated_value(
            values, "delay", 0.0, cc_values
        )
        offset_frames = int(
            _effective_cc_modulated_value(values, "offset", 0.0, cc_values)
        )
        if delay_seconds < 0.0 or offset_frames < 0:
            raise ValueError(
                f"SFZ region {region.index} has a negative delay/offset: {source}"
            )
        fade_in = _velocity_fade(
            values,
            "xfin_lovel",
            "xfin_hivel",
            region_index=region.index,
        )
        fade_out = _velocity_fade(
            values,
            "xfout_lovel",
            "xfout_hivel",
            region_index=region.index,
        )
        rt_decay = float(values["rt_decay"]) if "rt_decay" in values else None
        if rt_decay is not None and rt_decay < 0.0:
            raise ValueError(f"SFZ region {region.index} has negative rt_decay: {source}")
        # Standard SFZ semantics: a one-sided random bound keeps the other
        # end at its 0.0 / 1.0 default.
        has_random_low = "lorand" in values
        has_random_high = "hirand" in values
        random_low = float(values.get("lorand", 0.0))
        random_high = float(values.get("hirand", 1.0))
        if not 0.0 <= random_low <= random_high <= 1.0:
            raise ValueError(f"SFZ region {region.index} has invalid random range: {source}")
        stable_key = f"{stable_prefix}:{region.index}"
        item: dict[str, Any] = {
            "sample": str(sample_path),
            "root_midi": root_midi,
            "measured_tuning_cents": -tune - 100.0 * transpose,
            # In ignore-pitch mode the key is only a region selector, but SFZ
            # tune/transpose still alter native-speed playback.
            "native_playback_ratio": 2.0 ** ((tune + 100.0 * transpose) / 1200.0),
            "key_min": key_min,
            "key_max": key_max,
            "velocity_min": max(0.0, float(values.get("lovel", 0.0)) / 127.0),
            "velocity_max": min(1.0, float(values.get("hivel", 127.0)) / 127.0),
            "gain_db": _effective_cc_modulated_value(
                values, "volume", 0.0, cc_values
            ),
            "pan": max(
                -1.0, min(1.0, _effective_pan(values, cc_values) / 100.0)
            ),
            "delay_seconds": delay_seconds,
            "attack_seconds": attack_seconds,
            "decay_seconds": decay_seconds,
            "sustain_level": sustain_percent / 100.0,
            "release_seconds": release_seconds,
            "offset_frames": offset_frames,
            "loop_mode": loop_mode,
            "stable_key": stable_key,
            "random_min": random_low,
            "random_max": random_high,
            "_dedicated_has_random_range": has_random_low or has_random_high,
        }
        if "end" in values:
            sample_end = int(float(values["end"]))
            if sample_end < 0:
                raise ValueError(
                    f"SFZ region {region.index} uses unsupported silent end=-1: {source}"
                )
            # SFZ ``end`` is inclusive; SampleInstrument stores an exclusive
            # upper boundary, matching the existing loop_end adapter.
            item["sample_end"] = sample_end + 1
        if use_embedded_loops or loop_mode in ("loop_sustain", "loop_continuous"):
            item["use_embedded_loop"] = True
        if "loop_start" in values or "loop_end" in values:
            if "loop_start" not in values or "loop_end" not in values:
                raise ValueError(
                    f"SFZ region {region.index} must define loop_start and loop_end together"
                )
            item["loop_start"] = int(float(values["loop_start"]))
            # SFZ loop_end, like WAV smpl end, is inclusive.  SampleInstrument
            # stores an exclusive upper boundary.
            item["loop_end"] = int(float(values["loop_end"])) + 1
        if "seq_length" in values or "seq_position" in values:
            length = int(values.get("seq_length", values.get("seq_position", 1)))
            position = int(values.get("seq_position", 1))
            item["round_robin_length"] = length
            item["round_robin_position"] = position
        converted.append(item)
        runtime[stable_key] = DedicatedSfzRegionMetadata(
            group=_normalized_group(values.get("group")),
            off_by=_normalized_group(values.get("off_by")),
            off_time=(float(values["off_time"]) if "off_time" in values else None),
            velocity_fade_in=fade_in,
            velocity_fade_out=fade_out,
            rt_decay_db_per_second=rt_decay,
        )
    if not converted and requested_trigger == "attack":
        raise ValueError(f"SFZ contains no playable attack regions: {source}")
    return converted, runtime


def _relative_entry(root: Path, raw: object, *, kind: str) -> Path:
    return _safe_relative_path(root, root, str(raw), kind=kind)


def _manifest_playable_ranges(
    manifest: dict[str, Any],
) -> tuple[tuple[float, float], ...]:
    """Read ordered, disjoint inclusive MIDI spans from a manifest."""

    raw_ranges = manifest.get("playable_ranges")
    if raw_ranges is None:
        return ()
    if not isinstance(raw_ranges, list) or not raw_ranges:
        raise ValueError("dedicated_sfz playable_ranges must be a non-empty array")

    ranges: list[tuple[float, float]] = []
    previous_high: float | None = None
    for index, raw_span in enumerate(raw_ranges):
        if not isinstance(raw_span, list) or len(raw_span) != 2:
            raise ValueError(
                f"dedicated_sfz playable_ranges[{index}] must be a "
                "[note_min, note_max] pair"
            )
        if any(isinstance(value, bool) for value in raw_span):
            raise ValueError(
                f"dedicated_sfz playable_ranges[{index}] notes must be numbers"
            )
        try:
            low, high = float(raw_span[0]), float(raw_span[1])
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"dedicated_sfz playable_ranges[{index}] notes must be numbers"
            ) from error
        if not math.isfinite(low) or not math.isfinite(high):
            raise ValueError(
                f"dedicated_sfz playable_ranges[{index}] notes must be finite"
            )
        if not 0.0 <= low <= high <= 127.0:
            raise ValueError(
                f"dedicated_sfz playable_ranges[{index}] must satisfy "
                "0 <= min <= max <= 127"
            )
        if previous_high is not None and low <= previous_high:
            raise ValueError(
                "dedicated_sfz playable_ranges must be ordered, "
                "non-overlapping inclusive spans"
            )
        ranges.append((low, high))
        previous_high = high
    return tuple(ranges)


def _articulation_envelope_override(
    specification: dict[str, Any],
    field: str,
) -> float | None:
    """Return one explicit, finite per-articulation envelope override."""

    if field not in specification:
        return None
    raw = specification[field]
    if (
        isinstance(raw, bool)
        or not isinstance(raw, (int, float))
        or not math.isfinite(float(raw))
        or float(raw) < 0.0
    ):
        raise ValueError(
            f"dedicated SFZ articulation {field} must be finite and non-negative"
        )
    return float(raw)


def _read_sample_region_exclusions(
    manifest: dict[str, Any],
) -> frozenset[str]:
    """Read exact sample paths whose converted SFZ regions must be removed."""

    raw = manifest.get("sample_region_exclusions")
    if raw is None:
        return frozenset()
    if not isinstance(raw, list) or not raw:
        raise ValueError("sample_region_exclusions must be a non-empty array")

    exclusions: set[str] = set()
    for index, raw_path in enumerate(raw):
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(
                f"sample_region_exclusions[{index}] must be a non-empty "
                "asset-root-relative POSIX path"
            )
        relative = PurePosixPath(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if (
            not relative.parts
            or relative.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or "\\" in raw_path
            or ":" in raw_path
            or relative.as_posix() != raw_path
            or any(part in (".", "..") for part in relative.parts)
        ):
            raise ValueError(
                "sample_region_exclusions paths must be canonical "
                f"asset-root-relative POSIX paths, got {raw_path!r}"
            )
        if raw_path in exclusions:
            raise ValueError(
                f"duplicate sample_region_exclusions entry for {raw_path!r}"
            )
        exclusions.add(raw_path)
    return frozenset(exclusions)


def _read_sample_gain_db_overrides(
    manifest: dict[str, Any],
) -> dict[str, float]:
    """Read exact additive gain corrections for dedicated SFZ samples.

    Corrections use canonical asset-root-relative POSIX paths so tracked
    manifests can level individual upstream recordings without modifying the
    pinned SFZ/WAV resource tree.  Runtime matching remains case-sensitive and
    fail-closed even on Windows.
    """

    raw = manifest.get("sample_gain_db_overrides")
    if raw is None:
        return {}
    if not isinstance(raw, list) or not raw:
        raise ValueError("sample_gain_db_overrides must be a non-empty array")

    overrides: dict[str, float] = {}
    required = {"sample", "gain_db"}
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
                f"sample_gain_db_overrides[{index}] must declare sample and gain_db"
            )

        raw_path = specification["sample"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError(
                f"sample_gain_db_overrides[{index}].sample must be a "
                "non-empty asset-root-relative POSIX path"
            )
        relative = PurePosixPath(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if (
            not relative.parts
            or relative.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or "\\" in raw_path
            or ":" in raw_path
            or relative.as_posix() != raw_path
            or any(part in (".", "..") for part in relative.parts)
        ):
            raise ValueError(
                "sample_gain_db_overrides sample paths must be canonical "
                f"asset-root-relative POSIX paths, got {raw_path!r}"
            )
        if raw_path in overrides:
            raise ValueError(
                "duplicate sample_gain_db_overrides entry for "
                f"{raw_path!r}"
            )

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
        overrides[raw_path] = float(raw_gain)
    return overrides


def _apply_sample_gain_db_overrides(
    regions: list[dict[str, Any]],
    asset_root: Path,
    overrides: dict[str, float],
    matched: set[str],
) -> None:
    """Add exact sample corrections to converted attack/release regions."""

    if not overrides:
        return
    for region in regions:
        sample_path = Path(region["sample"]).resolve()
        try:
            relative = sample_path.relative_to(asset_root).as_posix()
        except ValueError as error:
            raise ValueError(
                "dedicated SFZ gain-corrected sample escapes asset root: "
                f"{sample_path}"
            ) from error
        correction = overrides.get(relative)
        if correction is None:
            continue
        region["gain_db"] = float(region.get("gain_db", 0.0)) + correction
        matched.add(relative)


def _intervals_overlap(
    first_low: float, first_high: float, second_low: float, second_high: float
) -> bool:
    return max(first_low, second_low) <= min(first_high, second_high)


def _same_round_robin_family(
    first: dict[str, Any],
    second: dict[str, Any],
    metadata: dict[str, DedicatedSfzRegionMetadata],
) -> bool:
    first_length = first.get("round_robin_length")
    second_length = second.get("round_robin_length")
    first_position = first.get("round_robin_position")
    second_position = second.get("round_robin_position")
    if (
        first_length is None
        or second_length is None
        or first_length != second_length
        or first_position == second_position
    ):
        return False
    conditions = ("key_min", "key_max", "velocity_min", "velocity_max")
    if any(float(first[key]) != float(second[key]) for key in conditions):
        return False
    first_metadata = metadata[str(first["stable_key"])]
    second_metadata = metadata[str(second["stable_key"])]
    return (
        first_metadata.velocity_fade_in == second_metadata.velocity_fade_in
        and first_metadata.velocity_fade_out == second_metadata.velocity_fade_out
        and first_metadata.group == second_metadata.group
        and first_metadata.off_by == second_metadata.off_by
    )


def _regions_conflict(
    first: dict[str, Any],
    second: dict[str, Any],
    metadata: dict[str, DedicatedSfzRegionMetadata],
) -> bool:
    if not _intervals_overlap(
        float(first["key_min"]),
        float(first["key_max"]),
        float(second["key_min"]),
        float(second["key_max"]),
    ):
        return False
    if not _intervals_overlap(
        float(first["velocity_min"]),
        float(first["velocity_max"]),
        float(second["velocity_min"]),
        float(second["velocity_max"]),
    ):
        return False
    if (
        bool(first.get("_dedicated_has_random_range", False))
        and bool(second.get("_dedicated_has_random_range", False))
        and (
            float(first["random_max"]) <= float(second["random_min"])
            or float(second["random_max"]) <= float(first["random_min"])
        )
    ):
        return False
    return not _same_round_robin_family(first, second, metadata)


def _partition_region_layers(
    regions: list[dict[str, Any]],
    metadata: dict[str, DedicatedSfzRegionMetadata],
) -> tuple[list[dict[str, Any]], ...]:
    """Greedily separate true SFZ layers while retaining RR in one engine."""

    layers: list[list[dict[str, Any]]] = []
    for region in regions:
        for layer in layers:
            if all(
                not _regions_conflict(region, existing, metadata)
                for existing in layer
            ):
                layer.append(region)
                break
        else:
            layers.append([region])
    return tuple(layers)


def _round_robin_family_signature(
    region: dict[str, Any],
    metadata: dict[str, DedicatedSfzRegionMetadata],
) -> tuple[object, ...] | None:
    """Return the selector/effect identity of one real RR family."""

    length = region.get("round_robin_length")
    position = region.get("round_robin_position")
    if length is None or position is None:
        return None
    runtime = metadata[str(region["stable_key"])]
    return (
        int(length),
        float(region["key_min"]),
        float(region["key_max"]),
        float(region["velocity_min"]),
        float(region["velocity_max"]),
        runtime.velocity_fade_in,
        runtime.velocity_fade_out,
        runtime.group,
        runtime.off_by,
    )


def _apply_sample_region_exclusions(
    regions: list[dict[str, Any]],
    metadata: dict[str, DedicatedSfzRegionMetadata],
    asset_root: Path,
    exclusions: frozenset[str],
    matched: set[str],
) -> None:
    """Remove exact attack/release regions and repair each affected RR family."""

    if not exclusions or not regions:
        return

    excluded_region_ids: set[int] = set()
    for region in regions:
        sample_path = Path(region["sample"]).resolve()
        try:
            relative = sample_path.relative_to(asset_root).as_posix()
        except ValueError as error:
            raise ValueError(
                "dedicated SFZ excluded sample escapes asset root: "
                f"{sample_path}"
            ) from error
        if relative in exclusions:
            excluded_region_ids.add(id(region))
            matched.add(relative)

    if not excluded_region_ids:
        return

    # Partition first, while the original complete RR set is still available.
    # This keeps same-position layered/microphone regions in independent
    # families, matching the engines the adapter would otherwise build.
    for layer in _partition_region_layers(regions, metadata):
        families: dict[tuple[object, ...], list[dict[str, Any]]] = {}
        for region in layer:
            signature = _round_robin_family_signature(region, metadata)
            if signature is not None:
                families.setdefault(signature, []).append(region)
        for family in families.values():
            if not any(id(region) in excluded_region_ids for region in family):
                continue
            survivors = [
                region
                for region in family
                if id(region) not in excluded_region_ids
            ]
            survivors.sort(
                key=lambda region: (
                    int(region["round_robin_position"]),
                    str(region["stable_key"]),
                )
            )
            if len(survivors) == 1:
                survivors[0].pop("round_robin_position", None)
                survivors[0].pop("round_robin_length", None)
            elif survivors:
                for position, region in enumerate(survivors, start=1):
                    region["round_robin_position"] = position
                    region["round_robin_length"] = len(survivors)

    removed_stable_keys = {
        str(region["stable_key"])
        for region in regions
        if id(region) in excluded_region_ids
    }
    regions[:] = [
        region for region in regions if id(region) not in excluded_region_ids
    ]
    for stable_key in removed_stable_keys:
        metadata.pop(stable_key, None)


class DedicatedSfzInstrument(Instrument):
    """Reusable, deterministic adapter for audited dedicated SFZ libraries.

    Manifest contract (schema registration is intentionally separate):

    * ``asset_root``: dedicated library root, relative to the manifest.
    * either ``sfz`` for one mapping or ``articulations`` mapping names to an
      SFZ path/string or ``{"sfz": ...}`` object.
    * ``pitch_mode``: ``pitched`` (normal transposition), ``fixed`` (requires
      ``fixed_midi_note``), or ``ignore`` (key selects a native-speed sample).
    * optional ``note_min``, ``note_max`` and disjoint ``playable_ranges``;
      plus ``default_articulation``, gains, envelope defaults, embedded-loop
      policy, expression smoothing, exact region exclusions and per-sample
      gain corrections.

    Every SFZ/include/sample is validated at construction.  There is no
    SoundFont or General MIDI fallback path.
    """

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        base = Path(base_directory).resolve()
        if "asset_root" not in manifest:
            raise ValueError("dedicated_sfz manifest requires asset_root")
        asset_root = (base / str(manifest["asset_root"])).resolve()
        if not asset_root.is_dir():
            raise ValueError(f"dedicated SFZ asset_root does not exist: {asset_root}")
        self.asset_root = asset_root
        apply_pitch_calibration = bool(
            manifest.get("apply_pitch_calibration", False)
        )
        calibration_samples: dict[str, Any] = {}
        calibrated_sample_paths: set[str] = set()
        if apply_pitch_calibration:
            calibration_name = manifest.get("pitch_calibration")
            if not isinstance(calibration_name, str) or not calibration_name:
                raise ValueError(
                    "apply_pitch_calibration requires pitch_calibration"
                )
            calibration_path = _relative_entry(
                base,
                calibration_name,
                kind="pitch calibration report",
            )
            if not calibration_path.is_file():
                raise ValueError(
                    f"dedicated SFZ pitch calibration does not exist: "
                    f"{calibration_path}"
                )
            calibration_document = json.loads(
                calibration_path.read_text(encoding="utf-8")
            )
            if calibration_document.get("applicable") is not True:
                raise ValueError(
                    "runtime pitch calibration must be an applicable report"
                )
            raw_samples = calibration_document.get("samples")
            if not isinstance(raw_samples, dict) or not raw_samples:
                raise ValueError(
                    "runtime pitch calibration samples must be a non-empty object"
                )
            calibration_samples = raw_samples
        self.pitch_mode = str(manifest.get("pitch_mode", "pitched")).lower()
        if self.pitch_mode not in _PITCH_MODES:
            raise ValueError(
                f"dedicated_sfz pitch_mode must be one of {sorted(_PITCH_MODES)}"
            )
        self.fixed_midi_note = (
            note_number(manifest["fixed_midi_note"])
            if "fixed_midi_note" in manifest
            else None
        )
        if self.pitch_mode == "fixed" and self.fixed_midi_note is None:
            raise ValueError("dedicated_sfz fixed pitch_mode requires fixed_midi_note")
        self.note_min = (
            note_number(manifest["note_min"]) if "note_min" in manifest else None
        )
        self.note_max = (
            note_number(manifest["note_max"]) if "note_max" in manifest else None
        )
        if (self.note_min is None) != (self.note_max is None):
            raise ValueError("dedicated_sfz must define note_min and note_max together")
        if self.note_min is not None and self.note_min > self.note_max:
            raise ValueError("dedicated_sfz has an invalid declared note range")
        self.playable_ranges = _manifest_playable_ranges(manifest)
        if self.playable_ranges:
            outer_min = self.playable_ranges[0][0]
            outer_max = self.playable_ranges[-1][1]
            if self.note_min is None:
                self.note_min = outer_min
                self.note_max = outer_max
            elif self.note_min != outer_min or self.note_max != outer_max:
                raise ValueError(
                    "dedicated_sfz note_min/note_max must match the outer "
                    "envelope of playable_ranges"
                )

        raw_articulations = manifest.get("articulations")
        if raw_articulations is None:
            if "sfz" not in manifest:
                raise ValueError("dedicated_sfz requires sfz or articulations")
            default_name = str(manifest.get("default_articulation", "default"))
            raw_articulations = {default_name: {"sfz": manifest["sfz"]}}
        elif "sfz" in manifest:
            raise ValueError("dedicated_sfz cannot define both sfz and articulations")
        if not isinstance(raw_articulations, dict) or not raw_articulations:
            raise ValueError("dedicated_sfz articulations must be a non-empty object")

        self.default_articulation = str(
            manifest.get("default_articulation", next(iter(raw_articulations)))
        )
        self.articulation = self.default_articulation
        self.articulations: dict[str, _ArticulationRuntime] = {}
        self.articulation_playable_ranges: dict[
            str, tuple[tuple[float, float], ...]
        ] = {}
        shared_cache: dict[Path, Any] = {}
        default_gain = float(manifest.get("gain", 1.0))
        velocity_exponent = float(manifest.get("velocity_exponent", 1.0))
        release_seconds = float(manifest.get("release_seconds", 0.25))
        embedded_default = bool(manifest.get("use_embedded_loops", True))
        release_trigger_gain = float(manifest.get("release_trigger_gain", 1.0))
        sample_region_exclusions = _read_sample_region_exclusions(manifest)
        matched_sample_region_exclusions: set[str] = set()
        sample_gain_db_overrides = _read_sample_gain_db_overrides(manifest)
        matched_sample_gain_db_overrides: set[str] = set()

        for articulation_name, raw_spec in raw_articulations.items():
            name = str(articulation_name)
            if not name:
                raise ValueError("dedicated_sfz articulation names must not be empty")
            if isinstance(raw_spec, str):
                spec: dict[str, Any] = {"sfz": raw_spec}
            elif isinstance(raw_spec, dict):
                spec = raw_spec
            else:
                raise ValueError(f"dedicated_sfz articulation {name!r} must be a path or object")
            articulation_ranges = _manifest_playable_ranges(spec)
            if not articulation_ranges:
                articulation_ranges = self.playable_ranges
            if not articulation_ranges and self.note_min is not None:
                assert self.note_max is not None
                articulation_ranges = ((self.note_min, self.note_max),)
            if self.note_min is not None:
                assert self.note_max is not None
                outside = [
                    (low, high)
                    for low, high in articulation_ranges
                    if low < self.note_min or high > self.note_max
                ]
                if outside:
                    raise ValueError(
                        f"dedicated_sfz articulation {name!r} playable_ranges "
                        f"must stay within {self.note_min:g}..{self.note_max:g}"
                    )
            self.articulation_playable_ranges[name] = articulation_ranges
            if "sfz" not in spec:
                raise ValueError(f"dedicated_sfz articulation {name!r} requires sfz")
            sfz_path = _relative_entry(
                asset_root, spec["sfz"], kind=f"articulation {name!r} SFZ"
            )
            if not sfz_path.is_file():
                raise ValueError(f"dedicated SFZ mapping does not exist: {sfz_path}")
            use_embedded = bool(spec.get("use_embedded_loops", embedded_default))
            raw_keyswitch = spec.get(
                "keyswitch_select", manifest.get("keyswitch_select")
            )
            keyswitch = (
                note_number(raw_keyswitch) if raw_keyswitch is not None else None
            )
            stable_prefix = sfz_path.relative_to(asset_root).as_posix()
            attack_regions, metadata = dedicated_regions_to_manifest(
                sfz_path,
                asset_root=asset_root,
                trigger="attack",
                use_embedded_loops=use_embedded,
                stable_prefix=stable_prefix,
                root_midi_fallback=self.fixed_midi_note,
                keyswitch_select=keyswitch,
            )
            release_regions, release_metadata = dedicated_regions_to_manifest(
                sfz_path,
                asset_root=asset_root,
                trigger="release",
                use_embedded_loops=False,
                stable_prefix=f"{stable_prefix}:release",
                root_midi_fallback=self.fixed_midi_note,
                keyswitch_select=keyswitch,
            )
            _apply_sample_region_exclusions(
                attack_regions,
                metadata,
                asset_root,
                sample_region_exclusions,
                matched_sample_region_exclusions,
            )
            _apply_sample_region_exclusions(
                release_regions,
                release_metadata,
                asset_root,
                sample_region_exclusions,
                matched_sample_region_exclusions,
            )
            if not attack_regions:
                raise ValueError(
                    f"dedicated SFZ articulation {name!r} contains no playable "
                    "attack regions after sample_region_exclusions"
                )
            attack_override = _articulation_envelope_override(
                spec,
                "attack_override_seconds",
            )
            if attack_override is not None:
                for region in attack_regions:
                    region["attack_seconds"] = attack_override
            _apply_sample_gain_db_overrides(
                attack_regions,
                asset_root,
                sample_gain_db_overrides,
                matched_sample_gain_db_overrides,
            )
            _apply_sample_gain_db_overrides(
                release_regions,
                asset_root,
                sample_gain_db_overrides,
                matched_sample_gain_db_overrides,
            )
            if apply_pitch_calibration:
                for region in attack_regions:
                    sample_path = Path(region["sample"]).resolve()
                    try:
                        relative = sample_path.relative_to(asset_root).as_posix()
                    except ValueError as error:
                        raise ValueError(
                            f"dedicated SFZ calibrated sample escapes asset root: "
                            f"{sample_path}"
                        ) from error
                    calibration = calibration_samples.get(relative)
                    measured = (
                        calibration.get("measured_detune_cents")
                        if isinstance(calibration, dict)
                        else None
                    )
                    if (
                        not isinstance(measured, (int, float))
                        or isinstance(measured, bool)
                        or not math.isfinite(float(measured))
                    ):
                        raise ValueError(
                            "runtime pitch calibration has no finite "
                            f"measured_detune_cents for {relative}"
                        )
                    region["measured_tuning_cents"] = float(measured)
                    calibrated_sample_paths.add(relative)
                for region in release_regions:
                    sample_path = Path(region["sample"]).resolve()
                    try:
                        relative = sample_path.relative_to(asset_root).as_posix()
                    except ValueError:
                        continue
                    calibration = calibration_samples.get(relative)
                    measured = (
                        calibration.get("measured_detune_cents")
                        if isinstance(calibration, dict)
                        else None
                    )
                    if isinstance(measured, (int, float)) and not isinstance(
                        measured, bool
                    ):
                        region["measured_tuning_cents"] = float(measured)
            articulation_gain = default_gain * float(spec.get("gain", 1.0))
            reference_a4 = float(manifest.get("reference_a4_hz", 440.0))
            articulation_velocity_exponent = float(
                spec.get("velocity_exponent", velocity_exponent)
            )

            def build_layers(
                region_set: list[dict[str, Any]],
                region_metadata: dict[str, DedicatedSfzRegionMetadata],
                *,
                layer_gain: float,
                layer_release_seconds: float,
            ) -> tuple[_EngineLayer, ...]:
                built: list[_EngineLayer] = []
                for layer_regions in _partition_region_layers(
                    region_set, region_metadata
                ):
                    engine = SampleInstrument.from_manifest(
                        {
                            "regions": layer_regions,
                            "reference_a4_hz": reference_a4,
                            "gain": layer_gain,
                            "velocity_exponent": articulation_velocity_exponent,
                            "release_seconds": layer_release_seconds,
                        },
                        sample_rate,
                        base_directory=str(asset_root),
                        sample_cache=shared_cache,
                    )
                    built.append(_EngineLayer(engine, region_metadata))
                return tuple(built)

            attack_layers = build_layers(
                attack_regions,
                metadata,
                layer_gain=articulation_gain,
                layer_release_seconds=float(
                    spec.get("release_seconds", release_seconds)
                ),
            )
            release_layers = build_layers(
                release_regions,
                release_metadata,
                layer_gain=articulation_gain
                * float(spec.get("release_trigger_gain", release_trigger_gain)),
                layer_release_seconds=release_seconds,
            ) if release_regions else ()
            self._validate_coverage(
                name,
                attack_regions,
                articulation_ranges,
            )
            release_override = (
                float(spec["release_override_seconds"])
                if "release_override_seconds" in spec
                else None
            )
            if release_override is not None and (
                not math.isfinite(release_override) or release_override < 0.0
            ):
                raise ValueError(
                    f"dedicated SFZ articulation {name!r} "
                    "release_override_seconds must be finite and non-negative"
                )
            self.articulations[name] = _ArticulationRuntime(
                attack_layers,
                release_layers,
                release_override,
            )

        missing_region_exclusions = sorted(
            set(sample_region_exclusions) - matched_sample_region_exclusions
        )
        if missing_region_exclusions:
            raise ValueError(
                "sample_region_exclusions did not match loaded dedicated SFZ "
                "attack or release regions across all articulations: "
                + ", ".join(missing_region_exclusions)
            )

        missing_gain_overrides = sorted(
            set(sample_gain_db_overrides) - matched_sample_gain_db_overrides
        )
        if missing_gain_overrides:
            raise ValueError(
                "sample_gain_db_overrides did not match loaded dedicated SFZ "
                "attack or release regions across all articulations: "
                + ", ".join(missing_gain_overrides)
            )

        if apply_pitch_calibration:
            stale_paths = (
                set(calibration_samples)
                - calibrated_sample_paths
                - matched_sample_region_exclusions
            )
            if stale_paths:
                preview = ", ".join(sorted(stale_paths)[:3])
                raise ValueError(
                    "runtime pitch calibration contains samples outside the "
                    f"current attack mappings: {preview}"
                )

        if self.default_articulation not in self.articulations:
            raise ValueError(
                f"unknown default dedicated SFZ articulation {self.default_articulation!r}"
            )
        self.routes: dict[int, _NoteRoute] = {}
        self._voice_groups: dict[
            tuple[str, int, int], DedicatedSfzRegionMetadata
        ] = {}
        self._next_note_id_value = int(manifest.get("auxiliary_note_id_base", 1_800_000_000))
        self.sustain_pedal = 0.0
        self.choke_seconds = max(0.001, float(manifest.get("choke_seconds", 0.025)))
        smoothing = max(0.001, float(manifest.get("control_smoothing_seconds", 0.012)))
        self._control_coefficient = 1.0 - math.exp(-1.0 / (smoothing * sample_rate))
        self.expression = self.expression_target = 1.0
        self.modulation = self.modulation_target = 1.0
        self.expression_exponent = max(0.01, float(manifest.get("expression_exponent", 1.0)))
        self.modulation_exponent = max(0.01, float(manifest.get("modulation_exponent", 1.0)))
        self.modulation_floor = min(
            1.0, max(0.0, float(manifest.get("modulation_floor", 0.0)))
        )

    def _validate_coverage(
        self,
        name: str,
        regions: list[dict[str, Any]],
        articulation_ranges: tuple[tuple[float, float], ...],
    ) -> None:
        if self.pitch_mode == "fixed":
            assert self.fixed_midi_note is not None
            if not any(
                float(item["key_min"]) <= self.fixed_midi_note <= float(item["key_max"])
                for item in regions
            ):
                raise ValueError(
                    f"dedicated SFZ articulation {name!r} does not map fixed MIDI "
                    f"note {self.fixed_midi_note:g}"
                )
            return
        if self.note_min is None:
            return
        assert self.note_max is not None
        declared_ranges = articulation_ranges or ((self.note_min, self.note_max),)
        missing = [
            note
            for low, high in declared_ranges
            for note in range(math.ceil(low), math.floor(high) + 1)
            if not any(
                float(item["key_min"]) <= note <= float(item["key_max"])
                for item in regions
            )
        ]
        if missing:
            preview = ", ".join(str(note) for note in missing[:8])
            suffix = " ..." if len(missing) > 8 else ""
            raise ValueError(
                f"dedicated SFZ articulation {name!r} does not cover declared MIDI "
                f"range; missing {preview}{suffix}"
            )

    def _next_note_id(self) -> int:
        self._next_note_id_value += 1
        return self._next_note_id_value

    def _input_midi(self, event: PerformanceEvent, tuning: EqualTemperament) -> float:
        if "midi_note" in event.payload:
            return float(event.payload["midi_note"])
        return 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / tuning.a4_hz)

    def _playback_payload(
        self, event: PerformanceEvent, tuning: EqualTemperament
    ) -> dict[str, Any]:
        input_midi = self._input_midi(event, tuning)
        if self.pitch_mode != "fixed" and self.note_min is not None:
            assert self.note_max is not None
            if not self.note_min <= input_midi <= self.note_max:
                raise ValueError(
                    f"dedicated SFZ note {input_midi:.3f} is outside declared range "
                    f"{self.note_min:g}..{self.note_max:g}"
                )
            playable_ranges = self.articulation_playable_ranges.get(
                self.articulation,
                self.playable_ranges,
            )
            if playable_ranges and not any(
                low <= input_midi <= high
                for low, high in playable_ranges
            ):
                ranges = ", ".join(
                    f"{low:g}..{high:g}" for low, high in playable_ranges
                )
                raise ValueError(
                    f"dedicated SFZ articulation {self.articulation!r} note "
                    f"{input_midi:.3f} is outside declared playable ranges {ranges}"
                )
        payload = dict(event.payload)
        if "velocity" in payload:
            # SFZ velocity layers are integer lovel/hivel bands; snapping the
            # float velocity onto the 1/127 grid keeps boundary values from
            # falling into the gap between two adjacent bands, where a far-key
            # full-velocity region would otherwise win region selection.
            payload["velocity"] = round(float(payload["velocity"]) * 127.0) / 127.0
        payload["_sample_random_value"] = zlib.crc32(
            f"{event.payload['note_id']}:{event.sequence}".encode("utf-8")
        ) / 0xFFFFFFFF
        if self.pitch_mode == "fixed":
            assert self.fixed_midi_note is not None
            payload.pop("pitch_hz", None)
            payload["midi_note"] = self.fixed_midi_note
        elif self.pitch_mode == "ignore":
            payload["midi_note"] = input_midi
            payload["_sample_ignore_pitch"] = True
        elif "pitch_hz" in payload:
            # Keep exact Hz for playback but make region selection respect the
            # score tuning rather than SampleInstrument's 440-Hz fallback.
            payload["midi_note"] = input_midi
        return payload

    @staticmethod
    def _selected_voice_matches(
        voice: Any, target_midi: float, velocity: float
    ) -> bool:
        half_velocity_step = 0.5 / 127.0
        region = voice.region
        key_matches = (
            region.key_min is None
            or region.key_max is None
            or region.key_min - 0.5 <= target_midi <= region.key_max + 0.5
        )
        velocity_matches = (
            region.velocity_min - half_velocity_step
            <= velocity
            <= region.velocity_max + half_velocity_step
        )
        return key_matches and velocity_matches

    def _trigger_layers(
        self,
        layers: tuple[_EngineLayer, ...],
        payload: dict[str, Any],
        event: PerformanceEvent,
        tuning: EqualTemperament,
        *,
        articulation: str,
        selection_phase: str,
        held_seconds: float = 0.0,
    ) -> list[tuple[_RoutedVoice, DedicatedSfzRegionMetadata]]:
        target_midi = float(payload["midi_note"])
        velocity = float(payload["velocity"])
        triggered: list[tuple[_RoutedVoice, DedicatedSfzRegionMetadata]] = []
        for layer_index, layer in enumerate(layers):
            wrapper_role_sha256 = stable_variant_sha256(
                "dedicated-sfz-wrapper-role-v1",
                {
                    "phase": selection_phase,
                    "articulation": articulation,
                    "layer_index": layer_index,
                },
            )
            internal_id = self._next_note_id()
            layer_payload = dict(payload)
            layer_payload["note_id"] = internal_id
            capture = current_runtime_variant_capture()
            selection_count_before = (
                capture.selection_count if capture is not None else None
            )
            layer.engine.handle_event(
                PerformanceEvent(
                    event.sample, event.sequence, "note_on", layer_payload
                ),
                tuning,
            )
            if capture is not None:
                assert selection_count_before is not None
                if capture.selection_count != selection_count_before + 1:
                    raise RuntimeVariantError(
                        "Dedicated SFZ layer did not emit exactly one "
                        "provisional runtime selection"
                    )
                selection_index = selection_count_before
            else:
                selection_index = None
            voice = layer.engine.voices[internal_id]
            if not self._selected_voice_matches(voice, target_midi, velocity):
                if capture is not None:
                    assert selection_index is not None
                    capture.finalize_selection(
                        selection_index,
                        wrapper_outcome={
                            "schema_version": 1,
                            "kind": "dedicated_sfz_wrapper_outcome",
                            "phase": selection_phase,
                            "articulation": articulation,
                            "layer_index": layer_index,
                            "wrapper_role_sha256": (
                                wrapper_role_sha256
                            ),
                            "event_sequence": event.sequence,
                            "target_midi": target_midi,
                            "velocity": velocity,
                            "key_velocity_match": False,
                            "wrapper_velocity_gain": None,
                            "route_committed": False,
                            "committed_amplitude": None,
                            "final_status": (
                                "discarded_key_or_velocity_mismatch"
                            ),
                        },
                    )
                del layer.engine.voices[internal_id]
                continue
            metadata = layer.region_runtime[voice.region.stable_key]
            gain = metadata.velocity_gain(velocity)
            if (
                held_seconds > 0.0
                and metadata.rt_decay_db_per_second is not None
            ):
                gain *= 10.0 ** (
                    -metadata.rt_decay_db_per_second * held_seconds / 20.0
                )
            if gain <= 1.0e-9:
                if capture is not None:
                    assert selection_index is not None
                    capture.finalize_selection(
                        selection_index,
                        wrapper_outcome={
                            "schema_version": 1,
                            "kind": "dedicated_sfz_wrapper_outcome",
                            "phase": selection_phase,
                            "articulation": articulation,
                            "layer_index": layer_index,
                            "wrapper_role_sha256": (
                                wrapper_role_sha256
                            ),
                            "event_sequence": event.sequence,
                            "target_midi": target_midi,
                            "velocity": velocity,
                            "key_velocity_match": True,
                            "wrapper_velocity_gain": gain,
                            "route_committed": False,
                            "committed_amplitude": None,
                            "final_status": (
                                "discarded_wrapper_gain_threshold"
                            ),
                        },
                    )
                del layer.engine.voices[internal_id]
                continue
            voice.amplitude *= gain
            if capture is not None:
                assert selection_index is not None
                capture.finalize_selection(
                    selection_index,
                    wrapper_outcome={
                        "schema_version": 1,
                        "kind": "dedicated_sfz_wrapper_outcome",
                        "phase": selection_phase,
                        "articulation": articulation,
                        "layer_index": layer_index,
                        "wrapper_role_sha256": wrapper_role_sha256,
                        "event_sequence": event.sequence,
                        "target_midi": target_midi,
                        "velocity": velocity,
                        "key_velocity_match": True,
                        "wrapper_velocity_gain": gain,
                        "route_committed": True,
                        "committed_amplitude": voice.amplitude,
                        "final_status": (
                            "retained_attack_voice"
                            if selection_phase == "note_on_attack"
                            else "retained_release_trigger_voice"
                        ),
                    },
                )
            triggered.append(
                (
                    _RoutedVoice(
                        layer_index,
                        internal_id,
                        voice.region.loop_mode == "one_shot",
                    ),
                    metadata,
                )
            )
        return triggered

    def _choke(self, articulation: str, group: str) -> None:
        runtime = self.articulations[articulation]
        victims = [
            (key, metadata)
            for key, metadata in self._voice_groups.items()
            if key[0] == articulation and metadata.group == group
        ]
        victim_keys = {key for key, _ in victims}
        for key, metadata in victims:
            _, layer_index, internal_id = key
            seconds = (
                max(0.001, metadata.off_time)
                if metadata.off_time is not None
                else self.choke_seconds
            )
            runtime.attack_layers[layer_index].engine.release_note(
                internal_id, release_seconds=seconds
            )
            self._voice_groups.pop(key, None)
        if victim_keys:
            for public_id, route in tuple(self.routes.items()):
                if route.articulation != articulation:
                    continue
                remaining = tuple(
                    voice
                    for voice in route.voices
                    if (articulation, voice.layer_index, voice.internal_note_id)
                    not in victim_keys
                )
                if remaining:
                    route.voices = remaining
                else:
                    del self.routes[public_id]

    def _start_release_trigger(
        self,
        route: _NoteRoute,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        release_velocity: float,
    ) -> None:
        release_layers = self.articulations[route.articulation].release_layers
        if not release_layers:
            return
        payload = dict(route.playback_payload)
        payload["velocity"] = release_velocity
        held_seconds = max(
            0.0, (event.sample - route.started_sample) / self.sample_rate
        )
        self._trigger_layers(
            release_layers,
            payload,
            event,
            tuning,
            articulation=route.articulation,
            selection_phase="note_off_release_trigger",
            held_seconds=held_seconds,
        )

    def _release_route(
        self,
        public_note_id: int,
        route: _NoteRoute,
        event: PerformanceEvent,
        tuning: EqualTemperament,
        release_velocity: float,
    ) -> None:
        runtime = self.articulations[route.articulation]
        for voice in route.voices:
            engine = runtime.attack_layers[voice.layer_index].engine
            if (
                runtime.release_override_seconds is not None
                and not voice.one_shot
            ):
                engine.release_note(
                    voice.internal_note_id,
                    release_seconds=runtime.release_override_seconds,
                )
            else:
                engine.handle_event(
                    PerformanceEvent(
                        event.sample,
                        event.sequence,
                        "note_off",
                        {
                            "note_id": voice.internal_note_id,
                            "release_velocity": release_velocity,
                        },
                    ),
                    tuning,
                )
            if not voice.one_shot:
                self._voice_groups.pop(
                    (
                        route.articulation,
                        voice.layer_index,
                        voice.internal_note_id,
                    ),
                    None,
                )
        self._start_release_trigger(route, event, tuning, release_velocity)
        self.routes.pop(public_note_id, None)

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in self.articulations:
                choices = ", ".join(sorted(self.articulations))
                raise ValueError(
                    f"unsupported dedicated SFZ articulation {name!r}; choose from {choices}"
                )
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            if name == "expression":
                self.expression_target = value
            elif name == "modulation":
                self.modulation_target = value
            elif name == "sustain_pedal":
                previous = self.sustain_pedal
                self.sustain_pedal = value
                if previous >= 0.5 and value < 0.5:
                    for public_id, route in tuple(self.routes.items()):
                        if route.pending_release_velocity is not None:
                            self._release_route(
                                public_id,
                                route,
                                event,
                                tuning,
                                route.pending_release_velocity,
                            )
            return

        if event.type == "note_on":
            public_id = int(event.payload["note_id"])
            if public_id in self.routes:
                raise ValueError(f"dedicated SFZ note_id {public_id} is already active")
            runtime = self.articulations[self.articulation]
            payload = self._playback_payload(event, tuning)
            triggered = self._trigger_layers(
                runtime.attack_layers,
                payload,
                event,
                tuning,
                articulation=self.articulation,
                selection_phase="note_on_attack",
            )
            if not triggered:
                raise ValueError(
                    f"dedicated SFZ articulation {self.articulation!r} has no "
                    f"region for MIDI {payload['midi_note']} at velocity "
                    f"{payload['velocity']}"
                )
            for _, metadata in triggered:
                if metadata.off_by is not None:
                    self._choke(self.articulation, metadata.off_by)
            for voice, metadata in triggered:
                if metadata.group is not None:
                    self._voice_groups[
                        (
                            self.articulation,
                            voice.layer_index,
                            voice.internal_note_id,
                        )
                    ] = metadata
            self.routes[public_id] = _NoteRoute(
                self.articulation,
                tuple(voice for voice, _ in triggered),
                payload,
                float(event.payload["velocity"]),
                event.sample,
            )
            return

        if event.type == "note_off":
            public_id = int(event.payload["note_id"])
            route = self.routes.get(public_id)
            if route is None:
                return
            # SFZ ``trigger=release`` is selected and scaled with the
            # corresponding note-on velocity, not MIDI note-off velocity.
            # The public protocol may carry release_velocity for backends
            # which model key-release speed, but it must not rewrite an SFZ
            # release region's velocity layer.
            release_velocity = route.velocity
            if self.sustain_pedal >= 0.5 and not route.one_shot:
                route.pending_release_velocity = release_velocity
            else:
                self._release_route(
                    public_id, route, event, tuning, release_velocity
                )

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._control_coefficient
        self.modulation += (
            self.modulation_target - self.modulation
        ) * self._control_coefficient
        left = 0.0
        right = 0.0
        for name, runtime in self.articulations.items():
            for layer in runtime.attack_layers:
                attack_left, attack_right = layer.engine.render_frame()
                left += attack_left
                right += attack_right
            for layer in runtime.release_layers:
                release_left, release_right = layer.engine.render_frame()
                left += release_left
                right += release_right
            for key in tuple(self._voice_groups):
                if key[0] != name:
                    continue
                _, layer_index, internal_id = key
                if internal_id not in runtime.attack_layers[layer_index].engine.voices:
                    del self._voice_groups[key]
        expression = self.expression**self.expression_exponent
        modulation = self.modulation_floor + (1.0 - self.modulation_floor) * (
            self.modulation**self.modulation_exponent
        )
        return left * expression * modulation, right * expression * modulation

    @property
    def active_voice_count(self) -> int:
        return sum(
            sum(layer.engine.active_voice_count for layer in runtime.attack_layers)
            + sum(layer.engine.active_voice_count for layer in runtime.release_layers)
            for runtime in self.articulations.values()
        )


def create_dedicated_sfz(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return DedicatedSfzInstrument(sample_rate, manifest, base_directory)


__all__ = [
    "DedicatedSfzDocument",
    "DedicatedSfzInstrument",
    "DedicatedSfzRegion",
    "DedicatedSfzRegionMetadata",
    "create_dedicated_sfz",
    "dedicated_regions_to_manifest",
    "parse_dedicated_sfz",
    "preprocess_sfz",
]
