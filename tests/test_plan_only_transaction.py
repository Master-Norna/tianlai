from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch
import warnings

import pytest

from tianlai import plan_only_publish as cli_module


NAMES = (
    "演奏计划.json",
    "渲染配置.json",
    "资源预检.json",
    "创作自检.json",
)


def _documents(generation: str) -> dict[str, object]:
    return {
        "演奏计划.json": {
            "z_generation": generation,
            "a_unicode": "天籁",
        },
        "渲染配置.json": {
            "z_generation": generation,
            "a_hall": False,
        },
        "资源预检.json": {
            "z_generation": generation,
            "a_passed": True,
        },
        "创作自检.json": {
            "z_generation": generation,
            "a_items": [],
        },
    }


def _expected_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def _fixed_bytes(directory: Path) -> dict[str, bytes]:
    return {name: (directory / name).read_bytes() for name in NAMES}


def test_new_directory_is_one_no_replace_publish_with_deterministic_json(
    tmp_path: Path,
) -> None:
    output = tmp_path / "计划 预检"
    documents = _documents("first")
    real_rename = cli_module._rename_noreplace
    moves: list[tuple[Path, Path]] = []

    def record(source: str | Path, destination: str | Path) -> None:
        moves.append((Path(source), Path(destination)))
        real_rename(source, destination)

    with patch.object(cli_module, "_rename_noreplace", side_effect=record):
        published = cli_module._write_plan_only_transaction(output, documents)

    assert output.is_dir()
    assert set(entry.name for entry in output.iterdir()) == set(NAMES)
    assert moves == [(moves[0][0], output)]
    assert moves[0][0].parent == output.parent
    assert ".plan-only-stage." in moves[0][0].name
    assert published == {name: output / name for name in NAMES}
    for name in NAMES:
        payload = (output / name).read_bytes()
        assert payload == _expected_bytes(documents[name])
        assert payload.startswith(b"{")
        assert payload.endswith(b"\n")
        assert b"\r" not in payload
        assert not payload.startswith(b"\xef\xbb\xbf")
    assert list(tmp_path.glob(f".{output.name}.plan-only-stage.*")) == []


def test_existing_directory_preserves_extras_and_uses_commit_order(
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    sentinel = output / "keep-user-data.txt"
    sentinel.write_bytes(b"must survive")
    documents = _documents("new")
    real_rename = cli_module._rename_noreplace
    old_moves: list[str] = []
    new_moves: list[str] = []

    def record(source: str | Path, destination: str | Path) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        if source_path.parent == output and source_path.name in NAMES:
            old_moves.append(source_path.name)
        if destination_path.parent == output and destination_path.name in NAMES:
            new_moves.append(destination_path.name)
        real_rename(source, destination)

    with patch.object(cli_module, "_rename_noreplace", side_effect=record):
        cli_module._write_plan_only_transaction(output, documents)

    assert old_moves == [
        "演奏计划.json",
        "渲染配置.json",
        "资源预检.json",
        "创作自检.json",
    ]
    assert new_moves == [
        "渲染配置.json",
        "资源预检.json",
        "创作自检.json",
        "演奏计划.json",
    ]
    assert sentinel.read_bytes() == b"must survive"
    assert _fixed_bytes(output) == {
        name: _expected_bytes(documents[name]) for name in NAMES
    }
    active = [
        path
        for path in tmp_path.glob(f".{output.name}.plan-only-transaction.*")
        if ".cleanup-preserved-" not in path.name
    ]
    assert active == []
    assert list(
        tmp_path.glob(
            f".{output.name}.plan-only-transaction.*.cleanup-preserved-*"
        )
    ) == []


@pytest.mark.parametrize("failed_install", (1, 2, 3, 4))
def test_each_existing_install_failure_restores_all_four_previous_files(
    tmp_path: Path,
    failed_install: int,
) -> None:
    output = tmp_path / f"rollback-{failed_install}"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    sentinel = output / "extra.bin"
    sentinel.write_bytes(b"extra")
    before = _fixed_bytes(output)
    real_rename = cli_module._rename_noreplace
    install_attempts = 0
    injected = False

    def fail_one_install(source: str | Path, destination: str | Path) -> None:
        nonlocal install_attempts, injected
        destination_path = Path(destination)
        if (
            destination_path.parent == output
            and destination_path.name in NAMES
            and not injected
        ):
            install_attempts += 1
            if install_attempts == failed_install:
                injected = True
                raise OSError(f"injected install failure {failed_install}")
        real_rename(source, destination)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=fail_one_install,
        ),
        pytest.raises(OSError, match=f"injected install failure {failed_install}"),
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert injected
    assert _fixed_bytes(output) == before
    assert sentinel.read_bytes() == b"extra"
    assert list(tmp_path.glob(f".{output.name}.plan-only-*")) == []


