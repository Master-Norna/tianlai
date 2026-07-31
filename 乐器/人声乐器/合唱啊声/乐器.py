"""Virtual Playing Orchestra 男女混声 Ah 合唱入口。"""

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_specials import create_vpo_mixed_choir


def create(*, manifest: dict[str, Any], sample_rate: int, base_directory: str) -> Instrument:
    return create_vpo_mixed_choir(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
