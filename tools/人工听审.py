"""为 103 件乐器生成、外发、填写、核验和汇总分层人工听审批次。

这是一个本地、无 UI 的轻量工作流。它不会改动乐器清单或 ``试听核验.json``，
也不会自动修改任何乐器状态。每条回答同时绑定批次 Hash 与当前 WAV
SHA-256；源 WAV、参考音频或批次内容变化后，旧回答会被判为 stale 并从汇总中
排除。

常用命令::

    python tools/人工听审.py create --layer technical --output 人工听审/技术层
    python tools/人工听审.py export-package --batch .../batch.json \
        --output .../发给听审者.zip
    python tools/人工听审.py import-response --batch-root 人工听审/技术层 \
        --response .../返回回答.json
    python tools/人工听审.py start --batch .../batch.json --output .../张三.json \
        --reviewer reviewer-zhang --role general_listener \
        --environment headphones --device "有线耳机"
    python tools/人工听审.py run --batch .../batch.json --response .../张三.json
    python tools/人工听审.py summary --batch-root 人工听审/技术层 \
        --responses 人工听审/技术层/responses --output 人工听审/技术层/summary.json
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sys
import tempfile
import zipfile
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
    canonical_json_sha256,
)


INSTRUMENT_ROOT_NAME = "乐器"
TEST_TOOL = "测试工具/参考振荡器"
SCHEMA_ROOT = ROOT / "schemas"

BATCH_SCHEMA = SCHEMA_ROOT / "listening-review-batch.schema.json"
RESPONSE_SCHEMA = SCHEMA_ROOT / "listening-review-response.schema.json"
ASSET_MAP_SCHEMA = SCHEMA_ROOT / "listening-review-assets.schema.json"
SUMMARY_SCHEMA = SCHEMA_ROOT / "listening-review-summary.schema.json"
OFFLINE_TEMPLATE = Path(__file__).with_name("人工听审离线模板.html")

HEX64 = re.compile(r"^[0-9a-f]{64}$")
UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
STATUS_CHOICES = ("pass", "reject", "unsure", "not_applicable")
ROLE_CHOICES = (
    "general_listener",
    "reference_listener",
    "instrument_expert",
    "review_lead",
)
REFERENCE_KINDS = {"reference", "anchor"}
OFFLINE_AUXILIARY_KINDS = {
    "reference": "reference",
    "anchor": "anchor",
    "context": "context",
    "previous_version": "previous-version",
}
OFFLINE_AUDIO_SUFFIXES = {
    ".wav",
    ".flac",
    ".mp3",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
}
STRICT_REFERENCE_LICENSES = {
    "CC0-1.0",
    "CC-BY-3.0",
    "CC-BY-4.0",
    "project-generated",
    "private-review-authorized",
}


class ReviewError(RuntimeError):
    """The review workflow cannot safely continue."""


def _question(
    question_id: str,
    title: str,
    acceptance_statement: str,
    listen_for: str,
    not_applicable_when: str,
    *,
    requires_reference: bool = False,
    requires_expert: bool = False,
    requires_context: bool = False,
) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "title": title,
        "acceptance_statement": acceptance_statement,
        "listen_for": listen_for,
        "not_applicable_when": not_applicable_when,
        "requires_reference": requires_reference,
        "requires_expert": requires_expert,
        "requires_context": requires_context,
    }


QUESTIONNAIRES: dict[str, dict[str, Any]] = {
    "technical": {
        "title": "普通听众技术缺陷筛查",
        "description": (
            "只判断可直接听见的故障，不要求知道该乐器应当有多像，也不要回答"
            "“是否100%还原”。"
        ),
        "allowed_roles": list(ROLE_CHOICES),
        "minimum_independent_reviewers": 2,
        "target_identity_visible": False,
        "questions": [
            _question(
                "tech_clicks_pops",
                "爆音与数字失真",
                "没有听到非演奏意图的 click、pop、削波或数字破裂声。",
                "起音、松键、踏板/奏法切换和片段衔接处的瞬时尖响。",
                "永不因乐器种类而天然不适用；听不确定请选 unsure。",
            ),
            _question(
                "tech_start_tail",
                "起音与尾音完整",
                "起音没有被切掉，尾音没有突然截断或不自然消失。",
                "首音攻击、短音末端、长音松键、自然衰减与循环结束。",
                "谱例完全没有可判断的起音或尾音时。",
            ),
            _question(
                "tech_dropouts_jumps",
                "静音、掉音与突变",
                "没有异常静音、掉音、声道跳变或突发音量/音色阶跃。",
                "音阶跨区、弱强切换、左右声道和连续长音中途。",
                "谱例没有跨区或连续段可判断时。",
            ),
            _question(
                "tech_repetition",
                "机械重复",
                "重复音或循环没有明显的机器枪式复制感、卡住感或周期接缝。",
                "连续同音、快速短音、滚奏、循环长音和重复噪声纹理。",
                "谱例没有重复音或循环可判断时。",
            ),
            _question(
                "tech_unwanted_noise",
                "异常噪声与刺耳感",
                "没有与演奏无关的刺耳噪声、异常底噪、直流跳变或数码杂质。",
                "静段、释音后、弱奏和高频瞬态；真实按键/呼吸噪声不自动算缺陷。",
                "声音本身就是噪声类拟音且无法区分异常噪声时。",
            ),
            _question(
                "tech_stereo",
                "声像稳定",
                "左右声像没有无意的瞬移、相位空洞或单边消失。",
                "戴耳机比较连续音符、长尾和多个力度段的左右位置。",
                "确认素材或目标本来就是单声道/刻意移动声像时。",
            ),
        ],
    },
    "identity": {
        "title": "参考音频辅助的音色身份核验",
        "description": (
            "必须先听许可与Hash都已记录的参考音频；判断身份和主要音色特征，"
            "不要求逐波形相同，也不问“100%像不像”。"
        ),
        "allowed_roles": [
            "reference_listener",
            "instrument_expert",
            "review_lead",
        ],
        "minimum_independent_reviewers": 2,
        "target_identity_visible": True,
        "questions": [
            _question(
                "identity_family",
                "乐器身份",
                "与参考相比，目标能被合理识别为所声明的同一种乐器或声音类别。",
                "发声机制、频谱重心、攻击形状、共鸣与尾音，而不是录音室空间。",
                "没有可用参考音频时必须选 not_applicable。",
                requires_reference=True,
            ),
            _question(
                "identity_register",
                "跨音域一致性",
                "低、中、高音区仍保持该乐器身份，没有明显变成另一类音色。",
                "音阶跨区、最高/最低音、根采样切换和大幅移调区域。",
                "谱例或参考没有覆盖可比音域时。",
                requires_reference=True,
            ),
            _question(
                "identity_dynamics",
                "动态与尾音特征",
                "弱强变化、攻击与尾音的总体性格与参考相容。",
                "弱奏是否只是变小、强奏是否异常变亮，以及释放/共鸣长度。",
                "目标或参考没有可比的弱强/尾音片段时。",
                requires_reference=True,
            ),
        ],
    },
    "expert": {
        "title": "乐器族专家真实性裁决",
        "description": (
            "只由声明了相应乐器族经验的专家填写；判断奏法标签、物理行为、"
            "音域与控制语义是否可信。没有专家结论时不能据此判定协作/语境验收通过。"
        ),
        "allowed_roles": ["instrument_expert", "review_lead"],
        "minimum_independent_reviewers": 1,
        "target_identity_visible": True,
        "questions": [
            _question(
                "expert_articulation",
                "奏法真实性",
                "试听中宣称的奏法听感与名称相符，没有用另一奏法或包络近似冒充。",
                "弓法、击法、制音、滚奏、连断、拨奏、颤音和特殊技法。",
                "该入口没有多奏法且谱例不涉及奏法真实性时。",
                requires_expert=True,
            ),
            _question(
                "expert_range",
                "音域与跨区行为",
                "公开音域、移调区和跨根采样区的音色/响应仍在可接受物理范围。",
                "最低/最高音、宽移调、音区换挡、非自然共振峰和错误八度。",
                "无固定音高或没有可定义乐器音域的声音。",
                requires_expert=True,
            ),
            _question(
                "expert_dynamics_rr",
                "力度、重复音与控制",
                "力度层、Round Robin、expression/modulation 等行为与乐器演奏逻辑相容。",
                "层切换、机器枪效应、虚构力度、风箱/弓压/气流或槌击响应。",
                "该声音没有力度、重复或连续控制语义时。",
                requires_expert=True,
            ),
            _question(
                "expert_release_resonance",
                "释音、循环与共鸣",
                "释音、踏板、制音、循环和共鸣行为符合该乐器的基本物理逻辑。",
                "松键噪声、循环接缝、无限延音、踏板抽象和共鸣建立/消失。",
                "纯一次性拟音且没有相关行为时。",
                requires_expert=True,
            ),
        ],
    },
    "context": {
        "title": "合奏或场景语境复核",
        "description": (
            "必须提供包含目标乐器的合奏/场景音频；判断它进入真实使用语境后"
            "是否出现独奏片段听不出的平衡、遮蔽或尾音问题。"
        ),
        "allowed_roles": [
            "reference_listener",
            "instrument_expert",
            "review_lead",
        ],
        "minimum_independent_reviewers": 2,
        "target_identity_visible": True,
        "questions": [
            _question(
                "context_role",
                "声部可辨与角色",
                "目标在合奏/场景中能承担预期角色，没有不自然地消失或凸出。",
                "旋律可辨性、低频支撑、节奏攻击、氛围层与前后景关系。",
                "没有包含目标的语境音频时必须选 not_applicable。",
                requires_context=True,
            ),
            _question(
                "context_balance",
                "动态与频段平衡",
                "弱强变化和频段占用不会导致明显失衡或异常遮蔽。",
                "强奏刺出、弱奏消失、低频堆积、高频尖锐和声像拥挤。",
                "语境片段没有可比较的动态或重叠声部时。",
                requires_context=True,
            ),
            _question(
                "context_tail_noise",
                "尾音与噪声累积",
                "多声部叠加后，尾音、循环和底噪没有累积成明显浑浊或伪影。",
                "密集段、休止前后、长释放、循环纹理和多个实例叠加。",
                "语境片段没有密集或休止段可判断时。",
                requires_context=True,
            ),
        ],
    },
}


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not UTC_TIMESTAMP.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        return None
    return parsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(document: dict[str, Any], *, omit: str) -> str:
    payload = {key: value for key, value in document.items() if key != omit}
    return canonical_json_sha256(payload)


def read_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise ReviewError(f"无法读取 JSON：{path}: {error}") from error
    if not isinstance(document, dict):
        raise ReviewError(f"JSON 根节点必须是对象：{path}")
    return document


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def resolve_project_path(project_root: Path, label: str) -> Path:
    root = project_root.resolve()
    candidate = Path(label)
    resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ReviewError(f"路径越出项目根目录：{label}")
    return resolved


def project_relative(project_root: Path, path: Path) -> str:
    resolved_root = project_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ReviewError(f"文件不在项目根目录内：{path}")
    return resolved.relative_to(resolved_root).as_posix()


def _declared_event_path(
    project_root: Path,
    report: dict[str, Any],
) -> Path:
    label = report.get("events")
    if not isinstance(label, str) or not label.strip():
        raise ReviewError("试听报告缺少 events")
    path = resolve_project_path(project_root, label)
    examples_root = (project_root / "examples").resolve()
    if not path.is_relative_to(examples_root):
        raise ReviewError(f"试听 events 越出 examples：{label}")
    if not path.is_file():
        raise ReviewError(f"试听 events 不存在：{label}")
    return path


def _report_identity_bindings(
    report: dict[str, Any],
    *,
    manifest_path: Path,
    events_path: Path,
) -> dict[str, str | None]:
    """Validate one current report and expose canonical plus legacy bindings.

    New reports bind parsed JSON semantics.  A pre-migration report may still
    be read, but only after its historical source-byte hashes match the exact
    current files; callers always receive canonical identities and therefore
    never create another raw-JSON batch.
    """

    actual_manifest_canonical = canonical_json_file_sha256(manifest_path)
    actual_events_canonical = canonical_json_file_sha256(events_path)
    canonical_fields = (
        "hash_algorithm",
        "canonicalization",
        "manifest_canonical_sha256",
        "events_canonical_sha256",
    )
    has_any_canonical = any(field in report for field in canonical_fields)
    legacy_manifest: str | None = None
    legacy_events: str | None = None

    if has_any_canonical:
        missing = [field for field in canonical_fields if field not in report]
        if missing:
            raise ReviewError(
                "试听报告的规范化 JSON 身份字段不完整："
                + "、".join(missing)
            )
        if report["hash_algorithm"] != HASH_ALGORITHM:
            raise ReviewError("试听报告 hash_algorithm 不受支持")
        if report["canonicalization"] != CANONICALIZATION:
            raise ReviewError("试听报告 canonicalization 不受支持")
        if (
            report["manifest_canonical_sha256"]
            != actual_manifest_canonical
        ):
            raise ReviewError(
                "当前manifest Hash已变化（manifest_canonical_sha256 已过期）"
            )
        if report["events_canonical_sha256"] != actual_events_canonical:
            raise ReviewError(
                "当前events Hash已变化（events_canonical_sha256 已过期）"
            )
    else:
        legacy_manifest = str(report.get("manifest_sha256", ""))
        legacy_events = str(report.get("events_sha256", ""))
        if not HEX64.fullmatch(legacy_manifest) or not HEX64.fullmatch(
            legacy_events
        ):
            raise ReviewError("试听报告缺少可验证的 JSON 身份字段")
        if sha256_file(manifest_path) != legacy_manifest:
            raise ReviewError("旧 manifest_sha256 已过期")
        if sha256_file(events_path) != legacy_events:
            raise ReviewError("旧 events_sha256 已过期")

    migration = report.get("identity_migration")
    if migration is not None:
        if not isinstance(migration, dict):
            raise ReviewError("试听报告 identity_migration 必须是对象")
        expected_header = {
            "status": "superseded_by_canonical_json_v1",
            "hash_algorithm": HASH_ALGORITHM,
            "hash_semantics": "source-file-bytes",
        }
        for field, expected in expected_header.items():
            if migration.get(field) != expected:
                raise ReviewError(
                    f"试听报告 identity_migration.{field} 无效"
                )
        migrated_manifest = str(migration.get("manifest_sha256", ""))
        migrated_events = str(migration.get("events_sha256", ""))
        if not HEX64.fullmatch(migrated_manifest) or not HEX64.fullmatch(
            migrated_events
        ):
            raise ReviewError("试听报告 identity_migration Hash 无效")
        if not has_any_canonical and (
            migrated_manifest != legacy_manifest
            or migrated_events != legacy_events
        ):
            raise ReviewError(
                "试听报告 identity_migration 与旧顶层 Hash 不一致"
            )
        legacy_manifest = migrated_manifest
        legacy_events = migrated_events

    return {
        "hash_algorithm": HASH_ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "manifest_canonical_sha256": actual_manifest_canonical,
        "events_canonical_sha256": actual_events_canonical,
        "legacy_manifest_sha256": legacy_manifest,
        "legacy_events_sha256": legacy_events,
    }


def discover_review_sources(
    project_root: Path,
    *,
    only: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Load current reports and refuse stale/missing source evidence."""

    root = project_root.resolve()
    instrument_root = root / INSTRUMENT_ROOT_NAME
    prefixes = tuple(item.strip().replace("\\", "/").strip("/") for item in only if item.strip())
    failures: list[str] = []
    sources: list[dict[str, Any]] = []

    for manifest_path in sorted(
        instrument_root.rglob("乐器.json"),
        key=lambda path: path.relative_to(instrument_root).as_posix().casefold(),
    ):
        directory = manifest_path.parent
        relative = directory.relative_to(instrument_root).as_posix()
        if relative == TEST_TOOL:
            continue
        if prefixes and not any(
            relative == prefix or relative.startswith(prefix + "/")
            for prefix in prefixes
        ):
            continue

        try:
            report_path = directory / "试听核验.json"
            manifest = read_json(manifest_path)
            report = read_json(report_path)
            wav_label = report.get("wav")
            if not isinstance(wav_label, str) or not wav_label:
                raise ReviewError("试听报告缺少 wav")
            wav_path = resolve_project_path(root, wav_label)
            if not wav_path.is_file():
                raise ReviewError(f"试听 WAV 不存在：{wav_label}")
            wav_hash = sha256_file(wav_path)
            if wav_hash != report.get("wav_sha256"):
                raise ReviewError("试听 WAV SHA-256 与报告不一致")
            events_path = _declared_event_path(root, report)
            identity = _report_identity_bindings(
                report,
                manifest_path=manifest_path,
                events_path=events_path,
            )
            if int(report.get("clipped_samples", -1)) != 0:
                raise ReviewError("试听报告包含削波，不应进入人工批次")
            coverage = report.get("coverage")
            if not isinstance(coverage, list) or not coverage or not all(
                isinstance(item, str) and item.strip() for item in coverage
            ):
                raise ReviewError("coverage 必须是非空字符串数组")

            sources.append(
                {
                    "instrument_path": relative,
                    "instrument_name": directory.name,
                    "category": " / ".join(Path(relative).parts[:-1]) or "未分类",
                    "family": family_key(relative),
                    "implementation_type": str(manifest.get("type", "")),
                    "license_status": str(
                        manifest.get("license_status", "project_root_license_pending")
                    ),
                    "source_wav": project_relative(root, wav_path),
                    "wav_sha256": wav_hash,
                    "report_path": project_relative(root, report_path),
                    "report_sha256": sha256_file(report_path),
                    "hash_algorithm": identity["hash_algorithm"],
                    "canonicalization": identity["canonicalization"],
                    "manifest_canonical_sha256": identity[
                        "manifest_canonical_sha256"
                    ],
                    "events_canonical_sha256": identity[
                        "events_canonical_sha256"
                    ],
                    "duration_seconds": float(report.get("duration_seconds", 0.0)),
                    "sample_rate": int(report.get("sample_rate", 0)),
                    "channels": int(report.get("channels", 0)),
                    "subtype": str(report.get("subtype", "")),
                    "coverage": list(coverage),
                }
            )
        except (KeyError, TypeError, ValueError, ReviewError) as error:
            failures.append(f"{relative}: {error}")

    if failures:
        raise ReviewError("试听工件不满足批次前置条件：\n" + "\n".join(failures))
    if not sources:
        raise ReviewError("筛选后没有可听审的正式乐器")
    return sources


