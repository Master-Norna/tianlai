"""Human-reviewed perceptual-onset evidence.

The probe is deliberately not an authority.  It may create a
``onset_candidate_report`` containing machine suggestions, but the conductor
may consume only an ``approved_onset_evidence`` document produced by the
manual workflow in this module.

The three documents form a hash-bound chain:

``candidate -> human review -> review-lead approval``.

Every stage also binds the current instrument manifest, its local audit files,
its optional local implementation, and an AST-derived closure of the Python
modules that can affect this instrument's direct render.  A relevant source or
resource change therefore makes old onset evidence stale without invalidating
every instrument merely because an unrelated CLI or MCP module changed.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from numbers import Real
import os
from pathlib import Path, PurePosixPath
import platform
import re
import sys
import tempfile
import time
from typing import Any, Literal
import wave

from .canonical_json import canonical_json_bytes as _project_canonical_json_bytes
from .plain_file import (
    PlainFileIdentity,
    read_plain_file_bytes,
    revalidate_plain_file,
)
from .runtime_variants import (
    RuntimeVariantError,
    capture_runtime_variants,
    onset_sampled_condition,
    onset_sampled_condition_id,
    prewarm_dedicated_sfz_variation_slot,
    validate_runtime_variant_observation_proof,
    validate_runtime_variant_proof_document,
    validate_runtime_variant_selection_receipt,
)


CANDIDATE_SCHEMA = (
    "https://tianlai.local/schemas/onset-candidate-report.schema.json"
)
REVIEW_SCHEMA = (
    "https://tianlai.local/schemas/onset-review-decision.schema.json"
)
APPROVED_SCHEMA = (
    "https://tianlai.local/schemas/approved-onset-evidence.schema.json"
)
SCHEMA_VERSION = 1
ANCHOR = "performance_note_on_output_frame"
CONTEXT = "isolated_attack"
VARIANT_COVERAGE = "runtime_default_only"
APPROVABLE_VARIANT_COVERAGE = "all_runtime_variants"
DEFAULT_ARTICULATION_SENTINEL = "__default__"
FINGERPRINT_ALGORITHM = "sha256-path-content-v1"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_OBSERVATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DECISION_STATUSES = frozenset(("pending", "measured", "exclude", "unsure"))
_CONDITION_COVERAGE_KIND = "sampled_conditions"
_CONDITION_ID_ALGORITHM = "onset-isolated-sampled-condition-v1"
_TRANSIENT_WINDOWS_REPLACE_ERRORS = frozenset((5, 32, 33))
_WINDOWS_REPLACE_RETRY_DELAYS = (0.010, 0.025, 0.050, 0.100)


class OnsetEvidenceError(ValueError):
    """An onset document is invalid, stale, or not safe to approve."""


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _reject_nonfinite_tree(value: Any, label: str = "document") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise OnsetEvidenceError(f"{label} contains a non-string key")
            _reject_nonfinite_tree(item, f"{label}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_nonfinite_tree(item, f"{label}[{index}]")
        return
    if isinstance(value, Real) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise OnsetEvidenceError(f"{label} must be finite")


def canonical_json_bytes(document: Any) -> bytes:
    """Return the sole canonical byte representation used for self hashes."""

    _reject_nonfinite_tree(document)
    try:
        return _project_canonical_json_bytes(document)
    except (TypeError, ValueError) as error:
        raise OnsetEvidenceError(f"document is not canonical JSON: {error}") from error


def canonical_sha256(
    document: dict[str, Any],
    *,
    omit: str | None = None,
) -> str:
    payload = (
        {key: value for key, value in document.items() if key != omit}
        if omit is not None
        else document
    )
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise OnsetEvidenceError(f"cannot hash {path}: {error}") from error
    return digest.hexdigest()


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OnsetEvidenceError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise OnsetEvidenceError(f"non-finite JSON constant is forbidden: {value}")


def read_json_strict(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        document = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_json_constant,
        )
    except OnsetEvidenceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise OnsetEvidenceError(f"cannot read JSON {source}: {error}") from error
    if not isinstance(document, dict):
        raise OnsetEvidenceError(f"JSON root must be an object: {source}")
    _reject_nonfinite_tree(document)
    return document


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _replace_json_temporary(
    temporary: Path,
    target: Path,
    *,
    identity: PlainFileIdentity,
    payload: bytes,
) -> None:
    """Replace once, retrying only short-lived Windows sharing conflicts.

    A failed replace leaves a mutable pathname behind.  Before every retry,
    bind that name back to the descriptor-checked file and bytes created by
    this writer.  If either changed, retain both entries and fail closed.
    """

    try:
        os.replace(temporary, target)
        return
    except OSError as error:
        if (
            not _is_windows_runtime()
            or getattr(error, "winerror", None)
            not in _TRANSIENT_WINDOWS_REPLACE_ERRORS
        ):
            raise
        last_error = error

    for delay in _WINDOWS_REPLACE_RETRY_DELAYS:
        time.sleep(delay)
        try:
            revalidate_plain_file(identity)
            current_identity, current_payload = read_plain_file_bytes(
                temporary,
                maximum_bytes=len(payload),
            )
            if current_identity != identity or current_payload != payload:
                raise OSError(
                    "atomic JSON temporary file changed before retry"
                )
        except OSError as validation_error:
            raise last_error from validation_error
        try:
            os.replace(temporary, target)
            return
        except OSError as error:
            if (
                not _is_windows_runtime()
                or getattr(error, "winerror", None)
                not in _TRANSIENT_WINDOWS_REPLACE_ERRORS
            ):
                raise
            last_error = error
    raise last_error


def write_json_atomic(path: str | Path, document: dict[str, Any]) -> None:
    """Durably finish a temporary file before replacing the destination."""

    target = Path(path)
    _reject_nonfinite_tree(document)
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
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
        created = os.fstat(output.fileno())
    payload_bytes = payload.encode("utf-8")
    identity, observed_payload = read_plain_file_bytes(
        temporary,
        maximum_bytes=len(payload_bytes),
    )
    if (
        identity.device != int(created.st_dev)
        or identity.inode != int(created.st_ino)
        or observed_payload != payload_bytes
    ):
        raise OSError("atomic JSON temporary file changed after writing")
    _replace_json_temporary(
        temporary,
        target,
        identity=identity,
        payload=payload_bytes,
    )
    # POSIX can durably persist the directory entry.  Windows cannot open
    # directories with os.open in the same portable manner, so the fsync
    # above plus atomic ReplaceFile semantics is the strongest common path.
    if os.name != "nt":
        flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
        directory_fd = os.open(target.parent, flags)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _expect_keys(
    value: Any,
    *,
    required: set[str],
    optional: set[str] = frozenset(),
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise OnsetEvidenceError(f"{label} must be an object")
    missing = sorted(required - value.keys())
    extra = sorted(value.keys() - required - optional)
    if missing:
        raise OnsetEvidenceError(f"{label} is missing fields: {', '.join(missing)}")
    if extra:
        raise OnsetEvidenceError(f"{label} has unknown fields: {', '.join(extra)}")
    return value


def _string(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        qualifier = "a string" if allow_empty else "a non-empty string"
        raise OnsetEvidenceError(f"{label} must be {qualifier}")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise OnsetEvidenceError(f"{label} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f"..{maximum}" if maximum is not None else " or greater"
        raise OnsetEvidenceError(f"{label} must be {minimum}{suffix}")
    return value


def _number_or_null(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise OnsetEvidenceError(f"{label} must be a finite number or null")
    number = float(value)
    if not math.isfinite(number):
        raise OnsetEvidenceError(f"{label} must be finite")
    return number


def _integer_or_null(value: Any, label: str) -> int | None:
    if value is None:
        return None
    return _integer(value, label)


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise OnsetEvidenceError(f"{label} must be boolean")
    return value


def _sha256(value: Any, label: str) -> str:
    text = _string(value, label)
    if _HEX64.fullmatch(text) is None:
        raise OnsetEvidenceError(f"{label} must be a lowercase SHA-256")
    return text


def _timestamp_or_null(value: Any, label: str) -> str | None:
    if value is None:
        return None
    text = _string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise OnsetEvidenceError(f"{label} must be an ISO 8601 timestamp") from error
    if parsed.tzinfo is None:
        raise OnsetEvidenceError(f"{label} must include a timezone")
    return text


def _project_relative_label(project_root: Path, path: Path) -> str:
    root = project_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise OnsetEvidenceError(f"path leaves project root: {path}")
    return resolved.relative_to(root).as_posix()


def resolve_project_path(
    project_root: str | Path,
    label: Any,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a stored POSIX project-relative path without traversal."""

    root = Path(project_root).resolve()
    text = _string(label, "project-relative path")
    if "\x00" in text or "\\" in text or ":" in text:
        raise OnsetEvidenceError(f"unsafe project-relative path: {text!r}")
    relative = PurePosixPath(text)
    if relative.is_absolute() or not relative.parts:
        raise OnsetEvidenceError(f"path must be project-relative: {text!r}")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise OnsetEvidenceError(f"unsafe project-relative path: {text!r}")
    resolved = root.joinpath(*relative.parts).resolve()
    if not resolved.is_relative_to(root):
        raise OnsetEvidenceError(f"path leaves project root: {text!r}")
    if must_exist and not resolved.is_file():
        raise OnsetEvidenceError(f"bound file does not exist: {text}")
    return resolved


def _optional_bound_file(
    project_root: Path,
    instrument_directory: Path,
    raw_path: Any,
    *,
    default_name: str | None,
) -> dict[str, Any]:
    if raw_path is None and default_name is None:
        return {"path": None, "sha256": None}
    raw = default_name if raw_path is None else _string(raw_path, "manifest file path")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise OnsetEvidenceError(f"manifest path must be relative: {raw!r}")
    resolved = (instrument_directory / candidate).resolve()
    if not resolved.is_relative_to(project_root):
        raise OnsetEvidenceError(f"manifest path leaves project root: {raw!r}")
    label = _project_relative_label(project_root, resolved)
    if not resolved.is_file():
        return {"path": label, "sha256": None}
    return {"path": label, "sha256": sha256_file(resolved)}


_RENDER_CLOSURE_BASE_SEEDS = frozenset(
    {
        "tianlai.audio",
        "tianlai.events",
        "tianlai.instrument",
        "tianlai.renderer",
        "tianlai.tuning",
        "tianlai.capability",
        "tianlai.onset_probe",
        "tianlai.onset_evidence",
    }
)

# ``create_instrument`` imports these backends inside a function, so a normal
# module-level AST import walk cannot see which branch one manifest will take.
# Keeping this table explicit also makes a new dispatcher type fail closed
# until its runtime module has been named and reviewed here.
_DYNAMIC_BACKEND_MODULES: dict[str, tuple[str, ...]] = {
    "oscillator": ("tianlai.oscillator",),
    "soundfont": ("tianlai.soundfont",),
    "synthesizer": ("tianlai.synthesizer",),
    "procedural_sfx": ("tianlai.procedural_sfx",),
    "dedicated_sfz": ("tianlai.dedicated_sfz",),
    "dedicated_fx": ("tianlai.dedicated_fx",),
    "reversed_cymbal": ("tianlai.reversed_cymbal",),
    "melodic_toms": ("tianlai.melodic_toms",),
    "modeled_instrument": ("tianlai.modeled_instruments",),
    "modeled_bianzhong": ("tianlai.bianzhong",),
    "sample": ("tianlai.sampler",),
    "piano": ("tianlai.piano",),
    "violin": ("tianlai.violin",),
    "cello": ("tianlai.cello",),
    "flute": ("tianlai.flute",),
    "vpo_solo_string": ("tianlai.vpo_strings",),
    "vpo_string_section": ("tianlai.vpo_strings",),
    "vpo_harp": ("tianlai.vpo_strings",),
    "vpo_brass": ("tianlai.vpo_brass",),
    "vpo_woodwind": ("tianlai.vpo_woodwinds",),
    "vpo_percussion": ("tianlai.vpo_percussion",),
    "vpo_mixed_choir": ("tianlai.vpo_specials",),
    "vpo_orchestral_hit": ("tianlai.vpo_specials",),
    "vpo_celesta": ("tianlai.vpo_specials",),
    "vpo_cowbell": ("tianlai.vpo_specials",),
    "mtg_solo_sax": ("tianlai.mtg_sax",),
    "vsco2_viola_section": ("tianlai.vsco2_viola",),
}


def _module_source_path(project_root: Path, module_name: str) -> Path | None:
    if module_name == "tianlai":
        candidate = project_root / "tianlai" / "__init__.py"
    elif module_name.startswith("tianlai."):
        relative = module_name.split(".")[1:]
        module_file = project_root / "tianlai" / Path(*relative).with_suffix(".py")
        package_file = project_root / "tianlai" / Path(*relative) / "__init__.py"
        candidate = module_file if module_file.is_file() else package_file
    else:
        return None
    resolved = candidate.resolve()
    source_root = (project_root / "tianlai").resolve()
    if not resolved.is_relative_to(source_root) or not resolved.is_file():
        return None
    return resolved


