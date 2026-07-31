import json
from pathlib import Path
import unittest

import pytest

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "乐器" / "管弦乐" / "弦乐组" / "大提琴" / "乐器.json"
WAVE_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3" / "libs"
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(WAVE_ROOT.is_dir(), "Virtual Playing Orchestra wave files are not installed")
class CelloInstrumentTests(unittest.TestCase):
    def create_cello(self, **overrides):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        manifest.update(overrides)
        return create_instrument(manifest, 48000, base_directory=str(MANIFEST.parent))

    def test_articulation_and_release_regions_are_separated(self) -> None:
        cello = self.create_cello()
        self.assertEqual(len(cello.engines["sustain"].regions), 9)
        self.assertEqual(len(cello.engines["slow_sustain"].regions), 9)
        self.assertEqual(len(cello.engines["staccato"].regions), 48)
        self.assertEqual(len(cello.engines["pizzicato"].regions), 21)
        self.assertEqual(len(cello.release_tails), 10)
        self.assertEqual(
            sum(region.loop_start is not None for region in cello.engines["sustain"].regions),
            9,
        )

    def test_manifest_release_overrides_sfz_sustain_regions(self) -> None:
        cello = self.create_cello(release_seconds=0.13)
        for engine_name in ("sustain", "slow_sustain"):
            self.assertTrue(
                all(
                    region.release_seconds == 0.13
                    for region in cello.engines[engine_name].regions
                )
            )

    def test_release_tail_may_be_disabled_for_dense_work(self) -> None:
        cello = self.create_cello(release_tail_gain=0.0)
        self.assertEqual(cello.release_tails, [])

    def test_note_off_triggers_matching_release_tail(self) -> None:
        cello = self.create_cello()
        tuning = EqualTemperament()
        cello.handle_event(
            PerformanceEvent(0, 0, "note_on", {"note_id": 1, "midi_note": 57, "velocity": 0.8}),
            tuning,
        )
        cello.handle_event(
            PerformanceEvent(48000, 1, "note_off", {"note_id": 1, "release_velocity": 0.5}),
            tuning,
        )
        self.assertEqual(sum(item[2].active_voice_count for item in cello.release_tails), 1)

    def test_overlapping_low_release_regions_are_layered(self) -> None:
        cello = self.create_cello()
        tuning = EqualTemperament()
        cello.handle_event(
            PerformanceEvent(0, 0, "note_on", {"note_id": 1, "midi_note": 36, "velocity": 0.8}),
            tuning,
        )
        cello.handle_event(
            PerformanceEvent(48000, 1, "note_off", {"note_id": 1, "release_velocity": 0.5}),
            tuning,
        )
        self.assertEqual(sum(item[2].active_voice_count for item in cello.release_tails), 2)

    def test_release_tail_uses_note_on_not_conflicting_note_off_velocity(self) -> None:
        def release_amplitude(note_on_velocity: float, note_off_velocity: float) -> float:
            cello = self.create_cello()
            tuning = EqualTemperament()
            cello.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {
                        "note_id": 1,
                        "midi_note": 57,
                        "velocity": note_on_velocity,
                    },
                ),
                tuning,
            )
            cello.handle_event(
                PerformanceEvent(
                    48000,
                    1,
                    "note_off",
                    {
                        "note_id": 1,
                        "release_velocity": note_off_velocity,
                    },
                ),
                tuning,
            )
            active = [
                voice
                for _low, _high, engine in cello.release_tails
                for voice in engine.voices.values()
            ]
            self.assertEqual(len(active), 1)
            return active[0].amplitude

        soft_fast_release = release_amplitude(0.2, 1.0)
        soft_slow_release = release_amplitude(0.2, 0.1)
        loud_slow_release = release_amplitude(0.8, 0.1)
        self.assertAlmostEqual(
            soft_fast_release,
            soft_slow_release,
            places=9,
        )
        self.assertGreater(loud_slow_release, soft_slow_release)

    def test_sustained_a3_uses_measured_pitch_calibration(self) -> None:
        cello = self.create_cello()
        calibration = json.loads(
            (MANIFEST.parent / "音准校准.json").read_text(encoding="utf-8")
        )["samples"]
        expected = next(
            item["measured_hz"]
            for path, item in calibration.items()
            if path.endswith("/Vibrato/3_A-PB-loop.wav")
        )
        region = next(
            item
            for item in cello.engines["sustain"].regions
            if item.path.name == "3_A-PB-loop.wav"
        )
        self.assertAlmostEqual(region.root_pitch_hz, expected, places=5)

    def test_sampled_range_is_enforced(self) -> None:
        cello = self.create_cello()
        with self.assertRaisesRegex(ValueError, "outside the sampled"):
            cello.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 35, "velocity": 0.8},
                ),
                EqualTemperament(),
            )

    def test_unknown_articulation_is_rejected(self) -> None:
        cello = self.create_cello()
        with self.assertRaisesRegex(ValueError, "unsupported cello articulation"):
            cello.handle_event(
                PerformanceEvent(0, 0, "articulation", {"name": "tremolo"}),
                EqualTemperament(),
            )


if __name__ == "__main__":
    unittest.main()
