"""Import scores into one creator-confirmed Tianlai project boundary.

The MIDI and MusicXML parsers deliberately stop before choosing instruments.
This module gives both import paths the same three-document result:

* a validated score-v1 document,
* an import report bound to the source bytes and canonical score JSON,
* a non-executable roster draft whose routing fields are intentionally empty.

``promote_roster`` is the only transition from that draft to an executable
roster.  It requires an explicit route for every score part, rechecks the score
hash, resolves every instrument through the live capability catalogue, and
applies the shared availability policy.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

from .capability import InstrumentCapability
from .canonical_json import CANONICALIZATION, canonical_json_sha256
from .midi_import import (
    build_roster_draft as build_midi_roster_draft,
    read_midi,
)
from .musicxml_import import read_musicxml
from .preflight import enforce_roster_availability
from .resource_limits import validate_score_resource_limits
from .roster import check_roster_covers_score, parse_roster_document
from .score import parse_score_document
from .score_time import validate_score_time_coordinates


IMPORT_FORMAT = "tianlai.project_import"
IMPORT_VERSION = 1
REPORT_FORMAT = "tianlai.import_report"
REPORT_VERSION = 1
DRAFT_FORMAT = "tianlai.roster_draft"
MAX_CANDIDATES_PER_PART = 16
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DEFAULT_FILENAMES = {
    "score": "score.json",
    "import_report": "import-report.json",
    "roster_draft": "roster-draft.json",
}


def _source_sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_length = 0
    with path.open("rb") as source:
        while True:
            block = source.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            byte_length += len(block)
    return digest.hexdigest(), byte_length


def _score_v1(score: object) -> tuple[dict[str, Any], Any]:
    if not isinstance(score, dict):
        raise ValueError("score must be an object")
    parsed = parse_score_document(score)
    validate_score_time_coordinates(parsed)
    validate_score_resource_limits(score, parsed)
    if parsed.schema_version != 1:
        raise ValueError("imported score must use schema_version 1")
    return score, parsed


def _score_part_ids(score: dict[str, Any]) -> list[str]:
    raw_parts = score.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise ValueError("score.parts must be a non-empty array")
    result: list[str] = []
    for position, raw in enumerate(raw_parts):
        if not isinstance(raw, dict):
            raise ValueError(f"score.parts[{position}] must be an object")
        part_id = raw.get("id")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(f"score.parts[{position}].id must be non-empty")
        if part_id in result:
            raise ValueError(f"duplicate score part id: {part_id}")
        result.append(part_id)
    return result


def _generic_report(
    *,
    source_kind: str,
    source_path: Path,
    source_format: str,
    source_sha256: str,
    source_byte_length: int,
    score: dict[str, Any],
    parser_report: Mapping[str, Any],
) -> dict[str, Any]:
    score_sha256 = canonical_json_sha256(score)
    report = copy.deepcopy(dict(parser_report))
    report.update(
        {
            "format": REPORT_FORMAT,
            "version": REPORT_VERSION,
            "source_kind": source_kind,
            "source": {
                "kind": source_kind,
                "name": source_path.name,
                "format": source_format,
                "sha256": source_sha256,
                "byte_length": source_byte_length,
            },
            "score": {
                "schema_version": 1,
                "canonical_sha256": score_sha256,
                "canonicalization": CANONICALIZATION,
            },
        }
    )
    return report


def _musicxml_roster_draft(
    score: dict[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    score_ids = _score_part_ids(score)
    raw_parts = report.get("parts")
    if not isinstance(raw_parts, list):
        raise ValueError("MusicXML import report parts must be an array")
    report_by_id: dict[str, Mapping[str, Any]] = {}
    for position, part in enumerate(raw_parts):
        if not isinstance(part, Mapping):
            raise ValueError(
                f"MusicXML import report parts[{position}] must be an object"
            )
        part_id = part.get("id")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(
                f"MusicXML import report parts[{position}].id must be non-empty"
            )
        if part_id in report_by_id:
            raise ValueError(f"duplicate MusicXML report part id: {part_id}")
        report_by_id[part_id] = part
    if set(report_by_id) != set(score_ids):
        raise ValueError("score parts do not match the MusicXML import report")

    assignments: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for part_id in score_ids:
        part = report_by_id[part_id]
        percussion = bool(part.get("percussion", False))
        route_kind = "kit" if percussion else "instrument"
        assignments.append(
            {
                "part": part_id,
                route_kind: None,
                "gain_db": 0.0,
                "pan": 0.0,
                "role": {
                    "function": "other",
                    "prominence": "midground",
                },
            }
        )
        evidence.append(
            {
                "part": part_id,
                "source": {
                    "part_name": str(part.get("name", part_id)),
                    "midi_channel": part.get("channel"),
                    "percussion": percussion,
                },
                "note_count": int(part.get("note_count", 0)),
                "range": part.get("range"),
                "noteheads": copy.deepcopy(part.get("noteheads", [])),
                "decisions": {
                    "routing": (
                        "kit_required" if percussion else "instrument_required"
                    ),
                    "gain_db": "default_zero_creator_may_override",
                    "pan": "default_center_creator_may_override",
                    "role": "default_other_midground_creator_may_override",
                    "balance_relations": "optional_creator_owned",
                },
            }
        )

    source = report["source"]
    score_binding = report["score"]
    return {
        "format": DRAFT_FORMAT,
        "version": 1,
        "status": "requires_creator_confirmation",
        "executable": False,
        "source": {
            "kind": "musicxml",
            "input": copy.deepcopy(source),
            "musicxml": {
                "sha256": source["sha256"],
                "byte_length": source["byte_length"],
                "format": source["format"],
            },
            "score": copy.deepcopy(score_binding),
        },
        "draft_roster": {
            "name": f"{score.get('title', '未命名总谱')} MusicXML 编制草稿",
            "assignments": assignments,
            "collaboration": {
                "mode": "manual",
                "balance_relations": [],
            },
        },
        "part_evidence": evidence,
        "notice": [
            "本文件不可直接渲染；每个声部必须由创作者或其授权 Agent 显式路由。",
            "MusicXML 乐器名、MIDI 通道和打击乐标记只作证据，不自动取得执行权限。",
            "普通声部必须选择 instrument；打击乐声部必须逐 notehead 填写 kit。",
            "中性 gain、pan、role 与 manual 协奏设置可由创作者显式覆盖。",
        ],
    }


def _normalise_midi_draft(
    draft: Mapping[str, Any],
    report: Mapping[str, Any],
) -> dict[str, Any]:
    result = copy.deepcopy(dict(draft))
    result["legacy_format"] = result.get("format")
    result["format"] = DRAFT_FORMAT
    result["source_kind"] = "midi"
    source = result.get("source")
    if not isinstance(source, dict):
        raise ValueError("MIDI roster draft source must be an object")
    source["kind"] = "midi"
    source["input"] = copy.deepcopy(report["source"])
    source["score"] = copy.deepcopy(report["score"])
    return result


def _attach_hints(
    bundle: dict[str, Any],
    capabilities: Mapping[str, InstrumentCapability] | None,
    *,
    trusted_only: bool,
    trusted_instruments: Collection[str] | None,
    candidate_limit: int,
) -> None:
    if capabilities is None:
        return
    bundle["roster_draft"]["routing_hints"] = build_routing_hints(
        bundle["roster_draft"],
        bundle["score"],
        capabilities,
        trusted_only=trusted_only,
        trusted_instruments=trusted_instruments,
        limit=candidate_limit,
    )


def import_midi_project(
    path: str | Path,
    *,
    capabilities: Mapping[str, InstrumentCapability] | None = None,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
    candidate_limit: int = 8,
) -> dict[str, Any]:
    """Import MIDI without assigning any Tianlai instrument."""

    source_path = Path(path)
    score, parser_report = read_midi(source_path)
    _score_v1(score)
    parser_document = parser_report.to_dict()
    report = _generic_report(
        source_kind="midi",
        source_path=source_path,
        source_format=f"smf-{parser_report.midi_format}",
        source_sha256=parser_report.source_midi_sha256,
        source_byte_length=parser_report.source_midi_byte_length,
        score=score,
        parser_report=parser_document,
    )
    # Preserve the established top-level evidence names used by existing
    # consumers while also exposing the unified ``source``/``score`` blocks.
    report["source_midi_sha256"] = report["source"]["sha256"]
    report["source_midi_byte_length"] = report["source"]["byte_length"]
    report["score_canonical_sha256"] = report["score"]["canonical_sha256"]
    draft = _normalise_midi_draft(
        build_midi_roster_draft(score, parser_report),
        report,
    )
    bundle = {
        "format": IMPORT_FORMAT,
        "version": IMPORT_VERSION,
        "score": copy.deepcopy(score),
        "import_report": report,
        "roster_draft": draft,
    }
    _attach_hints(
        bundle,
        capabilities,
        trusted_only=trusted_only,
        trusted_instruments=trusted_instruments,
        candidate_limit=candidate_limit,
    )
    validate_import_bundle(bundle)
    return bundle


def import_musicxml_project(
    path: str | Path,
    *,
    capabilities: Mapping[str, InstrumentCapability] | None = None,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
    candidate_limit: int = 8,
) -> dict[str, Any]:
    """Import MusicXML and bind the result to the original XML/MXL bytes."""

    source_path = Path(path)
    before_sha256, before_size = _source_sha256(source_path)
    score, parser_report = read_musicxml(source_path)
    after_sha256, after_size = _source_sha256(source_path)
    if (before_sha256, before_size) != (after_sha256, after_size):
        raise ValueError("MusicXML source changed while it was being imported")
    _score_v1(score)
    report = _generic_report(
        source_kind="musicxml",
        source_path=source_path,
        source_format=parser_report.source_format,
        source_sha256=before_sha256,
        source_byte_length=before_size,
        score=score,
        parser_report=parser_report.to_dict(),
    )
    report["source_musicxml_sha256"] = before_sha256
    report["source_musicxml_byte_length"] = before_size
    report["score_canonical_sha256"] = report["score"]["canonical_sha256"]
    draft = _musicxml_roster_draft(score, report)
    bundle = {
        "format": IMPORT_FORMAT,
        "version": IMPORT_VERSION,
        "score": copy.deepcopy(score),
        "import_report": report,
        "roster_draft": draft,
    }
    _attach_hints(
        bundle,
        capabilities,
        trusted_only=trusted_only,
        trusted_instruments=trusted_instruments,
        candidate_limit=candidate_limit,
    )
    validate_import_bundle(bundle)
    return bundle


def import_project(
    path: str | Path,
    *,
    source_kind: str | None = None,
    capabilities: Mapping[str, InstrumentCapability] | None = None,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
    candidate_limit: int = 8,
) -> dict[str, Any]:
    """Dispatch one supported score file into the unified import bundle."""

    source_path = Path(path)
    kind = None if source_kind is None else source_kind.strip().lower()
    if kind is None:
        suffix = source_path.suffix.lower()
        if suffix in {".mid", ".midi"}:
            kind = "midi"
        elif suffix in {".xml", ".musicxml", ".mxl"}:
            kind = "musicxml"
        else:
            raise ValueError(
                "cannot infer source kind; expected .mid/.midi/.xml/.musicxml/.mxl"
            )
    arguments = {
        "capabilities": capabilities,
        "trusted_only": trusted_only,
        "trusted_instruments": trusted_instruments,
        "candidate_limit": candidate_limit,
    }
    if kind == "midi":
        return import_midi_project(source_path, **arguments)
    if kind == "musicxml":
        return import_musicxml_project(source_path, **arguments)
    raise ValueError("source_kind must be 'midi' or 'musicxml'")


def _draft_document(draft: object) -> dict[str, Any]:
    if not isinstance(draft, Mapping):
        raise ValueError("roster draft must be an object")
    current: object = draft
    if draft.get("format") == IMPORT_FORMAT:
        current = draft.get("roster_draft")
    if not isinstance(current, dict):
        raise ValueError("roster draft must be an object")
    if current.get("format") not in {
        DRAFT_FORMAT,
        "tianlai.midi_roster_draft",
    }:
        raise ValueError("unsupported roster draft format")
    if current.get("version") != 1:
        raise ValueError("unsupported roster draft version")
    if current.get("executable") is not False:
        raise ValueError("roster draft must remain explicitly non-executable")
    if current.get("status") != "requires_creator_confirmation":
        raise ValueError("roster draft is not awaiting creator confirmation")
    return current


def _draft_score_sha256(draft: Mapping[str, Any]) -> str:
    source = draft.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("roster draft source must be an object")
    score = source.get("score")
    if not isinstance(score, Mapping):
        raise ValueError("roster draft source.score must be an object")
    digest = score.get("canonical_sha256")
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise ValueError("roster draft score hash is invalid")
    canonicalization = score.get("canonicalization")
    if canonicalization not in (None, CANONICALIZATION):
        raise ValueError("roster draft uses an unsupported score canonicalization")
    return digest


def _baseline_assignments(
    draft: Mapping[str, Any],
    score_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    raw_roster = draft.get("draft_roster")
    if not isinstance(raw_roster, Mapping):
        raise ValueError("roster draft draft_roster must be an object")
    raw_assignments = raw_roster.get("assignments")
    if not isinstance(raw_assignments, list):
        raise ValueError("roster draft assignments must be an array")
    result: dict[str, dict[str, Any]] = {}
    for position, assignment in enumerate(raw_assignments):
        if not isinstance(assignment, dict):
            raise ValueError(
                f"roster draft assignments[{position}] must be an object"
            )
        part_id = assignment.get("part")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(
                f"roster draft assignments[{position}].part must be non-empty"
            )
        if part_id in result:
            raise ValueError(f"duplicate roster draft part: {part_id}")
        has_instrument = "instrument" in assignment
        has_kit = "kit" in assignment
        if has_instrument == has_kit:
            raise ValueError(
                f"roster draft part {part_id!r} must declare exactly one route placeholder"
            )
        placeholder = assignment["instrument" if has_instrument else "kit"]
        if placeholder is not None:
            raise ValueError(
                f"roster draft part {part_id!r} already contains an executable route"
            )
        result[part_id] = copy.deepcopy(assignment)
    if set(result) != set(score_ids):
        raise ValueError("roster draft parts do not exactly match the bound score")
    return result


def validate_import_bundle(bundle: Mapping[str, Any]) -> None:
    """Validate the hashes and non-executable boundary of an import bundle."""

    if not isinstance(bundle, Mapping):
        raise ValueError("import bundle must be an object")
    if bundle.get("format") != IMPORT_FORMAT or bundle.get("version") != IMPORT_VERSION:
        raise ValueError("unsupported import bundle format or version")
    raw_score = bundle.get("score")
    if not isinstance(raw_score, dict):
        raise ValueError("import bundle score must be an object")
    score, _ = _score_v1(raw_score)
    score_sha256 = canonical_json_sha256(score)
    report = bundle.get("import_report")
    if not isinstance(report, Mapping):
        raise ValueError("import bundle import_report must be an object")
    if (
        report.get("format") != REPORT_FORMAT
        or report.get("version") != REPORT_VERSION
    ):
        raise ValueError("unsupported import report format or version")
    report_score = report.get("score")
    if (
        not isinstance(report_score, Mapping)
        or report_score.get("canonical_sha256") != score_sha256
        or report_score.get("canonicalization") != CANONICALIZATION
    ):
        raise ValueError("import report does not match the score document")
    source = report.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("import report source must be an object")
    source_kind = report.get("source_kind")
    if source_kind not in {"midi", "musicxml"}:
        raise ValueError("import report source_kind must be midi or musicxml")
    if source.get("kind") != source_kind:
        raise ValueError("import report source kind is inconsistent")
    digest = source.get("sha256")
    if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
        raise ValueError("import report source hash is invalid")
    byte_length = source.get("byte_length")
    if (
        isinstance(byte_length, bool)
        or not isinstance(byte_length, int)
        or byte_length < 0
    ):
        raise ValueError("import report source byte_length must be non-negative")
    prefix = "source_midi" if source_kind == "midi" else "source_musicxml"
    if report.get(f"{prefix}_sha256") != digest:
        raise ValueError("import report compatibility source hash is inconsistent")
    if report.get(f"{prefix}_byte_length") != byte_length:
        raise ValueError("import report compatibility source length is inconsistent")
    if report.get("score_canonical_sha256") != score_sha256:
        raise ValueError("import report compatibility score hash is inconsistent")
    draft = _draft_document(bundle.get("roster_draft", {}))
    if _draft_score_sha256(draft) != score_sha256:
        raise ValueError("roster draft does not match the score document")
    draft_source = draft.get("source")
    if (
        not isinstance(draft_source, Mapping)
        or draft_source.get("kind") != source_kind
    ):
        raise ValueError("roster draft source kind is inconsistent")
    draft_input = draft_source.get("input")
    if (
        not isinstance(draft_input, Mapping)
        or draft_input.get("sha256") != digest
        or draft_input.get("byte_length") != byte_length
        or draft_input.get("format") != source.get("format")
    ):
        raise ValueError("roster draft source evidence is inconsistent")
    _baseline_assignments(draft, _score_part_ids(score))


def _creator_assignments(
    assignments: Sequence[Mapping[str, Any]],
    score_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    if isinstance(assignments, (str, bytes)) or not isinstance(assignments, Sequence):
        raise ValueError("assignments must be an array")
    result: dict[str, dict[str, Any]] = {}
    for position, assignment in enumerate(assignments):
        if not isinstance(assignment, Mapping):
            raise ValueError(f"assignments[{position}] must be an object")
        part_id = assignment.get("part")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(f"assignments[{position}].part must be non-empty")
        if part_id in result:
            raise ValueError(f"score part {part_id!r} was assigned more than once")
        result[part_id] = copy.deepcopy(dict(assignment))
    missing = [part_id for part_id in score_ids if part_id not in result]
    extra = sorted(set(result) - set(score_ids))
    if missing:
        raise ValueError(
            "every score part must be assigned exactly once; missing: "
            + ", ".join(missing)
        )
    if extra:
        raise ValueError(
            "assignments reference parts outside the score: " + ", ".join(extra)
        )
    return result


def _validate_explicit_route(
    part_id: str,
    expected_kind: str,
    assignment: Mapping[str, Any],
) -> None:
    has_instrument = "instrument" in assignment
    has_kit = "kit" in assignment
    if has_instrument == has_kit:
        raise ValueError(
            f"part {part_id!r} must explicitly declare exactly one instrument or kit"
        )
    actual_kind = "instrument" if has_instrument else "kit"
    if actual_kind != expected_kind:
        raise ValueError(
            f"part {part_id!r} requires explicit {expected_kind} routing, "
            f"not {actual_kind}"
        )
    if actual_kind == "instrument":
        value = assignment["instrument"]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"part {part_id!r} instrument must be non-empty")
        return
    kit = assignment["kit"]
    if not isinstance(kit, Mapping) or not kit:
        raise ValueError(f"part {part_id!r} kit must be a non-empty object")
    for notehead, reference in kit.items():
        if not isinstance(notehead, str) or not notehead:
            raise ValueError(f"part {part_id!r} kit noteheads must be strings")
        if isinstance(reference, str):
            if not reference.strip():
                raise ValueError(
                    f"part {part_id!r} kit[{notehead!r}] instrument must be non-empty"
                )
        elif isinstance(reference, Mapping):
            instrument = reference.get("instrument")
            if not isinstance(instrument, str) or not instrument.strip():
                raise ValueError(
                    f"part {part_id!r} kit[{notehead!r}].instrument must be non-empty"
                )
        else:
            raise ValueError(
                f"part {part_id!r} kit[{notehead!r}] must name an instrument"
            )


def promote_roster(
    draft: Mapping[str, Any],
    score: dict[str, Any],
    assignments: Sequence[Mapping[str, Any]],
    capabilities: Mapping[str, InstrumentCapability],
    *,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
    name: str | None = None,
    collaboration: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Promote one hash-bound draft after explicit creator routing.

    Every score part must occur exactly once in ``assignments``.  A melodic
    draft placeholder must receive ``instrument``; a percussion placeholder
    must receive a non-empty ``kit``.  No Program Change, track name, candidate
    hint, or catalogue ordering is ever promoted automatically.
    """

    if not isinstance(trusted_only, bool):
        raise ValueError("trusted_only must be boolean")
    if not isinstance(capabilities, Mapping) or not capabilities:
        raise ValueError("capabilities must be a non-empty mapping")
    if trusted_only and trusted_instruments is None:
        raise ValueError(
            "trusted_only requires an explicit trusted_instruments collection"
        )
    score, parsed_score = _score_v1(score)
    score_ids = _score_part_ids(score)
    draft_document = _draft_document(draft)
    if _draft_score_sha256(draft_document) != canonical_json_sha256(score):
        raise ValueError("roster draft does not match the supplied score")
    baseline = _baseline_assignments(draft_document, score_ids)
    creator = _creator_assignments(assignments, score_ids)

    promoted: list[dict[str, Any]] = []
    for part_id in score_ids:
        base = baseline[part_id]
        expected_kind = "instrument" if "instrument" in base else "kit"
        chosen = creator[part_id]
        _validate_explicit_route(part_id, expected_kind, chosen)
        merged = {
            key: copy.deepcopy(value)
            for key, value in base.items()
            if key not in {"instrument", "kit"}
        }
        merged.update(copy.deepcopy(chosen))
        merged["part"] = part_id
        promoted.append(merged)

    raw_draft_roster = draft_document["draft_roster"]
    roster_document: dict[str, Any] = {
        "name": (
            name.strip()
            if isinstance(name, str) and name.strip()
            else f"{score.get('title', '未命名总谱')} 编制"
        ),
        "assignments": promoted,
        "collaboration": copy.deepcopy(
            collaboration
            if collaboration is not None
            else raw_draft_roster.get(
                "collaboration",
                {"mode": "manual", "balance_relations": []},
            )
        ),
    }
    parsed_roster = parse_roster_document(roster_document, dict(capabilities))
    check_roster_covers_score(parsed_roster, parsed_score)
    enforce_roster_availability(
        parsed_roster,
        trusted_only=trusted_only,
        trusted_instruments=trusted_instruments,
    )
    return roster_document


