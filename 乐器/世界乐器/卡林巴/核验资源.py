"""完整复算卡林巴 SFZ、逐采样、许可、映射及音频质量证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kalimba_analysis import generate_kalimba_resource_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_kalimba_resource_verification(here / "乐器.json")
    print(f"已核验 {report['sample_count']} 个卡林巴资源:{here / '资源核验.json'}")


if __name__ == "__main__":
    main()
