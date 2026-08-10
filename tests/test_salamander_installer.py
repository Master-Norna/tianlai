from __future__ import annotations

import hashlib
import json
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "乐器" / "键盘乐器" / "钢琴" / "获取音源.ps1"
MANIFEST = ROOT / "乐器" / "键盘乐器" / "钢琴" / "乐器.json"
ASSET_ROOT = ROOT / "音源" / "钢琴" / "SalamanderGrandPiano"
AUDIT_SCRIPT = ROOT / "乐器" / "键盘乐器" / "钢琴" / "核验资源.py"
AUDIT_REPORT = ROOT / "乐器" / "键盘乐器" / "钢琴" / "资源核验.json"

COMMIT = "3382bf9496bba2486f5ab0de55a264d1dfc38404"
TREE_COUNT = 668
TREE_BYTES = 748_451_483
TREE_SHA256 = (
    "FCF5B194A5E19057F006138F7EC852C0B1354E12D65C8250546F9CDB5CBDDB82"
)
FLAC_COUNT = 641
FLAC_BYTES = 748_397_030
FLAC_SET_SHA256 = (
    "1FA0E381904391B759CB3E82FF60BC54716AD99FD30ED07FD49C2128EF6239E5"
)
LICENSE_SHA256 = (
    "E6BC9E9C474700B708F568BAC9E5A8A9BCB2B1DAD53442F5BA449FCB848B8E76"
)
README_SHA256 = (
    "BE275B843D10A22E614E5F52BD414FE2CBDCBFD6165894B1DCCA738E8CBF391A"
)
SFZ_SHA256 = (
    "C8B282F03FDB2D9E6BE24A99DF0D97A05E7ECE718D1A14E0B882C518161F7837"
)

EXPECTED_SCALARS: dict[str, str | int] = {
    "repository": (
        "https://github.com/sfzinstruments/SalamanderGrandPiano.git"
    ),
    "commit": COMMIT,
    "expectedTreeFileCount": TREE_COUNT,
    "expectedTreeBytes": TREE_BYTES,
    "expectedTreeSha256": TREE_SHA256,
    "expectedFlacFileCount": FLAC_COUNT,
    "expectedFlacBytes": FLAC_BYTES,
    "expectedFlacSetSha256": FLAC_SET_SHA256,
    "expectedLicenseSha256": LICENSE_SHA256,
    "expectedReadmeSha256": README_SHA256,
    "expectedSfzSha256": SFZ_SHA256,
}


def _read_installer() -> str:
    raw = INSTALLER.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(
            "钢琴安装器必须使用 UTF-8 BOM，确保 Windows PowerShell 5.1 "
            "能正确读取中文路径"
        )
    return raw.decode("utf-8-sig")


def _scalar(text: str, name: str) -> str | int:
    match = re.search(
        rf'(?m)^\${re.escape(name)}\s*=\s*(?:"([^"\r\n]*)"|(\d+))\s*$',
        text,
    )
    if match is None:
        raise AssertionError(f"PowerShell 标量 ${name} 缺失或不是固定值")
    quoted, integer = match.groups()
    return quoted if quoted is not None else int(integer)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _installed_tree_digest() -> dict[str, str | int]:
    paths: list[tuple[str, Path]] = []
    for directory, names, files in os.walk(ASSET_ROOT):
        names[:] = [name for name in names if name != ".git"]
        parent = Path(directory)
        for name in files:
            path = parent / name
            relative = path.relative_to(ASSET_ROOT).as_posix()
            paths.append((relative, path))
    paths.sort(key=lambda item: item[0])

    tree_records: list[str] = []
    flac_records: list[str] = []
    tree_bytes = 0
    flac_bytes = 0
    flac_count = 0
    for relative, path in paths:
        digest = _sha256(path).lower()
        tree_records.append(f"{digest}  {relative}\n")
        size = path.stat().st_size
        tree_bytes += size
        if relative.startswith("Samples/") and relative.lower().endswith(".flac"):
            flac_records.append(f"{digest}  {relative}\n")
            flac_bytes += size
            flac_count += 1

    return {
        "tree_count": len(paths),
        "tree_bytes": tree_bytes,
        "tree_sha256": hashlib.sha256(
            "".join(tree_records).encode("utf-8")
        ).hexdigest().upper(),
        "flac_count": flac_count,
        "flac_bytes": flac_bytes,
        "flac_set_sha256": hashlib.sha256(
            "".join(flac_records).encode("utf-8")
        ).hexdigest().upper(),
    }