def test_failed_first_generation_in_existing_empty_directory_publishes_none(
    tmp_path: Path,
) -> None:
    output = tmp_path / "existing-empty"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    real_rename = cli_module._rename_noreplace
    installs = 0

    def fail_third_install(source: str | Path, destination: str | Path) -> None:
        nonlocal installs
        destination_path = Path(destination)
        if destination_path.parent == output and destination_path.name in NAMES:
            installs += 1
            if installs == 3:
                raise PermissionError("injected empty-directory failure")
        real_rename(source, destination)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=fail_third_install,
        ),
        pytest.raises(PermissionError, match="injected empty-directory failure"),
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert all(not (output / name).exists() for name in NAMES)
    assert list(tmp_path.glob(f".{output.name}.plan-only-*")) == []


def test_render_receipt_refuses_plan_only_without_touching_directory(
    tmp_path: Path,
) -> None:
    output = tmp_path / "render-generation"
    output.mkdir()
    for index, name in enumerate(NAMES):
        (output / name).write_bytes(f"old-{index}".encode("ascii"))
    (output / "渲染回执.json").write_bytes(b"verified render receipt")
    (output / "合奏.wav").write_bytes(b"audio must survive")
    before = {entry.name: entry.read_bytes() for entry in output.iterdir()}

    with pytest.raises(ValueError, match="渲染回执.json"):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert {entry.name: entry.read_bytes() for entry in output.iterdir()} == before
    assert list(tmp_path.glob(f".{output.name}.plan-only-*")) == []


def test_nonfinite_document_fails_before_lock_or_output_creation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "must-not-exist"
    documents = _documents("invalid")
    documents["创作自检.json"] = {"bad": float("nan")}

    with (
        patch.object(cli_module, "acquire_render_lock") as acquire,
        pytest.raises(ValueError),
    ):
        cli_module._write_plan_only_transaction(output, documents)

    acquire.assert_not_called()
    assert not output.exists()


def test_new_directory_racer_is_never_overwritten(
    tmp_path: Path,
) -> None:
    output = tmp_path / "racing-output"
    real_rename = cli_module._rename_noreplace
    raced = False

    def race_before_publish(source: str | Path, destination: str | Path) -> None:
        nonlocal raced
        destination_path = Path(destination)
        if destination_path == output and not raced:
            raced = True
            output.mkdir()
            (output / "racer.txt").write_bytes(b"racing writer")
        real_rename(source, destination)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=race_before_publish,
        ),
        pytest.raises((FileExistsError, OSError)),
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert raced
    assert (output / "racer.txt").read_bytes() == b"racing writer"
    assert all(not (output / name).exists() for name in NAMES)


