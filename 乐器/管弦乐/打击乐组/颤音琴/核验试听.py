"""渲染颤音琴固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "颤音琴_奏法.events.json",
        ROOT / "output" / "颤音琴_奏法_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "F3-F6 struck range; A3-F6 bowed range",
            "soft mallet damped/open",
            "hard mallet damped/open",
            "bowed",
            "two real velocity layers for both mallet sets",
            "one recorded take per root/layer; pseudo RR excluded",
            "sustain pedal",
            "weak/strong",
            "silent keyswitch excluded",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}")


if __name__ == "__main__":
    main()
