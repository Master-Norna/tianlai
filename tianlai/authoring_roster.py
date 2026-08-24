"""Editable roster contract used before a project is ready to render.

The formal :mod:`tianlai.roster` contract intentionally rejects incomplete
orchestration.  That is the correct execution boundary, but it is too strict
for an authoring document: a creator must be able to save a score before every
part has been assigned an instrument.

This module therefore adds exactly one authoring-only route, ``instrument:
null``.  Every other assignment field is a strictly validated subset of the
formal roster.  A complete authoring roster is converted back to the existing
formal document and cross-checked by the existing parser; the renderer never
needs to understand this authoring format.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import json
import math
from numbers import Real
from typing import Any, Mapping, TypeAlias

from .canonical_json import canonical_json_bytes
from .capability import InstrumentCapability
from .portable_filename import (
    PortableFilenameError,
    portable_filename_key,
    validate_executor_id,
)
from .roster import (
    check_roster_covers_score,
    parse_roster_document,
    validate_collaboration_document,
)
from .score import ScoreDocument, parse_pitch, pitch_name, parse_score_document
from .self_check import build_issue


AUTHORING_ROSTER_KIND = "tianlai.authoring_roster"
AUTHORING_ROSTER_READINESS_KIND = "tianlai.authoring_roster_readiness"
AUTHORING_ROSTER_ERROR_KIND = "tianlai.authoring_roster_error"
SCHEMA_VERSION = 1

# The score execution budget already caps projects at 256 parts and 512
# executors.  Keep the editable document bounded at the same public seam so a
# caller cannot be handed an unbounded array before project preflight.
MAX_ASSIGNMENTS = 256
MAX_EXPANDED_EXECUTORS = 512
MAX_KIT_ENTRIES = 512
MAX_GAIN_AUTOMATION_POINTS = 4096
MAX_ARTICULATION_MAP_ENTRIES = 256
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024
MAX_ID_LENGTH = 255
MAX_NAME_LENGTH = 4096
MAX_NOTE_LENGTH = 16_384
MAX_INSTRUMENT_REFERENCE_LENGTH = 4096
MAX_ARTICULATION_LENGTH = 255

LocationSegment: TypeAlias = str | int
Location: TypeAlias = tuple[LocationSegment, ...]

_TOP_LEVEL_KEYS = frozenset(
    {"kind", "schema_version", "name", "collaboration", "assignments"}
)
_ASSIGNMENT_KEYS = frozenset(
    {
        "part",
        "instrument",
        "kit",
        "_note",
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
    }
)
_KIT_ENTRY_KEYS = frozenset({"instrument", "transpose"})
_GAIN_AUTOMATION_KEYS = frozenset({"bar", "beat", "offset_db"})
_SEAT_KEYS = frozenset({"azimuth_deg", "distance_m"})
_ROLE_KEYS = frozenset({"function", "prominence", "label"})
_ROLE_FUNCTIONS = frozenset(
    {
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
    }
)
_ROLE_PROMINENCES = frozenset({"foreground", "midground", "background"})
_OVERRIDE_KEYS = frozenset(
    {"release_seconds", "release_tail_gain", "sample_variant"}
)
_COLLABORATION_KEYS = frozenset(
    {"mode", "analysis", "part_groups", "balance_relations"}
)
_COLLABORATION_ANALYSIS_KEYS = frozenset(
    {"metric", "window_ms", "hop_ms", "gate_dbfs"}
)
_PART_GROUP_KEYS = frozenset({"id", "parts"})
_BALANCE_RELATION_KEYS = frozenset(
    {
        "subject",
        "reference",
        "target_offset_db",
        "tolerance_db",
        "max_suggestion_db",
    }
)


class AuthoringRosterError(ValueError):
    """Stable, machine-readable authoring-roster contract failure.

    ``location_segments`` contains only locations inside the JSON document.
    It never contains a filesystem path, and the wrapped formal-roster error
    is deliberately not copied into the public message or issue payload.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        location_segments: Location = (),
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.location_segments = tuple(location_segments)
        # ``location`` is a convenient compatibility alias for callers that
        # already consume issue-like errors.
        self.location = self.location_segments
        self.details = copy.deepcopy(dict(details or {}))
        rendered_location = _render_location(self.location_segments)
        suffix = f" at {rendered_location}" if rendered_location else ""
        super().__init__(f"{code}{suffix}: {message}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": AUTHORING_ROSTER_ERROR_KIND,
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "code": self.code,
            "message": self.message,
            "location": list(self.location_segments),
        }
        if self.details:
            result["details"] = copy.deepcopy(self.details)
        return result

    def to_issue(self) -> dict[str, Any]:
        """Project this failure onto Tianlai's shared blocking-issue shape."""

        return build_issue(
            severity="error",
            code=self.code,
            stage="authoring_roster",
            message=self.message,
            scope={
                "kind": "authoring_roster",
                "location": list(self.location_segments),
            },
            suggestions=[
                "Correct this orchestration field before preparing a render."
            ],
            **copy.deepcopy(self.details),
        )


