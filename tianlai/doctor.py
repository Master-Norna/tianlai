"""Lightweight runtime and instrument-resource diagnostics.

The doctor deliberately inspects manifests and resource references without
constructing instrument instances.  A missing multi-gigabyte sample library
therefore remains a normal, reportable state instead of making diagnostics
crash or allocate audio buffers.

``collect_doctor_report`` is the stable Python API.  The module can also be
run directly:

``python -m tianlai.doctor --json``
"""

from __future__ import annotations

import argparse
from collections import Counter
from functools import lru_cache
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import struct
import sys
import uuid
from typing import Any, Iterable

from . import __version__
from .catalog import CatalogEntry, discover_instruments
from .resource_restore import (
    ResourceRestoreError,
    default_manifest_path,
    load_restore_manifest,
)
from .runtime_layout import RuntimeLayout, RuntimeLayoutError, discover_runtime_layout
from .trust import TrustPolicyError, load_trusted_instruments


REPORT_SCHEMA_VERSION = 2

_SELF_CONTAINED_TYPES = frozenset(
    {
        "modeled_bianzhong",
        "modeled_instrument",
        "oscillator",
        "procedural_sfx",
        "synthesizer",
    }
)
_DEDICATED_TYPES = frozenset({"dedicated_fx", "dedicated_sfz"})
_PATH_KEYS = frozenset({"sample", "sfz", "sfz_file", "soundfont", "source_sfz"})


def _distribution_version() -> str | None:
    try:
        return importlib.metadata.version("tianlai-audio")
    except importlib.metadata.PackageNotFoundError:
        return None


def _installed_version() -> str:
    """Report the code that is actually imported, not stale dist-info metadata."""

    return __version__


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _python_runtime_supported(
    *,
    implementation: str,
    version: tuple[int, int],
    bits: int,
) -> bool:
    return (
        implementation == "CPython"
        and (3, 11) <= version < (3, 15)
        and bits == 64
    )


def _windows_only_installer(
    *,
    layout: RuntimeLayout,
    path: Path,
    installer_id: str,
    **metadata: Any,
) -> dict[str, Any]:
    if not path.is_file():
        status = "missing"
    elif _is_windows_runtime():
        status = "available"
    else:
        status = "unavailable_on_platform"
    return {
        "status": status,
        "installer_id": installer_id,
        "path": _relative_or_absolute(path, layout.home),
        "required_platform": "Windows",
        **metadata,
    }


@lru_cache(maxsize=16)
def _restore_instrument_index(
    manifest_path: str,
    modified_ns: int,
    size: int,
) -> dict[str, dict[str, Any]]:
    # ``modified_ns`` and ``size`` are cache-busting inputs.  This keeps doctor
    # cheap across 103 entries while allowing an edited source checkout to be
    # re-read in the same Python process.
    del modified_ns, size
    manifest = load_restore_manifest(manifest_path)
    return {
        instrument_id: family
        for family in manifest["families"]
        for instrument_id in family["instrument_ids"]
    }


