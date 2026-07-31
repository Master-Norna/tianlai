"""渲染直升机固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "直升机_程序建模.events.json",
        ROOT / "output" / "直升机_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "旋翼四叶脉冲与发动机谐波",
            "modulation 转速",
            "确定性湍流",
            "停机淡出",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
