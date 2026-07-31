"""核验定音鼓采用的 VCSL SFZ、WAV、许可与固定版本。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vcsl_timpani import generate_vcsl_timpani_resource_verification


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_vcsl_timpani_resource_verification(here / "乐器.json")
    print(
        f"已核验 {report['sample_count']} 个定音鼓采样："
        f"{here / '资源核验.json'}"
    )
