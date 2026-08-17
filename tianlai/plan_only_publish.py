"""Private transactional publication for CLI plan-only documents.

A fresh output directory is published by one no-replace directory rename.
Existing plan-only directories retain unrelated entries and receive handled-
failure rollback across the fixed JSON paths, including the optional
realization source when supplied.  This module deliberately does not claim
crash atomicity across those filesystem entries.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import uuid
import warnings

from .atomic_publish import _pretty_json_bytes, _rename_noreplace
from .plain_file import PlainFileIdentity, revalidate_plain_file, sha256_plain_file
from .render_lock import (
    PlainDirectoryIdentity,
    acquire_render_lock,
    capture_plain_directory,
    revalidate_plain_directory,
)

_PLAN_ONLY_DOCUMENT_NAMES = (
    "演奏计划.json",
    "渲染配置.json",
    "资源预检.json",
    "创作自检.json",
)
_PLAN_ONLY_OPTIONAL_DOCUMENT_NAMES = ("演奏实现.json",)
_PLAN_ONLY_COMMIT_NAME = _PLAN_ONLY_DOCUMENT_NAMES[0]
_PLAN_ONLY_RECEIPT_NAME = "渲染回执.json"
_PLAN_ONLY_PRIVATE_ATTEMPTS = 16

_PlanOnlyFileSnapshot = tuple[PlainFileIdentity, str]


def _same_plan_only_file_snapshot(
    left: _PlanOnlyFileSnapshot,
    right: _PlanOnlyFileSnapshot,
) -> bool:
    """Compare one file across a same-volume rename.

    POSIX is allowed to update inode ctime during a rename, so ``changed_ns``
    is deliberately excluded.  Device/inode, size, mtime and the complete byte
    digest still bind the ordinary single-link file that was captured.
    """

    left_identity, left_digest = left
    right_identity, right_digest = right
    return (
        left_identity.device == right_identity.device
        and left_identity.inode == right_identity.inode
        and left_identity.size == right_identity.size
        and left_identity.modified_ns == right_identity.modified_ns
        and left_digest == right_digest
    )


def _plan_only_file_snapshot(path: Path) -> _PlanOnlyFileSnapshot:
    identity, digest = sha256_plain_file(path)
    return identity, digest


def _optional_plan_only_file_snapshot(
    path: Path,
) -> _PlanOnlyFileSnapshot | None:
    if not os.path.lexists(path):
        return None
    return _plan_only_file_snapshot(path)


def _require_plan_only_file_snapshot(
    path: Path,
    expected: _PlanOnlyFileSnapshot,
    message: str,
) -> _PlanOnlyFileSnapshot:
    try:
        observed = _plan_only_file_snapshot(path)
    except OSError as exc:
        raise RuntimeError(message) from exc
    if not _same_plan_only_file_snapshot(expected, observed):
        raise RuntimeError(message)
    return observed


def _same_plan_only_directory(
    left: PlainDirectoryIdentity,
    right: PlainDirectoryIdentity,
) -> bool:
    return left.device == right.device and left.inode == right.inode


def _write_plan_only_stage(
    stage: Path,
    payloads: dict[str, bytes],
) -> dict[str, _PlanOnlyFileSnapshot]:
    snapshots: dict[str, _PlanOnlyFileSnapshot] = {}
    for name in payloads:
        path = stage / name
        with path.open("xb") as output:
            output.write(payloads[name])
            output.flush()
            os.fsync(output.fileno())
        snapshots[name] = _plan_only_file_snapshot(path)
    return snapshots


def _require_plan_only_stage(
    stage_identity: PlainDirectoryIdentity,
    snapshots: dict[str, _PlanOnlyFileSnapshot],
) -> None:
    stage = revalidate_plain_directory(stage_identity)
    document_names = tuple(snapshots)
    if {entry.name for entry in stage.iterdir()} != set(document_names):
        raise RuntimeError("plan-only staging directory layout changed")
    for name in document_names:
        revalidate_plain_file(snapshots[name][0])
        _require_plan_only_file_snapshot(
            stage / name,
            snapshots[name],
            f"plan-only staged document changed before publication: {name}",
        )
    revalidate_plain_directory(stage_identity)


def _private_plan_only_sibling(parent: Path, prefix: str) -> Path:
    for _ in range(_PLAN_ONLY_PRIVATE_ATTEMPTS):
        candidate = parent / f"{prefix}{uuid.uuid4().hex}"
        if not os.path.lexists(candidate):
            return candidate
    raise RuntimeError("could not reserve a private plan-only transaction path")


def _preserve_plan_only_transaction(
    path: Path,
    *,
    identity: PlainDirectoryIdentity,
    parent_identity: PlainDirectoryIdentity,
    retain_recovery: bool = False,
) -> Path | None:
    """Retire one identity-bound private directory without deleting a racer."""

    parent = revalidate_plain_directory(parent_identity)
    if not os.path.lexists(path):
        return None
    if path.parent != parent or ".plan-only-" not in path.name:
        raise RuntimeError(f"refusing to retire an unowned plan-only path: {path}")
    identity_changed = False
    try:
        current = capture_plain_directory(path)
        identity_changed = not _same_plan_only_directory(identity, current)
    except OSError:
        identity_changed = True
    preserved = _private_plan_only_sibling(
        parent,
        (
            f"{path.name}.recovery-preserved-"
            if retain_recovery
            else f"{path.name}.cleanup-preserved-"
        ),
    )
    revalidate_plain_directory(parent_identity)
    moved: PlainDirectoryIdentity | None = None
    try:
        _rename_noreplace(path, preserved)
    except BaseException:
        # A fault-injection seam or platform wrapper can report an error after
        # the native move completed.  Rebind the unpredictable destination so
        # a successfully preserved recovery still receives its exact path.
        if os.path.lexists(path):
            raise
        moved = capture_plain_directory(preserved)
    revalidate_plain_directory(parent_identity)
    try:
        if moved is None:
            moved = capture_plain_directory(preserved)
        identity_changed = identity_changed or not _same_plan_only_directory(
            identity,
            moved,
        )
    except OSError:
        identity_changed = True
    if identity_changed:
        try:
            warnings.warn(
                "plan-only transaction identity changed during cleanup; "
                f"the replacement was preserved at {preserved}",
                RuntimeWarning,
                stacklevel=3,
            )
        except BaseException:
            # Warning filters must not turn a recovery diagnostic into a
            # rollback failure or replace the original publication error.
            pass
        return preserved
    if retain_recovery:
        return preserved
    assert moved is not None
    revalidate_plain_directory(parent_identity)
    revalidate_plain_directory(moved)
    # The quarantine name is unpredictable and was rebound to the captured
    # directory immediately above.  As with atomic_publish's file cleanup,
    # mutation of that random private name in the final removal boundary is
    # outside the cooperative-writer contract; an observed identity change is
    # always retained instead of recursively removed.
    shutil.rmtree(preserved)
    return None


def _note_plan_only_cleanup_failure(
    primary_error: BaseException | None,
    path: Path,
    cleanup_error: BaseException,
) -> None:
    message = f"plan-only transaction cleanup was preserved at {path}: {cleanup_error}"
    if primary_error is not None:
        try:
            primary_error.add_note(message)
        except BaseException:
            pass
        return
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except BaseException:
        pass


def _rollback_new_plan_only_directory(
    output: Path,
    stage: Path,
    stage_identity: PlainDirectoryIdentity,
    parent_identity: PlainDirectoryIdentity,
) -> tuple[list[str], bool]:
    errors: list[str] = []
    if os.path.lexists(stage):
        try:
            current_stage = capture_plain_directory(stage)
            if _same_plan_only_directory(stage_identity, current_stage):
                # The publish rename failed before moving the owned stage.
                # Normal finally cleanup can safely retire it.
                return [], False
            # The original private name may have been occupied after the
            # directory-level publish.  Move that unknown entry aside without
            # deleting it, then use the now-vacant name to withdraw the exact
            # directory we published.
            _preserve_plan_only_transaction(
                stage,
                identity=stage_identity,
                parent_identity=parent_identity,
            )
        except BaseException as exc:
            return [f"rollback staging path was occupied: {exc}"], False
    if not os.path.lexists(output):
        return ["published staging directory disappeared before rollback"], False
    withdrawn = False
    try:
        current = capture_plain_directory(output)
        if not _same_plan_only_directory(stage_identity, current):
            return ["output was occupied by another directory during rollback"], False
        try:
            _rename_noreplace(output, stage)
        except BaseException as move_error:
            try:
                moved_after_error = capture_plain_directory(stage)
            except BaseException:
                errors.append(str(move_error))
            else:
                if (
                    _same_plan_only_directory(stage_identity, moved_after_error)
                    and not os.path.lexists(output)
                ):
                    withdrawn = True
                else:
                    errors.append(str(move_error))
        else:
            withdrawn = True
        moved = capture_plain_directory(stage)
        if not _same_plan_only_directory(stage_identity, moved):
            errors.append("published staging directory changed during rollback")
            withdrawn = False
            if not os.path.lexists(output):
                try:
                    _rename_noreplace(stage, output)
                except BaseException as restore_error:
                    try:
                        restored = capture_plain_directory(output)
                    except BaseException:
                        errors.append(
                            "restore concurrently moved output directory: "
                            f"{restore_error}"
                        )
                    else:
                        if not _same_plan_only_directory(moved, restored):
                            errors.append(
                                "concurrent output directory identity changed "
                                "while it was restored"
                            )
    except BaseException as exc:
        errors.append(str(exc))
    return errors, withdrawn


def _publish_new_plan_only_directory(
    output: Path,
    payloads: dict[str, bytes],
    *,
    parent_identity: PlainDirectoryIdentity,
) -> None:
    document_names = tuple(payloads)
    parent = revalidate_plain_directory(parent_identity)
    stage = Path(
        tempfile.mkdtemp(
            dir=parent,
            prefix=f".{output.name or 'plan'}.plan-only-stage.",
        )
    )
    stage_identity: PlainDirectoryIdentity | None = None
    primary_error: BaseException | None = None
    retain_recovery = False
    recovery_path: Path | None = None
    stage_was_published = False
    try:
        stage_identity = capture_plain_directory(stage)
        snapshots = _write_plan_only_stage(stage, payloads)
        _require_plan_only_stage(stage_identity, snapshots)
        revalidate_plain_directory(parent_identity)
        try:
            _rename_noreplace(stage, output)
        except BaseException:
            try:
                moved_after_error = capture_plain_directory(output)
                stage_was_published = _same_plan_only_directory(
                    stage_identity,
                    moved_after_error,
                )
            except BaseException:
                # The native rename may have completed even when rebinding the
                # output transiently fails.  Let the identity-checking rollback
                # distinguish an owned stage, a moved stage, and a racer.
                stage_was_published = True
            raise
        else:
            stage_was_published = True
        moved = capture_plain_directory(output)
        if not _same_plan_only_directory(stage_identity, moved):
            raise RuntimeError("plan-only staging identity changed during publication")
        if {entry.name for entry in output.iterdir()} != set(document_names):
            raise RuntimeError("published plan-only directory layout changed")
        for name in document_names:
            _require_plan_only_file_snapshot(
                output / name,
                snapshots[name],
                f"published plan-only document changed: {name}",
            )
    except BaseException as exc:
        primary_error = exc
        rollback_errors: list[str] = []
        withdrawn = False
        if stage_identity is not None and stage_was_published:
            try:
                rollback_errors, withdrawn = _rollback_new_plan_only_directory(
                    output,
                    stage,
                    stage_identity,
                    parent_identity,
                )
            except BaseException as rollback_error:
                rollback_errors.append(
                    f"rollback helper failed: {rollback_error}"
                )
        if withdrawn:
            retain_recovery = True
            try:
                recovery_path = _preserve_plan_only_transaction(
                    stage,
                    identity=stage_identity,
                    parent_identity=parent_identity,
                    retain_recovery=True,
                )
            except BaseException as preservation_error:
                rollback_errors.append(
                    f"preserve recovery directory: {preservation_error}"
                )
            if recovery_path is not None:
                try:
                    exc.add_note(
                        "failed published plan-only generation was retained "
                        f"for recovery at {recovery_path}"
                    )
                except BaseException:
                    pass
        if rollback_errors:
            retain_recovery = True
            try:
                exc.add_note(
                    "plan-only directory publication rollback was incomplete"
                )
                exc.add_note(
                    "recovery state retained at "
                    f"{recovery_path or stage}"
                )
            except BaseException:
                pass
            for detail in rollback_errors:
                try:
                    exc.add_note(f"rollback error: {detail}")
                except BaseException:
                    pass
        raise
    finally:
        if os.path.lexists(stage):
            try:
                if stage_identity is None:
                    preserved = _private_plan_only_sibling(
                        parent,
                        f"{stage.name}.cleanup-preserved-",
                    )
                    _rename_noreplace(stage, preserved)
                    warnings.warn(
                        "unbound plan-only staging was preserved at "
                        f"{preserved}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                else:
                    preserved = _preserve_plan_only_transaction(
                        stage,
                        identity=stage_identity,
                        parent_identity=parent_identity,
                        retain_recovery=retain_recovery,
                    )
                    if (
                        retain_recovery
                        and recovery_path is None
                        and preserved is not None
                        and primary_error is not None
                    ):
                        recovery_path = preserved
                        try:
                            primary_error.add_note(
                                "plan-only recovery state retained at "
                                f"{recovery_path}"
                            )
                        except BaseException:
                            pass
            except BaseException as cleanup_error:
                _note_plan_only_cleanup_failure(
                    primary_error,
                    stage,
                    cleanup_error,
                )


def _rollback_existing_plan_only_documents(
    output: Path,
    stage: Path,
    backup: Path,
    old_snapshots: dict[str, _PlanOnlyFileSnapshot | None],
    new_snapshots: dict[str, _PlanOnlyFileSnapshot],
) -> list[str]:
    """Restore all managed old paths; the old plan is restored last."""

    errors: list[str] = []
    document_names = tuple(new_snapshots)
    auxiliary_names = tuple(
        name for name in document_names if name != _PLAN_ONLY_COMMIT_NAME
    )
    for name in reversed((*auxiliary_names, _PLAN_ONLY_COMMIT_NAME)):
        target = output / name
        try:
            current = _optional_plan_only_file_snapshot(target)
        except OSError as exc:
            errors.append(f"inspect new {name}: {exc}")
            continue
        if current is None or not _same_plan_only_file_snapshot(
            new_snapshots[name], current
        ):
            continue
        staged = stage / name
        if os.path.lexists(staged):
            errors.append(f"new staging path was occupied during rollback: {name}")
            continue
        withdrawal_error = _withdraw_installed_plan_only_document(
            target,
            staged,
            new_snapshots[name],
        )
        if withdrawal_error is not None:
            errors.append(withdrawal_error)

    restore_order = (*auxiliary_names, _PLAN_ONLY_COMMIT_NAME)
    for name in restore_order:
        target = output / name
        expected = old_snapshots[name]
        try:
            current = _optional_plan_only_file_snapshot(target)
        except OSError as exc:
            errors.append(f"inspect old {name}: {exc}")
            continue
        if expected is None:
            if current is not None:
                errors.append(f"unexpected document occupied rollback target: {name}")
            continue
        if current is not None and _same_plan_only_file_snapshot(
            expected, current
        ):
            continue
        source = backup / name
        try:
            backed_up = _optional_plan_only_file_snapshot(source)
        except OSError as exc:
            errors.append(f"inspect backup {name}: {exc}")
            continue
        if current is not None:
            errors.append(f"rollback target was occupied by another writer: {name}")
            continue
        if backed_up is None or not _same_plan_only_file_snapshot(
            expected, backed_up
        ):
            errors.append(f"previous document backup is unavailable: {name}")
            continue
        try:
            _rename_noreplace(source, target)
            _require_plan_only_file_snapshot(
                target,
                expected,
                f"previous document changed during rollback: {name}",
            )
        except BaseException as exc:
            try:
                restored = _optional_plan_only_file_snapshot(target)
                source_now = _optional_plan_only_file_snapshot(source)
            except OSError:
                restored = None
                source_now = backed_up
            if (
                restored is not None
                and _same_plan_only_file_snapshot(expected, restored)
                and source_now is None
            ):
                continue
            errors.append(f"restore old {name}: {exc}")

    for name in document_names:
        target = output / name
        expected = old_snapshots[name]
        try:
            current = _optional_plan_only_file_snapshot(target)
        except OSError as exc:
            errors.append(f"verify rollback {name}: {exc}")
            continue
        if expected is None:
            if current is not None:
                errors.append(f"rollback left a new document visible: {name}")
        elif current is None or not _same_plan_only_file_snapshot(
            expected, current
        ):
            errors.append(f"rollback did not restore previous document: {name}")
    return errors


def _withdraw_installed_plan_only_document(
    target: Path,
    staged: Path,
    expected: _PlanOnlyFileSnapshot,
) -> str | None:
    """Withdraw one installed file without hiding a source-swap racer."""

    move_error: BaseException | None = None
    try:
        _rename_noreplace(target, staged)
    except BaseException as exc:
        move_error = exc

    try:
        moved = _optional_plan_only_file_snapshot(staged)
        target_now = _optional_plan_only_file_snapshot(target)
    except OSError as inspect_error:
        return f"withdraw new {target.name}: cannot rebind moved entry: {inspect_error}"

    if (
        moved is not None
        and _same_plan_only_file_snapshot(expected, moved)
        and target_now is None
    ):
        # The desired new file was withdrawn.  Treat a wrapper error reported
        # after the native move as success so rollback can restore the old file.
        return None
    if moved is None:
        return f"withdraw new {target.name}: {move_error or 'moved entry is missing'}"

    # The prechecked source was exchanged before the rename.  If the public
    # path is vacant, return the actual moved entry there with no-replace
    # semantics.  The old generation restore loop will then observe the racer
    # and leave its own backup in transaction recovery.
    if target_now is None:
        restore_error: BaseException | None = None
        try:
            _rename_noreplace(staged, target)
        except BaseException as exc:
            restore_error = exc
        try:
            restored = _optional_plan_only_file_snapshot(target)
            staged_now = _optional_plan_only_file_snapshot(staged)
        except OSError as inspect_error:
            return (
                f"restore racing {target.name}: cannot verify recovery: "
                f"{inspect_error}"
            )
        if (
            restored is not None
            and _same_plan_only_file_snapshot(moved, restored)
            and staged_now is None
        ):
            return (
                f"new document source changed during rollback and the racing "
                f"entry was restored: {target.name}"
            )
        return (
            f"restore racing {target.name}: "
            f"{restore_error or 'recovered entry identity changed'}"
        )
    return (
        "new document source changed during rollback; its public path was "
        f"already occupied and the moved entry remains in recovery: {target.name}"
    )


def _backup_existing_plan_only_document(
    target: Path,
    backup: Path,
    expected: _PlanOnlyFileSnapshot,
) -> None:
    """Move the expected old file aside without hiding a racing replacement."""

    destination = backup / target.name
    move_error: BaseException | None = None
    try:
        _rename_noreplace(target, destination)
    except BaseException as exc:
        move_error = exc

    try:
        moved = _optional_plan_only_file_snapshot(destination)
        target_now = _optional_plan_only_file_snapshot(target)
    except OSError as inspect_error:
        if not os.path.lexists(target) and os.path.lexists(destination):
            restore_error: BaseException | None = None
            try:
                _rename_noreplace(destination, target)
            except BaseException as exc:
                restore_error = exc
            primary = move_error or inspect_error
            if os.path.lexists(target) and not os.path.lexists(destination):
                try:
                    primary.add_note(
                        "unverified plan-only backup entry was conservatively "
                        f"restored to {target}"
                    )
                except BaseException:
                    pass
                raise primary
            try:
                primary.add_note(
                    f"unverified plan-only entry retained at {destination}; "
                    f"restore error: {restore_error}"
                )
            except BaseException:
                pass
            raise primary
        if move_error is not None:
            try:
                move_error.add_note(
                    f"unverified plan-only entry retained at {destination}: "
                    f"{inspect_error}"
                )
            except BaseException:
                pass
            raise move_error
        raise RuntimeError(
            f"existing plan-only document could not be rebound: {target.name}"
        ) from inspect_error

    moved_expected = moved is not None and _same_plan_only_file_snapshot(
        expected,
        moved,
    )
    if moved_expected and target_now is None:
        # The operation succeeded even if a wrapper reported failure after the
        # native rename.  The expected old file is safely in the backup, so
        # handled rollback can restore it without replacing the primary error.
        if move_error is not None:
            raise move_error
        return
    if moved is None:
        if move_error is not None:
            raise move_error
        raise RuntimeError(
            f"existing plan-only backup disappeared: {target.name}"
        )

    # A source-swap racer was moved into the private backup after the expected
    # file was checked.  Restore that actual entry to its public path whenever
    # the path is vacant; never leave it hidden merely because it is not the
    # expected old generation.
    if target_now is None:
        restore_error: BaseException | None = None
        try:
            _rename_noreplace(destination, target)
        except BaseException as exc:
            restore_error = exc
        try:
            restored = _optional_plan_only_file_snapshot(target)
            destination_now = _optional_plan_only_file_snapshot(destination)
        except OSError as inspect_error:
            raise RuntimeError(
                f"racing plan-only document recovery is uncertain: {target.name}"
            ) from inspect_error
        if (
            restored is not None
            and _same_plan_only_file_snapshot(moved, restored)
            and destination_now is None
        ):
            raise RuntimeError(
                f"existing plan-only document changed during backup: {target.name}"
            ) from move_error
        if restore_error is not None:
            raise RuntimeError(
                "racing plan-only document could not be restored to its "
                f"public path: {target.name}: {restore_error}"
            ) from move_error
    raise RuntimeError(
        "racing plan-only document was retained in transaction recovery "
        f"because its public path was occupied: {target.name}"
    ) from move_error


def _publish_into_existing_plan_only_directory(
    output_identity: PlainDirectoryIdentity,
    payloads: dict[str, bytes],
) -> None:
    output = revalidate_plain_directory(output_identity)
    document_names = tuple(payloads)
    auxiliary_names = tuple(
        name for name in document_names if name != _PLAN_ONLY_COMMIT_NAME
    )
    receipt = output / _PLAN_ONLY_RECEIPT_NAME
    if os.path.lexists(receipt):
        raise ValueError(
            "plan-only refuses to modify a directory containing 渲染回执.json; "
            "choose a separate output directory"
        )
    for name in _PLAN_ONLY_OPTIONAL_DOCUMENT_NAMES:
        if name not in payloads and os.path.lexists(output / name):
            raise ValueError(
                "plan-only refuses to leave a stale optional document in "
                f"place: {name}; choose a separate output directory"
            )
    old_snapshots = {
        name: _optional_plan_only_file_snapshot(output / name)
        for name in document_names
    }
    parent_identity = capture_plain_directory(output.parent)
    parent = revalidate_plain_directory(parent_identity)
    transaction = Path(
        tempfile.mkdtemp(
            dir=parent,
            prefix=f".{output.name or 'plan'}.plan-only-transaction.",
        )
    )
    transaction_identity = capture_plain_directory(transaction)
    stage = transaction / "new"
    backup = transaction / "old"
    primary_error: BaseException | None = None
    publication_started = False
    retain_recovery = False
    recovery_path: Path | None = None
    new_snapshots: dict[str, _PlanOnlyFileSnapshot] = {}
    try:
        stage.mkdir()
        backup.mkdir()
        stage_identity = capture_plain_directory(stage)
        backup_identity = capture_plain_directory(backup)
        new_snapshots = _write_plan_only_stage(stage, payloads)
        _require_plan_only_stage(stage_identity, new_snapshots)
        revalidate_plain_directory(backup_identity)
        revalidate_plain_directory(output_identity)
        if os.path.lexists(receipt):
            raise RuntimeError("a render receipt appeared during plan-only staging")

        # Removing the old plan first makes it unavailable as a weak commit
        # signal while auxiliary documents are changing.  This ordering does
        # not claim crash atomicity across the managed directory entries.
        publication_started = True
        for name in (_PLAN_ONLY_COMMIT_NAME, *auxiliary_names):
            target = output / name
            expected = old_snapshots[name]
            revalidate_plain_directory(output_identity)
            if os.path.lexists(receipt):
                raise RuntimeError("a render receipt appeared during plan-only publication")
            if expected is None:
                if os.path.lexists(target):
                    raise RuntimeError(
                        f"plan-only output appeared during publication: {name}"
                    )
                continue
            revalidate_plain_file(expected[0])
            _require_plan_only_file_snapshot(
                target,
                expected,
                f"existing plan-only document changed before backup: {name}",
            )
            _backup_existing_plan_only_document(
                target,
                backup,
                expected,
            )

        for name in (*auxiliary_names, _PLAN_ONLY_COMMIT_NAME):
            target = output / name
            revalidate_plain_directory(output_identity)
            if os.path.lexists(receipt):
                raise RuntimeError("a render receipt appeared during plan-only publication")
            if os.path.lexists(target):
                raise RuntimeError(
                    f"plan-only publication target was occupied: {name}"
                )
            _require_plan_only_file_snapshot(
                stage / name,
                new_snapshots[name],
                f"staged plan-only document changed before install: {name}",
            )
            _rename_noreplace(stage / name, target)
            _require_plan_only_file_snapshot(
                target,
                new_snapshots[name],
                f"plan-only document changed during install: {name}",
            )

        revalidate_plain_directory(output_identity)
        if os.path.lexists(receipt):
            raise RuntimeError("a render receipt appeared during plan-only publication")
        for name in document_names:
            _require_plan_only_file_snapshot(
                output / name,
                new_snapshots[name],
                f"published plan-only document changed: {name}",
            )
    except BaseException as exc:
        primary_error = exc
        rollback_errors: list[str] = []
        if publication_started:
            try:
                rollback_errors = _rollback_existing_plan_only_documents(
                    output,
                    stage,
                    backup,
                    old_snapshots,
                    new_snapshots,
                )
            except BaseException as rollback_error:
                rollback_errors.append(
                    f"rollback helper failed: {rollback_error}"
                )
        if rollback_errors:
            retain_recovery = True
            try:
                recovery_path = _preserve_plan_only_transaction(
                    transaction,
                    identity=transaction_identity,
                    parent_identity=parent_identity,
                    retain_recovery=True,
                )
            except BaseException as preservation_error:
                rollback_errors.append(
                    f"preserve recovery directory: {preservation_error}"
                )
            try:
                exc.add_note(
                    "plan-only publication rollback was incomplete"
                )
                exc.add_note(
                    "recovery state retained at "
                    f"{recovery_path or transaction}"
                )
            except BaseException:
                pass
            for detail in rollback_errors:
                try:
                    exc.add_note(f"rollback error: {detail}")
                except BaseException:
                    pass
        raise
    finally:
        if os.path.lexists(transaction):
            try:
                _preserve_plan_only_transaction(
                    transaction,
                    identity=transaction_identity,
                    parent_identity=parent_identity,
                    retain_recovery=retain_recovery,
                )
            except BaseException as cleanup_error:
                _note_plan_only_cleanup_failure(
                    primary_error,
                    transaction,
                    cleanup_error,
                )


def _write_plan_only_transaction(
    output_directory: str | Path,
    documents: dict[str, object],
) -> dict[str, Path]:
    """Publish deterministic plan documents as one handled transaction.

    A new output is one directory-level no-replace rename.  An existing
    plan-only directory retains unrelated entries and receives failure
    rollback across the fixed JSON paths.  Filesystems do not provide one
    portable crash-atomic operation for those paths, so this contract is
    deliberately limited to cooperative locking and handled failures.
    """

    required = set(_PLAN_ONLY_DOCUMENT_NAMES)
    optional = set(_PLAN_ONLY_OPTIONAL_DOCUMENT_NAMES)
    if not required <= set(documents) or set(documents) - (required | optional):
        raise ValueError(
            "plan-only transaction requires the four core documents and "
            "only supported optional documents"
        )
    document_names = (
        *_PLAN_ONLY_DOCUMENT_NAMES,
        *(
            name
            for name in _PLAN_ONLY_OPTIONAL_DOCUMENT_NAMES
            if name in documents
        ),
    )
    payloads = {
        name: _pretty_json_bytes(documents[name])
        for name in document_names
    }
    requested = Path(output_directory)
    with acquire_render_lock(requested) as ownership:
        output = ownership.output_directory
        if os.path.lexists(output):
            output_identity = capture_plain_directory(output)
            _publish_into_existing_plan_only_directory(
                output_identity,
                payloads,
            )
        else:
            parent_identity = capture_plain_directory(output.parent)
            _publish_new_plan_only_directory(
                output,
                payloads,
                parent_identity=parent_identity,
            )
    return {name: Path(output_directory) / name for name in documents}
