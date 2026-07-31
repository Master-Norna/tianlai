"""Shared availability policy for collaboration-layer rosters.

Instrument references are resolved while parsing a :class:`~tianlai.roster.Roster`.
Policy checks must run *after* that step so every caller evaluates the same
canonical capability, whether the roster used a full catalogue path or a
unique short name.

``quarantined`` and the local-only ``soundfont`` compatibility backend are
hard public-pipeline boundaries.  They are deliberately independent from the
optional trusted-instrument allowlist: callers may open the curated palette,
but that must never publish either blocked class.
"""

from __future__ import annotations

from collections.abc import Collection

from .roster import Roster


def roster_availability_problems(
    roster: Roster,
    *,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
) -> tuple[str, ...]:
    """Return deduplicated policy failures for a fully resolved roster.

    ``trusted_instruments=None`` is valid only when ``trusted_only`` is false.
    A requested curated policy without an enforceable allowlist fails closed.
    Licence quarantine is enforced regardless of both optional arguments.
    """

    problems: list[str] = []
    seen_instruments: set[str] = set()
    trusted = (
        None if trusted_instruments is None else frozenset(trusted_instruments)
    )
    if trusted_only and trusted is None:
        return (
            "trusted_only=true 但可信乐器白名单不可用;已按 fail-closed 拒绝",
        )
    for executor in roster.executors:
        capability = executor.capability
        instrument = capability.relative_path
        if instrument in seen_instruments:
            continue
        seen_instruments.add(instrument)
        if capability.license_status == "quarantined":
            problems.append(
                f"{instrument}(许可证据已隔离;trusted_only=false 也不能放开)"
            )
        elif capability.implementation_type == "soundfont":
            problems.append(
                f"{instrument}(SoundFont 仅限显式本机兼容/测试;"
                "不进入 public/trusted 协作链路)"
            )
        elif trusted_only and trusted is not None and instrument not in trusted:
            problems.append(
                f"{instrument}(不在可信白名单;传 trusted_only=false 可放开)"
            )
    return tuple(problems)


def enforce_roster_availability(
    roster: Roster,
    *,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
) -> None:
    """Raise before planning or rendering when availability policy fails."""

    problems = roster_availability_problems(
        roster,
        trusted_only=trusted_only,
        trusted_instruments=trusted_instruments,
    )
    if problems:
        raise ValueError(f"编制里有不可用乐器: {'; '.join(problems)}")
