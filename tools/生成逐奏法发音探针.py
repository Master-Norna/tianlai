r"""生成不可自动批准的逐奏法发音候选证据。

PowerShell 示例：

    .\.venv\Scripts\python.exe tools\生成逐奏法发音探针.py `
      --manifest 乐器\管弦乐\打击乐组\定音鼓\乐器.json `
      --output output\发音探针\定音鼓 `
      --articulation hit --articulation roll --repeat 2

输出是无空间处理的乐器直出 PCM24 WAV、对应 performance JSON，以及一个
哈希绑定的候选报告。工具不会生成正式 ``发音延迟.json``。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.soundfont import prepare_fluidsynth_runtime

prepare_fluidsynth_runtime(str(ROOT))

from tianlai.onset_probe import DEFAULT_VELOCITIES, REPORT_FILENAME, run_probe_batch


def parse_velocities(values: list[str] | None) -> tuple[int, ...]:
    if not values:
        return DEFAULT_VELOCITIES
    parts = [
        part.strip()
        for value in values
        for part in value.split(",")
        if part.strip()
    ]
    try:
        velocities = tuple(int(part) for part in parts)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "velocities 必须是 1..127 的整数"
        ) from error
    if not velocities or any(not 1 <= value <= 127 for value in velocities):
        raise argparse.ArgumentTypeError("velocities 必须是 1..127 的整数")
    if len(set(velocities)) != len(velocities):
        raise argparse.ArgumentTypeError("velocities 不得重复")
    return velocities


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "通过正式事件/乐器工厂/渲染器生成逐奏法发音机器候选；"
            "报告永远 automatic_approval=false"
        )
    )
    parser.add_argument("--manifest", required=True, help="乐器.json 路径")
    parser.add_argument(
        "--output",
        required=True,
        help="新的批次输出目录（必须位于项目内，且不得已经存在）",
    )
    parser.add_argument(
        "--articulation",
        action="append",
        default=None,
        help="只测指定奏法；可重复。不传时测全部非渐强奏法",
    )
    parser.add_argument("--repeat", type=int, default=1, help="每个条件重复次数")
    parser.add_argument(
        "--sample-rate", type=int, default=48_000, help="渲染采样率"
    )
    parser.add_argument(
        "--pre-roll-seconds",
        "--pre-roll",
        dest="pre_roll_seconds",
        type=float,
        default=1.0,
        help="note_on 前静音时长",
    )
    parser.add_argument(
        "--note-seconds",
        type=float,
        default=4.0,
        help="note_on 到 note_off 的时长",
    )
    parser.add_argument(
        "--tail-seconds",
        type=float,
        default=0.5,
        help="note_off 后尾音时长",
    )
    parser.add_argument(
        "--velocities",
        nargs="+",
        default=None,
        metavar="MIDI",
        help="力度列表；支持空格或逗号分隔，默认 32,80,120",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        velocities = parse_velocities(args.velocities)
        report = run_probe_batch(
            Path(args.manifest),
            Path(args.output),
            articulations=args.articulation,
            repeat=args.repeat,
            sample_rate=args.sample_rate,
            pre_roll_seconds=args.pre_roll_seconds,
            note_seconds=args.note_seconds,
            tail_seconds=args.tail_seconds,
            velocities=velocities,
        )
    except (argparse.ArgumentTypeError, OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))

    summary = {
        "status": "candidate_generated",
        "automatic_approval": report["automatic_approval"],
        "observation_count": len(report["observations"]),
        "resolved_count": sum(
            item["analysis"]["status"] == "proposed"
            for item in report["observations"]
        ),
        "unresolved_count": sum(
            item["analysis"]["status"] == "unresolved"
            for item in report["observations"]
        ),
        "report": str((Path(args.output).resolve() / REPORT_FILENAME)),
        "candidate_sha256": report["candidate_sha256"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
