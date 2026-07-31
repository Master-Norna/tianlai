from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import struct
import unittest

import pytest

from tianlai.dedicated_sfz import dedicated_regions_to_manifest
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "乐器" / "管弦乐" / "打击乐组" / "定音鼓"
MANIFEST_PATH = DIRECTORY / "乐器.json"
VCSL_ROOT = ROOT / "音源" / "VCSL"
pytestmark = pytest.mark.external_assets
HIT_SFZ = "Membranophones/Struck Membranophones/Timpani 2 - Scale.sfz"
ROLL_SFZ = "Membranophones/Struck Membranophones/Timpani 1 - Roll.sfz"
EXPECTED_SAMPLE_SET_SHA256 = (
    "d323d3c2a7587be4948bf831b4956c6e0d6fec73aceb4255a425226be8c1803a"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless(VCSL_ROOT.is_dir(), "VCSL is not installed")
class VcslTimpaniTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.resource = load_json(DIRECTORY / "资源核验.json")
        cls.pitch = load_json(DIRECTORY / "音准校准.json")
        cls.comparison = load_json(DIRECTORY / "VCSL候选比较.json")
        cls.hit, cls.hit_metadata = dedicated_regions_to_manifest(
            VCSL_ROOT / HIT_SFZ,
            asset_root=VCSL_ROOT,
            trigger="attack",
            use_embedded_loops=False,
            stable_prefix=HIT_SFZ,
        )
        cls.roll, cls.roll_metadata = dedicated_regions_to_manifest(
            VCSL_ROOT / ROLL_SFZ,
            asset_root=VCSL_ROOT,
            trigger="attack",
            use_embedded_loops=False,
            stable_prefix=ROLL_SFZ,
        )

    def create_timpani(self):
        return create_instrument(
            self.manifest,
            48_000,
            base_directory=str(DIRECTORY),
        )

    @staticmethod
    def articulation(instrument, name: str) -> None:
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": name}),
            EqualTemperament(),
        )

    @staticmethod
    def note_on(
        instrument,
        *,
        note_id: int,
        midi_note: float,
        velocity: float,
        sequence: int = 0,
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
            EqualTemperament(),
        )

    @staticmethod
    def route_paths(instrument, note_id: int) -> list[Path]:
        route = instrument.routes[note_id]
        runtime = instrument.articulations[route.articulation]
        return [
            runtime.attack_layers[voice.layer_index]
            .engine.voices[voice.internal_note_id]
            .region.path
            for voice in route.voices
        ]

    def test_strict_cc0_release_and_every_selected_file_are_hash_locked(self) -> None:
        self.assertEqual(self.manifest["type"], "dedicated_sfz")
        self.assertEqual(self.manifest["license"], "CC0-1.0")
        self.assertEqual(self.manifest["license_status"], "approved")
        self.assertEqual(self.manifest["upstream_version"], "1.2.2-RC")
        self.assertEqual(
            self.manifest["upstream_commit"],
            "b6e6ac82d22248edee98a0bde185eb9ef6d439ad",
        )
        self.assertEqual(self.resource["sample_count"], 64)
        self.assertEqual(self.resource["sample_bytes"], 132_588_162)
        self.assertEqual(
            self.resource["sample_set_sha256"],
            EXPECTED_SAMPLE_SET_SHA256,
        )
        self.assertEqual(len(self.resource["sample_sha256"]), 64)
        for relative, expected in {
            **self.resource["source_sfz_sha256"],
            **self.resource["evidence_sha256"],
            **self.resource["sample_sha256"],
        }.items():
            actual = hashlib.sha256((VCSL_ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)

    def test_hit_is_54_pcm24_regions_with_three_real_layers_and_true_rr2(
        self,
    ) -> None:
        self.assertEqual(len(self.hit), 54)
        self.assertEqual(len({row["sample"] for row in self.hit}), 54)
        self.assertEqual(
            (
                min(row["key_min"] for row in self.hit),
                max(row["key_max"] for row in self.hit),
            ),
            (38.0, 59.0),
        )
        for note in range(38, 60):
            matches = [
                row
                for row in self.hit
                if row["key_min"] <= note <= row["key_max"]
            ]
            self.assertEqual(len(matches), 6, note)

        rr_families: dict[tuple, set[int]] = defaultdict(set)
        velocity_layers: dict[tuple[float, float], set[tuple]] = defaultdict(set)
        for row in self.hit:
            metadata = self.hit_metadata[row["stable_key"]]
            layer = (
                row["velocity_min"],
                row["velocity_max"],
                metadata.velocity_fade_in,
                metadata.velocity_fade_out,
            )
            key_range = (row["key_min"], row["key_max"])
            velocity_layers[key_range].add(layer)
            rr_families[(key_range, layer)].add(row["round_robin_position"])
            self.assertEqual(row["round_robin_length"], 2)
            self.assertTrue(
                metadata.velocity_fade_in is not None
                or metadata.velocity_fade_out is not None
            )
        self.assertEqual(len(rr_families), 27)
        self.assertTrue(
            all(positions == {1, 2} for positions in rr_families.values())
        )
        self.assertTrue(all(len(layers) == 3 for layers in velocity_layers.values()))
        self.assertEqual(
            sum(int(row["offset_frames"] > 0) for row in self.hit),
            40,
        )
        self.assertEqual(max(row["offset_frames"] for row in self.hit), 398)
        hit_report = self.resource["articulations"]["hit"]
        self.assertEqual(
            hit_report["sample_formats"],
            {".wav:44100Hz:2ch:PCM_24": 54},
        )
        self.assertEqual(hit_report["recorded_velocity_layer_count"], 3)
        self.assertEqual(hit_report["true_round_robin_count"], 2)
        self.assertEqual(
            hit_report["offsets"]["discarded_peak_ratio_over_5_percent_count"],
            0,
        )

    def test_roll_is_ten_finite_natural_samples_with_no_fake_rr_or_loop(
        self,
    ) -> None:
        self.assertEqual(len(self.roll), 10)
        self.assertEqual(len({row["sample"] for row in self.roll}), 10)
        self.assertEqual(
            (
                min(row["key_min"] for row in self.roll),
                max(row["key_max"] for row in self.roll),
            ),
            (41.0, 55.0),
        )
        self.assertTrue(
            all("round_robin_length" not in row for row in self.roll)
        )
        self.assertTrue(
            all(
                self.roll_metadata[row["stable_key"]].velocity_fade_in is None
                and self.roll_metadata[row["stable_key"]].velocity_fade_out is None
                for row in self.roll
            )
        )
        roll_report = self.resource["articulations"]["roll"]
        self.assertEqual(roll_report["recorded_velocity_layer_count"], 2)
        self.assertEqual(roll_report["true_round_robin_count"], 0)
        self.assertEqual(roll_report["embedded_loop_sample_count"], 0)
        self.assertGreaterEqual(roll_report["duration_seconds"]["minimum"], 15.0)
        self.assertEqual(
            roll_report["sample_formats"],
            {".wav:44100Hz:2ch:PCM_16": 10},
        )

    def test_runtime_preserves_crossfades_rr_and_articulation_specific_ranges(
        self,
    ) -> None:
        instrument = self.create_timpani()
        self.assertEqual(
            instrument.articulation_playable_ranges,
            {
                "hit": ((38.0, 59.0),),
                "roll": ((41.0, 55.0),),
            },
        )
        self.assertEqual(len(instrument.articulations["hit"].attack_layers), 3)
        self.assertEqual(len(instrument.articulations["roll"].attack_layers), 1)

        # Velocity 55 lies inside the low/middle upstream crossfade.
        self.note_on(
            instrument,
            note_id=1,
            midi_note=48,
            velocity=55 / 127,
            sequence=1,
        )
        self.assertEqual(len(instrument.routes[1].voices), 2)

        # Low-velocity repeated hits consume the two explicit SFZ positions.
        self.note_on(
            instrument,
            note_id=2,
            midi_note=48,
            velocity=0.2,
            sequence=2,
        )
        self.note_on(
            instrument,
            note_id=3,
            midi_note=48,
            velocity=0.2,
            sequence=3,
        )
        first = self.route_paths(instrument, 2)
        second = self.route_paths(instrument, 3)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first, second)

        self.note_on(
            instrument,
            note_id=4,
            midi_note=38,
            velocity=0.7,
        )
        self.note_on(
            instrument,
            note_id=5,
            midi_note=59,
            velocity=0.7,
        )
        with self.assertRaisesRegex(ValueError, "outside declared range"):
            self.note_on(
                instrument,
                note_id=6,
                midi_note=37,
                velocity=0.7,
            )

        self.articulation(instrument, "roll")
        self.note_on(
            instrument,
            note_id=7,
            midi_note=41,
            velocity=0.7,
        )
        self.note_on(
            instrument,
            note_id=8,
            midi_note=55,
            velocity=0.7,
        )
        with self.assertRaisesRegex(ValueError, "articulation 'roll'.*41..55"):
            self.note_on(
                instrument,
                note_id=9,
                midi_note=40,
                velocity=0.7,
            )
        with self.assertRaisesRegex(ValueError, "articulation 'roll'.*41..55"):
            self.note_on(
                instrument,
                note_id=10,
                midi_note=56,
                velocity=0.7,
            )

    def test_no_synthetic_variation_and_roll_releases_without_a_loop(self) -> None:
        instrument = self.create_timpani()
        for runtime in instrument.articulations.values():
            for layer in runtime.attack_layers:
                for region in layer.engine.regions:
                    self.assertEqual(region.pitch_random_cents, 0.0)
                    self.assertEqual(region.amplitude_random_db, 0.0)
                    self.assertEqual(region.delay_random_seconds, 0.0)
                    self.assertIsNone(region.loop_start)

        self.articulation(instrument, "roll")
        self.note_on(
            instrument,
            note_id=1,
            midi_note=49,
            velocity=0.72,
        )
        route = instrument.routes[1]
        routed = route.voices[0]
        voice = (
            instrument.articulations["roll"]
            .attack_layers[routed.layer_index]
            .engine.voices[routed.internal_note_id]
        )
        self.assertIsNone(voice.region.loop_start)
        instrument.handle_event(
            PerformanceEvent(1, 1, "note_off", {"note_id": 1}),
            EqualTemperament(),
        )
        self.assertTrue(voice.released)
        self.assertEqual(voice.release_samples, 48_000)

    def test_inharmonic_report_never_becomes_a_fake_fft_tuning_correction(
        self,
    ) -> None:
        self.assertEqual(self.pitch["summary"]["sample_count"], 64)
        self.assertEqual(self.pitch["summary"]["automatic_correction_count"], 0)
        self.assertEqual(self.pitch["summary"]["human_spectral_review"], "pending")
        self.assertTrue(
            all(
                record["automatic_tuning_correction_cents"] is None
                for record in self.pitch["samples"].values()
            )
        )
        self.assertIn(
            "inharmonic",
            self.pitch["pitch_semantics"],
        )

        # Playback follows the SFZ root/tune claim, not the diagnostic peak.
        instrument = self.create_timpani()
        source_row = self.hit[0]
        sample_path = Path(source_row["sample"]).resolve()
        region = next(
            region
            for layer in instrument.articulations["hit"].attack_layers
            for region in layer.engine.regions
            if region.path.resolve() == sample_path
        )
        expected = 440.0 * (
            2.0
            ** (
                (
                    float(source_row["root_midi"])
                    - 69.0
                    + float(source_row["measured_tuning_cents"]) / 100.0
                )
                / 12.0
            )
        )
        self.assertAlmostEqual(region.root_pitch_hz, expected, places=9)

    def test_candidate_comparison_records_honest_selection_and_limits(self) -> None:
        candidates = self.comparison["candidates"]
        self.assertEqual(
            set(candidates),
            {"timpani_1_hit", "timpani_1_roll", "timpani_2_scale"},
        )
        self.assertEqual(self.comparison["decision"]["hit"], "timpani_2_scale")
        self.assertEqual(self.comparison["decision"]["roll"], "timpani_1_roll")
        self.assertEqual(
            self.comparison["decision"]["forbidden_synthetic_claims"],
            {
                "pitch_random_cents": 0,
                "amplitude_random_db": 0,
                "delay_random_seconds": 0,
                "manufactured_velocity_layers": 0,
                "manufactured_round_robins": 0,
                "manufactured_loops": 0,
            },
        )
        limitations = " ".join(
            self.comparison["decision"]["declared_limitations"]
        )
        self.assertIn("timbral seam", limitations)
        self.assertIn("no loop", limitations)

    def _render_digest_and_peak(self) -> tuple[str, float]:
        instrument = self.create_timpani()
        tuning = EqualTemperament()
        events = {
            0: [
                PerformanceEvent(0, 0, "note_on", {
                    "note_id": 1,
                    "midi_note": 48,
                    "velocity": 55 / 127,
                })
            ],
            6_000: [
                PerformanceEvent(6_000, 1, "articulation", {"name": "roll"}),
                PerformanceEvent(6_000, 2, "note_on", {
                    "note_id": 2,
                    "midi_note": 49,
                    "velocity": 0.8,
                }),
            ],
            14_000: [
                PerformanceEvent(14_000, 3, "note_off", {"note_id": 2})
            ],
        }
        digest = hashlib.sha256()
        peak = 0.0
        for frame in range(24_000):
            for event in events.get(frame, ()):
                instrument.handle_event(event, tuning)
            left, right = instrument.render_frame()
            peak = max(peak, abs(left), abs(right))
            digest.update(struct.pack("<ff", left, right))
        return digest.hexdigest(), peak

    @pytest.mark.listening
    def test_render_is_deterministic_audible_and_unclipped(self) -> None:
        first_hash, first_peak = self._render_digest_and_peak()
        second_hash, second_peak = self._render_digest_and_peak()
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_peak, second_peak)
        self.assertGreater(first_peak, 0.001)
        self.assertLess(first_peak, 1.0)
        self.assertGreaterEqual(
            self.resource["audio_integrity"]["minimum_single_voice_headroom_db"],
            6.0,
        )


if __name__ == "__main__":
    unittest.main()
