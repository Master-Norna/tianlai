"""测量 VCSL Concert Harp 每个实际采用的拨弦录音。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_strings import generate_string_pitch_calibration


HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    result = generate_string_pitch_calibration(
        HERE / "乐器.json", HERE / "音准校准.json"
    )
    print(
        f"已校准 {result['summary']['sample_count']} 个竖琴录音，"
        f"{result['summary']['unique_root_count']} 个实录根音"
    )
