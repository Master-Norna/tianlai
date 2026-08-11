from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator
import numpy as np
import soundfile as sf

from tianlai.post_render_check import (
    POST_RENDER_CHECK_NAME,
    PostRenderCheckError,
    _Accumulator,
    _KWeighting,
    _LoudnessMeter,
    _TruePeakMeter,
    _calculate_lra,
    analyze_rendered_wav,
    require_post_render_check_pass,
    validate_post_render_check,
    write_post_render_check,
)


_SAMPLE_RATE = 48_000
_PCM24_LSB = 1.0 / 8_388_608.0


class PostRenderCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "post-render-check.schema.json"
        cls.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _write(
        self,
        samples: np.ndarray,
        *,
        name: str = "交付 音频.wav",
        sample_rate: int = _SAMPLE_RATE,
        subtype: str = "PCM_24",
    ) -> Path:
        path = self.root / name
        sf.write(path, np.asarray(samples, dtype=np.float64), sample_rate, format="WAV", subtype=subtype)
        return path

    def _analyze(
        self,
        path: Path,
        *,
        expected_activity: bool = False,
        artifact_path: str = "render/交付 音频.wav",
        sample_rate: int = _SAMPLE_RATE,
        frame_count: int | None = None,
        plan_sha256: str | None = None,
    ) -> dict:
        info = sf.info(path)
        return analyze_rendered_wav(
            path,
            artifact_path,
            sample_rate,
            int(info.frames) if frame_count is None else frame_count,
            expected_activity,
            plan_sha256,
        )

    @staticmethod
    def _codes(report: dict) -> set[str]:
        return {str(issue["code"]) for issue in report["issues"]}

    def test_clear_rest_report_is_bound_schema_valid_and_path_private(self) -> None:
        samples = np.zeros((_SAMPLE_RATE // 2, 2), dtype=np.float64)
        path = self._write(samples)
        plan_sha256 = "a" * 64
        report = self._analyze(path, plan_sha256=plan_sha256)

        self.validator.validate(report)
        validate_post_render_check(report)
        require_post_render_check_pass(report)
        self.assertEqual(POST_RENDER_CHECK_NAME, "渲染后自检.json")
        self.assertEqual(report["artifact"]["path"], "render/交付 音频.wav")
        self.assertEqual(
            report["artifact"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(report["artifact"]["size_bytes"], path.stat().st_size)
        self.assertEqual(report["performance_plan"]["sha256"], plan_sha256)
        self.assertEqual(report["summary"]["status"], "clear")
        self.assertEqual(report["issues"], [])
        self.assertEqual(report["measurements"]["sample"]["sample_peak_frame"], 0)
        self.assertEqual(report["measurements"]["sample"]["sample_peak_channel"], "left")
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("timestamp", encoded.lower())
        self.assertNotIn("elapsed", encoded.lower())

    def test_exact_silence_blocks_expected_activity_but_one_lsb_does_not(self) -> None:
        silence = self._write(np.zeros((_SAMPLE_RATE // 2, 2)), name="silence.wav")
        blocked = self._analyze(silence, expected_activity=True)
        self.assertIn("render.expected_activity_silent", self._codes(blocked))
        self.assertFalse(blocked["summary"]["can_proceed"])
        validate_post_render_check(blocked)
        with self.assertRaisesRegex(PostRenderCheckError, "expected_activity_silent"):
            require_post_render_check_pass(blocked)

        samples = np.zeros((_SAMPLE_RATE // 2, 2), dtype=np.float64)
        samples[10, 0] = _PCM24_LSB
        one_lsb = self._write(samples, name="one-lsb.wav")
        report = self._analyze(one_lsb, expected_activity=True)
        self.assertNotIn("render.expected_activity_silent", self._codes(report))
        self.assertIn("audio.near_silent_delivery", self._codes(report))
        self.assertNotIn("audio.mono_fold_cancellation", self._codes(report))
        self.assertGreater(report["measurements"]["stereo"]["mono_fold_rms"], 0.0)
        require_post_render_check_pass(report)

        samples[10, 1] = -_PCM24_LSB
        antiphase_lsb = self._write(samples, name="anti-phase-one-lsb.wav")
        antiphase_report = self._analyze(antiphase_lsb, expected_activity=True)
        self.assertIn("audio.mono_fold_cancellation", self._codes(antiphase_report))
        self.assertTrue(antiphase_report["measurements"]["stereo"]["mono_fold_silent"])
        require_post_render_check_pass(antiphase_report)

    def test_strict_wav_contract_and_argument_boundaries(self) -> None:
        stereo = np.zeros((800, 2), dtype=np.float64)
        pcm24 = self._write(stereo, sample_rate=8_000, name="stereo.wav")
        pcm16 = self._write(stereo, sample_rate=8_000, name="pcm16.wav", subtype="PCM_16")
        mono = self._write(stereo[:, 0], sample_rate=8_000, name="mono.wav")
        unreadable = self.root / "not-wav.wav"
        unreadable.write_bytes(b"not a wav")

        with self.assertRaisesRegex(ValueError, "PCM_24"):
            self._analyze(pcm16, sample_rate=8_000)
        with self.assertRaisesRegex(ValueError, "2 channels"):
            self._analyze(mono, sample_rate=8_000)
        with self.assertRaisesRegex(ValueError, "sample rate mismatch"):
            self._analyze(pcm24, sample_rate=8_001)
        with self.assertRaisesRegex(ValueError, "frame count mismatch"):
            self._analyze(pcm24, sample_rate=8_000, frame_count=799)
        with self.assertRaisesRegex(ValueError, "cannot be decoded"):
            analyze_rendered_wav(unreadable, "render/x.wav", 8_000, 1, False)
        for invalid_rate in (0, 7_999, 384_001, True):
            with self.subTest(invalid_rate=invalid_rate):
                with self.assertRaisesRegex(ValueError, "8000 to 384000"):
                    analyze_rendered_wav(pcm24, "render/x.wav", invalid_rate, 800, False)
        unavailable = self._analyze(pcm24, sample_rate=8_000)
        self.validator.validate(unavailable)
        validate_post_render_check(unavailable)
        self.assertEqual(unavailable["measurements"]["true_peak"]["status"], "unavailable")
        self.assertEqual(unavailable["measurements"]["loudness"]["status"], "unavailable")

    def test_portable_identity_and_sha_contracts(self) -> None:
        path = self._write(np.zeros((800, 2)), sample_rate=8_000)
        invalid_paths = (
            "",
            ".",
            "../x.wav",
            "a/../x.wav",
            "/x.wav",
            "C:/x.wav",
            "a\\x.wav",
            "a//x.wav",
            "a/./x.wav",
            "a\nx.wav",
        )
        for label in invalid_paths:
            with self.subTest(label=repr(label)):
                with self.assertRaisesRegex(ValueError, "relative POSIX"):
                    analyze_rendered_wav(path, label, 8_000, 800, False)
        for digest in ("A" * 64, "a" * 63, "g" * 64, 123):
            with self.subTest(digest=digest):
                with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
                    analyze_rendered_wav(path, "render/x.wav", 8_000, 800, False, digest)  # type: ignore[arg-type]

    def test_sub_400ms_loudness_is_unavailable_without_nan(self) -> None:
        frames = int(0.399 * _SAMPLE_RATE)
        timeline = np.arange(frames, dtype=np.float64) / _SAMPLE_RATE
        tone = 0.1 * np.sin(2.0 * math.pi * 1_000.0 * timeline)
        path = self._write(np.column_stack((tone, tone)), name="short.wav")
        report = self._analyze(path)
        loudness = report["measurements"]["loudness"]
        self.assertEqual(loudness["status"], "unavailable")
        self.assertIsNone(loudness["integrated_lufs"])
        self.assertIsNone(loudness["max_momentary_lufs"])
        self.assertIsNone(loudness["max_short_term_lufs"])
        self.assertEqual(
            loudness["reason"],
            "insufficient duration for one 400 ms loudness block",
        )
        self.assertEqual(loudness["lra"]["status"], "unavailable")
        json.dumps(report, allow_nan=False)
        validate_post_render_check(report)

    def test_bs1770_stereo_1khz_reference_loudness(self) -> None:
        frames = 4 * _SAMPLE_RATE
        timeline = np.arange(frames, dtype=np.float64) / _SAMPLE_RATE
        amplitude = 10.0 ** (-23.0 / 20.0)
        tone = amplitude * np.sin(2.0 * math.pi * 1_000.0 * timeline)
        path = self._write(np.column_stack((tone, tone)), name="loudness.wav")
        report = self._analyze(path)
        loudness = report["measurements"]["loudness"]
        self.assertEqual(loudness["status"], "available")
        self.assertAlmostEqual(loudness["integrated_lufs"], -23.0, delta=0.1)
        self.assertAlmostEqual(loudness["max_momentary_lufs"], -23.0, delta=0.1)
        self.assertAlmostEqual(loudness["max_short_term_lufs"], -23.0, delta=0.1)
        self.assertEqual(loudness["block_count"], 37)
        self.assertEqual(loudness["final_gated_block_count"], 37)

    def test_ebu_tech3341_relative_gate_reference_sequence(self) -> None:
        # EBU Tech 3341 test case 3: 10 s at -36 dBFS, 60 s at
        # -23 dBFS, then 10 s at -36 dBFS.  The quiet sections must not
        # pull the integrated result away from -23 LUFS.
        meter = _LoudnessMeter(_SAMPLE_RATE)
        segment_frames = (10 * _SAMPLE_RATE, 60 * _SAMPLE_RATE, 10 * _SAMPLE_RATE)
        segment_levels = (-36.0, -23.0, -36.0)
        frame_offset = 0
        block_frames = 65_536
        for frames, level_dbfs in zip(segment_frames, segment_levels, strict=True):
            amplitude = 10.0 ** (level_dbfs / 20.0)
            remaining = frames
            while remaining:
                take = min(block_frames, remaining)
                timeline = (
                    np.arange(frame_offset, frame_offset + take, dtype=np.float64)
                    / _SAMPLE_RATE
                )
                tone = amplitude * np.sin(2.0 * math.pi * 1_000.0 * timeline)
                meter.process(np.column_stack((tone, tone)))
                frame_offset += take
                remaining -= take
        report = meter.report()
        self.assertAlmostEqual(report["integrated_lufs"], -23.0, delta=0.1)
        self.assertGreater(report["absolute_gated_block_count"], report["final_gated_block_count"])

    def test_k_weighting_streaming_output_keeps_the_reference_bytes(self) -> None:
        rng = np.random.default_rng(20260811)
        samples = rng.normal(0.0, 0.2, size=(10_000, 2))
        meter = _KWeighting()
        outputs: list[np.ndarray] = []
        offset = 0
        for size in (1, 17, 257, 4_093, 5_632):
            outputs.append(meter.process(samples[offset : offset + size]))
            offset += size
        filtered = np.concatenate(outputs)
        payload = filtered.astype("<f8").tobytes() + meter._state.astype("<f8").tobytes()
        self.assertEqual(
            hashlib.sha256(payload).hexdigest(),
            "087435617b1c5cb77f8b2cfef179b268326841b5ed87924c53dd06d8bf728365",
        )

    def test_annex2_true_peak_reference_vectors_and_streaming_state(self) -> None:
        # EBU Tech 3341 v4 test cases 15-19.
        vectors = (
            (0.25, 0.0, 0.5, -6.0),
            (0.25, 45.0, 0.5, -6.0),
            (1.0 / 6.0, 60.0, 0.5, -6.0),
            (0.125, 67.5, 0.5, -6.0),
            (0.25, 45.0, 10.0 ** (3.0 / 20.0), 3.0),
        )
        frame_count = int(0.2 * _SAMPLE_RATE)
        timeline = np.arange(frame_count, dtype=np.float64)
        fade_frames = int(0.010 * _SAMPLE_RATE)
        fade = np.ones(frame_count, dtype=np.float64)
        fade[:fade_frames] = np.arange(fade_frames, dtype=np.float64) / fade_frames
        fade[-fade_frames:] = np.arange(fade_frames, 0, -1, dtype=np.float64) / fade_frames
        for frequency_ratio, phase_degrees, amplitude, expected_dbtp in vectors:
            with self.subTest(
                ratio=frequency_ratio,
                phase=phase_degrees,
                expected_dbtp=expected_dbtp,
            ):
                signal = amplitude * np.sin(
                    2.0 * math.pi * frequency_ratio * timeline
                    + math.radians(phase_degrees)
                ) * fade
                samples = np.column_stack((signal, signal))
                meter = _TruePeakMeter()
                offset = 0
                for size in (1, 17, 509, 4093, frame_count):
                    if offset >= frame_count:
                        break
                    meter.process(samples[offset : min(frame_count, offset + size)])
                    offset += size
                if offset < frame_count:
                    meter.process(samples[offset:])
                streamed_peak = meter.finish()
                whole = _TruePeakMeter()
                whole.process(samples)
                whole_peak = whole.finish()
                np.testing.assert_array_equal(streamed_peak, whole_peak)
                measured = 20.0 * math.log10(float(np.max(streamed_peak)))
                self.assertGreaterEqual(measured, expected_dbtp - 0.4)
                self.assertLessEqual(measured, expected_dbtp + 0.2)

    def test_lra_gates_percentiles_and_sixty_second_stability_boundary(self) -> None:
        levels = [-30.0] * 50 + [-20.0] * 50
        short = _calculate_lra(levels, 59.999)
        stable = _calculate_lra(levels, 60.0)
        self.assertEqual(short["stability"], "not_recommended")
        self.assertEqual(stable["stability"], "stable")
        self.assertEqual(stable["status"], "available")
        self.assertEqual(stable["p10_lufs"], -30.0)
        self.assertEqual(stable["p95_lufs"], -20.0)
        self.assertEqual(stable["value_lu"], 10.0)
        below = _calculate_lra([-math.inf, -80.0, -70.0], 60.0)
        self.assertEqual(below["status"], "unavailable")
        self.assertEqual(below["absolute_gated_sample_count"], 0)

    def test_metrics_cover_peak_location_mid_side_dc_stereo_extrema_and_tail(self) -> None:
        frames = 2 * _SAMPLE_RATE
        timeline = np.arange(frames, dtype=np.float64) / _SAMPLE_RATE
        tone = 0.1 * np.sin(2.0 * math.pi * 440.0 * timeline)
        anti_phase = np.column_stack((tone, -tone))
        anti_phase[123, 0] = 0.75
        anti_phase[123, 1] = -0.75
        anti_phase[-100:] = 0.08
        report = self._analyze(self._write(anti_phase, name="metrics.wav"))
        sample = report["measurements"]["sample"]
        stereo = report["measurements"]["stereo"]
        self.assertEqual(sample["sample_peak_frame"], 123)
        self.assertEqual(sample["sample_peak_channel"], "left")
        self.assertGreater(stereo["side_rms"], stereo["mid_rms"])
        self.assertIn("audio.mono_fold_cancellation", self._codes(report))
        self.assertIn("audio.tail_boundary_candidate", self._codes(report))

        constant = np.full((int(1.3 * _SAMPLE_RATE), 2), 0.02, dtype=np.float64)
        dc_report = self._analyze(self._write(constant, name="dc.wav"))
        self.assertIn("audio.sustained_dc_candidate", self._codes(dc_report))
        dc_issue = next(
            item for item in dc_report["issues"] if item["code"] == "audio.sustained_dc_candidate"
        )
        self.assertFalse(dc_issue["blocking"])

        plateau = np.zeros((_SAMPLE_RATE // 2, 2), dtype=np.float64)
        plateau[100:104, 0] = 1.0
        plateau_report = self._analyze(self._write(plateau, name="plateau.wav"))
        self.assertIn("audio.full_scale_plateau", self._codes(plateau_report))

        one_sided = np.column_stack((tone[: _SAMPLE_RATE // 2], np.zeros(_SAMPLE_RATE // 2)))
        one_sided_report = self._analyze(self._write(one_sided, name="one-sided.wav"))
        self.assertIn("audio.extreme_channel_imbalance", self._codes(one_sided_report))

    def test_canonical_internal_blocks_make_chunking_byte_deterministic(self) -> None:
        frames = 70_123
        rng = np.random.default_rng(20260808)
        samples = rng.integers(-500_000, 500_001, size=(frames, 2)).astype(np.float64)
        samples /= 8_388_608.0

        whole = _Accumulator(_SAMPLE_RATE)
        whole.process(samples)
        whole_report = whole.report()

        chunked = _Accumulator(_SAMPLE_RATE)
        offset = 0
        sizes = (1, 3, 257, 4096, 65_535, 11)
        index = 0
        while offset < frames:
            size = sizes[index % len(sizes)]
            chunked.process(samples[offset : min(frames, offset + size)])
            offset += size
            index += 1
        chunked_report = chunked.report()
        self.assertEqual(whole_report, chunked_report)
        self.assertEqual(
            json.dumps(whole_report, sort_keys=True, allow_nan=False),
            json.dumps(chunked_report, sort_keys=True, allow_nan=False),
        )

    def test_validator_rejects_forged_summary_issue_contract_identity_and_nonfinite(self) -> None:
        path = self._write(np.zeros((_SAMPLE_RATE // 2, 2)), name="blocked.wav")
        report = self._analyze(path, expected_activity=True)

        forged_summary = deepcopy(report)
        forged_summary["summary"]["can_proceed"] = True
        with self.assertRaisesRegex(PostRenderCheckError, "summary"):
            validate_post_render_check(forged_summary)

        missing_issue = deepcopy(report)
        missing_issue["issues"] = []
        missing_issue["summary"] = {
            **missing_issue["summary"],
            "status": "clear",
            "can_proceed": True,
            "blocking_count": 0,
            "issue_count": 0,
            "severity_counts": {},
            "decision_counts": {},
            "category_counts": {},
        }
        with self.assertRaisesRegex(PostRenderCheckError, "issues"):
            validate_post_render_check(missing_issue)

        forged_id = deepcopy(report)
        forged_id["issues"][0]["id"] = "selfcheck-" + "0" * 20
        with self.assertRaisesRegex(PostRenderCheckError, "bind its evidence"):
            validate_post_render_check(forged_id)

        wrong_type = deepcopy(report)
        wrong_type["version"] = True
        with self.assertRaisesRegex(PostRenderCheckError, "format or version"):
            validate_post_render_check(wrong_type)

        nonfinite = deepcopy(report)
        nonfinite["measurements"]["sample"]["sample_peak"] = math.nan
        with self.assertRaisesRegex(PostRenderCheckError, "finite JSON"):
            validate_post_render_check(nonfinite)

        missing_measurement = deepcopy(report)
        del missing_measurement["measurements"]["loudness"]["lra"]
        with self.assertRaisesRegex(PostRenderCheckError, "measurements.loudness"):
            validate_post_render_check(missing_measurement)

        wrong_measurement_version = deepcopy(report)
        wrong_measurement_version["measurement"]["version"] = True
        with self.assertRaisesRegex(PostRenderCheckError, "measurement contract"):
            validate_post_render_check(wrong_measurement_version)

    def test_writer_is_atomic_utf8_and_byte_deterministic(self) -> None:
        path = self._write(np.zeros((_SAMPLE_RATE // 2, 2)))
        report = self._analyze(path)
        first = self.root / "一/渲染后自检.json"
        second = self.root / "二/渲染后自检.json"
        write_post_render_check(first, report)
        write_post_render_check(second, report)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertTrue(first.read_bytes().endswith(b"\n"))
        self.assertEqual(json.loads(first.read_text(encoding="utf-8")), report)
        self.assertEqual(list(first.parent.glob("*.tianlai-part")), [])


if __name__ == "__main__":
    unittest.main()
