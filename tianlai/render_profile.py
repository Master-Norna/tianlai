"""Versioned, explicit render defaults shared by human and Agent entrypoints."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .space import SpaceConfig


PROFILE_KIND = "tianlai.render_profile"
PROFILE_VERSION = 1
DEFAULT_PROFILE_NAME = "preview-v1"
_PROFILE_KEYS = frozenset(
    {
        "kind",
        "schema_version",
        "name",
        "expression",
        "range_mode",
        "seed",
        "master_gain_db",
        "normalize_peak_db",
        "space",
        "collaboration_mode",
        "write_stems",
        "use_stem_cache",
        "refresh_stem_cache",
    }
)


def _finite(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


@dataclass(frozen=True, slots=True)
class RenderProfile:
    name: str = DEFAULT_PROFILE_NAME
    expression: str = "ensemble"
    range_mode: str = "compatibility"
    seed: int = 0
    master_gain_db: float = 0.0
    normalize_peak_db: float | None = -1.0
    space: SpaceConfig | None = SpaceConfig()
    collaboration_mode: str | None = None
    write_stems: bool = True
    use_stem_cache: bool = True
    refresh_stem_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": PROFILE_KIND,
            "schema_version": PROFILE_VERSION,
            "name": self.name,
            "expression": self.expression,
            "range_mode": self.range_mode,
            "seed": self.seed,
            "master_gain_db": self.master_gain_db,
            "normalize_peak_db": self.normalize_peak_db,
            "space": (
                {"enabled": False}
                if self.space is None
                else {
                    "enabled": True,
                    "config": self.space.to_dict(),
                }
            ),
            "collaboration_mode": self.collaboration_mode,
            "write_stems": self.write_stems,
            "use_stem_cache": self.use_stem_cache,
            "refresh_stem_cache": self.refresh_stem_cache,
        }


def parse_render_profile(
    value: dict[str, Any] | None = None,
) -> RenderProfile:
    """Parse one strict profile; ``None`` resolves to the public preview v1."""

    if value is None:
        return RenderProfile()
    if not isinstance(value, dict):
        raise ValueError("render profile must be an object")
    unknown = sorted(str(key) for key in value if key not in _PROFILE_KEYS)
    if unknown:
        raise ValueError(
            "render profile contains unknown fields: " + ", ".join(unknown)
        )
    if value.get("kind", PROFILE_KIND) != PROFILE_KIND:
        raise ValueError(f"render profile kind must be {PROFILE_KIND}")
    version = value.get("schema_version", PROFILE_VERSION)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != PROFILE_VERSION
    ):
        raise ValueError("render profile schema_version must be 1")
    name = str(value.get("name", DEFAULT_PROFILE_NAME)).strip()
    if not name:
        raise ValueError("render profile name must not be empty")
    expression = str(value.get("expression", "ensemble"))
    if expression not in {"ensemble", "strict"}:
        raise ValueError("render profile expression must be ensemble or strict")
    range_mode = str(value.get("range_mode", "compatibility"))
    if range_mode not in {"compatibility", "strict_hq"}:
        raise ValueError(
            "render profile range_mode must be compatibility or strict_hq"
        )
    raw_seed = value.get("seed", 0)
    if isinstance(raw_seed, bool) or not isinstance(raw_seed, int):
        raise ValueError("render profile seed must be an integer")
    master_gain_db = _finite(
        value.get("master_gain_db", 0.0),
        "render profile master_gain_db",
    )
    raw_normalize = value.get("normalize_peak_db", -1.0)
    normalize_peak_db = (
        None
        if raw_normalize is None
        else _finite(
            raw_normalize,
            "render profile normalize_peak_db",
        )
    )
    if normalize_peak_db is not None and normalize_peak_db > 0.0:
        raise ValueError("render profile normalize_peak_db must be <= 0")
    raw_space = value.get("space")
    if raw_space is None:
        space = SpaceConfig()
    else:
        if not isinstance(raw_space, dict):
            raise ValueError("render profile space must be an object")
        unknown_space = set(raw_space) - {"enabled", "config"}
        if unknown_space:
            raise ValueError(
                "render profile space contains unknown fields: "
                + ", ".join(sorted(unknown_space))
            )
        enabled = raw_space.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("render profile space.enabled must be boolean")
        config = raw_space.get("config", {})
        if not isinstance(config, dict):
            raise ValueError("render profile space.config must be an object")
        configured_space = SpaceConfig.from_dict(config)
        space = configured_space if enabled else None
    collaboration_mode = value.get("collaboration_mode")
    if collaboration_mode not in {None, "manual", "analyze", "suggest"}:
        raise ValueError(
            "render profile collaboration_mode must be manual, analyze, "
            "suggest or null"
        )
    booleans: dict[str, bool] = {}
    for field, default in (
        ("write_stems", True),
        ("use_stem_cache", True),
        ("refresh_stem_cache", False),
    ):
        item = value.get(field, default)
        if not isinstance(item, bool):
            raise ValueError(f"render profile {field} must be boolean")
        booleans[field] = item
    return RenderProfile(
        name=name,
        expression=expression,
        range_mode=range_mode,
        seed=raw_seed,
        master_gain_db=master_gain_db,
        normalize_peak_db=normalize_peak_db,
        space=space,
        collaboration_mode=collaboration_mode,
        **booleans,
    )


def profile_with_overrides(
    profile: RenderProfile,
    **overrides: Any,
) -> RenderProfile:
    """Apply only non-``None`` scalar overrides and validate the result."""

    document = profile.to_dict()
    for key, value in overrides.items():
        if value is None:
            continue
        if key == "space":
            document["space"] = (
                {"enabled": False}
                if value is False
                else {
                    "enabled": True,
                    "config": (
                        value.to_dict()
                        if isinstance(value, SpaceConfig)
                        else {}
                    ),
                }
            )
        else:
            document[key] = value
    return parse_render_profile(document)


__all__ = [
    "DEFAULT_PROFILE_NAME",
    "PROFILE_KIND",
    "PROFILE_VERSION",
    "RenderProfile",
    "parse_render_profile",
    "profile_with_overrides",
]
