from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from tianlai.canonical_json import (
    canonical_json_file_sha256,
    canonical_json_sha256,
)
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.onset_evidence import _render_python_closure
from tianlai.render_parallelism import derive_worker_resource_estimate
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "乐器"


def _release_python_sha256(path: Path) -> str:
    # Source releases contain committed Git blobs, and .gitattributes fixes
    # every Python payload to LF.  A Windows construction worktree may still
    # expose CRLF bytes before checkout normalization, so compare the release
    # representation rather than that platform-specific physical spelling.
    content = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest()


MIGRATED_DIRECTORIES = (
    CATALOG / "测试工具" / "参考振荡器",
    CATALOG / "环境与拟音" / "呼吸噪声",
    CATALOG / "环境与拟音" / "掌声",
    CATALOG / "环境与拟音" / "枪声",
    CATALOG / "环境与拟音" / "海浪",
    CATALOG / "环境与拟音" / "电话铃",
    CATALOG / "环境与拟音" / "直升机",
    CATALOG / "环境与拟音" / "雨境合成氛围",
    CATALOG / "环境与拟音" / "鸟鸣",
    CATALOG / "管弦乐" / "打击乐组" / "定音鼓",
)
SUBSTANTIVE_MIGRATED_DIRECTORIES = (
    CATALOG / "世界乐器" / "编钟",
    CATALOG / "管弦乐" / "弦乐组" / "中提琴",
    CATALOG / "管弦乐" / "弦乐组" / "大提琴",
    CATALOG / "管弦乐" / "弦乐组" / "小提琴",
    CATALOG / "管弦乐" / "木管组" / "长笛",
    CATALOG / "键盘乐器" / "钢琴",
)
CENTRAL_FACTORY_TYPES = frozenset(
    {
        "mtg_solo_sax",
        "vpo_brass",
        "vpo_celesta",
        "vpo_cowbell",
        "vpo_harp",
        "vpo_mixed_choir",
        "vpo_orchestral_hit",
        "vpo_percussion",
        "vpo_solo_string",
        "vpo_string_section",
        "vpo_woodwind",
    }
)
SUBSTANTIVE_FACTORY_TYPES = frozenset(
    {"modeled_bianzhong", "vsco2_viola_section", "cello", "violin", "flute", "piano"}
)


def _single_part_plan(manifest_path: Path) -> SimpleNamespace:
    capability = SimpleNamespace(manifest_path=str(manifest_path))
    executor = SimpleNamespace(capability=capability, override_map={})
    return SimpleNamespace(parts=(SimpleNamespace(executor=executor),))


