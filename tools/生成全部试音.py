"""重新生成全部正式声音入口的试听与机器核验报告。

每件乐器只渲染一次，WAV 输出目录镜像 ``乐器/`` 的分类层级，例如：

``output/全音域试音/管弦乐/弦乐组/小提琴.wav``

默认 ``ascending-scale`` 协议通常使用默认奏法，以固定力度和时值从每件乐器的
声明最低键升到最高键，合法区间内逐整数半音/触发键演奏。note 事件生命周期
不重叠，但普通乐器的实际释音可能跨入后续半音；因此它是紧凑的映射/复音压力
扫描，不是隔离音色结论。
事件谱原子写入 ``examples/全音域上行``，输出目录同时写入范围、空洞、键数和
哈希清单；长尾/打击映射例外会逐件记录。旧按乐器定制的谱例仍可用
``--profile existing`` 选择。

整批内容先写入临时目录。只要任意一件渲染失败，已有全音域试听目录和全部旧
报告都保持不变；全音域协议的事件目录也不会留下半批文件。全部成功后才替换
``output/全音域试音``、``examples/全音域上行`` 并逐文件原子替换报告。
旧 ``output/试音`` 永远不会被本工具改动。参考振荡器是测试工具，不计入正式
声音入口，也不会生成试听。

注意：这是耗时的全量任务。单元测试只会替换渲染函数，不会真的渲染全部未隔离
入口。登记总数仍按 103 件守护；``license_status=quarantined`` 的入口不渲染，
其既有可复现事件谱会在原子替换时保留。
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.catalog import discover_instruments
from tianlai.audition_protocol import (
    A4_HZ,
    CHANNELS,
    INTER_NOTE_GAP_SECONDS,
    NOTE_DURATION_SECONDS,
    PROTOCOL_ID,
    SAMPLE_RATE,
    TAIL_SECONDS,
    VELOCITY,
    FullRangeAudition,
    build_full_range_audition,
)
from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.dedicated_candidates import (
    generate_dedicated_audition_verification,
)


INSTRUMENT_ROOT = ROOT / "乐器"
# 只替换新协议目录；旧 output/试音 是不可变历史试听，永远不得触碰。
OUTPUT_ROOT = ROOT / "output" / "全音域试音"
EXAMPLES = ROOT / "examples"
FULL_RANGE_EVENTS_ROOT = EXAMPLES / "全音域上行"
TEST_TOOL = "测试工具/参考振荡器"
EXPECTED_INSTRUMENT_COUNT: int | None = 103
MIN_AUDITION_PEAK = 1.0e-6
MIN_AUDITION_RMS = 1.0e-8

_MANUAL_REVIEW_SUFFIX = "_review"
PROFILE_EXISTING = "existing"
PROFILE_ASCENDING_SCALE = "ascending-scale"
DEFAULT_PROFILE = PROFILE_ASCENDING_SCALE
PROFILES = (PROFILE_ASCENDING_SCALE, PROFILE_EXISTING)

_OBJECTIVE_REPORT_FIELDS = frozenset(
    {
        "status",
        "rendered_at",
        "platform",
        "sample_rate",
        "channels",
        "subtype",
        "frame_count",
        "duration_seconds",
        "peak_active_voices",
        "peak",
        "rms",
        "clipped_samples",
        "wav",
        "wav_persistence",
        "wav_sha256",
        "hash_algorithm",
        "canonicalization",
        "manifest_canonical_sha256",
        # Read-only compatibility with reports created before canonical JSON
        # identity was introduced. New reports never write these two fields.
        "manifest_sha256",
        "events",
        "events_canonical_sha256",
        "events_sha256",
        "coverage",
        "human_review",
        "audition_profile",
        "audition_protocol",
        "review_evidence_status",
        "specialized_machine_evidence_status",
        "preserved_specialized_machine_fields",
        "previous_protocol_evidence",
    }
)


class AuditionBatchError(RuntimeError):
    """One or more auditions failed before the batch could be committed."""

    def __init__(self, failures: list[tuple[str, str]]) -> None:
        self.failures = failures
        super().__init__(f"{len(failures)} 件试听生成失败")


class AuditionRollbackError(RuntimeError):
    """A failed commit whose recovery material must not be deleted."""

    def __init__(self, message: str, recovery_path: Path) -> None:
        self.recovery_path = recovery_path.resolve()
        super().__init__(
            f"{message}；恢复材料已保留：{self.recovery_path}"
        )


def _normalize_only_selector(raw: str) -> str:
    selector = str(raw).strip().replace("\\", "/")
    if not selector:
        raise ValueError("--only 不得为空")
    components = selector.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise ValueError(
            f"--only 必须是规范的乐器相对路径或唯一名称：{raw!r}"
        )
    path = PurePosixPath(selector)
    if path.is_absolute():
        raise ValueError(f"--only 不得使用绝对路径：{raw!r}")
    return path.as_posix()


def _select_only_entries(
    entries: list[Any],
    selectors: tuple[str, ...],
) -> list[Any]:
    """Resolve exact catalog-relative paths or unambiguous directory names."""

    by_relative: dict[str, Any] = {}
    by_name: dict[str, list[tuple[str, Any]]] = {}
    for entry in entries:
        directory = Path(entry.manifest_path).parent
        relative = directory.relative_to(INSTRUMENT_ROOT).as_posix()
        if relative in by_relative:
            raise ValueError(f"正式乐器目录重复：{relative}")
        by_relative[relative] = entry
        by_name.setdefault(directory.name, []).append((relative, entry))

    selected_relatives: set[str] = set()
    for raw in selectors:
        selector = _normalize_only_selector(raw)
        if selector in by_relative:
            selected_relatives.add(selector)
            continue
        if "/" in selector:
            raise ValueError(f"--only 未匹配正式乐器相对路径：{selector}")
        matches = by_name.get(selector, [])
        if not matches:
            raise ValueError(f"--only 未匹配正式乐器唯一名称：{selector}")
        if len(matches) != 1:
            labels = "、".join(relative for relative, _entry in matches)
            raise ValueError(
                f"--only 名称 {selector!r} 不唯一，请改用相对路径：{labels}"
            )
        selected_relatives.add(matches[0][0])

    return [
        entry
        for relative, entry in by_relative.items()
        if relative in selected_relatives
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audition_event_candidates(directory: Path) -> list[Path]:
    """Return legacy root-level fixed-audition examples for one instrument.

    Pitch-probe files such as ``小提琴_A4_音准.events.json`` are calibration
    inputs, not auditions, and must never become a fallback audition score.
    ``--profile existing`` intentionally does not recurse into
    ``examples/全音域上行`` even after the current report points there.
    """

    return [
        path
        for path in sorted(EXAMPLES.glob(f"{directory.name}_*.events.json"))
        if "_音准" not in path.stem
    ]


def _events_for(directory: Path) -> Path:
    """Resolve the current audition score without accepting another instrument's.

    A still-current hash in the old report wins. If that report is stale (for
    example while an instrument is being upgraded), the unique current score
    matching the instrument directory name is used.
    """

    candidates = _audition_event_candidates(directory)
    if not candidates:
        raise ValueError(f"找不到 {directory.name} 的固定试听谱例")

    audition = directory / "试听核验.json"
    if audition.is_file():
        report = json.loads(audition.read_text(encoding="utf-8"))
        recorded = str(report.get("events_canonical_sha256", ""))
        if recorded:
            for path in candidates:
                if canonical_json_file_sha256(path) == recorded:
                    return path
        legacy_recorded = str(report.get("events_sha256", ""))
        if legacy_recorded:
            for path in candidates:
                if _sha256(path) == legacy_recorded:
                    return path

    if len(candidates) != 1:
        names = "、".join(path.name for path in candidates)
        raise ValueError(
            f"{directory.name} 的旧 events 哈希已过期，且存在多个试听谱例：{names}"
        )
    return candidates[0]


def _existing_profile_coverage(events: Path) -> list[str]:
    return [
        f"旧固定谱例复算：{_relative_wav_label(events)}",
        (
            "仅核验该谱例实际包含的事件；不声明全音域、全部触发键、"
            "统一力度或全部奏法覆盖"
        ),
    ]


def _load_previous_report(directory: Path) -> dict[str, Any]:
    path = directory / "试听核验.json"
    if not path.is_file():
        raise FileNotFoundError(f"缺少旧试听报告：{path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    coverage = report.get("coverage")
    if not isinstance(coverage, list) or not all(
        isinstance(item, str) for item in coverage
    ):
        raise ValueError(f"{path} 的 coverage 必须是字符串数组")
    return report


def _relative_wav_label(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _preserve_manual_fields(
    fresh: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """Keep human decisions/queues while replacing objective machine evidence."""

    for key, value in previous.items():
        if key not in fresh and key not in _OBJECTIVE_REPORT_FIELDS:
            fresh[key] = value
    return fresh


def _reset_review_and_archive_specialized_evidence(
    fresh: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """Reset human state while retaining old event-bound evidence honestly.

    Special one-off gates (for example the accordion's old time-window signal
    checks) must not masquerade as measurements of the new full-range WAV.
    They remain available under ``previous_protocol_evidence`` together with
    the hashes that identify the exact score and audio they described.
    """

    manual_fields = {
        key
        for key in previous
        if key.endswith(_MANUAL_REVIEW_SUFFIX)
        or key in {"human_review", "semantic_review", "spectral_pitch_review"}
    }
    specialized = {
        key: value
        for key, value in previous.items()
        if key not in _OBJECTIVE_REPORT_FIELDS and key not in manual_fields
    }
    archived = previous.get("previous_protocol_evidence")
    if specialized:
        archived = {
            "status": "superseded_event_bound_machine_evidence",
            "wav_sha256": str(previous.get("wav_sha256", "")),
            "fields": specialized,
        }
        if previous.get("events_canonical_sha256"):
            archived.update(
                {
                    "hash_algorithm": str(
                        previous.get("hash_algorithm", HASH_ALGORITHM)
                    ),
                    "canonicalization": str(
                        previous.get("canonicalization", CANONICALIZATION)
                    ),
                    "events_canonical_sha256": str(
                        previous["events_canonical_sha256"]
                    ),
                }
            )
        elif previous.get("events_sha256"):
            archived.update(
                {
                    "hash_algorithm": HASH_ALGORITHM,
                    "hash_semantics": "file-bytes",
                    "events_sha256": str(previous["events_sha256"]),
                }
            )
    if archived is not None:
        fresh["previous_protocol_evidence"] = archived
        fresh["specialized_machine_evidence_status"] = (
            "previous_protocol_only_not_current_measurement"
        )
    fresh["human_review"] = "pending"
    fresh["review_evidence_status"] = (
        "pending_new_review_old_hash_bound_reviews_do_not_apply"
    )
    return fresh


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_report_signal(
    report: dict[str, Any],
    *,
    profile: str,
) -> None:
    peak = float(report["peak"])
    rms = float(report["rms"])
    clipped = int(report["clipped_samples"])
    if (
        not math.isfinite(peak)
        or not math.isfinite(rms)
        or peak <= MIN_AUDITION_PEAK
        or rms <= MIN_AUDITION_RMS
    ):
        raise ValueError(
            "试听未通过整件非静音门："
            f"peak={peak:.9g}（需 > {MIN_AUDITION_PEAK:g}），"
            f"rms={rms:.9g}（需 > {MIN_AUDITION_RMS:g}）"
        )
    if profile == PROFILE_ASCENDING_SCALE and (
        clipped != 0 or peak >= 0.999
    ):
        raise ValueError(
            "全音域试听未通过幅度门："
            f"peak={peak:.6f}, clipped_samples={clipped}"
        )


def _full_range_events_path(relative: Path) -> Path:
    return (
        FULL_RANGE_EVENTS_ROOT
        / relative.parent
        / f"{relative.name}_全音域上行.events.json"
    )


def _write_batch_manifest(
    staged_output: Path,
    entries: list[dict[str, Any]],
) -> None:
    document: dict[str, Any] = {
        "schema_version": 2,
        "profile": PROFILE_ASCENDING_SCALE,
        "protocol": PROTOCOL_ID,
        "generated_on": _datetime.date.today().isoformat(),
        "wav_persistence": "temporary",
        "instrument_count": len(entries),
        "settings": {
            "pitch_unit": "concert_midi_note_or_unpitched_trigger_key",
            "temperament": "equal",
            "a4_hz": A4_HZ,
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "velocity": VELOCITY,
            "default_gate_seconds": NOTE_DURATION_SECONDS,
            "default_gap_seconds": INTER_NOTE_GAP_SECONDS,
            "default_tail_seconds": TAIL_SECONDS,
            "single_note_events_overlap": False,
            "single_note_event_lifetimes_overlap": False,
            "audio_release_tails_may_overlap": True,
            "timing_semantics": (
                "常规批次是紧凑压力扫描；note事件不重叠不代表声音释音不重叠。"
                "音色修复验收应使用 full-range-chromatic-isolated-v1。"
            ),
            "minimum_peak": MIN_AUDITION_PEAK,
            "minimum_rms": MIN_AUDITION_RMS,
            "maximum_peak_exclusive": 0.999,
        },
        "review_notice": (
            "本批 WAV 与 events 使用新哈希；任何绑定旧 wav_sha256 或 "
            "events_canonical_sha256 的人工听审结论均不适用于本批。"
        ),
        "ordering": (
            "按乐器目录相对路径排序；每件内部按全部声明合法整数 MIDI 键升序，"
            "显式音域空洞不触发且列在 gaps。"
        ),
        "instruments": entries,
    }
    _write_json(staged_output / "_试听清单.json", document)

    (staged_output / "_试听顺序.txt").write_text(
        _render_batch_order(entries),
        encoding="utf-8",
    )


def _render_batch_order(entries: list[dict[str, Any]]) -> str:
    """Return the human-readable view bound to one full batch roster."""

    lines = [
        f"天籁 {len(entries)} 件乐器全音域上行试听顺序",
        f"协议：{PROTOCOL_ID}",
        (
            f"常规设置：力度 {VELOCITY:g}，gate "
            f"{NOTE_DURATION_SECONDS:g}s，间隔 "
            f"{INTER_NOTE_GAP_SECONDS:g}s；note事件不重叠，但普通乐器的"
            "声音释音可能跨入后续半音，本批按压力扫描理解。"
        ),
        "显式音域空洞不会触发，请以 _试听清单.json 的 gaps 为准。",
        "",
    ]
    for entry in entries:
        spans = "、".join(
            (
                f"{low}"
                if low == high
                else f"{low}-{high}"
            )
            for low, high in entry["declared_ranges"]
        )
        gap_label = ""
        if entry["gaps"]:
            gaps = "、".join(
                f"{low}" if low == high else f"{low}-{high}"
                for low, high in entry["gaps"]
            )
            gap_label = f"；空洞 {gaps}"
        lines.append(
            f"{entry['order']:03d}. {entry['instrument']} | "
            f"MIDI {spans} | {entry['key_count']} 键{gap_label}"
        )
    return "\n".join(lines) + "\n"


def _load_selective_batch_manifest(
    selected_instruments: tuple[str, ...],
    expected_instruments: tuple[str, ...],
) -> tuple[dict[str, Any], bytes, bytes, dict[str, int]] | None:
    """Validate the current full batch before a selective rebuild.

    The temporary output tree can legitimately exist without a full-batch
    manifest (for example after an earlier selective first build).  In that
    state there is no full-batch hash reference to update, so no partial
    manifest is invented.  If a manifest does exist, it is strict input.
    """

    manifest_path = OUTPUT_ROOT / "_试听清单.json"
    order_path = OUTPUT_ROOT / "_试听顺序.txt"
    if manifest_path.is_symlink():
        raise ValueError(f"选择性试听批次清单不得是符号链接：{manifest_path}")
    if not manifest_path.exists():
        if order_path.is_symlink() or order_path.exists():
            raise ValueError(
                "选择性试听存在顺序文本但缺少批次清单；拒绝继续"
            )
        return None
    if not manifest_path.is_file():
        raise ValueError(f"选择性试听批次清单不是普通文件：{manifest_path}")
    if order_path.is_symlink() or not order_path.is_file():
        raise ValueError(
            f"选择性试听批次顺序必须是普通文件：{order_path}"
        )

    original_bytes = manifest_path.read_bytes()
    original_order_bytes = order_path.read_bytes()
    try:
        document = json.loads(original_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"选择性试听批次清单不是有效 UTF-8 JSON：{manifest_path}"
        ) from error
    try:
        original_order_text = original_order_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(
            f"选择性试听批次顺序不是有效 UTF-8：{order_path}"
        ) from error
    original_order_text = original_order_text.replace("\r\n", "\n").replace(
        "\r",
        "\n",
    )
    if not isinstance(document, dict):
        raise ValueError("选择性试听批次清单根节点必须是对象")
    if document.get("schema_version") != 2:
        raise ValueError("选择性试听批次清单 schema_version 必须为 2")
    if document.get("profile") != PROFILE_ASCENDING_SCALE:
        raise ValueError(
            "选择性试听批次清单 profile 必须为 ascending-scale"
        )
    if document.get("protocol") != PROTOCOL_ID:
        raise ValueError(
            f"选择性试听批次清单 protocol 必须为 {PROTOCOL_ID}"
        )
    instruments = document.get("instruments")
    if not isinstance(instruments, list):
        raise ValueError("选择性试听批次清单 instruments 必须是数组")
    instrument_count = document.get("instrument_count")
    if type(instrument_count) is not int or instrument_count != len(instruments):
        raise ValueError(
            "选择性试听批次清单 instrument_count 与 instruments 数量不一致"
        )

    positions: dict[str, int] = {}
    for index, entry in enumerate(instruments):
        if not isinstance(entry, dict):
            raise ValueError(
                f"选择性试听批次清单 instruments[{index}] 必须是对象"
            )
        instrument = entry.get("instrument")
        if not isinstance(instrument, str) or not instrument:
            raise ValueError(
                f"选择性试听批次清单 instruments[{index}].instrument 无效"
            )
        if instrument in positions:
            raise ValueError(f"选择性试听批次清单乐器重复：{instrument}")
        if type(entry.get("order")) is not int:
            raise ValueError(
                f"选择性试听批次清单 {instrument} 缺少整数 order"
            )
        if entry["order"] != index + 1:
            raise ValueError(
                "选择性试听批次清单 order 必须从 1 连续且与数组顺序一致："
                f"{instrument} 实际为 {entry['order']}，期望 {index + 1}"
            )
        if not isinstance(entry.get("declared_ranges"), list):
            raise ValueError(
                f"选择性试听批次清单 {instrument} 缺少 declared_ranges"
            )
        if not isinstance(entry.get("gaps"), list):
            raise ValueError(f"选择性试听批次清单 {instrument} 缺少 gaps")
        if type(entry.get("key_count")) is not int:
            raise ValueError(
                f"选择性试听批次清单 {instrument} 缺少整数 key_count"
            )
        positions[instrument] = index

    actual_instruments = tuple(
        str(entry["instrument"]) for entry in instruments
    )
    if actual_instruments != expected_instruments:
        raise ValueError(
            "选择性试听批次清单与当前完整未隔离乐器 roster/order 不一致："
            f"期望 {expected_instruments!r}，实际 {actual_instruments!r}"
        )
    if instrument_count != len(expected_instruments):
        raise ValueError(
            "选择性试听批次清单数量与当前完整未隔离乐器数量不一致"
        )
    if original_order_text != _render_batch_order(instruments):
        raise ValueError("选择性试听批次顺序文本与当前批次清单不一致")

    selected_positions: dict[str, int] = {}
    for instrument in selected_instruments:
        if instrument not in positions:
            raise ValueError(
                f"选择性试听批次清单找不到所选乐器：{instrument}"
            )
        selected_positions[instrument] = positions[instrument]
    return document, original_bytes, original_order_bytes, selected_positions


def _stage_selective_batch_manifest(
    state: tuple[dict[str, Any], bytes, bytes, dict[str, int]],
    fresh_entries: list[dict[str, Any]],
    staged_manifest_path: Path,
    staged_order_path: Path,
) -> None:
    """Replace selected manifest entries while retaining batch identity/order."""

    document, original_bytes, original_order_bytes, selected_positions = state
    manifest_path = OUTPUT_ROOT / "_试听清单.json"
    order_path = OUTPUT_ROOT / "_试听顺序.txt"
    if (
        manifest_path.is_symlink()
        or not manifest_path.is_file()
        or manifest_path.read_bytes() != original_bytes
        or order_path.is_symlink()
        or not order_path.is_file()
        or order_path.read_bytes() != original_order_bytes
    ):
        raise RuntimeError(
            "选择性试听渲染期间批次清单或顺序文本发生变化；拒绝提交"
        )

    fresh_by_instrument: dict[str, dict[str, Any]] = {}
    for entry in fresh_entries:
        instrument = entry.get("instrument")
        if not isinstance(instrument, str) or not instrument:
            raise ValueError("选择性试听新批次条目缺少 instrument")
        if instrument in fresh_by_instrument:
            raise ValueError(f"选择性试听新批次条目重复：{instrument}")
        fresh_by_instrument[instrument] = entry
    if set(fresh_by_instrument) != set(selected_positions):
        missing = sorted(set(selected_positions) - set(fresh_by_instrument))
        unexpected = sorted(set(fresh_by_instrument) - set(selected_positions))
        raise ValueError(
            "选择性试听新批次条目与所选乐器不一致："
            f"缺少={missing}，多出={unexpected}"
        )

    instruments = document["instruments"]
    original_sequence = [entry["instrument"] for entry in instruments]
    original_count = document["instrument_count"]
    for instrument, index in selected_positions.items():
        previous = instruments[index]
        replacement = dict(fresh_by_instrument[instrument])
        replacement["order"] = previous["order"]
        instruments[index] = replacement
    if [entry["instrument"] for entry in instruments] != original_sequence:
        raise RuntimeError("选择性试听批次条目顺序发生变化；拒绝提交")
    if document["instrument_count"] != original_count:
        raise RuntimeError("选择性试听批次总数发生变化；拒绝提交")
    _write_json(staged_manifest_path, document)
    staged_order_path.write_text(
        _render_batch_order(instruments),
        encoding="utf-8",
    )


def _restore_reports(
    previous_backups: dict[Path, Path],
    installed_reports: list[Path],
) -> None:
    """Restore every report published by this transaction, best effort."""

    errors: list[str] = []
    for destination in reversed(installed_reports):
        try:
            backup = previous_backups.get(destination)
            if backup is not None:
                os.replace(backup, destination)
            elif destination.exists():
                destination.unlink()
        except BaseException as error:  # noqa: BLE001
            errors.append(
                f"{destination}: {type(error).__name__}: {error}"
            )
    if errors:
        raise RuntimeError("；".join(errors))


def _staged_output_artifacts(
    staged_wav: Path,
    final_wav: Path,
) -> dict[Path, Path]:
    """Map one selected WAV and renderer-owned same-name sidecars."""

    if not staged_wav.is_file():
        raise FileNotFoundError(f"试听渲染未生成 WAV：{staged_wav}")
    prefix = staged_wav.name + "."
    artifacts: dict[Path, Path] = {}
    for staged in sorted(staged_wav.parent.iterdir(), key=lambda path: path.name):
        if staged.name != staged_wav.name and not staged.name.startswith(prefix):
            continue
        if staged.is_symlink() or not staged.is_file():
            raise ValueError(f"选择性试听产物必须是普通文件：{staged}")
        artifacts[final_wav.parent / staged.name] = staged
    return artifacts


def _path_is_below(path: Path, root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return bool(relative.parts)


def _commit_selected_files(
    transaction_root: Path,
    staged_files: dict[Path, Path],
) -> None:
    """Replace only selected artifacts, restoring every old byte on failure."""

    if not staged_files:
        raise ValueError("选择性试听提交没有任何文件")
    expected_output_parent = (ROOT / "output").resolve()
    if OUTPUT_ROOT.resolve().parent != expected_output_parent:
        raise ValueError(f"拒绝写入 workspace output 之外的目录：{OUTPUT_ROOT}")
    immutable_legacy = (ROOT / "output" / "试音").resolve()
    if OUTPUT_ROOT.resolve() == immutable_legacy:
        raise ValueError(f"拒绝写入不可变旧试听目录：{immutable_legacy}")
    if FULL_RANGE_EVENTS_ROOT.resolve().parent != EXAMPLES.resolve():
        raise ValueError(
            "拒绝写入 workspace examples 之外的全音域事件目录："
            f"{FULL_RANGE_EVENTS_ROOT}"
        )

    transaction = transaction_root.resolve()
    allowed_roots = (
        OUTPUT_ROOT.resolve(),
        FULL_RANGE_EVENTS_ROOT.resolve(),
        INSTRUMENT_ROOT.resolve(),
    )
    ordered = sorted(
        (
            (Path(destination), Path(staged))
            for destination, staged in staged_files.items()
        ),
        key=lambda item: item[0].resolve().as_posix(),
    )
    staged_identities: set[Path] = set()
    for destination, staged in ordered:
        if not any(_path_is_below(destination, root) for root in allowed_roots):
            raise ValueError(f"选择性试听目标越界：{destination}")
        if destination.is_symlink():
            raise ValueError(f"选择性试听拒绝替换符号链接：{destination}")
        if destination.exists() and not destination.is_file():
            raise ValueError(f"选择性试听目标不是普通文件：{destination}")
        if staged.is_symlink() or not staged.is_file():
            raise ValueError(f"选择性试听 staging 不是普通文件：{staged}")
        staged_identity = staged.resolve()
        try:
            staged_identity.relative_to(transaction)
        except ValueError as error:
            raise ValueError(f"选择性试听 staging 越界：{staged}") from error
        if staged_identity in staged_identities:
            raise ValueError(f"选择性试听 staging 被重复使用：{staged}")
        staged_identities.add(staged_identity)

    backup_root = transaction_root / "previous-selected-files"
    backup_root.mkdir(parents=True, exist_ok=True)
    backups: dict[Path, Path] = {}
    for index, (destination, _staged) in enumerate(ordered):
        if destination.is_file():
            backup = backup_root / f"{index:04d}.previous"
            shutil.copy2(destination, backup)
            backups[destination] = backup

    installed: list[Path] = []
    try:
        for destination, staged in ordered:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
            installed.append(destination)
    except BaseException as commit_error:
        rollback_errors: list[str] = []
        for destination in reversed(installed):
            try:
                backup = backups.get(destination)
                if backup is not None:
                    os.replace(backup, destination)
                elif destination.exists():
                    destination.unlink()
            except BaseException as error:  # noqa: BLE001
                rollback_errors.append(
                    f"{destination}: {type(error).__name__}: {error}"
                )
        if rollback_errors:
            raise AuditionRollbackError(
                "选择性试听提交失败且回滚不完整："
                + "；".join(rollback_errors),
                transaction_root,
            ) from commit_error
        raise


def _commit_batch(
    transaction_root: Path,
    staged_output: Path,
    staged_reports: dict[Path, Path],
    *,
    staged_events: Path | None = None,
) -> None:
    """Commit a fully rendered batch, rolling back ordinary commit failures."""

    OUTPUT_ROOT.parent.mkdir(parents=True, exist_ok=True)
    expected_parent = (ROOT / "output").resolve()
    if OUTPUT_ROOT.resolve().parent != expected_parent:
        raise ValueError(f"拒绝替换 workspace output 之外的目录：{OUTPUT_ROOT}")
    immutable_legacy = (ROOT / "output" / "试音").resolve()
    if OUTPUT_ROOT.resolve() == immutable_legacy:
        raise ValueError(f"拒绝替换不可变旧试听目录：{immutable_legacy}")
    if staged_events is not None:
        FULL_RANGE_EVENTS_ROOT.parent.mkdir(parents=True, exist_ok=True)
        if FULL_RANGE_EVENTS_ROOT.resolve().parent != EXAMPLES.resolve():
            raise ValueError(
                "拒绝替换 workspace examples 之外的全音域事件目录："
                f"{FULL_RANGE_EVENTS_ROOT}"
            )

    report_backup_root = transaction_root / "previous-reports"
    report_backup_root.mkdir(parents=True, exist_ok=True)
    previous_reports: dict[Path, Path] = {}
    for index, (destination, staged) in enumerate(staged_reports.items()):
        if not _path_is_below(destination, INSTRUMENT_ROOT):
            raise ValueError(f"完整试听报告目标越界：{destination}")
        if destination.is_symlink():
            raise ValueError(f"完整试听拒绝替换报告符号链接：{destination}")
        if destination.exists() and not destination.is_file():
            raise ValueError(f"完整试听报告目标不是普通文件：{destination}")
        if staged.is_symlink() or not staged.is_file():
            raise ValueError(f"完整试听 staging 报告不是普通文件：{staged}")
        if destination.is_file():
            backup = report_backup_root / f"{index:04d}.previous.json"
            shutil.copy2(destination, backup)
            previous_reports[destination] = backup
    previous_output = transaction_root / "previous-output"
    previous_events = transaction_root / "previous-events"
    output_had_previous = OUTPUT_ROOT.exists()
    events_had_previous = (
        staged_events is not None and FULL_RANGE_EVENTS_ROOT.exists()
    )
    output_moved = False
    output_installed = False
    events_moved = False
    events_installed = False
    installed_reports: list[Path] = []

    try:
        if output_had_previous:
            os.replace(OUTPUT_ROOT, previous_output)
            output_moved = True
        os.replace(staged_output, OUTPUT_ROOT)
        output_installed = True
        if staged_events is not None:
            if events_had_previous:
                os.replace(FULL_RANGE_EVENTS_ROOT, previous_events)
                events_moved = True
            os.replace(staged_events, FULL_RANGE_EVENTS_ROOT)
            events_installed = True
        for destination, staged in staged_reports.items():
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
            installed_reports.append(destination)
    except BaseException as commit_error:
        rollback_errors: list[str] = []
        try:
            _restore_reports(previous_reports, installed_reports)
        except BaseException as error:  # noqa: BLE001
            rollback_errors.append(f"reports: {type(error).__name__}: {error}")
        try:
            if output_installed and OUTPUT_ROOT.exists():
                shutil.rmtree(OUTPUT_ROOT)
            if output_moved and previous_output.exists():
                os.replace(previous_output, OUTPUT_ROOT)
        except BaseException as error:  # noqa: BLE001
            rollback_errors.append(f"output: {type(error).__name__}: {error}")
        if staged_events is not None:
            try:
                if events_installed and FULL_RANGE_EVENTS_ROOT.exists():
                    shutil.rmtree(FULL_RANGE_EVENTS_ROOT)
                if events_moved and previous_events.exists():
                    os.replace(previous_events, FULL_RANGE_EVENTS_ROOT)
            except BaseException as error:  # noqa: BLE001
                rollback_errors.append(
                    f"events: {type(error).__name__}: {error}"
                )
        if rollback_errors:
            details = "；".join(rollback_errors)
            raise AuditionRollbackError(
                f"试听批次提交失败且回滚不完整：{details}",
                transaction_root,
            ) from commit_error
        raise

    if output_moved and previous_output.exists():
        shutil.rmtree(previous_output)
    if events_moved and previous_events.exists():
        shutil.rmtree(previous_events)


def generate_all_auditions(
    *,
    profile: str = DEFAULT_PROFILE,
    only: tuple[str, ...] | None = None,
) -> dict[str, list[tuple[str, float, int]]]:
    """Render all or an explicit subset and commit only a complete batch."""

    if profile not in PROFILES:
        choices = "、".join(PROFILES)
        raise ValueError(f"未知试听 profile {profile!r}；可选：{choices}")
    all_production_entries = []
    for entry in discover_instruments(INSTRUMENT_ROOT):
        directory = Path(entry.manifest_path).parent
        relative = directory.relative_to(INSTRUMENT_ROOT)
        if relative.as_posix() != TEST_TOOL:
            all_production_entries.append(entry)
    if (
        EXPECTED_INSTRUMENT_COUNT is not None
        and len(all_production_entries) != EXPECTED_INSTRUMENT_COUNT
    ):
        raise ValueError(
            "正式声音入口数量异常："
            f"期望 {EXPECTED_INSTRUMENT_COUNT}，实际 "
            f"{len(all_production_entries)}；拒绝开始渲染"
        )
    selectors = tuple(only or ())
    selective = bool(selectors)
    selected_entries = (
        _select_only_entries(all_production_entries, selectors)
        if selective
        else all_production_entries
    )
    quarantined_entries = [
        entry
        for entry in all_production_entries
        if getattr(entry, "license_status", None) == "quarantined"
    ]
    selected_quarantined = [
        entry
        for entry in selected_entries
        if getattr(entry, "license_status", None) == "quarantined"
    ]
    if selective and selected_quarantined:
        labels = "、".join(
            Path(entry.manifest_path)
            .parent.relative_to(INSTRUMENT_ROOT)
            .as_posix()
            for entry in selected_quarantined
        )
        raise ValueError(f"--only 不能重建许可隔离入口：{labels}")
    if selective and profile != PROFILE_ASCENDING_SCALE:
        raise ValueError(
            "--only 仅支持 ascending-scale 全音域协议；"
            "旧固定谱例不能安全更新全音域批次清单"
        )
    selected_instruments = tuple(
        Path(entry.manifest_path)
        .parent.relative_to(INSTRUMENT_ROOT)
        .as_posix()
        for entry in selected_entries
    )
    expected_batch_instruments = tuple(
        Path(entry.manifest_path)
        .parent.relative_to(INSTRUMENT_ROOT)
        .as_posix()
        for entry in all_production_entries
        if getattr(entry, "license_status", None) != "quarantined"
    )
    selective_batch_manifest = (
        _load_selective_batch_manifest(
            selected_instruments,
            expected_batch_instruments,
        )
        if selective
        else None
    )
    production_entries = [
        entry
        for entry in selected_entries
        if getattr(entry, "license_status", None) != "quarantined"
    ]
    quarantined_event_sources: list[tuple[Path, Path]] = []
    if profile == PROFILE_ASCENDING_SCALE and not selective:
        for entry in quarantined_entries:
            directory = Path(entry.manifest_path).parent
            relative = directory.relative_to(INSTRUMENT_ROOT)
            event_name = f"{directory.name}_全音域上行.events.json"
            current_events = (
                FULL_RANGE_EVENTS_ROOT / relative.parent / event_name
            )
            if not current_events.is_file():
                raise ValueError(
                    f"隔离入口缺少既有全音域事件谱：{relative.as_posix()}"
                )
            quarantined_event_sources.append(
                (current_events, relative.parent / event_name)
            )

    OUTPUT_ROOT.parent.mkdir(parents=True, exist_ok=True)
    transaction_root = Path(
        tempfile.mkdtemp(prefix=".生成全音域试音-", dir=OUTPUT_ROOT.parent)
    )
    staged_output = transaction_root / "全音域试音"
    staged_report_root = transaction_root / "报告"
    staged_events_root = transaction_root / "全音域上行"
    staged_output.mkdir(parents=True)
    staged_report_root.mkdir(parents=True)
    if profile == PROFILE_ASCENDING_SCALE:
        staged_events_root.mkdir(parents=True)
        for current_events, relative_events in quarantined_event_sources:
            staged_events = staged_events_root / relative_events
            staged_events.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(current_events, staged_events)

    groups: dict[str, list[tuple[str, float, int]]] = {}
    staged_reports: dict[Path, Path] = {}
    staged_output_files: dict[Path, Path] = {}
    staged_event_files: dict[Path, Path] = {}
    batch_entries: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []

    preserve_transaction = False
    try:
        for entry in production_entries:
            directory = Path(entry.manifest_path).parent
            relative = directory.relative_to(INSTRUMENT_ROOT)

            final_wav = OUTPUT_ROOT / relative.parent / f"{directory.name}.wav"
            staged_wav = (
                staged_output / relative.parent / f"{directory.name}.wav"
            )
            staged_report = staged_report_root / relative / "试听核验.json"
            try:
                previous = _load_previous_report(directory)
                plan: FullRangeAudition | None = None
                if profile == PROFILE_ASCENDING_SCALE:
                    plan = build_full_range_audition(
                        directory / "乐器.json",
                        instrument_root=INSTRUMENT_ROOT,
                    )
                    event_name = f"{directory.name}_全音域上行.events.json"
                    events = (
                        staged_events_root / relative.parent / event_name
                    )
                    _write_json(events, plan.document)
                    final_events = _full_range_events_path(relative)
                    if selective:
                        staged_event_files[final_events] = events
                    coverage = plan.coverage
                else:
                    events = _events_for(directory)
                    final_events = events
                    coverage = _existing_profile_coverage(events)
                staged_report.parent.mkdir(parents=True, exist_ok=True)
                report = generate_dedicated_audition_verification(
                    directory / "乐器.json",
                    events,
                    staged_wav,
                    output_path=staged_report,
                    coverage=coverage,
                )
                if profile == PROFILE_ASCENDING_SCALE:
                    report = _reset_review_and_archive_specialized_evidence(
                        report,
                        previous,
                    )
                else:
                    report = _preserve_manual_fields(report, previous)
                report["wav"] = _relative_wav_label(final_wav)
                report["events"] = _relative_wav_label(final_events)
                report["audition_profile"] = profile
                if profile == PROFILE_ASCENDING_SCALE:
                    report["audition_protocol"] = PROTOCOL_ID
                _validate_report_signal(report, profile=profile)
                _write_json(staged_report, report)
                staged_reports[directory / "试听核验.json"] = staged_report
                if selective:
                    for destination, staged in _staged_output_artifacts(
                        staged_wav,
                        final_wav,
                    ).items():
                        if destination in staged_output_files:
                            raise ValueError(
                                f"选择性试听输出目标重复：{destination}"
                            )
                        staged_output_files[destination] = staged

                if plan is not None:
                    batch_entry = plan.metadata()
                    batch_entry.update(
                        {
                            "order": len(batch_entries) + 1,
                            "wav": _relative_wav_label(final_wav),
                            "wav_persistence": "temporary",
                            "wav_sha256": str(report["wav_sha256"]),
                            "hash_algorithm": str(
                                report["hash_algorithm"]
                            ),
                            "canonicalization": str(
                                report["canonicalization"]
                            ),
                            "manifest_canonical_sha256": str(
                                report["manifest_canonical_sha256"]
                            ),
                            "events": _relative_wav_label(final_events),
                            "events_canonical_sha256": str(
                                report["events_canonical_sha256"]
                            ),
                            "peak": float(report["peak"]),
                            "clipped_samples": int(
                                report["clipped_samples"]
                            ),
                        }
                    )
                    batch_entries.append(batch_entry)

                category = relative.parent.as_posix() or "未分类"
                groups.setdefault(category, []).append(
                    (
                        directory.name,
                        float(report["peak"]),
                        int(report["clipped_samples"]),
                    )
                )
            except Exception as error:  # noqa: BLE001
                failures.append(
                    (relative.as_posix(), f"{type(error).__name__}: {error}")
                )

        if failures:
            raise AuditionBatchError(failures)
        if selective:
            if selective_batch_manifest is not None:
                staged_batch_manifest = staged_output / "_试听清单.json"
                staged_batch_order = staged_output / "_试听顺序.txt"
                _stage_selective_batch_manifest(
                    selective_batch_manifest,
                    batch_entries,
                    staged_batch_manifest,
                    staged_batch_order,
                )
                staged_output_files[
                    OUTPUT_ROOT / "_试听清单.json"
                ] = staged_batch_manifest
                staged_output_files[
                    OUTPUT_ROOT / "_试听顺序.txt"
                ] = staged_batch_order
            selected_files = dict(staged_output_files)
            selected_files.update(staged_event_files)
            selected_files.update(staged_reports)
            _commit_selected_files(transaction_root, selected_files)
        else:
            staged_events: Path | None = None
            if profile == PROFILE_ASCENDING_SCALE:
                _write_batch_manifest(staged_output, batch_entries)
                staged_events = staged_events_root
            _commit_batch(
                transaction_root,
                staged_output,
                staged_reports,
                staged_events=staged_events,
            )
        return groups
    except AuditionRollbackError:
        preserve_transaction = True
        raise
    finally:
        if transaction_root.exists() and not preserve_transaction:
            shutil.rmtree(transaction_root)


def _print_summary(
    groups: dict[str, list[tuple[str, float, int]]],
) -> None:
    total = sum(len(items) for items in groups.values())
    print(f"已生成 {total} 段试音，输出根目录：{OUTPUT_ROOT}\n")
    for category in sorted(groups):
        items = sorted(groups[category])
        loud = max(peak for _, peak, _ in items)
        clips = sum(clipped for _, _, clipped in items)
        print(
            f"== {category}（{len(items)} 件，最高峰值 {loud:.4f}，"
            f"削波 {clips}）"
        )
        for name, peak, clipped in items:
            flag = "" if clipped == 0 else f"  ← 削波 {clipped}"
            print(f"   {name:<16s} peak {peak:.4f}{flag}")
        print()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "原子生成 103 件登记乐器中的全部或指定未隔离入口试听；"
            "默认逐件覆盖全部声明合法整数键。"
        )
    )
    parser.add_argument(
        "--profile",
        choices=PROFILES,
        default=DEFAULT_PROFILE,
        help=(
            "ascending-scale=全音域半音上行（默认）；"
            "existing=沿用原有定制谱例"
        ),
    )
    parser.add_argument(
        "--only",
        action="append",
        metavar="相对路径或唯一名称",
        help=(
            "只重建指定正式乐器；可重复传入。相对路径以乐器/为根，"
            "仅传目录名时必须在正式目录中唯一"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        groups = generate_all_auditions(
            profile=args.profile,
            only=tuple(args.only) if args.only else None,
        )
    except AuditionBatchError as error:
        print(
            f"!! {len(error.failures)} 件渲染失败，"
            "现有全音域试听与报告保持不变："
        )
        for relative, message in error.failures:
            print(f"   {relative}: {message}")
        raise SystemExit(1) from error
    _print_summary(groups)


if __name__ == "__main__":
    main()
