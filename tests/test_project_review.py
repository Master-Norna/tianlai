from __future__ import annotations

import copy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tianlai.canonical_json import canonical_json_sha256
from tianlai.project_review import (
    build_project_review,
    build_project_review_safely,
)


def _capability(
    relative_path: str,
    *,
    collaboration_review_status: str = "passed",
) -> SimpleNamespace:
    return SimpleNamespace(
        relative_path=relative_path,
        collaboration_review_status=collaboration_review_status,
    )


def _role(prominence: str) -> SimpleNamespace:
    document = {"function": "support", "prominence": prominence}
    return SimpleNamespace(
        prominence=prominence,
        to_dict=lambda: copy.deepcopy(document),
    )


def _executor(
    executor_id: str,
    part_id: str,
    capability: SimpleNamespace,
    *,
    role: SimpleNamespace | None = None,
    gain_db: float = 0.0,
) -> SimpleNamespace:
    return SimpleNamespace(
        executor_id=executor_id,
        part_id=part_id,
        capability=capability,
        override_map={},
        role=role,
        gain_db=gain_db,
    )


def _trace(
    index: int,
    pitch: str,
    *,
    time_seconds: float | None = None,
    contract: dict | None = None,
) -> dict:
    item = {
        "小节": index + 1,
        "拍": 1.0,
        "音": pitch,
        "时间": float(index) if time_seconds is None else time_seconds,
        "推导": {},
    }
    if contract is not None:
        item["推导"]["音域合同"] = copy.deepcopy(contract)
    return item


class _Plan:
    def __init__(
        self,
        parts: tuple[SimpleNamespace, ...],
        *,
        advisories: tuple[SimpleNamespace, ...] = (),
        warnings: tuple[str, ...] = (),
        range_mode: str = "compatibility",
    ) -> None:
        self.parts = parts
        self.advisories = advisories
        self.warnings = warnings
        self.expression = SimpleNamespace(range_mode=range_mode)

    def to_dict(self) -> dict:
        # Advisories deliberately stay out of the executable plan document.
        return {
            "expression": {"range_mode": self.expression.range_mode},
            "parts": [
                {
                    "executor_id": str(part.executor.executor_id),
                    "part_id": str(part.executor.part_id),
                    "instrument": str(
                        part.executor.capability.relative_path
                    ),
                    "trace": copy.deepcopy(list(part.trace)),
                }
                for part in self.parts
            ],
            "warnings": list(self.warnings),
        }


def _roster(
    executors: tuple[SimpleNamespace, ...],
    *,
    collaboration_mode: str = "manual",
) -> SimpleNamespace:
    return SimpleNamespace(
        executors=executors,
        collaboration=SimpleNamespace(mode=collaboration_mode),
    )


def _empty_topology() -> dict:
    return {
        "format": "tianlai.orchestration_topology",
        "version": 1,
        "summary": {"reported_warning_count": 0},
        "warnings": [],
        "notice": "read-only topology fixture",
    }


