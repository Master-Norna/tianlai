"""真实 VPO 弦乐、铜管、大鼓、镲分层管弦重击入口。"""

from typing import Any

from tianlai.instrument import Instrument
from tianlai.vpo_specials import create_vpo_orchestral_hit


def create(*, manifest: dict[str, Any], sample_rate: int, base_directory: str) -> Instrument:
    return create_vpo_orchestral_hit(
        manifest=manifest, sample_rate=sample_rate, base_directory=base_directory
    )
