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
import ctypes
import ctypes.util
import errno
from functools import lru_cache
import importlib.metadata
import importlib.util
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
    _find_bsdtar_executable,
    default_manifest_path,
    load_restore_manifest,
)
from .runtime_layout import RuntimeLayout, RuntimeLayoutError, discover_runtime_layout
from .soundfont import (
    _find_project_fluidsynth_directory,
    _find_tianlai_runtime_root,
    _macos_homebrew_prefixes,
    _native_fluidsynth_libraries,
)
from .trust import TrustPolicyError, load_trusted_instruments


REPORT_SCHEMA_VERSION = 3

_PYFLUIDSYNTH_REQUIRED_VERSION = "1.4.0"
_FLUIDSYNTH_LIBRARY_PROBES = (
    "fluidsynth",
    "libfluidsynth",
    "libfluidsynth-3",
    "libfluidsynth-2",
    "libfluidsynth-1",
)

_SUPPORTED_PLATFORM_ARCHITECTURES = {
    "Windows": frozenset({"amd64", "x86_64"}),
    "Linux": frozenset({"amd64", "x86_64"}),
    "Darwin": frozenset({"arm64", "aarch64", "x86_64", "amd64"}),
}

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


def _normalised_machine(machine: str) -> str:
    value = machine.strip().casefold().replace("-", "_")
    aliases = {
        "x64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
    }
    return aliases.get(value, value)


def _platform_runtime_supported(
    *,
    system: str,
    machine: str,
    bits: int,
) -> bool:
    """Return whether this OS/architecture pair is in the public test contract."""

    if bits != 64:
        return False
    supported = _SUPPORTED_PLATFORM_ARCHITECTURES.get(system)
    if supported is None:
        return False
    normalised = _normalised_machine(machine)
    return normalised in {
        _normalised_machine(candidate) for candidate in supported
    }


def _passive_platform_identity() -> tuple[str, str, str, str]:
    """Return OS identity without invoking :mod:`platform` command fallbacks."""

    if os.name == "nt":
        system = "Windows"
        machine = (
            os.environ.get("PROCESSOR_ARCHITEW6432")
            or os.environ.get("PROCESSOR_ARCHITECTURE")
            or "unknown"
        )
        get_windows_version = getattr(sys, "getwindowsversion", None)
        if callable(get_windows_version):
            version = get_windows_version()
            release = f"{version.major}.{version.minor}.{version.build}"
        else:
            release = "unknown"
    elif hasattr(os, "uname"):
        identity = os.uname()
        system = {
            "darwin": "Darwin",
            "linux": "Linux",
        }.get(sys.platform.casefold(), identity.sysname)
        machine = identity.machine or "unknown"
        release = identity.release or "unknown"
    else:
        system = {
            "darwin": "Darwin",
            "linux": "Linux",
        }.get(sys.platform.casefold(), sys.platform or "unknown")
        machine = "unknown"
        release = "unknown"
    description = f"{system}-{release}-{machine}"
    return system, release, machine, description


def _probe_macos_rosetta_translation() -> bool:
    """Query the current macOS process without spawning a shell."""

    libc = ctypes.CDLL(None, use_errno=True)
    sysctlbyname = libc.sysctlbyname
    sysctlbyname.argtypes = (
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.c_void_p,
        ctypes.c_size_t,
    )
    sysctlbyname.restype = ctypes.c_int
    translated = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(translated))
    result = sysctlbyname(
        b"sysctl.proc_translated",
        ctypes.byref(translated),
        ctypes.byref(size),
        None,
        0,
    )
    if result == 0:
        return translated.value == 1
    error_number = ctypes.get_errno()
    if error_number == errno.ENOENT:
        # The key is absent on native Intel systems.
        return False
    raise OSError(error_number, os.strerror(error_number))


