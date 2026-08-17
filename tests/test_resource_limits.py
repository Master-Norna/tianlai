from __future__ import annotations

from dataclasses import dataclass
import os
import unittest
from unittest.mock import patch

from tianlai.events import parse_performance_document
from tianlai.resource_limits import (
    ProjectLimits,
    ResourceLimitError,
    _analysis_transaction_scratch_requirement,
    estimate_render_resources,
    validate_performance_document_resource_limits,
    validate_plan_resource_limits,
    validate_score_resource_limits,
    validate_single_render_resource_limits,
)
from tianlai.score import parse_score_document


def _score(note_count: int = 1) -> dict:
    return {
        "schema_version": 1,
        "title": "limits",
        "sample_rate": 48_000,
        "tail_seconds": 1.0,
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
                "id": "part",
                "name": "part",
                "notes": [
                    {
                        "event_id": f"event-{index}",
                        "bar": 1,
                        "beat": 1.0,
                        "duration_beats": 1.0,
                        "pitch": "C4",
                    }
                    for index in range(note_count)
                ],
            }
        ],
    }


@dataclass
class _Plan:
    duration_seconds: float
    sample_rate: int
    parts: tuple[object, ...]


class ResourceLimitTests(unittest.TestCase):
    def test_analysis_transaction_space_gate_covers_conservative_overlap(
        self,
    ) -> None:
        frames = 123
        reserve = 512 * 1024 * 1024
        self.assertEqual(
            _analysis_transaction_scratch_requirement(
                frames,
                write_stems=False,
            ),
            reserve + frames * (8 + 8),
        )
        self.assertEqual(
            _analysis_transaction_scratch_requirement(
                frames,
                write_stems=True,
            ),
            reserve + frames * (8 + 8 + 6),
        )

    def test_score_note_budget_is_enforced(self) -> None:
        document = _score(2)
        parsed = parse_score_document(document)
        with self.assertRaises(ResourceLimitError) as raised:
            validate_score_resource_limits(
                document,
                parsed,
                ProjectLimits(max_notes=1),
            )
        self.assertEqual(raised.exception.code, "score.too_many_notes")

    def test_performance_event_budget_is_enforced_before_parsing(self) -> None:
        document = {
            "events": [
                {
                    "time": 0.0,
                    "type": "control",
                    "name": f"control-{index}",
                    "value": 0.5,
                }
                for index in range(5)
            ]
        }
        with self.assertRaises(ResourceLimitError) as raised:
            validate_performance_document_resource_limits(
                document,
                ProjectLimits(max_notes=1),
            )
        self.assertEqual(
            raised.exception.code,
            "performance.too_many_events",
        )
        self.assertEqual(raised.exception.actual, 5)
        self.assertEqual(raised.exception.limit, 4)

    def test_performance_document_size_budget_is_enforced(self) -> None:
        with self.assertRaises(ResourceLimitError) as raised:
            validate_performance_document_resource_limits(
                {"events": []},
                ProjectLimits(max_score_json_bytes=1),
            )
        self.assertEqual(
            raised.exception.code,
            "performance.document_too_large",
        )

    def test_single_render_duration_budget_is_enforced(self) -> None:
        document = {
            "sample_rate": 8_000,
            "channels": 2,
            "tail_seconds": 0.0,
            "duration_seconds": 2.0,
            "events": [],
        }
        performance = parse_performance_document(document)
        with self.assertRaises(ResourceLimitError) as raised:
            validate_single_render_resource_limits(
                performance,
                ProjectLimits(max_plan_seconds=1),
            )
        self.assertEqual(raised.exception.code, "render.duration_too_long")

    def test_single_render_output_budget_is_enforced(self) -> None:
        document = {
            "sample_rate": 8_000,
            "channels": 2,
            "tail_seconds": 0.0,
            "duration_seconds": 0.001,
            "events": [],
        }
        performance = parse_performance_document(document)
        with self.assertRaises(ResourceLimitError) as raised:
            validate_single_render_resource_limits(
                performance,
                ProjectLimits(max_primary_output_bytes=47),
            )
        self.assertEqual(
            raised.exception.code,
            "render.output_budget_exceeded",
        )
        self.assertEqual(raised.exception.actual, 48)

    def test_render_estimate_accounts_for_stems_and_hall(self) -> None:
        plan = _Plan(10.0, 100, (object(), object()))
        estimate = estimate_render_resources(
            plan,
            write_stems=True,
            hall_tail_seconds=2.0,
        )
        self.assertEqual(estimate["frame_count"], 1200)
        self.assertEqual(
            estimate["estimated_audio_memory_bytes"],
            1200 * 192,
        )
        self.assertEqual(
            estimate["estimated_primary_output_bytes"],
            1200 * 6 * 3,
        )

    def test_memory_budget_is_enforced_before_render(self) -> None:
        plan = _Plan(10.0, 100, (object(),))
        with self.assertRaises(ResourceLimitError) as raised:
            validate_plan_resource_limits(
                plan,
                write_stems=False,
                limits=ProjectLimits(max_audio_memory_bytes=1),
            )
        self.assertEqual(
            raised.exception.code,
            "render.memory_budget_exceeded",
        )

    def test_environment_override_must_be_positive_integer(self) -> None:
        with patch.dict(
            os.environ,
            {"TIANLAI_MAX_NOTES": "not-a-number"},
        ):
            with self.assertRaisesRegex(
                ResourceLimitError,
                "TIANLAI_MAX_NOTES",
            ):
                ProjectLimits.from_environment()


if __name__ == "__main__":
    unittest.main()
