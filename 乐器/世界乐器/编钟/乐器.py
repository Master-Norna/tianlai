"""兼容旧的目录级编钟工厂；正式清单使用天籁内置调度。"""

from __future__ import annotations

from typing import Any

from tianlai.bianzhong import BianzhongInstrument, ENGINE_VERSION
from tianlai.bianzhong import create as _create_builtin
from tianlai.bianzhong import generate_pitch_calibration
from tianlai.bianzhong import generate_resource_verification
from tianlai.instrument import Instrument


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    compatible_manifest = dict(manifest)
    implementation = compatible_manifest.pop("implementation", None)
    if implementation not in (None, "乐器.py"):
        raise ValueError("unsupported compatibility implementation")
    return _create_builtin(
        manifest=compatible_manifest,
        sample_rate=sample_rate,
        base_directory=base_directory,
    )


__all__ = [
    "BianzhongInstrument",
    "ENGINE_VERSION",
    "create",
    "generate_pitch_calibration",
    "generate_resource_verification",
]
