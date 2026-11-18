from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

import tianlai.ensemble as ensemble_module

RECEIPT = ensemble_module.RENDER_RECEIPT_NAME
ARTIFACTS = tuple(
    name for name in ensemble_module._RENDER_ARTIFACT_NAMES if name != RECEIPT
)


def _write_generation(directory: Path, marker: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACTS:
        if name == "分轨":
            (directory / name).mkdir()
            (directory / name / "声部.wav").write_bytes(marker.encode())
            continue
        (directory / name).write_bytes(marker.encode())
    (directory / RECEIPT).write_bytes(marker.encode())


def test_interrupted_publication_restores_the_previous_generation(
    tmp_path: Path,
) -> None:
    final = tmp_path / "作品"
    final.mkdir()
    backup = tmp_path / f".{final.name}.render-backup.interrupted"
    _write_generation(backup, "previous")
    # A publication that died while installing the new generation leaves a
    # partial, uncommitted artifact in place and no receipt.
    (final / "合奏.wav").write_bytes(b"partial new mix")

    with pytest.warns(RuntimeWarning, match="恢复上一版完整产物"):
        ensemble_module._recover_interrupted_render_publication(final)

    assert (final / RECEIPT).read_bytes() == b"previous"
    assert (final / "合奏.wav").read_bytes() == b"previous"
    assert (final / "分轨" / "声部.wav").read_bytes() == b"previous"
    # The backup became the output directory; nothing hides beside it.
    assert ensemble_module._pending_render_backups(final) == []
    # The never-committed attempt was quarantined rather than deleted.
    preserved = list(
        tmp_path.glob(f".{final.name}.render-recover.*.cleanup-preserved-*")
    )
    assert len(preserved) == 1
    crashed = preserved[0] / "generation"
    assert (crashed / "合奏.wav").read_bytes() == b"partial new mix"
    assert not (crashed / RECEIPT).exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        [final.name, preserved[0].name]
    )


def test_new_only_artifacts_never_sit_beside_the_restored_receipt(
    tmp_path: Path,
) -> None:
    final = tmp_path / "作品"
    final.mkdir()
    backup = tmp_path / f".{final.name}.render-backup.interrupted"
    _write_generation(backup, "previous")
    # The previous generation was a manual render, so it has no mix report.
    (backup / ensemble_module.MIX_REPORT_NAME).unlink()
    # The interrupted publication had already installed its own report.
    (final / ensemble_module.MIX_REPORT_NAME).write_bytes(b"report of the attempt")

    with pytest.warns(RuntimeWarning, match="恢复上一版完整产物"):
        ensemble_module._recover_interrupted_render_publication(final)

    assert (final / RECEIPT).read_bytes() == b"previous"
    # A manual receipt beside a collaboration report is exactly the state
    # verify_render_generation refuses; recovery must not create it.
    assert not (final / ensemble_module.MIX_REPORT_NAME).exists()


def _link_directory(link: Path, target: Path) -> bool:
    """Create one directory link the way this platform and session allow."""

    try:
        link.symlink_to(target, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name != "nt":
        return False
    created = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
    )
    return created.returncode == 0


def test_linked_backup_is_never_treated_as_owned_state(tmp_path: Path) -> None:
    final = tmp_path / "作品"
    final.mkdir()
    elsewhere = tmp_path / "elsewhere"
    _write_generation(elsewhere, "not ours")
    link = tmp_path / f".{final.name}.render-backup.linked"
    if not _link_directory(link, elsewhere):  # pragma: no cover - platform
        pytest.skip("directory links are unavailable")

    with pytest.warns(RuntimeWarning, match="不是普通目录"):
        ensemble_module._recover_interrupted_render_publication(final)

    assert list(final.iterdir()) == []
    assert (elsewhere / RECEIPT).read_bytes() == b"not ours"
    assert (elsewhere / "合奏.wav").read_bytes() == b"not ours"


