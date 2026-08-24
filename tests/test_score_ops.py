from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from tianlai.cli import main as cli_main
from tianlai.score import parse_score_document
from tianlai.score_ops import (
    SCORE_COMPARE_RESULT_KIND,
    SCORE_PATCH_KIND,
    SCORE_PATCH_RESULT_KIND,
    SCORE_SLICE_QUERY_KIND,
    SCORE_SLICE_RESULT_KIND,
    ScoreOpsError,
    apply_score_patch,
    canonical_score_sha256,
    compare_scores,
    slice_score,
)
from tianlai.score_time import validate_score_time_coordinates


def _score() -> dict:
    return {
        "schema_version": 1,
        "title": "precise editing",
        "sample_rate": 48_000,
        "tail_seconds": 1,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 96,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "melody",
                "name": "Melody",
                "default_dynamic": "mf",
                "notes": [
                    {
                        "event_id": "melody-1",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                        "dynamic": "mf",
                    },
                    {
                        "event_id": "melody-2",
                        "bar": 1,
                        "beat": 3,
                        "duration_beats": 1,
                        "pitch": "E4",
                        "dynamic": "mf",
                    },
                    {
                        "event_id": "melody-3",
                        "bar": 2,
                        "beat": 1,
                        "duration_beats": 2,
                        "pitch": "G4",
                        "dynamic": "f",
                    },
                ],
            },
            {
                "id": "bass",
                "name": "Bass",
                "notes": [
                    {
                        "event_id": "bass-1",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 4,
                        "pitch": "C3",
                        "dynamic": "mp",
                    }
                ],
            },
        ],
    }


def _patch(
    score: dict,
    operations: list[dict],
    *,
    max_diff_entries: int | None = None,
) -> dict:
    result = {
        "kind": SCORE_PATCH_KIND,
        "schema_version": 1,
        "base_score_sha256": canonical_score_sha256(score),
        "operations": operations,
    }
    if max_diff_entries is not None:
        result["max_diff_entries"] = max_diff_entries
    return result


class CanonicalScoreHashTests(unittest.TestCase):
    def test_hash_ignores_object_key_insertion_order(self) -> None:
        score = _score()
        reordered = dict(reversed(list(copy.deepcopy(score).items())))

        self.assertEqual(
            canonical_score_sha256(reordered),
            canonical_score_sha256(score),
        )
        self.assertEqual(len(canonical_score_sha256(score)), 64)

    def test_hash_rejects_duplicate_ids_and_non_finite_values(self) -> None:
        duplicate = _score()
        duplicate["parts"][1]["notes"][0]["event_id"] = "melody-1"
        with self.assertRaisesRegex(ScoreOpsError, "duplicate event_id"):
            canonical_score_sha256(duplicate)

        non_finite = _score()
        non_finite["tail_seconds"] = math.inf
        with self.assertRaises(ScoreOpsError) as caught:
            canonical_score_sha256(non_finite)
        self.assertEqual(caught.exception.code, "non_canonical_json")
        self.assertFalse(caught.exception.to_dict()["ok"])


