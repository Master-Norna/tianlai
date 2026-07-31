from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap_linux.sh"


class MinimalLinuxBootstrapTests(unittest.TestCase):
    def test_bootstrap_has_a_portable_minimal_contract(self) -> None:
        raw = BOOTSTRAP.read_bytes()
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r\n", raw)
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attributes.splitlines())
        text = raw.decode("utf-8")

        self.assertIn('venv_root="$root/.venv"', text)
        self.assertIn('venv_python="$venv_root/bin/python"', text)
        self.assertIn("3.11|3.12|3.13|3.14", text)
        self.assertIn("64-bit", text)
        self.assertIn("sys.implementation.name", text)
        self.assertIn("'cpython'", text)
        self.assertIn("Usage: bash ./bootstrap_linux.sh", text)
        self.assertIn('install_target="${root}[mcp]"', text)
        self.assertIn('install_target="${root}[mcp,dev]"', text)
        self.assertIn("--no-build-isolation", text)
        self.assertIn("-m tianlai.doctor", text)
        self.assertIn("参考振荡器", text)
        self.assertIn('args:    ["-m", "tianlai.mcp_entry"]', text)
        self.assertIn("not external_assets and not listening", text)
        self.assertIn("-m tianlai.resource_restore", text)

        self.assertNotIn("Scripts/python.exe", text.replace(
            '"$venv_root/Scripts/python.exe"', ""
        ))
        self.assertNotIn("powershell", text.casefold())
        self.assertNotIn("安装全部音源.ps1", text)
        self.assertNotIn("安装通用音源.ps1", text)
        self.assertNotIn("pyfluidsynth", text.casefold())

    def test_windows_virtual_environment_is_rejected_explicitly(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('"$venv_root/Scripts/python.exe"', text)
        self.assertIn("Windows environment", text)
        self.assertIn("separate checkout", text)

    def test_bootstrap_parses_with_bash_when_available(self) -> None:
        executable = shutil.which("bash")
        if executable is None:
            self.skipTest("Bash is unavailable")
        result = subprocess.run(
            [executable, "-n", str(BOOTSTRAP)],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
        if (
            result.returncode != 0
            and os.name == "nt"
            and Path(executable).parent.name.casefold() == "system32"
        ):
            self.skipTest("Windows bash shim cannot start its WSL instance")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_help_is_side_effect_free_when_bash_is_available(self) -> None:
        executable = shutil.which("bash")
        if executable is None:
            self.skipTest("Bash is unavailable")
        result = subprocess.run(
            [executable, str(BOOTSTRAP), "--help"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            check=False,
        )
        if (
            result.returncode != 0
            and os.name == "nt"
            and Path(executable).parent.name.casefold() == "system32"
        ):
            self.skipTest("Windows bash shim cannot start its WSL instance")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--portable-tests", result.stdout)
        self.assertIn("--skip-smoke", result.stdout)
        self.assertIn("--python", result.stdout)


if __name__ == "__main__":
    unittest.main()
