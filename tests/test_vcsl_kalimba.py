from __future__ import annotations

import hashlib
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

import pytest

from tianlai.audio import wav_loop_points
from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.dedicated_candidates import dedicated_manifest_sources
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "乐器" / "世界乐器" / "卡林巴"
MANIFEST_PATH = DIRECTORY / "乐器.json"
VCSL_ROOT = ROOT / "音源" / "VCSL"
pytestmark = pytest.mark.external_assets
SFZ_RELATIVE = "Idiophones/Plucked Idiophones/Kalimba, Kenya.sfz"
D_SHARP_K13 = (
    "Idiophones/Plucked Idiophones/Kalimba, Kenya/"
    "Mbira6_Normal_MainSpirit_D#4_k13_vl3_rr2.wav"
)
B_K15 = (
    "Idiophones/Plucked Idiophones/Kalimba, Kenya/"
    "Mbira6_Normal_MainSpirit_B4_k15_vl3_rr2.wav"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_analysis_module():
    path = DIRECTORY / "kalimba_analysis.py"
    spec = importlib.util.spec_from_file_location(
        "tianlai_test_kalimba_analysis",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(VCSL_ROOT.is_dir(), "VCSL is not installed")
class VcslKalimbaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.resource = load_json(DIRECTORY / "资源核验.json")
        cls.pitch = load_json(DIRECTORY / "音准校准.json")
        cls.inventory = dedicated_manifest_sources(MANIFEST_PATH)
        cls.regions = cls.inventory["articulations"]["normal"]["attack_regions"]

    def test_strict_cc0_source_and_every_sample_are_hash_locked(self) -> None:
        self.assertEqual(self.manifest["license"], "CC0-1.0")
        self.assertEqual(self.manifest["license_status"], "approved")
        self.assertEqual(self.manifest["upstream_version"], "1.2.2-RC")
        self.assertEqual(
            self.manifest["upstream_commit"],
            "b6e6ac82d22248edee98a0bde185eb9ef6d439ad",
        )
        self.assertEqual(self.resource["sample_count"], 15)
        self.assertEqual(self.resource["sample_bytes"], 22_303_536)
        self.assertEqual(
            self.resource["sample_set_sha256"],
            "6ea996c51ec01f751e5a971517266ba1a3cdf148ce5bb7411030e71a88049f5d",
        )
        self.assertEqual(
            self.resource["source_file_sha256"][SFZ_RELATIVE],
            "29fb4d6e1a02e05170a6bb921510d1862e5727cb903a3f24fd866b2c320dc4a9",
        )
        self.assertEqual(
            self.resource["evidence_sha256"]["README.md"],
            "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b",
        )
        self.assertEqual(len(self.resource["sample_sha256"]), 15)
        for relative, expected in {
            **self.resource["source_file_sha256"],
            **self.resource["evidence_sha256"],
            **self.resource["sample_sha256"],
        }.items():
            actual = hashlib.sha256((VCSL_ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)

        aggregate = "".join(
            f"{digest}  {relative}\n"
            for relative, digest in sorted(self.resource["sample_sha256"].items())
        )
        self.assertEqual(
            hashlib.sha256(aggregate.encode("utf-8")).hexdigest(),
            self.resource["sample_set_sha256"],
        )

    def test_mapping_is_15_physical_tines_11_roots_and_four_real_rr2_families(
        self,
    ) -> None:
        self.assertEqual(len(self.regions), 15)
        self.assertEqual(
            sorted({int(region["root_midi"]) for region in self.regions}),
            [59, 61, 63, 66, 68, 71, 73, 75, 78, 81, 83],
        )
        rr_regions = [
            region for region in self.regions if "round_robin_length" in region
        ]
        self.assertEqual(len(rr_regions), 8)
        families = {
            (
                int(region["root_midi"]),
                int(region["key_min"]),
                int(region["key_max"]),
            )
            for region in rr_regions
        }
        self.assertEqual(len(families), 4)
        for family in families:
            matching = [
                region
                for region in rr_regions
                if (
                    int(region["root_midi"]),
                    int(region["key_min"]),
                    int(region["key_max"]),
                )
                == family
            ]
            self.assertEqual(
                {region["round_robin_position"] for region in matching},
                {1, 2},
            )
            self.assertEqual(
                {region["round_robin_length"] for region in matching},
                {2},
            )

        for note in range(59, 85):
            matches = [
                region
                for region in self.regions
                if region["key_min"] <= note <= region["key_max"]
            ]
            self.assertIn(len(matches), (1, 2), note)
            if len(matches) == 2:
                self.assertEqual(
                    {region["round_robin_position"] for region in matches},
                    {1, 2},
                )
        maximum_stretch = max(
            max(
                abs(region["key_min"] - region["root_midi"]),
                abs(region["key_max"] - region["root_midi"]),
            )
            for region in self.regions
        )
        self.assertEqual(maximum_stretch, 1.0)
        self.assertEqual(
            self.resource["mapping"]["ambiguous_or_missing_integer_notes"],
            0,
        )

    def test_audio_is_stereo_unlooped_unclipped_and_has_6db_headroom(self) -> None:
        import soundfile as sf

        peaks: dict[Path, float] = {}
        decoded_float32_stereo_bytes = 0
        for relative in self.resource["sample_sha256"]:
            path = (VCSL_ROOT / relative).resolve()
            info = sf.info(path)
            self.assertEqual(info.samplerate, 48_000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_24")
            decoded_float32_stereo_bytes += int(info.frames) * 2 * 4
            self.assertIsNone(wav_loop_points(path), relative)
            audio, _sample_rate = sf.read(path, dtype="float32", always_2d=True)
            peak = float(abs(audio).max())
            self.assertGreater(peak, 1e-6, relative)
            self.assertLess(peak, 1.0, relative)
            peaks[path] = peak

        self.assertEqual(
            self.resource["decoded_float32_stereo_bytes"],
            decoded_float32_stereo_bytes,
        )
        self.assertEqual(
            self.resource["decoded_float32_stereo_algorithm"],
            "sum unique runtime sample frame_count * 2 output channels * "
            "4-byte float32; mono sources are expanded to stereo by "
            "read_audio_float",
        )

        maximum = max(
            peaks[Path(region["sample"]).resolve()]
            * (10.0 ** (float(region["gain_db"]) / 20.0))
            * float(self.manifest["gain"])
            for region in self.regions
        )
        headroom_db = -20.0 * math.log10(maximum)
        self.assertGreaterEqual(headroom_db, 6.0)
        self.assertAlmostEqual(
            headroom_db,
            self.resource["audio_integrity"]["minimum_headroom_db"],
            places=5,
        )
        self.assertEqual(
            self.resource["audio_integrity"]["source_clipped_samples"],
            0,
        )
        self.assertEqual(self.resource["audio_integrity"]["silent_samples"], 0)
        self.assertEqual(self.resource["mapping"]["embedded_loop_count"], 0)

    def test_disputed_d_sharp_is_a_secondary_mode_false_positive(self) -> None:
        accepted = self.pitch["samples"][D_SHARP_K13]
        legacy = self.pitch["legacy_false_positive_diagnostics"][D_SHARP_K13]
        self.assertAlmostEqual(
            legacy["legacy_measured_detune_cents"],
            161.006267,
            places=5,
        )
        self.assertAlmostEqual(
            accepted["measured_detune_cents"],
            27.159654,
            places=5,
        )
        self.assertLess(
            abs(accepted["onset_label_band_detune_cents"]),
            3.0,
        )
        self.assertEqual(
            accepted["classification"],
            "long_lived_labelled_tine_mode",
        )
        self.assertIsNone(accepted["automatic_pitch_override_cents"])
        self.assertIn("secondary-mode false positive", self.pitch[
            "disputed_recording_findings"
        ]["D#4_k13"])

    def test_disputed_top_b_has_a_valid_b5_onset_and_real_lower_b_resonance(
        self,
    ) -> None:
        accepted = self.pitch["samples"][B_K15]
        legacy = self.pitch["legacy_false_positive_diagnostics"][B_K15]
        self.assertAlmostEqual(
            legacy["legacy_measured_detune_cents"],
            -116.468696,
            places=5,
        )
        self.assertAlmostEqual(
            accepted["measured_detune_cents"],
            -3.453143,
            places=5,
        )
        self.assertAlmostEqual(
            accepted["long_dominant_detune_cents"],
            -1180.471352,
            places=5,
        )
        self.assertEqual(
            accepted["classification"],
            "onset_tine_with_octave_lower_sympathetic_resonance",
        )
        self.assertLess(
            abs(
                self.pitch["disputed_recording_findings"][
                    "B4_k15_late_mode_vs_lower_B_tines_cents"
                ]
            ),
            2.0,
        )
        self.assertIsNone(accepted["automatic_pitch_override_cents"])

    def test_modal_report_is_reproducible_and_applies_no_pitch_override(self) -> None:
        module = load_analysis_module()
        with tempfile.TemporaryDirectory() as temporary:
            generated_pitch = module.generate_kalimba_pitch_calibration(
                MANIFEST_PATH,
                Path(temporary) / "pitch.json",
            )
            generated_resource = module.generate_kalimba_resource_verification(
                MANIFEST_PATH,
                Path(temporary) / "resource.json",
            )
        self.assertEqual(generated_pitch, self.pitch)
        self.assertEqual(generated_resource, self.resource)
        self.assertEqual(
            self.pitch["summary"]["maximum_absolute_residual_cents"],
            27.159654,
        )
        self.assertEqual(self.pitch["summary"]["residuals_above_50_cents"], 0)
        self.assertEqual(
            self.pitch["summary"]["automatic_pitch_override_count"],
            0,
        )
        self.assertFalse(self.manifest["apply_pitch_calibration"])
        self.assertEqual(
            self.resource["project_policy"]["average_temperament_corrections"],
            0,
        )

    def test_runtime_preserves_upstream_roots_and_rr_is_deterministic(self) -> None:
        instrument = create_instrument(
            self.manifest,
            48_000,
            base_directory=str(DIRECTORY),
        )
        engine = instrument.articulations["normal"].attack
        by_name = {region.path.name: region for region in engine.regions}
        for filename, midi_note in (
            ("Mbira6_Normal_MainSpirit_D#4_k13_vl3_rr2.wav", 75),
            ("Mbira6_Normal_MainSpirit_B4_k15_vl3_rr2.wav", 83),
        ):
            region = by_name[filename]
            self.assertAlmostEqual(
                region.root_pitch_hz,
                EqualTemperament().note_to_hz(midi_note),
                places=9,
            )

        tuning = EqualTemperament()
        selected: list[str] = []
        for index in range(2):
            note_id = index + 1
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    index,
                    "note_on",
                    {
                        "note_id": note_id,
                        "midi_note": 63,
                        "velocity": 0.75,
                    },
                ),
                tuning,
            )
            internal_id = instrument.routes[note_id].internal_note_id
            selected.append(engine.voices[internal_id].region.path.name)
        self.assertNotEqual(selected[0], selected[1])
        self.assertEqual(
            set(selected),
            {
                "Mbira6_Normal_MainSpirit_D#3_k6_vl3_rr2.wav",
                "Mbira6_Normal_MainSpirit_D#3_k9_vl3_rr2.wav",
            },
        )

    @pytest.mark.listening
    def test_focused_audition_is_hash_locked_audible_and_unclipped(self) -> None:
        report = load_json(DIRECTORY / "试听核验.json")
        self.assertEqual(report["status"], "machine_pass_human_pending")
        self.assertEqual(report["audition_profile"], "ascending-scale")
        self.assertEqual(report["sample_rate"], 48_000)
        self.assertEqual(report["channels"], 2)
        self.assertEqual(report["subtype"], "PCM_24")
        self.assertEqual(report["duration_seconds"], 17.23)
        self.assertEqual(report["clipped_samples"], 0)
        self.assertGreater(report["peak"], 0.01)
        self.assertLess(report["peak"], 1.0)
        self.assertGreater(report["rms"], 0.001)
        self.assertIn("MIDI 59-84", " ".join(report["coverage"]))
        wav_path = ROOT / report["wav"]
        self.assertEqual(report["wav_persistence"], "temporary")
        self.assertRegex(report["wav_sha256"], r"^[0-9a-f]{64}$")
        if wav_path.is_file():
            self.assertEqual(
                hashlib.sha256(wav_path.read_bytes()).hexdigest(),
                report["wav_sha256"],
            )
        self.assertEqual(
            report["hash_algorithm"],
            HASH_ALGORITHM,
        )
        self.assertEqual(
            report["canonicalization"],
            CANONICALIZATION,
        )
        self.assertEqual(
            canonical_json_file_sha256(MANIFEST_PATH),
            report["manifest_canonical_sha256"],
        )
        events_path = ROOT / report["events"]
        self.assertEqual(
            canonical_json_file_sha256(events_path),
            report["events_canonical_sha256"],
        )
        events = load_json(events_path)
        self.assertEqual(
            [
                event["midi_note"]
                for event in events["events"]
                if event["type"] == "note_on"
            ],
            list(range(59, 85)),
        )

    def test_internal_evidence_preserves_resonance_without_pitch_override(
        self,
    ) -> None:
        summary = self.pitch["summary"]
        policy = self.resource["project_policy"]
        finding = self.pitch["disputed_recording_findings"]["B4_k15"]

        self.assertEqual(summary["octave_sympathetic_recording_count"], 1)
        self.assertEqual(summary["automatic_pitch_override_count"], 0)
        self.assertTrue(policy["upstream_sfz_unchanged"])
        self.assertEqual(policy["pitch_root_overrides"], 0)
        self.assertEqual(policy["average_temperament_corrections"], 0)
        self.assertIn("not a root remap", finding)

        readme = (DIRECTORY / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("更像是上游把音片映射到了错误的音名", readme)


if __name__ == "__main__":
    unittest.main()