class ProjectReviewTests(unittest.TestCase):
    def _review_without_topology(
        self,
        plan: _Plan,
        roster: SimpleNamespace,
        **kwargs,
    ) -> dict:
        with patch(
            "tianlai.project_review.analyze_orchestration_topology",
            return_value=_empty_topology(),
        ):
            return build_project_review(plan, roster, **kwargs)

    def test_range_findings_are_grouped_non_blocking_and_leave_plan_unchanged(
        self,
    ) -> None:
        capability = _capability("实验乐器/边缘音色")
        executor = _executor("edge", "lead", capability)
        outside_hard = {
            "status": "outside_hard_playable_range",
            "legacy_covered": True,
            "profile_id": "edge",
            "coverage": {
                "hard_playable": False,
                "idiomatic": False,
                "extended": True,
                "current_high_quality": False,
            },
        }
        pending = {
            "status": "quality_pending",
            "legacy_covered": True,
            "profile_id": "pending",
            "coverage": {
                "hard_playable": True,
                "idiomatic": True,
                "extended": False,
                "current_high_quality": True,
            },
        }
        ordinary_candidate = {
            "status": "contract_candidate_unverified",
            "legacy_covered": True,
            "profile_id": "core",
            "coverage": {
                "hard_playable": True,
                "idiomatic": True,
                "extended": False,
                "current_high_quality": True,
            },
        }
        part = SimpleNamespace(
            executor=executor,
            trace=(
                _trace(0, "C4", contract=outside_hard),
                _trace(1, "D4", contract=outside_hard),
                _trace(2, "E4", contract=pending),
                _trace(3, "F4", contract=ordinary_candidate),
            ),
        )
        plan = _Plan((part,))
        roster = _roster((executor,))
        before = plan.to_dict()
        before_hash = canonical_json_sha256(before)

        report = self._review_without_topology(
            plan,
            roster,
            binding={
                "plan_input_sha256": "a" * 64,
                "performance_plan_sha256": before_hash,
            },
        )

        by_code = {item["code"]: item for item in report["items"]}
        self.assertEqual(
            set(by_code),
            {
                "range.outside_declared_hard_profile",
                "range.quality_pending",
            },
        )
        self.assertEqual(
            by_code["range.outside_declared_hard_profile"]["evidence"][
                "affected_note_count"
            ],
            2,
        )
        self.assertNotIn("range.contract_candidate_unverified", by_code)
        self.assertTrue(report["review_recommended"])
        self.assertTrue(report["continuation_allowed"])
        self.assertEqual(report["blocking_count"], 0)
        self.assertTrue(all(not item["blocking"] for item in report["items"]))
        self.assertEqual(report["binding"]["performance_plan_sha256"], before_hash)
        self.assertEqual(plan.to_dict(), before)
        self.assertEqual(canonical_json_sha256(plan.to_dict()), before_hash)

    def test_performance_and_collaboration_findings_never_change_execution(
        self,
    ) -> None:
        sitar_capability = _capability(
            "世界乐器/西塔琴",
            collaboration_review_status="untested",
        )
        cello_capability = _capability("管弦乐/弦乐组/大提琴")
        sitar = _executor("sitar", "sitar-part", sitar_capability, gain_db=-1.0)
        cello = _executor(
            "cello",
            "cello-part",
            cello_capability,
            role=_role("background"),
        )
        automatic_message = "automatic articulation review"
        onset_message = "onset compensation clipped"
        advisories = (
            SimpleNamespace(
                code="articulation.auto_dominant",
                level="warning",
                basis="measurement",
                confidence="high",
                scope={"executor_id": "sitar", "part_id": "sitar-part"},
                message=automatic_message,
                evidence={"ratio": 0.9, "note_count": 10},
                suggestions=("listen",),
            ),
            SimpleNamespace(
                code="onset.compensation_clipped_at_zero",
                level="warning",
                basis="measurement",
                confidence="high",
                scope={
                    "executor_id": "cello",
                    "part_id": "cello-part",
                    "bar": 1,
                    "beat": 1.0,
                },
                message=onset_message,
                evidence={"clipped_delay_seconds": 0.02},
                suggestions=("leave an upbeat",),
            ),
            SimpleNamespace(
                code="onset.compensation_clipped_at_zero",
                level="warning",
                basis="measurement",
                confidence="high",
                scope={
                    "executor_id": "cello",
                    "part_id": "cello-part",
                    "bar": 1,
                    "beat": 2.0,
                },
                message=onset_message,
                evidence={"clipped_delay_seconds": 0.03},
                suggestions=("leave an upbeat",),
            ),
        )
        parts = (
            SimpleNamespace(executor=sitar, trace=()),
            SimpleNamespace(executor=cello, trace=()),
        )
        plan = _Plan(
            parts,
            advisories=advisories,
            warnings=(automatic_message, onset_message),
        )
        roster = _roster((sitar, cello))
        before = plan.to_dict()

        report = self._review_without_topology(plan, roster)

        by_code = {item["code"]: item for item in report["items"]}
        self.assertTrue(
            {
                "articulation.auto_dominant",
                "onset.compensation_clipped_at_zero",
                "coverage.collaboration_unrecorded",
                "balance.sitar_low_level_context",
                "balance.cello_background_masking_candidate",
            }.issubset(by_code)
        )
        self.assertNotIn("performance.unclassified_advisory", by_code)
        onset = by_code["onset.compensation_clipped_at_zero"]
        self.assertEqual(onset["evidence"]["affected_note_count"], 2)
        self.assertAlmostEqual(
            onset["evidence"]["total_clipped_delay_seconds"],
            0.05,
        )
        self.assertTrue(report["continuation_allowed"])
        self.assertEqual(report["blocking_count"], 0)
        self.assertTrue(all(item["gate"] == "none" for item in report["items"]))
        self.assertEqual(plan.to_dict(), before)

    def test_exact_unison_is_advisory_but_octaves_and_dissonance_are_allowed(
        self,
    ) -> None:
        capability = _capability("实验乐器/同一音源")

        def project(
            first_pitch: str,
            second_pitch: str,
            *,
            second_delay: float = 0.0,
        ) -> tuple[_Plan, SimpleNamespace]:
            first_executor = _executor("first", "first-part", capability)
            second_executor = _executor("second", "second-part", capability)
            first = SimpleNamespace(
                executor=first_executor,
                trace=tuple(_trace(index, first_pitch) for index in range(8)),
            )
            second = SimpleNamespace(
                executor=second_executor,
                trace=tuple(
                    _trace(
                        index,
                        second_pitch,
                        time_seconds=float(index) + second_delay,
                    )
                    for index in range(8)
                ),
            )
            return _Plan((first, second)), _roster(
                (first_executor, second_executor)
            )

        unison_plan, unison_roster = project("C4", "C4")
        unison_before = unison_plan.to_dict()
        unison = build_project_review(unison_plan, unison_roster)
        unison_codes = {item["code"] for item in unison["items"]}
        self.assertIn(
            "orchestration.same_source_exact_unison_level_stack_candidate",
            unison_codes,
        )
        self.assertTrue(unison["continuation_allowed"])
        self.assertEqual(unison["blocking_count"], 0)
        self.assertEqual(unison_plan.to_dict(), unison_before)

        for label, first_pitch, second_pitch in (
            ("octave", "C4", "C5"),
            ("tritone", "C4", "F#4"),
        ):
            with self.subTest(label=label):
                plan, roster = project(first_pitch, second_pitch)
                before = plan.to_dict()
                report = build_project_review(plan, roster)
                codes = {item["code"] for item in report["items"]}

                self.assertFalse(
                    any(code.startswith("orchestration.") for code in codes),
                    report,
                )
                self.assertFalse(report["review_recommended"])
                self.assertTrue(report["continuation_allowed"])
                self.assertEqual(report["blocking_count"], 0)
                self.assertEqual(plan.to_dict(), before)
                if label == "octave":
                    summary = report["diagnostics"][
                        "orchestration_topology"
                    ]["summary"]
                    self.assertEqual(summary["octave_same_score_position_count"], 8)
                    self.assertEqual(summary["reported_warning_count"], 0)

    def test_unexpected_review_failure_never_becomes_a_render_gate(self) -> None:
        broken_plan = SimpleNamespace(
            parts=(SimpleNamespace(trace=()),),
            advisories=(),
            warnings=(),
            expression=SimpleNamespace(range_mode="compatibility"),
        )

        report = build_project_review_safely(
            broken_plan,
            _roster(()),
            binding={"performance_plan_sha256": "a" * 64},
        )

        self.assertEqual(report["status"], "informational")
        self.assertTrue(report["continuation_allowed"])
        self.assertEqual(report["blocking_count"], 0)
        self.assertEqual(
            report["items"][0]["code"],
            "diagnostics.project_review_unavailable",
        )
        self.assertEqual(report["diagnostics"], {"status": "unavailable"})

    def test_naturalness_diagnostic_failure_is_local_and_nonblocking(self) -> None:
        capability = _capability("实验乐器/自然性预审")
        executor = _executor("naturalness", "lead", capability)
        plan = _Plan((SimpleNamespace(executor=executor, trace=()),))
        roster = _roster((executor,))

        with patch(
            "tianlai.project_review.analyze_orchestration_topology",
            return_value=_empty_topology(),
        ), patch(
            "tianlai.project_review.analyze_performance_naturalness",
            side_effect=RuntimeError("fixture failure"),
        ):
            report = build_project_review(plan, roster)

        by_code = {item["code"]: item for item in report["items"]}
        assert "diagnostics.performance_naturalness_unavailable" in by_code
        assert report["diagnostics"]["performance_naturalness"]["status"] == (
            "unavailable"
        )
        naturalness = report["diagnostics"]["performance_naturalness"]
        assert naturalness["evidence_coverage"] == "unavailable"
        assert naturalness["candidates"] == []
        assert len(naturalness["report_sha256"]) == 64
        assert report["diagnostics"]["orchestration_topology"]["status"] == (
            "ready"
        )
        assert report["continuation_allowed"] is True
        assert report["blocking_count"] == 0

    def test_naturalness_opt_out_preserves_legacy_review_surface(self) -> None:
        capability = _capability("实验乐器/旧复核")
        executor = _executor("legacy", "lead", capability)
        plan = _Plan((SimpleNamespace(executor=executor, trace=()),))
        roster = _roster((executor,))

        with patch(
            "tianlai.project_review.analyze_orchestration_topology",
            return_value=_empty_topology(),
        ), patch(
            "tianlai.project_review.analyze_performance_naturalness"
        ) as naturalness:
            report = build_project_review(
                plan,
                roster,
                include_performance_naturalness=False,
            )

        naturalness.assert_not_called()
        assert "performance_naturalness" not in report["diagnostics"]
        assert all(
            item["stage"] != "performance_naturalness"
            for item in report["items"]
        )


if __name__ == "__main__":
    unittest.main()
