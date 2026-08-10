"""Strict loading of Tianlai's creator-curated instrument palette."""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any


class TrustPolicyError(ValueError):
    """The requested curated policy cannot be enforced safely."""


def load_allowlist_document(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.is_file():
        raise TrustPolicyError(f"可信乐器白名单不存在: {source}")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrustPolicyError(f"可信乐器白名单无法读取: {exc}") from exc
    if not isinstance(document, dict):
        raise TrustPolicyError("可信乐器白名单必须是 JSON 对象")
    return document


def load_trusted_instruments(
    path: str | Path,
    capabilities: Mapping[str, Any],
) -> frozenset[str]:
    """Load a non-empty allowlist and reject every unusable reference."""

    document = load_allowlist_document(path)
    raw = document.get("trusted")
    if not isinstance(raw, list) or not raw:
        raise TrustPolicyError("可信乐器白名单的 trusted 必须是非空数组")
    if any(
        not isinstance(instrument, str) or not instrument.strip()
        for instrument in raw
    ):
        raise TrustPolicyError("可信乐器白名单的 trusted 必须是非空字符串数组")
    if len(set(raw)) != len(raw):
        raise TrustPolicyError("可信乐器白名单含重复路径")
    invalid: list[str] = []
    for instrument in raw:
        capability = capabilities.get(instrument)
        if capability is None:
            invalid.append(f"{instrument}(目录中不存在)")
        elif capability.quality_tier != "formal":
            invalid.append(f"{instrument}(不是 formal 正式声音入口)")
        elif capability.license_status == "quarantined":
            invalid.append(f"{instrument}(许可证据已隔离)")
        elif capability.license_status not in {"approved", "grandfathered"}:
            invalid.append(f"{instrument}(没有公开可用的许可证状态)")
        elif capability.implementation_type == "soundfont":
            invalid.append(f"{instrument}(仅限本机兼容 SoundFont)")
    if invalid:
        raise TrustPolicyError(
            "可信乐器白名单包含不可发布入口: " + "; ".join(invalid)
        )
    return frozenset(raw)


def load_variant_hints(path: str | Path) -> dict[str, str]:
    document = load_allowlist_document(path)
    raw = document.get("变体提示", {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(instrument): hint.strip()
        for instrument, hint in raw.items()
        if isinstance(instrument, str)
        and isinstance(hint, str)
        and hint.strip()
    }


__all__ = [
    "TrustPolicyError",
    "load_allowlist_document",
    "load_trusted_instruments",
    "load_variant_hints",
]
