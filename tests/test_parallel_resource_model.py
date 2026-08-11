from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pytest

from tianlai.render_parallelism import derive_worker_resource_estimate
from tianlai.runtime_layout import discover_runtime_layout
from tools.update_decoded_sample_evidence import (
    _ALGORITHM_FIELD,
    _DECODED_FIELD,
    _SAMPLE_BYTES_ALGORITHM_FIELD,
    _TRUSTED_RUNTIME_VARIANTS,
    _VARIANT_BOUNDS_FIELD,
    _trusted_runtime_variant_bounds,
    _trusted_runtime_variant_evidence,
    update_catalogue,
)


_FIXED_WORKER_RESERVE = 256 * 1024 * 1024


@dataclass(frozen=True)
class _Capability:
    manifest_path: object


@dataclass(frozen=True)
class _Executor:
    capability: _Capability
    override_map: object


@dataclass(frozen=True)
class _Part:
    executor: _Executor


@dataclass(frozen=True)
class _Plan:
    parts: tuple[_Part, ...]


def _part(path: object) -> _Part:
    return _Part(_Executor(_Capability(path), {}))


def _write_json(path: Path, document: object) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False),
        encoding="utf-8",
    )


class WorkerResourceModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _manifest(
        self,
        name: str,
        document: dict[str, object],
        evidence: dict[str, object] | None = None,
    ) -> Path:
        directory = self.root / name
        directory.mkdir()
        manifest = directory / "乐器.json"
        _write_json(manifest, document)
        if evidence is not None:
            _write_json(directory / "资源核验.json", evidence)
        return manifest

    def test_asset_free_dsp_uses_the_fixed_conservative_reserve(self) -> None:
        manifest = self._manifest(
            "dsp",
            {
                "type": "synthesizer",
                "provenance_kind": "project_authored_dsp",
                "external_audio_assets": [],
            },
        )

        estimate = derive_worker_resource_estimate(_Plan((_part(manifest),)))

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(estimate.reason, "verified")
        self.assertEqual(
            estimate.worker_reserve_bytes_by_part,
            (_FIXED_WORKER_RESERVE,),
        )
        self.assertEqual(estimate.sample_backed_by_part, (False,))
        self.assertEqual(estimate.managed_worker_safe_by_part, (False,))

    def test_packaged_builtin_manifest_is_managed_worker_safe(self) -> None:
        catalog = discover_runtime_layout(require_catalog=True).catalog
        manifest = next(
            path
            for path in catalog.rglob("*.json")
            if path.name == "乐器.json"
            and json.loads(path.read_text(encoding="utf-8")).get("type")
            == "synthesizer"
            and "implementation"
            not in json.loads(path.read_text(encoding="utf-8"))
        )

        estimate = derive_worker_resource_estimate(_Plan((_part(manifest),)))

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(estimate.managed_worker_safe_by_part, (True,))
        self.assertRegex(estimate.manifest_sha256_by_part[0], r"^[0-9a-f]{64}$")

    def test_local_implementation_cannot_self_authorise_subprocesses(self) -> None:
        manifest = self._manifest(
            "local-factory",
            {
                "type": "oscillator",
                "implementation": "乐器.py",
                "runtime_asset_policy": "no_external_audio_assets",
            },
        )
        manifest.with_name("乐器.py").write_text(
            "def create(**kwargs): raise AssertionError\n",
            encoding="utf-8",
        )
        with patch(
            "tianlai.render_parallelism.discover_runtime_layout",
            return_value=SimpleNamespace(catalog=self.root),
        ):
            estimate = derive_worker_resource_estimate(
                _Plan((_part(manifest),))
            )

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(estimate.managed_worker_safe_by_part, (False,))

    def test_verified_external_catalog_keeps_builtin_acceleration(self) -> None:
        manifest = self._manifest(
            "external-builtin",
            {
                "type": "oscillator",
                "runtime_asset_policy": "no_external_audio_assets",
            },
        )
        with patch(
            "tianlai.render_parallelism.discover_runtime_layout",
            return_value=SimpleNamespace(catalog=self.root),
        ):
            estimate = derive_worker_resource_estimate(
                _Plan((_part(manifest),))
            )

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(estimate.managed_worker_safe_by_part, (True,))

    def test_structural_override_cannot_bypass_builtin_admission(self) -> None:
        manifest = self._manifest(
            "override-bypass",
            {
                "type": "synthesizer",
                "runtime_asset_policy": "no_external_audio_assets",
            },
        )
        part = _Part(
            _Executor(
                _Capability(manifest),
                {"implementation": "evil.py"},
            )
        )
        with patch(
            "tianlai.render_parallelism.discover_runtime_layout",
            return_value=SimpleNamespace(catalog=self.root),
        ):
            estimate = derive_worker_resource_estimate(_Plan((part,)))

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(estimate.managed_worker_safe_by_part, (False,))

    @pytest.mark.external_assets
    def test_builtin_sample_reports_freeze_decoded_memory_bounds(self) -> None:
        catalog = discover_runtime_layout(require_catalog=True).catalog
        checked, stale = update_catalogue(catalog, write=False)
        self.assertGreaterEqual(checked, 37)
        self.assertEqual(stale, 0)

    @pytest.mark.external_assets
    def test_managed_runtime_variants_use_the_largest_legal_single_variant(
        self,
    ) -> None:
        catalog = discover_runtime_layout(require_catalog=True).catalog
        targets = (
            catalog / "管弦乐" / "弦乐组" / "小提琴" / "乐器.json",
            catalog / "管弦乐" / "弦乐组" / "低音提琴" / "乐器.json",
        )
        for manifest_path in targets:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report = json.loads(
                manifest_path.with_name("资源核验.json").read_text(
                    encoding="utf-8"
                )
            )
            actual = _trusted_runtime_variant_bounds(manifest_path, manifest)
            authority = _trusted_runtime_variant_evidence(
                manifest_path,
                manifest,
            )
            with self.subTest(instrument=manifest_path.parent.name):
                self.assertEqual(
                    tuple(actual),
                    _TRUSTED_RUNTIME_VARIANTS,
                )
                self.assertEqual(report[_VARIANT_BOUNDS_FIELD], actual)
                self.assertNotIn("sample_count", report)
                self.assertNotIn("source_sfz_sha256", report)
                self.assertNotIn("sample_set_sha256", report)
                self.assertEqual(
                    report["sample_bytes"],
                    max(item["sample_bytes"] for item in actual.values()),
                )
                self.assertEqual(
                    report[_DECODED_FIELD],
                    max(item[_DECODED_FIELD] for item in actual.values()),
                )
                self.assertIn(
                    "max over trusted runtime variants",
                    report[_SAMPLE_BYTES_ALGORITHM_FIELD],
                )
                self.assertIn(
                    "max over trusted runtime variants",
                    report[_ALGORITHM_FIELD],
                )
                for bounds in actual.values():
                    self.assertLessEqual(
                        bounds["sample_bytes"], report["sample_bytes"]
                    )
                    self.assertLessEqual(
                        bounds[_DECODED_FIELD], report[_DECODED_FIELD]
                    )
                self.assertEqual(set(report["variants"]), set(authority))
                for variant, expected in authority.items():
                    frozen = report["variants"][variant]
                    for field in (
                        "source_sfz_sha256",
                        "sample_count",
                        "sample_bytes",
                        _DECODED_FIELD,
                        "sample_set_sha256",
                        "sample_set_hash_algorithm",
                    ):
                        self.assertEqual(
                            frozen[field],
                            expected[field],
                            f"{manifest_path.parent.name}/{variant}/{field}",
                        )

                part = _Part(
                    _Executor(
                        _Capability(manifest_path),
                        {"sample_variant": "SEC"},
                    )
                )
                estimate = derive_worker_resource_estimate(
                    _Plan((part,))
                )
                self.assertTrue(estimate.workers_safe, estimate.reason)
                self.assertEqual(
                    estimate.worker_reserve_bytes_by_part,
                    (_FIXED_WORKER_RESERVE + report[_DECODED_FIELD],),
                )

    def test_builtin_sample_is_admitted_with_verified_decoded_bound(self) -> None:
        catalog = discover_runtime_layout(require_catalog=True).catalog
        manifest = catalog / "世界乐器" / "卡林巴" / "乐器.json"

        estimate = derive_worker_resource_estimate(
            _Plan((_part(manifest),))
        )

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(estimate.managed_worker_safe_by_part, (True,))
        self.assertEqual(estimate.sample_backed_by_part, (True,))
        self.assertGreater(
            estimate.worker_reserve_bytes_by_part[0],
            _FIXED_WORKER_RESERVE,
        )

    def test_sample_bytes_are_read_from_each_manifest_sibling(self) -> None:
        sample_bytes = 850 * 1024 * 1024
        decoded_bytes = 1_100 * 1024 * 1024
        sampled = self._manifest(
            "sampled",
            {"type": "dedicated_sfz", "asset_root": "samples"},
            {
                "sample_count": 3,
                "sample_bytes": sample_bytes,
                "decoded_float32_stereo_bytes": decoded_bytes,
            },
        )
        modeled = self._manifest(
            "modeled",
            {
                "type": "modeled_instrument",
                "provenance_kind": "project_authored_dsp",
                "external_audio_assets": [],
            },
            {"external_assets": []},
        )

        estimate = derive_worker_resource_estimate(
            _Plan((_part(sampled), _part(modeled)))
        )

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(
            estimate.worker_reserve_bytes_by_part,
            (
                _FIXED_WORKER_RESERVE + decoded_bytes,
                _FIXED_WORKER_RESERVE,
            ),
        )
        self.assertEqual(estimate.sample_backed_by_part, (True, False))
        self.assertEqual(
            estimate.managed_worker_safe_by_part,
            (False, False),
        )

    def test_small_sample_inventory_keeps_the_runtime_floor(self) -> None:
        manifest = self._manifest(
            "small-sample",
            {"type": "sampled", "asset_root": "samples"},
            {
                "sample_bytes": 1234,
                "decoded_float32_stereo_bytes": 1234,
            },
        )

        estimate = derive_worker_resource_estimate(_Plan((_part(manifest),)))

        self.assertTrue(estimate.workers_safe)
        self.assertEqual(
            estimate.worker_reserve_bytes_by_part,
            (_FIXED_WORKER_RESERVE + 1234,),
        )
        self.assertEqual(estimate.sample_backed_by_part, (True,))

    def test_sample_backed_manifest_without_evidence_is_ineligible(self) -> None:
        manifest = self._manifest(
            "missing-evidence",
            {"type": "sampled", "asset_root": "samples"},
        )

        estimate = derive_worker_resource_estimate(_Plan((_part(manifest),)))

        self.assertFalse(estimate.workers_safe)
        self.assertEqual(estimate.reason, "part_0_sample_evidence_missing")
        self.assertEqual(
            len(estimate.worker_reserve_bytes_by_part),
            1,
        )

    def test_compressed_bytes_without_decoded_bound_are_ineligible(self) -> None:
        manifest = self._manifest(
            "missing-decoded-evidence",
            {"type": "sampled", "asset_root": "samples"},
            {"sample_bytes": 1234},
        )

        estimate = derive_worker_resource_estimate(_Plan((_part(manifest),)))

        self.assertFalse(estimate.workers_safe)
        self.assertEqual(
            estimate.reason,
            "part_0_decoded_sample_evidence_missing",
        )

    def test_invalid_sample_bytes_and_redirected_evidence_fail_closed(self) -> None:
        invalid = self._manifest(
            "invalid-evidence",
            {"type": "sampled", "asset_root": "samples"},
            {"sample_bytes": True},
        )
        null_sample_bytes = self._manifest(
            "null-sample-bytes",
            {"type": "sampled", "asset_root": "samples"},
            {"sample_bytes": None},
        )
        redirected = self._manifest(
            "redirected",
            {
                "type": "sampled",
                "asset_root": "samples",
                "resource_verification": "../资源核验.json",
            },
        )

        for manifest in (invalid, null_sample_bytes, redirected):
            with self.subTest(manifest=manifest.parent.name):
                estimate = derive_worker_resource_estimate(
                    _Plan((_part(manifest),))
                )
                self.assertFalse(estimate.workers_safe)
                self.assertEqual(
                    estimate.reason,
                    "part_0_resource_evidence_invalid",
                )

    def test_nonempty_external_asset_declaration_overrides_dsp_label(self) -> None:
        manifest = self._manifest(
            "contradictory",
            {
                "type": "synthesizer",
                "provenance_kind": "project_authored_dsp",
                "external_audio_assets": ["sample.wav"],
            },
        )

        estimate = derive_worker_resource_estimate(_Plan((_part(manifest),)))

        self.assertFalse(estimate.workers_safe)
        self.assertEqual(estimate.reason, "part_0_sample_evidence_missing")

    def test_malformed_plan_or_manifest_returns_a_stable_unsafe_verdict(self) -> None:
        malformed_manifest = self.root / "bad.json"
        malformed_manifest.write_text('{"type":"sampled",', encoding="utf-8")

        malformed = derive_worker_resource_estimate(
            _Plan((_part(malformed_manifest),))
        )
        missing_parts = derive_worker_resource_estimate(object())

        self.assertFalse(malformed.workers_safe)
        self.assertEqual(
            malformed.reason,
            "part_0_resource_evidence_invalid",
        )
        self.assertFalse(missing_parts.workers_safe)
        self.assertEqual(missing_parts.reason, "invalid_plan_parts")


if __name__ == "__main__":
    unittest.main()
