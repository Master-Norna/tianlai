"""Ensemble rendering: one stem per executor, then a deterministic mix.

Independent raw stems may be rendered by a small, automatically selected set
of managed subprocesses.  The coordinator still consumes every result in
performance-plan order, and automatically keeps the established in-process
serial path for short, heavy or otherwise ineligible work.  This preserves
three important properties:

* the sample libraries total well over 8 GB, so both worker memory and the
  anonymous scratch window are conservatively bounded;
* every stem stays byte-reproducible on its own, which lets the optional raw
  stem cache reuse an unchanged instrument performance without disturbing
  any other part;
* workers are admitted only for built-in manifest dispatch whose process
  independence and resource bounds can be proved; local factories keep the
  established serial path, while gain, analysis, writing and mixing remain
  in the coordinator.

The cache boundary is deliberately before assignment gain/automation, pan,
shared hall, collaboration analysis, master gain and normalization.  Those
mixing decisions are therefore always recomputed, even on a cache hit.

No hidden cross-part processing is applied: there is no automatic
side-chaining, bus compression or EQ.  The optional collaboration report
measures rendered dry stems and compares only relationships explicitly
declared in the roster; even ``suggest`` emits a bounded recommendation
without changing samples.

The mix refuses to clip.  ``write_wav_pcm24`` clamps silently at ±1.0, so a
hot mix would be destroyed without any visible sign; instead the peak is
measured before writing and an overload is reported with the exact amount of
gain to remove.  A complete generation is built off to the side and published
with the render receipt last, so a failed rerender cannot leave new audio
advertised by an old receipt.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import errno
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import stat
import tempfile
from types import SimpleNamespace
from typing import Any, Callable
import uuid
import warnings

from .adaptive_parallelism import (
    AdaptiveWorkload,
    make_adaptive_backend_key,
)
from .adaptive_runtime import AdaptiveRenderSession
from .audio import (
    WavFileEvidence,
    revalidate_wav_file_evidence,
    write_wav_pcm24,
    write_wav_pcm24_blocks,
    write_wav_pcm24_blocks_with_evidence,
    write_wav_pcm24_with_evidence,
)
from .canonical_json import canonical_json_bytes as _project_canonical_json_bytes
from .collaboration_report import (
    CollaborationReportBuilder,
    MIX_REPORT_NAME,
    TAIL_ANALYSIS_SECONDS,
    attach_stage_diagnostics,
)
from .conductor import PerformancePlan
from .events import parse_performance_document
from .instrument import create_instrument, factory_manifest_sha256
from .license_sidecar import (
    AudioArtifact,
    ENSEMBLE_ATTRIBUTION_NAME,
    ENSEMBLE_LICENSE_SIDECAR_NAME,
    InstrumentUse,
    write_license_sidecars,
)
from .onset_evidence import (
    canonical_json_bytes as _fingerprint_canonical_json_bytes,
    compute_runtime_fingerprint,
)
from .orchestration_topology import (
    analyze_orchestration_topology,
    attach_orchestration_topology,
)
from .post_render_check import (
    POST_RENDER_CHECK_NAME,
    REPORT_FORMAT as POST_RENDER_CHECK_FORMAT,
    REPORT_VERSION as POST_RENDER_CHECK_VERSION,
    analyze_rendered_wav,
    require_post_render_check_pass,
    write_post_render_check,
)
from .portable_filename import portable_stem_filename
from . import renderer as _renderer_module
from .renderer import (
    _prefer_dense_synth_frame_path,
    _prefer_frame_stream_path,
    render_document,
    render_document_blocks,
)
from .render_lock import (
    PlainDirectoryIdentity,
    acquire_render_lock,
    capture_plain_directory,
    revalidate_plain_directory,
)
from .render_parallelism import (
    automatic_worker_capacity,
    derive_parallelism_work_frames,
    derive_worker_resource_estimate,
    select_render_parallelism,
)
from .resource_limits import (
    ProjectLimits,
    _analysis_transaction_scratch_requirement,
    validate_render_request_resource_limits,
)
from .roster import CollaborationSettings
from .stem_cache import (
    PROCESS_SOURCE_TREE_SHA256,
    StemCache,
    build_cache_key,
    current_source_tree_matches,
)
from .stem_source import OwnedStemSource, StemBlockSource
from .stem_worker import (
    StemRenderJob,
    StemWorkerError,
    _ManagedWarmBinding,
    _retire_managed_stem_worker_session,
    _try_start_stem_worker,
    collect_stem_worker,
    managed_subprocess_workers_available,
    retire_idle_stem_workers,
    terminate_stem_worker,
)
from .stereo_stage_metrics import analyze_stereo_stage
from .workflow_binding import validate_workflow_authorization
from .worker_slots import (
    SessionScratchClaim,
    WorkerResourceClaim,
    WorkerSlotError,
    WorkerSlotPool,
    scratch_volume_identity,
)


RENDER_RECEIPT_VERSION = 3
_LEGACY_RENDER_RECEIPT_VERSIONS = frozenset({2})
_SUPPORTED_RENDER_RECEIPT_VERSIONS = (
    _LEGACY_RENDER_RECEIPT_VERSIONS | {RENDER_RECEIPT_VERSION}
)
RENDER_RECEIPT_NAME = "渲染回执.json"
PERFORMANCE_PLAN_NAME = "演奏计划.json"
CACHE_TELEMETRY_NAME = "缓存遥测.json"
CACHE_TELEMETRY_FORMAT = "tianlai.render_cache_telemetry"
CACHE_TELEMETRY_VERSION = 1
RAW_STEM_CACHE_STAGE = "raw_instrument_render_pre_assignment_gain_v1"
_DIRECT_STEM_CACHE_LOAD_BYTES = 32 * 1024 * 1024
_DIRECT_SERIAL_STEM_LOAD_BYTES = 32 * 1024 * 1024
_DIRECT_ANALYSIS_STEM_LOAD_BYTES = 32 * 1024 * 1024
_STREAMED_STEM_FREE_RESERVE_BYTES = 512 * 1024 * 1024
_STREAMED_STEM_OUTPUT_MARGIN_BYTES = 1024 * 1024
_MANAGED_WORKER_CHUNK_BYTES = 65_536 * 2 * 4
# A float64 stereo bus costs sixteen bytes per frame.  Keep this private and
# deliberately conservative: short renders stay on anonymous RAM, while a
# dry score large enough to matter can trade sequential local I/O for a much
# smaller coordinator working set without adding a user-facing setting.
_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES = 256 * 1024 * 1024
# A hall render retains a float64 stereo mix bus and a float32 stereo send bus,
# or twenty-four bytes per output frame in total.  Admit both files under one
# exact session claim once their combined size is large enough to materially
# reduce coordinator private memory.  This remains a private zero-configuration
# policy so projects never acquire another render knob.
_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES = 128 * 1024 * 1024
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _session_scratch_pool_factory() -> WorkerSlotPool:
    return WorkerSlotPool()


_LOWER_HEX = frozenset("0123456789abcdef")
_ORIGINAL_WRITE_WAV_PCM24 = write_wav_pcm24
_ORIGINAL_WRITE_WAV_PCM24_BLOCKS = write_wav_pcm24_blocks

_RENDER_ARTIFACT_NAMES = (
    PERFORMANCE_PLAN_NAME,
    "分轨",
    "合奏.wav",
    ENSEMBLE_LICENSE_SIDECAR_NAME,
    ENSEMBLE_ATTRIBUTION_NAME,
    MIX_REPORT_NAME,
    POST_RENDER_CHECK_NAME,
    CACHE_TELEMETRY_NAME,
    RENDER_RECEIPT_NAME,
)


def _authoring_project_receipt_binding(
    value: object,
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "project_id",
        "revision",
        "authoring_roster_canonical_sha256",
    }:
        raise ValueError("authoring project binding has an invalid shape")
    project_id = value.get("project_id")
    revision = value.get("revision")
    roster_hash = value.get("authoring_roster_canonical_sha256")
    if (
        not isinstance(project_id, str)
        or len(project_id) != 32
        or any(character not in _LOWER_HEX for character in project_id)
        or not isinstance(revision, str)
        or len(revision) != 64
        or any(character not in _LOWER_HEX for character in revision)
        or not isinstance(roster_hash, str)
        or len(roster_hash) != 64
        or any(character not in _LOWER_HEX for character in roster_hash)
    ):
        raise ValueError("authoring project binding contains an invalid identity")
    return {
        "project_id": project_id,
        "revision": revision,
        "authoring_roster_canonical_sha256": roster_hash,
    }


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} 必须是有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数值")
    return number


def _performance_has_explicit_expected_activity(performance: Any) -> bool:
    """Return true only for an explicitly positive note-on in one document."""

    if not isinstance(performance, dict):
        return False
    events = performance.get("events")
    if not isinstance(events, list):
        return False
    for event in events:
        if not isinstance(event, dict) or event.get("type") != "note_on":
            continue
        velocity = event.get("velocity")
        if (
            isinstance(velocity, Real)
            and not isinstance(velocity, bool)
            and math.isfinite(float(velocity))
            and float(velocity) > 0.0
        ):
            return True
    return False


def _plan_has_explicit_expected_activity(plan: PerformancePlan) -> bool:
    """Conservatively identify a plan that explicitly requests audible notes.

    This value is only allowed to strengthen the exact-digital-silence
    contract in the post-render checker.  A malformed or unfamiliar
    performance shape therefore returns ``False`` instead of guessing.  The
    conductor currently writes an explicit positive velocity on every
    compiled note-on, while an empty/rest-only part has no such event.
    """

    parts = getattr(plan, "parts", None)
    if not isinstance(parts, (tuple, list)):
        return False
    for part in parts:
        if _performance_has_explicit_expected_activity(
            getattr(part, "performance", None)
        ):
            return True
    return False


def _plan_document_has_explicit_expected_activity(document: Any) -> bool:
    """Re-derive the activity contract from a receipt-bound plan document."""

    if not isinstance(document, dict):
        return False
    parts = document.get("parts")
    if not isinstance(parts, list):
        return False
    for part in parts:
        if isinstance(part, dict) and _performance_has_explicit_expected_activity(
            part.get("performance")
        ):
            return True
    return False


def _resolved_collaboration_settings(
    plan: PerformancePlan,
    mode_override: str | None,
) -> CollaborationSettings:
    settings = getattr(plan, "collaboration", None)
    if not isinstance(settings, CollaborationSettings):
        settings = CollaborationSettings()
    if mode_override is None:
        return settings
    if mode_override not in ("manual", "analyze", "suggest"):
        raise ValueError(
            "collaboration_mode 必须是 manual、analyze 或 suggest"
        )
    return replace(settings, mode=mode_override, declared=True)


def _reject_nonfinite_tree(value: Any, label: str) -> None:
    """Reject non-finite numbers before any render artifact is written."""

    if isinstance(value, dict):
        for key, item in value.items():
            _reject_nonfinite_tree(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite_tree(item, f"{label}[{index}]")
        return
    if isinstance(value, Real) and not isinstance(value, bool):
        _finite_float(value, label)


def _canonical_json_bytes(document: Any) -> bytes:
    """Canonical UTF-8 JSON used to bind a receipt to one performance plan."""

    _reject_nonfinite_tree(document, "performance_plan")
    return _project_canonical_json_bytes(document)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_wav_pcm24_with_optional_evidence(
    path: Path,
    frames: Any,
    sample_rate: int,
    *,
    expected_frame_count: int,
) -> tuple[int, WavFileEvidence | None]:
    """Use writer evidence unless a legacy writer seam was monkeypatched."""

    if write_wav_pcm24 is not _ORIGINAL_WRITE_WAV_PCM24:
        return int(write_wav_pcm24(path, frames, sample_rate)), None
    result = write_wav_pcm24_with_evidence(
        path,
        frames,
        sample_rate,
        expected_frame_count=expected_frame_count,
    )
    return result.frame_count, result.evidence


def _write_wav_pcm24_blocks_with_optional_evidence(
    path: Path,
    blocks: Any,
    sample_rate: int,
    *,
    expected_frame_count: int,
) -> tuple[int, WavFileEvidence | None]:
    """Block-stream counterpart retaining the established patch seam."""

    if write_wav_pcm24_blocks is not _ORIGINAL_WRITE_WAV_PCM24_BLOCKS:
        return int(write_wav_pcm24_blocks(path, blocks, sample_rate)), None
    result = write_wav_pcm24_blocks_with_evidence(
        path,
        blocks,
        sample_rate,
        expected_frame_count=expected_frame_count,
    )
    return result.frame_count, result.evidence


def _sha256_written_wav(
    path: Path,
    evidence: WavFileEvidence | None,
) -> str:
    """Reuse bound writer evidence, with the established scan as fallback."""

    if evidence is None:
        return _sha256_file(path)
    return revalidate_wav_file_evidence(path, evidence)


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Durably finish a temporary JSON file, then atomically replace ``path``."""

    _reject_nonfinite_tree(document, "render_receipt")
    payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    # Successful replacement consumes the random temporary name.  If it
    # fails, retain whatever currently occupies that name; an unconditional
    # unlink could delete a post-failure replacement installed by a racer.
    os.replace(temporary, path)


def _warn_preserved_render_directory(message: str) -> None:
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except BaseException:
        # Cleanup is best-effort after commit or while another exception is
        # already propagating; warning filters must not change that outcome.
        pass


def _remove_private_render_directory(
    path: Path,
    parent: Path,
    prefix: str,
    *,
    parent_identity: PlainDirectoryIdentity | None = None,
    directory_identity: PlainDirectoryIdentity | None = None,
) -> Path | None:
    """Retire one renderer-owned staging/backup entry without deletion.

    The active entry is first renamed to an unpredictable same-parent name.
    Empty and non-empty directories, files, links, and entries replaced after
    an identity check all remain recoverable under
    ``.cleanup-preserved-*`` (using a compact sibling name when Windows path
    length rules cannot accommodate the descriptive name).  Even a
    non-recursive ``rmdir`` is deliberately avoided: a writer could replace
    the preserved path after its final identity check and otherwise have that
    replacement removed by name.
    """

    if parent_identity is None:
        parent_identity = capture_plain_directory(parent)
    resolved_parent = revalidate_plain_directory(parent_identity)
    if (
        path.parent != resolved_parent
        or path != resolved_parent / path.name
        or not path.name.startswith(prefix)
    ):
        raise RuntimeError(f"拒绝清理非渲染器私有目录: {path}")
    if directory_identity is None:
        directory_identity = capture_plain_directory(path)
    identity_changed = False
    try:
        revalidate_plain_directory(directory_identity)
    except BaseException:
        identity_changed = True
    revalidate_plain_directory(parent_identity)
    if not os.path.lexists(path):
        return None

    preserved: Path | None = None
    compact_recovery_name = False
    for _ in range(16):
        recovery_token = uuid.uuid4().hex
        candidate = resolved_parent / (
            f".cleanup-preserved-{recovery_token}"
            if compact_recovery_name
            else f"{path.name}.cleanup-preserved-{recovery_token}"
        )
        if os.path.lexists(candidate):
            continue
        try:
            os.rename(path, candidate)
        except FileNotFoundError as exc:
            # The renderer may be nested in a larger atomic publication which
            # has already moved the private parent out of this namespace.  If
            # the exact source name is now absent there is nothing left here
            # to preserve; treating that committed disappearance as cleanup
            # failure produces a false warning on Windows.
            if not os.path.lexists(path):
                return None
            if (
                not compact_recovery_name
                and getattr(exc, "winerror", None) == 3
            ):
                # Windows also reports an overlong destination path as
                # ERROR_PATH_NOT_FOUND even though the source still exists.
                compact_recovery_name = True
                continue
            raise
        except FileExistsError:
            continue
        except OSError as exc:
            # Some Windows filesystem layers surface a vanished source as a
            # plain OSError (WinError 3) instead of FileNotFoundError.  The
            # identity-bound private name being absent is still the same
            # committed postcondition: there is no active entry left for
            # this cleanup pass to move.
            if not os.path.lexists(path):
                return None
            if not compact_recovery_name and (
                getattr(exc, "winerror", None) in {3, 206}
                or exc.errno == errno.ENAMETOOLONG
            ):
                compact_recovery_name = True
                continue
            raise
        preserved = candidate
        break
    if preserved is None:
        raise RuntimeError("无法为渲染私有目录预留安全的保全名称")
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
        _warn_preserved_render_directory(
            "渲染私有目录在清理期间发生身份替换，"
            f"已安全保全于 {preserved}"
        )
    return preserved


def _artifact_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _bound_artifact_path(
    directory: Path,
    relative_path: Any,
    label: str,
) -> Path:
    """Resolve one receipt-owned relative file without allowing path escape."""

    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
    ):
        raise RuntimeError(f"{label} 的回执路径必须是非空 POSIX 相对路径")
    portable = PurePosixPath(relative_path)
    if portable.is_absolute() or any(
        part in ("", ".", "..") for part in portable.parts
    ):
        raise RuntimeError(f"{label} 的回执路径越出渲染目录: {relative_path!r}")
    candidate = directory.joinpath(*portable.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label} 对应文件不存在: {relative_path!r}") from exc
    root = directory.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"{label} 的回执路径越出渲染目录: {relative_path!r}") from exc
    if not resolved.is_file():
        raise RuntimeError(f"{label} 对应的不是普通文件: {relative_path!r}")
    return resolved


def _verify_hash_binding(
    directory: Path,
    binding: Any,
    label: str,
) -> Path:
    if not isinstance(binding, dict):
        raise RuntimeError(f"{label} 缺少文件绑定")
    path = _bound_artifact_path(directory, binding.get("path"), label)
    expected = binding.get("sha256")
    actual = _sha256_file(path)
    if not isinstance(expected, str) or expected != actual:
        raise RuntimeError(
            f"{label} 的 SHA-256 与渲染回执不一致: "
            f"expected={expected!r}, actual={actual}"
        )
    return path


def _verify_closed_cache_accounting(
    summary: Any,
    *,
    label: str,
    nested: bool,
) -> None:
    if not isinstance(summary, dict):
        raise RuntimeError(f"{label} 必须是对象")
    sections = (
        (summary.get("stem"), summary.get("relation"))
        if nested
        else (summary,)
    )
    for index, section in enumerate(sections):
        section_label = (
            f"{label}[{index}]" if nested else label
        )
        if not isinstance(section, dict):
            raise RuntimeError(f"{section_label} 缺少计数对象")
        values = tuple(
            section.get(field)
            for field in (
                "total",
                "accounted",
                "unaccounted",
                "hits",
                "misses",
                "bypassed",
            )
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for value in values
        ):
            raise RuntimeError(f"{section_label} 的缓存计数无效")
        total, accounted, unaccounted, hits, misses, bypassed = values
        if (
            accounted != hits + misses + bypassed
            or total != accounted + unaccounted
        ):
            raise RuntimeError(f"{section_label} 的缓存账本不闭合")


def _verify_cache_telemetry(
    directory: Path,
    receipt: dict[str, Any],
) -> None:
    path = directory / CACHE_TELEMETRY_NAME
    if not _artifact_exists(path):
        return
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("缓存遥测必须是普通文件")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("缓存遥测不是合法 UTF-8 JSON") from exc
    expected_keys = {
        "format",
        "version",
        "render_receipt",
        "performance_plan",
        "mix",
        "stem_cache",
        "analysis_cache",
    }
    if not isinstance(document, dict) or set(document) != expected_keys:
        raise RuntimeError("缓存遥测顶层结构无效")
    if (
        document["format"] != CACHE_TELEMETRY_FORMAT
        or document["version"] != CACHE_TELEMETRY_VERSION
    ):
        raise RuntimeError("缓存遥测格式或版本不受支持")
    receipt_binding = document["render_receipt"]
    if (
        not isinstance(receipt_binding, dict)
        or receipt_binding.get("path") != RENDER_RECEIPT_NAME
        or receipt_binding.get("sha256")
        != _sha256_file(directory / RENDER_RECEIPT_NAME)
    ):
        raise RuntimeError("缓存遥测没有绑定当前渲染回执")
    plan_binding = receipt.get("performance_plan")
    mix_binding = receipt.get("mix")
    if (
        not isinstance(document["performance_plan"], dict)
        or not isinstance(plan_binding, dict)
        or document["performance_plan"].get("canonical_sha256")
        != plan_binding.get("sha256")
    ):
        raise RuntimeError("缓存遥测没有绑定当前演奏计划")
    if (
        not isinstance(document["mix"], dict)
        or not isinstance(mix_binding, dict)
        or document["mix"].get("sha256") != mix_binding.get("sha256")
    ):
        raise RuntimeError("缓存遥测没有绑定当前合奏音频")
    stem = document["stem_cache"]
    analysis = document["analysis_cache"]
    if stem is None and analysis is None:
        raise RuntimeError("缓存遥测至少要包含一种缓存账本")
    if stem is not None:
        _verify_closed_cache_accounting(
            stem,
            label="stem_cache",
            nested=False,
        )
    if analysis is not None:
        _verify_closed_cache_accounting(
            analysis,
            label="analysis_cache",
            nested=True,
        )


def _verify_post_render_check(
    directory: Path,
    receipt: dict[str, Any],
    *,
    plan_document: dict[str, Any],
) -> dict[str, Any]:
    """Verify the v3 report against the exact receipt-bound mix and plan."""

    binding = receipt.get("post_render_check")
    if not isinstance(binding, dict) or binding.get("path") != POST_RENDER_CHECK_NAME:
        raise RuntimeError("v3 渲染回执缺少固定路径的渲染后自检绑定")
    report_path = _verify_hash_binding(
        directory,
        binding,
        "post_render_check",
    )
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("渲染后自检不是合法 UTF-8 JSON") from exc
    if (
        not isinstance(report, dict)
        or binding.get("format") != POST_RENDER_CHECK_FORMAT
        or isinstance(binding.get("version"), bool)
        or not isinstance(binding.get("version"), int)
        or binding.get("version") != POST_RENDER_CHECK_VERSION
        or report.get("format") != POST_RENDER_CHECK_FORMAT
        or isinstance(report.get("version"), bool)
        or not isinstance(report.get("version"), int)
        or report.get("version") != POST_RENDER_CHECK_VERSION
    ):
        raise RuntimeError("渲染后自检身份或版本与渲染回执不一致")
    try:
        require_post_render_check_pass(report)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("渲染后自检报告结构无效或包含硬阻断项") from exc

    mix_binding = receipt.get("mix")
    plan_binding = receipt.get("performance_plan")
    audio_format = receipt.get("audio_format")
    if (
        not isinstance(mix_binding, dict)
        or not isinstance(plan_binding, dict)
        or not isinstance(audio_format, dict)
    ):
        raise RuntimeError("v3 渲染回执缺少自检所需的音频或计划绑定")
    receipt_sample_rate = audio_format.get("sample_rate")
    receipt_frame_count = mix_binding.get("frame_count")
    if (
        audio_format.get("container") != "WAV"
        or audio_format.get("encoding") != "PCM"
        or audio_format.get("bits_per_sample") != 24
        or audio_format.get("channels") != 2
        or isinstance(receipt_sample_rate, bool)
        or not isinstance(receipt_sample_rate, int)
        or receipt_sample_rate <= 0
        or isinstance(receipt_frame_count, bool)
        or not isinstance(receipt_frame_count, int)
        or receipt_frame_count <= 0
    ):
        raise RuntimeError("v3 渲染回执的音频格式或帧数合同无效")

    artifact = report.get("artifact")
    if (
        not isinstance(artifact, dict)
        or artifact.get("path") != mix_binding.get("path")
        or artifact.get("sha256") != mix_binding.get("sha256")
    ):
        raise RuntimeError("渲染后自检没有绑定当前合奏音频")
    mix_path = _bound_artifact_path(
        directory,
        mix_binding.get("path"),
        "mix",
    )
    if artifact.get("size_bytes") != mix_path.stat().st_size:
        raise RuntimeError("渲染后自检记录的音频字节数与当前合奏不一致")
    report_plan = report.get("performance_plan")
    if (
        not isinstance(report_plan, dict)
        or report_plan.get("sha256") != plan_binding.get("sha256")
    ):
        raise RuntimeError("渲染后自检没有绑定当前演奏计划")

    report_audio_format = report.get("audio_format")
    expected_audio_format = {
        "container": audio_format.get("container"),
        "encoding": audio_format.get("encoding"),
        "bits_per_sample": audio_format.get("bits_per_sample"),
        "channels": audio_format.get("channels"),
        "sample_rate": receipt_sample_rate,
        "frame_count": receipt_frame_count,
    }
    if not isinstance(report_audio_format, dict) or any(
        report_audio_format.get(field) != value
        for field, value in expected_audio_format.items()
    ):
        raise RuntimeError("渲染后自检记录的音频格式或帧数与回执不一致")
    summary = report.get("summary")
    if not isinstance(summary, dict) or summary.get("can_proceed") is not True:
        raise RuntimeError("渲染后自检没有给出可发布的硬合同结论")
    expected_activity = _plan_document_has_explicit_expected_activity(
        plan_document
    )
    if summary.get("expected_activity") is not expected_activity:
        raise RuntimeError("渲染后自检的活动内容结论与演奏计划不一致")
    return report


