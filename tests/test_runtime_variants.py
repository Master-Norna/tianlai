from __future__ import annotations

import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from tianlai.runtime_variants import (
    RuntimeVariantError,
    capture_runtime_variants,
    current_runtime_variant_capture,
)
from tianlai.sampler import SampleInstrument


def _write_sample(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(
            b"".join(
                struct.pack("<h", value)
                for value in (0, 1_000, -1_000, 500, -500, 0, 250, -250)
            )
        )


def _instrument(
    directory: Path,
    regions: list[dict],
    *,
    runtime_component: str | None = None,
) -> SampleInstrument:
    for region in regions:
        path = directory / str(region["sample"])
        if not path.exists():
            _write_sample(path)
    manifest: dict = {"regions": regions}
    if runtime_component is not None:
        manifest["runtime_component"] = runtime_component
    return SampleInstrument.from_manifest(
        manifest,
        8_000,
        base_directory=str(directory),
    )


def _select(
    instrument: SampleInstrument,
    *,
    random_value: float = 0.5,
) -> str:
    return instrument._select_region(
        440.0,
        0.5,
        target_midi=69.0,
        random_value=random_value,
    ).path.name


def _catalog(receipt: dict, selection_index: int = 0) -> dict:
    catalog_sha256 = receipt["selections"][selection_index][
        "catalog_sha256"
    ]
    catalogs = {
        item["catalog_sha256"]: item["catalog"]
        for item in receipt["catalogs"]
    }
    return catalogs[catalog_sha256]


class RuntimeVariantCaptureTests(unittest.TestCase):
    def test_plain_round_robin_sequence_is_unchanged_by_capture(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tianlai_runtime_variants_rr_"
        ) as temporary:
            directory = Path(temporary)
            regions = [
                {"sample": "b.wav", "root_pitch_hz": 440.0},
                {"sample": "a.wav", "root_pitch_hz": 440.0},
            ]
            ordinary = _instrument(directory, regions)
            observed = _instrument(directory, regions)

            self.assertIsNone(current_runtime_variant_capture())
            ordinary_sequence = [_select(ordinary) for _ in range(4)]
            with capture_runtime_variants() as capture:
                self.assertIs(current_runtime_variant_capture(), capture)
                observed_sequence = [_select(observed) for _ in range(4)]
            self.assertIsNone(current_runtime_variant_capture())

            self.assertEqual(ordinary_sequence, ["a.wav", "b.wav"] * 2)
            self.assertEqual(observed_sequence, ordinary_sequence)
            self.assertEqual(capture.selection_count, 4)
            receipt = capture.receipt()
            self.assertEqual(
                len(
                    {
                        selection["choice_sha256"]
                        for selection in receipt["selections"]
                    }
                ),
                2,
            )
            self.assertFalse(
                receipt["all_conditions_deterministic_single"]
            )
            # Leaving the context prevents later ordinary selections from
            # mutating the completed receipt.
            _select(observed)
            self.assertEqual(capture.selection_count, 4)
            first_selection = receipt["selections"][0]
            first_catalog = _catalog(receipt)
            with self.assertRaisesRegex(RuntimeVariantError, "sealed"):
                capture.record_selection(
                    catalog=first_catalog,
                    choice_sha256=first_selection["choice_sha256"],
                    actual_selector=first_selection["actual_selector"],
                )

    def test_equal_candidates_without_rr_metadata_are_enumerated(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tianlai_runtime_variants_tie_"
        ) as temporary:
            directory = Path(temporary)
            instrument = _instrument(
                directory,
                [
                    {"sample": "first.wav", "root_pitch_hz": 440.0},
                    {"sample": "second.wav", "root_pitch_hz": 440.0},
                ],
            )
            with capture_runtime_variants() as capture:
                _select(instrument)
            receipt = capture.receipt()
            catalog = _catalog(receipt)

            self.assertEqual(len(catalog["choices"]), 2)
            self.assertTrue(
                all(
                    len(partition["choice_sha256s"]) == 2
                    for partition in catalog["partitions"]
                )
            )
            self.assertFalse(catalog["deterministic_single"])
            self.assertEqual(
                receipt["selections"][0]["actual_selector"][
                    "candidate_count"
                ],
                2,
            )

    def test_explicit_rr_position_controls_actual_and_catalog_order(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tianlai_runtime_variants_rr_order_"
        ) as temporary:
            directory = Path(temporary)
            regions = [
                {
                    "sample": "a_rr2.wav",
                    "root_pitch_hz": 440.0,
                    "round_robin_position": 2,
                    "round_robin_length": 2,
                },
                {
                    "sample": "z_rr1.wav",
                    "root_pitch_hz": 440.0,
                    "round_robin_position": 1,
                    "round_robin_length": 2,
                },
            ]
            ordinary = _instrument(directory, regions)
            instrument = _instrument(directory, regions)
            self.assertEqual(
                [_select(ordinary), _select(ordinary)],
                ["z_rr1.wav", "a_rr2.wav"],
            )

            with capture_runtime_variants() as capture:
                selected_names = [_select(instrument), _select(instrument)]
            receipt = capture.receipt()
            catalog = _catalog(receipt)
            middle = next(
                partition
                for partition in catalog["partitions"]
                if partition["kind"] == "open_interval"
            )
            selected_hashes = [
                selection["choice_sha256"]
                for selection in receipt["selections"]
            ]

            self.assertEqual(selected_names, ["z_rr1.wav", "a_rr2.wav"])
            self.assertEqual(
                selected_hashes,
                middle["choice_sha256s"],
            )
            self.assertEqual(
                [
                    selection["actual_selector"]["candidate_index"]
                    for selection in receipt["selections"]
                ],
                [0, 1],
            )

    def test_random_selector_catalog_covers_points_intervals_and_gap(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tianlai_runtime_variants_random_"
        ) as temporary:
            directory = Path(temporary)
            instrument = _instrument(
                directory,
                [
                    {
                        "sample": "low.wav",
                        "root_pitch_hz": 440.0,
                        "random_min": 0.0,
                        "random_max": 0.25,
                    },
                    {
                        "sample": "middle.wav",
                        "root_pitch_hz": 440.0,
                        "random_min": 0.25,
                        "random_max": 0.5,
                    },
                    {
                        "sample": "high.wav",
                        "root_pitch_hz": 440.0,
                        "random_min": 0.75,
                        "random_max": 1.0,
                    },
                ],
            )
            with capture_runtime_variants() as capture:
                self.assertEqual(
                    _select(instrument, random_value=0.1),
                    "low.wav",
                )
            catalog = _catalog(capture.receipt())
            points = {
                partition["value"]: partition
                for partition in catalog["partitions"]
                if partition["kind"] == "point"
            }
            intervals = {
                (partition["minimum"], partition["maximum"]): partition
                for partition in catalog["partitions"]
                if partition["kind"] == "open_interval"
            }

            self.assertEqual(len(points[0.25]["choice_sha256s"]), 2)
            self.assertEqual(points[0.25]["status"], "choices")
            self.assertEqual(
                intervals[(0.5, 0.75)]["status"],
                "gap",
            )
            self.assertEqual(
                intervals[(0.5, 0.75)]["choice_sha256s"],
                [],
            )
            self.assertTrue(catalog["has_selector_gaps"])
            self.assertEqual(len(catalog["choices"]), 3)
            self.assertFalse(catalog["deterministic_single"])

    def test_jitter_domains_block_deterministic_single(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tianlai_runtime_variants_jitter_"
        ) as temporary:
            directory = Path(temporary)
            jittered = _instrument(
                directory,
                [
                    {
                        "sample": "jittered.wav",
                        "root_pitch_hz": 440.0,
                        "pitch_random_cents": 4.0,
                        "amplitude_random_db": 1.5,
                        "delay_random_seconds": 0.003,
                    }
                ],
            )
            deterministic = _instrument(
                directory,
                [{"sample": "single.wav", "root_pitch_hz": 440.0}],
            )
            with capture_runtime_variants() as jitter_capture:
                _select(jittered)
            with capture_runtime_variants() as deterministic_capture:
                _select(deterministic)

            jitter_receipt = jitter_capture.receipt()
            jitter_catalog = _catalog(jitter_receipt)
            self.assertEqual(
                {
                    domain["domain"]
                    for domain in jitter_catalog["unexhausted_domains"]
                },
                {
                    "pitch_jitter_cents",
                    "amplitude_jitter_db",
                    "delay_jitter_seconds",
                },
            )
            self.assertFalse(jitter_catalog["deterministic_single"])
            self.assertFalse(
                jitter_receipt[
                    "all_conditions_deterministic_single"
                ]
            )

            deterministic_receipt = deterministic_capture.receipt()
            self.assertTrue(
                _catalog(deterministic_receipt)["deterministic_single"]
            )
            self.assertTrue(
                deterministic_receipt[
                    "all_conditions_deterministic_single"
                ]
            )

    def test_all_nested_sample_components_emit_selections(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="tianlai_runtime_variants_layers_"
        ) as temporary:
            directory = Path(temporary)
            main = _instrument(
                directory,
                [{"sample": "main.wav", "root_pitch_hz": 440.0}],
                runtime_component="main",
            )
            resonance = _instrument(
                directory,
                [{"sample": "resonance.wav", "root_pitch_hz": 440.0}],
                runtime_component="resonance",
            )
            with capture_runtime_variants() as capture:
                _select(main)
                _select(resonance)
            receipt = capture.receipt()

            self.assertEqual(receipt["selection_count"], 2)
            self.assertEqual(len(receipt["catalogs"]), 2)
            self.assertEqual(
                len(
                    {
                        selection["component_sha256"]
                        for selection in receipt["selections"]
                    }
                ),
                2,
            )

    def test_hashes_are_stable_across_roots_and_hide_absolute_paths(self) -> None:
        receipts: list[dict] = []
        roots: list[str] = []
        for prefix in (
            "tianlai_runtime_variants_portable_a_",
            "tianlai_runtime_variants_portable_b_",
        ):
            with tempfile.TemporaryDirectory(prefix=prefix) as temporary:
                directory = Path(temporary)
                roots.append(str(directory.resolve()))
                instrument = _instrument(
                    directory,
                    [{"sample": "same.wav", "root_pitch_hz": 440.0}],
                )
                with capture_runtime_variants() as capture:
                    _select(instrument, random_value=0.2)
                receipts.append(capture.receipt())

        first, second = receipts
        for field in (
            "component_sha256",
            "condition_sha256",
            "choice_sha256",
            "catalog_sha256",
        ):
            self.assertEqual(
                first["selections"][0][field],
                second["selections"][0][field],
            )
        self.assertEqual(first["receipt_sha256"], second["receipt_sha256"])
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertTrue(all(root not in serialized for root in roots))
        self.assertNotIn(".wav", serialized)


if __name__ == "__main__":
    unittest.main()