def _capability_candidates(
    capabilities: Mapping[str, InstrumentCapability],
    *,
    percussion: bool,
    low: float | None,
    high: float | None,
    trusted_only: bool,
    trusted_instruments: Collection[str] | None,
) -> list[dict[str, Any]]:
    if not isinstance(trusted_only, bool):
        raise ValueError("trusted_only must be boolean")
    if trusted_only and trusted_instruments is None:
        raise ValueError(
            "trusted_only requires an explicit trusted_instruments collection"
        )
    trusted = None if trusted_instruments is None else frozenset(trusted_instruments)
    seen: set[str] = set()
    rows: list[tuple[int, str, dict[str, Any]]] = []
    for capability in capabilities.values():
        instrument = capability.relative_path
        if instrument in seen:
            continue
        seen.add(instrument)
        if capability.license_status == "quarantined":
            continue
        if capability.implementation_type == "soundfont":
            continue
        if capability.quality_tier is None:
            continue
        if trusted_only and trusted is not None and instrument not in trusted:
            continue
        if percussion:
            if capability.pitched:
                continue
            fit = "not_applicable"
            rank = 0
        else:
            if not capability.pitched:
                continue
            covers = (
                low is not None
                and high is not None
                and capability.note_min is not None
                and capability.note_max is not None
                and capability.note_min <= low
                and high <= capability.note_max
            )
            overlaps = (
                low is not None
                and high is not None
                and capability.note_min is not None
                and capability.note_max is not None
                and capability.note_min <= high
                and low <= capability.note_max
            )
            fit = "covers_score_range" if covers else (
                "partial_overlap" if overlaps else "outside_score_range"
            )
            rank = {"covers_score_range": 0, "partial_overlap": 1}.get(fit, 2)
        rows.append(
            (
                rank,
                instrument,
                {
                    "instrument": instrument,
                    "name": capability.name,
                    "pitched": capability.pitched,
                    "range_fit": fit,
                    "note_min": capability.note_min,
                    "note_max": capability.note_max,
                    "articulations": list(capability.articulations),
                },
            )
        )
    rows.sort(key=lambda row: (row[0], row[1].casefold(), row[1]))
    return [row[2] for row in rows]


