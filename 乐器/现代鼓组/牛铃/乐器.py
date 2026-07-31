"""Virtual Playing Orchestra 独立 Cowbell 入口。"""

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_specials import create_vpo_cowbell


def create(*, manifest: dict[str, Any], sample_rate: int, base_directory: str) -> Instrument:
    return create_vpo_cowbell(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
