from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tianlai import atomic_publish
from tianlai import cli as cli_module


def _same_path(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    """Compare test-observed paths across ordinary Windows 8.3 aliases."""

    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


class AtomicPublishTests(unittest.TestCase):
    def test_reservation_never_recloses_an_ambiguous_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_close = os.close
            close_calls: list[int] = []

            def close_then_report(descriptor: int) -> None:
                close_calls.append(descriptor)
                real_close(descriptor)
                raise OSError("injected error after descriptor close")

            with (
                mock.patch.object(
                    atomic_publish.os,
                    "close",
                    side_effect=close_then_report,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "injected error after descriptor close",
                ),
            ):
                atomic_publish._reserve_private_file(
                    root,
                    prefix=".ambiguous-close.",
                    suffix=".tmp",
                )

            self.assertEqual(len(close_calls), 1)
            self.assertEqual(list(root.iterdir()), [])

    def test_post_write_failure_never_closes_a_recycled_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unrelated = root / "unrelated.bin"
            unrelated.write_bytes(b"must remain open")
            recycled_descriptor: int | None = None

            def open_unrelated_then_fail(_path):
                nonlocal recycled_descriptor
                recycled_descriptor = os.open(
                    unrelated,
                    os.O_RDONLY | getattr(os, "O_BINARY", 0),
                )
                raise OSError("injected post-write capture failure")

            with (
                mock.patch.object(
                    atomic_publish,
                    "_capture_file",
                    side_effect=open_unrelated_then_fail,
                ),
                self.assertRaisesRegex(
                    OSError,
                    "injected post-write capture failure",
                ),
            ):
                atomic_publish._write_private_payload(
                    atomic_publish.ensure_plain_directory_tree(root),
                    "document.json",
                    b"payload",
                )

            self.assertIsNotNone(recycled_descriptor)
            assert recycled_descriptor is not None
            try:
                self.assertEqual(
                    os.fstat(recycled_descriptor).st_size,
                    len(b"must remain open"),
                )
            finally:
                os.close(recycled_descriptor)

    def test_pretty_json_bytes_are_sorted_lf_utf8_and_finite(self) -> None:
        payload = atomic_publish._pretty_json_bytes(
            {"z": 1, "a": "天籁"}
        )

        self.assertEqual(
            payload,
            '{\n  "a": "天籁",\n  "z": 1\n}\n'.encode("utf-8"),
        )
        self.assertNotIn(b"\r", payload)
        with self.assertRaises(ValueError):
            atomic_publish._pretty_json_bytes({"bad": math.nan})

    def test_cli_json_writer_uses_the_same_cross_platform_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "document.json"
            value = {"z": 1, "a": "天籁"}

            cli_module._write_json_atomic(path, value)

            self.assertEqual(
                path.read_bytes(),
                atomic_publish._pretty_json_bytes(value),
            )

    def test_cli_overwrite_does_not_mislabel_a_publish_race(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "document.json"
            with (
                mock.patch.object(
                    cli_module,
                    "_publish_bytes_atomic",
                    side_effect=FileExistsError("concurrent writer"),
                ),
                self.assertRaisesRegex(FileExistsError, "concurrent writer"),
            ):
                cli_module._write_json_atomic(path, {"a": 1}, overwrite=True)

    def test_no_overwrite_never_clobbers_a_target_created_at_publish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            sentinel = b"concurrent writer"
            real_rename = atomic_publish._rename_noreplace
            raced = False

            def race(source, destination):
                nonlocal raced
                destination_path = Path(destination)
                if _same_path(destination_path, target) and not raced:
                    raced = True
                    target.write_bytes(sentinel)
                return real_rename(source, destination)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_rename_noreplace",
                    side_effect=race,
                ),
                self.assertRaises(FileExistsError),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=False,
                )

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), sentinel)

    def test_overwrite_replaces_one_plain_file_without_private_residue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            target.write_bytes(b"old")

            atomic_publish._publish_bytes_atomic(
                target,
                b"new",
                overwrite=True,
            )

            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(
                [
                    entry
                    for entry in root.iterdir()
                    if entry != target
                    and not entry.name.startswith(".tianlai-render-")
                ],
                [],
            )

    def test_overwrite_detects_target_swap_and_preserves_racing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            parked_old = root / "parked-old.json"
            target.write_bytes(b"old")
            sentinel = b"concurrent writer"
            real_rename = atomic_publish._rename_noreplace
            raced = False

            def race(source, destination):
                nonlocal raced
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not raced
                    and _same_path(source_path, target)
                    and "publish-backup" in destination_path.name
                ):
                    raced = True
                    os.rename(target, parked_old)
                    target.write_bytes(sentinel)
                return real_rename(source, destination)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_rename_noreplace",
                    side_effect=race,
                ),
                self.assertRaisesRegex(OSError, "changed before isolation"),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"new",
                    overwrite=True,
                )

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), sentinel)
            self.assertEqual(parked_old.read_bytes(), b"old")

    def test_overwrite_publish_race_does_not_clobber_racer_or_old_backup(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            target.write_bytes(b"old")
            sentinel = b"concurrent writer"
            real_rename = atomic_publish._rename_noreplace
            raced = False

            def race(source, destination):
                nonlocal raced
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not raced
                    and _same_path(destination_path, target)
                    and source_path.name.endswith(".publish.tmp")
                ):
                    raced = True
                    target.write_bytes(sentinel)
                return real_rename(source, destination)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_rename_noreplace",
                    side_effect=race,
                ),
                self.assertRaises(FileExistsError) as caught,
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"new",
                    overwrite=True,
                )

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), sentinel)
            backups = list(root.glob(".*.publish-backup.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), b"old")
            self.assertTrue(
                any(
                    "retained for recovery" in note
                    for note in getattr(caught.exception, "__notes__", ())
                )
            )

    def test_staging_cleanup_never_unlinks_a_racing_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            parked_payload = root / "parked-payload.tmp"
            sentinel = b"racing temporary entry"
            real_rename = atomic_publish._rename_noreplace
            raced = False

            def fail_publish_then_replace_temp(source, destination):
                nonlocal raced
                source_path = Path(source)
                destination_path = Path(destination)
                if _same_path(destination_path, target) and not raced:
                    raced = True
                    os.rename(source_path, parked_payload)
                    source_path.write_bytes(sentinel)
                    raise PermissionError("injected publication failure")
                return real_rename(source, destination)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_rename_noreplace",
                    side_effect=fail_publish_then_replace_temp,
                ),
                self.assertRaisesRegex(
                    PermissionError,
                    "injected publication failure",
                ),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=False,
                )

            self.assertTrue(raced)
            self.assertEqual(parked_payload.read_bytes(), b"ours")
            preserved = list(root.glob(".*.retired.*"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(preserved[0].read_bytes(), sentinel)
            self.assertFalse(target.exists())

    def test_private_file_claim_retires_its_owned_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".claimed-stage.",
                suffix=".tmp",
            )
            claimed_path = claim.path
            claimed_path.write_bytes(b"owned staging bytes")

            preserved = atomic_publish._retire_private_file(claim)

            self.assertIsNone(preserved)
            self.assertFalse(os.path.lexists(claimed_path))
            self.assertEqual(list(root.glob(".*.retired.*")), [])

    def test_private_file_writer_refuses_a_racing_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".claimed-stage.",
                suffix=".tmp",
            )
            parked_owned = root / "parked-owned.tmp"
            os.replace(claim.path, parked_owned)
            sentinel = b"racing replacement must not be truncated"
            claim.path.write_bytes(sentinel)

            with self.assertRaisesRegex(OSError, "changed before writer"):
                atomic_publish._write_private_file_bytes(
                    claim,
                    b"transaction payload",
                )

            self.assertEqual(claim.path.read_bytes(), sentinel)
            with self.assertWarnsRegex(RuntimeWarning, "preserved"):
                preserved = atomic_publish._retire_private_file(claim)
            self.assertIsNotNone(preserved)
            assert preserved is not None
            self.assertEqual(preserved.read_bytes(), sentinel)
            self.assertEqual(parked_owned.read_bytes(), b"")

    def test_private_file_claim_writes_and_seals_one_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".claimed-stage.",
                suffix=".tmp",
            )
            payload = b"one verified private generation"

            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )

            self.assertEqual(sealed.sha256, hashlib.sha256(payload).hexdigest())
            with self.assertRaisesRegex(RuntimeError, "sealed"):
                atomic_publish._write_private_file_bytes(claim, b"mutation")
            self.assertIsNone(atomic_publish._retire_private_file(claim))
            self.assertFalse(os.path.lexists(claim.path))

    def test_private_file_claim_rebinds_one_verified_atomic_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".claimed-stage.",
                suffix=".tmp",
            )
            payload = b"verified atomic replacement"
            replacement = root / "replacement.tmp"
            replacement.write_bytes(payload)
            os.replace(replacement, claim.path)

            rebound = atomic_publish._rebind_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            preserved = atomic_publish._retire_private_file(rebound)

            self.assertNotEqual(rebound.file_key, claim.file_key)
            self.assertIsNone(preserved)
            self.assertFalse(os.path.lexists(claim.path))

    def test_private_file_claim_retires_a_restored_backup_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".claimed-backup.",
                suffix=".tmp",
            )
            payload = b"previous public generation"
            claim.path.write_bytes(payload)
            restored = root / "restored.json"
            os.link(claim.path, restored)

            preserved = atomic_publish._retire_private_file(
                claim,
                allow_additional_links=True,
            )

            self.assertIsNone(preserved)
            self.assertFalse(os.path.lexists(claim.path))
            self.assertEqual(restored.read_bytes(), payload)

    def test_private_file_claim_preserves_a_racing_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".claimed-stage.",
                suffix=".tmp",
            )
            claim.path.write_bytes(b"owned staging bytes")
            parked_owned = root / "parked-owned.tmp"
            os.rename(claim.path, parked_owned)
            sentinel = b"racing replacement must survive"
            claim.path.write_bytes(sentinel)

            with self.assertWarnsRegex(
                RuntimeWarning,
                "replacement preserved",
            ):
                preserved = atomic_publish._retire_private_file(claim)

            self.assertEqual(parked_owned.read_bytes(), b"owned staging bytes")
            self.assertIsNotNone(preserved)
            assert preserved is not None
            self.assertEqual(preserved.read_bytes(), sentinel)
            self.assertFalse(claim.path.exists())

    def test_private_file_claim_preserves_when_identity_cannot_be_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".claimed-stage.",
                suffix=".tmp",
            )
            payload = b"owned but temporarily unverifiable"
            claim.path.write_bytes(payload)
            real_file_key = atomic_publish._path_file_key

            def fail_quarantine_capture(path: Path, **kwargs):
                if ".retired." in path.name:
                    raise PermissionError("injected identity failure")
                return real_file_key(path, **kwargs)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_path_file_key",
                    side_effect=fail_quarantine_capture,
                ),
                self.assertWarnsRegex(
                    RuntimeWarning,
                    "injected identity failure",
                ),
            ):
                preserved = atomic_publish._retire_private_file(claim)

            self.assertIsNotNone(preserved)
            assert preserved is not None
            self.assertEqual(preserved.read_bytes(), payload)
            self.assertFalse(claim.path.exists())

    def test_capture_failure_does_not_rebind_and_delete_a_racing_temp(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            parked_payload = root / "parked-payload.tmp"
            sentinel = b"racing temporary entry"
            real_capture = atomic_publish._capture_file
            injected = False

            def replace_on_first_capture(path: Path):
                nonlocal injected
                if not injected and path.name.endswith(".publish.tmp"):
                    injected = True
                    os.rename(path, parked_payload)
                    path.write_bytes(sentinel)
                    raise PermissionError("injected capture failure")
                return real_capture(path)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_capture_file",
                    side_effect=replace_on_first_capture,
                ),
                self.assertRaisesRegex(
                    PermissionError,
                    "injected capture failure",
                ),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=False,
                )

            self.assertTrue(injected)
            self.assertEqual(parked_payload.read_bytes(), b"ours")
            preserved = list(root.glob(".*.retired.*"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(preserved[0].read_bytes(), sentinel)
            self.assertFalse(target.exists())

    def test_target_appearing_during_staging_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            sentinel = b"concurrent writer"
            real_write = atomic_publish._write_private_payload

            def appear(*args, **kwargs):
                result = real_write(*args, **kwargs)
                target.write_bytes(sentinel)
                return result

            with (
                mock.patch.object(
                    atomic_publish,
                    "_write_private_payload",
                    side_effect=appear,
                ),
                self.assertRaises(FileExistsError),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=True,
                )

            self.assertEqual(target.read_bytes(), sentinel)

    def test_target_swapped_during_staging_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            parked_old = root / "parked-old.json"
            target.write_bytes(b"old")
            sentinel = b"concurrent writer"
            real_write = atomic_publish._write_private_payload

            def swap(*args, **kwargs):
                result = real_write(*args, **kwargs)
                os.rename(target, parked_old)
                target.write_bytes(sentinel)
                return result

            with (
                mock.patch.object(
                    atomic_publish,
                    "_write_private_payload",
                    side_effect=swap,
                ),
                self.assertRaisesRegex(OSError, "changed while staging"),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=True,
                )

            self.assertEqual(target.read_bytes(), sentinel)
            self.assertEqual(parked_old.read_bytes(), b"old")

    def test_failed_postcheck_withdraws_a_mutated_first_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            real_capture_moved = atomic_publish._capture_moved_file
            injected = False

            def mutate_then_check(path, expected_identity, expected_digest, *, message):
                nonlocal injected
                if "published file changed" in message and not injected:
                    injected = True
                    Path(path).write_bytes(b"MUTATED")
                return real_capture_moved(
                    path,
                    expected_identity,
                    expected_digest,
                    message=message,
                )

            with (
                mock.patch.object(
                    atomic_publish,
                    "_capture_moved_file",
                    side_effect=mutate_then_check,
                ),
                self.assertRaisesRegex(OSError, "published file changed") as caught,
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=False,
                )

            self.assertTrue(injected)
            self.assertFalse(target.exists())
            recoveries = list(root.glob(".*.publish-recovery.*"))
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_bytes(), b"MUTATED")
            self.assertTrue(
                any(
                    str(recoveries[0].resolve(strict=False)) in note
                    for note in getattr(caught.exception, "__notes__", ())
                )
            )

    def test_failed_postcheck_restores_old_and_retains_mutated_new(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            target.write_bytes(b"OLD")
            real_capture_moved = atomic_publish._capture_moved_file
            injected = False

            def mutate_then_check(path, expected_identity, expected_digest, *, message):
                nonlocal injected
                if "published file changed" in message and not injected:
                    injected = True
                    Path(path).write_bytes(b"MUTATED")
                return real_capture_moved(
                    path,
                    expected_identity,
                    expected_digest,
                    message=message,
                )

            with (
                mock.patch.object(
                    atomic_publish,
                    "_capture_moved_file",
                    side_effect=mutate_then_check,
                ),
                self.assertRaisesRegex(OSError, "published file changed"),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=True,
                )

            self.assertTrue(injected)
            self.assertEqual(target.read_bytes(), b"OLD")
            recoveries = list(root.glob(".*.publish-recovery.*"))
            self.assertEqual(len(recoveries), 1)
            self.assertEqual(recoveries[0].read_bytes(), b"MUTATED")

    def test_withdrawal_restores_a_target_swapped_after_identity_check(self) -> None:
        for old_payload in (None, b"OLD"):
            with self.subTest(overwrite=old_payload is not None):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    target = root / "document.json"
                    parked_new = root / "parked-mutated.json"
                    if old_payload is not None:
                        target.write_bytes(old_payload)
                    sentinel = b"CONCURRENT"
                    real_capture_moved = atomic_publish._capture_moved_file
                    real_rename = atomic_publish._rename_noreplace
                    postcheck_injected = False
                    withdrawal_swapped = False

                    def mutate_then_check(
                        path,
                        expected_identity,
                        expected_digest,
                        *,
                        message,
                    ):
                        nonlocal postcheck_injected
                        if "published file changed" in message and not postcheck_injected:
                            postcheck_injected = True
                            Path(path).write_bytes(b"MUTATED")
                        return real_capture_moved(
                            path,
                            expected_identity,
                            expected_digest,
                            message=message,
                        )

                    def swap_during_withdrawal(source, destination):
                        nonlocal withdrawal_swapped
                        source_path = Path(source)
                        destination_path = Path(destination)
                        if (
                            not withdrawal_swapped
                            and _same_path(source_path, target)
                            and "publish-recovery" in destination_path.name
                        ):
                            withdrawal_swapped = True
                            os.rename(target, parked_new)
                            target.write_bytes(sentinel)
                        return real_rename(source_path, destination_path)

                    with (
                        mock.patch.object(
                            atomic_publish,
                            "_capture_moved_file",
                            side_effect=mutate_then_check,
                        ),
                        mock.patch.object(
                            atomic_publish,
                            "_rename_noreplace",
                            side_effect=swap_during_withdrawal,
                        ),
                        self.assertRaisesRegex(OSError, "published file changed"),
                    ):
                        atomic_publish._publish_bytes_atomic(
                            target,
                            b"ours",
                            overwrite=old_payload is not None,
                        )

                    self.assertTrue(postcheck_injected and withdrawal_swapped)
                    self.assertEqual(target.read_bytes(), sentinel)
                    self.assertEqual(parked_new.read_bytes(), b"MUTATED")
                    self.assertEqual(list(root.glob(".*.publish-recovery.*")), [])
                    backups = list(root.glob(".*.publish-backup.*"))
                    if old_payload is None:
                        self.assertEqual(backups, [])
                    else:
                        self.assertEqual(len(backups), 1)
                        self.assertEqual(backups[0].read_bytes(), old_payload)

    def test_publish_move_then_error_is_detected_and_rolled_back(self) -> None:
        for old_payload in (None, b"OLD"):
            with self.subTest(overwrite=old_payload is not None):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    target = root / "document.json"
                    if old_payload is not None:
                        target.write_bytes(old_payload)
                    real_rename = atomic_publish._rename_noreplace
                    injected = False

                    def move_then_error(source, destination):
                        nonlocal injected
                        source_path = Path(source)
                        destination_path = Path(destination)
                        if (
                            not injected
                            and source_path.name.endswith(".publish.tmp")
                            and _same_path(destination_path, target)
                        ):
                            injected = True
                            real_rename(source_path, destination_path)
                            raise PermissionError("move committed before error")
                        return real_rename(source_path, destination_path)

                    with (
                        mock.patch.object(
                            atomic_publish,
                            "_rename_noreplace",
                            side_effect=move_then_error,
                        ),
                        self.assertRaisesRegex(
                            PermissionError,
                            "move committed before error",
                        ),
                    ):
                        atomic_publish._publish_bytes_atomic(
                            target,
                            b"ours",
                            overwrite=old_payload is not None,
                        )

                    self.assertTrue(injected)
                    if old_payload is None:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(target.read_bytes(), old_payload)
                    recoveries = list(root.glob(".*.publish-recovery.*"))
                    self.assertEqual(len(recoveries), 1)
                    self.assertEqual(recoveries[0].read_bytes(), b"ours")

    def test_backup_move_then_error_restores_the_captured_old_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            target.write_bytes(b"OLD")
            real_rename = atomic_publish._rename_noreplace
            injected = False

            def move_then_error(source, destination):
                nonlocal injected
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    not injected
                    and _same_path(source_path, target)
                    and "publish-backup" in destination_path.name
                ):
                    injected = True
                    real_rename(source_path, destination_path)
                    raise PermissionError("backup move committed before error")
                return real_rename(source_path, destination_path)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_rename_noreplace",
                    side_effect=move_then_error,
                ),
                self.assertRaisesRegex(
                    PermissionError,
                    "backup move committed before error",
                ),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=True,
                )

            self.assertTrue(injected)
            self.assertEqual(target.read_bytes(), b"OLD")
            self.assertEqual(list(root.glob(".*.publish-backup.*")), [])
            self.assertEqual(list(root.glob(".*.publish.tmp")), [])

    def test_publish_move_then_error_and_inspection_failure_still_withdraws(
        self,
    ) -> None:
        for old_payload in (None, b"OLD"):
            with self.subTest(overwrite=old_payload is not None):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    target = root / "document.json"
                    if old_payload is not None:
                        target.write_bytes(old_payload)
                    real_rename = atomic_publish._rename_noreplace
                    real_capture = atomic_publish._capture_file
                    moved = False
                    inspect_failed = False

                    def move_then_error(source, destination):
                        nonlocal moved
                        source_path = Path(source)
                        destination_path = Path(destination)
                        if (
                            not moved
                            and source_path.name.endswith(".publish.tmp")
                            and _same_path(destination_path, target)
                        ):
                            moved = True
                            real_rename(source_path, destination_path)
                            raise PermissionError("MOVE THEN ERROR")
                        return real_rename(source_path, destination_path)

                    def fail_first_target_inspection(path):
                        nonlocal inspect_failed
                        candidate = Path(path)
                        if (
                            moved
                            and _same_path(candidate, target)
                            and not inspect_failed
                        ):
                            inspect_failed = True
                            raise OSError("INSPECT FAILURE")
                        return real_capture(candidate)

                    with (
                        mock.patch.object(
                            atomic_publish,
                            "_rename_noreplace",
                            side_effect=move_then_error,
                        ),
                        mock.patch.object(
                            atomic_publish,
                            "_capture_file",
                            side_effect=fail_first_target_inspection,
                        ),
                        self.assertRaisesRegex(PermissionError, "MOVE THEN ERROR"),
                    ):
                        atomic_publish._publish_bytes_atomic(
                            target,
                            b"ours",
                            overwrite=old_payload is not None,
                        )

                    self.assertTrue(moved and inspect_failed)
                    if old_payload is None:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(target.read_bytes(), old_payload)
                    recovery = list(root.glob(".*.publish-recovery.*"))
                    self.assertEqual(len(recovery), 1)
                    self.assertEqual(recovery[0].read_bytes(), b"ours")

    def test_backup_move_then_error_and_inspection_failure_restores_old(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            target.write_bytes(b"OLD")
            real_rename = atomic_publish._rename_noreplace
            real_capture = atomic_publish._capture_file
            backup_path: Path | None = None
            inspect_failed = False

            def move_then_error(source, destination):
                nonlocal backup_path
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    _same_path(source_path, target)
                    and "publish-backup" in destination_path.name
                ):
                    backup_path = destination_path
                    real_rename(source_path, destination_path)
                    raise PermissionError("BACKUP MOVE THEN ERROR")
                return real_rename(source_path, destination_path)

            def fail_backup_inspection(path):
                nonlocal inspect_failed
                candidate = Path(path)
                if (
                    backup_path is not None
                    and _same_path(candidate, backup_path)
                    and not inspect_failed
                ):
                    inspect_failed = True
                    raise OSError("BACKUP INSPECT FAILURE")
                return real_capture(candidate)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_rename_noreplace",
                    side_effect=move_then_error,
                ),
                mock.patch.object(
                    atomic_publish,
                    "_capture_file",
                    side_effect=fail_backup_inspection,
                ),
                self.assertRaisesRegex(PermissionError, "BACKUP MOVE THEN ERROR"),
            ):
                atomic_publish._publish_bytes_atomic(
                    target,
                    b"ours",
                    overwrite=True,
                )

            self.assertTrue(inspect_failed)
            self.assertEqual(target.read_bytes(), b"OLD")

    def test_raced_recovery_name_does_not_hide_failed_publication(self) -> None:
        for old_payload in (None, b"OLD"):
            with self.subTest(overwrite=old_payload is not None):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    target = root / "document.json"
                    if old_payload is not None:
                        target.write_bytes(old_payload)
                    real_capture_moved = atomic_publish._capture_moved_file
                    real_rename = atomic_publish._rename_noreplace
                    injected = False
                    raced = False

                    def mutate_then_check(path, expected_identity, expected_digest, *, message):
                        nonlocal injected
                        if "published file changed" in message and not injected:
                            injected = True
                            Path(path).write_bytes(b"MUTATED")
                        return real_capture_moved(
                            path,
                            expected_identity,
                            expected_digest,
                            message=message,
                        )

                    def occupy_first_recovery(source, destination):
                        nonlocal raced
                        destination_path = Path(destination)
                        if not raced and "publish-recovery" in destination_path.name:
                            raced = True
                            destination_path.write_bytes(b"RACER")
                        return real_rename(source, destination)

                    with (
                        mock.patch.object(
                            atomic_publish,
                            "_capture_moved_file",
                            side_effect=mutate_then_check,
                        ),
                        mock.patch.object(
                            atomic_publish,
                            "_rename_noreplace",
                            side_effect=occupy_first_recovery,
                        ),
                        self.assertRaisesRegex(OSError, "published file changed"),
                    ):
                        atomic_publish._publish_bytes_atomic(
                            target,
                            b"ours",
                            overwrite=old_payload is not None,
                        )

                    self.assertTrue(injected and raced)
                    racer_entries = [
                        entry
                        for entry in root.glob(".*.publish-recovery.*")
                        if entry.read_bytes() == b"RACER"
                    ]
                    self.assertEqual(len(racer_entries), 1)
                    failed_entries = [
                        entry
                        for entry in root.glob(".*.publish-recovery.*")
                        if entry.read_bytes() == b"MUTATED"
                    ]
                    self.assertEqual(len(failed_entries), 1)
                    if old_payload is None:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(target.read_bytes(), old_payload)

    def test_sealed_private_file_relocation_advances_the_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".stage.",
                suffix=".tmp",
            )
            payload = b"verified private generation"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )

            relocated = atomic_publish._relocate_sealed_private_file(
                sealed,
                stem="publish-transfer",
            )

            self.assertNotEqual(relocated.claim.path, claim.path)
            self.assertFalse(os.path.lexists(claim.path))
            self.assertEqual(relocated.claim.path.read_bytes(), payload)
            with self.assertRaisesRegex(RuntimeError, "stale"):
                atomic_publish._retire_private_file(claim)
            self.assertIsNone(
                atomic_publish._retire_private_file(relocated.claim)
            )

    def test_relocation_accepts_a_move_then_reported_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".stage.",
                suffix=".tmp",
            )
            payload = b"committed relocation"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            real_rename = atomic_publish._rename_noreplace
            injected = False

            def move_then_raise(source, destination):
                nonlocal injected
                real_rename(source, destination)
                if not injected:
                    injected = True
                    raise OSError("injected error after relocation")

            with mock.patch.object(
                atomic_publish,
                "_rename_noreplace",
                side_effect=move_then_raise,
            ):
                relocated = atomic_publish._relocate_sealed_private_file(
                    sealed,
                    stem="publish-transfer",
                )

            self.assertTrue(injected)
            self.assertEqual(relocated.claim.path.read_bytes(), payload)
            self.assertIsNone(
                atomic_publish._retire_private_file(relocated.claim)
            )

    def test_relocation_keeps_committed_destination_when_source_reappears(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".stage.",
                suffix=".tmp",
            )
            payload = b"committed relocation"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            real_rename = atomic_publish._rename_noreplace
            sentinel = b"recreated source entry"
            injected = False

            def move_recreate_then_report(source, destination):
                nonlocal injected
                real_rename(source, destination)
                if not injected:
                    injected = True
                    Path(source).write_bytes(sentinel)
                    raise FileExistsError("injected post-commit report")

            with mock.patch.object(
                atomic_publish,
                "_rename_noreplace",
                side_effect=move_recreate_then_report,
            ):
                relocated = atomic_publish._relocate_sealed_private_file(
                    sealed,
                    stem="publish-transfer",
                )

            self.assertTrue(injected)
            self.assertEqual(relocated.claim.path.read_bytes(), payload)
            self.assertEqual(claim.path.read_bytes(), sentinel)
            self.assertIsNone(
                atomic_publish._retire_private_file(relocated.claim)
            )

    def test_relocation_rejects_a_replaced_source_generation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".stage.",
                suffix=".tmp",
            )
            payload = b"sealed source"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            parked = root / "parked-sealed.tmp"
            os.replace(claim.path, parked)
            sentinel = b"source-path racer"
            claim.path.write_bytes(sentinel)

            with self.assertRaisesRegex(OSError, "changed before relocation"):
                atomic_publish._relocate_sealed_private_file(
                    sealed,
                    stem="publish-transfer",
                )

            self.assertEqual(parked.read_bytes(), payload)
            self.assertEqual(claim.path.read_bytes(), sentinel)

    def test_relocation_never_rebinds_a_destination_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".stage.",
                suffix=".tmp",
            )
            payload = b"sealed source"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            parked = root / "parked-relocated.tmp"
            sentinel = b"destination-path racer"
            real_capture = atomic_publish._capture_file
            injected = False

            def replace_relocated_destination(path: Path):
                nonlocal injected
                if not injected and "publish-transfer" in path.name:
                    injected = True
                    os.replace(path, parked)
                    path.write_bytes(sentinel)
                return real_capture(path)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_capture_file",
                    side_effect=replace_relocated_destination,
                ),
                self.assertWarnsRegex(
                    RuntimeWarning,
                    "no longer matches its sealed generation",
                ),
                self.assertRaisesRegex(OSError, "not the sealed generation"),
            ):
                atomic_publish._relocate_sealed_private_file(
                    sealed,
                    stem="publish-transfer",
                )

            self.assertTrue(injected)
            self.assertEqual(parked.read_bytes(), payload)
            racers = [
                path
                for path in root.iterdir()
                if path.is_file() and path.read_bytes() == sentinel
            ]
            self.assertEqual(len(racers), 1)

    def test_private_writer_close_error_always_releases_active_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".stage.",
                suffix=".tmp",
            )
            real_fdopen = os.fdopen

            class _CloseAfterCommit:
                def __init__(self, raw):
                    self.raw = raw

                def __getattr__(self, name):
                    return getattr(self.raw, name)

                def close(self):
                    self.raw.close()
                    raise OSError("injected close failure")

            def fail_after_close(descriptor, mode):
                return _CloseAfterCommit(real_fdopen(descriptor, mode))

            with (
                mock.patch.object(
                    atomic_publish.os,
                    "fdopen",
                    side_effect=fail_after_close,
                ),
                self.assertRaisesRegex(OSError, "injected close failure"),
            ):
                with atomic_publish._open_private_file_claim(
                    claim,
                    truncate=True,
                ) as output:
                    output.write(b"committed bytes")

            self.assertFalse(claim._state.writer_active)
            self.assertIsNone(atomic_publish._retire_private_file(claim))

    def test_relocation_stem_cannot_escape_its_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".stage.",
                suffix=".tmp",
            )
            payload = b"sealed source"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )

            with self.assertRaisesRegex(ValueError, "one path component"):
                atomic_publish._relocate_sealed_private_file(
                    sealed,
                    stem="../escape",
                )
            self.assertIsNone(atomic_publish._retire_private_file(claim))

    def test_sealed_private_file_installs_at_one_exact_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".formal.",
                suffix=".wav",
            )
            payload = b"sealed formal mix"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            target = root / "mix.wav"

            installed = atomic_publish._install_sealed_private_file(
                sealed,
                target,
            )

            self.assertEqual(installed.claim.path, target.resolve())
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(os.path.lexists(claim.path))
            with self.assertRaisesRegex(RuntimeError, "stale"):
                atomic_publish._retire_private_file(claim)
            atomic_publish._retire_sealed_private_file(
                installed,
                require_present=True,
            )
            self.assertFalse(target.exists())

    def test_exact_install_never_replaces_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".formal.",
                suffix=".wav",
            )
            payload = b"sealed formal mix"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            target = root / "mix.wav"
            target.write_bytes(b"existing public generation")

            with self.assertRaises(FileExistsError):
                atomic_publish._install_sealed_private_file(sealed, target)

            self.assertEqual(target.read_bytes(), b"existing public generation")
            self.assertEqual(claim.path.read_bytes(), payload)
            atomic_publish._retire_sealed_private_file(
                sealed,
                require_present=True,
            )

    def test_exact_install_accepts_move_then_reported_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".formal.",
                suffix=".wav",
            )
            payload = b"committed formal mix"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            target = root / "mix.wav"
            real_rename = atomic_publish._rename_noreplace

            def move_then_raise(source, destination):
                real_rename(source, destination)
                raise OSError("injected error after exact install")

            with mock.patch.object(
                atomic_publish,
                "_rename_noreplace",
                side_effect=move_then_raise,
            ):
                installed = atomic_publish._install_sealed_private_file(
                    sealed,
                    target,
                )

            self.assertEqual(target.read_bytes(), payload)
            atomic_publish._retire_sealed_private_file(
                installed,
                require_present=True,
            )

    def test_exact_install_never_adopts_a_destination_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            claim = atomic_publish._reserve_private_file(
                root,
                prefix=".formal.",
                suffix=".wav",
            )
            payload = b"sealed formal mix"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )
            target = root / "mix.wav"
            parked = root / "parked-formal.wav"
            sentinel = b"destination racer"
            real_capture = atomic_publish._capture_file
            injected = False

            def replace_installed_target(path: Path):
                nonlocal injected
                if (
                    path.resolve(strict=False)
                    == target.resolve(strict=False)
                    and not injected
                ):
                    injected = True
                    os.replace(path, parked)
                    path.write_bytes(sentinel)
                return real_capture(path)

            with (
                mock.patch.object(
                    atomic_publish,
                    "_capture_file",
                    side_effect=replace_installed_target,
                ),
                self.assertWarnsRegex(
                    RuntimeWarning,
                    "no longer matches its sealed generation",
                ),
                self.assertRaisesRegex(OSError, "not the sealed"),
            ):
                atomic_publish._install_sealed_private_file(sealed, target)

            self.assertTrue(injected)
            self.assertEqual(parked.read_bytes(), payload)
            self.assertEqual(target.read_bytes(), sentinel)

    def test_exact_install_cannot_escape_the_captured_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            outside = root / "outside"
            private.mkdir()
            outside.mkdir()
            claim = atomic_publish._reserve_private_file(
                private,
                prefix=".formal.",
                suffix=".wav",
            )
            payload = b"sealed formal mix"
            atomic_publish._write_private_file_bytes(claim, payload)
            sealed = atomic_publish._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(payload).hexdigest(),
            )

            with self.assertRaisesRegex(OSError, "captured parent"):
                atomic_publish._install_sealed_private_file(
                    sealed,
                    outside / "mix.wav",
                )

            self.assertEqual(claim.path.read_bytes(), payload)
            self.assertFalse((outside / "mix.wav").exists())
            atomic_publish._retire_sealed_private_file(
                sealed,
                require_present=True,
            )


if __name__ == "__main__":
    unittest.main()
