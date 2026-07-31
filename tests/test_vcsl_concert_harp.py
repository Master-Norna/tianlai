from __future__ import annotations

import gc
import hashlib
import json
import math
from pathlib import Path
import struct
import unittest

import pytest

from tianlai.audio import wav_loop_points
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament
from tianlai.vpo_strings import (
    harp_source_regions,
    parse_vpo_sfz,
    vpo_regions_to_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / "乐器" / "管弦乐" / "拨弦组" / "竖琴"
MANIFEST_PATH = DIRECTORY / "乐器.json"
VCSL_ROOT = ROOT / "音源" / "VCSL"
SFZ_RELATIVE = "Chordophones/Composite Chordophones/Concert Harp.sfz"
SFZ_PATH = VCSL_ROOT / SFZ_RELATIVE
pytestmark = pytest.mark.external_assets
EXPECTED_ROOTS = [
    28,
    31,
    35,
    38,
    41,
    45,
    48,
    52,
    55,
    59,
    62,
    65,
    69,
    72,
    76,
    79,
    83,
    86,
    89,
    93,
    95,
    98,
    101,
]
D4_FORTE = (
    "Chordophones/Composite Chordophones/Concert Harp/KSHarp_D4_f1.wav"
)
D4_BRIDGE = (
    "Chordophones/Composite Chordophones/Concert Harp/KSHarp_D4_mf1.wav"
)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@unittest.skipUnless(VCSL_ROOT.is_dir(), "VCSL is not installed")
class VcslConcertHarpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_json(MANIFEST_PATH)
        cls.report = load_json(DIRECTORY / "资源核验.json")
        cls.pitch = load_json(DIRECTORY / "音准校准.json")
        cls.raw_regions = vpo_regions_to_manifest(
            SFZ_PATH,
            use_embedded_loops=False,
        )
        cls.region_sets = harp_source_regions(MANIFEST_PATH)

    def create_candidate(self, sample_rate: int = 48_000):
        return create_instrument(
            self.manifest,
            sample_rate,
            base_directory=str(DIRECTORY),
        )

    def test_strict_cc0_release_and_every_selected_file_are_hash_locked(self) -> None:
        self.assertEqual(self.manifest["license"], "CC0-1.0")
        self.assertEqual(self.manifest["license_status"], "approved")
        self.assertEqual(self.manifest["upstream_version"], "1.2.2-RC")
        self.assertEqual(
            self.manifest["upstream_commit"],
            "b6e6ac82d22248edee98a0bde185eb9ef6d439ad",
        )
        self.assertEqual(self.report["sample_count"], 45)
        self.assertEqual(self.report["sample_bytes"], 76_694_972)
        self.assertEqual(
            self.report["sample_set_sha256"],
            "fde6a8543ff0dac04989deb96ead10ede341e6bdae50240a123cef5bb6c497d7",
        )
        self.assertEqual(
            self.report["source_sfz_sha256"][SFZ_RELATIVE],
            "7d202064c7d264edfc14a0d2d0a56e47c7689c0c5eb485bec677bd7281d643e7",
        )
        self.assertEqual(
            self.report["evidence_sha256"]["README.md"],
            "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b",
        )
        self.assertEqual(len(self.report["sample_sha256"]), 45)
        for relative, expected in {
            **self.report["source_sfz_sha256"],
            **self.report["evidence_sha256"],
            **self.report["sample_sha256"],
        }.items():
            actual = hashlib.sha256((VCSL_ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)

        aggregate = "".join(
            f"{digest}  {relative}\n"
            for relative, digest in sorted(self.report["sample_sha256"].items())
        )
        self.assertEqual(
            hashlib.sha256(aggregate.encode("utf-8")).hexdigest(),
            self.report["sample_set_sha256"],
        )

    def test_mapping_is_45_recordings_23_roots_two_max_layers_no_rr_or_loop(
        self,
    ) -> None:
        self.assertEqual(len(self.raw_regions), 45)
        self.assertEqual(
            len({Path(row["sample"]).resolve() for row in self.raw_regions}),
            45,
        )
        self.assertEqual(set(self.region_sets), {"open", "dampened"})
        open_regions = self.region_sets["open"]
        dampened_regions = self.region_sets["dampened"]
        self.assertEqual(len(open_regions), 47)
        self.assertEqual(len(dampened_regions), 47)
        self.assertEqual(
            {row["sample"] for row in open_regions},
            {row["sample"] for row in dampened_regions},
        )
        self.assertEqual(
            sorted({int(row["root_midi"]) for row in self.raw_regions}),
            EXPECTED_ROOTS,
        )
        counts = {
            root: sum(int(row["root_midi"]) == root for row in self.raw_regions)
            for root in EXPECTED_ROOTS
        }
        self.assertEqual(counts[28], 1)
        self.assertEqual(
            {root: count for root, count in counts.items() if root != 28},
            {root: 2 for root in EXPECTED_ROOTS if root != 28},
        )
        self.assertEqual(
            self.report["mapping"]["recording_count_distribution"],
            {"1": 1, "2": 22},
        )

        for note in range(28, 103):
            for velocity_127 in range(128):
                velocity = velocity_127 / 127.0
                matches = [
                    row
                    for row in open_regions
                    if row["key_min"] <= note <= row["key_max"]
                    and row["velocity_min"] <= velocity <= row["velocity_max"]
                ]
                self.assertEqual(
                    len(matches),
                    1,
                    (note, velocity_127, [Path(row["sample"]).name for row in matches]),
                )

        maximum_stretch = max(
            max(
                abs(row["key_min"] - row["root_midi"]),
                abs(row["key_max"] - row["root_midi"]),
            )
            for row in open_regions
        )
        self.assertEqual(maximum_stretch, 2.0)
        self.assertTrue(
            all("round_robin_length" not in row for row in open_regions)
        )
        self.assertTrue(
            all(not row.get("use_embedded_loop", False) for row in open_regions)
        )
        self.assertTrue(
            all(
                values.get("seq_length") is None
                and values.get("seq_position") is None
                and values.get("loop_mode") is None
                for values in parse_vpo_sfz(SFZ_PATH)
            )
        )
        self.assertTrue(
            all(math.isclose(row["release_seconds"], 30.0) for row in open_regions)
        )
        self.assertTrue(
            all(
                math.isclose(row["release_seconds"], 0.35)
                for row in dampened_regions
            )
        )

    def test_d4_bridge_is_split_without_fabricating_audio_or_round_robin(
        self,
    ) -> None:
        open_regions = self.region_sets["open"]
        bridge = [
            row
            for row in open_regions
            if Path(row["sample"]).relative_to(VCSL_ROOT).as_posix() == D4_BRIDGE
        ]
        self.assertEqual(len(bridge), 3)
        self.assertEqual(
            {(int(row["key_min"]), int(row["key_max"])) for row in bridge},
            {(61, 61), (62, 62), (63, 63)},
        )
        edge = [row for row in bridge if int(row["key_min"]) != 62]
        self.assertTrue(all(row["velocity_max"] < 85.0 / 127.0 for row in edge))
        centre = next(row for row in bridge if int(row["key_min"]) == 62)
        self.assertGreater(centre["velocity_max"], 100.0 / 127.0)
        self.assertEqual(self.report["mapping"]["source_region_count"], 45)
        self.assertEqual(self.report["mapping"]["project_region_count"], 47)
        self.assertEqual(self.report["mapping"]["derived_bridge_region_count"], 2)

    def test_harmful_offset_is_overridden_and_41_valid_trims_are_preserved(
        self,
    ) -> None:
        raw_offsets = {
            Path(row["sample"]).relative_to(VCSL_ROOT).as_posix(): int(
                row["offset_frames"]
            )
            for row in self.raw_regions
            if int(row["offset_frames"]) != 0
        }
        self.assertEqual(len(raw_offsets), 42)
        self.assertEqual(raw_offsets[D4_FORTE], 3744)
        for articulation in ("open", "dampened"):
            corrected: dict[str, set[int]] = {}
            for row in self.region_sets[articulation]:
                relative = Path(row["sample"]).relative_to(VCSL_ROOT).as_posix()
                corrected.setdefault(relative, set()).add(int(row["offset_frames"]))
            self.assertEqual(corrected[D4_FORTE], {0})
            for relative, raw_offset in raw_offsets.items():
                if relative != D4_FORTE:
                    self.assertEqual(corrected[relative], {raw_offset}, relative)
        diagnostic = self.report["project_overrides"]["harmful_offset_diagnostic"]
        self.assertGreater(diagnostic["first_200ms_energy_discarded_percent"], 70.0)
        self.assertGreater(
            diagnostic["upstream_offset_ms_at_44100_hz"],
            diagnostic["attack_peak_ms"],
        )
        self.assertTrue(
            self.report["project_overrides"]["upstream_sfz_unchanged"]
        )

    def test_source_audio_has_full_stereo_tails_no_clipping_and_6db_margin(
        self,
    ) -> None:
        import soundfile as sf

        peaks: dict[Path, float] = {}
        tail_dbfs: list[float] = []
        for relative in self.report["sample_sha256"]:
            path = VCSL_ROOT / relative
            info = sf.info(path)
            self.assertEqual(info.samplerate, 44_100)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_16")
            self.assertIsNone(wav_loop_points(path), relative)
            audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
            peak = float(abs(audio).max())
            self.assertGreater(peak, 1e-6, relative)
            self.assertLess(peak, 1.0, relative)
            peaks[path.resolve()] = peak
            tail = audio[-round(sample_rate * 0.1) :]
            rms = math.sqrt(float((tail * tail).mean()))
            tail_dbfs.append(20.0 * math.log10(max(rms, 1e-12)))

        maximum = max(
            peaks[Path(row["sample"]).resolve()]
            * (10.0 ** (float(row["gain_db"]) / 20.0))
            * float(self.manifest["gain"])
            for row in self.region_sets["open"]
        )
        headroom_db = -20.0 * math.log10(maximum)
        self.assertGreaterEqual(headroom_db, 6.0)
        self.assertAlmostEqual(
            headroom_db,
            self.report["audio_integrity"]["minimum_headroom_db"],
            places=5,
        )
        self.assertLessEqual(max(tail_dbfs), -60.0)
        self.assertEqual(
            self.report["audio_integrity"]["final_100ms_rms_dbfs"][
                "samples_above_minus_60_dbfs"
            ],
            0,
        )

    def test_each_recording_is_measured_and_runtime_uses_its_measured_root(
        self,
    ) -> None:
        summary = self.pitch["summary"]
        self.assertEqual(summary["sample_count"], 45)
        self.assertEqual(summary["unique_root_count"], 23)
        self.assertAlmostEqual(summary["median_detune_cents"], -7.761711)
        self.assertAlmostEqual(
            summary["maximum_absolute_detune_cents"],
            30.508318,
        )
        self.assertAlmostEqual(
            summary["upstream_mapping_median_residual_cents"],
            -2.633154,
        )
        self.assertAlmostEqual(
            summary["upstream_mapping_maximum_absolute_residual_cents"],
            19.558785,
        )
        self.assertEqual(len(self.pitch["samples"]), 45)
        for item in self.pitch["samples"].values():
            self.assertAlmostEqual(
                item["upstream_mapped_residual_cents"],
                item["detune_cents"] + item["sfz_tune_cents"],
                places=5,
            )
            self.assertAlmostEqual(
                item["playback_correction_cents"],
                -item["detune_cents"],
                places=5,
            )

        candidate = self.create_candidate()
        for engine in candidate.engines.values():
            for region in engine.regions:
                relative = region.path.relative_to(VCSL_ROOT).as_posix()
                self.assertAlmostEqual(
                    region.root_pitch_hz,
                    self.pitch["samples"][relative]["measured_hz"],
                    places=5,
                )

    def test_range_articulation_pedal_fractional_tuning_and_release_semantics(
        self,
    ) -> None:
        tuning = EqualTemperament()
        candidate = self.create_candidate()
        for note in (27, 103):
            with self.subTest(note=note), self.assertRaisesRegex(
                ValueError,
                "outside the sampled",
            ):
                candidate.handle_event(
                    PerformanceEvent(
                        0,
                        note,
                        "note_on",
                        {"note_id": note, "midi_note": note, "velocity": 0.7},
                    ),
                    tuning,
                )

        candidate.handle_event(
            PerformanceEvent(0, 10, "articulation", {"name": "sustain"}),
            tuning,
        )
        candidate.handle_event(
            PerformanceEvent(
                0,
                11,
                "note_on",
                {"note_id": 1, "midi_note": 69.5, "velocity": 0.72},
            ),
            tuning,
        )
        self.assertEqual(candidate.note_routes[1], ("open", 1))
        open_voice = candidate.engines["open"].voices[1]

        tuned_432 = self.create_candidate()
        tuned_432.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 69.5, "velocity": 0.72},
            ),
            EqualTemperament(432.0),
        )
        voice_432 = tuned_432.engines["open"].voices[1]
        self.assertAlmostEqual(
            voice_432.increment / open_voice.increment,
            432.0 / 440.0,
            places=9,
        )

        candidate.handle_event(
            PerformanceEvent(
                1,
                12,
                "control",
                {"name": "sustain_pedal", "value": 1.0},
            ),
            tuning,
        )
        candidate.handle_event(
            PerformanceEvent(2, 13, "note_off", {"note_id": 1}),
            tuning,
        )
        self.assertTrue(open_voice.pending_release)
        self.assertFalse(open_voice.released)
        candidate.handle_event(
            PerformanceEvent(
                3,
                14,
                "control",
                {"name": "sustain_pedal", "value": 0.0},
            ),
            tuning,
        )
        self.assertTrue(open_voice.released)
        self.assertEqual(open_voice.release_samples, 30 * 48_000)

        candidate.handle_event(
            PerformanceEvent(4, 15, "articulation", {"name": "dampened"}),
            tuning,
        )
        candidate.handle_event(
            PerformanceEvent(
                4,
                16,
                "note_on",
                {"note_id": 2, "midi_note": 62, "velocity": 0.9},
            ),
            tuning,
        )
        dampened_voice = candidate.engines["dampened"].voices[2]
        candidate.handle_event(
            PerformanceEvent(5, 17, "note_off", {"note_id": 2}),
            tuning,
        )
        self.assertEqual(
            dampened_voice.release_samples,
            round(0.35 * 48_000),
        )

    def _render_digest_and_peak(self) -> tuple[str, float]:
        candidate = self.create_candidate()
        tuning = EqualTemperament()
        candidate.handle_event(
            PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "midi_note": 62, "velocity": 1.0},
            ),
            tuning,
        )
        digest = hashlib.sha256()
        peak = 0.0
        for frame_index in range(24_000):
            if frame_index == 12_000:
                candidate.handle_event(
                    PerformanceEvent(
                        frame_index,
                        1,
                        "note_off",
                        {"note_id": 1},
                    ),
                    tuning,
                )
            left, right = candidate.render_frame()
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
        self.assertGreater(first_peak, 0.005)
        self.assertLess(first_peak, 1.0)

    def test_documentation_does_not_overclaim_missing_harp_mechanics(self) -> None:
        readme = (DIRECTORY / "README.md").read_text(encoding="utf-8")
        source = (DIRECTORY / "来源.md").read_text(encoding="utf-8")
        for required in (
            "不是“三力度层”",
            "没有 round-robin",
            "不是**真实竖琴七个变音踏板",
            "没有真实向上/向下 glissando",
            "当前采样器还没有连续交叉淡化",
        ):
            self.assertIn(required, readme)
        self.assertIn("Folk Harp", source)
        self.assertIn("没有混入 Concert Harp", source)
        limitation_heading = "## 单音色 formal 的已知限制"
        self.assertIn(limitation_heading, readme)
        self.assertNotIn("100% 还原", readme.split(limitation_heading, 1)[0])


if __name__ == "__main__":
    unittest.main()