class SalamanderInstallerTests(unittest.TestCase):
    def test_installer_pins_commit_complete_tree_samples_and_evidence(
        self,
    ) -> None:
        text = _read_installer()
        for name, expected in EXPECTED_SCALARS.items():
            with self.subTest(variable=name):
                self.assertEqual(_scalar(text, name), expected)

        compact = " ".join(text.split())
        self.assertIn(
            "& $git.Source -c "
            '"protocol.version=2" -C $stage fetch --depth 1 origin $commit',
            compact,
        )
        self.assertIn(
            "& $git.Source -c "
            '"advice.detachedHead=false" -C $stage checkout --detach $commit',
            compact,
        )
        self.assertNotRegex(
            text,
            r"(?mi)^\s*(?:&\s*)?git(?:\.exe)?\s+lfs\b",
        )
        self.assertNotRegex(text, r"clone\s+--depth\s+1")

    def test_digest_contract_is_ordinal_utf8_and_excludes_git_metadata(
        self,
    ) -> None:
        text = _read_installer()
        compact = " ".join(text.split())
        for required in (
            '$gitPrefix = ".git/"',
            "[Array]::Sort($paths, [StringComparer]::Ordinal)",
            '[void]$treeRecords.Append("  ")',
            '[void]$treeRecords.Append("`n")',
            '[void]$flacRecords.Append("  ")',
            "[Text.Encoding]::UTF8.GetBytes($treeRecords.ToString())",
            "[Text.Encoding]::UTF8.GetBytes($flacRecords.ToString())",
            'Assert-FileHash $Root "LICENSE" $expectedLicenseSha256',
            'Assert-FileHash $Root "README.md" $expectedReadmeSha256',
            (
                'Assert-FileHash $Root "Salamander Grand Piano V3.sfz" '
                "$expectedSfzSha256"
            ),
        ):
            with self.subTest(required=required):
                haystack = text if 'Append("  ")' in required else compact
                self.assertIn(required, haystack)

    def test_resource_report_and_generator_retain_commit_and_evidence(
        self,
    ) -> None:
        report = json.loads(AUDIT_REPORT.read_text(encoding="utf-8"))
        self.assertEqual(report["upstream_version"], "V3 48kHz 24bit")
        self.assertEqual(report["upstream_commit"], COMMIT)
        self.assertEqual(
            report["evidence_sha256"],
            {
                "LICENSE": LICENSE_SHA256.lower(),
                "README.md": README_SHA256.lower(),
            },
        )
        self.assertEqual(report["sample_count"], FLAC_COUNT)
        self.assertEqual(report["sample_bytes"], FLAC_BYTES)
        self.assertEqual(
            report["sample_set_sha256"],
            FLAC_SET_SHA256.lower(),
        )

        generator = AUDIT_SCRIPT.read_text(encoding="utf-8")
        self.assertIn(COMMIT, generator)
        self.assertIn(
            'evidence_files=("LICENSE", "README.md")',
            generator,
        )

    def test_render_attribution_names_the_license_uri_and_runtime_changes(
        self,
    ) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        attribution = manifest["attribution"]
        self.assertIn(
            "https://creativecommons.org/licenses/by/3.0/",
            attribution,
        )
        self.assertIn("FLAC files remain unmodified", attribution)
        self.assertIn("custom sample map", attribution)
        self.assertIn("band-limited resampling", attribution)

    def test_stage_is_verified_before_atomic_switch_and_has_rollback(
        self,
    ) -> None:
        compact = " ".join(_read_installer().split())
        existing_check = compact.index(
            "$installed = Assert-SalamanderTree $target $git.Source"
        )
        early_return = compact.index("return", existing_check)
        stage_check = compact.index(
            "$verified = Assert-SalamanderTree $stage $git.Source"
        )
        old_move = compact.index(
            "[IO.Directory]::Move( "
            "[IO.Path]::GetFullPath($target), "
            "[IO.Path]::GetFullPath($previous) )",
            stage_check,
        )
        final_move = compact.index(
            "[IO.Directory]::Move( "
            "[IO.Path]::GetFullPath($stage), "
            "[IO.Path]::GetFullPath($target) )",
            old_move,
        )
        final_check = compact.index(
            "$final = Assert-SalamanderTree $target $git.Source",
            final_move,
        )
        rollback_new = compact.index(
            "[IO.Directory]::Move( "
            "[IO.Path]::GetFullPath($target), "
            "[IO.Path]::GetFullPath($stage) )",
            final_check,
        )
        rollback_old = compact.index(
            "[IO.Directory]::Move( "
            "[IO.Path]::GetFullPath($previous), "
            "[IO.Path]::GetFullPath($target) )",
            rollback_new,
        )
        self.assertLess(existing_check, early_return)
        self.assertLess(stage_check, old_move)
        self.assertLess(old_move, final_move)
        self.assertLess(final_move, final_check)
        self.assertLess(final_check, rollback_new)
        self.assertLess(rollback_new, rollback_old)
        self.assertNotIn(
            "Move-Item -LiteralPath $stage -Destination $target",
            compact,
        )

    @unittest.skipUnless(ASSET_ROOT.is_dir(), "Salamander 音源尚未安装")
    @pytest.mark.external_assets
    def test_current_local_tree_matches_every_installer_pin(self) -> None:
        digest = _installed_tree_digest()
        self.assertEqual(digest["tree_count"], TREE_COUNT)
        self.assertEqual(digest["tree_bytes"], TREE_BYTES)
        self.assertEqual(digest["tree_sha256"], TREE_SHA256)
        self.assertEqual(digest["flac_count"], FLAC_COUNT)
        self.assertEqual(digest["flac_bytes"], FLAC_BYTES)
        self.assertEqual(digest["flac_set_sha256"], FLAC_SET_SHA256)
        self.assertEqual(_sha256(ASSET_ROOT / "LICENSE"), LICENSE_SHA256)
        self.assertEqual(_sha256(ASSET_ROOT / "README.md"), README_SHA256)
        self.assertEqual(
            _sha256(ASSET_ROOT / "Salamander Grand Piano V3.sfz"),
            SFZ_SHA256,
        )

        git = shutil.which("git")
        if git is None or not (ASSET_ROOT / ".git").exists():
            return
        result = subprocess.run(
            [
                git,
                "-c",
                f"safe.directory={ASSET_ROOT}",
                "-C",
                str(ASSET_ROOT),
                "rev-parse",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().lower(), COMMIT)

    def test_installer_parses_with_windows_powershell(self) -> None:
        executable = shutil.which("pwsh")
        if os.name == "nt":
            executable = shutil.which("powershell.exe") or executable
        if executable is None:
            self.skipTest("PowerShell 不可用；文本安装契约仍已检查")

        command = (
            "$tokens = $null; $errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:TIANLAI_PS1, [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        environment = os.environ.copy()
        environment["TIANLAI_PS1"] = str(INSTALLER)
        result = subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            encoding=locale.getpreferredencoding(False),
            errors="replace",
            env=environment,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
