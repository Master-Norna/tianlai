"""Lightweight regression tests for the transactional all-audition generator."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "生成全部试音.py"


def _load_tool():
    name = "tianlai_test_generate_all_auditions"
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class GenerateAllAuditionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.instrument_root = self.root / "乐器"
        self.examples = self.root / "examples"
        self.output_root = self.root / "output" / "全音域试音"
        self.legacy_output_root = self.root / "output" / "试音"
        self.instrument_root.mkdir(parents=True)
        self.examples.mkdir(parents=True)
        self.output_root.mkdir(parents=True)
        self.legacy_output_root.mkdir(parents=True)
        (self.legacy_output_root / "不可变旧试听.txt").write_text(
            "legacy",
            encoding="utf-8",
        )

        self.tool.ROOT = self.root
        self.tool.INSTRUMENT_ROOT = self.instrument_root
        self.tool.EXAMPLES = self.examples
        self.tool.OUTPUT_ROOT = self.output_root
        self.tool.FULL_RANGE_EVENTS_ROOT = (
            self.examples / "全音域上行"
        )
        # Focused tests use tiny catalogues; production keeps the module
        # default of exactly 103 and has a dedicated rejection test below.
        self.tool.EXPECTED_INSTRUMENT_COUNT = None

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _assert_only_recovery_directory(self, recovery: Path) -> None:
        candidates = list(
            (self.root / "output").glob(".生成全音域试音-*")
        )
        self.assertEqual(len(candidates), 1, candidates)
        self.assertTrue(
            recovery.samefile(candidates[0]),
            f"{recovery} is not the same filesystem entry as {candidates[0]}",
        )

    def _make_instrument(
        self,
        relative: str,
        coverage: list[str],
        *,
        extra_review: tuple[str, object] | None = None,
        add_pitch_probe: bool = False,
        manifest_updates: dict[str, object] | None = None,
        unpitched: bool = False,
    ) -> tuple[Path, Path]:
        directory = self.instrument_root / Path(relative)
        directory.mkdir(parents=True)
        manifest = directory / "乐器.json"
        manifest_document: dict[str, object] = {
            "name": f"{directory.name} candidate",
            "type": "modeled_instrument",
            "quality_tier": "candidate",
        }
        if manifest_updates:
            manifest_document.update(manifest_updates)
        manifest.write_text(
            json.dumps(manifest_document, ensure_ascii=False),
            encoding="utf-8",
        )
        if unpitched:
            (directory / "音准校准.json").write_text(
                json.dumps(
                    {
                        "applicable": False,
                        "pitch_mode": "ignore",
                        "reason": "unit-test unpitched trigger map",
                        "samples": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        events = self.examples / f"{directory.name}_奏法.events.json"
        events.write_text(
            json.dumps({"instrument": directory.name, "events": []}, ensure_ascii=False),
            encoding="utf-8",
        )
        if add_pitch_probe:
            (self.examples / f"{directory.name}_A4_音准.events.json").write_text(
                json.dumps({"instrument": directory.name, "probe": True}),
                encoding="utf-8",
            )

        old_report = {
            "status": "machine_pass_human_pending",
            "peak": 0.1,
            "clipped_samples": 0,
            "wav_sha256": "0" * 64,
            "manifest_sha256": "1" * 64,
            "events_sha256": "2" * 64,
            "coverage": coverage,
            "human_review": "pending",
        }
        if extra_review is not None:
            old_report[extra_review[0]] = extra_review[1]
        (directory / "试听核验.json").write_text(
            json.dumps(old_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest, events

    @staticmethod
    def _fake_report(
        manifest_path: str | Path,
        events_path: str | Path,
        wav_path: str | Path,
        *,
        output_path: str | Path,
        coverage: list[str],
    ) -> dict[str, object]:
        manifest_path = Path(manifest_path)
        events_path = Path(events_path)
        wav_path = Path(wav_path)
        output_path = Path(output_path)
        wav_path.parent.mkdir(parents=True, exist_ok=True)
        wav_path.write_bytes(b"one synthetic test wav")
        report: dict[str, object] = {
            "status": "machine_pass_human_pending",
            "rendered_at": "2099-01-01",
            "platform": "test",
            "sample_rate": 48_000,
            "channels": 2,
            "frame_count": 48,
            "duration_seconds": 0.001,
            "peak_active_voices": 1,
            "peak": 0.25,
            "rms": 0.1,
            "clipped_samples": 0,
            "wav": str(wav_path),
            "wav_persistence": "temporary",
            "wav_sha256": _sha256(wav_path),
            "hash_algorithm": HASH_ALGORITHM,
            "canonicalization": CANONICALIZATION,
            "manifest_canonical_sha256": canonical_json_file_sha256(
                manifest_path
            ),
            "events_canonical_sha256": canonical_json_file_sha256(events_path),
            "coverage": coverage,
            "human_review": "pending",
        }
        if not output_path.parent.is_dir():
            raise FileNotFoundError(
                f"generator did not create staged report directory: "
                f"{output_path.parent}"
            )
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return report

    def _seed_existing_batch_manifest(
        self,
        entries: list[dict[str, object]],
        **extra: object,
    ) -> Path:
        normalized_entries: list[dict[str, object]] = []
        for entry in entries:
            normalized = dict(entry)
            normalized.setdefault("declared_ranges", [[60, 60]])
            normalized.setdefault("gaps", [])
            normalized.setdefault("key_count", 1)
            normalized_entries.append(normalized)
        path = self.output_root / "_试听清单.json"
        document: dict[str, object] = {
            "schema_version": 2,
            "profile": self.tool.PROFILE_ASCENDING_SCALE,
            "protocol": self.tool.PROTOCOL_ID,
            "instrument_count": len(normalized_entries),
            "instruments": normalized_entries,
        }
        document.update(extra)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (self.output_root / "_试听顺序.txt").write_text(
            self.tool._render_batch_order(normalized_entries),
            encoding="utf-8",
        )
        return path

    def test_repeated_only_cli_and_selector_resolution_are_exact(self) -> None:
        args = self.tool._parse_args(
            [
                "--only",
                "键盘乐器/测试钢琴",
                "--only",
                "测试长笛",
            ]
        )
        self.assertEqual(
            args.only,
            ["键盘乐器/测试钢琴", "测试长笛"],
        )

        piano, _ = self._make_instrument("键盘乐器/测试钢琴", ["old"])
        flute, _ = self._make_instrument("管弦乐/测试长笛", ["old"])
        entries = [
            SimpleNamespace(manifest_path=piano),
            SimpleNamespace(manifest_path=flute),
        ]
        selected = self.tool._select_only_entries(
            entries,
            ("键盘乐器\\测试钢琴", "测试长笛", "测试长笛"),
        )
        self.assertEqual(
            [Path(entry.manifest_path) for entry in selected],
            [piano, flute],
        )

        duplicate, _ = self._make_instrument("教学乐器/测试钢琴", ["old"])
        with self.assertRaisesRegex(ValueError, "名称 '测试钢琴' 不唯一"):
            self.tool._select_only_entries(
                [*entries, SimpleNamespace(manifest_path=duplicate)],
                ("测试钢琴",),
            )
        for invalid in ("../测试钢琴", "/测试钢琴", "键盘乐器//测试钢琴"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "--only"):
                    self.tool._select_only_entries(entries, (invalid,))

    def test_selective_full_range_replaces_only_selected_artifacts(self) -> None:
        selected_manifest, _ = self._make_instrument(
            "键盘乐器/选择键盘",
            ["selected old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        untouched_manifest, _ = self._make_instrument(
            "管弦乐/保留长笛",
            ["untouched old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 72,
                "note_max": 73,
            },
        )
        entries = [
            SimpleNamespace(manifest_path=selected_manifest),
            SimpleNamespace(manifest_path=untouched_manifest),
        ]
        self.tool.discover_instruments = lambda _root: entries

        selected_report = selected_manifest.parent / "试听核验.json"
        untouched_report = untouched_manifest.parent / "试听核验.json"
        old_selected_report = selected_report.read_bytes()
        old_untouched_report = untouched_report.read_bytes()

        selected_wav = self.output_root / "键盘乐器" / "选择键盘.wav"
        untouched_wav = self.output_root / "管弦乐" / "保留长笛.wav"
        selected_sidecar = selected_wav.with_name(
            selected_wav.name + ".许可与署名.txt"
        )
        untouched_sidecar = untouched_wav.with_name(
            untouched_wav.name + ".许可与署名.txt"
        )
        for path, content in (
            (selected_wav, b"old selected wav"),
            (selected_sidecar, b"old selected sidecar"),
            (untouched_wav, b"untouched wav"),
            (untouched_sidecar, b"untouched sidecar"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        output_marker = self.output_root / "未选公共文件.txt"
        output_marker.write_bytes(b"untouched output marker")
        batch_manifest = self.output_root / "_试听清单.json"
        old_selected_batch_entry = {
            "order": 1,
            "instrument": "键盘乐器/选择键盘",
            "declared_ranges": [[60, 61]],
            "gaps": [],
            "key_count": 2,
            "wav_sha256": "old-selected-wav-hash",
            "selected_legacy_marker": True,
        }
        old_untouched_batch_entry = {
            "order": 2,
            "instrument": "管弦乐/保留长笛",
            "declared_ranges": [[72, 73]],
            "gaps": [],
            "key_count": 2,
            "wav_sha256": "untouched-wav-hash",
            "untouched_marker": ["must", "stay", "identical"],
        }
        batch_manifest.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "profile": self.tool.PROFILE_ASCENDING_SCALE,
                    "protocol": self.tool.PROTOCOL_ID,
                    "instrument_count": 2,
                    "batch_marker": {"must": "stay"},
                    "instruments": [
                        old_selected_batch_entry,
                        old_untouched_batch_entry,
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        old_batch_manifest = batch_manifest.read_bytes()
        listening_order = self.output_root / "_试听顺序.txt"
        listening_order.write_text(
            self.tool._render_batch_order(
                [old_selected_batch_entry, old_untouched_batch_entry]
            ),
            encoding="utf-8",
        )

        selected_events = (
            self.tool.FULL_RANGE_EVENTS_ROOT
            / "键盘乐器"
            / "选择键盘_全音域上行.events.json"
        )
        untouched_events = (
            self.tool.FULL_RANGE_EVENTS_ROOT
            / "管弦乐"
            / "保留长笛_全音域上行.events.json"
        )
        selected_events.parent.mkdir(parents=True, exist_ok=True)
        untouched_events.parent.mkdir(parents=True, exist_ok=True)
        selected_events.write_bytes(b"old selected events")
        untouched_events.write_bytes(b"untouched events")

        rendered: list[Path] = []

        def fake_generate(*args, **kwargs):
            rendered.append(Path(args[0]))
            report = self._fake_report(*args, **kwargs)
            staged_wav = Path(args[2])
            staged_wav.with_name(
                staged_wav.name + ".许可与署名.txt"
            ).write_bytes(b"new selected sidecar")
            return report

        self.tool.generate_dedicated_audition_verification = fake_generate

        groups = self.tool.generate_all_auditions(
            only=("键盘乐器/选择键盘",)
        )

        self.assertEqual(rendered, [selected_manifest])
        self.assertEqual(sum(map(len, groups.values())), 1)
        self.assertEqual(selected_wav.read_bytes(), b"one synthetic test wav")
        self.assertEqual(selected_sidecar.read_bytes(), b"new selected sidecar")
        self.assertNotEqual(selected_report.read_bytes(), old_selected_report)
        self.assertNotEqual(selected_events.read_bytes(), b"old selected events")
        self.assertEqual(untouched_wav.read_bytes(), b"untouched wav")
        self.assertEqual(untouched_sidecar.read_bytes(), b"untouched sidecar")
        self.assertEqual(untouched_report.read_bytes(), old_untouched_report)
        self.assertEqual(untouched_events.read_bytes(), b"untouched events")
        self.assertEqual(output_marker.read_bytes(), b"untouched output marker")
        self.assertNotEqual(batch_manifest.read_bytes(), old_batch_manifest)
        updated_batch = json.loads(batch_manifest.read_text(encoding="utf-8"))
        self.assertEqual(updated_batch["instrument_count"], 2)
        self.assertEqual(updated_batch["batch_marker"], {"must": "stay"})
        self.assertEqual(
            [entry["instrument"] for entry in updated_batch["instruments"]],
            ["键盘乐器/选择键盘", "管弦乐/保留长笛"],
        )
        self.assertEqual(updated_batch["instruments"][0]["order"], 1)
        self.assertEqual(
            updated_batch["instruments"][0]["wav_sha256"],
            _sha256(selected_wav),
        )
        self.assertEqual(
            updated_batch["instruments"][0]["events_canonical_sha256"],
            canonical_json_file_sha256(selected_events),
        )
        self.assertEqual(
            updated_batch["instruments"][1],
            old_untouched_batch_entry,
        )
        self.assertEqual(
            listening_order.read_text(encoding="utf-8"),
            self.tool._render_batch_order(updated_batch["instruments"]),
        )
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )

    def test_selective_first_build_without_output_tree_creates_no_batch_manifest(
        self,
    ) -> None:
        manifest, _ = self._make_instrument(
            "键盘乐器/首建键盘",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=manifest)
        ]
        rendered = 0

        def fake_generate(*args, **kwargs):
            nonlocal rendered
            rendered += 1
            return self._fake_report(*args, **kwargs)

        self.tool.generate_dedicated_audition_verification = fake_generate
        shutil.rmtree(self.output_root)

        groups = self.tool.generate_all_auditions(only=("首建键盘",))
        repeated_groups = self.tool.generate_all_auditions(
            only=("首建键盘",)
        )

        self.assertEqual(sum(map(len, groups.values())), 1)
        self.assertEqual(sum(map(len, repeated_groups.values())), 1)
        self.assertEqual(rendered, 2)
        self.assertTrue(
            (self.output_root / "键盘乐器" / "首建键盘.wav").is_file()
        )
        self.assertTrue(
            (
                self.tool.FULL_RANGE_EVENTS_ROOT
                / "键盘乐器"
                / "首建键盘_全音域上行.events.json"
            ).is_file()
        )
        self.assertFalse((self.output_root / "_试听清单.json").exists())
        self.assertFalse((self.output_root / "_试听顺序.txt").exists())
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )

    def test_selective_rejects_invalid_or_unmatched_existing_batch_manifest(
        self,
    ) -> None:
        manifest, _ = self._make_instrument(
            "键盘乐器/清单键盘",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=manifest)
        ]
        rendered = 0

        def should_not_render(*args, **kwargs):
            nonlocal rendered
            rendered += 1
            return self._fake_report(*args, **kwargs)

        self.tool.generate_dedicated_audition_verification = should_not_render
        batch_manifest = self.output_root / "_试听清单.json"
        cases = {
            "invalid-json": b"{not-json",
            "count-mismatch": json.dumps(
                {
                    "schema_version": 2,
                    "profile": self.tool.PROFILE_ASCENDING_SCALE,
                    "protocol": self.tool.PROTOCOL_ID,
                    "instrument_count": 2,
                    "instruments": [
                        {"order": 1, "instrument": "键盘乐器/清单键盘"}
                    ],
                }
            ).encode("utf-8"),
            "selected-missing": json.dumps(
                {
                    "schema_version": 2,
                    "profile": self.tool.PROFILE_ASCENDING_SCALE,
                    "protocol": self.tool.PROTOCOL_ID,
                    "instrument_count": 1,
                    "instruments": [
                        {"order": 1, "instrument": "管弦乐/别的乐器"}
                    ],
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        }
        for label, content in cases.items():
            with self.subTest(label=label):
                batch_manifest.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.tool.generate_all_auditions(only=("清单键盘",))
                self.assertEqual(rendered, 0)
                self.assertEqual(batch_manifest.read_bytes(), content)
                self.assertEqual(
                    list(
                        (self.root / "output").glob(
                            ".生成全音域试音-*"
                        )
                    ),
                    [],
                )

    def test_selective_rejects_incomplete_reordered_or_stale_full_roster(
        self,
    ) -> None:
        first_manifest, _ = self._make_instrument(
            "键盘乐器/名册甲",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        second_manifest, _ = self._make_instrument(
            "管弦乐/名册乙",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 72,
                "note_max": 73,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=first_manifest),
            SimpleNamespace(manifest_path=second_manifest),
        ]
        rendered = 0

        def should_not_render(*args, **kwargs):
            nonlocal rendered
            rendered += 1
            return self._fake_report(*args, **kwargs)

        self.tool.generate_dedicated_audition_verification = should_not_render
        cases = (
            (
                "truncated",
                [{"order": 1, "instrument": "键盘乐器/名册甲"}],
                False,
            ),
            (
                "reordered",
                [
                    {"order": 1, "instrument": "管弦乐/名册乙"},
                    {"order": 2, "instrument": "键盘乐器/名册甲"},
                ],
                False,
            ),
            (
                "non-contiguous-order",
                [
                    {"order": 1, "instrument": "键盘乐器/名册甲"},
                    {"order": 3, "instrument": "管弦乐/名册乙"},
                ],
                False,
            ),
            (
                "stale-order-text",
                [
                    {"order": 1, "instrument": "键盘乐器/名册甲"},
                    {"order": 2, "instrument": "管弦乐/名册乙"},
                ],
                True,
            ),
        )
        for label, entries, corrupt_order_text in cases:
            with self.subTest(label=label):
                self._seed_existing_batch_manifest(entries)
                if corrupt_order_text:
                    (self.output_root / "_试听顺序.txt").write_text(
                        "stale\n",
                        encoding="utf-8",
                    )
                with self.assertRaises(ValueError):
                    self.tool.generate_all_auditions(only=("名册甲",))
                self.assertEqual(rendered, 0)
                self.assertEqual(
                    list(
                        (self.root / "output").glob(
                            ".生成全音域试音-*"
                        )
                    ),
                    [],
                )

    def test_selective_render_failure_preserves_all_existing_files(self) -> None:
        first_manifest, _ = self._make_instrument(
            "键盘乐器/事务甲",
            ["first old report"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        second_manifest, _ = self._make_instrument(
            "键盘乐器/事务乙",
            ["second old report"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 62,
                "note_max": 63,
            },
        )
        untouched_manifest, _ = self._make_instrument(
            "管弦乐/事务外长笛",
            ["untouched report"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 72,
                "note_max": 73,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=first_manifest),
            SimpleNamespace(manifest_path=second_manifest),
            SimpleNamespace(manifest_path=untouched_manifest),
        ]
        batch_manifest = self._seed_existing_batch_manifest(
            [
                {"order": 1, "instrument": "键盘乐器/事务甲"},
                {"order": 2, "instrument": "键盘乐器/事务乙"},
                {"order": 3, "instrument": "管弦乐/事务外长笛"},
            ],
            batch_marker="old batch",
        )

        old_files: dict[Path, bytes] = {batch_manifest: batch_manifest.read_bytes()}
        for relative, content in (
            ("键盘乐器/事务甲.wav", b"old first wav"),
            ("键盘乐器/事务乙.wav", b"old second wav"),
            ("管弦乐/事务外长笛.wav", b"untouched wav"),
        ):
            path = self.output_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            old_files[path] = content
        for relative, content in (
            ("键盘乐器/事务甲_全音域上行.events.json", b"old first events"),
            ("键盘乐器/事务乙_全音域上行.events.json", b"old second events"),
            ("管弦乐/事务外长笛_全音域上行.events.json", b"untouched events"),
        ):
            path = self.tool.FULL_RANGE_EVENTS_ROOT / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            old_files[path] = content
        for instrument_manifest in (
            first_manifest,
            second_manifest,
            untouched_manifest,
        ):
            report = instrument_manifest.parent / "试听核验.json"
            old_files[report] = report.read_bytes()

        rendered: list[Path] = []

        def fail_second(*args, **kwargs):
            manifest_path = Path(args[0])
            rendered.append(manifest_path)
            if manifest_path == second_manifest:
                raise RuntimeError("second selected render failed")
            return self._fake_report(*args, **kwargs)

        self.tool.generate_dedicated_audition_verification = fail_second

        with self.assertRaises(self.tool.AuditionBatchError) as raised:
            self.tool.generate_all_auditions(
                only=("键盘乐器/事务甲", "事务乙")
            )

        self.assertEqual(rendered, [first_manifest, second_manifest])
        self.assertIn("second selected render failed", raised.exception.failures[0][1])
        for path, content in old_files.items():
            with self.subTest(path=path):
                self.assertEqual(path.read_bytes(), content)
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )

    def test_selective_commit_failure_rolls_back_manifest_wav_event_and_report(
        self,
    ) -> None:
        selected_manifest, _ = self._make_instrument(
            "键盘乐器/提交回滚键盘",
            ["old selected report"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        untouched_manifest, _ = self._make_instrument(
            "管弦乐/提交外长笛",
            ["untouched report"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 72,
                "note_max": 73,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=selected_manifest),
            SimpleNamespace(manifest_path=untouched_manifest),
        ]
        batch_manifest = self._seed_existing_batch_manifest(
            [
                {"order": 1, "instrument": "键盘乐器/提交回滚键盘"},
                {"order": 2, "instrument": "管弦乐/提交外长笛"},
            ]
        )
        selected_wav = self.output_root / "键盘乐器" / "提交回滚键盘.wav"
        untouched_wav = self.output_root / "管弦乐" / "提交外长笛.wav"
        selected_events = (
            self.tool.FULL_RANGE_EVENTS_ROOT
            / "键盘乐器"
            / "提交回滚键盘_全音域上行.events.json"
        )
        untouched_events = (
            self.tool.FULL_RANGE_EVENTS_ROOT
            / "管弦乐"
            / "提交外长笛_全音域上行.events.json"
        )
        for path, content in (
            (selected_wav, b"old selected wav"),
            (untouched_wav, b"untouched wav"),
            (selected_events, b"old selected events"),
            (untouched_events, b"untouched events"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        selected_report = selected_manifest.parent / "试听核验.json"
        untouched_report = untouched_manifest.parent / "试听核验.json"
        old_files = {
            batch_manifest: batch_manifest.read_bytes(),
            self.output_root / "_试听顺序.txt": (
                self.output_root / "_试听顺序.txt"
            ).read_bytes(),
            selected_wav: selected_wav.read_bytes(),
            untouched_wav: untouched_wav.read_bytes(),
            selected_events: selected_events.read_bytes(),
            untouched_events: untouched_events.read_bytes(),
            selected_report: selected_report.read_bytes(),
            untouched_report: untouched_report.read_bytes(),
        }
        self.tool.generate_dedicated_audition_verification = self._fake_report
        real_replace = self.tool.os.replace
        destinations: list[Path] = []

        def fail_selected_report(source, destination):
            destination = Path(destination)
            destinations.append(destination)
            if destination == selected_report:
                raise OSError("selected report commit failed")
            return real_replace(source, destination)

        with mock.patch.object(self.tool.os, "replace", fail_selected_report):
            with self.assertRaisesRegex(
                OSError,
                "selected report commit failed",
            ):
                self.tool.generate_all_auditions(
                    only=("提交回滚键盘",)
                )

        self.assertIn(batch_manifest, destinations)
        self.assertIn(self.output_root / "_试听顺序.txt", destinations)
        self.assertIn(selected_wav, destinations)
        self.assertIn(selected_events, destinations)
        for path, content in old_files.items():
            with self.subTest(path=path):
                self.assertEqual(path.read_bytes(), content)
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )

    def test_incomplete_selective_rollback_preserves_recovery_directory(
        self,
    ) -> None:
        selected_manifest, _ = self._make_instrument(
            "键盘乐器/恢复键盘",
            ["old selected report"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        untouched_manifest, _ = self._make_instrument(
            "管弦乐/恢复外长笛",
            ["untouched report"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 72,
                "note_max": 73,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=selected_manifest),
            SimpleNamespace(manifest_path=untouched_manifest),
        ]
        batch_manifest = self._seed_existing_batch_manifest(
            [
                {"order": 1, "instrument": "键盘乐器/恢复键盘"},
                {"order": 2, "instrument": "管弦乐/恢复外长笛"},
            ]
        )
        selected_wav = self.output_root / "键盘乐器" / "恢复键盘.wav"
        selected_events = (
            self.tool.FULL_RANGE_EVENTS_ROOT
            / "键盘乐器"
            / "恢复键盘_全音域上行.events.json"
        )
        selected_wav.parent.mkdir(parents=True, exist_ok=True)
        selected_events.parent.mkdir(parents=True, exist_ok=True)
        selected_wav.write_bytes(b"old selected wav")
        selected_events.write_bytes(b"old selected events")
        self.tool.generate_dedicated_audition_verification = self._fake_report

        final_targets = {
            batch_manifest,
            self.output_root / "_试听顺序.txt",
            selected_wav,
            selected_events,
            selected_manifest.parent / "试听核验.json",
        }
        real_replace = self.tool.os.replace
        first_published: Path | None = None
        commit_failed = False

        def fail_commit_then_rollback(source, destination):
            nonlocal first_published, commit_failed
            source = Path(source)
            destination = Path(destination)
            if destination in final_targets and not commit_failed:
                if first_published is None:
                    first_published = destination
                    return real_replace(source, destination)
                commit_failed = True
                raise KeyboardInterrupt("intentional selective commit interruption")
            if (
                commit_failed
                and destination == first_published
                and source.name.endswith(".previous")
            ):
                raise OSError("intentional selective rollback failure")
            return real_replace(source, destination)

        with mock.patch.object(
            self.tool.os,
            "replace",
            fail_commit_then_rollback,
        ):
            with self.assertRaisesRegex(
                self.tool.AuditionRollbackError,
                "回滚不完整",
            ) as raised:
                self.tool.generate_all_auditions(only=("恢复键盘",))

        recovery = raised.exception.recovery_path
        self.assertTrue(recovery.is_dir())
        self._assert_only_recovery_directory(recovery)
        backups = list((recovery / "previous-selected-files").glob("*.previous"))
        self.assertTrue(backups)
        self.assertTrue(
            any(path.read_bytes() == b"old selected events" for path in backups)
        )

    def test_existing_renders_once_uses_honest_coverage_and_current_hashes(
        self,
    ) -> None:
        first_manifest, first_events = self._make_instrument(
            "键盘乐器/测试钢琴",
            ["低中高音域", "三档力度"],
            extra_review=("fallback_ab_review", "pending"),
            add_pitch_probe=True,
        )
        second_manifest, second_events = self._make_instrument(
            "管弦乐/测试长笛",
            ["全音域", "长短音"],
        )
        entries = [
            SimpleNamespace(manifest_path=first_manifest),
            SimpleNamespace(manifest_path=second_manifest),
        ]
        calls: list[tuple[Path, Path, Path]] = []

        def fake_generate(*args, **kwargs):
            calls.append((Path(args[0]), Path(args[1]), Path(args[2])))
            return self._fake_report(*args, **kwargs)

        self.tool.discover_instruments = lambda _root: entries
        self.tool.generate_dedicated_audition_verification = fake_generate
        (self.output_root / "旧文件.txt").write_text("old", encoding="utf-8")

        groups = self.tool.generate_all_auditions(
            profile=self.tool.PROFILE_EXISTING
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual({call[0] for call in calls}, {first_manifest, second_manifest})
        self.assertEqual({call[1] for call in calls}, {first_events, second_events})
        self.assertFalse((self.output_root / "旧文件.txt").exists())
        self.assertEqual(sum(map(len, groups.values())), 2)

        first_directory = first_manifest.parent
        first_report = json.loads(
            (first_directory / "试听核验.json").read_text(encoding="utf-8")
        )
        coverage = " ".join(first_report["coverage"])
        self.assertIn("旧固定谱例复算", coverage)
        self.assertIn("不声明全音域", coverage)
        self.assertNotIn("低中高音域", coverage)
        self.assertEqual(first_report["fallback_ab_review"], "pending")
        self.assertEqual(first_report["hash_algorithm"], HASH_ALGORITHM)
        self.assertEqual(first_report["canonicalization"], CANONICALIZATION)
        self.assertEqual(
            first_report["manifest_canonical_sha256"],
            canonical_json_file_sha256(first_manifest),
        )
        self.assertEqual(
            first_report["events_canonical_sha256"],
            canonical_json_file_sha256(first_events),
        )
        self.assertEqual(
            first_report["wav"],
            "output/全音域试音/键盘乐器/测试钢琴.wav",
        )
        self.assertTrue(
            (self.output_root / "键盘乐器" / "测试钢琴.wav").is_file()
        )
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )
        self.assertEqual(
            (self.legacy_output_root / "不可变旧试听.txt").read_text(
                encoding="utf-8"
            ),
            "legacy",
        )

    def test_existing_profile_ignores_nested_full_range_score_after_hash_switch(
        self,
    ) -> None:
        manifest, legacy = self._make_instrument(
            "键盘乐器/测试钢琴",
            ["old"],
        )
        nested = (
            self.examples
            / "全音域上行"
            / "键盘乐器"
            / "测试钢琴_全音域上行.events.json"
        )
        nested.parent.mkdir(parents=True)
        nested.write_text(
            json.dumps({"events": [{"new": True}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        report_path = manifest.parent / "试听核验.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["hash_algorithm"] = HASH_ALGORITHM
        report["canonicalization"] = CANONICALIZATION
        report["events_canonical_sha256"] = canonical_json_file_sha256(nested)
        report.pop("events_sha256", None)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False),
            encoding="utf-8",
        )

        self.assertEqual(self.tool._events_for(manifest.parent), legacy)

    def test_full_range_then_existing_switches_events_and_coverage_together(
        self,
    ) -> None:
        manifest, legacy = self._make_instrument(
            "键盘乐器/切换键盘",
            ["旧覆盖声明"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=manifest)
        ]
        self.tool.generate_dedicated_audition_verification = self._fake_report

        self.tool.generate_all_auditions(
            profile=self.tool.PROFILE_ASCENDING_SCALE
        )
        report_path = manifest.parent / "试听核验.json"
        full_range = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertIn("全声明合法键升序", " ".join(full_range["coverage"]))

        self.tool.generate_all_auditions(
            profile=self.tool.PROFILE_EXISTING
        )
        existing = json.loads(report_path.read_text(encoding="utf-8"))

        coverage = " ".join(existing["coverage"])
        self.assertEqual(existing["audition_profile"], "existing")
        self.assertEqual(
            existing["events_canonical_sha256"],
            canonical_json_file_sha256(legacy),
        )
        self.assertIn("旧固定谱例复算", coverage)
        self.assertIn("不声明全音域", coverage)
        self.assertNotIn("全声明合法键升序", coverage)
        self.assertTrue(self.tool.FULL_RANGE_EVENTS_ROOT.is_dir())

    def test_render_failure_leaves_old_output_and_reports_untouched(self) -> None:
        first_manifest, _ = self._make_instrument(
            "键盘乐器/测试钢琴",
            ["旧覆盖一"],
        )
        second_manifest, _ = self._make_instrument(
            "管弦乐/测试长笛",
            ["旧覆盖二"],
        )
        entries = [
            SimpleNamespace(manifest_path=first_manifest),
            SimpleNamespace(manifest_path=second_manifest),
        ]
        old_reports = {
            manifest.parent: (manifest.parent / "试听核验.json").read_bytes()
            for manifest in (first_manifest, second_manifest)
        }
        calls: list[Path] = []

        def fail_second(*args, **kwargs):
            manifest = Path(args[0])
            calls.append(manifest)
            if manifest == second_manifest:
                raise RuntimeError("intentional render failure")
            return self._fake_report(*args, **kwargs)

        self.tool.discover_instruments = lambda _root: entries
        self.tool.generate_dedicated_audition_verification = fail_second
        marker = self.output_root / "旧试听保留.txt"
        marker.write_text("old", encoding="utf-8")

        with self.assertRaises(self.tool.AuditionBatchError) as raised:
            self.tool.generate_all_auditions(
                profile=self.tool.PROFILE_EXISTING
            )

        self.assertEqual(len(raised.exception.failures), 1)
        self.assertEqual(calls, [first_manifest, second_manifest])
        self.assertEqual(marker.read_text(encoding="utf-8"), "old")
        for directory, old_content in old_reports.items():
            self.assertEqual(
                (directory / "试听核验.json").read_bytes(),
                old_content,
            )
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )

    def test_commit_failure_rolls_back_output_and_every_report(self) -> None:
        first_manifest, _ = self._make_instrument(
            "键盘乐器/测试钢琴",
            ["旧覆盖一"],
        )
        second_manifest, _ = self._make_instrument(
            "管弦乐/测试长笛",
            ["旧覆盖二"],
        )
        entries = [
            SimpleNamespace(manifest_path=first_manifest),
            SimpleNamespace(manifest_path=second_manifest),
        ]
        old_reports = {
            manifest.parent: (manifest.parent / "试听核验.json").read_bytes()
            for manifest in (first_manifest, second_manifest)
        }
        marker = self.output_root / "旧试听保留.txt"
        marker.write_text("old", encoding="utf-8")
        self.tool.discover_instruments = lambda _root: entries
        self.tool.generate_dedicated_audition_verification = self._fake_report

        real_replace = self.tool.os.replace
        failed = False
        second_report = second_manifest.parent / "试听核验.json"

        def fail_one_report_commit(source, destination):
            nonlocal failed
            if Path(destination) == second_report and not failed:
                failed = True
                raise OSError("intentional commit failure")
            return real_replace(source, destination)

        with mock.patch.object(self.tool.os, "replace", fail_one_report_commit):
            with self.assertRaisesRegex(OSError, "intentional commit failure"):
                self.tool.generate_all_auditions(
                    profile=self.tool.PROFILE_EXISTING
                )

        self.assertTrue(failed)
        self.assertEqual(marker.read_text(encoding="utf-8"), "old")
        for directory, old_content in old_reports.items():
            self.assertEqual(
                (directory / "试听核验.json").read_bytes(),
                old_content,
            )
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )

    def test_incomplete_full_batch_report_rollback_preserves_old_reports(
        self,
    ) -> None:
        first_manifest, _ = self._make_instrument(
            "键盘乐器/完整恢复甲",
            ["old first report"],
        )
        second_manifest, _ = self._make_instrument(
            "管弦乐/完整恢复乙",
            ["old second report"],
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=first_manifest),
            SimpleNamespace(manifest_path=second_manifest),
        ]
        self.tool.generate_dedicated_audition_verification = self._fake_report
        first_report = first_manifest.parent / "试听核验.json"
        second_report = second_manifest.parent / "试听核验.json"
        old_first_report = first_report.read_bytes()
        old_second_report = second_report.read_bytes()
        output_marker = self.output_root / "old.txt"
        output_marker.write_text("old output", encoding="utf-8")

        real_replace = self.tool.os.replace
        commit_interrupted = False

        def interrupt_second_report_and_fail_first_restore(source, destination):
            nonlocal commit_interrupted
            source = Path(source)
            destination = Path(destination)
            if destination == second_report and not commit_interrupted:
                commit_interrupted = True
                raise KeyboardInterrupt("full batch report commit interrupted")
            if (
                commit_interrupted
                and destination == first_report
                and source.parent.name == "previous-reports"
            ):
                raise OSError("full batch report restore failed")
            return real_replace(source, destination)

        with mock.patch.object(
            self.tool.os,
            "replace",
            interrupt_second_report_and_fail_first_restore,
        ):
            with self.assertRaisesRegex(
                self.tool.AuditionRollbackError,
                "回滚不完整",
            ) as raised:
                self.tool.generate_all_auditions(
                    profile=self.tool.PROFILE_EXISTING
                )

        self.assertTrue(commit_interrupted)
        recovery = raised.exception.recovery_path
        self.assertTrue(recovery.is_dir())
        self._assert_only_recovery_directory(recovery)
        backups = list((recovery / "previous-reports").glob("*.previous.json"))
        self.assertEqual(len(backups), 2)
        self.assertIn(old_first_report, [path.read_bytes() for path in backups])
        self.assertIn(old_second_report, [path.read_bytes() for path in backups])
        self.assertEqual(output_marker.read_text(encoding="utf-8"), "old output")
        self.assertEqual(second_report.read_bytes(), old_second_report)

    def test_failure_moving_old_output_never_deletes_that_output(self) -> None:
        old_marker = self.output_root / "old.txt"
        old_marker.write_text("old output", encoding="utf-8")
        transaction = self.root / "output" / ".transaction-output-move"
        staged_output = transaction / "全音域试音"
        staged_output.mkdir(parents=True)
        (staged_output / "new.txt").write_text("new", encoding="utf-8")
        previous_output = transaction / "previous-output"
        real_replace = self.tool.os.replace

        def fail_first_move(source, destination):
            if (
                Path(source) == self.output_root
                and Path(destination) == previous_output
            ):
                raise OSError("cannot move old output")
            return real_replace(source, destination)

        with mock.patch.object(self.tool.os, "replace", fail_first_move):
            with self.assertRaisesRegex(OSError, "cannot move old output"):
                self.tool._commit_batch(
                    transaction,
                    staged_output,
                    {},
                )

        self.assertEqual(old_marker.read_text(encoding="utf-8"), "old output")
        self.assertTrue((staged_output / "new.txt").is_file())

    def test_commit_refuses_immutable_legacy_output_root(self) -> None:
        transaction = self.root / "output" / ".legacy-refusal"
        staged_output = transaction / "全音域试音"
        staged_output.mkdir(parents=True)
        (staged_output / "new.txt").write_text("new", encoding="utf-8")
        self.tool.OUTPUT_ROOT = self.legacy_output_root

        with self.assertRaisesRegex(ValueError, "不可变旧试听目录"):
            self.tool._commit_batch(transaction, staged_output, {})

        self.assertEqual(
            (self.legacy_output_root / "不可变旧试听.txt").read_text(
                encoding="utf-8"
            ),
            "legacy",
        )

    def test_failure_moving_old_events_restores_output_and_keeps_events(
        self,
    ) -> None:
        old_output = self.output_root / "old.txt"
        old_output.write_text("old output", encoding="utf-8")
        self.tool.FULL_RANGE_EVENTS_ROOT.mkdir(parents=True)
        old_events = self.tool.FULL_RANGE_EVENTS_ROOT / "old.txt"
        old_events.write_text("old events", encoding="utf-8")

        transaction = self.root / "output" / ".transaction-events-move"
        staged_output = transaction / "全音域试音"
        staged_events = transaction / "全音域上行"
        staged_output.mkdir(parents=True)
        staged_events.mkdir(parents=True)
        (staged_output / "new.txt").write_text("new output", encoding="utf-8")
        (staged_events / "new.txt").write_text("new events", encoding="utf-8")
        previous_events = transaction / "previous-events"
        real_replace = self.tool.os.replace

        def fail_events_move(source, destination):
            if (
                Path(source) == self.tool.FULL_RANGE_EVENTS_ROOT
                and Path(destination) == previous_events
            ):
                raise OSError("cannot move old events")
            return real_replace(source, destination)

        with mock.patch.object(self.tool.os, "replace", fail_events_move):
            with self.assertRaisesRegex(OSError, "cannot move old events"):
                self.tool._commit_batch(
                    transaction,
                    staged_output,
                    {},
                    staged_events=staged_events,
                )

        self.assertEqual(old_output.read_text(encoding="utf-8"), "old output")
        self.assertEqual(old_events.read_text(encoding="utf-8"), "old events")
        self.assertTrue((staged_events / "new.txt").is_file())

    def test_default_profile_builds_full_range_events_manifest_and_hashes(
        self,
    ) -> None:
        manifest, _legacy_events = self._make_instrument(
            "键盘乐器/测试键盘",
            ["旧定制谱覆盖"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 62,
            },
        )
        old_report_path = manifest.parent / "试听核验.json"
        old_report = json.loads(old_report_path.read_text(encoding="utf-8"))
        old_report["fallback_ab_review"] = "approved-old-wave"
        old_report["range_policy"] = {"old_window": [1.0, 2.0]}
        old_report_path.write_text(
            json.dumps(old_report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=manifest)
        ]
        captured: list[dict] = []

        def fake_generate(*args, **kwargs):
            captured.append(
                json.loads(Path(args[1]).read_text(encoding="utf-8"))
            )
            return self._fake_report(*args, **kwargs)

        self.tool.generate_dedicated_audition_verification = fake_generate

        groups = self.tool.generate_all_auditions()

        self.assertEqual(sum(map(len, groups.values())), 1)
        note_ons = [
            event
            for event in captured[0]["events"]
            if event["type"] == "note_on"
        ]
        note_offs = [
            event
            for event in captured[0]["events"]
            if event["type"] == "note_off"
        ]
        self.assertEqual(
            [event["midi_note"] for event in note_ons],
            [60, 61, 62],
        )
        self.assertEqual(
            {event["velocity"] for event in note_ons},
            {self.tool.VELOCITY},
        )
        self.assertEqual(
            captured[0]["tuning"],
            {"temperament": "equal", "a4_hz": 440.0},
        )
        for note_off, following in zip(note_offs, note_ons[1:]):
            self.assertLess(note_off["time"], following["time"])

        final_events = (
            self.tool.FULL_RANGE_EVENTS_ROOT
            / "键盘乐器"
            / "测试键盘_全音域上行.events.json"
        )
        self.assertTrue(final_events.is_file())
        report = json.loads(old_report_path.read_text(encoding="utf-8"))
        self.assertEqual(
            report["events_canonical_sha256"],
            canonical_json_file_sha256(final_events),
        )
        self.assertEqual(report["audition_profile"], "ascending-scale")
        self.assertEqual(
            report["audition_protocol"],
            self.tool.PROTOCOL_ID,
        )
        self.assertEqual(report["human_review"], "pending")
        self.assertEqual(
            report["wav"],
            "output/全音域试音/键盘乐器/测试键盘.wav",
        )
        self.assertNotIn("fallback_ab_review", report)
        self.assertNotIn("range_policy", report)
        archived = report["previous_protocol_evidence"]
        self.assertEqual(
            archived["fields"]["range_policy"],
            {"old_window": [1.0, 2.0]},
        )
        self.assertIn("3 键", " ".join(report["coverage"]))

        # Re-running the same full-range protocol must not replace the
        # archived specialized evidence with ordinary report metadata.
        self.tool.generate_all_auditions()
        rerendered = json.loads(
            old_report_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            rerendered["previous_protocol_evidence"],
            archived,
        )

        batch = json.loads(
            (self.output_root / "_试听清单.json").read_text(encoding="utf-8")
        )
        self.assertEqual(batch["schema_version"], 2)
        self.assertEqual(batch["instrument_count"], 1)
        self.assertEqual(batch["wav_persistence"], "temporary")
        self.assertEqual(
            batch["settings"],
            {
                "pitch_unit": (
                    "concert_midi_note_or_unpitched_trigger_key"
                ),
                "temperament": "equal",
                "a4_hz": 440.0,
                "sample_rate": 48_000,
                "channels": 2,
                "velocity": 0.72,
                "default_gate_seconds": 0.48,
                "default_gap_seconds": 0.12,
                "default_tail_seconds": 1.5,
                "single_note_events_overlap": False,
                "single_note_event_lifetimes_overlap": False,
                "audio_release_tails_may_overlap": True,
                "timing_semantics": (
                    "常规批次是紧凑压力扫描；note事件不重叠不代表声音释音不重叠。"
                    "音色修复验收应使用 full-range-chromatic-isolated-v1。"
                ),
                "minimum_peak": self.tool.MIN_AUDITION_PEAK,
                "minimum_rms": self.tool.MIN_AUDITION_RMS,
                "maximum_peak_exclusive": 0.999,
            },
        )
        entry = batch["instruments"][0]
        self.assertEqual(entry["wav_persistence"], "temporary")
        self.assertEqual(entry["hash_algorithm"], HASH_ALGORITHM)
        self.assertEqual(entry["canonicalization"], CANONICALIZATION)
        self.assertEqual(
            entry["manifest_canonical_sha256"],
            canonical_json_file_sha256(manifest),
        )
        self.assertEqual(entry["declared_ranges"], [[60, 62]])
        self.assertEqual(entry["key_count"], 3)
        self.assertEqual(entry["event_count"], 3)
        self.assertEqual(entry["gaps"], [])
        self.assertEqual(entry["gap_seconds"], [0.12, 0.12, 0.12])
        self.assertEqual(entry["tail_seconds"], 1.5)
        self.assertTrue((self.output_root / "_试听顺序.txt").is_file())
        self.assertTrue(
            (self.legacy_output_root / "不可变旧试听.txt").is_file()
        )

    def test_quarantined_entry_is_not_rendered_but_keeps_event_recipe(
        self,
    ) -> None:
        active_manifest, _ = self._make_instrument(
            "键盘乐器/公开键盘",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 60,
                "license_status": "approved",
            },
        )
        quarantined_manifest, _ = self._make_instrument(
            "世界乐器/隔离班卓",
            ["historical"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 48,
                "note_max": 48,
                "license_status": "quarantined",
            },
        )
        quarantined_report = quarantined_manifest.parent / "试听核验.json"
        old_report_bytes = quarantined_report.read_bytes()
        old_events = (
            self.tool.FULL_RANGE_EVENTS_ROOT
            / "世界乐器"
            / "隔离班卓_全音域上行.events.json"
        )
        old_events.parent.mkdir(parents=True, exist_ok=True)
        old_events.write_text(
            json.dumps(
                {"instrument": "隔离班卓", "events": [{"type": "note_on"}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        old_events_bytes = old_events.read_bytes()
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(
                manifest_path=active_manifest,
                license_status="approved",
            ),
            SimpleNamespace(
                manifest_path=quarantined_manifest,
                license_status="quarantined",
            ),
        ]
        rendered: list[Path] = []

        def fake_generate(*args, **kwargs):
            rendered.append(Path(args[0]))
            return self._fake_report(*args, **kwargs)

        self.tool.generate_dedicated_audition_verification = fake_generate

        groups = self.tool.generate_all_auditions()

        self.assertEqual(sum(map(len, groups.values())), 1)
        self.assertEqual(rendered, [active_manifest])
        self.assertEqual(quarantined_report.read_bytes(), old_report_bytes)
        self.assertEqual(old_events.read_bytes(), old_events_bytes)
        self.assertFalse(
            (self.output_root / "世界乐器" / "隔离班卓.wav").exists()
        )
        batch = json.loads(
            (self.output_root / "_试听清单.json").read_text(encoding="utf-8")
        )
        self.assertEqual(batch["instrument_count"], 1)

    def test_segmented_ranges_skip_only_declared_holes_and_report_them(
        self,
    ) -> None:
        manifest, _ = self._make_instrument(
            "世界乐器/分段音域",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 36,
                "note_max": 42,
                "playable_ranges": [[36, 38], [41, 42]],
            },
        )

        plan = self.tool.build_full_range_audition(
            manifest,
            instrument_root=self.instrument_root,
        )

        self.assertEqual(plan.keys, (36, 37, 38, 41, 42))
        self.assertEqual(plan.declared_ranges, ((36, 38), (41, 42)))
        self.assertEqual(plan.gaps, ((39, 40),))
        self.assertIn("MIDI 39-40", " ".join(plan.coverage))

    def test_unpitched_keys_are_derived_without_silent_pitch_fallback(
        self,
    ) -> None:
        fixed_manifest, _ = self._make_instrument(
            "现代鼓组/固定鼓",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "pitch_mode": "fixed",
                "fixed_midi_note": 36,
                "default_articulation": "hit",
            },
            unpitched=True,
        )
        fixed = self.tool.build_full_range_audition(
            fixed_manifest,
            instrument_root=self.instrument_root,
        )
        self.assertEqual(fixed.keys, (36, 36, 36, 36))
        self.assertEqual(fixed.unique_keys, (36,))

        vpo_manifest, _ = self._make_instrument(
            "管弦乐/打击乐组/VPO木鱼",
            ["old"],
            manifest_updates={
                "type": "vpo_percussion",
                "profile": "woodblock",
                "default_articulation": "high",
            },
            unpitched=True,
        )
        vpo = self.tool.build_full_range_audition(
            vpo_manifest,
            instrument_root=self.instrument_root,
        )
        self.assertEqual(vpo.keys, (76, 77))
        self.assertEqual(
            [strike.articulation for strike in vpo.sequence],
            ["low", "high"],
        )

        sfx_manifest, _ = self._make_instrument(
            "环境与拟音/测试音效",
            ["old"],
            manifest_updates={
                "type": "procedural_sfx",
                "profile": "breath",
            },
        )
        sfx = self.tool.build_full_range_audition(
            sfx_manifest,
            instrument_root=self.instrument_root,
        )
        self.assertEqual(sfx.keys, (60,))
        self.assertIn("明确忽略音高", str(sfx.exception))
        self.assertAlmostEqual(sfx.sequence[0].duration_seconds, 1.28)
        self.assertEqual(sfx.tail_seconds, 1.5)
        self.assertIn("attack 0.08s + 稳态 1.2s", str(sfx.exception))

        unresolved_manifest, _ = self._make_instrument(
            "环境与拟音/未声明触发键",
            ["old"],
            manifest_updates={"type": "reversed_cymbal"},
        )
        with self.assertRaisesRegex(
            ValueError,
            "没有可证明的后端单触发例外",
        ):
            self.tool.build_full_range_audition(
                unresolved_manifest,
                instrument_root=self.instrument_root,
            )

    def test_procedural_sfx_one_shot_and_long_scene_lifecycles(
        self,
    ) -> None:
        gun_manifest, _ = self._make_instrument(
            "环境与拟音/测试枪声",
            ["old"],
            manifest_updates={
                "type": "procedural_sfx",
                "profile": "gunshot",
            },
        )
        ocean_manifest, _ = self._make_instrument(
            "环境与拟音/测试海浪",
            ["old"],
            manifest_updates={
                "type": "procedural_sfx",
                "profile": "ocean",
            },
        )

        gun = self.tool.build_full_range_audition(
            gun_manifest,
            instrument_root=self.instrument_root,
        )
        ocean = self.tool.build_full_range_audition(
            ocean_manifest,
            instrument_root=self.instrument_root,
        )

        self.assertEqual(gun.sequence[0].duration_seconds, 2.4)
        self.assertEqual(gun.tail_seconds, 1.5)
        self.assertIn("one-shot 完整持有 2.4s", str(gun.exception))
        self.assertAlmostEqual(ocean.sequence[0].duration_seconds, 2.6)
        self.assertAlmostEqual(ocean.tail_seconds, 2.85)
        self.assertIn("release 2.6s", str(ocean.exception))
        self.assertEqual(ocean.document["tail_seconds"], 2.85)

    def test_slow_pad_uses_attack_gate_and_release_isolation_per_note(
        self,
    ) -> None:
        manifest, _ = self._make_instrument(
            "电子乐器/测试光环铺底",
            ["old"],
            manifest_updates={
                "type": "synthesizer",
                "patch": "halo_pad",
                "note_min": 60,
                "note_max": 62,
            },
        )

        plan = self.tool.build_full_range_audition(
            manifest,
            instrument_root=self.instrument_root,
        )

        self.assertEqual(plan.keys, (60, 61, 62))
        self.assertEqual(
            [strike.duration_seconds for strike in plan.sequence],
            [2.1, 2.1, 2.1],
        )
        self.assertEqual(
            [strike.gap_seconds for strike in plan.sequence],
            [4.32, 4.32, 4.32],
        )
        self.assertEqual(plan.tail_seconds, 4.32)
        note_ons = [
            event
            for event in plan.document["events"]
            if event["type"] == "note_on"
        ]
        self.assertAlmostEqual(
            note_ons[1]["time"] - note_ons[0]["time"],
            6.42,
        )
        metadata = plan.metadata()
        self.assertEqual(metadata["gap_seconds"], [4.32, 4.32, 4.32])
        self.assertEqual(metadata["tail_seconds"], 4.32)
        self.assertIn("慢铺底 halo_pad", str(plan.exception))

    def test_reverse_cymbal_uses_each_verified_full_swell_duration(self) -> None:
        manifest, _ = self._make_instrument(
            "管弦乐/打击乐组/测试反向镲",
            ["old"],
            manifest_updates={
                "type": "reversed_cymbal",
                "note_min": 60,
                "note_max": 62,
                "variants": {
                    "60": {"sample": "unused-a.wav"},
                    "61": {"sample": "unused-b.wav"},
                    "62": {"sample": "unused-c.wav"},
                },
                "resource_verification": "资源核验.json",
            },
        )
        (manifest.parent / "资源核验.json").write_text(
            json.dumps(
                {
                    "variants": {
                        "60": {"swell_seconds": 4.0},
                        "61": {"swell_seconds": 5.5},
                        "62": {"swell_seconds": 7.25},
                    }
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        plan = self.tool.build_full_range_audition(
            manifest,
            instrument_root=self.instrument_root,
        )

        self.assertEqual(
            [strike.duration_seconds for strike in plan.sequence],
            [4.0, 5.5, 7.25],
        )
        self.assertIn("完整持有", str(plan.exception))

    def test_long_release_pitched_scan_records_dampened_override_source(
        self,
    ) -> None:
        manifest, _ = self._make_instrument(
            "管弦乐/拨弦组/测试竖琴",
            ["old"],
            manifest_updates={
                "type": "vpo_harp",
                "note_min": 60,
                "note_max": 62,
                "default_articulation": "open",
            },
        )

        plan = self.tool.build_full_range_audition(
            manifest,
            instrument_root=self.instrument_root,
        )

        self.assertEqual(plan.articulation, "dampened")
        self.assertEqual(
            plan.range_source,
            "capability.articulation_ranges:dampened",
        )
        self.assertIn("30 秒", str(plan.exception))
        note_ons = [
            event
            for event in plan.document["events"]
            if event["type"] == "note_on"
        ]
        self.assertEqual([event["midi_note"] for event in note_ons], [60, 61, 62])

    def test_ascending_peak_gate_rejects_batch_before_commit(self) -> None:
        manifest, _ = self._make_instrument(
            "键盘乐器/过响键盘",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        report_path = manifest.parent / "试听核验.json"
        old_report = report_path.read_bytes()
        marker = self.output_root / "旧试听保留.txt"
        marker.write_text("old", encoding="utf-8")
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=manifest)
        ]

        def too_loud(*args, **kwargs):
            report = self._fake_report(*args, **kwargs)
            report["peak"] = 0.999
            return report

        self.tool.generate_dedicated_audition_verification = too_loud

        with self.assertRaises(self.tool.AuditionBatchError) as raised:
            self.tool.generate_all_auditions()

        self.assertIn("未通过幅度门", raised.exception.failures[0][1])
        self.assertEqual(marker.read_text(encoding="utf-8"), "old")
        self.assertEqual(report_path.read_bytes(), old_report)
        self.assertFalse(self.tool.FULL_RANGE_EVENTS_ROOT.exists())

    def test_peak_and_rms_non_silence_gates_each_reject_the_batch(
        self,
    ) -> None:
        for peak, rms, label in (
            (0.0, 0.1, "peak"),
            (0.1, 0.0, "rms"),
        ):
            with self.subTest(label=label):
                manifest, _ = self._make_instrument(
                    f"键盘乐器/静音键盘-{label}",
                    ["old"],
                    manifest_updates={
                        "type": "oscillator",
                        "note_min": 60,
                        "note_max": 61,
                    },
                )
                report_path = manifest.parent / "试听核验.json"
                old_report = report_path.read_bytes()
                self.tool.discover_instruments = lambda _root, path=manifest: [
                    SimpleNamespace(manifest_path=path)
                ]

                def silent(*args, **kwargs):
                    report = self._fake_report(*args, **kwargs)
                    report["peak"] = peak
                    report["rms"] = rms
                    return report

                self.tool.generate_dedicated_audition_verification = silent
                with self.assertRaises(self.tool.AuditionBatchError) as raised:
                    self.tool.generate_all_auditions()

                self.assertIn(
                    "未通过整件非静音门",
                    raised.exception.failures[0][1],
                )
                self.assertEqual(report_path.read_bytes(), old_report)

    def test_production_count_must_be_exactly_103_before_rendering(self) -> None:
        manifest, _ = self._make_instrument(
            "键盘乐器/唯一入口",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=manifest)
        ]
        self.tool.EXPECTED_INSTRUMENT_COUNT = 103
        render_called = False

        def should_not_render(*args, **kwargs):
            nonlocal render_called
            render_called = True
            return self._fake_report(*args, **kwargs)

        self.tool.generate_dedicated_audition_verification = should_not_render

        with self.assertRaisesRegex(
            ValueError,
            "期望 103，实际 1；拒绝开始渲染",
        ):
            self.tool.generate_all_auditions(only=("不存在",))

        self.assertFalse(render_called)
        self.assertEqual(
            list((self.root / "output").glob(".生成全音域试音-*")),
            [],
        )

    def test_ascending_commit_failure_restores_output_events_and_reports(
        self,
    ) -> None:
        manifest, _ = self._make_instrument(
            "键盘乐器/事务键盘",
            ["old"],
            manifest_updates={
                "type": "oscillator",
                "note_min": 60,
                "note_max": 61,
            },
        )
        report_path = manifest.parent / "试听核验.json"
        old_report = report_path.read_bytes()
        output_marker = self.output_root / "旧试听保留.txt"
        output_marker.write_text("old output", encoding="utf-8")
        self.tool.FULL_RANGE_EVENTS_ROOT.mkdir(parents=True)
        events_marker = self.tool.FULL_RANGE_EVENTS_ROOT / "旧事件保留.txt"
        events_marker.write_text("old events", encoding="utf-8")
        self.tool.discover_instruments = lambda _root: [
            SimpleNamespace(manifest_path=manifest)
        ]
        self.tool.generate_dedicated_audition_verification = self._fake_report

        real_replace = self.tool.os.replace

        def fail_report(source, destination):
            if Path(destination) == report_path:
                raise OSError("fail ascending report commit")
            return real_replace(source, destination)

        with mock.patch.object(self.tool.os, "replace", fail_report):
            with self.assertRaisesRegex(
                OSError,
                "fail ascending report commit",
            ):
                self.tool.generate_all_auditions()

        self.assertEqual(output_marker.read_text(encoding="utf-8"), "old output")
        self.assertEqual(events_marker.read_text(encoding="utf-8"), "old events")
        self.assertEqual(report_path.read_bytes(), old_report)


if __name__ == "__main__":
    unittest.main()
