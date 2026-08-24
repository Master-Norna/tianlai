from __future__ import annotations

import copy
import math
import unittest

from tianlai.composition_map import (
    COMPOSITION_MAP_INSPECTION_KIND,
    COMPOSITION_MAP_KIND,
    CompositionMapError,
    composition_map_sha256,
    inspect_composition_map,
    normalize_composition_map,
    validate_composition_map,
)
from tianlai.score_ops import canonical_score_sha256


def _score() -> dict:
    return {
        "schema_version": 1,
        "title": "Current Work",
        "sample_rate": 48_000,
        "tail_seconds": 1,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 90,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "lead",
                "name": "Lead",
                "default_dynamic": "mp",
                "notes": [
                    {
                        "event_id": "lead-1",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                    },
                    {
                        "event_id": "lead-2",
                        "bar": 3,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "E4",
                        "dynamic": "f",
                        "articulation": "accent",
                    },
                ],
            },
            {
                "id": "support",
                "name": "Support",
                "notes": [
                    {
                        "event_id": "support-1",
                        "bar": 2,
                        "beat": 1,
                        "duration_beats": 4,
                        "pitch": "C3",
                    },
                    {
                        "event_id": "support-2",
                        "bar": 5,
                        "beat": 1,
                        "duration_beats": 2,
                        "pitch": "G2",
                    },
                ],
            },
        ],
    }


def _map() -> dict:
    return {
        "kind": COMPOSITION_MAP_KIND,
        "schema_version": 1,
        "nodes": [
            {
                "node_id": "seed",
                "label": "Seed",
                "function": "Establish the current work's identity.",
                "bar_range": {"start": 1, "end": 2},
                "depends_on_claim_ids": ["charter.promise"],
                "established_material": {"event_ids": ["lead-1"]},
                "preserve": ["pulse"],
                "transform": ["register"],
                "role_changes": [
                    {"part_id": "lead", "change": "enters the foreground"}
                ],
                "scarce_resources": ["high register"],
                "ending_response": None,
                "open_questions": ["Has the identity become traceable?"],
            },
            {
                "node_id": "answer",
                "label": "Answer",
                "function": "Respond without prescribing a cadence or climax.",
                "bar_range": {"start": 3, "end": 4},
                "depends_on_claim_ids": ["charter.ending", "missing.claim"],
                "established_material": {
                    "event_ids": ["lead-2", "deleted-event"]
                },
                "role_changes": [
                    {"part_id": "absent-part", "change": "withdraws"}
                ],
                "ending_response": "The opening interval returns in another role.",
            },
        ],
    }


