from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parents[1]
TUNING_SCRIPT = (
    ROOT / "乐器" / "键盘乐器" / "击弦古钢琴" / "校准SIMPK音源.py"
)
SPEC = importlib.util.spec_from_file_location(
    "tianlai_simpk_clavichord_tuning_test_target",
    TUNING_SCRIPT,
)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"cannot import SIMPK tuning script: {TUNING_SCRIPT}")
TUNING = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = TUNING
SPEC.loader.exec_module(TUNING)


VELOCITY_LAYERS = ((0, 40), (41, 109), (110, 127))
GROUPS = (
    {
        "trigger": "attack",
        "enabled": "true",
        "loopEnabled": "false",
        "start": "0",
        "release": "4",
        "ampVelTrack": "1",
        "tags": "reso",
    },
    {
        "trigger": "release",
        "enabled": "false",
        "loopEnabled": "false",
        "start": "0",
        "release": "0",
        "ampVelTrack": "1",
        "tags": "reso",
    },
    {
        "trigger": "attack",
        "enabled": "true",
        "loopEnabled": "false",
        "start": "0",
        "release": "4",
        "ampVelTrack": "1",
        "tags": "lupe",
    },
    {
        "trigger": "release",
        "enabled": "false",
        "loopEnabled": "false",
        "start": "0",
        "release": "0",
        "ampVelTrack": "0",
        "tags": "lupe",
    },
)


def midi_note_name(note: int) -> str:
    names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
    return f"{names[note % 12]}{note // 12 - 1}"


def sample_path(
    timbre: str,
    note: int,
    layer_index: int,
    round_robin: int,
) -> str:
    return (
        f"assets/wav/{timbre}/{note}_{midi_note_name(note)}_"
        f"{layer_index:02d}_{round_robin - 1:02d}.wav"
    )


def write_strict_synthetic_preset(root: Path) -> tuple[object, ...]:
    """Write a tiny-on-disk but structurally complete source XML fixture."""

    document = ET.Element("DecentSampler")
    groups = ET.SubElement(document, "groups", {"seqMode": "round_robin"})
    attack_end = 86_400
    for group_attributes in GROUPS:
        group = ET.SubElement(groups, "group", group_attributes)
        trigger = group_attributes["trigger"]
        timbre = group_attributes["tags"]
        for note in range(40, 103):
            for layer_index, (velocity_low, velocity_high) in enumerate(
                VELOCITY_LAYERS
            ):
                for round_robin in (1, 2):
                    ET.SubElement(
                        group,
                        "sample",
                        {
                            "rootNote": str(note),
                            "loNote": str(note),
                            "hiNote": str(note),
                            "loVel": str(velocity_low),
                            "hiVel": str(velocity_high),
                            "seqPosition": str(round_robin),
                            "path": sample_path(
                                timbre, note, layer_index, round_robin
                            ),
                            "start": (
                                "0"
                                if trigger == "attack"
                                else str(attack_end - 1)
                            ),
                            "end": (
                                str(attack_end)
                                if trigger == "attack"
                                else str(attack_end + 1)
                            ),
                        },
                    )
    preset = root / "clavichord.dspreset"
    ET.ElementTree(document).write(
        preset,
        encoding="utf-8",
        xml_declaration=True,
    )
    records = TUNING._load_converter().parse_dspreset(preset)
    if len(records) != 1_512:
        raise AssertionError("synthetic strict preset did not produce 1512 records")
    return records


def write_tone(
    root: Path,
    relative_path: str,
    *,
    note: int,
    detune_cents: float,
    silent: bool = False,
) -> int:
    sample_rate = 24_000
    frame_count = round(1.8 * sample_rate)
    time = np.arange(frame_count, dtype="float64") / sample_rate
    if silent:
        audio = np.zeros(frame_count, dtype="float64")
    else:
        frequency = TUNING.midi_to_hz(note) * (
            2.0 ** (detune_cents / 1200.0)
        )
        phase = 2.0 * math.pi * frequency * time
        envelope = np.exp(-0.45 * time)
        audio = (
            envelope
            * (
                np.sin(phase)
                + 0.35 * np.sin(2.0 * phase)
                + 0.18 * np.sin(3.0 * phase)
            )
            * 0.4
        )
    destination = root / Path(*Path(relative_path).parts)
    destination.parent.mkdir(parents=True, exist_ok=True)
    sf.write(destination, audio, sample_rate, subtype="FLOAT")
    return frame_count


