from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unicodedata
import unittest
from unittest import mock

from tianlai.render_lock import (
    RenderLockError,
    acquire_render_lock,
    render_lock_path,
)


ROOT = Path(__file__).resolve().parents[1]


class _ContextFailure(Exception):
    pass


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
