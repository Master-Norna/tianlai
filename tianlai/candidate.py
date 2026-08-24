"""Immutable render candidates and receipt-backed inspection."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any
import unicodedata
import uuid
import warnings

from .authoring_json import AuthoringJsonError, AuthoringJsonLimits, strict_json_loads
from .canonical_json import canonical_json_sha256
from .ensemble import CACHE_TELEMETRY_NAME, verify_render_generation
from .events import parse_performance_document
from .plain_file import read_plain_file_bytes
from .portable_filename import is_windows_reserved_filename
from .render_lock import (
    PlainDirectoryIdentity,
    acquire_render_lock,
    capture_plain_directory,
    ensure_authorized_child_directory,
    ensure_plain_directory_tree,
    revalidate_plain_directory,
)
from .realization import parse_realization_document
from .score import parse_pitch, parse_score_document, pitch_name
from .score_ops import compare_scores
from .score_v2_candidate import (
    SCORE_V2_CANDIDATE_VERSION,
    publish_score_v2_candidate_metadata,
    validate_score_v2_candidate_manifest,
)
from .utc_timestamp import (
    canonical_utc_now,
    validate_canonical_utc_timestamp,
)
from .workflow_binding import validate_workflow_authorization


CANDIDATE_FORMAT = "tianlai.candidate"
# The established writer deliberately remains on v2.  Candidate v3 is a
# separate Score-v2 protocol branch with its own publisher and receipt.
CANDIDATE_VERSION = 2
_SUPPORTED_CANDIDATE_VERSIONS = frozenset(
    {1, CANDIDATE_VERSION, SCORE_V2_CANDIDATE_VERSION}
)
MAX_CANDIDATE_JSON_BYTES = 32 * 1024 * 1024
# Retain the private spelling for compatibility with existing fault-injection
# tests while exposing the public publication budget to entrypoints.
_MAX_CANDIDATE_JSON_BYTES = MAX_CANDIDATE_JSON_BYTES
_CANDIDATE_JSON_LIMITS = AuthoringJsonLimits(
    max_document_bytes=_MAX_CANDIDATE_JSON_BYTES,
    max_depth=128,
    max_nodes=2_000_000,
    max_string_bytes=4 * 1024 * 1024,
    max_array_items=500_000,
    max_object_members=65_536,
)
CANDIDATE_MANIFEST_NAME = "候选.json"
AUTHORING_ROSTER_CANDIDATE_NAME = "authoring-roster.json"
PLAYBACK_MAP_KIND = "tianlai.candidate_playback_map"
PLAYBACK_MAP_VERSION = 1
PLAYBACK_MAP_SCHEMA_URI = (
    "https://tianlai.local/schemas/candidate-playback-map.schema.json"
)
MAX_PLAYBACK_MAP_SCHEDULED_NOTES = 250_000
_PLAYBACK_MAP_TRACE_TIME_TOLERANCE = 0.000000501
_PLAYBACK_MAP_TRACE_VELOCITY_TOLERANCE = 0.000050001
_PLAYBACK_MAP_DATETIME = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[Tt ](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?(?P<offset>[Zz]|[+-]\d{2}:\d{2})$"
)


def _parse_bounded_candidate_datetime(value: object) -> datetime:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("candidate date-time is invalid")
    match = _PLAYBACK_MAP_DATETIME.fullmatch(value)
    if match is None:
        raise ValueError("candidate date-time is invalid")
    second = int(match.group("second"))
    if second > 60:
        raise ValueError("candidate date-time is invalid")
    offset_text = match.group("offset")
    if offset_text in {"Z", "z"}:
        offset = timezone.utc
    else:
        offset_hour = int(offset_text[1:3])
        offset_minute = int(offset_text[4:6])
        if offset_hour > 23 or offset_minute > 59:
            raise ValueError("candidate date-time is invalid")
        direction = -1 if offset_text[0] == "-" else 1
        offset = timezone(
            direction
            * timedelta(hours=offset_hour, minutes=offset_minute)
        )
    try:
        # ``datetime`` deliberately rejects leap second 60.  Validate every
        # other calendar/time field with 59 in its place, while preserving the
        # RFC 3339 leap-second spelling in the candidate document itself.
        parsed = datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            min(second, 59),
            tzinfo=offset,
        )
    except ValueError as exc:
        raise ValueError("candidate date-time is invalid") from exc
    return parsed


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extended_windows_path(path: Path) -> Path:
    """Use a local filesystem spelling beyond the legacy Windows MAX_PATH."""

    if os.name != "nt":
        return path
    text = str(path.absolute())
    if text.startswith("\\\\?\\"):
        return path
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)


def _candidate_json_snapshot(
    path: Path,
    *,
    invalid_json_message: str,
    expected_file_sha256: object = None,
    hash_mismatch_message: str | None = None,
    map_read_error_to_invalid_json: bool = False,
) -> tuple[object, str]:
    """Capture, hash, and strictly parse one bounded candidate JSON file."""

    try:
        _identity, payload = read_plain_file_bytes(
            path,
            maximum_bytes=_MAX_CANDIDATE_JSON_BYTES,
        )
    except OSError as exc:
        if map_read_error_to_invalid_json:
            raise ValueError(invalid_json_message) from exc
        raise
    digest = hashlib.sha256(payload).hexdigest()
    if (
        hash_mismatch_message is not None
        and digest != expected_file_sha256
    ):
        raise ValueError(hash_mismatch_message)
    try:
        document = strict_json_loads(
            payload,
            limits=_CANDIDATE_JSON_LIMITS,
            require_object=False,
            require_js_safe_integers=False,
        )
    except AuthoringJsonError as exc:
        raise ValueError(invalid_json_message) from exc
    return document, digest


def _lower_hex(value: object, length: int, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != length
        or re.fullmatch(r"[0-9a-f]+", value) is None
    ):
        raise ValueError(f"{label} must be {length} lowercase hexadecimal characters")
    return value


def _candidate_authoring_input(
    value: object,
) -> tuple[dict[str, str], dict[str, Any]] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "project_id",
        "revision",
        "authoring_roster",
    }:
        raise ValueError("authoring project candidate input has an invalid shape")
    project_id = _lower_hex(value.get("project_id"), 32, label="project_id")
    revision = _lower_hex(value.get("revision"), 64, label="revision")
    authoring_roster = value.get("authoring_roster")
    if not isinstance(authoring_roster, dict):
        raise ValueError("authoring_roster must be an object")
    canonical_sha256 = canonical_json_sha256(authoring_roster)
    return (
        {
            "project_id": project_id,
            "revision": revision,
            "authoring_roster_canonical_sha256": canonical_sha256,
        },
        authoring_roster,
    )


def _candidate_authoring_manifest_binding(
    value: object,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "project_id",
        "revision",
        "authoring_roster",
    }:
        raise ValueError("candidate authoring_project binding has an invalid shape")
    project_id = _lower_hex(value.get("project_id"), 32, label="project_id")
    revision = _lower_hex(value.get("revision"), 64, label="revision")
    roster = value.get("authoring_roster")
    if not isinstance(roster, dict) or set(roster) != {
        "path",
        "canonical_sha256",
        "file_sha256",
    }:
        raise ValueError("candidate authoring roster binding has an invalid shape")
    if roster.get("path") != AUTHORING_ROSTER_CANDIDATE_NAME:
        raise ValueError("candidate authoring roster path is invalid")
    canonical_sha256 = _lower_hex(
        roster.get("canonical_sha256"),
        64,
        label="authoring roster canonical hash",
    )
    file_sha256 = _lower_hex(
        roster.get("file_sha256"),
        64,
        label="authoring roster file hash",
    )
    return {
        "project_id": project_id,
        "revision": revision,
        "authoring_roster": {
            "path": AUTHORING_ROSTER_CANDIDATE_NAME,
            "canonical_sha256": canonical_sha256,
            "file_sha256": file_sha256,
        },
    }


def _authoring_revision_identity(
    *,
    project_id: str,
    score_sha256: str,
    authoring_roster_sha256: str,
    render_profile_sha256: str,
) -> str:
    return canonical_json_sha256(
        {
            "kind": "tianlai.authoring_revision_binding",
            "schema_version": 1,
            "project_id": project_id,
            "documents": {
                "score": score_sha256,
                "authoring_roster": authoring_roster_sha256,
                "render_profile": render_profile_sha256,
            },
        }
    )


def _formal_roster_projection(authoring_roster: object) -> dict[str, Any]:
    """Project the editable roster onto the exact renderer-facing document."""

    if not isinstance(authoring_roster, dict):
        raise ValueError("candidate authoring roster must be an object")
    allowed = {
        "kind",
        "schema_version",
        "name",
        "collaboration",
        "assignments",
    }
    if (
        set(authoring_roster) - allowed
        or authoring_roster.get("kind") != "tianlai.authoring_roster"
        or authoring_roster.get("schema_version") != 1
        or not isinstance(authoring_roster.get("assignments"), list)
        or not authoring_roster["assignments"]
    ):
        raise ValueError("candidate authoring roster cannot produce a formal roster")
    projected: dict[str, Any] = {
        "assignments": authoring_roster["assignments"],
    }
    if "name" in authoring_roster:
        projected["name"] = authoring_roster["name"]
    if "collaboration" in authoring_roster:
        projected["collaboration"] = authoring_roster["collaboration"]
    return projected


def _verify_authoring_formal_roster(
    authoring_roster: object,
    formal_roster: object,
) -> None:
    if not isinstance(formal_roster, dict) or canonical_json_sha256(
        _formal_roster_projection(authoring_roster)
    ) != canonical_json_sha256(formal_roster):
        raise ValueError(
            "candidate formal roster does not match its authoring roster projection"
        )


def _instrument_reference_matches(reference: object, resolved: object) -> bool:
    if not isinstance(reference, str) or not isinstance(resolved, str):
        return False
    if reference == resolved:
        return True
    normalized = reference.replace("\\", "/")
    return "/" not in normalized and resolved.replace("\\", "/").rsplit(
        "/", 1
    )[-1] == normalized


def _expected_plan_routes(formal_roster: object) -> tuple[str, dict[str, dict[str, Any]]]:
    if not isinstance(formal_roster, dict):
        raise ValueError("candidate formal roster must be an object")
    assignments = formal_roster.get("assignments")
    if not isinstance(assignments, list) or not assignments:
        raise ValueError("candidate formal roster assignments are invalid")
    roster_name = formal_roster.get("name", "未命名编制")
    if not isinstance(roster_name, str):
        raise ValueError("candidate formal roster name is invalid")
    expected: dict[str, dict[str, Any]] = {}
    for index, assignment in enumerate(assignments):
        if not isinstance(assignment, dict):
            raise ValueError(
                f"candidate formal roster assignments[{index}] is invalid"
            )
        part_id = assignment.get("part")
        if not isinstance(part_id, str) or not part_id:
            raise ValueError(
                f"candidate formal roster assignments[{index}].part is invalid"
            )
        seat = assignment.get("seat", {})
        if not isinstance(seat, dict):
            raise ValueError(
                f"candidate formal roster assignments[{index}].seat is invalid"
            )
        expected_seat = {
            "azimuth_deg": seat.get("azimuth_deg", 0.0),
            "distance_m": seat.get("distance_m", 3.0),
        }
        common = {
            "part_id": part_id,
            "gain_db": assignment.get("gain_db", 0.0),
            "pan": assignment.get(
                "pan",
                max(
                    -1.0,
                    min(1.0, float(expected_seat["azimuth_deg"]) / 45.0),
                ),
            ),
            "seat": expected_seat,
            "duration_scale": assignment.get("duration_scale", 1.0),
            "dynamic_compression": assignment.get("dynamic_compression", 0.0),
            "articulation_map": assignment.get("articulation_map", {}),
        }
        if "role" in assignment:
            role = assignment["role"]
            if not isinstance(role, dict):
                raise ValueError("candidate formal roster role is invalid")
            normalized_role = dict(role)
            if isinstance(normalized_role.get("label"), str):
                normalized_role["label"] = normalized_role["label"].strip()
            common["role"] = normalized_role
        if assignment.get("gain_automation"):
            common["gain_automation"] = assignment["gain_automation"]
        if assignment.get("overrides"):
            common["overrides"] = assignment["overrides"]
        # When omitted, this value is supplied by the resolved instrument
        # capability.  Candidate verification is intentionally portable and
        # cannot consult a possibly changed external capability catalogue.
        # An explicit author override, however, must survive into the plan.
        if "articulation_auto" in assignment:
            common["articulation_auto"] = assignment["articulation_auto"]

        if "instrument" in assignment and "kit" not in assignment:
            executor_id = assignment.get("executor_id", part_id)
            if not isinstance(executor_id, str) or not executor_id:
                raise ValueError("candidate formal roster executor_id is invalid")
            route = {
                **common,
                "executor_id": executor_id,
                "instrument_reference": assignment["instrument"],
                "transpose": assignment.get("transpose", 0),
                "kit_pitch": None,
            }
            if executor_id in expected:
                raise ValueError("candidate formal roster has duplicate executor IDs")
            expected[executor_id] = route
            continue

        kit = assignment.get("kit")
        if "instrument" in assignment or not isinstance(kit, dict) or not kit:
            raise ValueError("candidate formal roster assignment route is invalid")
        for notehead, reference in sorted(kit.items()):
            if not isinstance(notehead, str):
                raise ValueError("candidate formal roster kit pitch is invalid")
            canonical_pitch = pitch_name(parse_pitch(notehead))
            executor_id = f"{part_id}.{canonical_pitch}"
            transpose = assignment.get("transpose", 0)
            instrument_reference: object = reference
            if isinstance(reference, dict):
                instrument_reference = reference.get("instrument")
                transpose = reference.get("transpose", transpose)
            route = {
                **common,
                "executor_id": executor_id,
                "instrument_reference": instrument_reference,
                "transpose": transpose,
                "kit_pitch": canonical_pitch,
            }
            if executor_id in expected:
                raise ValueError("candidate formal roster has duplicate executor IDs")
            expected[executor_id] = route
    return roster_name, expected


def _verify_formal_roster_plan(
    formal_roster: object,
    performance_plan: object,
) -> None:
    if not isinstance(performance_plan, dict):
        raise ValueError("candidate performance plan must be an object")
    roster_name, expected = _expected_plan_routes(formal_roster)
    if performance_plan.get("roster") != roster_name:
        raise ValueError("candidate performance plan disagrees with formal roster name")
    parts = performance_plan.get("parts")
    if not isinstance(parts, list) or len(parts) != len(expected):
        raise ValueError("candidate performance plan disagrees with formal roster routes")
    actual: dict[str, dict[str, Any]] = {}
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError("candidate performance plan route is invalid")
        executor_id = part.get("executor_id")
        if not isinstance(executor_id, str) or executor_id in actual:
            raise ValueError("candidate performance plan executor identity is invalid")
        actual[executor_id] = part
    if set(actual) != set(expected):
        raise ValueError("candidate performance plan disagrees with formal roster routes")
    for executor_id, route in expected.items():
        part = actual[executor_id]
        if not _instrument_reference_matches(
            route["instrument_reference"], part.get("instrument")
        ):
            raise ValueError(
                "candidate performance plan disagrees with formal roster instrument"
            )
        for key, value in route.items():
            if key == "instrument_reference":
                continue
            if part.get(key) != value:
                raise ValueError(
                    "candidate performance plan disagrees with formal roster "
                    f"route field {key}"
                )
        for optional in ("role", "gain_automation", "overrides"):
            if optional not in route and optional in part:
                raise ValueError(
                    "candidate performance plan disagrees with formal roster "
                    f"route field {optional}"
                )


def _receipt_performance_plan(
    directory: Path,
    receipt_document: object,
    *,
    expected_sha256: str,
) -> dict[str, Any]:
    if not isinstance(receipt_document, dict):
        raise ValueError("render receipt must be an object")
    binding = receipt_document.get("performance_plan")
    if not isinstance(binding, dict):
        raise ValueError("render receipt has no performance_plan binding")
    plan_path = _bound_artifact_path(
        directory,
        binding.get("path", ""),
        label="performance plan",
    )
    plan_document, _plan_file_sha256 = _candidate_json_snapshot(
        plan_path,
        expected_file_sha256=binding.get("file_sha256"),
        hash_mismatch_message=(
            "render receipt performance plan file hash mismatch"
        ),
        invalid_json_message=(
            "candidate performance plan is not valid UTF-8 JSON"
        ),
    )
    if not isinstance(plan_document, dict):
        raise ValueError("render receipt performance plan must be an object")
    canonical_sha256 = canonical_json_sha256(plan_document)
    if (
        canonical_sha256 != binding.get("sha256")
        or canonical_sha256 != expected_sha256
    ):
        raise ValueError("render receipt performance plan hash mismatch")
    return plan_document


def portable_directory_name(text: object, *, maximum_length: int = 72) -> str:
    """Return a clean portable display name for one work directory.

    The directory name is deliberately not an identity.  Candidate manifests
    keep the hash-bound :func:`portable_slug` work ID, while the filesystem
    parent stays readable for people browsing ``output/``.
    """

    original = unicodedata.normalize("NFC", str(text)).strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", original)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        cleaned = "untitled"
    if is_windows_reserved_filename(cleaned):
        cleaned = f"_{cleaned}"
    return cleaned[:maximum_length].rstrip(" ._") or "untitled"


def portable_slug(text: object, *, maximum_length: int = 72) -> str:
    original = unicodedata.normalize("NFC", str(text)).strip()
    cleaned = portable_directory_name(original, maximum_length=maximum_length)
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{digest}"
    stem_length = max(1, maximum_length - len(suffix))
    return f"{cleaned[:stem_length].rstrip(' ._') or 'untitled'}{suffix}"


def _expected_work_id_for_directory(
    document: dict[str, Any],
    directory: Path,
    explicit: str | None,
) -> str:
    """Resolve candidate identity across clean and legacy work directories."""

    title = document.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("candidate title is invalid")
    work_id = document.get("work_id")
    if not isinstance(work_id, str) or not work_id:
        raise ValueError("candidate work_id is invalid")
    if work_id != portable_slug(title):
        raise ValueError("candidate work_id does not match candidate title")
    legacy_parent = directory.parent.name == work_id
    clean_parent = directory.parent.name == portable_directory_name(title)
    if not (legacy_parent or clean_parent):
        raise ValueError(
            "candidate work_id does not match the clean or legacy work directory"
        )
    return work_id if explicit is None else explicit


def new_candidate_id(plan_sha256: str | None = None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    binding = (plan_sha256 or "unbound")[:8]
    return f"candidate-{timestamp}-{binding}-{uuid.uuid4().hex[:8]}"


def _candidate_json_text(value: object) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    )


def validate_candidate_json_size(
    value: object,
    *,
    label: str = "candidate JSON",
) -> int:
    """Return the published UTF-8 size or reject an oversized artifact."""

    size = len(_candidate_json_text(value).encode("utf-8"))
    if size > MAX_CANDIDATE_JSON_BYTES:
        raise ValueError(
            f"{label} published JSON size {size} exceeds candidate limit "
            f"{MAX_CANDIDATE_JSON_BYTES} bytes"
        )
    return size


def _write_json_atomic(path: Path, value: object) -> None:
    operational = _extended_windows_path(path)
    operational.parent.mkdir(parents=True, exist_ok=True)
    payload = _candidate_json_text(value)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=operational.parent,
        prefix=f".{operational.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    # On success the temporary name ceases to exist.  On failure it is
    # deliberately preserved: deleting a mutable pathname here could erase a
    # file installed by another writer after the failed replace.
    os.replace(temporary, operational)


@dataclass(frozen=True, slots=True)
class CandidateTarget:
    work_id: str
    candidate_id: str
    directory: Path
    replacing: bool
    expected_receipt_sha256: str | None = None
    expected_manifest_sha256: str | None = None
    work_directory_identity: PlainDirectoryIdentity | None = None
    directory_identity: PlainDirectoryIdentity | None = None


class CandidateAlreadyExistsError(FileExistsError):
    """One immutable candidate ID was already committed by another writer."""


@dataclass(frozen=True, slots=True)
class _CommittedBackup:
    path: Path
    identity: PlainDirectoryIdentity


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _is_plain_directory(path: Path) -> bool:
    try:
        return (
            path.is_dir()
            and not path.is_symlink()
            and path.resolve() == path.absolute()
        )
    except OSError:
        return False


def _previous_directories(
    final: Path,
    *,
    parent_identity: PlainDirectoryIdentity | None = None,
) -> tuple[Path, ...]:
    prefix = f".{final.name}."
    suffix = ".previous"
    if parent_identity is not None:
        parent = revalidate_plain_directory(parent_identity)
        if final.parent != parent:
            raise ValueError("candidate recovery path escaped its verified parent")
    if not final.parent.is_dir():
        return ()
    result = tuple(
        sorted(
            (
                child
                for child in final.parent.iterdir()
                if child.name.startswith(prefix)
                and child.name.endswith(suffix)
            ),
            key=lambda child: child.name,
        )
    )
    if parent_identity is not None:
        revalidate_plain_directory(parent_identity)
    return result


def _expected_backup_path(target: CandidateTarget) -> Path:
    manifest_sha256 = target.expected_manifest_sha256
    if not manifest_sha256:
        raise ValueError(
            "覆盖现有候选缺少准备阶段记录的候选清单 Hash"
        )
    return target.directory.with_name(
        f".{target.directory.name}.{manifest_sha256}.previous"
    )


def _verify_candidate_identity(
    document: dict[str, Any],
    *,
    expected_work_id: str,
    expected_candidate_id: str,
) -> None:
    if document.get("work_id") != expected_work_id:
        raise ValueError(
            "候选清单 work_id 与目标作品目录身份不一致"
        )
    if document.get("candidate_id") != expected_candidate_id:
        raise ValueError(
            "候选清单 candidate_id 与目标候选目录身份不一致"
        )


def _verify_candidate_generation(
    directory: Path,
    *,
    expected_work_id: str,
    expected_candidate_id: str,
) -> dict[str, Any]:
    _, document = load_candidate(
        directory,
        verify=True,
        expected_work_id=expected_work_id,
        expected_candidate_id=expected_candidate_id,
    )
    return document


def _normalized_expected_receipt_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None
    ):
        raise ValueError(
            "expected_receipt_sha256 必须是 64 位 SHA-256 十六进制字符串"
        )
    return value.lower()


def _recover_previous_if_safe(
    final: Path,
    *,
    work_id: str,
    candidate_id: str,
    overwrite: bool,
    expected_receipt_sha256: str | None,
    parent_identity: PlainDirectoryIdentity | None = None,
) -> None:
    previous = _previous_directories(
        final,
        parent_identity=parent_identity,
    )
    if not previous:
        return
    paths = ", ".join(str(path) for path in previous)
    if _path_exists(final):
        raise RuntimeError(
            "候选目录旁存在未清理的 .previous 事务残留；"
            "最终候选也存在，无法无歧义判断提交阶段，已失败关闭: "
            f"{paths}"
        )
    if len(previous) != 1:
        raise RuntimeError(
            "候选目录缺失且存在多个 .previous 事务残留，"
            f"无法无歧义恢复，已失败关闭: {paths}"
        )
    if not overwrite or expected_receipt_sha256 is None:
        raise RuntimeError(
            "发现可疑的中断覆盖事务；仅在显式 --overwrite 并提供"
            "旧渲染回执 Hash 后才允许验证恢复: "
            f"{previous[0]}"
        )

    backup = previous[0]
    pattern = re.compile(
        rf"^\.{re.escape(final.name)}\.([0-9a-f]{{64}})\.previous$"
    )
    match = pattern.fullmatch(backup.name)
    if (
        match is None
        or not _is_plain_directory(backup)
    ):
        raise RuntimeError(
            "发现无法自证身份的旧格式或异常 .previous 残留，"
            f"拒绝猜测恢复: {backup}"
        )
    manifest_path = backup / CANDIDATE_MANIFEST_NAME
    if (
        not manifest_path.is_file()
        or sha256_file(manifest_path) != match.group(1)
    ):
        raise RuntimeError(
            "中断事务备份的候选清单 Hash 与目录身份不一致，"
            f"拒绝恢复: {backup}"
        )
    _verify_candidate_generation(
        backup,
        expected_work_id=work_id,
        expected_candidate_id=candidate_id,
    )
    receipt = backup / "渲染回执.json"
    if (
        not receipt.is_file()
        or sha256_file(receipt) != expected_receipt_sha256
    ):
        raise RuntimeError(
            "中断事务备份的渲染回执与显式预期 Hash 不一致，"
            f"拒绝恢复: {backup}"
        )

    if parent_identity is not None:
        revalidate_plain_directory(parent_identity)
    os.replace(backup, final)
    if parent_identity is not None:
        revalidate_plain_directory(parent_identity)
    try:
        _verify_candidate_generation(
            final,
            expected_work_id=work_id,
            expected_candidate_id=candidate_id,
        )
    except BaseException as verification_error:
        try:
            if _path_exists(backup):
                raise RuntimeError(
                    "恢复后的备份路径已被其他写者占用"
                )
            os.replace(final, backup)
        except BaseException as rollback_error:
            raise RuntimeError(
                "恢复中断候选后复验失败，且无法撤回复原；"
                f"请保全并检查 {final} 与 {backup}"
            ) from rollback_error
        raise verification_error


def prepare_candidate_target(
    output_root: str | Path,
    title: str,
    *,
    plan_sha256: str | None = None,
    output_id: str | None = None,
    overwrite: bool = False,
    expected_receipt_sha256: str | None = None,
    clean_work_directory: bool = True,
) -> CandidateTarget:
    """Resolve and identity-bind one candidate publication target."""

    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be boolean")
    expected_receipt = _normalized_expected_receipt_sha256(
        expected_receipt_sha256
    )
    if not isinstance(clean_work_directory, bool):
        raise ValueError("clean_work_directory must be boolean")
    work_id = portable_slug(title)
    if clean_work_directory:
        work_directory_name = portable_directory_name(title)
        if work_directory_name.casefold() == "authoring-projects":
            raise ValueError("work directory name is reserved")
    else:
        work_directory_name = work_id
    candidate_id = (
        new_candidate_id(plan_sha256)
        if output_id is None
        else portable_slug(output_id, maximum_length=96)
    )
    root_request = Path(output_root).expanduser()
    if not root_request.is_absolute():
        root_request = root_request.absolute()
    root_identity = ensure_plain_directory_tree(root_request)
    work_identity = ensure_authorized_child_directory(
        root_identity,
        work_directory_name,
    )
    directory = work_identity.path / candidate_id
    with acquire_render_lock(directory, parent_identity=work_identity):
        revalidate_plain_directory(work_identity)
        _recover_previous_if_safe(
            directory,
            work_id=work_id,
            candidate_id=candidate_id,
            overwrite=overwrite,
            expected_receipt_sha256=expected_receipt,
            parent_identity=work_identity,
        )
        if not _path_exists(directory):
            return CandidateTarget(
                work_id,
                candidate_id,
                directory,
                False,
                None,
                None,
                work_identity,
                None,
            )
        if not overwrite:
            raise CandidateAlreadyExistsError(
                f"候选目录已存在，默认拒绝覆盖: {directory}"
            )
        if not _is_plain_directory(directory):
            raise ValueError(
                "现有候选目标必须是最终作品目录中的普通目录，"
                "不能是符号链接或目录联接"
            )
        if expected_receipt is None:
            raise ValueError(
                "覆盖现有候选必须提供 expected_receipt_sha256"
            )
        _, document = load_candidate(
            directory,
            verify=False,
            expected_work_id=work_id,
            expected_candidate_id=candidate_id,
        )
        receipt = directory / "渲染回执.json"
        if (
            not receipt.is_file()
            or sha256_file(receipt) != expected_receipt
        ):
            raise ValueError("现有候选的渲染回执与预期 Hash 不一致")
        manifest = directory / CANDIDATE_MANIFEST_NAME
        if not manifest.is_file():
            raise ValueError("现有候选缺少候选清单")
        manifest_sha256 = sha256_file(manifest)
        _verify_candidate_identity(
            document,
            expected_work_id=work_id,
            expected_candidate_id=candidate_id,
        )
        directory_identity = capture_plain_directory(directory)
        revalidate_plain_directory(work_identity)
        return CandidateTarget(
            work_id,
            candidate_id,
            directory,
            True,
            expected_receipt,
            manifest_sha256,
            work_identity,
            directory_identity,
        )


def _verify_replacement_identity(target: CandidateTarget) -> None:
    expected_receipt = target.expected_receipt_sha256
    expected_manifest = target.expected_manifest_sha256
    if not expected_receipt or not expected_manifest:
        raise ValueError(
            "覆盖现有候选缺少准备阶段记录的完整身份"
        )
    if target.work_directory_identity is not None:
        try:
            revalidate_plain_directory(target.work_directory_identity)
        except OSError as exc:
            raise ValueError(
                "candidate work directory \u53d1\u751f\u53d8\u5316 during render"
            ) from exc
    if target.directory_identity is not None:
        try:
            revalidate_plain_directory(target.directory_identity)
        except OSError as exc:
            raise ValueError(
                "existing candidate \u53d1\u751f\u53d8\u5316 during render"
            ) from exc
    if not _is_plain_directory(target.directory):
        raise ValueError(
            "覆盖候选目标必须是普通目录，不能是符号链接或目录联接"
        )
    _, document = load_candidate(
        target.directory,
        verify=False,
        expected_work_id=target.work_id,
        expected_candidate_id=target.candidate_id,
    )
    _verify_candidate_identity(
        document,
        expected_work_id=target.work_id,
        expected_candidate_id=target.candidate_id,
    )
    receipt = target.directory / "渲染回执.json"
    manifest = target.directory / CANDIDATE_MANIFEST_NAME
    if (
        not receipt.is_file()
        or sha256_file(receipt) != expected_receipt
        or not manifest.is_file()
        or sha256_file(manifest) != expected_manifest
    ):
        raise ValueError("现有候选在渲染期间发生变化，拒绝覆盖")


def _rollback_replacement(
    *,
    staging: Path,
    final: Path,
    backup: Path,
    publish_error: BaseException,
) -> None:
    rollback_errors: list[str] = []
    if _path_exists(final):
        if _path_exists(staging):
            rollback_errors.append(
                f"无法撤回新候选，暂存路径已被占用: {staging}"
            )
        else:
            try:
                os.replace(final, staging)
            except BaseException as exc:
                rollback_errors.append(f"撤回新候选失败: {exc}")
    if not _path_exists(final):
        if not _path_exists(backup):
            rollback_errors.append(f"旧候选备份丢失: {backup}")
        else:
            try:
                os.replace(backup, final)
            except BaseException as exc:
                rollback_errors.append(f"恢复旧候选失败: {exc}")
    if rollback_errors:
        raise RuntimeError(
            "候选发布失败且自动回滚不完整；"
            f"旧候选备份应保全于 {backup}；"
            + "; ".join(rollback_errors)
        ) from publish_error


def _safe_cleanup_private_directory(
    path: Path,
    *,
    parent: Path,
    prefix: str,
    label: str,
    parent_identity: PlainDirectoryIdentity | None = None,
    directory_identity: PlainDirectoryIdentity | None = None,
) -> None:
    """Move a private transaction entry out of the active namespace.

    An identity check cannot bind a later path lookup.  Recursive deletion
    could therefore erase a directory installed after the check, especially
    where ``shutil.rmtree`` has no descriptor-based traversal.  Cleanup uses
    only a same-parent rename to a recoverable name.  The preserved entry no
    longer has a ``.staging`` or ``.previous`` suffix, so it cannot block a
    later publication or recovery attempt.
    """

    try:
        if parent_identity is None:
            parent_identity = capture_plain_directory(parent)
        resolved_parent = revalidate_plain_directory(parent_identity)
        if (
            path.parent != resolved_parent
            or not path.name.startswith(prefix)
            or path != resolved_parent / path.name
        ):
            raise RuntimeError(
                f"拒绝清理身份异常的{label}: {path}"
            )
        if directory_identity is None:
            directory_identity = capture_plain_directory(path)
        identity_changed = False
        try:
            revalidate_plain_directory(directory_identity)
        except BaseException:
            # The checked entry has already been replaced.  Renaming the
            # current directory entry is recoverable; deleting it is not.
            identity_changed = True
        revalidate_plain_directory(parent_identity)
        if not os.path.lexists(path):
            return
        preserved: Path | None = None
        for _ in range(16):
            # Keep the quarantine name independent of the source basename.
            # Transaction names can already approach Windows' component/path
            # limits (notably ``.<candidate-id>.<uuid>.previous``); appending
            # another suffix made cleanup itself fail with ERROR_PATH_NOT_FOUND.
            candidate = resolved_parent / (
                f".cleanup-preserved-{uuid.uuid4().hex}"
            )
            if os.path.lexists(candidate):
                continue
            try:
                # os.rename does not replace an existing destination on
                # Windows.  A random suffix also makes accidental collision
                # negligible on platforms with different rename semantics.
                os.rename(path, candidate)
            except FileExistsError:
                continue
            except FileNotFoundError:
                # A concurrent actor may remove the private entry between the
                # existence check and rename.  There is then nothing left to
                # preserve, and claiming otherwise would be a false warning.
                revalidate_plain_directory(parent_identity)
                if not os.path.lexists(path):
                    return
                raise
            preserved = candidate
            break
        if preserved is None:
            raise RuntimeError(f"无法为{label}预留安全的保全名称")
        revalidate_plain_directory(parent_identity)
        if not identity_changed:
            try:
                moved_identity = capture_plain_directory(preserved)
                if (
                    moved_identity.device != directory_identity.device
                    or moved_identity.inode != directory_identity.inode
                ):
                    identity_changed = True
            except BaseException:
                identity_changed = True
        if identity_changed:
            warnings.warn(
                f"{label} identity changed during cleanup; the replacement "
                f"was safely preserved at {preserved}",
                RuntimeWarning,
                stacklevel=3,
            )
    except BaseException as exc:
        try:
            warnings.warn(
                f"{label}未能清理，已保留供检查: {path}: {exc}",
                RuntimeWarning,
                stacklevel=3,
            )
        except BaseException:
            # Warning filters may promote this diagnostic to an exception.
            # Cleanup occurs only after commit or while preserving an earlier
            # failure, so it must never change the publication result.
            pass


def _commit_candidate_staging(
    staging: Path,
    target: CandidateTarget,
) -> _CommittedBackup | None:
    """Move one fully verified staging generation into its final location."""

    final = target.directory
    work_identity = target.work_directory_identity
    if work_identity is None:
        work_identity = capture_plain_directory(final.parent)
    revalidate_plain_directory(work_identity)
    staging_identity = capture_plain_directory(staging)
    if staging.parent != final.parent:
        raise ValueError("候选暂存目录必须与最终目录位于同一父目录")
    if not target.replacing:
        if _path_exists(final):
            raise CandidateAlreadyExistsError(
                f"候选目录在渲染期间被创建，拒绝覆盖: {final}"
            )
        revalidate_plain_directory(work_identity)
        revalidate_plain_directory(staging_identity)
        os.replace(staging, final)
        try:
            revalidate_plain_directory(work_identity)
            moved_identity = capture_plain_directory(final)
            if (
                moved_identity.device != staging_identity.device
                or moved_identity.inode != staging_identity.inode
            ):
                raise RuntimeError(
                    "candidate staging identity changed during commit"
                )
            _verify_candidate_generation(
                final,
                expected_work_id=target.work_id,
                expected_candidate_id=target.candidate_id,
            )
        except BaseException as verification_error:
            try:
                if _path_exists(staging):
                    raise RuntimeError("候选暂存路径已被其他写者占用")
                os.replace(final, staging)
            except BaseException as rollback_error:
                raise RuntimeError(
                    "新候选改名后复验失败，且无法撤回最终目录；"
                    f"请保全并检查 {final}"
                ) from rollback_error
            raise verification_error
        return None

    revalidate_plain_directory(work_identity)
    _verify_replacement_identity(target)
    backup = _expected_backup_path(target)
    if _path_exists(backup):
        raise RuntimeError(
            f"确定身份的旧候选备份已存在，拒绝覆盖: {backup}"
        )
    final_identity = target.directory_identity or capture_plain_directory(final)
    revalidate_plain_directory(final_identity)
    os.replace(final, backup)
    try:
        revalidate_plain_directory(work_identity)
        backup_identity = capture_plain_directory(backup)
        if (
            backup_identity.device != final_identity.device
            or backup_identity.inode != final_identity.inode
        ):
            raise RuntimeError(
                "existing candidate identity changed during backup"
            )
        backup_target = CandidateTarget(
            target.work_id,
            target.candidate_id,
            backup,
            True,
            target.expected_receipt_sha256,
            target.expected_manifest_sha256,
            work_identity,
            backup_identity,
        )
        _verify_replacement_identity(backup_target)
        revalidate_plain_directory(work_identity)
        revalidate_plain_directory(staging_identity)
        os.replace(staging, final)
        revalidate_plain_directory(work_identity)
        moved_identity = capture_plain_directory(final)
        if (
            moved_identity.device != staging_identity.device
            or moved_identity.inode != staging_identity.inode
        ):
            raise RuntimeError("candidate staging identity changed during commit")
        _verify_candidate_generation(
            final,
            expected_work_id=target.work_id,
            expected_candidate_id=target.candidate_id,
        )
    except BaseException as publish_error:
        _rollback_replacement(
            staging=staging,
            final=final,
            backup=backup,
            publish_error=publish_error,
        )
        raise publish_error
    return _CommittedBackup(backup, backup_identity)


@contextmanager
def candidate_publication(target: CandidateTarget):
    """Render into a sibling staging directory and publish only when complete.

    A valid ``候选.json`` and all of its source bindings must be present before
    the final directory can appear.  For an explicitly authorised replacement,
    the original receipt is rechecked immediately before the directory swap.
    """

    parent = target.directory.parent
    work_identity = target.work_directory_identity
    if work_identity is None:
        work_identity = capture_plain_directory(parent)
    if revalidate_plain_directory(work_identity) != parent:
        raise ValueError("candidate target escaped its verified work directory")
    with acquire_render_lock(
        target.directory,
        parent_identity=work_identity,
    ):
        revalidate_plain_directory(work_identity)
        residuals = _previous_directories(
            target.directory,
            parent_identity=work_identity,
        )
        if residuals:
            raise RuntimeError(
                "候选发布前发现 .previous 事务残留，已失败关闭: "
                + ", ".join(str(path) for path in residuals)
            )
        if target.replacing:
            _verify_replacement_identity(target)
        elif _path_exists(target.directory):
            raise CandidateAlreadyExistsError(
                f"候选目录在准备后被创建，拒绝覆盖: {target.directory}"
            )

        staging_prefix = f".{target.candidate_id}."
        staging = Path(
            tempfile.mkdtemp(
                prefix=staging_prefix,
                suffix=".staging",
                dir=parent,
            )
        ).resolve()
        revalidate_plain_directory(work_identity)
        staging_identity = capture_plain_directory(staging)
        staged_target = CandidateTarget(
            target.work_id,
            target.candidate_id,
            staging,
            False,
            None,
            None,
            work_identity,
            staging_identity,
        )
        committed = False
        backup: _CommittedBackup | None = None
        try:
            yield staged_target
            revalidate_plain_directory(work_identity)
            revalidate_plain_directory(staging_identity)
            _verify_candidate_generation(
                staging,
                expected_work_id=target.work_id,
                expected_candidate_id=target.candidate_id,
            )
            backup = _commit_candidate_staging(staging, target)
            committed = True
        finally:
            if not committed and _path_exists(staging):
                _safe_cleanup_private_directory(
                    staging,
                    parent=parent,
                    prefix=staging_prefix,
                    parent_identity=work_identity,
                    directory_identity=staging_identity,
                    label="未发布候选暂存目录",
                )
        if backup is not None:
            _safe_cleanup_private_directory(
                backup.path,
                parent=parent,
                prefix=f".{target.directory.name}.",
                parent_identity=work_identity,
                directory_identity=backup.identity,
                label="已提交候选的旧版本备份",
            )


def publish_candidate_metadata(
    target: CandidateTarget,
    *,
    title: str,
    score: dict[str, Any],
    roster: dict[str, Any],
    render_profile: dict[str, Any],
    realization: dict[str, Any] | None = None,
    receipt_path: str | Path,
    plan_sha256: str,
    parent_candidate_id: str | None = None,
    authoring_project: dict[str, Any] | None = None,
    authoring_workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write source documents and install the candidate manifest last."""

    if target.work_directory_identity is not None:
        revalidate_plain_directory(target.work_directory_identity)
    if target.directory_identity is not None:
        directory = revalidate_plain_directory(target.directory_identity)
    else:
        directory = target.directory.resolve()
    if directory != target.directory:
        raise ValueError("candidate metadata target changed identity")
    receipt = Path(receipt_path).resolve()
    if receipt.parent != directory or not receipt.is_file():
        raise ValueError("render receipt must be inside the candidate directory")
    authoring_input = _candidate_authoring_input(authoring_project)
    if realization is not None and authoring_input is not None:
        raise ValueError(
            "realization is not yet part of the managed authoring revision "
            "identity; publish it through project-render without an "
            "authoring_project binding"
        )
    parsed_realization = (
        parse_realization_document(
            realization,
            score_document=score,
        )
        if realization is not None
        else None
    )
    if realization is not None:
        validate_candidate_json_size(
            realization,
            label="candidate realization",
        )
    workflow_input = validate_workflow_authorization(authoring_workflow)
    if workflow_input is not None:
        if authoring_input is None:
            raise ValueError(
                "authoring workflow binding requires an authoring project"
            )
        authoring_identity, _authoring_roster = authoring_input
        if (
            workflow_input["project_id"] != authoring_identity["project_id"]
            or workflow_input["authoring_revision"]
            != authoring_identity["revision"]
            or workflow_input["candidate_work_id"] != target.work_id
            or workflow_input["candidate_id"] != target.candidate_id
            or workflow_input["parent_candidate_id"] != parent_candidate_id
        ):
            raise ValueError(
                "authoring workflow binding disagrees with candidate identity"
            )
    receipt_document, receipt_sha256 = _candidate_json_snapshot(
        receipt,
        invalid_json_message="render receipt must be valid UTF-8 JSON",
        map_read_error_to_invalid_json=True,
    )
    if not isinstance(receipt_document, dict):
        raise ValueError("render receipt must be an object")
    if authoring_input is None:
        if "authoring_project" in receipt_document:
            raise ValueError(
                "render receipt has an unmanifested authoring project binding"
            )
    else:
        expected_receipt_binding, authoring_roster = authoring_input
        if receipt_document.get("authoring_project") != expected_receipt_binding:
            raise ValueError(
                "render receipt disagrees with the authoring project identity"
            )
        expected_revision = _authoring_revision_identity(
            project_id=expected_receipt_binding["project_id"],
            score_sha256=canonical_json_sha256(score),
            authoring_roster_sha256=expected_receipt_binding[
                "authoring_roster_canonical_sha256"
            ],
            render_profile_sha256=canonical_json_sha256(render_profile),
        )
        if expected_revision != expected_receipt_binding["revision"]:
            raise ValueError(
                "authoring revision binding does not match candidate documents"
            )
        _verify_authoring_formal_roster(authoring_roster, roster)
        performance_plan = _receipt_performance_plan(
            directory,
            receipt_document,
            expected_sha256=plan_sha256,
        )
        _verify_formal_roster_plan(roster, performance_plan)
    receipt_workflow = receipt_document.get("authoring_workflow")
    if workflow_input is None:
        if "authoring_workflow" in receipt_document:
            raise ValueError(
                "render receipt has an unmanifested authoring workflow binding"
            )
    elif receipt_workflow != workflow_input:
        raise ValueError(
            "render receipt disagrees with the authoring workflow authorization"
        )
    score_path = directory / "score.json"
    roster_path = directory / "roster.json"
    profile_path = directory / "render-profile.json"
    realization_path = directory / "realization.json"
    _write_json_atomic(score_path, score)
    _write_json_atomic(roster_path, roster)
    _write_json_atomic(profile_path, render_profile)
    if realization is not None:
        _write_json_atomic(realization_path, realization)
    if target.directory_identity is not None:
        revalidate_plain_directory(target.directory_identity)
    authoring_manifest_binding: dict[str, Any] | None = None
    if authoring_input is not None:
        receipt_authoring_binding, authoring_roster = authoring_input
        authoring_roster_path = directory / AUTHORING_ROSTER_CANDIDATE_NAME
        _write_json_atomic(authoring_roster_path, authoring_roster)
        authoring_manifest_binding = {
            "project_id": receipt_authoring_binding["project_id"],
            "revision": receipt_authoring_binding["revision"],
            "authoring_roster": {
                "path": authoring_roster_path.name,
                "canonical_sha256": receipt_authoring_binding[
                    "authoring_roster_canonical_sha256"
                ],
                "file_sha256": sha256_file(authoring_roster_path),
            },
        }
    manifest = {
        "format": CANDIDATE_FORMAT,
        "version": CANDIDATE_VERSION,
        "candidate_id": target.candidate_id,
        "work_id": target.work_id,
        "title": title,
        "created_at_utc": canonical_utc_now(),
        "parent_candidate_id": parent_candidate_id,
        "project": {
            "score": {
                "path": score_path.name,
                "canonical_sha256": canonical_json_sha256(score),
                "file_sha256": sha256_file(score_path),
            },
            "roster": {
                "path": roster_path.name,
                "canonical_sha256": canonical_json_sha256(roster),
                "file_sha256": sha256_file(roster_path),
            },
            "render_profile": {
                "path": profile_path.name,
                "canonical_sha256": canonical_json_sha256(render_profile),
                "file_sha256": sha256_file(profile_path),
            },
            "performance_plan_sha256": plan_sha256,
        },
        "render_receipt": {
            "path": receipt.name,
            # Bind the same bounded payload that was parsed and validated
            # above; reopening here could otherwise mix two generations.
            "sha256": receipt_sha256,
        },
    }
    if realization is not None:
        assert parsed_realization is not None
        manifest["project"]["realization"] = {
            "path": realization_path.name,
            "canonical_sha256": canonical_json_sha256(realization),
            "file_sha256": sha256_file(realization_path),
        }
    if authoring_manifest_binding is not None:
        manifest["authoring_project"] = authoring_manifest_binding
    if workflow_input is not None:
        manifest["authoring_workflow"] = workflow_input
    cache_telemetry = directory / CACHE_TELEMETRY_NAME
    if cache_telemetry.exists() or cache_telemetry.is_symlink():
        if cache_telemetry.is_symlink() or not cache_telemetry.is_file():
            raise ValueError(
                "cache telemetry must be a regular non-symlink file"
            )
        manifest["cache_telemetry"] = {
            "path": cache_telemetry.name,
            "sha256": sha256_file(cache_telemetry),
        }
    if target.directory_identity is not None:
        revalidate_plain_directory(target.directory_identity)
    _write_json_atomic(directory / CANDIDATE_MANIFEST_NAME, manifest)
    return manifest


