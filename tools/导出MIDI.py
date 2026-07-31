# -*- coding: utf-8 -*-
"""兼容入口：正式实现已迁到 ``tianlai export-midi``。

标准 MIDI 不能无损保存 Tianlai 的 event_id、奏法、短语和部分编制语义，
所以该入口不再声称与导入器“互为逆向”。它会生成机器可读 loss report，并在
存在阻断性损失时要求显式 ``--allow-lossy``。
"""

from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["export-midi", *sys.argv[1:]]))
