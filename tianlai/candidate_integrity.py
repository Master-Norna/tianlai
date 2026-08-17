"""Closed-generation, descriptor-bound verification for candidates.

The normal candidate loader intentionally retains its historical, permissive
read contract.  This module provides the stricter boundary used when a caller
needs evidence that all bytes read from one candidate form a closed and
internally consistent generation.

The result binds the generation captured through verified descriptors and the
directory entry sets observed during the read.  A portable filesystem cannot
freeze an entire tree against an uncooperative concurrent writer, and the
report does not claim that the live tree remains unchanged after return.  It
is not a signature and does not establish authorship, provenance, or trust in
the machine that produced the files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any
import unicodedata

from .authoring_json import AuthoringJsonLimits, strict_json_loads
from .candidate import (
    AUTHORING_ROSTER_CANDIDATE_NAME,
    CANDIDATE_FORMAT,
    CANDIDATE_MANIFEST_NAME,
    CANDIDATE_VERSION,
    _authoring_revision_identity,
    _candidate_authoring_manifest_binding,
    _parse_bounded_candidate_datetime,
    _verify_authoring_formal_roster,
    _verify_formal_roster_plan,
    canonical_json_sha256,
)
from .canonical_json import canonical_json_bytes
from .score_v2_candidate import (
    SCORE_V2_CANDIDATE_VERSION,
    SCORE_V2_RENDER_RECEIPT_NAME,
    ScoreV2CandidateArtifact,
    ScoreV2CandidateError,
    score_v2_candidate_expected_files,
    validate_score_v2_candidate_generation,
    validate_score_v2_candidate_manifest,
    validate_score_v2_render_receipt,
)
from .collaboration_report import MIX_REPORT_FORMAT
from .ensemble import _plan_document_has_explicit_expected_activity
from .plain_file import (
    PlainFileIdentity,
    read_plain_file_bytes,
    revalidate_plain_file,
    sha256_plain_file,
    sha256_plain_file_prefix,
)
from .portable_filename import is_windows_reserved_filename
from .post_render_check import (
    POST_RENDER_CHECK_NAME,
    REPORT_FORMAT as POST_RENDER_CHECK_FORMAT,
    REPORT_VERSION as POST_RENDER_CHECK_VERSION,
    require_post_render_check_pass,
)
from .render_lock import (
    PlainDirectoryIdentity,
    capture_plain_directory,
    revalidate_plain_directory,
)
from .realization import parse_realization_document
from .utc_timestamp import validate_canonical_utc_timestamp
from .workflow_binding import validate_workflow_authorization


CANDIDATE_VERIFY_RESULT_KIND = "tianlai.candidate_verify_result"
CANDIDATE_VERIFY_RESULT_VERSION = 1

_SUPPORTED_CANDIDATE_VERSIONS = frozenset(
    {1, CANDIDATE_VERSION, SCORE_V2_CANDIDATE_VERSION}
)
_SUPPORTED_RECEIPT_VERSIONS = frozenset({2, 3})
# Mix report v1 was published inside otherwise-valid candidate/receipt v2
# generations before the current v2 diagnostic schema shipped.  The strict
# verifier preserves both released identities; the receipt still binds the
# exact report version, mode, and bytes.
_SUPPORTED_MIX_REPORT_VERSIONS = frozenset({1, 2})
_RENDER_RECEIPT_NAME = "渲染回执.json"
_CACHE_TELEMETRY_NAME = "缓存遥测.json"
_CACHE_TELEMETRY_FORMAT = "tianlai.render_cache_telemetry"
_CACHE_TELEMETRY_VERSION = 1
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_JSON_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_CANDIDATE_FILES = 4096
_MAX_RELATIVE_PATH_BYTES = 4096
_MAX_PATH_COMPONENTS = 64
_MAX_COMPONENT_BYTES = 255
_REPARSE_POINT = 0x400
_JSON_LIMITS = AuthoringJsonLimits(
    max_document_bytes=_MAX_JSON_BYTES,
    max_depth=128,
    max_nodes=2_000_000,
    max_string_bytes=4 * 1024 * 1024,
    max_array_items=500_000,
    max_object_members=65_536,
)


class CandidateIntegrityError(ValueError):
    """One stable, user-facing candidate integrity failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"candidate_integrity.{code}: {message}")


@dataclass(frozen=True, slots=True)
class _Artifact:
    relative_path: str
    identity: PlainFileIdentity
    sha256: str
    payload: bytes | None
    prefix: bytes | None = None

    @property
    def size(self) -> int:
        return self.identity.size


def _fail(code: str, message: str) -> None:
    raise CandidateIntegrityError(code, message)


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & _REPARSE_POINT)


def _absolute(value: str | os.PathLike[str]) -> Path:
    try:
        return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))
    except (OSError, TypeError, ValueError) as exc:
        raise CandidateIntegrityError("invalid_path", "candidate path is invalid") from exc


def _candidate_request_directory(path: str | os.PathLike[str]) -> Path:
    requested = _absolute(path)
    try:
        status = os.lstat(requested)
    except FileNotFoundError:
        if requested.name == CANDIDATE_MANIFEST_NAME:
            return requested.parent
        raise
    if stat.S_ISDIR(status.st_mode):
        return requested
    if requested.name == CANDIDATE_MANIFEST_NAME:
        return requested.parent
    _fail(
        "invalid_path",
        f"candidate must be a directory or {CANDIDATE_MANIFEST_NAME}",
    )


def candidate_directory(path: str | os.PathLike[str]) -> Path:
    """Return the canonical plain candidate directory or fail closed."""

    try:
        requested = _candidate_request_directory(path)
        return revalidate_plain_directory(capture_plain_directory(requested))
    except CandidateIntegrityError:
        raise
    except OSError as exc:
        raise CandidateIntegrityError(
            "invalid_path", "candidate directory is unavailable or unsafe"
        ) from exc


def _portable_component_key(value: str, *, label: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or value[-1] in {" ", "."}
        or any(character in value for character in '<>:"/\\|?*')
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or is_windows_reserved_filename(value)
    ):
        _fail("unsafe_path", f"{label} contains an unsafe path component")
    return unicodedata.normalize("NFC", value).casefold()


