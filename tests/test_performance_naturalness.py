from __future__ import annotations

import copy

from tianlai.canonical_json import canonical_json_sha256
from tianlai.performance_naturalness import analyze_performance_naturalness
from tianlai.score import parse_score_document


def _score(
    *,
    note_count: int = 8,
    phrases: list[dict] | None = None,
    tied_pair: bool = False,
    default_articulation: str | None = None,
):
    notes = []
    for index in range(note_count):
        note = {
            "event_id": f"event-{index + 1}",
            "bar": index // 4 + 1,
            "beat": float(index % 4 + 1),
            "duration_beats": 1.0,
            "pitch": "C4",
        }
        if tied_pair and index == 0:
            note["tie"] = True
        notes.append(note)
    return parse_score_document(
        {
            "schema_version": 1,
            "title": "Naturalness fixture",
            "tempo_map": [
                {
                    "bar": 1,
                    "beat": 1.0,
                    "bpm": 120.0,
                    "beats_per_bar": 4,
                    "beat_unit": 4,
                }
            ],
            "parts": [
                {
                    "id": "lead",
                    "name": "Lead",
                    "notes": notes,
                    **({"phrases": phrases} if phrases is not None else {}),
                    **(
                        {"default_articulation": default_articulation}
                        if default_articulation is not None
                        else {}
                    ),
                }
            ],
        }
    )


def _trace_plan(note_count: int = 8, *, residual: bool = False) -> dict:
    trace = []
    for index in range(note_count):
        baseline_start = index * 0.5
        jitter = 0.008 if index % 2 == 0 else -0.008
        velocity_jitter = 0.03 if index % 2 == 0 else -0.03
        derivation = {}
        if residual:
            derivation["残差随机"] = (
                f"时值 {jitter * 1000:+.1f}ms / 力度 {velocity_jitter:+.3f}"
            )
        trace.append(
            {
                "小节": index // 4 + 1,
                "拍": float(index % 4 + 1),
                "音": "C4",
                "时间": round(baseline_start + (jitter if residual else 0.0), 6),
                "时长": 0.5,
                "力度": round(0.5 + (velocity_jitter if residual else 0.0), 4),
                "奏法": None,
                "推导": derivation,
                "source_event_id": f"event-{index + 1}",
            }
        )
    return {
        "title": "Naturalness fixture",
        "sample_rate": 48_000,
        "duration_seconds": note_count * 0.5,
        "roster": "fixture",
        "expression": {
            "mode": "ensemble",
            "structural": True,
            "physical": True,
            "range_mode": "compatibility",
            "humanize": {
                "depth": 1.0 if residual else 0.0,
                "timing_ms": 8.0,
                "velocity": 0.03,
                "seed": 0,
            },
        },
        "warnings": [],
        "parts": [
            {
                "executor_id": "lead-player",
                "part_id": "lead",
                "instrument": "fixture/instrument",
                "dynamic_compression": 0.0,
                "trace": trace,
            }
        ],
    }


def test_explicit_phrase_gaps_and_overlaps_follow_execution_semantics() -> None:
    score = _score(
        note_count=4,
        phrases=[
            {
                "start_bar": 1,
                "start_beat": 1.0,
                "end_bar": 1,
                "end_beat": 2.0,
            },
            {
                "start_bar": 1,
                "start_beat": 2.0,
                "end_bar": 1,
                "end_beat": 3.0,
            },
        ],
    )

    report = analyze_performance_naturalness(score, _trace_plan(4))
    by_code = {item["code"]: item for item in report["candidates"]}

    assert "performance.explicit_phrase_coverage_gap" in by_code
    assert "performance.explicit_phrase_overlap" in by_code
    assert by_code["performance.explicit_phrase_coverage_gap"]["evidence"][
        "affected_onset_count"
    ] == 1
    assert by_code["performance.explicit_phrase_overlap"]["evidence"][
        "affected_onset_count"
    ] == 1
    assert report["authority"]["workflow_blocking"] is False
    assert report["authority"]["automatic_change"] is False


def test_tie_continuation_is_not_a_false_phrase_gap() -> None:
    score = _score(
        note_count=2,
        tied_pair=True,
        phrases=[
            {
                "start_bar": 1,
                "start_beat": 1.0,
                "end_bar": 1,
                "end_beat": 1.0,
            }
        ],
    )

    plan = _trace_plan(2)
    plan["parts"][0]["trace"] = plan["parts"][0]["trace"][:1]
    report = analyze_performance_naturalness(score, plan)

    assert all(
        item["code"] != "performance.explicit_phrase_coverage_gap"
        for item in report["candidates"]
    )


