"""Prove the six substantive local factories byte-exact on built-in dispatch.

The frozen v0.8 manifest, implementation and (for VSCO2 viola) mapping are
read directly from Git.  Each tracked audition event document is rendered
through that historical local route and through the proposed current built-in
route.  The default command is read-only.  After the manifests have separately
been switched, ``--write`` transactionally rebinds only the seven tracked
audition reports.  Neither mode reads or writes ``output``.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
for entry in (ROOT, TOOLS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import reverify_builtin_dispatch_migration as legacy  # noqa: E402

from tianlai.instrument import (  # noqa: E402
    Instrument,
    _bind_factory_provenance,
    create_instrument,
)


BASELINE_REVISION = "4b3e3aa5b19a587ccc0e766212165a43a739ee12"
MIGRATED_AT = "2026-08-11"
CATALOG = ROOT / "乐器"
MANIFEST_NAME = "乐器.json"
IMPLEMENTATION_NAME = "乐器.py"
REPORT_NAME = "试听核验.json"
EXPRESSIVE_REPORT_NAME = "表现力试听核验.json"
VIOLA_MAPPING_NAME = "VSCO2中提琴映射.py"
VIOLA_MAPPING_MODULE = "VSCO2中提琴映射"
TARGETS = (
    "世界乐器/编钟",
    "管弦乐/弦乐组/中提琴",
    "管弦乐/弦乐组/大提琴",
    "管弦乐/弦乐组/小提琴",
    "管弦乐/木管组/长笛",
    "键盘乐器/钢琴",
)


class SubstantiveDispatchMigrationError(RuntimeError):
    """The proposed built-in route is incomplete or not byte-exact."""


def _git_bytes(relative_path: Path) -> bytes:
    previous = legacy.BASELINE_REVISION
    legacy.BASELINE_REVISION = BASELINE_REVISION
    try:
        return legacy._git_bytes(relative_path)
    finally:
        legacy.BASELINE_REVISION = previous


def _git_object(relative_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_git_bytes(relative_path).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise SubstantiveDispatchMigrationError(
            f"baseline JSON is invalid: {relative_path}"
        ) from error
    if not isinstance(value, dict):
        raise SubstantiveDispatchMigrationError(
            f"baseline JSON root is not an object: {relative_path}"
        )
    return value


def _current_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SubstantiveDispatchMigrationError(
            f"current JSON is invalid: {path}"
        ) from error
    if not isinstance(value, dict):
        raise SubstantiveDispatchMigrationError(
            f"current JSON root is not an object: {path}"
        )
    return value


def _manifest_pair(manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    relative = manifest_path.relative_to(ROOT)
    old = _git_object(relative)
    if old.get("implementation") != IMPLEMENTATION_NAME:
        raise SubstantiveDispatchMigrationError(
            f"baseline manifest has no expected local factory: {relative}"
        )
    new = dict(old)
    del new["implementation"]
    current = _current_object(manifest_path)
    if current not in (old, new):
        raise SubstantiveDispatchMigrationError(
            f"current manifest changed beyond the dispatch field: {relative}"
        )
    return old, new


def _exec_baseline_module(
    relative_path: Path,
    *,
    module_name: str,
    file_path: Path,
) -> ModuleType:
    try:
        source = _git_bytes(relative_path).decode("utf-8-sig")
    except UnicodeError as error:
        raise SubstantiveDispatchMigrationError(
            f"baseline Python is not UTF-8: {relative_path}"
        ) from error
    module = ModuleType(module_name)
    module.__file__ = str(file_path)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _old_instrument(
    manifest_path: Path,
    manifest: dict[str, Any],
    sample_rate: int,
) -> Instrument:
    directory = manifest_path.parent
    relative_implementation = (directory / IMPLEMENTATION_NAME).relative_to(ROOT)
    mapping_previous = sys.modules.get(VIOLA_MAPPING_MODULE)
    mapping_loaded = False
    if directory.relative_to(CATALOG).as_posix() == "管弦乐/弦乐组/中提琴":
        _exec_baseline_module(
            (directory / VIOLA_MAPPING_NAME).relative_to(ROOT),
            module_name=VIOLA_MAPPING_MODULE,
            file_path=directory / VIOLA_MAPPING_NAME,
        )
        mapping_loaded = True

    module_name = "tianlai_substantive_dispatch_old_" + hashlib.sha256(
        relative_implementation.as_posix().encode("utf-8")
    ).hexdigest()[:16]
    module: ModuleType | None = None
    instrument: object | None = None
    try:
        module = _exec_baseline_module(
            relative_implementation,
            module_name=module_name,
            file_path=directory / IMPLEMENTATION_NAME,
        )
        factory = getattr(module, "create", None)
        if not callable(factory):
            raise SubstantiveDispatchMigrationError(
                f"baseline implementation has no create(): {relative_implementation}"
            )
        instrument = factory(
            manifest=manifest,
            sample_rate=sample_rate,
            base_directory=str(directory),
        )
    finally:
        if module is not None:
            sys.modules.pop(module_name, None)
        if mapping_loaded:
            if mapping_previous is None:
                sys.modules.pop(VIOLA_MAPPING_MODULE, None)
            else:
                sys.modules[VIOLA_MAPPING_MODULE] = mapping_previous
    if not isinstance(instrument, Instrument):
        raise SubstantiveDispatchMigrationError(
            f"baseline factory returned a non-Instrument: {relative_implementation}"
        )
    return _bind_factory_provenance(
        instrument,
        manifest,
        sample_rate=sample_rate,
        factory_route="local_implementation_factory",
    )


def _new_instrument(
    manifest_path: Path,
    manifest: dict[str, Any],
    sample_rate: int,
) -> Instrument:
    instrument = create_instrument(
        manifest,
        sample_rate,
        base_directory=str(manifest_path.parent),
    )
    provenance = getattr(instrument, "_tianlai_factory_provenance", None)
    if not isinstance(provenance, dict) or provenance.get("factory_route") != (
        "builtin_manifest_dispatch_no_implementation"
    ):
        raise SubstantiveDispatchMigrationError(
            f"proposed route is not built-in dispatch: {manifest_path}"
        )
    return instrument


def _events(report_path: Path) -> dict[str, Any]:
    report = _current_object(report_path)
    events_path = legacy._project_file(report.get("events"))
    expected = report.get("events_canonical_sha256")
    actual = legacy.canonical_json_file_sha256(events_path)
    if expected != actual:
        raise SubstantiveDispatchMigrationError(
            f"frozen events changed: {report_path}"
        )
    return _current_object(events_path)


def _migration_record(previous_manifest_sha256: str) -> dict[str, Any]:
    return {
        "status": "implementation_relocated_to_builtin_no_audio_change",
        "migrated_at": MIGRATED_AT,
        "previous_manifest_canonical_sha256": previous_manifest_sha256,
        "changed_fields": ["implementation"],
        "audio_rerendered": False,
        "baseline_revision": BASELINE_REVISION,
        "verified_by": "tools/reverify_substantive_builtin_dispatch_migration.py",
        "byte_exact_fields": [
            "float64_stream",
            "float32_stream",
            "pcm24_wav",
            "frame_count",
            "peak_active_voices",
        ],
        "reason": (
            "The implementation was relocated into the trusted Tianlai source "
            "tree and the manifest now selects built-in dispatch; the directory "
            "compatibility wrapper remains available and current-source "
            "frozen-event A/B rendering was byte-exact."
        ),
    }


def _report_state(
    report_path: Path,
    *,
    previous_manifest_sha256: str,
    current_manifest_sha256: str,
) -> tuple[dict[str, Any], bool]:
    report = _current_object(report_path)
    bound = report.get("manifest_canonical_sha256")
    if bound == previous_manifest_sha256:
        if "factory_dispatch_migration" in report:
            raise SubstantiveDispatchMigrationError(
                f"stale report already claims a dispatch migration: {report_path}"
            )
        return report, True
    if bound == current_manifest_sha256:
        expected = _migration_record(previous_manifest_sha256)
        if report.get("factory_dispatch_migration") != expected:
            raise SubstantiveDispatchMigrationError(
                f"current report has invalid migration evidence: {report_path}"
            )
        return report, False
    raise SubstantiveDispatchMigrationError(
        f"report is bound to neither old nor new manifest: {report_path}"
    )


def _updated_report(
    report: dict[str, Any],
    *,
    previous_manifest_sha256: str,
    current_manifest_sha256: str,
) -> dict[str, Any]:
    updated: dict[str, Any] = {}
    inserted = False
    for key, value in report.items():
        if key == "manifest_canonical_sha256":
            updated[key] = current_manifest_sha256
            updated["factory_dispatch_migration"] = _migration_record(
                previous_manifest_sha256
            )
            inserted = True
        elif key != "factory_dispatch_migration":
            updated[key] = value
    if not inserted:
        raise SubstantiveDispatchMigrationError(
            "audition report has no manifest_canonical_sha256"
        )
    return updated


def _verify_route(
    manifest_path: Path,
    old_manifest: dict[str, Any],
    new_manifest: dict[str, Any],
    report_path: Path,
    item_root: Path,
) -> dict[str, Any]:
    events = _events(report_path)
    document_a = legacy.parse_performance_document(events)
    document_b = legacy.parse_performance_document(events)
    old = _old_instrument(manifest_path, old_manifest, document_a.sample_rate)
    old_name = type(old).__name__
    try:
        result_a = legacy._capture_render(
            old,
            document_a,
            raw_path=item_root / "old.float64.raw",
            wav_path=item_root / "old.wav",
        )
    finally:
        legacy._close_instrument(old)
    del old
    gc.collect()

    new = _new_instrument(manifest_path, new_manifest, document_b.sample_rate)
    try:
        if type(new).__name__ != old_name:
            raise SubstantiveDispatchMigrationError(
                f"factory class name changed: {old_name} -> {type(new).__name__}"
            )
        result_b = legacy._capture_render(
            new,
            document_b,
            raw_path=item_root / "new.float64.raw",
            wav_path=item_root / "new.wav",
            compare_raw_path=item_root / "old.float64.raw",
        )
    finally:
        legacy._close_instrument(new)
    del new
    gc.collect()

    fields = (
        "frame_count",
        "peak_active_voices",
        "float64_sha256",
        "float32_sha256",
        "pcm24_wav_sha256",
    )
    mismatches = [name for name in fields if result_a[name] != result_b[name]]
    if result_b["first_differing_frame"] is not None:
        mismatches.append("float64_stream_bytes")
    if mismatches:
        raise SubstantiveDispatchMigrationError(
            f"old/new audio differs for {manifest_path.parent}; "
            f"fields={sorted(set(mismatches))}; "
            f"first_frame={result_b['first_differing_frame']}"
        )
    return result_b


def verify(*, write: bool = False) -> tuple[int, int, int]:
    cases: list[tuple[Path, Path]] = []
    for relative in TARGETS:
        directory = CATALOG / Path(relative)
        manifest_path = directory / MANIFEST_NAME
        if not manifest_path.is_file() or not (directory / IMPLEMENTATION_NAME).is_file():
            raise SubstantiveDispatchMigrationError(
                f"migration target or compatibility wrapper is missing: {relative}"
            )
        cases.append((manifest_path, directory / REPORT_NAME))
        expressive = directory / EXPRESSIVE_REPORT_NAME
        if expressive.is_file():
            cases.append((manifest_path, expressive))
    if len(cases) != 7:
        raise SubstantiveDispatchMigrationError(
            f"expected 7 frozen audition routes, got {len(cases)}"
        )

    with tempfile.TemporaryDirectory(
        prefix="tianlai-substantive-dispatch-ab-"
    ) as temporary_directory:
        transaction = Path(temporary_directory)
        staged: dict[Path, Path] = {}
        stale_reports = 0
        for index, (manifest_path, report_path) in enumerate(cases, start=1):
            old_manifest, new_manifest = _manifest_pair(manifest_path)
            current_manifest = _current_object(manifest_path)
            if write and current_manifest != new_manifest:
                raise SubstantiveDispatchMigrationError(
                    f"--write requires the built-in manifest state: {manifest_path}"
                )
            previous_hash = legacy.canonical_json_sha256(old_manifest)
            current_hash = legacy.canonical_json_sha256(new_manifest)
            report, stale = _report_state(
                report_path,
                previous_manifest_sha256=previous_hash,
                current_manifest_sha256=current_hash,
            )
            stale_reports += int(stale)
            label = manifest_path.parent.relative_to(CATALOG).as_posix()
            print(f"verify {index:02d}/{len(cases)} {label}/{report_path.name}", flush=True)
            item_root = transaction / f"render-{index:02d}"
            try:
                result = _verify_route(
                    manifest_path,
                    old_manifest,
                    new_manifest,
                    report_path,
                    item_root,
                )
            finally:
                shutil.rmtree(item_root, ignore_errors=True)
            print(
                "  byte-exact "
                f"frames={result['frame_count']} "
                f"peak={result['peak_active_voices']} "
                f"f64={result['float64_sha256'][:12]} "
                f"f32={result['float32_sha256'][:12]} "
                f"pcm24={result['pcm24_wav_sha256'][:12]}",
                flush=True,
            )
            if write and stale:
                staged_path = transaction / "reports" / f"{index:02d}.json"
                legacy._write_json(
                    staged_path,
                    _updated_report(
                        report,
                        previous_manifest_sha256=previous_hash,
                        current_manifest_sha256=current_hash,
                    ),
                )
                staged[report_path] = staged_path
        if write and staged:
            legacy._commit_reports(staged, transaction)
    return len(TARGETS), len(cases), stale_reports


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()
    targets, routes, stale = verify(write=arguments.write)
    print(
        f"checked_targets={targets} checked_routes={routes} "
        f"stale_reports={stale} write={arguments.write}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