def test_recovery_retries_after_it_was_itself_interrupted(tmp_path: Path) -> None:
    final = tmp_path / "作品"
    backup = tmp_path / f".{final.name}.render-backup.interrupted"
    _write_generation(backup, "previous")
    # A recovery that died between its two directory renames leaves no output
    # directory at all; the next publication must still be able to restore.
    assert not final.exists()

    with pytest.warns(RuntimeWarning, match="恢复上一版完整产物"):
        ensemble_module._recover_interrupted_render_publication(final)

    assert (final / RECEIPT).read_bytes() == b"previous"
    assert (final / "合奏.wav").read_bytes() == b"previous"


def test_non_artifact_entries_survive_the_recovery(tmp_path: Path) -> None:
    final = tmp_path / "作品"
    final.mkdir()
    (final / "交付说明.md").write_bytes(b"listener notes")
    backup = tmp_path / f".{final.name}.render-backup.interrupted"
    _write_generation(backup, "previous")

    with pytest.warns(RuntimeWarning, match="恢复上一版完整产物"):
        ensemble_module._recover_interrupted_render_publication(final)

    assert (final / RECEIPT).read_bytes() == b"previous"
    assert (final / "交付说明.md").read_bytes() == b"listener notes"


def test_committed_generation_retires_a_leftover_backup(tmp_path: Path) -> None:
    final = tmp_path / "作品"
    _write_generation(final, "current")
    backup = tmp_path / f".{final.name}.render-backup.stale"
    _write_generation(backup, "previous")

    with pytest.warns(RuntimeWarning, match="退出活跃命名空间"):
        ensemble_module._recover_interrupted_render_publication(final)

    assert (final / RECEIPT).read_bytes() == b"current"
    assert ensemble_module._pending_render_backups(final) == []


def test_ambiguous_interrupted_publication_fails_closed(tmp_path: Path) -> None:
    final = tmp_path / "作品"
    final.mkdir()
    for suffix in ("one", "two"):
        _write_generation(tmp_path / f".{final.name}.render-backup.{suffix}", "old")

    with pytest.raises(RuntimeError, match="无法判断应恢复哪一版"):
        ensemble_module._recover_interrupted_render_publication(final)

    # Nothing was guessed or moved.
    assert list(final.iterdir()) == []


def test_clean_output_directory_is_accepted_without_the_output_lock(
    tmp_path: Path,
) -> None:
    empty = tmp_path / "private"
    empty.mkdir()
    absent = tmp_path / "absent"

    assert ensemble_module._claim_private_output(empty) == empty.resolve()
    assert ensemble_module._claim_private_output(absent) == absent.resolve()


def test_shared_output_directory_is_refused_without_the_output_lock(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared"
    _write_generation(shared, "existing")

    with pytest.raises(ValueError, match="私有空目录"):
        ensemble_module._claim_private_output(shared)

    file_target = tmp_path / "not-a-directory"
    file_target.write_bytes(b"x")
    with pytest.raises(ValueError, match="必须是目录"):
        ensemble_module._claim_private_output(file_target)


def test_one_process_cannot_render_twice_into_the_same_private_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ensemble_module,
        "validate_render_request_resource_limits",
        lambda *_args, **_kwargs: None,
    )
    entered = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def blocking_render(_plan, output, **_kwargs):
        calls.append(str(output))
        entered.set()
        assert release.wait(timeout=30)
        return None

    monkeypatch.setattr(ensemble_module, "_render_plan_locked", blocking_render)
    target = tmp_path / "private"
    errors: list[BaseException] = []

    def first() -> None:
        try:
            ensemble_module.render_plan(
                object(), target, _acquire_output_lock=False
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(timeout=30)
    try:
        with pytest.raises(RuntimeError, match="同一进程内已有渲染"):
            ensemble_module.render_plan(
                object(), target, _acquire_output_lock=False
            )
    finally:
        release.set()
        worker.join(timeout=30)

    assert errors == []
    assert len(calls) == 1
    assert ensemble_module._ACTIVE_PRIVATE_OUTPUTS == set()
