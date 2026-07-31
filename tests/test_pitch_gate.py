from __future__ import annotations

import json
import math
from pathlib import Path
import unittest

import numpy as np
import pytest

from tianlai.analysis import (
    analyze_instrument_pitch,
    analyze_signal_wide_pitch,
)


ROOT = Path(__file__).resolve().parents[1]
KEYBOARD_MANIFESTS = tuple(
    ROOT / "乐器" / "键盘乐器" / name / "乐器.json"
    for name in ("电钢琴", "合唱电钢琴", "击弦古钢琴")
)
INSTRUMENT_PITCH_GATE_NOTES = {
    # A real clavichord has stretched, strongly inharmonic partials.  Keep the
    # short-window end-to-end gate on low/middle/high notes whose fundamentals
    # are measurable; the dedicated SIMPK calibration still checks every one
    # of the 756 source recordings.
    "击弦古钢琴": (32, 61, 87),
}


class WidePitchEstimatorTests(unittest.TestCase):
    sample_rate = 24_000

    def _time(self) -> np.ndarray:
        return np.arange(self.sample_rate, dtype="float64") / self.sample_rate

    def test_dominant_second_harmonic_does_not_create_octave_error(self) -> None:
        time = self._time()
        audio = (
            0.04 * np.sin(2.0 * math.pi * 440.0 * time)
            + np.sin(2.0 * math.pi * 880.0 * time)
            + 0.42 * np.sin(2.0 * math.pi * 1320.0 * time)
            + 0.18 * np.sin(2.0 * math.pi * 1760.0 * time)
        )

        result = analyze_signal_wide_pitch(audio, self.sample_rate, 440.0)

        self.assertTrue(result.clear_pitch, result)
        self.assertTrue(result.within_tolerance(3.0), result)
        self.assertEqual(result.nearest_octave_error, 0)

    def test_missing_fundamental_is_recovered_from_harmonic_spacing(self) -> None:
        time = self._time()
        audio = (
            np.sin(2.0 * math.pi * 880.0 * time)
            + 0.7 * np.sin(2.0 * math.pi * 1320.0 * time)
            + 0.4 * np.sin(2.0 * math.pi * 1760.0 * time)
        )

        result = analyze_signal_wide_pitch(audio, self.sample_rate, 440.0)

        self.assertTrue(result.clear_pitch, result)
        self.assertTrue(result.within_tolerance(3.0), result)

    def test_stretched_low_string_partials_resolve_to_the_quiet_fundamental(
        self,
    ) -> None:
        time = self._time()
        fundamental = 40.30
        audio = np.exp(-1.2 * time) * (
            0.19 * np.sin(2.0 * math.pi * fundamental * time)
            + np.sin(2.0 * math.pi * (2.0 * fundamental) * time)
            # The odd partials are deliberately stretched by roughly 30 cents,
            # as they are in the real lowest Meatbass pizzicato recording.
            + 0.15 * np.sin(2.0 * math.pi * 123.0 * time)
            + 0.14 * np.sin(2.0 * math.pi * 205.0 * time)
        )

        result = analyze_signal_wide_pitch(
            audio,
            self.sample_rate,
            41.20344461410875,
        )

        self.assertTrue(result.clear_pitch, result)
        self.assertEqual(result.nearest_octave_error, 0, result)
        self.assertAlmostEqual(result.detune_cents or 0.0, -39.0, delta=4.0)

    def test_full_octave_mapping_errors_are_visible(self) -> None:
        time = self._time()
        cases = (
            (
                -1,
                np.sin(2.0 * math.pi * 220.0 * time)
                + 0.5 * np.sin(2.0 * math.pi * 440.0 * time)
                + 0.25 * np.sin(2.0 * math.pi * 660.0 * time),
            ),
            (
                1,
                np.sin(2.0 * math.pi * 880.0 * time)
                + 0.5 * np.sin(2.0 * math.pi * 1760.0 * time)
                + 0.25 * np.sin(2.0 * math.pi * 2640.0 * time),
            ),
        )
        for expected_octaves, audio in cases:
            with self.subTest(octaves=expected_octaves):
                result = analyze_signal_wide_pitch(
                    audio, self.sample_rate, 440.0
                )
                self.assertTrue(result.clear_pitch, result)
                self.assertEqual(result.nearest_octave_error, expected_octaves)
                self.assertAlmostEqual(
                    result.detune_cents or 0.0,
                    expected_octaves * 1200.0,
                    delta=5.0,
                )
                self.assertFalse(result.within_tolerance(35.0))

    def test_noise_and_inharmonic_percussion_are_not_forced_to_a_pitch(self) -> None:
        time = self._time()
        envelope = np.exp(-6.0 * time)
        inharmonic = envelope * sum(
            np.sin(2.0 * math.pi * frequency * time)
            for frequency in (517.0, 811.0, 1237.0, 1783.0)
        )
        noise = envelope * np.random.default_rng(20260725).normal(
            size=len(time)
        )

        for name, audio in (("inharmonic", inharmonic), ("noise", noise)):
            with self.subTest(signal=name):
                result = analyze_signal_wide_pitch(
                    audio, self.sample_rate, 440.0
                )
                self.assertFalse(result.clear_pitch, result)
                self.assertIsNone(result.measured_hz)
                self.assertIsNone(result.detune_cents)
                self.assertFalse(result.within_tolerance(35.0))


class KeyboardEndToEndPitchGateTests(unittest.TestCase):
    @staticmethod
    def _load_manifest(path: Path) -> dict[str, object]:
        with path.open("r", encoding="utf-8") as source:
            document = json.load(source)
        if not isinstance(document, dict):
            raise AssertionError(f"manifest root is not an object: {path}")
        return document

    @pytest.mark.external_assets
    def test_low_middle_high_notes_have_no_octave_or_tuning_error(self) -> None:
        missing_assets: list[str] = []
        manifests: list[tuple[Path, dict[str, object]]] = []
        for path in KEYBOARD_MANIFESTS:
            manifest = self._load_manifest(path)
            asset_root = (path.parent / str(manifest["asset_root"])).resolve()
            if not asset_root.is_dir():
                missing_assets.append(str(asset_root))
            manifests.append((path, manifest))
        if missing_assets:
            self.skipTest("pitch-gate assets are not installed: " + ", ".join(missing_assets))

        for path, manifest in manifests:
            note_min = float(manifest["note_min"])
            note_max = float(manifest["note_max"])
            span = note_max - note_min
            probe_notes = INSTRUMENT_PITCH_GATE_NOTES.get(
                path.parent.name,
                tuple(
                    sorted(
                        {
                            round(note_min + min(12.0, span * 0.18)),
                            round((note_min + note_max) * 0.5),
                            round(note_max - min(12.0, span * 0.18)),
                        }
                    )
                ),
            )
            for midi_note in probe_notes:
                with self.subTest(instrument=path.parent.name, midi_note=midi_note):
                    result = analyze_instrument_pitch(
                        path,
                        midi_note,
                        sample_rate=24_000,
                        duration_seconds=0.64,
                        maximum_frames=12_288,
                    )
                    self.assertTrue(result.clear_pitch, result)
                    self.assertEqual(result.nearest_octave_error, 0, result)
                    self.assertTrue(result.within_tolerance(35.0), result)


if __name__ == "__main__":
    unittest.main()