def _macos_rosetta_capability(
    *,
    system: str,
    machine: str,
    bits: int,
    active_probes: bool = True,
) -> dict[str, Any]:
    """Describe Rosetta separately from the supported process architecture."""

    process_architecture = _normalised_machine(machine)
    if system != "Darwin":
        return {
            "status": "not_applicable",
            "translated": None,
            "process_architecture": process_architecture,
            "host_architecture": None,
            "supported": None,
            "probe_performed": False,
            "error": None,
        }

    supported = _platform_runtime_supported(
        system=system,
        machine=machine,
        bits=bits,
    )
    if process_architecture != "x86_64":
        return {
            "status": "native",
            "translated": False,
            "process_architecture": process_architecture,
            "host_architecture": process_architecture,
            "supported": supported,
            "probe_performed": False,
            "error": None,
        }

    if not active_probes:
        return {
            "status": "not_probed",
            "translated": None,
            "process_architecture": process_architecture,
            "host_architecture": None,
            # An x86_64 process on macOS may be native Intel or translated by
            # Rosetta.  Passive mode cannot distinguish the two and therefore
            # must not claim that the native-only public contract is verified.
            "supported": False,
            "probe_performed": False,
            "error": None,
        }

    try:
        translated = _probe_macos_rosetta_translation()
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        return {
            "status": "unknown",
            "translated": None,
            "process_architecture": process_architecture,
            "host_architecture": None,
            # The public macOS contract is native-only.  If an x86_64 process
            # cannot prove whether it is native Intel or Rosetta-translated,
            # do not advertise it as a supported runtime.
            "supported": False,
            "probe_performed": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": "translated" if translated else "native",
        "translated": translated,
        "process_architecture": process_architecture,
        "host_architecture": "arm64" if translated else process_architecture,
        "supported": supported and not translated,
        "probe_performed": True,
        "error": None,
    }


def _same_resolved_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except (OSError, RuntimeError):
        return False


def _fluidsynth_directory_source(directory: Path, home: Path) -> str:
    """Describe the selected directory without reimplementing its selection."""

    runtime_root = _find_tianlai_runtime_root(home)
    if runtime_root is not None:
        runtime = runtime_root / "音源" / "通用" / "fluidsynth"
        if any(
            _same_resolved_path(directory, candidate)
            for candidate in (runtime / "bin", runtime / "lib", runtime)
        ):
            return "project_local"

    override = os.environ.get("TIANLAI_FLUIDSYNTH_DIR")
    if override:
        runtime = Path(override).expanduser()
        if any(
            _same_resolved_path(directory, candidate)
            for candidate in (runtime / "bin", runtime / "lib", runtime)
        ):
            return "environment_override"

    for prefix in _macos_homebrew_prefixes():
        if any(
            _same_resolved_path(directory, candidate)
            for candidate in (
                prefix / "opt" / "fluid-synth" / "lib",
                prefix / "lib",
            )
        ):
            return "homebrew"
    return "configured_directory"


def _load_native_fluidsynth_library(library: str | Path) -> None:
    """Load one candidate and verify a required FluidSynth API symbol."""

    value = os.fspath(library)
    loader = (
        getattr(ctypes, "WinDLL", ctypes.CDLL)
        if value.casefold().endswith(".dll")
        else ctypes.CDLL
    )
    handle = loader(value)
    try:
        getattr(handle, "new_fluid_settings")
    except AttributeError as exc:
        raise OSError(
            "loaded library does not export new_fluid_settings"
        ) from exc


