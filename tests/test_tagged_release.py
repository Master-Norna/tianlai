from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "build_tagged_release.py"
WORKFLOW_PATH = (
    ROOT / ".github" / "workflows" / "tagged-source-release.yml"
)
SPEC = importlib.util.spec_from_file_location(
    "tianlai_build_tagged_release",
    TOOL_PATH,
)
assert SPEC is not None and SPEC.loader is not None
tagged_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tagged_release
SPEC.loader.exec_module(tagged_release)


class TaggedReleaseTests(unittest.TestCase):
    def _git(self, repo: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(repo), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(
            completed.returncode,
            0,
            completed.stderr.decode("utf-8", errors="replace"),
        )
        return completed.stdout.decode("utf-8").strip()

    def _seed_release_repo(
        self,
        root: Path,
        *,
        version: str = "0.5.0rc2",
    ) -> Path:
        repo = root / "repo"
        repo.mkdir()
        self._git(repo, "init", "--quiet")
        self._git(repo, "config", "user.name", "Release Test")
        self._git(
            repo,
            "config",
            "user.email",
            "release@example.invalid",
        )
        self._git(repo, "config", "core.autocrlf", "false")
        (repo / "tianlai").mkdir()
        (repo / "pyproject.toml").write_text(
            (
                "[project]\n"
                'name = "tianlai-test"\n'
                f'version = "{version}"\n'
                "license-files = "
                '["LICENSE", "NOTICE", "OUTPUT_RIGHTS.md"]\n'
            ),
            encoding="utf-8",
        )
        (repo / "tianlai" / "__init__.py").write_text(
            f'__version__ = "{version}"\n',
            encoding="utf-8",
        )
        (repo / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
        (repo / "NOTICE").write_text("Test Author\n", encoding="utf-8")
        (repo / "OUTPUT_RIGHTS.md").write_text(
            "# Output rights\n",
            encoding="utf-8",
        )
        (repo / "README.md").write_text("# Fixture\n", encoding="utf-8")
        for relative in tagged_release.source_release._PUBLIC_DOCUMENT_PATHS:
            document = repo.joinpath(*relative.split("/"))
            document.parent.mkdir(parents=True, exist_ok=True)
            document.write_text(
                "{}\n" if document.suffix == ".json" else "# Fixture\n",
                encoding="utf-8",
            )
        self._git(repo, "add", "-A")
        self._git(repo, "commit", "--quiet", "-m", "initial release")
        return repo

    def test_tag_encodes_one_portable_exact_version(self) -> None:
        self.assertEqual(
            tagged_release.version_from_tag("v0.5.0rc2"),
            "0.5.0rc2",
        )
        self.assertEqual(
            tagged_release.version_from_tag("v1.2.3+build.7"),
            "1.2.3+build.7",
        )
        for invalid in (
            "",
            "0.5.0",
            "v",
            "v1..2",
            "v1/2",
            "v1\\2",
            "v../escape",
            "v1 2",
            "v版本1",
        ):
            with self.subTest(tag=invalid):
                with self.assertRaises(
                    tagged_release.TaggedReleaseError
                ):
                    tagged_release.version_from_tag(invalid)

    def test_build_emits_an_exact_checksum_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "release"
            payload = b"complete source archive"

            def fake_build(repo, output, *, expected_version):
                self.assertEqual(repo, ROOT)
                self.assertEqual(expected_version, "0.5.0rc2")
                Path(output).write_bytes(payload)
                return {
                    "project_version": expected_version,
                    "archive_sha256": hashlib.sha256(payload).hexdigest(),
                    "commit": "a" * 40,
                    "dirty": False,
                }

            with mock.patch.object(
                tagged_release,
                "resolve_tagged_head",
                return_value=(ROOT, "a" * 40),
            ), mock.patch.object(
                tagged_release.source_release,
                "build_source_release",
                side_effect=fake_build,
            ):
                result = tagged_release.build_tagged_release(
                    ROOT,
                    output_dir,
                    tag="v0.5.0rc2",
                )

            archive = Path(result["archive"])
            checksum = Path(result["checksum"])
            digest = hashlib.sha256(payload).hexdigest()
            self.assertEqual(
                archive.name,
                "tianlai-0.5.0rc2-source.zip",
            )
            self.assertEqual(archive.read_bytes(), payload)
            self.assertEqual(
                checksum.read_text(encoding="ascii"),
                f"{digest}  {archive.name}\n",
            )
            self.assertEqual(result["archive_sha256"], digest)
            self.assertEqual(result["tag"], "v0.5.0rc2")

    def test_incomplete_pair_is_removed_without_deleting_a_racing_file(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary) / "release"
            payload = b"complete source archive"
            digest = hashlib.sha256(payload).hexdigest()

            def fake_build(repo, output, *, expected_version):
                Path(output).write_bytes(payload)
                return {
                    "project_version": expected_version,
                    "archive_sha256": digest,
                    "commit": "b" * 40,
                    "dirty": False,
                }

            def checksum_race(target, **kwargs):
                target.write_bytes(b"another publisher")
                raise FileExistsError("simulated checksum race")

            with (
                mock.patch.object(
                    tagged_release,
                    "resolve_tagged_head",
                    return_value=(ROOT, "b" * 40),
                ),
                mock.patch.object(
                    tagged_release.source_release,
                    "build_source_release",
                    side_effect=fake_build,
                ),
                mock.patch.object(
                    tagged_release,
                    "_write_checksum",
                    side_effect=checksum_race,
                ),
                self.assertRaises(FileExistsError),
            ):
                tagged_release.build_tagged_release(
                    ROOT,
                    output_dir,
                    tag="v0.5.0rc2",
                )

            archive = output_dir / "tianlai-0.5.0rc2-source.zip"
            checksum = archive.with_suffix(".zip.sha256")
            self.assertFalse(archive.exists())
            self.assertEqual(checksum.read_bytes(), b"another publisher")

    def test_nonexistent_tag_is_rejected_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._seed_release_repo(root)
            output_dir = root / "release"
            with (
                mock.patch.object(
                    tagged_release.source_release,
                    "build_source_release",
                ) as builder,
                self.assertRaisesRegex(
                    tagged_release.TaggedReleaseError,
                    "does not exist",
                ),
            ):
                tagged_release.build_tagged_release(
                    repo,
                    output_dir,
                    tag="v0.5.0rc2",
                )
            builder.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_tag_pointing_at_another_commit_is_rejected_before_build(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = self._seed_release_repo(root)
            self._git(repo, "tag", "v0.5.0rc2")
            (repo / "README.md").write_text(
                "# Later commit\n",
                encoding="utf-8",
            )
            self._git(repo, "add", "README.md")
            self._git(repo, "commit", "--quiet", "-m", "later")
            output_dir = root / "release"
            with (
                mock.patch.object(
                    tagged_release.source_release,
                    "build_source_release",
                ) as builder,
                self.assertRaisesRegex(
                    tagged_release.TaggedReleaseError,
                    "does not point at current HEAD",
                ),
            ):
                tagged_release.build_tagged_release(
                    repo,
                    output_dir,
                    tag="v0.5.0rc2",
                )
            builder.assert_not_called()
            self.assertFalse(output_dir.exists())

    def test_lightweight_and_annotated_tags_at_head_build(self) -> None:
        for annotated in (False, True):
            with self.subTest(annotated=annotated):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    repo = self._seed_release_repo(root)
                    arguments = ["tag"]
                    if annotated:
                        arguments.extend(["--annotate", "--message", "release"])
                    arguments.append("v0.5.0rc2")
                    self._git(repo, *arguments)
                    # actions/checkout checks a tag event out at a detached
                    # HEAD while retaining refs/tags/<name>.
                    self._git(
                        repo,
                        "checkout",
                        "--quiet",
                        "--detach",
                        "v0.5.0rc2",
                    )

                    result = tagged_release.build_tagged_release(
                        repo,
                        root / "release",
                        tag="v0.5.0rc2",
                    )

                    self.assertEqual(
                        result["commit"],
                        self._git(repo, "rev-parse", "HEAD"),
                    )
                    self.assertTrue(Path(result["archive"]).is_file())
                    self.assertTrue(Path(result["checksum"]).is_file())

    def test_workflow_is_tag_only_read_only_and_attested(self) -> None:
        workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("ref: ${{ github.ref }}", workflow)
        self.assertIn("fetch-tags: true", workflow)
        self.assertIn("actions/checkout@v6", workflow)
        self.assertIn("actions/setup-python@v6", workflow)
        self.assertNotIn("actions/checkout@v7", workflow)
        self.assertNotIn("actions/setup-python@v7", workflow)
        self.assertIn(
            '    env:\n      PYTHONUTF8: "1"\n'
            '      PYTHONIOENCODING: "utf-8"\n'
            "    steps:",
            workflow,
        )
        self.assertEqual(workflow.count("actions/attest@v4"), 2)
        self.assertEqual(workflow.count("actions/upload-artifact@v7"), 2)
        self.assertEqual(workflow.count("actions/download-artifact@v8"), 2)
        self.assertIn(
            '-m "not external_assets and not listening"',
            workflow,
        )
        self.assertEqual(workflow.count("tools/build_tagged_release.py"), 1)
        self.assertIn(
            "TIANLAI_RELEASE_TAG: ${{ github.ref_name }}",
            workflow,
        )
        self.assertIn("--tag $env:TIANLAI_RELEASE_TAG", workflow)
        self.assertNotIn('--tag "${{ github.ref_name }}"', workflow)
        self.assertIn("needs: build-candidate", workflow)
        self.assertIn("needs: macos-release-gate", workflow)
        self.assertIn("source-stage", workflow)
        self.assertIn("shasum -a 256 -c", workflow)
        self.assertIn("sha256sum --check", workflow)
        self.assertIn("/bin/bash -n ./bootstrap_macos.sh", workflow)
        self.assertIn("/bin/bash -n ./bootstrap_linux.sh", workflow)
        self.assertIn("天籁 Linux 标签验收", workflow)
        self.assertIn(
            'python -m zipfile -e "${{ steps.candidate.outputs.archive }}"',
            workflow,
        )
        self.assertIn('test -s "output/首次出声/参考振荡器.wav"', workflow)
        self.assertIn("tests/test_smoke_soundfont_builder.py", workflow)
        self.assertIn(
            "native_backend_loads_unicode_path_and_renders_nonzero_audio",
            workflow,
        )
        self.assertIn('TIANLAI_REQUIRE_BSDTAR: "1"', workflow)
        probe = (
            'executable="$(python -S '
            './tianlai/resource_restore.py verify-bsdtar)"'
        )
        self.assertIn(probe, workflow)
        self.assertNotIn(
            "from tianlai.resource_restore import _find_bsdtar_executable",
            workflow,
        )
        self.assertLess(
            workflow.index(probe),
            workflow.index("--portable-tests"),
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
            workflow.index("tools/build_tagged_release.py"),
            workflow.index("needs: build-candidate"),
        )
        self.assertLess(
            workflow.index("needs: macos-release-gate"),
            workflow.index("actions/attest@v4"),
        )
        self.assertLess(
            workflow.index("Run Linux gates against the exact candidate"),
            workflow.index("actions/attest@v4"),
        )
        self.assertNotIn("gh release", workflow.casefold())
        self.assertNotIn("create-release", workflow.casefold())


if __name__ == "__main__":
    unittest.main()