def build_routing_hints(
    draft: Mapping[str, Any],
    score: dict[str, Any],
    capabilities: Mapping[str, InstrumentCapability],
    *,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Return a bounded palette, never an executable assignment."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("candidate limit must be an integer")
    if not 1 <= limit <= MAX_CANDIDATES_PER_PART:
        raise ValueError(
            f"candidate limit must be between 1 and {MAX_CANDIDATES_PER_PART}"
        )
    score, parsed_score = _score_v1(score)
    score_ids = _score_part_ids(score)
    draft_document = _draft_document(draft)
    if _draft_score_sha256(draft_document) != canonical_json_sha256(score):
        raise ValueError("roster draft does not match the supplied score")
    baseline = _baseline_assignments(draft_document, score_ids)
    parsed_by_id = {part.id: part for part in parsed_score.parts}
    parts: list[dict[str, Any]] = []
    for part_id in score_ids:
        percussion = "kit" in baseline[part_id]
        pitches = [float(note.midi) for note in parsed_by_id[part_id].notes]
        low = min(pitches) if pitches else None
        high = max(pitches) if pitches else None
        candidates = _capability_candidates(
            capabilities,
            percussion=percussion,
            low=low,
            high=high,
            trusted_only=trusted_only,
            trusted_instruments=trusted_instruments,
        )
        parts.append(
            {
                "part": part_id,
                "routing": "kit" if percussion else "instrument",
                "score_range": {"note_min": low, "note_max": high},
                "candidate_count_returned": min(len(candidates), limit),
                "candidates_truncated": len(candidates) > limit,
                "candidates": candidates[:limit],
                "notice": (
                    "仅为有界调色板提示；不会自动写入正式 roster。"
                ),
            }
        )
    return {
        "status": "non_executable_hints",
        "limit_per_part": limit,
        "trusted_only": trusted_only,
        "parts": parts,
    }


def _json_file_bytes(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _validate_replaceable_import_generation(
    target: Path,
    names: Mapping[str, str],
) -> None:
    """Refuse to move an arbitrary user directory under ``--overwrite``."""

    if target.is_symlink() or not target.is_dir():
        raise ValueError(
            "existing import destination is not a regular Tianlai generation"
        )
    expected = set(names.values())
    actual = {entry.name for entry in target.iterdir()}
    if actual != expected:
        raise ValueError(
            "existing import destination contains files outside the bound "
            "three-document generation; refusing overwrite"
        )
    try:
        existing = {
            key: json.loads(
                (target / filename).read_text(encoding="utf-8")
            )
            for key, filename in names.items()
        }
        validate_import_bundle(
            {
                "format": IMPORT_FORMAT,
                "version": IMPORT_VERSION,
                "score": existing["score"],
                "import_report": existing["import_report"],
                "roster_draft": existing["roster_draft"],
            }
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "existing import destination is not a verified Tianlai "
            "import generation; refusing overwrite"
        ) from exc


def write_import_bundle(
    bundle: Mapping[str, Any],
    destination: str | Path,
    *,
    overwrite: bool = False,
    filenames: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Atomically publish the three import documents as one directory.

    ``destination`` is a dedicated generation directory.  The JSON is fully
    validated and serialized in a private sibling before a single directory
    rename makes all three files visible.  Existing destinations are rejected
    by default.  Explicit replacement keeps the previous complete generation
    in a private backup until the new one has been committed.
    """

    validate_import_bundle(bundle)
    names = dict(_DEFAULT_FILENAMES)
    if filenames is not None:
        if set(filenames) != set(names):
            raise ValueError(
                "filenames must contain score, import_report, and roster_draft"
            )
        names = {key: str(value) for key, value in filenames.items()}
    if len(set(names.values())) != len(names):
        raise ValueError("import bundle filenames must be distinct")
    for label, name in names.items():
        if (
            not name
            or Path(name).name != name
            or name in {".", ".."}
            or "\x00" in name
        ):
            raise ValueError(f"invalid {label} filename: {name!r}")

    payloads = {
        "score": _json_file_bytes(bundle["score"]),
        "import_report": _json_file_bytes(bundle["import_report"]),
        "roster_draft": _json_file_bytes(bundle["roster_draft"]),
    }
    target = Path(destination)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"import destination already exists: {target}")
    if target.exists() and overwrite:
        _validate_replaceable_import_generation(target, names)

    stage = Path(
        tempfile.mkdtemp(
            dir=parent,
            prefix=f".{target.name or 'import'}.import-stage.",
        )
    )
    backup: Path | None = None
    published = False
    try:
        for key in ("score", "import_report", "roster_draft"):
            path = stage / names[key]
            with path.open("xb") as destination_file:
                destination_file.write(payloads[key])
                destination_file.flush()
                os.fsync(destination_file.fileno())

        if target.exists():
            if not overwrite:
                raise FileExistsError(
                    f"import destination already exists: {target}"
                )
            backup = Path(
                tempfile.mkdtemp(
                    dir=parent,
                    prefix=f".{target.name or 'import'}.import-backup.",
                )
            )
            backup.rmdir()
            os.replace(target, backup)
        try:
            os.replace(stage, target)
            published = True
        except Exception:
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
            raise
        if backup is not None and backup.exists():
            _remove_path(backup)
    finally:
        if stage.exists():
            _remove_path(stage)
        # A failed replacement either restored the old generation above or
        # leaves its complete backup for recovery; never delete that evidence.
        if published and backup is not None and backup.exists():
            _remove_path(backup)

    return {
        "directory": str(target),
        "score": str(target / names["score"]),
        "import_report": str(target / names["import_report"]),
        "roster_draft": str(target / names["roster_draft"]),
    }


__all__ = [
    "CANONICALIZATION",
    "DRAFT_FORMAT",
    "IMPORT_FORMAT",
    "IMPORT_VERSION",
    "MAX_CANDIDATES_PER_PART",
    "REPORT_FORMAT",
    "build_routing_hints",
    "canonical_json_sha256",
    "import_midi_project",
    "import_musicxml_project",
    "import_project",
    "promote_roster",
    "validate_import_bundle",
    "write_import_bundle",
]
