"""Immutable render candidates and receipt-backed inspection."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any
import unicodedata
import uuid
import warnings

from .canonical_json import canonical_json_sha256
from .ensemble import CACHE_TELEMETRY_NAME, verify_render_generation
from .render_lock import acquire_render_lock
from .score_ops import compare_scores


CANDIDATE_FORMAT = "tianlai.candidate"
CANDIDATE_VERSION = 1
CANDIDATE_MANIFEST_NAME = "候选.json"
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def portable_slug(text: object, *, maximum_length: int = 72) -> str:
    original = unicodedata.normalize("NFC", str(text)).strip()
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", original)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")
    if not cleaned:
        cleaned = "untitled"
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    suffix = f"-{digest}"
    stem_length = max(1, maximum_length - len(suffix))
    return f"{cleaned[:stem_length].rstrip(' ._') or 'untitled'}{suffix}"


def new_candidate_id(plan_sha256: str | None = None) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    binding = (plan_sha256 or "unbound")[:8]
    return f"candidate-{timestamp}-{binding}-{uuid.uuid4().hex[:8]}"


def _write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class CandidateTarget:
    work_id: str
    candidate_id: str
    directory: Path
    replacing: bool
    expected_receipt_sha256: str | None = None
    expected_manifest_sha256: str | None = None


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


def _previous_directories(final: Path) -> tuple[Path, ...]:
    prefix = f".{final.name}."
    suffix = ".previous"
    if not final.parent.is_dir():
        return ()
    return tuple(
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
) -> None:
    previous = _previous_directories(final)
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

    os.replace(backup, final)
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
) -> CandidateTarget:
    """Resolve and identity-bind one candidate publication target."""

    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be boolean")
    expected_receipt = _normalized_expected_receipt_sha256(
        expected_receipt_sha256
    )
    work_id = portable_slug(title)
    candidate_id = (
        new_candidate_id(plan_sha256)
        if output_id is None
        else portable_slug(output_id, maximum_length=96)
    )
    root = Path(output_root).expanduser().resolve()
    directory = root / work_id / candidate_id
    with acquire_render_lock(directory):
        _recover_previous_if_safe(
            directory,
            work_id=work_id,
            candidate_id=candidate_id,
            overwrite=overwrite,
            expected_receipt_sha256=expected_receipt,
        )
        if not _path_exists(directory):
            return CandidateTarget(
                work_id,
                candidate_id,
                directory,
                False,
                None,
                None,
            )
        if not overwrite:
            raise FileExistsError(
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
        return CandidateTarget(
            work_id,
            candidate_id,
            directory,
            True,
            expected_receipt,
            manifest_sha256,
        )


def _verify_replacement_identity(target: CandidateTarget) -> None:
    expected_receipt = target.expected_receipt_sha256
    expected_manifest = target.expected_manifest_sha256
    if not expected_receipt or not expected_manifest:
        raise ValueError(
            "覆盖现有候选缺少准备阶段记录的完整身份"
        )
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
) -> None:
    try:
        resolved_parent = parent.resolve()
        resolved = path.resolve()
        if (
            resolved.parent != resolved_parent
            or not path.name.startswith(prefix)
            or path.is_symlink()
        ):
            raise RuntimeError(
                f"拒绝清理身份异常的{label}: {path}"
            )
        if resolved.exists():
            shutil.rmtree(resolved)
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
) -> Path | None:
    """Move one fully verified staging generation into its final location."""

    final = target.directory
    if staging.parent != final.parent:
        raise ValueError("候选暂存目录必须与最终目录位于同一父目录")
    if not target.replacing:
        if _path_exists(final):
            raise FileExistsError(
                f"候选目录在渲染期间被创建，拒绝覆盖: {final}"
            )
        os.replace(staging, final)
        try:
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

    _verify_replacement_identity(target)
    backup = _expected_backup_path(target)
    if _path_exists(backup):
        raise RuntimeError(
            f"确定身份的旧候选备份已存在，拒绝覆盖: {backup}"
        )
    os.replace(final, backup)
    try:
        backup_target = CandidateTarget(
            target.work_id,
            target.candidate_id,
            backup,
            True,
            target.expected_receipt_sha256,
            target.expected_manifest_sha256,
        )
        _verify_replacement_identity(backup_target)
        os.replace(staging, final)
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
    return backup


@contextmanager
def candidate_publication(target: CandidateTarget):
    """Render into a sibling staging directory and publish only when complete.

    A valid ``候选.json`` and all of its source bindings must be present before
    the final directory can appear.  For an explicitly authorised replacement,
    the original receipt is rechecked immediately before the directory swap.
    """

    parent = target.directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    with acquire_render_lock(target.directory):
        residuals = _previous_directories(target.directory)
        if residuals:
            raise RuntimeError(
                "候选发布前发现 .previous 事务残留，已失败关闭: "
                + ", ".join(str(path) for path in residuals)
            )
        if target.replacing:
            _verify_replacement_identity(target)
        elif _path_exists(target.directory):
            raise FileExistsError(
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
        staged_target = CandidateTarget(
            target.work_id,
            target.candidate_id,
            staging,
            False,
            None,
            None,
        )
        committed = False
        backup: Path | None = None
        try:
            yield staged_target
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
                    label="未发布候选暂存目录",
                )
        if backup is not None:
            _safe_cleanup_private_directory(
                backup,
                parent=parent,
                prefix=f".{target.directory.name}.",
                label="已提交候选的旧版本备份",
            )


def publish_candidate_metadata(
    target: CandidateTarget,
    *,
    title: str,
    score: dict[str, Any],
    roster: dict[str, Any],
    render_profile: dict[str, Any],
    receipt_path: str | Path,
    plan_sha256: str,
    parent_candidate_id: str | None = None,
) -> dict[str, Any]:
    """Write source documents and install the candidate manifest last."""

    directory = target.directory.resolve()
    receipt = Path(receipt_path).resolve()
    if receipt.parent != directory or not receipt.is_file():
        raise ValueError("render receipt must be inside the candidate directory")
    score_path = directory / "score.json"
    roster_path = directory / "roster.json"
    profile_path = directory / "render-profile.json"
    _write_json_atomic(score_path, score)
    _write_json_atomic(roster_path, roster)
    _write_json_atomic(profile_path, render_profile)
    manifest = {
        "format": CANDIDATE_FORMAT,
        "version": CANDIDATE_VERSION,
        "candidate_id": target.candidate_id,
        "work_id": target.work_id,
        "title": title,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
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
            "sha256": sha256_file(receipt),
        },
    }
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
    if not resolved.is_file():
        raise ValueError(f"candidate {label} file is missing")
    return resolved


def load_candidate(
    path: str | Path,
    *,
    verify: bool = True,
    expected_work_id: str | None = None,
    expected_candidate_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    directory = _candidate_directory(path)
    manifest_path = directory / CANDIDATE_MANIFEST_NAME
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("format") != CANDIDATE_FORMAT
        or document.get("version") != CANDIDATE_VERSION
    ):
        raise ValueError("unsupported candidate manifest")
    _verify_candidate_identity(
        document,
        expected_work_id=(
            directory.parent.name
            if expected_work_id is None
            else expected_work_id
        ),
        expected_candidate_id=(
            directory.name
            if expected_candidate_id is None
            else expected_candidate_id
        ),
    )
    if verify:
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
            if sha256_file(source) != binding.get("file_sha256"):
                raise ValueError(f"candidate {key} file hash mismatch")
            value = json.loads(source.read_text(encoding="utf-8"))
            if canonical_json_sha256(value) != binding.get(
                "canonical_sha256"
            ):
                raise ValueError(f"candidate {key} canonical hash mismatch")
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
        if sha256_file(receipt) != receipt_binding.get("sha256"):
            raise ValueError("candidate render receipt hash mismatch")
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
        receipt_document = json.loads(receipt.read_text(encoding="utf-8"))
        performance_plan = receipt_document.get("performance_plan")
        if (
            not isinstance(performance_plan, dict)
            or document["project"].get("performance_plan_sha256")
            != performance_plan.get("sha256")
        ):
            raise ValueError(
                "candidate manifest and render receipt disagree on plan Hash"
            )
    return directory, document


def _verified_plan(
    directory: Path,
    candidate: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt_binding = candidate["render_receipt"]
    receipt_path = _bound_artifact_path(
        directory,
        receipt_binding["path"],
        label="render receipt",
    )
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    plan_binding = receipt.get("performance_plan")
    if not isinstance(plan_binding, dict):
        raise ValueError("render receipt has no performance_plan binding")
    plan_path = _bound_artifact_path(
        directory,
        plan_binding.get("path", ""),
        label="performance plan",
    )
    if sha256_file(plan_path) != plan_binding.get("file_sha256"):
        raise ValueError("candidate performance plan file hash mismatch")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if canonical_json_sha256(plan) != plan_binding.get("sha256"):
        raise ValueError("candidate performance plan canonical hash mismatch")
    if (
        candidate.get("project", {}).get("performance_plan_sha256")
        != plan_binding.get("sha256")
    ):
        raise ValueError("candidate manifest and receipt disagree on plan Hash")
    return plan, receipt


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
    before_score = json.loads(
        _bound_artifact_path(
            before_directory,
            before["project"]["score"]["path"],
            label="score",
        ).read_text(encoding="utf-8")
    )
    after_score = json.loads(
        _bound_artifact_path(
            after_directory,
            after["project"]["score"]["path"],
            label="score",
        ).read_text(encoding="utf-8")
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
            "before": json.loads(
                _bound_artifact_path(
                    before_directory,
                    before["render_receipt"]["path"],
                    label="render receipt",
                ).read_text(encoding="utf-8")
            )["mix"]["sha256"],
            "after": json.loads(
                _bound_artifact_path(
                    after_directory,
                    after["render_receipt"]["path"],
                    label="render receipt",
                ).read_text(encoding="utf-8")
            )["mix"]["sha256"],
        },
    }


__all__ = [
    "CANDIDATE_FORMAT",
    "CANDIDATE_MANIFEST_NAME",
    "CANDIDATE_VERSION",
    "CandidateTarget",
    "canonical_json_sha256",
    "candidate_publication",
    "compare_candidates",
    "load_candidate",
    "locate_candidate",
    "new_candidate_id",
    "portable_slug",
    "prepare_candidate_target",
    "publish_candidate_metadata",
    "sha256_file",
]
