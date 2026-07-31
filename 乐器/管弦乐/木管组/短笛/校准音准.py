"""生成 VPO 独奏短笛逐 WAV 音准校准表。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_woodwinds import generate_woodwind_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_woodwind_pitch_calibration(here / "乐器.json")
    print(f"已校准 {report['summary']['sample_count']} 个短笛采样：{here / '音准校准.json'}")


if __name__ == "__main__":
    main()
