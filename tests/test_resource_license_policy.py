from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import unittest

import pytest

from tianlai.upgrade_registry import HISTORICAL_UPGRADE_REGISTRY


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ROOT = ROOT / "乐器"
EXCEPTION_PATH = ROOT / "docs" / "音源许可例外.json"
RESTORE_MANIFEST_PATH = ROOT / "resource_restore_manifest.json"
TRUSTED_PATH = ROOT / "可信乐器.json"
MCP_DOCUMENTATION_PATH = ROOT / "docs" / "MCP.md"
APPROVED_LICENSES = {"CC0-1.0", "CC-BY-3.0", "CC-BY-4.0"}
LICENSE_STATUSES = {"approved", "grandfathered", "quarantined"}
EXCEPTION_STATUSES = {"grandfathered", "quarantined"}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _external_manifests() -> list[tuple[str, Path, dict]]:
    result = []
    for path in sorted(INSTRUMENT_ROOT.rglob("乐器.json")):
        manifest = _load_json(path)
        if not manifest.get("asset_root"):
            continue
        result.append((path.relative_to(ROOT).as_posix(), path, manifest))
    return result


def _report_evidence_hashes(report: dict) -> dict[str, str]:
    hashes = report.get("evidence_sha256") or report.get("license_file_sha256")
    if not isinstance(hashes, dict):
        return {}
    return {str(path): str(digest).lower() for path, digest in hashes.items()}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_asset_file(asset_root: Path, relative: str) -> Path:
    posix_path = PurePosixPath(relative)
    if (
        not relative
        or relative != posix_path.as_posix()
        or posix_path.is_absolute()
        or any(part in ("", ".", "..") for part in posix_path.parts)
    ):
        raise AssertionError(f"非规范音源证据路径: {relative!r}")
    resolved = (asset_root / posix_path).resolve()
    try:
        resolved.relative_to(asset_root)
    except ValueError as error:
        raise AssertionError(f"许可证据逃逸音源根: {relative!r}") from error
    return resolved


class ResourceLicensePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.external = _external_manifests()
        cls.exception_document = _load_json(EXCEPTION_PATH)
        cls.exceptions = cls.exception_document["exceptions"]

    def test_every_external_manifest_has_complete_controlled_metadata(self) -> None:
        self.assertGreaterEqual(len(self.external), 74)
        required_strings = ("origin", "upstream", "upstream_version", "license")
        for relative, _path, manifest in self.external:
            with self.subTest(manifest=relative):
                for field in required_strings:
                    self.assertTrue(
                        isinstance(manifest.get(field), str)
                        and manifest[field].strip(),
                        f"{relative} 缺少 {field}",
                    )
                evidence_files = manifest.get("evidence_files")
                self.assertIsInstance(evidence_files, list, relative)
                self.assertTrue(evidence_files, f"{relative} 无许可证据文件")
                self.assertEqual(
                    len(evidence_files),
                    len(set(evidence_files)),
                    f"{relative} 许可证据路径重复",
                )
                status = manifest.get("license_status")
                self.assertIn(status, LICENSE_STATUSES, relative)
                if manifest["license"] in APPROVED_LICENSES:
                    self.assertEqual(status, "approved", relative)
                    self.assertNotIn(relative, self.exceptions)
                else:
                    self.assertIn(relative, self.exceptions)
                    self.assertEqual(
                        status,
                        self.exceptions[relative]["status"],
                        relative,
                    )

    def test_every_by_licensed_external_manifest_has_frozen_attribution(self) -> None:
        checked = 0
        for relative, _path, manifest in self.external:
            if "BY" not in manifest["license"].upper():
                continue
            checked += 1
            with self.subTest(manifest=relative):
                for field in ("creator", "attribution"):
                    self.assertTrue(
                        isinstance(manifest.get(field), str)
                        and manifest[field].strip(),
                        f"{relative} 的 {manifest['license']} 缺少 {field}",
                    )
        self.assertGreaterEqual(checked, 38)

    def test_restore_families_record_attribution_and_vpo_evidence_scope(self) -> None:
        restore = _load_json(RESTORE_MANIFEST_PATH)
        families = {family["id"]: family for family in restore["families"]}

        for family_id in (
            "greg-sullivan-e-pianos",
            "salamander-grand-piano",
            "simpk-clavichord",
        ):
            with self.subTest(family=family_id):
                self.assertIs(
                    families[family_id]["license"][
                        "output_attribution_required"
                    ],
                    True,
                )

        self.assertIs(
            families["itsclipping-ganjo"]["license"][
                "output_attribution_required"
            ],
            False,
        )
        self.assertEqual(
            families["simpk-clavichord"]["source"]["repository"],
            "https://www.simpk.de/museum/besuch/projekte/"
            "klaviatur-tastatur-interface/samples.html",
        )

        vpo_license = families["virtual-playing-orchestra"]["license"]
        self.assertEqual(vpo_license["status"], "grandfathered")
        self.assertEqual(
            vpo_license["expression"],
            "Mixed upstream licences; see Documentation/license.htm",
        )
        self.assertEqual(
            vpo_license["evidence_files"],
            ["Documentation/license.htm"],
        )
        components = {
            item["source"]: item["license"]
            for item in vpo_license["components_declared_by_upstream"]
        }
        self.assertEqual(
            components,
            {
                "Sonatina Symphonic Orchestra": (
                    "Creative Commons Sampling Plus 1.0"
                ),
                "Mattias Westlund additional samples": (
                    "Creative Commons Attribution-ShareAlike 3.0 Unported"
                ),
                "No Budget Orchestra": (
                    "Creative Commons Attribution-ShareAlike 4.0 International"
                ),
                "VSCO 2 Community Edition": "CC0 1.0 Universal",
                "University of Iowa Electronic Music": (
                    "upstream unrestricted-use statement"
                ),
                "stamperadam": "CC0 1.0 Universal",
            },
        )
        self.assertIn("Aggregate declarations", vpo_license["evidence_scope"])
        self.assertIn("per-file", vpo_license["evidence_scope"])

        vpo_family = families["virtual-playing-orchestra"]
        self.assertEqual(len(vpo_family["instrument_ids"]), 31)
        vpo_statuses = {
            instrument_id: _load_json(
                INSTRUMENT_ROOT / PurePosixPath(instrument_id) / "乐器.json"
            )["license_status"]
            for instrument_id in vpo_family["instrument_ids"]
        }
        self.assertEqual(
            sum(status == "grandfathered" for status in vpo_statuses.values()),
            30,
        )
        self.assertEqual(
            vpo_statuses["管弦乐/弦乐组/中提琴"],
            "approved",
        )

    def test_central_exceptions_are_exhaustive_and_evidence_locked(self) -> None:
        self.assertEqual(self.exception_document["schema_version"], 1)
        non_approved = {
            relative
            for relative, _path, manifest in self.external
            if manifest["license"] not in APPROVED_LICENSES
        }
        self.assertEqual(set(self.exceptions), non_approved)

        manifest_by_path = {
            relative: (path, manifest)
            for relative, path, manifest in self.external
        }
        for relative, exception in self.exceptions.items():
            with self.subTest(manifest=relative):
                self.assertEqual(
                    set(exception),
                    {"status", "license", "reason", "evidence"},
                )
                self.assertIn(exception["status"], EXCEPTION_STATUSES)
                self.assertTrue(str(exception["reason"]).strip())
                manifest_path, manifest = manifest_by_path[relative]
                self.assertEqual(exception["status"], manifest["license_status"])
                self.assertEqual(exception["license"], manifest["license"])
                asset_root = (manifest_path.parent / manifest["asset_root"]).resolve()
                report = _load_json(manifest_path.with_name("资源核验.json"))
                hashes = _report_evidence_hashes(report)
                expected_evidence = {
                    _safe_asset_file(asset_root, evidence_file)
                    .relative_to(ROOT)
                    .as_posix(): hashes[evidence_file]
                    for evidence_file in manifest["evidence_files"]
                }
                actual_evidence = {
                    item["path"]: item["sha256"]
                    for item in exception["evidence"]
                }
                self.assertEqual(actual_evidence, expected_evidence)
                self.assertEqual(
                    len(actual_evidence),
                    len(exception["evidence"]),
                    f"{relative} 集中例外中有重复证据路径",
                )

    @pytest.mark.external_assets
    def test_installed_exception_evidence_matches_frozen_hashes(self) -> None:
        checked = 0
        for relative, exception in self.exceptions.items():
            evidence = {
                item["path"]: item["sha256"]
                for item in exception["evidence"]
            }
            paths = {
                label: (ROOT / PurePosixPath(label)).resolve()
                for label in evidence
            }
            existing = {
                label for label, path in paths.items() if path.is_file()
            }
            if not existing:
                continue
            missing = sorted(set(paths) - existing)
            self.assertEqual(
                missing,
                [],
                f"{relative} 的许可证据为部分安装",
            )
            checked += 1
            for label, path in paths.items():
                self.assertEqual(_sha256(path), evidence[label], path)
        if checked == 0:
            self.skipTest("未安装任何集中例外所绑定的第三方资源")

    def test_manifest_and_report_evidence_declarations_are_consistent(self) -> None:
        for relative, manifest_path, manifest in self.external:
            with self.subTest(manifest=relative):
                report_path = manifest_path.with_name("资源核验.json")
                self.assertTrue(report_path.is_file(), report_path)
                report = _load_json(report_path)
                hashes = _report_evidence_hashes(report)
                self.assertTrue(hashes, f"{relative} 报告无许可证据哈希")
                self.assertEqual(set(manifest["evidence_files"]), set(hashes), relative)

                for evidence_file in manifest["evidence_files"]:
                    self.assertRegex(hashes[evidence_file], r"^[0-9a-f]{64}$")

                for field in ("upstream", "origin", "license"):
                    if report.get(field):
                        self.assertEqual(manifest[field], report[field], relative)
                if report.get("upstream_version"):
                    expected_version = report["upstream_version"]
                elif report.get("upstream_commit"):
                    expected_version = report["upstream_commit"]
                elif report.get("sfz_version") and report.get("wave_version"):
                    expected_version = (
                        f"{report['sfz_version']} / {report['wave_version']}"
                    )
                else:
                    self.fail(f"{relative} 报告缺少固定版本证据")
                self.assertEqual(
                    manifest["upstream_version"],
                    expected_version,
                    relative,
                )

    @pytest.mark.external_assets
    def test_installed_manifest_evidence_files_match_frozen_hashes(self) -> None:
        checked = 0
        for relative, manifest_path, manifest in self.external:
            asset_root = (manifest_path.parent / manifest["asset_root"]).resolve()
            report = _load_json(manifest_path.with_name("资源核验.json"))
            hashes = _report_evidence_hashes(report)
            files = {
                evidence_file: _safe_asset_file(asset_root, evidence_file)
                for evidence_file in manifest["evidence_files"]
            }
            existing = {
                label for label, path in files.items() if path.is_file()
            }
            if not existing:
                continue
            missing = sorted(set(files) - existing)
            self.assertEqual(
                missing,
                [],
                f"{relative} 的许可证据为部分安装",
            )
            checked += 1
            for label, path in files.items():
                self.assertEqual(_sha256(path), hashes[label], path)
        if checked == 0:
            self.skipTest("未安装任何外部乐器许可证据")

    def test_mtg_manifests_pin_the_reported_commit(self) -> None:
        mtg_count = 0
        for relative, manifest_path, manifest in self.external:
            if manifest.get("type") != "mtg_solo_sax":
                continue
            mtg_count += 1
            report = _load_json(manifest_path.with_name("资源核验.json"))
            commit = manifest["upstream_version"]
            self.assertRegex(commit, r"^[0-9a-f]{40}$", relative)
            self.assertEqual(commit, manifest["upstream_commit"], relative)
            self.assertEqual(commit, report["upstream_commit"], relative)
            self.assertEqual(manifest["license_status"], "approved", relative)
        self.assertEqual(mtg_count, 4)

    def test_current_external_license_status_counts(self) -> None:
        counts = {
            status: sum(
                manifest["license_status"] == status
                for _relative, _path, manifest in self.external
            )
            for status in LICENSE_STATUSES
        }
        self.assertEqual(
            counts,
            {
                "approved": 43,
                "grandfathered": 31,
                "quarantined": 0,
            },
        )

    def test_trusted_list_contains_no_quarantined_instrument(self) -> None:
        trusted = _load_json(TRUSTED_PATH)["trusted"]
        status_by_instrument = {
            Path(relative)
            .parent.relative_to("乐器")
            .as_posix(): manifest["license_status"]
            for relative, _path, manifest in self.external
        }
        quarantined_trusted = sorted(
            instrument
            for instrument in trusted
            if status_by_instrument.get(instrument) == "quarantined"
        )
        self.assertEqual(quarantined_trusted, [])

    def test_mcp_documents_the_trusted_allowlist_source(self) -> None:
        trusted = _load_json(TRUSTED_PATH)["trusted"]
        self.assertEqual(len(trusted), len(set(trusted)))
        documentation = MCP_DOCUMENTATION_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "[`可信乐器.json`](../可信乐器.json)",
            documentation,
            "docs/MCP.md 应指向可信策展清单的唯一数据源",
        )
        self.assertIn("不应", documentation)
        self.assertIn("固化数量", documentation)

    def test_banjo_is_the_cc0_ganjo_replacement(self) -> None:
        relative = "乐器/世界乐器/班卓琴/乐器.json"
        manifest_path = ROOT / PurePosixPath(relative)
        manifest = _load_json(manifest_path)
        report = _load_json(manifest_path.with_name("资源核验.json"))
        self.assertEqual(manifest["license"], "CC0-1.0")
        self.assertEqual(report["license"], "CC0-1.0")
        self.assertEqual(manifest["license_status"], "approved")
        self.assertNotIn(relative, self.exceptions)
        self.assertIn("ganjo", manifest["upstream"].casefold())
        self.assertIn("ganjo", report["upstream"].casefold())
        self.assertEqual(manifest["creator"], "itsclipping")
        self.assertIn("provenance", manifest["attribution"])
        self.assertIn("does not imply endorsement", manifest["attribution"])
        self.assertIn("v1.000", manifest["upstream_version"])
        self.assertIn("v1.000", report["upstream_version"])
        for path in (
            manifest_path.with_name("README.md"),
            manifest_path.with_name("来源.md"),
        ):
            text = path.read_text(encoding="utf-8")
            self.assertIn("CC0", text)
            self.assertIn("ganjo", text.casefold())

        registry = dict(HISTORICAL_UPGRADE_REGISTRY)
        self.assertEqual(registry["SAM-06"], "世界乐器/班卓琴")

    def test_exception_status_vocabulary_is_closed(self) -> None:
        schema = _load_json(ROOT / "schemas" / "instrument.schema.json")
        status_schema = schema["$defs"]["candidateMetadata"]["properties"][
            "license_status"
        ]
        self.assertEqual(set(status_schema["enum"]), LICENSE_STATUSES)
        self.assertEqual(
            {
                entry["status"]
                for entry in self.exceptions.values()
            }
            <= EXCEPTION_STATUSES,
            True,
        )
        self.assertIsNone(
            re.search(
                r'"status"\s*:\s*"(?!grandfathered"|quarantined")',
                EXCEPTION_PATH.read_text(encoding="utf-8"),
            )
        )


if __name__ == "__main__":
    unittest.main()