def attacks_from_records(
    records: tuple[object, ...],
    selected: dict[str, tuple[float, bool]],
    root: Path,
    *,
    byte_identical_fixture: bool = False,
) -> tuple[object, ...]:
    results = []
    canonical_path: Path | None = None
    canonical_frame_count: int | None = None
    canonical_specification: tuple[int, tuple[float, bool]] | None = None
    for record in records:
        if (
            getattr(record, "trigger") != "attack"
            or not getattr(record, "enabled")
            or getattr(record, "sample_path") not in selected
        ):
            continue
        relative_path = getattr(record, "sample_path")
        detune, silent = selected[relative_path]
        specification = (getattr(record, "root_note"), (detune, silent))
        if byte_identical_fixture and canonical_path is not None:
            if specification != canonical_specification:
                raise ValueError(
                    "byte-identical fixtures require one shared tone "
                    "specification"
                )
            destination = root / Path(*Path(relative_path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # FLOAT WAV files can carry a libsndfile PEAK write timestamp.
            # Copy one canonical file so this fixture is truly byte-identical.
            shutil.copyfile(canonical_path, destination)
            if canonical_frame_count is None:
                raise AssertionError("missing canonical fixture frame count")
            frame_count = canonical_frame_count
        else:
            frame_count = write_tone(
                root,
                relative_path,
                note=getattr(record, "root_note"),
                detune_cents=detune,
                silent=silent,
            )
            if byte_identical_fixture:
                canonical_path = root / Path(*Path(relative_path).parts)
                canonical_frame_count = frame_count
                canonical_specification = specification
        results.append(
            TUNING.AttackSample(
                sample_path=relative_path,
                root_note=getattr(record, "root_note"),
                velocity_low=getattr(record, "velocity_low"),
                velocity_high=getattr(record, "velocity_high"),
                round_robin_position=getattr(
                    record, "round_robin_position"
                ),
                timbre=getattr(record, "timbre"),
                offset_frames=0,
                end_frame_exclusive=frame_count,
            )
        )
    if len(results) != len(selected):
        raise AssertionError(
            f"selected {len(selected)} source records, found {len(results)}"
        )
    return tuple(results)


def fast_settings(*, peer_limit: float = 25.0):
    return TUNING.CalibrationSettings(
        source_root_note_offset=0,
        window_starts_seconds=(0.08, 0.32),
        maximum_frames=16_384,
        wide_cents_step=1.0,
        minimum_clear_windows=2,
        maximum_peer_deviation_cents=peer_limit,
    )


class SimpkClavichordTuningTests(unittest.TestCase):
    def test_strict_mode_rejects_converter_playback_offset_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with mock.patch.object(
                TUNING,
                "_load_converter",
                return_value=SimpleNamespace(PLAYBACK_NOTE_OFFSET=-11),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "playback-note offset drift",
                ):
                    TUNING.calibrate_simpk_source(
                        temporary,
                        settings=TUNING.CalibrationSettings(
                            source_root_note_offset=-12
                        ),
                    )

    def test_positive_and_negative_cents_make_exact_deterministic_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = write_strict_synthetic_preset(root)
            positive = sample_path("lupe", 69, 0, 1)
            negative = sample_path("reso", 70, 0, 1)
            attacks = attacks_from_records(
                records,
                {
                    positive: (14.0, False),
                    negative: (-17.0, False),
                },
                root,
            )
            calibration_path = root / "calibration.json"
            diagnostics_path = root / "diagnostics.json"

            calibration, diagnostics = TUNING.calibrate_simpk_source(
                root,
                calibration_path=calibration_path,
                diagnostics_path=diagnostics_path,
                settings=fast_settings(),
                expected_sample_count=2,
                expected_samples_per_note=1,
                attack_samples=attacks,
            )

            self.assertEqual(
                set(calibration),
                {"schema_version", "unit", "measured_detune_cents"},
            )
            self.assertEqual(calibration["schema_version"], 1)
            self.assertEqual(calibration["unit"], "cents")
            values = calibration["measured_detune_cents"]
            self.assertEqual(list(values), sorted(values))
            self.assertAlmostEqual(values[positive], 14.0, delta=2.0)
            self.assertAlmostEqual(values[negative], -17.0, delta=2.0)
            self.assertEqual(diagnostics["status"], "passed")
            for path in (positive, negative):
                sample = diagnostics["samples"][path]
                self.assertEqual(sample["status"], "accepted")
                self.assertGreater(sample["confidence"], 0.5)
                self.assertTrue(
                    all(
                        "wide_harmonic_residual_cents" in window
                        for window in sample["windows"]
                    )
                )
                self.assertTrue(
                    all(
                        window["local_autocorrelation_signal_strategy"]
                        == (
                            "median of a strict majority of non-boundary "
                            "channel/downmix estimates"
                        )
                        and all(
                            component["status"] == "accepted"
                            for component in window[
                                "local_autocorrelation_components"
                            ].values()
                        )
                        for window in sample["windows"]
                    )
                )
                self.assertIn("velocity_layer", sample)
                self.assertIn("round_robin_position", sample)

            converter = TUNING._load_converter()
            loaded = converter.load_tuning_table(
                calibration_path,
                expected_sample_paths={positive, negative},
            )
            self.assertEqual(loaded, values)
            first_payload = calibration_path.read_bytes()
            TUNING.calibrate_simpk_source(
                root,
                calibration_path=calibration_path,
                diagnostics_path=diagnostics_path,
                settings=fast_settings(),
                expected_sample_count=2,
                expected_samples_per_note=1,
                attack_samples=reversed(attacks),
            )
            self.assertEqual(calibration_path.read_bytes(), first_payload)

    def test_byte_identical_velocity_paths_share_a_stable_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = write_strict_synthetic_preset(root)
            selected = {
                sample_path("lupe", 69, layer_index, 1): (7.0, False)
                for layer_index in range(3)
            }
            attacks = attacks_from_records(
                records,
                selected,
                root,
                byte_identical_fixture=True,
            )
            calibration_path = root / "calibration.json"
            diagnostics_path = root / "diagnostics.json"

            calibration, diagnostics = TUNING.calibrate_simpk_source(
                root,
                calibration_path=calibration_path,
                diagnostics_path=diagnostics_path,
                settings=fast_settings(),
                expected_sample_count=3,
                expected_samples_per_note=3,
                attack_samples=attacks,
            )

            values = calibration["measured_detune_cents"]
            self.assertEqual(set(values), set(selected))
            self.assertEqual(len(set(values.values())), 1)
            uniqueness = diagnostics["source_audio_uniqueness"]
            self.assertEqual(uniqueness["mapped_attack_path_count"], 3)
            self.assertEqual(uniqueness["unique_audio_sha256_count"], 1)
            self.assertEqual(
                uniqueness[
                    "groups_byte_identical_across_all_three_velocity_zones"
                ],
                1,
            )
            self.assertEqual(
                uniqueness["effective_velocity_recording_layers"],
                1,
            )
            self.assertTrue(
                all(
                    record["consensus_window_count"] >= 2
                    and record["calibration_pitch_estimator"]
                    == "median of selected harmonic-constrained FFT detunes"
                    for record in diagnostics["samples"].values()
                )
            )

    def test_same_key_outlier_rejects_entire_formal_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = write_strict_synthetic_preset(root)
            selected = {
                sample_path("lupe", 69, 0, 1): (2.0, False),
                sample_path("lupe", 69, 1, 2): (3.0, False),
                sample_path("reso", 69, 0, 2): (4.0, False),
                sample_path("reso", 69, 1, 1): (55.0, False),
            }
            attacks = attacks_from_records(records, selected, root)
            calibration_path = root / "must-not-exist.json"
            diagnostics_path = root / "rejection.json"

            with self.assertRaisesRegex(
                TUNING.CalibrationRejectedError,
                "calibration rejected",
            ):
                TUNING.calibrate_simpk_source(
                    root,
                    calibration_path=calibration_path,
                    diagnostics_path=diagnostics_path,
                    settings=fast_settings(peer_limit=15.0),
                    expected_sample_count=4,
                    expected_samples_per_note=4,
                    attack_samples=attacks,
                )

            self.assertFalse(calibration_path.exists())
            diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
            self.assertEqual(diagnostics["status"], "failed")
            outlier = sample_path("reso", 69, 1, 1)
            self.assertEqual(
                diagnostics["notes"]["69"]["status"],
                "rejected_round_robin_disagreement",
            )
            self.assertGreater(
                abs(
                    diagnostics["notes"]["69"]["subgroup_cross_checks"][
                        "round_robin_within_timbre"
                    ]["reso"]["maximum_median_spread_cents"]
                ),
                40.0,
            )
            self.assertTrue(
                any(
                    failure["code"] == "round_robin_disagreement"
                    for failure in diagnostics["failures"]
                )
            )

    def test_no_clear_pitch_is_diagnostic_not_a_guessed_correction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            records = write_strict_synthetic_preset(root)
            silent_path = sample_path("lupe", 69, 0, 1)
            attacks = attacks_from_records(
                records,
                {silent_path: (0.0, True)},
                root,
            )
            calibration_path = root / "must-not-exist.json"
            diagnostics_path = root / "no-clear.json"

            with self.assertRaises(TUNING.CalibrationRejectedError):
                TUNING.calibrate_simpk_source(
                    root,
                    calibration_path=calibration_path,
                    diagnostics_path=diagnostics_path,
                    settings=fast_settings(),
                    expected_sample_count=1,
                    expected_samples_per_note=1,
                    attack_samples=attacks,
                )

            self.assertFalse(calibration_path.exists())
            diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
            record = diagnostics["samples"][silent_path]
            self.assertEqual(
                record["status"],
                "rejected_insufficient_clear_windows",
            )
            self.assertEqual(record["clear_window_count"], 0)
            self.assertEqual(
                [
                    window["status"]
                    for window in record["windows"]
                ],
                [
                    "analysis_error",
                    "analysis_error",
                ],
                record,
            )
            self.assertTrue(
                all(
                    "silent" in window["reason"]
                    for window in record["windows"]
                )
            )


if __name__ == "__main__":
    unittest.main()
