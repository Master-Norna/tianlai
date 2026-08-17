from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tianlai.canonical_json import canonical_json_bytes
from tianlai.resource_limits import (
    PlanDocumentBudgetTracker,
    ProjectLimits,
    ResourceLimitError,
    validate_plan_document_resource_limits,
)


class PlanDocumentBudgetTests(unittest.TestCase):
    def test_environment_has_an_independent_plan_document_budget(self) -> None:
        with patch.dict(
            os.environ,
            {"TIANLAI_MAX_PLAN_MIB": "3"},
        ):
            limits = ProjectLimits.from_environment()
        self.assertEqual(limits.max_plan_json_bytes, 3 * 1024 * 1024)
        self.assertEqual(
            ProjectLimits().max_plan_json_bytes,
            32 * 1024 * 1024,
        )

    def test_invalid_plan_environment_override_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"TIANLAI_MAX_PLAN_MIB": "0"},
        ):
            with self.assertRaisesRegex(
                ResourceLimitError,
                "TIANLAI_MAX_PLAN_MIB",
            ):
                ProjectLimits.from_environment()

    def test_fragment_charge_uses_canonical_utf8_and_framing(self) -> None:
        fragment = {"value": "雪", "event": 1}
        tracker = PlanDocumentBudgetTracker(
            ProjectLimits(max_plan_json_bytes=1024),
        )

        charged = tracker.charge_fragment(fragment, framing_bytes=3)

        self.assertEqual(
            charged,
            len(canonical_json_bytes(fragment)) + 3,
        )
        self.assertEqual(tracker.charged_bytes, charged)
        self.assertEqual(tracker.fragment_count, 1)

    def test_fragment_gate_fails_before_committing_tracker_state(self) -> None:
        first = {"event": "first"}
        first_cost = len(canonical_json_bytes(first)) + 1
        tracker = PlanDocumentBudgetTracker(
            ProjectLimits(max_plan_json_bytes=first_cost),
        )
        tracker.charge_fragment(first)

        with self.assertRaises(ResourceLimitError) as raised:
            tracker.charge_fragment({"event": "would-not-be-retained"})

        self.assertEqual(raised.exception.code, "plan.document_too_large")
        self.assertGreater(raised.exception.actual, raised.exception.limit)
        self.assertEqual(tracker.charged_bytes, first_cost)
        self.assertEqual(tracker.fragment_count, 1)

    def test_nonportable_fragment_fails_without_committing_state(self) -> None:
        tracker = PlanDocumentBudgetTracker(
            ProjectLimits(max_plan_json_bytes=1024),
        )

        with self.assertRaises(ResourceLimitError) as raised:
            tracker.charge_fragment({"value": float("nan")})

        self.assertEqual(raised.exception.code, "plan.nonportable_json")
        self.assertEqual(tracker.charged_bytes, 0)
        self.assertEqual(tracker.fragment_count, 0)

    def test_final_validation_accounts_for_the_complete_document(self) -> None:
        raw_plan = {
            "schema_version": 1,
            "parts": [],
            "metadata": {"audit": "x" * 40},
        }
        exact_size = len(canonical_json_bytes(raw_plan))
        tracker = PlanDocumentBudgetTracker(
            ProjectLimits(max_plan_json_bytes=exact_size),
        )
        tracker.charge_fragment({"schema_version": 1})

        report = tracker.validate_final(raw_plan)

        self.assertEqual(report["plan_json_bytes"], exact_size)
        self.assertEqual(
            report["incrementally_charged_bytes"],
            tracker.charged_bytes,
        )
        self.assertEqual(report["charged_fragment_count"], 1)

    def test_final_validation_catches_untracked_structural_payload(self) -> None:
        raw_plan = {
            "schema_version": 1,
            "parts": [],
            "metadata": {"audit": "x" * 40},
        }
        exact_size = len(canonical_json_bytes(raw_plan))
        tracker = PlanDocumentBudgetTracker(
            ProjectLimits(max_plan_json_bytes=exact_size - 1),
        )
        tracker.charge_fragment(None, framing_bytes=0)

        with self.assertRaises(ResourceLimitError) as raised:
            tracker.validate_final(raw_plan)

        self.assertEqual(raised.exception.code, "plan.document_too_large")
        self.assertEqual(raised.exception.actual, exact_size)
        self.assertEqual(raised.exception.limit, exact_size - 1)

    def test_direct_final_validation_rejects_nonportable_json(self) -> None:
        with self.assertRaises(ResourceLimitError) as raised:
            validate_plan_document_resource_limits(
                {"duration_seconds": float("inf")},
                ProjectLimits(max_plan_json_bytes=1024),
            )

        self.assertEqual(raised.exception.code, "plan.nonportable_json")

    def test_invalid_framing_does_not_encode_or_charge(self) -> None:
        tracker = PlanDocumentBudgetTracker(
            ProjectLimits(max_plan_json_bytes=1024),
        )

        with self.assertRaisesRegex(ValueError, "framing_bytes"):
            tracker.charge_fragment({}, framing_bytes=True)

        self.assertEqual(tracker.charged_bytes, 0)
        self.assertEqual(tracker.fragment_count, 0)


if __name__ == "__main__":
    unittest.main()