def _tracked_resource_installer(
    *,
    layout: RuntimeLayout,
    manifest_path: Path,
) -> dict[str, Any] | None:
    restore_manifest = default_manifest_path(layout.home)
    if not restore_manifest.is_file():
        return None
    try:
        metadata = restore_manifest.stat()
        index = _restore_instrument_index(
            str(restore_manifest),
            metadata.st_mtime_ns,
            metadata.st_size,
        )
        instrument_id = (
            manifest_path.parent.relative_to(layout.catalog).as_posix()
        )
    except (OSError, ValueError, ResourceRestoreError):
        return None
    family = index.get(instrument_id)
    if family is None:
        return None
    wrapper = layout.home / "安装可恢复音源.cmd"
    common = {
        "resource_family": family["id"],
        "resource_group": family["group"],
    }
    if _is_windows_runtime():
        return _windows_only_installer(
            layout=layout,
            path=wrapper,
            installer_id=f"resource-restore:{family['id']}",
            arguments=["-ResourceFamily", family["id"]],
            **common,
        )

    module = "tianlai.resource_restore"
    return {
        "status": "available",
        "installer_id": f"resource-restore:{family['id']}",
        "path": _relative_or_absolute(Path(sys.executable), layout.home),
        "module": module,
        "arguments": [
            "-m",
            module,
            "--home",
            str(layout.home),
            "install",
            "--family",
            family["id"],
        ],
        **common,
    }


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _directory_writability(path: Path) -> dict[str, Any]:
    """Probe the directory, or its nearest parent when it is not created yet."""

    requested = path.resolve()
    if requested.exists() and not requested.is_dir():
        return {
            "path": str(requested),
            "exists": True,
            "writable": False,
            "probe_directory": None,
            "error": "path exists but is not a directory",
        }

    probe_directory = requested
    while not probe_directory.exists() and probe_directory != probe_directory.parent:
        probe_directory = probe_directory.parent
    if not probe_directory.is_dir():
        return {
            "path": str(requested),
            "exists": requested.exists(),
            "writable": False,
            "probe_directory": str(probe_directory),
            "error": "no existing parent directory is available",
        }

    probe = probe_directory / f".tianlai-write-probe-{uuid.uuid4().hex}.tmp"
    error: str | None = None
    writable = False
    try:
        with probe.open("xb"):
            pass
        writable = True
    except OSError as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as exc:
            writable = False
            cleanup = f"{type(exc).__name__}: {exc}"
            error = f"{error}; cleanup failed: {cleanup}" if error else cleanup
    return {
        "path": str(requested),
        "exists": requested.is_dir(),
        "writable": writable,
        "probe_directory": str(probe_directory),
        "error": error,
    }


