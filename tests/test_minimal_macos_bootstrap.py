from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "bootstrap_macos.sh"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TAGGED_WORKFLOW = ROOT / ".github" / "workflows" / "tagged-source-release.yml"


def _usable_bash() -> str | None:
    candidates: list[Path] = []
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            candidates.extend(
                (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe")
            )
    discovered = shutil.which("bash")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in dict.fromkeys(candidates):
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            [str(candidate), "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode == 0:
            return str(candidate)
    return None


class MinimalMacOSBootstrapTests(unittest.TestCase):
    def test_intel_extra_selects_the_last_universal2_cryptography_line(self) -> None:
        metadata = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        mcp = metadata["project"]["optional-dependencies"]["mcp"]
        self.assertIn("mcp==1.28.1", mcp)
        self.assertIn(
            "cryptography>=48,<49; sys_platform == 'darwin' and "
            "platform_machine == 'x86_64'",
            mcp,
        )

    def test_bootstrap_has_a_native_portable_contract(self) -> None:
        raw = BOOTSTRAP.read_bytes()
        self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"))
        self.assertNotIn(b"\r\n", raw)
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.sh text eol=lf", attributes.splitlines())
        text = raw.decode("utf-8")

        self.assertIn('venv_root="$root/.venv"', text)
        self.assertIn('venv_python="$venv_root/bin/python"', text)
        self.assertIn("3.11|3.12|3.13|3.14", text)
        self.assertIn("native 64-bit", text)
        self.assertIn("platform.system()", text)
        self.assertIn("platform.machine()", text)
        self.assertIn("host_system=$(uname -s", text)
        self.assertIn("host_machine=$(uname -m", text)
        self.assertIn("sysctl.proc_translated", text)
        self.assertIn("Rosetta translation is active", text)
        self.assertIn("arm64|x86_64", text)
        self.assertIn("Usage: bash ./bootstrap_macos.sh", text)
        self.assertIn('install_target="${root}[mcp]"', text)
        self.assertIn('install_target="${root}[mcp,dev]"', text)
        self.assertNotIn("--no-build-isolation", text)
        self.assertIn("-m tianlai.doctor", text)
        self.assertIn("参考振荡器", text)
        self.assertIn("import soundfile as sf", text)
        self.assertIn('args:    ["-m", "tianlai.mcp_entry"]', text)
        self.assertIn("not external_assets and not listening", text)
        self.assertIn("-m tianlai.resource_restore", text)
        self.assertIn("all 74 external-resource entries", text)
        self.assertIn("15 frozen families", text)

        self.assertNotIn("powershell", text.casefold())
        self.assertNotIn("apt-get", text)
        self.assertNotIn("sudo", text)
        self.assertNotIn("安装全部音源.ps1", text)
        self.assertNotIn("安装通用音源.ps1", text)
        self.assertNotIn("pyfluidsynth", text.casefold())

    def test_windows_virtual_environment_is_rejected_explicitly(self) -> None:
        text = BOOTSTRAP.read_text(encoding="utf-8")
        self.assertIn('"$venv_root/Scripts/python.exe"', text)
        self.assertIn("Windows environment", text)
        self.assertIn("separate checkout", text)

    def test_bootstrap_parses_with_bash_when_available(self) -> None:
        executable = _usable_bash()
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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_help_is_side_effect_free_when_bash_is_available(self) -> None:
        executable = _usable_bash()
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
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("--portable-tests", result.stdout)
        self.assertIn("--skip-smoke", result.stdout)
        self.assertIn("--python", result.stdout)
        self.assertIn("Apple Silicon or Intel", result.stdout)

    def test_macos_workflows_pin_native_bash_and_render_a_real_smoke_bank(self) -> None:
        ci = CI_WORKFLOW.read_text(encoding="utf-8")
        tagged = TAGGED_WORKFLOW.read_text(encoding="utf-8")
        for runner, architecture, machine in (
            ("macos-15", "arm64", "arm64"),
            ("macos-15-intel", "x64", "x86_64"),
        ):
            for version in ("3.11", "3.12", "3.13", "3.14"):
                self.assertIn(
                    f"- runner: {runner}\n"
                    f"            architecture: {architecture}\n"
                    f"            machine: {machine}\n"
                    f'            python-version: "{version}"',
                    ci,
                )
        for runner, architecture, machine in (
            ("macos-26", "arm64", "arm64"),
            ("macos-26-intel", "x64", "x86_64"),
        ):
            self.assertIn(
                f"- runner: {runner}\n"
                f"            architecture: {architecture}\n"
                f"            machine: {machine}\n"
                '            python-version: "3.14"',
                ci,
            )
        self.assertEqual(tagged.count("- runner: macos-"), 4)
        self.assertIn("if: matrix.soundfont", tagged)
        for workflow in (ci, tagged):
            mac_job_name = (
                "macos-portable:" if workflow is ci else "macos-release-gate:"
            )
            mac_job = workflow[workflow.index(mac_job_name) :]
            if workflow is tagged:
                mac_job = mac_job[: mac_job.index("\n  attest-candidate:")]
            self.assertIn("actions/checkout@v6", workflow)
            self.assertIn("actions/setup-python@v6", workflow)
            self.assertNotIn("actions/checkout@v7", workflow)
            self.assertNotIn("actions/setup-python@v7", workflow)
            self.assertIn("macos-15", workflow)
            self.assertIn("macos-15-intel", workflow)
            self.assertIn("macos-26", workflow)
            self.assertIn("macos-26-intel", workflow)
            self.assertIn("architecture: arm64", workflow)
            self.assertIn("architecture: x64", workflow)
            self.assertIn("/bin/bash -n ./bootstrap_macos.sh", workflow)
            self.assertIn("/bin/bash ./bootstrap_macos.sh", workflow)
            self.assertIn("tests/test_smoke_soundfont_builder.py", workflow)
            self.assertIn(
                "native_backend_loads_unicode_path_and_renders_nonzero_audio",
                workflow,
            )
            self.assertIn("HOMEBREW_NO_AUTO_UPDATE", workflow)
            self.assertIn("attempt in 1 2 3", workflow)
            self.assertIn('TIANLAI_REQUIRE_BSDTAR: "1"', workflow)
            self.assertIn(
                "from tianlai.resource_restore import _find_bsdtar_executable",
                workflow,
            )
            self.assertIn("executable = _find_bsdtar_executable()", workflow)
            self.assertIn(
                'if test "$EXPECTED_MACHINE" = "x86_64"; then',
                mac_job,
            )
            self.assertIn(
                'version("cryptography")',
                mac_job,
            )
            self.assertIn(
                "Verified Intel-compatible cryptography",
                mac_job,
            )
            self.assertLess(
                mac_job.index("executable = _find_bsdtar_executable()"),
                mac_job.index("--portable-tests"),
            )
            self.assertIn(
                'TIANLAI_REQUIRE_NATIVE_FLUIDSYNTH: "1"',
                workflow,
            )
            self.assertIn('importlib.util.find_spec("fluidsynth")', workflow)
            self.assertIn(
                "runtime = prepare_fluidsynth_runtime(Path.cwd())",
                workflow,
            )
            self.assertIn("if runtime is None:", workflow)
            self.assertLess(
                mac_job.index('importlib.util.find_spec("fluidsynth")'),
                mac_job.index(
                    "native_backend_loads_unicode_path_and_renders_nonzero_audio"
                ),
            )


if __name__ == "__main__":
    unittest.main()
