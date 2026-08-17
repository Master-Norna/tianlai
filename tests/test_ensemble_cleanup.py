from __future__ import annotations

import errno
import os
from pathlib import Path
import warnings

import pytest

import tianlai.ensemble as ensemble_module
from tianlai.render_lock import capture_plain_directory


def _private_directory(tmp_path: Path, name: str):
    parent = tmp_path.resolve()
    path = parent / name
    path.mkdir()
    return (
        parent,
        path,
        capture_plain_directory(parent),
        capture_plain_directory(path),
    )


def test_empty_private_render_directory_is_preserved_without_path_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, path, parent_identity, path_identity = _private_directory(
        tmp_path, ".mix.render-stage.empty"
    )

    def reject_path_deletion(_path) -> None:
        raise AssertionError("cleanup must not delete a mutable directory path")

    monkeypatch.setattr(ensemble_module.os, "rmdir", reject_path_deletion)
    preserved = ensemble_module._remove_private_render_directory(
        path,
        parent,
        ".mix.render-stage.",
        parent_identity=parent_identity,
        directory_identity=path_identity,
    )

    assert preserved is not None
    assert not path.exists()
    assert preserved.is_dir()
    assert list(parent.glob(".mix.render-stage.*")) == [preserved]


def test_atomic_json_replace_failure_does_not_unlink_racing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "receipt.json"
    parked_payload = tmp_path / "parked-receipt-payload.tmp"
    sentinel = b"racing receipt sentinel"
    observed_temporary: Path | None = None
    real_replace = os.replace

    def fail_after_replacing_temporary_name(source, destination):
        nonlocal observed_temporary
        source_path = Path(source)
        destination_path = Path(destination)
        if destination_path == target:
            observed_temporary = source_path
            os.rename(source_path, parked_payload)
            source_path.write_bytes(sentinel)
            raise PermissionError("simulated receipt replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(
        ensemble_module.os,
        "replace",
        fail_after_replacing_temporary_name,
    )
    with pytest.raises(PermissionError, match="simulated receipt replace failure"):
        ensemble_module._write_json_atomic(target, {"state": "complete"})

    assert observed_temporary is not None
    assert observed_temporary.read_bytes() == sentinel
    assert b'"state": "complete"' in parked_payload.read_bytes()
    assert not target.exists()


def test_failed_nonempty_render_stage_is_recoverably_renamed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "published"

    def fail_after_partial_output(_plan, staging: Path, **_kwargs):
        nested = staging / "partial"
        nested.mkdir()
        (nested / "recoverable.bin").write_bytes(b"partial render")
        raise RuntimeError("simulated render failure")

    monkeypatch.setattr(
        ensemble_module,
        "_render_plan_generation",
        fail_after_partial_output,
    )
    with pytest.raises(RuntimeError, match="simulated render failure"):
        ensemble_module._render_plan_locked(object(), output)

    assert not output.exists()
    assert list(tmp_path.glob(".published.render-stage.*")) == list(
        tmp_path.glob(
            ".published.render-stage.*.cleanup-preserved-*"
        )
    )
    preserved = list(
        tmp_path.glob(
            ".published.render-stage.*.cleanup-preserved-*"
        )
    )
    assert len(preserved) == 1
    assert (preserved[0] / "partial" / "recoverable.bin").read_bytes() == (
        b"partial render"
    )


def test_cleanup_race_preserves_post_validation_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, path, parent_identity, path_identity = _private_directory(
        tmp_path, ".mix.render-backup.race"
    )
    (path / "original.bin").write_bytes(b"original")
    parked_original = parent / "parked-original"
    replacement_marker = b"replacement must not be deleted"
    real_revalidate = ensemble_module.revalidate_plain_directory
    raced = False

    def replace_after_identity_check(identity):
        nonlocal raced
        resolved = real_revalidate(identity)
        if identity is path_identity and not raced:
            raced = True
            os.rename(path, parked_original)
            path.mkdir()
            nested = path / "nested"
            nested.mkdir()
            (nested / "replacement.bin").write_bytes(replacement_marker)
        return resolved

    monkeypatch.setattr(
        ensemble_module,
        "revalidate_plain_directory",
        replace_after_identity_check,
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        preserved = ensemble_module._remove_private_render_directory(
            path,
            parent,
            ".mix.render-backup.",
            parent_identity=parent_identity,
            directory_identity=path_identity,
        )

    assert preserved is not None
    assert not path.exists()
    assert (parked_original / "original.bin").read_bytes() == b"original"
    assert (preserved / "nested" / "replacement.bin").read_bytes() == (
        replacement_marker
    )
    assert any("身份替换" in str(item.message) for item in caught)


@pytest.mark.parametrize("rename_error", (FileNotFoundError, OSError))
def test_cleanup_accepts_source_disappearance_during_the_rename_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rename_error: type[OSError],
) -> None:
    parent, path, parent_identity, path_identity = _private_directory(
        tmp_path, ".mix.render-stage.committed"
    )
    real_rename = os.rename

    def disappear_before_rename(source, destination):
        if Path(source) == path:
            os.rmdir(path)
            raise rename_error("private parent generation was committed")
        return real_rename(source, destination)

    monkeypatch.setattr(ensemble_module.os, "rename", disappear_before_rename)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        preserved = ensemble_module._remove_private_render_directory(
            path,
            parent,
            ".mix.render-stage.",
            parent_identity=parent_identity,
            directory_identity=path_identity,
        )

    assert preserved is None
    assert not path.exists()
    assert caught == []


def test_cleanup_uses_compact_recovery_name_when_descriptive_name_is_too_long(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, path, parent_identity, path_identity = _private_directory(
        tmp_path, ".mix.render-stage.path-limit"
    )
    real_rename = os.rename

    def reject_descriptive_recovery_name(source, destination):
        if Path(destination).name.startswith(path.name):
            raise OSError(errno.ENAMETOOLONG, "simulated path-length limit")
        return real_rename(source, destination)

    monkeypatch.setattr(
        ensemble_module.os,
        "rename",
        reject_descriptive_recovery_name,
    )
    preserved = ensemble_module._remove_private_render_directory(
        path,
        parent,
        ".mix.render-stage.",
        parent_identity=parent_identity,
        directory_identity=path_identity,
    )

    assert preserved is not None
    assert preserved.name.startswith(".cleanup-preserved-")
    assert preserved.is_dir()
