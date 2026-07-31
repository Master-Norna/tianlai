"""生成 VCSL 颤音琴逐录音运行时音准校准表。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_pitch_calibration


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_dedicated_pitch_calibration(here / "乐器.json")
    print(f"已校准 {report['summary']['sample_count']} 个颤音琴采样：{here / '音准校准.json'}")
