"""渲染电钢琴固定表现力试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "电钢琴_奏法.events.json",
        ROOT / "output" / "表现力试听" / "键盘乐器" / "电钢琴_candidate.wav",
        output_path=report_path,
        coverage=[
            "A0-C8 低/中/高音域",
            "PP/MP/F/FF 四档真实力度层与长短音",
            "81 个 44.1 kHz 单声道 FLAC 经版本化带限重采样输出到 48 kHz",
            "端到端宽频根音检查,禁止整八度映射错误",
            "奏法:normal",
            "note-off 释放与尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{report_path}")


if __name__ == "__main__":
    main()
