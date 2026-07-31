from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from tianlai.cli import main as cli_main


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


class PublicWorkflowSmokeTests(unittest.TestCase):
    def _run(self, arguments: list[str]) -> str:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli_main(arguments)
        self.assertEqual(
            status,
            0,
            f"stdout:\n{stdout.getvalue()}\nstderr:\n{stderr.getvalue()}",
        )
        return stdout.getvalue()

    def test_musicxml_to_two_receipt_bound_candidates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="天籁 闭环 ") as temporary:
            work = Path(temporary)
            imported = work / "导入-01"
            self._run(
                [
                    "project-import",
                    "--input",
                    str(EXAMPLES / "最小闭环.musicxml"),
                    "--output",
                    str(imported),
                    "--root",
                    str(ROOT / "乐器"),
                ]
            )
            score = imported / "最小闭环.score.json"
            report = imported / "最小闭环.import-report.json"
            draft = imported / "最小闭环.roster-draft.json"
            self.assertTrue(score.is_file())
            self.assertTrue(report.is_file())
            self.assertTrue(draft.is_file())
            report_document = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(
                report_document["semantic_loss_warning_count"],
                0,
            )

            roster = work / "最小闭环.roster.json"
            self._run(
                [
                    "roster-promote",
                    "--score",
                    str(score),
                    "--draft",
                    str(draft),
                    "--assign",
                    "P1=测试工具/参考振荡器",
                    "--output",
                    str(roster),
                    "--open-palette",
                    "--root",
                    str(ROOT / "乐器"),
                ]
            )

            candidates = work / "候选"
            first = json.loads(
                self._run(
                    [
                        "project-render",
                        "--score",
                        str(score),
                        "--roster",
                        str(roster),
                        "--render-profile",
                        str(EXAMPLES / "最小闭环.render-profile.json"),
                        "--title",
                        "最小闭环测试",
                        "--output-root",
                        str(candidates),
                        "--output-id",
                        "first",
                        "--root",
                        str(ROOT / "乐器"),
                    ]
                )
            )
            first_directory = Path(first["candidate_directory"])
            self.assertTrue((first_directory / "候选.json").is_file())
            self.assertTrue(Path(first["mix_wav"]).is_file())

            locate = json.loads(
                self._run(
                    [
                        "candidate-locate",
                        "--candidate",
                        str(first_directory),
                        "--at",
                        "0.25",
                    ]
                )
            )
            self.assertEqual(
                locate["active_events"][0]["source_event_id"],
                "event-000001",
            )

            patched_score = work / "最小闭环.rev02.score.json"
            self._run(
                [
                    "score-patch",
                    "--score",
                    str(score),
                    "--patch",
                    str(EXAMPLES / "最小闭环.patch.json"),
                    "--output",
                    str(patched_score),
                ]
            )
            second = json.loads(
                self._run(
                    [
                        "project-render",
                        "--score",
                        str(patched_score),
                        "--roster",
                        str(roster),
                        "--render-profile",
                        str(EXAMPLES / "最小闭环.render-profile.json"),
                        "--title",
                        "最小闭环测试",
                        "--output-root",
                        str(candidates),
                        "--output-id",
                        "second",
                        "--parent-candidate",
                        first["candidate_id"],
                        "--root",
                        str(ROOT / "乐器"),
                    ]
                )
            )
            comparison = json.loads(
                self._run(
                    [
                        "candidate-compare",
                        "--before",
                        str(first_directory),
                        "--after",
                        second["candidate_directory"],
                    ]
                )
            )
            self.assertTrue(comparison["parent_relationship"])
            self.assertEqual(comparison["score"]["counts"]["updated"], 1)
            self.assertNotEqual(
                comparison["mix_sha256"]["before"],
                comparison["mix_sha256"]["after"],
            )


if __name__ == "__main__":
    unittest.main()