def _native_fluidsynth_capability(
    layout: RuntimeLayout,
    *,
    active_probes: bool = True,
) -> dict[str, Any]:
    """Mirror discovery and prove the selected native library can be loaded."""

    if not active_probes:
        # Candidate discovery on macOS consults host-specific search locations.
        # A strict passive MCP report does not need to resolve or touch them;
        # operator-side doctor mode remains the authority for native readiness.
        return {
            "status": "not_probed",
            "library": None,
            "directory": None,
            "source": None,
            "probe": None,
            "load_verified": False,
            "probe_performed": False,
            "availability_estimate": "not_inspected",
            "error": None,
        }

    try:
        directory = _find_project_fluidsynth_directory(layout.home)
        if directory is not None:
            libraries = _native_fluidsynth_libraries(directory)
            if libraries:
                library = libraries[0].resolve()
                source = _fluidsynth_directory_source(directory, layout.home)
                try:
                    _load_native_fluidsynth_library(library)
                except (AttributeError, OSError, TypeError, ValueError) as exc:
                    return {
                        "status": "error",
                        "library": str(library),
                        "directory": str(directory.resolve()),
                        "source": source,
                        "probe": None,
                        "load_verified": False,
                        "probe_performed": True,
                        "availability_estimate": "candidate_present",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                return {
                    "status": "available",
                    "library": str(library),
                    "directory": str(directory.resolve()),
                    "source": source,
                    "probe": None,
                    "load_verified": True,
                    "probe_performed": True,
                    "availability_estimate": "candidate_present",
                    "error": None,
                }

        # When Tianlai has no selected runtime directory, pyfluidsynth probes
        # these names in this exact order during its normal import path.
        load_errors: list[tuple[str, str, str]] = []
        for probe in _FLUIDSYNTH_LIBRARY_PROBES:
            library = ctypes.util.find_library(probe)
            if library:
                try:
                    _load_native_fluidsynth_library(library)
                except (AttributeError, OSError, TypeError, ValueError) as exc:
                    load_errors.append(
                        (probe, str(library), f"{type(exc).__name__}: {exc}")
                    )
                    continue
                return {
                    "status": "available",
                    "library": str(library),
                    "directory": None,
                    "source": "system_lookup",
                    "probe": probe,
                    "load_verified": True,
                    "probe_performed": True,
                    "availability_estimate": "candidate_present",
                    "error": None,
                }
        if load_errors:
            probe, library, _ = load_errors[0]
            return {
                "status": "error",
                "library": library,
                "directory": None,
                "source": "system_lookup",
                "probe": probe,
                "load_verified": False,
                "probe_performed": True,
                "availability_estimate": "candidate_present",
                "error": "; ".join(
                    f"{candidate_probe} -> {candidate_library}: {error}"
                    for candidate_probe, candidate_library, error in load_errors
                ),
            }
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "error",
            "library": None,
            "directory": None,
            "source": None,
            "probe": None,
            "load_verified": False,
            "probe_performed": False,
            "availability_estimate": "unknown",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "status": "optional_missing",
        "library": None,
        "directory": None,
        "source": None,
        "probe": None,
        "load_verified": False,
        "probe_performed": False,
        "availability_estimate": "candidate_not_found",
        "error": None,
    }


def _pyfluidsynth_capability() -> dict[str, Any]:
    """Inspect the Python binding without importing or loading its native code."""

    module_available = "fluidsynth" in sys.modules
    module_error: str | None = None
    if not module_available:
        try:
            module_available = importlib.util.find_spec("fluidsynth") is not None
        except (ImportError, AttributeError, ValueError) as exc:
            module_error = f"{type(exc).__name__}: {exc}"

    try:
        version = importlib.metadata.version("pyfluidsynth")
    except importlib.metadata.PackageNotFoundError:
        version = None
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "error",
            "distribution": "pyfluidsynth",
            "module": "fluidsynth",
            "module_available": module_available,
            "version": None,
            "required_version": _PYFLUIDSYNTH_REQUIRED_VERSION,
            "error": f"{type(exc).__name__}: {exc}",
        }

    if module_available and version == _PYFLUIDSYNTH_REQUIRED_VERSION:
        status = "available"
        error = None
    elif not module_available and version is None and module_error is None:
        status = "optional_missing"
        error = None
    else:
        status = "incompatible"
        details: list[str] = []
        if not module_available:
            details.append("the fluidsynth module is not importable")
        if version is None:
            details.append("the pyfluidsynth distribution version is unavailable")
        elif version != _PYFLUIDSYNTH_REQUIRED_VERSION:
            details.append(
                "pyfluidsynth "
                f"{version} is installed; {_PYFLUIDSYNTH_REQUIRED_VERSION} is required"
            )
        if module_error is not None:
            details.append(module_error)
        error = "; ".join(details)
    return {
        "status": status,
        "distribution": "pyfluidsynth",
        "module": "fluidsynth",
        "module_available": module_available,
        "version": version,
        "required_version": _PYFLUIDSYNTH_REQUIRED_VERSION,
        "error": error,
    }