def test_residual_randomness_reports_relation_flips_without_aesthetic_score() -> None:
    report = analyze_performance_naturalness(
        _score(),
        _trace_plan(residual=True),
        binding={
            "score_sha256": "a" * 64,
            "performance_plan_sha256": "b" * 64,
        },
    )
    by_code = {item["code"]: item for item in report["candidates"]}

    finding = by_code["performance.residual_connection_flip_candidate"]
    assert finding["evidence"]["reported_flip_count"] == 7
    assert finding["level"] == "info"
    assert finding["blocking"] is False
    assert finding["automatic_change"] is False
    assert "naturalness_score" not in report
    assert "pass" not in report
    assert report["facts"]["waveform_response"]["status"] == "unavailable"
    assert (
        report["facts"]["waveform_response"][
            "global_metrics_used_as_naturalness_evidence"
        ]
        is False
    )


def test_connection_candidate_identity_ignores_example_display_bound() -> None:
    score = _score()
    plan = _trace_plan(residual=True)

    one = analyze_performance_naturalness(score, plan, max_examples=1)
    eight = analyze_performance_naturalness(score, plan, max_examples=8)

    one_finding = next(
        item
        for item in one["candidates"]
        if item["code"] == "performance.residual_connection_flip_candidate"
    )
    eight_finding = next(
        item
        for item in eight["candidates"]
        if item["code"] == "performance.residual_connection_flip_candidate"
    )
    assert one_finding["candidate_id"] == eight_finding["candidate_id"]
    assert one_finding["evidence"]["examples_truncated"] is True
    assert eight_finding["evidence"]["examples_truncated"] is False


def test_connection_candidate_level_does_not_depend_on_velocity_residual() -> None:
    score = _score(default_articulation="sustain")
    with_velocity = _trace_plan(residual=True)
    without_velocity = copy.deepcopy(with_velocity)
    for item in without_velocity["parts"][0]["trace"]:
        timing = item["推导"]["残差随机"].split(" / ", 1)[0]
        item["推导"]["残差随机"] = f"{timing} / 力度 +0.000"

    first = analyze_performance_naturalness(score, with_velocity)
    second = analyze_performance_naturalness(score, without_velocity)
    first_finding = next(
        item
        for item in first["candidates"]
        if item["code"] == "performance.residual_connection_flip_candidate"
    )
    second_finding = next(
        item
        for item in second["candidates"]
        if item["code"] == "performance.residual_connection_flip_candidate"
    )

    assert first_finding["candidate_id"] == second_finding["candidate_id"]
    assert first_finding["level"] == second_finding["level"] == "info"
    assert first_finding["evidence"]["reported_flip_count"] == (
        second_finding["evidence"]["reported_flip_count"]
    )


def test_default_articulation_does_not_hide_random_connection_change() -> None:
    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"),
        _trace_plan(residual=True),
    )

    finding = next(
        item
        for item in report["candidates"]
        if item["code"] == "performance.residual_connection_flip_candidate"
    )
    assert finding["evidence"]["reported_flip_count"] == 7


def test_exact_timing_realization_is_excluded_from_jitter_counterfactual() -> None:
    plan = _trace_plan(residual=True)
    for item in plan["parts"][0]["trace"]:
        item["推导"]["realization"] = {
            "timing_offset_ms": {"strategy": "replace", "value": 0.0}
        }

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    assert all(
        item["code"] != "performance.residual_connection_flip_candidate"
        for item in report["candidates"]
    )
    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["excluded_connection_event_count"] == 8
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_velocity_only_realization_is_also_a_quantized_counterfactual() -> None:
    plan = _trace_plan(residual=True)
    for item in plan["parts"][0]["trace"]:
        item["推导"]["realization"] = {
            "velocity": {"strategy": "replace", "value": 0.6}
        }

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["excluded_connection_event_count"] == 8
    assert executor["connection_check_status"] == "partial_evidence"
    assert all(
        item["code"] != "performance.residual_connection_flip_candidate"
        for item in report["candidates"]
    )