class ScoreSliceTests(unittest.TestCase):
    def test_intersected_slice_returns_a_complete_valid_fragment(self) -> None:
        score = _score()
        result = slice_score(
            score,
            {
                "kind": SCORE_SLICE_QUERY_KIND,
                "schema_version": 1,
                "part_ids": ["melody"],
                "event_ids": ["melody-1", "melody-3"],
                "bar_range": {"start": 1, "end": 1},
                "max_notes": 8,
            },
        )

        self.assertEqual(result["kind"], SCORE_SLICE_RESULT_KIND)
        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "fragment")
        self.assertFalse(result["truncated"])
        self.assertEqual(result["matched_note_count"], 1)
        fragment = result["fragment"]
        self.assertEqual(
            [note["event_id"] for note in fragment["parts"][0]["notes"]],
            ["melody-1"],
        )
        parsed = parse_score_document(fragment)
        validate_score_time_coordinates(parsed)
        self.assertEqual(
            result["fragment_sha256"],
            canonical_score_sha256(fragment),
        )

    def test_large_slice_returns_bounded_summary_not_partial_score(self) -> None:
        result = slice_score(
            _score(),
            {
                "kind": SCORE_SLICE_QUERY_KIND,
                "schema_version": 1,
                "max_notes": 1,
            },
        )

        self.assertEqual(result["mode"], "summary")
        self.assertTrue(result["truncated"])
        self.assertNotIn("fragment", result)
        self.assertEqual(result["matched_note_count"], 4)
        self.assertEqual(result["preview_note_count"], 1)
        self.assertEqual(len(result["event_preview"]), 1)
        self.assertEqual(
            sum(item["matched_note_count"] for item in result["by_part"]),
            4,
        )

    def test_unknown_filter_identity_is_rejected(self) -> None:
        with self.assertRaises(ScoreOpsError) as caught:
            slice_score(
                _score(),
                {
                    "kind": SCORE_SLICE_QUERY_KIND,
                    "schema_version": 1,
                    "event_ids": ["does-not-exist"],
                },
            )
        self.assertEqual(caught.exception.code, "event_not_found")


