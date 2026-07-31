"""渲染管钟固定试听并复算 WAV 指标与 Hash。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_audition_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_dedicated_audition_verification(
        here / "乐器.json",
        ROOT / "examples" / "管钟_奏法.events.json",
        ROOT / "output" / "管钟_奏法_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "C4-G5 sounding range; 11 roots; maximum 2-semitone stretch",
            "2 genuinely recorded velocity layers; 0 round robins",
            "open natural no-loop long tail",
            "damped uses the same recordings with a modeled 120-ms envelope",
            "sustain-pedal release behavior",
            "stereo source image preserved",
            "two harmful upstream offsets overridden to 0 in project mapping",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