def _candidate_directory(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_dir():
        return candidate
    if candidate.name == CANDIDATE_MANIFEST_NAME and candidate.is_file():
        return candidate.parent
    raise ValueError(
        f"candidate must be a directory or {CANDIDATE_MANIFEST_NAME}: {path}"
    )


def _bound_artifact_path(
    directory: Path,
    value: object,
    *,
    label: str,
) -> Path:
    raw = Path(str(value))
    if raw.is_absolute():
        raise ValueError(f"candidate {label} path must be relative")
    resolved = (directory / raw).resolve()
    try:
        resolved.relative_to(directory)
    except ValueError as exc:
        raise ValueError(
            f"candidate {label} path escapes its generation directory"
        ) from exc
    operational = _extended_windows_path(resolved)
    if not operational.is_file():
        raise ValueError(f"candidate {label} file is missing")
    return operational


def load_candidate(
    path: str | Path,
    *,
    verify: bool = True,
    expected_work_id: str | None = None,
    expected_candidate_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    directory = _candidate_directory(path)
    manifest_path = directory / CANDIDATE_MANIFEST_NAME
    document, _manifest_sha256 = _candidate_json_snapshot(
        manifest_path,
        invalid_json_message="candidate manifest is invalid JSON",
    )
    version = document.get("version") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("format") != CANDIDATE_FORMAT
        or isinstance(version, bool)
        or version not in _SUPPORTED_CANDIDATE_VERSIONS
    ):
        raise ValueError("unsupported candidate manifest")
    created_at_utc = document.get("created_at_utc")
    if version in {CANDIDATE_VERSION, SCORE_V2_CANDIDATE_VERSION}:
        try:
            validate_canonical_utc_timestamp(created_at_utc)
        except ValueError as exc:
            raise ValueError(
                "candidate created_at_utc is not canonical UTC"
            ) from exc
    else:
        try:
            _parse_bounded_candidate_datetime(created_at_utc)
        except ValueError as exc:
            raise ValueError(
                "legacy candidate created_at_utc is invalid"
            ) from exc
    if version == SCORE_V2_CANDIDATE_VERSION:
        resolved_work_id = _expected_work_id_for_directory(
            document,
            directory,
            expected_work_id,
        )
        validate_score_v2_candidate_manifest(
            document,
            expected_work_id=resolved_work_id,
            expected_candidate_id=(
                directory.name
                if expected_candidate_id is None
                else expected_candidate_id
            ),
        )
        if verify:
            # Import lazily: candidate_integrity imports the legacy candidate
            # constants and helpers, while its v3 branch is the stronger
            # descriptor-bound closed-generation reader.
            from .candidate_integrity import verify_candidate_integrity

            verify_candidate_integrity(
                directory,
                expected_work_id=expected_work_id,
                expected_candidate_id=expected_candidate_id,
            )
        return directory, document

    if "authoring_project" in document:
        authoring_binding = _candidate_authoring_manifest_binding(
            document.get("authoring_project")
        )
        if authoring_binding is None:
            raise ValueError("candidate authoring_project binding cannot be null")
    else:
        authoring_binding = None
    if "authoring_workflow" in document:
        try:
            workflow_binding = validate_workflow_authorization(
                document.get("authoring_workflow"),
                allow_none=False,
            )
        except ValueError as exc:
            raise ValueError(
                "candidate authoring_workflow binding is invalid"
            ) from exc
    else:
        workflow_binding = None
    if workflow_binding is not None:
        if version != CANDIDATE_VERSION:
            raise ValueError(
                "managed authoring workflow requires the current candidate version"
            )
        if authoring_binding is None:
            raise ValueError(
                "candidate authoring workflow lacks an authoring project binding"
            )
        if (
            workflow_binding["project_id"] != authoring_binding["project_id"]
            or workflow_binding["authoring_revision"]
            != authoring_binding["revision"]
            or workflow_binding["candidate_work_id"] != document.get("work_id")
            or workflow_binding["candidate_id"] != document.get("candidate_id")
            or workflow_binding["parent_candidate_id"]
            != document.get("parent_candidate_id")
        ):
            raise ValueError(
                "candidate authoring workflow and candidate identity disagree"
            )
    resolved_work_id = _expected_work_id_for_directory(
        document,
        directory,
        expected_work_id,
    )
    _verify_candidate_identity(
        document,
        expected_work_id=resolved_work_id,
        expected_candidate_id=(
            directory.name
            if expected_candidate_id is None
            else expected_candidate_id
        ),
    )
    if verify:
        verified_project_hashes: dict[str, str] = {}
        verified_project_documents: dict[str, dict[str, Any]] = {}
        for key in ("score", "roster", "render_profile"):
            project = document.get("project")
            if not isinstance(project, dict):
                raise ValueError("candidate project binding is missing")
            binding = project.get(key)
            if not isinstance(binding, dict):
                raise ValueError(f"candidate project.{key} binding is missing")
            source = _bound_artifact_path(
                directory,
                binding.get("path", ""),
                label=key,
            )
            value, _source_sha256 = _candidate_json_snapshot(
                source,
                expected_file_sha256=binding.get("file_sha256"),
                hash_mismatch_message=(
                    f"candidate {key} file hash mismatch"
                ),
                invalid_json_message=f"candidate {key} is invalid JSON",
            )
            if not isinstance(value, dict):
                raise ValueError(f"candidate {key} must be an object")
            if canonical_json_sha256(value) != binding.get(
                "canonical_sha256"
            ):
                raise ValueError(f"candidate {key} canonical hash mismatch")
            verified_project_hashes[key] = binding["canonical_sha256"]
            verified_project_documents[key] = value
        realization_binding = project.get("realization")
        realization_artifact = directory / "realization.json"
        parsed_realization = None
        if realization_binding is None:
            if (
                realization_artifact.exists()
                or realization_artifact.is_symlink()
            ):
                raise ValueError(
                    "candidate realization exists without a project binding"
                )
        else:
            if not isinstance(realization_binding, dict):
                raise ValueError(
                    "candidate project.realization binding is invalid"
                )
            realization_source = _bound_artifact_path(
                directory,
                realization_binding.get("path", ""),
                label="realization",
            )
            realization_document, _realization_file_sha256 = (
                _candidate_json_snapshot(
                    realization_source,
                    expected_file_sha256=realization_binding.get(
                        "file_sha256"
                    ),
                    hash_mismatch_message=(
                        "candidate realization file hash mismatch"
                    ),
                    invalid_json_message=(
                        "candidate realization is invalid JSON"
                    ),
                )
            )
            if not isinstance(realization_document, dict):
                raise ValueError("candidate realization must be an object")
            realization_sha256 = canonical_json_sha256(
                realization_document
            )
            if realization_sha256 != realization_binding.get(
                "canonical_sha256"
            ):
                raise ValueError(
                    "candidate realization canonical hash mismatch"
                )
            parsed_realization = parse_realization_document(
                realization_document,
                score_document=verified_project_documents["score"],
            )
            verified_project_hashes["realization"] = realization_sha256
            verified_project_documents["realization"] = (
                realization_document
            )
        receipt_binding = document.get("render_receipt")
        if not isinstance(receipt_binding, dict):
            raise ValueError("candidate render_receipt binding is missing")
        if receipt_binding.get("path") != "渲染回执.json":
            raise ValueError(
                "candidate render_receipt must bind 渲染回执.json"
            )
        receipt = _bound_artifact_path(
            directory,
            receipt_binding.get("path", ""),
            label="render receipt",
        )
        receipt_document, _receipt_sha256 = _candidate_json_snapshot(
            receipt,
            expected_file_sha256=receipt_binding.get("sha256"),
            hash_mismatch_message="candidate render receipt hash mismatch",
            invalid_json_message="candidate render receipt is invalid JSON",
        )
        if not isinstance(receipt_document, dict):
            raise ValueError("candidate render receipt must be an object")
        receipt_workflow = receipt_document.get("authoring_workflow")
        if workflow_binding is None:
            if "authoring_workflow" in receipt_document:
                raise ValueError(
                    "candidate receipt has an unmanifested workflow binding"
                )
        elif receipt_workflow != workflow_binding:
            raise ValueError(
                "candidate manifest and receipt disagree on workflow authorization"
            )
        authoring_artifact = directory / AUTHORING_ROSTER_CANDIDATE_NAME
        if authoring_binding is None:
            if "authoring_project" in receipt_document:
                raise ValueError(
                    "candidate receipt has an unmanifested authoring binding"
                )
            if authoring_artifact.exists() or authoring_artifact.is_symlink():
                raise ValueError(
                    "candidate authoring roster exists without a manifest binding"
                )
        else:
            roster_binding = authoring_binding["authoring_roster"]
            roster_path = _bound_artifact_path(
                directory,
                roster_binding["path"],
                label="authoring roster",
            )
            authoring_roster_document, _roster_sha256 = (
                _candidate_json_snapshot(
                    roster_path,
                    expected_file_sha256=roster_binding["file_sha256"],
                    hash_mismatch_message=(
                        "candidate authoring roster file hash mismatch"
                    ),
                    invalid_json_message=(
                        "candidate authoring roster is invalid JSON"
                    ),
                )
            )
            if (
                canonical_json_sha256(authoring_roster_document)
                != roster_binding["canonical_sha256"]
            ):
                raise ValueError(
                    "candidate authoring roster canonical hash mismatch"
                )
            computed_revision = _authoring_revision_identity(
                project_id=authoring_binding["project_id"],
                score_sha256=verified_project_hashes["score"],
                authoring_roster_sha256=roster_binding["canonical_sha256"],
                render_profile_sha256=verified_project_hashes[
                    "render_profile"
                ],
            )
            if computed_revision != authoring_binding["revision"]:
                raise ValueError(
                    "candidate authoring revision binding does not match its documents"
                )
            expected_receipt_authoring = {
                "project_id": authoring_binding["project_id"],
                "revision": authoring_binding["revision"],
                "authoring_roster_canonical_sha256": roster_binding[
                    "canonical_sha256"
                ],
            }
            if receipt_document.get("authoring_project") != expected_receipt_authoring:
                raise ValueError(
                    "candidate manifest and receipt disagree on authoring identity"
                )
            _verify_authoring_formal_roster(
                authoring_roster_document,
                verified_project_documents["roster"],
            )
        telemetry_binding = document.get("cache_telemetry")
        telemetry_path = directory / CACHE_TELEMETRY_NAME
        telemetry_exists = (
            telemetry_path.exists() or telemetry_path.is_symlink()
        )
        if telemetry_binding is None and telemetry_exists:
            raise ValueError(
                "candidate cache telemetry exists without a manifest binding"
            )
        if telemetry_binding is not None:
            if not isinstance(telemetry_binding, dict):
                raise ValueError(
                    "candidate cache_telemetry binding is invalid"
                )
            if telemetry_binding.get("path") != CACHE_TELEMETRY_NAME:
                raise ValueError(
                    "candidate cache_telemetry must bind "
                    f"{CACHE_TELEMETRY_NAME}"
                )
            telemetry = _bound_artifact_path(
                directory,
                telemetry_binding.get("path", ""),
                label="cache telemetry",
            )
            if sha256_file(telemetry) != telemetry_binding.get(
                "sha256"
            ):
                raise ValueError(
                    "candidate cache telemetry hash mismatch"
                )
        verify_render_generation(directory)
        performance_plan = receipt_document.get("performance_plan")
        if (
            not isinstance(performance_plan, dict)
            or document["project"].get("performance_plan_sha256")
            != performance_plan.get("sha256")
        ):
            raise ValueError(
                "candidate manifest and render receipt disagree on plan Hash"
            )
        plan_document = _receipt_performance_plan(
            directory,
            receipt_document,
            expected_sha256=document["project"][
                "performance_plan_sha256"
            ],
        )
        plan_realization = plan_document.get("realization")
        if parsed_realization is None:
            if plan_realization is not None:
                raise ValueError(
                    "candidate performance plan has an unmanifested "
                    "realization binding"
                )
        elif parsed_realization.is_noop:
            if plan_realization is not None:
                raise ValueError(
                    "candidate no-op realization unexpectedly changed the "
                    "performance plan"
                )
        else:
            expected_plan_realization = {
                "kind": parsed_realization.kind,
                "schema_version": parsed_realization.schema_version,
                "score_sha256": parsed_realization.score_sha256,
                "canonical_sha256": verified_project_hashes[
                    "realization"
                ],
                "defaults_profile": parsed_realization.defaults_profile,
                "mode": parsed_realization.mode,
            }
            if plan_realization != expected_plan_realization:
                raise ValueError(
                    "candidate performance plan realization binding "
                    "disagrees with realization.json"
                )
        if authoring_binding is not None:
            _verify_formal_roster_plan(
                verified_project_documents["roster"],
                plan_document,
            )
    return directory, document


def _playback_map_json_snapshot(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, Any], str]:
    document, digest = _candidate_json_snapshot(
        path,
        invalid_json_message=(
            f"candidate playback map {label} is not valid UTF-8 JSON"
        ),
        map_read_error_to_invalid_json=True,
    )
    if not isinstance(document, dict):
        raise ValueError(f"candidate playback map {label} must be an object")
    return document, digest


