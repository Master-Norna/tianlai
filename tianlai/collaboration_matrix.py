"""Strict, deterministic evidence matrix for ensemble acceptance.

This module deliberately sits outside the renderer and the instrument
manifests.  It aggregates evidence that was produced elsewhere; it never
renders audio and never writes an acceptance result back to an instrument.

``fixture.instruments`` lists the evaluated targets, not necessarily every
performer in the bound roster.  The roster hash binds the complete ensemble.
``machine_complete`` is therefore a result of the assertions attributed to one
target, not a claim that the instrument has passed ensemble listening review.
"""

from __future__ import annotations

import copy
import json
import math
from numbers import Integral, Real
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Iterable, Mapping
import unicodedata

from .canonical_json import canonical_json_bytes as _project_canonical_json_bytes


COLLABORATION_MATRIX_FORMAT = "tianlai.collaboration_matrix"
COLLABORATION_MATRIX_VERSION = 1
COLLABORATION_MATRIX_NOTICE = (
    "machine_complete 仅表示所有归属于该乐器的引用 fixture 机器断言"
    "均已执行且未失败；"
    "它不等于协奏通过，矩阵也不会写回乐器 manifest；"
    "最终结论仍须人工语境听审。"
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ISO_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:[Zz]|[+-]\d{2}:\d{2})?"
)
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
_ROLE_PROMINENCE = frozenset({"foreground", "midground", "background"})
_ASSERTION_STATUSES = frozenset({"pass", "fail", "inconclusive"})
_CANDIDATE_SEVERITIES = frozenset({"info", "warning"})
_HUMAN_STATUSES = frozenset({"pending", "pass", "reject", "conflict"})
_HARD_STATUSES = frozenset(
    {"not_covered", "machine_complete", "machine_failed", "inconclusive"}
)
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)


class CollaborationMatrixError(ValueError):
    """A collaboration-matrix document violates its evidence contract."""


