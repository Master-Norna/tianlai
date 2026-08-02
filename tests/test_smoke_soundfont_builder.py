from __future__ import annotations

import hashlib
import importlib.util
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

import numpy as np

from tianlai.events import PerformanceEvent
from tianlai.soundfont import SoundFontInstrument, prepare_fluidsynth_runtime
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "tools" / "build_smoke_soundfont.py"
SPEC = importlib.util.spec_from_file_location("tianlai_smoke_soundfont", BUILDER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery failure
    raise RuntimeError("could not load smoke SoundFont builder")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


def _chunks(payload: bytes, *, offset: int = 0):
    while offset < len(payload):
        identifier = payload[offset : offset + 4]
        size = struct.unpack_from("<I", payload, offset + 4)[0]
        start = offset + 8
        end = start + size
        yield identifier, payload[start:end]
        offset = end + (size % 2)


class SmokeSoundFontBuilderTests(unittest.TestCase):
    def _run_native_gate(self) -> unittest.TestResult:
        case = type(self)(
            "test_native_backend_loads_unicode_path_and_renders_nonzero_audio"
        )
        result = unittest.TestResult()
        case.run(result)
        return result

    def test_bank_is_deterministic_and_has_complete_hydra_sections(self) -> None:
        first = builder.build_smoke_soundfont()
        second = builder.build_smoke_soundfont()
        self.assertEqual(first, second)
        self.assertEqual(first[:4], b"RIFF")
        self.assertEqual(struct.unpack_from("<I", first, 4)[0], len(first) - 8)
        self.assertEqual(first[8:12], b"sfbk")

        top = list(_chunks(first, offset=12))
        self.assertEqual([body[:4] for kind, body in top if kind == b"LIST"], [
            b"INFO",
            b"sdta",
            b"pdta",
        ])
        pdta = top[-1][1]
        hydra = [kind for kind, _ in _chunks(pdta, offset=4)]
        self.assertEqual(
            hydra,
            [b"phdr", b"pbag", b"pmod", b"pgen", b"inst", b"ibag", b"imod", b"igen", b"shdr"],
        )
        self.assertEqual(
            hashlib.sha256(first).hexdigest(),
            "88c036852da1cb36bc2882ec9dafdf05519e964a292119bb4a1672b6c1b38e45",
        )

    def test_publish_supports_unicode_and_space_but_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "天籁 原生后端" / "微型 正弦.sf2"
            result = builder.write_smoke_soundfont(output)
            self.assertTrue(output.is_file())
            self.assertEqual(result["bytes"], output.stat().st_size)
            self.assertEqual(result["sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            with self.assertRaises(FileExistsError):
                builder.write_smoke_soundfont(output)

    def test_native_backend_loads_unicode_path_and_renders_nonzero_audio(self) -> None:
        native_runtime_required = (
            os.environ.get("TIANLAI_REQUIRE_NATIVE_FLUIDSYNTH") == "1"
        )
        if importlib.util.find_spec("fluidsynth") is None:
            if native_runtime_required:
                self.fail("pyfluidsynth is required by this platform gate")
            self.skipTest("pyfluidsynth is not installed")
        if prepare_fluidsynth_runtime(ROOT) is None:
            if native_runtime_required:
                self.fail("a native FluidSynth runtime is required by this platform gate")
            self.skipTest("a FluidSynth native runtime is not installed")

        with tempfile.TemporaryDirectory(prefix="天籁 原生 SoundFont ") as temporary:
            output = Path(temporary) / "含 空格" / "微型 正弦.sf2"
            builder.write_smoke_soundfont(output)
            instrument = SoundFontInstrument(
                48_000,
                {
                    "name": "first-party native smoke",
                    "type": "soundfont",
                    "soundfont": str(output),
                    "bank": 0,
                    "program": 0,
                    "channel_count": 1,
                    "reverb": False,
                    "chorus": False,
                },
                temporary,
            )
            try:
                instrument.handle_event(
                    PerformanceEvent(
                        0,
                        1,
                        "note_on",
                        {"note_id": 1, "midi_note": 69.0, "velocity": 0.8},
                    ),
                    EqualTemperament(),
                )
                audio = np.asarray(
                    [instrument.render_frame() for _ in range(4_096)],
                    dtype=np.float64,
                )
            finally:
                instrument.close()
            self.assertEqual(audio.shape, (4_096, 2))
            self.assertTrue(np.isfinite(audio).all())
            self.assertGreater(float(np.max(np.abs(audio))), 1e-6)

    def test_strict_native_gate_fails_instead_of_skipping_missing_binding(
        self,
    ) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"TIANLAI_REQUIRE_NATIVE_FLUIDSYNTH": "1"},
            ),
            mock.patch.object(importlib.util, "find_spec", return_value=None),
        ):
            result = self._run_native_gate()

        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("pyfluidsynth is required", result.failures[0][1])

    def test_strict_native_gate_fails_instead_of_skipping_missing_runtime(
        self,
    ) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"TIANLAI_REQUIRE_NATIVE_FLUIDSYNTH": "1"},
            ),
            mock.patch.object(importlib.util, "find_spec", return_value=object()),
            mock.patch(
                f"{__name__}.prepare_fluidsynth_runtime",
                return_value=None,
            ),
        ):
            result = self._run_native_gate()

        self.assertEqual(result.skipped, [])
        self.assertEqual(len(result.failures), 1)
        self.assertIn("native FluidSynth runtime is required", result.failures[0][1])


if __name__ == "__main__":
    unittest.main()
