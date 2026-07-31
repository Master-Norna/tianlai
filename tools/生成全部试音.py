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
from pathlib import Path
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
    (staged_output / "_试听顺序.txt").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _restore_reports(previous_bytes: dict[Path, bytes]) -> None:
    """Best-effort rollback for a rare failure during the commit phase."""

    for path, content in previous_bytes.items():
        if path.is_file() and path.read_bytes() == content:
            # The failing destination normally still contains its old bytes.
            # Avoid another replace against the same unavailable destination so
            # output/events rollback can continue.
            continue
        temporary = path.with_name(f".{path.name}.rollback.tmp")
        temporary.write_bytes(content)
        os.replace(temporary, path)


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

    previous_reports = {
        destination: destination.read_bytes()
        for destination in staged_reports
        if destination.is_file()
    }
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
    except Exception as commit_error:
        rollback_errors: list[str] = []
        try:
            _restore_reports(previous_reports)
        except Exception as error:  # noqa: BLE001
            rollback_errors.append(f"reports: {type(error).__name__}: {error}")
        try:
            if output_installed and OUTPUT_ROOT.exists():
                shutil.rmtree(OUTPUT_ROOT)
            if output_moved and previous_output.exists():
                os.replace(previous_output, OUTPUT_ROOT)
        except Exception as error:  # noqa: BLE001
            rollback_errors.append(f"output: {type(error).__name__}: {error}")
        if staged_events is not None:
            try:
                if events_installed and FULL_RANGE_EVENTS_ROOT.exists():
                    shutil.rmtree(FULL_RANGE_EVENTS_ROOT)
                if events_moved and previous_events.exists():
                    os.replace(previous_events, FULL_RANGE_EVENTS_ROOT)
            except Exception as error:  # noqa: BLE001
                rollback_errors.append(
                    f"events: {type(error).__name__}: {error}"
                )
        if rollback_errors:
            details = "；".join(rollback_errors)
            raise RuntimeError(
                f"试听批次提交失败且回滚不完整：{details}"
            ) from commit_error
        raise

    if output_moved and previous_output.exists():
        shutil.rmtree(previous_output)
    if events_moved and previous_events.exists():
        shutil.rmtree(previous_events)


def generate_all_auditions(
    *,
    profile: str = DEFAULT_PROFILE,
) -> dict[str, list[tuple[str, float, int]]]:
    """Render every production entry once and commit only a complete batch."""

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
    quarantined_entries = [
        entry
        for entry in all_production_entries
        if getattr(entry, "license_status", None) == "quarantined"
    ]
    production_entries = [
        entry
        for entry in all_production_entries
        if getattr(entry, "license_status", None) != "quarantined"
    ]
    quarantined_event_sources: list[tuple[Path, Path]] = []
    if profile == PROFILE_ASCENDING_SCALE:
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
    batch_entries: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []

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
    finally:
        if transaction_root.exists():
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
            "原子生成 103 件登记乐器中的全部未隔离入口试听；"
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    try:
        groups = generate_all_auditions(profile=args.profile)
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
