from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from tianlai.self_check import (
    build_issue,
    build_review_item,
    build_review_report,
    paginate_issues,
)


class IssueContractTests(unittest.TestCase):
    def test_error_is_a_non_overridable_blocker(self) -> None:
        issue = build_issue(
            severity="error",
            code="license.quarantined",
            stage="availability_policy",
            message="licence evidence is quarantined",
            instrument_id="世界乐器/测试乐器",
            **{
                # Details supplied by a caller must never be able to weaken
                # the fields owned by the self-check contract.
                "blocking": False,
                "decision": "inform",
                "override": {"mode": "allowed"},
                "automatic_change": True,
            },
        )

        self.assertEqual(issue["severity"], "error")
        self.assertTrue(issue["blocking"])
        self.assertEqual(issue["decision"], "block")
        self.assertEqual(issue["override"], {"mode": "forbidden"})
        self.assertFalse(issue["automatic_change"])
        self.assertEqual(issue["scope"]["instrument_id"], "世界乐器/测试乐器")

    def test_warning_and_info_are_non_blocking_even_with_hostile_details(self) -> None:
        for severity, decision in (("warning", "review"), ("info", "inform")):
            with self.subTest(severity=severity):
                issue = build_issue(
                    severity=severity,
                    code=f"creative.{severity}",
                    stage="creative_context",
                    message="an unusual but renderable choice",
                    **{
                        "blocking": True,
                        "decision": "block",
                        "override": {"mode": "forbidden"},
                        "automatic_change": True,
                    },
                )

                self.assertFalse(issue["blocking"])
                self.assertEqual(issue["decision"], decision)
                self.assertEqual(issue["gate"], "none")
                self.assertEqual(issue["override"], {"mode": "not_needed"})
                self.assertFalse(issue["automatic_change"])

    def test_stable_id_is_not_changed_by_message_wording(self) -> None:
        common = {
            "severity": "warning",
            "code": "range.quality_pending",
            "stage": "range_review",
            "scope": {"executor_id": "violin", "bar": 3, "beat": 1.0},
            "evidence": {"status": "quality_pending"},
        }
        first = build_issue(message="first wording", **common)
        second = build_issue(message="reworded for a UI", **common)

        self.assertEqual(first["id"], second["id"])

    def test_pagination_is_blocker_first_and_counts_the_unpaged_set(self) -> None:
        issues = [
            build_issue(
                severity="warning",
                code="creative.review",
                stage="creative_context",
                message="review",
            ),
            build_issue(
                severity="error",
                code="score.first",
                stage="score_document",
                message="first blocker",
            ),
            build_issue(
                severity="info",
                code="creative.info",
                stage="creative_context",
                message="information",
            ),
            build_issue(
                severity="error",
                code="score.second",
                stage="score_document",
                message="second blocker",
            ),
        ]

        page, counts, truncated = paginate_issues(issues, 2)

        self.assertEqual(
            [item["code"] for item in page],
            ["score.first", "score.second"],
        )
        self.assertEqual(counts, {"error": 2, "info": 1, "warning": 1})
        self.assertTrue(truncated)


class ReviewContractTests(unittest.TestCase):
    def test_report_deduplicates_stable_findings_and_preserves_hash_binding(
        self,
    ) -> None:
        common = {
            "level": "warning",
            "code": "range.outside_current_hq_candidate",
            "stage": "range_review",
            "basis": "instrument_contract",
            "confidence": "high",
            "scope": {"executor_id": "flute", "part_id": "lead"},
            "evidence": {"affected_note_count": 2},
        }
        first = build_review_item(message="first wording", **common)
        duplicate = build_review_item(message="later wording", **common)
        information = build_review_item(
            level="info",
            code="coverage.collaboration_unrecorded",
            stage="creative_context",
            message="coverage information",
            basis="coverage",
            confidence="high",
            evidence={"instrument_count": 1},
        )
        binding = {
            "plan_input_sha256": "a" * 64,
            "performance_plan_sha256": "b" * 64,
            "unused": None,
        }

        report = build_review_report(
            [information, first, duplicate],
            binding=binding,
            max_items=1,
        )

        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(report["item_counts"], {"info": 1, "warning": 1})
        self.assertEqual(report["items"], [first])
        self.assertTrue(report["items_truncated"])
        self.assertTrue(report["review_recommended"])
        self.assertTrue(report["continuation_allowed"])
        self.assertEqual(report["blocking_count"], 0)
        self.assertTrue(all(not item["blocking"] for item in report["items"]))
        self.assertEqual(
            report["binding"],
            {
                "plan_input_sha256": "a" * 64,
                "performance_plan_sha256": "b" * 64,
            },
        )
        self.assertFalse(report["policy"]["review_findings_block_render"])
        self.assertFalse(report["policy"]["automatic_score_changes"])
        self.assertFalse(report["policy"]["automatic_audio_changes"])
        self.assertFalse(report["policy"]["generic_force_override"])

    def test_finding_id_changes_with_scope_or_evidence_not_project_binding(self) -> None:
        base = build_review_item(
            level="warning",
            code="performance.review",
            stage="performance_plan",
            message="review",
            basis="measurement",
            confidence="high",
            scope={"executor_id": "one"},
            evidence={"ratio": 0.9},
        )
        changed_scope = build_review_item(
            level="warning",
            code="performance.review",
            stage="performance_plan",
            message="review",
            basis="measurement",
            confidence="high",
            scope={"executor_id": "two"},
            evidence={"ratio": 0.9},
        )
        changed_evidence = build_review_item(
            level="warning",
            code="performance.review",
            stage="performance_plan",
            message="review",
            basis="measurement",
            confidence="high",
            scope={"executor_id": "one"},
            evidence={"ratio": 0.8},
        )

        first_report = build_review_report(
            [base],
            binding={"plan_input_sha256": "1" * 64},
        )
        second_report = build_review_report(
            [base],
            binding={"plan_input_sha256": "2" * 64},
        )

        self.assertNotEqual(base["id"], changed_scope["id"])
        self.assertNotEqual(base["id"], changed_evidence["id"])
        self.assertEqual(
            first_report["items"][0]["id"],
            second_report["items"][0]["id"],
        )
        self.assertNotEqual(first_report["binding"], second_report["binding"])

    def test_report_rejects_a_forged_blocking_review_item(self) -> None:
        item = build_review_item(
            level="warning",
            code="creative.review",
            stage="creative_context",
            message="review",
            basis="evidence",
            confidence="medium",
        )
        item["blocking"] = True
        item["decision"] = "block"

        with self.assertRaisesRegex(ValueError, "non-blocking"):
            build_review_report([item])

    def test_report_has_a_valid_public_json_schema(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (root / "schemas" / "project-review.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        report = build_review_report(
            [
                build_review_item(
                    level="info",
                    code="coverage.collaboration_unrecorded",
                    stage="creative_context",
                    message="coverage information",
                    basis="coverage",
                    confidence="high",
                )
            ],
            binding={"performance_plan_sha256": "a" * 64},
        )

        Draft202012Validator(schema).validate(report)


if __name__ == "__main__":
    unittest.main()
