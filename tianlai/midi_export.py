"""Explicitly lossy score-to-MIDI export for external notation editors.

Standard MIDI can preserve notes, tempo, meter and velocity, but it cannot
faithfully carry Tianlai's stable event identities, phrases, arbitrary
articulations, roster mix policy or microtonal notation.  The exporter
therefore validates the score, reports every unsupported semantic category,
and requires ``allow_lossy=True`` before crossing that boundary.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import struct
from typing import Any

from .atomic_publish import _publish_bytes_atomic
from .canonical_json import canonical_json_sha256 as _canonical_sha256
from .conductor import velocity_for_dynamic
from .score import parse_score_document
from .score_time import validate_score_time_coordinates


DIVISION = 480
_MELODIC_CHANNELS = tuple(channel for channel in range(16) if channel != 9)

# General MIDI program numbers are zero-based.  The mapping is for editor
# previews only; it never claims that a GM patch is Tianlai's real instrument.
_GM_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("击弦古钢琴", 6),
    ("羽管键琴", 6),
    ("钢片琴", 8),
    ("音乐盒", 10),
    ("颤音琴", 11),
    ("马林巴", 12),
    ("木琴", 13),
    ("编钟", 14),
    ("管钟", 14),
    ("钢琴", 0),
    ("竖琴", 46),
    ("定音鼓", 47),
    ("小提琴", 40),
    ("中提琴", 41),
    ("大提琴", 42),
    ("低音提琴", 43),
    ("弦乐合奏", 48),
    ("小号", 56),
    ("长号", 57),
    ("大号", 58),
    ("圆号", 60),
    ("双簧管", 68),
    ("英国管", 69),
    ("巴松", 70),
    ("单簧管", 71),
    ("短笛", 72),
    ("长笛", 73),
    ("竖笛", 74),
    ("排箫", 75),
    ("piccolo", 72),
    ("flute", 73),
    ("recorder", 74),
    ("clarinet", 71),
    ("oboe", 68),
    ("english horn", 69),
    ("bassoon", 70),
    ("violin", 40),
    ("viola", 41),
    ("cello", 42),
    ("contrabass", 43),
    ("strings", 48),
    ("trumpet", 56),
    ("trombone", 57),
    ("tuba", 58),
    ("french horn", 60),
    ("piano", 0),
)
_DRUM_KEYWORDS = (
    "鼓",
    "镲",
    "打击",
    "percussion",
    "drum",
    "cymbal",
)


class MidiExportLossError(ValueError):
    """The score contains semantics that require explicit lossy approval."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        codes = ", ".join(
            sorted(
                {
                    str(item["code"])
                    for item in report["losses"]
                    if item["blocking"]
                }
            )
        )
        super().__init__(
            "MIDI 无法无损表达该乐谱；检查 loss report 后显式传 "
            f"--allow-lossy。阻断项: {codes}"
        )


def _vlq(value: int) -> bytes:
    if value < 0:
        raise ValueError("MIDI delta time must not be negative")
    encoded = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        encoded.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(encoded)


def _track_chunk(events: list[tuple[int, int, bytes]]) -> bytes:
    events.sort(key=lambda item: (item[0], item[1]))
    body = bytearray()
    previous = 0
    for tick, _priority, payload in events:
        body += _vlq(tick - previous)
        body += payload
        previous = tick
    body += b"\x00\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(body)) + bytes(body)


def _meta_text(kind: int, text: str) -> bytes:
    encoded = text.encode("utf-8")
    return bytes((0xFF, kind)) + _vlq(len(encoded)) + encoded


def _program_for(text: str) -> int | None:
    lowered = text.casefold()
    for keyword, program in _GM_KEYWORDS:
        if keyword.casefold() in lowered:
            return program
    return None


def _looks_percussive(text: str) -> bool:
    lowered = text.casefold()
    return any(keyword.casefold() in lowered for keyword in _DRUM_KEYWORDS)


