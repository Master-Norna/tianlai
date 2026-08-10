from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai import ensemble as ensemble_module
from tianlai.audio import read_wav_float, write_wav_pcm24
from tianlai.ensemble import render_plan
from tianlai.render_lock import RenderLockError, acquire_render_lock
from tianlai.space import SpaceConfig


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(document: dict) -> str:
    payload = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _FakePlan:
    def __init__(self, manifest_path: Path, *, invalid_value: float | None = None):
        capability = SimpleNamespace(
            manifest_path=str(manifest_path),
            relative_path="测试工具/回执乐器",
            quality_tier="formal",
            collaboration_review_status="untested",
            license_status="approved",
        )
        executor = SimpleNamespace(
            executor_id="receipt",
            part_id="part",
            capability=capability,
            override_map={},
            gain_db=-3.0,
            pan=0.25,
            seat=SimpleNamespace(distance_m=4.0),
        )
        points = (
            SimpleNamespace(time_seconds=0.0, offset_db=0.0),
            SimpleNamespace(time_seconds=0.005, offset_db=-2.0),
        )
        self.parts = (
            SimpleNamespace(
                executor=executor,
                performance={},
                gain_envelope=points,
            ),
        )
        self.sample_rate = 8000
        self.duration_seconds = 0.01
        self._invalid_value = invalid_value

    def to_dict(self) -> dict:
        document = {
            "title": "receipt",
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "parts": [
                {
                    "executor_id": "receipt",
                    "gain_envelope": [
                        {
                            "time_seconds": 0.0,
                            "offset_db": 0.0,
                            "effective_gain_db": -3.0,
                        },
                        {
                            "time_seconds": 0.005,
                            "offset_db": -2.0,
                            "effective_gain_db": -5.0,
                        },
                    ],
                }
            ],
        }
        if self._invalid_value is not None:
            document["duration_seconds"] = self._invalid_value
        return document


class RenderReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.manifest = self.root / "乐器.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "回执乐器",
                    "upstream": "Receipt Samples",
                    "creator": "Test Recorder",
                    "origin": "https://example.invalid/receipt",
                    "license": "CC-BY-4.0",
                    "license_status": "approved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.plan = _FakePlan(self.manifest)
        self.buffer = np.zeros((80, 2), dtype=np.float32)
        self.buffer[0] = (0.2, -0.1)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _render(self, directory: Path, **kwargs):
        fake_result = (
            self.buffer.copy(),
            2,
            _sha256(self.manifest),
        )
        with patch("tianlai.ensemble._render_part", return_value=fake_result):
            return render_plan(self.plan, directory, **kwargs)

    def test_expected_activity_requires_an_explicit_positive_note_on(self) -> None:
        self.assertFalse(
            ensemble_module._plan_has_explicit_expected_activity(self.plan)
        )
        self.plan.parts[0].performance = {
            "events": [
                {
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                }
            ]
        }
        self.assertFalse(
            ensemble_module._plan_has_explicit_expected_activity(self.plan)
        )
        self.plan.parts[0].performance["events"][0]["velocity"] = 0.0
        self.assertFalse(
            ensemble_module._plan_has_explicit_expected_activity(self.plan)
        )
        self.plan.parts[0].performance["events"][0]["velocity"] = 0.7
        self.assertTrue(
            ensemble_module._plan_has_explicit_expected_activity(self.plan)
        )

    def test_explicit_activity_with_exact_silence_fails_before_publication(
        self,
    ) -> None:
        self.plan.parts[0].performance = {
            "events": [
                {
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                    "velocity": 0.7,
                }
            ]
        }
        self.buffer.fill(0.0)
        directory = self.root / "expected-activity-silence"

        with self.assertRaisesRegex(RuntimeError, "未通过"):
            self._render(directory, write_stems=False)

        self.assertFalse((directory / "渲染回执.json").exists())
        self.assertFalse((directory / "渲染后自检.json").exists())

    def test_receipt_binds_plan_dsp_manifests_and_wavs(self) -> None:
        directory = self.root / "render"
        space = SpaceConfig(
            name="低采样率审计厅",
            wet_db=-18.0,
            room_size=0.6,
            predelay_ms=12.0,
            damping_hz=6500.0,
            highpass_hz=120.0,
            reference_distance_m=3.5,
            distance_exponent=0.7,
            min_send=0.4,
            max_send=2.0,
        )
        result = self._render(
            directory,
            write_stems=True,
            master_gain_db=-1.5,
            normalize_peak_db=-1.0,
            space=space,
        )

        receipt_path = directory / "渲染回执.json"
        self.assertEqual(result.receipt_path, str(receipt_path.resolve()))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["format"], "tianlai.render_receipt")
        self.assertEqual(receipt["version"], 3)
        plan_path = directory / "演奏计划.json"
        self.assertEqual(result.plan_path, str(plan_path.resolve()))
        self.assertEqual(receipt["performance_plan"]["path"], "演奏计划.json")
        self.assertEqual(
            receipt["performance_plan"]["file_sha256"],
            _sha256(plan_path),
        )
        license_sidecar_path = directory / "许可与署名.json"
        attribution_path = directory / "许可与署名.txt"
        self.assertEqual(
            result.license_sidecar_path,
            str(license_sidecar_path.resolve()),
        )
        self.assertEqual(
            result.attribution_path,
            str(attribution_path.resolve()),
        )
        self.assertEqual(
            receipt["license_sidecar"],
            {
                "path": "许可与署名.json",
                "sha256": _sha256(license_sidecar_path),
            },
        )
        self.assertEqual(
            receipt["attribution_notice"],
            {
                "path": "许可与署名.txt",
                "sha256": _sha256(attribution_path),
            },
        )
        license_sidecar = json.loads(
            license_sidecar_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            license_sidecar["scope"],
            {
                "rule": "actual_render_inputs_only",
                "instrument_count": 1,
                "audio_artifact_count": 2,
            },
        )
        self.assertEqual(
            license_sidecar["instruments"][0]["creator"],
            "Test Recorder",
        )
        self.assertEqual(
            license_sidecar["instruments"][0]["used_by"],
            ["receipt"],
        )
        self.assertEqual(
            receipt["performance_plan"]["sha256"],
            _canonical_sha256(self.plan.to_dict()),
        )
        self.assertEqual(
            receipt["audio_format"],
            {
                "bits_per_sample": 24,
                "channels": 2,
                "container": "WAV",
                "encoding": "PCM",
                "sample_rate": 8000,
            },
        )
        self.assertEqual(receipt["master_gain_db"], -1.5)
        self.assertEqual(
            receipt["collaboration"],
            {
                "plan_mode": "manual",
                "requested_override": None,
                "effective_mode": "manual",
                "audio_modified": False,
                "report_enabled": False,
            },
        )
        self.assertFalse((directory / "混音诊断.json").exists())
        post_render_check_path = directory / "渲染后自检.json"
        self.assertEqual(
            result.post_render_check_path,
            str(post_render_check_path.resolve()),
        )
        self.assertEqual(
            receipt["post_render_check"],
            {
                "path": "渲染后自检.json",
                "sha256": _sha256(post_render_check_path),
                "format": result.post_render_check["format"],
                "version": result.post_render_check["version"],
            },
        )
        post_render_check = json.loads(
            post_render_check_path.read_text(encoding="utf-8")
        )
        self.assertEqual(post_render_check, result.post_render_check)
        self.assertEqual(
            post_render_check["summary"],
            result.post_render_check_summary,
        )
        self.assertIs(post_render_check["summary"]["can_proceed"], True)
        self.assertEqual(receipt["normalize"]["requested_peak_dbfs"], -1.0)
        self.assertTrue(
            math.isfinite(receipt["normalize"]["applied_gain_db"])
        )
        self.assertAlmostEqual(
            receipt["normalize"]["post_normalize_peak"],
            10.0 ** (-1.0 / 20.0),
            places=6,
        )
        self.assertEqual(receipt["space"]["parameters"], space.to_dict())
        self.assertEqual(
            receipt["space"]["effective_filter_hz"],
            {"highpass_hz": 120.0, "damping_hz": 3920.0},
        )
        self.assertEqual(
            receipt["space"]["effective_tail_seconds"],
            space.tail_seconds(self.plan.sample_rate),
        )
        self.assertTrue(receipt["space"]["wet_signal_present"])
        self.assertGreater(
            receipt["mix"]["frame_count"],
            round(self.plan.duration_seconds * self.plan.sample_rate),
        )

        mix = directory / receipt["mix"]["path"]
        stem = receipt["stems"][0]
        stem_wav = directory / stem["wav"]["path"]
        self.assertEqual(receipt["mix"]["sha256"], _sha256(mix))
        self.assertEqual(stem["wav"]["sha256"], _sha256(stem_wav))
        self.assertEqual(stem["manifest"]["sha256"], _sha256(self.manifest))
        self.assertEqual(
            stem["manifest"]["path"], "测试工具/回执乐器/乐器.json"
        )
        self.assertEqual(
            stem["release_status"],
            {
                "quality_tier": "formal",
                "collaboration_review_status": "untested",
                "license_status": "approved",
            },
        )
        self.assertEqual(stem["gain_automation"][1]["offset_db"], -2.0)
        self.assertEqual(stem["gain_automation"][1]["effective_gain_db"], -5.0)

    def test_antiphase_stem_reaches_the_shared_hall(self) -> None:
        directory = self.root / "antiphase-hall"
        self.buffer.fill(0.0)
        self.buffer[0] = (0.5, -0.5)
        space = SpaceConfig(
            wet_db=-6.0,
            predelay_ms=0.0,
            damping_hz=0.0,
            highpass_hz=0.0,
        )

        result = self._render(
            directory,
            write_stems=False,
            space=space,
        )

        sample_rate, decoded = read_wav_float(result.mix_path)
        tail = np.asarray(decoded[80:], dtype=np.float64)
        self.assertEqual(sample_rate, self.plan.sample_rate)
        self.assertGreater(float(np.max(np.abs(tail))), 1e-6)
        self.assertEqual(result.space, space.to_dict())
        receipt = json.loads(
            (directory / "渲染回执.json").read_text(encoding="utf-8")
        )
        self.assertTrue(receipt["space"]["enabled"])
        self.assertTrue(receipt["space"]["wet_signal_present"])

    def test_silent_hall_is_still_reported_as_enabled(self) -> None:
        directory = self.root / "silent-hall"
        self.buffer.fill(0.0)
        space = SpaceConfig()

        result = self._render(
            directory,
            write_stems=False,
            space=space,
        )

        self.assertEqual(result.space, space.to_dict())
        receipt = json.loads(
            (directory / "渲染回执.json").read_text(encoding="utf-8")
        )
        self.assertTrue(receipt["space"]["enabled"])
        self.assertFalse(receipt["space"]["wet_signal_present"])

    def test_no_stems_is_explicit_in_receipt(self) -> None:
        directory = self.root / "no-stems"
        self._render(directory, write_stems=False)
        receipt = json.loads(
            (directory / "渲染回执.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt["stems"][0]["wav"],
            {"written": False, "path": None, "sha256": None},
        )
        sidecar = json.loads(
            (directory / "许可与署名.json").read_text(encoding="utf-8")
        )
        self.assertEqual(sidecar["scope"]["audio_artifact_count"], 1)
        self.assertEqual(
            [item["role"] for item in sidecar["audio_artifacts"]],
            ["mix"],
        )
        self.assertFalse((directory / "分轨").exists())

    def test_receipt_is_byte_deterministic_across_output_directories(self) -> None:
        first = self.root / "deterministic-a"
        second = self.root / "deterministic-b"
        self._render(first)
        self._render(second)
        self.assertEqual(
            (first / "渲染回执.json").read_bytes(),
            (second / "渲染回执.json").read_bytes(),
        )
        self.assertEqual(
            (first / "许可与署名.json").read_bytes(),
            (second / "许可与署名.json").read_bytes(),
        )
        self.assertEqual(
            (first / "许可与署名.txt").read_bytes(),
            (second / "许可与署名.txt").read_bytes(),
        )
        self.assertEqual(
            (first / "渲染后自检.json").read_bytes(),
            (second / "渲染后自检.json").read_bytes(),
        )

    def test_legacy_v2_receipt_remains_verifiable_without_post_render_check(
        self,
    ) -> None:
        directory = self.root / "legacy-v2"
        self._render(directory)
        receipt_path = directory / "渲染回执.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["version"] = 2
        receipt.pop("post_render_check")
        (directory / "渲染后自检.json").unlink()
        receipt_path.write_text(
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

        ensemble_module.verify_render_generation(directory)

    def test_v3_post_render_check_content_cannot_be_rebound_to_wrong_mix(
        self,
    ) -> None:
        directory = self.root / "tampered-post-render-check"
        self._render(directory)
        report_path = directory / "渲染后自检.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["artifact"]["sha256"] = "0" * 64
        report_path.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        receipt_path = directory / "渲染回执.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["post_render_check"]["sha256"] = _sha256(report_path)
        receipt_path.write_text(
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(RuntimeError, "当前合奏音频"):
            ensemble_module.verify_render_generation(directory)

    def test_v3_rejects_a_forged_post_render_summary(self) -> None:
        directory = self.root / "forged-post-render-summary"
        self._render(directory)
        report_path = directory / "渲染后自检.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["summary"]["blocking_count"] = 1
        report_path.write_text(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        receipt_path = directory / "渲染回执.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["post_render_check"]["sha256"] = _sha256(report_path)
        receipt_path.write_text(
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )

        with self.assertRaisesRegex(RuntimeError, "结构无效"):
            ensemble_module.verify_render_generation(directory)

    def test_nonfinite_inputs_fail_before_artifacts_are_created(self) -> None:
        cases = (
            ("master", self.plan, {"master_gain_db": float("nan")}),
            ("normalize", self.plan, {"normalize_peak_db": float("inf")}),
            (
                "plan",
                _FakePlan(self.manifest, invalid_value=float("-inf")),
                {},
            ),
        )
        for label, plan, kwargs in cases:
            directory = self.root / label
            with self.subTest(label=label):
                with patch("tianlai.ensemble._render_part") as render_part:
                    with self.assertRaisesRegex(ValueError, "有限"):
                        render_plan(plan, directory, **kwargs)
                    render_part.assert_not_called()
                self.assertFalse(directory.exists())

    def test_receipt_is_not_published_when_mix_write_fails(self) -> None:
        directory = self.root / "failed-mix"
        real_writer = write_wav_pcm24

        def writer(path, frames, sample_rate):
            if Path(path).name == "合奏.wav":
                raise OSError("simulated mix failure")
            return real_writer(path, frames, sample_rate)

        fake_result = (
            self.buffer.copy(),
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch("tianlai.ensemble.write_wav_pcm24", side_effect=writer),
        ):
            with self.assertRaisesRegex(OSError, "mix failure"):
                render_plan(self.plan, directory)
        self.assertFalse((directory / "渲染回执.json").exists())

    def test_same_output_lock_refuses_before_rendering_any_stem(self) -> None:
        directory = self.root / "locked"

        with acquire_render_lock(directory):
            with patch("tianlai.ensemble._render_part") as render_part:
                with self.assertRaises(RenderLockError):
                    render_plan(self.plan, directory)
                render_part.assert_not_called()

        self.assertFalse(directory.exists())

    def test_staging_hash_mismatch_never_replaces_previous_generation(
        self,
    ) -> None:
        directory = self.root / "staging-integrity"
        self._render(directory, write_stems=True)
        before = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        real_generation = ensemble_module._render_plan_generation

        def tampered_generation(*args, **kwargs):
            result = real_generation(*args, **kwargs)
            Path(result.mix_path).write_bytes(b"tampered after receipt")
            return result

        fake_result = (
            self.buffer.copy() * 0.5,
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch(
                "tianlai.ensemble._render_plan_generation",
                side_effect=tampered_generation,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                render_plan(self.plan, directory, write_stems=True)

        after = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_failed_final_integrity_check_rolls_back_previous_generation(
        self,
    ) -> None:
        directory = self.root / "final-integrity"
        self._render(directory, write_stems=True)
        before = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        real_verify = ensemble_module._verify_render_generation
        verify_count = 0

        def fail_second_verification(path):
            nonlocal verify_count
            verify_count += 1
            if verify_count == 2:
                raise RuntimeError("simulated final integrity failure")
            return real_verify(path)

        fake_result = (
            self.buffer.copy() * 0.5,
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch(
                "tianlai.ensemble._verify_render_generation",
                side_effect=fail_second_verification,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "integrity failure"):
                render_plan(self.plan, directory, write_stems=True)

        after = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_failed_rerender_leaves_previous_generation_byte_identical(self) -> None:
        directory = self.root / "failed-rerender"
        self._render(directory, write_stems=True)
        before = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        real_writer = write_wav_pcm24

        def writer(path, frames, sample_rate):
            if Path(path).name == "合奏.wav":
                raise OSError("simulated rerender failure")
            return real_writer(path, frames, sample_rate)

        fake_result = (
            self.buffer.copy() * 0.5,
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch("tianlai.ensemble.write_wav_pcm24", side_effect=writer),
        ):
            with self.assertRaisesRegex(OSError, "rerender failure"):
                render_plan(self.plan, directory, write_stems=True)
        after = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_post_render_analysis_failure_leaves_previous_generation_unchanged(
        self,
    ) -> None:
        directory = self.root / "failed-post-render-analysis"
        self._render(directory, write_stems=True)
        before = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        fake_result = (
            self.buffer.copy() * 0.5,
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch(
                "tianlai.ensemble.analyze_rendered_wav",
                side_effect=RuntimeError("simulated post-render failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "post-render failure"):
                render_plan(self.plan, directory, write_stems=True)
        after = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_blocking_post_render_result_is_not_published(self) -> None:
        directory = self.root / "blocked-post-render-result"
        self._render(directory, write_stems=True)
        before = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        fake_result = (
            self.buffer.copy() * 0.5,
            1,
            _sha256(self.manifest),
        )
        blocked_report = {
            "format": ensemble_module.POST_RENDER_CHECK_FORMAT,
            "version": ensemble_module.POST_RENDER_CHECK_VERSION,
            "summary": {
                "status": "blocked",
                "can_proceed": False,
                "blocking_count": 1,
            },
        }
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch(
                "tianlai.ensemble.analyze_rendered_wav",
                return_value=blocked_report,
            ),
            patch("tianlai.ensemble.write_post_render_check") as writer,
        ):
            with self.assertRaisesRegex(RuntimeError, "未通过"):
                render_plan(self.plan, directory, write_stems=True)
            writer.assert_not_called()
        after = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_successful_no_stems_rerender_removes_previous_stem_directory(
        self,
    ) -> None:
        directory = self.root / "remove-stems"
        self._render(directory, write_stems=True)
        stale = directory / "分轨" / "stale.wav"
        stale.write_bytes(b"old")
        self.assertTrue(stale.is_file())

        self._render(directory, write_stems=False)

        self.assertFalse((directory / "分轨").exists())
        receipt = json.loads(
            (directory / "渲染回执.json").read_text(encoding="utf-8")
        )
        self.assertFalse(receipt["stems"][0]["wav"]["written"])

    def test_publish_failure_rolls_back_the_previous_complete_generation(
        self,
    ) -> None:
        directory = self.root / "publish-rollback"
        self._render(directory, write_stems=True)
        before = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        real_replace = __import__("os").replace
        failed = False

        def replace_once(source, target):
            nonlocal failed
            source_path = Path(source)
            target_path = Path(target)
            if (
                not failed
                and source_path.name == "合奏.wav"
                and target_path.resolve(strict=False)
                == (directory / "合奏.wav").resolve(strict=False)
            ):
                failed = True
                raise OSError("simulated publish failure")
            return real_replace(source, target)

        fake_result = (
            self.buffer.copy() * 0.5,
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch("tianlai.ensemble.os.replace", side_effect=replace_once),
        ):
            with self.assertRaisesRegex(OSError, "publish failure"):
                render_plan(self.plan, directory, write_stems=True)
        after = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_post_render_check_publish_failure_rolls_back_previous_generation(
        self,
    ) -> None:
        directory = self.root / "post-render-publish-rollback"
        self._render(directory, write_stems=True)
        before = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        real_replace = __import__("os").replace
        failed = False

        def replace_once(source, target):
            nonlocal failed
            source_path = Path(source)
            target_path = Path(target)
            if (
                not failed
                and source_path.name == "渲染后自检.json"
                and target_path.resolve(strict=False)
                == (directory / "渲染后自检.json").resolve(strict=False)
            ):
                failed = True
                raise OSError("simulated post-render publish failure")
            return real_replace(source, target)

        fake_result = (
            self.buffer.copy() * 0.5,
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch("tianlai.ensemble.os.replace", side_effect=replace_once),
        ):
            with self.assertRaisesRegex(OSError, "post-render publish failure"):
                render_plan(self.plan, directory, write_stems=True)
        after = {
            path.relative_to(directory).as_posix(): _sha256(path)
            for path in directory.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    def test_atomic_replace_failure_leaves_no_partial_receipt(self) -> None:
        directory = self.root / "failed-receipt"
        directory.mkdir()
        receipt_path = directory / "渲染回执.json"
        previous = b'{"previous":"valid"}\n'
        receipt_path.write_bytes(previous)
        fake_result = (
            self.buffer.copy(),
            1,
            _sha256(self.manifest),
        )
        with (
            patch("tianlai.ensemble._render_part", return_value=fake_result),
            patch("tianlai.ensemble.os.replace", side_effect=OSError("replace")),
        ):
            with self.assertRaisesRegex(OSError, "replace"):
                render_plan(self.plan, directory, write_stems=False)
        self.assertEqual(receipt_path.read_bytes(), previous)
        self.assertEqual(list(directory.glob(".渲染回执.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
