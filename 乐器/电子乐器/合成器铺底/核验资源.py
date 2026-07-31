"""冻结合成器铺底的合成引擎 SHA-256、补丁与种子。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.instrument_audit import generate_engine_resource_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_engine_resource_verification(here / "乐器.json",
        ROOT / "tianlai" / "synthesizer.py",
    )
    print("完成:", report.get("sample_count", report.get("engine_sha256", "记录已写出")))


if __name__ == "__main__":
    main()
