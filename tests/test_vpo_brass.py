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
from tianlai.vpo_brass import create_vpo_brass


ROOT = Path(__file__).resolve().parents[1]
BRASS_ROOT = ROOT / "乐器" / "管弦乐" / "铜管组"
VPO_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3"
MANIFESTS = {
    name: BRASS_ROOT / name / "乐器.json"
    for name in ("圆号", "大号", "小号", "长号", "铜管合奏")
}
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(
    (VPO_ROOT / "libs").is_dir(),
    "Virtual Playing Orchestra wave files are not installed",
)
class VpoBrassTests(unittest.TestCase):
    def create_brass(self, name: str, sample_rate: int = 48_000):
        path = MANIFESTS[name]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return create_instrument(manifest, sample_rate, base_directory=str(path.parent))

    def test_five_candidates_never_silently_fall_back_to_gm(self) -> None:
        for path in MANIFESTS.values():
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["type"], "vpo_brass")
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

        # 弱音小号已升级为"VPO 独奏小号采样 + 确定性弱音器滤波建模"的
        # dedicated_fx candidate;这里守住它不再是通用 SoundFont,且
        # 弱音链与建模声明保持显式。
        muted = json.loads(
            (BRASS_ROOT / "弱音小号" / "乐器.json").read_text(encoding="utf-8")
        )
        self.assertEqual(muted["type"], "dedicated_fx")
        self.assertEqual(muted["quality_tier"], "formal")
        self.assertNotIn("soundfont", muted)
        self.assertTrue(
            any(effect.get("type") == "peak" for effect in muted["effects"])
        )

    def test_real_vpo_region_counts_layers_ranges_and_loops(self) -> None:
        expected = {
            "圆号": [("solo", 35, 77, 39, 39)],
            "大号": [("solo", 26, 62, 9, 12)],
            "小号": [("solo", 54, 84, 54, 54)],
            "长号": [("solo", 40, 77, 20, 20)],
            "铜管合奏": [
                ("horn", 35, 77, 26, 26),
                ("trombone", 40, 77, 18, 18),
                ("trumpet", 54, 84, 23, 23),
                ("tuba", 26, 62, 9, 12),
            ],
        }
        for name, layer_expectations in expected.items():
            instrument = self.create_brass(name)
            actual = []
            for layer in instrument.layers:
                sustain = layer.engines["sustain_0"]
                staccato = layer.engines["staccato"]
                actual.append(
                    (
                        layer.name,
                        int(layer.note_min),
                        int(layer.note_max),
                        len(sustain.regions),
                        len(staccato.regions),
                    )
                )
                self.assertTrue(
                    all(region.loop_start is not None for region in sustain.regions)
                )
                self.assertTrue(
                    all(region.loop_start is None for region in staccato.regions)
                )
            self.assertEqual(actual, layer_expectations)

    def test_calibration_and_resource_freeze_are_complete(self) -> None:
        expected_calibration = {
            "圆号": 39,
            "大号": 9,
            "小号": 54,
            "长号": 20,
            "铜管合奏": 76,
        }
        expected_assets = {
            "圆号": 39,
            "大号": 21,
            "小号": 54,
            "长号": 20,
            "铜管合奏": 88,
        }
        for name, path in MANIFESTS.items():
            calibration = json.loads(
                (path.parent / "音准校准.json").read_text(encoding="utf-8")
            )
            audit = json.loads(
                (path.parent / "资源核验.json").read_text(encoding="utf-8")
            )
            self.assertEqual(calibration["summary"]["sample_count"], expected_calibration[name])
            self.assertEqual(len(calibration["samples"]), expected_calibration[name])
            self.assertEqual(audit["sample_count"], expected_assets[name])
            self.assertEqual(len(audit["source_sfz_sha256"]), 2)
            for relative, expected_hash in audit["source_sfz_sha256"].items():
                actual = hashlib.sha256((VPO_ROOT / relative).read_bytes()).hexdigest()
                self.assertEqual(actual, expected_hash)

            instrument = self.create_brass(name)
            calibrated = {
                relative: item["measured_hz"]
                for relative, item in calibration["samples"].items()
            }
            checked = 0
            for layer in instrument.layers:
                for region in layer.engines["sustain_0"].regions:
                    relative = region.path.relative_to(VPO_ROOT).as_posix()
                    self.assertAlmostEqual(
                        region.root_pitch_hz, calibrated[relative], places=5
                    )
                    checked += 1
            self.assertEqual(checked, expected_calibration[name])

    def test_candidate_ranges_written_transposition_and_articulations(self) -> None:
        expected = {
            "圆号": (35, 77, 42, 84, -7),
            "大号": (26, 62, 26, 62, 0),
            "小号": (54, 84, 56, 86, -2),
            "长号": (40, 77, 40, 77, 0),
            "铜管合奏": (26, 84, 26, 84, 0),
        }
        tuning = EqualTemperament()
        for name, values in expected.items():
            minimum, maximum, written_minimum, written_maximum, transposition = values
            manifest = json.loads(MANIFESTS[name].read_text(encoding="utf-8"))
            self.assertEqual(manifest["input_pitch_semantics"], "concert_pitch")
            self.assertEqual(manifest["written_note_min"], written_minimum)
            self.assertEqual(manifest["written_note_max"], written_maximum)
            self.assertEqual(manifest["written_to_concert_semitones"], transposition)
            instrument = self.create_brass(name)
            for articulation in (
                "normal",
                "sustain",
                "slow_sustain",
                "staccato",
                "accent",
            ):
                instrument.handle_event(
                    PerformanceEvent(0, 0, "articulation", {"name": articulation}),
                    tuning,
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
                    PerformanceEvent(0, 2, "articulation", {"name": "flutter"}),
                    tuning,
                )

    def test_expression_breath_modulation_tuning_and_pedal(self) -> None:
        tuning = EqualTemperament()
        instrument = self.create_brass("圆号")
        instrument.handle_event(
            PerformanceEvent(0, 0, "control", {"name": "expression", "value": 0.5}),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(0, 1, "control", {"name": "breath", "value": 0.4}),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(0, 2, "control", {"name": "modulation", "value": 1.0}),
            tuning,
        )
        self.assertAlmostEqual(instrument.expression_target, 0.5**1.25)
        self.assertAlmostEqual(instrument.breath_target, 0.4**1.15)
        instrument.handle_event(
            PerformanceEvent(
                0,
                3,
                "note_on",
                {"note_id": 7, "midi_note": 69, "velocity": 0.76},
            ),
            tuning,
        )
        route = instrument.note_routes[7][0]
        slow_voice = route.engine.voices[route.note_id]
        self.assertGreaterEqual(slow_voice.attack_samples, round(0.5 * 48_000))
        instrument.handle_event(
            PerformanceEvent(1, 4, "control", {"name": "sustain_pedal", "value": 1.0}),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(2, 5, "note_off", {"note_id": 7}), tuning
        )
        self.assertTrue(slow_voice.pending_release)
        self.assertFalse(slow_voice.released)
        instrument.handle_event(
            PerformanceEvent(3, 6, "control", {"name": "sustain_pedal", "value": 0.0}),
            tuning,
        )
        self.assertTrue(slow_voice.released)

        tuned_440 = self.create_brass("圆号")
        tuned_432 = self.create_brass("圆号")
        note = PerformanceEvent(
            0, 0, "note_on", {"note_id": 1, "midi_note": 69, "velocity": 0.7}
        )
        tuned_440.handle_event(note, EqualTemperament(440.0))
        tuned_432.handle_event(note, EqualTemperament(432.0))
        route_440 = tuned_440.note_routes[1][0]
        route_432 = tuned_432.note_routes[1][0]
        increment_440 = route_440.engine.voices[route_440.note_id].increment
        increment_432 = route_432.engine.voices[route_432.note_id].increment
        self.assertAlmostEqual(increment_432 / increment_440, 432.0 / 440.0, places=9)

    def test_all_brass_uses_real_section_crossfades(self) -> None:
        instrument = self.create_brass("铜管合奏")
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 36, "velocity": 0.75},
            ),
            tuning,
        )
        routes = instrument.note_routes[1]
        self.assertEqual(len(routes), 2)  # tuba fades out while horns fade in
        amplitudes = [route.engine.voices[route.note_id].amplitude for route in routes]
        self.assertTrue(all(value > 0.0 for value in amplitudes))
        self.assertNotAlmostEqual(amplitudes[0], amplitudes[1])

    def test_missing_vpo_assets_fail_loudly(self) -> None:
        manifest = json.loads(MANIFESTS["圆号"].read_text(encoding="utf-8"))
        manifest["asset_root"] = "missing-vpo-assets"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "VPO Brass directory does not exist"):
                create_vpo_brass(
                    manifest=manifest,
                    sample_rate=48_000,
                    base_directory=temporary,
                )

    def _render_digest_and_peak(self, name: str) -> tuple[str, float]:
        instrument = self.create_brass(name)
        tuning = EqualTemperament()
        manifest = json.loads(MANIFESTS[name].read_text(encoding="utf-8"))
        note = round((manifest["note_min"] + manifest["note_max"]) / 2)
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
        for frame_index in range(24_000):
            if frame_index == 12_000:
                instrument.handle_event(
                    PerformanceEvent(frame_index, 2, "note_off", {"note_id": 1}),
                    tuning,
                )
            left, right = instrument.render_frame()
            peak = max(peak, abs(left), abs(right))
            digest.update(struct.pack("<ff", left, right))
        return digest.hexdigest(), peak

    @pytest.mark.listening
    def test_render_is_deterministic_audible_and_unclipped(self) -> None:
        for name in MANIFESTS:
            first_hash, first_peak = self._render_digest_and_peak(name)
            gc.collect()
            second_hash, second_peak = self._render_digest_and_peak(name)
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_peak, second_peak)
            self.assertGreater(first_peak, 0.01)
            self.assertLess(first_peak, 1.0)


if __name__ == "__main__":
    unittest.main()
