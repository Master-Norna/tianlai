"""核验 VCSL CC0 颤音琴映射、样本与许可证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_resource_verification


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_dedicated_resource_verification(here / "乐器.json")
    print(f"已核验 {report['sample_count']} 个颤音琴采样：{here / '资源核验.json'}")
