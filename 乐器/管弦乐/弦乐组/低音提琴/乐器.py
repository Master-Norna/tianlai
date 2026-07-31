"""Virtual Playing Orchestra 独奏低音提琴入口。"""

from __future__ import annotations

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_strings import create_vpo_solo_string


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return create_vpo_solo_string(
        manifest=manifest,
        sample_rate=sample_rate,
        base_directory=base_directory,
    )
