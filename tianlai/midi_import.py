"""Standard MIDI File import: a recorded performance becomes a score document.

Written against the SMF specification rather than pulled in as a dependency.
The format is small and fully specified, and parsing it here keeps the import
path deterministic and auditable like everything else in the project.

Two decisions are worth stating, because both are places where a careless
importer silently rewrites the music:

* **Tempo changes are kept where they are.**  A rubato passage speeds up and
  slows down inside a bar; snapping those changes to bar lines would visibly
  alter the timing.  Meter changes are still forced onto downbeats, because
  that is a musical fact rather than a limitation.
* **Per-note velocities are preserved exactly.**  A MIDI file records what a
  player actually did.  Folding that into eight dynamic marks and letting the
  conductor re-expand it would throw away real information and substitute
  invented information.  Each note therefore carries both the nearest dynamic
  mark (so the score stays readable and editable) and its exact velocity,
  which takes precedence.
* **Mixer messages remain evidence, not hidden policy.**  Program Change and
  CC7/CC10/CC11 are retained in a hash-bound roster draft.  Only a constant
  CC10 value that is already present when the part starts can become a static
  pan candidate.  MIDI volume/expression values have no portable dB law, so
  they are never silently converted into Tianlai gain automation.
"""

from __future__ import annotations

import copy
from collections import deque
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
from typing import Any

from .canonical_json import canonical_json_sha256 as _canonical_json_sha256
from .score import pitch_name, upgrade_legacy_score_to_v1


_DEFAULT_TEMPO_US = 500_000  # SMF 默认 120 BPM
_DYNAMIC_STEPS = (
    (0.16, "ppp"),
    (0.27, "pp"),
    (0.39, "p"),
    (0.51, "mp"),
    (0.64, "mf"),
    (0.77, "f"),
    (0.90, "ff"),
    (1.01, "fff"),
)
GM_PERCUSSION_CHANNEL = 9
_MAX_MIDI_BYTES = 64 * 1024 * 1024
_MAX_MIDI_TRACKS = 1024
_MAX_TRACK_EVENTS = 1_000_000
_MAX_IMPORTED_NOTES = 250_000


@dataclass(slots=True)
class _RawNote:
    tick: int
    start_event_index: int
    duration_ticks: int
    midi: int
    velocity: int


@dataclass(slots=True)
class _RawController:
    tick: int
    track_event_index: int
    controller: int
    value: int


@dataclass(slots=True)
class _RawProgramChange:
    tick: int
    track_event_index: int
    program: int


