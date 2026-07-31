from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import soundfile as sf

from tianlai.audition_protocol import AuditionStrike, FullRangeAudition
from tianlai.dedicated_candidates import (
    generate_dedicated_audition_verification,
)


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "生成修后复验.py"
SPEC = importlib.util.spec_from_file_location("tianlai_repair_review_tool", TOOL_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import {TOOL_PATH}")
TOOL = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TOOL
SPEC.loader.exec_module(TOOL)


def _base_plan() -> FullRangeAudition:
    return FullRangeAudition(
        instrument="测试类/测试乐器",
        articulation="sustain",
        pitch_semantics="pitched_chromatic",
        range_source="test",
        declared_ranges=((60, 60),),
        gaps=(),
        sequence=(AuditionStrike(60, "sustain"),),
        tail_seconds=1.5,
        exception=None,
        document={},
    )


class RepairReviewToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.instrument_root = self.root / "乐器"
        self.output_root = self.root / "output" / "修后复验"
        self.baseline = self.root / "output" / "全音域试音"
        instrument = self.instrument_root / "测试类" / "测试乐器"
        instrument.mkdir(parents=True)
        (instrument / "乐器.json").write_text("{}\n", encoding="utf-8")
        self.baseline.mkdir(parents=True)
        (self.baseline / "keep.txt").write_text("baseline", encoding="utf-8")

        self.originals = {
            name: getattr(TOOL, name)
            for name in ("ROOT", "INSTRUMENT_ROOT", "OUTPUT_ROOT", "BASELINE_ROOT")
        }
        TOOL.ROOT = self.root
        TOOL.INSTRUMENT_ROOT = self.instrument_root
        TOOL.OUTPUT_ROOT = self.output_root
        TOOL.BASELINE_ROOT = self.baseline
        self.target = TOOL.ReviewTarget(
            "测试类/测试乐器",
            0.7,
            0.4,
            0.2,
            "听测试音。",
        )

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(TOOL, name, value)
        self.temporary.cleanup()

    @staticmethod
    def _fake_render(
        _manifest: Path,
        _events: Path,
        wav: Path,
        *,
        output_path: Path,
        coverage: list[str],
    ) -> dict:
        sample_rate = 48_000
        time = np.arange(sample_rate // 20, dtype=np.float64) / sample_rate
        mono = 0.1 * np.sin(2.0 * np.pi * 440.0 * time)
        audio = np.column_stack((mono, mono))
        wav.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(wav), audio, sample_rate, subtype="PCM_24")
        report = {
            "peak": 0.1,
            "rms": 0.07,
            "clipped_samples": 0,
            "duration_seconds": 0.05,
            "peak_active_voices": 1,
            "coverage": coverage,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("{}\n", encoding="utf-8")
        return report

    def test_generates_only_new_folder_and_preserves_baseline(self) -> None:
        with (
            mock.patch.object(TOOL, "build_full_range_audition", return_value=_base_plan()),
            mock.patch.object(
                TOOL,
                "generate_dedicated_audition_verification",
                side_effect=self._fake_render,
            ),
        ):
            entries = TOOL.generate_repair_review(targets=(self.target,))

        self.assertEqual(len(entries), 1)
        self.assertEqual(
            (self.baseline / "keep.txt").read_text(encoding="utf-8"),
            "baseline",
        )
        self.assertTrue((self.output_root / "01_测试乐器.wav").is_file())
        document = json.loads(
            (self.output_root / "_复验清单.json").read_text(encoding="utf-8")
        )
        self.assertEqual(document["protocol"], TOOL.ISOLATED_PROTOCOL_ID)
        self.assertEqual(document["instrument_count"], 1)
        self.assertEqual(document["wav_persistence"], "temporary")
        self.assertEqual(
            document["instruments"][0]["wav_persistence"],
            "temporary",
        )
        report = json.loads(
            (
                self.output_root
                / "_reports"
                / "01_测试乐器.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            report["wav"],
            "output/修后复验/01_测试乐器.wav",
        )
        self.assertEqual(
            report["events"],
            "output/修后复验/_events/01_测试乐器.events.json",
        )
        self.assertEqual(
            report["audition_protocol"],
            TOOL.ISOLATED_PROTOCOL_ID,
        )
        events = json.loads(
            (
                self.output_root
                / "_events"
                / "01_测试乐器.events.json"
            ).read_text(encoding="utf-8")
        )
        note_off = next(
            event for event in events["events"] if event["type"] == "note_off"
        )
        self.assertEqual(note_off["time"], 0.95)

    def test_existing_folder_requires_replace(self) -> None:
        self.output_root.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            TOOL.generate_repair_review(targets=(self.target,))

    def test_only_selection_accepts_short_name_and_rejects_unknown(self) -> None:
        original = TOOL.TARGETS
        try:
            TOOL.TARGETS = (self.target,)
            self.assertEqual(
                TOOL._selected_targets(("测试乐器",)),
                (self.target,),
            )
            with self.assertRaisesRegex(ValueError, "未知 --only"):
                TOOL._selected_targets(("不存在",))
        finally:
            TOOL.TARGETS = original

    def test_real_verifier_creates_nested_report_parent(self) -> None:
        """事务目录无需提前创建 `_reports`，底层核验器会负责。"""

        manifest = ROOT / "乐器" / "管弦乐" / "打击乐组" / "钢鼓" / "乐器.json"
        events = ROOT / "examples" / "钢鼓_奏法.events.json"
        wav = self.root / "render" / "steelpan.wav"
        report = self.root / "nested" / "_reports" / "steelpan.json"

        generated = generate_dedicated_audition_verification(
            manifest,
            events,
            wav,
            output_path=report,
            coverage=["nested-parent-regression"],
        )

        self.assertTrue(report.is_file())
        self.assertEqual(generated["wav_persistence"], "temporary")
        self.assertEqual(generated["audition_profile"], "fixed-example")


if __name__ == "__main__":
    unittest.main()
