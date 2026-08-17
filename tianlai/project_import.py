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
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
from typing import Any
import warnings

from .atomic_publish import _pretty_json_bytes, _rename_noreplace
from .capability import InstrumentCapability
from .canonical_json import CANONICALIZATION, canonical_json_sha256
from .midi_import import (
    build_roster_draft as build_midi_roster_draft,
    read_midi,
)
from .musicxml_import import read_musicxml
from .plain_file import (
    PlainFileIdentity,
    read_plain_file_bytes,
    revalidate_plain_file,
)
from .preflight import enforce_roster_availability
from .render_lock import (
    PlainDirectoryIdentity,
    capture_plain_directory,
    ensure_plain_directory_tree,
    revalidate_plain_directory,
)
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
_MAX_IMPORT_DOCUMENT_BYTES = 64 * 1024 * 1024
_PRIVATE_CLEANUP_ATTEMPTS = 16


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
                    "midi_playback": copy.deepcopy(
                        part.get("midi_playback", [])
                    ),
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
    source_bytes: bytes | None = None,
    capabilities: Mapping[str, InstrumentCapability] | None = None,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
    candidate_limit: int = 8,
) -> dict[str, Any]:
    """Import MIDI without assigning any Tianlai instrument."""

    source_path = Path(path)
    score, parser_report = read_midi(
        source_path,
        source_bytes=source_bytes,
    )
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
    source_bytes: bytes | None = None,
    capabilities: Mapping[str, InstrumentCapability] | None = None,
    trusted_only: bool = False,
    trusted_instruments: Collection[str] | None = None,
    candidate_limit: int = 8,
) -> dict[str, Any]:
    """Import MusicXML and bind the result to the original XML/MXL bytes."""

    source_path = Path(path)
    if source_bytes is None:
        before_sha256, before_size = _source_sha256(source_path)
        score, parser_report = read_musicxml(source_path)
        after_sha256, after_size = _source_sha256(source_path)
        if (before_sha256, before_size) != (after_sha256, after_size):
            raise ValueError("MusicXML source changed while it was being imported")
    else:
        if not isinstance(source_bytes, bytes):
            raise TypeError("source_bytes must be bytes")
        before_sha256 = hashlib.sha256(source_bytes).hexdigest()
        before_size = len(source_bytes)
        score, parser_report = read_musicxml(
            source_path,
            source_bytes=source_bytes,
        )
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
    source_bytes: bytes | None = None,
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
        "source_bytes": source_bytes,
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
            if capability.routing_class != "percussion":
                continue
            fit = "explicit_kit_mapping_required"
            if instrument.startswith("现代鼓组/"):
                rank = 0
            elif not capability.pitched:
                rank = 1
            else:
                rank = 2
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
                    "routing_class": capability.routing_class,
                    "range_fit": fit,
                    "note_min": capability.note_min,
                    "note_max": capability.note_max,
                    "articulations": list(capability.articulations),
                },
            )
        )
    rows.sort(key=lambda row: (row[0], row[1].casefold(), row[1]))
    if percussion:
        # Keep a bounded hint page representative: modern kit pieces,
        # orchestral unpitched percussion, and pitched percussion should all be
        # visible before truncation.  This is discovery order only; it never
        # assigns an instrument automatically.
        buckets = {
            rank: [row for row in rows if row[0] == rank]
            for rank in sorted({row[0] for row in rows})
        }
        diversified: list[tuple[int, str, dict[str, Any]]] = []
        while any(buckets.values()):
            for rank in sorted(buckets):
                if buckets[rank]:
                    diversified.append(buckets[rank].pop(0))
        rows = diversified
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
    return _pretty_json_bytes(document)


def _remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _VerifiedImportGeneration:
    directory: PlainDirectoryIdentity
    files: tuple[tuple[str, PlainFileIdentity, bytes], ...]


def _same_directory_identity(
    left: PlainDirectoryIdentity,
    right: PlainDirectoryIdentity,
) -> bool:
    return left.device == right.device and left.inode == right.inode


_ImportEntryIdentity = tuple[int, int, int]


def _import_entry_identity(path: Path) -> _ImportEntryIdentity:
    value = os.lstat(path)
    return int(value.st_dev), int(value.st_ino), stat.S_IFMT(value.st_mode)


