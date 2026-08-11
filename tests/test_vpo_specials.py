import gc
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

import pytest

from tianlai.events import PerformanceEvent
from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.instrument import create_instrument
from tianlai.renderer import render_to_wav
from tianlai.tuning import EqualTemperament
from tianlai.vpo_specials import (
    create_vpo_celesta,
    create_vpo_cowbell,
    create_vpo_mixed_choir,
    create_vpo_orchestral_hit,
)


ROOT = Path(__file__).resolve().parents[1]
VPO_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3"
MANIFESTS = {
    "钢片琴": ROOT / "乐器" / "键盘乐器" / "钢片琴" / "乐器.json",
    "合唱啊声": ROOT / "乐器" / "人声乐器" / "合唱啊声" / "乐器.json",
    "牛铃": ROOT / "乐器" / "现代鼓组" / "牛铃" / "乐器.json",
    "管弦重击": ROOT / "乐器" / "电子乐器" / "管弦重击" / "乐器.json",
}
EXAMPLES = {
    name: ROOT / "examples" / f"{name}_奏法.events.json" for name in MANIFESTS
}
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(VPO_ROOT.is_dir(), "Virtual Playing Orchestra is not installed")
class VpoSpecialCandidateTests(unittest.TestCase):
    def create_candidate(self, name: str, sample_rate: int = 48_000):
        path = MANIFESTS[name]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return create_instrument(
            manifest, sample_rate, base_directory=str(path.parent)
        )

    @staticmethod
    def note_on(
        instrument,
        note_id: int,
        midi_note: float,
        velocity: float,
        *,
        sequence: int = 0,
        tuning: EqualTemperament | None = None,
    ) -> None:
        instrument.handle_event(
            PerformanceEvent(
                0,
                sequence,
                "note_on",
                {
                    "note_id": note_id,
                    "midi_note": midi_note,
                    "velocity": velocity,
                },
            ),
            tuning or EqualTemperament(),
        )

    def test_four_candidates_are_dedicated_and_have_no_silent_gm(self) -> None:
        expected_types = {
            "钢片琴": "vpo_celesta",
            "合唱啊声": "vpo_mixed_choir",
            "牛铃": "vpo_cowbell",
            "管弦重击": "vpo_orchestral_hit",
        }
        for name, instrument_type in expected_types.items():
            manifest = json.loads(MANIFESTS[name].read_text(encoding="utf-8"))
            self.assertEqual(manifest["type"], instrument_type)
            self.assertEqual(manifest["quality_tier"], "formal")
            self.assertEqual(
                manifest["collaboration_review_status"], "untested"
            )
            self.assertNotIn("implementation", manifest)
            self.assertTrue(MANIFESTS[name].with_name("乐器.py").is_file())
            self.assertEqual(
                manifest["fallback_policy"], "explicit_only_no_silent_gm"
            )
            self.assertNotIn("soundfont", manifest)
            self.assertTrue(
                (MANIFESTS[name].parent / manifest["asset_root"]).resolve().is_dir()
            )
            instrument = create_instrument(
                manifest,
                48_000,
                base_directory=str(MANIFESTS[name].parent),
            )
            self.assertEqual(
                instrument._tianlai_factory_provenance["factory_route"],
                "builtin_manifest_dispatch_no_implementation",
            )
            del instrument
            gc.collect()

    def test_real_region_counts_layers_and_embedded_loops(self) -> None:
        celesta = self.create_candidate("钢片琴")
        self.assertEqual(
            {name: len(engine.regions) for name, engine in celesta.engines.items()},
            {"soft": 11, "hard": 10},
        )
        self.assertTrue(
            all(
                region.loop_start is None
                for engine in celesta.engines.values()
                for region in engine.regions
            )
        )

        choir = self.create_candidate("合唱啊声")
        self.assertEqual(
            {name: len(engine.regions) for name, engine in choir.engines.items()},
            {"sustain": 37, "normal": 37},
        )
        self.assertTrue(
            all(
                region.loop_start is not None
                for engine in choir.engines.values()
                for region in engine.regions
            )
        )
        self.assertEqual((choir._hold_samples, choir._decay_samples), (40_320, 1_056_000))
        self.assertEqual(choir._sustain_level, 0.7)

        cowbell = self.create_candidate("牛铃")
        self.assertEqual(
            set(cowbell.engines),
            {("soft", 1), ("soft", 2), ("hard", 1), ("hard", 2)},
        )
        cowbell_names = {
            region.path.name.lower()
            for engine in cowbell.engines.values()
            for region in engine.regions
        }
        self.assertEqual(len(cowbell_names), 4)
        self.assertTrue(all("cowbell" in name for name in cowbell_names))
        self.assertTrue(all("agogo" not in name for name in cowbell_names))

        hit = self.create_candidate("管弦重击")
        string_counts = {
            section.name: {
                component: sum(len(source.engine.regions) for source in sources)
                for component, sources in section.engines.items()
            }
            for section in hit.strings.sections
        }
        self.assertEqual(
            string_counts,
            {
                "bass": {"accent_attack": 26, "accent_sustain": 12},
                "cello": {"accent_attack": 40, "accent_sustain": 11},
                "viola": {"accent_attack": 24, "accent_sustain": 27},
                "violin": {"accent_attack": 28, "accent_sustain": 14},
            },
        )
        self.assertEqual(
            {key: len(engine.regions) for key, engine in hit.brass.engines.items()},
            {
                ("tuba", "attack"): 12,
                ("tuba", "sustain"): 9,
                ("horn", "attack"): 26,
                ("horn", "sustain"): 26,
                ("trombone", "attack"): 18,
                ("trombone", "sustain"): 18,
                ("trumpet", "attack"): 23,
                ("trumpet", "sustain"): 23,
            },
        )
        self.assertEqual(len(hit.bass_drum.engines), 4)
        self.assertEqual(len(hit.cymbal.engines), 4)

    def test_resource_freezes_and_pitch_calibrations_cover_real_defaults(self) -> None:
        expected = {
            "钢片琴": (20, 20, 1),
            "合唱啊声": (37, 37, 2),
            "牛铃": (None, 4, 1),
            "管弦重击": (140, 278, 4),
        }
        for name, (calibration_count, sample_count, sfz_count) in expected.items():
            directory = MANIFESTS[name].parent
            calibration = json.loads(
                (directory / "音准校准.json").read_text(encoding="utf-8")
            )
            audit = json.loads(
                (directory / "资源核验.json").read_text(encoding="utf-8")
            )
            self.assertEqual(audit["sample_count"], sample_count)
            self.assertEqual(len(audit["source_sfz_sha256"]), sfz_count)
            self.assertEqual(len(audit["sample_set_sha256"]), 64)
            for relative, expected_hash in audit["source_sfz_sha256"].items():
                self.assertEqual(
                    hashlib.sha256((VPO_ROOT / relative).read_bytes()).hexdigest(),
                    expected_hash,
                )
            if calibration_count is None:
                self.assertFalse(calibration["applicable"])
                self.assertEqual(calibration["samples"], {})
            else:
                self.assertTrue(calibration["applicable"])
                self.assertEqual(
                    calibration["summary"]["sample_count"], calibration_count
                )
                self.assertEqual(len(calibration["samples"]), calibration_count)

        for name in ("钢片琴", "合唱啊声"):
            candidate = self.create_candidate(name)
            calibration = json.loads(
                (MANIFESTS[name].parent / "音准校准.json").read_text(
                    encoding="utf-8"
                )
            )["samples"]
            for engine in candidate.engines.values():
                for region in engine.regions:
                    relative = region.path.relative_to(VPO_ROOT).as_posix()
                    self.assertAlmostEqual(
                        region.root_pitch_hz,
                        calibration[relative]["measured_hz"],
                        places=4,
                    )

        hit = self.create_candidate("管弦重击")
        hit_calibration = json.loads(
            (MANIFESTS["管弦重击"].parent / "音准校准.json").read_text(
                encoding="utf-8"
            )
        )["samples"]
        stable_engines = [
            source.engine
            for section in hit.strings.sections
            for source in section.engines["accent_sustain"]
        ] + [
            engine
            for (section, component), engine in hit.brass.engines.items()
            if component == "sustain"
        ]
        for engine in stable_engines:
            for region in engine.regions:
                relative = region.path.relative_to(VPO_ROOT).as_posix()
                self.assertAlmostEqual(
                    region.root_pitch_hz,
                    hit_calibration[relative]["measured_hz"],
                    places=4,
                )

    def test_machine_audition_reports_are_frozen_and_human_pending(self) -> None:
        for name, manifest_path in MANIFESTS.items():
            report = json.loads(
                (manifest_path.parent / "试听核验.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["status"], "machine_pass_human_pending")
            self.assertEqual(report["human_review"], "pending")
            self.assertEqual(report["sample_rate"], 48_000)
            self.assertEqual(report["channels"], 2)
            self.assertEqual(report["subtype"], "PCM_24")
            self.assertEqual(report["clipped_samples"], 0)
            self.assertGreater(report["peak"], 0.002)
            self.assertLess(report["peak"], 1.0)
            self.assertEqual(
                report["hash_algorithm"],
                HASH_ALGORITHM,
            )
            self.assertEqual(
                report["canonicalization"],
                CANONICALIZATION,
            )
            self.assertEqual(
                report["manifest_canonical_sha256"],
                canonical_json_file_sha256(manifest_path),
            )
            events_path = (
                ROOT / report["events"]
                if report.get("events")
                else EXAMPLES[name]
            )
            self.assertEqual(
                report["events_canonical_sha256"],
                canonical_json_file_sha256(events_path),
            )
            self.assertEqual(len(report["wav_sha256"]), 64)

    def test_celesta_continuous_velocity_crossfade_and_pedal(self) -> None:
        tuning = EqualTemperament()
        celesta = self.create_candidate("钢片琴")
        self.note_on(celesta, 1, 72, 0.25)
        self.assertEqual(len(celesta.routes[1]), 1)
        self.assertIs(celesta.routes[1][0].engine, celesta.engines["soft"])

        self.note_on(celesta, 2, 72, 0.65)
        self.assertEqual(len(celesta.routes[2]), 2)
        soft_route, hard_route = celesta.routes[2]
        self.assertGreater(soft_route.engine.voices[soft_route.note_id].amplitude, 0.0)
        self.assertGreater(hard_route.engine.voices[hard_route.note_id].amplitude, 0.0)

        celesta.handle_event(
            PerformanceEvent(0, 3, "control", {"name": "sustain_pedal", "value": 1.0}),
            tuning,
        )
        celesta.handle_event(
            PerformanceEvent(1, 4, "note_off", {"note_id": 2}), tuning
        )
        self.assertTrue(
            all(route.engine.voices[route.note_id].pending_release for route in (soft_route, hard_route))
        )
        celesta.handle_event(
            PerformanceEvent(2, 5, "control", {"name": "sustain_pedal", "value": 0.0}),
            tuning,
        )
        self.assertTrue(
            all(route.engine.voices[route.note_id].released for route in (soft_route, hard_route))
        )

    def test_choir_modwheel_attack_and_long_envelope(self) -> None:
        tuning = EqualTemperament()
        dry = self.create_candidate("合唱啊声")
        slow = self.create_candidate("合唱啊声")
        slow.handle_event(
            PerformanceEvent(0, 0, "control", {"name": "modulation", "value": 1.0}),
            tuning,
        )
        self.note_on(dry, 1, 60, 0.5)
        self.note_on(slow, 1, 60, 0.5)
        dry_route = dry.routes[1]
        slow_route = slow.routes[1]
        dry_voice = dry_route.engine.voices[dry_route.note_id]
        slow_voice = slow_route.engine.voices[slow_route.note_id]
        self.assertEqual(slow_voice.attack_samples - dry_voice.attack_samples, 48_000)

        contour = dry.contours[dry_route.note_id]
        contour.age_samples = dry._hold_samples + dry._decay_samples
        dry.render_frame()
        self.assertAlmostEqual(
            dry_voice.amplitude, contour.base_amplitude * 0.7, places=9
        )

        sustain = self.create_candidate("合唱啊声")
        sustain.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "sustain"}), tuning
        )
        sustain.handle_event(
            PerformanceEvent(0, 1, "control", {"name": "modulation", "value": 1.0}),
            tuning,
        )
        self.note_on(sustain, 1, 67, 0.5)
        route = sustain.routes[1]
        self.assertEqual(
            route.engine.voices[route.note_id].attack_samples,
            dry_voice.attack_samples,
        )

    def test_cowbell_global_rr_crossfade_and_fixed_pitch(self) -> None:
        cowbell = self.create_candidate("牛铃")
        self.note_on(cowbell, 1, 36, 0.2, sequence=1)
        self.assertEqual(cowbell.engines[("soft", 1)].active_voice_count, 1)
        self.note_on(cowbell, 2, 84, 0.2, sequence=2)
        self.assertEqual(cowbell.engines[("soft", 2)].active_voice_count, 1)
        self.note_on(cowbell, 3, 48, 0.2, sequence=3)
        self.assertEqual(cowbell.engines[("soft", 1)].active_voice_count, 2)

        overlap = self.create_candidate("牛铃")
        self.note_on(overlap, 1, 60, 0.65)
        self.assertEqual(overlap.engines[("soft", 1)].active_voice_count, 1)
        self.assertEqual(overlap.engines[("hard", 1)].active_voice_count, 1)

        midi = self.create_candidate("牛铃")
        hz = self.create_candidate("牛铃")
        self.note_on(midi, 1, 24, 0.4, sequence=8)
        hz.handle_event(
            PerformanceEvent(
                0,
                8,
                "note_on",
                {"note_id": 1, "pitch_hz": 880.0, "velocity": 0.4},
            ),
            EqualTemperament(432.0),
        )
        midi_voice = next(
            voice for engine in midi.engines.values() for voice in engine.voices.values()
        )
        hz_voice = next(
            voice for engine in hz.engines.values() for voice in engine.voices.values()
        )
        self.assertEqual(midi_voice.region.path, hz_voice.region.path)
        self.assertEqual(midi_voice.increment, hz_voice.increment)

    def test_orchestral_hit_layers_selected_percussion_and_fixed_gate(self) -> None:
        hit = self.create_candidate("管弦重击")
        tuning = EqualTemperament()
        self.note_on(hit, 1, 60.5, 0.62)
        layer_id = hit._scheduled[0].note_id
        self.assertIn(layer_id, hit.strings.note_routes)
        self.assertIn(layer_id, hit.brass.routes)
        self.assertEqual(hit.bass_drum.engines[("low", 1)].active_voice_count, 1)
        self.assertEqual(hit.bass_drum.engines[("high", 1)].active_voice_count, 1)
        self.assertEqual(hit.cymbal.engines[("low", 1)].active_voice_count, 1)
        self.assertEqual(hit.cymbal.engines[("high", 1)].active_voice_count, 0)

        hit.handle_event(PerformanceEvent(1, 1, "note_off", {"note_id": 1}), tuning)
        self.assertIn(layer_id, hit.strings.note_routes)
        self.assertIn(layer_id, hit.brass.routes)

        hit._scheduled[0].transient_samples = 1
        hit._scheduled[0].remaining_samples = 2
        hit.render_frame()
        self.assertNotIn(layer_id, hit.brass.attack_routes)
        hit.render_frame()
        self.assertNotIn(layer_id, hit.strings.note_routes)
        self.assertNotIn(layer_id, hit.brass.routes)

        with self.assertRaisesRegex(ValueError, "outside layered range"):
            self.note_on(hit, 2, 25, 0.8)
        with self.assertRaisesRegex(ValueError, "outside layered range"):
            self.note_on(hit, 3, 85, 0.8)

        reused = self.create_candidate("管弦重击")
        self.note_on(reused, 7, 60, 0.8, sequence=7)
        reused.handle_event(
            PerformanceEvent(1, 8, "note_off", {"note_id": 7}), tuning
        )
        self.note_on(reused, 7, 64, 0.8, sequence=9)
        first_layer = reused._scheduled[0].note_id
        second_layer = reused._scheduled[1].note_id
        self.assertNotEqual(first_layer, second_layer)
        reused._scheduled[0].transient_samples = 1
        reused._scheduled[0].remaining_samples = 1
        reused.render_frame()
        self.assertNotIn(first_layer, reused.strings.note_routes)
        self.assertIn(second_layer, reused.strings.note_routes)

    def test_fractional_pitch_and_a4_change_tonal_playback_rate(self) -> None:
        for name, note in (("钢片琴", 69.5), ("合唱啊声", 69.5)):
            first = self.create_candidate(name)
            second = self.create_candidate(name)
            self.note_on(
                first, 1, note, 0.72, sequence=4, tuning=EqualTemperament(440.0)
            )
            self.note_on(
                second, 1, note, 0.72, sequence=4, tuning=EqualTemperament(442.0)
            )
            first_route = first.routes[1]
            second_route = second.routes[1]
            if isinstance(first_route, tuple):
                first_route = first_route[0]
                second_route = second_route[0]
            first_voice = first_route.engine.voices[first_route.note_id]
            second_voice = second_route.engine.voices[second_route.note_id]
            self.assertEqual(first_voice.region.path, second_voice.region.path)
            self.assertAlmostEqual(
                second_voice.increment / first_voice.increment,
                442.0 / 440.0,
                places=9,
            )

        first_hit = self.create_candidate("管弦重击")
        second_hit = self.create_candidate("管弦重击")
        self.note_on(
            first_hit, 1, 60.5, 0.72, sequence=4, tuning=EqualTemperament(440.0)
        )
        self.note_on(
            second_hit, 1, 60.5, 0.72, sequence=4, tuning=EqualTemperament(442.0)
        )
        first_layer_id = first_hit._scheduled[0].note_id
        second_layer_id = second_hit._scheduled[0].note_id
        first_route = first_hit.strings.note_routes[first_layer_id][0]
        second_route = second_hit.strings.note_routes[second_layer_id][0]
        first_voice = first_route.engine.voices[first_route.note_id]
        second_voice = second_route.engine.voices[second_route.note_id]
        self.assertEqual(first_voice.region.path, second_voice.region.path)
        self.assertAlmostEqual(
            second_voice.increment / first_voice.increment, 442.0 / 440.0, places=9
        )

    def test_missing_resources_fail_loudly(self) -> None:
        factories = {
            "钢片琴": create_vpo_celesta,
            "合唱啊声": create_vpo_mixed_choir,
            "牛铃": create_vpo_cowbell,
            "管弦重击": create_vpo_orchestral_hit,
        }
        with tempfile.TemporaryDirectory() as temporary:
            for name, factory in factories.items():
                manifest = json.loads(MANIFESTS[name].read_text(encoding="utf-8"))
                manifest["asset_root"] = "missing-vpo"
                with self.assertRaises((ValueError, FileNotFoundError), msg=name):
                    factory(
                        manifest=manifest,
                        sample_rate=48_000,
                        base_directory=temporary,
                    )

    def _render_digest_and_peak(self, name: str) -> tuple[str, float]:
        candidate = self.create_candidate(name)
        note = {"钢片琴": 72, "合唱啊声": 60, "牛铃": 60, "管弦重击": 60}[name]
        self.note_on(candidate, 1, note, 0.82, sequence=19)
        digest = hashlib.sha256()
        peak = 0.0
        for _ in range(12_000):
            left, right = candidate.render_frame()
            peak = max(peak, abs(left), abs(right))
            digest.update(struct.pack("<ff", left, right))
        return digest.hexdigest(), peak

    @pytest.mark.listening
    def test_render_is_deterministic_audible_and_unclipped(self) -> None:
        for name in MANIFESTS:
            first_hash, first_peak = self._render_digest_and_peak(name)
            gc.collect()
            second_hash, second_peak = self._render_digest_and_peak(name)
            self.assertEqual(first_hash, second_hash, name)
            self.assertEqual(first_peak, second_peak, name)
            self.assertGreater(first_peak, 0.002, name)
            self.assertLess(first_peak, 1.0, name)

    def test_chinese_path_end_to_end_wav_is_pcm24(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as temporary:
            output = Path(temporary) / "中文 输出 24位.wav"
            result = render_to_wav(
                MANIFESTS["牛铃"], EXAMPLES["牛铃"], output
            )
            self.assertGreater(result.frame_count, 1)
            with wave.open(str(output), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 2)
                self.assertEqual(wav.getframerate(), 48_000)
                self.assertEqual(wav.getsampwidth(), 3)


if __name__ == "__main__":
    unittest.main()
