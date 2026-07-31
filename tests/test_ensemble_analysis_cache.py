from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai.analysis_cache import CollaborationAnalysisCache
from tianlai import collaboration_report as report_module
from tianlai.collaboration_report import MIX_REPORT_NAME
from tianlai.ensemble import (
    CACHE_TELEMETRY_NAME,
    RENDER_RECEIPT_NAME,
    render_plan,
    verify_render_generation,
)
from tianlai.roster import (
    BalanceRelation,
    CollaborationAnalysis,
    CollaborationSettings,
    Role,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _settings(*, target_offset_db: float = -8.0) -> CollaborationSettings:
    return CollaborationSettings(
        mode="analyze",
        analysis=CollaborationAnalysis(
            window_ms=200.0,
            hop_ms=100.0,
            gate_dbfs=-60.0,
        ),
        balance_relations=(
            BalanceRelation(
                subject="atmosphere",
                reference="melody",
                target_offset_db=target_offset_db,
                tolerance_db=1.0,
                max_suggestion_db=4.0,
            ),
        ),
        declared=True,
    )


class _Plan:
    def __init__(
        self,
        root: Path,
        *,
        cello_gain_db: float = 0.0,
        cello_seat_m: float = 3.0,
        target_offset_db: float = -8.0,
    ) -> None:
        self.sample_rate = 8_000
        self.duration_seconds = 1.0
        self.collaboration = _settings(
            target_offset_db=target_offset_db
        )
        specs = (
            (
                "cello",
                "atmosphere",
                cello_gain_db,
                cello_seat_m,
                Role("pad", "background", "atmosphere"),
            ),
            (
                "melody",
                "melody",
                0.0,
                2.0,
                Role("lead", "foreground", "melody"),
            ),
        )
        parts = []
        for executor_id, part_id, gain_db, seat_m, role in specs:
            manifest = root / f"{executor_id}.json"
            if not manifest.exists():
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
                relative_path=f"test/{executor_id}",
                quality_tier="formal",
                collaboration_review_status="untested",
                license_status="approved",
            )
            executor = SimpleNamespace(
                executor_id=executor_id,
                part_id=part_id,
                capability=capability,
                override_map={},
                gain_db=gain_db,
                pan=0.0,
                seat=SimpleNamespace(distance_m=seat_m),
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
            "title": "analysis-cache",
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "collaboration": self.collaboration.to_dict(),
            "parts": [
                {
                    "executor_id": part.executor.executor_id,
                    "part_id": part.executor.part_id,
                    "gain_db": part.executor.gain_db,
                    "seat_m": part.executor.seat.distance_m,
                    "role": part.executor.role.to_dict(),
                }
                for part in self.parts
            ],
        }


class EnsembleAnalysisCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = self.root / "analysis-cache"
        time = np.arange(8_000) / 8_000.0
        cello = 0.18 * np.sin(2.0 * np.pi * 220.0 * time)
        melody = 0.10 * np.sin(2.0 * np.pi * 440.0 * time)
        self.buffers = {
            "cello": np.column_stack((cello, cello)).astype(np.float32),
            "melody": np.column_stack((melody, melody)).astype(np.float32),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _render(
        self,
        plan: _Plan,
        output: str,
        *,
        buffers: dict[str, np.ndarray] | None = None,
    ):
        selected = self.buffers if buffers is None else buffers

        def render_part(part, _sample_rate):
            manifest = Path(part.executor.capability.manifest_path)
            return (
                selected[part.executor.executor_id].copy(),
                1,
                _sha256(manifest),
            )

        with patch(
            "tianlai.ensemble._render_part",
            side_effect=render_part,
        ):
            return render_plan(
                plan,
                self.root / output,
                write_stems=False,
                normalize_peak_db=-1.0,
                analysis_cache_directory=self.cache,
            )

    def test_cold_hot_reports_and_receipts_are_identical(self) -> None:
        plan = _Plan(self.root)
        cold = self._render(plan, "cold")
        hot = self._render(plan, "hot")

        self.assertEqual(
            Path(cold.mix_path).read_bytes(),
            Path(hot.mix_path).read_bytes(),
        )
        self.assertEqual(
            (self.root / "cold" / MIX_REPORT_NAME).read_bytes(),
            (self.root / "hot" / MIX_REPORT_NAME).read_bytes(),
        )
        self.assertEqual(
            (self.root / "cold" / RENDER_RECEIPT_NAME).read_bytes(),
            (self.root / "hot" / RENDER_RECEIPT_NAME).read_bytes(),
        )
        self.assertEqual(cold.analysis_cache["stem"]["misses"], 2)
        self.assertEqual(cold.analysis_cache["relation"]["misses"], 1)
        self.assertEqual(hot.analysis_cache["stem"]["hits"], 2)
        self.assertEqual(hot.analysis_cache["relation"]["hits"], 1)
        self.assertEqual(
            hot.analysis_cache["performed_fft_input_frame_visits"],
            0,
        )
        self.assertEqual(
            hot.analysis_cache["avoided_fft_input_frame_visits"],
            hot.mix_report["analysis"]["workload"][
                "fft_input_frame_visits"
            ],
        )

    def test_seat_only_hits_every_analysis_entry(self) -> None:
        self._render(_Plan(self.root, cello_seat_m=3.0), "seat-cold")
        changed = self._render(
            _Plan(self.root, cello_seat_m=9.0),
            "seat-hot",
        )

        self.assertEqual(changed.analysis_cache["stem"]["hits"], 2)
        self.assertEqual(changed.analysis_cache["stem"]["misses"], 0)
        self.assertEqual(changed.analysis_cache["relation"]["hits"], 1)
        self.assertEqual(changed.analysis_cache["relation"]["misses"], 0)

    def test_one_gain_change_invalidates_one_stem_and_its_relation(self) -> None:
        self._render(_Plan(self.root), "gain-cold")
        changed = self._render(
            _Plan(self.root, cello_gain_db=-3.0),
            "gain-changed",
        )

        self.assertEqual(changed.analysis_cache["stem"]["hits"], 1)
        self.assertEqual(changed.analysis_cache["stem"]["misses"], 1)
        self.assertEqual(changed.analysis_cache["relation"]["hits"], 0)
        self.assertEqual(changed.analysis_cache["relation"]["misses"], 1)

    def test_relation_change_reuses_stems_but_invalidates_relation(self) -> None:
        self._render(_Plan(self.root), "relation-cold")
        changed = self._render(
            _Plan(self.root, target_offset_db=-6.0),
            "relation-changed",
        )

        self.assertEqual(changed.analysis_cache["stem"]["hits"], 2)
        self.assertEqual(changed.analysis_cache["relation"]["hits"], 0)
        self.assertEqual(changed.analysis_cache["relation"]["misses"], 1)

    def test_audio_change_invalidates_changed_stem_and_relation(self) -> None:
        plan = _Plan(self.root)
        self._render(plan, "audio-cold")
        changed_buffers = dict(self.buffers)
        changed_buffers["cello"] = (
            self.buffers["cello"] * np.float32(0.75)
        )
        changed = self._render(
            plan,
            "audio-changed",
            buffers=changed_buffers,
        )

        self.assertEqual(changed.analysis_cache["stem"]["hits"], 1)
        self.assertEqual(changed.analysis_cache["stem"]["misses"], 1)
        self.assertEqual(changed.analysis_cache["relation"]["misses"], 1)

    def test_corrupt_entry_falls_back_repairs_and_preserves_output(self) -> None:
        plan = _Plan(self.root)
        cold = self._render(plan, "corrupt-cold")
        entry = next(
            path
            for path in self.cache.rglob("*.json")
            if json.loads(path.read_text(encoding="utf-8"))["kind"]
            == "stem_metrics"
        )
        entry.write_bytes(b"{not-json")

        repaired = self._render(plan, "corrupt-repaired")
        self.assertEqual(
            Path(cold.mix_path).read_bytes(),
            Path(repaired.mix_path).read_bytes(),
        )
        self.assertEqual(
            repaired.analysis_cache["stem"]["corrupt_fallbacks"],
            1,
        )
        self.assertEqual(repaired.analysis_cache["stem"]["misses"], 1)
        self.assertEqual(repaired.analysis_cache["stem"]["hits"], 1)
        self.assertGreaterEqual(
            repaired.analysis_cache["stem"]["writes"],
            1,
        )

    def test_deeply_nested_entry_falls_back_instead_of_aborting(self) -> None:
        plan = _Plan(self.root)
        cold = self._render(plan, "deep-cold")
        entry = next(
            path
            for path in self.cache.rglob("*.json")
            if json.loads(path.read_text(encoding="utf-8"))["kind"]
            == "stem_metrics"
        )
        entry.write_text(
            '{"nested":' + "[" * 1_500 + "0" + "]" * 1_500 + "}",
            encoding="utf-8",
        )

        repaired = self._render(plan, "deep-repaired")

        self.assertEqual(
            Path(cold.mix_path).read_bytes(),
            Path(repaired.mix_path).read_bytes(),
        )
        self.assertEqual(
            repaired.analysis_cache["stem"]["corrupt_fallbacks"],
            1,
        )

    def test_source_change_after_last_stem_analysis_is_explained(self) -> None:
        state = {"matches": True, "analyzed": 0}
        real_analyze = report_module.analyze_track

        def analyze_then_change_on_last(*args, **kwargs):
            result = real_analyze(*args, **kwargs)
            state["analyzed"] += 1
            if state["analyzed"] == 2:
                state["matches"] = False
            return result

        with (
            patch(
                "tianlai.collaboration_report.current_source_tree_matches",
                side_effect=lambda: state["matches"],
            ),
            patch(
                "tianlai.collaboration_report.analyze_track",
                side_effect=analyze_then_change_on_last,
            ),
        ):
            result = self._render(_Plan(self.root), "source-drift-stem")

        self.assertFalse(result.analysis_cache["active"])
        self.assertEqual(result.analysis_cache["stem"]["write_skips"], 1)
        self.assertEqual(
            result.analysis_cache["reason_counts"][
                "stem:producer_source_changed_before_store"
            ],
            1,
        )
        self.assertEqual(
            result.analysis_cache["stem"]["unaccounted"],
            0,
        )

    def test_source_change_after_relation_analysis_is_explained(self) -> None:
        state = {"matches": True}
        real_analyze = report_module.analyze_temporal_balance

        def analyze_then_change(*args, **kwargs):
            result = real_analyze(*args, **kwargs)
            state["matches"] = False
            return result

        with (
            patch(
                "tianlai.collaboration_report.current_source_tree_matches",
                side_effect=lambda: state["matches"],
            ),
            patch(
                "tianlai.collaboration_report.analyze_temporal_balance",
                side_effect=analyze_then_change,
            ),
        ):
            result = self._render(_Plan(self.root), "source-drift-relation")

        self.assertFalse(result.analysis_cache["active"])
        self.assertEqual(
            result.analysis_cache["relation"]["write_skips"],
            1,
        )
        self.assertEqual(
            result.analysis_cache["reason_counts"][
                "relation:producer_source_changed_before_store"
            ],
            1,
        )
        self.assertEqual(
            result.analysis_cache["relation"]["unaccounted"],
            0,
        )

    def test_concurrent_publication_is_atomic_and_non_throwing(self) -> None:
        cache = CollaborationAnalysisCache(self.cache)
        identity = {
            "format": "test.identity",
            "version": 1,
            "audio": {"sha256": "a" * 64},
        }
        payload = {"value": 1}

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = tuple(
                pool.map(
                    lambda _index: cache.store(
                        identity,
                        kind="test",
                        payload=payload,
                    ),
                    range(16),
                )
            )

        self.assertTrue(
            {result.status for result in results}
            <= {"stored", "exists", "busy"}
        )
        lookup = cache.load(identity, kind="test")
        self.assertTrue(lookup.hit)
        self.assertEqual(lookup.payload, payload)

    def test_persisted_telemetry_is_closed_and_binds_receipt(self) -> None:
        result = self._render(_Plan(self.root), "telemetry")
        telemetry_path = Path(result.cache_telemetry_path)
        telemetry = json.loads(
            telemetry_path.read_text(encoding="utf-8")
        )
        receipt_path = self.root / "telemetry" / RENDER_RECEIPT_NAME
        self.assertEqual(
            telemetry["render_receipt"]["sha256"],
            _sha256(receipt_path),
        )
        for section in (
            telemetry["analysis_cache"]["stem"],
            telemetry["analysis_cache"]["relation"],
        ):
            self.assertEqual(
                section["total"],
                section["accounted"] + section["unaccounted"],
            )
            self.assertEqual(
                section["accounted"],
                section["hits"]
                + section["misses"]
                + section["bypassed"],
            )
        verify_render_generation(self.root / "telemetry")

        telemetry["analysis_cache"]["stem"]["accounted"] += 1
        telemetry_path.write_text(
            json.dumps(telemetry),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "账本不闭合"):
            verify_render_generation(self.root / "telemetry")


if __name__ == "__main__":
    unittest.main()
