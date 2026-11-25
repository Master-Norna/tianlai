from pathlib import Path
import tempfile
import unittest

from tianlai.cache_usage import cache_usage


class CacheUsageTests(unittest.TestCase):
    def test_counts_only_known_cache_payloads_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / ".tianlai-cache"
            self.assertEqual(cache_usage(root)["total_bytes"], 0)
            self.assertFalse(root.exists())
            key = "ab" * 32
            for name, suffix, payload in (
                ("stems", ".f32le", b"12345678"),
                ("stems", ".json", b"123"),
                ("analysis", ".json", b"12345"),
                ("analysis", ".tmp", b"ignored"),
                ("analysis", ".f32le", b"ignored"),
                ("unknown", ".json", b"ignored"),
            ):
                directory = root / name / "v1" / "ab"
                directory.mkdir(parents=True, exist_ok=True)
                (directory / (key + suffix)).write_bytes(payload)
            before = sorted(root.rglob("*"))
            report = cache_usage(root)
            self.assertEqual(report["stems"], {"files": 2, "bytes": 11})
            self.assertEqual(report["analysis"], {"files": 1, "bytes": 5})
            self.assertEqual(report["total_bytes"], 16)
            self.assertEqual(before, sorted(root.rglob("*")))

    def test_does_not_follow_linked_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / ".tianlai-cache"
            directory = root / "stems" / "v1"
            directory.mkdir(parents=True)
            external = base / "external"
            external.mkdir()
            (external / ("ab" * 32 + ".json")).write_bytes(b"outside")
            try:
                (directory / "ab").symlink_to(external, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            self.assertEqual(cache_usage(root)["total_bytes"], 0)
