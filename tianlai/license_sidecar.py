"""Per-render licence and attribution sidecars.

The project-wide licence ledger answers "what may Tianlai use?".  A rendered
audio file needs a narrower answer: "which of those instruments were actually
used here?".  This module builds that answer directly from the exact instrument
manifests bound to one render.  It deliberately does not infer creators,
licences, or permissions from an instrument name.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable

from .provenance import is_project_authored_dsp_provenance


LICENSE_SIDECAR_FORMAT = "tianlai.render_license_sidecar"
LICENSE_SIDECAR_VERSION = 1
ENSEMBLE_LICENSE_SIDECAR_NAME = "许可与署名.json"
ENSEMBLE_ATTRIBUTION_NAME = "许可与署名.txt"

UPSTREAM_TERMS_NOTICE = (
    "公开发布或分发本次渲染音频前，请逐项核对并遵守这里列出的上游条款，"
    "保留适用的作者/音源库署名、许可链接和修改说明；本文件不替代上游许可证原文。"
)
MISSING_FIELD_NOTICE = (
    "字段为空只表示乐器清单没有声明该事实，不表示不存在权利人、没有署名要求"
    "或可以不受限制地发布。"
)

_CREATOR_FIELDS = (
    "creator",
    "author",
    "sample_creator",
    "recorded_by",
)
_ATTRIBUTION_FIELDS = (
    "attribution",
    "attribution_text",
    "credit",
)


@dataclass(frozen=True, slots=True)
class InstrumentUse:
    """One manifest used by one or more render executors."""

    manifest_path: str | Path
    used_by: tuple[str, ...]
    manifest_label: str | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class AudioArtifact:
    """One audio artifact covered by the sidecar."""

    role: str
    path: str | Path
    label: str


@dataclass(frozen=True, slots=True)
class LicenseSidecarResult:
    json_path: str
    text_path: str
    json_sha256: str
    text_sha256: str
    document: dict[str, Any]


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def single_render_sidecar_paths(
    output_path: str | Path,
) -> tuple[Path, Path]:
    """Return adjacent, unambiguous paths for one standalone WAV."""

    audio = Path(output_path)
    return (
        audio.with_name(f"{audio.name}.许可与署名.json"),
        audio.with_name(f"{audio.name}.许可与署名.txt"),
    )


def portable_manifest_label(path: str | Path) -> tuple[str, bool]:
    """Avoid leaking a machine-local absolute path into a portable sidecar."""

    manifest_path = Path(path).resolve()
    parts = manifest_path.parts
    indexes = [
        index
        for index, part in enumerate(parts[:-1])
        if part == "乐器"
    ]
    if indexes:
        relative = Path(*parts[indexes[-1] + 1 :])
        return relative.as_posix(), True
    return manifest_path.name, False


def _optional_text(
    manifest: dict[str, Any],
    field: str,
) -> str | None:
    value = manifest.get(field)
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _first_optional_text(
    manifest: dict[str, Any],
    fields: Iterable[str],
) -> tuple[str | None, str | None]:
    for field in fields:
        value = _optional_text(manifest, field)
        if value is not None:
            return value, field
    return None, None


def _evidence_files(manifest: dict[str, Any]) -> list[str]:
    raw = manifest.get("evidence_files")
    if not isinstance(raw, list):
        return []
    return [
        item.strip()
        for item in raw
        if isinstance(item, str) and item.strip()
    ]


def _load_manifest_record(
    use: InstrumentUse,
) -> tuple[str, dict[str, Any], str]:
    path = Path(use.manifest_path).resolve()
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if use.expected_sha256 is not None and digest != use.expected_sha256:
        raise ValueError(
            f"乐器清单在渲染期间发生变化：{path}；"
            f"渲染绑定 {use.expected_sha256}，当前 {digest}"
        )
    manifest = json.loads(raw.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"乐器清单根节点必须是对象：{path}")
    if use.manifest_label is not None:
        label = Path(use.manifest_label).as_posix()
        portable = True
    else:
        label, portable = portable_manifest_label(path)

    creator, creator_field = _first_optional_text(
        manifest,
        _CREATOR_FIELDS,
    )
    attribution, attribution_field = _first_optional_text(
        manifest,
        _ATTRIBUTION_FIELDS,
    )
    fields: dict[str, str | None] = {
        "instrument": _optional_text(manifest, "name"),
        "upstream": _optional_text(manifest, "upstream"),
        "creator": creator,
        "origin": _optional_text(manifest, "origin"),
        "upstream_version": _optional_text(manifest, "upstream_version"),
        "license": _optional_text(manifest, "license"),
        "license_status": _optional_text(manifest, "license_status"),
        "attribution": attribution,
    }
    project_authored_dsp = is_project_authored_dsp_provenance(manifest)
    warnings: list[str] = []
    if not portable:
        warnings.append(
            "manifest_catalog_path_unavailable_only_filename_recorded"
        )
    required_source_fields = (
        ("instrument",)
        if project_authored_dsp
        else (
            "instrument",
            "upstream",
            "creator",
            "origin",
            "license",
            "license_status",
        )
    )
    for field in required_source_fields:
        if fields[field] is None:
            warnings.append(f"{field}_missing_in_manifest")

    record: dict[str, Any] = {
        **fields,
        "creator_manifest_field": creator_field,
        "attribution_manifest_field": attribution_field,
        "manifest": {
            "path": label,
            "sha256": digest,
        },
        "used_by": sorted(set(str(item) for item in use.used_by)),
        "evidence_files": _evidence_files(manifest),
        "upstream_terms_action": (
            "not_applicable_no_third_party_audio_assets"
            if project_authored_dsp
            else "review_and_retain_all_applicable_upstream_notices"
        ),
        "warnings": warnings,
    }
    if project_authored_dsp:
        record.update(
            {
                "provenance_kind": manifest["provenance_kind"],
                "implementation_license": manifest["implementation_license"],
                "external_audio_assets": manifest["external_audio_assets"],
                "audio_asset_license": manifest["audio_asset_license"],
            }
        )
    return str(path), record, label


def build_license_sidecar_document(
    instrument_uses: Iterable[InstrumentUse],
    audio_artifacts: Iterable[AudioArtifact],
) -> dict[str, Any]:
    """Build deterministic attribution data for only this render's inputs."""

    records_by_path: dict[str, dict[str, Any]] = {}
    labels_by_path: dict[str, str] = {}
    for use in instrument_uses:
        identity, record, label = _load_manifest_record(use)
        previous = records_by_path.get(identity)
        if previous is None:
            records_by_path[identity] = record
            labels_by_path[identity] = label
            continue
        if previous["manifest"]["sha256"] != record["manifest"]["sha256"]:
            raise ValueError(f"同一乐器清单出现不一致摘要：{label}")
        previous["used_by"] = sorted(
            set(previous["used_by"]) | set(record["used_by"])
        )

    records = [
        records_by_path[identity]
        for identity in sorted(
            records_by_path,
            key=lambda item: (
                labels_by_path[item],
                records_by_path[item]["manifest"]["sha256"],
            ),
        )
    ]

    outputs = []
    for artifact in audio_artifacts:
        path = Path(artifact.path)
        outputs.append(
            {
                "role": str(artifact.role),
                "path": Path(artifact.label).as_posix(),
                "sha256": sha256_file(path),
            }
        )
    outputs.sort(key=lambda item: (item["path"], item["role"]))

    warnings = [
        f"{record['manifest']['path']}: {warning}"
        for record in records
        for warning in record["warnings"]
    ]
    return {
        "format": LICENSE_SIDECAR_FORMAT,
        "version": LICENSE_SIDECAR_VERSION,
        "hash_algorithm": "SHA-256",
        "scope": {
            "rule": "actual_render_inputs_only",
            "instrument_count": len(records),
            "audio_artifact_count": len(outputs),
        },
        "upstream_terms_notice": UPSTREAM_TERMS_NOTICE,
        "missing_field_notice": MISSING_FIELD_NOTICE,
        "audio_artifacts": outputs,
        "instruments": records,
        "warnings": warnings,
    }


