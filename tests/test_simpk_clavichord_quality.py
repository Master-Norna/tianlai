from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock
import wave

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
QUALITY_PATH = (
    ROOT / "乐器" / "键盘乐器" / "击弦古钢琴" / "核验SIMPK质量.py"
)
SPEC = importlib.util.spec_from_file_location(
    "tianlai_simpk_clavichord_quality",
    QUALITY_PATH,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import quality audit: {QUALITY_PATH}")
QUALITY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = QUALITY
SPEC.loader.exec_module(QUALITY)


def _pcm24_bytes(frames: np.ndarray) -> bytes:
    values = np.asarray(frames, dtype=np.int64).reshape(-1)
    if np.any(values < -8_388_608) or np.any(values > 8_388_607):
        raise ValueError("value does not fit signed 24-bit PCM")
    unsigned = np.where(values < 0, values + 0x1000000, values).astype(np.uint32)
    octets = np.empty((unsigned.size, 3), dtype=np.uint8)
    octets[:, 0] = unsigned & 0xFF
    octets[:, 1] = (unsigned >> 8) & 0xFF
    octets[:, 2] = (unsigned >> 16) & 0xFF
    return octets.tobytes()


def _write_wav(path: Path, frames: np.ndarray, sample_rate: int = 48_000) -> None:
    values = np.asarray(frames, dtype=np.int32)
    if values.ndim != 2:
        raise ValueError("frames must be two-dimensional")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(values.shape[1])
        output.setsampwidth(3)
        output.setframerate(sample_rate)
        output.writeframes(_pcm24_bytes(values))


def _mapping(
    path: Path,
    *,
    name: str,
    note: int = 69,
    velocity_low: int = 41,
    velocity_high: int = 109,
    round_robin: int = 1,
    timbre: str = "lupe",
) -> SimpleNamespace:
    with wave.open(str(path), "rb") as input_file:
        frame_count = input_file.getnframes()
        channels = input_file.getnchannels()
    return SimpleNamespace(
        sample_path=f"assets/wav/{timbre}/{name}.wav",
        sample_file=path,
        root_note=note,
        velocity_low=velocity_low,
        velocity_high=velocity_high,
        round_robin_position=round_robin,
        timbre=timbre,
        channels=channels,
        frame_count=frame_count,
        offset_frames=0,
        end_frame_exclusive=frame_count,
        release_seconds=4.0,
    )


class SimpkClavichordQualityTests(unittest.TestCase):
    def test_committed_manifest_and_evidence_report_the_audited_reality(
        self,
    ) -> None:
        instrument_root = ROOT / "乐器" / "键盘乐器" / "击弦古钢琴"
        manifest = json.loads(
            (instrument_root / "乐器.json").read_text(encoding="utf-8")
        )
        evidence = json.loads(
            (instrument_root / "SIMPK来源证据.json").read_text(encoding="utf-8")
        )
        report = json.loads(
            (instrument_root / "样本质量核验.json").read_text(encoding="utf-8")
        )

        self.assertEqual((manifest["note_min"], manifest["note_max"]), (28, 90))
        self.assertIn("1 unique recorded velocity", manifest["sampled_range"])
        self.assertEqual(
            evidence["playback_mapping"],
            {
                "source_note_range": [40, 102],
                "playback_note_offset": -12,
                "sounding_note_range": [28, 90],
                "sounding_range": "E1-F#6",
                "policy": "preserve_recorded_native_octave",
                "description": (
                    "Preserve the recorded native octave; do not pitch the WAV "
                    "data up by 12 semitones."
                ),
            },
        )
        uniqueness = evidence["content_uniqueness"]
        self.assertEqual(uniqueness["hashed_sample_count"], 756)
        self.assertEqual(uniqueness["unique_pcm_content_count"], 252)
        self.assertEqual(uniqueness["duplicate_pcm_group_count"], 252)
        self.assertEqual(
            uniqueness["duplicate_group_categories"],
            {
                "cross_velocity_layer": 252,
                "cross_round_robin": 0,
                "cross_key": 0,
                "cross_timbre": 0,
            },
        )
        self.assertEqual(report["playback_mapping"]["sounding_note_range"], [28, 90])
        self.assertEqual(report["content_uniqueness"]["unique_pcm_content_count"], 252)
        self.assertEqual(report["hard_failure_count"], 0)
        self.assertEqual(report["status"], "review")

    def test_stream_metrics_classify_silence_clipping_and_tail_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            quiet_tone = np.zeros((4_800, 2), dtype=np.int32)
            phase = np.arange(4_800, dtype=np.float64) / 48_000.0
            quiet_tone[:, 0] = (
                np.sin(2.0 * np.pi * 440.0 * phase) * 400_000
            ).astype(np.int32)
            quiet_tone[:, 1] = quiet_tone[:, 0]

            silent_path = root / "silent.wav"
            clipped_path = root / "clipped.wav"
            tail_path = root / "tail.wav"
            _write_wav(silent_path, np.zeros((240, 2), dtype=np.int32))
            clipped = quiet_tone.copy()
            clipped[100:104] = 8_388_607
            _write_wav(clipped_path, clipped)
            tail = quiet_tone.copy()
            tail[-2] = 0
            tail[-1] = 3_000_000
            _write_wav(tail_path, tail)

            report = QUALITY.audit_validated_samples(
                [
                    _mapping(silent_path, name="silent"),
                    _mapping(clipped_path, name="clipped", note=70),
                    _mapping(tail_path, name="tail", note=71),
                ],
                source_name="fixture",
                chunk_frames=127,
                require_full_coverage=False,
            )

        by_name = {
            Path(record["sample_path"]).stem: record
            for record in report["samples"]
        }
        self.assertIn(
            "silent_or_only_one_lsb",
            by_name["silent"]["hard_failures"],
        )
        self.assertIn(
            "confirmed_digital_clipping",
            by_name["clipped"]["hard_failures"],
        )
        self.assertGreaterEqual(
            by_name["clipped"]["diagnostics"]["longest_rail_run_frames"],
            3,
        )
        self.assertIn(
            "tail_discontinuity_or_cutoff",
            by_name["tail"]["review_risks"],
        )
        self.assertFalse(by_name["tail"]["hard_failures"])
        self.assertEqual(report["status"], "fail")

    def test_leading_quiet_and_low_level_are_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "soft.wav"
            frames = np.zeros((9_600, 2), dtype=np.int32)
            frames[7_200:, 0] = 2_000
            frames[7_200:, 1] = -2_000
            _write_wav(path, frames)
            report = QUALITY.audit_validated_samples(
                [_mapping(path, name="soft")],
                source_name="fixture",
                chunk_frames=313,
                require_full_coverage=False,
            )

        record = report["samples"][0]
        self.assertFalse(record["hard_failures"])
        self.assertIn("very_low_level", record["review_risks"])
        self.assertIn("long_leading_digital_silence", record["review_risks"])
        self.assertIn("long_leading_quiet_section", record["review_risks"])
        self.assertEqual(record["status"], "review")

    def test_report_is_deterministic_across_input_order_and_chunk_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phase = np.arange(997, dtype=np.float64) / 48_000.0
            frames = np.column_stack(
                (
                    np.sin(2.0 * np.pi * 330.0 * phase),
                    np.sin(2.0 * np.pi * 330.0 * phase + 0.2),
                )
            )
            frames = (frames * 1_000_000).astype(np.int32)
            first_path = root / "first.wav"
            second_path = root / "second.wav"
            _write_wav(first_path, frames)
            _write_wav(second_path, -frames)
            first = _mapping(first_path, name="first", note=60)
            second = _mapping(
                second_path,
                name="second",
                note=61,
                round_robin=2,
                timbre="reso",
            )

            report_a = QUALITY.audit_validated_samples(
                [second, first],
                source_name="fixture",
                chunk_frames=17,
                require_full_coverage=False,
            )
            report_b = QUALITY.audit_validated_samples(
                [first, second],
                source_name="fixture",
                chunk_frames=251,
                require_full_coverage=False,
            )

        self.assertEqual(
            QUALITY.render_report_json(report_a),
            QUALITY.render_report_json(report_b),
        )
        self.assertEqual(
            report_a["playback_mapping"],
            {
                "source_note_range": [40, 102],
                "playback_note_offset": -12,
                "sounding_note_range": [28, 90],
                "policy": "preserve_recorded_native_octave",
            },
        )
        self.assertFalse(report_a["coverage"]["complete"])
        self.assertEqual(report_a["global_hard_failures"], [])

    def test_exact_pcm_reuse_across_velocity_layers_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phase = np.arange(1_000, dtype=np.float64) / 48_000.0
            tone = (
                np.sin(2.0 * np.pi * 220.0 * phase) * 1_000_000
            ).astype(np.int32)
            frames = np.column_stack((tone, tone))
            soft_path = root / "soft.wav"
            loud_path = root / "loud.wav"
            _write_wav(soft_path, frames)
            _write_wav(loud_path, frames)
            report = QUALITY.audit_validated_samples(
                [
                    _mapping(
                        soft_path,
                        name="soft",
                        velocity_low=0,
                        velocity_high=40,
                    ),
                    _mapping(
                        loud_path,
                        name="loud",
                        velocity_low=110,
                        velocity_high=127,
                    ),
                ],
                source_name="fixture",
                require_full_coverage=False,
            )

        uniqueness = report["content_uniqueness"]
        self.assertEqual(uniqueness["unique_pcm_content_count"], 1)
        self.assertEqual(uniqueness["duplicate_pcm_group_count"], 1)
        self.assertEqual(
            uniqueness["duplicate_group_categories"]["cross_velocity_layer"],
            1,
        )
        for record in report["samples"]:
            self.assertIn(
                "exact_pcm_duplicate_cross_velocity_layer",
                record["review_risks"],
            )
            self.assertFalse(record["hard_failures"])

    def test_required_format_mismatch_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-rate.wav"
            frames = np.full((32, 2), 20_000, dtype=np.int32)
            _write_wav(path, frames, sample_rate=44_100)
            report = QUALITY.audit_validated_samples(
                [_mapping(path, name="wrong-rate")],
                source_name="fixture",
                require_full_coverage=False,
            )

        self.assertIn(
            "unexpected_sample_rate",
            report["samples"][0]["hard_failures"],
        )

    def test_physically_truncated_pcm_is_a_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truncated.wav"
            frames = np.full((128, 2), 200_000, dtype=np.int32)
            _write_wav(path, frames)
            sample = _mapping(path, name="truncated")
            payload = path.read_bytes()
            path.write_bytes(payload[:-36])
            report = QUALITY.audit_validated_samples(
                [sample],
                source_name="fixture",
                require_full_coverage=False,
            )

        self.assertIn(
            "truncated_pcm_data",
            report["samples"][0]["hard_failures"],
        )

    def test_expected_key_velocity_rr_coverage_is_exact(self) -> None:
        samples = []
        for timbre in QUALITY.CONVERTER.TIMBRES:
            for note in range(
                QUALITY.CONVERTER.NOTE_MIN,
                QUALITY.CONVERTER.NOTE_MAX + 1,
            ):
                for velocity_low, velocity_high in (
                    QUALITY.CONVERTER.VELOCITY_LAYERS
                ):
                    for round_robin in range(
                        1,
                        QUALITY.CONVERTER.ROUND_ROBIN_LENGTH + 1,
                    ):
                        samples.append(
                            SimpleNamespace(
                                sample_path=(
                                    f"{timbre}/{note}/{velocity_low}/"
                                    f"{round_robin}.wav"
                                ),
                                timbre=timbre,
                                root_note=note,
                                velocity_low=velocity_low,
                                velocity_high=velocity_high,
                                round_robin_position=round_robin,
                            )
                        )

        coverage, failures = QUALITY._coverage(
            samples,
            require_full_coverage=True,
        )
        self.assertTrue(coverage["complete"])
        self.assertEqual(coverage["actual_mapping_count"], 756)
        self.assertEqual(failures, [])

        incomplete, failures = QUALITY._coverage(
            samples[:-1],
            require_full_coverage=True,
        )
        self.assertFalse(incomplete["complete"])
        self.assertEqual(incomplete["missing_mapping_count"], 1)
        self.assertEqual(failures, ["mapping_coverage_mismatch"])

    def test_report_write_is_atomic_and_cleans_failed_temporary_file(self) -> None:
        report = {
            "status": "pass",
            "playback_mapping": dict(QUALITY.PLAYBACK_MAPPING),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "report.json"
            destination.write_text("previous\n", encoding="utf-8")
            with mock.patch.object(
                QUALITY.os,
                "replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated replace failure"):
                    QUALITY.write_report(destination, report)
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "previous\n",
            )
            self.assertEqual(list(root.glob(".report.json.*.tmp")), [])

            QUALITY.write_report(destination, report)
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                QUALITY.render_report_json(report),
            )
            self.assertEqual(list(root.glob(".report.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
