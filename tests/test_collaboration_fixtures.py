from __future__ import annotations

import json
import math
from pathlib import Path
import unittest

from tianlai.capability import load_capabilities
from tianlai.collaboration_fixtures import (
    all_fixture_documents,
    build_fixture_documents,
    fixture_ids,
)
from tianlai.conductor import ExpressionSettings, build_plan
from tianlai.roster import parse_roster_document
from tianlai.score import parse_pitch, parse_score_document
from tianlai.space import SpaceConfig


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_IDS = (
    "01-cello-background-typical-v1",
    "01-cello-background-stress-v1",
    "02-sitar-visibility-typical-v1",
    "02-sitar-visibility-stress-v1",
    "03-orchestral-depth-typical-v1",
    "03-orchestral-depth-stress-v1",
    "04-modern-rhythm-typical-v1",
    "04-modern-rhythm-stress-v1",
    "05-atmosphere-tail-typical-v1",
    "05-atmosphere-tail-stress-v1",
    "06-lead-handoff-typical-v1",
    "06-lead-handoff-stress-v1",
)

EXPECTED_ROLES = {
    "键盘乐器/钢琴": ("lead", "foreground"),
    "管弦乐/弦乐组/大提琴": ("pad", "background"),
    "低音乐器/原声贝斯": ("bass", "midground"),
    "世界乐器/西塔琴": ("lead", "foreground"),
    "弹拨乐器/尼龙弦吉他": ("harmony", "midground"),
    "管弦乐/打击乐组/太鼓": ("accent", "midground"),
    "管弦乐/木管组/长笛": ("lead", "foreground"),
    "管弦乐/弦乐组/小提琴": ("countermelody", "midground"),
    "管弦乐/弦乐组/弦乐合奏": ("pad", "background"),
    "管弦乐/铜管组/圆号": ("harmony", "background"),
    "弹拨乐器/清音电吉他": ("lead", "foreground"),
    "低音乐器/指弹电贝斯": ("bass", "midground"),
    "电子乐器/温暖铺底": ("pad", "background"),
    "现代鼓组/底鼓": ("rhythm", "midground"),
    "现代鼓组/边击军鼓": ("rhythm", "midground"),
    "现代鼓组/闭合踩镲": ("rhythm", "midground"),
    "现代鼓组/强音镲": ("accent", "midground"),
    "键盘乐器/电钢琴": ("lead", "foreground"),
    "环境与拟音/雨境合成氛围": ("ambience", "background"),
    "管弦乐/打击乐组/反向镲": ("effect", "midground"),
}

UNPITCHED_TRIGGER_KEYS = {
    "管弦乐/打击乐组/太鼓": {60, 61, 62},
    "现代鼓组/底鼓": {60},
    "现代鼓组/边击军鼓": {62},
    "现代鼓组/闭合踩镲": {42},
    "现代鼓组/强音镲": {66},
    "环境与拟音/雨境合成氛围": {60},
    "管弦乐/打击乐组/反向镲": {60, 62},
}


class CollaborationFixtureCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")
        cls.documents = all_fixture_documents()
        cls.parsed = []
        cls.plans = []
        for document in cls.documents:
            score = parse_score_document(document["score"])
            roster = parse_roster_document(
                document["roster"],
                cls.capabilities,
            )
            expression = ExpressionSettings.from_dict(
                {
                    "mode": "ensemble",
                    "humanize": {"seed": document["seed"]},
                }
            )
            plan = build_plan(score, roster, expression)
            cls.parsed.append((score, roster))
            cls.plans.append(plan)

    def assertStrictJsonValue(self, value: object, path: str = "$") -> None:
        if value is None or type(value) in (str, bool, int):
            return
        if type(value) is float:
            self.assertTrue(math.isfinite(value), path)
            return
        if type(value) is list:
            for index, item in enumerate(value):
                self.assertStrictJsonValue(item, f"{path}[{index}]")
            return
        if type(value) is dict:
            for key, item in value.items():
                self.assertIs(type(key), str, path)
                self.assertStrictJsonValue(item, f"{path}.{key}")
            return
        self.fail(f"{path} is not a strict JSON value: {type(value).__name__}")

    def test_ids_and_all_documents_have_stable_family_major_order(self) -> None:
        self.assertEqual(fixture_ids(), EXPECTED_IDS)
        self.assertIs(type(fixture_ids()), tuple)
        self.assertIs(type(self.documents), tuple)
        self.assertEqual(
            tuple(document["fixture_id"] for document in self.documents),
            EXPECTED_IDS,
        )
        self.assertEqual(len(set(EXPECTED_IDS)), 12)

    def test_public_documents_are_strict_json_and_deterministic(self) -> None:
        required = {
            "fixture_id",
            "family",
            "variant",
            "seed",
            "space",
            "master_gain_db",
            "normalize_peak_db",
            "score",
            "roster",
            "targets",
            "human_questions",
        }
        for fixture_id, document in zip(
            EXPECTED_IDS,
            self.documents,
            strict=True,
        ):
            with self.subTest(fixture_id=fixture_id):
                self.assertTrue(required <= set(document))
                self.assertStrictJsonValue(document)
                first = json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                second = json.dumps(
                    build_fixture_documents(fixture_id),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                self.assertEqual(first, second)

    def test_build_returns_independent_trees_and_rejects_unknown_ids(self) -> None:
        fixture_id = EXPECTED_IDS[0]
        changed = build_fixture_documents(fixture_id)
        changed["score"]["parts"][0]["notes"][0]["pitch"] = "C-1"
        changed["targets"].clear()

        rebuilt = build_fixture_documents(fixture_id)
        self.assertNotEqual(
            rebuilt["score"]["parts"][0]["notes"][0]["pitch"],
            "C-1",
        )
        self.assertTrue(rebuilt["targets"])
        with self.assertRaises(KeyError):
            build_fixture_documents("missing-fixture")

    def test_typical_stress_pairs_freeze_every_render_setting(self) -> None:
        for position in range(0, len(self.documents), 2):
            typical = self.documents[position]
            stress = self.documents[position + 1]
            with self.subTest(family=typical["family"]):
                self.assertEqual(typical["variant"], "typical")
                self.assertEqual(stress["variant"], "stress")
                self.assertNotEqual(typical["score"], stress["score"])
                fixed_typical = {
                    key: value
                    for key, value in typical.items()
                    if key not in {"fixture_id", "variant", "score"}
                }
                fixed_stress = {
                    key: value
                    for key, value in stress.items()
                    if key not in {"fixture_id", "variant", "score"}
                }
                self.assertEqual(fixed_typical, fixed_stress)
                self.assertIsNone(typical["normalize_peak_db"])

    def test_lead_handoff_relations_and_planned_texture_are_adjacent(
        self,
    ) -> None:
        expected_relations = [
            ("sitar_lead", "piano_lead"),
            ("flute_lead", "sitar_lead"),
            ("guitar_lead", "flute_lead"),
        ]
        chain = (
            "piano_lead",
            "sitar_lead",
            "flute_lead",
            "guitar_lead",
        )
        family_rows = [
            (document, plan)
            for document, plan in zip(
                self.documents,
                self.plans,
                strict=True,
            )
            if document["family"] == "06-lead-handoff"
        ]
        self.assertEqual(len(family_rows), 2)
        for document, plan in family_rows:
            relations = document["roster"]["collaboration"][
                "balance_relations"
            ]
            self.assertEqual(
                [
                    (relation["subject"], relation["reference"])
                    for relation in relations
                ],
                expected_relations,
            )
            events_by_part = {
                part.executor.part_id: part.performance["events"]
                for part in plan.parts
            }
            for outgoing, incoming in zip(
                chain[:-1],
                chain[1:],
                strict=True,
            ):
                outgoing_note_off = max(
                    event["time"]
                    for event in events_by_part[outgoing]
                    if event["type"] == "note_off"
                )
                incoming_note_on = min(
                    event["time"]
                    for event in events_by_part[incoming]
                    if event["type"] == "note_on"
                )
                if document["variant"] == "typical":
                    self.assertGreaterEqual(
                        incoming_note_on - outgoing_note_off,
                        1.0,
                        (document["fixture_id"], outgoing, incoming),
                    )
                else:
                    self.assertGreaterEqual(
                        outgoing_note_off - incoming_note_on,
                        1.7,
                        (document["fixture_id"], outgoing, incoming),
                    )
            if document["variant"] == "stress":
                for outgoing, second_next in zip(
                    chain[:-2],
                    chain[2:],
                    strict=True,
                ):
                    outgoing_note_off = max(
                        event["time"]
                        for event in events_by_part[outgoing]
                        if event["type"] == "note_off"
                    )
                    second_next_note_on = min(
                        event["time"]
                        for event in events_by_part[second_next]
                        if event["type"] == "note_on"
                    )
                    self.assertGreaterEqual(
                        second_next_note_on - outgoing_note_off,
                        1.0,
                        (document["fixture_id"], outgoing, second_next),
                    )

    def test_documents_parse_and_build_without_rendering(self) -> None:
        for document, (score, roster), plan in zip(
            self.documents,
            self.parsed,
            self.plans,
            strict=True,
        ):
            with self.subTest(fixture_id=document["fixture_id"]):
                self.assertEqual(plan.expression.seed, document["seed"])
                self.assertEqual(
                    len(plan.parts),
                    len(roster.executors),
                )
                self.assertTrue(all(part.trace for part in plan.parts))
                self.assertEqual(
                    {part.id for part in score.parts},
                    {executor.part_id for executor in roster.executors},
                )

    def test_all_written_pitches_and_articulations_are_capability_legal(
        self,
    ) -> None:
        for document, (score, roster) in zip(
            self.documents,
            self.parsed,
            strict=True,
        ):
            with self.subTest(fixture_id=document["fixture_id"]):
                assignment_path = {
                    assignment["part"]: assignment["instrument"]
                    for assignment in document["roster"]["assignments"]
                }
                for part in score.parts:
                    for note in part.notes:
                        executor = roster.route(part.id, note.midi)
                        marking = note.articulation or part.default_articulation
                        articulation, _reason = executor.mapped_articulation(
                            marking
                        )
                        capability = executor.capability
                        played_midi = note.midi + executor.transpose
                        if (
                            capability.note_min is not None
                            and capability.note_max is not None
                        ):
                            self.assertTrue(
                                capability.covers(
                                    played_midi,
                                    articulation,
                                ),
                                (
                                    document["fixture_id"],
                                    part.id,
                                    played_midi,
                                    articulation,
                                ),
                            )
                        path = assignment_path[part.id]
                        if path in UNPITCHED_TRIGGER_KEYS:
                            self.assertIn(
                                round(note.midi),
                                UNPITCHED_TRIGGER_KEYS[path],
                            )

    def test_every_note_writes_the_formal_schema_fields_explicitly(self) -> None:
        required = {"bar", "beat", "duration_beats", "pitch"}
        for document in self.documents:
            for part in document["score"]["parts"]:
                for note in part["notes"]:
                    self.assertTrue(
                        required <= set(note),
                        (document["fixture_id"], part["id"], note),
                    )

    def test_target_roles_are_complete_and_cross_fixture_consistent(self) -> None:
        seen: dict[str, tuple[str, str]] = {}
        for document in self.documents:
            assignments = {
                assignment["part"]: assignment
                for assignment in document["roster"]["assignments"]
            }
            self.assertEqual(len(document["targets"]), len(assignments))
            for target in document["targets"]:
                assignment = assignments[target["part_id"]]
                self.assertEqual(
                    target["instrument_path"],
                    assignment["instrument"],
                )
                self.assertEqual(target["role"], assignment["role"])
                role = (
                    target["role"]["function"],
                    target["role"]["prominence"],
                )
                previous = seen.setdefault(target["instrument_path"], role)
                self.assertEqual(previous, role)
        self.assertEqual(seen, EXPECTED_ROLES)

    def test_balance_targets_are_suggest_only_candidates(self) -> None:
        for document, (_score, roster) in zip(
            self.documents,
            self.parsed,
            strict=True,
        ):
            with self.subTest(fixture_id=document["fixture_id"]):
                self.assertEqual(document["balance_target_status"], "candidate")
                self.assertEqual(
                    document["balance_calibration"],
                    {
                        "round": 2,
                        "status": "machine_candidate_pending_human",
                        "scope": "fixture_roster_only",
                        "metric": "overlap_active_rms",
                        "analyzer_modified_audio": False,
                    },
                )
                self.assertEqual(
                    document["roster"]["collaboration"]["mode"],
                    "suggest",
                )
                self.assertEqual(roster.collaboration.mode, "suggest")
                self.assertNotIn("assertions", document)
                self.assertTrue(document["human_questions"])
                self.assertTrue(
                    all(
                        isinstance(question, str) and question.strip()
                        for question in document["human_questions"]
                    )
                )

    def test_round_two_static_gains_are_fixture_local_candidates(self) -> None:
        expected = {
            "01-cello-background": {
                "cello_pad": -20.450481,
                "acoustic_bass": -4.61295,
            },
            "02-sitar-visibility": {
                "sitar_lead": 12.0,
            },
            "03-orchestral-depth": {
                "violin_counter": -3.157131,
                "strings_pad": -6.0,
                "horn_harmony": -4.0,
            },
            "04-modern-rhythm": {
                "finger_bass": -9.11787,
                "warm_pad": 2.040791,
            },
            "05-atmosphere-tail": {
                "warm_pad": 9.598508,
                "rain_ambience": -3.0,
            },
        }
        seen: set[str] = set()
        for document in self.documents:
            family = document["family"]
            if family not in expected:
                continue
            assignments = {
                assignment["part"]: assignment
                for assignment in document["roster"]["assignments"]
            }
            for part_id, gain_db in expected[family].items():
                self.assertEqual(
                    assignments[part_id]["gain_db"],
                    gain_db,
                    (document["fixture_id"], part_id),
                )
            seen.add(family)
        self.assertEqual(seen, set(expected))

    def test_rain_pair_keeps_the_global_gate_and_uses_background_headroom(
        self,
    ) -> None:
        rows = [
            document
            for document in self.documents
            if document["family"] == "05-atmosphere-tail"
        ]
        self.assertEqual(len(rows), 2)
        dynamics: dict[str, str] = {}
        for document in rows:
            roster = document["roster"]
            self.assertEqual(
                roster["collaboration"]["analysis"]["gate_dbfs"],
                -60.0,
            )
            assignment = next(
                item
                for item in roster["assignments"]
                if item["part"] == "rain_ambience"
            )
            self.assertEqual(assignment["gain_db"], -3.0)
            self.assertEqual(
                assignment["role"],
                {
                    "function": "ambience",
                    "prominence": "background",
                },
            )
            part = next(
                item
                for item in document["score"]["parts"]
                if item["id"] == "rain_ambience"
            )
            self.assertEqual(len(part["notes"]), 1)
            dynamics[document["variant"]] = part["notes"][0]["dynamic"]
        self.assertEqual(dynamics, {"typical": "mp", "stress": "mf"})

    def test_long_lifecycle_targets_have_complete_gates_and_tails(self) -> None:
        swells = {
            "typical": (60, 15.807710),
            "stress": (62, 20.906757),
        }
        for document in self.documents:
            target_paths = {
                target["instrument_path"] for target in document["targets"]
            }
            score = document["score"]
            if "电子乐器/温暖铺底" in target_paths:
                self.assertGreaterEqual(score["tail_seconds"], 3.02)
            if "环境与拟音/雨境合成氛围" in target_paths:
                self.assertGreaterEqual(score["tail_seconds"], 2.45)
            if "管弦乐/打击乐组/反向镲" not in target_paths:
                continue
            part = next(
                item
                for item in score["parts"]
                if item["id"] == "reverse_cymbal_effect"
            )
            self.assertEqual(len(part["notes"]), 1)
            note = part["notes"][0]
            expected_key, swell_seconds = swells[document["variant"]]
            self.assertEqual(round(parse_pitch(note["pitch"])), expected_key)
            nominal_seconds = note["duration_beats"] * 60.0 / 96.0
            self.assertGreater(nominal_seconds * 0.95, swell_seconds)

    def test_reverse_cymbal_natural_end_aligns_to_marked_emphasis(self) -> None:
        for document, plan in zip(
            self.documents,
            self.plans,
            strict=True,
        ):
            if document["family"] != "05-atmosphere-tail":
                continue
            contracts = document.get("timing_contracts")
            self.assertIsInstance(contracts, list)
            self.assertEqual(len(contracts), 1)
            contract = contracts[0]
            self.assertEqual(
                contract["kind"],
                "natural_sample_end_to_marked_note_on",
            )
            self.assertEqual(
                contract["source_anchor"],
                "natural_sample_end",
            )
            self.assertEqual(
                contract["target_anchor"],
                "planned_note_on",
            )

            score_source = next(
                part
                for part in document["score"]["parts"]
                if part["id"] == contract["source_part_id"]
            )
            self.assertEqual(len(score_source["notes"]), 1)
            source_note = score_source["notes"][0]
            source_midi = str(round(parse_pitch(source_note["pitch"])))
            natural_duration = contract[
                "source_duration_seconds_by_midi_note"
            ][source_midi]

            marker = contract["target_marker"]
            score_target = next(
                part
                for part in document["score"]["parts"]
                if part["id"] == contract["target_part_id"]
            )
            marked_notes = [
                note
                for note in score_target["notes"]
                if all(note.get(key) == value for key, value in marker.items())
            ]
            self.assertEqual(len(marked_notes), 1)
            marked = marked_notes[0]

            plan_by_part = {
                part.executor.part_id: part for part in plan.parts
            }
            source_trace = plan_by_part[contract["source_part_id"]].trace
            self.assertEqual(len(source_trace), 1)
            target_trace = [
                item
                for item in plan_by_part[contract["target_part_id"]].trace
                if (
                    item["小节"] == marked["bar"]
                    and item["拍"] == marked["beat"]
                    and item["音"] == marked["pitch"]
                )
            ]
            self.assertEqual(len(target_trace), 1)
            natural_end = source_trace[0]["时间"] + natural_duration
            marked_onset = target_trace[0]["时间"]
            error_ms = abs(natural_end - marked_onset) * 1000.0
            self.assertLessEqual(
                error_ms,
                contract["tolerance_ms"],
                (document["fixture_id"], error_ms),
            )

    def test_combined_listening_duration_stays_between_four_and_five_minutes(
        self,
    ) -> None:
        plan_seconds = sum(plan.duration_seconds for plan in self.plans)
        rendered_seconds = sum(
            plan.duration_seconds
            + SpaceConfig.from_dict(document["space"]).tail_seconds(
                plan.sample_rate
            )
            for document, plan in zip(
                self.documents,
                self.plans,
                strict=True,
            )
        )
        self.assertGreaterEqual(plan_seconds, 4.0 * 60.0)
        self.assertLessEqual(plan_seconds, 5.0 * 60.0)
        self.assertGreaterEqual(rendered_seconds, 4.0 * 60.0)
        self.assertLessEqual(rendered_seconds, 5.0 * 60.0)


if __name__ == "__main__":
    unittest.main()