def _verify_render_generation(directory: Path) -> None:
    """Verify every published file hash before a generation is accepted."""

    receipt_path = directory / RENDER_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("渲染回执不存在、不可读或不是合法 JSON") from exc
    if not isinstance(receipt, dict):
        raise RuntimeError("渲染回执顶层必须是对象")
    receipt_version = receipt.get("version")
    if (
        receipt.get("format") != "tianlai.render_receipt"
        or isinstance(receipt_version, bool)
        or not isinstance(receipt_version, int)
        or receipt_version not in _SUPPORTED_RENDER_RECEIPT_VERSIONS
    ):
        raise RuntimeError("渲染回执格式或版本不受支持")
    try:
        receipt_authoring_binding = _authoring_project_receipt_binding(
            receipt.get("authoring_project")
        )
    except ValueError as exc:
        raise RuntimeError("渲染回执的作者工程身份绑定无效") from exc
    if "authoring_project" in receipt and receipt_authoring_binding is None:
        raise RuntimeError("渲染回执的作者工程身份绑定不得为 null")
    try:
        receipt_workflow_binding = validate_workflow_authorization(
            receipt.get("authoring_workflow")
        )
    except ValueError as exc:
        raise RuntimeError("渲染回执的作者工作流授权绑定无效") from exc
    if "authoring_workflow" in receipt and receipt_workflow_binding is None:
        raise RuntimeError("渲染回执的作者工作流授权绑定不得为 null")
    if receipt_workflow_binding is not None:
        if receipt_authoring_binding is None:
            raise RuntimeError("作者工作流授权缺少作者工程身份绑定")
        if (
            receipt_workflow_binding["project_id"]
            != receipt_authoring_binding["project_id"]
            or receipt_workflow_binding["authoring_revision"]
            != receipt_authoring_binding["revision"]
        ):
            raise RuntimeError("作者工作流授权与作者工程身份不一致")

    plan_binding = receipt.get("performance_plan")
    if not isinstance(plan_binding, dict):
        raise RuntimeError("渲染回执缺少 performance_plan")
    plan_path = _bound_artifact_path(
        directory,
        plan_binding.get("path"),
        "performance_plan",
    )
    plan_file_hash = _sha256_file(plan_path)
    if plan_binding.get("file_sha256") != plan_file_hash:
        raise RuntimeError("演奏计划文件哈希与渲染回执不一致")
    try:
        plan_document = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("演奏计划不是合法 UTF-8 JSON") from exc
    plan_canonical_hash = hashlib.sha256(
        _canonical_json_bytes(plan_document)
    ).hexdigest()
    if plan_binding.get("sha256") != plan_canonical_hash:
        raise RuntimeError("演奏计划规范化哈希与渲染回执不一致")

    _verify_hash_binding(directory, receipt.get("mix"), "mix")
    _verify_hash_binding(
        directory,
        receipt.get("license_sidecar"),
        "license_sidecar",
    )
    _verify_hash_binding(
        directory,
        receipt.get("attribution_notice"),
        "attribution_notice",
    )

    stems = receipt.get("stems")
    if not isinstance(stems, list):
        raise RuntimeError("渲染回执的 stems 必须是数组")
    for index, stem in enumerate(stems):
        if not isinstance(stem, dict) or not isinstance(stem.get("wav"), dict):
            raise RuntimeError(f"stems[{index}] 缺少 WAV 绑定")
        wav = stem["wav"]
        if wav.get("written") is True:
            _verify_hash_binding(directory, wav, f"stems[{index}].wav")
        elif not (
            wav.get("written") is False
            and wav.get("path") is None
            and wav.get("sha256") is None
        ):
            raise RuntimeError(f"stems[{index}].wav 的未写入状态不完整")

    collaboration = receipt.get("collaboration")
    if not isinstance(collaboration, dict):
        raise RuntimeError("渲染回执缺少 collaboration")
    report_enabled = collaboration.get("report_enabled")
    if report_enabled is True:
        report_binding = receipt.get("mix_report")
        report_path = _verify_hash_binding(
            directory,
            report_binding,
            "mix_report",
        )
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("协奏诊断不是合法 UTF-8 JSON") from exc
        if (
            not isinstance(report, dict)
            or report.get("format") != report_binding.get("format")
            or report.get("version") != report_binding.get("version")
            or report.get("mode") != collaboration.get("effective_mode")
        ):
            raise RuntimeError("协奏诊断身份或有效模式与渲染回执不一致")
    elif report_enabled is False:
        if "mix_report" in receipt or (directory / MIX_REPORT_NAME).exists():
            raise RuntimeError("manual 模式不得保留协奏诊断")
    else:
        raise RuntimeError("collaboration.report_enabled 必须是布尔值")
    if receipt_version == RENDER_RECEIPT_VERSION:
        _verify_post_render_check(
            directory,
            receipt,
            plan_document=plan_document,
        )
    _verify_cache_telemetry(directory, receipt)


def verify_render_generation(directory: str | Path) -> None:
    """Verify every receipt-bound artifact in one render generation.

    This public boundary is shared by the renderer and the immutable-candidate
    publisher.  Keeping one verifier prevents the outer publication layer from
    accepting a receipt whose plan, mix, stems, licence sidecar, attribution
    notice, collaboration report, or v3 post-render check changed after the
    renderer returned.
    """

    _verify_render_generation(Path(directory).resolve())


def _publish_render_artifacts(staging: Path, final: Path) -> None:
    """Publish one complete render, with the receipt acting as commit marker.

    Every expensive or failure-prone operation happens in ``staging`` first.
    Publication removes the previous receipt before touching audio, installs
    all new artifacts, and installs the new receipt last.  If a filesystem
    operation fails, already moved entries are rolled back and the previous
    receipt is restored last, so a visible receipt always describes one
    complete generation.
    """

    parent = final.parent
    final.mkdir(parents=True, exist_ok=True)
    parent_identity = capture_plain_directory(parent)
    backup_prefix = f".{final.name}.render-backup."
    backup = Path(tempfile.mkdtemp(dir=parent, prefix=backup_prefix))
    backup_identity = capture_plain_directory(backup)

    old_order = (
        RENDER_RECEIPT_NAME,
        *(
            name
            for name in _RENDER_ARTIFACT_NAMES
            if name != RENDER_RECEIPT_NAME
        ),
    )
    new_order = (
        *(
            name
            for name in _RENDER_ARTIFACT_NAMES
            if name != RENDER_RECEIPT_NAME
        ),
        RENDER_RECEIPT_NAME,
    )
    moved_old: list[str] = []
    installed_new: list[str] = []
    cleanup_backup = True
    try:
        for name in old_order:
            target = final / name
            if _artifact_exists(target):
                os.replace(target, backup / name)
                moved_old.append(name)

        for name in new_order:
            source = staging / name
            if _artifact_exists(source):
                os.replace(source, final / name)
                installed_new.append(name)

        if not (final / RENDER_RECEIPT_NAME).is_file():
            raise RuntimeError("完整渲染没有生成提交标记渲染回执.json")
        _verify_render_generation(final)
    except BaseException:
        rollback_errors: list[str] = []
        for name in reversed(installed_new):
            target = final / name
            if not _artifact_exists(target):
                continue
            try:
                os.replace(target, staging / name)
            except OSError as exc:
                rollback_errors.append(f"撤回新产物 {name}: {exc}")
        # Restore data first and the old receipt last, preserving the same
        # commit-marker rule during rollback.
        for name in (
            *(
                item
                for item in reversed(moved_old)
                if item != RENDER_RECEIPT_NAME
            ),
            *(
                (RENDER_RECEIPT_NAME,)
                if RENDER_RECEIPT_NAME in moved_old
                else ()
            ),
        ):
            source = backup / name
            if not _artifact_exists(source):
                continue
            try:
                os.replace(source, final / name)
            except OSError as exc:
                rollback_errors.append(f"恢复旧产物 {name}: {exc}")
        if rollback_errors:
            cleanup_backup = False
            try:
                preserved_backup = _remove_private_render_directory(
                    backup,
                    parent,
                    backup_prefix,
                    parent_identity=parent_identity,
                    directory_identity=backup_identity,
                )
            except BaseException as cleanup_error:
                preserved_backup = backup
                rollback_errors.append(
                    f"保全旧产物备份: {cleanup_error}"
                )
            raise RuntimeError(
                "渲染发布失败且回滚不完整；旧产物备份保留在 "
                f"{preserved_backup or backup}: " + "; ".join(rollback_errors)
            )
        raise
    finally:
        if cleanup_backup:
            try:
                _remove_private_render_directory(
                    backup,
                    parent,
                    backup_prefix,
                    parent_identity=parent_identity,
                    directory_identity=backup_identity,
                )
            except Exception as exc:
                # At this point either the new generation was committed or
                # the original exception is already propagating.  Backup
                # cleanup must not turn a valid visible generation into a
                # false render failure or mask the real publication error.
                warnings.warn(
                    f"渲染私有备份清理失败，可稍后删除 {backup}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )


@dataclass(frozen=True, slots=True)
class StemResult:
    executor_id: str
    part_id: str
    instrument: str
    path: str | None
    peak: float
    gain_db: float
    pan: float
    peak_voices: int
    gain_envelope: tuple[dict[str, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "executor_id": self.executor_id,
            "part_id": self.part_id,
            "instrument": self.instrument,
            "path": self.path,
            "peak": round(self.peak, 6),
            "gain_db": self.gain_db,
            "pan": self.pan,
            "peak_voices": self.peak_voices,
        }
        if self.gain_envelope:
            data["gain_envelope"] = list(self.gain_envelope)
        return data


@dataclass(frozen=True, slots=True)
class EnsembleResult:
    sample_rate: int
    frame_count: int
    duration_seconds: float
    mix_path: str
    mix_peak: float
    stems: tuple[StemResult, ...]
    normalize_gain_db: float = 0.0
    pre_normalize_peak: float | None = None
    space: dict[str, Any] | None = None
    plan_path: str | None = None
    mix_report_path: str | None = None
    mix_report: dict[str, Any] | None = None
    post_render_check_path: str | None = None
    post_render_check: dict[str, Any] | None = None
    post_render_check_summary: dict[str, Any] | None = None
    collaboration_mode: str = "manual"
    receipt_path: str | None = None
    license_sidecar_path: str | None = None
    attribution_path: str | None = None
    stem_cache: dict[str, Any] | None = None
    analysis_cache: dict[str, Any] | None = None
    cache_telemetry_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "sample_rate": self.sample_rate,
            "frame_count": self.frame_count,
            "duration_seconds": self.duration_seconds,
            "mix_path": self.mix_path,
            "mix_peak": round(self.mix_peak, 6),
            "stems": [stem.to_dict() for stem in self.stems],
        }
        if self.space is not None:
            # 厅堂只作用于合奏总线;分轨仍是全干。把厅堂参数记进结果,
            # 让"这份合奏经过了哪个空间"可查、可复算。
            data["space"] = self.space
        if self.pre_normalize_peak is not None:
            # 归一是可选的成品电平层,开了就把它施加的增益如实记进结果,
            # 让"这份成品比忠实渲染响了多少"随时可查、可复算。
            data["normalize"] = {
                "pre_normalize_peak": round(self.pre_normalize_peak, 6),
                "applied_gain_db": round(self.normalize_gain_db, 4),
            }
        if self.plan_path is not None:
            data["plan_path"] = self.plan_path
        if self.mix_report_path is not None:
            data["mix_report_path"] = self.mix_report_path
        if self.mix_report is not None:
            data["mix_report"] = self.mix_report
        if self.post_render_check_path is not None:
            data["post_render_check_path"] = self.post_render_check_path
        if self.post_render_check is not None:
            data["post_render_check"] = self.post_render_check
        if self.post_render_check_summary is not None:
            data["post_render_check_summary"] = self.post_render_check_summary
        data["collaboration_mode"] = self.collaboration_mode
        if self.receipt_path is not None:
            data["receipt_path"] = self.receipt_path
        if self.license_sidecar_path is not None:
            data["license_sidecar_path"] = self.license_sidecar_path
        if self.attribution_path is not None:
            data["attribution_path"] = self.attribution_path
        if self.stem_cache is not None:
            # Cache hit/miss is runtime telemetry, not audio provenance.  It
            # intentionally stays out of 渲染回执.json so a cold and a warm
            # render of the same inputs retain byte-identical receipts.
            data["stem_cache"] = self.stem_cache
        if self.analysis_cache is not None:
            data["analysis_cache"] = self.analysis_cache
        if self.cache_telemetry_path is not None:
            data["cache_telemetry_path"] = self.cache_telemetry_path
        return data


def balance_gains(pan: float) -> tuple[float, float]:
    """Stereo balance for an already-stereo source.

    A stereo stem is not a point source, so repositioning it with a
    constant-power pan law would both narrow the image and add up to 3 dB of
    gain on one side.  A balance control instead attenuates the far channel
    and leaves the centre at unity, which can never introduce clipping.
    """

    if pan >= 0.0:
        return 1.0 - pan, 1.0
    return 1.0, 1.0 + pan


def apply_gain_envelope(
    buffer: Any,
    sample_rate: int,
    base_gain_db: float,
    points: Any,
) -> None:
    """Apply a compiled dB envelope in place without allocating a full-track curve.

    ``points`` are conductor-plan points with ``time_seconds`` and
    ``offset_db`` attributes.  Working in bounded chunks keeps long orchestral
    renders from needing a second track-sized float64 allocation.
    """

    import numpy as np

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if not points:
        if base_gain_db == 0.0:
            return
        buffer *= 10.0 ** (base_gain_db / 20.0)
        return
    times = np.asarray([point.time_seconds for point in points], dtype=np.float64)
    offsets = np.asarray([point.offset_db for point in points], dtype=np.float64)
    if (
        times.ndim != 1
        or times.size == 0
        or not np.isfinite(times).all()
        or not np.isfinite(offsets).all()
        or times[0] < 0.0
        or np.any(np.diff(times) <= 0.0)
    ):
        raise ValueError("gain envelope points must be finite and strictly ordered")

    chunk_frames = 65_536
    for start in range(0, int(buffer.shape[0]), chunk_frames):
        end = min(int(buffer.shape[0]), start + chunk_frames)
        frame_times = np.arange(start, end, dtype=np.float64) / float(sample_rate)
        db = np.interp(
            frame_times,
            times,
            offsets,
            left=offsets[0],
            right=offsets[-1],
        )
        db += base_gain_db
        np.divide(db, 20.0, out=db)
        np.power(10.0, db, out=db)
        buffer[start:end] *= db[:, np.newaxis]


def _compile_gain_envelope_points(points: Any) -> tuple[Any, Any]:
    """Validate one curve and freeze its numeric interpolation data."""

    import numpy as np

    times = np.asarray(
        [point.time_seconds for point in points],
        dtype=np.float64,
    )
    offsets = np.asarray(
        [point.offset_db for point in points],
        dtype=np.float64,
    )
    if (
        times.ndim != 1
        or times.size == 0
        or not np.isfinite(times).all()
        or not np.isfinite(offsets).all()
        or times[0] < 0.0
        or np.any(np.diff(times) <= 0.0)
    ):
        raise ValueError(
            "gain envelope points must be finite and strictly ordered"
        )
    return times, offsets


def _apply_gain_envelope_block(
    buffer: Any,
    sample_rate: int,
    base_gain_db: float,
    points: Any,
    *,
    frame_offset: int,
    compiled_points: tuple[Any, Any] | None = None,
) -> None:
    """Block form of ``apply_gain_envelope`` with absolute frame timing."""

    import numpy as np

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if frame_offset < 0:
        raise ValueError("frame_offset must be non-negative")
    if not points:
        if base_gain_db != 0.0:
            buffer *= 10.0 ** (base_gain_db / 20.0)
        return
    if compiled_points is None:
        times, offsets = _compile_gain_envelope_points(points)
    else:
        times, offsets = compiled_points
    stop = frame_offset + int(buffer.shape[0])
    frame_times = np.arange(frame_offset, stop, dtype=np.float64) / float(
        sample_rate
    )
    db = np.interp(
        frame_times,
        times,
        offsets,
        left=offsets[0],
        right=offsets[-1],
    )
    db += base_gain_db
    np.divide(db, 20.0, out=db)
    np.power(10.0, db, out=db)
    buffer *= db[:, np.newaxis]


@dataclass(frozen=True, slots=True)
class _RawStemCacheIdentity:
    key: str
    manifest_sha256: str
    frame_count: int


@dataclass(frozen=True, slots=True)
class _PreparedParallelStemCache:
    """One parent-side cache decision prepared for ordered consumption."""

    identity: _RawStemCacheIdentity | None
    lookup: Any | None
    accounting: tuple[tuple[str, str], ...]
    disable_session: bool = False


def _local_instrument_python_sha256(manifest_path: Path) -> str:
    """Bind local helper modules that the generic runtime closure may not see."""

    records: list[dict[str, str]] = []
    for source in sorted(
        manifest_path.parent.rglob("*.py"),
        key=lambda item: item.relative_to(manifest_path.parent).as_posix(),
    ):
        if source.is_symlink() or not source.is_file():
            raise ValueError(
                f"乐器本地 Python 依赖不是普通文件: {source}"
            )
        records.append(
            {
                "path": source.relative_to(
                    manifest_path.parent
                ).as_posix(),
                "sha256": _sha256_file(source),
            }
        )
    return hashlib.sha256(_canonical_json_bytes(records)).hexdigest()


def _assert_cacheable_runtime_asset_graph(
    manifest: dict[str, Any],
    fingerprint: dict[str, Any],
) -> None:
    """Reject an empty graph unless the manifest explicitly declares DSP-only audio."""

    graph = fingerprint.get("runtime_asset_graph")
    if not isinstance(graph, dict):
        raise ValueError("运行资源指纹缺少 runtime_asset_graph")
    file_count = graph.get("file_count")
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or file_count < 0
    ):
        raise ValueError("运行资源指纹的 file_count 无效")
    if file_count:
        return
    if (
        manifest.get("provenance_kind") == "project_authored_dsp"
        and manifest.get("external_audio_assets") == []
    ):
        return
    if manifest.get("runtime_asset_policy") == "no_external_audio_assets":
        return
    raise ValueError(
        "空运行资源图仅允许明确声明为项目自研 DSP 或无外部音频资产的乐器"
    )


def _raw_stem_cache_identity(
    part: Any,
    sample_rate: int,
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ],
) -> _RawStemCacheIdentity:
    """Build one live, fail-closed identity for pre-assignment-gain audio."""

    document = parse_performance_document(part.performance)
    if document.sample_rate != sample_rate:
        raise ValueError(
            f"声部 {part.executor.executor_id!r} 的采样率与总谱不一致"
        )

    manifest_path = Path(
        part.executor.capability.manifest_path
    ).resolve()
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    override_map = part.executor.override_map
    effective_manifest = (
        {**manifest, **override_map}
        if override_map
        else manifest
    )
    effective_manifest_sha256 = factory_manifest_sha256(
        effective_manifest
    )
    local_python_sha256 = _local_instrument_python_sha256(
        manifest_path
    )
    runtime_key = (
        os.path.normcase(str(manifest_path)),
        effective_manifest_sha256,
        sample_rate,
        local_python_sha256,
    )
    runtime_fingerprint_sha256 = runtime_fingerprints.get(runtime_key)
    if runtime_fingerprint_sha256 is None:
        fingerprint = compute_runtime_fingerprint(
            _PROJECT_ROOT,
            manifest_path,
            effective_manifest=effective_manifest,
            sample_rate_hz=sample_rate,
        )
        _assert_cacheable_runtime_asset_graph(
            effective_manifest,
            fingerprint,
        )
        runtime_fingerprint_sha256 = hashlib.sha256(
            _fingerprint_canonical_json_bytes(fingerprint)
        ).hexdigest()
        runtime_fingerprints[runtime_key] = (
            runtime_fingerprint_sha256
        )

    key = build_cache_key(
        {
            "format": "tianlai.raw_stem_cache_identity",
            "version": 1,
            "stage": RAW_STEM_CACHE_STAGE,
            "audio": {
                "sample_rate": sample_rate,
                "channels": 2,
                "dtype": "<f4",
                "frame_count": document.total_samples,
            },
            # The complete ordered performance is the audible part input.
            # Executor IDs, roles and trace text are intentionally absent.
            "performance": part.performance,
            "instrument": {
                "manifest_sha256": manifest_sha256,
                "effective_manifest_sha256": (
                    effective_manifest_sha256
                ),
                "runtime_fingerprint_sha256": (
                    runtime_fingerprint_sha256
                ),
                "local_python_sha256": local_python_sha256,
            },
            # The captured digest describes the Python source from which this
            # process was loaded.  A live disk mismatch disables caching below.
            "producer_source_tree_sha256": (
                PROCESS_SOURCE_TREE_SHA256
            ),
        }
    )
    return _RawStemCacheIdentity(
        key=key,
        manifest_sha256=manifest_sha256,
        frame_count=document.total_samples,
    )


def _new_stem_cache_summary(
    *,
    refresh_requested: bool,
    total: int,
) -> dict[str, Any]:
    return {
        "requested": True,
        "active": True,
        "stage": RAW_STEM_CACHE_STAGE,
        "refresh_requested": refresh_requested,
        "total": total,
        "accounted": 0,
        "unaccounted": total,
        "hits": 0,
        "misses": 0,
        "bypassed": 0,
        "corrupt_fallbacks": 0,
        "writes": 0,
        "write_skips": 0,
        "write_failures": 0,
        "conflicts": 0,
        "reason_counts": {},
    }


def _finalize_stem_cache_summary(summary: dict[str, Any]) -> None:
    accounted = (
        int(summary["hits"])
        + int(summary["misses"])
        + int(summary["bypassed"])
    )
    summary["accounted"] = accounted
    summary["unaccounted"] = int(summary["total"]) - accounted


def _note_cache_result(
    summary: dict[str, Any],
    field: str,
    reason: str,
) -> None:
    summary[field] = int(summary[field]) + 1
    reasons = summary["reason_counts"]
    reasons[reason] = int(reasons.get(reason, 0)) + 1


def _cache_lookup_matches(
    lookup: Any,
    identity: _RawStemCacheIdentity,
    sample_rate: int,
) -> bool:
    if (
        not lookup.hit
        or lookup.record is None
        or (lookup.audio is None and lookup.source is None)
    ):
        return False
    metadata = lookup.record.metadata
    shape_matches = (
        tuple(lookup.audio.shape) == (identity.frame_count, 2)
        if lookup.audio is not None
        else lookup.source.shape == (identity.frame_count, 2)
    )
    return (
        metadata.get("stage") == RAW_STEM_CACHE_STAGE
        and metadata.get("sample_rate") == sample_rate
        and metadata.get("frame_count") == identity.frame_count
        and metadata.get("manifest_sha256")
        == identity.manifest_sha256
        and shape_matches
    )


def _close_cache_lookup(lookup: Any | None) -> None:
    source = None if lookup is None else getattr(lookup, "source", None)
    if source is not None:
        source.close()


