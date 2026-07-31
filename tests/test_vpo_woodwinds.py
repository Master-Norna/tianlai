import gc
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

import pytest

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament
from tianlai.vpo_woodwinds import (
    create_vpo_solo_woodwind,
    woodwind_source_regions,
)


ROOT = Path(__file__).resolve().parents[1]
WOODWIND_ROOT = ROOT / "乐器" / "管弦乐" / "木管组"
VPO_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3"
WAVE_ROOT = VPO_ROOT / "libs"
MANIFESTS = {
    "clarinet": WOODWIND_ROOT / "单簧管" / "乐器.json",
    "oboe": WOODWIND_ROOT / "双簧管" / "乐器.json",
    "bassoon": WOODWIND_ROOT / "大管" / "乐器.json",
    "piccolo": WOODWIND_ROOT / "短笛" / "乐器.json",
    "english-horn": WOODWIND_ROOT / "英国管" / "乐器.json",
}
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(WAVE_ROOT.is_dir(), "Virtual Playing Orchestra wave files are not installed")
class VpoWoodwindTests(unittest.TestCase):
    def create_woodwind(self, key: str):
        path = MANIFESTS[key]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return create_instrument(manifest, 48_000, base_directory=str(path.parent))

    def test_candidate_manifests_record_sounding_and_written_ranges(self) -> None:
        expected = {
            "clarinet": (50, 94, 52, 96, -2),
            "oboe": (58, 93, 58, 93, 0),
            "bassoon": (34, 75, 34, 75, 0),
            "piccolo": (74, 108, 62, 96, 12),
            "english-horn": (52, 82, 59, 89, -7),
        }
        for key, ranges in expected.items():
            manifest = json.loads(MANIFESTS[key].read_text(encoding="utf-8"))
            self.assertEqual(manifest["quality_tier"], "formal")
            self.assertEqual(
                manifest["collaboration_review_status"], "untested"
            )
            self.assertEqual(manifest["type"], "vpo_woodwind")
            self.assertEqual(manifest["fallback_policy"], "explicit_only_no_silent_gm")
            self.assertEqual(manifest["pitch_input"], "sounding")
            self.assertEqual(
                (
                    manifest["note_min"],
                    manifest["note_max"],
                    manifest["written_note_min"],
                    manifest["written_note_max"],
                    manifest["written_to_sounding_semitones"],
                ),
                ranges,
            )
            self.assertEqual(ranges[2] + ranges[4], ranges[0])
            self.assertEqual(ranges[3] + ranges[4], ranges[1])

    def test_real_solo_sfz_layers_ranges_and_loops(self) -> None:
        expected_counts = {
            "clarinet": (26, 26),
            "oboe": (9, 9),
            "bassoon": (13, 66),
            "piccolo": (10, 10),
            "english-horn": (9, 9),
        }
        for key, (sustain_count, attack_count) in expected_counts.items():
            instrument = self.create_woodwind(key)
            self.assertEqual(
                {name: len(engine.regions) for name, engine in instrument.engines.items()},
                {
                    "sustain": sustain_count,
                    "slow_sustain": sustain_count,
                    "staccato": attack_count,
                    "accent_attack": attack_count,
                    "accent_sustain": sustain_count,
                },
            )
            for name in ("sustain", "slow_sustain", "accent_sustain"):
                self.assertEqual(
                    sum(
                        region.loop_start is not None
                        for region in instrument.engines[name].regions
                    ),
                    sustain_count,
                )
            self.assertEqual(
                sum(
                    region.loop_start is not None
                    for region in instrument.engines["staccato"].regions
                ),
                0,
            )

    def test_calibration_and_resource_hash_reports_cover_every_selected_wave(self) -> None:
        for key, manifest_path in MANIFESTS.items():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            calibration = json.loads(
                (manifest_path.parent / "音准校准.json").read_text(encoding="utf-8")
            )
            verification = json.loads(
                (manifest_path.parent / "资源核验.json").read_text(encoding="utf-8")
            )
            regions = woodwind_source_regions(VPO_ROOT, manifest["sfz_prefix"])
            sample_paths = sorted(
                {
                    Path(region["sample"])
                    for region_set in regions.values()
                    for region in region_set
                },
                key=lambda path: path.relative_to(VPO_ROOT).as_posix(),
            )
            self.assertEqual(len(sample_paths), verification["sample_count"])
            self.assertEqual(len(sample_paths), calibration["summary"]["sample_count"])
            self.assertEqual(
                {path.relative_to(VPO_ROOT).as_posix() for path in sample_paths},
                set(calibration["samples"]),
            )
            lines = []
            for path in sample_paths:
                relative = path.relative_to(VPO_ROOT).as_posix()
                lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}\n")
            self.assertEqual(
                hashlib.sha256("".join(lines).encode("utf-8")).hexdigest(),
                verification["sample_set_sha256"],
            )
            for relative, expected_hash in verification["source_sfz_sha256"].items():
                self.assertEqual(
                    hashlib.sha256((VPO_ROOT / relative).read_bytes()).hexdigest(),
                    expected_hash,
                )

            instrument = self.create_woodwind(key)
            checked_path = sample_paths[0].relative_to(VPO_ROOT).as_posix()
            expected_hz = calibration["samples"][checked_path]["measured_hz"]
            checked_region = next(
                region
                for engine in instrument.engines.values()
                for region in engine.regions
                if region.path == sample_paths[0]
            )
            self.assertAlmostEqual(checked_region.root_pitch_hz, expected_hz, places=5)

    def test_all_articulations_expression_breath_and_ranges(self) -> None:
        tuning = EqualTemperament()
        for key, manifest_path in MANIFESTS.items():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            instrument = self.create_woodwind(key)
            for name in ("sustain", "slow_sustain", "staccato", "accent"):
                instrument.handle_event(
                    PerformanceEvent(0, 0, "articulation", {"name": name}), tuning
                )
            instrument.handle_event(
                PerformanceEvent(0, 1, "control", {"name": "expression", "value": 0.5}),
                tuning,
            )
            instrument.handle_event(
                PerformanceEvent(0, 2, "control", {"name": "breath", "value": 0.4}),
                tuning,
            )
            self.assertAlmostEqual(instrument.expression_target, 0.5**1.3)
            self.assertAlmostEqual(instrument.breath_target, 0.4**1.08)
            for note in (manifest["note_min"] - 1, manifest["note_max"] + 1):
                with self.assertRaisesRegex(ValueError, "outside the sampled sounding"):
                    instrument.handle_event(
                        PerformanceEvent(
                            0,
                            3,
                            "note_on",
                            {"note_id": 1, "midi_note": note, "velocity": 0.8},
                        ),
                        tuning,
                    )
            with self.assertRaisesRegex(ValueError, "unsupported"):
                instrument.handle_event(
                    PerformanceEvent(0, 4, "articulation", {"name": "flutter_tongue"}),
                    tuning,
                )

    def test_piccolo_written_note_is_not_mistaken_for_sounding_pitch(self) -> None:
        piccolo = self.create_woodwind("piccolo")
        tuning = EqualTemperament()
        with self.assertRaisesRegex(ValueError, "D5-C9"):
            piccolo.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 62, "velocity": 0.8},
                ),
                tuning,
            )
        piccolo.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 2, "midi_note": 74, "velocity": 0.8},
            ),
            tuning,
        )
        self.assertEqual(len(piccolo.note_routes[2].voices), 1)

    def test_accent_layers_short_release_and_monophonic_choke(self) -> None:
        instrument = self.create_woodwind("oboe")
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}), tuning
        )
        instrument.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 1, "midi_note": 69, "velocity": 0.82},
            ),
            tuning,
        )
        first_route = instrument.note_routes[1]
        self.assertEqual(len(first_route.voices), 2)
        attack = next(voice for voice in first_route.voices if not voice.sustained)
        sustained = next(voice for voice in first_route.voices if voice.sustained)
        for _ in range(instrument._short_gate_samples):
            instrument.render_frame()
        self.assertTrue(instrument.engines[attack.engine_name].voices[attack.note_id].released)
        self.assertFalse(
            instrument.engines[sustained.engine_name].voices[sustained.note_id].released
        )

        instrument.handle_event(
            PerformanceEvent(
                instrument._short_gate_samples,
                2,
                "note_on",
                {"note_id": 2, "midi_note": 71, "velocity": 0.8},
            ),
            tuning,
        )
        self.assertEqual(first_route.voices, ())
        old_sustain = instrument.engines[sustained.engine_name].voices[sustained.note_id]
        self.assertTrue(old_sustain.released)
        self.assertEqual(
            old_sustain.release_samples, round(instrument.legato_release_seconds * 48_000)
        )

    def test_custom_a4_changes_sample_increment_without_changing_mapping(self) -> None:
        path = MANIFESTS["clarinet"]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        increments = []
        selected_paths = []
        for a4 in (440.0, 432.0):
            instrument = create_instrument(
                manifest, 48_000, base_directory=str(path.parent)
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 69, "velocity": 0.8},
                ),
                EqualTemperament(a4),
            )
            route = instrument.note_routes[1].voices[0]
            voice = instrument.engines[route.engine_name].voices[route.note_id]
            increments.append(voice.increment)
            selected_paths.append(voice.region.path)
        self.assertEqual(selected_paths[0], selected_paths[1])
        self.assertAlmostEqual(increments[1] / increments[0], 432.0 / 440.0, places=9)

    def test_missing_resource_tree_fails_explicitly(self) -> None:
        path = MANIFESTS["oboe"]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temporary:
            manifest["asset_root"] = "不存在的 VPO 音源"
            with self.assertRaisesRegex(ValueError, "VPO 木管音源不存在"):
                create_vpo_solo_woodwind(
                    manifest=manifest, sample_rate=48_000, base_directory=temporary
                )

    def _render_digest_and_peak(self, key: str) -> tuple[str, float]:
        instrument = self.create_woodwind(key)
        manifest = json.loads(MANIFESTS[key].read_text(encoding="utf-8"))
        note = round((manifest["note_min"] + manifest["note_max"]) / 2)
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}), tuning
        )
        instrument.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 1, "midi_note": note, "velocity": 0.84},
            ),
            tuning,
        )
        digest = hashlib.sha256()
        peak = 0.0
        for frame_index in range(24_000):
            if frame_index == 12_000:
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
            left = float(left)
            right = float(right)
            peak = max(peak, abs(left), abs(right))
            digest.update(struct.pack("<ff", left, right))
        return digest.hexdigest(), peak

    @pytest.mark.listening
    def test_render_is_deterministic_audible_finite_and_unclipped(self) -> None:
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
