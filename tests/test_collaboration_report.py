from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import numpy as np

from tianlai._window_batches import window_rms
from tianlai.collaboration_report import (
    CollaborationReportBuilder,
    _block_activity,
    _shared_block_evidence,
)
from tianlai.mix_analysis import MixAnalysisConfig
from tianlai.roster import (
    BalanceRelation,
    CollaborationAnalysis,
    CollaborationSettings,
    PartGroup,
    Role,
)
from tianlai.spectral_overlap import analyze_spectral_overlap
from tianlai.temporal_balance import analyze_temporal_balance


SAMPLE_RATE = 8000


def _executor(
    executor_id: str,
    part_id: str,
    *,
    role: Role | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        executor_id=executor_id,
        part_id=part_id,
        capability=SimpleNamespace(relative_path=f"测试/{executor_id}"),
        gain_db=-3.0,
        pan=0.0,
        role=role,
    )


def _settings(
    *,
    mode: str = "suggest",
    target: float = -6.0,
    tolerance: float = 0.25,
    maximum: float = 4.0,
) -> CollaborationSettings:
    return CollaborationSettings(
        mode=mode,
        analysis=CollaborationAnalysis(
            window_ms=200.0,
            hop_ms=100.0,
            gate_dbfs=-60.0,
        ),
        balance_relations=(
            BalanceRelation(
                subject="pad",
                reference="lead",
                target_offset_db=target,
                tolerance_db=tolerance,
                max_suggestion_db=maximum,
            ),
        ),
        declared=True,
    )


