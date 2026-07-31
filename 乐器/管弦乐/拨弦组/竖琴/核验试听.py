"""渲染竖琴固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "竖琴_奏法.events.json",
        ROOT / "output" / "竖琴_奏法_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "E1-F#7 exact VCSL Concert Harp mapped range",
            "weak/medium/strong velocity",
            "discrete midpoint selection across the two-recording roots",
            "open/sustain alias and project-envelope dampened articulation",
            "30-second natural-tail release and 350 ms damping release",
            "score-level sustain pedal hold/release",
            "fractional MIDI pitch and deterministic rendering",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
