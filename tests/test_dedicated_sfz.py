import json
import math
from pathlib import Path
import struct
import tempfile
import unittest
import wave

import pytest

from tianlai.dedicated_sfz import (
    DedicatedSfzInstrument,
    dedicated_regions_to_manifest,
    parse_dedicated_sfz,
    preprocess_sfz,
)
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]


def write_constant_wav(
    path: Path,
    value: float,
    *,
    frame_count: int = 128,
    sample_rate: int = 8000,
    embedded_loop: tuple[int, int] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = round(max(-1.0, min(1.0, value)) * 32767.0)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack("<h", sample) * frame_count)
    if embedded_loop is not None:
        start, end_inclusive = embedded_loop
        payload = bytearray(60)
        struct.pack_into("<I", payload, 28, 1)
        struct.pack_into(
            "<IIIIII", payload, 36, 0, 0, start, end_inclusive, 0, 0
        )
        with path.open("ab") as output:
            output.write(b"smpl")
            output.write(struct.pack("<I", len(payload)))
            output.write(payload)
        with path.open("r+b") as output:
            output.seek(0, 2)
            riff_size = output.tell() - 8
            output.seek(4)
            output.write(struct.pack("<I", riff_size))


def event(event_type: str, sequence: int, **payload: object) -> PerformanceEvent:
    return PerformanceEvent(0, sequence, event_type, dict(payload))


