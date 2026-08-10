from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import unittest

from tianlai.capability import (
    ArticulationOnset,
    DurationArticulationRule,
    InstrumentCapability,
    OnsetEvidenceRef,
)
from tianlai.conductor import (
    ExpressionSettings,
    PerformancePlan,
    _pair_note_ids,
    build_plan,
)
from tianlai.roster import Executor, Roster, Seat
from tianlai.score import parse_score_document


_EVIDENCE_A = "a" * 64
_EVIDENCE_B = "b" * 64
_RUNTIME_FINGERPRINT = "c" * 64
_ANCHOR = "performance_note_on_output_frame"


def _evidence(sha256: str = _EVIDENCE_A) -> OnsetEvidenceRef:
    return OnsetEvidenceRef(
        path="测试乐器/发音延迟.json",
        sha256=sha256,
        runtime_fingerprint=_RUNTIME_FINGERPRINT,
        review_lead="test-review-lead",
    )


def _onset(
    articulation: str,
    *,
    frames: int,
    sample_rate_hz: int = 1_000,
    context: str = "isolated_attack",
    evidence_sha256: str = _EVIDENCE_A,
) -> ArticulationOnset:
    return ArticulationOnset(
        articulation=articulation,
        frames=frames,
        sample_rate_hz=sample_rate_hz,
        context=context,
        anchor=_ANCHOR,
        evidence=_evidence(evidence_sha256),
    )


def _capability(
    *onsets: ArticulationOnset,
    articulations: tuple[str, ...] = ("accent", "slow", "sustain"),
    default_articulation: str = "sustain",
    onset_overlap_policy: str = "conservative",
    duration_articulation_rules: tuple[DurationArticulationRule, ...] = (),
) -> InstrumentCapability:
    return InstrumentCapability(
        name="起音调度测试乐器",
        relative_path="测试/起音调度测试乐器",
        manifest_path="测试/起音调度测试乐器/乐器.json",
        implementation_type="oscillator",
        pitched=True,
        note_min=0.0,
        note_max=127.0,
        articulations=articulations,
        default_articulation=default_articulation,
        articulation_source="test",
        onset_seconds=None,
        quality_tier="candidate",
        license_status="approved",
        articulation_onsets=tuple(onsets),
        onset_overlap_policy=onset_overlap_policy,
        duration_articulation_rules=duration_articulation_rules,
    )


def _score(
    notes: list[dict[str, object]],
    *,
    default_articulation: str | None = None,
):
    part: dict[str, object] = {
        "id": "solo",
        "default_dynamic": "mf",
        "notes": notes,
    }
    if default_articulation is not None:
        part["default_articulation"] = default_articulation
    return parse_score_document(
        {
            "title": "onset scheduler contract",
            "sample_rate": 48_000,
            "tail_seconds": 0.0,
            "tempo_map": [
                {
                    "bar": 1,
                    "beat": 1,
                    "bpm": 60,
                    "beats_per_bar": 4,
                    "beat_unit": 4,
                }
            ],
            "parts": [part],
        }
    )


def _roster(
    capability: InstrumentCapability,
    *,
    articulation_map: tuple[tuple[str, str], ...] = (),
    articulation_auto: bool = True,
    overrides: tuple[tuple[str, object], ...] = (),
) -> Roster:
    return Roster(
        name="onset scheduler roster",
        executors=(
            Executor(
                executor_id="solo",
                part_id="solo",
                capability=capability,
                gain_db=0.0,
                pan=0.0,
                seat=Seat(azimuth_deg=0.0, distance_m=3.0),
                transpose=0,
                articulation_map=articulation_map,
                kit_pitch=None,
                articulation_auto=articulation_auto,
                overrides=overrides,
            ),
        ),
    )


def _settings(*, physical: bool = True, structural: bool = False) -> ExpressionSettings:
    return ExpressionSettings(
        mode="ensemble" if structural else "strict",
        structural=structural,
        physical=physical,
        humanize_depth=0.0,
        timing_ms=0.0,
        velocity_spread=0.0,
        seed=0,
    )


