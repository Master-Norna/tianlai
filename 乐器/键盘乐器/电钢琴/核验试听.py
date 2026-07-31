"""渲染电钢琴固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "电钢琴_奏法.events.json",
        ROOT / "output" / "电钢琴_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "A0-C8 低/中/高音域",
            "PP/MP/F/FF 四档真实力度层与长短音",
            "端到端宽频根音检查,禁止整八度映射错误",
            "奏法:normal",
            "note-off 释放与尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
