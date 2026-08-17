from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import warnings

from tianlai import atomic_publish as atomic_publish_module
import tianlai.renderer as renderer_module
from tianlai.instrument import Instrument
from tianlai.post_render_check import POST_RENDER_CHECK_NAME


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_MANIFEST = (
    ROOT / "乐器" / "测试工具" / "参考振荡器" / "乐器.json"
)


class _ConstantInstrument(Instrument):
    def __init__(self, sample_rate: int = 8_000) -> None:
        super().__init__(sample_rate)

    def handle_event(self, event, tuning) -> None:
        del event, tuning

    def render_frame(self) -> tuple[float, float]:
        return 0.01, -0.01

    @property
    def active_voice_count(self) -> int:
        return 0


def _write_tiny_performance(directory: Path) -> Path:
    path = directory / "tiny.events.json"
    path.write_text(
        json.dumps(
            {
                "sample_rate": 8_000,
                "channels": 2,
                "tail_seconds": 0.0,
                "duration_seconds": 0.001,
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _render_quartet(target: Path) -> tuple[Path, Path, Path, Path]:
    return (
        target,
        target.with_name(f"{target.name}.许可与署名.json"),
        target.with_name(f"{target.name}.许可与署名.txt"),
        Path(f"{target}.{POST_RENDER_CHECK_NAME}"),
    )


def _seed_old_quartet(target: Path) -> dict[Path, bytes]:
    audio_path, json_path, text_path, report_path = _render_quartet(target)
    previous = {
        audio_path: b"previous verified render",
        json_path: b'{"state":"previous verified sidecar"}\n',
        text_path: "此前已核验的署名\n".encode(),
        report_path: b'{"state":"previous verified report"}\n',
    }
    for path, payload in previous.items():
        path.write_bytes(payload)
    return previous


def _sealed_stage(
    directory: Path,
    *,
    label: str,
    payload: bytes,
):
    claim = renderer_module._reserve_private_file(
        directory,
        prefix=f".{label}.",
        suffix=".tianlai-stage",
    )
    atomic_publish_module._write_private_file_bytes(claim, payload)
    return renderer_module._seal_private_file_claim(
        claim,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )


class RendererTransactionBoundaryTests(unittest.TestCase):
    def test_render_claim_cleanup_continues_after_one_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = renderer_module._reserve_private_file(
                directory,
                prefix=".first-stage.",
                suffix=".tmp",
            )
            second = renderer_module._reserve_private_file(
                directory,
                prefix=".second-stage.",
                suffix=".tmp",
            )
            real_retire = renderer_module._retire_private_file
            calls = []

            def fail_first(claim, **kwargs):
                calls.append(claim)
                if claim is first:
                    raise OSError("injected first cleanup failure")
                return real_retire(claim, **kwargs)

            with (
                patch.object(
                    renderer_module,
                    "_retire_private_file",
                    side_effect=fail_first,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "injected first cleanup failure",
                ),
            ):
                renderer_module._retire_render_claims((first, second))

            self.assertEqual(calls, [first, second])
            self.assertFalse(second.path.exists())
            self.assertIsNone(real_retire(first))

    def test_target_appearing_inside_publish_call_is_never_overwritten(
        self,
    ) -> None:
        for existing in (False, True):
            with (
                self.subTest(existing=existing),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                target = directory / "artifact.json"
                old_payload = b"old public generation"
                if existing:
                    target.write_bytes(old_payload)
                staged = _sealed_stage(
                    directory,
                    label="publish-window",
                    payload=b"new transaction generation",
                )
                sentinel = b"concurrent writer sentinel"
                real_replace = renderer_module._replace_published_file
                replace_calls = 0

                def appear_then_replace(source: Path, destination: Path) -> None:
                    nonlocal replace_calls
                    replace_calls += 1
                    self.assertFalse(
                        os.path.lexists(destination),
                        "the old target should already be isolated",
                    )
                    destination.write_bytes(sentinel)
                    real_replace(source, destination)

                try:
                    with patch.object(
                        renderer_module,
                        "_replace_published_file",
                        side_effect=appear_then_replace,
                    ):
                        if existing:
                            with self.assertRaises(RuntimeError):
                                renderer_module._publish_staged_artifacts(
                                    ((staged, target),)
                                )
                        else:
                            with self.assertRaises(FileExistsError):
                                renderer_module._publish_staged_artifacts(
                                    ((staged, target),)
                                )

                    self.assertEqual(replace_calls, 1)
                    self.assertEqual(
                        target.read_bytes(),
                        sentinel,
                        "publication overwrote a target created in its call window",
                    )
                    if existing:
                        old_survivors = [
                            path
                            for path in directory.iterdir()
                            if path.is_file() and path.read_bytes() == old_payload
                        ]
                        self.assertTrue(
                            old_survivors,
                            "the isolated old generation was lost",
                        )
                finally:
                    atomic_publish_module._retire_private_file(staged.claim)

    def test_preserve_recognizes_post_commit_noreplace_errors(self) -> None:
        for error_type in (FileExistsError, FileNotFoundError, OSError):
            with (
                self.subTest(error_type=error_type.__name__),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                target = directory / "artifact.json"
                payload = b"verified published generation"
                staged = _sealed_stage(
                    directory,
                    label=f"preserve-{error_type.__name__}",
                    payload=payload,
                )
                renderer_module._rename_noreplace(staged.claim.path, target)
                real_rename = renderer_module._rename_noreplace
                injected = False

                def move_then_report(source: Path, destination: Path) -> None:
                    nonlocal injected
                    real_rename(source, destination)
                    if not injected:
                        injected = True
                        raise error_type("injected post-commit report")

                try:
                    with patch.object(
                        renderer_module,
                        "_rename_noreplace",
                        side_effect=move_then_report,
                    ):
                        preserved = (
                            renderer_module._preserve_published_generation(
                                staged,
                                target,
                            )
                        )

                    self.assertTrue(injected)
                    self.assertFalse(target.exists())
                    self.assertEqual(preserved.read_bytes(), payload)
                    renderer_module._retire_withdrawn_published_generation(
                        staged,
                        preserved,
                    )
                    self.assertFalse(preserved.exists())
                finally:
                    atomic_publish_module._retire_private_file(staged.claim)

    def test_preserve_keeps_committed_generation_when_public_reappears(
        self,
    ) -> None:
        for error_type in (FileExistsError, FileNotFoundError):
            with (
                self.subTest(error_type=error_type.__name__),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                target = directory / "artifact.json"
                payload = b"verified published generation"
                concurrent_payload = b"concurrent public generation"
                staged = _sealed_stage(
                    directory,
                    label=f"preserve-recreated-{error_type.__name__}",
                    payload=payload,
                )
                renderer_module._rename_noreplace(staged.claim.path, target)
                real_rename = renderer_module._rename_noreplace
                injected = False

                def move_recreate_then_report(
                    source: Path,
                    destination: Path,
                ) -> None:
                    nonlocal injected
                    real_rename(source, destination)
                    if not injected:
                        injected = True
                        target.write_bytes(concurrent_payload)
                        raise error_type("injected post-commit report")

                try:
                    with patch.object(
                        renderer_module,
                        "_rename_noreplace",
                        side_effect=move_recreate_then_report,
                    ):
                        preserved = (
                            renderer_module._preserve_published_generation(
                                staged,
                                target,
                            )
                        )

                    self.assertTrue(injected)
                    self.assertEqual(target.read_bytes(), concurrent_payload)
                    self.assertEqual(preserved.read_bytes(), payload)
                    renderer_module._retire_withdrawn_published_generation(
                        staged,
                        preserved,
                    )
                    self.assertFalse(preserved.exists())
                finally:
                    atomic_publish_module._retire_private_file(staged.claim)

    def test_preserve_does_not_mask_a_precommit_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "artifact.json"
            payload = b"verified published generation"
            staged = _sealed_stage(
                directory,
                label="preserve-permission",
                payload=payload,
            )
            renderer_module._rename_noreplace(staged.claim.path, target)

            try:
                with (
                    patch.object(
                        renderer_module,
                        "_rename_noreplace",
                        side_effect=PermissionError("injected access denied"),
                    ) as rename,
                    self.assertRaisesRegex(
                        PermissionError,
                        "injected access denied",
                    ),
                ):
                    renderer_module._preserve_published_generation(
                        staged,
                        target,
                    )

                rename.assert_called_once()
                self.assertEqual(target.read_bytes(), payload)
            finally:
                atomic_publish_module._retire_private_file(staged.claim)

    def test_isolation_keeps_committed_backup_when_public_is_recreated(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "artifact.json"
            old_payload = b"old public generation"
            concurrent_payload = b"concurrent public generation"
            target.write_bytes(old_payload)
            snapshot = renderer_module._capture_output_target(target)
            self.assertIsNotNone(snapshot)
            real_rename = renderer_module._rename_noreplace
            injected = False

            def move_recreate_then_report(
                source: Path,
                destination: Path,
            ) -> None:
                nonlocal injected
                if (
                    not injected
                    and Path(source).resolve(strict=False)
                    == target.resolve(strict=False)
                ):
                    injected = True
                    real_rename(source, destination)
                    target.write_bytes(concurrent_payload)
                    raise FileExistsError("injected post-commit report")
                real_rename(source, destination)

            backup = None
            try:
                with patch.object(
                    renderer_module,
                    "_rename_noreplace",
                    side_effect=move_recreate_then_report,
                ):
                    assert snapshot is not None
                    backup = renderer_module._isolate_output_target(
                        target,
                        snapshot,
                    )

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), concurrent_payload)
                self.assertEqual(backup.claim.path.read_bytes(), old_payload)
            finally:
                if backup is not None:
                    atomic_publish_module._retire_private_file(backup.claim)

    def test_withdrawn_cleanup_never_adopts_a_recovery_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = b"withdrawn transaction generation"
            staged = _sealed_stage(
                directory,
                label="withdrawn-cleanup",
                payload=payload,
            )
            preserved = directory / ".artifact.rollback-preserved"
            os.replace(staged.claim.path, preserved)
            parked_owned = directory / "parked-withdrawn-generation"
            racer_payload = b"recovery-name racer"
            real_sha256 = renderer_module.sha256_plain_file
            injected = False

            def replace_before_cleanup_capture(path):
                nonlocal injected
                if not injected and Path(path) == preserved:
                    injected = True
                    os.replace(preserved, parked_owned)
                    preserved.write_bytes(racer_payload)
                return real_sha256(path)

            try:
                with (
                    patch.object(
                        renderer_module,
                        "sha256_plain_file",
                        side_effect=replace_before_cleanup_capture,
                    ),
                    self.assertWarnsRegex(
                        RuntimeWarning,
                        "identity-bound cleanup was not completed",
                    ),
                ):
                    renderer_module._retire_withdrawn_published_generation(
                        staged,
                        preserved,
                    )

                self.assertTrue(injected)
                self.assertEqual(parked_owned.read_bytes(), payload)
                self.assertEqual(preserved.read_bytes(), racer_payload)
            finally:
                atomic_publish_module._retire_private_file(staged.claim)

    def test_relative_existing_target_can_be_republished(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            previous_cwd = Path.cwd()
            staged = None
            try:
                os.chdir(directory)
                target = Path("artifact.json")
                target.write_bytes(b"old relative generation")
                new_payload = b"new relative generation"
                staged = _sealed_stage(
                    directory,
                    label="relative-republish",
                    payload=new_payload,
                )

                renderer_module._publish_staged_artifacts(
                    ((staged, target),)
                )

                self.assertEqual(target.read_bytes(), new_payload)
            finally:
                os.chdir(previous_cwd)
                if staged is not None:
                    atomic_publish_module._retire_private_file(staged.claim)

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 paths are required")
    def test_existing_final_component_short_alias_is_republished_canonically(
        self,
    ) -> None:
        import ctypes

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "existing-render-generation-artifact.json"
            target.write_bytes(b"old canonical generation")
            buffer = ctypes.create_unicode_buffer(32_768)
            length = ctypes.windll.kernel32.GetShortPathNameW(
                str(target),
                buffer,
                len(buffer),
            )
            if not length or length >= len(buffer):
                self.skipTest("GetShortPathNameW did not return an alias")
            short_target = Path(buffer.value)
            if short_target.name.casefold() == target.name.casefold():
                self.skipTest("8.3 final-name generation is disabled")

            staged = _sealed_stage(
                directory,
                label="short-final-component",
                payload=b"new canonical generation",
            )
            try:
                renderer_module._publish_staged_artifacts(
                    ((staged, short_target),)
                )

                self.assertEqual(
                    target.read_bytes(),
                    b"new canonical generation",
                )
                published = renderer_module._capture_output_target(target)
                self.assertIsNotNone(published)
                assert published is not None
                self.assertEqual(published.identity.path, target.resolve())
                self.assertNotIn(
                    short_target.name.casefold(),
                    {
                        path.name.casefold()
                        for path in directory.iterdir()
                        if path.name.casefold() != target.name.casefold()
                    },
                )
                self.assertFalse(staged.claim.path.exists())
            finally:
                atomic_publish_module._retire_private_file(staged.claim)

    def test_relative_render_output_is_frozen_before_callback_changes_cwd(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requested_directory = root / "requested"
            changed_directory = root / "changed"
            requested_directory.mkdir()
            changed_directory.mkdir()
            performance = _write_tiny_performance(root)
            previous_cwd = Path.cwd()
            callback_called = False

            def change_cwd(_manifest) -> None:
                nonlocal callback_called
                callback_called = True
                os.chdir(changed_directory)

            try:
                os.chdir(requested_directory)
                with patch.object(
                    renderer_module,
                    "create_instrument",
                    return_value=_ConstantInstrument(),
                ):
                    result = renderer_module.render_to_wav_atomic(
                        INSTRUMENT_MANIFEST,
                        performance,
                        Path("relative.wav"),
                        _manifest_validator=change_cwd,
                    )
            finally:
                os.chdir(previous_cwd)

            expected = requested_directory / "relative.wav"
            self.assertTrue(callback_called)
            self.assertTrue(expected.exists())
            self.assertFalse((changed_directory / "relative.wav").exists())
            self.assertEqual(
                Path(result.license_sidecar_path).resolve(strict=True),
                expected.with_name(
                    f"{expected.name}.许可与署名.json"
                ).resolve(strict=True),
            )
            locks = list(requested_directory.glob(".tianlai-render-*.lock"))
            self.assertEqual(len(locks), 1)
            self.assertEqual(
                list(changed_directory.glob(".tianlai-render-*.lock")),
                [],
            )

    def test_relative_inputs_are_frozen_together_before_manifest_callback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requested_directory = root / "requested"
            changed_directory = root / "changed"
            requested_directory.mkdir()
            changed_directory.mkdir()
            (requested_directory / "manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (changed_directory / "manifest.json").write_text(
                "{}",
                encoding="utf-8",
            )
            for directory, sample_rate in (
                (requested_directory, 8_000),
                (changed_directory, 16_000),
            ):
                (directory / "events.json").write_text(
                    json.dumps(
                        {
                            "sample_rate": sample_rate,
                            "channels": 2,
                            "tail_seconds": 0.0,
                            "duration_seconds": 0.001,
                            "events": [],
                        }
                    ),
                    encoding="utf-8",
                )
            previous_cwd = Path.cwd()

            def change_cwd(_manifest) -> None:
                os.chdir(changed_directory)

            try:
                os.chdir(requested_directory)
                inputs = renderer_module._capture_single_render_inputs(
                    Path("manifest.json"),
                    Path("events.json"),
                    manifest_validator=change_cwd,
                )
            finally:
                os.chdir(previous_cwd)

            self.assertEqual(
                inputs.manifest.path,
                (requested_directory / "manifest.json").resolve(),
            )
            self.assertEqual(
                inputs.performance.path,
                (requested_directory / "events.json").resolve(),
            )
            self.assertEqual(inputs.parsed_performance.sample_rate, 8_000)

    def test_mutated_backup_does_not_withdraw_current_public_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first_target = directory / "first.json"
            second_target = directory / "second.json"
            first_old = b"first old generation"
            second_old = b"second old generation"
            first_new = b"first new generation"
            second_new = b"second new generation"
            first_target.write_bytes(first_old)
            second_target.write_bytes(second_old)
            first_stage = _sealed_stage(
                directory,
                label="first-backup-mutation",
                payload=first_new,
            )
            second_stage = _sealed_stage(
                directory,
                label="second-backup-mutation",
                payload=second_new,
            )
            real_replace = renderer_module._replace_published_file
            replace_calls = 0

            def mutate_backup_then_fail(sealed, destination: Path) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    real_replace(sealed, destination)
                    return
                first_backups = list(
                    directory.glob(".first.json.tianlai-backup-*")
                )
                self.assertEqual(len(first_backups), 1)
                first_backups[0].write_bytes(b"mutated rollback generation")
                raise OSError("injected second publication failure")

            try:
                with (
                    patch.object(
                        renderer_module,
                        "_replace_published_file",
                        side_effect=mutate_backup_then_fail,
                    ),
                    self.assertRaisesRegex(RuntimeError, "回滚不完整"),
                ):
                    renderer_module._publish_staged_artifacts(
                        (
                            (first_stage, first_target),
                            (second_stage, second_target),
                        )
                    )

                self.assertEqual(first_target.read_bytes(), first_new)
                self.assertEqual(second_target.read_bytes(), second_old)
            finally:
                atomic_publish_module._retire_private_file(first_stage.claim)
                atomic_publish_module._retire_private_file(second_stage.claim)

    def test_restore_leaves_a_public_replacement_after_verified_move(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "artifact.json"
            old_payload = b"sealed previous generation"
            concurrent_payload = b"concurrent public generation"
            backup = _sealed_stage(
                directory,
                label="rollback-backup",
                payload=old_payload,
            )
            parked_old = directory / "parked-restored-old"
            real_rename = renderer_module._rename_noreplace
            rename_calls = 0

            def restore_then_replace(source: Path, destination: Path) -> None:
                nonlocal rename_calls
                rename_calls += 1
                if rename_calls == 1:
                    self.assertEqual(source, backup.claim.path)
                    self.assertEqual(destination, target)
                    real_rename(source, destination)
                    os.replace(destination, parked_old)
                    destination.write_bytes(concurrent_payload)
                    return
                real_rename(source, destination)

            try:
                with (
                    patch.object(
                        renderer_module,
                        "_rename_noreplace",
                        side_effect=restore_then_replace,
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        "public entry was left untouched",
                    ),
                ):
                    renderer_module._restore_published_file(backup, target)

                self.assertEqual(rename_calls, 1)
                self.assertEqual(target.read_bytes(), concurrent_payload)
                self.assertEqual(parked_old.read_bytes(), old_payload)
            finally:
                atomic_publish_module._retire_private_file(backup.claim)

    def test_restore_post_commit_error_still_leaves_public_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "artifact.json"
            old_payload = b"sealed previous generation"
            concurrent_payload = b"concurrent public generation"
            backup = _sealed_stage(
                directory,
                label="rollback-backup-post-error",
                payload=old_payload,
            )
            parked_old = directory / "parked-restored-old"
            real_rename = renderer_module._rename_noreplace
            injected = False

            def restore_replace_then_raise(source: Path, destination: Path) -> None:
                nonlocal injected
                if not injected and source == backup.claim.path:
                    injected = True
                    real_rename(source, destination)
                    os.replace(destination, parked_old)
                    destination.write_bytes(concurrent_payload)
                    raise OSError("injected error after committed restore")
                real_rename(source, destination)

            try:
                with (
                    patch.object(
                        renderer_module,
                        "_rename_noreplace",
                        side_effect=restore_replace_then_raise,
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        "does not match its sealed backup",
                    ),
                ):
                    renderer_module._restore_published_file(backup, target)

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), concurrent_payload)
                self.assertEqual(parked_old.read_bytes(), old_payload)
            finally:
                atomic_publish_module._retire_private_file(backup.claim)

    def test_isolation_seal_failure_restores_the_old_public_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "artifact.json"
            old_payload = b"old public generation"
            target.write_bytes(old_payload)
            staged = _sealed_stage(
                directory,
                label="seal-failure-stage",
                payload=b"new generation",
            )
            real_seal = renderer_module._seal_private_file_claim
            injected = False

            def fail_backup_seal(claim, **kwargs):
                nonlocal injected
                if "tianlai-backup" in claim.path.name:
                    injected = True
                    raise OSError("injected rollback seal failure")
                return real_seal(claim, **kwargs)

            try:
                with (
                    patch.object(
                        renderer_module,
                        "_seal_private_file_claim",
                        side_effect=fail_backup_seal,
                    ),
                    self.assertRaisesRegex(
                        OSError,
                        "injected rollback seal failure",
                    ),
                ):
                    renderer_module._publish_staged_artifacts(
                        ((staged, target),)
                    )

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), old_payload)
                self.assertEqual(
                    list(directory.glob(".*.tianlai-backup-*")),
                    [],
                )
            finally:
                atomic_publish_module._retire_private_file(staged.claim)

    def test_public_replacement_inside_install_postcheck_is_left_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "artifact.json"
            good_payload = b"sealed stage generation"
            concurrent_payload = b"concurrent public generation"
            staged = _sealed_stage(
                directory,
                label="inner-install-stage",
                payload=good_payload,
            )
            parked_good = directory / "parked-good-stage"
            real_rename = renderer_module._rename_noreplace
            injected = False

            def install_then_replace(source: Path, destination: Path) -> None:
                nonlocal injected
                if not injected and source == staged.claim.path:
                    injected = True
                    real_rename(source, destination)
                    os.replace(destination, parked_good)
                    destination.write_bytes(concurrent_payload)
                    return
                real_rename(source, destination)

            try:
                with (
                    patch.object(
                        renderer_module,
                        "_rename_noreplace",
                        side_effect=install_then_replace,
                    ),
                    self.assertRaisesRegex(
                        RuntimeError,
                        "回滚不完整",
                    ),
                ):
                    renderer_module._publish_staged_artifacts(
                        ((staged, target),)
                    )

                self.assertTrue(injected)
                self.assertEqual(target.read_bytes(), concurrent_payload)
                self.assertEqual(parked_good.read_bytes(), good_payload)
            finally:
                atomic_publish_module._retire_private_file(staged.claim)

    def test_concurrent_replacement_of_first_publication_is_left_untouched(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first_target = directory / "first.json"
            second_target = directory / "second.json"
            first_old = b"first old generation"
            second_old = b"second old generation"
            first_new = b"first new generation"
            second_new = b"second new generation"
            sentinel = b"concurrent public owner"
            first_target.write_bytes(first_old)
            second_target.write_bytes(second_old)
            first_stage = _sealed_stage(
                directory,
                label="first",
                payload=first_new,
            )
            second_stage = _sealed_stage(
                directory,
                label="second",
                payload=second_new,
            )
            parked_first_new = directory / "parked-first-new"
            real_replace = renderer_module._replace_published_file
            replace_calls = 0

            def replace_first_then_lose_ownership(
                source: Path,
                destination: Path,
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    self.assertEqual(
                        destination,
                        first_target.resolve(strict=False),
                    )
                    real_replace(source, destination)
                    return
                self.assertEqual(
                    destination,
                    second_target.resolve(strict=False),
                )
                os.replace(first_target, parked_first_new)
                first_target.write_bytes(sentinel)
                raise OSError("injected second publication failure")

            try:
                with (
                    patch.object(
                        renderer_module,
                        "_replace_published_file",
                        side_effect=replace_first_then_lose_ownership,
                    ),
                    self.assertRaises(RuntimeError) as caught,
                ):
                    renderer_module._publish_staged_artifacts(
                        (
                            (first_stage, first_target),
                            (second_stage, second_target),
                        )
                    )

                self.assertEqual(replace_calls, 2)
                self.assertIn(
                    "public target was replaced by another writer",
                    str(caught.exception),
                )
                self.assertIsInstance(caught.exception.__cause__, OSError)
                self.assertIn(
                    "second publication failure",
                    str(caught.exception.__cause__),
                )
                self.assertEqual(
                    first_target.read_bytes(),
                    sentinel,
                    "rollback displaced a concurrent public generation",
                )
                self.assertEqual(second_target.read_bytes(), second_old)
                first_old_survivors = [
                    path
                    for path in directory.iterdir()
                    if path.is_file() and path.read_bytes() == first_old
                ]
                self.assertTrue(
                    first_old_survivors,
                    "rollback did not retain the first target's sealed backup",
                )
                self.assertNotIn(first_target, first_old_survivors)
                self.assertEqual(parked_first_new.read_bytes(), first_new)
            finally:
                atomic_publish_module._retire_private_file(first_stage.claim)
                atomic_publish_module._retire_private_file(second_stage.claim)

    def test_reserved_writer_never_destroys_a_racing_sentinel(self) -> None:
        reservations = (
            ("wav", 0),
            ("license-json", 1),
            ("attribution-text", 2),
            ("post-render-check", 3),
        )
        for member, reservation_index in reservations:
            with (
                self.subTest(member=member),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                target = directory / "transaction.wav"
                performance = _write_tiny_performance(directory)
                sentinel = f"racing sentinel for {member}".encode()
                parked_claim = directory / f"parked-owned-{member}"
                real_reserve = renderer_module._reserve_private_file
                reserve_count = 0
                raced_path: Path | None = None

                def reserve_then_replace(*args, **kwargs):
                    nonlocal reserve_count, raced_path
                    claim = real_reserve(*args, **kwargs)
                    current_index = reserve_count
                    reserve_count += 1
                    if current_index == reservation_index:
                        raced_path = claim.path
                        os.replace(claim.path, parked_claim)
                        claim.path.write_bytes(sentinel)
                    return claim

                with (
                    patch.object(
                        renderer_module,
                        "_reserve_private_file",
                        side_effect=reserve_then_replace,
                    ),
                    patch.object(
                        renderer_module,
                        "create_instrument",
                        return_value=_ConstantInstrument(),
                    ),
                    warnings.catch_warnings(),
                ):
                    warnings.simplefilter("ignore", RuntimeWarning)
                    try:
                        renderer_module.render_to_wav_atomic(
                            INSTRUMENT_MANIFEST,
                            performance,
                            target,
                        )
                    except Exception:
                        pass

                self.assertIsNotNone(raced_path)
                survivors = [
                    path
                    for path in directory.iterdir()
                    if path.is_file() and path.read_bytes() == sentinel
                ]
                self.assertTrue(
                    survivors,
                    f"{member} writer truncated or replaced the racing sentinel",
                )
                self.assertFalse(
                    any(path in _render_quartet(target) for path in survivors),
                    f"{member} racing sentinel was published as a public artifact",
                )

    def test_post_commit_replace_error_rolls_back_with_or_without_old_target(
        self,
    ) -> None:
        for existing in (False, True):
            with (
                self.subTest(existing=existing),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                target = directory / "artifact.json"
                staged_claim = renderer_module._reserve_private_file(
                    directory,
                    prefix=".artifact.",
                    suffix=".tianlai-stage",
                )
                atomic_publish_module._write_private_file_bytes(
                    staged_claim,
                    b"new transaction bytes",
                )
                staged = renderer_module._seal_private_file_claim(
                    staged_claim,
                    expected_sha256=hashlib.sha256(
                        b"new transaction bytes"
                    ).hexdigest(),
                )
                old_payload = b"previous transaction bytes"
                if existing:
                    target.write_bytes(old_payload)
                real_replace = renderer_module._replace_published_file

                def commit_then_raise(source: Path, destination: Path) -> None:
                    real_replace(source, destination)
                    raise OSError("injected error after committed replace")

                with patch.object(
                    renderer_module,
                    "_replace_published_file",
                    side_effect=commit_then_raise,
                ):
                    with self.assertRaisesRegex(
                        OSError,
                        "after committed replace",
                    ):
                        renderer_module._publish_staged_artifacts(
                            ((staged, target),)
                        )

                if existing:
                    self.assertEqual(target.read_bytes(), old_payload)
                else:
                    self.assertFalse(
                        target.exists(),
                        "a post-commit exception left a new target published",
                    )
                leaked_new = [
                    path
                    for path in directory.iterdir()
                    if path.is_file()
                    and path.read_bytes() == b"new transaction bytes"
                ]
                self.assertEqual(
                    leaked_new,
                    [],
                    "ordinary rollback retained the failed transaction bytes",
                )

    def test_verified_stage_replacement_is_never_published(self) -> None:
        members = (
            ("license-json", 0, 1),
            ("attribution-text", 1, 2),
            ("post-render-check", 2, 3),
            ("wav", 3, 0),
        )
        for member, staged_index, public_index in members:
            with (
                self.subTest(member=member),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                target = directory / "transaction.wav"
                performance = _write_tiny_performance(directory)
                previous = _seed_old_quartet(target)
                selected_public = _render_quartet(target)[public_index]
                attacker_payload = f"unverified replacement for {member}".encode()
                parked_verified = directory / f"parked-verified-{member}"
                real_publish = renderer_module._publish_staged_artifacts
                swapped = False

                def swap_after_validation(staged_targets):
                    nonlocal swapped
                    sealed, destination = staged_targets[staged_index]
                    self.assertEqual(destination, selected_public)
                    staged = sealed.claim.path
                    os.replace(staged, parked_verified)
                    staged.write_bytes(attacker_payload)
                    swapped = True
                    return real_publish(staged_targets)

                failure: Exception | None = None
                with (
                    patch.object(
                        renderer_module,
                        "create_instrument",
                        return_value=_ConstantInstrument(),
                    ),
                    patch.object(
                        renderer_module,
                        "_publish_staged_artifacts",
                        side_effect=swap_after_validation,
                    ),
                    warnings.catch_warnings(),
                ):
                    warnings.simplefilter("ignore", RuntimeWarning)
                    try:
                        renderer_module.render_to_wav_atomic(
                            INSTRUMENT_MANIFEST,
                            performance,
                            target,
                        )
                    except Exception as error:
                        failure = error

                self.assertTrue(swapped)
                self.assertIsNotNone(
                    failure,
                    f"unverified {member} bytes were accepted and published: "
                    f"{selected_public.read_bytes()!r}",
                )
                for path, payload in previous.items():
                    self.assertEqual(path.read_bytes(), payload)

    def test_partial_sidecar_write_failure_leaves_no_owned_stage(self) -> None:
        from tianlai import license_sidecar as sidecar_module

        for failure_point in ("before-second-write", "after-second-write"):
            with (
                self.subTest(failure_point=failure_point),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                target = directory / "transaction.wav"
                performance = _write_tiny_performance(directory)
                previous = _seed_old_quartet(target)
                real_write = sidecar_module._write_private_file_bytes
                write_count = 0

                def fail_second_write(claim, payload):
                    nonlocal write_count
                    write_count += 1
                    if write_count == 2:
                        if failure_point == "after-second-write":
                            real_write(claim, payload)
                        raise OSError(f"injected {failure_point}")
                    return real_write(claim, payload)

                with (
                    patch.object(
                        renderer_module,
                        "create_instrument",
                        return_value=_ConstantInstrument(),
                    ),
                    patch.object(
                        sidecar_module,
                        "_write_private_file_bytes",
                        side_effect=fail_second_write,
                    ),
                    self.assertRaisesRegex(OSError, failure_point),
                ):
                    renderer_module.render_to_wav_atomic(
                        INSTRUMENT_MANIFEST,
                        performance,
                        target,
                    )

                self.assertEqual(write_count, 2)
                for path, payload in previous.items():
                    self.assertEqual(path.read_bytes(), payload)
                leaked = [
                    path
                    for path in directory.iterdir()
                    if (
                        ".retired." in path.name
                        or "tianlai-stage" in path.name
                        or path.name.endswith(".tianlai-part")
                        or path.name.endswith(".tmp")
                    )
                ]
                self.assertEqual(leaked, [])

    def test_post_report_write_then_error_leaves_no_owned_stage(self) -> None:
        from tianlai import post_render_check as post_check_module

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "transaction.wav"
            performance = _write_tiny_performance(directory)
            previous = _seed_old_quartet(target)
            real_write = post_check_module._write_private_file_bytes

            def write_then_fail(claim, payload):
                real_write(claim, payload)
                raise OSError("injected report post-write failure")

            with (
                patch.object(
                    renderer_module,
                    "create_instrument",
                    return_value=_ConstantInstrument(),
                ),
                patch.object(
                    post_check_module,
                    "_write_private_file_bytes",
                    side_effect=write_then_fail,
                ),
                self.assertRaisesRegex(OSError, "post-write failure"),
            ):
                renderer_module.render_to_wav_atomic(
                    INSTRUMENT_MANIFEST,
                    performance,
                    target,
                )

            for path, payload in previous.items():
                self.assertEqual(path.read_bytes(), payload)
            leaked = [
                path
                for path in directory.iterdir()
                if (
                    ".retired." in path.name
                    or "tianlai-stage" in path.name
                    or path.name.endswith(".tianlai-part")
                    or path.name.endswith(".tmp")
                )
            ]
            self.assertEqual(leaked, [])


if __name__ == "__main__":
    unittest.main()