def _directory_entry_identity(
    identity: PlainDirectoryIdentity,
) -> _ImportEntryIdentity:
    return identity.device, identity.inode, stat.S_IFDIR


def _same_file_identity(
    left: PlainFileIdentity,
    right: PlainFileIdentity,
) -> bool:
    return (
        left.device == right.device
        and left.inode == right.inode
        and left.size == right.size
        and left.modified_ns == right.modified_ns
        and left.changed_ns == right.changed_ns
    )


def _same_import_generation(
    left: _VerifiedImportGeneration,
    right: _VerifiedImportGeneration,
) -> bool:
    if not _same_directory_identity(left.directory, right.directory):
        return False
    left_files = {key: (identity, payload) for key, identity, payload in left.files}
    right_files = {
        key: (identity, payload) for key, identity, payload in right.files
    }
    if left_files.keys() != right_files.keys():
        return False
    return all(
        _same_file_identity(left_files[key][0], right_files[key][0])
        and left_files[key][1] == right_files[key][1]
        for key in left_files
    )


def _capture_private_import_directory(
    target: Path,
    entry_names: Collection[str],
    *,
    require_all: bool,
) -> _VerifiedImportGeneration:
    """Bind the exact ordinary files currently inside a private directory."""

    directory_identity = capture_plain_directory(target)
    directory = revalidate_plain_directory(directory_identity)
    allowed = set(entry_names)
    actual = {entry.name for entry in directory.iterdir()}
    if (require_all and actual != allowed) or not actual.issubset(allowed):
        raise OSError("import transaction directory layout changed")
    captured: list[tuple[str, PlainFileIdentity, bytes]] = []
    for name in sorted(actual):
        identity, payload = read_plain_file_bytes(
            directory / name,
            maximum_bytes=_MAX_IMPORT_DOCUMENT_BYTES,
        )
        if not _same_directory_identity(
            identity.parent_identity,
            directory_identity,
        ):
            raise OSError(
                "import document escaped its verified generation directory"
            )
        captured.append((name, identity, payload))
    for _name, identity, _payload in captured:
        revalidate_plain_file(identity)
    revalidate_plain_directory(directory_identity)
    if {entry.name for entry in directory.iterdir()} != actual:
        raise OSError("import transaction changed while it was captured")
    revalidate_plain_directory(directory_identity)
    return _VerifiedImportGeneration(directory_identity, tuple(captured))


def _capture_import_generation(
    target: Path,
    names: Mapping[str, str],
) -> _VerifiedImportGeneration:
    """Read and bind one exact, ordinary three-document generation."""

    expected = set(names.values())
    try:
        snapshot = _capture_private_import_directory(
            target,
            expected,
            require_all=True,
        )
    except OSError as exc:
        raise ValueError(
            "existing import destination contains files outside the bound "
            "three-document generation; refusing overwrite"
        ) from exc
    payload_by_name = {
        name: payload for name, _identity, payload in snapshot.files
    }
    documents = {
        key: json.loads(payload_by_name[names[key]].decode("utf-8"))
        for key in ("score", "import_report", "roster_draft")
    }

    validate_import_bundle(
        {
            "format": IMPORT_FORMAT,
            "version": IMPORT_VERSION,
            "score": documents["score"],
            "import_report": documents["import_report"],
            "roster_draft": documents["roster_draft"],
        }
    )
    for _name, identity, _payload in snapshot.files:
        revalidate_plain_file(identity)
    revalidate_plain_directory(snapshot.directory)
    if {
        entry.name for entry in snapshot.directory.path.iterdir()
    } != expected:
        raise OSError("import generation changed while it was being verified")
    revalidate_plain_directory(snapshot.directory)
    return snapshot


def _require_unchanged_import_generation(
    expected: _VerifiedImportGeneration,
    path: Path,
    names: Mapping[str, str],
    *,
    message: str,
) -> _VerifiedImportGeneration:
    try:
        current = _capture_import_generation(path, names)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(message) from exc
    if not _same_import_generation(expected, current):
        raise RuntimeError(message)
    return current


def _private_sibling_path(parent: Path, prefix: str) -> Path:
    for _ in range(_PRIVATE_CLEANUP_ATTEMPTS):
        candidate = parent / f"{prefix}{secrets.token_hex(16)}"
        if not os.path.lexists(candidate):
            return candidate
    raise RuntimeError("could not allocate a private import transaction path")


