from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from tianlai.cli import main as cli_main
from tianlai.midi_export import MidiExportLossError, build_midi, export_midi
from tianlai.midi_import import read_midi


def _score() -> dict:
    return {
        "schema_version": 1,
        "title": "MIDI export",
        "sample_rate": 48_000,
        "tail_seconds": 1.0,
        "tempo_map": [
            {
                "bar": 1,
                "beat": 1.0,
                "bpm": 100.0,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "小提琴",
                "name": "小提琴",
                "default_dynamic": "mf",
                "notes": [
                    {
                        "event_id": "violin-1",
                        "bar": 1,
                        "beat": 1.0,
                        "duration_beats": 1.0,
                        "pitch": "C4",
                        "dynamic": "p",
                    },
                    {
                        "event_id": "violin-2",
                        "bar": 1,
                        "beat": 2.0,
                        "duration_beats": 1.0,
                        "pitch": "D4",
                        "dynamic": "f",
                    },
                ],
            }
        ],
    }


class MidiExportTests(unittest.TestCase):
    def test_v1_identity_loss_requires_explicit_approval(self) -> None:
        with self.assertRaises(MidiExportLossError) as raised:
            build_midi(_score())
        report = raised.exception.report
        self.assertFalse(report["ok"])
        self.assertIn(
            "stable_event_ids_not_representable",
            {item["code"] for item in report["losses"]},
        )

    def test_legacy_score_does_not_claim_stable_event_identity_loss(self) -> None:
        score = copy.deepcopy(_score())
        score.pop("schema_version")
        for part in score["parts"]:
            for note in part["notes"]:
                note.pop("event_id")

        _payload, report = build_midi(score, allow_lossy=True)

        self.assertNotIn(
            "stable_event_ids_not_representable",
            {item["code"] for item in report["losses"]},
        )

    def test_round_trip_preserves_note_count_and_distinct_dynamics(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "editing.mid"
            report = export_midi(
                _score(),
                path,
                allow_lossy=True,
            )
            imported, imported_report = read_midi(path)

        self.assertEqual(report["parts"][0]["gm_program_0based"], 40)
        self.assertEqual(
            imported_report.parts[0]["program_changes"][0][
                "program_0_127"
            ],
            40,
        )
        notes = imported["parts"][0]["notes"]
        self.assertEqual(len(notes), 2)
        self.assertEqual([note["dynamic"] for note in notes], ["p", "f"])
        self.assertLess(notes[0]["velocity"], notes[1]["velocity"])

    def test_microtones_and_articulations_are_reported_not_hidden(self) -> None:
        score = _score()
        score["parts"][0]["notes"][0]["pitch"] = 60.5
        score["parts"][0]["notes"][0]["articulation"] = "legato"
        with self.assertRaises(MidiExportLossError) as raised:
            build_midi(score)
        codes = {item["code"] for item in raised.exception.report["losses"]}
        self.assertIn("microtonal_pitch_quantized", codes)
        self.assertIn("articulation_not_representable", codes)

    def test_musicxml_staff_voice_identity_is_reported_as_loss(self) -> None:
        score = _score()
        score["parts"][0]["notes"][0]["staff"] = 1
        score["parts"][0]["notes"][0]["voice"] = "2"
        with self.assertRaises(MidiExportLossError) as raised:
            build_midi(score)
        codes = {item["code"] for item in raised.exception.report["losses"]}
        self.assertIn("staff_voice_identity_not_representable", codes)

    def test_nondefault_tuning_and_actual_midi_quantization_are_reported(self) -> None:
        score = _score()
        score["tuning"] = {"temperament": "equal", "a4_hz": 442.0}
        score["tempo_map"][0]["bpm"] = 123.0
        note = score["parts"][0]["notes"][0]
        note["beat"] = 1.0 + 1.0 / 7.0
        note["duration_beats"] = 1.0 / 7.0
        note["velocity"] = 0.5

        _payload, report = build_midi(score, allow_lossy=True)
        codes = {item["code"] for item in report["losses"]}

        self.assertIn("score_tuning_not_representable", codes)
        self.assertIn("tempo_value_quantized_to_integer_microseconds", codes)
        self.assertIn("note_timing_quantized_to_480_ppq", codes)
        self.assertIn("velocity_quantized_to_midi_7bit", codes)

    def test_extremely_slow_tempo_is_a_reported_clamp_not_overflow(self) -> None:
        score = _score()
        score["tempo_map"][0]["bpm"] = 1.0

        payload, report = build_midi(score, allow_lossy=True)

        self.assertTrue(payload.startswith(b"MThd"))
        self.assertIn(
            "tempo_clamped_to_midi_range",
            {item["code"] for item in report["losses"]},
        )

    def test_used_roster_execution_and_mix_semantics_are_listed(self) -> None:
        roster = {
            "drop_parts": ["unused-part"],
            "assignments": [
                {
                    "part": "小提琴",
                    "instrument": "管弦乐/弦乐组/小提琴",
                    "executor_id": "first-violin-desk",
                    "gain_db": -3.0,
                    "gain_automation": [
                        {"bar": 1, "beat": 1.0, "offset_db": 0.0}
                    ],
                    "pan": 0.25,
                    "transpose": 1,
                    "dynamic_compression": 0.2,
                    "duration_scale": 0.9,
                    "articulation_auto": False,
                    "seat": {"azimuth_deg": 20.0, "distance_m": 3.0},
                    "articulation_map": {"short": "staccato"},
                    "overrides": {"release_seconds": 0.2},
                }
            ],
        }

        _payload, report = build_midi(
            _score(),
            roster=roster,
            allow_lossy=True,
        )
        by_code = {item["code"]: item for item in report["losses"]}

        self.assertIn("roster_drop_parts_not_applied", by_code)
        self.assertIn("dedicated_instrument_approximated_by_gm", by_code)
        self.assertEqual(
            by_code["roster_execution_semantics_not_representable"]["details"][
                "fields"
            ],
            [
                "articulation_auto",
                "articulation_map",
                "duration_scale",
                "dynamic_compression",
                "executor_id",
                "gain_automation",
                "gain_db",
                "overrides",
                "pan",
                "seat",
                "transpose",
            ],
        )

    def test_more_than_fifteen_melodic_parts_is_a_blocking_loss(self) -> None:
        score = _score()
        template = score["parts"][0]
        score["parts"] = []
        for index in range(16):
            part = copy.deepcopy(template)
            part["id"] = f"part-{index}"
            part["name"] = f"part-{index}"
            for note in part["notes"]:
                note["event_id"] = f"{part['id']}-{note['event_id']}"
            score["parts"].append(part)
        with self.assertRaises(MidiExportLossError) as raised:
            build_midi(score)
        self.assertIn(
            "midi_channel_limit_exceeded",
            {item["code"] for item in raised.exception.report["losses"]},
        )

    def test_cli_writes_loss_report_even_when_export_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            score_path = root / "score.json"
            midi_path = root / "score.mid"
            report_path = root / "loss.json"
            score_path.write_text(
                json.dumps(_score(), ensure_ascii=False),
                encoding="utf-8",
            )
            with (
                redirect_stdout(io.StringIO()),
                redirect_stderr(io.StringIO()),
            ):
                result = cli_main(
                    [
                        "export-midi",
                        "--score",
                        str(score_path),
                        "--output",
                        str(midi_path),
                        "--report-output",
                        str(report_path),
                    ]
                )
            self.assertEqual(result, 2)
            self.assertFalse(midi_path.exists())
            self.assertTrue(report_path.is_file())
            self.assertFalse(
                json.loads(report_path.read_text(encoding="utf-8"))["ok"]
            )


if __name__ == "__main__":
    unittest.main()
