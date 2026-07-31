"""渲染合唱铺底探测音并 FFT 实测音准,按补丁设计失谐与测量下限判定。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.instrument_audit import generate_synth_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_synth_pitch_calibration(here / "乐器.json")
    print("完成:", report.get("sample_count", report.get("engine_sha256", "记录已写出")))


if __name__ == "__main__":
    main()
