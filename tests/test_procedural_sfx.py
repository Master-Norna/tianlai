from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.procedural_sfx import ENGINE_VERSION, SFX_PROFILES, ProceduralSfxInstrument
from tianlai.renderer import render_to_wav
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
SFX_ROOT = ROOT / "乐器" / "环境与拟音"
INSTRUMENTS = {
    "呼吸噪声": "breath",
    "掌声": "applause",
    "枪声": "gunshot",
    "海浪": "ocean",
    "电话铃": "telephone_bell",
    "直升机": "helicopter",
    "雨境合成氛围": "rain_atmosphere",
    "鸟鸣": "bird_chorus",
}


def _manifest(profile: str, seed: int = 12345) -> dict[str, object]:
    return {
        "type": "procedural_sfx",
        "profile": profile,
        "engine_version": ENGINE_VERSION,
        "seed": seed,
    }


def _note(event_type: str, note_id: int = 1) -> PerformanceEvent:
    payload: dict[str, object] = {"note_id": note_id}
    if event_type == "note_on":
        payload.update({"midi_note": 60, "velocity": 0.8})
    else:
        payload["release_velocity"] = 0.5
    return PerformanceEvent(0, 0, event_type, payload)


class ProceduralSfxTests(unittest.TestCase):
    def test_eight_catalog_entries_use_builtin_managed_implementations(self) -> None:
        profiles: set[str] = set()
        seeds: set[int] = set()
        for name, expected_profile in INSTRUMENTS.items():
            path = SFX_ROOT / name / "乐器.json"
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["type"], "procedural_sfx")
            self.assertEqual(manifest["profile"], expected_profile)
            self.assertEqual(manifest["engine_version"], ENGINE_VERSION)
            self.assertEqual(manifest["quality_tier"], "formal")
            self.assertEqual(
                manifest["collaboration_review_status"], "untested"
            )
            self.assertNotIn("manual_review", manifest)
            self.assertNotIn("implementation", manifest)
            self.assertTrue((path.parent / "乐器.py").is_file())
            self.assertNotIn("soundfont", manifest)
            instrument = create_instrument(
                manifest,
                8_000,
                base_directory=str(path.parent),
            )
            self.assertIsInstance(instrument, ProceduralSfxInstrument)
            self.assertEqual(
                instrument._tianlai_factory_provenance["factory_route"],
                "builtin_manifest_dispatch_no_implementation",
            )
            profiles.add(str(manifest["profile"]))
            seeds.add(int(manifest["seed"]))
        self.assertEqual(profiles, set(SFX_PROFILES))
        self.assertEqual(len(seeds), 8)

    def _signature(self, profile: str) -> tuple[str, float]:
        instrument = ProceduralSfxInstrument.from_manifest(_manifest(profile), 16_000)
        instrument.handle_event(_note("note_on"), EqualTemperament())
        digest = hashlib.sha256()
        peak = 0.0
        for _ in range(16_000):
            left, right = instrument.render_frame()
            self.assertTrue(math.isfinite(left) and math.isfinite(right))
            peak = max(peak, abs(left), abs(right))
            digest.update(struct.pack("<ff", left, right))
        return digest.hexdigest(), peak

    def test_all_eight_models_are_deterministic_distinct_and_audible(self) -> None:
        self.assertEqual(
            set(SFX_PROFILES),
            {
                "breath",
                "applause",
                "gunshot",
                "ocean",
                "telephone_bell",
                "helicopter",
                "rain_atmosphere",
                "bird_chorus",
            },
        )
        signatures: set[str] = set()
        for profile in SFX_PROFILES:
            first = self._signature(profile)
            second = self._signature(profile)
            self.assertEqual(first, second)
            self.assertGreater(first[1], 0.005)
            self.assertLess(first[1], 1.0)
            signatures.add(first[0])
        self.assertEqual(len(signatures), 8)

    def test_seed_changes_stochastic_scene_but_remains_repeatable(self) -> None:
        first = ProceduralSfxInstrument.from_manifest(_manifest("rain_atmosphere", 1), 8_000)
        second = ProceduralSfxInstrument.from_manifest(_manifest("rain_atmosphere", 2), 8_000)
        tuning = EqualTemperament()
        first.handle_event(_note("note_on"), tuning)
        second.handle_event(_note("note_on"), tuning)
        first_frames = tuple(first.render_frame() for _ in range(2_000))
        second_frames = tuple(second.render_frame() for _ in range(2_000))
        self.assertNotEqual(first_frames, second_frames)

    def test_expression_modulation_distance_and_pedal_are_smoothed(self) -> None:
        instrument = ProceduralSfxInstrument.from_manifest(
            {
                **_manifest("helicopter"),
                "expression_smoothing_seconds": 0.002,
            },
            8_000,
        )
        tuning = EqualTemperament()
        instrument.handle_event(_note("note_on"), tuning)
        for sequence, (name, value) in enumerate(
            (("expression", 0.3), ("modulation", 0.9), ("distance", 0.8)),
            start=1,
        ):
            instrument.handle_event(
                PerformanceEvent(0, sequence, "control", {"name": name, "value": value}),
                tuning,
            )
        for _ in range(160):
            instrument.render_frame()
        self.assertAlmostEqual(instrument.expression, 0.3, delta=0.001)
        self.assertAlmostEqual(instrument.modulation, 0.9, delta=0.001)
        self.assertAlmostEqual(instrument.distance, 0.8, delta=0.001)

        instrument.handle_event(
            PerformanceEvent(0, 4, "control", {"name": "sustain_pedal", "value": 1.0}),
            tuning,
        )
        instrument.handle_event(_note("note_off"), tuning)
        self.assertTrue(instrument.voices[1].pending_release)
        instrument.handle_event(
            PerformanceEvent(0, 5, "control", {"name": "sustain_pedal", "value": 0.0}),
            tuning,
        )
        self.assertEqual(instrument.voices[1].stage, "release")

    def test_gunshot_is_a_natural_one_shot_and_ignores_early_note_off(self) -> None:
        manifest = {
            **_manifest("gunshot"),
            "parameters": {
                "one_shot_seconds": 0.03,
                "release_seconds": 0.01,
            },
        }
        instrument = ProceduralSfxInstrument.from_manifest(manifest, 8_000)
        tuning = EqualTemperament()
        instrument.handle_event(_note("note_on"), tuning)
        instrument.handle_event(_note("note_off"), tuning)
        self.assertNotEqual(instrument.voices[1].stage, "release")
        for _ in range(400):
            instrument.render_frame()
        self.assertEqual(instrument.active_voice_count, 0)

    def test_version_parameter_and_seed_validation(self) -> None:
        with self.assertRaisesRegex(ValueError, "engine_version"):
            ProceduralSfxInstrument.from_manifest(
                {**_manifest("ocean"), "engine_version": "future"}, 48_000
            )
        with self.assertRaisesRegex(ValueError, "unknown procedural SFX"):
            ProceduralSfxInstrument.from_manifest(_manifest("generic_noise"), 48_000)
        with self.assertRaisesRegex(ValueError, "seed"):
            ProceduralSfxInstrument.from_manifest(_manifest("ocean", -1), 48_000)
        with self.assertRaisesRegex(ValueError, "unknown procedural SFX parameter"):
            ProceduralSfxInstrument.from_manifest(
                {**_manifest("ocean"), "parameters": {"mystery": 1.0}}, 48_000
            )

    def test_source_hash_examples_and_schema_are_frozen(self) -> None:
        engine_hash = hashlib.sha256(
            (ROOT / "tianlai" / "procedural_sfx.py").read_bytes()
        ).hexdigest().upper()
        self.assertEqual(
            engine_hash,
            "C7A516711FAA970FF6CA5035EE3D22251E43F23FA5D0D28D4DEDD9248ABF5122",
        )
        for name in INSTRUMENTS:
            source = (SFX_ROOT / name / "来源.md").read_text(encoding="utf-8")
            self.assertIn(engine_hash, source)
            resources = json.loads(
                (SFX_ROOT / name / "资源核验.json").read_text(encoding="utf-8")
            )
            self.assertEqual(resources["profile"], INSTRUMENTS[name])
            self.assertEqual(resources["engine_sha256"], engine_hash)
            self.assertEqual(resources["external_assets"], [])
            example = ROOT / "examples" / f"{name}_程序建模.events.json"
            self.assertTrue(example.is_file())
            self.assertGreater(len(json.loads(example.read_text(encoding="utf-8"))["events"]), 2)
            audition = json.loads(
                (SFX_ROOT / name / "试听核验.json").read_text(encoding="utf-8")
            )
            # 102 件试听核验已统一由 dedicated_candidates 生成器写出,
            # status 取统一值;人工复查状态另见 human_review 字段。
            self.assertEqual(audition["status"], "machine_pass_human_pending")
            self.assertEqual(audition["human_review"], "pending")
            self.assertEqual(audition["sample_rate"], 48_000)
            self.assertEqual(audition["clipped_samples"], 0)
            self.assertGreater(audition["peak"], 0.01)
            self.assertLess(audition["peak"], 1.0)
            self.assertEqual(len(audition["wav_sha256"]), 64)

        schema = json.loads(
            (ROOT / "schemas" / "instrument.schema.json").read_text(encoding="utf-8")
        )
        branches = [
            branch
            for branch in schema["oneOf"]
            if branch.get("properties", {}).get("type", {}).get("const")
            == "procedural_sfx"
        ]
        self.assertEqual(len(branches), 1)
        self.assertEqual(
            set(branches[0]["properties"]["profile"]["enum"]), set(SFX_PROFILES)
        )

    def test_windows_chinese_path_end_to_end_pcm24(self) -> None:
        performance = {
            "sample_rate": 8_000,
            "duration_seconds": 0.4,
            "events": [
                {
                    "time": 0.0,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                    "velocity": 0.8,
                },
                {"time": 0.1, "type": "control", "name": "modulation", "value": 0.8},
                {"time": 0.2, "type": "note_off", "note_id": 1},
            ],
        }
        with tempfile.TemporaryDirectory(prefix="天籁环境拟音_") as temporary_directory:
            temporary = Path(temporary_directory)
            events = temporary / "场景事件.json"
            events.write_text(
                json.dumps(performance, ensure_ascii=False), encoding="utf-8"
            )
            for name in INSTRUMENTS:
                output = temporary / f"{name}_试听.wav"
                result = render_to_wav(SFX_ROOT / name / "乐器.json", events, output)
                self.assertGreater(result.peak_active_voices, 0)
                with wave.open(str(output), "rb") as audio:
                    self.assertEqual(audio.getnchannels(), 2)
                    self.assertEqual(audio.getsampwidth(), 3)
                    self.assertEqual(audio.getframerate(), 8_000)
                    self.assertEqual(audio.getnframes(), 3_200)


if __name__ == "__main__":
    unittest.main()