class _TopLevelTianlaiImports(ast.NodeVisitor):
    """Collect imports executed while a module is initialized.

    Function and class bodies are deliberately not descended into.  Their
    lazy dispatcher imports are selected by ``_DYNAMIC_BACKEND_MODULES``;
    audit-only helpers such as pitch-analysis routines must not make every
    render proof depend on unrelated tooling.
    """

    def __init__(
        self,
        *,
        project_root: Path,
        current_module: str | None,
    ) -> None:
        self.project_root = project_root
        self.current_module = current_module
        self.modules: set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _add(self, module_name: str) -> bool:
        if not module_name.startswith("tianlai"):
            return False
        if _module_source_path(self.project_root, module_name) is None:
            return False
        self.modules.add(module_name)
        return True

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            if self.current_module is None:
                return
            package = self.current_module.split(".")[:-1]
            if node.level > len(package):
                raise OnsetEvidenceError(
                    f"relative import escapes tianlai in {self.current_module}"
                )
            base = package[: len(package) - node.level + 1]
            if node.module:
                base.extend(node.module.split("."))
            module_name = ".".join(base)
        else:
            module_name = node.module or ""
        if not module_name.startswith("tianlai"):
            return

        added_base = self._add(module_name)
        # ``from tianlai import audio`` and ``from . import audio`` name
        # submodules in aliases rather than node.module.
        if not node.module or module_name == "tianlai":
            for alias in node.names:
                if alias.name == "*":
                    continue
                self._add(f"{module_name}.{alias.name}")
        elif not added_base:
            raise OnsetEvidenceError(
                f"cannot resolve project import {module_name!r}"
            )


def _source_import_modules(
    path: Path,
    *,
    project_root: Path,
    current_module: str | None,
) -> set[str]:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as error:
        raise OnsetEvidenceError(f"cannot parse Python source {path}: {error}") from error
    visitor = _TopLevelTianlaiImports(
        project_root=project_root,
        current_module=current_module,
    )
    visitor.visit(tree)
    return visitor.modules


def _render_python_closure(
    project_root: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    instrument_type = str(manifest.get("type", "")).strip()
    implementation_raw = manifest.get("implementation")
    implementation_path: Path | None = None
    local_imports: set[str] = set()
    if implementation_raw is not None:
        implementation_path = (
            manifest_path.parent / _string(
                implementation_raw,
                "manifest implementation",
            )
        ).resolve()
        if (
            not implementation_path.is_relative_to(project_root)
            or not implementation_path.is_file()
        ):
            raise OnsetEvidenceError(
                f"local implementation is missing or unsafe: {implementation_path}"
            )
        local_imports = _source_import_modules(
            implementation_path,
            project_root=project_root,
            current_module=None,
        )

    backend_modules = set(_DYNAMIC_BACKEND_MODULES.get(instrument_type, ()))
    if not backend_modules and implementation_path is None:
        raise OnsetEvidenceError(
            f"unknown dynamic instrument backend {instrument_type!r}; "
            "add an explicit render-closure mapping"
        )
    entries = set(_RENDER_CLOSURE_BASE_SEEDS) | backend_modules | local_imports
    pending = list(sorted(entries, reverse=True))
    visited: set[str] = set()
    while pending:
        module_name = pending.pop()
        if module_name in visited:
            continue
        path = _module_source_path(project_root, module_name)
        if path is None:
            raise OnsetEvidenceError(
                f"render closure cannot resolve module {module_name!r}"
            )
        visited.add(module_name)
        imports = _source_import_modules(
            path,
            project_root=project_root,
            current_module=module_name,
        )
        for imported in sorted(imports, reverse=True):
            if imported not in visited:
                pending.append(imported)

    records: list[dict[str, Any]] = []
    for module_name in sorted(visited):
        source_path = _module_source_path(project_root, module_name)
        if source_path is None:
            # The module was resolved above. Rechecking keeps the failure mode
            # explicit if the source tree changes concurrently.
            raise OnsetEvidenceError(
                f"render closure source disappeared: {module_name!r}"
            )
        records.append(
            {
                "path": _project_relative_label(project_root, source_path),
                "sha256": sha256_file(source_path),
            }
        )
    records.sort(key=lambda record: record["path"])
    aggregate = hashlib.sha256(
        "".join(
            f"{record['sha256']}  {record['path']}\n" for record in records
        ).encode("utf-8")
    ).hexdigest()
    return {
        "algorithm": "ast-render-import-closure-v1",
        "entry_modules": sorted(entries),
        "file_count": len(records),
        "files": records,
        "sha256": aggregate,
    }


def _runtime_dependencies(*, soundfont_applicable: bool) -> dict[str, Any]:
    try:
        import numpy
        import soundfile
    except ImportError as error:
        raise OnsetEvidenceError(
            f"cannot fingerprint required audio dependency: {error}"
        ) from error

    fluidsynth: dict[str, Any] = {
        "applicable": soundfont_applicable,
        "package_version": None,
        "api_version": None,
        "native_identifier": None,
        "native_sha256": None,
        "native_version": None,
    }
    if soundfont_applicable:
        try:
            package_version = importlib.metadata.version("pyfluidsynth")
            import fluidsynth as fluidsynth_module
        except (ImportError, importlib.metadata.PackageNotFoundError) as error:
            raise OnsetEvidenceError(
                f"soundfont backend requires a fingerprintable FluidSynth: {error}"
            ) from error
        fluidsynth["package_version"] = package_version
        fluidsynth["api_version"] = str(
            getattr(fluidsynth_module, "api_version", "")
        ) or None
        native = getattr(fluidsynth_module, "_fl", None)
        native_name = getattr(native, "_name", None)
        if native_name:
            fluidsynth["native_identifier"] = str(native_name)
            native_path = Path(str(native_name))
            if native_path.is_file():
                fluidsynth["native_sha256"] = sha256_file(native_path)
        version_function = getattr(fluidsynth_module, "fluid_version", None)
        try:
            if not callable(version_function):
                raise RuntimeError("fluid_version is unavailable")
            from ctypes import byref, c_int

            major, minor, micro = c_int(), c_int(), c_int()
            version_function(byref(major), byref(minor), byref(micro))
            fluidsynth["native_version"] = (
                f"{major.value}.{minor.value}.{micro.value}"
            )
        except Exception as error:
            raise OnsetEvidenceError(
                f"cannot fingerprint the native FluidSynth version: {error}"
            ) from error
        if (
            fluidsynth["api_version"] is None
            or fluidsynth["native_identifier"] is None
        ):
            raise OnsetEvidenceError(
                "soundfont backend exposes no stable pyFluidSynth/native identity"
            )

    return {
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "byteorder": sys.byteorder,
        },
        "numpy": {"version": str(numpy.__version__)},
        "soundfile": {
            "version": str(soundfile.__version__),
            "libsndfile_version": str(
                getattr(soundfile, "__libsndfile_version__", "unknown")
            ),
        },
        "pyfluidsynth": fluidsynth,
    }


_UNSUPPORTED_SIGNATURE_VALUE = object()


def _stable_type_name(value: Any) -> str:
    module = type(value).__module__
    module = re.sub(
        r"^(tianlai_(?:local_instrument|capability_probe))_[0-9a-f]{8,64}$",
        r"\1_<path>",
        module,
    )
    return f"{module}.{type(value).__qualname__}"


def _signature_value(value: Any, project_root: Path) -> Any:
    """Convert runtime-region fields to stable, data-only JSON values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OnsetEvidenceError("runtime region contains a non-finite value")
        return value
    if isinstance(value, Path):
        return {"project_path": _project_relative_label(project_root, value)}
    if isinstance(value, (tuple, list)):
        converted: list[Any] = []
        for item in value:
            result = _signature_value(item, project_root)
            if result is _UNSUPPORTED_SIGNATURE_VALUE:
                return _UNSUPPORTED_SIGNATURE_VALUE
            converted.append(result)
        return converted
    if isinstance(value, dict):
        converted_dict: dict[str, Any] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if not isinstance(key, str):
                return _UNSUPPORTED_SIGNATURE_VALUE
            result = _signature_value(item, project_root)
            if result is _UNSUPPORTED_SIGNATURE_VALUE:
                return _UNSUPPORTED_SIGNATURE_VALUE
            converted_dict[key] = result
        return converted_dict
    return _UNSUPPORTED_SIGNATURE_VALUE


def _object_signature(value: Any, project_root: Path) -> dict[str, Any] | None:
    names: set[str] = set(getattr(value, "__dict__", {}))
    for klass in type(value).__mro__:
        slots = getattr(klass, "__slots__", ()) or ()
        if isinstance(slots, str):
            names.add(slots)
        else:
            names.update(slots)
    fields: dict[str, Any] = {}
    for name in sorted(names):
        if name.startswith("__") or name in {"frames", "sample"}:
            continue
        try:
            raw = getattr(value, name)
        except (AttributeError, ValueError):
            continue
        converted = _signature_value(raw, project_root)
        if converted is not _UNSUPPORTED_SIGNATURE_VALUE:
            fields[name] = converted
    if not fields:
        return None
    return {
        "type": _stable_type_name(value),
        "fields": fields,
    }


def _runtime_asset_graph(
    project_root: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    sample_rate_hz: int,
) -> dict[str, Any]:
    """Hash files and parsed region parameters the instrument actually loads.

    Hashing only ``资源核验.json`` would leave a stale-evidence hole: a WAV
    could be replaced while the report was accidentally left untouched.
    Constructing the current instrument provides the exact runtime file graph,
    including samples reached through SFZ includes and local adapters.  Parsed
    region signatures additionally bind offsets, envelopes, loops and routing
    even when a mapping file changes without changing its sample file set.
    """

    instrument_type = str(manifest.get("type", ""))
    has_external_graph = any(
        field in manifest
        for field in ("asset_root", "soundfont", "sample", "regions")
    ) or instrument_type in {"sample", "soundfont"}
    if not has_external_graph:
        empty_hash = canonical_sha256({"files": [], "region_groups": []})
        return {
            "algorithm": "constructed-runtime-asset-graph-v1",
            "sample_rate_hz": sample_rate_hz,
            "file_count": 0,
            "total_bytes": 0,
            "region_count": 0,
            "sha256": empty_hash,
        }

    try:
        from .instrument import create_instrument

        instrument = create_instrument(
            manifest,
            sample_rate_hz,
            base_directory=str(manifest_path.parent),
        )
    except Exception as error:
        raise OnsetEvidenceError(
            f"cannot construct instrument for runtime asset fingerprint: {error}"
        ) from error

    files: dict[str, Path] = {}
    region_groups: list[dict[str, Any]] = []
    seen: set[int] = set()

    def visit(value: Any, depth: int, route: str) -> None:
        if depth > 10 or id(value) in seen:
            return
        if isinstance(value, Path):
            resolved = value.resolve()
            if resolved.is_file():
                label = _project_relative_label(project_root, resolved)
                files[label] = resolved
            return
        if value is None or isinstance(value, (str, bytes, bool, int, float)):
            return
        # Decoded sample arrays are intentionally lazy in the current sampler.
        # If another backend eagerly decodes one, its source Path is still
        # visited while walking the owning sample record; never traverse the
        # potentially gigabyte-sized array object.
        if type(value).__module__.split(".", 1)[0] == "numpy":
            return
        seen.add(id(value))

        regions = getattr(value, "regions", None)
        if regions is not None:
            try:
                signatures: list[dict[str, Any]] = []
                for region in regions:
                    signature = _object_signature(region, project_root)
                    if signature is not None:
                        signatures.append(signature)
                if signatures:
                    # Preserve region order and the deterministic object-graph
                    # route (for example engines.sustain versus
                    # engines.staccato).  Flattening/sorting individual
                    # regions would miss an SFZ reorder or articulation swap.
                    region_groups.append(
                        {
                            "route": route,
                            "owner_type": _stable_type_name(value),
                            "regions": signatures,
                        }
                    )
            except TypeError:
                pass

        if isinstance(value, dict):
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
                visit(item, depth + 1, f"{route}[{str(key)!r}]")
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                visit(item, depth + 1, f"{route}[{index}]")
            return
        if isinstance(value, (set, frozenset)):
            for item in sorted(value, key=lambda member: repr(member)):
                visit(item, depth + 1, f"{route}[set]")
            return
        names: set[str] = set(getattr(value, "__dict__", {}))
        for klass in type(value).__mro__:
            slots = getattr(klass, "__slots__", ()) or ()
            if isinstance(slots, str):
                names.add(slots)
            else:
                names.update(slots)
        for name in sorted(names):
            if name.startswith("__"):
                continue
            try:
                visit(getattr(value, name), depth + 1, f"{route}.{name}")
            except (AttributeError, ValueError):
                continue

    try:
        visit(instrument, 0, "instrument")
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()

    if not files:
        raise OnsetEvidenceError(
            "instrument declares external assets but its constructed runtime "
            "graph contains no files"
        )
    file_records = [
        {
            "path": label,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
        for label, path in sorted(files.items())
    ]
    sorted_region_groups = sorted(
        region_groups,
        key=lambda item: canonical_json_bytes(item),
    )
    graph_hash = canonical_sha256(
        {
            "files": file_records,
            "region_groups": sorted_region_groups,
        }
    )
    return {
        "algorithm": "constructed-runtime-asset-graph-v1",
        "sample_rate_hz": sample_rate_hz,
        "file_count": len(file_records),
        "total_bytes": sum(record["bytes"] for record in file_records),
        "region_count": sum(
            len(group["regions"]) for group in sorted_region_groups
        ),
        "sha256": graph_hash,
    }


def _effective_runtime_manifest(
    effective_manifest: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Canonicalize a caller-provided runtime manifest without mutating it."""

    if effective_manifest is None:
        return None
    if not isinstance(effective_manifest, dict):
        raise OnsetEvidenceError("effective_manifest must be an object")
    try:
        # JSON round-tripping provides both a deep copy and the exact JSON
        # value that construction and field parsing will observe.
        copied = json.loads(canonical_json_bytes(effective_manifest))
    except (OnsetEvidenceError, json.JSONDecodeError) as error:
        raise OnsetEvidenceError(
            f"effective_manifest is not canonical JSON: {error}"
        ) from error
    if not isinstance(copied, dict):  # Defensive: the input was checked above.
        raise OnsetEvidenceError("effective_manifest must be an object")
    return copied