def _build(
    capability: InstrumentCapability,
    notes: list[dict[str, object]],
    *,
    articulation_map: tuple[tuple[str, str], ...] = (),
    articulation_auto: bool = True,
    physical: bool = True,
    structural: bool = False,
) -> PerformancePlan:
    return build_plan(
        _score(notes),
        _roster(
            capability,
            articulation_map=articulation_map,
            articulation_auto=articulation_auto,
        ),
        _settings(physical=physical, structural=structural),
    )


def _events(plan: PerformancePlan, event_type: str) -> list[dict[str, object]]:
    return [
        event
        for event in plan.parts[0].performance["events"]
        if event["type"] == event_type
    ]


def _audit(trace: dict[str, object]) -> dict[str, object]:
    derivation = trace["推导"]
    assert isinstance(derivation, dict)
    audit = derivation["发音补偿审计"]
    assert isinstance(audit, dict)
    return audit


def _canonical_plan_sha256(plan: PerformancePlan) -> str:
    encoded = json.dumps(
        plan.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class OnsetSchedulerTests(unittest.TestCase):
    def test_roster_mapping_uses_the_final_backend_articulation_onset(self) -> None:
        capability = _capability(
            _onset("slow", frames=120),
            _onset("sustain", frames=310),
        )
        plan = _build(
            capability,
            [
                {
                    "bar": 1,
                    "beat": 2,
                    "duration_beats": 1,
                    "pitch": "C4",
                    "articulation": "swell",
                }
            ],
            articulation_map=(("swell", "slow"),),
            articulation_auto=False,
        )

        trace = plan.parts[0].trace[0]
        audit = _audit(trace)
        self.assertEqual(trace["奏法"], "slow")
        self.assertEqual(audit["final_articulation"], "slow")
        self.assertAlmostEqual(audit["requested_delay_seconds"], 0.120)
        self.assertAlmostEqual(audit["scheduled_start_seconds"], 0.880)

    def test_automatic_articulation_uses_the_onset_of_its_final_choice(self) -> None:
        capability = _capability(
            _onset("accent", frames=40),
            _onset("sustain", frames=260),
            articulations=("accent", "sustain"),
            duration_articulation_rules=(
                DurationArticulationRule(
                    rule_id="test-neutral-short-v1",
                    source_articulation="sustain",
                    target_articulation="accent",
                    below_seconds=1.2,
                ),
            ),
        )
        plan = _build(
            capability,
            [
                {
                    "bar": 1,
                    "beat": 2,
                    "duration_beats": 0.5,
                    "pitch": "C4",
                }
            ],
            structural=True,
        )

        trace = plan.parts[0].trace[0]
        audit = _audit(trace)
        self.assertEqual(trace["奏法"], "accent")
        self.assertEqual(audit["final_articulation"], "accent")
        self.assertAlmostEqual(audit["requested_delay_seconds"], 0.040)
        self.assertNotAlmostEqual(audit["requested_delay_seconds"], 0.260)

    def test_compensation_moves_note_on_but_not_absolute_note_off(self) -> None:
        capability = _capability(_onset("sustain", frames=250))
        note = {
            "bar": 1,
            "beat": 2,
            "duration_beats": 1,
            "pitch": "C4",
        }
        compensated = _build(capability, [note], physical=True)
        uncompensated = _build(capability, [note], physical=False)

        compensated_on = _events(compensated, "note_on")[0]
        uncompensated_on = _events(uncompensated, "note_on")[0]
        compensated_off = _events(compensated, "note_off")[0]
        uncompensated_off = _events(uncompensated, "note_off")[0]
        self.assertAlmostEqual(
            uncompensated_on["time"] - compensated_on["time"],
            0.250,
        )
        self.assertEqual(compensated_off["time"], uncompensated_off["time"])
        self.assertEqual(compensated_on["note_id"], compensated_off["note_id"])

        trace = compensated.parts[0].trace[0]
        self.assertAlmostEqual(
            trace["时间"] + trace["时长"],
            compensated_off["time"],
            places=6,
        )

    def test_approved_zero_delay_still_emits_structured_audit(self) -> None:
        capability = _capability(_onset("sustain", frames=0))
        plan = _build(
            capability,
            [
                {
                    "bar": 1,
                    "beat": 2,
                    "duration_beats": 1,
                    "pitch": "C4",
                }
            ],
        )

        audit = _audit(plan.parts[0].trace[0])
        self.assertEqual(audit["status"], "applied")
        self.assertEqual(audit["context"], "isolated_attack")
        self.assertEqual(audit["requested_delay_seconds"], 0.0)
        self.assertEqual(audit["applied_delay_seconds"], 0.0)
        self.assertEqual(audit["clipped_delay_seconds"], 0.0)
        self.assertEqual(audit["logical_start_seconds"], 1.0)
        self.assertEqual(audit["scheduled_start_seconds"], 1.0)
        self.assertEqual(audit["evidence"]["sha256"], _EVIDENCE_A)

    def test_opening_clamp_records_requested_applied_and_clipped_delay(self) -> None:
        capability = _capability(_onset("sustain", frames=125))
        note = {
            "bar": 1,
            "beat": 1,
            "duration_beats": 1,
            "pitch": "C4",
        }
        compensated = _build(capability, [note])
        uncompensated = _build(capability, [note], physical=False)

        audit = _audit(compensated.parts[0].trace[0])
        self.assertEqual(audit["logical_start_seconds"], 0.0)
        self.assertEqual(audit["scheduled_start_seconds"], 0.0)
        self.assertEqual(audit["requested_delay_seconds"], 0.125)
        self.assertEqual(audit["applied_delay_seconds"], 0.0)
        self.assertEqual(audit["clipped_delay_seconds"], 0.125)
        self.assertEqual(
            _events(compensated, "note_off")[0]["time"],
            _events(uncompensated, "note_off")[0]["time"],
        )
        self.assertTrue(any("负时间" in warning for warning in compensated.warnings))
        self.assertIn(
            "onset.compensation_clipped_at_zero",
            {item.code for item in compensated.advisories},
        )
        self.assertNotIn("advisories", compensated.to_dict())
        self.assertEqual(
            _canonical_plan_sha256(compensated),
            _canonical_plan_sha256(replace(compensated, advisories=())),
        )

    def test_dominant_auto_articulation_is_review_only_and_unhashed(self) -> None:
        capability = _capability(
            _onset("accent", frames=40),
            _onset("sustain", frames=260),
            articulations=("accent", "sustain"),
            duration_articulation_rules=(
                DurationArticulationRule(
                    rule_id="test-neutral-short-v1",
                    source_articulation="sustain",
                    target_articulation="accent",
                    below_seconds=1.2,
                ),
            ),
        )
        notes = [
            {
                "bar": 1 + index // 4,
                "beat": 1 + index % 4,
                "duration_beats": 0.25,
                "pitch": "C4",
            }
            for index in range(8)
        ]

        plan = _build(capability, notes, structural=True)

        advisory = next(
            item
            for item in plan.advisories
            if item.code == "articulation.auto_dominant"
        )
        self.assertEqual(advisory.level, "warning")
        self.assertEqual(advisory.evidence["automatic_articulation_count"], 8)
        self.assertEqual(advisory.evidence["ratio"], 1.0)
        self.assertEqual(
            _canonical_plan_sha256(plan),
            _canonical_plan_sha256(replace(plan, advisories=())),
        )

    def test_negative_opening_humanize_is_clamped_before_release_and_audit(
        self,
    ) -> None:
        capability = _capability(_onset("sustain", frames=125))
        plan = build_plan(
            _score(
                [
                    {
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 0.01,
                        "pitch": "C4",
                    }
                ]
            ),
            _roster(capability),
            ExpressionSettings(
                mode="ensemble",
                structural=False,
                physical=True,
                humanize_depth=1.0,
                timing_ms=100.0,
                velocity_spread=0.0,
                seed=4,
            ),
        )

        trace = plan.parts[0].trace[0]
        audit = _audit(trace)
        note_off = _events(plan, "note_off")[0]
        self.assertEqual(audit["logical_start_seconds"], 0.0)
        self.assertEqual(audit["scheduled_start_seconds"], 0.0)
        self.assertEqual(audit["applied_delay_seconds"], 0.0)
        self.assertEqual(audit["clipped_delay_seconds"], 0.125)
        self.assertEqual(audit["release_seconds"], note_off["time"])
        self.assertEqual(note_off["time"], 0.02)
        self.assertIn("时间边界", trace["推导"])

    def test_physical_false_disables_shift_audit_and_warning(self) -> None:
        capability = _capability(_onset("sustain", frames=125))
        plan = _build(
            capability,
            [
                {
                    "bar": 1,
                    "beat": 1,
                    "duration_beats": 1,
                    "pitch": "C4",
                }
            ],
            physical=False,
        )

        trace = plan.parts[0].trace[0]
        self.assertEqual(_events(plan, "note_on")[0]["time"], 0.0)
        self.assertNotIn("发音补偿", trace["推导"])
        self.assertNotIn("发音补偿审计", trace["推导"])
        self.assertEqual(plan.warnings, ())

    def test_sample_variant_override_never_reuses_base_manifest_onset(self) -> None:
        capability = _capability(_onset("sustain", frames=125))
        note = {
            "bar": 1,
            "beat": 2,
            "duration_beats": 1,
            "pitch": "C4",
        }
        plan = build_plan(
            _score([note]),
            _roster(
                capability,
                overrides=(("sample_variant", "SEC"),),
            ),
            _settings(),
        )

        trace = plan.parts[0].trace[0]
        audit = _audit(trace)
        self.assertEqual(_events(plan, "note_on")[0]["time"], 1.0)
        self.assertEqual(
            audit["status"],
            "not_applied_runtime_configuration_mismatch",
        )
        self.assertEqual(
            audit["onset_affecting_overrides"],
            ["sample_variant"],
        )

    def test_default_conservative_overlap_preserves_connected_and_chord_behavior(
        self,
    ) -> None:
        capability = _capability(_onset("sustain", frames=100))
        self.assertEqual(capability.onset_overlap_policy, "conservative")
        self.assertEqual(
            capability.to_dict()["onset_overlap_policy"],
            "conservative",
        )
        plan = _build(
            capability,
            [
                {
                    "bar": 1,
                    "beat": 2,
                    "duration_beats": 3,
                    "pitch": "C4",
                },
                {
                    "bar": 1,
                    "beat": 2,
                    "duration_beats": 3,
                    "pitch": "E4",
                },
                {
                    "bar": 1,
                    "beat": 3,
                    "duration_beats": 1,
                    "pitch": "G4",
                },
            ],
        )

        by_pitch = {trace["音"]: trace for trace in plan.parts[0].trace}
        for pitch in ("C4", "E4"):
            with self.subTest(pitch=pitch):
                audit = _audit(by_pitch[pitch])
                self.assertEqual(audit["status"], "applied")
                self.assertEqual(audit["context"], "isolated_attack")
                self.assertEqual(
                    audit["onset_overlap_policy"],
                    "conservative",
                )
                self.assertEqual(audit["scheduled_start_seconds"], 0.9)

        connected_audit = _audit(by_pitch["G4"])
        self.assertEqual(
            connected_audit["status"],
            "not_applied_unapproved_context",
        )
        self.assertEqual(connected_audit["context"], "connected_transition")
        self.assertEqual(
            connected_audit["onset_overlap_policy"],
            "conservative",
        )
        self.assertEqual(connected_audit["requested_delay_seconds"], 0.0)
        self.assertEqual(connected_audit["applied_delay_seconds"], 0.0)
        self.assertEqual(connected_audit["clipped_delay_seconds"], 0.0)
        self.assertEqual(connected_audit["logical_start_seconds"], 2.0)
        self.assertEqual(connected_audit["scheduled_start_seconds"], 2.0)
        self.assertEqual(
            connected_audit["available_evidence"]["sha256"],
            _EVIDENCE_A,
        )

    def test_polyphonic_overlap_keeps_attacks_isolated_and_note_off_frozen(
        self,
    ) -> None:
        capability = _capability(
            _onset("sustain", frames=100),
            onset_overlap_policy="polyphonic_independent",
        )
        notes = [
            {
                "bar": 1,
                "beat": 2,
                "duration_beats": 3,
                "pitch": "C4",
            },
            {
                "bar": 1,
                "beat": 3,
                "duration_beats": 1,
                "pitch": "G4",
            },
        ]
        compensated = _build(capability, notes)
        uncompensated = _build(capability, notes, physical=False)

        by_pitch = {
            trace["音"]: trace
            for trace in compensated.parts[0].trace
        }
        later_audit = _audit(by_pitch["G4"])
        self.assertEqual(later_audit["status"], "applied")
        self.assertEqual(later_audit["context"], "isolated_attack")
        self.assertEqual(
            later_audit["onset_overlap_policy"],
            "polyphonic_independent",
        )
        self.assertEqual(later_audit["logical_start_seconds"], 2.0)
        self.assertEqual(later_audit["scheduled_start_seconds"], 1.9)
        self.assertEqual(
            _events(compensated, "note_off"),
            _events(uncompensated, "note_off"),
        )

    def test_monophonic_overlap_marks_later_onset_connected(self) -> None:
        capability = _capability(
            _onset("sustain", frames=100),
            onset_overlap_policy="monophonic_connected",
        )
        plan = _build(
            capability,
            [
                {
                    "bar": 1,
                    "beat": 2,
                    "duration_beats": 3,
                    "pitch": "C4",
                },
                {
                    "bar": 1,
                    "beat": 3,
                    "duration_beats": 1,
                    "pitch": "G4",
                },
            ],
        )

        by_pitch = {trace["音"]: trace for trace in plan.parts[0].trace}
        audit = _audit(by_pitch["G4"])
        self.assertEqual(audit["status"], "not_applied_unapproved_context")
        self.assertEqual(audit["context"], "connected_transition")
        self.assertEqual(
            audit["onset_overlap_policy"],
            "monophonic_connected",
        )
        self.assertEqual(
            next(
                event["time"]
                for event in _events(plan, "note_on")
                if event["midi_note"] == 67.0
            ),
            2.0,
        )

    def test_equal_time_mixed_articulations_remain_atomic_and_ids_stay_paired(
        self,
    ) -> None:
        raw = [
            {
                "time": 1.0,
                "kind": 0,
                "event": {"type": "articulation", "name": "artA"},
            },
            {
                "time": 1.0,
                "kind": 1,
                "event": {
                    "type": "note_on",
                    "midi_note": 60.0,
                    "velocity": 0.7,
                },
            },
            {
                "time": 2.0,
                "kind": 2,
                "event": {"type": "note_off"},
            },
            {
                "time": 1.0,
                "kind": 0,
                "event": {"type": "articulation", "name": "artB"},
            },
            {
                "time": 1.0,
                "kind": 1,
                "event": {
                    "type": "note_on",
                    "midi_note": 64.0,
                    "velocity": 0.7,
                },
            },
            {
                "time": 2.0,
                "kind": 2,
                "event": {"type": "note_off"},
            },
        ]

        events = _pair_note_ids(raw)
        self.assertEqual(
            [
                (event["type"], event.get("name"))
                for event in events[:4]
            ],
            [
                ("articulation", "artA"),
                ("note_on", None),
                ("articulation", "artB"),
                ("note_on", None),
            ],
        )
        note_ons = [event for event in events if event["type"] == "note_on"]
        note_offs = [event for event in events if event["type"] == "note_off"]
        self.assertEqual([event["note_id"] for event in note_ons], [1, 2])
        self.assertEqual([event["note_id"] for event in note_offs], [1, 2])

    def test_plan_document_and_hash_bind_the_approved_evidence_sha(self) -> None:
        note = {
            "bar": 1,
            "beat": 2,
            "duration_beats": 1,
            "pitch": "C4",
        }
        first = _build(
            _capability(
                _onset("sustain", frames=100, evidence_sha256=_EVIDENCE_A)
            ),
            [note],
        )
        second = _build(
            _capability(
                _onset("sustain", frames=100, evidence_sha256=_EVIDENCE_B)
            ),
            [note],
        )

        first_document = first.to_dict()
        second_document = second.to_dict()
        first_audit = first_document["parts"][0]["trace"][0]["推导"][
            "发音补偿审计"
        ]
        second_audit = second_document["parts"][0]["trace"][0]["推导"][
            "发音补偿审计"
        ]
        self.assertEqual(first_audit["evidence"]["sha256"], _EVIDENCE_A)
        self.assertEqual(second_audit["evidence"]["sha256"], _EVIDENCE_B)
        self.assertNotEqual(first_document, second_document)
        self.assertNotEqual(
            _canonical_plan_sha256(first),
            _canonical_plan_sha256(second),
        )


if __name__ == "__main__":
    unittest.main()
