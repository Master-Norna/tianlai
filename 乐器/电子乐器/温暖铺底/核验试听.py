"""渲染温暖铺底固定试听并复算 WAV 指标与 Hash。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_audition_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_dedicated_audition_verification(
        here / "乐器.json",
        ROOT / "examples" / "温暖铺底_程序合成.events.json",
        ROOT / "output" / "温暖铺底_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "温暖铺底 全音域低/中/高探测音",
            "弱/中/强力度",
            "expression 与 modulation 控制",
            "ADSR 起音与释放",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
