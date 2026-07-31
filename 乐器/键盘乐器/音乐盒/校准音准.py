"""渲染音乐盒测试音并 FFT 实测音准(或登记不适用理由)。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.modeled_instruments import generate_modeled_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_modeled_pitch_calibration(here / "乐器.json")
    print("完成:", report.get("engine_sha256", report.get("summary", "记录已写出")))


if __name__ == "__main__":
    main()
