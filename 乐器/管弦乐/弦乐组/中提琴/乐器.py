"""兼容旧的目录级中提琴工厂；正式清单使用天籁内置调度。"""

from __future__ import annotations

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vsco2_viola import Vsco2ViolaSectionInstrument
from tianlai.vsco2_viola import create as _create_builtin


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return _create_builtin(
        manifest=manifest,
        sample_rate=sample_rate,
        base_directory=base_directory,
    )


__all__ = ["Vsco2ViolaSectionInstrument", "create"]
