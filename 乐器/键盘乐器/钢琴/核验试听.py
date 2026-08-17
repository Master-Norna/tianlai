"""渲染钢琴固定试听并复算 WAV 指标与 Hash。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_audition_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_dedicated_audition_verification(
        here / "乐器.json",
        ROOT / "examples" / "钢琴_C大调.events.json",
        ROOT / "output" / "表现力试听" / "键盘乐器" / "钢琴_candidate.wav",
        # 表现力谱例与正式全音域门是两套互补证据，不得再共用文件。
        # 否则重跑本脚本会覆盖全音域 Hash，并丢失其历史许可迁移记录。
        output_path=here / "表现力试听核验.json",
        coverage=[
            "C 大调四声部和弦",
            "四档输入力度与跨音区采样映射",
            "延音踏板与踏板噪",
            "释音与三层弦共鸣(S/L 之一与 V3 并行)",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
