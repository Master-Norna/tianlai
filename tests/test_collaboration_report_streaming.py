from __future__ import annotations

from copy import deepcopy
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import numpy as np

from tianlai import collaboration_report as report_module
from tianlai.collaboration_report import CollaborationReportBuilder
from tianlai.roster import (
    BalanceRelation,
    CollaborationAnalysis,
    CollaborationSettings,
)


SAMPLE_RATE = 8_000


def _executor(executor_id: str, part_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        executor_id=executor_id,
        part_id=part_id,
        capability=SimpleNamespace(relative_path=f"test/{executor_id}"),
        gain_db=-3.0,
        pan=0.0,
        role=None,
    )


def _settings(*, relations: bool = True) -> CollaborationSettings:
    return CollaborationSettings(
        mode="analyze",
        analysis=CollaborationAnalysis(
            window_ms=200.0,
            hop_ms=100.0,
            gate_dbfs=-60.0,
        ),
        balance_relations=(
            (BalanceRelation("pad", "lead", -6.0, 0.25, 4.0),)
            if relations
            else ()
        ),
        declared=True,
    )


def _sine(amplitude: float, seconds: float = 1.0) -> np.ndarray:
    time = np.arange(round(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    mono = amplitude * np.sin(2.0 * np.pi * 440.0 * time)
    return np.column_stack((mono, mono)).astype(np.float32)


def _sha256(audio: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(audio, dtype=np.float32)
    return hashlib.sha256(memoryview(contiguous).cast("B")).hexdigest()


def _append_irregular(transaction: object, audio: np.ndarray) -> None:
    starts = range(0, int(audio.shape[0]), 317)
    for start in starts:
        transaction.append(audio[start : start + 317])


class CollaborationReportStreamingTests(unittest.TestCase):
    def _assert_abort_only_rejects_followup_work(
        self,
        builder: CollaborationReportBuilder,
        audio: np.ndarray,
    ) -> None:
        executor = _executor("lead-after-failure", "lead")
        with self.assertRaisesRegex(RuntimeError, "abort-only"):
            builder.add_stem(executor, audio)
        with self.assertRaisesRegex(RuntimeError, "abort-only"):
            builder._begin_stem_transaction(
                executor,
                frame_count=len(audio),
            )
        with self.assertRaisesRegex(RuntimeError, "abort-only"):
            builder.build()
        self.assertTrue(builder._closed)

    def test_streamed_and_array_reports_are_field_exact_and_retain_relations(
        self,
    ) -> None:
        pad = _sine(0.1, 2.0)
        lead = _sine(0.2, 2.0)
        with (
            tempfile.TemporaryDirectory() as array_temporary,
            tempfile.TemporaryDirectory() as stream_temporary,
        ):
            array_builder = CollaborationReportBuilder(
                _settings(),
                SAMPLE_RATE,
                scratch_parent=array_temporary,
            )
            array_builder.add_stem(_executor("pad", "pad"), pad)
            array_builder.add_stem(_executor("lead", "lead"), lead)
            expected = array_builder.build()

            stream_root = Path(stream_temporary)
            stream_builder = CollaborationReportBuilder(
                _settings(),
                SAMPLE_RATE,
                scratch_parent=stream_root,
            )
            pad_transaction = stream_builder._begin_stem_transaction(
                _executor("pad", "pad"),
                frame_count=len(pad),
                expected_audio_sha256=_sha256(pad),
            )
            retained_mapping = pad_transaction._audio
            _append_irregular(pad_transaction, pad)
            self.assertEqual(pad_transaction.finish(), _sha256(pad))
            self.assertTrue(pad_transaction.closed)
            self.assertTrue(pad_transaction.retained)
            self.assertIs(
                stream_builder._part_buffers["pad"],
                retained_mapping,
            )

            lead_transaction = stream_builder._begin_stem_transaction(
                _executor("lead", "lead"),
                frame_count=len(lead),
                expected_audio_sha256=_sha256(lead),
            )
            _append_irregular(lead_transaction, lead)
            lead_transaction.finish()
            actual = stream_builder.build()

            self.assertEqual(actual, expected)
            self.assertEqual(list(stream_root.iterdir()), [])

    def test_nonrelation_mapping_is_released_immediately_after_diagnostics(
        self,
    ) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=root,
            )
            transaction = builder._begin_stem_transaction(
                _executor("texture", "texture"),
                frame_count=len(audio),
                expected_audio_sha256=_sha256(audio),
            )
            mapping = transaction._audio
            _append_irregular(transaction, audio)
            transaction.finish()

            self.assertFalse(transaction.retained)
            self.assertTrue(transaction.closed)
            self.assertEqual(builder._scratch_handles, [])
            self.assertEqual(list(root.iterdir()), [])
            assert mapping is not None
            self.assertTrue(mapping._mmap.closed)
            self.assertEqual(builder.build()["summary"]["stem_count"], 1)

    def test_finish_view_owns_nonrelation_mapping_until_caller_close(self) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            transaction = builder._begin_stem_transaction(
                _executor("texture", "texture"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            mapping = transaction._audio

            view = transaction.finish_view()

            self.assertFalse(view.builder_owned)
            self.assertFalse(view.closed)
            self.assertIs(view.audio, mapping)
            assert mapping is not None
            self.assertFalse(mapping._mmap.closed)
            self.assertEqual(builder._scratch_handles, [])
            view.close()
            self.assertTrue(view.closed)
            self.assertTrue(mapping._mmap.closed)
            self.assertEqual(builder.build()["summary"]["stem_count"], 1)

    def test_finish_view_context_cleanup_does_not_mask_body_error(self) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            transaction = builder._begin_stem_transaction(
                _executor("texture", "texture"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            view = transaction.finish_view()
            mapping = view.audio
            real_close = report_module._close_private_stem_scratch

            def close_then_fail(*args: object, **kwargs: object) -> None:
                real_close(*args, **kwargs)
                raise OSError("injected view cleanup failure")

            with mock.patch(
                "tianlai.collaboration_report._close_private_stem_scratch",
                side_effect=close_then_fail,
            ):
                with self.assertRaisesRegex(RuntimeError, "body failure"):
                    with view:
                        raise RuntimeError("body failure")

            self.assertTrue(view.closed)
            self.assertTrue(mapping._mmap.closed)
            builder.close()

    def test_finish_view_borrows_first_relation_mapping_from_builder(self) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            transaction = builder._begin_stem_transaction(
                _executor("pad", "pad"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            mapping = transaction._audio

            view = transaction.finish_view()

            self.assertTrue(view.builder_owned)
            self.assertIs(view.audio, mapping)
            self.assertIs(builder._part_buffers["pad"], mapping)
            view.close()
            assert mapping is not None
            self.assertFalse(mapping._mmap.closed)
            builder.close()
            self.assertTrue(mapping._mmap.closed)

    def test_second_executor_aggregates_then_releases_its_mapping(self) -> None:
        first = _sine(0.025)
        second = _sine(0.075)
        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            first_transaction = builder._begin_stem_transaction(
                _executor("pad-a", "pad"),
                frame_count=len(first),
            )
            first_transaction.append(first)
            first_transaction.finish()
            retained = builder._part_buffers["pad"]

            second_transaction = builder._begin_stem_transaction(
                _executor("pad-b", "pad"),
                frame_count=len(second),
            )
            second_mapping = second_transaction._audio
            second_transaction.append(second)
            second_transaction.finish()

            self.assertFalse(second_transaction.retained)
            assert second_mapping is not None
            self.assertTrue(second_mapping._mmap.closed)
            np.testing.assert_array_equal(retained, first + second)
            builder.close()

    def test_relation_shape_failure_is_preflight_atomic_and_abort_only(
        self,
    ) -> None:
        pad_a = _sine(0.025, 0.2)
        pad_b = _sine(0.075, 0.1)
        lead = _sine(0.1, 0.2)
        self.assertEqual(len(pad_a), 1_600)
        self.assertEqual(len(pad_b), 800)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(),
                SAMPLE_RATE,
                scratch_parent=root / "scratch",
                cache_directory=root / "cache",
                expected_stem_count=3,
            )
            first = builder._begin_stem_transaction(
                _executor("pad-a", "pad"),
                frame_count=len(pad_a),
            )
            first.append(pad_a)
            first_view = first.finish_view()
            retained_mapping = first_view.audio
            first_view.close()
            retained_before = np.array(retained_mapping, copy=True)
            entries_before = tuple(builder._entries)
            telemetry_before = deepcopy(builder.cache_summary)

            second = builder._begin_stem_transaction(
                _executor("pad-b", "pad"),
                frame_count=len(pad_b),
            )
            second.append(pad_b)
            failed_mapping = second._audio
            with self.assertRaisesRegex(ValueError, "时间线长度不一致"):
                second.finish_view()

            self.assertTrue(second.closed)
            assert failed_mapping is not None
            self.assertTrue(failed_mapping._mmap.closed)
            self.assertTrue(builder._abort_only)
            self.assertEqual(tuple(builder._entries), entries_before)
            self.assertEqual(builder.cache_summary, telemetry_before)
            np.testing.assert_array_equal(retained_mapping, retained_before)

            self._assert_abort_only_rejects_followup_work(builder, lead)
            self.assertTrue(retained_mapping._mmap.closed)

    def test_cache_post_failure_aborts_and_cleanup_preserves_first_error(
        self,
    ) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=root / "scratch",
                cache_directory=root / "cache",
            )
            transaction = builder._begin_stem_transaction(
                _executor("texture", "texture"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            mapping = transaction._audio
            real_close = report_module._close_private_stem_scratch

            def close_then_fail(*args: object, **kwargs: object) -> None:
                real_close(*args, **kwargs)
                raise OSError("injected cleanup failure")

            with (
                mock.patch(
                    "tianlai.collaboration_report._note_analysis_cache",
                    side_effect=MemoryError(
                        "injected cache telemetry failure"
                    ),
                ),
                mock.patch(
                    "tianlai.collaboration_report._close_private_stem_scratch",
                    side_effect=close_then_fail,
                ),
            ):
                with self.assertRaisesRegex(
                    MemoryError,
                    "injected cache telemetry failure",
                ):
                    transaction.finish_view()

            self.assertTrue(transaction.closed)
            assert mapping is not None
            self.assertTrue(mapping._mmap.closed)
            self.assertEqual(builder._entries, [])
            self.assertTrue(builder._abort_only)
            self._assert_abort_only_rejects_followup_work(builder, audio)

    def test_entries_post_failure_makes_transaction_builder_abort_only(
        self,
    ) -> None:
        class FailingEntries(list[object]):
            def append(self, _entry: object) -> None:
                raise MemoryError("injected entries append failure")

        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            builder._entries = FailingEntries()
            transaction = builder._begin_stem_transaction(
                _executor("texture", "texture"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            mapping = transaction._audio

            with self.assertRaisesRegex(
                MemoryError,
                "injected entries append failure",
            ):
                transaction.finish_view()

            self.assertTrue(transaction.closed)
            assert mapping is not None
            self.assertTrue(mapping._mmap.closed)
            self.assertEqual(builder._entries, [])
            self.assertTrue(builder._abort_only)
            self._assert_abort_only_rejects_followup_work(builder, audio)

    def test_relation_adoption_memory_error_makes_builder_abort_only(
        self,
    ) -> None:
        class FailingHandles(list[object]):
            def append(self, _handle: object) -> None:
                raise MemoryError("injected relation adoption failure")

        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            builder._scratch_handles = FailingHandles()
            transaction = builder._begin_stem_transaction(
                _executor("pad-a", "pad"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            mapping = transaction._audio

            with self.assertRaisesRegex(
                MemoryError,
                "injected relation adoption failure",
            ):
                transaction.finish_view()

            self.assertTrue(transaction.closed)
            assert mapping is not None
            self.assertTrue(mapping._mmap.closed)
            self.assertNotIn("pad", builder._part_buffers)
            self.assertTrue(builder._abort_only)
            self._assert_abort_only_rejects_followup_work(builder, audio)

    def test_append_gates_are_terminal_and_release_private_scratch(self) -> None:
        cases = (
            (
                "wrong dtype",
                1,
                np.zeros((1, 2), dtype=np.float64),
                "float32 stereo",
            ),
            (
                "wrong channels",
                1,
                np.zeros((1, 1), dtype=np.float32),
                "float32 stereo",
            ),
            (
                "empty",
                1,
                np.zeros((0, 2), dtype=np.float32),
                "between 1 and 65536",
            ),
            (
                "oversized block",
                65_537,
                np.zeros((65_537, 2), dtype=np.float32),
                "between 1 and 65536",
            ),
            (
                "too many frames",
                1,
                np.zeros((2, 2), dtype=np.float32),
                "too many frames",
            ),
            (
                "nonfinite",
                1,
                np.full((1, 2), np.nan, dtype=np.float32),
                "not finite",
            ),
        )
        for label, frame_count, block, message in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                builder = CollaborationReportBuilder(
                    _settings(relations=False),
                    SAMPLE_RATE,
                    scratch_parent=root,
                )
                transaction = builder._begin_stem_transaction(
                    _executor("part", "part"),
                    frame_count=frame_count,
                )
                with self.assertRaisesRegex(ValueError, message):
                    transaction.append(block)
                self.assertTrue(transaction.closed)
                self.assertEqual(builder._stem_transactions, set())
                self.assertEqual(list(root.iterdir()), [])
                builder.close()

    def test_finish_rejects_count_expected_sha_and_mapping_damage(self) -> None:
        audio = _sine(0.1)
        for label in ("count", "expected_sha", "mapping_damage"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                builder = CollaborationReportBuilder(
                    _settings(relations=False),
                    SAMPLE_RATE,
                    scratch_parent=root,
                )
                transaction = builder._begin_stem_transaction(
                    _executor("part", "part"),
                    frame_count=len(audio),
                    expected_audio_sha256=(
                        "0" * 64 if label == "expected_sha" else None
                    ),
                )
                transaction.append(
                    audio[:-1] if label == "count" else audio
                )
                if label == "mapping_damage":
                    assert transaction._audio is not None
                    transaction._audio[0, 0] += np.float32(0.125)
                message = {
                    "count": "frame count is incomplete",
                    "expected_sha": "differs from the expected source",
                    "mapping_damage": "differs from appended blocks",
                }[label]
                with self.assertRaisesRegex(ValueError, message):
                    transaction.finish()
                self.assertTrue(transaction.closed)
                self.assertEqual(builder._entries, [])
                self.assertEqual(list(root.iterdir()), [])
                builder.close()

    def test_finish_rejects_nonfinite_mapping_and_identity_drift(self) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            transaction = builder._begin_stem_transaction(
                _executor("nan", "nan"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            assert transaction._audio is not None
            transaction._audio[0, 0] = np.nan
            with self.assertRaisesRegex(ValueError, "not finite"):
                transaction.finish()
            builder.close()

        with tempfile.TemporaryDirectory() as temporary:
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=temporary,
            )
            transaction = builder._begin_stem_transaction(
                _executor("identity", "identity"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            assert transaction._scratch is not None
            descriptor = transaction._scratch.fileno()
            real_fstat = os.fstat

            def changed_identity(candidate: int):
                status = real_fstat(candidate)
                if candidate == descriptor:
                    fields = list(status)
                    fields[1] = int(status.st_ino) + 1
                    return os.stat_result(fields)
                return status

            with (
                mock.patch(
                    "tianlai.collaboration_report.os.fstat",
                    side_effect=changed_identity,
                ),
                self.assertRaisesRegex(ValueError, "identity or length changed"),
            ):
                transaction.finish()
            builder.close()

    def test_primary_validation_error_survives_cleanup_failure(self) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=root,
            )
            transaction = builder._begin_stem_transaction(
                _executor("part", "part"),
                frame_count=len(audio),
            )
            transaction.append(audio)
            assert transaction._audio is not None
            transaction._audio[0, 0] += np.float32(0.125)
            real_close = report_module._close_private_stem_scratch

            def close_then_fail(*args: object, **kwargs: object) -> None:
                real_close(*args, **kwargs)
                raise OSError("injected cleanup failure")

            with (
                mock.patch(
                    "tianlai.collaboration_report._close_private_stem_scratch",
                    side_effect=close_then_fail,
                ),
                self.assertRaisesRegex(ValueError, "differs from appended blocks"),
            ):
                transaction.finish()

            self.assertEqual(list(root.iterdir()), [])
            builder.close()

    def test_build_rejects_and_closes_an_unfinished_transaction(self) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=root,
            )
            transaction = builder._begin_stem_transaction(
                _executor("part", "part"),
                frame_count=len(audio),
            )
            transaction.append(audio[:100])

            with self.assertRaisesRegex(RuntimeError, "unfinished"):
                builder.build()

            self.assertTrue(transaction.closed)
            self.assertEqual(list(root.iterdir()), [])
            builder.close()

    def test_finish_content_hash_is_reused_by_the_cache_diagnostic_path(
        self,
    ) -> None:
        audio = _sine(0.1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=root / "scratch",
                cache_directory=root / "cache",
                expected_stem_count=1,
            )
            transaction = builder._begin_stem_transaction(
                _executor("part", "part"),
                frame_count=len(audio),
                expected_audio_sha256=_sha256(audio),
            )
            transaction.append(audio)
            with mock.patch(
                "tianlai.collaboration_report._audio_content_sha256",
                wraps=report_module._audio_content_sha256,
            ) as content_sha256:
                transaction.finish()

            self.assertEqual(content_sha256.call_count, 1)
            builder.build()

    def test_invalid_begin_arguments_do_not_create_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(relations=False),
                SAMPLE_RATE,
                scratch_parent=root,
            )
            for frame_count in (0, -1, True, 1.5):
                with self.subTest(frame_count=frame_count), self.assertRaises(
                    ValueError
                ):
                    builder._begin_stem_transaction(
                        _executor("part", "part"),
                        frame_count=frame_count,
                    )
            for digest in ("bad", "A" * 64, 1):
                with self.subTest(digest=digest), self.assertRaises(ValueError):
                    builder._begin_stem_transaction(
                        _executor("part", "part"),
                        frame_count=1,
                        expected_audio_sha256=digest,
                    )
            self.assertEqual(list(root.iterdir()), [])
            builder.close()


if __name__ == "__main__":
    unittest.main()