def _load_stem_cache_for_render(
    cache: StemCache,
    key: str,
    *,
    snapshot_directory: Path,
    stream_cache_hits: bool,
    direct_cache_fallback: bool = False,
) -> Any:
    """Prefer RAM for small hits and bounded scratch for long tracks."""

    if not stream_cache_hits:
        # Collaboration analysis needs random access to the complete stem, so
        # snapshotting first would only add I/O and a disk-space dependency.
        return cache.load(key)
    lookup = cache.load(
        key,
        maximum_audio_bytes=_DIRECT_STEM_CACHE_LOAD_BYTES,
    )
    if lookup.status != "too_large":
        return lookup
    snapshot = cache.open_verified(
        key,
        snapshot_directory=snapshot_directory,
    )
    if not direct_cache_fallback or snapshot.status != "unavailable":
        return snapshot

    # Collaboration analysis historically loaded a verified cache hit into
    # memory.  If bounded snapshot scratch is unavailable, retain that exact
    # hit path instead of silently turning it into a render miss.  Manual
    # rendering leaves this disabled and therefore keeps its bounded contract.
    _close_cache_lookup(snapshot)
    return cache.load(key)


def _prepare_parallel_stem_cache(
    part: Any,
    sample_rate: int,
    *,
    cache: StemCache,
    snapshot_directory: Path,
    stream_cache_hits: bool,
    refresh: bool,
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ],
    summary: dict[str, Any],
    direct_cache_fallback: bool = False,
) -> _PreparedParallelStemCache:
    """Inspect one cache entry without changing ordered telemetry yet.

    A parallel look-ahead window may finish later stems first.  Deferring all
    counters until the coordinator consumes this part keeps the existing
    session-disable and reason ordering identical to serial rendering.
    """

    if not summary["active"]:
        return _PreparedParallelStemCache(
            None,
            None,
            (("bypassed", "session_disabled"),),
        )
    if not current_source_tree_matches():
        return _PreparedParallelStemCache(
            None,
            None,
            (
                (
                    "bypassed",
                    "producer_source_changed_restart_required",
                ),
            ),
            disable_session=True,
        )
    try:
        identity = _raw_stem_cache_identity(
            part,
            sample_rate,
            runtime_fingerprints,
        )
    except MemoryError:
        raise
    except Exception:
        return _PreparedParallelStemCache(
            None,
            None,
            (("bypassed", "live_identity_unavailable"),),
        )

    if refresh:
        return _PreparedParallelStemCache(
            identity,
            None,
            (("misses", "refresh_requested"),),
        )

    lookup = _load_stem_cache_for_render(
        cache,
        identity.key,
        snapshot_directory=snapshot_directory,
        stream_cache_hits=stream_cache_hits,
        direct_cache_fallback=direct_cache_fallback,
    )
    if _cache_lookup_matches(lookup, identity, sample_rate):
        return _PreparedParallelStemCache(
            identity,
            lookup,
            (("hits", "verified_hit"),),
        )
    if lookup.status in ("corrupt", "incomplete") or lookup.hit:
        _close_cache_lookup(lookup)
        return _PreparedParallelStemCache(
            identity,
            None,
            (
                (
                    "corrupt_fallbacks",
                    "metadata_mismatch" if lookup.hit else lookup.status,
                ),
                ("misses", "corrupt_or_incomplete"),
            ),
        )
    if lookup.status == "missing":
        return _PreparedParallelStemCache(
            identity,
            None,
            (("misses", "not_found"),),
        )
    return _PreparedParallelStemCache(
        identity,
        None,
        (("bypassed", f"lookup_{lookup.status}"),),
    )


def _activate_prepared_cache_accounting(
    prepared: _PreparedParallelStemCache,
    summary: dict[str, Any],
) -> bool:
    """Apply deferred counters and report whether publication stays eligible."""

    if not summary["active"]:
        _note_cache_result(summary, "bypassed", "session_disabled")
        return False
    if prepared.disable_session:
        summary["active"] = False
    for field, reason in prepared.accounting:
        _note_cache_result(summary, field, reason)
    return prepared.identity is not None and not prepared.disable_session


def _note_cache_store_result(
    summary: dict[str, Any],
    stored: Any,
) -> None:
    """Map buffered and streaming stores into the established telemetry."""

    if stored.status in ("stored", "repaired"):
        _note_cache_result(
            summary,
            "writes",
            f"store_{stored.status}",
        )
    elif stored.status in ("exists", "busy"):
        _note_cache_result(
            summary,
            "write_skips",
            f"store_{stored.status}",
        )
    elif stored.status == "conflict":
        _note_cache_result(summary, "conflicts", "store_conflict")
    else:
        _note_cache_result(
            summary,
            "write_failures",
            f"store_{stored.status}",
        )


def _cache_publication_is_live(
    part: Any,
    sample_rate: int,
    *,
    summary: dict[str, Any],
    identity: _RawStemCacheIdentity,
    manifest_sha256: str,
) -> bool:
    """Recheck every live identity after authoritative source completion."""

    if manifest_sha256 != identity.manifest_sha256:
        _note_cache_result(
            summary,
            "write_skips",
            "manifest_changed_during_render",
        )
        return False
    if not current_source_tree_matches():
        summary["active"] = False
        _note_cache_result(
            summary,
            "write_skips",
            "producer_source_changed_during_render",
        )
        return False
    try:
        post_render_identity = _raw_stem_cache_identity(
            part,
            sample_rate,
            {},
        )
    except MemoryError:
        raise
    except Exception:
        summary["active"] = False
        _note_cache_result(
            summary,
            "write_skips",
            "live_identity_recheck_unavailable",
        )
        return False
    if post_render_identity.key != identity.key:
        summary["active"] = False
        _note_cache_result(
            summary,
            "write_skips",
            "live_identity_changed_during_render",
        )
        return False
    return True


def _store_parallel_rendered_stem(
    part: Any,
    sample_rate: int,
    *,
    cache: StemCache,
    summary: dict[str, Any],
    identity: _RawStemCacheIdentity,
    buffer: Any,
    peak_voices: int,
    manifest_sha256: str,
) -> None:
    """Publish an already-materialised serial stem after the live gates."""

    if not _cache_publication_is_live(
        part,
        sample_rate,
        summary=summary,
        identity=identity,
        manifest_sha256=manifest_sha256,
    ):
        return

    stored = cache.store(
        identity.key,
        buffer,
        stage=RAW_STEM_CACHE_STAGE,
        sample_rate=sample_rate,
        peak_voices=peak_voices,
        manifest_sha256=manifest_sha256,
    )
    _note_cache_store_result(summary, stored)


class _StreamedRawStemIterator:
    """Release the wrapped source even if iteration never starts."""

    __slots__ = ("_owner", "_iterator", "_closed")

    def __init__(self, owner: "_StreamedRawStem", iterator: Any) -> None:
        self._owner: _StreamedRawStem | None = owner
        self._iterator = iterator
        self._closed = False

    def __iter__(self) -> "_StreamedRawStemIterator":
        return self

    def __next__(self) -> Any:
        if self._closed or self._iterator is None:
            raise StopIteration
        try:
            return next(self._iterator)
        except StopIteration:
            self._closed = True
            self._iterator = None
            self._owner = None
            raise
        except BaseException:
            self._closed = True
            self._iterator = None
            self._owner = None
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        iterator = self._iterator
        owner = self._owner
        self._iterator = None
        self._owner = None
        if iterator is not None:
            try:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            except BaseException as exc:
                pass
        if owner is not None:
            owner._abort(suppress_errors=True)

    def __del__(self) -> None:
        try:
            self.close()
        except BaseException:
            pass