def test_new_directory_rollback_with_reoccupied_stage_withdraws_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "post-publish-failure"
    real_require = cli_module._require_plan_only_file_snapshot
    real_rename = cli_module._rename_noreplace
    stage_path: Path | None = None
    injected = False

    def reoccupy_stage_after_publish(source, destination):
        nonlocal stage_path
        source_path = Path(source)
        destination_path = Path(destination)
        real_rename(source_path, destination_path)
        if destination_path == output:
            stage_path = source_path
            stage_path.mkdir()
            (stage_path / "racer.txt").write_bytes(b"preserve me")

    def fail_post_publish(path, expected, message):
        nonlocal injected
        candidate = Path(path)
        if candidate.parent == output and not injected:
            injected = True
            assert stage_path is not None
            raise OSError("injected post-publish verification failure")
        return real_require(candidate, expected, message)

    with (
        patch.object(
            cli_module,
            "_require_plan_only_file_snapshot",
            side_effect=fail_post_publish,
        ),
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=reoccupy_stage_after_publish,
        ),
        pytest.raises(
            OSError,
            match="injected post-publish verification failure",
        ),
        pytest.warns(RuntimeWarning, match="identity changed during cleanup"),
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert injected and stage_path is not None
    assert not output.exists()
    preserved = list(
        tmp_path.glob(
            f".{output.name}.plan-only-stage.*.cleanup-preserved-*"
        )
    )
    assert len(preserved) == 1
    assert (preserved[0] / "racer.txt").read_bytes() == b"preserve me"


def test_new_directory_move_then_error_preserves_concurrent_extra_in_recovery(
    tmp_path: Path,
) -> None:
    output = tmp_path / "move-then-extra"
    real_rename = cli_module._rename_noreplace
    injected = False

    def move_add_extra_then_fail(source, destination):
        nonlocal injected
        source_path = Path(source)
        destination_path = Path(destination)
        real_rename(source_path, destination_path)
        if destination_path == output and not injected:
            injected = True
            (output / "concurrent-extra.txt").write_bytes(
                b"concurrent data must survive"
            )
            raise PermissionError("PRIMARY move reported failure")

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=move_add_extra_then_fail,
        ),
        pytest.raises(
            PermissionError,
            match="PRIMARY move reported failure",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    recovery = list(
        tmp_path.glob(
            f".{output.name}.plan-only-stage.*.recovery-preserved-*"
        )
    )
    assert injected
    assert not output.exists()
    assert len(recovery) == 1
    assert (recovery[0] / "concurrent-extra.txt").read_bytes() == (
        b"concurrent data must survive"
    )
    assert _fixed_bytes(recovery[0]) == {
        name: _expected_bytes(_documents("new")[name]) for name in NAMES
    }
    assert caught.value.__cause__ is None
    assert any(
        str(recovery[0]) in note
        for note in getattr(caught.value, "__notes__", ())
    )


def test_new_directory_postcheck_mutation_is_preserved_in_recovery(
    tmp_path: Path,
) -> None:
    output = tmp_path / "postcheck-mutation"
    documents = _documents("new")
    real_rename = cli_module._rename_noreplace
    mutated_name = "渲染配置.json"
    mutated_bytes = b'{"mutated":true}\n'
    injected = False

    def move_then_mutate(source, destination):
        nonlocal injected
        source_path = Path(source)
        destination_path = Path(destination)
        real_rename(source_path, destination_path)
        if destination_path == output and not injected:
            injected = True
            (output / mutated_name).write_bytes(mutated_bytes)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=move_then_mutate,
        ),
        pytest.raises(
            RuntimeError,
            match="published plan-only document changed",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, documents)

    recovery = list(
        tmp_path.glob(
            f".{output.name}.plan-only-stage.*.recovery-preserved-*"
        )
    )
    assert injected
    assert not output.exists()
    assert len(recovery) == 1
    assert (recovery[0] / mutated_name).read_bytes() == mutated_bytes
    for name in NAMES:
        if name != mutated_name:
            assert (recovery[0] / name).read_bytes() == _expected_bytes(
                documents[name]
            )
    assert caught.value.__cause__ is None
    assert any(
        str(recovery[0]) in note
        for note in getattr(caught.value, "__notes__", ())
    )


