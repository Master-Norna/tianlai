"""Virtual Playing Orchestra 颤弓弦乐入口。"""

from __future__ import annotations

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_strings import create_vpo_string_section


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return create_vpo_string_section(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
