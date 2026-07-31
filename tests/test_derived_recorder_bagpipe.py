from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import unittest

import numpy as np
import pytest
import soundfile as sf

import tianlai.resource_restore as restore_module
from tianlai.derived_samples import build_derived_resources, sha256_file
from tianlai.instrument import create_instrument
from tianlai.renderer import render_to_wav


ROOT = Path(__file__).resolve().parents[1]
RECORDER = ROOT / "乐器" / "管弦乐" / "木管组" / "竖笛"
BAGPIPE = ROOT / "乐器" / "世界乐器" / "风笛"
RECORDER_RECIPE = RECORDER / "预处理参数.json"
BAGPIPE_RECIPE = BAGPIPE / "预处理参数.json"
RECORDER_DERIVED = ROOT / "音源" / "派生" / "竖笛-v1"
BAGPIPE_DERIVED = ROOT / "音源" / "派生" / "风笛-v1"
pytestmark = pytest.mark.external_assets


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _spectral_centroid(path: Path, start: float, duration: float) -> float:
    audio, sample_rate = sf.read(
        str(path),
        dtype="float64",
        always_2d=True,
    )
    mono = np.mean(audio, axis=1)
    selected = mono[
        int(round(start * sample_rate)) : int(round((start + duration) * sample_rate))
    ]
    selected -= np.mean(selected)
    magnitude = np.abs(np.fft.rfft(selected * np.hanning(len(selected))))
    frequencies = np.fft.rfftfreq(len(selected), 1.0 / sample_rate)
    return float(np.sum(magnitude * frequencies) / np.sum(magnitude))


def _steady_rms_dbfs(path: Path, window: dict) -> float:
    audio, sample_rate = sf.read(
        str(path),
        dtype="float64",
        always_2d=True,
    )
    start = int(window.get("offset_frames", 0)) + int(
        round(float(window["start_seconds"]) * sample_rate)
    )
    end = start + int(round(float(window["duration_seconds"]) * sample_rate))
    rms = float(np.sqrt(np.mean(np.square(audio[start:end], dtype=np.float64))))
    return 20.0 * math.log10(rms)


_RESOURCES_AVAILABLE = all(
    path.is_file()
    for path in (
        ROOT
        / "音源"
        / "VCSL"
        / "Aerophones"
        / "Edge-blown Aerophones"
        / "Baroque Soprano Recorder - Sustain.sfz",
        ROOT / "音源" / "FreePats" / "Bagpipe" / "Bagpipe-20221204.sfz",
        RECORDER_DERIVED / "处理说明.json",
        BAGPIPE_DERIVED / "处理说明.json",
    )
)


