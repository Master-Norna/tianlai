from __future__ import annotations

import hashlib
import gc
import math
import os
import tempfile
from typing import BinaryIO
import unittest
from unittest.mock import patch

import numpy as np

from tianlai.stem_source import (
    MAX_STEM_BLOCK_FRAMES,
    OwnedStemSource,
    StemBlockSource,
    StemSourceError,
)


def _audio_bytes(audio: np.ndarray) -> bytes:
    return np.asarray(audio, dtype="<f4", order="C").tobytes(order="C")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _CloseRaises:
    def __init__(self, source: BinaryIO) -> None:
        self.source = source

    def fileno(self) -> int:
        return self.source.fileno()

    def seek(self, *args: object) -> int:
        return self.source.seek(*args)

    def read(self, size: int = -1) -> bytes:
        return self.source.read(size)

    def close(self) -> None:
        self.source.close()
        raise OSError("injected close failure")


class OwnedStemSourceTests(unittest.TestCase):
    @staticmethod
    def _source(
        audio: np.ndarray,
        *,
        prefix: bytes = b"",
        suffix: bytes = b"",
        expected_sha256: str | None = None,
        callback: object | None = None,
    ) -> tuple[OwnedStemSource, BinaryIO, bytes]:
        payload = _audio_bytes(audio)
        raw = tempfile.TemporaryFile(mode="w+b")
        raw.write(prefix + payload + suffix)
        raw.flush()
        source = OwnedStemSource(
            raw,
            audio_offset=len(prefix),
            frame_count=int(audio.shape[0]),
            expected_sha256=(
                _sha256(payload)
                if expected_sha256 is None
                else expected_sha256
            ),
            completion_callback=callback,  # type: ignore[arg-type]
        )
        return source, raw, payload

    def test_offset_span_yields_immutable_bounded_blocks_and_completes_on_close(
        self,
    ) -> None:
        audio = np.arange(20, dtype=np.float32).reshape(10, 2) / 20.0
        completions: list[bool] = []
        source, raw, _payload = self._source(
            audio,
            prefix=b"protocol-prefix",
            suffix=b"protocol-suffix",
            callback=completions.append,
        )

        blocks = tuple(source.iter_blocks(3))

        self.assertEqual([block.shape[0] for block in blocks], [3, 3, 3, 1])
        self.assertTrue(all(block.dtype == np.dtype("<f4") for block in blocks))
        self.assertTrue(all(not block.flags.writeable for block in blocks))
        np.testing.assert_array_equal(np.concatenate(blocks), audio)
        self.assertTrue(source.verified)
        self.assertFalse(source.closed)
        self.assertEqual(completions, [])

        source.close()
        self.assertTrue(source.closed)
        self.assertTrue(raw.closed)
        self.assertEqual(completions, [True])
        source.close()
        self.assertEqual(completions, [True])

    def test_materialise_is_owned_writable_and_mutually_exclusive(self) -> None:
        audio = np.linspace(-0.75, 0.75, 18, dtype=np.float32).reshape(9, 2)
        completions: list[bool] = []
        source, _raw, _payload = self._source(
            audio,
            callback=completions.append,
        )

        materialised = source.materialise()

        np.testing.assert_array_equal(materialised, audio)
        self.assertTrue(materialised.flags.owndata)
        self.assertTrue(materialised.flags.writeable)
        materialised[0, 0] = 0.125
        with self.assertRaisesRegex(ValueError, "only be consumed once"):
            source.iter_blocks()
        source.close()
        self.assertEqual(completions, [True])

    def test_invalid_block_size_does_not_consume_or_complete_source(self) -> None:
        audio = np.zeros((2, 2), dtype=np.float32)
        completions: list[bool] = []
        source, _raw, _payload = self._source(
            audio,
            callback=completions.append,
        )

        for invalid in (True, 0, -1, MAX_STEM_BLOCK_FRAMES + 1, 1.5):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    source.iter_blocks(invalid)  # type: ignore[arg-type]
        np.testing.assert_array_equal(
            np.concatenate(tuple(source.iter_blocks(1))),
            audio,
        )
        source.close()
        self.assertEqual(completions, [True])

    def test_abandoned_iterator_closes_source_and_reports_failure_once(self) -> None:
        audio = np.ones((8, 2), dtype=np.float32) * 0.25
        completions: list[bool] = []
        source, raw, _payload = self._source(
            audio,
            callback=completions.append,
        )
        blocks = source.iter_blocks(2)
        next(blocks)

        blocks.close()

        self.assertTrue(source.closed)
        self.assertTrue(raw.closed)
        self.assertEqual(completions, [False])
        source.close()
        self.assertEqual(completions, [False])

    def test_unconsumed_close_reports_failure(self) -> None:
        completions: list[bool] = []
        source, raw, _payload = self._source(
            np.empty((0, 2), dtype=np.float32),
            callback=completions.append,
        )

        source.close()

        self.assertTrue(raw.closed)
        self.assertEqual(completions, [False])

    def test_zero_length_source_can_verify_successfully(self) -> None:
        completions: list[bool] = []
        source, _raw, _payload = self._source(
            np.empty((0, 2), dtype=np.float32),
            callback=completions.append,
        )

        self.assertEqual(tuple(source.iter_blocks()), ())
        self.assertTrue(source.verified)
        source.close()
        self.assertEqual(completions, [True])

    def test_nonfinite_sample_wins_over_later_digest_error(self) -> None:
        audio = np.array([[0.0, math.nan], [0.25, -0.25]], dtype=np.float32)
        completions: list[bool] = []
        source, raw, _payload = self._source(
            audio,
            expected_sha256="0" * 64,
            callback=completions.append,
        )

        with self.assertRaisesRegex(StemSourceError, "non-finite"):
            tuple(source.iter_blocks(1))

        self.assertTrue(source.closed)
        self.assertTrue(raw.closed)
        self.assertEqual(completions, [False])

    def test_digest_mismatch_closes_and_reports_failure(self) -> None:
        audio = np.ones((3, 2), dtype=np.float32)
        completions: list[bool] = []
        source, raw, _payload = self._source(
            audio,
            expected_sha256="0" * 64,
            callback=completions.append,
        )

        with self.assertRaisesRegex(StemSourceError, "SHA-256"):
            tuple(source.iter_blocks())

        self.assertTrue(raw.closed)
        self.assertEqual(completions, [False])

    def test_truncation_after_construction_is_rejected(self) -> None:
        audio = np.ones((6, 2), dtype=np.float32)
        completions: list[bool] = []
        source, raw, _payload = self._source(
            audio,
            callback=completions.append,
        )
        raw.truncate(8)
        raw.flush()

        with self.assertRaisesRegex(StemSourceError, "truncated"):
            tuple(source.iter_blocks())

        self.assertEqual(completions, [False])

    def test_total_file_growth_is_rejected_even_when_audio_span_is_unchanged(
        self,
    ) -> None:
        audio = np.ones((4, 2), dtype=np.float32)
        completions: list[bool] = []
        source, raw, _payload = self._source(
            audio,
            callback=completions.append,
        )
        raw.seek(0, os.SEEK_END)
        raw.write(b"unexpected-tail")
        raw.flush()

        with self.assertRaisesRegex(StemSourceError, "length changed"):
            tuple(source.iter_blocks())

        self.assertEqual(completions, [False])

    def test_materialisation_allocation_failure_releases_lease(self) -> None:
        audio = np.ones((4, 2), dtype=np.float32)
        completions: list[bool] = []
        source, raw, _payload = self._source(
            audio,
            callback=completions.append,
        )

        with patch(
            "tianlai.stem_source.np.empty",
            side_effect=MemoryError("injected allocation failure"),
        ):
            with self.assertRaises(MemoryError):
                source.materialise()

        self.assertTrue(raw.closed)
        self.assertEqual(completions, [False])

    def test_never_started_iterator_close_releases_lease(self) -> None:
        completions: list[bool] = []
        source, raw, _payload = self._source(
            np.ones((4, 2), dtype=np.float32),
            callback=completions.append,
        )

        blocks = source.iter_blocks(2)
        blocks.close()

        self.assertTrue(source.closed)
        self.assertTrue(raw.closed)
        self.assertEqual(completions, [False])
        blocks.close()
        self.assertEqual(completions, [False])

    def test_never_started_iterator_gc_releases_lease(self) -> None:
        completions: list[bool] = []
        source, raw, _payload = self._source(
            np.ones((4, 2), dtype=np.float32),
            callback=completions.append,
        )

        blocks = source.iter_blocks(2)
        del blocks
        gc.collect()

        self.assertTrue(source.closed)
        self.assertTrue(raw.closed)
        self.assertEqual(completions, [False])

    def test_context_body_error_remains_primary_when_callback_also_fails(
        self,
    ) -> None:
        calls: list[bool] = []

        def failing_callback(success: bool) -> None:
            calls.append(success)
            raise OSError("callback failure")

        source, raw, _payload = self._source(
            np.ones((2, 2), dtype=np.float32),
            callback=failing_callback,
        )

        with self.assertRaisesRegex(RuntimeError, "primary consumer failure"):
            with source:
                next(source.iter_blocks(1))
                raise RuntimeError("primary consumer failure")

        self.assertTrue(raw.closed)
        self.assertEqual(calls, [False])

    def test_successful_callback_error_occurs_after_descriptor_close_once(self) -> None:
        states: list[tuple[bool, bool]] = []
        raw_holder: list[BinaryIO] = []

        def failing_callback(success: bool) -> None:
            states.append((success, raw_holder[0].closed))
            raise OSError("callback failure")

        source, raw, _payload = self._source(
            np.ones((2, 2), dtype=np.float32),
            callback=failing_callback,
        )
        raw_holder.append(raw)
        tuple(source.iter_blocks())

        with self.assertRaisesRegex(OSError, "callback failure"):
            source.close()
        source.close()

        self.assertEqual(states, [(True, True)])

    def test_descriptor_close_error_still_notifies_callback(self) -> None:
        audio = np.ones((2, 2), dtype=np.float32)
        payload = _audio_bytes(audio)
        raw = tempfile.TemporaryFile(mode="w+b")
        raw.write(payload)
        raw.flush()
        completions: list[bool] = []
        source = OwnedStemSource(
            _CloseRaises(raw),  # type: ignore[arg-type]
            audio_offset=0,
            frame_count=2,
            expected_sha256=_sha256(payload),
            completion_callback=completions.append,
        )
        tuple(source.iter_blocks())

        with self.assertRaisesRegex(OSError, "close failure"):
            source.close()

        self.assertEqual(completions, [False])
        self.assertTrue(raw.closed)

    def test_failed_constructor_does_not_take_file_or_callback_ownership(self) -> None:
        raw = tempfile.TemporaryFile(mode="w+b")
        raw.write(b"short")
        raw.flush()
        completions: list[bool] = []

        with self.assertRaisesRegex(ValueError, "span exceeds"):
            OwnedStemSource(
                raw,
                audio_offset=0,
                frame_count=10,
                expected_sha256="0" * 64,
                completion_callback=completions.append,
            )

        self.assertFalse(raw.closed)
        self.assertEqual(completions, [])
        raw.close()

    def test_structurally_implements_common_block_source_protocol(self) -> None:
        source, _raw, _payload = self._source(
            np.zeros((1, 2), dtype=np.float32)
        )
        try:
            self.assertIsInstance(source, StemBlockSource)
        finally:
            source.close()


if __name__ == "__main__":
    unittest.main()