def _relative_parts(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        _fail("unsafe_path", f"{label} must be a non-empty POSIX relative path")
    if len(value.encode("utf-8")) > _MAX_RELATIVE_PATH_BYTES:
        _fail("resource_limit", f"{label} exceeds the portable path limit")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        _fail("unsafe_path", f"{label} must be a portable relative file path")
    portable = PurePosixPath(value)
    parts = tuple(value.split("/"))
    if portable.is_absolute() or not parts or len(parts) > _MAX_PATH_COMPONENTS:
        _fail("unsafe_path", f"{label} must stay inside the candidate")
    for part in parts:
        if len(part.encode("utf-8")) > _MAX_COMPONENT_BYTES:
            _fail("resource_limit", f"{label} contains an oversized component")
        _portable_component_key(part, label=label)
    return parts


def _path_key(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        unicodedata.normalize("NFC", component).casefold() for component in parts
    )


def _strict_json(payload: bytes, *, label: str) -> dict[str, Any]:
    # Existing candidate JSON has always been UTF-8 without a BOM.  The shared
    # authoring parser accepts a BOM for request compatibility, so retain the
    # narrower candidate boundary explicitly here.
    if payload.startswith(b"\xef\xbb\xbf"):
        _fail("invalid_json", f"{label} must be UTF-8 without a BOM")
    try:
        value = strict_json_loads(
            payload,
            limits=_JSON_LIMITS,
            require_js_safe_integers=False,
        )
    except ValueError as exc:
        raise CandidateIntegrityError(
            "invalid_json",
            f"{label} is not strict bounded JSON",
        ) from exc
    if not isinstance(value, dict):
        _fail("invalid_json", f"{label} must be a JSON object")
    return value


def _capture_json(
    path: Path,
    *,
    relative_path: str,
    parent_identity: PlainDirectoryIdentity,
) -> tuple[_Artifact, dict[str, Any]]:
    try:
        identity, payload = read_plain_file_bytes(path, maximum_bytes=_MAX_JSON_BYTES)
    except OSError as exc:
        raise CandidateIntegrityError(
            "unsafe_artifact", f"cannot safely read {relative_path}"
        ) from exc
    if identity.parent_identity != parent_identity:
        _fail("generation_changed", f"{relative_path} escaped its captured directory")
    artifact = _Artifact(
        relative_path=relative_path,
        identity=identity,
        sha256=hashlib.sha256(payload).hexdigest(),
        payload=payload,
    )
    return artifact, _strict_json(payload, label=relative_path)


def _lower_hex(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail("invalid_binding", f"{label} must be lowercase SHA-256 hexadecimal")
    return value


def _exact_keys(
    value: object,
    required: set[str],
    optional: set[str],
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - (
        required | optional
    ):
        _fail("invalid_document", f"{label} has an invalid shape")
    return value


def _file_binding(value: object, *, label: str, file_hash: str = "sha256") -> str:
    if not isinstance(value, dict):
        _fail("invalid_binding", f"{label} binding is missing")
    _lower_hex(value.get(file_hash), label=f"{label}.{file_hash}")
    path = value.get("path")
    parts = _relative_parts(path, label=f"{label}.path")
    return "/".join(parts)


def _validate_manifest(
    document: dict[str, Any],
    *,
    expected_work_id: str,
    expected_candidate_id: str,
) -> tuple[int, dict[str, Any] | None, dict[str, Any] | None]:
    version = document.get("version")
    if version == SCORE_V2_CANDIDATE_VERSION:
        try:
            validate_score_v2_candidate_manifest(
                document,
                expected_work_id=expected_work_id,
                expected_candidate_id=expected_candidate_id,
            )
        except ScoreV2CandidateError as exc:
            raise CandidateIntegrityError(
                "invalid_document", str(exc)
            ) from exc
        try:
            validate_canonical_utc_timestamp(document.get("created_at_utc"))
        except ValueError as exc:
            raise CandidateIntegrityError(
                "invalid_document", "candidate timestamp is invalid"
            ) from exc
        return SCORE_V2_CANDIDATE_VERSION, None, None

    required = {
        "format",
        "version",
        "candidate_id",
        "work_id",
        "title",
        "created_at_utc",
        "parent_candidate_id",
        "project",
        "render_receipt",
    }
    optional = {"authoring_project", "authoring_workflow", "cache_telemetry"}
    _exact_keys(document, required, optional, label="candidate manifest")
    version = document.get("version")
    if (
        document.get("format") != CANDIDATE_FORMAT
        or type(version) is not int
        or version not in _SUPPORTED_CANDIDATE_VERSIONS
    ):
        _fail("unsupported_candidate", "candidate manifest format/version is unsupported")
    if not isinstance(document.get("title"), str):
        _fail("invalid_document", "candidate title must be a string")
    parent_id = document.get("parent_candidate_id")
    if parent_id is not None and not isinstance(parent_id, str):
        _fail("invalid_document", "candidate parent_candidate_id is invalid")
    if document.get("work_id") != expected_work_id:
        _fail("identity_mismatch", "candidate work_id does not match its directory")
    if document.get("candidate_id") != expected_candidate_id:
        _fail("identity_mismatch", "candidate_id does not match its directory")
    try:
        if version == CANDIDATE_VERSION:
            validate_canonical_utc_timestamp(document.get("created_at_utc"))
        else:
            _parse_bounded_candidate_datetime(document.get("created_at_utc"))
    except ValueError as exc:
        raise CandidateIntegrityError(
            "invalid_document", "candidate timestamp is invalid"
        ) from exc

    project = _exact_keys(
        document.get("project"),
        {"score", "roster", "render_profile", "performance_plan_sha256"},
        {"realization"},
        label="candidate project",
    )
    for key in ("score", "roster", "render_profile"):
        binding = _exact_keys(
            project.get(key),
            {"path", "canonical_sha256", "file_sha256"},
            set(),
            label=f"candidate project.{key}",
        )
        _relative_parts(binding.get("path"), label=f"project.{key}.path")
        _lower_hex(binding.get("canonical_sha256"), label=f"project.{key}.canonical")
        _lower_hex(binding.get("file_sha256"), label=f"project.{key}.file")
    if "realization" in project:
        realization_binding = _exact_keys(
            project.get("realization"),
            {"path", "canonical_sha256", "file_sha256"},
            set(),
            label="candidate project.realization",
        )
        _relative_parts(
            realization_binding.get("path"),
            label="project.realization.path",
        )
        _lower_hex(
            realization_binding.get("canonical_sha256"),
            label="project.realization.canonical",
        )
        _lower_hex(
            realization_binding.get("file_sha256"),
            label="project.realization.file",
        )
    _lower_hex(project.get("performance_plan_sha256"), label="performance plan")
    receipt_binding = _exact_keys(
        document.get("render_receipt"),
        {"path", "sha256"},
        set(),
        label="candidate render_receipt",
    )
    if receipt_binding.get("path") != _RENDER_RECEIPT_NAME:
        _fail("invalid_binding", "candidate must bind the fixed render receipt path")
    _lower_hex(receipt_binding.get("sha256"), label="render receipt")

    try:
        authoring = _candidate_authoring_manifest_binding(
            document.get("authoring_project")
        )
    except ValueError as exc:
        raise CandidateIntegrityError(
            "invalid_document", "candidate authoring binding is invalid"
        ) from exc
    if "authoring_project" in document and authoring is None:
        _fail("invalid_document", "candidate authoring binding cannot be null")
    if "authoring_workflow" in document:
        if version != CANDIDATE_VERSION or authoring is None:
            _fail("invalid_document", "candidate workflow requires current authoring binding")
        try:
            workflow = validate_workflow_authorization(
                document.get("authoring_workflow"), allow_none=False
            )
        except (TypeError, ValueError) as exc:
            raise CandidateIntegrityError(
                "invalid_document", "candidate workflow binding is invalid"
            ) from exc
        assert workflow is not None
        if (
            workflow["project_id"] != authoring["project_id"]
            or workflow["authoring_revision"] != authoring["revision"]
            or workflow["candidate_work_id"] != document["work_id"]
            or workflow["candidate_id"] != document["candidate_id"]
            or workflow["parent_candidate_id"] != document["parent_candidate_id"]
        ):
            _fail("identity_mismatch", "candidate workflow identity disagrees")
    else:
        workflow = None

    telemetry = document.get("cache_telemetry")
    if telemetry is not None:
        telemetry = _exact_keys(
            telemetry,
            {"path", "sha256"},
            set(),
            label="candidate cache_telemetry",
        )
        if telemetry.get("path") != _CACHE_TELEMETRY_NAME:
            _fail("invalid_binding", "candidate cache telemetry path is invalid")
        _lower_hex(telemetry.get("sha256"), label="cache telemetry")
    elif "cache_telemetry" in document:
        _fail("invalid_binding", "candidate cache telemetry cannot be null")
    return int(version), authoring, workflow


def _add_expected(
    expected: dict[tuple[str, ...], str],
    portable_paths: dict[tuple[str, ...], str],
    path: object,
    *,
    role: str,
) -> str:
    parts = _relative_parts(path, label=role)
    key = _path_key(parts)
    previous_role = portable_paths.get(key)
    if previous_role is not None:
        _fail(
            "path_collision",
            f"{role} collides with {previous_role} at {'/'.join(parts)}",
        )
    expected[parts] = role
    portable_paths[key] = role
    return "/".join(parts)


def _derive_expected_files(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
) -> tuple[dict[tuple[str, ...], str], int, dict[str, bool]]:
    expected: dict[tuple[str, ...], str] = {
        (CANDIDATE_MANIFEST_NAME,): "candidate manifest"
    }
    portable_paths = {
        _path_key((CANDIDATE_MANIFEST_NAME,)): "candidate manifest"
    }
    project = manifest["project"]
    for key in ("score", "roster", "render_profile"):
        _add_expected(
            expected,
            portable_paths,
            project[key].get("path"),
            role=f"project.{key}",
        )
    realization = project.get("realization")
    has_realization = realization is not None
    if has_realization:
        _add_expected(
            expected,
            portable_paths,
            realization.get("path"),
            role="project.realization",
        )
    _add_expected(
        expected,
        portable_paths,
        manifest["render_receipt"].get("path"),
        role="render receipt",
    )
    authoring = manifest.get("authoring_project")
    has_authoring = authoring is not None
    if has_authoring:
        _add_expected(
            expected,
            portable_paths,
            authoring["authoring_roster"].get("path"),
            role="authoring roster",
        )
    telemetry = manifest.get("cache_telemetry")
    has_telemetry = telemetry is not None
    if has_telemetry:
        _add_expected(
            expected,
            portable_paths,
            telemetry.get("path"),
            role="cache telemetry",
        )

    _add_expected(
        expected,
        portable_paths,
        _file_binding(receipt.get("performance_plan"), label="performance_plan", file_hash="file_sha256"),
        role="performance plan",
    )
    for key in ("mix", "license_sidecar", "attribution_notice"):
        _add_expected(
            expected,
            portable_paths,
            _file_binding(receipt.get(key), label=key),
            role=key,
        )
    stems = receipt.get("stems")
    if not isinstance(stems, list) or len(stems) > _MAX_CANDIDATE_FILES:
        _fail("invalid_receipt", "render receipt stems are invalid or excessive")
    stem_count = 0
    for index, stem in enumerate(stems):
        wav = stem.get("wav") if isinstance(stem, dict) else None
        if not isinstance(wav, dict):
            _fail("invalid_receipt", f"stems[{index}] lacks a WAV binding")
        if wav.get("written") is True:
            _add_expected(
                expected,
                portable_paths,
                _file_binding(wav, label=f"stems[{index}].wav"),
                role=f"stems[{index}].wav",
            )
            stem_count += 1
        elif not (
            wav.get("written") is False
            and wav.get("path") is None
            and wav.get("sha256") is None
        ):
            _fail("invalid_receipt", f"stems[{index}] unwritten state is incomplete")

    collaboration = receipt.get("collaboration")
    if not isinstance(collaboration, dict):
        _fail("invalid_receipt", "render receipt collaboration binding is missing")
    report_enabled = collaboration.get("report_enabled")
    if report_enabled is True:
        _add_expected(
            expected,
            portable_paths,
            _file_binding(receipt.get("mix_report"), label="mix_report"),
            role="mix report",
        )
        has_mix_report = True
    elif report_enabled is False:
        if "mix_report" in receipt:
            _fail("invalid_receipt", "manual render must not bind a mix report")
        has_mix_report = False
    else:
        _fail("invalid_receipt", "collaboration.report_enabled must be boolean")

    receipt_version = receipt.get("version")
    if receipt_version == 3:
        binding = receipt.get("post_render_check")
        path = _file_binding(binding, label="post_render_check")
        if path != POST_RENDER_CHECK_NAME:
            _fail("invalid_receipt", "v3 receipt must bind the fixed post-check path")
        _add_expected(
            expected,
            portable_paths,
            path,
            role="post-render check",
        )
        has_postcheck = True
    elif "post_render_check" in receipt:
        _fail("invalid_receipt", "legacy v2 receipt must not bind a post-render check")
    else:
        has_postcheck = False

    if len(expected) > _MAX_CANDIDATE_FILES:
        _fail("resource_limit", "candidate binds too many files")
    return expected, stem_count, {
        "realization": has_realization,
        "authoring_roster": has_authoring,
        "cache_telemetry": has_telemetry,
        "mix_report": has_mix_report,
        "post_render_check": has_postcheck,
    }


def _validate_receipt_header(receipt: dict[str, Any]) -> int:
    version = receipt.get("version")
    if (
        receipt.get("format") != "tianlai.render_receipt"
        or type(version) is not int
        or version not in _SUPPORTED_RECEIPT_VERSIONS
    ):
        _fail("unsupported_receipt", "render receipt format/version is unsupported")
    return int(version)


def _expected_children(
    expected: dict[tuple[str, ...], str],
    parent: tuple[str, ...],
) -> dict[tuple[str, ...], tuple[str, bool]]:
    result: dict[tuple[str, ...], tuple[str, bool]] = {}
    length = len(parent)
    for parts in expected:
        if parts[:length] != parent or len(parts) <= length:
            continue
        component = parts[length]
        child_key = (_portable_component_key(component, label="candidate artifact"),)
        is_directory = len(parts) > length + 1
        previous = result.get(child_key)
        if previous is not None and previous != (component, is_directory):
            _fail("path_collision", "candidate bindings collide by portable filename")
        if previous is not None and previous[1] != is_directory:
            _fail("path_collision", "candidate path is both a file and directory")
        result[child_key] = (component, is_directory or (previous[1] if previous else False))
    return result


def _scan_tree(
    root_identity: PlainDirectoryIdentity,
    expected: dict[tuple[str, ...], str],
) -> tuple[
    dict[tuple[str, ...], PlainDirectoryIdentity],
    dict[tuple[str, ...], Path],
]:
    directories: dict[tuple[str, ...], PlainDirectoryIdentity] = {
        (): root_identity
    }
    files: dict[tuple[str, ...], Path] = {}
    pending = [()]
    while pending:
        relative = pending.pop()
        identity = directories[relative]
        directory = revalidate_plain_directory(identity)
        wanted = _expected_children(expected, relative)
        actual: dict[tuple[str, ...], tuple[str, os.stat_result]] = {}
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    key = (_portable_component_key(entry.name, label="candidate entry"),)
                    if key in actual:
                        _fail("path_collision", "candidate contains portable-name collisions")
                    if len(actual) >= len(wanted):
                        _fail(
                            "closed_world_violation",
                            "candidate contains an unbound directory entry",
                        )
                    status = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(status.st_mode) or _is_reparse(status):
                        _fail("unsafe_artifact", f"candidate entry is a link: {entry.name}")
                    actual[key] = (entry.name, status)
        except OSError as exc:
            raise CandidateIntegrityError(
                "generation_changed", "candidate directory changed while enumerating"
            ) from exc
        if set(actual) != set(wanted):
            missing = sorted(set(wanted) - set(actual))
            extra = sorted(set(actual) - set(wanted))
            _fail(
                "closed_world_violation",
                f"candidate entry set differs (missing={len(missing)}, extra={len(extra)})",
            )
        for key, (expected_name, is_directory) in wanted.items():
            actual_name, status = actual[key]
            if actual_name != expected_name:
                _fail(
                    "path_mismatch",
                    "candidate artifact spelling differs from its binding",
                )
            child_relative = (*relative, expected_name)
            child_path = directory / actual_name
            if is_directory:
                if not stat.S_ISDIR(status.st_mode):
                    _fail("unsafe_artifact", "candidate directory binding is not a directory")
                child_identity = capture_plain_directory(child_path)
                if child_identity.path.parent != directory:
                    _fail("generation_changed", "candidate child directory escaped its parent")
                directories[child_relative] = child_identity
                pending.append(child_relative)
            else:
                if not stat.S_ISREG(status.st_mode):
                    _fail("unsafe_artifact", "candidate file binding is not a regular file")
                files[child_relative] = child_path
        revalidate_plain_directory(identity)
    return directories, files


def _capture_artifacts(
    expected: dict[tuple[str, ...], str],
    directories: dict[tuple[str, ...], PlainDirectoryIdentity],
    paths: dict[tuple[str, ...], Path],
    initial: dict[tuple[str, ...], _Artifact],
    *,
    maximum_file_bytes: dict[tuple[str, ...], int] | None = None,
) -> tuple[dict[tuple[str, ...], _Artifact], dict[tuple[str, ...], dict[str, Any]]]:
    artifacts = dict(initial)
    documents: dict[tuple[str, ...], dict[str, Any]] = {}
    total_json_bytes = sum(
        artifact.size for artifact in initial.values() if artifact.payload is not None
    )
    semantic_json_roles = {
        "candidate manifest",
        "render receipt",
        "project.score",
        "project.roster",
        "project.render_profile",
        "project.realization",
        "performance plan",
        "authoring roster",
        "cache telemetry",
        "mix report",
        "post-render check",
    }
    for parts in sorted(expected, key=lambda value: "/".join(value).encode("utf-8")):
        role = expected[parts]
        parent_identity = directories[parts[:-1]]
        if parts in artifacts:
            artifact = artifacts[parts]
            if artifact.identity.parent_identity != parent_identity:
                _fail("generation_changed", f"{role} changed parent identity")
            if artifact.payload is not None:
                documents[parts] = _strict_json(artifact.payload, label=role)
            continue
        path = paths[parts]
        relative = "/".join(parts)
        if role in semantic_json_roles or parts[-1].casefold().endswith(".json"):
            artifact, document = _capture_json(
                path,
                relative_path=relative,
                parent_identity=parent_identity,
            )
            total_json_bytes += artifact.size
            if total_json_bytes > _MAX_JSON_TOTAL_BYTES:
                _fail("resource_limit", "candidate JSON payloads exceed the total limit")
            documents[parts] = document
        elif role == "score-v2 mix":
            try:
                identity, digest, prefix = sha256_plain_file_prefix(
                    path,
                    prefix_bytes=44,
                    maximum_bytes=(
                        None
                        if maximum_file_bytes is None
                        else maximum_file_bytes.get(parts)
                    ),
                )
            except OSError as exc:
                raise CandidateIntegrityError(
                    "unsafe_artifact", f"cannot safely hash {relative}"
                ) from exc
            if identity.parent_identity != parent_identity:
                _fail("generation_changed", f"{relative} changed parent identity")
            artifact = _Artifact(relative, identity, digest, None, prefix)
        else:
            try:
                identity, digest = sha256_plain_file(path)
            except OSError as exc:
                raise CandidateIntegrityError(
                    "unsafe_artifact", f"cannot safely hash {relative}"
                ) from exc
            if identity.parent_identity != parent_identity:
                _fail("generation_changed", f"{relative} changed parent identity")
            artifact = _Artifact(relative, identity, digest, None)
        artifacts[parts] = artifact
    return artifacts, documents


def _artifact_for_path(
    artifacts: dict[tuple[str, ...], _Artifact],
    value: object,
    *,
    label: str,
) -> _Artifact:
    parts = _relative_parts(value, label=label)
    try:
        return artifacts[parts]
    except KeyError as exc:
        raise CandidateIntegrityError(
            "missing_artifact", f"{label} is not present in the captured generation"
        ) from exc


def _document_for_path(
    documents: dict[tuple[str, ...], dict[str, Any]],
    value: object,
    *,
    label: str,
) -> dict[str, Any]:
    parts = _relative_parts(value, label=label)
    try:
        return documents[parts]
    except KeyError as exc:
        raise CandidateIntegrityError(
            "invalid_json", f"{label} is not a captured JSON object"
        ) from exc


def _require_digest(actual: _Artifact, expected: object, *, label: str) -> None:
    expected_hash = _lower_hex(expected, label=label)
    if actual.sha256 != expected_hash:
        _fail("hash_mismatch", f"{label} SHA-256 does not match")


def _validate_cache_accounting(value: object, *, label: str, nested: bool) -> None:
    if not isinstance(value, dict):
        _fail("invalid_telemetry", f"{label} must be an object")
    sections = (value.get("stem"), value.get("relation")) if nested else (value,)
    for section in sections:
        if not isinstance(section, dict):
            _fail("invalid_telemetry", f"{label} accounting is incomplete")
        values = tuple(
            section.get(field)
            for field in ("total", "accounted", "unaccounted", "hits", "misses", "bypassed")
        )
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in values
        ):
            _fail("invalid_telemetry", f"{label} accounting values are invalid")
        total, accounted, unaccounted, hits, misses, bypassed = values
        if accounted != hits + misses + bypassed or total != accounted + unaccounted:
            _fail("invalid_telemetry", f"{label} accounting does not close")


def _validate_semantics(
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    artifacts: dict[tuple[str, ...], _Artifact],
    documents: dict[tuple[str, ...], dict[str, Any]],
    *,
    authoring: dict[str, Any] | None,
    workflow: dict[str, Any] | None,
) -> None:
    manifest_artifact = artifacts[(CANDIDATE_MANIFEST_NAME,)]
    receipt_artifact = artifacts[(_RENDER_RECEIPT_NAME,)]
    _require_digest(
        receipt_artifact,
        manifest["render_receipt"].get("sha256"),
        label="render receipt",
    )

    project_documents: dict[str, dict[str, Any]] = {}
    for key in ("score", "roster", "render_profile"):
        binding = manifest["project"][key]
        artifact = _artifact_for_path(
            artifacts, binding.get("path"), label=f"project.{key}"
        )
        _require_digest(artifact, binding.get("file_sha256"), label=f"project.{key}")
        document = _document_for_path(
            documents, binding.get("path"), label=f"project.{key}"
        )
        if canonical_json_sha256(document) != binding.get("canonical_sha256"):
            _fail("hash_mismatch", f"project.{key} canonical SHA-256 does not match")
        project_documents[key] = document

    realization_binding = manifest["project"].get("realization")
    parsed_realization = None
    if realization_binding is not None:
        realization_artifact = _artifact_for_path(
            artifacts,
            realization_binding.get("path"),
            label="project.realization",
        )
        _require_digest(
            realization_artifact,
            realization_binding.get("file_sha256"),
            label="project.realization",
        )
        realization_document = _document_for_path(
            documents,
            realization_binding.get("path"),
            label="project.realization",
        )
        realization_sha256 = canonical_json_sha256(realization_document)
        if realization_sha256 != realization_binding.get("canonical_sha256"):
            _fail(
                "hash_mismatch",
                "project.realization canonical SHA-256 does not match",
            )
        try:
            parsed_realization = parse_realization_document(
                realization_document,
                score_document=project_documents["score"],
            )
        except (TypeError, ValueError) as exc:
            raise CandidateIntegrityError(
                "identity_mismatch",
                "project.realization does not bind the captured score",
            ) from exc

    plan_binding = receipt["performance_plan"]
    plan_artifact = _artifact_for_path(
        artifacts, plan_binding.get("path"), label="performance_plan"
    )
    _require_digest(
        plan_artifact, plan_binding.get("file_sha256"), label="performance plan file"
    )
    plan = _document_for_path(
        documents, plan_binding.get("path"), label="performance plan"
    )
    plan_hash = canonical_json_sha256(plan)
    if (
        plan_hash != plan_binding.get("sha256")
        or plan_hash != manifest["project"].get("performance_plan_sha256")
    ):
        _fail("hash_mismatch", "performance plan canonical SHA-256 does not match")

    plan_realization = plan.get("realization")
    if parsed_realization is None:
        if plan_realization is not None:
            _fail(
                "identity_mismatch",
                "performance plan has an unmanifested realization binding",
            )
    elif parsed_realization.is_noop:
        if plan_realization is not None:
            _fail(
                "identity_mismatch",
                "no-op realization unexpectedly changed the performance plan",
            )
    else:
        expected_plan_realization = {
            "kind": parsed_realization.kind,
            "schema_version": parsed_realization.schema_version,
            "score_sha256": parsed_realization.score_sha256,
            "canonical_sha256": realization_binding["canonical_sha256"],
            "defaults_profile": parsed_realization.defaults_profile,
            "mode": parsed_realization.mode,
        }
        if plan_realization != expected_plan_realization:
            _fail(
                "identity_mismatch",
                "performance plan realization binding disagrees with source",
            )

    for key in ("mix", "license_sidecar", "attribution_notice"):
        binding = receipt[key]
        artifact = _artifact_for_path(artifacts, binding.get("path"), label=key)
        _require_digest(artifact, binding.get("sha256"), label=key)
    for index, stem in enumerate(receipt["stems"]):
        wav = stem["wav"]
        if wav.get("written") is True:
            artifact = _artifact_for_path(
                artifacts, wav.get("path"), label=f"stems[{index}].wav"
            )
            _require_digest(artifact, wav.get("sha256"), label=f"stems[{index}].wav")

    receipt_authoring = receipt.get("authoring_project")
    if authoring is None:
        if "authoring_project" in receipt:
            _fail("identity_mismatch", "receipt has an unmanifested authoring binding")
    else:
        roster_binding = authoring["authoring_roster"]
        roster_artifact = _artifact_for_path(
            artifacts, roster_binding.get("path"), label="authoring roster"
        )
        _require_digest(
            roster_artifact,
            roster_binding.get("file_sha256"),
            label="authoring roster",
        )
        authoring_roster = _document_for_path(
            documents, roster_binding.get("path"), label="authoring roster"
        )
        if canonical_json_sha256(authoring_roster) != roster_binding.get(
            "canonical_sha256"
        ):
            _fail("hash_mismatch", "authoring roster canonical SHA-256 does not match")
        revision = _authoring_revision_identity(
            project_id=authoring["project_id"],
            score_sha256=manifest["project"]["score"]["canonical_sha256"],
            authoring_roster_sha256=roster_binding["canonical_sha256"],
            render_profile_sha256=manifest["project"]["render_profile"][
                "canonical_sha256"
            ],
        )
        if revision != authoring["revision"]:
            _fail("identity_mismatch", "authoring revision does not match its documents")
        expected_receipt_authoring = {
            "project_id": authoring["project_id"],
            "revision": authoring["revision"],
            "authoring_roster_canonical_sha256": roster_binding[
                "canonical_sha256"
            ],
        }
        if receipt_authoring != expected_receipt_authoring:
            _fail("identity_mismatch", "manifest and receipt authoring identities disagree")
        try:
            _verify_authoring_formal_roster(
                authoring_roster, project_documents["roster"]
            )
            _verify_formal_roster_plan(project_documents["roster"], plan)
        except (TypeError, ValueError) as exc:
            raise CandidateIntegrityError(
                "identity_mismatch", "authoring roster, formal roster, and plan disagree"
            ) from exc

    receipt_workflow = receipt.get("authoring_workflow")
    if workflow is None:
        if "authoring_workflow" in receipt:
            _fail("identity_mismatch", "receipt has an unmanifested workflow binding")
    elif receipt_workflow != workflow:
        _fail("identity_mismatch", "manifest and receipt workflow identities disagree")

    collaboration = receipt["collaboration"]
    if collaboration.get("report_enabled") is True:
        binding = receipt["mix_report"]
        artifact = _artifact_for_path(
            artifacts, binding.get("path"), label="mix report"
        )
        _require_digest(artifact, binding.get("sha256"), label="mix report")
        report = _document_for_path(
            documents, binding.get("path"), label="mix report"
        )
        if (
            report.get("format") != binding.get("format")
            or type(report.get("version")) is not int
            or type(binding.get("version")) is not int
            or report.get("version") != binding.get("version")
            or report.get("mode") != collaboration.get("effective_mode")
            or binding.get("mode") != report.get("mode")
            or binding.get("scope") != report.get("scope")
            or binding.get("format") != MIX_REPORT_FORMAT
            or binding.get("version") not in _SUPPORTED_MIX_REPORT_VERSIONS
        ):
            _fail("identity_mismatch", "mix report identity disagrees with receipt")

    telemetry_binding = manifest.get("cache_telemetry")
    if telemetry_binding is not None:
        artifact = _artifact_for_path(
            artifacts, telemetry_binding.get("path"), label="cache telemetry"
        )
        _require_digest(
            artifact, telemetry_binding.get("sha256"), label="cache telemetry"
        )
        telemetry = _document_for_path(
            documents, telemetry_binding.get("path"), label="cache telemetry"
        )
        required = {
            "format",
            "version",
            "render_receipt",
            "performance_plan",
            "mix",
            "stem_cache",
            "analysis_cache",
        }
        if set(telemetry) != required or (
            telemetry.get("format") != _CACHE_TELEMETRY_FORMAT
            or type(telemetry.get("version")) is not int
            or telemetry.get("version") != _CACHE_TELEMETRY_VERSION
        ):
            _fail("invalid_telemetry", "cache telemetry shape/version is invalid")
        receipt_ref = telemetry.get("render_receipt")
        plan_ref = telemetry.get("performance_plan")
        mix_ref = telemetry.get("mix")
        if (
            not isinstance(receipt_ref, dict)
            or receipt_ref.get("path") != _RENDER_RECEIPT_NAME
            or receipt_ref.get("sha256") != receipt_artifact.sha256
            or not isinstance(plan_ref, dict)
            or plan_ref.get("canonical_sha256") != plan_hash
            or not isinstance(mix_ref, dict)
            or mix_ref.get("sha256") != receipt["mix"].get("sha256")
        ):
            _fail("invalid_telemetry", "cache telemetry bindings disagree")
        if telemetry.get("stem_cache") is None and telemetry.get("analysis_cache") is None:
            _fail("invalid_telemetry", "cache telemetry has no accounting")
        if telemetry.get("stem_cache") is not None:
            _validate_cache_accounting(
                telemetry["stem_cache"], label="stem_cache", nested=False
            )
        if telemetry.get("analysis_cache") is not None:
            _validate_cache_accounting(
                telemetry["analysis_cache"], label="analysis_cache", nested=True
            )

    if receipt.get("version") == 3:
        binding = receipt["post_render_check"]
        artifact = _artifact_for_path(
            artifacts, binding.get("path"), label="post-render check"
        )
        _require_digest(artifact, binding.get("sha256"), label="post-render check")
        report = _document_for_path(
            documents, binding.get("path"), label="post-render check"
        )
        if (
            type(binding.get("version")) is not int
            or type(report.get("version")) is not int
            or binding.get("format") != POST_RENDER_CHECK_FORMAT
            or binding.get("version") != POST_RENDER_CHECK_VERSION
            or report.get("format") != POST_RENDER_CHECK_FORMAT
            or report.get("version") != POST_RENDER_CHECK_VERSION
        ):
            _fail("invalid_postcheck", "post-render check identity is invalid")
        try:
            require_post_render_check_pass(report)
        except (TypeError, ValueError) as exc:
            raise CandidateIntegrityError(
                "invalid_postcheck", "post-render check does not pass"
            ) from exc
        audio_format = receipt.get("audio_format")
        mix_binding = receipt["mix"]
        sample_rate = (
            audio_format.get("sample_rate")
            if isinstance(audio_format, dict)
            else None
        )
        frame_count = mix_binding.get("frame_count")
        if (
            not isinstance(audio_format, dict)
            or audio_format.get("container") != "WAV"
            or audio_format.get("encoding") != "PCM"
            or audio_format.get("bits_per_sample") != 24
            or audio_format.get("channels") != 2
            or isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or sample_rate <= 0
            or isinstance(frame_count, bool)
            or not isinstance(frame_count, int)
            or frame_count <= 0
        ):
            _fail("invalid_postcheck", "receipt audio contract is invalid")
        report_artifact = report.get("artifact")
        mix_artifact = _artifact_for_path(
            artifacts, mix_binding.get("path"), label="mix"
        )
        expected_audio = {
            "container": "WAV",
            "encoding": "PCM",
            "bits_per_sample": 24,
            "channels": 2,
            "sample_rate": sample_rate,
            "frame_count": frame_count,
        }
        if (
            not isinstance(report_artifact, dict)
            or report_artifact.get("path") != mix_binding.get("path")
            or report_artifact.get("sha256") != mix_binding.get("sha256")
            or report_artifact.get("size_bytes") != mix_artifact.size
            or not isinstance(report.get("performance_plan"), dict)
            or report["performance_plan"].get("sha256") != plan_hash
            or not isinstance(report.get("audio_format"), dict)
            or any(report["audio_format"].get(key) != value for key, value in expected_audio.items())
            or not isinstance(report.get("summary"), dict)
            or report["summary"].get("can_proceed") is not True
            or report["summary"].get("expected_activity")
            is not _plan_document_has_explicit_expected_activity(plan)
        ):
            _fail("invalid_postcheck", "post-render check bindings disagree")

    # Keep these locals intentional: all semantic inputs above originate from
    # captured descriptor bytes, including the manifest itself.
    if not manifest_artifact.sha256 or not receipt_artifact.sha256:
        _fail("generation_changed", "candidate generation digest is incomplete")


def _rescan_and_revalidate(
    expected: dict[tuple[str, ...], str],
    directories: dict[tuple[str, ...], PlainDirectoryIdentity],
    artifacts: dict[tuple[str, ...], _Artifact],
) -> None:
    for relative in sorted(directories, key=len, reverse=True):
        identity = directories[relative]
        directory = revalidate_plain_directory(identity)
        wanted = _expected_children(expected, relative)
        observed: dict[tuple[str, ...], bool] = {}
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    key = (_portable_component_key(entry.name, label="candidate entry"),)
                    if key in observed:
                        _fail("path_collision", "candidate acquired a filename collision")
                    if len(observed) >= len(wanted):
                        _fail(
                            "generation_changed",
                            "candidate acquired an unbound directory entry",
                        )
                    # ``DirEntry.stat`` reports zero device/inode/link-count
                    # for ordinary files on supported Windows Python builds.
                    # Bind the enumerated spelling with lstat instead; the
                    # descriptor pass below then closes its remaining race.
                    status = os.lstat(directory / entry.name)
                    if stat.S_ISLNK(status.st_mode) or _is_reparse(status):
                        _fail("generation_changed", "candidate entry became a link")
                    expected_name, expected_directory = wanted.get(
                        key, (entry.name, False)
                    )
                    if entry.name != expected_name:
                        _fail(
                            "generation_changed",
                            "candidate artifact spelling changed after capture",
                        )
                    child = (*relative, expected_name)
                    if expected_directory:
                        captured_directory = directories.get(child)
                        if (
                            not stat.S_ISDIR(status.st_mode)
                            or captured_directory is None
                            or int(status.st_dev) != captured_directory.device
                            or int(status.st_ino) != captured_directory.inode
                        ):
                            _fail(
                                "generation_changed",
                                "candidate directory identity changed after capture",
                            )
                    else:
                        captured_artifact = artifacts.get(child)
                        captured_file = (
                            captured_artifact.identity
                            if captured_artifact is not None
                            else None
                        )
                        if (
                            not stat.S_ISREG(status.st_mode)
                            or int(status.st_nlink) != 1
                            or int(status.st_ino) == 0
                            or captured_file is None
                            or int(status.st_dev) != captured_file.device
                            or int(status.st_ino) != captured_file.inode
                            or int(status.st_size) != captured_file.size
                            or int(status.st_mtime_ns) != captured_file.modified_ns
                            or (
                                os.name != "nt"
                                and int(status.st_ctime_ns)
                                != captured_file.changed_ns
                            )
                        ):
                            _fail(
                                "generation_changed",
                                "candidate file identity changed after capture",
                            )
                    observed[key] = stat.S_ISDIR(status.st_mode)
        except OSError as exc:
            raise CandidateIntegrityError(
                "generation_changed", "candidate changed during final enumeration"
            ) from exc
        expected_types = {key: value[1] for key, value in wanted.items()}
        if observed != expected_types:
            _fail("generation_changed", "candidate entry set changed after capture")
        revalidate_plain_directory(identity)
    # The exact directory walk above catches a name that was swapped after an
    # earlier descriptor check.  Re-open every captured file only afterwards,
    # so a mutation during that walk is also rejected before the root identity
    # is accepted by the caller.
    for parts in sorted(artifacts, key=len, reverse=True):
        try:
            revalidate_plain_file(artifacts[parts].identity)
        except OSError as exc:
            raise CandidateIntegrityError(
                "generation_changed", f"{'/'.join(parts)} changed after capture"
            ) from exc


def _generation_sha256(artifacts: dict[tuple[str, ...], _Artifact]) -> str:
    digest = hashlib.sha256(b"tianlai.candidate.closed-generation.v1\0")
    for parts in sorted(artifacts, key=lambda value: "/".join(value).encode("utf-8")):
        artifact = artifacts[parts]
        path_bytes = "/".join(parts).encode("utf-8")
        digest.update(len(path_bytes).to_bytes(4, "big"))
        digest.update(path_bytes)
        digest.update(artifact.size.to_bytes(8, "big"))
        digest.update(bytes.fromhex(artifact.sha256))
    return digest.hexdigest()


def _verify_score_v2_candidate_integrity(
    *,
    root_identity: PlainDirectoryIdentity,
    work_identity: PlainDirectoryIdentity,
    manifest_artifact: _Artifact,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Verify the fixed, flat Candidate-v3 generation from descriptors."""

    root = revalidate_plain_directory(root_identity)
    if manifest_artifact.payload != canonical_json_bytes(manifest):
        _fail(
            "noncanonical_artifact",
            "Candidate-v3 manifest must use canonical JSON bytes",
        )
    receipt_artifact, receipt = _capture_json(
        root / SCORE_V2_RENDER_RECEIPT_NAME,
        relative_path=SCORE_V2_RENDER_RECEIPT_NAME,
        parent_identity=root_identity,
    )
    try:
        validate_score_v2_render_receipt(receipt)
        named_expected = score_v2_candidate_expected_files(
            manifest,
            receipt,
            candidate_manifest_name=CANDIDATE_MANIFEST_NAME,
        )
    except ScoreV2CandidateError as exc:
        raise CandidateIntegrityError(
            "invalid_score_v2_candidate", str(exc)
        ) from exc
    expected = {(name,): role for name, role in named_expected.items()}
    directories, paths = _scan_tree(root_identity, expected)
    initial = {
        (CANDIDATE_MANIFEST_NAME,): manifest_artifact,
        (SCORE_V2_RENDER_RECEIPT_NAME,): receipt_artifact,
    }
    captured, _documents = _capture_artifacts(
        expected,
        directories,
        paths,
        initial,
        maximum_file_bytes={
            (receipt["mix"]["path"],): receipt["mix"]["size_bytes"],
        },
    )
    portable_artifacts = {
        parts[0]: ScoreV2CandidateArtifact(
            sha256=artifact.sha256,
            size_bytes=artifact.size,
            payload=artifact.payload,
            prefix=artifact.prefix,
        )
        for parts, artifact in captured.items()
        if len(parts) == 1
    }
    try:
        validate_score_v2_candidate_generation(
            manifest,
            receipt,
            portable_artifacts,
        )
    except ScoreV2CandidateError as exc:
        raise CandidateIntegrityError(
            "invalid_score_v2_candidate", str(exc)
        ) from exc
    _rescan_and_revalidate(expected, directories, captured)
    revalidate_plain_directory(root_identity)
    revalidate_plain_directory(work_identity)
    total_bytes = sum(artifact.size for artifact in captured.values())
    return {
        "kind": CANDIDATE_VERIFY_RESULT_KIND,
        "schema_version": CANDIDATE_VERIFY_RESULT_VERSION,
        "ok": True,
        "integrity_verified": True,
        "candidate": {
            "format": CANDIDATE_FORMAT,
            "version": SCORE_V2_CANDIDATE_VERSION,
            "pipeline": "score_v2",
            "work_id": manifest["work_id"],
            "candidate_id": manifest["candidate_id"],
            "manifest_sha256": manifest_artifact.sha256,
            "render_receipt_version": receipt["schema_version"],
            "render_receipt_sha256": receipt_artifact.sha256,
            "generation_sha256": _generation_sha256(captured),
        },
        "integrity": {
            "bound_entry_set_closed_when_enumerated": True,
            "root_file_count": len(captured),
            "stem_file_count": 0,
            "total_file_count": len(captured),
            "total_byte_count": total_bytes,
            "optional_artifacts": {
                "score_v2": True,
                "realization": False,
                "authoring_roster": False,
                "cache_telemetry": False,
                "mix_report": False,
                "postcheck": True,
            },
            "scope": "descriptor_bound_closed_generation_read",
            "semantic_scope": (
                "score_v2_single_executor_builtin_oscillator_asset_free"
            ),
            "runtime_authority_document_reusable": False,
            "audio_execution_recomputed": False,
            "live_tree_immutable_after_return": False,
            "uncooperative_concurrent_writer_excluded": False,
            "authorship_verified": False,
            "provenance_verified": False,
        },
    }


def verify_candidate_integrity(
    path: str | os.PathLike[str],
    *,
    expected_work_id: str | None = None,
    expected_candidate_id: str | None = None,
) -> dict[str, Any]:
    """Verify one descriptor-bound candidate generation and return a report."""

    try:
        requested_directory = _candidate_request_directory(path)
    except CandidateIntegrityError:
        raise
    except OSError as exc:
        raise CandidateIntegrityError(
            "invalid_path", "candidate path is unavailable"
        ) from exc
    try:
        root_identity = capture_plain_directory(requested_directory)
        root = revalidate_plain_directory(root_identity)
        work_identity = capture_plain_directory(root.parent)
        work = revalidate_plain_directory(work_identity)
    except OSError as exc:
        raise CandidateIntegrityError(
            "unsafe_directory", "candidate directory is not a stable plain directory"
        ) from exc
    if root.parent != work:
        _fail("unsafe_directory", "candidate escaped its captured work directory")

    manifest_artifact, manifest = _capture_json(
        root / CANDIDATE_MANIFEST_NAME,
        relative_path=CANDIDATE_MANIFEST_NAME,
        parent_identity=root_identity,
    )
    candidate_version, authoring, workflow = _validate_manifest(
        manifest,
        expected_work_id=work.name if expected_work_id is None else expected_work_id,
        expected_candidate_id=root.name if expected_candidate_id is None else expected_candidate_id,
    )
    if candidate_version == SCORE_V2_CANDIDATE_VERSION:
        return _verify_score_v2_candidate_integrity(
            root_identity=root_identity,
            work_identity=work_identity,
            manifest_artifact=manifest_artifact,
            manifest=manifest,
        )
    receipt_artifact, receipt = _capture_json(
        root / _RENDER_RECEIPT_NAME,
        relative_path=_RENDER_RECEIPT_NAME,
        parent_identity=root_identity,
    )
    receipt_version = _validate_receipt_header(receipt)
    expected, stem_count, optional = _derive_expected_files(manifest, receipt)
    directories, paths = _scan_tree(root_identity, expected)
    initial = {
        (CANDIDATE_MANIFEST_NAME,): manifest_artifact,
        (_RENDER_RECEIPT_NAME,): receipt_artifact,
    }
    artifacts, documents = _capture_artifacts(
        expected, directories, paths, initial
    )
    documents[(CANDIDATE_MANIFEST_NAME,)] = manifest
    documents[(_RENDER_RECEIPT_NAME,)] = receipt
    _validate_semantics(
        manifest,
        receipt,
        artifacts,
        documents,
        authoring=authoring,
        workflow=workflow,
    )
    _rescan_and_revalidate(expected, directories, artifacts)
    revalidate_plain_directory(root_identity)
    revalidate_plain_directory(work_identity)

    total_bytes = sum(artifact.size for artifact in artifacts.values())
    root_files = sum(1 for parts in artifacts if len(parts) == 1)
    return {
        "kind": CANDIDATE_VERIFY_RESULT_KIND,
        "schema_version": CANDIDATE_VERIFY_RESULT_VERSION,
        "ok": True,
        "integrity_verified": True,
        "candidate": {
            "format": CANDIDATE_FORMAT,
            "version": candidate_version,
            "work_id": manifest["work_id"],
            "candidate_id": manifest["candidate_id"],
            "manifest_sha256": manifest_artifact.sha256,
            "render_receipt_version": receipt_version,
            "render_receipt_sha256": receipt_artifact.sha256,
            "generation_sha256": _generation_sha256(artifacts),
        },
        "integrity": {
            "bound_entry_set_closed_when_enumerated": True,
            "root_file_count": root_files,
            "stem_file_count": stem_count,
            "total_file_count": len(artifacts),
            "total_byte_count": total_bytes,
            "optional_artifacts": optional,
            "scope": "descriptor_bound_closed_generation_read",
            "live_tree_immutable_after_return": False,
            "uncooperative_concurrent_writer_excluded": False,
            "authorship_verified": False,
            "provenance_verified": False,
        },
    }


__all__ = [
    "CANDIDATE_VERIFY_RESULT_KIND",
    "CANDIDATE_VERIFY_RESULT_VERSION",
    "CandidateIntegrityError",
    "candidate_directory",
    "verify_candidate_integrity",
]