class CompositionMapContractTests(unittest.TestCase):
    def test_normalization_expands_defaults_and_hashes_semantic_value(self) -> None:
        minimal = {
            "kind": COMPOSITION_MAP_KIND,
            "schema_version": 1,
            "nodes": [
                {
                    "node_id": "process-state",
                    "label": "Process state",
                    "function": "Let a texture evolve without a fixed section form.",
                }
            ],
        }
        before = copy.deepcopy(minimal)

        normalized = normalize_composition_map(minimal)

        self.assertEqual(minimal, before)
        node = normalized["nodes"][0]
        self.assertIsNone(node["bar_range"])
        self.assertEqual(node["depends_on_claim_ids"], [])
        self.assertEqual(node["established_material"], {"event_ids": []})
        self.assertEqual(node["role_changes"], [])
        expanded_reordered = {
            "nodes": [dict(reversed(list(node.items())))],
            "schema_version": 1,
            "kind": COMPOSITION_MAP_KIND,
        }
        self.assertEqual(
            composition_map_sha256(minimal),
            composition_map_sha256(expanded_reordered),
        )
        self.assertRegex(composition_map_sha256(minimal), r"^[0-9a-f]{64}$")

    def test_strict_types_unknown_fields_and_duplicate_ids_are_rejected(self) -> None:
        mutations = []

        wrong_version = _map()
        wrong_version["schema_version"] = True
        mutations.append((wrong_version, "unsupported_schema_version"))

        boolean_start = _map()
        boolean_start["nodes"][0]["bar_range"]["start"] = True
        mutations.append((boolean_start, "positive_integer_required"))

        float_end = _map()
        float_end["nodes"][0]["bar_range"]["end"] = 2.0
        mutations.append((float_end, "positive_integer_required"))

        unknown = _map()
        unknown["nodes"][0]["historical_example"] = "forbidden"
        mutations.append((unknown, "unknown_field"))

        duplicate_node = _map()
        duplicate_node["nodes"][1]["node_id"] = "seed"
        mutations.append((duplicate_node, "duplicate_node_id"))

        duplicate_event = _map()
        duplicate_event["nodes"][0]["established_material"]["event_ids"] *= 2
        mutations.append((duplicate_event, "duplicate_list_item"))

        invalid_identifier = _map()
        invalid_identifier["nodes"][0]["node_id"] = "Localized Node"
        mutations.append((invalid_identifier, "invalid_stable_id"))

        for document, code in mutations:
            with self.subTest(code=code):
                with self.assertRaises(CompositionMapError) as caught:
                    normalize_composition_map(document)
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.to_dict()["ok"])

    def test_validation_can_bind_dependencies_to_current_charter(self) -> None:
        validate_composition_map(
            _map(),
            charter_claim_ids={
                "charter.promise": "promise",
                "charter.ending": "ending",
                "missing.claim": "now present",
            },
        )
        with self.assertRaises(CompositionMapError) as caught:
            validate_composition_map(
                _map(),
                charter_claim_ids=["charter.promise", "charter.ending"],
            )
        self.assertEqual(caught.exception.code, "claim_not_found")
        self.assertEqual(caught.exception.details["claim_ids"], ["missing.claim"])

    def test_preserve_and_transform_cannot_repeat_the_same_directive(self) -> None:
        document = _map()
        document["nodes"][0]["transform"] = ["pulse"]
        with self.assertRaises(CompositionMapError) as caught:
            normalize_composition_map(document)
        self.assertEqual(caught.exception.code, "conflicting_node_directive")


