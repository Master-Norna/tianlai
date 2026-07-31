"""Fail-closed resource budgets shared by validation and rendering.

Tianlai is intentionally able to render large scores, but accepting an
unbounded Agent-supplied document is unsafe: a perfectly finite duration can
still request gigabytes of RAM or disk.  This module keeps those operational
limits separate from musical validation and makes every default override
explicit through environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from typing import Any

from .canonical_json import canonical_json_bytes


class ResourceLimitError(ValueError):
    """A valid-looking project exceeds a configured operational budget."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        actual: int | float | None = None,
        limit: int | float | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.actual = actual
        self.limit = limit
        self.preflight: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "actual": self.actual,
            "limit": self.limit,
        }


def _environment_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ResourceLimitError(
            "limits.invalid_environment",
            f"{name} must be a positive integer",
        ) from exc
    if value < 1:
        raise ResourceLimitError(
            "limits.invalid_environment",
            f"{name} must be a positive integer",
        )
    return value


@dataclass(frozen=True, slots=True)
class ProjectLimits:
    """Operational defaults suitable for a local general-purpose computer."""

    max_score_json_bytes: int = 64 * 1024 * 1024
    max_parts: int = 256
    max_notes: int = 250_000
    max_executors: int = 512
    max_plan_seconds: int = 2 * 60 * 60
    max_audio_memory_bytes: int = 2 * 1024 * 1024 * 1024
    max_primary_output_bytes: int = 64 * 1024 * 1024 * 1024

    @classmethod
    def from_environment(cls) -> "ProjectLimits":
        """Read deliberate local overrides without accepting silent disablement."""

        return cls(
            max_score_json_bytes=_environment_positive_int(
                "TIANLAI_MAX_SCORE_MIB", 64
            )
            * 1024
            * 1024,
            max_parts=_environment_positive_int(
                "TIANLAI_MAX_PARTS", 256
            ),
            max_notes=_environment_positive_int(
                "TIANLAI_MAX_NOTES", 250_000
            ),
            max_executors=_environment_positive_int(
                "TIANLAI_MAX_EXECUTORS", 512
            ),
            max_plan_seconds=_environment_positive_int(
                "TIANLAI_MAX_PLAN_SECONDS", 2 * 60 * 60
            ),
            max_audio_memory_bytes=_environment_positive_int(
                "TIANLAI_MAX_AUDIO_MEMORY_MIB", 2048
            )
            * 1024
            * 1024,
            max_primary_output_bytes=_environment_positive_int(
                "TIANLAI_MAX_OUTPUT_MIB", 64 * 1024
            )
            * 1024
            * 1024,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            field: int(getattr(self, field))
            for field in self.__dataclass_fields__
        }


def _raise_if_above(
    *,
    code: str,
    label: str,
    actual: int | float,
    limit: int | float,
    override: str,
) -> None:
    if actual > limit:
        raise ResourceLimitError(
            code,
            f"{label} {actual:g} exceeds limit {limit:g}; "
            f"raise {override} deliberately if this project is trusted",
            actual=actual,
            limit=limit,
        )


def validate_score_resource_limits(
    raw_score: dict[str, Any],
    parsed_score: Any,
    limits: ProjectLimits | None = None,
) -> dict[str, int]:
    """Validate document size and fan-out after structural score parsing."""

    limits = limits or ProjectLimits.from_environment()
    try:
        encoded = canonical_json_bytes(raw_score)
    except (TypeError, ValueError) as exc:
        raise ResourceLimitError(
            "score.nonportable_json",
            "score must be finite, portable JSON",
        ) from exc
    part_count = len(parsed_score.parts)
    note_count = sum(len(part.notes) for part in parsed_score.parts)
    _raise_if_above(
        code="score.document_too_large",
        label="score JSON bytes",
        actual=len(encoded),
        limit=limits.max_score_json_bytes,
        override="TIANLAI_MAX_SCORE_MIB",
    )
    _raise_if_above(
        code="score.too_many_parts",
        label="score part count",
        actual=part_count,
        limit=limits.max_parts,
        override="TIANLAI_MAX_PARTS",
    )
    _raise_if_above(
        code="score.too_many_notes",
        label="score note count",
        actual=note_count,
        limit=limits.max_notes,
        override="TIANLAI_MAX_NOTES",
    )
    return {
        "score_json_bytes": len(encoded),
        "part_count": part_count,
        "note_count": note_count,
    }


