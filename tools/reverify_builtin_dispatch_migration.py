"""Verify and record byte-exact migration to built-in manifest dispatch.

The migration deliberately does not regenerate listening evidence.  For each
affected instrument it reconstructs the v0.8 manifest and compatibility
wrapper from Git,
then renders the same frozen events through the old and new factory routes
with the *current* renderer.  Only when the complete float64 stream, float32
stem stream and PCM-24 WAV are byte-identical for every target does ``--write``
rebind the tracked report to the new manifest identity.

No path under ``output`` is read or written.  Render products live only in a
system temporary transaction and are removed before the command returns.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import ModuleType
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.audio import write_wav_pcm24_blocks  # noqa: E402
from tianlai.canonical_json import (  # noqa: E402
    canonical_json_file_sha256,
    canonical_json_sha256,
)
from tianlai.events import (  # noqa: E402
    PerformanceDocument,
    parse_performance_document,
)
from tianlai.instrument import (  # noqa: E402
    Instrument,
    _bind_factory_provenance,
    create_instrument,
)
from tianlai.renderer import render_document  # noqa: E402


CATALOG = ROOT / "乐器"
MANIFEST_NAME = "乐器.json"
REPORT_NAME = "试听核验.json"
LEGACY_IMPLEMENTATION = "乐器.py"
BASELINE_REVISION = "c190472b81b6b4f42def87a57c9fc3fb8fc5d0b9"
EXPECTED_TARGET_COUNT = 41
EXPECTED_REPORT_COUNT = 40
MIGRATED_AT = "2026-08-11"
_CHUNK_FRAMES = 65_536
_TARGET_TYPES = frozenset(
    {
        "mtg_solo_sax",
        "oscillator",
        "procedural_sfx",
        "vpo_brass",
        "vpo_celesta",
        "vpo_cowbell",
        "vpo_harp",
        "vpo_mixed_choir",
        "vpo_orchestral_hit",
        "vpo_percussion",
        "vpo_solo_string",
        "vpo_string_section",
        "vpo_woodwind",
    }
)
_TIMPANI = "管弦乐/打击乐组/定音鼓"
_REFERENCE_OSCILLATOR = "测试工具/参考振荡器"


class FactoryDispatchMigrationError(RuntimeError):
    """A wrapper migration cannot be proved or recorded safely."""


def _load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FactoryDispatchMigrationError(
            f"cannot read JSON object {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FactoryDispatchMigrationError(f"JSON root must be an object: {path}")
    return value


def _git_bytes(relative_path: Path) -> bytes:
    label = relative_path.as_posix()
    process = subprocess.run(
        ["git", "show", f"{BASELINE_REVISION}:{label}"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace").strip()
        raise FactoryDispatchMigrationError(
            f"cannot read {label} from {BASELINE_REVISION}: {detail}"
        )
    return process.stdout


def _git_object(relative_path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_git_bytes(relative_path).decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise FactoryDispatchMigrationError(
            f"baseline JSON is invalid: {relative_path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise FactoryDispatchMigrationError(
            f"baseline JSON root must be an object: {relative_path}"
        )
    return value


def _target_manifests() -> tuple[Path, ...]:
    targets: list[Path] = []
    for manifest_path in sorted(CATALOG.rglob(MANIFEST_NAME)):
        manifest = _load_object(manifest_path)
        relative = manifest_path.parent.relative_to(CATALOG).as_posix()
        if manifest.get("type") not in _TARGET_TYPES and relative != _TIMPANI:
            continue
        if "implementation" in manifest:
            raise FactoryDispatchMigrationError(
                f"target still names a local implementation: {manifest_path}"
            )
        wrapper_path = manifest_path.parent / LEGACY_IMPLEMENTATION
        if not wrapper_path.is_file():
            raise FactoryDispatchMigrationError(
                f"compatibility wrapper is missing: {manifest_path.parent}"
            )
        relative_wrapper = wrapper_path.relative_to(ROOT)
        if wrapper_path.read_bytes() != _git_bytes(relative_wrapper):
            raise FactoryDispatchMigrationError(
                f"compatibility wrapper differs from v0.8: {relative_wrapper}"
            )
        targets.append(manifest_path)
    if len(targets) != EXPECTED_TARGET_COUNT:
        raise FactoryDispatchMigrationError(
            f"expected {EXPECTED_TARGET_COUNT} migrated manifests, got {len(targets)}"
        )
    return tuple(targets)


def _baseline_pair(manifest_path: Path) -> tuple[dict[str, Any], str, str]:
    relative_manifest = manifest_path.relative_to(ROOT)
    old_manifest = _git_object(relative_manifest)
    current_manifest = _load_object(manifest_path)
    implementation = old_manifest.get("implementation")
    if implementation != LEGACY_IMPLEMENTATION:
        raise FactoryDispatchMigrationError(
            f"baseline manifest does not name the expected wrapper: {relative_manifest}"
        )
    expected_current = dict(old_manifest)
    del expected_current["implementation"]
    if current_manifest != expected_current:
        raise FactoryDispatchMigrationError(
            "manifest migration changed fields other than implementation: "
            f"{relative_manifest}"
        )
    return (
        old_manifest,
        canonical_json_sha256(old_manifest),
        canonical_json_file_sha256(manifest_path),
    )


def _project_file(label: object) -> Path:
    if not isinstance(label, str) or not label.strip():
        raise FactoryDispatchMigrationError("audition report has no events path")
    candidate = (ROOT / label).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as error:
        raise FactoryDispatchMigrationError(
            f"events path escapes the project: {label}"
        ) from error
    if not candidate.is_file():
        raise FactoryDispatchMigrationError(f"events file is missing: {label}")
    return candidate


def _synthetic_reference_events() -> dict[str, Any]:
    return {
        "sample_rate": 8_000,
        "channels": 2,
        "duration_seconds": 0.5,
        "tail_seconds": 0.1,
        "events": [
            {
                "time": 0.0,
                "type": "note_on",
                "note_id": 1,
                "midi_note": 69,
                "velocity": 0.72,
            },
            {
                "time": 0.08,
                "type": "control",
                "name": "sustain_pedal",
                "value": 1.0,
            },
            {
                "time": 0.12,
                "type": "note_on",
                "note_id": 2,
                "midi_note": 76,
                "velocity": 0.61,
            },
            {
                "time": 0.22,
                "type": "note_off",
                "note_id": 1,
                "release_velocity": 0.35,
            },
            {
                "time": 0.27,
                "type": "note_off",
                "note_id": 2,
                "release_velocity": 0.8,
            },
            {
                "time": 0.31,
                "type": "control",
                "name": "sustain_pedal",
                "value": 0.0,
            },
        ],
    }


def _events_for_target(
    manifest_path: Path,
    report: dict[str, Any] | None,
) -> dict[str, Any]:
    relative = manifest_path.parent.relative_to(CATALOG).as_posix()
    if report is None:
        if relative != _REFERENCE_OSCILLATOR:
            raise FactoryDispatchMigrationError(
                f"only the reference oscillator may omit formal events: {relative}"
            )
        return _synthetic_reference_events()
    events_path = _project_file(report.get("events"))
    expected_hash = report.get("events_canonical_sha256")
    actual_hash = canonical_json_file_sha256(events_path)
    if expected_hash != actual_hash:
        raise FactoryDispatchMigrationError(
            f"frozen event identity is stale for {relative}"
        )
    return _load_object(events_path)


def _old_instrument(
    manifest_path: Path,
    old_manifest: dict[str, Any],
    sample_rate: int,
) -> Instrument:
    relative_wrapper = (
        manifest_path.parent / LEGACY_IMPLEMENTATION
    ).relative_to(ROOT)
    try:
        source = _git_bytes(relative_wrapper).decode("utf-8-sig")
    except UnicodeError as error:
        raise FactoryDispatchMigrationError(
            f"baseline wrapper is not UTF-8: {relative_wrapper}"
        ) from error
    module_name = "tianlai_dispatch_migration_" + hashlib.sha256(
        relative_wrapper.as_posix().encode("utf-8")
    ).hexdigest()[:16]
    module = ModuleType(module_name)
    module.__file__ = str(manifest_path.parent / LEGACY_IMPLEMENTATION)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        factory = getattr(module, "create", None)
        if not callable(factory):
            raise FactoryDispatchMigrationError(
                f"baseline wrapper has no create(): {relative_wrapper}"
            )
        instrument = factory(
            manifest=old_manifest,
            sample_rate=sample_rate,
            base_directory=str(manifest_path.parent),
        )
    finally:
        sys.modules.pop(module_name, None)
    if not isinstance(instrument, Instrument):
        raise FactoryDispatchMigrationError(
            f"baseline wrapper returned a non-Instrument: {relative_wrapper}"
        )
    return _bind_factory_provenance(
        instrument,
        old_manifest,
        sample_rate=sample_rate,
        factory_route="local_implementation_factory",
    )


def _new_instrument(
    manifest_path: Path,
    sample_rate: int,
) -> Instrument:
    manifest = _load_object(manifest_path)
    instrument = create_instrument(
        manifest,
        sample_rate,
        base_directory=str(manifest_path.parent),
    )
    provenance = instrument._tianlai_factory_provenance
    if not isinstance(provenance, dict) or provenance.get("factory_route") != (
        "builtin_manifest_dispatch_no_implementation"
    ):
        raise FactoryDispatchMigrationError(
            f"new factory did not use builtin dispatch: {manifest_path}"
        )
    return instrument


def _close_instrument(instrument: Instrument) -> None:
    close = getattr(instrument, "close", None)
    if callable(close):
        close()


def _first_differing_frame(left: bytes, right: bytes, frame_offset: int) -> int:
    limit = min(len(left), len(right))
    for byte_index in range(limit):
        if left[byte_index] != right[byte_index]:
            return frame_offset + byte_index // 16
    return frame_offset + limit // 16


def _capture_render(
    instrument: Instrument,
    document: PerformanceDocument,
    *,
    raw_path: Path,
    wav_path: Path,
    compare_raw_path: Path | None = None,
) -> dict[str, Any]:
    import numpy as np

    frames, peak = render_document(instrument, document)
    iterator = iter(frames)
    float64_hash = hashlib.sha256()
    float32_hash = hashlib.sha256()
    first_difference: int | None = None
    frame_offset = 0
    comparison = (
        compare_raw_path.open("rb") if compare_raw_path is not None else None
    )
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    def blocks() -> Iterator[Any]:
        nonlocal first_difference, frame_offset
        with raw_path.open("wb") as raw:
            while frame_offset < document.total_samples:
                requested = min(
                    _CHUNK_FRAMES,
                    document.total_samples - frame_offset,
                )
                try:
                    flat = np.fromiter(
                        (
                            sample
                            for frame in islice(iterator, requested)
                            for sample in frame
                        ),
                        dtype=np.float64,
                        count=requested * 2,
                    )
                except ValueError as error:
                    raise FactoryDispatchMigrationError(
                        "renderer produced fewer frames than declared"
                    ) from error
                block = flat.reshape(requested, 2)
                float64_bytes = block.astype("<f8", copy=False).tobytes(order="C")
                float32_bytes = block.astype("<f4").tobytes(order="C")
                raw.write(float64_bytes)
                float64_hash.update(float64_bytes)
                float32_hash.update(float32_bytes)
                if comparison is not None:
                    expected = comparison.read(len(float64_bytes))
                    if expected != float64_bytes and first_difference is None:
                        first_difference = _first_differing_frame(
                            expected,
                            float64_bytes,
                            frame_offset,
                        )
                frame_offset += requested
                yield block

    try:
        frame_count = write_wav_pcm24_blocks(
            wav_path,
            blocks(),
            document.sample_rate,
        )
        sentinel = object()
        if next(iterator, sentinel) is not sentinel:
            raise FactoryDispatchMigrationError(
                "renderer produced more frames than declared"
            )
        if comparison is not None and comparison.read(1):
            if first_difference is None:
                first_difference = document.total_samples
    finally:
        if comparison is not None:
            comparison.close()
    return {
        "frame_count": frame_count,
        "peak_active_voices": peak[0],
        "float64_sha256": float64_hash.hexdigest(),
        "float32_sha256": float32_hash.hexdigest(),
        "pcm24_wav_sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
        "first_differing_frame": first_difference,
    }


def _verify_audio_route(
    manifest_path: Path,
    old_manifest: dict[str, Any],
    events: dict[str, Any],
    item_root: Path,
) -> dict[str, Any]:
    document_a = parse_performance_document(events)
    document_b = parse_performance_document(events)
    old = _old_instrument(manifest_path, old_manifest, document_a.sample_rate)
    old_type = type(old)
    try:
        result_a = _capture_render(
            old,
            document_a,
            raw_path=item_root / "old.float64.raw",
            wav_path=item_root / "old.wav",
        )
    finally:
        _close_instrument(old)
    del old
    gc.collect()

    new = _new_instrument(manifest_path, document_b.sample_rate)
    try:
        if type(new) is not old_type:
            raise FactoryDispatchMigrationError(
                "factory class changed for "
                f"{manifest_path.parent.relative_to(CATALOG).as_posix()}: "
                f"{old_type.__module__}.{old_type.__qualname__} -> "
                f"{type(new).__module__}.{type(new).__qualname__}"
            )
        result_b = _capture_render(
            new,
            document_b,
            raw_path=item_root / "new.float64.raw",
            wav_path=item_root / "new.wav",
            compare_raw_path=item_root / "old.float64.raw",
        )
    finally:
        _close_instrument(new)
    del new
    gc.collect()

    compared_fields = (
        "frame_count",
        "peak_active_voices",
        "float64_sha256",
        "float32_sha256",
        "pcm24_wav_sha256",
    )
    mismatches = [
        field for field in compared_fields if result_a[field] != result_b[field]
    ]
    if result_b["first_differing_frame"] is not None:
        mismatches.append("float64_stream_bytes")
    if mismatches:
        relative = manifest_path.parent.relative_to(CATALOG).as_posix()
        raise FactoryDispatchMigrationError(
            f"old/new route audio differs for {relative}; "
            f"fields={sorted(set(mismatches))}; "
            f"first_differing_frame={result_b['first_differing_frame']}"
        )
    return result_b


def _migration_record(previous_manifest_sha256: str) -> dict[str, Any]:
    return {
        "status": "factory_route_only_no_audio_change",
        "migrated_at": MIGRATED_AT,
        "previous_manifest_canonical_sha256": previous_manifest_sha256,
        "changed_fields": ["implementation"],
        "audio_rerendered": False,
        "reason": (
            "Only the redundant implementation field and factory route changed; "
            "the compatibility wrapper remains available and current-source "
            "frozen-event A/B rendering was byte-exact."
        ),
    }


def _validate_migration_record(
    record: object,
    *,
    previous_manifest_sha256: str,
    report_path: Path,
) -> None:
    expected = _migration_record(previous_manifest_sha256)
    if record != expected:
        raise FactoryDispatchMigrationError(
            f"report has invalid factory dispatch migration: {report_path}"
        )


def _updated_report(
    report: dict[str, Any],
    *,
    current_manifest_sha256: str,
    previous_manifest_sha256: str,
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
        raise FactoryDispatchMigrationError(
            "audition report has no manifest_canonical_sha256"
        )
    return updated


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _prepare_same_directory_copy(
    source: Path,
    destination: Path,
    *,
    purpose: str,
) -> Path:
    payload = source.read_bytes()
    expected_sha256 = hashlib.sha256(payload).hexdigest()
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-{purpose}-",
        suffix=".json.tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        shutil.copystat(destination, temporary_path)
        actual_sha256 = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise FactoryDispatchMigrationError(
                f"prepared {purpose} copy failed verification: {destination}"
            )
        return temporary_path
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def _commit_reports(staged: dict[Path, Path], transaction: Path) -> None:
    del transaction
    prepared: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    installed: list[Path] = []
    preserved_backups: set[Path] = set()
    try:
        for destination in sorted(staged):
            prepared[destination] = _prepare_same_directory_copy(
                staged[destination],
                destination,
                purpose="prepared",
            )
            backups[destination] = _prepare_same_directory_copy(
                destination,
                destination,
                purpose="backup",
            )
        if len(prepared) != len(staged) or len(backups) != len(staged):
            raise FactoryDispatchMigrationError(
                "report transaction preparation is incomplete"
            )
        for destination in sorted(staged):
            installed.append(destination)
            os.replace(prepared[destination], destination)
    except BaseException as error:
        rollback_errors: list[str] = []
        for destination in reversed(installed):
            backup = backups[destination]
            try:
                os.replace(backup, destination)
            except OSError as rollback_error:
                preserved_backups.add(backup)
                rollback_errors.append(
                    f"destination={destination}, backup={backup}: {rollback_error}"
                )
        if rollback_errors:
            raise FactoryDispatchMigrationError(
                "report commit failed and rollback was incomplete: "
                + "; ".join(rollback_errors)
            ) from error
        raise
    finally:
        for temporary_path in prepared.values():
            temporary_path.unlink(missing_ok=True)
        for temporary_path in backups.values():
            if temporary_path not in preserved_backups:
                temporary_path.unlink(missing_ok=True)


def _report_state(
    manifest_path: Path,
    *,
    previous_hash: str,
    current_hash: str,
) -> tuple[dict[str, Any] | None, bool]:
    report_path = manifest_path.with_name(REPORT_NAME)
    if not report_path.is_file():
        return None, False
    report = _load_object(report_path)
    bound_hash = report.get("manifest_canonical_sha256")
    if bound_hash == previous_hash:
        if "factory_dispatch_migration" in report:
            raise FactoryDispatchMigrationError(
                f"stale report already claims a dispatch migration: {report_path}"
            )
        return report, True
    if bound_hash == current_hash:
        _validate_migration_record(
            report.get("factory_dispatch_migration"),
            previous_manifest_sha256=previous_hash,
            report_path=report_path,
        )
        return report, False
    raise FactoryDispatchMigrationError(
        f"report is bound to neither old nor current manifest: {report_path}"
    )


def verify_catalogue(*, write: bool) -> tuple[int, int, int]:
    targets = _target_manifests()
    prepared: list[
        tuple[Path, dict[str, Any], str, str, dict[str, Any] | None, bool]
    ] = []
    report_count = 0
    stale_count = 0
    for manifest_path in targets:
        old_manifest, previous_hash, current_hash = _baseline_pair(manifest_path)
        report, stale = _report_state(
            manifest_path,
            previous_hash=previous_hash,
            current_hash=current_hash,
        )
        if report is not None:
            report_count += 1
        if stale:
            stale_count += 1
        prepared.append(
            (
                manifest_path,
                old_manifest,
                previous_hash,
                current_hash,
                report,
                stale,
            )
        )
    if report_count != EXPECTED_REPORT_COUNT:
        raise FactoryDispatchMigrationError(
            f"expected {EXPECTED_REPORT_COUNT} audition reports, got {report_count}"
        )
    if not write or stale_count == 0:
        return len(targets), report_count, stale_count

    with tempfile.TemporaryDirectory(
        prefix="tianlai-builtin-dispatch-evidence-"
    ) as temporary_directory:
        transaction = Path(temporary_directory)
        staging = transaction / "staging"
        staging.mkdir()
        staged: dict[Path, Path] = {}
        for index, (
            manifest_path,
            old_manifest,
            previous_hash,
            current_hash,
            report,
            stale,
        ) in enumerate(prepared, start=1):
            relative = manifest_path.parent.relative_to(CATALOG).as_posix()
            print(
                f"verify {index:02d}/{len(prepared)} {relative}",
                flush=True,
            )
            events = _events_for_target(manifest_path, report)
            item_root = transaction / f"render-{index:04d}"
            try:
                result = _verify_audio_route(
                    manifest_path,
                    old_manifest,
                    events,
                    item_root,
                )
            finally:
                shutil.rmtree(item_root, ignore_errors=True)
            print(
                "  byte-exact "
                f"frames={result['frame_count']} "
                f"peak_voices={result['peak_active_voices']} "
                f"f64={result['float64_sha256'][:12]} "
                f"f32={result['float32_sha256'][:12]} "
                f"pcm24={result['pcm24_wav_sha256'][:12]}",
                flush=True,
            )
            if stale:
                if report is None:
                    raise FactoryDispatchMigrationError(
                        f"internal stale report state is missing: {relative}"
                    )
                report_path = manifest_path.with_name(REPORT_NAME)
                staged_path = staging / f"{index:04d}.json"
                _write_json(
                    staged_path,
                    _updated_report(
                        report,
                        current_manifest_sha256=current_hash,
                        previous_manifest_sha256=previous_hash,
                    ),
                )
                staged[report_path] = staged_path
        if len(staged) != stale_count:
            raise FactoryDispatchMigrationError(
                "staged report count does not match stale report count"
            )
        _commit_reports(staged, transaction)
    return len(targets), report_count, stale_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()
    checked, reports, stale = verify_catalogue(write=arguments.write)
    action = "updated" if arguments.write else "stale"
    print(f"checked={checked} reports={reports} {action}={stale}")
    return 0 if arguments.write or stale == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
