from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tianlai.cli import main as cli_main


REPORT = {
    "kind": "tianlai.candidate_verify_result",
    "schema_version": 1,
    "integrity_verified": True,
    "candidate_id": "candidate-01",
}


class CandidateIntegrityCliTests(unittest.TestCase):
    def _run(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli_main(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    @patch("tianlai.candidate_integrity.verify_candidate_integrity")
    @patch("tianlai.candidate_integrity.candidate_directory")
    def test_stdout_mode_prints_the_integrity_report(
        self,
        candidate_directory_mock,
        verify_mock,
    ) -> None:
        directory = Path("saved-candidate")
        candidate_directory_mock.return_value = directory
        verify_mock.return_value = REPORT

        status, stdout, stderr = self._run(
            ["candidate-verify", "--candidate", "saved-candidate/候选.json"]
        )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout), REPORT)
        self.assertEqual(stderr, "")
        candidate_directory_mock.assert_called_once_with(
            "saved-candidate/候选.json"
        )
        verify_mock.assert_called_once_with(directory)

    @patch("tianlai.candidate_integrity.verify_candidate_integrity")
    @patch("tianlai.candidate_integrity.candidate_directory")
    def test_output_mode_writes_the_report_atomically_outside_the_candidate(
        self,
        candidate_directory_mock,
        verify_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            directory = base / "candidate"
            directory.mkdir()
            output = base / "reports" / "candidate-01.json"
            output.parent.mkdir()
            candidate_directory_mock.return_value = directory
            verify_mock.return_value = REPORT

            status, stdout, stderr = self._run(
                [
                    "candidate-verify",
                    "--candidate",
                    str(directory),
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(status, 0)
            self.assertEqual(stdout.strip(), str(output.resolve()))
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                REPORT,
            )
            verify_mock.assert_called_once_with(directory)

    @patch("tianlai.candidate_integrity.verify_candidate_integrity")
    @patch("tianlai.candidate_integrity.candidate_directory")
    def test_candidate_local_output_is_rejected_before_verification(
        self,
        candidate_directory_mock,
        verify_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "candidate"
            directory.mkdir()
            output = directory / "候选.json"
            candidate_directory_mock.return_value = directory

            status, stdout, stderr = self._run(
                [
                    "candidate-verify",
                    "--candidate",
                    str(directory),
                    "--output",
                    str(output),
                ]
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertIn("must be written outside", stderr)
            self.assertFalse(output.exists())
            verify_mock.assert_not_called()

    @patch("tianlai.candidate_integrity.verify_candidate_integrity")
    @patch("tianlai.candidate_integrity.candidate_directory")
    def test_output_refuses_overwrite_until_explicitly_enabled(
        self,
        candidate_directory_mock,
        verify_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            directory = base / "candidate"
            directory.mkdir()
            output = base / "integrity.json"
            output.write_text("preserve me", encoding="utf-8")
            candidate_directory_mock.return_value = directory
            verify_mock.return_value = REPORT
            arguments = [
                "candidate-verify",
                "--candidate",
                str(directory),
                "--output",
                str(output),
            ]

            status, stdout, stderr = self._run(arguments)

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertIn("--overwrite", stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), "preserve me")

            status, stdout, stderr = self._run([*arguments, "--overwrite"])

            self.assertEqual(status, 0)
            self.assertEqual(stdout.strip(), str(output.resolve()))
            self.assertEqual(stderr, "")
            self.assertEqual(
                json.loads(output.read_text(encoding="utf-8")),
                REPORT,
            )

    @patch("tianlai.candidate_integrity.verify_candidate_integrity")
    @patch("tianlai.candidate_integrity.candidate_directory")
    def test_verification_failure_preserves_an_existing_output(
        self,
        candidate_directory_mock,
        verify_mock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            directory = base / "candidate"
            directory.mkdir()
            output = base / "integrity.json"
            output.write_text("previous report", encoding="utf-8")
            candidate_directory_mock.return_value = directory
            verify_mock.side_effect = ValueError("candidate snapshot drifted")

            status, stdout, stderr = self._run(
                [
                    "candidate-verify",
                    "--candidate",
                    str(directory),
                    "--output",
                    str(output),
                    "--overwrite",
                ]
            )

            self.assertEqual(status, 2)
            self.assertEqual(stdout, "")
            self.assertIn("candidate snapshot drifted", stderr)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "previous report",
            )


if __name__ == "__main__":
    unittest.main()
