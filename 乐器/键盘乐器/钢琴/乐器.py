"""兼容旧的目录级钢琴工厂；正式清单使用天籁内置调度。"""

from __future__ import annotations

from typing import Any

from tianlai.instrument import Instrument
from tianlai.piano import PianoInstrument
from tianlai.piano import create as _create_builtin
from tianlai.sampler import SampleInstrument


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return _create_builtin(
        manifest=manifest,
        sample_rate=sample_rate,
        base_directory=base_directory,
    )


__all__ = ["PianoInstrument", "SampleInstrument", "create"]