class DedicatedSfzPreprocessorTests(unittest.TestCase):
    def test_chinese_space_paths_include_macros_and_full_inheritance(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "中文 音源"
            data = root / "数据 目录"
            samples = root / "采样 空间"
            data.mkdir(parents=True)
            samples.mkdir()
            write_constant_wav(samples / "轮转 一.wav", 0.1)
            write_constant_wav(samples / "轮转 二.wav", 0.2)
            (root / "主 映射.sfz").write_text(
                "#define $EXT wav\n"
                "#define $TOP 100\n"
                '#include "数据 目录/层.txt"\n',
                encoding="utf-8",
            )
            (data / "层.txt").write_text(
                "<control> default_path=采样 空间/\n"
                "<global> ampeg_release=0.4 volume=-2\n"
                "<master> pan=20 group=7\n"
                "<group> hivel=$TOP off_by=8\n"
                '#include "数据 目录/区域.txt"\n',
                encoding="utf-8",
            )
            (data / "区域.txt").write_text(
                "<region> sample=轮转 一.$EXT key=C4 seq_length=2 seq_position=1 "
                "offset=2 delay=0.01 ampeg_attack=0.02 tune=12\n"
                "<region> sample=\"轮转 二.$EXT\" key=C4 seq_length=2 seq_position=2\n",
                encoding="utf-8",
            )

            flattened = preprocess_sfz(root / "主 映射.sfz", asset_root=root)
            self.assertNotIn("#include", flattened)
            self.assertNotIn("$EXT", flattened)
            document = parse_dedicated_sfz(root / "主 映射.sfz", asset_root=root)
            self.assertEqual(len(document.source_files), 3)
            self.assertEqual(document.control["default_path"], "采样 空间/")
            self.assertEqual(len(document.regions), 2)
            first = document.regions[0].values
            self.assertEqual(first["ampeg_release"], "0.4")
            self.assertEqual(first["pan"], "20")
            self.assertEqual(first["group"], "7")
            self.assertEqual(first["off_by"], "8")
            self.assertEqual(first["hivel"], "100")
            self.assertEqual(first["sample"], "轮转 一.wav")

            regions, runtime = dedicated_regions_to_manifest(
                root / "主 映射.sfz",
                asset_root=root,
                stable_prefix="中文/主 映射.sfz",
            )
            self.assertEqual(len(regions), 2)
            self.assertEqual(
                Path(regions[0]["sample"]),
                (samples / "轮转 一.wav").resolve(),
            )
            self.assertEqual(regions[0]["round_robin_position"], 1)
            self.assertEqual(regions[0]["round_robin_length"], 2)
            self.assertEqual(regions[0]["offset_frames"], 2)
            self.assertAlmostEqual(regions[0]["delay_seconds"], 0.01)
            self.assertAlmostEqual(regions[0]["attack_seconds"], 0.02)
            self.assertAlmostEqual(regions[0]["release_seconds"], 0.4)
            self.assertAlmostEqual(regions[0]["gain_db"], -2.0)
            self.assertAlmostEqual(regions[0]["pan"], 0.2)
            self.assertAlmostEqual(regions[0]["measured_tuning_cents"], -12.0)
            self.assertEqual(runtime["中文/主 映射.sfz:0"].group, "7")
            self.assertEqual(runtime["中文/主 映射.sfz:0"].off_by, "8")

    def test_include_escape_and_cycle_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            root = parent / "root"
            root.mkdir()
            (parent / "outside.txt").write_text("<region> sample=x.wav key=60\n")
            escape = root / "escape.sfz"
            escape.write_text('#include "../outside.txt"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes asset_root"):
                preprocess_sfz(escape, asset_root=root)

            (root / "a.sfz").write_text('#include "b.txt"\n', encoding="utf-8")
            (root / "b.txt").write_text('#include "a.sfz"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cyclic SFZ include"):
                preprocess_sfz(root / "a.sfz", asset_root=root)

    def test_undefined_macro_is_an_explicit_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = root / "bad.sfz"
            path.write_text("<region> sample=x.$MISSING key=60\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "undefined SFZ macro"):
                parse_dedicated_sfz(path, asset_root=root)

    def test_inclusive_sfz_end_becomes_exclusive_sample_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_constant_wav(root / "trim.wav", 0.25, frame_count=8)
            path = root / "trim.sfz"
            path.write_text(
                "<region> sample=trim.wav key=60 offset=1 end=4\n",
                encoding="utf-8",
            )

            regions, _ = dedicated_regions_to_manifest(path, asset_root=root)

            self.assertEqual(regions[0]["offset_frames"], 1)
            self.assertEqual(regions[0]["sample_end"], 5)


class DedicatedSfzInstrumentTests(unittest.TestCase):
    def _instrument(
        self,
        directory: Path,
        sfz_text: str,
        *,
        manifest_extra: dict[str, object] | None = None,
    ) -> DedicatedSfzInstrument:
        root = directory / "专用 音源"
        root.mkdir(parents=True, exist_ok=True)
        (root / "映射 文件.sfz").write_text(sfz_text, encoding="utf-8")
        manifest: dict[str, object] = {
            "type": "dedicated_sfz",
            "asset_root": "专用 音源",
            "sfz": "映射 文件.sfz",
            "default_articulation": "default",
            "pitch_mode": "pitched",
            "note_min": 60,
            "note_max": 60,
            "control_smoothing_seconds": 0.001,
        }
        if manifest_extra:
            manifest.update(manifest_extra)
        return DedicatedSfzInstrument(8000, manifest, str(directory))

    def _selected_name(
        self, instrument: DedicatedSfzInstrument, public_id: int
    ) -> str:
        route = instrument.routes[public_id]
        voice = instrument.articulations[route.articulation].attack.voices[
            route.internal_note_id
        ]
        return voice.region.path.name

    def test_sample_gain_overrides_are_additive_and_audited_across_articulations(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "dedicated-assets"
            write_constant_wav(root / "samples" / "normal.wav", 0.1)
            write_constant_wav(root / "samples" / "accent.wav", 0.2)
            write_constant_wav(root / "samples" / "release.wav", 0.3)
            (root / "normal.sfz").write_text(
                "<region> sample=samples/normal.wav key=60 volume=-2\n",
                encoding="utf-8",
            )
            (root / "accent.sfz").write_text(
                "<region> sample=samples/accent.wav key=60 volume=-4\n"
                "<region> sample=samples/release.wav key=60 trigger=release "
                "volume=-6\n",
                encoding="utf-8",
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "dedicated-assets",
                "articulations": {
                    "normal": "normal.sfz",
                    "accent": "accent.sfz",
                },
                "default_articulation": "normal",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
                "sample_gain_db_overrides": [
                    {
                        "sample": "samples/accent.wav",
                        "gain_db": 3.0,
                    },
                    {
                        "sample": "samples/release.wav",
                        "gain_db": 2.0,
                    },
                ],
            }

            instrument = DedicatedSfzInstrument(8000, manifest, str(directory))
            normal = instrument.articulations["normal"].attack.regions[0]
            accent = instrument.articulations["accent"].attack.regions[0]
            release = instrument.articulations["accent"].release
            self.assertIsNotNone(release)
            assert release is not None
            release_region = release.regions[0]
            self.assertAlmostEqual(normal.gain, 10.0 ** (-2.0 / 20.0))
            self.assertAlmostEqual(accent.gain, 10.0 ** (-1.0 / 20.0))
            self.assertAlmostEqual(release_region.gain, 10.0 ** (-4.0 / 20.0))

            stale_manifest = {
                **manifest,
                "sample_gain_db_overrides": [
                    *manifest["sample_gain_db_overrides"],
                    {
                        "sample": "samples/not-loaded.wav",
                        "gain_db": 1.0,
                    },
                ],
            }
            with self.assertRaisesRegex(
                ValueError,
                "did not match loaded dedicated SFZ attack or release regions "
                "across all articulations",
            ):
                DedicatedSfzInstrument(8000, stale_manifest, str(directory))

    def test_sample_gain_overrides_reject_unsafe_or_ambiguous_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "dedicated-assets"
            write_constant_wav(root / "tone.wav", 0.1)
            (root / "mapped.sfz").write_text(
                "<region> sample=tone.wav key=60\n",
                encoding="utf-8",
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "dedicated-assets",
                "sfz": "mapped.sfz",
                "default_articulation": "default",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
            }
            invalid_cases = (
                (
                    "empty array",
                    [],
                    "must be a non-empty array",
                ),
                (
                    "absolute POSIX path",
                    [{"sample": "/tone.wav", "gain_db": 0.0}],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "parent traversal",
                    [{"sample": "samples/../tone.wav", "gain_db": 0.0}],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "Windows drive path",
                    [{"sample": "C:/tone.wav", "gain_db": 0.0}],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "backslash path",
                    [{"sample": "samples\\tone.wav", "gain_db": 0.0}],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "duplicate sample",
                    [
                        {"sample": "tone.wav", "gain_db": 1.0},
                        {"sample": "tone.wav", "gain_db": 2.0},
                    ],
                    "duplicate sample_gain_db_overrides entry",
                ),
                (
                    "not finite",
                    [{"sample": "tone.wav", "gain_db": math.nan}],
                    "finite dB corrections",
                ),
                (
                    "infinite",
                    [{"sample": "tone.wav", "gain_db": math.inf}],
                    "finite dB corrections",
                ),
                (
                    "positive limit",
                    [{"sample": "tone.wav", "gain_db": 24.01}],
                    "between -24 and \\+24",
                ),
                (
                    "negative limit",
                    [{"sample": "tone.wav", "gain_db": -24.01}],
                    "between -24 and \\+24",
                ),
                (
                    "unknown field",
                    [
                        {
                            "sample": "tone.wav",
                            "gain_db": 0.0,
                            "note": 60,
                        }
                    ],
                    "contains unknown fields",
                ),
            )
            for label, overrides, message in invalid_cases:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(ValueError, message):
                        DedicatedSfzInstrument(
                            8000,
                            {
                                **manifest,
                                "sample_gain_db_overrides": overrides,
                            },
                            str(directory),
                        )

            for boundary in (-24.0, 24.0):
                with self.subTest(boundary=boundary):
                    instrument = DedicatedSfzInstrument(
                        8000,
                        {
                            **manifest,
                            "sample_gain_db_overrides": [
                                {
                                    "sample": "tone.wav",
                                    "gain_db": boundary,
                                }
                            ],
                        },
                        str(directory),
                    )
                    expected = 10.0 ** (boundary / 20.0)
                    self.assertAlmostEqual(
                        instrument.articulations["default"].attack.regions[0].gain,
                        expected,
                    )

    def test_sample_region_exclusions_apply_everywhere_and_repair_round_robin(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "dedicated-assets"
            for name, value in (
                ("rr-1.wav", 0.1),
                ("bad-shared.wav", 0.2),
                ("rr-3.wav", 0.3),
                ("pair-good.wav", 0.4),
                ("pair-bad.wav", 0.5),
                ("accent.wav", 0.6),
            ):
                write_constant_wav(root / "samples" / name, value)
            (root / "normal.sfz").write_text(
                "<global> loop_mode=one_shot\n"
                "<group> key=60 seq_length=3\n"
                "<region> sample=samples/rr-1.wav seq_position=1\n"
                "<region> sample=samples/bad-shared.wav seq_position=2\n"
                "<region> sample=samples/rr-3.wav seq_position=3\n"
                "<group> key=61 seq_length=2\n"
                "<region> sample=samples/pair-good.wav seq_position=1\n"
                "<region> sample=samples/pair-bad.wav seq_position=2\n"
                "<region> sample=samples/bad-shared.wav key=60 trigger=release "
                "seq_length=1 seq_position=1\n",
                encoding="utf-8",
            )
            (root / "accent.sfz").write_text(
                "<region> sample=samples/bad-shared.wav lokey=60 hikey=61 "
                "pitch_keycenter=60 hivel=63\n"
                "<region> sample=samples/accent.wav lokey=60 hikey=61 "
                "pitch_keycenter=60 lovel=64\n"
                "<region> sample=samples/bad-shared.wav lokey=60 hikey=61 "
                "pitch_keycenter=60 trigger=release\n",
                encoding="utf-8",
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "dedicated-assets",
                "articulations": {
                    "normal": "normal.sfz",
                    "accent": "accent.sfz",
                },
                "default_articulation": "normal",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 61,
                "sample_region_exclusions": [
                    "samples/bad-shared.wav",
                    "samples/pair-bad.wav",
                ],
            }

            instrument = DedicatedSfzInstrument(8000, manifest, str(directory))
            all_regions = [
                region
                for articulation in instrument.articulations.values()
                for layer in (
                    *articulation.attack_layers,
                    *articulation.release_layers,
                )
                for region in layer.engine.regions
            ]
            self.assertNotIn(
                "bad-shared.wav",
                {region.path.name for region in all_regions},
            )
            self.assertNotIn(
                "pair-bad.wav",
                {region.path.name for region in all_regions},
            )
            self.assertIsNone(instrument.articulations["normal"].release)
            self.assertIsNone(instrument.articulations["accent"].release)

            normal_regions = {
                region.path.name: region
                for region in instrument.articulations["normal"].attack.regions
            }
            self.assertEqual(
                (
                    normal_regions["rr-1.wav"].round_robin_position,
                    normal_regions["rr-1.wav"].round_robin_length,
                ),
                (1, 2),
            )
            self.assertEqual(
                (
                    normal_regions["rr-3.wav"].round_robin_position,
                    normal_regions["rr-3.wav"].round_robin_length,
                ),
                (2, 2),
            )
            self.assertIsNone(
                normal_regions["pair-good.wav"].round_robin_position
            )
            self.assertIsNone(
                normal_regions["pair-good.wav"].round_robin_length
            )

            tuning = EqualTemperament()
            for sequence, note_id in enumerate((1, 2), start=1):
                instrument.handle_event(
                    event(
                        "note_on",
                        sequence,
                        note_id=note_id,
                        midi_note=60,
                        velocity=0.8,
                    ),
                    tuning,
                )
            self.assertEqual(
                [self._selected_name(instrument, note_id) for note_id in (1, 2)],
                ["rr-1.wav", "rr-3.wav"],
            )

    def test_excluded_calibration_sample_is_not_stale_but_other_samples_are(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "dedicated-assets"
            for name, value in (
                ("rr-1.wav", 0.1),
                ("bad.wav", 0.2),
                ("rr-3.wav", 0.3),
            ):
                write_constant_wav(root / name, value)
            (root / "mapped.sfz").write_text(
                "<global> key=60 seq_length=3\n"
                "<region> sample=rr-1.wav seq_position=1\n"
                "<region> sample=bad.wav seq_position=2\n"
                "<region> sample=rr-3.wav seq_position=3\n",
                encoding="utf-8",
            )
            calibration_samples = {
                name: {"measured_detune_cents": cents}
                for name, cents in (
                    ("rr-1.wav", 1.0),
                    ("bad.wav", 2.0),
                    ("rr-3.wav", 3.0),
                )
            }
            calibration_path = directory / "pitch.json"
            calibration_path.write_text(
                json.dumps(
                    {
                        "applicable": True,
                        "samples": calibration_samples,
                    }
                ),
                encoding="utf-8",
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "dedicated-assets",
                "sfz": "mapped.sfz",
                "default_articulation": "default",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
                "pitch_calibration": "pitch.json",
                "apply_pitch_calibration": True,
                "sample_region_exclusions": ["bad.wav"],
            }

            instrument = DedicatedSfzInstrument(8000, manifest, str(directory))
            self.assertEqual(
                {
                    region.path.name
                    for region in instrument.articulations["default"].attack.regions
                },
                {"rr-1.wav", "rr-3.wav"},
            )

            calibration_path.write_text(
                json.dumps(
                    {
                        "applicable": True,
                        "samples": {
                            **calibration_samples,
                            "unmapped.wav": {
                                "measured_detune_cents": 0.0,
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "outside the current attack mappings: unmapped.wav",
            ):
                DedicatedSfzInstrument(8000, manifest, str(directory))

    def test_sample_region_exclusions_reject_unsafe_unmatched_or_coverage_loss(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "dedicated-assets"
            write_constant_wav(root / "bad.wav", 0.1)
            write_constant_wav(root / "good.wav", 0.2)
            (root / "mapped.sfz").write_text(
                "<region> sample=bad.wav key=60\n"
                "<region> sample=good.wav key=61\n",
                encoding="utf-8",
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "dedicated-assets",
                "sfz": "mapped.sfz",
                "default_articulation": "default",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 61,
            }
            invalid_cases = (
                ("not an array", "bad.wav", "must be a non-empty array"),
                ("empty array", [], "must be a non-empty array"),
                (
                    "empty path",
                    [""],
                    "must be a non-empty asset-root-relative POSIX path",
                ),
                (
                    "non-string path",
                    [None],
                    "must be a non-empty asset-root-relative POSIX path",
                ),
                (
                    "absolute POSIX path",
                    ["/bad.wav"],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "parent traversal",
                    ["samples/../bad.wav"],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "dot path",
                    ["./bad.wav"],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "repeated slash",
                    ["samples//bad.wav"],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "Windows drive path",
                    ["C:/bad.wav"],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "backslash path",
                    ["samples\\bad.wav"],
                    "canonical asset-root-relative POSIX paths",
                ),
                (
                    "duplicate path",
                    ["bad.wav", "bad.wav"],
                    "duplicate sample_region_exclusions entry",
                ),
            )
            for label, exclusions, message in invalid_cases:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(ValueError, message):
                        DedicatedSfzInstrument(
                            8000,
                            {
                                **manifest,
                                "sample_region_exclusions": exclusions,
                            },
                            str(directory),
                        )

            with self.assertRaisesRegex(
                ValueError,
                "did not match loaded dedicated SFZ attack or release regions "
                "across all articulations: not-mapped.wav",
            ):
                DedicatedSfzInstrument(
                    8000,
                    {
                        **manifest,
                        "sample_region_exclusions": ["not-mapped.wav"],
                    },
                    str(directory),
                )

            with self.assertRaisesRegex(
                ValueError,
                "does not cover declared MIDI range; missing 60",
            ):
                DedicatedSfzInstrument(
                    8000,
                    {
                        **manifest,
                        "sample_region_exclusions": ["bad.wav"],
                    },
                    str(directory),
                )

    def test_velocity_layers_and_round_robin_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = directory / "专用 音源" / "样本 库"
            for name, value in (
                ("弱 一.wav", 0.1),
                ("弱 二.wav", 0.2),
                ("强 一.wav", 0.3),
                ("强 二.wav", 0.4),
            ):
                write_constant_wav(samples / name, value)
            instrument = self._instrument(
                directory,
                "<control> default_path=样本 库/\n"
                "<global> seq_length=2 loop_mode=one_shot\n"
                "<group> hivel=63\n"
                "<region> sample=弱 一.wav key=60 seq_position=1\n"
                "<region> sample=弱 二.wav key=60 seq_position=2\n"
                "<group> lovel=64\n"
                "<region> sample=强 一.wav key=60 seq_position=1\n"
                "<region> sample=强 二.wav key=60 seq_position=2\n",
            )
            tuning = EqualTemperament()
            for sequence, (note_id, velocity) in enumerate(
                ((1, 0.2), (2, 0.2), (3, 0.8), (4, 0.8))
            ):
                instrument.handle_event(
                    event(
                        "note_on",
                        sequence,
                        note_id=note_id,
                        midi_note=60,
                        velocity=velocity,
                    ),
                    tuning,
                )
            self.assertEqual(
                [self._selected_name(instrument, note_id) for note_id in range(1, 5)],
                ["弱 一.wav", "弱 二.wav", "强 一.wav", "强 二.wav"],
            )

            second = self._instrument(
                directory,
                "<control> default_path=样本 库/\n"
                "<global> seq_length=2 loop_mode=one_shot\n"
                "<region> sample=弱 一.wav key=60 seq_position=1\n"
                "<region> sample=弱 二.wav key=60 seq_position=2\n",
            )
            second.handle_event(
                event("note_on", 0, note_id=9, midi_note=60, velocity=0.2), tuning
            )
            self.assertEqual(self._selected_name(second, 9), "弱 一.wav")

    def test_lorand_hirand_use_one_repeatable_choice_not_layered_noise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = directory / "专用 音源"
            write_constant_wav(samples / "random-a.wav", 0.1)
            write_constant_wav(samples / "random-b.wav", 0.2)
            mapping = (
                "<region> sample=random-a.wav key=60 lorand=0 hirand=0.5 "
                "loop_mode=one_shot\n"
                "<region> sample=random-b.wav key=60 lorand=0.5 hirand=1 "
                "loop_mode=one_shot\n"
            )
            first = self._instrument(directory, mapping)
            tuning = EqualTemperament()
            first.handle_event(
                event("note_on", 17, note_id=3, midi_note=60, velocity=1.0), tuning
            )
            self.assertEqual(len(first.articulations["default"].attack_layers), 1)
            self.assertEqual(len(first.routes[3].voices), 1)
            first_name = self._selected_name(first, 3)

            second = self._instrument(directory, mapping)
            second.handle_event(
                event("note_on", 17, note_id=3, midi_note=60, velocity=1.0), tuning
            )
            self.assertEqual(self._selected_name(second, 3), first_name)

    def test_embedded_loop_release_trigger_and_sustain_pedal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = directory / "专用 音源" / "样本"
            write_constant_wav(
                samples / "持续.wav",
                0.2,
                frame_count=16,
                embedded_loop=(4, 11),
            )
            write_constant_wav(samples / "释放.wav", 0.6, frame_count=32)
            instrument = self._instrument(
                directory,
                "<control> default_path=样本/\n"
                "<region> sample=持续.wav key=60 loop_mode=loop_sustain "
                "ampeg_release=0.01\n"
                "<region> sample=释放.wav key=60 trigger=release\n",
            )
            tuning = EqualTemperament()
            instrument.handle_event(
                event("note_on", 0, note_id=1, midi_note=60, velocity=1.0), tuning
            )
            route = instrument.routes[1]
            attack = instrument.articulations["default"].attack
            for _ in range(80):
                instrument.render_frame()
            self.assertIn(route.internal_note_id, attack.voices)

            instrument.handle_event(
                event("control", 1, name="sustain_pedal", value=1.0), tuning
            )
            instrument.handle_event(
                event("note_off", 2, note_id=1, release_velocity=0.9), tuning
            )
            self.assertFalse(attack.voices[route.internal_note_id].released)
            release = instrument.articulations["default"].release
            self.assertIsNotNone(release)
            assert release is not None
            self.assertEqual(release.active_voice_count, 0)

            instrument.handle_event(
                event("control", 3, name="sustain_pedal", value=0.0), tuning
            )
            self.assertTrue(attack.voices[route.internal_note_id].released)
            self.assertEqual(release.active_voice_count, 1)
            release_voice = next(iter(release.voices.values()))
            self.assertEqual(release_voice.region.path.name, "释放.wav")
            self.assertEqual(release_voice.region.loop_mode, "one_shot")

    def test_runtime_pitch_calibration_and_release_override_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "tone.wav", 0.25, frame_count=800)
            (root / "mapped.sfz").write_text(
                "<region> sample=tone.wav key=60 ampeg_release=0.5\n",
                encoding="utf-8",
            )
            (directory / "音准校准.json").write_text(
                json.dumps(
                    {
                        "applicable": True,
                        "samples": {
                            "tone.wav": {
                                "measured_detune_cents": 50.0,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            instrument = DedicatedSfzInstrument(
                8000,
                {
                    "type": "dedicated_sfz",
                    "asset_root": "专用 音源",
                    "pitch_calibration": "音准校准.json",
                    "apply_pitch_calibration": True,
                    "articulations": {
                        "damped": {
                            "sfz": "mapped.sfz",
                            "attack_override_seconds": 0.005,
                            "release_override_seconds": 0.01,
                        }
                    },
                    "default_articulation": "damped",
                    "pitch_mode": "pitched",
                    "note_min": 60,
                    "note_max": 60,
                },
                str(directory),
            )
            region = instrument.articulations["damped"].attack.regions[0]
            self.assertEqual(region.attack_seconds, 0.005)
            expected_root = EqualTemperament().note_to_hz(60) * (
                2.0 ** (50.0 / 1200.0)
            )
            self.assertAlmostEqual(region.root_pitch_hz, expected_root)

            tuning = EqualTemperament()
            instrument.handle_event(
                event("note_on", 0, note_id=1, midi_note=60, velocity=1.0),
                tuning,
            )
            route = instrument.routes[1]
            voice = instrument.articulations["damped"].attack.voices[
                route.internal_note_id
            ]
            self.assertEqual(voice.release_samples, 4000)
            instrument.handle_event(
                event("note_off", 1, note_id=1),
                tuning,
            )
            self.assertTrue(voice.released)
            self.assertEqual(voice.release_samples, 80)

            invalid_manifest = {
                "type": "dedicated_sfz",
                "asset_root": "专用 音源",
                "articulations": {
                    "bad": {
                        "sfz": "mapped.sfz",
                        "attack_override_seconds": -0.01,
                    }
                },
                "default_articulation": "bad",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
            }
            with self.assertRaisesRegex(
                ValueError,
                "attack_override_seconds must be finite and non-negative",
            ):
                DedicatedSfzInstrument(
                    8000,
                    invalid_manifest,
                    str(directory),
                )

    def test_runtime_pitch_calibration_rejects_stale_or_missing_samples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "tone.wav", 0.25)
            (root / "mapped.sfz").write_text(
                "<region> sample=tone.wav key=60\n",
                encoding="utf-8",
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "专用 音源",
                "pitch_calibration": "音准校准.json",
                "apply_pitch_calibration": True,
                "articulations": {"default": "mapped.sfz"},
                "default_articulation": "default",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
            }
            for samples, message in (
                (
                    {"other.wav": {"measured_detune_cents": 0.0}},
                    "no finite measured_detune_cents",
                ),
                (
                    {
                        "tone.wav": {"measured_detune_cents": 0.0},
                        "stale.wav": {"measured_detune_cents": 0.0},
                    },
                    "outside the current attack mappings",
                ),
            ):
                with self.subTest(message=message):
                    (directory / "音准校准.json").write_text(
                        json.dumps({"applicable": True, "samples": samples}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, message):
                        DedicatedSfzInstrument(
                            8000,
                            manifest,
                            str(directory),
                        )

    def test_group_off_by_chokes_an_existing_voice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = directory / "专用 音源"
            write_constant_wav(
                samples / "open.wav", 0.2, embedded_loop=(8, 63)
            )
            write_constant_wav(samples / "closed.wav", 0.4)
            instrument = self._instrument(
                directory,
                "<group> group=1 off_time=0.005\n"
                "<region> sample=open.wav key=60 loop_mode=loop_sustain\n"
                "<group> group=2 off_by=1\n"
                "<region> sample=closed.wav key=61 loop_mode=one_shot\n",
                manifest_extra={"note_min": 60, "note_max": 61},
            )
            tuning = EqualTemperament()
            instrument.handle_event(
                event("note_on", 0, note_id=1, midi_note=60, velocity=1.0), tuning
            )
            first_route = instrument.routes[1]
            instrument.handle_event(
                event("note_on", 1, note_id=2, midi_note=61, velocity=1.0), tuning
            )
            attack = instrument.articulations["default"].attack
            self.assertNotIn(1, instrument.routes)
            self.assertTrue(attack.voices[first_route.internal_note_id].released)
            self.assertAlmostEqual(
                attack.voices[first_route.internal_note_id].release_samples / 8000,
                0.005,
            )

    def test_pitch_modes_fixed_and_ignore_do_not_masquerade_as_pitched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = directory / "专用 音源"
            write_constant_wav(samples / "native.wav", 0.25)
            fixed = self._instrument(
                directory,
                "<region> sample=native.wav key=36 loop_mode=one_shot\n",
                manifest_extra={
                    "pitch_mode": "fixed",
                    "fixed_midi_note": 36,
                    "note_min": 0,
                    "note_max": 127,
                },
            )
            tuning = EqualTemperament()
            fixed.handle_event(
                event("note_on", 0, note_id=1, midi_note=96, velocity=1.0), tuning
            )
            fixed_route = fixed.routes[1]
            fixed_voice = fixed.articulations["default"].attack.voices[
                fixed_route.internal_note_id
            ]
            self.assertAlmostEqual(fixed_voice.increment, 1.0)

        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = directory / "专用 音源"
            write_constant_wav(samples / "native.wav", 0.25)
            ignored = self._instrument(
                directory,
                "<region> sample=native.wav lokey=60 hikey=61 pitch_keycenter=60 "
                "loop_mode=one_shot tune=100\n",
                manifest_extra={
                    "pitch_mode": "ignore",
                    "note_min": 60,
                    "note_max": 61,
                },
            )
            ignored.handle_event(
                event("note_on", 0, note_id=1, midi_note=61, velocity=1.0), tuning
            )
            ignored_route = ignored.routes[1]
            ignored_voice = ignored.articulations["default"].attack.voices[
                ignored_route.internal_note_id
            ]
            self.assertAlmostEqual(ignored_voice.increment, 2.0 ** (100.0 / 1200.0))

    def test_segmented_ranges_gate_pitched_and_ignore_inputs(self) -> None:
        for pitch_mode in ("pitched", "ignore"):
            with self.subTest(pitch_mode=pitch_mode):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    root = directory / "专用 音源"
                    write_constant_wav(root / "native.wav", 0.25)
                    (root / "segmented.sfz").write_text(
                        "<region> sample=native.wav lokey=60 hikey=61 "
                        "pitch_keycenter=60\n"
                        "<region> sample=native.wav lokey=64 hikey=65 "
                        "pitch_keycenter=64\n",
                        encoding="utf-8",
                    )
                    instrument = DedicatedSfzInstrument(
                        8000,
                        {
                            "type": "dedicated_sfz",
                            "asset_root": "专用 音源",
                            "sfz": "segmented.sfz",
                            "pitch_mode": pitch_mode,
                            "playable_ranges": [[60, 61], [64, 65]],
                        },
                        str(directory),
                    )
                    self.assertEqual(instrument.note_min, 60.0)
                    self.assertEqual(instrument.note_max, 65.0)
                    self.assertEqual(
                        instrument.playable_ranges,
                        ((60.0, 61.0), (64.0, 65.0)),
                    )
                    tuning = EqualTemperament()
                    for sequence, note in enumerate((61, 64), start=1):
                        instrument.handle_event(
                            event(
                                "note_on",
                                sequence,
                                note_id=sequence,
                                midi_note=note,
                                velocity=1.0,
                            ),
                            tuning,
                        )
                        self.assertIn(sequence, instrument.routes)
                    for sequence, note in enumerate((62, 63), start=10):
                        with self.assertRaisesRegex(
                            ValueError,
                            "outside declared playable ranges 60..61, 64..65",
                        ):
                            instrument.handle_event(
                                event(
                                    "note_on",
                                    sequence,
                                    note_id=sequence,
                                    midi_note=note,
                                    velocity=1.0,
                                ),
                                tuning,
                            )
                        self.assertNotIn(sequence, instrument.routes)

    def test_segmented_ranges_must_match_explicit_outer_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "native.wav", 0.25)
            (root / "segmented.sfz").write_text(
                "<region> sample=native.wav lokey=60 hikey=61 "
                "pitch_keycenter=60\n"
                "<region> sample=native.wav lokey=64 hikey=65 "
                "pitch_keycenter=64\n",
                encoding="utf-8",
            )
            base = {
                "type": "dedicated_sfz",
                "asset_root": "专用 音源",
                "sfz": "segmented.sfz",
                "pitch_mode": "pitched",
                "playable_ranges": [[60, 61], [64, 65]],
            }
            matching = DedicatedSfzInstrument(
                8000,
                {**base, "note_min": 60, "note_max": 65},
                str(directory),
            )
            self.assertEqual(matching.note_min, 60.0)
            self.assertEqual(matching.note_max, 65.0)
            for note_min, note_max in ((59, 65), (60, 66), (61, 65)):
                with self.subTest(note_min=note_min, note_max=note_max):
                    with self.assertRaisesRegex(
                        ValueError,
                        "must match the outer envelope",
                    ):
                        DedicatedSfzInstrument(
                            8000,
                            {
                                **base,
                                "note_min": note_min,
                                "note_max": note_max,
                            },
                            str(directory),
                        )

    def test_fixed_pitch_still_ignores_segmented_input_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "native.wav", 0.25)
            (root / "fixed.sfz").write_text(
                "<region> sample=native.wav key=36 loop_mode=one_shot\n",
                encoding="utf-8",
            )
            instrument = DedicatedSfzInstrument(
                8000,
                {
                    "type": "dedicated_sfz",
                    "asset_root": "专用 音源",
                    "sfz": "fixed.sfz",
                    "pitch_mode": "fixed",
                    "fixed_midi_note": 36,
                    "playable_ranges": [[36, 36]],
                },
                str(directory),
            )
            instrument.handle_event(
                event("note_on", 0, note_id=1, midi_note=96, velocity=1.0),
                EqualTemperament(),
            )
            self.assertIn(1, instrument.routes)
            voice = instrument.articulations["default"].attack.voices[
                instrument.routes[1].internal_note_id
            ]
            self.assertAlmostEqual(voice.increment, 1.0)

    @pytest.mark.external_assets
    def test_real_bagpipe_chanter_and_drones_have_independent_ranges(self) -> None:
        manifest_path = ROOT / "乐器" / "世界乐器" / "风笛" / "乐器.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        asset_root = (
            manifest_path.parent / str(manifest["asset_root"])
        ).resolve()
        derived = asset_root / "派生" / "风笛-v1"
        if not (derived / "chanter.sfz").is_file():
            self.skipTest(f"derived FreePats bagpipe resource is not installed: {derived}")

        instrument = DedicatedSfzInstrument(
            8000,
            manifest,
            str(manifest_path.parent),
        )
        tuning = EqualTemperament()
        for sequence, note in enumerate((64, 81), start=1):
            instrument.handle_event(
                event(
                    "note_on",
                    sequence,
                    note_id=sequence,
                    midi_note=note,
                    velocity=0.7,
                ),
                tuning,
            )
            self.assertIn(sequence, instrument.routes)
        for sequence, note in enumerate((43, 55, 63), start=10):
            with self.assertRaisesRegex(
                ValueError,
                "outside declared playable ranges 64..81",
            ):
                instrument.handle_event(
                    event(
                        "note_on",
                        sequence,
                        note_id=sequence,
                        midi_note=note,
                        velocity=0.7,
                    ),
                    tuning,
                )
            self.assertNotIn(sequence, instrument.routes)

        instrument.handle_event(event("articulation", 20, name="drone_low"), tuning)
        instrument.handle_event(
            event("note_on", 21, note_id=21, midi_note=43, velocity=0.7),
            tuning,
        )
        self.assertIn(21, instrument.routes)
        instrument.handle_event(event("articulation", 22, name="drone_high"), tuning)
        instrument.handle_event(
            event("note_on", 23, note_id=23, midi_note=55, velocity=0.7),
            tuning,
        )
        self.assertIn(23, instrument.routes)

    def test_expression_modulation_and_multiple_articulations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "库"
            root.mkdir()
            write_constant_wav(root / "a.wav", 0.4, frame_count=4096)
            write_constant_wav(root / "b.wav", 0.2, frame_count=4096)
            (root / "长.sfz").write_text(
                "<region> sample=a.wav key=60 loop_mode=one_shot\n", encoding="utf-8"
            )
            (root / "短.sfz").write_text(
                "<region> sample=b.wav key=60 loop_mode=one_shot\n", encoding="utf-8"
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "库",
                "articulations": {"long": "长.sfz", "short": {"sfz": "短.sfz"}},
                "default_articulation": "long",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
                "control_smoothing_seconds": 0.001,
            }
            instrument = create_instrument(manifest, 8000, base_directory=str(directory))
            self.assertIsInstance(instrument, DedicatedSfzInstrument)
            tuning = EqualTemperament()
            instrument.handle_event(
                event("articulation", 0, name="short"), tuning
            )
            instrument.handle_event(
                event("note_on", 1, note_id=1, midi_note=60, velocity=1.0), tuning
            )
            self.assertEqual(instrument.articulation, "short")
            before = abs(instrument.render_frame()[0])
            instrument.handle_event(
                event("control", 2, name="expression", value=0.0), tuning
            )
            instrument.handle_event(
                event("control", 3, name="modulation", value=0.0), tuning
            )
            for _ in range(80):
                after = abs(instrument.render_frame()[0])
            self.assertGreater(before, 0.1)
            self.assertLess(after, before * 0.01)

    def test_velocity_crossfade_adsr_and_release_trigger_rt_decay(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            samples = directory / "专用 音源"
            write_constant_wav(samples / "soft.wav", 0.4, frame_count=4096)
            write_constant_wav(samples / "hard.wav", 0.4, frame_count=4096)
            write_constant_wav(samples / "release.wav", 0.6, frame_count=4096)
            instrument = self._instrument(
                directory,
                "<region> sample=soft.wav key=60 xfout_lovel=32 xfout_hivel=96 "
                "ampeg_attack=0.01 ampeg_decay=0.02 ampeg_sustain=25\n"
                "<region> sample=hard.wav key=60 xfin_lovel=32 xfin_hivel=96 "
                "ampeg_attack=0.01 ampeg_decay=0.02 ampeg_sustain=25\n"
                "<group> trigger=release rt_decay=6\n"
                "<region> sample=release.wav key=60\n",
            )
            tuning = EqualTemperament()
            instrument.handle_event(
                event("note_on", 0, note_id=1, midi_note=60, velocity=0.5), tuning
            )
            runtime = instrument.articulations["default"]
            self.assertEqual(len(runtime.attack_layers), 2)
            route = instrument.routes[1]
            self.assertEqual(len(route.voices), 2)
            voices = [
                runtime.attack_layers[item.layer_index].engine.voices[
                    item.internal_note_id
                ]
                for item in route.voices
            ]
            self.assertTrue(all(voice.attack_samples == 80 for voice in voices))
            self.assertTrue(all(voice.decay_samples == 160 for voice in voices))
            # 输入力度 0.5 会先对齐到 SFZ 的整数力度网格(64/127),
            # 等功率交叉渐变的振幅和因此等于对齐后的力度。
            self.assertAlmostEqual(
                sum(voice.amplitude for voice in voices), round(0.5 * 127.0) / 127.0
            )
            for _ in range(80):
                instrument.render_frame()
            self.assertTrue(all(math.isclose(voice.envelope, 1.0) for voice in voices))
            for _ in range(160):
                instrument.render_frame()
            self.assertTrue(all(math.isclose(voice.envelope, 0.25) for voice in voices))

            instrument.handle_event(
                PerformanceEvent(
                    16_000,
                    1,
                    "note_off",
                    {"note_id": 1, "release_velocity": 1.0},
                ),
                tuning,
            )
            self.assertEqual(len(runtime.release_layers), 1)
            release_voice = next(iter(runtime.release_layers[0].engine.voices.values()))
            # SFZ release triggers use the corresponding note-on velocity
            # 0.5, not the deliberately conflicting note-off velocity 1.0.
            # The held-note rt_decay then contributes the remaining -12 dB.
            self.assertAlmostEqual(
                release_voice.amplitude,
                0.5 * 10.0 ** (-12.0 / 20.0),
                places=7,
            )

    def test_cc10_pan_idiom_lands_centred_instead_of_hard_left(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "音.wav", 0.4)
            # 常见惯用法:基准硬左 + CC10 调制回中。只读 pan 会把整件
            # 乐器停在硬左(Karoryfer Meatbass 曾因此右声道全哑)。
            (root / "cc10.sfz").write_text(
                "<region> sample=音.wav key=60 pan=-100 pan_oncc10=200\n",
                encoding="utf-8",
            )
            # 双极曲线写法:CC10 默认 64 即中点,声像应保持基准值。
            (root / "bipolar.sfz").write_text(
                "<region> sample=音.wav key=60 pan=0 pan_oncc10=100 "
                "pan_curvecc10=1\n",
                encoding="utf-8",
            )
            base = {
                "type": "dedicated_sfz",
                "asset_root": "专用 音源",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
            }
            for name in ("cc10.sfz", "bipolar.sfz"):
                instrument = create_instrument(
                    {**base, "sfz": name}, 8000, base_directory=str(directory)
                )
                regions = [
                    region
                    for layer in instrument.articulations["default"].attack_layers
                    for region in layer.engine.regions
                ]
                self.assertEqual(len(regions), 1, name)
                self.assertAlmostEqual(regions[0].pan, 0.0, delta=0.02, msg=name)

    def test_static_cc_routing_and_envelope_modulation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "basic.wav", 0.3, frame_count=4096)
            write_constant_wav(root / "three-voice.wav", 0.6, frame_count=4096)
            instrument = self._instrument(
                directory,
                "<control> set_cc103=127\n"
                "<global> ampeg_attack=0.001 ampeg_decay=0.001 "
                "ampeg_sustain=0 ampeg_sustain_oncc103=100\n"
                "<group> hicc107=13\n"
                "<region> sample=basic.wav key=60\n"
                "<group> locc107=26 hicc107=40\n"
                "<region> sample=three-voice.wav key=60\n",
            )
            tuning = EqualTemperament()
            instrument.handle_event(
                event("note_on", 0, note_id=1, midi_note=60, velocity=1.0),
                tuning,
            )
            route = instrument.routes[1]
            self.assertEqual(len(route.voices), 1)
            voice = instrument.articulations["default"].attack.voices[
                route.internal_note_id
            ]
            self.assertEqual(voice.region.path.name, "basic.wav")
            for _ in range(round(0.2 * 8000)):
                instrument.render_frame()
            self.assertGreater(voice.envelope, 0.99)
            self.assertGreater(abs(instrument.render_frame()[0]), 0.1)

    def test_unknown_dynamic_cc_routing_fails_instead_of_layering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "sample.wav", 0.2)
            with self.assertRaisesRegex(ValueError, "dynamic routing opcode"):
                self._instrument(
                    directory,
                    "<region> sample=sample.wav key=60 lohdcc107=0.2\n",
                )

    def test_static_keyswitch_selection_and_dynamic_opcode_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            write_constant_wav(root / "普通.wav", 0.2)
            write_constant_wav(root / "备选.wav", 0.4)
            (root / "keyswitch.sfz").write_text(
                "<global> sw_lokey=36 sw_hikey=37 sw_default=36\n"
                "<region> sample=普通.wav key=60 sw_lolast=36 sw_hilast=36\n"
                "<region> sample=备选.wav key=60 sw_lolast=37 sw_hilast=37\n",
                encoding="utf-8",
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "专用 音源",
                "sfz": "keyswitch.sfz",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
            }
            # sw_default 静态选中默认层;每个奏法只保留一个键切换层。
            instrument = create_instrument(manifest, 8000, base_directory=str(directory))
            runtime = instrument.articulations["default"]
            self.assertEqual(
                sum(len(layer.engine.regions) for layer in runtime.attack_layers), 1
            )
            # 显式 keyswitch_select 覆盖 sw_default,选中另一层。
            alternate = create_instrument(
                {**manifest, "keyswitch_select": 37},
                8000,
                base_directory=str(directory),
            )
            alternate_runtime = alternate.articulations["default"]
            selected = [
                Path(region.path).name
                for layer in alternate_runtime.attack_layers
                for region in layer.engine.regions
            ]
            self.assertEqual(selected, ["备选.wav"])

            # 没有 sw_default 也没有 keyswitch_select 时仍然快速失败。
            (root / "no_default.sfz").write_text(
                "<region> sample=普通.wav key=60 sw_lolast=36 sw_hilast=36\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires a keyswitch"):
                create_instrument(
                    {**manifest, "sfz": "no_default.sfz"},
                    8000,
                    base_directory=str(directory),
                )

            # 动态键切换操作码依旧拒绝。
            (root / "dynamic.sfz").write_text(
                "<region> sample=普通.wav key=60 sw_down=36\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "dynamic keyswitch"):
                create_instrument(
                    {**manifest, "sfz": "dynamic.sfz"},
                    8000,
                    base_directory=str(directory),
                )

    def test_missing_assets_fail_without_any_general_midi_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            root = directory / "专用 音源"
            root.mkdir()
            (root / "missing.sfz").write_text(
                "<region> sample=不存在.wav key=60\n", encoding="utf-8"
            )
            manifest = {
                "type": "dedicated_sfz",
                "asset_root": "专用 音源",
                "sfz": "missing.sfz",
                "pitch_mode": "pitched",
                "note_min": 60,
                "note_max": 60,
            }
            with self.assertRaisesRegex(ValueError, "sample file does not exist"):
                create_instrument(manifest, 8000, base_directory=str(directory))


if __name__ == "__main__":
    unittest.main()
