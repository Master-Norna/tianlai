from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
import tempfile
import unittest
from unittest import mock

from jsonschema import Draft202012Validator, FormatChecker

import tianlai.candidate as candidate_module
from tianlai.candidate import (
    CANDIDATE_MANIFEST_NAME,
    MAX_PLAYBACK_MAP_SCHEDULED_NOTES,
    PLAYBACK_MAP_KIND,
    PLAYBACK_MAP_SCHEMA_URI,
    PLAYBACK_MAP_VERSION,
    build_candidate_playback_map,
    canonical_json_sha256,
    prepare_candidate_target,
    publish_candidate_metadata,
    sha256_file,
)
from tianlai.audio import write_wav_pcm24
from tianlai.post_render_check import (
    POST_RENDER_CHECK_NAME,
    analyze_rendered_wav,
    write_post_render_check,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "candidate-playback-map.schema.json"


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


def _default_parts() -> list[dict]:
    return [
        {
            "part_id": "lead",
            "executor_id": "lead",
            "instrument": "测试工具/参考振荡器",
            "notes": [
                {
                    "event_id": "event-000001",
                    "note_id": 1,
                    "start": 0.12345649,
                    "end": 1.23456751,
                    "beat": 1.0,
                    "pitch": "C4",
                    "midi_note": 60.0,
                    "velocity": 0.7,
                    "articulation": "sustain",
                }
            ],
        }
    ]


def _documents(
    *,
    schema_version: int | None = 1,
    parts: list[dict] | None = None,
) -> tuple[dict, dict]:
    raw_parts = _default_parts() if parts is None else parts
    score: dict = {
        "title": "播放映射合同测试",
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
        "parts": [],
    }
    if schema_version is not None:
        score["schema_version"] = schema_version

    plan_parts: list[dict] = []
    score_parts_by_id: dict[str, dict] = {}
    latest_end = 0.0
    for raw_part in raw_parts:
        score_notes: list[dict] = []
        performance_events: list[dict] = []
        trace: list[dict] = []
        for raw_note in raw_part["notes"]:
            source_event_id = raw_note["event_id"]
            score_note = {
                "bar": raw_note.get("bar", 1),
                "beat": raw_note["beat"],
                "duration_beats": raw_note.get("duration_beats", 1.0),
                "pitch": raw_note["pitch"],
                "velocity": raw_note["velocity"],
            }
            if schema_version == 1:
                score_note["event_id"] = source_event_id
            score_notes.append(score_note)

            note_on = {
                "time": raw_note["start"],
                "type": "note_on",
                "note_id": raw_note["note_id"],
                "midi_note": raw_note["midi_note"],
                "velocity": raw_note["velocity"],
            }
            note_off = {
                "time": raw_note["end"],
                "type": "note_off",
                "note_id": raw_note["note_id"],
                "release_velocity": raw_note.get("release_velocity", 0.4),
            }
            trace_entry = {
                "时间": round(raw_note["start"], 6),
                "时长": round(raw_note["end"] - raw_note["start"], 6),
                "小节": raw_note.get("bar", 1),
                "拍": raw_note["beat"],
                "音": raw_note["pitch"],
                "力度": round(raw_note["velocity"], 4),
                "奏法": raw_note["articulation"],
                "推导": {"测试": "此字段不会进入公共播放映射"},
            }
            if schema_version == 1:
                note_on["source_event_id"] = source_event_id
                note_off["source_event_id"] = source_event_id
                trace_entry["source_event_id"] = source_event_id
            if raw_note["articulation"] is not None:
                performance_events.append(
                    {
                        "time": raw_note["start"],
                        "type": "articulation",
                        "name": raw_note["articulation"],
                    }
                )
            performance_events.extend((note_on, note_off))
            trace.append(trace_entry)
            latest_end = max(latest_end, float(raw_note["end"]))
        performance_events.sort(
            key=lambda event: (
                event["time"],
                0 if event["type"] in {"articulation", "note_on"} else 1,
            )
        )
        score_part = score_parts_by_id.setdefault(
            raw_part["part_id"],
            {"id": raw_part["part_id"], "notes": []},
        )
        score_part["notes"].extend(score_notes)
        plan_parts.append(
            {
                "executor_id": raw_part["executor_id"],
                "part_id": raw_part["part_id"],
                "instrument": raw_part["instrument"],
                "performance": {
                    "sample_rate": 8_000,
                    "channels": 2,
                    "duration_seconds": 3.0,
                    "events": performance_events,
                },
                "trace": trace,
            }
        )
    score["parts"] = list(score_parts_by_id.values())
    if latest_end > 3.0:
        raise ValueError("test fixture note extends beyond its fixed duration")
    plan = {
        "title": score["title"],
        "sample_rate": 8_000,
        "duration_seconds": 3.0,
        "parts": plan_parts,
    }
    return score, plan


def _publish_candidate(
    root: Path,
    *,
    schema_version: int | None = 1,
    parts: list[dict] | None = None,
    mutate_documents=None,
    receipt_version: int = 2,
    space_tail_seconds: float | None = None,
) -> Path:
    score, plan = _documents(schema_version=schema_version, parts=parts)
    if mutate_documents is not None:
        mutate_documents(score, plan)
    plan_sha256 = canonical_json_sha256(plan)
    target = prepare_candidate_target(
        root,
        score["title"],
        output_id="候选 playback map",
        plan_sha256=plan_sha256,
    )
    directory = target.directory
    directory.mkdir(parents=True)

    plan_path = directory / "演奏计划.json"
    _write_json(plan_path, plan)
    mix_path = directory / "合奏.wav"
    dry_frame_count = 24_000
    space_tail_frames = (
        0
        if space_tail_seconds is None
        else max(0, math.ceil(space_tail_seconds * 8_000))
    )
    mix_frame_count = dry_frame_count + space_tail_frames
    write_wav_pcm24(
        mix_path,
        (
            (
                0.08 * math.sin(2.0 * math.pi * 220.0 * frame / 8_000),
                0.08 * math.sin(2.0 * math.pi * 220.0 * frame / 8_000),
            )
            for frame in range(mix_frame_count)
        ),
        8_000,
    )
    license_path = directory / "合奏-许可.json"
    _write_json(license_path, {"format": "test-license-sidecar"})
    attribution_path = directory / "署名说明.txt"
    attribution_path.write_text("test attribution\n", encoding="utf-8")
    receipt = {
        "format": "tianlai.render_receipt",
        "version": receipt_version,
        "audio_format": {
            "container": "WAV",
            "encoding": "PCM",
            "bits_per_sample": 24,
            "channels": 2,
            "sample_rate": 8_000,
        },
        "performance_plan": {
            "path": plan_path.name,
            "file_sha256": sha256_file(plan_path),
            "sha256": plan_sha256,
        },
        "mix": {
            "path": mix_path.name,
            "frame_count": mix_frame_count,
            "sha256": sha256_file(mix_path),
        },
        "stems": [],
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
        "space": (
            {"enabled": False}
            if space_tail_seconds is None
            else {
                "enabled": True,
                "effective_tail_seconds": space_tail_seconds,
            }
        ),
    }
    if receipt_version == 3:
        report = analyze_rendered_wav(
            mix_path,
            artifact_path=mix_path.name,
            expected_sample_rate=8_000,
            expected_frame_count=mix_frame_count,
            expected_activity=True,
            plan_sha256=plan_sha256,
        )
        report_path = directory / POST_RENDER_CHECK_NAME
        write_post_render_check(report_path, report)
        receipt["post_render_check"] = {
            "path": report_path.name,
            "sha256": sha256_file(report_path),
            "format": report["format"],
            "version": report["version"],
        }
    receipt_path = directory / "渲染回执.json"
    _write_json(receipt_path, receipt)
    publish_candidate_metadata(
        target,
        title=score["title"],
        score=score,
        roster={
            "name": "playback-map-test",
            "assignments": [
                {
                    "part": part["part_id"],
                    "instrument": part["instrument"],
                }
                for part in (_default_parts() if parts is None else parts)
            ],
        },
        render_profile={"kind": "test-profile"},
        receipt_path=receipt_path,
        plan_sha256=plan_sha256,
    )
    return directory


def _tree_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


class CandidatePlaybackMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(
            cls.schema,
            format_checker=FormatChecker(),
        )

    def test_v2_candidate_builds_schema_valid_exact_bound_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(
                Path(temporary) / "Unicode 空格 路径 🎵"
            )
            before = _tree_snapshot(directory)

            result = build_candidate_playback_map(directory)

            self.validator.validate(result)
            self.assertEqual(_tree_snapshot(directory), before)
            self.assertEqual(result["kind"], PLAYBACK_MAP_KIND)
            self.assertEqual(result["schema_version"], PLAYBACK_MAP_VERSION)
            self.assertEqual(result["$schema"], PLAYBACK_MAP_SCHEMA_URI)
            self.assertEqual(
                result["bindings"]["candidate_manifest"]["sha256"],
                sha256_file(directory / CANDIDATE_MANIFEST_NAME),
            )
            self.assertEqual(
                result["bindings"]["mix"]["size_bytes"],
                (directory / "合奏.wav").stat().st_size,
            )
            for binding in result["bindings"].values():
                path = binding["path"]
                self.assertFalse(Path(path).is_absolute())
                self.assertNotIn("..", PurePosixPath(path).parts)

            event = result["events"][0]
            self.assertEqual(result["summary"]["score_schema_version"], 1)
            self.assertEqual(result["summary"]["stable_identity_count"], 1)
            self.assertEqual(
                result["summary"]["legacy_unstable_identity_count"],
                0,
            )
            self.assertEqual(event["source_event_id"], "event-000001")
            self.assertTrue(event["stable_identity"])
            self.assertEqual(event["note_on"]["seconds"], 0.12345649)
            self.assertEqual(event["note_on"]["frame"], 988)
            self.assertEqual(event["note_off"]["seconds"], 1.23456751)
            self.assertEqual(
                event["note_off"]["frame"],
                round(1.23456751 * 8_000),
            )
            self.assertEqual(event["trace"]["bar"], 1)
            self.assertEqual(event["trace"]["sounding_pitch"], "C4")
            self.assertNotIn("推导", event["trace"])
            self.assertTrue(result["timeline"]["note_off_frame_exclusive"])
            self.assertFalse(
                result["timeline"]["audible_release_or_space_tail_exact"]
            )

    def test_v2_and_v3_dry_and_space_generations_build(self) -> None:
        for receipt_version in (2, 3):
            for space_tail_seconds in (None, 0.25):
                with (
                    self.subTest(
                        receipt_version=receipt_version,
                        space_tail_seconds=space_tail_seconds,
                    ),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    directory = _publish_candidate(
                        Path(temporary),
                        receipt_version=receipt_version,
                        space_tail_seconds=space_tail_seconds,
                    )

                    result = build_candidate_playback_map(directory)

                    self.validator.validate(result)
                    expected_frames = 24_000 + (
                        0
                        if space_tail_seconds is None
                        else math.ceil(space_tail_seconds * 8_000)
                    )
                    self.assertEqual(
                        result["timeline"]["frame_count"],
                        expected_frames,
                    )

    def test_trace_quantization_does_not_reject_a_valid_serialized_plan(self) -> None:
        def place_on_rounding_boundary(_score: dict, plan: dict) -> None:
            events = plan["parts"][0]["performance"]["events"]
            for event in events:
                if event["type"] in {"articulation", "note_on"}:
                    event["time"] = 0.701046675
                elif event["type"] == "note_off":
                    event["time"] = 0.963674175
            trace = plan["parts"][0]["trace"][0]
            trace["时间"] = 0.701047
            # The conductor rounds this duration before it independently
            # rounds both event timestamps.  Reconstructing the subtraction
            # can therefore land on the other side of a microsecond tie.
            trace["时长"] = 0.262627

        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(
                Path(temporary),
                mutate_documents=place_on_rounding_boundary,
            )

            result = build_candidate_playback_map(directory)

            self.validator.validate(result)
            self.assertEqual(result["events"][0]["trace"]["bar"], 1)

    def test_result_is_deterministic_and_accepts_manifest_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary))

            first = build_candidate_playback_map(directory)
            second = build_candidate_playback_map(
                directory / CANDIDATE_MANIFEST_NAME
            )

            self.assertEqual(first, second)
            self.assertEqual(
                json.dumps(first, ensure_ascii=False, sort_keys=True),
                json.dumps(second, ensure_ascii=False, sort_keys=True),
            )

    def test_chords_overlap_and_multiple_executors_sort_stably(self) -> None:
        parts = [
            {
                "part_id": "lead",
                "executor_id": "z-lead",
                "instrument": "测试工具/参考振荡器",
                "notes": [
                    {
                        "event_id": "lead-c",
                        "note_id": 1,
                        "start": 0.25,
                        "end": 1.0,
                        "beat": 1.0,
                        "pitch": "C4",
                        "midi_note": 60.0,
                        "velocity": 0.7,
                        "articulation": "sustain",
                    },
                    {
                        "event_id": "lead-e",
                        "note_id": 2,
                        "start": 0.25,
                        "end": 0.75,
                        "beat": 1.0,
                        "pitch": "E4",
                        "midi_note": 64.0,
                        "velocity": 0.6,
                        "articulation": "tenuto",
                    },
                ],
            },
            {
                "part_id": "bass",
                "executor_id": "a-bass",
                "instrument": "测试工具/参考振荡器",
                "notes": [
                    {
                        "event_id": "bass-c",
                        "note_id": 1,
                        "start": 0.1,
                        "end": 1.5,
                        "beat": 1.0,
                        "pitch": "C3",
                        "midi_note": 48.0,
                        "velocity": 0.8,
                        "articulation": None,
                    }
                ],
            },
        ]

        def reverse_trace(_score: dict, plan: dict) -> None:
            plan["parts"][0]["trace"].reverse()

        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(
                Path(temporary),
                parts=parts,
                mutate_documents=reverse_trace,
            )
            result = build_candidate_playback_map(directory)

            self.validator.validate(result)
            self.assertEqual(
                [event["source_event_id"] for event in result["events"]],
                ["bass-c", "lead-c", "lead-e"],
            )
            self.assertEqual(result["events"][1]["trace"]["sounding_pitch"], "C4")
            self.assertEqual(result["events"][2]["trace"]["sounding_pitch"], "E4")
            self.assertEqual(result["summary"]["scheduled_note_count"], 3)
            self.assertEqual(result["summary"]["executor_count"], 2)

    def test_one_kit_part_can_route_to_multiple_executors(self) -> None:
        parts = [
            {
                "part_id": "drum-kit",
                "executor_id": "kick",
                "instrument": "测试工具/底鼓",
                "notes": [
                    {
                        "event_id": "kick-1",
                        "note_id": 1,
                        "start": 0.1,
                        "end": 0.3,
                        "beat": 1.0,
                        "pitch": "C2",
                        "midi_note": 36.0,
                        "velocity": 0.8,
                        "articulation": None,
                    }
                ],
            },
            {
                "part_id": "drum-kit",
                "executor_id": "snare",
                "instrument": "测试工具/军鼓",
                "notes": [
                    {
                        "event_id": "snare-1",
                        "note_id": 1,
                        "start": 0.1,
                        "end": 0.35,
                        "beat": 1.0,
                        "pitch": "D2",
                        "midi_note": 38.0,
                        "velocity": 0.7,
                        "articulation": None,
                    }
                ],
            },
        ]
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary), parts=parts)

            result = build_candidate_playback_map(directory)

            self.validator.validate(result)
            self.assertEqual(
                [event["source_event_id"] for event in result["events"]],
                ["kick-1", "snare-1"],
            )
            self.assertEqual(
                {event["part_id"] for event in result["events"]},
                {"drum-kit"},
            )
            self.assertEqual(result["summary"]["executor_count"], 2)

    def test_legacy_candidate_remains_usable_with_explicitly_unstable_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(
                Path(temporary),
                schema_version=None,
            )

            result = build_candidate_playback_map(directory)

            self.validator.validate(result)
            self.assertIsNone(result["summary"]["score_schema_version"])
            self.assertEqual(result["summary"]["stable_identity_count"], 0)
            self.assertEqual(
                result["summary"]["legacy_unstable_identity_count"],
                1,
            )
            self.assertIsNone(result["events"][0]["source_event_id"])
            self.assertFalse(result["events"][0]["stable_identity"])
            self.assertIsNone(result["events"][0]["trace"]["bar"])
            self.assertIsNone(result["events"][0]["trace"]["beat"])

    def test_any_bound_candidate_artifact_tamper_fails_closed(self) -> None:
        def tamper_manifest(directory: Path) -> None:
            path = directory / CANDIDATE_MANIFEST_NAME
            document = json.loads(path.read_text(encoding="utf-8"))
            document["candidate_id"] = "forged-candidate"
            _write_json(path, document)

        def tamper_score(directory: Path) -> None:
            path = directory / "score.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["title"] = "forged score"
            _write_json(path, document)

        def tamper_plan(directory: Path) -> None:
            path = directory / "演奏计划.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["title"] = "forged plan"
            _write_json(path, document)

        def tamper_mix(directory: Path) -> None:
            path = directory / "合奏.wav"
            path.write_bytes(path.read_bytes() + b"forged")

        def tamper_receipt(directory: Path) -> None:
            path = directory / "渲染回执.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            document["collaboration"]["effective_mode"] = "forged"
            _write_json(path, document)

        cases = {
            "candidate manifest": tamper_manifest,
            "score": tamper_score,
            "performance plan": tamper_plan,
            "mix WAV": tamper_mix,
            "render receipt": tamper_receipt,
        }
        for label, tamper in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                directory = _publish_candidate(Path(temporary))
                tamper(directory)
                with self.assertRaises((RuntimeError, ValueError)):
                    build_candidate_playback_map(directory)

    def test_candidate_metadata_must_match_the_public_map_schema(self) -> None:
        for field, value in (
            ("title", {"private_path": "C:/Users/alice/secret"}),
            ("created_at_utc", "not-a-date"),
            ("parent_candidate_id", 7),
        ):
            with (
                self.subTest(field=field),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = _publish_candidate(Path(temporary))
                path = directory / CANDIDATE_MANIFEST_NAME
                manifest = json.loads(path.read_text(encoding="utf-8"))
                manifest[field] = value
                _write_json(path, manifest)

                with self.assertRaisesRegex(ValueError, field):
                    build_candidate_playback_map(directory)

    def test_legacy_candidate_datetime_spellings_survive_playback_map(self) -> None:
        timestamps = (
            "2026-08-09t12:34:56z",
            "2026-08-09 12:34:56+08:00",
            "1990-12-31T23:59:60Z",
        )
        for index, timestamp in enumerate(timestamps):
            with (
                self.subTest(timestamp=timestamp),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = _publish_candidate(Path(temporary))
                path = directory / CANDIDATE_MANIFEST_NAME
                manifest = json.loads(path.read_text(encoding="utf-8"))
                manifest["version"] = 1
                manifest["created_at_utc"] = timestamp
                _write_json(path, manifest)

                playback_map = build_candidate_playback_map(directory)

                self.validator.validate(playback_map)
                self.assertEqual(
                    playback_map["candidate"]["created_at_utc"],
                    timestamp,
                )

    def test_concurrent_generation_changes_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary))
            original_load = candidate_module.load_candidate

            def change_manifest_after_verification(*args, **kwargs):
                loaded_directory, manifest = original_load(*args, **kwargs)
                path = loaded_directory / CANDIDATE_MANIFEST_NAME
                changed = json.loads(path.read_text(encoding="utf-8"))
                changed["title"] = "changed after verification"
                _write_json(path, changed)
                return loaded_directory, manifest

            with (
                mock.patch.object(
                    candidate_module,
                    "load_candidate",
                    side_effect=change_manifest_after_verification,
                ),
                self.assertRaisesRegex(ValueError, "changed during verification"),
            ):
                build_candidate_playback_map(directory)

        with tempfile.TemporaryDirectory() as temporary:
            directory = _publish_candidate(Path(temporary))
            original_verified_plan = candidate_module._verified_plan

            def change_mix_after_verification(*args, **kwargs):
                plan, receipt = original_verified_plan(*args, **kwargs)
                mix = directory / receipt["mix"]["path"]
                mix.write_bytes(mix.read_bytes() + b"changed")
                return plan, receipt

            with (
                mock.patch.object(
                    candidate_module,
                    "_verified_plan",
                    side_effect=change_mix_after_verification,
                ),
                self.assertRaisesRegex(ValueError, "rendered mix hash mismatch"),
            ):
                build_candidate_playback_map(directory)

    def test_semantically_inconsistent_freshly_bound_plan_fails_closed(self) -> None:
        def missing_source(_score: dict, plan: dict) -> None:
            events = plan["parts"][0]["performance"]["events"]
            for event in events:
                if event["type"] in {"note_on", "note_off"}:
                    event.pop("source_event_id")

        def wrong_trace_time(_score: dict, plan: dict) -> None:
            plan["parts"][0]["trace"][0]["时间"] = 0.5

        def wrong_score_position(_score: dict, plan: dict) -> None:
            plan["parts"][0]["trace"][0]["小节"] = 2

        def wrong_sounding_pitch(_score: dict, plan: dict) -> None:
            plan["parts"][0]["trace"][0]["音"] = "C#9"

        def wrong_velocity(_score: dict, plan: dict) -> None:
            plan["parts"][0]["trace"][0]["力度"] = 0.1

        def wrong_articulation(_score: dict, plan: dict) -> None:
            plan["parts"][0]["trace"][0]["奏法"] = "forged"

        def orphan_note_on(_score: dict, plan: dict) -> None:
            events = plan["parts"][0]["performance"]["events"]
            plan["parts"][0]["performance"]["events"] = [
                event for event in events if event["type"] != "note_off"
            ]

        def unknown_source(_score: dict, plan: dict) -> None:
            events = plan["parts"][0]["performance"]["events"]
            for event in events:
                if event["type"] in {"note_on", "note_off"}:
                    event["source_event_id"] = "not-in-score"
            plan["parts"][0]["trace"][0]["source_event_id"] = "not-in-score"

        for label, mutate in {
            "missing v1 source identity": missing_source,
            "rounded trace differs from exact schedule": wrong_trace_time,
            "trace position differs from score": wrong_score_position,
            "trace pitch differs from performance": wrong_sounding_pitch,
            "trace velocity differs from performance": wrong_velocity,
            "trace articulation differs from state": wrong_articulation,
            "unpaired note_on": orphan_note_on,
            "unknown score source": unknown_source,
        }.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                directory = _publish_candidate(
                    Path(temporary),
                    mutate_documents=mutate,
                )
                with self.assertRaises(ValueError):
                    build_candidate_playback_map(directory)

    def test_public_note_limit_accepts_boundary_and_rejects_next_note(self) -> None:
        self.assertEqual(MAX_PLAYBACK_MAP_SCHEDULED_NOTES, 250_000)
        self.assertEqual(
            self.schema["properties"]["limits"]["properties"][
                "max_scheduled_note_count"
            ]["const"],
            250_000,
        )
        self.assertEqual(
            self.schema["properties"]["events"]["maxItems"],
            250_000,
        )

        def notes(count: int) -> list[dict]:
            return [
                {
                    "event_id": f"event-{index}",
                    "note_id": index,
                    "start": index / 10.0,
                    "end": index / 10.0 + 0.05,
                    "beat": float(index),
                    "pitch": "C4",
                    "midi_note": 60.0,
                    "velocity": 0.7,
                    "articulation": None,
                }
                for index in range(1, count + 1)
            ]

        with mock.patch.object(
            candidate_module,
            "MAX_PLAYBACK_MAP_SCHEDULED_NOTES",
            2,
        ):
            with tempfile.TemporaryDirectory() as temporary:
                boundary = _publish_candidate(
                    Path(temporary),
                    parts=[
                        {
                            "part_id": "lead",
                            "executor_id": "lead",
                            "instrument": "测试工具/参考振荡器",
                            "notes": notes(2),
                        }
                    ],
                )
                result = build_candidate_playback_map(boundary)
                self.assertEqual(len(result["events"]), 2)
                self.assertEqual(result["limits"]["max_scheduled_note_count"], 2)
            with tempfile.TemporaryDirectory() as temporary:
                over_limit = _publish_candidate(
                    Path(temporary),
                    parts=[
                        {
                            "part_id": "lead",
                            "executor_id": "lead",
                            "instrument": "测试工具/参考振荡器",
                            "notes": notes(3),
                        }
                    ],
                )
                with self.assertRaisesRegex(ValueError, "exceeds hard limit 2"):
                    build_candidate_playback_map(over_limit)


if __name__ == "__main__":
    unittest.main()
