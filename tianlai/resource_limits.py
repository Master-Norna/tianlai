"""Fail-closed resource budgets shared by validation and rendering.

Tianlai is intentionally able to render large scores, but accepting an
unbounded Agent-supplied document is unsafe: a perfectly finite duration can
still request gigabytes of RAM or disk.  This module keeps those operational
limits separate from musical validation and makes every default override
explicit through environment variables.  Stem production may use bounded
internal parallelism, while the coordinator still consumes, writes and mixes
stems strictly in performance-plan order.  The additional worker window has
its own automatic, fail-closed resource policy; it is not a user setting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Any

from .canonical_json import canonical_json_bytes


_FLOAT32_STEREO_BYTES_PER_FRAME = 2 * 4
_PCM24_STEREO_BYTES_PER_FRAME = 2 * 3
_ANALYSIS_TRANSACTION_FREE_RESERVE_BYTES = 512 * 1024 * 1024
# A compiled standalone performance normally needs note-on and note-off events,
# plus a bounded allowance for control and articulation changes.  Deriving this
# fan-out budget from the score note budget keeps the two render entrypoints on
# one explicit configuration surface instead of introducing a second event cap.
_PERFORMANCE_EVENTS_PER_NOTE_BUDGET = 4
# A performance plan contains resolved events and audit evidence, so its
# canonical bytes and Python object graph need an independent bound.  Trusted
# non-candidate jobs may opt in to a larger ceiling through a separate,
# explicit environment override.
# Candidate verification already treats every bound JSON member as a 32 MiB
# maximum.  Keeping the default compiled-plan budget at that same boundary
# prevents project-render from creating a candidate which its own verifier
# must reject.  Non-candidate batch users may raise the explicit environment
# override, while candidate publication retains its fixed integrity ceiling.
_DEFAULT_MAX_PLAN_MIB = 32


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
    max_plan_json_bytes: int = _DEFAULT_MAX_PLAN_MIB * 1024 * 1024
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
            max_plan_json_bytes=_environment_positive_int(
                "TIANLAI_MAX_PLAN_MIB", _DEFAULT_MAX_PLAN_MIB
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


def performance_event_limit(
    limits: ProjectLimits | None = None,
) -> int:
    """Return the shared compiled-event ceiling for one trusted project."""

    limits = limits or ProjectLimits.from_environment()
    return limits.max_notes * _PERFORMANCE_EVENTS_PER_NOTE_BUDGET


@dataclass(slots=True)
class PlanDocumentBudgetTracker:
    """Incrementally bound a performance plan before its object graph exists.

    Call :meth:`charge_fragment` immediately before retaining each small JSON
    fragment (for example, an event or trace row).  The fragment is encoded in
    Tianlai's canonical JSON form, charged together with a conservative byte
    of sequence/mapping framing, and rejected before the caller appends it if
    the configured plan budget would be exceeded.  Container metadata can be
    charged as another fragment, with ``framing_bytes`` adjusted when useful.

    Incremental accounting deliberately does not claim to reproduce every
    byte of an as-yet unbuilt nested document.  :meth:`validate_final` is the
    authoritative exact check and must be called on the completed JSON-ready
    plan.  This two-stage contract prevents event/trace amplification during
    construction while still covering structural bytes and caller omissions.
    """

    limits: ProjectLimits | None = None
    charged_bytes: int = field(init=False, default=0)
    fragment_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        if self.limits is None:
            self.limits = ProjectLimits.from_environment()

    @property
    def limit_bytes(self) -> int:
        """Return the configured canonical plan-document ceiling."""

        assert self.limits is not None
        return self.limits.max_plan_json_bytes

    def charge_fragment(
        self,
        fragment: Any,
        *,
        framing_bytes: int = 1,
    ) -> int:
        """Canonically charge one fragment before the caller retains it.

        The default framing byte accounts for the comma or bracket/brace that
        accompanies a value in its containing JSON structure.  A caller may
        provide a larger exact/conservative value for richer local framing.
        State is committed only after the prospective total passes the gate.
        """

        if (
            isinstance(framing_bytes, bool)
            or not isinstance(framing_bytes, int)
            or framing_bytes < 0
        ):
            raise ValueError("framing_bytes must be a non-negative integer")
        try:
            encoded_size = len(canonical_json_bytes(fragment))
        except (TypeError, ValueError) as exc:
            raise ResourceLimitError(
                "plan.nonportable_json",
                "performance plan fragments must be finite, portable JSON",
            ) from exc
        delta = encoded_size + framing_bytes
        prospective = self.charged_bytes + delta
        _raise_if_above(
            code="plan.document_too_large",
            label="estimated performance plan JSON bytes",
            actual=prospective,
            limit=self.limit_bytes,
            override="TIANLAI_MAX_PLAN_MIB",
        )
        self.charged_bytes = prospective
        self.fragment_count += 1
        return delta

    def validate_final(
        self,
        raw_plan: dict[str, Any],
    ) -> dict[str, int]:
        """Apply the exact whole-document gate and return audit counters."""

        report = validate_plan_document_resource_limits(
            raw_plan,
            self.limits,
        )
        return {
            **report,
            "incrementally_charged_bytes": self.charged_bytes,
            "charged_fragment_count": self.fragment_count,
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


def validate_performance_document_resource_limits(
    raw_performance: dict[str, Any],
    limits: ProjectLimits | None = None,
) -> dict[str, int]:
    """Reject oversized standalone event documents before event parsing."""

    limits = limits or ProjectLimits.from_environment()
    raw_events = raw_performance.get("events")
    event_count = len(raw_events) if isinstance(raw_events, list) else 0
    event_limit = performance_event_limit(limits)
    _raise_if_above(
        code="performance.too_many_events",
        label="performance event count",
        actual=event_count,
        limit=event_limit,
        override="TIANLAI_MAX_NOTES",
    )
    try:
        encoded = canonical_json_bytes(raw_performance)
    except (TypeError, ValueError) as exc:
        raise ResourceLimitError(
            "performance.nonportable_json",
            "performance must be finite, portable JSON",
        ) from exc
    _raise_if_above(
        code="performance.document_too_large",
        label="performance JSON bytes",
        actual=len(encoded),
        limit=limits.max_score_json_bytes,
        override="TIANLAI_MAX_SCORE_MIB",
    )
    return {
        "performance_json_bytes": len(encoded),
        "event_count": event_count,
        "event_limit": event_limit,
    }


def validate_plan_document_resource_limits(
    raw_plan: dict[str, Any],
    limits: ProjectLimits | None = None,
) -> dict[str, int]:
    """Apply the authoritative canonical-size gate to a completed plan.

    Use :class:`PlanDocumentBudgetTracker` while constructing an untrusted or
    highly amplified plan; this final check accounts for the complete nested
    document, including container keys and punctuation that are intentionally
    only approximated by fragment charging.
    """

    limits = limits or ProjectLimits.from_environment()
    try:
        encoded = canonical_json_bytes(raw_plan)
    except (TypeError, ValueError) as exc:
        raise ResourceLimitError(
            "plan.nonportable_json",
            "performance plan must be finite, portable JSON",
        ) from exc
    _raise_if_above(
        code="plan.document_too_large",
        label="performance plan JSON bytes",
        actual=len(encoded),
        limit=limits.max_plan_json_bytes,
        override="TIANLAI_MAX_PLAN_MIB",
    )
    return {"plan_json_bytes": len(encoded)}


def validate_single_render_resource_limits(
    performance: Any,
    limits: ProjectLimits | None = None,
) -> dict[str, int | float]:
    """Gate streamed standalone PCM duration and disk output before staging."""

    limits = limits or ProjectLimits.from_environment()
    frame_count = int(performance.total_samples)
    sample_rate = int(performance.sample_rate)
    duration_seconds = frame_count / sample_rate
    estimated_primary_output_bytes = (
        frame_count * _PCM24_STEREO_BYTES_PER_FRAME
    )
    _raise_if_above(
        code="render.duration_too_long",
        label="performance seconds",
        actual=duration_seconds,
        limit=limits.max_plan_seconds,
        override="TIANLAI_MAX_PLAN_SECONDS",
    )
    _raise_if_above(
        code="render.output_budget_exceeded",
        label="estimated primary output bytes",
        actual=estimated_primary_output_bytes,
        limit=limits.max_primary_output_bytes,
        override="TIANLAI_MAX_OUTPUT_MIB",
    )
    return {
        "duration_seconds": duration_seconds,
        "sample_rate": sample_rate,
        "frame_count": frame_count,
        "estimated_primary_output_bytes": estimated_primary_output_bytes,
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
    # This public preflight retains the established final-processing baseline:
    # the float64 stereo mix bus (16 B/frame), a conservative allowance for
    # one coordinator-owned float32 stem (8 B/frame), and bounded numeric
    # overhead.  Long manual-mode stems may instead use bounded private
    # scratch blocks, but retaining the larger baseline keeps analysis and
    # short/direct paths covered.  An optional bounded
    # worker window is admitted separately by the automatic parallelism policy
    # using the runtime CPU count, configured audio-memory budget, live
    # scratch-space facts and verified instrument-resource evidence.
    # The shared hall is materially heavier: stereo mid/side decomposition,
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


def _analysis_transaction_scratch_requirement(
    frame_count: int,
    *,
    write_stems: bool,
) -> int:
    """Return the conservative same-volume free-space gate for one stem.

    The live probe happens after the immutable raw source already exists.  It
    therefore reserves an additional float32 analysis mapping, a possible raw
    cache tee, an optional PCM24 stem payload, and a fixed safety margin.
    """

    if (
        isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count < 0
    ):
        raise ValueError("frame_count must be a non-negative integer")
    if not isinstance(write_stems, bool):
        raise ValueError("write_stems must be boolean")
    bytes_per_frame = 2 * _FLOAT32_STEREO_BYTES_PER_FRAME
    if write_stems:
        bytes_per_frame += _PCM24_STEREO_BYTES_PER_FRAME
    return (
        frame_count * bytes_per_frame
        + _ANALYSIS_TRANSACTION_FREE_RESERVE_BYTES
    )


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
                "write_stems changes published disk output only; stems may "
                "render with bounded internal parallelism using managed "
                "children that may stay warm only inside one proven render "
                "run; their incremental memory and exact raw "
                "scratch claims are admitted atomically across same-user "
                "processes; unavailable slots select the complete serial "
                "path; long manual-mode serial stems also use bounded private "
                "scratch when space permits, and the coordinator consumes, "
                "writes and mixes strictly in performance-plan order"
            ),
            "stem_cache": (
                "the coordinator consumes stems in plan order and verifies "
                "existing cache audio before use; small hits may load directly "
                "while long stems use bounded blocks and at most one exact-size "
                "anonymous snapshot on private render scratch guarded by a "
                "512 MiB free-space reserve; the internal worker window is "
                "bounded separately and cache entries do not accumulate in RAM"
            ),
            "collaboration_analysis": (
                "bounded FFT batches; relation audio uses scratch float32 "
                "memmap; long verified block sources may enter diagnostics "
                "through an automatic same-volume transaction only when live "
                "free space covers analysis, a possible cache tee, optional "
                "PCM24 stem output and a 512 MiB reserve"
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
    "PlanDocumentBudgetTracker",
    "ProjectLimits",
    "ResourceLimitError",
    "estimate_render_resources",
    "performance_event_limit",
    "validate_performance_document_resource_limits",
    "validate_plan_document_resource_limits",
    "validate_plan_resource_limits",
    "validate_render_request_resource_limits",
    "validate_score_resource_limits",
    "validate_single_render_resource_limits",
]