def _roster_by_part(
    roster: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    if roster is None:
        return {}
    if not isinstance(roster, dict):
        raise ValueError("roster must be an object")
    assignments = roster.get("assignments")
    if not isinstance(assignments, list):
        raise ValueError("roster.assignments must be an array")
    result: dict[str, list[dict[str, Any]]] = {}
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            raise ValueError(f"roster.assignments[{index}] must be an object")
        part = assignment.get("part")
        if not isinstance(part, str) or not part.strip():
            raise ValueError(
                f"roster.assignments[{index}].part must be a non-empty string"
            )
        result.setdefault(part, []).append(assignment)
    return result


def _atomic_bytes(path: Path, payload: bytes, *, overwrite: bool) -> None:
    try:
        _publish_bytes_atomic(path, payload, overwrite=overwrite)
    except FileExistsError as exc:
        if overwrite:
            raise
        raise ValueError(
            f"输出已存在，默认拒绝覆盖: {path};确认后传 --overwrite"
        ) from exc


def _loss(
    losses: list[dict[str, Any]],
    code: str,
    message: str,
    *,
    blocking: bool,
    part_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    item: dict[str, Any] = {
        "code": code,
        "blocking": blocking,
        "message": message,
    }
    if part_id is not None:
        item["part_id"] = part_id
    if details is not None:
        item["details"] = details
    losses.append(item)


def build_midi(
    score: dict[str, Any],
    *,
    roster: dict[str, Any] | None = None,
    allow_lossy: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """Build an SMF format-1 payload and its machine-readable loss report."""

    parsed = parse_score_document(score)
    validate_score_time_coordinates(parsed)
    roster_parts = _roster_by_part(roster)
    score_part_ids = {part.id for part in parsed.parts}
    unknown_roster_parts = sorted(set(roster_parts) - score_part_ids)
    if unknown_roster_parts:
        raise ValueError(
            "roster references unknown score parts: "
            + ", ".join(unknown_roster_parts)
        )

    losses: list[dict[str, Any]] = []
    if parsed.has_stable_event_identity:
        _loss(
            losses,
            "stable_event_ids_not_representable",
            "标准 MIDI 不保证保存 Tianlai event_id；重新导入时身份可能重建",
            blocking=True,
        )
    if any(part.phrases for part in parsed.parts):
        _loss(
            losses,
            "phrases_not_representable",
            "score phrases 不进入标准 MIDI 音符轨",
            blocking=True,
        )
    tuning = parsed.tuning
    if (
        str(tuning.get("temperament", "equal")) != "equal"
        or float(tuning.get("a4_hz", 440.0)) != 440.0
    ):
        _loss(
            losses,
            "score_tuning_not_representable",
            "标准 MIDI 音符键位不携带 Tianlai 的非默认调律",
            blocking=True,
            details={"tuning": dict(tuning)},
        )
    if roster is not None and roster.get("drop_parts"):
        _loss(
            losses,
            "roster_drop_parts_not_applied",
            "MIDI 导出不会执行 roster.drop_parts，编辑副本仍包含这些 score 声部",
            blocking=True,
            details={"drop_parts": roster.get("drop_parts")},
        )

    meta: list[tuple[int, int, bytes]] = [
        (0, 0, _meta_text(0x03, parsed.title))
    ]
    previous_meter: tuple[int, int] | None = None
    tempo_position_quantized = False
    tempo_value_quantized = False
    tempo_value_clamped = False
    meter_numerator_clamped = False
    for entry in parsed.tempo_map.entries:
        quarter = parsed.tempo_map.quarter_at(entry.bar, entry.beat)
        raw_tick = quarter * DIVISION
        tick = round(raw_tick)
        tempo_position_quantized = tempo_position_quantized or raw_tick != tick
        raw_microseconds = 60_000_000 / entry.bpm
        microseconds = round(raw_microseconds)
        tempo_value_quantized = (
            tempo_value_quantized or raw_microseconds != microseconds
        )
        if microseconds > 0xFF_FF_FF:
            tempo_value_clamped = True
            microseconds = 0xFF_FF_FF
        meta.append(
            (tick, 1, b"\xff\x51\x03" + microseconds.to_bytes(3, "big"))
        )
        meter = (entry.beats_per_bar, entry.beat_unit)
        if entry.changes_meter and meter != previous_meter:
            denominator_power = entry.beat_unit.bit_length() - 1
            midi_numerator = min(255, entry.beats_per_bar)
            meter_numerator_clamped = (
                meter_numerator_clamped
                or midi_numerator != entry.beats_per_bar
            )
            meta.append(
                (
                    tick,
                    1,
                    bytes(
                        (
                            0xFF,
                            0x58,
                            0x04,
                            midi_numerator,
                            denominator_power,
                            24,
                            8,
                        )
                    ),
                )
            )
            previous_meter = meter
    if tempo_position_quantized:
        _loss(
            losses,
            "tempo_position_quantized_to_480_ppq",
            "速度事件位置已量化到 MIDI 的每四分音符 480 tick",
            blocking=True,
        )
    if tempo_value_quantized:
        _loss(
            losses,
            "tempo_value_quantized_to_integer_microseconds",
            "BPM 已量化为 MIDI 的整数 microseconds-per-quarter",
            blocking=True,
        )
    if tempo_value_clamped:
        _loss(
            losses,
            "tempo_clamped_to_midi_range",
            "过慢速度超出 MIDI 三字节 tempo 上限，已夹到可表示的最慢速度",
            blocking=True,
        )
    if meter_numerator_clamped:
        _loss(
            losses,
            "meter_numerator_clamped_to_midi_range",
            "拍号分子超出 MIDI 单字节范围，已夹到 255",
            blocking=True,
        )
    tracks = [_track_chunk(meta)]
    melodic_channel_index = 0
    part_reports: list[dict[str, Any]] = []

    for part in parsed.parts:
        if not part.notes:
            part_reports.append(
                {
                    "part_id": part.id,
                    "note_count": 0,
                    "track_written": False,
                }
            )
            continue
        assignments = roster_parts.get(part.id, [])
        if len(assignments) > 1:
            _loss(
                losses,
                "split_assignment_not_representable",
                "一个 score 声部被拆给多个执行器，单条 MIDI 轨无法表达该路由",
                blocking=True,
                part_id=part.id,
            )
        assignment = assignments[0] if len(assignments) == 1 else {}
        instrument = assignment.get("instrument")
        kit = assignment.get("kit")
        if isinstance(instrument, str) and instrument.strip():
            _loss(
                losses,
                "dedicated_instrument_approximated_by_gm",
                "Tianlai 专用乐器只能近似映射为 General MIDI program",
                blocking=True,
                part_id=part.id,
                details={"instrument": instrument},
            )
        if isinstance(kit, dict) and kit:
            _loss(
                losses,
                "kit_routing_not_representable",
                "逐符头 Tianlai kit 路由不会进入单一 MIDI 打击通道",
                blocking=True,
                part_id=part.id,
                details={"noteheads": sorted(str(key) for key in kit)},
            )
        unrepresented_roster_fields: set[str] = set()
        for roster_assignment in assignments:
            for field, default in (
                ("gain_db", 0),
                ("pan", 0),
                ("transpose", 0),
                ("dynamic_compression", 0),
                ("duration_scale", 1),
            ):
                if (
                    field in roster_assignment
                    and roster_assignment[field] != default
                ):
                    unrepresented_roster_fields.add(field)
            for field in (
                "executor_id",
                "gain_automation",
                "seat",
                "articulation_map",
                "overrides",
            ):
                if roster_assignment.get(field):
                    unrepresented_roster_fields.add(field)
            if "articulation_auto" in roster_assignment:
                unrepresented_roster_fields.add("articulation_auto")
        if unrepresented_roster_fields:
            _loss(
                losses,
                "roster_execution_semantics_not_representable",
                "roster 的执行或混音参数不会进入标准 MIDI 编辑副本",
                blocking=True,
                part_id=part.id,
                details={"fields": sorted(unrepresented_roster_fields)},
            )
        descriptor = " ".join(
            value
            for value in (
                part.id,
                part.name,
                instrument if isinstance(instrument, str) else "",
            )
            if value
        )
        percussion = isinstance(kit, dict) or _looks_percussive(descriptor)
        if percussion:
            channel = 9
            program = None
        else:
            if melodic_channel_index >= len(_MELODIC_CHANNELS):
                _loss(
                    losses,
                    "midi_channel_limit_exceeded",
                    "同时可分配独立音色的旋律声部超过 15 个",
                    blocking=True,
                    part_id=part.id,
                )
                channel = _MELODIC_CHANNELS[-1]
            else:
                channel = _MELODIC_CHANNELS[melodic_channel_index]
            melodic_channel_index += 1
            program = _program_for(descriptor)
            if program is None:
                program = 0
                _loss(
                    losses,
                    "gm_program_defaulted",
                    "没有可靠的 General MIDI 对应音色；编辑器预览退回钢琴",
                    blocking=False,
                    part_id=part.id,
                )

        events: list[tuple[int, int, bytes]] = [
            (0, 0, _meta_text(0x03, part.name or part.id))
        ]
        if program is not None:
            events.append((0, 1, bytes((0xC0 | channel, program))))

        has_articulation = False
        has_tie = False
        has_voice_identity = False
        has_microtone = False
        has_out_of_range_pitch = False
        has_timing_quantization = False
        has_velocity_quantization = False
        for note in part.notes:
            start_quarter = parsed.tempo_map.quarter_at(
                note.bar,
                note.beat,
            )
            meter = parsed.tempo_map.entry_at_bar(note.bar)
            duration_quarters = (
                note.duration_beats * meter.quarters_per_beat
            )
            raw_start = start_quarter * DIVISION
            raw_length = duration_quarters * DIVISION
            start = round(raw_start)
            length = max(1, round(raw_length))
            has_timing_quantization = has_timing_quantization or any(
                value != rounded
                for value, rounded in (
                    (raw_start, start),
                    (raw_length, length),
                )
            )
            if note.midi != round(note.midi):
                has_microtone = True
            key = round(note.midi)
            if not 0 <= key <= 127:
                has_out_of_range_pitch = True
            key = min(127, max(0, key))
            dynamic = note.dynamic or part.default_dynamic
            velocity = (
                note.velocity
                if note.velocity is not None
                else velocity_for_dynamic(dynamic)
            )
            midi_velocity = min(127, max(1, round(velocity * 127)))
            has_velocity_quantization = (
                has_velocity_quantization
                or velocity * 127 != midi_velocity
            )
            events.append(
                (start, 3, bytes((0x90 | channel, key, midi_velocity)))
            )
            events.append(
                (
                    start + length,
                    2,
                    bytes((0x80 | channel, key, 0)),
                )
            )
            has_articulation = has_articulation or (
                note.articulation is not None
                or part.default_articulation is not None
            )
            has_tie = has_tie or note.tie
            has_voice_identity = has_voice_identity or (
                note.staff is not None or note.voice is not None
            )
        if has_timing_quantization:
            _loss(
                losses,
                "note_timing_quantized_to_480_ppq",
                "音符起点或时值已量化到 MIDI 的每四分音符 480 tick",
                blocking=True,
                part_id=part.id,
            )
        if has_velocity_quantization:
            _loss(
                losses,
                "velocity_quantized_to_midi_7bit",
                "音符力度已量化到 MIDI 的 1..127 整数 velocity",
                blocking=True,
                part_id=part.id,
            )
        if has_articulation:
            _loss(
                losses,
                "articulation_not_representable",
                "Tianlai 奏法记号不会进入普通 MIDI 音符事件",
                blocking=True,
                part_id=part.id,
            )
        if has_tie:
            _loss(
                losses,
                "tie_not_representable",
                "连音身份不会进入普通 MIDI 音符事件",
                blocking=True,
                part_id=part.id,
            )
        if has_voice_identity:
            _loss(
                losses,
                "staff_voice_identity_not_representable",
                "普通 MIDI 音符事件不保存 MusicXML staff/voice 身份；"
                "重新导入后多声部连音归属可能需要人工复核",
                blocking=True,
                part_id=part.id,
            )
        if has_microtone:
            _loss(
                losses,
                "microtonal_pitch_quantized",
                "微分音被量化到最近的 MIDI 半音",
                blocking=True,
                part_id=part.id,
            )
        if has_out_of_range_pitch:
            _loss(
                losses,
                "pitch_clamped_to_midi_range",
                "超出 0..127 的音高被截到标准 MIDI 范围",
                blocking=True,
                part_id=part.id,
            )
        tracks.append(_track_chunk(events))
        part_reports.append(
            {
                "part_id": part.id,
                "note_count": len(part.notes),
                "track_written": True,
                "channel_1based": channel + 1,
                "percussion": percussion,
                "gm_program_0based": program,
            }
        )

    report: dict[str, Any] = {
        "kind": "tianlai.midi_export_report",
        "schema_version": 1,
        "ok": not any(item["blocking"] for item in losses) or allow_lossy,
        "allow_lossy": allow_lossy,
        "source_score_sha256": _canonical_sha256(score),
        "source_roster_sha256": (
            _canonical_sha256(roster) if roster is not None else None
        ),
        "track_count": len(tracks),
        "parts": part_reports,
        "losses": losses,
        "blocking_loss_count": sum(
            1 for item in losses if item["blocking"]
        ),
    }
    if report["blocking_loss_count"] and not allow_lossy:
        raise MidiExportLossError(report)
    header = b"MThd" + struct.pack(
        ">IHHH",
        6,
        1,
        len(tracks),
        DIVISION,
    )
    payload = header + b"".join(tracks)
    report["midi_bytes"] = len(payload)
    report["midi_sha256"] = hashlib.sha256(payload).hexdigest()
    return payload, report


def export_midi(
    score: dict[str, Any],
    output: str | Path,
    *,
    roster: dict[str, Any] | None = None,
    allow_lossy: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    payload, report = build_midi(
        score,
        roster=roster,
        allow_lossy=allow_lossy,
    )
    target = Path(output)
    _atomic_bytes(target, payload, overwrite=overwrite)
    return {
        **report,
        "output": str(target.resolve()),
    }


__all__ = [
    "DIVISION",
    "MidiExportLossError",
    "build_midi",
    "export_midi",
]
