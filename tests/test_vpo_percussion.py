import gc
import hashlib
import json
from pathlib import Path
import struct
import unittest

import pytest

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament
from tianlai.vpo_percussion import PERCUSSION_PROFILES, percussion_source_regions


ROOT = Path(__file__).resolve().parents[1]
PERCUSSION_ROOT = ROOT / "乐器" / "管弦乐" / "打击乐组"
VPO_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3"
MANIFESTS = {
    "triangle": PERCUSSION_ROOT / "三角铁" / "乐器.json",
    "snare": PERCUSSION_ROOT / "小军鼓" / "乐器.json",
    "xylophone": PERCUSSION_ROOT / "木琴" / "乐器.json",
    "woodblock": PERCUSSION_ROOT / "木鱼" / "乐器.json",
    "bass_drum": PERCUSSION_ROOT / "管弦大鼓" / "乐器.json",
    "cymbals": PERCUSSION_ROOT / "管弦钹" / "乐器.json",
    "tubular_bells": PERCUSSION_ROOT / "管钟" / "乐器.json",
    "glockenspiel": PERCUSSION_ROOT / "钟琴" / "乐器.json",
}
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(VPO_ROOT.is_dir(), "Virtual Playing Orchestra is not installed")
class VpoPercussionTests(unittest.TestCase):
    def create_percussion(self, key: str):
        path = MANIFESTS[key]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return create_instrument(manifest, 48_000, base_directory=str(path.parent))

    def note_on(
        self,
        instrument,
        *,
        note_id: int,
        midi_note: float,
        velocity: float = 0.8,
        sequence: int = 0,
        tuning: EqualTemperament | None = None,
    ) -> None:
        instrument.handle_event(
            PerformanceEvent(
                0,
                sequence,
                "note_on",
                {
                    "note_id": note_id,
                    "midi_note": midi_note,
                    "velocity": velocity,
                },
            ),
            tuning or EqualTemperament(),
        )

    def articulation(self, instrument, name: str) -> None:
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": name}),
            EqualTemperament(),
        )

    def test_candidates_do_not_silently_fall_back_to_soundfont(self) -> None:
        for path in MANIFESTS.values():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["type"], "vpo_percussion")
            self.assertEqual(manifest["quality_tier"], "formal")
            self.assertEqual(
                manifest["collaboration_review_status"], "untested"
            )
            self.assertNotIn("implementation", manifest)
            self.assertTrue(path.with_name("乐器.py").is_file())
            self.assertNotIn("soundfont", manifest)
            instrument = create_instrument(
                manifest,
                48_000,
                base_directory=str(path.parent),
            )
            self.assertEqual(
                instrument._tianlai_factory_provenance["factory_route"],
                "builtin_manifest_dispatch_no_implementation",
            )
            del instrument
            gc.collect()

        # 马林巴已改用 VCSL 专用多采样升级为 dedicated_sfz candidate;
        # 守住它不再是通用 SoundFont,且 VPO 树内确实没有马林巴素材。
        marimba = json.loads(
            (PERCUSSION_ROOT / "马林巴" / "乐器.json").read_text(encoding="utf-8")
        )
        self.assertEqual(marimba["type"], "dedicated_sfz")
        self.assertEqual(marimba["quality_tier"], "formal")
        self.assertIn("VCSL", marimba["upstream"])
        self.assertFalse(any("marimba" in path.name.lower() for path in VPO_ROOT.rglob("*")))

    def test_real_region_counts_samples_loops_and_silent_switch_filters(self) -> None:
        expected = {
            "triangle": ({"open": 4, "muted": 1, "roll": 1}, 6),
            "snare": (
                {
                    "left": 2,
                    "alternating": 4,
                    "hit": 4,
                    "right": 2,
                    "kit2_left": 2,
                    "kit2_right": 2,
                    "tap": 4,
                    "roll": 2,
                    "roll_looped": 1,
                },
                15,
            ),
            "xylophone": ({"hit": 30}, 30),
            "woodblock": ({"low": 1, "high": 1}, 2),
            "bass_drum": ({"drum_1": 2, "drum_2": 4}, 6),
            "cymbals": (
                {
                    "roll_soft": 2,
                    "piatti": 1,
                    "roll_alt": 1,
                    "piatti_high": 1,
                    "crescendo_short": 1,
                    "crash": 4,
                    "crescendo_medium": 1,
                    "suspended_hit": 4,
                    "crescendo_long": 1,
                    "suspended_high": 4,
                },
                15,
            ),
            "tubular_bells": ({"open": 24, "damped": 24}, 24),
            "glockenspiel": ({"hit": 6}, 6),
            "vibraphone": ({"damped": 22, "open": 22}, 22),
        }
        for key, (counts, unique_count) in expected.items():
            sets = percussion_source_regions(VPO_ROOT, key)
            self.assertEqual({name: len(rows) for name, rows in sets.items()}, counts)
            paths = {row["sample"] for rows in sets.values() for row in rows}
            self.assertEqual(len(paths), unique_count)
            self.assertTrue(all(Path(path).is_file() for path in paths))
            self.assertTrue(
                all(
                    not Path(row["stable_key"]).is_absolute()
                    and "VirtualPlayingOrchestra" not in row["stable_key"]
                    for rows in sets.values()
                    for row in rows
                )
            )

        snare = self.create_percussion("snare")
        self.assertEqual(
            sum(
                region.loop_start is not None
                for region in snare.engines["roll_looped"].regions
            ),
            1,
        )
        tubular = self.create_percussion("tubular_bells")
        self.assertEqual(len(tubular.engines["open"].regions), 22)
        self.assertEqual(len(tubular.engines["damped"].regions), 22)
        self.assertTrue(
            all(region.path.name != "silence.wav" for region in tubular.engines["open"].regions)
        )
    def test_vibraphone_neighboring_roots_are_not_claimed_as_round_robin(self) -> None:
        region_sets = percussion_source_regions(VPO_ROOT, "vibraphone")
        for articulation, rows in region_sets.items():
            self.assertEqual(len(rows), 22, articulation)
            self.assertTrue(
                all("round_robin_length" not in row for row in rows),
                articulation,
            )
            self.assertTrue(
                all(
                    row["key_min"] <= row["root_midi"] <= row["key_max"]
                    for row in rows
                ),
                articulation,
            )

    def test_sampled_sounding_ranges_and_xylophone_octave_are_enforced(self) -> None:
        expected = {
            "xylophone": (60, 108),
            "tubular_bells": (60, 79),
            "glockenspiel": (77, 108),
        }
        for key, (minimum, maximum) in expected.items():
            instrument = self.create_percussion(key)
            self.note_on(instrument, note_id=1, midi_note=minimum)
            self.note_on(instrument, note_id=2, midi_note=maximum)
            with self.assertRaisesRegex(ValueError, "outside sampled range"):
                self.note_on(instrument, note_id=3, midi_note=minimum - 1)
            with self.assertRaisesRegex(ValueError, "outside sampled range"):
                self.note_on(instrument, note_id=4, midi_note=maximum + 1)

        xylophone = self.create_percussion("xylophone")
        self.note_on(xylophone, note_id=50, midi_note=60)
        route = xylophone.routes[50]
        region = xylophone.engines["hit"].voices[route.internal_note_id].region
        self.assertEqual(region.key_min, 60)
        self.assertEqual(region.root_pitch_hz > 300.0, True)

    def test_rr_sequence_uses_sfz_position_and_is_reproducible(self) -> None:
        instrument = self.create_percussion("xylophone")
        selected: list[str] = []
        for index in range(3):
            self.note_on(instrument, note_id=index + 1, midi_note=60, sequence=index)
            route = instrument.routes[index + 1]
            voice = instrument.engines["hit"].voices[route.internal_note_id]
            selected.append(voice.region.path.name)
        self.assertNotEqual(selected[0], selected[1])
        self.assertEqual(selected[0], selected[2])
        self.assertNotIn("_2", selected[0])
        self.assertIn("_2", selected[1])

    def test_discrete_crossfade_boundary_never_becomes_four_way_rr(self) -> None:
        low = self.create_percussion("bass_drum")
        high = self.create_percussion("bass_drum")
        self.note_on(low, note_id=1, midi_note=60, velocity=79 / 127)
        self.note_on(high, note_id=1, midi_note=60, velocity=80 / 127)
        low_route = low.routes[1]
        high_route = high.routes[1]
        low_voice = low.engines["drum_2"].voices[low_route.internal_note_id]
        high_voice = high.engines["drum_2"].voices[high_route.internal_note_id]
        self.assertIn("v5", low_voice.region.path.name)
        self.assertIn("v7", high_voice.region.path.name)
        self.assertLess(low_voice.region.velocity_max, high_voice.region.velocity_min + 1e-12)

    def test_sfz_humanization_is_stable_but_event_sensitive(self) -> None:
        first = self.create_percussion("bass_drum")
        repeat = self.create_percussion("bass_drum")
        changed = self.create_percussion("bass_drum")
        self.note_on(first, note_id=1, midi_note=60, sequence=7)
        self.note_on(repeat, note_id=1, midi_note=60, sequence=7)
        self.note_on(changed, note_id=1, midi_note=60, sequence=8)

        def voice_state(instrument) -> tuple[Path, float, float, int]:
            route = instrument.routes[1]
            voice = instrument.engines["drum_2"].voices[route.internal_note_id]
            return voice.region.path, voice.increment, voice.amplitude, voice.delay_samples

        self.assertEqual(voice_state(first), voice_state(repeat))
        self.assertEqual(voice_state(first)[0], voice_state(changed)[0])
        self.assertNotEqual(voice_state(first)[1:], voice_state(changed)[1:])

    def test_triangle_off_by_choke_matches_upstream_groups(self) -> None:
        instrument = self.create_percussion("triangle")
        self.articulation(instrument, "roll")
        self.note_on(instrument, note_id=1, midi_note=60)
        roll_id = instrument.routes[1].internal_note_id
        self.assertFalse(instrument.engines["roll"].voices[roll_id].released)

        self.articulation(instrument, "open")
        self.note_on(instrument, note_id=2, midi_note=60)
        self.assertTrue(instrument.engines["roll"].voices[roll_id].released)
        open_id = instrument.routes[2].internal_note_id

        self.articulation(instrument, "muted")
        self.note_on(instrument, note_id=3, midi_note=60)
        self.assertTrue(instrument.engines["open"].voices[open_id].released)

        self.articulation(instrument, "open")
        self.note_on(instrument, note_id=4, midi_note=60)
        untouched_open = instrument.routes[4].internal_note_id
        self.articulation(instrument, "roll")
        self.note_on(instrument, note_id=5, midi_note=60)
        self.assertFalse(instrument.engines["open"].voices[untouched_open].released)

    def test_tubular_damper_pedal_uses_short_click_safe_release(self) -> None:
        instrument = self.create_percussion("tubular_bells")
        self.articulation(instrument, "damped")
        self.note_on(instrument, note_id=1, midi_note=60)
        route = instrument.routes[1]
        voice = instrument.engines["damped"].voices[route.internal_note_id]
        self.assertEqual(voice.region.stereo_width, 1.0)
        self.assertIsNone(voice.region.loop_start)
        instrument.handle_event(
            PerformanceEvent(1, 1, "control", {"name": "sustain_pedal", "value": 1.0}),
            EqualTemperament(),
        )
        instrument.handle_event(
            PerformanceEvent(2, 2, "note_off", {"note_id": 1}),
            EqualTemperament(),
        )
        self.assertTrue(voice.pending_release)
        instrument.handle_event(
            PerformanceEvent(3, 3, "control", {"name": "sustain_pedal", "value": 0.0}),
            EqualTemperament(),
        )
        self.assertTrue(voice.released)
        self.assertEqual(voice.release_samples, round(0.12 * 48_000))

    def test_calibration_is_applied_but_tubular_bells_remain_honest(self) -> None:
        for key in ("xylophone", "glockenspiel"):
            path = MANIFESTS[key]
            report = json.loads((path.parent / "音准校准.json").read_text(encoding="utf-8"))
            instrument = self.create_percussion(key)
            sample_path, measurement = next(iter(report["samples"].items()))
            region = next(
                region
                for engine in instrument.engines.values()
                for region in engine.regions
                if region.path.relative_to(VPO_ROOT).as_posix() == sample_path
            )
            self.assertAlmostEqual(region.root_pitch_hz, measurement["measured_hz"], places=5)

        tubular_report = json.loads(
            (MANIFESTS["tubular_bells"].parent / "音准校准.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(tubular_report["summary"]["automatic_correction_count"], 0)
        self.assertEqual(tubular_report["summary"]["human_spectral_review"], "pending")
        self.assertTrue(
            all(
                sample["automatic_cents_correction"] is None
                for sample in tubular_report["samples"].values()
            )
        )

    def test_a4_and_fractional_midi_change_real_playback_rate(self) -> None:
        first = self.create_percussion("xylophone")
        second = self.create_percussion("xylophone")
        self.note_on(
            first,
            note_id=1,
            midi_note=69.25,
            sequence=8,
            tuning=EqualTemperament(440.0),
        )
        self.note_on(
            second,
            note_id=1,
            midi_note=69.25,
            sequence=8,
            tuning=EqualTemperament(442.0),
        )
        first_route = first.routes[1]
        second_route = second.routes[1]
        first_voice = first.engines["hit"].voices[first_route.internal_note_id]
        second_voice = second.engines["hit"].voices[second_route.internal_note_id]
        self.assertEqual(first_voice.region.path, second_voice.region.path)
        self.assertAlmostEqual(second_voice.increment / first_voice.increment, 442 / 440, places=9)

    def _render_digest_and_peak(self, key: str) -> tuple[str, float]:
        instrument = self.create_percussion(key)
        tuning = EqualTemperament()
        profile = PERCUSSION_PROFILES[key]
        if key == "tubular_bells":
            self.articulation(instrument, "damped")
            note = 67
        elif key == "xylophone":
            note = 72
        elif key == "glockenspiel":
            note = 91
        else:
            note = 60
        self.note_on(instrument, note_id=1, midi_note=note, velocity=0.82, sequence=19)
        digest = hashlib.sha256()
        peak = 0.0
        for frame_index in range(16_000):
            if frame_index == 8_000 and not profile.articulations[instrument.articulation].one_shot:
                instrument.handle_event(
                    PerformanceEvent(frame_index, 20, "note_off", {"note_id": 1}),
                    tuning,
                )
            left, right = instrument.render_frame()
            peak = max(peak, abs(left), abs(right))
            digest.update(struct.pack("<ff", left, right))
        return digest.hexdigest(), peak

    @pytest.mark.listening
    def test_render_is_deterministic_audible_and_unclipped(self) -> None:
        for key in MANIFESTS:
            first_hash, first_peak = self._render_digest_and_peak(key)
            gc.collect()
            second_hash, second_peak = self._render_digest_and_peak(key)
            self.assertEqual(first_hash, second_hash, key)
            self.assertEqual(first_peak, second_peak, key)
            self.assertGreater(first_peak, 0.001, key)
            self.assertLess(first_peak, 1.0, key)


if __name__ == "__main__":
    unittest.main()