def _reject_nonfinite_tree(value: Any, label: str = "document") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CollaborationMatrixError(
                    f"{label} contains a non-string key"
                )
            _reject_nonfinite_tree(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite_tree(item, f"{label}[{index}]")
        return
    if isinstance(value, Integral) and not isinstance(value, bool):
        return
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            finite = math.isfinite(float(value))
        except OverflowError:
            finite = False
        if not finite:
            raise CollaborationMatrixError(f"{label} must be finite")


def _require_json_value(value: Any, label: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CollaborationMatrixError(f"{label} must be finite")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_value(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CollaborationMatrixError(
                    f"{label} contains a non-string key"
                )
            _require_json_value(item, f"{label}.{key}")
        return
    raise CollaborationMatrixError(f"{label} must be a JSON value")


def canonical_json_bytes(document: Any) -> bytes:
    """Return stable UTF-8 JSON bytes for hashing or byte comparisons."""

    _reject_nonfinite_tree(document)
    try:
        return _project_canonical_json_bytes(document)
    except (TypeError, ValueError) as error:
        raise CollaborationMatrixError(
            f"document is not canonical JSON: {error}"
        ) from error


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CollaborationMatrixError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise CollaborationMatrixError(
        f"non-finite JSON constant is forbidden: {value}"
    )


def _expect_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CollaborationMatrixError(f"{label} must be an object")
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required - optional)
    if missing:
        raise CollaborationMatrixError(
            f"{label} is missing fields: {', '.join(missing)}"
        )
    if extra:
        raise CollaborationMatrixError(
            f"{label} has unknown fields: {', '.join(extra)}"
        )
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CollaborationMatrixError(f"{label} must be a non-empty string")
    return value


def _require_identifier(value: Any, label: str) -> str:
    text = _require_string(value, label)
    if not _IDENTIFIER.fullmatch(text):
        raise CollaborationMatrixError(
            f"{label} must use only identifier-safe characters"
        )
    return text


def _require_integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CollaborationMatrixError(f"{label} must be an integer")
    if value < minimum:
        raise CollaborationMatrixError(
            f"{label} must be {minimum} or greater"
        )
    return value


def _require_sha256(value: Any, label: str) -> str:
    text = _require_string(value, label)
    if not _HEX64.fullmatch(text):
        raise CollaborationMatrixError(
            f"{label} must be a lowercase SHA-256 digest"
        )
    return text


def _require_relative_path(value: Any, label: str) -> str:
    text = _require_string(value, label)
    if (
        "\\" in text
        or text != text.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or any(character in '<>:"|?*' for character in text)
    ):
        raise CollaborationMatrixError(
            f"{label} must be a portable forward-slash relative path"
        )
    path = PurePosixPath(text)
    if (
        path.is_absolute()
        or text != path.as_posix()
        or text != unicodedata.normalize("NFC", text)
        or text in {".", ".."}
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.match(r"^[A-Za-z]:", text)
    ):
        raise CollaborationMatrixError(
            f"{label} must be a safe relative path"
        )
    for part in path.parts:
        if part.endswith((" ", ".")):
            raise CollaborationMatrixError(
                f"{label} has a path segment with a non-portable ending"
            )
        reserved_basename = part.partition(".")[0].rstrip(" ").casefold()
        if reserved_basename in _WINDOWS_RESERVED_BASENAMES:
            raise CollaborationMatrixError(
                f"{label} uses a Windows-reserved path segment"
            )
    return text


def _require_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise CollaborationMatrixError(f"{label} must be an array")
    return value


def _require_enum(
    value: Any,
    allowed: frozenset[str],
    label: str,
) -> str:
    text = _require_string(value, label)
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise CollaborationMatrixError(
            f"{label} must be one of: {choices}"
        )
    return text


def _reject_duplicates(values: Iterable[str], label: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise CollaborationMatrixError(f"{label} contains duplicate {value!r}")
        seen.add(value)


def _reject_duplicate_paths(values: Iterable[str], label: str) -> None:
    seen: dict[str, str] = {}
    for value in values:
        key = unicodedata.normalize("NFC", value).casefold()
        previous = seen.get(key)
        if previous is not None:
            raise CollaborationMatrixError(
                f"{label} contains duplicate-equivalent paths "
                f"{previous!r} and {value!r}"
            )
        seen[key] = value


def _validate_role(value: Any, label: str) -> None:
    role = _expect_keys(
        value,
        required={"function", "prominence"},
        optional={"label"},
        label=label,
    )
    _require_enum(role["function"], _ROLE_FUNCTIONS, f"{label}.function")
    _require_enum(
        role["prominence"],
        _ROLE_PROMINENCE,
        f"{label}.prominence",
    )
    if "label" in role:
        _require_string(role["label"], f"{label}.label")


def _validate_assertion(value: Any, label: str) -> None:
    assertion = _expect_keys(
        value,
        required={
            "code",
            "instrument_paths",
            "status",
            "observed",
            "expected",
            "tolerance",
            "unit",
            "evidence_path",
        },
        label=label,
    )
    _require_identifier(assertion["code"], f"{label}.code")
    _validate_attribution(
        assertion["instrument_paths"],
        f"{label}.instrument_paths",
    )
    _require_enum(
        assertion["status"],
        _ASSERTION_STATUSES,
        f"{label}.status",
    )
    if assertion["status"] == "inconclusive":
        if assertion["observed"] is not None:
            raise CollaborationMatrixError(
                f"{label}.observed must be null when status is inconclusive"
            )
    elif assertion["observed"] is None:
        raise CollaborationMatrixError(
            f"{label}.observed must not be null for pass or fail"
        )
    _require_json_value(assertion["observed"], f"{label}.observed")
    _require_json_value(assertion["expected"], f"{label}.expected")
    tolerance = assertion["tolerance"]
    if tolerance is not None:
        if isinstance(tolerance, bool) or not isinstance(tolerance, Real):
            raise CollaborationMatrixError(
                f"{label}.tolerance must be a finite non-negative number or null"
            )
        try:
            finite = (
                True
                if isinstance(tolerance, Integral)
                else math.isfinite(float(tolerance))
            )
        except OverflowError:
            finite = False
        if not finite or tolerance < 0:
            raise CollaborationMatrixError(
                f"{label}.tolerance must be a finite non-negative number or null"
            )
    _require_string(assertion["unit"], f"{label}.unit")
    _require_relative_path(
        assertion["evidence_path"],
        f"{label}.evidence_path",
    )


def _validate_candidate(value: Any, label: str) -> None:
    candidate = _expect_keys(
        value,
        required={
            "code",
            "instrument_paths",
            "severity",
            "info",
            "evidence_path",
        },
        label=label,
    )
    _require_identifier(candidate["code"], f"{label}.code")
    _validate_attribution(
        candidate["instrument_paths"],
        f"{label}.instrument_paths",
    )
    _require_enum(
        candidate["severity"],
        _CANDIDATE_SEVERITIES,
        f"{label}.severity",
    )
    _require_string(candidate["info"], f"{label}.info")
    _require_relative_path(
        candidate["evidence_path"],
        f"{label}.evidence_path",
    )


def _validate_human_check(value: Any, label: str) -> None:
    check = _expect_keys(
        value,
        required={"code", "instrument_paths", "status", "evidence_path"},
        label=label,
    )
    _require_identifier(check["code"], f"{label}.code")
    _validate_attribution(
        check["instrument_paths"],
        f"{label}.instrument_paths",
    )
    status = _require_enum(
        check["status"],
        _HUMAN_STATUSES,
        f"{label}.status",
    )
    evidence_path = check["evidence_path"]
    if evidence_path is None:
        if status != "pending":
            raise CollaborationMatrixError(
                f"{label}.evidence_path may be null only while pending"
            )
    else:
        _require_relative_path(evidence_path, f"{label}.evidence_path")


def _validate_attribution(value: Any, label: str) -> list[str]:
    paths = _require_list(value, label)
    if not paths:
        raise CollaborationMatrixError(
            f"{label} must contain at least one instrument path"
        )
    validated = [
        _require_relative_path(path, f"{label}[{index}]")
        for index, path in enumerate(paths)
    ]
    _reject_duplicate_paths(validated, label)
    return validated


def _validate_fixture(value: Any, index: int) -> None:
    label = f"fixtures[{index}]"
    fixture = _expect_keys(
        value,
        required={
            "fixture_id",
            "profile_version",
            "score_sha256",
            "roster_sha256",
            "space_sha256",
            "receipt_sha256",
            "instruments",
            "roles",
            "assertions",
            "candidates",
            "human_checks",
        },
        label=label,
    )
    _require_identifier(fixture["fixture_id"], f"{label}.fixture_id")
    _require_integer(
        fixture["profile_version"],
        f"{label}.profile_version",
        minimum=1,
    )
    _require_sha256(fixture["score_sha256"], f"{label}.score_sha256")
    _require_sha256(fixture["roster_sha256"], f"{label}.roster_sha256")
    if fixture["space_sha256"] is not None:
        _require_sha256(fixture["space_sha256"], f"{label}.space_sha256")
    _require_sha256(fixture["receipt_sha256"], f"{label}.receipt_sha256")

    instruments = _require_list(fixture["instruments"], f"{label}.instruments")
    if not instruments:
        raise CollaborationMatrixError(
            f"{label}.instruments must contain at least one path"
        )
    instrument_paths = [
        _require_relative_path(path, f"{label}.instruments[{path_index}]")
        for path_index, path in enumerate(instruments)
    ]
    _reject_duplicate_paths(instrument_paths, f"{label}.instruments")

    roles = _require_list(fixture["roles"], f"{label}.roles")
    role_paths: list[str] = []
    for role_index, value in enumerate(roles):
        role_label = f"{label}.roles[{role_index}]"
        entry = _expect_keys(
            value,
            required={"instrument_path", "role"},
            label=role_label,
        )
        role_paths.append(
            _require_relative_path(
                entry["instrument_path"],
                f"{role_label}.instrument_path",
            )
        )
        _validate_role(entry["role"], f"{role_label}.role")
    _reject_duplicate_paths(role_paths, f"{label}.roles instrument paths")
    if set(role_paths) != set(instrument_paths):
        raise CollaborationMatrixError(
            f"{label}.roles must cover exactly the fixture instruments"
        )

    assertions = _require_list(fixture["assertions"], f"{label}.assertions")
    if not assertions:
        raise CollaborationMatrixError(
            f"{label}.assertions must contain at least one assertion"
        )
    for assertion_index, assertion in enumerate(assertions):
        _validate_assertion(
            assertion,
            f"{label}.assertions[{assertion_index}]",
        )
    _reject_duplicates(
        (assertion["code"] for assertion in assertions),
        f"{label}.assertion codes",
    )
    fixture_path_set = set(instrument_paths)
    for assertion_index, assertion in enumerate(assertions):
        attributed = set(assertion["instrument_paths"])
        if not attributed <= fixture_path_set:
            raise CollaborationMatrixError(
                f"{label}.assertions[{assertion_index}].instrument_paths "
                "must be a subset of fixture instruments"
            )
    uncovered = sorted(
        fixture_path_set
        - {
            instrument_path
            for assertion in assertions
            for instrument_path in assertion["instrument_paths"]
        }
    )
    if uncovered:
        raise CollaborationMatrixError(
            f"{label}.assertions do not cover fixture instruments: "
            f"{', '.join(uncovered)}"
        )

    candidates = _require_list(fixture["candidates"], f"{label}.candidates")
    for candidate_index, candidate in enumerate(candidates):
        _validate_candidate(
            candidate,
            f"{label}.candidates[{candidate_index}]",
        )
    _reject_duplicates(
        (candidate["code"] for candidate in candidates),
        f"{label}.candidate codes",
    )
    for candidate_index, candidate in enumerate(candidates):
        if not set(candidate["instrument_paths"]) <= fixture_path_set:
            raise CollaborationMatrixError(
                f"{label}.candidates[{candidate_index}].instrument_paths "
                "must be a subset of fixture instruments"
            )

    human_checks = _require_list(
        fixture["human_checks"],
        f"{label}.human_checks",
    )
    for check_index, check in enumerate(human_checks):
        _validate_human_check(
            check,
            f"{label}.human_checks[{check_index}]",
        )
    _reject_duplicates(
        (check["code"] for check in human_checks),
        f"{label}.human-check codes",
    )
    for check_index, check in enumerate(human_checks):
        if not set(check["instrument_paths"]) <= fixture_path_set:
            raise CollaborationMatrixError(
                f"{label}.human_checks[{check_index}].instrument_paths "
                "must be a subset of fixture instruments"
            )


def _validate_instrument(value: Any, index: int) -> None:
    label = f"instruments[{index}]"
    instrument = _expect_keys(
        value,
        required={
            "instrument_path",
            "manifest_sha256",
            "probe_profile_id",
            "role",
            "solo_formal",
            "fixture_ids",
            "receipt_sha256",
            "hard_status",
            "candidate_codes",
            "human_status",
        },
        label=label,
    )
    _require_relative_path(
        instrument["instrument_path"],
        f"{label}.instrument_path",
    )
    _require_sha256(instrument["manifest_sha256"], f"{label}.manifest_sha256")
    _require_identifier(
        instrument["probe_profile_id"],
        f"{label}.probe_profile_id",
    )
    _validate_role(instrument["role"], f"{label}.role")
    if not isinstance(instrument["solo_formal"], bool):
        raise CollaborationMatrixError(f"{label}.solo_formal must be a boolean")

    fixture_ids = _require_list(instrument["fixture_ids"], f"{label}.fixture_ids")
    for fixture_index, fixture_id in enumerate(fixture_ids):
        _require_identifier(
            fixture_id,
            f"{label}.fixture_ids[{fixture_index}]",
        )
    _reject_duplicates(fixture_ids, f"{label}.fixture_ids")

    receipt_hashes = _require_list(
        instrument["receipt_sha256"],
        f"{label}.receipt_sha256",
    )
    for receipt_index, receipt_hash in enumerate(receipt_hashes):
        _require_sha256(
            receipt_hash,
            f"{label}.receipt_sha256[{receipt_index}]",
        )
    _require_enum(
        instrument["hard_status"],
        _HARD_STATUSES,
        f"{label}.hard_status",
    )

    candidate_codes = _require_list(
        instrument["candidate_codes"],
        f"{label}.candidate_codes",
    )
    for candidate_index, candidate_code in enumerate(candidate_codes):
        _require_identifier(
            candidate_code,
            f"{label}.candidate_codes[{candidate_index}]",
        )
    _reject_duplicates(candidate_codes, f"{label}.candidate_codes")
    _require_enum(
        instrument["human_status"],
        _HUMAN_STATUSES,
        f"{label}.human_status",
    )


def _derived_hard_status(
    fixtures: Iterable[dict[str, Any]],
    instrument_path: str,
) -> str:
    statuses = [
        assertion["status"]
        for fixture in fixtures
        for assertion in fixture["assertions"]
        if instrument_path in assertion["instrument_paths"]
    ]
    if not statuses:
        return "not_covered"
    if "fail" in statuses:
        return "machine_failed"
    if "inconclusive" in statuses:
        return "inconclusive"
    return "machine_complete"


def _derived_human_status(
    fixtures: Iterable[dict[str, Any]],
    instrument_path: str,
) -> str:
    selected = list(fixtures)
    if not selected:
        return "pending"

    checks_by_fixture = [
        [
            check
            for check in fixture["human_checks"]
            if instrument_path in check["instrument_paths"]
        ]
        for fixture in selected
    ]
    statuses = [
        check["status"]
        for checks in checks_by_fixture
        for check in checks
    ]
    # A conflict or rejection is a terminal blocker even if another context is
    # still pending.  A pass is intentionally stricter: every referenced
    # fixture must contain an attributed check and all checks must pass.
    if "conflict" in statuses or (
        "pass" in statuses and "reject" in statuses
    ):
        return "conflict"
    if "reject" in statuses:
        return "reject"
    if (
        any(not checks for checks in checks_by_fixture)
        or not statuses
        or "pending" in statuses
    ):
        return "pending"
    return "pass"


def _canonical_order(document: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(document)
    result["fixtures"].sort(key=lambda fixture: fixture["fixture_id"])
    for fixture in result["fixtures"]:
        fixture["instruments"].sort()
        fixture["roles"].sort(key=lambda entry: entry["instrument_path"])
        fixture["assertions"].sort(key=lambda item: item["code"])
        fixture["candidates"].sort(key=lambda item: item["code"])
        fixture["human_checks"].sort(key=lambda item: item["code"])
        for collection_name in ("assertions", "candidates", "human_checks"):
            for item in fixture[collection_name]:
                item["instrument_paths"].sort()

    result["instruments"].sort(key=lambda item: item["instrument_path"])
    fixture_by_id = {
        fixture["fixture_id"]: fixture for fixture in result["fixtures"]
    }
    for instrument in result["instruments"]:
        pairs = sorted(
            zip(
                instrument["fixture_ids"],
                instrument["receipt_sha256"],
                strict=True,
            ),
            key=lambda pair: pair[0],
        )
        instrument["fixture_ids"] = [fixture_id for fixture_id, _ in pairs]
        instrument["receipt_sha256"] = [receipt for _, receipt in pairs]
        instrument["candidate_codes"].sort()
        # The lookup also makes accidental ordering dependencies visible while
        # preserving the already validated hash binding.
        for fixture_id, receipt in zip(
            instrument["fixture_ids"],
            instrument["receipt_sha256"],
            strict=True,
        ):
            if fixture_by_id[fixture_id]["receipt_sha256"] != receipt:
                raise CollaborationMatrixError(
                    "receipt ordering no longer matches fixture ordering"
                )
    return result


def validate_collaboration_matrix(document: Any) -> dict[str, Any]:
    """Validate all local fields and cross references, returning a safe copy."""

    _reject_nonfinite_tree(document)
    matrix = _expect_keys(
        document,
        required={
            "format",
            "version",
            "generated_from",
            "coverage",
            "fixtures",
            "instruments",
            "notice",
        },
        label="collaboration matrix",
    )
    if matrix["format"] != COLLABORATION_MATRIX_FORMAT:
        raise CollaborationMatrixError(
            f"format must be {COLLABORATION_MATRIX_FORMAT!r}"
        )
    _require_integer(matrix["version"], "version", minimum=1)
    if matrix["version"] != COLLABORATION_MATRIX_VERSION:
        raise CollaborationMatrixError(
            f"version must be {COLLABORATION_MATRIX_VERSION}"
        )
    generated_from = _require_identifier(
        matrix["generated_from"],
        "generated_from",
    )
    if _ISO_TIMESTAMP.match(generated_from):
        raise CollaborationMatrixError(
            "generated_from must be stable provenance, not a timestamp"
        )
    if matrix["notice"] != COLLABORATION_MATRIX_NOTICE:
        raise CollaborationMatrixError("notice must use the fixed safety notice")

    coverage = _expect_keys(
        matrix["coverage"],
        required={
            "registered",
            "solo_formal",
            "machine_fixture_covered",
            "human_context_reviewed",
        },
        label="coverage",
    )
    for key in (
        "registered",
        "solo_formal",
        "machine_fixture_covered",
        "human_context_reviewed",
    ):
        _require_integer(coverage[key], f"coverage.{key}")

    fixtures = _require_list(matrix["fixtures"], "fixtures")
    for fixture_index, fixture in enumerate(fixtures):
        _validate_fixture(fixture, fixture_index)
    fixture_ids = [fixture["fixture_id"] for fixture in fixtures]
    _reject_duplicates(fixture_ids, "fixture IDs")
    fixture_by_id = {
        fixture["fixture_id"]: fixture for fixture in fixtures
    }

    instruments = _require_list(matrix["instruments"], "instruments")
    if not instruments:
        raise CollaborationMatrixError(
            "instruments must contain at least one registered instrument"
        )
    for instrument_index, instrument in enumerate(instruments):
        _validate_instrument(instrument, instrument_index)
    instrument_paths = [
        instrument["instrument_path"] for instrument in instruments
    ]
    _reject_duplicate_paths(instrument_paths, "instrument paths")
    instrument_by_path = {
        instrument["instrument_path"]: instrument
        for instrument in instruments
    }

    for fixture_index, fixture in enumerate(fixtures):
        fixture_id = fixture["fixture_id"]
        for instrument_path in fixture["instruments"]:
            if instrument_path not in instrument_by_path:
                raise CollaborationMatrixError(
                    f"fixtures[{fixture_index}] references unknown instrument "
                    f"path {instrument_path!r}"
                )
            if fixture_id not in instrument_by_path[instrument_path]["fixture_ids"]:
                raise CollaborationMatrixError(
                    f"instrument {instrument_path!r} does not link back to "
                    f"fixture {fixture_id!r}"
                )
            fixture_role = next(
                role_entry["role"]
                for role_entry in fixture["roles"]
                if role_entry["instrument_path"] == instrument_path
            )
            if fixture_role != instrument_by_path[instrument_path]["role"]:
                raise CollaborationMatrixError(
                    f"fixture {fixture_id!r} role for {instrument_path!r} "
                    "must match the instrument role"
                )

    for instrument_index, instrument in enumerate(instruments):
        label = f"instruments[{instrument_index}]"
        instrument_path = instrument["instrument_path"]
        selected_fixtures: list[dict[str, Any]] = []
        for fixture_id in instrument["fixture_ids"]:
            fixture = fixture_by_id.get(fixture_id)
            if fixture is None:
                raise CollaborationMatrixError(
                    f"{label}.fixture_ids references unknown fixture "
                    f"{fixture_id!r}"
                )
            if instrument_path not in fixture["instruments"]:
                raise CollaborationMatrixError(
                    f"fixture {fixture_id!r} does not include instrument "
                    f"{instrument_path!r}"
                )
            selected_fixtures.append(fixture)

        expected_receipts = [
            fixture["receipt_sha256"] for fixture in selected_fixtures
        ]
        if instrument["receipt_sha256"] != expected_receipts:
            raise CollaborationMatrixError(
                f"{label}.receipt_sha256 must match fixture_ids in order"
            )

        expected_hard_status = _derived_hard_status(
            selected_fixtures,
            instrument_path,
        )
        if instrument["hard_status"] != expected_hard_status:
            raise CollaborationMatrixError(
                f"{label}.hard_status must be {expected_hard_status!r} "
                "for its fixture assertions"
            )

        expected_candidate_codes = sorted(
            {
                candidate["code"]
                for fixture in selected_fixtures
                for candidate in fixture["candidates"]
                if instrument_path in candidate["instrument_paths"]
            }
        )
        if sorted(instrument["candidate_codes"]) != expected_candidate_codes:
            raise CollaborationMatrixError(
                f"{label}.candidate_codes must equal the candidates from "
                "its referenced fixtures"
            )

        expected_human_status = _derived_human_status(
            selected_fixtures,
            instrument_path,
        )
        if instrument["human_status"] != expected_human_status:
            raise CollaborationMatrixError(
                f"{label}.human_status must be {expected_human_status!r} "
                "for its attributed fixture human checks"
            )

    expected_coverage = {
        "registered": len(instruments),
        "solo_formal": sum(
            1 for instrument in instruments if instrument["solo_formal"]
        ),
        "machine_fixture_covered": sum(
            1 for instrument in instruments if instrument["fixture_ids"]
        ),
        "human_context_reviewed": sum(
            1
            for instrument in instruments
            if instrument["human_status"] != "pending"
        ),
    }
    if coverage != expected_coverage:
        raise CollaborationMatrixError(
            "coverage does not match instrument detail counts: "
            f"expected {expected_coverage!r}"
        )

    canonical = _canonical_order(matrix)
    canonical_json_bytes(canonical)
    return canonical


def _copy_mapping_entries(
    values: Iterable[Mapping[str, Any]],
    label: str,
) -> list[dict[str, Any]]:
    if isinstance(values, (str, bytes, Mapping)):
        raise CollaborationMatrixError(
            f"{label} must be an iterable of objects"
        )
    try:
        iterator = iter(values)
    except TypeError as error:
        raise CollaborationMatrixError(
            f"{label} must be an iterable of objects"
        ) from error
    result: list[dict[str, Any]] = []
    for index, value in enumerate(iterator):
        if not isinstance(value, Mapping):
            raise CollaborationMatrixError(
                f"{label}[{index}] must be an object"
            )
        result.append(copy.deepcopy(dict(value)))
    return result


def build_collaboration_matrix(
    *,
    generated_from: str,
    fixtures: Iterable[Mapping[str, Any]],
    instruments: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build and validate a matrix entirely in memory.

    ``fixtures`` and ``instruments`` are full serialized entries.  Coverage is
    derived instead of trusted, which prevents a producer from accidentally
    publishing stale totals.
    """

    fixture_entries = _copy_mapping_entries(fixtures, "fixtures")
    instrument_entries = _copy_mapping_entries(instruments, "instruments")
    document = {
        "format": COLLABORATION_MATRIX_FORMAT,
        "version": COLLABORATION_MATRIX_VERSION,
        "generated_from": generated_from,
        "coverage": {
            "registered": len(instrument_entries),
            "solo_formal": sum(
                1
                for instrument in instrument_entries
                if instrument.get("solo_formal") is True
            ),
            "machine_fixture_covered": sum(
                1
                for instrument in instrument_entries
                if isinstance(instrument.get("fixture_ids"), list)
                and bool(instrument["fixture_ids"])
            ),
            "human_context_reviewed": sum(
                1
                for instrument in instrument_entries
                if isinstance(instrument.get("human_status"), str)
                and instrument.get("human_status") in _HUMAN_STATUSES
                and instrument.get("human_status") != "pending"
            ),
        },
        "fixtures": fixture_entries,
        "instruments": instrument_entries,
        "notice": COLLABORATION_MATRIX_NOTICE,
    }
    return validate_collaboration_matrix(document)


def load_collaboration_matrix(path: str | Path) -> dict[str, Any]:
    """Read strict JSON and validate a collaboration matrix without writing."""

    source = Path(path)
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_json_constant,
        )
    except CollaborationMatrixError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError) as error:
        raise CollaborationMatrixError(
            f"cannot read collaboration matrix {source}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise CollaborationMatrixError(
            f"collaboration matrix root must be an object: {source}"
        )
    return validate_collaboration_matrix(document)


def write_collaboration_matrix_atomic(
    path: str | Path,
    document: Mapping[str, Any],
) -> None:
    """Validate and atomically replace one explicitly requested matrix file."""

    if not isinstance(document, Mapping):
        raise CollaborationMatrixError(
            "collaboration matrix must be an object"
        )
    validated = validate_collaboration_matrix(dict(document))
    payload = (
        json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    with os.fdopen(
        descriptor,
        "w",
        encoding="utf-8",
        newline="\n",
    ) as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    # Success consumes the temporary name.  Preserve it on failure instead of
    # unlinking a pathname that a concurrent actor may already have replaced.
    os.replace(temporary, target)
    if os.name != "nt":
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        directory_fd = os.open(target.parent, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


__all__ = [
    "COLLABORATION_MATRIX_FORMAT",
    "COLLABORATION_MATRIX_NOTICE",
    "COLLABORATION_MATRIX_VERSION",
    "CollaborationMatrixError",
    "build_collaboration_matrix",
    "canonical_json_bytes",
    "load_collaboration_matrix",
    "validate_collaboration_matrix",
    "write_collaboration_matrix_atomic",
]
