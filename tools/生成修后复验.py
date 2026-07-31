"""只重建本轮人工听审指出的乐器，生成互不叠尾的修后复验包。

这不是新的 102 件正式试听批次，也不会改动 ``output/全音域试音``、
``output/试音``、乐器目录里的机器核验报告或原始音源。输出固定写到
``output/修后复验``，采用全音域相同键位但更慢的隔离音色协议。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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

from tianlai.audition_protocol import (  # noqa: E402
    ISOLATED_PROTOCOL_ID,
    FullRangeAudition,
    build_full_range_audition,
    isolate_full_range_audition,
    restrict_full_range_audition,
)
from tianlai.canonical_json import (  # noqa: E402
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.dedicated_candidates import (  # noqa: E402
    generate_dedicated_audition_verification,
)


INSTRUMENT_ROOT = ROOT / "乐器"
OUTPUT_ROOT = ROOT / "output" / "修后复验"
BASELINE_ROOT = ROOT / "output" / "全音域试音"


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    relative_path: str
    gate_seconds: float
    release_seconds: float
    silence_seconds: float
    listen_for: str
    review_ranges: tuple[tuple[int, int], ...] | None = None

    @property
    def name(self) -> str:
        return Path(self.relative_path).name


# release 取底层实际包络的保守上界，而不是清单里可能被 SFZ 覆盖的值。
TARGETS: tuple[ReviewTarget, ...] = (
    ReviewTarget(
        "管弦乐/打击乐组/钢鼓",
        0.60,
        0.45,
        0.20,
        "检查全程是否仍有电流感；相邻音已清空尾音，拍频不会跨音累积。",
    ),
    ReviewTarget(
        "管弦乐/打击乐组/太鼓",
        0.55,
        0.45,
        0.22,
        "依次听中心击、边缘击、ka 鼓边木击；应奏法不同但共享同一鼓身身份。",
    ),
    ReviewTarget(
        "管弦乐/木管组/竖笛",
        0.90,
        0.35,
        0.22,
        "检查根采样边界的响度、声像和音色是否连续，尤其 MIDI 88 以上高区。",
    ),
    ReviewTarget(
        "管弦乐/弦乐组/低音提琴",
        1.00,
        1.80,
        0.25,
        "重点听 MIDI 39–40 电平及高区清晰度；本文件不会把四个半音尾音叠在一起。",
    ),
    ReviewTarget(
        "管弦乐/弦乐组/小提琴",
        0.80,
        1.60,
        0.25,
        "检查高仿核心音域顶端是否自然；不再用单枚 Bb6 样本伪装到 A7。",
        ((55, 94),),
    ),
    ReviewTarget(
        "键盘乐器/音乐盒",
        0.55,
        0.60,
        0.22,
        "检查前半段是否仍有间歇电流感，以及高模态消退是否平滑。",
    ),
    ReviewTarget(
        "世界乐器/风笛",
        1.00,
        0.55,
        0.25,
        "这里只审旋律管；两根持续低音管已拆成独立奏法，不再混入同一音阶。",
    ),
    ReviewTarget(
        "世界乐器/民谣提琴",
        0.75,
        0.12,
        0.25,
        "检查回声是否消失、音头是否更适合 fiddle；慢抒情奏法仍另行保留。",
    ),
    ReviewTarget(
        "世界乐器/日本筝",
        0.60,
        0.40,
        0.22,
        "重点听 MIDI 69、86 的起音是否还像电流跳变，并检查全程是否有直流咔嗒。",
    ),
)


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _relative(path: Path) -> str:
    return path.resolve().relative_to(ROOT.resolve()).as_posix()


def _signal_extra(wav_path: Path) -> dict[str, float]:
    import numpy as np
    import soundfile as sf

    audio, _sample_rate = sf.read(
        str(wav_path),
        dtype="float64",
        always_2d=True,
    )
    if audio.size == 0:
        return {"dc_mean": 0.0, "max_sample_step": 0.0}
    mono = np.mean(audio, axis=1)
    maximum_step = (
        float(np.max(np.abs(np.diff(mono))))
        if mono.size > 1
        else 0.0
    )
    return {
        "dc_mean": round(float(np.mean(mono)), 9),
        "max_sample_step": round(maximum_step, 9),
    }


def _validate_report(report: dict[str, Any], wav_path: Path) -> None:
    peak = float(report["peak"])
    rms = float(report["rms"])
    clipped = int(report["clipped_samples"])
    if not math.isfinite(peak) or not math.isfinite(rms):
        raise ValueError(f"{wav_path.name} 的幅度指标不是有限数")
    if peak <= 1.0e-6 or rms <= 1.0e-8:
        raise ValueError(
            f"{wav_path.name} 近似静音：peak={peak:g}, rms={rms:g}"
        )
    if peak >= 0.999 or clipped:
        raise ValueError(
            f"{wav_path.name} 未通过幅度门：peak={peak:g}, clipped={clipped}"
        )


def _selected_targets(only: tuple[str, ...]) -> tuple[ReviewTarget, ...]:
    if not only:
        return TARGETS
    requested = set(only)
    matches = tuple(
        target
        for target in TARGETS
        if target.name in requested or target.relative_path in requested
    )
    resolved = {
        target.name
        for target in matches
    } | {
        target.relative_path
        for target in matches
    }
    missing = sorted(requested - resolved)
    if missing:
        choices = "、".join(target.name for target in TARGETS)
        raise ValueError(
            f"未知 --only：{'、'.join(missing)}；可选：{choices}"
        )
    return matches


def generate_repair_review(
    *,
    targets: tuple[ReviewTarget, ...] = TARGETS,
    replace: bool = False,
) -> list[dict[str, Any]]:
    """Render the selected repair targets and atomically publish the folder."""

    expected_parent = (ROOT / "output").resolve()
    destination = OUTPUT_ROOT.resolve()
    if destination.parent != expected_parent:
        raise ValueError(f"拒绝写到 workspace output 之外：{destination}")
    if destination in {
        (ROOT / "output" / "试音").resolve(),
        (ROOT / "output" / "全音域试音").resolve(),
    }:
        raise ValueError(f"拒绝覆盖历史/全量试听目录：{destination}")
    if OUTPUT_ROOT.exists() and not replace:
        raise FileExistsError(
            f"{OUTPUT_ROOT} 已存在；确认要重建时传 --replace"
        )

    OUTPUT_ROOT.parent.mkdir(parents=True, exist_ok=True)
    transaction = Path(
        tempfile.mkdtemp(prefix=".生成修后复验-", dir=OUTPUT_ROOT.parent)
    )
    staged = transaction / "修后复验"
    staged.mkdir()
    entries: list[dict[str, Any]] = []

    try:
        for order, target in enumerate(targets, start=1):
            manifest = INSTRUMENT_ROOT / target.relative_path / "乐器.json"
            if not manifest.is_file():
                raise FileNotFoundError(f"缺少乐器清单：{manifest}")
            base = build_full_range_audition(
                manifest,
                instrument_root=INSTRUMENT_ROOT,
            )
            if target.review_ranges is not None:
                base = restrict_full_range_audition(
                    base,
                    ranges=target.review_ranges,
                    reason=(
                        "仅覆盖清单中当前高仿核心区；兼容/极端升调区"
                        "保留在正式能力合同中，但不冒充本轮复验通过"
                    ),
                )
            plan: FullRangeAudition = isolate_full_range_audition(
                base,
                gate_seconds=target.gate_seconds,
                release_seconds=target.release_seconds,
                silence_seconds=target.silence_seconds,
            )

            stem = f"{order:02d}_{target.name}"
            events = staged / "_events" / f"{stem}.events.json"
            report_path = staged / "_reports" / f"{stem}.json"
            wav = staged / f"{stem}.wav"
            _write_json(events, plan.document)
            report = generate_dedicated_audition_verification(
                manifest,
                events,
                wav,
                output_path=report_path,
                coverage=plan.coverage,
            )
            _validate_report(report, wav)
            extra = _signal_extra(wav)
            report.update(
                {
                    **extra,
                    "wav": _relative(OUTPUT_ROOT / wav.name),
                    "events": _relative(
                        OUTPUT_ROOT / "_events" / events.name
                    ),
                    "audition_profile": "isolated-repair-review",
                    "audition_protocol": ISOLATED_PROTOCOL_ID,
                }
            )
            _write_json(report_path, report)
            entries.append(
                {
                    "order": order,
                    "instrument": target.relative_path,
                    "wav": wav.name,
                    "wav_persistence": "temporary",
                    "wav_sha256": _sha256(wav),
                    "events": _relative(
                        OUTPUT_ROOT / "_events" / events.name
                    ),
                    "hash_algorithm": HASH_ALGORITHM,
                    "canonicalization": CANONICALIZATION,
                    "events_canonical_sha256": canonical_json_file_sha256(
                        events
                    ),
                    "manifest_canonical_sha256": canonical_json_file_sha256(
                        manifest
                    ),
                    "key_count": len(plan.unique_keys),
                    "declared_ranges": [
                        list(span) for span in plan.declared_ranges
                    ],
                    "gate_seconds": sorted(
                        {
                            round(strike.duration_seconds, 6)
                            for strike in plan.sequence
                        }
                    ),
                    "gap_seconds": sorted(
                        {
                            round(strike.gap_seconds, 6)
                            for strike in plan.sequence
                        }
                    ),
                    "tail_seconds": plan.tail_seconds,
                    "duration_seconds": report["duration_seconds"],
                    "peak": report["peak"],
                    "rms": report["rms"],
                    "clipped_samples": report["clipped_samples"],
                    "peak_active_voices": report["peak_active_voices"],
                    **extra,
                    "listen_for": target.listen_for,
                }
            )

        manifest_document = {
            "schema_version": 1,
            "protocol": ISOLATED_PROTOCOL_ID,
            "generated_on": _datetime.date.today().isoformat(),
            "wav_persistence": "temporary",
            "baseline_preserved": _relative(BASELINE_ROOT),
            "instrument_count": len(entries),
            "notice": (
                "这里只含本轮人工指出并已修复的乐器；旧秒数不再对应，"
                "请按文件顺序和 MIDI 问题点复验。"
            ),
            "instruments": entries,
        }
        _write_json(staged / "_复验清单.json", manifest_document)
        lines = [
            "天籁：修后隔离复验",
            f"协议：{ISOLATED_PROTOCOL_ID}",
            "原 output/全音域试音 保持不动；本批每个音等尾部结束后再进入下一音。",
            "由于节奏已放慢，修前问题的秒数不再适用，请按下列 MIDI/描述听。",
            "",
        ]
        for entry in entries:
            lines.extend(
                (
                    f"{entry['order']:02d}. {entry['wav']} "
                    f"（{entry['instrument']}）",
                    f"    {entry['listen_for']}",
                )
            )
        (staged / "_试听顺序.txt").write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )

        previous = transaction / "previous"
        if OUTPUT_ROOT.exists():
            os.replace(OUTPUT_ROOT, previous)
        try:
            os.replace(staged, OUTPUT_ROOT)
        except Exception:
            if previous.exists() and not OUTPUT_ROOT.exists():
                os.replace(previous, OUTPUT_ROOT)
            raise
        if previous.exists():
            shutil.rmtree(previous)
        return entries
    finally:
        if transaction.exists():
            shutil.rmtree(transaction)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只生成本轮 9 件异常乐器的修后隔离复验包。",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="若 output/修后复验 已存在，原子替换该目录。",
    )
    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="只渲染指定中文短名或相对路径；可重复。",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    arguments = _parse_args(argv)
    targets = _selected_targets(tuple(arguments.only))
    entries = generate_repair_review(
        targets=targets,
        replace=arguments.replace,
    )
    total_seconds = sum(float(entry["duration_seconds"]) for entry in entries)
    print(
        f"已生成 {len(entries)} 件修后复验：{OUTPUT_ROOT}；"
        f"总时长 {total_seconds / 60.0:.1f} 分钟。"
    )


if __name__ == "__main__":
    main()