def compute_runtime_fingerprint(
    project_root: str | Path,
    manifest_path: str | Path,
    *,
    effective_manifest: dict[str, Any] | None = None,
    sample_rate_hz: int = 48_000,
) -> dict[str, Any]:
    """Bind every runtime input relevant to onset interpretation.

    ``effective_manifest`` represents the normalized manifest passed to the
    runtime.  The on-disk manifest remains the path/hash authority in the
    returned fingerprint, so a caller cannot substitute its identity.
    """

    runtime_sample_rate_hz = _integer(
        sample_rate_hz,
        "sample_rate_hz",
        minimum=8_000,
        maximum=384_000,
    )
    root = Path(project_root).resolve()
    manifest_file = Path(manifest_path)
    if not manifest_file.is_absolute():
        manifest_file = root / manifest_file
    manifest_file = manifest_file.resolve()
    if not manifest_file.is_relative_to(root) or not manifest_file.is_file():
        raise OnsetEvidenceError(
            f"instrument manifest must be a file under project root: {manifest_file}"
        )
    disk_manifest = read_json_strict(manifest_file)
    runtime_manifest = _effective_runtime_manifest(effective_manifest)
    manifest = disk_manifest if runtime_manifest is None else runtime_manifest
    instrument_directory = manifest_file.parent
    raw_implementation = manifest.get("implementation")
    implementation = _optional_bound_file(
        root,
        instrument_directory,
        raw_implementation,
        default_name=None,
    )
    resource_verification = _optional_bound_file(
        root,
        instrument_directory,
        manifest.get("resource_verification"),
        default_name="资源核验.json",
    )
    pitch_calibration = _optional_bound_file(
        root,
        instrument_directory,
        manifest.get("pitch_calibration"),
        default_name="音准校准.json",
    )
    render_python_closure = _render_python_closure(
        root,
        manifest_file,
        manifest,
    )
    soundfont_applicable = any(
        record["path"] == "tianlai/soundfont.py"
        for record in render_python_closure["files"]
    )
    return {
        "algorithm": FINGERPRINT_ALGORITHM,
        "manifest": {
            "path": _project_relative_label(root, manifest_file),
            "sha256": sha256_file(manifest_file),
        },
        "render_python_closure": render_python_closure,
        "runtime_dependencies": _runtime_dependencies(
            soundfont_applicable=soundfont_applicable,
        ),
        "local_implementation": implementation,
        "resource_verification": resource_verification,
        "pitch_calibration": pitch_calibration,
        "runtime_asset_graph": _runtime_asset_graph(
            root,
            manifest_file,
            manifest,
            sample_rate_hz=runtime_sample_rate_hz,
        ),
    }


def _validate_render_python_closure(
    raw_closure: Any,
    *,
    project_root: str | Path,
) -> dict[str, Any]:
    closure = _expect_keys(
        raw_closure,
        required={
            "algorithm",
            "entry_modules",
            "file_count",
            "files",
            "sha256",
        },
        label="runtime_fingerprint.render_python_closure",
    )
    if closure["algorithm"] != "ast-render-import-closure-v1":
        raise OnsetEvidenceError("unsupported render Python closure algorithm")

    raw_entries = closure["entry_modules"]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise OnsetEvidenceError(
            "render Python closure entry_modules must be a non-empty array"
        )
    entries = [
        _string(module, f"render Python closure entry_modules[{index}]")
        for index, module in enumerate(raw_entries)
    ]
    if entries != sorted(set(entries)):
        raise OnsetEvidenceError(
            "render Python closure entry_modules must be sorted and unique"
        )
    module_pattern = re.compile(
        r"^tianlai(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
    )
    forbidden_modules = (
        "tianlai.cli",
        "tianlai.mcp_server",
        "tianlai.__main__",
    )
    for module in entries:
        if module_pattern.fullmatch(module) is None:
            raise OnsetEvidenceError(
                f"invalid render Python closure module: {module!r}"
            )
        if any(
            module == forbidden or module.startswith(f"{forbidden}.")
            for forbidden in forbidden_modules
        ):
            raise OnsetEvidenceError(
                f"unrelated module entered render Python closure: {module}"
            )
    missing_seeds = sorted(_RENDER_CLOSURE_BASE_SEEDS - set(entries))
    if missing_seeds:
        raise OnsetEvidenceError(
            "render Python closure is missing base entries: "
            + ", ".join(missing_seeds)
        )

    raw_files = closure["files"]
    if not isinstance(raw_files, list) or not raw_files:
        raise OnsetEvidenceError(
            "render Python closure files must be a non-empty array"
        )
    records: list[dict[str, Any]] = []
    paths: list[str] = []
    for index, raw_record in enumerate(raw_files):
        record = _expect_keys(
            raw_record,
            required={"path", "sha256"},
            label=f"runtime_fingerprint.render_python_closure.files[{index}]",
        )
        path_label = _string(
            record["path"],
            f"render Python closure files[{index}].path",
        )
        resolved = resolve_project_path(project_root, path_label)
        relative_parts = PurePosixPath(path_label).parts
        if (
            not relative_parts
            or relative_parts[0] != "tianlai"
            or resolved.suffix != ".py"
        ):
            raise OnsetEvidenceError(
                f"render closure source is not a tianlai Python file: {path_label}"
            )
        if path_label in {
            "tianlai/cli.py",
            "tianlai/mcp_server.py",
            "tianlai/__main__.py",
        }:
            raise OnsetEvidenceError(
                f"unrelated source entered render Python closure: {path_label}"
            )
        _sha256(
            record["sha256"],
            f"render Python closure files[{index}].sha256",
        )
        paths.append(path_label)
        records.append(record)
    if paths != sorted(set(paths)):
        raise OnsetEvidenceError(
            "render Python closure files must be path-sorted and unique"
        )
    file_count = _integer(
        closure["file_count"],
        "render Python closure file_count",
        minimum=1,
    )
    if file_count != len(records):
        raise OnsetEvidenceError(
            "render Python closure file_count does not match files"
        )
    stored_aggregate = _sha256(
        closure["sha256"],
        "render Python closure sha256",
    )
    computed_aggregate = hashlib.sha256(
        "".join(
            f"{record['sha256']}  {record['path']}\n" for record in records
        ).encode("utf-8")
    ).hexdigest()
    if stored_aggregate != computed_aggregate:
        raise OnsetEvidenceError(
            "render Python closure aggregate hash does not match its files"
        )
    return closure


def _nullable_dependency_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    return _string(value, label)


def _validate_runtime_dependencies(raw_dependencies: Any) -> dict[str, Any]:
    dependencies = _expect_keys(
        raw_dependencies,
        required={"python", "numpy", "soundfile", "pyfluidsynth"},
        label="runtime_fingerprint.runtime_dependencies",
    )
    python = _expect_keys(
        dependencies["python"],
        required={
            "implementation",
            "version",
            "system",
            "machine",
            "byteorder",
        },
        label="runtime_fingerprint.runtime_dependencies.python",
    )
    for field in ("implementation", "version", "system", "machine"):
        _string(
            python[field],
            f"runtime_fingerprint.runtime_dependencies.python.{field}",
        )
    if python["byteorder"] not in {"little", "big"}:
        raise OnsetEvidenceError(
            "runtime dependency Python byteorder must be little or big"
        )

    numpy = _expect_keys(
        dependencies["numpy"],
        required={"version"},
        label="runtime_fingerprint.runtime_dependencies.numpy",
    )
    _string(numpy["version"], "runtime dependency numpy.version")

    soundfile = _expect_keys(
        dependencies["soundfile"],
        required={"version", "libsndfile_version"},
        label="runtime_fingerprint.runtime_dependencies.soundfile",
    )
    _string(soundfile["version"], "runtime dependency soundfile.version")
    _string(
        soundfile["libsndfile_version"],
        "runtime dependency soundfile.libsndfile_version",
    )

    pyfluidsynth = _expect_keys(
        dependencies["pyfluidsynth"],
        required={
            "applicable",
            "package_version",
            "api_version",
            "native_identifier",
            "native_sha256",
            "native_version",
        },
        label="runtime_fingerprint.runtime_dependencies.pyfluidsynth",
    )
    applicable = _boolean(
        pyfluidsynth["applicable"],
        "runtime dependency pyfluidsynth.applicable",
    )
    nullable_fields = (
        "package_version",
        "api_version",
        "native_identifier",
        "native_version",
    )
    for field in nullable_fields:
        _nullable_dependency_string(
            pyfluidsynth[field],
            f"runtime dependency pyfluidsynth.{field}",
        )
    if pyfluidsynth["native_sha256"] is not None:
        _sha256(
            pyfluidsynth["native_sha256"],
            "runtime dependency pyfluidsynth.native_sha256",
        )
    if applicable:
        for field in nullable_fields:
            if pyfluidsynth[field] is None:
                raise OnsetEvidenceError(
                    f"applicable pyfluidsynth dependency lacks {field}"
                )
    elif any(
        pyfluidsynth[field] is not None
        for field in (*nullable_fields, "native_sha256")
    ):
        raise OnsetEvidenceError(
            "non-applicable pyfluidsynth dependency must contain null identities"
        )
    return dependencies


def validate_runtime_fingerprint(
    fingerprint: Any,
    *,
    project_root: str | Path,
    manifest_path: str | Path | None = None,
    effective_manifest: dict[str, Any] | None = None,
    sample_rate_hz: int = 48_000,
) -> dict[str, Any]:
    """Fail closed unless a stored fingerprint equals the current runtime.

    Callers that used a runtime manifest overlay or a non-default sample rate
    while computing the fingerprint must provide the same values here.
    """

    value = _expect_keys(
        fingerprint,
        required={
            "algorithm",
            "manifest",
            "render_python_closure",
            "runtime_dependencies",
            "local_implementation",
            "resource_verification",
            "pitch_calibration",
            "runtime_asset_graph",
        },
        label="runtime_fingerprint",
    )
    if value["algorithm"] != FINGERPRINT_ALGORITHM:
        raise OnsetEvidenceError("unsupported runtime fingerprint algorithm")

    manifest = _expect_keys(
        value["manifest"],
        required={"path", "sha256"},
        label="runtime_fingerprint.manifest",
    )
    stored_manifest_path = resolve_project_path(project_root, manifest["path"])
    _sha256(manifest["sha256"], "runtime_fingerprint.manifest.sha256")
    if manifest_path is not None:
        expected = Path(manifest_path)
        if not expected.is_absolute():
            expected = Path(project_root) / expected
        if stored_manifest_path.resolve() != expected.resolve():
            raise OnsetEvidenceError("runtime fingerprint names another manifest")

    _validate_render_python_closure(
        value["render_python_closure"],
        project_root=project_root,
    )
    _validate_runtime_dependencies(value["runtime_dependencies"])

    for field in (
        "local_implementation",
        "resource_verification",
        "pitch_calibration",
    ):
        entry = _expect_keys(
            value[field],
            required={"path", "sha256"},
            label=f"runtime_fingerprint.{field}",
        )
        if entry["path"] is None:
            if entry["sha256"] is not None:
                raise OnsetEvidenceError(f"{field} cannot hash a null path")
        else:
            resolve_project_path(
                project_root,
                entry["path"],
                must_exist=entry["sha256"] is not None,
            )
            if entry["sha256"] is not None:
                _sha256(entry["sha256"], f"runtime_fingerprint.{field}.sha256")

    asset_graph = _expect_keys(
        value["runtime_asset_graph"],
        required={
            "algorithm",
            "sample_rate_hz",
            "file_count",
            "total_bytes",
            "region_count",
            "sha256",
        },
        label="runtime_fingerprint.runtime_asset_graph",
    )
    if asset_graph["algorithm"] != "constructed-runtime-asset-graph-v1":
        raise OnsetEvidenceError("unsupported runtime asset graph algorithm")
    _integer(
        asset_graph["sample_rate_hz"],
        "runtime asset graph sample_rate_hz",
        minimum=8_000,
        maximum=384_000,
    )
    for field in ("file_count", "total_bytes", "region_count"):
        _integer(
            asset_graph[field],
            f"runtime asset graph {field}",
        )
    _sha256(asset_graph["sha256"], "runtime asset graph sha256")

    current = compute_runtime_fingerprint(
        project_root,
        stored_manifest_path,
        effective_manifest=effective_manifest,
        sample_rate_hz=sample_rate_hz,
    )
    if value != current:
        raise OnsetEvidenceError(
            "runtime fingerprint is stale: manifest, render source/dependency, "
            "implementation, resource verification, pitch calibration, or "
            "runtime asset graph changed"
        )
    return value


