from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai import ensemble as ensemble_module
from tianlai.collaboration_report import (
    MIX_REPORT_NAME,
    CollaborationReportBuilder,
)
from tianlai.ensemble import render_plan
from tianlai.roster import (
    BalanceRelation,
    CollaborationAnalysis,
    CollaborationSettings,
    PartGroup,
    Role,
)


class _Plan:
    def __init__(
        self,
        root: Path,
        collaboration: CollaborationSettings,
        part_specs=None,
    ) -> None:
        self.sample_rate = 8000
        self.duration_seconds = 1.0
        self.collaboration = collaboration
        parts = []
        if part_specs is None:
            part_specs = (
                (
                    "cello",
                    "atmosphere",
                    Role("pad", "background", "大提琴氛围"),
                ),
                (
                    "melody",
                    "melody",
                    Role("lead", "foreground", "主旋律"),
                ),
            )
        for executor_id, part_id, role in part_specs:
            manifest = root / f"{executor_id}.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": executor_id,
                        "upstream": "test",
                        "creator": "test",
                        "origin": "https://example.invalid/test",
                        "license": "CC0-1.0",
                        "license_status": "approved",
                    }
                ),
                encoding="utf-8",
            )
            capability = SimpleNamespace(
                manifest_path=str(manifest),
                relative_path=f"测试/{executor_id}",
                quality_tier="formal",
                collaboration_review_status="untested",
                license_status="approved",
            )
            executor = SimpleNamespace(
                executor_id=executor_id,
                part_id=part_id,
                capability=capability,
                override_map={},
                gain_db=0.0,
                pan=0.0,
                seat=SimpleNamespace(distance_m=3.0),
                role=role,
            )
            parts.append(
                SimpleNamespace(
                    executor=executor,
                    performance={},
                    gain_envelope=(),
                )
            )
        self.parts = tuple(parts)

    def to_dict(self) -> dict:
        return {
            "title": "mix-report",
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "collaboration": self.collaboration.to_dict(),
            "parts": [
                {
                    "executor_id": part.executor.executor_id,
                    "part_id": part.executor.part_id,
                    "role": part.executor.role.to_dict(),
                }
                for part in self.parts
            ],
        }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _settings(mode: str) -> CollaborationSettings:
    return CollaborationSettings(
        mode=mode,
        analysis=CollaborationAnalysis(
            window_ms=200.0,
            hop_ms=100.0,
            gate_dbfs=-60.0,
        ),
        balance_relations=(
            BalanceRelation(
                subject="atmosphere",
                reference="melody",
                target_offset_db=-8.0,
                tolerance_db=1.0,
                max_suggestion_db=4.0,
            ),
        ),
        declared=True,
    )


class EnsembleMixReportIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        time = np.arange(8000) / 8000.0
        cello = 0.4 * np.sin(2.0 * np.pi * 220.0 * time)
        melody = 0.1 * np.sin(2.0 * np.pi * 440.0 * time)
        self.buffers = {
            "cello": np.column_stack((cello, cello)).astype(np.float32),
            "melody": np.column_stack((melody, melody)).astype(np.float32),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _render(self, mode: str, directory: Path):
        plan = _Plan(self.root, _settings(mode))

        def render_part(part, _sample_rate):
            manifest = Path(part.executor.capability.manifest_path)
            return (
                self.buffers[part.executor.executor_id].copy(),
                1,
                _sha256(manifest),
            )

        with patch("tianlai.ensemble._render_part", side_effect=render_part):
            return render_plan(
                plan,
                directory,
                write_stems=False,
                normalize_peak_db=-1.0,
            )

    def test_suggest_mode_writes_and_receipt_binds_mix_report(self) -> None:
        directory = self.root / "suggest"

        result = self._render("suggest", directory)

        report_path = directory / MIX_REPORT_NAME
        self.assertEqual(result.mix_report_path, str(report_path.resolve()))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        relation = report["balance_relations"][0]
        self.assertEqual(relation["status"], "outside_tolerance")
        self.assertEqual(
            relation["suggested_subject_gain_adjustment_db"],
            -4.0,
        )
        self.assertFalse(report["audio_modified"])
        self.assertEqual(
            set(report["stage_metrics"]),
            {
                "post_pan_pre_space",
                "post_space_pre_master",
                "final",
            },
        )
        self.assertEqual(
            report["stage_metrics"]["final"]["frame_count"],
            result.frame_count,
        )
        receipt = json.loads(
            (directory / "渲染回执.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            receipt["collaboration"],
            {
                "plan_mode": "suggest",
                "requested_override": None,
                "effective_mode": "suggest",
                "audio_modified": False,
                "report_enabled": True,
            },
        )
        self.assertEqual(receipt["mix_report"]["path"], MIX_REPORT_NAME)
        self.assertEqual(
            receipt["mix_report"]["sha256"],
            _sha256(report_path),
        )

    def test_identity_mix_reuses_stage_scan_without_aliasing_reports(self) -> None:
        plan = _Plan(self.root, _settings("analyze"))

        def render_part(part, _sample_rate):
            manifest = Path(part.executor.capability.manifest_path)
            return (
                self.buffers[part.executor.executor_id].copy(),
                1,
                _sha256(manifest),
            )

        with (
            patch("tianlai.ensemble._render_part", side_effect=render_part),
            patch.object(
                ensemble_module,
                "analyze_stereo_stage",
                wraps=ensemble_module.analyze_stereo_stage,
            ) as analyze_stage,
        ):
            result = render_plan(
                plan,
                self.root / "identity-stage-scan",
                write_stems=False,
            )

        self.assertEqual(analyze_stage.call_count, 1)
        stages = result.mix_report["stage_metrics"]
        self.assertEqual(stages["post_pan_pre_space"], stages["post_space_pre_master"])
        self.assertEqual(stages["post_space_pre_master"], stages["final"])
        self.assertIsNot(stages["post_pan_pre_space"], stages["post_space_pre_master"])
        self.assertIsNot(stages["post_space_pre_master"], stages["final"])

    def test_explicit_part_group_is_an_analysis_view_not_an_audio_bus(
        self,
    ) -> None:
        settings = CollaborationSettings(
            mode="analyze",
            analysis=CollaborationAnalysis(
                window_ms=200.0,
                hop_ms=100.0,
                gate_dbfs=-60.0,
            ),
            balance_relations=(
                BalanceRelation("piano", "melody", 0.0, 0.1, 4.0),
            ),
            declared=True,
            part_groups=(
                PartGroup("piano", ("piano_left", "piano_right")),
            ),
        )
        plan = _Plan(
            self.root,
            settings,
            part_specs=(
                (
                    "left",
                    "piano_left",
                    Role("harmony", "midground", "钢琴左手"),
                ),
                (
                    "right",
                    "piano_right",
                    Role("harmony", "midground", "钢琴右手"),
                ),
                (
                    "melody",
                    "melody",
                    Role("lead", "foreground", "旋律"),
                ),
            ),
        )
        time = np.arange(8000) / 8000.0
        tone = np.sin(2.0 * np.pi * 440.0 * time)
        buffers = {
            "left": np.column_stack((0.05 * tone, 0.05 * tone)).astype(
                np.float32
            ),
            "right": np.column_stack((0.05 * tone, 0.05 * tone)).astype(
                np.float32
            ),
            "melody": np.column_stack((0.1 * tone, 0.1 * tone)).astype(
                np.float32
            ),
        }

        def render_part(part, _sample_rate):
            manifest = Path(part.executor.capability.manifest_path)
            return (
                buffers[part.executor.executor_id].copy(),
                1,
                _sha256(manifest),
            )

        with patch("tianlai.ensemble._render_part", side_effect=render_part):
            analyzed = render_plan(
                plan,
                self.root / "group-analyzed",
                write_stems=False,
                normalize_peak_db=-1.0,
            )
        with patch("tianlai.ensemble._render_part", side_effect=render_part):
            manual = render_plan(
                plan,
                self.root / "group-manual",
                write_stems=False,
                normalize_peak_db=-1.0,
                collaboration_mode="manual",
            )

        relation = analyzed.mix_report["balance_relations"][0]
        self.assertEqual(relation["status"], "within_tolerance")
        self.assertEqual(
            relation["subject_endpoint"],
            {
                "endpoint_kind": "part_group",
                "expanded_parts": ["piano_left", "piano_right"],
            },
        )
        self.assertEqual(
            Path(analyzed.mix_path).read_bytes(),
            Path(manual.mix_path).read_bytes(),
        )
        self.assertEqual(
            list(self.root.glob(".collaboration-analysis.*")),
            [],
        )

    def test_antiphase_final_mix_is_only_a_mono_fold_candidate(self) -> None:
        for buffer in self.buffers.values():
            buffer[:, 1] *= -1.0

        result = self._render("analyze", self.root / "antiphase")
        report = result.mix_report
        self.assertIsNotNone(report)
        final = report["stage_metrics"]["final"]

        self.assertTrue(final["mono_fold_silent"])
        self.assertIsNone(final["mono_fold_delta_db"])
        self.assertEqual(report["summary"]["stage_candidate_count"], 1)
        self.assertIn(
            "final_mix_mono_fold_candidate",
            {warning["code"] for warning in report["warnings"]},
        )
        self.assertFalse(report["audio_modified"])

    def test_manual_override_preserves_audio_and_removes_old_report(self) -> None:
        analyzed = self._render("analyze", self.root / "analyzed")
        plan = _Plan(self.root, _settings("analyze"))

        def render_part(part, _sample_rate):
            manifest = Path(part.executor.capability.manifest_path)
            return (
                self.buffers[part.executor.executor_id].copy(),
                1,
                _sha256(manifest),
            )

        manual_directory = self.root / "manual"
        with patch("tianlai.ensemble._render_part", side_effect=render_part):
            manual = render_plan(
                plan,
                manual_directory,
                write_stems=False,
                normalize_peak_db=-1.0,
                collaboration_mode="manual",
            )
        self.assertIsNone(manual.mix_report_path)
        self.assertEqual(manual.collaboration_mode, "manual")
        self.assertFalse((manual_directory / MIX_REPORT_NAME).exists())
        manual_receipt = json.loads(
            (manual_directory / "渲染回执.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manual_receipt["collaboration"],
            {
                "plan_mode": "analyze",
                "requested_override": "manual",
                "effective_mode": "manual",
                "audio_modified": False,
                "report_enabled": False,
            },
        )
        stored_plan = json.loads(
            (manual_directory / "演奏计划.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored_plan["collaboration"]["mode"], "analyze")
        self.assertEqual(
            Path(analyzed.mix_path).read_bytes(),
            Path(manual.mix_path).read_bytes(),
        )

        # Reusing the same directory in manual mode must not expose a stale
        # report from the previous analyzed generation.
        reused = self.root / "reused"
        self._render("analyze", reused)
        with patch("tianlai.ensemble._render_part", side_effect=render_part):
            render_plan(
                plan,
                reused,
                write_stems=False,
                normalize_peak_db=-1.0,
                collaboration_mode="manual",
            )
        self.assertFalse((reused / MIX_REPORT_NAME).exists())

    def test_render_failure_immediately_closes_relation_scratch(self) -> None:
        plan = _Plan(self.root, _settings("analyze"))
        real_close = CollaborationReportBuilder.close
        closed_scratch_handle_counts: list[int] = []

        def render_part(part, _sample_rate):
            if part.executor.executor_id == "melody":
                raise RuntimeError("deliberate second-stem failure")
            manifest = Path(part.executor.capability.manifest_path)
            return (
                self.buffers[part.executor.executor_id].copy(),
                1,
                _sha256(manifest),
            )

        def tracked_close(builder):
            closed_scratch_handle_counts.append(len(builder._scratch_handles))
            real_close(builder)

        with (
            patch(
                "tianlai.ensemble._render_part",
                side_effect=render_part,
            ),
            patch.object(
                CollaborationReportBuilder,
                "close",
                new=tracked_close,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "deliberate second-stem failure",
            ),
        ):
            render_plan(
                plan,
                self.root / "failed",
                write_stems=False,
            )

        self.assertEqual(closed_scratch_handle_counts, [1])
        self.assertEqual(
            list(self.root.glob(".collaboration-analysis.*")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
