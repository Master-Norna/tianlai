from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import unittest

import pytest

from tianlai.dedicated_candidates import dedicated_manifest_sources
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "乐器" / "管弦乐" / "打击乐组" / "颤音琴"
MANIFEST_PATH = DIRECTORY / "乐器.json"
VCSL_ROOT = ROOT / "音源" / "VCSL"
pytestmark = pytest.mark.external_assets
SOFT_SFZ = "Idiophones/Struck Idiophones/Vibraphone - Soft Mallets.sfz"
HARD_SFZ = "Idiophones/Struck Idiophones/Vibraphone - Hard Mallets.sfz"
BOWED_SFZ = "Idiophones/Struck Idiophones/Vibraphone - Bowed.sfz"
MALLET_ROOTS = [53.0, 57.0, 60.0, 64.0, 67.0, 71.0, 74.0, 77.0, 81.0, 84.0, 88.0]
BOWED_ROOTS = [57.0, 64.0, 67.0, 74.0, 81.0, 88.0]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless(VCSL_ROOT.is_dir(), "VCSL is not installed")
class VcslVibraphoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.resource = load_json(DIRECTORY / "资源核验.json")
        cls.pitch = load_json(DIRECTORY / "音准校准.json")
        cls.inventory = dedicated_manifest_sources(MANIFEST_PATH)

    def create_vibraphone(self):
        return create_instrument(
            self.manifest,
            48_000,
            base_directory=str(DIRECTORY),
        )

    def test_release_is_strict_cc0_and_all_selected_assets_are_hash_locked(self) -> None:
        self.assertEqual(self.manifest["type"], "dedicated_sfz")
        self.assertEqual(self.manifest["license"], "CC0-1.0")
        self.assertEqual(self.manifest["license_status"], "approved")
        self.assertEqual(self.manifest["upstream_version"], "1.2.2-RC")
        self.assertEqual(
            self.manifest["upstream_commit"],
            "b6e6ac82d22248edee98a0bde185eb9ef6d439ad",
        )
        self.assertEqual(self.manifest["fallback_policy"], "explicit_only_no_silent_gm")
        self.assertEqual(self.resource["sample_count"], 50)
        self.assertEqual(self.resource["sample_bytes"], 99_488_896)
        self.assertEqual(
            self.resource["sample_set_sha256"],
            "9f51a385e6dd5d660c59ef1f2cfc7b636ff9953b6a7ab4daf371287ffc20777a",
        )
        self.assertEqual(
            self.resource["source_file_sha256"],
            {
                BOWED_SFZ: "304e09d434b181d9f1b48648cee246dc44568f36a92b81e7715098858c1f5245",
                HARD_SFZ: "75413685fe26f2245c62932336e20a3b254bcb28d409a7f0ff37de2030087a1f",
                SOFT_SFZ: "92ff34fd739d14df29580f33421c54244121dc347e21d4263e58f3ce72c9b605",
            },
        )
        self.assertEqual(
            self.resource["evidence_sha256"]["README.md"],
            "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b",
        )
        for relative, expected in {
            **self.resource["source_file_sha256"],
            **self.resource["evidence_sha256"],
        }.items():
            self.assertEqual(
                hashlib.sha256((VCSL_ROOT / relative).read_bytes()).hexdigest(),
                expected,
                relative,
            )

        sample_paths = sorted(
            {
                Path(region["sample"])
                for data in self.inventory["articulations"].values()
                for region in (
                    *data["attack_regions"],
                    *data["release_regions"],
                )
            },
            key=lambda path: path.relative_to(VCSL_ROOT).as_posix(),
        )
        self.assertEqual(len(sample_paths), 50)
        locked_lines = [
            (
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                f"{path.relative_to(VCSL_ROOT).as_posix()}\n"
            )
            for path in sample_paths
        ]
        self.assertEqual(
            hashlib.sha256("".join(locked_lines).encode("utf-8")).hexdigest(),
            self.resource["sample_set_sha256"],
        )

    def test_mapping_has_real_velocity_layers_and_no_invented_round_robin(self) -> None:
        mappings = self.inventory["articulations"]
        self.assertEqual(
            set(mappings),
            {"damped", "open", "hard_damped", "hard_open", "bowed"},
        )
        expected = {
            "damped": (22, MALLET_ROOTS),
            "open": (22, MALLET_ROOTS),
            "hard_damped": (22, MALLET_ROOTS),
            "hard_open": (22, MALLET_ROOTS),
            "bowed": (6, BOWED_ROOTS),
        }
        for name, (count, roots) in expected.items():
            with self.subTest(articulation=name):
                regions = mappings[name]["attack_regions"]
                self.assertEqual(len(regions), count)
                self.assertEqual(
                    sorted({float(region["root_midi"]) for region in regions}),
                    roots,
                )
                self.assertTrue(
                    all("round_robin_length" not in region for region in regions)
                )
                self.assertTrue(
                    all(not region["_dedicated_has_random_range"] for region in regions)
                )
                self.assertEqual(mappings[name]["release_regions"], [])

        for name in ("damped", "open", "hard_damped", "hard_open"):
            regions = mappings[name]["attack_regions"]
            self.assertEqual(
                {
                    (
                        round(float(region["velocity_min"]) * 127),
                        round(float(region["velocity_max"]) * 127),
                    )
                    for region in regions
                },
                {(0, 83), (84, 127)},
            )
            for note in range(53, 90):
                matches = [
                    region
                    for region in regions
                    if region["key_min"] <= note <= region["key_max"]
                ]
                self.assertEqual(len(matches), 2, (name, note))

        self.assertEqual(
            {
                Path(region["sample"]).relative_to(VCSL_ROOT).as_posix()
                for region in mappings["damped"]["attack_regions"]
            },
            {
                Path(region["sample"]).relative_to(VCSL_ROOT).as_posix()
                for region in mappings["open"]["attack_regions"]
            },
        )
        self.assertEqual(
            {
                Path(region["sample"]).relative_to(VCSL_ROOT).as_posix()
                for region in mappings["hard_damped"]["attack_regions"]
            },
            {
                Path(region["sample"]).relative_to(VCSL_ROOT).as_posix()
                for region in mappings["hard_open"]["attack_regions"]
            },
        )

    def test_sources_are_stereo_pcm16_audible_unclipped_and_keep_headroom(self) -> None:
        import soundfile as sf

        attack_regions = [
            region
            for data in self.inventory["articulations"].values()
            for region in data["attack_regions"]
        ]
        sample_paths = sorted({Path(region["sample"]) for region in attack_regions})
        peaks: dict[Path, float] = {}
        maximum_offset_ratio = 0.0
        for path in sample_paths:
            relative = path.relative_to(VCSL_ROOT).as_posix()
            info = sf.info(path)
            self.assertEqual(info.samplerate, 44_100, relative)
            self.assertEqual(info.channels, 2, relative)
            self.assertEqual(info.subtype, "PCM_16", relative)
            peak = 0.0
            for block in sf.blocks(
                path,
                blocksize=65_536,
                dtype="float32",
                always_2d=True,
            ):
                peak = max(peak, float(abs(block).max()))
            self.assertGreater(peak, 1e-6, relative)
            self.assertLess(peak, 1.0, relative)
            peaks[path.resolve()] = peak

        effective_maximum = 0.0
        for region in attack_regions:
            path = Path(region["sample"]).resolve()
            info = sf.info(path)
            maximum_offset_ratio = max(
                maximum_offset_ratio,
                float(region.get("offset_frames", 0)) / info.frames,
            )
            effective_maximum = max(
                effective_maximum,
                peaks[path]
                * (10.0 ** (float(region["gain_db"]) / 20.0))
                * float(self.manifest["gain"]),
            )
        self.assertLess(maximum_offset_ratio, 0.05)
        self.assertGreaterEqual(-20.0 * math.log10(effective_maximum), 6.0)

    def test_runtime_applies_every_measured_pitch_and_articulation_range(self) -> None:
        instrument = self.create_vibraphone()
        self.assertTrue(self.manifest["apply_pitch_calibration"])
        self.assertTrue(self.pitch["applicable"])
        self.assertTrue(self.pitch["applied_to_runtime"])
        self.assertEqual(self.pitch["summary"]["sample_count"], 50)
        self.assertLessEqual(
            self.pitch["summary"]["maximum_absolute_measured_detune_cents"],
            5.0,
        )
        self.assertEqual(
            instrument.articulation_playable_ranges,
            {
                "damped": ((53.0, 89.0),),
                "open": ((53.0, 89.0),),
                "hard_damped": ((53.0, 89.0),),
                "hard_open": ((53.0, 89.0),),
                "bowed": ((57.0, 89.0),),
            },
        )

        seen: set[str] = set()
        for runtime in instrument.articulations.values():
            for layer in runtime.attack_layers:
                for region in layer.engine.regions:
                    relative = region.path.relative_to(VCSL_ROOT).as_posix()
                    calibration = self.pitch["samples"][relative]
                    expected_hz = 440.0 * 2.0 ** (
                        (
                            float(calibration["root_midi"])
                            - 69.0
                            + float(calibration["measured_detune_cents"]) / 100.0
                        )
                        / 12.0
                    )
                    self.assertAlmostEqual(region.root_pitch_hz, expected_hz, places=6)
                    seen.add(relative)
        self.assertEqual(seen, set(self.pitch["samples"]))

    def test_project_damping_is_explicit_and_each_articulation_is_audible(self) -> None:
        instrument = self.create_vibraphone()
        self.assertEqual(
            {
                name: runtime.release_override_seconds
                for name, runtime in instrument.articulations.items()
            },
            {
                "damped": 0.35,
                "open": None,
                "hard_damped": 0.35,
                "hard_open": None,
                "bowed": None,
            },
        )
        tuning = EqualTemperament()
        peaks: dict[str, float] = {}
        notes = {
            "damped": (69, 0.35),
            "open": (69, 0.9),
            "hard_damped": (77, 0.35),
            "hard_open": (77, 0.9),
            "bowed": (74, 0.7),
        }
        for sequence, (name, (note, velocity)) in enumerate(notes.items(), 1):
            instrument.handle_event(
                PerformanceEvent(0, sequence * 2, "articulation", {"name": name}),
                tuning,
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    sequence * 2 + 1,
                    "note_on",
                    {
                        "note_id": sequence,
                        "midi_note": note,
                        "velocity": velocity,
                    },
                ),
                tuning,
            )
            peak = 0.0
            for _ in range(48_000):
                left, right = instrument.render_frame()
                peak = max(peak, abs(left), abs(right))
            peaks[name] = peak
            for runtime in instrument.articulations.values():
                for layer in (*runtime.attack_layers, *runtime.release_layers):
                    layer.engine.voices.clear()
            instrument.routes.clear()

        for name, peak in peaks.items():
            self.assertGreater(peak, 1e-5, name)
            self.assertLess(peak, 1.0, name)

        instrument.handle_event(
            PerformanceEvent(0, 100, "articulation", {"name": "damped"}),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(
                0,
                101,
                "note_on",
                {"note_id": 100, "midi_note": 69, "velocity": 0.8},
            ),
            tuning,
        )
        route = instrument.routes[100]
        routed_voice = route.voices[0]
        voice = instrument.articulations["damped"].attack_layers[
            routed_voice.layer_index
        ].engine.voices[routed_voice.internal_note_id]
        instrument.handle_event(
            PerformanceEvent(1, 102, "note_off", {"note_id": 100}),
            tuning,
        )
        self.assertEqual(voice.release_samples, round(0.35 * 48_000))


if __name__ == "__main__":
    unittest.main()
