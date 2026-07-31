import json
import math
from pathlib import Path
import unittest

import pytest

from tianlai.analysis import analyze_instrument_pitch
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "乐器" / "低音乐器" / "原声贝斯" / "乐器.json"
ASSET_ROOT = ROOT / "音源" / "Karoryfer" / "karoryfer.meatbass-master"
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(
    MANIFEST.is_file() and ASSET_ROOT.is_dir(),
    "Karoryfer Meatbass resources are not installed",
)
class MeatbassRegressionTests(unittest.TestCase):
    def test_pizzicato_and_arco_low_middle_high_pitch_gate(self) -> None:
        for articulation in ("pizzicato", "arco"):
            for midi_note in (28, 42, 55):
                with self.subTest(
                    articulation=articulation,
                    midi_note=midi_note,
                ):
                    result = analyze_instrument_pitch(
                        MANIFEST,
                        midi_note,
                        sample_rate=24_000,
                        duration_seconds=0.64,
                        maximum_frames=12_288,
                        articulation=articulation,
                    )
                    self.assertTrue(result.clear_pitch, result)
                    self.assertEqual(result.nearest_octave_error, 0, result)
                    self.assertTrue(result.within_tolerance(35.0), result)

    def test_default_cc_map_is_single_voice_and_survives_200ms(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        instrument = create_instrument(
            manifest,
            48_000,
            base_directory=str(MANIFEST.parent),
        )
        tuning = EqualTemperament()

        for sequence, articulation in enumerate(("pizzicato", "arco")):
            if articulation != instrument.articulation:
                instrument.handle_event(
                    PerformanceEvent(
                        sequence * 100_000,
                        sequence * 3,
                        "articulation",
                        {"name": articulation},
                    ),
                    tuning,
                )
            note_id = sequence + 1
            instrument.handle_event(
                PerformanceEvent(
                    sequence * 100_000,
                    sequence * 3 + 1,
                    "note_on",
                    {"note_id": note_id, "midi_note": 45, "velocity": 0.8},
                ),
                tuning,
            )
            route = instrument.routes[note_id]
            self.assertEqual(
                len(route.voices),
                1,
                f"{articulation} must select one CC107 map",
            )

            tail_energy = 0.0
            tail_frames = 0
            for frame_index in range(round(0.25 * 48_000)):
                left, right = instrument.render_frame()
                if frame_index >= round(0.2 * 48_000):
                    tail_energy += 0.5 * (left * left + right * right)
                    tail_frames += 1
            tail_rms = math.sqrt(tail_energy / tail_frames)
            self.assertGreater(
                tail_rms,
                1.0e-5,
                f"{articulation} became silent after 200 ms",
            )

            instrument.handle_event(
                PerformanceEvent(
                    sequence * 100_000 + round(0.25 * 48_000),
                    sequence * 3 + 2,
                    "note_off",
                    {"note_id": note_id, "release_velocity": 0.8},
                ),
                tuning,
            )
            for _ in range(round(0.6 * 48_000)):
                instrument.render_frame()


if __name__ == "__main__":
    unittest.main()
