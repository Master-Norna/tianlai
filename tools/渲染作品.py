"""兼容旧作品清单的批量重渲工具。

链路每更新一次(指挥层、发音补偿、归一……),旧的成品就都过时了。与其一首
一首手工敲命令,这里读 乐谱/JSON/作品清单.json,把每首谱交给当前链路重渲一遍,
按「原创 / 内部测试 / 公有域」分组落盘,并打印峰值与归一增益一览。

乐谱目录整体被 git 忽略,清单与其引用的谱/编制都不入库,所以本工具对任何
缺失的作品**跳过而非报错**:干净检出的仓库里没有这些谱,工具照样能运行,只是
没有作品可渲。

用法:

    .\\.venv\\Scripts\\python.exe tools\\渲染作品.py [--only 关键字] [--plan-only]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.capability import load_capabilities
from tianlai.conductor import ExpressionSettings, build_plan
from tianlai.ensemble import render_plan
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document
from tianlai.space import SpaceConfig

SCORE_DIR = ROOT / "乐谱" / "JSON"
REGISTRY = SCORE_DIR / "作品清单.json"
OUTPUT_ROOT = ROOT / "output" / "作品"


def _clear_output_root() -> None:
    expected_parent = (ROOT / "output").resolve()
    target = OUTPUT_ROOT.resolve()
    if (
        target.parent != expected_parent
        or target == expected_parent
        or OUTPUT_ROOT.is_symlink()
    ):
        raise RuntimeError(
            f"拒绝清理非预期作品目录: {target}"
        )
    shutil.rmtree(target)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="", help="只渲标题含该串的作品")
    parser.add_argument(
        "--plan-only", action="store_true", help="只写演奏计划,不渲染音频"
    )
    parser.add_argument(
        "--expression", default="ensemble", choices=("ensemble", "strict")
    )
    parser.add_argument(
        "--range-mode",
        default="compatibility",
        choices=("compatibility", "strict_hq"),
        help="音域合同模式；strict_hq 对缺失或未核验的高质量范围 fail closed",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="危险操作：整批渲染前显式清空 output/作品；默认保留所有候选",
    )
    parser.add_argument(
        "--no-stem-cache",
        action="store_true",
        help="关闭 output/.tianlai-cache 下的增益前原始分轨缓存",
    )
    parser.add_argument(
        "--refresh-stem-cache",
        action="store_true",
        help="忽略已有缓存并重算原始分轨",
    )
    arguments = parser.parse_args()

    if not REGISTRY.is_file():
        print(f"没有作品清单:{REGISTRY}(乐谱目录未入库,干净检出时属正常)")
        return

    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    works = registry.get("作品", [])
    capabilities = load_capabilities(ROOT / "乐器")
    # 全局默认厅堂:整个乐团共处一个真实空间。作品可用 "space" 覆盖或
    # 用 {"enabled": false} 关掉(如需要绝对干声的分析)。缺省即小厅堂。
    registry_space = registry.get("空间默认", {})

    if OUTPUT_ROOT.exists() and arguments.clean and not arguments.only:
        # 清理必须由用户显式请求，局部重渲永远不清空其他候选。
        _clear_output_root()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    rendered = 0
    skipped: list[str] = []
    for work in works:
        title = str(work.get("标题", "未命名"))
        if arguments.only and arguments.only not in title:
            continue
        group = str(work.get("分组", "其他"))
        score_path = SCORE_DIR / str(work.get("score", ""))
        roster_path = SCORE_DIR / str(work.get("roster", ""))
        if not score_path.is_file() or not roster_path.is_file():
            skipped.append(f"{title}(缺谱或编制)")
            continue

        score = parse_score_document(json.loads(score_path.read_text(encoding="utf-8")))
        roster = parse_roster_document(
            json.loads(roster_path.read_text(encoding="utf-8")), capabilities
        )
        settings = ExpressionSettings.from_dict(
            {
                "mode": arguments.expression,
                "range_mode": arguments.range_mode,
                "humanize": {"seed": int(work.get("seed", 0))},
            }
        )
        plan = build_plan(score, roster, settings)
        space = SpaceConfig.from_dict(work.get("space", registry_space))
        directory = OUTPUT_ROOT / group / title
        print(f"\n■ [{group}] {title}")
        print(f"   {len(plan.parts)} 执行器,{plan.duration_seconds:.1f}s")
        if arguments.plan_only:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "演奏计划.json").write_text(
                json.dumps(plan.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            rendered += 1
            continue
        result = render_plan(
            plan,
            directory,
            write_stems=bool(work.get("write_stems", True)),
            master_gain_db=float(work.get("master_gain_db", 0.0)),
            normalize_peak_db=work.get("normalize_peak_db", -1.0),
            space=space,
            collaboration_mode=work.get("collaboration_mode"),
            stem_cache_directory=(
                None
                if arguments.no_stem_cache
                else ROOT / "output" / ".tianlai-cache" / "stems"
            ),
            refresh_stem_cache=arguments.refresh_stem_cache,
        )
        if space is not None:
            print(f"   厅堂:{space.name}(湿 {space.wet_db:+.0f}dB,房间 {space.room_size})")
        for stem in result.stems:
            print(
                f"     {stem.executor_id:16s} 峰值 {stem.peak:.4f} "
                f"复音 {stem.peak_voices}"
            )
        if result.pre_normalize_peak is not None:
            print(
                f"     归一 {result.pre_normalize_peak:.4f} "
                f"→ {result.normalize_gain_db:+.1f}dB → 总线 {result.mix_peak:.4f}"
            )
        if result.mix_report is not None:
            summary = result.mix_report["summary"]
            print(
                "     协奏诊断 "
                f"{result.mix_report['mode']}："
                f"{summary['warning_count']} 条告警，"
                f"{summary['outside_tolerance']} 组超出目标"
            )
        if result.stem_cache is not None:
            cache = result.stem_cache
            print(
                "     原始分轨缓存 "
                f"命中 {cache['hits']} / 未命中 {cache['misses']} / "
                f"绕过 {cache['bypassed']} / 写入 {cache['writes']}"
            )
        rendered += 1

    print(f"\n渲染 {rendered} 首,输出 {OUTPUT_ROOT}")
    if skipped:
        print("跳过(缺文件):" + "、".join(skipped))


if __name__ == "__main__":
    main()
