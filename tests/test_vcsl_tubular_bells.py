from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import unittest

import pytest

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament
from tianlai.vpo_percussion import (
    percussion_source_regions,
    vpo_percussion_regions,
)


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "乐器" / "管弦乐" / "打击乐组" / "管钟"
MANIFEST_PATH = DIRECTORY / "乐器.json"
VCSL_ROOT = ROOT / "音源" / "VCSL"
pytestmark = pytest.mark.external_assets
SFZ_RELATIVE = "Idiophones/Struck Idiophones/Tubular Bells 2.sfz"
EXPECTED_ROOTS = [60, 62, 64, 65, 67, 69, 71, 72, 74, 76, 77]
OFFSET_FIXES = {
    (
        "Idiophones/Struck Idiophones/Tubular Bells 2/"
        "TB_hit_B4_v2_1.wav"
    ): 1026,
    (
        "Idiophones/Struck Idiophones/Tubular Bells 2/"
        "TB_hit_C5_v4_1.wav"
    ): 2727,
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless(VCSL_ROOT.is_dir(), "VCSL is not installed")
class VcslTubularBellsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.report = load_json(DIRECTORY / "资源核验.json")
        cls.pitch = load_json(DIRECTORY / "音准校准.json")
        cls.region_sets = percussion_source_regions(
            VCSL_ROOT,
            cls.manifest["profile"],
        )

    def test_strict_cc0_release_and_every_selected_file_are_hash_locked(self) -> None:
        self.assertEqual(self.manifest["license"], "CC0-1.0")
        self.assertEqual(self.manifest["license_status"], "approved")
        self.assertEqual(self.manifest["upstream_version"], "1.2.2-RC")
        self.assertEqual(
            self.manifest["upstream_commit"],
            "b6e6ac82d22248edee98a0bde185eb9ef6d439ad",
        )
        self.assertEqual(self.report["sample_count"], 22)
        self.assertEqual(self.report["sample_bytes"], 75_496_208)
        self.assertEqual(
            self.report["sample_set_sha256"],
            "f35617d893237a552b722d8471d9f73790cc75a12dcf615a6b80db4dd966a3cc",
        )
        self.assertEqual(
            self.report["source_sfz_sha256"][SFZ_RELATIVE],
            "9ce2237fe3d23921500c6e537bb03b563b2a29dc7f9ba31c6b72bc49631208c6",
        )
        self.assertEqual(
            self.report["evidence_sha256"]["README.md"],
            "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b",
        )
        self.assertEqual(len(self.report["sample_sha256"]), 22)
        for relative, expected in {
            **self.report["source_sfz_sha256"],
            **self.report["evidence_sha256"],
            **self.report["sample_sha256"],
        }.items():
            actual = hashlib.sha256((VCSL_ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)

    def test_mapping_is_11_roots_2_real_velocities_no_rr_or_loop(self) -> None:
        self.assertEqual(set(self.region_sets), {"open", "damped"})
        open_regions = self.region_sets["open"]
        damped_regions = self.region_sets["damped"]
        self.assertEqual(len(open_regions), 22)
        self.assertEqual(len(damped_regions), 22)
        self.assertEqual(
            {row["sample"] for row in open_regions},
            {row["sample"] for row in damped_regions},
        )
        self.assertEqual(
            sorted({int(row["root_midi"]) for row in open_regions}),
            EXPECTED_ROOTS,
        )
        self.assertEqual(
            {
                (row["velocity_min"], row["velocity_max"])
                for row in open_regions
            },
            {
                (0.0, 83.5 / 127.0),
                (83.5 / 127.0, 1.0),
            },
        )
        for note in range(60, 80):
            matches = [
                row
                for row in open_regions
                if row["key_min"] <= note <= row["key_max"]
            ]
            self.assertEqual(len(matches), 2, note)
            self.assertEqual(
                {(row["velocity_min"], row["velocity_max"]) for row in matches},
                {
                    (0.0, 83.5 / 127.0),
                    (83.5 / 127.0, 1.0),
                },
            )
        maximum_stretch = max(
            max(
                abs(row["key_min"] - row["root_midi"]),
                abs(row["key_max"] - row["root_midi"]),
            )
            for row in open_regions
        )
        self.assertEqual(maximum_stretch, 2.0)
        self.assertTrue(
            all("round_robin_length" not in row for row in open_regions)
        )
        self.assertTrue(
            all(not row.get("use_embedded_loop", False) for row in open_regions)
        )
        self.assertTrue(
            all(math.isclose(row["stereo_width"], 1.0) for row in open_regions)
        )
        self.assertTrue(
            all(math.isclose(row["release_seconds"], 30.0) for row in open_regions)
        )
        self.assertTrue(
            all(math.isclose(row["release_seconds"], 0.12) for row in damped_regions)
        )

    def test_harmful_upstream_offsets_are_overridden_without_editing_sfz(self) -> None:
        raw = vpo_percussion_regions(VCSL_ROOT / SFZ_RELATIVE)
        raw_offsets = {
            Path(row["sample"]).relative_to(VCSL_ROOT).as_posix(): row[
                "offset_frames"
            ]
            for row in raw
            if row["offset_frames"]
        }
        self.assertEqual(raw_offsets, OFFSET_FIXES)
        for articulation in ("open", "damped"):
            corrected = {
                Path(row["sample"]).relative_to(VCSL_ROOT).as_posix(): row[
                    "offset_frames"
                ]
                for row in self.region_sets[articulation]
            }
            self.assertEqual(
                {path: corrected[path] for path in OFFSET_FIXES},
                {path: 0 for path in OFFSET_FIXES},
            )

    def test_source_audio_is_stereo_audible_unclipped_and_gain_has_6db_margin(
        self,
    ) -> None:
        import soundfile as sf

        peaks: dict[Path, float] = {}
        for relative in self.report["sample_sha256"]:
            path = VCSL_ROOT / relative
            info = sf.info(path)
            self.assertEqual(info.samplerate, 44_100)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_16")
            audio, _sample_rate = sf.read(path, dtype="float32", always_2d=True)
            peak = float(abs(audio).max())
            self.assertGreater(peak, 1e-6, relative)
            self.assertLess(peak, 1.0, relative)
            peaks[path.resolve()] = peak

        maximum = max(
            peaks[Path(row["sample"]).resolve()]
            * (10.0 ** (float(row["gain_db"]) / 20.0))
            * float(self.manifest["gain"])
            for row in self.region_sets["open"]
        )
        headroom_db = -20.0 * math.log10(maximum)
        self.assertGreaterEqual(headroom_db, 6.0)
        self.assertAlmostEqual(
            headroom_db,
            self.report["audio_integrity"]["minimum_headroom_db"],
            places=5,
        )

    def test_open_is_natural_and_damped_is_only_project_envelope(self) -> None:
        instrument = create_instrument(
            self.manifest,
            48_000,
            base_directory=str(DIRECTORY),
        )
        self.assertEqual(len(instrument.engines["open"].regions), 22)
        self.assertEqual(len(instrument.engines["damped"].regions), 22)
        self.assertTrue(
            all(region.loop_start is None for region in instrument.engines["open"].regions)
        )
        self.assertTrue(
            all(
                math.isclose(region.stereo_width, 1.0)
                for region in instrument.engines["open"].regions
            )
        )
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "damped"}),
            EqualTemperament(),
        )
        instrument.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 1, "midi_note": 60, "velocity": 0.8},
            ),
            EqualTemperament(),
        )
        route = instrument.routes[1]
        voice = instrument.engines["damped"].voices[route.internal_note_id]
        instrument.handle_event(
            PerformanceEvent(
                1,
                2,
                "control",
                {"name": "sustain_pedal", "value": 1.0},
            ),
            EqualTemperament(),
        )
        instrument.handle_event(
            PerformanceEvent(2, 3, "note_off", {"note_id": 1}),
            EqualTemperament(),
        )
        self.assertTrue(voice.pending_release)
        instrument.handle_event(
            PerformanceEvent(
                3,
                4,
                "control",
                {"name": "sustain_pedal", "value": 0.0},
            ),
            EqualTemperament(),
        )
        self.assertTrue(voice.released)
        self.assertEqual(voice.release_samples, round(0.12 * 48_000))

    def test_pitch_report_does_not_turn_an_inharmonic_peak_into_fake_tuning(
        self,
    ) -> None:
        summary = self.pitch["summary"]
        self.assertEqual(summary["sample_count"], 22)
        self.assertEqual(summary["unique_root_count"], 11)
        self.assertEqual(summary["root_midi_notes"], [float(x) for x in EXPECTED_ROOTS])
        self.assertEqual(summary["recorded_velocity_layer_count"], 2)
        self.assertEqual(summary["round_robin_count"], 0)
        self.assertEqual(summary["automatic_correction_count"], 0)
        self.assertEqual(summary["human_spectral_review"], "pending")
        self.assertTrue(
            all(
                item["automatic_cents_correction"] is None
                for item in self.pitch["samples"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
