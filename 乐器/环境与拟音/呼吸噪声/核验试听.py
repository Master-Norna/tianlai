"""渲染呼吸噪声固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "呼吸噪声_程序建模.events.json",
        ROOT / "output" / "呼吸噪声_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "近讲呼吸循环",
            "力度与 expression 气压",
            "modulation 气道形态",
            "note-off 呼气淡出",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
