from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
from typing import Iterable, Sequence
import wave
import xml.etree.ElementTree as ET


NOTE_MIN = 40
NOTE_MAX = 102
# The published DecentSampler preset labels every recording one octave above
# its sounding fundamental.  Keep NOTE_MIN/NOTE_MAX as strict upstream-map
# constants, but key the generated SFZ at the measured native octave instead
# of transposing every recording upward and changing the instrument's timbre.
PLAYBACK_NOTE_OFFSET = -12
PLAYBACK_NOTE_MIN = NOTE_MIN + PLAYBACK_NOTE_OFFSET
PLAYBACK_NOTE_MAX = NOTE_MAX + PLAYBACK_NOTE_OFFSET
VELOCITY_LAYERS = ((0, 40), (41, 109), (110, 127))
ROUND_ROBIN_LENGTH = 2
TIMBRES = ("lupe", "reso")
PRESET_NAME = "clavichord.dspreset"
DEFAULT_OUTPUT_DIRECTORY = "tianlai"
NORMAL_SFZ_NAME = "normal.sfz"
RESONANCE_SFZ_NAME = "resonance.sfz"

_SAMPLE_ATTRIBUTES = {
    "rootNote",
    "loNote",
    "hiNote",
    "loVel",
    "hiVel",
    "seqPosition",
    "path",
    "start",
    "end",
}
_GROUP_ATTRIBUTES = {
    "trigger",
    "enabled",
    "loopEnabled",
    "start",
    "release",
    "ampVelTrack",
    "tags",
}
_EXPECTED_GROUP_VALUES = {
    ("reso", "attack"): {
        "trigger": "attack",
        "enabled": "true",
        "loopEnabled": "false",
        "start": "0",
        "release": "4",
        "ampVelTrack": "1",
        "tags": "reso",
    },
    ("reso", "release"): {
        "trigger": "release",
        "enabled": "false",
        "loopEnabled": "false",
        "start": "0",
        "release": "0",
        "ampVelTrack": "1",
        "tags": "reso",
    },
    ("lupe", "attack"): {
        "trigger": "attack",
        "enabled": "true",
        "loopEnabled": "false",
        "start": "0",
        "release": "4",
        "ampVelTrack": "1",
        "tags": "lupe",
    },
    ("lupe", "release"): {
        "trigger": "release",
        "enabled": "false",
        "loopEnabled": "false",
        "start": "0",
        "release": "0",
        "ampVelTrack": "0",
        "tags": "lupe",
    },
}
_SAMPLE_PATH = re.compile(
    r"^assets/wav/(?P<timbre>lupe|reso)/"
    r"(?P<note>\d+)_(?P<note_name>[A-G]#?\d)_"
    r"(?P<layer>\d{2})_(?P<round_robin>\d{2})\.wav$"
)
_KNOWN_BROKEN_LABEL = 'label="Strings <-> Resonance"'
_REPAIRED_LABEL = 'label="Strings &lt;-> Resonance"'


@dataclass(frozen=True, slots=True)
class SampleRecord:
    """One source mapping from the SIMPK DecentSampler preset.

    ``start_raw_samples`` and ``end_raw_samples`` retain the source values.
    The SIMPK exporter counts interleaved PCM sample points rather than audio
    frames; frame conversion therefore requires the WAV channel count.
    """

    sample_path: str
    root_note: int
    velocity_low: int
    velocity_high: int
    round_robin_position: int
    timbre: str
    trigger: str
    enabled: bool
    start_raw_samples: int
    end_raw_samples: int
    release_seconds: float


@dataclass(frozen=True, slots=True)
class ValidatedAttackSample:
    """A playable attack mapping with source boundaries converted to frames."""

    sample_path: str
    sample_file: Path
    root_note: int
    velocity_low: int
    velocity_high: int
    round_robin_position: int
    timbre: str
    channels: int
    frame_count: int
    offset_frames: int
    end_frame_exclusive: int
    release_seconds: float


@dataclass(frozen=True, slots=True)
class ConversionResult:
    normal_sfz: Path
    resonance_sfz: Path
    attack_sample_count: int
    tuning_applied: bool


