from __future__ import annotations

import importlib.metadata
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tomllib
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_VERSION = tomllib.loads(
    (ROOT / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
EXPECTED_RUNTIME_MODULES = frozenset(
    {
        "tianlai/_console_encoding.py",
        "tianlai/cli.py",
        "tianlai/realization.py",
        "tianlai/realization_compile.py",
        "tianlai/score_source.py",
        "tianlai/score_v2.py",
        "tianlai/score_v2_migration.py",
        "tianlai/score_v2_execution_profile.py",
        "tianlai/score_v2_capability_source.py",
        "tianlai/score_v2_capability_adapter.py",
        "tianlai/score_v2_plan.py",
        "tianlai/score_v2_time.py",
        "tianlai/score_v2_runtime_source.py",
        "tianlai/score_v2_runtime_authority.py",
        "tianlai/score_v2_performance.py",
        "tianlai/score_v2_project_render.py",
        "tianlai/score_v2_candidate.py",
        "tianlai/score_v2_formal_render.py",
        "tianlai/score_v2_private_wav.py",
        "tianlai/score_v2_renderer.py",
    }
)


def _setuptools_is_build_capable() -> bool:
    if importlib.util.find_spec("setuptools") is None:
        return False
    try:
        major = int(importlib.metadata.version("setuptools").split(".", 1)[0])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return False
    return major >= 77


@pytest.mark.skipif(
    not _setuptools_is_build_capable(),
    reason="distribution test requires the declared setuptools>=77 builder",
)
def test_score_v2_runtime_is_closed_in_wheel_and_engine_sdist(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "source"
    staging.mkdir()
    for filename in (
        "pyproject.toml",
        "MANIFEST.in",
        "README.pypi.md",
        "LICENSE",
        "NOTICE",
        "OUTPUT_RIGHTS.md",
        "TRADEMARKS.md",
    ):
        shutil.copy2(ROOT / filename, staging / filename)
    shutil.copytree(ROOT / "tianlai", staging / "tianlai")
    # Copy the public Schemas deliberately.  PyPI artifacts are the reusable
    # engine and must not accidentally absorb the formal source ZIP's public
    # contract tree; the runtime modules above do not load these files.
    shutil.copytree(ROOT / "schemas", staging / "schemas")

    distribution = staging / "dist"
    distribution.mkdir()
    built = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from setuptools.build_meta import build_sdist, build_wheel; "
                "print(build_wheel('dist')); print(build_sdist('dist'))"
            ),
        ],
        cwd=staging,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr

    wheels = list(distribution.glob("*.whl"))
    sdists = list(distribution.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1

    installed = tmp_path / "installed"
    installed.mkdir()
    with ZipFile(wheels[0]) as archive:
        wheel_members = set(archive.namelist())
        assert EXPECTED_RUNTIME_MODULES.issubset(wheel_members)
        assert not any(name.startswith("schemas/") for name in wheel_members)
        metadata_name = next(
            name for name in wheel_members if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
        assert f"Version: {PROJECT_VERSION}" in metadata.splitlines()
        archive.extractall(installed)

    with tarfile.open(sdists[0], mode="r:gz") as archive:
        sdist_members = {
            "/".join(Path(name).parts[1:]) for name in archive.getnames()
        }
        assert EXPECTED_RUNTIME_MODULES.issubset(sdist_members)
        assert "README.pypi.md" in sdist_members
        assert not any(name.startswith("schemas/") for name in sdist_members)
        sdist_source = tmp_path / "sdist-source"
        archive.extractall(sdist_source)

    sdist_roots = [path for path in sdist_source.iterdir() if path.is_dir()]
    assert len(sdist_roots) == 1

    empty_cwd = tmp_path / "empty-cwd"
    empty_cwd.mkdir()
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed)
    # Reproduce a Western Windows redirected console.  The installed CLI must
    # establish UTF-8 itself before argparse writes Unicode help text.
    environment["PYTHONUTF8"] = "0"
    environment["PYTHONIOENCODING"] = "cp1252:strict"
    help_result = subprocess.run(
        [sys.executable, "-m", "tianlai", "--help"],
        cwd=empty_cwd,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert help_result.returncode == 0, help_result.stdout + help_result.stderr
    assert "migrate-score-v2" in help_result.stdout
    assert "project-render" in help_result.stdout
    assert "project-render-v2" in help_result.stdout

    project_render_v2_help = subprocess.run(
        [
            sys.executable,
            "-m",
            "tianlai",
            "project-render-v2",
            "--help",
        ],
        cwd=empty_cwd,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert project_render_v2_help.returncode == 0, (
        project_render_v2_help.stdout + project_render_v2_help.stderr
    )
    for required_option in (
        "--score",
        "--roster",
        "--execution-profile",
        "--sample-rate",
    ):
        assert required_option in project_render_v2_help.stdout

    legacy_project_render_help = subprocess.run(
        [sys.executable, "-m", "tianlai", "project-render", "--help"],
        cwd=empty_cwd,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert legacy_project_render_help.returncode == 0, (
        legacy_project_render_help.stdout + legacy_project_render_help.stderr
    )
    assert "--render-profile" in legacy_project_render_help.stdout
    assert "--execution-profile" not in legacy_project_render_help.stdout

    sdist_environment = os.environ.copy()
    sdist_environment["PYTHONPATH"] = str(sdist_roots[0])
    sdist_environment["PYTHONUTF8"] = "0"
    sdist_environment["PYTHONIOENCODING"] = "cp1252:strict"
    sdist_v2_help = subprocess.run(
        [
            sys.executable,
            "-m",
            "tianlai",
            "project-render-v2",
            "--help",
        ],
        cwd=empty_cwd,
        env=sdist_environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert sdist_v2_help.returncode == 0, (
        sdist_v2_help.stdout + sdist_v2_help.stderr
    )
    assert "--execution-profile" in sdist_v2_help.stdout

    migration_help = subprocess.run(
        [sys.executable, "-m", "tianlai", "migrate-score-v2", "--help"],
        cwd=empty_cwd,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert migration_help.returncode == 0, (
        migration_help.stdout + migration_help.stderr
    )
    assert "migration bundle" in migration_help.stdout
    assert "does not render score-v2" in migration_help.stdout

    version_result = subprocess.run(
        [sys.executable, "-m", "tianlai", "--version"],
        cwd=empty_cwd,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == f"tianlai {PROJECT_VERSION}"

    import_result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import tianlai.realization, tianlai.realization_compile, "
                "tianlai.cli, "
                "tianlai.score_source, tianlai.score_v2, "
                "tianlai.score_v2_migration, "
                "tianlai.score_v2_execution_profile, "
                "tianlai.score_v2_capability_source, "
                "tianlai.score_v2_capability_adapter, "
                "tianlai.score_v2_plan, "
                "tianlai.score_v2_time, "
                "tianlai.score_v2_runtime_source, "
                "tianlai.score_v2_runtime_authority, "
                "tianlai.score_v2_performance, "
                "tianlai.score_v2_project_render, "
                "tianlai.score_v2_candidate, "
                "tianlai.score_v2_formal_render, "
                "tianlai.score_v2_renderer; "
                "from tianlai.candidate import CANDIDATE_VERSION; "
                "from tianlai.score_v2_candidate import "
                "SCORE_V2_CANDIDATE_VERSION; "
                "assert CANDIDATE_VERSION == 2; "
                "assert SCORE_V2_CANDIDATE_VERSION == 3"
            ),
        ],
        cwd=empty_cwd,
        env=environment,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        timeout=30,
    )
    assert import_result.returncode == 0, (
        import_result.stdout + import_result.stderr
    )
