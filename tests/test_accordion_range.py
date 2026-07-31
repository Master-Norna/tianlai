from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

import pytest

from tianlai.dedicated_candidates import dedicated_manifest_sources
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "乐器" / "键盘乐器" / "手风琴"
MANIFEST = DIRECTORY / "乐器.json"
pytestmark = pytest.mark.external_assets


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _note_on(note_id: int, midi_note: int) -> PerformanceEvent:
    return PerformanceEvent(
        0,
        note_id,
        "note_on",
        {"note_id": note_id, "midi_note": midi_note, "velocity": 0.72},
    )


class AccordionRangeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = _load(MANIFEST)
        asset_root = (
            MANIFEST.parent / str(cls.manifest["asset_root"])
        ).resolve()
        if not asset_root.exists():
            raise unittest.SkipTest(
                f"FreePats accordion resource is not installed: {asset_root}"
            )
        required = [
            asset_root / str(relative)
            for relative in cls.manifest["articulations"].values()
        ]
        missing = [path for path in required if not path.is_file()]
        if missing:
            raise AssertionError(
                "FreePats accordion resource is partially installed; "
                f"missing: {', '.join(str(path) for path in missing)}"
            )
        cls.inventory = dedicated_manifest_sources(MANIFEST)
        cls.attacks = cls.inventory["articulations"]["sustain"]["attack_regions"]
        cls.releases = cls.inventory["articulations"]["sustain"]["release_regions"]

    def create_accordion(self):
        return create_instrument(
            self.manifest,
            48_000,
            base_directory=str(DIRECTORY),
        )

    def test_manifest_uses_the_exact_cc0_release_and_correct_origin(self) -> None:
        self.assertEqual(self.manifest["license"], "CC0-1.0")
        self.assertEqual(self.manifest["license_status"], "approved")
        self.assertEqual(
            self.manifest["origin"],
            "https://freepats.zenvoid.org/Organ/accordion.html",
        )
        self.assertEqual(self.manifest["upstream_version"], "2024-03-29")
        self.assertEqual(
            self.manifest["fallback_policy"],
            "explicit_only_no_silent_gm",
        )
        self.assertEqual(self.manifest["default_articulation"], "sustain")
        self.assertEqual(set(self.manifest["articulations"]), {"sustain"})

    def test_source_has_17_attack_release_pairs_and_no_higher_root(self) -> None:
        roots = sorted({int(region["root_midi"]) for region in self.attacks})
        self.assertEqual(
            roots,
            [47, 50, 54, 55, 57, 59, 60, 62, 64, 66, 67, 69, 71, 72, 74, 76, 79],
        )
        self.assertEqual(len(self.attacks), 17)
        self.assertEqual(len(self.releases), 17)
        self.assertTrue(all(region["loop_mode"] == "loop_continuous" for region in self.attacks))
        self.assertTrue(all(region["loop_mode"] == "one_shot" for region in self.releases))
        self.assertEqual(max(float(region["key_max"]) for region in self.attacks), 127.0)

        attack_names = {Path(region["sample"]).name for region in self.attacks}
        release_names = {Path(region["sample"]).name for region in self.releases}
        self.assertEqual(
            {name.removesuffix("_rel.flac") for name in release_names},
            {name.removesuffix(".flac") for name in attack_names},
        )

    def test_bounded_extension_never_exceeds_an_existing_core_zone(self) -> None:
        self.assertEqual(self.manifest["note_min"], 50)
        self.assertEqual(self.manifest["note_max"], 82)
        self.assertEqual(self.manifest["range_evidence"], "资源核验.json")
        rationale = self.manifest["modeling_rationale"]
        self.assertIn("MIDI 50-79", rationale)
        self.assertIn("MIDI 80-82", rationale)
        self.assertIn("+3 semitones", rationale)
        self.assertIn("MIDI 83-91 is rejected", rationale)

        def mapped_root(note: int) -> int:
            matches = [
                region
                for region in self.attacks
                if float(region["key_min"]) <= note <= float(region["key_max"])
            ]
            self.assertEqual(len(matches), 1, note)
            return int(matches[0]["root_midi"])

        core_shift = max(note - mapped_root(note) for note in range(50, 80))
        extension_shifts = [note - mapped_root(note) for note in range(80, 83)]
        self.assertEqual(core_shift, 3)
        self.assertEqual(extension_shifts, [1, 2, 3])
        self.assertLessEqual(max(extension_shifts), core_shift)
        self.assertIn("MIDI 83-91", self.manifest["sampled_range_note"])

    def test_runtime_accepts_82_but_rejects_the_former_83_to_91_tail(self) -> None:
        instrument = self.create_accordion()
        tuning = EqualTemperament()
        instrument.handle_event(_note_on(1, 79), tuning)
        route_79 = instrument.routes[1]
        voice_79 = instrument.articulations["sustain"].attack.voices[
            route_79.internal_note_id
        ]
        instrument.handle_event(_note_on(2, 82), tuning)
        route_82 = instrument.routes[2]
        voice_82 = instrument.articulations["sustain"].attack.voices[
            route_82.internal_note_id
        ]
        self.assertEqual(voice_79.region.path.name, "Button Accordion HN G6.flac")
        self.assertEqual(voice_82.region.path, voice_79.region.path)
        self.assertAlmostEqual(
            voice_82.increment / voice_79.increment,
            2.0 ** (3.0 / 12.0),
            places=6,
        )

        for note in (49, 83, 91):
            with self.subTest(note=note):
                with self.assertRaisesRegex(ValueError, "outside declared range"):
                    instrument.handle_event(_note_on(100 + note, note), tuning)

    def test_resource_report_freezes_the_range_and_license_evidence(self) -> None:
        report = _load(DIRECTORY / "资源核验.json")
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["license"], "CC0-1.0")
        self.assertEqual(report["origin"], self.manifest["origin"])
        self.assertEqual(report["sample_count"], 34)
        self.assertEqual(report["sample_formats"], {".flac:44100Hz:2ch": 34})
        self.assertEqual(set(report["evidence_sha256"]), {"LICENSE.txt", "README.txt"})
        self.assertEqual(
            report["manifest_sha256"],
            hashlib.sha256(MANIFEST.read_bytes()).hexdigest(),
        )
        policy = report["runtime_range_policy"]
        self.assertEqual(policy["core_playable_range"], [50, 79])
        self.assertEqual(policy["bounded_extension_range"], [80, 82])
        self.assertEqual(policy["legacy_rejected_range"], [83, 91])
        self.assertEqual(policy["maximum_core_upward_transposition_semitones"], 3)
        self.assertEqual(
            policy["maximum_extension_upward_transposition_semitones"],
            3,
        )
        self.assertTrue(policy["extension_not_wider_than_existing_core_zone"])
        self.assertEqual(policy["note_mapping"]["82"]["root_midi"], 79)

    def test_audition_covers_both_high_boundaries_without_clipping(self) -> None:
        report = _load(DIRECTORY / "试听核验.json")
        self.assertEqual(report["status"], "machine_pass_human_pending")
        self.assertEqual(report["audition_profile"], "ascending-scale")
        self.assertEqual(report["clipped_samples"], 0)
        self.assertLess(report["peak"], 0.98)
        self.assertIn("MIDI 50-82", " ".join(report["coverage"]))
        events_path = ROOT / report["events"]
        events = _load(events_path)
        note_ons = [
            event["midi_note"]
            for event in events["events"]
            if event["type"] == "note_on"
        ]
        self.assertEqual(note_ons, list(range(50, 83)))

        previous = report["previous_protocol_evidence"]
        self.assertEqual(
            previous["status"],
            "superseded_event_bound_machine_evidence",
        )
        policy = previous["fields"]["range_policy"]
        self.assertEqual(policy["highest_real_root_audited"], 79)
        self.assertEqual(policy["bounded_extension_top_audited"], 82)
        self.assertEqual(max(policy["audition_note_ons"]), 82)
        self.assertGreater(
            previous["fields"]["signal_gates"]["bounded_extension_top"]["rms"],
            0.005,
        )
        self.assertLessEqual(
            previous["fields"]["signal_gates"]["final_50ms"]["rms"],
            0.00001,
        )
        wav_path = ROOT / report["wav"]
        self.assertEqual(report["wav_persistence"], "temporary")
        self.assertRegex(report["wav_sha256"], r"^[0-9a-f]{64}$")
        if wav_path.is_file():
            self.assertEqual(
                hashlib.sha256(wav_path.read_bytes()).hexdigest(),
                report["wav_sha256"],
            )

    def test_source_documentation_records_resource_choice_and_range(self) -> None:
        text = (DIRECTORY / "来源.md").read_text(encoding="utf-8")
        for marker in (
            "FreePats",
            "CC0-1.0",
            "MIDI 50–79",
            "MIDI 83–91",
        ):
            self.assertIn(marker, text)
        self.assertIn("没有混入其他手风琴或合成资源", text)


if __name__ == "__main__":
    unittest.main()
