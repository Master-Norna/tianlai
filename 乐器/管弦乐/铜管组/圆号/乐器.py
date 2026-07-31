"""Virtual Playing Orchestra 独奏圆号入口。"""

from __future__ import annotations

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_brass import create_vpo_brass


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return create_vpo_brass(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
