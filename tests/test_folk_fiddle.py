from __future__ import annotations

import json
from pathlib import Path
import unittest

import pytest

from tianlai.capability import read_capability
from tianlai.conductor import build_plan
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "乐器" / "世界乐器" / "民谣提琴" / "乐器.json"
WAVE_ROOT = (
    ROOT
    / "音源"
    / "VirtualPlayingOrchestra"
    / "Virtual-Playing-Orchestra3"
    / "libs"
)


class FolkFiddleConductorContractTests(unittest.TestCase):
    def test_real_build_plan_keeps_unmarked_short_notes_on_fiddle(self) -> None:
        capability = read_capability(
            MANIFEST,
            root=ROOT / "乐器",
            defer_onset_evidence=True,
        )
        self.assertFalse(capability.articulation_auto_default)
        self.assertFalse(capability.to_dict()["articulation_auto_default"])

        self.assertEqual(capability.license_status, "grandfathered")
        roster = parse_roster_document(
            {
                "assignments": [
                    {
                        "part": "fiddle",
                        "instrument": capability.relative_path,
                    }
                ]
            },
            {capability.relative_path: capability},
        )
        self.assertFalse(roster.executors[0].articulation_auto)
        self.assertFalse(roster.executors[0].to_dict()["articulation_auto"])

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
                        "id": "fiddle",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 1,
                                "pitch": "A4",
                                "velocity": 0.72,
                            },
                            {
                                "bar": 1,
                                "beat": 2,
                                "duration_beats": 1,
                                "pitch": "B4",
                                "velocity": 0.72,
                                "articulation": "sustain",
                            },
                        ],
                    }
                ],
            }
        )
        plan = build_plan(score, roster)
        self.assertEqual(
            [entry["奏法"] for entry in plan.parts[0].trace],
            ["fiddle", "sustain"],
        )
        self.assertEqual(
            [
                event["name"]
                for event in plan.parts[0].performance["events"]
                if event["type"] == "articulation"
            ],
            ["fiddle", "sustain"],
        )


@unittest.skipUnless(
    WAVE_ROOT.is_dir(),
    "Virtual Playing Orchestra wave files are not installed",
)
@pytest.mark.external_assets
class FolkFiddleStyleTests(unittest.TestCase):
    def create_fiddle(self):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return create_instrument(
            manifest,
            48_000,
            base_directory=str(MANIFEST.parent),
        )

    def test_default_is_fast_fiddle_while_sustain_keeps_legacy_envelope(self) -> None:
        instrument = self.create_fiddle()
        self.assertEqual(instrument.default_articulation, "fiddle")
        self.assertEqual(instrument.articulation, "fiddle")

        fiddle = instrument.articulations["fiddle"]
        lyrical = instrument.articulations["sustain"]
        self.assertEqual(fiddle.release_override_seconds, 0.12)
        self.assertIsNone(lyrical.release_override_seconds)
        self.assertEqual(
            {region.attack_seconds for region in fiddle.attack.regions},
            {0.02},
        )
        self.assertEqual(
            {region.attack_seconds for region in lyrical.attack.regions},
            {0.3},
        )
        self.assertEqual(
            {region.path for region in fiddle.attack.regions},
            {region.path for region in lyrical.attack.regions},
        )

    def test_fiddle_release_is_short_but_explicit_sustain_is_unchanged(self) -> None:
        tuning = EqualTemperament(440.0)

        def released_voice(articulation: str):
            instrument = self.create_fiddle()
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "articulation",
                    {"name": articulation},
                ),
                tuning,
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    1,
                    "note_on",
                    {
                        "note_id": 1,
                        "midi_note": 69,
                        "velocity": 0.72,
                    },
                ),
                tuning,
            )
            runtime = instrument.articulations[articulation]
            voice = next(
                iter(
                    next(
                        layer.engine.voices
                        for layer in runtime.attack_layers
                        if layer.engine.voices
                    ).values()
                )
            )
            instrument.handle_event(
                PerformanceEvent(
                    1,
                    2,
                    "note_off",
                    {"note_id": 1, "release_velocity": 0.5},
                ),
                tuning,
            )
            self.assertTrue(voice.released)
            return voice

        self.assertEqual(released_voice("fiddle").release_samples, 5_760)
        self.assertEqual(released_voice("sustain").release_samples, 76_800)


if __name__ == "__main__":
    unittest.main()
