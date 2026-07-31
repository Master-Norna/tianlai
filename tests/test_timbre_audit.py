from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import unittest

import numpy as np

from tianlai.timbre_audit import (
    CLAIM,
    TimbreAuditError,
    analyze_timbre_audio,
    build_machine_timbre_matrix_report,
    enumerate_integer_notes,
    validate_machine_timbre_matrix_report,
)


def digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def coordinate(
    midi_note: int | None,
    velocity: int,
    variant: str = "variant-a",
    *,
    bundle: str | None = None,
) -> dict:
    return {
        "runtime_configuration_sha256": digest("configuration"),
        "final_articulation": "sustain",
        "midi_note": midi_note,
        "velocity": velocity,
        "variant_lane_sha256": digest("lane:" + variant),
        "variant_bundle_sha256": digest("bundle:" + (bundle or variant)),
    }


def metrics(
    *,
    loudness: float = -20.0,
    centroid: float = 1_000.0,
    rolloff: float = 2_000.0,
    flatness: float = 0.1,
    blockers: list[str] | None = None,
) -> dict:
    return {
        "analysis_version": 1,
        "sample_rate_hz": 48_000,
        "frame_count": 48_000,
        "note_on_frame": 4_800,
        "note_off_frame": 43_200,
        "peak": 0.5,
        "peak_dbfs": -6.020599913279624,
        "rms": 0.1,
        "loudness_proxy_dbfs": loudness,
        "crest_factor_db": 13.979400086720377,
        "left_rms": 0.1,
        "right_rms": 0.1,
        "pre_roll_rms": 0.0,
        "tail_rms": 0.0,
        "dc_offset_left": 0.0,
        "dc_offset_right": 0.0,
        "clipping_sample_count": 0,
        "spectral_centroid_hz": centroid,
        "spectral_rolloff_85_hz": rolloff,
        "spectral_bandwidth_hz": 400.0,
        "spectral_flatness": flatness,
        "stereo_correlation": 1.0,
        "stereo_width_ratio": 0.0,
        "peak_after_note_on_seconds": 0.01,
        "active_tail_seconds": 0.0,
        "pitch": None,
        "machine_blockers": list(blockers or ()),
    }


def observation(raw_coordinate: dict, raw_metrics: dict) -> dict:
    midi_note = raw_coordinate["midi_note"]
    root = float(midi_note - 5) if midi_note is not None else 55.0
    return {
        "coordinate": raw_coordinate,
        "metrics": raw_metrics,
        "performance_sha256": digest("performance" + repr(raw_coordinate)),
        "wav_sha256": digest("wav" + repr(raw_coordinate)),
        "selection_receipt_sha256": digest("receipt" + repr(raw_coordinate)),
        "variant_coverage_status": "all_runtime_variants",
        "variant_coverage_proof_sha256": digest(
            "variant-proof" + repr(raw_coordinate)
        ),
        "runtime_fingerprint_sha256": digest("runtime"),
        "source_facts": {
            "source_mode": "sampled",
            "selection_scope": "actual_audible_bundle",
            "selected_root_midis": [root],
            "transposition_semitones": [5.0],
            "maximum_absolute_transposition_semitones": 5.0,
        },
    }


