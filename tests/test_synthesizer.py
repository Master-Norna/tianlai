from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

from tianlai.events import PerformanceEvent, parse_performance_document
from tianlai.instrument import create_instrument
from tianlai.renderer import render_to_wav
from tianlai.synthesizer import (
    ENGINE_VERSION,
    PATCH_PROFILES,
    SynthesizerInstrument,
    _pulse,
    _saw,
)
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
SYNTH_ROOT = ROOT / "乐器" / "电子乐器"
INSTRUMENTS = {
    "光环铺底": "halo_pad",
    "合唱铺底": "choir_pad",
    "合成器低音": "synth_bass",
    "合成器铺底": "broad_pad",
    "合成器主音": "synth_lead",
    "合成铜管": "synth_brass",
    "合成弦乐": "synth_strings",
    "金属铺底": "metallic_pad",
    "扫频铺底": "sweep_pad",
    "温暖铺底": "warm_pad",
}


def _manifest(name: str) -> dict[str, object]:
    return json.loads((SYNTH_ROOT / name / "乐器.json").read_text(encoding="utf-8"))


def _create(name: str, sample_rate: int = 16_000) -> SynthesizerInstrument:
    path = SYNTH_ROOT / name / "乐器.json"
    instrument = create_instrument(
        _manifest(name), sample_rate, base_directory=str(path.parent)
    )
    assert isinstance(instrument, SynthesizerInstrument)
    return instrument


def _note_on(note_id: int, note: float, velocity: float = 0.8) -> PerformanceEvent:
    return PerformanceEvent(
        0,
        note_id,
        "note_on",
        {"note_id": note_id, "midi_note": note, "velocity": velocity},
    )


