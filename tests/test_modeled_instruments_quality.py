"""Perceptual-signal regressions for the deterministic modeled instruments.

These tests intentionally measure the failure modes found by the first
full-range human review: DC-biased plucked strings, discontinuous attacks,
electrical modal beating/clicks, abrupt Nyquist-boundary timbre changes, and
three taiko strokes that did not sound as if they shared one drum body.
"""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
import sys
import unittest

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.events import PerformanceEvent
from tianlai.modeled_instruments import (
    ENGINE_VERSION,
    ModeledInstrument,
    PROFILES,
    _bandlimit_gain,
)
from tianlai.tuning import EqualTemperament


SAMPLE_RATE = 48_000
TUNING = EqualTemperament(440.0)

PLUCKED_MANIFESTS = {
    "shamisen": ROOT / "乐器" / "世界乐器" / "三味线" / "乐器.json",
    "koto": ROOT / "乐器" / "世界乐器" / "日本筝" / "乐器.json",
    "sitar": ROOT / "乐器" / "世界乐器" / "西塔琴" / "乐器.json",
}
STEELPAN_MANIFEST = (
    ROOT / "乐器" / "管弦乐" / "打击乐组" / "钢鼓" / "乐器.json"
)
MUSIC_BOX_MANIFEST = ROOT / "乐器" / "键盘乐器" / "音乐盒" / "乐器.json"
TAIKO_MANIFEST = (
    ROOT / "乐器" / "管弦乐" / "打击乐组" / "太鼓" / "乐器.json"
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _render_note(
    manifest: dict,
    base: Path,
    midi: int,
    *,
    seconds: float,
    velocity: float = 0.72,
) -> np.ndarray:
    instrument = ModeledInstrument(
        SAMPLE_RATE,
        manifest,
        str(base),
    )
    instrument.handle_event(
        PerformanceEvent(
            0,
            0,
            "note_on",
            {
                "note_id": 1,
                "midi_note": midi,
                "velocity": velocity,
            },
        ),
        TUNING,
    )
    return np.fromiter(
        (instrument.render_frame()[0] for _ in range(round(seconds * SAMPLE_RATE))),
        dtype=np.float64,
    )


def _probe_notes(manifest: dict) -> tuple[int, ...]:
    low = int(manifest["note_min"])
    high = int(manifest["note_max"])
    return tuple(sorted({low, (low + high) // 2, high}))


def _pitch_error_cents(signal: np.ndarray, midi: int) -> float:
    start = round(0.25 * SAMPLE_RATE)
    segment = signal[start:] - np.mean(signal[start:])
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    frequencies = np.fft.rfftfreq(len(segment), 1.0 / SAMPLE_RATE)
    expected = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
    search_ratio = 2.0 ** (120.0 / 1200.0)
    bins = np.flatnonzero(
        (frequencies >= expected / search_ratio)
        & (frequencies <= expected * search_ratio)
    )
    peak = int(bins[np.argmax(spectrum[bins])])
    delta = 0.0
    if 0 < peak < len(spectrum) - 1:
        left, center, right = np.log(spectrum[peak - 1 : peak + 2] + 1.0e-20)
        denominator = left - 2.0 * center + right
        if denominator != 0.0:
            delta = float(0.5 * (left - right) / denominator)
    measured = (peak + delta) * SAMPLE_RATE / len(segment)
    return 1200.0 * math.log2(measured / expected)


def _modal_noise_only(
    manifest_path: Path,
    midi: int,
    *,
    seconds: float,
) -> tuple[np.ndarray, np.ndarray]:
    manifest = _load(manifest_path)
    quiet = {
        **manifest,
        "model_params": {
            **manifest.get("model_params", {}),
            "attack_noise": 0.0,
        },
    }
    audible = _render_note(
        manifest, manifest_path.parent, midi, seconds=seconds
    )
    without_noise = _render_note(
        quiet, manifest_path.parent, midi, seconds=seconds
    )
    return audible, audible - without_noise


class ModeledVersionTests(unittest.TestCase):
    def test_modeled_manifests_and_generated_evidence_are_hash_locked(self) -> None:
        self.assertEqual(ENGINE_VERSION, "1.1.0")
        engine_path = ROOT / "tianlai" / "modeled_instruments.py"
        engine_sha256 = hashlib.sha256(engine_path.read_bytes()).hexdigest()
        manifests = []
        for path in (ROOT / "乐器").rglob("乐器.json"):
            manifest = _load(path)
            if manifest.get("type") == "modeled_instrument":
                manifests.append((path, manifest))
        self.assertEqual(len(manifests), 10)
        for path, manifest in manifests:
            label = str(path.relative_to(ROOT))
            with self.subTest(manifest=label):
                self.assertEqual(manifest.get("engine_version"), ENGINE_VERSION)
                resource = _load(path.parent / manifest["resource_verification"])
                pitch = _load(path.parent / manifest["pitch_calibration"])
                source = (path.parent / "来源.md").read_text(encoding="utf-8")
                self.assertEqual(resource.get("engine_version"), ENGINE_VERSION)
                self.assertEqual(resource.get("engine_sha256"), engine_sha256)
                self.assertEqual(resource.get("profile"), manifest.get("profile"))
                self.assertEqual(resource.get("seed"), manifest.get("seed"))
                self.assertEqual(pitch.get("engine_version"), ENGINE_VERSION)
                self.assertIn(f"引擎版本:{ENGINE_VERSION}", source)


class PluckedStringSignalTests(unittest.TestCase):
    def test_koto_long_tail_survives_zero_crossings_then_release_ends(self) -> None:
        manifest = _load(PLUCKED_MANIFESTS["koto"])
        for midi, seconds in ((69, 2.3), (50, 6.0)):
            instrument = ModeledInstrument(
                SAMPLE_RATE,
                manifest,
                str(PLUCKED_MANIFESTS["koto"].parent),
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": midi, "velocity": 0.72},
                ),
                TUNING,
            )
            tail_peak = 0.0
            tail_start = round((seconds - 0.01) * SAMPLE_RATE)
            for frame in range(round(seconds * SAMPLE_RATE)):
                left, _ = instrument.render_frame()
                if frame >= tail_start:
                    tail_peak = max(tail_peak, abs(left))
            with self.subTest(midi=midi):
                self.assertEqual(instrument.active_voice_count, 1)
                self.assertGreater(tail_peak, 1.0e-3)

            instrument.handle_event(
                PerformanceEvent(0, 1, "note_off", {"note_id": 1}),
                TUNING,
            )
            for _ in range(round(0.3 * SAMPLE_RATE)):
                instrument.render_frame()
            with self.subTest(midi=midi, phase="release"):
                self.assertEqual(instrument.active_voice_count, 0)

    def test_plucked_natural_silence_window_eventually_ends_voice(self) -> None:
        manifest = _load(PLUCKED_MANIFESTS["koto"])
        manifest = {
            **manifest,
            "model_params": {
                **manifest.get("model_params", {}),
                "damping": 0.9,
                "velocity_brightness": 0.0,
            },
        }
        instrument = ModeledInstrument(
            SAMPLE_RATE,
            manifest,
            str(PLUCKED_MANIFESTS["koto"].parent),
        )
        instrument.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 69, "velocity": 0.72},
            ),
            TUNING,
        )
        for frame in range(round(3.0 * SAMPLE_RATE)):
            instrument.render_frame()
            if instrument.active_voice_count == 0:
                break
        self.assertEqual(instrument.active_voice_count, 0)
        self.assertGreater(frame, SAMPLE_RATE)

    def test_three_profiles_are_dc_safe_and_start_smoothly_across_range(self) -> None:
        for profile, path in PLUCKED_MANIFESTS.items():
            manifest = _load(path)
            with self.subTest(profile=profile, engine_version=manifest["engine_version"]):
                self.assertEqual(manifest["profile"], profile)
            for midi in _probe_notes(manifest):
                with self.subTest(profile=profile, midi=midi):
                    signal = _render_note(
                        manifest,
                        path.parent,
                        midi,
                        seconds=0.55,
                    )
                    steady = signal[
                        round(0.05 * SAMPLE_RATE) : round(0.4 * SAMPLE_RATE)
                    ]
                    self.assertGreater(np.max(np.abs(signal)), 0.01)
                    # Before the fix the koto mean reached about +0.1 FS.
                    self.assertLess(abs(float(np.mean(steady))), 0.001)
                    # The core and contact transient both begin at equilibrium.
                    self.assertAlmostEqual(float(signal[0]), 0.0, places=14)
                    self.assertLess(
                        float(np.max(np.abs(signal[:12]))),
                        0.02,
                        "first 0.25 ms must remain inside the smooth attack",
                    )

    def test_three_profiles_remain_bit_deterministic(self) -> None:
        for profile, path in PLUCKED_MANIFESTS.items():
            manifest = _load(path)
            midi = int(manifest["note_max"])
            first = _render_note(
                manifest, path.parent, midi, seconds=0.12
            )
            second = _render_note(
                manifest, path.parent, midi, seconds=0.12
            )
            with self.subTest(profile=profile):
                self.assertTrue(np.array_equal(first, second))

    def test_three_profiles_keep_calibrated_pitch(self) -> None:
        for profile, path in PLUCKED_MANIFESTS.items():
            manifest = _load(path)
            for midi in _probe_notes(manifest):
                signal = _render_note(
                    manifest,
                    path.parent,
                    midi,
                    seconds=1.2,
                    velocity=0.8,
                )
                error = _pitch_error_cents(signal, midi)
                with self.subTest(profile=profile, midi=midi):
                    self.assertLess(abs(error), 0.5)


