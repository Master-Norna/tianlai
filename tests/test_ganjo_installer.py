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
INSTALLER = ROOT / "乐器" / "世界乐器" / "班卓琴" / "获取音源.ps1"
ALL_RESOURCES_INSTALLER = ROOT / "安装全部音源.ps1"
RESTORE_MANIFEST = ROOT / "resource_restore_manifest.json"
ASSET_ROOT = ROOT / "音源" / "itsclipping" / "ganjo-v1.000"

TAG = "v1.000"
COMMIT = "ccff5cd5cd3b513873a48994c07724d9d3c39e1c"
TREE_COUNT = 66
TREE_BYTES = 26_113_258
TREE_SHA256 = (
    "AA16FA9940BC962EDAAE6AC48E1552889781ADE51B0D4B2BAA2762408B0AF91F"
)
WAV_COUNT = 61
WAV_BYTES = 24_480_172
WAV_SET_SHA256 = (
    "B6C7D842CAB222F5AAE00CE2128AF495A16057D56925E51D7AFD125177229A46"
)
LICENSE_SHA256 = (
    "F4E7F373B9B996950337E8D41A4A2939C2D90B7725E9BAF3D5084A22717AD328"
)
README_SHA256 = (
    "B79A853A0B8D48D6FBC7CC64B0CC56C5738572BDDB075B2B277CFFA81E90A08D"
)
SFZ_SHA256 = (
    "9717CACBD1F12C55233B5EDC85D10FAD02229B231B7BD3188C4E7BF5227F3214"
)

EXPECTED_SCALARS: dict[str, str | int] = {
    "repository": "https://github.com/sfzinstruments/ganjo.git",
    "tag": TAG,
    "commit": COMMIT,
    "expectedTreeFileCount": TREE_COUNT,
    "expectedTreeBytes": TREE_BYTES,
    "expectedTreeSha256": TREE_SHA256,
    "expectedWavFileCount": WAV_COUNT,
    "expectedWavBytes": WAV_BYTES,
    "expectedWavSetSha256": WAV_SET_SHA256,
    "expectedLicenseSha256": LICENSE_SHA256,
    "expectedReadmeSha256": README_SHA256,
    "expectedSfzSha256": SFZ_SHA256,
}