class SynthesizerInstrumentTests(unittest.TestCase):
    def test_all_ten_candidates_use_distinct_versioned_topologies(self) -> None:
        self.assertEqual(set(PATCH_PROFILES), set(INSTRUMENTS.values()))
        topologies: set[str] = set()
        seeds: set[int] = set()
        for name, expected_patch in INSTRUMENTS.items():
            manifest = _manifest(name)
            self.assertEqual(manifest["name"], name)
            self.assertEqual(manifest["type"], "synthesizer")
            self.assertEqual(manifest["patch"], expected_patch)
            self.assertEqual(manifest["engine_version"], ENGINE_VERSION)
            self.assertEqual(manifest["quality_tier"], "formal")
            self.assertEqual(
                manifest["collaboration_review_status"], "untested"
            )
            self.assertNotIn("manual_review", manifest)
            self.assertNotIn("soundfont", manifest)
            topologies.add(PATCH_PROFILES[expected_patch].oscillator)
            seeds.add(int(manifest["seed"]))
            self.assertIsInstance(_create(name), SynthesizerInstrument)
        self.assertEqual(len(topologies), 10)
        self.assertEqual(len(seeds), 10)

        # The ten entries above are inherently synthesized instruments.  The
        # one-shot production effect remains outside that DSP batch and now
        # has its own real VPO section/percussion implementation.
        impact = json.loads(
            (SYNTH_ROOT / "管弦重击" / "乐器.json").read_text(encoding="utf-8")
        )
        self.assertEqual(impact["type"], "vpo_orchestral_hit")
        self.assertEqual(impact["quality_tier"], "formal")
        self.assertEqual(impact["fallback_policy"], "explicit_only_no_silent_gm")
        self.assertNotIn("soundfont", impact)

    def test_a4_microtonal_and_direct_hz_drive_exact_carrier_frequency(self) -> None:
        manifest = copy.deepcopy(_manifest("合成器主音"))
        manifest["parameters"] = {
            "unison_voices": 1,
            "detune_cents": 0.0,
            "vibrato_cents": 0.0,
            "filter_lfo_octaves": 0.0,
        }
        instrument = SynthesizerInstrument.from_manifest(manifest, 48_000)
        tuning = EqualTemperament(432.0)
        instrument.handle_event(_note_on(1, 69.5), tuning)
        expected = 432.0 * 2.0 ** (0.5 / 12.0)
        voice = instrument.voices[1]
        self.assertAlmostEqual(voice.frequency, expected, places=12)
        phase_before = voice.phases[0]
        instrument.render_frame()
        phase_increment = (voice.phases[0] - phase_before) % 1.0
        self.assertAlmostEqual(phase_increment, expected / 48_000.0, places=12)

        direct = PerformanceEvent(
            0,
            2,
            "note_on",
            {"note_id": 2, "pitch_hz": 445.25, "velocity": 0.7},
        )
        instrument.handle_event(direct, tuning)
        self.assertEqual(instrument.voices[2].frequency, 445.25)

    def test_every_manifest_enforces_its_hard_range(self) -> None:
        tuning = EqualTemperament()
        for name in INSTRUMENTS:
            manifest = _manifest(name)
            minimum = float(manifest["note_min"])
            maximum = float(manifest["note_max"])
            instrument = _create(name, 48_000)
            with self.assertRaisesRegex(ValueError, "outside calibrated range"):
                instrument.handle_event(_note_on(1, minimum - 0.01), tuning)
            with self.assertRaisesRegex(ValueError, "outside calibrated range"):
                instrument.handle_event(_note_on(2, maximum + 0.01), tuning)
            instrument.handle_event(_note_on(3, minimum), tuning)
            instrument.handle_event(_note_on(4, maximum), tuning)
            left, right = instrument.render_frame()
            self.assertTrue(math.isfinite(left) and math.isfinite(right))

    def test_polyblep_saw_and_pulse_are_continuous_at_wrap(self) -> None:
        phase_step = 0.1
        epsilon = 0.001
        self.assertLess(
            abs(_saw(1.0 - epsilon, phase_step) - _saw(epsilon, phase_step)),
            0.05,
        )
        self.assertLess(
            abs(
                _pulse(1.0 - epsilon, phase_step, 0.4)
                - _pulse(epsilon, phase_step, 0.4)
            ),
            0.05,
        )

    def test_velocity_expression_modulation_pedal_and_release(self) -> None:
        manifest = copy.deepcopy(_manifest("合成器主音"))
        manifest["parameters"] = {
            "attack_seconds": 0.001,
            "decay_seconds": 0.001,
            "sustain_level": 1.0,
            "release_seconds": 0.01,
            "unison_voices": 1,
            "detune_cents": 0.0,
        }
        tuning = EqualTemperament()
        low = SynthesizerInstrument.from_manifest(manifest, 16_000)
        high = SynthesizerInstrument.from_manifest(manifest, 16_000)
        low.handle_event(_note_on(1, 60, 0.25), tuning)
        high.handle_event(_note_on(1, 60, 0.9), tuning)
        low_energy = 0.0
        high_energy = 0.0
        for _ in range(800):
            low_frame = low.render_frame()
            high_frame = high.render_frame()
            low_energy += low_frame[0] ** 2 + low_frame[1] ** 2
            high_energy += high_frame[0] ** 2 + high_frame[1] ** 2
        self.assertGreater(high_energy, low_energy * 5.0)

        high.handle_event(
            PerformanceEvent(0, 2, "control", {"name": "expression", "value": 0.35}),
            tuning,
        )
        high.handle_event(
            PerformanceEvent(0, 3, "control", {"name": "modulation", "value": 0.9}),
            tuning,
        )
        for _ in range(500):
            high.render_frame()
        self.assertAlmostEqual(high.expression, 0.35, delta=0.03)
        self.assertAlmostEqual(high.modulation, 0.9, delta=0.04)

        high.handle_event(
            PerformanceEvent(0, 4, "control", {"name": "sustain_pedal", "value": 1.0}),
            tuning,
        )
        high.handle_event(
            PerformanceEvent(0, 5, "note_off", {"note_id": 1}), tuning
        )
        self.assertTrue(high.voices[1].pending_release)
        for _ in range(300):
            high.render_frame()
        self.assertEqual(high.active_voice_count, 1)
        high.handle_event(
            PerformanceEvent(0, 6, "control", {"name": "sustain_pedal", "value": 0.0}),
            tuning,
        )
        for _ in range(161):
            high.render_frame()
        self.assertEqual(high.active_voice_count, 0)

    def _render_signature(
        self, name: str
    ) -> tuple[str, float, np.ndarray, tuple[float, ...]]:
        instrument = _create(name)
        tuning = EqualTemperament()
        note = 60.0
        instrument.handle_event(_note_on(1, note, 0.82), tuning)
        digest = hashlib.sha256()
        mono: list[float] = []
        peak = 0.0
        for frame_index in range(10_240):
            if frame_index == 4_000:
                instrument.handle_event(
                    PerformanceEvent(
                        frame_index,
                        2,
                        "control",
                        {"name": "modulation", "value": 0.75},
                    ),
                    tuning,
                )
            left, right = instrument.render_frame()
            self.assertTrue(math.isfinite(left) and math.isfinite(right))
            peak = max(peak, abs(left), abs(right))
            digest.update(struct.pack("<ff", left, right))
            if frame_index >= 2_048:
                mono.append((left + right) * 0.5)
        array = np.asarray(mono, dtype=np.float64)
        spectrum = np.abs(np.fft.rfft(array * np.hanning(len(array))))
        spectrum /= max(float(np.linalg.norm(spectrum)), 1.0e-15)
        return digest.hexdigest(), peak, spectrum, tuple(mono)

    def test_all_patches_are_deterministic_distinct_audible_and_unclipped(self) -> None:
        first: dict[str, tuple[str, float, np.ndarray, tuple[float, ...]]] = {}
        for name in INSTRUMENTS:
            first[name] = self._render_signature(name)
            repeat = self._render_signature(name)
            self.assertEqual(first[name][0], repeat[0])
            self.assertEqual(first[name][1], repeat[1])
            self.assertGreater(first[name][1], 1.0e-5)
            self.assertLess(first[name][1], 1.0)
        self.assertEqual(len({result[0] for result in first.values()}), 10)

        names = list(INSTRUMENTS)
        for left_index, left_name in enumerate(names):
            for right_name in names[left_index + 1 :]:
                distance = float(
                    np.linalg.norm(first[left_name][2] - first[right_name][2])
                )
                self.assertGreater(
                    distance,
                    0.015,
                    f"{left_name} and {right_name} are spectrally indistinct",
                )

    def test_resonant_filter_remains_finite_under_polyphony(self) -> None:
        manifest = copy.deepcopy(_manifest("扫频铺底"))
        manifest["parameters"] = {
            "resonance": 0.97,
            "attack_seconds": 0.002,
            "filter_lfo_octaves": 4.0,
        }
        instrument = SynthesizerInstrument.from_manifest(manifest, 16_000)
        tuning = EqualTemperament()
        for note_id, note in enumerate((36, 43, 48, 55, 60), start=1):
            instrument.handle_event(_note_on(note_id, note, 1.0), tuning)
        instrument.handle_event(
            PerformanceEvent(0, 10, "control", {"name": "modulation", "value": 1.0}),
            tuning,
        )
        peak = 0.0
        for _ in range(12_000):
            frame = instrument.render_frame()
            self.assertTrue(all(math.isfinite(value) for value in frame))
            peak = max(peak, *(abs(value) for value in frame))
        self.assertGreater(peak, 1.0e-5)
        self.assertLess(peak, 1.0)

    def test_examples_and_schema_cover_all_ten_patches(self) -> None:
        for name in INSTRUMENTS:
            example = ROOT / "examples" / f"{name}_程序合成.events.json"
            document = parse_performance_document(
                json.loads(example.read_text(encoding="utf-8"))
            )
            self.assertGreater(len(document.events), 2)

        schema = json.loads(
            (ROOT / "schemas" / "instrument.schema.json").read_text(encoding="utf-8")
        )
        synth_branches = [
            branch
            for branch in schema["oneOf"]
            if branch.get("properties", {}).get("type", {}).get("const") == "synthesizer"
        ]
        self.assertEqual(len(synth_branches), 1)
        self.assertEqual(
            set(synth_branches[0]["properties"]["patch"]["enum"]),
            set(PATCH_PROFILES),
        )

    def test_windows_chinese_path_end_to_end_wav_render(self) -> None:
        performance = {
            "sample_rate": 16_000,
            "duration_seconds": 0.16,
            "events": [
                {
                    "time": 0.0,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60.25,
                    "velocity": 0.8,
                },
                {
                    "time": 0.08,
                    "type": "control",
                    "name": "modulation",
                    "value": 0.7,
                },
                {"time": 0.09, "type": "note_off", "note_id": 1},
            ],
        }
        with tempfile.TemporaryDirectory(prefix="天籁合成器_") as temporary_directory:
            temporary = Path(temporary_directory)
            events_path = temporary / "微分音事件.json"
            events_path.write_text(
                json.dumps(performance, ensure_ascii=False), encoding="utf-8"
            )
            for name in INSTRUMENTS:
                output = temporary / f"{name}_试听.wav"
                result = render_to_wav(
                    SYNTH_ROOT / name / "乐器.json", events_path, output
                )
                self.assertEqual(result.frame_count, 2_560)
                self.assertGreater(result.peak_active_voices, 0)
                with wave.open(str(output), "rb") as audio:
                    self.assertEqual(audio.getnchannels(), 2)
                    self.assertEqual(audio.getsampwidth(), 3)
                    self.assertEqual(audio.getframerate(), 16_000)
                    self.assertEqual(audio.getnframes(), 2_560)

    def test_dense_production_render_keeps_the_established_frame_path(
        self,
    ) -> None:
        performance = {
            "sample_rate": 8_000,
            "duration_seconds": 0.12,
            "events": [
                {
                    "time": 0.0,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                    "velocity": 0.8,
                },
                {"time": 0.1, "type": "note_off", "note_id": 1},
            ],
        }
        with tempfile.TemporaryDirectory(prefix="天籁密集合成器_") as raw:
            temporary = Path(raw)
            events_path = temporary / "持续发声.events.json"
            events_path.write_text(
                json.dumps(performance, ensure_ascii=False),
                encoding="utf-8",
            )
            with patch(
                "tianlai.renderer.render_document_blocks",
                side_effect=AssertionError("dense synth entered block path"),
            ):
                result = render_to_wav(
                    SYNTH_ROOT / "温暖铺底" / "乐器.json",
                    events_path,
                    temporary / "持续发声.wav",
                )
        self.assertEqual(result.frame_count, 960)
        self.assertGreater(result.peak_active_voices, 0)

    def test_resource_evidence_tracks_the_current_synth_engine(self) -> None:
        engine_sha256 = hashlib.sha256(
            (ROOT / "tianlai" / "synthesizer.py").read_bytes()
        ).hexdigest().upper()
        for name in INSTRUMENTS:
            with self.subTest(instrument=name):
                report = json.loads(
                    (SYNTH_ROOT / name / "资源核验.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(report["engine_sha256"], engine_sha256)


if __name__ == "__main__":
    unittest.main()
