"""测量管弦重击中弦乐持续层与铜管层的根采样。"""

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
    print(f"已校准 {result['summary']['sample_count']} 个管弦重击有音高采样")
