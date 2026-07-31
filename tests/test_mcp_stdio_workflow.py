from __future__ import annotations

import asyncio
from datetime import timedelta
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
_HAS_MCP = importlib.util.find_spec("mcp") is not None

EXPECTED_TOOLS = {
    "list_instruments",
    "score_and_roster_format",
    "import_midi",
    "import_musicxml",
    "import_score_project",
    "confirm_roster",
    "upgrade_score",
    "get_score_slice",
    "patch_score",
    "compare_score_versions",
    "validate_project",
    "locate",
    "locate_rendered_candidate",
    "compare_rendered_candidates",
    "render",
}

MINIMAL_MUSICXML = """\
<?xml version="1.0" encoding="UTF-8"?>
<score-partwise version="4.0">
  <work><work-title>MCP stdio portable loop</work-title></work>
  <part-list>
    <score-part id="P1"><part-name>Bells</part-name></score-part>
  </part-list>
  <part id="P1">
    <measure number="1">
      <attributes>
        <divisions>1</divisions>
        <key><fifths>0</fifths></key>
        <time><beats>4</beats><beat-type>4</beat-type></time>
        <clef><sign>G</sign><line>2</line></clef>
      </attributes>
      <direction placement="above">
        <direction-type>
          <metronome>
            <beat-unit>quarter</beat-unit>
            <per-minute>240</per-minute>
          </metronome>
        </direction-type>
        <sound tempo="240"/>
      </direction>
      <note>
        <pitch><step>C</step><octave>4</octave></pitch>
        <duration>1</duration><voice>1</voice><type>quarter</type>
      </note>
      <note>
        <pitch><step>D</step><octave>4</octave></pitch>
        <duration>1</duration><voice>1</voice><type>quarter</type>
      </note>
      <note>
        <pitch><step>E</step><octave>4</octave></pitch>
        <duration>1</duration><voice>1</voice><type>quarter</type>
      </note>
      <note>
        <pitch><step>G</step><octave>4</octave></pitch>
        <duration>1</duration><voice>1</voice><type>quarter</type>
      </note>
    </measure>
  </part>
</score-partwise>
"""