class _StreamedRawStem:
    """Single-consumer raw source with an optional cache tee transaction."""

    __slots__ = (
        "_source",
        "_transaction",
        "_finish_cache",
        "_consumed",
        "_iterator_active",
        "_closed",
        "_completed",
    )

    def __init__(
        self,
        source: StemBlockSource,
        *,
        transaction: Any | None = None,
        finish_cache: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(source, StemBlockSource):
            raise TypeError("streamed raw stem requires a StemBlockSource")
        if source.closed:
            raise ValueError("streamed raw stem source is closed")
        if (transaction is None) != (finish_cache is None):
            raise ValueError(
                "streamed raw cache transaction and finisher must be paired"
            )
        self._source = source
        self._transaction = transaction
        self._finish_cache = finish_cache
        self._consumed = False
        self._iterator_active = False
        self._closed = False
        self._completed = False

    @property
    def frame_count(self) -> int:
        return self._source.frame_count

    @property
    def shape(self) -> tuple[int, int]:
        return self._source.shape

    @property
    def audio_sha256(self) -> str:
        return self._source.audio_sha256

    @property
    def closed(self) -> bool:
        return self._closed

    def _abort(self, *, suppress_errors: bool) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None
        transaction = self._transaction
        self._transaction = None
        self._finish_cache = None
        if transaction is not None:
            try:
                transaction.abort()
            except BaseException as exc:
                first_error = exc
        try:
            self._source.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None and not suppress_errors:
            raise first_error

    def iter_blocks(self, block_frames: int = 65_536) -> Any:
        if (
            isinstance(block_frames, bool)
            or not isinstance(block_frames, int)
            or block_frames <= 0
            or block_frames > 65_536
        ):
            raise ValueError("block_frames must be between 1 and 65536")
        if self._closed:
            raise ValueError("streamed raw stem is closed")
        if self._consumed or self._iterator_active:
            raise ValueError("streamed raw stem can only be consumed once")
        self._consumed = True
        self._iterator_active = True
        return _StreamedRawStemIterator(
            self,
            self._iter_blocks(block_frames),
        )

    def _iter_blocks(self, block_frames: int) -> Any:
        completed = False
        try:
            for block in self._source.iter_blocks(block_frames):
                transaction = self._transaction
                if transaction is not None:
                    # The exact immutable pre-gain bytes enter the cache tee
                    # before the sole writable processing copy is exposed.
                    transaction.append(block)
                yield block

            # Exhaustion above includes the source's SHA/finite/identity and
            # length gates.  Its close must also succeed before publication.
            self._source.close()
            finish_cache = self._finish_cache
            if finish_cache is not None:
                finish_cache()
            self._transaction = None
            self._finish_cache = None
            self._completed = True
            self._closed = True
            completed = True
        except BaseException:
            self._abort(suppress_errors=True)
            raise
        finally:
            self._iterator_active = False
            if not completed and not self._closed:
                self._abort(suppress_errors=True)

    def materialise(self) -> Any:
        import numpy as np

        blocks = self.iter_blocks()
        try:
            audio = np.empty(self.shape, dtype="<f4")
            offset = 0
            for block in blocks:
                stop = offset + int(block.shape[0])
                if stop > self.frame_count:
                    raise RuntimeError("streamed raw stem produced excess frames")
                audio[offset:stop] = block
                offset = stop
            if offset != self.frame_count:
                raise RuntimeError("streamed raw stem changed frame count")
            return audio
        except BaseException:
            try:
                blocks.close()
            except BaseException as exc:
                pass
            self._abort(suppress_errors=True)
            raise

    def close(self) -> None:
        if self._completed:
            return
        self._abort(suppress_errors=False)

    def __enter__(self) -> "_StreamedRawStem":
        if self._closed:
            raise ValueError("streamed raw stem is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self._abort(suppress_errors=True)
        except BaseException:
            pass


def _render_part_cached(
    part: Any,
    sample_rate: int,
    *,
    cache: StemCache,
    snapshot_directory: Path,
    stream_cache_hits: bool,
    refresh: bool,
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ],
    summary: dict[str, Any],
    adaptive_session: AdaptiveRenderSession | None = None,
    adaptive_backend_key: str = "",
    adaptive_work_frames: int = 0,
    stream_output: bool = False,
    direct_cache_fallback: bool = False,
) -> tuple[Any, int, str]:
    """Reuse one raw stem when every live input still matches."""

    if not summary["active"]:
        _note_cache_result(summary, "bypassed", "session_disabled")
        return _render_part_adaptively(
            part,
            sample_rate,
            adaptive_session=adaptive_session,
            adaptive_backend_key=adaptive_backend_key,
            adaptive_work_frames=adaptive_work_frames,
            stream_output=stream_output,
            scratch_directory=snapshot_directory,
        )
    if not current_source_tree_matches():
        summary["active"] = False
        _note_cache_result(
            summary,
            "bypassed",
            "producer_source_changed_restart_required",
        )
        return _render_part_adaptively(
            part,
            sample_rate,
            adaptive_session=adaptive_session,
            adaptive_backend_key=adaptive_backend_key,
            adaptive_work_frames=adaptive_work_frames,
            stream_output=stream_output,
            scratch_directory=snapshot_directory,
        )

    try:
        identity = _raw_stem_cache_identity(
            part,
            sample_rate,
            runtime_fingerprints,
        )
    except MemoryError:
        raise
    except Exception:
        # Fingerprinting is a safety gate, not a new render dependency.
        # External manifests or unavailable evidence simply take the audited
        # uncached path; they are never allowed a weak cache identity.
        _note_cache_result(
            summary,
            "bypassed",
            "live_identity_unavailable",
        )
        return _render_part_adaptively(
            part,
            sample_rate,
            adaptive_session=adaptive_session,
            adaptive_backend_key=adaptive_backend_key,
            adaptive_work_frames=adaptive_work_frames,
            stream_output=stream_output,
            scratch_directory=snapshot_directory,
        )

    if refresh:
        _note_cache_result(summary, "misses", "refresh_requested")
    else:
        lookup = _load_stem_cache_for_render(
            cache,
            identity.key,
            snapshot_directory=snapshot_directory,
            stream_cache_hits=stream_cache_hits,
            direct_cache_fallback=direct_cache_fallback,
        )
        if _cache_lookup_matches(lookup, identity, sample_rate):
            assert lookup.record is not None
            # A long cache verification/snapshot copy must not bridge a live
            # source edit.  Close the private source before taking the normal
            # renderer so a non-authoritative cache never pins an obsolete
            # producer generation into this render.
            if not current_source_tree_matches():
                _close_cache_lookup(lookup)
                summary["active"] = False
                _note_cache_result(
                    summary,
                    "bypassed",
                    "producer_source_changed_restart_required",
                )
                return _render_part_adaptively(
                    part,
                    sample_rate,
                    adaptive_session=adaptive_session,
                    adaptive_backend_key=adaptive_backend_key,
                    adaptive_work_frames=adaptive_work_frames,
                    stream_output=stream_output,
                    scratch_directory=snapshot_directory,
                )
            _note_cache_result(summary, "hits", "verified_hit")
            return (
                (
                    lookup.source
                    if lookup.source is not None
                    else lookup.audio
                ),
                int(lookup.record.metadata["peak_voices"]),
                identity.manifest_sha256,
            )
        if lookup.status in ("corrupt", "incomplete") or lookup.hit:
            _note_cache_result(
                summary,
                "corrupt_fallbacks",
                (
                    "metadata_mismatch"
                    if lookup.hit
                    else lookup.status
                ),
            )
            _note_cache_result(
                summary,
                "misses",
                "corrupt_or_incomplete",
            )
        elif lookup.status == "missing":
            _note_cache_result(summary, "misses", "not_found")
        else:
            _note_cache_result(
                summary,
                "bypassed",
                f"lookup_{lookup.status}",
            )
        # A structurally valid cache payload can still fail the live semantic
        # identity check.  Drop its track-sized ndarray before rendering the
        # replacement so the serial refresh path also keeps one parent stem.
        _close_cache_lookup(lookup)
        lookup = None

    buffer, peak_voices, manifest_sha256 = _render_part_adaptively(
        part,
        sample_rate,
        adaptive_session=adaptive_session,
        adaptive_backend_key=adaptive_backend_key,
        adaptive_work_frames=adaptive_work_frames,
        stream_output=stream_output,
        scratch_directory=snapshot_directory,
    )
    if isinstance(buffer, StemBlockSource):
        buffer = _wrap_streamed_cache_store(
            part,
            sample_rate,
            source=buffer,
            cache=cache,
            summary=summary,
            identity=identity,
            peak_voices=peak_voices,
            manifest_sha256=manifest_sha256,
        )
    else:
        _store_parallel_rendered_stem(
            part,
            sample_rate,
            cache=cache,
            summary=summary,
            identity=identity,
            buffer=buffer,
            peak_voices=peak_voices,
            manifest_sha256=manifest_sha256,
        )
    return buffer, peak_voices, manifest_sha256


def _render_part(part: Any, sample_rate: int) -> tuple[Any, int, str]:
    # A serial barrier (heavy/sample/native part or a transparent worker
    # fallback) is budgeted without any idle subprocess RSS.  Preserve warm
    # reuse across cache hits, which never enter this renderer, but physically
    # reap idle stem children before allocating an in-process instrument.
    retire_idle_stem_workers()
    import numpy as np

    manifest_path = Path(part.executor.capability.manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    # 编制表可为这一次使用微调乐器的标量参数(如调短 release_seconds 以减轻
    # 密集段落的回音),覆盖只作用于本执行器的实例,不动乐器目录里的清单。
    override_map = part.executor.override_map
    if override_map:
        manifest = {**manifest, **override_map}
    document = parse_performance_document(part.performance)
    if document.sample_rate != sample_rate:
        raise ValueError(
            f"声部 {part.executor.executor_id!r} 的采样率与总谱不一致"
        )
    instrument = create_instrument(
        manifest, sample_rate, base_directory=str(manifest_path.parent)
    )
    try:
        if _prefer_frame_stream_path(instrument, document):
            frames, peak = render_document(instrument, document)

            def stereo_samples() -> Any:
                for left, right in frames:
                    yield left
                    yield right

            buffer = np.fromiter(
                stereo_samples(),
                dtype=np.float32,
                count=document.total_samples * 2,
            ).reshape(document.total_samples, 2)
        else:
            blocks, peak = render_document_blocks(
                instrument,
                document,
                sample_dtype=np.float32,
            )
            buffer = np.empty((document.total_samples, 2), dtype=np.float32)
            offset = 0
            for block in blocks:
                frame_count = int(block.shape[0])
                stop = offset + frame_count
                if stop > document.total_samples:
                    raise RuntimeError("stem renderer produced excess frames")
                buffer[offset:stop] = block
                offset = stop
            if offset != document.total_samples:
                raise RuntimeError(
                    "stem renderer produced an invalid frame count"
                )
        return buffer, peak[0], manifest_sha256
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()


def _prefer_streamed_serial_stem(
    part: Any,
    sample_rate: int,
    scratch_directory: Path,
) -> bool:
    """Use private scratch only when it removes meaningful coordinator RAM."""

    try:
        # Private embedders and tests have historically replaced
        # ``_render_part`` as the in-process compatibility seam.  Never route
        # around that replacement merely because a stem is long.
        if _render_part is not _ORIGINAL_RENDER_PART:
            return False
        document = parse_performance_document(part.performance)
        if document.sample_rate != sample_rate:
            return False
        byte_count = document.total_samples * 2 * 4
        if byte_count <= _DIRECT_SERIAL_STEM_LOAD_BYTES:
            return False
        scratch = scratch_directory.resolve(strict=True)
        # A published PCM24 stem may be written while the raw private source
        # is still open.  ``write_stems`` is intentionally not threaded into
        # this internal transport choice; always reserving its exact 6-byte
        # frame payload keeps the optional fast path safe and conservative.
        possible_pcm24_stem = (
            document.total_samples * 2 * 3
            + _STREAMED_STEM_OUTPUT_MARGIN_BYTES
        )
        return shutil.disk_usage(scratch).free >= (
            byte_count
            + max(
                _STREAMED_STEM_FREE_RESERVE_BYTES,
                possible_pcm24_stem,
            )
        )
    except MemoryError:
        raise
    except Exception:
        # This is only a transport choice.  The established renderer remains
        # responsible for reporting malformed performance input precisely.
        return False


def _revalidate_serial_stem_scratch(
    identity: PlainDirectoryIdentity,
    expected_volume_id: str,
    *,
    lease: Any | None = None,
) -> Path:
    directory = revalidate_plain_directory(identity)
    if scratch_volume_identity(directory) != expected_volume_id:
        raise WorkerSlotError("serial stem scratch volume identity changed")
    if lease is None:
        return directory
    lease_identity = capture_plain_directory(lease.scratch_directory)
    if not _same_plain_directory_identity(identity, lease_identity):
        raise WorkerSlotError("serial stem scratch lease has the wrong directory")
    return directory


def _try_reserve_serial_stem_scratch(
    scratch_directory: Path,
    *,
    scratch_bytes: int,
) -> tuple[Any, PlainDirectoryIdentity, str] | None:
    """Reserve one exact raw source, retaining RAM as the optional fallback."""

    identity = capture_plain_directory(scratch_directory)
    directory = revalidate_plain_directory(identity)
    expected_volume_id = scratch_volume_identity(directory)
    try:
        pool = _session_scratch_pool_factory()
        lease = pool.reserve_session_scratch(
            SessionScratchClaim(
                scratch_bytes=scratch_bytes,
                scratch_directory=directory,
            )
        )
    except MemoryError:
        raise
    except (OSError, ValueError, WorkerSlotError):
        _revalidate_serial_stem_scratch(identity, expected_volume_id)
        return None
    if lease is None:
        _revalidate_serial_stem_scratch(identity, expected_volume_id)
        return None
    try:
        _revalidate_serial_stem_scratch(
            identity,
            expected_volume_id,
            lease=lease,
        )
        admitted_claim = getattr(lease, "claim", None)
        if (
            getattr(admitted_claim, "scratch_bytes", None) != scratch_bytes
            or getattr(admitted_claim, "scratch_volume_id", None)
            != expected_volume_id
        ):
            raise WorkerSlotError("serial stem scratch lease has the wrong claim")
    except BaseException:
        try:
            lease.close()
        except BaseException:
            pass
        raise
    return lease, identity, expected_volume_id


def _render_part_source(
    part: Any,
    sample_rate: int,
    *,
    scratch_directory: Path,
) -> tuple[StemBlockSource, int, str]:
    """Render one long serial stem to bounded private float32 scratch."""

    retire_idle_stem_workers()
    import numpy as np

    manifest_path = Path(part.executor.capability.manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    override_map = part.executor.override_map
    if override_map:
        manifest = {**manifest, **override_map}
    document = parse_performance_document(part.performance)
    if document.sample_rate != sample_rate:
        raise ValueError(
            f"声部 {part.executor.executor_id!r} 的采样率与总谱不一致"
        )

    reserved = _try_reserve_serial_stem_scratch(
        scratch_directory,
        scratch_bytes=document.total_samples * 2 * 4,
    )
    if reserved is None:
        return _render_part(part, sample_rate)
    scratch_lease, scratch_identity, scratch_volume_id = reserved

    try:
        temporary: Any | None = tempfile.TemporaryFile(
            mode="w+b",
            dir=scratch_lease.scratch_directory,
        )
    except MemoryError:
        try:
            scratch_lease.close()
        except BaseException:
            pass
        raise
    except OSError:
        # The transport is optional and no instrument has been constructed
        # yet, so an unavailable scratch handle can safely retain the exact
        # established in-memory renderer.
        try:
            _revalidate_serial_stem_scratch(
                scratch_identity,
                scratch_volume_id,
                lease=scratch_lease,
            )
        except BaseException:
            try:
                scratch_lease.close()
            except BaseException:
                pass
            raise
        try:
            scratch_lease.close()
        except BaseException:
            pass
        return _render_part(part, sample_rate)
    try:
        _revalidate_serial_stem_scratch(
            scratch_identity,
            scratch_volume_id,
            lease=scratch_lease,
        )
    except BaseException:
        try:
            temporary.close()
        except BaseException:
            pass
        try:
            scratch_lease.close()
        except BaseException:
            pass
        raise
    digest = hashlib.sha256()
    written_frames = 0

    def append_block(raw_block: Any) -> None:
        nonlocal written_frames

        block = np.asarray(raw_block, dtype="<f4", order="C")
        if block.ndim != 2 or block.shape[1:] != (2,):
            raise RuntimeError("stem renderer produced an invalid block")
        frame_count = int(block.shape[0])
        if frame_count <= 0 or frame_count > 65_536:
            raise RuntimeError("stem renderer produced an invalid block size")
        if written_frames + frame_count > document.total_samples:
            raise RuntimeError("stem renderer produced excess frames")
        payload = memoryview(block).cast("B")
        digest.update(payload)
        offset = 0
        while offset < len(payload):
            count = temporary.write(payload[offset:])
            if count is None or count <= 0:
                raise OSError("stem scratch write made no progress")
            offset += count
        written_frames += frame_count

    instrument: Any | None = None
    render_failed = False
    try:
        instrument = create_instrument(
            manifest,
            sample_rate,
            base_directory=str(manifest_path.parent),
        )
        if _prefer_frame_stream_path(instrument, document):
            frames, peak = render_document(instrument, document)

            def stereo_samples() -> Any:
                for left, right in frames:
                    yield left
                    yield right

            samples = stereo_samples()
            remaining = document.total_samples
            while remaining:
                frame_count = min(65_536, remaining)
                block = np.fromiter(
                    samples,
                    dtype=np.float32,
                    count=frame_count * 2,
                )
                if block.size != frame_count * 2:
                    raise RuntimeError(
                        "stem renderer produced an invalid frame count"
                    )
                append_block(block.reshape(frame_count, 2))
                remaining -= frame_count
        else:
            blocks, peak = render_document_blocks(
                instrument,
                document,
                sample_dtype=np.float32,
            )
            for block in blocks:
                append_block(block)
        if written_frames != document.total_samples:
            raise RuntimeError("stem renderer produced an invalid frame count")
    except BaseException:
        render_failed = True
        try:
            temporary.close()
        except BaseException:
            pass
        try:
            scratch_lease.close()
        except BaseException:
            pass
        raise
    finally:
        close = None if instrument is None else getattr(instrument, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as exc:
                try:
                    temporary.close()
                except BaseException:
                    pass
                try:
                    scratch_lease.close()
                except BaseException:
                    pass
                if render_failed:
                    _warn_cleanup(
                        "serial stem instrument cleanup did not complete: "
                        f"{type(exc).__name__}"
                    )
                else:
                    raise

    try:
        temporary.flush()
        source = OwnedStemSource(
            temporary,
            audio_offset=0,
            frame_count=document.total_samples,
            expected_sha256=digest.hexdigest(),
            completion_callback=lambda _success: scratch_lease.close(),
        )
    except BaseException:
        try:
            temporary.close()
        except BaseException:
            pass
        try:
            scratch_lease.close()
        except BaseException:
            pass
        raise
    temporary = None
    return source, peak[0], manifest_sha256


_ORIGINAL_RENDER_PART = _render_part
_ORIGINAL_CREATE_INSTRUMENT = create_instrument
_ORIGINAL_PARSE_PERFORMANCE_DOCUMENT = parse_performance_document
_ORIGINAL_RENDER_DOCUMENT = render_document
_ORIGINAL_RENDER_DOCUMENT_BLOCKS = render_document_blocks
_ORIGINAL_PREFER_DENSE_SYNTH_FRAME_PATH = _prefer_dense_synth_frame_path
_ORIGINAL_PREFER_FRAME_STREAM_PATH = _prefer_frame_stream_path
_ORIGINAL_RENDERER_EXACT_BUILTIN_RENDER_BLOCK = (
    _renderer_module._exact_builtin_render_block
)
_ORIGINAL_RENDERER_PREFER_DENSE_SYNTH_FRAME_PATH = (
    _renderer_module._prefer_dense_synth_frame_path
)
_ORIGINAL_RENDERER_PREFER_FRAME_STREAM_PATH = (
    _renderer_module._prefer_frame_stream_path
)
_ORIGINAL_RENDERER_RENDER_DOCUMENT = _renderer_module.render_document
_ORIGINAL_RENDERER_RENDER_DOCUMENT_BLOCKS = (
    _renderer_module.render_document_blocks
)


class _ManagedStemBatchFailure(RuntimeError):
    """Internal signal that the remaining batch must use the serial path."""

    def __init__(self, position: int, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.position = position
        self.__cause__ = cause


def _warn_cleanup(message: str) -> None:
    """Report best-effort cleanup without ever replacing a primary error."""

    try:
        warnings.warn(message, RuntimeWarning)
    except BaseException:
        pass


@dataclass(frozen=True, slots=True)
class _AutomaticStemParallelism:
    worker_count: int
    worker_count_by_part: tuple[int, ...]
    manifest_sha256_by_part: tuple[str, ...]
    worker_reserve_bytes_by_part: tuple[int, ...] = ()
    sample_backed_by_part: tuple[bool, ...] = ()
    adaptive_backend_key_by_part: tuple[str, ...] = ()
    adaptive_work_frames_by_part: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class _ManagedWorkerSlotContext:
    """Private resource evidence shared by one render coordinator."""

    pool: WorkerSlotPool
    owner_id: str
    owner_cpu_capacity: int
    worker_memory_bytes_by_part: tuple[int, ...]
    coordinator_memory_bytes: int
    memory_budget_bytes: int
    scratch_directory: Path


def _managed_warm_binding_for_run(
    *,
    start: int,
    end: int,
    parts: tuple[Any, ...],
    slot_context: _ManagedWorkerSlotContext,
) -> _ManagedWarmBinding | None:
    """Prove one conservative per-slot ceiling for a complete known run."""

    try:
        if not (0 <= start < end <= len(parts)):
            return None
        worker_memory_ceiling = max(
            slot_context.worker_memory_bytes_by_part[index]
            for index in range(start, end)
        )
        scratch_ceiling = max(
            parse_performance_document(parts[index].performance).total_samples
            * 2
            * 4
            for index in range(start, end)
        )
        binding = _ManagedWarmBinding(
            owner_id=slot_context.owner_id,
            scratch_directory=slot_context.scratch_directory,
            scratch_volume_id=scratch_volume_identity(
                slot_context.scratch_directory
            ),
            worker_memory_ceiling_bytes=worker_memory_ceiling,
            coordinator_memory_bytes=(
                slot_context.coordinator_memory_bytes + scratch_ceiling
            ),
            memory_budget_bytes=slot_context.memory_budget_bytes,
            scratch_ceiling_bytes=scratch_ceiling,
        )
        return binding
    except MemoryError:
        raise
    except Exception:
        return None


def _automatic_worker_slot_context(
    plan: PerformancePlan,
    *,
    parallelism: _AutomaticStemParallelism,
    scratch_directory: Path,
    hall_tail_seconds: float,
) -> _ManagedWorkerSlotContext | None:
    """Bind global admission to the same evidence as local parallelism."""

    try:
        parts = tuple(plan.parts)
        if (
            len(parallelism.worker_count_by_part) != len(parts)
            or len(parallelism.manifest_sha256_by_part) != len(parts)
        ):
            return None
        worker_reserves = parallelism.worker_reserve_bytes_by_part
        sample_backed = parallelism.sample_backed_by_part
        if (
            len(worker_reserves) != len(parts)
            or len(sample_backed) != len(parts)
        ):
            # Compatibility for tests or embedders constructing the private
            # decision object directly: recover only from independent
            # per-part probes.  Never use the whole-plan probe because a
            # serial barrier can leave later entries at an unverified default.
            recovered_reserves: list[int] = []
            recovered_sample_backed: list[bool] = []
            for index, part in enumerate(parts):
                estimate = derive_worker_resource_estimate(
                    SimpleNamespace(parts=(part,))
                )
                reserve = estimate.worker_reserve_bytes_by_part
                sample = estimate.sample_backed_by_part
                manifest = estimate.manifest_sha256_by_part
                if not (len(reserve) == len(sample) == len(manifest) == 1):
                    return None
                if parallelism.worker_count_by_part[index] > 1 and (
                    not estimate.workers_safe
                    or estimate.managed_worker_safe_by_part != (True,)
                    or manifest[0]
                    != parallelism.manifest_sha256_by_part[index]
                ):
                    return None
                recovered_reserves.append(int(reserve[0]))
                recovered_sample_backed.append(bool(sample[0]))
            worker_reserves = tuple(recovered_reserves)
            sample_backed = tuple(recovered_sample_backed)
        scratch = scratch_directory.resolve(strict=True)
        if not scratch.is_dir():
            return None
        limits = ProjectLimits.from_environment()
        decision = select_render_parallelism(
            plan,
            hall_tail_seconds=hall_tail_seconds,
            limits=limits,
            # This call extracts resource facts only.  Per-part eligibility
            # and the actual worker count remain bound by the independently
            # authorised decision above.
            workers_safe=False,
            scratch_available_bytes=shutil.disk_usage(scratch).free,
            worker_reserve_bytes_by_part=worker_reserves,
            sample_backed_by_part=sample_backed,
        )
        worker_memory = tuple(
            int(value) + _MANAGED_WORKER_CHUNK_BYTES
            for value in worker_reserves
        )
        if (
            not worker_memory
            or any(value <= 0 for value in worker_memory)
            or decision.coordinator_bytes <= 0
            or decision.memory_budget_bytes <= 0
        ):
            return None
        return _ManagedWorkerSlotContext(
            pool=WorkerSlotPool(),
            owner_id=uuid.uuid4().hex,
            owner_cpu_capacity=automatic_worker_capacity(),
            worker_memory_bytes_by_part=worker_memory,
            coordinator_memory_bytes=int(decision.coordinator_bytes),
            memory_budget_bytes=int(decision.memory_budget_bytes),
            scratch_directory=scratch,
        )
    except MemoryError:
        raise
    except Exception:
        # The ledger is an optional managed-child throttle.  An unavailable
        # per-user directory, volume identity or host fact keeps the complete
        # in-process renderer rather than becoming a user-facing failure.
        return None


def _parallel_runtime_is_pristine() -> bool:
    """Avoid moving monkeypatched or embedded renderer state into children."""

    return (
        _render_part is _ORIGINAL_RENDER_PART
        and create_instrument is _ORIGINAL_CREATE_INSTRUMENT
        and parse_performance_document
        is _ORIGINAL_PARSE_PERFORMANCE_DOCUMENT
        and render_document is _ORIGINAL_RENDER_DOCUMENT
        and render_document_blocks is _ORIGINAL_RENDER_DOCUMENT_BLOCKS
        and _prefer_dense_synth_frame_path
        is _ORIGINAL_PREFER_DENSE_SYNTH_FRAME_PATH
        and _prefer_frame_stream_path
        is _ORIGINAL_PREFER_FRAME_STREAM_PATH
        and _renderer_module._exact_builtin_render_block
        is _ORIGINAL_RENDERER_EXACT_BUILTIN_RENDER_BLOCK
        and _renderer_module._prefer_dense_synth_frame_path
        is _ORIGINAL_RENDERER_PREFER_DENSE_SYNTH_FRAME_PATH
        and _renderer_module._prefer_frame_stream_path
        is _ORIGINAL_RENDERER_PREFER_FRAME_STREAM_PATH
        and _renderer_module.render_document
        is _ORIGINAL_RENDERER_RENDER_DOCUMENT
        and _renderer_module.render_document_blocks
        is _ORIGINAL_RENDERER_RENDER_DOCUMENT_BLOCKS
        and managed_subprocess_workers_available()
    )


def _automatic_stem_worker_count(
    plan: PerformancePlan,
    *,
    scratch_directory: Path,
    hall_tail_seconds: float,
    _resources: Any | None = None,
    adaptive_session: AdaptiveRenderSession | None = None,
    adaptive_workloads: tuple[AdaptiveWorkload, ...] = (),
) -> int:
    """Select an internal worker count; every uncertainty means serial."""

    try:
        resources = (
            derive_worker_resource_estimate(plan)
            if _resources is None
            else _resources
        )
        scratch_free = shutil.disk_usage(scratch_directory).free
        workers_safe = (
            resources.workers_safe
            and all(resources.managed_worker_safe_by_part)
            and _parallel_runtime_is_pristine()
            and current_source_tree_matches()
        )
        decision = select_render_parallelism(
            plan,
            hall_tail_seconds=hall_tail_seconds,
            workers_safe=workers_safe,
            scratch_available_bytes=scratch_free,
            worker_reserve_bytes_by_part=(
                resources.worker_reserve_bytes_by_part
            ),
            sample_backed_by_part=resources.sample_backed_by_part,
        )
        if (
            adaptive_session is not None
            and len(adaptive_workloads) == len(plan.parts)
            and adaptive_workloads
        ):
            recommendation = adaptive_session.recommend(
                decision,
                adaptive_workloads,
                managed_execution="managed_cold",
            )
            worker_limit = getattr(recommendation, "worker_limit", None)
            allow_short = getattr(
                recommendation,
                "allow_short_workload",
                False,
            )
            if (
                type(worker_limit) is int
                and 1 <= worker_limit <= automatic_worker_capacity()
                and type(allow_short) is bool
            ):
                decision = select_render_parallelism(
                    plan,
                    hall_tail_seconds=hall_tail_seconds,
                    workers_safe=workers_safe,
                    scratch_available_bytes=scratch_free,
                    worker_reserve_bytes_by_part=(
                        resources.worker_reserve_bytes_by_part
                    ),
                    sample_backed_by_part=(
                        resources.sample_backed_by_part
                    ),
                    adaptive_worker_limit=worker_limit,
                    adaptive_short_workload=allow_short,
                )
        return decision.worker_count
    except MemoryError:
        raise
    except Exception:
        # Parallelism is an optional execution detail.  The established
        # renderer remains the complete fallback for probes, disk facts or
        # custom plan objects that cannot prove worker safety.
        return 1


def _balanced_worker_group_sizes(
    part_count: int,
    maximum_workers: int,
) -> tuple[int, ...]:
    """Split a run without leaving an avoidable one-part serial tail."""

    maximum_workers = min(maximum_workers, part_count)
    if part_count <= 0 or maximum_workers <= 0:
        return ()
    if maximum_workers == 1:
        return (1,) * part_count
    sizes: list[int] = []
    remaining = part_count
    while remaining:
        size = min(maximum_workers, remaining)
        if (
            maximum_workers > 2
            and remaining > maximum_workers
            and remaining % maximum_workers == 1
        ):
            size = maximum_workers - 1
        sizes.append(size)
        remaining -= size
    return tuple(sizes)


def _automatic_stem_parallelism(
    plan: PerformancePlan,
    *,
    scratch_directory: Path,
    hall_tail_seconds: float,
    adaptive_session: AdaptiveRenderSession | None = None,
) -> _AutomaticStemParallelism:
    """Bind one worker decision to the manifests it authorised."""

    try:
        parts = tuple(plan.parts)
        memory_budget = max(
            1,
            int(ProjectLimits.from_environment().max_audio_memory_bytes),
        )
        counts = [1] * len(parts)
        hashes = [""] * len(parts)
        worker_reserves = [memory_budget] * len(parts)
        sample_backed = [False] * len(parts)
        estimates: list[Any | None] = []
        eligible: list[bool] = []
        estimate_cache: dict[tuple[str, bytes], Any] = {}
        for index, part in enumerate(parts):
            try:
                estimate_key: tuple[str, bytes] | None = (
                    os.fspath(part.executor.capability.manifest_path),
                    _project_canonical_json_bytes(
                        part.executor.override_map
                    ),
                )
            except (AttributeError, TypeError, ValueError, OverflowError):
                estimate_key = None
            estimate = (
                estimate_cache.get(estimate_key)
                if estimate_key is not None
                else None
            )
            if estimate is None:
                estimate = derive_worker_resource_estimate(
                    SimpleNamespace(parts=(part,))
                )
                if estimate_key is not None:
                    estimate_cache[estimate_key] = estimate
            estimates.append(estimate)
            manifest_hash = (
                estimate.manifest_sha256_by_part[0]
                if len(estimate.manifest_sha256_by_part) == 1
                else ""
            )
            hashes[index] = manifest_hash
            worker_reserve = (
                estimate.worker_reserve_bytes_by_part[0]
                if len(estimate.worker_reserve_bytes_by_part) == 1
                else memory_budget
            )
            worker_reserves[index] = int(worker_reserve)
            if len(estimate.sample_backed_by_part) == 1:
                sample_backed[index] = bool(
                    estimate.sample_backed_by_part[0]
                )
            eligible.append(
                estimate.workers_safe
                and estimate.managed_worker_safe_by_part == (True,)
                and isinstance(manifest_hash, str)
                and len(manifest_hash) == 64
                # A worker that consumes more than half the existing audio
                # memory budget can never safely share a parallel window.
                # Treat it as a serial barrier so lightweight runs before
                # and after it retain their automatic speed-up.
                and worker_reserve <= memory_budget // 2
            )

        work_frames = derive_parallelism_work_frames(
            plan,
            sample_backed_by_part=tuple(sample_backed),
        )
        adaptive_work = (
            tuple(work_frames)
            if work_frames is not None and len(work_frames) == len(parts)
            else (0,) * len(parts)
        )
        backend_keys: list[str] = []
        for index, part in enumerate(parts):
            try:
                backend_key = make_adaptive_backend_key(
                    manifest_sha256=hashes[index],
                    engine_sha256=PROCESS_SOURCE_TREE_SHA256,
                    overrides_json=_project_canonical_json_bytes(
                        part.executor.override_map
                    ),
                    sample_backed=sample_backed[index],
                )
            except MemoryError:
                raise
            except Exception:
                backend_key = None
            backend_keys.append(backend_key or "")

        def admit_run(start: int, end: int) -> None:
            run_estimates = estimates[start:end]
            run_resources = SimpleNamespace(
                workers_safe=True,
                managed_worker_safe_by_part=(True,) * (end - start),
                worker_reserve_bytes_by_part=tuple(
                    estimate.worker_reserve_bytes_by_part[0]
                    for estimate in run_estimates
                ),
                sample_backed_by_part=tuple(
                    estimate.sample_backed_by_part[0]
                    for estimate in run_estimates
                ),
                manifest_sha256_by_part=tuple(hashes[start:end]),
            )
            run_plan = SimpleNamespace(
                duration_seconds=plan.duration_seconds,
                sample_rate=plan.sample_rate,
                parts=parts[start:end],
            )
            run_workloads: tuple[AdaptiveWorkload, ...] = ()
            if all(
                backend_keys[index] and adaptive_work[index] > 0
                for index in range(start, end)
            ):
                run_workloads = tuple(
                    AdaptiveWorkload(
                        backend_keys[index],
                        adaptive_work[index],
                    )
                    for index in range(start, end)
                )
            worker_kwargs: dict[str, Any] = {
                "scratch_directory": scratch_directory,
                "hall_tail_seconds": hall_tail_seconds,
                "_resources": run_resources,
            }
            if adaptive_session is not None and run_workloads:
                worker_kwargs.update(
                    adaptive_session=adaptive_session,
                    adaptive_workloads=run_workloads,
                )
            worker_count = _automatic_stem_worker_count(
                run_plan,
                **worker_kwargs,
            )
            if (
                isinstance(worker_count, bool)
                or not isinstance(worker_count, int)
            ):
                return
            worker_count = min(worker_count, end - start)
            if worker_count <= 1:
                return
            group_sizes = _balanced_worker_group_sizes(
                end - start,
                worker_count,
            )
            if len(group_sizes) == 1:
                counts[start:end] = [worker_count] * (end - start)
                return

            group_start = start
            for group_size in group_sizes:
                group_end = group_start + group_size
                if group_size > 1:
                    # Re-run the same zero-configuration policy for each
                    # balanced subgroup.  This prevents a short/sparse tail
                    # from paying process startup merely because the larger
                    # containing run was worthwhile.
                    admit_run(group_start, group_end)
                group_start = group_end

        cursor = 0
        while cursor < len(parts):
            if not eligible[cursor]:
                cursor += 1
                continue
            end = cursor + 1
            while end < len(parts) and eligible[end]:
                end += 1
            admit_run(cursor, end)
            cursor = end

        return _AutomaticStemParallelism(
            max(counts, default=1),
            tuple(counts),
            tuple(hashes),
            tuple(worker_reserves),
            tuple(sample_backed),
            tuple(backend_keys),
            tuple(adaptive_work),
        )
    except MemoryError:
        raise
    except Exception:
        part_count = len(getattr(plan, "parts", ()))
        return _AutomaticStemParallelism(
            1,
            (1,) * part_count,
            ("",) * part_count,
            adaptive_backend_key_by_part=("",) * part_count,
            adaptive_work_frames_by_part=(0,) * part_count,
        )


def _stem_worker_job(
    index: int,
    part: Any,
    sample_rate: int,
    expected_manifest_sha256: str,
) -> StemRenderJob:
    return StemRenderJob.create(
        index=index,
        executor_id=part.executor.executor_id,
        manifest_path=part.executor.capability.manifest_path,
        sample_rate=sample_rate,
        performance=part.performance,
        overrides=part.executor.override_map,
        expected_manifest_sha256=expected_manifest_sha256,
    )


def _iter_managed_stem_batch(
    jobs: tuple[StemRenderJob, ...],
    *,
    scratch_directory: Path,
    allow_warm_start: bool,
    slot_context: _ManagedWorkerSlotContext,
    warm_binding: _ManagedWarmBinding | None = None,
    adaptive_session: AdaptiveRenderSession | None = None,
    adaptive_backend_key_by_part: tuple[str, ...] = (),
    adaptive_work_frames_by_part: tuple[int, ...] = (),
    disable_warm_for_run: Callable[[], None] | None = None,
) -> Any:
    """Collect one bounded batch, then yield its sources in job order."""

    handles: list[Any | None] = []
    adaptive_observations: list[Any | None] = []
    reservation: Any | None = None
    detached_results: list[
        tuple[int, StemBlockSource, int, str] | None
    ] = []
    completed_normally = False

    def close_failed_slot(slot: Any | None) -> None:
        if slot is None:
            return
        try:
            slot.close()
        except BaseException as exc:
            _warn_cleanup(
                "managed worker slot cleanup did not complete: "
                f"{type(exc).__name__}"
            )

    def resource_claims(
        binding: _ManagedWarmBinding | None,
    ) -> tuple[WorkerResourceClaim, ...]:
        parent_stem_bytes = max(
            job.frame_count * 2 * 4 for job in jobs
        )
        coordinator_memory_bytes = (
            binding.coordinator_memory_bytes
            if binding is not None
            else slot_context.coordinator_memory_bytes
            + parent_stem_bytes
        )
        return tuple(
            WorkerResourceClaim(
                owner_id=slot_context.owner_id,
                owner_cpu_capacity=slot_context.owner_cpu_capacity,
                worker_memory_bytes=(
                    binding.worker_memory_ceiling_bytes
                    if binding is not None
                    else slot_context.worker_memory_bytes_by_part[job.index]
                ),
                coordinator_memory_bytes=coordinator_memory_bytes,
                memory_budget_bytes=slot_context.memory_budget_bytes,
                scratch_bytes=(
                    binding.scratch_ceiling_bytes
                    if binding is not None
                    else job.frame_count * 2 * 4
                ),
                scratch_directory=slot_context.scratch_directory,
            )
            for job in jobs
        )

    def begin_adaptive_batch() -> None:
        """Open every timing before this batch can start its first child."""

        adaptive_observations.clear()
        # Allocate the bounded ownership table before creating any advisor
        # token.  Once begin_managed succeeds, storing the token cannot need a
        # later list growth whose MemoryError would orphan that live timing.
        adaptive_observations.extend([None] * len(jobs))
        for position, job in enumerate(jobs):
            observation = None
            if (
                adaptive_session is not None
                and 0 <= job.index
                < len(adaptive_backend_key_by_part)
                and job.index < len(adaptive_work_frames_by_part)
                and adaptive_backend_key_by_part[job.index]
                and adaptive_work_frames_by_part[job.index] > 0
            ):
                observation = adaptive_session.begin_managed(
                    backend_key=adaptive_backend_key_by_part[job.index],
                    work_frames=adaptive_work_frames_by_part[job.index],
                )
            adaptive_observations[position] = observation

    def abandon_warm_attempt(
        binding: _ManagedWarmBinding,
    ) -> None:
        """Retire a partial warm batch before exact one-shot retry."""

        nonlocal reservation

        first_error: BaseException | None = None
        if adaptive_session is not None:
            for position, observation in enumerate(adaptive_observations):
                if observation is None:
                    continue
                try:
                    adaptive_session.discard_managed(observation)
                except BaseException as exc:
                    _warn_cleanup(
                        "adaptive warm retry cleanup did not complete: "
                        f"{type(exc).__name__}"
                    )
                adaptive_observations[position] = None
        for position, handle in enumerate(handles):
            if handle is None:
                continue
            try:
                terminate_stem_worker(handle)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                handles[position] = None
        if reservation is not None:
            try:
                reservation.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                reservation = None
        try:
            # This also removes idle binding siblings which were not checked
            # out before the missing/stale child made the batch incomplete.
            _retire_managed_stem_worker_session(
                binding.owner_id,
                force=True,
            )
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error
        handles.clear()
        adaptive_observations.clear()

    try:
        try:
            if warm_binding is None or allow_warm_start:
                # First remove unrelated legacy/session RSS.  A later batch
                # in this exact binding deliberately keeps its admitted idle
                # children for reuse.
                retire_idle_stem_workers()
            claims = resource_claims(warm_binding)
            if warm_binding is None or allow_warm_start:
                reservation = slot_context.pool.reserve_exact(claims)
            if (
                reservation is None
                and warm_binding is not None
                and allow_warm_start
            ):
                # A whole-run ceiling can be deliberately more conservative
                # than either exact batch.  If that persistent reservation
                # does not fit, retain safe batch parallelism: permanently
                # disable warmth for this run and retry this untouched batch
                # with its exact one-shot claims.
                if disable_warm_for_run is not None:
                    disable_warm_for_run()
                warm_binding = None
                allow_warm_start = False
                claims = resource_claims(None)
                reservation = slot_context.pool.reserve_exact(claims)
        except MemoryError:
            raise
        except Exception as exc:
            raise _ManagedStemBatchFailure(0, exc) from exc
        if (warm_binding is None or allow_warm_start) and reservation is None:
            raise _ManagedStemBatchFailure(
                0,
                StemWorkerError(
                    "global managed stem worker capacity is unavailable"
                ),
            )

        while True:
            start_failure: Exception | None = None
            try:
                # A managed route describes one admitted batch, not a series
                # of independently timed child launches.  Begin every member
                # before the first warm checkout, Popen or slot hand-off so
                # sequential startup cannot shift later routes' time origin.
                begin_adaptive_batch()
            except MemoryError:
                raise
            except Exception as exc:
                start_failure = exc
            for job in jobs:
                if start_failure is not None:
                    break
                slot: Any | None = None
                try:
                    if reservation is not None:
                        slot = reservation.take()
                    handle = _try_start_stem_worker(
                        job,
                        scratch_directory=scratch_directory,
                        allow_warm_start=(
                            warm_binding is not None and allow_warm_start
                        ),
                        allow_warm_reuse=warm_binding is not None,
                        reserved_slot=slot,
                        managed_warm_binding=warm_binding,
                        managed_worker_memory_bytes=(
                            None
                            if warm_binding is None
                            else slot_context.worker_memory_bytes_by_part[
                                job.index
                            ]
                        ),
                    )
                except MemoryError:
                    close_failed_slot(slot)
                    raise
                except Exception as exc:
                    close_failed_slot(slot)
                    start_failure = exc
                    break
                if handle is None:
                    close_failed_slot(slot)
                    start_failure = StemWorkerError(
                        "managed stem worker capacity is unavailable"
                    )
                    break
                handles.append(handle)

            if start_failure is None:
                break
            if warm_binding is None:
                raise _ManagedStemBatchFailure(0, start_failure) from (
                    start_failure
                )

            failed_binding = warm_binding
            try:
                if disable_warm_for_run is not None:
                    disable_warm_for_run()
                abandon_warm_attempt(failed_binding)
                warm_binding = None
                allow_warm_start = False
                reservation = slot_context.pool.reserve_exact(
                    resource_claims(None)
                )
            except MemoryError:
                raise
            except Exception as exc:
                raise _ManagedStemBatchFailure(0, exc) from exc
            if reservation is None:
                raise _ManagedStemBatchFailure(
                    0,
                    StemWorkerError(
                        "global managed stem worker capacity is unavailable"
                    ),
                )

        # One child cannot amortise process startup.  This also ensures that a
        # competing render which obtains the remaining global permit keeps the
        # whole batch on the serial path instead of slowing both renders down.
        if len(handles) < 2:
            raise _ManagedStemBatchFailure(
                0,
                StemWorkerError("managed stem batch is too small"),
            )

        # Collect every child before exposing the first source.  Apart from
        # keeping this list bounded by the admitted worker count (currently
        # at most four), this keeps downstream cache/mix/consumption outside
        # every child timing and gives an all-or-none fallback boundary.  A
        # later position conservatively includes earlier ordered protocol,
        # SHA and bounded validation time.  That can only overestimate its
        # managed cost; retaining the sample also lets heterogeneous backend
        # plans eventually build complete per-backend evidence.
        for position, handle in enumerate(handles):
            assert handle is not None
            result: Any | None = None
            transferred_source: StemBlockSource | None = None
            try:
                result = collect_stem_worker(handle)
                handles[position] = None
                try:
                    frozen_observation = None
                    if adaptive_session is not None:
                        frozen_observation = (
                            adaptive_session.freeze_managed(
                                adaptive_observations[position],
                                warm_used=bool(result._warm_used),
                                concurrent_workers=len(handles),
                            )
                        )
                    adaptive_observations[position] = None
                    if frozen_observation is None:
                        transferred_source = result.detach_source()
                    else:
                        transferred_source = result.detach_source(
                            completion_callback=frozen_observation.resolve
                        )
                    detached_results.append(
                        (
                            result.index,
                            transferred_source,
                            result.peak_voices,
                            result.manifest_sha256,
                        )
                    )
                except BaseException:
                    try:
                        if transferred_source is not None:
                            transferred_source.close()
                        else:
                            result.close()
                    except BaseException as exc:
                        _warn_cleanup(
                            "managed stem result cleanup did not complete: "
                            f"{type(exc).__name__}"
                        )
                    raise
            except MemoryError:
                # Resource exhaustion is not a recoverable worker-protocol
                # failure.  Continuing with the in-process renderer would
                # usually require at least as much memory and can hide the
                # host's cancellation/pressure signal.
                raise
            except Exception as exc:
                raise _ManagedStemBatchFailure(0, exc) from exc

        for position, detached in enumerate(detached_results):
            assert detached is not None
            transferred_source = detached[1]
            resumed_normally = False
            source_was_open = False
            close_error: BaseException | None = None
            try:
                yield detached
                resumed_normally = True
            finally:
                if not transferred_source.closed:
                    source_was_open = True
                    try:
                        transferred_source.close()
                    except BaseException as exc:
                        close_error = exc
                detached_results[position] = None
                if not resumed_normally and close_error is not None:
                    _warn_cleanup(
                        "managed stem source cleanup did not complete: "
                        f"{type(close_error).__name__}"
                    )
            if resumed_normally and source_was_open:
                cause = close_error or StemWorkerError(
                    "managed stem source was not explicitly consumed and closed"
                )
                raise _ManagedStemBatchFailure(position + 1, cause) from cause
        completed_normally = True
    finally:
        cleanup_error: BaseException | None = None
        for detached in detached_results:
            if detached is None:
                continue
            transferred_source = detached[1]
            if transferred_source.closed:
                continue
            try:
                transferred_source.close()
            except BaseException as exc:
                if completed_normally and cleanup_error is None:
                    cleanup_error = exc
                else:
                    _warn_cleanup(
                        "managed stem source cleanup did not complete: "
                        f"{type(exc).__name__}"
                    )
        if reservation is not None:
            try:
                reservation.close()
            except BaseException as exc:
                if completed_normally and cleanup_error is None:
                    cleanup_error = exc
                else:
                    _warn_cleanup(
                        "managed stem reservation cleanup did not complete: "
                        f"{type(exc).__name__}"
                    )
        if adaptive_session is not None:
            for observation in adaptive_observations:
                if observation is None:
                    continue
                try:
                    adaptive_session.discard_managed(observation)
                except BaseException as exc:
                    if completed_normally and cleanup_error is None:
                        cleanup_error = exc
                    else:
                        _warn_cleanup(
                            "adaptive managed timing cleanup did not "
                            "complete: "
                            f"{type(exc).__name__}"
                        )
        for handle in handles:
            if handle is not None:
                try:
                    terminate_stem_worker(handle)
                except BaseException as exc:
                    if completed_normally and cleanup_error is None:
                        cleanup_error = exc
                    else:
                        _warn_cleanup(
                            "managed stem worker cleanup did not complete: "
                            f"{type(exc).__name__}"
                        )
        if completed_normally and cleanup_error is not None:
            raise cleanup_error


def _render_part_adaptively(
    part: Any,
    sample_rate: int,
    *,
    adaptive_session: AdaptiveRenderSession | None = None,
    adaptive_backend_key: str = "",
    adaptive_work_frames: int = 0,
    stream_output: bool = False,
    scratch_directory: Path | None = None,
) -> tuple[Any, int, str]:
    """Time only one real uncached backend render, never its consumers."""

    observation = None
    if (
        adaptive_session is not None
        and adaptive_backend_key
        and type(adaptive_work_frames) is int
        and adaptive_work_frames > 0
    ):
        observation = adaptive_session.begin_serial(
            backend_key=adaptive_backend_key,
            work_frames=adaptive_work_frames,
        )
    if stream_output:
        if scratch_directory is None:
            raise ValueError("streamed serial render requires scratch")
        rendered = _render_part_source(
            part,
            sample_rate,
            scratch_directory=scratch_directory,
        )
    else:
        rendered = _render_part(part, sample_rate)
    if adaptive_session is not None and observation is not None:
        try:
            frozen = adaptive_session.freeze_serial(observation)
            if frozen is not None:
                # The enclosing render-scoped transaction still withholds
                # this timing until downstream validation consumes the stem
                # and the complete raw-stem phase succeeds.
                frozen.resolve(True)
        except BaseException:
            if isinstance(rendered[0], StemBlockSource):
                try:
                    rendered[0].close()
                except BaseException:
                    pass
            raise
    return rendered


def _serial_raw_stem(
    part: Any,
    sample_rate: int,
    *,
    cache: StemCache | None,
    snapshot_directory: Path,
    stream_cache_hits: bool = True,
    refresh: bool,
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ],
    summary: dict[str, Any] | None,
    adaptive_session: AdaptiveRenderSession | None = None,
    adaptive_backend_key: str = "",
    adaptive_work_frames: int = 0,
    direct_cache_fallback: bool = False,
) -> tuple[Any, int, str]:
    # A worker_count<=1 part is a separate serial resource phase even when its
    # raw stem is already cached.  Reap the preceding parallel run's idle RSS
    # before cache.load or an in-process fallback crosses that budget barrier.
    # Prepared cache hits inside one admitted parallel run bypass this helper
    # and intentionally keep warm reuse alive.
    retire_idle_stem_workers()
    stream_output = stream_cache_hits and _prefer_streamed_serial_stem(
        part,
        sample_rate,
        snapshot_directory,
    )
    if cache is None or summary is None:
        return _render_part_adaptively(
            part,
            sample_rate,
            adaptive_session=adaptive_session,
            adaptive_backend_key=adaptive_backend_key,
            adaptive_work_frames=adaptive_work_frames,
            stream_output=stream_output,
            scratch_directory=snapshot_directory,
        )
    return _render_part_cached(
        part,
        sample_rate,
        cache=cache,
        snapshot_directory=snapshot_directory,
        stream_cache_hits=stream_cache_hits,
        refresh=refresh,
        runtime_fingerprints=runtime_fingerprints,
        summary=summary,
        adaptive_session=adaptive_session,
        adaptive_backend_key=adaptive_backend_key,
        adaptive_work_frames=adaptive_work_frames,
        stream_output=stream_output,
        direct_cache_fallback=direct_cache_fallback,
    )


def _wrap_streamed_cache_store(
    part: Any,
    sample_rate: int,
    *,
    source: StemBlockSource,
    cache: StemCache,
    summary: dict[str, Any],
    identity: _RawStemCacheIdentity,
    peak_voices: int,
    manifest_sha256: str,
) -> StemBlockSource:
    """Tee one authoritative streamed source into a cache transaction."""

    try:
        transaction = cache.begin_streaming_store(
            identity.key,
            stage=RAW_STEM_CACHE_STAGE,
            sample_rate=sample_rate,
            peak_voices=peak_voices,
            manifest_sha256=manifest_sha256,
        )
    except BaseException:
        try:
            source.close()
        except BaseException:
            pass
        raise

    def finish_cache() -> None:
        if not _cache_publication_is_live(
            part,
            sample_rate,
            summary=summary,
            identity=identity,
            manifest_sha256=manifest_sha256,
        ):
            transaction.abort()
            return
        stored = transaction.finish(
            source.frame_count,
            source.audio_sha256,
        )
        _note_cache_store_result(summary, stored)

    try:
        return _StreamedRawStem(
            source,
            transaction=transaction,
            finish_cache=finish_cache,
        )
    except BaseException:
        try:
            transaction.abort()
        except BaseException:
            pass
        try:
            source.close()
        except BaseException:
            pass
        raise


def _consume_prepared_parallel_render(
    part: Any,
    sample_rate: int,
    prepared: _PreparedParallelStemCache,
    rendered: tuple[Any, int, str],
    *,
    cache: StemCache,
    summary: dict[str, Any],
) -> tuple[Any, int, str]:
    buffer, peak_voices, manifest_sha256 = rendered
    cache_eligible = _activate_prepared_cache_accounting(
        prepared,
        summary,
    )
    if cache_eligible:
        assert prepared.identity is not None
        if isinstance(buffer, StemBlockSource):
            buffer = _wrap_streamed_cache_store(
                part,
                sample_rate,
                source=buffer,
                cache=cache,
                summary=summary,
                identity=prepared.identity,
                peak_voices=peak_voices,
                manifest_sha256=manifest_sha256,
            )
        else:
            _store_parallel_rendered_stem(
                part,
                sample_rate,
                cache=cache,
                summary=summary,
                identity=prepared.identity,
                buffer=buffer,
                peak_voices=peak_voices,
                manifest_sha256=manifest_sha256,
            )
    return buffer, peak_voices, manifest_sha256


def _render_prepared_stem_serially(
    part: Any,
    sample_rate: int,
    prepared: _PreparedParallelStemCache,
    *,
    cache: StemCache,
    summary: dict[str, Any],
    adaptive_session: AdaptiveRenderSession | None = None,
    adaptive_backend_key: str = "",
    adaptive_work_frames: int = 0,
    stream_output: bool = False,
    scratch_directory: Path | None = None,
) -> tuple[Any, int, str]:
    rendered = _render_part_adaptively(
        part,
        sample_rate,
        adaptive_session=adaptive_session,
        adaptive_backend_key=adaptive_backend_key,
        adaptive_work_frames=adaptive_work_frames,
        stream_output=stream_output,
        scratch_directory=scratch_directory,
    )
    return _consume_prepared_parallel_render(
        part,
        sample_rate,
        prepared,
        rendered,
        cache=cache,
        summary=summary,
    )


def _consume_prepared_cache_hit(
    part: Any,
    sample_rate: int,
    prepared: _PreparedParallelStemCache,
    *,
    summary: dict[str, Any],
    adaptive_session: AdaptiveRenderSession | None = None,
    adaptive_backend_key: str = "",
    adaptive_work_frames: int = 0,
    stream_output: bool = False,
    scratch_directory: Path | None = None,
) -> tuple[Any, int, str]:
    """Use a look-ahead hit only while the ordered cache session is live."""

    lookup = prepared.lookup
    assert lookup is not None
    assert prepared.identity is not None
    if summary["active"] and current_source_tree_matches():
        _activate_prepared_cache_accounting(prepared, summary)
        assert lookup.record is not None
        return (
            (
                lookup.source
                if lookup.source is not None
                else lookup.audio
            ),
            int(lookup.record.metadata["peak_voices"]),
            prepared.identity.manifest_sha256,
        )

    _close_cache_lookup(lookup)
    if summary["active"]:
        summary["active"] = False
        _note_cache_result(
            summary,
            "bypassed",
            "producer_source_changed_restart_required",
        )
    else:
        _note_cache_result(summary, "bypassed", "session_disabled")
    return _render_part_adaptively(
        part,
        sample_rate,
        adaptive_session=adaptive_session,
        adaptive_backend_key=adaptive_backend_key,
        adaptive_work_frames=adaptive_work_frames,
        stream_output=stream_output,
        scratch_directory=scratch_directory,
    )


def _iter_raw_stems_in_plan_order_body(
    plan: PerformancePlan,
    *,
    scratch_directory: Path,
    hall_tail_seconds: float,
    cache: StemCache | None,
    stream_cache_hits: bool,
    direct_cache_fallback: bool,
    refresh: bool,
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ],
    summary: dict[str, Any] | None,
    slot_context_holder: list[_ManagedWorkerSlotContext],
    adaptive_session: AdaptiveRenderSession,
) -> Any:
    """Yield raw stems in plan order with transparent bounded parallelism."""

    parallelism = _automatic_stem_parallelism(
        plan,
        scratch_directory=scratch_directory,
        hall_tail_seconds=hall_tail_seconds,
        adaptive_session=adaptive_session,
    )
    worker_count_by_part = parallelism.worker_count_by_part
    manifest_sha256_by_part = parallelism.manifest_sha256_by_part
    adaptive_backend_key_by_part = tuple(
        getattr(parallelism, "adaptive_backend_key_by_part", ())
    )
    adaptive_work_frames_by_part = tuple(
        getattr(parallelism, "adaptive_work_frames_by_part", ())
    )
    parallel_available = parallelism.worker_count > 1
    slot_context = (
        _automatic_worker_slot_context(
            plan,
            parallelism=parallelism,
            scratch_directory=scratch_directory,
            hall_tail_seconds=hall_tail_seconds,
        )
        if parallel_available
        else None
    )
    if slot_context is None:
        parallel_available = False
    else:
        slot_context_holder.append(slot_context)
    parts = plan.parts
    index = 0
    previous_worker_count: int | None = None
    warm_binding: _ManagedWarmBinding | None = None
    warm_disabled_for_run = False

    def adaptive_fact(part_index: int) -> tuple[str, int]:
        if (
            0 <= part_index < len(adaptive_backend_key_by_part)
            and part_index < len(adaptive_work_frames_by_part)
        ):
            return (
                adaptive_backend_key_by_part[part_index],
                adaptive_work_frames_by_part[part_index],
            )
        return "", 0

    def disable_warm_for_current_run() -> None:
        nonlocal warm_binding
        nonlocal warm_disabled_for_run

        warm_binding = None
        warm_disabled_for_run = True

    while index < len(parts):
        # A source edit after policy selection means a freshly imported child
        # could execute different code from this coordinator.  Keep rendering
        # functional by switching the rest of this generation to serial.
        if parallel_available and not current_source_tree_matches():
            parallel_available = False

        worker_count = (
            worker_count_by_part[index]
            if parallel_available
            and index < len(worker_count_by_part)
            else 1
        )
        if (
            previous_worker_count is not None
            and worker_count != previous_worker_count
        ):
            # Each count is a separately admitted memory-reserve run.  Never
            # carry the larger run's idle RSS into the next run merely because
            # both happen to use managed workers.
            retire_idle_stem_workers()
            warm_binding = None
            warm_disabled_for_run = False
        previous_worker_count = worker_count
        if worker_count <= 1:
            part = parts[index]
            adaptive_backend_key, adaptive_work_frames = adaptive_fact(index)
            rendered = _serial_raw_stem(
                part,
                plan.sample_rate,
                cache=cache,
                snapshot_directory=scratch_directory,
                stream_cache_hits=stream_cache_hits,
                refresh=refresh,
                runtime_fingerprints=runtime_fingerprints,
                summary=summary,
                adaptive_session=adaptive_session,
                adaptive_backend_key=adaptive_backend_key,
                adaptive_work_frames=adaptive_work_frames,
                direct_cache_fallback=direct_cache_fallback,
            )
            yield (index, part, *rendered)
            rendered = None
            index += 1
            continue

        if cache is None or summary is None:
            run_end = index
            while (
                run_end < len(parts)
                and worker_count_by_part[run_end] == worker_count
            ):
                run_end += 1
            group_end = index
            while (
                group_end < len(parts)
                and group_end < index + worker_count
                and worker_count_by_part[group_end] == worker_count
            ):
                group_end += 1
            group = tuple(
                (part_index, parts[part_index])
                for part_index in range(index, group_end)
            )
            if len(group) < 2:
                part = parts[index]
                adaptive_backend_key, adaptive_work_frames = adaptive_fact(
                    index
                )
                rendered = _serial_raw_stem(
                    part,
                    plan.sample_rate,
                    cache=cache,
                    snapshot_directory=scratch_directory,
                    stream_cache_hits=stream_cache_hits,
                    refresh=refresh,
                    runtime_fingerprints=runtime_fingerprints,
                    summary=summary,
                    adaptive_session=adaptive_session,
                    adaptive_backend_key=adaptive_backend_key,
                    adaptive_work_frames=adaptive_work_frames,
                    direct_cache_fallback=direct_cache_fallback,
                )
                yield (index, part, *rendered)
                rendered = None
                index += 1
                continue
            try:
                jobs = tuple(
                    _stem_worker_job(
                        part_index,
                        part,
                        plan.sample_rate,
                        manifest_sha256_by_part[part_index],
                    )
                    for part_index, part in group
                )
            except MemoryError:
                raise
            except Exception:
                parallel_available = False
                continue
            try:
                allow_warm_start = False
                if (
                    group_end < run_end
                    and warm_binding is None
                    and not warm_disabled_for_run
                ):
                    candidate_binding = _managed_warm_binding_for_run(
                        start=index,
                        end=run_end,
                        parts=parts,
                        slot_context=slot_context,
                    )
                    if candidate_binding is not None:
                        warm_binding = candidate_binding
                        allow_warm_start = True
                position = 0
                for worker_result in _iter_managed_stem_batch(
                    jobs,
                    scratch_directory=scratch_directory,
                    # Only create persistent children when this contiguous
                    # parallel run has another batch that can reuse them.
                    # A final batch still checks out existing idle workers.
                    allow_warm_start=allow_warm_start,
                    slot_context=slot_context,
                    warm_binding=warm_binding,
                    adaptive_session=adaptive_session,
                    adaptive_backend_key_by_part=(
                        adaptive_backend_key_by_part
                    ),
                    adaptive_work_frames_by_part=(
                        adaptive_work_frames_by_part
                    ),
                    disable_warm_for_run=disable_warm_for_current_run,
                ):
                    part_index, buffer, peak_voices, manifest_sha256 = (
                        worker_result
                    )
                    expected_index, part = group[position]
                    if part_index != expected_index:
                        raise _ManagedStemBatchFailure(
                            position,
                            StemWorkerError(
                                "managed stem results changed plan order"
                            ),
                        )
                    yield (
                        part_index,
                        part,
                        buffer,
                        peak_voices,
                        manifest_sha256,
                    )
                    worker_result = None
                    buffer = None
                    position += 1
                index += len(group)
            except _ManagedStemBatchFailure as failure:
                index += failure.position
                parallel_available = False
            continue

        prepared_group: list[
            tuple[int, Any, _PreparedParallelStemCache]
        ] = []
        cached_head: tuple[
            int, Any, _PreparedParallelStemCache
        ] | None = None
        cursor = index
        source_became_unsafe = False
        while (
            cursor < len(parts)
            and len(prepared_group) < worker_count
            and worker_count_by_part[cursor] == worker_count
        ):
            part = parts[cursor]
            prepared = _prepare_parallel_stem_cache(
                part,
                plan.sample_rate,
                cache=cache,
                snapshot_directory=scratch_directory,
                stream_cache_hits=stream_cache_hits,
                direct_cache_fallback=direct_cache_fallback,
                refresh=refresh,
                runtime_fingerprints=runtime_fingerprints,
                summary=summary,
            )
            if prepared.disable_session:
                source_became_unsafe = True
                break
            if prepared.lookup is not None:
                if not prepared_group:
                    cached_head = (cursor, part, prepared)
                else:
                    # This boundary hit is reloaded only after the preceding
                    # misses are consumed.  Close its verified descriptor
                    # before any worker result enters coordinator memory.
                    _close_cache_lookup(prepared.lookup)
                    prepared = None
                # The next ordered iteration reopens and verifies this
                # boundary hit after the batch has been consumed.
                break
            prepared_group.append((cursor, part, prepared))
            cursor += 1

        if source_became_unsafe:
            parallel_available = False
            continue
        if cached_head is not None:
            part_index, part, prepared = cached_head
            stream_output = (
                stream_cache_hits
                and _prefer_streamed_serial_stem(
                    part,
                    plan.sample_rate,
                    scratch_directory,
                )
            )
            rendered = _consume_prepared_cache_hit(
                part,
                plan.sample_rate,
                prepared,
                summary=summary,
                adaptive_session=adaptive_session,
                adaptive_backend_key=adaptive_fact(part_index)[0],
                adaptive_work_frames=adaptive_fact(part_index)[1],
                stream_output=stream_output,
                scratch_directory=scratch_directory,
            )
            yield (part_index, part, *rendered)
            rendered = None
            prepared = None
            cached_head = None
            index += 1
            continue
        if len(prepared_group) < 2:
            if not prepared_group:
                part = parts[index]
                adaptive_backend_key, adaptive_work_frames = adaptive_fact(
                    index
                )
                rendered = _serial_raw_stem(
                    part,
                    plan.sample_rate,
                    cache=cache,
                    snapshot_directory=scratch_directory,
                    stream_cache_hits=stream_cache_hits,
                    refresh=refresh,
                    runtime_fingerprints=runtime_fingerprints,
                    summary=summary,
                    adaptive_session=adaptive_session,
                    adaptive_backend_key=adaptive_backend_key,
                    adaptive_work_frames=adaptive_work_frames,
                    direct_cache_fallback=direct_cache_fallback,
                )
                yield (index, part, *rendered)
                rendered = None
                index += 1
                continue
            part_index, part, prepared = prepared_group[0]
            stream_output = (
                stream_cache_hits
                and _prefer_streamed_serial_stem(
                    part,
                    plan.sample_rate,
                    scratch_directory,
                )
            )
            rendered = _render_prepared_stem_serially(
                part,
                plan.sample_rate,
                prepared,
                cache=cache,
                summary=summary,
                adaptive_session=adaptive_session,
                adaptive_backend_key=adaptive_fact(part_index)[0],
                adaptive_work_frames=adaptive_fact(part_index)[1],
                stream_output=stream_output,
                scratch_directory=scratch_directory,
            )
            yield (part_index, part, *rendered)
            rendered = None
            index += 1
            continue

        try:
            jobs = tuple(
                _stem_worker_job(
                    part_index,
                    part,
                    plan.sample_rate,
                    manifest_sha256_by_part[part_index],
                )
                for part_index, part, _prepared in prepared_group
            )
        except MemoryError:
            raise
        except Exception:
            parallel_available = False
            continue
        try:
            allow_warm_start = False
            if refresh:
                run_end = index
                while (
                    run_end < len(parts)
                    and worker_count_by_part[run_end] == worker_count
                ):
                    run_end += 1
                if (
                    cursor < run_end
                    and warm_binding is None
                    and not warm_disabled_for_run
                ):
                    candidate_binding = _managed_warm_binding_for_run(
                        start=index,
                        end=run_end,
                        parts=parts,
                        slot_context=slot_context,
                    )
                    if candidate_binding is not None:
                        warm_binding = candidate_binding
                        allow_warm_start = True
            position = 0
            for worker_result in _iter_managed_stem_batch(
                jobs,
                scratch_directory=scratch_directory,
                # Cache look-ahead deliberately retains no full future hit or
                # miss payload, so ordinary lookup cannot prove a later miss
                # batch without reintroducing coordinator memory pressure.
                # Refresh mode is the exception: every later prepared entry is
                # a known miss, so it can safely use the no-cache run policy.
                allow_warm_start=allow_warm_start,
                slot_context=slot_context,
                warm_binding=warm_binding,
                adaptive_session=adaptive_session,
                adaptive_backend_key_by_part=(
                    adaptive_backend_key_by_part
                ),
                adaptive_work_frames_by_part=(
                    adaptive_work_frames_by_part
                ),
                disable_warm_for_run=disable_warm_for_current_run,
            ):
                part_index, buffer, peak_voices, manifest_sha256 = (
                    worker_result
                )
                expected_index, part, prepared = prepared_group[position]
                if part_index != expected_index:
                    raise _ManagedStemBatchFailure(
                        position,
                        StemWorkerError(
                            "managed stem results changed plan order"
                        ),
                    )
                rendered = _consume_prepared_parallel_render(
                    part,
                    plan.sample_rate,
                    prepared,
                    (buffer, peak_voices, manifest_sha256),
                    cache=cache,
                    summary=summary,
                )
                yield (part_index, part, *rendered)
                worker_result = None
                rendered = None
                buffer = None
                position += 1
            index += len(prepared_group)
        except _ManagedStemBatchFailure as failure:
            index += failure.position
            parallel_available = False


def _iter_raw_stems_in_plan_order(
    plan: PerformancePlan,
    *,
    scratch_directory: Path,
    hall_tail_seconds: float,
    cache: StemCache | None,
    stream_cache_hits: bool,
    refresh: bool,
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ],
    summary: dict[str, Any] | None,
    direct_cache_fallback: bool = False,
) -> Any:
    """Own and deterministically retire one render session's warm workers."""

    slot_context_holder: list[_ManagedWorkerSlotContext] = []
    adaptive_session = AdaptiveRenderSession()
    try:
        yield from _iter_raw_stems_in_plan_order_body(
            plan,
            scratch_directory=scratch_directory,
            hall_tail_seconds=hall_tail_seconds,
            cache=cache,
            stream_cache_hits=stream_cache_hits,
            direct_cache_fallback=direct_cache_fallback,
            refresh=refresh,
            runtime_fingerprints=runtime_fingerprints,
            summary=summary,
            slot_context_holder=slot_context_holder,
            adaptive_session=adaptive_session,
        )
    except BaseException:
        if slot_context_holder:
            try:
                _retire_managed_stem_worker_session(
                    slot_context_holder[0].owner_id,
                    force=True,
                )
            except BaseException as exc:
                _warn_cleanup(
                    "managed stem session cleanup did not complete: "
                    f"{type(exc).__name__}"
                )
        try:
            adaptive_session.cancel()
        except BaseException as exc:
            _warn_cleanup(
                "adaptive render timing cleanup did not complete: "
                f"{type(exc).__name__}"
            )
        raise
    else:
        try:
            if slot_context_holder:
                _retire_managed_stem_worker_session(
                    slot_context_holder[0].owner_id,
                    force=False,
                )
            adaptive_session.complete()
        except BaseException:
            try:
                adaptive_session.cancel()
            except BaseException as exc:
                _warn_cleanup(
                    "adaptive render timing cleanup did not complete: "
                    f"{type(exc).__name__}"
                )
            raise


def _absolute_peak(samples: Any) -> float:
    """Return an ndarray peak without allocating a full-size ``abs`` copy."""

    import numpy as np

    if samples.size == 0:
        return 0.0
    minimum = float(np.min(samples))
    maximum = float(np.max(samples))
    return max(abs(minimum), abs(maximum))


def _validate_stem_peak(executor: Any, stem_peak: float) -> None:
    """Apply one user-visible finite/overload contract to every stem path."""

    if not math.isfinite(stem_peak):
        raise ValueError(
            f"分轨 {executor.executor_id!r} 产生了非有限样本"
        )
    if stem_peak > 1.0:
        headroom = 20.0 * math.log10(stem_peak)
        raise ValueError(
            f"分轨 {executor.executor_id!r} "
            f"过载:峰值 {stem_peak:.4f}"
            f"(超出 {headroom:+.2f} dB)。"
            "写盘会被静默削平,因此拒绝输出。"
            f"请把该声部的 gain_db 从 "
            f"{executor.gain_db:.1f} 降到 "
            f"{executor.gain_db - headroom:.1f} 或更低"
        )


def _accumulate_stem(
    bus: Any,
    send_bus: Any | None,
    buffer: Any,
    length: int,
    left_gain: float,
    right_gain: float,
    send_scale: float | None,
) -> None:
    """Mix one stem with bounded multiplication scratch arrays."""

    chunk_frames = 65_536
    for start in range(0, length, chunk_frames):
        end = min(length, start + chunk_frames)
        bus[start:end, 0] += buffer[start:end, 0] * left_gain
        bus[start:end, 1] += buffer[start:end, 1] * right_gain
        if send_bus is not None:
            assert send_scale is not None
            send_bus[start:end] += buffer[start:end] * send_scale


def _try_begin_streamed_analysis_transaction(
    builder: CollaborationReportBuilder,
    executor: Any,
    source: StemBlockSource,
    *,
    staging_identity: PlainDirectoryIdentity,
    write_stems: bool,
) -> Any | None:
    """Choose the long-stem transaction before consuming any source byte."""

    raw_bytes = source.frame_count * 2 * 4
    if raw_bytes <= _DIRECT_ANALYSIS_STEM_LOAD_BYTES:
        return None
    staging = revalidate_plain_directory(staging_identity)
    required = _analysis_transaction_scratch_requirement(
        source.frame_count,
        write_stems=write_stems,
    )
    try:
        free_bytes = shutil.disk_usage(staging).free
    except MemoryError:
        raise
    except Exception:
        # Probe failure is a performance-path failure only, but an identity
        # failure is a safety boundary and must remain fail-closed.
        revalidate_plain_directory(staging_identity)
        return None

    # Revalidate even on an insufficient-space result: otherwise a directory
    # replacement racing the probe could be hidden by the normal fallback.
    revalidate_plain_directory(staging_identity)
    if free_bytes < required:
        return None
    try:
        return builder._begin_stem_transaction(
            executor,
            frame_count=source.frame_count,
        )
    except MemoryError:
        raise
    except OSError as exc:
        if exc.errno in {
            errno.ELOOP,
            errno.ENOENT,
            errno.ENOTDIR,
            errno.ESTALE,
        }:
            raise
        revalidate_plain_directory(staging_identity)
        return None
    except Exception:
        # The source has not been iterated yet.  A non-identity mapping failure
        # can therefore retain the established materialisation path without
        # rerendering or weakening source validation.
        revalidate_plain_directory(staging_identity)
        return None


def _consume_streamed_analysis_stem(
    source: StemBlockSource,
    transaction: Any,
    *,
    sample_rate: int,
    executor: Any,
    gain_envelope: Any,
) -> tuple[Any, float]:
    """Validate a raw source, then diagnose its post-gain scratch mapping."""

    import numpy as np

    frame_offset = 0
    stem_peak = 0.0
    post_gain_nonfinite = False
    compiled_gain_envelope: tuple[Any, Any] | None = None
    raw_blocks: Any | None = None
    try:
        raw_blocks = source.iter_blocks(65_536)
        for raw_block in raw_blocks:
            block = np.array(
                raw_block,
                dtype=np.float32,
                order="C",
                copy=True,
            )
            if gain_envelope and compiled_gain_envelope is None:
                compiled_gain_envelope = _compile_gain_envelope_points(
                    gain_envelope
                )
            _apply_gain_envelope_block(
                block,
                sample_rate,
                executor.gain_db,
                gain_envelope,
                frame_offset=frame_offset,
                compiled_points=compiled_gain_envelope,
            )
            stop = frame_offset + int(block.shape[0])
            if stop > source.frame_count:
                raise RuntimeError("streamed analysis stem produced excess frames")
            if not post_gain_nonfinite:
                try:
                    transaction.append(block)
                except ValueError as exc:
                    if str(exc) != "stem analysis block is not finite":
                        raise
                    # Preserve the established user-facing peak error, while
                    # still draining the raw source through its SHA/finite/
                    # identity gates before accepting that failure.
                    post_gain_nonfinite = True
            if post_gain_nonfinite:
                stem_peak = math.nan
            else:
                stem_peak = max(stem_peak, _absolute_peak(block))
            frame_offset = stop

        # Iterator exhaustion closes every source verification gate.  An
        # explicit close must also succeed before peak acceptance, diagnostics,
        # WAV output or either mix bus can observe the stem.
        source.close()
        if frame_offset != source.frame_count:
            raise RuntimeError("streamed analysis stem changed frame count")
        _validate_stem_peak(executor, stem_peak)
        view = transaction.finish_view()
        return view, stem_peak
    except BaseException:
        close_blocks = getattr(raw_blocks, "close", None)
        if callable(close_blocks):
            try:
                close_blocks()
            except BaseException:
                pass
        try:
            source.close()
        except BaseException:
            pass
        try:
            transaction.close()
        except BaseException:
            pass
        raise


def _consume_streamed_raw_stem(
    source: StemBlockSource,
    *,
    sample_rate: int,
    base_gain_db: float,
    gain_envelope: Any,
    bus: Any,
    send_bus: Any | None,
    total_frames: int,
    left_gain: float,
    right_gain: float,
    send_scale: float | None,
    stem_target: Path | None,
    stem_evidence_sink: Callable[[WavFileEvidence | None], None] | None = None,
) -> float:
    """Tee, gain, write and mix one verified raw source in one bounded pass."""

    import numpy as np

    frame_offset = 0
    stem_peak = 0.0
    compiled_gain_envelope: tuple[Any, Any] | None = None

    raw_blocks: Any | None = None

    def processed_blocks() -> Any:
        nonlocal frame_offset, stem_peak, compiled_gain_envelope
        assert raw_blocks is not None
        for raw_block in raw_blocks:
            # Sources deliberately expose immutable raw bytes.  This is the
            # sole bounded writable copy used by gain, WAV encoding and both
            # mix buses; no track-sized ndarray is materialised.
            block = np.array(
                raw_block,
                dtype=np.float32,
                order="C",
                copy=True,
            )
            if gain_envelope and compiled_gain_envelope is None:
                compiled_gain_envelope = _compile_gain_envelope_points(
                    gain_envelope
                )
            _apply_gain_envelope_block(
                block,
                sample_rate,
                base_gain_db,
                gain_envelope,
                frame_offset=frame_offset,
                compiled_points=compiled_gain_envelope,
            )
            block_peak = _absolute_peak(block)
            if not math.isfinite(block_peak):
                raise ValueError("streamed stem produced non-finite samples")
            stem_peak = max(stem_peak, block_peak)

            mix_frames = min(
                int(block.shape[0]),
                max(0, total_frames - frame_offset),
            )
            if mix_frames:
                _accumulate_stem(
                    bus[frame_offset : frame_offset + mix_frames],
                    (
                        None
                        if send_bus is None
                        else send_bus[
                            frame_offset : frame_offset + mix_frames
                        ]
                    ),
                    block,
                    mix_frames,
                    left_gain,
                    right_gain,
                    send_scale,
                )
            frame_offset += int(block.shape[0])
            yield block

    blocks: Any | None = None
    try:
        raw_blocks = source.iter_blocks(65_536)
        blocks = processed_blocks()
        if stem_target is None:
            for _block in blocks:
                pass
        else:
            written, write_evidence = (
                _write_wav_pcm24_blocks_with_optional_evidence(
                    stem_target,
                    blocks,
                    sample_rate,
                    expected_frame_count=source.frame_count,
                )
            )
            if written != source.frame_count:
                raise RuntimeError(
                    "streamed stem writer changed frame count"
                )
            if stem_evidence_sink is not None:
                stem_evidence_sink(write_evidence)
        if frame_offset != source.frame_count:
            raise RuntimeError("streamed stem changed frame count")
    except BaseException:
        for iterator in (blocks, raw_blocks):
            close_iterator = getattr(iterator, "close", None)
            if callable(close_iterator):
                try:
                    close_iterator()
                except BaseException:
                    pass
        try:
            source.close()
        except BaseException:
            pass
        raise
    else:
        source.close()
        return stem_peak


# Private compatibility seam retained for focused cache-failure tests and
# embedders; the implementation now accepts every StemBlockSource.
_consume_verified_cache_stem = _consume_streamed_raw_stem


def _mapped_dry_mix_bus_layout(plan: PerformancePlan) -> tuple[int, int]:
    """Return the no-hall float64 stereo bus shape and exact byte claim."""

    dry_frames = max(1, round(plan.duration_seconds * plan.sample_rate))
    return dry_frames, dry_frames * 2 * 8


def _is_builtin_space_config(value: Any) -> bool:
    from .space import SpaceConfig

    return type(value) is SpaceConfig


def _mapped_hall_mix_buses_layout(
    plan: PerformancePlan,
    space: Any,
) -> tuple[int, int, int, int]:
    """Return total frames plus exact mix, send and aggregate byte claims."""

    dry_frames = max(1, round(plan.duration_seconds * plan.sample_rate))
    tail_frames = max(
        0,
        math.ceil(space.tail_seconds(plan.sample_rate) * plan.sample_rate),
    )
    total_frames = dry_frames + tail_frames
    mix_bytes = total_frames * 2 * 8
    send_bytes = total_frames * 2 * 4
    return total_frames, mix_bytes, send_bytes, mix_bytes + send_bytes


def _effective_collaboration_mode_for_mapped_bus(
    plan: PerformancePlan,
    mode_override: str | None,
) -> str | None:
    """Conservatively identify manual mode without moving public validation."""

    if mode_override is not None:
        return mode_override if mode_override in ("manual", "analyze", "suggest") else None
    settings = getattr(plan, "collaboration", None)
    return settings.mode if isinstance(settings, CollaborationSettings) else "manual"


def _same_plain_directory_identity(
    first: PlainDirectoryIdentity,
    second: PlainDirectoryIdentity,
) -> bool:
    return (
        first.path == second.path
        and first.device == second.device
        and first.inode == second.inode
    )


def _revalidate_mapped_scratch_directory(
    identity: PlainDirectoryIdentity,
    expected_volume_id: str,
    *,
    lease: Any | None = None,
) -> Path:
    """Keep the mapped file on the directory and volume that were admitted."""

    directory = revalidate_plain_directory(identity)
    current_volume_id = scratch_volume_identity(directory)
    if current_volume_id != expected_volume_id:
        raise WorkerSlotError("mapped mix scratch volume identity changed")
    if lease is None:
        return directory

    lease_identity = capture_plain_directory(lease.scratch_directory)
    if not _same_plain_directory_identity(identity, lease_identity):
        raise WorkerSlotError("mapped mix scratch directory identity changed")
    admitted_claim = getattr(lease, "claim", None)
    if (
        admitted_claim is None
        or getattr(admitted_claim, "scratch_volume_id", None)
        != expected_volume_id
    ):
        raise WorkerSlotError("mapped mix scratch lease has the wrong volume")
    return directory


def _close_mapped_dry_mix_bus_resources(
    mapping: Any | None,
    temporary: Any | None,
    lease: Any | None,
) -> BaseException | None:
    """Close mapping, file and ledger lock in that order; return first error."""

    first_error: BaseException | None = None
    for resource in (mapping, temporary, lease):
        if resource is None:
            continue
        try:
            resource.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _close_mapped_hall_mix_bus_resources(
    mix_mapping: Any | None,
    send_mapping: Any | None,
    mix_temporary: Any | None,
    send_temporary: Any | None,
    lease: Any | None,
) -> BaseException | None:
    """Close every mapping, then every file, then the aggregate lease."""

    first_error: BaseException | None = None
    seen_mappings: set[int] = set()
    for mapping in (mix_mapping, send_mapping):
        if mapping is None or id(mapping) in seen_mappings:
            continue
        seen_mappings.add(id(mapping))
        try:
            mapping.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    for resource in (mix_temporary, send_temporary, lease):
        if resource is None:
            continue
        try:
            resource.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    return first_error


def _annotate_cleanup_error(
    primary: BaseException,
    cleanup_error: BaseException | None,
) -> None:
    if cleanup_error is None:
        return
    try:
        primary.add_note(
            "mapped dry mix bus cleanup also failed: "
            f"{type(cleanup_error).__name__}: {cleanup_error}"
        )
    except BaseException:
        pass


class _MappedDryMixBusTransport:
    """Own one mapped bus and its backing-file scratch admission."""

    __slots__ = ("bus", "_mapping", "_temporary", "_lease", "_closed")

    def __init__(
        self,
        bus: Any,
        mapping: Any,
        temporary: Any,
        lease: Any,
    ) -> None:
        self.bus = bus
        self._mapping = mapping
        self._temporary = temporary
        self._lease = lease
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        mapping = self._mapping
        temporary = self._temporary
        lease = self._lease
        self._mapping = None
        self._temporary = None
        self._lease = None
        cleanup_error = _close_mapped_dry_mix_bus_resources(
            mapping,
            temporary,
            lease,
        )
        # Drop the ndarray only after its mmap is explicitly closed.  This is
        # significant on Windows, where the TemporaryFile directory entry is
        # not retired until the file handle closes.
        self.bus = None
        if cleanup_error is not None:
            raise cleanup_error


class _MappedHallMixBusesTransport:
    """Own the mapped float64 mix and float32 hall-send buses together."""

    __slots__ = (
        "bus",
        "send_bus",
        "_mix_mapping",
        "_send_mapping",
        "_mix_temporary",
        "_send_temporary",
        "_lease",
        "_closed",
    )

    def __init__(
        self,
        bus: Any,
        send_bus: Any,
        mix_mapping: Any,
        send_mapping: Any,
        mix_temporary: Any,
        send_temporary: Any,
        lease: Any,
    ) -> None:
        self.bus = bus
        self.send_bus = send_bus
        self._mix_mapping = mix_mapping
        self._send_mapping = send_mapping
        self._mix_temporary = mix_temporary
        self._send_temporary = send_temporary
        self._lease = lease
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        mix_mapping = self._mix_mapping
        send_mapping = self._send_mapping
        mix_temporary = self._mix_temporary
        send_temporary = self._send_temporary
        lease = self._lease
        self._mix_mapping = None
        self._send_mapping = None
        self._mix_temporary = None
        self._send_temporary = None
        self._lease = None
        cleanup_error = _close_mapped_hall_mix_bus_resources(
            mix_mapping,
            send_mapping,
            mix_temporary,
            send_temporary,
            lease,
        )
        self.bus = None
        self.send_bus = None
        if cleanup_error is not None:
            raise cleanup_error


def _mapped_dry_mix_bus_transport_factory(
    bus: Any,
    mapping: Any,
    temporary: Any,
    lease: Any,
) -> _MappedDryMixBusTransport:
    """Injectable ownership-transfer seam for the fully built dry transport."""

    return _MappedDryMixBusTransport(bus, mapping, temporary, lease)


def _mapped_hall_mix_buses_transport_factory(
    bus: Any,
    send_bus: Any,
    mix_mapping: Any,
    send_mapping: Any,
    mix_temporary: Any,
    send_temporary: Any,
    lease: Any,
) -> _MappedHallMixBusesTransport:
    """Injectable ownership-transfer seam for the fully built hall transport."""

    return _MappedHallMixBusesTransport(
        bus,
        send_bus,
        mix_mapping,
        send_mapping,
        mix_temporary,
        send_temporary,
        lease,
    )


def _try_mapped_dry_mix_bus(
    plan: PerformancePlan,
    scratch_identity: PlainDirectoryIdentity,
    *,
    space: Any | None,
    collaboration_mode: str | None,
) -> _MappedDryMixBusTransport | None:
    """Acquire and create the automatic long-score dry bus, or use RAM.

    Resource unavailability is an optimization miss.  Memory exhaustion and
    any inability to prove the scratch directory/volume identity are hard
    failures, because silently changing storage after admission would evade
    the shared worker/session scratch budget.
    """

    import numpy as np

    # Keep the private optimization transparent to tests/embedders that
    # replace the generation function and pass an opaque sentinel plan.
    if not isinstance(plan, PerformancePlan):
        return None
    effective_mode = _effective_collaboration_mode_for_mapped_bus(
        plan,
        collaboration_mode,
    )
    if space is not None or effective_mode != "manual":
        return None
    dry_frames, bus_bytes = _mapped_dry_mix_bus_layout(plan)
    if bus_bytes < _MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES:
        return None

    scratch_directory = revalidate_plain_directory(scratch_identity)
    expected_volume_id = scratch_volume_identity(scratch_directory)
    try:
        pool = WorkerSlotPool()
        lease = pool.reserve_session_scratch(
            SessionScratchClaim(
                scratch_bytes=bus_bytes,
                scratch_directory=scratch_directory,
            )
        )
    except MemoryError:
        raise
    except (OSError, ValueError, WorkerSlotError):
        # The per-user allocator is optional.  Re-prove the requested scratch
        # identity before classifying this as an ordinary RAM fallback.
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
        )
        return None
    if lease is None:
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
        )
        return None

    mapping: Any | None = None
    temporary: Any | None = None
    bus: Any | None = None
    try:
        scratch_directory = _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
            lease=lease,
        )
        admitted_claim = lease.claim
        if getattr(admitted_claim, "scratch_bytes", None) != bus_bytes:
            raise WorkerSlotError("mapped mix scratch lease has the wrong size")
        temporary = tempfile.TemporaryFile(
            mode="w+b",
            prefix=".tianlai-dry-mix-bus.",
            suffix=".tmp",
            dir=scratch_directory,
        )
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
            lease=lease,
        )
        temporary.truncate(bus_bytes)
        status = os.fstat(temporary.fileno())
        if not stat.S_ISREG(status.st_mode) or status.st_size != bus_bytes:
            raise WorkerSlotError("mapped mix scratch file has an invalid shape")
        bus = np.memmap(
            temporary,
            mode="r+",
            dtype=np.float64,
            shape=(dry_frames, 2),
        )
        mapping = getattr(bus, "_mmap", None)
        if mapping is None:
            raise WorkerSlotError("mapped mix bus has no owned mapping")
        # A newly extended ordinary file normally reads as zero without
        # dirtying every page.  Verify the exact zero bit pattern instead of
        # eagerly writing the full mapping; if a filesystem exposes any other
        # extension contents, initialise it explicitly before rendering.
        if bool(np.any(bus.view(np.uint8))):
            bus.fill(0.0)
        if os.fstat(temporary.fileno()).st_size != bus_bytes:
            raise WorkerSlotError("mapped mix scratch file size changed")
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
            lease=lease,
        )
        assert bus is not None and mapping is not None and temporary is not None
        return _mapped_dry_mix_bus_transport_factory(
            bus,
            mapping,
            temporary,
            lease,
        )
    except MemoryError as primary:
        cleanup_error = _close_mapped_dry_mix_bus_resources(
            mapping,
            temporary,
            lease,
        )
        _annotate_cleanup_error(primary, cleanup_error)
        raise
    except OSError as primary:
        cleanup_error = _close_mapped_dry_mix_bus_resources(
            mapping,
            temporary,
            lease,
        )
        try:
            _revalidate_mapped_scratch_directory(
                scratch_identity,
                expected_volume_id,
            )
        except BaseException as identity_error:
            _annotate_cleanup_error(identity_error, cleanup_error)
            raise identity_error from primary
        identity_errnos = {
            errno.ELOOP,
            errno.ENOENT,
            errno.ENOTDIR,
            errno.ESTALE,
            errno.EXDEV,
            errno.ENOTSUP,
        }
        if primary.errno in identity_errnos or cleanup_error is not None:
            _annotate_cleanup_error(primary, cleanup_error)
            raise
        # Ordinary create/truncate/map failures leave no live scratch claim
        # and simply retain the established anonymous-RAM bus.
        return None
    except BaseException as primary:
        cleanup_error = _close_mapped_dry_mix_bus_resources(
            mapping,
            temporary,
            lease,
        )
        _annotate_cleanup_error(primary, cleanup_error)
        raise

