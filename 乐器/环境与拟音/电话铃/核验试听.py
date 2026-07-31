"""渲染电话铃固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "电话铃_程序建模.events.json",
        ROOT / "output" / "电话铃_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "机电双钟 820/1040 Hz",
            "20 Hz 锤击",
            "2 秒响 4 秒停节律",
            "金属淡出",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
