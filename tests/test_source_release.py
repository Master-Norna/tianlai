from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "build_source_release.py"
SPEC = importlib.util.spec_from_file_location(
    "tianlai_build_source_release",
    TOOL_PATH,
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

EXPECTED_PUBLIC_MARKDOWN_PAIRS = (
    ("README.md", "README.en.md"),
    ("CONTRIBUTING.md", "CONTRIBUTING.en.md"),
    ("SECURITY.md", "SECURITY.en.md"),
    ("TRADEMARKS.md", "TRADEMARKS.en.md"),
    ("OUTPUT_RIGHTS.md", "OUTPUT_RIGHTS.en.md"),
    ("docs/README.md", "docs/README.en.md"),
    ("docs/Linux快速开始.md", "docs/Linux快速开始.en.md"),
    ("docs/macOS快速开始.md", "docs/macOS快速开始.en.md"),
    ("docs/MCP.md", "docs/MCP.en.md"),
    (
        "docs/VPO音源许可与安装说明.md",
        "docs/VPO音源许可与安装说明.en.md",
    ),
    ("docs/Windows安装与巡检.md", "docs/Windows安装与巡检.en.md"),
    ("docs/Windows最小启动.md", "docs/Windows最小启动.en.md"),
    (
        "docs/从乐谱到第二次渲染.md",
        "docs/从乐谱到第二次渲染.en.md",
    ),
    ("docs/当前状态.md", "docs/当前状态.en.md"),
    ("docs/音源许可政策.md", "docs/音源许可政策.en.md"),
    (
        "docs/音乐创作参考笔记/README.md",
        "docs/音乐创作参考笔记/README.en.md",
    ),
    (
        "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.md",
        "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.en.md",
    ),
    ("output/README.md", "output/README.en.md"),
    ("音源/README.md", "音源/README.en.md"),
    ("乐谱/README.md", "乐谱/README.en.md"),
)
EXPECTED_PUBLIC_DOCUMENTS = frozenset(
    {
        "docs/音源许可例外.json",
        *(
            path
            for pair in EXPECTED_PUBLIC_MARKDOWN_PAIRS
            for path in pair
        ),
    }
)
EXPECTED_PUBLIC_MUSIC_REFERENCE_DOCUMENTS = frozenset(
    {
        "docs/音乐创作参考笔记/README.md",
        "docs/音乐创作参考笔记/README.en.md",
        "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.md",
        "docs/音乐创作参考笔记/天籁音乐宪法-v0.1.en.md",
    }
)
REPOSITORY_ONLY_MUSIC_REFERENCE_PDF = (
    "docs/音乐创作参考笔记/音乐的“好听”能否被量化.pdf"
)
EXPECTED_LIFECYCLE_ANCHORS = frozenset(
    {
        "output/README.md",
        "output/README.en.md",
        "音源/README.md",
        "音源/README.en.md",
        "乐谱/README.md",
        "乐谱/README.en.md",
    }
)
EXPECTED_INSTRUMENT_DOCUMENT_PAIRS = (
    ("README.md", "README.en.md"),
    ("来源.md", "来源.en.md"),
)
EXPECTED_INSTRUMENT_DOCUMENT_NAMES = frozenset(
    name
    for pair in EXPECTED_INSTRUMENT_DOCUMENT_PAIRS
    for name in pair
)
EXPECTED_FIXTURE_INSTRUMENT_DOCUMENTS = frozenset(
    f"乐器/键盘乐器/钢琴/{name}"
    for name in EXPECTED_INSTRUMENT_DOCUMENT_NAMES
)
EXPECTED_REPOSITORY_ONLY_DOCUMENTS = frozenset(
    {
        "CHANGELOG.md",
        "CHANGELOG.en.md",
        "INTERNAL_NOTES.md",
        "docs/maintainer/operator-notes.md",
        "docs/maintainer/operator-notes.en.md",
        "docs/maintainer/release-process.md",
        "docs/design/output-notes.md",
        "docs/research/instrument-ledger.md",
        "docs/research/creative-notes.md",
        "docs/research/quality-check.md",
        REPOSITORY_ONLY_MUSIC_REFERENCE_PDF,
        "乐器/键盘乐器/钢琴/resource-evaluation.md",
        "乐器/键盘乐器/钢琴/resource-evaluation.en.md",
    }
)
EXPECTED_EXCLUDED_ROOT_DOCUMENTS = frozenset(
    {
        "人工听审/README.md",
        "发布包/README.md",
    }
)


class SourceReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.output_root = self.root / "releases"
        self._git("init", "--quiet")
        self._git("config", "user.name", "Release Test")
        self._git("config", "user.email", "release@example.invalid")
        self._git("config", "core.autocrlf", "false")
        self._seed_project()
        self._git("add", "-A")
        self._git("commit", "--quiet", "-m", "seed")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _git(
        self,
        *arguments: str,
        input_bytes: bytes | None = None,
    ) -> bytes:
        completed = subprocess.run(
            ["git", "-C", str(self.repo), *arguments],
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode:
            self.fail(
                completed.stderr.decode("utf-8", errors="replace")
                or f"git exited with {completed.returncode}"
            )
        return completed.stdout

    def _write(self, relative: str, content: str | bytes) -> None:
        path = self.repo.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            path.write_bytes(content)

    def _seed_project(self) -> None:
        self._write(
            "pyproject.toml",
            (
                "[project]\n"
                'name = "tianlai-test"\n'
                'version = "0.4.7"\n'
                'license-files = ["LICENSE", "NOTICE", "OUTPUT_RIGHTS.md"]\n'
            ),
        )
        self._write("LICENSE", "Apache License 2.0 test fixture\n")
        self._write("NOTICE", "Copyright Test Author\n")
        self._write("OUTPUT_RIGHTS.md", "# Output rights\n")
        self._write("README.md", "# Test project\n")
        self._write("tianlai/__init__.py", '__version__ = "0.4.7"\n')
        self._write("tianlai/core.py", "VALUE = 'committed'\n")
        self._write("乐器/键盘乐器/钢琴/乐器.json", '{"name": "钢琴"}\n')
        self._write("乐器/键盘乐器/钢琴/乐器.py", "ENGINE = True\n")
        for chinese, english in EXPECTED_INSTRUMENT_DOCUMENT_PAIRS:
            prefix = "乐器/键盘乐器/钢琴/"
            self._write(
                prefix + chinese,
                f"[English]({english})\n\n# Public instrument documentation\n",
            )
            self._write(
                prefix + english,
                f"[Chinese]({chinese})\n\n# Public instrument documentation\n",
            )
        self._write("schemas/score.schema.json", '{"type": "object"}\n')
        self._write("examples/demo.events.json", '{"events": []}\n')
        for path in EXPECTED_PUBLIC_DOCUMENTS:
            self._write(path, "# Public documentation\n")
        for path in EXPECTED_REPOSITORY_ONLY_DOCUMENTS:
            self._write(path, "# Repository-only documentation\n")
        for path in EXPECTED_EXCLUDED_ROOT_DOCUMENTS:
            self._write(path, "# Excluded root documentation\n")
        self._write(
            "CHANGELOG.md",
            (
                "# Historical ledger\n\n"
                "[Internal audit](docs/maintainer/operator-notes.md)\n"
            ),
        )
        self._write(
            "安装环境.ps1",
            "\ufeffWrite-Host 'install'\r\n".encode("utf-8"),
        )
        self._write(
            "启动.cmd",
            b"@echo off\r\nexit /b 0\r\n",
        )

        # All of these are deliberately tracked. The builder's release policy,
        # not .gitignore, must keep them out.
        self._write(".venv/large.bin", b"environment")
        self._write("音源/private.wav", b"RIFFnot-a-release-asset")
        self._write("音源/README.md", "# Resources\n")
        self._write("output/render.wav", b"render")
        self._write("output/README.md", "# Outputs\n")
        self._write("发布包/old.zip", b"old archive")
        self._write("乐谱/private.mid", b"MThd")
        self._write("乐谱/README.md", "# Scores\n")
        self._write("人工听审/private.json", "{}")
        self._write("cache/state.bin", b"cache")
        self._write("module/__pycache__/core.pyc", b"bytecode")
        self._write("scratch.tmp", b"temporary")

    def _manifest(self, archive_path: Path) -> tuple[dict, zipfile.ZipFile]:
        archive = zipfile.ZipFile(archive_path, "r")
        document = json.loads(
            archive.read(release.MANIFEST_NAME).decode("utf-8")
        )
        return document, archive

    def test_clean_release_is_complete_auditable_and_deterministic(self) -> None:
        first = self.output_root / "first.zip"
        second = self.output_root / "second.zip"
        result = release.build_source_release(self.repo, first)
        release.build_source_release(self.repo, second)

        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(
            result["archive_sha256"],
            hashlib.sha256(first.read_bytes()).hexdigest(),
        )

        manifest, archive = self._manifest(first)
        with archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertIsNone(archive.testzip())
            self.assertIn("tianlai/core.py", names)
            self.assertIn("乐器/键盘乐器/钢琴/乐器.json", names)
            self.assertIn("乐器/键盘乐器/钢琴/乐器.py", names)
            self.assertTrue(
                EXPECTED_FIXTURE_INSTRUMENT_DOCUMENTS.issubset(names)
            )
            self.assertIn("schemas/score.schema.json", names)
            self.assertIn("examples/demo.events.json", names)
            self.assertTrue(EXPECTED_PUBLIC_DOCUMENTS.issubset(names))
            self.assertIn("安装环境.ps1", names)
            self.assertIn("启动.cmd", names)
            self.assertIn("音源/README.md", names)
            self.assertIn("output/README.md", names)
            self.assertIn("乐谱/README.md", names)
            for required in (
                "LICENSE",
                "NOTICE",
                "OUTPUT_RIGHTS.md",
                "pyproject.toml",
                release.MANIFEST_NAME,
            ):
                self.assertIn(required, names)

            for excluded in (
                ".venv/large.bin",
                "音源/private.wav",
                "output/render.wav",
                "发布包/old.zip",
                "乐谱/private.mid",
                "人工听审/private.json",
                "cache/state.bin",
                "module/__pycache__/core.pyc",
                "scratch.tmp",
                *EXPECTED_REPOSITORY_ONLY_DOCUMENTS,
            ):
                self.assertNotIn(excluded, names)

            chinese = archive.getinfo("乐器/键盘乐器/钢琴/乐器.json")
            self.assertTrue(chinese.flag_bits & 0x800)
            self.assertEqual(chinese.date_time, (1980, 1, 1, 0, 0, 0))
            self.assertEqual(
                archive.read("安装环境.ps1"),
                "\ufeffWrite-Host 'install'\r\n".encode("utf-8"),
            )
            self.assertEqual(
                archive.read("启动.cmd"),
                b"@echo off\r\nexit /b 0\r\n",
            )

            records = {row["path"]: row for row in manifest["files"]}
            self.assertNotIn(release.MANIFEST_NAME, records)
            self.assertEqual(manifest["file_count"], len(records))
            self.assertEqual(
                set(records) | {release.MANIFEST_NAME},
                set(names),
            )
            for path, record in records.items():
                payload = archive.read(path)
                self.assertEqual(record["size"], len(payload))
                self.assertEqual(
                    record["sha256"],
                    hashlib.sha256(payload).hexdigest(),
                )

        self.assertEqual(
            manifest["format"],
            release.MANIFEST_FORMAT,
        )
        self.assertEqual(manifest["format_version"], 2)
        self.assertEqual(manifest["project_version"], "0.4.7")
        self.assertEqual(
            manifest["commit"],
            self._git("rev-parse", "HEAD").decode("ascii").strip(),
        )
        self.assertFalse(manifest["dirty"])
        self.assertFalse(manifest["local_test_only"])
        self.assertGreater(
            manifest["exclusions"]["excluded_tracked_path_count"],
            len(EXPECTED_REPOSITORY_ONLY_DOCUMENTS),
        )
        self.assertEqual(
            set(manifest["exclusions"]["included_lifecycle_anchors"]),
            set(EXPECTED_LIFECYCLE_ANCHORS),
        )
        self.assertEqual(
            set(manifest["exclusions"]["public_document_allowlist"]),
            set(EXPECTED_PUBLIC_DOCUMENTS),
        )
        self.assertEqual(release._PUBLIC_DOCUMENT_PATHS, EXPECTED_PUBLIC_DOCUMENTS)
        self.assertEqual(
            {
                path
                for path in release._PUBLIC_DOCUMENT_PATHS
                if path.startswith("docs/音乐创作参考笔记/")
            },
            EXPECTED_PUBLIC_MUSIC_REFERENCE_DOCUMENTS,
        )
        self.assertNotIn(
            REPOSITORY_ONLY_MUSIC_REFERENCE_PDF,
            release._PUBLIC_DOCUMENT_PATHS,
        )
        self.assertEqual(
            release._PUBLIC_MARKDOWN_PAIRS,
            EXPECTED_PUBLIC_MARKDOWN_PAIRS,
        )
        self.assertEqual(
            release._ROOT_LIFECYCLE_ANCHORS,
            EXPECTED_LIFECYCLE_ANCHORS,
        )
        self.assertEqual(
            release._PUBLIC_INSTRUMENT_DOCUMENT_PAIRS,
            EXPECTED_INSTRUMENT_DOCUMENT_PAIRS,
        )
        self.assertEqual(
            release._PUBLIC_INSTRUMENT_DOCUMENT_NAMES,
            EXPECTED_INSTRUMENT_DOCUMENT_NAMES,
        )
        self.assertEqual(
            set(
                manifest["exclusions"][
                    "public_instrument_document_names"
                ]
            ),
            set(EXPECTED_INSTRUMENT_DOCUMENT_NAMES),
        )
        self.assertEqual(
            release._REPOSITORY_ONLY_ROOT_DOCUMENT_PATHS,
            {"CHANGELOG.md", "CHANGELOG.en.md"},
        )
        self.assertEqual(
            manifest["exclusions"]["repository_only_document_count"],
            len(EXPECTED_REPOSITORY_ONLY_DOCUMENTS)
            + len(EXPECTED_EXCLUDED_ROOT_DOCUMENTS),
        )
        self.assertIn(
            "explicit public allowlist",
            manifest["exclusions"]["policy"],
        )
        serialized_manifest = json.dumps(manifest, ensure_ascii=False)
        for hidden_path in (
            *EXPECTED_REPOSITORY_ONLY_DOCUMENTS,
            *EXPECTED_EXCLUDED_ROOT_DOCUMENTS,
            "人工听审/private.json",
            "发布包/old.zip",
            "音源/private.wav",
            ".venv/large.bin",
        ):
            self.assertNotIn(hidden_path, serialized_manifest)

    def test_public_documents_are_required_nonempty_and_link_closed(self) -> None:
        self._write("docs/README.md", " \n")
        self._git("add", "docs/README.md")
        self._git("commit", "--quiet", "-m", "empty public document")
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "public release documents",
        ):
            release.build_source_release(
                self.repo,
                self.output_root / "empty-public-doc.zip",
            )

    def test_public_markdown_translation_is_required(self) -> None:
        target = "docs/macOS快速开始.en.md"
        self._git("rm", "--quiet", target)
        self._git("commit", "--quiet", "-m", "remove public translation")
        output = self.output_root / "missing-public-translation.zip"
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "public release documents",
        ) as raised:
            release.build_source_release(self.repo, output)
        self.assertIn(target, str(raised.exception))
        self.assertFalse(output.exists())

    def test_instrument_chinese_document_requires_english_pair(self) -> None:
        target = "乐器/键盘乐器/钢琴/来源.en.md"
        self._git("rm", "--quiet", target)
        self._git("commit", "--quiet", "-m", "remove instrument translation")
        output = self.output_root / "missing-instrument-english.zip"
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "instrument documentation must be bilingual",
        ) as raised:
            release.build_source_release(self.repo, output)
        self.assertIn(target, str(raised.exception))
        self.assertFalse(output.exists())

    def test_instrument_english_document_requires_chinese_pair(self) -> None:
        target = "乐器/键盘乐器/钢琴/README.md"
        self._git("rm", "--quiet", target)
        self._git("commit", "--quiet", "-m", "remove instrument source text")
        output = self.output_root / "missing-instrument-chinese.zip"
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "instrument documentation must be bilingual",
        ) as raised:
            release.build_source_release(self.repo, output)
        self.assertIn(target, str(raised.exception))
        self.assertFalse(output.exists())

    def test_instrument_public_document_must_be_nonempty(self) -> None:
        target = "乐器/键盘乐器/钢琴/README.en.md"
        self._write(target, " \n")
        self._git("add", target)
        self._git("commit", "--quiet", "-m", "empty instrument translation")
        output = self.output_root / "empty-instrument-translation.zip"
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "public instrument documents must be non-empty",
        ) as raised:
            release.build_source_release(self.repo, output)
        self.assertIn(target, str(raised.exception))
        self.assertFalse(output.exists())

    def test_public_markdown_cannot_link_to_repository_only_document(
        self,
    ) -> None:
        target = "docs/maintainer/operator-notes.md"
        self._write(
            "docs/README.md",
            f"# Public documentation\n\n[Notes](maintainer/operator-notes.md)\n",
        )
        self._git("add", "docs/README.md")
        self._git("commit", "--quiet", "-m", "add excluded documentation link")
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "Markdown link target is not included",
        ):
            release.build_source_release(
                self.repo,
                self.output_root / "broken-public-link.zip",
            )
        self.assertFalse((self.output_root / "broken-public-link.zip").exists())
        self.assertTrue((self.repo / target).is_file())

    def test_dirty_staged_repository_document_deletion_hides_its_path(
        self,
    ) -> None:
        target = "docs/maintainer/operator-notes.md"
        self._git("rm", "--quiet", target)
        output = self.output_root / "hidden-staged-document.zip"
        release.build_source_release(
            self.repo,
            output,
            allow_dirty=True,
        )
        manifest, archive = self._manifest(output)
        with archive:
            self.assertNotIn(target, archive.namelist())
        serialized = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn(target, serialized)
        self.assertEqual(
            manifest["exclusions"]["repository_only_document_count"],
            len(EXPECTED_REPOSITORY_ONLY_DOCUMENTS)
            + len(EXPECTED_EXCLUDED_ROOT_DOCUMENTS),
        )

    def test_dirty_tree_is_rejected_or_explicitly_marked_local_only(self) -> None:
        self._write("tianlai/core.py", b"VALUE = 'working tree'\n")
        self._write("untracked-secret.txt", "never publish\n")
        rejected = self.output_root / "rejected.zip"
        with self.assertRaisesRegex(release.ReleaseBuildError, "dirty"):
            release.build_source_release(self.repo, rejected)
        self.assertFalse(rejected.exists())

        output = self.output_root / "dirty.zip"
        release.build_source_release(
            self.repo,
            output,
            allow_dirty=True,
        )
        manifest, archive = self._manifest(output)
        with archive:
            self.assertEqual(
                archive.read("tianlai/core.py"),
                b"VALUE = 'working tree'\n",
            )
            self.assertNotIn("untracked-secret.txt", archive.namelist())
        self.assertTrue(manifest["dirty"])
        self.assertTrue(manifest["allow_dirty_requested"])
        self.assertTrue(manifest["local_test_only"])

    def test_committed_cmd_rejects_bom_and_non_crlf_line_endings(self) -> None:
        invalid = {
            "utf8-bom": b"\xef\xbb\xbf@echo off\r\n",
            "invalid-utf8": b"@echo off\r\n\xff\r\n",
            "bare-lf": b"@echo off\nexit /b 0\n",
            "bare-cr": b"@echo off\rexit /b 0\r",
        }
        for label, content in invalid.items():
            with self.subTest(label=label):
                path = f"invalid-{label}.cmd"
                self._write(path, content)
                self._git("add", path)
                self._git("commit", "--quiet", "-m", f"add {label}")
                with self.assertRaisesRegex(
                    release.ReleaseBuildError,
                    "byte-order mark|UTF-8|CRLF",
                ):
                    release.build_source_release(
                        self.repo,
                        self.output_root / f"{label}.zip",
                    )
                self._git("rm", "--quiet", path)
                self._git("commit", "--quiet", "-m", f"remove {label}")

    def test_dirty_cmd_uses_the_same_byte_contract(self) -> None:
        self._write("启动.cmd", b"@echo off\nexit /b 0\n")
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "CRLF",
        ):
            release.build_source_release(
                self.repo,
                self.output_root / "dirty-cmd.zip",
                allow_dirty=True,
            )

    def test_dirty_snapshot_aggregates_unstaged_deletions(self) -> None:
        (self.repo / "tianlai" / "core.py").unlink()
        output = self.output_root / "dirty-deletion.zip"
        release.build_source_release(
            self.repo,
            output,
            allow_dirty=True,
        )
        manifest, archive = self._manifest(output)
        with archive:
            self.assertNotIn("tianlai/core.py", archive.namelist())
        self.assertNotIn(
            "excluded_tracked_paths",
            manifest["exclusions"],
        )
        self.assertGreater(
            manifest["exclusions"]["excluded_tracked_path_count"],
            0,
        )

    def test_dirty_snapshot_aggregates_staged_deletions_from_commit(self) -> None:
        self._git("rm", "--quiet", "tianlai/core.py")
        output = self.output_root / "staged-deletion.zip"
        release.build_source_release(
            self.repo,
            output,
            allow_dirty=True,
        )
        manifest, archive = self._manifest(output)
        with archive:
            self.assertNotIn("tianlai/core.py", archive.namelist())
        self.assertNotIn(
            "excluded_tracked_paths",
            manifest["exclusions"],
        )
        self.assertGreater(
            manifest["exclusions"]["excluded_tracked_path_count"],
            0,
        )

    def test_tracked_symbolic_link_mode_is_rejected_without_following(self) -> None:
        object_id = self._git(
            "hash-object",
            "-w",
            "--stdin",
            input_bytes=b"tianlai/core.py",
        ).decode("ascii").strip()
        self._git(
            "update-index",
            "--add",
            "--cacheinfo",
            f"120000,{object_id},linked-core.py",
        )

        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "symbolic links",
        ):
            release.build_source_release(
                self.repo,
                self.output_root / "link.zip",
                allow_dirty=True,
            )

    def test_missing_legal_file_and_version_mismatch_fail_closed(self) -> None:
        self._git("rm", "--quiet", "NOTICE")
        self._git("commit", "--quiet", "-m", "remove notice")
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "required release files",
        ):
            release.build_source_release(
                self.repo,
                self.output_root / "missing-notice.zip",
            )

        self._write("NOTICE", "restored\n")
        self._write("tianlai/__init__.py", '__version__ = "9.9.9"\n')
        self._git("add", "-A")
        self._git("commit", "--quiet", "-m", "restore with bad version")
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "does not match",
        ):
            release.build_source_release(
                self.repo,
                self.output_root / "version-mismatch.zip",
            )

    def test_expected_release_version_is_checked_before_publication(
        self,
    ) -> None:
        accepted = self.output_root / "accepted-version.zip"
        result = release.build_source_release(
            self.repo,
            accepted,
            expected_version="0.4.7",
        )
        self.assertEqual(result["project_version"], "0.4.7")
        self.assertTrue(accepted.is_file())

        rejected = self.output_root / "rejected-version.zip"
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "expected release version",
        ):
            release.build_source_release(
                self.repo,
                rejected,
                expected_version="0.4.8",
            )
        self.assertFalse(rejected.exists())

    def test_every_pyproject_license_file_must_be_tracked_and_nonempty(
        self,
    ) -> None:
        pyproject = (
            self.repo / "pyproject.toml"
        ).read_text(encoding="utf-8")
        pyproject = pyproject.replace(
            '"OUTPUT_RIGHTS.md"]',
            '"OUTPUT_RIGHTS.md", "LEGAL_EXTRA.md"]',
        )
        self._write("pyproject.toml", pyproject)
        self._git("add", "pyproject.toml")
        self._git("commit", "--quiet", "-m", "declare untracked legal file")

        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "declares a missing",
        ):
            release.build_source_release(
                self.repo,
                self.output_root / "missing-declared-license.zip",
            )

    def test_portability_policy_rejects_unsafe_names_and_collisions(self) -> None:
        invalid = (
            "../escape.py",
            r"folder\escape.py",
            "CON.txt",
            "CONIN$.txt",
            "COM¹.txt",
            "LPT³.log",
            "trailing./file.py",
            "bad:name.py",
            ("a" * 241) + ".py",
        )
        for path in invalid:
            with self.subTest(path=path):
                with self.assertRaises(release.ReleaseBuildError):
                    release._validate_portable_path(path)

        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "not Unicode NFC",
        ):
            release._validate_portable_path("docs/Cafe\u0301.md")

        entries = [
            release.TrackedFile("Case.py", "100644", "a" * 40),
            release.TrackedFile("case.py", "100644", "b" * 40),
            *[
                release.TrackedFile(name, "100644", "c" * 40)
                for name in sorted(release._REQUIRED_ROOT_FILES)
            ],
        ]
        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "collide",
        ):
            release._select_release_entries(entries)

        normalisation_entries = [
            release.TrackedFile("Caf\u00e9.py", "100644", "a" * 40),
            release.TrackedFile("Cafe\u0301.py", "100644", "b" * 40),
            *[
                release.TrackedFile(name, "100644", "c" * 40)
                for name in sorted(release._REQUIRED_ROOT_FILES)
            ],
        ]
        with self.assertRaises(release.ReleaseBuildError):
            release._select_release_entries(normalisation_entries)

    def test_default_refuses_overwrite_and_failed_replace_preserves_old_zip(
        self,
    ) -> None:
        output = self.output_root / "release.zip"
        release.build_source_release(self.repo, output)
        before = output.read_bytes()
        with self.assertRaises(FileExistsError):
            release.build_source_release(self.repo, output)
        self.assertEqual(output.read_bytes(), before)

        with (
            mock.patch.object(
                release.os,
                "replace",
                side_effect=OSError("simulated replace failure"),
            ),
            self.assertRaisesRegex(OSError, "simulated replace failure"),
        ):
            release.build_source_release(
                self.repo,
                output,
                overwrite=True,
            )
        self.assertEqual(output.read_bytes(), before)
        leftovers = list(self.output_root.glob(".tianlai-source-release.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_output_may_not_overwrite_a_tracked_zip(self) -> None:
        tracked_output = self.repo / "docs" / "artifact.zip"
        tracked_output.write_bytes(b"tracked artifact")
        self._git("add", "docs/artifact.zip")
        self._git("commit", "--quiet", "-m", "track an archive")
        before = tracked_output.read_bytes()

        with self.assertRaisesRegex(
            release.ReleaseBuildError,
            "Git-tracked path",
        ):
            release.build_source_release(
                self.repo,
                tracked_output,
                overwrite=True,
            )
        self.assertEqual(tracked_output.read_bytes(), before)
        self.assertEqual(self._git("status", "--porcelain"), b"")

    def test_output_alias_identity_cannot_bypass_tracked_zip_guard(self) -> None:
        tracked_output = self.repo / "docs" / "artifact.zip"
        tracked_output.write_bytes(b"tracked artifact")
        self._git("add", "docs/artifact.zip")
        self._git("commit", "--quiet", "-m", "track an archive")
        alias_output = self.root / "RUNNER~1" / "docs" / "artifact.zip"
        real_identity = release._canonical_path_identity

        def simulated_windows_identity(path: Path, *, purpose: str) -> Path:
            if path == alias_output.absolute():
                return tracked_output.resolve()
            return real_identity(path, purpose=purpose)

        with (
            mock.patch.object(
                release,
                "_canonical_path_identity",
                side_effect=simulated_windows_identity,
            ),
            self.assertRaisesRegex(
                release.ReleaseBuildError,
                "Git-tracked path",
            ),
        ):
            release.build_source_release(
                self.repo,
                alias_output,
                overwrite=True,
            )

        self.assertEqual(tracked_output.read_bytes(), b"tracked artifact")
        self.assertEqual(self._git("status", "--porcelain"), b"")

    def test_output_identity_change_before_publication_fails_closed(self) -> None:
        output = self.output_root / "identity-change.zip"
        initial_identity = output.resolve(strict=False)
        changed_identity = self.root / "redirected" / output.name
        real_identity = release._canonical_path_identity
        output_identity_calls = 0

        def changing_identity(path: Path, *, purpose: str) -> Path:
            nonlocal output_identity_calls
            if path == output.absolute():
                output_identity_calls += 1
                if output_identity_calls == 1:
                    return initial_identity
                return changed_identity
            return real_identity(path, purpose=purpose)

        with (
            mock.patch.object(
                release,
                "_canonical_path_identity",
                side_effect=changing_identity,
            ),
            self.assertRaisesRegex(
                release.ReleaseBuildError,
                "identity changed",
            ),
        ):
            release.build_source_release(self.repo, output)

        self.assertEqual(output_identity_calls, 2)
        self.assertFalse(output.exists())
        self.assertEqual(
            list(self.output_root.glob(".tianlai-source-release.*.tmp")),
            [],
        )

    def test_output_parent_link_cannot_bypass_tracked_path_guard(self) -> None:
        tracked_output = self.repo / "docs" / "artifact.zip"
        tracked_output.write_bytes(b"tracked artifact")
        self._git("add", "docs/artifact.zip")
        self._git("commit", "--quiet", "-m", "track an archive")
        alias = self.repo / "alias"
        if os.name == "nt":
            linked = subprocess.run(
                [
                    "cmd.exe",
                    "/d",
                    "/c",
                    "mklink",
                    "/J",
                    str(alias),
                    str(self.repo / "docs"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if linked.returncode:
                self.skipTest("could not create a Windows junction")
        else:
            try:
                alias.symlink_to(self.repo / "docs", target_is_directory=True)
            except OSError:
                self.skipTest("could not create a directory symlink")

        try:
            with self.assertRaisesRegex(
                release.ReleaseBuildError,
                "symbolic link or reparse point",
            ):
                release.build_source_release(
                    self.repo,
                    alias / "artifact.zip",
                    allow_dirty=True,
                    overwrite=True,
                )
            self.assertEqual(tracked_output.read_bytes(), b"tracked artifact")
        finally:
            if os.name == "nt":
                alias.rmdir()
            else:
                alias.unlink()

    def test_post_publish_temporary_cleanup_cannot_report_false_failure(
        self,
    ) -> None:
        output = self.output_root / "cleanup-warning.zip"
        real_unlink = release.Path.unlink

        def fail_private_unlink(path, *args, **kwargs):
            if path.name.startswith(".tianlai-source-release."):
                raise OSError("simulated temporary cleanup failure")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(
            release.Path,
            "unlink",
            autospec=True,
            side_effect=fail_private_unlink,
        ):
            result = release.build_source_release(self.repo, output)

        self.assertTrue(zipfile.is_zipfile(output))
        self.assertEqual(
            result["archive_sha256"],
            hashlib.sha256(output.read_bytes()).hexdigest(),
        )
        leftovers = list(self.output_root.glob(".tianlai-source-release.*.tmp"))
        self.assertEqual(len(leftovers), 1)
        real_unlink(leftovers[0])

    def test_failed_first_publish_leaves_no_partial_output(self) -> None:
        output = self.output_root / "release.zip"
        with (
            mock.patch.object(
                release.os,
                "link",
                side_effect=OSError("simulated publish failure"),
            ),
            self.assertRaisesRegex(OSError, "simulated publish failure"),
        ):
            release.build_source_release(self.repo, output)
        self.assertFalse(output.exists())
        leftovers = list(self.output_root.glob(".tianlai-source-release.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_cli_uses_the_same_clean_repository_contract(self) -> None:
        output = self.output_root / "cli.zip"
        completed = subprocess.run(
            [
                sys.executable,
                str(TOOL_PATH),
                "--repo",
                str(self.repo),
                "--output",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        summary = json.loads(completed.stdout.decode("utf-8"))
        self.assertEqual(Path(summary["output"]), output)
        self.assertEqual(summary["project_version"], "0.4.7")
        self.assertFalse(summary["dirty"])
        self.assertTrue(zipfile.is_zipfile(output))


if __name__ == "__main__":
    unittest.main()
