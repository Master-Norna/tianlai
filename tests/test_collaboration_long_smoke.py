from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai.ensemble import render_plan
from tianlai.roster import (
    BalanceRelation,
    CollaborationAnalysis,
    CollaborationSettings,
    Role,
)


SAMPLE_RATE = 8_000
DURATION_SECONDS = 60.0
FRAME_COUNT = round(SAMPLE_RATE * DURATION_SECONDS)
EXECUTOR_COUNT = 8
RELATION_COUNT = 6
RELATION_PART_COUNT = 7


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _LongPlan:
    def __init__(self, root: Path) -> None:
        self.sample_rate = SAMPLE_RATE
        self.duration_seconds = DURATION_SECONDS
        self.collaboration = CollaborationSettings(
            mode="analyze",
            analysis=CollaborationAnalysis(
                window_ms=400.0,
                hop_ms=100.0,
                gate_dbfs=-60.0,
            ),
            balance_relations=tuple(
                BalanceRelation(
                    subject=f"part-{index}",
                    reference="part-0",
                    target_offset_db=-float(index),
                    tolerance_db=3.0,
                    max_suggestion_db=3.0,
                )
                for index in range(1, RELATION_COUNT + 1)
            ),
            declared=True,
        )
        parts = []
        for index in range(EXECUTOR_COUNT):
            manifest = root / f"instrument-{index}.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": f"instrument-{index}",
                        "upstream": "test",
                        "creator": "test",
                        "origin": "https://example.invalid/long-smoke",
                        "license": "CC0-1.0",
                        "license_status": "approved",
                    }
                ),
                encoding="utf-8",
            )
            executor = SimpleNamespace(
                executor_id=f"executor-{index}",
                part_id=f"part-{index}",
                capability=SimpleNamespace(
                    manifest_path=str(manifest),
                    relative_path=f"测试/长曲/{index}",
                    quality_tier="formal",
                    collaboration_review_status="untested",
                    license_status="approved",
                ),
                override_map={},
                gain_db=-18.0,
                pan=(index - 3.5) / 7.0,
                seat=SimpleNamespace(distance_m=3.0),
                role=Role(
                    "lead" if index == 0 else "harmony",
                    "foreground" if index == 0 else "midground",
                ),
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
            "title": "60-second collaboration smoke",
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


class CollaborationLongSmokeTests(unittest.TestCase):
    def test_sixty_second_multi_relation_report_is_bounded_and_complete(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan = _LongPlan(root)
            output = root / "render"
            sample_index = np.arange(FRAME_COUNT, dtype=np.float64)

            def render_part(part, _sample_rate):
                index = int(part.executor.executor_id.rsplit("-", 1)[1])
                frequency = 110.0 + index * 37.0
                mono = (
                    0.025
                    * np.sin(
                        2.0
                        * np.pi
                        * frequency
                        * sample_index
                        / SAMPLE_RATE
                    )
                ).astype(np.float32)
                buffer = np.column_stack((mono, mono))
                manifest = Path(
                    part.executor.capability.manifest_path
                )
                return buffer, 1, _sha256(manifest)

            with patch(
                "tianlai.ensemble._render_part",
                side_effect=render_part,
            ):
                result = render_plan(
                    plan,
                    output,
                    write_stems=False,
                )

            report = result.mix_report
            self.assertIsNotNone(report)
            workload = report["analysis"]["workload"]
            self.assertEqual(workload["executor_count"], EXECUTOR_COUNT)
            self.assertEqual(workload["relation_count"], RELATION_COUNT)
            self.assertEqual(
                workload["unique_relation_part_count"],
                RELATION_PART_COUNT,
            )
            self.assertEqual(workload["window_frames"], 3_200)
            self.assertEqual(workload["stem_window_count"], 4_800)
            self.assertEqual(
                workload["stem_active_fft_window_count"],
                4_800,
            )
            self.assertEqual(
                workload["relation_shared_active_window_count"],
                3_600,
            )
            self.assertEqual(
                workload["relation_pair_fft_window_count"],
                7_200,
            )
            self.assertEqual(
                workload["fft_input_frame_visits"],
                38_400_000,
            )
            self.assertEqual(
                workload["relation_buffer_bytes"],
                RELATION_PART_COUNT * FRAME_COUNT * 2 * 4,
            )
            self.assertEqual(result.frame_count, FRAME_COUNT)
            self.assertTrue(Path(result.mix_path).is_file())
            self.assertTrue(Path(result.receipt_path).is_file())
            self.assertEqual(
                list(root.glob(".collaboration-analysis.*")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
