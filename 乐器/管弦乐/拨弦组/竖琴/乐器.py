"""VCSL Concert Harp 竖琴入口（沿用兼容的 vpo_harp 适配器名）。"""

from __future__ import annotations

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_strings import create_vpo_harp


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return create_vpo_harp(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
