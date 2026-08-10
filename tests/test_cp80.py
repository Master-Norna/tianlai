import hashlib
import json
from pathlib import Path
import unittest

import numpy as np
import pytest

from tianlai.dedicated_candidates import dedicated_manifest_sources
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
KEYBOARD_ROOT = ROOT / "乐器" / "键盘乐器"
INSTRUMENTS = ("电钢琴", "合唱电钢琴")
UPSTREAM_COMMIT = "8c3e581acda3594b553948ff0222d4f84a698376"
UPSTREAM_ORIGIN = "https://github.com/sfzinstruments/GregSullivan.E-Pianos"
CP80_SFZ_SHA256 = "4c9fa22ddebcc56a026e711c0d6a4eef7a20c0905f7c6f482466f040a4fa9c3f"
LICENSE_SHA256 = "e6bc9e9c474700b708f568bac9e5a8a9bcb2b1dad53442f5ba449fcb848b8e76"
README_SHA256 = "9f9a7d4b205abb9c2fd2d03012e56c7b25aef552ea022bfecb9009febaa1a4de"
SAMPLE_SET_SHA256 = "abbb9b2f9f3ecdb50ac39b6d3f15ad068fc2d09592864deeee1cf857b8d174da"
SAMPLE_BYTES = 11_003_179