def _validate_performance_anchor(
    performance_path: Path,
    *,
    sample_rate_hz: int,
    note_on_frame: int,
    articulation: str,
    midi_note: int,
    velocity: int,
) -> None:
    try:
        from .events import parse_performance_document

        performance = parse_performance_document(
            read_json_strict(performance_path)
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, OnsetEvidenceError):
            raise
        raise OnsetEvidenceError(
            f"invalid bound performance {performance_path}: {error}"
        ) from error
    if performance.sample_rate != sample_rate_hz:
        raise OnsetEvidenceError("performance and candidate sample rates differ")
    note_events = [event for event in performance.events if event.type == "note_on"]
    if len(note_events) != 1:
        raise OnsetEvidenceError(
            "an onset probe performance must contain exactly one note_on"
        )
    note = note_events[0]
    if note.sample != note_on_frame:
        raise OnsetEvidenceError(
            "candidate note_on_frame does not match the bound performance"
        )
    if abs(float(note.payload.get("midi_note", -999.0)) - midi_note) > 1e-9:
        raise OnsetEvidenceError("candidate MIDI note does not match performance")
    if round(float(note.payload["velocity"]) * 127.0) != velocity:
        raise OnsetEvidenceError("candidate velocity does not match performance")

    selected_articulation: str | None = None
    for event in performance.events:
        if event.sample > note.sample:
            break
        if event.type == "articulation":
            selected_articulation = str(event.payload["name"])
        if event is note:
            break
    effective_articulation = (
        selected_articulation
        if selected_articulation is not None
        else DEFAULT_ARTICULATION_SENTINEL
    )
    if effective_articulation != articulation:
        raise OnsetEvidenceError(
            "candidate final_articulation does not match the articulation "
            "active at the bound note_on"
        )


