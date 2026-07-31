from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from tianlai.canonical_json import canonical_json_file_sha256


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "迁移试听证据哈希.py"


def _load_tool():
    name = "tianlai_test_audition_evidence_migration"
    spec = importlib.util.spec_from_file_location(name, TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {TOOL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _raw_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AuditionEvidenceMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.directory = self.root / "乐器" / "键盘乐器" / "测试钢琴"
        self.directory.mkdir(parents=True)
        self.events = self.root / "examples" / "测试.events.json"
        self.events.parent.mkdir()
        self.manifest = self.directory / "乐器.json"
        self.report = self.directory / "试听核验.json"
        self.manifest.write_bytes(b'{\r\n  "z": 1,\r\n  "a": 2\r\n}\r\n')
        self.events.write_bytes(
            '{"events": [],\r\n  "schema_version": 1}\r\n'.encode("utf-8")
        )
        self.report.write_text(
            json.dumps(
                {
                    "wav_sha256": "0" * 64,
                    "manifest_sha256": _raw_hash(self.manifest),
                    "events": "examples/测试.events.json",
                    "events_sha256": _raw_hash(self.events),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_migration_preserves_legacy_chain_and_writes_canonical_identity(
        self,
    ) -> None:
        self.assertTrue(
            self.tool.migrate_report(self.report, root=self.root, write=True)
        )
        migrated = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertNotIn("manifest_sha256", migrated)
        self.assertNotIn("events_sha256", migrated)
        self.assertEqual(migrated["hash_algorithm"], "SHA-256")
        self.assertEqual(migrated["canonicalization"], "tianlai-json-v1")
        self.assertEqual(
            migrated["manifest_canonical_sha256"],
            canonical_json_file_sha256(self.manifest),
        )
        self.assertEqual(
            migrated["events_canonical_sha256"],
            canonical_json_file_sha256(self.events),
        )
        self.assertEqual(
            migrated["identity_migration"]["manifest_sha256"],
            _raw_hash(self.manifest),
        )
        self.assertFalse(
            self.tool.migrate_report(self.report, root=self.root, write=False)
        )

    def test_stale_legacy_binding_is_rejected(self) -> None:
        document = json.loads(self.report.read_text(encoding="utf-8"))
        document["manifest_sha256"] = "f" * 64
        self.report.write_text(json.dumps(document), encoding="utf-8")
        with self.assertRaisesRegex(
            self.tool.AuditionEvidenceMigrationError,
            "旧 manifest 字节 Hash 已过期",
        ):
            self.tool.migrate_report(self.report, root=self.root, write=True)


if __name__ == "__main__":
    unittest.main()