class CompositionMapInspectionTests(unittest.TestCase):
    def test_inspection_returns_whole_score_facts_coverage_and_questions(self) -> None:
        score = _score()
        composition_map = _map()
        before_score = copy.deepcopy(score)
        before_map = copy.deepcopy(composition_map)

        result = inspect_composition_map(
            score,
            composition_map,
            {
                "charter.promise": "Remain identifiable",
                "charter.ending": "Answer the opening",
                "charter.unused": "Keep one uncertainty open",
            },
        )

        self.assertEqual(score, before_score)
        self.assertEqual(composition_map, before_map)
        self.assertEqual(result["kind"], COMPOSITION_MAP_INSPECTION_KIND)
        self.assertTrue(result["ok"])
        self.assertTrue(result["read_only"])
        self.assertEqual(result["score_sha256"], canonical_score_sha256(score))
        self.assertEqual(
            result["composition_map_sha256"],
            composition_map_sha256(composition_map),
        )
        self.assertEqual(result["score_facts"]["onset_event_count"], 4)
        self.assertEqual(result["score_facts"]["mapped_onset_event_count"], 3)
        self.assertEqual(result["score_facts"]["unmapped_onset_event_count"], 1)
        self.assertEqual(
            result["score_facts"]["unmapped_onset_bar_ranges"],
            [{"start": 5, "end": 5}],
        )
        self.assertEqual(
            result["dependency_coverage"]["missing_claim_ids"],
            ["missing.claim"],
        )
        self.assertEqual(
            result["dependency_coverage"]["unreferenced_claim_ids"],
            ["charter.unused"],
        )
        seed = result["node_facts"][0]
        self.assertEqual(seed["observed"]["onset_event_count"], 2)
        self.assertEqual(seed["observed"]["active_part_ids"], ["lead", "support"])
        self.assertEqual(
            seed["established_material"]["locations"][0]["score_path"],
            ["parts", 0, "notes", 0],
        )
        self.assertEqual(
            seed["location"]["score_sha256"], result["score_sha256"]
        )

        kinds = {question["question_kind"] for question in result["questions"]}
        self.assertTrue(
            {
                "declared_open_question",
                "missing_claim_dependency",
                "established_material_not_found",
                "role_part_not_found",
                "unmapped_score_regions",
                "unreferenced_charter_claims",
            }.issubset(kinds)
        )
        for question in result["questions"]:
            self.assertRegex(question["question_id"], r"^question-[0-9a-f]{20}$")
            self.assertEqual(
                question["location"]["score_sha256"], result["score_sha256"]
            )

    def test_inspector_exposes_authority_boundaries_not_aesthetic_scores(self) -> None:
        result = inspect_composition_map(
            _score(),
            {
                "kind": COMPOSITION_MAP_KIND,
                "schema_version": 1,
                "nodes": [
                    {
                        "node_id": "one-state",
                        "label": "One state",
                        "function": "Sustain a process with no prescribed climax.",
                        "depends_on_claim_ids": ["charter.process"],
                    }
                ],
            },
            ["charter.process"],
        )

        self.assertFalse(result["authority_boundary"]["aesthetic_score"])
        self.assertFalse(result["authority_boundary"]["automatic_edit"])
        self.assertFalse(result["authority_boundary"]["audio_audition"])
        self.assertFalse(result["authority_boundary"]["fixed_form_assumption"])
        self.assertNotIn("score", result)
        self.assertNotIn("rating", result)
        self.assertEqual(result["map_facts"]["node_count"], 1)
        self.assertEqual(result["map_facts"]["nodes_without_bar_range"], ["one-state"])

    def test_non_monotonic_node_order_becomes_a_deterministic_question(self) -> None:
        composition_map = _map()
        composition_map["nodes"].reverse()
        normalized = normalize_composition_map(composition_map)
        self.assertEqual(
            [node["node_id"] for node in normalized["nodes"]],
            ["answer", "seed"],
        )

        first = inspect_composition_map(
            _score(),
            composition_map,
            ["charter.promise", "charter.ending", "missing.claim"],
        )
        second = inspect_composition_map(
            copy.deepcopy(_score()),
            copy.deepcopy(composition_map),
            ["missing.claim", "charter.ending", "charter.promise"],
        )

        questions = [
            question
            for question in first["questions"]
            if question["question_kind"] == "non_monotonic_node_order"
        ]
        self.assertEqual(len(questions), 1)
        question = questions[0]
        self.assertEqual(
            question["basis"]["declared_order_inversions"],
            [
                {
                    "declared_before": {
                        "array_index": 0,
                        "node_id": "answer",
                        "bar_range": {"start": 3, "end": 4},
                    },
                    "declared_after": {
                        "array_index": 1,
                        "node_id": "seed",
                        "bar_range": {"start": 1, "end": 2},
                    },
                }
            ],
        )
        self.assertEqual(
            question["location"]["bar_range"], {"start": 1, "end": 4}
        )
        self.assertEqual(first, second)
        self.assertNotIn(
            "non_monotonic_node_order",
            {
                item["question_kind"]
                for item in inspect_composition_map(
                    _score(),
                    _map(),
                    ["charter.promise", "charter.ending", "missing.claim"],
                )["questions"]
            },
        )

    def test_bool_and_nonfinite_score_numbers_are_rejected_cleanly(self) -> None:
        boolean_bar = _score()
        boolean_bar["parts"][0]["notes"][0]["bar"] = True
        with self.assertRaises(CompositionMapError) as caught:
            inspect_composition_map(boolean_bar, _map(), [])
        self.assertEqual(caught.exception.code, "invalid_score")

        nonfinite = _score()
        nonfinite["parts"][0]["notes"][0]["beat"] = math.nan
        with self.assertRaises(CompositionMapError) as caught:
            inspect_composition_map(nonfinite, _map(), [])
        self.assertEqual(caught.exception.code, "invalid_score")

    def test_question_ids_and_report_are_deterministic(self) -> None:
        first = inspect_composition_map(
            _score(), _map(), ["charter.promise", "charter.ending"]
        )
        second = inspect_composition_map(
            copy.deepcopy(_score()),
            copy.deepcopy(_map()),
            ["charter.ending", "charter.promise"],
        )
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
