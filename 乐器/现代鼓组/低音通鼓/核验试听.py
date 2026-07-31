"""渲染低音通鼓固定试听并复算 WAV 指标与 Hash。"""

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
        ROOT / "examples" / "低音通鼓_奏法.events.json",
        ROOT / "output" / "低音通鼓_candidate.wav",
        output_path=here / "试听核验.json",
        coverage=[
            "全部键位变体:60=rimFLS 混合边击 2RR; 61=rimS 边击 2 力度×2RR; 62=HitM 软槌 3 力度×2RR; 63=RollM 软槌滚奏 2 力度; 64=HitS 鼓棒 3 力度×2RR; 65=RollS 鼓棒滚奏 2 力度",
            "弱/中/强/极强四档力度",
            "note-off 释放与尾音",
        ],
    )
    print(f"峰值 {report['peak']:.6f},削波 {report['clipped_samples']}:{here / '试听核验.json'}")


if __name__ == "__main__":
    main()
