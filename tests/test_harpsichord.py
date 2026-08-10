from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import runpy
import tempfile
import unittest

import pytest

from tianlai.analysis import analyze_instrument_pitch
from tianlai.dedicated_candidates import dedicated_manifest_sources
from tianlai.dedicated_sfz import DedicatedSfzInstrument
from tianlai.events import PerformanceEvent
from tianlai.tuning import EqualTemperament


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

    @classmethod
    def _render_registration_probe_sequence(
        cls,
        articulation: str,
    ) -> tuple[
        list[tuple[float, float]],
        list[tuple[range, range]],
        list[tuple[int, ...]],
    ]:
        """Render the same low/middle/high attack and release probes."""

        sample_rate = 48_000
        instrument = DedicatedSfzInstrument(
            sample_rate,
            cls.manifest,
            str(MANIFEST.parent),
        )
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(
                0,
                0,
                "articulation",
                {"name": articulation},
            ),
            tuning,
        )

        frames: list[tuple[float, float]] = []
        spans: list[tuple[range, range]] = []
        release_voice_counts: list[tuple[int, ...]] = []
        current_sample = 0
        attack_frames = 2_048
        release_frames = 4_096
        for sequence, midi_note in enumerate((29, 56, 84), start=1):
            note_id = 10_000 + sequence
            instrument.handle_event(
                PerformanceEvent(
                    current_sample,
                    sequence * 2 - 1,
                    "note_on",
                    {
                        "note_id": note_id,
                        "midi_note": midi_note,
                        "velocity": 0.72,
                    },
                ),
                tuning,
            )
            attack_start = len(frames)
            for _ in range(attack_frames):
                frames.append(instrument.render_frame())
                current_sample += 1
            attack_stop = len(frames)

            instrument.handle_event(
                PerformanceEvent(
                    current_sample,
                    sequence * 2,
                    "note_off",
                    {"note_id": note_id},
                ),
                tuning,
            )
            runtime = instrument.articulations[articulation]
            release_voice_counts.append(
                tuple(
                    layer.engine.active_voice_count
                    for layer in runtime.release_layers
                )
            )
            release_start = len(frames)
            for _ in range(release_frames):
                frames.append(instrument.render_frame())
                current_sample += 1
            release_stop = len(frames)
            spans.append(
                (
                    range(attack_start, attack_stop),
                    range(release_start, release_stop),
                )
            )
        return frames, spans, release_voice_counts

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

    def test_every_attack_and_release_layer_is_bound_to_bandlimited_resampling(
        self,
    ) -> None:
        self.assertEqual(self.manifest["resampling_quality"], "bandlimited")
        self.assertEqual(self.manifest["gain"], 0.8)
        instrument = DedicatedSfzInstrument(
            48_000,
            self.manifest,
            str(MANIFEST.parent),
        )
        expected_layer_counts = {
            "full": (2, 2),
            "eight_foot": (1, 1),
            "four_foot": (1, 1),
        }
        for articulation, (attack_count, release_count) in expected_layer_counts.items():
            with self.subTest(articulation=articulation):
                runtime = instrument.articulations[articulation]
                self.assertEqual(len(runtime.attack_layers), attack_count)
                self.assertEqual(len(runtime.release_layers), release_count)
                for layer in (*runtime.attack_layers, *runtime.release_layers):
                    self.assertEqual(
                        layer.engine.resampling_quality,
                        "bandlimited",
                    )

    def test_full_registration_equals_eight_plus_four_foot_during_attack_and_release(
        self,
    ) -> None:
        full, spans, full_release_counts = (
            self._render_registration_probe_sequence("full")
        )
        eight, eight_spans, eight_release_counts = (
            self._render_registration_probe_sequence("eight_foot")
        )
        four, four_spans, four_release_counts = (
            self._render_registration_probe_sequence("four_foot")
        )
        self.assertEqual(spans, eight_spans)
        self.assertEqual(spans, four_spans)
        self.assertEqual(len(full), len(eight))
        self.assertEqual(len(full), len(four))

        for probe_index, midi_note in enumerate((29, 56, 84)):
            with self.subTest(midi_note=midi_note):
                expected_active_releases = probe_index + 1
                self.assertEqual(
                    full_release_counts[probe_index],
                    (expected_active_releases, expected_active_releases),
                )
                self.assertEqual(
                    eight_release_counts[probe_index],
                    (expected_active_releases,),
                )
                self.assertEqual(
                    four_release_counts[probe_index],
                    (expected_active_releases,),
                )

                attack_span, release_span = spans[probe_index]
                for phase, indices in (
                    ("attack", attack_span),
                    ("release", release_span),
                ):
                    maximum_difference = max(
                        abs(
                            full[index][channel]
                            - (eight[index][channel] + four[index][channel])
                        )
                        for index in indices
                        for channel in (0, 1)
                    )
                    self.assertLessEqual(
                        maximum_difference,
                        1.0e-12,
                        f"{midi_note=} {phase=}",
                    )

    def test_full_ten_note_maximum_velocity_chord_keeps_render_headroom(
        self,
    ) -> None:
        instrument = DedicatedSfzInstrument(
            48_000,
            self.manifest,
            str(MANIFEST.parent),
        )
        tuning = EqualTemperament()
        instrument.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "full"}),
            tuning,
        )
        notes = (36, 43, 48, 52, 55, 60, 64, 67, 72, 76)
        for note_id, midi_note in enumerate(notes, start=1):
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    note_id,
                    "note_on",
                    {
                        "note_id": note_id,
                        "midi_note": midi_note,
                        "velocity": 1.0,
                    },
                ),
                tuning,
            )

        peak = 0.0
        for _ in range(9_600):
            frame = instrument.render_frame()
            self.assertTrue(all(math.isfinite(channel) for channel in frame))
            peak = max(peak, *(abs(channel) for channel in frame))
        for note_id in range(1, len(notes) + 1):
            instrument.handle_event(
                PerformanceEvent(
                    9_600,
                    20 + note_id,
                    "note_off",
                    {"note_id": note_id},
                ),
                tuning,
            )
        for _ in range(9_600):
            frame = instrument.render_frame()
            self.assertTrue(all(math.isfinite(channel) for channel in frame))
            peak = max(peak, *(abs(channel) for channel in frame))

        self.assertGreater(peak, 0.1)
        self.assertLess(peak, 0.90)

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
