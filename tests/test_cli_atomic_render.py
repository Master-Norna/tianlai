from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from tianlai.cli import main as cli_main
from tianlai.events import parse_performance_document
from tianlai.post_render_check import POST_RENDER_CHECK_NAME
from tianlai.renderer import load_json_object


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "乐器" / "测试工具" / "参考振荡器" / "乐器.json"


class AtomicCliRenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary_directory.name)
        self.events = self.base / "events.json"
        self.events.write_text(
            json.dumps(
                {
                    "sample_rate": 8_000,
                    "channels": 2,
                    "tail_seconds": 0.01,
                    "events": [
                        {
                            "time": 0.0,
                            "type": "note_on",
                            "note_id": 1,
                            "midi_note": 69,
                            "velocity": 0.5,
                        },
                        {
                            "time": 0.02,
                            "type": "note_off",
                            "note_id": 1,
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.performance = parse_performance_document(
            load_json_object(self.events)
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run(self, output: Path) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = cli_main(
                [
                    "render",
                    "--instrument",
                    str(MANIFEST),
                    "--events",
                    str(self.events),
                    "--output",
                    str(output),
                ]
            )
        return exit_code, stdout.getvalue(), stderr.getvalue()

    @staticmethod
    def _parts_for(output: Path) -> list[Path]:
        return sorted(
            output.parent.glob(f".{output.name}.*.tianlai-part")
        )

    def test_success_uses_unique_sibling_part_then_publishes_valid_wav(
        self,
    ) -> None:
        from tianlai import renderer

        output = self.base / "result.wav"
        stale = self.base / f".{output.name}.stale.tianlai-part"
        stale.write_bytes(b"orphan from a force-killed process")
        written_paths: list[Path] = []
        real_write = renderer._write_wav_pcm24_blocks

        def recording_write(path, blocks, sample_rate, **kwargs):
            written_paths.append(Path(path))
            return real_write(path, blocks, sample_rate, **kwargs)

        with patch(
            "tianlai.renderer._write_wav_pcm24_blocks",
            side_effect=recording_write,
        ):
            exit_code, stdout, stderr = self._run(output)

        self.assertEqual(exit_code, 0, stderr)
        self.assertEqual(len(written_paths), 1)
        part = written_paths[0]
        self.assertEqual(part.parent.resolve(), output.parent.resolve())
        self.assertTrue(part.name.startswith(f".{output.name}."))
        self.assertTrue(part.name.endswith(".tianlai-part"))
        self.assertNotEqual(part, stale)
        self.assertFalse(part.exists())
        self.assertEqual(stale.read_bytes(), b"orphan from a force-killed process")

        info = sf.info(str(output))
        self.assertEqual(info.format, "WAV")
        self.assertEqual(info.subtype, "PCM_24")
        self.assertEqual(info.samplerate, 8_000)
        self.assertEqual(info.channels, 2)
        self.assertEqual(info.frames, self.performance.total_samples)
        self.assertIn(str(output.resolve()), stdout)
        self.assertEqual(stderr, "")

        sidecar = json.loads(
            output.with_name(
                f"{output.name}.许可与署名.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            sidecar["audio_artifacts"][0]["path"],
            output.name,
        )
        post_render_check = Path(f"{output}.{POST_RENDER_CHECK_NAME}")
        self.assertTrue(post_render_check.is_file())
        report = json.loads(post_render_check.read_text(encoding="utf-8"))
        self.assertIsInstance(report.get("summary"), dict)
        self.assertIn(str(post_render_check.resolve()), stdout)
        self.assertIn("渲染后自检状态:", stdout)

    def test_unreadable_part_preserves_existing_target_and_is_removed(
        self,
    ) -> None:
        output = self.base / "existing.wav"
        original = b"previous trusted WAV bytes"
        output.write_bytes(original)

        def broken_write(path, _blocks, _sample_rate, **_kwargs):
            Path(path).write_bytes(b"not a readable WAV")
            return self.performance.total_samples

        with patch(
            "tianlai.renderer._write_wav_pcm24_blocks",
            side_effect=broken_write,
        ):
            exit_code, _stdout, stderr = self._run(output)

        self.assertEqual(exit_code, 2)
        self.assertIn("soundfile", stderr)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self._parts_for(output), [])

    def test_sample_rate_channel_and_frame_mismatches_fail_closed(
        self,
    ) -> None:
        expected_frames = self.performance.total_samples
        cases = (
            ("sample-rate", 44_100, 2, expected_frames),
            ("channels", 8_000, 1, expected_frames),
            ("frames", 8_000, 2, expected_frames - 1),
        )
        for name, sample_rate, channels, actual_frames in cases:
            with self.subTest(name=name):
                output = self.base / f"{name}.wav"
                original = f"old-{name}".encode()
                output.write_bytes(original)

                def mismatched_write(
                    path,
                    _blocks,
                    _sample_rate,
                    *,
                    sr=sample_rate,
                    channel_count=channels,
                    frame_count=actual_frames,
                    **_kwargs,
                ):
                    sf.write(
                        str(path),
                        np.zeros(
                            (frame_count, channel_count),
                            dtype=np.float32,
                        ),
                        sr,
                        format="WAV",
                        subtype="PCM_24",
                    )
                    return expected_frames

                with patch(
                    "tianlai.renderer._write_wav_pcm24_blocks",
                    side_effect=mismatched_write,
                ):
                    exit_code, _stdout, _stderr = self._run(output)

                self.assertEqual(exit_code, 2)
                self.assertEqual(output.read_bytes(), original)
                self.assertEqual(self._parts_for(output), [])

    def test_replace_failure_preserves_existing_target_and_cleans_part(
        self,
    ) -> None:
        from tianlai import renderer

        output = self.base / "locked.wav"
        original = b"old locked target"
        output.write_bytes(original)
        real_write = renderer._write_wav_pcm24_blocks

        with (
            patch(
                "tianlai.renderer._write_wav_pcm24_blocks",
                side_effect=real_write,
            ),
            patch(
                "tianlai.renderer.os.replace",
                side_effect=OSError("simulated locked destination"),
            ),
        ):
            exit_code, _stdout, stderr = self._run(output)

        self.assertEqual(exit_code, 2)
        self.assertIn("simulated locked destination", stderr)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self._parts_for(output), [])

    def test_base_exception_before_replace_cannot_touch_final_wav(
        self,
    ) -> None:
        output = self.base / "interrupted.wav"
        original = b"old target"
        output.write_bytes(original)

        def interrupted_write(path, _blocks, _sample_rate, **_kwargs):
            Path(path).write_bytes(b"partial")
            raise KeyboardInterrupt()

        with patch(
            "tianlai.renderer._write_wav_pcm24_blocks",
            side_effect=interrupted_write,
        ):
            with self.assertRaises(KeyboardInterrupt):
                self._run(output)

        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self._parts_for(output), [])


if __name__ == "__main__":
    unittest.main()