def test_new_directory_rollback_restores_a_racer_swapped_after_identity_check(
    tmp_path: Path,
) -> None:
    output = tmp_path / "rollback-source-swap"
    parked_published = tmp_path / "parked-published-generation"
    real_require = cli_module._require_plan_only_file_snapshot
    real_rename = cli_module._rename_noreplace
    verification_failed = False
    raced = False

    def fail_post_publish(path, expected, message):
        nonlocal verification_failed
        candidate = Path(path)
        if candidate.parent == output and not verification_failed:
            verification_failed = True
            raise OSError("PRIMARY postcheck failure")
        return real_require(candidate, expected, message)

    def swap_before_rollback_move(source, destination):
        nonlocal raced
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            verification_failed
            and not raced
            and source_path == output
            and ".plan-only-stage." in destination_path.name
        ):
            raced = True
            os.replace(output, parked_published)
            output.mkdir()
            (output / "racer.txt").write_bytes(b"restore this racer")
        real_rename(source_path, destination_path)

    with (
        patch.object(
            cli_module,
            "_require_plan_only_file_snapshot",
            side_effect=fail_post_publish,
        ),
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=swap_before_rollback_move,
        ),
        pytest.raises(OSError, match="PRIMARY postcheck failure") as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert verification_failed and raced
    assert (output / "racer.txt").read_bytes() == b"restore this racer"
    assert _fixed_bytes(parked_published) == {
        name: _expected_bytes(_documents("new")[name]) for name in NAMES
    }
    assert caught.value.__cause__ is None


def test_warning_as_error_cannot_block_withdrawal_or_delete_stage_racer(
    tmp_path: Path,
) -> None:
    output = tmp_path / "warning-filter"
    real_require = cli_module._require_plan_only_file_snapshot
    real_rename = cli_module._rename_noreplace
    stage_path: Path | None = None
    injected = False

    def publish_then_reoccupy_stage(source, destination):
        nonlocal stage_path
        source_path = Path(source)
        destination_path = Path(destination)
        real_rename(source_path, destination_path)
        if destination_path == output:
            stage_path = source_path
            stage_path.mkdir()
            (stage_path / "racer.txt").write_bytes(b"warning-filter racer")

    def fail_post_publish(path, expected, message):
        nonlocal injected
        candidate = Path(path)
        if candidate.parent == output and not injected:
            injected = True
            raise OSError("PRIMARY warning-filter failure")
        return real_require(candidate, expected, message)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        with (
            patch.object(
                cli_module,
                "_require_plan_only_file_snapshot",
                side_effect=fail_post_publish,
            ),
            patch.object(
                cli_module,
                "_rename_noreplace",
                side_effect=publish_then_reoccupy_stage,
            ),
            pytest.raises(
                OSError,
                match="PRIMARY warning-filter failure",
            ) as caught,
        ):
            cli_module._write_plan_only_transaction(output, _documents("new"))

    assert injected and stage_path is not None
    assert not output.exists()
    stage_racers = list(
        tmp_path.glob(
            f".{output.name}.plan-only-stage.*.cleanup-preserved-*"
        )
    )
    recoveries = list(
        tmp_path.glob(
            f".{output.name}.plan-only-stage.*.recovery-preserved-*"
        )
    )
    assert len(stage_racers) == 1
    assert (stage_racers[0] / "racer.txt").read_bytes() == (
        b"warning-filter racer"
    )
    assert len(recoveries) == 1
    assert caught.value.__cause__ is None
    assert any(
        str(recoveries[0]) in note
        for note in getattr(caught.value, "__notes__", ())
    )


def test_incomplete_rollback_preserves_racer_and_previous_backup(
    tmp_path: Path,
) -> None:
    output = tmp_path / "rollback-racer"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    before = _fixed_bytes(output)
    real_rename = cli_module._rename_noreplace
    install_attempts = 0
    publish_failed = False
    racer_installed = False

    def fail_then_race_rollback(
        source: str | Path,
        destination: str | Path,
    ) -> None:
        nonlocal install_attempts, publish_failed, racer_installed
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            destination_path.parent == output
            and destination_path.name in NAMES
            and not publish_failed
        ):
            install_attempts += 1
            if install_attempts == 2:
                publish_failed = True
                raise OSError("primary publication failure")
        if (
            publish_failed
            and not racer_installed
            and source_path == output / "渲染配置.json"
            and destination_path.parent.name == "new"
        ):
            racer_installed = True
            real_rename(source_path, output / "withdrawn-new-profile.json")
            source_path.write_bytes(b"concurrent writer must survive")
            raise PermissionError("racer blocks rollback")
        real_rename(source, destination)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=fail_then_race_rollback,
        ),
        pytest.raises(OSError, match="primary publication failure") as raised,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert publish_failed
    assert racer_installed
    assert raised.value.__cause__ is None
    assert (output / "渲染配置.json").read_bytes() == (
        b"concurrent writer must survive"
    )
    for name in ("演奏计划.json", "资源预检.json", "创作自检.json"):
        assert (output / name).read_bytes() == before[name]

    recovery = list(
        tmp_path.glob(
            f".{output.name}.plan-only-transaction.*.recovery-preserved-*"
        )
    )
    assert len(recovery) == 1
    assert (recovery[0] / "old" / "渲染配置.json").read_bytes() == before[
        "渲染配置.json"
    ]
    assert any("rollback was incomplete" in note for note in raised.value.__notes__)
    assert any(str(recovery[0]) in note for note in raised.value.__notes__)


