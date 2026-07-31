"""渲染短笛固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "短笛_奏法.events.json",
        ROOT / "output" / "短笛_奏法_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "D5-C9 sounding low/mid/high range",
            "D4-C8 written range metadata and +12 semitone rule",
            "weak/medium/strong velocity",
            "sustain",
            "slow_sustain",
            "staccato",
            "accent",
            "expression",
            "breath",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
