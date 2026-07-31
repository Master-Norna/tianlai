"""Virtual Playing Orchestra 管弦大鼓入口。"""

from __future__ import annotations

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_percussion import create_vpo_percussion


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return create_vpo_percussion(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
