"""Ensemble rendering: one stem per executor, then a deterministic mix.

Stems are rendered **sequentially**, not by advancing every instrument
together.  Three reasons, in order of weight:

* the sample libraries total well over 8 GB, so holding a full orchestra in
  memory at once is not realistic while holding one instrument always is;
* every stem stays byte-reproducible on its own, which lets the optional raw
  stem cache reuse an unchanged instrument performance without disturbing
  any other part;
* it reuses the single-instrument path that the 103 instruments were already
  audited against, so nothing downstream of ``create_instrument`` changes.

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

from dataclasses import dataclass, replace
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import tempfile
from typing import Any
import warnings

from .audio import write_wav_pcm24
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
from .renderer import render_document
from .render_lock import acquire_render_lock
from .resource_limits import validate_render_request_resource_limits
from .roster import CollaborationSettings
from .stem_cache import (
    PROCESS_SOURCE_TREE_SHA256,
    StemCache,
    build_cache_key,
    current_source_tree_matches,
)
from .stereo_stage_metrics import analyze_stereo_stage


RENDER_RECEIPT_VERSION = 2
RENDER_RECEIPT_NAME = "渲染回执.json"
PERFORMANCE_PLAN_NAME = "演奏计划.json"
CACHE_TELEMETRY_NAME = "缓存遥测.json"
CACHE_TELEMETRY_FORMAT = "tianlai.render_cache_telemetry"
CACHE_TELEMETRY_VERSION = 1
RAW_STEM_CACHE_STAGE = "raw_instrument_render_pre_assignment_gain_v1"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_RENDER_ARTIFACT_NAMES = (
    PERFORMANCE_PLAN_NAME,
    "分轨",
    "合奏.wav",
    ENSEMBLE_LICENSE_SIDECAR_NAME,
    ENSEMBLE_ATTRIBUTION_NAME,
    MIX_REPORT_NAME,
    CACHE_TELEMETRY_NAME,
    RENDER_RECEIPT_NAME,
)


def _finite_float(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{label} 必须是有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} 必须是有限数值")
    return number


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
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_private_render_directory(path: Path, parent: Path, prefix: str) -> None:
    """Delete only a renderer-owned staging/backup directory.

    This guard is deliberately strict because cleanup runs on exception paths.
    It must never turn a malformed output path into a recursive delete outside
    the selected output parent.
    """

    resolved_parent = parent.resolve()
    resolved = path.resolve()
    if resolved.parent != resolved_parent or not resolved.name.startswith(prefix):
        raise RuntimeError(f"拒绝清理非渲染器私有目录: {resolved}")
    if resolved.exists():
        shutil.rmtree(resolved)


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


def _verify_render_generation(directory: Path) -> None:
    """Verify every published file hash before a generation is accepted."""

    receipt_path = directory / RENDER_RECEIPT_NAME
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("渲染回执不存在、不可读或不是合法 JSON") from exc
    if not isinstance(receipt, dict):
        raise RuntimeError("渲染回执顶层必须是对象")
    if (
        receipt.get("format") != "tianlai.render_receipt"
        or receipt.get("version") != RENDER_RECEIPT_VERSION
    ):
        raise RuntimeError("渲染回执格式或版本不受支持")

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
    _verify_cache_telemetry(directory, receipt)


def verify_render_generation(directory: str | Path) -> None:
    """Verify every receipt-bound artifact in one render generation.

    This public boundary is shared by the renderer and the immutable-candidate
    publisher.  Keeping one verifier prevents the outer publication layer from
    accepting a receipt whose plan, mix, stems, licence sidecar, attribution
    notice, or collaboration report changed after the renderer returned.
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
    backup_prefix = f".{final.name}.render-backup."
    backup = Path(tempfile.mkdtemp(dir=parent, prefix=backup_prefix))

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
            raise RuntimeError(
                "渲染发布失败且回滚不完整；旧产物备份保留在 "
                f"{backup}: " + "; ".join(rollback_errors)
            )
        raise
    finally:
        if cleanup_backup:
            try:
                _remove_private_render_directory(backup, parent, backup_prefix)
            except OSError as exc:
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
        db = base_gain_db + np.interp(
            frame_times,
            times,
            offsets,
            left=offsets[0],
            right=offsets[-1],
        )
        buffer[start:end] *= np.power(10.0, db / 20.0)[:, np.newaxis]


@dataclass(frozen=True, slots=True)
class _RawStemCacheIdentity:
    key: str
    manifest_sha256: str
    frame_count: int


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
    if not lookup.hit or lookup.record is None or lookup.audio is None:
        return False
    metadata = lookup.record.metadata
    return (
        metadata.get("stage") == RAW_STEM_CACHE_STAGE
        and metadata.get("sample_rate") == sample_rate
        and metadata.get("frame_count") == identity.frame_count
        and metadata.get("manifest_sha256")
        == identity.manifest_sha256
        and tuple(lookup.audio.shape) == (identity.frame_count, 2)
    )