def _read_installer() -> str:
    raw = INSTALLER.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(
            "ganjo 安装器必须使用 UTF-8 BOM，确保 Windows PowerShell 5.1 "
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
    wav_records: list[str] = []
    tree_bytes = 0
    wav_bytes = 0
    wav_count = 0
    for relative, path in paths:
        digest = _sha256(path).lower()
        tree_records.append(f"{digest}  {relative}\n")
        size = path.stat().st_size
        tree_bytes += size
        if relative.lower().endswith(".wav"):
            wav_records.append(f"{digest}  {relative}\n")
            wav_bytes += size
            wav_count += 1

    return {
        "tree_count": len(paths),
        "tree_bytes": tree_bytes,
        "tree_sha256": hashlib.sha256(
            "".join(tree_records).encode("utf-8")
        ).hexdigest().upper(),
        "wav_count": wav_count,
        "wav_bytes": wav_bytes,
        "wav_set_sha256": hashlib.sha256(
            "".join(wav_records).encode("utf-8")
        ).hexdigest().upper(),
    }


class GanjoInstallerTests(unittest.TestCase):
    def test_installer_pins_tag_commit_complete_tree_samples_and_evidence(
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
        self.assertNotRegex(text, r"(?mi)^\s*(?:&\s*)?git(?:\.exe)?\s+lfs\b")
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
            '[void]$wavRecords.Append("  ")',
            "[Text.Encoding]::UTF8.GetBytes($treeRecords.ToString())",
            "[Text.Encoding]::UTF8.GetBytes($wavRecords.ToString())",
            'Assert-FileHash $Root "LICENSE.md" $expectedLicenseSha256',
            'Assert-FileHash $Root "README.md" $expectedReadmeSha256',
            'Assert-FileHash $Root "ganjo.sfz" $expectedSfzSha256',
        ):
            with self.subTest(required=required):
                haystack = text if 'Append("  ")' in required else compact
                self.assertIn(required, haystack)

    def test_existing_target_is_validated_and_never_replaced(self) -> None:
        text = _read_installer()
        compact = " ".join(text.split())
        existing_guard = compact.index(
            "if (Test-Path -LiteralPath $target)"
        )
        existing_check = compact.index(
            "$installed = Assert-GanjoTree $target $git.Source",
            existing_guard,
        )
        early_return = compact.index("return", existing_check)
        stage_creation = compact.index(
            "New-Item -ItemType Directory -Force -Path $resourceRoot",
            early_return,
        )
        self.assertLess(existing_guard, existing_check)
        self.assertLess(existing_check, early_return)
        self.assertLess(early_return, stage_creation)
        self.assertIn(
            "安装器不会替换或删除它",
            compact,
        )
        self.assertNotIn("$previous", text)
        self.assertNotIn("$destinationMoved", text)

    def test_stage_is_verified_before_atomic_switch_and_rolls_back(self) -> None:
        compact = " ".join(_read_installer().split())
        stage_check = compact.index(
            "$verified = Assert-GanjoTree $stage $git.Source"
        )
        final_move = compact.index(
            "[IO.Directory]::Move( "
            "[IO.Path]::GetFullPath($stage), "
            "[IO.Path]::GetFullPath($target) )",
            stage_check,
        )
        final_check = compact.index(
            "$final = Assert-GanjoTree $target $git.Source",
            final_move,
        )
        rollback = compact.index(
            "[IO.Directory]::Move( "
            "[IO.Path]::GetFullPath($target), "
            "[IO.Path]::GetFullPath($stage) )",
            final_check,
        )
        self.assertLess(stage_check, final_move)
        self.assertLess(final_move, final_check)
        self.assertLess(final_check, rollback)
        self.assertNotIn(
            "Move-Item -LiteralPath $stage -Destination $target",
            compact,
        )

    def test_root_installer_routes_ganjo_through_the_unified_manifest(self) -> None:
        text = ALL_RESOURCES_INSTALLER.read_text(encoding="utf-8-sig")
        self.assertIn('"tianlai.resource_restore"', text)
        self.assertIn("resource_restore_manifest.json", text)
        self.assertNotIn('乐器\\世界乐器\\班卓琴\\获取音源.ps1', text)
        document = json.loads(RESTORE_MANIFEST.read_text(encoding="utf-8"))
        family = next(
            item
            for item in document["families"]
            if item["id"] == "itsclipping-ganjo"
        )
        self.assertEqual(family["instrument_ids"], ["世界乐器/班卓琴"])
        self.assertEqual(family["source"]["commit"], COMMIT)

    @unittest.skipUnless(ASSET_ROOT.is_dir(), "ganjo 音源尚未安装")
    @pytest.mark.external_assets
    def test_current_local_tree_matches_every_installer_pin(self) -> None:
        digest = _installed_tree_digest()
        self.assertEqual(digest["tree_count"], TREE_COUNT)
        self.assertEqual(digest["tree_bytes"], TREE_BYTES)
        self.assertEqual(digest["tree_sha256"], TREE_SHA256)
        self.assertEqual(digest["wav_count"], WAV_COUNT)
        self.assertEqual(digest["wav_bytes"], WAV_BYTES)
        self.assertEqual(digest["wav_set_sha256"], WAV_SET_SHA256)
        self.assertEqual(_sha256(ASSET_ROOT / "LICENSE.md"), LICENSE_SHA256)
        self.assertEqual(_sha256(ASSET_ROOT / "README.md"), README_SHA256)
        self.assertEqual(_sha256(ASSET_ROOT / "ganjo.sfz"), SFZ_SHA256)

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
