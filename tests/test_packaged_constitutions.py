from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from zipfile import ZipFile

import pytest


ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    (
        "天籁音乐宪法-v0.2.md",
        "3ff471c09a08648db4c3f5cee5e4230932277278b68c89dc49872b4bbe2dc78d",
        "C0.06",
    ),
    (
        "天籁音乐宪法-v0.2.en.md",
        "f1291258812784ef64fa7a019cfaf9b250fc8ca279d8f97c68fc088362af3908",
        "C4.1.16",
    ),
)
CONSTITUTION_LF_RULES = {
    "docs/音乐创作参考笔记/天籁音乐宪法-v*.md text eol=lf",
    "tianlai/_resources/constitutions/*.md text eol=lf",
}
CONSTITUTION_CLAUSE_LINE = re.compile(
    r"^\* \*\*(?P<clause_id>C[0-8](?:\.[A-Z])?(?:\.[0-9]{1,3}){1,2})｜"
    r"[^*]+\*\*[：:]\s*.+$",
    flags=re.MULTILINE,
)
EXPECTED_CONSTITUTION_FILES = {filename for filename, _hash, _id in DOCUMENTS}
SETUPTOOLS_BUILD_REQUIREMENT = "setuptools>=77"


def _requirement_contains_version(requirement: str, version: str) -> bool:
    """Evaluate the build requirement without adding a test dependency.

    ``packaging`` is normally available either directly or through
    setuptools.  The conservative fallback supports this project's exact
    lower-bound contract so collection still works in a minimal environment.
    """

    try:
        from packaging.requirements import Requirement
    except ModuleNotFoundError:
        try:
            from setuptools._vendor.packaging.requirements import Requirement
        except (ImportError, ModuleNotFoundError):
            Requirement = None  # type: ignore[assignment,misc]
    if Requirement is not None:
        parsed = Requirement(requirement)
        return parsed.specifier.contains(version)

    matched_requirement = re.fullmatch(
        r"setuptools>=(\d+(?:\.\d+)*)",
        requirement,
        flags=re.IGNORECASE,
    )
    matched_version = re.match(
        r"(\d+(?:\.\d+)*)(.*)",
        version,
        flags=re.IGNORECASE,
    )
    if matched_requirement is None or matched_version is None:
        return False

    def release(value: str) -> tuple[int, ...]:
        fields = [int(field) for field in value.split(".")]
        while len(fields) > 1 and fields[-1] == 0:
            fields.pop()
        return tuple(fields)

    installed_release = release(matched_version.group(1))
    required_release = release(matched_requirement.group(1))
    suffix = matched_version.group(2).lower()
    if any(marker in suffix for marker in ("a", "b", "rc", "dev")):
        return False
    width = max(len(installed_release), len(required_release))
    return installed_release + (0,) * (width - len(installed_release)) >= (
        required_release + (0,) * (width - len(required_release))
    )


def _installed_setuptools_satisfies_build_contract() -> bool:
    if importlib.util.find_spec("setuptools") is None:
        return False
    try:
        installed = importlib.metadata.version("setuptools")
    except importlib.metadata.PackageNotFoundError:
        return False
    return _requirement_contains_version(
        SETUPTOOLS_BUILD_REQUIREMENT,
        installed,
    )


def test_gitattributes_forces_constitution_sources_to_lf() -> None:
    rules = {
        line.strip()
        for line in (ROOT / ".gitattributes")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert CONSTITUTION_LF_RULES <= rules


@pytest.mark.skipif(
    not (ROOT / ".git").exists(),
    reason="Git metadata is intentionally absent from source distributions",
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
def test_git_applies_constitution_lf_rules(
    relative_path: str,
) -> None:
    git = shutil.which("git")
    assert git is not None, "a Git checkout must have git available"
    checked = subprocess.run(
        [
            git,
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


def test_packaged_constitution_directory_contains_only_v02_bilingual_pair(
) -> None:
    resource_root = ROOT / "tianlai" / "_resources" / "constitutions"
    assert {path.name for path in resource_root.glob("*.md")} == (
        EXPECTED_CONSTITUTION_FILES
    )


def test_v02_bilingual_constitutions_have_the_same_150_clause_ids() -> None:
    docs_root = ROOT / "docs" / "音乐创作参考笔记"
    clause_sets: list[list[str]] = []
    for filename, _expected_sha256, _example_clause_id in DOCUMENTS:
        text = (docs_root / filename).read_text(encoding="utf-8")
        ids = [
            match.group("clause_id")
            for match in CONSTITUTION_CLAUSE_LINE.finditer(text)
        ]
        assert len(ids) == 150
        assert len(set(ids)) == 150
        clause_sets.append(ids)

    assert set(clause_sets[0]) == set(clause_sets[1])


def test_pyproject_declares_constitutions_as_package_data() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    setuptools = project["tool"]["setuptools"]

    assert setuptools["include-package-data"] is False
    assert setuptools["package-data"]["tianlai"] == [
        "_resources/constitutions/*.md"
    ]


def test_build_and_dev_contract_require_pep639_capable_setuptools() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_system = project["build-system"]
    package = project["project"]

    assert build_system["build-backend"] == "setuptools.build_meta"
    assert SETUPTOOLS_BUILD_REQUIREMENT in build_system["requires"]
    assert SETUPTOOLS_BUILD_REQUIREMENT in package["optional-dependencies"]["dev"]
    assert SETUPTOOLS_BUILD_REQUIREMENT not in package["dependencies"]


@pytest.mark.parametrize(
    ("version", "expected"),
    (
        ("76.9.9", False),
        ("77.0.0rc1", False),
        ("77.0.0", True),
        ("83.0.0", True),
    ),
)
def test_setuptools_build_requirement_gate(
    version: str,
    expected: bool,
) -> None:
    assert (
        _requirement_contains_version(SETUPTOOLS_BUILD_REQUIREMENT, version)
        is expected
    )


@pytest.mark.parametrize(
    ("installed", "expected"),
    (("76.9.9", False), ("77.0.0", True)),
)
def test_installed_setuptools_gate_uses_the_build_contract(
    monkeypatch: pytest.MonkeyPatch,
    installed: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: object() if name == "setuptools" else None,
    )
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: installed)

    assert _installed_setuptools_satisfies_build_contract() is expected


@pytest.mark.skipif(
    importlib.util.find_spec("mcp") is None
    or not _installed_setuptools_satisfies_build_contract(),
    reason=(
        "wheel lookup test needs optional mcp and an installed setuptools "
        "satisfying the pyproject build-system requirement"
    ),
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
        assert {
            member
            for member in members
            if member.startswith("tianlai/_resources/constitutions/")
            and member.endswith(".md")
        } == {
            f"tianlai/_resources/constitutions/{filename}"
            for filename in EXPECTED_CONSTITUTION_FILES
        }
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
zh = mcp_server.get_music_constitution_clauses(['C0.06'], 'zh-CN')
en = mcp_server.get_music_constitution_clauses(['C4.1.16'], 'en')
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
    mcp_server.get_music_constitution_clauses(['C0.06'], 'zh-CN'),
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
