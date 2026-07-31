"""生成首批协奏 fixture 的本机校准输入，并可选择生成计划或渲染。

默认行为只冻结 ``score.json``、``roster.json`` 和 ``metadata.json``。
``--plan-only`` 会额外解析输入并写出演奏计划，``--render`` 才会调用正式
合奏渲染器。所有产物都是临时本机校准材料；本工具不生成协奏验收矩阵，也不
写回任何乐器 manifest。除 ``--list`` 外，每次执行都会用本次选择替换生成器
拥有的完整输出快照；``--only`` 不是增量追加。
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.capability import load_capabilities
from tianlai.collaboration_fixtures import (
    build_fixture_documents,
    fixture_ids,
)
from tianlai.conductor import ExpressionSettings, build_plan
from tianlai.ensemble import PERFORMANCE_PLAN_NAME, render_plan
from tianlai.render_lock import acquire_render_lock
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document
from tianlai.space import SpaceConfig


DEFAULT_OUTPUT_ROOT = ROOT / "output" / "协奏校准"
INPUT_DIRECTORY_NAME = "输入"
RENDER_DIRECTORY_NAME = "渲染"
MANIFEST_NAME = "_清单.json"
LISTENING_ORDER_NAME = "_试听顺序.txt"
INPUT_FILENAMES = {
    "score": "score.json",
    "roster": "roster.json",
    "metadata": "metadata.json",
}
GENERATION_MODES = frozenset(("inputs", "plan-only", "render"))
GENERATOR_OWNED_DIRECTORY_NAMES = (
    INPUT_DIRECTORY_NAME,
    RENDER_DIRECTORY_NAME,
)
_WARNING_LABELS = {
    "balance_relation_outside_tolerance": "相对平衡偏离",
    "balance_relation_insufficient_overlap": "共同活动证据不足",
    "spectral_overlap_candidate": "频带重叠候选",
    "temporal_balance_drift_candidate": "段落平衡漂移",
    "mono_fold_cancellation_candidate": "单声道折叠候选",
    "space_tail_truncation_candidate": "空间尾音候选",
}


def _json_bytes(document: Any) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    """Write one complete file beside its destination, then replace it."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, document: Any) -> str:
    payload = _json_bytes(document)
    _write_bytes_atomic(path, payload)
    return hashlib.sha256(payload).hexdigest()


