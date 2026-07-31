"""测量 VPO 钢片琴的 20 个去重根采样。"""

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
    print(f"已校准 {result['summary']['sample_count']} 个钢片琴根采样")
