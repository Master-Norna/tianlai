"""逐样本比较三套本地 VCSL 定音鼓候选。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vcsl_timpani import generate_vcsl_timpani_candidate_comparison


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_vcsl_timpani_candidate_comparison(here / "乐器.json")
    print(
        f"已比较 {len(report['candidates'])} 套定音鼓候选："
        f"{here / 'VCSL候选比较.json'}"
    )
