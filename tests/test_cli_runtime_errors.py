from __future__ import annotations

from contextlib import redirect_stderr
import io
from pathlib import Path
import tempfile
import unittest

from tianlai.cli import main as cli_main


class AnalyzePitchCliErrorTests(unittest.TestCase):
    def _run(self, path: Path) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = cli_main(
                [
                    "analyze-pitch",
                    "--audio",
                    str(path),
                    "--expected-hz",
                    "440",
                ]
            )
        return status, stderr.getvalue()

    def test_missing_audio_is_a_concise_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            status, error = self._run(
                Path(temporary_directory) / "不存在.wav"
            )

        self.assertEqual(status, 2)
        self.assertIn("error: 无法读取音频文件", error)
        self.assertNotIn("Traceback", error)

    def test_corrupt_audio_is_a_concise_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "损坏.wav"
            path.write_bytes(b"not an audio container")
            status, error = self._run(path)

        self.assertEqual(status, 2)
        self.assertIn("error: 无法读取音频文件", error)
        self.assertNotIn("Traceback", error)

    def test_non_finite_expected_pitch_is_rejected_before_file_io(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = cli_main(
                [
                    "analyze-pitch",
                    "--audio",
                    "anything.wav",
                    "--expected-hz",
                    "nan",
                ]
            )

        self.assertEqual(status, 2)
        self.assertEqual(
            stderr.getvalue().strip(),
            "error: expected_hz must be finite and positive",
        )


if __name__ == "__main__":
    unittest.main()
