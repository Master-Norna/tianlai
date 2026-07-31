"""渲染雨境合成氛围固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "雨境合成氛围_程序建模.events.json",
        ROOT / "output" / "雨境合成氛围_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "雨幕与远场层",
            "modulation 雨滴密度",
            "立体声滴落",
            "长淡出",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
