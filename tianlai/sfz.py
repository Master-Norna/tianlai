from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


_HEADER = re.compile(r"<(control|global|master|group|region)>", re.IGNORECASE)
# opcode 的**起点**。SFZ 里 `sample=` 的值可以含空格(真实音源常见,如
# `libs/SSO/Samples/1st Violins/...`),所以值不能按空白切断:它一直延伸到
# **下一个 opcode 开始**或行尾。按起点切分正好同时满足两种情况——普通
# opcode 的值本来就不含空格,切出来不变;含空格的路径则完整保留。
_OPCODE_START = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=")
_NOTE = re.compile(r"^([A-Ga-g])([#b]?)(-?\d+)$")


def _parse_opcodes(text: str) -> list[tuple[str, str]]:
    """把一段 SFZ 文本切成 (opcode, 值);值可含空格,延伸到下一个 opcode。"""

    matches = list(_OPCODE_START.finditer(text))
    pairs: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end].strip().strip('"')
        pairs.append((match.group(1).lower(), value))
    return pairs


@dataclass(frozen=True, slots=True)
class SfzRegion:
    values: dict[str, str]


def note_number(value: str | int | float) -> float:
    try:
        return float(value)
    except ValueError:
        match = _NOTE.match(str(value))
        if match is None:
            raise ValueError(f"invalid SFZ note: {value!r}") from None
        letter, accidental, octave_text = match.groups()
        semitone = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}[
            letter.upper()
        ]
        if accidental == "#":
            semitone += 1
        elif accidental == "b":
            semitone -= 1
        return float((int(octave_text) + 1) * 12 + semitone)


def parse_sfz(path: str | Path) -> tuple[SfzRegion, ...]:
    """Parse the conservative SFZ subset used for sample-region mapping."""

    source_path = Path(path)
    text = source_path.read_text(encoding="utf-8-sig", errors="replace")
    global_values: dict[str, str] = {}
    group_values: dict[str, str] = {}
    current_kind = ""
    current_values: dict[str, str] | None = None
    regions: list[SfzRegion] = []

    def finish_region() -> None:
        nonlocal current_values
        if current_kind == "region" and current_values is not None:
            regions.append(SfzRegion(dict(current_values)))
            current_values = None

    for raw_line in text.splitlines():
        line = raw_line.split("//", 1)[0].strip()
        if not line or line.startswith("#"):
            continue
        position = 0
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
            if current_values is None:
                continue
            for key, value in _parse_opcodes(opcode_text):
                current_values[key] = value

    finish_region()
    return tuple(regions)


def regions_to_manifest(
    sfz_path: str | Path,
    *,
    use_embedded_loops: bool,
    trigger: str | None = "attack",
) -> list[dict[str, Any]]:
    """Convert parsed SFZ regions into Tianlai's sample-region format."""

    sfz_path = Path(sfz_path).resolve()
    converted: list[dict[str, Any]] = []
    for index, region in enumerate(parse_sfz(sfz_path)):
        values = region.values
        region_trigger = values.get("trigger", "attack").lower()
        if trigger is not None and region_trigger != trigger.lower():
            continue
        sample_name = values.get("sample")
        if not sample_name:
            continue
        sample_path = (sfz_path.parent / sample_name.replace("\\", "/")).resolve()
        root_value = values.get("pitch_keycenter", values.get("key"))
        if root_value is None:
            raise ValueError(f"SFZ region {index} has no pitch_keycenter: {sfz_path}")
        root_midi = note_number(root_value)
        key_min = note_number(values.get("lokey", values.get("key", root_value)))
        key_max = note_number(values.get("hikey", values.get("key", root_value)))
        tune_correction = float(values.get("tune", 0.0))
        item: dict[str, Any] = {
            "sample": str(sample_path),
            "root_midi": root_midi,
            "measured_tuning_cents": -tune_correction,
            "key_min": key_min,
            "key_max": key_max,
            "velocity_min": max(0.0, (float(values.get("lovel", 0.0)) - 0.5) / 127.0),
            "velocity_max": min(1.0, (float(values.get("hivel", 127.0)) + 0.5) / 127.0),
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
        raise ValueError(f"SFZ contains no playable regions: {sfz_path}")
    return converted
