from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    (
        "天籁音乐宪法-v0.1.md",
        "3c26f99806b2044b3fd45cbdc8ef12ffadf871d75dc119799881b0d992b75985",
        "C0.02",
    ),
    (
        "天籁音乐宪法-v0.1.en.md",
        "ca0cc236d93bca684a918f14814695835cf9aa437640294a4f02f898393903a9",
        "C0.03",
    ),
)


@pytest.mark.parametrize(
    "relative_path",
    tuple(
        path
        for filename, _expected_sha256, _clause_id in DOCUMENTS
        for path in (
            f"docs/音乐创作参考笔记/{filename}",
            f"tianlai/_resources/constitutions/{filename}",
        )
    ),
)
def test_constitution_sources_are_forced_to_lf_by_git(
    relative_path: str,
) -> None:
    checked = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "check-attr",
            "eol",
            "--",
            relative_path,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.rstrip().endswith(": eol: lf")


@pytest.mark.parametrize(("filename", "expected_sha256", "_clause_id"), DOCUMENTS)
def test_packaged_constitution_is_byte_exact_docs_copy(
    filename: str,
    expected_sha256: str,
    _clause_id: str,
) -> None:
    docs_payload = (
        ROOT / "docs" / "音乐创作参考笔记" / filename
    ).read_bytes()
    packaged_payload = (
        ROOT / "tianlai" / "_resources" / "constitutions" / filename
    ).read_bytes()

    assert packaged_payload == docs_payload
    assert hashlib.sha256(packaged_payload).hexdigest() == expected_sha256


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None,
    reason="optional mcp package is not installed",
)
def test_wheel_contains_constitutions_and_lookup_needs_no_repo_docs(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "wheel-source"
    staging.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", staging / "pyproject.toml")
    for filename in (
        "README.md",
        "LICENSE",
        "NOTICE",
        "OUTPUT_RIGHTS.md",
        "TRADEMARKS.md",
    ):
        shutil.copy2(ROOT / filename, staging / filename)
    shutil.copytree(ROOT / "tianlai", staging / "tianlai")
    distribution = staging / "dist"
    distribution.mkdir()

    built = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from setuptools.build_meta import build_wheel; "
                "print(build_wheel('dist'))"
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
    assert len(wheels) == 1

    installed = tmp_path / "installed"
    installed.mkdir()
    with ZipFile(wheels[0]) as archive:
        members = set(archive.namelist())
        for filename, expected_sha256, _clause_id in DOCUMENTS:
            member = f"tianlai/_resources/constitutions/{filename}"
            assert member in members
            payload = archive.read(member)
            assert hashlib.sha256(payload).hexdigest() == expected_sha256
        archive.extractall(installed)

    empty_working_directory = tmp_path / "empty-cwd"
    empty_working_directory.mkdir()
    assert not (empty_working_directory / "docs").exists()
    output = tmp_path / "runtime-output"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed)
    environment["TIANLAI_OUTPUT_DIR"] = str(output)
    environment.pop("TIANLAI_HOME", None)
    lookup_script = """
import json
from pathlib import Path
from tianlai import mcp_server

installed = Path(mcp_server.__file__).resolve()
assert 'installed' in installed.parts
zh = mcp_server.get_music_constitution_clauses(['C0.02'], 'zh-CN')
en = mcp_server.get_music_constitution_clauses(['C0.03'], 'en')
assert zh['ok'], zh
assert en['ok'], en
print(json.dumps({
    'zh': zh['constitution']['content_sha256'],
    'en': en['constitution']['content_sha256'],
    'package_file': installed.name,
}))
"""
    looked_up = subprocess.run(
        [sys.executable, "-c", lookup_script],
        cwd=empty_working_directory,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert looked_up.returncode == 0, looked_up.stdout + looked_up.stderr
    result = json.loads(looked_up.stdout)
    assert result == {
        "zh": DOCUMENTS[0][1],
        "en": DOCUMENTS[1][1],
        "package_file": "mcp_server.py",
    }

    tampered_resource = (
        installed
        / "tianlai"
        / "_resources"
        / "constitutions"
        / DOCUMENTS[0][0]
    )
    tampered_resource.write_bytes(tampered_resource.read_bytes() + b"\n")
    tamper_script = """
import json
from tianlai import mcp_server

print(json.dumps(
    mcp_server.get_music_constitution_clauses(['C0.02'], 'zh-CN'),
    ensure_ascii=False,
))
"""
    rejected = subprocess.run(
        [sys.executable, "-c", tamper_script],
        cwd=empty_working_directory,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert rejected.returncode == 0, rejected.stdout + rejected.stderr
    rejection = json.loads(rejected.stdout)
    assert rejection["ok"] is False
    assert rejection["error"]["code"] == (
        "creative_workflow.constitution_integrity_mismatch"
    )
    assert str(installed) not in rejected.stdout
    assert str(tmp_path) not in rejected.stdout
