"""测量竖笛根采样音准(或登记不适用理由)。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_dedicated_pitch_calibration(here / "乐器.json")
    if report.get("applicable"):
        summary = report["summary"]
        print(
            f"已校准 {summary['sample_count']} 个根采样,"
            f"残差中位 {summary['median_residual_cents']:+.3f} c"
        )
    else:
        print("无固定音高:已登记不适用理由")


if __name__ == "__main__":
    main()