def render_human_attribution(document: dict[str, Any]) -> str:
    """Render a compact notice without manufacturing missing credits."""

    records = document["instruments"]
    lines = [
        "天籁渲染许可与署名",
        "",
        "范围：只列本次渲染实际使用的乐器，不是项目全局音源清单。",
    ]
    if any(
        record.get("provenance_kind") != "project_authored_dsp"
        for record in records
    ):
        lines.append(str(document["upstream_terms_notice"]))
    if any(
        warning.endswith("_missing_in_manifest")
        for record in records
        for warning in record["warnings"]
    ):
        lines.append(str(document["missing_field_notice"]))
    lines.append("")
    for index, record in enumerate(records, 1):
        name = record.get("instrument") or "（清单未声明名称）"
        lines.extend(
            [
                f"{index}. {name}",
                f"   清单：{record['manifest']['path']}",
                f"   使用者：{', '.join(record['used_by']) or '（未声明）'}",
            ]
        )
        if record.get("provenance_kind") == "project_authored_dsp":
            lines.extend(
                [
                    "   来源类型：自研 DSP",
                    f"   实现许可：{record['implementation_license']}",
                    "   第三方采样：无",
                    "   音频资产许可：不适用",
                    f"   许可状态：{record['license_status']}",
                ]
            )
        else:
            lines.extend(
                [
                    f"   上游：{record.get('upstream') or '（清单未声明）'}",
                    f"   创作者/录音者：{record.get('creator') or '（清单未声明）'}",
                    f"   来源：{record.get('origin') or '（清单未声明）'}",
                    f"   版本：{record.get('upstream_version') or '（清单未声明）'}",
                    f"   许可：{record.get('license') or '（清单未声明）'}",
                    f"   许可状态：{record.get('license_status') or '（清单未声明）'}",
                ]
            )
        if record.get("attribution"):
            lines.append(f"   上游署名文本：{record['attribution']}")
        evidence = record.get("evidence_files") or []
        if evidence:
            lines.append(f"   证据文件：{', '.join(evidence)}")
        if record["warnings"]:
            lines.append(f"   数据告警：{', '.join(record['warnings'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    with os.fdopen(descriptor, "wb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    # A successful replacement consumes ``temporary``.  On failure, preserve
    # the exact temporary name for recovery: unlinking it by path here could
    # delete a different file installed at that name after a race.
    os.replace(temporary, path)


def write_license_sidecars(
    json_path: str | Path,
    text_path: str | Path,
    *,
    instrument_uses: Iterable[InstrumentUse],
    audio_artifacts: Iterable[AudioArtifact],
) -> LicenseSidecarResult:
    """Atomically publish the machine and human views of one sidecar."""

    document = build_license_sidecar_document(
        instrument_uses,
        audio_artifacts,
    )
    json_payload = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    text_payload = render_human_attribution(document).encode("utf-8")
    json_target = Path(json_path)
    text_target = Path(text_path)
    _atomic_write(json_target, json_payload)
    _atomic_write(text_target, text_payload)
    return LicenseSidecarResult(
        json_path=str(json_target),
        text_path=str(text_target),
        json_sha256=hashlib.sha256(json_payload).hexdigest(),
        text_sha256=hashlib.sha256(text_payload).hexdigest(),
        document=document,
    )
