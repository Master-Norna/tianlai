"""渲染卡林巴固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "卡林巴_奏法.events.json",
        ROOT / "output" / "卡林巴_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "B3-C6 低/中/高音域",
            "弱/中/强力度缩放（单实录力度层）与长短音",
            "D#4_k13 对应区的目标音片与 B4_k15 高音片起音",
            "B4_k15 起音后逐渐显现的低八度真实共鸣",
            "奏法:normal",
            "note-off 释放与尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
