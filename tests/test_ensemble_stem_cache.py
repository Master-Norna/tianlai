from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from tianlai.ensemble import render_plan
from tianlai.events import parse_performance_document
from tianlai.stem_cache import StemCache, VerifiedStemSource


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
        gain_envelope: tuple[object, ...] = (),
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
            role=None,
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
                gain_envelope=gain_envelope,
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
                    "gain_envelope": [
                        {
                            "time_seconds": point.time_seconds,
                            "offset_db": point.offset_db,
                        }
                        for point in part.gain_envelope
                    ],
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

            with patch.object(
                StemCache,
                "open_verified",
                side_effect=AssertionError(
                    "small cache hits must not create a scratch snapshot"
                ),
            ):
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

    def test_multiblock_gain_automation_hot_path_is_byte_identical(
        self,
    ) -> None:
        plan = _FakePlan(
            self.manifest,
            duration_seconds=9.0,
            gain_db=-8.0,
            pan=0.35,
            gain_envelope=(
                SimpleNamespace(time_seconds=0.0, offset_db=-3.0),
                SimpleNamespace(time_seconds=4.0, offset_db=2.0),
                SimpleNamespace(time_seconds=8.0, offset_db=-1.0),
            ),
        )
        fingerprint = {
            "runtime": "stable",
            "runtime_asset_graph": {"file_count": 0},
        }
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
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
                self.root / "automation-cold",
                stem_cache_directory=self.cache,
            )
            with patch.object(
                VerifiedStemSource,
                "materialise",
                side_effect=AssertionError("manual hit was materialised"),
            ), patch(
                "tianlai.ensemble._DIRECT_STEM_CACHE_LOAD_BYTES",
                0,
            ):
                hot = render_plan(
                    plan,
                    self.root / "automation-hot",
                    stem_cache_directory=self.cache,
                )

        self.assertEqual(render_part.call_count, 1)
        self.assertEqual(hot.stem_cache["hits"], 1)
        self.assertEqual(
            self._public_artifacts(hot),
            self._public_artifacts(cold),
        )

    def test_failed_stream_consumer_explicitly_closes_verified_source(
        self,
    ) -> None:
        plan = _FakePlan(self.manifest)
        fingerprint = {
            "runtime": "stable",
            "runtime_asset_graph": {"file_count": 0},
        }
        common = (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
        )
        with common[0], common[1], patch(
            "tianlai.ensemble._render_part",
            side_effect=self._fake_render,
        ):
            render_plan(
                plan,
                self.root / "close-cold",
                stem_cache_directory=self.cache,
            )

        opened: list[VerifiedStemSource] = []
        snapshot_directories: list[Path] = []
        original_open = StemCache.open_verified

        def tracking_open(cache: StemCache, key: str, **kwargs: object):
            snapshot_directories.append(
                Path(kwargs["snapshot_directory"])
            )
            lookup = original_open(cache, key, **kwargs)
            if lookup.source is not None:
                opened.append(lookup.source)
            return lookup

        with (
            patch("tianlai.ensemble._DIRECT_STEM_CACHE_LOAD_BYTES", 0),
            patch.object(StemCache, "open_verified", new=tracking_open),
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._consume_verified_cache_stem",
                side_effect=RuntimeError("injected consumer failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "consumer failure"):
                render_plan(
                    plan,
                    self.root / "close-hot",
                    stem_cache_directory=self.cache,
                )

        self.assertTrue(opened, "cache hit did not expose a verified source")
        self.assertTrue(
            all(source.closed for source in opened),
            "failed stream consumption left a verified source open",
        )
        self.assertTrue(
            snapshot_directories,
            "verified cache lookup did not receive a snapshot directory",
        )
        for path in snapshot_directories:
            with self.subTest(snapshot_directory=path):
                # Windows may canonicalise an ordinary RUNNER~1 ancestor to
                # its long spelling.  The staging entry is already gone here,
                # so bind its still-existing parent by filesystem identity and
                # retain the exact private-name constraint separately.
                self.assertTrue(
                    os.path.samefile(path.parent, self.root),
                    (
                        "verified snapshot escaped the render parent: "
                        f"{path.parent} != {self.root}"
                    ),
                )
                self.assertTrue(
                    path.name.startswith(".close-hot.render-stage."),
                    f"verified snapshot used an unexpected name: {path.name}",
                )

    def test_snapshot_unavailable_transparently_uses_normal_renderer(
        self,
    ) -> None:
        plan = _FakePlan(self.manifest)
        fingerprint = {
            "runtime": "stable",
            "runtime_asset_graph": {"file_count": 0},
        }
        with (
            patch("tianlai.ensemble._DIRECT_STEM_CACHE_LOAD_BYTES", 0),
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
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
                self.root / "snapshot-cold",
                stem_cache_directory=self.cache,
            )
            with patch(
                "tianlai.stem_cache.tempfile.TemporaryFile",
                side_effect=OSError("snapshot unavailable"),
            ):
                fallback = render_plan(
                    plan,
                    self.root / "snapshot-fallback",
                    stem_cache_directory=self.cache,
                )

        self.assertEqual(render_part.call_count, 2)
        self.assertEqual(fallback.stem_cache["hits"], 0)
        self.assertEqual(fallback.stem_cache["bypassed"], 1)
        self.assertIn(
            "lookup_unavailable",
            fallback.stem_cache["reason_counts"],
        )
        self.assertEqual(
            self._public_artifacts(fallback),
            self._public_artifacts(cold),
        )

    def test_source_change_during_snapshot_closes_hit_and_rerenders(
        self,
    ) -> None:
        plan = _FakePlan(self.manifest)
        fingerprint = {
            "runtime": "stable",
            "runtime_asset_graph": {"file_count": 0},
        }
        serial_policy = SimpleNamespace(
            worker_count=1,
            worker_count_by_part=(1,),
            manifest_sha256_by_part=("",),
        )
        with (
            patch("tianlai.ensemble._DIRECT_STEM_CACHE_LOAD_BYTES", 0),
            patch(
                "tianlai.ensemble._automatic_stem_parallelism",
                return_value=serial_policy,
            ),
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ),
        ):
            render_plan(
                plan,
                self.root / "source-change-cold",
                stem_cache_directory=self.cache,
            )

        opened_sources: list[VerifiedStemSource] = []
        original_open = StemCache.open_verified

        def tracking_open(
            cache: StemCache,
            key: str,
            **kwargs: object,
        ):
            lookup = original_open(cache, key, **kwargs)
            if lookup.source is not None:
                opened_sources.append(lookup.source)
            return lookup

        source_checks = iter((True, False))

        def rerender_after_release(part: object, sample_rate: int):
            self.assertTrue(opened_sources)
            self.assertTrue(opened_sources[-1].closed)
            return self._fake_render(part, sample_rate)

        with (
            patch("tianlai.ensemble._DIRECT_STEM_CACHE_LOAD_BYTES", 0),
            patch(
                "tianlai.ensemble._automatic_stem_parallelism",
                return_value=serial_policy,
            ),
            patch.object(StemCache, "open_verified", new=tracking_open),
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                side_effect=lambda: next(source_checks),
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=rerender_after_release,
            ) as render_part,
        ):
            result = render_plan(
                plan,
                self.root / "source-change-hot",
                stem_cache_directory=self.cache,
            )

        self.assertEqual(render_part.call_count, 1)
        self.assertFalse(result.stem_cache["active"])
        self.assertEqual(result.stem_cache["hits"], 0)
        self.assertEqual(result.stem_cache["bypassed"], 1)
        self.assertEqual(
            result.stem_cache["reason_counts"],
            {"producer_source_changed_restart_required": 1},
        )

    def test_collaboration_analysis_preserves_full_buffer_interface(
        self,
    ) -> None:
        plan = _FakePlan(self.manifest, duration_seconds=0.25)
        fingerprint = {
            "runtime": "stable",
            "runtime_asset_graph": {"file_count": 0},
        }
        materialised: list[int] = []
        original_materialise = VerifiedStemSource.materialise

        def tracking_materialise(source: VerifiedStemSource) -> np.ndarray:
            materialised.append(source.frame_count)
            return original_materialise(source)

        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
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
            render_plan(
                plan,
                self.root / "analysis-cold",
                stem_cache_directory=self.cache,
            )
            with patch.object(
                VerifiedStemSource,
                "materialise",
                new=tracking_materialise,
            ), patch.object(
                StemCache,
                "open_verified",
                side_effect=AssertionError(
                    "analysis cache hits must use the direct full-buffer load"
                ),
            ):
                analyzed = render_plan(
                    plan,
                    self.root / "analysis-hot",
                    stem_cache_directory=self.cache,
                    collaboration_mode="analyze",
                )

        self.assertEqual(render_part.call_count, 1)
        self.assertEqual(materialised, [])
        self.assertEqual(analyzed.stem_cache["hits"], 1)
        self.assertIsNotNone(analyzed.mix_report)

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

    def test_semantic_cache_mismatch_releases_audio_before_rerender(self) -> None:
        plan = _FakePlan(self.manifest, duration_seconds=0.25)
        fingerprint = {
            "runtime": "stable",
            "runtime_asset_graph": {"file_count": 0},
        }
        with (
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=self._fake_render,
            ),
        ):
            render_plan(
                plan,
                self.root / "semantic-cold",
                stem_cache_directory=self.cache,
            )

        metadata_path = next(self.cache.rglob("*.json"))
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["stage"] = "valid-but-not-the-raw-stem-stage"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        opened_sources: list[VerifiedStemSource] = []
        original_open = StemCache.open_verified

        def tracking_open(cache: StemCache, key: str, **kwargs: object):
            lookup = original_open(cache, key, **kwargs)
            if lookup.source is not None:
                opened_sources.append(lookup.source)
            return lookup

        def rerender_after_release(part: object, sample_rate: int):
            self.assertTrue(opened_sources)
            self.assertTrue(opened_sources[-1].closed)
            return self._fake_render(part, sample_rate)

        with (
            patch("tianlai.ensemble._DIRECT_STEM_CACHE_LOAD_BYTES", 0),
            patch.object(StemCache, "open_verified", new=tracking_open),
            patch(
                "tianlai.ensemble.compute_runtime_fingerprint",
                return_value=fingerprint,
            ),
            patch(
                "tianlai.ensemble.current_source_tree_matches",
                return_value=True,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=rerender_after_release,
            ),
        ):
            result = render_plan(
                plan,
                self.root / "semantic-repair",
                stem_cache_directory=self.cache,
            )

        self.assertEqual(result.stem_cache["corrupt_fallbacks"], 1)
        self.assertEqual(result.stem_cache["writes"], 1)

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
