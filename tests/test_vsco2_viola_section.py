from __future__ import annotations

import gc
import hashlib
import json
import math
from pathlib import Path
import struct
import unittest

import pytest

from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "乐器" / "管弦乐" / "弦乐组" / "中提琴"
MANIFEST_PATH = HERE / "乐器.json"
MAPPING_SOURCE = ROOT / "tianlai" / "vsco2_viola_mapping.py"
IMPLEMENTATION_SOURCE = ROOT / "tianlai" / "vsco2_viola.py"
VPO_ROOT = (
    ROOT
    / "音源"
    / "VirtualPlayingOrchestra"
    / "Virtual-Playing-Orchestra3"
)
SOURCE_ROOT = (
    VPO_ROOT
    / "libs"
    / "VSCO2-CE"
    / "Strings"
    / "Viola Section"
)
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(
    SOURCE_ROOT.is_dir(),
    "VSCO2-CE Viola Section is not installed",
)
class Vsco2ViolaSectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.calibration = json.loads(
            (HERE / "音准校准.json").read_text(encoding="utf-8")
        )
        cls.resource = json.loads(
            (HERE / "资源核验.json").read_text(encoding="utf-8")
        )

    def create_viola(self):
        return create_instrument(
            self.manifest,
            48_000,
            base_directory=str(HERE),
        )

    def test_manifest_is_pure_cc0_candidate_without_fallback(self) -> None:
        self.assertEqual(self.manifest["type"], "vsco2_viola_section")
        self.assertEqual(self.manifest["quality_tier"], "formal")
        self.assertEqual(
            self.manifest["collaboration_review_status"], "untested"
        )
        self.assertEqual(self.manifest["license_status"], "approved")
        self.assertEqual(
            self.manifest["allowed_articulations"],
            ["sustain", "spiccato"],
        )
        self.assertEqual(
            self.manifest["recorded_velocity_layers"],
            {"sustain": 1, "spiccato": 1},
        )
        self.assertEqual(
            self.manifest["recorded_round_robins"],
            {"sustain": 1, "spiccato": 2},
        )
        self.assertNotIn("soundfont", self.manifest)
        self.assertNotIn("pizzicato", self.manifest["allowed_articulations"])
        self.assertNotIn("staccato", self.manifest["allowed_articulations"])

    def test_runtime_loads_exactly_36_files_inside_cc0_subtree(self) -> None:
        instrument = self.create_viola()
        self.assertEqual(
            {name: len(engine.regions) for name, engine in instrument.engines.items()},
            {"sustain": 12, "spiccato": 24},
        )
        all_regions = [
            region
            for engine in instrument.engines.values()
            for region in engine.regions
        ]
        self.assertEqual(len({region.path for region in all_regions}), 36)
        self.assertTrue(
            all(region.path.is_relative_to(SOURCE_ROOT) for region in all_regions)
        )
        sustain = instrument.engines["sustain"].regions
        spiccato = instrument.engines["spiccato"].regions
        self.assertTrue(
            all(
                region.loop_start is not None and region.loop_end is not None
                for region in sustain
            )
        )
        self.assertTrue(
            all(
                region.loop_start is None
                and region.loop_end is None
                and region.loop_mode == "one_shot"
                for region in spiccato
            )
        )
        self.assertEqual(
            {(region.velocity_min, region.velocity_max) for region in all_regions},
            {(0.0, 1.0)},
        )
        self.assertEqual(
            {region.round_robin_position for region in sustain},
            {None},
        )
        self.assertEqual(
            {region.round_robin_position for region in spiccato},
            {1, 2},
        )

    def test_calibration_is_complete_and_applied_per_file(self) -> None:
        self.assertEqual(self.calibration["status"], "passed")
        self.assertEqual(
            self.calibration["summary"]["accepted_sample_count"],
            36,
        )
        self.assertEqual(self.calibration["failures"], [])
        instrument = self.create_viola()
        checked = 0
        for engine in instrument.engines.values():
            for region in engine.regions:
                relative = region.path.relative_to(VPO_ROOT).as_posix()
                record = self.calibration["samples"][relative]
                expected_hz = 440.0 * 2.0 ** (
                    (float(record["root_midi"]) - 69.0) / 12.0
                )
                corrected_root = expected_hz * 2.0 ** (
                    float(record["measured_detune_cents"]) / 1200.0
                )
                self.assertAlmostEqual(
                    region.root_pitch_hz,
                    corrected_root,
                    places=5,
                )
                self.assertAlmostEqual(
                    region.root_pitch_hz,
                    float(record["measured_hz"]),
                    places=5,
                )
                checked += 1
        self.assertEqual(checked, 36)

    def test_true_spiccato_round_robin_and_range_are_enforced(self) -> None:
        instrument = self.create_viola()
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(
                0,
                0,
                "articulation",
                {"name": "spiccato"},
            ),
            tuning,
        )
        positions = []
        for index in range(3):
            note_id = index + 1
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    index * 2 + 1,
                    "note_on",
                    {
                        "note_id": note_id,
                        "midi_note": 62,
                        "velocity": 0.72,
                    },
                ),
                tuning,
            )
            positions.append(
                list(instrument.engines["spiccato"].voices.values())[
                    -1
                ].region.round_robin_position
            )
            instrument.handle_event(
                PerformanceEvent(
                    1,
                    index * 2 + 2,
                    "note_off",
                    {"note_id": note_id, "release_velocity": 0.5},
                ),
                tuning,
            )
        self.assertEqual(positions, [1, 2, 1])
        self.assertTrue(
            all(
                not voice.released
                for voice in instrument.engines["spiccato"].voices.values()
            )
        )

        for note in (47, 94):
            with self.assertRaisesRegex(ValueError, "outside the sampled"):
                instrument.handle_event(
                    PerformanceEvent(
                        0,
                        20 + note,
                        "note_on",
                        {
                            "note_id": 20 + note,
                            "midi_note": note,
                            "velocity": 0.7,
                        },
                    ),
                    tuning,
                )
        for false_name in ("staccato", "pizzicato", "accent", "tremolo"):
            with self.assertRaisesRegex(ValueError, "unsupported"):
                instrument.handle_event(
                    PerformanceEvent(
                        0,
                        100,
                        "articulation",
                        {"name": false_name},
                    ),
                    tuning,
                )

    def test_sustain_expression_pedal_and_release(self) -> None:
        instrument = self.create_viola()
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(
                0,
                0,
                "control",
                {"name": "expression", "value": 0.5},
            ),
            tuning,
        )
        self.assertAlmostEqual(instrument.expression_target, 0.5**1.35)
        instrument.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 7, "midi_note": 60, "velocity": 0.75},
            ),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(
                1,
                2,
                "control",
                {"name": "sustain_pedal", "value": 1.0},
            ),
            tuning,
        )
        instrument.handle_event(
            PerformanceEvent(
                2,
                3,
                "note_off",
                {"note_id": 7, "release_velocity": 0.4},
            ),
            tuning,
        )
        voice = instrument.engines["sustain"].voices[7]
        self.assertTrue(voice.pending_release)
        self.assertFalse(voice.released)
        instrument.handle_event(
            PerformanceEvent(
                3,
                4,
                "control",
                {"name": "sustain_pedal", "value": 0.0},
            ),
            tuning,
        )
        self.assertTrue(voice.released)

    def _render_digest_and_peak(self) -> tuple[str, float]:
        instrument = self.create_viola()
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(
                0,
                0,
                "articulation",
                {"name": "spiccato"},
            ),
            tuning,
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
        digest = hashlib.sha256()
        peak = 0.0
        for frame_index in range(24_000):
            if frame_index == 6_000:
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
        first_hash, first_peak = self._render_digest_and_peak()
        gc.collect()
        second_hash, second_peak = self._render_digest_and_peak()
        self.assertEqual(first_hash, second_hash)
        self.assertEqual(first_peak, second_peak)
        self.assertGreater(first_peak, 0.02)
        self.assertLess(first_peak, 1.0)

    def test_resource_report_freezes_license_formats_and_signal_gates(self) -> None:
        self.assertEqual(self.resource["schema_version"], 2)
        self.assertEqual(self.resource["status"], "passed")
        self.assertEqual(self.resource["license"], "CC0-1.0")
        self.assertEqual(self.resource["license_status"], "approved")
        self.assertEqual(self.resource["sample_count"], 36)
        self.assertEqual(self.resource["unique_audio_sha256_count"], 36)
        self.assertEqual(
            self.resource["sample_formats"],
            {
                "WAV:PCM_16:44100Hz:2ch": 33,
                "WAV:PCM_24:44100Hz:2ch": 3,
            },
        )
        self.assertEqual(
            self.resource["signal_summary"]["clipped_value_count"],
            0,
        )
        self.assertLessEqual(
            self.resource["signal_summary"][
                "maximum_spiccato_tail_20ms_rms"
            ],
            self.resource["signal_summary"][
                "spiccato_tail_20ms_rms_limit"
            ],
        )
        self.assertLessEqual(
            self.resource["signal_summary"][
                "maximum_sustain_loop_seam_difference"
            ],
            self.resource["signal_summary"][
                "sustain_loop_seam_review_limit"
            ],
        )
        self.assertEqual(self.resource["hash_algorithm"], HASH_ALGORITHM)
        self.assertEqual(
            self.resource["canonicalization"],
            CANONICALIZATION,
        )
        self.assertEqual(
            self.resource["manifest_canonical_sha256"],
            canonical_json_file_sha256(MANIFEST_PATH),
        )
        self.assertEqual(
            self.resource["mapping_source"],
            "tianlai/vsco2_viola_mapping.py",
        )
        self.assertEqual(
            self.resource["mapping_sha256"],
            hashlib.sha256(MAPPING_SOURCE.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            self.resource["implementation_source"],
            "tianlai/vsco2_viola.py",
        )
        self.assertEqual(
            self.resource["implementation_sha256"],
            hashlib.sha256(IMPLEMENTATION_SOURCE.read_bytes()).hexdigest(),
        )
        for relative, digest in self.resource["evidence_sha256"].items():
            self.assertEqual(
                hashlib.sha256((VPO_ROOT / relative).read_bytes()).hexdigest(),
                digest,
            )
        for relative, digest in self.resource[
            "mapping_evidence_sha256"
        ].items():
            self.assertEqual(
                hashlib.sha256((VPO_ROOT / relative).read_bytes()).hexdigest(),
                digest,
            )


if __name__ == "__main__":
    unittest.main()
