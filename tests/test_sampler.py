import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from tianlai.events import PerformanceEvent
from tianlai.renderer import render_to_wav
from tianlai.sampler import SampleInstrument
from tianlai.tuning import EqualTemperament


def write_sine(path: Path, frequency: float, sample_rate: int = 48000) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        frames = bytearray()
        for index in range(sample_rate):
            value = round(math.sin(math.tau * frequency * index / sample_rate) * 16000)
            frames.extend(struct.pack("<h", value))
        output.writeframes(frames)


class SampleInstrumentTests(unittest.TestCase):
    def test_sample_manifest_renders_at_requested_pitch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_sine(directory / "a4.wav", 440.0)
            (directory / "instrument.json").write_text(
                json.dumps(
                    {
                        "name": "test sample",
                        "type": "sample",
                        "regions": [{"sample": "a4.wav", "root_pitch_hz": 440.0}],
                        "release_seconds": 0.05,
                    }
                ),
                encoding="utf-8",
            )
            (directory / "events.json").write_text(
                json.dumps(
                    {
                        "sample_rate": 48000,
                        "duration_seconds": 0.2,
                        "events": [
                            {"time": 0, "type": "note_on", "note_id": 1, "pitch_hz": 880.0, "velocity": 1.0},
                            {"time": 0.15, "type": "note_off", "note_id": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = render_to_wav(
                directory / "instrument.json", directory / "events.json", directory / "result.wav"
            )
            self.assertEqual(result.frame_count, 9600)
            self.assertEqual(result.peak_active_voices, 1)
            self.assertGreater((directory / "result.wav").stat().st_size, 1000)
            with wave.open(str(directory / "result.wav"), "rb") as rendered:
                raw = rendered.readframes(4800)

            def decode_left(frame_index: int) -> int:
                offset = frame_index * 6
                value = raw[offset] | (raw[offset + 1] << 8) | (raw[offset + 2] << 16)
                return value - (1 << 24) if value & 0x800000 else value

            positive_crossings = 0
            previous = decode_left(0)
            for frame_index in range(1, 4800):
                current = decode_left(frame_index)
                if previous <= 0 < current:
                    positive_crossings += 1
                previous = current
            measured_hz = positive_crossings / 0.1
            self.assertAlmostEqual(measured_hz, 880.0, delta=10.0)

    def test_explicit_loop_keeps_a_sustained_voice_alive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_sine(directory / "a4.wav", 440.0)
            (directory / "instrument.json").write_text(
                json.dumps(
                    {
                        "name": "loop test",
                        "type": "sample",
                        "regions": [
                            {
                                "sample": "a4.wav",
                                "root_pitch_hz": 440.0,
                                "loop_start": 12000,
                                "loop_end": 36000,
                            }
                        ],
                        "release_seconds": 0.02,
                    }
                ),
                encoding="utf-8",
            )
            (directory / "events.json").write_text(
                json.dumps(
                    {
                        "sample_rate": 48000,
                        "duration_seconds": 1.3,
                        "events": [
                            {"time": 0, "type": "note_on", "note_id": 1, "pitch_hz": 440, "velocity": 1},
                            {"time": 1.2, "type": "note_off", "note_id": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = directory / "result.wav"
            render_to_wav(directory / "instrument.json", directory / "events.json", output)
            with wave.open(str(output), "rb") as rendered:
                rendered.setpos(52800)
                raw = rendered.readframes(2400)
            self.assertTrue(any(byte != 0 for byte in raw))

    def test_region_delay_defers_sample_playback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_sine(directory / "a4.wav", 440.0)
            (directory / "instrument.json").write_text(
                json.dumps(
                    {
                        "name": "delay test",
                        "type": "sample",
                        "regions": [
                            {
                                "sample": "a4.wav",
                                "root_pitch_hz": 440.0,
                                "delay_seconds": 0.05,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (directory / "events.json").write_text(
                json.dumps(
                    {
                        "sample_rate": 48000,
                        "duration_seconds": 0.12,
                        "events": [
                            {"time": 0, "type": "note_on", "note_id": 1, "pitch_hz": 440, "velocity": 1},
                            {"time": 0.1, "type": "note_off", "note_id": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = directory / "result.wav"
            render_to_wav(directory / "instrument.json", directory / "events.json", output)
            with wave.open(str(output), "rb") as rendered:
                silent = rendered.readframes(2200)
                rendered.setpos(2600)
                sounding = rendered.readframes(500)
            self.assertFalse(any(silent))
            self.assertTrue(any(sounding))

    def test_loop_boundary_interpolates_back_to_loop_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            path = directory / "loop.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(
                    b"".join(
                        struct.pack("<h", value)
                        for value in (0, 10000, 20000, -20000, 0)
                    )
                )
            instrument = SampleInstrument.from_manifest(
                {
                    "regions": [
                        {
                            "sample": "loop.wav",
                            "root_pitch_hz": 440,
                            "loop_start": 1,
                            "loop_end": 3,
                        }
                    ]
                },
                8000,
                base_directory=str(directory),
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "pitch_hz": 220, "velocity": 1},
                ),
                EqualTemperament(),
            )
            frames = [instrument.render_frame()[0] for _ in range(6)]
            self.assertGreater(frames[5], 0.4)

    def test_one_shot_ignores_note_off_and_does_not_loop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_sine(directory / "a4.wav", 440.0, sample_rate=8000)
            instrument = SampleInstrument.from_manifest(
                {
                    "regions": [
                        {
                            "sample": "a4.wav",
                            "root_pitch_hz": 440.0,
                            "loop_start": 1000,
                            "loop_end": 2000,
                            "loop_mode": "one_shot",
                        }
                    ]
                },
                8000,
                base_directory=str(directory),
            )
            tuning = EqualTemperament()
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "pitch_hz": 440.0, "velocity": 1.0},
                ),
                tuning,
            )
            instrument.handle_event(
                PerformanceEvent(100, 1, "note_off", {"note_id": 1}), tuning
            )
            self.assertFalse(instrument.voices[1].released)
            for _ in range(8001):
                instrument.render_frame()
            self.assertEqual(instrument.active_voice_count, 0)

    def test_sample_end_stops_at_the_exclusive_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            path = directory / "trim.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(
                    b"".join(
                        struct.pack("<h", value)
                        for value in (4000, 8000, 12000, 16000, 20000)
                    )
                )
            instrument = SampleInstrument.from_manifest(
                {
                    "regions": [
                        {
                            "sample": "trim.wav",
                            "root_pitch_hz": 440.0,
                            "offset_frames": 1,
                            "sample_end": 4,
                            "loop_mode": "one_shot",
                        }
                    ]
                },
                8000,
                base_directory=str(directory),
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "pitch_hz": 440.0, "velocity": 1.0},
                ),
                EqualTemperament(),
            )

            frames = [instrument.render_frame()[0] for _ in range(4)]

            self.assertTrue(all(value > 0.0 for value in frames[:3]))
            self.assertEqual(frames[3], 0.0)
            self.assertEqual(instrument.active_voice_count, 0)

    def test_sample_end_must_follow_offset_and_stay_inside_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_sine(directory / "a4.wav", 440.0, sample_rate=8000)
            for sample_end in (100, 8001):
                with self.subTest(sample_end=sample_end):
                    with self.assertRaisesRegex(ValueError, "invalid sample end"):
                        SampleInstrument.from_manifest(
                            {
                                "regions": [
                                    {
                                        "sample": "a4.wav",
                                        "root_pitch_hz": 440.0,
                                        "offset_frames": 100,
                                        "sample_end": sample_end,
                                    }
                                ]
                            },
                            8000,
                            base_directory=str(directory),
                        )


if __name__ == "__main__":
    unittest.main()
