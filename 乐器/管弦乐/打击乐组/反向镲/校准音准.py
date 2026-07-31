"""生成反向镲的音准校准记录。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.reversed_cymbal import generate_reversed_cymbal_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_reversed_cymbal_pitch_calibration(here / "乐器.json")
    print("完成:", report.get("sample_count", "记录已写出"))


if __name__ == "__main__":
    main()