def _passive_pyfluidsynth_capability() -> dict[str, Any]:
    """Inspect distribution metadata without invoking import-system finders."""

    try:
        version = importlib.metadata.version("pyfluidsynth")
    except importlib.metadata.PackageNotFoundError:
        version = None
    except (OSError, RuntimeError, ValueError):
        version = None
    return {
        "status": "not_probed",
        "distribution": "pyfluidsynth",
        "module": "fluidsynth",
        "module_available": "fluidsynth" in sys.modules,
        "version": version,
        "required_version": _PYFLUIDSYNTH_REQUIRED_VERSION,
        "error": None,
    }


def _fluidsynth_capability(
    layout: RuntimeLayout,
    *,
    active_probes: bool = True,
) -> dict[str, Any]:
    native = _native_fluidsynth_capability(
        layout,
        active_probes=active_probes,
    )
    binding = (
        _pyfluidsynth_capability()
        if active_probes
        else _passive_pyfluidsynth_capability()
    )
    ready = native["status"] == "available" and binding["status"] == "available"
    broken = native["status"] == "error" or binding["status"] in {
        "error",
        "incompatible",
    }
    return {
        "status": (
            "not_probed"
            if not active_probes
            else "available"
            if ready
            else "error"
            if broken
            else "optional_missing"
        ),
        "ready": ready,
        "readiness_verified": active_probes,
        "required_for_core": False,
        # Retain the previous convenience field while exposing both independent
        # layers explicitly for machine consumers.
        "library": native["library"],
        "native": native,
        "python_binding": binding,
    }


def _platform_capabilities(
    layout: RuntimeLayout,
    *,
    active_probes: bool = True,
) -> dict[str, Any]:
    restore_manifest = default_manifest_path(layout.home)
    resource_restore: dict[str, Any] = {
        "status": "unavailable",
        "manifest": _relative_or_absolute(restore_manifest, layout.home),
        "family_count": 0,
        "instrument_count": 0,
        "archive_formats": [],
        "seven_zip_extractor": None,
        "seven_zip_extractor_status": "not_required",
        "seven_zip_extractor_probe_performed": False,
        "error": None,
    }
    if restore_manifest.is_file():
        try:
            manifest = load_restore_manifest(restore_manifest)
            archives = [
                archive
                for family in manifest["families"]
                for archive in family.get("archives", [family.get("archive")])
                if archive is not None
            ]
            formats = sorted({archive["format"] for archive in archives})
            resource_restore.update(
                {
                    "family_count": manifest["totals"]["family_count"],
                    "instrument_count": manifest["totals"]["instrument_count"],
                    "archive_formats": formats,
                }
            )
            extractor: str | None = None
            if "7z" in formats:
                if active_probes:
                    extractor = _find_bsdtar_executable()
            resource_restore.update(
                {
                    "status": "available",
                    "seven_zip_extractor": extractor,
                    "seven_zip_extractor_status": (
                        "not_probed"
                        if "7z" in formats and not active_probes
                        else "available"
                        if extractor is not None
                        else "missing"
                        if "7z" in formats
                        else "not_required"
                    ),
                    "seven_zip_extractor_probe_performed": (
                        "7z" in formats and active_probes
                    ),
                }
            )
        except (OSError, RuntimeError, ValueError, ResourceRestoreError) as exc:
            resource_restore["status"] = "degraded"
            resource_restore["error"] = f"{type(exc).__name__}: {exc}"

    return {
        "resource_restore": resource_restore,
        "fluidsynth": _fluidsynth_capability(
            layout,
            active_probes=active_probes,
        ),
    }


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
        instrument_id = _canonical_relative(
            manifest_path.parent,
            layout.catalog,
        ).as_posix()
    except (OSError, RuntimeError, ValueError, ResourceRestoreError):
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