def _write_text_atomic(path: Path, text: str) -> str:
    payload = text.encode("utf-8")
    _write_bytes_atomic(path, payload)
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_fixture_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("fixture_id 必须是非空字符串")
    if (
        value in (".", "..")
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise ValueError(f"fixture_id 不能作为安全目录名：{value!r}")
    return value


def _catalogue_ids() -> tuple[str, ...]:
    raw = fixture_ids()
    if not isinstance(raw, tuple):
        raise TypeError("fixture_ids() 必须返回 tuple")
    identifiers = tuple(_safe_fixture_id(value) for value in raw)
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("fixture_ids() 不得包含重复项")
    return identifiers


def _selected_ids(only: str | None) -> tuple[str, ...]:
    identifiers = _catalogue_ids()
    if only is None:
        return identifiers
    requested = _safe_fixture_id(only)
    if requested not in identifiers:
        choices = "、".join(identifiers)
        raise ValueError(f"未知 --only {requested!r}；可选：{choices}")
    return (requested,)


def _split_fixture_document(
    requested_id: str,
    document: Any,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not isinstance(document, dict):
        raise TypeError(
            f"build_fixture_documents({requested_id!r}) 必须返回对象"
        )
    required = {
        "fixture_id",
        "family",
        "variant",
        "seed",
        "space",
        "master_gain_db",
        "normalize_peak_db",
        "score",
        "roster",
        "targets",
        "human_questions",
    }
    missing = sorted(required - set(document))
    if missing:
        raise ValueError(
            f"fixture {requested_id!r} 缺少字段：{', '.join(missing)}"
        )
    document_id = _safe_fixture_id(document["fixture_id"])
    if document_id != requested_id:
        raise ValueError(
            f"fixture 请求 {requested_id!r} 却返回 {document_id!r}"
        )
    if document["variant"] not in ("typical", "stress"):
        raise ValueError(
            f"fixture {requested_id!r} variant 必须是 typical 或 stress"
        )
    if (
        isinstance(document["seed"], bool)
        or not isinstance(document["seed"], int)
    ):
        raise ValueError(f"fixture {requested_id!r} seed 必须是整数")
    if document["normalize_peak_db"] is not None:
        raise ValueError(
            f"fixture {requested_id!r} 必须固定 normalize_peak_db=null"
        )
    if not isinstance(document["score"], dict):
        raise ValueError(f"fixture {requested_id!r} score 必须是对象")
    if not isinstance(document["roster"], dict):
        raise ValueError(f"fixture {requested_id!r} roster 必须是对象")
    if not isinstance(document["targets"], list):
        raise ValueError(f"fixture {requested_id!r} targets 必须是数组")
    if not isinstance(document["human_questions"], list):
        raise ValueError(
            f"fixture {requested_id!r} human_questions 必须是数组"
        )
    try:
        master_gain_db = float(document["master_gain_db"])
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"fixture {requested_id!r} master_gain_db 必须是有限数值"
        ) from error
    if not math.isfinite(master_gain_db):
        raise ValueError(
            f"fixture {requested_id!r} master_gain_db 必须是有限数值"
        )
    # Validate the declared space before any output is published.  The
    # resulting SpaceConfig is rebuilt during rendering so the metadata JSON
    # remains the source of truth.
    SpaceConfig.from_dict(document["space"])

    score = copy.deepcopy(document["score"])
    roster = copy.deepcopy(document["roster"])
    metadata = {
        key: copy.deepcopy(value)
        for key, value in document.items()
        if key not in ("score", "roster")
    }
    return score, roster, metadata


def _load_fixture_documents(
    identifiers: tuple[str, ...],
) -> tuple[tuple[dict[str, Any], dict[str, Any], dict[str, Any]], ...]:
    documents = tuple(
        _split_fixture_document(
            identifier,
            build_fixture_documents(identifier),
        )
        for identifier in identifiers
    )
    if len(identifiers) > 1:
        for offset in range(0, len(identifiers), 2):
            pair = documents[offset : offset + 2]
            if len(pair) != 2:
                raise ValueError("完整 fixture 目录必须由 typical/stress 成对组成")
            typical = pair[0][2]
            stress = pair[1][2]
            if (
                typical["variant"] != "typical"
                or stress["variant"] != "stress"
                or typical["family"] != stress["family"]
            ):
                raise ValueError(
                    "fixture_ids() 必须按同家族 typical→stress 成对排列"
                )
    return documents


def _resolved_artifact_file(
    path: Path,
    output_root: Path,
) -> tuple[Path, Path]:
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(output_root.resolve())
    except ValueError as error:
        raise ValueError(f"产物路径越出 output root：{resolved}") from error
    if not resolved.is_file():
        raise ValueError(f"预期产物不是普通文件：{resolved}")
    return resolved, relative


def _path_record(path: Path, output_root: Path) -> dict[str, str]:
    resolved, relative = _resolved_artifact_file(path, output_root)
    return {
        "path": relative.as_posix(),
        "sha256": _sha256_file(resolved),
    }


def _json_artifact_record(
    path: Path,
    output_root: Path,
    *,
    label: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Hash and parse the exact same on-disk JSON bytes."""

    resolved, relative = _resolved_artifact_file(path, output_root)
    payload = resolved.read_bytes()
    record = {
        "path": relative.as_posix(),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} 不是有效 UTF-8 JSON：{resolved}") from error
    if not isinstance(document, dict):
        raise ValueError(f"{label} 根必须是对象：{resolved}")
    return record, document


def _input_entry(
    identifier: str,
    metadata: dict[str, Any],
    records: dict[str, dict[str, str]],
) -> dict[str, Any]:
    return {
        "fixture_id": identifier,
        "family": copy.deepcopy(metadata["family"]),
        "variant": metadata["variant"],
        "targets": copy.deepcopy(metadata["targets"]),
        "human_questions": copy.deepcopy(metadata["human_questions"]),
        "render_settings": {
            "seed": metadata["seed"],
            "space": copy.deepcopy(metadata["space"]),
            "master_gain_db": float(metadata["master_gain_db"]),
            "normalize_peak_db": metadata["normalize_peak_db"],
        },
        "inputs": records,
        "duration_seconds": None,
        "machine_warnings": {
            "build_plan": [],
            "mix_report": [],
        },
        "plan": None,
        "render": None,
    }


def _write_inputs(
    output_root: Path,
    identifier: str,
    score: dict[str, Any],
    roster: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, dict[str, str]]:
    directory = output_root / INPUT_DIRECTORY_NAME / identifier
    directory.mkdir(parents=True, exist_ok=True)
    documents = {
        "score": score,
        "roster": roster,
        "metadata": metadata,
    }
    records: dict[str, dict[str, str]] = {}
    # Metadata is written last and acts as the per-fixture commit marker.
    # Invalidate an older marker before replacing either document it binds.
    (directory / INPUT_FILENAMES["metadata"]).unlink(missing_ok=True)
    for name in ("score", "roster", "metadata"):
        path = directory / INPUT_FILENAMES[name]
        digest = _write_json_atomic(path, documents[name])
        records[name] = {
            "path": path.relative_to(output_root).as_posix(),
            "sha256": digest,
        }
    return records


def _invalidate_root_publication(output_root: Path) -> None:
    """Remove the previous commit marker before changing its bound files."""

    # Unlinking one file is atomic on the supported local filesystems.  The
    # manifest goes first, so an interruption while removing the advisory
    # listening order can never leave an old marker pointing at new inputs.
    (output_root / MANIFEST_NAME).unlink(missing_ok=True)
    (output_root / LISTENING_ORDER_NAME).unlink(missing_ok=True)


def _validated_owned_directory(
    output_root: Path,
    name: str,
) -> Path:
    """Return one exact generator-owned child after strict containment checks."""

    if name not in GENERATOR_OWNED_DIRECTORY_NAMES:
        raise ValueError(f"拒绝清理非生成器目录：{name!r}")
    resolved_root = output_root.resolve(strict=True)
    literal = resolved_root / name
    if literal.parent != resolved_root or literal.name != name:
        raise ValueError(f"生成器目录不是 output root 的字面子目录：{literal}")
    if literal.is_symlink():
        raise ValueError(f"拒绝递归清理符号链接：{literal}")
    resolved = literal.resolve(strict=False)
    if resolved != literal:
        raise ValueError(
            f"生成器目录解析后越出字面子目录，拒绝清理：{literal} -> {resolved}"
        )
    if literal.exists() and not literal.is_dir():
        raise ValueError(f"生成器目录路径已存在但不是目录：{literal}")
    return literal


def _clear_generator_owned_directories(output_root: Path) -> None:
    """Converge only the literal ``输入`` and ``渲染`` subdirectories."""

    # Validate both targets before deleting either one.  A hostile link or an
    # unexpected file at the second path must not leave a half-cleaned tree.
    targets = tuple(
        _validated_owned_directory(output_root, name)
        for name in GENERATOR_OWNED_DIRECTORY_NAMES
    )
    for target in targets:
        if target.exists():
            # The resolved absolute target was checked above to be the exact
            # intended child of the resolved output root.
            shutil.rmtree(target)


def _build_performance_plan(
    score_document: dict[str, Any],
    roster_document: dict[str, Any],
    metadata: dict[str, Any],
    capabilities: Any,
) -> Any:
    score = parse_score_document(copy.deepcopy(score_document))
    roster = parse_roster_document(
        copy.deepcopy(roster_document),
        capabilities,
    )
    settings = ExpressionSettings.from_dict(
        {
            "mode": "ensemble",
            "range_mode": "compatibility",
            "humanize": {"seed": metadata["seed"]},
        }
    )
    return build_plan(score, roster, settings)


def _report_warnings_from_disk(
    result: Any,
    report: dict[str, Any],
) -> list[Any]:
    memory_report = getattr(result, "mix_report", None)
    if memory_report is not None and memory_report != report:
        raise ValueError("render_plan 内存 mix_report 与磁盘协奏诊断不一致")
    warnings = report.get("warnings")
    if not isinstance(warnings, list):
        raise ValueError("协奏诊断报告 warnings 必须是数组")
    return copy.deepcopy(warnings)


def _receipt_duration_seconds(
    result: Any,
    receipt: dict[str, Any],
) -> float:
    try:
        audio_format = receipt["audio_format"]
        mix = receipt["mix"]
        sample_rate = audio_format["sample_rate"]
        frame_count = mix["frame_count"]
    except (KeyError, TypeError) as error:
        raise ValueError(
            "渲染回执缺少 audio_format.sample_rate 或 mix.frame_count"
        ) from error
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or sample_rate <= 0
    ):
        raise ValueError("渲染回执 sample_rate 必须是正整数")
    if (
        isinstance(frame_count, bool)
        or not isinstance(frame_count, int)
        or frame_count < 0
    ):
        raise ValueError("渲染回执 frame_count 必须是非负整数")
    duration = frame_count / sample_rate

    result_sample_rate = getattr(result, "sample_rate", None)
    if result_sample_rate is not None and result_sample_rate != sample_rate:
        raise ValueError("渲染结果 sample_rate 与磁盘回执不一致")
    result_frame_count = getattr(result, "frame_count", None)
    if result_frame_count is not None and result_frame_count != frame_count:
        raise ValueError("渲染结果 frame_count 与磁盘回执不一致")
    result_duration = getattr(result, "duration_seconds", None)
    if result_duration is not None:
        if isinstance(result_duration, bool):
            raise ValueError("渲染结果 duration_seconds 必须是有限数值")
        try:
            result_duration_number = float(result_duration)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "渲染结果 duration_seconds 必须是有限数值"
            ) from error
        if (
            not math.isfinite(result_duration_number)
            or not math.isclose(
                result_duration_number,
                duration,
                rel_tol=1.0e-12,
                abs_tol=1.0e-12,
            )
        ):
            raise ValueError(
                "渲染结果 duration_seconds 与磁盘回执推导时长不一致"
            )
    return duration


def _write_plan_only(
    output_root: Path,
    identifier: str,
    plan: Any,
) -> dict[str, str]:
    directory = output_root / RENDER_DIRECTORY_NAME / identifier
    path = directory / PERFORMANCE_PLAN_NAME
    digest = _write_json_atomic(path, plan.to_dict())
    return {
        "path": path.relative_to(output_root).as_posix(),
        "sha256": digest,
    }


def _render_fixture(
    output_root: Path,
    identifier: str,
    plan: Any,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], list[Any], float]:
    directory = (output_root / RENDER_DIRECTORY_NAME / identifier).resolve()
    space = SpaceConfig.from_dict(copy.deepcopy(metadata["space"]))
    result = render_plan(
        plan,
        directory,
        write_stems=False,
        master_gain_db=float(metadata["master_gain_db"]),
        normalize_peak_db=metadata["normalize_peak_db"],
        space=space,
        collaboration_mode=None,
    )
    receipt_path_value = getattr(result, "receipt_path", None)
    report_path_value = getattr(result, "mix_report_path", None)
    mix_path_value = getattr(result, "mix_path", None)
    if not receipt_path_value:
        raise ValueError(f"fixture {identifier!r} 渲染后没有回执")
    if not report_path_value:
        raise ValueError(f"fixture {identifier!r} 渲染后没有协奏诊断报告")
    if not mix_path_value:
        raise ValueError(f"fixture {identifier!r} 渲染后没有合奏 WAV")
    receipt_path = Path(receipt_path_value)
    report_path = Path(report_path_value)
    mix_path = Path(mix_path_value)
    receipt_record, receipt = _json_artifact_record(
        receipt_path,
        output_root,
        label="渲染回执",
    )
    report_record, report = _json_artifact_record(
        report_path,
        output_root,
        label="协奏诊断报告",
    )
    artifacts = {
        "status": "rendered",
        "directory": directory.relative_to(output_root.resolve()).as_posix(),
        "receipt": receipt_record,
        "mix_report": report_record,
        "wav": _path_record(mix_path, output_root),
    }
    duration = _receipt_duration_seconds(result, receipt)
    warnings = _report_warnings_from_disk(result, report)
    return artifacts, warnings, duration


def _listening_order(
    entries: list[dict[str, Any]],
    *,
    mode: str,
) -> str:
    lines = [
        "天籁首批协奏 fixture 试听顺序",
        "顺序：每个家族 typical 后紧跟 stress；--only 时只列所选项。",
        "用途：临时本机口径校准，不是协奏验收矩阵，不写回任何乐器 manifest。",
        "机器提示只负责分流排查，不等于听感失败；完整证据见 _清单.json 与各协奏报告。",
        f"本次模式：{mode}",
        "",
    ]
    for order, entry in enumerate(entries, start=1):
        lines.append(
            f"{order:02d}. [{entry['family']}] "
            f"{entry['variant']} | {entry['fixture_id']}"
        )
        lines.append("    目标：")
        for target in entry["targets"]:
            role = target["role"]
            lines.append(
                "      - "
                f"{target['instrument_path']} | "
                f"{role['function']}/{role['prominence']}"
            )
        lines.append("    请听：")
        for question in entry["human_questions"]:
            lines.append(f"      - {question}")
        duration = entry["duration_seconds"]
        lines.append(
            "    时长："
            + (
                "未构建"
                if duration is None
                else f"{float(duration):.3f} s"
            )
        )
        render = entry["render"]
        if render is not None:
            lines.append(f"    WAV：{render['wav']['path']}")
            lines.append(f"    回执：{render['receipt']['path']}")
            lines.append(f"    协奏报告：{render['mix_report']['path']}")
        elif entry["plan"] is not None:
            lines.append(
                f"    音频：未渲染（plan-only）；计划：{entry['plan']['path']}"
            )
        else:
            lines.append("    音频：未渲染（inputs only）")
        machine_warnings = entry["machine_warnings"]
        if duration is None:
            lines.append("    机器提示：未运行")
        elif not (
            machine_warnings["build_plan"]
            or machine_warnings["mix_report"]
        ):
            lines.append("    机器提示：无")
        else:
            summaries: list[str] = []
            build_warnings = machine_warnings["build_plan"]
            if build_warnings:
                summaries.append(f"计划 warning {len(build_warnings)}")
            counts: dict[str, int] = {}
            for warning in machine_warnings["mix_report"]:
                code = (
                    str(warning.get("code", "unknown"))
                    if isinstance(warning, dict)
                    else "unknown"
                )
                counts[code] = counts.get(code, 0) + 1
            for code in sorted(counts):
                label = _WARNING_LABELS.get(code, code)
                summaries.append(f"{label} {counts[code]}")
            lines.append(f"    机器提示：{'；'.join(summaries)}")
    return "\n".join(lines) + "\n"


def _publish_root_files(
    output_root: Path,
    manifest: dict[str, Any],
    *,
    listening_order: str,
) -> None:
    """Bind the order file into the manifest and publish the marker last."""

    order_path = output_root / LISTENING_ORDER_NAME
    manifest_path = output_root / MANIFEST_NAME
    try:
        _write_text_atomic(order_path, listening_order)
        order_record = _path_record(order_path, output_root)
        manifest["listening_order"] = order_record
        _write_json_atomic(manifest_path, manifest)
        # Detect a writer that changed the advisory order while the final
        # marker was being published.  Withdraw both files instead of leaving
        # a manifest whose recorded hash is already false.
        if _path_record(order_path, output_root) != order_record:
            raise RuntimeError("试听顺序在最终清单发布期间发生变化")
    except BaseException:
        # A patched/failing writer may raise after replacing the destination,
        # so remove the marker explicitly rather than assuming it was absent.
        manifest_path.unlink(missing_ok=True)
        order_path.unlink(missing_ok=True)
        raise


def generate_calibration(
    *,
    output_root: str | Path | None = None,
    only: str | None = None,
    mode: str = "inputs",
) -> dict[str, Any]:
    """Generate one complete local calibration manifest.

    The root manifest is the final commit marker.  It is written only after
    every selected input and requested plan/render artifact has succeeded.
    """

    if mode not in GENERATION_MODES:
        raise ValueError(
            f"mode 必须是 {', '.join(sorted(GENERATION_MODES))}"
        )
    root = Path(
        DEFAULT_OUTPUT_ROOT if output_root is None else output_root
    ).resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"output root 已存在但不是目录：{root}")

    # Catalogue construction and capability loading are read-only.  Finish
    # them before taking ownership so invalid requests leave no lock sidecar
    # and concurrent callers hold the publication lock only while needed.
    identifiers = _selected_ids(only)
    documents = _load_fixture_documents(identifiers)
    capabilities: Any = None
    if mode != "inputs":
        capabilities = load_capabilities(ROOT / "乐器")

    with acquire_render_lock(root):
        return _generate_calibration_owned(
            output_root=root,
            mode=mode,
            identifiers=identifiers,
            documents=documents,
            capabilities=capabilities,
        )


def _generate_calibration_owned(
    *,
    output_root: Path,
    mode: str,
    identifiers: tuple[str, ...],
    documents: tuple[
        tuple[dict[str, Any], dict[str, Any], dict[str, Any]],
        ...,
    ],
    capabilities: Any,
) -> dict[str, Any]:
    """Generate while holding exclusive ownership of ``output_root``."""

    output_root.mkdir(parents=True, exist_ok=True)

    _invalidate_root_publication(output_root)
    _clear_generator_owned_directories(output_root)
    entries: list[dict[str, Any]] = []
    for identifier, (score, roster, metadata) in zip(
        identifiers,
        documents,
        strict=True,
    ):
        records = _write_inputs(
            output_root,
            identifier,
            score,
            roster,
            metadata,
        )
        entry = _input_entry(identifier, metadata, records)
        if mode != "inputs":
            plan = _build_performance_plan(
                score,
                roster,
                metadata,
                capabilities,
            )
            entry["duration_seconds"] = float(plan.duration_seconds)
            entry["machine_warnings"]["build_plan"] = [
                str(warning) for warning in plan.warnings
            ]
            if mode == "plan-only":
                entry["plan"] = _write_plan_only(
                    output_root,
                    identifier,
                    plan,
                )
            else:
                render, report_warnings, duration = _render_fixture(
                    output_root,
                    identifier,
                    plan,
                    metadata,
                )
                entry["plan"] = _path_record(
                    output_root
                    / RENDER_DIRECTORY_NAME
                    / identifier
                    / PERFORMANCE_PLAN_NAME,
                    output_root,
                )
                entry["render"] = render
                entry["duration_seconds"] = duration
                entry["machine_warnings"]["mix_report"] = report_warnings
        entries.append(entry)

    manifest: dict[str, Any] = {
        "format": "tianlai.collaboration_calibration_manifest",
        "version": 1,
        "scope": "temporary_local_calibration_only",
        "artifact_persistence": "temporary",
        "fixture_count": len(entries),
        "mode": mode,
        "ordering": "catalogue_family_order_typical_then_stress",
        "acceptance_matrix_written": False,
        "instrument_manifests_modified": False,
        "fixtures": entries,
        "notice": (
            "本清单只索引本机临时校准输入和产物；它不是协奏验收矩阵，"
            "不得据此提升 collaboration_review_status。"
        ),
    }
    # Listening order is advisory; the JSON manifest binds its disk hash and
    # remains the root-level commit marker for this run.
    _publish_root_files(
        output_root,
        manifest,
        listening_order=_listening_order(entries, mode=mode),
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "生成首批协奏 fixture 的临时本机校准输入；"
            "默认不加载音源也不渲染。"
        )
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--render",
        action="store_true",
        help="逐份调用正式 build_plan/render_plan 并记录回执、报告和 WAV Hash。",
    )
    action.add_argument(
        "--plan-only",
        action="store_true",
        help="解析输入并只写演奏计划，不渲染音频。",
    )
    parser.add_argument(
        "--only",
        metavar="FIXTURE_ID",
        help=(
            "用一个精确 fixture_id 替换生成器拥有的完整输出快照；"
            "不是增量追加。"
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="按试听顺序列出 fixture_id，不写任何文件。",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=f"输出根目录（默认 {DEFAULT_OUTPUT_ROOT}）。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.list_only:
            identifiers = _selected_ids(arguments.only)
        else:
            mode = (
                "render"
                if arguments.render
                else ("plan-only" if arguments.plan_only else "inputs")
            )
            manifest = generate_calibration(
                output_root=arguments.output_root,
                only=arguments.only,
                mode=mode,
            )
    except (OSError, TypeError, ValueError, RuntimeError) as error:
        parser.error(str(error))

    if arguments.list_only:
        for identifier in identifiers:
            print(identifier)
        return 0

    print(
        f"已生成 {manifest['fixture_count']} 份协奏校准 {mode} 产物："
        f"{Path(arguments.output_root).resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