def family_key(relative: str) -> str:
    parts = Path(relative).parts
    if parts and parts[0] == "管弦乐" and len(parts) >= 2:
        return "/".join(parts[:2])
    return parts[0] if parts else "未分类"


def load_asset_map(
    path: Path | None,
    project_root: Path,
) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    document = read_json(path)
    schema_issues = _schema_errors(document, ASSET_MAP_SCHEMA)
    if schema_issues:
        raise ReviewError(
            "参考/语境资产表不符合 Schema：\n" + "\n".join(schema_issues)
        )
    if document.get("schema_version") != 1:
        raise ReviewError("参考/语境资产表 schema_version 必须为 1")
    raw_items = document.get("items")
    if not isinstance(raw_items, dict):
        raise ReviewError("参考/语境资产表 items 必须是对象")

    result: dict[str, list[dict[str, Any]]] = {}
    for instrument_path, raw_assets in raw_items.items():
        if not isinstance(instrument_path, str) or not isinstance(raw_assets, list):
            raise ReviewError("资产表必须是 乐器相对路径 -> 资产数组")
        prepared: list[dict[str, Any]] = []
        for raw in raw_assets:
            if not isinstance(raw, dict):
                raise ReviewError(f"{instrument_path} 的资产必须是对象")
            kind = str(raw.get("kind", ""))
            if kind not in {"reference", "anchor", "context", "previous_version"}:
                raise ReviewError(f"{instrument_path} 的资产 kind 无效：{kind}")
            license_name = str(raw.get("license", ""))
            if license_name not in STRICT_REFERENCE_LICENSES:
                raise ReviewError(
                    f"{instrument_path} 的听审资产许可不在白名单：{license_name}"
                )
            notes = str(raw.get("notes", "")).strip()
            if license_name == "private-review-authorized":
                if kind != "context":
                    raise ReviewError(
                        f"{instrument_path} 的 private-review-authorized "
                        "只允许用于 context 语境资产"
                    )
            asset_path = resolve_project_path(project_root, str(raw.get("path", "")))
            if not asset_path.is_file():
                raise ReviewError(f"听审资产不存在：{asset_path}")
            prepared.append(
                {
                    "kind": kind,
                    "label": str(raw.get("label", "")).strip(),
                    "path": project_relative(project_root, asset_path),
                    "sha256": sha256_file(asset_path),
                    "source": str(raw.get("source", "")).strip(),
                    "license": license_name,
                    "notes": notes,
                }
            )
            if not prepared[-1]["label"] or not prepared[-1]["source"]:
                raise ReviewError(f"{instrument_path} 的资产缺 label 或 source")
        result[instrument_path.replace("\\", "/").strip("/")] = prepared
    return result


def stratified_order(
    sources: list[dict[str, Any]],
    rng: random.Random,
) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        buckets.setdefault(str(source["family"]), []).append(source)
    keys = list(buckets)
    rng.shuffle(keys)
    for values in buckets.values():
        rng.shuffle(values)

    ordered: list[dict[str, Any]] = []
    while keys:
        next_keys: list[str] = []
        for key in keys:
            bucket = buckets[key]
            if bucket:
                ordered.append(bucket.pop())
            if bucket:
                next_keys.append(key)
        keys = next_keys
        if len(keys) > 1:
            shift = rng.randrange(len(keys))
            keys = keys[shift:] + keys[:shift]
    return ordered