def _decode_upstream_xml(payload: bytes, source: Path) -> str:
    """Decode the preset strictly, tolerating the two published SIMPK variants.

    The currently published archive is valid UTF-8.  An earlier/package-tool
    variant has also circulated with CP-1252 bytes under the same UTF-8 XML
    declaration, so a strict CP-1252 fallback is intentional.
    """

    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return payload.decode("cp1252")
        except UnicodeDecodeError as error:
            raise ValueError(f"cannot decode SIMPK preset: {source}") from error


def _parse_nonnegative_int(value: str, *, field: str) -> int:
    if re.fullmatch(r"0|[1-9]\d*", value) is None:
        raise ValueError(f"{field} must be a canonical non-negative integer: {value!r}")
    return int(value)


def _midi_note_name(note: int) -> str:
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return f"{names[note % 12]}{note // 12 - 1}"


def _canonical_sample_path(raw_path: str) -> str:
    normalized = raw_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError(f"unsafe SIMPK sample path: {raw_path!r}")
    return path.as_posix()


def _zone_key(record: SampleRecord) -> tuple[int, int, int, int]:
    return (
        record.root_note,
        record.velocity_low,
        record.velocity_high,
        record.round_robin_position,
    )


def _record_sort_key(
    record: SampleRecord,
) -> tuple[str, str, int, int, int]:
    return (
        record.timbre,
        record.trigger,
        record.root_note,
        record.velocity_low,
        record.round_robin_position,
    )


def _expected_zone_keys() -> set[tuple[int, int, int, int]]:
    return {
        (note, velocity_low, velocity_high, round_robin)
        for note in range(NOTE_MIN, NOTE_MAX + 1)
        for velocity_low, velocity_high in VELOCITY_LAYERS
        for round_robin in range(1, ROUND_ROBIN_LENGTH + 1)
    }


