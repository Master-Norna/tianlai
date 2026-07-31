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
from tianlai.events import parse_performance_document
from tianlai.stem_cache import StemCache


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _FakePlan:
    def __init__(
        self,
        manifest_path: Path,
        *,
        sample_rate: int = 8_000,
        duration_seconds: float = 0.01,
        gain_db: float = -6.0,
        pan: float = 0.0,
        override_map: dict | None = None,
    ) -> None:
        capability = SimpleNamespace(
            manifest_path=str(manifest_path),
            relative_path="测试乐器/缓存乐器",
            quality_tier="formal",
            collaboration_review_status="untested",
            license_status="approved",
        )
        executor = SimpleNamespace(
            executor_id="cached_part",
            part_id="part",
            capability=capability,
            override_map=dict(override_map or {}),
            gain_db=gain_db,
            pan=pan,
            seat=SimpleNamespace(distance_m=3.0),
        )
        performance = {
            "sample_rate": sample_rate,
            "channels": 2,
            "duration_seconds": duration_seconds,
            "tail_seconds": 0.0,
            "events": [],
        }
        self.parts = (
            SimpleNamespace(
                executor=executor,
                performance=performance,
                gain_envelope=(),
            ),
        )
        self.sample_rate = sample_rate
        self.duration_seconds = duration_seconds

    def to_dict(self) -> dict:
        part = self.parts[0]
        executor = part.executor
        return {
            "title": "stem cache",
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "parts": [
                {
                    "executor_id": executor.executor_id,
                    "instrument": executor.capability.relative_path,
                    "performance": part.performance,
                    "override": executor.override_map,
                    "gain_db": executor.gain_db,
                    "pan": executor.pan,
                }
            ],
        }


class _FakeSpace:
    def to_dict(self) -> dict:
        return {"name": "cache-test-hall", "wet_db": -18.0}

    def effective_filter_frequencies(
        self,
        sample_rate: int,
    ) -> tuple[float, float]:
        return 100.0, min(3_000.0, sample_rate * 0.49)

    def tail_seconds(self, sample_rate: int) -> float:
        return 0.0

    def send_scale(self, distance_m: float) -> float:
        return 0.5


class EnsembleStemCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cache = self.root / "stem-cache"
        self.manifest = self.root / "instrument.json"
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "Cache Instrument",
                    "upstream": "Cache Test Samples",
                    "creator": "Test Recorder",
                    "origin": "https://example.invalid/cache",
                    "license": "CC0-1.0",
                    "license_status": "approved",
                    "provenance_kind": "project_authored_dsp",
                    "implementation_license": "Apache-2.0",
                    "external_audio_assets": [],
                    "audio_asset_license": "not_applicable",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _expected_raw(part: object) -> np.ndarray:
        document = parse_performance_document(part.performance)
        values = np.linspace(
            -0.2,
            0.2,
            document.total_samples * 2,
            dtype=np.float32,
        )
        return values.reshape((document.total_samples, 2))

    def _fake_render(
        self,
        part: object,
        sample_rate: int,
    ) -> tuple[np.ndarray, int, str]:
        self.assertEqual(part.performance["sample_rate"], sample_rate)
        return (
            self._expected_raw(part),
            2,
            _sha256(Path(part.executor.capability.manifest_path)),
        )

    @staticmethod
    def _public_artifacts(result: object) -> dict[str, bytes]:
        stem_path = result.stems[0].path
        assert stem_path is not None
        paths = {
            "mix": Path(result.mix_path),
            "stem": Path(stem_path),
            "receipt": Path(result.receipt_path),
            "license_json": Path(result.license_sidecar_path),
            "license_text": Path(result.attribution_path),
        }
        return {name: path.read_bytes() for name, path in paths.items()}

    def test_cold_then_hot_is_byte_identical_and_reports_hit(self) -> None:
        plan = _FakePlan(self.manifest)
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value={
                    "runtime": "stable",
                    "runtime_asset_graph": {"file_count": 0},
                },
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ) as render_part,
        ):
            cold = render_plan(
                plan,
                self.root / "cold",
                stem_cache_directory=self.cache,
            )
            cold_artifacts = self._public_artifacts(cold)
            self.assertEqual(render_part.call_count, 1)
            self.assertEqual(cold.stem_cache["misses"], 1)
            self.assertEqual(cold.stem_cache["writes"], 1)
            self.assertEqual(cold.stem_cache["hits"], 0)

            hot = render_plan(
                plan,
                self.root / "hot",
                stem_cache_directory=self.cache,
            )
            refreshed = render_plan(
                plan,
                self.root / "refreshed",
                stem_cache_directory=self.cache,
                refresh_stem_cache=True,
            )

        self.assertEqual(render_part.call_count, 2)
        self.assertEqual(hot.stem_cache["hits"], 1)
        self.assertEqual(hot.stem_cache["misses"], 0)
        self.assertEqual(hot.stem_cache["writes"], 0)
        self.assertEqual(self._public_artifacts(hot), cold_artifacts)
        self.assertEqual(refreshed.stem_cache["hits"], 0)
        self.assertEqual(refreshed.stem_cache["misses"], 1)
        self.assertEqual(refreshed.stem_cache["write_skips"], 1)
        self.assertEqual(
            self._public_artifacts(refreshed),
            cold_artifacts,
        )
        receipt = json.loads(Path(hot.receipt_path).read_text(encoding="utf-8"))
        self.assertNotIn("stem_cache", receipt)

    def test_mix_controls_and_hall_share_unmodified_owned_raw_stem(
        self,
    ) -> None:
        first_plan = _FakePlan(
            self.manifest,
            gain_db=-12.0,
            pan=-0.5,
        )
        second_plan = _FakePlan(
            self.manifest,
            gain_db=-3.0,
            pan=0.75,
        )
        expected = self._expected_raw(first_plan.parts[0])
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value={
                    "runtime": "stable",
                    "runtime_asset_graph": {"file_count": 0},
                },
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ) as render_part,
            patch(
                "tianlai.space.render_reverb_stereo",
                side_effect=lambda left, right, sample_rate, space: (
                    np.zeros_like(left),
                    np.zeros_like(right),
                ),
            ),
        ):
            cold = render_plan(
                first_plan,
                self.root / "mix-cold",
                master_gain_db=-2.0,
                stem_cache_directory=self.cache,
            )
            changed_mix = render_plan(
                second_plan,
                self.root / "mix-controls",
                master_gain_db=-7.0,
                stem_cache_directory=self.cache,
            )
            changed_hall = render_plan(
                second_plan,
                self.root / "mix-hall",
                master_gain_db=-7.0,
                space=_FakeSpace(),
                stem_cache_directory=self.cache,
            )

        self.assertEqual(render_part.call_count, 1)
        self.assertEqual(cold.stem_cache["writes"], 1)
        self.assertEqual(changed_mix.stem_cache["hits"], 1)
        self.assertEqual(changed_hall.stem_cache["hits"], 1)

        metadata_path = next(self.cache.rglob("*.json"))
        loaded = StemCache(self.cache).load(metadata_path.stem)
        self.assertTrue(loaded.hit)
        assert loaded.audio is not None
        self.assertTrue(loaded.audio.flags.owndata)
        self.assertTrue(loaded.audio.flags.writeable)
        np.testing.assert_array_equal(loaded.audio, expected)

        # A warm render applies assignment gain in place.  Loading once more
        # proves that operation touched only the owned copy, not cached raw PCM.
        loaded.audio[:] = 0.0
        reloaded = StemCache(self.cache).load(metadata_path.stem)
        self.assertTrue(reloaded.hit)
        assert reloaded.audio is not None
        np.testing.assert_array_equal(reloaded.audio, expected)

    def test_performance_override_and_sample_rate_each_invalidate(self) -> None:
        plans = (
            _FakePlan(self.manifest),
            _FakePlan(self.manifest, duration_seconds=0.012),
            _FakePlan(
                self.manifest,
                override_map={"release_seconds": 0.25},
            ),
            _FakePlan(
                self.manifest,
                sample_rate=9_000,
            ),
        )
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value={
                    "runtime": "stable",
                    "runtime_asset_graph": {"file_count": 0},
                },
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ) as render_part,
        ):
            results = [
                render_plan(
                    plan,
                    self.root / f"invalidate-{index}",
                    stem_cache_directory=self.cache,
                )
                for index, plan in enumerate(plans)
            ]

        self.assertEqual(render_part.call_count, len(plans))
        for result in results:
            self.assertEqual(result.stem_cache["hits"], 0)
            self.assertEqual(result.stem_cache["misses"], 1)
            self.assertEqual(result.stem_cache["writes"], 1)
        self.assertEqual(len(tuple(self.cache.rglob("*.json"))), len(plans))

    def test_corrupt_raw_and_metadata_fall_back_then_repair(self) -> None:
        plan = _FakePlan(self.manifest)
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value={
                    "runtime": "stable",
                    "runtime_asset_graph": {"file_count": 0},
                },
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ) as render_part,
        ):
            cold = render_plan(
                plan,
                self.root / "corrupt-cold",
                stem_cache_directory=self.cache,
            )
            baseline_mix = Path(cold.mix_path).read_bytes()
            audio_path = next(self.cache.rglob("*.f32le"))
            metadata_path = next(self.cache.rglob("*.json"))

            audio_path.write_bytes(b"corrupt raw payload")
            raw_repair = render_plan(
                plan,
                self.root / "corrupt-raw-repair",
                stem_cache_directory=self.cache,
            )
            self.assertEqual(render_part.call_count, 2)
            self.assertEqual(raw_repair.stem_cache["corrupt_fallbacks"], 1)
            self.assertEqual(raw_repair.stem_cache["misses"], 1)
            self.assertEqual(raw_repair.stem_cache["writes"], 1)
            self.assertEqual(Path(raw_repair.mix_path).read_bytes(), baseline_mix)

            raw_repaired_hit = render_plan(
                plan,
                self.root / "corrupt-raw-hot",
                stem_cache_directory=self.cache,
            )
            self.assertEqual(render_part.call_count, 2)
            self.assertEqual(raw_repaired_hit.stem_cache["hits"], 1)

            metadata_path.write_text("{}", encoding="utf-8")
            metadata_repair = render_plan(
                plan,
                self.root / "corrupt-metadata-repair",
                stem_cache_directory=self.cache,
            )
            self.assertEqual(render_part.call_count, 3)
            self.assertEqual(
                metadata_repair.stem_cache["corrupt_fallbacks"],
                1,
            )
            self.assertEqual(metadata_repair.stem_cache["misses"], 1)
            self.assertEqual(metadata_repair.stem_cache["writes"], 1)
            self.assertEqual(
                Path(metadata_repair.mix_path).read_bytes(),
                baseline_mix,
            )

            metadata_repaired_hit = render_plan(
                plan,
                self.root / "corrupt-metadata-hot",
                stem_cache_directory=self.cache,
            )

        self.assertEqual(render_part.call_count, 3)
        self.assertEqual(metadata_repaired_hit.stem_cache["hits"], 1)

    def test_source_tree_mismatch_bypasses_cache_fail_closed(self) -> None:
        plan = _FakePlan(self.manifest)
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value={
                    "runtime": "stable",
                    "runtime_asset_graph": {"file_count": 0},
                },
            ) as fingerprint,
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=False,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ) as render_part,
        ):
            results = (
                render_plan(
                    plan,
                    self.root / "mismatch-1",
                    stem_cache_directory=self.cache,
                ),
                render_plan(
                    plan,
                    self.root / "mismatch-2",
                    stem_cache_directory=self.cache,
                ),
            )

        self.assertEqual(render_part.call_count, 2)
        fingerprint.assert_not_called()
        for result in results:
            self.assertFalse(result.stem_cache["active"])
            self.assertEqual(result.stem_cache["total"], 1)
            self.assertEqual(result.stem_cache["accounted"], 1)
            self.assertEqual(result.stem_cache["unaccounted"], 0)
            self.assertEqual(result.stem_cache["bypassed"], 1)
            self.assertEqual(result.stem_cache["hits"], 0)
            self.assertEqual(result.stem_cache["writes"], 0)
            self.assertEqual(
                result.stem_cache["reason_counts"],
                {"producer_source_changed_restart_required": 1},
            )
            telemetry = json.loads(
                Path(result.cache_telemetry_path).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(telemetry["stem_cache"], result.stem_cache)
            self.assertEqual(
                telemetry["render_receipt"]["sha256"],
                _sha256(Path(result.receipt_path)),
            )
        self.assertFalse(self.cache.exists())

    def test_unknown_custom_backend_with_empty_asset_graph_is_not_cached(
        self,
    ) -> None:
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document.pop("provenance_kind")
        document.pop("implementation_license")
        document.pop("external_audio_assets")
        document.pop("audio_asset_license")
        document["implementation"] = "乐器.py"
        self.manifest.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.root / "乐器.py").write_text(
            "def create(**kwargs):\n    raise AssertionError('not constructed')\n",
            encoding="utf-8",
        )
        plan = _FakePlan(self.manifest)
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value={
                    "runtime": "unknown-custom",
                    "runtime_asset_graph": {"file_count": 0},
                },
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ) as render_part,
        ):
            result = render_plan(
                plan,
                self.root / "unknown-custom",
                stem_cache_directory=self.cache,
            )

        self.assertEqual(render_part.call_count, 1)
        self.assertEqual(result.stem_cache["bypassed"], 1)
        self.assertEqual(result.stem_cache["writes"], 0)
        self.assertEqual(
            result.stem_cache["reason_counts"],
            {"live_identity_unavailable": 1},
        )
        self.assertFalse(self.cache.exists())

    def test_asset_identity_change_during_render_skips_publication(self) -> None:
        plan = _FakePlan(self.manifest)
        before = {
            "runtime": "before",
            "runtime_asset_graph": {"file_count": 0},
        }
        after = {
            "runtime": "after",
            "runtime_asset_graph": {"file_count": 0},
        }
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                side_effect=(before, after),
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ) as render_part,
        ):
            result = render_plan(
                plan,
                self.root / "identity-race",
                stem_cache_directory=self.cache,
            )

        self.assertEqual(render_part.call_count, 1)
        self.assertFalse(result.stem_cache["active"])
        self.assertEqual(result.stem_cache["write_skips"], 1)
        self.assertEqual(result.stem_cache["writes"], 0)
        self.assertEqual(
            result.stem_cache["reason_counts"],
            {
                "not_found": 1,
                "live_identity_changed_during_render": 1,
            },
        )
        self.assertFalse(self.cache.exists())

    def test_real_reference_oscillator_live_fingerprint_hits(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        manifest = (
            project_root
            / "乐器"
            / "测试工具"
            / "参考振荡器"
            / "乐器.json"
        )
        self.assertTrue(manifest.is_file())
        plan = _FakePlan(
            manifest,
            duration_seconds=0.002,
            gain_db=-18.0,
        )
        cache = self.root / "live-fingerprint-cache"

        cold = render_plan(
            plan,
            self.root / "live-fingerprint-cold",
            stem_cache_directory=cache,
        )
        hot = render_plan(
            plan,
            self.root / "live-fingerprint-hot",
            stem_cache_directory=cache,
        )

        self.assertTrue(cold.stem_cache["active"])
        self.assertEqual(cold.stem_cache["misses"], 1)
        self.assertEqual(cold.stem_cache["writes"], 1)
        self.assertEqual(hot.stem_cache["hits"], 1)
        self.assertEqual(hot.stem_cache["misses"], 0)
        self.assertEqual(self._public_artifacts(hot), self._public_artifacts(cold))


if __name__ == "__main__":
    unittest.main()
