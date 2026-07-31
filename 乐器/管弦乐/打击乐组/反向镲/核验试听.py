"""渲染反向镲固定试听并复算 WAV 指标与 Hash。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_audition_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_dedicated_audition_verification(
        here / "乐器.json",
        ROOT / "examples" / "反向镲_奏法.events.json",
        ROOT / "output" / "反向镲_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "键 60/61/62 三种倒放变体(亮击/暗击/滚奏长涌)",
            "弱/中/强力度振幅缩放",
            "note_off 骤停淡出与完整上升沿",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
