"""Portable structural contracts for the dedicated Salamander piano.

These tests deliberately do not decode any Salamander samples.  A temporary
``Samples`` directory satisfies the piano's installation guard while a mocked
``SampleInstrument.from_manifest`` records the six child manifests.  This
keeps the release gate able to catch mapping/configuration regressions on a
clean source checkout where the CC-BY sample library is not installed.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PIANO_DIRECTORY = ROOT / "乐器" / "键盘乐器" / "钢琴"
IMPLEMENTATION_PATH = PIANO_DIRECTORY / "乐器.py"
EXPRESSIVE_AUDITION_SCRIPT = PIANO_DIRECTORY / "核验试听.py"

SPEC = importlib.util.spec_from_file_location(
    "tianlai_test_piano_contract",
    IMPLEMENTATION_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import piano implementation: {IMPLEMENTATION_PATH}")
PIANO_MODULE = importlib.util.module_from_spec(SPEC)
# dataclasses resolves postponed annotations through ``sys.modules`` while
# executing the module, so the dynamically loaded module must be registered.
sys.modules[SPEC.name] = PIANO_MODULE
SPEC.loader.exec_module(PIANO_MODULE)


def _sample_names(manifest: dict) -> tuple[str, ...]:
    return tuple(Path(region["sample"]).name for region in manifest["regions"])


def _manifest_kind(manifest: dict) -> str:
    names = _sample_names(manifest)
    first = names[0]
    if first.startswith("rel"):
        return "hammer"
    if first.startswith("harmV3"):
        return "resonance_v3"
    if first.startswith(("harmS", "harmL")):
        return "resonance"
    if first.startswith("pedalD"):
        return "pedal_down"
    if first.startswith("pedalU"):
        return "pedal_up"
    return "main"


class PianoPortableContractTests(unittest.TestCase):
    def _capture_child_manifests(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base_directory = Path(temporary.name)
        (base_directory / "assets" / "Samples").mkdir(parents=True)

        captured: list[dict] = []

        def fake_from_manifest(manifest, sample_rate, *, base_directory):
            del sample_rate, base_directory
            snapshot = copy.deepcopy(manifest)
            captured.append(snapshot)
            return SimpleNamespace(manifest=snapshot)

        manifest = {
            "asset_root": "assets",
            "resampling_quality": "bandlimited",
        }
        with mock.patch.object(
            PIANO_MODULE.SampleInstrument,
            "from_manifest",
            side_effect=fake_from_manifest,
        ):
            piano = PIANO_MODULE.PianoInstrument(
                48_000,
                manifest,
                str(base_directory),
            )

        self.assertEqual(len(captured), 6)
        by_kind = {_manifest_kind(child): child for child in captured}
        self.assertEqual(
            set(by_kind),
            {
                "main",
                "hammer",
                "resonance",
                "resonance_v3",
                "pedal_down",
                "pedal_up",
            },
        )
        return piano, by_kind

    def test_child_region_inventory_and_bandlimited_resampling_are_bound(self) -> None:
        piano, manifests = self._capture_child_manifests()

        self.assertEqual(len(manifests["main"]["regions"]), 30 * 16)
        self.assertEqual(len(manifests["hammer"]["regions"]), 88)
        self.assertEqual(len(manifests["resonance"]["regions"]), 23 * 2)
        self.assertEqual(len(manifests["resonance_v3"]["regions"]), 23)
        self.assertEqual(len(manifests["pedal_down"]["regions"]), 2)
        self.assertEqual(len(manifests["pedal_up"]["regions"]), 2)

        for name, child in manifests.items():
            self.assertEqual(
                child.get("resampling_quality"),
                "bandlimited",
                f"{name} must inherit the piano's release-quality resampler",
            )

        self.assertIs(
            piano.resonance_v3.manifest,
            manifests["resonance_v3"],
        )

    def test_main_mapping_has_explicit_upstream_key_ranges_and_c8_correction(
        self,
    ) -> None:
        _, manifests = self._capture_child_manifests()
        regions = manifests["main"]["regions"]
        self.assertTrue(
            all("key_min" in region and "key_max" in region for region in regions)
        )

        # The final upstream zone explicitly assigns both B7 and C8 to the
        # file named C8.  That file is actually recorded at C#8, so the root
        # correction and the key interval are two inseparable parts of the
        # mapping contract.
        c8_regions = [
            region
            for region in regions
            if Path(region["sample"]).name.startswith("C8v")
        ]
        self.assertEqual(len(c8_regions), 16)
        self.assertEqual(
            {
                (
                    region["key_min"],
                    region["key_max"],
                    region["root_midi"],
                    region.get("measured_tuning_cents"),
                )
                for region in c8_regions
            },
            {(107, 108, 108, 100.0)},
        )

        a7_regions = [
            region
            for region in regions
            if Path(region["sample"]).name.startswith("A7v")
        ]
        self.assertEqual(
            {(region["key_min"], region["key_max"]) for region in a7_regions},
            {(104, 106)},
        )

        covered = {
            midi_note
            for region in regions[:30]
            for midi_note in range(int(region["key_min"]), int(region["key_max"]) + 1)
        }
        self.assertEqual(covered, set(range(21, 109)))

    def test_v3_release_layer_preserves_upstream_velocity_and_hold_decay(self) -> None:
        piano, manifests = self._capture_child_manifests()
        self.assertAlmostEqual(
            manifests["resonance_v3"]["velocity_exponent"],
            1.92,
        )
        self.assertAlmostEqual(piano.resonance_v3_velocity_exponent, 1.92)
        self.assertAlmostEqual(
            piano.resonance_v3_rt_decay_db_per_second,
            2.0,
        )

    def test_expressive_audition_cannot_overwrite_full_range_evidence(self) -> None:
        source = EXPRESSIVE_AUDITION_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            'output_path=here / "表现力试听核验.json"',
            source,
        )
        self.assertNotIn(
            'output_path=here / "试听核验.json"',
            source,
        )


if __name__ == "__main__":
    unittest.main()