def test_existing_first_backup_source_swap_restores_racer_to_public_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "old-backup-source-swap"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    old_plan = output / NAMES[0]
    parked_old = output / "parked-expected-old-plan.json"
    real_rename = cli_module._rename_noreplace
    raced = False

    def swap_before_first_backup(source, destination):
        nonlocal raced
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not raced
            and source_path == old_plan
            and destination_path.parent.name == "old"
        ):
            raced = True
            os.replace(old_plan, parked_old)
            old_plan.write_bytes(b"concurrent old-path racer")
        real_rename(source_path, destination_path)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=swap_before_first_backup,
        ),
        pytest.raises(
            RuntimeError,
            match="existing plan-only document changed during backup",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert raced
    assert old_plan.read_bytes() == b"concurrent old-path racer"
    assert parked_old.read_bytes() == _expected_bytes(_documents("old")[NAMES[0]])
    assert caught.value.__cause__ is None


def test_existing_first_backup_move_then_error_is_recognized_and_rolls_back(
    tmp_path: Path,
) -> None:
    output = tmp_path / "old-backup-move-then-error"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    before = _fixed_bytes(output)
    old_plan = output / NAMES[0]
    real_rename = cli_module._rename_noreplace
    injected = False

    def move_first_backup_then_fail(source, destination):
        nonlocal injected
        source_path = Path(source)
        destination_path = Path(destination)
        real_rename(source_path, destination_path)
        if (
            not injected
            and source_path == old_plan
            and destination_path.parent.name == "old"
        ):
            injected = True
            raise PermissionError("PRIMARY backup move reported failure")

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=move_first_backup_then_fail,
        ),
        pytest.raises(
            PermissionError,
            match="PRIMARY backup move reported failure",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert injected
    assert _fixed_bytes(output) == before
    assert list(tmp_path.glob(f".{output.name}.plan-only-transaction.*")) == []
    assert caught.value.__cause__ is None


def test_existing_backup_inspection_failure_restores_unknown_entry_publicly(
    tmp_path: Path,
) -> None:
    output = tmp_path / "old-backup-inspection-failure"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    before = _fixed_bytes(output)
    old_plan = output / NAMES[0]
    real_rename = cli_module._rename_noreplace
    real_optional = cli_module._optional_plan_only_file_snapshot
    backup_path: Path | None = None
    inspect_failed = False

    def record_backup_move(source, destination):
        nonlocal backup_path
        source_path = Path(source)
        destination_path = Path(destination)
        real_rename(source_path, destination_path)
        if source_path == old_plan and destination_path.parent.name == "old":
            backup_path = destination_path

    def fail_first_backup_snapshot(path):
        nonlocal inspect_failed
        candidate = Path(path)
        if backup_path is not None and candidate == backup_path and not inspect_failed:
            inspect_failed = True
            raise OSError("transient backup inspection failure")
        return real_optional(candidate)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=record_backup_move,
        ),
        patch.object(
            cli_module,
            "_optional_plan_only_file_snapshot",
            side_effect=fail_first_backup_snapshot,
        ),
        pytest.raises(OSError, match="transient backup inspection failure") as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert inspect_failed
    assert _fixed_bytes(output) == before
    assert list(tmp_path.glob(f".{output.name}.plan-only-transaction.*")) == []
    assert any(
        "conservatively restored" in note
        for note in getattr(caught.value, "__notes__", ())
    )


