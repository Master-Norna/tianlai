"""Signal and contract tests for the built-in procedural bianzhong backend."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

from jsonschema import Draft202012Validator
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.events import PerformanceEvent
from tianlai.audition_protocol import build_full_range_audition
from tianlai.bianzhong import (
    BianzhongInstrument,
    ENGINE_VERSION,
    generate_pitch_calibration,
    generate_resource_verification,
)
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


DIRECTORY = ROOT / "乐器" / "世界乐器" / "编钟"
ENGINE_SOURCE = ROOT / "tianlai" / "bianzhong.py"
COMPATIBILITY_WRAPPER = DIRECTORY / "乐器.py"
MANIFEST_PATH = DIRECTORY / "乐器.json"
SCHEMA_PATH = ROOT / "schemas" / "instrument.schema.json"
TUNING = EqualTemperament(440.0)

def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _event(
    event_type: str,
    sequence: int = 0,
    **payload: object,
) -> PerformanceEvent:
    return PerformanceEvent(0, sequence, event_type, dict(payload))


def _instrument(
    *,
    sample_rate: int = 48_000,
    manifest: dict | None = None,
) -> BianzhongInstrument:
    return BianzhongInstrument(
        sample_rate,
        manifest or _manifest(),
        str(DIRECTORY),
    )


def _strike(
    instrument: BianzhongInstrument,
    midi_note: float,
    *,
    velocity: float = 0.8,
    note_id: int = 1,
    articulation: str | None = None,
) -> None:
    if articulation is not None:
        instrument.handle_event(
            _event("articulation", name=articulation),
            TUNING,
        )
    instrument.handle_event(
        _event(
            "note_on",
            1,
            note_id=note_id,
            midi_note=midi_note,
            velocity=velocity,
        ),
        TUNING,
    )


def _render(
    instrument: BianzhongInstrument,
    frames: int,
) -> np.ndarray:
    result = np.empty((frames, 2), dtype=np.float64)
    for index in range(frames):
        result[index] = instrument.render_frame()
    return result


def _render_note(
    midi_note: int,
    *,
    seconds: float,
    velocity: float = 0.8,
    articulation: str = "zhenggu",
    sample_rate: int = 48_000,
    manifest: dict | None = None,
) -> np.ndarray:
    instrument = _instrument(sample_rate=sample_rate, manifest=manifest)
    _strike(
        instrument,
        midi_note,
        velocity=velocity,
        articulation=articulation,
    )
    return _render(instrument, round(seconds * sample_rate))


def _fft_pitch(signal: np.ndarray, midi_note: int, sample_rate: int) -> float:
    mono = np.mean(signal, axis=1)
    start = round(0.30 * sample_rate)
    segment = mono[start:] - np.mean(mono[start:])
    nfft = 1
    while nfft < len(segment) * 8:
        nfft *= 2
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment)), n=nfft))
    frequencies = np.fft.rfftfreq(nfft, 1.0 / sample_rate)
    expected = 440.0 * 2.0 ** ((midi_note - 69.0) / 12.0)
    ratio = 2.0 ** (80.0 / 1200.0)
    bins = np.flatnonzero(
        (frequencies >= expected / ratio)
        & (frequencies <= expected * ratio)
    )
    peak = int(bins[np.argmax(spectrum[bins])])
    delta = 0.0
    if 0 < peak < len(spectrum) - 1:
        left, center, right = np.log(spectrum[peak - 1 : peak + 2] + 1.0e-24)
        denominator = left - 2.0 * center + right
        if denominator != 0.0:
            delta = float(0.5 * (left - right) / denominator)
    measured = (peak + delta) * sample_rate / nfft
    return 1200.0 * math.log2(measured / expected)


def _spectral_centroid(signal: np.ndarray, sample_rate: int) -> float:
    mono = np.mean(signal, axis=1)
    windowed = mono * np.hanning(len(mono))
    power = np.abs(np.fft.rfft(windowed)) ** 2
    frequencies = np.fft.rfftfreq(len(mono), 1.0 / sample_rate)
    return float(np.sum(frequencies * power) / np.sum(power))


class BianzhongManifestTests(unittest.TestCase):
    def test_manifest_is_formal_after_listening_and_schema_valid(self) -> None:
        manifest = _manifest()
        self.assertEqual(manifest["type"], "modeled_bianzhong")
        self.assertNotIn("implementation", manifest)
        self.assertEqual(manifest["engine_version"], ENGINE_VERSION)
        self.assertEqual(manifest["quality_tier"], "formal")
        self.assertEqual(manifest["collaboration_review_status"], "untested")
        self.assertEqual(manifest["fallback_policy"], "explicit_only_no_silent_gm")
        self.assertEqual((manifest["note_min"], manifest["note_max"]), (36, 98))
        self.assertEqual(
            manifest["allowed_articulations"],
            ["zhenggu", "cegu"],
        )
        self.assertEqual(manifest["external_assets"], [])

        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        self.assertFalse(
            list(validator.iter_errors(manifest)),
            "the shipped bianzhong manifest must satisfy its strict schema branch",
        )
        candidate = copy.deepcopy(manifest)
        candidate["quality_tier"] = "candidate"
        candidate["upgrade_status"] = "first_candidate_deterministic_modal_model"
        self.assertFalse(
            list(validator.iter_errors(candidate)),
            "schema must retain the candidate state for future model revisions",
        )

        for field, invalid in (
            ("engine_version", "1.0.1"),
            ("note_max", 96),
            ("implementation", "other.py"),
            ("supported_controls", ["expression"]),
        ):
            with self.subTest(field=field):
                broken = copy.deepcopy(manifest)
                broken[field] = invalid
                self.assertTrue(list(validator.iter_errors(broken)))

    def test_chinese_path_factory_route_loads_builtin_backend(self) -> None:
        instrument = create_instrument(
            _manifest(),
            48_000,
            base_directory=str(DIRECTORY),
        )
        self.assertEqual(type(instrument).__name__, "BianzhongInstrument")
        _strike(instrument, 60)
        signal = _render(instrument, 2_000)
        self.assertGreater(float(np.max(np.abs(signal))), 0.01)
        provenance = instrument._tianlai_factory_provenance
        self.assertIsNotNone(provenance)
        self.assertEqual(
            provenance["factory_route"],
            "builtin_manifest_dispatch_no_implementation",
        )

    def test_directory_wrapper_accepts_current_and_historical_manifests(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "tianlai_test_bianzhong_compatibility_wrapper",
            COMPATIBILITY_WRAPPER,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        current = _manifest()
        current.pop("implementation", None)
        historical = dict(current, implementation="乐器.py")
        for manifest in (current, historical):
            with self.subTest(historical="implementation" in manifest):
                instrument = module.create(
                    manifest=manifest,
                    sample_rate=8_000,
                    base_directory=str(DIRECTORY),
                )
                self.assertIsInstance(instrument, BianzhongInstrument)

    def test_manifest_version_range_and_declarations_fail_closed(self) -> None:
        mutations = (
            ("type", "modeled_instrument"),
            ("implementation", "中央.py"),
            ("engine_version", "0.9.0"),
            ("fallback_policy", "silent_gm"),
            ("note_min", 35),
            ("note_max", 96),
            ("allowed_articulations", ["cegu", "zhenggu"]),
            ("supported_controls", ["expression", "brightness"]),
            ("gain", 0.151),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                manifest = _manifest()
                manifest[field] = value
                with self.assertRaises(ValueError):
                    _instrument(manifest=manifest)

    def test_standard_full_range_recipe_keeps_a_natural_final_tail(self) -> None:
        plan = build_full_range_audition(
            MANIFEST_PATH,
            instrument_root=ROOT / "乐器",
        )
        self.assertEqual(plan.keys, tuple(range(36, 99)))
        self.assertEqual(plan.tail_seconds, 6.0)
        self.assertEqual(plan.document["tail_seconds"], 6.0)
        self.assertIn("自然落静", plan.exception or "")


class BianzhongDeterminismAndPitchTests(unittest.TestCase):
    def test_same_seed_is_bit_deterministic_and_seed_changes_colour(self) -> None:
        first = _render_note(62, seconds=0.32, velocity=0.73)
        second = _render_note(62, seconds=0.32, velocity=0.73)
        self.assertTrue(np.array_equal(first, second))

        changed = _manifest()
        changed["seed"] += 1
        third = _render_note(
            62,
            seconds=0.32,
            velocity=0.73,
            manifest=changed,
        )
        self.assertFalse(np.array_equal(first, third))
        self.assertGreater(float(np.max(np.abs(first - third))), 1.0e-5)

    def test_both_articulations_keep_requested_fundamental(self) -> None:
        for articulation in ("zhenggu", "cegu"):
            signal = _render_note(
                60,
                seconds=1.55,
                velocity=0.82,
                articulation=articulation,
            )
            error = _fft_pitch(signal, 60, 48_000)
            with self.subTest(articulation=articulation):
                self.assertLess(abs(error), 0.35)

    def test_articulations_are_distinct_without_hidden_transposition(self) -> None:
        zhenggu = _render_note(67, seconds=0.75, articulation="zhenggu")
        cegu = _render_note(67, seconds=0.75, articulation="cegu")
        z_mono = np.mean(zhenggu, axis=1)
        c_mono = np.mean(cegu, axis=1)
        correlation = float(np.corrcoef(z_mono, c_mono)[0, 1])
        self.assertLess(correlation, 0.92)
        self.assertGreater(
            abs(
                _spectral_centroid(zhenggu, 48_000)
                - _spectral_centroid(cegu, 48_000)
            ),
            40.0,
        )
        self.assertLess(abs(_fft_pitch(zhenggu, 67, 48_000)), 0.5)
        self.assertLess(abs(_fft_pitch(cegu, 67, 48_000)), 0.5)

    def test_articulation_switch_affects_only_future_strikes(self) -> None:
        switched = _instrument()
        reference = _instrument()
        _strike(switched, 64, articulation="zhenggu")
        _strike(reference, 64, articulation="zhenggu")
        before_switched = _render(switched, 4_000)
        before_reference = _render(reference, 4_000)
        self.assertTrue(np.array_equal(before_switched, before_reference))

        switched.handle_event(
            _event("articulation", 2, name="cegu"),
            TUNING,
        )
        after_switched = _render(switched, 4_000)
        after_reference = _render(reference, 4_000)
        self.assertTrue(
            np.array_equal(after_switched, after_reference),
            "changing articulation must not mutate an already ringing voice",
        )

        _strike(switched, 69, note_id=2)
        _strike(reference, 69, note_id=2)
        self.assertFalse(
            np.array_equal(
                _render(switched, 4_000),
                _render(reference, 4_000),
            )
        )


class BianzhongDynamicsAndLifetimeTests(unittest.TestCase):
    def test_velocity_changes_level_and_brightness(self) -> None:
        quiet = _render_note(60, seconds=0.45, velocity=0.22)
        loud = _render_note(60, seconds=0.45, velocity=1.0)
        quiet_rms = float(np.sqrt(np.mean(quiet**2)))
        loud_rms = float(np.sqrt(np.mean(loud**2)))
        self.assertGreater(loud_rms, quiet_rms * 3.0)
        attack = slice(round(0.005 * 48_000), round(0.18 * 48_000))
        self.assertGreater(
            _spectral_centroid(loud[attack], 48_000),
            _spectral_centroid(quiet[attack], 48_000) * 1.08,
        )

    def test_note_off_does_not_truncate_one_shot_and_id_can_be_reused(self) -> None:
        released = _instrument()
        natural = _instrument()
        _strike(released, 86, note_id=7)
        _strike(natural, 86, note_id=7)
        self.assertTrue(
            np.array_equal(_render(released, 2_000), _render(natural, 2_000))
        )
        released.handle_event(_event("note_off", 2, note_id=7), TUNING)
        after_release = _render(released, 8_000)
        natural_tail = _render(natural, 8_000)
        self.assertTrue(np.array_equal(after_release, natural_tail))
        self.assertEqual(released.active_voice_count, 1)

        _strike(released, 90, note_id=7)
        self.assertEqual(released.active_voice_count, 2)
        self.assertGreater(float(np.max(np.abs(_render(released, 2_000)))), 0.01)

    def test_old_tail_completion_cannot_release_a_reused_note_id(self) -> None:
        instrument = _instrument()
        _strike(instrument, 98, note_id=11)
        old_voice = instrument._voices[0]
        old_voice.maximum_samples = 120
        instrument.handle_event(_event("note_off", 2, note_id=11), TUNING)
        _strike(instrument, 60, note_id=11)
        _render(instrument, 130)
        self.assertEqual(instrument.active_voice_count, 1)
        with self.assertRaises(ValueError):
            _strike(instrument, 62, note_id=11)

    def test_nearly_silent_active_tails_do_not_pull_down_a_new_attack(self) -> None:
        with_tails = _instrument()
        for note_id, midi_note in enumerate(range(70, 78), 1):
            _strike(with_tails, midi_note, note_id=note_id)
        for voice in with_tails._voices:
            voice.age = 10_000
            for mode in voice.modes:
                mode.envelope = 1.0e-9

        clean = _instrument()
        _strike(with_tails, 60, note_id=100)
        _strike(clean, 60, note_id=100)
        crowded_attack = _render(with_tails, 3_000)
        clean_attack = _render(clean, 3_000)
        crowded_peak = float(np.max(np.abs(crowded_attack)))
        clean_peak = float(np.max(np.abs(clean_attack)))
        self.assertAlmostEqual(crowded_peak / clean_peak, 1.0, delta=0.002)

    def test_high_note_ends_naturally_and_low_tail_is_longer(self) -> None:
        high_instrument = _instrument()
        _strike(high_instrument, 98, velocity=0.8)
        frames = round(6.6 * 48_000)
        high_signal = _render(high_instrument, frames)
        self.assertEqual(high_instrument.active_voice_count, 0)
        self.assertAlmostEqual(float(high_signal[-1, 0]), 0.0, places=14)
        self.assertAlmostEqual(float(high_signal[-1, 1]), 0.0, places=14)

        low = _render_note(36, seconds=4.2, velocity=0.8)
        high = _render_note(98, seconds=4.2, velocity=0.8)
        tail = round(0.20 * 48_000)
        low_tail_rms = float(np.sqrt(np.mean(low[-tail:] ** 2)))
        high_tail_rms = float(np.sqrt(np.mean(high[-tail:] ** 2)))
        self.assertGreater(low_tail_rms, high_tail_rms * 20.0)

    def test_expression_and_modulation_are_smoothed(self) -> None:
        controlled = _instrument()
        reference = _instrument()
        _strike(controlled, 60)
        _strike(reference, 60)
        self.assertTrue(
            np.array_equal(_render(controlled, 4_000), _render(reference, 4_000))
        )
        controlled.handle_event(
            _event("control", 2, name="expression", value=0.0),
            TUNING,
        )
        first_controlled = controlled.render_frame()
        first_reference = reference.render_frame()
        self.assertGreater(abs(first_controlled[0]), abs(first_reference[0]) * 0.95)
        controlled_tail = _render(controlled, 7_000)
        reference_tail = _render(reference, 7_000)
        self.assertLess(
            float(np.sqrt(np.mean(controlled_tail[-1_000:] ** 2))),
            float(np.sqrt(np.mean(reference_tail[-1_000:] ** 2))) * 0.04,
        )

        modulated = _instrument()
        plain = _instrument()
        _strike(modulated, 64)
        _strike(plain, 64)
        _render(modulated, 4_000)
        _render(plain, 4_000)
        modulated.handle_event(
            _event("control", 2, name="modulation", value=1.0),
            TUNING,
        )
        first_modulated = modulated.render_frame()
        first_plain = plain.render_frame()
        self.assertLess(abs(first_modulated[0] - first_plain[0]), 0.002)
        modulated_tail = _render(modulated, 8_000)
        plain_tail = _render(plain, 8_000)
        self.assertFalse(np.array_equal(modulated_tail, plain_tail))
        self.assertGreater(
            _spectral_centroid(modulated_tail, 48_000),
            _spectral_centroid(plain_tail, 48_000),
        )


class BianzhongSafetyTests(unittest.TestCase):
    def test_polyphony_is_finite_clip_free_dc_safe_and_narrow_stereo(self) -> None:
        instrument = _instrument()
        for note_id, midi_note in enumerate((48, 53, 57, 60, 64, 67, 72, 76), 1):
            _strike(
                instrument,
                midi_note,
                velocity=1.0,
                note_id=note_id,
                articulation="zhenggu" if note_id == 1 else None,
            )
        signal = _render(instrument, round(0.65 * 48_000))
        self.assertTrue(np.all(np.isfinite(signal)))
        self.assertLess(float(np.max(np.abs(signal))), 0.98)
        steady = signal[round(0.05 * 48_000) :]
        self.assertLess(float(np.max(np.abs(np.mean(steady, axis=0)))), 0.002)
        self.assertGreater(
            float(np.corrcoef(steady[:, 0], steady[:, 1])[0, 1]),
            0.90,
        )
        self.assertEqual(instrument.active_voice_count, 8)

    def test_44100_and_48000_high_range_are_bandlimited_and_smooth(self) -> None:
        for sample_rate in (44_100, 48_000):
            signal = _render_note(
                98,
                seconds=0.55,
                velocity=1.0,
                articulation="cegu",
                sample_rate=sample_rate,
            )
            mono = np.mean(signal, axis=1)
            self.assertTrue(np.all(np.isfinite(mono)))
            self.assertAlmostEqual(float(mono[0]), 0.0, places=14)
            self.assertLess(float(np.max(np.abs(signal))), 0.5)

            spectrum = np.abs(np.fft.rfft(mono * np.hanning(len(mono)))) ** 2
            frequencies = np.fft.rfftfreq(len(mono), 1.0 / sample_rate)
            forbidden = frequencies >= sample_rate * 0.46
            fraction = float(np.sum(spectrum[forbidden]) / np.sum(spectrum))
            with self.subTest(sample_rate=sample_rate):
                self.assertLess(fraction, 1.0e-7)

    def test_invalid_events_fail_closed(self) -> None:
        instrument = _instrument()
        _strike(instrument, 60, note_id=9)
        with self.assertRaises(ValueError):
            _strike(instrument, 61, note_id=9)
        for midi_note in (35.999, 98.001):
            with self.subTest(midi_note=midi_note):
                with self.assertRaises(ValueError):
                    _strike(instrument, midi_note, note_id=20)
        with self.assertRaises(ValueError):
            instrument.handle_event(
                _event("articulation", name="rim"),
                TUNING,
            )
        with self.assertRaises(ValueError):
            instrument.handle_event(
                _event("control", name="reverb", value=0.5),
                TUNING,
            )
        with self.assertRaises(ValueError):
            instrument.handle_event(
                _event("control", name="expression", value=float("nan")),
                TUNING,
            )
        with self.assertRaises(ValueError):
            instrument.handle_event(_event("aftertouch", value=0.5), TUNING)


class BianzhongEvidenceTests(unittest.TestCase):
    def test_resource_and_pitch_generators_write_hash_locked_json(self) -> None:
        engine_hash = hashlib.sha256(ENGINE_SOURCE.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as temporary:
            resource_path = Path(temporary) / "resource.json"
            pitch_path = Path(temporary) / "pitch.json"
            resource = generate_resource_verification(
                MANIFEST_PATH,
                output_path=resource_path,
            )
            pitch = generate_pitch_calibration(
                MANIFEST_PATH,
                output_path=pitch_path,
                probe_notes=(36, 67, 98),
            )
            self.assertTrue(resource_path.is_file())
            self.assertTrue(pitch_path.is_file())
        self.assertEqual(resource["engine_sha256"], engine_hash)
        self.assertEqual(resource["external_assets"], [])
        self.assertEqual(resource["external_asset_bytes"], 0)
        self.assertTrue(resource["project_authored"])
        self.assertIn("not measurements", " ".join(resource["limitations"]))
        self.assertEqual(pitch["engine_sha256"], engine_hash)
        self.assertEqual(pitch["engine_version"], ENGINE_VERSION)
        self.assertEqual(pitch["summary"]["probe_count"], 6)
        self.assertLess(pitch["summary"]["maximum_absolute_error_cents"], 0.05)
        self.assertEqual(set(pitch["probes"]), {"zhenggu", "cegu"})

    def test_shipped_evidence_matches_current_engine(self) -> None:
        resource = json.loads(
            (DIRECTORY / "资源核验.json").read_text(encoding="utf-8")
        )
        pitch = json.loads(
            (DIRECTORY / "音准校准.json").read_text(encoding="utf-8")
        )
        digest = hashlib.sha256(ENGINE_SOURCE.read_bytes()).hexdigest()
        self.assertEqual(resource["engine_sha256"], digest)
        self.assertEqual(pitch["engine_sha256"], digest)
        self.assertEqual(resource["external_assets"], [])
        self.assertLess(pitch["summary"]["maximum_absolute_error_cents"], 0.05)


if __name__ == "__main__":
    unittest.main()
