from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator

from tianlai.capability import InstrumentCapability
from tianlai.cli import main as cli_main
from tianlai.midi_import import build_roster_draft, read_midi
from tianlai.roster import parse_roster_document


ROOT = Path(__file__).resolve().parents[1]
TEST_INSTRUMENT_PATH = "测试/MIDI草稿乐器"
TEST_CAPABILITIES = {
    TEST_INSTRUMENT_PATH: InstrumentCapability(
        name="MIDI 草稿测试乐器",
        relative_path=TEST_INSTRUMENT_PATH,
        manifest_path=f"{TEST_INSTRUMENT_PATH}/乐器.json",
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
}


def _vlq(value: int) -> bytes:
    """Encode one non-negative SMF variable-length quantity."""

    if value < 0:
        raise ValueError("SMF delta time cannot be negative")
    encoded = bytearray((value & 0x7F,))
    value >>= 7
    while value:
        encoded.insert(0, 0x80 | (value & 0x7F))
        value >>= 7
    return bytes(encoded)


def _track(events: list[tuple[int, bytes]]) -> bytes:
    body = b"".join(_vlq(delta) + event for delta, event in events)
    body += b"\x00\xff\x2f\x00"
    return b"MTrk" + len(body).to_bytes(4, "big") + body


def _track_name(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return b"\xff\x03" + _vlq(len(encoded)) + encoded


def _midi_header(midi_format: int, track_count: int) -> bytes:
    return b"".join(
        (
            b"MThd",
            (6).to_bytes(4, "big"),
            midi_format.to_bytes(2, "big"),
            track_count.to_bytes(2, "big"),
            (480).to_bytes(2, "big"),
        )
    )


def _midi_fixture() -> bytes:
    """Build a format-1 SMF covering all mixer-evidence branches.

    Track and event indices are deliberately observable:

    * track 1 contains two channels with the same track name;
    * channel 1 repeats one constant CC10 before and after its first note;
    * channel 2 changes CC10;
    * track 2/channel 3 has no CC10;
    * track 3/channel 4 sends a constant CC10 at the first note's tick, but
      after that note-on in stream order.

    Several controller messages use running status, while Program Change uses
    its one-byte SMF payload.  This catches parsers that consume the following
    delta time as a second Program Change byte.
    """

    conductor = _track(
        [
            (0, _track_name("Conductor")),
            (0, b"\xff\x51\x03\x07\xa1\x20"),  # 120 BPM
            (0, b"\xff\x58\x04\x04\x02\x18\x08"),  # 4/4
        ]
    )
    shared = _track(
        [
            (0, _track_name("Shared")),
            (0, b"\xc0\x28"),  # channel 1, raw program 40
            (0, b"\xb0\x07\x64"),  # CC7 = 100
            (0, b"\x0a\x20"),  # running status: CC10 = 32
            (0, b"\x0b\x5a"),  # running status: CC11 = 90
            (0, b"\x90\x3c\x50"),  # channel 1 note-on
            (0, b"\xc1\x29"),  # channel 2, raw program 41
            (0, b"\xb1\x0a\x14"),  # CC10 = 20
            (0, b"\x07\x46"),  # running status: CC7 = 70
            (0, b"\x0b\x64"),  # running status: CC11 = 100
            (0, b"\x91\x3e\x58"),  # channel 2 note-on
            (240, b"\xb0\x0a\x20"),  # channel 1 repeats CC10 = 32
            (0, b"\xb1\x0a\x64"),  # channel 2 changes CC10 to 100
            (240, b"\x80\x3c\x40"),
            (0, b"\x81\x3e\x40"),
        ]
    )
    no_pan = _track(
        [
            (0, _track_name("No Pan")),
            (0, b"\xc2\x2a"),
            (0, b"\xb2\x07\x40"),  # CC7 = 64
            (0, b"\x0b\x50"),  # running status: CC11 = 80
            (0, b"\x92\x43\x60"),
            (480, b"\x82\x43\x40"),
        ]
    )
    late_pan = _track(
        [
            (0, _track_name("Late Pan")),
            (0, b"\xc3\x2b"),
            (0, b"\x93\x45\x60"),
            (0, b"\xb3\x0a\x60"),  # same tick, but after note-on
            (480, b"\x83\x45\x40"),
        ]
    )
    tracks = (conductor, shared, no_pan, late_pan)
    return b"".join(
        (
            b"MThd",
            (6).to_bytes(4, "big"),
            (1).to_bytes(2, "big"),
            len(tracks).to_bytes(2, "big"),
            (480).to_bytes(2, "big"),
            *tracks,
        )
    )


def _controller_safety_fixture() -> bytes:
    """Build CC42/CC121 blockers plus one General MIDI percussion part."""

    conductor = _track(
        [
            (0, _track_name("Conductor")),
            (0, b"\xff\x51\x03\x07\xa1\x20"),
            (0, b"\xff\x58\x04\x04\x02\x18\x08"),
        ]
    )
    cc42_blocked = _track(
        [
            (0, _track_name("CC42 Blocked")),
            (0, b"\xc4\x2c"),
            (0, b"\xb4\x0a\x20"),  # CC10 = 32
            (0, b"\x2a\x01"),  # running status: CC42 pan LSB
            (0, b"\x01\x40"),  # running status: unsupported CC1
            (0, b"\x94\x3c\x58"),
            (480, b"\x84\x3c\x40"),
        ]
    )
    cc121_blocked = _track(
        [
            (0, _track_name("CC121 Blocked")),
            (0, b"\xc5\x2d"),
            (0, b"\xb5\x0a\x60"),  # CC10 = 96
            (0, b"\x79\x00"),  # running status: reset all controllers
            (0, b"\x95\x40\x58"),
            (480, b"\x85\x40\x40"),
        ]
    )
    percussion = _track(
        [
            (0, _track_name("Drums")),
            (0, b"\xc9\x00"),
            (0, b"\xb9\x07\x64"),
            (0, b"\x99\x24\x64"),  # channel 10, GM bass-drum key
            (480, b"\x89\x24\x40"),
        ]
    )
    tracks = (conductor, cc42_blocked, cc121_blocked, percussion)
    return b"".join(
        (
            b"MThd",
            (6).to_bytes(4, "big"),
            (1).to_bytes(2, "big"),
            len(tracks).to_bytes(2, "big"),
            (480).to_bytes(2, "big"),
            *tracks,
        )
    )


def _overlapping_and_unbalanced_notes_fixture() -> bytes:
    """Exercise same-pitch overlap plus visible repair of malformed notes."""

    notes = _track(
        [
            (0, _track_name("Overlap")),
            (0, b"\x90\x3c\x40"),      # C4, first note
            (120, b"\x90\x3c\x60"),    # C4 overlaps the first
            (120, b"\x80\x3c\x40"),    # closes first C4 (FIFO)
            (120, b"\x80\x3c\x40"),    # closes second C4
            (0, b"\x80\x3e\x40"),      # unmatched D4 note-off
            (0, b"\x90\x40\x50"),      # E4 never receives a note-off
            (120, b"\xff\x01\x00"),    # make the declared track end audible
        ]
    )
    return _midi_header(0, 1) + notes


def _mid_bar_meter_change_fixture() -> bytes:
    notes = _track(
        [
            (0, b"\xff\x58\x04\x04\x02\x18\x08"),
            (0, b"\x90\x3c\x50"),
            (240, b"\xff\x58\x04\x03\x02\x18\x08"),
            (240, b"\x80\x3c\x40"),
        ]
    )
    return _midi_header(0, 1) + notes


def _conflicting_tempo_and_unhandled_meta_fixture() -> bytes:
    notes = _track(
        [
            (0, b"\xff\x51\x03\x07\xa1\x20"),  # 120 BPM, kept
            (0, b"\xff\x51\x03\x09\x27\xc0"),  # 100 BPM, conflict
            (0, b"\xff\x05\x02la"),              # lyric meta
            (0, b"\xf0\x02\x01\x02"),            # SysEx payload
            (0, b"\x90\x3c\x50"),
            (480, b"\x80\x3c\x40"),
        ]
    )
    return _midi_header(0, 1) + notes + b"TAIL"


def _canonical_sha256(document: object) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _by_part(items: list[dict]) -> dict[str, dict]:
    return {item["part"]: item for item in items}


def _report_by_id(report_document: dict) -> dict[str, dict]:
    return {item["id"]: item for item in report_document["parts"]}


def _event_projection(
    events: list[dict],
    value_key: str,
) -> list[tuple[int, int, int]]:
    return [
        (event["tick"], event["track_event_index"], event[value_key])
        for event in events
    ]


class MidiRosterDraftTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._temporary_directory = tempfile.TemporaryDirectory()
        cls.midi_path = (
            Path(cls._temporary_directory.name) / "mixer-evidence.mid"
        )
        cls.midi_bytes = _midi_fixture()
        cls.midi_path.write_bytes(cls.midi_bytes)
        cls.safety_midi_path = (
            Path(cls._temporary_directory.name) / "controller-safety.mid"
        )
        cls.safety_midi_bytes = _controller_safety_fixture()
        cls.safety_midi_path.write_bytes(cls.safety_midi_bytes)

        cls.schema = json.loads(
            (ROOT / "schemas" / "midi-roster-draft.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary_directory.cleanup()

    def _read_and_build(self) -> tuple[dict, object, dict]:
        score, report = read_midi(self.midi_path)
        return score, report, build_roster_draft(score, report)

    def _read_and_build_safety(self) -> tuple[dict, object, dict]:
        score, report = read_midi(self.safety_midi_path)
        return score, report, build_roster_draft(score, report)

    def _assert_schema_valid(self, draft: dict) -> None:
        errors = sorted(
            self.validator.iter_errors(draft),
            key=lambda error: tuple(
                str(item) for item in error.absolute_path
            ),
        )
        self.assertEqual([], errors, [error.message for error in errors])

    def _assert_score_and_draft_consistent(
        self,
        score: dict,
        draft: dict,
        midi_bytes: bytes,
    ) -> None:
        score_ids = [part["id"] for part in score["parts"]]
        self.assertEqual(
            draft["source"]["midi"]["sha256"],
            hashlib.sha256(midi_bytes).hexdigest(),
        )
        self.assertEqual(
            draft["source"]["score"]["canonical_sha256"],
            _canonical_sha256(score),
        )
        self.assertEqual(
            [
                assignment["part"]
                for assignment in draft["draft_roster"]["assignments"]
            ],
            score_ids,
        )
        self.assertEqual(
            [item["part"] for item in draft["part_evidence"]],
            score_ids,
        )

    def _assert_midi_rejected(
        self,
        filename: str,
        payload: bytes,
        message_pattern: str,
    ) -> None:
        path = Path(self._temporary_directory.name) / filename
        path.write_bytes(payload)
        with self.assertRaisesRegex(ValueError, message_pattern):
            read_midi(path)

    def test_rejects_mthd_length_shorter_than_six_bytes(self) -> None:
        self._assert_midi_rejected(
            "short-header.mid",
            b"MThd" + (5).to_bytes(4, "big") + bytes(5),
            r"MThd.*小于 6",
        )

    def test_rejects_oversized_midi_before_reading_payload(self) -> None:
        path = Path(self._temporary_directory.name) / "oversized.mid"
        with path.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, r"超过 64 MiB"):
            read_midi(path)

    def test_path_import_uses_one_descriptor_instead_of_path_reopen(self) -> None:
        path = Path(self._temporary_directory.name) / "single-open.mid"
        path.write_bytes(_midi_fixture())
        with (
            mock.patch.object(
                Path,
                "stat",
                side_effect=AssertionError("pathname stat is forbidden"),
            ),
            mock.patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("pathname reopen is forbidden"),
            ),
        ):
            document, _report = read_midi(path)

        self.assertEqual(document["schema_version"], 1)

    def test_rejects_format_zero_with_multiple_tracks(self) -> None:
        self._assert_midi_rejected(
            "format-zero-two-tracks.mid",
            _midi_header(0, 2),
            r"format 0.*一个轨道",
        )

    def test_rejects_excessive_declared_track_count_before_looping(self) -> None:
        self._assert_midi_rejected(
            "too-many-tracks.mid",
            _midi_header(1, 1025),
            r"轨道数超过 1024",
        )

    def test_rejects_zero_microseconds_per_quarter_tempo(self) -> None:
        tempo_zero = b"\x00\xff\x51\x03\x00\x00\x00"
        payload = b"".join(
            (
                _midi_header(0, 1),
                b"MTrk",
                len(tempo_zero).to_bytes(4, "big"),
                tempo_zero,
            )
        )
        self._assert_midi_rejected(
            "zero-tempo.mid",
            payload,
            r"值为 0 的非法速度",
        )

    def test_rejects_track_length_past_end_of_file(self) -> None:
        payload = b"".join(
            (
                _midi_header(0, 1),
                b"MTrk",
                (4).to_bytes(4, "big"),
                b"\x00",
            )
        )
        self._assert_midi_rejected(
            "track-past-eof.mid",
            payload,
            r"轨道 0.*声明长度超过文件边界",
        )

    def test_same_pitch_overlap_is_preserved_and_repairs_are_reported(
        self,
    ) -> None:
        path = Path(self._temporary_directory.name) / "overlap.mid"
        path.write_bytes(_overlapping_and_unbalanced_notes_fixture())

        score, report = read_midi(path)

        notes = score["parts"][0]["notes"]
        self.assertEqual(len(notes), 3)
        self.assertEqual(
            [
                (
                    note["pitch"],
                    note["beat"],
                    note["duration_beats"],
                    note["velocity"],
                )
                for note in notes
            ],
            [
                ("C4", 1.0, 0.5, round(64 / 127, 6)),
                ("C4", 1.25, 0.5, round(96 / 127, 6)),
                ("E4", 1.75, 0.25, round(80 / 127, 6)),
            ],
        )
        warning_text = "\n".join(report.warnings)
        self.assertIn("同通道同音重叠", warning_text)
        self.assertIn("FIFO", warning_text)
        self.assertIn("找不到对应 note-on", warning_text)
        self.assertIn("轨道结束位置补齐", warning_text)

    def test_mid_bar_time_signature_change_is_rejected_not_relabelled(
        self,
    ) -> None:
        self._assert_midi_rejected(
            "mid-bar-meter.mid",
            _mid_bar_meter_change_fixture(),
            r"拍号变化不在小节线上",
        )

    def test_conflicting_tempo_and_unhandled_stream_data_are_visible(
        self,
    ) -> None:
        path = Path(self._temporary_directory.name) / "semantic-loss.mid"
        path.write_bytes(_conflicting_tempo_and_unhandled_meta_fixture())

        score, report = read_midi(path)

        self.assertEqual(score["tempo_map"][0]["bpm"], 120.0)
        warning_text = "\n".join(report.warnings)
        self.assertIn("冲突速度事件", warning_text)
        self.assertIn("meta 事件类型：0x05", warning_text)
        self.assertIn("SysEx", warning_text)
        self.assertIn("声明轨道结束后仍有 4 字节", warning_text)

    def test_read_midi_binds_source_and_canonical_score_hashes(self) -> None:
        score, report = read_midi(self.midi_path)
        report_document = report.to_dict()

        self.assertEqual(
            report_document["source_midi_sha256"],
            hashlib.sha256(self.midi_bytes).hexdigest(),
        )
        self.assertEqual(
            report_document["score_canonical_sha256"],
            _canonical_sha256(score),
        )
        self.assertEqual(report_document["ticks_per_quarter"], 480)
        self.assertEqual(report_document["source_midi_byte_length"], len(
            self.midi_bytes
        ))
        self.assertEqual(report_document["midi_format"], 1)
        self.assertEqual(report_document["track_count"], 4)

        draft = build_roster_draft(score, report)
        self.assertEqual(
            draft["source"],
            {
                "midi": {
                    "sha256": report_document["source_midi_sha256"],
                    "byte_length": len(self.midi_bytes),
                    "smf_format": 1,
                    "track_count": 4,
                    "ticks_per_quarter": 480,
                },
                "score": {
                    "canonical_sha256": report_document[
                        "score_canonical_sha256"
                    ],
                    "canonicalization": "tianlai-json-v1",
                },
            },
        )

    def test_smf_mixer_events_keep_track_channel_tick_and_stream_order(
        self,
    ) -> None:
        _score, report = read_midi(self.midi_path)
        parts = _report_by_id(report.to_dict())
        self.assertEqual(
            set(parts),
            {"Shared", "Shared_2", "No Pan", "Late Pan"},
        )

        constant = parts["Shared"]
        self.assertEqual(
            (
                constant["track_index_0based"],
                constant["channel_1based"],
                constant["track_name"],
                constant["percussion"],
            ),
            (1, 1, "Shared", False),
        )
        self.assertEqual(
            _event_projection(
                constant["program_changes"],
                "program_0_127",
            ),
            [(0, 1, 40)],
        )
        self.assertEqual(
            _event_projection(constant["cc7_volume"], "value_0_127"),
            [(0, 2, 100)],
        )
        self.assertEqual(
            _event_projection(constant["cc10_pan"], "value_0_127"),
            [(0, 3, 32), (240, 11, 32)],
        )
        self.assertEqual(
            _event_projection(constant["cc11_expression"], "value_0_127"),
            [(0, 4, 90)],
        )
        self.assertEqual(
            (
                constant["cc10_pan"][1]["bar"],
                constant["cc10_pan"][1]["beat"],
            ),
            (1, 1.5),
        )

        varying = parts["Shared_2"]
        self.assertEqual(
            (
                varying["track_index_0based"],
                varying["channel_1based"],
            ),
            (1, 2),
        )
        self.assertEqual(
            _event_projection(varying["program_changes"], "program_0_127"),
            [(0, 6, 41)],
        )
        self.assertEqual(
            _event_projection(varying["cc7_volume"], "value_0_127"),
            [(0, 8, 70)],
        )
        self.assertEqual(
            _event_projection(varying["cc10_pan"], "value_0_127"),
            [(0, 7, 20), (240, 12, 100)],
        )
        self.assertEqual(
            _event_projection(varying["cc11_expression"], "value_0_127"),
            [(0, 9, 100)],
        )

        missing = parts["No Pan"]
        self.assertEqual(
            (
                missing["track_index_0based"],
                missing["channel_1based"],
            ),
            (2, 3),
        )
        self.assertEqual(missing["cc10_pan"], [])
        self.assertEqual(
            _event_projection(missing["cc7_volume"], "value_0_127"),
            [(0, 2, 64)],
        )
        self.assertEqual(
            _event_projection(missing["cc11_expression"], "value_0_127"),
            [(0, 3, 80)],
        )

        late = parts["Late Pan"]
        self.assertEqual(
            (
                late["track_index_0based"],
                late["channel_1based"],
                late["first_note_tick"],
                late["first_note_event_index"],
            ),
            (3, 4, 0, 2),
        )
        self.assertEqual(
            _event_projection(late["cc10_pan"], "value_0_127"),
            [(0, 3, 96)],
        )

    def test_draft_contract_is_schema_valid_but_not_an_executable_roster(
        self,
    ) -> None:
        score, _report, draft = self._read_and_build()
        self.assertEqual(
            set(draft),
            {
                "format",
                "version",
                "status",
                "executable",
                "source",
                "draft_roster",
                "part_evidence",
                "notice",
            },
        )
        self.assertEqual(draft["format"], "tianlai.midi_roster_draft")
        self.assertEqual(draft["version"], 1)
        self.assertEqual(
            draft["status"],
            "requires_creator_confirmation",
        )
        self.assertIs(draft["executable"], False)
        self._assert_schema_valid(draft)

        score_ids = [part["id"] for part in score["parts"]]
        assignments = draft["draft_roster"]["assignments"]
        evidence = draft["part_evidence"]
        self.assertEqual(
            [assignment["part"] for assignment in assignments],
            score_ids,
        )
        self.assertEqual(
            [item["part"] for item in evidence],
            score_ids,
        )
        self.assertEqual(
            {
                item["part"]: item["source"]
                for item in evidence
            },
            {
                "Shared": {
                    "track_index_0based": 1,
                    "channel_1based": 1,
                    "track_name": "Shared",
                    "percussion": False,
                },
                "Shared_2": {
                    "track_index_0based": 1,
                    "channel_1based": 2,
                    "track_name": "Shared",
                    "percussion": False,
                },
                "No Pan": {
                    "track_index_0based": 2,
                    "channel_1based": 3,
                    "track_name": "No Pan",
                    "percussion": False,
                },
                "Late Pan": {
                    "track_index_0based": 3,
                    "channel_1based": 4,
                    "track_name": "Late Pan",
                    "percussion": False,
                },
            },
        )
        for assignment in assignments:
            with self.subTest(part=assignment["part"]):
                self.assertIsNone(assignment["instrument"])
                self.assertNotIn("kit", assignment)
                self.assertEqual(assignment["gain_db"], 0.0)
                self.assertEqual(
                    assignment["role"],
                    {
                        "function": "other",
                        "prominence": "midground",
                    },
                )
        for item in evidence:
            with self.subTest(part=item["part"], field="decisions"):
                self.assertEqual(
                    item["decisions"]["routing"],
                    "instrument_required",
                )
                self.assertEqual(
                    item["decisions"]["role"],
                    "default_other_midground_creator_may_override",
                )
                self.assertEqual(
                    item["decisions"]["balance_relations"],
                    "optional_creator_owned",
                )
        self.assertEqual(
            {
                assignment["part"]: assignment["pan"]
                for assignment in assignments
            },
            {
                "Shared": -0.5,
                "Shared_2": 0.0,
                "No Pan": 0.0,
                "Late Pan": 0.0,
            },
        )

        with self.assertRaisesRegex(ValueError, "未知字段"):
            parse_roster_document(draft, {})
        with self.assertRaises((TypeError, ValueError)):
            parse_roster_document(draft["draft_roster"], {})

    def test_confirmed_pitched_draft_can_use_formal_roster_defaults(
        self,
    ) -> None:
        _score, _report, draft = self._read_and_build()
        draft_without_optional_defaults = copy.deepcopy(draft)
        roster_document = draft_without_optional_defaults["draft_roster"]
        for assignment in roster_document["assignments"]:
            self.assertIn("instrument", assignment)
            assignment.pop("gain_db")
            assignment.pop("pan")
            assignment.pop("role")

        self._assert_schema_valid(draft_without_optional_defaults)
        for assignment in roster_document["assignments"]:
            assignment["instrument"] = TEST_INSTRUMENT_PATH
        roster = parse_roster_document(
            roster_document,
            TEST_CAPABILITIES,
        )
        self.assertEqual(
            [executor.part_id for executor in roster.executors],
            ["Shared", "Shared_2", "No Pan", "Late Pan"],
        )
        for executor in roster.executors:
            with self.subTest(part=executor.part_id):
                self.assertEqual(
                    executor.capability.relative_path,
                    TEST_INSTRUMENT_PATH,
                )
                self.assertEqual(executor.gain_db, 0.0)
                self.assertEqual(executor.pan, 0.0)
                self.assertIsNone(executor.role)

    def test_cc10_candidates_are_static_pre_note_evidence_only(self) -> None:
        _score, _report, draft = self._read_and_build()
        evidence = _by_part(draft["part_evidence"])

        constant = evidence["Shared"]
        self.assertEqual(
            constant["cc10_pan"]["status"],
            "static",
        )
        self.assertEqual(constant["cc10_pan"]["distinct_values"], [32])
        self.assertEqual(
            constant["pan_candidate"],
            {
                "status": "candidate",
                "value": -0.5,
                "source_value_0_127": 32,
                "reason": "constant_cc10_effective_before_first_note",
                "blockers": [],
            },
        )
        self.assertEqual(
            constant["decisions"]["pan"],
            "default_from_midi_candidate_creator_may_override",
        )

        varying = evidence["Shared_2"]
        self.assertEqual(varying["cc10_pan"]["status"], "varying")
        self.assertEqual(
            varying["cc10_pan"]["distinct_values"],
            [20, 100],
        )
        self.assertEqual(
            varying["pan_candidate"],
            {
                "status": "requires_creator_decision",
                "value": None,
                "source_value_0_127": None,
                "reason": "cc10_changes_over_time",
                "blockers": [],
            },
        )

        missing = evidence["No Pan"]
        self.assertEqual(missing["cc10_pan"]["status"], "missing")
        self.assertEqual(missing["cc10_pan"]["distinct_values"], [])
        self.assertEqual(
            missing["pan_candidate"],
            {
                "status": "absent",
                "value": None,
                "source_value_0_127": None,
                "reason": "no_cc10_event",
                "blockers": [],
            },
        )

        late = evidence["Late Pan"]
        self.assertEqual(late["cc10_pan"]["status"], "static")
        self.assertEqual(
            late["pan_candidate"],
            {
                "status": "requires_creator_decision",
                "value": None,
                "source_value_0_127": None,
                "reason": "first_cc10_after_first_note",
                "blockers": [],
            },
        )

        # Only one unambiguous pre-note value may seed the reversible default.
        self.assertEqual(
            {
                assignment["part"]: assignment["pan"]
                for assignment in draft["draft_roster"]["assignments"]
            },
            {
                "Shared": -0.5,
                "Shared_2": 0.0,
                "No Pan": 0.0,
                "Late Pan": 0.0,
            },
        )

    def test_cc42_and_cc121_block_otherwise_static_pan_candidates(
        self,
    ) -> None:
        _score, report, draft = self._read_and_build_safety()
        report_parts = _report_by_id(report.to_dict())
        evidence = _by_part(draft["part_evidence"])
        assignments = _by_part(draft["draft_roster"]["assignments"])

        cc42_report = report_parts["CC42 Blocked"]
        self.assertEqual(
            _event_projection(cc42_report["cc10_pan"], "value_0_127"),
            [(0, 2, 32)],
        )
        self.assertEqual(
            _event_projection(cc42_report["cc42_pan_lsb"], "value_0_127"),
            [(0, 3, 1)],
        )
        self.assertEqual(
            cc42_report["unhandled_midi_messages"],
            ["cc_1"],
        )
        cc42 = evidence["CC42 Blocked"]
        self.assertEqual(cc42["cc42_pan_lsb"]["status"], "static")
        self.assertEqual(cc42["cc42_pan_lsb"]["distinct_values"], [1])
        self.assertEqual(cc42["cc121_reset_all_controllers"]["status"], "missing")
        self.assertEqual(cc42["unhandled_midi_messages"], ["cc_1"])
        self.assertEqual(
            cc42["pan_candidate"],
            {
                "status": "requires_creator_decision",
                "value": None,
                "source_value_0_127": None,
                "reason": "unsupported_pan_controller_semantics",
                "blockers": ["cc42_pan_lsb_present"],
            },
        )

        cc121_report = report_parts["CC121 Blocked"]
        self.assertEqual(
            _event_projection(cc121_report["cc10_pan"], "value_0_127"),
            [(0, 2, 96)],
        )
        self.assertEqual(
            _event_projection(
                cc121_report["cc121_reset_all_controllers"],
                "value_0_127",
            ),
            [(0, 3, 0)],
        )
        cc121 = evidence["CC121 Blocked"]
        self.assertEqual(cc121["cc42_pan_lsb"]["status"], "missing")
        self.assertEqual(
            cc121["cc121_reset_all_controllers"]["status"],
            "static",
        )
        self.assertEqual(
            cc121["cc121_reset_all_controllers"]["distinct_values"],
            [0],
        )
        self.assertEqual(
            cc121["pan_candidate"],
            {
                "status": "requires_creator_decision",
                "value": None,
                "source_value_0_127": None,
                "reason": "unsupported_pan_controller_semantics",
                "blockers": ["cc121_reset_all_controllers_present"],
            },
        )

        for part_id in ("CC42 Blocked", "CC121 Blocked"):
            with self.subTest(part=part_id):
                self.assertEqual(assignments[part_id]["pan"], 0.0)
                self.assertEqual(
                    evidence[part_id]["decisions"]["pan"],
                    "default_center_creator_may_override",
                )

    def test_percussion_channel_produces_unresolved_kit_draft(self) -> None:
        _score, _report, draft = self._read_and_build_safety()
        self._assert_schema_valid(draft)
        assignments = _by_part(draft["draft_roster"]["assignments"])
        evidence = _by_part(draft["part_evidence"])

        self.assertEqual(
            assignments["Drums"],
            {
                "part": "Drums",
                "kit": None,
                "gain_db": 0.0,
                "pan": 0.0,
                "role": {
                    "function": "other",
                    "prominence": "midground",
                },
            },
        )
        self.assertNotIn("instrument", assignments["Drums"])
        self.assertEqual(
            evidence["Drums"]["source"],
            {
                "track_index_0based": 3,
                "channel_1based": 10,
                "track_name": "Drums",
                "percussion": True,
            },
        )
        self.assertEqual(
            evidence["Drums"]["decisions"]["routing"],
            "kit_required",
        )
        for part_id in ("CC42 Blocked", "CC121 Blocked"):
            with self.subTest(part=part_id):
                self.assertIn("instrument", assignments[part_id])
                self.assertNotIn("kit", assignments[part_id])
                self.assertIsNone(assignments[part_id]["instrument"])
                self.assertEqual(
                    evidence[part_id]["decisions"]["routing"],
                    "instrument_required",
                )

    def test_cc7_and_cc11_never_become_gain_db(self) -> None:
        _score, _report, draft = self._read_and_build()
        evidence = _by_part(draft["part_evidence"])
        assignments = _by_part(draft["draft_roster"]["assignments"])

        self.assertEqual(
            evidence["Shared"]["cc7_volume"]["distinct_values"],
            [100],
        )
        self.assertEqual(
            evidence["Shared"]["cc11_expression"]["distinct_values"],
            [90],
        )
        self.assertEqual(
            evidence["No Pan"]["cc7_volume"]["distinct_values"],
            [64],
        )
        self.assertEqual(
            evidence["No Pan"]["cc11_expression"]["distinct_values"],
            [80],
        )
        for part_id, item in evidence.items():
            with self.subTest(part=part_id):
                self.assertEqual(
                    item["gain_db_candidate"],
                    {
                        "status": "not_derived",
                        "value": None,
                        "fallback_default_db": 0.0,
                        "reason": "cc7_cc11_have_no_portable_db_mapping",
                    },
                )
                self.assertEqual(
                    item["decisions"]["gain_db"],
                    "default_zero_creator_may_override",
                )
                self.assertEqual(assignments[part_id]["gain_db"], 0.0)
                self.assertNotIn("gain_automation", assignments[part_id])

    def test_repeated_import_and_draft_build_are_deterministic(self) -> None:
        first_score, first_report, first_draft = self._read_and_build()
        second_score, second_report, second_draft = self._read_and_build()

        self.assertEqual(first_score, second_score)
        self.assertEqual(first_report.to_dict(), second_report.to_dict())
        self.assertEqual(first_draft, second_draft)
        self.assertEqual(
            json.dumps(
                first_draft,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            json.dumps(
                second_draft,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )

        changed_score = copy.deepcopy(first_score)
        changed_score["tail_seconds"] += 0.25
        with self.assertRaisesRegex(ValueError, "does not match"):
            build_roster_draft(changed_score, first_report)

    def test_cli_import_midi_writes_default_sibling_roster_draft(
        self,
    ) -> None:
        output = (
            Path(self._temporary_directory.name)
            / "cli-default.score.json"
        )
        draft_output = (
            Path(self._temporary_directory.name)
            / "cli-default.roster-draft.json"
        )
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            result = cli_main(
                [
                    "import-midi",
                    "--midi",
                    str(self.midi_path),
                    "--output",
                    str(output),
                ]
            )

        self.assertEqual(result, 0, stdout.getvalue())
        self.assertTrue(output.is_file())
        self.assertTrue(draft_output.is_file())
        score = json.loads(output.read_text(encoding="utf-8"))
        draft = json.loads(draft_output.read_text(encoding="utf-8"))
        self._assert_score_and_draft_consistent(
            score,
            draft,
            self.midi_bytes,
        )
        self._assert_schema_valid(draft)

    def test_cli_import_midi_honors_custom_roster_draft_output(
        self,
    ) -> None:
        output = (
            Path(self._temporary_directory.name)
            / "cli-custom.score.json"
        )
        default_draft_output = (
            Path(self._temporary_directory.name)
            / "cli-custom.roster-draft.json"
        )
        draft_output = (
            Path(self._temporary_directory.name)
            / "custom-drafts"
            / "chosen.json"
        )
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            result = cli_main(
                [
                    "import-midi",
                    "--midi",
                    str(self.midi_path),
                    "--output",
                    str(output),
                    "--roster-draft-output",
                    str(draft_output),
                ]
            )

        self.assertEqual(result, 0, stdout.getvalue())
        self.assertTrue(output.is_file())
        self.assertTrue(draft_output.is_file())
        self.assertFalse(default_draft_output.exists())
        score = json.loads(output.read_text(encoding="utf-8"))
        draft = json.loads(draft_output.read_text(encoding="utf-8"))
        self._assert_score_and_draft_consistent(
            score,
            draft,
            self.midi_bytes,
        )
        self._assert_schema_valid(draft)

    @unittest.skipUnless(
        importlib.util.find_spec("mcp") is not None,
        "optional mcp package is not installed",
    )
    def test_mcp_import_midi_returns_bound_score_draft_and_report(
        self,
    ) -> None:
        from tianlai.mcp_server import import_midi as mcp_import_midi

        with mock.patch.dict(
            "os.environ",
            {"TIANLAI_INPUT_ROOTS": str(self.midi_path.parent)},
        ):
            result = mcp_import_midi(str(self.midi_path))

        self.assertNotIn("error", result)
        self.assertTrue(
            {"score", "roster_draft", "report", "warnings"}.issubset(result)
        )
        score = result["score"]
        draft = result["roster_draft"]
        report = result["report"]
        self.assertEqual(score["schema_version"], 1)
        self.assertTrue(
            all(
                isinstance(note.get("event_id"), str)
                for part in score["parts"]
                for note in part["notes"]
            )
        )
        self._assert_score_and_draft_consistent(
            score,
            draft,
            self.midi_bytes,
        )
        self.assertEqual(
            report["source_midi_sha256"],
            draft["source"]["midi"]["sha256"],
        )
        self.assertEqual(
            report["score_canonical_sha256"],
            draft["source"]["score"]["canonical_sha256"],
        )
        self.assertEqual(result["warnings"], report["warnings"])
        self.assertEqual(
            [part["id"] for part in report["parts"]],
            [part["id"] for part in score["parts"]],
        )
        self._assert_schema_valid(draft)


if __name__ == "__main__":
    unittest.main()
