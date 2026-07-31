from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
import unittest
from unittest import mock

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "巡检全音域音准.py"
SPEC = importlib.util.spec_from_file_location("tianlai_pitch_audit_tool", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import pitch audit tool: {TOOL_PATH}")
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOOL
SPEC.loader.exec_module(TOOL)


class PitchAuditToolTests(unittest.TestCase):
    sample_rate = 24_000

    def test_full_octave_error_is_not_hidden_by_a_narrow_search(self) -> None:
        time = (
            np.arange(self.sample_rate, dtype="float64")
            / float(self.sample_rate)
        )
        audio = (
            np.sin(2.0 * math.pi * 880.0 * time)
            + 0.5 * np.sin(2.0 * math.pi * 1760.0 * time)
            + 0.25 * np.sin(2.0 * math.pi * 2640.0 * time)
        )

        result = TOOL.measure(audio, self.sample_rate, 440.0)

        self.assertGreaterEqual(TOOL.WIDE_SEARCH_CENTS, 1_800.0)
        self.assertTrue(result.clear_pitch, result)
        self.assertEqual(result.nearest_octave_error, 1)
        self.assertAlmostEqual(result.detune_cents, 1200.0, delta=5.0)
        self.assertEqual(TOOL.classify_pitch(result, 25.0), "octave_error")

    def test_silence_is_reported_as_no_clear_pitch(self) -> None:
        audio = np.zeros(self.sample_rate, dtype="float64")

        result = TOOL.measure(audio, self.sample_rate, 440.0)

        self.assertFalse(result.clear_pitch)
        self.assertEqual(result.status, "no_clear_pitch")
        self.assertEqual(
            TOOL.classify_pitch(result, 25.0),
            "no_clear_pitch",
        )

    def test_low_note_uses_the_rendered_long_window_not_32768_frames(self) -> None:
        sample_rate = 48_000
        expected_hz = 440.0 * 2.0 ** ((35.0 - 69.0) / 12.0)
        frame_count = 5 * sample_rate
        frequencies = np.full(frame_count, expected_hz, dtype="float64")
        # Model a low string whose first 0.8 s settles from about -40 cents.
        # A 32768-frame window sees only that transient and reports a false
        # failure; the already-rendered five-second window resolves the stable
        # fundamental correctly.
        frequencies[: round(0.8 * sample_rate)] = (
            expected_hz * 2.0 ** (-40.0 / 1200.0)
        )
        phase = 2.0 * math.pi * np.cumsum(frequencies) / sample_rate
        audio = (
            np.sin(phase)
            + 0.35 * np.sin(2.0 * phase)
            + 0.20 * np.sin(3.0 * phase)
        )

        truncated = TOOL.analyze_signal_wide_pitch(
            audio,
            sample_rate,
            expected_hz,
            start_seconds=TOOL.ANALYSIS_START_SECONDS,
            maximum_frames=32_768,
            search_cents=TOOL.WIDE_SEARCH_CENTS,
        )
        measured = TOOL.measure(audio, sample_rate, expected_hz)

        self.assertTrue(truncated.clear_pitch, truncated)
        self.assertIsNotNone(truncated.detune_cents)
        assert truncated.detune_cents is not None
        self.assertGreater(abs(truncated.detune_cents), 35.0)
        self.assertTrue(measured.within_tolerance(5.0), measured)

    def test_render_note_selects_manifest_calibration_articulation_first(
        self,
    ) -> None:
        class RecordingInstrument:
            def __init__(self) -> None:
                self.events = []
                self.closed = False

            def handle_event(self, event, tuning) -> None:
                self.events.append(event)

            def render_frame(self) -> tuple[float, float]:
                return (0.0, 0.0)

            def close(self) -> None:
                self.closed = True

        instrument = RecordingInstrument()
        manifest = {"calibration_articulation": "arco"}
        with mock.patch.object(
            TOOL,
            "create_instrument",
            return_value=instrument,
        ):
            buffer, expected_hz = TOOL.render_note(
                manifest,
                ROOT,
                midi=69,
                sample_rate=8_000,
            )

        self.assertGreaterEqual(len(buffer), 4_096)
        self.assertAlmostEqual(expected_hz, 440.0)
        self.assertEqual(
            [event.type for event in instrument.events],
            ["articulation", "note_on"],
        )
        self.assertEqual(
            [event.sequence for event in instrument.events],
            [0, 1],
        )
        self.assertEqual(instrument.events[0].payload, {"name": "arco"})
        self.assertEqual(instrument.events[1].payload["midi_note"], 69)
        self.assertTrue(instrument.closed)

    def test_segmented_ranges_do_not_fill_the_bagpipe_hole(self) -> None:
        notes = TOOL.manifest_midi_notes(
            {
                "playable_ranges": [
                    [36, 59],
                    [64, 81],
                ]
            }
        )

        self.assertEqual(notes[0], 36)
        self.assertEqual(notes[-1], 81)
        self.assertIn(59, notes)
        self.assertIn(64, notes)
        for note in (60, 61, 62, 63):
            self.assertNotIn(note, notes)
        self.assertEqual(len(notes), 42)


if __name__ == "__main__":
    unittest.main()