class ModalSignalTests(unittest.TestCase):
    def test_steelpan_has_no_strong_near_coincident_pair_or_noise_click(self) -> None:
        modes = PROFILES["steelpan"]["params"]["modes"]
        for index, first in enumerate(modes):
            for second in modes[index + 1 :]:
                if abs(float(first[0]) - float(second[0])) <= 0.015:
                    self.assertLess(
                        min(float(first[1]), float(second[1])),
                        0.2,
                        "near-coincident steelpan modes may not both be strong",
                    )

        audible, noise = _modal_noise_only(
            STEELPAN_MANIFEST,
            69,
            seconds=0.08,
        )
        attack_frames = round(0.0035 * SAMPLE_RATE)
        self.assertAlmostEqual(float(audible[0]), 0.0, places=14)
        self.assertLess(float(np.max(np.abs(noise[:attack_frames]))), 0.02)
        self.assertLess(float(np.max(np.abs(noise[attack_frames:]))), 1.0e-12)

    def test_music_box_noise_is_smoothed_and_bandlimit_is_continuous(self) -> None:
        params = PROFILES["music_box"]["params"]
        start = float(params["bandlimit_start_ratio"])
        stop = float(params["bandlimit_stop_ratio"])
        self.assertLess(start, stop)
        self.assertAlmostEqual(
            _bandlimit_gain(
                SAMPLE_RATE * start,
                SAMPLE_RATE,
                start_ratio=start,
                stop_ratio=stop,
            ),
            1.0,
        )
        self.assertAlmostEqual(
            _bandlimit_gain(
                SAMPLE_RATE * (start + stop) * 0.5,
                SAMPLE_RATE,
                start_ratio=start,
                stop_ratio=stop,
            ),
            0.5,
        )
        self.assertAlmostEqual(
            _bandlimit_gain(
                SAMPLE_RATE * stop,
                SAMPLE_RATE,
                start_ratio=start,
                stop_ratio=stop,
            ),
            0.0,
        )

        # These are the two formerly abrupt chromatic boundaries.
        for ratio, before, after in ((16.2, 88, 89), (8.93, 98, 99)):
            frequency_before = (
                440.0 * 2.0 ** ((before - 69.0) / 12.0) * ratio
            )
            frequency_after = (
                440.0 * 2.0 ** ((after - 69.0) / 12.0) * ratio
            )
            gain_before = _bandlimit_gain(
                frequency_before,
                SAMPLE_RATE,
                start_ratio=start,
                stop_ratio=stop,
            )
            gain_after = _bandlimit_gain(
                frequency_after,
                SAMPLE_RATE,
                start_ratio=start,
                stop_ratio=stop,
            )
            with self.subTest(ratio=ratio):
                self.assertLess(gain_before - gain_after, 0.03)

        audible, noise = _modal_noise_only(
            MUSIC_BOX_MANIFEST,
            72,
            seconds=0.08,
        )
        attack_frames = round(0.0025 * SAMPLE_RATE)
        self.assertAlmostEqual(float(audible[0]), 0.0, places=14)
        self.assertLess(float(np.max(np.abs(noise[:attack_frames]))), 0.01)
        self.assertLess(float(np.max(np.abs(noise[attack_frames:]))), 1.0e-12)

    def test_pitched_modal_profiles_keep_fundamental_tuning(self) -> None:
        for name, path in (
            ("steelpan", STEELPAN_MANIFEST),
            ("music_box", MUSIC_BOX_MANIFEST),
        ):
            manifest = _load(path)
            for midi in _probe_notes(manifest):
                signal = _render_note(
                    manifest,
                    path.parent,
                    midi,
                    seconds=1.2,
                    velocity=0.8,
                )
                error = _pitch_error_cents(signal, midi)
                with self.subTest(profile=name, midi=midi):
                    self.assertLess(abs(error), 0.5)