def _sine(amplitude: float, seconds: float = 1.0) -> np.ndarray:
    time = np.arange(round(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    mono = amplitude * np.sin(2.0 * np.pi * 440.0 * time)
    return np.column_stack((mono, mono)).astype(np.float32)


class CollaborationReportTests(unittest.TestCase):
    def test_matching_declared_relation_needs_no_adjustment(self) -> None:
        builder = CollaborationReportBuilder(_settings(), SAMPLE_RATE)
        pad = _sine(0.1)
        lead = _sine(0.2)
        builder.add_stem(
            _executor(
                "cello",
                "pad",
                role=Role("pad", "background", "氛围大提琴"),
            ),
            pad,
        )
        builder.add_stem(
            _executor("melody", "lead", role=Role("lead", "foreground")),
            lead,
        )

        report = builder.build()

        relation = report["balance_relations"][0]
        self.assertEqual(relation["status"], "within_tolerance")
        self.assertAlmostEqual(
            relation["measured_offset_db"],
            -6.0206,
            places=3,
        )
        self.assertEqual(
            relation["suggested_subject_gain_adjustment_db"],
            0.0,
        )
        draft = relation["gain_automation_draft"]
        self.assertEqual(draft["status"], "creator_review_required")
        self.assertFalse(draft["executable"])
        self.assertFalse(draft["audio_modified"])
        self.assertEqual(draft["source_candidate_segment_count"], 0)
        self.assertFalse(draft["segments_truncated"])
        self.assertEqual(draft["segments"], [])
        self.assertEqual(
            report["summary"]["automation_draft_relation_count"],
            1,
        )
        self.assertEqual(
            report["summary"]["automation_draft_segment_count"],
            0,
        )
        self.assertFalse(report["audio_modified"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(
            report["stems"][0]["role"],
            {
                "function": "pad",
                "prominence": "background",
                "label": "氛围大提琴",
            },
        )
        self.assertEqual(
            relation["temporal_balance"]["status"],
            "stable_within_tolerance",
        )
        self.assertEqual(
            relation["overlap_evidence"]["status"],
            "sufficient",
        )
        self.assertEqual(
            relation["overlap_evidence"]["block_ms"],
            10.0,
        )
        self.assertEqual(
            relation["overlap_evidence"]["shared_active_seconds"],
            1.0,
        )
        self.assertEqual(
            report["analysis"]["workload"]["executor_count"],
            2,
        )
        self.assertEqual(
            report["analysis"]["workload"][
                "unique_relation_part_count"
            ],
            2,
        )
        self.assertEqual(
            report["analysis"]["workload"]["relation_buffer_bytes"],
            2 * SAMPLE_RATE * 2 * 4,
        )

    def test_explicit_part_group_sums_member_parts_after_part_aggregation(
        self,
    ) -> None:
        settings = CollaborationSettings(
            mode="suggest",
            analysis=CollaborationAnalysis(
                window_ms=200.0,
                hop_ms=100.0,
                gate_dbfs=-60.0,
            ),
            balance_relations=(
                BalanceRelation("piano", "lead", 0.0, 0.1, 4.0),
            ),
            declared=True,
            part_groups=(
                PartGroup("piano", ("piano_left", "piano_right")),
            ),
        )
        builder = CollaborationReportBuilder(settings, SAMPLE_RATE)
        # Two executors on one part model the existing kit expansion rule:
        # they merge to piano_left before the two piano parts form the group.
        builder.add_stem(
            _executor("left-a", "piano_left"),
            _sine(0.025),
        )
        builder.add_stem(
            _executor("left-b", "piano_left"),
            _sine(0.025),
        )
        builder.add_stem(
            _executor("right", "piano_right"),
            _sine(0.05),
        )
        builder.add_stem(_executor("lead", "lead"), _sine(0.1))
        builder.add_stem(
            _executor("unrelated", "unrelated"),
            _sine(0.8),
        )

        materialized_group = builder._endpoint_buffer("piano")
        self.assertIs(
            materialized_group,
            builder._endpoint_buffer("piano"),
        )
        report = builder.build()
        relation = report["balance_relations"][0]

        self.assertEqual(relation["status"], "within_tolerance")
        self.assertAlmostEqual(
            relation["measured_offset_db"],
            0.0,
            places=5,
        )
        self.assertEqual(
            relation["subject_endpoint"],
            {
                "endpoint_kind": "part_group",
                "expanded_parts": ["piano_left", "piano_right"],
            },
        )
        self.assertEqual(
            relation["reference_endpoint"],
            {
                "endpoint_kind": "part",
                "expanded_parts": ["lead"],
            },
        )
        self.assertEqual(
            relation["suggested_subject_gain_adjustment_db"],
            0.0,
        )
        draft = relation["gain_automation_draft"]
        self.assertEqual(
            draft["subject_endpoint"],
            relation["subject_endpoint"],
        )
        self.assertEqual(
            draft["workflow"]["write_target_parts"],
            ["piano_left", "piano_right"],
        )
        self.assertEqual(
            draft["workflow"]["subject_application"],
            "creator_distributes_or_uniformly_applies_to_expanded_parts",
        )
        self.assertEqual(
            report["analysis"]["workload"]["unique_relation_part_count"],
            3,
        )
        self.assertEqual(
            report["analysis"]["workload"]["relation_buffer_bytes"],
            4 * SAMPLE_RATE * 2 * 4,
        )

    def test_builder_rejects_overlapping_expanded_relation_endpoints(
        self,
    ) -> None:
        settings = CollaborationSettings(
            mode="analyze",
            balance_relations=(
                BalanceRelation("ensemble", "lead", 0.0, 1.0, 4.0),
            ),
            declared=True,
            part_groups=(
                PartGroup("ensemble", ("pad", "lead")),
            ),
        )
        with self.assertRaisesRegex(ValueError, "disjoint"):
            CollaborationReportBuilder(settings, SAMPLE_RATE)

    def test_suggestion_is_bounded_and_does_not_change_input(self) -> None:
        builder = CollaborationReportBuilder(
            _settings(target=-8.0, tolerance=1.0, maximum=3.0),
            SAMPLE_RATE,
        )
        pad = _sine(0.4)
        lead = _sine(0.1)
        original = pad.copy()
        builder.add_stem(_executor("pad", "pad"), pad)
        builder.add_stem(_executor("lead", "lead"), lead)

        report = builder.build()
        relation = report["balance_relations"][0]

        self.assertEqual(relation["status"], "outside_tolerance")
        self.assertEqual(
            relation["suggested_subject_gain_adjustment_db"],
            -3.0,
        )
        draft = relation["gain_automation_draft"]
        self.assertEqual(draft["subject"], "pad")
        self.assertEqual(draft["reference"], "lead")
        self.assertEqual(draft["time_basis"], "seconds")
        self.assertEqual(
            draft["adjustment_semantics"],
            "additive_subject_gain_offset_db_not_absolute_gain",
        )
        self.assertFalse(draft["executable"])
        self.assertEqual(draft["source_candidate_segment_count"], 1)
        self.assertFalse(draft["segments_truncated"])
        self.assertEqual(len(draft["segments"]), 1)
        self.assertEqual(
            draft["segments"][0][
                "suggested_subject_gain_adjustment_db"
            ],
            -3.0,
        )
        self.assertEqual(
            relation["spectral_screening"]["status"],
            "candidate",
        )
        self.assertIn(
            "spectral_overlap_candidate",
            {warning["code"] for warning in report["warnings"]},
        )
        np.testing.assert_array_equal(pad, original)

    def test_analyze_mode_reports_but_does_not_suggest(self) -> None:
        builder = CollaborationReportBuilder(
            _settings(mode="analyze"),
            SAMPLE_RATE,
        )
        builder.add_stem(_executor("pad", "pad"), _sine(0.4))
        builder.add_stem(_executor("lead", "lead"), _sine(0.1))

        relation = builder.build()["balance_relations"][0]

        self.assertEqual(relation["status"], "outside_tolerance")
        self.assertNotIn("suggested_subject_gain_adjustment_db", relation)
        self.assertNotIn("gain_automation_draft", relation)
        self.assertEqual(
            relation["temporal_balance"]["candidate_segment_count"],
            1,
        )

    def test_nonoverlapping_parts_are_explicitly_inconclusive(self) -> None:
        frames = SAMPLE_RATE * 3
        pad = np.zeros((frames, 2), dtype=np.float32)
        lead = np.zeros((frames, 2), dtype=np.float32)
        pad[: SAMPLE_RATE // 2] = _sine(0.1, 0.5)
        lead[-SAMPLE_RATE // 2 :] = _sine(0.2, 0.5)
        builder = CollaborationReportBuilder(_settings(), SAMPLE_RATE)
        builder.add_stem(_executor("pad", "pad"), pad)
        builder.add_stem(_executor("lead", "lead"), lead)

        report = builder.build()

        relation = report["balance_relations"][0]
        self.assertEqual(relation["status"], "insufficient_overlap")
        self.assertIsNone(relation["deviation_db"])
        self.assertNotIn("suggested_subject_gain_adjustment_db", relation)
        self.assertNotIn("gain_automation_draft", relation)
        self.assertEqual(
            report["warnings"][0]["code"],
            "balance_relation_insufficient_overlap",
        )

    def test_momentary_overlap_is_not_used_for_a_gain_suggestion(self) -> None:
        frames = SAMPLE_RATE * 2
        pad = np.zeros((frames, 2), dtype=np.float32)
        lead = np.zeros((frames, 2), dtype=np.float32)
        short = _sine(0.2, 0.05)
        pad[: short.shape[0]] = short
        lead[: short.shape[0]] = short * 0.5
        builder = CollaborationReportBuilder(_settings(), SAMPLE_RATE)
        builder.add_stem(_executor("pad", "pad"), pad)
        builder.add_stem(_executor("lead", "lead"), lead)

        report = builder.build()
        relation = report["balance_relations"][0]

        self.assertGreater(
            relation["measurement"]["shared_active_window_count"],
            0,
        )
        self.assertLess(
            relation["measurement"]["shared_active_window_count"],
            report["analysis"]["minimum_shared_window_count"],
        )
        self.assertEqual(relation["status"], "insufficient_overlap")
        self.assertEqual(
            relation["spectral_screening"]["status"],
            "insufficient_overlap",
        )
        self.assertIsNone(
            relation["measurement"]["first_minus_second_db"]
        )
        self.assertNotIn(
            "suggested_subject_gain_adjustment_db",
            relation,
        )
        self.assertNotIn("gain_automation_draft", relation)
        self.assertEqual(
            relation["temporal_balance"]["status"],
            "insufficient_overlap",
        )

    def test_overlapping_main_windows_cannot_multiply_one_impulse_into_half_second(
        self,
    ) -> None:
        frames = SAMPLE_RATE * 2
        impulse = np.zeros((frames, 2), dtype=np.float32)
        # Straddle the 400 ms main-window boundary.  The two samples appear in
        # five overlapping 400/100 ms windows, but only two independent 10 ms
        # activity blocks and therefore represent 20 ms of block coverage.
        impulse[3199:3201] = 0.5
        settings = CollaborationSettings(
            mode="suggest",
            analysis=CollaborationAnalysis(
                window_ms=400.0,
                hop_ms=100.0,
                gate_dbfs=-60.0,
            ),
            balance_relations=(
                BalanceRelation("pad", "lead", 0.0, 1.0, 4.0),
            ),
            declared=True,
        )
        builder = CollaborationReportBuilder(settings, SAMPLE_RATE)
        builder.add_stem(_executor("pad", "pad"), impulse)
        builder.add_stem(_executor("lead", "lead"), impulse)

        with (
            mock.patch(
                "tianlai.collaboration_report.analyze_spectral_overlap"
            ) as spectral_analysis,
            mock.patch(
                "tianlai.collaboration_report.analyze_temporal_balance"
            ) as temporal_analysis,
        ):
            report = builder.build()
        relation = report["balance_relations"][0]
        evidence = relation["overlap_evidence"]

        spectral_analysis.assert_not_called()
        temporal_analysis.assert_not_called()
        self.assertGreaterEqual(
            relation["measurement"]["shared_active_window_count"],
            3,
        )
        self.assertEqual(evidence["shared_active_block_count"], 2)
        self.assertEqual(evidence["shared_active_seconds"], 0.02)
        self.assertEqual(evidence["status"], "insufficient")
        self.assertEqual(relation["status"], "insufficient_overlap")
        self.assertIsNone(
            relation["measurement"]["first_minus_second_db"]
        )
        self.assertIsNone(relation["temporal_balance"]["median_db"])
        self.assertEqual(
            report["analysis"]["workload"][
                "relation_pair_fft_window_count"
            ],
            0,
        )
        self.assertNotIn(
            "suggested_subject_gain_adjustment_db",
            relation,
        )

    def test_block_coverage_counts_only_real_frames_in_a_partial_tail(
        self,
    ) -> None:
        # 10 ms is 80 frames at 8 kHz: this timeline has one full block and
        # one 5-frame tail.  Both are active, but evidence must report 85
        # real frames rather than 160 frames including zero padding.
        audio = np.full((85, 2), 0.5, dtype=np.float32)
        config = MixAnalysisConfig(
            window_seconds=0.02,
            hop_seconds=0.01,
            absolute_gate_dbfs=-60.0,
            relative_gate_db=-40.0,
        )
        activity = _block_activity(audio, SAMPLE_RATE, config)

        evidence = _shared_block_evidence(
            activity,
            activity,
            SAMPLE_RATE,
            shared_main_window_count=3,
        )

        self.assertEqual(evidence["shared_active_block_count"], 2)
        self.assertEqual(evidence["shared_active_seconds"], 85 / SAMPLE_RATE)
        self.assertEqual(evidence["status"], "insufficient")

    def test_relation_status_uses_the_same_six_decimal_value_it_publishes(
        self,
    ) -> None:
        ratio = 10.0 ** (2.0000004 / 20.0)
        builder = CollaborationReportBuilder(
            _settings(target=0.0, tolerance=2.0),
            SAMPLE_RATE,
        )
        builder.add_stem(_executor("pad", "pad"), _sine(0.1 * ratio))
        builder.add_stem(_executor("lead", "lead"), _sine(0.1))

        relation = builder.build()["balance_relations"][0]

        self.assertEqual(relation["measured_offset_db"], 2.0)
        self.assertEqual(relation["deviation_db"], 2.0)
        self.assertEqual(relation["status"], "within_tolerance")
        self.assertEqual(
            relation["suggested_subject_gain_adjustment_db"],
            0.0,
        )

    def test_candidate_predicates_use_only_serialized_spectral_and_temporal_values(
        self,
    ) -> None:
        pad = _sine(0.4, 2.0)
        lead = _sine(0.1, 2.0)
        config = MixAnalysisConfig(
            window_seconds=0.2,
            hop_seconds=0.1,
            absolute_gate_dbfs=-60.0,
            relative_gate_db=-40.0,
        )
        real_spectral = analyze_spectral_overlap(
            pad,
            lead,
            SAMPLE_RATE,
            config,
        )
        spectral_document = real_spectral.to_dict()
        spectral_document["first_band_energy_ratios"] = {
            name: (0.08 if name == "low_mid" else 0.0)
            for name in spectral_document["first_band_energy_ratios"]
        }
        spectral_document["second_band_energy_ratios"] = copy.deepcopy(
            spectral_document["first_band_energy_ratios"]
        )
        spectral_document["band_first_minus_second_db"] = {
            name: (-7.0 if name == "low_mid" else None)
            for name in spectral_document["band_first_minus_second_db"]
        }
        # The public 0.650000000 coefficient is at the inclusive threshold.
        # A raw value just below it must not secretly override that conclusion.
        spectral_document["spectral_overlap_coefficient"] = 0.65
        fake_spectral = SimpleNamespace(
            shared_active_window_count=(
                real_spectral.shared_active_window_count
            ),
            spectral_overlap_coefficient=0.6499999996,
            first_band_energy_ratios=(("low_mid", 0.0800000004),),
            second_band_energy_ratios=(("low_mid", 0.0800000004),),
            band_first_minus_second_db=(("low_mid", -6.9999996),),
            to_dict=lambda: copy.deepcopy(spectral_document),
        )

        real_temporal = analyze_temporal_balance(
            pad,
            lead,
            SAMPLE_RATE,
            config,
            target_offset_db=-8.0,
            tolerance_db=1.0,
            minimum_shared_window_count=3,
        )
        temporal_document = real_temporal.to_dict()
        temporal_document["robust_span_db"] = 2.0
        fake_temporal = SimpleNamespace(
            robust_span_db=2.0000004,
            to_dict=lambda: copy.deepcopy(temporal_document),
        )

        builder = CollaborationReportBuilder(
            _settings(target=-8.0, tolerance=1.0),
            SAMPLE_RATE,
        )
        builder.add_stem(_executor("pad", "pad"), pad)
        builder.add_stem(_executor("lead", "lead"), lead)
        with (
            mock.patch(
                "tianlai.collaboration_report.analyze_spectral_overlap",
                return_value=fake_spectral,
            ),
            mock.patch(
                "tianlai.collaboration_report.analyze_temporal_balance",
                return_value=fake_temporal,
            ),
        ):
            report = builder.build()

        relation = report["balance_relations"][0]
        # Overall spectral overlap is inclusive at 0.65 and the visible
        # relation deviation is above tolerance, so it is a candidate.
        self.assertEqual(
            relation["spectral_screening"]["status"],
            "candidate",
        )
        # The visible band difference is exactly target+tolerance (-7), so
        # the strict "greater than" band predicate does not include it.
        self.assertEqual(
            relation["spectral_screening"]["candidate_bands"],
            [],
        )
        # Visible robust_span == 2*tolerance, so the strict temporal predicate
        # is clear even though the deliberately conflicting raw object is not.
        self.assertEqual(
            report["summary"]["temporal_balance_drift_candidates"],
            0,
        )

    def test_fixed_block_activity_is_cached_once_per_unique_relation_part(
        self,
    ) -> None:
        settings = CollaborationSettings(
            mode="analyze",
            analysis=CollaborationAnalysis(
                window_ms=200.0,
                hop_ms=100.0,
                gate_dbfs=-60.0,
            ),
            balance_relations=(
                BalanceRelation("pad", "lead", -6.0, 1.0, 4.0),
                BalanceRelation("bass", "lead", -6.0, 1.0, 4.0),
            ),
            declared=True,
        )
        builder = CollaborationReportBuilder(settings, SAMPLE_RATE)
        builder.add_stem(_executor("pad", "pad"), _sine(0.1))
        builder.add_stem(_executor("bass", "bass"), _sine(0.1))
        builder.add_stem(_executor("lead", "lead"), _sine(0.2))

        with mock.patch(
            "tianlai.collaboration_report.window_rms",
            wraps=window_rms,
        ) as block_rms:
            builder.build()

        self.assertEqual(block_rms.call_count, 3)
        self.assertTrue(
            all(
                isinstance(call.args[1], range)
                for call in block_rms.call_args_list
            )
        )

    def test_manual_mode_cannot_accidentally_run_analysis(self) -> None:
        with self.assertRaisesRegex(ValueError, "analyze or suggest"):
            CollaborationReportBuilder(
                CollaborationSettings(),
                SAMPLE_RATE,
            )

    def test_dynamic_drift_is_separate_from_whole_piece_balance(self) -> None:
        builder = CollaborationReportBuilder(
            _settings(target=0.0, tolerance=1.0),
            SAMPLE_RATE,
        )
        pad = _sine(1.0, 2.0)
        lead = _sine(0.2, 2.0)
        halfway = pad.shape[0] // 2
        pad[:halfway] *= 0.1
        pad[halfway:] *= 0.4
        builder.add_stem(_executor("pad", "pad"), pad)
        builder.add_stem(_executor("lead", "lead"), lead)

        report = builder.build()
        relation = report["balance_relations"][0]
        temporal = relation["temporal_balance"]

        self.assertEqual(
            temporal["status"],
            "varies_outside_tolerance",
        )
        self.assertGreater(temporal["robust_span_db"], 10.0)
        self.assertEqual(
            report["summary"]["temporal_balance_drift_candidates"],
            1,
        )
        segments = temporal["candidate_segments"]
        self.assertEqual(
            [segment["direction"] for segment in segments],
            ["subject_too_quiet", "subject_too_loud"],
        )
        draft = relation["gain_automation_draft"]
        self.assertEqual(draft["status"], "creator_review_required")
        self.assertFalse(draft["executable"])
        self.assertEqual(
            [
                segment["suggested_subject_gain_adjustment_db"]
                for segment in draft["segments"]
            ],
            [4.0, -4.0],
        )
        self.assertEqual(
            report["summary"]["automation_draft_relation_count"],
            1,
        )
        self.assertEqual(
            report["summary"]["automation_draft_segment_count"],
            2,
        )
        self.assertIn(
            "temporal_balance_drift_candidate",
            {warning["code"] for warning in report["warnings"]},
        )

    def test_scratch_memmap_aggregates_kit_part_and_cleans_itself(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            builder = CollaborationReportBuilder(
                _settings(),
                SAMPLE_RATE,
                scratch_parent=root,
            )
            builder.add_stem(
                _executor("pad-a", "pad"),
                _sine(0.05),
            )
            builder.add_stem(
                _executor("pad-b", "pad"),
                _sine(0.05),
            )
            builder.add_stem(
                _executor("lead", "lead"),
                _sine(0.2),
            )
            self.assertTrue(
                list(root.glob(".collaboration-analysis.*"))
            )

            report = builder.build()

            self.assertEqual(
                report["analysis"]["relation_buffer_storage"],
                "scratch_float32_memmap",
            )
            self.assertEqual(
                report["balance_relations"][0]["status"],
                "within_tolerance",
            )
            self.assertEqual(
                list(root.glob(".collaboration-analysis.*")),
                [],
            )
            with self.assertRaisesRegex(RuntimeError, "closed"):
                builder.build()


if __name__ == "__main__":
    unittest.main()