def _canonical_relative(path: Path, root: Path) -> Path:
    """Relativise canonical identities, including Windows 8.3 aliases."""

    return path.resolve(strict=False).relative_to(root.resolve(strict=False))


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return _canonical_relative(path, root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return str(path)


def _directory_writability(path: Path) -> dict[str, Any]:
    """Probe the directory, or its nearest parent when it is not created yet."""

    requested = path.resolve()
    if requested.exists() and not requested.is_dir():
        return {
            "path": str(requested),
            "exists": True,
            "writable": False,
            "writable_estimate": False,
            "writability_status": "unwritable",
            "probe_performed": False,
            "verification": "filesystem_metadata",
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
            "writable_estimate": False,
            "writability_status": "unwritable",
            "probe_performed": False,
            "verification": "filesystem_metadata",
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
        "writable_estimate": writable,
        "writability_status": (
            "verified_writable" if writable else "verified_unwritable"
        ),
        "probe_performed": True,
        "verification": "active_probe",
        "probe_directory": str(probe_directory),
        "error": error,
    }


def _passive_directory_writability(path: Path) -> dict[str, Any]:
    """Estimate writability from metadata without creating or deleting files."""

    requested = path.resolve()
    if requested.exists() and not requested.is_dir():
        return {
            "path": str(requested),
            "exists": True,
            "writable": None,
            "writable_estimate": False,
            "writability_status": "not_probed",
            "probe_performed": False,
            "verification": "passive_estimate",
            "probe_directory": None,
            "error": "path exists but is not a directory",
        }

    probe_directory = requested
    while not probe_directory.exists() and probe_directory != probe_directory.parent:
        probe_directory = probe_directory.parent
    available = probe_directory.is_dir()
    return {
        "path": str(requested),
        "exists": requested.is_dir(),
        # Keep verified writability explicitly unknown.  ``os.access`` is only
        # a permission/identity estimate and cannot prove that a future create
        # will succeed (ACLs, quotas and read-only mounts may still intervene).
        "writable": None,
        "writable_estimate": bool(
            available and os.access(probe_directory, os.W_OK)
        ),
        "writability_status": "not_probed",
        "probe_performed": False,
        "verification": "passive_estimate",
        "probe_directory": str(probe_directory) if available else None,
        "error": None if available else "no existing parent directory is available",
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

    tracked = _tracked_resource_installer(
        layout=layout,
        manifest_path=manifest_path,
    )
    if tracked is not None:
        return tracked

    # Per-entry PowerShell launchers remain backwards-compatible fallbacks,
    # but the frozen cross-platform restore manifest is authoritative whenever
    # an entry is mapped there.
    local = manifest_path.parent / "获取音源.ps1"
    if local.is_file():
        return _windows_only_installer(
            layout=layout,
            path=local,
            installer_id=manifest_path.parent.name,
        )

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
        _relative_or_absolute(
            Path(entry.manifest_path).parent,
            catalog_root,
        ): entry
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


def _selected_instrument_ids(
    values: Iterable[str] | None,
) -> frozenset[str] | None:
    if values is None:
        return None
    if isinstance(values, str):
        values = (values,)
    return frozenset(
        str(value).strip().replace("\\", "/").strip("/")
        for value in values
        if str(value).strip().replace("\\", "/").strip("/")
    )


def collect_doctor_report(
    *,
    start: str | Path | None = None,
    layout: RuntimeLayout | None = None,
    verify_references: bool = True,
    active_probes: bool = True,
    selected_instrument_ids: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable runtime, catalogue and resource report.

    ``active_probes=False`` performs no create/delete writability probe, native
    library load, archive-tool process discovery or Rosetta sysctl query.
    ``selected_instrument_ids`` limits manifest/resource/SFZ inspection to the
    named catalogue entries; unknown IDs are intentionally ignored here so a
    calling protocol can apply its own validation policy.
    """

    layout = layout or discover_runtime_layout(start=start)
    all_entries, catalog_errors = _catalog_entries(layout)
    trusted, trusted_report = _load_allowlist(
        layout.allowlist,
        all_entries,
        catalog_root=layout.catalog,
    )
    selected = _selected_instrument_ids(selected_instrument_ids)
    if selected is None:
        entries = all_entries
    else:
        entries = [
            entry
            for entry in all_entries
            if _relative_or_absolute(
                Path(entry.manifest_path).parent,
                layout.catalog,
            )
            in selected
        ]

    instruments: list[dict[str, Any]] = []
    for entry in entries:
        manifest_path = Path(entry.manifest_path)
        relative = _relative_or_absolute(
            manifest_path.parent,
            layout.catalog,
        )
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
    python_version = sys.version_info[:2]
    python_bits = struct.calcsize("P") * 8
    if active_probes:
        implementation = platform.python_implementation()
        python_version_text = platform.python_version()
        platform_system = platform.system()
        platform_release = platform.release()
        platform_machine = platform.machine()
        platform_description = platform.platform()
    else:
        implementation_name = getattr(sys.implementation, "name", "")
        implementation = (
            "CPython"
            if implementation_name.casefold() == "cpython"
            else implementation_name or "unknown"
        )
        python_version_text = ".".join(
            str(value) for value in sys.version_info[:3]
        )
        (
            platform_system,
            platform_release,
            platform_machine,
            platform_description,
        ) = _passive_platform_identity()
    python_supported = _python_runtime_supported(
        implementation=implementation,
        version=python_version,
        bits=python_bits,
    )
    platform_supported = _platform_runtime_supported(
        system=platform_system,
        machine=platform_machine,
        bits=python_bits,
    )
    rosetta = _macos_rosetta_capability(
        system=platform_system,
        machine=platform_machine,
        bits=python_bits,
        active_probes=active_probes,
    )
    if platform_system == "Darwin":
        platform_supported = platform_supported and rosetta["supported"] is True
    platform_capabilities = _platform_capabilities(
        layout,
        active_probes=active_probes,
    )
    if not python_supported or not platform_supported:
        summary["status"] = "error"
    elif (
        (
            platform_capabilities["fluidsynth"]["status"] == "error"
            or platform_capabilities["resource_restore"]["status"]
            == "degraded"
        )
        and summary["status"] == "ready"
    ):
        # Optional capabilities degrade rather than invalidate the core
        # runtime, but must not be hidden behind an overall ``ready`` label.
        summary["status"] = "degraded"
    writability = {
        name: (
            _directory_writability(path)
            if active_probes
            else _passive_directory_writability(path)
        )
        for name, path in (
            ("home", layout.home),
            ("resources", layout.resources),
            ("output", layout.output),
        )
    }
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
            "version": python_version_text,
            "implementation": implementation,
            "executable": sys.executable,
            "bits": python_bits,
            "supported": python_supported,
        },
        "platform": {
            "system": platform_system,
            "release": platform_release,
            "machine": platform_machine,
            "normalised_machine": _normalised_machine(platform_machine),
            "platform": platform_description,
            "supported": platform_supported,
            "rosetta": rosetta,
            "validated_architectures": sorted(
                {
                    _normalised_machine(candidate)
                    for candidate in _SUPPORTED_PLATFORM_ARCHITECTURES.get(
                        platform_system,
                        (),
                    )
                }
            ),
        },
        "probe_policy": {
            "active_probes": active_probes,
            "native_library_load_performed": bool(
                platform_capabilities.get("fluidsynth", {})
                .get("native", {})
                .get("probe_performed")
            ),
            "archive_tool_probe_performed": bool(
                platform_capabilities.get("resource_restore", {}).get(
                    "seven_zip_extractor_probe_performed"
                )
            ),
            "rosetta_probe_performed": bool(rosetta.get("probe_performed")),
            "writability_probe_performed": any(
                item["probe_performed"] for item in writability.values()
            ),
        },
        "capabilities": platform_capabilities,
        "layout": layout.to_dict(),
        "writability": writability,
        "catalog": {
            "status": catalog_status,
            "count": len(instruments),
            "total_count": len(all_entries),
            "errors": catalog_errors,
        },
        "selection": {
            "active": selected is not None,
            "requested_count": len(selected) if selected is not None else None,
            "matched_count": len(entries),
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


def _print_json_utf8(payload: str) -> None:
    """Emit machine-readable CLI JSON with deterministic UTF-8 bytes.

    Windows uses the active legacy code page for redirected stdout unless the
    stream is reconfigured explicitly.  That makes otherwise valid JSON fail
    as soon as a path or diagnostic contains non-ASCII text.  Real text
    streams support ``reconfigure``; in-process tests commonly redirect stdout
    to ``StringIO``, where writing the Unicode string directly is sufficient.
    """

    stream = sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    print(payload, file=stream)


def _human_summary(report: dict[str, Any]) -> str:
    summary = report["summary"]
    layout = report["layout"]
    fluidsynth = report["capabilities"]["fluidsynth"]
    status = str(summary["status"])
    if status == "ready":
        result_label = "已就绪"
    elif status == "degraded" and summary["resource_missing_count"]:
        result_label = "可运行（按需补充乐器资源）"
    elif status == "degraded":
        result_label = "可运行（有可选能力提醒）"
    else:
        result_label = "需要处理"

    lines = [
        f"天籁 {report['version']} 自检：{result_label}",
        f"机器状态：{status}",
        "",
        "[运行环境]",
        (
            f"  - Python {report['python']['version']} "
            f"({report['python']['bits']}-bit，"
            f"{report['python']['implementation']})："
            f"{'支持' if report['python']['supported'] else '不支持'}"
        ),
        (
            f"  - {report['platform']['system']} {report['platform']['release']} "
            f"({report['platform']['normalised_machine']})："
            f"{'支持' if report['platform']['supported'] else '不支持'}"
        ),
        f"  - 项目目录：{layout['home']}（来源：{layout['source']}）",
        (
            f"  - 乐器目录：共 {summary['catalog_count']} 件，"
            f"正式 {summary['production_count']} 件，"
            f"策展子集 {summary['trusted_count']} 件"
        ),
    ]
    if report["platform"]["system"] == "Darwin":
        rosetta = report["platform"]["rosetta"]
        lines.append(
            "  - macOS 进程模式："
            f"{rosetta['status']}（进程={rosetta['process_architecture']}，"
            f"宿主={rosetta['host_architecture'] or 'unknown'}）"
        )

    mandatory: list[str] = []
    if not report["python"]["supported"]:
        mandatory.append(
            "当前 Python 运行时不在支持范围内；请改用受支持的 64 位 CPython。"
        )
    if not report["platform"]["supported"]:
        mandatory.append(
            "当前操作系统或处理器架构不在支持范围内。"
        )
    if report["catalog"]["status"] != "ready":
        message = f"乐器目录状态为 {report['catalog']['status']}"
        catalog_errors = report["catalog"].get("errors", [])
        if catalog_errors:
            message += f"：{catalog_errors[0].get('message', '请检查目录内容')}"
        mandatory.append(message)
    if report["trusted"]["status"] != "ready":
        message = f"可信乐器策略状态为 {report['trusted']['status']}"
        if report["trusted"].get("error"):
            message += f"：{report['trusted']['error']}"
        mandatory.append(message)

    invalid = [
        item
        for item in report["instruments"]
        if item.get("production") is not False
        and item["resource"]["status"] == "invalid"
    ]
    if invalid:
        mandatory.append(
            f"{len(invalid)} 件正式乐器的资源合同无效；"
            "请修复清单、许可/来源证据或资源引用。"
        )

    lines.extend(("", "[必须处理]"))
    if mandatory:
        lines.extend(f"  - {message}" for message in mandatory)
        for item in invalid[:5]:
            problems = item["resource"].get("problems", [])
            first = (
                problems[0].get("message", "资源合同无效")
                if problems
                else "资源合同无效"
            )
            lines.append(f"    · {item['id']}：{first}")
        if len(invalid) > 5:
            lines.append(f"    · 另有 {len(invalid) - 5} 件；使用 --json 查看详情")
    else:
        lines.append("  - 无")

    missing = [
        item
        for item in report["instruments"]
        if item.get("production") is not False
        and item["resource"]["status"] == "missing"
    ]
    missing_families = {
        str(family)
        for item in missing
        for family in (item["resource"].get("installer", {}).get("resource_family"),)
        if family
    }
    lines.extend(("", "[按需安装]"))
    if missing:
        family_text = (
            f"，涉及 {len(missing_families)} 个资源包"
            if missing_families
            else ""
        )
        lines.extend(
            (
                (
                    f"  - {len(missing)} 件正式乐器的资源文件尚未就绪"
                    f"{family_text}；已就绪 "
                    f"{summary['resource_ready_count']}/"
                    f"{summary['production_count']} 件。"
                ),
                "  - 这些缺失项不影响已就绪或自包含乐器；"
                "只需在项目实际使用对应乐器前安装。",
                (
                    "  - 安装入口：可用 "
                    f"{summary['installer_available_count']}，"
                    "仅其他平台可用 "
                    f"{summary['installer_unavailable_on_platform_count']}，"
                    "无自动入口 "
                    f"{summary['installer_unavailable_count']}，"
                    "入口文件缺失 "
                    f"{summary['installer_missing_count']}。"
                ),
                "  - 使用 --json 查看具体乐器与恢复入口；"
                "需要完整资源门禁时再加 --require-all-resources。",
            )
        )
    else:
        lines.append("  - 当前检查范围内没有待安装资源。")

    restore = report["capabilities"]["resource_restore"]
    fluid_label = {
        "available": "可用",
        "optional_missing": "未安装",
        "not_probed": "未主动探测",
        "error": "本地探测异常",
    }.get(str(fluidsynth["status"]), str(fluidsynth["status"]))
    restore_label = {
        "available": "可用",
        "unavailable": "未提供",
        "degraded": "部分能力异常",
    }.get(str(restore["status"]), str(restore["status"]))
    lines.extend(
        (
            "",
            "[可选能力]",
            (
                f"  - FluidSynth：{fluid_label}（原生库="
                f"{fluidsynth['native']['status']}，pyfluidsynth="
                f"{fluidsynth['python_binding']['status']}）；不属于核心渲染依赖。"
            ),
            f"  - 统一资源恢复：{restore_label}。",
        )
    )
    if report["distribution"]["matches_imported_code"] is False:
        lines.append(
            "  - 安装包版本元数据与当前导入代码不一致；"
            "开发工作区可继续诊断，正式安装建议核对来源。"
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
            _print_json_utf8(
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
    if args.json:
        _print_json_utf8(doctor_report_json(report))
    else:
        print(_human_summary(report))
    if report["catalog"]["status"] != "ready":
        return 1
    if report["trusted"]["status"] != "ready":
        return 1
    if not report["python"]["supported"] or not report["platform"]["supported"]:
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
    "_platform_runtime_supported",
    "collect_doctor_report",
    "doctor_report_json",
    "main",
]