def _try_mapped_hall_mix_buses(
    plan: PerformancePlan,
    scratch_identity: PlainDirectoryIdentity,
    *,
    space: Any | None,
    collaboration_mode: str | None,
) -> _MappedHallMixBusesTransport | None:
    """Acquire both long manual hall buses under one exact scratch claim."""

    import numpy as np

    # Calling arbitrary space objects before the established generation
    # validation could change their side effects or first error.  The immutable
    # built-in value object is the only safe zero-configuration fast path.
    if not isinstance(plan, PerformancePlan) or not _is_builtin_space_config(space):
        return None
    if _effective_collaboration_mode_for_mapped_bus(
        plan,
        collaboration_mode,
    ) != "manual":
        return None
    # A hand-constructed invalid plan must retain generation's validation and
    # error order.  Valid public plans always satisfy these inexpensive guards.
    if (
        isinstance(plan.sample_rate, bool)
        or not isinstance(plan.sample_rate, int)
        or not 8_000 <= plan.sample_rate <= 384_000
        or isinstance(plan.duration_seconds, bool)
        or not isinstance(plan.duration_seconds, Real)
        or not math.isfinite(float(plan.duration_seconds))
        or plan.duration_seconds < 0.0
    ):
        return None

    total_frames, mix_bytes, send_bytes, scratch_bytes = (
        _mapped_hall_mix_buses_layout(plan, space)
    )
    if scratch_bytes < _MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES:
        return None

    scratch_directory = revalidate_plain_directory(scratch_identity)
    expected_volume_id = scratch_volume_identity(scratch_directory)
    try:
        pool = WorkerSlotPool()
        lease = pool.reserve_session_scratch(
            SessionScratchClaim(
                scratch_bytes=scratch_bytes,
                scratch_directory=scratch_directory,
            )
        )
    except MemoryError:
        raise
    except (OSError, ValueError, WorkerSlotError):
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
        )
        return None
    if lease is None:
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
        )
        return None

    mix_mapping: Any | None = None
    send_mapping: Any | None = None
    mix_temporary: Any | None = None
    send_temporary: Any | None = None
    bus: Any | None = None
    send_bus: Any | None = None
    try:
        scratch_directory = _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
            lease=lease,
        )
        admitted_claim = lease.claim
        if getattr(admitted_claim, "scratch_bytes", None) != scratch_bytes:
            raise WorkerSlotError("mapped hall scratch lease has the wrong size")

        mix_temporary = tempfile.TemporaryFile(
            mode="w+b",
            prefix=".tianlai-hall-mix-bus.",
            suffix=".tmp",
            dir=scratch_directory,
        )
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
            lease=lease,
        )
        mix_temporary.truncate(mix_bytes)
        mix_status = os.fstat(mix_temporary.fileno())
        if not stat.S_ISREG(mix_status.st_mode) or mix_status.st_size != mix_bytes:
            raise WorkerSlotError("mapped hall mix scratch file has an invalid shape")
        bus = np.memmap(
            mix_temporary,
            mode="r+",
            dtype=np.float64,
            shape=(total_frames, 2),
        )
        mix_mapping = getattr(bus, "_mmap", None)
        if mix_mapping is None:
            raise WorkerSlotError("mapped hall mix bus has no owned mapping")

        send_temporary = tempfile.TemporaryFile(
            mode="w+b",
            prefix=".tianlai-hall-send-bus.",
            suffix=".tmp",
            dir=scratch_directory,
        )
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
            lease=lease,
        )
        send_temporary.truncate(send_bytes)
        send_status = os.fstat(send_temporary.fileno())
        if not stat.S_ISREG(send_status.st_mode) or send_status.st_size != send_bytes:
            raise WorkerSlotError("mapped hall send scratch file has an invalid shape")
        send_bus = np.memmap(
            send_temporary,
            mode="r+",
            dtype=np.float32,
            shape=(total_frames, 2),
        )
        send_mapping = getattr(send_bus, "_mmap", None)
        if send_mapping is None:
            raise WorkerSlotError("mapped hall send bus has no owned mapping")

        for mapped_bus, expected_dtype in (
            (bus, np.dtype(np.float64)),
            (send_bus, np.dtype(np.float32)),
        ):
            if (
                not isinstance(mapped_bus, np.ndarray)
                or mapped_bus.shape != (total_frames, 2)
                or mapped_bus.dtype != expected_dtype
                or not mapped_bus.flags.c_contiguous
                or not mapped_bus.flags.writeable
            ):
                raise WorkerSlotError("mapped hall bus has an invalid array layout")
            if bool(np.any(mapped_bus.view(np.uint8))):
                mapped_bus.fill(0.0)
                if bool(np.any(mapped_bus.view(np.uint8))):
                    raise WorkerSlotError("mapped hall bus could not be zeroed")

        if (
            os.fstat(mix_temporary.fileno()).st_size != mix_bytes
            or os.fstat(send_temporary.fileno()).st_size != send_bytes
        ):
            raise WorkerSlotError("mapped hall scratch file size changed")
        _revalidate_mapped_scratch_directory(
            scratch_identity,
            expected_volume_id,
            lease=lease,
        )
        assert (
            bus is not None
            and send_bus is not None
            and mix_mapping is not None
            and send_mapping is not None
            and mix_temporary is not None
            and send_temporary is not None
        )
        return _mapped_hall_mix_buses_transport_factory(
            bus,
            send_bus,
            mix_mapping,
            send_mapping,
            mix_temporary,
            send_temporary,
            lease,
        )
    except MemoryError as primary:
        cleanup_error = _close_mapped_hall_mix_bus_resources(
            mix_mapping,
            send_mapping,
            mix_temporary,
            send_temporary,
            lease,
        )
        _annotate_cleanup_error(primary, cleanup_error)
        raise
    except OSError as primary:
        cleanup_error = _close_mapped_hall_mix_bus_resources(
            mix_mapping,
            send_mapping,
            mix_temporary,
            send_temporary,
            lease,
        )
        try:
            _revalidate_mapped_scratch_directory(
                scratch_identity,
                expected_volume_id,
            )
        except BaseException as identity_error:
            _annotate_cleanup_error(identity_error, cleanup_error)
            raise identity_error from primary
        identity_errnos = {
            errno.ELOOP,
            errno.ENOENT,
            errno.ENOTDIR,
            errno.ESTALE,
            errno.EXDEV,
            errno.ENOTSUP,
        }
        if primary.errno in identity_errnos or cleanup_error is not None:
            _annotate_cleanup_error(primary, cleanup_error)
            raise
        return None
    except BaseException as primary:
        cleanup_error = _close_mapped_hall_mix_bus_resources(
            mix_mapping,
            send_mapping,
            mix_temporary,
            send_temporary,
            lease,
        )
        _annotate_cleanup_error(primary, cleanup_error)
        raise

