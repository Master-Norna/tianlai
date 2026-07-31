"""精确音高参考乐器。

这个乐器不是对真实乐器的仿真。它用于验证调律、事件时间、复音、踏板和
渲染确定性，也是每件具体乐器独立目录结构的最小示例。
"""

from typing import Any

from tianlai.instrument import Instrument
from tianlai.oscillator import OscillatorInstrument


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    del base_directory
    return OscillatorInstrument.from_manifest(manifest, sample_rate)