def balanced_sizes(total: int, minimum: int, maximum: int) -> list[int]:
    if total <= 0:
        return []
    if total <= maximum:
        return [total]
    lower = math.ceil(total / maximum)
    upper = max(lower, total // minimum)
    for count in range(lower, upper + 1):
        base, remainder = divmod(total, count)
        if base >= minimum and base + (1 if remainder else 0) <= maximum:
            return [base + 1] * remainder + [base] * (count - remainder)
    # 9–11 件等小集合无法严格拆成 6–8；保留单批并在 plan 中明确超出建议。
    return [total]


def chunk_by_sizes(
    values: list[dict[str, Any]],
    sizes: list[int],
) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    offset = 0
    for size in sizes:
        chunks.append(values[offset : offset + size])
        offset += size
    if offset != len(values):
        raise AssertionError("批次分片没有完整消费输入")
    return chunks


def _prepare_chunks(
    sources: list[dict[str, Any]],
    *,
    grouping: str,
    rng: random.Random,
    minimum: int,
    maximum: int,
) -> list[tuple[str, list[dict[str, Any]]]]:
    if grouping == "stratified_mixed":
        ordered = stratified_order(sources, rng)
        return [
            ("mixed", chunk)
            for chunk in chunk_by_sizes(
                ordered,
                balanced_sizes(len(ordered), minimum, maximum),
            )
        ]
    if grouping != "family":
        raise ReviewError(f"未知 grouping：{grouping}")

    families: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        families.setdefault(str(source["family"]), []).append(source)
    chunks: list[tuple[str, list[dict[str, Any]]]] = []
    for family in sorted(families):
        values = families[family]
        rng.shuffle(values)
        for chunk in chunk_by_sizes(
            values,
            balanced_sizes(len(values), minimum, maximum),
        ):
            chunks.append((family, chunk))
    return chunks


def _materialize_audio(source: Path, destination: Path, mode: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if mode == "hardlink":
        try:
            os.link(source, destination)
        except OSError as error:
            raise ReviewError(
                f"无法为 {source} 创建硬链接；可改用 --materialize copy 或 none：{error}"
            ) from error
    elif mode == "copy":
        shutil.copy2(source, destination)
    else:
        raise ReviewError(f"未知 materialize 模式：{mode}")


def _batch_digest(
    layer: str,
    seed: int,
    index: int,
    sources: list[dict[str, Any]],
) -> str:
    payload = {
        "layer": layer,
        "seed": seed,
        "index": index,
        "items": [
            [source["instrument_path"], source["wav_sha256"]]
            for source in sources
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]


def create_review_plan(
    project_root: Path,
    output_root: Path,
    *,
    layer: str,
    seed: int = 20260725,
    minimum_batch_size: int = 6,
    maximum_batch_size: int = 8,
    grouping: str = "stratified_mixed",
    materialize: str = "hardlink",
    asset_map_path: Path | None = None,
    only: Iterable[str] = (),
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a transactionally published set of blinded review batches."""

    if layer not in QUESTIONNAIRES:
        raise ReviewError(f"未知听审层：{layer}")
    if not 1 <= minimum_batch_size <= maximum_batch_size <= 20:
        raise ReviewError("批次大小必须满足 1 <= minimum <= maximum <= 20")
    if materialize not in {"hardlink", "copy", "none"}:
        raise ReviewError("materialize 必须是 hardlink、copy 或 none")

    project_root = project_root.resolve()
    output_root = output_root.resolve()
    if output_root.exists():
        raise ReviewError(f"输出目录已存在；为防止覆盖评审记录，请换新目录：{output_root}")
    output_root.parent.mkdir(parents=True, exist_ok=True)

    sources = discover_review_sources(project_root, only=only)
    asset_map = load_asset_map(asset_map_path, project_root)
    skipped: list[str] = []
    eligible: list[dict[str, Any]] = []
    for source in sources:
        assets = copy.deepcopy(asset_map.get(str(source["instrument_path"]), []))
        source = copy.deepcopy(source)
        source["references"] = [
            asset for asset in assets if asset["kind"] in REFERENCE_KINDS
        ]
        source["context_audio"] = [
            asset for asset in assets if asset["kind"] == "context"
        ]
        source["previous_versions"] = [
            asset for asset in assets if asset["kind"] == "previous_version"
        ]
        if layer == "identity" and not source["references"]:
            skipped.append(str(source["instrument_path"]))
            continue
        if layer == "context" and not source["context_audio"]:
            skipped.append(str(source["instrument_path"]))
            continue
        eligible.append(source)
    if not eligible:
        raise ReviewError(
            f"{layer} 层没有同时满足资产与许可要求的可审乐器"
        )

    rng = random.Random(seed)
    chunks = _prepare_chunks(
        eligible,
        grouping=grouping,
        rng=rng,
        minimum=minimum_batch_size,
        maximum=maximum_batch_size,
    )
    created = created_at or utc_now()
    questionnaire = QUESTIONNAIRES[layer]
    staging = Path(
        tempfile.mkdtemp(prefix=".人工听审-", dir=output_root.parent)
    )
    batch_entries: list[dict[str, Any]] = []

    try:
        for batch_index, (family_scope, chunk) in enumerate(chunks, 1):
            digest = _batch_digest(layer, seed, batch_index, chunk)
            batch_id = f"{layer}-{batch_index:03d}-{digest}"
            batch_dir = staging / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            items: list[dict[str, Any]] = []

            for order, source in enumerate(chunk, 1):
                source_wav = resolve_project_path(
                    project_root, str(source["source_wav"])
                )
                blind_label = f"样本-{order:02d}"
                if materialize == "none":
                    playback_origin = "project"
                    playback_wav = str(source["source_wav"])
                else:
                    relative_playback = Path("audio") / f"{order:02d}.wav"
                    _materialize_audio(
                        source_wav,
                        batch_dir / relative_playback,
                        materialize,
                    )
                    if (
                        sha256_file(batch_dir / relative_playback)
                        != source["wav_sha256"]
                    ):
                        raise ReviewError(f"{blind_label} 物化后 Hash 改变")
                    playback_origin = "batch"
                    playback_wav = relative_playback.as_posix()

                item_id = f"I{order:02d}-{str(source['wav_sha256'])[:10]}"
                items.append(
                    {
                        "item_id": item_id,
                        "order": order,
                        "blind_label": blind_label,
                        "instrument_path": source["instrument_path"],
                        "instrument_name": source["instrument_name"],
                        "category": source["category"],
                        "family": source["family"],
                        "implementation_type": source["implementation_type"],
                        "source_wav": source["source_wav"],
                        "playback_origin": playback_origin,
                        "playback_wav": playback_wav,
                        "wav_sha256": source["wav_sha256"],
                        "report_path": source["report_path"],
                        "report_sha256": source["report_sha256"],
                        "hash_algorithm": source["hash_algorithm"],
                        "canonicalization": source["canonicalization"],
                        "manifest_canonical_sha256": source[
                            "manifest_canonical_sha256"
                        ],
                        "events_canonical_sha256": source[
                            "events_canonical_sha256"
                        ],
                        "duration_seconds": source["duration_seconds"],
                        "sample_rate": source["sample_rate"],
                        "channels": source["channels"],
                        "subtype": source["subtype"],
                        "coverage": source["coverage"],
                        "references": source["references"],
                        "context_audio": source["context_audio"],
                        "previous_versions": source["previous_versions"],
                    }
                )

            batch: dict[str, Any] = {
                "$schema": "https://tianlai.local/schemas/listening-review-batch.schema.json",
                "schema_version": 2,
                "kind": "listening_review_batch",
                "batch_id": batch_id,
                "batch_sha256": "0" * 64,
                "created_at": created,
                "layer": layer,
                "questionnaire_version": 1,
                "title": questionnaire["title"],
                "description": questionnaire["description"],
                "randomization": {
                    "seed": seed,
                    "grouping": grouping,
                    "family_scope": family_scope,
                    "blind_order": True,
                },
                "review_policy": {
                    "allowed_roles": list(questionnaire["allowed_roles"]),
                    "minimum_independent_reviewers": int(
                        questionnaire["minimum_independent_reviewers"]
                    ),
                    "target_identity_visible": bool(
                        questionnaire["target_identity_visible"]
                    ),
                    "estimated_minutes": round(len(items) * 3.5),
                    "status_vocabulary": list(STATUS_CHOICES),
                    "non_pass_comment_required": True,
                    "automatic_collaboration_promotion": False,
                },
                "questions": copy.deepcopy(questionnaire["questions"]),
                "items": items,
            }
            batch["batch_sha256"] = canonical_sha256(
                batch, omit="batch_sha256"
            )
            write_json_atomic(batch_dir / "batch.json", batch)
            _write_play_order(batch_dir / "播放顺序.txt", batch)
            batch_entries.append(
                {
                    "batch_id": batch_id,
                    "path": (
                        Path("batches") / batch_id / "batch.json"
                    ).as_posix(),
                    "batch_sha256": batch["batch_sha256"],
                    "item_count": len(items),
                    "family_scope": family_scope,
                    "estimated_minutes": batch["review_policy"][
                        "estimated_minutes"
                    ],
                    "recommended_size_met": (
                        minimum_batch_size <= len(items) <= maximum_batch_size
                    ),
                }
            )

        plan = {
            "schema_version": 2,
            "kind": "listening_review_plan",
            "created_at": created,
            "layer": layer,
            "seed": seed,
            "grouping": grouping,
            "materialize": materialize,
            "source_instrument_count": len(sources),
            "included_instrument_count": len(eligible),
            "skipped_without_required_assets": skipped,
            "recommended_batch_size": [
                minimum_batch_size,
                maximum_batch_size,
            ],
            "batch_count": len(batch_entries),
            "batches": batch_entries,
            "responses_directory": "responses",
            "notice": (
                "批次不会修改乐器或试听报告；所有结论必须经 validate/summary "
                "复核当前WAV Hash。"
            ),
        }
        write_json_atomic(staging / "plan.json", plan)
        (staging / "responses").mkdir()
        os.replace(staging, output_root)
        return plan
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _write_play_order(path: Path, batch: dict[str, Any]) -> None:
    reveal = bool(batch["review_policy"]["target_identity_visible"])
    lines = [
        f"{batch['title']} — {batch['batch_id']}",
        f"预计 {batch['review_policy']['estimated_minutes']} 分钟；"
        "请保持播放设备与音量不变，允许重复听。",
        "pass=符合验收陈述；reject=听到缺陷；unsure=无法确定；"
        "not_applicable=谱例/材料不适用。",
        "",
    ]
    for item in batch["items"]:
        identity = (
            f" — {item['instrument_path']}" if reveal else ""
        )
        lines.append(
            f"{item['order']:02d}. {item['blind_label']}{identity}  "
            f"{item['playback_wav']}"
        )
    lines.extend(
        [
            "",
            "请用本工具 start + run/record 填写；不要直接修改 batch.json。",
            "普通技术层不要打开 batch.json 猜乐器名称，完成后再由协调者揭盲。",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_batch(path: Path) -> dict[str, Any]:
    batch = read_json(path)
    if batch.get("kind") != "listening_review_batch":
        raise ReviewError(f"不是听审批次：{path}")
    expected = canonical_sha256(batch, omit="batch_sha256")
    if batch.get("batch_sha256") != expected:
        raise ReviewError(f"批次自身 Hash 无效：{path}")
    return batch


def _schema_errors(document: dict[str, Any], schema_path: Path) -> list[str]:
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return []
    schema = read_json(schema_path)
    validator = Draft202012Validator(schema)
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(document),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"schema:{location}: {error.message}")
    return errors


def _batch_document_for_current_schema(
    batch: dict[str, Any],
) -> dict[str, Any]:
    """Return a validation-only v2 view without rewriting archived v1 batches."""

    if batch.get("schema_version") != 1:
        return batch
    compatible = copy.deepcopy(batch)
    compatible["schema_version"] = 2
    policy = compatible.get("review_policy")
    if isinstance(policy, dict):
        legacy = policy.pop("automatic_formal_promotion", None)
        policy["automatic_collaboration_promotion"] = legacy
    return compatible


def validate_batch(
    batch_path: Path,
    project_root: Path,
) -> list[str]:
    issues: list[str] = []
    try:
        batch = read_json(batch_path)
    except ReviewError as error:
        return [str(error)]
    issues.extend(
        _schema_errors(
            _batch_document_for_current_schema(batch),
            BATCH_SCHEMA,
        )
    )
    if batch.get("kind") != "listening_review_batch":
        issues.append("kind 不是 listening_review_batch")
        return issues
    expected = canonical_sha256(batch, omit="batch_sha256")
    if batch.get("batch_sha256") != expected:
        issues.append("stale:批次自身内容与 batch_sha256 不一致")

    item_ids: set[str] = set()
    orders: set[int] = set()
    for item in batch.get("items", []):
        item_id = str(item.get("item_id", ""))
        if item_id in item_ids:
            issues.append(f"重复 item_id：{item_id}")
        item_ids.add(item_id)
        try:
            order = int(item["order"])
        except (KeyError, TypeError, ValueError):
            issues.append(f"{item_id} order 无效")
            continue
        if order in orders:
            issues.append(f"重复 order：{order}")
        orders.add(order)
        try:
            instrument_root = (
                project_root / INSTRUMENT_ROOT_NAME
            ).resolve()
            instrument_dir = (
                instrument_root
                / Path(str(item["instrument_path"]))
            ).resolve()
            if not instrument_dir.is_relative_to(instrument_root):
                issues.append(f"{item_id} 乐器路径越出乐器根目录")
                continue
            manifest_path = instrument_dir / "乐器.json"
            if not manifest_path.is_file():
                issues.append(f"stale:{item_id} 当前乐器清单不存在")
                continue

            report_path = resolve_project_path(
                project_root, str(item["report_path"])
            )
            if not report_path.is_file():
                issues.append(f"stale:{item_id} 当前试听报告不存在")
                continue

            current_report = read_json(report_path)
            events_path = _declared_event_path(project_root, current_report)
            report_identity = _report_identity_bindings(
                current_report,
                manifest_path=manifest_path,
                events_path=events_path,
            )

            canonical_fields = (
                "hash_algorithm",
                "canonicalization",
                "manifest_canonical_sha256",
                "events_canonical_sha256",
            )
            legacy_fields = ("manifest_sha256", "events_sha256")
            has_canonical = any(field in item for field in canonical_fields)
            has_legacy = any(field in item for field in legacy_fields)
            complete_canonical = all(
                field in item for field in canonical_fields
            )
            complete_legacy = all(field in item for field in legacy_fields)

            if complete_canonical and not has_legacy:
                if item["hash_algorithm"] != HASH_ALGORITHM:
                    issues.append(
                        f"stale:{item_id} hash_algorithm 已变化"
                    )
                if item["canonicalization"] != CANONICALIZATION:
                    issues.append(
                        f"stale:{item_id} canonicalization 已变化"
                    )
                if (
                    item["manifest_canonical_sha256"]
                    != report_identity["manifest_canonical_sha256"]
                ):
                    issues.append(
                        f"stale:{item_id} 当前manifest Hash已变化"
                    )
                if (
                    item["events_canonical_sha256"]
                    != report_identity["events_canonical_sha256"]
                ):
                    issues.append(
                        f"stale:{item_id} 当前events Hash已变化"
                    )
            elif complete_legacy and not has_canonical:
                # Archived batches keep their original source-byte contract.
                # A migrated report explicitly carries those old identities
                # under identity_migration, so the schema-only report rewrite
                # does not make a valid historical batch look stale.
                if sha256_file(manifest_path) != item["manifest_sha256"]:
                    issues.append(
                        f"stale:{item_id} 当前manifest Hash已变化"
                    )
                if sha256_file(events_path) != item["events_sha256"]:
                    issues.append(
                        f"stale:{item_id} 当前events Hash已变化"
                    )
                if (
                    report_identity["legacy_manifest_sha256"]
                    != item["manifest_sha256"]
                ):
                    issues.append(
                        f"stale:{item_id} 试听报告的manifest_sha256已变化"
                    )
                if (
                    report_identity["legacy_events_sha256"]
                    != item["events_sha256"]
                ):
                    issues.append(
                        f"stale:{item_id} 试听报告的events_sha256已变化"
                    )
            else:
                issues.append(
                    f"{item_id}: JSON 身份字段不完整或混用新旧合同"
                )

            if current_report.get("wav_sha256") != item.get("wav_sha256"):
                issues.append(
                    f"stale:{item_id} 试听报告的wav_sha256已变化"
                )
            for field in (
                "duration_seconds",
                "sample_rate",
                "channels",
                "subtype",
                "coverage",
            ):
                if current_report.get(field) != item.get(field):
                    issues.append(
                        f"stale:{item_id} 试听报告的{field}已变化"
                    )

            source = resolve_project_path(
                project_root, str(item["source_wav"])
            )
            if not source.is_file():
                issues.append(f"stale:{item_id} 当前源WAV不存在")
            elif sha256_file(source) != item.get("wav_sha256"):
                issues.append(f"stale:{item_id} 当前源WAV Hash已变化")

            if item.get("playback_origin") == "batch":
                playback = (batch_path.parent / str(item["playback_wav"])).resolve()
                batch_directory = batch_path.parent.resolve()
                if not playback.is_relative_to(batch_directory):
                    issues.append(
                        f"{item_id} 播放文件路径越出批次目录"
                    )
                    continue
            else:
                playback = resolve_project_path(
                    project_root, str(item["playback_wav"])
                )
            if not playback.is_file():
                issues.append(f"stale:{item_id} 播放文件不存在")
            elif sha256_file(playback) != item.get("wav_sha256"):
                issues.append(f"stale:{item_id} 播放文件Hash已变化")

            for asset in (
                list(item.get("references", []))
                + list(item.get("context_audio", []))
                + list(item.get("previous_versions", []))
            ):
                asset_path = resolve_project_path(
                    project_root, str(asset["path"])
                )
                if not asset_path.is_file():
                    issues.append(
                        f"stale:{item_id} 听审资产不存在：{asset.get('label')}"
                    )
                elif sha256_file(asset_path) != asset.get("sha256"):
                    issues.append(
                        f"stale:{item_id} 听审资产Hash已变化：{asset.get('label')}"
                    )
        except ReviewError as error:
            issues.append(f"stale:{item_id} {error}")
        except KeyError as error:
            issues.append(f"{item_id}: {error}")
    return issues


def _batch_playback_path(
    batch_path: Path,
    project_root: Path,
    item: dict[str, Any],
) -> Path:
    if item.get("playback_origin") == "batch":
        batch_directory = batch_path.parent.resolve()
        playback = (
            batch_directory / str(item.get("playback_wav", ""))
        ).resolve()
        if not playback.is_relative_to(batch_directory):
            raise ReviewError(
                f"{item.get('item_id', '<unknown>')} 播放文件路径越出批次目录"
            )
        return playback
    return resolve_project_path(project_root, str(item.get("playback_wav", "")))


def _safe_filename_component(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .")
    if not cleaned:
        cleaned = fallback
    if cleaned.upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }:
        cleaned = f"_{cleaned}"
    return cleaned[:96].rstrip(" .") or fallback


def _assert_package_destination(package_root: Path, destination: Path) -> None:
    resolved_root = package_root.resolve()
    resolved_destination = destination.resolve()
    if not resolved_destination.is_relative_to(resolved_root):
        raise ReviewError(f"听审包目标路径越出包根目录：{destination}")


def _copy_frozen_audio(
    source: Path,
    destination: Path,
    *,
    package_root: Path,
    expected_sha256: str,
    label: str,
) -> dict[str, Any]:
    _assert_package_destination(package_root, destination)
    if not source.is_file():
        raise ReviewError(f"{label} 不存在：{source}")
    if sha256_file(source) != expected_sha256:
        raise ReviewError(f"{label} 的源文件 Hash 已变化")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    copied_sha256 = sha256_file(destination)
    if copied_sha256 != expected_sha256:
        raise ReviewError(f"{label} 复制进听审包后 Hash 改变")
    return {
        "path": destination.as_posix(),
        "sha256": copied_sha256,
    }


def _offline_json_for_html(document: dict[str, Any]) -> str:
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _offline_instructions(batch: dict[str, Any]) -> str:
    visible = bool(batch["review_policy"]["target_identity_visible"])
    lines = [
        f"{batch['title']} — {batch['batch_id']}",
        "",
        "重要：先把 ZIP 完整解压，再双击“天籁听审问卷.html”。",
        "不要直接在压缩包预览窗口中打开网页，否则浏览器可能找不到 audio 文件。",
        "本听审包不需要 Python、虚拟环境、网络或项目源码。",
        "",
        "操作：",
        "1. 使用固定设备和舒适音量，关闭空间音效、自动响度等增强。",
        "2. 按网页中的 01、02……顺序听；每段可以重复播放。",
        "3. 每个问题选择 通过、不通过、不确定或不适用。",
        "4. 非“通过”答案必须写时间点和现象，例如“00:12.30 尾音突然截断”。",
        "5. 听完后点“导出完整回答”，只把下载得到的 JSON 文件发回协调者。",
        "",
        f"预计用时：{batch['review_policy']['estimated_minutes']} 分钟",
        f"批次校验码：{batch['batch_sha256']}",
        "",
        "播放顺序：",
    ]
    for item in batch["items"]:
        identity = (
            f" — {item['instrument_path']}" if visible else ""
        )
        lines.append(
            f"{item['order']:02d}. {item['blind_label']}{identity}  "
            f"audio/{item['order']:02d}.wav  "
            f"约 {float(item['duration_seconds']):.1f} 秒"
        )
    lines.extend(
        [
            "",
            "SHA256SUMS.txt 供协调者核验包文件；网页不会声称已在浏览器内复算 WAV Hash。",
            "许可与署名.txt 随包保留来源与许可；它不对应播放编号，技术盲听请提交后再看。",
            "技术层请保持盲听，不要向协调者索要乐器名称后再作答。",
            "",
        ]
    )
    return "\n".join(lines)


def _manifest_for_review_item(
    project_root: Path,
    item: dict[str, Any],
) -> dict[str, Any]:
    instrument_root = (project_root / INSTRUMENT_ROOT_NAME).resolve()
    instrument_dir = (
        instrument_root / Path(str(item.get("instrument_path", "")))
    ).resolve()
    if not instrument_dir.is_relative_to(instrument_root):
        raise ReviewError(
            f"{item.get('item_id', '<unknown>')} 乐器路径越出乐器根目录"
        )
    manifest_path = instrument_dir / "乐器.json"
    if not manifest_path.is_file():
        raise ReviewError(f"乐器清单不存在：{manifest_path}")
    return read_json(manifest_path)


def _license_links(license_name: str) -> list[str]:
    normalized = license_name.upper().replace(" ", "-")
    links: list[str] = []
    mappings = (
        ("CC0", "https://creativecommons.org/publicdomain/zero/1.0/"),
        (
            "SAMPLING-PLUS-1.0",
            "https://creativecommons.org/licenses/sampling+/1.0/",
        ),
        ("CC-BY-3.0", "https://creativecommons.org/licenses/by/3.0/"),
        ("CC-BY-4.0", "https://creativecommons.org/licenses/by/4.0/"),
        (
            "CC-BY-SA-3.0",
            "https://creativecommons.org/licenses/by-sa/3.0/",
        ),
        (
            "CC-BY-SA-4.0",
            "https://creativecommons.org/licenses/by-sa/4.0/",
        ),
        (
            "CC-BY-NC-3.0",
            "https://creativecommons.org/licenses/by-nc/3.0/",
        ),
        (
            "CC-BY-NC-4.0",
            "https://creativecommons.org/licenses/by-nc/4.0/",
        ),
        ("GPL-3.0", "https://www.gnu.org/licenses/gpl-3.0.html"),
    )
    for marker, link in mappings:
        if marker in normalized and link not in links:
            links.append(link)
    return links


def _offline_attribution_notice(
    batch: dict[str, Any],
    project_root: Path,
) -> str:
    """Build one project-wide source pool shared by every offline package."""

    records: dict[tuple[Any, ...], dict[str, Any]] = {}
    instrument_root = project_root / INSTRUMENT_ROOT_NAME
    for manifest_path in sorted(instrument_root.rglob("乐器.json")):
        relative = manifest_path.parent.relative_to(instrument_root).as_posix()
        if relative == TEST_TOOL:
            continue
        manifest = read_json(manifest_path)
        license_status = str(
            manifest.get("license_status", "project_root_license_pending")
        )
        upstream = str(
            manifest.get("upstream")
            or "天籁项目自研程序生成声音"
        )
        origin = str(manifest.get("origin", ""))
        version = str(
            manifest.get("upstream_version")
            or manifest.get("upstream_commit")
            or ""
        )
        license_name = str(manifest.get("license", ""))
        if not license_name:
            license_name = (
                "项目自有测试渲染；项目根级公开发布许可证尚未确定"
            )
        evidence = manifest.get("evidence_files", [])
        if not isinstance(evidence, list):
            evidence = []
        evidence_names = tuple(
            sorted(
                {
                    Path(str(value)).name
                    for value in evidence
                    if str(value).strip()
                }
            )
        )
        key = (
            upstream,
            origin,
            version,
            license_name,
            license_status,
            evidence_names,
        )
        records[key] = {
            "upstream": upstream,
            "origin": origin,
            "version": version,
            "license": license_name,
            "license_status": license_status,
            "license_links": _license_links(license_name),
            "evidence": evidence_names,
        }

    contains_private_context = any(
        asset.get("license") == "private-review-authorized"
        for item in batch["items"]
        for asset in item.get("context_audio", [])
    )
    if contains_private_context:
        distribution_lines = [
            "本包含获准用于内部测试的第三方作品语境，只能交给项目负责人指定的",
            "听审者；请勿公开上传或继续转发。",
        ]
    else:
        distribution_lines = [
            "本包可用于项目组织的普通听众或大众质量审核；分享时请完整保留本文件。",
            "请勿把匿名试听 WAV 拆出并重新发布为独立样本库或素材包。",
        ]

    lines = [
        "天籁离线听审包：许可与署名",
        "",
        "本文件是整个 103 件乐器项目的通用来源池；所有技术层听审包使用同一份",
        "清单。某来源出现在这里，不表示当前批次使用了它，也不表示它与",
        "01.wav、02.wav……之间存在任何对应关系。",
        "技术盲听者请先提交回答，再阅读本文件。",
        "",
        "本包只含按固定谱例生成的短试听成品，不含原始采样、SFZ 或音源库。",
        *distribution_lines,
        "这里管理的是乐器渲染成品的来源许可；若以后使用有版权的内部测试曲目，",
        "曲目作品权与录音权仍须另行核对，不能由本清单替代。",
        "",
        "统一修改说明：由天籁音乐渲染内核按固定谱例渲染为 48 kHz PCM_24",
        "立体声 WAV；可能应用清单声明的重映射、调音、包络、力度、声像与",
        "确定性效果链，并按匿名顺序重命名。未修改或复制原始采样文件。",
        "",
        "全项目来源与许可池（不表示当前批次构成）：",
    ]
    ordered_records = sorted(
        records.values(),
        key=lambda value: (
            str(value["upstream"]).casefold(),
            str(value["origin"]).casefold(),
            str(value["license"]).casefold(),
        ),
    )
    for index, record in enumerate(ordered_records, 1):
        lines.extend(
            [
                "",
                f"{index}. 来源/作者或项目：{record['upstream']}",
                f"   许可状态：{record['license_status']}",
                f"   许可证：{record['license']}",
            ]
        )
        if record["origin"]:
            lines.append(f"   来源链接：{record['origin']}")
        if record["version"]:
            lines.append(f"   固定版本：{record['version']}")
        for link in record["license_links"]:
            lines.append(f"   许可证链接：{link}")
        if record["evidence"]:
            lines.append(
                "   项目内许可证据文件名："
                + "；".join(record["evidence"])
            )
    if batch["review_policy"]["target_identity_visible"]:
        auxiliary_records: set[tuple[str, str, str, str]] = set()
        for item in batch["items"]:
            for asset in (
                list(item.get("references", []))
                + list(item.get("context_audio", []))
                + list(item.get("previous_versions", []))
            ):
                auxiliary_records.add(
                    (
                        str(asset.get("label", "")),
                        str(asset.get("source", "")),
                        str(asset.get("license", "")),
                        str(asset.get("notes", "")),
                    )
                )
        if auxiliary_records:
            lines.extend(
                [
                    "",
                    "本批辅助听审材料（本层已显示乐器身份）：",
                ]
            )
            for index, (label, source, license_name, notes) in enumerate(
                sorted(auxiliary_records),
                1,
            ):
                lines.extend(
                    [
                        "",
                        f"{index}. 材料：{label}",
                        f"   来源：{source}",
                        f"   许可/授权：{license_name}",
                    ]
                )
                if notes:
                    lines.append(f"   使用说明：{notes}")
    lines.extend(["", "—— 许可说明结束 ——", ""])
    return "\n".join(lines)


def export_offline_package(
    batch_path: Path,
    output_path: Path,
    project_root: Path,
    *,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """Export one blinded, dependency-free listening package."""

    batch_path = batch_path.resolve()
    project_root = project_root.resolve()
    output_path = output_path.resolve()
    if output_path.exists():
        raise ReviewError(f"听审包输出已存在，拒绝覆盖：{output_path}")
    issues = validate_batch(batch_path, project_root)
    if issues:
        raise ReviewError("批次不可导出：\n" + "\n".join(issues))
    batch = load_batch(batch_path)
    if not OFFLINE_TEMPLATE.is_file():
        raise ReviewError(f"离线网页模板不存在：{OFFLINE_TEMPLATE}")
    template = OFFLINE_TEMPLATE.read_text(encoding="utf-8")
    marker = "__TIANLAI_OFFLINE_REVIEW_DATA__"
    if template.count(marker) != 1:
        raise ReviewError("离线网页模板的数据占位符必须恰好出现一次")
    attribution_notice = _offline_attribution_notice(batch, project_root)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=".听审包-", dir=output_path.parent)
    )
    archive_mode = output_path.suffix.casefold() == ".zip"
    requested_name = output_path.stem if archive_mode else output_path.name
    package_name = _safe_filename_component(
        requested_name,
        fallback=batch["batch_id"],
    )
    package_root = staging / package_name
    package_root.mkdir()
    visible = bool(batch["review_policy"]["target_identity_visible"])
    file_records: list[dict[str, Any]] = []
    public_items: list[dict[str, Any]] = []

    try:
        for item in batch["items"]:
            order = int(item["order"])
            target_relative = Path("audio") / f"{order:02d}.wav"
            target_source = _batch_playback_path(
                batch_path,
                project_root,
                item,
            )
            target_record = _copy_frozen_audio(
                target_source,
                package_root / target_relative,
                package_root=package_root,
                expected_sha256=str(item["wav_sha256"]),
                label=str(item["blind_label"]),
            )
            target_record["path"] = target_relative.as_posix()
            target_record["purpose"] = "target"
            file_records.append(target_record)

            public_item: dict[str, Any] = {
                "item_id": item["item_id"],
                "order": order,
                "blind_label": item["blind_label"],
                "playback_wav": target_relative.as_posix(),
                "wav_sha256": item["wav_sha256"],
                "duration_seconds": item["duration_seconds"],
                "sample_rate": item["sample_rate"],
                "channels": item["channels"],
                "coverage": list(item["coverage"]) if visible else [],
                "auxiliary_audio": [],
            }
            if visible:
                public_item["display_identity"] = item["instrument_path"]
                asset_groups = (
                    (
                        "reference",
                        REFERENCE_KINDS,
                        item["references"],
                    ),
                    (
                        "context",
                        {"context"},
                        item["context_audio"],
                    ),
                    (
                        "previous_version",
                        {"previous_version"},
                        item["previous_versions"],
                    ),
                )
                for purpose, expected_kinds, assets in asset_groups:
                    for asset_index, asset in enumerate(assets, 1):
                        asset_kind = str(asset.get("kind", ""))
                        if (
                            asset_kind not in OFFLINE_AUXILIARY_KINDS
                            or asset_kind not in expected_kinds
                        ):
                            raise ReviewError(
                                f"{item['blind_label']} 的辅助音频 kind 无效："
                                f"{asset_kind!r}（所在组 {purpose}）"
                            )
                        license_name = str(asset.get("license", ""))
                        if license_name not in STRICT_REFERENCE_LICENSES:
                            raise ReviewError(
                                f"{item['blind_label']} 的辅助音频许可不在白名单："
                                f"{license_name}"
                            )
                        asset_path = resolve_project_path(
                            project_root, str(asset["path"])
                        )
                        suffix = asset_path.suffix.casefold()
                        if suffix not in OFFLINE_AUDIO_SUFFIXES:
                            raise ReviewError(
                                f"{item['blind_label']} 的辅助音频扩展名不支持："
                                f"{suffix or '<none>'}"
                            )
                        file_token = OFFLINE_AUXILIARY_KINDS[asset_kind]
                        asset_relative = Path("audio") / (
                            f"{order:02d}-{file_token}-{asset_index:02d}{suffix}"
                        )
                        asset_record = _copy_frozen_audio(
                            asset_path,
                            package_root / asset_relative,
                            package_root=package_root,
                            expected_sha256=str(asset["sha256"]),
                            label=f"{item['blind_label']} / {asset['label']}",
                        )
                        asset_record["path"] = asset_relative.as_posix()
                        asset_record["purpose"] = asset_kind
                        file_records.append(asset_record)
                        public_item["auxiliary_audio"].append(
                            {
                                "kind": asset_kind,
                                "label": asset["label"],
                                "playback_audio": asset_relative.as_posix(),
                                "sha256": asset["sha256"],
                                "source": asset["source"],
                                "license": asset["license"],
                                "notes": asset["notes"],
                            }
                        )
            public_items.append(public_item)

        package_data = {
            "schema_version": 2,
            "kind": "listening_review_offline_package",
            "exported_at": exported_at or utc_now(),
            "batch_id": batch["batch_id"],
            "batch_sha256": batch["batch_sha256"],
            "layer": batch["layer"],
            "questionnaire_version": batch["questionnaire_version"],
            "title": batch["title"],
            "description": batch["description"],
            "review_policy": {
                "allowed_roles": batch["review_policy"]["allowed_roles"],
                "minimum_independent_reviewers": batch["review_policy"][
                    "minimum_independent_reviewers"
                ],
                "target_identity_visible": visible,
                "estimated_minutes": batch["review_policy"][
                    "estimated_minutes"
                ],
                "status_vocabulary": batch["review_policy"][
                    "status_vocabulary"
                ],
                "non_pass_comment_required": True,
                "automatic_collaboration_promotion": False,
            },
            "questions": copy.deepcopy(batch["questions"]),
            "items": public_items,
            "files": file_records,
        }
        html_text = template.replace(
            marker,
            _offline_json_for_html(package_data),
        )
        (package_root / "天籁听审问卷.html").write_text(
            html_text,
            encoding="utf-8",
        )
        (package_root / "使用说明.txt").write_text(
            _offline_instructions(batch),
            encoding="utf-8",
        )
        (package_root / "许可与署名.txt").write_text(
            attribution_notice,
            encoding="utf-8",
        )

        checksum_lines: list[str] = []
        for path in sorted(
            (
                candidate
                for candidate in package_root.rglob("*")
                if candidate.is_file()
            ),
            key=lambda candidate: candidate.relative_to(
                package_root
            ).as_posix(),
        ):
            relative = path.relative_to(package_root).as_posix()
            checksum_lines.append(f"{sha256_file(path)}  {relative}")
        (package_root / "SHA256SUMS.txt").write_text(
            "\n".join(checksum_lines) + "\n",
            encoding="utf-8",
        )

        if archive_mode:
            staged_archive = staging / f"{package_name}.zip"
            with zipfile.ZipFile(
                staged_archive,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=6,
            ) as archive:
                for path in sorted(
                    (
                        candidate
                        for candidate in package_root.rglob("*")
                        if candidate.is_file()
                    ),
                    key=lambda candidate: candidate.relative_to(
                        package_root
                    ).as_posix(),
                ):
                    archive.write(
                        path,
                        (
                            Path(package_name)
                            / path.relative_to(package_root)
                        ).as_posix(),
                    )
            os.replace(staged_archive, output_path)
        else:
            os.replace(package_root, output_path)

        return {
            "batch_id": batch["batch_id"],
            "batch_sha256": batch["batch_sha256"],
            "output": str(output_path),
            "archive": archive_mode,
            "item_count": len(public_items),
            "audio_file_count": len(file_records),
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def import_offline_response(
    batch_path: Path,
    response_path: Path,
    responses_root: Path,
    project_root: Path,
) -> Path:
    """Validate an offline response and atomically add it to the review root."""

    batch_path = batch_path.resolve()
    response_path = response_path.resolve()
    responses_root = responses_root.resolve()
    project_root = project_root.resolve()
    if not response_path.is_file():
        raise ReviewError(f"找不到待导入响应：{response_path}")
    if response_path.stat().st_size > 8 * 1024 * 1024:
        raise ReviewError("响应 JSON 超过 8 MiB，拒绝导入")
    batch_issues = validate_batch(batch_path, project_root)
    if batch_issues:
        raise ReviewError("当前批次不可导入响应：\n" + "\n".join(batch_issues))
    batch = load_batch(batch_path)
    response = read_json(response_path)
    issues = validate_response_document(
        batch,
        response,
        require_complete=True,
        check_schema=True,
    )
    if response.get("completion_status") != "complete":
        issues.append("导入只接受 completion_status=complete")
    if issues:
        raise ReviewError("离线响应无效：\n" + "\n".join(issues))

    encoded = json.dumps(
        response,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    response_digest = hashlib.sha256(encoded).hexdigest()
    destination = responses_root / (
        f"{batch['batch_id']}--{response_digest[:12]}.json"
    )

    if responses_root.exists() and not responses_root.is_dir():
        raise ReviewError(f"responses 目标不是目录：{responses_root}")
    if responses_root.is_dir():
        for existing_path in sorted(responses_root.rglob("*.json")):
            try:
                existing = read_json(existing_path)
            except ReviewError:
                continue
            if existing.get("kind") != "listening_review_response":
                continue
            if existing.get("response_id") == response.get("response_id"):
                if existing == response:
                    return existing_path
                raise ReviewError(
                    f"response_id 已存在但内容不同：{existing_path}"
                )
            existing_reviewer = existing.get("reviewer")
            if not isinstance(existing_reviewer, dict):
                existing_reviewer = {}
            response_reviewer = response.get("reviewer")
            if not isinstance(response_reviewer, dict):
                response_reviewer = {}
            if (
                existing.get("batch_id") == response.get("batch_id")
                and existing_reviewer.get("reviewer_id")
                == response_reviewer.get("reviewer_id")
            ):
                raise ReviewError(
                    "同一批次已导入该 reviewer_id 的另一份响应："
                    f"{existing_path}"
                )

    responses_root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        existing = read_json(destination)
        if existing == response:
            return destination
        raise ReviewError(f"目标响应文件已存在但内容不同：{destination}")
    write_json_atomic(destination, response)
    final_issues = validate_response(
        batch_path,
        destination,
        project_root,
        require_complete=True,
    )
    if final_issues:
        destination.unlink(missing_ok=True)
        raise ReviewError("导入后复核失败：\n" + "\n".join(final_issues))
    return destination


def locate_batch_for_offline_response(
    batch_root: Path,
    response_path: Path,
) -> Path:
    """Locate exactly one canonical batch using an offline response binding."""

    batch_root = batch_root.resolve()
    response_path = response_path.resolve()
    if not batch_root.is_dir():
        raise ReviewError(f"听审批次根目录不存在：{batch_root}")
    if not response_path.is_file():
        raise ReviewError(f"找不到待导入响应：{response_path}")
    if response_path.stat().st_size > 8 * 1024 * 1024:
        raise ReviewError("响应 JSON 超过 8 MiB，拒绝导入")
    response = read_json(response_path)
    batch_id = response.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id.strip():
        raise ReviewError("离线响应缺少有效 batch_id")
    batch_sha256 = response.get("batch_sha256")
    if not isinstance(batch_sha256, str) or not HEX64.fullmatch(batch_sha256):
        raise ReviewError("离线响应缺少有效 batch_sha256")
    id_matches: list[Path] = []
    exact_matches: list[Path] = []
    for candidate in sorted(batch_root.rglob("batch.json")):
        try:
            batch = load_batch(candidate)
        except ReviewError:
            continue
        if batch.get("batch_id") == batch_id:
            resolved = candidate.resolve()
            id_matches.append(resolved)
            if batch.get("batch_sha256") == batch_sha256:
                exact_matches.append(resolved)
    if len(exact_matches) == 1:
        return exact_matches[0]
    if not exact_matches and id_matches:
        raise ReviewError(
            f"找到 batch_id={batch_id}，但没有 batch_sha256="
            f"{batch_sha256} 的精确批次"
        )
    if len(exact_matches) != 1:
        raise ReviewError(
            f"batch_id={batch_id}、batch_sha256={batch_sha256} "
            f"在 {batch_root} 中匹配到 {len(exact_matches)} 个批次，"
            "要求恰好 1 个"
        )
    raise AssertionError("unreachable")


def start_response(
    batch_path: Path,
    output_path: Path,
    project_root: Path,
    *,
    reviewer_id: str,
    role: str,
    listening_environment: str,
    device: str,
    display_name: str = "",
    expertise: Iterable[str] = (),
    notes: str = "",
    started_at: str | None = None,
) -> dict[str, Any]:
    if output_path.exists():
        raise ReviewError(f"响应文件已存在，拒绝覆盖：{output_path}")
    batch_issues = validate_batch(batch_path, project_root)
    if batch_issues:
        raise ReviewError("批次不可开始：\n" + "\n".join(batch_issues))
    batch = load_batch(batch_path)
    if role not in batch["review_policy"]["allowed_roles"]:
        raise ReviewError(f"{batch['layer']} 层不接受 reviewer role={role}")
    expertise_values = [item.strip() for item in expertise if item.strip()]
    if batch["layer"] == "expert" and not expertise_values:
        raise ReviewError("专家层必须声明至少一个 expertise 乐器族")
    if listening_environment not in {"headphones", "speakers", "other"}:
        raise ReviewError("environment 必须是 headphones、speakers 或 other")
    if not reviewer_id.strip() or not device.strip():
        raise ReviewError("reviewer 与 device 不能为空")

    timestamp = started_at or utc_now()
    if parse_utc_timestamp(timestamp) is None:
        raise ReviewError("started_at 必须是以 Z 结尾的有效 UTC 时间")
    response = {
        "$schema": "https://tianlai.local/schemas/listening-review-response.schema.json",
        "schema_version": 1,
        "kind": "listening_review_response",
        "response_id": (
            f"{batch['batch_id']}-{hashlib.sha256((reviewer_id + timestamp).encode()).hexdigest()[:10]}"
        ),
        "batch_id": batch["batch_id"],
        "batch_sha256": batch["batch_sha256"],
        "layer": batch["layer"],
        "completion_status": "draft",
        "reviewer": {
            "reviewer_id": reviewer_id.strip(),
            "display_name": display_name.strip(),
            "role": role,
            "expertise": expertise_values,
        },
        "session": {
            "started_at": timestamp,
            "completed_at": None,
            "listening_environment": listening_environment,
            "device": device.strip(),
            "notes": notes.strip(),
        },
        "answers": [],
    }
    schema_issues = _schema_errors(response, RESPONSE_SCHEMA)
    if schema_issues:
        raise ReviewError("新响应不符合 Schema：\n" + "\n".join(schema_issues))
    write_json_atomic(output_path, response)
    return response


def _find_item(batch: dict[str, Any], label: str) -> dict[str, Any]:
    matches = [
        item
        for item in batch["items"]
        if label in {
            str(item["item_id"]),
            str(item["blind_label"]),
            str(item["order"]),
        }
    ]
    if len(matches) != 1:
        raise ReviewError(f"找不到唯一 item：{label}")
    return matches[0]


def _find_question(batch: dict[str, Any], question_id: str) -> dict[str, Any]:
    matches = [
        question
        for question in batch["questions"]
        if question["question_id"] == question_id
    ]
    if len(matches) != 1:
        raise ReviewError(f"找不到 question：{question_id}")
    return matches[0]


def _expected_pairs(batch: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (str(item["item_id"]), str(question["question_id"]))
        for item in batch["items"]
        for question in batch["questions"]
    }


def record_answer(
    batch_path: Path,
    response_path: Path,
    project_root: Path,
    *,
    item_label: str,
    question_id: str,
    status: str,
    comment: str = "",
    answered_at: str | None = None,
) -> dict[str, Any]:
    if status not in STATUS_CHOICES:
        raise ReviewError(f"status 必须是：{', '.join(STATUS_CHOICES)}")
    comment = comment.strip()
    if status != "pass" and not comment:
        raise ReviewError("reject/unsure/not_applicable 必须写评论说明")
    batch_issues = validate_batch(batch_path, project_root)
    if batch_issues:
        raise ReviewError("批次或音频已失效：\n" + "\n".join(batch_issues))
    batch = load_batch(batch_path)
    response = read_json(response_path)
    if (
        response.get("batch_id") != batch["batch_id"]
        or response.get("batch_sha256") != batch["batch_sha256"]
    ):
        raise ReviewError("响应没有绑定当前批次")
    if response.get("reviewer", {}).get("role") not in batch["review_policy"][
        "allowed_roles"
    ]:
        raise ReviewError("审阅者角色不允许填写该层")

    item = _find_item(batch, item_label)
    question = _find_question(batch, question_id)
    if question["requires_reference"] and not item["references"]:
        if status != "not_applicable":
            raise ReviewError("本题没有参考音频，只能选 not_applicable")
    if question["requires_context"] and not item["context_audio"]:
        if status != "not_applicable":
            raise ReviewError("本题没有合奏/场景音频，只能选 not_applicable")
    if question["requires_expert"] and response["reviewer"]["role"] not in {
        "instrument_expert",
        "review_lead",
    }:
        raise ReviewError("本题只接受 instrument_expert 或 review_lead")

    answer_timestamp = answered_at or utc_now()
    if parse_utc_timestamp(answer_timestamp) is None:
        raise ReviewError("answered_at 必须是以 Z 结尾的有效 UTC 时间")
    answer = {
        "item_id": item["item_id"],
        "wav_sha256": item["wav_sha256"],
        "question_id": question_id,
        "status": status,
        "comment": comment,
        "answered_at": answer_timestamp,
    }
    answers = [
        existing
        for existing in response.get("answers", [])
        if (
            existing.get("item_id"),
            existing.get("question_id"),
        )
        != (item["item_id"], question_id)
    ]
    answers.append(answer)
    answers.sort(key=lambda value: (value["item_id"], value["question_id"]))
    response["answers"] = answers
    answered_pairs = {
        (str(value["item_id"]), str(value["question_id"]))
        for value in answers
    }
    if answered_pairs == _expected_pairs(batch):
        response["completion_status"] = "complete"
        response["session"]["completed_at"] = answer["answered_at"]
    else:
        response["completion_status"] = "draft"
        response["session"]["completed_at"] = None

    issues = validate_response_document(
        batch,
        response,
        require_complete=False,
        check_schema=True,
    )
    if issues:
        raise ReviewError("响应更新后无效：\n" + "\n".join(issues))
    write_json_atomic(response_path, response)
    return response


def validate_response_document(
    batch: dict[str, Any],
    response: dict[str, Any],
    *,
    require_complete: bool,
    check_schema: bool,
) -> list[str]:
    issues: list[str] = []
    if check_schema:
        issues.extend(_schema_errors(response, RESPONSE_SCHEMA))
    response_keys = {
        "$schema",
        "schema_version",
        "kind",
        "response_id",
        "batch_id",
        "batch_sha256",
        "layer",
        "completion_status",
        "reviewer",
        "session",
        "answers",
    }
    missing_response_keys = sorted(response_keys - set(response))
    extra_response_keys = sorted(set(response) - response_keys)
    if missing_response_keys:
        issues.append(
            "response 缺字段：" + ", ".join(missing_response_keys)
        )
    if extra_response_keys:
        issues.append(
            "response 含未知字段：" + ", ".join(extra_response_keys)
        )
    if response.get("$schema") != (
        "https://tianlai.local/schemas/listening-review-response.schema.json"
    ):
        issues.append("response $schema 无效")
    if response.get("kind") != "listening_review_response":
        issues.append("kind 不是 listening_review_response")
        return issues
    if response.get("schema_version") != 1:
        issues.append("response schema_version 必须为 1")
    if not isinstance(response.get("response_id"), str) or not str(
        response.get("response_id", "")
    ).strip():
        issues.append("response_id 必须是非空字符串")
    if response.get("completion_status") not in {"draft", "complete"}:
        issues.append("completion_status 无效")
    if response.get("batch_id") != batch.get("batch_id"):
        issues.append("stale:response batch_id 不匹配")
    if response.get("batch_sha256") != batch.get("batch_sha256"):
        issues.append("stale:response 绑定的是旧 batch_sha256")
    if response.get("layer") != batch.get("layer"):
        issues.append("response layer 与 batch 不一致")
    reviewer = response.get("reviewer")
    if not isinstance(reviewer, dict):
        issues.append("reviewer 必须是对象")
        reviewer = {}
    reviewer_keys = {"reviewer_id", "display_name", "role", "expertise"}
    if isinstance(response.get("reviewer"), dict):
        missing_reviewer_keys = sorted(reviewer_keys - set(reviewer))
        extra_reviewer_keys = sorted(set(reviewer) - reviewer_keys)
        if missing_reviewer_keys:
            issues.append(
                "reviewer 缺字段：" + ", ".join(missing_reviewer_keys)
            )
        if extra_reviewer_keys:
            issues.append(
                "reviewer 含未知字段：" + ", ".join(extra_reviewer_keys)
            )
    if not isinstance(reviewer.get("reviewer_id"), str) or not str(
        reviewer.get("reviewer_id", "")
    ).strip():
        issues.append("reviewer_id 必须是非空字符串")
    if not isinstance(reviewer.get("display_name"), str):
        issues.append("reviewer.display_name 必须是字符串")
    expertise = reviewer.get("expertise")
    if not isinstance(expertise, list) or not all(
        isinstance(value, str) and value.strip() for value in expertise
    ):
        issues.append("reviewer.expertise 必须是字符串数组")
        expertise = []
    elif len(expertise) != len(set(expertise)):
        issues.append("reviewer.expertise 不能重复")
    role = reviewer.get("role")
    if role not in batch.get("review_policy", {}).get("allowed_roles", []):
        issues.append(f"reviewer role={role} 不允许填写该层")
    if batch.get("layer") == "expert" and not expertise:
        issues.append("专家层响应没有 expertise")
    session = response.get("session")
    if not isinstance(session, dict):
        issues.append("session 必须是对象")
        session = {}
    session_keys = {
        "started_at",
        "completed_at",
        "listening_environment",
        "device",
        "notes",
    }
    if isinstance(response.get("session"), dict):
        missing_session_keys = sorted(session_keys - set(session))
        extra_session_keys = sorted(set(session) - session_keys)
        if missing_session_keys:
            issues.append(
                "session 缺字段：" + ", ".join(missing_session_keys)
            )
        if extra_session_keys:
            issues.append(
                "session 含未知字段：" + ", ".join(extra_session_keys)
            )
    if session.get("listening_environment") not in {
        "headphones",
        "speakers",
        "other",
    }:
        issues.append("session.listening_environment 无效")
    if not isinstance(session.get("device"), str) or not str(
        session.get("device", "")
    ).strip():
        issues.append("session.device 必须是非空字符串")
    started_at = session.get("started_at")
    parsed_started_at = parse_utc_timestamp(started_at)
    if parsed_started_at is None:
        issues.append("session.started_at 必须是以 Z 结尾的有效 UTC 时间")
    completed_at = session.get("completed_at")
    parsed_completed_at = parse_utc_timestamp(completed_at)
    completion_status = response.get("completion_status")
    if completion_status == "complete":
        if parsed_completed_at is None:
            issues.append(
                "完整响应的 session.completed_at "
                "必须是以 Z 结尾的有效 UTC 时间"
            )
        elif (
            parsed_started_at is not None
            and parsed_completed_at < parsed_started_at
        ):
            issues.append("session.completed_at 早于 started_at")
    elif completion_status == "draft" and completed_at is not None:
        issues.append("草稿响应的 session.completed_at 必须为 null")
    if not isinstance(session.get("notes"), str):
        issues.append("session.notes 必须是字符串")

    items = {str(item["item_id"]): item for item in batch.get("items", [])}
    questions = {
        str(question["question_id"]): question
        for question in batch.get("questions", [])
    }
    seen: set[tuple[str, str]] = set()
    answers = response.get("answers")
    if not isinstance(answers, list):
        issues.append("answers 必须是数组")
        answers = []
    for answer_index, answer in enumerate(answers):
        if not isinstance(answer, dict):
            issues.append(f"answers/{answer_index} 必须是对象")
            continue
        answer_keys = {
            "item_id",
            "wav_sha256",
            "question_id",
            "status",
            "comment",
            "answered_at",
        }
        missing_answer_keys = sorted(answer_keys - set(answer))
        extra_answer_keys = sorted(set(answer) - answer_keys)
        if missing_answer_keys:
            issues.append(
                f"answers/{answer_index} 缺字段："
                + ", ".join(missing_answer_keys)
            )
        if extra_answer_keys:
            issues.append(
                f"answers/{answer_index} 含未知字段："
                + ", ".join(extra_answer_keys)
            )
        item_id = str(answer.get("item_id", ""))
        question_id = str(answer.get("question_id", ""))
        key = (item_id, question_id)
        if key in seen:
            issues.append(f"重复答案：{item_id}/{question_id}")
            continue
        seen.add(key)
        item = items.get(item_id)
        question = questions.get(question_id)
        if item is None:
            issues.append(f"答案引用未知 item：{item_id}")
            continue
        if question is None:
            issues.append(f"答案引用未知 question：{question_id}")
            continue
        if answer.get("wav_sha256") != item.get("wav_sha256"):
            issues.append(f"stale:{item_id} 答案绑定的是旧WAV Hash")
        status = answer.get("status")
        if status not in STATUS_CHOICES:
            issues.append(f"{item_id}/{question_id} status 无效")
        comment = answer.get("comment")
        if not isinstance(comment, str):
            issues.append(f"{item_id}/{question_id} comment 必须是字符串")
        if status != "pass" and not isinstance(comment, str):
            issues.append(f"{item_id}/{question_id} 非pass答案缺评论")
        elif status != "pass" and not comment.strip():
            issues.append(f"{item_id}/{question_id} 非pass答案缺评论")
        answered_at = parse_utc_timestamp(answer.get("answered_at"))
        if answered_at is None:
            issues.append(
                f"{item_id}/{question_id} answered_at "
                "必须是以 Z 结尾的有效 UTC 时间"
            )
        elif parsed_started_at is not None and answered_at < parsed_started_at:
            issues.append(f"{item_id}/{question_id} answered_at 早于 started_at")
        elif (
            parsed_completed_at is not None
            and answered_at > parsed_completed_at
        ):
            issues.append(f"{item_id}/{question_id} answered_at 晚于 completed_at")
        if (
            question.get("requires_reference")
            and not item.get("references")
            and status != "not_applicable"
        ):
            issues.append(f"{item_id}/{question_id} 无参考却未选not_applicable")
        if (
            question.get("requires_context")
            and not item.get("context_audio")
            and status != "not_applicable"
        ):
            issues.append(f"{item_id}/{question_id} 无语境音频却未选not_applicable")
        if question.get("requires_expert") and role not in {
            "instrument_expert",
            "review_lead",
        }:
            issues.append(f"{item_id}/{question_id} 不是专家角色")

    expected = _expected_pairs(batch)
    complete = seen == expected
    if response.get("completion_status") == "complete" and not complete:
        issues.append("completion_status=complete 但答案不完整")
    if require_complete and not complete:
        issues.append(f"响应未完成：{len(seen)}/{len(expected)}")
    return issues


def validate_response(
    batch_path: Path,
    response_path: Path,
    project_root: Path,
    *,
    require_complete: bool = True,
) -> list[str]:
    batch_issues = validate_batch(batch_path, project_root)
    try:
        batch = read_json(batch_path)
        response = read_json(response_path)
    except ReviewError as error:
        return batch_issues + [str(error)]
    return batch_issues + validate_response_document(
        batch,
        response,
        require_complete=require_complete,
        check_schema=True,
    )


def _question_disposition(
    counts: dict[str, int],
    minimum_reviewers: int,
) -> str:
    if counts["pass"] and counts["reject"]:
        return "conflict"
    if counts["reject"]:
        return "reject"
    if counts["unsure"]:
        return "unsure"
    if (
        counts["not_applicable"]
        and sum(counts.values()) == counts["not_applicable"]
    ):
        return "not_applicable"
    if counts["pass"] >= minimum_reviewers:
        return "pass"
    return "insufficient"


def _item_disposition(question_results: list[dict[str, Any]]) -> str:
    dispositions = {result["disposition"] for result in question_results}
    for value in ("reject", "conflict", "unsure", "insufficient"):
        if value in dispositions:
            return value
    if dispositions == {"not_applicable"}:
        return "not_applicable"
    if dispositions <= {"pass", "not_applicable"} and "pass" in dispositions:
        return "pass"
    return "insufficient"


def _response_reviewer_id(response: dict[str, Any]) -> str:
    reviewer = response.get("reviewer")
    if not isinstance(reviewer, dict):
        return ""
    return str(reviewer.get("reviewer_id", ""))


def summarize_reviews(
    batch_root: Path,
    responses_root: Path,
    project_root: Path,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    batch_paths = sorted(batch_root.rglob("batch.json"))
    if not batch_paths:
        raise ReviewError(f"找不到 batch.json：{batch_root}")

    batches: dict[str, tuple[Path, dict[str, Any], list[str]]] = {}
    for path in batch_paths:
        document = read_json(path)
        batch_id = str(document.get("batch_id", ""))
        if not batch_id or batch_id in batches:
            raise ReviewError(f"批次ID缺失或重复：{path}")
        batches[batch_id] = (path, document, validate_batch(path, project_root))

    response_paths = sorted(responses_root.rglob("*.json")) if responses_root.exists() else []
    parsed_responses: list[tuple[Path, dict[str, Any]]] = []
    ignored_nonresponses = 0
    for path in response_paths:
        try:
            document = read_json(path)
        except ReviewError:
            parsed_responses.append((path, {"kind": "invalid_json"}))
            continue
        if document.get("kind") != "listening_review_response":
            ignored_nonresponses += 1
            continue
        parsed_responses.append((path, document))

    duplicate_keys: set[tuple[str, str]] = set()
    seen_keys: set[tuple[str, str]] = set()
    for _path, response in parsed_responses:
        key = (
            str(response.get("batch_id", "")),
            _response_reviewer_id(response),
        )
        if key in seen_keys:
            duplicate_keys.add(key)
        seen_keys.add(key)

    accepted: list[dict[str, Any]] = []
    response_audit: list[dict[str, Any]] = []
    for path, response in parsed_responses:
        batch_id = str(response.get("batch_id", ""))
        reviewer_id = _response_reviewer_id(response)
        key = (batch_id, reviewer_id)
        issues: list[str] = []
        if batch_id not in batches:
            issues.append("响应引用未知批次")
        elif key in duplicate_keys:
            issues.append("同一 reviewer 对同一批次有重复响应，全部排除")
        else:
            _batch_path, batch, batch_issues = batches[batch_id]
            issues.extend(batch_issues)
            issues.extend(
                validate_response_document(
                    batch,
                    response,
                    require_complete=False,
                    check_schema=True,
                )
            )
        if any(issue.startswith("stale:") for issue in issues):
            state = "stale"
        elif issues:
            state = "invalid"
        elif response.get("completion_status") != "complete":
            state = "draft"
        else:
            state = "accepted"
            accepted.append(response)
        response_audit.append(
            {
                "path": project_relative(project_root, path)
                if path.resolve().is_relative_to(project_root.resolve())
                else str(path.resolve()),
                "batch_id": batch_id,
                "reviewer_id": reviewer_id,
                "state": state,
                "issues": issues,
            }
        )

    answers_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for response in accepted:
        for answer in response["answers"]:
            key = (
                str(response["batch_id"]),
                str(answer["item_id"]),
                str(answer["question_id"]),
            )
            answers_by_key.setdefault(key, []).append(
                {
                    "status": answer["status"],
                    "comment": answer["comment"],
                    "reviewer_id": response["reviewer"]["reviewer_id"],
                    "role": response["reviewer"]["role"],
                }
            )

    item_results: list[dict[str, Any]] = []
    expert_reviewers: set[str] = set()
    for batch_id, (_path, batch, _issues) in batches.items():
        minimum = int(
            batch["review_policy"]["minimum_independent_reviewers"]
        )
        for item in batch["items"]:
            question_results: list[dict[str, Any]] = []
            reviewers: set[str] = set()
            for question in batch["questions"]:
                answers = answers_by_key.get(
                    (
                        batch_id,
                        str(item["item_id"]),
                        str(question["question_id"]),
                    ),
                    [],
                )
                counts = {status: 0 for status in STATUS_CHOICES}
                for answer in answers:
                    counts[str(answer["status"])] += 1
                    reviewers.add(str(answer["reviewer_id"]))
                    if (
                        batch["layer"] == "expert"
                        and answer["role"] in {
                            "instrument_expert",
                            "review_lead",
                        }
                    ):
                        expert_reviewers.add(str(answer["reviewer_id"]))
                question_results.append(
                    {
                        "question_id": question["question_id"],
                        "counts": counts,
                        "disposition": _question_disposition(
                            counts, minimum
                        ),
                        "comments": [
                            {
                                "reviewer_id": answer["reviewer_id"],
                                "status": answer["status"],
                                "comment": answer["comment"],
                            }
                            for answer in answers
                            if answer["comment"]
                        ],
                    }
                )
            item_results.append(
                {
                    "batch_id": batch_id,
                    "layer": batch["layer"],
                    "item_id": item["item_id"],
                    "instrument_path": item["instrument_path"],
                    "wav_sha256": item["wav_sha256"],
                    "independent_reviewer_count": len(reviewers),
                    "required_independent_reviewers": minimum,
                    "disposition": _item_disposition(question_results),
                    "questions": question_results,
                }
            )

    disposition_counts: dict[str, int] = {}
    for item in item_results:
        disposition_counts[item["disposition"]] = (
            disposition_counts.get(item["disposition"], 0) + 1
        )
    accepted_states = {
        state: sum(1 for item in response_audit if item["state"] == state)
        for state in ("accepted", "draft", "stale", "invalid")
    }
    required_layers = {"technical", "identity", "expert", "context"}
    layers_by_instrument: dict[str, dict[str, list[str]]] = {}
    for item in item_results:
        layers_by_instrument.setdefault(
            str(item["instrument_path"]), {}
        ).setdefault(str(item["layer"]), []).append(
            str(item["disposition"])
        )
    complete_layer_set = 0
    missing_required_layers = 0
    for layer_results in layers_by_instrument.values():
        if not required_layers <= set(layer_results):
            missing_required_layers += 1
            continue
        if all(
            all(disposition == "pass" for disposition in layer_results[layer])
            for layer in required_layers
        ):
            complete_layer_set += 1

    blocked_reasons: list[str] = []
    if not expert_reviewers:
        blocked_reasons.append("没有有效的乐器族专家响应")
    if missing_required_layers:
        blocked_reasons.append(
            f"{missing_required_layers}件缺少技术/身份/专家/语境中的至少一层"
        )
    if any(
        item["disposition"] != "pass"
        for item in item_results
        if item["layer"] in {"technical", "identity", "expert", "context"}
    ):
        blocked_reasons.append("仍有reject/conflict/unsure/insufficient/not_applicable项")

    summary = {
        "$schema": "https://tianlai.local/schemas/listening-review-summary.schema.json",
        "schema_version": 2,
        "kind": "listening_review_summary",
        "generated_at": generated_at or utc_now(),
        "batch_count": len(batches),
        "item_review_scope_count": len(item_results),
        "response_file_count": len(parsed_responses),
        "ignored_nonresponse_json_count": ignored_nonresponses,
        "response_states": accepted_states,
        "disposition_counts": disposition_counts,
        "expert_reviewer_count": len(expert_reviewers),
        "collaboration_review_gate": {
            "automatic_status_change": False,
            "required_layers": sorted(required_layers),
            "instruments_with_all_required_layers_passed": complete_layer_set,
            "instruments_missing_required_layers": missing_required_layers,
            "status": (
                "blocked"
                if blocked_reasons
                else "eligible_for_review_lead_decision"
            ),
            "reasons": blocked_reasons,
            "notice": (
                "四层结果仅用于协作/语境验收；汇总工具永不修改乐器清单、"
                "试听报告或 quality_tier。"
            ),
        },
        "responses": response_audit,
        "items": item_results,
    }
    schema_issues = _schema_errors(summary, SUMMARY_SCHEMA)
    if schema_issues:
        raise ReviewError("汇总不符合 Schema：\n" + "\n".join(schema_issues))
    return summary


def show_batch(batch_path: Path, project_root: Path, *, reveal: bool) -> None:
    issues = validate_batch(batch_path, project_root)
    if issues:
        raise ReviewError("批次无效：\n" + "\n".join(issues))
    batch = load_batch(batch_path)
    visible = reveal or batch["review_policy"]["target_identity_visible"]
    print(f"{batch['title']}  {batch['batch_id']}")
    print(
        f"{len(batch['items'])} 件，预计 "
        f"{batch['review_policy']['estimated_minutes']} 分钟\n"
    )
    for item in batch["items"]:
        identity = f" — {item['instrument_path']}" if visible else ""
        if item["playback_origin"] == "batch":
            playback = batch_path.parent / item["playback_wav"]
        else:
            playback = resolve_project_path(
                project_root, item["playback_wav"]
            )
        print(f"{item['blind_label']}{identity}")
        print(f"  播放：{playback}")
        if visible:
            print("  覆盖：" + "；".join(item["coverage"]))
        for asset in item["references"]:
            print(
                f"  参考：{asset['label']} — "
                f"{resolve_project_path(project_root, asset['path'])}"
            )
        for asset in item["context_audio"]:
            print(
                f"  语境：{asset['label']} — "
                f"{resolve_project_path(project_root, asset['path'])}"
            )
        for asset in item["previous_versions"]:
            print(
                f"  旧版：{asset['label']} — "
                f"{resolve_project_path(project_root, asset['path'])}"
            )
    print("\n问题：")
    for question in batch["questions"]:
        print(
            f"- {question['question_id']}：{question['acceptance_statement']}"
        )


def run_interactive(
    batch_path: Path,
    response_path: Path,
    project_root: Path,
) -> dict[str, Any]:
    issues = validate_response(
        batch_path,
        response_path,
        project_root,
        require_complete=False,
    )
    if issues:
        raise ReviewError("无法继续响应：\n" + "\n".join(issues))
    batch = load_batch(batch_path)
    response = read_json(response_path)
    answered = {
        (answer["item_id"], answer["question_id"])
        for answer in response["answers"]
    }
    visible = bool(batch["review_policy"]["target_identity_visible"])
    aliases = {
        "p": "pass",
        "pass": "pass",
        "r": "reject",
        "reject": "reject",
        "u": "unsure",
        "unsure": "unsure",
        "n": "not_applicable",
        "na": "not_applicable",
        "not_applicable": "not_applicable",
    }

    for item in batch["items"]:
        pending = [
            question
            for question in batch["questions"]
            if (item["item_id"], question["question_id"]) not in answered
        ]
        if not pending:
            continue
        identity = f" — {item['instrument_path']}" if visible else ""
        if item["playback_origin"] == "batch":
            playback = batch_path.parent / item["playback_wav"]
        else:
            playback = resolve_project_path(project_root, item["playback_wav"])
        print(f"\n== {item['blind_label']}{identity} ==")
        print(f"播放文件：{playback}")
        if visible:
            print("谱例覆盖：" + "；".join(item["coverage"]))
        for asset in item["references"]:
            print(
                f"参考音频 {asset['label']}："
                f"{resolve_project_path(project_root, asset['path'])}"
            )
        for asset in item["context_audio"]:
            print(
                f"语境音频 {asset['label']}："
                f"{resolve_project_path(project_root, asset['path'])}"
            )
        for asset in item["previous_versions"]:
            print(
                f"旧版音频 {asset['label']}："
                f"{resolve_project_path(project_root, asset['path'])}"
            )
        input("播放并可重复听后按 Enter 开始回答；Ctrl+C 可安全停止：")
        for question in pending:
            print(f"\n{question['title']}")
            print(f"PASS 表示：{question['acceptance_statement']}")
            print(f"重点听：{question['listen_for']}")
            while True:
                raw = input("选择 [p]ass/[r]eject/[u]nsure/[n]ot_applicable：").strip().lower()
                status = aliases.get(raw)
                if status:
                    break
                print("输入无效，请用 p/r/u/n。")
            comment = ""
            if status != "pass":
                while not comment:
                    comment = input("请说明原因或时间点：").strip()
            else:
                comment = input("可选评论（直接 Enter 跳过）：").strip()
            response = record_answer(
                batch_path,
                response_path,
                project_root,
                item_label=str(item["item_id"]),
                question_id=str(question["question_id"]),
                status=status,
                comment=comment,
            )
    print(
        f"\n当前状态：{response['completion_status']}，"
        f"已回答 {len(response['answers'])}/{len(_expected_pairs(batch))}"
    )
    return response


def _path_argument(value: str) -> Path:
    return Path(value).expanduser()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成、外发、填写、核验和汇总分层人工听审批次"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="生成随机化听审批次")
    create.add_argument("--project-root", type=_path_argument, default=ROOT)
    create.add_argument("--output", type=_path_argument, required=True)
    create.add_argument("--layer", choices=sorted(QUESTIONNAIRES), required=True)
    create.add_argument("--seed", type=int, default=20260725)
    create.add_argument("--min-batch-size", type=int, default=6)
    create.add_argument("--batch-size", type=int, default=8)
    create.add_argument(
        "--grouping",
        choices=("stratified_mixed", "family"),
        default="stratified_mixed",
    )
    create.add_argument(
        "--materialize",
        choices=("hardlink", "copy", "none"),
        default="hardlink",
    )
    create.add_argument("--asset-map", type=_path_argument)
    create.add_argument("--only", action="append", default=[])

    export_package = subparsers.add_parser(
        "export-package",
        help="导出无需安装环境的匿名离线听审 ZIP/目录",
    )
    export_package.add_argument(
        "--project-root",
        type=_path_argument,
        default=ROOT,
    )
    export_package.add_argument("--batch", type=_path_argument, required=True)
    export_package.add_argument(
        "--output",
        type=_path_argument,
        required=True,
        help="以 .zip 结尾时生成 ZIP，否则生成目录",
    )

    import_response = subparsers.add_parser(
        "import-response",
        help="核验并导入离线网页返回的响应 JSON",
    )
    import_response.add_argument(
        "--project-root",
        type=_path_argument,
        default=ROOT,
    )
    import_source = import_response.add_mutually_exclusive_group(required=True)
    import_source.add_argument("--batch", type=_path_argument)
    import_source.add_argument(
        "--batch-root",
        type=_path_argument,
        help="按响应中的 batch_id 自动定位 batch.json",
    )
    import_response.add_argument(
        "--response",
        type=_path_argument,
        required=True,
    )
    import_response.add_argument(
        "--responses",
        type=_path_argument,
        help="响应目录；使用 --batch-root 时默认为 <batch-root>/responses",
    )
    start = subparsers.add_parser("start", help="创建一份审阅者响应")
    start.add_argument("--project-root", type=_path_argument, default=ROOT)
    start.add_argument("--batch", type=_path_argument, required=True)
    start.add_argument("--output", type=_path_argument, required=True)
    start.add_argument("--reviewer", required=True)
    start.add_argument("--display-name", default="")
    start.add_argument("--role", choices=ROLE_CHOICES, required=True)
    start.add_argument("--expertise", action="append", default=[])
    start.add_argument(
        "--environment",
        choices=("headphones", "speakers", "other"),
        required=True,
    )
    start.add_argument("--device", required=True)
    start.add_argument("--notes", default="")

    record = subparsers.add_parser("record", help="写入或更正一个答案")
    record.add_argument("--project-root", type=_path_argument, default=ROOT)
    record.add_argument("--batch", type=_path_argument, required=True)
    record.add_argument("--response", type=_path_argument, required=True)
    record.add_argument("--item", required=True)
    record.add_argument("--question", required=True)
    record.add_argument("--status", choices=STATUS_CHOICES, required=True)
    record.add_argument("--comment", default="")

    run = subparsers.add_parser("run", help="交互式完成一份响应")
    run.add_argument("--project-root", type=_path_argument, default=ROOT)
    run.add_argument("--batch", type=_path_argument, required=True)
    run.add_argument("--response", type=_path_argument, required=True)

    validate = subparsers.add_parser("validate", help="验证批次或响应")
    validate.add_argument("--project-root", type=_path_argument, default=ROOT)
    validate.add_argument("--batch", type=_path_argument, required=True)
    validate.add_argument("--response", type=_path_argument)
    validate.add_argument("--allow-draft", action="store_true")

    show = subparsers.add_parser("show", help="显示批次播放顺序与问题")
    show.add_argument("--project-root", type=_path_argument, default=ROOT)
    show.add_argument("--batch", type=_path_argument, required=True)
    show.add_argument("--reveal", action="store_true")

    summary = subparsers.add_parser("summary", help="汇总当前有效响应")
    summary.add_argument("--project-root", type=_path_argument, default=ROOT)
    summary.add_argument("--batch-root", type=_path_argument, required=True)
    summary.add_argument("--responses", type=_path_argument, required=True)
    summary.add_argument("--output", type=_path_argument, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            project_root = args.project_root.resolve()
            output = (
                args.output.resolve()
                if args.output.is_absolute()
                else (project_root / args.output).resolve()
            )
            asset_map = (
                args.asset_map.resolve()
                if args.asset_map and args.asset_map.is_absolute()
                else (
                    (project_root / args.asset_map).resolve()
                    if args.asset_map
                    else None
                )
            )
            plan = create_review_plan(
                project_root,
                output,
                layer=args.layer,
                seed=args.seed,
                minimum_batch_size=args.min_batch_size,
                maximum_batch_size=args.batch_size,
                grouping=args.grouping,
                materialize=args.materialize,
                asset_map_path=asset_map,
                only=args.only,
            )
            print(
                f"已生成 {plan['batch_count']} 批、"
                f"{plan['included_instrument_count']} 件：{output}"
            )
            if plan["skipped_without_required_assets"]:
                print(
                    f"因缺参考/语境资产跳过 "
                    f"{len(plan['skipped_without_required_assets'])} 件"
                )
            return 0

        project_root = args.project_root.resolve()
        if args.command == "export-package":
            result = export_offline_package(
                args.batch.resolve(),
                args.output.resolve(),
                project_root,
            )
            package_kind = "ZIP" if result["archive"] else "目录"
            print(
                f"已导出 {package_kind}：{result['item_count']} 件、"
                f"{result['audio_file_count']} 个音频：{result['output']}"
            )
            return 0
        if args.command == "import-response":
            if args.batch_root:
                batch_root = args.batch_root.resolve()
                batch_path = locate_batch_for_offline_response(
                    batch_root,
                    args.response.resolve(),
                )
                responses_root = (
                    args.responses.resolve()
                    if args.responses
                    else (batch_root / "responses").resolve()
                )
            else:
                batch_path = args.batch.resolve()
                if args.responses:
                    responses_root = args.responses.resolve()
                elif (
                    batch_path.parent.parent.name == "batches"
                    and len(batch_path.parents) >= 3
                ):
                    responses_root = (
                        batch_path.parent.parent.parent / "responses"
                    ).resolve()
                else:
                    raise ReviewError(
                        "非标准批次路径必须显式提供 --responses"
                    )
            destination = import_offline_response(
                batch_path,
                args.response.resolve(),
                responses_root,
                project_root,
            )
            print(f"离线响应验证通过并已导入：{destination}")
            return 0
        if args.command == "start":
            start_response(
                args.batch.resolve(),
                args.output.resolve(),
                project_root,
                reviewer_id=args.reviewer,
                display_name=args.display_name,
                role=args.role,
                expertise=args.expertise,
                listening_environment=args.environment,
                device=args.device,
                notes=args.notes,
            )
            print(f"已创建响应：{args.output.resolve()}")
            return 0
        if args.command == "record":
            response = record_answer(
                args.batch.resolve(),
                args.response.resolve(),
                project_root,
                item_label=args.item,
                question_id=args.question,
                status=args.status,
                comment=args.comment,
            )
            print(
                f"已保存；状态 {response['completion_status']}，"
                f"答案 {len(response['answers'])}"
            )
            return 0
        if args.command == "run":
            run_interactive(
                args.batch.resolve(),
                args.response.resolve(),
                project_root,
            )
            return 0
        if args.command == "validate":
            if args.response:
                issues = validate_response(
                    args.batch.resolve(),
                    args.response.resolve(),
                    project_root,
                    require_complete=not args.allow_draft,
                )
            else:
                issues = validate_batch(args.batch.resolve(), project_root)
            if issues:
                print("验证失败：", file=sys.stderr)
                for issue in issues:
                    print(f"- {issue}", file=sys.stderr)
                return 2
            print("验证通过：批次、响应与当前音频 Hash 一致。")
            return 0
        if args.command == "show":
            show_batch(
                args.batch.resolve(),
                project_root,
                reveal=args.reveal,
            )
            return 0
        if args.command == "summary":
            document = summarize_reviews(
                args.batch_root.resolve(),
                args.responses.resolve(),
                project_root,
            )
            write_json_atomic(args.output.resolve(), document)
            print(
                f"已汇总 {document['response_states']['accepted']} 份有效响应，"
                f"stale={document['response_states']['stale']}："
                f"{args.output.resolve()}"
            )
            return 0
        raise AssertionError(args.command)
    except (KeyboardInterrupt, EOFError):
        print("\n已停止；每个已回答条目都已原子保存。", file=sys.stderr)
        return 130
    except ReviewError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