def _render_plan_generation(
    plan: PerformancePlan,
    output_directory: str | Path,
    *,
    write_stems: bool = True,
    master_gain_db: float = 0.0,
    normalize_peak_db: float | None = None,
    space: "Any | None" = None,
    collaboration_mode: str | None = None,
    stem_cache_directory: str | Path | None = None,
    refresh_stem_cache: bool = False,
    analysis_cache_directory: str | Path | None = None,
    _authoring_project_binding: dict[str, str] | None = None,
    _authoring_workflow_binding: dict[str, Any] | None = None,
    _progress_callback: Callable[[str, int, int], None] | None = None,
    _dry_mix_bus: Any | None = None,
    _hall_send_bus: Any | None = None,
) -> EnsembleResult:
    """Render every executor to a stem and sum them into one mix.

    ``normalize_peak_db`` is an optional master-level stage, off by default.
    When set (e.g. ``-1.0``), the summed bus is scaled by a single measured
    scalar so its peak lands exactly there — nothing else changes: not the
    balance between parts, not the dynamic contour within a part.  A soft solo
    piano piece renders honestly quiet; this is the one place allowed to lift
    the whole thing to a delivery level, and it records how much it lifted.
    """

    import numpy as np

    master_gain_db = _finite_float(master_gain_db, "master_gain_db")
    collaboration = _resolved_collaboration_settings(
        plan,
        collaboration_mode,
    )
    plan_collaboration = getattr(plan, "collaboration", None)
    plan_collaboration_mode = (
        plan_collaboration.mode
        if isinstance(plan_collaboration, CollaborationSettings)
        else "manual"
    )
    if normalize_peak_db is not None:
        normalize_peak_db = _finite_float(
            normalize_peak_db, "normalize_peak_db"
        )
        if normalize_peak_db > 0.0:
            raise ValueError("normalize_peak_db 必须 ≤ 0（以 dBFS 计，满刻度为 0）")

    plan_document = plan.to_dict()
    plan_canonical = _canonical_json_bytes(plan_document)
    plan_sha256 = hashlib.sha256(plan_canonical).hexdigest()

    space_parameters: dict[str, Any] | None = None
    effective_filter_hz: dict[str, float] | None = None
    effective_tail_seconds: float | None = None
    if space is not None:
        space_parameters = space.to_dict()
        if not isinstance(space_parameters, dict):
            raise ValueError("space.to_dict() 必须返回对象")
        _reject_nonfinite_tree(space_parameters, "space")
        highpass_hz, damping_hz = space.effective_filter_frequencies(
            plan.sample_rate
        )
        effective_filter_hz = {
            "highpass_hz": _finite_float(
                highpass_hz, "space.effective_filter_hz.highpass_hz"
            ),
            "damping_hz": _finite_float(
                damping_hz, "space.effective_filter_hz.damping_hz"
            ),
        }
        effective_tail_seconds = _finite_float(
            space.tail_seconds(plan.sample_rate),
            "space.effective_tail_seconds",
        )
        if effective_tail_seconds < 0.0:
            raise ValueError("space.effective_tail_seconds 不得为负数")

    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    staging_identity = (
        capture_plain_directory(directory)
        if collaboration.mode in ("analyze", "suggest")
        else None
    )
    plan_path = directory / PERFORMANCE_PLAN_NAME
    _write_json_atomic(plan_path, plan_document)
    stem_directory = directory / "分轨"
    if write_stems:
        stem_directory.mkdir(parents=True, exist_ok=True)

    dry_frames = max(1, round(plan.duration_seconds * plan.sample_rate))
    reverb_tail_frames = (
        0
        if effective_tail_seconds is None
        else max(0, math.ceil(effective_tail_seconds * plan.sample_rate))
    )
    total_frames = dry_frames + reverb_tail_frames
    if _dry_mix_bus is None:
        if _hall_send_bus is not None:
            raise ValueError(
                "private mapped hall buses must provide mix and send together"
            )
        bus = np.zeros((total_frames, 2), dtype=np.float64)
    else:
        if (
            collaboration.mode != "manual"
            or (
                space is None
                and reverb_tail_frames != 0
            )
        ):
            raise ValueError(
                "private mapped mix bus requires manual mode and an exact tail"
            )
        if space is None and _hall_send_bus is not None:
            raise ValueError("private mapped dry mix bus cannot provide a hall send")
        if space is not None and _hall_send_bus is None:
            raise ValueError(
                "private mapped hall buses must provide mix and send together"
            )
        if (
            not isinstance(_dry_mix_bus, np.ndarray)
            or _dry_mix_bus.shape != (total_frames, 2)
            or _dry_mix_bus.dtype != np.dtype(np.float64)
            or not _dry_mix_bus.flags.c_contiguous
            or not _dry_mix_bus.flags.writeable
        ):
            raise ValueError(
                "private mapped dry mix bus must be writable C-contiguous "
                "float64 stereo with the exact render length"
            )
        bus = _dry_mix_bus
    # 共享厅堂保留每条干分轨的左右相位。各声部按座位距离送入同一条
    # 立体声总线，渲染完统一加回合奏——分轨本身仍是全干、可复算。
    if _hall_send_bus is None:
        send_bus = (
            np.zeros((total_frames, 2), dtype=np.float32)
            if space is not None
            else None
        )
    else:
        if (
            space is None
            or collaboration.mode != "manual"
            or not _is_builtin_space_config(space)
            or not isinstance(_hall_send_bus, np.ndarray)
            or _hall_send_bus.shape != (total_frames, 2)
            or _hall_send_bus.dtype != np.dtype(np.float32)
            or not _hall_send_bus.flags.c_contiguous
            or not _hall_send_bus.flags.writeable
        ):
            raise ValueError(
                "private mapped hall send bus must be writable C-contiguous "
                "float32 stereo with the exact render length"
            )
        send_bus = _hall_send_bus
    stems: list[StemResult] = []
    stem_write_evidence_by_executor: dict[
        str,
        WavFileEvidence | None,
    ] = {}
    manifest_sha256_by_executor: dict[str, str] = {}
    stem_cache = (
        StemCache(stem_cache_directory)
        if stem_cache_directory is not None
        else None
    )
    stem_cache_summary = (
        _new_stem_cache_summary(
            refresh_requested=bool(refresh_stem_cache),
            total=len(plan.parts),
        )
        if stem_cache is not None
        else None
    )
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ] = {}
    mix_report_builder = (
        CollaborationReportBuilder(
            collaboration,
            plan.sample_rate,
            # The private staging identity is revalidated both by the runtime
            # transaction gate and by the builder itself.  Keeping analysis
            # scratch here binds its free-space claim to cache/WAV staging on
            # the same volume; every mapping is closed before publication.
            scratch_parent=directory,
            cache_directory=analysis_cache_directory,
            expected_stem_count=len(plan.parts),
            dry_frame_count=dry_frames,
        )
        if collaboration.mode in ("analyze", "suggest")
        else None
    )
    mix_report: dict[str, Any] | None = None
    mix_report_path: Path | None = None
    analysis_cache_summary: dict[str, Any] | None = None

    render_loop_completed = False
    render_part_total = max(1, len(plan.parts))
    if _progress_callback is not None:
        _progress_callback("render_parts", 0, render_part_total)
    buffer: Any | None = None
    analysis_view: Any | None = None
    raw_stems = _iter_raw_stems_in_plan_order(
        plan,
        scratch_directory=directory,
        hall_tail_seconds=(effective_tail_seconds or 0.0),
        cache=stem_cache,
        stream_cache_hits=True,
        direct_cache_fallback=mix_report_builder is not None,
        refresh=bool(refresh_stem_cache),
        runtime_fingerprints=runtime_fingerprints,
        summary=stem_cache_summary,
    )

    def close_raw_phase(*, suppress_errors: bool) -> None:
        nonlocal analysis_view, buffer

        errors: list[BaseException] = []
        close_raw_stems = getattr(raw_stems, "close", None)
        if callable(close_raw_stems):
            try:
                close_raw_stems()
            except BaseException as exc:
                errors.append(exc)
        try:
            # The phase-max resource model assumes stem children have exited
            # before analysis/reverb/final mix.
            retire_idle_stem_workers()
        except BaseException as exc:
            errors.append(exc)
        if isinstance(buffer, StemBlockSource):
            try:
                buffer.close()
            except BaseException as exc:
                errors.append(exc)
        buffer = None
        if analysis_view is not None:
            try:
                analysis_view.close()
            except BaseException as exc:
                errors.append(exc)
            analysis_view = None
        if (
            (not render_loop_completed or errors)
            and mix_report_builder is not None
        ):
            try:
                mix_report_builder.close()
            except BaseException as exc:
                errors.append(exc)
        if not errors:
            return
        if suppress_errors:
            for error in errors:
                _warn_cleanup(
                    "raw stem phase cleanup did not complete: "
                    f"{type(error).__name__}"
                )
            return
        raise errors[0]

    try:
        for (
            part_index,
            part,
            buffer,
            peak_voices,
            manifest_sha256,
        ) in raw_stems:
            manifest_sha256_by_executor[
                part.executor.executor_id
            ] = manifest_sha256
            streamed_analysis = False
            if isinstance(buffer, StemBlockSource):
                if mix_report_builder is not None:
                    assert staging_identity is not None
                    streamed_source = buffer
                    transaction = _try_begin_streamed_analysis_transaction(
                        mix_report_builder,
                        part.executor,
                        streamed_source,
                        staging_identity=staging_identity,
                        write_stems=write_stems,
                    )
                    if transaction is None:
                        # Short sources and insufficient/unavailable scratch
                        # retain the established full-array path.  The gate ran
                        # before consumption, so this never rerenders a stem.
                        try:
                            buffer = streamed_source.materialise()
                        except BaseException:
                            try:
                                streamed_source.close()
                            except BaseException:
                                pass
                            raise
                        else:
                            streamed_source.close()
                    else:
                        analysis_view, stem_peak = (
                            _consume_streamed_analysis_stem(
                                streamed_source,
                                transaction,
                                sample_rate=plan.sample_rate,
                                executor=part.executor,
                                gain_envelope=part.gain_envelope,
                            )
                        )
                        buffer = analysis_view.audio
                        streamed_analysis = True
                else:
                    stem_path: str | None = None
                    target: Path | None = None
                    if write_stems:
                        target = stem_directory / portable_stem_filename(
                            part.executor.executor_id
                        )
                        stem_path = str(target)
                    left_gain, right_gain = balance_gains(
                        part.executor.pan
                    )
                    send_scale: float | None = None
                    if send_bus is not None:
                        send_scale = space.send_scale(
                            part.executor.seat.distance_m
                        )
                    stem_evidence_sink = (
                        None
                        if target is None
                        else lambda evidence, executor_id=(
                            part.executor.executor_id
                        ): stem_write_evidence_by_executor.__setitem__(
                            executor_id,
                            evidence,
                        )
                    )
                    stem_peak = _consume_verified_cache_stem(
                        buffer,
                        sample_rate=plan.sample_rate,
                        base_gain_db=part.executor.gain_db,
                        gain_envelope=part.gain_envelope,
                        bus=bus,
                        send_bus=send_bus,
                        total_frames=total_frames,
                        left_gain=left_gain,
                        right_gain=right_gain,
                        send_scale=send_scale,
                        stem_target=target,
                        stem_evidence_sink=stem_evidence_sink,
                    )
                    _validate_stem_peak(part.executor, stem_peak)
                    stems.append(
                        StemResult(
                            executor_id=part.executor.executor_id,
                            part_id=part.executor.part_id,
                            instrument=(
                                part.executor.capability.relative_path
                            ),
                            path=stem_path,
                            peak=stem_peak,
                            gain_db=part.executor.gain_db,
                            pan=part.executor.pan,
                            peak_voices=peak_voices,
                            gain_envelope=tuple(
                                {
                                    "time_seconds": round(
                                        point.time_seconds,
                                        6,
                                    ),
                                    "offset_db": point.offset_db,
                                    "effective_gain_db": (
                                        part.executor.gain_db
                                        + point.offset_db
                                    ),
                                }
                                for point in part.gain_envelope
                            ),
                        )
                    )
                    if _progress_callback is not None:
                        _progress_callback(
                            "render_parts",
                            part_index + 1,
                            render_part_total,
                        )
                    buffer = None
                    continue
            # 增益在写盘之前施加。分轨是后置增益、前置声像的信号:这样它反映的
            # 就是它在合奏里的电平,让总线不过载的那个增益同样让分轨不过载,而
            # 分轨自己的立体声像仍然完整,方便拿去重新混。
            if not streamed_analysis:
                apply_gain_envelope(
                    buffer,
                    plan.sample_rate,
                    part.executor.gain_db,
                    part.gain_envelope,
                )
                stem_peak = _absolute_peak(buffer)
                _validate_stem_peak(part.executor, stem_peak)
                if mix_report_builder is not None:
                    mix_report_builder.add_stem(part.executor, buffer)
            stem_path: str | None = None
            if write_stems:
                target = stem_directory / portable_stem_filename(
                    part.executor.executor_id
                )
                written, stem_write_evidence = (
                    _write_wav_pcm24_with_optional_evidence(
                        target,
                        buffer,
                        plan.sample_rate,
                        expected_frame_count=int(buffer.shape[0]),
                    )
                )
                if written != int(buffer.shape[0]):
                    raise RuntimeError("stem writer changed frame count")
                stem_write_evidence_by_executor[
                    part.executor.executor_id
                ] = stem_write_evidence
                stem_path = str(target)

            left_gain, right_gain = balance_gains(part.executor.pan)
            length = min(total_frames, buffer.shape[0])
            send_scale: float | None = None
            if send_bus is not None:
                # 送入厅堂用后置增益的干声(声像之前),越远的座位送得越湿,
                # 让远处乐器听起来更靠里——直达声电平仍由 gain_db 决定,
                # 不重复衰减。
                send_scale = space.send_scale(
                    part.executor.seat.distance_m
                )
            _accumulate_stem(
                bus,
                send_bus,
                buffer,
                length,
                left_gain,
                right_gain,
                send_scale,
            )
            stems.append(
                StemResult(
                    executor_id=part.executor.executor_id,
                    part_id=part.executor.part_id,
                    instrument=part.executor.capability.relative_path,
                    path=stem_path,
                    peak=stem_peak,
                    gain_db=part.executor.gain_db,
                    pan=part.executor.pan,
                    peak_voices=peak_voices,
                    gain_envelope=tuple(
                        {
                            "time_seconds": round(
                                point.time_seconds,
                                6,
                            ),
                            "offset_db": point.offset_db,
                            "effective_gain_db": (
                                part.executor.gain_db
                                + point.offset_db
                            ),
                        }
                        for point in part.gain_envelope
                    ),
                )
            )
            if analysis_view is not None:
                completed_view = analysis_view
                analysis_view = None
                completed_view.close()
                buffer = None
            if _progress_callback is not None:
                _progress_callback(
                    "render_parts",
                    part_index + 1,
                    render_part_total,
                )
            # The raw-stem iterator is resumed only after this assignment;
            # otherwise the for-loop target would keep the previous ndarray
            # alive while the next worker result is loaded.
            buffer = None
        render_loop_completed = True
    except BaseException:
        close_raw_phase(suppress_errors=True)
        raise
    else:
        close_raw_phase(suppress_errors=False)
    if stem_cache_summary is not None:
        _finalize_stem_cache_summary(stem_cache_summary)

    if _progress_callback is not None:
        _progress_callback("mix", 0, 1)

    # Finish dry-stem diagnostics before allocating/processing the shared
    # hall.  The builder closes and deletes its relation memmaps in build(),
    # keeping long-score analysis and reverb from peaking at the same time.
    if mix_report_builder is not None:
        mix_report = mix_report_builder.build()
        analysis_cache_summary = mix_report_builder.cache_summary
        attach_orchestration_topology(
            mix_report,
            analyze_orchestration_topology(plan),
        )
        mix_report_path = directory / MIX_REPORT_NAME
        dry_stage_metrics = analyze_stereo_stage(
            bus[:dry_frames],
            plan.sample_rate,
        ).to_dict()
    else:
        dry_stage_metrics = None

    space_dict: dict[str, Any] | None = (
        space_parameters if space is not None else None
    )
    wet_signal_present = False
    if (
        space is not None
        and send_bus is not None
        and _absolute_peak(send_bus) > 0.0
    ):
        from .space import render_reverb_stereo

        wet_signal_present = True
        wet_left, wet_right = render_reverb_stereo(
            send_bus[:, 0],
            send_bus[:, 1],
            plan.sample_rate,
            space,
        )
        bus[:, 0] += wet_left
        bus[:, 1] += wet_right
        wet_left = None
        wet_right = None
    # The dry send is never consulted after the optional hall render.
    send_bus = None

    if mix_report is None:
        post_space_stage_metrics = None
    elif space is None:
        # No hall also means no tail extension, so this stage is the exact
        # same buffer measured above.  Keep independent report objects without
        # rescanning every sample.
        assert dry_stage_metrics is not None
        post_space_stage_metrics = deepcopy(dry_stage_metrics)
    else:
        post_space_stage_metrics = analyze_stereo_stage(
            bus,
            plan.sample_rate,
            tail_window_seconds=TAIL_ANALYSIS_SECONDS,
        ).to_dict()

    master_scale = 10.0 ** (master_gain_db / 20.0)
    if master_scale != 1.0:
        bus *= master_scale
    mix_peak = (
        float(post_space_stage_metrics["sample_peak"])
        if master_scale == 1.0 and post_space_stage_metrics is not None
        else _absolute_peak(bus)
    )
    if not math.isfinite(mix_peak):
        raise ValueError("合奏总线产生了非有限样本")
    measured_pre_normalize_peak = mix_peak

    normalize_gain_db = 0.0
    pre_normalize_peak: float | None = None
    if normalize_peak_db is not None:
        pre_normalize_peak = mix_peak
        if mix_peak > 0.0:
            target = 10.0 ** (normalize_peak_db / 20.0)
            if not math.isfinite(target) or target <= 0.0:
                raise ValueError("normalize_peak_db 太低，无法表示有限目标峰值")
            scale = target / mix_peak
            if not math.isfinite(scale):
                raise ValueError("归一化增益不是有限数值")
            bus *= scale
            normalize_gain_db = float(20.0 * np.log10(scale))
            mix_peak = _absolute_peak(bus)
            if not math.isfinite(normalize_gain_db) or not math.isfinite(mix_peak):
                raise ValueError("归一化结果包含非有限数值")
    elif mix_peak > 1.0:
        # 不归一时,总线只拒绝削波;归一时目标峰值 ≤0 dBFS,不可能过载。
        headroom = 20.0 * np.log10(mix_peak)
        raise ValueError(
            f"合奏总线过载:峰值 {mix_peak:.4f}(超出 {headroom:+.2f} dB)。"
            f"写盘会被静默削平,因此拒绝输出。请把 master_gain_db 降到 "
            f"{master_gain_db - headroom:.2f} 或更低,或在编制表里调低各声部 gain_db"
        )

    if mix_report is not None:
        if master_scale == 1.0 and normalize_peak_db is None:
            assert post_space_stage_metrics is not None
            final_stage_metrics = deepcopy(post_space_stage_metrics)
        else:
            final_stage_metrics = analyze_stereo_stage(
                bus,
                plan.sample_rate,
                tail_window_seconds=(
                    TAIL_ANALYSIS_SECONDS if space is not None else None
                ),
            ).to_dict()
        if (
            dry_stage_metrics is None
            or post_space_stage_metrics is None
        ):
            raise RuntimeError("协奏阶段仪表没有完整生成")
        attach_stage_diagnostics(
            mix_report,
            post_pan_pre_space=dry_stage_metrics,
            post_space_pre_master=post_space_stage_metrics,
            final=final_stage_metrics,
        )
        if mix_report_path is None:
            raise RuntimeError("协奏诊断路径没有生成")
        _write_json_atomic(mix_report_path, mix_report)

    mix_path = directory / "合奏.wav"
    frame_count, mix_write_evidence = _write_wav_pcm24_with_optional_evidence(
        mix_path,
        bus,
        plan.sample_rate,
        expected_frame_count=total_frames,
    )
    if _progress_callback is not None:
        _progress_callback("mix", 1, 1)
    # Bind the exact writer output before any downstream reader sees it.  The
    # post-render checker is specified as read-only; keeping this digest lets
    # the orchestration layer fail closed if that invariant ever regresses.
    mix_sha256 = _sha256_written_wav(mix_path, mix_write_evidence)

    stem_receipts: list[dict[str, Any]] = []
    instrument_uses: list[InstrumentUse] = []
    audio_artifacts = [
        AudioArtifact(
            role="mix",
            path=mix_path,
            label=mix_path.relative_to(directory).as_posix(),
            write_evidence=mix_write_evidence,
        )
    ]
    for part, stem in zip(plan.parts, stems, strict=True):
        manifest_path = Path(part.executor.capability.manifest_path)
        manifest_label = (
            Path(part.executor.capability.relative_path) / manifest_path.name
        ).as_posix()
        wav_receipt: dict[str, Any]
        if stem.path is None:
            wav_receipt = {
                "written": False,
                "path": None,
                "sha256": None,
            }
        else:
            stem_path = Path(stem.path)
            stem_write_evidence = stem_write_evidence_by_executor.get(
                stem.executor_id
            )
            wav_receipt = {
                "written": True,
                "path": stem_path.relative_to(directory).as_posix(),
                "sha256": _sha256_written_wav(
                    stem_path,
                    stem_write_evidence,
                ),
            }
            audio_artifacts.append(
                AudioArtifact(
                    role=f"stem:{stem.executor_id}",
                    path=stem_path,
                    label=stem_path.relative_to(directory).as_posix(),
                    write_evidence=stem_write_evidence,
                )
            )
        instrument_uses.append(
            InstrumentUse(
                manifest_path=manifest_path,
                manifest_label=manifest_label,
                used_by=(stem.executor_id,),
                expected_sha256=manifest_sha256_by_executor[
                    stem.executor_id
                ],
            )
        )
        stem_receipts.append(
            {
                "executor_id": stem.executor_id,
                "part_id": stem.part_id,
                "instrument": stem.instrument,
                "manifest": {
                    "path": manifest_label,
                    "sha256": manifest_sha256_by_executor[stem.executor_id],
                },
                "release_status": {
                    "quality_tier": getattr(
                        part.executor.capability,
                        "quality_tier",
                        None,
                    ),
                    "collaboration_review_status": getattr(
                        part.executor.capability,
                        "collaboration_review_status",
                        None,
                    ),
                    "license_status": getattr(
                        part.executor.capability,
                        "license_status",
                        None,
                    ),
                },
                "wav": wav_receipt,
                "peak": stem.peak,
                "peak_voices": stem.peak_voices,
                "gain_db": stem.gain_db,
                "pan": stem.pan,
                "gain_automation": list(stem.gain_envelope),
            }
        )

    license_sidecar_path = directory / ENSEMBLE_LICENSE_SIDECAR_NAME
    attribution_path = directory / ENSEMBLE_ATTRIBUTION_NAME
    license_sidecars = write_license_sidecars(
        license_sidecar_path,
        attribution_path,
        instrument_uses=instrument_uses,
        audio_artifacts=audio_artifacts,
    )

    post_render_check_path = directory / POST_RENDER_CHECK_NAME
    if _progress_callback is not None:
        _progress_callback("post_check", 0, 1)
    post_render_check_report = analyze_rendered_wav(
        mix_path,
        artifact_path=mix_path.relative_to(directory).as_posix(),
        expected_sample_rate=plan.sample_rate,
        expected_frame_count=frame_count,
        expected_activity=_plan_has_explicit_expected_activity(plan),
        plan_sha256=plan_sha256,
    )
    if _sha256_file(mix_path) != mix_sha256:
        raise RuntimeError("渲染后自检不得修改已经写出的合奏音频")
    try:
        require_post_render_check_pass(post_render_check_report)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"渲染后自检未通过: {exc}") from exc
    post_render_check_summary = post_render_check_report.get("summary")
    if not isinstance(post_render_check_summary, dict):  # defensive typing
        raise RuntimeError("渲染后自检缺少 summary 对象")
    write_post_render_check(
        post_render_check_path,
        post_render_check_report,
    )
    if _progress_callback is not None:
        _progress_callback("post_check", 1, 1)

    authoring_binding = _authoring_project_receipt_binding(
        _authoring_project_binding
    )
    workflow_binding = validate_workflow_authorization(
        _authoring_workflow_binding
    )
    if workflow_binding is not None:
        if authoring_binding is None:
            raise ValueError(
                "authoring workflow binding requires an authoring project binding"
            )
        if (
            workflow_binding["project_id"] != authoring_binding["project_id"]
            or workflow_binding["authoring_revision"]
            != authoring_binding["revision"]
        ):
            raise ValueError(
                "authoring workflow and project bindings disagree"
            )
    receipt_path = directory / RENDER_RECEIPT_NAME
    receipt: dict[str, Any] = {
        "format": "tianlai.render_receipt",
        "version": RENDER_RECEIPT_VERSION,
        "hash_algorithm": "SHA-256",
        "performance_plan": {
            "path": plan_path.relative_to(directory).as_posix(),
            "file_sha256": _sha256_file(plan_path),
            "sha256": plan_sha256,
            "canonicalization": (
                "UTF-8 JSON; object keys sorted; compact separators; "
                "ensure_ascii=false; NaN/Infinity forbidden"
            ),
        },
        "audio_format": {
            "container": "WAV",
            "encoding": "PCM",
            "bits_per_sample": 24,
            "channels": 2,
            "sample_rate": plan.sample_rate,
        },
        "master_gain_db": master_gain_db,
        "normalize": {
            "requested_peak_dbfs": normalize_peak_db,
            "pre_normalize_peak": measured_pre_normalize_peak,
            "applied_gain_db": normalize_gain_db,
            "post_normalize_peak": mix_peak,
        },
        "collaboration": {
            "plan_mode": plan_collaboration_mode,
            "requested_override": collaboration_mode,
            "effective_mode": collaboration.mode,
            "audio_modified": False,
            "report_enabled": mix_report is not None,
        },
        "space": (
            {"enabled": False}
            if space_parameters is None
            else {
                "enabled": True,
                "parameters": space_parameters,
                "effective_filter_hz": effective_filter_hz,
                "effective_tail_seconds": effective_tail_seconds,
                "wet_signal_present": wet_signal_present,
            }
        ),
        "mix": {
            "path": mix_path.relative_to(directory).as_posix(),
            "sha256": mix_sha256,
            "peak": mix_peak,
            "frame_count": frame_count,
        },
        "stems": stem_receipts,
        "license_sidecar": {
            "path": license_sidecar_path.relative_to(directory).as_posix(),
            "sha256": license_sidecars.json_sha256,
        },
        "attribution_notice": {
            "path": attribution_path.relative_to(directory).as_posix(),
            "sha256": license_sidecars.text_sha256,
        },
        "post_render_check": {
            "path": post_render_check_path.relative_to(directory).as_posix(),
            "sha256": _sha256_file(post_render_check_path),
            "format": post_render_check_report["format"],
            "version": post_render_check_report["version"],
        },
    }
    if authoring_binding is not None:
        receipt["authoring_project"] = authoring_binding
    if workflow_binding is not None:
        receipt["authoring_workflow"] = workflow_binding
    if mix_report_path is not None and mix_report is not None:
        receipt["mix_report"] = {
            "path": mix_report_path.relative_to(directory).as_posix(),
            "sha256": _sha256_file(mix_report_path),
            "format": mix_report["format"],
            "version": mix_report["version"],
            "mode": mix_report["mode"],
            "scope": mix_report["scope"],
        }
    _write_json_atomic(receipt_path, receipt)
    cache_telemetry_path: Path | None = None
    if (
        stem_cache_summary is not None
        or analysis_cache_summary is not None
    ):
        cache_telemetry_path = directory / CACHE_TELEMETRY_NAME
        _write_json_atomic(
            cache_telemetry_path,
            {
                "format": CACHE_TELEMETRY_FORMAT,
                "version": CACHE_TELEMETRY_VERSION,
                "render_receipt": {
                    "path": RENDER_RECEIPT_NAME,
                    "sha256": _sha256_file(receipt_path),
                },
                "performance_plan": {
                    "canonical_sha256": plan_sha256,
                },
                "mix": {
                    "sha256": receipt["mix"]["sha256"],
                },
                "stem_cache": stem_cache_summary,
                "analysis_cache": analysis_cache_summary,
            },
        )

    return EnsembleResult(
        sample_rate=plan.sample_rate,
        frame_count=frame_count,
        duration_seconds=frame_count / plan.sample_rate,
        mix_path=str(mix_path),
        mix_peak=mix_peak,
        stems=tuple(stems),
        normalize_gain_db=normalize_gain_db,
        pre_normalize_peak=pre_normalize_peak,
        space=space_dict,
        plan_path=str(plan_path),
        mix_report_path=(
            str(mix_report_path) if mix_report_path is not None else None
        ),
        mix_report=mix_report,
        post_render_check_path=str(post_render_check_path),
        post_render_check=post_render_check_report,
        post_render_check_summary=post_render_check_summary,
        collaboration_mode=collaboration.mode,
        receipt_path=str(receipt_path),
        license_sidecar_path=str(license_sidecar_path),
        attribution_path=str(attribution_path),
        stem_cache=stem_cache_summary,
        analysis_cache=analysis_cache_summary,
        cache_telemetry_path=(
            str(cache_telemetry_path)
            if cache_telemetry_path is not None
            else None
        ),
    )


