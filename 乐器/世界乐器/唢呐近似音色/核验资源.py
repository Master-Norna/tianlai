"""登记唢呐近似音色建模引擎版本、参数与引擎 SHA-256。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.modeled_instruments import generate_modeled_resource_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_modeled_resource_verification(here / "乐器.json")
    print("完成:", report.get("engine_sha256", report.get("summary", "记录已写出")))


if __name__ == "__main__":
    main()
