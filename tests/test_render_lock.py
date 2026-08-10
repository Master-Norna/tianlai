from __future__ import annotations

from contextlib import ExitStack
import errno
import json
import multiprocessing
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unicodedata
import unittest
from unittest import mock

import tianlai.render_lock as render_lock_module
from tianlai.render_lock import (
    RenderLockError,
    acquire_render_lock,
    capture_plain_directory,
    render_lock_path,
)


ROOT = Path(__file__).resolve().parents[1]


class _ContextFailure(Exception):
    pass


def _compete_for_initial_render_lock(
    target_value: str,
    both_opened,
    both_attempted,
    events,
) -> None:
    """Exercise the first-use sidecar race in an independent process."""

    real_try_lock = render_lock_module._try_lock

    def synchronized_try_lock(handle) -> None:
        events.put(("size", os.fstat(handle.fileno()).st_size))
        both_opened.wait(timeout=30.0)
        try:
            real_try_lock(handle)
        finally:
            # The winner keeps ownership until the loser has made its
            # non-blocking operating-system lock attempt.
            both_attempted.wait(timeout=30.0)

    render_lock_module._try_lock = synchronized_try_lock
    try:
        try:
            with acquire_render_lock(Path(target_value)):
                outcome = "owned"
        except RenderLockError:
            outcome = "busy"
        events.put(("outcome", outcome))
    except BaseException as exc:
        events.put(("error", f"{type(exc).__name__}: {exc}"))
        raise
    finally:
        render_lock_module._try_lock = real_try_lock


class RenderLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_lock_path_is_stable_for_resolved_target_and_stays_outside_it(
        self,
    ) -> None:
        target = self.root / "nested" / "render"
        alias = self.root / "nested" / "child" / ".." / "render"

        direct = render_lock_path(target)
        through_alias = render_lock_path(alias)

        self.assertEqual(direct, through_alias)
        self.assertEqual(direct.parent, target.resolve().parent)
        self.assertNotEqual(direct.parent, target.resolve())
        self.assertFalse(direct.is_relative_to(target.resolve()))

        if os.name == "nt":
            self.assertEqual(
                direct,
                render_lock_path(Path(str(target).swapcase())),
            )

    def test_lock_creation_safely_creates_a_missing_multilevel_parent(self) -> None:
        target = self.root / "one" / "two" / "three" / "render"

        with acquire_render_lock(target) as owned:
            self.assertEqual(owned.output_directory, target.resolve())
            self.assertTrue(target.parent.is_dir())

    def test_macos_system_var_and_tmp_aliases_compare_as_canonical_paths(
        self,
    ) -> None:
        with (
            mock.patch(
                "tianlai.render_lock._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.render_lock._is_macos_runtime",
                return_value=True,
            ),
            mock.patch(
                "tianlai.render_lock.os.path.abspath",
                side_effect=lambda value: value,
            ),
        ):
            self.assertEqual(
                render_lock_module._path_comparison_key(
                    Path("/var/folders/example/output")
                ),
                render_lock_module._path_comparison_key(
                    Path("/private/var/folders/example/output")
                ),
            )
            self.assertEqual(
                render_lock_module._path_comparison_key(Path("/tmp/output")),
                render_lock_module._path_comparison_key(
                    Path("/private/tmp/output")
                ),
            )
            self.assertNotEqual(
                render_lock_module._path_comparison_key(Path("/opt/alias/output")),
                render_lock_module._path_comparison_key(Path("/srv/real/output")),
            )

    def test_macos_exact_var_alias_is_captured_but_arbitrary_link_is_not(
        self,
    ) -> None:
        status = SimpleNamespace(st_dev=7, st_ino=11)

        def capture(requested: str, resolved: str):
            with ExitStack() as stack:
                stack.enter_context(
                    mock.patch(
                        "tianlai.render_lock._is_windows_runtime",
                        return_value=False,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "tianlai.render_lock._is_macos_runtime",
                        return_value=True,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "tianlai.render_lock.os.path.abspath",
                        side_effect=lambda value: value,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "tianlai.render_lock._plain_directory_status",
                        return_value=status,
                    )
                )
                stack.enter_context(
                    mock.patch(
                        "tianlai.render_lock.os.path.samestat",
                        return_value=True,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        Path,
                        "is_absolute",
                        return_value=True,
                    )
                )
                stack.enter_context(
                    mock.patch.object(
                        Path,
                        "resolve",
                        return_value=Path(resolved),
                    )
                )
                return capture_plain_directory(Path(requested))

        identity = capture("/var", "/private/var")
        self.assertEqual(identity.path.as_posix(), "/private/var")
        with self.assertRaises(OSError) as raised:
            capture("/opt/alias", "/srv/real")
        self.assertEqual(raised.exception.errno, errno.ELOOP)

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 paths are required")
    def test_windows_short_name_alias_is_captured_as_plain_directory(self) -> None:
        import ctypes

        target = self.root / "Tianlai ordinary directory with spaces"
        target.mkdir()
        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetShortPathNameW(
            str(target),
            buffer,
            len(buffer),
        )
        if not length or length >= len(buffer):
            self.skipTest("GetShortPathNameW did not return an alias")
        short_path = Path(buffer.value)
        if render_lock_module._path_comparison_key(short_path) == (
            render_lock_module._path_comparison_key(target)
        ):
            self.skipTest("8.3 short-name generation is disabled on this volume")

        identity = capture_plain_directory(short_path)

        self.assertEqual(identity.path, target.resolve())
        self.assertTrue(os.path.samefile(identity.path, short_path))

    @unittest.skipUnless(os.name == "nt", "Windows 8.3 paths are required")
    def test_windows_short_name_parent_acquires_the_canonical_render_lock(
        self,
    ) -> None:
        import ctypes

        parent = self.root / "Tianlai ordinary parent with spaces"
        parent.mkdir()
        buffer = ctypes.create_unicode_buffer(32_768)
        length = ctypes.windll.kernel32.GetShortPathNameW(
            str(parent),
            buffer,
            len(buffer),
        )
        if not length or length >= len(buffer):
            self.skipTest("GetShortPathNameW did not return an alias")
        short_parent = Path(buffer.value)
        if render_lock_module._path_comparison_key(short_parent) == (
            render_lock_module._path_comparison_key(parent)
        ):
            self.skipTest("8.3 short-name generation is disabled on this volume")

        parent_identity = capture_plain_directory(short_parent)
        with acquire_render_lock(
            short_parent / "render target",
            parent_identity=parent_identity,
        ) as owned:
            self.assertEqual(
                owned.output_directory,
                parent.resolve() / "render target",
            )
            self.assertEqual(owned.lock_path.parent, parent.resolve())

    def test_render_lock_rejects_a_different_verified_parent(self) -> None:
        requested_parent = self.root / "requested"
        authorised_parent = self.root / "authorised"
        requested_parent.mkdir()
        authorised_parent.mkdir()
        identity = capture_plain_directory(authorised_parent)

        with self.assertRaises(OSError) as raised:
            with acquire_render_lock(
                requested_parent / "render",
                parent_identity=identity,
            ):
                self.fail("a mismatched parent identity was accepted")

        self.assertEqual(raised.exception.errno, errno.EPERM)

    def test_directory_alias_binding_propagates_final_revalidation_failure(
        self,
    ) -> None:
        authorised = render_lock_module.PlainDirectoryIdentity(
            path=self.root / "canonical",
            device=7,
            inode=11,
        )
        observed = render_lock_module.PlainDirectoryIdentity(
            path=authorised.path,
            device=7,
            inode=11,
        )
        changed = OSError(errno.ESTALE, "injected final revalidation failure")

        with (
            mock.patch(
                "tianlai.render_lock.capture_plain_directory",
                return_value=observed,
            ),
            mock.patch(
                "tianlai.render_lock.revalidate_plain_directory",
                side_effect=(authorised.path, authorised.path, changed),
            ),
            self.assertRaises(OSError) as raised,
        ):
            render_lock_module._bind_plain_directory_path(
                self.root / "RUNNER~1",
                authorised,
                message="must remain bound",
            )

        self.assertIs(raised.exception, changed)

    def test_directory_alias_binding_rejects_distinct_zero_identities(
        self,
    ) -> None:
        authorised = render_lock_module.PlainDirectoryIdentity(
            path=self.root / "canonical",
            device=0,
            inode=0,
        )
        observed = render_lock_module.PlainDirectoryIdentity(
            path=self.root / "different",
            device=0,
            inode=0,
        )

        with (
            mock.patch(
                "tianlai.render_lock.capture_plain_directory",
                return_value=observed,
            ),
            mock.patch(
                "tianlai.render_lock.revalidate_plain_directory",
                return_value=authorised.path,
            ),
            self.assertRaises(OSError) as raised,
        ):
            render_lock_module._bind_plain_directory_path(
                self.root / "RUNNER~1",
                authorised,
                message="must remain bound",
            )

        self.assertEqual(raised.exception.errno, errno.EPERM)

    def test_capture_rejects_zero_inode_on_the_final_directory_only(
        self,
    ) -> None:
        requested = self.root / "zero-identity"
        status = SimpleNamespace(st_dev=7, st_ino=0)

        with (
            mock.patch(
                "tianlai.render_lock._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.render_lock._is_macos_runtime",
                return_value=False,
            ),
            mock.patch.object(Path, "resolve", return_value=requested),
            mock.patch(
                "tianlai.render_lock._plain_directory_status",
                return_value=status,
            ),
        ):
            with self.assertRaises(OSError) as raised:
                capture_plain_directory(requested)

        self.assertEqual(raised.exception.errno, errno.ENOTSUP)

    def test_open_lock_validation_rejects_zero_file_identity(self) -> None:
        path = self.root / "zero-identity.lock"
        status = SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_ino=0,
            st_nlink=1,
        )

        with (
            mock.patch("tianlai.render_lock.os.fstat", return_value=status),
            mock.patch("tianlai.render_lock.os.lstat", return_value=status),
            mock.patch("tianlai.render_lock.os.path.samestat") as samestat,
            self.assertRaises(OSError) as raised,
        ):
            render_lock_module._validate_open_lock_file(path, 123)

        self.assertEqual(raised.exception.errno, errno.ENOTSUP)
        samestat.assert_not_called()

    def test_windows_alias_exception_still_revalidates_every_ancestor(
        self,
    ) -> None:
        requested = self.root / "RUNNER~1" / "output"
        resolved = self.root / "Runner Name" / "output"
        status = SimpleNamespace(st_dev=7, st_ino=11)
        requested_ancestry = ((requested, status),)
        resolved_ancestry = ((resolved, status),)

        with (
            mock.patch(
                "tianlai.render_lock._is_windows_runtime",
                return_value=True,
            ),
            mock.patch.object(Path, "resolve", return_value=resolved),
            mock.patch(
                "tianlai.render_lock._windows_long_path_name",
                side_effect=(resolved, resolved),
            ) as long_path_name,
            mock.patch(
                "tianlai.render_lock._capture_plain_directory_ancestry",
                side_effect=(requested_ancestry, resolved_ancestry),
            ) as capture_ancestry,
            mock.patch(
                "tianlai.render_lock._revalidate_plain_directory_ancestry",
            ) as revalidate_ancestry,
            mock.patch(
                "tianlai.render_lock._plain_directory_status",
                return_value=status,
            ),
            mock.patch(
                "tianlai.render_lock.os.path.samestat",
                return_value=True,
            ),
        ):
            identity = capture_plain_directory(requested)

        self.assertEqual(identity.path, resolved)
        self.assertEqual(long_path_name.call_args_list, [
            mock.call(requested),
            mock.call(requested),
        ])
        self.assertEqual(capture_ancestry.call_args_list, [
            mock.call(requested),
            mock.call(resolved),
        ])
        self.assertEqual(revalidate_ancestry.call_args_list, [
            mock.call(requested_ancestry),
            mock.call(resolved_ancestry),
        ])

    def test_windows_alias_exception_rejects_non_short_name_resolution(
        self,
    ) -> None:
        requested = self.root / "RUNNER~1" / "output"
        expanded = self.root / "Runner Name" / "output"
        resolved = self.root / "outside" / "output"
        status = SimpleNamespace(st_dev=7, st_ino=11)

        with (
            mock.patch(
                "tianlai.render_lock._is_windows_runtime",
                return_value=True,
            ),
            mock.patch.object(Path, "resolve", return_value=resolved),
            mock.patch(
                "tianlai.render_lock._windows_long_path_name",
                side_effect=(expanded, expanded),
            ),
            mock.patch(
                "tianlai.render_lock._capture_plain_directory_ancestry",
                return_value=((requested, status),),
            ),
            mock.patch(
                "tianlai.render_lock._plain_directory_status",
                return_value=status,
            ),
        ):
            with self.assertRaises(OSError) as raised:
                capture_plain_directory(requested)

        self.assertEqual(raised.exception.errno, errno.ELOOP)

    def test_plain_directory_ancestry_rejects_a_reparse_ancestor(self) -> None:
        target = self.root / "linked" / "output"
        ordinary = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=0,
            st_ino=17,
        )
        reparse = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=getattr(
                stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0x0400,
            ),
            st_ino=19,
        )

        def fake_lstat(path: Path) -> SimpleNamespace:
            return reparse if Path(path).name == "linked" else ordinary

        with mock.patch("tianlai.render_lock.os.lstat", side_effect=fake_lstat):
            with self.assertRaises(OSError) as raised:
                render_lock_module._capture_plain_directory_ancestry(target)

        self.assertEqual(raised.exception.errno, errno.ELOOP)

    def test_macos_case_and_unicode_aliases_share_one_lock(self) -> None:
        nfc_target = self.root / "Café Output"
        case_alias = self.root / "CAFÉ OUTPUT"
        nfd_alias = self.root / unicodedata.normalize("NFD", "Café Output")

        with (
            mock.patch(
                "tianlai.render_lock._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.render_lock._is_macos_runtime",
                return_value=True,
            ),
        ):
            expected = render_lock_path(nfc_target)
            self.assertEqual(expected, render_lock_path(case_alias))
            self.assertEqual(expected, render_lock_path(nfd_alias))

            with acquire_render_lock(nfc_target):
                for alias in (case_alias, nfd_alias):
                    with (
                        self.subTest(alias=alias.name),
                        self.assertRaises(RenderLockError),
                    ):
                        with acquire_render_lock(alias):
                            self.fail("a macOS path alias bypassed the lock")

    def test_other_posix_runtime_keeps_case_distinct(self) -> None:
        with (
            mock.patch(
                "tianlai.render_lock._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.render_lock._is_macos_runtime",
                return_value=False,
            ),
        ):
            self.assertNotEqual(
                render_lock_path(self.root / "Output"),
                render_lock_path(self.root / "output"),
            )

    def test_filesystem_root_is_rejected_instead_of_locking_inside_it(
        self,
    ) -> None:
        root = Path(Path.cwd().anchor)
        with self.assertRaisesRegex(ValueError, "根目录"):
            render_lock_path(root)

    def test_same_target_fails_immediately_in_the_same_process(self) -> None:
        target = self.root / "same"
        with acquire_render_lock(target) as owned:
            started = time.monotonic()
            with self.assertRaises(RenderLockError) as raised:
                with acquire_render_lock(target):
                    self.fail("the same render target was locked twice")
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 1.0)
            self.assertEqual(raised.exception.output_directory, target.resolve())
            self.assertEqual(raised.exception.lock_path, owned.lock_path)
            self.assertIn("等待现有渲染完成后重试", str(raised.exception))

    def test_two_first_callers_lock_empty_sidecar_before_metadata_write(
        self,
    ) -> None:
        target = self.root / "first-use-race"
        context = multiprocessing.get_context("spawn")
        both_opened = context.Barrier(2)
        both_attempted = context.Barrier(2)
        events = context.Queue()
        processes = [
            context.Process(
                target=_compete_for_initial_render_lock,
                args=(str(target), both_opened, both_attempted, events),
                name=f"render-lock-first-use-{index}",
            )
            for index in range(2)
        ]
        records: list[tuple[str, object]] = []
        try:
            for process in processes:
                process.start()
            deadline = time.monotonic() + 45.0
            for process in processes:
                process.join(timeout=max(0.0, deadline - time.monotonic()))

            self.assertFalse(
                any(process.is_alive() for process in processes),
                "render-lock competitors did not finish",
            )
            self.assertEqual(
                [process.exitcode for process in processes],
                [0, 0],
            )
            records = [events.get(timeout=5.0) for _ in range(4)]
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=10.0)
            events.close()
            events.join_thread()

        errors = [value for kind, value in records if kind == "error"]
        self.assertEqual(errors, [])
        observed_sizes = [value for kind, value in records if kind == "size"]
        outcomes = [value for kind, value in records if kind == "outcome"]

        self.assertEqual(observed_sizes, [0, 0])
        self.assertEqual(sorted(outcomes), ["busy", "owned"])
        self.assertGreaterEqual(render_lock_path(target).stat().st_size, 1)

    def test_different_targets_do_not_block_each_other(self) -> None:
        first = self.root / "first"
        second = self.root / "second"
        with acquire_render_lock(first) as first_lock:
            with acquire_render_lock(second) as second_lock:
                self.assertNotEqual(first_lock.lock_path, second_lock.lock_path)
                self.assertEqual(first_lock.output_directory, first.resolve())
                self.assertEqual(second_lock.output_directory, second.resolve())

    def test_exception_inside_context_releases_the_lock(self) -> None:
        target = self.root / "exception"
        with self.assertRaises(_ContextFailure):
            with acquire_render_lock(target):
                raise _ContextFailure

        with acquire_render_lock(target) as acquired_again:
            self.assertEqual(acquired_again.output_directory, target.resolve())

    def test_retained_lock_file_contains_diagnostic_owner_metadata(self) -> None:
        target = self.root / "metadata"
        with acquire_render_lock(target) as owned:
            with owned.lock_path.open("rb") as source:
                source.seek(1)
                document = json.loads(source.read().decode("utf-8"))
            self.assertEqual(document["format"], "tianlai.render_lock")
            self.assertEqual(document["version"], 1)
            self.assertEqual(document["pid"], owned.owner_pid)
            self.assertEqual(
                document["output_directory"],
                str(target.resolve()),
            )

        self.assertTrue(owned.lock_path.is_file())

    def test_hard_link_lock_file_is_rejected_without_touching_target(
        self,
    ) -> None:
        target = self.root / "hard-link-output"
        lock_path = render_lock_path(target)
        victim = self.root / "must-not-change.txt"
        original = b"sensitive content that must remain intact\n"
        victim.write_bytes(original)
        try:
            os.link(victim, lock_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"hard links are unavailable: {exc}")

        with self.assertRaises(OSError):
            with acquire_render_lock(target):
                self.fail("an unsafe hard-linked lock file was accepted")

        self.assertEqual(victim.read_bytes(), original)
        self.assertEqual(lock_path.read_bytes(), original)

    def test_symbolic_link_lock_file_is_rejected_without_touching_target(
        self,
    ) -> None:
        target = self.root / "symlink-output"
        lock_path = render_lock_path(target)
        victim = self.root / "must-not-truncate.txt"
        original = b"unrelated file content must survive\n"
        victim.write_bytes(original)
        try:
            os.symlink(victim, lock_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")

        with self.assertRaises(OSError):
            with acquire_render_lock(target):
                self.fail("an unsafe symlink lock file was accepted")

        self.assertEqual(victim.read_bytes(), original)
        self.assertTrue(lock_path.is_symlink())

    def test_child_process_competes_and_crash_releases_os_lock(self) -> None:
        target = self.root / "subprocess"
        ready = self.root / "child-ready"
        child_code = (
            "from pathlib import Path\n"
            "import sys\n"
            "import time\n"
            "from tianlai.render_lock import acquire_render_lock\n"
            "target = Path(sys.argv[1])\n"
            "ready = Path(sys.argv[2])\n"
            "with acquire_render_lock(target):\n"
            "    ready.write_text('locked', encoding='utf-8')\n"
            "    time.sleep(60)\n"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", child_code, str(target), str(ready)],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10.0
            while not ready.is_file() and time.monotonic() < deadline:
                if process.poll() is not None:
                    _stdout, stderr = process.communicate()
                    self.fail(f"lock child exited before readiness: {stderr}")
                time.sleep(0.02)
            self.assertTrue(ready.is_file(), "lock child did not become ready")

            started = time.monotonic()
            with self.assertRaises(RenderLockError):
                with acquire_render_lock(target):
                    self.fail("parent acquired a child-owned render target")
            self.assertLess(time.monotonic() - started, 1.0)

            # TerminateProcess on Windows and SIGTERM on POSIX both skip the
            # child's context cleanup.  The kernel must still release the lock.
            process.terminate()
            process.wait(timeout=10.0)
            # Windows may expose a very short release-visibility delay after
            # TerminateProcess has reported exit.  Keep production acquisition
            # non-blocking; only this crash-recovery assertion retries briefly.
            recovery_deadline = time.monotonic() + 1.0
            while True:
                try:
                    with acquire_render_lock(target) as recovered:
                        self.assertEqual(
                            recovered.output_directory,
                            target.resolve(),
                        )
                    break
                except RenderLockError:
                    if (
                        os.name != "nt"
                        or time.monotonic() >= recovery_deadline
                    ):
                        raise
                    time.sleep(0.01)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10.0)
            process.communicate(timeout=10.0)


if __name__ == "__main__":
    unittest.main()
