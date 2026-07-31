"""重新生成编钟的无外部音源资源核验报告。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


HERE = Path(__file__).resolve().parent
ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
IMPLEMENTATION = HERE / "乐器.py"


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "tianlai_bianzhong_resource_audit",
        IMPLEMENTATION,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载编钟实现")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    report = _load_engine().generate_resource_verification(HERE / "乐器.json")
    print(
        "外部资源 "
        f"{len(report['external_assets'])} 项，"
        f"引擎 SHA-256 {report['engine_sha256']}"
    )


if __name__ == "__main__":
    main()
