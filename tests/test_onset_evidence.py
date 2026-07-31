from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import wave

from tianlai.capability import load_capabilities, read_capability
from tianlai.onset_evidence import (
    ANCHOR,
    CANDIDATE_SCHEMA,
    CONTEXT,
    OnsetEvidenceError,
    canonical_json_bytes,
    canonical_sha256,
    compute_runtime_fingerprint,
    create_review_draft,
    finalize_review,
    load_approved_onset_evidence,
    load_candidate_report,
    promote_review,
    read_json_strict,
    record_review_decision,
    sha256_file,
    validate_runtime_fingerprint,
    write_json_atomic,
)
from tianlai.instrument import create_instrument
from tianlai.runtime_variants import (
    capture_runtime_variants,
    certify_deterministic_single_observation,
    onset_sampled_condition,
    onset_sampled_condition_id,
)


class OnsetEvidenceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="tianlai_test_onset_evidence_"
        )
        self.root = Path(self.temporary.name)
        (self.root / "tianlai").mkdir()
        render_sources = {
            "audio.py": "# deterministic test audio\n",
            "events.py": "# deterministic test events\n",
            "instrument.py": "# deterministic test instrument dispatcher\n",
            "renderer.py": "# deterministic test renderer\n",
            "tuning.py": "# deterministic test tuning\n",
            "capability.py": "# deterministic test capability contract\n",
            "onset_probe.py": "# deterministic test probe\n",
            "onset_evidence.py": "# deterministic test evidence algorithm\n",
            "sampler.py": "from .audio import TEST_AUDIO\n",
            "oscillator.py": "from .instrument import Instrument\n",
            "synthesizer.py": "from .events import TEST_EVENTS\n",
            "mcp_server.py": "# deliberately outside the render closure\n",
            "cli.py": "# deliberately outside the render closure\n",
        }
        for filename, source in render_sources.items():
            (self.root / "tianlai" / filename).write_text(
                source,
                encoding="utf-8",
            )
        (self.root / "tianlai" / "runtime.py").write_text(
            "RUNTIME = 1\n",
            encoding="utf-8",
        )
        self.instrument_dir = self.root / "乐器" / "弦乐组" / "测试琴"
        self.instrument_dir.mkdir(parents=True)
        self.manifest = self.instrument_dir / "乐器.json"
        self.implementation = self.instrument_dir / "乐器.py"
        self.resource = self.instrument_dir / "资源核验.json"
        self.pitch = self.instrument_dir / "音准校准.json"
        self.implementation.write_text(
            "from tianlai.sampler import SampleInstrument\n"
            "class Instrument: pass\n",
            encoding="utf-8",
        )
        self.resource.write_text('{"verified":true}\n', encoding="utf-8")
        self.pitch.write_text('{"applicable":true}\n', encoding="utf-8")
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "测试琴",
                    "type": "local",
                    "implementation": "乐器.py",
                    "resource_verification": "资源核验.json",
                    "pitch_calibration": "音准校准.json",
                    "default_articulation": "sustain",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.probes = self.root / "output" / "onset"
        self.probes.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _write_wav(self, path: Path, frames: int = 4000) -> None:
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48_000)
            output.writeframes(b"\0\0\0\0" * frames)

    def _observation(
        self,
        index: int,
        *,
        articulation: str = "sustain",
        analysis_status: str = "proposed",
    ) -> dict:
        observation_id = f"obs-{index:02d}"
        performance_path = self.probes / f"{observation_id}.events.json"
        wav_path = self.probes / f"{observation_id}.wav"
        midi_note = 60 + index
        velocity = 32
        performance = {
            "sample_rate": 48_000,
            "channels": 2,
            "duration_seconds": 0.08,
            "tail_seconds": 0.03,
            "events": [
                {
                    "time": 0.0,
                    "type": "articulation",
                    "name": articulation,
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
        performance_path.write_text(
            json.dumps(performance, ensure_ascii=False),
            encoding="utf-8",
        )
        self._write_wav(wav_path)
        proposed = analysis_status == "proposed"
        return {
            "observation_id": observation_id,
            "final_articulation": articulation,
            "midi_note": midi_note,
            "velocity": velocity,
            "performance_path": self._relative(performance_path),
            "performance_sha256": sha256_file(performance_path),
            "wav_path": self._relative(wav_path),
            "wav_sha256": sha256_file(wav_path),
            "note_on_frame": 480,
            "analysis": {
                "status": analysis_status,
                "candidate_onset_frame": 100 if proposed else None,
                "t10_frame": 80 if proposed else None,
                "t50_frame": 110 if proposed else None,
                "t90_frame": 160 if proposed else None,
                "peak_frame": 220 if proposed else None,
                "snr_db": 35.0 if proposed else None,
                "pre_roll_leak": False,
                "clipped": False,
                "reason": None if proposed else "no stable threshold crossing",
                "noise_floor_rms": 0.0001 if proposed else None,
                "threshold_rms": 0.002 if proposed else None,
                "peak_rms": 0.2 if proposed else None,
                "clipping_sample_count": 0,
                "pre_roll_peak_rms": 0.0002 if proposed else None,
            },
        }

    def _candidate(
        self,
        *,
        statuses: tuple[str, ...] = ("proposed", "proposed", "proposed"),
        articulations: tuple[str, ...] | None = None,
        variant_coverage: str = "all_runtime_variants",
    ) -> Path:
        if variant_coverage == "all_runtime_variants":
            self.manifest.write_text(
                json.dumps(
                    {
                        "name": "测试振荡器",
                        "type": "oscillator",
                        "resource_verification": "资源核验.json",
                        "pitch_calibration": "音准校准.json",
                        "default_articulation": "sustain",
                        "attack_seconds": 0.005,
                        "release_seconds": 0.02,
                        "gain": 0.2,
                        "velocity_exponent": 1.0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        if articulations is None:
            articulations = tuple("sustain" for _ in statuses)
        observations = [
            self._observation(
                index,
                articulation=articulations[index],
                analysis_status=status,
            )
            for index, status in enumerate(statuses)
        ]
        condition_ids: list[str] = []
        if variant_coverage == "all_runtime_variants":
            manifest_document = read_json_strict(self.manifest)
            for observation in observations:
                sampled_condition = onset_sampled_condition(
                    final_articulation=observation["final_articulation"],
                    midi_note=observation["midi_note"],
                    velocity=observation["velocity"],
                    sample_rate_hz=48_000,
                )
                condition_id = onset_sampled_condition_id(
                    final_articulation=observation["final_articulation"],
                    midi_note=observation["midi_note"],
                    velocity=observation["velocity"],
                    sample_rate_hz=48_000,
                )
                instrument = create_instrument(
                    manifest_document,
                    48_000,
                    base_directory=str(self.instrument_dir),
                )
                try:
                    with capture_runtime_variants() as capture:
                        pass
                    receipt = capture.receipt()
                    proof = certify_deterministic_single_observation(
                        instrument=instrument,
                        manifest=manifest_document,
                        selection_receipt=receipt,
                        condition_id=condition_id,
                        sampled_condition=sampled_condition,
                        variation_slot=0,
                    )
                finally:
                    close = getattr(instrument, "close", None)
                    if callable(close):
                        close()
                observation.update(
                    {
                        "condition_id": condition_id,
                        "variation_slot": 0,
                        "variant_catalog_proof": proof,
                        "selection_receipt": receipt,
                    }
                )
                condition_ids.append(condition_id)
        fingerprint = compute_runtime_fingerprint(self.root, self.manifest)
        document = {
            "$schema": CANDIDATE_SCHEMA,
            "schema_version": 1,
            "kind": "onset_candidate_report",
            "candidate_sha256": "0" * 64,
            "automatic_approval": False,
            "created_at": "2026-07-25T12:00:00Z",
            "instrument": {
                "manifest_path": fingerprint["manifest"]["path"],
                "manifest_sha256": fingerprint["manifest"]["sha256"],
            },
            "runtime_fingerprint": fingerprint,
            "protocol": {
                "anchor": ANCHOR,
                "context": CONTEXT,
                "variant_coverage": variant_coverage,
                "signal_stage": "instrument_direct_output_no_space",
                "pre_roll_frames": 480,
                "sample_rate_hz": 48_000,
                "algorithm_sha256": sha256_file(
                    self.root / "tianlai" / "onset_probe.py"
                ),
                "window_ms": 5.0,
                "hop_ms": 1.0,
                "threshold_policy": "adaptive_noise_floor_v1",
            },
            "observations": observations,
        }
        if variant_coverage == "all_runtime_variants":
            unique_condition_ids = sorted(set(condition_ids))
            document["protocol"]["condition_coverage"] = {
                "kind": "sampled_conditions",
                "condition_id_algorithm": (
                    "onset-isolated-sampled-condition-v1"
                ),
                "unique_condition_count": len(unique_condition_ids),
                "condition_ids": unique_condition_ids,
            }
        document["candidate_sha256"] = canonical_sha256(
            document,
            omit="candidate_sha256",
        )
        path = self.probes / "candidate.json"
        write_json_atomic(path, document)
        return path

    def _complete_measured_review(
        self,
        candidate: Path,
        delays: tuple[int, ...],
    ) -> Path:
        review = self.probes / "review.json"
        candidate_document = load_candidate_report(
            candidate,
            project_root=self.root,
        )
        create_review_draft(
            candidate,
            review,
            project_root=self.root,
            reviewer_id="human-1",
            display_name="人工审阅者",
            created_at="2026-07-25T13:00:00Z",
        )
        for observation, delay in zip(
            candidate_document["observations"],
            delays,
            strict=True,
        ):
            record_review_decision(
                review,
                project_root=self.root,
                observation_id=observation["observation_id"],
                status="measured",
                measured_delay_frames=delay,
                comment="manual waveform/listening pick",
                decided_at="2026-07-25T13:10:00Z",
            )
        finalize_review(
            review,
            project_root=self.root,
            completed_at="2026-07-25T13:20:00Z",
        )
        return review

    def test_successful_manual_chain_and_strict_loader(self) -> None:
        candidate = self._candidate()
        review = self._complete_measured_review(candidate, (100, 120, 110))
        approved_path = self.instrument_dir / "发音延迟.json"
        approved = promote_review(
            candidate,
            review,
            approved_path,
            project_root=self.root,
            explicit_approval=True,
            review_lead="lead-1",
            review_lead_display_name="审核负责人",
            approved_at="2026-07-25T14:00:00Z",
        )
        self.assertFalse(approved["automatic_approval"])
        self.assertEqual(approved["articulations"]["sustain"]["frames"], 110)
        self.assertEqual(
            approved["articulations"]["sustain"]["sample_rate_hz"],
            48_000,
        )
        loaded = load_approved_onset_evidence(
            approved_path,
            project_root=self.root,
            manifest_path=self.manifest,
        )
        self.assertEqual(loaded["approved_sha256"], approved["approved_sha256"])
        self.assertEqual(
            loaded["review_lead"]["attestation"],
            "explicit_manual_approval",
        )
        capability = read_capability(
            self.manifest,
            root=self.root / "乐器",
        )
        onset = capability.onset_for("sustain", context="isolated_attack")
        self.assertIsNotNone(onset)
        assert onset is not None
        self.assertEqual(onset.frames, 110)
        self.assertEqual(onset.sample_rate_hz, 48_000)
        self.assertEqual(onset.evidence.sha256, sha256_file(approved_path))

    def test_lightweight_runtime_loader_does_not_require_source_artifacts(
        self,
    ) -> None:
        candidate = self._candidate()
        review = self._complete_measured_review(candidate, (100, 110, 120))
        approved_path = self.instrument_dir / "发音延迟.json"
        promote_review(
            candidate,
            review,
            approved_path,
            project_root=self.root,
            explicit_approval=True,
            review_lead="lead",
        )
        approved_bytes = approved_path.read_bytes()
        candidate.unlink()
        review.unlink()
        with self.assertRaisesRegex(OnsetEvidenceError, "does not exist"):
            load_approved_onset_evidence(
                approved_path,
                project_root=self.root,
                manifest_path=self.manifest,
            )
        runtime = load_approved_onset_evidence(
            approved_path,
            project_root=self.root,
            manifest_path=self.manifest,
            verify_source_chain=False,
        )
        self.assertEqual(runtime["articulations"]["sustain"]["frames"], 110)

        tampered = read_json_strict(approved_path)
        tampered["articulations"]["sustain"]["frames"] += 1
        write_json_atomic(approved_path, tampered)
        with self.assertRaisesRegex(OnsetEvidenceError, "self hash"):
            load_approved_onset_evidence(
                approved_path,
                project_root=self.root,
                manifest_path=self.manifest,
                verify_source_chain=False,
            )
        approved_path.write_bytes(approved_bytes)
        self.resource.write_text('{"verified":false}\n', encoding="utf-8")
        with self.assertRaisesRegex(OnsetEvidenceError, "runtime fingerprint is stale"):
            load_approved_onset_evidence(
                approved_path,
                project_root=self.root,
                manifest_path=self.manifest,
                verify_source_chain=False,
            )
        with self.assertRaisesRegex(OnsetEvidenceError, "runtime fingerprint is stale"):
            load_approved_onset_evidence(
                approved_path,
                project_root=self.root,
                manifest_path=self.manifest,
            )

    def test_lightweight_loader_rejects_local_factory_exact_class_origin(
        self,
    ) -> None:
        candidate = self._candidate(statuses=("proposed",))
        review = self._complete_measured_review(candidate, (100,))
        approved_path = self.instrument_dir / "发音延迟.json"
        promote_review(
            candidate,
            review,
            approved_path,
            project_root=self.root,
            explicit_approval=True,
            review_lead="lead",
        )
        implementation = self.instrument_dir / "factory.py"
        implementation.write_text(
            "from tianlai.oscillator import OscillatorInstrument\n"
            "def create(*, manifest, sample_rate, base_directory):\n"
            "    return OscillatorInstrument.from_manifest("
            "manifest, sample_rate)\n",
            encoding="utf-8",
        )
        manifest = read_json_strict(self.manifest)
        manifest["implementation"] = implementation.name
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        approved = read_json_strict(approved_path)
        fingerprint = compute_runtime_fingerprint(
            self.root,
            self.manifest,
        )
        approved["instrument"]["manifest_sha256"] = fingerprint[
            "manifest"
        ]["sha256"]
        approved["runtime_fingerprint"] = fingerprint
        approved["approved_sha256"] = canonical_sha256(
            approved,
            omit="approved_sha256",
        )
        write_json_atomic(approved_path, approved)
        with self.assertRaisesRegex(
            OnsetEvidenceError,
            "portable runtime variant contract.*local implementation",
        ):
            load_approved_onset_evidence(
                approved_path,
                project_root=self.root,
                manifest_path=self.manifest,
                verify_source_chain=False,
            )

    def test_candidate_replay_rejects_local_factory_exact_class_origin(
        self,
    ) -> None:
        candidate_path = self._candidate(statuses=("proposed",))
        implementation = self.instrument_dir / "factory.py"
        implementation.write_text(
            "from tianlai.oscillator import OscillatorInstrument\n"
            "def create(*, manifest, sample_rate, base_directory):\n"
            "    return OscillatorInstrument.from_manifest("
            "manifest, sample_rate)\n",
            encoding="utf-8",
        )
        manifest = read_json_strict(self.manifest)
        manifest["implementation"] = implementation.name
        self.manifest.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        candidate = read_json_strict(candidate_path)
        fingerprint = compute_runtime_fingerprint(
            self.root,
            self.manifest,
        )
        candidate["instrument"]["manifest_sha256"] = fingerprint[
            "manifest"
        ]["sha256"]
        candidate["runtime_fingerprint"] = fingerprint
        candidate["candidate_sha256"] = canonical_sha256(
            candidate,
            omit="candidate_sha256",
        )
        write_json_atomic(candidate_path, candidate)
        with self.assertRaisesRegex(
            OnsetEvidenceError,
            "local implementation factories",
        ):
            load_candidate_report(candidate_path, project_root=self.root)

    def test_candidate_and_approved_tampering_are_rejected(self) -> None:
        candidate = self._candidate()
        original = read_json_strict(candidate)
        tampered = copy.deepcopy(original)
        tampered["observations"][0]["velocity"] = 120
        write_json_atomic(candidate, tampered)
        with self.assertRaisesRegex(OnsetEvidenceError, "self hash"):
            load_candidate_report(candidate, project_root=self.root)
        write_json_atomic(candidate, original)

        review = self._complete_measured_review(candidate, (100, 110, 120))
        approved_path = self.instrument_dir / "发音延迟.json"
        approved = promote_review(
            candidate,
            review,
            approved_path,
            project_root=self.root,
            explicit_approval=True,
            review_lead="lead",
        )
        original_approved = copy.deepcopy(approved)
        approved["articulations"]["sustain"]["frames"] += 1
        approved["approved_sha256"] = canonical_sha256(
            approved,
            omit="approved_sha256",
        )
        write_json_atomic(approved_path, approved)
        with self.assertRaisesRegex(OnsetEvidenceError, "portable manual proof"):
            load_approved_onset_evidence(
                approved_path,
                project_root=self.root,
                manifest_path=self.manifest,
            )
        with self.assertRaisesRegex(ValueError, "portable manual proof"):
            read_capability(
                self.manifest,
                root=self.root / "乐器",
            )
        discovered = load_capabilities(self.root / "乐器")
        deferred = discovered["弦乐组/测试琴"]
        self.assertEqual(deferred.articulation_onsets, ())
        self.assertEqual(
            deferred.to_dict()["onset_evidence_status"],
            "deferred",
        )
        with self.assertRaisesRegex(ValueError, "portable manual proof"):
            deferred.resolve_articulation_onsets()

        proof_tamper = copy.deepcopy(original_approved)
        for decision in proof_tamper["portable_proof"]["review"]["decisions"]:
            if decision["status"] == "measured":
                decision["measured_onset_frame"] += 10
        proof_tamper["articulations"]["sustain"]["frames"] += 10
        proof_tamper["approved_sha256"] = canonical_sha256(
            proof_tamper,
            omit="approved_sha256",
        )
        write_json_atomic(approved_path, proof_tamper)
        load_approved_onset_evidence(
            approved_path,
            project_root=self.root,
            manifest_path=self.manifest,
            verify_source_chain=False,
        )
        with self.assertRaisesRegex(OnsetEvidenceError, "full candidate/review"):
            load_approved_onset_evidence(
                approved_path,
                project_root=self.root,
                manifest_path=self.manifest,
            )

    def test_runtime_fingerprint_staleness_is_fail_closed(self) -> None:
        candidate = self._candidate()
        self.resource.write_text('{"verified":false}\n', encoding="utf-8")
        with self.assertRaisesRegex(OnsetEvidenceError, "runtime fingerprint is stale"):
            load_candidate_report(candidate, project_root=self.root)

    def test_runtime_fingerprint_default_arguments_are_byte_compatible(
        self,
    ) -> None:
        with mock.patch(
            "tianlai.onset_evidence._runtime_dependencies",
            return_value={"fixed": True},
        ):
            default = compute_runtime_fingerprint(self.root, self.manifest)
            explicit_default = compute_runtime_fingerprint(
                self.root,
                self.manifest,
                effective_manifest=None,
                sample_rate_hz=48_000,
            )
        self.assertEqual(
            canonical_json_bytes(default),
            canonical_json_bytes(explicit_default),
        )

    def test_runtime_fingerprint_uses_effective_manifest_and_sample_rate(
        self,
    ) -> None:
        sample = self.instrument_dir / "effective-runtime.wav"
        self._write_wav(sample)
        effective_resource = self.instrument_dir / "effective-resource.json"
        effective_pitch = self.instrument_dir / "effective-pitch.json"
        effective_resource.write_text('{"verified":true}\n', encoding="utf-8")
        effective_pitch.write_text('{"applicable":true}\n', encoding="utf-8")
        effective_manifest = {
            "name": "effective runtime fixture",
            "type": "sample",
            "sample": sample.name,
            "resource_verification": effective_resource.name,
            "pitch_calibration": effective_pitch.name,
        }
        original_effective_manifest = copy.deepcopy(effective_manifest)

        class RuntimeInstrument:
            def __init__(self, sample_path: Path) -> None:
                self.sample_path = sample_path

            def close(self) -> None:
                pass

        constructed: list[tuple[dict, int, str]] = []

        def create_for_graph(
            manifest: dict,
            sample_rate: int,
            *,
            base_directory: str,
        ) -> RuntimeInstrument:
            constructed.append(
                (copy.deepcopy(manifest), sample_rate, base_directory)
            )
            manifest["construction_mutation"] = True
            return RuntimeInstrument(sample)

        with mock.patch(
            "tianlai.instrument.create_instrument",
            side_effect=create_for_graph,
        ):
            fingerprint = compute_runtime_fingerprint(
                self.root,
                self.manifest,
                effective_manifest=effective_manifest,
                sample_rate_hz=44_100,
            )
            validated = validate_runtime_fingerprint(
                fingerprint,
                project_root=self.root,
                manifest_path=self.manifest,
                effective_manifest=effective_manifest,
                sample_rate_hz=44_100,
            )

        self.assertEqual(effective_manifest, original_effective_manifest)
        self.assertEqual(validated, fingerprint)
        self.assertEqual(
            constructed,
            [
                (
                    original_effective_manifest,
                    44_100,
                    str(self.instrument_dir.resolve()),
                ),
                (
                    original_effective_manifest,
                    44_100,
                    str(self.instrument_dir.resolve()),
                ),
            ],
        )
        self.assertEqual(
            fingerprint["manifest"]["sha256"],
            sha256_file(self.manifest),
        )
        self.assertEqual(
            fingerprint["resource_verification"]["path"],
            self._relative(effective_resource),
        )
        self.assertEqual(
            fingerprint["pitch_calibration"]["path"],
            self._relative(effective_pitch),
        )
        self.assertEqual(
            fingerprint["runtime_asset_graph"]["sample_rate_hz"],
            44_100,
        )

    def test_runtime_fingerprint_rejects_invalid_sample_rates(self) -> None:
        for sample_rate_hz in (True, 7_999, 384_001, 44_100.0):
            with self.subTest(sample_rate_hz=sample_rate_hz):
                with self.assertRaisesRegex(OnsetEvidenceError, "sample_rate_hz"):
                    compute_runtime_fingerprint(
                        self.root,
                        self.manifest,
                        sample_rate_hz=sample_rate_hz,
                    )

    def test_render_closure_ignores_unrelated_cli_and_mcp_sources(self) -> None:
        frozen = compute_runtime_fingerprint(self.root, self.manifest)
        closure = frozen["render_python_closure"]
        paths = [record["path"] for record in closure["files"]]
        self.assertIn("tianlai/renderer.py", paths)
        self.assertIn("tianlai/sampler.py", paths)
        self.assertNotIn("tianlai/cli.py", paths)
        self.assertNotIn("tianlai/mcp_server.py", paths)
        self.assertNotIn("tianlai/runtime.py", paths)
        self.assertFalse(
            frozen["runtime_dependencies"]["pyfluidsynth"]["applicable"]
        )

        mcp_source = self.root / "tianlai" / "mcp_server.py"
        mcp_source.write_text(
            "# unrelated MCP change must not stale render evidence\n",
            encoding="utf-8",
        )
        self.assertEqual(
            validate_runtime_fingerprint(
                frozen,
                project_root=self.root,
                manifest_path=self.manifest,
            ),
            frozen,
        )

    def test_render_closure_stales_for_each_bound_render_source(self) -> None:
        targets = (
            self.root / "tianlai" / "renderer.py",
            self.root / "tianlai" / "sampler.py",
            self.implementation,
        )
        for target in targets:
            with self.subTest(target=target.name):
                frozen = compute_runtime_fingerprint(self.root, self.manifest)
                original = target.read_text(encoding="utf-8")
                target.write_text(
                    original + "\n# render-affecting revision\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    OnsetEvidenceError,
                    "runtime fingerprint is stale",
                ):
                    validate_runtime_fingerprint(
                        frozen,
                        project_root=self.root,
                        manifest_path=self.manifest,
                    )
                target.write_text(original, encoding="utf-8")
                validate_runtime_fingerprint(
                    frozen,
                    project_root=self.root,
                    manifest_path=self.manifest,
                )

    def test_dynamic_backend_module_is_explicitly_bound(self) -> None:
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "synthetic backend fixture",
                    "type": "synthesizer",
                    "resource_verification": self.resource.name,
                    "pitch_calibration": self.pitch.name,
                    "default_articulation": "sustain",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        frozen = compute_runtime_fingerprint(self.root, self.manifest)
        closure = frozen["render_python_closure"]
        self.assertIn("tianlai.synthesizer", closure["entry_modules"])
        self.assertIn(
            "tianlai/synthesizer.py",
            [record["path"] for record in closure["files"]],
        )

        backend = self.root / "tianlai" / "synthesizer.py"
        backend.write_text(
            "from .events import TEST_EVENTS\nBACKEND_REVISION = 2\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            OnsetEvidenceError,
            "runtime fingerprint is stale",
        ):
            validate_runtime_fingerprint(
                frozen,
                project_root=self.root,
                manifest_path=self.manifest,
            )

    def test_runtime_dependency_identity_is_fail_closed(self) -> None:
        frozen = compute_runtime_fingerprint(self.root, self.manifest)
        frozen["runtime_dependencies"]["numpy"]["version"] += "-tampered"
        with self.assertRaisesRegex(
            OnsetEvidenceError,
            "runtime fingerprint is stale",
        ):
            validate_runtime_fingerprint(
                frozen,
                project_root=self.root,
                manifest_path=self.manifest,
            )

    def test_runtime_asset_replacement_stales_fingerprint_without_report_edit(
        self,
    ) -> None:
        sample = self.instrument_dir / "runtime.wav"
        self._write_wav(sample, frames=800)
        self.manifest.write_text(
            json.dumps(
                {
                    "name": "采样测试琴",
                    "type": "sample",
                    "asset_root": ".",
                    "resource_verification": "资源核验.json",
                    "pitch_calibration": "音准校准.json",
                    "regions": [
                        {
                            "sample": "runtime.wav",
                            "root_midi": 60,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        frozen = compute_runtime_fingerprint(self.root, self.manifest)
        self.assertEqual(frozen["runtime_asset_graph"]["file_count"], 1)
        with sample.open("r+b") as output:
            output.seek(-1, os.SEEK_END)
            output.write(b"\x01")
            output.flush()
            os.fsync(output.fileno())
        with self.assertRaisesRegex(OnsetEvidenceError, "runtime fingerprint is stale"):
            validate_runtime_fingerprint(
                frozen,
                project_root=self.root,
                manifest_path=self.manifest,
            )

    def test_pending_review_cannot_finalize_or_approve(self) -> None:
        candidate = self._candidate()
        review = self.probes / "review.json"
        create_review_draft(
            candidate,
            review,
            project_root=self.root,
            reviewer_id="human",
        )
        with self.assertRaisesRegex(OnsetEvidenceError, "mutually exclusive"):
            record_review_decision(
                review,
                project_root=self.root,
                observation_id="obs-00",
                status="measured",
                measured_onset_frame=580,
                measured_delay_frames=100,
            )
        with self.assertRaisesRegex(OnsetEvidenceError, "remain unreviewed"):
            finalize_review(review, project_root=self.root)
        with self.assertRaisesRegex(OnsetEvidenceError, "not been finalized"):
            promote_review(
                candidate,
                review,
                self.instrument_dir / "发音延迟.json",
                project_root=self.root,
                explicit_approval=True,
                review_lead="lead",
            )
        with self.assertRaisesRegex(OnsetEvidenceError, "explicit_approval"):
            promote_review(
                candidate,
                review,
                self.instrument_dir / "发音延迟.json",
                project_root=self.root,
                explicit_approval=False,
                review_lead="lead",
            )

    def test_unresolved_machine_observation_blocks_even_if_excluded(self) -> None:
        candidate = self._candidate(statuses=("unresolved",))
        review = self.probes / "review.json"
        create_review_draft(
            candidate,
            review,
            project_root=self.root,
            reviewer_id="human",
        )
        record_review_decision(
            review,
            project_root=self.root,
            observation_id="obs-00",
            status="exclude",
            comment="machine could not resolve this probe",
        )
        finalize_review(review, project_root=self.root)
        with self.assertRaisesRegex(OnsetEvidenceError, "unresolved machine"):
            promote_review(
                candidate,
                review,
                self.instrument_dir / "发音延迟.json",
                project_root=self.root,
                explicit_approval=True,
                review_lead="lead",
            )

    def test_unsure_human_decision_blocks_approval(self) -> None:
        candidate = self._candidate(statuses=("proposed",))
        review = self.probes / "review.json"
        create_review_draft(
            candidate,
            review,
            project_root=self.root,
            reviewer_id="human",
        )
        record_review_decision(
            review,
            project_root=self.root,
            observation_id="obs-00",
            status="unsure",
            comment="I cannot identify the perceptual onset confidently",
        )
        finalize_review(review, project_root=self.root)
        with self.assertRaisesRegex(OnsetEvidenceError, "human unsure"):
            promote_review(
                candidate,
                review,
                self.instrument_dir / "发音延迟.json",
                project_root=self.root,
                explicit_approval=True,
                review_lead="lead",
            )

    def test_more_than_30ms_spread_rejects_scalar_approval(self) -> None:
        candidate = self._candidate(statuses=("proposed", "proposed"))
        review = self._complete_measured_review(candidate, (0, 1600))
        with self.assertRaisesRegex(OnsetEvidenceError, "single scalar"):
            promote_review(
                candidate,
                review,
                self.instrument_dir / "发音延迟.json",
                project_root=self.root,
                explicit_approval=True,
                review_lead="lead",
            )

    def test_runtime_default_only_candidate_can_never_be_promoted(self) -> None:
        candidate = self._candidate(
            statuses=("proposed",),
            variant_coverage="runtime_default_only",
        )
        review = self._complete_measured_review(candidate, (100,))
        with self.assertRaisesRegex(
            OnsetEvidenceError,
            "all_runtime_variants",
        ):
            promote_review(
                candidate,
                review,
                self.instrument_dir / "发音延迟.json",
                project_root=self.root,
                explicit_approval=True,
                review_lead="lead",
            )

    def test_changing_only_variant_coverage_cannot_forge_certification(
        self,
    ) -> None:
        candidate = self._candidate(
            statuses=("proposed",),
            variant_coverage="runtime_default_only",
        )
        document = read_json_strict(candidate)
        document["protocol"]["variant_coverage"] = "all_runtime_variants"
        document["candidate_sha256"] = canonical_sha256(
            document,
            omit="candidate_sha256",
        )
        write_json_atomic(candidate, document)
        with self.assertRaisesRegex(
            OnsetEvidenceError,
            "condition coverage|variant evidence",
        ):
            load_candidate_report(candidate, project_root=self.root)

    def test_certified_probe_rejects_private_sampler_event_controls(
        self,
    ) -> None:
        candidate = self._candidate(statuses=("proposed",))
        document = read_json_strict(candidate)
        observation = document["observations"][0]
        performance_path = self.root / observation["performance_path"]
        performance = read_json_strict(performance_path)
        note_on = next(
            event
            for event in performance["events"]
            if event["type"] == "note_on"
        )
        note_on["_sample_ignore_pitch"] = True
        performance_path.write_text(
            json.dumps(performance, ensure_ascii=False),
            encoding="utf-8",
        )
        observation["performance_sha256"] = sha256_file(performance_path)
        document["candidate_sha256"] = canonical_sha256(
            document,
            omit="candidate_sha256",
        )
        write_json_atomic(candidate, document)
        with self.assertRaisesRegex(
            OnsetEvidenceError,
            "unknown fields.*_sample_ignore_pitch",
        ):
            load_candidate_report(candidate, project_root=self.root)

    def test_implicit_backend_default_uses_reserved_articulation_sentinel(
        self,
    ) -> None:
        candidate = self._candidate(
            statuses=("proposed",),
            variant_coverage="runtime_default_only",
        )
        document = read_json_strict(candidate)
        observation = document["observations"][0]
        performance_path = self.root / observation["performance_path"]
        performance = read_json_strict(performance_path)
        performance["events"] = [
            event
            for event in performance["events"]
            if event["type"] != "articulation"
        ]
        performance_path.write_text(
            json.dumps(performance, ensure_ascii=False),
            encoding="utf-8",
        )
        observation["performance_sha256"] = sha256_file(performance_path)
        observation["final_articulation"] = "__default__"
        document["candidate_sha256"] = canonical_sha256(
            document,
            omit="candidate_sha256",
        )
        write_json_atomic(candidate, document)
        loaded = load_candidate_report(candidate, project_root=self.root)
        self.assertEqual(
            loaded["observations"][0]["final_articulation"],
            "__default__",
        )

    def test_dangerous_paths_nonfinite_and_atomic_replace(self) -> None:
        candidate = self._candidate()
        review = self.probes / "review.json"
        create_review_draft(
            candidate,
            review,
            project_root=self.root,
            reviewer_id="human",
        )
        with tempfile.TemporaryDirectory(
            prefix="tianlai_outside_onset_review_"
        ) as outside_directory:
            outside_review = Path(outside_directory) / "review.json"
            outside_review.write_bytes(review.read_bytes())
            original_bytes = outside_review.read_bytes()
            with self.assertRaisesRegex(OnsetEvidenceError, "leaves project root"):
                record_review_decision(
                    outside_review,
                    project_root=self.root,
                    observation_id="obs-00",
                    status="measured",
                    measured_onset_frame=580,
                )
            self.assertEqual(outside_review.read_bytes(), original_bytes)

        document = read_json_strict(candidate)
        document["observations"][0]["wav_path"] = "../outside.wav"
        document["candidate_sha256"] = canonical_sha256(
            document,
            omit="candidate_sha256",
        )
        write_json_atomic(candidate, document)
        with self.assertRaisesRegex(OnsetEvidenceError, "unsafe"):
            load_candidate_report(candidate, project_root=self.root)
        with self.assertRaises(OnsetEvidenceError):
            canonical_sha256({"bad": math.inf})

        target = self.probes / "atomic.json"
        with mock.patch(
            "tianlai.onset_evidence.os.replace",
            wraps=os.replace,
        ) as replace:
            write_json_atomic(target, {"ok": True})
        replace.assert_called_once()
        self.assertEqual(read_json_strict(target), {"ok": True})
        self.assertFalse(any(self.probes.glob(".atomic.json.*.tmp")))

    def test_json_schemas_accept_generated_chain_when_jsonschema_available(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is an optional development dependency")
        candidate_path = self._candidate()
        review_path = self._complete_measured_review(
            candidate_path,
            (100, 110, 120),
        )
        approved_path = self.instrument_dir / "发音延迟.json"
        promote_review(
            candidate_path,
            review_path,
            approved_path,
            project_root=self.root,
            explicit_approval=True,
            review_lead="lead",
        )
        pairs = (
            (
                candidate_path,
                Path("schemas/onset-candidate-report.schema.json"),
            ),
            (
                review_path,
                Path("schemas/onset-review-decision.schema.json"),
            ),
            (
                approved_path,
                Path("schemas/approved-onset-evidence.schema.json"),
            ),
        )
        for document_path, schema_path in pairs:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.Draft202012Validator(
                schema,
                format_checker=jsonschema.FormatChecker(),
            ).validate(read_json_strict(document_path))


if __name__ == "__main__":
    unittest.main()
