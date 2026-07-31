from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import unicodedata

from jsonschema import Draft202012Validator

from tianlai.collaboration_matrix import (
    COLLABORATION_MATRIX_FORMAT,
    COLLABORATION_MATRIX_NOTICE,
    COLLABORATION_MATRIX_VERSION,
    CollaborationMatrixError,
    build_collaboration_matrix,
    canonical_json_bytes,
    load_collaboration_matrix,
    validate_collaboration_matrix,
    write_collaboration_matrix_atomic,
)


VIOLIN = "乐器/弦乐/Violin/乐器.json"
CELLO = "乐器/弦乐/Cello/乐器.json"
PIANO = "乐器/键盘/Piano/乐器.json"


def _hash(character: str) -> str:
    return character * 64


def _role(
    function: str,
    prominence: str,
    label: str,
) -> dict:
    return {
        "function": function,
        "prominence": prominence,
        "label": label,
    }


def _raw_entries() -> tuple[list[dict], list[dict]]:
    violin_role = _role("lead", "foreground", "主旋律")
    cello_role = _role("bass", "background", "低音支撑")
    piano_role = _role("harmony", "midground", "和声")
    fixture_a = {
        "fixture_id": "typical-balance-v1",
        "profile_version": 1,
        "score_sha256": _hash("1"),
        "roster_sha256": _hash("2"),
        "space_sha256": None,
        "receipt_sha256": _hash("3"),
        "instruments": [CELLO, VIOLIN],
        "roles": [
            {"instrument_path": CELLO, "role": copy.deepcopy(cello_role)},
            {"instrument_path": VIOLIN, "role": copy.deepcopy(violin_role)},
        ],
        "assertions": [
            {
                "code": "balance-window",
                "instrument_paths": [VIOLIN, CELLO],
                "status": "pass",
                "observed": {
                    "relation_db": -3.2,
                    "windows": [0.5, 0.75, 1.0],
                },
                "expected": {"maximum_db": 0.0},
                "tolerance": 0.5,
                "unit": "dB",
                "evidence_path": "evidence/typical/balance.json",
            }
        ],
        "candidates": [
            {
                "code": "cello-mask-risk",
                "instrument_paths": [CELLO],
                "severity": "warning",
                "info": "低音密集处需要人工确认清晰度",
                "evidence_path": "evidence/typical/cello-mask.json",
            }
        ],
        "human_checks": [
            {
                "code": "typical-violin-listen",
                "instrument_paths": [VIOLIN],
                "status": "pass",
                "evidence_path": "reviews/typical/violin.json",
            },
            {
                "code": "typical-cello-listen",
                "instrument_paths": [CELLO],
                "status": "pending",
                "evidence_path": None,
            },
        ],
    }
    fixture_b = {
        "fixture_id": "stress-tail-v1",
        "profile_version": 1,
        "score_sha256": _hash("4"),
        "roster_sha256": _hash("5"),
        "space_sha256": _hash("6"),
        "receipt_sha256": _hash("a"),
        "instruments": [VIOLIN],
        "roles": [
            {"instrument_path": VIOLIN, "role": copy.deepcopy(violin_role)}
        ],
        "assertions": [
            {
                "code": "tail-separation",
                "instrument_paths": [VIOLIN],
                "status": "inconclusive",
                "observed": None,
                "expected": -30.0,
                "tolerance": None,
                "unit": "dBFS",
                "evidence_path": "evidence/stress/tail.json",
            }
        ],
        "candidates": [
            {
                "code": "violin-tail-risk",
                "instrument_paths": [VIOLIN],
                "severity": "info",
                "info": "空间尾音可能遮住下一句",
                "evidence_path": "evidence/stress/tail-candidate.json",
            }
        ],
        "human_checks": [
            {
                "code": "stress-violin-listen",
                "instrument_paths": [VIOLIN],
                "status": "pass",
                "evidence_path": "reviews/stress/violin.json",
            }
        ],
    }
    instruments = [
        {
            "instrument_path": PIANO,
            "manifest_sha256": _hash("d"),
            "probe_profile_id": "piano-baseline-v1",
            "role": piano_role,
            "solo_formal": False,
            "fixture_ids": [],
            "receipt_sha256": [],
            "hard_status": "not_covered",
            "candidate_codes": [],
            "human_status": "pending",
        },
        {
            "instrument_path": VIOLIN,
            "manifest_sha256": _hash("b"),
            "probe_profile_id": "violin-baseline-v1",
            "role": violin_role,
            "solo_formal": True,
            "fixture_ids": ["stress-tail-v1", "typical-balance-v1"],
            "receipt_sha256": [_hash("a"), _hash("3")],
            "hard_status": "inconclusive",
            "candidate_codes": ["violin-tail-risk"],
            "human_status": "pass",
        },
        {
            "instrument_path": CELLO,
            "manifest_sha256": _hash("c"),
            "probe_profile_id": "cello-baseline-v1",
            "role": cello_role,
            "solo_formal": True,
            "fixture_ids": ["typical-balance-v1"],
            "receipt_sha256": [_hash("3")],
            "hard_status": "machine_complete",
            "candidate_codes": ["cello-mask-risk"],
            "human_status": "pending",
        },
    ]
    return [fixture_b, fixture_a], instruments


