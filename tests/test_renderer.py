import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import wave

from tianlai.renderer import render_to_wav


ROOT = Path(__file__).resolve().parents[1]


class RendererTests(unittest.TestCase):
    def test_render_is_deterministic_and_pcm24(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            first = Path(temporary_directory) / "first.wav"
            second = Path(temporary_directory) / "second.wav"
            result = render_to_wav(
                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                ROOT / "examples/c_major.events.json",
                first,
            )
            render_to_wav(
                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                ROOT / "examples/c_major.events.json",
                second,
            )
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            self.assertEqual(result.sample_rate, 48000)
            self.assertEqual(result.peak_active_voices, 4)
            self.assertEqual(
                result.license_sidecar_path,
                str(first.with_name(f"{first.name}.许可与署名.json")),
            )
            self.assertEqual(
                result.attribution_path,
                str(first.with_name(f"{first.name}.许可与署名.txt")),
            )
            sidecar = json.loads(
                Path(result.license_sidecar_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                sidecar["scope"]["rule"],
                "actual_render_inputs_only",
            )
            self.assertEqual(sidecar["scope"]["instrument_count"], 1)
            self.assertEqual(
                sidecar["instruments"][0]["manifest"]["path"],
                "测试工具/参考振荡器/乐器.json",
            )
            self.assertIsNone(sidecar["instruments"][0]["creator"])
            self.assertEqual(
                sidecar["audio_artifacts"][0]["sha256"],
                hashlib.sha256(first.read_bytes()).hexdigest(),
            )
            with wave.open(str(first), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 2)
                self.assertEqual(audio.getsampwidth(), 3)
                self.assertEqual(audio.getframerate(), 48000)
                self.assertEqual(audio.getnframes(), result.frame_count)


if __name__ == "__main__":
    unittest.main()