def test_existing_backup_source_swap_with_reoccupied_public_path_is_retained(
    tmp_path: Path,
) -> None:
    output = tmp_path / "old-backup-reoccupied"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    old_plan = output / NAMES[0]
    parked_old = output / "parked-expected-old-plan.json"
    real_rename = cli_module._rename_noreplace
    raced = False

    def swap_and_reoccupy_after_backup(source, destination):
        nonlocal raced
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            not raced
            and source_path == old_plan
            and destination_path.parent.name == "old"
        ):
            raced = True
            os.replace(old_plan, parked_old)
            old_plan.write_bytes(b"first racing entry")
            real_rename(source_path, destination_path)
            old_plan.write_bytes(b"second public occupant")
            return
        real_rename(source_path, destination_path)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=swap_and_reoccupy_after_backup,
        ),
        pytest.raises(
            RuntimeError,
            match="retained in transaction recovery",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    recovery = list(
        tmp_path.glob(
            f".{output.name}.plan-only-transaction.*.recovery-preserved-*"
        )
    )
    assert raced
    assert old_plan.read_bytes() == b"second public occupant"
    assert parked_old.read_bytes() == _expected_bytes(_documents("old")[NAMES[0]])
    assert len(recovery) == 1
    assert (recovery[0] / "old" / NAMES[0]).read_bytes() == (
        b"first racing entry"
    )
    assert caught.value.__cause__ is None
    assert any(
        str(recovery[0]) in note
        for note in getattr(caught.value, "__notes__", ())
    )


@pytest.mark.parametrize("move_then_error", (False, True))
def test_existing_rollback_source_swap_restores_racer_to_public_path(
    tmp_path: Path,
    move_then_error: bool,
) -> None:
    output = tmp_path / f"rollback-new-source-swap-{move_then_error}"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    before = _fixed_bytes(output)
    target = output / "渲染配置.json"
    parked_expected_new = tmp_path / "parked-expected-new-profile.json"
    real_rename = cli_module._rename_noreplace
    install_attempts = 0
    publish_failed = False
    raced = False

    def fail_install_then_swap_rollback(source, destination):
        nonlocal install_attempts, publish_failed, raced
        source_path = Path(source)
        destination_path = Path(destination)
        if (
            destination_path.parent == output
            and destination_path.name in NAMES
            and source_path.parent.name == "new"
            and not publish_failed
        ):
            install_attempts += 1
            if install_attempts == 2:
                publish_failed = True
                raise OSError("PRIMARY existing publication failure")
        if (
            publish_failed
            and not raced
            and source_path == target
            and destination_path.parent.name == "new"
        ):
            raced = True
            os.replace(target, parked_expected_new)
            target.write_bytes(b"rollback source-swap racer")
            real_rename(source_path, destination_path)
            if move_then_error:
                raise PermissionError("rollback move reported failure")
            return
        real_rename(source_path, destination_path)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=fail_install_then_swap_rollback,
        ),
        pytest.raises(
            OSError,
            match="PRIMARY existing publication failure",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    recovery = list(
        tmp_path.glob(
            f".{output.name}.plan-only-transaction.*.recovery-preserved-*"
        )
    )
    assert publish_failed and raced
    assert target.read_bytes() == b"rollback source-swap racer"
    assert parked_expected_new.read_bytes() == _expected_bytes(
        _documents("new")["渲染配置.json"]
    )
    assert len(recovery) == 1
    assert (recovery[0] / "old" / "渲染配置.json").read_bytes() == before[
        "渲染配置.json"
    ]
    assert caught.value.__cause__ is None
    assert any(
        str(recovery[0]) in note
        for note in getattr(caught.value, "__notes__", ())
    )


def test_new_directory_move_error_with_transient_output_capture_is_withdrawn(
    tmp_path: Path,
) -> None:
    output = tmp_path / "move-error-capture-failure"
    real_rename = cli_module._rename_noreplace
    real_capture = cli_module.capture_plain_directory
    moved = False
    capture_failed = False

    def move_then_fail(source, destination):
        nonlocal moved
        source_path = Path(source)
        destination_path = Path(destination)
        real_rename(source_path, destination_path)
        if destination_path == output and not moved:
            moved = True
            raise PermissionError("PRIMARY move-then-error")

    def fail_first_output_capture(path):
        nonlocal capture_failed
        candidate = Path(path)
        if moved and candidate == output and not capture_failed:
            capture_failed = True
            raise OSError("transient output capture failure")
        return real_capture(candidate)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=move_then_fail,
        ),
        patch.object(
            cli_module,
            "capture_plain_directory",
            side_effect=fail_first_output_capture,
        ),
        pytest.raises(
            PermissionError,
            match="PRIMARY move-then-error",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    recovery = list(
        tmp_path.glob(
            f".{output.name}.plan-only-stage.*.recovery-preserved-*"
        )
    )
    assert moved and capture_failed
    assert not output.exists()
    assert len(recovery) == 1
    assert _fixed_bytes(recovery[0]) == {
        name: _expected_bytes(_documents("new")[name]) for name in NAMES
    }
    assert caught.value.__cause__ is None
    assert any(
        str(recovery[0]) in note
        for note in getattr(caught.value, "__notes__", ())
    )


def test_existing_symlink_document_is_rejected_before_staging(
    tmp_path: Path,
) -> None:
    output = tmp_path / "unsafe"
    output.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside must survive")
    link = output / "演奏计划.json"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError):
        pytest.skip("this host cannot create a test symlink")

    with pytest.raises(OSError):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert link.is_symlink()
    assert outside.read_bytes() == b"outside must survive"
    assert list(tmp_path.glob(f".{output.name}.plan-only-*")) == []