@dataclass(slots=True)
class _RawPart:
    track: int
    channel: int
    name: str
    notes: list[_RawNote] = field(default_factory=list)
    controllers: list[_RawController] = field(default_factory=list)
    program_changes: list[_RawProgramChange] = field(default_factory=list)
    unhandled_messages: set[str] = field(default_factory=set)

    @property
    def is_percussion(self) -> bool:
        return self.channel == GM_PERCUSSION_CHANNEL


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What the importer found, so a roster can be written against it."""

    title: str
    source_midi_sha256: str
    source_midi_byte_length: int
    midi_format: int
    track_count: int
    score_canonical_sha256: str
    ticks_per_quarter: int
    parts: tuple[dict[str, Any], ...]
    tempo_changes: int
    meter_changes: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source_midi_sha256": self.source_midi_sha256,
            "source_midi_byte_length": self.source_midi_byte_length,
            "midi_format": self.midi_format,
            "track_count": self.track_count,
            "score_canonical_sha256": self.score_canonical_sha256,
            "ticks_per_quarter": self.ticks_per_quarter,
            "tempo_changes": self.tempo_changes,
            "meter_changes": self.meter_changes,
            "warnings": list(self.warnings),
            "parts": list(self.parts),
        }


class _Reader:
    __slots__ = ("data", "position")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def byte(self) -> int:
        if self.position >= len(self.data):
            raise ValueError("MIDI 数据在读取中意外结束")
        value = self.data[self.position]
        self.position += 1
        return value

    def read(self, count: int) -> bytes:
        if self.position + count > len(self.data):
            raise ValueError("MIDI 数据在读取中意外结束")
        chunk = self.data[self.position : self.position + count]
        self.position += count
        return chunk

    def integer(self, count: int) -> int:
        return int.from_bytes(self.read(count), "big")

    def variable(self) -> int:
        value = 0
        for _ in range(4):
            byte = self.byte()
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return value
        raise ValueError("MIDI 变长数值超过 4 字节")


_FILENAME_HOSTILE = set('/\\:*?"<>|')


def _clean_name(raw: bytes) -> str:
    """Normalise a track name into something usable as a part id.

    Track names routinely carry trailing NULs, and exported arrangements
    happily put ``|`` or ``/`` in them.  A part id becomes an executor id and
    then a stem filename, so both classes of character have to go: an
    invisible control character silently fails to match the roster entry a
    human typed, and a path separator would escape the output directory.
    """

    text = raw.decode("utf-8", errors="replace")
    cleaned = "".join(
        " " if character in _FILENAME_HOSTILE else character
        for character in text
        if character.isprintable()
    )
    return " ".join(cleaned.split())


def _nearest_dynamic(velocity: float) -> str:
    for threshold, mark in _DYNAMIC_STEPS:
        if velocity < threshold:
            return mark
    return "fff"


@dataclass(slots=True)
class _MeterSegment:
    start_tick: int
    start_bar: int
    ticks_per_bar: float
    numerator: int
    denominator: int

    @property
    def ticks_per_beat(self) -> float:
        return self.ticks_per_bar / self.numerator


class _BarClock:
    """Converts absolute ticks into (bar, beat) under a changing meter."""

    def __init__(
        self, meters: list[tuple[int, int, int]], ticks_per_quarter: int
    ) -> None:
        if not meters or meters[0][0] != 0:
            meters = [(0, 4, 4), *meters]
        self.segments: list[_MeterSegment] = []
        for index, (tick, numerator, denominator) in enumerate(meters):
            ticks_per_bar = numerator * (4.0 / denominator) * ticks_per_quarter
            if index == 0:
                start_bar = 1
            else:
                previous = self.segments[-1]
                span = tick - previous.start_tick
                completed_bars = span / previous.ticks_per_bar
                rounded_bars = round(completed_bars)
                # The score contract cannot represent a time-signature change
                # inside a bar without changing the performed timeline.
                # Reject it instead of silently relabelling that tick as a
                # downbeat or rounding it to another position.
                if not math.isclose(
                    completed_bars,
                    rounded_bars,
                    rel_tol=0.0,
                    abs_tol=1e-6,
                ):
                    raise ValueError(
                        "MIDI 拍号变化不在小节线上："
                        f"tick={tick}，前一拍号起点={previous.start_tick}；"
                        "请在制谱软件中对齐拍号后重新导出"
                    )
                start_bar = previous.start_bar + int(rounded_bars)
            self.segments.append(
                _MeterSegment(tick, start_bar, ticks_per_bar, numerator, denominator)
            )

    def segment_at(self, tick: int) -> _MeterSegment:
        chosen = self.segments[0]
        for segment in self.segments:
            if segment.start_tick <= tick:
                chosen = segment
            else:
                break
        return chosen

    def position(self, tick: int) -> tuple[int, float]:
        segment = self.segment_at(tick)
        offset = tick - segment.start_tick
        bar_index = int(offset // segment.ticks_per_bar)
        remainder = offset - bar_index * segment.ticks_per_bar
        return segment.start_bar + bar_index, 1.0 + remainder / segment.ticks_per_beat

    def beats(self, tick: int, duration_ticks: int) -> float:
        return duration_ticks / self.segment_at(tick).ticks_per_beat


def _parse_track(
    reader: _Reader, track_index: int, length: int
) -> tuple[
    list[_RawPart],
    list[tuple[int, int]],
    list[tuple[int, int, int]],
    str,
    list[str],
]:
    end = reader.position + length
    if end > len(reader.data):
        raise ValueError(f"MIDI 轨道 {track_index} 的声明长度超过文件边界")
    tick = 0
    status = 0
    track_name = ""
    parts: dict[int, _RawPart] = {}
    # SMF note-off events do not carry a note identity.  A queue preserves
    # every repeated note-on instead of silently replacing an earlier note;
    # overlapping same-channel/same-pitch notes are paired FIFO and surfaced
    # as an explicit import warning below.
    open_notes: dict[
        tuple[int, int],
        deque[tuple[int, int, int]],
    ] = {}
    tempos: list[tuple[int, int]] = []
    meters: list[tuple[int, int, int]] = []
    overlapping_note_ons = 0
    unmatched_note_offs = 0
    synthesized_note_offs = 0
    ignored_meta_types: set[int] = set()
    sysex_events = 0
    end_of_track_seen = False
    bytes_after_end_of_track = 0
    track_event_index = 0

    def part_for(channel: int) -> _RawPart:
        if channel not in parts:
            parts[channel] = _RawPart(track=track_index, channel=channel, name="")
        return parts[channel]

    while reader.position < end:
        if track_event_index >= _MAX_TRACK_EVENTS:
            raise ValueError(
                f"MIDI 轨道 {track_index} 的事件数超过 "
                f"{_MAX_TRACK_EVENTS}"
            )
        current_event_index = track_event_index
        track_event_index += 1
        tick += reader.variable()
        byte = reader.byte()
        if byte == 0xFF:
            status = 0
            meta_type = reader.byte()
            payload = reader.read(reader.variable())
            if meta_type == 0x03 and not track_name:
                track_name = _clean_name(payload)
            elif meta_type == 0x51 and len(payload) == 3:
                microseconds = int.from_bytes(payload, "big")
                if microseconds == 0:
                    raise ValueError(
                        f"MIDI 轨道 {track_index} 含值为 0 的非法速度事件"
                    )
                tempos.append((tick, microseconds))
            elif meta_type == 0x58 and len(payload) >= 2:
                numerator = payload[0]
                denominator_exponent = payload[1]
                if numerator == 0 or denominator_exponent > 5:
                    raise ValueError(
                        f"MIDI 轨道 {track_index} 含当前无法表示的拍号"
                    )
                meters.append(
                    (tick, numerator, 1 << denominator_exponent)
                )
            elif meta_type == 0x2F:
                if payload:
                    raise ValueError(
                        f"MIDI 轨道 {track_index} 的 End-of-Track "
                        "事件长度必须为 0"
                    )
                end_of_track_seen = True
                bytes_after_end_of_track = end - reader.position
                break
            else:
                ignored_meta_types.add(meta_type)
            continue
        if byte in (0xF0, 0xF7):
            status = 0
            reader.read(reader.variable())
            sysex_events += 1
            continue
        if byte & 0x80:
            if not 0x80 <= byte <= 0xEF:
                raise ValueError(
                    f"MIDI 轨道 {track_index} 含不支持的状态字节 "
                    f"0x{byte:02X}"
                )
            status = byte
            data1 = reader.byte()
            if data1 & 0x80:
                raise ValueError(
                    f"MIDI 轨道 {track_index} 的事件 {current_event_index} "
                    "缺少合法的第一个数据字节"
                )
        else:
            # 运行状态:省略了状态字节,刚读到的其实是第一个数据字节。
            if not status:
                raise ValueError("MIDI 轨道以运行状态开头,缺少状态字节")
            data1 = byte
        command = status & 0xF0
        channel = status & 0x0F
        if command in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
            data2 = reader.byte()
            if data2 & 0x80:
                raise ValueError(
                    f"MIDI 轨道 {track_index} 的事件 {current_event_index} "
                    "缺少合法的第二个数据字节"
                )
        else:
            data2 = 0
        if command == 0x90 and data2 > 0:
            queue = open_notes.setdefault(
                (channel, data1),
                deque(),
            )
            if queue:
                overlapping_note_ons += 1
            queue.append((tick, data2, current_event_index))
        elif command == 0x80 or (command == 0x90 and data2 == 0):
            key = (channel, data1)
            queue = open_notes.get(key)
            if queue:
                started = queue.popleft()
                if not queue:
                    del open_notes[key]
                start_tick, velocity, start_event_index = started
                part_for(channel).notes.append(
                    _RawNote(
                        tick=start_tick,
                        start_event_index=start_event_index,
                        duration_ticks=max(1, tick - start_tick),
                        midi=data1,
                        velocity=velocity,
                    )
                )
            else:
                unmatched_note_offs += 1
        elif command == 0xB0 and data1 in (7, 10, 11, 42, 121):
            part_for(channel).controllers.append(
                _RawController(
                    tick=tick,
                    track_event_index=current_event_index,
                    controller=data1,
                    value=data2,
                )
            )
        elif command == 0xC0:
            part_for(channel).program_changes.append(
                _RawProgramChange(
                    tick=tick,
                    track_event_index=current_event_index,
                    program=data1,
                )
            )
        elif command == 0xB0:
            part_for(channel).unhandled_messages.add(f"cc_{data1}")
        elif command == 0xA0:
            part_for(channel).unhandled_messages.add("poly_aftertouch")
        elif command == 0xD0:
            part_for(channel).unhandled_messages.add("channel_pressure")
        elif command == 0xE0:
            part_for(channel).unhandled_messages.add("pitch_bend")
    # Malformed/exporter-truncated tracks occasionally omit note-offs.  Keep
    # the audible note up to the declared track end instead of dropping it,
    # but make that repair visible so the default project-import policy can
    # require creator acknowledgement.
    for (channel, midi), queue in open_notes.items():
        while queue:
            start_tick, velocity, start_event_index = queue.popleft()
            part_for(channel).notes.append(
                _RawNote(
                    tick=start_tick,
                    start_event_index=start_event_index,
                    duration_ticks=max(1, tick - start_tick),
                    midi=midi,
                    velocity=velocity,
                )
            )
            synthesized_note_offs += 1

    reader.position = end
    for part in parts.values():
        part.name = track_name
    warnings: list[str] = []
    if overlapping_note_ons:
        warnings.append(
            f"MIDI 轨道 {track_index} 有 {overlapping_note_ons} 次同通道同音重叠 "
            "note-on；SMF 不含音符身份，已按 FIFO 与后续 note-off 配对"
        )
    if unmatched_note_offs:
        warnings.append(
            f"MIDI 轨道 {track_index} 有 {unmatched_note_offs} 个找不到对应 "
            "note-on 的 note-off，已忽略"
        )
    if synthesized_note_offs:
        warnings.append(
            f"MIDI 轨道 {track_index} 有 {synthesized_note_offs} 个未关闭 "
            "note-on，已在轨道结束位置补齐 note-off"
        )
    if ignored_meta_types:
        rendered_types = ", ".join(
            f"0x{meta_type:02X}" for meta_type in sorted(ignored_meta_types)
        )
        warnings.append(
            f"MIDI 轨道 {track_index} 含当前未进入 score/roster 的 "
            f"meta 事件类型：{rendered_types}"
        )
    if sysex_events:
        warnings.append(
            f"MIDI 轨道 {track_index} 含 {sysex_events} 个未进入 "
            "score/roster 的 SysEx 事件"
        )
    if not end_of_track_seen:
        warnings.append(
            f"MIDI 轨道 {track_index} 缺少 End-of-Track 事件；"
            "已按声明轨道长度结束"
        )
    elif bytes_after_end_of_track:
        warnings.append(
            f"MIDI 轨道 {track_index} 的 End-of-Track 后仍有 "
            f"{bytes_after_end_of_track} 字节，已忽略"
        )
    return list(parts.values()), tempos, meters, track_name, warnings


def _select_timeline_events(
    events: list[tuple[int, ...]],
    *,
    label: str,
    warnings: list[str],
) -> list[tuple[int, ...]]:
    """Keep the first event at a tick and report conflicting duplicates."""

    selected: dict[int, tuple[int, ...]] = {}
    conflict_count = 0
    examples: list[str] = []
    for event in events:
        tick = event[0]
        previous = selected.get(tick)
        if previous is None:
            selected[tick] = event
            continue
        if previous[1:] == event[1:]:
            continue
        conflict_count += 1
        if len(examples) < 8:
            examples.append(
                f"tick {tick}: {previous[1:]} / {event[1:]}"
            )
    if conflict_count:
        suffix = "；示例 " + "，".join(examples) if examples else ""
        warnings.append(
            f"MIDI 同一 tick 有 {conflict_count} 组冲突{label}事件；"
            f"按轨道顺序保留首项{suffix}"
        )
    return [selected[tick] for tick in sorted(selected)]


def read_midi(path: str | Path) -> tuple[dict[str, Any], ImportReport]:
    """Parse a Standard MIDI File into a score document plus a report."""

    source_path = Path(path)
    size = source_path.stat().st_size
    if size > _MAX_MIDI_BYTES:
        raise ValueError(
            f"MIDI 文件超过 {_MAX_MIDI_BYTES // 1024 // 1024} MiB"
        )
    data = source_path.read_bytes()
    source_midi_sha256 = hashlib.sha256(data).hexdigest()
    reader = _Reader(data)
    if reader.read(4) != b"MThd":
        raise ValueError(f"不是标准 MIDI 文件(缺少 MThd 头):{path}")
    header_length = reader.integer(4)
    if header_length < 6:
        raise ValueError("MIDI MThd 头长度不能小于 6 字节")
    header_end = reader.position + header_length
    if header_end > len(data):
        raise ValueError("MIDI MThd 头的声明长度超过文件边界")
    midi_format = reader.integer(2)
    track_count = reader.integer(2)
    division = reader.integer(2)
    reader.position = header_end
    if division & 0x8000:
        raise ValueError("暂不支持 SMPTE 时基的 MIDI 文件;请导出为每四分音符 tick 的格式")
    ticks_per_quarter = division
    if ticks_per_quarter <= 0:
        raise ValueError("MIDI 时基必须为正")
    if midi_format not in (0, 1):
        raise ValueError(f"暂不支持 MIDI format {midi_format};请另存为 format 0 或 1")
    if track_count <= 0:
        raise ValueError("MIDI 文件必须至少声明一个轨道")
    if track_count > _MAX_MIDI_TRACKS:
        raise ValueError(
            f"MIDI 轨道数超过 {_MAX_MIDI_TRACKS}"
        )
    if midi_format == 0 and track_count != 1:
        raise ValueError("MIDI format 0 必须且只能声明一个轨道")

    raw_parts: list[_RawPart] = []
    tempos: list[tuple[int, int]] = []
    meters: list[tuple[int, int, int]] = []
    warnings: list[str] = []
    for track_index in range(track_count):
        marker = reader.read(4)
        length = reader.integer(4)
        if marker != b"MTrk":
            raise ValueError(
                f"MIDI 第 {track_index} 个轨道缺少 MTrk 标记"
            )
        (
            parts,
            track_tempos,
            track_meters,
            _,
            track_warnings,
        ) = _parse_track(reader, track_index, length)
        raw_parts.extend(parts)
        note_count = sum(
            len(part.notes) for part in raw_parts
        )
        if note_count > _MAX_IMPORTED_NOTES:
            raise ValueError(
                f"MIDI 可配对音符数超过 {_MAX_IMPORTED_NOTES}"
            )
        tempos.extend(track_tempos)
        meters.extend(track_meters)
        warnings.extend(track_warnings)

    if reader.position < len(data):
        warnings.append(
            f"MIDI 声明轨道结束后仍有 {len(data) - reader.position} 字节，"
            "当前未解析"
        )

    ignored_streams = [part for part in raw_parts if not part.notes]
    for part in ignored_streams:
        warnings.append(
            "忽略无音符 MIDI 流："
            f"track={part.track}, channel={part.channel + 1}；"
            "其 Program/CC 不能安全归属到任何 score 声部"
        )
    raw_parts = [part for part in raw_parts if part.notes]
    if not raw_parts:
        raise ValueError("这个 MIDI 文件里没有任何音符")

    tempos = [
        (int(item[0]), int(item[1]))
        for item in _select_timeline_events(
            tempos,
            label="速度",
            warnings=warnings,
        )
    ]
    meters = [
        (int(item[0]), int(item[1]), int(item[2]))
        for item in _select_timeline_events(
            meters,
            label="拍号",
            warnings=warnings,
        )
    ]
    clock = _BarClock(meters, ticks_per_quarter)

    # 速度与拍号合成一张表:拍号只落在小节线上,速度可以落在任意位置。
    entries: dict[tuple[int, float], dict[str, Any]] = {}
    for tick, numerator, denominator in meters:
        bar, beat = clock.position(tick)
        entries.setdefault((bar, 1.0), {"bar": bar, "beat": 1.0})
        entries[(bar, 1.0)]["beats_per_bar"] = numerator
        entries[(bar, 1.0)]["beat_unit"] = denominator
    for tick, microseconds in tempos:
        bar, beat = clock.position(tick)
        key = (bar, round(beat, 6))
        entries.setdefault(key, {"bar": bar, "beat": key[1]})
        entries[key]["bpm"] = round(60_000_000.0 / microseconds, 6)

    first = entries.setdefault((1, 1.0), {"bar": 1, "beat": 1.0})
    first.setdefault("bpm", round(60_000_000.0 / _DEFAULT_TEMPO_US, 6))
    first.setdefault("beats_per_bar", 4)
    first.setdefault("beat_unit", 4)
    tempo_map = [entries[key] for key in sorted(entries)]
    for entry in tempo_map:
        if entry["beat"] == 1.0:
            entry.pop("beat", None)

    parts_document: list[dict[str, Any]] = []
    report_parts: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for part in sorted(raw_parts, key=lambda item: (item.track, item.channel)):
        base = part.name or f"轨{part.track}"
        identifier = base
        suffix = 2
        while identifier in used_ids:
            identifier = f"{base}_{suffix}"
            suffix += 1
        used_ids.add(identifier)

        notes: list[dict[str, Any]] = []
        for note in sorted(part.notes, key=lambda item: (item.tick, item.midi)):
            bar, beat = clock.position(note.tick)
            velocity = note.velocity / 127.0
            notes.append(
                {
                    "bar": bar,
                    "beat": round(beat, 6),
                    "duration_beats": round(
                        clock.beats(note.tick, note.duration_ticks), 6
                    ),
                    "pitch": pitch_name(note.midi),
                    "dynamic": _nearest_dynamic(velocity),
                    "velocity": round(velocity, 6),
                }
            )
        def positioned_event(
            tick: int,
            track_event_index: int,
            **values: Any,
        ) -> dict[str, Any]:
            bar, beat = clock.position(tick)
            return {
                "tick": tick,
                "track_event_index": track_event_index,
                "bar": bar,
                "beat": round(beat, 6),
                **values,
            }

        program_changes = [
            positioned_event(
                event.tick,
                event.track_event_index,
                program_0_127=event.program,
            )
            for event in sorted(
                part.program_changes,
                key=lambda event: (
                    event.tick,
                    event.track_event_index,
                ),
            )
        ]
        controllers: dict[int, list[dict[str, Any]]] = {
            7: [],
            10: [],
            11: [],
            42: [],
            121: [],
        }
        for event in sorted(
            part.controllers,
            key=lambda event: (
                event.tick,
                event.track_event_index,
            ),
        ):
            values: dict[str, Any] = {
                "value_0_127": event.value,
                "normalized_0_1": round(event.value / 127.0, 6),
            }
            if event.controller == 10:
                values["normalized_pan"] = _midi_pan(event.value)
            controllers[event.controller].append(
                positioned_event(
                    event.tick,
                    event.track_event_index,
                    **values,
                )
            )
        parts_document.append(
            {
                "id": identifier,
                "name": f"{base}(通道 {part.channel + 1})",
                "notes": notes,
            }
        )
        pitches = sorted({note.midi for note in part.notes})
        report_parts.append(
            {
                "id": identifier,
                "track_index_0based": part.track,
                "channel_1based": part.channel + 1,
                # 兼容 v0.4 的导入报告读取方；新代码应使用注明基数的字段。
                "channel": part.channel + 1,
                "track_name": part.name,
                "percussion": part.is_percussion,
                "note_count": len(part.notes),
                "first_note_tick": min(note.tick for note in part.notes),
                "first_note_event_index": min(
                    note.start_event_index for note in part.notes
                ),
                "last_note_end_tick": max(
                    note.tick + note.duration_ticks for note in part.notes
                ),
                "range": f"{pitch_name(pitches[0])}~{pitch_name(pitches[-1])}",
                "noteheads": [pitch_name(value) for value in pitches]
                if part.is_percussion
                else [],
                "program_changes": program_changes,
                "cc7_volume": controllers[7],
                "cc10_pan": controllers[10],
                "cc11_expression": controllers[11],
                "cc42_pan_lsb": controllers[42],
                "cc121_reset_all_controllers": controllers[121],
                "unhandled_midi_messages": sorted(
                    part.unhandled_messages
                ),
            }
        )
        if part.unhandled_messages:
            warnings.append(
                f"声部 {identifier!r} 含当前未导入 score/roster 草稿的 MIDI "
                f"消息：{', '.join(sorted(part.unhandled_messages))}"
            )

    last_tick = max(
        note.tick + note.duration_ticks for part in raw_parts for note in part.notes
    )
    last_bar, _ = clock.position(last_tick)
    title = Path(path).stem
    document = upgrade_legacy_score_to_v1({
        "title": title,
        "sample_rate": 48_000,
        "tail_seconds": 3.0,
        "tempo_map": tempo_map,
        "parts": parts_document,
    })
    report = ImportReport(
        title=title,
        source_midi_sha256=source_midi_sha256,
        source_midi_byte_length=len(data),
        midi_format=midi_format,
        track_count=track_count,
        score_canonical_sha256=_canonical_json_sha256(document),
        ticks_per_quarter=ticks_per_quarter,
        parts=tuple(report_parts),
        tempo_changes=len(tempos),
        meter_changes=len(meters),
        warnings=tuple(dict.fromkeys(warnings))
        + (f"共 {last_bar} 小节",),
    )
    return document, report


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _midi_pan(value: int) -> float:
    """Map MIDI's asymmetric 0..64..127 pan scale onto -1..0..1."""

    if not 0 <= value <= 127:
        raise ValueError("MIDI CC10 value must be between 0 and 127")
    if value <= 64:
        return round((value - 64) / 64.0, 6)
    return round((value - 64) / 63.0, 6)


