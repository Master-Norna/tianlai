"""MusicXML import into Tianlai's instrument-neutral score document.

The importer deliberately targets *sounding music*, not page layout.  Written
pitches are baked to concert pitch, durations are normalised through
``divisions``, and MusicXML's sequential cursor (including ``chord``,
``backup`` and ``forward``) is flattened into Tianlai's bar/beat coordinates.

MusicXML can describe much more than the current score model.  Features that
affect playback but cannot yet be represented are never silently guessed:
they are either rejected when timing would become ambiguous, or listed in the
returned report.  Page-only data such as clefs and engraving positions is
ignored because it does not change rendered sound.

Compressed ``.mxl`` files are read in memory from the rootfile named by
``META-INF/container.xml``.  Nothing is extracted to disk, and archive paths,
sizes, duplicate members and XML entity declarations are checked before
parsing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
import math
from pathlib import Path, PurePosixPath
import re
from typing import Any, Iterable
import xml.etree.ElementTree as ET
import zipfile

from .score import pitch_name, upgrade_legacy_score_to_v1


_DEFAULT_BPM = 120.0
_MAX_XML_BYTES = 64 * 1024 * 1024
_MAX_CONTAINER_BYTES = 1024 * 1024
_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 1024
_MAX_COMPRESSION_RATIO = 2000
_DYNAMIC_MARKS = ("ppp", "pp", "p", "mp", "mf", "f", "ff", "fff")
_HOSTILE_IDENTIFIER = set('/\\:*?"<>|')
_NOTE_OFFSETS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_ARTICULATIONS = {
    "staccato": "staccato",
    "staccatissimo": "staccatissimo",
    "tenuto": "tenuto",
    "accent": "accent",
    "strong-accent": "marcato",
    "detached-legato": "portato",
    "spiccato": "spiccato",
}
_BEAT_UNIT_QUARTERS = {
    "maxima": Fraction(32),
    "long": Fraction(16),
    "breve": Fraction(8),
    "whole": Fraction(4),
    "half": Fraction(2),
    "quarter": Fraction(1),
    "eighth": Fraction(1, 2),
    "16th": Fraction(1, 4),
    "32nd": Fraction(1, 8),
    "64th": Fraction(1, 16),
    "128th": Fraction(1, 32),
    "256th": Fraction(1, 64),
    "512th": Fraction(1, 128),
    "1024th": Fraction(1, 256),
}


@dataclass(frozen=True, slots=True)
class ImportReport:
    """A compact inventory for writing the roster after import."""

    title: str
    source_format: str
    parts: tuple[dict[str, Any], ...]
    measures: int
    tempo_changes: int
    meter_changes: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source_format": self.source_format,
            "measures": self.measures,
            "tempo_changes": self.tempo_changes,
            "meter_changes": self.meter_changes,
            "warnings": list(self.warnings),
            "parts": list(self.parts),
        }


@dataclass(slots=True)
class _PartInfo:
    source_id: str
    identifier: str
    name: str
    midi_unpitched: dict[str, int] = field(default_factory=dict)
    midi_channel: int | None = None
    percussion: bool = False


@dataclass(slots=True)
class _RawNote:
    bar: int
    onset_quarters: Fraction
    notation_onset_quarters: Fraction
    duration_quarters: Fraction
    sequence: int
    staff: int
    voice: str
    midi: float
    dynamic: str | None
    velocity: float | None
    articulation: str | None
    tie: bool


@dataclass(slots=True)
class _DynamicEvent:
    bar: int
    offset_quarters: Fraction
    sequence: int
    staff: int | None
    voice: str | None
    mark: str | None
    velocity: float | None


@dataclass(slots=True)
class _TempoEvent:
    bar: int
    offset_quarters: Fraction
    part_order: int
    sequence: int
    bpm: float


@dataclass(slots=True)
class _MeterEvent:
    bar: int
    part_order: int
    numerator: int
    denominator: int


@dataclass(slots=True)
class _PartResult:
    info: _PartInfo
    notes: list[_RawNote]
    dynamics: list[_DynamicEvent]
    measure_widths: dict[int, Fraction]
    implicit_measures: set[int]
    staff_numbers: set[int]


def _local_name(tag: object) -> str:
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for candidate in element:
        if _local_name(candidate.tag) == name:
            return candidate
    return None


def _descendants(element: ET.Element, name: str) -> Iterable[ET.Element]:
    for candidate in element.iter():
        if _local_name(candidate.tag) == name:
            yield candidate


def _text(element: ET.Element | None, default: str = "") -> str:
    if element is None:
        return default
    return "".join(element.itertext()).strip()


def _clean_text(text: str, fallback: str) -> str:
    cleaned = "".join(character for character in text if character.isprintable())
    cleaned = " ".join(cleaned.split())
    return cleaned or fallback


def _clean_identifier(text: str, fallback: str) -> str:
    cleaned = "".join(
        " " if character in _HOSTILE_IDENTIFIER else character
        for character in text
        if character.isprintable()
    )
    cleaned = " ".join(cleaned.split())
    return cleaned or fallback


def _fraction(text: str, label: str, *, positive: bool = False) -> Fraction:
    try:
        value = Fraction(text.strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"{label} 不是有效数值: {text!r}") from exc
    if positive and value <= 0:
        raise ValueError(f"{label} 必须为正")
    return value


def _integer(text: str, label: str, *, minimum: int | None = None) -> int:
    try:
        value = int(text.strip())
    except ValueError as exc:
        raise ValueError(f"{label} 不是整数: {text!r}") from exc
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} 必须至少为 {minimum}")
    return value


def _float(text: str, label: str) -> float:
    try:
        value = float(text.strip())
    except ValueError as exc:
        raise ValueError(f"{label} 不是有效数值: {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} 必须是有限数值")
    return value


def _append_warning(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)


def _xml_text_for_security_scan(payload: bytes, label: str) -> str | None:
    """Decode multibyte XML encodings so DTD checks cannot be NUL-bypassed."""

    encoding: str | None = None
    if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        encoding = "utf-32"
    elif payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif payload.startswith(b"\x00\x00\x00<"):
        encoding = "utf-32-be"
    elif payload.startswith(b"<\x00\x00\x00"):
        encoding = "utf-32-le"
    elif payload.startswith(b"\x00<\x00?"):
        encoding = "utf-16-be"
    elif payload.startswith(b"<\x00?\x00"):
        encoding = "utf-16-le"
    if encoding is None:
        return None
    try:
        return payload.decode(encoding)
    except UnicodeError as exc:
        raise ValueError(f"{label} 的 XML 编码损坏") from exc


def _parse_xml(payload: bytes, label: str) -> ET.Element:
    if len(payload) > _MAX_XML_BYTES:
        raise ValueError(f"{label} 解压后的 XML 超过 {_MAX_XML_BYTES // 1024 // 1024} MiB")
    uppercase = payload.upper()
    multibyte_text = _xml_text_for_security_scan(payload, label)
    uppercase_text = multibyte_text.upper() if multibyte_text is not None else ""
    # Standard MusicXML commonly carries an external PUBLIC doctype.  ElementTree
    # does not fetch that external subset, so it is safe to accept.  Internal
    # subsets/entities are rejected because they enable entity expansion.
    unsafe_bytes = b"<!ENTITY" in uppercase or re.search(
        br"<!DOCTYPE[^>]*\[", uppercase, re.DOTALL
    )
    unsafe_text = "<!ENTITY" in uppercase_text or re.search(
        r"<!DOCTYPE[^>]*\[", uppercase_text, re.DOTALL
    )
    if unsafe_bytes or unsafe_text:
        raise ValueError(f"{label} 含有不允许的 DTD/ENTITY 声明")
    try:
        return ET.fromstring(payload)
    except (ET.ParseError, LookupError, UnicodeError) as exc:
        raise ValueError(f"{label} XML 解析失败: {exc}") from exc


def _checked_zip_member(package: zipfile.ZipFile, name: str, limit: int) -> bytes:
    matches = [info for info in package.infolist() if info.filename == name]
    if len(matches) != 1:
        if not matches:
            raise ValueError(f"MXL 缺少 {name}")
        raise ValueError(f"MXL 含有重复成员 {name}")
    info = matches[0]
    if info.is_dir():
        raise ValueError(f"MXL 成员 {name} 不能是目录")
    if info.flag_bits & 0x1:
        raise ValueError(f"MXL 成员 {name} 已加密，无法安全读取")
    if info.file_size > limit:
        raise ValueError(f"MXL 成员 {name} 解压后过大")
    if info.file_size and info.file_size / max(info.compress_size, 1) > _MAX_COMPRESSION_RATIO:
        raise ValueError(f"MXL 成员 {name} 压缩比异常")
    with package.open(info, "r") as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise ValueError(f"MXL 成员 {name} 解压后过大")
    return payload


def _safe_rootfile_name(raw: str) -> str:
    if not raw or "\\" in raw or raw.startswith("/") or ":" in raw:
        raise ValueError(f"MXL rootfile 路径不安全: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"MXL rootfile 路径不安全: {raw!r}")
    normalised = path.as_posix()
    if normalised != raw:
        raise ValueError(f"MXL rootfile 路径不规范: {raw!r}")
    return normalised


def _read_mxl(path: Path) -> bytes:
    if path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ValueError(f"MXL 文件超过 {_MAX_ARCHIVE_BYTES // 1024 // 1024} MiB")
    try:
        with zipfile.ZipFile(path, "r") as package:
            infos = package.infolist()
            if len(infos) > _MAX_ARCHIVE_ENTRIES:
                raise ValueError(f"MXL 成员数超过 {_MAX_ARCHIVE_ENTRIES}")
            if sum(info.file_size for info in infos) > _MAX_ARCHIVE_BYTES:
                raise ValueError("MXL 解压后总大小超过安全上限")
            container_payload = _checked_zip_member(
                package, "META-INF/container.xml", _MAX_CONTAINER_BYTES
            )
            container = _parse_xml(container_payload, "MXL container.xml")
            if _local_name(container.tag) != "container":
                raise ValueError("MXL container.xml 的根元素必须是 container")
            rootfiles = [
                candidate
                for candidate in _descendants(container, "rootfile")
                if candidate.get("full-path")
            ]
            if not rootfiles:
                raise ValueError("MXL container.xml 没有 rootfile")
            root_name = _safe_rootfile_name(str(rootfiles[0].get("full-path")))
            return _checked_zip_member(package, root_name, _MAX_XML_BYTES)
    except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
        raise ValueError(f"损坏或不受支持的 MXL 压缩包: {path}") from exc


def _read_source(path: Path) -> tuple[bytes, str]:
    if not path.is_file():
        raise FileNotFoundError(f"找不到 MusicXML 文件: {path}")
    suffix = path.suffix.lower()
    if suffix not in (".musicxml", ".xml", ".mxl"):
        raise ValueError("MusicXML 输入扩展名必须是 .musicxml、.xml 或 .mxl")
    if suffix == ".mxl":
        return _read_mxl(path), "mxl"
    size = path.stat().st_size
    if size > _MAX_XML_BYTES:
        raise ValueError(f"MusicXML 文件超过 {_MAX_XML_BYTES // 1024 // 1024} MiB")
    return path.read_bytes(), suffix.lstrip(".")


def _title(root: ET.Element, path: Path) -> str:
    movement = next(_descendants(root, "movement-title"), None)
    work = next(_descendants(root, "work-title"), None)
    return _clean_text(_text(movement) or _text(work), path.stem)


def _parse_part_list(root: ET.Element) -> tuple[dict[str, _PartInfo], list[str]]:
    warnings: list[str] = []
    part_list = _child(root, "part-list")
    if part_list is None:
        raise ValueError("MusicXML 缺少 part-list")
    result: dict[str, _PartInfo] = {}
    used_ids: set[str] = set()
    for position, score_part in enumerate(_children(part_list, "score-part"), start=1):
        source_id = str(score_part.get("id", "")).strip()
        if not source_id:
            raise ValueError(f"part-list 第 {position} 个 score-part 缺少 id")
        if source_id in result:
            raise ValueError(f"part-list 含有重复 part id: {source_id}")
        identifier = _clean_identifier(source_id, f"P{position}")
        base = identifier
        suffix = 2
        while identifier in used_ids:
            identifier = f"{base}_{suffix}"
            suffix += 1
        if identifier != source_id:
            _append_warning(warnings, f"声部 id {source_id!r} 已清理为 {identifier!r}")
        used_ids.add(identifier)
        name = _clean_text(_text(_child(score_part, "part-name")), identifier)
        midi_unpitched: dict[str, int] = {}
        midi_channel: int | None = None
        percussion = False
        for midi_instrument in _children(score_part, "midi-instrument"):
            instrument_id = str(midi_instrument.get("id", "")).strip()
            channel_text = _text(_child(midi_instrument, "midi-channel"))
            if channel_text:
                channel = _integer(channel_text, f"{source_id} midi-channel", minimum=1)
                if channel > 16:
                    raise ValueError(f"{source_id} midi-channel 必须在 1~16")
                if midi_channel is None:
                    midi_channel = channel
                if channel == 10:
                    percussion = True
            unpitched_text = _text(_child(midi_instrument, "midi-unpitched"))
            if unpitched_text:
                value = _integer(
                    unpitched_text, f"{source_id} midi-unpitched", minimum=1
                )
                if value > 128:
                    raise ValueError(f"{source_id} midi-unpitched 必须在 1~128")
                if not instrument_id:
                    raise ValueError(f"{source_id} 的 midi-unpitched 缺少 instrument id")
                midi_unpitched[instrument_id] = value - 1
                percussion = True
        result[source_id] = _PartInfo(
            source_id=source_id,
            identifier=identifier,
            name=name,
            midi_unpitched=midi_unpitched,
            midi_channel=midi_channel,
            percussion=percussion,
        )
    return result, warnings


def _parse_time(time: ET.Element, label: str, warnings: list[str]) -> tuple[int, int]:
    beats_elements = _children(time, "beats")
    beat_type_elements = _children(time, "beat-type")
    if not beats_elements or not beat_type_elements:
        if _child(time, "senza-misura") is not None:
            raise ValueError(f"{label} 使用无拍号记谱，当前内部时间线无法表达")
        raise ValueError(f"{label} 的 time 必须同时含 beats 与 beat-type")
    if len(beats_elements) != len(beat_type_elements):
        raise ValueError(f"{label} 的 beats/beat-type 数量不匹配")

    segments: list[tuple[int, int]] = []
    labels: list[str] = []
    for index, (beats_element, beat_type_element) in enumerate(
        zip(beats_elements, beat_type_elements, strict=True),
        start=1,
    ):
        beats_text = _text(beats_element)
        denominator = _integer(
            _text(beat_type_element), f"{label} 第 {index} 个 beat-type", minimum=1
        )
        if denominator not in (1, 2, 4, 8, 16, 32):
            raise ValueError(f"{label} beat-type 必须是 1、2、4、8、16 或 32")
        try:
            groups = [int(piece.strip()) for piece in beats_text.split("+")]
        except ValueError as exc:
            raise ValueError(f"{label} 的复合拍 beats 无效: {beats_text!r}") from exc
        if not groups or any(group < 1 for group in groups):
            raise ValueError(f"{label} 的 beats 每组都必须为正")
        numerator = sum(groups)
        segments.append((numerator, denominator))
        labels.append(f"{beats_text}/{denominator}")
        if len(groups) > 1:
            _append_warning(
                warnings,
                f"{label} 的复合拍分组 {beats_text} 已保留总拍数，"
                "但内部谱暂不保存重音分组",
            )

    if len(segments) == 1:
        return segments[0]

    # MusicXML permits mixed denominators, e.g. 2/4 + 3/8.  Tianlai stores
    # one denominator per bar, so collapse to an exactly equivalent meter
    # (7/8 here) rather than silently truncating the bar.
    quarter_width = sum(
        (Fraction(numerator * 4, denominator) for numerator, denominator in segments),
        Fraction(0),
    )
    preferred_denominator = max(denominator for _, denominator in segments)
    candidates = [preferred_denominator, 1, 2, 4, 8, 16, 32]
    for denominator in dict.fromkeys(candidates):
        beats = quarter_width * denominator / 4
        if beats.denominator == 1 and beats >= 1:
            collapsed = (int(beats), denominator)
            _append_warning(
                warnings,
                f"{label} 的混合分母拍号 {' + '.join(labels)} 已等时值折算为 "
                f"{collapsed[0]}/{collapsed[1]}，重音分组未保存",
            )
            return collapsed
    raise ValueError(f"{label} 的混合分母拍号无法用内部拍单位精确表达")


def _parse_transpose(transpose: ET.Element, label: str) -> tuple[int | None, float]:
    staff_text = str(transpose.get("number", "")).strip()
    staff = _integer(staff_text, f"{label} transpose number", minimum=1) if staff_text else None
    chromatic_text = _text(_child(transpose, "chromatic"), "0")
    octave_text = _text(_child(transpose, "octave-change"), "0")
    semitones = _float(chromatic_text, f"{label} chromatic") + 12.0 * _float(
        octave_text, f"{label} octave-change"
    )
    return staff, semitones


def _pitch(
    pitch: ET.Element,
    transpose: float,
    label: str,
) -> float:
    step = _text(_child(pitch, "step")).upper()
    if step not in _NOTE_OFFSETS:
        raise ValueError(f"{label} 的 pitch step 无效: {step!r}")
    octave_text = _text(_child(pitch, "octave"))
    if not octave_text:
        raise ValueError(f"{label} 的 pitch 缺少 octave")
    octave = _integer(octave_text, f"{label} octave")
    alter_text = _text(_child(pitch, "alter"), "0")
    alter = _float(alter_text, f"{label} alter")
    value = (octave + 1) * 12 + _NOTE_OFFSETS[step] + alter + transpose
    if not math.isfinite(value):
        raise ValueError(f"{label} 的实音音高无效")
    return float(value)


def _dynamic_from_element(
    dynamics: ET.Element | None,
    label: str,
    warnings: list[str],
) -> str | None:
    if dynamics is None:
        return None
    for child in dynamics:
        name = _local_name(child.tag)
        if name in _DYNAMIC_MARKS:
            return name
        if name == "other-dynamics":
            name = _text(child)
        if name:
            _append_warning(warnings, f"{label} 的力度记号 {name!r} 暂不能直接表达")
    return None


def _velocity_from_percent(
    raw_percent: str,
    label: str,
    warnings: list[str],
) -> float:
    percent = _float(raw_percent, f"{label} dynamics")
    if percent < 0:
        raise ValueError(f"{label} dynamics 不能为负")
    # MusicXML: percentage of the default forte MIDI velocity (90).
    raw = percent * 90.0 / 100.0 / 127.0
    if raw <= 0:
        _append_warning(warnings, f"{label} 的 dynamics<=0，已夹到最小可发声力度")
    return round(min(1.0, max(1.0 / 127.0, raw)), 6)


def _velocity_from_sound(
    sound: ET.Element | None,
    label: str,
    warnings: list[str],
) -> float | None:
    if sound is None or sound.get("dynamics") is None:
        return None
    return _velocity_from_percent(str(sound.get("dynamics")), label, warnings)


def _nearest_dynamic(velocity: float) -> str:
    thresholds = (
        (0.16, "ppp"),
        (0.27, "pp"),
        (0.39, "p"),
        (0.51, "mp"),
        (0.64, "mf"),
        (0.77, "f"),
        (0.90, "ff"),
    )
    for threshold, mark in thresholds:
        if velocity < threshold:
            return mark
    return "fff"


def _metronome_bpm(
    direction: ET.Element,
    label: str,
    warnings: list[str],
) -> float | None:
    metronomes = list(_descendants(direction, "metronome"))
    if not metronomes:
        return None
    metronome = metronomes[0]
    beat_unit = _text(_child(metronome, "beat-unit")).lower()
    per_minute = _text(_child(metronome, "per-minute"))
    if not beat_unit or not per_minute:
        _append_warning(warnings, f"{label} 的复杂 metronome 标记暂未导入")
        return None
    if beat_unit not in _BEAT_UNIT_QUARTERS:
        _append_warning(warnings, f"{label} 的 metronome beat-unit {beat_unit!r} 未识别")
        return None
    unit_quarters = _BEAT_UNIT_QUARTERS[beat_unit]
    dots = len(_children(metronome, "beat-unit-dot"))
    increment = unit_quarters
    for _ in range(dots):
        increment /= 2
        unit_quarters += increment
    bpm = _float(per_minute, f"{label} per-minute") * float(unit_quarters)
    if not 1.0 <= bpm <= 600.0:
        raise ValueError(f"{label} 换算后的四分音符 BPM 必须在 1~600")
    return bpm


def _direction_position(
    direction: ET.Element,
    cursor: Fraction,
    divisions: Fraction | None,
    label: str,
    warnings: list[str],
) -> Fraction:
    sound = _child(direction, "sound")
    # MusicXML explicitly gives sound/offset precedence over direction/offset.
    sound_offset = _child(sound, "offset") if sound is not None else None
    offset_element = sound_offset
    if offset_element is None:
        direction_offset = _child(direction, "offset")
        # For direction/offset, MusicXML defaults sound="no": without an
        # explicit yes it is engraving displacement only and must not move
        # tempo or dynamics.  A sound/offset is inherently a playback offset.
        if (
            direction_offset is None
            or str(direction_offset.get("sound", "no")).lower() != "yes"
        ):
            return cursor
        offset_element = direction_offset
    if offset_element is None or not _text(offset_element):
        return cursor
    if divisions is None:
        raise ValueError(f"{label} 在 divisions 声明之前使用 offset")
    position = cursor + _fraction(_text(offset_element), f"{label} offset") / divisions
    if position < 0:
        _append_warning(warnings, f"{label} 的负 offset 越过小节线，已对齐到小节开头")
        return Fraction(0)
    return position


def _note_articulation(
    note: ET.Element,
    label: str,
    warnings: list[str],
) -> str | None:
    found: list[str] = []
    if str(note.get("pizzicato", "")).lower() == "yes":
        found.append("pizzicato")
    for notations in _children(note, "notations"):
        for articulations in _children(notations, "articulations"):
            for child in articulations:
                name = _local_name(child.tag)
                mapped = _ARTICULATIONS.get(name)
                if mapped is not None:
                    found.append(mapped)
                elif name not in ("breath-mark", "caesura"):
                    _append_warning(warnings, f"{label} 的奏法 {name!r} 暂未映射")
        for child in notations:
            name = _local_name(child.tag)
            if name in ("ornaments", "fermata", "glissando", "slide", "arpeggiate", "technical", "slur"):
                _append_warning(warnings, f"{label} 的 {name} 记号未进入内部乐谱")
    unique = list(dict.fromkeys(found))
    if len(unique) > 1:
        _append_warning(
            warnings,
            f"{label} 同时含多个奏法（{', '.join(unique)}），当前只保留 {unique[0]}",
        )
    return unique[0] if unique else None


def _note_dynamic(
    note: ET.Element,
    label: str,
    warnings: list[str],
) -> str | None:
    for notations in _children(note, "notations"):
        mark = _dynamic_from_element(_child(notations, "dynamics"), label, warnings)
        if mark is not None:
            return mark
    return None


def _has_tie_start(note: ET.Element) -> bool:
    if any(candidate.get("type") == "start" for candidate in _children(note, "tie")):
        return True
    for notations in _children(note, "notations"):
        if any(
            candidate.get("type") == "start"
            for candidate in _children(notations, "tied")
        ):
            return True
    return False


def _warn_direction_features(
    direction: ET.Element,
    label: str,
    warnings: list[str],
) -> None:
    # octave-shift is engraving-only in MusicXML: pitch data already contains
    # the performed octave, so it must not be applied again here.
    for name in ("wedge", "pedal", "dashes", "bracket"):
        if any(True for _ in _descendants(direction, name)):
            _append_warning(warnings, f"{label} 的 {name} 连续控制暂未进入内部乐谱")
    for sound in _children(direction, "sound"):
        _warn_sound_features(sound, label, warnings)


def _warn_sound_features(
    sound: ET.Element,
    label: str,
    warnings: list[str],
) -> None:
    playback = [
        key
        for key in ("dacapo", "dalsegno", "tocoda", "fine", "segno", "coda")
        if sound.get(key) is not None
    ]
    if playback:
        _append_warning(
            warnings,
            f"{label} 的跳转播放标记（{', '.join(playback)}）未展开，按书写顺序导入一次",
        )
    if _child(sound, "midi-instrument") is not None:
        _append_warning(
            warnings,
            f"{label} 含播放中的乐器切换；内部声部不保存中途换乐器，需在编制层人工拆分",
        )


def _parse_part(
    part: ET.Element,
    info: _PartInfo,
    part_order: int,
    meter_events: list[_MeterEvent],
    tempo_events: list[_TempoEvent],
    warnings: list[str],
) -> _PartResult:
    divisions: Fraction | None = None
    current_meter = (4, 4)
    global_transpose = 0.0
    staff_transpose: dict[int, float] = {}
    notes: list[_RawNote] = []
    dynamics: list[_DynamicEvent] = []
    measure_widths: dict[int, Fraction] = {}
    implicit_measures: set[int] = set()
    staff_numbers: set[int] = set()
    instrument_ids: set[str] = set()
    sequence = 0

    measures = _children(part, "measure")
    for bar, measure in enumerate(measures, start=1):
        display_number = str(measure.get("number", "")).strip()
        if (
            str(measure.get("implicit", "")).lower() == "yes"
            or display_number == "0"
        ):
            implicit_measures.add(bar)
        cursor = Fraction(0)
        maximum = Fraction(0)
        last_non_chord_start: Fraction | None = None
        last_chord_staff: int | None = None
        last_chord_voice: str | None = None
        last_chord_instrument = ""
        label = f"声部 {info.identifier} 第 {bar} 小节"

        for item in measure:
            name = _local_name(item.tag)
            sequence += 1
            if name == "attributes":
                divisions_element = _child(item, "divisions")
                if divisions_element is not None:
                    divisions = _fraction(
                        _text(divisions_element), f"{label} divisions", positive=True
                    )
                for time in _children(item, "time"):
                    meter = _parse_time(time, label, warnings)
                    if cursor:
                        _append_warning(
                            warnings,
                            f"{label} 的拍号变化不在小节开头，已按该小节起点处理",
                        )
                    current_meter = meter
                    meter_events.append(
                        _MeterEvent(bar, part_order, meter[0], meter[1])
                    )
                for transpose in _children(item, "transpose"):
                    staff, semitones = _parse_transpose(transpose, label)
                    if staff is None:
                        global_transpose = semitones
                        # An unnumbered transpose applies to every staff.
                        staff_transpose.clear()
                    else:
                        staff_transpose[staff] = semitones
                    if _child(transpose, "double") is not None:
                        _append_warning(
                            warnings,
                            f"{label} 的 transpose/double 表示八度加倍声部，"
                            "当前只保留主音高，需在编制层另加执行器",
                        )
                for measure_style in _children(item, "measure-style"):
                    shorthand = [
                        feature
                        for feature in ("measure-repeat", "beat-repeat", "slash", "multiple-rest")
                        if _child(measure_style, feature) is not None
                    ]
                    if shorthand:
                        _append_warning(
                            warnings,
                            f"{label} 的简写记谱（{', '.join(shorthand)}）未展开；"
                            "请在制谱软件中展开后再导入以获得完整演奏",
                        )
                continue

            if name == "direction":
                position = _direction_position(
                    item, cursor, divisions, label, warnings
                )
                sound = _child(item, "sound")
                tempo: float | None = None
                if sound is not None and sound.get("tempo") is not None:
                    tempo = _float(str(sound.get("tempo")), f"{label} sound tempo")
                    if not 1.0 <= tempo <= 600.0:
                        raise ValueError(f"{label} 的 sound tempo 必须在 1~600 BPM")
                else:
                    tempo = _metronome_bpm(item, label, warnings)
                if tempo is not None:
                    tempo_events.append(
                        _TempoEvent(bar, position, part_order, sequence, tempo)
                    )
                mark = None
                for direction_type in _children(item, "direction-type"):
                    mark = _dynamic_from_element(
                        _child(direction_type, "dynamics"), label, warnings
                    )
                    if mark is not None:
                        break
                velocity = _velocity_from_sound(sound, label, warnings)
                if mark is not None or velocity is not None:
                    if mark is None and velocity is not None:
                        mark = _nearest_dynamic(velocity)
                    staff_element = _child(item, "staff")
                    event_staff = (
                        _integer(
                            _text(staff_element),
                            f"{label} direction staff",
                            minimum=1,
                        )
                        if staff_element is not None and _text(staff_element)
                        else None
                    )
                    voice_text = _text(_child(item, "voice"))
                    dynamics.append(
                        _DynamicEvent(
                            bar,
                            position,
                            sequence,
                            event_staff,
                            voice_text or None,
                            mark,
                            velocity,
                        )
                    )
                _warn_direction_features(item, label, warnings)
                continue

            if name == "sound":
                position = cursor
                offset_element = _child(item, "offset")
                if offset_element is not None and _text(offset_element):
                    if divisions is None:
                        raise ValueError(f"{label} 在 divisions 声明之前使用 sound/offset")
                    position += _fraction(
                        _text(offset_element), f"{label} sound offset"
                    ) / divisions
                    if position < 0:
                        _append_warning(
                            warnings,
                            f"{label} 的 sound 负 offset 越过小节线，已对齐到小节开头",
                        )
                        position = Fraction(0)
                if item.get("tempo") is not None:
                    tempo = _float(str(item.get("tempo")), f"{label} sound tempo")
                    if not 1.0 <= tempo <= 600.0:
                        raise ValueError(f"{label} 的 sound tempo 必须在 1~600 BPM")
                    tempo_events.append(
                        _TempoEvent(bar, position, part_order, sequence, tempo)
                    )
                velocity = _velocity_from_sound(item, label, warnings)
                if velocity is not None:
                    dynamics.append(
                        _DynamicEvent(
                            bar,
                            position,
                            sequence,
                            None,
                            None,
                            _nearest_dynamic(velocity),
                            velocity,
                        )
                    )
                _warn_sound_features(item, label, warnings)
                continue

            if name in ("backup", "forward"):
                if divisions is None:
                    raise ValueError(f"{label} 在 divisions 声明之前使用 {name}")
                duration_element = _child(item, "duration")
                if duration_element is None:
                    raise ValueError(f"{label} 的 {name} 缺少 duration")
                distance = _fraction(
                    _text(duration_element), f"{label} {name} duration", positive=True
                ) / divisions
                if name == "backup":
                    cursor -= distance
                    if cursor < 0:
                        raise ValueError(f"{label} 的 backup 令时间游标越过小节开头")
                    last_non_chord_start = None
                else:
                    cursor += distance
                    maximum = max(maximum, cursor)
                    last_non_chord_start = None
                last_chord_staff = None
                last_chord_voice = None
                last_chord_instrument = ""
                continue

            if name == "barline":
                if _child(item, "repeat") is not None:
                    _append_warning(
                        warnings,
                        f"{label} 含 repeat，首版未展开重复段，按书写顺序导入一次",
                    )
                if _child(item, "ending") is not None:
                    _append_warning(
                        warnings,
                        f"{label} 含 ending，首版未展开结尾分支，按书写顺序导入一次",
                    )
                continue

            if name != "note":
                continue

            note_label = f"{label} 第 {sequence} 个元素"
            is_grace = _child(item, "grace") is not None
            is_chord = _child(item, "chord") is not None
            if is_grace:
                _append_warning(
                    warnings,
                    f"{label} 含 grace note（倚音），当前无独立时值模型，已跳过",
                )
                continue
            duration_element = _child(item, "duration")
            if duration_element is None:
                raise ValueError(f"{note_label} 缺少 duration")
            if divisions is None:
                raise ValueError(f"{note_label} 在首个 divisions 声明之前出现有时值音符")
            duration = _fraction(
                _text(duration_element), f"{note_label} duration", positive=True
            ) / divisions
            if is_chord:
                if last_non_chord_start is None:
                    raise ValueError(f"{note_label} 的 chord 前没有可共享起点的音符")
                onset = last_non_chord_start
            else:
                onset = cursor
                last_non_chord_start = onset
                cursor += duration
            maximum = max(maximum, onset + duration, cursor)

            if _child(item, "lyric") is not None:
                _append_warning(warnings, f"{label} 的歌词未进入内部乐谱")
            staff_element = _child(item, "staff")
            staff_text = _text(staff_element)
            if not staff_text and is_chord and last_chord_staff is not None:
                staff_text = str(last_chord_staff)
            if not staff_text:
                staff_text = "1"
            staff = _integer(staff_text, f"{note_label} staff", minimum=1)
            voice_text = _text(_child(item, "voice"))
            if not voice_text and is_chord and last_chord_voice is not None:
                voice_text = last_chord_voice
            voice = voice_text or "1"
            staff_numbers.add(staff)
            if _child(item, "rest") is not None:
                last_non_chord_start = None
                last_chord_staff = None
                last_chord_voice = None
                last_chord_instrument = ""
                continue
            if _child(item, "cue") is not None:
                _append_warning(warnings, f"{label} 的 cue note 已按提示音跳过")
                last_non_chord_start = None
                last_chord_staff = None
                last_chord_voice = None
                last_chord_instrument = ""
                continue

            pitch_element = _child(item, "pitch")
            unpitched = _child(item, "unpitched")
            instrument = _child(item, "instrument")
            instrument_id = (
                str(instrument.get("id", "")).strip()
                if instrument is not None
                else ""
            )
            if not instrument_id and is_chord:
                instrument_id = last_chord_instrument
            if instrument_id:
                instrument_ids.add(instrument_id)
                if len(instrument_ids) > 1 and pitch_element is not None:
                    _append_warning(
                        warnings,
                        f"{label} 的 pitched notes 中途切换 instrument id；"
                        "当前仍展平为一个声部，编制时需人工拆分",
                    )
            if pitch_element is not None:
                semitones = staff_transpose.get(staff, global_transpose)
                midi = _pitch(pitch_element, semitones, note_label)
            elif unpitched is not None:
                if instrument_id not in info.midi_unpitched:
                    _append_warning(
                        warnings,
                        f"{label} 的无音高打击音缺少 midi-unpitched 映射，已跳过",
                    )
                    continue
                midi = float(info.midi_unpitched[instrument_id])
                info.percussion = True
            else:
                raise ValueError(f"{note_label} 既不是 rest，也没有 pitch/unpitched")

            sound_start = onset
            sound_end = onset + duration
            if item.get("attack") is not None:
                sound_start += _fraction(
                    str(item.get("attack")), f"{note_label} attack"
                ) / divisions
            if item.get("release") is not None:
                sound_end += _fraction(
                    str(item.get("release")), f"{note_label} release"
                ) / divisions
            if sound_start < 0:
                _append_warning(
                    warnings,
                    f"{note_label} 的 attack 越过小节开头，已裁到当前小节起点",
                )
                sound_start = Fraction(0)
            sounding_duration = sound_end - sound_start
            if sounding_duration <= 0:
                raise ValueError(f"{note_label} 的 attack/release 令实际发声时值不为正")
            note_mark = _note_dynamic(item, note_label, warnings)
            if note_mark is not None:
                # A steady p..fff mark attached to a note starts at that note
                # musically, just like the same mark in a direction.  Keep it
                # as staff/voice state for following notes as well.
                dynamics.append(
                    _DynamicEvent(
                        bar,
                        onset,
                        sequence,
                        staff,
                        voice,
                        note_mark,
                        None,
                    )
                )
            note_velocity = None
            if item.get("dynamics") is not None:
                note_velocity = _velocity_from_percent(
                    str(item.get("dynamics")), note_label, warnings
                )
                if note_mark is None:
                    note_mark = _nearest_dynamic(note_velocity)
            notes.append(
                _RawNote(
                    bar=bar,
                    onset_quarters=sound_start,
                    notation_onset_quarters=onset,
                    duration_quarters=sounding_duration,
                    sequence=sequence,
                    staff=staff,
                    voice=voice,
                    midi=midi,
                    dynamic=note_mark,
                    velocity=note_velocity,
                    articulation=_note_articulation(item, note_label, warnings),
                    tie=_has_tie_start(item),
                )
            )
            last_chord_staff = staff
            last_chord_voice = voice
            last_chord_instrument = instrument_id

        measure_widths[bar] = maximum
        expected = Fraction(current_meter[0] * 4, current_meter[1])
        if maximum > expected:
            _append_warning(
                warnings,
                f"{label} 的内容宽度 {float(maximum):g} 个四分音符超过拍号容量 "
                f"{float(expected):g}，按谱面起点保留",
            )

    if len(staff_numbers) > 1:
        _append_warning(
            warnings,
            f"声部 {info.identifier} 的 {len(staff_numbers)} 个谱表已展平到同一内部声部",
        )
    return _PartResult(
        info=info,
        notes=notes,
        dynamics=dynamics,
        measure_widths=measure_widths,
        implicit_measures=implicit_measures,
        staff_numbers=staff_numbers,
    )


def _resolve_meters(
    events: list[_MeterEvent],
    parts: list[_PartResult],
    measures: int,
    warnings: list[str],
) -> tuple[dict[int, tuple[int, int]], dict[int, tuple[int, int]], bool]:
    selected: dict[int, tuple[int, int]] = {}
    grouped: dict[int, list[_MeterEvent]] = {}
    for event in events:
        grouped.setdefault(event.bar, []).append(event)
    for bar in sorted(grouped):
        candidates = sorted(grouped[bar], key=lambda item: item.part_order)
        chosen = (candidates[0].numerator, candidates[0].denominator)
        conflicts = {
            (candidate.numerator, candidate.denominator) for candidate in candidates
        }
        if len(conflicts) > 1:
            _append_warning(
                warnings,
                f"第 {bar} 小节各声部拍号冲突，采用首个声部的 {chosen[0]}/{chosen[1]}",
            )
        selected[bar] = chosen
    selected.setdefault(1, (4, 4))

    by_bar: dict[int, tuple[int, int]] = {}
    current = (4, 4)
    for bar in range(1, max(1, measures) + 1):
        if bar in selected:
            current = selected[bar]
        by_bar[bar] = current

    pickup = False
    original_first = by_bar[1]
    expected = Fraction(original_first[0] * 4, original_first[1])
    implicit = any(1 in part.implicit_measures for part in parts)
    widths = [
        part.measure_widths.get(1, Fraction(0))
        for part in parts
        if part.measure_widths.get(1, Fraction(0)) > 0
    ]
    width = max(widths, default=Fraction(0))
    if width > 0 and width < expected and implicit:
        preferred = [original_first[1], 1, 2, 4, 8, 16, 32]
        short_meter: tuple[int, int] | None = None
        for denominator in dict.fromkeys(preferred):
            beats = width * denominator / 4
            if beats.denominator == 1 and beats >= 1:
                short_meter = (int(beats), denominator)
                break
        if short_meter is None:
            raise ValueError("不完整首小节无法用内部支持的拍单位精确表达")
        by_bar[1] = short_meter
        selected[1] = short_meter
        if measures >= 2:
            restored = selected.get(2, original_first)
            selected[2] = restored
            by_bar[2] = restored
            current = restored
            for bar in range(3, measures + 1):
                if bar in selected:
                    current = selected[bar]
                by_bar[bar] = current
        pickup = True
        _append_warning(
            warnings,
            f"检测到时值为 {float(width):g} 个四分音符的不完整首小节，"
            f"已编码为 {short_meter[0]}/{short_meter[1]}，第 2 小节恢复 "
            f"{original_first[0]}/{original_first[1]}",
        )
    return by_bar, selected, pickup


def _normalise_position(
    bar: int,
    offset: Fraction,
    meters: dict[int, tuple[int, int]],
    measures: int,
) -> tuple[int, Fraction]:
    while bar < measures:
        numerator, denominator = meters.get(bar, meters[max(meters)])
        width = Fraction(numerator * 4, denominator)
        if offset < width:
            break
        offset -= width
        bar += 1
    return bar, offset


def _tempo_map(
    tempo_events: list[_TempoEvent],
    meters: dict[int, tuple[int, int]],
    meter_changes: dict[int, tuple[int, int]],
    measures: int,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], int]:
    selected_tempos: dict[tuple[int, Fraction], float] = {}
    for event in sorted(
        tempo_events,
        key=lambda item: (
            item.bar,
            item.offset_quarters,
            item.part_order,
            item.sequence,
        ),
    ):
        bar, offset = _normalise_position(
            event.bar, event.offset_quarters, meters, measures
        )
        key = (bar, offset)
        previous = selected_tempos.get(key)
        if previous is None:
            selected_tempos[key] = event.bpm
        elif not math.isclose(previous, event.bpm, abs_tol=1e-6):
            _append_warning(
                warnings,
                f"第 {bar} 小节同一位置存在冲突速度 {previous:g}/{event.bpm:g} BPM，"
                f"采用首个声部的 {previous:g}",
            )

    entries: dict[tuple[int, Fraction], dict[str, Any]] = {}
    for bar, (numerator, denominator) in sorted(meter_changes.items()):
        key = (bar, Fraction(0))
        entries.setdefault(key, {"bar": bar})
        entries[key]["beats_per_bar"] = numerator
        entries[key]["beat_unit"] = denominator
    for (bar, offset), bpm in selected_tempos.items():
        denominator = meters.get(bar, (4, 4))[1]
        beat = Fraction(1) + offset * denominator / 4
        key = (bar, beat - 1)
        entry = entries.setdefault(key, {"bar": bar})
        if beat != 1:
            entry["beat"] = round(float(beat), 6)
        entry["bpm"] = round(float(bpm), 6)

    first = entries.setdefault((1, Fraction(0)), {"bar": 1})
    first.setdefault("bpm", _DEFAULT_BPM)
    first.setdefault("beats_per_bar", meters[1][0])
    first.setdefault("beat_unit", meters[1][1])
    return [entries[key] for key in sorted(entries)], len(selected_tempos)


def _apply_dynamics(result: _PartResult) -> None:
    # Process directions and notes in score order.  Sequence is essential when
    # a second voice backs up to the same onset: its dynamic must not
    # retroactively change a first-voice note already encountered there.
    timeline: list[tuple[int, Fraction, int, int, object]] = []
    timeline.extend(
        (event.bar, event.offset_quarters, event.sequence, 0, event)
        for event in result.dynamics
    )
    timeline.extend(
        (note.bar, note.notation_onset_quarters, note.sequence, 1, note)
        for note in result.notes
    )
    states: dict[tuple[int | None, str | None], _DynamicEvent] = {}
    for _, _, _, kind, item in sorted(timeline, key=lambda entry: entry[:4]):
        if kind == 0:
            event = item
            assert isinstance(event, _DynamicEvent)
            states[(event.staff, event.voice)] = event
            continue
        note = item
        assert isinstance(note, _RawNote)
        if note.dynamic is not None or note.velocity is not None:
            # A per-note notation or exact velocity is more specific than any
            # persistent direction and must not inherit an older exact value.
            continue
        candidates = [
            event
            for (staff, voice), event in states.items()
            if (staff is None or staff == note.staff)
            and (voice is None or voice == note.voice)
        ]
        if not candidates:
            continue
        chosen = max(
            candidates,
            key=lambda event: (
                event.bar,
                event.offset_quarters,
                event.sequence,
                int(event.staff is not None) + int(event.voice is not None),
            ),
        )
        note.dynamic = chosen.mark
        note.velocity = chosen.velocity


def _score_pitch(value: float) -> str | float:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return pitch_name(value)
    return round(value, 6)


def _range_pitch(value: float) -> str:
    if math.isclose(value, round(value), abs_tol=1e-9):
        return pitch_name(value)
    return f"{value:.3f}".rstrip("0").rstrip(".")


def read_musicxml(path: str | Path) -> tuple[dict[str, Any], ImportReport]:
    """Read ``.musicxml``/``.xml``/``.mxl`` into a validated score-shaped dict."""

    source = Path(path)
    payload, source_format = _read_source(source)
    root = _parse_xml(payload, str(source))
    root_name = _local_name(root.tag)
    if root_name == "score-timewise":
        raise ValueError(
            "暂不支持 score-timewise；请在制谱软件中另存为 score-partwise MusicXML"
        )
    if root_name != "score-partwise":
        raise ValueError(f"MusicXML 根元素必须是 score-partwise，实际为 {root_name!r}")

    part_info, warnings = _parse_part_list(root)
    meter_events: list[_MeterEvent] = []
    tempo_events: list[_TempoEvent] = []
    results: list[_PartResult] = []
    part_elements = _children(root, "part")
    if not part_elements:
        raise ValueError("MusicXML 没有 part")
    used_source_ids: set[str] = set()
    for part_order, part in enumerate(part_elements):
        source_id = str(part.get("id", "")).strip()
        if not source_id:
            raise ValueError(f"第 {part_order + 1} 个 part 缺少 id")
        if source_id in used_source_ids:
            raise ValueError(f"MusicXML 含有重复 part: {source_id}")
        used_source_ids.add(source_id)
        info = part_info.get(source_id)
        if info is None:
            identifier = _clean_identifier(source_id, f"P{part_order + 1}")
            info = _PartInfo(source_id, identifier, identifier)
            _append_warning(
                warnings,
                f"part {source_id!r} 未在 part-list 声明，已按名称导入",
            )
        results.append(
            _parse_part(
                part,
                info,
                part_order,
                meter_events,
                tempo_events,
                warnings,
            )
        )

    results = [result for result in results if result.notes]
    if not results:
        raise ValueError("这个 MusicXML 文件里没有可导入的发声音符")
    measures = max(
        (max(result.measure_widths, default=0) for result in results), default=1
    )
    meters, meter_changes, _ = _resolve_meters(
        meter_events, results, measures, warnings
    )
    tempo_map, tempo_count = _tempo_map(
        tempo_events, meters, meter_changes, measures, warnings
    )

    parts_document: list[dict[str, Any]] = []
    report_parts: list[dict[str, Any]] = []
    for result in results:
        _apply_dynamics(result)
        notes_document: list[dict[str, Any]] = []
        for note in sorted(
            result.notes,
            key=lambda item: (item.bar, item.onset_quarters, item.midi),
        ):
            denominator = meters.get(note.bar, (4, 4))[1]
            beat = Fraction(1) + note.onset_quarters * denominator / 4
            duration = note.duration_quarters * denominator / 4
            note_document: dict[str, Any] = {
                "bar": note.bar,
                "beat": round(float(beat), 6),
                "duration_beats": round(float(duration), 6),
                "pitch": _score_pitch(note.midi),
            }
            if note.dynamic is not None:
                note_document["dynamic"] = note.dynamic
            if note.velocity is not None:
                note_document["velocity"] = round(note.velocity, 6)
            if note.articulation is not None:
                note_document["articulation"] = note.articulation
            if note.tie:
                note_document["tie"] = True
            # Voice numbers are scoped to a staff in MusicXML.  Preserve both
            # so tied notes cannot be joined to an equal-pitch note from
            # another flattened piano voice during conducting.
            note_document["staff"] = note.staff
            note_document["voice"] = note.voice
            notes_document.append(note_document)
        parts_document.append(
            {
                "id": result.info.identifier,
                "name": result.info.name,
                "notes": notes_document,
            }
        )
        pitches = sorted({note.midi for note in result.notes})
        report_parts.append(
            {
                "id": result.info.identifier,
                "name": result.info.name,
                "channel": result.info.midi_channel,
                "percussion": result.info.percussion,
                "note_count": len(result.notes),
                "range": f"{_range_pitch(pitches[0])}~{_range_pitch(pitches[-1])}",
                "noteheads": (
                    [_range_pitch(value) for value in pitches]
                    if result.info.percussion
                    else []
                ),
            }
        )

    document = upgrade_legacy_score_to_v1({
        "title": _title(root, source),
        "sample_rate": 48_000,
        "tail_seconds": 3.0,
        "tempo_map": tempo_map,
        "parts": parts_document,
    })
    report = ImportReport(
        title=document["title"],
        source_format=source_format,
        parts=tuple(report_parts),
        measures=measures,
        tempo_changes=tempo_count,
        meter_changes=len(meter_changes),
        warnings=tuple(warnings),
    )
    return document, report
