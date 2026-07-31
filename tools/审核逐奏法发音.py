"""人工审核并明确批准逐奏法发音延迟候选。

机器探针只能生成 candidate。本工具强制经过四个分开的动作：

1. ``draft`` 创建全为 pending 的人工审核；
2. ``record`` 每次只填写一条 measured / exclude / unsure；
3. ``finalize`` 确认没有未填写条目；
4. ``approve`` 由审核负责人显式传入 ``--confirm`` 后生成
   ``发音延迟.json``。

这里刻意没有“全部接受”命令。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.onset_evidence import (  # noqa: E402
    OnsetEvidenceError,
    create_review_draft,
    finalize_review,
    promote_review,
    record_review_decision,
)


def _path(value: str) -> Path:
    return Path(value)


def _under_root(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="逐条人工审核逐奏法发音探针；机器结果不能自动批准",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    draft = subparsers.add_parser("draft", help="从候选报告创建 pending 审核")
    draft.add_argument("--project-root", type=_path, default=ROOT)
    draft.add_argument("--candidate", type=_path, required=True)
    draft.add_argument("--output", type=_path, required=True)
    draft.add_argument("--reviewer", required=True)
    draft.add_argument("--display-name", default="")

    record = subparsers.add_parser("record", help="记录一条人工判断")
    record.add_argument("--project-root", type=_path, default=ROOT)
    record.add_argument("--review", type=_path, required=True)
    record.add_argument("--observation", required=True)
    record.add_argument(
        "--status",
        choices=("measured", "exclude", "unsure"),
        required=True,
    )
    measured = record.add_mutually_exclusive_group()
    measured.add_argument(
        "--measured-frame",
        type=int,
        help="WAV 中的绝对起音帧",
    )
    measured.add_argument(
        "--measured-delay-frames",
        type=int,
        help="相对 candidate note_on_frame 的人工延迟帧",
    )
    record.add_argument("--comment", default="")

    finalize = subparsers.add_parser(
        "finalize",
        help="确认全部条目已有人工判断并完成审核",
    )
    finalize.add_argument("--project-root", type=_path, default=ROOT)
    finalize.add_argument("--review", type=_path, required=True)

    approve = subparsers.add_parser(
        "approve",
        help="审核负责人明确批准并生成发音延迟.json",
    )
    approve.add_argument("--project-root", type=_path, default=ROOT)
    approve.add_argument("--candidate", type=_path, required=True)
    approve.add_argument("--review", type=_path, required=True)
    approve.add_argument("--output", type=_path, required=True)
    approve.add_argument("--review-lead", required=True)
    approve.add_argument("--display-name", default="")
    approve.add_argument("--max-spread-ms", type=float, default=30.0)
    approve.add_argument(
        "--confirm",
        action="store_true",
        help="明确确认这是人工审核负责人执行的批准动作",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = args.project_root.resolve()
    try:
        if args.command == "draft":
            output = _under_root(root, args.output)
            document = create_review_draft(
                _under_root(root, args.candidate),
                output,
                project_root=root,
                reviewer_id=args.reviewer,
                display_name=args.display_name,
            )
            print(
                f"已创建人工审核草稿：{output}；"
                f"{len(document['decisions'])} 条均为 pending"
            )
            return 0

        if args.command == "record":
            review = _under_root(root, args.review)
            document = record_review_decision(
                review,
                project_root=root,
                observation_id=args.observation,
                status=args.status,
                measured_onset_frame=args.measured_frame,
                measured_delay_frames=args.measured_delay_frames,
                comment=args.comment,
            )
            remaining = sum(
                decision["status"] == "pending"
                for decision in document["decisions"]
            )
            print(f"已原子保存单条判断：{review}；剩余 pending={remaining}")
            return 0

        if args.command == "finalize":
            review = _under_root(root, args.review)
            finalize_review(review, project_root=root)
            print(f"人工审核已完成：{review}")
            return 0

        if args.command == "approve":
            output = _under_root(root, args.output)
            document = promote_review(
                _under_root(root, args.candidate),
                _under_root(root, args.review),
                output,
                project_root=root,
                explicit_approval=args.confirm,
                review_lead=args.review_lead,
                review_lead_display_name=args.display_name,
                max_spread_ms=args.max_spread_ms,
            )
            print(
                f"已生成审核负责人批准的证据：{output}；"
                f"逐奏法={len(document['articulations'])}"
            )
            return 0

        raise AssertionError(args.command)
    except OnsetEvidenceError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
