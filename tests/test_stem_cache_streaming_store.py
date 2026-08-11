from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai import stem_cache as stem_cache_module
from tianlai.render_lock import acquire_render_lock
from tianlai.stem_cache import StemCache


_KEY = "a" * 64
_MANIFEST_SHA256 = "b" * 64
_RACING_SENTINEL = b"replacement installed by a racing writer"


def _payload(audio: np.ndarray) -> bytes:
    return np.asarray(audio, dtype="<f4", order="C").tobytes(order="C")


def _sha256(audio: np.ndarray) -> str:
    return hashlib.sha256(_payload(audio)).hexdigest()


class StreamingStemCacheStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "cache"
        self.cache = StemCache(self.root)
        self.audio = np.linspace(
            -0.5,
            0.5,
            18,
            dtype=np.float32,
        ).reshape(9, 2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _begin(self, *, stage: str = "instrument"):
        return self.cache.begin_streaming_store(
            _KEY,
            stage=stage,
            sample_rate=48_000,
            peak_voices=3,
            manifest_sha256=_MANIFEST_SHA256,
        )

    def _append_all(self, transaction, audio: np.ndarray | None = None) -> None:
        selected = self.audio if audio is None else audio
        transaction.append(selected[:4])
        transaction.append(selected[4:])

    def _finish(self, transaction, audio: np.ndarray | None = None):
        selected = self.audio if audio is None else audio
        return transaction.finish(len(selected), _sha256(selected))

    def _assert_zero_byte_diagnostic(self, path: Path) -> None:
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_size, 0)

    def _public_store(self, audio: np.ndarray | None = None):
        selected = self.audio if audio is None else audio
        return self.cache.store(
            _KEY,
            selected,
            stage="instrument",
            sample_rate=48_000,
            peak_voices=3,
            manifest_sha256=_MANIFEST_SHA256,
        )

    def test_streaming_store_publishes_exact_audio_and_metadata(self) -> None:
        transaction = self._begin()
        temporary_path = transaction.temporary_path
        assert temporary_path is not None and temporary_path.exists()

        self._append_all(transaction)
        result = self._finish(transaction)

        self.assertEqual(result.status, "stored")
        self.assertFalse(temporary_path.exists())
        loaded = self.cache.load(_KEY)
        self.assertTrue(loaded.hit)
        np.testing.assert_array_equal(loaded.audio, self.audio)
        assert loaded.record is not None
        self.assertEqual(
            loaded.record.metadata["audio_sha256"],
            _sha256(self.audio),
        )
        self.assertEqual(loaded.record.metadata["frame_count"], len(self.audio))

    def test_begin_and_append_do_not_hold_key_lock(self) -> None:
        transaction = self._begin()
        self._append_all(transaction)

        with acquire_render_lock(self.root / ".locks" / _KEY):
            result = self._finish(transaction)

        self.assertEqual(result.status, "busy")
        assert transaction.temporary_path is not None
        self._assert_zero_byte_diagnostic(transaction.temporary_path)
        self.assertEqual(self.cache.load(_KEY).status, "missing")

    def test_same_existing_entry_is_reported_without_materialising_it(self) -> None:
        self.assertEqual(self._public_store().status, "stored")
        transaction = self._begin()
        self._append_all(transaction)

        with patch(
            "tianlai.stem_cache.np.fromfile",
            side_effect=AssertionError("existing audio was materialised"),
        ):
            result = self._finish(transaction)

        self.assertEqual(result.status, "exists")
        np.testing.assert_array_equal(self.cache.load(_KEY).audio, self.audio)

    def test_same_audio_repairs_only_stale_semantic_metadata(self) -> None:
        stored = self._public_store()
        assert stored.record is not None
        metadata = json.loads(
            stored.record.metadata_path.read_text(encoding="utf-8")
        )
        metadata["stage"] = "stale-stage"
        stored.record.metadata_path.write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )
        self.assertTrue(self.cache.load(_KEY).hit)
        transaction = self._begin()
        self._append_all(transaction)

        result = self._finish(transaction)

        self.assertEqual(result.status, "repaired")
        loaded = self.cache.load(_KEY)
        assert loaded.record is not None
        self.assertEqual(loaded.record.metadata["stage"], "instrument")
        np.testing.assert_array_equal(loaded.audio, self.audio)

    def test_valid_existing_different_audio_is_preserved_as_conflict(self) -> None:
        self.assertEqual(self._public_store().status, "stored")
        other = self.audio.copy()
        other[0, 0] = 0.75
        transaction = self._begin()
        self._append_all(transaction, other)

        result = self._finish(transaction, other)

        self.assertEqual(result.status, "conflict")
        np.testing.assert_array_equal(self.cache.load(_KEY).audio, self.audio)

    def test_corrupt_existing_entry_is_replaced(self) -> None:
        stored = self._public_store()
        assert stored.record is not None
        corrupted = self.audio.copy()
        corrupted[0, 0] = np.nan
        stored.record.audio_path.write_bytes(_payload(corrupted))
        metadata = json.loads(
            stored.record.metadata_path.read_text(encoding="utf-8")
        )
        metadata["audio_sha256"] = _sha256(corrupted)
        stored.record.metadata_path.write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )
        self.assertEqual(self.cache.load(_KEY).status, "corrupt")
        transaction = self._begin()
        self._append_all(transaction)

        result = self._finish(transaction)

        self.assertEqual(result.status, "stored")
        np.testing.assert_array_equal(self.cache.load(_KEY).audio, self.audio)

    def test_nonfinite_block_disables_only_cache_transaction(self) -> None:
        invalid = self.audio.copy()
        invalid[2, 1] = np.nan
        transaction = self._begin()
        temporary_path = transaction.temporary_path
        assert temporary_path is not None

        transaction.append(invalid)
        result = transaction.finish(len(invalid), _sha256(invalid))

        self.assertEqual(result.status, "invalid_input")
        self.assertIn("NaN", result.reason or "")
        self._assert_zero_byte_diagnostic(temporary_path)
        self.assertEqual(self.cache.load(_KEY).status, "missing")

    def test_invalid_dtype_and_oversized_block_are_bounded(self) -> None:
        for block in (
            self.audio.astype(np.float64),
            np.zeros((65_537, 2), dtype="<f4"),
            np.zeros((1, 1), dtype="<f4"),
        ):
            with self.subTest(shape=block.shape, dtype=str(block.dtype)):
                transaction = self._begin()
                transaction.append(block)
                result = transaction.finish(0, hashlib.sha256(b"").hexdigest())
                self.assertEqual(result.status, "invalid_input")

    def test_short_excess_and_digest_mismatch_never_publish(self) -> None:
        cases = (
            (self.audio[:4], len(self.audio), _sha256(self.audio), "short"),
            (self.audio, 4, _sha256(self.audio[:4]), "excess"),
            (self.audio, len(self.audio), "0" * 64, "SHA-256"),
        )
        for block, frames, digest, reason in cases:
            with self.subTest(reason=reason):
                transaction = self._begin()
                transaction.append(block)
                result = transaction.finish(frames, digest)
                self.assertEqual(result.status, "invalid_input")
                self.assertIn(reason, result.reason or "")
                self.assertEqual(self.cache.load(_KEY).status, "missing")

    def test_disk_write_failure_is_structured_and_render_can_continue(self) -> None:
        transaction = self._begin()
        temporary_path = transaction.temporary_path
        assert temporary_path is not None

        with patch(
            "tianlai.stem_cache._write_stream_payload",
            side_effect=OSError("disk full"),
        ):
            transaction.append(self.audio)

        # The authoritative raw block remains caller-owned and unchanged.
        np.testing.assert_array_equal(
            self.audio,
            np.linspace(-0.5, 0.5, 18, dtype=np.float32).reshape(9, 2),
        )
        result = self._finish(transaction)
        self.assertEqual(result.status, "write_error")
        self.assertIn("disk full", result.reason or "")
        self._assert_zero_byte_diagnostic(temporary_path)

    def test_memory_error_propagates_and_aborts_private_temp(self) -> None:
        transaction = self._begin()
        temporary_path = transaction.temporary_path
        assert temporary_path is not None

        with patch(
            "tianlai.stem_cache._write_stream_payload",
            side_effect=MemoryError("host pressure"),
        ):
            with self.assertRaises(MemoryError):
                transaction.append(self.audio)

        self._assert_zero_byte_diagnostic(temporary_path)

    def test_fsync_failure_is_write_error_without_publication(self) -> None:
        transaction = self._begin()
        self._append_all(transaction)
        temporary_path = transaction.temporary_path
        assert temporary_path is not None

        with patch(
            "tianlai.stem_cache.os.fsync",
            side_effect=OSError("flush failed"),
        ):
            result = self._finish(transaction)

        self.assertEqual(result.status, "write_error")
        self._assert_zero_byte_diagnostic(temporary_path)
        self.assertEqual(self.cache.load(_KEY).status, "missing")

    def test_metadata_failure_leaves_audio_without_commit_marker(self) -> None:
        transaction = self._begin()
        self._append_all(transaction)
        audio_path = self.root / "v1" / _KEY[:2] / f"{_KEY}.f32le"
        metadata_path = self.root / "v1" / _KEY[:2] / f"{_KEY}.json"

        with patch.object(
            StemCache,
            "_write_atomic",
            side_effect=OSError("metadata commit failed"),
        ):
            result = self._finish(transaction)

        self.assertEqual(result.status, "write_error")
        self.assertEqual(audio_path.read_bytes(), _payload(self.audio))
        self.assertFalse(metadata_path.exists())
        self.assertEqual(self.cache.load(_KEY).status, "incomplete")

    def test_failed_audio_replace_preserves_both_racing_entries(self) -> None:
        transaction = self._begin()
        self._append_all(transaction)
        audio_path = self.root / "v1" / _KEY[:2] / f"{_KEY}.f32le"
        preserved_writer_entry = self.root / "writer-entry-before-race"
        observed_temporary: list[Path] = []

        def fail_after_path_replacement(source: object, target: object) -> None:
            source_path = Path(source)
            target_path = Path(target)
            # The destination does not exist yet, so compare its exact entry
            # name and bind the existing parent directory by identity.  A
            # lexical comparison rejects the harmless Windows RUNNER~1 alias;
            # name + parent identity still rejects every wrong cache target.
            self.assertEqual(target_path.name, audio_path.name)
            self.assertTrue(
                os.path.samefile(target_path.parent, audio_path.parent),
                (
                    "streaming cache attempted publication in another "
                    f"directory: {target_path.parent} != {audio_path.parent}"
                ),
            )
            source_path.rename(preserved_writer_entry)
            source_path.write_bytes(_RACING_SENTINEL)
            observed_temporary.append(source_path)
            raise PermissionError("injected publication failure")

        with patch.object(
            stem_cache_module.os,
            "replace",
            side_effect=fail_after_path_replacement,
        ):
            result = self._finish(transaction)

        self.assertEqual(result.status, "write_error")
        self.assertEqual(preserved_writer_entry.read_bytes(), _payload(self.audio))
        self.assertEqual(len(observed_temporary), 1)
        self.assertEqual(observed_temporary[0].read_bytes(), _RACING_SENTINEL)
        self.assertFalse(audio_path.exists())

    def test_temp_identity_replacement_is_preserved_and_never_published(self) -> None:
        transaction = self._begin()
        self._append_all(transaction)
        temporary_path = transaction.temporary_path
        assert temporary_path is not None
        original = temporary_path.with_name("original-private-temp")
        try:
            temporary_path.rename(original)
        except OSError:
            transaction.abort()
            self.skipTest("platform does not allow replacing an open temp")
        temporary_path.write_bytes(_RACING_SENTINEL)

        result = self._finish(transaction)

        self.assertEqual(result.status, "write_error")
        self.assertEqual(temporary_path.read_bytes(), _RACING_SENTINEL)
        # Cleanup can safely truncate only the still-open original inode.  It
        # must never unlink or modify the replacement at the old pathname.
        self.assertEqual(original.read_bytes(), b"")
        self.assertEqual(self.cache.load(_KEY).status, "missing")

    def test_abort_never_touches_a_replacement_at_the_private_name(self) -> None:
        transaction = self._begin()
        transaction.append(self.audio[:2])
        temporary_path = transaction.temporary_path
        assert temporary_path is not None
        original = temporary_path.with_name("abort-original-private-temp")
        try:
            temporary_path.rename(original)
        except OSError:
            transaction.abort()
            self.skipTest("platform does not allow replacing an open temp")
        temporary_path.write_bytes(_RACING_SENTINEL)

        transaction.abort()

        self.assertEqual(temporary_path.read_bytes(), _RACING_SENTINEL)
        self.assertEqual(original.read_bytes(), b"")

    def test_abort_truncates_only_its_open_private_temp(self) -> None:
        transaction = self._begin()
        transaction.append(self.audio[:2])
        temporary_path = transaction.temporary_path
        assert temporary_path is not None and temporary_path.exists()

        transaction.abort()
        transaction.abort()

        self._assert_zero_byte_diagnostic(temporary_path)
        self.assertEqual(self.cache.load(_KEY).status, "missing")
        with self.assertRaisesRegex(ValueError, "aborted"):
            transaction.finish(2, _sha256(self.audio[:2]))

    @unittest.skipUnless(
        os.name != "nt" and "fork" in multiprocessing.get_all_start_methods(),
        "requires POSIX fork descriptor inheritance",
    )
    def test_fork_child_abort_cannot_truncate_parent_transaction(self) -> None:
        transaction = self._begin()
        transaction.append(self.audio[:4])
        context = multiprocessing.get_context("fork")
        child = context.Process(target=transaction.abort)
        child.start()
        child.join(timeout=5)
        self.assertFalse(child.is_alive())
        self.assertEqual(child.exitcode, 0)

        transaction.append(self.audio[4:])
        result = self._finish(transaction)

        self.assertEqual(result.status, "stored")
        loaded = self.cache.load(_KEY)
        self.assertTrue(loaded.hit)
        np.testing.assert_array_equal(loaded.audio, self.audio)

    def test_invalid_begin_is_nonthrowing_and_creates_no_temp(self) -> None:
        transaction = self.cache.begin_streaming_store(
            "bad-key",
            stage="instrument",
            sample_rate=48_000,
            peak_voices=0,
            manifest_sha256=_MANIFEST_SHA256,
        )

        transaction.append(self.audio)
        result = transaction.finish(len(self.audio), _sha256(self.audio))

        self.assertEqual(result.status, "invalid_input")
        self.assertIsNone(transaction.temporary_path)
        self.assertFalse(self.root.exists())


if __name__ == "__main__":
    unittest.main()