class TimbreSignalAnalysisTests(unittest.TestCase):
    def test_antiphase_stereo_is_audible_and_spectrally_measured(self) -> None:
        sample_rate = 48_000
        frame_count = 12_000
        note_on = 1_000
        note_off = 11_000
        phase = np.arange(note_off - note_on) / sample_rate
        tone = 0.2 * np.sin(2.0 * math.pi * 440.0 * phase)
        audio = np.zeros((frame_count, 2), dtype=np.float64)
        audio[note_on:note_off, 0] = tone
        audio[note_on:note_off, 1] = -tone

        report = analyze_timbre_audio(
            audio,
            sample_rate,
            note_on,
            note_off,
            expected_hz=440.0,
        )

        self.assertGreater(report["rms"], 0.1)
        self.assertGreater(report["spectral_centroid_hz"], 400.0)
        self.assertLess(report["stereo_correlation"], -0.99)
        self.assertNotIn(
            "silent_or_insufficient_signal_to_noise",
            report["machine_blockers"],
        )
        self.assertEqual(report["pitch"]["nearest_octave_error"], 0)

    def test_objective_integrity_failures_become_blockers(self) -> None:
        audio = np.zeros((4_000, 2), dtype=np.float64)
        audio[:1_000] = 0.02
        audio[1_000:3_000] = 0.001
        audio[2_000, 0] = 1.2

        report = analyze_timbre_audio(
            audio,
            48_000,
            1_000,
            3_000,
            pre_quantization_clipping_sample_count=1,
        )

        self.assertIn("pre_quantization_clipping", report["machine_blockers"])
        self.assertIn("pre_roll_leak", report["machine_blockers"])
        self.assertIn(
            "silent_or_insufficient_signal_to_noise",
            report["machine_blockers"],
        )

    def test_nonfinite_audio_is_rejected_not_summarized(self) -> None:
        audio = np.zeros((5_000, 2), dtype=np.float64)
        audio[2_000, 0] = np.nan
        with self.assertRaisesRegex(TimbreAuditError, "non-finite"):
            analyze_timbre_audio(audio, 48_000, 1_000, 4_000)


