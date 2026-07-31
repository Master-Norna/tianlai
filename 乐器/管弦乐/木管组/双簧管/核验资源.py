"""核验 VPO 双簧管 SFZ、WAV、许可与版本证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_woodwinds import generate_woodwind_resource_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_woodwind_resource_verification(
        here / "乐器.json",
        license_files=("Documentation/license.htm",),
    )
    print(f"已核验 {report['sample_count']} 个双簧管采样：{here / '资源核验.json'}")


if __name__ == "__main__":
    main()
