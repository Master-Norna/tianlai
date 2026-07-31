"""用非谐和音片模态法复核卡林巴根采样，不自动强拉平均律。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalimba_analysis import generate_kalimba_pitch_calibration


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_kalimba_pitch_calibration(here / "乐器.json")
    summary = report["summary"]
    print(
        f"已复核 {summary['sample_count']} 个音片录音，"
        f"映射后最大模态残差 "
        f"{summary['maximum_absolute_residual_cents']:.3f} c，"
        "未施加自动音高覆盖"
    )


if __name__ == "__main__":
    main()
