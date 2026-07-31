from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tianlai.path_policy import (
    INPUT_ROOTS_ENV,
    InputPathPolicy,
    InputPathPolicyError,
    discover_mcp_input_policy,
)
from tianlai.runtime_layout import RuntimeLayout


def _layout(home: Path, *, output: Path | None = None) -> RuntimeLayout:
    return RuntimeLayout(
        home=home,
        catalog=home / "乐器",
        allowlist=home / "可信乐器.json",
        schemas=home / "schemas",
        resources=home / "音源",
        output=output or home / "output",
        source="test",
        catalog_ready=False,
    )


def _make_directory_link(link: Path, target: Path) -> None:
    symlink_error: OSError | None = None
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except OSError as exc:
        symlink_error = exc
        if os.name != "nt":
            raise
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        # ``cmd.exe`` writes through the active Windows OEM code page.  Keep
        # the unused output as bytes so Python UTF-8 mode cannot make the
        # subprocess reader apply an incompatible text decoder.
        text=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        assert symlink_error is not None
        raise symlink_error


class InputPathPolicyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows junction fallback only")
    def test_junction_fallback_keeps_cmd_output_binary(self) -> None:
        link = Path("linked")
        target = Path("target")
        symlink_error = OSError("symlinks unavailable")
        completed = subprocess.CompletedProcess([], 0, b"\xb4", b"\xb4")

        with (
            patch.object(Path, "symlink_to", side_effect=symlink_error),
            patch(
                f"{__name__}.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            _make_directory_link(link, target)

        self.assertIs(run.call_args.kwargs["text"], False)

    def test_relative_input_is_resolved_from_runtime_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            score_dir = home / "乐谱" / "MIDI"
            score_dir.mkdir(parents=True)
            score = score_dir / "example.mid"
            score.write_bytes(b"MThd")
            elsewhere = home / "elsewhere"
            elsewhere.mkdir()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("pathlib.Path.cwd", return_value=elsewhere),
            ):
                policy = discover_mcp_input_policy(layout=_layout(home))
                resolved = policy.resolve_file("乐谱/MIDI/example.mid")

        self.assertEqual(resolved, score.resolve())

    def test_dot_dot_escape_is_rejected_after_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            home.mkdir()
            outside = base / "outside.xml"
            outside.write_text("<score-partwise/>", encoding="utf-8")
            policy = InputPathPolicy.from_roots([home])

            with self.assertRaises(InputPathPolicyError) as caught:
                policy.resolve_file("../outside.xml")

        self.assertEqual(
            caught.exception.code,
            "input_path.outside_allowed_roots",
        )
        issue = caught.exception.to_issue()
        self.assertEqual(issue["severity"], "error")
        self.assertEqual(issue["stage"], "input_policy")
        self.assertIn("allowed_roots", issue)
        self.assertIn(INPUT_ROOTS_ENV, issue["message"])

    def test_absolute_outside_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            home.mkdir()
            outside = base / "secret.mid"
            outside.write_bytes(b"secret")
            policy = InputPathPolicy.from_roots([home])

            with self.assertRaises(InputPathPolicyError) as caught:
                policy.resolve_file(outside)

        self.assertEqual(
            caught.exception.code,
            "input_path.outside_allowed_roots",
        )
        result = caught.exception.to_result(stage="source_import")
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["issues"][0]["code"],
            "input_path.outside_allowed_roots",
        )
        self.assertEqual(result["issues"][0]["stage"], "source_import")

    def test_existing_directory_is_not_an_input_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            folder = home / "乐谱"
            folder.mkdir()
            policy = InputPathPolicy.from_roots([home])

            with self.assertRaises(InputPathPolicyError) as caught:
                policy.resolve_file(folder)

        self.assertEqual(
            caught.exception.code,
            "input_path.not_regular_file",
        )

    def test_missing_and_empty_paths_have_distinct_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = InputPathPolicy.from_roots([temporary])
            with self.assertRaises(InputPathPolicyError) as missing:
                policy.resolve_file("missing.mid")
            with self.assertRaises(InputPathPolicyError) as empty:
                policy.resolve_file("  ")

        self.assertEqual(missing.exception.code, "input_path.not_found")
        self.assertEqual(empty.exception.code, "input_path.invalid")

    def test_environment_roots_extend_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            home.mkdir()
            first = base / "imports-one"
            second = base / "imports-two"
            first.mkdir()
            second.mkdir()
            source = second / "score.mxl"
            source.write_bytes(b"PK")

            with patch.dict(
                os.environ,
                {
                    INPUT_ROOTS_ENV: os.pathsep.join(
                        (str(first), str(second))
                    )
                },
                clear=True,
            ):
                policy = discover_mcp_input_policy(layout=_layout(home))
                resolved = policy.resolve_file(source)

        self.assertEqual(resolved, source.resolve())
        self.assertIn(home.resolve(), policy.allowed_roots)
        self.assertIn(first.resolve(), policy.allowed_roots)
        self.assertIn(second.resolve(), policy.allowed_roots)

    def test_invalid_environment_root_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            missing = home / "missing"
            with patch.dict(
                os.environ,
                {INPUT_ROOTS_ENV: str(missing)},
                clear=True,
            ):
                with self.assertRaises(InputPathPolicyError) as caught:
                    discover_mcp_input_policy(layout=_layout(home))

        self.assertEqual(caught.exception.code, "input_roots.invalid")
        self.assertIn(INPUT_ROOTS_ENV, caught.exception.message)

    def test_empty_environment_value_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(
                os.environ,
                {INPUT_ROOTS_ENV: f" {os.pathsep} "},
                clear=True,
            ):
                with self.assertRaises(InputPathPolicyError) as caught:
                    discover_mcp_input_policy(layout=_layout(home))

        self.assertEqual(
            caught.exception.code,
            "input_roots.empty_environment",
        )

    def test_symlink_escape_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            home.mkdir()
            outside = base / "outside"
            outside.mkdir()
            secret = outside / "secret.musicxml"
            secret.write_text("<score-partwise/>", encoding="utf-8")
            link = home / "linked"
            try:
                _make_directory_link(link, outside)
            except OSError as exc:
                self.skipTest(f"filesystem links are unavailable: {exc}")

            policy = InputPathPolicy.from_roots([home])
            with self.assertRaises(InputPathPolicyError) as caught:
                policy.resolve_file(link / secret.name)

        self.assertEqual(
            caught.exception.code,
            "input_path.outside_allowed_roots",
        )
        self.assertEqual(
            Path(caught.exception.resolved_path),
            secret.resolve(),
        )

    def test_symlink_that_remains_inside_root_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            actual = home / "scores" / "real.mid"
            actual.parent.mkdir()
            actual.write_bytes(b"MThd")
            link = home / "linked-scores"
            try:
                _make_directory_link(link, actual.parent)
            except OSError as exc:
                self.skipTest(f"filesystem links are unavailable: {exc}")

            policy = InputPathPolicy.from_roots([home])
            self.assertEqual(
                policy.resolve_file(link / actual.name),
                actual.resolve(),
            )

    def test_separately_configured_existing_output_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "project"
            home.mkdir()
            output = base / "renders"
            output.mkdir()
            receipt = output / "渲染回执.json"
            receipt.write_text("{}", encoding="utf-8")

            with patch.dict(os.environ, {}, clear=True):
                policy = discover_mcp_input_policy(
                    layout=_layout(home, output=output)
                )

            self.assertEqual(policy.resolve_file(receipt), receipt.resolve())

    def test_policy_description_is_stable_and_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            policy = InputPathPolicy.from_roots([temporary])
            document = policy.to_dict()

        self.assertEqual(document["kind"], "tianlai.input_path_policy")
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(
            document["extension_environment_variable"],
            INPUT_ROOTS_ENV,
        )


if __name__ == "__main__":
    unittest.main()