def _require_unchanged_private_directory(
    expected: _VerifiedImportGeneration,
    path: Path,
    *,
    message: str,
) -> _VerifiedImportGeneration:
    try:
        current = _capture_private_import_directory(
            path,
            {name for name, _identity, _payload in expected.files},
            require_all=True,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RuntimeError(message) from exc
    if not _same_import_generation(expected, current):
        raise RuntimeError(message)
    return current


def _cleanup_verified_import_generation(
    generation: _VerifiedImportGeneration,
    *,
    parent_identity: PlainDirectoryIdentity,
    prefix: str,
) -> None:
    """Remove only the exact private generation captured by this call.

    The directory is first moved into an unpredictable mode-0700 quarantine.
    A replacement installed at the source just before that rename is detected
    after the move and retained rather than recursively deleted.  Recursive
    removal begins only after the moved identity is rebound inside that private
    directory.  The remaining same-user adversarial window after this final
    check is the documented boundary on platforms without descriptor-relative
    tree removal (notably Windows); ordinary writers cannot predict or enter
    the quarantine before removal.
    """

    parent = revalidate_plain_directory(parent_identity)
    path = generation.directory.path
    if not os.path.lexists(path):
        return
    if (
        path.parent != parent
        or path != parent / path.name
        or not path.name.startswith(prefix)
    ):
        raise RuntimeError("refusing to clean an unowned import transaction path")
    _require_unchanged_private_directory(
        generation,
        path,
        message="import transaction identity changed before cleanup",
    )
    quarantine_root = Path(
        tempfile.mkdtemp(
            dir=parent,
            prefix=".tianlai-import-cleanup.",
        )
    )
    moved_to_quarantine = False
    try:
        quarantine_root_identity = capture_plain_directory(quarantine_root)
        revalidate_plain_directory(parent_identity)
        quarantine = quarantine_root / "generation"
        revalidate_plain_directory(parent_identity)
        _rename_noreplace(path, quarantine)
        moved_to_quarantine = True
        moved = _require_unchanged_private_directory(
            generation,
            quarantine,
            message=(
                "import transaction identity changed during cleanup; the "
                f"replacement was preserved at {quarantine}"
            ),
        )
        revalidate_plain_directory(parent_identity)
        revalidate_plain_directory(quarantine_root_identity)
        revalidate_plain_directory(moved.directory)
        _remove_path(quarantine_root)
    except BaseException:
        if not moved_to_quarantine:
            try:
                if not any(quarantine_root.iterdir()):
                    quarantine_root.rmdir()
            except BaseException:
                pass
        raise


def _report_cleanup_failure(
    primary_error: BaseException | None,
    label: str,
    cleanup_error: BaseException,
) -> None:
    message = f"{label} cleanup was not completed: {cleanup_error}"
    if primary_error is not None:
        try:
            primary_error.add_note(message)
        except BaseException:
            pass
        return
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except BaseException:
        # Warning filters must not turn post-commit cleanup into failure.
        pass


def _validate_replaceable_import_generation(
    target: Path,
    names: Mapping[str, str],
) -> _VerifiedImportGeneration:
    """Refuse to move an arbitrary user directory under ``--overwrite``."""

    try:
        return _capture_import_generation(target, names)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(
            "existing import destination is not a verified Tianlai "
            "import generation; refusing overwrite"
        ) from exc


def _withdraw_failed_stage_generation(
    *,
    target: Path,
    stage_identity: PlainDirectoryIdentity,
    parent_identity: PlainDirectoryIdentity,
) -> tuple[Path | None, str | None]:
    """Withdraw a failed publication using directory identity alone.

    A post-publish content check can fail precisely because a file inside the
    moved stage was modified.  Requiring the full generation snapshot again
    would strand those failed bytes at the public target.  Directory identity
    is sufficient to prove ownership of the entry being withdrawn; its
    untrusted contents are retained at the returned recovery path and are
    deliberately never passed to normal cleanup.
    """

    parent = revalidate_plain_directory(parent_identity)
    original_stage = stage_identity.path
    recovery_prefix = f".{target.name or 'import'}.import-recovery."

    def restore_concurrent_entry(candidate: Path, reason: str) -> str:
        if os.path.lexists(target):
            return f"{reason}; concurrent entry retained at {candidate}"
        try:
            revalidate_plain_directory(parent_identity)
            _rename_noreplace(candidate, target)
        except BaseException as restore_error:
            return (
                f"{reason}; concurrent entry retained at {candidate}; "
                f"restore error: {restore_error}"
            )
        return f"{reason}; concurrent entry restored to {target}"

    candidate = original_stage
    for _ in range(_PRIVATE_CLEANUP_ATTEMPTS + 1):
        try:
            observed_target = capture_plain_directory(target)
        except BaseException as exc:
            return None, f"failed generation could not be rebound: {exc}"
        if not _same_directory_identity(stage_identity, observed_target):
            return None, (
                "publication target no longer names the moved staging "
                "directory; the concurrent entry was left untouched"
            )
        if os.path.lexists(candidate):
            candidate = _private_sibling_path(parent, recovery_prefix)
        revalidate_plain_directory(parent_identity)
        try:
            _rename_noreplace(target, candidate)
        except FileExistsError:
            candidate = _private_sibling_path(parent, recovery_prefix)
            continue
        except BaseException as exc:
            # A fault-injection seam may report failure after the native move
            # completed.  Rebind the candidate before deciding it was lost.
            try:
                moved_after_error = capture_plain_directory(candidate)
            except BaseException:
                return None, f"failed generation withdrawal failed: {exc}"
            if _same_directory_identity(stage_identity, moved_after_error):
                return candidate, (
                    "withdrawal reported an error after moving the failed "
                    f"generation: {exc}"
                )
            return None, restore_concurrent_entry(
                candidate,
                "withdrawal moved a concurrent destination entry after "
                f"reporting an error: {exc}",
            )
        try:
            moved_identity = capture_plain_directory(candidate)
            revalidate_plain_directory(parent_identity)
        except BaseException as exc:
            return candidate, (
                "failed generation was withdrawn but its recovery identity "
                f"could not be rebound: {exc}"
            )
        if not _same_directory_identity(stage_identity, moved_identity):
            return None, restore_concurrent_entry(
                candidate,
                "withdrawal moved a concurrent destination entry",
            )
        return candidate, None
    return None, "could not allocate an exclusive failed-generation recovery path"


def _restore_import_directory_entry(
    source: Path,
    target: Path,
    identity: _ImportEntryIdentity,
) -> tuple[bool, str | None]:
    """Restore one moved directory and recognize move-then-error results."""

    if os.path.lexists(target):
        return False, f"restore target is occupied: {target}"
    restore_error: BaseException | None = None
    try:
        _rename_noreplace(source, target)
    except BaseException as exc:
        restore_error = exc
    try:
        restored = _import_entry_identity(target)
    except BaseException as inspect_error:
        return False, (
            f"restored directory could not be rebound: {inspect_error}; "
            f"rename error: {restore_error}"
        )
    if (
        identity == restored
        and not os.path.lexists(source)
    ):
        return True, str(restore_error) if restore_error is not None else None
    return False, (
        "restored directory identity changed; "
        f"rename error: {restore_error}"
    )


def _isolate_existing_import_generation(
    *,
    target: Path,
    backup: Path,
    expected: _VerifiedImportGeneration,
    names: Mapping[str, str],
) -> _VerifiedImportGeneration:
    """Move the expected old generation without hiding a source-swap racer."""

    move_error: BaseException | None = None
    try:
        _rename_noreplace(target, backup)
    except BaseException as exc:
        move_error = exc

    try:
        moved_identity = _import_entry_identity(backup)
    except BaseException as inspect_error:
        if not os.path.lexists(target) and os.path.lexists(backup):
            restore_error: BaseException | None = None
            try:
                _rename_noreplace(backup, target)
            except BaseException as exc:
                restore_error = exc
            if os.path.lexists(target) and not os.path.lexists(backup):
                primary = move_error or inspect_error
                try:
                    primary.add_note(
                        "unverified import backup entry was conservatively "
                        f"restored to {target}"
                    )
                except BaseException:
                    pass
                raise primary
            primary = move_error or inspect_error
            try:
                primary.add_note(
                    f"unverified import backup retained at {backup}; "
                    f"restore error: {restore_error}"
                )
            except BaseException:
                pass
            raise primary
        if move_error is not None:
            try:
                move_error.add_note(
                    f"unverified import backup retained at {backup}: "
                    f"{inspect_error}"
                )
            except BaseException:
                pass
            raise move_error
        raise RuntimeError(
            "existing import generation could not be inspected after backup"
        ) from inspect_error

    target_identity: _ImportEntryIdentity | None = None
    if os.path.lexists(target):
        try:
            target_identity = _import_entry_identity(target)
        except BaseException:
            target_identity = None
    target_occupied = target_identity is not None or os.path.lexists(target)
    if (
        move_error is not None
        and target_identity is not None
        and _directory_entry_identity(expected.directory) == target_identity
    ):
        # The exclusive destination was occupied before any move; the expected
        # public generation never left its path.
        raise move_error
    if _directory_entry_identity(expected.directory) == moved_identity:
        if target_occupied:
            failure = RuntimeError(
                "import destination was occupied after its previous generation "
                "was isolated"
            )
            try:
                failure.add_note(
                    f"previous import generation retained at {backup}"
                )
            except BaseException:
                pass
            raise failure from move_error
        if move_error is not None:
            restored, restore_note = _restore_import_directory_entry(
                backup,
                target,
                moved_identity,
            )
            if restored:
                _require_unchanged_import_generation(
                    expected,
                    target,
                    names,
                    message=(
                        "previous import generation changed while recovering "
                        "from a reported backup error"
                    ),
                )
                if restore_note is not None:
                    try:
                        move_error.add_note(
                            "previous import generation was restored despite "
                            f"a recovery rename diagnostic: {restore_note}"
                        )
                    except BaseException:
                        pass
                raise move_error
            try:
                move_error.add_note(
                    "previous import generation could not be restored; "
                    f"retained at {backup}: {restore_note}"
                )
            except BaseException:
                pass
            raise move_error
        try:
            return _require_unchanged_import_generation(
                expected,
                backup,
                names,
                message="existing import generation changed during backup",
            )
        except BaseException as verification_error:
            try:
                current_backup_identity = _import_entry_identity(backup)
            except BaseException as identity_error:
                failure = RuntimeError(
                    "changed import backup could not be rebound for recovery"
                )
                try:
                    failure.add_note(
                        f"unverified changed import entry retained at {backup}: "
                        f"{identity_error}"
                    )
                except BaseException:
                    pass
                raise failure from verification_error
            restored, restore_note = _restore_import_directory_entry(
                backup,
                target,
                current_backup_identity,
            )
            failure = RuntimeError(
                "existing import generation changed during backup"
            )
            if restored:
                try:
                    failure.add_note(
                        f"changed import destination was restored to {target}"
                    )
                except BaseException:
                    pass
            else:
                try:
                    failure.add_note(
                        f"changed import destination retained at {backup}: "
                        f"{restore_note}"
                    )
                except BaseException:
                    pass
            raise failure from verification_error

    failure = RuntimeError(
        "existing import destination was replaced concurrently during backup"
    )
    if not target_occupied:
        restored, restore_note = _restore_import_directory_entry(
            backup,
            target,
            moved_identity,
        )
        if restored:
            try:
                failure.add_note(
                    f"concurrent import destination was restored to {target}"
                )
            except BaseException:
                pass
            raise failure from move_error
        try:
            failure.add_note(
                f"concurrent import destination retained at {backup}: "
                f"{restore_note}"
            )
        except BaseException:
            pass
        raise failure from move_error
    try:
        failure.add_note(
            "concurrent import destination backup retained at "
            f"{backup}; public path is occupied and was not overwritten"
        )
    except BaseException:
        pass
    raise failure from move_error


def _rollback_import_replacement(
    *,
    target: Path,
    stage: _VerifiedImportGeneration,
    backup: _VerifiedImportGeneration,
    names: Mapping[str, str],
    publish_error: BaseException,
) -> None:
    """Restore an exact old generation without overwriting a racer."""

    try:
        if os.path.lexists(target):
            stage_path = stage.directory.path
            if os.path.lexists(stage_path):
                raise RuntimeError(
                    "import staging path was occupied during rollback"
                )
            current_identity = capture_plain_directory(target)
            if not _same_directory_identity(
                stage.directory,
                current_identity,
            ):
                raise RuntimeError(
                    "import publish failed after another writer occupied "
                    "the destination"
                )
            move_error: BaseException | None = None
            try:
                _rename_noreplace(target, stage_path)
            except BaseException as exc:
                move_error = exc
            moved_entry_identity = _import_entry_identity(stage_path)
            if (
                _directory_entry_identity(stage.directory)
                != moved_entry_identity
            ):
                restored, restore_note = _restore_import_directory_entry(
                    stage_path,
                    target,
                    moved_entry_identity,
                )
                if restored:
                    raise RuntimeError(
                        "new import generation was replaced during rollback; "
                        "the racing destination was restored"
                    ) from move_error
                raise RuntimeError(
                    "new import generation was replaced during rollback; "
                    f"the racing destination remains at {stage_path}: "
                    f"{restore_note}"
                ) from move_error
            if move_error is not None:
                # The expected new generation moved successfully.  Preserve it
                # at stage_path and continue restoring the old backup; the
                # original publication error remains the public exception.
                try:
                    publish_error.add_note(
                        "new generation withdrawal reported an error after "
                        f"moving it to {stage_path}: {move_error}"
                    )
                except BaseException:
                    pass
        if os.path.lexists(target):
            raise RuntimeError(
                "import destination remained occupied during rollback"
            )
        _require_unchanged_import_generation(
            backup,
            backup.directory.path,
            names,
            message="previous import generation changed before rollback",
        )
        try:
            _rename_noreplace(backup.directory.path, target)
        except BaseException as restore_error:
            try:
                _require_unchanged_import_generation(
                    backup,
                    target,
                    names,
                    message=(
                        "previous import generation was not restored after "
                        "the rollback rename reported an error"
                    ),
                )
            except BaseException:
                raise restore_error
            # The native move completed and restored the exact old generation;
            # do not attach a false incomplete-rollback note to the primary
            # publication error merely because a wrapper reported failure.
            return
        _require_unchanged_import_generation(
            backup,
            target,
            names,
            message="previous import generation changed during rollback",
        )
    except BaseException as rollback_error:
        message = (
            "import publication failed and automatic rollback was incomplete; "
            "the expected previous-generation backup path is "
            f"{backup.directory.path}; "
            f"rollback error: {rollback_error}"
        )
        try:
            publish_error.add_note(message)
        except BaseException:
            pass


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
    requested_target = Path(destination)
    requested_parent = requested_target.parent
    parent_identity = ensure_plain_directory_tree(requested_parent)
    parent = revalidate_plain_directory(parent_identity)
    target = parent / requested_target.name
    existing_generation: _VerifiedImportGeneration | None = None
    if os.path.lexists(target) and not overwrite:
        raise FileExistsError(f"import destination already exists: {target}")
    if os.path.lexists(target) and overwrite:
        existing_generation = _validate_replaceable_import_generation(
            target,
            names,
        )

    stage = Path(
        tempfile.mkdtemp(
            dir=parent,
            prefix=f".{target.name or 'import'}.import-stage.",
        )
    )
    stage_identity: PlainDirectoryIdentity | None = None
    stage_generation: _VerifiedImportGeneration | None = None
    backup: Path | None = None
    backup_generation: _VerifiedImportGeneration | None = None
    published = False
    stage_moved = False
    failed_stage_recovery: Path | None = None
    primary_error: BaseException | None = None
    try:
        stage_identity = capture_plain_directory(stage)
        revalidate_plain_directory(parent_identity)
        for key in ("score", "import_report", "roster_draft"):
            path = stage / names[key]
            with path.open("xb") as destination_file:
                destination_file.write(payloads[key])
                destination_file.flush()
                os.fsync(destination_file.fileno())
        observed_stage_generation = _capture_import_generation(stage, names)
        if not _same_directory_identity(
            observed_stage_generation.directory,
            stage_identity,
        ):
            raise RuntimeError("import staging directory identity changed")
        stage_generation = observed_stage_generation

        if os.path.lexists(target):
            if not overwrite:
                raise FileExistsError(
                    f"import destination already exists: {target}"
                )
            if existing_generation is None:
                raise RuntimeError(
                    "import destination appeared while publication was staged"
                )
            backup = _private_sibling_path(
                parent,
                f".{target.name or 'import'}.import-backup.",
            )
            _require_unchanged_import_generation(
                stage_generation,
                stage,
                names,
                message="import staging generation changed before publication",
            )
            revalidate_plain_directory(parent_identity)
            # This is intentionally the last target-path operation before the
            # rename.  A replacement in the remaining kernel boundary is
            # detected by the identity check at ``backup`` below.
            existing_generation = _require_unchanged_import_generation(
                existing_generation,
                target,
                names,
                message=(
                    "existing import generation changed before replacement"
                ),
            )
            # Never assume that the pathname moved by the exclusive rename is
            # the generation checked immediately above.  The helper restores
            # an actual source-swap racer to the public path when it is vacant.
            backup_generation = _isolate_existing_import_generation(
                target=target,
                backup=backup,
                expected=existing_generation,
                names=names,
            )
        elif existing_generation is not None:
            raise RuntimeError(
                "existing import destination disappeared before replacement"
            )

        if os.path.lexists(target):
            raise RuntimeError(
                "import destination was occupied before publication"
            )
        stage_generation = _require_unchanged_import_generation(
            stage_generation,
            stage,
            names,
            message="import staging generation changed before publication",
        )
        revalidate_plain_directory(parent_identity)
        try:
            _rename_noreplace(stage, target)
        except BaseException:
            # A native/seam error can be reported after the move completed.
            # Detect ownership by directory identity so the outer handler can
            # still withdraw a failed first publication.
            try:
                moved_after_error = capture_plain_directory(target)
                stage_moved = _same_directory_identity(
                    stage_generation.directory,
                    moved_after_error,
                )
            except BaseException:
                pass
            raise
        else:
            stage_moved = True
        _require_unchanged_import_generation(
            stage_generation,
            target,
            names,
            message=(
                "import staging generation was replaced concurrently during "
                "publication"
            ),
        )
        published = True
    except BaseException as exc:
        primary_error = exc
        rollback_stage = stage_generation
        if stage_moved:
            failed_stage_recovery, withdrawal_note = (
                _withdraw_failed_stage_generation(
                target=target,
                stage_identity=stage_generation.directory,
                parent_identity=parent_identity,
            )
            )
            if withdrawal_note is not None:
                try:
                    exc.add_note(withdrawal_note)
                except BaseException:
                    pass
            if failed_stage_recovery is not None:
                try:
                    exc.add_note(
                        "failed import generation was retained for recovery at "
                        f"{failed_stage_recovery}"
                    )
                except BaseException:
                    pass
                # It may have failed content verification, so it must never be
                # processed by the normal complete-generation cleanup path.
                stage_generation = None
        if backup_generation is not None and not published:
            _rollback_import_replacement(
                target=target,
                stage=rollback_stage,
                backup=backup_generation,
                names=names,
                publish_error=exc,
            )
        raise
    finally:
        if stage_identity is None and os.path.lexists(stage):
            _report_cleanup_failure(
                primary_error,
                "unverified import staging directory",
                RuntimeError("directory identity was not captured; retained"),
            )
        if (
            failed_stage_recovery is None
            and
            stage_generation is None
            and stage_identity is not None
            and os.path.lexists(stage)
        ):
            try:
                partial_stage = _capture_private_import_directory(
                    stage,
                    set(names.values()),
                    require_all=False,
                )
                if not _same_directory_identity(
                    partial_stage.directory,
                    stage_identity,
                ):
                    raise RuntimeError(
                        "import staging directory was replaced before cleanup"
                    )
                stage_generation = partial_stage
            except BaseException as cleanup_error:
                _report_cleanup_failure(
                    primary_error,
                    "partial import staging directory",
                    cleanup_error,
                )
        if failed_stage_recovery is None and stage_generation is not None:
            try:
                _cleanup_verified_import_generation(
                    stage_generation,
                    parent_identity=parent_identity,
                    prefix=f".{target.name or 'import'}.import-stage.",
                )
            except BaseException as cleanup_error:
                _report_cleanup_failure(
                    primary_error,
                    "import staging directory",
                    cleanup_error,
                )
        # A failed replacement either restored the old generation above or
        # leaves its complete backup for recovery; never delete that evidence.
        if published and backup_generation is not None:
            try:
                _cleanup_verified_import_generation(
                    backup_generation,
                    parent_identity=parent_identity,
                    prefix=f".{target.name or 'import'}.import-backup.",
                )
            except BaseException as cleanup_error:
                _report_cleanup_failure(
                    primary_error,
                    "previous import generation",
                    cleanup_error,
                )

    return {
        "directory": str(requested_target),
        "score": str(requested_target / names["score"]),
        "import_report": str(requested_target / names["import_report"]),
        "roster_draft": str(requested_target / names["roster_draft"]),
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