def _render_plan_locked(
    plan: PerformancePlan,
    output_directory: str | Path,
    *,
    write_stems: bool = True,
    master_gain_db: float = 0.0,
    normalize_peak_db: float | None = None,
    space: "Any | None" = None,
    collaboration_mode: str | None = None,
    stem_cache_directory: str | Path | None = None,
    refresh_stem_cache: bool = False,
    analysis_cache_directory: str | Path | None = None,
    _authoring_project_binding: dict[str, str] | None = None,
    _authoring_workflow_binding: dict[str, Any] | None = None,
    _progress_callback: Callable[[str, int, int], None] | None = None,
) -> EnsembleResult:
    final_directory = Path(output_directory)
    parent = final_directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    parent_identity = capture_plain_directory(parent)
    staging_prefix = f".{final_directory.name}.render-stage."
    staging = Path(tempfile.mkdtemp(dir=parent, prefix=staging_prefix))
    staging_identity = capture_plain_directory(staging)
    mapped_transport: (
        _MappedDryMixBusTransport | _MappedHallMixBusesTransport | None
    ) = None
    try:
        if space is None:
            mapped_transport = _try_mapped_dry_mix_bus(
                plan,
                staging_identity,
                space=space,
                collaboration_mode=collaboration_mode,
            )
        else:
            mapped_transport = _try_mapped_hall_mix_buses(
                plan,
                staging_identity,
                space=space,
                collaboration_mode=collaboration_mode,
            )
        if isinstance(mapped_transport, _MappedHallMixBusesTransport):
            private_generation_arguments = {
                "_dry_mix_bus": mapped_transport.bus,
                "_hall_send_bus": mapped_transport.send_bus,
            }
        elif mapped_transport is not None:
            private_generation_arguments = {
                "_dry_mix_bus": mapped_transport.bus,
            }
        else:
            private_generation_arguments = {}
        try:
            staged = _render_plan_generation(
                plan,
                staging,
                write_stems=write_stems,
                master_gain_db=master_gain_db,
                normalize_peak_db=normalize_peak_db,
                space=space,
                collaboration_mode=collaboration_mode,
                stem_cache_directory=stem_cache_directory,
                refresh_stem_cache=refresh_stem_cache,
                analysis_cache_directory=analysis_cache_directory,
                _authoring_project_binding=_authoring_project_binding,
                _authoring_workflow_binding=_authoring_workflow_binding,
                _progress_callback=_progress_callback,
                **private_generation_arguments,
            )
        except BaseException:
            if mapped_transport is not None:
                try:
                    mapped_transport.close()
                except BaseException as cleanup_error:
                    _warn_cleanup(
                        "mapped dry mix bus cleanup failed after render "
                        f"failure: {cleanup_error}"
                    )
                finally:
                    mapped_transport = None
            raise
        else:
            # TemporaryFile has a visible staging-directory entry on Windows.
            # Retire the mapping, handle and lease before generation
            # verification enumerates the artifacts.
            if mapped_transport is not None:
                mapped_transport.close()
                mapped_transport = None
        _verify_render_generation(staging)
        published_stems = tuple(
            replace(
                stem,
                path=(
                    None
                    if stem.path is None
                    else str(
                        final_directory
                        / Path(stem.path).relative_to(staging)
                    )
                ),
            )
            for stem in staged.stems
        )
        published = replace(
            staged,
            mix_path=str(final_directory / "合奏.wav"),
            stems=published_stems,
            plan_path=str(final_directory / PERFORMANCE_PLAN_NAME),
            mix_report_path=(
                str(final_directory / MIX_REPORT_NAME)
                if staged.mix_report_path is not None
                else None
            ),
            post_render_check_path=str(
                final_directory / POST_RENDER_CHECK_NAME
            ),
            receipt_path=str(final_directory / RENDER_RECEIPT_NAME),
            license_sidecar_path=str(
                final_directory / ENSEMBLE_LICENSE_SIDECAR_NAME
            ),
            attribution_path=str(
                final_directory / ENSEMBLE_ATTRIBUTION_NAME
            ),
            cache_telemetry_path=(
                str(final_directory / CACHE_TELEMETRY_NAME)
                if staged.cache_telemetry_path is not None
                else None
            ),
        )
        if _progress_callback is not None:
            _progress_callback("publish", 0, 1)
        _publish_render_artifacts(staging, final_directory)
        if _progress_callback is not None:
            _progress_callback("publish", 1, 1)
        return published
    finally:
        if mapped_transport is not None:
            try:
                mapped_transport.close()
            except BaseException as cleanup_error:
                _warn_cleanup(
                    "mapped dry mix bus cleanup failed while retiring the "
                    f"private render directory: {cleanup_error}"
                )
        try:
            _remove_private_render_directory(
                staging,
                parent,
                staging_prefix,
                parent_identity=parent_identity,
                directory_identity=staging_identity,
            )
        except BaseException as exc:
            _warn_preserved_render_directory(
                f"渲染私有暂存目录无法退出活跃命名空间 "
                f"{staging}: {exc}"
            )


