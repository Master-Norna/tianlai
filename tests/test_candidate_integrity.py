from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tianlai.candidate_integrity as candidate_integrity
from tianlai.candidate import (
    CANDIDATE_MANIFEST_NAME,
    canonical_json_sha256,
    prepare_candidate_target,
    publish_candidate_metadata,
    sha256_file,
)
from tianlai.candidate_integrity import (
    CandidateIntegrityError,
    verify_candidate_integrity,
)


_INTEGRITY_ERRORS = (OSError, RuntimeError, ValueError)


def _write_json(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _score() -> dict[str, object]:
    return {
        "schema_version": 1,
        "title": "candidate integrity test",
        "sample_rate": 8_000,
        "tail_seconds": 0.25,
        "tempo_map": [
            {
                "bar": 1,
                "bpm": 60,
                "beats_per_bar": 4,
                "beat_unit": 4,
            }
        ],
        "parts": [
            {
                "id": "lead",
                "notes": [
                    {
                        "event_id": "event-000001",
                        "bar": 1,
                        "beat": 1,
                        "duration_beats": 1,
                        "pitch": "C4",
                        "velocity": 0.7,
                    }
                ],
            }
        ],
    }


def _plan() -> dict[str, object]:
    return {
        "title": "candidate integrity test",
        "sample_rate": 8_000,
        "duration_seconds": 2.0,
        "parts": [
            {
                "executor_id": "lead",
                "part_id": "lead",
                "instrument": "test/reference-oscillator",
                "trace": [
                    {
                        "source_event_id": "event-000001",
                        "时间": 0.5,
                        "时长": 1.0,
                        "小节": 1,
                        "拍": 1,
                        "音": "C4",
                        "奏法": "sustain",
                    }
                ],
            }
        ],
    }


def _publish_candidate(output_root: Path) -> Path:
    score = _score()
    roster = {
        "name": "test",
        "assignments": [
            {
                "part": "lead",
                "instrument": "test/reference-oscillator",
            }
        ],
    }
    render_profile = {
        "kind": "tianlai.render_profile",
        "schema_version": 1,
        "name": "test-dry",
        "expression": "strict",
        "range_mode": "compatibility",
        "seed": 0,
        "master_gain_db": 0.0,
        "normalize_peak_db": None,
        "space": {"enabled": False},
        "collaboration_mode": None,
        "write_stems": True,
        "use_stem_cache": False,
        "refresh_stem_cache": False,
    }
    plan = _plan()
    plan_sha256 = canonical_json_sha256(plan)
    target = prepare_candidate_target(
        output_root,
        "candidate integrity test",
        output_id="candidate-integrity",
        plan_sha256=plan_sha256,
    )
    directory = target.directory

    plan_path = directory / "演奏计划.json"
    _write_json(plan_path, plan)
    mix_path = directory / "合奏.wav"
    mix_path.write_bytes(b"candidate-audio-identity")
    stem_path = directory / "分轨" / "lead.wav"
    stem_path.parent.mkdir(parents=True, exist_ok=True)
    stem_path.write_bytes(b"candidate-stem-identity")
    license_path = directory / "合奏-许可.json"
    _write_json(license_path, {"format": "test-license-sidecar"})
    attribution_path = directory / "署名说明.txt"
    attribution_path.write_text("test attribution\n", encoding="utf-8")

    receipt = {
        "format": "tianlai.render_receipt",
        "version": 2,
        "audio_format": {"sample_rate": 8_000},
        "performance_plan": {
            "path": plan_path.name,
            "file_sha256": sha256_file(plan_path),
            "sha256": plan_sha256,
        },
        "mix": {
            "path": mix_path.name,
            "frame_count": 16_000,
            "sha256": hashlib.sha256(mix_path.read_bytes()).hexdigest(),
        },
        "stems": [
            {
                "wav": {
                    "written": True,
                    "path": stem_path.relative_to(directory).as_posix(),
                    "sha256": sha256_file(stem_path),
                }
            }
        ],
        "license_sidecar": {
            "path": license_path.name,
            "sha256": sha256_file(license_path),
        },
        "attribution_notice": {
            "path": attribution_path.name,
            "sha256": sha256_file(attribution_path),
        },
        "collaboration": {
            "effective_mode": "manual",
            "report_enabled": False,
        },
    }
    receipt_path = directory / "渲染回执.json"
    _write_json(receipt_path, receipt)
    publish_candidate_metadata(
        target,
        title="candidate integrity test",
        score=score,
        roster=roster,
        render_profile=render_profile,
        receipt_path=receipt_path,
        plan_sha256=plan_sha256,
    )
    return directory


def _manifest(directory: Path) -> dict[str, object]:
    return json.loads(
        (directory / CANDIDATE_MANIFEST_NAME).read_text(encoding="utf-8")
    )


def _bound_path(
    directory: Path,
    *keys: str,
) -> Path:
    value: object = _manifest(directory)
    for key in keys:
        if not isinstance(value, dict):
            raise AssertionError(f"candidate binding {keys!r} is not an object")
        value = value[key]
    if not isinstance(value, str):
        raise AssertionError(f"candidate binding {keys!r} is not a path")
    return directory / value


class CandidateIntegrityTests(unittest.TestCase):
    def test_missing_path_has_a_stable_integrity_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing-candidate"

            with self.assertRaises(CandidateIntegrityError) as caught:
                verify_candidate_integrity(missing)

            self.assertEqual(caught.exception.code, "invalid_path")

    @unittest.skipIf(
        os.path.normcase("Artifact.JSON") == os.path.normcase("artifact.json"),
        "requires a case-sensitive filesystem",
    )
    def test_binding_requires_exact_case_sensitive_spelling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary) / "output")
            score_path = _bound_path(directory, "project", "score", "path")
            renamed = score_path.with_name(score_path.name.swapcase())
            score_path.rename(renamed)

            with self.assertRaises(CandidateIntegrityError) as caught:
                verify_candidate_integrity(directory)

            self.assertEqual(caught.exception.code, "path_mismatch")

    def test_valid_closed_world_candidate_has_a_stable_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary) / "output")

            first = verify_candidate_integrity(directory)
            second = verify_candidate_integrity(directory)
            through_manifest = verify_candidate_integrity(
                directory / CANDIDATE_MANIFEST_NAME
            )

            self.assertIs(first["integrity_verified"], True)
            self.assertIs(
                first["integrity"]["bound_entry_set_closed_when_enumerated"],
                True,
            )
            self.assertIs(
                first["integrity"]["live_tree_immutable_after_return"],
                False,
            )
            self.assertIs(
                first["integrity"]["uncooperative_concurrent_writer_excluded"],
                False,
            )
            self.assertEqual(first, second)
            self.assertEqual(first, through_manifest)

    def test_v2_receipt_does_not_require_a_post_render_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary) / "output")
            receipt = json.loads(
                (directory / "渲染回执.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(receipt["version"], 2)
            self.assertNotIn("post_render_check", receipt)

            report = verify_candidate_integrity(directory)

            self.assertIs(report["integrity_verified"], True)

    def test_legacy_v1_mix_report_remains_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary) / "output")
            report_path = directory / "legacy-mix-report.json"
            _write_json(
                report_path,
                {
                    "format": "tianlai.mix_report",
                    "version": 1,
                    "mode": "analyze",
                    "scope": "machine_triage_only",
                },
            )
            receipt_path = _bound_path(
                directory,
                "render_receipt",
                "path",
            )
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["collaboration"] = {
                "effective_mode": "analyze",
                "report_enabled": True,
            }
            receipt["mix_report"] = {
                "path": report_path.name,
                "sha256": sha256_file(report_path),
                "format": "tianlai.mix_report",
                "version": 1,
                "mode": "analyze",
                "scope": "machine_triage_only",
            }
            _write_json(receipt_path, receipt)
            manifest_path = directory / CANDIDATE_MANIFEST_NAME
            manifest = _manifest(directory)
            manifest["render_receipt"]["sha256"] = sha256_file(receipt_path)
            _write_json(manifest_path, manifest)

            result = verify_candidate_integrity(directory)

            self.assertTrue(result["integrity_verified"])
            self.assertTrue(
                result["integrity"]["optional_artifacts"]["mix_report"]
            )

    def test_render_profile_seed_outside_javascript_range_is_compatible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary) / "output")
            profile_path = _bound_path(
                directory,
                "project",
                "render_profile",
                "path",
            )
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile["seed"] = 2**53
            _write_json(profile_path, profile)
            manifest_path = directory / CANDIDATE_MANIFEST_NAME
            manifest = _manifest(directory)
            binding = manifest["project"]["render_profile"]
            binding["canonical_sha256"] = canonical_json_sha256(profile)
            binding["file_sha256"] = sha256_file(profile_path)
            _write_json(manifest_path, manifest)

            result = verify_candidate_integrity(directory)

            self.assertTrue(result["integrity_verified"])

    def test_legacy_candidate_v1_timestamp_remains_verifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary) / "output")
            manifest_path = directory / CANDIDATE_MANIFEST_NAME
            manifest = _manifest(directory)
            manifest["version"] = 1
            manifest["created_at_utc"] = "2026-08-13T00:00:00+00:00"
            _write_json(manifest_path, manifest)

            result = verify_candidate_integrity(directory)

            self.assertEqual(result["candidate"]["version"], 1)
            self.assertEqual(result["candidate"]["render_receipt_version"], 2)

    def test_unbound_file_or_directory_is_rejected(self) -> None:
        for extra_kind in ("file", "directory"):
            with self.subTest(extra_kind=extra_kind):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = _publish_candidate(
                        Path(temporary) / "output"
                    )
                    extra = directory / "unbound"
                    if extra_kind == "file":
                        extra.write_text("not in the receipt\n", encoding="utf-8")
                    else:
                        extra.mkdir()

                    with self.assertRaises(_INTEGRITY_ERRORS):
                        verify_candidate_integrity(directory)

    def test_missing_or_tampered_bound_artifact_is_rejected(self) -> None:
        for mutation in ("missing", "tampered"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = _publish_candidate(
                        Path(temporary) / "output"
                    )
                    if mutation == "missing":
                        (directory / "署名说明.txt").unlink()
                    else:
                        mix = directory / "合奏.wav"
                        mix.write_bytes(mix.read_bytes() + b"tampered")

                    with self.assertRaises(_INTEGRITY_ERRORS):
                        verify_candidate_integrity(directory)

    def test_hard_linked_bound_artifact_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish_candidate(root / "output")
            score_path = _bound_path(directory, "project", "score", "path")
            external = root / "shared-score.json"
            external.write_bytes(score_path.read_bytes())
            score_path.unlink()
            try:
                os.link(external, score_path)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable: {exc}")

            with self.assertRaises(_INTEGRITY_ERRORS):
                verify_candidate_integrity(directory)

    def test_symbolic_linked_bound_artifact_is_rejected_when_supported(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish_candidate(root / "output")
            score_path = _bound_path(directory, "project", "score", "path")
            external = root / "symlink-score.json"
            external.write_bytes(score_path.read_bytes())
            score_path.unlink()
            try:
                os.symlink(external, score_path)
            except OSError as exc:
                self.skipTest(f"symbolic links are unavailable: {exc}")

            with self.assertRaises(_INTEGRITY_ERRORS):
                verify_candidate_integrity(directory)

    def test_manifest_rejects_duplicate_members_and_non_finite_numbers(
        self,
    ) -> None:
        for malformed in ("duplicate-member", "nan"):
            with self.subTest(malformed=malformed):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = _publish_candidate(
                        Path(temporary) / "output"
                    )
                    manifest_path = directory / CANDIDATE_MANIFEST_NAME
                    if malformed == "duplicate-member":
                        payload = manifest_path.read_text(encoding="utf-8")
                        payload = payload.replace(
                            "{\n",
                            '{\n  "format": "duplicate-format",\n',
                            1,
                        )
                    else:
                        document = _manifest(directory)
                        document["title"] = float("nan")
                        payload = (
                            json.dumps(
                                document,
                                ensure_ascii=False,
                                allow_nan=True,
                                indent=2,
                            )
                            + "\n"
                        )
                    manifest_path.write_text(payload, encoding="utf-8")

                    with self.assertRaises(CandidateIntegrityError) as caught:
                        verify_candidate_integrity(directory)

                    self.assertEqual(caught.exception.code, "invalid_json")

    def test_manifest_and_receipt_paths_fail_closed(self) -> None:
        cases = (
            ("manifest-collision", "path_collision"),
            ("manifest-unsafe", "unsafe_path"),
            ("receipt-collision", "path_collision"),
            ("receipt-unsafe", "unsafe_path"),
        )
        for mutation, expected_code in cases:
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = _publish_candidate(
                        Path(temporary) / "output"
                    )
                    if mutation.startswith("manifest-"):
                        path = directory / CANDIDATE_MANIFEST_NAME
                        document = _manifest(directory)
                        project = document["project"]
                        if mutation == "manifest-collision":
                            project["roster"]["path"] = project["score"][
                                "path"
                            ]
                        else:
                            project["score"]["path"] = "../outside.json"
                    else:
                        path = _bound_path(
                            directory,
                            "render_receipt",
                            "path",
                        )
                        document = json.loads(path.read_text(encoding="utf-8"))
                        if mutation == "receipt-collision":
                            document["mix"]["path"] = document[
                                "performance_plan"
                            ]["path"]
                        else:
                            document["mix"]["path"] = "../outside.wav"
                    _write_json(path, document)

                    with self.assertRaises(CandidateIntegrityError) as caught:
                        verify_candidate_integrity(directory)

                    self.assertEqual(caught.exception.code, expected_code)

    def test_late_unbound_entry_added_after_tree_scan_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary) / "output")
            original_scan = candidate_integrity._scan_tree

            def scan_then_add(*args: object, **kwargs: object) -> object:
                captured = original_scan(*args, **kwargs)
                (directory / "late-unbound.txt").write_text(
                    "appeared after enumeration\n",
                    encoding="utf-8",
                )
                return captured

            with mock.patch.object(
                candidate_integrity,
                "_scan_tree",
                side_effect=scan_then_add,
            ):
                with self.assertRaises(CandidateIntegrityError) as caught:
                    verify_candidate_integrity(directory)

            self.assertEqual(caught.exception.code, "generation_changed")

    def test_bound_file_swapped_after_descriptor_hash_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish_candidate(root / "output")
            mix_path = _bound_path(directory, "render_receipt", "path")
            receipt = json.loads(mix_path.read_text(encoding="utf-8"))
            mix_path = directory / receipt["mix"]["path"]
            replacement = root / "replacement.wav"
            replacement.write_bytes(mix_path.read_bytes())
            original_hash = candidate_integrity.sha256_plain_file
            swapped = False

            def hash_then_swap(path: object, **kwargs: object) -> object:
                nonlocal swapped
                captured = original_hash(path, **kwargs)
                if Path(path) == mix_path and not swapped:
                    os.replace(replacement, mix_path)
                    swapped = True
                return captured

            with mock.patch.object(
                candidate_integrity,
                "sha256_plain_file",
                side_effect=hash_then_swap,
            ):
                with self.assertRaises(CandidateIntegrityError) as caught:
                    verify_candidate_integrity(directory)

            self.assertTrue(swapped)
            self.assertEqual(caught.exception.code, "generation_changed")

    def test_bound_file_swapped_during_final_enumeration_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish_candidate(root / "output")
            manifest_path = directory / CANDIDATE_MANIFEST_NAME
            replacement = root / "replacement.json"
            replacement.write_bytes(manifest_path.read_bytes())
            original_scandir = candidate_integrity.os.scandir
            root_scans = 0

            def scandir_then_swap(path: object) -> object:
                nonlocal root_scans
                if Path(path) == directory:
                    root_scans += 1
                    if root_scans == 2:
                        os.replace(replacement, manifest_path)
                return original_scandir(path)

            with mock.patch.object(
                candidate_integrity.os,
                "scandir",
                side_effect=scandir_then_swap,
            ):
                with self.assertRaises(CandidateIntegrityError) as caught:
                    verify_candidate_integrity(directory)

            self.assertEqual(root_scans, 2)
            self.assertEqual(caught.exception.code, "generation_changed")


if __name__ == "__main__":
    unittest.main()
