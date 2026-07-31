"""复算低音提琴的 VPO 资源、许可与 Hash 证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_strings import generate_string_resource_audit


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_string_resource_audit(here / "乐器.json", here / "资源核验.json")
    print("已核验资源:", report.get("sample_count", "记录已写出"))


if __name__ == "__main__":
    main()