@unittest.skipUnless(
    _RESOURCES_AVAILABLE,
    "VCSL/FreePats originals and their offline derived resources are required",
)
class DerivedRecorderBagpipeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        temporary = Path(cls.temporary.name)
        cls.rebuilt_recorder = build_derived_resources(
            RECORDER_RECIPE,
            output_root=temporary / "recorder",
        )
        cls.rebuilt_bagpipe = build_derived_resources(
            BAGPIPE_RECIPE,
            output_root=temporary / "bagpipe",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_offline_recipes_are_hash_locked_and_deterministic(self) -> None:
        frozen_recorder = _load(RECORDER_DERIVED / "处理说明.json")
        frozen_bagpipe = _load(BAGPIPE_DERIVED / "处理说明.json")
        for rebuilt, frozen, expected_count in (
            (self.rebuilt_recorder, frozen_recorder, 13),
            (self.rebuilt_bagpipe, frozen_bagpipe, 3),
        ):
            self.assertFalse(rebuilt["original_resources_modified"])
            self.assertFalse(rebuilt["runtime_processing"])
            self.assertEqual(len(rebuilt["audio_outputs"]), expected_count)
            self.assertEqual(
                rebuilt["recipe_sha256"],
                frozen["recipe_sha256"],
            )
            self.assertEqual(
                {
                    name: item["output_sha256"]
                    for name, item in rebuilt["audio_outputs"].items()
                },
                {
                    name: item["output_sha256"]
                    for name, item in frozen["audio_outputs"].items()
                },
            )
            self.assertEqual(rebuilt["text_outputs"], frozen["text_outputs"])

        for recipe_path in (RECORDER_RECIPE, BAGPIPE_RECIPE):
            recipe = _load(recipe_path)
            for job in recipe["audio_jobs"]:
                self.assertEqual(
                    sha256_file(ROOT / job["source"]),
                    job["sha256"],
                )

    @unittest.skipUnless(os.name == "nt", "Windows extended-length path contract")
    def test_both_derived_families_build_below_a_long_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            short_root = Path(temporary)
            long_container = short_root / "long-derived-root"
            long_root = long_container.joinpath(
                *(
                    f"parent-{index:02d}-abcdefghijklmnop"
                    for index in range(9)
                )
            )
            recorder_root = long_root / "recorder"
            bagpipe_root = long_root / "bagpipe"
            self.assertGreater(len(str(recorder_root)), 260)
            try:
                recorder = build_derived_resources(
                    RECORDER_RECIPE,
                    output_root=recorder_root,
                )
                bagpipe = build_derived_resources(
                    BAGPIPE_RECIPE,
                    output_root=bagpipe_root,
                )
                self.assertEqual(len(recorder["audio_outputs"]), 13)
                self.assertEqual(len(bagpipe["audio_outputs"]), 3)
                self.assertTrue(
                    restore_module._path_is_plain_file(
                        recorder_root / "处理说明.json"
                    )
                )
                self.assertTrue(
                    restore_module._path_is_plain_file(
                        bagpipe_root / "处理说明.json"
                    )
                )
            finally:
                if restore_module._path_exists(long_container):
                    shutil.rmtree(
                        restore_module._windows_extended_path(long_container)
                    )

    def test_recorder_sustain_is_centered_and_steady_level_matched(self) -> None:
        receipt = _load(RECORDER_DERIVED / "处理说明.json")
        self.assertEqual(len(receipt["audio_outputs"]), 13)
        spectrally_repaired = set()
        for relative, record in receipt["audio_outputs"].items():
            path = RECORDER_DERIVED / relative
            info = sf.info(str(path))
            self.assertEqual(info.samplerate, 48_000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(info.subtype, "PCM_24")
            audio, _ = sf.read(str(path), dtype="float64", always_2d=True)
            self.assertTrue(np.array_equal(audio[:, 0], audio[:, 1]))
            self.assertAlmostEqual(
                _steady_rms_dbfs(path, record["steady_window"]),
                -20.0,
                places=3,
            )
            if record["spectral_transitions"]:
                spectrally_repaired.add(Path(relative).stem)
        self.assertEqual(
            spectrally_repaired,
            {
                "SopRecorder_Sus_E5_rr1_Main",
                "SopRecorder_Sus_F#5_rr1_Main",
                "SopRecorder_Sus_A#5_rr1_Main",
                "SopRecorder_Sus_C6_rr1_Main",
            },
        )

    def test_bagpipe_default_range_is_chanter_and_drones_are_independent(self) -> None:
        manifest = _load(BAGPIPE / "乐器.json")
        instrument = create_instrument(
            manifest,
            48_000,
            base_directory=str(BAGPIPE),
        )
        self.assertEqual(instrument.default_articulation, "chanter")
        self.assertEqual(
            instrument.articulation_playable_ranges,
            {
                "chanter": ((64.0, 81.0),),
                "drone_low": ((43.0, 43.0),),
                "drone_high": ((55.0, 55.0),),
            },
        )
        self.assertEqual(
            manifest["playable_ranges"],
            [[43, 43], [55, 55], [64, 81]],
        )

    def test_bagpipe_f4_g4_derived_seam_is_level_and_spectrum_matched(self) -> None:
        receipt = _load(BAGPIPE_DERIVED / "处理说明.json")
        paths = {
            name: BAGPIPE_DERIVED / "samples" / f"{name}.wav"
            for name in ("F4_31", "G4_31", "G4_32")
        }
        levels = {
            name: _steady_rms_dbfs(
                path,
                receipt["audio_outputs"][f"samples/{name}.wav"]["steady_window"],
            )
            for name, path in paths.items()
        }
        self.assertLess(max(levels.values()) - min(levels.values()), 0.01)
        centroids = {
            name: _spectral_centroid(path, 1.0, 1.5)
            for name, path in paths.items()
        }
        self.assertLess(centroids["G4_31"] / centroids["F4_31"], 1.08)
        self.assertLess(centroids["G4_32"] / centroids["F4_31"], 1.14)

    def test_both_manifests_render_short_isolated_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            cases = (
                (
                    RECORDER / "乐器.json",
                    {
                        "sample_rate": 48000,
                        "channels": 2,
                        "tail_seconds": 0.5,
                        "tuning": {"temperament": "equal", "a4_hz": 440.0},
                        "events": [
                            {"time": 0.0, "type": "articulation", "name": "sustain"},
                            {
                                "time": 0.0,
                                "type": "note_on",
                                "note_id": 1,
                                "midi_note": 88,
                                "velocity": 0.72,
                            },
                            {
                                "time": 0.45,
                                "type": "note_off",
                                "note_id": 1,
                                "release_velocity": 0.5,
                            },
                            {
                                "time": 0.8,
                                "type": "note_on",
                                "note_id": 2,
                                "midi_note": 96,
                                "velocity": 0.72,
                            },
                            {
                                "time": 1.25,
                                "type": "note_off",
                                "note_id": 2,
                                "release_velocity": 0.5,
                            },
                        ],
                    },
                    "recorder",
                ),
                (
                    BAGPIPE / "乐器.json",
                    {
                        "sample_rate": 48000,
                        "channels": 2,
                        "tail_seconds": 0.8,
                        "tuning": {"temperament": "equal", "a4_hz": 440.0},
                        "events": [
                            {"time": 0.0, "type": "articulation", "name": "drone_low"},
                            {
                                "time": 0.0,
                                "type": "note_on",
                                "note_id": 1,
                                "midi_note": 43,
                                "velocity": 0.72,
                            },
                            {
                                "time": 0.02,
                                "type": "articulation",
                                "name": "drone_high",
                            },
                            {
                                "time": 0.02,
                                "type": "note_on",
                                "note_id": 2,
                                "midi_note": 55,
                                "velocity": 0.72,
                            },
                            {"time": 0.04, "type": "articulation", "name": "chanter"},
                            {
                                "time": 0.08,
                                "type": "note_on",
                                "note_id": 3,
                                "midi_note": 65,
                                "velocity": 0.72,
                            },
                            {
                                "time": 0.5,
                                "type": "note_off",
                                "note_id": 3,
                                "release_velocity": 0.5,
                            },
                            {
                                "time": 0.62,
                                "type": "note_on",
                                "note_id": 4,
                                "midi_note": 66,
                                "velocity": 0.72,
                            },
                            {
                                "time": 1.04,
                                "type": "note_off",
                                "note_id": 4,
                                "release_velocity": 0.5,
                            },
                            {
                                "time": 1.2,
                                "type": "note_off",
                                "note_id": 1,
                                "release_velocity": 0.5,
                            },
                            {
                                "time": 1.2,
                                "type": "note_off",
                                "note_id": 2,
                                "release_velocity": 0.5,
                            },
                        ],
                    },
                    "bagpipe",
                ),
            )
            for manifest, document, stem in cases:
                events = directory / f"{stem}.events.json"
                wav = directory / f"{stem}.wav"
                events.write_text(
                    json.dumps(document, ensure_ascii=False),
                    encoding="utf-8",
                )
                render_to_wav(manifest, events, wav)
                audio, sample_rate = sf.read(
                    str(wav),
                    dtype="float64",
                    always_2d=True,
                )
                self.assertEqual(sample_rate, 48_000)
                self.assertTrue(np.all(np.isfinite(audio)))
                peak = float(np.max(np.abs(audio)))
                self.assertGreater(peak, 0.01)
                self.assertLess(peak, 1.0)


if __name__ == "__main__":
    unittest.main()
