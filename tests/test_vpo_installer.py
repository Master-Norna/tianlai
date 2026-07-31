from __future__ import annotations

import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "安装VPO音源.ps1"
ALL_RESOURCES_INSTALLER = ROOT / "安装全部音源.ps1"
DELEGATES = (
    ROOT / "乐器" / "管弦乐" / "木管组" / "长笛" / "获取音源.ps1",
    ROOT / "乐器" / "管弦乐" / "弦乐组" / "小提琴" / "获取音源.ps1",
    ROOT / "乐器" / "管弦乐" / "弦乐组" / "大提琴" / "获取音源.ps1",
)
POWERSHELL_SCRIPTS = (INSTALLER, ALL_RESOURCES_INSTALLER, *DELEGATES)

EXPECTED_SCALARS: dict[str, str | int] = {
    "waveArchiveName": "Virtual-Playing-Orchestra3-2-wave-files.zip",
    "waveUrl": (
        "https://virtualplaying.com/go/"
        "virtual-playing-orchestra-v3-2-wave-files-archive/"
    ),
    "waveBytes": 616_114_842,
    "waveSha256": (
        "CA8F1E0B56EEDE35314994646E5F1F307EC349616C967FBECF627C43AA646E90"
    ),
    "scriptArchiveName": (
        "Virtual-Playing-Orchestra3-3-standard-scripts.zip"
    ),
    "scriptUrl": (
        "https://virtualplaying.com/go/"
        "virtual-playing-orchestra-v3-3-standard-scripts/"
    ),
    "scriptBytes": 544_010,
    "scriptSha256": (
        "F0F2BF0E42D2A39C5F49401ADDCFFA840FD8F5525670F5945BF5093A5442BDA5"
    ),
    "expectedTreeDirectory": "Virtual-Playing-Orchestra3",
    "expectedTreeFileCount": 1_922,
    "expectedTreeBytes": 724_695_982,
    "expectedTreeSha256": (
        "B06390C70D9D701481BC6DB0CF13B6ED6F3EF6B660DAC9A51034B9BE368DF317"
    ),
    "expectedLicenseSha256": (
        "852E3BE507B193625EAF76BD18F4740209287781FB95F2A06D78AE9205D4682E"
    ),
}


def _read_powershell(path: Path) -> str:
    raw = path.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(
            f"{path.relative_to(ROOT)} must use a UTF-8 BOM for "
            "Windows PowerShell 5.1 Chinese-path compatibility"
        )
    return raw.decode("utf-8-sig")


def _scalar(text: str, name: str) -> str | int:
    match = re.search(
        rf'(?m)^\${re.escape(name)}\s*=\s*(?:"([^"\r\n]*)"|(\d+))\s*$',
        text,
    )
    if match is None:
        raise AssertionError(f"PowerShell scalar ${name} is missing or dynamic")
    quoted, integer = match.groups()
    return quoted if quoted is not None else int(integer)


class VpoInstallerTests(unittest.TestCase):
    def test_installer_pins_both_archives_and_the_complete_tree(self) -> None:
        text = _read_powershell(INSTALLER)
        for name, expected in EXPECTED_SCALARS.items():
            with self.subTest(variable=name):
                self.assertEqual(_scalar(text, name), expected)

        compact = " ".join(text.split())
        self.assertIn(
            'Get-VerifiedArchive "VPO Wave Files 3.2" '
            "$waveUrl $waveZip $waveBytes $waveSha256",
            compact,
        )
        self.assertIn(
            'Get-VerifiedArchive "VPO Standard Orchestra 3.3" '
            "$scriptUrl $scriptZip $scriptBytes $scriptSha256",
            compact,
        )

    def test_tree_digest_contract_is_ordinal_utf8_and_checked_before_move(
        self,
    ) -> None:
        text = _read_powershell(INSTALLER)
        compact = " ".join(text.split())
        self.assertIn('$records.Append("  ")', text)
        self.assertIn('$records.Append("`n")', text)
        for required in (
            "[Array]::Sort($paths, [StringComparer]::Ordinal)",
            "[Text.Encoding]::UTF8.GetBytes($records.ToString())",
            'Join-Path $tree "Documentation\\license.htm"',
            "(Get-Sha256 $license) -ne $expectedLicenseSha256",
            "Assert-VpoTree $destination",
            "Assert-VpoTree $stage",
        ):
            with self.subTest(required=required):
                self.assertIn(required, compact)

        wave_expand = compact.index(
            "Expand-Archive -LiteralPath $waveZip "
            "-DestinationPath $stage -Force"
        )
        script_expand = compact.index(
            "Expand-Archive -LiteralPath $scriptZip "
            "-DestinationPath $stage -Force"
        )
        tree_check = compact.index("$verified = Assert-VpoTree $stage")
        final_move = compact.index(
            "Move-Item -LiteralPath $stage -Destination $destination"
        )
        self.assertLess(wave_expand, script_expand)
        self.assertLess(script_expand, tree_check)
        self.assertLess(tree_check, final_move)

    def test_instrument_installers_are_delegates_only(self) -> None:
        expected_statements = [
            '$ErrorActionPreference = "Stop"',
            (
                "$root = [IO.Path]::GetFullPath((Join-Path "
                '$PSScriptRoot "..\\..\\..\\.."))'
            ),
            '& (Join-Path $root "安装VPO音源.ps1")',
        ]
        for path in DELEGATES:
            text = _read_powershell(path)
            statements = [
                line.strip()
                for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
            with self.subTest(script=path.relative_to(ROOT).as_posix()):
                self.assertEqual(statements, expected_statements)
                self.assertNotRegex(
                    text,
                    r"https?://|curl|Invoke-WebRequest|Expand-Archive|"
                    r"Get-FileHash|Move-Item|Remove-Item",
                )

    def test_root_collection_installer_uses_the_central_vpo_installer(
        self,
    ) -> None:
        text = _read_powershell(ALL_RESOURCES_INSTALLER)
        self.assertEqual(text.count('"安装VPO音源.ps1"'), 1)
        self.assertNotIn("木管组\\长笛\\获取音源.ps1", text)

    def test_relevant_scripts_parse_with_powershell(self) -> None:
        executable = shutil.which("pwsh")
        if os.name == "nt":
            executable = shutil.which("powershell.exe") or executable
        if executable is None:
            self.skipTest(
                "PowerShell is unavailable; textual installer contracts "
                "were still checked"
            )

        parser_command = (
            "$tokens = $null; $errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:TIANLAI_PS1, [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        encoding = locale.getpreferredencoding(False)
        for path in POWERSHELL_SCRIPTS:
            environment = os.environ.copy()
            environment["TIANLAI_PS1"] = str(path)
            result = subprocess.run(
                [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    parser_command,
                ],
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                env=environment,
                timeout=30,
                check=False,
            )
            with self.subTest(script=path.relative_to(ROOT).as_posix()):
                self.assertEqual(
                    result.returncode,
                    0,
                    result.stdout + result.stderr,
                )


if __name__ == "__main__":
    unittest.main()
