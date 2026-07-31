"""渲染太鼓固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "太鼓_奏法.events.json",
        ROOT / "output" / "太鼓_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "键位:60=don 中心击(82 Hz 圆膜模态); 61=边缘击(118 Hz,高模态偏重); 62=ka 鼓边木击(短促高频)",
            "弱/中/强三档力度",
            "note-off 释放与自然衰减",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