class TaikoIdentityTests(unittest.TestCase):
    def test_three_strokes_share_body_but_remain_distinct_and_balanced(self) -> None:
        manifest = _load(TAIKO_MANIFEST)
        rendered = {
            midi: _render_note(
                manifest,
                TAIKO_MANIFEST.parent,
                midi,
                seconds=0.5,
            )
            for midi in (60, 61, 62)
        }
        for midi, signal in rendered.items():
            with self.subTest(midi=midi):
                self.assertAlmostEqual(float(signal[0]), 0.0, places=14)
                self.assertTrue(
                    np.array_equal(
                        signal,
                        _render_note(
                            manifest,
                            TAIKO_MANIFEST.parent,
                            midi,
                            seconds=0.5,
                        ),
                    )
                )

        spectra: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        centroids: dict[int, float] = {}
        rms: dict[int, float] = {}
        low_body_fraction: dict[int, float] = {}
        for midi, signal in rendered.items():
            windowed = signal * np.hanning(len(signal))
            power = np.abs(np.fft.rfft(windowed)) ** 2
            frequencies = np.fft.rfftfreq(len(signal), 1.0 / SAMPLE_RATE)
            spectra[midi] = (frequencies, power)
            centroids[midi] = float(
                np.sum(frequencies * power) / np.sum(power)
            )
            rms[midi] = float(np.sqrt(np.mean(signal * signal)))
            low = (frequencies >= 50.0) & (frequencies <= 460.0)
            low_body_fraction[midi] = float(
                np.sum(power[low]) / np.sum(power)
            )

        self.assertLess(centroids[60], centroids[61])
        self.assertLess(centroids[61], centroids[62])
        self.assertGreater(low_body_fraction[62], 0.3)
        self.assertLess(max(rms.values()) / min(rms.values()), 3.0)

        normalized_low_spectra = {}
        for midi, (frequencies, power) in spectra.items():
            low = (frequencies >= 50.0) & (frequencies <= 460.0)
            feature = np.log1p(np.sqrt(power[low]))
            normalized_low_spectra[midi] = (
                feature - np.mean(feature)
            ) / (np.std(feature) + 1.0e-12)
        self.assertGreater(
            float(
                np.corrcoef(
                    normalized_low_spectra[60],
                    normalized_low_spectra[61],
                )[0, 1]
            ),
            0.9,
        )
        for rim_key in (60, 61):
            self.assertGreater(
                float(
                    np.corrcoef(
                        normalized_low_spectra[rim_key],
                        normalized_low_spectra[62],
                    )[0, 1]
                ),
                0.4,
            )

    def test_three_full_velocity_strokes_keep_emergency_headroom(self) -> None:
        manifest = _load(TAIKO_MANIFEST)
        instrument = ModeledInstrument(
            SAMPLE_RATE,
            manifest,
            str(TAIKO_MANIFEST.parent),
        )
        for sequence, midi in enumerate((60, 61, 62), start=1):
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    sequence,
                    "note_on",
                    {
                        "note_id": sequence,
                        "midi_note": midi,
                        "velocity": 1.0,
                    },
                ),
                TUNING,
            )
        signal = np.fromiter(
            (
                instrument.render_frame()[0]
                for _ in range(round(0.5 * SAMPLE_RATE))
            ),
            dtype=np.float64,
        )
        self.assertLess(float(np.max(np.abs(signal))), 0.98)


if __name__ == "__main__":
    unittest.main()
