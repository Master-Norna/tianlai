"""上低音萨克斯本地工厂：固定使用 MTG Solo Sax 专用后端。"""

from typing import Any

from tianlai.instrument import Instrument
from tianlai.mtg_sax import create_mtg_sax


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return create_mtg_sax(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
