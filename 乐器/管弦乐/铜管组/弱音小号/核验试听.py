"""渲染弱音小号固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "弱音小号_奏法.events.json",
        ROOT / "output" / "弱音小号_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "F#3-A#5 低/中/高音域",
            "确定性效果链:520 Hz 高通 → 1.65 kHz Q2.2 +9 dB 谐振峰 → 4.2 kHz 低通,近似直管弱音器的鼻音共振传递特性",
            "弱/中/强三档力度与长短音",
            "奏法:sustain、staccato、accent",
            "note-off 释放与尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
