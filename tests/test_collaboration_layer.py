from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

from tianlai.capability import (
    DurationArticulationRule,
    InstrumentCapability,
    load_capabilities,
    resolve_capability,
)
from tianlai.conductor import ExpressionSettings, build_plan
from tianlai.ensemble import balance_gains, render_plan
from tianlai.events import parse_performance_document
from tianlai.roster import parse_roster_document
from tianlai.score import parse_pitch, parse_score_document, pitch_name


ROOT = Path(__file__).resolve().parents[1]
SCORE_PATH = ROOT / "examples" / "小编制示例.score.json"
ROSTER_PATH = ROOT / "examples" / "小编制示例.roster.json"


def _load_score() -> dict:
    return json.loads(SCORE_PATH.read_text(encoding="utf-8"))


def _load_roster() -> dict:
    return json.loads(ROSTER_PATH.read_text(encoding="utf-8"))


class PitchNotationTests(unittest.TestCase):
    def test_middle_c_is_midi_60(self) -> None:
        self.assertEqual(parse_pitch("C4"), 60.0)
        self.assertEqual(parse_pitch("A4"), 69.0)

    def test_accidentals_and_round_trip(self) -> None:
        self.assertEqual(parse_pitch("C#4"), 61.0)
        self.assertEqual(parse_pitch("Bb3"), 58.0)
        self.assertEqual(pitch_name(61), "C#4")

    def test_missing_octave_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "octave"):
            parse_pitch("C")


class TempoMapTests(unittest.TestCase):
    def test_beats_convert_through_a_tempo_change(self) -> None:
        score = parse_score_document(
            {
                "tempo_map": [
                    {"bar": 1, "bpm": 60, "beats_per_bar": 4, "beat_unit": 4},
                    {"bar": 3, "bpm": 120},
                ],
                "parts": [{"id": "a", "notes": []}],
            }
        )
        tempo = score.tempo_map
        # 前两小节 60 BPM,每拍 1 秒,共 8 秒。
        self.assertAlmostEqual(tempo.seconds_at(3, 1.0), 8.0)
        # 第三小节起 120 BPM,每拍 0.5 秒。
        self.assertAlmostEqual(tempo.seconds_at(3, 3.0), 9.0)

    def test_compound_meter_beats_are_counted_in_the_beat_unit(self) -> None:
        score = parse_score_document(
            {
                "tempo_map": [{"bar": 1, "bpm": 120, "beats_per_bar": 6, "beat_unit": 8}],
                "parts": [{"id": "a", "notes": []}],
            }
        )
        # 6/8 一小节 = 6 个八分 = 3 个四分;120 BPM 下 1.5 秒。
        self.assertAlmostEqual(score.tempo_map.seconds_at(2, 1.0), 1.5)

    def test_first_tempo_entry_must_be_complete(self) -> None:
        with self.assertRaisesRegex(ValueError, "bpm"):
            parse_score_document(
                {
                    "tempo_map": [{"bar": 1, "beats_per_bar": 4, "beat_unit": 4}],
                    "parts": [{"id": "a", "notes": []}],
                }
            )


