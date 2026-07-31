"""生成 VCSL Tubular Bells 2 管钟的非谐波根音映射审计表。"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_percussion import generate_percussion_pitch_calibration


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_percussion_pitch_calibration(here / "乐器.json")
    print(
        f"已审计 {report['summary']['sample_count']} 个管钟根音映射；"
        f"自动 cents 校正已禁用：{here / '音准校准.json'}"
    )