def test_existing_rollback_helper_failure_preserves_the_primary_error(
    tmp_path: Path,
) -> None:
    output = tmp_path / "rollback-helper-failure"
    cli_module._write_plan_only_transaction(output, _documents("old"))
    real_rename = cli_module._rename_noreplace
    injected = False

    def fail_first_install(source, destination):
        nonlocal injected
        destination_path = Path(destination)
        if destination_path.parent == output and not injected:
            injected = True
            raise PermissionError("PRIMARY publish failure")
        real_rename(source, destination)

    with (
        patch.object(
            cli_module,
            "_rename_noreplace",
            side_effect=fail_first_install,
        ),
        patch.object(
            cli_module,
            "_rollback_existing_plan_only_documents",
            side_effect=RuntimeError("SECONDARY rollback failure"),
        ),
        pytest.raises(
            PermissionError,
            match="PRIMARY publish failure",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert injected
    assert any(
        "SECONDARY rollback failure" in note
        for note in getattr(caught.value, "__notes__", ())
    )


def test_new_directory_rollback_helper_failure_preserves_the_primary_error(
    tmp_path: Path,
) -> None:
    output = tmp_path / "new-rollback-helper-failure"
    real_require = cli_module._require_plan_only_file_snapshot
    injected = False

    def fail_post_publish(path, expected, message):
        nonlocal injected
        candidate = Path(path)
        if candidate.parent == output and not injected:
            injected = True
            raise PermissionError("PRIMARY new publish failure")
        return real_require(candidate, expected, message)

    with (
        patch.object(
            cli_module,
            "_require_plan_only_file_snapshot",
            side_effect=fail_post_publish,
        ),
        patch.object(
            cli_module,
            "_rollback_new_plan_only_directory",
            side_effect=RuntimeError("SECONDARY new rollback failure"),
        ),
        pytest.raises(
            PermissionError,
            match="PRIMARY new publish failure",
        ) as caught,
    ):
        cli_module._write_plan_only_transaction(output, _documents("new"))

    assert injected
    assert any(
        "SECONDARY new rollback failure" in note
        for note in getattr(caught.value, "__notes__", ())
    )