def parse_dspreset(path: Path) -> tuple[SampleRecord, ...]:
    """Parse and strictly validate the published SIMPK clavichord mapping.

    All four source groups are returned, including the two disabled release
    groups.  Paths are canonical source-root-relative POSIX strings so the
    calibration table and generated SFZ files use exactly the same keys.
    """

    source = Path(path)
    text = _decode_upstream_xml(source.read_bytes(), source)
    broken_label_count = text.count(_KNOWN_BROKEN_LABEL)
    if broken_label_count > 1:
        raise ValueError("SIMPK preset contains more than one known malformed label")
    if broken_label_count:
        text = text.replace(_KNOWN_BROKEN_LABEL, _REPAIRED_LABEL)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise ValueError(f"invalid SIMPK DecentSampler XML: {source}: {error}") from error

    if root.tag != "DecentSampler":
        raise ValueError(f"unexpected SIMPK XML root: {root.tag!r}")
    groups_elements = root.findall("groups")
    if len(groups_elements) != 1:
        raise ValueError("SIMPK preset must contain exactly one <groups> element")
    groups_element = groups_elements[0]
    if groups_element.attrib != {"seqMode": "round_robin"}:
        raise ValueError(
            f"unexpected SIMPK <groups> attributes: {groups_element.attrib!r}"
        )
    if any(child.tag != "group" for child in groups_element):
        raise ValueError("SIMPK <groups> may contain only <group> children")
    groups = groups_element.findall("group")
    if len(groups) != 4:
        raise ValueError(f"SIMPK preset must contain four groups, found {len(groups)}")

    expected_zones = _expected_zone_keys()
    seen_groups: set[tuple[str, str]] = set()
    all_records: list[SampleRecord] = []
    records_by_group: dict[tuple[str, str], dict[tuple[int, int, int, int], SampleRecord]] = {}

    for group_index, group in enumerate(groups):
        if set(group.attrib) != _GROUP_ATTRIBUTES:
            raise ValueError(
                f"SIMPK group {group_index} has unexpected attributes: "
                f"{sorted(set(group.attrib) ^ _GROUP_ATTRIBUTES)!r}"
            )
        timbre = group.attrib["tags"]
        trigger = group.attrib["trigger"]
        group_key = (timbre, trigger)
        expected_group = _EXPECTED_GROUP_VALUES.get(group_key)
        if expected_group is None or group_key in seen_groups:
            raise ValueError(f"unexpected or duplicate SIMPK group: {group_key!r}")
        if group.attrib != expected_group:
            raise ValueError(
                f"SIMPK group {group_key!r} changed: "
                f"expected {expected_group!r}, found {group.attrib!r}"
            )
        seen_groups.add(group_key)
        enabled = group.attrib["enabled"] == "true"
        release_seconds = float(group.attrib["release"])

        zone_records: dict[tuple[int, int, int, int], SampleRecord] = {}
        samples = group.findall("sample")
        if any(child.tag != "sample" for child in group):
            raise ValueError(
                f"SIMPK group {group_key!r} may contain only <sample> children"
            )
        if len(samples) != len(expected_zones):
            raise ValueError(
                f"SIMPK group {group_key!r} must contain {len(expected_zones)} "
                f"samples, found {len(samples)}"
            )
        for sample_index, sample in enumerate(samples):
            if len(sample):
                raise ValueError(
                    f"SIMPK sample {group_key!r}/{sample_index} must be empty"
                )
            if set(sample.attrib) != _SAMPLE_ATTRIBUTES:
                raise ValueError(
                    f"SIMPK sample {group_key!r}/{sample_index} has unexpected "
                    f"attributes: {sorted(set(sample.attrib) ^ _SAMPLE_ATTRIBUTES)!r}"
                )
            root_note = _parse_nonnegative_int(
                sample.attrib["rootNote"], field="rootNote"
            )
            low_note = _parse_nonnegative_int(sample.attrib["loNote"], field="loNote")
            high_note = _parse_nonnegative_int(
                sample.attrib["hiNote"], field="hiNote"
            )
            velocity_low = _parse_nonnegative_int(
                sample.attrib["loVel"], field="loVel"
            )
            velocity_high = _parse_nonnegative_int(
                sample.attrib["hiVel"], field="hiVel"
            )
            round_robin = _parse_nonnegative_int(
                sample.attrib["seqPosition"], field="seqPosition"
            )
            start = _parse_nonnegative_int(sample.attrib["start"], field="start")
            end = _parse_nonnegative_int(sample.attrib["end"], field="end")
            if low_note != root_note or high_note != root_note:
                raise ValueError(
                    f"SIMPK sample must use an exact-key zone: {sample.attrib['path']!r}"
                )
            if end <= start:
                raise ValueError(
                    f"SIMPK sample has an empty/reversed boundary: "
                    f"{sample.attrib['path']!r}"
                )

            sample_path = _canonical_sample_path(sample.attrib["path"])
            path_match = _SAMPLE_PATH.fullmatch(sample_path)
            if path_match is None:
                raise ValueError(f"unexpected SIMPK sample path: {sample_path!r}")
            if path_match.group("timbre") != timbre:
                raise ValueError(
                    f"SIMPK sample path/group timbre mismatch: {sample_path!r}"
                )
            layer_index = VELOCITY_LAYERS.index(
                (velocity_low, velocity_high)
            ) if (velocity_low, velocity_high) in VELOCITY_LAYERS else -1
            expected_note_name = _midi_note_name(root_note)
            if (
                int(path_match.group("note")) != root_note
                or path_match.group("note_name") != expected_note_name
                or int(path_match.group("layer")) != layer_index
                or int(path_match.group("round_robin")) != round_robin - 1
            ):
                raise ValueError(
                    f"SIMPK filename disagrees with its mapping: {sample_path!r}"
                )

            record = SampleRecord(
                sample_path=sample_path,
                root_note=root_note,
                velocity_low=velocity_low,
                velocity_high=velocity_high,
                round_robin_position=round_robin,
                timbre=timbre,
                trigger=trigger,
                enabled=enabled,
                start_raw_samples=start,
                end_raw_samples=end,
                release_seconds=release_seconds,
            )
            key = _zone_key(record)
            if key in zone_records:
                raise ValueError(
                    f"duplicate SIMPK zone in group {group_key!r}: {key!r}"
                )
            zone_records[key] = record
            all_records.append(record)

        actual_zones = set(zone_records)
        if actual_zones != expected_zones:
            missing = sorted(expected_zones - actual_zones)[:5]
            extra = sorted(actual_zones - expected_zones)[:5]
            raise ValueError(
                f"SIMPK group {group_key!r} has wrong note/velocity/RR coverage; "
                f"missing={missing!r}, unexpected={extra!r}"
            )
        records_by_group[group_key] = zone_records

    if seen_groups != set(_EXPECTED_GROUP_VALUES):
        raise ValueError(
            f"SIMPK group set changed: "
            f"missing={sorted(set(_EXPECTED_GROUP_VALUES) - seen_groups)!r}"
        )

    for timbre in TIMBRES:
        attacks = records_by_group[(timbre, "attack")]
        releases = records_by_group[(timbre, "release")]
        for key in sorted(expected_zones):
            attack = attacks[key]
            release = releases[key]
            if attack.sample_path != release.sample_path:
                raise ValueError(
                    f"SIMPK attack/release paths differ for {timbre}/{key!r}"
                )
            if attack.start_raw_samples != 0:
                raise ValueError(
                    f"SIMPK attack offset changed for {attack.sample_path!r}: "
                    f"{attack.start_raw_samples}"
                )
            # This exact one-raw-sample overlap is present in all 756 source
            # attack/release pairs and is evidence for the SIMPK exporter's
            # source-specific boundary convention.
            if release.start_raw_samples != attack.end_raw_samples - 1:
                raise ValueError(
                    f"SIMPK attack/release boundary changed for "
                    f"{attack.sample_path!r}"
                )

    return tuple(sorted(all_records, key=_record_sort_key))