def _pan_candidate(
    events: list[dict[str, Any]],
    *,
    first_note_tick: int,
    first_note_event_index: int,
    blockers: list[str],
) -> dict[str, Any]:
    if blockers:
        return {
            "status": "requires_creator_decision",
            "value": None,
            "source_value_0_127": None,
            "reason": "unsupported_pan_controller_semantics",
            "blockers": blockers,
        }
    if not events:
        return {
            "status": "absent",
            "value": None,
            "source_value_0_127": None,
            "reason": "no_cc10_event",
            "blockers": [],
        }
    values = {event["value_0_127"] for event in events}
    if len(values) != 1:
        return {
            "status": "requires_creator_decision",
            "value": None,
            "source_value_0_127": None,
            "reason": "cc10_changes_over_time",
            "blockers": [],
        }
    first = events[0]
    if (
        first["tick"] > first_note_tick
        or (
            first["tick"] == first_note_tick
            and first["track_event_index"] > first_note_event_index
        )
    ):
        return {
            "status": "requires_creator_decision",
            "value": None,
            "source_value_0_127": None,
            "reason": "first_cc10_after_first_note",
            "blockers": [],
        }
    return {
        "status": "candidate",
        "value": first["normalized_pan"],
        "source_value_0_127": first["value_0_127"],
        "reason": "constant_cc10_effective_before_first_note",
        "blockers": [],
    }