def estimate_render_resources(
    plan: Any,
    *,
    write_stems: bool,
    hall_tail_seconds: float = 0.0,
) -> dict[str, int | float]:
    """Conservatively estimate the engine-owned audio arrays and PCM output."""

    duration = float(plan.duration_seconds)
    sample_rate = int(plan.sample_rate)
    executor_count = len(plan.parts)
    if (
        not math.isfinite(duration)
        or duration < 0.0
        or not math.isfinite(hall_tail_seconds)
        or hall_tail_seconds < 0.0
    ):
        raise ResourceLimitError(
            "render.nonfinite_duration",
            "render duration and hall tail must be finite and non-negative",
        )
    frame_count = max(
        1,
        math.ceil((duration + hall_tail_seconds) * sample_rate),
    )
    # Dry rendering keeps the float64 stereo mix bus (16 B/frame) plus one
    # float32 stereo stem (8 B/frame) and bounded numeric overhead.  The
    # shared hall is materially heavier: stereo mid/side decomposition,
    # four reverb banks, FFT complex spectra/responses and wet outputs overlap
    # in memory.  Use a deliberately conservative peak model instead of
    # pretending the send bus alone describes the cost.
    memory_bytes_per_frame = (
        192 if hall_tail_seconds > 0.0 else 32
    )
    estimated_memory_bytes = frame_count * memory_bytes_per_frame
    # PCM24 stereo is 6 B/frame.  This is the primary published audio only;
    # filesystem overhead, JSON receipts and optional caches are deliberately
    # not presented as exact figures.
    output_streams = 1 + (executor_count if write_stems else 0)
    estimated_primary_output_bytes = frame_count * 6 * output_streams
    return {
        "duration_seconds": duration,
        "sample_rate": sample_rate,
        "executor_count": executor_count,
        "frame_count": frame_count,
        "estimated_audio_memory_bytes": estimated_memory_bytes,
        "estimated_primary_output_bytes": estimated_primary_output_bytes,
    }


_RENDER_BUDGETS = (
    (
        "executor_count",
        "render.too_many_executors",
        "executor count",
        "max_executors",
        "TIANLAI_MAX_EXECUTORS",
    ),
    (
        "duration_seconds",
        "render.duration_too_long",
        "plan seconds",
        "max_plan_seconds",
        "TIANLAI_MAX_PLAN_SECONDS",
    ),
    (
        "estimated_audio_memory_bytes",
        "render.memory_budget_exceeded",
        "estimated audio memory bytes",
        "max_audio_memory_bytes",
        "TIANLAI_MAX_AUDIO_MEMORY_MIB",
    ),
    (
        "estimated_primary_output_bytes",
        "render.output_budget_exceeded",
        "estimated primary output bytes",
        "max_primary_output_bytes",
        "TIANLAI_MAX_OUTPUT_MIB",
    ),
)


def _render_preflight_report(
    plan: Any,
    *,
    write_stems: bool,
    hall_tail_seconds: float,
    collaboration_mode: str,
    stem_cache_enabled: bool,
    limits: ProjectLimits,
) -> dict[str, Any]:
    estimate = estimate_render_resources(
        plan,
        write_stems=write_stems,
        hall_tail_seconds=hall_tail_seconds,
    )
    gates: dict[str, dict[str, Any]] = {}
    for (
        estimate_field,
        code,
        label,
        limit_field,
        override,
    ) in _RENDER_BUDGETS:
        actual = estimate[estimate_field]
        limit = getattr(limits, limit_field)
        gates[estimate_field] = {
            "status": "passed" if actual <= limit else "failed",
            "code": code,
            "label": label,
            "actual": actual,
            "limit": limit,
            "override": override,
        }
    bytes_per_frame = 192 if hall_tail_seconds > 0.0 else 32
    status = (
        "passed"
        if all(gate["status"] == "passed" for gate in gates.values())
        else "failed"
    )
    return {
        **estimate,
        "status": status,
        "passed": status == "passed",
        "render_parameters": {
            "write_stems": write_stems,
            "space_enabled": hall_tail_seconds > 0.0,
            "hall_tail_seconds": hall_tail_seconds,
            "collaboration_mode": collaboration_mode,
            "analysis_enabled": collaboration_mode in {"analyze", "suggest"},
            "stem_cache_enabled": stem_cache_enabled,
        },
        "memory_model": {
            "bytes_per_frame": bytes_per_frame,
            "dry_render_bytes_per_frame": 32,
            "shared_hall_peak_bytes_per_frame": (
                160 if hall_tail_seconds > 0.0 else 0
            ),
            "write_stems": (
                "disk_output_only; stems are rendered and written sequentially"
            ),
            "stem_cache": (
                "one active float32 stem; cache entries do not accumulate in RAM"
            ),
            "collaboration_analysis": (
                "bounded FFT batches; relation audio uses scratch float32 memmap"
            ),
        },
        "limits": limits.to_dict(),
        "gates": gates,
    }


