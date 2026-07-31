from __future__ import annotations

import locale
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMMON_INSTALLER = ROOT / "安装通用音源.ps1"
ALL_INSTALLER = ROOT / "安装全部音源.ps1"


def _read_windows_powershell(path: Path) -> str:
    raw = path.read_bytes()
    if not raw.startswith(b"\xef\xbb\xbf"):
        raise AssertionError(
            f"{path.relative_to(ROOT)} must retain a UTF-8 BOM for "
            "Windows PowerShell 5.1 Chinese-path compatibility"
        )
    return raw.decode("utf-8-sig")


class CommonSoundFontPolicyTests(unittest.TestCase):
    def test_common_banks_require_an_explicit_local_compatibility_switch(
        self,
    ) -> None:
        text = _read_windows_powershell(COMMON_INSTALLER)
        compact = " ".join(text.split())

        self.assertIn(
            "[switch] $InstallLocalCompatibilitySoundFonts",
            compact,
        )
        gate = compact.index("if ($InstallLocalCompatibilitySoundFonts)")
        general_user_install = compact.index(
            "Install-GeneralUser",
            compact.index("if ($InstallLocalCompatibilitySoundFonts)"),
        )
        timgm_install = compact.index(
            "Install-TimGmLocalCompatibility",
            compact.index("if ($InstallLocalCompatibilitySoundFonts)"),
        )
        self.assertLess(gate, general_user_install)
        self.assertLess(gate, timgm_install)
        self.assertIn("GeneralUser GS 上游说明承认", text)
        self.assertIn("GPL-2.0 条款没有明确的渲染音频输出例外", text)
        self.assertIn("不得进入天籁 public/trusted", text)

    def test_collection_installer_does_not_opt_into_local_banks(self) -> None:
        text = _read_windows_powershell(ALL_INSTALLER)

        self.assertIn('"安装通用音源.ps1"', text)
        self.assertNotIn("InstallLocalCompatibilitySoundFonts", text)
        self.assertIn("不默认安装许可未进入公开边界", text)

    def test_installers_parse_with_powershell(self) -> None:
        executable = shutil.which("pwsh")
        if os.name == "nt":
            executable = shutil.which("powershell.exe") or executable
        if executable is None:
            self.skipTest("PowerShell is unavailable; textual contracts were checked")

        parser_command = (
            "$tokens = $null; $errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:TIANLAI_PS1, [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        encoding = locale.getpreferredencoding(False)
        for path in (COMMON_INSTALLER, ALL_INSTALLER):
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