class ManagedCatalogueDispatchTests(unittest.TestCase):
    @staticmethod
    def migrated_manifests() -> tuple[Path, ...]:
        paths = {
            directory / "乐器.json"
            for directory in MIGRATED_DIRECTORIES + SUBSTANTIVE_MIGRATED_DIRECTORIES
        }
        for manifest_path in CATALOG.rglob("乐器.json"):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("type") in CENTRAL_FACTORY_TYPES:
                paths.add(manifest_path)
        return tuple(sorted(paths))

    def test_redundant_local_factories_stay_on_builtin_dispatch(self) -> None:
        for directory in MIGRATED_DIRECTORIES:
            with self.subTest(instrument=directory.name):
                manifest_path = directory / "乐器.json"
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                self.assertNotIn("implementation", manifest)
                self.assertTrue((directory / "乐器.py").is_file())

                estimate = derive_worker_resource_estimate(
                    _single_part_plan(manifest_path)
                )
                self.assertTrue(estimate.workers_safe, estimate.reason)
                self.assertEqual(
                    estimate.managed_worker_safe_by_part,
                    (True,),
                )
                self.assertEqual(len(estimate.manifest_sha256_by_part[0]), 64)

    def test_central_factories_are_managed_with_unreferenced_compatibility_wrappers(
        self,
    ) -> None:
        migrated: list[Path] = []
        for manifest_path in sorted(CATALOG.rglob("乐器.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("type") not in CENTRAL_FACTORY_TYPES:
                continue
            migrated.append(manifest_path)
            with self.subTest(instrument=manifest_path.parent.name):
                self.assertNotIn("implementation", manifest)
                self.assertTrue((manifest_path.parent / "乐器.py").is_file())
                estimate = derive_worker_resource_estimate(
                    _single_part_plan(manifest_path)
                )
                self.assertTrue(estimate.workers_safe, estimate.reason)
                self.assertEqual(
                    estimate.managed_worker_safe_by_part,
                    (True,),
                )
                self.assertEqual(estimate.sample_backed_by_part, (True,))
        self.assertEqual(len(migrated), 31)

    def test_substantive_factories_are_builtin_managed_with_compatibility_wrappers(
        self,
    ) -> None:
        for directory in SUBSTANTIVE_MIGRATED_DIRECTORIES:
            manifest_path = directory / "乐器.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            with self.subTest(instrument=directory.name):
                self.assertIn(manifest["type"], SUBSTANTIVE_FACTORY_TYPES)
                self.assertNotIn("implementation", manifest)
                self.assertTrue((directory / "乐器.py").is_file())
                estimate = derive_worker_resource_estimate(
                    _single_part_plan(manifest_path)
                )
                self.assertTrue(estimate.workers_safe, estimate.reason)
                self.assertEqual(estimate.managed_worker_safe_by_part, (True,))
                self.assertEqual(
                    estimate.sample_backed_by_part,
                    (manifest["type"] != "modeled_bianzhong",),
                )

    def test_no_substantive_local_factories_remain(self) -> None:
        local_directories: set[str] = set()
        managed_count = 0
        for manifest_path in sorted(CATALOG.rglob("乐器.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            relative = manifest_path.parent.relative_to(CATALOG).as_posix()
            if manifest.get("implementation") is not None:
                local_directories.add(relative)
                continue
            estimate = derive_worker_resource_estimate(
                _single_part_plan(manifest_path)
            )
            if (
                estimate.workers_safe
                and estimate.managed_worker_safe_by_part == (True,)
            ):
                managed_count += 1
        self.assertEqual(local_directories, set())
        self.assertEqual(managed_count, 104)

    def test_substantive_directory_wrappers_remain_directly_importable(self) -> None:
        expected_classes = {
            "modeled_bianzhong": "BianzhongInstrument",
            "vsco2_viola_section": "Vsco2ViolaSectionInstrument",
            "cello": "CelloInstrument",
            "violin": "ViolinInstrument",
            "flute": "FluteInstrument",
            "piano": "PianoInstrument",
        }
        for index, directory in enumerate(SUBSTANTIVE_MIGRATED_DIRECTORIES):
            manifest = json.loads(
                (directory / "乐器.json").read_text(encoding="utf-8")
            )
            wrapper_path = directory / "乐器.py"
            module_name = f"tianlai_test_substantive_wrapper_{index}"
            spec = importlib.util.spec_from_file_location(module_name, wrapper_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(module_name, None)
            with self.subTest(instrument=directory.name):
                self.assertTrue(callable(module.create))
                self.assertTrue(
                    isinstance(getattr(module, expected_classes[manifest["type"]]), type)
                )

        mapping_wrapper = (
            CATALOG
            / "管弦乐"
            / "弦乐组"
            / "中提琴"
            / "VSCO2中提琴映射.py"
        )
        spec = importlib.util.spec_from_file_location(
            "tianlai_test_vsco2_viola_mapping_wrapper",
            mapping_wrapper,
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertTrue(callable(module.build_region_sets))

    def test_timpani_has_a_positive_decoded_memory_bound(self) -> None:
        report_path = (
            CATALOG
            / "管弦乐"
            / "打击乐组"
            / "定音鼓"
            / "资源核验.json"
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertGreater(report["sample_bytes"], 0)
        self.assertGreater(report["decoded_float32_stereo_bytes"], 0)
        self.assertIn("unique runtime sample", report["decoded_float32_stereo_algorithm"])

    def test_audition_reports_record_only_the_factory_route_migration(self) -> None:
        reports = 0
        for manifest_path in self.migrated_manifests():
            report_path = manifest_path.with_name("试听核验.json")
            if not report_path.is_file():
                continue
            reports += 1
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            report = json.loads(report_path.read_text(encoding="utf-8"))
            previous_manifest = dict(manifest)
            previous_manifest["implementation"] = "乐器.py"
            migration = report.get("factory_dispatch_migration")
            with self.subTest(instrument=manifest_path.parent.name):
                self.assertEqual(
                    report.get("manifest_canonical_sha256"),
                    canonical_json_file_sha256(manifest_path),
                )
                self.assertIsInstance(migration, dict)
                if manifest.get("type") in SUBSTANTIVE_FACTORY_TYPES:
                    self.assertEqual(
                        migration.get("status"),
                        "implementation_relocated_to_builtin_no_audio_change",
                    )
                    self.assertEqual(
                        migration.get("baseline_revision"),
                        "4b3e3aa5b19a587ccc0e766212165a43a739ee12",
                    )
                    self.assertEqual(
                        migration.get("verified_by"),
                        "tools/reverify_substantive_builtin_dispatch_migration.py",
                    )
                else:
                    self.assertEqual(
                        migration.get("status"),
                        "factory_route_only_no_audio_change",
                    )
                self.assertEqual(
                    migration.get("previous_manifest_canonical_sha256"),
                    canonical_json_sha256(previous_manifest),
                )
                self.assertEqual(
                    migration.get("changed_fields"),
                    ["implementation"],
                )
                self.assertIs(migration.get("audio_rerendered"), False)
                self.assertRegex(
                    str(migration.get("migrated_at", "")),
                    r"^\d{4}-\d{2}-\d{2}$",
                )
        self.assertEqual(reports, 46)

    def test_substantive_render_closures_track_builtin_sources(self) -> None:
        expected_sources = {
            "modeled_bianzhong": "tianlai/bianzhong.py",
            "vsco2_viola_section": "tianlai/vsco2_viola.py",
            "cello": "tianlai/cello.py",
            "violin": "tianlai/violin.py",
            "flute": "tianlai/flute.py",
            "piano": "tianlai/piano.py",
        }
        for directory in SUBSTANTIVE_MIGRATED_DIRECTORIES:
            manifest_path = directory / "乐器.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            closure = _render_python_closure(ROOT, manifest_path, manifest)
            paths = {entry["path"] for entry in closure["files"]}
            with self.subTest(instrument=directory.name):
                self.assertIn(expected_sources[manifest["type"]], paths)
                self.assertNotIn(
                    (directory / "乐器.py").relative_to(ROOT).as_posix(),
                    paths,
                )

    def test_substantive_resource_reports_bind_builtin_sources(self) -> None:
        expected_sources = {
            "modeled_bianzhong": "tianlai/bianzhong.py",
            "vsco2_viola_section": "tianlai/vsco2_viola.py",
            "cello": "tianlai/cello.py",
            "violin": "tianlai/violin.py",
            "flute": "tianlai/flute.py",
            "piano": "tianlai/piano.py",
        }
        for directory in SUBSTANTIVE_MIGRATED_DIRECTORIES:
            manifest = json.loads(
                (directory / "乐器.json").read_text(encoding="utf-8")
            )
            report = json.loads(
                (directory / "资源核验.json").read_text(encoding="utf-8")
            )
            expected = expected_sources[manifest["type"]]
            if manifest["type"] == "modeled_bianzhong":
                source = report.get("engine")
                digest = report.get("engine_sha256")
            else:
                source = report.get("implementation_source")
                digest = report.get("implementation_sha256")
            with self.subTest(instrument=directory.name):
                self.assertEqual(source, expected)
                self.assertEqual(
                    digest,
                    _release_python_sha256(ROOT / expected),
                )

    def test_cowbell_render_closure_tracks_its_actual_builtin_backend(self) -> None:
        manifest_path = CATALOG / "现代鼓组" / "牛铃" / "乐器.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        closure = _render_python_closure(ROOT, manifest_path, manifest)
        paths = {entry["path"] for entry in closure["files"]}
        self.assertIn("tianlai/vpo_specials.py", paths)
        self.assertNotIn("乐器/现代鼓组/牛铃/乐器.py", paths)

    def test_reference_compatibility_wrapper_remains_audio_equivalent(self) -> None:
        directory = CATALOG / "测试工具" / "参考振荡器"
        manifest_path = directory / "乐器.json"
        wrapper_path = directory / "乐器.py"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        module_name = "tianlai_test_reference_compatibility_wrapper"
        spec = importlib.util.spec_from_file_location(module_name, wrapper_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
            compatibility = module.create(
                manifest=manifest,
                sample_rate=8_000,
                base_directory=str(directory),
            )
        finally:
            sys.modules.pop(module_name, None)
        builtin = create_instrument(
            manifest,
            8_000,
            base_directory=str(directory),
        )
        self.assertIs(type(compatibility), type(builtin))
        event = PerformanceEvent(
            sample=0,
            sequence=0,
            type="note_on",
            payload={"note_id": 1, "midi_note": 69.0, "velocity": 0.7},
        )
        tuning = EqualTemperament()
        compatibility.handle_event(event, tuning)
        builtin.handle_event(event, tuning)
        self.assertEqual(
            [compatibility.render_frame() for _ in range(1_024)],
            [builtin.render_frame() for _ in range(1_024)],
        )
        self.assertEqual(
            builtin._tianlai_factory_provenance["factory_route"],
            "builtin_manifest_dispatch_no_implementation",
        )


if __name__ == "__main__":
    unittest.main()
