from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from tianlai.authoring_roster import (
    AUTHORING_ROSTER_KIND,
    AUTHORING_ROSTER_READINESS_KIND,
    AuthoringRosterError,
    MAX_NAME_LENGTH,
    authoring_roster_readiness,
    parse_authoring_roster_document,
    to_formal_roster,
)
from tianlai.capability import InstrumentCapability
from tianlai.roster import check_roster_covers_score, parse_roster_document
from tianlai.score import parse_score_document


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ID = "世界乐器/测试琴"
CAPABILITY = InstrumentCapability(
    name="测试琴",
    relative_path=INSTRUMENT_ID,
    manifest_path="世界乐器/测试琴/乐器.json",
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
CAPABILITIES = {INSTRUMENT_ID: CAPABILITY}


def _score() -> dict:
    return {
        "schema_version": 1,
        "title": "作者态编制合同",
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1,
                "bpm": 72,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "旋律",
                "notes": [
                    {
                        "event_id": "event-melody",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                    }
                ],
            },
            {
                "id": "打击",
                "notes": [
                    {
                        "event_id": "event-percussion",
                        "bar": 1,
                        "beat": 2,
                        "duration_beats": 1,
                        "pitch": "D4",
                    }
                ],
            },
        ],
    }


def _blank() -> dict:
    return {
        "kind": AUTHORING_ROSTER_KIND,
        "schema_version": 1,
        "name": "默认配器",
        "assignments": [
            {"part": "旋律", "instrument": None},
            {"part": "打击", "instrument": None},
        ],
    }


def _assigned() -> dict:
    document = _blank()
    document["assignments"] = [
        {
            "part": "旋律",
            "instrument": INSTRUMENT_ID,
            "_note": "保留作者说明",
            "executor_id": "主奏",
            "gain_db": -3.5,
            "gain_automation": [
                {"bar": 1, "beat": 1, "offset_db": 0},
                {"bar": 2, "beat": 1.5, "offset_db": 1.25},
            ],
            "pan": -0.2,
            "transpose": 0,
            "dynamic_compression": 0.2,
            "duration_scale": 0.9,
            "articulation_auto": False,
            "seat": {"azimuth_deg": -12, "distance_m": 3.5},
            "role": {
                "function": "lead",
                "prominence": "foreground",
                "label": "主旋律",
            },
            "articulation_map": {"legato": "sustain"},
            "overrides": {
                "release_seconds": 0.25,
                "release_tail_gain": 0.8,
                "sample_variant": "SOLO",
            },
        },
        {"part": "打击", "instrument": INSTRUMENT_ID},
    ]
    return document


def _kit() -> dict:
    document = _assigned()
    document["assignments"][1] = {
        "part": "打击",
        "kit": {
            "D4": INSTRUMENT_ID,
            "E4": {"instrument": INSTRUMENT_ID, "transpose": -2},
        },
        "gain_db": -6,
    }
    return document


def _with_collaboration() -> dict:
    document = _assigned()
    first_part = document["assignments"][0]["part"]
    second_part = document["assignments"][1]["part"]
    document["collaboration"] = {
        "mode": "suggest",
        "analysis": {
            "metric": "overlap_active_rms",
            "window_ms": 400,
            "hop_ms": 100,
            "gate_dbfs": -60,
        },
        "part_groups": [
            {"id": "lead_group", "parts": [first_part]},
        ],
        "balance_relations": [
            {
                "subject": second_part,
                "reference": "lead_group",
                "target_offset_db": -6,
                "tolerance_db": 2,
                "max_suggestion_db": 4,
            }
        ],
    }
    return document


class AuthoringRosterContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(
            (ROOT / "schemas" / "authoring-roster.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def assert_contract_error(
        self,
        document: dict,
        code: str,
        location: tuple[str | int, ...],
    ) -> AuthoringRosterError:
        with self.assertRaises(AuthoringRosterError) as raised:
            parse_authoring_roster_document(document, _score())
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(raised.exception.location_segments, location)
        return raised.exception

    def test_blank_unassigned_document_is_savable_and_not_ready(self) -> None:
        document = _blank()
        self.validator.validate(document)
        parsed = parse_authoring_roster_document(document, _score())

        self.assertEqual(parsed.to_dict(), document)
        self.assertEqual(parsed.unassigned_parts, ("旋律", "打击"))
        self.assertFalse(parsed.ready)
        self.assertEqual(
            authoring_roster_readiness(parsed, _score()),
            {
                "kind": AUTHORING_ROSTER_READINESS_KIND,
                "schema_version": 1,
                "ready": False,
                "total_parts": 2,
                "assigned_parts": 0,
                "unassigned": [
                    {
                        "part": "旋律",
                        "location": ["assignments", 0, "instrument"],
                    },
                    {
                        "part": "打击",
                        "location": ["assignments", 1, "instrument"],
                    },
                ],
            },
        )

    def test_unassigned_conversion_is_a_structured_blocker(self) -> None:
        with self.assertRaises(AuthoringRosterError) as raised:
            to_formal_roster(_blank(), _score(), CAPABILITIES)
        error = raised.exception
        self.assertEqual(error.code, "authoring_roster.unassigned_part")
        self.assertEqual(
            error.location_segments, ("assignments", 0, "instrument")
        )
        self.assertEqual(error.details, {"parts": ["旋律", "打击"]})
        issue = error.to_issue()
        self.assertTrue(issue["blocking"])
        self.assertEqual(issue["scope"]["location"], ["assignments", 0, "instrument"])
        self.assertNotIn("\\", json.dumps(issue, ensure_ascii=False))

    def test_complete_assignment_round_trips_every_safe_parameter(self) -> None:
        document = _assigned()
        self.validator.validate(document)
        parsed = parse_authoring_roster_document(document, _score())
        readiness = authoring_roster_readiness(parsed, _score())
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["assigned_parts"], 2)
        self.assertEqual(readiness["unassigned"], [])

        formal = to_formal_roster(parsed, _score(), CAPABILITIES)
        self.assertNotIn("kind", formal)
        self.assertNotIn("schema_version", formal)
        self.assertEqual(formal["name"], "默认配器")
        self.assertEqual(formal["assignments"], document["assignments"])
        resolved = parse_roster_document(formal, CAPABILITIES)
        check_roster_covers_score(resolved, parse_score_document(_score()))
        self.assertEqual(len(resolved.executors), 2)
        self.assertEqual(resolved.executors[0].executor_id, "主奏")

    def test_collaboration_round_trips_without_materializing_defaults(
        self,
    ) -> None:
        document = _with_collaboration()
        self.validator.validate(document)

        parsed = parse_authoring_roster_document(document, _score())

        self.assertEqual(parsed.collaboration, document["collaboration"])
        self.assertEqual(parsed.to_dict(), document)

    def test_collaboration_unknown_fields_are_rejected(self) -> None:
        document = _with_collaboration()
        document["collaboration"]["balance_relations"][0][
            "auto_apply"
        ] = True

        self.assert_contract_error(
            document,
            "authoring_roster.unknown_field",
            ("collaboration", "balance_relations", 0, "auto_apply"),
        )
        self.assertTrue(list(self.validator.iter_errors(document)))

    def test_collaboration_relation_reaches_formal_conversion(self) -> None:
        document = _with_collaboration()
        formal = to_formal_roster(document, _score(), CAPABILITIES)

        self.assertEqual(formal["collaboration"], document["collaboration"])
        resolved = parse_roster_document(formal, CAPABILITIES)
        self.assertTrue(resolved.collaboration.declared)
        self.assertEqual(len(resolved.collaboration.balance_relations), 1)
        relation = resolved.collaboration.balance_relations[0]
        self.assertEqual(relation.subject, document["assignments"][1]["part"])
        self.assertEqual(relation.reference, "lead_group")
        self.assertEqual(relation.target_offset_db, -6.0)

    def test_collaboration_rejects_undeclared_relation_endpoint(self) -> None:
        document = _with_collaboration()
        document["collaboration"]["balance_relations"][0][
            "reference"
        ] = "missing_group"

        self.assert_contract_error(
            document,
            "authoring_roster.invalid_collaboration",
            ("collaboration",),
        )

    def test_non_empty_kit_route_converts_to_formal_roster(self) -> None:
        document = _kit()
        self.validator.validate(document)
        formal = to_formal_roster(document, _score(), CAPABILITIES)
        resolved = parse_roster_document(formal, CAPABILITIES)
        check_roster_covers_score(resolved, parse_score_document(_score()))
        kit_executors = resolved.executors_for("打击")
        self.assertEqual(len(kit_executors), 2)
        self.assertEqual(
            {executor.transpose for executor in kit_executors}, {-2, 0}
        )

    def test_route_is_exactly_one_of_null_string_or_non_empty_kit(self) -> None:
        missing = _blank()
        missing["assignments"][0].pop("instrument")
        self.assert_contract_error(
            missing,
            "authoring_roster.invalid_route",
            ("assignments", 0),
        )

        both = _blank()
        both["assignments"][0]["kit"] = {"C4": INSTRUMENT_ID}
        self.assert_contract_error(
            both,
            "authoring_roster.invalid_route",
            ("assignments", 0),
        )

        empty_kit = _blank()
        empty_kit["assignments"][0].pop("instrument")
        empty_kit["assignments"][0]["kit"] = {}
        self.assert_contract_error(
            empty_kit,
            "authoring_roster.invalid_kit",
            ("assignments", 0, "kit"),
        )

        for document in (missing, both, empty_kit):
            with self.subTest(document=document):
                self.assertTrue(list(self.validator.iter_errors(document)))

    def test_every_score_part_must_appear_exactly_once(self) -> None:
        duplicate = _blank()
        duplicate["assignments"][1]["part"] = "旋律"
        error = self.assert_contract_error(
            duplicate,
            "authoring_roster.duplicate_part",
            ("assignments", 1, "part"),
        )
        self.assertEqual(error.details["first_index"], 0)

        missing = _blank()
        missing["assignments"].pop()
        error = self.assert_contract_error(
            missing,
            "authoring_roster.missing_part",
            ("assignments",),
        )
        self.assertEqual(error.details, {"parts": ["打击"]})

        extra = _blank()
        extra["assignments"].append({"part": "幽灵", "instrument": None})
        self.assert_contract_error(
            extra,
            "authoring_roster.extra_part",
            ("assignments", 2, "part"),
        )

    def test_unknown_fields_are_rejected_at_every_object_layer(self) -> None:
        cases: list[tuple[dict, tuple[str | int, ...]]] = []

        top = _assigned()
        top["names"] = "typo"
        cases.append((top, ("names",)))

        assignment = _assigned()
        assignment["assignments"][0]["gian_db"] = -3
        cases.append((assignment, ("assignments", 0, "gian_db")))

        seat = _assigned()
        seat["assignments"][0]["seat"]["distance"] = 4
        cases.append((seat, ("assignments", 0, "seat", "distance")))

        point = _assigned()
        point["assignments"][0]["gain_automation"][0]["offset_dB"] = 1
        cases.append(
            (
                point,
                ("assignments", 0, "gain_automation", 0, "offset_dB"),
            )
        )

        kit = _kit()
        kit["assignments"][1]["kit"]["E4"]["transopse"] = 2
        cases.append((kit, ("assignments", 1, "kit", "E4", "transopse")))

        override = _assigned()
        override["assignments"][0]["overrides"]["asset_root"] = "C:/private"
        cases.append((override, ("assignments", 0, "overrides", "asset_root")))

        for document, location in cases:
            with self.subTest(location=location):
                self.assert_contract_error(
                    document,
                    "authoring_roster.unknown_field",
                    location,
                )
                self.assertTrue(list(self.validator.iter_errors(document)))

    def test_numbers_have_strict_types_finite_values_and_formal_bounds(self) -> None:
        cases = (
            ("gain_db", True, "authoring_roster.invalid_type"),
            ("gain_db", math.nan, "authoring_roster.nonfinite_number"),
            ("gain_db", -60.01, "authoring_roster.out_of_range"),
            ("gain_db", 12.01, "authoring_roster.out_of_range"),
            ("pan", 1.01, "authoring_roster.out_of_range"),
            ("dynamic_compression", -0.01, "authoring_roster.out_of_range"),
            ("duration_scale", 2.01, "authoring_roster.out_of_range"),
            ("transpose", 1.5, "authoring_roster.invalid_type"),
            ("articulation_auto", 1, "authoring_roster.invalid_type"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                document = _assigned()
                document["assignments"][0][field] = value
                self.assert_contract_error(
                    document,
                    code,
                    ("assignments", 0, field),
                )

        release = _assigned()
        release["assignments"][0]["overrides"]["release_seconds"] = -0.1
        self.assert_contract_error(
            release,
            "authoring_roster.out_of_range",
            ("assignments", 0, "overrides", "release_seconds"),
        )

    def test_document_limits_fail_before_unbounded_authoring_state_is_kept(self) -> None:
        document = _blank()
        document["name"] = "声" * (MAX_NAME_LENGTH + 1)
        error = self.assert_contract_error(
            document,
            "authoring_roster.limit_exceeded",
            ("name",),
        )
        self.assertEqual(error.details["maximum"], MAX_NAME_LENGTH)
        self.assertTrue(list(self.validator.iter_errors(document)))

    def test_gain_automation_start_order_and_nested_types_are_strict(self) -> None:
        late = _assigned()
        late["assignments"][0]["gain_automation"][0]["beat"] = 2
        self.assert_contract_error(
            late,
            "authoring_roster.invalid_automation_start",
            ("assignments", 0, "gain_automation", 0),
        )

        duplicate = _assigned()
        duplicate["assignments"][0]["gain_automation"][1].update(
            {"bar": 1, "beat": 1}
        )
        self.assert_contract_error(
            duplicate,
            "authoring_roster.invalid_automation_order",
            ("assignments", 0, "gain_automation", 1),
        )

        bool_bar = _assigned()
        bool_bar["assignments"][0]["gain_automation"][0]["bar"] = True
        self.assert_contract_error(
            bool_bar,
            "authoring_roster.invalid_type",
            ("assignments", 0, "gain_automation", 0, "bar"),
        )

    def test_portable_ids_allow_unicode_and_detect_normalized_collisions(self) -> None:
        unicode_document = _assigned()
        unicode_document["assignments"][0]["executor_id"] = "第一小提琴・独奏"
        parsed = parse_authoring_roster_document(unicode_document, _score())
        self.assertEqual(
            parsed.assignments[0].to_dict()["executor_id"],
            "第一小提琴・独奏",
        )

        collision = _assigned()
        collision["assignments"][0]["executor_id"] = "Café"
        collision["assignments"][1]["executor_id"] = "Cafe\u0301"
        self.assert_contract_error(
            collision,
            "authoring_roster.portable_id_conflict",
            ("assignments", 1, "executor_id"),
        )

        reserved = _assigned()
        reserved["assignments"][0]["executor_id"] = "CON.wav"
        self.assert_contract_error(
            reserved,
            "authoring_roster.invalid_portable_id",
            ("assignments", 0, "executor_id"),
        )

        for executor_id in ("CONIN$", "conout$.wav", "COM¹", "LPT³.wav"):
            with self.subTest(executor_id=executor_id):
                reserved = _assigned()
                reserved["assignments"][0]["executor_id"] = executor_id
                self.assert_contract_error(
                    reserved,
                    "authoring_roster.invalid_portable_id",
                    ("assignments", 0, "executor_id"),
                )

        too_many_utf8_bytes = _assigned()
        too_many_utf8_bytes["assignments"][0]["executor_id"] = "乐" * 84
        self.assert_contract_error(
            too_many_utf8_bytes,
            "authoring_roster.invalid_portable_id",
            ("assignments", 0, "executor_id"),
        )

    def test_instrument_reference_is_a_clean_catalog_relative_id(self) -> None:
        invalid = (
            "/世界乐器/测试琴",
            "\\\\server\\catalog",
            "C:/catalog/instrument",
            "世界乐器\\测试琴",
            "世界乐器//测试琴",
            "世界乐器/../测试琴",
            "./世界乐器/测试琴",
            "世界乐器/测试琴/",
            ".",
            "..",
        )
        for reference in invalid:
            with self.subTest(reference=reference):
                document = _assigned()
                document["assignments"][0]["instrument"] = reference
                self.assert_contract_error(
                    document,
                    "authoring_roster.invalid_instrument_reference",
                    ("assignments", 0, "instrument"),
                )
                self.assertTrue(list(self.validator.iter_errors(document)))

    def test_kit_notehead_has_a_255_character_schema_and_runtime_limit(self) -> None:
        document = _kit()
        notehead = "C" * 256
        document["assignments"][1]["kit"] = {notehead: INSTRUMENT_ID}
        self.assert_contract_error(
            document,
            "authoring_roster.invalid_kit_notehead",
            ("assignments", 1, "kit", notehead),
        )
        self.assertTrue(list(self.validator.iter_errors(document)))
        self.assertEqual(
            self.schema["$defs"]["kit"]["propertyNames"]["maxLength"],
            255,
        )

    def test_equivalent_kit_noteheads_cannot_overwrite_one_executor(self) -> None:
        document = _kit()
        document["assignments"][1]["kit"] = {
            "C4": INSTRUMENT_ID,
            "B#3": INSTRUMENT_ID,
        }
        self.assert_contract_error(
            document,
            "authoring_roster.duplicate_kit_pitch",
            ("assignments", 1, "kit", "B#3"),
        )

    def test_schema_and_runtime_close_all_declared_object_layers(self) -> None:
        self.assertFalse(self.schema["additionalProperties"])
        assignment = self.schema["$defs"]["assignment"]
        self.assertFalse(assignment["additionalProperties"])
        for name in ("gainAutomationPoint", "seat", "role", "overrides"):
            self.assertFalse(self.schema["$defs"][name]["additionalProperties"])
        kit_entry_object = self.schema["$defs"]["kitEntry"]["oneOf"][1]
        self.assertFalse(kit_entry_object["additionalProperties"])

    def test_formal_failure_does_not_expose_underlying_paths(self) -> None:
        document = _assigned()
        document["assignments"][0]["instrument"] = "不存在/秘密路径"
        with self.assertRaises(AuthoringRosterError) as raised:
            to_formal_roster(document, _score(), CAPABILITIES)
        error = raised.exception
        self.assertEqual(
            error.code, "authoring_roster.formal_validation_failed"
        )
        serialized = json.dumps(error.to_dict(), ensure_ascii=False)
        self.assertNotIn("不存在", serialized)
        self.assertNotIn("秘密路径", serialized)
        self.assertEqual(error.location_segments, ("assignments",))

    def test_parser_does_not_mutate_caller_documents(self) -> None:
        document = _assigned()
        original = copy.deepcopy(document)
        parsed = parse_authoring_roster_document(document, _score())
        parsed_copy = parsed.to_dict()
        parsed_copy["assignments"][0]["instrument"] = "changed"
        self.assertEqual(document, original)
        self.assertEqual(parsed.to_dict(), original)


if __name__ == "__main__":
    unittest.main()