def _enforce_render_preflight(report: dict[str, Any]) -> None:
    for (
        estimate_field,
        code,
        label,
        _limit_field,
        override,
    ) in _RENDER_BUDGETS:
        gate = report["gates"][estimate_field]
        if gate["status"] != "failed":
            continue
        error = ResourceLimitError(
            code,
            f"{label} {gate['actual']:g} exceeds limit {gate['limit']:g}; "
            f"raise {override} deliberately if this project is trusted",
            actual=gate["actual"],
            limit=gate["limit"],
        )
        error.preflight = report
        raise error


def _effective_collaboration_mode(
    plan: Any,
    collaboration_mode: str | None,
) -> str:
    mode = collaboration_mode
    if mode is None:
        mode = getattr(
            getattr(plan, "collaboration", None),
            "mode",
            "manual",
        )
    if mode not in {"manual", "analyze", "suggest"}:
        raise ResourceLimitError(
            "render.invalid_collaboration_mode",
            "collaboration_mode must be manual, analyze or suggest",
        )
    return str(mode)


def validate_render_request_resource_limits(
    plan: Any,
    *,
    write_stems: bool,
    space: Any | None,
    collaboration_mode: str | None,
    stem_cache_enabled: bool,
    limits: ProjectLimits | None = None,
) -> dict[str, Any]:
    """Gate one concrete render request and return its auditable estimate.

    This is the common entrypoint for dry-run validation, CLI ``--plan-only``
    and the renderer itself.  In particular, the hall tail is resolved from
    the exact ``space`` object rather than silently assuming a dry render.
    """

    if not isinstance(write_stems, bool):
        raise ResourceLimitError(
            "render.invalid_write_stems",
            "write_stems must be boolean",
        )
    if not isinstance(stem_cache_enabled, bool):
        raise ResourceLimitError(
            "render.invalid_stem_cache",
            "stem_cache_enabled must be boolean",
        )
    effective_collaboration_mode = _effective_collaboration_mode(
        plan,
        collaboration_mode,
    )
    hall_tail_seconds = (
        0.0
        if space is None
        else float(space.tail_seconds(plan.sample_rate))
    )
    limits = limits or ProjectLimits.from_environment()
    report = _render_preflight_report(
        plan,
        write_stems=write_stems,
        hall_tail_seconds=hall_tail_seconds,
        collaboration_mode=effective_collaboration_mode,
        stem_cache_enabled=stem_cache_enabled,
        limits=limits,
    )
    _enforce_render_preflight(report)
    return report


def validate_plan_resource_limits(
    plan: Any,
    *,
    write_stems: bool,
    hall_tail_seconds: float = 0.0,
    limits: ProjectLimits | None = None,
) -> dict[str, Any]:
    """Reject a compiled plan before any output directory or array is created."""

    limits = limits or ProjectLimits.from_environment()
    report = _render_preflight_report(
        plan,
        write_stems=write_stems,
        hall_tail_seconds=hall_tail_seconds,
        collaboration_mode=_effective_collaboration_mode(plan, None),
        stem_cache_enabled=False,
        limits=limits,
    )
    _enforce_render_preflight(report)
    return report


__all__ = [
    "ProjectLimits",
    "ResourceLimitError",
    "estimate_render_resources",
    "validate_plan_resource_limits",
    "validate_render_request_resource_limits",
    "validate_score_resource_limits",
]
