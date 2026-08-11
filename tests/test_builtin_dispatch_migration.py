from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "reverify_builtin_dispatch_migration.py"


def _load_tool():
    name = "tianlai_test_builtin_dispatch_migration"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {TOOL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class BuiltinDispatchMigrationTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        self.temporary = tempfile.TemporaryDirectory(
            prefix="tianlai_dispatch_transaction_",
            dir=ROOT,
        )
        self.root = Path(self.temporary.name)
        self.staging = self.root / "staging"
        self.staging.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _documents(self, count: int = 3) -> tuple[dict[Path, Path], dict[Path, bytes]]:
        staged: dict[Path, Path] = {}
        previous: dict[Path, bytes] = {}
        for index in range(count):
            directory = self.root / f"target-{index:02d}"
            directory.mkdir()
            destination = directory / "report.json"
            source = self.staging / f"{index:02d}.json"
            old_bytes = f'{{"old":{index}}}\n'.encode()
            destination.write_bytes(old_bytes)
            source.write_bytes(f'{{"new":{index}}}\n'.encode())
            staged[destination] = source
            previous[destination] = old_bytes
        return staged, previous

    def _same_directory_temporaries(self) -> list[Path]:
        return sorted(self.root.rglob(".*.json.tmp"))

    def test_install_failure_restores_every_report_and_cleans_temporaries(self) -> None:
        staged, previous = self._documents(count=40)
        real_replace = os.replace
        install_calls = 0

        def fail_nth_install(source, destination):
            nonlocal install_calls
            if "-prepared-" in Path(source).name:
                install_calls += 1
                if install_calls == 23:
                    raise OSError("injected install failure")
            return real_replace(source, destination)

        with mock.patch.object(self.tool.os, "replace", side_effect=fail_nth_install):
            with self.assertRaisesRegex(OSError, "injected install failure"):
                self.tool._commit_reports(staged, self.root)

        self.assertEqual(
            {path: path.read_bytes() for path in previous},
            previous,
        )
        self.assertEqual(self._same_directory_temporaries(), [])

    def test_failed_rollback_preserves_the_exact_backup_and_reports_its_path(
        self,
    ) -> None:
        staged, previous = self._documents(count=2)
        destinations = sorted(staged)
        real_replace = os.replace
        install_calls = 0

        def fail_install_then_rollback(source, destination):
            nonlocal install_calls
            source_path = Path(source)
            destination_path = Path(destination)
            if "-prepared-" in source_path.name:
                install_calls += 1
                if install_calls == 2:
                    raise OSError("injected install failure")
            if (
                "-backup-" in source_path.name
                and destination_path == destinations[0]
            ):
                raise OSError("injected rollback failure")
            return real_replace(source, destination)

        with mock.patch.object(
            self.tool.os,
            "replace",
            side_effect=fail_install_then_rollback,
        ):
            with self.assertRaisesRegex(
                self.tool.FactoryDispatchMigrationError,
                "rollback was incomplete.*backup=",
            ) as raised:
                self.tool._commit_reports(staged, self.root)

        backups = self._same_directory_temporaries()
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), previous[destinations[0]])
        self.assertIn(str(backups[0]), str(raised.exception))
        self.assertEqual(destinations[1].read_bytes(), previous[destinations[1]])


if __name__ == "__main__":
    unittest.main()