def _midi_observation(
    events: list[dict[str, Any]],
    *,
    value_key: str,
) -> dict[str, Any]:
    distinct = sorted({event[value_key] for event in events})
    return {
        "status": (
            "missing"
            if not distinct
            else ("static" if len(distinct) == 1 else "varying")
        ),
        "distinct_values": distinct,
        "events": copy.deepcopy(events),
    }


def build_roster_draft(
    score_document: dict[str, Any],
    report: ImportReport,
) -> dict[str, Any]:
    """Build a safe roster draft with neutral creator-overridable defaults.

    Gain, pan, role and collaboration policy already have explicit defaults,
    so a creator does not have to fill boilerplate.  The only unresolved field
    is instrument/kit routing: Program Change is merely a hint and cannot
    safely name one of Tianlai's 103 dedicated entries.
    """

    if not isinstance(score_document, dict):
        raise TypeError("score_document must be an object")
    if not isinstance(report, ImportReport):
        raise TypeError("report must be an ImportReport")
    if not _SHA256_PATTERN.fullmatch(report.source_midi_sha256):
        raise ValueError("report source_midi_sha256 is invalid")
    if not _SHA256_PATTERN.fullmatch(report.score_canonical_sha256):
        raise ValueError("report score_canonical_sha256 is invalid")
    score_sha256 = _canonical_json_sha256(score_document)
    if report.score_canonical_sha256 != score_sha256:
        raise ValueError("score document does not match the MIDI import report")
    raw_parts = score_document.get("parts")
    if not isinstance(raw_parts, list):
        raise ValueError("score_document.parts must be an array")
    score_ids = [
        part.get("id") if isinstance(part, dict) else None
        for part in raw_parts
    ]
    if any(not isinstance(part_id, str) or not part_id for part_id in score_ids):
        raise ValueError("every score part must have a non-empty id")
    if len(set(score_ids)) != len(score_ids):
        raise ValueError("score part ids must be unique")
    report_by_id: dict[str, dict[str, Any]] = {}
    for part in report.parts:
        if not isinstance(part, dict):
            raise ValueError("report parts must be objects")
        part_id = part.get("id")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError("every report part must have a non-empty id")
        if part_id in report_by_id:
            raise ValueError(f"duplicate report part id: {part_id}")
        report_by_id[part_id] = part
    if set(score_ids) != set(report_by_id):
        raise ValueError("score parts do not match the MIDI import report")

    assignments: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for part_id in score_ids:
        part = report_by_id[part_id]
        blockers = [
            code
            for code, events in (
                ("cc42_pan_lsb_present", part["cc42_pan_lsb"]),
                (
                    "cc121_reset_all_controllers_present",
                    part["cc121_reset_all_controllers"],
                ),
            )
            if events
        ]
        pan = _pan_candidate(
            part["cc10_pan"],
            first_note_tick=int(part["first_note_tick"]),
            first_note_event_index=int(part["first_note_event_index"]),
            blockers=blockers,
        )
        assignment = {
            "part": part_id,
            (
                "kit"
                if bool(part["percussion"])
                else "instrument"
            ): None,
            "gain_db": 0.0,
            "pan": (
                pan["value"]
                if pan["status"] == "candidate"
                else 0.0
            ),
            "role": {
                "function": "other",
                "prominence": "midground",
            },
        }
        assignments.append(assignment)
        evidence.append(
            {
                "part": part_id,
                "source": {
                    "track_index_0based": int(
                        part["track_index_0based"]
                    ),
                    "channel_1based": int(part["channel_1based"]),
                    "track_name": str(part["track_name"]),
                    "percussion": bool(part["percussion"]),
                },
                "note_velocity": "preserved_exactly_in_score",
                "program_changes": _midi_observation(
                    part["program_changes"],
                    value_key="program_0_127",
                ),
                "cc7_volume": _midi_observation(
                    part["cc7_volume"],
                    value_key="value_0_127",
                ),
                "cc10_pan": _midi_observation(
                    part["cc10_pan"],
                    value_key="value_0_127",
                ),
                "cc11_expression": _midi_observation(
                    part["cc11_expression"],
                    value_key="value_0_127",
                ),
                "cc42_pan_lsb": _midi_observation(
                    part["cc42_pan_lsb"],
                    value_key="value_0_127",
                ),
                "cc121_reset_all_controllers": _midi_observation(
                    part["cc121_reset_all_controllers"],
                    value_key="value_0_127",
                ),
                "unhandled_midi_messages": list(
                    part["unhandled_midi_messages"]
                ),
                "gain_db_candidate": {
                    "status": "not_derived",
                    "value": None,
                    "fallback_default_db": 0.0,
                    "reason": "cc7_cc11_have_no_portable_db_mapping",
                },
                "pan_candidate": pan,
                "decisions": {
                    "routing": (
                        "kit_required"
                        if bool(part["percussion"])
                        else "instrument_required"
                    ),
                    "gain_db": "default_zero_creator_may_override",
                    "pan": (
                        "default_from_midi_candidate_creator_may_override"
                        if pan["status"] == "candidate"
                        else "default_center_creator_may_override"
                    ),
                    "role": "default_other_midground_creator_may_override",
                    "balance_relations": "optional_creator_owned",
                },
            }
        )

    title = score_document.get("title")
    if not isinstance(title, str) or not title.strip():
        title = report.title
    return {
        "format": "tianlai.midi_roster_draft",
        "version": 1,
        "status": "requires_creator_confirmation",
        "executable": False,
        "source": {
            "midi": {
                "sha256": report.source_midi_sha256,
                "byte_length": report.source_midi_byte_length,
                "smf_format": report.midi_format,
                "track_count": report.track_count,
                "ticks_per_quarter": report.ticks_per_quarter,
            },
            "score": {
                "canonical_sha256": score_sha256,
                "canonicalization": "tianlai-json-v1",
            },
        },
        "draft_roster": {
            "name": f"{title} MIDI 编制草稿",
            "assignments": assignments,
            "collaboration": {
                "mode": "manual",
                "balance_relations": [],
            },
        },
        "part_evidence": evidence,
        "notice": [
            "本文件是不可直接渲染的草稿；必须由创作者或其授权 Agent 显式确认。",
            "逐音 velocity 已保留在 score；Program Change 只作选乐器线索。",
            "CC7/CC11 没有可移植的 dB 映射；gain_db 因而默认 0 dB，不生成自动化。",
            "可靠的恒定 CC10 作为声像默认值；缺失、变化或有歧义时默认居中。",
            "角色默认 other/midground、协奏模式默认 manual；创作者都可以覆盖。",
            "选择 instrument 或填写 kit 后取出 draft_roster，并按正式 roster 合同重新校验。",
        ],
    }
