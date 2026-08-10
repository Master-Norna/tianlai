from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai import analysis_cache
from tianlai import license_sidecar
from tianlai import post_render_check
from tianlai import stem_cache


_RACING_SENTINEL = b"entry installed by a racing writer"


class AtomicWriteFailurePreservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _assert_failed_replace_preserves_both_entries(
        self,
        module,
        destination: Path,
        write,
        expected_temporary_payload: bytes,
    ) -> None:
        preserved_writer_entry = self.root / "writer-entry-before-race"
        observed_temporary: list[Path] = []

        def fail_after_path_replacement(source, target) -> None:
            source_path = Path(source)
            self.assertEqual(Path(target), destination)
            source_path.rename(preserved_writer_entry)
            source_path.write_bytes(_RACING_SENTINEL)
            observed_temporary.append(source_path)
            raise PermissionError("injected atomic publication failure")

        with patch.object(module.os, "replace", side_effect=fail_after_path_replacement):
            with self.assertRaisesRegex(
                PermissionError,
                "injected atomic publication failure",
            ):
                write()

        self.assertEqual(len(observed_temporary), 1)
        self.assertFalse(destination.exists())
        self.assertEqual(
            preserved_writer_entry.read_bytes(),
            expected_temporary_payload,
        )
        self.assertEqual(observed_temporary[0].read_bytes(), _RACING_SENTINEL)

    def test_license_sidecar_failure_preserves_racing_entry(self) -> None:
        destination = self.root / "sidecar" / "licenses.json"
        payload = b'{"license":"test"}\n'
        self._assert_failed_replace_preserves_both_entries(
            license_sidecar,
            destination,
            lambda: license_sidecar._atomic_write(destination, payload),
            payload,
        )

    def test_post_render_check_failure_preserves_racing_entry(self) -> None:
        destination = self.root / "post-render-check.json"
        with patch.object(post_render_check, "validate_post_render_check"):
            self._assert_failed_replace_preserves_both_entries(
                post_render_check,
                destination,
                lambda: post_render_check.write_post_render_check(
                    destination,
                    {},
                ),
                b"{}\n",
            )

    def test_stem_metadata_failure_preserves_racing_entry(self) -> None:
        destination = self.root / "stem" / "entry.json"
        destination.parent.mkdir()
        payload = b'{"stem":"metadata"}\n'
        self._assert_failed_replace_preserves_both_entries(
            stem_cache,
            destination,
            lambda: stem_cache.StemCache._write_atomic(destination, payload),
            payload,
        )

    def test_stem_audio_failure_preserves_racing_entry(self) -> None:
        destination = self.root / "stem-audio" / "entry.f32le"
        destination.parent.mkdir()
        audio = np.array(
            [[0.0, 0.25], [-0.5, 1.0]],
            dtype="<f4",
        )
        self._assert_failed_replace_preserves_both_entries(
            stem_cache,
            destination,
            lambda: stem_cache.StemCache._write_audio_atomic(
                destination,
                audio,
            ),
            audio.tobytes(),
        )

    def test_analysis_cache_failure_preserves_racing_entry(self) -> None:
        destination = self.root / "analysis" / "entry.json"
        destination.parent.mkdir()
        payload = b'{"analysis":"result"}\n'
        self._assert_failed_replace_preserves_both_entries(
            analysis_cache,
            destination,
            lambda: analysis_cache.CollaborationAnalysisCache._write_atomic(
                destination,
                payload,
            ),
            payload,
        )


if __name__ == "__main__":
    unittest.main()
