import gc
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

import pytest

from tianlai.audio import read_audio_float
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.mtg_sax import (
    create_mtg_sax,
    flac_loop_points,
    mtg_sax_source_inventory,
    parse_mtg_sfz,
)
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
SAX_ROOT = ROOT / "乐器" / "现代管乐"
ASSET_ROOT = ROOT / "音源" / "MTG-Solo-Sax"
MANIFESTS = {
    "baritone": SAX_ROOT / "上低音萨克斯" / "乐器.json",
    "alto": SAX_ROOT / "中音萨克斯" / "乐器.json",
    "tenor": SAX_ROOT / "次中音萨克斯" / "乐器.json",
    "soprano": SAX_ROOT / "高音萨克斯" / "乐器.json",
}
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(
    (ASSET_ROOT / "MTG Solo Saxophones" / "Samples").is_dir(),
    "MTG Solo Saxophones samples are not installed",
)
class MtgSoloSaxTests(unittest.TestCase):
    def manifest(self, key: str) -> dict:
        return json.loads(MANIFESTS[key].read_text(encoding="utf-8"))

    def create_sax(self, key: str):
        path = MANIFESTS[key]
        return create_instrument(
            self.manifest(key), 48_000, base_directory=str(path.parent)
        )

    def test_flac_decodes_and_preserved_loop_is_read(self) -> None:
        sample = (
            ASSET_ROOT
            / "MTG Solo Saxophones"
            / "Samples"
            / "alt_p_12.flac"
        )
        sample_rate, frames = read_audio_float(sample)
        self.assertEqual(sample_rate, 48_000)
        self.assertEqual(frames.shape, (232_244, 2))
        self.assertEqual(flac_loop_points(sample), (166_073, 170_287))

    def test_preprocessor_expands_nested_includes_and_macros(self) -> None:
        sfz = ASSET_ROOT / "MTG Solo Saxophones" / "MTG Alto Sax.sfz"
        document = parse_mtg_sfz(sfz)
        self.assertEqual(document.macros["$EXT"], "flac")
        self.assertEqual(len(document.source_files), 11)
        self.assertFalse(any("$" in region["sample"] for region in document.regions))
        self.assertEqual(
            sum(region.get("master_label") == "attack" for region in document.regions),
            198,
        )
        self.assertEqual(
            sum(region.get("master_label") == "legato" for region in document.regions),
            198,
        )
        self.assertEqual(
            sum(region.get("master_label") == "noises" for region in document.regions),
            104,
        )

    def test_candidate_ranges_transposition_and_status(self) -> None:
        expected = {
            "baritone": (36, 69, 57, 90, -21, "E-flat"),
            "alto": (49, 81, 58, 90, -9, "E-flat"),
            "tenor": (44, 76, 58, 90, -14, "B-flat"),
            "soprano": (56, 88, 58, 90, -2, "B-flat"),
        }
        for key, values in expected.items():
            manifest = self.manifest(key)
            self.assertEqual(manifest["type"], "mtg_solo_sax")
            self.assertEqual(manifest["quality_tier"], "formal")
            self.assertEqual(
                manifest["collaboration_review_status"], "untested"
            )
            self.assertEqual(
                manifest["fallback_policy"], "explicit_only_no_silent_gm"
            )
            self.assertEqual(manifest["pitch_input"], "sounding")
            actual = (
                manifest["note_min"],
                manifest["note_max"],
                manifest["written_note_min"],
                manifest["written_note_max"],
                manifest["written_to_sounding_semitones"],
                manifest["transposing_instrument_key"],
            )
            self.assertEqual(actual, values)
            self.assertEqual(values[2] + values[4], values[0])
            self.assertEqual(values[3] + values[4], values[1])

    def test_real_layers_three_rr_loops_and_noise_pools(self) -> None:
        expected = {
            "baritone": (306, 100, 90, 40, 3),
            "alto": (198, 66, 68, 36, 2),
            "tenor": (198, 66, 64, 33, 2),
            "soprano": (198, 66, 80, 28, 2),
        }
        for key, (region_count, sample_count, breath, clicks, layers) in expected.items():
            manifest = self.manifest(key)
            inventory = mtg_sax_source_inventory(
                manifest, base_directory=MANIFESTS[key].parent
            )
            regions = inventory["pitch_regions"]
            self.assertEqual(len(regions), region_count)
            self.assertEqual(len({item["sample"] for item in regions}), sample_count)
            self.assertEqual(len(inventory["breath_regions"]), breath)
            self.assertEqual(len(inventory["key_click_regions"]), clicks)
            self.assertTrue(all(item["loop_end"] > item["loop_start"] for item in regions))
            self.assertEqual(
                len({(item["velocity_min"], item["velocity_max"]) for item in regions}),
                layers,
            )
            by_key_layer: dict[tuple, set[int]] = {}
            for item in regions:
                bucket = (
                    item["key_min"],
                    item["velocity_min"],
                    item["velocity_max"],
                )
                by_key_layer.setdefault(bucket, set()).add(item["round_robin_position"])
                self.assertEqual(item["round_robin_length"], 3)
            self.assertTrue(by_key_layer)
            self.assertTrue(all(positions == {1, 2, 3} for positions in by_key_layer.values()))

    def test_runtime_cycles_rr_in_source_sequence_order(self) -> None:
        instrument = self.create_sax("alto")
        tuning = EqualTemperament()
        selected = []
        for sequence in range(3):
            public_id = sequence + 1
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    sequence,
                    "note_on",
                    {"note_id": public_id, "midi_note": 65, "velocity": 0.62},
                ),
                tuning,
            )
            route = instrument.note_routes[public_id]
            voice = instrument.engines[route.engine_name].voices[route.note_id]
            selected.append(voice.region.round_robin_position)
        self.assertEqual(selected, [1, 2, 3])

    def test_calibration_and_hash_reports_cover_all_used_samples(self) -> None:
        for key, manifest_path in MANIFESTS.items():
            manifest = self.manifest(key)
            inventory = mtg_sax_source_inventory(
                manifest, base_directory=manifest_path.parent
            )
            calibration = json.loads(
                (manifest_path.parent / "音准校准.json").read_text(encoding="utf-8")
            )
            verification = json.loads(
                (manifest_path.parent / "资源核验.json").read_text(encoding="utf-8")
            )
            pitched = {
                Path(item["sample"]).relative_to(ASSET_ROOT).as_posix()
                for item in inventory["pitch_regions"]
            }
            all_samples = sorted(
                {
                    Path(item["sample"])
                    for group in ("pitch_regions", "breath_regions", "key_click_regions")
                    for item in inventory[group]
                },
                key=lambda path: path.relative_to(ASSET_ROOT).as_posix(),
            )
            self.assertEqual(set(calibration["samples"]), pitched)
            self.assertEqual(calibration["summary"]["sample_count"], len(pitched))
            self.assertEqual(verification["sample_count"], len(all_samples))
            lines = [
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                f"{path.relative_to(ASSET_ROOT).as_posix()}\n"
                for path in all_samples
            ]
            self.assertEqual(
                hashlib.sha256("".join(lines).encode("utf-8")).hexdigest(),
                verification["sample_set_sha256"],
            )
            for relative, expected_hash in verification["source_file_sha256"].items():
                self.assertEqual(
                    hashlib.sha256((ASSET_ROOT / relative).read_bytes()).hexdigest(),
                    expected_hash,
                )

    def test_controls_vibrato_legato_noise_and_pedal(self) -> None:
        instrument = self.create_sax("tenor")
        tuning = EqualTemperament()
        for name, value in (
            ("expression", 0.5),
            ("breath", 0.4),
            ("modulation", 1.0),
            ("noise", 0.3),
        ):
            instrument.handle_event(
                PerformanceEvent(0, 0, "control", {"name": name, "value": value}),
                tuning,
            )
        self.assertAlmostEqual(instrument.expression_target, 0.5**1.3)
        self.assertAlmostEqual(instrument.breath_target, 0.4**1.08)
        self.assertEqual(instrument.modulation_target, 1.0)
        self.assertEqual(instrument.noise_target, 0.3)

        instrument.handle_event(
            PerformanceEvent(0, 1, "articulation", {"name": "legato"}), tuning
        )
        instrument.handle_event(
            PerformanceEvent(
                0, 2, "note_on", {"note_id": 1, "midi_note": 60, "velocity": 0.7}
            ),
            tuning,
        )
        first_route = instrument.note_routes[1]
        first_voice = instrument.engines[first_route.engine_name].voices[first_route.note_id]
        base_increment = first_voice.increment
        for _ in range(5_000):
            instrument.render_frame()
        self.assertNotAlmostEqual(first_voice.increment, base_increment, places=8)

        instrument.handle_event(
            PerformanceEvent(
                5_000,
                3,
                "note_on",
                {"note_id": 2, "midi_note": 62, "velocity": 0.72},
            ),
            tuning,
        )
        second_route = instrument.note_routes[2]
        self.assertEqual(second_route.engine_name, "legato")
        second_voice = instrument.engines["legato"].voices[second_route.note_id]
        self.assertEqual(second_voice.region.offset_frames, 20_000)
        self.assertTrue(first_voice.released)

        pedal_sax = self.create_sax("tenor")
        pedal_sax.handle_event(
            PerformanceEvent(
                0, 0, "note_on", {"note_id": 9, "midi_note": 60, "velocity": 0.7}
            ),
            tuning,
        )
        route = pedal_sax.note_routes[9]
        voice = pedal_sax.engines[route.engine_name].voices[route.note_id]
        pedal_sax.handle_event(
            PerformanceEvent(
                1, 1, "control", {"name": "sustain_pedal", "value": 1.0}
            ),
            tuning,
        )
        pedal_sax.handle_event(
            PerformanceEvent(2, 2, "note_off", {"note_id": 9, "release_velocity": 0.5}),
            tuning,
        )
        self.assertTrue(voice.pending_release)
        self.assertFalse(voice.released)
        pedal_sax.handle_event(
            PerformanceEvent(
                3, 3, "control", {"name": "sustain_pedal", "value": 0.0}
            ),
            tuning,
        )
        self.assertTrue(voice.released)

    def test_ranges_and_custom_a4(self) -> None:
        for key, manifest_path in MANIFESTS.items():
            manifest = self.manifest(key)
            sax = self.create_sax(key)
            for note in (manifest["note_min"] - 1, manifest["note_max"] + 1):
                with self.assertRaisesRegex(ValueError, "outside the sampled sounding"):
                    sax.handle_event(
                        PerformanceEvent(
                            0,
                            0,
                            "note_on",
                            {"note_id": 1, "midi_note": note, "velocity": 0.8},
                        ),
                        EqualTemperament(),
                    )

        increments = []
        paths = []
        for a4 in (440.0, 432.0):
            sax = self.create_sax("alto")
            sax.handle_event(
                PerformanceEvent(
                    0, 0, "note_on", {"note_id": 1, "midi_note": 69, "velocity": 0.8}
                ),
                EqualTemperament(a4),
            )
            route = sax.note_routes[1]
            voice = sax.engines[route.engine_name].voices[route.note_id]
            increments.append(voice.increment)
            paths.append(voice.region.path)
        self.assertEqual(paths[0], paths[1])
        self.assertAlmostEqual(increments[1] / increments[0], 432.0 / 440.0, places=9)

    def test_missing_resource_tree_fails_without_gm(self) -> None:
        manifest = self.manifest("soprano")
        with tempfile.TemporaryDirectory() as temporary:
            manifest["asset_root"] = "不存在的 MTG 音源"
            with self.assertRaisesRegex(ValueError, "MTG Solo Sax 音源不存在"):
                create_mtg_sax(
                    manifest=manifest,
                    sample_rate=48_000,
                    base_directory=temporary,
                )

    def _render_digest_and_peak(self, key: str) -> tuple[str, float]:
        sax = self.create_sax(key)
        manifest = self.manifest(key)
        note = round((manifest["note_min"] + manifest["note_max"]) / 2)
        tuning = EqualTemperament()
        sax.handle_event(
            PerformanceEvent(0, 0, "control", {"name": "modulation", "value": 0.7}),
            tuning,
        )
        sax.handle_event(
            PerformanceEvent(
                0, 1, "note_on", {"note_id": 1, "midi_note": note, "velocity": 0.84}
            ),
            tuning,
        )
        digest = hashlib.sha256()
        peak = 0.0
        for frame_index in range(18_000):
            if frame_index == 10_000:
                sax.handle_event(
                    PerformanceEvent(
                        frame_index,
                        2,
                        "note_off",
                        {"note_id": 1, "release_velocity": 0.5},
                    ),
                    tuning,
                )
            left, right = sax.render_frame()
            left = float(left)
            right = float(right)
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
            self.assertGreater(first_peak, 0.005)
            self.assertLess(first_peak, 1.0)


if __name__ == "__main__":
    unittest.main()
