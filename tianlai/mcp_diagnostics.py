"""Privacy-bounded diagnostics and resource planning for MCP consumers.

The underlying doctor intentionally contains local filesystem details for a human
operator.  MCP clients need a smaller boundary: stable statuses, relative catalogue
identities and actionable counts, without usernames, absolute paths, environment
values, download URLs or native-loader errors.  This module owns that projection.

All functions are read-only.  Runtime and project diagnosis use the doctor's
passive inspection policy: no native-library load, external program, temporary
write probe or network operation is started.  Restore planning only calls the
non-mutating planner; it never downloads, extracts or installs resources.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .doctor import _passive_directory_writability, collect_doctor_report
from .resource_restore import (
    build_restore_plan,
    family_for_instrument,
    load_restore_manifest,
    select_families,
)
from .runtime_layout import RuntimeLayout
from .self_check import build_issue, paginate_issues, summarize_issues


_SCHEMA_VERSION = 1
_CHECK_LEVELS = frozenset({"quick", "references"})
_RESOURCE_STATUSES = frozenset({"ready", "missing", "invalid"})
_RESOURCE_CHECK_LEVELS = frozenset(
    {"manifest_only", "manifest_references", "sfz_references"}
)
_SAFE_RUNTIME_SOURCES = frozenset(
    {
        "environment",
        "working_tree",
        "source_package",
        "engine_only_working_directory",
    }
)
_SAFE_NATIVE_SOURCES = frozenset(
    {
        "project_local",
        "environment_override",
        "homebrew",
        "configured_directory",
        "system_lookup",
    }
)
_SAFE_ARCHIVE_FORMATS = frozenset({"zip", "tar.xz", "7z"})
_SAFE_TOKEN = re.compile(r"^[0-9A-Za-z._+\-]{1,80}$")
_MAX_SELECTOR_ITEMS = 128
_MAX_SELECTOR_LENGTH = 256


def _bounded_limit(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer between 1 and 256")
    if not 1 <= value <= 256:
        raise ValueError(f"{field} must be between 1 and 256")
    return value


def _safe_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return default
    return value


def _safe_token(value: object, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if _SAFE_TOKEN.fullmatch(text) else default


def _safe_relative_path(value: object) -> str | None:
    text = str(value or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or text.startswith("/")
        or any(part in {"", ".", ".."} for part in path.parts)
        or any(":" in part for part in path.parts)
    ):
        return None
    return path.as_posix()


def _safe_identifier(value: object) -> str | None:
    path = _safe_relative_path(value)
    if path is None or "/" in path:
        return None
    return path


def _safe_public_text(value: object, *, fallback: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 200
        or any(character in text for character in "\r\n\x00")
        or "://" in text
        or _safe_relative_path(text) is None
        and (text.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", text))
    ):
        return fallback
    return text


def _status(value: object, allowed: frozenset[str], default: str) -> str:
    text = str(value or "")
    return text if text in allowed else default


def _issue(
    *,
    severity: str,
    code: str,
    stage: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    return build_issue(
        severity=severity,
        code=code,
        stage=stage,
        message=message,
        **details,
    )


def _page_issues(
    issues: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int], bool]:
    return paginate_issues(issues, limit)


def _safe_writability(raw: object) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    exists = item.get("exists") if isinstance(item.get("exists"), bool) else None
    writable = item.get("writable")
    writable = writable if isinstance(writable, bool) else None
    writable_estimate = item.get("writable_estimate")
    if not isinstance(writable_estimate, bool):
        writable_estimate = writable is True
    probe_performed = item.get("probe_performed") is True
    verification = _status(
        item.get("verification"),
        frozenset({"active_probe", "filesystem_metadata", "passive_estimate"}),
        "passive_estimate",
    )
    if writable is True:
        status = "verified_ready"
    elif writable is False:
        status = "unavailable"
    elif writable_estimate:
        status = "estimated_ready"
    else:
        status = "unavailable"
    return {
        "status": status,
        "exists": exists,
        "writable": writable,
        "writable_estimate": writable_estimate,
        "probe_performed": probe_performed,
        "verification": verification,
    }


def _safe_render_environment(
    report: dict[str, Any],
    *,
    output_target: Path,
) -> dict[str, Any]:
    python = report.get("python") if isinstance(report.get("python"), dict) else {}
    platform = (
        report.get("platform") if isinstance(report.get("platform"), dict) else {}
    )
    # Render writes below ``output/mcp`` rather than directly to the runtime's
    # output root.  Estimate the exact target so an existing read-only or
    # non-directory ``mcp`` entry cannot be hidden by a writable parent.
    output = _safe_writability(_passive_directory_writability(output_target))
    python_supported = python.get("supported") is True
    platform_supported = platform.get("supported") is True
    rosetta = (
        platform.get("rosetta")
        if isinstance(platform.get("rosetta"), dict)
        else {}
    )
    macos_translation_identity = rosetta.get("probe_performed") is True
    output_writable_estimate = output["writable_estimate"] is True
    return {
        "ready_for_render_attempt": (
            python_supported and platform_supported and output_writable_estimate
        ),
        "python_supported": python_supported,
        "platform_supported": platform_supported,
        "macos_translation_identity_check_performed": (
            macos_translation_identity
        ),
        "output": output,
        "active_write_probe_performed": output["probe_performed"],
    }


def _resource_family(raw_resource: object) -> str | None:
    resource = raw_resource if isinstance(raw_resource, dict) else {}
    installer = resource.get("installer")
    if not isinstance(installer, dict):
        return None
    return _safe_identifier(installer.get("resource_family"))


def _safe_resource_row(raw: object) -> dict[str, Any] | None:
    item = raw if isinstance(raw, dict) else {}
    instrument_id = _safe_relative_path(item.get("id"))
    resource = item.get("resource")
    if instrument_id is None or not isinstance(resource, dict):
        return None
    status = _status(resource.get("status"), _RESOURCE_STATUSES, "invalid")
    check_level = _status(
        resource.get("check_level"),
        _RESOURCE_CHECK_LEVELS,
        "manifest_only",
    )
    asset_backed = resource.get("asset_backed")
    return {
        "instrument_id": instrument_id,
        "status": status,
        "check_level": check_level,
        "asset_backed": asset_backed if isinstance(asset_backed, bool) else None,
        "resource_family": _resource_family(resource),
    }


def collect_runtime_diagnosis(
    layout: RuntimeLayout,
    *,
    check_level: str = "quick",
    max_issues: int = 32,
) -> dict[str, Any]:
    """Collect a bounded runtime diagnosis without returning local paths.

    ``quick`` checks explicit manifest references; ``references`` additionally
    expands dedicated SFZ references.  Both modes are strictly passive: they do
    not load native libraries, launch external programs or create probe files.
    """

    if check_level not in _CHECK_LEVELS:
        raise ValueError("check_level must be 'quick' or 'references'")
    limit = _bounded_limit(max_issues, "max_issues")
    report = collect_doctor_report(
        layout=layout,
        verify_references=check_level == "references",
        active_probes=False,
    )

    python = report.get("python") if isinstance(report.get("python"), dict) else {}
    platform = (
        report.get("platform") if isinstance(report.get("platform"), dict) else {}
    )
    distribution = (
        report.get("distribution")
        if isinstance(report.get("distribution"), dict)
        else {}
    )
    raw_layout = (
        report.get("layout") if isinstance(report.get("layout"), dict) else {}
    )
    catalog = (
        report.get("catalog") if isinstance(report.get("catalog"), dict) else {}
    )
    trusted = (
        report.get("trusted") if isinstance(report.get("trusted"), dict) else {}
    )
    capabilities = (
        report.get("capabilities")
        if isinstance(report.get("capabilities"), dict)
        else {}
    )
    restore = (
        capabilities.get("resource_restore")
        if isinstance(capabilities.get("resource_restore"), dict)
        else {}
    )
    fluidsynth = (
        capabilities.get("fluidsynth")
        if isinstance(capabilities.get("fluidsynth"), dict)
        else {}
    )
    native = (
        fluidsynth.get("native")
        if isinstance(fluidsynth.get("native"), dict)
        else {}
    )
    binding = (
        fluidsynth.get("python_binding")
        if isinstance(fluidsynth.get("python_binding"), dict)
        else {}
    )
    raw_writability = (
        report.get("writability")
        if isinstance(report.get("writability"), dict)
        else {}
    )
    writability = {
        name: _safe_writability(raw_writability.get(name))
        for name in ("home", "resources", "output")
    }
    writability["output"] = _safe_writability(
        _passive_directory_writability(layout.output / "mcp")
    )

    python_supported = python.get("supported") is True
    platform_supported = platform.get("supported") is True
    catalog_status = _status(
        catalog.get("status"),
        frozenset({"ready", "missing", "invalid"}),
        "invalid",
    )
    trusted_status = _status(
        trusted.get("status"),
        frozenset({"ready", "missing", "invalid"}),
        "invalid",
    )
    distribution_matches = distribution.get("matches_imported_code") is not False

    issues: list[dict[str, Any]] = []
    if not python_supported:
        issues.append(
            _issue(
                severity="error",
                code="runtime.python_unsupported",
                stage="python",
                message="The active Python runtime is outside Tianlai's supported contract.",
            )
        )
    if not platform_supported:
        issues.append(
            _issue(
                severity="error",
                code="runtime.platform_unsupported",
                stage="platform",
                message=(
                    "The active platform is unsupported, translated, or its "
                    "native execution identity could not be verified."
                ),
            )
        )
    if not distribution_matches:
        issues.append(
            _issue(
                severity="warning",
                code="runtime.distribution_version_mismatch",
                stage="distribution",
                message="Installed package metadata does not match the imported Tianlai code.",
            )
        )
    if catalog_status != "ready":
        issues.append(
            _issue(
                severity="error",
                code=f"catalog.{catalog_status}",
                stage="catalog",
                message="The Tianlai instrument catalogue is not ready.",
            )
        )
    if trusted_status != "ready":
        issues.append(
            _issue(
                severity="error",
                code=f"trust.{trusted_status}",
                stage="trusted",
                message="The trusted-instrument policy is not ready.",
            )
        )
    for name in ("home", "resources", "output"):
        if not writability[name]["writable_estimate"]:
            issues.append(
                _issue(
                    severity="error" if name == "output" else "warning",
                    code=f"layout.{name}_not_writable",
                    stage="writability",
                    message=f"The configured Tianlai {name} location is not writable.",
                    location=name,
                )
            )

    instruments = report.get("instruments")
    safe_resource_rows: list[dict[str, Any]] = []
    if isinstance(instruments, list):
        for raw_item in instruments:
            item = raw_item if isinstance(raw_item, dict) else {}
            if item.get("production") is False:
                continue
            row = _safe_resource_row(item)
            if row is None:
                continue
            safe_resource_rows.append(row)
    # A clean lightweight tree may have 74 missing instrument entries backed by
    # only 15 frozen resource families.  Runtime diagnosis is an overview, so emit
    # one issue per family/status instead of flooding an MCP context.  The project
    # readiness helper below still reports exact requested instrument IDs.
    resource_issue_groups = Counter(
        (row["status"], row["resource_family"] or "untracked")
        for row in safe_resource_rows
        if row["status"] != "ready"
    )
    for (resource_status, family_id), instrument_count in sorted(
        resource_issue_groups.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        issues.append(
            _issue(
                severity="warning" if resource_status == "missing" else "error",
                code=f"resource.{resource_status}",
                stage="resources",
                message=(
                    "Instrument resources are missing."
                    if resource_status == "missing"
                    else "The instrument resource contract is invalid."
                ),
                resource_family=(None if family_id == "untracked" else family_id),
                instrument_count=instrument_count,
            )
        )

    restore_status = _status(
        restore.get("status"),
        frozenset({"available", "unavailable", "degraded"}),
        "unavailable",
    )
    fluidsynth_status = _status(
        fluidsynth.get("status"),
        frozenset({"available", "optional_missing", "not_probed", "error"}),
        "error",
    )
    if restore_status == "degraded":
        issues.append(
            _issue(
                severity="warning",
                code="capability.resource_restore_degraded",
                stage="capabilities",
                message="The optional resource-restore capability is degraded.",
            )
        )
    if fluidsynth_status == "error":
        issues.append(
            _issue(
                severity="warning",
                code="capability.fluidsynth_error",
                stage="capabilities",
                message="The optional FluidSynth capability failed its local probe.",
            )
        )

    summary_raw = (
        report.get("summary") if isinstance(report.get("summary"), dict) else {}
    )
    summary = {
        key: _safe_int(summary_raw.get(key))
        for key in (
            "catalog_count",
            "production_count",
            "test_utility_count",
            "trusted_count",
            "trusted_ready_count",
            "resource_ready_count",
            "resource_missing_count",
            "resource_invalid_count",
            "asset_backed_count",
            "self_contained_count",
        )
    }
    all_resources_ready = (
        summary["resource_missing_count"] == 0
        and summary["resource_invalid_count"] == 0
    )
    core_ready = (
        python_supported
        and platform_supported
        and catalog_status == "ready"
        and trusted_status == "ready"
        and writability["output"]["writable_estimate"]
        and summary["resource_invalid_count"] == 0
    )
    status = (
        "error"
        if not core_ready
        else "degraded"
        if issues or not all_resources_ready
        else "ready"
    )
    visible_issues, issue_counts, truncated = _page_issues(issues, limit)

    raw_source = str(raw_layout.get("source") or "")
    rosetta = platform.get("rosetta")
    rosetta = rosetta if isinstance(rosetta, dict) else {}
    macos_translation_identity = rosetta.get("probe_performed") is True
    native_source = str(native.get("source") or "")
    checks = {
        "python": {
            "status": "ready" if python_supported else "unsupported",
            "version": _safe_token(python.get("version")),
            "implementation": _safe_token(python.get("implementation")),
            "bits": _safe_int(python.get("bits")),
            "supported": python_supported,
        },
        "platform": {
            "status": "ready" if platform_supported else "unsupported",
            "system": _safe_token(platform.get("system")),
            "machine": _safe_token(platform.get("normalised_machine")),
            "supported": platform_supported,
            "rosetta": {
                "status": _status(
                    rosetta.get("status"),
                    frozenset(
                        {
                            "not_applicable",
                            "native",
                            "translated",
                            "not_probed",
                            "unknown",
                        }
                    ),
                    "unknown",
                ),
                "translated": (
                    rosetta.get("translated")
                    if isinstance(rosetta.get("translated"), bool)
                    else None
                ),
                "identity_check_performed": macos_translation_identity,
            },
        },
        "distribution": {
            "status": "ready" if distribution_matches else "mismatch",
            "name": "tianlai-audio",
            "version": _safe_token(distribution.get("version"), "not_installed"),
            "matches_imported_code": distribution_matches,
        },
        "layout": {
            "status": "ready" if catalog_status == "ready" else catalog_status,
            "source": (
                raw_source if raw_source in _SAFE_RUNTIME_SOURCES else "unknown"
            ),
            "catalog_ready": raw_layout.get("catalog_ready") is True,
        },
        "writability": writability,
        "catalog": {
            "status": catalog_status,
            "count": _safe_int(catalog.get("count")),
        },
        "trusted": {
            "status": trusted_status,
            "count": _safe_int(trusted.get("count")),
        },
        "resource_restore": {
            "status": restore_status,
            "family_count": _safe_int(restore.get("family_count")),
            "instrument_count": _safe_int(restore.get("instrument_count")),
            "archive_formats": sorted(
                {
                    str(item)
                    for item in (
                        restore.get("archive_formats")
                        if isinstance(restore.get("archive_formats"), list)
                        else []
                    )
                    if str(item) in _SAFE_ARCHIVE_FORMATS
                }
            ),
            "seven_zip_extractor_available": bool(
                restore.get("seven_zip_extractor")
            ),
            "seven_zip_extractor_status": _status(
                restore.get("seven_zip_extractor_status"),
                frozenset({"available", "missing", "not_required", "not_probed"}),
                "not_probed",
            ),
            "probe_performed": (
                restore.get("seven_zip_extractor_probe_performed") is True
            ),
        },
        "fluidsynth": {
            "status": fluidsynth_status,
            "ready": fluidsynth.get("ready") is True,
            "required_for_core": False,
            "native": {
                "status": _status(
                    native.get("status"),
                    frozenset(
                        {"available", "optional_missing", "not_probed", "error"}
                    ),
                    "error",
                ),
                "source": (
                    native_source
                    if native_source in _SAFE_NATIVE_SOURCES
                    else None
                ),
                "probe": _safe_token(native.get("probe"), "none"),
                "load_verified": native.get("load_verified") is True,
                "probe_performed": native.get("probe_performed") is True,
                "availability_estimate": _status(
                    native.get("availability_estimate"),
                    frozenset(
                        {
                            "candidate_present",
                            "candidate_not_found",
                            "not_inspected",
                            "unknown",
                        }
                    ),
                    "unknown",
                ),
            },
            "python_binding": {
                "status": _status(
                    binding.get("status"),
                    frozenset(
                        {
                            "available",
                            "optional_missing",
                            "not_probed",
                            "incompatible",
                            "error",
                        }
                    ),
                    "error",
                ),
                "module_available": binding.get("module_available") is True,
                "version": _safe_token(binding.get("version"), "not_installed"),
                "required_version": _safe_token(
                    binding.get("required_version"), "unknown"
                ),
            },
        },
    }
    return {
        "kind": "tianlai.runtime_diagnosis_result",
        "schema_version": _SCHEMA_VERSION,
        "ok": core_ready,
        "status": status,
        "core_ready": core_ready,
        "all_resources_ready": all_resources_ready,
        "check_level": check_level,
        "version": _safe_token(report.get("version")),
        "active_probes": {
            "native_library_probe": False,
            "external_program_probe": False,
            "ephemeral_writability_probe": False,
        },
        "passive_checks": {
            "filesystem_metadata": True,
            "instrument_reference_scan": check_level == "references",
            "macos_translation_identity": macos_translation_identity,
        },
        "network": False,
        "persistent_writes": False,
        "checks": checks,
        "summary": summary,
        "self_check": summarize_issues(issues),
        "issues": visible_issues,
        "issue_counts": issue_counts,
        "issues_truncated": truncated,
    }


def collect_instrument_resource_readiness(
    layout: RuntimeLayout,
    instrument_ids: Sequence[str],
    *,
    verify_references: bool = True,
    max_issues: int = 64,
) -> dict[str, Any]:
    """Return resource-reference readiness for only the requested instruments."""

    limit = _bounded_limit(max_issues, "max_issues")
    if isinstance(instrument_ids, str):
        raise ValueError("instrument_ids must be a sequence of relative IDs")
    if len(instrument_ids) > _MAX_SELECTOR_ITEMS:
        raise ValueError(
            f"instrument_ids may contain at most {_MAX_SELECTOR_ITEMS} items"
        )
    requested: list[str] = []
    seen: set[str] = set()
    invalid_count = 0
    for raw in instrument_ids:
        instrument_id = (
            _safe_relative_path(raw)
            if isinstance(raw, str) and len(raw) <= _MAX_SELECTOR_LENGTH
            else None
        )
        if instrument_id is None:
            invalid_count += 1
        elif instrument_id not in seen:
            seen.add(instrument_id)
            requested.append(instrument_id)

    report = collect_doctor_report(
        layout=layout,
        verify_references=bool(verify_references),
        active_probes=False,
        selected_instrument_ids=requested,
    )
    environment = _safe_render_environment(
        report,
        output_target=layout.output / "mcp",
    )
    raw_instruments = report.get("instruments")
    index: dict[str, dict[str, Any]] = {}
    if isinstance(raw_instruments, list):
        for raw in raw_instruments:
            row = _safe_resource_row(raw)
            if row is not None:
                index[row["instrument_id"]] = row

    issues: list[dict[str, Any]] = [
        _issue(
            severity="error",
            code="resource.instrument_id_invalid",
            stage="selection",
            message="A requested instrument ID is not a safe relative catalogue ID.",
        )
        for _ in range(invalid_count)
    ]
    rows: list[dict[str, Any]] = []
    restore_ids: list[str] = []
    for instrument_id in requested:
        row = index.get(instrument_id)
        if row is None:
            rows.append(
                {
                    "instrument_id": instrument_id,
                    "status": "unlisted",
                    "check_level": "not_run",
                    "asset_backed": None,
                    "resource_family": None,
                }
            )
            issues.append(
                _issue(
                    severity="error",
                    code="resource.instrument_unlisted",
                    stage="resources",
                    message="The requested instrument is not in the active catalogue.",
                    instrument_id=instrument_id,
                )
            )
            continue
        rows.append(row)
        if row["status"] == "missing":
            issues.append(
                _issue(
                    severity="error",
                    code="resource.missing",
                    stage="resources",
                    message="Required instrument resources are missing.",
                    instrument_id=instrument_id,
                    resource_family=row["resource_family"],
                )
            )
            if row["resource_family"] is not None:
                restore_ids.append(instrument_id)
        elif row["status"] == "invalid":
            issues.append(
                _issue(
                    severity="error",
                    code="resource.invalid",
                    stage="resources",
                    message="The required instrument resource contract is invalid.",
                    instrument_id=instrument_id,
                    resource_family=row["resource_family"],
                )
            )

    if not environment["python_supported"]:
        issues.append(
            _issue(
                severity="error",
                code="runtime.python_unsupported",
                stage="render_environment",
                message="The active Python runtime is outside Tianlai's supported contract.",
            )
        )
    if not environment["platform_supported"]:
        issues.append(
            _issue(
                severity="error",
                code="runtime.platform_unsupported_or_unverified",
                stage="render_environment",
                message=(
                    "The active platform is unsupported, translated, or its "
                    "native execution identity could not be verified."
                ),
            )
        )
    if not environment["output"]["writable_estimate"]:
        issues.append(
            _issue(
                severity="error",
                code="layout.output_not_writable",
                stage="render_environment",
                message="The configured Tianlai output location is not writable.",
            )
        )
    counts = Counter(row["status"] for row in rows)
    ready = (
        invalid_count == 0
        and len(rows) == len(requested)
        and bool(rows or not requested)
        and all(row["status"] == "ready" for row in rows)
    )
    environment_ready = environment["ready_for_render_attempt"] is True
    overall_ready = ready and environment_ready
    status = (
        "ready"
        if overall_ready
        else "environment_blocked"
        if ready
        else "invalid"
        if invalid_count or counts.get("invalid") or counts.get("unlisted")
        else "missing"
    )
    visible, issue_counts, truncated = _page_issues(issues, limit)
    return {
        "kind": "tianlai.instrument_resource_readiness_result",
        "schema_version": _SCHEMA_VERSION,
        "ok": overall_ready,
        "status": status,
        "resource_references_ready": ready,
        "render_environment_ready": environment_ready,
        "verify_references": bool(verify_references),
        "network": False,
        "persistent_writes": False,
        "summary": {
            "required_count": len(rows),
            "ready_count": counts.get("ready", 0),
            "missing_count": counts.get("missing", 0),
            "invalid_count": counts.get("invalid", 0),
            "unlisted_count": counts.get("unlisted", 0),
        },
        "render_environment": environment,
        "active_probes": {
            "native_library_probe": False,
            "external_program_probe": False,
            "ephemeral_writability_probe": False,
        },
        "passive_checks": {
            "filesystem_metadata": True,
            "selected_instrument_reference_scan": bool(verify_references),
            "macos_translation_identity": environment[
                "macos_translation_identity_check_performed"
            ],
        },
        "instruments": rows,
        "restore_plan_handoff": {"instrument_ids": restore_ids},
        "self_check": summarize_issues(issues),
        "issues": visible,
        "issue_counts": issue_counts,
        "issues_truncated": truncated,
    }


def _normalise_selector_values(
    values: Sequence[str] | None,
    *,
    allow_slash: bool,
) -> tuple[list[str], int]:
    if values is None:
        return [], 0
    if isinstance(values, str):
        return [], 1
    if len(values) > _MAX_SELECTOR_ITEMS:
        raise ValueError(
            f"selector lists may contain at most {_MAX_SELECTOR_ITEMS} items"
        )
    valid: list[str] = []
    seen: set[str] = set()
    invalid = 0
    for raw in values:
        if not isinstance(raw, str) or len(raw) > _MAX_SELECTOR_LENGTH:
            invalid += 1
            continue
        value = _safe_relative_path(raw) if allow_slash else _safe_identifier(raw)
        if value is None:
            invalid += 1
        elif value not in seen:
            seen.add(value)
            valid.append(value)
    return valid, invalid


def build_safe_resource_restore_plan(
    layout: RuntimeLayout,
    *,
    instrument_ids: Sequence[str] | None = None,
    family_ids: Sequence[str] | None = None,
    groups: Sequence[str] | None = None,
    max_items: int = 64,
) -> dict[str, Any]:
    """Build a manifest-ordered, non-mutating and path-safe restore plan."""

    limit = _bounded_limit(max_items, "max_items")
    requested_instruments, invalid_instruments = _normalise_selector_values(
        instrument_ids,
        allow_slash=True,
    )
    requested_families, invalid_families = _normalise_selector_values(
        family_ids,
        allow_slash=False,
    )
    requested_groups, invalid_groups = _normalise_selector_values(
        groups,
        allow_slash=False,
    )
    if (
        len(requested_instruments)
        + len(requested_families)
        + len(requested_groups)
        > _MAX_SELECTOR_ITEMS
    ):
        raise ValueError(
            f"all selectors together may contain at most {_MAX_SELECTOR_ITEMS} "
            "unique items"
        )
    manifest = load_restore_manifest(home=layout.home)
    families = manifest["families"]
    known_family_ids = {family["id"] for family in families}
    known_groups = {family["group"] for family in families}

    issues: list[dict[str, Any]] = []
    for selector, count in (
        ("instrument", invalid_instruments),
        ("family", invalid_families),
        ("group", invalid_groups),
    ):
        issues.extend(
            _issue(
                severity="error",
                code=f"selection.invalid_{selector}_id",
                stage="selection",
                message=f"A {selector} selector is not a safe relative identifier.",
            )
            for _ in range(count)
        )

    selected_ids: set[str] = set()
    unknown_instruments: list[str] = []
    for instrument_id in requested_instruments:
        family = family_for_instrument(manifest, instrument_id)
        if family is None:
            unknown_instruments.append(instrument_id)
        else:
            selected_ids.add(family["id"])

    unknown_families = [
        family_id
        for family_id in requested_families
        if family_id not in known_family_ids
    ]
    unknown_groups = [group for group in requested_groups if group not in known_groups]
    known_requested_families = [
        family_id
        for family_id in requested_families
        if family_id in known_family_ids
    ]
    known_requested_groups = [
        group for group in requested_groups if group in known_groups
    ]
    selected_by_family_or_group = select_families(
        manifest,
        family_ids=known_requested_families,
        groups=known_requested_groups,
    )
    # select_families intentionally means "all" for an empty selection.  In a
    # mixed request where family/group selectors are absent, only instrument
    # mappings should be used instead.
    if known_requested_families or known_requested_groups:
        selected_ids.update(family["id"] for family in selected_by_family_or_group)

    explicit_selection = bool(
        requested_instruments
        or requested_families
        or requested_groups
        or invalid_instruments
        or invalid_families
        or invalid_groups
    )
    if not explicit_selection:
        selected_ids.update(family["id"] for family in select_families(manifest))

    for selector, values in (
        ("instrument", unknown_instruments),
        ("family", unknown_families),
        ("group", unknown_groups),
    ):
        issues.extend(
            _issue(
                severity="error",
                code=f"selection.unknown_{selector}",
                stage="selection",
                message=f"The requested {selector} selector is unknown.",
                selector=value,
            )
            for value in values
        )

    selected = [family for family in families if family["id"] in selected_ids]
    raw_plan = build_restore_plan(
        selected,
        resource_root=layout.resources,
    )
    safe_items: list[dict[str, Any]] = []
    for raw in raw_plan["items"]:
        family_id = _safe_identifier(raw.get("family_id")) or "unknown"
        source_state = _status(
            raw.get("source_state"),
            frozenset({"missing", "present_unverified", "conflict"}),
            "conflict",
        )
        if source_state == "conflict":
            issues.append(
                _issue(
                    severity="error",
                    code="resource.target_conflict",
                    stage="plan",
                    message="A resource target exists but is not a plain directory.",
                    family_id=family_id,
                )
            )
        license_status = _status(
            raw.get("license_status"),
            frozenset({"approved", "grandfathered"}),
            "grandfathered",
        )
        safe_items.append(
            {
                "family_id": family_id,
                "group": _safe_identifier(raw.get("group")) or "unknown",
                "display_name": _safe_public_text(
                    raw.get("display_name"),
                    fallback=f"Resource family {family_id}",
                ),
                "instrument_count": _safe_int(raw.get("instrument_count")),
                "target": _safe_relative_path(raw.get("target")),
                "source_state": source_state,
                "archive_count": _safe_int(raw.get("archive_count")),
                "archives_cached_count": _safe_int(
                    raw.get("archives_cached_count")
                ),
                "archive_cached": raw.get("archive_cached") is True,
                "estimated_download_bytes": _safe_int(
                    raw.get("estimated_download_bytes")
                ),
                "installed_bytes": _safe_int(raw.get("installed_bytes")),
                "derived": [
                    {
                        "target": _safe_relative_path(derived.get("target")),
                        "state": _status(
                            derived.get("state"),
                            frozenset(
                                {"missing", "present_unverified", "conflict"}
                            ),
                            "conflict",
                        ),
                    }
                    for derived in raw.get("derived", [])
                    if isinstance(derived, dict)
                ],
                "license": {
                    "expression": _safe_public_text(
                        raw.get("license"),
                        fallback="license-review-required",
                    ),
                    "status": license_status,
                },
            }
        )

    visible_issues, issue_counts, issues_truncated = _page_issues(issues, limit)
    returned_items = safe_items[:limit]
    return {
        "kind": "tianlai.resource_restore_plan_result",
        "schema_version": _SCHEMA_VERSION,
        "ok": not any(item["severity"] == "error" for item in issues),
        "status": (
            "blocked"
            if any(item["severity"] == "error" for item in issues)
            else "ready"
        ),
        "network": False,
        "persistent_writes": False,
        "downloads_started": False,
        "restore_started": False,
        "selection": {
            "default_all": not explicit_selection,
            "instrument_ids": requested_instruments,
            "family_ids": requested_families,
            "groups": requested_groups,
            "selected_family_ids": [item["family_id"] for item in safe_items],
        },
        "summary": {
            "family_count": _safe_int(raw_plan.get("family_count")),
            "instrument_count": _safe_int(raw_plan.get("instrument_count")),
            "estimated_download_bytes": _safe_int(
                raw_plan.get("estimated_download_bytes")
            ),
            "additional_installed_bytes": _safe_int(
                raw_plan.get("additional_installed_bytes")
            ),
        },
        "families": returned_items,
        "families_truncated": len(safe_items) > limit,
        "issues": visible_issues,
        "issue_counts": issue_counts,
        "issues_truncated": issues_truncated,
    }


__all__ = [
    "build_safe_resource_restore_plan",
    "collect_instrument_resource_readiness",
    "collect_runtime_diagnosis",
]
