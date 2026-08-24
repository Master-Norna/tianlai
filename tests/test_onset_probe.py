from __future__ import annotations

import json
import copy
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import wave

import numpy as np

from tianlai.audio import read_wav_float
from tianlai.dedicated_sfz import (
    DedicatedSfzInstrument,
    DedicatedSfzRegionMetadata,
)
from tianlai.events import parse_performance_document
from tianlai.instrument import create_instrument
from tianlai.onset_evidence import (
    OnsetEvidenceError,
    canonical_sha256,
    create_review_draft,
    finalize_review,
    load_candidate_report,
    promote_review,
    record_review_decision,
    validate_candidate_report,
)
from tianlai.onset_probe import (
    ProbeSpec,
    REPORT_FILENAME,
    analyze_stereo_onset,
    build_probe_specs,
    run_probe_batch,
    select_probe_notes,
)
from tianlai.runtime_variants import (
    RuntimeVariantError,
    capture_runtime_variants,
    certify_deterministic_single_observation,
    certify_runtime_variant_observation,
    onset_sampled_condition,
    onset_sampled_condition_id,
    stable_variant_sha256,
    validate_runtime_variant_proof_document,
)
from tianlai.sampler import SampleInstrument


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"


def _write_minimal_sample(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(
            b"".join(
                struct.pack("<h", value)
                for value in (0, 1_200, -1_200, 600, -600, 0)
            )
        )


def _rehash_runtime_receipt(receipt: dict) -> None:
    receipt["receipt_sha256"] = stable_variant_sha256(
        "runtime-variant-selection-receipt-v1",
        {
            key: value
            for key, value in receipt.items()
            if key != "receipt_sha256"
        },
    )


def _rehash_first_runtime_catalog(receipt: dict) -> None:
    record = receipt["catalogs"][0]
    old_catalog_sha256 = record["catalog_sha256"]
    catalog = record["catalog"]
    catalog["condition_sha256"] = stable_variant_sha256(
        "sample-region-condition-v1",
        catalog["condition"],
    )
    new_catalog_sha256 = stable_variant_sha256(
        "runtime-variant-catalog-v1",
        catalog,
    )
    record["catalog_sha256"] = new_catalog_sha256
    for selection in receipt["selections"]:
        if selection["catalog_sha256"] == old_catalog_sha256:
            selection["catalog_sha256"] = new_catalog_sha256
            selection["condition_sha256"] = catalog["condition_sha256"]
    _rehash_runtime_receipt(receipt)


def _rehash_runtime_proof_outer_hashes(proof: dict) -> None:
    proof["top_level_contract_sha256"] = stable_variant_sha256(
        "top-level-runtime-variant-contract-v1",
        proof["top_level_contract"],
    )
    hash_kind = (
        "finite-rr-runtime-variant-proof-v1"
        if proof["kind"] == "finite_rr_runtime_variant_proof"
        else "deterministic-single-runtime-variant-proof-v1"
    )
    proof["proof_sha256"] = stable_variant_sha256(
        hash_kind,
        {
            key: value
            for key, value in proof.items()
            if key != "proof_sha256"
        },
    )


def _rehash_runtime_proof(proof: dict) -> None:
    contract = proof["top_level_contract"]
    attack = contract.get("attack_phase_contract")
    if attack is not None:
        bindings = attack["ordered_layer_bindings"]
        attack["retained_bundle_sha256"] = stable_variant_sha256(
            "dedicated-sfz-retained-attack-bundle-v1",
            [binding for binding in bindings if binding["route_retained"]],
        )
        attack["static_audio_bundle_sha256"] = stable_variant_sha256(
            "dedicated-sfz-static-audio-attack-bundle-v1",
            [
                binding
                for binding in bindings
                if binding["static_audio_contributing"]
            ],
        )
        cycle = contract.get("finite_rr_cycle_contract")
        if cycle is not None:
            cycle["slot_bundle_sha256"] = stable_variant_sha256(
                "dedicated-sfz-finite-rr-slot-bundle-v1",
                [
                    {
                        "layer_index": binding["layer_index"],
                        "choice_sha256": binding["choice_sha256"],
                        "route_retained": binding["route_retained"],
                        "wrapper_outcome_sha256": binding[
                            "wrapper_outcome_sha256"
                        ],
                    }
                    for binding in bindings
                ],
            )
            if proof["kind"] == "finite_rr_runtime_variant_proof":
                proof["slot_bundle_sha256"] = cycle["slot_bundle_sha256"]
    _rehash_runtime_proof_outer_hashes(proof)


class OnsetProbePureFunctionTests(unittest.TestCase):
    def test_probe_integer_inputs_reject_boolean_aliases(self) -> None:
        base = {
            "manifest_path": ROOT / "instruments" / "placeholder.json",
            "output_directory": OUTPUT / "placeholder",
            "articulation": "sustain",
            "midi_note": 60,
            "velocity": 80,
            "repeat_index": 0,
            "variation_slot": 0,
            "sample_rate": 8_000,
        }
        for field, value in (
            ("midi_note", True),
            ("velocity", True),
            ("repeat_index", False),
            ("variation_slot", False),
            ("sample_rate", True),
        ):
            with self.subTest(field=field):
                arguments = dict(base)
                arguments[field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*integer",
                ):
                    ProbeSpec(**arguments)

        for field, value in (
            ("pre_roll_seconds", True),
            ("note_seconds", True),
            ("tail_seconds", False),
        ):
            with self.subTest(numeric_field=field):
                arguments = dict(base)
                arguments[field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*finite number",
                ):
                    ProbeSpec(**arguments)

        frames = np.zeros((20, 2), dtype=np.float64)
        analysis_cases = (
            ("sample_rate", True),
            ("note_on_frame", False),
            ("note_off_frame", True),
        )
        for field, value in analysis_cases:
            with self.subTest(analysis_field=field):
                arguments = {
                    "sample_rate": 8_000,
                    "note_on_frame": 0,
                    "note_off_frame": 10,
                }
                arguments[field] = value
                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*integer",
                ):
                    analyze_stereo_onset(
                        frames,
                        arguments["sample_rate"],
                        arguments["note_on_frame"],
                        note_off_frame=arguments["note_off_frame"],
                    )

        for field in ("window_ms", "hop_ms"):
            with self.subTest(analysis_numeric_field=field):
                optional = {field: True}
                with self.assertRaisesRegex(
                    ValueError,
                    rf"{field}.*finite number",
                ):
                    analyze_stereo_onset(
                        frames,
                        8_000,
                        0,
                        note_off_frame=10,
                        **optional,
                    )

        with self.assertRaisesRegex(
            ValueError,
            r"ranges\[0\]\.low.*finite number",
        ):
            select_probe_notes(((True, 40),))

    def test_each_range_contributes_low_middle_high_without_crossing_holes(
        self,
    ) -> None:
        self.assertEqual(
            select_probe_notes(((36, 40), (60, 64))),
            (36, 38, 40, 60, 62, 64),
        )
        self.assertEqual(
            select_probe_notes(((53, 53), (57, 58))),
            (53, 57, 58),
        )

    def test_fractional_range_selects_only_legal_integer_notes(self) -> None:
        self.assertEqual(
            select_probe_notes(((36.2, 40.8),)),
            (37, 38, 40),
        )
        with self.assertRaisesRegex(ValueError, "no legal integer"):
            select_probe_notes(((36.2, 36.8),))

    def test_antiphase_stereo_is_not_mistaken_for_silence(self) -> None:
        sample_rate = 8_000
        note_on_frame = 200
        frames = np.zeros((800, 2), dtype=np.float64)
        time = np.arange(800 - 240) / sample_rate
        signal = 0.25 * np.sin(2.0 * np.pi * 440.0 * time)
        frames[240:, 0] = signal
        frames[240:, 1] = -signal

        result = analyze_stereo_onset(
            frames,
            sample_rate,
            note_on_frame,
            note_off_frame=800,
        )

        self.assertEqual(result["status"], "proposed")
        self.assertIsNotNone(result["candidate_onset_frame"])
        self.assertGreater(result["peak_rms"], 0.1)
        self.assertFalse(result["pre_roll_leak"])

    def test_centered_rms_timestamps_step_and_ramp_within_one_millisecond(
        self,
    ) -> None:
        sample_rate = 8_000
        note_on_frame = 400
        note_off_frame = 1_600

        step = np.zeros((2_000, 2), dtype=np.float64)
        step[note_on_frame:note_off_frame] = 0.25
        step_result = analyze_stereo_onset(
            step,
            sample_rate,
            note_on_frame,
            note_off_frame=note_off_frame,
        )
        self.assertEqual(step_result["status"], "proposed")
        self.assertLessEqual(
            abs(step_result["candidate_onset_frame"]),
            round(0.001 * sample_rate),
        )

        ramp = np.zeros_like(step)
        ramp_frames = 800
        amplitude = np.linspace(0.0, 0.25, ramp_frames, endpoint=False)
        ramp[note_on_frame : note_on_frame + ramp_frames, 0] = amplitude
        ramp[note_on_frame : note_on_frame + ramp_frames, 1] = amplitude
        ramp[note_on_frame + ramp_frames : note_off_frame] = 0.25
        ramp_result = analyze_stereo_onset(
            ramp,
            sample_rate,
            note_on_frame,
            note_off_frame=note_off_frame,
        )
        expected_one_percent_frame = round(0.01 * ramp_frames)
        self.assertEqual(ramp_result["status"], "proposed")
        self.assertLessEqual(
            abs(
                ramp_result["candidate_onset_frame"]
                - expected_one_percent_frame
            ),
            round(0.001 * sample_rate),
        )
        self.assertLessEqual(
            abs(ramp_result["t50_frame"] - ramp_frames // 2),
            round(0.001 * sample_rate),
        )

    def test_release_and_tail_can_never_become_attack(self) -> None:
        frames = np.zeros((1_200, 2), dtype=np.float64)
        frames[600:] = 0.25
        result = analyze_stereo_onset(
            frames,
            8_000,
            200,
            note_off_frame=600,
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNone(result["candidate_onset_frame"])

    def test_leak_clipping_and_peak_at_analysis_end_block_candidate(self) -> None:
        frames = np.zeros((2_000, 2), dtype=np.float64)
        frames[:400] = 0.0005
        frames[400:1_600] = 0.2
        leak = analyze_stereo_onset(
            frames,
            8_000,
            400,
            note_off_frame=1_600,
        )
        self.assertTrue(leak["pre_roll_leak"])
        self.assertEqual(leak["status"], "unresolved")
        self.assertIsNone(leak["candidate_onset_frame"])

        clean = np.zeros_like(frames)
        clean[400:1_600] = 0.2
        clipped = analyze_stereo_onset(
            clean,
            8_000,
            400,
            note_off_frame=1_600,
            pre_quantization_clipping_sample_count=3,
        )
        self.assertTrue(clipped["clipped"])
        self.assertEqual(clipped["status"], "unresolved")
        self.assertIsNone(clipped["candidate_onset_frame"])

        rising = np.zeros_like(frames)
        rising[400:1_600, 0] = np.linspace(0.0, 0.2, 1_200)
        rising[400:1_600, 1] = rising[400:1_600, 0]
        near_end = analyze_stereo_onset(
            rising,
            8_000,
            400,
            note_off_frame=1_600,
        )
        self.assertEqual(near_end["status"], "unresolved")
        self.assertIn("longer note", near_end["reason"])

    def test_silence_is_unresolved_and_never_fabricates_an_onset(self) -> None:
        result = analyze_stereo_onset(
            np.zeros((800, 2), dtype=np.float64),
            8_000,
            200,
            note_off_frame=800,
        )
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNone(result["candidate_onset_frame"])
        self.assertIsNotNone(result["reason"])

    def test_default_attack_window_is_long_enough_for_slow_samples(self) -> None:
        spec = ProbeSpec(
            manifest_path=ROOT / "乐器" / "placeholder.json",
            output_directory=OUTPUT / "placeholder",
            articulation="slow",
            midi_note=60,
        )
        self.assertEqual(spec.note_seconds, 4.0)

        sample_rate = 8_000
        note_on_frame = sample_rate
        note_off_frame = note_on_frame + round(4.0 * sample_rate)
        slow_attack_frames = round(2.35 * sample_rate)
        frames = np.zeros((note_off_frame + 2_000, 2), dtype=np.float64)
        ramp = np.linspace(0.0, 0.2, slow_attack_frames, endpoint=False)
        frames[
            note_on_frame : note_on_frame + slow_attack_frames,
            0,
        ] = ramp
        frames[
            note_on_frame : note_on_frame + slow_attack_frames,
            1,
        ] = ramp
        frames[note_on_frame + slow_attack_frames : note_off_frame] = 0.2
        result = analyze_stereo_onset(
            frames,
            sample_rate,
            note_on_frame,
            note_off_frame=note_off_frame,
        )
        self.assertEqual(result["status"], "proposed")
        self.assertGreater(result["peak_frame"], slow_attack_frames)


class DeterministicSingleVariantProtocolTests(unittest.TestCase):
    def _write_sample(self, path: Path) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8_000)
            output.writeframes(
                b"".join(
                    struct.pack("<h", value)
                    for value in (0, 1200, -1200, 600, -600, 0)
                )
            )

    def _sample_instrument(
        self,
        directory: Path,
        regions: list[dict],
        *,
        cls: type[SampleInstrument] = SampleInstrument,
    ) -> SampleInstrument:
        for region in regions:
            sample = directory / region["sample"]
            if not sample.exists():
                self._write_sample(sample)
        manifest = {"type": "sample", "regions": regions}
        if cls is SampleInstrument:
            instrument = create_instrument(
                manifest,
                8_000,
                base_directory=str(directory),
            )
            assert type(instrument) is SampleInstrument
        else:
            instrument = cls.from_manifest(
                manifest,
                8_000,
                base_directory=str(directory),
            )
        instrument._test_factory_manifest = manifest
        return instrument

    def _condition(self, velocity: int) -> tuple[dict, str]:
        payload = onset_sampled_condition(
            final_articulation="sustain",
            midi_note=69,
            velocity=velocity,
            sample_rate_hz=8_000,
        )
        identifier = onset_sampled_condition_id(
            final_articulation="sustain",
            midi_note=69,
            velocity=velocity,
            sample_rate_hz=8_000,
        )
        return payload, identifier

    def _capture_selection(
        self,
        instrument: SampleInstrument,
        velocity: int,
    ) -> dict:
        with capture_runtime_variants() as capture:
            instrument._select_region(
                440.0,
                velocity / 127.0,
                target_midi=69.0,
                random_value=0.5,
            )
        return capture.receipt()

    def test_selector_and_proof_integer_fields_reject_boolean_aliases(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="onset-integer-contract-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            instrument = self._sample_instrument(
                directory,
                [{"sample": "one.wav", "root_pitch_hz": 440.0}],
            )
            receipt = self._capture_selection(instrument, 80)
            condition, condition_id = self._condition(80)
            proof = certify_deterministic_single_observation(
                instrument=instrument,
                manifest=instrument._test_factory_manifest,
                selection_receipt=receipt,
                condition_id=condition_id,
                sampled_condition=condition,
            )

            selector_cases = (
                ("candidate_count", True),
                ("candidate_index", False),
                ("round_robin_counter_before", False),
            )
            for field, value in selector_cases:
                with self.subTest(selector_field=field):
                    forged_receipt = copy.deepcopy(receipt)
                    forged_receipt["selections"][0]["actual_selector"][
                        field
                    ] = value
                    _rehash_runtime_receipt(forged_receipt)
                    with self.assertRaisesRegex(
                        RuntimeVariantError,
                        rf"{field}.*integer",
                    ):
                        certify_deterministic_single_observation(
                            instrument=instrument,
                            manifest=instrument._test_factory_manifest,
                            selection_receipt=forged_receipt,
                            condition_id=condition_id,
                            sampled_condition=condition,
                        )

            proof_cases = (
                (
                    "schema_version",
                    lambda value: value.__setitem__("schema_version", True),
                ),
                (
                    "variation_slot",
                    lambda value: value.__setitem__("variation_slot", False),
                ),
                (
                    "top_level_contract.schema_version",
                    lambda value: value["top_level_contract"].__setitem__(
                        "schema_version", True
                    ),
                ),
                (
                    "expected_selection_count",
                    lambda value: value["top_level_contract"].__setitem__(
                        "expected_selection_count", True
                    ),
                ),
                (
                    "midi_note",
                    lambda value: value["sampled_condition"].__setitem__(
                        "midi_note", 69.0
                    ),
                ),
            )
            for field, mutate in proof_cases:
                with self.subTest(proof_field=field):
                    forged_proof = copy.deepcopy(proof)
                    mutate(forged_proof)
                    _rehash_runtime_proof(forged_proof)
                    with self.assertRaisesRegex(
                        RuntimeVariantError,
                        rf"{field}.*integer",
                    ):
                        validate_runtime_variant_proof_document(
                            forged_proof,
                            selection_receipt=receipt,
                            condition_id=condition_id,
                            sampled_condition=condition,
                            variation_slot=0,
                        )

            with self.assertRaisesRegex(
                RuntimeVariantError,
                "variation_slot.*integer",
            ):
                certify_runtime_variant_observation(
                    instrument=instrument,
                    manifest=instrument._test_factory_manifest,
                    selection_receipt=receipt,
                    condition_id=condition_id,
                    sampled_condition=condition,
                    variation_slot=False,
                )

            equivalent_receipt = copy.deepcopy(receipt)
            catalog_condition = equivalent_receipt["catalogs"][0][
                "catalog"
            ]["condition"]
            catalog_condition["pitch_hz"] = int(
                catalog_condition["pitch_hz"]
            )
            _rehash_first_runtime_catalog(equivalent_receipt)
            equivalent_proof = certify_deterministic_single_observation(
                instrument=instrument,
                manifest=instrument._test_factory_manifest,
                selection_receipt=equivalent_receipt,
                condition_id=condition_id,
                sampled_condition=condition,
            )
            self.assertEqual(
                equivalent_proof["selection_receipt_sha256"],
                equivalent_receipt["receipt_sha256"],
            )

    def test_forged_single_catalog_for_real_multi_candidate_is_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="onset-forged-catalog-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            instrument = self._sample_instrument(
                directory,
                [
                    {"sample": "rr1.wav", "root_pitch_hz": 440.0},
                    {"sample": "rr2.wav", "root_pitch_hz": 440.0},
                ],
            )
            receipt = self._capture_selection(instrument, 80)
            forged = copy.deepcopy(receipt)
            catalog = forged["catalogs"][0]["catalog"]
            sole_choice = catalog["choices"][0]
            sole_hash = sole_choice["choice_sha256"]
            catalog["choices"] = [sole_choice]
            catalog["has_selector_gaps"] = False
            catalog["unexhausted_domains"] = []
            catalog["deterministic_single"] = True
            for partition in catalog["partitions"]:
                partition["status"] = "choices"
                partition["choice_sha256s"] = [sole_hash]
            catalog_hash = stable_variant_sha256(
                "runtime-variant-catalog-v1",
                catalog,
            )
            forged["catalogs"][0]["catalog_sha256"] = catalog_hash
            selection = forged["selections"][0]
            selection.update(
                {
                    "choice_sha256": sole_hash,
                    "catalog_sha256": catalog_hash,
                    "deterministic_single": True,
                    "unexhausted_domains": [],
                    "actual_selector": {
                        "random_value": 0.5,
                        "round_robin_counter_before": 0,
                        "candidate_count": 1,
                        "candidate_index": 0,
                    },
                }
            )
            forged["all_conditions_deterministic_single"] = True
            forged["receipt_sha256"] = stable_variant_sha256(
                "runtime-variant-selection-receipt-v1",
                {
                    key: value
                    for key, value in forged.items()
                    if key != "receipt_sha256"
                },
            )
            condition, condition_id = self._condition(80)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "live exact SampleInstrument catalog",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=instrument._test_factory_manifest,
                    selection_receipt=forged,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

    def test_exact_single_sample_is_certifiable_but_receipt_stays_capture_only(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="onset-single-sample-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            instrument = self._sample_instrument(
                directory,
                [{"sample": "one.wav", "root_pitch_hz": 440.0}],
            )
            receipt = self._capture_selection(instrument, 80)
            condition, condition_id = self._condition(80)
            proof = certify_deterministic_single_observation(
                instrument=instrument,
                manifest=instrument._test_factory_manifest,
                selection_receipt=receipt,
                condition_id=condition_id,
                sampled_condition=condition,
            )
            self.assertEqual(
                receipt["claim"],
                "capture_only_not_variant_certification",
            )
            self.assertEqual(
                proof["claim"],
                "all_runtime_variants_at_one_sampled_condition",
            )
            self.assertEqual(proof["variation_slot"], 0)

    def test_random_gap_and_jitter_cannot_use_single_variant_route(
        self,
    ) -> None:
        cases = {
            "random-gap": [
                {
                    "sample": "gap.wav",
                    "root_pitch_hz": 440.0,
                    "random_min": 0.25,
                    "random_max": 0.75,
                }
            ],
            "jitter": [
                {
                    "sample": "jitter.wav",
                    "root_pitch_hz": 440.0,
                    "delay_random_seconds": 0.002,
                }
            ],
        }
        for name, regions in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory(
                prefix=f"onset-{name}-",
                dir=OUTPUT,
            ) as temporary:
                instrument = self._sample_instrument(
                    Path(temporary),
                    regions,
                )
                receipt = self._capture_selection(instrument, 80)
                condition, condition_id = self._condition(80)
                with self.assertRaises(RuntimeVariantError):
                    certify_deterministic_single_observation(
                        instrument=instrument,
                        manifest=instrument._test_factory_manifest,
                        selection_receipt=receipt,
                        condition_id=condition_id,
                        sampled_condition=condition,
                    )

    def test_low_velocity_receipt_cannot_certify_high_velocity_condition(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="onset-cross-velocity-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            instrument = self._sample_instrument(
                directory,
                [
                    {
                        "sample": "low.wav",
                        "root_pitch_hz": 440.0,
                        "velocity_min": 0.0,
                        "velocity_max": 0.5,
                    },
                    {
                        "sample": "high-a.wav",
                        "root_pitch_hz": 440.0,
                        "velocity_min": 0.75,
                        "velocity_max": 1.0,
                    },
                    {
                        "sample": "high-b.wav",
                        "root_pitch_hz": 440.0,
                        "velocity_min": 0.75,
                        "velocity_max": 1.0,
                    },
                ],
            )
            low_receipt = self._capture_selection(instrument, 32)
            high_condition, high_condition_id = self._condition(120)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "another pitch/velocity|live exact",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=instrument._test_factory_manifest,
                    selection_receipt=low_receipt,
                    condition_id=high_condition_id,
                    sampled_condition=high_condition,
                )

    def test_sample_subclass_wrapper_cannot_claim_top_level_completeness(
        self,
    ) -> None:
        class WrapperWithHiddenChoice(SampleInstrument):
            pass

        with tempfile.TemporaryDirectory(
            prefix="onset-wrapper-contract-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            instrument = self._sample_instrument(
                directory,
                [{"sample": "one.wav", "root_pitch_hz": 440.0}],
                cls=WrapperWithHiddenChoice,
            )
            receipt = self._capture_selection(instrument, 80)
            condition, condition_id = self._condition(80)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "top-level backend",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=instrument._test_factory_manifest,
                    selection_receipt=receipt,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

    def test_local_factory_returning_exact_builtin_cannot_forge_origin(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="onset-local-factory-origin-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            implementation = directory / "factory.py"
            implementation.write_text(
                "from tianlai.oscillator import OscillatorInstrument\n"
                "def create(*, manifest, sample_rate, base_directory):\n"
                "    return OscillatorInstrument.from_manifest("
                "manifest, sample_rate)\n",
                encoding="utf-8",
            )
            manifest = {
                "type": "oscillator",
                "implementation": "factory.py",
            }
            instrument = create_instrument(
                manifest,
                8_000,
                base_directory=str(directory),
            )
            self.assertEqual(
                type(instrument).__name__,
                "OscillatorInstrument",
            )
            with capture_runtime_variants() as capture:
                pass
            condition, condition_id = self._condition(80)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "local implementation factories",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=manifest,
                    selection_receipt=capture.receipt(),
                    condition_id=condition_id,
                    sampled_condition=condition,
                )


