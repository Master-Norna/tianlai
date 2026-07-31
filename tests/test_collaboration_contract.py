from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import unittest

from jsonschema import Draft202012Validator

from tianlai.capability import InstrumentCapability, load_capabilities
from tianlai.conductor import build_plan
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document


ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = InstrumentCapability(
    name="测试乐器",
    relative_path="测试乐器",
    manifest_path="测试乐器/乐器.json",
    implementation_type="oscillator",
    pitched=True,
    note_min=0.0,
    note_max=127.0,
    articulations=("sustain",),
    default_articulation="sustain",
    articulation_source="test",
    onset_seconds=None,
    quality_tier="candidate",
    license_status="approved",
)
CAPABILITIES = {"测试乐器": CAPABILITY}


def _score() -> dict:
    return {
        "title": "v0.5 collaboration contract",
        "tempo_map": [
            {
                "bar": 1,
                "bpm": 60,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "melody",
                "notes": [
                    {
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                    }
                ],
            },
            {
                "id": "cello_atmos",
                "notes": [
                    {
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 4,
                        "pitch": "C3",
                    }
                ],
            },
        ],
    }


def _roster() -> dict:
    return {
        "name": "v0.5 collaboration contract",
        "assignments": [
            {
                "part": "melody",
                "instrument": "测试乐器",
                "role": {
                    "function": "lead",
                    "prominence": "foreground",
                },
            },
            {
                "part": "cello_atmos",
                "instrument": "测试乐器",
                "role": {
                    "function": "pad",
                    "prominence": "background",
                    "label": "大提琴氛围层",
                },
            },
        ],
        "collaboration": {
            "mode": "suggest",
            "analysis": {
                "metric": "overlap_active_rms",
                "window_ms": 400,
                "hop_ms": 100,
                "gate_dbfs": -60,
            },
            "balance_relations": [
                {
                    "subject": "cello_atmos",
                    "reference": "melody",
                    "target_offset_db": -8,
                    "tolerance_db": 2,
                    "max_suggestion_db": 4,
                }
            ],
        },
    }


