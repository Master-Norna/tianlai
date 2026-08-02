from __future__ import annotations

import copy
import hashlib
from http.client import IncompleteRead
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
import zipfile

import tianlai.resource_restore as restore_module
from tianlai.resource_restore import (
    MANIFEST_KIND,
    MANIFEST_SCHEMA_VERSION,
    ResourceRestoreError,
    build_restore_plan,
    download_archive,
    family_for_instrument,
    load_restore_manifest,
    restore_family,
    safe_extract_archive,
    tree_digest,
    validate_restore_manifest,
)


def _restore_family_process(
    family,
    home: str,
    resource_root: str,
    start,
    results,
) -> None:
    try:
        if not start.wait(20):
            raise RuntimeError("restore start gate timed out")
        result = restore_family(
            family,
            home=home,
            resource_root=resource_root,
            allow_file_urls=True,
        )
        results.put(("ok", result["status"]))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _lock_probe_process(lock_path: str, acquired, *, abrupt: bool) -> None:
    with restore_module._FamilyRestoreLock(Path(lock_path)):
        acquired.set()
        if abrupt:
            os._exit(0)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_manifest(archive: Path, source_tree: Path) -> dict[str, object]:
    digest = tree_digest(source_tree)
    size = archive.stat().st_size
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "kind": MANIFEST_KIND,
        "tree_hash": {
            "algorithm": "tianlai-tree-sha256-v1",
            "record": "test",
        },
        "totals": {
            "family_count": 1,
            "instrument_count": 1,
            "estimated_download_bytes": size,
            "installed_bytes_including_derived": digest.bytes,
            "recommended_free_bytes": max(1, size + digest.bytes),
        },
        "families": [
            {
                "id": "fixture",
                "group": "test",
                "display_name": "Local fixture",
                "instrument_ids": ["测试/本地归档"],
                "license": {
                    "expression": "CC0-1.0",
                    "status": "approved",
                    "evidence_files": ["LICENSE"],
                },
                "source": {
                    "project": "test",
                    "repository": "https://example.invalid/test",
                    "version": "1",
                    "commit": None,
                },
                "archive": {
                    "url": archive.as_uri(),
                    "filename": "fixture.zip",
                    "format": "zip",
                    "bytes": size,
                    "estimated_bytes": size,
                    "max_bytes": size,
                    "sha256": _sha256(archive),
                    "verification": "test archive and complete tree",
                },
                "install": {
                    "target": "Fixtures/library",
                    "strip_single_root": True,
                    "tree": digest.to_dict(),
                    "derived": [],
                },
            }
        ],
    }


class WindowsExtendedPathContractTests(unittest.TestCase):
    def test_extended_unc_prefix_is_case_insensitive_and_keeps_missing_tail(
        self,
    ) -> None:
        expected = r"\\server.example\share\not-installed\deeper\file.bin"
        for unc in ("UNC", "unc", "UnC"):
            with self.subTest(unc=unc):
                prefixed = (
                    rf"\\?\{unc}\server.example\share"
                    r"\not-installed\deeper\file.bin"
                )
                self.assertEqual(
                    restore_module._without_windows_extended_prefix(prefixed),
                    expected,
                )

    @unittest.skipUnless(os.name == "nt", "Windows path spelling contract")
    def test_standard_unc_gets_extended_without_needing_to_exist(self) -> None:
        standard = r"\\server.example\share\not-installed\deeper\file.bin"
        self.assertEqual(
            restore_module._windows_extended_path(standard),
            r"\\?\UNC\server.example\share\not-installed\deeper\file.bin",
        )

    def test_only_drive_absolute_paths_use_the_generic_extended_branch(self) -> None:
        self.assertEqual(
            restore_module._without_windows_extended_prefix(
                r"\\?\C:\not-installed\file.bin"
            ),
            r"C:\not-installed\file.bin",
        )
        invalid = (
            r"\\?\relative\file.bin",
            r"\\?\GLOBALROOT\Device\HarddiskVolumeShadowCopy1",
            r"\\?\Volume{00000000-0000-0000-0000-000000000000}\file.bin",
            "\\\\?\\é:\\file.bin",
            r"\\?\UNC\server-only",
            r"\\.\PhysicalDrive0",
            r"\\.\pipe\tianlai",
        )
        for path in invalid:
            with (
                self.subTest(path=path),
                self.assertRaisesRegex(
                    ResourceRestoreError,
                    "namespace|invalid extended UNC",
                ),
            ):
                restore_module._without_windows_extended_prefix(path)

    @unittest.skipUnless(os.name == "nt", "Windows device namespace contract")
    def test_windows_extended_path_rejects_device_namespace(self) -> None:
        with self.assertRaisesRegex(
            ResourceRestoreError,
            "device namespace",
        ):
            restore_module._windows_extended_path(r"\\.\pipe\tianlai")


