from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from tianlai.cli import main as cli_main


PROJECT = Path(__file__).resolve().parents[1]


class CliAvailabilityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.manifest = self.base / "instrument.json"
        self.events = self.base / "events.json"
        self.output = self.base / "blocked.wav"
        self.manifest.write_text(
            json.dumps(
                {
                    "id": "policy-test",
                    "name": "隔离测试乐器",
                    "license_status": "quarantined",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run(self, command: str) -> tuple[int, str]:
        arguments = [
            command,
            "--instrument",
            str(self.manifest),
            "--events",
            str(self.events),
        ]
        if command == "render":
            arguments.extend(("--output", str(self.output)))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = cli_main(arguments)
        return result, stderr.getvalue()

    def test_render_rejects_quarantined_manifest_before_reading_events(self) -> None:
        result, stderr = self._run("render")

        self.assertEqual(result, 2)
        self.assertIn("license_status=quarantined", stderr)
        self.assertFalse(self.output.exists())

    def test_validate_rejects_quarantined_manifest_before_reading_events(self) -> None:
        result, stderr = self._run("validate")

        self.assertEqual(result, 2)
        self.assertIn("license_status=quarantined", stderr)

    def test_render_and_validate_share_the_duration_budget(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "id": "budget-test",
                    "name": "预算测试乐器",
                    "license_status": "approved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.events.write_text(
            json.dumps(
                {
                    "sample_rate": 8_000,
                    "channels": 2,
                    "tail_seconds": 0.0,
                    "duration_seconds": 7_201.0,
                    "events": [],
                }
            ),
            encoding="utf-8",
        )

        for command in ("render", "validate"):
            with self.subTest(command=command):
                result, stderr = self._run(command)
                self.assertEqual(result, 2)
                self.assertIn("exceeds limit", stderr)
        self.assertFalse(self.output.exists())

    def test_single_render_prints_its_license_sidecars(self) -> None:
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["license_status"] = "approved"
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        result = SimpleNamespace(
            duration_seconds=1.0,
            sample_rate=48_000,
            frame_count=48_000,
            peak_active_voices=1,
            license_sidecar_path=str(self.base / "blocked.wav.许可与署名.json"),
            attribution_path=str(self.base / "blocked.wav.许可与署名.txt"),
        )
        stdout = io.StringIO()
        with (
            patch("tianlai.cli.render_to_wav_atomic", return_value=result),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = cli_main(
                [
                    "render",
                    "--instrument",
                    str(self.manifest),
                    "--events",
                    str(self.events),
                    "--output",
                    str(self.output),
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("许可清单:", stdout.getvalue())
        self.assertIn("署名说明:", stdout.getvalue())

    def test_soundfont_manifest_requires_explicit_local_compatibility_switch(
        self,
    ) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "id": "local-soundfont",
                    "name": "本机兼容测试",
                    "type": "soundfont",
                    "license_status": "approved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        result, stderr = self._run("render")

        self.assertEqual(result, 2)
        self.assertIn("local compatibility/test backend", stderr)
        self.assertFalse(self.output.exists())

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = cli_main(
                [
                    "render",
                    "--instrument",
                    str(self.manifest),
                    "--events",
                    str(self.events),
                    "--output",
                    str(self.output),
                    "--allow-local-compatibility-soundfont",
                ]
            )
        self.assertEqual(result, 2)
        self.assertIn("explicit local compatibility/test mode", stderr.getvalue())
        self.assertNotIn("disabled on the public CLI path", stderr.getvalue())

    def _discover(self, command: str, *, include_quarantined: bool) -> list[dict]:
        arguments = [
            command,
            "--root",
            str(PROJECT / "乐器"),
            "--json",
        ]
        if include_quarantined:
            arguments.append("--include-quarantined")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = cli_main(arguments)
        self.assertEqual(result, 0)
        return json.loads(stdout.getvalue())

    def test_public_catalog_lists_all_instruments_and_no_quarantine(self) -> None:
        entries = self._discover("catalog", include_quarantined=False)

        self.assertEqual(len(entries), 103)
        self.assertNotIn("quarantined", {entry["license_status"] for entry in entries})
        self.assertNotIn(None, {entry["quality_tier"] for entry in entries})
        self.assertIn("班卓琴", {entry["name"] for entry in entries})

    def test_public_capabilities_list_all_instruments_and_no_quarantine(self) -> None:
        entries = self._discover("capabilities", include_quarantined=False)

        self.assertEqual(len(entries), 103)
        self.assertNotIn("quarantined", {entry["license_status"] for entry in entries})
        self.assertNotIn(None, {entry["quality_tier"] for entry in entries})
        self.assertIn(
            "世界乐器/班卓琴",
            {entry["relative_path"] for entry in entries},
        )

    def test_explicit_audit_switch_lists_all_formal_instruments(self) -> None:
        for command in ("catalog", "capabilities"):
            with self.subTest(command=command):
                entries = self._discover(command, include_quarantined=True)
                self.assertEqual(len(entries), 103)
                self.assertEqual(
                    sum(entry["license_status"] == "quarantined" for entry in entries),
                    0,
                )
                self.assertNotIn(None, {entry["quality_tier"] for entry in entries})

    def test_public_catalog_hides_local_soundfont_compatibility_entry(self) -> None:
        root = self.base / "catalog"
        directory = root / "兼容" / "本机SoundFont"
        directory.mkdir(parents=True)
        (directory / "乐器.json").write_text(
            json.dumps(
                {
                    "name": "本机SoundFont",
                    "type": "soundfont",
                    "quality_tier": "fallback",
                    "license_status": "approved",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        def discover(*extra: str) -> list[dict]:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = cli_main(
                    ["catalog", "--root", str(root), "--json", *extra]
                )
            self.assertEqual(result, 0)
            return json.loads(stdout.getvalue())

        self.assertEqual(discover(), [])
        self.assertEqual(
            len(discover("--include-local-compatibility")),
            1,
        )


if __name__ == "__main__":
    unittest.main()