@dataclass(frozen=True, slots=True)
class AuthoringAssignment:
    """One immutable, validated authoring assignment."""

    index: int
    part: str
    route: str
    _canonical_payload: bytes

    @property
    def assigned(self) -> bool:
        return self.route != "unassigned"

    @property
    def instrument(self) -> str | None:
        value = self.to_dict().get("instrument")
        return value if isinstance(value, str) else None

    @property
    def kit(self) -> dict[str, Any] | None:
        value = self.to_dict().get("kit")
        return value if isinstance(value, dict) else None

    def to_dict(self) -> dict[str, Any]:
        document = json.loads(self._canonical_payload.decode("utf-8"))
        if not isinstance(document, dict):  # pragma: no cover - constructor invariant
            raise AssertionError("authoring assignment payload is not an object")
        return document


@dataclass(frozen=True, slots=True)
class AuthoringRoster:
    """A validated authoring roster bound to one score part set."""

    name: str | None
    assignments: tuple[AuthoringAssignment, ...]
    _collaboration_payload: bytes | None = None

    @property
    def collaboration(self) -> dict[str, Any] | None:
        if self._collaboration_payload is None:
            return None
        document = json.loads(self._collaboration_payload.decode("utf-8"))
        if not isinstance(document, dict):  # pragma: no cover - constructor invariant
            raise AssertionError("authoring collaboration payload is not an object")
        return document

    @property
    def unassigned_parts(self) -> tuple[str, ...]:
        return tuple(item.part for item in self.assignments if not item.assigned)

    @property
    def ready(self) -> bool:
        return not self.unassigned_parts

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "kind": AUTHORING_ROSTER_KIND,
            "schema_version": SCHEMA_VERSION,
            "assignments": [item.to_dict() for item in self.assignments],
        }
        if self.name is not None:
            document["name"] = self.name
        collaboration = self.collaboration
        if collaboration is not None:
            document["collaboration"] = collaboration
        return document


def _render_location(location: Location) -> str:
    rendered = "$"
    for segment in location:
        if isinstance(segment, int):
            rendered += f"[{segment}]"
        else:
            rendered += f".{segment}"
    return rendered if location else ""


def _error(
    code: str,
    message: str,
    location: Location = (),
    *,
    details: Mapping[str, Any] | None = None,
) -> AuthoringRosterError:
    return AuthoringRosterError(
        code,
        message,
        location_segments=location,
        details=details,
    )


def _reject_unknown_keys(
    value: dict[Any, Any], allowed: frozenset[str], location: Location
) -> None:
    unknown = sorted(
        (key for key in value if not isinstance(key, str) or key not in allowed),
        key=lambda item: str(item),
    )
    if unknown:
        key = unknown[0]
        segment = key if isinstance(key, str) else str(key)
        raise _error(
            "authoring_roster.unknown_field",
            "The document contains an unsupported field.",
            (*location, segment),
        )


def _require_field(
    value: dict[str, Any], key: str, location: Location
) -> Any:
    if key not in value:
        raise _error(
            "authoring_roster.missing_field",
            "A required field is missing.",
            (*location, key),
        )
    return value[key]


def _bounded_string(
    value: object,
    location: Location,
    *,
    maximum: int,
    non_empty: bool = True,
) -> str:
    if not isinstance(value, str):
        raise _error(
            "authoring_roster.invalid_type",
            "Expected a string.",
            location,
        )
    if non_empty and not value:
        raise _error(
            "authoring_roster.invalid_value",
            "The string must not be empty.",
            location,
        )
    if len(value) > maximum:
        raise _error(
            "authoring_roster.limit_exceeded",
            f"The string may contain at most {maximum} characters.",
            location,
            details={"actual": len(value), "maximum": maximum},
        )
    return value


