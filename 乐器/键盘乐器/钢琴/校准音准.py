"""测量钢琴实际加载根采样的音准。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.instrument_audit import generate_sampled_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_sampled_pitch_calibration(here / "乐器.json")
    summary = report["summary"]
    print(
        f"已校准 {summary['sample_count']} 根采样,"
        f"中位 {summary['median_detune_cents']:+.3f} c"
    )


if __name__ == "__main__":
    main()
