"""VCSL CC0 颤音琴专用 SFZ 入口。"""

from __future__ import annotations

from typing import Any

from tianlai.dedicated_sfz import DedicatedSfzInstrument
from tianlai.instrument import Instrument


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return DedicatedSfzInstrument(sample_rate, manifest, base_directory)
