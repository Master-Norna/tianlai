import gc
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

import pytest

from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament
from tianlai.vpo_strings import create_vpo_harp, create_vpo_string_section


ROOT = Path(__file__).resolve().parents[1]
STRING_ROOT = ROOT / "乐器" / "管弦乐" / "弦乐组"
HARP_ROOT = ROOT / "乐器" / "管弦乐" / "拨弦组" / "竖琴"
VPO_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3"
VCSL_ROOT = ROOT / "音源" / "VCSL"
MANIFESTS = {
    "弦乐合奏": STRING_ROOT / "弦乐合奏" / "乐器.json",
    "拨奏弦乐": STRING_ROOT / "拨奏弦乐" / "乐器.json",
    "颤弓弦乐": STRING_ROOT / "颤弓弦乐" / "乐器.json",
    "竖琴": HARP_ROOT / "乐器.json",
}
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(
    (VPO_ROOT / "libs").is_dir() and VCSL_ROOT.is_dir(),
    "Virtual Playing Orchestra and VCSL wave files are not installed",
)
class VpoStringSectionAndHarpTests(unittest.TestCase):
    def create_candidate(
        self,
        name: str,
        sample_rate: int = 48_000,
        **overrides,
    ):
        path = MANIFESTS[name]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest.update(overrides)
        return create_instrument(
            manifest, sample_rate, base_directory=str(path.parent)
        )

    def test_four_candidates_use_dedicated_code_and_never_silent_gm(self) -> None:
        expected = {
            "弦乐合奏": ("vpo_string_section", [
                "sustain", "staccato", "pizzicato", "tremolo", "accent"
            ]),
            "拨奏弦乐": ("vpo_string_section", ["pizzicato"]),
            "颤弓弦乐": ("vpo_string_section", ["tremolo"]),
            "竖琴": ("vpo_harp", None),
        }
        for name, (instrument_type, articulations) in expected.items():
            manifest = json.loads(MANIFESTS[name].read_text(encoding="utf-8"))
            self.assertEqual(manifest["type"], instrument_type)
            self.assertEqual(manifest["quality_tier"], "formal")
            self.assertEqual(
                manifest["collaboration_review_status"], "untested"
            )
            self.assertNotIn("implementation", manifest)
            self.assertTrue(MANIFESTS[name].with_name("乐器.py").is_file())
            self.assertEqual(manifest["fallback_policy"], "explicit_only_no_silent_gm")
            self.assertNotIn("soundfont", manifest)
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
            if articulations is not None:
                self.assertEqual(manifest["allowed_articulations"], articulations)

    def test_real_region_counts_layers_round_robin_and_loops(self) -> None:
        ensemble = self.create_candidate("弦乐合奏")
        expected_sources = {
            "bass": {"sustain": [12], "staccato": [26], "pizzicato": [12], "tremolo": [26]},
            "cello": {"sustain": [11], "staccato": [40], "pizzicato": [24], "tremolo": [13]},
            "viola": {"sustain": [15, 12], "staccato": [24], "pizzicato": [13], "tremolo": [20, 26]},
            "violin": {"sustain": [14], "staccato": [28], "pizzicato": [14], "tremolo": [21, 10]},
        }
        for section in ensemble.sections:
            for articulation, counts in expected_sources[section.name].items():
                actual = sorted(
                    len(source.engine.regions)
                    for source in section.engines[articulation]
                )
                self.assertEqual(actual, sorted(counts))
            if section.name != "bass":
                self.assertTrue(
                    all(
                        region.loop_start is not None
                        for source in section.engines["tremolo"]
                        for region in source.engine.regions
                    )
                )
            self.assertTrue(
                all(
                    region.loop_start is None
                    for source in section.engines["pizzicato"]
                    for region in source.engine.regions
                )
            )

        bass = next(section for section in ensemble.sections if section.name == "bass")
        engine = bass.engines["staccato"][0].engine
        tuning = EqualTemperament()
        event = lambda note_id: PerformanceEvent(
            0,
            note_id,
            "note_on",
            {"note_id": note_id, "midi_note": 42, "velocity": 0.75},
        )
        ensemble.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "staccato"}), tuning
        )
        ensemble.handle_event(event(1), tuning)
        first = engine.voices[max(engine.voices)].region.path
        ensemble.handle_event(event(2), tuning)
        second = engine.voices[max(engine.voices)].region.path
        self.assertNotEqual(first, second)

        harp = self.create_candidate("竖琴")
        self.assertEqual(
            {name: len(engine.regions) for name, engine in harp.engines.items()},
            {"open": 47, "dampened": 47},
        )
        self.assertTrue(
            all(region.loop_start is None for region in harp.engines["open"].regions)
        )

    def test_manifest_release_overrides_section_sustain_regions(self) -> None:
        ensemble = self.create_candidate("弦乐合奏", release_seconds=0.19)
        for section in ensemble.sections:
            for engine_name in ("sustain", "tremolo", "accent_sustain"):
                for source in section.engines.get(engine_name, ()):
                    self.assertTrue(
                        all(
                            region.release_seconds == 0.19
                            for region in source.engine.regions
                        )
                    )

    def test_calibration_and_resource_freeze_cover_each_real_default(self) -> None:
        expected = {
            "弦乐合奏": (64, 341, 5, "sustain"),
            "拨奏弦乐": (63, 63, 1, "pizzicato"),
            "颤弓弦乐": (96, 96, 1, "tremolo"),
            "竖琴": (45, 45, 1, "open"),
        }
        for name, (calibration_count, asset_count, sfz_count, articulation) in expected.items():
            path = MANIFESTS[name]
            calibration = json.loads(
                (path.parent / "音准校准.json").read_text(encoding="utf-8")
            )
            audit = json.loads(
                (path.parent / "资源核验.json").read_text(encoding="utf-8")
            )
            self.assertEqual(calibration["summary"]["sample_count"], calibration_count)
            self.assertEqual(len(calibration["samples"]), calibration_count)
            self.assertEqual(audit["sample_count"], asset_count)
            self.assertEqual(len(audit["source_sfz_sha256"]), sfz_count)
            manifest = json.loads(path.read_text(encoding="utf-8"))
            asset_root = (path.parent / manifest["asset_root"]).resolve()
            for relative, expected_hash in audit["source_sfz_sha256"].items():
                actual_hash = hashlib.sha256((asset_root / relative).read_bytes()).hexdigest()
                self.assertEqual(actual_hash, expected_hash)

            candidate = self.create_candidate(name)
            if name == "竖琴":
                engines = [candidate.engines[articulation]]
            else:
                engines = [
                    source.engine
                    for section in candidate.sections
                    for source in section.engines[articulation]
                ]
            checked = 0
            for engine in engines:
                for region in engine.regions:
                    relative = region.path.relative_to(asset_root).as_posix()
                    self.assertAlmostEqual(
                        region.root_pitch_hz,
                        calibration["samples"][relative]["measured_hz"],
                        places=5,
                    )
                    checked += 1
            self.assertGreaterEqual(checked, calibration_count)

    def test_concert_pitch_ranges_articulations_and_crossfades(self) -> None:
        tuning = EqualTemperament()
        ensemble = self.create_candidate("弦乐合奏")
        for name in ("sustain", "staccato", "pizzicato", "tremolo", "accent"):
            ensemble.handle_event(
                PerformanceEvent(0, 0, "articulation", {"name": name}), tuning
            )
        with self.assertRaisesRegex(ValueError, "outside the sampled"):
            ensemble.handle_event(
                PerformanceEvent(
                    0, 1, "note_on", {"note_id": 1, "midi_note": 23, "velocity": 0.8}
                ),
                tuning,
            )
        ensemble.handle_event(
            PerformanceEvent(0, 2, "articulation", {"name": "sustain"}), tuning
        )
        ensemble.handle_event(
            PerformanceEvent(
                0, 3, "note_on", {"note_id": 3, "midi_note": 42, "velocity": 0.8}
            ),
            tuning,
        )
        self.assertEqual(len(ensemble.note_routes[3]), 2)

        pizzicato = self.create_candidate("拨奏弦乐")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            pizzicato.handle_event(
                PerformanceEvent(0, 0, "articulation", {"name": "sustain"}), tuning
            )
        tremolo = self.create_candidate("颤弓弦乐")
        with self.assertRaisesRegex(ValueError, "unsupported"):
            tremolo.handle_event(
                PerformanceEvent(0, 0, "articulation", {"name": "pizzicato"}), tuning
            )

    def test_a4_fractional_pitch_expression_and_pedal(self) -> None:
        event = PerformanceEvent(
            0, 0, "note_on", {"note_id": 1, "midi_note": 69.5, "velocity": 0.72}
        )
        tuned_440 = self.create_candidate("颤弓弦乐")
        tuned_432 = self.create_candidate("颤弓弦乐")
        tuned_440.handle_event(event, EqualTemperament(440.0))
        tuned_432.handle_event(event, EqualTemperament(432.0))
        route_440 = tuned_440.note_routes[1][0]
        route_432 = tuned_432.note_routes[1][0]
        increment_440 = route_440.engine.voices[route_440.note_id].increment
        increment_432 = route_432.engine.voices[route_432.note_id].increment
        self.assertAlmostEqual(increment_432 / increment_440, 432.0 / 440.0, places=9)

        harp = self.create_candidate("竖琴")
        tuning = EqualTemperament()
        harp.handle_event(
            PerformanceEvent(0, 0, "control", {"name": "expression", "value": 0.5}),
            tuning,
        )
        self.assertAlmostEqual(harp.expression_target, 0.5**1.2)
        harp.handle_event(
            PerformanceEvent(0, 1, "control", {"name": "sustain_pedal", "value": 1.0}),
            tuning,
        )
        harp.handle_event(
            PerformanceEvent(
                0, 2, "note_on", {"note_id": 7, "midi_note": 60, "velocity": 0.8}
            ),
            tuning,
        )
        harp.handle_event(PerformanceEvent(1, 3, "note_off", {"note_id": 7}), tuning)
        voice = harp.engines["open"].voices[7]
        self.assertTrue(voice.pending_release)
        self.assertFalse(voice.released)
        harp.handle_event(
            PerformanceEvent(2, 4, "control", {"name": "sustain_pedal", "value": 0.0}),
            tuning,
        )
        self.assertTrue(voice.released)
        self.assertEqual(voice.release_samples, 30 * 48_000)

        harp.handle_event(
            PerformanceEvent(3, 5, "articulation", {"name": "dampened"}), tuning
        )
        harp.handle_event(
            PerformanceEvent(
                3, 6, "note_on", {"note_id": 8, "midi_note": 60, "velocity": 0.8}
            ),
            tuning,
        )
        harp.handle_event(PerformanceEvent(4, 7, "note_off", {"note_id": 8}), tuning)
        self.assertEqual(
            harp.engines["dampened"].voices[8].release_samples,
            round(0.35 * 48_000),
        )

    def test_missing_assets_fail_loudly(self) -> None:
        section_manifest = json.loads(MANIFESTS["弦乐合奏"].read_text(encoding="utf-8"))
        harp_manifest = json.loads(MANIFESTS["竖琴"].read_text(encoding="utf-8"))
        section_manifest["asset_root"] = "missing-vpo"
        harp_manifest["asset_root"] = "missing-vpo"
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "VPO Strings directory does not exist"):
                create_vpo_string_section(
                    manifest=section_manifest,
                    sample_rate=48_000,
                    base_directory=temporary,
                )
            with self.assertRaisesRegex(ValueError, "concert harp mapping is missing"):
                create_vpo_harp(
                    manifest=harp_manifest,
                    sample_rate=48_000,
                    base_directory=temporary,
                )

    def _render_digest_and_peak(self, name: str) -> tuple[str, float]:
        candidate = self.create_candidate(name)
        tuning = EqualTemperament()
        note = 60
        candidate.handle_event(
            PerformanceEvent(
                0, 0, "note_on", {"note_id": 1, "midi_note": note, "velocity": 0.82}
            ),
            tuning,
        )
        digest = hashlib.sha256()
        peak = 0.0
        for frame_index in range(18_000):
            if frame_index == 9_000:
                candidate.handle_event(
                    PerformanceEvent(frame_index, 1, "note_off", {"note_id": 1}), tuning
                )
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
            self.assertEqual(first_hash, second_hash)
            self.assertEqual(first_peak, second_peak)
            self.assertGreater(first_peak, 0.005)
            self.assertLess(first_peak, 1.0)


if __name__ == "__main__":
    unittest.main()
