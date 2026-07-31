"""渲染枪声固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "枪声_程序建模.events.json",
        ROOT / "output" / "枪声_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "通用枪口冲击",
            "力度冲击强度",
            "压力脉冲/机械尾声/三组反射",
            "自然 one-shot",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
