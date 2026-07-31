"""记录牛铃的无固定音高校准结论。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_specials import generate_special_pitch_calibration


HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    result = generate_special_pitch_calibration(
        HERE / "乐器.json", HERE / "音准校准.json"
    )
    print(f"牛铃音高校准适用：{result['applicable']}")
