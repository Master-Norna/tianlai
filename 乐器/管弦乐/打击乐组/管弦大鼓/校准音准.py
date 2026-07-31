"""生成管弦大鼓音准适用性报告。"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_percussion import generate_percussion_pitch_calibration


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_percussion_pitch_calibration(here / "乐器.json")
    print(f"管弦大鼓固定音高适用性：{report['applicable']}；{here / '音准校准.json'}")
