from __future__ import annotations

import copy
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
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
    capture_plain_directory,
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


def _json_with_duplicate_first_member(path: Path) -> bytes:
    payload = path.read_bytes()
    document = json.loads(payload)
    if not isinstance(document, dict) or not document:
        raise AssertionError("test fixture must be a non-empty JSON object")
    key = next(iter(document))
    duplicate = json.dumps(
        {key: document[key]},
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")[1:-1]
    opening = payload.index(b"{")
    return payload[: opening + 1] + duplicate + b"," + payload[opening + 1 :]


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
    authoring_roster: dict | None = None,
    roster_instrument: str = "测试工具/参考振荡器",
    plan_instrument: str = "测试工具/参考振荡器",
    plan_articulation_auto: bool = True,
) -> Path:
    score = _score(pitch)
    score["title"] = score_title
    roster = {
        "name": "test",
        "assignments": [
            {
                "part": "lead",
                "instrument": roster_instrument,
            }
        ],
    }
    if authoring_roster is not None:
        roster = {
            key: copy.deepcopy(value)
            for key, value in authoring_roster.items()
            if key not in {"kind", "schema_version"}
        }
        roster["assignments"][0]["instrument"] = roster_instrument
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
    if authoring_roster is not None:
        plan["roster"] = "test"
        plan["parts"][0].update(
            {
                "instrument": plan_instrument,
                "instrument_name": "candidate identity test",
                "gain_db": 0.0,
                "pan": 0.0,
                "seat": {"azimuth_deg": 0.0, "distance_m": 3.0},
                "transpose": 0,
                "duration_scale": 1.0,
                "dynamic_compression": 0.0,
                "articulation_auto": plan_articulation_auto,
                "articulation_map": {},
                "kit_pitch": None,
            }
        )
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
    authoring_project: dict | None = None
    if authoring_roster is not None:
        project_id = "a" * 32
        authoring_roster_sha256 = canonical_json_sha256(authoring_roster)
        revision = canonical_json_sha256(
            {
                "kind": "tianlai.authoring_revision_binding",
                "schema_version": 1,
                "project_id": project_id,
                "documents": {
                    "score": canonical_json_sha256(score),
                    "authoring_roster": authoring_roster_sha256,
                    "render_profile": canonical_json_sha256(profile),
                },
            }
        )
        receipt["authoring_project"] = {
            "project_id": project_id,
            "revision": revision,
            "authoring_roster_canonical_sha256": authoring_roster_sha256,
        }
        authoring_project = {
            "project_id": project_id,
            "revision": revision,
            "authoring_roster": authoring_roster,
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
        authoring_project=authoring_project,
    )
    return directory


def _tree_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


class CandidateTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows 8.3 paths are required")
    def test_candidate_round_trip_accepts_a_short_name_output_ancestor(
        self,
    ) -> None:
        import ctypes

        with tempfile.TemporaryDirectory(
            prefix="Tianlai candidate parent with spaces "
        ) as temporary:
            long_parent = Path(temporary)
            buffer = ctypes.create_unicode_buffer(32_768)
            length = ctypes.windll.kernel32.GetShortPathNameW(
                str(long_parent),
                buffer,
                len(buffer),
            )
            if not length or length >= len(buffer):
                self.skipTest("GetShortPathNameW did not return an alias")
            short_parent = Path(buffer.value)
            if short_parent == long_parent:
                self.skipTest(
                    "8.3 short-name generation is disabled on this volume"
                )

            published = _publish(
                short_parent / "output",
                output_id="short-path-candidate",
            )
            directory, document = load_candidate(published, verify=True)

            self.assertEqual(directory, published.resolve())
            self.assertTrue(directory.is_relative_to(long_parent.resolve()))
            self.assertEqual(
                document["candidate_id"],
                candidate_module.portable_slug(
                    "short-path-candidate",
                    maximum_length=96,
                ),
            )

    def test_atomic_json_uses_exclusive_temp_without_truncating_collision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            occupied = root / ".document.json.observer.tmp"
            sentinel = b"pre-existing observer file"
            occupied.write_bytes(sentinel)
            real_mkstemp = tempfile.mkstemp

            with mock.patch(
                "tianlai.candidate.tempfile.mkstemp",
                side_effect=real_mkstemp,
            ) as exclusive_create:
                candidate_module._write_json_atomic(
                    target, {"state": "complete"}
                )

            self.assertEqual(occupied.read_bytes(), sentinel)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"state": "complete"},
            )
            exclusive_create.assert_called_once()

    def test_atomic_json_replace_failure_does_not_unlink_racing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "document.json"
            parked_payload = root / "parked-written-payload.tmp"
            sentinel = b"racing writer sentinel"
            observed_temporary: Path | None = None
            real_replace = os.replace

            def fail_after_replacing_temporary_name(source, destination):
                nonlocal observed_temporary
                source_path = Path(source)
                destination_path = Path(destination)
                if (
                    destination_path.name == target.name
                    and os.path.samefile(
                        destination_path.parent,
                        target.parent,
                    )
                ):
                    observed_temporary = source_path
                    os.rename(source_path, parked_payload)
                    source_path.write_bytes(sentinel)
                    raise PermissionError("simulated replace failure")
                return real_replace(source, destination)

            with mock.patch(
                "tianlai.candidate.os.replace",
                side_effect=fail_after_replacing_temporary_name,
            ):
                with self.assertRaisesRegex(
                    PermissionError, "simulated replace failure"
                ):
                    candidate_module._write_json_atomic(
                        target, {"state": "complete"}
                    )

            self.assertIsNotNone(observed_temporary)
            assert observed_temporary is not None
            self.assertEqual(observed_temporary.read_bytes(), sentinel)
            self.assertIn(b'"state": "complete"', parked_payload.read_bytes())
            self.assertFalse(target.exists())

    def test_prepare_creates_a_missing_multilevel_output_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary) / "one" / "two" / "renders"

            target = prepare_candidate_target(
                output_root,
                "nested root",
                output_id="candidate",
            )

            self.assertTrue(output_root.is_dir())
            self.assertTrue(target.directory.parent.is_dir())

    @unittest.skipIf(os.name == "nt", "POSIX symlink contract")
    def test_prepare_rejects_symlink_work_directory_before_lock_or_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authorised"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            title = "linked work"
            work = root / candidate_module.portable_slug(title)
            os.symlink(outside, work, target_is_directory=True)

            with self.assertRaises(OSError):
                prepare_candidate_target(root, title, output_id="blocked")

            self.assertEqual(list(outside.iterdir()), [])

    @unittest.skipUnless(os.name == "nt", "Windows junction contract")
    def test_prepare_rejects_junction_work_directory_before_lock_or_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authorised"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            title = "junction work"
            work = root / candidate_module.portable_slug(title)
            created = subprocess.run(
                ["cmd", "/d", "/c", "mklink", "/J", str(work), str(outside)],
                check=False,
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest(f"junction creation unavailable: {created.stderr}")
            try:
                with self.assertRaises(OSError):
                    prepare_candidate_target(root, title, output_id="blocked")
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                if os.path.lexists(work):
                    os.rmdir(work)

    def test_publication_rejects_work_directory_identity_swap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "authorised"
            outside = Path(temporary) / "outside"
            root.mkdir()
            outside.mkdir()
            target = prepare_candidate_target(
                root,
                "identity swap",
                output_id="blocked",
            )
            work = target.directory.parent
            parked = root / "parked-work"
            os.replace(work, parked)
            linked = False
            try:
                if os.name == "nt":
                    created = subprocess.run(
                        [
                            "cmd",
                            "/d",
                            "/c",
                            "mklink",
                            "/J",
                            str(work),
                            str(outside),
                        ],
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    if created.returncode != 0:
                        self.skipTest(
                            f"junction creation unavailable: {created.stderr}"
                        )
                else:
                    os.symlink(outside, work, target_is_directory=True)
                linked = True

                with self.assertRaises((OSError, ValueError)):
                    with candidate_publication(target):
                        self.fail("publication entered an identity-swapped work dir")
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                if linked and os.path.lexists(work):
                    if os.name == "nt":
                        os.rmdir(work)
                    else:
                        work.unlink()
                if parked.exists() and not work.exists():
                    os.replace(parked, work)

    def test_legacy_v1_candidate_without_authoring_binding_still_loads(self) -> None:
        timestamps = (
            "2026-08-09T20:34:56.123456+08:00",
            "2026-08-09t12:34:56z",
            "2026-08-09 12:34:56+08:00",
            "1990-12-31T23:59:60Z",
        )
        for index, timestamp in enumerate(timestamps):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as temporary:
                directory = _publish(
                    Path(temporary), output_id=f"legacy-v1-{index}"
                )
                manifest_path = directory / CANDIDATE_MANIFEST_NAME
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                manifest["version"] = 1
                manifest["created_at_utc"] = timestamp
                manifest.pop("authoring_project", None)
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

                _loaded, document = load_candidate(directory, verify=True)

                self.assertEqual(document["version"], 1)
                self.assertEqual(document["created_at_utc"], timestamp)
                self.assertNotIn("authoring_project", document)

    def test_legacy_v1_candidate_rejects_invalid_calendar_and_offset_fields(
        self,
    ) -> None:
        invalid = (
            "2026-02-31T12:34:56Z",
            "2026-08-09T24:00:00Z",
            "2026-08-09T12:60:00Z",
            "2026-08-09T12:34:61Z",
            "2026-08-09T12:34:56+24:00",
            "2026-08-09T12:34:56+08:60",
        )
        for index, timestamp in enumerate(invalid):
            with self.subTest(timestamp=timestamp), tempfile.TemporaryDirectory() as temporary:
                directory = _publish(
                    Path(temporary), output_id=f"invalid-v1-{index}"
                )
                manifest_path = directory / CANDIDATE_MANIFEST_NAME
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
                manifest["version"] = 1
                manifest["created_at_utc"] = timestamp
                _write_json(manifest_path, manifest)

                with self.assertRaisesRegex(ValueError, "created_at_utc"):
                    load_candidate(directory, verify=False)

    def test_publish_rejects_disconnected_authoring_roster_and_plan(self) -> None:
        authoring_roster = {
            "kind": "tianlai.authoring_roster",
            "schema_version": 1,
            "name": "test",
            "assignments": [
                {
                    "part": "lead",
                    "instrument": "测试工具/参考振荡器",
                }
            ],
        }
        cases = (
            (
                "测试工具/另一个乐器",
                "测试工具/另一个乐器",
                "formal roster",
            ),
            (
                "测试工具/参考振荡器",
                "测试工具/另一个乐器",
                "performance plan",
            ),
        )
        for index, (roster_instrument, plan_instrument, message) in enumerate(
            cases
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                target = prepare_candidate_target(
                    Path(temporary),
                    "candidate chain",
                    output_id=f"chain-{index}",
                )
                with self.assertRaisesRegex(ValueError, message):
                    _populate_candidate(
                        target,
                        authoring_roster=authoring_roster,
                        roster_instrument=roster_instrument,
                        plan_instrument=plan_instrument,
                    )

    def test_authoring_plan_checks_explicit_articulation_auto_only(self) -> None:
        base_roster = {
            "kind": "tianlai.authoring_roster",
            "schema_version": 1,
            "name": "test",
            "assignments": [
                {
                    "part": "lead",
                    "instrument": "测试工具/参考振荡器",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            explicit = copy.deepcopy(base_roster)
            explicit["assignments"][0]["articulation_auto"] = False
            target = prepare_candidate_target(
                Path(temporary),
                "candidate chain",
                output_id="explicit-articulation-auto",
            )
            with self.assertRaisesRegex(ValueError, "articulation_auto"):
                _populate_candidate(
                    target,
                    authoring_roster=explicit,
                    plan_articulation_auto=True,
                )

        # When omitted, the plan value comes from the resolved instrument
        # capability.  A portable candidate has no external capability
        # catalogue from which load_candidate could safely recompute it.
        with tempfile.TemporaryDirectory() as temporary:
            target = prepare_candidate_target(
                Path(temporary),
                "candidate chain",
                output_id="derived-articulation-auto",
            )
            directory = _populate_candidate(
                target,
                authoring_roster=base_roster,
                plan_articulation_auto=False,
            )
            load_candidate(directory, verify=True)

    def test_load_rejects_fully_rehashed_authoring_chain_contradictions(
        self,
    ) -> None:
        authoring_roster = {
            "kind": "tianlai.authoring_roster",
            "schema_version": 1,
            "name": "test",
            "assignments": [
                {
                    "part": "lead",
                    "instrument": "测试工具/参考振荡器",
                }
            ],
        }
        for change_formal, message in (
            (True, "formal roster"),
            (False, "performance plan"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as temporary:
                target = prepare_candidate_target(
                    Path(temporary),
                    "candidate chain",
                    output_id=f"forged-{int(change_formal)}",
                )
                directory = _populate_candidate(
                    target,
                    authoring_roster=authoring_roster,
                )
                roster_path = directory / "roster.json"
                plan_path = directory / "演奏计划.json"
                receipt_path = directory / "渲染回执.json"
                manifest_path = directory / CANDIDATE_MANIFEST_NAME
                roster = json.loads(roster_path.read_text(encoding="utf-8"))
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                if change_formal:
                    roster["assignments"][0]["instrument"] = "测试工具/另一个乐器"
                    _write_json(roster_path, roster)
                plan["parts"][0]["instrument"] = "测试工具/另一个乐器"
                _write_json(plan_path, plan)
                plan_sha256 = canonical_json_sha256(plan)
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                receipt["performance_plan"].update(
                    {
                        "file_sha256": sha256_file(plan_path),
                        "sha256": plan_sha256,
                    }
                )
                _write_json(receipt_path, receipt)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if change_formal:
                    manifest["project"]["roster"].update(
                        {
                            "canonical_sha256": canonical_json_sha256(roster),
                            "file_sha256": sha256_file(roster_path),
                        }
                    )
                manifest["project"]["performance_plan_sha256"] = plan_sha256
                manifest["render_receipt"]["sha256"] = sha256_file(receipt_path)
                _write_json(manifest_path, manifest)

                with self.assertRaisesRegex(ValueError, message):
                    load_candidate(directory, verify=True)

    def test_v2_candidate_timestamp_is_canonical_and_tamper_closed(self) -> None:
        invalid = (
            "2026-08-09T12:34:56+00:00",
            "2026-08-09T12:34:56.000Z\n",
            "2026-02-31T12:34:56.000Z",
            "2026-08-09t12:34:56.000z",
            "2026-08-09 12:34:56.000+08:00",
            "1990-12-31T23:59:60.000Z",
        )
        for index, timestamp in enumerate(invalid):
            with self.subTest(timestamp=repr(timestamp)):
                with tempfile.TemporaryDirectory() as temporary:
                    directory = _publish(
                        Path(temporary), output_id=f"timestamp-{index}"
                    )
                    manifest_path = directory / CANDIDATE_MANIFEST_NAME
                    manifest = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                    self.assertRegex(
                        manifest["created_at_utc"],
                        r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
                        r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$",
                    )
                    manifest["created_at_utc"] = timestamp
                    manifest_path.write_text(
                        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "canonical UTC"):
                        load_candidate(directory, verify=False)

    def test_load_bounds_candidate_manifest_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish(
                Path(temporary),
                output_id="bounded-manifest",
            )
            manifest_path = directory / CANDIDATE_MANIFEST_NAME
            payload = manifest_path.read_bytes()

            with mock.patch.object(
                candidate_module,
                "_MAX_CANDIDATE_JSON_BYTES",
                len(payload) - 1,
            ):
                with self.assertRaisesRegex(OSError, "configured byte limit"):
                    load_candidate(directory, verify=False)

    def test_load_rejects_non_strict_candidate_manifest_json(self) -> None:
        mutations = {
            "duplicate member": lambda path: _json_with_duplicate_first_member(
                path
            ),
            "non-finite number": lambda path: (
                path.read_bytes()[:1]
                + b'"ignored_non_finite":NaN,'
                + path.read_bytes()[1:]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                directory = _publish(
                    Path(temporary),
                    output_id=f"strict-manifest-{label.replace(' ', '-')}",
                )
                manifest_path = directory / CANDIDATE_MANIFEST_NAME
                manifest_path.write_bytes(mutate(manifest_path))

                with self.assertRaisesRegex(
                    ValueError,
                    "candidate manifest is invalid JSON",
                ):
                    load_candidate(directory, verify=False)

    def test_load_strictly_parses_hash_bound_project_and_receipt_json(
        self,
    ) -> None:
        cases = (
            ("score", "file_sha256", "candidate score is invalid JSON"),
            ("roster", "file_sha256", "candidate roster is invalid JSON"),
            (
                "render_profile",
                "file_sha256",
                "candidate render_profile is invalid JSON",
            ),
            (
                "render_receipt",
                "sha256",
                "candidate render receipt is invalid JSON",
            ),
        )
        for binding_name, hash_field, message in cases:
            with (
                self.subTest(binding=binding_name),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = _publish(
                    Path(temporary),
                    output_id=f"strict-{binding_name}",
                )
                manifest_path = directory / CANDIDATE_MANIFEST_NAME
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if binding_name == "render_receipt":
                    binding = manifest["render_receipt"]
                else:
                    binding = manifest["project"][binding_name]
                artifact_path = directory / binding["path"]
                duplicate_payload = _json_with_duplicate_first_member(
                    artifact_path
                )
                artifact_path.write_bytes(duplicate_payload)
                binding[hash_field] = hashlib.sha256(
                    duplicate_payload
                ).hexdigest()
                _write_json(manifest_path, manifest)

                with self.assertRaisesRegex(ValueError, message):
                    load_candidate(directory, verify=True)

    def test_candidate_json_snapshot_hashes_captured_payload_during_race(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "snapshot.json"
            captured_payload = b'{"state":"captured"}'
            replacement_payload = b'{"state":"replacement"}'
            path.write_bytes(captured_payload)
            real_read = candidate_module.read_plain_file_bytes

            def replace_path_after_read(*args, **kwargs):
                identity, payload = real_read(*args, **kwargs)
                path.write_bytes(replacement_payload)
                return identity, payload

            with mock.patch.object(
                candidate_module,
                "read_plain_file_bytes",
                side_effect=replace_path_after_read,
            ):
                document, digest = candidate_module._candidate_json_snapshot(
                    path,
                    invalid_json_message="invalid test JSON",
                    expected_file_sha256=hashlib.sha256(
                        captured_payload
                    ).hexdigest(),
                    hash_mismatch_message="captured payload hash mismatch",
                )

            self.assertEqual(document, {"state": "captured"})
            self.assertEqual(digest, hashlib.sha256(captured_payload).hexdigest())
            self.assertNotEqual(
                digest,
                hashlib.sha256(replacement_payload).hexdigest(),
            )

    def test_locate_strictly_parses_receipt_bound_performance_plan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish(
                Path(temporary),
                output_id="strict-locate-plan",
            )
            manifest_path = directory / CANDIDATE_MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            receipt_path = directory / manifest["render_receipt"]["path"]
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            plan_path = directory / receipt["performance_plan"]["path"]
            duplicate_payload = _json_with_duplicate_first_member(plan_path)
            plan_path.write_bytes(duplicate_payload)
            receipt["performance_plan"]["file_sha256"] = hashlib.sha256(
                duplicate_payload
            ).hexdigest()
            _write_json(receipt_path, receipt)
            manifest["render_receipt"]["sha256"] = sha256_file(receipt_path)
            _write_json(manifest_path, manifest)

            with self.assertRaisesRegex(
                ValueError,
                "performance plan is not valid UTF-8 JSON",
            ):
                locate_candidate(directory, at_seconds=0.75)

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

    def test_committed_backup_cleanup_revalidates_captured_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = _publish(root, output_id="cleanup-identity")
            receipt_sha256 = sha256_file(directory / "渲染回执.json")
            target = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="cleanup-identity",
                overwrite=True,
                expected_receipt_sha256=receipt_sha256,
            )
            parked_backup = directory.parent / "parked-old-candidate"
            real_commit = candidate_module._commit_candidate_staging
            replacement_marker = b"must not be deleted"

            def replace_backup_after_commit(
                staging: Path,
                final_target: CandidateTarget,
            ):
                committed_backup = real_commit(staging, final_target)
                self.assertIsNotNone(committed_backup)
                assert committed_backup is not None
                os.replace(committed_backup.path, parked_backup)
                committed_backup.path.mkdir()
                (committed_backup.path / "unrelated.txt").write_bytes(
                    replacement_marker
                )
                return committed_backup

            with (
                warnings.catch_warnings(record=True) as caught,
                mock.patch(
                    "tianlai.candidate._commit_candidate_staging",
                    side_effect=replace_backup_after_commit,
                ),
            ):
                warnings.simplefilter("always", RuntimeWarning)
                with candidate_publication(target) as staging:
                    _populate_candidate(staging, pitch="D4")

            replacement = target.directory.with_name(
                f".{target.directory.name}."
                f"{target.expected_manifest_sha256}.previous"
            )
            preserved = list(
                directory.parent.glob(
                    ".cleanup-preserved-*"
                )
            )
            self.assertEqual(len(preserved), 1)
            self.assertEqual(
                (preserved[0] / "unrelated.txt").read_bytes(),
                replacement_marker,
            )
            self.assertFalse(replacement.exists())
            self.assertTrue(parked_backup.is_dir())
            self.assertTrue(
                any("identity changed" in str(item.message) for item in caught)
            )
            load_candidate(directory, verify=True)
            follow_up = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="cleanup-identity",
                overwrite=True,
                expected_receipt_sha256=sha256_file(
                    directory / "渲染回执.json"
                ),
            )
            self.assertTrue(follow_up.replacing)

    def test_cleanup_race_renames_replacement_without_deleting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            cleanup = parent / ".race.token.staging"
            cleanup.mkdir()
            (cleanup / "original.txt").write_bytes(b"original generation")
            parent_identity = capture_plain_directory(parent)
            cleanup_identity = capture_plain_directory(cleanup)
            parked_original = parent / "parked-original"
            replacement_marker = b"replacement must survive"
            real_revalidate = candidate_module.revalidate_plain_directory
            raced = False

            def replace_after_identity_check(identity):
                nonlocal raced
                resolved = real_revalidate(identity)
                if identity is cleanup_identity and not raced:
                    raced = True
                    os.rename(cleanup, parked_original)
                    cleanup.mkdir()
                    nested = cleanup / "nested"
                    nested.mkdir()
                    (nested / "marker.bin").write_bytes(replacement_marker)
                return resolved

            with (
                warnings.catch_warnings(record=True) as caught,
                mock.patch(
                    "tianlai.candidate.revalidate_plain_directory",
                    side_effect=replace_after_identity_check,
                ),
            ):
                warnings.simplefilter("always", RuntimeWarning)
                candidate_module._safe_cleanup_private_directory(
                    cleanup,
                    parent=parent,
                    prefix=".race.",
                    label="race cleanup",
                    parent_identity=parent_identity,
                    directory_identity=cleanup_identity,
                )

            preserved = list(
                parent.glob(
                    ".cleanup-preserved-*"
                )
            )
            self.assertEqual(len(preserved), 1)
            self.assertEqual(
                (preserved[0] / "nested" / "marker.bin").read_bytes(),
                replacement_marker,
            )
            self.assertEqual(
                (parked_original / "original.txt").read_bytes(),
                b"original generation",
            )
            self.assertFalse(cleanup.exists())
            self.assertTrue(
                any("identity changed" in str(item.message) for item in caught)
            )

            # The active staging name is free for a later operation; preserved
            # entries deliberately do not match the transaction suffix.
            cleanup.mkdir()
            self.assertEqual(
                list(parent.glob(".race.*.staging")),
                [cleanup],
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

    def test_cleanup_preservation_does_not_block_next_overwrite(self) -> None:
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
            with warnings.catch_warnings():
                warnings.simplefilter("error", RuntimeWarning)
                with candidate_publication(target) as staging:
                    _populate_candidate(staging, pitch="D4")

            score = json.loads(
                (directory / "score.json").read_text(encoding="utf-8")
            )
            self.assertEqual(score["parts"][0]["notes"][0]["pitch"], "D4")
            self.assertEqual(
                0,
                len(
                    list(
                        directory.parent.glob(
                            f".{directory.name}.*.previous"
                        )
                    )
                ),
            )
            self.assertEqual(
                1,
                len(
                    list(
                        directory.parent.glob(
                            ".cleanup-preserved-*"
                        )
                    )
                ),
            )
            follow_up = prepare_candidate_target(
                root,
                "候选合同测试",
                output_id="cleanup-warning",
                overwrite=True,
                expected_receipt_sha256=sha256_file(
                    directory / "渲染回执.json"
                ),
            )
            self.assertTrue(follow_up.replacing)

    def test_cleanup_uses_short_quarantine_name_for_long_transaction_entry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            # The old cleanup spelling appended another UUID-bearing suffix
            # to this name and exceeded Windows' 255-character component
            # limit.  A quarantine name must not inherit the source length.
            cleanup = parent / ("." + "x" * 150 + ".previous")
            cleanup.mkdir()
            (cleanup / "marker.bin").write_bytes(b"recoverable")
            parent_identity = capture_plain_directory(parent)
            cleanup_identity = capture_plain_directory(cleanup)

            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", RuntimeWarning)
                candidate_module._safe_cleanup_private_directory(
                    cleanup,
                    parent=parent,
                    prefix=".",
                    label="long cleanup",
                    parent_identity=parent_identity,
                    directory_identity=cleanup_identity,
                )

            preserved = list(parent.glob(".cleanup-preserved-*"))
            self.assertEqual(len(preserved), 1)
            self.assertLess(len(preserved[0].name), 64)
            self.assertEqual(
                (preserved[0] / "marker.bin").read_bytes(),
                b"recoverable",
            )
            self.assertFalse(cleanup.exists())
            self.assertEqual(caught, [])

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

    def test_compare_rejects_score_replacement_after_candidate_load(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before = _publish(root, output_id="compare-race-before")
            after = _publish(root, output_id="compare-race-after", pitch="D4")
            real_load = candidate_module.load_candidate
            load_count = 0

            def load_then_replace_score(*args, **kwargs):
                nonlocal load_count
                loaded = real_load(*args, **kwargs)
                load_count += 1
                if load_count == 2:
                    score_path = before / "score.json"
                    score_path.write_bytes(score_path.read_bytes() + b" ")
                return loaded

            with mock.patch.object(
                candidate_module,
                "load_candidate",
                side_effect=load_then_replace_score,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "candidate score file hash mismatch",
                ):
                    compare_candidates(before, after)

    def test_compare_strictly_reloads_manifest_bound_json(self) -> None:
        cases = (
            (
                "score",
                "candidate score is invalid JSON",
            ),
            (
                "render receipt",
                "candidate render receipt is invalid JSON",
            ),
        )
        for artifact, message in cases:
            with (
                self.subTest(artifact=artifact),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                before = _publish(
                    root,
                    output_id=f"compare-strict-before-{artifact.replace(' ', '-')}",
                )
                after = _publish(
                    root,
                    output_id=f"compare-strict-after-{artifact.replace(' ', '-')}",
                    pitch="D4",
                )
                before_directory, before_manifest = load_candidate(before)
                after_directory, after_manifest = load_candidate(after)
                if artifact == "score":
                    binding = before_manifest["project"]["score"]
                    hash_key = "file_sha256"
                else:
                    binding = before_manifest["render_receipt"]
                    hash_key = "sha256"
                artifact_path = before_directory / binding["path"]
                duplicate_payload = _json_with_duplicate_first_member(
                    artifact_path
                )
                artifact_path.write_bytes(duplicate_payload)
                binding[hash_key] = hashlib.sha256(
                    duplicate_payload
                ).hexdigest()

                with mock.patch.object(
                    candidate_module,
                    "load_candidate",
                    side_effect=(
                        (before_directory, before_manifest),
                        (after_directory, after_manifest),
                    ),
                ):
                    with self.assertRaisesRegex(ValueError, message):
                        compare_candidates(before, after)

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
            self.assertTrue(result["project_review"]["continuation_allowed"])
            self.assertEqual(result["project_review"]["blocking_count"], 0)
            self.assertEqual(
                result["project_review"]["binding"][
                    "performance_plan_sha256"
                ],
                result["performance_plan_sha256"],
            )
            directory = Path(result["candidate_directory"])
            self.assertTrue((directory / "合奏.wav").is_file())
            for key in (
                "candidate_manifest",
                "mix_wav",
                "post_render_check",
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
            self.assertIs(
                result["post_render_check_summary"]["can_proceed"],
                True,
            )
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
