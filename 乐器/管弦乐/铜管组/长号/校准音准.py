"""实测 VPO 独奏长号持续音根采样的基频。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_brass import generate_pitch_calibration


HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    result = generate_pitch_calibration(HERE / "乐器.json", HERE / "音准校准.json")
    print(f"已校准 {result['summary']['sample_count']} 个长号持续音根采样")
