"""生成 VCSL 定音鼓的非谐波低模态诊断报告。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vcsl_timpani import generate_vcsl_timpani_pitch_calibration


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_vcsl_timpani_pitch_calibration(here / "乐器.json")
    print(
        f"已诊断 {report['summary']['sample_count']} 个定音鼓采样："
        f"{here / '音准校准.json'}"
    )
