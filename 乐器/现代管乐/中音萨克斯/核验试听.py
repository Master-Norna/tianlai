"""渲染中音萨克斯固定试听并复算 WAV 指标与 Hash。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.mtg_sax import generate_mtg_sax_audition_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_mtg_sax_audition_verification(
        here / "乐器.json",
        ROOT / "examples" / "中音萨克斯_奏法.events.json",
        ROOT / "output" / "中音萨克斯_MTG_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "Db3-A5 实音低/中/高音域",
            "2 个真实力度层与每音 3 RR",
            "循环 sustain 与候选伪连奏",
            "expression、breath、modulation vibrato",
            "真实呼吸噪声、按键声、note-off 与延音踏板",
        ],
    )
    print(f"峰值 {report['peak']:.6f}，削波 {report['clipped_samples']}：{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
