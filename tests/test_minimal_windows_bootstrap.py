from __future__ import annotations

import locale
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
CMD = ROOT / "安装运行环境.cmd"
BOOTSTRAP = ROOT / "bootstrap_windows.ps1"


class MinimalWindowsBootstrapTests(unittest.TestCase):
    def test_cmd_uses_process_only_execution_policy_bypass(self) -> None:
        raw = CMD.read_bytes()
        self.assertTrue(all(byte < 128 for byte in raw))
        text = raw.decode("ascii")
        self.assertIn("-ExecutionPolicy Bypass", text)
        self.assertIn("-NoProfile", text)
        self.assertIn("bootstrap_windows.ps1", text)
        self.assertNotIn("Set-ExecutionPolicy", text)

    def test_bootstrap_is_bom_safe_minimal_and_idempotent(self) -> None:
        raw = BOOTSTRAP.read_bytes()
        self.assertTrue(
            raw.startswith(b"\xef\xbb\xbf"),
            "Windows PowerShell 5.1 Chinese text requires a UTF-8 BOM",
        )
        text = raw.decode("utf-8-sig")
        self.assertIn('Join-Path $root ".venv"', text)
        self.assertIn('"${root}[mcp]"', text)
        self.assertIn('"setuptools>=77"', text)
        self.assertIn('"--no-build-isolation"', text)
        self.assertIn('"tianlai.doctor"', text)
        self.assertIn("参考振荡器", text)
        self.assertIn("Test-Path -LiteralPath $venvPython", text)
        self.assertIn("foreach ($candidate in $candidates)", text)
        self.assertIn('"-3.14", "-3.13", "-3.12", "-3.11"', text)
        self.assertIn("Facts = $facts", text)
        self.assertIn("sys.implementation.name", text)
        self.assertIn('"cpython"', text)
        self.assertNotIn("Set-ExecutionPolicy", text)
        self.assertNotIn("Install-FluidSynth", text)
        self.assertNotIn("安装全部音源.ps1", text)
        self.assertNotIn("安装通用音源.ps1", text)

    def test_bootstrap_parses_with_windows_powershell(self) -> None:
        executable = shutil.which("pwsh")
        if os.name == "nt":
            executable = shutil.which("powershell.exe") or executable
        if executable is None:
            self.skipTest("PowerShell is unavailable")
        command = (
            "$tokens = $null; $errors = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile("
            "$env:TIANLAI_BOOTSTRAP, [ref]$tokens, [ref]$errors); "
            "if ($errors.Count -gt 0) { "
            "$errors | ForEach-Object { $_.ToString() }; exit 1 }"
        )
        environment = dict(**__import__("os").environ)
        environment["TIANLAI_BOOTSTRAP"] = str(BOOTSTRAP)
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

    def test_dependency_groups_keep_soundfont_optional(self) -> None:
        metadata = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        project = metadata["project"]
        dependencies = project["dependencies"]
        extras = project["optional-dependencies"]
        scripts = project["scripts"]
        self.assertNotIn("pyfluidsynth==1.4.0", dependencies)
        self.assertEqual(extras["soundfont"], ["pyfluidsynth==1.4.0"])
        self.assertIn("mcp==2.0.0", extras["mcp"])
        self.assertIn(
            "cryptography>=48,<49; sys_platform == 'darwin' and "
            "platform_machine == 'x86_64'",
            extras["mcp"],
        )
        self.assertTrue(any(item.startswith("pytest") for item in extras["dev"]))
        self.assertTrue(any(item.startswith("jsonschema") for item in extras["dev"]))
        self.assertEqual(scripts["tianlai-doctor"], "tianlai.doctor:main")

        core = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        soundfont = (ROOT / "requirements-soundfont.txt").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("pyfluidsynth", core)
        self.assertIn("-r requirements.txt", soundfont)
        self.assertIn("pyfluidsynth==1.4.0", soundfont)


if __name__ == "__main__":
    unittest.main()