class TimbreMatrixTests(unittest.TestCase):
    def test_disjoint_ranges_never_fill_a_hole(self) -> None:
        self.assertEqual(
            enumerate_integer_notes(((40, 42), (48.2, 50.8))),
            (40, 41, 42, 49, 50),
        )

    def test_missing_cell_and_signal_failure_block_before_humans(self) -> None:
        first = coordinate(60, 32)
        second = coordinate(61, 32)
        report = build_machine_timbre_matrix_report(
            (first, second),
            (
                observation(
                    first,
                    metrics(blockers=["pre_quantization_clipping"]),
                ),
            ),
        )

        self.assertFalse(report["automatic_approval"])
        self.assertEqual(report["claim"], CLAIM)
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["disposition"], "blocked_before_human_review")
        self.assertEqual(
            {item["kind"] for item in report["machine_blockers"]},
            {"missing_matrix_cells", "cell_machine_blockers"},
        )

    def test_unavailable_audible_source_facts_block_the_cell(self) -> None:
        raw_coordinate = coordinate(60, 80)
        raw_observation = observation(raw_coordinate, metrics())
        raw_observation["source_facts"] = {
            "source_mode": "unavailable",
            "selection_scope": "unverified",
            "selected_root_midis": [],
            "transposition_semitones": [],
            "maximum_absolute_transposition_semitones": None,
        }
        report = build_machine_timbre_matrix_report(
            (raw_coordinate,),
            (raw_observation,),
        )
        self.assertEqual(report["disposition"], "blocked_before_human_review")
        self.assertEqual(
            report["machine_blockers"][0]["reasons"],
            ["source_selection_facts_unavailable"],
        )

    def test_partial_runtime_variant_coverage_can_never_be_machine_complete(self) -> None:
        raw_coordinate = coordinate(60, 80)
        raw_observation = observation(raw_coordinate, metrics())
        raw_observation["variant_coverage_status"] = "runtime_default_only"
        raw_observation["variant_coverage_proof_sha256"] = None
        report = build_machine_timbre_matrix_report(
            (raw_coordinate,),
            (raw_observation,),
        )
        self.assertEqual(report["disposition"], "blocked_before_human_review")
        self.assertEqual(
            report["machine_blockers"][0]["reasons"],
            ["runtime_variant_coverage_incomplete:runtime_default_only"],
        )

    def test_pitch_velocity_and_variant_anomalies_are_triage_only(self) -> None:
        coordinates = [
            coordinate(60, 32, "a"),
            coordinate(61, 32, "a"),
            coordinate(60, 120, "a"),
            coordinate(60, 32, "b"),
            coordinate(60, 32, "c"),
        ]
        observations = [
            observation(coordinates[0], metrics(loudness=-15, centroid=500)),
            observation(
                coordinates[1],
                metrics(loudness=-15, centroid=4_000, flatness=0.8),
            ),
            observation(
                coordinates[2],
                metrics(loudness=-25, centroid=3_000),
            ),
            observation(
                coordinates[3],
                metrics(loudness=-15, centroid=520),
            ),
            observation(
                coordinates[4],
                metrics(loudness=-40, centroid=8_000),
            ),
        ]

        report = build_machine_timbre_matrix_report(
            coordinates,
            observations,
        )

        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(
            report["disposition"],
            "machine_complete_human_review_required",
        )
        kinds = {item["kind"] for item in report["anomaly_candidates"]}
        self.assertEqual(
            kinds,
            {
                "adjacent_pitch_continuity",
                "velocity_response",
                "variant_consistency",
            },
        )
        self.assertFalse(report["automatic_approval"])

    def test_exact_sample_mapping_boundaries_are_always_listening_points(self) -> None:
        left = coordinate(60, 80, "rr1", bundle="root-60")
        right = coordinate(61, 80, "rr1", bundle="root-61")
        report = build_machine_timbre_matrix_report(
            (left, right),
            (
                observation(left, metrics()),
                observation(right, metrics()),
            ),
        )
        self.assertEqual(
            [item["kind"] for item in report["anomaly_candidates"]],
            ["source_mapping_boundary"],
        )

    def test_report_hash_and_never_approve_claim_are_fail_closed(self) -> None:
        raw_coordinate = coordinate(60, 80)
        report = build_machine_timbre_matrix_report(
            (raw_coordinate,),
            (observation(raw_coordinate, metrics()),),
        )
        self.assertEqual(
            validate_machine_timbre_matrix_report(report),
            report,
        )

        tampered = copy.deepcopy(report)
        tampered["automatic_approval"] = True
        with self.assertRaisesRegex(TimbreAuditError, "never claim approval"):
            validate_machine_timbre_matrix_report(tampered)

        tampered = copy.deepcopy(report)
        tampered["cells"][0]["metrics"]["spectral_centroid_hz"] = 9999.0
        with self.assertRaisesRegex(TimbreAuditError, "self-hash"):
            validate_machine_timbre_matrix_report(tampered)

    def test_duplicate_or_unbound_observation_is_rejected(self) -> None:
        raw_coordinate = coordinate(60, 80)
        raw_observation = observation(raw_coordinate, metrics())
        with self.assertRaisesRegex(TimbreAuditError, "repeat"):
            build_machine_timbre_matrix_report(
                (raw_coordinate,),
                (raw_observation, raw_observation),
            )

        broken = copy.deepcopy(raw_observation)
        broken["wav_sha256"] = "not-a-hash"
        with self.assertRaisesRegex(TimbreAuditError, "lowercase SHA-256"):
            build_machine_timbre_matrix_report(
                (raw_coordinate,),
                (broken,),
            )

        broken = copy.deepcopy(raw_observation)
        broken["source_facts"]["transposition_semitones"] = [4.0]
        broken["source_facts"][
            "maximum_absolute_transposition_semitones"
        ] = 4.0
        with self.assertRaisesRegex(TimbreAuditError, "target minus root"):
            build_machine_timbre_matrix_report(
                (raw_coordinate,),
                (broken,),
            )

    def test_generated_report_matches_the_published_json_schema(self) -> None:
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema is not installed")
        raw_coordinate = coordinate(60, 80)
        report = build_machine_timbre_matrix_report(
            (raw_coordinate,),
            (observation(raw_coordinate, metrics()),),
        )
        schema = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "schemas"
                / "timbre-matrix-report.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(report)


if __name__ == "__main__":
    unittest.main()
