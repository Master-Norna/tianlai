"""登记海浪的音准不适用理由。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.instrument_audit import generate_not_applicable_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_not_applicable_pitch_calibration(here / "乐器.json")
    print("完成:", report.get("sample_count", report.get("engine_sha256", "记录已写出")))


if __name__ == "__main__":
    main()