class CollaborationContractTests(unittest.TestCase):
    def test_declared_role_and_collaboration_round_trip_to_resolved_roster(
        self,
    ) -> None:
        roster = parse_roster_document(_roster(), CAPABILITIES)

        self.assertEqual(roster.collaboration.mode, "suggest")
        self.assertTrue(roster.collaboration.declared)
        self.assertEqual(
            roster.collaboration.analysis.metric,
            "overlap_active_rms",
        )
        self.assertEqual(
            roster.executors[1].role.to_dict(),
            {
                "function": "pad",
                "prominence": "background",
                "label": "大提琴氛围层",
            },
        )
        serialized = roster.to_dict()
        self.assertEqual(
            serialized["collaboration"],
            _roster()["collaboration"],
        )
        self.assertEqual(
            serialized["executors"][0]["role"],
            {
                "function": "lead",
                "prominence": "foreground",
            },
        )

    def test_creator_declared_part_group_round_trips_and_is_a_relation_endpoint(
        self,
    ) -> None:
        data = _roster()
        data["assignments"].append(
            {
                "part": "piano_right",
                "instrument": "测试乐器",
            }
        )
        data["collaboration"]["part_groups"] = [
            {
                "id": "piano",
                "parts": ["melody", "piano_right"],
            }
        ]
        data["collaboration"]["balance_relations"][0]["reference"] = "piano"

        roster = parse_roster_document(data, CAPABILITIES)

        self.assertEqual(
            roster.collaboration.part_groups[0].to_dict(),
            {
                "id": "piano",
                "parts": ["melody", "piano_right"],
            },
        )
        self.assertEqual(
            roster.collaboration.balance_relations[0].reference,
            "piano",
        )
        self.assertEqual(
            roster.to_dict()["collaboration"]["part_groups"],
            data["collaboration"]["part_groups"],
        )

    def test_part_groups_are_explicit_flat_views_of_assigned_parts(self) -> None:
        cases = (
            (
                "id conflicts with a part",
                [{"id": "melody", "parts": ["cello_atmos"]}],
                "conflicts with an assigned part",
            ),
            (
                "duplicate id",
                [
                    {"id": "ensemble", "parts": ["melody"]},
                    {"id": "ensemble", "parts": ["cello_atmos"]},
                ],
                "duplicate id",
            ),
            (
                "empty members",
                [{"id": "ensemble", "parts": []}],
                "non-empty array",
            ),
            (
                "duplicate member",
                [{"id": "ensemble", "parts": ["melody", "melody"]}],
                "duplicate part",
            ),
            (
                "unknown member",
                [{"id": "ensemble", "parts": ["ghost"]}],
                "unassigned part",
            ),
            (
                "nested group",
                [
                    {"id": "first", "parts": ["melody"]},
                    {"id": "second", "parts": ["first"]},
                ],
                "cannot be nested",
            ),
        )
        for name, groups, expected in cases:
            with self.subTest(case=name):
                data = _roster()
                data["collaboration"]["part_groups"] = groups
                with self.assertRaisesRegex(ValueError, expected):
                    parse_roster_document(data, CAPABILITIES)

    def test_group_endpoint_must_be_declared_and_relation_endpoints_differ(
        self,
    ) -> None:
        data = _roster()
        relation = data["collaboration"]["balance_relations"][0]
        relation["reference"] = "piano"
        with self.assertRaisesRegex(ValueError, "undeclared part group"):
            parse_roster_document(data, CAPABILITIES)

        data = _roster()
        data["collaboration"]["part_groups"] = [
            {"id": "ensemble", "parts": ["melody", "cello_atmos"]}
        ]
        relation = data["collaboration"]["balance_relations"][0]
        relation["subject"] = "ensemble"
        relation["reference"] = "ensemble"
        with self.assertRaisesRegex(ValueError, "different parts"):
            parse_roster_document(data, CAPABILITIES)

        data = _roster()
        data["collaboration"]["part_groups"] = [
            {"id": "ensemble", "parts": ["melody", "cello_atmos"]}
        ]
        relation = data["collaboration"]["balance_relations"][0]
        relation["subject"] = "ensemble"
        relation["reference"] = "melody"
        with self.assertRaisesRegex(ValueError, "disjoint"):
            parse_roster_document(data, CAPABILITIES)

        data = _roster()
        data["collaboration"]["part_groups"] = [
            {"id": "first", "parts": ["melody"]},
            {
                "id": "second",
                "parts": ["melody", "cello_atmos"],
            },
        ]
        relation = data["collaboration"]["balance_relations"][0]
        relation["subject"] = "first"
        relation["reference"] = "second"
        with self.assertRaisesRegex(ValueError, "shared parts: melody"):
            parse_roster_document(data, CAPABILITIES)

    def test_role_is_auditable_in_the_plan_but_does_not_change_notes(
        self,
    ) -> None:
        score = parse_score_document(_score())
        with_roles = build_plan(
            score,
            parse_roster_document(_roster(), CAPABILITIES),
        )
        without_role_data = _roster()
        without_role_data.pop("collaboration")
        for assignment in without_role_data["assignments"]:
            assignment.pop("role")
        without_roles = build_plan(
            score,
            parse_roster_document(without_role_data, CAPABILITIES),
        )

        by_id = {
            part.executor.executor_id: part for part in with_roles.parts
        }
        self.assertEqual(
            by_id["melody"].to_dict()["role"],
            {
                "function": "lead",
                "prominence": "foreground",
            },
        )
        for with_role, without_role in zip(
            with_roles.parts, without_roles.parts, strict=True
        ):
            self.assertEqual(
                with_role.performance,
                without_role.performance,
            )
            self.assertEqual(with_role.trace, without_role.trace)

    def test_absent_v05_fields_keep_legacy_serialization_clean(self) -> None:
        data = _roster()
        data.pop("collaboration")
        for assignment in data["assignments"]:
            assignment.pop("role")
        roster = parse_roster_document(data, CAPABILITIES)

        self.assertEqual(roster.collaboration.mode, "manual")
        self.assertFalse(roster.collaboration.declared)
        self.assertNotIn("collaboration", roster.to_dict())
        for executor in roster.executors:
            self.assertIsNone(executor.role)
            self.assertNotIn("role", executor.to_dict())
        plan = build_plan(parse_score_document(_score()), roster).to_dict()
        self.assertNotIn("collaboration", plan)
        for part in plan["parts"]:
            self.assertNotIn("role", part)

    def test_committed_legacy_example_keeps_v05_fields_absent(self) -> None:
        score_data = json.loads(
            (ROOT / "examples" / "小编制示例.score.json").read_text(
                encoding="utf-8"
            )
        )
        roster_data = json.loads(
            (ROOT / "examples" / "小编制示例.roster.json").read_text(
                encoding="utf-8"
            )
        )
        roster = parse_roster_document(
            roster_data,
            load_capabilities(ROOT / "乐器"),
        )

        self.assertNotIn("collaboration", roster.to_dict())
        self.assertTrue(
            all("role" not in item for item in roster.to_dict()["executors"])
        )
        plan = build_plan(
            parse_score_document(score_data),
            roster,
        ).to_dict()
        self.assertNotIn("collaboration", plan)
        self.assertTrue(all("role" not in part for part in plan["parts"]))

    def test_explicit_empty_collaboration_uses_canonical_manual_defaults(
        self,
    ) -> None:
        data = _roster()
        data["collaboration"] = {}
        roster = parse_roster_document(data, CAPABILITIES)

        self.assertEqual(
            roster.to_dict()["collaboration"],
            {
                "mode": "manual",
                "analysis": {
                    "metric": "overlap_active_rms",
                    "window_ms": 400.0,
                    "hop_ms": 100.0,
                    "gate_dbfs": -60.0,
                },
                "balance_relations": [],
            },
        )

    def test_all_role_enum_values_are_accepted(self) -> None:
        functions = (
            "lead",
            "countermelody",
            "harmony",
            "pad",
            "bass",
            "rhythm",
            "accent",
            "texture",
            "ambience",
            "effect",
            "other",
        )
        for function in functions:
            with self.subTest(function=function):
                data = _roster()
                data["assignments"][0]["role"]["function"] = function
                parse_roster_document(data, CAPABILITIES)
        for prominence in ("foreground", "midground", "background"):
            with self.subTest(prominence=prominence):
                data = _roster()
                data["assignments"][0]["role"]["prominence"] = prominence
                parse_roster_document(data, CAPABILITIES)

    def test_role_requires_both_axes_and_rejects_unknowns(self) -> None:
        for missing in ("function", "prominence"):
            with self.subTest(missing=missing):
                data = _roster()
                del data["assignments"][0]["role"][missing]
                with self.assertRaisesRegex(
                    ValueError,
                    re.escape(
                        f"roster.assignments[0].role.{missing} is required"
                    ),
                ):
                    parse_roster_document(data, CAPABILITIES)

        invalid = (
            ("function", "soloist"),
            ("prominence", "front"),
        )
        for key, value in invalid:
            with self.subTest(key=key):
                data = _roster()
                data["assignments"][0]["role"][key] = value
                with self.assertRaisesRegex(ValueError, f"role\\.{key}"):
                    parse_roster_document(data, CAPABILITIES)

        data = _roster()
        data["assignments"][0]["role"]["priority"] = 10
        with self.assertRaisesRegex(
            ValueError,
            re.escape("roster.assignments[0].role.priority"),
        ):
            parse_roster_document(data, CAPABILITIES)

    def test_explicit_null_or_blank_role_values_are_rejected(self) -> None:
        invalid_roles = (
            None,
            {"function": "lead", "prominence": "foreground", "label": None},
            {"function": "lead", "prominence": "foreground", "label": "   "},
        )
        for role in invalid_roles:
            with self.subTest(role=role):
                data = _roster()
                data["assignments"][0]["role"] = role
                with self.assertRaises(ValueError):
                    parse_roster_document(data, CAPABILITIES)

    def test_collaboration_objects_reject_unknown_fields(self) -> None:
        mutations = (
            ("collaboration", lambda data: data["collaboration"].update({"auto_apply": True})),
            ("analysis", lambda data: data["collaboration"]["analysis"].update({"lufs": True})),
            (
                "part_group",
                lambda data: data["collaboration"].update(
                    {
                        "part_groups": [
                            {
                                "id": "ensemble",
                                "parts": ["melody"],
                                "gain_db": -3,
                            }
                        ]
                    }
                ),
            ),
            (
                "relation",
                lambda data: data["collaboration"]["balance_relations"][0].update(
                    {"ducking": True}
                ),
            ),
        )
        expected_paths = (
            "roster.collaboration.auto_apply",
            "roster.collaboration.analysis.lufs",
            "roster.collaboration.part_groups[0].gain_db",
            "roster.collaboration.balance_relations[0].ducking",
        )
        for (name, mutate), expected in zip(
            mutations, expected_paths, strict=True
        ):
            with self.subTest(layer=name):
                data = _roster()
                mutate(data)
                with self.assertRaisesRegex(
                    ValueError, re.escape(expected)
                ):
                    parse_roster_document(data, CAPABILITIES)

    def test_collaboration_mode_and_metric_are_closed_enums(self) -> None:
        data = _roster()
        data["collaboration"]["mode"] = "apply"
        with self.assertRaisesRegex(ValueError, "collaboration.mode"):
            parse_roster_document(data, CAPABILITIES)

        data = _roster()
        data["collaboration"]["analysis"]["metric"] = "lufs"
        with self.assertRaisesRegex(ValueError, "analysis.metric"):
            parse_roster_document(data, CAPABILITIES)

    def test_analysis_and_relation_numbers_must_be_finite(self) -> None:
        analysis_fields = ("window_ms", "hop_ms", "gate_dbfs")
        relation_fields = (
            "target_offset_db",
            "tolerance_db",
            "max_suggestion_db",
        )
        for field_name in analysis_fields:
            for invalid in (math.nan, math.inf, -math.inf, True, "400"):
                with self.subTest(field=field_name, invalid=invalid):
                    data = _roster()
                    data["collaboration"]["analysis"][field_name] = invalid
                    with self.assertRaisesRegex(
                        ValueError,
                        re.escape(
                            f"roster.collaboration.analysis.{field_name}"
                        ),
                    ):
                        parse_roster_document(data, CAPABILITIES)
        for field_name in relation_fields:
            for invalid in (math.nan, math.inf, -math.inf, False, "-8"):
                with self.subTest(field=field_name, invalid=invalid):
                    data = _roster()
                    relation = data["collaboration"][
                        "balance_relations"
                    ][0]
                    relation[field_name] = invalid
                    with self.assertRaisesRegex(
                        ValueError,
                        re.escape(
                            "roster.collaboration.balance_relations[0]."
                            f"{field_name}"
                        ),
                    ):
                        parse_roster_document(data, CAPABILITIES)

    def test_semantically_invalid_analysis_ranges_are_rejected(self) -> None:
        cases = (
            ("window_ms", 19.999),
            ("window_ms", 2000.001),
            ("hop_ms", 9.999),
            ("hop_ms", 2000.001),
            ("gate_dbfs", 0.1),
            ("gate_dbfs", -301),
        )
        for field_name, invalid in cases:
            with self.subTest(field=field_name):
                data = _roster()
                data["collaboration"]["analysis"][field_name] = invalid
                with self.assertRaisesRegex(ValueError, field_name):
                    parse_roster_document(data, CAPABILITIES)

        data = _roster()
        data["collaboration"]["analysis"]["window_ms"] = 50
        data["collaboration"]["analysis"]["hop_ms"] = 100
        with self.assertRaisesRegex(ValueError, "must not exceed window_ms"):
            parse_roster_document(data, CAPABILITIES)

        for window_ms, hop_ms in ((20, 10), (2000, 2000)):
            with self.subTest(window_ms=window_ms, hop_ms=hop_ms):
                data = _roster()
                data["collaboration"]["analysis"]["window_ms"] = window_ms
                data["collaboration"]["analysis"]["hop_ms"] = hop_ms
                parsed = parse_roster_document(data, CAPABILITIES)
                self.assertEqual(
                    parsed.collaboration.analysis.window_ms,
                    float(window_ms),
                )
                self.assertEqual(
                    parsed.collaboration.analysis.hop_ms,
                    float(hop_ms),
                )

        for field_name in ("tolerance_db", "max_suggestion_db"):
            with self.subTest(field=field_name):
                data = _roster()
                relation = data["collaboration"]["balance_relations"][0]
                relation[field_name] = -0.1
                with self.assertRaisesRegex(ValueError, field_name):
                    parse_roster_document(data, CAPABILITIES)

    def test_balance_relations_require_complete_distinct_assigned_parts(
        self,
    ) -> None:
        required = (
            "subject",
            "reference",
            "target_offset_db",
            "tolerance_db",
            "max_suggestion_db",
        )
        for missing in required:
            with self.subTest(missing=missing):
                data = _roster()
                del data["collaboration"]["balance_relations"][0][missing]
                with self.assertRaisesRegex(ValueError, missing):
                    parse_roster_document(data, CAPABILITIES)

        for key in ("subject", "reference"):
            with self.subTest(unassigned=key):
                data = _roster()
                data["collaboration"]["balance_relations"][0][key] = "ghost"
                with self.assertRaisesRegex(ValueError, "unassigned part"):
                    parse_roster_document(data, CAPABILITIES)

        data = _roster()
        relation = data["collaboration"]["balance_relations"][0]
        relation["subject"] = relation["reference"]
        with self.assertRaisesRegex(ValueError, "different parts"):
            parse_roster_document(data, CAPABILITIES)

    def test_duplicate_balance_relation_is_rejected(self) -> None:
        data = _roster()
        relation = data["collaboration"]["balance_relations"][0]
        data["collaboration"]["balance_relations"].append(
            copy.deepcopy(relation)
        )
        with self.assertRaisesRegex(ValueError, "duplicate relation"):
            parse_roster_document(data, CAPABILITIES)

    def test_balance_relation_count_has_a_workload_guard(self) -> None:
        data = _roster()
        relation = data["collaboration"]["balance_relations"][0]
        data["collaboration"]["balance_relations"] = [
            copy.deepcopy(relation)
            for _index in range(257)
        ]

        with self.assertRaisesRegex(ValueError, "最多允许 256"):
            parse_roster_document(data, CAPABILITIES)


class CollaborationContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (ROOT / "schemas" / "roster.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def test_schema_accepts_v05_contract_and_legacy_roster(self) -> None:
        self.validator.validate(_roster())
        grouped = _roster()
        grouped["collaboration"]["part_groups"] = [
            {"id": "ensemble", "parts": ["melody", "cello_atmos"]}
        ]
        grouped["collaboration"]["balance_relations"][0][
            "reference"
        ] = "ensemble"
        self.validator.validate(grouped)
        legacy = _roster()
        legacy.pop("collaboration")
        for assignment in legacy["assignments"]:
            assignment.pop("role")
        self.validator.validate(legacy)

    def test_schema_rejects_incomplete_role_and_relation(self) -> None:
        data = _roster()
        del data["assignments"][0]["role"]["prominence"]
        self.assertTrue(list(self.validator.iter_errors(data)))

        data = _roster()
        relation = data["collaboration"]["balance_relations"][0]
        data["collaboration"]["balance_relations"] = [
            copy.deepcopy(relation)
            for _index in range(257)
        ]
        self.assertTrue(list(self.validator.iter_errors(data)))

        data = _roster()
        del data["collaboration"]["balance_relations"][0][
            "max_suggestion_db"
        ]
        self.assertTrue(list(self.validator.iter_errors(data)))

    def test_schema_enforces_collaboration_analysis_workload_bounds(
        self,
    ) -> None:
        invalid = (
            ("window_ms", 19.999),
            ("window_ms", 2000.001),
            ("hop_ms", 9.999),
            ("hop_ms", 2000.001),
        )
        for field_name, value in invalid:
            with self.subTest(field=field_name, value=value):
                data = _roster()
                data["collaboration"]["analysis"][field_name] = value
                self.assertTrue(list(self.validator.iter_errors(data)))

        for window_ms, hop_ms in ((20, 10), (2000, 2000)):
            with self.subTest(window_ms=window_ms, hop_ms=hop_ms):
                data = _roster()
                data["collaboration"]["analysis"]["window_ms"] = window_ms
                data["collaboration"]["analysis"]["hop_ms"] = hop_ms
                self.validator.validate(data)

    def test_schema_keeps_all_new_object_layers_closed(self) -> None:
        collaboration = self.schema["properties"]["collaboration"]
        analysis = collaboration["properties"]["analysis"]
        part_group = collaboration["properties"]["part_groups"]["items"]
        relation = collaboration["properties"]["balance_relations"]["items"]
        role = self.schema["properties"]["assignments"]["items"][
            "properties"
        ]["role"]
        for layer in (collaboration, analysis, part_group, relation, role):
            self.assertFalse(layer["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
