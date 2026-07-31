from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest import mock

import tianlai.candidate as candidate_module
from tianlai.candidate import (
    CANDIDATE_MANIFEST_NAME,
    CandidateTarget,
    canonical_json_sha256,
    candidate_publication,
    compare_candidates,
    load_candidate,
    locate_candidate,
    prepare_candidate_target,
    publish_candidate_metadata,
    sha256_file,
)
from tianlai.cli import main as cli_main
from tianlai.ensemble import CACHE_TELEMETRY_NAME
from tianlai.render_lock import (
    RenderLockError,
    acquire_render_lock,
    render_lock_path,
)


ROOT = Path(__file__).resolve().parents[1]


def _score(pitch: str = "C4") -> dict:
    return {
        "schema_version": 1,
        "title": "候选合同测试",
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
                        "pitch": pitch,
                        "velocity": 0.7,
                    }
                ],
            }
        ],
    }


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


def _plan(pitch: str = "C4") -> dict:
    return {
        "title": "候选合同测试",
        "sample_rate": 8_000,
        "duration_seconds": 2.0,
        "parts": [
            {
                "executor_id": "lead",
                "part_id": "lead",
                "instrument": "测试工具/参考振荡器",
                "trace": [
                    {
                        "source_event_id": "event-000001",
                        "时间": 0.5,
                        "时长": 1.0,
                        "小节": 1,
                        "拍": 1,
                        "音": pitch,
                        "奏法": "sustain",
                    }
                ],
            }
        ],
    }


def _publish(
    root: Path,
    *,
    output_id: str,
    pitch: str = "C4",
    parent_candidate_id: str | None = None,
) -> Path:
    plan = _plan(pitch)
    plan_sha256 = canonical_json_sha256(plan)
    target = prepare_candidate_target(
        root,
        "候选合同测试",
        output_id=output_id,
        plan_sha256=plan_sha256,
    )
    return _populate_candidate(
        target,
        pitch=pitch,
        parent_candidate_id=parent_candidate_id,
    )