def _normalise_evidence_files(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        return tuple(str(item) for item in raw if str(item).strip())
    return ()


def _manifest_reference_strings(value: Any, *, key: str | None = None) -> Iterable[str]:
    """Yield explicit sample/SFZ/SoundFont paths without guessing prose fields."""

    if key in _PATH_KEYS and isinstance(value, str) and value.strip():
        yield value
        return
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _manifest_reference_strings(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _manifest_reference_strings(child, key=key)


def _safe_resource_path(root: Path, raw: str) -> tuple[Path | None, str | None]:
    candidate = Path(raw.replace("\\", "/"))
    if candidate.is_absolute():
        return None, f"absolute resource reference is not allowed: {raw}"
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, f"resource reference escapes asset_root: {raw}"
    return resolved, None


def _installer_for(
    *,
    layout: RuntimeLayout,
    manifest_path: Path,
    manifest: dict[str, Any],
    asset_backed: bool,
) -> dict[str, Any]:
    if not asset_backed:
        return {
            "status": "not_required",
            "installer_id": None,
            "path": None,
        }

    local = manifest_path.parent / "获取音源.ps1"
    if local.is_file():
        return _windows_only_installer(
            layout=layout,
            path=local,
            installer_id=manifest_path.parent.name,
        )

    tracked = _tracked_resource_installer(
        layout=layout,
        manifest_path=manifest_path,
    )
    if tracked is not None:
        return tracked

    raw_root = str(manifest.get("asset_root", "")).replace("\\", "/").casefold()
    rules = (
        (
            "virtualplayingorchestra",
            "virtual-playing-orchestra",
            layout.home / "安装VPO音源.ps1",
        ),
        (
            "gregsullivan.e-pianos",
            "greg-sullivan-e-pianos",
            layout.catalog / "键盘乐器" / "获取GregSullivan电钢琴音源.ps1",
        ),
        (
            "salamandergrandpiano",
            "salamander-grand-piano",
            layout.catalog / "键盘乐器" / "钢琴" / "获取音源.ps1",
        ),
        (
            "simpk_03_clavichord",
            "simpk-clavichord",
            layout.catalog / "键盘乐器" / "击弦古钢琴" / "获取音源.ps1",
        ),
        (
            "itsclipping",
            "itsclipping-ganjo",
            layout.catalog / "世界乐器" / "班卓琴" / "获取音源.ps1",
        ),
    )
    if str(manifest.get("type", "")) == "soundfont":
        rules = (
            (
                "",
                "local-soundfont-compatibility",
                layout.home / "安装通用音源.ps1",
            ),
        )
    for marker, identifier, path in rules:
        if marker in raw_root:
            return _windows_only_installer(
                layout=layout,
                path=path,
                installer_id=identifier,
            )
    return {
        "status": "unavailable",
        "installer_id": None,
        "path": None,
    }


def _resource_status(
    *,
    layout: RuntimeLayout,
    manifest_path: Path,
    manifest: dict[str, Any],
    verify_references: bool,
) -> dict[str, Any]:
    problems: list[dict[str, str]] = []
    implementation_type = str(manifest.get("type", "")).strip()
    implementation = manifest.get("implementation")
    if implementation is not None:
        implementation_path = (manifest_path.parent / str(implementation)).resolve()
        if not implementation_path.is_file():
            problems.append(
                {
                    "kind": "implementation",
                    "path": str(implementation_path),
                    "message": "instrument implementation file is missing",
                }
            )

    raw_asset_root = str(manifest.get("asset_root", "")).strip()
    raw_soundfont = str(manifest.get("soundfont", "")).strip()
    external_assets = manifest.get(
        "external_audio_assets",
        manifest.get("external_assets", []),
    )
    asset_backed = bool(raw_asset_root or raw_soundfont or external_assets)
    asset_root: Path | None = None

    if raw_asset_root:
        asset_root = (manifest_path.parent / raw_asset_root).resolve()
        if not asset_root.is_dir():
            problems.append(
                {
                    "kind": "asset_root",
                    "path": str(asset_root),
                    "message": "asset_root directory is missing",
                }
            )
    elif raw_soundfont:
        asset_root = manifest_path.parent.resolve()
    elif external_assets:
        problems.append(
            {
                "kind": "manifest",
                "path": str(manifest_path),
                "message": "external assets are declared without asset_root/soundfont",
            }
        )
    elif implementation_type not in _SELF_CONTAINED_TYPES:
        problems.append(
            {
                "kind": "manifest",
                "path": str(manifest_path),
                "message": (
                    "resource contract is unknown: no asset_root and the "
                    f"{implementation_type!r} backend is not self-contained"
                ),
            }
        )

    if asset_root is not None and asset_root.is_dir():
        for relative in _normalise_evidence_files(manifest.get("evidence_files")):
            evidence, error = _safe_resource_path(asset_root, relative)
            if error:
                problems.append(
                    {
                        "kind": "evidence",
                        "path": relative,
                        "message": error,
                    }
                )
            elif evidence is not None and not evidence.is_file():
                problems.append(
                    {
                        "kind": "evidence",
                        "path": str(evidence),
                        "message": "licence/provenance evidence file is missing",
                    }
                )

        for relative in sorted(set(_manifest_reference_strings(manifest))):
            reference, error = _safe_resource_path(asset_root, relative)
            if error:
                problems.append(
                    {
                        "kind": "resource_reference",
                        "path": relative,
                        "message": error,
                    }
                )
            elif reference is not None and not reference.is_file():
                problems.append(
                    {
                        "kind": "resource_reference",
                        "path": str(reference),
                        "message": "manifest-referenced resource file is missing",
                    }
                )

        if (
            verify_references
            and implementation_type in _DEDICATED_TYPES
            and not problems
        ):
            # This existing audit helper expands SFZ includes and verifies every
            # referenced sample with Path.is_file().  It never decodes audio or
            # constructs an Instrument.
            try:
                from .dedicated_candidates import dedicated_manifest_sources

                dedicated_manifest_sources(manifest_path)
            except (OSError, TypeError, ValueError) as exc:
                text = str(exc)
                kind = (
                    "resource_reference"
                    if "does not exist" in text.lower() or "missing" in text.lower()
                    else "resource_contract"
                )
                problems.append(
                    {
                        "kind": kind,
                        "path": str(manifest_path),
                        "message": text,
                    }
                )

    if problems:
        missing_kinds = {
            "asset_root",
            "evidence",
            "implementation",
            "resource_reference",
        }
        status = (
            "missing"
            if all(item["kind"] in missing_kinds for item in problems)
            else "invalid"
        )
    else:
        status = "ready"

    return {
        "status": status,
        "asset_backed": asset_backed,
        "asset_root": str(asset_root) if asset_root is not None else None,
        "check_level": (
            "sfz_references"
            if verify_references and implementation_type in _DEDICATED_TYPES
            else "manifest_references"
        ),
        "problems": problems,
        "installer": _installer_for(
            layout=layout,
            manifest_path=manifest_path,
            manifest=manifest,
            asset_backed=asset_backed,
        ),
    }


def _load_allowlist(
    path: Path,
    entries: list[CatalogEntry],
    *,
    catalog_root: Path,
) -> tuple[frozenset[str], dict[str, Any]]:
    capabilities = {
        Path(entry.manifest_path)
        .parent.relative_to(catalog_root)
        .as_posix(): entry
        for entry in entries
    }
    try:
        trusted = load_trusted_instruments(path, capabilities)
    except TrustPolicyError as exc:
        return frozenset(), {
            "status": "missing" if not path.is_file() else "invalid",
            "count": 0,
            "error": str(exc),
        }
    return trusted, {
        "status": "ready",
        "count": len(trusted),
        "error": None,
    }


def _catalog_entries(
    layout: RuntimeLayout,
) -> tuple[list[CatalogEntry], list[dict[str, str]]]:
    try:
        return discover_instruments(layout.catalog), []
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return [], [
            {
                "path": str(layout.catalog),
                "message": f"{type(exc).__name__}: {exc}",
            }
        ]


def collect_doctor_report(
    *,
    start: str | Path | None = None,
    layout: RuntimeLayout | None = None,
    verify_references: bool = True,
) -> dict[str, Any]:
    """Return a JSON-serialisable runtime, catalogue and resource report."""

    layout = layout or discover_runtime_layout(start=start)
    entries, catalog_errors = _catalog_entries(layout)
    trusted, trusted_report = _load_allowlist(
        layout.allowlist,
        entries,
        catalog_root=layout.catalog,
    )

    instruments: list[dict[str, Any]] = []
    for entry in entries:
        manifest_path = Path(entry.manifest_path)
        relative = manifest_path.parent.relative_to(layout.catalog).as_posix()
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be an object")
            resource = _resource_status(
                layout=layout,
                manifest_path=manifest_path,
                manifest=manifest,
                verify_references=verify_references,
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            resource = {
                "status": "invalid",
                "asset_backed": False,
                "asset_root": None,
                "check_level": "manifest_only",
                "problems": [
                    {
                        "kind": "manifest",
                        "path": str(manifest_path),
                        "message": f"{type(exc).__name__}: {exc}",
                    }
                ],
                "installer": {
                    "status": "unknown",
                    "installer_id": None,
                    "path": None,
                },
            }
        instruments.append(
            {
                "id": relative,
                "name": entry.name,
                "category": entry.category,
                "type": entry.implementation_type,
                "manifest": _relative_or_absolute(manifest_path, layout.home),
                "trusted": relative in trusted,
                "production": not relative.startswith("测试工具/"),
                "resource": resource,
            }
        )

    production = [item for item in instruments if item["production"]]
    resource_counts = Counter(item["resource"]["status"] for item in production)
    installer_counts = Counter(
        item["resource"]["installer"]["status"] for item in production
    )
    trusted_ready = sum(
        1
        for item in instruments
        if item["trusted"] and item["resource"]["status"] == "ready"
    )
    catalog_status = (
        "missing"
        if not layout.catalog_ready
        else "invalid"
        if catalog_errors
        else "ready"
    )
    summary = {
        "status": (
            "error"
            if (
                catalog_status != "ready"
                or trusted_report["status"] != "ready"
                or resource_counts.get("invalid", 0)
            )
            else "degraded"
            if resource_counts.get("missing", 0)
            else "ready"
        ),
        "catalog_count": len(instruments),
        "production_count": len(production),
        "test_utility_count": len(instruments) - len(production),
        "trusted_count": len(trusted),
        "trusted_ready_count": trusted_ready,
        "resource_ready_count": resource_counts.get("ready", 0),
        "resource_missing_count": resource_counts.get("missing", 0),
        "resource_invalid_count": resource_counts.get("invalid", 0),
        "asset_backed_count": sum(
            bool(item["resource"]["asset_backed"]) for item in production
        ),
        "self_contained_count": sum(
            not bool(item["resource"]["asset_backed"]) for item in production
        ),
        "installer_available_count": installer_counts.get("available", 0),
        "installer_unavailable_on_platform_count": installer_counts.get(
            "unavailable_on_platform",
            0,
        ),
        "installer_unavailable_count": installer_counts.get("unavailable", 0),
        "installer_missing_count": installer_counts.get("missing", 0),
    }
    distribution_version = _distribution_version()
    implementation = platform.python_implementation()
    python_version = sys.version_info[:2]
    python_bits = struct.calcsize("P") * 8
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "version": _installed_version(),
        "distribution": {
            "name": "tianlai-audio",
            "version": distribution_version,
            "matches_imported_code": (
                distribution_version is None
                or distribution_version == _installed_version()
            ),
        },
        "python": {
            "version": platform.python_version(),
            "implementation": implementation,
            "executable": sys.executable,
            "bits": python_bits,
            "supported": _python_runtime_supported(
                implementation=implementation,
                version=python_version,
                bits=python_bits,
            ),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "platform": platform.platform(),
        },
        "layout": layout.to_dict(),
        "writability": {
            "home": _directory_writability(layout.home),
            "resources": _directory_writability(layout.resources),
            "output": _directory_writability(layout.output),
        },
        "catalog": {
            "status": catalog_status,
            "count": len(instruments),
            "errors": catalog_errors,
        },
        "trusted": trusted_report,
        "instruments": instruments,
        "summary": summary,
    }


def doctor_report_json(
    report: dict[str, Any] | None = None,
    *,
    indent: int | None = 2,
    **collect_options: Any,
) -> str:
    """Serialise an existing or newly collected report as portable UTF-8 JSON."""

    document = report or collect_doctor_report(**collect_options)
    return json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        indent=indent,
        sort_keys=True,
    )


def _human_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    layout = report["layout"]
    lines = [
        f"天籁 {report['version']} 运行诊断：{summary['status']}",
        (
            f"Python {report['python']['version']} "
            f"({report['python']['bits']}-bit)，{report['platform']['system']} "
            f"{report['platform']['release']}"
        ),
        f"项目目录：{layout['home']}（{layout['source']}）",
        (
            f"目录：{summary['catalog_count']}，正式乐器："
            f"{summary['production_count']}，可信：{summary['trusted_count']}"
        ),
        (
            f"正式资源：ready={summary['resource_ready_count']}，"
            f"missing={summary['resource_missing_count']}，"
            f"invalid={summary['resource_invalid_count']}"
        ),
        (
            f"外部资源安装器：available="
            f"{summary['installer_available_count']}，"
            "unavailable_on_platform="
            f"{summary['installer_unavailable_on_platform_count']}，"
            f"unavailable={summary['installer_unavailable_count']}，"
            f"missing={summary['installer_missing_count']}"
        ),
    ]
    missing = [
        item for item in report["instruments"] if item["resource"]["status"] != "ready"
    ]
    if missing:
        lines.append("未就绪乐器：")
        visible = missing[:10]
        for item in visible:
            first = item["resource"]["problems"][0]["message"]
            lines.append(f"  - {item['id']}：{first}")
        if len(missing) > len(visible):
            lines.append(
                f"  - ……其余 {len(missing) - len(visible)} 件请使用 --json 查看"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查天籁运行布局和乐器资源")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument(
        "--quick",
        action="store_true",
        help="只检查 manifest 显式引用，不展开 SFZ 的全部样本引用",
    )
    parser.add_argument(
        "--require-all-resources",
        action="store_true",
        help="任一正式乐器资源未就绪时返回非零退出码",
    )
    parser.add_argument(
        "--start",
        type=Path,
        help="从指定目录开始发现源码布局（TIANLAI_HOME 优先）",
    )
    args = parser.parse_args(argv)
    try:
        report = collect_doctor_report(
            start=args.start,
            verify_references=not args.quick,
        )
    except RuntimeLayoutError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "schema_version": REPORT_SCHEMA_VERSION,
                        "summary": {"status": "error"},
                        "error": str(exc),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"天籁运行诊断失败：{exc}", file=sys.stderr)
        return 1
    print(doctor_report_json(report) if args.json else _human_summary(report))
    if report["catalog"]["status"] != "ready":
        return 1
    if report["trusted"]["status"] != "ready":
        return 1
    if report["summary"]["resource_invalid_count"]:
        return 1
    if args.require_all_resources and report["summary"]["resource_missing_count"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "collect_doctor_report",
    "doctor_report_json",
    "main",
]
