"""Optional, score-bound performance realization declarations.

``score`` remains the instrument-neutral statement of musical intent.  A
realization is a sparse, optional layer that says how selected score events
should be performed more precisely.  It does not contain backend controls,
instrument keyswitches, mix automation, or rendered events.

This module owns the source contract, parsing and cross-document validation;
the conductor delegates capability resolution to ``realization_compile``.
An absent realization, or an empty one produced by
:func:`empty_realization`, remains a strict no-op for the rendering pipeline.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
import hmac
import json
import math
import re
from typing import Any

from .canonical_json import canonical_json_bytes, canonical_json_sha256
from .score import ScoreDocument, TempoEntry, parse_score_document


REALIZATION_KIND = "tianlai.realization"
REALIZATION_SCHEMA_VERSION = 1
DEFAULTS_PROFILE_V1 = "tianlai.realization-defaults-v1"

REALIZATION_MODES = frozenset(("interpreted", "captured"))
MERGE_STRATEGIES = frozenset(("auto", "add", "scale", "replace", "lock"))
CONTROL_NAMES = frozenset(
    ("expression", "sustain_pedal", "una_corda", "breath")
)
CONTROL_INTERPOLATIONS = frozenset(("step", "linear"))
CONTROL_VALUE_POLICIES = frozenset(("exact", "adapt"))
CONTROL_TIME_POLICIES = frozenset(("exact", "adapt"))
CONTROL_SEMANTIC_POLICIES = frozenset(("exact", "approximate"))

MAX_NOTE_OVERRIDES = 250_000
MAX_CONTROL_LANES = 4_096
MAX_CONTROL_POINTS_PER_LANE = 65_536
MAX_TOTAL_CONTROL_POINTS = 1_000_000
MAX_TIMING_OFFSET_MS = 60_000.0
MAX_GATE_RATIO = 16.0
MAX_OVERRIDE_SCALE = 16.0
MAX_REALIZATION_JSON_BYTES = 64 * 1024 * 1024
MAX_STABLE_ID_LENGTH = 128

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_FIELDS = frozenset(
    (
        "kind",
        "schema_version",
        "score_sha256",
        "defaults_profile",
        "mode",
        "note_overrides",
        "control_lanes",
    )
)
_NOTE_OVERRIDE_FIELDS = frozenset(
    (
        "event_id",
        "timing_offset_ms",
        "gate_ratio",
        "velocity",
        "release_velocity",
    )
)
_NUMERIC_OVERRIDE_FIELDS = frozenset(
    ("strategy", "value", "value_policy", "semantic_policy")
)
_CONTROL_LANE_FIELDS = frozenset(
    (
        "lane_id",
        "target",
        "control",
        "interpolation",
        "time_policy",
        "value_policy",
        "semantic_policy",
        "points",
    )
)
_CONTROL_TARGET_FIELDS = frozenset(("part_id", "voice"))
_CONTROL_POINT_FIELDS = frozenset(("bar", "beat", "value"))


@dataclass(frozen=True, slots=True)
class NumericOverride:
    """One explicit merge instruction for a numeric performance parameter.

    ``auto`` carries no value and leaves the inherited value untouched.
    ``add`` and ``scale`` operate on the automatically interpreted value.
    ``replace`` replaces it at the realization merge stage.  ``lock`` has the
    same numeric result as ``replace`` but additionally forbids later musical
    automation from changing it.  Physical onset compensation, safety checks,
    and backend capability validation remain downstream of every strategy.
    """

    strategy: str
    value: float | None = None
    value_policy: str | None = None
    semantic_policy: str | None = None

    def __post_init__(self) -> None:
        strategy = _required_text(self.strategy, "override.strategy")
        if strategy not in MERGE_STRATEGIES:
            choices = ", ".join(sorted(MERGE_STRATEGIES))
            raise ValueError(f"override.strategy must be one of {choices}")
        if strategy == "auto":
            if self.value is not None:
                raise ValueError(
                    "override.value must be absent when strategy is auto"
                )
            if self.value_policy is not None:
                raise ValueError(
                    "override.value_policy must be absent when strategy is auto"
                )
            if self.semantic_policy is not None:
                raise ValueError(
                    "override.semantic_policy must be absent when strategy is auto"
                )
            return
        if self.value is None:
            raise ValueError(
                f"override.value is required for strategy {strategy}"
            )
        object.__setattr__(
            self,
            "value",
            _finite_number(self.value, "override.value"),
        )
        policy = _required_text(
            self.value_policy,
            "override.value_policy",
        )
        if policy not in CONTROL_VALUE_POLICIES:
            choices = ", ".join(sorted(CONTROL_VALUE_POLICIES))
            raise ValueError(
                f"override.value_policy must be one of {choices}"
            )
        semantic_policy = _required_text(
            self.semantic_policy,
            "override.semantic_policy",
        )
        if semantic_policy not in CONTROL_SEMANTIC_POLICIES:
            choices = ", ".join(sorted(CONTROL_SEMANTIC_POLICIES))
            raise ValueError(
                f"override.semantic_policy must be one of {choices}"
            )

    @property
    def is_noop(self) -> bool:
        return self.strategy == "auto"

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"strategy": self.strategy}
        if self.value is not None:
            result["value"] = self.value
            result["value_policy"] = self.value_policy
            result["semantic_policy"] = self.semantic_policy
        return result


@dataclass(frozen=True, slots=True)
class NoteRealizationOverride:
    """Sparse realization instructions for one stable score-v1 event ID."""

    event_id: str
    timing_offset_ms: NumericOverride | None = None
    gate_ratio: NumericOverride | None = None
    velocity: NumericOverride | None = None
    release_velocity: NumericOverride | None = None

    def __post_init__(self) -> None:
        _stable_id(self.event_id, "note_override.event_id")
        present = False
        for field in (
            "timing_offset_ms",
            "gate_ratio",
            "velocity",
            "release_velocity",
        ):
            value = getattr(self, field)
            if value is None:
                continue
            present = True
            if not isinstance(value, NumericOverride):
                raise TypeError(
                    f"note_override.{field} must be a NumericOverride"
                )
            if not value.is_noop:
                assert value.value is not None
                _validate_override_value(
                    field,
                    value.strategy,
                    value.value,
                    f"note_override.{field}.value",
                )
        if not present:
            raise ValueError(
                "note_override must declare at least one override parameter"
            )

    @property
    def is_noop(self) -> bool:
        values = (
            self.timing_offset_ms,
            self.gate_ratio,
            self.velocity,
            self.release_velocity,
        )
        return all(value is None or value.is_noop for value in values)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"event_id": self.event_id}
        for field in (
            "timing_offset_ms",
            "gate_ratio",
            "velocity",
            "release_velocity",
        ):
            value = getattr(self, field)
            if value is not None:
                result[field] = value.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class ControlTarget:
    """A whole score part, or every matching voice label within that part."""

    part_id: str
    voice: str | None = None

    def __post_init__(self) -> None:
        _stable_id(self.part_id, "control_target.part_id")
        if self.voice is not None:
            _stable_id(self.voice, "control_target.voice")

    def to_dict(self) -> dict[str, str]:
        result = {"part_id": self.part_id}
        if self.voice is not None:
            result["voice"] = self.voice
        return result


@dataclass(frozen=True, slots=True)
class ControlPoint:
    """One normalized control value at an exact score bar/beat location."""

    bar: int
    beat: float
    value: float

    def __post_init__(self) -> None:
        bar = _positive_integer(self.bar, "control_point.bar")
        beat = _finite_number(self.beat, "control_point.beat")
        if beat < 1.0:
            raise ValueError("control_point.beat must be at least 1")
        value = _finite_number(self.value, "control_point.value")
        _bounded(
            value,
            minimum=0.0,
            maximum=1.0,
            path="control_point.value",
        )
        object.__setattr__(self, "bar", bar)
        object.__setattr__(self, "beat", beat)
        object.__setattr__(self, "value", value)

    def to_dict(self) -> dict[str, int | float]:
        return {"bar": self.bar, "beat": self.beat, "value": self.value}


@dataclass(frozen=True, slots=True)
class ControlLane:
    """A sparse, ordered part/voice control envelope."""

    lane_id: str
    target: ControlTarget
    control: str
    interpolation: str
    time_policy: str
    value_policy: str
    semantic_policy: str
    points: tuple[ControlPoint, ...]

    def __post_init__(self) -> None:
        _stable_id(self.lane_id, "control_lane.lane_id")
        if not isinstance(self.target, ControlTarget):
            raise TypeError("control_lane.target must be a ControlTarget")
        control = _required_text(self.control, "control_lane.control")
        if control not in CONTROL_NAMES:
            choices = ", ".join(sorted(CONTROL_NAMES))
            raise ValueError(f"control_lane.control must be one of {choices}")
        interpolation = _required_text(
            self.interpolation,
            "control_lane.interpolation",
        )
        if interpolation not in CONTROL_INTERPOLATIONS:
            choices = ", ".join(sorted(CONTROL_INTERPOLATIONS))
            raise ValueError(
                f"control_lane.interpolation must be one of {choices}"
            )
        time_policy = _required_text(
            self.time_policy,
            "control_lane.time_policy",
        )
        if time_policy not in CONTROL_TIME_POLICIES:
            choices = ", ".join(sorted(CONTROL_TIME_POLICIES))
            raise ValueError(
                f"control_lane.time_policy must be one of {choices}"
            )
        value_policy = _required_text(
            self.value_policy,
            "control_lane.value_policy",
        )
        if value_policy not in CONTROL_VALUE_POLICIES:
            choices = ", ".join(sorted(CONTROL_VALUE_POLICIES))
            raise ValueError(
                f"control_lane.value_policy must be one of {choices}"
            )
        semantic_policy = _required_text(
            self.semantic_policy,
            "control_lane.semantic_policy",
        )
        if semantic_policy not in CONTROL_SEMANTIC_POLICIES:
            choices = ", ".join(sorted(CONTROL_SEMANTIC_POLICIES))
            raise ValueError(
                f"control_lane.semantic_policy must be one of {choices}"
            )
        if not isinstance(self.points, tuple):
            raise TypeError("control_lane.points must be a tuple")
        if not self.points:
            raise ValueError("control_lane.points must not be empty")
        if len(self.points) > MAX_CONTROL_POINTS_PER_LANE:
            raise ValueError(
                "control_lane.points exceeds "
                f"{MAX_CONTROL_POINTS_PER_LANE} points"
            )
        previous: tuple[int, float] | None = None
        for position, point in enumerate(self.points):
            if not isinstance(point, ControlPoint):
                raise TypeError(
                    f"control_lane.points[{position}] must be a ControlPoint"
                )
            current = (point.bar, point.beat)
            if previous is not None and current <= previous:
                raise ValueError(
                    f"control_lane.points[{position}] must follow the "
                    "previous point"
                )
            previous = current

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "target": self.target.to_dict(),
            "control": self.control,
            "interpolation": self.interpolation,
            "time_policy": self.time_policy,
            "value_policy": self.value_policy,
            "semantic_policy": self.semantic_policy,
            "points": [point.to_dict() for point in self.points],
        }


@dataclass(frozen=True, slots=True)
class RealizationDocument:
    """A structurally validated, immutable realization-v1 document.

    Score identity and references still require
    :func:`parse_realization_document` with raw ``score_document`` evidence.
    """

    score_sha256: str
    defaults_profile: str
    mode: str
    note_overrides: tuple[NoteRealizationOverride, ...] = ()
    control_lanes: tuple[ControlLane, ...] = ()
    kind: str = REALIZATION_KIND
    schema_version: int = REALIZATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.kind != REALIZATION_KIND:
            raise ValueError(f"realization.kind must be {REALIZATION_KIND!r}")
        try:
            schema_version = _integer(self.schema_version, "schema_version")
        except ValueError:
            schema_version = None
        if schema_version != REALIZATION_SCHEMA_VERSION:
            raise ValueError(
                "realization.schema_version must be "
                f"{REALIZATION_SCHEMA_VERSION}"
            )
        object.__setattr__(self, "schema_version", schema_version)
        _sha256(self.score_sha256, "realization.score_sha256")
        defaults_profile = _required_text(
            self.defaults_profile,
            "realization.defaults_profile",
        )
        if defaults_profile != DEFAULTS_PROFILE_V1:
            raise ValueError(
                "realization.defaults_profile is unsupported; expected "
                f"{DEFAULTS_PROFILE_V1!r}"
            )
        mode = _required_text(self.mode, "realization.mode")
        if mode not in REALIZATION_MODES:
            choices = ", ".join(sorted(REALIZATION_MODES))
            raise ValueError(f"realization.mode must be one of {choices}")
        if not isinstance(self.note_overrides, tuple):
            raise TypeError("realization.note_overrides must be a tuple")
        if not isinstance(self.control_lanes, tuple):
            raise TypeError("realization.control_lanes must be a tuple")
        if len(self.note_overrides) > MAX_NOTE_OVERRIDES:
            raise ValueError(
                "realization.note_overrides exceeds "
                f"{MAX_NOTE_OVERRIDES} entries"
            )
        if len(self.control_lanes) > MAX_CONTROL_LANES:
            raise ValueError(
                "realization.control_lanes exceeds "
                f"{MAX_CONTROL_LANES} entries"
            )

        seen_event_ids: set[str] = set()
        for position, override in enumerate(self.note_overrides):
            if not isinstance(override, NoteRealizationOverride):
                raise TypeError(
                    "realization.note_overrides"
                    f"[{position}] must be a NoteRealizationOverride"
                )
            if override.event_id in seen_event_ids:
                raise ValueError(
                    "realization.note_overrides contains duplicate event_id "
                    f"at index {position}: {override.event_id!r}"
                )
            seen_event_ids.add(override.event_id)

        total_points = 0
        seen_lane_ids: set[str] = set()
        seen_lane_targets: set[tuple[str, str | None, str]] = set()
        for position, lane in enumerate(self.control_lanes):
            if not isinstance(lane, ControlLane):
                raise TypeError(
                    "realization.control_lanes"
                    f"[{position}] must be a ControlLane"
                )
            total_points += len(lane.points)
            if lane.lane_id in seen_lane_ids:
                raise ValueError(
                    "realization.control_lanes contains duplicate lane_id "
                    f"at index {position}: {lane.lane_id!r}"
                )
            seen_lane_ids.add(lane.lane_id)
            target_key = (
                lane.target.part_id,
                lane.target.voice,
                lane.control,
            )
            if target_key in seen_lane_targets:
                raise ValueError(
                    "realization.control_lanes contains competing lanes for "
                    f"target/control at index {position}: {target_key!r}"
                )
            seen_lane_targets.add(target_key)
        if total_points > MAX_TOTAL_CONTROL_POINTS:
            raise ValueError(
                "realization control point count exceeds "
                f"{MAX_TOTAL_CONTROL_POINTS}"
            )

    @property
    def is_noop(self) -> bool:
        return not self.control_lanes and all(
            override.is_noop for override in self.note_overrides
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "schema_version": self.schema_version,
            "score_sha256": self.score_sha256,
            "defaults_profile": self.defaults_profile,
            "mode": self.mode,
            "note_overrides": [item.to_dict() for item in self.note_overrides],
            "control_lanes": [item.to_dict() for item in self.control_lanes],
        }


def _reject_unknown_fields(
    value: dict[Any, Any], allowed: frozenset[str], path: str
) -> None:
    unknown = sorted(str(key) for key in value if key not in allowed)
    if unknown:
        raise ValueError(f"{path} contains unknown fields: {', '.join(unknown)}")


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a non-empty string")
    return value


def _stable_id(value: object, path: str) -> str:
    text = _required_text(value, path)
    if len(text) > MAX_STABLE_ID_LENGTH:
        raise ValueError(
            f"{path} exceeds {MAX_STABLE_ID_LENGTH} characters"
        )
    return text


def _finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{path} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{path} must be a finite number")
    return number


def _integer(value: object, path: str) -> int:
    """Return the JSON-Schema integer value as a Python ``int``.

    Draft 2020-12 defines ``integer`` by mathematical value, so a finite
    floating-point value such as ``1.0`` is an integer instance too.  Booleans
    and non-finite/non-integral floats remain outside that value space.
    """

    if isinstance(value, bool):
        raise ValueError(f"{path} must be an integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    raise ValueError(f"{path} must be an integer")


def _positive_integer(value: object, path: str) -> int:
    try:
        integer = _integer(value, path)
    except ValueError:
        raise ValueError(f"{path} must be an integer starting at 1")
    if integer < 1:
        raise ValueError(f"{path} must be an integer starting at 1")
    return integer


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{path} must be a lowercase SHA-256 digest")
    return value


def _bounded(
    value: float,
    *,
    minimum: float,
    maximum: float,
    path: str,
    exclusive_minimum: bool = False,
) -> None:
    lower_ok = value > minimum if exclusive_minimum else value >= minimum
    if not lower_ok or value > maximum:
        lower = ">" if exclusive_minimum else ">="
        raise ValueError(
            f"{path} must be {lower} {minimum:g} and <= {maximum:g}"
        )


def _validate_override_value(
    parameter: str,
    strategy: str,
    value: float,
    path: str,
) -> None:
    if parameter == "timing_offset_ms":
        if strategy == "scale":
            _bounded(
                value,
                minimum=0.0,
                maximum=MAX_OVERRIDE_SCALE,
                path=path,
            )
        else:
            _bounded(
                value,
                minimum=-MAX_TIMING_OFFSET_MS,
                maximum=MAX_TIMING_OFFSET_MS,
                path=path,
            )
        return

    if parameter == "gate_ratio":
        if strategy == "add":
            _bounded(
                value,
                minimum=-MAX_GATE_RATIO,
                maximum=MAX_GATE_RATIO,
                path=path,
            )
        else:
            _bounded(
                value,
                minimum=0.0,
                maximum=MAX_GATE_RATIO,
                path=path,
                exclusive_minimum=True,
            )
        return

    if parameter in ("velocity", "release_velocity"):
        if strategy == "add":
            _bounded(value, minimum=-1.0, maximum=1.0, path=path)
        elif strategy == "scale":
            _bounded(
                value,
                minimum=0.0,
                maximum=MAX_OVERRIDE_SCALE,
                path=path,
            )
        else:
            _bounded(value, minimum=0.0, maximum=1.0, path=path)
        return

    raise AssertionError(f"unhandled realization parameter: {parameter}")


def _parse_numeric_override(
    raw: object,
    *,
    parameter: str,
    path: str,
) -> NumericOverride:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_fields(raw, _NUMERIC_OVERRIDE_FIELDS, path)
    strategy = _required_text(raw.get("strategy"), f"{path}.strategy")
    if strategy not in MERGE_STRATEGIES:
        choices = ", ".join(sorted(MERGE_STRATEGIES))
        raise ValueError(f"{path}.strategy must be one of {choices}")
    if strategy == "auto":
        if (
            "value" in raw
            or "value_policy" in raw
            or "semantic_policy" in raw
        ):
            raise ValueError(
                f"{path}.value, value_policy, and semantic_policy must be "
                "absent when strategy is auto"
            )
        return NumericOverride(strategy="auto")
    if "value" not in raw:
        raise ValueError(f"{path}.value is required for strategy {strategy}")
    value = _finite_number(raw["value"], f"{path}.value")
    value_policy = _required_text(
        raw.get("value_policy"),
        f"{path}.value_policy",
    )
    if value_policy not in CONTROL_VALUE_POLICIES:
        choices = ", ".join(sorted(CONTROL_VALUE_POLICIES))
        raise ValueError(f"{path}.value_policy must be one of {choices}")
    semantic_policy = _required_text(
        raw.get("semantic_policy"),
        f"{path}.semantic_policy",
    )
    if semantic_policy not in CONTROL_SEMANTIC_POLICIES:
        choices = ", ".join(sorted(CONTROL_SEMANTIC_POLICIES))
        raise ValueError(f"{path}.semantic_policy must be one of {choices}")
    _validate_override_value(parameter, strategy, value, f"{path}.value")
    return NumericOverride(
        strategy=strategy,
        value=value,
        value_policy=value_policy,
        semantic_policy=semantic_policy,
    )


def _parse_note_override(raw: object, position: int) -> NoteRealizationOverride:
    path = f"realization.note_overrides[{position}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_fields(raw, _NOTE_OVERRIDE_FIELDS, path)
    event_id = _stable_id(raw.get("event_id"), f"{path}.event_id")
    present = [
        field
        for field in (
            "timing_offset_ms",
            "gate_ratio",
            "velocity",
            "release_velocity",
        )
        if field in raw
    ]
    if not present:
        raise ValueError(f"{path} must declare at least one override parameter")
    parsed = {
        field: _parse_numeric_override(
            raw[field],
            parameter=field,
            path=f"{path}.{field}",
        )
        for field in present
    }
    return NoteRealizationOverride(event_id=event_id, **parsed)


def _parse_control_target(raw: object, path: str) -> ControlTarget:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_fields(raw, _CONTROL_TARGET_FIELDS, path)
    part_id = _stable_id(raw.get("part_id"), f"{path}.part_id")
    voice = raw.get("voice")
    if voice is not None:
        voice = _stable_id(voice, f"{path}.voice")
    return ControlTarget(part_id=part_id, voice=voice)


def _parse_control_point(
    raw: object,
    lane_position: int,
    point_position: int,
) -> ControlPoint:
    path = (
        f"realization.control_lanes[{lane_position}].points[{point_position}]"
    )
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_fields(raw, _CONTROL_POINT_FIELDS, path)
    if "bar" not in raw or "beat" not in raw or "value" not in raw:
        raise ValueError(f"{path} requires bar, beat, and value")
    bar = _positive_integer(raw["bar"], f"{path}.bar")
    beat = _finite_number(raw["beat"], f"{path}.beat")
    if beat < 1.0:
        raise ValueError(f"{path}.beat must be at least 1")
    value = _finite_number(raw["value"], f"{path}.value")
    _bounded(value, minimum=0.0, maximum=1.0, path=f"{path}.value")
    return ControlPoint(bar=bar, beat=beat, value=value)


def _parse_control_lane(raw: object, position: int) -> ControlLane:
    path = f"realization.control_lanes[{position}]"
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_fields(raw, _CONTROL_LANE_FIELDS, path)
    lane_id = _stable_id(raw.get("lane_id"), f"{path}.lane_id")
    target = _parse_control_target(raw.get("target"), f"{path}.target")
    control = _required_text(raw.get("control"), f"{path}.control")
    if control not in CONTROL_NAMES:
        choices = ", ".join(sorted(CONTROL_NAMES))
        raise ValueError(f"{path}.control must be one of {choices}")
    interpolation = _required_text(
        raw.get("interpolation"), f"{path}.interpolation"
    )
    if interpolation not in CONTROL_INTERPOLATIONS:
        choices = ", ".join(sorted(CONTROL_INTERPOLATIONS))
        raise ValueError(f"{path}.interpolation must be one of {choices}")
    time_policy = _required_text(
        raw.get("time_policy"), f"{path}.time_policy"
    )
    if time_policy not in CONTROL_TIME_POLICIES:
        choices = ", ".join(sorted(CONTROL_TIME_POLICIES))
        raise ValueError(f"{path}.time_policy must be one of {choices}")
    value_policy = _required_text(
        raw.get("value_policy"), f"{path}.value_policy"
    )
    if value_policy not in CONTROL_VALUE_POLICIES:
        choices = ", ".join(sorted(CONTROL_VALUE_POLICIES))
        raise ValueError(f"{path}.value_policy must be one of {choices}")
    semantic_policy = _required_text(
        raw.get("semantic_policy"), f"{path}.semantic_policy"
    )
    if semantic_policy not in CONTROL_SEMANTIC_POLICIES:
        choices = ", ".join(sorted(CONTROL_SEMANTIC_POLICIES))
        raise ValueError(f"{path}.semantic_policy must be one of {choices}")
    raw_points = raw.get("points")
    if not isinstance(raw_points, list) or not raw_points:
        raise ValueError(f"{path}.points must be a non-empty array")
    if len(raw_points) > MAX_CONTROL_POINTS_PER_LANE:
        raise ValueError(
            f"{path}.points exceeds {MAX_CONTROL_POINTS_PER_LANE} points"
        )
    points = tuple(
        _parse_control_point(item, position, point_position)
        for point_position, item in enumerate(raw_points)
    )
    first = points[0]
    previous = (first.bar, first.beat)
    for point_position, point in enumerate(points[1:], start=1):
        current = (point.bar, point.beat)
        if current <= previous:
            raise ValueError(
                f"{path}.points[{point_position}] must follow the previous point"
            )
        previous = current
    return ControlLane(
        lane_id=lane_id,
        target=target,
        control=control,
        interpolation=interpolation,
        time_policy=time_policy,
        value_policy=value_policy,
        semantic_policy=semantic_policy,
        points=points,
    )


@dataclass(frozen=True, slots=True)
class _ScoreTimeIndex:
    """Finite, logarithmic score-time lookup for bound realization data."""

    meter_bars: tuple[int, ...]
    meter_entries: tuple[TempoEntry, ...]
    meter_quarters: tuple[float, ...]
    tempo_quarters: tuple[float, ...]
    tempo_seconds: tuple[float, ...]
    tempo_bpms: tuple[float, ...]

    @classmethod
    def from_score(cls, score: ScoreDocument) -> _ScoreTimeIndex:
        entries = score.tempo_map.entries
        if not entries:
            raise ValueError("score.tempo_map must contain at least one entry")
        meter_entries = tuple(entry for entry in entries if entry.changes_meter)
        if not meter_entries or meter_entries[0].bar != 1:
            raise ValueError("score.tempo_map must define meter at bar 1")

        meter_quarters = [0.0]
        for position, entry in enumerate(meter_entries[1:], start=1):
            previous = meter_entries[position - 1]
            try:
                span = (
                    entry.bar - previous.bar
                ) * previous.quarters_per_bar
                quarter = meter_quarters[-1] + span
            except OverflowError as exc:
                raise ValueError(
                    "score.tempo_map exceeds the finite score-time range"
                ) from exc
            if not math.isfinite(quarter):
                raise ValueError(
                    "score.tempo_map exceeds the finite score-time range"
                )
            meter_quarters.append(quarter)

        index = cls(
            meter_bars=tuple(entry.bar for entry in meter_entries),
            meter_entries=meter_entries,
            meter_quarters=tuple(meter_quarters),
            tempo_quarters=(),
            tempo_seconds=(),
            tempo_bpms=(),
        )
        tempo_quarters: list[float] = []
        for position, entry in enumerate(entries):
            quarter, _, _ = index.position(
                entry.bar,
                entry.beat,
                path=f"score.tempo_map[{position}]",
                resolve_seconds=False,
            )
            if tempo_quarters and quarter <= tempo_quarters[-1]:
                raise ValueError(
                    f"score.tempo_map[{position}] does not advance "
                    "logical score time"
                )
            tempo_quarters.append(quarter)
        if not math.isclose(tempo_quarters[0], 0.0, abs_tol=1e-12):
            raise ValueError("score.tempo_map[0] must start at bar 1 beat 1")

        tempo_seconds = [0.0]
        for position in range(1, len(entries)):
            span = tempo_quarters[position] - tempo_quarters[position - 1]
            seconds = (
                tempo_seconds[-1]
                + span * 60.0 / entries[position - 1].bpm
            )
            if not math.isfinite(seconds):
                raise ValueError(
                    "score.tempo_map exceeds the finite score-time range"
                )
            tempo_seconds.append(seconds)

        index = cls(
            meter_bars=index.meter_bars,
            meter_entries=index.meter_entries,
            meter_quarters=index.meter_quarters,
            tempo_quarters=tuple(tempo_quarters),
            tempo_seconds=tuple(tempo_seconds),
            tempo_bpms=tuple(entry.bpm for entry in entries),
        )
        index.validate_score_coordinates(score)
        return index

    def position(
        self,
        bar: int,
        beat: float,
        *,
        path: str,
        resolve_seconds: bool = True,
    ) -> tuple[float, float, float]:
        checked_bar = _positive_integer(bar, f"{path}.bar")
        checked_beat = _finite_number(beat, f"{path}.beat")
        meter_position = bisect_right(self.meter_bars, checked_bar) - 1
        if meter_position < 0:
            raise ValueError(f"{path}.bar has no governing meter")
        meter = self.meter_entries[meter_position]
        try:
            upper = float(meter.beats_per_bar) + 1.0
            quarters_per_bar = meter.quarters_per_bar
            quarters_per_beat = meter.quarters_per_beat
        except OverflowError as exc:
            raise ValueError(
                f"{path} exceeds the finite score-time range"
            ) from exc
        if not all(
            math.isfinite(value)
            for value in (upper, quarters_per_bar, quarters_per_beat)
        ):
            raise ValueError(f"{path} exceeds the finite score-time range")
        if checked_beat < 1.0 or checked_beat >= upper:
            raise ValueError(
                f"{path}.beat={checked_beat:g} is outside [1, {upper:g}) "
                f"for bar {checked_bar} in "
                f"{meter.beats_per_bar}/{meter.beat_unit}"
            )
        try:
            quarter = (
                self.meter_quarters[meter_position]
                + (checked_bar - meter.bar) * quarters_per_bar
                + (checked_beat - 1.0) * quarters_per_beat
            )
        except OverflowError as exc:
            raise ValueError(
                f"{path} exceeds the finite score-time range"
            ) from exc
        if not math.isfinite(quarter):
            raise ValueError(f"{path} exceeds the finite score-time range")
        if not resolve_seconds:
            return quarter, 0.0, quarters_per_beat
        tempo_position = bisect_right(self.tempo_quarters, quarter) - 1
        if tempo_position < 0:
            raise ValueError(f"{path} precedes the score tempo domain")
        seconds = self.tempo_seconds[tempo_position] + (
            quarter - self.tempo_quarters[tempo_position]
        ) * 60.0 / self.tempo_bpms[tempo_position]
        if not math.isfinite(seconds):
            raise ValueError(f"{path} exceeds the finite score-time range")
        return quarter, seconds, quarters_per_beat

    def seconds_at_quarter(self, quarter: float, *, path: str) -> float:
        checked = _finite_number(quarter, path)
        if checked < 0.0:
            raise ValueError(f"{path} must not be negative")
        tempo_position = bisect_right(self.tempo_quarters, checked) - 1
        if tempo_position < 0:
            raise ValueError(f"{path} precedes the score tempo domain")
        seconds = self.tempo_seconds[tempo_position] + (
            checked - self.tempo_quarters[tempo_position]
        ) * 60.0 / self.tempo_bpms[tempo_position]
        if not math.isfinite(seconds):
            raise ValueError(f"{path} exceeds the finite score-time range")
        return seconds

    def validate_score_coordinates(self, score: ScoreDocument) -> None:
        if not math.isfinite(score.tail_seconds) or score.tail_seconds < 0.0:
            raise ValueError(
                "score.tail_seconds must be a finite non-negative number"
            )
        for part_position, part in enumerate(score.parts):
            part_path = f"score.parts[{part_position}]"
            for note in part.notes:
                start, _, quarters_per_beat = self.position(
                    note.bar,
                    note.beat,
                    path=f"{part_path}.notes[{note.index}]",
                )
                try:
                    end = start + note.duration_beats * quarters_per_beat
                except OverflowError as exc:
                    raise ValueError(
                        f"{part_path}.notes[{note.index}] exceeds the finite "
                        "score-time range"
                    ) from exc
                if not math.isfinite(end) or end <= start:
                    raise ValueError(
                        f"{part_path}.notes[{note.index}] exceeds the finite "
                        "score-time range"
                    )
            for phrase_position, phrase in enumerate(part.phrases):
                phrase_path = f"{part_path}.phrases[{phrase_position}]"
                start, _, _ = self.position(
                    phrase.start_bar,
                    phrase.start_beat,
                    path=f"{phrase_path}.start",
                )
                end, _, _ = self.position(
                    phrase.end_bar,
                    phrase.end_beat,
                    path=f"{phrase_path}.end",
                )
                if end < start:
                    raise ValueError(
                        f"{phrase_path}.end must not precede "
                        f"{phrase_path}.start"
                    )


def validate_realization_references(
    realization: RealizationDocument,
    score: ScoreDocument,
) -> None:
    """Validate every sparse realization reference against one parsed score.

    Stable score-event identity is required because legacy scores can only
    address notes by mutable array position.  A voice target denotes every
    note in the named part whose ``voice`` string matches; MusicXML staff
    scoping does not change that explicit definition.
    """

    if not isinstance(realization, RealizationDocument):
        raise TypeError("realization must be a RealizationDocument")
    if not isinstance(score, ScoreDocument):
        raise TypeError("score must be a parsed ScoreDocument")
    if not score.has_stable_event_identity:
        raise ValueError("realization references require score.schema_version 1")
    time_index = _ScoreTimeIndex.from_score(score)

    event_ids = {
        note.source_event_id
        for part in score.parts
        for note in part.notes
        if note.source_event_id is not None
    }
    tie_continuations = _tie_continuation_heads(score, time_index)
    for position, override in enumerate(realization.note_overrides):
        if override.event_id not in event_ids:
            raise ValueError(
                "realization.note_overrides"
                f"[{position}].event_id references unknown score event "
                f"{override.event_id!r}"
            )
        chain_head = tie_continuations.get(override.event_id)
        if chain_head is not None:
            raise ValueError(
                "realization.note_overrides"
                f"[{position}].event_id targets tie continuation "
                f"{override.event_id!r}; realization v1 requires the "
                f"tie-chain head event_id {chain_head!r}"
            )

    parts = {
        part.id: (part, frozenset(note.voice for note in part.notes))
        for part in score.parts
    }
    for lane_position, lane in enumerate(realization.control_lanes):
        path = f"realization.control_lanes[{lane_position}]"
        part_context = parts.get(lane.target.part_id)
        if part_context is None:
            raise ValueError(
                f"{path}.target.part_id references unknown score part "
                f"{lane.target.part_id!r}"
            )
        part, voices = part_context
        if not part.notes:
            raise ValueError(f"{path}.target references a score part with no notes")
        if lane.target.voice is not None and lane.target.voice not in voices:
            raise ValueError(
                f"{path}.target.voice references no note in part "
                f"{lane.target.part_id!r}: {lane.target.voice!r}"
            )
        for point_position, point in enumerate(lane.points):
            point_path = f"{path}.points[{point_position}]"
            time_index.position(
                point.bar,
                point.beat,
                path=point_path,
            )


def realization_control_point_seconds(
    realization: RealizationDocument,
    score: ScoreDocument,
) -> dict[str, tuple[float, ...]]:
    """Resolve every lane point with one indexed score-time snapshot."""

    if not isinstance(realization, RealizationDocument):
        raise TypeError("realization must be a RealizationDocument")
    if not isinstance(score, ScoreDocument):
        raise TypeError("score must be a parsed ScoreDocument")
    time_index = _ScoreTimeIndex.from_score(score)
    result: dict[str, tuple[float, ...]] = {}
    for lane_position, lane in enumerate(realization.control_lanes):
        values: list[float] = []
        for point_position, point in enumerate(lane.points):
            _quarter, seconds, _quarters_per_beat = time_index.position(
                point.bar,
                point.beat,
                path=(
                    f"realization.control_lanes[{lane_position}]"
                    f".points[{point_position}]"
                ),
            )
            values.append(seconds)
        result[lane.lane_id] = tuple(values)
    return result


def realization_note_time_bounds(
    score: ScoreDocument,
) -> tuple[tuple[str, int, float, float], ...]:
    """Return finite note start/end seconds using one indexed tempo map."""

    if not isinstance(score, ScoreDocument):
        raise TypeError("score must be a parsed ScoreDocument")
    time_index = _ScoreTimeIndex.from_score(score)
    result: list[tuple[str, int, float, float]] = []
    for part in score.parts:
        for note in part.notes:
            path = f"score.parts[{part.id!r}].notes[{note.index}]"
            start_quarter, start_seconds, quarters_per_beat = (
                time_index.position(note.bar, note.beat, path=path)
            )
            end_quarter = (
                start_quarter
                + note.duration_beats * quarters_per_beat
            )
            end_seconds = time_index.seconds_at_quarter(
                end_quarter,
                path=f"{path}.end",
            )
            result.append((part.id, note.index, start_seconds, end_seconds))
    return tuple(result)


def _tie_continuation_heads(
    score: ScoreDocument,
    time_index: _ScoreTimeIndex,
) -> dict[str, str]:
    """Return continuation event IDs mapped to their sounding chain head.

    This deliberately mirrors the conductor's score-level tie merge without
    importing the conductor.  A continuation currently has no independent
    rendered event identity, so accepting its override would silently lose
    intent when realization compilation is added later.
    """

    continuations: dict[str, str] = {}
    for part in score.parts:
        pending: dict[
            tuple[float, int | None, str | None],
            tuple[float, str | None],
        ] = {}
        for note in part.notes:
            start, _, quarters_per_beat = time_index.position(
                note.bar,
                note.beat,
                path=f"score.parts[{part.id!r}].notes[{note.index}]",
            )
            duration = note.duration_beats * quarters_per_beat
            tie_key = (note.midi, note.staff, note.voice)
            held = pending.pop(tie_key, None)
            if held is not None:
                held_end, chain_head = held
                if math.isclose(held_end, start, abs_tol=1e-6):
                    if (
                        note.source_event_id is not None
                        and chain_head is not None
                    ):
                        continuations[note.source_event_id] = chain_head
                    if note.tie:
                        pending[tie_key] = (held_end + duration, chain_head)
                    continue
            if note.tie:
                pending[tie_key] = (
                    start + duration,
                    note.source_event_id,
                )
    return continuations


def parse_realization_document(
    data: dict[str, Any],
    *,
    score_document: dict[str, Any] | None = None,
    score: ScoreDocument | None = None,
    expected_score_sha256: str | None = None,
) -> RealizationDocument:
    """Parse realization v1 and optionally bind it to score identity/content.

    Passing raw ``score_document`` proves the binding by hashing it internally,
    parsing it, and checking every event, part, voice and bar/beat reference.
    A separately parsed ``score`` may also be supplied, but only alongside its
    raw document; equality with the internal parse is then required.  This
    prevents overlapping IDs in a different revision from being mistaken for
    proof of identity.  ``expected_score_sha256`` alone is only a caller hash
    assertion.  Shape-only tooling may omit all contextual arguments.
    """

    if not isinstance(data, dict):
        raise ValueError("realization must be an object")
    if score is not None and score_document is None:
        raise ValueError(
            "parsed score reference validation requires score_document"
        )
    if score_document is not None and not isinstance(score_document, dict):
        raise ValueError("score_document must be an object")
    # Freeze each caller-owned mapping once before reading any field.  Parsing
    # and hashing a live dict in separate passes can otherwise bind generation
    # B while validating references against generation A.
    try:
        realization_payload = canonical_json_bytes(data)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "realization and score_document must be finite portable JSON"
        ) from exc
    if len(realization_payload) > MAX_REALIZATION_JSON_BYTES:
        raise ValueError(
            "realization canonical JSON exceeds "
            f"{MAX_REALIZATION_JSON_BYTES} bytes"
        )
    try:
        data = json.loads(realization_payload)
        frozen_score_document = (
            json.loads(canonical_json_bytes(score_document))
            if score_document is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "realization and score_document must be finite portable JSON"
        ) from exc
    _reject_unknown_fields(data, _DOCUMENT_FIELDS, "realization")
    required = (
        "kind",
        "schema_version",
        "score_sha256",
        "defaults_profile",
        "mode",
        "note_overrides",
        "control_lanes",
    )
    missing = [field for field in required if field not in data]
    if missing:
        raise ValueError(
            "realization is missing required fields: " + ", ".join(missing)
        )
    if data["kind"] != REALIZATION_KIND:
        raise ValueError(f"realization.kind must be {REALIZATION_KIND!r}")
    try:
        version = _integer(
            data["schema_version"], "realization.schema_version"
        )
    except ValueError:
        version = None
    if version != REALIZATION_SCHEMA_VERSION:
        raise ValueError(
            f"realization.schema_version must be {REALIZATION_SCHEMA_VERSION}"
        )
    score_sha256 = _sha256(data["score_sha256"], "realization.score_sha256")
    defaults_profile = _required_text(
        data["defaults_profile"], "realization.defaults_profile"
    )
    if defaults_profile != DEFAULTS_PROFILE_V1:
        raise ValueError(
            "realization.defaults_profile is unsupported; expected "
            f"{DEFAULTS_PROFILE_V1!r}"
        )
    mode = _required_text(data["mode"], "realization.mode")
    if mode not in REALIZATION_MODES:
        choices = ", ".join(sorted(REALIZATION_MODES))
        raise ValueError(f"realization.mode must be one of {choices}")

    raw_overrides = data["note_overrides"]
    if not isinstance(raw_overrides, list):
        raise ValueError("realization.note_overrides must be an array")
    if len(raw_overrides) > MAX_NOTE_OVERRIDES:
        raise ValueError(
            f"realization.note_overrides exceeds {MAX_NOTE_OVERRIDES} entries"
        )
    note_overrides = tuple(
        _parse_note_override(item, position)
        for position, item in enumerate(raw_overrides)
    )
    seen_event_ids: set[str] = set()
    for position, override in enumerate(note_overrides):
        if override.event_id in seen_event_ids:
            raise ValueError(
                "realization.note_overrides contains duplicate event_id at "
                f"index {position}: {override.event_id!r}"
            )
        seen_event_ids.add(override.event_id)

    raw_lanes = data["control_lanes"]
    if not isinstance(raw_lanes, list):
        raise ValueError("realization.control_lanes must be an array")
    if len(raw_lanes) > MAX_CONTROL_LANES:
        raise ValueError(
            f"realization.control_lanes exceeds {MAX_CONTROL_LANES} entries"
        )
    parsed_lanes: list[ControlLane] = []
    total_points = 0
    for position, item in enumerate(raw_lanes):
        lane = _parse_control_lane(item, position)
        total_points += len(lane.points)
        if total_points > MAX_TOTAL_CONTROL_POINTS:
            raise ValueError(
                "realization control point count exceeds "
                f"{MAX_TOTAL_CONTROL_POINTS}"
            )
        parsed_lanes.append(lane)
    control_lanes = tuple(parsed_lanes)
    seen_lane_ids: set[str] = set()
    seen_lane_targets: set[tuple[str, str | None, str]] = set()
    for position, lane in enumerate(control_lanes):
        if lane.lane_id in seen_lane_ids:
            raise ValueError(
                "realization.control_lanes contains duplicate lane_id at "
                f"index {position}: {lane.lane_id!r}"
            )
        seen_lane_ids.add(lane.lane_id)
        target_key = (lane.target.part_id, lane.target.voice, lane.control)
        if target_key in seen_lane_targets:
            raise ValueError(
                "realization.control_lanes contains competing lanes for "
                f"target/control at index {position}: {target_key!r}"
            )
        seen_lane_targets.add(target_key)

    document = RealizationDocument(
        score_sha256=score_sha256,
        defaults_profile=defaults_profile,
        mode=mode,
        note_overrides=note_overrides,
        control_lanes=control_lanes,
    )
    if expected_score_sha256 is not None:
        expected = _sha256(expected_score_sha256, "expected_score_sha256")
        if not hmac.compare_digest(document.score_sha256, expected):
            raise ValueError(
                "realization.score_sha256 does not match the expected score"
            )
    if frozen_score_document is not None:
        parsed_score = parse_score_document(frozen_score_document)
        actual_hash = canonical_json_sha256(frozen_score_document)
        if not hmac.compare_digest(document.score_sha256, actual_hash):
            raise ValueError(
                "realization.score_sha256 does not match score_document"
            )
        if expected_score_sha256 is not None and not hmac.compare_digest(
            expected_score_sha256,
            actual_hash,
        ):
            raise ValueError(
                "expected_score_sha256 does not match score_document"
            )
        if score is not None:
            if not isinstance(score, ScoreDocument):
                raise TypeError("score must be a parsed ScoreDocument")
            if score != parsed_score:
                raise ValueError(
                    "score does not match the parsed score_document"
                )
        validate_realization_references(document, parsed_score)
    return document


def empty_realization(
    score_sha256: str,
    *,
    mode: str = "interpreted",
) -> RealizationDocument:
    """Return the canonical empty realization-v1 no-op for one score hash."""

    return parse_realization_document(
        {
            "kind": REALIZATION_KIND,
            "schema_version": REALIZATION_SCHEMA_VERSION,
            "score_sha256": score_sha256,
            "defaults_profile": DEFAULTS_PROFILE_V1,
            "mode": mode,
            "note_overrides": [],
            "control_lanes": [],
        },
        expected_score_sha256=score_sha256,
    )


__all__ = (
    "CONTROL_INTERPOLATIONS",
    "CONTROL_NAMES",
    "CONTROL_SEMANTIC_POLICIES",
    "CONTROL_TIME_POLICIES",
    "CONTROL_VALUE_POLICIES",
    "DEFAULTS_PROFILE_V1",
    "MERGE_STRATEGIES",
    "REALIZATION_KIND",
    "REALIZATION_MODES",
    "REALIZATION_SCHEMA_VERSION",
    "ControlLane",
    "ControlPoint",
    "ControlTarget",
    "NoteRealizationOverride",
    "NumericOverride",
    "RealizationDocument",
    "empty_realization",
    "parse_realization_document",
    "realization_control_point_seconds",
    "realization_note_time_bounds",
    "validate_realization_references",
)
