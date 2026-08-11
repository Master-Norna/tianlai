import gc
import hashlib
import json
import math
from pathlib import Path
import struct
import unittest

import pytest

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament
from tianlai.vpo_strings import vpo_regions_to_manifest


ROOT = Path(__file__).resolve().parents[1]
STRING_ROOT = ROOT / "乐器" / "管弦乐" / "弦乐组"
VPO_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3"
WAVE_ROOT = VPO_ROOT / "libs"
MANIFESTS = {
    "bass": STRING_ROOT / "低音提琴" / "乐器.json",
}
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(WAVE_ROOT.is_dir(), "Virtual Playing Orchestra wave files are not installed")
class VpoSoloStringTests(unittest.TestCase):
    def create_string(self, key: str, **overrides):
        path = MANIFESTS[key]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update(overrides)
        return create_instrument(manifest, 48_000, base_directory=str(path.parent))

    def test_candidates_never_silently_fall_back_to_gm(self) -> None:
        for path in MANIFESTS.values():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["type"], "vpo_solo_string")
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

    def test_vpo_parser_preserves_unquoted_windows_paths_with_spaces(self) -> None:
        regions = vpo_regions_to_manifest(
            VPO_ROOT / "Strings" / "bass-SOLO-sustain.sfz",
            use_embedded_loops=True,
        )
        self.assertEqual(len(regions), 12)
        self.assertTrue(all(Path(region["sample"]).is_file() for region in regions))
        self.assertTrue(
            all("Solo Contrabass" in Path(region["sample"]).parts for region in regions)
        )

    def test_real_sfz_region_counts_ranges_layers_and_loops(self) -> None:
        bass = self.create_string("bass")
        self.assertEqual(
            {name: len(engine.regions) for name, engine in bass.engines.items()},
            {
                "sustain": 12,
                "slow_sustain": 12,
                "staccato": 22,
                "pizzicato": 21,
                "accent_attack": 22,
                "accent_sustain": 12,
            },
        )
        self.assertEqual(
            sum(region.loop_start is not None for region in bass.engines["sustain"].regions),
            12,
        )
        self.assertEqual(
            {region.delay_seconds for region in bass.engines["accent_sustain"].regions},
            {0.12},
        )

    def test_manifest_release_overrides_all_gated_sustain_regions(self) -> None:
        bass = self.create_string("bass", release_seconds=0.17)
        for engine_name in ("sustain", "slow_sustain", "accent_sustain"):
            self.assertTrue(
                all(
                    region.release_seconds == 0.17
                    for region in bass.engines[engine_name].regions
                )
            )

    def test_sustained_regions_use_generated_pitch_calibration(self) -> None:
        checks = {
            "bass": "BKCtbss_SusVib_E1_v3_rr1-PB-loop.wav",
        }
        for key, filename in checks.items():
            instrument = self.create_string(key)
            calibration = json.loads(
                (MANIFESTS[key].parent / "音准校准.json").read_text(encoding="utf-8")
            )
            expected = next(
                item["measured_hz"]
                for path, item in calibration["samples"].items()
                if path.endswith(filename)
            )
            region = next(
                region
                for region in instrument.engines["sustain"].regions
                if region.path.name == filename
            )
            self.assertAlmostEqual(region.root_pitch_hz, expected, places=5)

    def test_asset_relative_sample_gain_override_is_applied_exactly(self) -> None:
        manifest_path = MANIFESTS["bass"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        specification = manifest["sample_gain_db_overrides"][0]
        self.assertEqual(specification["sample_variant"], "SOLO")
        override_path = specification["sample"]
        correction_db = specification["gain_db"]
        instrument = create_instrument(
            manifest,
            48_000,
            base_directory=str(manifest_path.parent),
        )
        corrected = next(
            region
            for region in instrument.engines["sustain"].regions
            if region.path.name == Path(override_path).name
        )
        upstream = next(
            region
            for region in vpo_regions_to_manifest(
                VPO_ROOT / "Strings" / "bass-SOLO-sustain.sfz",
                use_embedded_loops=True,
            )
            if Path(region["sample"]).name == Path(override_path).name
        )
        expected_gain = 10.0 ** (
            (float(upstream["gain_db"]) + float(correction_db)) / 20.0
        )
        self.assertAlmostEqual(corrected.gain, expected_gain, places=12)

        manifest["sample_gain_db_overrides"] = [
            {
                "sample_variant": "SOLO",
                "sample": "libs/VSCO2-CE/Strings/Solo Contrabass/missing.wav",
                "gain_db": 1.0,
            }
        ]
        with self.assertRaisesRegex(ValueError, "did not match loaded VPO regions"):
            create_instrument(
                manifest,
                48_000,
                base_directory=str(manifest_path.parent),
            )

    def test_sample_gain_override_is_scoped_to_the_selected_variant(self) -> None:
        manifest_path = MANIFESTS["bass"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        # The tracked correction belongs only to the SOLO source family.  SEC
        # is a legal roster override and must load without applying or trying
        # to resolve a path from another family.
        manifest["sample_variant"] = "SEC"
        section = create_instrument(
            manifest,
            48_000,
            base_directory=str(manifest_path.parent),
        )
        self.assertTrue(section.engines["sustain"].regions)
        self.assertTrue(
            all(
                "Solo Contrabass" not in region.path.parts
                for region in section.engines["sustain"].regions
            )
        )

        # A correction explicitly scoped to the selected SEC family remains
        # fail-closed when its exact path is not in the loaded SFZ mappings.
        manifest["sample_gain_db_overrides"] = [
            {
                "sample_variant": "SEC",
                "sample": "Strings/missing-sec.wav",
                "gain_db": 1.0,
            }
        ]
        with self.assertRaisesRegex(ValueError, "did not match loaded VPO regions"):
            create_instrument(
                manifest,
                48_000,
                base_directory=str(manifest_path.parent),
            )

    def test_bass_e2_zone_is_perceptually_level_with_its_neighbours(self) -> None:
        import numpy as np

        tuning = EqualTemperament(440.0)

        def isolated_level_db(midi_note: int) -> float:
            instrument = self.create_string("bass")
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {
                        "note_id": 1,
                        "midi_note": midi_note,
                        "velocity": 0.72,
                    },
                ),
                tuning,
            )
            frames = np.asarray(
                [instrument.render_frame() for _ in range(round(0.44 * 48_000))],
                dtype=np.float64,
            )
            steady = frames[round(0.08 * 48_000) :]
            rms = float(np.sqrt(np.mean(steady * steady)))
            return 20.0 * math.log10(rms)

        corrected = [isolated_level_db(note) for note in (39, 40)]
        neighbours = [isolated_level_db(note) for note in (37, 38, 41, 42)]
        reference = float(np.median(neighbours))
        for note, level in zip((39, 40), corrected, strict=True):
            self.assertLessEqual(
                abs(level - reference),
                2.0,
                f"MIDI {note} remains {level - reference:+.2f} dB from neighbours",
            )

    def test_candidate_sampled_ranges_and_articulations_are_enforced(self) -> None:
        tuning = EqualTemperament()
        expected = {"bass": (24, 67)}
        articulations = (
            "sustain",
            "slow_sustain",
            "staccato",
            "pizzicato",
            "accent",
        )
        for key, (minimum, maximum) in expected.items():
            instrument = self.create_string(key)
            for name in articulations:
                instrument.handle_event(
                    PerformanceEvent(0, 0, "articulation", {"name": name}), tuning
                )
            with self.assertRaisesRegex(ValueError, "outside the sampled"):
                instrument.handle_event(
                    PerformanceEvent(
                        0,
                        1,
                        "note_on",
                        {"note_id": 1, "midi_note": minimum - 1, "velocity": 0.8},
                    ),
                    tuning,
                )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                instrument.handle_event(
                    PerformanceEvent(0, 2, "articulation", {"name": "tremolo"}), tuning
                )

    def test_expression_pedal_and_release_semantics(self) -> None:
        tuning = EqualTemperament()
        instrument = self.create_string("bass")
        instrument.handle_event(
            PerformanceEvent(0, 0, "control", {"name": "expression", "value": 0.5}),
            tuning,
        )
        self.assertAlmostEqual(instrument.expression_target, 0.5**1.35)
        instrument.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 7, "midi_note": 40, "velocity": 0.75},
            ),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(100, 2, "control", {"name": "sustain_pedal", "value": 1.0}),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(200, 3, "note_off", {"note_id": 7, "release_velocity": 0.3}),
            tuning,
        )
        voice = instrument.engines["sustain"].voices[7]
        self.assertTrue(voice.pending_release)
        self.assertFalse(voice.released)
        instrument.handle_event(
            PerformanceEvent(300, 4, "control", {"name": "sustain_pedal", "value": 0.0}),
            tuning,
        )
        self.assertTrue(voice.released)

    def _render_digest_and_peak(self, key: str) -> tuple[str, float]:
        instrument = self.create_string(key)
        tuning = EqualTemperament()
        note = 40
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}), tuning
        )
        instrument.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 1, "midi_note": note, "velocity": 0.82},
            ),
            tuning,
        )
        digest = hashlib.sha256()
        peak = 0.0
        for frame_index in range(36_000):
            if frame_index == 18_000:
                instrument.handle_event(
                    PerformanceEvent(
                        frame_index,
                        2,
                        "note_off",
                        {"note_id": 1, "release_velocity": 0.5},
                    ),
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
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_peak, second_peak)
            self.assertGreater(first_peak, 0.02)
            self.assertLess(first_peak, 1.0)


if __name__ == "__main__":
    unittest.main()
