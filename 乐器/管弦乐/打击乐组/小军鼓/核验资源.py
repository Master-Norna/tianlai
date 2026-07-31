"""核验小军鼓采用的 VPO SFZ、WAV、许可与版本。"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_percussion import generate_percussion_resource_verification


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    report = generate_percussion_resource_verification(
        here / "乐器.json",
        license_files=("Documentation/license.htm", "libs/VSCO2-CE/LICENSE.txt"),
    )
    print(f"已核验 {report['sample_count']} 个小军鼓采样：{here / '资源核验.json'}")
