"""VCSL 定音鼓专用 SFZ 入口。"""

from __future__ import annotations

from typing import Any

from tianlai.dedicated_sfz import create_dedicated_sfz
from tianlai.instrument import Instrument


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return create_dedicated_sfz(
        manifest=manifest,
        sample_rate=sample_rate,
        base_directory=base_directory,
    )
