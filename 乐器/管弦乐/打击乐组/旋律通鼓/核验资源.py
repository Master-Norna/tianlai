"""复算旋律通鼓的采样、许可与 Hash 证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.melodic_toms import generate_melodic_toms_resource_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_melodic_toms_resource_verification(here / "乐器.json")
    print("完成:", report.get("sample_count", "记录已写出"))


if __name__ == "__main__":
    main()
