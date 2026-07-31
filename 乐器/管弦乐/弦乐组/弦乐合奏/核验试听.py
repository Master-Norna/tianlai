"""渲染弦乐合奏固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "弦乐合奏_奏法.events.json",
        ROOT / "output" / "弦乐合奏_奏法_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "C1-A7 low/mid/high sampled range",
            "weak/medium/strong velocity",
            "bass/cello/viola/violin equal-power crossfades",
            "sustain, staccato, pizzicato, tremolo, accent",
            "deterministic staccato round robin",
            "expression and sustain-pedal release",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
