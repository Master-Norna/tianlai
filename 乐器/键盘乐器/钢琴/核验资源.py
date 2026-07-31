"""复算钢琴实际加载采样的 SHA-256 与许可证据。"""

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.instrument_audit import generate_sampled_resource_verification


UPSTREAM_COMMIT = "3382bf9496bba2486f5ab0de55a264d1dfc38404"


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_sampled_resource_verification(here / "乐器.json",
        license_note="CC-BY-3.0",
        upstream="Salamander Grand Piano(Alexander Holm 录制的 Yamaha C5)",
        origin="https://archive.org/details/SalamanderGrandPianoV3",
        upstream_version="V3 48kHz 24bit",
        evidence_files=("LICENSE", "README.md"),
    )
    report["upstream_commit"] = UPSTREAM_COMMIT
    (here / "资源核验.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("完成:", report.get("sample_count", report.get("engine_sha256", "记录已写出")))


if __name__ == "__main__":
    main()