def _safe_sample_file(source_root: Path, sample_path: str) -> Path:
    candidate = (source_root / PurePosixPath(sample_path)).resolve()
    try:
        candidate.relative_to(source_root)
    except ValueError as error:
        raise ValueError(f"SIMPK sample escapes source root: {sample_path!r}") from error
    if not candidate.is_file():
        raise ValueError(f"SIMPK sample file does not exist: {candidate}")
    return candidate


def _wav_metadata(path: Path) -> tuple[int, int]:
    try:
        with wave.open(str(path), "rb") as input_file:
            channels = input_file.getnchannels()
            sample_width = input_file.getsampwidth()
            sample_rate = input_file.getframerate()
            frame_count = input_file.getnframes()
            compression = input_file.getcomptype()
    except (wave.Error, EOFError) as error:
        raise ValueError(f"cannot read SIMPK WAV metadata: {path}: {error}") from error
    if (
        channels != 2
        or sample_width != 3
        or sample_rate != 48000
        or compression != "NONE"
        or frame_count <= 0
    ):
        raise ValueError(
            f"unexpected SIMPK WAV format for {path}: channels={channels}, "
            f"sample_width={sample_width}, sample_rate={sample_rate}, "
            f"compression={compression!r}, frames={frame_count}"
        )
    return channels, frame_count


