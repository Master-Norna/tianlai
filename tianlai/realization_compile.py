"""Compile sparse realization intent against one concrete roster.

The realization document is deliberately independent of instruments.  This
module is the narrow adapter which proves that its semantic controls are
actually executable by the selected roster and resolves any explicitly
authorised quantisation before the performance plan is frozen.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from .capability import InstrumentCapability
from .realization import (
    MAX_GATE_RATIO,
    MAX_TIMING_OFFSET_MS,
    ControlLane,
    NoteRealizationOverride,
    NumericOverride,
    RealizationDocument,
    realization_control_point_seconds,
)
from .resource_limits import (
    PlanDocumentBudgetTracker,
    ProjectLimits,
    ResourceLimitError,
    performance_event_limit,
)
from .roster import Roster
from .score import ScoreDocument


@dataclass(frozen=True, slots=True)
class NumericResolution:
    """One note parameter after applying an explicit merge instruction."""

    value: float | None
    evidence: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class CompiledControlEvent:
    """One capability-resolved part control ready for a performance plan."""

    executor_id: str
    time_seconds: float
    name: str
    value: float
    lane_id: str
    part_id: str
    voice: str | None
    interpolation: str
    time_policy: str
    value_policy: str
    semantic_policy: str
    requested_value: float | None
    application: str
    fidelity: str
    semantic_fidelity: str
    approximation_reason: str | None
    capability_source: str
    steps: int | None
    quantization_exponent: float | None
    bar: int
    beat: float
    materialized_default: bool = False

    def performance_event(self) -> dict[str, Any]:
        return {
            "time": round(self.time_seconds, 9),
            "type": "control",
            "name": self.name,
            "value": self.value,
        }

    def trace_entry(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "lane_id": self.lane_id,
            "target": {
                "part_id": self.part_id,
                **({"voice": self.voice} if self.voice is not None else {}),
            },
            "control": self.name,
            "interpolation": self.interpolation,
            "time_policy": self.time_policy,
            "value_policy": self.value_policy,
            "semantic_policy": self.semantic_policy,
            "bar": self.bar,
            "beat": self.beat,
            "time_seconds": round(self.time_seconds, 9),
            "resolved_value": self.value,
            "application": self.application,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "approximation_reason": self.approximation_reason,
            "steps": self.steps,
            "quantization_exponent": self.quantization_exponent,
            "capability_source": self.capability_source,
            "materialized_default": self.materialized_default,
        }
        if self.requested_value is not None:
            result["requested_value"] = self.requested_value
            result["adapted"] = not math.isclose(
                self.requested_value,
                self.value,
                rel_tol=0.0,
                abs_tol=1.0e-12,
            )
        return result


def active_note_overrides(
    realization: RealizationDocument | None,
) -> dict[str, NoteRealizationOverride]:
    """Return only overrides which can change the compiled performance."""

    if realization is None:
        return {}
    return {
        item.event_id: item
        for item in realization.note_overrides
        if not item.is_noop
    }


def _resolved_bound(parameter: str, value: float, *, path: str) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{path} resolves to a non-finite value")
    if parameter == "timing_offset_ms":
        if not -MAX_TIMING_OFFSET_MS <= value <= MAX_TIMING_OFFSET_MS:
            raise ValueError(
                f"{path} resolves outside +/-{MAX_TIMING_OFFSET_MS:g} ms"
            )
        return
    if parameter == "gate_ratio":
        if not 0.0 < value <= MAX_GATE_RATIO:
            raise ValueError(
                f"{path} must resolve within (0, {MAX_GATE_RATIO:g}]"
            )
        return
    if parameter in {"velocity", "release_velocity"}:
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{path} must resolve between 0 and 1")
        return
    raise AssertionError(f"unknown realization parameter: {parameter}")


def resolve_numeric_override(
    parameter: str,
    automatic_value: float | None,
    override: NumericOverride | None,
    *,
    path: str,
) -> NumericResolution:
    """Merge one override without silently clamping an invalid result."""

    if override is None or override.strategy == "auto":
        return NumericResolution(automatic_value, None)
    assert override.value is not None
    strategy = override.strategy
    operand = override.value
    if strategy in {"add", "scale"}:
        if automatic_value is None:
            raise ValueError(
                f"{path}.{strategy} requires an inherited automatic value"
            )
        if strategy == "add":
            resolved = automatic_value + operand
        else:
            resolved = automatic_value * operand
    elif strategy in {"replace", "lock"}:
        resolved = operand
    else:  # The realization parser owns the strategy vocabulary.
        raise AssertionError(f"unsupported merge strategy: {strategy}")
    _resolved_bound(parameter, resolved, path=path)
    return NumericResolution(
        resolved,
        {
            "strategy": strategy,
            "automatic_value": automatic_value,
            "operand": operand,
            "resolved_value": resolved,
            "value_policy": override.value_policy,
            "semantic_policy": override.semantic_policy,
            "locked": strategy == "lock",
            "contract_scope": "performance_plan",
        },
    )


def require_release_velocity_support(
    capability: InstrumentCapability,
    *,
    event_id: str,
) -> None:
    """Reject a release-speed override which the selected backend ignores."""

    if not capability.supports_release_velocity:
        raise ValueError(
            f"realization note {event_id!r} requests release_velocity, but "
            f"{capability.name} ({capability.implementation_type}) does not declare an "
            "audible release-velocity implementation"
        )


def _control_route_index(
    score: ScoreDocument,
    roster: Roster,
) -> tuple[
    dict[tuple[str, str | None], tuple[Any, ...]],
    dict[str, int],
]:
    routes: dict[tuple[str, str | None], dict[str, Any]] = {}
    note_counts: dict[str, int] = {}
    dropped = set(roster.dropped_parts)
    for part in score.parts:
        if part.id in dropped:
            continue
        for note in part.notes:
            executor = roster.route(part.id, note.midi)
            note_counts[executor.executor_id] = (
                note_counts.get(executor.executor_id, 0) + 1
            )
            routes.setdefault((part.id, None), {})[
                executor.executor_id
            ] = executor
            if note.voice is not None:
                routes.setdefault((part.id, note.voice), {})[
                    executor.executor_id
                ] = executor
    return (
        {
            key: tuple(values[item] for item in sorted(values))
            for key, values in routes.items()
        },
        note_counts,
    )


def _lane_executors(
    lane: ControlLane,
    roster: Roster,
    route_index: dict[tuple[str, str | None], tuple[Any, ...]],
) -> tuple[Any, ...]:
    if lane.target.part_id in roster.dropped_parts:
        raise ValueError(
            f"realization control lane {lane.lane_id!r} targets dropped part "
            f"{lane.target.part_id!r}"
        )
    executors = route_index.get(
        (lane.target.part_id, lane.target.voice),
        (),
    )
    if not executors:
        raise ValueError(
            f"realization control lane {lane.lane_id!r} has no routed notes"
        )
    return executors


def compile_control_lanes(
    realization: RealizationDocument | None,
    score: ScoreDocument,
    roster: Roster,
    *,
    event_limit: int | None = None,
    max_time_seconds: float | None = None,
    control_trace_event_limit: int | None = None,
    plan_budget: PlanDocumentBudgetTracker | None = None,
) -> dict[str, tuple[CompiledControlEvent, ...]]:
    """Resolve lanes against exact instrument controls, defaults and grids."""

    if realization is None or not realization.control_lanes:
        return {}
    if (
        event_limit is None
        or max_time_seconds is None
        or control_trace_event_limit is None
    ):
        limits = ProjectLimits.from_environment()
        if event_limit is None:
            event_limit = performance_event_limit(limits)
        if max_time_seconds is None:
            max_time_seconds = float(limits.max_plan_seconds)
        if control_trace_event_limit is None:
            control_trace_event_limit = max(
                1,
                limits.max_score_json_bytes // 512,
            )
    if (
        isinstance(event_limit, bool)
        or not isinstance(event_limit, int)
        or event_limit < 1
    ):
        raise ValueError("event_limit must be a positive integer")
    if (
        isinstance(max_time_seconds, bool)
        or not isinstance(max_time_seconds, (int, float))
        or not math.isfinite(float(max_time_seconds))
        or float(max_time_seconds) <= 0.0
    ):
        raise ValueError("max_time_seconds must be finite and positive")
    if (
        isinstance(control_trace_event_limit, bool)
        or not isinstance(control_trace_event_limit, int)
        or control_trace_event_limit < 1
    ):
        raise ValueError(
            "control_trace_event_limit must be a positive integer"
        )
    point_seconds = realization_control_point_seconds(realization, score)
    route_index, note_counts = _control_route_index(score, roster)
    per_executor_events = {
        executor_id: count * 3
        for executor_id, count in note_counts.items()
    }
    aggregate_events = sum(per_executor_events.values())
    control_event_limit = min(
        max(1, event_limit // 4),
        control_trace_event_limit,
    )
    control_event_count = 0
    if aggregate_events > event_limit:
        raise ResourceLimitError(
            "realization.too_many_compiled_events",
            "score note fan-out leaves no room within the compiled "
            f"performance event limit {event_limit}; raise "
            "TIANLAI_MAX_NOTES deliberately if this project is trusted",
            actual=aggregate_events,
            limit=event_limit,
        )
    prepared: list[
        tuple[
            ControlLane,
            tuple[tuple[Any, Any], ...],
            tuple[float, ...],
        ]
    ] = []
    for lane in realization.control_lanes:
        executors = _lane_executors(lane, roster, route_index)
        lane_times = point_seconds[lane.lane_id]
        first_time = lane_times[0]
        for point_time in lane_times:
            if point_time > float(max_time_seconds):
                raise ResourceLimitError(
                    "realization.control_time_too_late",
                    f"realization lane {lane.lane_id!r} reaches "
                    f"{point_time:g}s, exceeding plan limit "
                    f"{float(max_time_seconds):g}s; raise "
                    "TIANLAI_MAX_PLAN_SECONDS deliberately if trusted",
                    actual=point_time,
                    limit=float(max_time_seconds),
                )
            frame = point_time * score.sample_rate
            if not math.isfinite(frame):
                raise ValueError(
                    f"realization lane {lane.lane_id!r} exceeds the finite "
                    "sample timeline"
                )
        scope = "per_note" if lane.target.voice is not None else "part"
        resolved_executors: list[tuple[Any, Any]] = []
        lane_event_count = 0
        for executor in executors:
            capability = executor.capability.require_control(
                lane.control,
                scope=scope,
                interpolation=lane.interpolation,
            )
            if (
                capability.semantic_fidelity == "approximated"
                and lane.semantic_policy != "approximate"
            ):
                raise ValueError(
                    f"realization control lane {lane.lane_id!r} requires "
                    f"semantic_policy='approximate' for "
                    f"{executor.capability.name} control {lane.control!r}: "
                    f"{capability.approximation_reason}"
                )
            per_executor_addition = len(lane.points) + int(first_time > 0.0)
            lane_event_count += per_executor_addition
            count = per_executor_events.get(executor.executor_id, 0)
            count += per_executor_addition
            per_executor_events[executor.executor_id] = count
            if count > event_limit:
                raise ResourceLimitError(
                    "realization.too_many_executor_events",
                    f"executor {executor.executor_id!r} would compile {count} "
                    f"events, exceeding limit {event_limit}",
                    actual=count,
                    limit=event_limit,
                )
            resolved_executors.append((executor, capability))
        control_event_count += lane_event_count
        if control_event_count > control_event_limit:
            raise ResourceLimitError(
                "realization.too_many_control_events",
                "realization control fan-out would materialize "
                f"{control_event_count} control events, exceeding limit "
                f"{control_event_limit}; raise TIANLAI_MAX_NOTES "
                "deliberately if this project is trusted",
                actual=control_event_count,
                limit=control_event_limit,
            )
        aggregate_events += lane_event_count
        if aggregate_events > event_limit:
            raise ResourceLimitError(
                "realization.too_many_compiled_events",
                "realization control fan-out would compile "
                f"{aggregate_events} events, exceeding limit {event_limit}; "
                "raise TIANLAI_MAX_NOTES deliberately if this project is trusted",
                actual=aggregate_events,
                limit=event_limit,
            )
        prepared.append((lane, tuple(resolved_executors), lane_times))
    compiled: dict[str, list[CompiledControlEvent]] = {}
    for lane, resolved_executors, lane_times in prepared:
        # A voice selector is deliberately modelled for the future, but every
        # audited runtime control is currently part-wide.  Asking for per-note
        # scope makes InstrumentCapability fail closed instead of leaking one
        # voice's expression/pedal state into another.
        for executor, capability in resolved_executors:
            destination = compiled.setdefault(executor.executor_id, [])
            if lane_times[0] > 0.0:
                # Materialise the backend's audited initial state so a sparse
                # lane never depends on an undocumented constructor default.
                capability.require_value(capability.default_value)
                compiled_default = CompiledControlEvent(
                    executor_id=executor.executor_id,
                    time_seconds=0.0,
                    name=lane.control,
                    value=capability.default_value,
                    lane_id=lane.lane_id,
                    part_id=lane.target.part_id,
                    voice=lane.target.voice,
                    interpolation=lane.interpolation,
                    time_policy=lane.time_policy,
                    value_policy=lane.value_policy,
                    semantic_policy=lane.semantic_policy,
                    requested_value=None,
                    application=capability.application,
                    fidelity=capability.fidelity,
                    semantic_fidelity=capability.semantic_fidelity,
                    approximation_reason=capability.approximation_reason,
                    capability_source=capability.source,
                    steps=capability.steps,
                    quantization_exponent=capability.quantization_exponent,
                    bar=1,
                    beat=1.0,
                    materialized_default=True,
                )
                if plan_budget is not None:
                    plan_budget.charge_fragment(
                        {
                            "event": compiled_default.performance_event(),
                            "trace": compiled_default.trace_entry(),
                        },
                        framing_bytes=192,
                    )
                destination.append(compiled_default)
            for point, point_time in zip(lane.points, lane_times):
                if lane.value_policy == "exact":
                    resolved = capability.require_value(point.value)
                elif lane.value_policy == "adapt":
                    resolved = capability.adapt_value(point.value)
                else:
                    raise AssertionError(
                        f"unsupported lane value policy: {lane.value_policy}"
                    )
                compiled_point = CompiledControlEvent(
                    executor_id=executor.executor_id,
                    time_seconds=point_time,
                    name=lane.control,
                    value=resolved,
                    lane_id=lane.lane_id,
                    part_id=lane.target.part_id,
                    voice=lane.target.voice,
                    interpolation=lane.interpolation,
                    time_policy=lane.time_policy,
                    value_policy=lane.value_policy,
                    semantic_policy=lane.semantic_policy,
                    requested_value=point.value,
                    application=capability.application,
                    fidelity=capability.fidelity,
                    semantic_fidelity=capability.semantic_fidelity,
                    approximation_reason=capability.approximation_reason,
                    capability_source=capability.source,
                    steps=capability.steps,
                    quantization_exponent=capability.quantization_exponent,
                    bar=point.bar,
                    beat=point.beat,
                )
                if plan_budget is not None:
                    plan_budget.charge_fragment(
                        {
                            "event": compiled_point.performance_event(),
                            "trace": compiled_point.trace_entry(),
                        },
                        framing_bytes=192,
                    )
                destination.append(compiled_point)
    return {
        executor_id: tuple(
            sorted(
                events,
                key=lambda item: (
                    item.time_seconds,
                    item.lane_id,
                    item.name,
                    item.materialized_default,
                ),
            )
        )
        for executor_id, events in compiled.items()
    }


__all__ = (
    "CompiledControlEvent",
    "NumericResolution",
    "active_note_overrides",
    "compile_control_lanes",
    "require_release_velocity_support",
    "resolve_numeric_override",
)
