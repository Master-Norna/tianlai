from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from jsonschema import Draft202012Validator
import numpy as np

from tianlai.collaboration_report import (
    CollaborationReportBuilder,
    attach_stage_diagnostics,
)
from tianlai.orchestration_topology import (
    analyze_orchestration_topology,
    attach_orchestration_topology,
)
from tianlai.roster import (
    BalanceRelation,
    CollaborationAnalysis,
    CollaborationSettings,
    PartGroup,
    Role,
)
from tianlai.stereo_stage_metrics import analyze_stereo_stage


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 8_000


def _executor(
    executor_id: str,
    part_id: str,
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


def _settings(mode: str) -> CollaborationSettings:
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
                target_offset_db=-8.0,
                tolerance_db=1.0,
                max_suggestion_db=4.0,
            ),
        ),
        declared=True,
    )


def _tone(amplitude: float, seconds: float = 1.0) -> np.ndarray:
    time = np.arange(round(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    mono = amplitude * np.sin(2.0 * np.pi * 440.0 * time)
    return np.column_stack((mono, mono)).astype(np.float32)


def _with_stage_metrics(report: dict, audio: np.ndarray) -> dict:
    attach_orchestration_topology(
        report,
        analyze_orchestration_topology(SimpleNamespace(parts=())),
    )
    metrics = analyze_stereo_stage(audio, SAMPLE_RATE).to_dict()
    attach_stage_diagnostics(
        report,
        post_pan_pre_space=metrics,
        post_space_pre_master=metrics,
        final=metrics,
    )
    return report


def _measured_report(mode: str) -> dict:
    builder = CollaborationReportBuilder(_settings(mode), SAMPLE_RATE)
    builder.add_stem(
        _executor(
            "cello",
            "pad",
            Role("pad", "background", "大提琴氛围"),
        ),
        _tone(0.4),
    )
    builder.add_stem(
        _executor("melody", "lead", Role("lead", "foreground")),
        _tone(0.1),
    )
    return _with_stage_metrics(
        builder.build(),
        _tone(0.4) + _tone(0.1),
    )


def _insufficient_report(mode: str = "suggest") -> dict:
    frames = SAMPLE_RATE * 3
    pad = np.zeros((frames, 2), dtype=np.float32)
    lead = np.zeros((frames, 2), dtype=np.float32)
    pad[: SAMPLE_RATE // 2] = _tone(0.1, 0.5)
    lead[-SAMPLE_RATE // 2 :] = _tone(0.2, 0.5)
    builder = CollaborationReportBuilder(_settings(mode), SAMPLE_RATE)
    builder.add_stem(_executor("pad", "pad"), pad)
    builder.add_stem(_executor("lead", "lead"), lead)
    return _with_stage_metrics(builder.build(), pad + lead)


def _silent_report() -> dict:
    settings = CollaborationSettings(
        mode="analyze",
        analysis=CollaborationAnalysis(),
        balance_relations=(),
        declared=True,
    )
    builder = CollaborationReportBuilder(settings, SAMPLE_RATE)
    builder.add_stem(
        _executor("silent", "silent"),
        np.zeros((SAMPLE_RATE, 2), dtype=np.float32),
    )
    return _with_stage_metrics(
        builder.build(),
        np.zeros((SAMPLE_RATE, 2), dtype=np.float32),
    )


class MixReportSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (ROOT / "schemas" / "mix-report.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def assert_valid(self, document: dict) -> None:
        errors = sorted(
            self.validator.iter_errors(document),
            key=lambda error: tuple(str(item) for item in error.absolute_path),
        )
        self.assertEqual(
            errors,
            [],
            "\n".join(
                f"{list(error.absolute_path)}: {error.message}"
                for error in errors
            ),
        )

    def assert_invalid(self, document: dict) -> None:
        self.assertTrue(list(self.validator.iter_errors(document)))

    def test_schema_accepts_every_generated_relation_variant(self) -> None:
        documents = (
            _measured_report("analyze"),
            _measured_report("suggest"),
            _insufficient_report("analyze"),
            _insufficient_report("suggest"),
        )
        for document in documents:
            with self.subTest(
                mode=document["mode"],
                status=document["balance_relations"][0]["status"],
            ):
                self.assert_valid(document)
                json.dumps(document, ensure_ascii=False, allow_nan=False)

    def test_silent_metrics_are_explicit_null_and_schema_valid(self) -> None:
        report = _silent_report()
        metrics = report["stems"][0]["metrics"]

        for key in (
            "peak_dbfs",
            "rms_dbfs",
            "active_rms_dbfs",
            "crest_factor_db",
            "spectral_centroid_hz",
            "stereo_correlation",
            "stereo_width",
        ):
            self.assertIsNone(metrics[key])
        self.assertTrue(
            all(
                value is None
                for value in metrics["band_energy_ratios"].values()
            )
        )
        self.assertIsNone(report["stems"][0]["role"])
        self.assert_valid(report)

    def test_mode_controls_whether_a_measured_relation_has_a_suggestion(
        self,
    ) -> None:
        analyze = _measured_report("analyze")
        self.assertNotIn(
            "gain_automation_draft",
            analyze["balance_relations"][0],
        )
        analyze["balance_relations"][0][
            "suggested_subject_gain_adjustment_db"
        ] = -4.0
        self.assert_invalid(analyze)

        analyze = _measured_report("analyze")
        analyze["balance_relations"][0]["gain_automation_draft"] = (
            _measured_report("suggest")["balance_relations"][0][
                "gain_automation_draft"
            ]
        )
        self.assert_invalid(analyze)

        suggest = _measured_report("suggest")
        del suggest["balance_relations"][0][
            "suggested_subject_gain_adjustment_db"
        ]
        self.assert_invalid(suggest)

        suggest = _measured_report("suggest")
        del suggest["balance_relations"][0]["gain_automation_draft"]
        self.assert_invalid(suggest)

    def test_suggest_exposes_only_a_bounded_creator_review_draft(self) -> None:
        report = _measured_report("suggest")
        relation = report["balance_relations"][0]
        temporal = relation["temporal_balance"]
        draft = relation["gain_automation_draft"]

        self.assertEqual(temporal["candidate_segment_count"], 1)
        self.assertFalse(temporal["candidate_segments_truncated"])
        self.assertEqual(
            temporal["candidate_segment_policy"]["time_basis"],
            "seconds",
        )
        self.assertEqual(
            temporal["candidate_segment_policy"][
                "minimum_shared_window_coverage_seconds_per_bucket"
            ],
            0.5,
        )
        self.assertEqual(
            temporal["candidate_segment_policy"]["segment_boundaries"],
            "union_evidence_bounds_not_full_bucket_bounds",
        )
        self.assertFalse(
            temporal["candidate_segment_policy"][
                "raw_window_sequence_included"
            ]
        )
        self.assertFalse(draft["executable"])
        self.assertFalse(draft["audio_modified"])
        self.assertEqual(draft["status"], "creator_review_required")
        self.assertEqual(draft["time_basis"], "seconds")
        self.assertEqual(
            draft["subject_endpoint"],
            {
                "endpoint_kind": "part",
                "expanded_parts": ["pad"],
            },
        )
        self.assertEqual(
            draft["source_candidate_segment_count"],
            temporal["candidate_segment_count"],
        )
        self.assertEqual(
            draft["segments_truncated"],
            temporal["candidate_segments_truncated"],
        )
        self.assertEqual(
            draft["workflow"]["locate_boundaries_with"],
            "MCP locate(at_seconds=...)",
        )
        self.assertTrue(
            draft["workflow"]["creator_review_required"]
        )
        self.assertEqual(
            draft["workflow"]["write_target"],
            "roster.assignments[].gain_automation",
        )
        self.assertEqual(
            draft["workflow"]["write_target_parts"],
            ["pad"],
        )
        self.assertEqual(
            draft["workflow"]["subject_application"],
            "review_then_write_subject_part",
        )
        self.assertEqual(
            report["summary"]["automation_draft_relation_count"],
            1,
        )
        self.assertEqual(
            report["summary"]["automation_draft_segment_count"],
            len(draft["segments"]),
        )
        for segment in draft["segments"]:
            self.assertLessEqual(
                abs(segment["suggested_subject_gain_adjustment_db"]),
                draft["maximum_absolute_adjustment_db"],
            )
        self.assert_valid(report)

        for key, value in (
            ("executable", True),
            ("audio_modified", True),
            ("status", "ready_to_apply"),
            ("time_basis", "bars"),
        ):
            with self.subTest(key=key):
                invalid = _measured_report("suggest")
                invalid["balance_relations"][0][
                    "gain_automation_draft"
                ][key] = value
                self.assert_invalid(invalid)

    def test_relation_endpoints_disclose_part_or_creator_declared_group(
        self,
    ) -> None:
        report = _measured_report("analyze")
        relation = report["balance_relations"][0]
        self.assertEqual(
            relation["subject_endpoint"],
            {
                "endpoint_kind": "part",
                "expanded_parts": ["pad"],
            },
        )
        self.assertEqual(
            relation["reference_endpoint"],
            {
                "endpoint_kind": "part",
                "expanded_parts": ["lead"],
            },
        )
        self.assert_valid(report)

        for endpoint_key in ("subject_endpoint", "reference_endpoint"):
            with self.subTest(endpoint=endpoint_key):
                invalid = _measured_report("analyze")
                invalid["balance_relations"][0][endpoint_key][
                    "endpoint_kind"
                ] = "guessed_family"
                self.assert_invalid(invalid)

        grouped_settings = CollaborationSettings(
            mode="suggest",
            analysis=CollaborationAnalysis(
                window_ms=200.0,
                hop_ms=100.0,
                gate_dbfs=-60.0,
            ),
            balance_relations=(
                BalanceRelation("ensemble", "lead", 0.0, 1.0, 4.0),
            ),
            declared=True,
            part_groups=(
                PartGroup("ensemble", ("left", "right")),
            ),
        )
        builder = CollaborationReportBuilder(grouped_settings, SAMPLE_RATE)
        builder.add_stem(_executor("left", "left"), _tone(0.05))
        builder.add_stem(_executor("right", "right"), _tone(0.05))
        builder.add_stem(_executor("lead", "lead"), _tone(0.1))
        grouped = _with_stage_metrics(builder.build(), _tone(0.2))
        self.assertEqual(
            grouped["balance_relations"][0]["subject_endpoint"],
            {
                "endpoint_kind": "part_group",
                "expanded_parts": ["left", "right"],
            },
        )
        group_draft = grouped["balance_relations"][0][
            "gain_automation_draft"
        ]
        self.assertEqual(
            group_draft["subject_endpoint"],
            grouped["balance_relations"][0]["subject_endpoint"],
        )
        self.assertEqual(
            group_draft["workflow"]["write_target_parts"],
            ["left", "right"],
        )
        self.assertEqual(
            group_draft["workflow"]["subject_application"],
            "creator_distributes_or_uniformly_applies_to_expanded_parts",
        )
        self.assert_valid(grouped)

    def test_topology_candidate_is_strict_and_advisory(self) -> None:
        report = _measured_report("analyze")
        topology = report["orchestration_topology"]
        first = {
            "executor_id": "violin-1",
            "part_id": "violin_i",
            "instrument": "管弦乐/弦乐组/小提琴",
            "sample_variant": None,
        }
        second = {
            "executor_id": "violin-2",
            "part_id": "violin_ii",
            "instrument": "管弦乐/弦乐组/小提琴",
            "sample_variant": None,
        }
        pair = {
            "first": first,
            "second": second,
            "same_source": True,
            "first_note_count": 10,
            "second_note_count": 10,
            "same_pitch_same_score_position_count": 8,
            "same_pitch_coverage_of_shorter_part": 0.8,
            "octave_same_score_position_count": 0,
            "exact_simultaneous_same_pitch_count": 0,
            "exact_simultaneous_same_pitch_ratio": 0.0,
            "short_delay_same_pitch_count": 8,
            "short_delay_same_pitch_ratio": 1.0,
            "near_simultaneous_same_pitch_count": 8,
            "near_simultaneous_same_pitch_ratio": 1.0,
            "median_scheduled_start_delta_ms": 3.0,
            "maximum_scheduled_start_delta_ms": 8.0,
            "status": "same_source_unison_phase_candidate",
        }
        warning = {
            "code": "same_source_unison_phase_candidate",
            "first_executor_id": "violin-1",
            "second_executor_id": "violin-2",
            "instrument": "管弦乐/弦乐组/小提琴",
            "same_pitch_same_score_position_count": 8,
            "same_pitch_coverage_of_shorter_part": 0.8,
            "short_delay_same_pitch_ratio": 1.0,
            "near_simultaneous_same_pitch_ratio": 1.0,
            "median_scheduled_start_delta_ms": 3.0,
            "message": "同源近同步同音候选，只读。",
        }
        topology["pairs"] = [pair]
        topology["summary"] = {
            "pair_count": 1,
            "reported_pair_count": 1,
            "pairs_truncated": False,
            "same_pitch_same_score_position_count": 8,
            "octave_same_score_position_count": 0,
            "same_source_unison_phase_candidate_count": 1,
            "same_source_exact_unison_level_stack_candidate_count": 0,
            "reported_warning_count": 1,
            "warnings_truncated": False,
        }
        topology["warnings"] = [warning]
        report["warnings"].append(warning)
        report["summary"]["same_source_unison_phase_candidates"] = 1
        report["summary"]["warning_count"] = len(report["warnings"])
        self.assertFalse(topology["audio_modified"])
        self.assertFalse(
            topology["analysis"]["octave_doubling_is_phase_candidate"]
        )
        self.assert_valid(report)

        exact = copy.deepcopy(report)
        exact_pair = exact["orchestration_topology"]["pairs"][0]
        exact_pair.update(
            {
                "exact_simultaneous_same_pitch_count": 8,
                "exact_simultaneous_same_pitch_ratio": 1.0,
                "short_delay_same_pitch_count": 0,
                "short_delay_same_pitch_ratio": 0.0,
                "median_scheduled_start_delta_ms": 0.0,
                "maximum_scheduled_start_delta_ms": 0.0,
                "status": (
                    "same_source_exact_unison_level_stack_candidate"
                ),
            }
        )
        exact_warning = {
            "code": "same_source_exact_unison_level_stack_candidate",
            "first_executor_id": "violin-1",
            "second_executor_id": "violin-2",
            "instrument": "管弦乐/弦乐组/小提琴",
            "same_pitch_same_score_position_count": 8,
            "same_pitch_coverage_of_shorter_part": 0.8,
            "exact_simultaneous_same_pitch_ratio": 1.0,
            "message": "精确同源同音属于电平叠加候选，不是相位候选。",
        }
        exact["orchestration_topology"]["summary"].update(
            {
                "same_source_unison_phase_candidate_count": 0,
                "same_source_exact_unison_level_stack_candidate_count": 1,
            }
        )
        exact["orchestration_topology"]["warnings"] = [exact_warning]
        exact["warnings"][-1] = exact_warning
        exact["summary"]["same_source_unison_phase_candidates"] = 0
        exact["summary"][
            "same_source_exact_unison_level_stack_candidates"
        ] = 1
        self.assert_valid(exact)

        invalid = copy.deepcopy(report)
        invalid["orchestration_topology"]["pairs"][0]["extra"] = True
        self.assert_invalid(invalid)

        invalid = copy.deepcopy(report)
        invalid["orchestration_topology"]["warnings"][0]["extra"] = True
        self.assert_invalid(invalid)

        invalid = copy.deepcopy(report)
        invalid["orchestration_topology"]["pairs"][0][
            "same_source"
        ] = False
        self.assert_invalid(invalid)

    def test_insufficient_overlap_cannot_claim_a_measurement(self) -> None:
        report = _insufficient_report()
        relation = report["balance_relations"][0]
        self.assertNotIn("gain_automation_draft", relation)
        self.assertEqual(
            relation["temporal_balance"]["candidate_segments"],
            [],
        )
        relation["measured_offset_db"] = 0.0
        self.assert_invalid(report)

        report = _insufficient_report()
        report["balance_relations"][0]["deviation_db"] = 0.0
        self.assert_invalid(report)

        report = _insufficient_report()
        report["balance_relations"][0]["overlap_evidence"][
            "status"
        ] = "sufficient"
        self.assert_invalid(report)

        report = _measured_report("suggest")
        report["balance_relations"][0]["overlap_evidence"][
            "status"
        ] = "insufficient"
        self.assert_invalid(report)

        report = _insufficient_report()
        report["balance_relations"][0]["gain_automation_draft"] = (
            _measured_report("suggest")["balance_relations"][0][
                "gain_automation_draft"
            ]
        )
        self.assert_invalid(report)

    def test_every_report_object_layer_rejects_unknown_fields(self) -> None:
        base = _measured_report("suggest")
        mutations = (
            lambda item: item.update({"extra": True}),
            lambda item: item["analysis"].update({"extra": True}),
            lambda item: item["analysis"]["spectral_screening"].update(
                {"extra": True}
            ),
            lambda item: item["analysis"]["temporal_screening"].update(
                {"extra": True}
            ),
            lambda item: item["analysis"]["stage_screening"].update(
                {"extra": True}
            ),
            lambda item: item["analysis"]["workload"].update(
                {"extra": True}
            ),
            lambda item: item["stems"][0].update({"extra": True}),
            lambda item: item["stems"][0]["metrics"].update({"extra": True}),
            lambda item: item["stems"][0]["metrics"]["gate"].update(
                {"extra": True}
            ),
            lambda item: item["stems"][0]["metrics"][
                "band_energy_ratios"
            ].update({"extra": 0.0}),
            lambda item: item["stems"][0]["role"].update({"extra": True}),
            lambda item: item["balance_relations"][0].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "subject_endpoint"
            ].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "measurement"
            ].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "overlap_evidence"
            ].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "spectral_overlap"
            ].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "spectral_overlap"
            ]["first_band_energy_ratios"].update({"extra": 0.0}),
            lambda item: item["balance_relations"][0][
                "spectral_screening"
            ].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "temporal_balance"
            ].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "temporal_balance"
            ]["candidate_segment_policy"].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "temporal_balance"
            ]["candidate_segments"][0].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "temporal_balance"
            ]["quantization"].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "gain_automation_draft"
            ].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "gain_automation_draft"
            ]["subject_endpoint"].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "gain_automation_draft"
            ]["segments"][0].update({"extra": True}),
            lambda item: item["balance_relations"][0][
                "gain_automation_draft"
            ]["workflow"].update({"extra": True}),
            lambda item: item["orchestration_topology"].update(
                {"extra": True}
            ),
            lambda item: item["orchestration_topology"][
                "analysis"
            ].update({"extra": True}),
            lambda item: item["orchestration_topology"][
                "summary"
            ].update({"extra": True}),
            lambda item: item["stage_metrics"].update({"extra": True}),
            lambda item: item["stage_metrics"]["final"].update(
                {"extra": True}
            ),
            lambda item: item["summary"].update({"extra": True}),
            lambda item: item["warnings"][0].update({"extra": True}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(layer=index):
                document = copy.deepcopy(base)
                mutate(document)
                self.assert_invalid(document)

    def test_null_is_limited_to_metrics_that_can_be_inconclusive(self) -> None:
        report = _silent_report()
        report["stems"][0]["metrics"]["sample_peak"] = None
        self.assert_invalid(report)

        report = _measured_report("suggest")
        report["balance_relations"][0]["measured_offset_db"] = None
        self.assert_invalid(report)

    def test_schema_constants_bind_the_report_identity_and_scope(self) -> None:
        cases = (
            ("format", "other"),
            ("version", 1),
            ("scope", "formal_review"),
            ("relation_audio_stage", "post_master"),
            ("relation_sample_stage", "pcm24_decoded"),
            ("stage_metrics_sample_stage", "float32_pre_pcm24"),
            ("audio_modified", True),
            ("notice", "听起来不错"),
        )
        for key, value in cases:
            with self.subTest(key=key):
                report = _measured_report("analyze")
                report[key] = value
                self.assert_invalid(report)

    def test_schema_rejects_analysis_windows_outside_workload_bounds(
        self,
    ) -> None:
        for key, value in (
            ("window_ms", 19.999),
            ("window_ms", 2000.001),
            ("hop_ms", 9.999),
            ("hop_ms", 2000.001),
        ):
            with self.subTest(layer="analysis", key=key, value=value):
                report = _measured_report("analyze")
                report["analysis"][key] = value
                self.assert_invalid(report)

        for key, value in (
            ("window_seconds", 0.019999),
            ("window_seconds", 2.000001),
            ("hop_seconds", 0.009999),
            ("hop_seconds", 2.000001),
        ):
            with self.subTest(layer="gate", key=key, value=value):
                report = _measured_report("analyze")
                report["stems"][0]["metrics"]["gate"][key] = value
                self.assert_invalid(report)

    def test_all_declared_object_layers_are_closed(self) -> None:
        closed = (
            self.schema,
            self.schema["properties"]["analysis"],
            self.schema["properties"]["analysis"]["properties"][
                "spectral_screening"
            ],
            self.schema["properties"]["analysis"]["properties"][
                "temporal_screening"
            ],
            self.schema["properties"]["analysis"]["properties"][
                "stage_screening"
            ],
            self.schema["properties"]["analysis"]["properties"][
                "workload"
            ],
            self.schema["properties"]["summary"],
            self.schema["$defs"]["gate"],
            self.schema["$defs"]["role"],
            self.schema["$defs"]["trackMetrics"],
            self.schema["$defs"]["trackMetrics"]["properties"][
                "band_energy_ratios"
            ],
            self.schema["$defs"]["stem"],
            self.schema["$defs"]["overlapMeasurement"],
            self.schema["$defs"]["balanceRelationResult"],
            self.schema["$defs"]["overlapEvidence"],
            self.schema["$defs"]["nullableBandNumbers"],
            self.schema["$defs"]["spectralOverlap"],
            self.schema["$defs"]["spectralScreening"],
            self.schema["$defs"]["temporalBalance"],
            self.schema["$defs"]["candidateSegmentPolicy"],
            self.schema["$defs"]["temporalBalanceSegment"],
            self.schema["$defs"]["temporalBalance"]["properties"][
                "quantization"
            ],
            self.schema["$defs"]["gainAutomationDraftSegment"],
            self.schema["$defs"]["gainAutomationDraft"],
            self.schema["$defs"]["gainAutomationDraftWorkflow"],
            self.schema["$defs"]["topologyExecutor"],
            self.schema["$defs"]["orchestrationTopologyPair"],
            self.schema["$defs"]["orchestrationTopologyAnalysis"],
            self.schema["$defs"]["orchestrationTopologySummary"],
            self.schema["$defs"]["orchestrationTopology"],
            self.schema["properties"]["stage_metrics"],
            self.schema["$defs"]["stereoStageMetrics"],
            self.schema["$defs"]["tailWindowMetrics"],
            self.schema["$defs"]["insufficientOverlapWarning"],
            self.schema["$defs"]["outsideToleranceWarning"],
            self.schema["$defs"]["spectralOverlapWarning"],
            self.schema["$defs"]["temporalBalanceWarning"],
            self.schema["$defs"]["sameSourceUnisonPhaseWarning"],
            self.schema["$defs"]["monoFoldWarning"],
            self.schema["$defs"]["spaceTailWarning"],
        )
        for layer in closed:
            self.assertFalse(layer["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