def validate_simpk_source(source_root: Path) -> tuple[ValidatedAttackSample, ...]:
    """Validate every source WAV and return the 756 playable attack mappings."""

    root = Path(source_root).resolve()
    if not root.is_dir():
        raise ValueError(f"SIMPK source root does not exist: {root}")
    records = parse_dspreset(root / PRESET_NAME)
    by_group = {
        (record.timbre, record.trigger, _zone_key(record)): record
        for record in records
    }
    attacks = [
        record
        for record in records
        if record.trigger == "attack" and record.enabled
    ]
    expected_attack_count = (
        len(TIMBRES)
        * (NOTE_MAX - NOTE_MIN + 1)
        * len(VELOCITY_LAYERS)
        * ROUND_ROBIN_LENGTH
    )
    if len(attacks) != expected_attack_count:
        raise ValueError(
            f"SIMPK playable attack count changed: "
            f"expected {expected_attack_count}, found {len(attacks)}"
        )

    validated: list[ValidatedAttackSample] = []
    seen_paths: set[str] = set()
    for attack in attacks:
        if attack.sample_path in seen_paths:
            raise ValueError(
                f"SIMPK playable sample path is duplicated: {attack.sample_path!r}"
            )
        seen_paths.add(attack.sample_path)
        sample_file = _safe_sample_file(root, attack.sample_path)
        channels, frame_count = _wav_metadata(sample_file)
        release = by_group[(attack.timbre, "release", _zone_key(attack))]
        raw_sample_count = frame_count * channels
        if release.end_raw_samples != raw_sample_count:
            raise ValueError(
                f"SIMPK release end does not equal WAV frames*channels for "
                f"{attack.sample_path!r}: {release.end_raw_samples} != "
                f"{frame_count}*{channels}"
            )
        if (
            attack.start_raw_samples % channels != 0
            or attack.end_raw_samples % channels != 0
        ):
            raise ValueError(
                f"SIMPK attack boundary is not frame-aligned for "
                f"{attack.sample_path!r}"
            )
        offset_frames = attack.start_raw_samples // channels
        end_frame_exclusive = attack.end_raw_samples // channels
        if not 0 <= offset_frames < end_frame_exclusive <= frame_count:
            raise ValueError(
                f"SIMPK attack frame boundary is invalid for "
                f"{attack.sample_path!r}: {offset_frames}:{end_frame_exclusive}/"
                f"{frame_count}"
            )
        validated.append(
            ValidatedAttackSample(
                sample_path=attack.sample_path,
                sample_file=sample_file,
                root_note=attack.root_note,
                velocity_low=attack.velocity_low,
                velocity_high=attack.velocity_high,
                round_robin_position=attack.round_robin_position,
                timbre=attack.timbre,
                channels=channels,
                frame_count=frame_count,
                offset_frames=offset_frames,
                end_frame_exclusive=end_frame_exclusive,
                release_seconds=attack.release_seconds,
            )
        )
    return tuple(
        sorted(
            validated,
            key=lambda item: (
                item.timbre,
                item.root_note,
                item.velocity_low,
                item.round_robin_position,
            ),
        )
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key is not allowed: {key!r}")
        result[key] = value
    return result


def load_tuning_table(
    path: Path,
    *,
    expected_sample_paths: Iterable[str],
) -> dict[str, float]:
    """Load a complete, exact path-to-measured-detune table."""

    source = Path(path)
    try:
        raw = json.loads(
            source.read_text(encoding="utf-8-sig"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"invalid SIMPK tuning JSON: {source}: {error}") from error
    if not isinstance(raw, dict):
        raise ValueError("SIMPK tuning table must be a JSON object")
    expected_top_level = {"schema_version", "unit", "measured_detune_cents"}
    if set(raw) != expected_top_level:
        raise ValueError(
            f"SIMPK tuning table keys must be {sorted(expected_top_level)!r}"
        )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise ValueError("SIMPK tuning schema_version must be integer 1")
    if raw["unit"] != "cents":
        raise ValueError("SIMPK tuning unit must be 'cents'")
    values = raw["measured_detune_cents"]
    if not isinstance(values, dict):
        raise ValueError("measured_detune_cents must be a JSON object")

    converted: dict[str, float] = {}
    for sample_path, value in values.items():
        if (
            not isinstance(sample_path, str)
            or _canonical_sample_path(sample_path) != sample_path
        ):
            raise ValueError(
                f"tuning-table path must be canonical source-relative POSIX: "
                f"{sample_path!r}"
            )
        if type(value) not in (int, float):
            raise ValueError(
                f"measured detune must be a JSON number for {sample_path!r}"
            )
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(
                f"measured detune must be finite for {sample_path!r}"
            )
        converted[sample_path] = number

    expected = set(expected_sample_paths)
    actual = set(converted)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(
            "SIMPK tuning table must cover every playable attack sample exactly; "
            f"missing_count={len(missing)}, missing={missing[:5]!r}, "
            f"unexpected_count={len(unexpected)}, unexpected={unexpected[:5]!r}"
        )
    return converted


def _format_sfz_number(value: float) -> str:
    if value == 0.0:
        return "0"
    return repr(float(value))


def _relative_sfz_sample_path(sample_file: Path, output_directory: Path) -> str:
    return os.path.relpath(sample_file, output_directory).replace("\\", "/")


def _render_sfz(
    samples: Sequence[ValidatedAttackSample],
    *,
    timbre: str,
    output_directory: Path,
    tuning: dict[str, float] | None,
) -> str:
    selected = [sample for sample in samples if sample.timbre == timbre]
    expected_count = (
        (NOTE_MAX - NOTE_MIN + 1)
        * len(VELOCITY_LAYERS)
        * ROUND_ROBIN_LENGTH
    )
    if len(selected) != expected_count:
        raise ValueError(
            f"cannot render {timbre!r} SFZ: expected {expected_count} samples, "
            f"found {len(selected)}"
        )
    lines = [
        "// Generated deterministically from SIMPK_03_Clavichord.",
        "// The published rootNote map is one octave above the recorded pitch;",
        "// keys below preserve the recordings at their measured native octave.",
        "// Source end values are exclusive interleaved PCM positions;",
        "// SFZ end below is an inclusive audio-frame position.",
        "<global> loop_mode=no_loop ampeg_release=4 seq_length=2",
    ]
    for sample in sorted(
        selected,
        key=lambda item: (
            item.root_note,
            item.velocity_low,
            item.round_robin_position,
        ),
    ):
        if sample.release_seconds != 4.0:
            raise ValueError(
                f"unexpected attack release for {sample.sample_path!r}: "
                f"{sample.release_seconds}"
            )
        sfz_end_inclusive = sample.end_frame_exclusive - 1
        playback_note = sample.root_note + PLAYBACK_NOTE_OFFSET
        if not PLAYBACK_NOTE_MIN <= playback_note <= PLAYBACK_NOTE_MAX:
            raise ValueError(
                f"SIMPK playback note is outside the audited native range for "
                f"{sample.sample_path!r}: {playback_note}"
            )
        opcodes = [
            f'sample="{_relative_sfz_sample_path(sample.sample_file, output_directory)}"',
            f"key={playback_note}",
            f"lovel={sample.velocity_low}",
            f"hivel={sample.velocity_high}",
            f"seq_position={sample.round_robin_position}",
            f"offset={sample.offset_frames}",
            f"end={sfz_end_inclusive}",
        ]
        if tuning is not None:
            measured_detune = tuning[sample.sample_path]
            opcodes.append(f"tune={_format_sfz_number(-measured_detune)}")
        lines.append("<region> " + " ".join(opcodes))
    return "\n".join(lines) + "\n"


def _safe_output_directory(source_root: Path, output_directory: Path) -> Path:
    resolved = output_directory.resolve()
    try:
        resolved.relative_to(source_root)
    except ValueError as error:
        raise ValueError(
            f"SIMPK output directory must stay inside source root: {resolved}"
        ) from error
    return resolved


def convert_simpk_clavichord(
    source_root: Path,
    *,
    output_directory: Path | None = None,
    tuning_table: Path | None = None,
    require_complete_tuning: bool = False,
) -> ConversionResult:
    """Validate the source and generate normal/resonance SFZ mappings."""

    root = Path(source_root).resolve()
    output = _safe_output_directory(
        root,
        (
            Path(output_directory)
            if output_directory is not None
            else root / DEFAULT_OUTPUT_DIRECTORY
        ),
    )
    samples = validate_simpk_source(root)
    expected_paths = {sample.sample_path for sample in samples}
    if require_complete_tuning and tuning_table is None:
        raise ValueError(
            "formal SIMPK conversion requires --tuning-table with complete coverage"
        )
    tuning = (
        load_tuning_table(
            Path(tuning_table),
            expected_sample_paths=expected_paths,
        )
        if tuning_table is not None
        else None
    )

    normal_text = _render_sfz(
        samples,
        timbre="lupe",
        output_directory=output,
        tuning=tuning,
    )
    resonance_text = _render_sfz(
        samples,
        timbre="reso",
        output_directory=output,
        tuning=tuning,
    )
    output.mkdir(parents=True, exist_ok=True)
    normal_path = output / NORMAL_SFZ_NAME
    resonance_path = output / RESONANCE_SFZ_NAME
    normal_path.write_text(normal_text, encoding="utf-8", newline="\n")
    resonance_path.write_text(resonance_text, encoding="utf-8", newline="\n")
    return ConversionResult(
        normal_sfz=normal_path,
        resonance_sfz=resonance_path,
        attack_sample_count=len(samples),
        tuning_applied=tuning is not None,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate and convert the SIMPK clavichord DecentSampler preset."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help=f"Directory containing {PRESET_NAME} and assets/.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=f"Generated SFZ directory (default: source-root/{DEFAULT_OUTPUT_DIRECTORY}).",
    )
    parser.add_argument(
        "--tuning-table",
        type=Path,
        help="Strict complete measured-detune JSON table.",
    )
    parser.add_argument(
        "--require-complete-tuning",
        action="store_true",
        help="Fail unless a complete tuning table is supplied.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _argument_parser().parse_args(argv)
    result = convert_simpk_clavichord(
        arguments.source_root,
        output_directory=arguments.output_dir,
        tuning_table=arguments.tuning_table,
        require_complete_tuning=arguments.require_complete_tuning,
    )
    print(f"normal_sfz={result.normal_sfz}")
    print(f"resonance_sfz={result.resonance_sfz}")
    print(f"attack_samples={result.attack_sample_count}")
    print(f"tuning_applied={str(result.tuning_applied).lower()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