def test_without_residual_realization_and_clipping_need_no_counterfactual() -> None:
    plan = _trace_plan()
    for item in plan["parts"][0]["trace"]:
        item["推导"]["realization"] = {
            "velocity": {"strategy": "replace", "value": 0.6}
        }
    first = plan["parts"][0]["trace"][0]
    first["推导"]["时间边界"] = "fixture boundary"
    first["推导"]["发音补偿审计"] = {
        "status": "applied",
        "context": "isolated_attack",
        "final_articulation": "sustain",
        "clipped_delay_seconds": 0.01,
    }

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["parsed_residual_evidence_count"] == 0
    assert executor["usable_timing_residual_event_count"] == 0
    assert executor["excluded_connection_event_count"] == 0
    assert executor["connection_check_status"] == "ready"
    assert report["evidence_coverage"] == "complete_for_current_checks"


def test_excluded_middle_event_is_a_barrier_not_a_new_adjacency() -> None:
    plan = _trace_plan(residual=True)
    plan["parts"][0]["trace"][3]["推导"]["realization"] = {
        "gate_ratio": {"strategy": "replace", "value": 0.8}
    }

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    assert all(
        item["code"] != "performance.residual_connection_flip_candidate"
        for item in report["candidates"]
    )
    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["excluded_connection_event_count"] == 1
    assert executor["comparable_connection_count"] == 0
    assert executor["connection_check_status"] == "partial_evidence"


def test_reconstructed_negative_start_is_a_counterfactual_boundary() -> None:
    plan = _trace_plan(residual=True)
    plan["parts"][0]["trace"][0]["时间"] = 0.004

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["excluded_connection_event_count"] == 1
    assert executor["connection_check_status"] == "partial_evidence"
    assert all(
        item["code"] != "performance.residual_connection_flip_candidate"
        for item in report["candidates"]
    )


def test_missing_event_mapping_is_explicit_partial_evidence() -> None:
    plan = _trace_plan(residual=True)
    for item in plan["parts"][0]["trace"]:
        item.pop("source_event_id")

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["unmapped_connection_event_count"] == 8
    assert executor["usable_connection_event_count"] == 0
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_malformed_residual_evidence_cannot_masquerade_as_no_randomness() -> None:
    plan = _trace_plan(residual=True)
    plan["parts"][0]["trace"][0]["推导"]["残差随机"] = (
        "时值 +8.0ms / 力度 unavailable"
    )
    plan["parts"][0]["trace"][1]["推导"]["残差随机"] = (
        f"时值 {'9' * 400}ms / 力度 +0.030"
    )

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["invalid_residual_evidence_count"] == 2
    assert executor["parsed_residual_evidence_count"] == 6
    assert executor["usable_timing_residual_event_count"] == 6
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["facts"]["performance_plan"][
        "invalid_residual_evidence_count"
    ] == 2
    assert report["evidence_coverage"] == "partial"


def test_declared_timing_humanize_requires_residual_trace_evidence() -> None:
    plan = _trace_plan(residual=True)
    for item in plan["parts"][0]["trace"]:
        item["推导"].pop("残差随机")

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["missing_residual_evidence_count"] == 8
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_residual_trace_cannot_contradict_disabled_humanize() -> None:
    plan = _trace_plan(residual=True)
    plan["expression"]["humanize"]["depth"] = 0.0

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["parsed_residual_evidence_count"] == 8
    assert executor["usable_timing_residual_event_count"] == 0
    assert executor["unexpected_residual_evidence_count"] == 8
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_velocity_only_humanize_residual_is_parsed_but_not_timing_evidence() -> None:
    plan = _trace_plan()
    plan["expression"]["humanize"]["depth"] = 1.0
    plan["expression"]["humanize"]["timing_ms"] = 0.0
    for index, item in enumerate(plan["parts"][0]["trace"]):
        velocity = "+0.030" if index % 2 == 0 else "-0.030"
        item["推导"]["残差随机"] = f"时值 +0.0ms / 力度 {velocity}"

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["parsed_residual_evidence_count"] == 8
    assert executor["usable_timing_residual_event_count"] == 0
    assert executor["connection_check_status"] == "ready"
    assert report["evidence_coverage"] == "complete_for_current_checks"


def test_residual_timing_must_fit_declared_humanize_range() -> None:
    plan = _trace_plan(residual=True)
    plan["parts"][0]["trace"][0]["推导"]["残差随机"] = (
        "时值 +99.0ms / 力度 +0.030"
    )

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["out_of_range_residual_evidence_count"] == 1
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_extended_humanize_depth_uses_the_conductor_contract() -> None:
    plan = _trace_plan(residual=True)
    plan["expression"]["humanize"]["depth"] = 4.0

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    contract = report["facts"]["performance_plan"]["humanize_timing_contract"]
    assert contract["status"] == "ready"
    assert contract["depth"] == 4.0
    assert report["evidence_coverage"] == "complete_for_current_checks"


