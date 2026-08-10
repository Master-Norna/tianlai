"""渲染羽管键琴固定表现力试听并复算 WAV 指标与 Hash。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_audition_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report_path = here / "表现力试听核验.json"
    report = generate_dedicated_audition_verification(
        here / "乐器.json",
        ROOT / "examples" / "羽管键琴_奏法.events.json",
        ROOT / "output" / "羽管键琴_candidate.wav",
        output_path=report_path,
        coverage=[
            "按键 F1-C6；8′ 实音 F1-C6、4′ 实音 F2-C7",
            "三档输入力度与长短音（单采样，仅增益响应）",
            "奏法:full(8′+4′)、eight_foot、four_foot(+1八度)",
            "108 个 44.1 kHz 立体声 WAV 的持续层与释键层均经版本化带限重采样输出到 48 kHz",
            "note-off 释放与尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{report_path}")


if __name__ == "__main__":
    main()
