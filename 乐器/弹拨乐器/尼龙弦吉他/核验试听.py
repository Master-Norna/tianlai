"""渲染尼龙弦吉他固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "尼龙弦吉他_奏法.events.json",
        ROOT / "output" / "尼龙弦吉他_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "E2-B5 低/中/高音域",
            "弱/中/强三档力度与长短音",
            "奏法:normal",
            "note-off 释放与尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