def test_assigned_part_score_to_trace_coverage_is_bidirectional() -> None:
    plan = _trace_plan(residual=True)
    plan["parts"][0]["trace"] = plan["parts"][0]["trace"][:2]

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    coverage = report["facts"]["performance_plan"]["part_trace_coverage"][0]
    assert coverage["status"] == "partial_evidence"
    assert coverage["observed_score_event_count"] == 2
    assert coverage["missing_score_event_count"] == 6
    assert report["status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_duplicate_event_id_within_one_executor_is_partial_evidence() -> None:
    plan = _trace_plan(2, residual=True)
    duplicate = copy.deepcopy(plan["parts"][0]["trace"][0])
    duplicate["时间"] = 0.25
    plan["parts"][0]["trace"].append(duplicate)

    report = analyze_performance_naturalness(
        _score(note_count=2, default_articulation="sustain"), plan
    )

    coverage = report["facts"]["performance_plan"]["part_trace_coverage"][0]
    executor = report["facts"]["performance_plan"]["executors"][0]
    assert coverage["duplicate_trace_event_count"] == 1
    assert executor["duplicate_trace_event_count"] == 1
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_cross_part_event_ids_are_mismatches_not_valid_mapping() -> None:
    parts = []
    for part_id, prefix, pitch in (
        ("lead", "event", "C4"),
        ("other", "other", "D4"),
    ):
        parts.append(
            {
                "id": part_id,
                "name": part_id,
                "default_articulation": "sustain",
                "notes": [
                    {
                        "event_id": f"{prefix}-{index + 1}",
                        "bar": index // 4 + 1,
                        "beat": float(index % 4 + 1),
                        "duration_beats": 1.0,
                        "pitch": pitch,
                    }
                    for index in range(8)
                ],
            }
        )
    score = parse_score_document(
        {
            "schema_version": 1,
            "title": "Cross-part mapping fixture",
            "tempo_map": [
                {
                    "bar": 1,
                    "beat": 1.0,
                    "bpm": 120.0,
                    "beats_per_bar": 4,
                    "beat_unit": 4,
                }
            ],
            "parts": parts,
        }
    )
    plan = _trace_plan(residual=True)
    for index, item in enumerate(plan["parts"][0]["trace"]):
        item["source_event_id"] = f"other-{index + 1}"

    report = analyze_performance_naturalness(score, plan)

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["mismatched_connection_event_count"] == 8
    assert executor["mapped_connection_event_count"] == 0
    assert executor["connection_check_status"] == "partial_evidence"
    coverage = report["facts"]["performance_plan"]["part_trace_coverage"][0]
    assert coverage["part_id"] == "lead"
    assert coverage["missing_score_event_count"] == 8
    assert report["evidence_coverage"] == "partial"


def test_nonfinite_trace_timing_is_invalid_partial_evidence() -> None:
    plan = _trace_plan(residual=True)
    plan["parts"][0]["trace"][0]["时间"] = float("nan")

    report = analyze_performance_naturalness(
        _score(default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["invalid_connection_event_count"] == 1
    assert executor["connection_check_status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_residual_rounding_near_relation_boundary_is_indeterminate() -> None:
    plan = _trace_plan(2, residual=True)
    plan["parts"][0]["trace"][0]["时长"] = 0.49495

    report = analyze_performance_naturalness(
        _score(note_count=2, default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["indeterminate_connection_count"] == 1
    assert executor["comparable_connection_count"] == 0
    assert executor["connection_check_status"] == "partial_evidence"
    assert all(
        item["code"] != "performance.residual_connection_flip_candidate"
        for item in report["candidates"]
    )


def test_exact_relation_boundary_without_residual_is_not_indeterminate() -> None:
    plan = _trace_plan(2)
    plan["parts"][0]["trace"][0]["时长"] = 0.495

    report = analyze_performance_naturalness(
        _score(note_count=2, default_articulation="sustain"), plan
    )

    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["indeterminate_connection_count"] == 0
    assert executor["connection_check_status"] == "ready"
    assert report["evidence_coverage"] == "complete_for_current_checks"


def test_missing_score_never_claims_no_machine_candidate() -> None:
    report = analyze_performance_naturalness(None, _trace_plan())

    assert report["status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"
    assert any(
        item["reason_code"] == "parsed_score_not_supplied"
        for item in report["unavailable"]
    )


def test_expected_connected_onset_context_is_fact_not_false_candidate() -> None:
    plan = _trace_plan()
    trace = plan["parts"][0]["trace"]
    for index, item in enumerate(trace):
        item["推导"]["发音补偿审计"] = {
            "status": (
                "applied" if index % 2 == 0 else "not_applied_unapproved_context"
            ),
            "context": (
                "isolated_attack" if index % 2 == 0 else "connected_transition"
            ),
            "final_articulation": "sustain",
        }

    report = analyze_performance_naturalness(_score(), plan)

    assert all(
        item["code"] != "performance.onset_runtime_configuration_mismatch"
        for item in report["candidates"]
    )
    groups = report["facts"]["performance_plan"]["executors"][0][
        "onset_compensation_groups"
    ]
    assert len(groups) == 2


def test_onset_runtime_configuration_mismatch_is_bounded_candidate() -> None:
    plan = _trace_plan()
    trace = plan["parts"][0]["trace"]
    for item in trace:
        item["推导"]["发音补偿审计"] = {
            "status": "not_applied_runtime_configuration_mismatch",
            "context": "isolated_attack",
            "final_articulation": "sustain",
        }

    report = analyze_performance_naturalness(_score(), plan, max_examples=2)

    finding = next(
        item
        for item in report["candidates"]
        if item["code"] == "performance.onset_runtime_configuration_mismatch"
    )
    assert finding["level"] == "info"
    assert finding["evidence"]["examples_truncated_by_status"] == {
        "not_applied_runtime_configuration_mismatch": True
    }


def test_one_realization_does_not_hide_a_long_sparse_part() -> None:
    plan = _trace_plan(20)
    plan["parts"][0]["trace"][0]["推导"]["realization"] = {
        "velocity": {"strategy": "replace", "value": 0.6}
    }

    report = analyze_performance_naturalness(_score(note_count=20), plan)

    finding = next(
        item
        for item in report["candidates"]
        if item["code"] == "performance.authored_direction_sparse"
    )
    assert finding["evidence"]["explicitly_directed_onset_count"] == 1
    assert finding["evidence"]["has_realization"] is True
    assert "不要为了消除候选默认增加控制" in finding["suggestions"][0]


def test_one_shot_kit_does_not_invent_note_connection_candidates() -> None:
    plan = _trace_plan(residual=True)
    plan["parts"][0]["kit_pitch"] = "F#2"

    report = analyze_performance_naturalness(_score(), plan)

    assert all(
        item["code"] != "performance.residual_connection_flip_candidate"
        for item in report["candidates"]
    )
    executor = report["facts"]["performance_plan"]["executors"][0]
    assert executor["connection_semantics"] == "not_applicable_one_shot_kit"
    assert executor["comparable_connection_count"] == 0


def test_one_shot_kit_still_requires_complete_part_trace_identity() -> None:
    plan = _trace_plan(residual=True)
    part = plan["parts"][0]
    part["kit_pitch"] = "F#2"
    extra = copy.deepcopy(part["trace"][0])
    extra["source_event_id"] = "unknown-event"
    part["trace"].append(extra)
    unidentified = copy.deepcopy(part["trace"][0])
    unidentified.pop("source_event_id")
    part["trace"].append(unidentified)

    report = analyze_performance_naturalness(_score(), plan)

    coverage = report["facts"]["performance_plan"]["part_trace_coverage"][0]
    assert coverage["unexpected_trace_event_count"] == 1
    assert coverage["unidentified_trace_event_count"] == 1
    assert coverage["status"] == "partial_evidence"
    assert report["evidence_coverage"] == "partial"


def test_static_mechanics_are_allowed_and_report_is_deterministic_and_read_only() -> None:
    score = _score()
    plan = _trace_plan()
    before = copy.deepcopy(plan)

    first = analyze_performance_naturalness(score, plan)
    second = analyze_performance_naturalness(score, plan)

    assert first == second
    assert plan == before
    assert any(
        item["code"] == "performance.authored_direction_sparse"
        for item in first["candidates"]
    )
    assert all(item["level"] != "warning" for item in first["candidates"])
    assert first["authority"]["intentional_mechanics_allowed"] is True
    assert first["report_sha256"] == canonical_json_sha256(
        {key: value for key, value in first.items() if key != "report_sha256"}
    )
