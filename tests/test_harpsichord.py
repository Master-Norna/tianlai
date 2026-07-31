from __future__ import annotations

import hashlib
import json
from pathlib import Path
import runpy
import tempfile
import unittest

import pytest

from tianlai.analysis import analyze_instrument_pitch
from tianlai.dedicated_candidates import dedicated_manifest_sources


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "乐器" / "键盘乐器" / "羽管键琴"
MANIFEST = DIRECTORY / "乐器.json"
CALIBRATION_SCRIPT = DIRECTORY / "校准音准.py"
pytestmark = pytest.mark.external_assets


def load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"JSON root is not an object: {path}")
    return document


def matching(regions: list[dict[str, object]], note: int) -> list[dict[str, object]]:
    return [
        region
        for region in regions
        if float(region["key_min"]) <= note <= float(region["key_max"])
    ]


class HarpsichordAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST)
        asset_root = (
            MANIFEST.parent / str(cls.manifest["asset_root"])
        ).resolve()
        if not asset_root.exists():
            raise unittest.SkipTest(f"VCSL is not installed: {asset_root}")
        required = [
            asset_root / str(relative)
            for relative in cls.manifest["articulations"].values()
        ]
        missing = [path for path in required if not path.is_file()]
        if missing:
            raise AssertionError(
                "VCSL harpsichord resource is partially installed; "
                f"missing: {', '.join(str(path) for path in missing)}"
            )
        cls.inventory = dedicated_manifest_sources(MANIFEST)

    def test_release_tag_cc0_and_frozen_hashes_are_consistent(self) -> None:
        self.assertEqual(self.manifest["upstream_version"], "1.2.2-RC")
        self.assertEqual(self.manifest["license"], "CC0-1.0")
        report = load_json(DIRECTORY / "资源核验.json")
        self.assertEqual(report["upstream_version"], self.manifest["upstream_version"])
        self.assertEqual(report["license"], self.manifest["license"])
        self.assertEqual(report["sample_count"], 108)
        self.assertEqual(report["region_count"], 216)
        self.assertEqual(
            report["sample_set_sha256"],
            "beb602e35200655599162843aa3a62c2f3ff328c78a21fabdfc1bb403dce9c6f",
        )

        asset_root = Path(self.inventory["asset_root"])
        for relative, expected in report["source_file_sha256"].items():
            actual = hashlib.sha256((asset_root / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)
        for relative, expected in report["evidence_sha256"].items():
            actual = hashlib.sha256((asset_root / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)

    def test_all_three_stops_cover_every_keyboard_key_with_correct_registers(
        self,
    ) -> None:
        self.assertEqual(self.manifest["default_articulation"], "full")
        self.assertEqual(
            self.manifest["calibration_articulation"],
            "eight_foot",
        )
        articulations = self.inventory["articulations"]
        expected_counts = {
            "full": (54, 54, 2),
            "eight_foot": (28, 28, 1),
            "four_foot": (26, 26, 1),
        }
        for name, (attack_count, release_count, per_key) in expected_counts.items():
            data = articulations[name]
            self.assertEqual(len(data["attack_regions"]), attack_count)
            self.assertEqual(len(data["release_regions"]), release_count)
            for note in range(29, 85):
                with self.subTest(articulation=name, midi_note=note):
                    attacks = matching(data["attack_regions"], note)
                    releases = matching(data["release_regions"], note)
                    self.assertEqual(len(attacks), per_key)
                    self.assertEqual(len(releases), per_key)
                    attack_paths = {Path(item["sample"]).as_posix() for item in attacks}
                    release_paths = {Path(item["sample"]).as_posix() for item in releases}
                    if name == "eight_foot":
                        self.assertTrue(all("/Sustains/Low/" in p for p in attack_paths))
                        self.assertTrue(all("/Releases/Low/" in p for p in release_paths))
                    elif name == "four_foot":
                        self.assertTrue(all("/Sustains/High/" in p for p in attack_paths))
                        self.assertTrue(all("/Releases/High/" in p for p in release_paths))
                    else:
                        self.assertEqual(
                            {"/Sustains/Low/" in p for p in attack_paths},
                            {False, True},
                        )
                        self.assertEqual(
                            {"/Releases/Low/" in p for p in release_paths},
                            {False, True},
                        )

    def test_register_aware_calibration_is_reproducible(self) -> None:
        namespace = runpy.run_path(str(CALIBRATION_SCRIPT))
        generate = namespace["generate_harpsichord_pitch_calibration"]
        with tempfile.TemporaryDirectory() as temporary:
            regenerated = generate(
                MANIFEST,
                output_path=Path(temporary) / "pitch.json",
            )
        frozen = load_json(DIRECTORY / "音准校准.json")
        self.assertEqual(regenerated, frozen)

        summary = frozen["summary"]
        self.assertEqual(summary["sample_count"], 54)
        self.assertLessEqual(summary["maximum_absolute_residual_cents"], 9.0)
        registers = frozen["registers"]
        self.assertEqual(registers["eight_foot"]["sample_count"], 28)
        self.assertEqual(registers["four_foot"]["sample_count"], 26)
        self.assertEqual(
            registers["four_foot"]["intentional_register_offset_cents"],
            1200.0,
        )
        high_samples = [
            item
            for item in frozen["samples"].values()
            if item["register"] == "four_foot"
        ]
        self.assertTrue(high_samples)
        self.assertTrue(
            all(
                item["sounding_root_midi"] == item["root_midi"] + 12.0
                for item in high_samples
            )
        )

    def test_low_middle_high_end_to_end_pitch_respects_stop_register(self) -> None:
        probes = (29, 56, 84)
        for articulation in ("full", "eight_foot", "four_foot"):
            expected_offset = 1200.0 if articulation == "four_foot" else 0.0
            for midi_note in probes:
                with self.subTest(
                    articulation=articulation,
                    midi_note=midi_note,
                ):
                    result = analyze_instrument_pitch(
                        MANIFEST,
                        midi_note,
                        articulation=articulation,
                        sample_rate=24_000,
                        duration_seconds=1.8,
                        maximum_frames=32_768,
                    )
                    self.assertTrue(result.clear_pitch, result)
                    self.assertIsNotNone(result.detune_cents)
                    assert result.detune_cents is not None
                    self.assertLessEqual(
                        abs(result.detune_cents - expected_offset),
                        15.0,
                        result,
                    )
                    self.assertEqual(
                        result.nearest_octave_error,
                        1 if articulation == "four_foot" else 0,
                        result,
                    )

    def test_no_loop_is_claimed_without_real_wav_or_sfz_boundaries(self) -> None:
        report = load_json(DIRECTORY / "资源核验.json")
        for name in ("full", "eight_foot", "four_foot"):
            self.assertEqual(report["articulations"][name]["looped_regions"], 0)


if __name__ == "__main__":
    unittest.main()