def _wav_properties(path: Path) -> tuple[int, int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            return source.getframerate(), source.getnframes(), source.getnchannels()
    except (OSError, EOFError, wave.Error) as error:
        raise OnsetEvidenceError(f"cannot inspect bound WAV {path}: {error}") from error


def _validate_candidate_analysis(
    analysis: Any,
    *,
    label: str,
    maximum_relative_frame: int,
) -> dict[str, Any]:
    value = _expect_keys(
        analysis,
        required={
            "status",
            "candidate_onset_frame",
            "t10_frame",
            "t50_frame",
            "t90_frame",
            "peak_frame",
            "snr_db",
            "pre_roll_leak",
            "clipped",
            "reason",
            "noise_floor_rms",
            "threshold_rms",
            "peak_rms",
            "clipping_sample_count",
            "pre_roll_peak_rms",
        },
        label=label,
    )
    if value["status"] not in {"proposed", "unresolved"}:
        raise OnsetEvidenceError(f"{label}.status is invalid")
    frames: dict[str, int | None] = {}
    for field in (
        "candidate_onset_frame",
        "t10_frame",
        "t50_frame",
        "t90_frame",
        "peak_frame",
    ):
        frame = _integer_or_null(value[field], f"{label}.{field}")
        if frame is not None and frame > maximum_relative_frame:
            raise OnsetEvidenceError(f"{label}.{field} lies after the WAV")
        frames[field] = frame
    _number_or_null(value["snr_db"], f"{label}.snr_db")
    _boolean(value["pre_roll_leak"], f"{label}.pre_roll_leak")
    clipped = _boolean(value["clipped"], f"{label}.clipped")
    if value["reason"] is not None:
        _string(value["reason"], f"{label}.reason")
    for field in (
        "noise_floor_rms",
        "threshold_rms",
        "peak_rms",
        "pre_roll_peak_rms",
    ):
        metric = _number_or_null(value[field], f"{label}.{field}")
        if metric is not None and metric < 0.0:
            raise OnsetEvidenceError(f"{label}.{field} must not be negative")
    clipping_count = _integer(
        value["clipping_sample_count"],
        f"{label}.clipping_sample_count",
    )
    if clipped != (clipping_count > 0):
        raise OnsetEvidenceError(
            f"{label}.clipped disagrees with clipping_sample_count"
        )

    if value["status"] == "proposed":
        if frames["candidate_onset_frame"] is None:
            raise OnsetEvidenceError(
                f"{label}.candidate_onset_frame is required for proposed analysis"
            )
    else:
        if frames["candidate_onset_frame"] is not None:
            raise OnsetEvidenceError(
                f"{label}.unresolved analysis cannot propose an onset frame"
            )
        if value["reason"] is None:
            raise OnsetEvidenceError(
                f"{label}.unresolved analysis requires a reason"
            )
    ordered = [frames[field] for field in ("t10_frame", "t50_frame", "t90_frame")]
    present = [frame for frame in ordered if frame is not None]
    if len(present) > 1 and present != sorted(present):
        raise OnsetEvidenceError(f"{label} must satisfy t10 <= t50 <= t90")
    return value


def _validate_condition_coverage(
    raw_value: Any,
    *,
    required: bool,
) -> dict[str, Any] | None:
    if raw_value is None:
        if required:
            raise OnsetEvidenceError(
                "all_runtime_variants requires explicit sampled condition coverage"
            )
        return None
    value = _expect_keys(
        raw_value,
        required={
            "kind",
            "condition_id_algorithm",
            "unique_condition_count",
            "condition_ids",
        },
        label="candidate.protocol.condition_coverage",
    )
    if value["kind"] != _CONDITION_COVERAGE_KIND:
        raise OnsetEvidenceError(
            "candidate condition coverage must remain sampled_conditions"
        )
    if value["condition_id_algorithm"] != _CONDITION_ID_ALGORITHM:
        raise OnsetEvidenceError(
            "candidate condition identifier algorithm is unsupported"
        )
    count = _integer(
        value["unique_condition_count"],
        "candidate.protocol.condition_coverage.unique_condition_count",
        minimum=1,
    )
    if not isinstance(value["condition_ids"], list):
        raise OnsetEvidenceError(
            "candidate condition coverage identifiers must be an array"
        )
    identifiers = [
        _sha256(
            item,
            "candidate.protocol.condition_coverage.condition_ids",
        )
        for item in value["condition_ids"]
    ]
    if identifiers != sorted(set(identifiers)):
        raise OnsetEvidenceError(
            "sampled condition identifiers must be unique and sorted"
        )
    if count != len(identifiers):
        raise OnsetEvidenceError(
            "sampled condition count differs from its identifier set"
        )
    return value


def _validate_complete_runtime_variant_slots(
    observations: list[dict[str, Any]],
    *,
    required: bool,
) -> None:
    """Require every finite-RR condition to contain its complete live cycle."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        proof = observation.get("variant_catalog_proof")
        condition_id = observation.get("condition_id")
        if proof is None or not isinstance(condition_id, str):
            continue
        grouped.setdefault(condition_id, []).append(observation)
    if not required:
        return
    for condition_id, condition_observations in grouped.items():
        finite = [
            observation
            for observation in condition_observations
            if observation["variant_catalog_proof"].get("kind")
            == "finite_rr_runtime_variant_proof"
        ]
        deterministic = [
            observation
            for observation in condition_observations
            if observation["variant_catalog_proof"].get("kind")
            == "deterministic_single_runtime_variant_proof"
        ]
        if finite and deterministic:
            raise OnsetEvidenceError(
                "one sampled condition mixes deterministic and finite-RR proofs"
            )
        if deterministic:
            if {
                observation["variation_slot"]
                for observation in deterministic
            } != {0}:
                raise OnsetEvidenceError(
                    "deterministic condition must use only variation slot 0"
                )
            continue
        if not finite:
            raise OnsetEvidenceError(
                f"condition {condition_id} has no recognized runtime proof"
            )
        periods = {
            proof["variation_period"]
            for proof in (
                observation["variant_catalog_proof"]
                for observation in finite
            )
        }
        if len(periods) != 1:
            raise OnsetEvidenceError(
                "finite-RR observations disagree on variation period"
            )
        period = next(iter(periods))
        bundles_by_slot: dict[int, set[str]] = {}
        for observation in finite:
            proof = observation["variant_catalog_proof"]
            slot = observation["variation_slot"]
            if proof["variation_slot"] != slot:
                raise OnsetEvidenceError(
                    "finite-RR observation slot differs from its proof"
                )
            bundles_by_slot.setdefault(slot, set()).add(
                proof["slot_bundle_sha256"]
            )
        if set(bundles_by_slot) != set(range(period)):
            raise OnsetEvidenceError(
                "finite-RR condition does not render every cycle slot"
            )
        if any(
            len(bundle_hashes) != 1
            for bundle_hashes in bundles_by_slot.values()
        ):
            raise OnsetEvidenceError(
                "repeated finite-RR slot renders disagree on their bundle"
            )
        unique_bundles = {
            next(iter(bundle_hashes))
            for bundle_hashes in bundles_by_slot.values()
        }
        if len(unique_bundles) != period:
            raise OnsetEvidenceError(
                "finite-RR cycle contains duplicate slot bundles"
            )


def _replay_and_validate_variant_proof(
    *,
    manifest_document: dict[str, Any],
    manifest_path: Path,
    performance_path: Path,
    note_on_frame: int,
    condition_id: str,
    sampled_condition: dict[str, Any],
    variation_slot: int,
    selection_receipt: Any,
    variant_catalog_proof: Any,
    label: str,
) -> None:
    """Replay the exact allowed selector event and compare its capture.

    Only the whitelisted phase-one backends can pass the proof validator.
    Replaying through ``create_instrument`` prevents a local/wrapper backend
    from forging the name of a built-in complete-selection contract.
    """

    try:
        from .events import parse_performance_document
        from .instrument import create_instrument

        raw_performance = read_json_strict(performance_path)
        _expect_keys(
            raw_performance,
            required={
                "sample_rate",
                "channels",
                "duration_seconds",
                "tail_seconds",
                "events",
            },
            label=f"{label}.certified_performance",
        )
        if not isinstance(raw_performance["events"], list):
            raise OnsetEvidenceError(
                f"{label}.certified_performance.events must be an array"
            )
        for event_index, raw_event in enumerate(
            raw_performance["events"]
        ):
            event_label = (
                f"{label}.certified_performance.events[{event_index}]"
            )
            if not isinstance(raw_event, dict):
                raise OnsetEvidenceError(f"{event_label} must be an object")
            event_type = raw_event.get("type")
            if event_type == "articulation":
                expected_keys = {"time", "type", "name"}
            elif event_type == "note_on":
                expected_keys = {
                    "time",
                    "type",
                    "note_id",
                    "midi_note",
                    "velocity",
                }
                if raw_event.get("note_id") != 1:
                    raise OnsetEvidenceError(
                        f"{event_label}.note_id must be the isolated probe id 1"
                    )
            elif event_type == "note_off":
                expected_keys = {
                    "time",
                    "type",
                    "note_id",
                    "release_velocity",
                }
                if raw_event.get("note_id") != 1:
                    raise OnsetEvidenceError(
                        f"{event_label}.note_id must be the isolated probe id 1"
                    )
            else:
                raise OnsetEvidenceError(
                    f"{event_label} is not part of the certified probe grammar"
                )
            _expect_keys(
                raw_event,
                required=expected_keys,
                label=event_label,
            )
        performance = parse_performance_document(
            raw_performance
        )
        instrument = create_instrument(
            json.loads(canonical_json_bytes(manifest_document).decode("utf-8")),
            performance.sample_rate,
            base_directory=str(manifest_path.parent),
        )
        try:
            if variation_slot:
                prewarm_dedicated_sfz_variation_slot(
                    instrument=instrument,
                    manifest=manifest_document,
                    sampled_condition=sampled_condition,
                    variation_slot=variation_slot,
                )
            found_note_on = False
            capture = None
            for event in performance.events:
                if event.sample > note_on_frame:
                    break
                if (
                    event.type == "note_on"
                    and event.sample == note_on_frame
                ):
                    # Mirror the probe's attack-only capture boundary.  In
                    # particular, an articulation event is setup rather than
                    # an attack selector, and note_off release-trigger
                    # catalogs belong to another phase.
                    with capture_runtime_variants() as attack_capture:
                        instrument.handle_event(
                            event,
                            performance.tuning,
                        )
                    capture = attack_capture
                    found_note_on = True
                    break
                else:
                    instrument.handle_event(event, performance.tuning)
            if not found_note_on or capture is None:
                raise OnsetEvidenceError(
                    f"{label} replay did not reach its bound note_on"
                )
            replayed_receipt = capture.receipt()
            stored_receipt = validate_runtime_variant_selection_receipt(
                selection_receipt
            )
            if replayed_receipt != stored_receipt:
                raise OnsetEvidenceError(
                    f"{label} selection receipt does not match runtime replay"
                )
            validate_runtime_variant_observation_proof(
                variant_catalog_proof,
                instrument=instrument,
                manifest=manifest_document,
                selection_receipt=stored_receipt,
                condition_id=condition_id,
                sampled_condition=sampled_condition,
                variation_slot=variation_slot,
            )
        finally:
            close = getattr(instrument, "close", None)
            if callable(close):
                close()
    except OnsetEvidenceError:
        raise
    except (KeyError, TypeError, ValueError, RuntimeVariantError) as error:
        raise OnsetEvidenceError(
            f"{label} runtime variant certification is invalid: {error}"
        ) from error


def validate_candidate_report(
    document: Any,
    *,
    project_root: str | Path,
    verify_current: bool = True,
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    candidate = _expect_keys(
        document,
        required={
            "$schema",
            "schema_version",
            "kind",
            "candidate_sha256",
            "automatic_approval",
            "created_at",
            "instrument",
            "runtime_fingerprint",
            "protocol",
            "observations",
        },
        label="candidate",
    )
    if candidate["$schema"] != CANDIDATE_SCHEMA:
        raise OnsetEvidenceError("candidate has an unexpected $schema")
    if candidate["schema_version"] != SCHEMA_VERSION:
        raise OnsetEvidenceError("candidate schema_version is unsupported")
    if candidate["kind"] != "onset_candidate_report":
        raise OnsetEvidenceError("candidate kind is invalid")
    _sha256(candidate["candidate_sha256"], "candidate.candidate_sha256")
    if candidate["automatic_approval"] is not False:
        raise OnsetEvidenceError("machine candidates may never auto-approve")
    _timestamp_or_null(candidate["created_at"], "candidate.created_at")
    if candidate["candidate_sha256"] != canonical_sha256(
        candidate,
        omit="candidate_sha256",
    ):
        raise OnsetEvidenceError("candidate self hash does not match its contents")

    instrument = _expect_keys(
        candidate["instrument"],
        required={"manifest_path", "manifest_sha256"},
        label="candidate.instrument",
    )
    manifest_path = resolve_project_path(
        project_root,
        instrument["manifest_path"],
    )
    expected_manifest_hash = _sha256(
        instrument["manifest_sha256"],
        "candidate.instrument.manifest_sha256",
    )
    if sha256_file(manifest_path) != expected_manifest_hash:
        raise OnsetEvidenceError("candidate manifest hash is stale")
    manifest_document = read_json_strict(manifest_path)
    if str(manifest_document.get("type", "")).casefold() == "reversed_cymbal":
        raise OnsetEvidenceError(
            "reversed_cymbal is anticipatory and cannot supply attack onset evidence"
        )

    protocol = _expect_keys(
        candidate["protocol"],
        required={
            "anchor",
            "context",
            "variant_coverage",
            "signal_stage",
            "pre_roll_frames",
            "sample_rate_hz",
            "algorithm_sha256",
            "window_ms",
            "hop_ms",
            "threshold_policy",
        },
        optional={"condition_coverage"},
        label="candidate.protocol",
    )
    if protocol["anchor"] != ANCHOR:
        raise OnsetEvidenceError("candidate protocol uses an unknown anchor")
    if protocol["context"] != CONTEXT:
        raise OnsetEvidenceError("candidate protocol context must be isolated_attack")
    if protocol["variant_coverage"] not in {
        VARIANT_COVERAGE,
        APPROVABLE_VARIANT_COVERAGE,
    }:
        raise OnsetEvidenceError(
            "candidate variant coverage declaration is unsupported"
        )
    condition_coverage = _validate_condition_coverage(
        protocol.get("condition_coverage"),
        required=(
            protocol["variant_coverage"]
            == APPROVABLE_VARIANT_COVERAGE
        ),
    )
    if protocol["signal_stage"] != "instrument_direct_output_no_space":
        raise OnsetEvidenceError(
            "candidate signal stage must be instrument_direct_output_no_space"
        )
    pre_roll_frames = _integer(
        protocol["pre_roll_frames"],
        "candidate.protocol.pre_roll_frames",
        minimum=1,
    )
    sample_rate_hz = _integer(
        protocol["sample_rate_hz"],
        "candidate.protocol.sample_rate_hz",
        minimum=8_000,
        maximum=384_000,
    )
    algorithm_hash = _sha256(
        protocol["algorithm_sha256"],
        "candidate.protocol.algorithm_sha256",
    )
    for field in ("window_ms", "hop_ms"):
        number = _number_or_null(
            protocol[field],
            f"candidate.protocol.{field}",
        )
        if number is None or number <= 0.0:
            raise OnsetEvidenceError(f"candidate.protocol.{field} must be positive")
    _string(protocol["threshold_policy"], "candidate.protocol.threshold_policy")
    probe_source = Path(project_root).resolve() / "tianlai" / "onset_probe.py"
    if verify_current and (
        not probe_source.is_file() or sha256_file(probe_source) != algorithm_hash
    ):
        raise OnsetEvidenceError("candidate analysis algorithm hash is stale")

    if not isinstance(candidate["observations"], list) or not candidate["observations"]:
        raise OnsetEvidenceError("candidate.observations must be non-empty")
    identifiers: set[str] = set()
    bound_paths: set[str] = set()
    observed_condition_ids: set[str] = set()
    for index, raw_observation in enumerate(candidate["observations"]):
        label = f"candidate.observations[{index}]"
        observation = _expect_keys(
            raw_observation,
            required={
                "observation_id",
                "final_articulation",
                "midi_note",
                "velocity",
                "performance_path",
                "performance_sha256",
                "wav_path",
                "wav_sha256",
                "note_on_frame",
                "analysis",
            },
            optional={
                "condition_id",
                "variation_slot",
                "variant_catalog_proof",
                "selection_receipt",
            },
            label=label,
        )
        observation_id = _string(
            observation["observation_id"],
            f"{label}.observation_id",
        )
        if _OBSERVATION_ID.fullmatch(observation_id) is None:
            raise OnsetEvidenceError(f"{label}.observation_id is not portable")
        if observation_id in identifiers:
            raise OnsetEvidenceError(f"duplicate observation_id: {observation_id}")
        identifiers.add(observation_id)
        articulation = _string(
            observation["final_articulation"],
            f"{label}.final_articulation",
        )
        if articulation.casefold().startswith("crescendo_"):
            raise OnsetEvidenceError(
                f"{label} uses anticipatory crescendo audio, not an attack onset"
            )
        midi_note = _integer(
            observation["midi_note"],
            f"{label}.midi_note",
            maximum=127,
        )
        velocity = _integer(
            observation["velocity"],
            f"{label}.velocity",
            minimum=1,
            maximum=127,
        )
        note_on_frame = _integer(
            observation["note_on_frame"],
            f"{label}.note_on_frame",
        )
        if note_on_frame != pre_roll_frames:
            raise OnsetEvidenceError(
                f"{label}.note_on_frame must equal the fixed pre-roll anchor"
            )
        variant_fields = {
            "condition_id",
            "variation_slot",
            "variant_catalog_proof",
            "selection_receipt",
        }
        present_variant_fields = variant_fields.intersection(observation)
        if present_variant_fields and present_variant_fields != variant_fields:
            raise OnsetEvidenceError(
                f"{label} must bind condition, slot, catalog proof, and "
                "selection receipt together"
            )
        has_variant_binding = present_variant_fields == variant_fields
        expected_condition_id = onset_sampled_condition_id(
            final_articulation=articulation,
            midi_note=midi_note,
            velocity=velocity,
            sample_rate_hz=sample_rate_hz,
        )
        expected_sampled_condition = onset_sampled_condition(
            final_articulation=articulation,
            midi_note=midi_note,
            velocity=velocity,
            sample_rate_hz=sample_rate_hz,
        )
        if has_variant_binding:
            condition_id = _sha256(
                observation["condition_id"],
                f"{label}.condition_id",
            )
            if condition_id != expected_condition_id:
                raise OnsetEvidenceError(
                    f"{label}.condition_id does not match its sampled condition"
                )
            variation_slot = _integer(
                observation["variation_slot"],
                f"{label}.variation_slot",
            )
            try:
                stored_receipt = validate_runtime_variant_selection_receipt(
                    observation["selection_receipt"]
                )
            except RuntimeVariantError as error:
                raise OnsetEvidenceError(
                    f"{label}.selection_receipt is invalid: {error}"
                ) from error
            if observation["variant_catalog_proof"] is not None:
                try:
                    validate_runtime_variant_proof_document(
                        observation["variant_catalog_proof"],
                        selection_receipt=stored_receipt,
                        condition_id=condition_id,
                        sampled_condition=expected_sampled_condition,
                        variation_slot=variation_slot,
                    )
                except RuntimeVariantError as error:
                    raise OnsetEvidenceError(
                        f"{label}.variant_catalog_proof is invalid: {error}"
                    ) from error
            observed_condition_ids.add(condition_id)
        else:
            condition_id = expected_condition_id
            variation_slot = 0
            if (
                protocol["variant_coverage"]
                == APPROVABLE_VARIANT_COVERAGE
            ):
                raise OnsetEvidenceError(
                    f"{label} lacks certified runtime variant evidence"
                )
        performance_path = resolve_project_path(
            project_root,
            observation["performance_path"],
        )
        wav_path = resolve_project_path(project_root, observation["wav_path"])
        for path_label in (
            str(observation["performance_path"]),
            str(observation["wav_path"]),
        ):
            if path_label in bound_paths:
                raise OnsetEvidenceError(
                    f"each observation must bind unique artifacts: {path_label}"
                )
            bound_paths.add(path_label)
        if verify_artifacts:
            if sha256_file(performance_path) != _sha256(
                observation["performance_sha256"],
                f"{label}.performance_sha256",
            ):
                raise OnsetEvidenceError(f"{label} performance hash is stale")
            if sha256_file(wav_path) != _sha256(
                observation["wav_sha256"],
                f"{label}.wav_sha256",
            ):
                raise OnsetEvidenceError(f"{label} WAV hash is stale")
            wav_rate, wav_frames, wav_channels = _wav_properties(wav_path)
            if wav_rate != sample_rate_hz or wav_channels != 2:
                raise OnsetEvidenceError(
                    f"{label} WAV must be stereo at the protocol sample rate"
                )
            if note_on_frame >= wav_frames:
                raise OnsetEvidenceError(f"{label} note_on anchor lies after the WAV")
            _validate_performance_anchor(
                performance_path,
                sample_rate_hz=sample_rate_hz,
                note_on_frame=note_on_frame,
                articulation=articulation,
                midi_note=midi_note,
                velocity=velocity,
            )
            if observation.get("variant_catalog_proof") is not None:
                _replay_and_validate_variant_proof(
                    manifest_document=manifest_document,
                    manifest_path=manifest_path,
                    performance_path=performance_path,
                    note_on_frame=note_on_frame,
                    condition_id=condition_id,
                    sampled_condition=expected_sampled_condition,
                    variation_slot=variation_slot,
                    selection_receipt=observation["selection_receipt"],
                    variant_catalog_proof=observation[
                        "variant_catalog_proof"
                    ],
                    label=label,
                )
            maximum_relative_frame = wav_frames - note_on_frame - 1
        else:
            _sha256(
                observation["performance_sha256"],
                f"{label}.performance_sha256",
            )
            _sha256(observation["wav_sha256"], f"{label}.wav_sha256")
            maximum_relative_frame = 2**63 - 1
        _validate_candidate_analysis(
            observation["analysis"],
            label=f"{label}.analysis",
            maximum_relative_frame=maximum_relative_frame,
        )

    _validate_complete_runtime_variant_slots(
        candidate["observations"],
        required=(
            protocol["variant_coverage"]
            == APPROVABLE_VARIANT_COVERAGE
        ),
    )
    if condition_coverage is not None:
        declared_condition_ids = condition_coverage["condition_ids"]
        if not observed_condition_ids:
            raise OnsetEvidenceError(
                "condition coverage is declared without bound observations"
            )
        if declared_condition_ids != sorted(observed_condition_ids):
            raise OnsetEvidenceError(
                "sampled condition coverage differs from observation bindings"
            )
    elif observed_condition_ids:
        raise OnsetEvidenceError(
            "variant-bound observations require explicit sampled condition coverage"
        )
    if (
        protocol["variant_coverage"] == APPROVABLE_VARIANT_COVERAGE
        and any(
            observation.get("variant_catalog_proof") is None
            for observation in candidate["observations"]
        )
    ):
        raise OnsetEvidenceError(
            "all_runtime_variants requires a certified proof for every observation"
        )

    if verify_current:
        fingerprint = validate_runtime_fingerprint(
            candidate["runtime_fingerprint"],
            project_root=project_root,
            manifest_path=manifest_path,
        )
    else:
        fingerprint = candidate["runtime_fingerprint"]
    if fingerprint["manifest"] != {
        "path": instrument["manifest_path"],
        "sha256": instrument["manifest_sha256"],
    }:
        raise OnsetEvidenceError(
            "candidate instrument binding differs from runtime fingerprint"
        )
    return candidate


def load_candidate_report(
    path: str | Path,
    *,
    project_root: str | Path,
    verify_current: bool = True,
    verify_artifacts: bool = True,
) -> dict[str, Any]:
    root = Path(project_root).resolve()
    source = Path(path)
    if not source.is_absolute():
        source = root / source
    source = source.resolve()
    _project_relative_label(root, source)
    return validate_candidate_report(
        read_json_strict(source),
        project_root=root,
        verify_current=verify_current,
        verify_artifacts=verify_artifacts,
    )


def _candidate_observation_map(
    candidate: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        str(observation["observation_id"]): observation
        for observation in candidate["observations"]
    }


def validate_review_decision(
    document: Any,
    *,
    project_root: str | Path,
    require_complete: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    review = _expect_keys(
        document,
        required={
            "$schema",
            "schema_version",
            "kind",
            "review_sha256",
            "automatic_decision",
            "candidate_path",
            "candidate_sha256",
            "candidate_file_sha256",
            "status",
            "reviewer",
            "created_at",
            "completed_at",
            "decisions",
        },
        label="review",
    )
    if review["$schema"] != REVIEW_SCHEMA:
        raise OnsetEvidenceError("review has an unexpected $schema")
    if review["schema_version"] != SCHEMA_VERSION:
        raise OnsetEvidenceError("review schema_version is unsupported")
    if review["kind"] != "onset_review_decision":
        raise OnsetEvidenceError("review kind is invalid")
    _sha256(review["review_sha256"], "review.review_sha256")
    if review["review_sha256"] != canonical_sha256(
        review,
        omit="review_sha256",
    ):
        raise OnsetEvidenceError("review self hash does not match its contents")
    if review["automatic_decision"] is not False:
        raise OnsetEvidenceError("onset review decisions must be manual")
    candidate_path = resolve_project_path(project_root, review["candidate_path"])
    candidate = load_candidate_report(
        candidate_path,
        project_root=project_root,
    )
    if review["candidate_sha256"] != candidate["candidate_sha256"]:
        raise OnsetEvidenceError("review binds another candidate self hash")
    _sha256(review["candidate_file_sha256"], "review.candidate_file_sha256")
    if sha256_file(candidate_path) != review["candidate_file_sha256"]:
        raise OnsetEvidenceError("review candidate file hash is stale")
    if review["status"] not in {"draft", "complete"}:
        raise OnsetEvidenceError("review.status must be draft or complete")
    if require_complete and review["status"] != "complete":
        raise OnsetEvidenceError("review has not been finalized")

    reviewer = _expect_keys(
        review["reviewer"],
        required={"reviewer_id", "display_name"},
        label="review.reviewer",
    )
    _string(reviewer["reviewer_id"], "review.reviewer.reviewer_id")
    _string(
        reviewer["display_name"],
        "review.reviewer.display_name",
        allow_empty=True,
    )
    _timestamp_or_null(review["created_at"], "review.created_at")
    completed_at = _timestamp_or_null(
        review["completed_at"],
        "review.completed_at",
    )
    if review["status"] == "draft" and completed_at is not None:
        raise OnsetEvidenceError("draft review cannot have completed_at")
    if review["status"] == "complete" and completed_at is None:
        raise OnsetEvidenceError("complete review requires completed_at")

    if not isinstance(review["decisions"], list):
        raise OnsetEvidenceError("review.decisions must be an array")
    observations = _candidate_observation_map(candidate)
    seen: set[str] = set()
    for index, raw_decision in enumerate(review["decisions"]):
        label = f"review.decisions[{index}]"
        decision = _expect_keys(
            raw_decision,
            required={
                "observation_id",
                "status",
                "measured_onset_frame",
                "comment",
                "decided_at",
            },
            label=label,
        )
        observation_id = _string(
            decision["observation_id"],
            f"{label}.observation_id",
        )
        if observation_id in seen:
            raise OnsetEvidenceError(f"duplicate review decision: {observation_id}")
        seen.add(observation_id)
        if observation_id not in observations:
            raise OnsetEvidenceError(
                f"review contains unknown observation: {observation_id}"
            )
        status = decision["status"]
        if status not in _DECISION_STATUSES:
            raise OnsetEvidenceError(f"{label}.status is invalid")
        comment = _string(
            decision["comment"],
            f"{label}.comment",
            allow_empty=True,
        )
        measured_frame = _integer_or_null(
            decision["measured_onset_frame"],
            f"{label}.measured_onset_frame",
        )
        decided_at = _timestamp_or_null(
            decision["decided_at"],
            f"{label}.decided_at",
        )
        observation = observations[observation_id]
        if status == "pending":
            if measured_frame is not None or decided_at is not None or comment:
                raise OnsetEvidenceError(
                    f"{label} pending decision must remain empty"
                )
        elif status == "measured":
            if measured_frame is None or decided_at is None:
                raise OnsetEvidenceError(
                    f"{label} measured decision needs a frame and timestamp"
                )
            if measured_frame < observation["note_on_frame"]:
                raise OnsetEvidenceError(
                    f"{label} measured onset precedes the fixed note_on anchor"
                )
            wav_path = resolve_project_path(
                project_root,
                observation["wav_path"],
            )
            _, wav_frames, _ = _wav_properties(wav_path)
            if measured_frame >= wav_frames:
                raise OnsetEvidenceError(
                    f"{label} measured onset lies after the bound WAV"
                )
        else:
            if measured_frame is not None or decided_at is None:
                raise OnsetEvidenceError(
                    f"{label} {status} decision cannot carry a measured frame"
                )
            if not comment.strip():
                raise OnsetEvidenceError(
                    f"{label} {status} decision requires a reason"
                )

    if seen != set(observations):
        missing = sorted(set(observations) - seen)
        raise OnsetEvidenceError(
            "review is missing observation decisions: " + ", ".join(missing)
        )
    if review["status"] == "complete" and any(
        decision["status"] == "pending" for decision in review["decisions"]
    ):
        raise OnsetEvidenceError("complete review still contains pending decisions")
    return review, candidate


def load_review_decision(
    path: str | Path,
    *,
    project_root: str | Path,
    require_complete: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    root = Path(project_root).resolve()
    source = Path(path)
    if not source.is_absolute():
        source = root / source
    source = source.resolve()
    _project_relative_label(root, source)
    return validate_review_decision(
        read_json_strict(source),
        project_root=root,
        require_complete=require_complete,
    )


def create_review_draft(
    candidate_path: str | Path,
    output_path: str | Path,
    *,
    project_root: str | Path,
    reviewer_id: str,
    display_name: str = "",
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create one pending decision per observation; never auto-accept anything."""

    root = Path(project_root).resolve()
    candidate_file = Path(candidate_path)
    if not candidate_file.is_absolute():
        candidate_file = root / candidate_file
    candidate_file = candidate_file.resolve()
    output_file = Path(output_path)
    if not output_file.is_absolute():
        output_file = root / output_file
    output_file = output_file.resolve()
    _project_relative_label(root, output_file)
    if output_file == candidate_file:
        raise OnsetEvidenceError("review output must not overwrite its candidate")
    candidate = load_candidate_report(
        candidate_file,
        project_root=root,
    )
    reviewer = _string(reviewer_id, "reviewer_id")
    display = _string(display_name, "display_name", allow_empty=True)
    timestamp = created_at or _now_utc()
    _timestamp_or_null(timestamp, "created_at")
    review: dict[str, Any] = {
        "$schema": REVIEW_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": "onset_review_decision",
        "review_sha256": "0" * 64,
        "automatic_decision": False,
        "candidate_path": _project_relative_label(root, candidate_file),
        "candidate_sha256": candidate["candidate_sha256"],
        "candidate_file_sha256": sha256_file(candidate_file),
        "status": "draft",
        "reviewer": {
            "reviewer_id": reviewer,
            "display_name": display,
        },
        "created_at": timestamp,
        "completed_at": None,
        "decisions": [
            {
                "observation_id": observation["observation_id"],
                "status": "pending",
                "measured_onset_frame": None,
                "comment": "",
                "decided_at": None,
            }
            for observation in candidate["observations"]
        ],
    }
    review["review_sha256"] = canonical_sha256(review, omit="review_sha256")
    validate_review_decision(review, project_root=root)
    write_json_atomic(output_file, review)
    return review


def record_review_decision(
    review_path: str | Path,
    *,
    project_root: str | Path,
    observation_id: str,
    status: Literal["measured", "exclude", "unsure"],
    measured_onset_frame: int | None = None,
    measured_delay_frames: int | None = None,
    comment: str = "",
    decided_at: str | None = None,
) -> dict[str, Any]:
    """Record exactly one human decision.

    There is intentionally no batch or "accept all" counterpart.
    """

    review_file = Path(review_path)
    root = Path(project_root).resolve()
    if not review_file.is_absolute():
        review_file = root / review_file
    review_file = review_file.resolve()
    _project_relative_label(root, review_file)
    review, candidate = load_review_decision(
        review_file,
        project_root=root,
    )
    if review["status"] != "draft":
        raise OnsetEvidenceError("a finalized review cannot be edited")
    identifier = _string(observation_id, "observation_id")
    observations = _candidate_observation_map(candidate)
    if identifier not in observations:
        raise OnsetEvidenceError(f"unknown observation_id: {identifier}")
    if measured_onset_frame is not None and measured_delay_frames is not None:
        raise OnsetEvidenceError(
            "measured_onset_frame and measured_delay_frames are mutually exclusive"
        )
    if status not in {"measured", "exclude", "unsure"}:
        raise OnsetEvidenceError(
            "recorded status must be measured, exclude, or unsure"
        )
    if status == "measured":
        if measured_delay_frames is not None:
            delay = _integer(measured_delay_frames, "measured_delay_frames")
            measured_onset_frame = (
                int(observations[identifier]["note_on_frame"]) + delay
            )
        _integer(measured_onset_frame, "measured_onset_frame")
    elif measured_onset_frame is not None or measured_delay_frames is not None:
        raise OnsetEvidenceError(
            f"{status} decision cannot carry a measured frame or delay"
        )
    note = _string(comment, "comment", allow_empty=True)
    if status != "measured" and not note.strip():
        raise OnsetEvidenceError(f"{status} decision requires a comment")
    timestamp = decided_at or _now_utc()
    _timestamp_or_null(timestamp, "decided_at")

    matches = [
        decision
        for decision in review["decisions"]
        if decision["observation_id"] == identifier
    ]
    if len(matches) != 1:
        raise OnsetEvidenceError(f"unknown observation_id: {identifier}")
    decision = matches[0]
    decision["status"] = status
    decision["measured_onset_frame"] = (
        measured_onset_frame if status == "measured" else None
    )
    decision["comment"] = note
    decision["decided_at"] = timestamp
    review["review_sha256"] = canonical_sha256(review, omit="review_sha256")
    validate_review_decision(review, project_root=root)
    write_json_atomic(review_file, review)
    return review


def finalize_review(
    review_path: str | Path,
    *,
    project_root: str | Path,
    completed_at: str | None = None,
) -> dict[str, Any]:
    review_file = Path(review_path)
    root = Path(project_root).resolve()
    if not review_file.is_absolute():
        review_file = root / review_file
    review_file = review_file.resolve()
    _project_relative_label(root, review_file)
    review, _ = load_review_decision(
        review_file,
        project_root=root,
    )
    if review["status"] != "draft":
        raise OnsetEvidenceError("review is already finalized")
    pending = [
        decision["observation_id"]
        for decision in review["decisions"]
        if decision["status"] == "pending"
    ]
    if pending:
        raise OnsetEvidenceError(
            "cannot finalize; observations remain unreviewed: "
            + ", ".join(pending)
        )
    timestamp = completed_at or _now_utc()
    _timestamp_or_null(timestamp, "completed_at")
    review["status"] = "complete"
    review["completed_at"] = timestamp
    review["review_sha256"] = canonical_sha256(review, omit="review_sha256")
    validate_review_decision(
        review,
        project_root=root,
        require_complete=True,
    )
    write_json_atomic(review_file, review)
    return review


def _positive_finite(value: Any, label: str) -> float:
    number = _number_or_null(value, label)
    if number is None or number <= 0.0:
        raise OnsetEvidenceError(f"{label} must be positive")
    return number


def _build_portable_proof(
    candidate: dict[str, Any],
    review: dict[str, Any],
) -> dict[str, Any]:
    """Extract the compact proof needed by a source-free release package."""

    return {
        "candidate": {
            "candidate_sha256": candidate["candidate_sha256"],
            "automatic_approval": candidate["automatic_approval"],
            "protocol": {
                "anchor": candidate["protocol"]["anchor"],
                "context": candidate["protocol"]["context"],
                "variant_coverage": candidate["protocol"]["variant_coverage"],
                "condition_coverage": json.loads(
                    canonical_json_bytes(
                        candidate["protocol"]["condition_coverage"]
                    ).decode("utf-8")
                ),
                "signal_stage": candidate["protocol"]["signal_stage"],
                "sample_rate_hz": candidate["protocol"]["sample_rate_hz"],
            },
            "observations": [
                {
                    "observation_id": observation["observation_id"],
                    "final_articulation": observation["final_articulation"],
                    "midi_note": observation["midi_note"],
                    "velocity": observation["velocity"],
                    "note_on_frame": observation["note_on_frame"],
                    "condition_id": observation["condition_id"],
                    "variation_slot": observation["variation_slot"],
                    "variant_catalog_proof": json.loads(
                        canonical_json_bytes(
                            observation["variant_catalog_proof"]
                        ).decode("utf-8")
                    ),
                    "selection_receipt": json.loads(
                        canonical_json_bytes(
                            observation["selection_receipt"]
                        ).decode("utf-8")
                    ),
                    "analysis_status": observation["analysis"]["status"],
                    "performance_sha256": observation["performance_sha256"],
                    "wav_sha256": observation["wav_sha256"],
                }
                for observation in candidate["observations"]
            ],
        },
        "review": {
            "review_sha256": review["review_sha256"],
            "candidate_sha256": review["candidate_sha256"],
            "automatic_decision": review["automatic_decision"],
            "status": review["status"],
            "reviewer": dict(review["reviewer"]),
            "decisions": [
                {
                    "observation_id": decision["observation_id"],
                    "status": decision["status"],
                    "measured_onset_frame": decision["measured_onset_frame"],
                }
                for decision in review["decisions"]
            ],
        },
    }


def _validate_portable_proof(proof: Any) -> dict[str, Any]:
    value = _expect_keys(
        proof,
        required={"candidate", "review"},
        label="approved.portable_proof",
    )
    candidate = _expect_keys(
        value["candidate"],
        required={
            "candidate_sha256",
            "automatic_approval",
            "protocol",
            "observations",
        },
        label="approved.portable_proof.candidate",
    )
    _sha256(
        candidate["candidate_sha256"],
        "approved.portable_proof.candidate.candidate_sha256",
    )
    if candidate["automatic_approval"] is not False:
        raise OnsetEvidenceError("portable candidate proof cannot auto-approve")
    protocol = _expect_keys(
        candidate["protocol"],
        required={
            "anchor",
            "context",
            "variant_coverage",
            "condition_coverage",
            "signal_stage",
            "sample_rate_hz",
        },
        label="approved.portable_proof.candidate.protocol",
    )
    if protocol["anchor"] != ANCHOR or protocol["context"] != CONTEXT:
        raise OnsetEvidenceError("portable proof anchor or context is unsupported")
    if protocol["variant_coverage"] != APPROVABLE_VARIANT_COVERAGE:
        raise OnsetEvidenceError(
            "portable proof does not cover all runtime variants"
        )
    condition_coverage = _validate_condition_coverage(
        protocol["condition_coverage"],
        required=True,
    )
    assert condition_coverage is not None
    if protocol["signal_stage"] != "instrument_direct_output_no_space":
        raise OnsetEvidenceError("portable proof signal stage is unsupported")
    _integer(
        protocol["sample_rate_hz"],
        "approved.portable_proof.candidate.protocol.sample_rate_hz",
        minimum=8_000,
        maximum=384_000,
    )
    if not isinstance(candidate["observations"], list) or not candidate["observations"]:
        raise OnsetEvidenceError("portable proof observations must be non-empty")
    observation_ids: set[str] = set()
    observed_condition_ids: set[str] = set()
    for index, raw_observation in enumerate(candidate["observations"]):
        label = f"approved.portable_proof.candidate.observations[{index}]"
        observation = _expect_keys(
            raw_observation,
            required={
                "observation_id",
                "final_articulation",
                "midi_note",
                "velocity",
                "note_on_frame",
                "condition_id",
                "variation_slot",
                "variant_catalog_proof",
                "selection_receipt",
                "analysis_status",
                "performance_sha256",
                "wav_sha256",
            },
            label=label,
        )
        observation_id = _string(
            observation["observation_id"],
            f"{label}.observation_id",
        )
        if _OBSERVATION_ID.fullmatch(observation_id) is None:
            raise OnsetEvidenceError(f"{label}.observation_id is not portable")
        if observation_id in observation_ids:
            raise OnsetEvidenceError(
                f"portable proof repeats observation {observation_id}"
            )
        observation_ids.add(observation_id)
        articulation = _string(
            observation["final_articulation"],
            f"{label}.final_articulation",
        )
        if articulation.casefold().startswith("crescendo_"):
            raise OnsetEvidenceError(
                "portable proof contains anticipatory crescendo audio"
            )
        midi_note = _integer(
            observation["midi_note"],
            f"{label}.midi_note",
            maximum=127,
        )
        velocity = _integer(
            observation["velocity"],
            f"{label}.velocity",
            minimum=1,
            maximum=127,
        )
        _integer(observation["note_on_frame"], f"{label}.note_on_frame")
        condition_id = _sha256(
            observation["condition_id"],
            f"{label}.condition_id",
        )
        expected_condition_id = onset_sampled_condition_id(
            final_articulation=articulation,
            midi_note=midi_note,
            velocity=velocity,
            sample_rate_hz=int(protocol["sample_rate_hz"]),
        )
        expected_sampled_condition = onset_sampled_condition(
            final_articulation=articulation,
            midi_note=midi_note,
            velocity=velocity,
            sample_rate_hz=int(protocol["sample_rate_hz"]),
        )
        if condition_id != expected_condition_id:
            raise OnsetEvidenceError(
                f"{label}.condition_id does not match its sampled condition"
            )
        variation_slot = _integer(
            observation["variation_slot"],
            f"{label}.variation_slot",
        )
        try:
            receipt = validate_runtime_variant_selection_receipt(
                observation["selection_receipt"]
            )
            validate_runtime_variant_proof_document(
                observation["variant_catalog_proof"],
                selection_receipt=receipt,
                condition_id=condition_id,
                sampled_condition=expected_sampled_condition,
                variation_slot=variation_slot,
            )
        except RuntimeVariantError as error:
            raise OnsetEvidenceError(
                f"{label} runtime variant proof is invalid: {error}"
            ) from error
        observed_condition_ids.add(condition_id)
        if observation["analysis_status"] not in {"proposed", "unresolved"}:
            raise OnsetEvidenceError(f"{label}.analysis_status is invalid")
        _sha256(observation["performance_sha256"], f"{label}.performance_sha256")
        _sha256(observation["wav_sha256"], f"{label}.wav_sha256")

    _validate_complete_runtime_variant_slots(
        candidate["observations"],
        required=True,
    )
    if condition_coverage["condition_ids"] != sorted(
        observed_condition_ids
    ):
        raise OnsetEvidenceError(
            "portable sampled condition coverage differs from its observations"
        )

    review = _expect_keys(
        value["review"],
        required={
            "review_sha256",
            "candidate_sha256",
            "automatic_decision",
            "status",
            "reviewer",
            "decisions",
        },
        label="approved.portable_proof.review",
    )
    _sha256(
        review["review_sha256"],
        "approved.portable_proof.review.review_sha256",
    )
    _sha256(
        review["candidate_sha256"],
        "approved.portable_proof.review.candidate_sha256",
    )
    if review["candidate_sha256"] != candidate["candidate_sha256"]:
        raise OnsetEvidenceError("portable review binds another candidate")
    if review["automatic_decision"] is not False:
        raise OnsetEvidenceError("portable review proof cannot be automatic")
    if review["status"] != "complete":
        raise OnsetEvidenceError("portable review proof must be complete")
    reviewer = _expect_keys(
        review["reviewer"],
        required={"reviewer_id", "display_name"},
        label="approved.portable_proof.review.reviewer",
    )
    _string(reviewer["reviewer_id"], "portable proof reviewer_id")
    _string(
        reviewer["display_name"],
        "portable proof reviewer display_name",
        allow_empty=True,
    )
    if not isinstance(review["decisions"], list):
        raise OnsetEvidenceError("portable proof decisions must be an array")
    decision_ids: set[str] = set()
    for index, raw_decision in enumerate(review["decisions"]):
        label = f"approved.portable_proof.review.decisions[{index}]"
        decision = _expect_keys(
            raw_decision,
            required={
                "observation_id",
                "status",
                "measured_onset_frame",
            },
            label=label,
        )
        observation_id = _string(
            decision["observation_id"],
            f"{label}.observation_id",
        )
        if observation_id in decision_ids:
            raise OnsetEvidenceError(
                f"portable proof repeats decision {observation_id}"
            )
        decision_ids.add(observation_id)
        if decision["status"] not in _DECISION_STATUSES:
            raise OnsetEvidenceError(f"{label}.status is invalid")
        measured = _integer_or_null(
            decision["measured_onset_frame"],
            f"{label}.measured_onset_frame",
        )
        if decision["status"] == "measured" and measured is None:
            raise OnsetEvidenceError(f"{label} measured decision needs a frame")
        if decision["status"] != "measured" and measured is not None:
            raise OnsetEvidenceError(
                f"{label} non-measured decision cannot carry a frame"
            )
    if decision_ids != observation_ids:
        raise OnsetEvidenceError(
            "portable proof decisions must cover every observation exactly once"
        )
    return value


def _derive_portable_articulations(
    proof: dict[str, Any],
    *,
    max_spread_ms: float,
) -> dict[str, dict[str, Any]]:
    proof = _validate_portable_proof(proof)
    threshold_ms = _positive_finite(max_spread_ms, "max_spread_ms")
    candidate = proof["candidate"]
    review = proof["review"]
    observations = {
        observation["observation_id"]: observation
        for observation in candidate["observations"]
    }
    unresolved = [
        observation["observation_id"]
        for observation in candidate["observations"]
        if observation["analysis_status"] == "unresolved"
    ]
    if unresolved:
        raise OnsetEvidenceError(
            "unresolved machine observations block approval: "
            + ", ".join(unresolved)
        )
    unsure = [
        decision["observation_id"]
        for decision in review["decisions"]
        if decision["status"] == "unsure"
    ]
    if unsure:
        raise OnsetEvidenceError(
            "human unsure decisions block approval: " + ", ".join(unsure)
        )

    sample_rate_hz = int(candidate["protocol"]["sample_rate_hz"])
    grouped: dict[str, list[tuple[str, int]]] = {}
    for decision in review["decisions"]:
        observation = observations[decision["observation_id"]]
        if decision["status"] == "exclude":
            continue
        if observation["analysis_status"] != "proposed":
            raise OnsetEvidenceError(
                "only proposed observations can contribute to approval"
            )
        if decision["status"] != "measured":
            raise OnsetEvidenceError(
                "every included proposed observation must be manually measured"
            )
        delay = int(decision["measured_onset_frame"]) - int(
            observation["note_on_frame"]
        )
        if delay < 0:
            raise OnsetEvidenceError("manual onset delay cannot be negative")
        articulation = str(observation["final_articulation"])
        grouped.setdefault(articulation, []).append(
            (str(observation["observation_id"]), delay)
        )

    if not grouped:
        raise OnsetEvidenceError("review contains no measured onset observations")
    result: dict[str, dict[str, Any]] = {}
    for articulation, observations_and_delays in sorted(grouped.items()):
        observations_and_delays.sort(key=lambda item: item[0])
        delays = sorted(delay for _, delay in observations_and_delays)
        spread_frames = delays[-1] - delays[0]
        spread_ms = spread_frames * 1000.0 / sample_rate_hz
        if spread_ms > threshold_ms + 1e-12:
            raise OnsetEvidenceError(
                f"articulation {articulation!r} onset spread is "
                f"{spread_ms:.3f} ms, above {threshold_ms:.3f} ms; "
                "a single scalar delay would be misleading"
            )
        middle = len(delays) // 2
        if len(delays) % 2:
            median_frames = delays[middle]
        else:
            median_frames = (delays[middle - 1] + delays[middle] + 1) // 2
        result[articulation] = {
            "final_articulation": articulation,
            "frames": median_frames,
            "sample_rate_hz": sample_rate_hz,
            "observation_count": len(delays),
            "spread_frames": spread_frames,
            "aggregation": "median_frames_half_up",
            "observation_ids": [
                observation_id
                for observation_id, _ in observations_and_delays
            ],
        }
    return result


def _validate_portable_runtime_contracts(
    proof: dict[str, Any],
    *,
    manifest_path: Path,
) -> None:
    """Bind portable certifications to the current exact built-in backend."""

    try:
        from .instrument import create_instrument

        manifest = read_json_strict(manifest_path)
        sample_rate = int(
            proof["candidate"]["protocol"]["sample_rate_hz"]
        )
        for index, observation in enumerate(
            proof["candidate"]["observations"]
        ):
            instrument = create_instrument(
                json.loads(canonical_json_bytes(manifest).decode("utf-8")),
                sample_rate,
                base_directory=str(manifest_path.parent),
            )
            try:
                sampled_condition = onset_sampled_condition(
                    final_articulation=observation[
                        "final_articulation"
                    ],
                    midi_note=observation["midi_note"],
                    velocity=observation["velocity"],
                    sample_rate_hz=sample_rate,
                )
                if observation["variation_slot"]:
                    prewarm_dedicated_sfz_variation_slot(
                        instrument=instrument,
                        manifest=manifest,
                        sampled_condition=sampled_condition,
                        variation_slot=observation["variation_slot"],
                    )
                validate_runtime_variant_observation_proof(
                    observation["variant_catalog_proof"],
                    instrument=instrument,
                    manifest=manifest,
                    selection_receipt=observation["selection_receipt"],
                    condition_id=observation["condition_id"],
                    sampled_condition=sampled_condition,
                    variation_slot=observation["variation_slot"],
                )
            finally:
                close = getattr(instrument, "close", None)
                if callable(close):
                    close()
    except (KeyError, TypeError, ValueError, RuntimeVariantError) as error:
        raise OnsetEvidenceError(
            "portable runtime variant contract is invalid for the current "
            f"backend: {error}"
        ) from error


def _derive_approved_articulations(
    candidate: dict[str, Any],
    review: dict[str, Any],
    *,
    max_spread_ms: float,
) -> dict[str, dict[str, Any]]:
    return _derive_portable_articulations(
        _build_portable_proof(candidate, review),
        max_spread_ms=max_spread_ms,
    )


def promote_review(
    candidate_path: str | Path,
    review_path: str | Path,
    output_path: str | Path,
    *,
    project_root: str | Path,
    explicit_approval: bool,
    review_lead: str,
    review_lead_display_name: str = "",
    max_spread_ms: float = 30.0,
    approved_at: str | None = None,
) -> dict[str, Any]:
    """Promote a complete manual review into conductor-readable evidence."""

    if explicit_approval is not True:
        raise OnsetEvidenceError(
            "approval requires the explicit_approval=True review-lead action"
        )
    lead_id = _string(review_lead, "review_lead")
    lead_display = _string(
        review_lead_display_name,
        "review_lead_display_name",
        allow_empty=True,
    )
    root = Path(project_root).resolve()
    candidate_file = Path(candidate_path)
    if not candidate_file.is_absolute():
        candidate_file = root / candidate_file
    candidate_file = candidate_file.resolve()
    review_file = Path(review_path)
    if not review_file.is_absolute():
        review_file = root / review_file
    review_file = review_file.resolve()
    _project_relative_label(root, candidate_file)
    _project_relative_label(root, review_file)
    output_file = Path(output_path)
    if not output_file.is_absolute():
        output_file = root / output_file
    output_file = output_file.resolve()
    _project_relative_label(root, output_file)
    if output_file in {candidate_file, review_file}:
        raise OnsetEvidenceError(
            "approved output must not overwrite its candidate or review"
        )
    candidate = load_candidate_report(
        candidate_file,
        project_root=root,
    )
    review, review_candidate = load_review_decision(
        review_file,
        project_root=root,
        require_complete=True,
    )
    if review_candidate["candidate_sha256"] != candidate["candidate_sha256"]:
        raise OnsetEvidenceError("review does not belong to this candidate")
    if review["candidate_path"] != _project_relative_label(
        root,
        candidate_file,
    ):
        # Do not accept another byte-identical candidate copied elsewhere.
        raise OnsetEvidenceError("review binds a different candidate path")
    if (
        candidate["protocol"]["variant_coverage"]
        != APPROVABLE_VARIANT_COVERAGE
    ):
        raise OnsetEvidenceError(
            "runtime_default_only probes are research candidates only; "
            "approval requires all_runtime_variants coverage"
        )

    threshold_ms = _positive_finite(max_spread_ms, "max_spread_ms")
    articulations = _derive_approved_articulations(
        candidate,
        review,
        max_spread_ms=threshold_ms,
    )
    timestamp = approved_at or _now_utc()
    _timestamp_or_null(timestamp, "approved_at")
    approved: dict[str, Any] = {
        "$schema": APPROVED_SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "kind": "approved_onset_evidence",
        "approved_sha256": "0" * 64,
        "automatic_approval": False,
        "approved_at": timestamp,
        "anchor": ANCHOR,
        "context": CONTEXT,
        "instrument": dict(candidate["instrument"]),
        "runtime_fingerprint": candidate["runtime_fingerprint"],
        "review_lead": {
            "reviewer_id": lead_id,
            "display_name": lead_display,
            "attestation": "explicit_manual_approval",
        },
        "policy": {
            "max_spread_ms": threshold_ms,
            "unresolved": "block",
            "unsure": "block",
            "variant_coverage": APPROVABLE_VARIANT_COVERAGE,
            "condition_coverage": _CONDITION_COVERAGE_KIND,
        },
        "sources": {
            "candidate_path": _project_relative_label(root, candidate_file),
            "candidate_sha256": candidate["candidate_sha256"],
            "candidate_file_sha256": sha256_file(candidate_file),
            "review_path": _project_relative_label(root, review_file),
            "review_sha256": review["review_sha256"],
            "review_file_sha256": sha256_file(review_file),
        },
        "portable_proof": _build_portable_proof(candidate, review),
        "articulations": articulations,
    }
    approved["approved_sha256"] = canonical_sha256(
        approved,
        omit="approved_sha256",
    )
    validate_approved_onset_evidence(
        approved,
        project_root=root,
        manifest_path=resolve_project_path(
            root,
            candidate["instrument"]["manifest_path"],
        ),
    )
    write_json_atomic(output_file, approved)
    return approved


def _validate_approved_articulation(
    raw_value: Any,
    *,
    name: str,
) -> dict[str, Any]:
    value = _expect_keys(
        raw_value,
        required={
            "final_articulation",
            "frames",
            "sample_rate_hz",
            "observation_count",
            "spread_frames",
            "aggregation",
            "observation_ids",
        },
        label=f"approved.articulations[{name!r}]",
    )
    if value["final_articulation"] != name:
        raise OnsetEvidenceError(
            f"approved articulation key and final_articulation differ: {name!r}"
        )
    _integer(value["frames"], f"approved.articulations[{name!r}].frames")
    _integer(
        value["sample_rate_hz"],
        f"approved.articulations[{name!r}].sample_rate_hz",
        minimum=8_000,
        maximum=384_000,
    )
    _integer(
        value["observation_count"],
        f"approved.articulations[{name!r}].observation_count",
        minimum=1,
    )
    _integer(
        value["spread_frames"],
        f"approved.articulations[{name!r}].spread_frames",
    )
    if value["aggregation"] != "median_frames_half_up":
        raise OnsetEvidenceError("approved onset aggregation is unsupported")
    if not isinstance(value["observation_ids"], list) or not value["observation_ids"]:
        raise OnsetEvidenceError(
            f"approved articulation {name!r} needs observation_ids"
        )
    identifiers = [
        _string(item, f"approved.articulations[{name!r}].observation_ids")
        for item in value["observation_ids"]
    ]
    if identifiers != sorted(set(identifiers)):
        raise OnsetEvidenceError(
            f"approved articulation {name!r} observation_ids must be unique "
            "and sorted"
        )
    if len(identifiers) != value["observation_count"]:
        raise OnsetEvidenceError(
            f"approved articulation {name!r} observation_count is inconsistent"
        )
    return value


def validate_approved_onset_evidence(
    document: Any,
    *,
    project_root: str | Path,
    manifest_path: str | Path | None = None,
    verify_source_chain: bool = True,
) -> dict[str, Any]:
    if not isinstance(verify_source_chain, bool):
        raise OnsetEvidenceError("verify_source_chain must be boolean")
    approved = _expect_keys(
        document,
        required={
            "$schema",
            "schema_version",
            "kind",
            "approved_sha256",
            "automatic_approval",
            "approved_at",
            "anchor",
            "context",
            "instrument",
            "runtime_fingerprint",
            "review_lead",
            "policy",
            "sources",
            "portable_proof",
            "articulations",
        },
        label="approved",
    )
    if approved["$schema"] != APPROVED_SCHEMA:
        raise OnsetEvidenceError("approved evidence has an unexpected $schema")
    if approved["schema_version"] != SCHEMA_VERSION:
        raise OnsetEvidenceError("approved evidence schema_version is unsupported")
    if approved["kind"] != "approved_onset_evidence":
        raise OnsetEvidenceError("approved evidence kind is invalid")
    _sha256(approved["approved_sha256"], "approved.approved_sha256")
    if approved["approved_sha256"] != canonical_sha256(
        approved,
        omit="approved_sha256",
    ):
        raise OnsetEvidenceError("approved evidence self hash is invalid")
    if approved["automatic_approval"] is not False:
        raise OnsetEvidenceError("automatic onset approval is forbidden")
    _timestamp_or_null(approved["approved_at"], "approved.approved_at")
    if approved["anchor"] != ANCHOR or approved["context"] != CONTEXT:
        raise OnsetEvidenceError("approved onset anchor or context is unsupported")

    instrument = _expect_keys(
        approved["instrument"],
        required={"manifest_path", "manifest_sha256"},
        label="approved.instrument",
    )
    stored_manifest = resolve_project_path(
        project_root,
        instrument["manifest_path"],
    )
    _sha256(instrument["manifest_sha256"], "approved.instrument.manifest_sha256")
    if sha256_file(stored_manifest) != instrument["manifest_sha256"]:
        raise OnsetEvidenceError("approved manifest hash is stale")
    if manifest_path is not None:
        expected_manifest = Path(manifest_path)
        if not expected_manifest.is_absolute():
            expected_manifest = Path(project_root) / expected_manifest
        if stored_manifest.resolve() != expected_manifest.resolve():
            raise OnsetEvidenceError("approved onset evidence belongs to another instrument")
    fingerprint = validate_runtime_fingerprint(
        approved["runtime_fingerprint"],
        project_root=project_root,
        manifest_path=stored_manifest,
    )
    if fingerprint["manifest"] != {
        "path": instrument["manifest_path"],
        "sha256": instrument["manifest_sha256"],
    }:
        raise OnsetEvidenceError(
            "approved instrument differs from runtime fingerprint"
        )

    lead = _expect_keys(
        approved["review_lead"],
        required={"reviewer_id", "display_name", "attestation"},
        label="approved.review_lead",
    )
    _string(lead["reviewer_id"], "approved.review_lead.reviewer_id")
    _string(
        lead["display_name"],
        "approved.review_lead.display_name",
        allow_empty=True,
    )
    if lead["attestation"] != "explicit_manual_approval":
        raise OnsetEvidenceError("approved evidence lacks review-lead attestation")

    policy = _expect_keys(
        approved["policy"],
        required={
            "max_spread_ms",
            "unresolved",
            "unsure",
            "variant_coverage",
            "condition_coverage",
        },
        label="approved.policy",
    )
    threshold_ms = _positive_finite(
        policy["max_spread_ms"],
        "approved.policy.max_spread_ms",
    )
    if policy["unresolved"] != "block" or policy["unsure"] != "block":
        raise OnsetEvidenceError("approved evidence weakens blocking review policy")
    if policy["variant_coverage"] != APPROVABLE_VARIANT_COVERAGE:
        raise OnsetEvidenceError("approved evidence overstates variant coverage")
    if policy["condition_coverage"] != _CONDITION_COVERAGE_KIND:
        raise OnsetEvidenceError(
            "approved evidence must describe sampled condition coverage"
        )

    sources = _expect_keys(
        approved["sources"],
        required={
            "candidate_path",
            "candidate_sha256",
            "candidate_file_sha256",
            "review_path",
            "review_sha256",
            "review_file_sha256",
        },
        label="approved.sources",
    )
    candidate_path = resolve_project_path(
        project_root,
        sources["candidate_path"],
        must_exist=verify_source_chain,
    )
    review_path = resolve_project_path(
        project_root,
        sources["review_path"],
        must_exist=verify_source_chain,
    )
    for field in (
        "candidate_sha256",
        "candidate_file_sha256",
        "review_sha256",
        "review_file_sha256",
    ):
        _sha256(sources[field], f"approved.sources.{field}")

    portable_proof = _validate_portable_proof(approved["portable_proof"])
    _validate_portable_runtime_contracts(
        portable_proof,
        manifest_path=stored_manifest,
    )
    if (
        portable_proof["candidate"]["candidate_sha256"]
        != sources["candidate_sha256"]
    ):
        raise OnsetEvidenceError(
            "portable proof candidate hash differs from approved sources"
        )
    if portable_proof["review"]["review_sha256"] != sources["review_sha256"]:
        raise OnsetEvidenceError(
            "portable proof review hash differs from approved sources"
        )
    if portable_proof["candidate"]["protocol"]["anchor"] != approved["anchor"]:
        raise OnsetEvidenceError("portable proof anchor differs from approved")
    if portable_proof["candidate"]["protocol"]["context"] != approved["context"]:
        raise OnsetEvidenceError("portable proof context differs from approved")
    if (
        portable_proof["candidate"]["protocol"]["variant_coverage"]
        != policy["variant_coverage"]
    ):
        raise OnsetEvidenceError(
            "portable proof variant coverage differs from approved policy"
        )
    if (
        portable_proof["candidate"]["protocol"]["condition_coverage"]["kind"]
        != policy["condition_coverage"]
    ):
        raise OnsetEvidenceError(
            "portable proof condition coverage differs from approved policy"
        )
    if not isinstance(approved["articulations"], dict) or not approved["articulations"]:
        raise OnsetEvidenceError("approved.articulations must be non-empty")
    for name, raw_articulation in approved["articulations"].items():
        _string(name, "approved articulation name")
        _validate_approved_articulation(raw_articulation, name=name)
    portable_recomputed = _derive_portable_articulations(
        portable_proof,
        max_spread_ms=threshold_ms,
    )
    if approved["articulations"] != portable_recomputed:
        raise OnsetEvidenceError(
            "approved articulation values do not match portable manual proof"
        )
    if verify_source_chain:
        if sha256_file(candidate_path) != sources["candidate_file_sha256"]:
            raise OnsetEvidenceError("approved candidate source file is stale")
        if sha256_file(review_path) != sources["review_file_sha256"]:
            raise OnsetEvidenceError("approved review source file is stale")
        candidate = load_candidate_report(
            candidate_path,
            project_root=project_root,
        )
        review, review_candidate = load_review_decision(
            review_path,
            project_root=project_root,
            require_complete=True,
        )
        if candidate["candidate_sha256"] != sources["candidate_sha256"]:
            raise OnsetEvidenceError("approved candidate self hash is stale")
        if review["review_sha256"] != sources["review_sha256"]:
            raise OnsetEvidenceError("approved review self hash is stale")
        if review_candidate["candidate_sha256"] != candidate["candidate_sha256"]:
            raise OnsetEvidenceError("approved review and candidate do not match")
        if candidate["instrument"] != instrument:
            raise OnsetEvidenceError(
                "approved source candidate names another instrument"
            )
        expected_proof = _build_portable_proof(candidate, review)
        if approved["portable_proof"] != expected_proof:
            raise OnsetEvidenceError(
                "portable proof differs from the full candidate/review chain"
            )
        recomputed = _derive_approved_articulations(
            candidate,
            review,
            max_spread_ms=threshold_ms,
        )
        if approved["articulations"] != recomputed:
            raise OnsetEvidenceError(
                "approved articulation values do not match the bound manual review"
            )
    return approved


def load_approved_onset_evidence(
    path: str | Path,
    *,
    project_root: str | Path,
    manifest_path: str | Path | None = None,
    verify_source_chain: bool = True,
) -> dict[str, Any]:
    """Strict conductor loader.

    This function intentionally raises for a missing file as well as for an
    invalid one.  ``verify_source_chain=True`` is the audit/worktree mode and
    replays the complete candidate -> review -> approval chain.  A lightweight
    release may set it to false: source paths and hashes remain bound by the
    approved document, while only the approved self hash, policy, review-lead
    attestation, current manifest/runtime fingerprint and embedded portable
    manual proof are required locally.  Articulation medians and spreads are
    always recomputed from that proof, even when the bulky probe WAVs are not
    shipped.
    """

    root = Path(project_root).resolve()
    source = Path(path)
    if not source.is_absolute():
        source = root / source
    source = source.resolve()
    _project_relative_label(root, source)
    return validate_approved_onset_evidence(
        read_json_strict(source),
        project_root=root,
        manifest_path=manifest_path,
        verify_source_chain=verify_source_chain,
    )


__all__ = [
    "ANCHOR",
    "APPROVABLE_VARIANT_COVERAGE",
    "APPROVED_SCHEMA",
    "CANDIDATE_SCHEMA",
    "CONTEXT",
    "DEFAULT_ARTICULATION_SENTINEL",
    "OnsetEvidenceError",
    "REVIEW_SCHEMA",
    "VARIANT_COVERAGE",
    "canonical_json_bytes",
    "canonical_sha256",
    "compute_runtime_fingerprint",
    "create_review_draft",
    "finalize_review",
    "load_approved_onset_evidence",
    "load_candidate_report",
    "load_review_decision",
    "promote_review",
    "read_json_strict",
    "record_review_decision",
    "resolve_project_path",
    "sha256_file",
    "validate_approved_onset_evidence",
    "validate_candidate_report",
    "validate_review_decision",
    "validate_runtime_fingerprint",
    "write_json_atomic",
]