class MidiImportTests(unittest.TestCase):
    """用自包含的标准 MIDI 标的验证导入器，不绑定任何第三方音源包。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.MIDI = Path(cls._temporary_directory.name) / "import-fixture.mid"

        # Format 0，480 PPQ，120 BPM，4/4；故意保留尾随 NUL 的轨道名，
        # 并使用 37/91 两个非力度记号查表值，覆盖本类的清洗与原样力度测试。
        track = b"".join(
            (
                b"\x00\xff\x03\x08Fixture\x00",
                b"\x00\xff\x51\x03\x07\xa1\x20",
                b"\x00\xff\x58\x04\x04\x02\x18\x08",
                b"\x00\xc0\x69",
                b"\x00\x90\x3c\x25",
                b"\x83\x60\x80\x3c\x40",
                b"\x00\x90\x40\x5b",
                b"\x83\x60\x80\x40\x40",
                b"\x00\xff\x2f\x00",
            )
        )
        payload = b"".join(
            (
                b"MThd",
                (6).to_bytes(4, "big"),
                (0).to_bytes(2, "big"),
                (1).to_bytes(2, "big"),
                (480).to_bytes(2, "big"),
                b"MTrk",
                len(track).to_bytes(4, "big"),
                track,
            )
        )
        cls.MIDI.write_bytes(payload)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    def test_import_produces_a_valid_score(self) -> None:
        from tianlai.midi_import import read_midi

        document, report = read_midi(self.MIDI)
        score = parse_score_document(document)
        self.assertEqual(score.schema_version, 1)
        self.assertEqual(len(score.parts), 1)
        self.assertEqual(len(score.parts[0].notes), report.parts[0]["note_count"])
        self.assertTrue(
            all(
                note.source_event_id is not None
                for note in score.parts[0].notes
            )
        )

    def test_tempo_arithmetic_survives_the_round_trip(self) -> None:
        from tianlai.midi_import import read_midi

        score = parse_score_document(read_midi(self.MIDI)[0])
        entry = score.tempo_map.entries[0]
        # 120 BPM 的 4/4,一小节正好 2 秒。
        self.assertAlmostEqual(entry.bpm, 120.0)
        self.assertAlmostEqual(score.tempo_map.seconds_at(2, 1.0), 2.0)

    def test_exact_velocities_are_preserved_not_quantised(self) -> None:
        from tianlai.midi_import import read_midi

        from tianlai.conductor import _DYNAMIC_VELOCITY

        score = parse_score_document(read_midi(self.MIDI)[0])
        velocities = {note.velocity for note in score.parts[0].notes}
        self.assertTrue(velocities)
        for velocity in velocities:
            # 每个力度都正好落在 1/127 的整数格上,说明是原样透传而非重新赋值。
            self.assertAlmostEqual(velocity * 127.0, round(velocity * 127.0), places=3)
        self.assertFalse(
            velocities <= set(_DYNAMIC_VELOCITY.values()),
            "力度被压成了力度记号的查表值",
        )

    def test_control_characters_are_stripped_from_part_ids(self) -> None:
        """MIDI 轨道名常带尾随 NUL,带进声部 id 会与编制表静默失配。"""

        from tianlai.midi_import import read_midi

        document, _ = read_midi(self.MIDI)
        for part in document["parts"]:
            self.assertTrue(part["id"].isprintable())
            self.assertEqual(part["id"], part["id"].strip())

    def test_smpte_timebase_is_refused_with_a_usable_message(self) -> None:
        import tempfile

        from tianlai.midi_import import read_midi

        header = b"MThd" + (6).to_bytes(4, "big") + bytes([0, 0, 0, 1, 0xE7, 0x28])
        with tempfile.NamedTemporaryFile(suffix=".mid", delete=False) as handle:
            handle.write(header)
            path = handle.name
        try:
            with self.assertRaisesRegex(ValueError, "SMPTE"):
                read_midi(path)
        finally:
            Path(path).unlink(missing_ok=True)


class ScoreExtensionTests(unittest.TestCase):
    def test_tempo_may_change_mid_bar_for_rubato(self) -> None:
        score = parse_score_document(
            {
                "tempo_map": [
                    {"bar": 1, "bpm": 60, "beats_per_bar": 4, "beat_unit": 4},
                    {"bar": 1, "beat": 3, "bpm": 120},
                ],
                "parts": [{"id": "a", "notes": []}],
            }
        )
        # 前两拍 60 BPM = 2 秒,后两拍 120 BPM = 1 秒。
        self.assertAlmostEqual(score.tempo_map.seconds_at(2, 1.0), 3.0)

    def test_meter_may_not_change_mid_bar(self) -> None:
        with self.assertRaisesRegex(ValueError, "拍号只能在小节线上"):
            parse_score_document(
                {
                    "tempo_map": [
                        {"bar": 1, "bpm": 60, "beats_per_bar": 4, "beat_unit": 4},
                        {"bar": 2, "beat": 3, "beats_per_bar": 3},
                    ],
                    "parts": [{"id": "a", "notes": []}],
                }
            )

    def test_note_velocity_overrides_the_dynamic_mark(self) -> None:
        capabilities = load_capabilities(ROOT / "乐器")
        document = _load_score()
        for part in document["parts"]:
            if part["id"] == "bass":
                part["notes"][0]["velocity"] = 0.123
        plan = build_plan(
            parse_score_document(document),
            parse_roster_document(_load_roster(), capabilities),
            ExpressionSettings.from_dict({"mode": "strict"}),
        )
        bass = next(part for part in plan.parts if part.executor.part_id == "bass")
        self.assertAlmostEqual(bass.trace[0]["力度"], 0.123, places=3)
        self.assertIn("演奏自带", bass.trace[0]["推导"]["力度记号"])


class CapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_every_capability_records_where_its_vocabulary_came_from(self) -> None:
        for capability in self.capabilities.values():
            self.assertTrue(
                capability.articulation_source,
                f"{capability.relative_path} 没有记录奏法来源",
            )

    def test_fixed_pitch_percussion_is_not_range_checked(self) -> None:
        kick = self.capabilities["现代鼓组/底鼓"]
        self.assertTrue(kick.ignores_pitch)
        self.assertTrue(kick.covers(9999.0))

    def test_declared_ranges_match_the_real_instruments(self) -> None:
        # 这四件是本轮补声明的,下限都应落在真实乐器的最低音上。
        self.assertEqual(self.capabilities["管弦乐/弦乐组/小提琴"].note_min, 55.0)
        self.assertEqual(self.capabilities["管弦乐/弦乐组/大提琴"].note_min, 36.0)
        self.assertEqual(self.capabilities["管弦乐/木管组/长笛"].note_min, 60.0)
        self.assertEqual(self.capabilities["键盘乐器/钢琴"].note_max, 108.0)

    def test_ambiguous_reference_is_rejected_rather_than_guessed(self) -> None:
        with self.assertRaisesRegex(ValueError, "不存在"):
            resolve_capability(self.capabilities, "根本没有这件乐器")


class RosterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_one_drum_part_expands_into_several_executors(self) -> None:
        roster = parse_roster_document(_load_roster(), self.capabilities)
        drums = roster.executors_for("drums")
        self.assertEqual(len(drums), 3)
        self.assertEqual(
            {executor.capability.relative_path for executor in drums},
            {"现代鼓组/底鼓", "现代鼓组/边击军鼓", "现代鼓组/闭合踩镲"},
        )

    def test_noteheads_route_to_their_own_instrument(self) -> None:
        roster = parse_roster_document(_load_roster(), self.capabilities)
        self.assertEqual(
            roster.route("drums", parse_pitch("C2")).capability.relative_path,
            "现代鼓组/底鼓",
        )
        self.assertEqual(
            roster.route("drums", parse_pitch("F#2")).capability.relative_path,
            "现代鼓组/闭合踩镲",
        )

    def test_unmapped_notehead_is_reported_not_dropped(self) -> None:
        roster = parse_roster_document(_load_roster(), self.capabilities)
        with self.assertRaisesRegex(ValueError, "没有对应乐器"):
            roster.route("drums", parse_pitch("A5"))

    def test_kit_entry_may_carry_a_transpose(self) -> None:
        """打击件按固定键位映射,kit 条目要能移调把谱面鼓音送进该件的键位。"""

        document = _load_roster()
        for assignment in document["assignments"]:
            if assignment.get("part") == "drums":
                # 把 C2 改成带 transpose 的对象写法,路由仍按原符头 C2。
                assignment["kit"]["C2"] = {
                    "instrument": "现代鼓组/底鼓",
                    "transpose": 24,
                }
        roster = parse_roster_document(document, self.capabilities)
        executor = roster.route("drums", parse_pitch("C2"))
        self.assertEqual(executor.capability.relative_path, "现代鼓组/底鼓")
        self.assertEqual(executor.transpose, 24)

    def test_transpose_rejects_coercion_but_accepts_schema_integer_float(self) -> None:
        for invalid in (True, "12", 1.5):
            with self.subTest(value=invalid):
                document = _load_roster()
                document["assignments"][0]["transpose"] = invalid
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    parse_roster_document(document, self.capabilities)

        document = _load_roster()
        document["assignments"][0]["transpose"] = 12.0
        roster = parse_roster_document(document, self.capabilities)
        self.assertEqual(roster.executors[0].transpose, 12)

        for invalid in (False, "24", 1.5):
            with self.subTest(kit_value=invalid):
                document = _load_roster()
                drums = next(
                    item
                    for item in document["assignments"]
                    if item.get("part") == "drums"
                )
                drums["kit"]["C2"] = {
                    "instrument": "现代鼓组/底鼓",
                    "transpose": invalid,
                }
                with self.assertRaisesRegex(ValueError, "must be an integer"):
                    parse_roster_document(document, self.capabilities)

    def test_articulation_map_rejects_non_string_coercion(self) -> None:
        for mapping in ({1: "legato"}, {"legato": 1}, {"": "legato"}):
            with self.subTest(mapping=mapping):
                document = _load_roster()
                document["assignments"][0]["articulation_map"] = mapping
                with self.assertRaisesRegex(
                    ValueError,
                    "articulation_map",
                ):
                    parse_roster_document(document, self.capabilities)

    def test_per_executor_override_reaches_the_rendered_manifest(self) -> None:
        """编制表的 overrides 要能并入构造该执行器时的乐器清单。"""

        document = _load_roster()
        target = None
        for assignment in document["assignments"]:
            if "instrument" in assignment:
                assignment["overrides"] = {"release_seconds": 0.25}
                target = assignment["part"]
                break
        roster = parse_roster_document(document, self.capabilities)
        executor = next(e for e in roster.executors if e.part_id == target)
        self.assertEqual(executor.override_map, {"release_seconds": 0.25})

    def test_override_rejects_structural_fields(self) -> None:
        """覆盖只允许标量,不能替换采样区一类结构,以免悄悄改写乐器身份。"""

        document = _load_roster()
        for assignment in document["assignments"]:
            if "instrument" in assignment:
                assignment["overrides"] = {"regions": [{"sample": "x"}]}
                break
        with self.assertRaisesRegex(ValueError, "结构性字段"):
            parse_roster_document(document, self.capabilities)

    def test_assignment_must_pick_exactly_one_of_instrument_or_kit(self) -> None:
        document = _load_roster()
        document["assignments"][0]["kit"] = {"C2": "现代鼓组/底鼓"}
        with self.assertRaisesRegex(ValueError, "instrument 或 kit"):
            parse_roster_document(document, self.capabilities)

    def test_unplayed_score_part_is_refused(self) -> None:
        from tianlai.roster import check_roster_covers_score

        score = parse_score_document(_load_score())
        document = _load_roster()
        document["assignments"] = [
            item for item in document["assignments"] if item["part"] != "bass"
        ]
        roster = parse_roster_document(document, self.capabilities)
        with self.assertRaisesRegex(ValueError, "没人演奏"):
            check_roster_covers_score(roster, score)

    def test_dropping_a_part_needs_explicit_declaration(self) -> None:
        """删声部必须显式声明,不能靠不指派就静默消失。"""

        from tianlai.roster import check_roster_covers_score

        score = parse_score_document(_load_score())
        document = _load_roster()
        document["assignments"] = [
            item for item in document["assignments"] if item["part"] != "bass"
        ]
        document["drop_parts"] = ["bass"]
        roster = parse_roster_document(document, self.capabilities)
        # 显式声明后闸门放行,且演奏计划里不含被删声部。
        check_roster_covers_score(roster, score)
        plan = build_plan(
            score, roster, ExpressionSettings.from_dict({"mode": "strict"})
        )
        self.assertNotIn("bass", {p.executor.part_id for p in plan.parts})

    def test_dropping_a_nonexistent_part_is_rejected(self) -> None:
        from tianlai.roster import check_roster_covers_score

        score = parse_score_document(_load_score())
        document = _load_roster()
        document["drop_parts"] = ["there_is_no_such_part"]
        roster = parse_roster_document(document, self.capabilities)
        with self.assertRaisesRegex(ValueError, "不存在的声部"):
            check_roster_covers_score(roster, score)

    def test_a_part_cannot_be_both_assigned_and_dropped(self) -> None:
        document = _load_roster()
        document["drop_parts"] = ["bass"]
        with self.assertRaisesRegex(ValueError, "自相矛盾"):
            parse_roster_document(document, self.capabilities)

    def test_duration_scale_shortens_only_its_own_part(self) -> None:
        """密集写作里音符互相拖尾是浑浊的主因,时值缩放要能按声部单独调。"""

        score = parse_score_document(_load_score())
        document = _load_roster()
        for assignment in document["assignments"]:
            if assignment["part"] == "strings":
                assignment["duration_scale"] = 0.5
        shortened = build_plan(
            score,
            parse_roster_document(document, self.capabilities),
            ExpressionSettings.from_dict({"mode": "strict"}),
        )
        baseline = build_plan(
            score,
            parse_roster_document(_load_roster(), self.capabilities),
            ExpressionSettings.from_dict({"mode": "strict"}),
        )

        def held(plan, part_id: str) -> float:
            part = next(item for item in plan.parts if item.executor.part_id == part_id)
            return sum(entry["时长"] for entry in part.trace)

        self.assertAlmostEqual(
            held(shortened, "strings"), held(baseline, "strings") * 0.5, places=3
        )
        self.assertAlmostEqual(held(shortened, "piano"), held(baseline, "piano"))

    def test_dynamic_compression_lifts_soft_notes_and_leaves_loud_ones(self) -> None:
        """实测钢琴动态范围 32 dB、弦乐仅约 10 dB,弱奏段落因此失衡。

        压缩要做的是抬起弱奏、几乎不动强奏;若它把强奏也一起拉动,就成了把
        整条力度线压平,那是另一回事。
        """

        document = _load_score()
        for part in document["parts"]:
            if part["id"] == "piano":
                for note in part["notes"]:
                    note["velocity"] = 0.30
                part["notes"][0]["velocity"] = 0.90
        roster_document = _load_roster()
        for assignment in roster_document["assignments"]:
            if assignment["part"] == "piano":
                assignment["dynamic_compression"] = 0.6
        plan = build_plan(
            parse_score_document(document),
            parse_roster_document(roster_document, self.capabilities),
            ExpressionSettings.from_dict({"mode": "strict"}),
        )
        piano = next(part for part in plan.parts if part.executor.part_id == "piano")
        loud = [e for e in piano.trace if "0.900" in e["推导"]["力度记号"]]
        soft = [e for e in piano.trace if "0.300" in e["推导"]["力度记号"]]
        self.assertTrue(loud and soft)
        self.assertGreater(soft[0]["力度"], 0.30 + 0.20, "弱奏没有被抬起")
        self.assertLess(abs(loud[0]["力度"] - 0.90), 0.10, "强奏被动得太多")
        self.assertIn("动态压缩", soft[0]["推导"])

    def test_duration_scale_is_range_checked(self) -> None:
        document = _load_roster()
        document["assignments"][0]["duration_scale"] = 5.0
        with self.assertRaisesRegex(ValueError, "duration_scale"):
            parse_roster_document(document, self.capabilities)

    def test_seat_azimuth_drives_the_default_pan(self) -> None:
        roster = parse_roster_document(_load_roster(), self.capabilities)
        bass = roster.executors_for("bass")[0]
        self.assertLess(bass.pan, 0.0)  # 坐在左侧
        self.assertAlmostEqual(bass.pan, -20.0 / 45.0)


class ArticulationGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_unsupported_articulation_raises_instead_of_substituting(self) -> None:
        score = _load_score()
        for part in score["parts"]:
            if part["id"] == "bass":
                part["notes"][0]["articulation"] = "flutter_tongue"
        document = parse_score_document(score)
        roster = parse_roster_document(_load_roster(), self.capabilities)
        with self.assertRaisesRegex(ValueError, "不支持"):
            build_plan(document, roster)

    def test_roster_dictionary_translates_a_score_marking(self) -> None:
        score = _load_score()
        for part in score["parts"]:
            if part["id"] == "bass":
                for note in part["notes"]:
                    note["articulation"] = "pizzicato"
        roster_document = _load_roster()
        for assignment in roster_document["assignments"]:
            if assignment["part"] == "bass":
                assignment["articulation_map"] = {"pizzicato": "arco"}
        plan = build_plan(
            parse_score_document(score),
            parse_roster_document(roster_document, self.capabilities),
        )
        bass = next(part for part in plan.parts if part.executor.part_id == "bass")
        self.assertEqual(bass.trace[0]["奏法"], "arco")
        self.assertIn("编制表映射", bass.trace[0]["推导"]["奏法"])

    def test_out_of_range_note_names_the_bar_and_the_instrument(self) -> None:
        score = _load_score()
        for part in score["parts"]:
            if part["id"] == "bass":
                part["notes"][0]["pitch"] = "C6"
        document = parse_score_document(score)
        roster = parse_roster_document(_load_roster(), self.capabilities)
        with self.assertRaisesRegex(ValueError, "超出"):
            build_plan(document, roster)

    def _single_note_plan(
        self,
        *,
        instrument: str,
        pitch: str,
        articulation: str,
        articulation_map: dict[str, str] | None = None,
    ):
        score = parse_score_document(
            {
                "tempo_map": [
                    {
                        "bar": 1,
                        "beat": 1,
                        "bpm": 120,
                        "beats_per_bar": 4,
                        "beat_unit": 4,
                    }
                ],
                "parts": [
                    {
                        "id": "solo",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 1,
                                "pitch": pitch,
                                "velocity": 0.7,
                                "articulation": articulation,
                            }
                        ],
                    }
                ],
            }
        )
        assignment = {
            "part": "solo",
            "instrument": instrument,
            "articulation_auto": False,
        }
        if articulation_map is not None:
            assignment["articulation_map"] = articulation_map
        roster = parse_roster_document(
            {"assignments": [assignment]},
            self.capabilities,
        )
        return build_plan(
            score,
            roster,
            ExpressionSettings.from_dict({"mode": "strict"}),
        )

    def test_timpani_roll_is_rejected_before_render_outside_its_own_range(
        self,
    ) -> None:
        hit = self._single_note_plan(
            instrument="管弦乐/打击乐组/定音鼓",
            pitch="D2",
            articulation="hit",
        )
        self.assertEqual(hit.parts[0].trace[0]["奏法"], "hit")

        with self.assertRaisesRegex(
            ValueError,
            "奏法 'roll' 的可演奏分段 F2~G3",
        ):
            self._single_note_plan(
                instrument="管弦乐/打击乐组/定音鼓",
                pitch="D2",
                articulation="roll",
            )

    def test_vibraphone_bowed_range_differs_from_mallet_range(self) -> None:
        open_plan = self._single_note_plan(
            instrument="管弦乐/打击乐组/颤音琴",
            pitch="F3",
            articulation="open",
        )
        bowed_plan = self._single_note_plan(
            instrument="管弦乐/打击乐组/颤音琴",
            pitch="A3",
            articulation="bowed",
        )
        self.assertEqual(open_plan.parts[0].trace[0]["奏法"], "open")
        self.assertEqual(bowed_plan.parts[0].trace[0]["奏法"], "bowed")
        with self.assertRaisesRegex(
            ValueError,
            "奏法 'bowed' 的可演奏分段 A3~F6",
        ):
            self._single_note_plan(
                instrument="管弦乐/打击乐组/颤音琴",
                pitch="F3",
                articulation="bowed",
            )

    def test_range_gate_uses_the_roster_mapped_backend_articulation(self) -> None:
        with self.assertRaisesRegex(ValueError, "奏法 'roll'"):
            self._single_note_plan(
                instrument="管弦乐/打击乐组/定音鼓",
                pitch="D2",
                articulation="swell",
                articulation_map={"swell": "roll"},
            )

    def test_automatic_articulation_skips_a_candidate_that_cannot_play_pitch(
        self,
    ) -> None:
        capability = InstrumentCapability(
            name="自动奏法测试",
            relative_path="自动奏法测试",
            manifest_path="自动奏法测试/乐器.json",
            implementation_type="oscillator",
            pitched=True,
            note_min=48.0,
            note_max=72.0,
            articulations=("accent", "sustain"),
            default_articulation="sustain",
            articulation_source="test",
            onset_seconds=None,
            quality_tier="candidate",
            license_status="approved",
            articulation_playable_ranges=(
                ("accent", ((60.0, 72.0),)),
            ),
            duration_articulation_rules=(
                DurationArticulationRule(
                    rule_id="test_short_accent",
                    source_articulation="sustain",
                    target_articulation="accent",
                    below_seconds=1.2,
                ),
            ),
        )
        score = parse_score_document(
            {
                "tempo_map": [
                    {
                        "bar": 1,
                        "beat": 1,
                        "bpm": 120,
                        "beats_per_bar": 4,
                        "beat_unit": 4,
                    }
                ],
                "parts": [
                    {
                        "id": "solo",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 1,
                                "pitch": "G3",
                                "velocity": 0.7,
                            },
                            {
                                "bar": 1,
                                "beat": 2,
                                "duration_beats": 1,
                                "pitch": "C4",
                                "velocity": 0.7,
                            },
                        ],
                    }
                ],
            }
        )
        roster = parse_roster_document(
            {
                "assignments": [
                    {
                        "part": "solo",
                        "instrument": "自动奏法测试",
                    }
                ]
            },
            {"自动奏法测试": capability},
        )
        plan = build_plan(score, roster)
        self.assertEqual(
            [entry["奏法"] for entry in plan.parts[0].trace],
            ["sustain", "accent"],
        )

    def test_supporting_accent_does_not_authorize_automatic_substitution(
        self,
    ) -> None:
        capability = InstrumentCapability(
            name="无自动规则测试",
            relative_path="无自动规则测试",
            manifest_path="无自动规则测试/乐器.json",
            implementation_type="oscillator",
            pitched=True,
            note_min=48.0,
            note_max=84.0,
            articulations=("accent", "sustain"),
            default_articulation="sustain",
            articulation_source="test",
            onset_seconds=None,
            quality_tier="candidate",
            license_status="approved",
        )
        score = parse_score_document(
            {
                "tempo_map": [
                    {
                        "bar": 1,
                        "beat": 1,
                        "bpm": 120,
                        "beats_per_bar": 4,
                        "beat_unit": 4,
                    }
                ],
                "parts": [
                    {
                        "id": "solo",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 0.125,
                                "pitch": "C4",
                                "velocity": 0.7,
                            }
                        ],
                    }
                ],
            }
        )
        roster = parse_roster_document(
            {
                "assignments": [
                    {
                        "part": "solo",
                        "instrument": "无自动规则测试",
                    }
                ]
            },
            {"无自动规则测试": capability},
        )
        plan = build_plan(score, roster)
        self.assertEqual(plan.parts[0].trace[0]["奏法"], "sustain")

    def test_duration_rule_uses_effective_gate_and_strict_threshold(self) -> None:
        capability = InstrumentCapability(
            name="时值规则测试",
            relative_path="时值规则测试",
            manifest_path="时值规则测试/乐器.json",
            implementation_type="oscillator",
            pitched=True,
            note_min=48.0,
            note_max=84.0,
            articulations=("accent", "sustain"),
            default_articulation="sustain",
            articulation_source="test",
            onset_seconds=None,
            quality_tier="candidate",
            license_status="approved",
            duration_articulation_rules=(
                DurationArticulationRule(
                    rule_id="below_one_second",
                    source_articulation="sustain",
                    target_articulation="accent",
                    below_seconds=1.0,
                ),
            ),
        )
        score_document = {
            "tempo_map": [
                {
                    "bar": 1,
                    "beat": 1,
                    "bpm": 60,
                    "beats_per_bar": 4,
                    "beat_unit": 4,
                }
            ],
            "parts": [
                {
                    "id": "solo",
                    "notes": [
                        {
                            "bar": 1,
                            "beat": 1,
                            "duration_beats": 1.0,
                            "pitch": "C4",
                            "velocity": 0.7,
                        },
                        {
                            "bar": 1,
                            "beat": 2,
                            "duration_beats": 2.0,
                            "pitch": "D4",
                            "velocity": 0.7,
                        },
                    ],
                }
            ],
        }
        score = parse_score_document(score_document)

        def articulations(duration_scale: float) -> list[str | None]:
            roster = parse_roster_document(
                {
                    "assignments": [
                        {
                            "part": "solo",
                            "instrument": "时值规则测试",
                            "duration_scale": duration_scale,
                        }
                    ]
                },
                {"时值规则测试": capability},
            )
            return [
                entry["奏法"]
                for entry in build_plan(score, roster).parts[0].trace
            ]

        # The unmarked gate shape is 0.95: the first note is below the
        # threshold, while the second is not.
        self.assertEqual(articulations(1.0), ["accent", "sustain"])
        # Roster duration_scale participates in the deterministic gate:
        # after halving, both are below one second (the second is 0.95s).
        self.assertEqual(articulations(0.5), ["accent", "accent"])

        disabled_roster = parse_roster_document(
            {
                "assignments": [
                    {
                        "part": "solo",
                        "instrument": "时值规则测试",
                        "articulation_auto": False,
                    }
                ]
            },
            {"时值规则测试": capability},
        )
        self.assertEqual(
            [
                entry["奏法"]
                for entry in build_plan(score, disabled_roster).parts[0].trace
            ],
            ["sustain", "sustain"],
        )

        explicit_default_document = copy.deepcopy(score_document)
        explicit_default_document["parts"][0][
            "default_articulation"
        ] = "sustain"
        explicit_default = parse_score_document(explicit_default_document)
        enabled_roster = parse_roster_document(
            {
                "assignments": [
                    {
                        "part": "solo",
                        "instrument": "时值规则测试",
                    }
                ]
            },
            {"时值规则测试": capability},
        )
        self.assertEqual(
            [
                entry["奏法"]
                for entry in build_plan(
                    explicit_default,
                    enabled_roster,
                ).parts[0].trace
            ],
            ["sustain", "sustain"],
        )


class ConductorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")
        cls.score = parse_score_document(_load_score())
        cls.roster = parse_roster_document(_load_roster(), cls.capabilities)

    def _plan(self, **overrides):
        settings = ExpressionSettings.from_dict(
            {"mode": "ensemble", "humanize": {"seed": 20260722, **overrides}}
        )
        return build_plan(self.score, self.roster, settings)

    def test_plan_emits_valid_performance_documents(self) -> None:
        plan = self._plan()
        for part in plan.parts:
            document = parse_performance_document(part.performance)
            self.assertGreater(len(document.events), 0)

    def test_every_note_carries_a_readable_derivation(self) -> None:
        plan = self._plan()
        piano = next(part for part in plan.parts if part.executor.part_id == "piano")
        for entry in piano.trace:
            self.assertIn("力度记号", entry["推导"])
            self.assertIn("节拍重音", entry["推导"])

    def test_downbeats_are_stronger_than_offbeats(self) -> None:
        plan = self._plan(depth=0.0)
        drums = next(
            part for part in plan.parts if part.executor.executor_id == "drums.F#2"
        )
        downbeat = next(item for item in drums.trace if item["拍"] == 1.0)
        offbeat = next(item for item in drums.trace if item["拍"] == 1.5)
        self.assertGreater(downbeat["力度"], offbeat["力度"])

    def test_closing_chord_shares_one_phrase_end_shape_and_onset(self) -> None:
        """句尾属于整个同起音和弦，不能只推迟音高排序后的最后一个音。"""

        # Keep residual humanisation enabled: one written chord is one
        # physical gesture, so per-note velocity variation may differ but
        # timing jitter must not turn it into a random flam.
        plan = self._plan()
        strings = next(part for part in plan.parts if part.executor.part_id == "strings")
        closing = [
            entry
            for entry in strings.trace
            if entry["小节"] == 8 and entry["拍"] == 1.0
        ]
        self.assertEqual(len(closing), 3)
        self.assertEqual({entry["时间"] for entry in closing}, {closing[0]["时间"]})
        phrase_shapes = {entry["推导"]["乐句"] for entry in closing}
        self.assertEqual(len(phrase_shapes), 1)
        self.assertIn("句尾收", next(iter(phrase_shapes)))

    def test_adding_a_chord_tone_does_not_move_later_phrase_positions(self) -> None:
        """和弦音数量不是乐句进度；加内声部不能改写后续起音的塑形。"""

        baseline = self._plan(depth=0.0)
        edited_document = copy.deepcopy(_load_score())
        strings_document = next(
            part for part in edited_document["parts"] if part["id"] == "strings"
        )
        strings_document["notes"].append(
            {
                "event_id": "strings-added-chord-tone",
                "bar": 1,
                "beat": 1,
                "duration_beats": 4,
                "pitch": "E4",
                "dynamic": "p",
                "articulation": "sustain",
            }
        )
        edited = build_plan(
            parse_score_document(edited_document),
            self.roster,
            ExpressionSettings.from_dict(
                {"mode": "ensemble", "humanize": {"depth": 0.0, "seed": 20260722}}
            ),
        )

        def second_bar_c(plan):
            strings = next(
                part for part in plan.parts if part.executor.part_id == "strings"
            )
            return next(
                entry
                for entry in strings.trace
                if entry["小节"] == 2
                and entry["拍"] == 1.0
                and entry["音"] == "C4"
            )

        before = second_bar_c(baseline)
        after = second_bar_c(edited)
        self.assertEqual(before["推导"]["乐句"], after["推导"]["乐句"])
        self.assertEqual(before["时间"], after["时间"])
        self.assertEqual(before["力度"], after["力度"])

    def test_short_upper_chord_tone_does_not_create_a_false_phrase_break(self) -> None:
        """休止从整个起音组的最长发声算，不能只看最高音的短时值。"""

        score = parse_score_document(
            {
                "tempo_map": [
                    {"bar": 1, "bpm": 60, "beats_per_bar": 4, "beat_unit": 4}
                ],
                "parts": [
                    {
                        "id": "strings",
                        "default_dynamic": "p",
                        "default_articulation": "sustain",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 4,
                                "pitch": "C4",
                            },
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 4,
                                "pitch": "E4",
                            },
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 0.25,
                                "pitch": "G4",
                            },
                            {
                                "bar": 2,
                                "beat": 1,
                                "duration_beats": 1,
                                "pitch": "C4",
                            },
                        ],
                    }
                ],
            }
        )
        roster = parse_roster_document(
            {
                "assignments": [
                    {
                        "part": "strings",
                        "instrument": "管弦乐/弦乐组/中提琴",
                    }
                ]
            },
            self.capabilities,
        )
        plan = build_plan(
            score,
            roster,
            ExpressionSettings.from_dict(
                {"mode": "ensemble", "humanize": {"depth": 0.0}}
            ),
        )
        first_chord = [
            entry
            for entry in plan.parts[0].trace
            if entry["小节"] == 1 and entry["拍"] == 1.0
        ]
        self.assertEqual(len(first_chord), 3)
        self.assertEqual({entry["时间"] for entry in first_chord}, {0.0})
        phrase_shapes = {entry["推导"]["乐句"] for entry in first_chord}
        self.assertEqual(len(phrase_shapes), 1)
        self.assertIn("句首推", next(iter(phrase_shapes)))

    def test_part_default_articulation_shapes_like_the_same_note_marking(self) -> None:
        """声部默认断奏不仅要切采样，也要与逐音断奏使用同一表情塑形。"""

        def make_plan(*, note_marking: bool):
            part = {
                "id": "strings",
                "default_dynamic": "mf",
                "notes": [
                    {
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 2,
                        "pitch": "C4",
                        **({"articulation": "staccato"} if note_marking else {}),
                    }
                ],
            }
            if not note_marking:
                part["default_articulation"] = "staccato"
            score = parse_score_document(
                {
                    "tempo_map": [
                        {"bar": 1, "bpm": 60, "beats_per_bar": 4, "beat_unit": 4}
                    ],
                    "parts": [part],
                }
            )
            roster = parse_roster_document(
                {
                    "assignments": [
                        {
                            "part": "strings",
                            "instrument": "管弦乐/弦乐组/低音提琴",
                        }
                    ]
                },
                self.capabilities,
            )
            return build_plan(
                score,
                roster,
                ExpressionSettings.from_dict(
                    {"mode": "ensemble", "humanize": {"depth": 0.0}}
                ),
            )

        inherited = make_plan(note_marking=False).parts[0].trace[0]
        explicit = make_plan(note_marking=True).parts[0].trace[0]
        self.assertEqual(inherited["奏法"], "staccato")
        self.assertEqual(inherited["时长"], explicit["时长"])
        self.assertEqual(inherited["力度"], explicit["力度"])
        self.assertEqual(
            inherited["推导"]["奏法记号"], explicit["推导"]["奏法记号"]
        )

    def test_strict_mode_drops_structural_expression(self) -> None:
        strict = build_plan(
            self.score, self.roster, ExpressionSettings.from_dict({"mode": "strict"})
        )
        drums = next(
            part for part in strict.parts if part.executor.executor_id == "drums.F#2"
        )
        downbeat = next(item for item in drums.trace if item["拍"] == 1.0)
        self.assertNotIn("节拍重音", downbeat["推导"])
        self.assertNotIn("残差随机", downbeat["推导"])

    def test_the_same_seed_reproduces_the_plan_exactly(self) -> None:
        first = json.dumps(self._plan().to_dict(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(self._plan().to_dict(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_a_different_seed_changes_the_plan(self) -> None:
        other = build_plan(
            self.score,
            self.roster,
            ExpressionSettings.from_dict({"mode": "ensemble", "humanize": {"seed": 1}}),
        )
        self.assertNotEqual(
            json.dumps(self._plan().to_dict(), ensure_ascii=False, sort_keys=True),
            json.dumps(other.to_dict(), ensure_ascii=False, sort_keys=True),
        )

    def test_editing_one_part_leaves_the_others_byte_identical(self) -> None:
        """按音符身份取随机数,而不是按流水顺序,改一处不该动到别处。"""

        baseline = self._plan()
        edited_document = copy.deepcopy(_load_score())
        for part in edited_document["parts"]:
            if part["id"] == "piano":
                part["notes"].append(
                    {
                        "event_id": "piano-added-note",
                        "bar": 2,
                        "beat": 4,
                        "duration_beats": 0.5,
                        "pitch": "D5",
                    }
                )
        edited = build_plan(
            parse_score_document(edited_document),
            self.roster,
            ExpressionSettings.from_dict(
                {"mode": "ensemble", "humanize": {"seed": 20260722}}
            ),
        )
        for executor_id in ("drums.C2", "drums.D2", "bass", "strings"):
            before = next(
                part for part in baseline.parts if part.executor.executor_id == executor_id
            )
            after = next(
                part for part in edited.parts if part.executor.executor_id == executor_id
            )
            self.assertEqual(
                before.performance["events"],
                after.performance["events"],
                f"{executor_id} 不该因为钢琴多了一个音而改变",
            )

    def test_note_offs_close_their_own_notes(self) -> None:
        plan = self._plan()
        for part in plan.parts:
            open_ids: set[int] = set()
            for event in part.performance["events"]:
                if event["type"] == "note_on":
                    self.assertNotIn(event["note_id"], open_ids)
                    open_ids.add(event["note_id"])
                elif event["type"] == "note_off":
                    self.assertIn(event["note_id"], open_ids)
                    open_ids.remove(event["note_id"])
            self.assertEqual(open_ids, set())

    def test_articulation_events_are_only_emitted_on_change(self) -> None:
        plan = self._plan()
        strings = next(part for part in plan.parts if part.executor.part_id == "strings")
        articulations = [
            event for event in strings.performance["events"] if event["type"] == "articulation"
        ]
        self.assertEqual(len(articulations), 1)
        self.assertEqual(articulations[0]["name"], "sustain")

    def test_percussion_is_sent_its_own_fixed_note(self) -> None:
        plan = self._plan()
        kick = next(part for part in plan.parts if part.executor.executor_id == "drums.C2")
        for event in kick.performance["events"]:
            if event["type"] == "note_on":
                self.assertEqual(event["midi_note"], 60.0)


class MixBusTests(unittest.TestCase):
    def test_balance_keeps_the_centre_at_unity(self) -> None:
        self.assertEqual(balance_gains(0.0), (1.0, 1.0))

    def test_balance_only_ever_attenuates(self) -> None:
        for pan in (-1.0, -0.5, 0.0, 0.25, 1.0):
            left, right = balance_gains(pan)
            self.assertLessEqual(left, 1.0)
            self.assertLessEqual(right, 1.0)
            self.assertGreaterEqual(min(left, right), 0.0)


class NormalizeTests(unittest.TestCase):
    """峰值归一是可选的成品电平层,默认关,开了要如实记录施加的增益。"""

    def setUp(self) -> None:
        self.capabilities = load_capabilities(ROOT / "乐器")
        score = parse_score_document(
            {
                "schema_version": 1,
                "title": "自包含归一化测试",
                "sample_rate": 8_000,
                "tail_seconds": 0.05,
                "tempo_map": [
                    {
                        "bar": 1,
                        "bpm": 120,
                        "beats_per_bar": 4,
                        "beat_unit": 4,
                    }
                ],
                "parts": [
                    {
                        "id": "tone",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 0.5,
                                "pitch": "A4",
                                "velocity": 0.8,
                                "event_id": "normalize-tone-001",
                            }
                        ],
                    }
                ],
            }
        )
        roster = parse_roster_document(
            {
                "name": "自包含归一化测试",
                "assignments": [
                    {
                        "part": "tone",
                        "instrument": "测试工具/参考振荡器",
                    }
                ],
            },
            self.capabilities,
        )
        self.plan = build_plan(
            score, roster, ExpressionSettings.from_dict({"mode": "strict"})
        )
        self._tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_off_by_default_leaves_the_mix_untouched(self) -> None:
        result = render_plan(self.plan, self.directory / "plain", write_stems=False)
        self.assertIsNone(result.pre_normalize_peak)
        self.assertNotIn("normalize", result.to_dict())

    def test_normalized_peak_lands_on_the_target(self) -> None:
        result = render_plan(
            self.plan,
            self.directory / "norm",
            write_stems=False,
            normalize_peak_db=-1.0,
        )
        target = 10.0 ** (-1.0 / 20.0)
        self.assertAlmostEqual(result.mix_peak, target, places=4)
        self.assertIsNotNone(result.pre_normalize_peak)
        self.assertIn("normalize", result.to_dict())

    def test_normalization_is_a_single_scalar_not_a_rebalance(self) -> None:
        """归一只能整体乘一个标量:目标峰值与实测峰值之比。"""

        plain = render_plan(self.plan, self.directory / "a", write_stems=False)
        norm = render_plan(
            self.plan,
            self.directory / "b",
            write_stems=False,
            normalize_peak_db=-1.0,
        )
        expected_gain = 20.0 * math.log10(
            (10.0 ** (-1.0 / 20.0)) / plain.mix_peak
        )
        self.assertAlmostEqual(norm.normalize_gain_db, expected_gain, places=3)

    def test_positive_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "dBFS"):
            render_plan(
                self.plan,
                self.directory / "bad",
                write_stems=False,
                normalize_peak_db=3.0,
            )


if __name__ == "__main__":
    unittest.main()
