"""渲染鸟鸣固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "鸟鸣_程序建模.events.json",
        ROOT / "output" / "鸟鸣_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "通用 FM 鸟群鸣叫",
            "modulation 鸣叫密度",
            "上下滑频与方位",
            "群落淡出",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
