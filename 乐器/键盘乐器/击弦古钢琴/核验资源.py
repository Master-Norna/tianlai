"""复算击弦古钢琴的上游 SFZ、采样、许可与 Hash 证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import generate_dedicated_resource_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_dedicated_resource_verification(here / "乐器.json")
    print(f"已核验 {report['sample_count']} 个击弦古钢琴资源:{here / '资源核验.json'}")


if __name__ == "__main__":
    main()
