from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import numpy as np

from tianlai.render_lock import acquire_render_lock
from tianlai.stem_cache import (
    PROCESS_SOURCE_TREE_SHA256,
    StemCache,
    build_cache_key,
    current_source_tree_matches,
    source_tree_digest,
)


class StemCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "cache"
        self.cache = StemCache(self.root)
        self.key = build_cache_key({"part": "violin", "notes": [60, 62]})
        self.manifest = hashlib.sha256(b"manifest").hexdigest()
        self.audio = np.array([[0.0, 0.25], [-0.5, 1.0]], dtype=np.float32)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(self):
        return self.cache.store(
            self.key,
            self.audio,
            stage="instrument",
            sample_rate=48_000,
            peak_voices=3,
            manifest_sha256=self.manifest,
        )

    def test_round_trip_is_raw_stereo_float32_and_owned(self) -> None:
        stored = self._store()
        self.assertEqual(stored.status, "stored")
        assert stored.record is not None
        self.assertEqual(
            stored.record.audio_path.parent,
            (self.root / "v1" / self.key[:2]).resolve(),
        )
        self.assertEqual(stored.record.audio_path.read_bytes(), self.audio.astype("<f4").tobytes())
        self.assertEqual(set(stored.record.metadata), {
            "format", "version", "key", "stage", "dtype", "channels", "sample_rate",
            "frame_count", "byte_length", "audio_sha256", "peak_voices", "manifest_sha256",
        })
        loaded = self.cache.load(self.key)
        self.assertTrue(loaded.hit)
        assert loaded.audio is not None
        self.assertEqual(loaded.audio.dtype, np.dtype("<f4"))
        self.assertTrue(loaded.audio.flags.writeable)
        self.assertTrue(loaded.audio.flags.owndata)
        np.testing.assert_array_equal(loaded.audio, self.audio)

    def test_missing_invalid_and_nonfinite_input_are_structured(self) -> None:
        self.assertEqual(self.cache.load(self.key).status, "missing")
        self.assertEqual(self.cache.load("bad").status, "invalid_key")
        bad = self.audio.copy()
        bad[0, 0] = np.nan
        self.assertEqual(
            self.cache.store(self.key, bad, stage="x", sample_rate=1, peak_voices=0, manifest_sha256=self.manifest).status,
            "invalid_input",
        )

    def test_metadata_duplicate_unknown_and_hash_corruption_are_rejected(self) -> None:
        stored = self._store()
        assert stored.record is not None
        path = stored.record.metadata_path
        path.write_text('{"format":"x","format":"x"}', encoding="utf-8")
        self.assertEqual(self.cache.load(self.key).status, "corrupt")
        self._store()  # Repair the deliberately corrupted entry.
        document = json.loads(path.read_text(encoding="utf-8"))
        document["unexpected"] = 1
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(self.cache.load(self.key).status, "corrupt")

        # JSON's NaN token is not valid cache metadata even though Python's
        # default JSON decoder would normally accept it.
        path.write_text('{"sample_rate":NaN}', encoding="utf-8")
        self.assertEqual(self.cache.load(self.key).status, "corrupt")

    def test_valid_existing_different_audio_is_never_overwritten(self) -> None:
        self.assertEqual(self._store().status, "stored")
        other = self.audio.copy()
        other[0, 0] = 0.75
        result = self.cache.store(
            self.key, other, stage="instrument", sample_rate=48_000, peak_voices=3, manifest_sha256=self.manifest
        )
        self.assertEqual(result.status, "conflict")
        np.testing.assert_array_equal(self.cache.load(self.key).audio, self.audio)

    def test_valid_but_stale_semantic_metadata_is_repaired(self) -> None:
        stored = self._store()
        assert stored.record is not None
        document = json.loads(
            stored.record.metadata_path.read_text(encoding="utf-8")
        )
        document["stage"] = "stale-stage"
        stored.record.metadata_path.write_text(
            json.dumps(document),
            encoding="utf-8",
        )
        self.assertTrue(self.cache.load(self.key).hit)

        repaired = self._store()

        self.assertEqual(repaired.status, "repaired")
        loaded = self.cache.load(self.key)
        self.assertTrue(loaded.hit)
        assert loaded.record is not None
        self.assertEqual(loaded.record.metadata["stage"], "instrument")
        np.testing.assert_array_equal(loaded.audio, self.audio)

    def test_symlink_entry_is_not_followed(self) -> None:
        self._store()
        entry = self.root / "v1" / self.key[:2] / f"{self.key}.f32le"
        replacement = self.root / "outside.f32le"
        replacement.write_bytes(entry.read_bytes())
        try:
            entry.unlink()
            entry.symlink_to(replacement)
        except (OSError, NotImplementedError):
            self.skipTest("symbolic links are unavailable")
        self.assertEqual(self.cache.load(self.key).status, "corrupt")

    def test_key_lock_is_nonblocking_and_returns_busy(self) -> None:
        self.root.mkdir(parents=True)
        with acquire_render_lock(self.root / ".locks" / self.key):
            self.assertEqual(self._store().status, "busy")

    def test_canonical_key_and_source_digest_helpers(self) -> None:
        self.assertEqual(build_cache_key({"a": 1, "b": [2]}), build_cache_key({"b": [2], "a": 1}))
        self.assertTrue(current_source_tree_matches())
        self.assertEqual(source_tree_digest(), PROCESS_SOURCE_TREE_SHA256)


if __name__ == "__main__":
    unittest.main()
