"""渲染击弦古钢琴固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "击弦古钢琴_奏法.events.json",
        ROOT / "output" / "击弦古钢琴_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "E1-F#6 实际音高范围的低/中/高代表音与两端点",
            "弱/中/强三档输入力度及上游映射区（同组 PCM 相同，不宣称真实力度层）",
            "奏法:normal、resonance",
            "1 个真实力度、两路 Round Robin、note-off 4 秒释放与自然尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
