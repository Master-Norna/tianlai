from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai import stem_cache as stem_cache_module
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

    def test_bounded_load_skips_track_allocation_above_limit(self) -> None:
        self.assertEqual(self._store().status, "stored")

        with patch(
            "tianlai.stem_cache.np.fromfile",
            side_effect=AssertionError("oversized cache audio was loaded"),
        ) as fromfile:
            lookup = self.cache.load(
                self.key,
                maximum_audio_bytes=self.audio.nbytes - 1,
            )

        self.assertEqual(lookup.status, "too_large")
        self.assertIsNone(lookup.audio)
        fromfile.assert_not_called()

        loaded = self.cache.load(
            self.key,
            maximum_audio_bytes=self.audio.nbytes,
        )
        self.assertTrue(loaded.hit)
        np.testing.assert_array_equal(loaded.audio, self.audio)

    def test_bounded_load_rejects_invalid_limit_without_reading(self) -> None:
        self.assertEqual(self._store().status, "stored")

        with patch(
            "tianlai.stem_cache.np.fromfile",
            side_effect=AssertionError("invalid bounded load read audio"),
        ) as fromfile:
            for value in (-1, True, 1.5):
                lookup = self.cache.load(  # type: ignore[arg-type]
                    self.key,
                    maximum_audio_bytes=value,
                )
                self.assertEqual(lookup.status, "invalid_limit")

        fromfile.assert_not_called()

    def test_bounded_load_does_not_follow_replaced_large_metadata(self) -> None:
        stored = self._store()
        assert stored.record is not None
        metadata_path = stored.record.metadata_path
        replacement = metadata_path.with_name("large-replacement.json")
        replacement.write_bytes(b"x" * (stem_cache_module._MAX_METADATA_BYTES + 1))
        real_open = Path.open
        replaced = False

        def replace_before_open(path: Path, *args: object, **kwargs: object):
            nonlocal replaced
            if path == metadata_path and not replaced:
                replaced = True
                replacement.replace(metadata_path)
            return real_open(path, *args, **kwargs)

        with (
            patch.object(Path, "open", new=replace_before_open),
            patch("tianlai.stem_cache.np.fromfile") as fromfile,
        ):
            lookup = self.cache.load(
                self.key,
                maximum_audio_bytes=self.audio.nbytes,
            )

        self.assertTrue(replaced)
        self.assertEqual(lookup.status, "corrupt")
        fromfile.assert_not_called()

    def test_verified_source_replays_same_descriptor_in_bounded_blocks(
        self,
    ) -> None:
        stored = self._store()
        assert stored.record is not None

        with patch(
            "tianlai.stem_cache.np.fromfile",
            side_effect=AssertionError("verified source was materialised"),
        ):
            lookup = self.cache.open_verified(self.key)
            self.assertTrue(lookup.hit)
            self.assertIsNone(lookup.audio)
            assert lookup.source is not None
            with lookup.source as source:
                blocks = tuple(source.iter_blocks(block_frames=1))

        self.assertEqual([block.shape for block in blocks], [(1, 2), (1, 2)])
        self.assertTrue(all(not block.flags.writeable for block in blocks))
        np.testing.assert_array_equal(np.concatenate(blocks), self.audio)

    def test_verified_source_keeps_open_payload_after_path_replacement(
        self,
    ) -> None:
        stored = self._store()
        assert stored.record is not None
        lookup = self.cache.open_verified(self.key)
        assert lookup.source is not None
        replacement = stored.record.audio_path.with_name("replacement.f32le")
        replacement.write_bytes(np.zeros_like(self.audio).astype("<f4").tobytes())
        try:
            replacement.replace(stored.record.audio_path)
        except OSError:
            lookup.source.close()
            self.skipTest("platform does not allow replacing an open payload")

        with lookup.source as source:
            replayed = np.concatenate(tuple(source.iter_blocks(1)))

        np.testing.assert_array_equal(replayed, self.audio)

    def test_verified_snapshot_ignores_later_in_place_cache_change(
        self,
    ) -> None:
        stored = self._store()
        assert stored.record is not None
        lookup = self.cache.open_verified(self.key)
        assert lookup.source is not None
        iterator = lookup.source.iter_blocks(block_frames=1)
        first = next(iterator)
        np.testing.assert_array_equal(first, self.audio[:1])
        with stored.record.audio_path.open("r+b", buffering=0) as target:
            target.seek(self.audio[:1].nbytes)
            target.write(np.zeros((1, 2), dtype="<f4").tobytes())

        second = tuple(iterator)
        lookup.source.close()
        np.testing.assert_array_equal(np.concatenate((first, *second)), self.audio)

    def test_verified_snapshot_rejects_silent_copy_damage(self) -> None:
        self._store()
        real_temporary_file = tempfile.TemporaryFile

        class CorruptingSnapshot:
            def __init__(self, *args: object, **kwargs: object) -> None:
                self._file = real_temporary_file(*args, **kwargs)
                self._corrupted = False

            def write(self, payload: object) -> int:
                damaged = bytearray(payload)
                if damaged and not self._corrupted:
                    damaged[0] ^= 1
                    self._corrupted = True
                return self._file.write(damaged)

            def __getattr__(self, name: str) -> object:
                return getattr(self._file, name)

        with patch(
            "tianlai.stem_cache.tempfile.TemporaryFile",
            side_effect=CorruptingSnapshot,
        ):
            lookup = self.cache.open_verified(
                self.key,
                snapshot_directory=self.root,
            )

        self.assertEqual(lookup.status, "corrupt")
        self.assertIn("snapshot digest", lookup.reason or "")
        self.assertIsNone(lookup.source)

    def test_verified_open_scans_source_and_snapshot_before_exposure(
        self,
    ) -> None:
        self._store()
        real_evidence = stem_cache_module._stream_open_audio_evidence

        with patch(
            "tianlai.stem_cache._stream_open_audio_evidence",
            wraps=real_evidence,
        ) as evidence:
            lookup = self.cache.open_verified(
                self.key,
                snapshot_directory=self.root,
            )

        self.assertTrue(lookup.hit)
        self.assertEqual(evidence.call_count, 2)
        self.assertIsNotNone(evidence.call_args_list[0].kwargs.get("snapshot"))
        self.assertIsNone(evidence.call_args_list[1].kwargs.get("snapshot"))
        assert lookup.source is not None
        with lookup.source as source:
            replayed = np.concatenate(tuple(source.iter_blocks(1)))
        np.testing.assert_array_equal(replayed, self.audio)

    def test_verified_copy_rejects_source_sha_before_returning_a_hit(
        self,
    ) -> None:
        stored = self._store()
        assert stored.record is not None
        damaged = bytearray(stored.record.audio_path.read_bytes())
        damaged[0] ^= 1
        stored.record.audio_path.write_bytes(damaged)

        lookup = self.cache.open_verified(
            self.key,
            snapshot_directory=self.root,
        )

        self.assertEqual(lookup.status, "corrupt")
        self.assertIn("audio digest differs", lookup.reason or "")

    def test_verified_copy_rejects_nonfinite_source_before_returning_a_hit(
        self,
    ) -> None:
        stored = self._store()
        assert stored.record is not None
        damaged = self.audio.copy()
        damaged[0, 0] = np.nan
        raw = damaged.astype("<f4").tobytes()
        stored.record.audio_path.write_bytes(raw)
        document = json.loads(
            stored.record.metadata_path.read_text(encoding="utf-8")
        )
        document["audio_sha256"] = hashlib.sha256(raw).hexdigest()
        stored.record.metadata_path.write_text(
            json.dumps(document),
            encoding="utf-8",
        )

        lookup = self.cache.open_verified(
            self.key,
            snapshot_directory=self.root,
        )

        self.assertEqual(lookup.status, "corrupt")
        self.assertIn("non-finite", lookup.reason or "")

    def test_verified_copy_rejects_source_identity_drift(self) -> None:
        self._store()
        real_fstat = os.fstat
        audio_fstats = 0

        def drift_on_final_source_check(descriptor: int):
            nonlocal audio_fstats
            status = real_fstat(descriptor)
            if status.st_size == self.audio.nbytes:
                audio_fstats += 1
                if audio_fstats == 3:
                    fields = list(status)
                    fields[1] = int(status.st_ino) + 1
                    return os.stat_result(fields)
            return status

        with patch(
            "tianlai.stem_cache.os.fstat",
            side_effect=drift_on_final_source_check,
        ):
            lookup = self.cache.open_verified(
                self.key,
                snapshot_directory=self.root,
            )

        self.assertEqual(lookup.status, "corrupt")
        self.assertIn("audio file changed during lookup", lookup.reason or "")

    def test_verified_consumption_rejects_nonfinite_snapshot(self) -> None:
        self._store()
        lookup = self.cache.open_verified(
            self.key,
            snapshot_directory=self.root,
        )
        assert lookup.source is not None
        private_snapshot = lookup.source._source
        private_snapshot.seek(0)
        private_snapshot.write(np.asarray([np.nan], dtype="<f4").tobytes())
        private_snapshot.flush()

        with lookup.source as source:
            with self.assertRaisesRegex(ValueError, "non-finite"):
                tuple(source.iter_blocks(1))

    def test_verified_consumption_rejects_snapshot_identity_drift(self) -> None:
        self._store()
        lookup = self.cache.open_verified(
            self.key,
            snapshot_directory=self.root,
        )
        assert lookup.source is not None
        private_descriptor = lookup.source._source.fileno()
        real_fstat = os.fstat

        def drift_snapshot_identity(descriptor: int):
            status = real_fstat(descriptor)
            if descriptor == private_descriptor:
                fields = list(status)
                fields[1] = int(status.st_ino) + 1
                return os.stat_result(fields)
            return status

        with (
            patch(
                "tianlai.stem_cache.os.fstat",
                side_effect=drift_snapshot_identity,
            ),
            lookup.source as source,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "audio file changed during consumption",
            ):
                tuple(source.iter_blocks(1))

    def test_verified_source_rechecks_snapshot_digest_on_consumption(
        self,
    ) -> None:
        self._store()
        lookup = self.cache.open_verified(
            self.key,
            snapshot_directory=self.root,
        )
        assert lookup.source is not None
        private_snapshot = lookup.source._source
        private_snapshot.seek(0)
        first_byte = private_snapshot.read(1)
        private_snapshot.seek(0)
        private_snapshot.write(bytes((first_byte[0] ^ 1,)))
        private_snapshot.flush()

        with lookup.source as source:
            with self.assertRaisesRegex(
                ValueError,
                "digest changed during consumption",
            ):
                tuple(source.iter_blocks(1))

    def test_verified_source_rejects_unbounded_or_second_consumption(
        self,
    ) -> None:
        self._store()
        lookup = self.cache.open_verified(self.key)
        assert lookup.source is not None
        with lookup.source as source:
            with self.assertRaisesRegex(ValueError, "between 1 and 65536"):
                tuple(source.iter_blocks(65_537))
            iterator = source.iter_blocks(1)
            next(iterator)
            iterator.close()
            with self.assertRaisesRegex(ValueError, "only be consumed once"):
                tuple(source.iter_blocks(1))

    def test_verified_source_materialise_returns_owned_writable_audio(
        self,
    ) -> None:
        self._store()
        lookup = self.cache.open_verified(self.key)
        assert lookup.source is not None
        with lookup.source as source:
            audio = source.materialise()

        self.assertTrue(audio.flags.owndata)
        self.assertTrue(audio.flags.writeable)
        np.testing.assert_array_equal(audio, self.audio)

    def test_verified_snapshot_refuses_to_consume_volume_reserve(
        self,
    ) -> None:
        self._store()
        with (
            patch(
                "tianlai.stem_cache.shutil.disk_usage",
                return_value=SimpleNamespace(free=0),
            ),
            patch("tianlai.stem_cache.tempfile.TemporaryFile") as temporary,
        ):
            lookup = self.cache.open_verified(
                self.key,
                snapshot_directory=self.root,
            )

        self.assertEqual(lookup.status, "unavailable")
        self.assertIn("insufficient free space", lookup.reason or "")
        temporary.assert_not_called()

    def test_verified_snapshot_rejects_linked_directory(self) -> None:
        self._store()
        real_directory = Path(self.temporary.name) / "snapshot-real"
        linked_directory = Path(self.temporary.name) / "snapshot-link"
        real_directory.mkdir()
        try:
            linked_directory.symlink_to(real_directory, target_is_directory=True)
        except OSError:
            self.skipTest("platform does not permit directory symlinks")

        with patch("tianlai.stem_cache.tempfile.TemporaryFile") as temporary:
            lookup = self.cache.open_verified(
                self.key,
                snapshot_directory=linked_directory,
            )

        self.assertEqual(lookup.status, "unavailable")
        self.assertIn("cache read failed", lookup.reason or "")
        temporary.assert_not_called()

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

    def test_existing_publication_is_verified_without_loading_a_second_stem(
        self,
    ) -> None:
        self.assertEqual(self._store().status, "stored")

        with patch(
            "tianlai.stem_cache.np.fromfile",
            side_effect=AssertionError("existing cache audio was materialised"),
        ):
            result = self._store()

        self.assertEqual(result.status, "exists")

    def test_streamed_existing_verification_rejects_nonfinite_audio(self) -> None:
        stored = self._store()
        assert stored.record is not None
        corrupted = self.audio.copy()
        corrupted[0, 0] = np.nan
        stored.record.audio_path.write_bytes(corrupted.astype("<f4").tobytes())
        document = json.loads(
            stored.record.metadata_path.read_text(encoding="utf-8")
        )
        document["audio_sha256"] = hashlib.sha256(
            corrupted.astype("<f4").tobytes()
        ).hexdigest()
        stored.record.metadata_path.write_text(
            json.dumps(document),
            encoding="utf-8",
        )

        result = self._store()

        self.assertEqual(result.status, "stored")
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

    def test_audio_growth_during_metadata_read_is_rejected_before_load(
        self,
    ) -> None:
        stored = self._store()
        assert stored.record is not None
        real_load = stem_cache_module._strict_json_load

        def parse_then_grow(raw: bytes):
            document = real_load(raw)
            with stored.record.audio_path.open("ab") as target:
                target.write(b"\x00" * (1024 * 1024))
            return document

        with (
            patch(
                "tianlai.stem_cache._strict_json_load",
                side_effect=parse_then_grow,
            ),
            patch("tianlai.stem_cache.np.fromfile") as fromfile,
        ):
            loaded = self.cache.load(self.key)

        self.assertEqual(loaded.status, "corrupt")
        fromfile.assert_not_called()

    def test_same_size_audio_replacement_is_rejected_before_load(self) -> None:
        stored = self._store()
        assert stored.record is not None
        real_load = stem_cache_module._strict_json_load

        def parse_then_replace(raw: bytes):
            document = real_load(raw)
            replacement = stored.record.audio_path.with_name("replacement")
            replacement.write_bytes(
                b"\x00" * stored.record.audio_path.stat().st_size
            )
            replacement.replace(stored.record.audio_path)
            return document

        with (
            patch(
                "tianlai.stem_cache._strict_json_load",
                side_effect=parse_then_replace,
            ),
            patch("tianlai.stem_cache.np.fromfile") as fromfile,
        ):
            loaded = self.cache.load(self.key)

        self.assertEqual(loaded.status, "corrupt")
        fromfile.assert_not_called()

    def test_key_lock_is_nonblocking_and_returns_busy(self) -> None:
        self.root.mkdir(parents=True)
        with acquire_render_lock(self.root / ".locks" / self.key):
            self.assertEqual(self._store().status, "busy")

    def test_canonical_key_and_source_digest_helpers(self) -> None:
        self.assertEqual(build_cache_key({"a": 1, "b": [2]}), build_cache_key({"b": [2], "a": 1}))
        self.assertTrue(current_source_tree_matches())
        self.assertEqual(source_tree_digest(), PROCESS_SOURCE_TREE_SHA256)

    def test_source_tree_digest_preserves_the_frozen_byte_contract(self) -> None:
        source_root = Path(self.temporary.name) / "source"
        files = {
            "tianlai/__init__.py": b'NAME = "tianlai"\n',
            "tianlai/nested/module.py": b"VALUE = 1\n",
        }
        for relative, payload in files.items():
            path = source_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)

        expected = hashlib.sha256()
        for relative, payload in sorted(files.items()):
            encoded_name = relative.encode("utf-8")
            expected.update(len(encoded_name).to_bytes(4, "big"))
            expected.update(encoded_name)
            expected.update(len(payload).to_bytes(8, "big"))
            expected.update(payload)

        first = source_tree_digest(source_root)
        self.assertEqual(first, expected.hexdigest())

        # Same-length edits must still be detected; this is deliberately a
        # full content hash rather than a size/mtime shortcut.
        changed = source_root / "tianlai" / "nested" / "module.py"
        changed.write_bytes(b"VALUE = 2\n")
        self.assertNotEqual(source_tree_digest(source_root), first)


if __name__ == "__main__":
    unittest.main()
