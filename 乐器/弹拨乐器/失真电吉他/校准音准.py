"""测量失真电吉他采样核心的根采样音准。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_dedicated_pitch_calibration(here / "乐器.json")
    print("完成:", report.get("sample_count", report.get("summary", {}).get("sample_count", "N/A")))


if __name__ == "__main__":
    main()
