from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest

from tianlai.orchestration_topology import analyze_orchestration_topology


def _part(
    executor_id: str,
    *,
    instrument: str = "管弦乐/弦乐组/小提琴",
    variant: str | None = None,
    pitches: tuple[str, ...],
    delay_ms: float = 0.0,
) -> SimpleNamespace:
    overrides = (
        {"sample_variant": variant} if variant is not None else {}
    )
    executor = SimpleNamespace(
        executor_id=executor_id,
        part_id=executor_id,
        capability=SimpleNamespace(relative_path=instrument),
        override_map=overrides,
    )
    trace = tuple(
        {
            "小节": index // 4 + 1,
            "拍": float(index % 4 + 1),
            "音": pitch,
            "时间": index * 0.5 + delay_ms / 1000.0,
            "时长": 0.4,
        }
        for index, pitch in enumerate(pitches)
    )
    return SimpleNamespace(executor=executor, trace=trace)


class OrchestrationTopologyTests(unittest.TestCase):
    def test_same_source_near_unison_is_advisory_candidate(self) -> None:
        pitches = ("G4", "A4", "B4", "D5") * 3
        plan = SimpleNamespace(
            parts=(
                _part("第一小提琴", pitches=pitches),
                _part("第二小提琴", pitches=pitches, delay_ms=5.0),
            )
        )
        before = copy.deepcopy(plan)

        report = analyze_orchestration_topology(plan)

        self.assertEqual(
            report["summary"][
                "same_source_unison_phase_candidate_count"
            ],
            1,
        )
        self.assertFalse(report["audio_modified"])
        self.assertEqual(
            report["pairs"][0]["status"],
            "same_source_unison_phase_candidate",
        )
        self.assertEqual(
            report["pairs"][0]["median_scheduled_start_delta_ms"],
            5.0,
        )
        self.assertEqual(
            report["warnings"][0]["code"],
            "same_source_unison_phase_candidate",
        )
        self.assertEqual(plan.parts, before.parts)

    def test_exact_unison_is_level_stack_not_phase_candidate(self) -> None:
        pitches = ("G4", "A4", "B4", "D5") * 3
        plan = SimpleNamespace(
            parts=(
                _part("第一轨", pitches=pitches),
                _part("复制轨", pitches=pitches),
            )
        )

        report = analyze_orchestration_topology(plan)

        self.assertEqual(
            report["summary"][
                "same_source_unison_phase_candidate_count"
            ],
            0,
        )
        self.assertEqual(
            report["summary"][
                "same_source_exact_unison_level_stack_candidate_count"
            ],
            1,
        )
        self.assertEqual(
            report["pairs"][0]["status"],
            "same_source_exact_unison_level_stack_candidate",
        )
        self.assertEqual(
            report["warnings"][0]["code"],
            "same_source_exact_unison_level_stack_candidate",
        )

    def test_different_source_same_notes_is_context_only(self) -> None:
        pitches = ("G4", "A4", "B4", "D5") * 3
        plan = SimpleNamespace(
            parts=(
                _part("小提琴", pitches=pitches),
                _part(
                    "长笛",
                    instrument="管弦乐/木管组/长笛",
                    pitches=pitches,
                    delay_ms=4.0,
                ),
            )
        )

        report = analyze_orchestration_topology(plan)

        self.assertEqual(
            report["summary"][
                "same_source_unison_phase_candidate_count"
            ],
            0,
        )
        self.assertEqual(report["pairs"][0]["status"], "context_only")
        self.assertFalse(report["pairs"][0]["same_source"])

    def test_explicit_variant_makes_sources_distinct(self) -> None:
        pitches = ("G4", "A4", "B4", "D5") * 3
        plan = SimpleNamespace(
            parts=(
                _part("一提", variant="SEC1", pitches=pitches),
                _part(
                    "二提",
                    variant="SEC2",
                    pitches=pitches,
                    delay_ms=3.0,
                ),
            )
        )

        report = analyze_orchestration_topology(plan)

        self.assertFalse(report["pairs"][0]["same_source"])
        self.assertEqual(report["warnings"], [])

    def test_octave_doubling_is_not_a_phase_candidate(self) -> None:
        first = ("G4", "A4", "B4", "D5") * 3
        second = ("G3", "A3", "B3", "D4") * 3
        plan = SimpleNamespace(
            parts=(
                _part("高声部", pitches=first),
                _part("低声部", pitches=second, delay_ms=2.0),
            )
        )

        report = analyze_orchestration_topology(plan)

        self.assertEqual(
            report["summary"]["octave_same_score_position_count"],
            12,
        )
        self.assertEqual(
            report["summary"][
                "same_source_unison_phase_candidate_count"
            ],
            0,
        )
        self.assertEqual(report["pairs"][0]["status"], "context_only")

    def test_low_coverage_does_not_raise_candidate(self) -> None:
        first = ("G4",) * 4 + ("A4",) * 8
        second = ("G4",) * 4 + ("C5",) * 8
        plan = SimpleNamespace(
            parts=(
                _part("甲", pitches=first),
                _part("乙", pitches=second, delay_ms=5.0),
            )
        )

        report = analyze_orchestration_topology(plan)

        self.assertEqual(
            report["pairs"][0][
                "same_pitch_same_score_position_count"
            ],
            4,
        )
        self.assertEqual(report["warnings"], [])

    def test_result_is_deterministic_across_part_order(self) -> None:
        pitches = ("G4", "A4", "B4", "D5") * 3
        first = _part("甲", pitches=pitches)
        second = _part("乙", pitches=pitches, delay_ms=7.0)

        forward = analyze_orchestration_topology(
            SimpleNamespace(parts=(first, second))
        )
        reverse = analyze_orchestration_topology(
            SimpleNamespace(parts=(second, first))
        )

        self.assertEqual(forward, reverse)

    def test_phase_candidate_is_retained_before_context_when_pairs_truncate(
        self,
    ) -> None:
        pitches = ("G4", "A4", "B4", "D5") * 3
        parts = [
            _part(
                f"context-{index:03d}",
                instrument=f"测试/不同源/{index:03d}",
                pitches=pitches,
            )
            for index in range(18)
        ]
        parts.extend(
            (
                _part("zz-phase-a", pitches=pitches),
                _part("zz-phase-b", pitches=pitches, delay_ms=5.0),
            )
        )

        report = analyze_orchestration_topology(
            SimpleNamespace(parts=tuple(parts))
        )

        self.assertTrue(report["summary"]["pairs_truncated"])
        self.assertEqual(
            report["pairs"][0]["status"],
            "same_source_unison_phase_candidate",
        )
        self.assertEqual(
            report["warnings"][0]["code"],
            "same_source_unison_phase_candidate",
        )


if __name__ == "__main__":
    unittest.main()