def render_plan(
    plan: PerformancePlan,
    output_directory: str | Path,
    *,
    write_stems: bool = True,
    master_gain_db: float = 0.0,
    normalize_peak_db: float | None = None,
    space: "Any | None" = None,
    collaboration_mode: str | None = None,
    stem_cache_directory: str | Path | None = None,
    refresh_stem_cache: bool = False,
    analysis_cache_directory: str | Path | None = None,
    _acquire_output_lock: bool = True,
    _authoring_project_binding: dict[str, str] | None = None,
    _authoring_workflow_binding: dict[str, Any] | None = None,
    _progress_callback: Callable[[str, int, int], None] | None = None,
) -> EnsembleResult:
    """Render and publish one self-verified generation under exclusive ownership.

    Normal calls use a resolved-path cross-process lock to prevent two writers
    from targeting the same output at once.  Immutable-candidate publication
    may pass the private ``_acquire_output_lock=False`` switch only while it
    owns the stable final-candidate lock and renders into its newly created,
    private staging directory.  The generation is built in a private sibling
    directory, every receipt hash is verified before publication, and the
    receipt is installed last as the commit marker.  Ordinary render or
    filesystem errors therefore leave the previous complete generation
    available.

    Passing ``stem_cache_directory`` enables a non-authoritative cache of raw
    instrument renders.  Every hit is verified against the current
    performance, effective manifest, live audio assets, render source and
    runtime dependencies.  Cache errors fall back to the normal renderer and
    never weaken output publication.  ``refresh_stem_cache`` bypasses reads
    and rerenders the raw stems; a conflicting valid content-addressed entry
    is preserved rather than overwritten.
    """

    if not isinstance(refresh_stem_cache, bool):
        raise ValueError("refresh_stem_cache 必须是布尔值")
    if not isinstance(_acquire_output_lock, bool):
        raise ValueError("_acquire_output_lock 必须是布尔值")
    normalized_authoring_binding = _authoring_project_receipt_binding(
        _authoring_project_binding
    )
    normalized_workflow_binding = validate_workflow_authorization(
        _authoring_workflow_binding
    )
    if normalized_workflow_binding is not None:
        if normalized_authoring_binding is None:
            raise ValueError(
                "authoring workflow binding requires an authoring project binding"
            )
        if (
            normalized_workflow_binding["project_id"]
            != normalized_authoring_binding["project_id"]
            or normalized_workflow_binding["authoring_revision"]
            != normalized_authoring_binding["revision"]
        ):
            raise ValueError(
                "authoring workflow and project bindings disagree"
            )
    # This gate runs before the lock creates its parent directory and before
    # the coordinator allocates or maps a full-length mix bus.  Validation and
    # rendering therefore agree on the same finite, bounded operational
    # contract.
    validate_render_request_resource_limits(
        plan,
        write_stems=write_stems,
        space=space,
        collaboration_mode=collaboration_mode,
        stem_cache_enabled=stem_cache_directory is not None,
    )
    render_arguments = {
        "write_stems": write_stems,
        "master_gain_db": master_gain_db,
        "normalize_peak_db": normalize_peak_db,
        "space": space,
        "collaboration_mode": collaboration_mode,
        "stem_cache_directory": stem_cache_directory,
        "refresh_stem_cache": refresh_stem_cache,
        "analysis_cache_directory": analysis_cache_directory,
        "_authoring_project_binding": normalized_authoring_binding,
        "_authoring_workflow_binding": normalized_workflow_binding,
        "_progress_callback": _progress_callback,
    }
    if not _acquire_output_lock:
        # Immutable-candidate publication already owns the stable final
        # candidate lock.  Its staging directory is freshly created and
        # private, so acquiring a second lock keyed by that random name would
        # add no exclusion while leaving an orphan sidecar after every render.
        return _render_plan_locked(
            plan,
            Path(output_directory).resolve(),
            **render_arguments,
        )
    with acquire_render_lock(output_directory) as ownership:
        return _render_plan_locked(
            plan,
            ownership.output_directory,
            **render_arguments,
        )


def suggested_master_gain_db(plan_peak: float, target_peak: float = 0.89) -> float:
    """How much master gain would land a known peak just under full scale."""

    import numpy as np

    if plan_peak <= 0.0:
        return 0.0
    return float(20.0 * np.log10(target_peak / plan_peak))
