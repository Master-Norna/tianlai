from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

import pytest

from tianlai.dedicated_sfz import dedicated_regions_to_manifest


ROOT = Path(__file__).resolve().parents[1]
CONVERTER_PATH = (
    ROOT / "乐器" / "键盘乐器" / "击弦古钢琴" / "转换SIMPK音源.py"
)
SPEC = importlib.util.spec_from_file_location(
    "tianlai_simpk_clavichord_converter", CONVERTER_PATH
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import converter: {CONVERTER_PATH}")
CONVERTER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = CONVERTER
SPEC.loader.exec_module(CONVERTER)


def _note_name(note: int) -> str:
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return f"{names[note % 12]}{note // 12 - 1}"


def _write_stereo_24_bit_wav(path: Path, *, frame_count: int = 12) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(3)
        output.setframerate(48000)
        output.writeframes(b"\0" * frame_count * 2 * 3)


def _sample_xml(
    timbre: str,
    note: int,
    layer: int,
    velocity_low: int,
    velocity_high: int,
    round_robin: int,
    *,
    trigger: str,
) -> str:
    path = (
        f"assets\\wav\\{timbre}\\{note}_{_note_name(note)}_"
        f"{layer:02d}_{round_robin - 1:02d}.wav"
    )
    if trigger == "attack":
        start, end = 0, 16
    else:
        start, end = 15, 24
    return (
        f'<sample rootNote="{note}" loNote="{note}" hiNote="{note}" '
        f'loVel="{velocity_low}" hiVel="{velocity_high}" '
        f'seqPosition="{round_robin}" path="{path}" '
        f'start="{start}" end="{end}" />'
    )


def _group_xml(timbre: str, trigger: str) -> str:
    enabled = "true" if trigger == "attack" else "false"
    release = "4" if trigger == "attack" else "0"
    amp_velocity = "0" if (timbre, trigger) == ("lupe", "release") else "1"
    samples = []
    # Deliberately reverse source order: generated SFZ order must still be stable.
    for note in range(CONVERTER.NOTE_MAX, CONVERTER.NOTE_MIN - 1, -1):
        for layer, (velocity_low, velocity_high) in reversed(
            tuple(enumerate(CONVERTER.VELOCITY_LAYERS))
        ):
            for round_robin in (2, 1):
                samples.append(
                    _sample_xml(
                        timbre,
                        note,
                        layer,
                        velocity_low,
                        velocity_high,
                        round_robin,
                        trigger=trigger,
                    )
                )
    return (
        f'<group trigger="{trigger}" enabled="{enabled}" loopEnabled="false" '
        f'start="0" release="{release}" ampVelTrack="{amp_velocity}" '
        f'tags="{timbre}">\n'
        + "\n".join(samples)
        + "\n</group>"
    )


def _build_source_fixture(root: Path) -> bytes:
    for timbre in CONVERTER.TIMBRES:
        for note in range(CONVERTER.NOTE_MIN, CONVERTER.NOTE_MAX + 1):
            for layer, _velocities in enumerate(CONVERTER.VELOCITY_LAYERS):
                for round_robin_zero_based in range(CONVERTER.ROUND_ROBIN_LENGTH):
                    filename = (
                        f"{note}_{_note_name(note)}_{layer:02d}_"
                        f"{round_robin_zero_based:02d}.wav"
                    )
                    _write_stereo_24_bit_wav(
                        root / "assets" / "wav" / timbre / filename
                    )
    text = (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        "<!-- Für den CP-1252 fallback test. -->\n"
        "<DecentSampler>\n"
        '<ui><labeled-knob label="Strings <-> Resonance" /></ui>\n'
        '<groups seqMode="round_robin">\n'
        + _group_xml("reso", "attack")
        + "\n"
        + _group_xml("reso", "release")
        + "\n"
        + _group_xml("lupe", "attack")
        + "\n"
        + _group_xml("lupe", "release")
        + "\n</groups>\n"
        "<effects />\n"
        "</DecentSampler>\n"
    )
    payload = text.encode("cp1252")
    (root / CONVERTER.PRESET_NAME).write_bytes(payload)
    return payload


class SimpkClavichordConverterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.source_root = Path(cls.temporary_directory.name) / "SIMPK_03_Clavichord"
        cls.source_root.mkdir()
        cls.baseline_preset = _build_source_fixture(cls.source_root)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def setUp(self) -> None:
        (self.source_root / CONVERTER.PRESET_NAME).write_bytes(
            self.baseline_preset
        )

    def _active_paths(self) -> list[str]:
        return [
            record.sample_path
            for record in CONVERTER.parse_dspreset(
                self.source_root / CONVERTER.PRESET_NAME
            )
            if record.trigger == "attack" and record.enabled
        ]

    def _write_tuning_table(
        self,
        path: Path,
        values: dict[str, object],
    ) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "unit": "cents",
                    "measured_detune_cents": values,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def test_cp1252_and_known_bad_label_are_parsed_strictly(self) -> None:
        records = CONVERTER.parse_dspreset(
            self.source_root / CONVERTER.PRESET_NAME
        )

        self.assertEqual(len(records), 1512)
        self.assertEqual(
            len(
                [
                    record
                    for record in records
                    if record.trigger == "attack" and record.enabled
                ]
            ),
            756,
        )
        validated = CONVERTER.validate_simpk_source(self.source_root)
        self.assertEqual(len(validated), 756)
        self.assertEqual({sample.channels for sample in validated}, {2})
        self.assertEqual({sample.offset_frames for sample in validated}, {0})
        self.assertEqual({sample.end_frame_exclusive for sample in validated}, {8})

    def test_generated_sfz_is_deterministic_and_uses_inclusive_frame_end(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(dir=self.source_root) as first_directory:
            first = CONVERTER.convert_simpk_clavichord(
                self.source_root,
                output_directory=Path(first_directory),
            )
            normal_bytes = first.normal_sfz.read_bytes()
            resonance_bytes = first.resonance_sfz.read_bytes()
            normal_regions, _runtime = dedicated_regions_to_manifest(
                first.normal_sfz,
                asset_root=self.source_root,
                stable_prefix="tianlai/normal.sfz",
            )
        with tempfile.TemporaryDirectory(dir=self.source_root) as second_directory:
            second = CONVERTER.convert_simpk_clavichord(
                self.source_root,
                output_directory=Path(second_directory),
            )
            self.assertEqual(normal_bytes, second.normal_sfz.read_bytes())
            self.assertEqual(resonance_bytes, second.resonance_sfz.read_bytes())

        self.assertEqual(first.attack_sample_count, 756)
        self.assertFalse(first.tuning_applied)
        self.assertEqual(len(normal_regions), 378)
        self.assertEqual(
            {region["root_midi"] for region in normal_regions},
            set(
                range(
                    CONVERTER.PLAYBACK_NOTE_MIN,
                    CONVERTER.PLAYBACK_NOTE_MAX + 1,
                )
            ),
        )
        first_region = normal_regions[0]
        # The source preset labels this recording MIDI 40, but its measured
        # fundamental is MIDI 28.  Preserve the native octave instead of
        # pitching every sample up twelve semitones.
        self.assertEqual(first_region["root_midi"], 28.0)
        self.assertEqual(first_region["key_min"], 28.0)
        self.assertEqual(first_region["key_max"], 28.0)
        self.assertEqual(first_region["round_robin_length"], 2)
        self.assertEqual(first_region["round_robin_position"], 1)
        self.assertEqual(first_region["offset_frames"], 0)
        # SIMPK attack end=16 raw stereo values -> exclusive frame 8 ->
        # inclusive SFZ end=7 -> engine-internal exclusive sample_end=8.
        self.assertEqual(first_region["sample_end"], 8)
        self.assertEqual(first_region["loop_mode"], "no_loop")
        self.assertEqual(first_region["release_seconds"], 4.0)

    def test_complete_tuning_table_writes_inverse_sfz_tune(self) -> None:
        active_paths = self._active_paths()
        values: dict[str, object] = {sample_path: 0.0 for sample_path in active_paths}
        target = "assets/wav/lupe/69_A4_01_00.wav"
        values[target] = 12.5
        with tempfile.TemporaryDirectory(dir=self.source_root) as directory:
            directory_path = Path(directory)
            tuning_path = directory_path / "tuning.json"
            self._write_tuning_table(tuning_path, values)
            output = directory_path / "generated"
            result = CONVERTER.convert_simpk_clavichord(
                self.source_root,
                output_directory=output,
                tuning_table=tuning_path,
                require_complete_tuning=True,
            )
            regions, _runtime = dedicated_regions_to_manifest(
                result.normal_sfz,
                asset_root=self.source_root,
                stable_prefix="tianlai/normal.sfz",
            )

        self.assertTrue(result.tuning_applied)
        matching = [
            region
            for region in regions
            if Path(region["sample"]).as_posix().endswith(target)
        ]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["measured_tuning_cents"], 12.5)
        self.assertEqual(matching[0]["native_playback_ratio"], 2.0 ** (-12.5 / 1200.0))

    def test_tuning_table_rejects_missing_extra_and_non_numeric_entries(
        self,
    ) -> None:
        active_paths = self._active_paths()
        complete: dict[str, object] = {
            sample_path: 0.0 for sample_path in active_paths
        }
        with tempfile.TemporaryDirectory(dir=self.source_root) as directory:
            directory_path = Path(directory)
            tuning_path = directory_path / "tuning.json"

            cases: list[tuple[str, dict[str, object], str]] = []
            missing = dict(complete)
            missing.pop(active_paths[0])
            cases.append(("missing", missing, "missing_count=1"))
            extra = dict(complete)
            extra["assets/wav/lupe/not-a-source-sample.wav"] = 0.0
            cases.append(("extra", extra, "unexpected_count=1"))
            boolean = dict(complete)
            boolean[active_paths[0]] = True
            cases.append(("boolean", boolean, "must be a JSON number"))

            for name, values, expected_error in cases:
                with self.subTest(name=name):
                    self._write_tuning_table(tuning_path, values)
                    with self.assertRaisesRegex(ValueError, expected_error):
                        CONVERTER.load_tuning_table(
                            tuning_path,
                            expected_sample_paths=active_paths,
                        )

            tuning_path.write_text(
                '{"schema_version":1,"unit":"cents",'
                '"measured_detune_cents":{"x":NaN}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "non-finite"):
                CONVERTER.load_tuning_table(
                    tuning_path,
                    expected_sample_paths=active_paths,
                )

    def test_formal_mode_requires_a_tuning_table(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.source_root) as directory:
            with self.assertRaisesRegex(ValueError, "requires --tuning-table"):
                CONVERTER.convert_simpk_clavichord(
                    self.source_root,
                    output_directory=Path(directory),
                    require_complete_tuning=True,
                )

    def test_source_mapping_changes_are_rejected(self) -> None:
        source = self.baseline_preset.decode("cp1252")
        mutations = (
            (
                "velocity",
                source.replace('loVel="0" hiVel="40"', 'loVel="0" hiVel="39"', 1),
                "filename disagrees|wrong note/velocity/RR coverage",
            ),
            (
                "enabled",
                source.replace(
                    'trigger="attack" enabled="true"',
                    'trigger="attack" enabled="false"',
                    1,
                ),
                "group .* changed",
            ),
            (
                "release boundary",
                source.replace('start="15" end="24"', 'start="14" end="24"', 1),
                "attack/release boundary changed",
            ),
            (
                "path",
                source.replace(
                    "assets\\wav\\reso\\102_F#7_02_01.wav",
                    "..\\outside.wav",
                    1,
                ),
                "unsafe SIMPK sample path",
            ),
        )
        preset = self.source_root / CONVERTER.PRESET_NAME
        for name, mutated, expected_error in mutations:
            with self.subTest(name=name):
                preset.write_bytes(mutated.encode("cp1252"))
                with self.assertRaisesRegex(ValueError, expected_error):
                    CONVERTER.parse_dspreset(preset)
                preset.write_bytes(self.baseline_preset)


@pytest.mark.external_assets
class InstalledSimpkClavichordResourceTests(unittest.TestCase):
    def test_full_workspace_resource_when_installed(self) -> None:
        source_root = ROOT / "音源" / "SIMPK_03_Clavichord"
        if not (source_root / CONVERTER.PRESET_NAME).is_file():
            self.skipTest("SIMPK clavichord resource has not been extracted yet")

        samples = CONVERTER.validate_simpk_source(source_root)

        self.assertEqual(len(samples), 756)
        self.assertEqual(
            {sample.root_note for sample in samples},
            set(range(CONVERTER.NOTE_MIN, CONVERTER.NOTE_MAX + 1)),
        )
        self.assertEqual(
            {
                (sample.velocity_low, sample.velocity_high)
                for sample in samples
            },
            set(CONVERTER.VELOCITY_LAYERS),
        )
        self.assertEqual(
            {sample.round_robin_position for sample in samples},
            {1, 2},
        )
        self.assertEqual({sample.timbre for sample in samples}, {"lupe", "reso"})


if __name__ == "__main__":
    unittest.main()
