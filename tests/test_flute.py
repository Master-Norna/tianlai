import json
from pathlib import Path
import unittest

import pytest

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "乐器" / "管弦乐" / "木管组" / "长笛" / "乐器.json"
WAVE_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3" / "libs"
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(WAVE_ROOT.is_dir(), "Virtual Playing Orchestra wave files are not installed")
class FluteInstrumentTests(unittest.TestCase):
    def create_flute(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return create_instrument(manifest, 48000, base_directory=str(MANIFEST.parent))

    def test_region_sets_and_accent_delays_are_loaded(self) -> None:
        flute = self.create_flute()
        self.assertEqual(len(flute.engines["sustain"].regions), 10)
        self.assertEqual(len(flute.engines["slow_sustain"].regions), 10)
        self.assertEqual(len(flute.engines["legato"].regions), 10)
        self.assertEqual(len(flute.engines["accent_sustain"].regions), 10)
        self.assertEqual(len(flute.staccato_layers), 10)
        self.assertEqual(
            sum(region.loop_start is not None for region in flute.engines["sustain"].regions),
            10,
        )
        delays = sorted(
            round(region.delay_seconds, 3)
            for region in flute.engines["accent_sustain"].regions
        )
        self.assertEqual(delays[0], 0.04)
        self.assertEqual(delays[-1], 0.12)

    def test_overlapping_staccato_regions_are_layered(self) -> None:
        flute = self.create_flute()
        tuning = EqualTemperament()
        flute.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "staccato"}),
            tuning,
        )
        flute.handle_event(
            PerformanceEvent(0, 1, "note_on", {"note_id": 1, "midi_note": 67, "velocity": 0.8}),
            tuning,
        )
        self.assertEqual(len(flute.note_routes[1].voices), 2)

    def test_overlapping_notes_create_short_legato_crossfade(self) -> None:
        flute = self.create_flute()
        tuning = EqualTemperament()
        flute.handle_event(
            PerformanceEvent(0, 0, "note_on", {"note_id": 1, "midi_note": 60, "velocity": 0.75}),
            tuning,
        )
        first = flute.note_routes[1].voices[0]
        flute.handle_event(
            PerformanceEvent(12000, 1, "note_on", {"note_id": 2, "midi_note": 64, "velocity": 0.78}),
            tuning,
        )
        old_voice = flute.engines[first.engine_name].voices[first.note_id]
        second = flute.note_routes[2].voices[0]
        self.assertTrue(old_voice.released)
        self.assertEqual(old_voice.release_samples, round(0.055 * 48000))
        self.assertNotEqual(first.note_id, second.note_id)
        self.assertEqual(second.engine_name, "legato")
        self.assertEqual(flute.note_routes[1].voices, ())

    def test_new_note_shortens_an_existing_release_tail(self) -> None:
        flute = self.create_flute()
        tuning = EqualTemperament()
        flute.handle_event(
            PerformanceEvent(0, 0, "note_on", {"note_id": 1, "midi_note": 69, "velocity": 0.8}),
            tuning,
        )
        first = flute.note_routes[1].voices[0]
        for _ in range(3000):
            flute.render_frame()
        flute.handle_event(
            PerformanceEvent(3000, 1, "note_off", {"note_id": 1, "release_velocity": 0.5}),
            tuning,
        )
        old_voice = flute.engines[first.engine_name].voices[first.note_id]
        old_step = old_voice.release_step
        flute.handle_event(
            PerformanceEvent(4000, 2, "note_on", {"note_id": 2, "midi_note": 71, "velocity": 0.8}),
            tuning,
        )
        self.assertEqual(old_voice.release_samples, round(0.055 * 48000))
        self.assertGreater(old_voice.release_step, old_step)

    def test_note_off_before_accent_delay_cancels_sustain_layer(self) -> None:
        flute = self.create_flute()
        tuning = EqualTemperament()
        flute.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}),
            tuning,
        )
        flute.handle_event(
            PerformanceEvent(0, 1, "note_on", {"note_id": 1, "midi_note": 60, "velocity": 0.8}),
            tuning,
        )
        sustained = next(voice for voice in flute.note_routes[1].voices if voice.sustained)
        flute.handle_event(
            PerformanceEvent(1, 2, "note_off", {"note_id": 1, "release_velocity": 0.5}),
            tuning,
        )
        flute.render_frame()
        self.assertNotIn(sustained.note_id, flute.engines["accent_sustain"].voices)

    def test_sustained_a4_uses_measured_pitch_calibration(self) -> None:
        flute = self.create_flute()
        calibration = json.loads(
            (MANIFEST.parent / "音准校准.json").read_text(encoding="utf-8")
        )["samples"]
        expected = next(
            item["measured_hz"]
            for path, item in calibration.items()
            if path.endswith("/susvib-PB/LDFlute_susvib_A3_v1_1-PB-loop.wav")
        )
        region = next(
            item
            for item in flute.engines["sustain"].regions
            if item.path.name == "LDFlute_susvib_A3_v1_1-PB-loop.wav"
        )
        self.assertAlmostEqual(region.root_pitch_hz, expected, places=5)

    def test_sampled_range_and_articulation_are_enforced(self) -> None:
        flute = self.create_flute()
        with self.assertRaisesRegex(ValueError, "outside the sampled"):
            flute.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 59, "velocity": 0.8},
                ),
                EqualTemperament(),
            )
        with self.assertRaisesRegex(ValueError, "unsupported flute articulation"):
            flute.handle_event(
                PerformanceEvent(0, 1, "articulation", {"name": "flutter_tongue"}),
                EqualTemperament(),
            )


if __name__ == "__main__":
    unittest.main()