def _playback_map_file_snapshot(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                size_bytes += len(block)
    except OSError as exc:
        raise ValueError(
            "candidate playback map rendered mix could not be read"
        ) from exc
    return digest.hexdigest(), size_bytes


def _verified_plan(
    directory: Path,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if candidate.get("version") == SCORE_V2_CANDIDATE_VERSION:
        raise ValueError(
            "candidate playback/locate does not yet support Score-v2 Candidate v3"
        )
    receipt_binding = candidate["render_receipt"]
    receipt_path = _bound_artifact_path(
        directory,
        receipt_binding["path"],
        label="render receipt",
    )
    receipt, receipt_sha256 = _playback_map_json_snapshot(
        receipt_path,
        label="render receipt",
    )
    if receipt_sha256 != receipt_binding.get("sha256"):
        raise ValueError("candidate render receipt hash mismatch")
    plan_binding = receipt.get("performance_plan")
    if not isinstance(plan_binding, dict):
        raise ValueError("render receipt has no performance_plan binding")
    plan_path = _bound_artifact_path(
        directory,
        plan_binding.get("path", ""),
        label="performance plan",
    )
    plan, plan_file_sha256 = _playback_map_json_snapshot(
        plan_path,
        label="performance plan",
    )
    if plan_file_sha256 != plan_binding.get("file_sha256"):
        raise ValueError("candidate performance plan file hash mismatch")
    if canonical_json_sha256(plan) != plan_binding.get("sha256"):
        raise ValueError("candidate performance plan canonical hash mismatch")
    if (
        candidate.get("project", {}).get("performance_plan_sha256")
        != plan_binding.get("sha256")
    ):
        raise ValueError("candidate manifest and receipt disagree on plan Hash")
    return plan, receipt


def _playback_map_finite_number(value: object, *, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"candidate playback map {label} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"candidate playback map {label} must be a finite number"
        ) from exc
    if not math.isfinite(number):
        raise ValueError(f"candidate playback map {label} must be a finite number")
    return number


def _playback_map_positive_integer(value: object, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(
            f"candidate playback map {label} must be a positive integer"
        )
    return value


def _playback_map_required_string(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"candidate playback map {label} must be a non-empty string"
        )
    return value


def _playback_map_frame(
    seconds: object,
    *,
    sample_rate: int,
    label: str,
) -> tuple[float, int]:
    time_seconds = _playback_map_finite_number(seconds, label=label)
    if time_seconds < 0.0:
        raise ValueError(
            f"candidate playback map {label} must not be negative"
        )
    return time_seconds, round(time_seconds * sample_rate)


def _playback_map_trace(
    trace: object,
    *,
    label: str,
    stable_identity: bool,
) -> dict[str, Any]:
    if not isinstance(trace, dict):
        raise ValueError(f"candidate playback map {label} must be an object")
    if stable_identity:
        bar: int | None = _playback_map_positive_integer(
            trace.get("小节"),
            label=f"{label}.小节",
        )
        beat: float | None = _playback_map_finite_number(
            trace.get("拍"),
            label=f"{label}.拍",
        )
        if beat < 1.0:
            raise ValueError(
                f"candidate playback map {label}.拍 must be at least 1"
            )
    else:
        # Legacy scores have no event identity that can bind these notation
        # coordinates to the scheduled note.  Keep the candidate usable, but
        # do not present positional trace labels as verified score facts.
        bar = None
        beat = None
    sounding_pitch = _playback_map_required_string(
        trace.get("音"),
        label=f"{label}.音",
    )
    articulation = trace.get("奏法")
    if articulation is not None and not isinstance(articulation, str):
        raise ValueError(
            f"candidate playback map {label}.奏法 must be a string or null"
        )
    raw_velocity = trace.get("力度")
    resolved_velocity = (
        None
        if raw_velocity is None
        else _playback_map_finite_number(
            raw_velocity,
            label=f"{label}.力度",
        )
    )
    if resolved_velocity is not None and not 0.0 <= resolved_velocity <= 1.0:
        raise ValueError(
            f"candidate playback map {label}.力度 must be within [0, 1]"
        )
    return {
        "bar": bar,
        "beat": beat,
        "sounding_pitch": sounding_pitch,
        "resolved_articulation": articulation,
        "resolved_velocity": resolved_velocity,
    }


def _playback_map_candidate_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    title = candidate.get("title")
    if not isinstance(title, str):
        raise ValueError("candidate playback map candidate.title must be a string")
    created_at_utc = candidate.get("created_at_utc")
    try:
        _parse_bounded_candidate_datetime(created_at_utc)
    except ValueError as exc:
        raise ValueError(
            "candidate playback map candidate.created_at_utc must be an "
            "RFC 3339 date-time"
        ) from exc
    parent_candidate_id = candidate.get("parent_candidate_id")
    if parent_candidate_id is not None and not isinstance(
        parent_candidate_id,
        str,
    ):
        raise ValueError(
            "candidate playback map candidate.parent_candidate_id must be "
            "a string or null"
        )
    return {
        "candidate_id": candidate["candidate_id"],
        "work_id": candidate["work_id"],
        "title": title,
        "created_at_utc": created_at_utc,
        "parent_candidate_id": parent_candidate_id,
    }


def build_candidate_playback_map(
    path: str | Path,
    *,
    expected_work_id: str | None = None,
    expected_candidate_id: str | None = None,
) -> dict[str, Any]:
    """Build one verified, frame-addressable note map for candidate playback.

    This is deliberately a one-shot boundary.  It verifies the immutable
    candidate generation once, pairs exact performance ``note_on``/``note_off``
    events, and projects only the trace fields a playback UI needs.  Callers
    should index the returned rows locally instead of repeatedly invoking
    :func:`locate_candidate` while the playhead advances.

    ``note_off.frame`` is an exclusive gate boundary.  Acoustic sample release,
    resonance, and shared-space tails remain deliberately outside this map.
    The public hard limit prevents a locally supplied candidate from making
    this materialised JSON result an unbounded memory allocation.
    """

    directory, candidate = load_candidate(
        path,
        verify=True,
        expected_work_id=expected_work_id,
        expected_candidate_id=expected_candidate_id,
    )
    manifest_path = directory / CANDIDATE_MANIFEST_NAME
    manifest_snapshot, manifest_sha256 = _playback_map_json_snapshot(
        manifest_path,
        label="candidate manifest",
    )
    if manifest_snapshot != candidate:
        raise ValueError(
            "candidate playback map candidate manifest changed during verification"
        )
    candidate_metadata = _playback_map_candidate_metadata(manifest_snapshot)
    plan, receipt = _verified_plan(directory, candidate)

    audio_format = receipt.get("audio_format")
    mix_binding = receipt.get("mix")
    if not isinstance(audio_format, dict) or not isinstance(mix_binding, dict):
        raise ValueError(
            "candidate playback map render receipt lacks audio_format or mix"
        )
    sample_rate = _playback_map_positive_integer(
        audio_format.get("sample_rate"),
        label="audio_format.sample_rate",
    )
    if not 8_000 <= sample_rate <= 384_000:
        raise ValueError(
            "candidate playback map audio_format.sample_rate is unsupported"
        )
    frame_count = _playback_map_positive_integer(
        mix_binding.get("frame_count"),
        label="mix.frame_count",
    )
    plan_sample_rate = _playback_map_positive_integer(
        plan.get("sample_rate"),
        label="performance_plan.sample_rate",
    )
    if plan_sample_rate != sample_rate:
        raise ValueError(
            "candidate playback map plan and rendered mix sample rates disagree"
        )
    plan_duration_seconds = _playback_map_finite_number(
        plan.get("duration_seconds"),
        label="performance_plan.duration_seconds",
    )
    if plan_duration_seconds < 0.0:
        raise ValueError(
            "candidate playback map performance_plan.duration_seconds "
            "must not be negative"
        )
    dry_frame_count = max(1, round(plan_duration_seconds * sample_rate))

    space = receipt.get("space")
    if not isinstance(space, dict) or not isinstance(space.get("enabled"), bool):
        raise ValueError(
            "candidate playback map render receipt has no valid space contract"
        )
    if space["enabled"]:
        effective_tail_seconds = _playback_map_finite_number(
            space.get("effective_tail_seconds"),
            label="space.effective_tail_seconds",
        )
        if effective_tail_seconds < 0.0:
            raise ValueError(
                "candidate playback map space.effective_tail_seconds "
                "must not be negative"
            )
        expected_mix_frame_count = dry_frame_count + max(
            0,
            math.ceil(effective_tail_seconds * sample_rate),
        )
    else:
        expected_mix_frame_count = dry_frame_count
    if frame_count != expected_mix_frame_count:
        raise ValueError(
            "candidate playback map rendered mix frame count disagrees with "
            "the performance-plan duration and space tail"
        )

    project = candidate.get("project")
    if not isinstance(project, dict):
        raise ValueError("candidate playback map candidate project binding is missing")
    score_binding = project.get("score")
    if not isinstance(score_binding, dict):
        raise ValueError("candidate playback map candidate score binding is missing")
    score_path = _bound_artifact_path(
        directory,
        score_binding.get("path", ""),
        label="score",
    )
    score_document, score_file_sha256 = _playback_map_json_snapshot(
        score_path,
        label="score",
    )
    if score_file_sha256 != score_binding.get("file_sha256"):
        raise ValueError("candidate playback map score file hash mismatch")
    if canonical_json_sha256(score_document) != score_binding.get(
        "canonical_sha256"
    ):
        raise ValueError("candidate playback map score canonical hash mismatch")
    parsed_score = parse_score_document(score_document)
    source_notes = {
        note.source_event_id: (part.id, note)
        for part in parsed_score.parts
        for note in part.notes
        if note.source_event_id is not None
    }
    score_event_count = sum(len(part.notes) for part in parsed_score.parts)
    score_part_ids = {part.id for part in parsed_score.parts}

    raw_parts = plan.get("parts")
    if not isinstance(raw_parts, list):
        raise ValueError(
            "candidate playback map performance_plan.parts must be an array"
        )

    scheduled_note_count = 0
    for part_index, raw_part in enumerate(raw_parts):
        if not isinstance(raw_part, dict):
            raise ValueError(
                f"candidate playback map performance_plan.parts[{part_index}] "
                "must be an object"
            )
        performance = raw_part.get("performance")
        if not isinstance(performance, dict):
            raise ValueError(
                f"candidate playback map performance_plan.parts[{part_index}] "
                "lacks a performance object"
            )
        raw_events = performance.get("events")
        if not isinstance(raw_events, list):
            raise ValueError(
                f"candidate playback map performance_plan.parts[{part_index}]."
                "performance.events must be an array"
            )
        scheduled_note_count += sum(
            1
            for event in raw_events
            if isinstance(event, dict) and event.get("type") == "note_on"
        )
        if scheduled_note_count > MAX_PLAYBACK_MAP_SCHEDULED_NOTES:
            raise ValueError(
                "candidate playback map scheduled note count "
                f"{scheduled_note_count} exceeds hard limit "
                f"{MAX_PLAYBACK_MAP_SCHEDULED_NOTES}"
            )

    rows: list[dict[str, Any]] = []
    stable_identity_count = 0
    seen_executor_ids: set[str] = set()
    seen_source_event_ids: set[str] = set()
    for part_index, raw_part in enumerate(raw_parts):
        part_label = f"performance_plan.parts[{part_index}]"
        executor_id = _playback_map_required_string(
            raw_part.get("executor_id"),
            label=f"{part_label}.executor_id",
        )
        if executor_id in seen_executor_ids:
            raise ValueError(
                f"candidate playback map duplicate executor_id {executor_id!r}"
            )
        seen_executor_ids.add(executor_id)
        part_id = _playback_map_required_string(
            raw_part.get("part_id"),
            label=f"{part_label}.part_id",
        )
        if part_id not in score_part_ids:
            raise ValueError(
                f"candidate playback map plan references unknown score part {part_id!r}"
            )
        instrument = _playback_map_required_string(
            raw_part.get("instrument"),
            label=f"{part_label}.instrument",
        )
        performance = raw_part["performance"]
        parsed_performance = parse_performance_document(performance)
        if parsed_performance.sample_rate != sample_rate:
            raise ValueError(
                f"candidate playback map {part_label} sample rate disagrees "
                "with the rendered mix"
            )
        if parsed_performance.total_samples != dry_frame_count:
            raise ValueError(
                f"candidate playback map {part_label} duration disagrees "
                "with the performance-plan duration"
            )

        traces = raw_part.get("trace")
        if not isinstance(traces, list):
            raise ValueError(
                f"candidate playback map {part_label}.trace must be an array"
            )
        traces_by_source: dict[str, tuple[int, dict[str, Any]]] = {}
        for trace_index, trace in enumerate(traces):
            if not isinstance(trace, dict):
                raise ValueError(
                    f"candidate playback map {part_label}.trace[{trace_index}] "
                    "must be an object"
                )
            source_event_id = trace.get("source_event_id")
            if source_event_id is None:
                continue
            source_event_id = _playback_map_required_string(
                source_event_id,
                label=f"{part_label}.trace[{trace_index}].source_event_id",
            )
            if source_event_id in traces_by_source:
                raise ValueError(
                    "candidate playback map trace contains duplicate "
                    f"source_event_id {source_event_id!r}"
                )
            traces_by_source[source_event_id] = (trace_index, trace)

        raw_events = performance["events"]
        active: dict[
            int,
            tuple[dict[str, Any], int, str | None],
        ] = {}
        current_articulation: str | None = None
        consumed_trace_indexes: set[int] = set()
        for event_index, raw_event in enumerate(raw_events):
            if not isinstance(raw_event, dict):
                # parse_performance_document has already diagnosed the exact
                # shape, but retain a fail-closed guard for type checkers.
                raise ValueError(
                    f"candidate playback map {part_label}.performance.events"
                    f"[{event_index}] must be an object"
                )
            event_type = raw_event.get("type")
            if event_type == "articulation":
                current_articulation = _playback_map_required_string(
                    raw_event.get("name"),
                    label=(
                        f"{part_label}.performance.events[{event_index}].name"
                    ),
                )
                continue
            if event_type not in {"note_on", "note_off"}:
                continue
            note_id = _playback_map_positive_integer(
                raw_event.get("note_id"),
                label=(
                    f"{part_label}.performance.events[{event_index}].note_id"
                ),
            )
            if event_type == "note_on":
                if note_id in active:
                    raise ValueError(
                        f"candidate playback map note_id {note_id} is already active "
                        f"for executor {executor_id!r}"
                    )
                active[note_id] = (
                    raw_event,
                    event_index,
                    current_articulation,
                )
                continue

            opened = active.pop(note_id, None)
            if opened is None:
                raise ValueError(
                    f"candidate playback map note_off {note_id} has no active note_on "
                    f"for executor {executor_id!r}"
                )
            note_on, note_on_index, scheduled_articulation = opened
            source_event_id = note_on.get("source_event_id")
            if source_event_id is not None:
                source_event_id = _playback_map_required_string(
                    source_event_id,
                    label=(
                        f"{part_label}.performance.events[{note_on_index}]."
                        "source_event_id"
                    ),
                )
            if raw_event.get("source_event_id") != source_event_id:
                raise ValueError(
                    "candidate playback map note_on/note_off source_event_id mismatch"
                )

            if parsed_score.has_stable_event_identity:
                if source_event_id is None:
                    raise ValueError(
                        "candidate playback map score v1 scheduled note lacks "
                        "source_event_id"
                    )
                source_note = source_notes.get(source_event_id)
                if source_note is None:
                    raise ValueError(
                        "candidate playback map scheduled note references unknown "
                        f"score event {source_event_id!r}"
                    )
                source_part_id, parsed_source_note = source_note
                if source_part_id != part_id:
                    raise ValueError(
                        "candidate playback map scheduled note source part disagrees "
                        f"for event {source_event_id!r}"
                    )
                if source_event_id in seen_source_event_ids:
                    raise ValueError(
                        "candidate playback map scheduled source_event_id is duplicated: "
                        f"{source_event_id!r}"
                    )
                seen_source_event_ids.add(source_event_id)
                trace_candidate = traces_by_source.get(source_event_id)
                if trace_candidate is None:
                    raise ValueError(
                        "candidate playback map has no trace for score event "
                        f"{source_event_id!r}"
                    )
                trace_index, trace = trace_candidate
                stable_identity = True
                stable_identity_count += 1
            else:
                parsed_source_note = None
                if source_event_id is not None:
                    raise ValueError(
                        "candidate playback map legacy score must not expose a "
                        "source_event_id"
                    )
                trace_index = note_id - 1
                if not 0 <= trace_index < len(traces):
                    raise ValueError(
                        "candidate playback map legacy note_id has no positional trace"
                    )
                trace = traces[trace_index]
                if trace.get("source_event_id") is not None:
                    raise ValueError(
                        "candidate playback map legacy trace must not expose a "
                        "source_event_id"
                    )
                stable_identity = False
            if trace_index in consumed_trace_indexes:
                raise ValueError(
                    f"candidate playback map trace[{trace_index}] is used more than once "
                    f"for executor {executor_id!r}"
                )
            consumed_trace_indexes.add(trace_index)

            start_seconds, start_frame = _playback_map_frame(
                note_on.get("time"),
                sample_rate=sample_rate,
                label=f"{part_label}.performance.events[{note_on_index}].time",
            )
            end_seconds, end_frame = _playback_map_frame(
                raw_event.get("time"),
                sample_rate=sample_rate,
                label=f"{part_label}.performance.events[{event_index}].time",
            )
            if (
                end_seconds <= start_seconds
                or end_frame <= start_frame
                or start_frame >= frame_count
                or end_frame > frame_count
            ):
                raise ValueError(
                    "candidate playback map scheduled gate is empty or outside "
                    "the rendered mix timeline"
                )

            trace_time = trace.get("时间")
            if trace_time is not None:
                resolved_trace_time = _playback_map_finite_number(
                    trace_time,
                    label=f"{part_label}.trace[{trace_index}].时间",
                )
                if not math.isclose(
                    resolved_trace_time,
                    start_seconds,
                    rel_tol=0.0,
                    abs_tol=_PLAYBACK_MAP_TRACE_TIME_TOLERANCE,
                ):
                    raise ValueError(
                        "candidate playback map trace start disagrees with exact "
                        "performance note_on"
                    )
            trace_duration = trace.get("时长")
            if trace_duration is not None:
                resolved_trace_duration = _playback_map_finite_number(
                    trace_duration,
                    label=f"{part_label}.trace[{trace_index}].时长",
                )
                if not math.isclose(
                    resolved_trace_duration,
                    end_seconds - start_seconds,
                    rel_tol=0.0,
                    abs_tol=_PLAYBACK_MAP_TRACE_TIME_TOLERANCE,
                ):
                    raise ValueError(
                        "candidate playback map trace duration disagrees with exact "
                        "performance note gate"
                    )
            projected_trace = _playback_map_trace(
                trace,
                label=f"{part_label}.trace[{trace_index}]",
                stable_identity=stable_identity,
            )

            velocity = _playback_map_finite_number(
                note_on.get("velocity"),
                label=(
                    f"{part_label}.performance.events[{note_on_index}].velocity"
                ),
            )
            note_on_result: dict[str, Any] = {
                "seconds": start_seconds,
                "frame": start_frame,
                "velocity": velocity,
            }
            if "midi_note" in note_on:
                note_on_result["midi_note"] = _playback_map_finite_number(
                    note_on["midi_note"],
                    label=(
                        f"{part_label}.performance.events[{note_on_index}]."
                        "midi_note"
                    ),
                )
            elif "pitch_hz" in note_on:
                pitch_hz = _playback_map_finite_number(
                    note_on["pitch_hz"],
                    label=(
                        f"{part_label}.performance.events[{note_on_index}]."
                        "pitch_hz"
                    ),
                )
                if pitch_hz <= 0.0:
                    raise ValueError(
                        "candidate playback map note_on.pitch_hz must be positive"
                    )
                note_on_result["pitch_hz"] = pitch_hz
            else:  # parse_performance_document already rejects this branch.
                raise ValueError(
                    "candidate playback map note_on lacks sounding pitch"
                )
            if "midi_note" in note_on_result:
                expected_sounding_pitch = pitch_name(
                    note_on_result["midi_note"]
                )
            else:
                expected_sounding_pitch = pitch_name(
                    69.0
                    + 12.0
                    * math.log2(
                        note_on_result["pitch_hz"]
                        / parsed_performance.tuning.a4_hz
                    )
                )
            if projected_trace["sounding_pitch"] != expected_sounding_pitch:
                raise ValueError(
                    "candidate playback map trace sounding pitch disagrees "
                    "with performance note_on"
                )
            trace_velocity = projected_trace["resolved_velocity"]
            if trace_velocity is not None and not math.isclose(
                trace_velocity,
                velocity,
                rel_tol=0.0,
                abs_tol=_PLAYBACK_MAP_TRACE_VELOCITY_TOLERANCE,
            ):
                raise ValueError(
                    "candidate playback map trace velocity disagrees with "
                    "performance note_on"
                )
            if (
                projected_trace["resolved_articulation"]
                != scheduled_articulation
            ):
                raise ValueError(
                    "candidate playback map trace articulation disagrees with "
                    "the performance articulation state"
                )
            if parsed_source_note is not None and (
                projected_trace["bar"] != parsed_source_note.bar
                or not math.isclose(
                    projected_trace["beat"],
                    parsed_source_note.beat,
                    rel_tol=0.0,
                    abs_tol=1.0e-9,
                )
            ):
                raise ValueError(
                    "candidate playback map trace score position disagrees "
                    f"for event {source_event_id!r}"
                )
            note_off_result: dict[str, Any] = {
                "seconds": end_seconds,
                "frame": end_frame,
            }
            if "release_velocity" in raw_event:
                note_off_result["release_velocity"] = (
                    _playback_map_finite_number(
                        raw_event["release_velocity"],
                        label=(
                            f"{part_label}.performance.events[{event_index}]."
                            "release_velocity"
                        ),
                    )
                )
            rows.append(
                {
                    "executor_id": executor_id,
                    "part_id": part_id,
                    "instrument": instrument,
                    "note_id": note_id,
                    "source_event_id": source_event_id,
                    "stable_identity": stable_identity,
                    "note_on": note_on_result,
                    "note_off": note_off_result,
                    "trace": projected_trace,
                }
            )
        if active:
            raise ValueError(
                "candidate playback map performance contains note_on events "
                f"without note_off for executor {executor_id!r}"
            )
        if len(consumed_trace_indexes) != len(traces):
            raise ValueError(
                "candidate playback map performance and trace note counts disagree "
                f"for executor {executor_id!r}"
            )

    rows.sort(
        key=lambda row: (
            row["note_on"]["frame"],
            row["note_on"]["seconds"],
            row["executor_id"],
            row["note_id"],
        )
    )

    receipt_binding = candidate.get("render_receipt")
    plan_binding = receipt.get("performance_plan")
    if not isinstance(receipt_binding, dict) or not isinstance(plan_binding, dict):
        raise ValueError(
            "candidate playback map candidate receipt or plan binding is missing"
        )
    mix_path = _bound_artifact_path(
        directory,
        mix_binding.get("path", ""),
        label="mix",
    )
    mix_sha256, mix_size_bytes = _playback_map_file_snapshot(mix_path)
    if mix_size_bytes < 1:
        raise ValueError("candidate playback map rendered mix must not be empty")
    if mix_sha256 != mix_binding.get("sha256"):
        raise ValueError("candidate playback map rendered mix hash mismatch")

    return {
        "$schema": PLAYBACK_MAP_SCHEMA_URI,
        "kind": PLAYBACK_MAP_KIND,
        "schema_version": PLAYBACK_MAP_VERSION,
        "candidate": candidate_metadata,
        "bindings": {
            "candidate_manifest": {
                "path": CANDIDATE_MANIFEST_NAME,
                "sha256": manifest_sha256,
            },
            "score": {
                "path": str(score_binding["path"]),
                "canonical_sha256": str(score_binding["canonical_sha256"]),
                "file_sha256": str(score_binding["file_sha256"]),
            },
            "performance_plan": {
                "path": str(plan_binding["path"]),
                "canonical_sha256": str(plan_binding["sha256"]),
                "file_sha256": str(plan_binding["file_sha256"]),
            },
            "render_receipt": {
                "path": str(receipt_binding["path"]),
                "sha256": str(receipt_binding["sha256"]),
            },
            "mix": {
                "path": str(mix_binding["path"]),
                "sha256": str(mix_binding["sha256"]),
                "size_bytes": mix_size_bytes,
            },
        },
        "timeline": {
            "basis": "scheduled_note_gate",
            "sample_rate": sample_rate,
            "frame_count": frame_count,
            "duration_seconds": frame_count / sample_rate,
            "sample_rounding": "python_round_ties_to_even",
            "note_off_frame_exclusive": True,
            "audible_release_or_space_tail_exact": False,
        },
        "limits": {
            "max_scheduled_note_count": MAX_PLAYBACK_MAP_SCHEDULED_NOTES,
        },
        "events": rows,
        "summary": {
            "score_schema_version": parsed_score.schema_version,
            "score_event_count": score_event_count,
            "scheduled_note_count": len(rows),
            "stable_identity_count": stable_identity_count,
            "legacy_unstable_identity_count": len(rows) - stable_identity_count,
            "executor_count": len(raw_parts),
        },
    }


def locate_candidate(
    path: str | Path,
    *,
    at_seconds: float,
    tail_lookback_seconds: float = 5.0,
    upcoming_seconds: float = 2.0,
    max_events: int = 128,
) -> dict[str, Any]:
    """Locate what the user actually heard from the saved render generation."""

    values = (at_seconds, tail_lookback_seconds, upcoming_seconds)
    if any(
        isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0.0
        for value in values
    ):
        raise ValueError("candidate locate times must be finite and non-negative")
    if (
        isinstance(max_events, bool)
        or not isinstance(max_events, int)
        or not 1 <= max_events <= 1024
    ):
        raise ValueError("max_events must be between 1 and 1024")
    at = float(at_seconds)
    directory, candidate = load_candidate(path)
    plan, receipt = _verified_plan(directory, candidate)
    duration = (
        float(receipt["mix"]["frame_count"])
        / float(receipt["audio_format"]["sample_rate"])
    )
    if at > duration:
        raise ValueError("at_seconds exceeds the rendered candidate duration")
    active: list[dict[str, Any]] = []
    possible_tails: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []
    for part in plan.get("parts", []):
        if not isinstance(part, dict):
            continue
        identity = {
            "executor_id": part.get("executor_id"),
            "part_id": part.get("part_id"),
            "instrument": part.get("instrument"),
        }
        for trace in part.get("trace", []):
            if not isinstance(trace, dict):
                continue
            start = float(trace.get("时间", 0.0))
            end = start + float(trace.get("时长", 0.0))
            row = {
                **identity,
                "source_event_id": trace.get("source_event_id"),
                "start_seconds": start,
                "gate_end_seconds": end,
                "bar": trace.get("小节"),
                "beat": trace.get("拍"),
                "pitch": trace.get("音"),
                "articulation": trace.get("奏法"),
            }
            if start <= at < end:
                active.append(row)
            elif end <= at and end >= at - float(tail_lookback_seconds):
                possible_tails.append(row)
            elif at < start <= at + float(upcoming_seconds):
                upcoming.append(row)
    key = lambda row: (
        float(row["start_seconds"]),
        str(row.get("executor_id")),
        str(row.get("source_event_id")),
    )
    active.sort(key=key)
    possible_tails.sort(key=key, reverse=True)
    upcoming.sort(key=key)
    total = len(active) + len(possible_tails) + len(upcoming)
    remaining = max_events

    def take(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal remaining
        selected = rows[:remaining]
        remaining -= len(selected)
        return selected

    returned_active = take(active)
    returned_tails = take(possible_tails)
    returned_upcoming = take(upcoming)
    return {
        "kind": "tianlai.candidate_locate_result",
        "schema_version": 1,
        "ok": True,
        "candidate_id": candidate["candidate_id"],
        "candidate_directory": str(directory),
        "at_seconds": at,
        "rendered_duration_seconds": duration,
        "performance_plan_sha256": candidate["project"][
            "performance_plan_sha256"
        ],
        "score_sha256": candidate["project"]["score"][
            "canonical_sha256"
        ],
        "roster_sha256": candidate["project"]["roster"][
            "canonical_sha256"
        ],
        "active_events": returned_active,
        "possible_release_or_space_sources": returned_tails,
        "upcoming_events": returned_upcoming,
        "summary": {
            "matched_event_count": total,
            "returned_event_count": (
                len(returned_active)
                + len(returned_tails)
                + len(returned_upcoming)
            ),
            "truncated": total > max_events,
        },
        "tail_semantics": (
            "possible_release_or_space_sources 只列查询点前的候选贡献源；"
            "采样释音、共鸣与共享厅堂并无逐样本因果证明，不能据此断言仍可听见。"
        ),
    }


def compare_candidates(
    before_path: str | Path,
    after_path: str | Path,
    *,
    max_changes: int = 256,
) -> dict[str, Any]:
    before_directory, before = load_candidate(before_path)
    after_directory, after = load_candidate(after_path)
    if (
        before.get("version") == SCORE_V2_CANDIDATE_VERSION
        or after.get("version") == SCORE_V2_CANDIDATE_VERSION
    ):
        raise ValueError(
            "candidate comparison does not yet support Score-v2 Candidate v3"
        )

    def snapshot(
        directory: Path,
        binding: dict[str, Any],
        *,
        label: str,
        hash_key: str,
        hash_mismatch_message: str,
    ) -> dict[str, Any]:
        document, _digest = _candidate_json_snapshot(
            _bound_artifact_path(
                directory,
                binding.get("path", ""),
                label=label,
            ),
            expected_file_sha256=binding.get(hash_key),
            hash_mismatch_message=hash_mismatch_message,
            invalid_json_message=f"candidate {label} is invalid JSON",
        )
        if not isinstance(document, dict):
            raise ValueError(f"candidate {label} must be an object")
        return document

    before_score = snapshot(
        before_directory,
        before["project"]["score"],
        label="score",
        hash_key="file_sha256",
        hash_mismatch_message="candidate score file hash mismatch",
    )
    after_score = snapshot(
        after_directory,
        after["project"]["score"],
        label="score",
        hash_key="file_sha256",
        hash_mismatch_message="candidate score file hash mismatch",
    )
    before_receipt = snapshot(
        before_directory,
        before["render_receipt"],
        label="render receipt",
        hash_key="sha256",
        hash_mismatch_message="candidate render receipt hash mismatch",
    )
    after_receipt = snapshot(
        after_directory,
        after["render_receipt"],
        label="render receipt",
        hash_key="sha256",
        hash_mismatch_message="candidate render receipt hash mismatch",
    )
    score_diff = compare_scores(
        before_score,
        after_score,
        max_changes=max_changes,
    )
    return {
        "kind": "tianlai.candidate_compare_result",
        "schema_version": 1,
        "ok": True,
        "before_candidate_id": before["candidate_id"],
        "after_candidate_id": after["candidate_id"],
        "parent_relationship": (
            after.get("parent_candidate_id") == before["candidate_id"]
        ),
        "score": score_diff,
        "roster_changed": (
            before["project"]["roster"]["canonical_sha256"]
            != after["project"]["roster"]["canonical_sha256"]
        ),
        "render_profile_changed": (
            before["project"]["render_profile"]["canonical_sha256"]
            != after["project"]["render_profile"]["canonical_sha256"]
        ),
        "performance_plan_changed": (
            before["project"]["performance_plan_sha256"]
            != after["project"]["performance_plan_sha256"]
        ),
        "mix_sha256": {
            "before": before_receipt["mix"]["sha256"],
            "after": after_receipt["mix"]["sha256"],
        },
    }


__all__ = [
    "CANDIDATE_FORMAT",
    "CANDIDATE_MANIFEST_NAME",
    "CANDIDATE_VERSION",
    "SCORE_V2_CANDIDATE_VERSION",
    "MAX_CANDIDATE_JSON_BYTES",
    "MAX_PLAYBACK_MAP_SCHEDULED_NOTES",
    "PLAYBACK_MAP_KIND",
    "PLAYBACK_MAP_SCHEMA_URI",
    "PLAYBACK_MAP_VERSION",
    "CandidateTarget",
    "CandidateAlreadyExistsError",
    "build_candidate_playback_map",
    "canonical_json_sha256",
    "candidate_publication",
    "compare_candidates",
    "load_candidate",
    "locate_candidate",
    "new_candidate_id",
    "portable_directory_name",
    "portable_slug",
    "prepare_candidate_target",
    "publish_candidate_metadata",
    "publish_score_v2_candidate_metadata",
    "sha256_file",
    "validate_candidate_json_size",
]