class DedicatedSfzVariantProtocolTests(unittest.TestCase):
    def _write_sample(self, path: Path, scale: int) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8_000)
            values = (0, scale, -scale, scale // 2, -scale // 2) * 160
            output.writeframes(
                b"".join(struct.pack("<h", value) for value in values)
            )

    def _fixture(
        self,
        directory: Path,
        *,
        round_robin: bool = False,
    ) -> tuple[Path, dict]:
        assets = directory / "assets"
        assets.mkdir(parents=True)
        for index, name in enumerate(
            ("base.wav", "point.wav", "release.wav", "rr2.wav"),
            start=1,
        ):
            self._write_sample(assets / name, 500 * index)
        if round_robin:
            base_regions = (
                "<region> sample=base.wav lokey=60 hikey=70 "
                "pitch_keycenter=65 seq_length=2 seq_position=1\n"
                "<region> sample=rr2.wav lokey=60 hikey=70 "
                "pitch_keycenter=65 seq_length=2 seq_position=2\n"
            )
        else:
            base_regions = (
                "<region> sample=base.wav lokey=60 hikey=70 "
                "pitch_keycenter=65\n"
            )
        (assets / "instrument.sfz").write_text(
            base_regions
            + "<region> sample=point.wav key=60 pitch_keycenter=60\n"
            + "<region> trigger=release sample=release.wav "
            "lokey=60 hikey=70 pitch_keycenter=65\n",
            encoding="utf-8",
        )
        manifest = {
            "name": "dedicated-sfz-variant-test",
            "type": "dedicated_sfz",
            "quality_tier": "candidate",
            "license_status": "approved",
            "asset_root": "assets",
            "pitch_mode": "pitched",
            "note_min": 65,
            "note_max": 65,
            "articulations": {
                "sustain": {
                    "sfz": "instrument.sfz",
                    "playable_ranges": [[65, 65]],
                }
            },
            "default_articulation": "sustain",
            "release_seconds": 0.02,
            "gain": 0.2,
        }
        manifest_path = directory / "乐器.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest_path, manifest

    def _attack_capture(
        self,
        directory: Path,
        manifest: dict,
        *,
        midi_note: int = 65,
        velocity: int = 80,
        instrument_class: type[DedicatedSfzInstrument] = (
            DedicatedSfzInstrument
        ),
    ) -> tuple[DedicatedSfzInstrument, object, dict]:
        performance = parse_performance_document(
            {
                "sample_rate": 8_000,
                "channels": 2,
                "duration_seconds": 0.09,
                "tail_seconds": 0.02,
                "events": [
                    {
                        "time": 0.0,
                        "type": "articulation",
                        "name": "sustain",
                    },
                    {
                        "time": 0.01,
                        "type": "note_on",
                        "note_id": 1,
                        "midi_note": midi_note,
                        "velocity": velocity / 127.0,
                    },
                    {
                        "time": 0.05,
                        "type": "note_off",
                        "note_id": 1,
                        "release_velocity": 0.5,
                    },
                ],
            }
        )
        if instrument_class is DedicatedSfzInstrument:
            created = create_instrument(
                manifest,
                8_000,
                base_directory=str(directory),
            )
            assert type(created) is DedicatedSfzInstrument
            instrument = created
        else:
            instrument = instrument_class(
                8_000,
                manifest,
                str(directory),
            )
        instrument.handle_event(
            performance.events[0],
            performance.tuning,
        )
        with capture_runtime_variants() as capture:
            instrument.handle_event(
                performance.events[1],
                performance.tuning,
            )
        return instrument, performance, capture.receipt()

    def _condition(
        self,
        *,
        midi_note: int = 65,
        velocity: int = 80,
    ) -> tuple[dict, str]:
        condition = onset_sampled_condition(
            final_articulation="sustain",
            midi_note=midi_note,
            velocity=velocity,
            sample_rate_hz=8_000,
        )
        return condition, onset_sampled_condition_id(
            final_articulation="sustain",
            midi_note=midi_note,
            velocity=velocity,
            sample_rate_hz=8_000,
        )

    def test_exact_composite_reconstructs_discarded_and_retained_layers(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-composite-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            _manifest_path, manifest = self._fixture(directory)
            instrument, performance, receipt = self._attack_capture(
                directory,
                manifest,
            )
            condition, condition_id = self._condition()
            proof = certify_deterministic_single_observation(
                instrument=instrument,
                manifest=manifest,
                selection_receipt=receipt,
                condition_id=condition_id,
                sampled_condition=condition,
            )

            contract = proof["top_level_contract"]
            phase = contract["attack_phase_contract"]
            self.assertEqual(
                contract["backend"],
                "builtin_dedicated_sfz",
            )
            self.assertEqual(receipt["selection_count"], 2)
            self.assertEqual(phase["capture_scope"], "note_on_attack_only")
            self.assertEqual(phase["retained_layer_indexes"], [0])
            self.assertEqual(
                [
                    binding["retention"]
                    for binding in phase["ordered_layer_bindings"]
                ],
                [
                    "retained_attack_voice",
                    "discarded_key_or_velocity_mismatch",
                ],
            )

            # The same exact backend has a release-trigger selector, but it is
            # deliberately a separate note_off phase and cannot be substituted
            # for the certified attack receipt.
            with capture_runtime_variants() as release_capture:
                instrument.handle_event(
                    performance.events[2],
                    performance.tuning,
                )
            release_receipt = release_capture.receipt()
            self.assertEqual(release_receipt["selection_count"], 1)
            with self.assertRaises(RuntimeVariantError):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=manifest,
                    selection_receipt=release_receipt,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

    def test_composite_rejects_cross_condition_and_layer_order_reuse(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-attacks-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            _manifest_path, manifest = self._fixture(directory)
            instrument, _performance, receipt = self._attack_capture(
                directory,
                manifest,
            )
            high_condition, high_condition_id = self._condition(
                velocity=120,
            )
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "another layer|live exact layer catalog",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=manifest,
                    selection_receipt=receipt,
                    condition_id=high_condition_id,
                    sampled_condition=high_condition,
                )

            reordered = copy.deepcopy(receipt)
            reordered["selections"].reverse()
            for index, selection in enumerate(reordered["selections"]):
                selection["selection_index"] = index
            reordered["receipt_sha256"] = stable_variant_sha256(
                "runtime-variant-selection-receipt-v1",
                {
                    key: value
                    for key, value in reordered.items()
                    if key != "receipt_sha256"
                },
            )
            condition, condition_id = self._condition()
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "another layer",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=manifest,
                    selection_receipt=reordered,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

    def test_composite_rejects_a_same_type_manifest_from_another_instance(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-manifest-binding-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            _manifest_path, manifest = self._fixture(directory)
            instrument, _performance, receipt = self._attack_capture(
                directory,
                manifest,
            )
            other_manifest = copy.deepcopy(manifest)
            other_manifest["gain"] = 0.123
            condition, condition_id = self._condition()
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "construction manifest",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=other_manifest,
                    selection_receipt=receipt,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

    def test_dedicated_subclass_and_local_implementation_fail_closed(
        self,
    ) -> None:
        class HiddenWrapper(DedicatedSfzInstrument):
            pass

        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-provenance-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            _manifest_path, manifest = self._fixture(directory)
            wrapper, _performance, receipt = self._attack_capture(
                directory,
                manifest,
                instrument_class=HiddenWrapper,
            )
            condition, condition_id = self._condition()
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "top-level backend",
            ):
                certify_deterministic_single_observation(
                    instrument=wrapper,
                    manifest=manifest,
                    selection_receipt=receipt,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

            exact, _performance, exact_receipt = self._attack_capture(
                directory,
                manifest,
            )
            local_manifest = dict(manifest)
            local_manifest["implementation"] = "wrapper.py"
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "local implementation",
            ):
                certify_deterministic_single_observation(
                    instrument=exact,
                    manifest=local_manifest,
                    selection_receipt=exact_receipt,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

    def test_transient_wrapper_gain_monkeypatch_cannot_self_heal_proof(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-wrapper-commit-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            _manifest_path, manifest = self._fixture(directory)
            (directory / "assets" / "instrument.sfz").write_text(
                "<region> sample=base.wav lokey=60 hikey=70 "
                "pitch_keycenter=65\n"
                "<region> sample=point.wav key=65 pitch_keycenter=65 "
                "xfin_lovel=100 xfin_hivel=127\n",
                encoding="utf-8",
            )
            performance = parse_performance_document(
                {
                    "sample_rate": 8_000,
                    "channels": 2,
                    "duration_seconds": 0.05,
                    "tail_seconds": 0.02,
                    "events": [
                        {
                            "time": 0.0,
                            "type": "articulation",
                            "name": "sustain",
                        },
                        {
                            "time": 0.01,
                            "type": "note_on",
                            "note_id": 1,
                            "midi_note": 65,
                            "velocity": 1 / 127.0,
                        },
                        {
                            "time": 0.03,
                            "type": "note_off",
                            "note_id": 1,
                            "release_velocity": 0.5,
                        },
                    ],
                }
            )
            instrument = create_instrument(
                manifest,
                8_000,
                base_directory=str(directory),
            )
            assert type(instrument) is DedicatedSfzInstrument
            instrument.handle_event(
                performance.events[0],
                performance.tuning,
            )
            with mock.patch.object(
                DedicatedSfzRegionMetadata,
                "velocity_gain",
                return_value=1.0,
            ):
                with capture_runtime_variants() as capture:
                    instrument.handle_event(
                        performance.events[1],
                        performance.tuning,
                    )
            receipt = capture.receipt()
            self.assertEqual(
                [
                    selection["wrapper_outcome"]["route_committed"]
                    for selection in receipt["selections"]
                ],
                [True, True],
            )
            self.assertEqual(len(instrument.routes[1].voices), 2)
            condition, condition_id = self._condition(velocity=1)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                "actual wrapper commit",
            ):
                certify_deterministic_single_observation(
                    instrument=instrument,
                    manifest=manifest,
                    selection_receipt=receipt,
                    condition_id=condition_id,
                    sampled_condition=condition,
                )

    def test_content_identical_components_keep_distinct_wrapper_roles(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-component-collision-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            _manifest_path, manifest = self._fixture(directory)
            (directory / "assets" / "instrument.sfz").write_text(
                "<region> sample=base.wav lokey=60 hikey=70 "
                "pitch_keycenter=65\n",
                encoding="utf-8",
            )
            first = create_instrument(
                manifest,
                8_000,
                base_directory=str(directory),
            )
            second = create_instrument(
                manifest,
                8_000,
                base_directory=str(directory),
            )
            assert type(first) is DedicatedSfzInstrument
            assert type(second) is DedicatedSfzInstrument
            first_runtime = first.articulations["sustain"]
            second_runtime = second.articulations["sustain"]
            first_runtime.attack_layers = (
                first_runtime.attack_layers[0],
                second_runtime.attack_layers[0],
            )
            performance = parse_performance_document(
                {
                    "sample_rate": 8_000,
                    "channels": 2,
                    "duration_seconds": 0.05,
                    "tail_seconds": 0.02,
                    "events": [
                        {
                            "time": 0.0,
                            "type": "articulation",
                            "name": "sustain",
                        },
                        {
                            "time": 0.01,
                            "type": "note_on",
                            "note_id": 1,
                            "midi_note": 65,
                            "velocity": 80 / 127.0,
                        },
                        {
                            "time": 0.03,
                            "type": "note_off",
                            "note_id": 1,
                            "release_velocity": 0.5,
                        },
                    ],
                }
            )
            first.handle_event(
                performance.events[0],
                performance.tuning,
            )
            with capture_runtime_variants() as capture:
                first.handle_event(
                    performance.events[1],
                    performance.tuning,
                )
            receipt = capture.receipt()
            self.assertEqual(receipt["selection_count"], 2)
            self.assertEqual(len(receipt["catalogs"]), 1)
            self.assertEqual(
                len(
                    {
                        selection["component_sha256"]
                        for selection in receipt["selections"]
                    }
                ),
                1,
            )
            condition, condition_id = self._condition()
            proof = certify_deterministic_single_observation(
                instrument=first,
                manifest=manifest,
                selection_receipt=receipt,
                condition_id=condition_id,
                sampled_condition=condition,
            )
            contract = proof["top_level_contract"]
            self.assertEqual(
                len(contract["expected_component_sha256s"]),
                2,
            )
            self.assertEqual(
                len(
                    {
                        binding["wrapper_role_sha256"]
                        for binding in contract["attack_phase_contract"][
                            "ordered_layer_bindings"
                        ]
                    }
                ),
                2,
            )

    def test_probe_receipt_excludes_release_trigger_and_replays_exactly(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-probe-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            manifest_path, _manifest = self._fixture(directory)
            output = directory / "published"
            report = run_probe_batch(
                manifest_path,
                output,
                sample_rate=8_000,
                pre_roll_seconds=0.025,
                note_seconds=0.05,
                tail_seconds=0.03,
                velocities=(80,),
            )
            self.assertEqual(
                report["protocol"]["variant_coverage"],
                "all_runtime_variants",
            )
            self.assertEqual(len(report["observations"]), 1)
            observation = report["observations"][0]
            self.assertEqual(
                observation["selection_receipt"]["selection_count"],
                2,
            )
            self.assertEqual(
                observation["variant_catalog_proof"][
                    "top_level_contract"
                ]["attack_phase_contract"]["retained_layer_indexes"],
                [0],
            )
            load_candidate_report(
                output / REPORT_FILENAME,
                project_root=ROOT,
            )

    def test_finite_rr_cycle_renders_every_natural_slot_and_blocks_omission(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-finite-rr-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            manifest_path, _manifest = self._fixture(
                directory,
                round_robin=True,
            )
            output = directory / "published"
            report = run_probe_batch(
                manifest_path,
                output,
                sample_rate=8_000,
                pre_roll_seconds=0.025,
                note_seconds=0.05,
                tail_seconds=0.03,
                velocities=(80,),
            )
            observations = report["observations"]
            self.assertEqual(
                report["protocol"]["variant_coverage"],
                "all_runtime_variants",
            )
            self.assertEqual(len(observations), 2)
            self.assertEqual(
                [item["variation_slot"] for item in observations],
                [0, 1],
            )
            self.assertEqual(
                {
                    item["variant_catalog_proof"]["kind"]
                    for item in observations
                },
                {"finite_rr_runtime_variant_proof"},
            )
            self.assertEqual(
                {
                    item["variant_catalog_proof"]["variation_period"]
                    for item in observations
                },
                {2},
            )
            self.assertEqual(
                len(
                    {
                        item["variant_catalog_proof"][
                            "slot_bundle_sha256"
                        ]
                        for item in observations
                    }
                ),
                2,
            )
            load_candidate_report(
                output / REPORT_FILENAME,
                project_root=ROOT,
            )

            slot_one = observations[1]
            original_proof = slot_one["variant_catalog_proof"]
            expected_condition = copy.deepcopy(
                original_proof["sampled_condition"]
            )
            finite_proof_cases = (
                (
                    "variation_slot",
                    lambda value: value.__setitem__("variation_slot", True),
                ),
                (
                    "variation_period",
                    lambda value: value.__setitem__("variation_period", 2.0),
                ),
                (
                    "cycle variation_slot",
                    lambda value: value["top_level_contract"][
                        "finite_rr_cycle_contract"
                    ].__setitem__("variation_slot", True),
                ),
                (
                    "cycle variation_period",
                    lambda value: value["top_level_contract"][
                        "finite_rr_cycle_contract"
                    ].__setitem__("variation_period", 2.0),
                ),
            )
            for field, mutate in finite_proof_cases:
                with self.subTest(finite_proof_field=field):
                    forged_proof = copy.deepcopy(original_proof)
                    mutate(forged_proof)
                    _rehash_runtime_proof(forged_proof)
                    with self.assertRaisesRegex(
                        RuntimeVariantError,
                        r"variation_(?:slot|period).*integer",
                    ):
                        validate_runtime_variant_proof_document(
                            forged_proof,
                            selection_receipt=slot_one["selection_receipt"],
                            condition_id=slot_one["condition_id"],
                            sampled_condition=expected_condition,
                            variation_slot=1,
                        )

            attack_scalar_cases = (
                (
                    "selector_random_value",
                    lambda attack: attack.__setitem__(
                        "selector_random_value", True
                    ),
                    r"selector_random_value.*finite number",
                ),
                (
                    "wrapper_velocity_gain",
                    lambda attack: attack["ordered_layer_bindings"][
                        0
                    ].__setitem__("wrapper_velocity_gain", True),
                    r"wrapper_velocity_gain.*finite number",
                ),
                (
                    "effective_static_gain",
                    lambda attack: attack["ordered_layer_bindings"][
                        0
                    ].__setitem__("effective_static_gain", True),
                    r"effective_static_gain.*finite number",
                ),
                (
                    "route_retained",
                    lambda attack: attack["ordered_layer_bindings"][
                        0
                    ].__setitem__("route_retained", 1),
                    r"route_retained.*boolean",
                ),
            )
            for field, mutate, pattern in attack_scalar_cases:
                with self.subTest(attack_scalar_field=field):
                    forged_proof = copy.deepcopy(original_proof)
                    mutate(
                        forged_proof["top_level_contract"][
                            "attack_phase_contract"
                        ]
                    )
                    _rehash_runtime_proof(forged_proof)
                    with self.assertRaisesRegex(
                        RuntimeVariantError,
                        pattern,
                    ):
                        validate_runtime_variant_proof_document(
                            forged_proof,
                            selection_receipt=slot_one["selection_receipt"],
                            condition_id=slot_one["condition_id"],
                            sampled_condition=expected_condition,
                            variation_slot=1,
                        )

            stale_bundle_proof = copy.deepcopy(original_proof)
            stale_binding = stale_bundle_proof["top_level_contract"][
                "attack_phase_contract"
            ]["ordered_layer_bindings"][0]
            stale_binding["wrapper_velocity_gain"] = int(
                stale_binding["wrapper_velocity_gain"]
            )
            _rehash_runtime_proof_outer_hashes(stale_bundle_proof)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                r"retained_bundle_sha256.*invalid",
            ):
                validate_runtime_variant_proof_document(
                    stale_bundle_proof,
                    selection_receipt=slot_one["selection_receipt"],
                    condition_id=slot_one["condition_id"],
                    sampled_condition=expected_condition,
                    variation_slot=1,
                )

            forged_wrapper_proof = copy.deepcopy(original_proof)
            forged_wrapper_proof["top_level_contract"][
                "attack_phase_contract"
            ]["ordered_layer_bindings"][0]["wrapper_outcome_sha256"] = (
                "0" * 64
            )
            _rehash_runtime_proof(forged_wrapper_proof)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                r"wrapper_outcome_sha256 does not bind its receipt",
            ):
                validate_runtime_variant_proof_document(
                    forged_wrapper_proof,
                    selection_receipt=slot_one["selection_receipt"],
                    condition_id=slot_one["condition_id"],
                    sampled_condition=expected_condition,
                    variation_slot=1,
                )

            forged_slot_proof = copy.deepcopy(original_proof)
            forged_cycle = forged_slot_proof["top_level_contract"][
                "finite_rr_cycle_contract"
            ]
            forged_cycle["slot_bundle_sha256"] = "0" * 64
            forged_slot_proof["slot_bundle_sha256"] = "0" * 64
            _rehash_runtime_proof_outer_hashes(forged_slot_proof)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                r"slot_bundle_sha256 is invalid",
            ):
                validate_runtime_variant_proof_document(
                    forged_slot_proof,
                    selection_receipt=slot_one["selection_receipt"],
                    condition_id=slot_one["condition_id"],
                    sampled_condition=expected_condition,
                    variation_slot=1,
                )

            equivalent_report = copy.deepcopy(report)
            equivalent_proof = equivalent_report["observations"][1][
                "variant_catalog_proof"
            ]
            binding = equivalent_proof["top_level_contract"][
                "attack_phase_contract"
            ]["ordered_layer_bindings"][0]
            wrapper_gain = binding["wrapper_velocity_gain"]
            self.assertIsInstance(wrapper_gain, float)
            self.assertTrue(wrapper_gain.is_integer())
            binding["wrapper_velocity_gain"] = int(wrapper_gain)
            _rehash_runtime_proof(equivalent_proof)
            equivalent_report["candidate_sha256"] = canonical_sha256(
                equivalent_report,
                omit="candidate_sha256",
            )
            validate_candidate_report(
                equivalent_report,
                project_root=ROOT,
                verify_artifacts=True,
            )

            forged_receipt = copy.deepcopy(slot_one["selection_receipt"])
            rr_selection = next(
                selection
                for selection in forged_receipt["selections"]
                if selection["actual_selector"]["candidate_index"] == 1
            )
            rr_selection["actual_selector"]["candidate_index"] = True
            _rehash_runtime_receipt(forged_receipt)
            forged_proof = copy.deepcopy(original_proof)
            forged_proof["selection_receipt_sha256"] = forged_receipt[
                "receipt_sha256"
            ]
            _rehash_runtime_proof(forged_proof)
            with self.assertRaisesRegex(
                RuntimeVariantError,
                r"candidate_index.*integer",
            ):
                validate_runtime_variant_proof_document(
                    forged_proof,
                    selection_receipt=forged_receipt,
                    condition_id=slot_one["condition_id"],
                    sampled_condition=expected_condition,
                    variation_slot=1,
                )

            omitted = copy.deepcopy(report)
            omitted["observations"] = [omitted["observations"][0]]
            omitted["candidate_sha256"] = canonical_sha256(
                omitted,
                omit="candidate_sha256",
            )
            with self.assertRaisesRegex(
                OnsetEvidenceError,
                "every cycle slot",
            ):
                validate_candidate_report(
                    omitted,
                    project_root=ROOT,
                    verify_artifacts=True,
                )

    def test_random_partition_remains_explicit_runtime_default_downgrade(
        self,
    ) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="onset-dedicated-random-partition-",
            dir=OUTPUT,
        ) as temporary:
            directory = Path(temporary)
            manifest_path, _manifest = self._fixture(directory)
            (directory / "assets" / "instrument.sfz").write_text(
                "<region> sample=base.wav key=65 pitch_keycenter=65 "
                "lorand=0 hirand=0.5\n"
                "<region> sample=rr2.wav key=65 pitch_keycenter=65 "
                "lorand=0.5 hirand=1\n",
                encoding="utf-8",
            )
            report = run_probe_batch(
                manifest_path,
                directory / "published",
                sample_rate=8_000,
                pre_roll_seconds=0.025,
                note_seconds=0.05,
                tail_seconds=0.03,
                velocities=(80,),
            )
            self.assertEqual(
                report["protocol"]["variant_coverage"],
                "runtime_default_only",
            )
            self.assertEqual(len(report["observations"]), 1)
            self.assertIsNone(
                report["observations"][0]["variant_catalog_proof"]
            )


class OnsetProbeEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="onset-probe-test-",
            dir=OUTPUT,
        )
        self.base = Path(self.temporary.name)
        self.manifest_path = self.base / "乐器.json"
        self.manifest_path.write_text(
            json.dumps(
                {
                    "name": "最小振荡器",
                    "type": "oscillator",
                    "quality_tier": "candidate",
                    "license_status": "approved",
                    "pitch_mode": "pitched",
                    "note_min": 60,
                    "note_max": 60,
                    "articulations": {
                        "sustain": {
                            "playable_ranges": [[60, 60]],
                        }
                    },
                    "default_articulation": "sustain",
                    "attack_seconds": 0.002,
                    "release_seconds": 0.02,
                    "gain": 0.2,
                    "velocity_exponent": 1.0,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_minimal_oscillator_uses_fresh_instances_and_publishes_candidate(
        self,
    ) -> None:
        from tianlai import onset_probe

        output = self.base / "published"
        real_factory = onset_probe.create_instrument
        with mock.patch(
            "tianlai.onset_probe.create_instrument",
            wraps=real_factory,
        ) as factory:
            report = run_probe_batch(
                self.manifest_path,
                output,
                repeat=2,
                sample_rate=8_000,
                pre_roll_seconds=0.025,
                note_seconds=0.05,
                tail_seconds=0.03,
                velocities=(80,),
            )

        self.assertEqual(factory.call_count, 2)
        self.assertFalse(report["automatic_approval"])
        self.assertEqual(report["protocol"]["sample_rate_hz"], 8_000)
        self.assertEqual(
            report["runtime_fingerprint"]["runtime_asset_graph"][
                "sample_rate_hz"
            ],
            report["protocol"]["sample_rate_hz"],
        )
        self.assertEqual(
            report["protocol"]["variant_coverage"],
            "all_runtime_variants",
        )
        self.assertEqual(
            report["protocol"]["condition_coverage"][
                "unique_condition_count"
            ],
            1,
        )
        self.assertEqual(len(report["observations"]), 2)
        self.assertTrue((output / REPORT_FILENAME).is_file())
        self.assertFalse(any(output.rglob("发音延迟.json")))
        for observation in report["observations"]:
            self.assertEqual(observation["note_on_frame"], 200)
            self.assertEqual(observation["final_articulation"], "sustain")
            self.assertEqual(observation["variation_slot"], 0)
            self.assertIsNotNone(observation["variant_catalog_proof"])
            self.assertEqual(
                observation["selection_receipt"]["claim"],
                "capture_only_not_variant_certification",
            )
            wav_path = ROOT / observation["wav_path"]
            performance_path = ROOT / observation["performance_path"]
            with wave.open(str(wav_path), "rb") as source:
                self.assertEqual(source.getnchannels(), 2)
                self.assertEqual(source.getsampwidth(), 3)
                self.assertEqual(source.getframerate(), 8_000)
            decoded_rate, decoded = read_wav_float(wav_path)
            performance = parse_performance_document(
                json.loads(performance_path.read_text(encoding="utf-8"))
            )
            note_on = next(
                event.sample
                for event in performance.events
                if event.type == "note_on"
            )
            note_off = next(
                event.sample
                for event in performance.events
                if event.type == "note_off"
            )
            recomputed = analyze_stereo_onset(
                decoded,
                decoded_rate,
                note_on,
                note_off_frame=note_off,
                pre_quantization_clipping_sample_count=observation[
                    "analysis"
                ]["clipping_sample_count"],
            )
            self.assertEqual(recomputed, observation["analysis"])

        validated = load_candidate_report(
            output / REPORT_FILENAME,
            project_root=ROOT,
        )
        self.assertEqual(validated["candidate_sha256"], report["candidate_sha256"])

        review_path = self.base / "oscillator-review.json"
        create_review_draft(
            output / REPORT_FILENAME,
            review_path,
            project_root=ROOT,
            reviewer_id="test-human",
        )
        for observation in report["observations"]:
            self.assertEqual(observation["analysis"]["status"], "proposed")
            record_review_decision(
                review_path,
                project_root=ROOT,
                observation_id=observation["observation_id"],
                status="measured",
                measured_delay_frames=observation["analysis"][
                    "candidate_onset_frame"
                ],
                comment="test-only manual pick",
            )
        finalize_review(review_path, project_root=ROOT)
        approved = promote_review(
            output / REPORT_FILENAME,
            review_path,
            self.base / "oscillator-approved.json",
            project_root=ROOT,
            explicit_approval=True,
            review_lead="test-lead",
        )
        self.assertFalse(approved["automatic_approval"])
        self.assertEqual(
            approved["policy"]["condition_coverage"],
            "sampled_conditions",
        )

    def test_sample_replay_preserves_json_number_equivalence(self) -> None:
        sample_path = self.base / "one.wav"
        _write_minimal_sample(sample_path)
        self.manifest_path.write_text(
            json.dumps(
                {
                    "name": "number-equivalence sample",
                    "type": "sample",
                    "quality_tier": "candidate",
                    "license_status": "approved",
                    "pitch_mode": "pitched",
                    "note_min": 69,
                    "note_max": 69,
                    "articulations": {
                        "sustain": {"playable_ranges": [[69, 69]]}
                    },
                    "default_articulation": "sustain",
                    "regions": [
                        {
                            "sample": sample_path.name,
                            "root_pitch_hz": 440.0,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        output = self.base / "sample-number-equivalence"
        report = run_probe_batch(
            self.manifest_path,
            output,
            sample_rate=8_000,
            pre_roll_seconds=0.025,
            note_seconds=0.05,
            tail_seconds=0.03,
            velocities=(80,),
        )

        equivalent = copy.deepcopy(report)
        observation = equivalent["observations"][0]
        receipt = observation["selection_receipt"]
        condition = receipt["catalogs"][0]["catalog"]["condition"]
        self.assertEqual(condition["pitch_hz"], 440.0)
        condition["pitch_hz"] = 440
        _rehash_first_runtime_catalog(receipt)

        proof = observation["variant_catalog_proof"]
        proof["selection_receipt_sha256"] = receipt["receipt_sha256"]
        proof["catalog_sha256s"] = sorted(
            record["catalog_sha256"] for record in receipt["catalogs"]
        )
        _rehash_runtime_proof(proof)
        equivalent["candidate_sha256"] = canonical_sha256(
            equivalent,
            omit="candidate_sha256",
        )

        validate_candidate_report(
            equivalent,
            project_root=ROOT,
            verify_artifacts=True,
        )

    def test_articulation_range_holes_expand_per_segment(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["note_min"] = 36
        manifest["note_max"] = 64
        manifest["articulations"]["sustain"]["playable_ranges"] = [
            [36, 40],
            [60, 64],
        ]
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        specs = build_probe_specs(
            self.manifest_path,
            self.base / "range-output",
            sample_rate=8_000,
            velocities=(80,),
        )
        self.assertEqual(
            tuple(spec.midi_note for spec in specs),
            (36, 38, 40, 60, 62, 64),
        )

    def test_failed_batch_never_publishes_partial_output(self) -> None:
        from tianlai import onset_probe

        output = self.base / "must-not-exist"
        real_factory = onset_probe.create_instrument
        call_count = 0

        def fail_second(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("injected render failure")
            return real_factory(*args, **kwargs)

        with mock.patch(
            "tianlai.onset_probe.create_instrument",
            side_effect=fail_second,
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                run_probe_batch(
                    self.manifest_path,
                    output,
                    repeat=2,
                    sample_rate=8_000,
                    pre_roll_seconds=0.025,
                    note_seconds=0.05,
                    tail_seconds=0.03,
                    velocities=(80,),
                )
        self.assertFalse(output.exists())

    def test_manifest_change_during_render_invalidates_whole_batch(self) -> None:
        from tianlai import onset_probe

        output = self.base / "manifest-toctou"
        real_render = onset_probe._render_one

        def mutate_manifest(*args, **kwargs):
            result = real_render(*args, **kwargs)
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            manifest["gain"] = 0.123
            self.manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            return result

        with mock.patch(
            "tianlai.onset_probe._render_one",
            side_effect=mutate_manifest,
        ):
            with self.assertRaisesRegex(RuntimeError, "manifest changed"):
                run_probe_batch(
                    self.manifest_path,
                    output,
                    sample_rate=8_000,
                    pre_roll_seconds=0.025,
                    note_seconds=0.05,
                    tail_seconds=0.03,
                    velocities=(80,),
                )
        self.assertFalse(output.exists())

    def test_strict_self_validation_happens_before_publication(self) -> None:
        output = self.base / "strict-rejection"
        with mock.patch(
            "tianlai.onset_probe.validate_candidate_report",
            side_effect=ValueError("strict candidate rejection"),
        ) as validator:
            with self.assertRaisesRegex(ValueError, "strict candidate"):
                run_probe_batch(
                    self.manifest_path,
                    output,
                    sample_rate=8_000,
                    pre_roll_seconds=0.025,
                    note_seconds=0.05,
                    tail_seconds=0.03,
                    velocities=(80,),
                )
        validator.assert_called_once()
        self.assertFalse(output.exists())

    def test_no_vocabulary_uses_sentinel_without_fake_articulation_event(
        self,
    ) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest.pop("articulations")
        manifest.pop("default_articulation")
        self.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        output = self.base / "sentinel"
        report = run_probe_batch(
            self.manifest_path,
            output,
            sample_rate=8_000,
            pre_roll_seconds=0.025,
            note_seconds=0.05,
            tail_seconds=0.03,
            velocities=(80,),
        )
        observation = report["observations"][0]
        self.assertEqual(observation["final_articulation"], "__default__")
        performance = json.loads(
            (ROOT / observation["performance_path"]).read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "articulation",
            {event["type"] for event in performance["events"]},
        )
        load_candidate_report(output / REPORT_FILENAME, project_root=ROOT)

    def test_anticipatory_sources_are_forbidden(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["type"] = "reversed_cymbal"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reversed_cymbal"):
            build_probe_specs(self.manifest_path, self.base / "reversed")

        manifest["type"] = "oscillator"
        manifest["articulations"] = {"crescendo_short": {}}
        manifest["default_articulation"] = "crescendo_short"
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no onset-safe|forbidden"):
            build_probe_specs(self.manifest_path, self.base / "crescendo")

    def test_quarantined_instrument_cannot_produce_onset_evidence(self) -> None:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        manifest["license_status"] = "quarantined"
        self.manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "quarantined"):
            build_probe_specs(self.manifest_path, self.base / "quarantined-spec")
        output = self.base / "quarantined-run"
        with self.assertRaisesRegex(ValueError, "quarantined"):
            run_probe_batch(
                self.manifest_path,
                output,
                sample_rate=8_000,
                pre_roll_seconds=0.025,
                note_seconds=0.05,
                tail_seconds=0.03,
                velocities=(80,),
            )
        self.assertFalse(output.exists())

    def test_probe_downgrades_local_factory_returning_exact_oscillator(
        self,
    ) -> None:
        implementation = self.base / "factory.py"
        implementation.write_text(
            "from tianlai.oscillator import OscillatorInstrument\n"
            "def create(*, manifest, sample_rate, base_directory):\n"
            "    return OscillatorInstrument.from_manifest("
            "manifest, sample_rate)\n",
            encoding="utf-8",
        )
        manifest = json.loads(
            self.manifest_path.read_text(encoding="utf-8")
        )
        manifest["implementation"] = implementation.name
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        output = self.base / "local-factory-probe"
        report = run_probe_batch(
            self.manifest_path,
            output,
            sample_rate=8_000,
            pre_roll_seconds=0.025,
            note_seconds=0.05,
            tail_seconds=0.03,
            velocities=(80,),
        )
        self.assertEqual(
            report["protocol"]["variant_coverage"],
            "runtime_default_only",
        )
        self.assertIsNone(
            report["observations"][0]["variant_catalog_proof"]
        )
        load_candidate_report(
            output / REPORT_FILENAME,
            project_root=ROOT,
        )


if __name__ == "__main__":
    unittest.main()