def load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"JSON root is not an object: {path}")
    return document


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GregSullivanCp80Tests(unittest.TestCase):
    def test_both_instruments_pin_the_same_attributable_source(self) -> None:
        for instrument_name in INSTRUMENTS:
            with self.subTest(instrument=instrument_name):
                manifest = load_json(
                    KEYBOARD_ROOT / instrument_name / "乐器.json"
                )
                self.assertEqual(manifest["license"], "CC-BY-3.0")
                self.assertEqual(manifest["origin"], UPSTREAM_ORIGIN)
                self.assertEqual(manifest["upstream_version"], UPSTREAM_COMMIT)
                self.assertEqual(
                    manifest["asset_root"],
                    "../../../音源/GregSullivan.E-Pianos",
                )
                self.assertEqual(manifest["note_min"], 21)
                self.assertEqual(manifest["note_max"], 108)
                self.assertEqual(
                    manifest["articulations"],
                    {"normal": "CP80/CP80.sfz"},
                )
                self.assertEqual(
                    manifest["evidence_files"],
                    ["LICENSE", "README.md"],
                )

    def test_installer_pins_evidence_and_uses_a_non_merging_final_move(
        self,
    ) -> None:
        installer = KEYBOARD_ROOT / "获取GregSullivan电钢琴音源.ps1"
        raw = installer.read_bytes()
        text = raw.decode("ascii")
        for expected in (
            UPSTREAM_COMMIT,
            CP80_SFZ_SHA256.upper(),
            LICENSE_SHA256.upper(),
            README_SHA256.upper(),
            SAMPLE_SET_SHA256.upper(),
            f"$sampleBytes = [Int64]{SAMPLE_BYTES}",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        self.assertIn("[IO.Directory]::Move(", text)
        self.assertNotIn(
            "Move-Item -LiteralPath $stage -Destination $target",
            text,
        )

    @pytest.mark.external_assets
    def test_installed_files_match_every_frozen_installer_hash(self) -> None:
        manifest_path = KEYBOARD_ROOT / "电钢琴" / "乐器.json"
        manifest = load_json(manifest_path)
        asset_root = (
            manifest_path.parent / str(manifest["asset_root"])
        ).resolve()
        if not asset_root.is_dir():
            self.skipTest(f"CP80 resource is not installed: {asset_root}")

        report = load_json(manifest_path.parent / "资源核验.json")
        self.assertEqual(
            file_sha256(asset_root / "CP80" / "CP80.sfz"),
            CP80_SFZ_SHA256,
        )
        self.assertEqual(file_sha256(asset_root / "LICENSE"), LICENSE_SHA256)
        self.assertEqual(file_sha256(asset_root / "README.md"), README_SHA256)

        samples = sorted((asset_root / "CP80" / "Samples").glob("*.flac"))
        self.assertEqual(len(samples), 81)
        self.assertEqual(sum(path.stat().st_size for path in samples), SAMPLE_BYTES)
        records = "".join(
            f"{file_sha256(path)}  CP80/Samples/{path.name}\n"
            for path in samples
        )
        aggregate = hashlib.sha256(records.encode("utf-8")).hexdigest()
        self.assertEqual(aggregate, SAMPLE_SET_SHA256)
        self.assertEqual(report["sample_set_sha256"], SAMPLE_SET_SHA256)

    @pytest.mark.external_assets
    def test_real_mapping_has_four_exclusive_velocity_layers(self) -> None:
        manifest_path = KEYBOARD_ROOT / "电钢琴" / "乐器.json"
        manifest = load_json(manifest_path)
        asset_root = (
            manifest_path.parent / str(manifest["asset_root"])
        ).resolve()
        if not asset_root.is_dir():
            self.skipTest(f"CP80 resource is not installed: {asset_root}")

        inventory = dedicated_manifest_sources(manifest_path)
        regions = inventory["articulations"]["normal"]["attack_regions"]
        self.assertEqual(len(regions), 81)
        self.assertEqual(len({item["sample"] for item in regions}), 81)
        self.assertEqual(min(item["key_min"] for item in regions), 21.0)
        self.assertEqual(max(item["key_max"] for item in regions), 108.0)
        maximum_stretch = max(
            max(
                abs(item["key_min"] - item["root_midi"]),
                abs(item["key_max"] - item["root_midi"]),
            )
            for item in regions
        )
        self.assertEqual(maximum_stretch, 6.0)

        for velocity, layer in (
            (0.20, "-PP."),
            (0.50, "-MP."),
            (0.72, "-F."),
            (0.95, "-FF."),
        ):
            matches = [
                item
                for item in regions
                if item["key_min"] <= 60 <= item["key_max"]
                and item["velocity_min"] <= velocity <= item["velocity_max"]
            ]
            with self.subTest(velocity=velocity):
                self.assertEqual(len(matches), 1)
                self.assertIn(layer, Path(matches[0]["sample"]).name)

    @pytest.mark.external_assets
    def test_real_cp80_bandlimited_replay_preserves_selection_and_onset(
        self,
    ) -> None:
        manifest_path = KEYBOARD_ROOT / "电钢琴" / "乐器.json"
        manifest = load_json(manifest_path)
        asset_root = (
            manifest_path.parent / str(manifest["asset_root"])
        ).resolve()
        if not asset_root.is_dir():
            self.skipTest(f"CP80 resource is not installed: {asset_root}")

        sample_rate = 48_000
        frame_count = sample_rate // 10
        onset_frame_count = sample_rate // 20
        one_db_low = 10.0 ** (-1.0 / 20.0)
        one_db_high = 10.0 ** (1.0 / 20.0)
        tuning = EqualTemperament()
        probes = (
            ("range-A0", 21, 0.72),
            ("range-F#1", 30, 0.72),
            ("range-C4", 60, 0.72),
            ("range-C8", 108, 0.72),
            ("velocity-PP", 66, 0.20),
            ("velocity-MP", 66, 0.50),
            ("velocity-F", 66, 0.72),
            ("velocity-FF", 66, 0.95),
        )

        def render_probe(
            quality: str,
            midi_note: int,
            velocity: float,
        ) -> tuple[np.ndarray, tuple[tuple[int, str, str], ...]]:
            runtime_manifest = dict(manifest)
            runtime_manifest["resampling_quality"] = quality
            instrument = create_instrument(
                runtime_manifest,
                sample_rate,
                base_directory=str(manifest_path.parent),
            )
            try:
                instrument.handle_event(
                    PerformanceEvent(
                        0,
                        0,
                        "note_on",
                        {
                            "note_id": 1,
                            "midi_note": midi_note,
                            "velocity": velocity,
                        },
                    ),
                    tuning,
                )
                route = instrument.routes[1]
                articulation = instrument.articulations[route.articulation]
                selection = tuple(
                    (
                        voice.layer_index,
                        articulation.attack_layers[
                            voice.layer_index
                        ].engine.voices[
                            voice.internal_note_id
                        ].region.stable_key,
                        articulation.attack_layers[
                            voice.layer_index
                        ].engine.voices[
                            voice.internal_note_id
                        ].region.path.as_posix(),
                    )
                    for voice in route.voices
                )
                frames = np.array(
                    [instrument.render_frame() for _ in range(frame_count)],
                    dtype=np.float64,
                )
                return frames, selection
            finally:
                close = getattr(instrument, "close", None)
                if callable(close):
                    close()

        def rms(values: np.ndarray) -> float:
            return float(np.sqrt(np.mean(np.square(values))))

        def first_one_percent_crossing(values: np.ndarray) -> int:
            absolute = np.abs(values)
            threshold = float(np.max(absolute)) * 0.01
            matches = np.flatnonzero(absolute >= threshold)
            self.assertGreater(matches.size, 0)
            return int(matches[0])

        for label, midi_note, velocity in probes:
            with self.subTest(
                probe=label,
                midi_note=midi_note,
                velocity=velocity,
            ):
                linear, linear_selection = render_probe(
                    "linear",
                    midi_note,
                    velocity,
                )
                bandlimited, bandlimited_selection = render_probe(
                    "bandlimited",
                    midi_note,
                    velocity,
                )

                self.assertEqual(linear_selection, bandlimited_selection)
                self.assertTrue(np.all(np.isfinite(linear)))
                self.assertTrue(np.all(np.isfinite(bandlimited)))
                self.assertFalse(np.array_equal(linear, bandlimited))

                linear_mono = linear[:, 0]
                bandlimited_mono = bandlimited[:, 0]
                correlation = float(
                    np.corrcoef(linear_mono, bandlimited_mono)[0, 1]
                )
                self.assertTrue(np.isfinite(correlation))
                self.assertGreaterEqual(correlation, 0.99)

                linear_rms = rms(linear_mono)
                bandlimited_rms = rms(bandlimited_mono)
                linear_peak = float(np.max(np.abs(linear_mono)))
                bandlimited_peak = float(np.max(np.abs(bandlimited_mono)))
                self.assertGreater(linear_rms, 0.0)
                self.assertGreater(linear_peak, 0.0)
                self.assertGreaterEqual(
                    bandlimited_rms / linear_rms,
                    one_db_low,
                )
                self.assertLessEqual(
                    bandlimited_rms / linear_rms,
                    one_db_high,
                )
                self.assertGreaterEqual(
                    bandlimited_peak / linear_peak,
                    one_db_low,
                )
                self.assertLessEqual(
                    bandlimited_peak / linear_peak,
                    one_db_high,
                )

                linear_onset_rms = rms(
                    linear_mono[:onset_frame_count]
                )
                bandlimited_onset_rms = rms(
                    bandlimited_mono[:onset_frame_count]
                )
                self.assertGreater(linear_onset_rms, 0.0)
                self.assertGreaterEqual(
                    bandlimited_onset_rms / linear_onset_rms,
                    one_db_low,
                )
                self.assertLessEqual(
                    bandlimited_onset_rms / linear_onset_rms,
                    one_db_high,
                )

                self.assertLessEqual(
                    abs(
                        first_one_percent_crossing(bandlimited_mono)
                        - first_one_percent_crossing(linear_mono)
                    ),
                    2,
                )
                self.assertLessEqual(
                    abs(
                        int(np.argmax(np.abs(bandlimited_mono)))
                        - int(np.argmax(np.abs(linear_mono)))
                    ),
                    2,
                )

    def test_frozen_reports_match_the_installed_cp80(self) -> None:
        for instrument_name in INSTRUMENTS:
            directory = KEYBOARD_ROOT / instrument_name
            with self.subTest(instrument=instrument_name):
                resource = load_json(directory / "资源核验.json")
                pitch = load_json(directory / "音准校准.json")
                audition = load_json(directory / "试听核验.json")

                self.assertEqual(resource["sample_count"], 81)
                self.assertEqual(resource["region_count"], 81)
                self.assertEqual(
                    resource["articulations"]["normal"]["looped_regions"],
                    0,
                )
                self.assertEqual(pitch["summary"]["sample_count"], 81)
                self.assertLess(
                    abs(pitch["summary"]["median_residual_cents"]),
                    1.0,
                )
                self.assertEqual(audition["clipped_samples"], 0)
                self.assertGreater(audition["rms"], 0.01)

    @pytest.mark.external_assets
    @pytest.mark.listening
    def test_chorus_entry_is_stereo_and_deterministic(self) -> None:
        manifest_path = KEYBOARD_ROOT / "合唱电钢琴" / "乐器.json"
        manifest = load_json(manifest_path)
        asset_root = (
            manifest_path.parent / str(manifest["asset_root"])
        ).resolve()
        if not asset_root.is_dir():
            self.skipTest(f"CP80 resource is not installed: {asset_root}")

        tuning = EqualTemperament()

        def render_once() -> np.ndarray:
            instrument = create_instrument(
                manifest,
                16_000,
                base_directory=str(manifest_path.parent),
            )
            instrument.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 60, "velocity": 0.72},
                ),
                tuning,
            )
            frames = np.array(
                [instrument.render_frame() for _ in range(8_192)],
                dtype=np.float64,
            )
            close = getattr(instrument, "close", None)
            if callable(close):
                close()
            return frames

        first = render_once()
        second = render_once()
        self.assertTrue(np.array_equal(first, second))
        self.assertGreater(
            float(np.sqrt(np.mean(np.square(first[:, 0] - first[:, 1])))),
            1.0e-4,
        )


if __name__ == "__main__":
    unittest.main()