def _finite_number(
    value: object,
    location: Location,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise _error(
            "authoring_roster.invalid_type",
            "Expected a finite JSON number.",
            location,
        )
    result = float(value)
    if not math.isfinite(result):
        raise _error(
            "authoring_roster.nonfinite_number",
            "Numbers must be finite.",
            location,
        )
    if minimum is not None and result < minimum:
        raise _error(
            "authoring_roster.out_of_range",
            f"The value must be at least {minimum:g}.",
            location,
            details={"minimum": minimum},
        )
    if maximum is not None and result > maximum:
        raise _error(
            "authoring_roster.out_of_range",
            f"The value must be at most {maximum:g}.",
            location,
            details={"maximum": maximum},
        )
    return result


def _integer(
    value: object,
    location: Location,
    *,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(
            "authoring_roster.invalid_type",
            "Expected an integer.",
            location,
        )
    if minimum is not None and value < minimum:
        raise _error(
            "authoring_roster.out_of_range",
            f"The value must be at least {minimum}.",
            location,
            details={"minimum": minimum},
        )
    return value


def _portable_filename_key(value: str) -> str:
    return portable_filename_key(value)


def _portable_id(value: object, location: Location) -> str:
    text = _bounded_string(
        value,
        location,
        maximum=MAX_ID_LENGTH,
    )
    try:
        return validate_executor_id(text)
    except PortableFilenameError as exc:
        raise _error(
            "authoring_roster.invalid_portable_id",
            f"The identifier cannot become a portable WAV filename: {exc.reason}.",
            location,
        ) from exc


def _validate_gain_automation(value: object, location: Location) -> None:
    if not isinstance(value, list) or not value:
        raise _error(
            "authoring_roster.invalid_type",
            "Gain automation must be a non-empty array.",
            location,
        )
    if len(value) > MAX_GAIN_AUTOMATION_POINTS:
        raise _error(
            "authoring_roster.limit_exceeded",
            f"Gain automation may contain at most {MAX_GAIN_AUTOMATION_POINTS} points.",
            location,
            details={
                "actual": len(value),
                "maximum": MAX_GAIN_AUTOMATION_POINTS,
            },
        )
    previous: tuple[int, float] | None = None
    for index, raw in enumerate(value):
        item_location = (*location, index)
        if not isinstance(raw, dict):
            raise _error(
                "authoring_roster.invalid_type",
                "A gain-automation point must be an object.",
                item_location,
            )
        _reject_unknown_keys(raw, _GAIN_AUTOMATION_KEYS, item_location)
        bar = _integer(
            _require_field(raw, "bar", item_location),
            (*item_location, "bar"),
            minimum=1,
        )
        beat = _finite_number(
            _require_field(raw, "beat", item_location),
            (*item_location, "beat"),
            minimum=1.0,
        )
        _finite_number(
            _require_field(raw, "offset_db", item_location),
            (*item_location, "offset_db"),
            minimum=-24.0,
            maximum=24.0,
        )
        point = (bar, beat)
        if previous is not None and point <= previous:
            raise _error(
                "authoring_roster.invalid_automation_order",
                "Gain-automation points must be strictly ordered without duplicates.",
                item_location,
            )
        previous = point
    first = value[0]
    if first["bar"] != 1 or float(first["beat"]) != 1.0:
        raise _error(
            "authoring_roster.invalid_automation_start",
            "Gain automation must start at bar 1, beat 1.",
            (*location, 0),
        )


def _validate_seat(value: object, location: Location) -> None:
    if not isinstance(value, dict):
        raise _error(
            "authoring_roster.invalid_type",
            "Seat must be an object.",
            location,
        )
    _reject_unknown_keys(value, _SEAT_KEYS, location)
    if "azimuth_deg" in value:
        _finite_number(
            value["azimuth_deg"],
            (*location, "azimuth_deg"),
            minimum=-90.0,
            maximum=90.0,
        )
    if "distance_m" in value:
        _finite_number(
            value["distance_m"],
            (*location, "distance_m"),
            minimum=0.1,
            maximum=60.0,
        )


def _validate_role(value: object, location: Location) -> None:
    if not isinstance(value, dict):
        raise _error(
            "authoring_roster.invalid_type",
            "Role must be an object.",
            location,
        )
    _reject_unknown_keys(value, _ROLE_KEYS, location)
    function = _require_field(value, "function", location)
    if not isinstance(function, str) or function not in _ROLE_FUNCTIONS:
        raise _error(
            "authoring_roster.invalid_value",
            "Role function is not supported.",
            (*location, "function"),
        )
    prominence = _require_field(value, "prominence", location)
    if not isinstance(prominence, str) or prominence not in _ROLE_PROMINENCES:
        raise _error(
            "authoring_roster.invalid_value",
            "Role prominence is not supported.",
            (*location, "prominence"),
        )
    if "label" in value:
        _bounded_string(
            value["label"],
            (*location, "label"),
            maximum=MAX_NAME_LENGTH,
        )


def _validate_articulation_map(value: object, location: Location) -> None:
    if not isinstance(value, dict):
        raise _error(
            "authoring_roster.invalid_type",
            "Articulation map must be an object.",
            location,
        )
    if len(value) > MAX_ARTICULATION_MAP_ENTRIES:
        raise _error(
            "authoring_roster.limit_exceeded",
            f"Articulation map may contain at most {MAX_ARTICULATION_MAP_ENTRIES} entries.",
            location,
            details={
                "actual": len(value),
                "maximum": MAX_ARTICULATION_MAP_ENTRIES,
            },
        )
    for key, target in value.items():
        if not isinstance(key, str):
            raise _error(
                "authoring_roster.invalid_type",
                "Articulation-map keys must be strings.",
                (*location, str(key)),
            )
        _bounded_string(
            key,
            (*location, key),
            maximum=MAX_ARTICULATION_LENGTH,
        )
        _bounded_string(
            target,
            (*location, key),
            maximum=MAX_ARTICULATION_LENGTH,
        )


def _validate_overrides(value: object, location: Location) -> None:
    if not isinstance(value, dict):
        raise _error(
            "authoring_roster.invalid_type",
            "Overrides must be an object.",
            location,
        )
    _reject_unknown_keys(value, _OVERRIDE_KEYS, location)
    if "release_seconds" in value:
        _finite_number(
            value["release_seconds"],
            (*location, "release_seconds"),
            minimum=0.0,
        )
    if "release_tail_gain" in value:
        _finite_number(
            value["release_tail_gain"],
            (*location, "release_tail_gain"),
            minimum=0.0,
            maximum=1.0,
        )
    if "sample_variant" in value:
        _bounded_string(
            value["sample_variant"],
            (*location, "sample_variant"),
            maximum=MAX_ID_LENGTH,
        )


def _reject_collaboration_unknown_keys(
    value: object, location: Location
) -> None:
    """Retain authoring-style locations while sharing formal semantics."""

    if not isinstance(value, dict):
        return
    _reject_unknown_keys(value, _COLLABORATION_KEYS, location)

    analysis = value.get("analysis")
    if isinstance(analysis, dict):
        _reject_unknown_keys(
            analysis,
            _COLLABORATION_ANALYSIS_KEYS,
            (*location, "analysis"),
        )

    part_groups = value.get("part_groups")
    if isinstance(part_groups, list):
        for index, group in enumerate(part_groups):
            if isinstance(group, dict):
                _reject_unknown_keys(
                    group,
                    _PART_GROUP_KEYS,
                    (*location, "part_groups", index),
                )

    relations = value.get("balance_relations")
    if isinstance(relations, list):
        for index, relation in enumerate(relations):
            if isinstance(relation, dict):
                _reject_unknown_keys(
                    relation,
                    _BALANCE_RELATION_KEYS,
                    (*location, "balance_relations", index),
                )


def _validate_shared_assignment_fields(
    raw: dict[str, Any], location: Location
) -> None:
    if "_note" in raw:
        _bounded_string(
            raw["_note"],
            (*location, "_note"),
            maximum=MAX_NOTE_LENGTH,
            non_empty=False,
        )
    if "executor_id" in raw:
        _portable_id(raw["executor_id"], (*location, "executor_id"))
    if "gain_db" in raw:
        _finite_number(
            raw["gain_db"],
            (*location, "gain_db"),
            minimum=-60.0,
            maximum=12.0,
        )
    if "gain_automation" in raw:
        _validate_gain_automation(
            raw["gain_automation"], (*location, "gain_automation")
        )
    if "pan" in raw:
        _finite_number(
            raw["pan"],
            (*location, "pan"),
            minimum=-1.0,
            maximum=1.0,
        )
    if "transpose" in raw:
        _integer(raw["transpose"], (*location, "transpose"))
    if "dynamic_compression" in raw:
        _finite_number(
            raw["dynamic_compression"],
            (*location, "dynamic_compression"),
            minimum=0.0,
            maximum=1.0,
        )
    if "duration_scale" in raw:
        _finite_number(
            raw["duration_scale"],
            (*location, "duration_scale"),
            minimum=0.1,
            maximum=2.0,
        )
    if "articulation_auto" in raw and not isinstance(
        raw["articulation_auto"], bool
    ):
        raise _error(
            "authoring_roster.invalid_type",
            "Articulation-auto must be boolean.",
            (*location, "articulation_auto"),
        )
    if "seat" in raw:
        _validate_seat(raw["seat"], (*location, "seat"))
    if "role" in raw:
        _validate_role(raw["role"], (*location, "role"))
    if "articulation_map" in raw:
        _validate_articulation_map(
            raw["articulation_map"], (*location, "articulation_map")
        )
    if "overrides" in raw:
        _validate_overrides(raw["overrides"], (*location, "overrides"))


def _instrument_reference(value: object, location: Location) -> str:
    text = _bounded_string(
        value,
        location,
        maximum=MAX_INSTRUMENT_REFERENCE_LENGTH,
    )
    if (
        text != text.strip()
        or text.startswith(("/", "\\"))
        or text.endswith("/")
        or "\\" in text
        or ":" in text
        or "//" in text
        or any(segment in {"", ".", ".."} for segment in text.split("/"))
    ):
        raise _error(
            "authoring_roster.invalid_instrument_reference",
            "Instrument references must be clean catalog-relative IDs.",
            location,
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise _error(
            "authoring_roster.invalid_instrument_reference",
            "Instrument references cannot contain control characters.",
            location,
        )
    return text


def _validate_kit(value: object, location: Location) -> None:
    if not isinstance(value, dict) or not value:
        raise _error(
            "authoring_roster.invalid_kit",
            "A kit route must be a non-empty object.",
            location,
        )
    if len(value) > MAX_KIT_ENTRIES:
        raise _error(
            "authoring_roster.limit_exceeded",
            f"A kit may contain at most {MAX_KIT_ENTRIES} entries.",
            location,
            details={"actual": len(value), "maximum": MAX_KIT_ENTRIES},
        )
    seen_pitches: dict[float, str] = {}
    for notehead, reference in value.items():
        if (
            not isinstance(notehead, str)
            or not notehead
            or len(notehead) > 255
        ):
            raise _error(
                "authoring_roster.invalid_kit_notehead",
                "Kit noteheads must be non-empty strings of at most 255 characters.",
                (*location, str(notehead)),
            )
        try:
            midi = parse_pitch(notehead)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _error(
                "authoring_roster.invalid_kit_notehead",
                "The kit notehead is not a valid finite MIDI pitch.",
                (*location, notehead),
            ) from exc
        previous = seen_pitches.get(midi)
        if previous is not None:
            raise _error(
                "authoring_roster.duplicate_kit_pitch",
                "Two kit noteheads resolve to the same MIDI pitch.",
                (*location, notehead),
                details={"first_notehead": previous},
            )
        seen_pitches[midi] = notehead
        entry_location = (*location, notehead)
        if isinstance(reference, str):
            _instrument_reference(reference, entry_location)
            continue
        if not isinstance(reference, dict):
            raise _error(
                "authoring_roster.invalid_type",
                "A kit entry must be an instrument string or an entry object.",
                entry_location,
            )
        _reject_unknown_keys(reference, _KIT_ENTRY_KEYS, entry_location)
        _instrument_reference(
            _require_field(reference, "instrument", entry_location),
            (*entry_location, "instrument"),
        )
        if "transpose" in reference:
            _integer(
                reference["transpose"], (*entry_location, "transpose")
            )


def _parse_assignment(raw: object, index: int) -> AuthoringAssignment:
    location: Location = ("assignments", index)
    if not isinstance(raw, dict):
        raise _error(
            "authoring_roster.invalid_type",
            "An assignment must be an object.",
            location,
        )
    _reject_unknown_keys(raw, _ASSIGNMENT_KEYS, location)
    part = _portable_id(
        _require_field(raw, "part", location), (*location, "part")
    )
    has_instrument = "instrument" in raw
    has_kit = "kit" in raw
    if has_instrument == has_kit:
        raise _error(
            "authoring_roster.invalid_route",
            "Declare exactly one route: instrument (string or null) or kit.",
            location,
        )
    if has_instrument:
        instrument = raw["instrument"]
        if instrument is None:
            route = "unassigned"
        else:
            _instrument_reference(instrument, (*location, "instrument"))
            route = "instrument"
    else:
        _validate_kit(raw["kit"], (*location, "kit"))
        route = "kit"
    _validate_shared_assignment_fields(raw, location)
    try:
        payload = canonical_json_bytes(raw)
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise _error(
            "authoring_roster.nonportable_json",
            "The assignment must contain portable JSON values.",
            location,
        ) from exc
    return AuthoringAssignment(
        index=index,
        part=part,
        route=route,
        _canonical_payload=payload,
    )


def _coerce_score(score: ScoreDocument | dict[str, Any]) -> ScoreDocument:
    if isinstance(score, ScoreDocument):
        return score
    if not isinstance(score, dict):
        raise _error(
            "authoring_roster.invalid_score",
            "The bound score must be a parsed score or a score JSON object.",
        )
    try:
        return parse_score_document(score)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise _error(
            "authoring_roster.invalid_score",
            "The bound score document is invalid.",
        ) from exc


def _validate_part_binding(
    assignments: tuple[AuthoringAssignment, ...], score: ScoreDocument
) -> None:
    score_parts = tuple(part.id for part in score.parts)
    score_part_set = set(score_parts)
    seen: dict[str, int] = {}
    for assignment in assignments:
        previous = seen.get(assignment.part)
        if previous is not None:
            raise _error(
                "authoring_roster.duplicate_part",
                "A score part may appear in the authoring roster only once.",
                ("assignments", assignment.index, "part"),
                details={"part": assignment.part, "first_index": previous},
            )
        seen[assignment.part] = assignment.index
        if assignment.part not in score_part_set:
            raise _error(
                "authoring_roster.extra_part",
                "The assignment refers to a part that is not in the bound score.",
                ("assignments", assignment.index, "part"),
                details={"part": assignment.part},
            )
    missing = [part for part in score_parts if part not in seen]
    if missing:
        raise _error(
            "authoring_roster.missing_part",
            "Every score part must have exactly one authoring assignment.",
            ("assignments",),
            details={"parts": missing},
        )


def _validate_executor_budget_and_ids(
    assignments: tuple[AuthoringAssignment, ...],
) -> None:
    expanded = 0
    seen: dict[str, tuple[str, Location]] = {}
    for assignment in assignments:
        if not assignment.assigned:
            continue
        raw = assignment.to_dict()
        emitted: list[tuple[str, Location]] = []
        if assignment.route == "instrument":
            executor_id = str(raw.get("executor_id", assignment.part))
            emitted.append(
                (
                    executor_id,
                    (
                        "assignments",
                        assignment.index,
                        "executor_id"
                        if "executor_id" in raw
                        else "part",
                    ),
                )
            )
        else:
            kit = raw["kit"]
            for notehead in sorted(kit):
                midi = parse_pitch(notehead)
                emitted.append(
                    (
                        f"{assignment.part}.{pitch_name(midi)}",
                        ("assignments", assignment.index, "kit", notehead),
                    )
                )
        expanded += len(emitted)
        if expanded > MAX_EXPANDED_EXECUTORS:
            raise _error(
                "authoring_roster.limit_exceeded",
                f"Assigned routes may expand to at most {MAX_EXPANDED_EXECUTORS} executors.",
                ("assignments", assignment.index),
                details={
                    "actual": expanded,
                    "maximum": MAX_EXPANDED_EXECUTORS,
                },
            )
        for executor_id, location in emitted:
            _portable_id(executor_id, location)
            key = _portable_filename_key(executor_id)
            previous = seen.get(key)
            if previous is not None:
                previous_id, _previous_location = previous
                raise _error(
                    "authoring_roster.portable_id_conflict",
                    "Two assigned routes would create the same portable executor filename.",
                    location,
                    details={"first_executor_id": previous_id},
                )
            seen[key] = (executor_id, location)


def parse_authoring_roster_document(
    data: dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> AuthoringRoster:
    """Validate and bind one authoring roster to every part in ``score``."""

    if not isinstance(data, dict):
        raise _error(
            "authoring_roster.invalid_type",
            "The authoring roster must be a JSON object.",
        )
    _reject_unknown_keys(data, _TOP_LEVEL_KEYS, ())
    kind = _require_field(data, "kind", ())
    if kind != AUTHORING_ROSTER_KIND:
        raise _error(
            "authoring_roster.unsupported_kind",
            f"kind must be {AUTHORING_ROSTER_KIND!r}.",
            ("kind",),
        )
    schema_version = _require_field(data, "schema_version", ())
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise _error(
            "authoring_roster.unsupported_schema_version",
            f"schema_version must be {SCHEMA_VERSION}.",
            ("schema_version",),
        )
    name: str | None = None
    if "name" in data:
        name = _bounded_string(
            data["name"],
            ("name",),
            maximum=MAX_NAME_LENGTH,
            non_empty=False,
        )
    raw_assignments = _require_field(data, "assignments", ())
    if not isinstance(raw_assignments, list) or not raw_assignments:
        raise _error(
            "authoring_roster.invalid_type",
            "assignments must be a non-empty array.",
            ("assignments",),
        )
    if len(raw_assignments) > MAX_ASSIGNMENTS:
        raise _error(
            "authoring_roster.limit_exceeded",
            f"assignments may contain at most {MAX_ASSIGNMENTS} items.",
            ("assignments",),
            details={
                "actual": len(raw_assignments),
                "maximum": MAX_ASSIGNMENTS,
            },
        )
    assignments = tuple(
        _parse_assignment(raw, index)
        for index, raw in enumerate(raw_assignments)
    )
    collaboration_payload: bytes | None = None
    if "collaboration" in data:
        raw_collaboration = data["collaboration"]
        _reject_collaboration_unknown_keys(
            raw_collaboration,
            ("collaboration",),
        )
        try:
            validate_collaboration_document(
                raw_collaboration,
                frozenset(item.part for item in assignments),
                path="authoring_roster.collaboration",
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise _error(
                "authoring_roster.invalid_collaboration",
                "The collaboration intent is invalid.",
                ("collaboration",),
            ) from exc
        try:
            collaboration_payload = canonical_json_bytes(raw_collaboration)
        except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
            raise _error(
                "authoring_roster.nonportable_json",
                "The collaboration intent must contain finite, portable JSON values.",
                ("collaboration",),
            ) from exc
    try:
        encoded = canonical_json_bytes(data)
    except (TypeError, ValueError, OverflowError, UnicodeError) as exc:
        raise _error(
            "authoring_roster.nonportable_json",
            "The authoring roster must contain finite, portable JSON values.",
        ) from exc
    if len(encoded) > MAX_DOCUMENT_BYTES:
        raise _error(
            "authoring_roster.limit_exceeded",
            f"The authoring roster may contain at most {MAX_DOCUMENT_BYTES} UTF-8 bytes.",
            (),
            details={"actual": len(encoded), "maximum": MAX_DOCUMENT_BYTES},
        )
    parsed_score = _coerce_score(score)
    _validate_part_binding(assignments, parsed_score)
    _validate_executor_budget_and_ids(assignments)
    return AuthoringRoster(
        name=name,
        assignments=assignments,
        _collaboration_payload=collaboration_payload,
    )


def validate_authoring_roster_document(
    data: dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> AuthoringRoster:
    """Validation spelling retained for callers that do not need to parse files."""

    return parse_authoring_roster_document(data, score)


def parse_authoring_roster(
    data: dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> AuthoringRoster:
    return parse_authoring_roster_document(data, score)


def validate_authoring_roster(
    data: dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> AuthoringRoster:
    return parse_authoring_roster_document(data, score)


def _coerce_authoring(
    authoring: AuthoringRoster | dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> tuple[AuthoringRoster, ScoreDocument]:
    parsed_score = _coerce_score(score)
    raw = authoring.to_dict() if isinstance(authoring, AuthoringRoster) else authoring
    if not isinstance(raw, dict):
        raise _error(
            "authoring_roster.invalid_type",
            "The authoring roster must be a parsed roster or a JSON object.",
        )
    return parse_authoring_roster_document(raw, parsed_score), parsed_score


def authoring_roster_readiness(
    authoring: AuthoringRoster | dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> dict[str, Any]:
    """Return route-completeness only; capability readiness stays downstream."""

    parsed, parsed_score = _coerce_authoring(authoring, score)
    unassigned = [
        {
            "part": assignment.part,
            "location": ["assignments", assignment.index, "instrument"],
        }
        for assignment in parsed.assignments
        if not assignment.assigned
    ]
    assigned_count = len(parsed.assignments) - len(unassigned)
    return {
        "kind": AUTHORING_ROSTER_READINESS_KIND,
        "schema_version": SCHEMA_VERSION,
        "ready": not unassigned,
        "total_parts": len(parsed_score.parts),
        "assigned_parts": assigned_count,
        "unassigned": unassigned,
    }


def project_authoring_roster_readiness(
    authoring: AuthoringRoster | dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> dict[str, Any]:
    return authoring_roster_readiness(authoring, score)


def readiness_projection(
    authoring: AuthoringRoster | dict[str, Any],
    score: ScoreDocument | dict[str, Any],
) -> dict[str, Any]:
    return authoring_roster_readiness(authoring, score)


def to_formal_roster(
    authoring: AuthoringRoster | dict[str, Any],
    score: ScoreDocument | dict[str, Any],
    capabilities: dict[str, InstrumentCapability],
) -> dict[str, Any]:
    """Convert a complete authoring roster into the existing formal contract.

    An incomplete roster raises a structured authoring error.  A complete
    result is accepted only after the formal parser resolves capabilities and
    ``check_roster_covers_score`` independently confirms coverage.
    """

    parsed, parsed_score = _coerce_authoring(authoring, score)
    unassigned = [item for item in parsed.assignments if not item.assigned]
    if unassigned:
        first = unassigned[0]
        raise _error(
            "authoring_roster.unassigned_part",
            "Every score part must be assigned before preparing a render.",
            ("assignments", first.index, "instrument"),
            details={"parts": [item.part for item in unassigned]},
        )

    formal: dict[str, Any] = {
        "assignments": [item.to_dict() for item in parsed.assignments]
    }
    if parsed.name is not None:
        formal["name"] = parsed.name
    collaboration = parsed.collaboration
    if collaboration is not None:
        formal["collaboration"] = collaboration
    try:
        resolved = parse_roster_document(formal, capabilities)
        check_roster_covers_score(resolved, parsed_score)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        # The formal parser may read instrument manifests, whose native errors
        # can contain absolute paths.  Preserve that diagnostic only as the
        # chained exception; never expose it in this public structured error.
        raise _error(
            "authoring_roster.formal_validation_failed",
            "The assigned roster could not be validated against the available instrument capabilities.",
            ("assignments",),
        ) from exc
    return copy.deepcopy(formal)


__all__ = [
    "AUTHORING_ROSTER_ERROR_KIND",
    "AUTHORING_ROSTER_KIND",
    "AUTHORING_ROSTER_READINESS_KIND",
    "AuthoringAssignment",
    "AuthoringRoster",
    "AuthoringRosterError",
    "MAX_ARTICULATION_MAP_ENTRIES",
    "MAX_ASSIGNMENTS",
    "MAX_DOCUMENT_BYTES",
    "MAX_EXPANDED_EXECUTORS",
    "MAX_GAIN_AUTOMATION_POINTS",
    "MAX_ID_LENGTH",
    "MAX_INSTRUMENT_REFERENCE_LENGTH",
    "MAX_KIT_ENTRIES",
    "MAX_NAME_LENGTH",
    "MAX_NOTE_LENGTH",
    "SCHEMA_VERSION",
    "authoring_roster_readiness",
    "parse_authoring_roster",
    "parse_authoring_roster_document",
    "project_authoring_roster_readiness",
    "readiness_projection",
    "to_formal_roster",
    "validate_authoring_roster",
    "validate_authoring_roster_document",
]