def _populate_candidate(
    target: CandidateTarget,
    *,
    pitch: str = "C4",
    parent_candidate_id: str | None = None,
    score_title: str = "候选合同测试",
    with_cache_telemetry: bool = False,
) -> Path:
    score = _score(pitch)
    score["title"] = score_title
    roster = {
        "name": "test",
        "assignments": [
            {
                "part": "lead",
                "instrument": "测试工具/参考振荡器",
            }
        ],
    }
    profile = {
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
    plan = _plan(pitch)
    plan_sha256 = canonical_json_sha256(plan)
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
        "audio_format": {
            "sample_rate": 8_000,
        },
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
    if with_cache_telemetry:
        _write_json(
            directory / CACHE_TELEMETRY_NAME,
            {
                "format": "tianlai.render_cache_telemetry",
                "version": 1,
                "render_receipt": {
                    "path": receipt_path.name,
                    "sha256": sha256_file(receipt_path),
                },
                "performance_plan": {
                    "canonical_sha256": plan_sha256,
                },
                "mix": {
                    "sha256": receipt["mix"]["sha256"],
                },
                "stem_cache": {
                    "total": 1,
                    "accounted": 1,
                    "unaccounted": 0,
                    "hits": 0,
                    "misses": 0,
                    "bypassed": 1,
                },
                "analysis_cache": None,
            },
        )
    publish_candidate_metadata(
        target,
        title="候选合同测试",
        score=score,
        roster=roster,
        render_profile=profile,
        receipt_path=receipt_path,
        plan_sha256=plan_sha256,
        parent_candidate_id=parent_candidate_id,
    )
    return directory


def _tree_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


class CandidateTests(unittest.TestCase):
    def test_manifest_binds_every_source_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish(
                Path(temporary),
                output_id="first",
            )
            loaded_directory, manifest = load_candidate(directory)

            self.assertEqual(loaded_directory, directory.resolve())
            self.assertEqual(manifest["format"], "tianlai.candidate")
            self.assertIn("render_profile", manifest["project"])
            self.assertTrue((directory / CANDIDATE_MANIFEST_NAME).is_file())

            (directory / "score.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "score file hash mismatch"):
                load_candidate(directory)

    def test_optional_cache_telemetry_is_hash_bound_and_tamper_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="cache-telemetry",
                plan_sha256=canonical_json_sha256(_plan()),
            )
            directory = _populate_candidate(
                target,
                with_cache_telemetry=True,
            )
            _, manifest = load_candidate(directory)
            self.assertEqual(
                manifest["cache_telemetry"]["path"],
                CACHE_TELEMETRY_NAME,
            )

            telemetry_path = directory / CACHE_TELEMETRY_NAME
            telemetry = json.loads(
                telemetry_path.read_text(encoding="utf-8")
            )
            telemetry["stem_cache"]["reason_counts"] = {
                "forged": 1,
            }
            _write_json(telemetry_path, telemetry)
            with self.assertRaisesRegex(
                ValueError,
                "cache telemetry hash mismatch",
            ):
                load_candidate(directory)

    def test_unbound_cache_telemetry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish(
                Path(temporary),
                output_id="unbound-cache-telemetry",
            )
            _write_json(
                directory / CACHE_TELEMETRY_NAME,
                {
                    "format": "tianlai.render_cache_telemetry",
                    "version": 1,
                },
            )

            with self.assertRaisesRegex(
                ValueError,
                "exists without a manifest binding",
            ):
                load_candidate(directory)

    def test_manifest_artifact_cannot_escape_candidate_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="escape")
            manifest_path = directory / CANDIDATE_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            outside = root / "outside.json"
            _write_json(outside, _score())
            manifest["project"]["score"] = {
                "path": "../../outside.json",
                "file_sha256": sha256_file(outside),
                "canonical_sha256": canonical_json_sha256(_score()),
            }
            _write_json(manifest_path, manifest)

            with self.assertRaisesRegex(ValueError, "escapes"):
                load_candidate(directory)

    def test_locate_uses_saved_plan_not_a_recompiled_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish(
                Path(temporary),
                output_id="heard",
            )

            active = locate_candidate(directory, at_seconds=0.75)
            tail = locate_candidate(directory, at_seconds=1.75)

            self.assertEqual(
                active["active_events"][0]["source_event_id"],
                "event-000001",
            )
            self.assertEqual(active["active_events"][0]["pitch"], "C4")
            self.assertFalse(active["summary"]["truncated"])
            self.assertEqual(
                tail["possible_release_or_space_sources"][0][
                    "source_event_id"
                ],
                "event-000001",
            )

    def test_overwrite_requires_the_current_receipt_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="fixed")
            receipt_sha256 = sha256_file(directory / "渲染回执.json")

            with self.assertRaises(FileExistsError):
                prepare_candidate_target(
                    root,
                    "候选合同测试",
                    output_id="fixed",
                )
            with self.assertRaisesRegex(ValueError, "预期 Hash"):
                prepare_candidate_target(
                    root,
                    "候选合同测试",
                    output_id="fixed",
                    overwrite=True,
                    expected_receipt_sha256="0" * 64,
                )
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="fixed",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )
            self.assertTrue(target.replacing)

    def test_failed_new_publication_leaves_no_candidate_or_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="failed-new",
            )

            with self.assertRaisesRegex(RuntimeError, "render failed"):
                with candidate_publication(target) as staging:
                    _populate_candidate(staging)
                    raise RuntimeError("render failed")

            self.assertFalse(target.directory.exists())
            self.assertEqual(
                [],
                list(
                    target.directory.parent.glob(
                        f".{target.candidate_id}.*.staging"
                    )
                ),
            )

    def test_new_publication_preserves_candidate_created_during_render(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="raced-new",
            )

            with self.assertRaisesRegex(
                FileExistsError,
                "渲染期间被创建",
            ):
                with candidate_publication(target) as staging:
                    _populate_candidate(staging, pitch="D4")
                    _populate_candidate(target, pitch="C4")

            _, manifest = load_candidate(target.directory)
            score = json.loads(
                (
                    target.directory
                    / manifest["project"]["score"]["path"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(score["parts"][0]["notes"][0]["pitch"], "C4")

    def test_overwrite_swap_failure_restores_old_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="rollback")
            before = _tree_snapshot(directory)
            receipt_sha256 = sha256_file(directory / "渲染回执.json")
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="rollback",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )
            real_replace = os.replace
            staged_directory: Path | None = None

            def fail_new_directory_swap(
                source: str | os.PathLike[str],
                destination: str | os.PathLike[str],
            ) -> None:
                source_path = Path(source).resolve()
                destination_path = Path(destination).resolve()
                if (
                    staged_directory is not None
                    and source_path == staged_directory
                    and destination_path == directory.resolve()
                ):
                    raise PermissionError("simulated Windows directory lock")
                real_replace(source, destination)

            with mock.patch(
                "tianlai.candidate.os.replace",
                side_effect=fail_new_directory_swap,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "simulated Windows directory lock",
                ):
                    with candidate_publication(target) as staging:
                        staged_directory = staging.directory.resolve()
                        _populate_candidate(staging, pitch="D4")

            self.assertEqual(_tree_snapshot(directory), before)
            load_candidate(directory)
            self.assertEqual(
                [],
                list(directory.parent.glob(f".{directory.name}.*.previous")),
            )

    def test_successful_overwrite_replaces_one_complete_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="replace")
            receipt_sha256 = sha256_file(directory / "渲染回执.json")
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="replace",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )

            with candidate_publication(target) as staging:
                _populate_candidate(staging, pitch="D4")

            published_directory, manifest = load_candidate(directory)
            score = json.loads(
                (
                    published_directory
                    / manifest["project"]["score"]["path"]
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(score["parts"][0]["notes"][0]["pitch"], "D4")
            self.assertEqual(
                [],
                list(directory.parent.glob(f".{directory.name}.*.previous")),
            )
            self.assertEqual(
                [],
                list(
                    directory.parent.glob(
                        f".{target.candidate_id}.*.staging"
                    )
                ),
            )

    def test_publication_verifies_every_receipt_bound_artifact(self) -> None:
        artifacts = (
            "演奏计划.json",
            "合奏.wav",
            "分轨/lead.wav",
            "合奏-许可.json",
            "署名说明.txt",
        )
        for index, relative in enumerate(artifacts):
            with self.subTest(artifact=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    target = prepare_candidate_target(
                        root,
                        "候选合同测试",
                        output_id=f"tampered-{index}",
                    )

                    with self.assertRaises(RuntimeError):
                        with candidate_publication(target) as staging:
                            _populate_candidate(staging)
                            artifact = staging.directory / relative
                            artifact.write_bytes(
                                artifact.read_bytes() + b"tampered"
                            )

                    self.assertFalse(target.directory.exists())

    def test_post_rename_verification_rolls_back_old_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="post-rename")
            before = _tree_snapshot(directory)
            receipt_sha256 = sha256_file(directory / "渲染回执.json")
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="post-rename",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )
            real_commit = candidate_module._commit_candidate_staging

            def tamper_after_preflight(
                staging: Path,
                final_target: CandidateTarget,
            ) -> Path | None:
                (staging / "score.json").write_text(
                    "{}\n",
                    encoding="utf-8",
                )
                return real_commit(staging, final_target)

            with mock.patch(
                "tianlai.candidate._commit_candidate_staging",
                side_effect=tamper_after_preflight,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "score file hash mismatch",
                ):
                    with candidate_publication(target) as staging:
                        _populate_candidate(staging, pitch="D4")

            self.assertEqual(_tree_snapshot(directory), before)
            load_candidate(directory)
            self.assertEqual(
                [],
                list(directory.parent.glob(f".{directory.name}.*.previous")),
            )

    def test_manifest_hash_blocks_same_receipt_aba_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="aba")
            receipt_sha256 = sha256_file(directory / "渲染回执.json")
            first = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="aba",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )
            stale = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="aba",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )

            with candidate_publication(first) as staging:
                _populate_candidate(
                    staging,
                    score_title="first concurrent writer",
                )
            self.assertEqual(
                sha256_file(directory / "渲染回执.json"),
                receipt_sha256,
            )

            with self.assertRaisesRegex(ValueError, "发生变化"):
                with candidate_publication(stale):
                    self.fail("stale writer must fail before rendering")

            score = json.loads(
                (directory / "score.json").read_text(encoding="utf-8")
            )
            self.assertEqual(score["title"], "first concurrent writer")

    def test_publication_owns_the_stable_final_candidate_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = prepare_candidate_target(
                Path(temporary),
                "候选合同测试",
                output_id="locked-final",
            )

            with acquire_render_lock(target.directory):
                with self.assertRaises(RenderLockError):
                    with candidate_publication(target):
                        self.fail("a second publisher must not enter")

    def test_cleanup_warning_cannot_turn_commit_into_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="cleanup-warning")
            receipt_sha256 = sha256_file(directory / "渲染回执.json")
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="cleanup-warning",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )
            real_rmtree = candidate_module.shutil.rmtree

            def fail_backup_cleanup(
                path: str | os.PathLike[str],
                *args: object,
                **kwargs: object,
            ) -> None:
                if Path(path).name.endswith(".previous"):
                    raise PermissionError("simulated locked backup")
                real_rmtree(path, *args, **kwargs)

            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                with mock.patch(
                    "tianlai.candidate.shutil.rmtree",
                    side_effect=fail_backup_cleanup,
                ):
                    with candidate_publication(target) as staging:
                        _populate_candidate(staging, pitch="D4")

            score = json.loads(
                (directory / "score.json").read_text(encoding="utf-8")
            )
            self.assertEqual(score["parts"][0]["notes"][0]["pitch"], "D4")
            self.assertEqual(
                1,
                len(
                    list(
                        directory.parent.glob(
                            f".{directory.name}.*.previous"
                        )
                    )
                ),
            )
            with self.assertRaisesRegex(RuntimeError, "失败关闭"):
                prepare_candidate_target(
                    root,
                    "候选合同测试",
                    output_id="cleanup-warning",
                    overwrite=True,
                    expected_receipt_sha256=sha256_file(
                        directory / "渲染回执.json"
                    ),
                )

    def test_prepare_recovers_one_self_identifying_previous_generation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="recover")
            receipt_sha256 = sha256_file(directory / "渲染回执.json")
            interrupted = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="recover",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )
            self.assertIsNotNone(interrupted.expected_manifest_sha256)
            backup = directory.with_name(
                f".{directory.name}."
                f"{interrupted.expected_manifest_sha256}.previous"
            )
            os.replace(directory, backup)

            recovered = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="recover",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )

            self.assertTrue(recovered.replacing)
            self.assertEqual(
                recovered.expected_manifest_sha256,
                interrupted.expected_manifest_sha256,
            )
            self.assertTrue(directory.is_dir())
            self.assertFalse(backup.exists())
            load_candidate(directory)

    def test_publication_rejects_manifest_target_identity_mismatch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = prepare_candidate_target(
                Path(temporary),
                "候选合同测试",
                output_id="identity",
            )

            with self.assertRaisesRegex(ValueError, "candidate_id"):
                with candidate_publication(target) as staging:
                    _populate_candidate(staging)
                    manifest_path = (
                        staging.directory / CANDIDATE_MANIFEST_NAME
                    )
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    manifest["candidate_id"] = "foreign-candidate"
                    _write_json(manifest_path, manifest)

            self.assertFalse(target.directory.exists())

    def test_compare_reports_score_and_parent_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = _publish(root, output_id="before")
            before_manifest = load_candidate(before)[1]
            after = _publish(
                root,
                output_id="after",
                pitch="D4",
                parent_candidate_id=before_manifest["candidate_id"],
            )

            result = compare_candidates(before, after)

            self.assertTrue(result["parent_relationship"])
            self.assertEqual(result["score"]["counts"]["updated"], 1)
            self.assertTrue(result["performance_plan_changed"])

    def test_project_render_cli_publishes_a_verified_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            score = _score()
            score["tempo_map"][0]["bpm"] = 240
            score["parts"][0]["notes"][0]["duration_beats"] = 0.25
            score["parts"][0]["notes"][0]["articulation"] = "zhenggu"
            score["tail_seconds"] = 0.05
            roster = {
                "name": "reference",
                "assignments": [
                    {
                        "part": "lead",
                        "instrument": "世界乐器/编钟",
                    }
                ],
            }
            profile = {
                "kind": "tianlai.render_profile",
                "schema_version": 1,
                "name": "test-fast",
                "expression": "strict",
                "range_mode": "compatibility",
                "seed": 0,
                "master_gain_db": 0.0,
                "normalize_peak_db": None,
                "space": {"enabled": False},
                "collaboration_mode": None,
                "write_stems": False,
                "use_stem_cache": False,
                "refresh_stem_cache": False,
            }
            score_path = root / "score.json"
            roster_path = root / "roster.json"
            profile_path = root / "profile.json"
            output_root = root / "candidates"
            _write_json(score_path, score)
            _write_json(roster_path, roster)
            _write_json(profile_path, profile)
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                status = cli_main(
                    [
                        "project-render",
                        "--score",
                        str(score_path),
                        "--roster",
                        str(roster_path),
                        "--render-profile",
                        str(profile_path),
                        "--output-root",
                        str(output_root),
                        "--output-id",
                        "cli-smoke",
                        "--root",
                        str(ROOT / "乐器"),
                    ]
                )

            self.assertEqual(status, 0, stdout.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertTrue(result["ok"])
            directory = Path(result["candidate_directory"])
            self.assertTrue((directory / "合奏.wav").is_file())
            for key in (
                "candidate_manifest",
                "mix_wav",
                "render_receipt",
            ):
                published_path = Path(result[key])
                self.assertTrue(published_path.is_file(), key)
                self.assertEqual(
                    published_path.resolve().parent,
                    directory.resolve(),
                    key,
                )
                self.assertNotIn(".staging", str(published_path))
            self.assertEqual(
                load_candidate(directory)[1]["candidate_id"],
                "cli-smoke-"
                + hashlib.sha256(b"cli-smoke").hexdigest()[:10],
            )
            self.assertEqual(
                [render_lock_path(directory).resolve()],
                sorted(
                    (
                        path.resolve()
                        for path in directory.parent.glob(
                            ".tianlai-render-*.lock"
                        )
                    ),
                    key=str,
                ),
            )

    def test_project_render_cli_seat_only_rerender_hits_analysis_cache(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            score = {
                "schema_version": 1,
                "title": "CLI analysis cache",
                "sample_rate": 8_000,
                "tail_seconds": 0.05,
                "tempo_map": [
                    {
                        "bar": 1,
                        "bpm": 240,
                        "beats_per_bar": 4,
                        "beat_unit": 4,
                    }
                ],
                "parts": [
                    {
                        "id": "pad",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 1,
                                "pitch": "C4",
                                "event_id": "pad-1",
                            }
                        ],
                    },
                    {
                        "id": "lead",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 1,
                                "pitch": "E4",
                                "event_id": "lead-1",
                            }
                        ],
                    },
                ],
            }

            def roster(distance_m: float) -> dict:
                return {
                    "name": "CLI cache roster",
                    "collaboration": {
                        "mode": "analyze",
                        "analysis": {
                            "metric": "overlap_active_rms",
                            "window_ms": 100,
                            "hop_ms": 50,
                            "gate_dbfs": -70,
                        },
                        "balance_relations": [
                            {
                                "subject": "pad",
                                "reference": "lead",
                                "target_offset_db": 0,
                                "tolerance_db": 2,
                                "max_suggestion_db": 3,
                            }
                        ],
                    },
                    "assignments": [
                        {
                            "part": "pad",
                            "instrument": "测试工具/参考振荡器",
                            "role": {
                                "function": "pad",
                                "prominence": "background",
                            },
                            "seat": {
                                "azimuth_deg": -10,
                                "distance_m": distance_m,
                            },
                        },
                        {
                            "part": "lead",
                            "instrument": "测试工具/参考振荡器",
                            "role": {
                                "function": "lead",
                                "prominence": "foreground",
                            },
                            "seat": {
                                "azimuth_deg": 10,
                                "distance_m": 2,
                            },
                        },
                    ],
                }

            profile = {
                "kind": "tianlai.render_profile",
                "schema_version": 1,
                "name": "cli-cache",
                "expression": "strict",
                "range_mode": "compatibility",
                "seed": 0,
                "master_gain_db": -6,
                "normalize_peak_db": None,
                "space": {"enabled": False},
                "collaboration_mode": None,
                "write_stems": False,
                "use_stem_cache": True,
                "refresh_stem_cache": False,
            }
            score_path = root / "score.json"
            roster_path = root / "roster.json"
            profile_path = root / "profile.json"
            output_root = root / "candidates"
            _write_json(score_path, score)
            _write_json(roster_path, roster(3.0))
            _write_json(profile_path, profile)

            def run(output_id: str) -> dict:
                stdout = io.StringIO()
                with redirect_stdout(stdout):
                    status = cli_main(
                        [
                            "project-render",
                            "--score",
                            str(score_path),
                            "--roster",
                            str(roster_path),
                            "--render-profile",
                            str(profile_path),
                            "--output-root",
                            str(output_root),
                            "--output-id",
                            output_id,
                            "--root",
                            str(ROOT / "乐器"),
                        ]
                    )
                self.assertEqual(status, 0, stdout.getvalue())
                return json.loads(stdout.getvalue())

            run("cold")
            _write_json(roster_path, roster(8.0))
            hot = run("seat-hot")
            directory = Path(hot["candidate_directory"])
            telemetry = json.loads(
                (directory / CACHE_TELEMETRY_NAME).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                telemetry["analysis_cache"]["stem"]["hits"],
                2,
            )
            self.assertEqual(
                telemetry["analysis_cache"]["relation"]["hits"],
                1,
            )
            self.assertEqual(
                telemetry["analysis_cache"][
                    "performed_fft_input_frame_visits"
                ],
                0,
            )
            manifest = load_candidate(directory)[1]
            self.assertEqual(
                manifest["cache_telemetry"]["sha256"],
                sha256_file(directory / CACHE_TELEMETRY_NAME),
            )


if __name__ == "__main__":
    unittest.main()
