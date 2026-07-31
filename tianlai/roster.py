"""Roster: binds instrument-neutral score parts to actual players.

This is where a score stops being abstract.  A roster answers four questions
the score deliberately refuses to answer:

* which instrument plays this part,
* how a notated marking such as ``pizzicato`` becomes that instrument's own
  articulation name,
* where the player sits, which decides both panning and (later) the distance
  and angle the spatial layer applies,
* and, for a percussion staff, **which of several instruments** each notehead
  routes to.

That last one is not a corner case.  A drum kit is one staff in the score but
ten separate instruments in this project (底鼓, 边击军鼓, 踩镲…), so one part
legitimately expands into many executors.  Anything downstream therefore
counts executors, never parts.

No mapping is ever invented.  If a score asks for an articulation the chosen
instrument does not have, resolution raises instead of quietly substituting a
plain note — the same refusal as ``explicit_only_no_silent_gm`` one layer up.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from numbers import Real
from typing import Any
import unicodedata

from .capability import InstrumentCapability, resolve_capability
from .score import ScoreDocument, parse_pitch, pitch_name


_ID_FORBIDDEN = set('/\\:*?"<>|')
_WINDOWS_RESERVED_BASENAMES = frozenset(
    ("con", "prn", "aux", "nul")
    + tuple(f"com{number}" for number in range(1, 10))
    + tuple(f"lpt{number}" for number in range(1, 10))
)
_ROSTER_KEYS = frozenset(
    ("name", "drop_parts", "assignments", "collaboration")
)
_ASSIGNMENT_KEYS = frozenset(
    (
        "part",
        "instrument",
        "kit",
        "executor_id",
        "gain_db",
        "gain_automation",
        "pan",
        "transpose",
        "dynamic_compression",
        "duration_scale",
        "articulation_auto",
        "seat",
        "role",
        "articulation_map",
        "overrides",
        # 作品编制中使用的纯文本人工注释；不进入执行计划。
        "_note",
    )
)
_SEAT_KEYS = frozenset(("azimuth_deg", "distance_m"))
_KIT_ENTRY_KEYS = frozenset(("instrument", "transpose"))
_GAIN_AUTOMATION_KEYS = frozenset(("bar", "beat", "offset_db"))
_ROLE_KEYS = frozenset(("function", "prominence", "label"))
_ROLE_FUNCTIONS = frozenset(
    (
        "lead",
        "countermelody",
        "harmony",
        "pad",
        "bass",
        "rhythm",
        "accent",
        "texture",
        "ambience",
        "effect",
        "other",
    )
)
_ROLE_PROMINENCES = frozenset(("foreground", "midground", "background"))
_COLLABORATION_KEYS = frozenset(
    ("mode", "analysis", "part_groups", "balance_relations")
)
_COLLABORATION_MODES = frozenset(("manual", "analyze", "suggest"))
_COLLABORATION_ANALYSIS_KEYS = frozenset(
    ("metric", "window_ms", "hop_ms", "gate_dbfs")
)
_COLLABORATION_METRIC = "overlap_active_rms"
_BALANCE_RELATION_KEYS = frozenset(
    (
        "subject",
        "reference",
        "target_offset_db",
        "tolerance_db",
        "max_suggestion_db",
    )
)
_PART_GROUP_KEYS = frozenset(("id", "parts"))
_DEFAULT_ANALYSIS_WINDOW_MS = 400.0
_DEFAULT_ANALYSIS_HOP_MS = 100.0
_DEFAULT_ANALYSIS_GATE_DBFS = -60.0
_MIN_ANALYSIS_WINDOW_MS = 20.0
_MAX_ANALYSIS_WINDOW_MS = 2_000.0
_MIN_ANALYSIS_HOP_MS = 10.0
MAX_BALANCE_RELATIONS = 256


def _reject_unknown_keys(
    mapping: dict[Any, Any], allowed: frozenset[str], path: str
) -> None:
    """Reject misspelled document fields with their complete JSON path."""

    unknown = sorted(
        (str(key) for key in mapping if key not in allowed),
        key=str,
    )
    if unknown:
        locations = ", ".join(f"{path}.{key}" for key in unknown)
        raise ValueError(f"{path} 包含未知字段: {locations}")


def _finite_number(value: object, path: str) -> float:
    """Return one JSON number while rejecting booleans and NaN/Infinity."""

    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{path} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{path} must be a finite number")
    return number


@dataclass(frozen=True, slots=True)
class Seat:
    """Where a player sits, seen from the listener at the origin."""

    azimuth_deg: float
    distance_m: float

    @property
    def pan(self) -> float:
        """Stereo position in ``[-1, 1]``; ±45° maps to a hard side."""

        return max(-1.0, min(1.0, self.azimuth_deg / 45.0))

    def to_dict(self) -> dict[str, Any]:
        return {"azimuth_deg": self.azimuth_deg, "distance_m": self.distance_m}


@dataclass(frozen=True, slots=True)
class GainAutomationPoint:
    """One musical-time point on a part's mix-gain envelope.

    The value is an offset from ``gain_db`` rather than a replacement for it:
    the static gain remains the part's baseline balance, while this envelope
    records the conductor/engineer's ride through the piece.  Interpolation is
    linear in dB after the conductor converts bar/beat positions to seconds.
    """

    bar: int
    beat: float
    offset_db: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar": self.bar,
            "beat": self.beat,
            "offset_db": self.offset_db,
        }


@dataclass(frozen=True, slots=True)
class Role:
    """One assignment's declared musical function and foreground depth.

    Roles are orchestration intent, not an implicit mixing preset.  The
    collaboration analyzer may use them as context, but merely declaring a
    role must never change gain, timing, articulation or spatial processing.
    """

    function: str
    prominence: str
    label: str | None = None

    def to_dict(self) -> dict[str, str]:
        data = {
            "function": self.function,
            "prominence": self.prominence,
        }
        if self.label is not None:
            data["label"] = self.label
        return data


@dataclass(frozen=True, slots=True)
class CollaborationAnalysis:
    """Deterministic analysis-window settings; no audio mutation is implied."""

    metric: str = _COLLABORATION_METRIC
    window_ms: float = _DEFAULT_ANALYSIS_WINDOW_MS
    hop_ms: float = _DEFAULT_ANALYSIS_HOP_MS
    gate_dbfs: float = _DEFAULT_ANALYSIS_GATE_DBFS

    def to_dict(self) -> dict[str, str | float]:
        return {
            "metric": self.metric,
            "window_ms": self.window_ms,
            "hop_ms": self.hop_ms,
            "gate_dbfs": self.gate_dbfs,
        }


@dataclass(frozen=True, slots=True)
class BalanceRelation:
    """A declared relative-level intent between two collaboration endpoints."""

    subject: str
    reference: str
    target_offset_db: float
    tolerance_db: float
    max_suggestion_db: float

    def to_dict(self) -> dict[str, str | float]:
        return {
            "subject": self.subject,
            "reference": self.reference,
            "target_offset_db": self.target_offset_db,
            "tolerance_db": self.tolerance_db,
            "max_suggestion_db": self.max_suggestion_db,
        }


@dataclass(frozen=True, slots=True)
class PartGroup:
    """A creator-declared analysis endpoint made from assigned score parts.

    This is not a render bus and owns no gain, pan or effects.  Its members are
    summed only while the read-only collaboration report measures a declared
    relation.
    """

    id: str
    parts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parts": list(self.parts),
        }


@dataclass(frozen=True, slots=True)
class CollaborationSettings:
    """Opt-in ensemble analysis contract.

    ``manual`` is the compatibility default.  ``analyze`` and ``suggest``
    authorize diagnostics and bounded recommendations respectively; neither
    mode authorizes the renderer to change audio by itself.
    """

    mode: str = "manual"
    analysis: CollaborationAnalysis = field(
        default_factory=CollaborationAnalysis
    )
    balance_relations: tuple[BalanceRelation, ...] = ()
    # Absence and an explicit {"mode": "manual"} have the same effective
    # behavior, but only the latter may be serialized.  This preserves old
    # roster/plan documents byte-for-structure when no v0.5 field was written.
    declared: bool = field(default=False, compare=False, repr=False)
    part_groups: tuple[PartGroup, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "mode": self.mode,
            "analysis": self.analysis.to_dict(),
            "balance_relations": [
                relation.to_dict() for relation in self.balance_relations
            ],
        }
        # Keep already-authored collaboration documents structurally stable
        # unless the creator has actually declared a group.
        if self.part_groups:
            data["part_groups"] = [
                group.to_dict() for group in self.part_groups
            ]
        return data


@dataclass(frozen=True, slots=True)
class Executor:
    """One instrument that will render exactly one stem."""

    executor_id: str
    part_id: str
    capability: InstrumentCapability
    gain_db: float
    pan: float
    seat: Seat
    transpose: int
    articulation_map: tuple[tuple[str, str], ...]
    kit_pitch: float | None
    role: Role | None = None
    duration_scale: float = 1.0
    dynamic_compression: float = 0.0
    articulation_auto: bool = True
    gain_automation: tuple[GainAutomationPoint, ...] = ()
    overrides: tuple[tuple[str, Any], ...] = ()

    @property
    def override_map(self) -> dict[str, Any]:
        return dict(self.overrides)

    def mapped_articulation(self, marking: str | None) -> tuple[str | None, str]:
        """Translate a score marking into this instrument's own name.

        Returns the resolved name plus a short reason string that ends up in
        the performance plan, so an auditor can see whether a name came from
        the roster dictionary, straight through, or from the default.
        """

        capability = self.capability
        if marking is None:
            return capability.default_articulation, "默认奏法"
        for source, target in self.articulation_map:
            if source == marking:
                if not capability.supports(target):
                    raise ValueError(
                        f"编制表把 {self.part_id!r} 的 {marking!r} 映射到 "
                        f"{target!r},但 {capability.name} 没有这个奏法;"
                        f"可选: {', '.join(capability.articulations) or '(无)'}"
                    )
                return target, f"编制表映射 {marking}→{target}"
        if capability.supports(marking):
            return marking, f"直通 {marking}"
        raise ValueError(
            f"声部 {self.part_id!r} 要求奏法 {marking!r},"
            f"但 {capability.name} 不支持,且编制表未给出映射;"
            f"该乐器可选: {', '.join(capability.articulations) or '(无)'}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor_id": self.executor_id,
            "part_id": self.part_id,
            "instrument": self.capability.relative_path,
            "instrument_name": self.capability.name,
            "gain_db": self.gain_db,
            "pan": self.pan,
            "seat": self.seat.to_dict(),
            "transpose": self.transpose,
            "duration_scale": self.duration_scale,
            "dynamic_compression": self.dynamic_compression,
            "articulation_auto": self.articulation_auto,
            "articulation_map": dict(self.articulation_map),
            **({"role": self.role.to_dict()} if self.role is not None else {}),
            **(
                {
                    "gain_automation": [
                        point.to_dict() for point in self.gain_automation
                    ]
                }
                if self.gain_automation
                else {}
            ),
            "kit_pitch": (
                None if self.kit_pitch is None else pitch_name(self.kit_pitch)
            ),
            **({"overrides": dict(self.overrides)} if self.overrides else {}),
        }


@dataclass(frozen=True, slots=True)
class Roster:
    name: str
    executors: tuple[Executor, ...]
    dropped_parts: tuple[str, ...] = ()
    collaboration: CollaborationSettings = field(
        default_factory=CollaborationSettings
    )

    def executors_for(self, part_id: str) -> tuple[Executor, ...]:
        return tuple(item for item in self.executors if item.part_id == part_id)

    def route(self, part_id: str, midi: float) -> Executor:
        """Pick the executor that plays this notehead within its part."""

        candidates = self.executors_for(part_id)
        if not candidates:
            raise ValueError(f"编制表没有为声部 {part_id!r} 指派乐器")
        if len(candidates) == 1 and candidates[0].kit_pitch is None:
            return candidates[0]
        for candidate in candidates:
            if candidate.kit_pitch is not None and math.isclose(
                candidate.kit_pitch, midi, abs_tol=1e-6
            ):
                return candidate
        mapped = ", ".join(
            pitch_name(item.kit_pitch)
            for item in candidates
            if item.kit_pitch is not None
        )
        raise ValueError(
            f"声部 {part_id!r} 的音 {pitch_name(midi)} 在套件映射里没有对应乐器;"
            f"已映射的位置: {mapped or '(无)'}"
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "executors": [executor.to_dict() for executor in self.executors],
        }
        if self.dropped_parts:
            data["dropped_parts"] = list(self.dropped_parts)
        if self.collaboration.declared:
            data["collaboration"] = self.collaboration.to_dict()
        return data


def _portable_filename_key(value: str) -> str:
    """Return the cross-platform comparison key for one output basename.

    Windows normally compares filenames case-insensitively, while macOS and
    archives may expose canonically equivalent Unicode spellings differently.
    Keep the author's display spelling, but reserve one NFC + casefold key so
    two executors can never target the same portable output filename.
    """

    return unicodedata.normalize("NFC", value).casefold()


def _is_windows_reserved_filename(value: str) -> bool:
    """Whether ``value`` starts with a reserved DOS device basename.

    Windows reserves these names even when an extension follows (``CON.wav``).
    Stripping spaces immediately before the first dot also closes spellings
    that Windows path normalization may collapse.
    """

    basename = _portable_filename_key(value).partition(".")[0].rstrip(" ")
    return basename in _WINDOWS_RESERVED_BASENAMES


def _check_id(value: str, label: str) -> str:
    if value.endswith((" ", ".")):
        ending = "空格" if value.endswith(" ") else "句点"
        raise ValueError(
            f"{label} {value!r} 不能以{ending}结尾；Windows 会折叠该文件名"
        )
    text = value.strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if any(
        character in _ID_FORBIDDEN or ord(character) < 32
        for character in value
    ):
        raise ValueError(
            f"{label} {value!r} 含有不能用作文件名的字符;分轨文件以它命名"
        )
    if _is_windows_reserved_filename(text):
        raise ValueError(
            f"{label} {value!r} 使用了 Windows 保留设备名"
            "（CON/PRN/AUX/NUL/COM1..9/LPT1..9，带扩展名也不允许）"
        )
    return text


def _register_executor_id(
    executor_id: str,
    seen_ids: dict[str, str],
) -> None:
    key = _portable_filename_key(executor_id)
    previous = seen_ids.get(key)
    if previous is not None:
        raise ValueError(
            f"executor_id {executor_id!r} 与已有 {previous!r} 冲突；"
            "分轨文件名按 Unicode NFC 规范化并忽略大小写后必须唯一"
        )
    seen_ids[key] = executor_id


def _parse_seat(raw: object, label: str, path: str) -> Seat:
    if raw is None:
        return Seat(azimuth_deg=0.0, distance_m=3.0)
    if not isinstance(raw, dict):
        raise ValueError(f"{label} seat must be an object")
    _reject_unknown_keys(raw, _SEAT_KEYS, path)
    azimuth = float(raw.get("azimuth_deg", 0.0))
    if not -90.0 <= azimuth <= 90.0:
        raise ValueError(f"{label} seat azimuth_deg must be between -90 and 90")
    distance = float(raw.get("distance_m", 3.0))
    if not 0.1 <= distance <= 60.0:
        raise ValueError(f"{label} seat distance_m must be between 0.1 and 60")
    return Seat(azimuth_deg=azimuth, distance_m=distance)


def _parse_articulation_map(raw: object, label: str) -> tuple[tuple[str, str], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{label} articulation_map must be an object")
    return tuple(sorted((str(key), str(value)) for key, value in raw.items()))


def _parse_gain_automation(
    raw: object, label: str, path: str
) -> tuple[GainAutomationPoint, ...]:
    """Validate a deterministic, piecewise-linear mix ride.

    Requiring the first point at bar 1 beat 1 removes an otherwise ambiguous
    question: should a later first point jump there, or should its value have
    applied since the beginning?  Authors can still hold the baseline for any
    length by writing an initial zero point and another zero point at the start
    of the intended ramp.
    """

    if raw is None:
        return ()
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{label} gain_automation must be a non-empty array")
    points: list[GainAutomationPoint] = []
    previous: tuple[int, float] | None = None
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(
                f"{label} gain_automation[{position}] must be an object"
            )
        _reject_unknown_keys(
            item,
            _GAIN_AUTOMATION_KEYS,
            f"{path}[{position}]",
        )
        bar = int(item.get("bar", 0))
        beat = float(item.get("beat", 1.0))
        offset_db = float(item.get("offset_db", math.nan))
        if bar < 1:
            raise ValueError(
                f"{label} gain_automation[{position}].bar must be at least 1"
            )
        if not math.isfinite(beat) or beat < 1.0:
            raise ValueError(
                f"{label} gain_automation[{position}].beat must be finite and at least 1"
            )
        if not math.isfinite(offset_db) or not -24.0 <= offset_db <= 24.0:
            raise ValueError(
                f"{label} gain_automation[{position}].offset_db "
                "must be between -24 and 24"
            )
        location = (bar, beat)
        if previous is not None and location <= previous:
            raise ValueError(
                f"{label} gain_automation points must be ordered without duplicates"
            )
        points.append(
            GainAutomationPoint(bar=bar, beat=beat, offset_db=offset_db)
        )
        previous = location
    if (points[0].bar, points[0].beat) != (1, 1.0):
        raise ValueError(
            f"{label} gain_automation must start at bar 1 beat 1"
        )
    return tuple(points)


def _parse_role(raw: object, label: str, path: str) -> Role:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_keys(raw, _ROLE_KEYS, path)
    for key in ("function", "prominence"):
        if key not in raw:
            raise ValueError(f"{path}.{key} is required")

    function = raw["function"]
    if not isinstance(function, str) or function not in _ROLE_FUNCTIONS:
        supported = ", ".join(sorted(_ROLE_FUNCTIONS))
        raise ValueError(
            f"{label} role.function must be one of: {supported}"
        )
    prominence = raw["prominence"]
    if not isinstance(prominence, str) or prominence not in _ROLE_PROMINENCES:
        supported = ", ".join(sorted(_ROLE_PROMINENCES))
        raise ValueError(
            f"{label} role.prominence must be one of: {supported}"
        )

    role_label: str | None = None
    if "label" in raw:
        value = raw["label"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}.label must be a non-empty string")
        role_label = value.strip()
    return Role(
        function=function,
        prominence=prominence,
        label=role_label,
    )


def _parse_collaboration_analysis(
    raw: object, path: str
) -> CollaborationAnalysis:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_keys(raw, _COLLABORATION_ANALYSIS_KEYS, path)

    metric = raw.get("metric", _COLLABORATION_METRIC)
    if metric != _COLLABORATION_METRIC:
        raise ValueError(
            f"{path}.metric must be {_COLLABORATION_METRIC!r}"
        )
    window_ms = _finite_number(
        raw.get("window_ms", _DEFAULT_ANALYSIS_WINDOW_MS),
        f"{path}.window_ms",
    )
    hop_ms = _finite_number(
        raw.get("hop_ms", _DEFAULT_ANALYSIS_HOP_MS),
        f"{path}.hop_ms",
    )
    gate_dbfs = _finite_number(
        raw.get("gate_dbfs", _DEFAULT_ANALYSIS_GATE_DBFS),
        f"{path}.gate_dbfs",
    )
    if not _MIN_ANALYSIS_WINDOW_MS <= window_ms <= _MAX_ANALYSIS_WINDOW_MS:
        raise ValueError(
            f"{path}.window_ms must be between "
            f"{_MIN_ANALYSIS_WINDOW_MS:g} and {_MAX_ANALYSIS_WINDOW_MS:g}"
        )
    if hop_ms < _MIN_ANALYSIS_HOP_MS:
        raise ValueError(
            f"{path}.hop_ms must be at least {_MIN_ANALYSIS_HOP_MS:g}"
        )
    if hop_ms > window_ms:
        raise ValueError(f"{path}.hop_ms must not exceed window_ms")
    if not -300.0 <= gate_dbfs <= 0.0:
        raise ValueError(
            f"{path}.gate_dbfs must be between -300 and 0 dBFS"
        )
    return CollaborationAnalysis(
        metric=_COLLABORATION_METRIC,
        window_ms=window_ms,
        hop_ms=hop_ms,
        gate_dbfs=gate_dbfs,
    )


def _parse_part_groups(
    raw: object,
    assigned_parts: frozenset[str],
    path: str,
) -> tuple[PartGroup, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{path} must be an array")

    groups: list[PartGroup] = []
    seen_ids: set[str] = set()
    for position, item in enumerate(raw):
        item_path = f"{path}[{position}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_path} must be an object")
        _reject_unknown_keys(item, _PART_GROUP_KEYS, item_path)
        for key in ("id", "parts"):
            if key not in item:
                raise ValueError(f"{item_path}.{key} is required")

        raw_id = item["id"]
        if not isinstance(raw_id, str):
            raise ValueError(f"{item_path}.id must be a non-empty string")
        group_id = _check_id(raw_id, f"{item_path}.id")
        if group_id in assigned_parts:
            raise ValueError(
                f"{item_path}.id {group_id!r} conflicts with an assigned part"
            )
        if group_id in seen_ids:
            raise ValueError(f"{path} contains duplicate id {group_id!r}")

        raw_parts = item["parts"]
        if not isinstance(raw_parts, list) or not raw_parts:
            raise ValueError(f"{item_path}.parts must be a non-empty array")
        members: list[str] = []
        seen_members: set[str] = set()
        for member_position, raw_member in enumerate(raw_parts):
            member_path = f"{item_path}.parts[{member_position}]"
            if not isinstance(raw_member, str) or not raw_member.strip():
                raise ValueError(
                    f"{member_path} must be a non-empty assigned part id"
                )
            member = raw_member.strip()
            # Checking exclusively against assignments also forbids nesting:
            # another group id can never stand in for a concrete score part.
            if member not in assigned_parts:
                raise ValueError(
                    f"{member_path} refers to unassigned part {member!r}; "
                    "part groups cannot be nested"
                )
            if member in seen_members:
                raise ValueError(
                    f"{item_path}.parts contains duplicate part {member!r}"
                )
            seen_members.add(member)
            members.append(member)

        seen_ids.add(group_id)
        groups.append(PartGroup(id=group_id, parts=tuple(members)))
    return tuple(groups)


def _required_relation_endpoint(
    raw: dict[Any, Any],
    key: str,
    path: str,
    available_endpoints: frozenset[str],
) -> str:
    if key not in raw:
        raise ValueError(f"{path}.{key} is required")
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{path}.{key} must be a non-empty part or part-group id"
        )
    endpoint_id = value.strip()
    if endpoint_id not in available_endpoints:
        raise ValueError(
            f"{path}.{key} refers to unassigned part or undeclared "
            f"part group {endpoint_id!r}"
        )
    return endpoint_id


def _parse_balance_relation(
    raw: object,
    path: str,
    endpoint_parts: dict[str, tuple[str, ...]],
) -> BalanceRelation:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_keys(raw, _BALANCE_RELATION_KEYS, path)
    for key in (
        "target_offset_db",
        "tolerance_db",
        "max_suggestion_db",
    ):
        if key not in raw:
            raise ValueError(f"{path}.{key} is required")

    subject = _required_relation_endpoint(
        raw, "subject", path, frozenset(endpoint_parts)
    )
    reference = _required_relation_endpoint(
        raw, "reference", path, frozenset(endpoint_parts)
    )
    shared_parts = sorted(
        set(endpoint_parts[subject]) & set(endpoint_parts[reference])
    )
    if subject == reference or shared_parts:
        raise ValueError(
            f"{path} subject and reference must name different parts or "
            "part-group endpoints whose expanded parts are disjoint"
            + (
                f"; shared parts: {', '.join(shared_parts)}"
                if shared_parts
                else ""
            )
        )
    target_offset_db = _finite_number(
        raw["target_offset_db"], f"{path}.target_offset_db"
    )
    tolerance_db = _finite_number(
        raw["tolerance_db"], f"{path}.tolerance_db"
    )
    max_suggestion_db = _finite_number(
        raw["max_suggestion_db"], f"{path}.max_suggestion_db"
    )
    if tolerance_db < 0.0:
        raise ValueError(f"{path}.tolerance_db must not be negative")
    if max_suggestion_db < 0.0:
        raise ValueError(
            f"{path}.max_suggestion_db must not be negative"
        )
    return BalanceRelation(
        subject=subject,
        reference=reference,
        target_offset_db=target_offset_db,
        tolerance_db=tolerance_db,
        max_suggestion_db=max_suggestion_db,
    )


def _parse_collaboration(
    raw: object,
    assigned_parts: frozenset[str],
    path: str = "roster.collaboration",
) -> CollaborationSettings:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")
    _reject_unknown_keys(raw, _COLLABORATION_KEYS, path)

    mode = raw.get("mode", "manual")
    if not isinstance(mode, str) or mode not in _COLLABORATION_MODES:
        supported = ", ".join(sorted(_COLLABORATION_MODES))
        raise ValueError(f"{path}.mode must be one of: {supported}")
    analysis = (
        _parse_collaboration_analysis(raw["analysis"], f"{path}.analysis")
        if "analysis" in raw
        else CollaborationAnalysis()
    )
    part_groups = _parse_part_groups(
        raw.get("part_groups", []),
        assigned_parts,
        f"{path}.part_groups",
    )
    endpoint_parts = {
        part_id: (part_id,) for part_id in assigned_parts
    }
    endpoint_parts.update(
        {group.id: group.parts for group in part_groups}
    )

    raw_relations = raw.get("balance_relations", [])
    if not isinstance(raw_relations, list):
        raise ValueError(f"{path}.balance_relations must be an array")
    if len(raw_relations) > MAX_BALANCE_RELATIONS:
        raise ValueError(
            f"{path}.balance_relations 最多允许 "
            f"{MAX_BALANCE_RELATIONS} 条，避免单次诊断工作量失控"
        )
    relations: list[BalanceRelation] = []
    seen_pairs: set[tuple[str, str]] = set()
    for position, item in enumerate(raw_relations):
        relation = _parse_balance_relation(
            item,
            f"{path}.balance_relations[{position}]",
            endpoint_parts,
        )
        pair = (relation.subject, relation.reference)
        if pair in seen_pairs:
            raise ValueError(
                f"{path}.balance_relations contains duplicate relation "
                f"{relation.subject!r} -> {relation.reference!r}"
            )
        seen_pairs.add(pair)
        relations.append(relation)
    return CollaborationSettings(
        mode=mode,
        analysis=analysis,
        balance_relations=tuple(relations),
        declared=True,
        part_groups=part_groups,
    )


# 编制表不是第二份乐器清单。这里采用显式白名单：每增加一种可调参数都要
# 经过一次代码审阅，而不是仅凭“它碰巧是标量”就放行。否则 type、
# implementation、asset_root、license_status 等身份/资源/许可字段同样都是
# 字符串，浅合并后足以把已经通过能力与许可检查的乐器换成另一条执行路径。
_OVERRIDE_SCALARS = (int, float, str, bool)
_OVERRIDE_ALLOWED_FIELDS = frozenset(
    (
        # 密集写作可缩短采样/模型的释音尾，减少声部间堆叠。
        "release_seconds",
        # 大提琴的独立离弦 one-shot 不受主释音控制；作品可显式缩放或关闭。
        "release_tail_gain",
        # 小提琴明确提供 SOLO / SEC 两套同身份、同许可的采样变体。
        "sample_variant",
    )
)


def _parse_overrides(raw: object, label: str) -> tuple[tuple[str, Any], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"{label} overrides must be an object")
    items: list[tuple[str, Any]] = []
    for key, value in raw.items():
        if key not in _OVERRIDE_ALLOWED_FIELDS:
            allowed = ", ".join(sorted(_OVERRIDE_ALLOWED_FIELDS))
            raise ValueError(
                f"{label} overrides[{key!r}] 不在允许清单中；"
                f"可选: {allowed}。乐器身份、资源、许可与能力等结构性字段禁止覆盖"
            )
        if not isinstance(value, _OVERRIDE_SCALARS):
            raise ValueError(
                f"{label} overrides[{key!r}] 只能是标量(数值/字符串/布尔),"
                "不能替换乐器的结构性字段"
            )
        if key == "release_tail_gain":
            value = _finite_number(
                value,
                f"{label}.overrides.release_tail_gain",
            )
            if not 0.0 <= value <= 1.0:
                raise ValueError(
                    f"{label}.overrides.release_tail_gain must be between 0 and 1"
                )
        items.append((str(key), value))
    return tuple(sorted(items))


def parse_roster_document(
    data: dict[str, Any], capabilities: dict[str, InstrumentCapability]
) -> Roster:
    """Validate a roster and resolve every reference to a real instrument."""

    if not isinstance(data, dict):
        raise ValueError("roster must be an object")
    _reject_unknown_keys(data, _ROSTER_KEYS, "roster")
    raw_assignments = data.get("assignments")
    if not isinstance(raw_assignments, list) or not raw_assignments:
        raise ValueError("assignments must be a non-empty array")

    executors: list[Executor] = []
    seen_parts: set[str] = set()
    seen_ids: dict[str, str] = {}
    for position, raw in enumerate(raw_assignments):
        if not isinstance(raw, dict):
            raise ValueError(f"assignments[{position}] must be an object")
        assignment_path = f"roster.assignments[{position}]"
        _reject_unknown_keys(raw, _ASSIGNMENT_KEYS, assignment_path)
        if "_note" in raw and not isinstance(raw["_note"], str):
            raise ValueError(f"{assignment_path}._note must be a string")
        part_id = _check_id(str(raw.get("part", "")), f"assignments[{position}].part")
        if part_id in seen_parts:
            raise ValueError(f"声部 {part_id!r} 被指派了两次")
        seen_parts.add(part_id)
        label = f"声部 {part_id!r}"
        role = (
            _parse_role(raw["role"], label, f"{assignment_path}.role")
            if "role" in raw
            else None
        )
        gain_db = float(raw.get("gain_db", 0.0))
        if not -60.0 <= gain_db <= 12.0:
            raise ValueError(f"{label} gain_db must be between -60 and 12")
        seat = _parse_seat(raw.get("seat"), label, f"{assignment_path}.seat")
        pan = float(raw["pan"]) if "pan" in raw else seat.pan
        if not -1.0 <= pan <= 1.0:
            raise ValueError(f"{label} pan must be between -1 and 1")
        transpose = int(raw.get("transpose", 0))
        # 密集写作里音符互相拖尾是"糊"的主要来源:释音包络是指数型,声明
        # 0.8 秒实际能拖到 2 秒。按声部缩短发声时长比换音源见效快得多,而且
        # 0.85 上下还听不成断奏。
        duration_scale = float(raw.get("duration_scale", 1.0))
        if not 0.1 <= duration_scale <= 2.0:
            raise ValueError(f"{label} duration_scale must be between 0.1 and 2.0")
        # 同一个力度值在不同乐器上响度差别很大:实测钢琴从 v=0.2 到 v=0.9
        # 跨越 32 dB,而弦乐只有约 10 dB。于是弱奏段落钢琴会被弦乐盖住,强奏
        # 段落才冒出来——同一份编制在乐曲不同段落的平衡是漂移的。压缩把该
        # 声部的力度往上端收拢,抬起弱奏而几乎不动强奏。
        dynamic_compression = float(raw.get("dynamic_compression", 0.0))
        if not 0.0 <= dynamic_compression <= 1.0:
            raise ValueError(
                f"{label} dynamic_compression must be between 0 and 1"
            )
        # 指挥层会按时值给短音挑更快的起音层。省略时继承乐器清单的默认
        # 策略；编制表仍可显式覆盖，并把最终布尔值写进 Executor 供计划审计。
        articulation_auto_override: bool | None = None
        if "articulation_auto" in raw:
            if not isinstance(raw["articulation_auto"], bool):
                raise ValueError(f"{label} articulation_auto must be boolean")
            articulation_auto_override = raw["articulation_auto"]
        articulation_map = _parse_articulation_map(raw.get("articulation_map"), label)
        # 声部平衡必须与演奏力度分离:力度会同时改变响度与音色,不能拿它替代
        # 指挥/混音师随乐段推拉推子的动作。自动化点按音乐位置写入编制表,
        # 指挥层再通过速度表编译成秒,渲染层仅执行确定性的 dB 包络。
        gain_automation = _parse_gain_automation(
            raw.get("gain_automation"),
            label,
            f"{assignment_path}.gain_automation",
        )
        # 逐执行器的受控乐器参数:让某一次使用微调 release_seconds 或选择
        # 已审定的 sample_variant，而不 fork 乐器本身。解析器使用显式白名单，
        # 不允许编制表覆盖执行后端、资源路径、许可或能力身份。
        overrides = _parse_overrides(raw.get("overrides"), label)

        has_instrument = "instrument" in raw
        has_kit = "kit" in raw
        if has_instrument == has_kit:
            raise ValueError(f"{label} 必须且只能声明 instrument 或 kit 其中之一")

        if has_instrument:
            capability = resolve_capability(capabilities, str(raw["instrument"]))
            executor_id = _check_id(
                str(raw.get("executor_id", part_id)), f"{label} executor_id"
            )
            _register_executor_id(executor_id, seen_ids)
            executors.append(
                Executor(
                    executor_id=executor_id,
                    part_id=part_id,
                    capability=capability,
                    gain_db=gain_db,
                    pan=pan,
                    seat=seat,
                    transpose=transpose,
                    articulation_map=articulation_map,
                    kit_pitch=None,
                    role=role,
                    duration_scale=duration_scale,
                    dynamic_compression=dynamic_compression,
                    articulation_auto=(
                        capability.articulation_auto_default
                        if articulation_auto_override is None
                        else articulation_auto_override
                    ),
                    gain_automation=gain_automation,
                    overrides=overrides,
                )
            )
            continue

        kit = raw["kit"]
        if not isinstance(kit, dict) or not kit:
            raise ValueError(f"{label} kit must be a non-empty object")
        for notehead, reference in sorted(kit.items()):
            midi = parse_pitch(notehead)
            # kit 条目可以是乐器路径字符串,也可以是 {instrument, transpose}:
            # 打击件多按特定键位映射(踩镲 42~44、镲 63~66…),谱面鼓音未必落在
            # 那里,transpose 把它移进该件的键位范围,否则会因超出音域而无声。
            entry_transpose = transpose
            if isinstance(reference, dict):
                _reject_unknown_keys(
                    reference,
                    _KIT_ENTRY_KEYS,
                    f"{assignment_path}.kit[{notehead!r}]",
                )
                instrument_ref = str(reference.get("instrument", ""))
                entry_transpose = int(reference.get("transpose", transpose))
            else:
                instrument_ref = str(reference)
            capability = resolve_capability(capabilities, instrument_ref)
            executor_id = _check_id(
                f"{part_id}.{pitch_name(midi)}",
                f"{label} 套件展开后的 executor_id",
            )
            _register_executor_id(executor_id, seen_ids)
            executors.append(
                Executor(
                    executor_id=executor_id,
                    part_id=part_id,
                    capability=capability,
                    gain_db=gain_db,
                    pan=pan,
                    seat=seat,
                    transpose=entry_transpose,
                    articulation_map=articulation_map,
                    kit_pitch=midi,
                    role=role,
                    duration_scale=duration_scale,
                    dynamic_compression=dynamic_compression,
                    articulation_auto=(
                        capability.articulation_auto_default
                        if articulation_auto_override is None
                        else articulation_auto_override
                    ),
                    gain_automation=gain_automation,
                    overrides=overrides,
                )
            )

    raw_dropped = data.get("drop_parts", [])
    if not isinstance(raw_dropped, list):
        raise ValueError("drop_parts must be an array of part ids")
    dropped: list[str] = []
    for item in raw_dropped:
        text = str(item).strip()
        if not text:
            raise ValueError("drop_parts entries must be non-empty part ids")
        if text in seen_parts:
            raise ValueError(f"声部 {text!r} 既被指派又被列入 drop_parts,自相矛盾")
        dropped.append(text)

    collaboration = (
        _parse_collaboration(
            data["collaboration"],
            frozenset(seen_parts),
        )
        if "collaboration" in data
        else CollaborationSettings()
    )

    return Roster(
        name=str(data.get("name", "未命名编制")),
        executors=tuple(executors),
        dropped_parts=tuple(dropped),
        collaboration=collaboration,
    )


def check_roster_covers_score(roster: Roster, score: ScoreDocument) -> None:
    """Refuse to proceed when the roster and score disagree about parts.

    Dropping a part is allowed, but only when it is named explicitly in
    ``drop_parts`` — the same refusal-to-guess as everywhere else.  Silently
    ignoring an unassigned staff would let a whole line vanish without a trace.
    """

    score_parts = {part.id for part in score.parts}
    roster_parts = {executor.part_id for executor in roster.executors}
    dropped = set(roster.dropped_parts)

    phantom_drops = sorted(dropped - score_parts)
    if phantom_drops:
        raise ValueError(
            f"drop_parts 列出了总谱里不存在的声部: {', '.join(phantom_drops)}"
        )
    unplayed = sorted(score_parts - roster_parts - dropped)
    if unplayed:
        raise ValueError(
            f"总谱有声部没人演奏: {', '.join(unplayed)};"
            "请在编制表里指派乐器,或用 drop_parts 明确声明有意删除"
        )
    extra = sorted(roster_parts - score_parts)
    if extra:
        raise ValueError(f"编制表指派了总谱里不存在的声部: {', '.join(extra)}")