class ScorePatchTests(unittest.TestCase):
    def test_hash_conflict_fails_without_mutating_input(self) -> None:
        score = _score()
        before = copy.deepcopy(score)
        patch = _patch(
            score,
            [
                {
                    "op": "update_note",
                    "event_id": "melody-1",
                    "changes": {"pitch": "D4"},
                }
            ],
        )
        patch["base_score_sha256"] = "0" * 64

        with self.assertRaises(ScoreOpsError) as caught:
            apply_score_patch(score, patch)
        self.assertEqual(caught.exception.code, "base_score_hash_mismatch")
        self.assertEqual(score, before)

    def test_old_value_conflict_fails_without_mutating_input(self) -> None:
        score = _score()
        before = copy.deepcopy(score)

        with self.assertRaises(ScoreOpsError) as caught:
            apply_score_patch(
                score,
                _patch(
                    score,
                    [
                        {
                            "op": "update_note",
                            "event_id": "melody-1",
                            "expect": {"pitch": "D4"},
                            "changes": {"pitch": "E4"},
                        }
                    ],
                ),
            )
        self.assertEqual(caught.exception.code, "expectation_failed")
        self.assertEqual(
            caught.exception.to_dict()["details"]["actual"]["value"],
            "C4",
        )
        self.assertEqual(score, before)

    def test_boolean_and_numeric_expectations_are_distinct(self) -> None:
        cases = (
            (
                "numeric actual does not satisfy boolean expectation",
                {},
                {
                    "op": "delete_note",
                    "event_id": "melody-1",
                    "expect": {"bar": True},
                },
                "bar",
            ),
            (
                "boolean actual does not satisfy numeric expectation",
                {"tie": True},
                {
                    "op": "update_note",
                    "event_id": "melody-1",
                    "expect": {"tie": 1},
                    "changes": {"dynamic": "f"},
                },
                "tie",
            ),
        )

        for label, note_changes, operation, expected_field in cases:
            with self.subTest(label=label):
                score = _score()
                score["parts"][0]["notes"][0].update(note_changes)
                before = copy.deepcopy(score)

                with self.assertRaises(ScoreOpsError) as caught:
                    apply_score_patch(score, _patch(score, [operation]))

                self.assertEqual(caught.exception.code, "expectation_failed")
                self.assertEqual(
                    caught.exception.to_dict()["details"]["field"],
                    expected_field,
                )
                self.assertEqual(score, before)

    def test_non_integer_expectations_keep_json_number_equivalence(self) -> None:
        score = _score()
        result = apply_score_patch(
            score,
            _patch(
                score,
                [
                    {
                        "op": "update_note",
                        "event_id": "melody-1",
                        "expect": {"beat": 1.0},
                        "changes": {"dynamic": "f"},
                    }
                ],
            ),
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["score"]["parts"][0]["notes"][0]["dynamic"], "f")

    def test_integer_expectations_reject_integral_floats(self) -> None:
        cases = (("bar", {}), ("staff", {"staff": 1}))

        for field, note_changes in cases:
            with self.subTest(field=field):
                score = _score()
                score["parts"][0]["notes"][0].update(note_changes)
                before = copy.deepcopy(score)

                with self.assertRaises(ScoreOpsError) as caught:
                    apply_score_patch(
                        score,
                        _patch(
                            score,
                            [
                                {
                                    "op": "update_note",
                                    "event_id": "melody-1",
                                    "expect": {field: 1.0},
                                    "changes": {"dynamic": "f"},
                                }
                            ],
                        ),
                    )

                self.assertEqual(caught.exception.code, "expectation_failed")
                self.assertEqual(
                    caught.exception.to_dict()["details"]["field"],
                    field,
                )
                self.assertEqual(score, before)

    def test_update_preserves_identity_and_returns_a_structured_diff(self) -> None:
        score = _score()
        result = apply_score_patch(
            score,
            _patch(
                score,
                [
                    {
                        "op": "update_note",
                        "event_id": "melody-1",
                        "expect": {"pitch": "C4", "articulation": None},
                        "changes": {
                            "pitch": "D4",
                            "articulation": "legato",
                        },
                    }
                ],
            ),
        )

        self.assertEqual(result["kind"], SCORE_PATCH_RESULT_KIND)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        edited = result["score"]
        note = edited["parts"][0]["notes"][0]
        self.assertEqual(note["event_id"], "melody-1")
        self.assertEqual(note["pitch"], "D4")
        self.assertEqual(score["parts"][0]["notes"][0]["pitch"], "C4")
        self.assertEqual(result["diff"]["counts"]["updated"], 1)
        self.assertIn(
            "pitch",
            result["diff"]["changes"][0]["field_changes"],
        )

    def test_add_allocates_a_unique_deterministic_engine_id(self) -> None:
        score = _score()
        patch = _patch(
            score,
            [
                {
                    "op": "add_note",
                    "part_id": "melody",
                    "note": {
                        "bar": 2,
                        "beat": 3,
                        "duration_beats": 1,
                        "pitch": "A4",
                        "dynamic": "mp",
                    },
                }
            ],
        )

        first = apply_score_patch(score, patch)
        second = apply_score_patch(score, patch)
        first_id = first["operation_results"][0]["event_id"]
        second_id = second["operation_results"][0]["event_id"]
        self.assertEqual(first_id, second_id)
        self.assertTrue(first_id.startswith("event-"))
        self.assertNotIn(first_id, {"melody-1", "melody-2", "melody-3", "bass-1"})
        self.assertEqual(
            first["after_score_sha256"],
            second["after_score_sha256"],
        )
        validate_score_time_coordinates(parse_score_document(first["score"]))

    def test_event_id_cannot_be_modified_or_supplied_for_an_add(self) -> None:
        score = _score()
        with self.assertRaises(ScoreOpsError) as modified:
            apply_score_patch(
                score,
                _patch(
                    score,
                    [
                        {
                            "op": "update_note",
                            "event_id": "melody-1",
                            "changes": {"event_id": "replacement"},
                        }
                    ],
                ),
            )
        self.assertEqual(modified.exception.code, "event_id_immutable")

        with self.assertRaises(ScoreOpsError) as supplied:
            apply_score_patch(
                score,
                _patch(
                    score,
                    [
                        {
                            "op": "add_note",
                            "part_id": "melody",
                            "note": {
                                "event_id": "melody-1",
                                "bar": 2,
                                "beat": 2,
                                "duration_beats": 1,
                                "pitch": "A4",
                            },
                        }
                    ],
                ),
            )
        self.assertEqual(supplied.exception.code, "event_id_engine_owned")

    def test_delete_uses_identity_and_old_value_precondition(self) -> None:
        score = _score()
        result = apply_score_patch(
            score,
            _patch(
                score,
                [
                    {
                        "op": "delete_note",
                        "event_id": "bass-1",
                        "expect": {"pitch": "C3"},
                    }
                ],
            ),
        )

        self.assertEqual(result["diff"]["counts"]["deleted"], 1)
        self.assertEqual(result["score"]["parts"][1]["notes"], [])

    def test_invalid_and_non_finite_changes_are_rejected_atomically(self) -> None:
        for value, expected_code in (
            (math.nan, "non_canonical_json"),
            ("not-a-pitch", "patched_score_invalid"),
        ):
            with self.subTest(value=value):
                score = _score()
                before = copy.deepcopy(score)
                with self.assertRaises(ScoreOpsError) as caught:
                    apply_score_patch(
                        score,
                        _patch(
                            score,
                            [
                                {
                                    "op": "update_note",
                                    "event_id": "melody-1",
                                    "changes": {"pitch": value},
                                }
                            ],
                        ),
                    )
                self.assertEqual(caught.exception.code, expected_code)
                self.assertEqual(score, before)


