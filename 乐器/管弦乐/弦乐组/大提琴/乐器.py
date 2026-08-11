"""兼容旧的目录级大提琴工厂；正式清单使用天籁内置调度。"""

from __future__ import annotations

from typing import Any

from tianlai.cello import CelloInstrument
from tianlai.cello import create as _create_builtin
from tianlai.instrument import Instrument


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return _create_builtin(
        manifest=manifest,
        sample_rate=sample_rate,
        base_directory=base_directory,
    )


__all__ = ["CelloInstrument", "create"]