def _render_part_cached(
    part: Any,
    sample_rate: int,
    *,
    cache: StemCache,
    refresh: bool,
    runtime_fingerprints: dict[
        tuple[str, str, int, str],
        str,
    ],
    summary: dict[str, Any],
) -> tuple[Any, int, str]:
    """Reuse one raw stem when every live input still matches."""

    if not summary["active"]:
        _note_cache_result(summary, "bypassed", "session_disabled")
        return _render_part(part, sample_rate)
    if not current_source_tree_matches():
        summary["active"] = False
        _note_cache_result(
            summary,
            "bypassed",
            "producer_source_changed_restart_required",
        )
        return _render_part(part, sample_rate)

    try:
        identity = _raw_stem_cache_identity(
            part,
            sample_rate,
            runtime_fingerprints,
        )
    except Exception:
        # Fingerprinting is a safety gate, not a new render dependency.
        # External manifests or unavailable evidence simply take the audited
        # uncached path; they are never allowed a weak cache identity.
        _note_cache_result(
            summary,
            "bypassed",
            "live_identity_unavailable",
        )
        return _render_part(part, sample_rate)

    if refresh:
        _note_cache_result(summary, "misses", "refresh_requested")
    else:
        lookup = cache.load(identity.key)
        if _cache_lookup_matches(lookup, identity, sample_rate):
            assert lookup.audio is not None
            assert lookup.record is not None
            _note_cache_result(summary, "hits", "verified_hit")
            return (
                lookup.audio,
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

    buffer, peak_voices, manifest_sha256 = _render_part(
        part,
        sample_rate,
    )
    if manifest_sha256 != identity.manifest_sha256:
        _note_cache_result(
            summary,
            "write_skips",
            "manifest_changed_during_render",
        )
        return buffer, peak_voices, manifest_sha256
    if not current_source_tree_matches():
        summary["active"] = False
        _note_cache_result(
            summary,
            "write_skips",
            "producer_source_changed_during_render",
        )
        return buffer, peak_voices, manifest_sha256
    try:
        post_render_identity = _raw_stem_cache_identity(
            part,
            sample_rate,
            {},
        )
    except Exception:
        summary["active"] = False
        _note_cache_result(
            summary,
            "write_skips",
            "live_identity_recheck_unavailable",
        )
        return buffer, peak_voices, manifest_sha256
    if post_render_identity.key != identity.key:
        summary["active"] = False
        _note_cache_result(
            summary,
            "write_skips",
            "live_identity_changed_during_render",
        )
        return buffer, peak_voices, manifest_sha256

    stored = cache.store(
        identity.key,
        buffer,
        stage=RAW_STEM_CACHE_STAGE,
        sample_rate=sample_rate,
        peak_voices=peak_voices,
        manifest_sha256=manifest_sha256,
    )
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
    return buffer, peak_voices, manifest_sha256


def _render_part(part: Any, sample_rate: int) -> tuple[Any, int, str]:
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
        frames, peak = render_document(instrument, document)
        buffer = np.empty((document.total_samples, 2), dtype=np.float32)
        for index, (left, right) in enumerate(frames):
            buffer[index, 0] = left
            buffer[index, 1] = right
        return buffer, peak[0], manifest_sha256
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()


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
    bus = np.zeros((total_frames, 2), dtype=np.float64)
    # 共享厅堂保留每条干分轨的左右相位。各声部按座位距离送入同一条
    # 立体声总线，渲染完统一加回合奏——分轨本身仍是全干、可复算。
    send_bus = (
        np.zeros((total_frames, 2), dtype=np.float32)
        if space is not None
        else None
    )
    stems: list[StemResult] = []
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
            # Keep scratch beside, not inside, the private render generation.
            # The render-loop finally block closes Windows memmaps immediately
            # on failure; keeping the unique scratch beside staging also lets
            # staging cleanup proceed independently.
            scratch_parent=directory.parent,
            cache_directory=analysis_cache_directory,
            expected_stem_count=len(plan.parts),
        )
        if collaboration.mode in ("analyze", "suggest")
        else None
    )
    mix_report: dict[str, Any] | None = None
    mix_report_path: Path | None = None
    analysis_cache_summary: dict[str, Any] | None = None

    render_loop_completed = False
    try:
        for part in plan.parts:
            if stem_cache is None or stem_cache_summary is None:
                buffer, peak_voices, manifest_sha256 = _render_part(
                    part,
                    plan.sample_rate,
                )
            else:
                buffer, peak_voices, manifest_sha256 = (
                    _render_part_cached(
                        part,
                        plan.sample_rate,
                        cache=stem_cache,
                        refresh=bool(refresh_stem_cache),
                        runtime_fingerprints=runtime_fingerprints,
                        summary=stem_cache_summary,
                    )
                )
            manifest_sha256_by_executor[
                part.executor.executor_id
            ] = manifest_sha256
            # 增益在写盘之前施加。分轨是后置增益、前置声像的信号:这样它反映的
            # 就是它在合奏里的电平,让总线不过载的那个增益同样让分轨不过载,而
            # 分轨自己的立体声像仍然完整,方便拿去重新混。
            apply_gain_envelope(
                buffer,
                plan.sample_rate,
                part.executor.gain_db,
                part.gain_envelope,
            )
            stem_peak = (
                float(np.max(np.abs(buffer))) if buffer.size else 0.0
            )
            if not math.isfinite(stem_peak):
                raise ValueError(
                    f"分轨 {part.executor.executor_id!r} 产生了非有限样本"
                )
            if stem_peak > 1.0:
                headroom = 20.0 * np.log10(stem_peak)
                raise ValueError(
                    f"分轨 {part.executor.executor_id!r} "
                    f"过载:峰值 {stem_peak:.4f}"
                    f"(超出 {headroom:+.2f} dB)。"
                    "写盘会被静默削平,因此拒绝输出。"
                    f"请把该声部的 gain_db 从 "
                    f"{part.executor.gain_db:.1f} 降到 "
                    f"{part.executor.gain_db - headroom:.1f} 或更低"
                )
            if mix_report_builder is not None:
                mix_report_builder.add_stem(part.executor, buffer)
            stem_path: str | None = None
            if write_stems:
                target = (
                    stem_directory / f"{part.executor.executor_id}.wav"
                )
                write_wav_pcm24(
                    target,
                    (
                        (float(row[0]), float(row[1]))
                        for row in buffer
                    ),
                    plan.sample_rate,
                )
                stem_path = str(target)

            left_gain, right_gain = balance_gains(part.executor.pan)
            length = min(total_frames, buffer.shape[0])
            bus[:length, 0] += buffer[:length, 0] * left_gain
            bus[:length, 1] += buffer[:length, 1] * right_gain
            if send_bus is not None:
                # 送入厅堂用后置增益的干声(声像之前),越远的座位送得越湿,
                # 让远处乐器听起来更靠里——直达声电平仍由 gain_db 决定,
                # 不重复衰减。
                send_scale = space.send_scale(
                    part.executor.seat.distance_m
                )
                send_bus[:length] += buffer[:length] * send_scale
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
        render_loop_completed = True
    finally:
        if (
            not render_loop_completed
            and mix_report_builder is not None
        ):
            mix_report_builder.close()
    if stem_cache_summary is not None:
        _finalize_stem_cache_summary(stem_cache_summary)

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
        and float(np.max(np.abs(send_bus))) > 0.0
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

    post_space_stage_metrics = (
        analyze_stereo_stage(
            bus,
            plan.sample_rate,
            tail_window_seconds=(
                TAIL_ANALYSIS_SECONDS if space is not None else None
            ),
        ).to_dict()
        if mix_report is not None
        else None
    )

    bus *= 10.0 ** (master_gain_db / 20.0)
    mix_peak = float(np.max(np.abs(bus))) if bus.size else 0.0
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
            mix_peak = float(np.max(np.abs(bus)))
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
    frame_count = write_wav_pcm24(
        mix_path,
        ((float(row[0]), float(row[1])) for row in bus),
        plan.sample_rate,
    )

    stem_receipts: list[dict[str, Any]] = []
    instrument_uses: list[InstrumentUse] = []
    audio_artifacts = [
        AudioArtifact(
            role="mix",
            path=mix_path,
            label=mix_path.relative_to(directory).as_posix(),
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
            wav_receipt = {
                "written": True,
                "path": stem_path.relative_to(directory).as_posix(),
                "sha256": _sha256_file(stem_path),
            }
            audio_artifacts.append(
                AudioArtifact(
                    role=f"stem:{stem.executor_id}",
                    path=stem_path,
                    label=stem_path.relative_to(directory).as_posix(),
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
            "sha256": _sha256_file(mix_path),
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
    }
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
) -> EnsembleResult:
    final_directory = Path(output_directory)
    parent = final_directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    staging_prefix = f".{final_directory.name}.render-stage."
    with tempfile.TemporaryDirectory(
        dir=parent,
        prefix=staging_prefix,
    ) as temporary_name:
        staging = Path(temporary_name)
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
        )
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
        _publish_render_artifacts(staging, final_directory)
        return published


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
    # This gate runs before the lock creates its parent directory and before
    # NumPy allocates a full-length mix bus.  Validation and rendering therefore
    # agree on the same finite, bounded operational contract.
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