class ShippedResourceRestoreManifestTests(unittest.TestCase):
    def test_manifest_maps_all_74_external_resource_catalogue_entries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = load_restore_manifest(home=root)

        self.assertEqual(manifest["totals"]["family_count"], 15)
        self.assertEqual(manifest["totals"]["instrument_count"], 74)
        instruments = [
            instrument
            for family in manifest["families"]
            for instrument in family["instrument_ids"]
        ]
        self.assertEqual(len(instruments), len(set(instruments)))
        for instrument in instruments:
            self.assertTrue(
                (root / "乐器" / Path(*instrument.split("/")) / "乐器.json").is_file(),
                instrument,
            )
        self.assertEqual(
            family_for_instrument(manifest, "管弦乐/木管组/竖笛")["id"],
            "vcsl",
        )
        self.assertEqual(
            family_for_instrument(manifest, "世界乐器/风笛")["id"],
            "freepats-bagpipe",
        )
        self.assertEqual(
            family_for_instrument(manifest, "键盘乐器/钢琴")["id"],
            "salamander-grand-piano",
        )
        self.assertEqual(
            family_for_instrument(manifest, "管弦乐/弦乐组/小提琴")["id"],
            "virtual-playing-orchestra",
        )

    def test_clean_install_plan_is_complete_and_has_no_side_effects(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = load_restore_manifest(home=root)
        with tempfile.TemporaryDirectory(prefix="tianlai_restore_plan_") as temporary:
            resources = Path(temporary) / "音源"
            plan = build_restore_plan(
                manifest["families"],
                resource_root=resources,
            )

            self.assertEqual(plan["family_count"], 15)
            self.assertEqual(plan["instrument_count"], 74)
            self.assertEqual(
                plan["estimated_download_bytes"],
                manifest["totals"]["estimated_download_bytes"],
            )
            self.assertEqual(
                plan["additional_installed_bytes"],
                manifest["totals"]["installed_bytes_including_derived"],
            )
            self.assertTrue(
                all(item["source_state"] == "missing" for item in plan["items"])
            )
            self.assertFalse(resources.exists())

    def test_vcsl_uses_a_fixed_commit_and_tree_not_codeload_bytes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = load_restore_manifest(home=root)
        vcsl = next(
            family for family in manifest["families"] if family["id"] == "vcsl"
        )
        commit = vcsl["source"]["commit"]
        archive = vcsl["archive"]

        self.assertIn(commit, archive["url"])
        self.assertIsNone(archive["bytes"])
        self.assertIsNone(archive["sha256"])
        self.assertRegex(vcsl["install"]["tree"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("fixed_commit", archive["verification"])
        self.assertIn("complete_tree", archive["verification"])

    def test_vpo_uses_two_frozen_upstream_archives_and_one_merged_tree(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = load_restore_manifest(home=root)
        vpo = next(
            family
            for family in manifest["families"]
            if family["id"] == "virtual-playing-orchestra"
        )

        self.assertNotIn("archive", vpo)
        self.assertEqual(len(vpo["archives"]), 2)
        self.assertEqual(
            sum(archive["bytes"] for archive in vpo["archives"]),
            616_658_852,
        )
        for archive in vpo["archives"]:
            self.assertRegex(archive["sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(archive["bytes"], archive["max_bytes"])
        self.assertEqual(vpo["install"]["tree"]["files"], 1922)
        self.assertEqual(len(vpo["instrument_ids"]), 31)
        self.assertTrue(vpo["license"]["output_attribution_required"])

    def test_simpk_recipe_pins_source_tree_code_and_non_code_inputs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        manifest = load_restore_manifest(home=root)
        simpk = next(
            family
            for family in manifest["families"]
            if family["id"] == "simpk-clavichord"
        )
        install = simpk["install"]
        recipe = install["recipe"]

        self.assertEqual(recipe["id"], "simpk-clavichord-v1")
        self.assertEqual(install["source_tree"]["files"], 762)
        self.assertEqual(install["tree"]["files"], 766)
        for path_key, hash_key in (
            ("converter", "converter_sha256"),
            ("source_evidence", "source_evidence_sha256"),
            ("tuning_table", "tuning_table_sha256"),
        ):
            path = root / Path(*recipe[path_key].split("/"))
            self.assertTrue(path.is_file(), path)
            self.assertEqual(_sha256(path), recipe[hash_key])

    def test_generated_github_archives_cannot_pin_container_bytes(self) -> None:
        root = Path(__file__).resolve().parents[1]
        document = json.loads(
            (root / "resource_restore_manifest.json").read_text(encoding="utf-8")
        )
        vcsl = next(
            family for family in document["families"] if family["id"] == "vcsl"
        )
        vcsl["archive"]["bytes"] = vcsl["archive"]["estimated_bytes"]
        vcsl["archive"]["sha256"] = "0" * 64

        with self.assertRaisesRegex(
            ResourceRestoreError,
            "container bytes are not stable",
        ):
            validate_restore_manifest(document)

    def test_every_7z_family_has_a_frozen_container_identity(self) -> None:
        root = Path(__file__).resolve().parents[1]
        document = json.loads(
            (root / "resource_restore_manifest.json").read_text(encoding="utf-8")
        )
        families = [
            family
            for family in document["families"]
            if any(
                archive["format"] == "7z"
                for archive in family.get("archives", [family.get("archive")])
                if archive is not None
            )
        ]

        self.assertEqual(
            {family["id"] for family in families},
            {
                "freepats-bagpipe",
                "freepats-button-accordion",
                "freepats-spanish-guitar",
            },
        )
        for family in families:
            with self.subTest(family=family["id"]):
                self.assertIsInstance(family["archive"]["bytes"], int)
                self.assertGreater(family["archive"]["bytes"], 0)
                self.assertRegex(
                    family["archive"]["sha256"],
                    r"^[0-9a-f]{64}$",
                )

                for field in ("bytes", "sha256"):
                    invalid = copy.deepcopy(document)
                    candidate = next(
                        item
                        for item in invalid["families"]
                        if item["id"] == family["id"]
                    )
                    candidate["archive"][field] = None
                    with self.assertRaisesRegex(
                        ResourceRestoreError,
                        "fixed archive SHA-256 and exact byte length",
                    ):
                        validate_restore_manifest(invalid)

    @unittest.skipUnless(os.name == "nt", "Windows launcher contract")
    def test_cmd_plan_covers_all_74_external_resource_entries(self) -> None:
        root = Path(__file__).resolve().parents[1]
        all_assets = subprocess.run(
            ["cmd.exe", "/d", "/c", "安装可恢复音源.cmd -PlanOnly"],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        all_text = all_assets.stdout.decode("utf-8", errors="replace")
        self.assertEqual(all_assets.returncode, 0, all_text)
        self.assertIn("此外会安装项目本地 FluidSynth", all_text)
        self.assertIn("15 组 / 74 件乐器", all_text)

        only_new = subprocess.run(
            [
                "cmd.exe",
                "/d",
                "/c",
                "安装可恢复音源.cmd -RestorableOnly -PlanOnly",
            ],
            cwd=root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        new_text = only_new.stdout.decode("utf-8", errors="replace")
        self.assertEqual(only_new.returncode, 0, new_text)
        self.assertNotIn("此外会安装项目本地 FluidSynth", new_text)
        self.assertIn("15 组 / 74 件乐器", new_text)


class ResourceRestoreEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="tianlai_restore_e2e_")
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.payload = self.root / "fixture-v1"
        (self.payload / "samples").mkdir(parents=True)
        (self.payload / "LICENSE").write_text("CC0 test fixture\n", encoding="utf-8")
        (self.payload / "instrument.sfz").write_text(
            "<region> sample=samples/tone.wav key=60\n",
            encoding="utf-8",
        )
        (self.payload / "samples" / "tone.wav").write_bytes(b"fixture-pcm")
        self.archive = self.root / "upstream.zip"
        with zipfile.ZipFile(
            self.archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as bundle:
            for path in sorted(self.payload.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(self.payload.parent).as_posix()
                    bundle.write(path, relative)
        document = _fixture_manifest(self.archive, self.payload)
        self.manifest = validate_restore_manifest(
            document,
            allow_file_urls=True,
        )
        self.family = self.manifest["families"][0]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_download_part_safe_extract_full_verify_and_atomic_install(self) -> None:
        resources = self.home / "音源"
        result = restore_family(
            self.family,
            home=self.home,
            resource_root=resources,
            allow_file_urls=True,
        )

        target = resources / "Fixtures" / "library"
        cache = resources / "下载缓存" / "fixture.zip"
        self.assertEqual(result["status"], "installed")
        self.assertEqual(tree_digest(target), tree_digest(self.payload))
        self.assertTrue(cache.is_file())
        self.assertFalse(cache.with_name(cache.name + ".part").exists())
        self.assertTrue(
            (resources / ".tianlai" / "receipts" / "fixture.json").is_file()
        )
        self.assertFalse(
            any(".tianlai-unpacking-" in path.name for path in target.parent.iterdir())
        )

        second = restore_family(
            self.family,
            home=self.home,
            resource_root=resources,
            allow_file_urls=True,
        )
        self.assertEqual(second["status"], "already_verified")

    def test_two_archives_merge_only_after_the_complete_tree_verifies(self) -> None:
        combined = self.root / "combined" / "split-v1"
        (combined / "samples").mkdir(parents=True)
        (combined / "LICENSE").write_text("CC0 split fixture\n", encoding="utf-8")
        (combined / "instrument.sfz").write_text(
            "<region> sample=samples/a.wav key=60\n",
            encoding="utf-8",
        )
        (combined / "samples" / "a.wav").write_bytes(b"split-a")
        (combined / "samples" / "b.wav").write_bytes(b"split-b")

        archives = []
        members = (
            ("split-a.zip", ("LICENSE", "samples/a.wav")),
            ("split-b.zip", ("LICENSE", "instrument.sfz", "samples/b.wav")),
        )
        for filename, relative_paths in members:
            archive_path = self.root / filename
            with zipfile.ZipFile(
                archive_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as bundle:
                for relative in relative_paths:
                    bundle.write(
                        combined / Path(*relative.split("/")),
                        f"split-v1/{relative}",
                    )
            size = archive_path.stat().st_size
            archives.append(
                {
                    "url": archive_path.as_uri(),
                    "filename": filename,
                    "format": "zip",
                    "bytes": size,
                    "estimated_bytes": size,
                    "max_bytes": size,
                    "sha256": _sha256(archive_path),
                    "verification": "test split archive and merged tree",
                }
            )

        document = _fixture_manifest(self.archive, self.payload)
        family = document["families"][0]
        family.pop("archive")
        family["archives"] = archives
        family["install"]["target"] = "Fixtures/split"
        family["install"]["tree"] = tree_digest(combined).to_dict()
        family["install"]["identical_overlaps"] = {
            "LICENSE": _sha256(combined / "LICENSE")
        }
        total_archive_bytes = sum(item["estimated_bytes"] for item in archives)
        document["totals"]["estimated_download_bytes"] = total_archive_bytes
        document["totals"]["installed_bytes_including_derived"] = tree_digest(
            combined
        ).bytes
        validated = validate_restore_manifest(document, allow_file_urls=True)
        split_family = validated["families"][0]
        resources = self.home / "音源"

        result = restore_family(
            split_family,
            home=self.home,
            resource_root=resources,
            allow_file_urls=True,
        )

        target = resources / "Fixtures" / "split"
        self.assertEqual(result["status"], "installed")
        self.assertEqual(tree_digest(target), tree_digest(combined))
        self.assertTrue(all((resources / "下载缓存" / item["filename"]).is_file() for item in archives))
        receipt = json.loads(
            (resources / ".tianlai" / "receipts" / "fixture.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(receipt["schema_version"], 2)
        self.assertEqual(len(receipt["archives"]), 2)

    def test_two_archive_file_collision_fails_without_publishing_target(self) -> None:
        first = self.root / "collision-a.zip"
        second = self.root / "collision-b.zip"
        for archive, payload in ((first, b"first"), (second, b"second")):
            with zipfile.ZipFile(archive, mode="w") as bundle:
                bundle.writestr("collision-v1/LICENSE", payload)
        document = _fixture_manifest(self.archive, self.payload)
        family = document["families"][0]
        family.pop("archive")
        family["archives"] = []
        for archive in (first, second):
            size = archive.stat().st_size
            family["archives"].append(
                {
                    "url": archive.as_uri(),
                    "filename": archive.name,
                    "format": "zip",
                    "bytes": size,
                    "estimated_bytes": size,
                    "max_bytes": size,
                    "sha256": _sha256(archive),
                    "verification": "test collision archive",
                }
            )
        document["totals"]["estimated_download_bytes"] = sum(
            archive.stat().st_size for archive in (first, second)
        )
        validated = validate_restore_manifest(document, allow_file_urls=True)
        resources = self.home / "音源"

        with self.assertRaisesRegex(ResourceRestoreError, "duplicate|colliding"):
            restore_family(
                validated["families"][0],
                home=self.home,
                resource_root=resources,
                allow_file_urls=True,
            )

        self.assertFalse((resources / "Fixtures" / "library").exists())

    def test_mismatched_existing_target_is_never_merged_or_replaced(self) -> None:
        resources = self.home / "音源"
        target = resources / "Fixtures" / "library"
        target.mkdir(parents=True)
        marker = target / "user-file.txt"
        marker.write_text("do not replace\n", encoding="utf-8")

        with self.assertRaisesRegex(ResourceRestoreError, "tree mismatch"):
            restore_family(
                self.family,
                home=self.home,
                resource_root=resources,
                allow_file_urls=True,
            )

        self.assertEqual(marker.read_text(encoding="utf-8"), "do not replace\n")
        self.assertFalse((resources / "下载缓存").exists())

    def test_two_processes_restore_one_family_under_one_process_lock(self) -> None:
        resources = self.home / "音源"
        lock_path = restore_module._family_restore_lock_path(
            resources,
            self.family["id"],
        )
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        results = context.Queue()
        processes = [
            context.Process(
                target=_restore_family_process,
                args=(
                    self.family,
                    str(self.home),
                    str(resources),
                    start,
                    results,
                ),
            )
            for _ in range(2)
        ]
        try:
            with restore_module._FamilyRestoreLock(lock_path):
                for process in processes:
                    process.start()
                start.set()
                time_limit = 20
                for process in processes:
                    self.assertTrue(process.is_alive())

            outcomes = [results.get(timeout=time_limit) for _ in processes]
            for process in processes:
                process.join(timeout=time_limit)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            results.close()
            results.join_thread()

        self.assertEqual(
            sorted(outcomes),
            [("ok", "already_verified"), ("ok", "installed")],
        )
        cache = resources / "下载缓存" / "fixture.zip"
        target = resources / "Fixtures" / "library"
        receipt = resources / ".tianlai" / "receipts" / "fixture.json"
        self.assertEqual(cache.read_bytes(), self.archive.read_bytes())
        self.assertFalse(cache.with_name(cache.name + ".part").exists())
        self.assertEqual(tree_digest(target), tree_digest(self.payload))
        self.assertEqual(
            json.loads(receipt.read_text(encoding="utf-8"))["family_id"],
            "fixture",
        )

    def test_competing_cache_after_check_is_never_overwritten(self) -> None:
        resources = self.home / "音源"
        cache = resources / "下载缓存" / "fixture.zip"
        competitor = b"x" * self.archive.stat().st_size
        real_link = restore_module._link_path

        def create_competitor_then_link(source, destination):
            with open(
                restore_module._windows_extended_path(destination),
                "xb",
            ) as stream:
                stream.write(competitor)
            return real_link(source, destination)

        with (
            mock.patch(
                "tianlai.resource_restore._link_path",
                side_effect=create_competitor_then_link,
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "refusing to overwrite a competing archive cache",
            ),
        ):
            restore_family(
                self.family,
                home=self.home,
                resource_root=resources,
                allow_file_urls=True,
            )

        self.assertEqual(cache.read_bytes(), competitor)
        self.assertEqual(
            cache.with_name(cache.name + ".part").read_bytes(),
            self.archive.read_bytes(),
        )
        self.assertFalse((resources / "Fixtures" / "library").exists())

    def test_competing_empty_tree_after_check_is_never_replaced(self) -> None:
        staged = self.root / "tree-stage"
        target = self.root / "tree-target"
        staged.mkdir()
        (staged / "payload.bin").write_bytes(b"candidate")
        expected = tree_digest(staged).to_dict()
        real_rename = restore_module._rename_path_noreplace
        competing_identity: list[tuple[int, int]] = []

        def create_competitor_then_rename(source, destination):
            restore_module._mkdir_path(destination)
            metadata = restore_module._path_lstat(destination)
            competing_identity.append((metadata.st_dev, metadata.st_ino))
            return real_rename(source, destination)

        with (
            mock.patch(
                "tianlai.resource_restore._rename_path_noreplace",
                side_effect=create_competitor_then_rename,
            ),
            self.assertRaisesRegex(ResourceRestoreError, "tree mismatch"),
        ):
            restore_module._atomic_install_tree(staged, target, expected)

        self.assertTrue(staged.is_dir())
        self.assertEqual((staged / "payload.bin").read_bytes(), b"candidate")
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        current = restore_module._path_lstat(target)
        self.assertEqual(
            (current.st_dev, current.st_ino),
            competing_identity[0],
        )

    def test_complete_corrupt_part_with_known_size_and_sha_restarts_once(self) -> None:
        resources = self.home / "音源"
        cache = resources / "下载缓存"
        cache.mkdir(parents=True)
        partial = cache / "fixture.zip.part"
        partial.write_bytes(b"x" * self.archive.stat().st_size)

        result = restore_family(
            self.family,
            home=self.home,
            resource_root=resources,
            allow_file_urls=True,
        )

        self.assertEqual(result["status"], "installed")
        self.assertFalse(partial.exists())
        self.assertEqual(
            (cache / "fixture.zip").read_bytes(),
            self.archive.read_bytes(),
        )

    def test_http_416_unknown_container_retries_after_tree_mismatch(self) -> None:
        wrong_payload = self.root / "wrong-fixture-v1"
        (wrong_payload / "samples").mkdir(parents=True)
        (wrong_payload / "LICENSE").write_text("CC0 test fixture\n", encoding="utf-8")
        (wrong_payload / "instrument.sfz").write_text(
            "<region> sample=samples/tone.wav key=60\n",
            encoding="utf-8",
        )
        (wrong_payload / "samples" / "tone.wav").write_bytes(b"wrong-pcm")
        wrong_archive = self.root / "wrong.zip"
        with zipfile.ZipFile(wrong_archive, mode="w") as bundle:
            for path in sorted(wrong_payload.rglob("*")):
                if path.is_file():
                    relative = Path("fixture-v1") / path.relative_to(wrong_payload)
                    bundle.write(path, relative.as_posix())

        document = _fixture_manifest(self.archive, self.payload)
        document["families"][0]["source"]["commit"] = "a" * 40
        document["families"][0]["archive"]["bytes"] = None
        document["families"][0]["archive"]["sha256"] = None
        document["families"][0]["archive"]["max_bytes"] = max(
            self.archive.stat().st_size,
            wrong_archive.stat().st_size,
        ) + 100
        family = validate_restore_manifest(
            document,
            allow_file_urls=True,
        )["families"][0]
        resources = self.home / "音源"
        partial = resources / "下载缓存" / "fixture.zip.part"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(wrong_archive.read_bytes())
        calls = 0

        def simulated_download(url, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError(url, 416, "range not satisfiable", {}, None)
            destination.write_bytes(self.archive.read_bytes())

        with mock.patch(
            "tianlai.resource_restore._download_once",
            side_effect=simulated_download,
        ):
            result = restore_family(
                family,
                home=self.home,
                resource_root=resources,
                allow_file_urls=True,
            )

        self.assertEqual(result["status"], "installed")
        self.assertEqual(calls, 2)
        self.assertFalse(partial.exists())
        self.assertEqual(
            tree_digest(resources / "Fixtures" / "library"),
            tree_digest(self.payload),
        )

    def test_explicit_restart_removes_only_part_not_cache_or_target(self) -> None:
        resources = self.home / "音源"
        restore_family(
            self.family,
            home=self.home,
            resource_root=resources,
            allow_file_urls=True,
        )
        cache = resources / "下载缓存"
        published = cache / "fixture.zip"
        target = resources / "Fixtures" / "library"
        published_before = published.read_bytes()
        target_before = tree_digest(target)
        partial = cache / "fixture.zip.part"
        partial.write_bytes(b"abandoned partial")

        result = download_archive(
            self.family,
            cache,
            allow_file_urls=True,
            restart_download=True,
        )

        self.assertEqual(result.path, published.resolve())
        self.assertFalse(partial.exists())
        self.assertEqual(published.read_bytes(), published_before)
        self.assertEqual(tree_digest(target), target_before)

    def test_incomplete_http_read_keeps_part_and_resumes_with_range(self) -> None:
        archive_bytes = self.archive.read_bytes()
        split_at = max(1, len(archive_bytes) // 3)
        first_chunk = archive_bytes[:split_at]
        remaining = archive_bytes[split_at:]
        requested_ranges: list[str | None] = []

        class FakeResponse:
            def __init__(
                self,
                *,
                status: int,
                payload: bytes,
                fail_after_payload: bool,
                content_length: int,
                content_range: str | None = None,
            ) -> None:
                self.status = status
                self.headers = {"Content-Length": str(content_length)}
                if content_range is not None:
                    self.headers["Content-Range"] = content_range
                self.payload = payload
                self.fail_after_payload = fail_after_payload
                self.read_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size: int) -> bytes:
                self.read_count += 1
                if self.fail_after_payload and self.read_count == 1:
                    raise IncompleteRead(self.payload, 1)
                if self.read_count == 1:
                    return self.payload
                return b""

        def fake_urlopen(request, *, timeout):
            del timeout
            requested_ranges.append(request.get_header("Range"))
            if len(requested_ranges) == 1:
                return FakeResponse(
                    status=200,
                    payload=first_chunk,
                    fail_after_payload=True,
                    content_length=len(archive_bytes),
                )
            return FakeResponse(
                status=206,
                payload=remaining,
                fail_after_payload=False,
                content_length=len(remaining),
                content_range=(
                    f"bytes {split_at}-{len(archive_bytes) - 1}/"
                    f"{len(archive_bytes)}"
                ),
            )

        cache = self.home / "音源" / "下载缓存"
        with (
            mock.patch(
                "tianlai.resource_restore.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("tianlai.resource_restore.time.sleep"),
        ):
            result = download_archive(
                self.family,
                cache,
                allow_file_urls=True,
                retries=2,
            )

        self.assertEqual(requested_ranges, [None, f"bytes={split_at}-"])
        self.assertTrue(result.pending_promotion)
        self.assertEqual(result.path.read_bytes(), archive_bytes)

    def test_clean_http_eof_before_content_length_resumes_with_range(self) -> None:
        archive_bytes = self.archive.read_bytes()
        split_at = max(1, len(archive_bytes) // 3)
        requested_ranges: list[str | None] = []

        class FakeResponse:
            def __init__(self, status, payload, headers):
                self.status = status
                self.payload = payload
                self.headers = headers
                self.read_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size: int) -> bytes:
                self.read_count += 1
                return self.payload if self.read_count == 1 else b""

        def fake_urlopen(request, *, timeout):
            del timeout
            requested_ranges.append(request.get_header("Range"))
            if len(requested_ranges) == 1:
                return FakeResponse(
                    200,
                    archive_bytes[:split_at],
                    {"Content-Length": str(len(archive_bytes))},
                )
            return FakeResponse(
                206,
                archive_bytes[split_at:],
                {
                    "Content-Length": str(len(archive_bytes) - split_at),
                    "Content-Range": (
                        f"bytes {split_at}-{len(archive_bytes) - 1}/"
                        f"{len(archive_bytes)}"
                    ),
                },
            )

        cache = self.home / "音源" / "下载缓存"
        with (
            mock.patch(
                "tianlai.resource_restore.urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch("tianlai.resource_restore.time.sleep"),
        ):
            result = download_archive(
                self.family,
                cache,
                allow_file_urls=True,
                retries=2,
            )

        self.assertEqual(requested_ranges, [None, f"bytes={split_at}-"])
        self.assertEqual(result.path.read_bytes(), archive_bytes)

    def test_http_206_requires_matching_bounded_content_range(self) -> None:
        archive_bytes = self.archive.read_bytes()
        split_at = max(1, len(archive_bytes) // 3)
        cache = self.home / "音源" / "下载缓存"
        cache.mkdir(parents=True)
        partial = cache / "fixture.zip.part"

        class FakeResponse:
            status = 206

            def __init__(self, headers):
                self.headers = headers

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size: int) -> bytes:
                return b""

        remaining = len(archive_bytes) - split_at
        cases = (
            (
                {"Content-Length": str(remaining)},
                "missing Content-Range",
            ),
            (
                {
                    "Content-Length": str(remaining),
                    "Content-Range": (
                        f"bytes 0-{len(archive_bytes) - 1}/"
                        f"{len(archive_bytes)}"
                    ),
                },
                "local partial ends",
            ),
            (
                {
                    "Content-Length": str(remaining),
                    "Content-Range": (
                        f"bytes {split_at}-{len(archive_bytes) - 1}/"
                        f"{self.family['archive']['max_bytes'] + 1}"
                    ),
                },
                "safety ceiling",
            ),
        )
        for headers, message in cases:
            with self.subTest(headers=headers):
                partial.write_bytes(archive_bytes[:split_at])
                with (
                    mock.patch(
                        "tianlai.resource_restore.urlopen",
                        return_value=FakeResponse(headers),
                    ),
                    mock.patch("tianlai.resource_restore.time.sleep"),
                    self.assertRaisesRegex(ResourceRestoreError, message),
                ):
                    download_archive(
                        self.family,
                        cache,
                        allow_file_urls=True,
                        retries=1,
                    )
                self.assertEqual(
                    partial.read_bytes(),
                    archive_bytes[:split_at],
                )

    def test_corrupt_fixed_commit_zip_is_wrapped_and_redownloaded_once(
        self,
    ) -> None:
        document = _fixture_manifest(self.archive, self.payload)
        document["families"][0]["source"]["commit"] = "a" * 40
        document["families"][0]["archive"]["bytes"] = None
        document["families"][0]["archive"]["sha256"] = None
        document["families"][0]["archive"]["max_bytes"] += 100
        family = validate_restore_manifest(
            document,
            allow_file_urls=True,
        )["families"][0]
        resources = self.home / "音源"
        calls = 0

        def simulated_download(_url, destination, **_kwargs):
            nonlocal calls
            calls += 1
            destination.write_bytes(
                b"truncated zip"
                if calls == 1
                else self.archive.read_bytes()
            )

        with mock.patch(
            "tianlai.resource_restore._download_once",
            side_effect=simulated_download,
        ):
            result = restore_family(
                family,
                home=self.home,
                resource_root=resources,
                allow_file_urls=True,
            )

        self.assertEqual(result["status"], "installed")
        self.assertEqual(calls, 2)
        self.assertEqual(
            tree_digest(resources / "Fixtures" / "library"),
            tree_digest(self.payload),
        )

    def test_exhausted_incomplete_http_reads_are_wrapped(self) -> None:
        cache = self.home / "音源" / "下载缓存"
        with (
            mock.patch(
                "tianlai.resource_restore._download_once",
                side_effect=IncompleteRead(b"partial", 10),
            ),
            mock.patch("tianlai.resource_restore.time.sleep"),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "download failed after 2 attempts",
            ),
        ):
            download_archive(
                self.family,
                cache,
                retries=2,
            )

    def test_linux_7z_resolver_discovers_explicit_bsdtar(self) -> None:
        executable = "/usr/bin/bsdtar"
        version = subprocess.CompletedProcess(
            args=[executable, "--version"],
            returncode=0,
            stdout=b"bsdtar 3.7.7 - libarchive 3.7.7\n",
            stderr=None,
        )
        with (
            mock.patch(
                "tianlai.resource_restore._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.resource_restore._is_macos_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.resource_restore.shutil.which",
                return_value=executable,
            ) as which,
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                return_value=version,
            ) as run,
        ):
            selected = restore_module._find_bsdtar_executable()

        self.assertEqual(selected, executable)
        which.assert_called_once_with("bsdtar")
        run.assert_called_once_with(
            [executable, "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

    def test_linux_7z_resolver_never_falls_back_to_gnu_tar(self) -> None:
        def locate(command: str) -> str | None:
            if command == "tar":
                return "/usr/bin/tar"
            return None

        with (
            mock.patch(
                "tianlai.resource_restore._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.resource_restore._is_macos_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.resource_restore.shutil.which",
                side_effect=locate,
            ) as which,
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
            ) as run,
            self.assertRaisesRegex(
                ResourceRestoreError,
                "libarchive-tools",
            ),
        ):
            restore_module._find_bsdtar_executable()

        which.assert_called_once_with("bsdtar")
        run.assert_not_called()

    def test_macos_tar_fallback_accepts_verified_system_bsdtar(self) -> None:
        executable = "/usr/bin/tar"

        def locate(command: str) -> str | None:
            if command == "tar":
                return executable
            return None

        version = subprocess.CompletedProcess(
            args=[executable, "--version"],
            returncode=0,
            stdout=b"bsdtar 3.5.3 - libarchive 3.5.3\n",
            stderr=None,
        )
        with (
            mock.patch(
                "tianlai.resource_restore._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.resource_restore._is_macos_runtime",
                return_value=True,
            ),
            mock.patch(
                "tianlai.resource_restore.shutil.which",
                side_effect=locate,
            ) as which,
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                return_value=version,
            ),
        ):
            selected = restore_module._find_bsdtar_executable()

        self.assertEqual(selected, executable)
        self.assertEqual(
            which.call_args_list,
            [mock.call("bsdtar"), mock.call("tar")],
        )

    def test_macos_gnu_tar_on_path_falls_back_to_verified_system_tar(self) -> None:
        gnu_tar = "/usr/local/bin/tar"
        system_tar = "/usr/bin/tar"

        def locate(command: str) -> str | None:
            if command == "tar":
                return gnu_tar
            return None

        def version(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            executable = arguments[0]
            output = (
                b"tar (GNU tar) 1.35\n"
                if executable == gnu_tar
                else b"bsdtar 3.5.3 - libarchive 3.5.3\n"
            )
            return subprocess.CompletedProcess(
                args=arguments,
                returncode=0,
                stdout=output,
                stderr=None,
            )

        with (
            mock.patch(
                "tianlai.resource_restore._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.resource_restore._is_macos_runtime",
                return_value=True,
            ),
            mock.patch(
                "tianlai.resource_restore.shutil.which",
                side_effect=locate,
            ) as which,
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                side_effect=version,
            ) as run,
        ):
            selected = restore_module._find_bsdtar_executable()

        self.assertEqual(selected, system_tar)
        self.assertEqual(
            which.call_args_list,
            [mock.call("bsdtar"), mock.call("tar")],
        )
        self.assertEqual(
            [call.args[0][0] for call in run.call_args_list],
            [gnu_tar, system_tar],
        )

    def test_macos_tar_fallback_rejects_when_no_bsdtar_is_verified(self) -> None:
        executable = "/usr/local/bin/tar"

        def locate(command: str) -> str | None:
            if command == "tar":
                return executable
            return None

        gnu_version = subprocess.CompletedProcess(
            args=[executable, "--version"],
            returncode=0,
            stdout=b"tar (GNU tar) 1.35\n",
            stderr=None,
        )
        with (
            mock.patch(
                "tianlai.resource_restore._is_windows_runtime",
                return_value=False,
            ),
            mock.patch(
                "tianlai.resource_restore._is_macos_runtime",
                return_value=True,
            ),
            mock.patch(
                "tianlai.resource_restore.shutil.which",
                side_effect=locate,
            ) as which,
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                return_value=gnu_version,
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "macOS normally provides.*GNU tar is not a supported",
            ),
        ):
            restore_module._find_bsdtar_executable()

        self.assertEqual(
            which.call_args_list,
            [mock.call("bsdtar"), mock.call("tar")],
        )

    def test_windows_tar_fallback_must_report_bsdtar(self) -> None:
        executable = r"C:\Windows\System32\tar.exe"

        def locate(command: str) -> str | None:
            if command == "tar":
                return executable
            return None

        gnu_version = subprocess.CompletedProcess(
            args=[executable, "--version"],
            returncode=0,
            stdout=b"tar (GNU tar) 1.35\n",
            stderr=None,
        )
        with (
            mock.patch(
                "tianlai.resource_restore._is_windows_runtime",
                return_value=True,
            ),
            mock.patch(
                "tianlai.resource_restore.shutil.which",
                side_effect=locate,
            ) as which,
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                return_value=gnu_version,
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "GNU tar is not a supported 7z extractor",
            ),
        ):
            restore_module._find_bsdtar_executable()

        self.assertEqual(
            which.call_args_list,
            [mock.call("bsdtar"), mock.call("tar")],
        )

    def test_windows_tar_fallback_accepts_verified_bsdtar(self) -> None:
        executable = r"C:\Windows\System32\tar.exe"

        def locate(command: str) -> str | None:
            if command == "tar":
                return executable
            return None

        version = subprocess.CompletedProcess(
            args=[executable, "--version"],
            returncode=0,
            stdout=b"bsdtar 3.7.7 - libarchive 3.7.7\n",
            stderr=None,
        )
        with (
            mock.patch(
                "tianlai.resource_restore._is_windows_runtime",
                return_value=True,
            ),
            mock.patch(
                "tianlai.resource_restore.shutil.which",
                side_effect=locate,
            ) as which,
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                return_value=version,
            ),
        ):
            selected = restore_module._find_bsdtar_executable()

        self.assertEqual(selected, executable)
        self.assertEqual(
            which.call_args_list,
            [mock.call("bsdtar"), mock.call("tar")],
        )

    def test_7z_restore_requires_bsdtar_before_download(self) -> None:
        family = copy.deepcopy(self.family)
        family["archive"]["format"] = "7z"
        resources = self.home / "音源"
        cache = resources / "下载缓存"
        with (
            mock.patch(
                "tianlai.resource_restore._find_bsdtar_executable",
                side_effect=ResourceRestoreError("missing bsdtar"),
            ),
            mock.patch(
                "tianlai.resource_restore.download_archive",
            ) as download,
            self.assertRaisesRegex(ResourceRestoreError, "missing bsdtar"),
        ):
            restore_module._restore_source_tree(
                family,
                resource_root=resources,
                cache_root=cache,
                allow_file_urls=True,
                restart_download=False,
            )

        download.assert_not_called()
        self.assertFalse(resources.exists())

    def test_real_bsdtar_7z_preflight_enforces_frozen_size_limit(self) -> None:
        try:
            tar_executable = restore_module._find_bsdtar_executable()
        except ResourceRestoreError as exc:
            if os.environ.get("TIANLAI_REQUIRE_BSDTAR") == "1":
                self.fail(f"bsdtar is required by this platform gate: {exc}")
            self.skipTest(str(exc))

        container = self.root / "sevenzip-input"
        payload_root = container / "fixture-v1"
        payload_root.mkdir(parents=True)
        payload = bytes(range(251)) * 2
        (payload_root / "payload.bin").write_bytes(payload)
        archive = self.root / "fixture.7z"
        created = subprocess.run(
            [
                tar_executable,
                "-acf",
                str(archive),
                "-C",
                str(container),
                "fixture-v1",
            ],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(
            created.returncode,
            0,
            created.stderr.decode("utf-8", errors="replace"),
        )
        archive_sha = _sha256(archive)
        archive_bytes = archive.stat().st_size

        too_small = self.root / "sevenzip-too-small"
        with self.assertRaisesRegex(
            ResourceRestoreError,
            "7z declared size exceeds",
        ):
            safe_extract_archive(
                archive,
                too_small,
                archive_format="7z",
                max_unpacked_bytes=len(payload) - 1,
                expected_archive_sha256=archive_sha,
                expected_archive_bytes=archive_bytes,
            )
        self.assertFalse(too_small.exists())

        destination = self.root / "sevenzip-stage"
        safe_extract_archive(
            archive,
            destination,
            archive_format="7z",
            max_unpacked_bytes=len(payload),
            expected_archive_sha256=archive_sha,
            expected_archive_bytes=archive_bytes,
        )
        self.assertEqual(
            (destination / "fixture-v1" / "payload.bin").read_bytes(),
            payload,
        )

    def test_strict_real_7z_gate_fails_instead_of_skipping_missing_bsdtar(
        self,
    ) -> None:
        case = type(self)(
            "test_real_bsdtar_7z_preflight_enforces_frozen_size_limit"
        )
        result = unittest.TestResult()
        with (
            mock.patch.dict(os.environ, {"TIANLAI_REQUIRE_BSDTAR": "1"}),
            mock.patch(
                "tianlai.resource_restore._find_bsdtar_executable",
                side_effect=ResourceRestoreError("missing bsdtar fixture"),
            ),
        ):
            case.run(result)

        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("bsdtar is required", result.failures[0][1])

    def test_7z_fixed_identity_is_checked_before_member_inspection(self) -> None:
        with (
            mock.patch(
                "tianlai.resource_restore._inspect_7z",
            ) as inspect_7z,
            self.assertRaisesRegex(
                ResourceRestoreError,
                "7z archive SHA-256 mismatch",
            ),
        ):
            safe_extract_archive(
                self.archive,
                self.root / "sevenzip-identity-stage",
                archive_format="7z",
                max_unpacked_bytes=100,
                expected_archive_sha256="0" * 64,
                expected_archive_bytes=self.archive.stat().st_size,
            )
        inspect_7z.assert_not_called()

    def test_7z_uses_resolved_bsdtar_commands_for_inspection_and_extraction(
        self,
    ) -> None:
        executable = "/usr/bin/bsdtar"
        names = subprocess.CompletedProcess(
            args=[executable, "-tf", "fixture.7z"],
            returncode=0,
            stdout=b"fixture-v1/payload.bin\n",
            stderr=b"",
        )
        metadata = subprocess.CompletedProcess(
            args=[executable, "-tvf", "fixture.7z"],
            returncode=0,
            stdout=(
                b"-rw-r--r--  0 0  0  7 Jan 01 1970 "
                b"fixture-v1/payload.bin\n"
            ),
            stderr=b"",
        )
        extracted = subprocess.CompletedProcess(
            args=[executable, "-xf", "fixture.7z"],
            returncode=0,
            stdout=b"",
            stderr=b"",
        )
        destination = self.root / "sevenzip-bsdtar-stage"
        with (
            mock.patch(
                "tianlai.resource_restore._find_bsdtar_executable",
                return_value=executable,
            ),
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                side_effect=[names, metadata, extracted],
            ) as run,
            mock.patch(
                "tianlai.resource_restore._assert_plain_extracted_tree",
            ) as assert_plain,
        ):
            selected = restore_module._inspect_7z(
                self.archive,
                max_unpacked_bytes=100,
            )
            restore_module._extract_7z(
                self.archive,
                destination,
                max_unpacked_bytes=100,
                tar_executable=selected,
            )

        archive_path = restore_module._windows_extended_path(self.archive)
        destination_path = restore_module._windows_extended_path(destination)
        self.assertEqual(
            run.call_args_list,
            [
                mock.call(
                    [executable, "-tf", archive_path],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
                mock.call(
                    [executable, "-tvf", archive_path],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
                mock.call(
                    [
                        executable,
                        "-xf",
                        archive_path,
                        "-C",
                        destination_path,
                        "--no-same-owner",
                        "--no-same-permissions",
                    ],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
            ],
        )
        assert_plain.assert_called_once_with(
            destination,
            max_total_bytes=100,
        )

    def test_7z_link_metadata_is_rejected_during_preflight(self) -> None:
        names = subprocess.CompletedProcess(
            args=["bsdtar", "-tf", "fixture.7z"],
            returncode=0,
            stdout=b"fixture-v1/link\n",
            stderr=b"",
        )
        metadata = subprocess.CompletedProcess(
            args=["bsdtar", "-tvf", "fixture.7z"],
            returncode=0,
            stdout=(
                b"lrwxrwxrwx  0 0  0  0 Jan 01 1970 "
                b"fixture-v1/link -> target\n"
            ),
            stderr=b"",
        )
        with (
            mock.patch(
                "tianlai.resource_restore._find_bsdtar_executable",
                return_value="bsdtar",
            ),
            mock.patch(
                "tianlai.resource_restore.subprocess.run",
                side_effect=[names, metadata],
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "link or special entry",
            ),
        ):
            restore_module._inspect_7z(
                self.archive,
                max_unpacked_bytes=100,
            )

    def test_source_cleanup_failure_is_a_note_on_the_primary_error(self) -> None:
        resources = self.home / "音源"
        cache = resources / "下载缓存"

        def fail_after_creating_stage(_archive, destination, **_kwargs):
            Path(destination).mkdir(parents=True)
            raise ResourceRestoreError("primary source extraction failure")

        with (
            mock.patch(
                "tianlai.resource_restore.download_archive",
                return_value=restore_module.DownloadedArchive(
                    self.archive,
                    cache / "fixture.zip",
                    False,
                ),
            ),
            mock.patch(
                "tianlai.resource_restore.safe_extract_archive",
                side_effect=fail_after_creating_stage,
            ),
            mock.patch(
                "tianlai.resource_restore._safe_remove_staging",
                side_effect=ResourceRestoreError("source cleanup failure"),
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "primary source extraction failure",
            ) as raised,
        ):
            restore_module._restore_source_tree(
                self.family,
                resource_root=resources,
                cache_root=cache,
                allow_file_urls=True,
                restart_download=False,
            )

        self.assertTrue(
            any(
                "source cleanup failure" in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )

    def test_derived_cleanup_failure_is_a_note_on_the_primary_error(self) -> None:
        resources = self.home / "音源"
        recipe = self.home / "recipe.json"
        recipe.write_text("{}\n", encoding="utf-8")
        derived = {
            "target": "Derived/fixture",
            "recipe": "recipe.json",
            "tree": {
                "files": 1,
                "bytes": 1,
                "sha256": "0" * 64,
            },
        }

        def fail_after_creating_stage(_recipe, *, output_root):
            Path(output_root).mkdir(parents=True)
            raise ResourceRestoreError("primary derived build failure")

        with (
            mock.patch(
                "tianlai.derived_samples.build_derived_resources",
                side_effect=fail_after_creating_stage,
            ),
            mock.patch(
                "tianlai.resource_restore._safe_remove_staging",
                side_effect=ResourceRestoreError("derived cleanup failure"),
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "primary derived build failure",
            ) as raised,
        ):
            restore_module._restore_derived_tree(
                derived,
                home=self.home,
                resource_root=resources,
            )

        self.assertTrue(
            any(
                "derived cleanup failure" in note
                for note in getattr(raised.exception, "__notes__", ())
            )
        )

    def test_traversal_archive_is_rejected_before_writing_outside_stage(self) -> None:
        malicious = self.root / "malicious.zip"
        with zipfile.ZipFile(malicious, mode="w") as bundle:
            bundle.writestr("../escape.txt", "blocked")

        with self.assertRaisesRegex(ResourceRestoreError, "unsafe archive member"):
            safe_extract_archive(
                malicious,
                self.root / "stage",
                archive_format="zip",
                max_unpacked_bytes=100,
            )

        self.assertFalse((self.root / "escape.txt").exists())

    def test_windows_ambiguous_member_names_and_parent_conflicts_are_rejected(
        self,
    ) -> None:
        bad_names = (
            "fixture-v1/CON/sample.wav",
            "fixture-v1/device./sample.wav",
            "fixture-v1/trailing /sample.wav",
            "fixture-v1/stream:name/sample.wav",
            "fixture-v1/wild?card/sample.wav",
            "fixture-v1/control\x01name/sample.wav",
        )
        for index, name in enumerate(bad_names):
            with self.subTest(name=name):
                archive = self.root / f"unsafe-name-{index}.zip"
                with zipfile.ZipFile(archive, mode="w") as bundle:
                    bundle.writestr(name, b"blocked")
                with self.assertRaisesRegex(
                    ResourceRestoreError,
                    "Windows path|device name",
                ):
                    safe_extract_archive(
                        archive,
                        self.root / f"unsafe-name-stage-{index}",
                        archive_format="zip",
                        max_unpacked_bytes=100,
                    )

        conflicts = (
            (
                "file-parent",
                (
                    ("fixture-v1/item", b"file"),
                    ("fixture-v1/item/child.wav", b"child"),
                ),
            ),
            (
                "implicit-directory-case",
                (
                    ("fixture-v1/Folder/one.wav", b"one"),
                    ("fixture-v1/folder/two.wav", b"two"),
                ),
            ),
            (
                "unicode-normalisation",
                (
                    ("fixture-v1/Caf\u00e9/tone.wav", b"one"),
                    ("fixture-v1/Cafe\u0301/tone.wav", b"two"),
                ),
            ),
        )
        for index, (label, members) in enumerate(conflicts):
            with self.subTest(label=label):
                archive = self.root / f"unsafe-conflict-{index}.zip"
                with zipfile.ZipFile(archive, mode="w") as bundle:
                    for name, payload in members:
                        bundle.writestr(name, payload)
                with self.assertRaisesRegex(
                    ResourceRestoreError,
                    "parent directory|case-colliding",
                ):
                    safe_extract_archive(
                        archive,
                        self.root / f"unsafe-conflict-stage-{index}",
                        archive_format="zip",
                        max_unpacked_bytes=100,
                    )

    def test_extraction_os_error_is_reported_as_restore_error(self) -> None:
        stage = self.root / "io-error-stage"
        with (
            mock.patch(
                "tianlai.resource_restore._extract_zip",
                side_effect=OSError(206, "simulated long-path failure"),
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "cannot safely extract archive",
            ),
        ):
            safe_extract_archive(
                self.archive,
                stage,
                archive_format="zip",
                max_unpacked_bytes=100,
            )

    def test_abrupt_process_exit_releases_long_path_family_lock(self) -> None:
        long_container = self.root / "long-lock-root"
        resources = long_container.joinpath(
            *(
                f"segment-{index:02d}-abcdefghijklmnop"
                for index in range(8)
            )
        )
        lock_path = restore_module._family_restore_lock_path(
            resources,
            self.family["id"],
        )
        if os.name == "nt":
            self.assertGreater(len(str(lock_path)), 260)
            self.assertTrue(
                restore_module._windows_extended_path(lock_path).startswith(
                    "\\\\?\\"
                )
            )
        context = multiprocessing.get_context("spawn")
        first_acquired = context.Event()
        second_acquired = context.Event()
        first = context.Process(
            target=_lock_probe_process,
            args=(str(lock_path), first_acquired),
            kwargs={"abrupt": True},
        )
        second = context.Process(
            target=_lock_probe_process,
            args=(str(lock_path), second_acquired),
            kwargs={"abrupt": False},
        )
        try:
            first.start()
            self.assertTrue(first_acquired.wait(20))
            first.join(timeout=20)
            self.assertFalse(first.is_alive())
            self.assertEqual(first.exitcode, 0)

            second.start()
            self.assertTrue(
                second_acquired.wait(20),
                "the operating system did not release the exited process lock",
            )
            second.join(timeout=20)
            self.assertFalse(second.is_alive())
            self.assertEqual(second.exitcode, 0)
            self.assertTrue(restore_module._path_is_plain_file(lock_path))
        finally:
            for process in (first, second):
                if process.is_alive():
                    process.terminate()
                process.join(timeout=5)
            if restore_module._path_exists(long_container):
                shutil.rmtree(
                    restore_module._windows_extended_path(long_container)
                )

    def test_receipt_uses_private_temp_and_cleans_it_after_replace_failure(
        self,
    ) -> None:
        resources = self.home / "音源"
        digest = tree_digest(self.payload)
        with (
            mock.patch(
                "tianlai.resource_restore._replace_path",
                side_effect=OSError("simulated receipt replace failure"),
            ),
            self.assertRaisesRegex(
                ResourceRestoreError,
                "cannot atomically write resource receipt",
            ),
        ):
            restore_module._write_receipt(
                self.family,
                resource_root=resources,
                source_digest=digest,
                derived_digests=[],
            )

        receipts = resources / ".tianlai" / "receipts"
        self.assertFalse((receipts / "fixture.json").exists())
        self.assertEqual(list(receipts.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows extended-length path contract")
    def test_deep_zip_tree_verification_and_atomic_publish_need_no_global_policy(
        self,
    ) -> None:
        archive = self.root / "long-path.zip"
        nested_parts = tuple(
            f"segment-{index:02d}-abcdefghijklmnop" for index in range(8)
        )
        relative_file = Path(*nested_parts) / "sample-payload.bin"
        payload = b"long-path-resource-payload"
        licence = b"CC0 long-path fixture\n"
        with zipfile.ZipFile(
            archive,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as bundle:
            bundle.writestr("fixture-v1/LICENSE", licence)
            bundle.writestr(
                (Path("fixture-v1") / relative_file).as_posix(),
                payload,
            )

        long_container = self.root / "long-path-root"
        long_parent = long_container.joinpath(
            *(
                f"parent-{index:02d}-abcdefghijklmnop"
                for index in range(8)
            )
        )
        target = long_parent / "Fixtures" / "library"
        published_file = target / relative_file
        unpack_example = (
            target.parent / ".library.tianlai-unpacking-0123456789abcdef"
        )
        self.assertGreater(len(str(unpack_example)), 260)
        self.assertGreater(len(str(published_file)), 260)
        self.assertTrue(
            restore_module._windows_extended_path(unpack_example).startswith(
                "\\\\?\\"
            )
        )

        records = []
        for relative, content in (
            ("LICENSE", licence),
            (relative_file.as_posix(), payload),
        ):
            records.append(
                f"{hashlib.sha256(content).hexdigest()}  {relative}\n"
            )
        expected = {
            "files": len(records),
            "bytes": len(licence) + len(payload),
            "sha256": hashlib.sha256("".join(records).encode("utf-8")).hexdigest(),
        }
        family = copy.deepcopy(self.family)
        family["archive"].update(
            {
                "url": archive.as_uri(),
                "bytes": archive.stat().st_size,
                "estimated_bytes": archive.stat().st_size,
                "max_bytes": archive.stat().st_size,
                "sha256": _sha256(archive),
            }
        )
        family["install"]["tree"] = expected

        try:
            result = restore_family(
                family,
                home=self.home,
                resource_root=long_parent,
                cache_root=long_parent / "下载缓存",
                allow_file_urls=True,
            )
            self.assertEqual(result["status"], "installed")
            self.assertEqual(tree_digest(target).to_dict(), expected)
            with open(
                restore_module._windows_extended_path(published_file),
                "rb",
            ) as stream:
                self.assertEqual(stream.read(), payload)
            self.assertTrue(
                restore_module._path_is_plain_file(
                    long_parent / ".tianlai" / "receipts" / "fixture.json"
                )
            )
            with os.scandir(
                restore_module._windows_extended_path(target.parent)
            ) as entries:
                self.assertFalse(
                    any(
                        ".tianlai-unpacking-" in entry.name
                        for entry in entries
                    )
                )
        finally:
            if restore_module._path_exists(long_container):
                shutil.rmtree(
                    restore_module._windows_extended_path(long_container)
                )


if __name__ == "__main__":
    unittest.main()
