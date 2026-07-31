"""重新生成编钟两种击位的低、中、高音准校准报告。"""

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
        "tianlai_bianzhong_pitch_audit",
        IMPLEMENTATION,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载编钟实现")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    report = _load_engine().generate_pitch_calibration(HERE / "乐器.json")
    summary = report["summary"]
    print(
        f"{summary['probe_count']} 个探针，"
        "最大绝对误差 "
        f"{summary['maximum_absolute_error_cents']:.6f} cents"
    )


if __name__ == "__main__":
    main()