class ScoreCompareTests(unittest.TestCase):
    def test_diff_is_bounded_but_counts_all_changes(self) -> None:
        before = _score()
        after = copy.deepcopy(before)
        after["parts"][0]["notes"][0]["pitch"] = "D4"
        after["parts"][0]["notes"][1]["dynamic"] = "ff"
        after["parts"][1]["notes"].append(
            {
                "event_id": "bass-2",
                "bar": 2,
                "beat": 1,
                "duration_beats": 2,
                "pitch": "G2",
            }
        )

        result = compare_scores(before, after, max_changes=1)

        self.assertEqual(result["kind"], SCORE_COMPARE_RESULT_KIND)
        self.assertTrue(result["ok"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["total_change_count"], 3)
        self.assertEqual(result["returned_change_count"], 1)
        self.assertTrue(result["changes_truncated"])
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["counts"]["updated"], 2)
        self.assertEqual(result["counts"]["added"], 1)


class ScoreOpsCliTests(unittest.TestCase):
    def test_cli_upgrade_patch_and_compare_form_a_file_revision_loop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = _score()
            legacy.pop("schema_version")
            for part in legacy["parts"]:
                for note in part["notes"]:
                    note.pop("event_id")
            legacy_path = root / "legacy.json"
            v1_path = root / "v1.json"
            patch_path = root / "patch.json"
            v2_path = root / "v2.json"
            legacy_path.write_text(
                json.dumps(legacy, ensure_ascii=False),
                encoding="utf-8",
            )

            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "upgrade-score",
                            "--score",
                            str(legacy_path),
                            "--output",
                            str(v1_path),
                        ]
                    ),
                    0,
                )
            v1 = json.loads(v1_path.read_text(encoding="utf-8"))
            patch_path.write_text(
                json.dumps(
                    {
                        "kind": SCORE_PATCH_KIND,
                        "schema_version": 1,
                        "base_score_sha256": canonical_score_sha256(v1),
                        "operations": [
                            {
                                "op": "update_note",
                                "event_id": "event-000001",
                                "expect": {"pitch": "C4"},
                                "changes": {"pitch": "D4"},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "score-patch",
                            "--score",
                            str(v1_path),
                            "--patch",
                            str(patch_path),
                            "--output",
                            str(v2_path),
                        ]
                    ),
                    0,
                )
            comparison = io.StringIO()
            with redirect_stdout(comparison):
                self.assertEqual(
                    cli_main(
                        [
                            "score-compare",
                            "--before",
                            str(v1_path),
                            "--after",
                            str(v2_path),
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                json.loads(comparison.getvalue())["counts"]["updated"],
                1,
            )

    def test_cli_refuses_to_overwrite_a_score_revision_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            score_path = root / "score.json"
            output_path = root / "existing.json"
            score_path.write_text(
                json.dumps(_score(), ensure_ascii=False),
                encoding="utf-8",
            )
            output_path.write_text("keep", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                exit_code = cli_main(
                    [
                        "upgrade-score",
                        "--score",
                        str(score_path),
                        "--output",
                        str(output_path),
                    ]
                )
            self.assertEqual(exit_code, 2)
            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                "keep",
            )


if __name__ == "__main__":
    unittest.main()
