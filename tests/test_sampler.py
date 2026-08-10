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


def write_mono_values(
    path: Path,
    values: list[float],
    *,
    sample_rate: int = 8000,
) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(
            b"".join(
                struct.pack("<h", round(max(-1.0, min(1.0, value)) * 32767))
                for value in values
            )
        )


def write_stereo_values(
    path: Path,
    values: list[tuple[float, float]],
    *,
    sample_rate: int = 8000,
) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(
            b"".join(
                struct.pack(
                    "<hh",
                    round(max(-1.0, min(1.0, left)) * 32767),
                    round(max(-1.0, min(1.0, right)) * 32767),
                )
                for left, right in values
            )
        )


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

    def test_bandlimited_resampler_reduces_pitch_up_alias_energy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(
                directory / "above-new-nyquist.wav",
                [math.sin(math.tau * 3000.0 * index / 8000.0) for index in range(8000)],
            )

            def render(quality: str) -> list[float]:
                instrument = SampleInstrument.from_manifest(
                    {
                        "resampling_quality": quality,
                        "regions": [
                            {
                                "sample": "above-new-nyquist.wav",
                                "root_pitch_hz": 440.0,
                            }
                        ],
                    },
                    8000,
                    base_directory=str(directory),
                )
                instrument.handle_event(
                    PerformanceEvent(
                        0,
                        0,
                        "note_on",
                        {"note_id": 1, "pitch_hz": 880.0, "velocity": 1.0},
                    ),
                    EqualTemperament(),
                )
                return [instrument.render_frame()[0] for _ in range(1800)]

            linear = render("linear")[64:]
            bandlimited = render("bandlimited")[64:]
            linear_rms = math.sqrt(sum(value * value for value in linear) / len(linear))
            bandlimited_rms = math.sqrt(
                sum(value * value for value in bandlimited) / len(bandlimited)
            )

            self.assertGreater(linear_rms, 0.6)
            self.assertLess(bandlimited_rms, linear_rms * 0.25)

    def test_bandlimited_resampler_preserves_dc_gain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(directory / "dc.wav", [0.25] * 2000)
            instrument = SampleInstrument.from_manifest(
                {
                    "resampling_quality": "bandlimited",
                    "regions": [{"sample": "dc.wav", "root_pitch_hz": 440.0}],
                },
                8000,
                base_directory=str(directory),
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "pitch_hz": 660.0, "velocity": 1.0},
                ),
                EqualTemperament(),
            )

            frames = [instrument.render_frame()[0] for _ in range(400)]
            expected = round(0.25 * 32767) / 32768.0
            for value in frames[32:]:
                self.assertAlmostEqual(value, expected, delta=1e-6)

    def test_bandlimited_resampler_preserves_pitch_up_passband(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(
                directory / "passband.wav",
                [math.sin(math.tau * 500.0 * index / 8000.0) for index in range(8000)],
            )
            instrument = SampleInstrument.from_manifest(
                {
                    "resampling_quality": "bandlimited",
                    "regions": [
                        {"sample": "passband.wav", "root_pitch_hz": 440.0}
                    ],
                },
                8000,
                base_directory=str(directory),
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "pitch_hz": 880.0, "velocity": 1.0},
                ),
                EqualTemperament(),
            )

            rendered = [instrument.render_frame()[0] for _ in range(1800)][64:]
            expected = [
                math.sin(math.tau * 1000.0 * index / 8000.0)
                for index in range(64, 1800)
            ]
            rms = math.sqrt(
                sum(value * value for value in rendered) / len(rendered)
            )
            correlation = sum(
                actual * target for actual, target in zip(rendered, expected)
            ) / math.sqrt(
                sum(actual * actual for actual in rendered)
                * sum(target * target for target in expected)
            )

            self.assertGreater(rms, 0.65)
            self.assertGreater(correlation, 0.999)

    def test_bandlimited_native_rate_is_bit_identical_to_linear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(
                directory / "native.wav",
                [0.125, -0.375, 0.625, -0.25, 0.5],
            )

            def render(quality: str) -> list[tuple[float, float]]:
                instrument = SampleInstrument.from_manifest(
                    {
                        "resampling_quality": quality,
                        "regions": [
                            {
                                "sample": "native.wav",
                                "root_pitch_hz": 440.0,
                                "loop_mode": "one_shot",
                            }
                        ],
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
                return [instrument.render_frame() for _ in range(6)]

            self.assertEqual(render("bandlimited"), render("linear"))

    def test_bandlimited_sustain_release_keeps_wrapped_filter_history(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(
                directory / "loop.wav",
                [-0.75] * 8 + [0.25] * 9 + [0.75] * 7 + [0.25] * 8,
            )

            def create() -> SampleInstrument:
                return SampleInstrument.from_manifest(
                    {
                        "resampling_quality": "bandlimited",
                        "release_seconds": 1.0,
                        "regions": [
                            {
                                "sample": "loop.wav",
                                "root_pitch_hz": 440.0,
                                "loop_start": 8,
                                "loop_end": 24,
                                "loop_mode": "loop_sustain",
                            }
                        ],
                    },
                    8000,
                    base_directory=str(directory),
                )

            sustained = create()
            released = create()
            tuning = EqualTemperament()
            note_on = PerformanceEvent(
                0,
                0,
                "note_on",
                {"note_id": 1, "pitch_hz": 242.0, "velocity": 1.0},
            )
            sustained.handle_event(note_on, tuning)
            released.handle_event(note_on, tuning)

            for _ in range(80):
                sustained.render_frame()
                released.render_frame()
                if sustained.voices[1].looped:
                    break
            else:
                self.fail("test voice did not cross its sustain loop")

            self.assertGreater(sustained.voices[1].position, 8.0)
            self.assertLess(sustained.voices[1].position, 8.55)
            self.assertEqual(
                released.voices[1].position,
                sustained.voices[1].position,
            )
            released.handle_event(
                PerformanceEvent(0, 1, "note_off", {"note_id": 1}),
                tuning,
            )

            sustained_frame = sustained.render_frame()[0]
            released_frame = released.render_frame()[0]
            release_envelope = released.voices[1].envelope

            self.assertGreater(sustained_frame, 0.1)
            self.assertAlmostEqual(
                released_frame / release_envelope,
                sustained_frame,
                delta=1e-12,
            )

    def test_bandlimited_cutoff_tracks_runtime_pitch_modulation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(directory / "modulated.wav", [0.25] * 512)
            instrument = SampleInstrument.from_manifest(
                {
                    "resampling_quality": "bandlimited",
                    "regions": [
                        {
                            "sample": "modulated.wav",
                            "root_pitch_hz": 440.0,
                        }
                    ],
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
            voice = instrument.voices[1]
            self.assertIsNone(voice.resampler_cutoff_index)
            self.assertIsNone(voice.resampler_table)

            voice.increment = 1.04
            instrument.render_frame()
            expected_index = math.floor((1.0 / 1.04) * 128)
            self.assertEqual(voice.resampler_cutoff_index, expected_index)
            first_table = voice.resampler_table
            self.assertIsNotNone(first_table)

            voice.increment = 1.08
            instrument.render_frame()
            self.assertEqual(
                voice.resampler_cutoff_index,
                math.floor((1.0 / 1.08) * 128),
            )
            self.assertIsNot(voice.resampler_table, first_table)

            voice.increment = 1.0
            instrument.render_frame()
            self.assertEqual(voice.resampler_cutoff_index, 128)
            self.assertIsNotNone(voice.resampler_table)

            voice.position = 4.0
            instrument.render_frame()
            self.assertIsNone(voice.resampler_cutoff_index)
            self.assertIsNone(voice.resampler_table)

    def test_bandlimited_rejects_increment_beyond_supported_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(directory / "extreme.wav", [0.25] * 512)
            instrument = SampleInstrument.from_manifest(
                {
                    "resampling_quality": "bandlimited",
                    "regions": [
                        {"sample": "extreme.wav", "root_pitch_hz": 1.0}
                    ],
                },
                8000,
                base_directory=str(directory),
            )
            with self.assertRaisesRegex(ValueError, "supported maximum"):
                instrument.handle_event(
                    PerformanceEvent(
                        0,
                        0,
                        "note_on",
                        {"note_id": 1, "pitch_hz": 129.0, "velocity": 1.0},
                    ),
                    EqualTemperament(),
                )

    def test_stereo_sfz_pan_is_balance_without_three_db_boost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_stereo_values(
                directory / "stereo.wav",
                [(0.25, 0.5)] * 8,
            )

            def first_frame(pan: float) -> tuple[float, float]:
                instrument = SampleInstrument.from_manifest(
                    {
                        "regions": [
                            {
                                "sample": "stereo.wav",
                                "root_pitch_hz": 440.0,
                                "pan": pan,
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
                return instrument.render_frame()

            centre = first_frame(0.0)
            hard_left = first_frame(-1.0)
            hard_right = first_frame(1.0)

            self.assertAlmostEqual(centre[0], 0.25, delta=1e-4)
            self.assertAlmostEqual(centre[1], 0.5, delta=1e-4)
            self.assertAlmostEqual(hard_left[0], centre[0], delta=1e-9)
            self.assertEqual(hard_left[1], 0.0)
            self.assertEqual(hard_right[0], 0.0)
            self.assertAlmostEqual(hard_right[1], centre[1], delta=1e-9)

    def test_resampling_quality_is_validated_and_bound_to_runtime_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            write_mono_values(directory / "sample.wav", [0.25] * 8)
            with self.assertRaisesRegex(ValueError, "resampling_quality"):
                SampleInstrument.from_manifest(
                    {
                        "resampling_quality": "cubic",
                        "regions": [
                            {"sample": "sample.wav", "root_pitch_hz": 440.0}
                        ],
                    },
                    8000,
                    base_directory=str(directory),
                )

            contracts = []
            for quality in ("linear", "bandlimited"):
                instrument = SampleInstrument.from_manifest(
                    {
                        "resampling_quality": quality,
                        "regions": [
                            {"sample": "sample.wav", "root_pitch_hz": 440.0}
                        ],
                    },
                    8000,
                    base_directory=str(directory),
                )
                contracts.append(
                    instrument.runtime_variant_contract()[
                        "expected_component_sha256s"
                    ][0]
                )
            self.assertNotEqual(contracts[0], contracts[1])

    def test_bandlimited_resampler_loads_unicode_and_space_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "采样 Space ü"
            directory.mkdir()
            write_mono_values(directory / "萨克斯 音.wav", [0.25] * 64)
            instrument = SampleInstrument.from_manifest(
                {
                    "resampling_quality": "bandlimited",
                    "regions": [
                        {"sample": "萨克斯 音.wav", "root_pitch_hz": 440.0}
                    ],
                },
                48_000,
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

            left, right = instrument.render_frame()
            self.assertGreater(left, 0.0)
            self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