@unittest.skipUnless(_HAS_MCP, "未安装 mcp 可选组件，跳过真实 stdio 闭环")
class McpStdioWorkflowTests(unittest.TestCase):
    def test_real_sdk_client_completes_two_candidate_edit_loop(self) -> None:
        self.assertEqual(importlib.metadata.version("mcp"), "1.28.1")
        asyncio.run(self._run_workflow())

    async def _run_workflow(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(
            prefix="天籁 MCP stdio "
        ) as temporary:
            sandbox = Path(temporary)
            runtime = sandbox / "运行目录"
            input_root = sandbox / "输入"
            output_root = sandbox / "输出"
            resource_root = sandbox / "空音源"
            for directory in (
                runtime,
                input_root,
                output_root,
                resource_root,
            ):
                directory.mkdir(parents=True)
            source = input_root / "最小编钟.musicxml"
            source.write_text(MINIMAL_MUSICXML, encoding="utf-8")
            stderr_path = sandbox / "mcp-stderr.log"

            server = StdioServerParameters(
                command=sys.executable,
                args=["-m", "tianlai.mcp_server"],
                cwd=runtime,
                env={
                    "TIANLAI_HOME": str(ROOT),
                    "TIANLAI_OUTPUT_DIR": str(output_root),
                    "TIANLAI_RESOURCE_DIR": str(resource_root),
                    "TIANLAI_INPUT_ROOTS": str(input_root),
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                },
                encoding="utf-8",
                encoding_error_handler="strict",
            )

            with stderr_path.open(
                "w+",
                encoding="utf-8",
            ) as stderr:
                async with stdio_client(
                    server,
                    errlog=stderr,
                ) as (read_stream, write_stream):
                    async with ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=120),
                    ) as session:
                        initialized = await session.initialize()
                        self.assertEqual(
                            initialized.serverInfo.name,
                            "tianlai",
                        )
                        listed = await session.list_tools()
                        self.assertIsNone(listed.nextCursor)
                        self.assertEqual(len(listed.tools), 15)
                        self.assertEqual(
                            {tool.name for tool in listed.tools},
                            EXPECTED_TOOLS,
                        )

                        imported = await self._call(
                            session,
                            "import_score_project",
                            {
                                "source_path": str(source),
                                "trusted_only": False,
                            },
                        )
                        self.assertTrue(imported["ok"], imported)
                        self.assertFalse(imported["audio_rendered"])
                        bundle = imported["bundle"]
                        score = bundle["score"]
                        draft = bundle["roster_draft"]
                        self.assertEqual(len(score["parts"]), 1)
                        part_id = score["parts"][0]["id"]

                        confirmed = await self._call(
                            session,
                            "confirm_roster",
                            {
                                "score": score,
                                "roster_draft": draft,
                                "assignments": [
                                    {
                                        "part": part_id,
                                        "instrument": "世界乐器/编钟",
                                    }
                                ],
                                "trusted_only": False,
                                "name": "MCP stdio 编钟",
                            },
                        )
                        self.assertTrue(confirmed["ok"], confirmed)
                        roster = confirmed["roster"]
                        self.assertEqual(
                            roster["assignments"][0]["instrument"],
                            "世界乐器/编钟",
                        )

                        first_validation = await self._call(
                            session,
                            "validate_project",
                            {
                                "score": score,
                                "roster": roster,
                                "trusted_only": False,
                                "hall": False,
                                "write_stems": False,
                                "use_stem_cache": False,
                            },
                        )
                        self.assertTrue(
                            first_validation["ok"],
                            first_validation,
                        )
                        first_handoff = first_validation[
                            "render_handoff"
                        ]
                        first = await self._call(
                            session,
                            "render",
                            {
                                "score": score,
                                "roster": roster,
                                "title": "MCP stdio portable",
                                "output_id": "first",
                                "trusted_only": False,
                                **first_handoff,
                            },
                        )
                        self.assertTrue(first["ok"], first)
                        self.assertEqual(
                            first["render_profile_sha256"],
                            first_handoff[
                                "expected_render_profile_sha256"
                            ],
                        )
                        self.assertIsNone(first["parent_candidate_id"])
                        self.assertIsNone(first["stems_dir"])
                        self.assertIsNone(first["hall"])
                        first_directory = Path(
                            first["candidate_directory"]
                        ).resolve()
                        self._assert_inside(
                            first_directory,
                            output_root / "mcp",
                        )
                        self.assertTrue(Path(first["mix_wav"]).is_file())

                        located = await self._call(
                            session,
                            "locate_rendered_candidate",
                            {
                                "candidate_directory": str(
                                    first_directory
                                ),
                                "at_seconds": 0.1,
                            },
                        )
                        self.assertTrue(located["ok"], located)
                        self.assertEqual(
                            located["candidate_id"],
                            first["candidate_id"],
                        )
                        located_ids = {
                            event["source_event_id"]
                            for field in (
                                "active_events",
                                "possible_release_or_space_sources",
                                "upcoming_events",
                            )
                            for event in located[field]
                        }
                        first_note = score["parts"][0]["notes"][0]
                        self.assertIn(
                            first_note["event_id"],
                            located_ids,
                        )

                        patched = await self._call(
                            session,
                            "patch_score",
                            {
                                "score": score,
                                "patch": {
                                    "kind": "tianlai.score_patch",
                                    "schema_version": 1,
                                    "base_score_sha256": bundle[
                                        "import_report"
                                    ]["score_canonical_sha256"],
                                    "operations": [
                                        {
                                            "op": "update_note",
                                            "event_id": first_note[
                                                "event_id"
                                            ],
                                            "expect": {
                                                "pitch": first_note["pitch"],
                                            },
                                            "changes": {"pitch": "F4"},
                                        }
                                    ],
                                },
                            },
                        )
                        self.assertTrue(patched["ok"], patched)
                        self.assertEqual(
                            patched["diff"]["counts"]["updated"],
                            1,
                        )

                        second_validation = await self._call(
                            session,
                            "validate_project",
                            {
                                "score": patched["score"],
                                "roster": roster,
                                "trusted_only": False,
                                "hall": False,
                                "write_stems": False,
                                "use_stem_cache": False,
                            },
                        )
                        self.assertTrue(
                            second_validation["ok"],
                            second_validation,
                        )
                        second_handoff = second_validation[
                            "render_handoff"
                        ]
                        second = await self._call(
                            session,
                            "render",
                            {
                                "score": patched["score"],
                                "roster": roster,
                                "title": "MCP stdio portable",
                                "output_id": "second",
                                "parent_candidate_id": first[
                                    "candidate_id"
                                ],
                                "trusted_only": False,
                                **second_handoff,
                            },
                        )
                        self.assertTrue(second["ok"], second)
                        self.assertEqual(
                            second["render_profile_sha256"],
                            second_handoff[
                                "expected_render_profile_sha256"
                            ],
                        )
                        self.assertEqual(
                            second["parent_candidate_id"],
                            first["candidate_id"],
                        )
                        second_directory = Path(
                            second["candidate_directory"]
                        ).resolve()
                        self._assert_inside(
                            second_directory,
                            output_root / "mcp",
                        )
                        self.assertTrue(Path(second["mix_wav"]).is_file())

                        compared = await self._call(
                            session,
                            "compare_rendered_candidates",
                            {
                                "before_candidate_directory": str(
                                    first_directory
                                ),
                                "after_candidate_directory": str(
                                    second_directory
                                ),
                            },
                        )
                        self.assertTrue(compared["ok"], compared)
                        self.assertTrue(compared["parent_relationship"])
                        self.assertEqual(
                            compared["score"]["counts"]["updated"],
                            1,
                        )
                        self.assertNotEqual(
                            compared["mix_sha256"]["before"],
                            compared["mix_sha256"]["after"],
                        )

            self.assertTrue((output_root / "mcp").is_dir())
            self.assertFalse(any(resource_root.iterdir()))

    async def _call(
        self,
        session,
        name: str,
        arguments: dict,
    ) -> dict:
        result = await session.call_tool(name, arguments)
        self.assertFalse(
            result.isError,
            f"{name} transport error: {result.content}",
        )
        self.assertIsNone(result.structuredContent)
        self.assertEqual(len(result.content), 1)
        block = result.content[0]
        self.assertEqual(block.type, "text")
        document = json.loads(block.text)
        self.assertIsInstance(document, dict)
        return document

    def _assert_inside(self, path: Path, parent: Path) -> None:
        path.relative_to(parent.resolve())
        self.assertNotEqual(path, parent.resolve())


if __name__ == "__main__":
    unittest.main()
