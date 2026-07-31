"""渲染管弦重击固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "管弦重击_奏法.events.json",
        ROOT / "output" / "管弦重击_奏法_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "D1-C6 low/mid/high common tonal range",
            "four string and four brass section crossfades",
            "separate brass attack and looped sustain layers",
            "bass drum D2 crossfade/RR and crash F#4 velocity/RR",
            "fixed one-shot gate and fractional MIDI pitch",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