def _build() -> dict:
    fixtures, instruments = _raw_entries()
    return build_collaboration_matrix(
        generated_from="v0.5-synthetic-contract",
        fixtures=fixtures,
        instruments=instruments,
    )


class CollaborationMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "collaboration-matrix.schema.json"
        )
        cls.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(cls.schema)

    def assertInvalid(
        self,
        document: dict,
        message: str | None = None,
    ) -> None:
        with self.assertRaises(CollaborationMatrixError) as raised:
            validate_collaboration_matrix(document)
        if message is not None:
            self.assertIn(message, str(raised.exception))

    def assertSchemaInvalid(self, document: dict) -> None:
        self.assertTrue(
            list(self.schema_validator.iter_errors(document)),
            "schema unexpectedly accepted an invalid contract document",
        )

    def test_builds_multi_fixture_matrix_with_derived_coverage(self) -> None:
        matrix = _build()

        self.assertEqual(matrix["format"], COLLABORATION_MATRIX_FORMAT)
        self.assertEqual(matrix["version"], COLLABORATION_MATRIX_VERSION)
        self.assertEqual(matrix["notice"], COLLABORATION_MATRIX_NOTICE)
        self.assertEqual(
            matrix["coverage"],
            {
                "registered": 3,
                "solo_formal": 2,
                "machine_fixture_covered": 2,
                "human_context_reviewed": 1,
            },
        )
        self.assertEqual(
            [fixture["fixture_id"] for fixture in matrix["fixtures"]],
            ["stress-tail-v1", "typical-balance-v1"],
        )
        self.assertEqual(
            [item["instrument_path"] for item in matrix["instruments"]],
            sorted([CELLO, PIANO, VIOLIN]),
        )
        violin = next(
            item
            for item in matrix["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        self.assertEqual(
            violin["fixture_ids"],
            ["stress-tail-v1", "typical-balance-v1"],
        )
        self.assertEqual(
            violin["receipt_sha256"],
            [_hash("a"), _hash("3")],
        )
        self.assertEqual(violin["hard_status"], "inconclusive")
        self.assertEqual(violin["human_status"], "pass")
        self.assertNotIn("generated_at", matrix)
        self.assertNotIn("timestamp", canonical_json_bytes(matrix).decode("utf-8"))
        self.assertFalse(list(self.schema_validator.iter_errors(matrix)))

    def test_build_is_deterministic_and_does_not_mutate_inputs(self) -> None:
        fixtures, instruments = _raw_entries()
        original_fixtures = copy.deepcopy(fixtures)
        original_instruments = copy.deepcopy(instruments)
        first = build_collaboration_matrix(
            generated_from="v0.5-synthetic-contract",
            fixtures=fixtures,
            instruments=instruments,
        )
        self.assertEqual(fixtures, original_fixtures)
        self.assertEqual(instruments, original_instruments)

        shuffled_fixtures = copy.deepcopy(fixtures)
        shuffled_instruments = copy.deepcopy(instruments)
        shuffled_fixtures.reverse()
        shuffled_instruments.reverse()
        for fixture in shuffled_fixtures:
            for key in (
                "instruments",
                "roles",
                "assertions",
                "candidates",
                "human_checks",
            ):
                fixture[key].reverse()
            for key in ("assertions", "candidates", "human_checks"):
                for item in fixture[key]:
                    item["instrument_paths"].reverse()
        for instrument in shuffled_instruments:
            pairs = list(
                zip(
                    instrument["fixture_ids"],
                    instrument["receipt_sha256"],
                    strict=True,
                )
            )
            pairs.reverse()
            instrument["fixture_ids"] = [item[0] for item in pairs]
            instrument["receipt_sha256"] = [item[1] for item in pairs]
            instrument["candidate_codes"].reverse()
        second = build_collaboration_matrix(
            generated_from="v0.5-synthetic-contract",
            fixtures=shuffled_fixtures,
            instruments=shuffled_instruments,
        )

        self.assertEqual(first, second)
        self.assertEqual(canonical_json_bytes(first), canonical_json_bytes(second))

    def test_unknown_fields_are_rejected_at_every_contract_layer(self) -> None:
        mutations = [
            lambda item: item.__setitem__("generated_at", "forbidden"),
            lambda item: item["coverage"].__setitem__("other", 0),
            lambda item: item["fixtures"][0].__setitem__("other", 0),
            lambda item: item["fixtures"][0]["roles"][0].__setitem__("other", 0),
            lambda item: item["fixtures"][0]["roles"][0]["role"].__setitem__(
                "other", 0
            ),
            lambda item: item["fixtures"][0]["assertions"][0].__setitem__(
                "other", 0
            ),
            lambda item: item["fixtures"][0]["candidates"][0].__setitem__(
                "other", 0
            ),
            lambda item: item["fixtures"][0]["human_checks"][0].__setitem__(
                "other", 0
            ),
            lambda item: item["instruments"][0].__setitem__("other", 0),
            lambda item: item["instruments"][0]["role"].__setitem__("other", 0),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(layer=index):
                document = _build()
                mutate(document)
                self.assertInvalid(document, "unknown fields")
                self.assertSchemaInvalid(document)

    def test_missing_required_fields_are_rejected_at_every_layer(self) -> None:
        matrix = _build()
        object_paths = [
            (),
            ("coverage",),
            ("fixtures", 0),
            ("fixtures", 0, "roles", 0),
            ("fixtures", 0, "roles", 0, "role"),
            ("fixtures", 0, "assertions", 0),
            ("fixtures", 0, "candidates", 0),
            ("fixtures", 0, "human_checks", 0),
            ("instruments", 0),
            ("instruments", 0, "role"),
        ]
        for object_path in object_paths:
            target = matrix
            for part in object_path:
                target = target[part]
            for key in tuple(target):
                if key == "label":
                    continue
                with self.subTest(path=object_path, key=key):
                    document = copy.deepcopy(matrix)
                    mutated = document
                    for part in object_path:
                        mutated = mutated[part]
                    del mutated[key]
                    self.assertInvalid(document)
                    self.assertSchemaInvalid(document)

    def test_bool_as_integer_and_nonfinite_numbers_are_rejected(self) -> None:
        for mutate in (
            lambda item: item.__setitem__("version", True),
            lambda item: item["coverage"].__setitem__("registered", True),
            lambda item: item["fixtures"][0].__setitem__("profile_version", True),
            lambda item: item["fixtures"][0]["assertions"][0].__setitem__(
                "observed", {"nested": [1.0, math.nan]}
            ),
            lambda item: item["fixtures"][0]["assertions"][0].__setitem__(
                "expected", math.inf
            ),
            lambda item: item["fixtures"][0]["assertions"][0].__setitem__(
                "tolerance", -math.inf
            ),
        ):
            document = _build()
            mutate(document)
            self.assertInvalid(document)

        large_but_json_safe_integer = 10**1000
        document = _build()
        typical = next(
            item
            for item in document["fixtures"]
            if item["fixture_id"] == "typical-balance-v1"
        )
        assertion = typical["assertions"][0]
        assertion["observed"] = large_but_json_safe_integer
        assertion["expected"] = {"upper": large_but_json_safe_integer}
        assertion["tolerance"] = large_but_json_safe_integer
        validate_collaboration_matrix(document)

    def test_assertion_status_cannot_claim_pass_without_observation(self) -> None:
        pass_without_observation = _build()
        stress_assertion = pass_without_observation["fixtures"][0]["assertions"][0]
        stress_assertion["status"] = "pass"
        violin = next(
            item
            for item in pass_without_observation["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["hard_status"] = "machine_complete"
        self.assertInvalid(pass_without_observation, "must not be null")
        self.assertSchemaInvalid(pass_without_observation)

        inconclusive_with_observation = _build()
        typical = next(
            item
            for item in inconclusive_with_observation["fixtures"]
            if item["fixture_id"] == "typical-balance-v1"
        )
        typical["assertions"][0]["status"] = "inconclusive"
        self.assertInvalid(inconclusive_with_observation, "must be null")
        self.assertSchemaInvalid(inconclusive_with_observation)

    def test_build_rejects_non_object_containers_and_entries_cleanly(self) -> None:
        valid_fixtures, valid_instruments = _raw_entries()
        cases = (
            ([], []),
            (None, valid_instruments),
            ("not-an-array", valid_instruments),
            ([None], valid_instruments),
            (valid_fixtures, None),
            (valid_fixtures, "not-an-array"),
            (valid_fixtures, [None]),
        )
        for fixtures, instruments in cases:
            with self.subTest(
                fixtures_type=type(fixtures).__name__,
                instruments_type=type(instruments).__name__,
            ):
                with self.assertRaises(CollaborationMatrixError):
                    build_collaboration_matrix(
                        generated_from="v0.5-synthetic-contract",
                        fixtures=fixtures,
                        instruments=instruments,
                    )

        fixtures, instruments = _raw_entries()
        instruments[0]["human_status"] = []
        with self.assertRaises(CollaborationMatrixError):
            build_collaboration_matrix(
                generated_from="v0.5-synthetic-contract",
                fixtures=fixtures,
                instruments=instruments,
            )

    def test_hash_format_is_strict_for_all_hash_locations(self) -> None:
        mutations = [
            lambda item, value: item["fixtures"][0].__setitem__(
                "score_sha256", value
            ),
            lambda item, value: item["fixtures"][0].__setitem__(
                "roster_sha256", value
            ),
            lambda item, value: item["fixtures"][0].__setitem__(
                "space_sha256", value
            ),
            lambda item, value: item["fixtures"][0].__setitem__(
                "receipt_sha256", value
            ),
            lambda item, value: item["instruments"][0].__setitem__(
                "manifest_sha256", value
            ),
            lambda item, value: item["instruments"][0]["receipt_sha256"].__setitem__(
                0, value
            ),
        ]
        for bad_hash in (
            "a" * 63,
            "a" * 65,
            "A" * 64,
            f" {_hash('a')}",
            f"{_hash('a')} ",
        ):
            for index, mutate in enumerate(mutations):
                with self.subTest(hash=bad_hash[:8], location=index):
                    document = _build()
                    mutate(document, bad_hash)
                    self.assertInvalid(document, "SHA-256")

        document = _build()
        document["fixtures"][0]["space_sha256"] = None
        validate_collaboration_matrix(document)

    def test_duplicate_ids_paths_and_codes_are_rejected(self) -> None:
        cases = []

        duplicate_fixture = _build()
        duplicate_fixture["fixtures"].append(
            copy.deepcopy(duplicate_fixture["fixtures"][0])
        )
        cases.append(duplicate_fixture)

        duplicate_instrument = _build()
        duplicate_instrument["instruments"].append(
            copy.deepcopy(duplicate_instrument["instruments"][0])
        )
        cases.append(duplicate_instrument)

        duplicate_fixture_path = _build()
        duplicate_fixture_path["fixtures"][0]["instruments"].append(VIOLIN)
        cases.append(duplicate_fixture_path)

        duplicate_role_path = _build()
        duplicate_role_path["fixtures"][0]["roles"].append(
            copy.deepcopy(duplicate_role_path["fixtures"][0]["roles"][0])
        )
        cases.append(duplicate_role_path)

        for collection in ("assertions", "candidates", "human_checks"):
            duplicate_code = _build()
            duplicate_code["fixtures"][0][collection].append(
                copy.deepcopy(duplicate_code["fixtures"][0][collection][0])
            )
            cases.append(duplicate_code)

        duplicate_fixture_ref = _build()
        violin = next(
            item
            for item in duplicate_fixture_ref["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["fixture_ids"].append(violin["fixture_ids"][0])
        violin["receipt_sha256"].append(violin["receipt_sha256"][0])
        cases.append(duplicate_fixture_ref)

        duplicate_candidate_code = _build()
        cello = next(
            item
            for item in duplicate_candidate_code["instruments"]
            if item["instrument_path"] == CELLO
        )
        cello["candidate_codes"].append(cello["candidate_codes"][0])
        cases.append(duplicate_candidate_code)

        duplicate_attribution = _build()
        paths = duplicate_attribution["fixtures"][0]["assertions"][0][
            "instrument_paths"
        ]
        paths.append(paths[0])
        cases.append(duplicate_attribution)

        for index, document in enumerate(cases):
            with self.subTest(case=index):
                self.assertInvalid(document, "duplicate")

    def test_paths_are_canonical_nfc_and_windows_collision_safe(self) -> None:
        schema_representable_bad_paths = (
            "a//b.json",
            "a/./b.json",
            "a/../b.json",
            "/absolute/b.json",
            "C:/absolute/b.json",
            "a\\b.json",
            "a/b.json/",
            "a/CON.json",
            "a/CoM1.txt",
            "a/CON .txt",
            "a/control\nname.json",
        )
        for bad_path in schema_representable_bad_paths:
            document = _build()
            document["instruments"][0]["instrument_path"] = bad_path
            with self.subTest(path=bad_path):
                self.assertInvalid(document)
                self.assertSchemaInvalid(document)

        nfd_path = unicodedata.normalize(
            "NFD",
            "乐器/弦乐/Café/乐器.json",
        )
        document = _build()
        document["instruments"][0]["instrument_path"] = nfd_path
        self.assertInvalid(document)

        document = _build()
        duplicate = copy.deepcopy(document["instruments"][0])
        duplicate["instrument_path"] = duplicate["instrument_path"].casefold()
        document["instruments"].append(duplicate)
        self.assertInvalid(document, "duplicate-equivalent paths")

    def test_meaningful_strings_and_paths_match_schema_runtime_rules(self) -> None:
        mutations = (
            lambda item: item["fixtures"][0]["roles"][0]["role"].__setitem__(
                "label", "   "
            ),
            lambda item: item["fixtures"][0]["assertions"][0].__setitem__(
                "unit", "   "
            ),
            lambda item: item["fixtures"][0]["candidates"][0].__setitem__(
                "info", "   "
            ),
            lambda item: item["fixtures"][0]["assertions"][0].__setitem__(
                "evidence_path", " evidence/a.json"
            ),
        )
        for index, mutate in enumerate(mutations):
            document = _build()
            mutate(document)
            with self.subTest(case=index):
                self.assertInvalid(document)
                self.assertSchemaInvalid(document)

    def test_cross_references_receipts_roles_and_candidates_are_bound(self) -> None:
        unknown_fixture = _build()
        violin = next(
            item
            for item in unknown_fixture["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["fixture_ids"].append("missing-fixture")
        violin["receipt_sha256"].append(_hash("f"))
        self.assertInvalid(unknown_fixture, "unknown fixture")

        missing_backlink = _build()
        violin = next(
            item
            for item in missing_backlink["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["fixture_ids"].remove("stress-tail-v1")
        violin["receipt_sha256"].remove(_hash("a"))
        self.assertInvalid(missing_backlink, "does not link back")

        bad_receipt = _build()
        violin = next(
            item
            for item in bad_receipt["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["receipt_sha256"].reverse()
        self.assertInvalid(bad_receipt, "match fixture_ids in order")

        bad_role = _build()
        violin = next(
            item
            for item in bad_role["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["role"]["prominence"] = "midground"
        self.assertInvalid(bad_role, "must match the instrument role")

        bad_candidates = _build()
        cello = next(
            item
            for item in bad_candidates["instruments"]
            if item["instrument_path"] == CELLO
        )
        cello["candidate_codes"] = []
        self.assertInvalid(bad_candidates, "candidate_codes")

        missing_role = _build()
        missing_role["fixtures"][0]["roles"] = []
        self.assertInvalid(missing_role, "cover exactly")

    def test_fixture_unknown_instrument_and_attribution_subset_are_rejected(
        self,
    ) -> None:
        unknown = _build()
        fixture = unknown["fixtures"][0]
        fixture["instruments"][0] = "乐器/弦乐/Missing/乐器.json"
        fixture["roles"][0]["instrument_path"] = fixture["instruments"][0]
        for collection in ("assertions", "candidates", "human_checks"):
            for item in fixture[collection]:
                item["instrument_paths"] = [fixture["instruments"][0]]
        self.assertInvalid(unknown, "unknown instrument path")

        outside = _build()
        outside["fixtures"][0]["candidates"][0]["instrument_paths"] = [CELLO]
        self.assertInvalid(outside, "subset of fixture instruments")

        uncovered = _build()
        fixture = next(
            item
            for item in uncovered["fixtures"]
            if item["fixture_id"] == "typical-balance-v1"
        )
        fixture["assertions"][0]["instrument_paths"] = [VIOLIN]
        self.assertInvalid(uncovered, "do not cover fixture instruments")

    def test_assertions_candidates_and_human_checks_do_not_cross_authority(
        self,
    ) -> None:
        document = _build()
        fixture = next(
            item
            for item in document["fixtures"]
            if item["fixture_id"] == "typical-balance-v1"
        )
        fixture["assertions"][0]["instrument_paths"] = [VIOLIN]
        fixture["assertions"][0]["status"] = "fail"
        fixture["assertions"].append(
            {
                "code": "cello-only-pass",
                "instrument_paths": [CELLO],
                "status": "pass",
                "observed": -6.0,
                "expected": -6.0,
                "tolerance": 1.0,
                "unit": "dB",
                "evidence_path": "evidence/typical/cello-only.json",
            }
        )
        violin = next(
            item
            for item in document["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["hard_status"] = "machine_failed"
        cello = next(
            item
            for item in document["instruments"]
            if item["instrument_path"] == CELLO
        )

        validated = validate_collaboration_matrix(document)
        validated_cello = next(
            item
            for item in validated["instruments"]
            if item["instrument_path"] == CELLO
        )
        self.assertEqual(validated_cello["hard_status"], "machine_complete")
        self.assertEqual(cello["candidate_codes"], ["cello-mask-risk"])
        self.assertEqual(violin["candidate_codes"], ["violin-tail-risk"])
        self.assertEqual(violin["human_status"], "pass")

        # A warning candidate remains advisory and cannot turn a passing hard
        # assertion into machine_failed.
        self.assertEqual(validated_cello["hard_status"], "machine_complete")

    def test_hard_and_human_statuses_are_evidence_derived(self) -> None:
        for status in (
            "not_covered",
            "machine_complete",
            "machine_failed",
        ):
            document = _build()
            violin = next(
                item
                for item in document["instruments"]
                if item["instrument_path"] == VIOLIN
            )
            violin["hard_status"] = status
            with self.subTest(hard_status=status):
                self.assertInvalid(document, "hard_status")

        no_checks = _build()
        stress = next(
            item
            for item in no_checks["fixtures"]
            if item["fixture_id"] == "stress-tail-v1"
        )
        stress["human_checks"] = []
        violin = next(
            item
            for item in no_checks["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["human_status"] = "pending"
        no_checks["coverage"]["human_context_reviewed"] = 0
        validate_collaboration_matrix(no_checks)

        pending = _build()
        stress = next(
            item
            for item in pending["fixtures"]
            if item["fixture_id"] == "stress-tail-v1"
        )
        stress["human_checks"][0]["status"] = "pending"
        stress["human_checks"][0]["evidence_path"] = None
        violin = next(
            item
            for item in pending["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["human_status"] = "pending"
        pending["coverage"]["human_context_reviewed"] = 0
        validate_collaboration_matrix(pending)

        rejected = _build()
        for fixture in rejected["fixtures"]:
            for check in fixture["human_checks"]:
                if VIOLIN in check["instrument_paths"]:
                    check["status"] = "reject"
        violin = next(
            item
            for item in rejected["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["human_status"] = "reject"
        validate_collaboration_matrix(rejected)

        conflict = _build()
        stress = next(
            item
            for item in conflict["fixtures"]
            if item["fixture_id"] == "stress-tail-v1"
        )
        stress["human_checks"][0]["status"] = "reject"
        violin = next(
            item
            for item in conflict["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["human_status"] = "conflict"
        validate_collaboration_matrix(conflict)

        reject_beats_pending = _build()
        for fixture in reject_beats_pending["fixtures"]:
            for check in fixture["human_checks"]:
                if VIOLIN not in check["instrument_paths"]:
                    continue
                if fixture["fixture_id"] == "stress-tail-v1":
                    check["status"] = "reject"
                else:
                    check["status"] = "pending"
                    check["evidence_path"] = None
        violin = next(
            item
            for item in reject_beats_pending["instruments"]
            if item["instrument_path"] == VIOLIN
        )
        violin["human_status"] = "reject"
        validate_collaboration_matrix(reject_beats_pending)

        forged = _build()
        piano = next(
            item
            for item in forged["instruments"]
            if item["instrument_path"] == PIANO
        )
        piano["human_status"] = "pass"
        forged["coverage"]["human_context_reviewed"] += 1
        self.assertInvalid(forged, "human_status")

    def test_every_coverage_count_is_recomputed_from_instrument_detail(self) -> None:
        for field in (
            "registered",
            "solo_formal",
            "machine_fixture_covered",
            "human_context_reviewed",
        ):
            document = _build()
            document["coverage"][field] += 1
            with self.subTest(field=field):
                self.assertInvalid(document, "coverage does not match")

    def test_generated_from_and_notice_cannot_be_timestamps_or_weakened(
        self,
    ) -> None:
        timestamp = _build()
        timestamp["generated_from"] = "2026-07-27T12:34:56Z"
        self.assertInvalid(timestamp, "not a timestamp")
        self.assertSchemaInvalid(timestamp)

        timestamp_prefix = _build()
        timestamp_prefix["generated_from"] = "2026-07-27T12:34:56Z-run"
        self.assertInvalid(timestamp_prefix, "not a timestamp")
        self.assertSchemaInvalid(timestamp_prefix)

        changed_notice = _build()
        changed_notice["notice"] = "machine_complete 表示协奏通过"
        self.assertInvalid(changed_notice, "fixed safety notice")
        self.assertSchemaInvalid(changed_notice)

    def test_load_is_duplicate_safe_and_rejects_nonfinite_json(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tianlai_matrix_load_"
        ) as temporary:
            root = Path(temporary)
            duplicate = root / "duplicate.json"
            duplicate.write_text(
                '{"format":"a","format":"b"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                CollaborationMatrixError,
                "duplicate JSON key",
            ):
                load_collaboration_matrix(duplicate)

            for index, payload in enumerate(
                (
                    '{"value":NaN}',
                    '{"value":Infinity}',
                    '{"value":-Infinity}',
                    '{"value":1e999}',
                )
            ):
                path = root / f"nonfinite-{index}.json"
                path.write_text(payload, encoding="utf-8")
                with self.subTest(payload=payload):
                    with self.assertRaises(CollaborationMatrixError):
                        load_collaboration_matrix(path)

            array_root = root / "array.json"
            array_root.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(
                CollaborationMatrixError,
                "root must be an object",
            ):
                load_collaboration_matrix(array_root)

            huge_integer = root / "huge-integer.json"
            huge_integer.write_text(
                '{"value":' + ("9" * 5000) + "}",
                encoding="utf-8",
            )
            with self.assertRaises(CollaborationMatrixError):
                load_collaboration_matrix(huge_integer)

    def test_atomic_write_round_trip_and_replace_failure_safety(self) -> None:
        matrix = _build()
        with tempfile.TemporaryDirectory(
            prefix="tianlai_matrix_write_"
        ) as temporary:
            root = Path(temporary)
            target = root / "nested" / "matrix.json"
            write_collaboration_matrix_atomic(target, matrix)
            self.assertEqual(load_collaboration_matrix(target), matrix)
            self.assertTrue(target.read_bytes().endswith(b"\n"))

            previous = target.read_bytes()
            with mock.patch(
                "tianlai.collaboration_matrix.os.replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(OSError):
                    write_collaboration_matrix_atomic(target, matrix)
            self.assertEqual(target.read_bytes(), previous)
            self.assertFalse(
                list(target.parent.glob(f".{target.name}.*.tmp"))
            )

    def test_invalid_atomic_write_does_not_create_parent_directory(self) -> None:
        document = _build()
        document["coverage"]["registered"] += 1
        with tempfile.TemporaryDirectory(
            prefix="tianlai_matrix_invalid_write_"
        ) as temporary:
            target = Path(temporary) / "absent" / "matrix.json"
            with self.assertRaises(CollaborationMatrixError):
                write_collaboration_matrix_atomic(target, document)
            self.assertFalse(target.parent.exists())

    def test_build_validate_and_explicit_write_never_touch_manifests(self) -> None:
        matrix = _build()
        with tempfile.TemporaryDirectory(
            prefix="tianlai_matrix_manifest_"
        ) as temporary:
            root = Path(temporary)
            manifest = root / "乐器" / "弦乐" / "Violin" / "乐器.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_bytes(b'{"collaboration_review_status":"untested"}\n')
            before = manifest.read_bytes()

            validate_collaboration_matrix(matrix)
            output = root / "artifacts" / "collaboration-matrix.json"
            write_collaboration_matrix_atomic(output, matrix)

            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(
                sorted(path.relative_to(root).as_posix() for path in root.rglob("*")),
                [
                    "artifacts",
                    "artifacts/collaboration-matrix.json",
                    "乐器",
                    "乐器/弦乐",
                    "乐器/弦乐/Violin",
                    "乐器/弦乐/Violin/乐器.json",
                ],
            )


if __name__ == "__main__":
    unittest.main()
