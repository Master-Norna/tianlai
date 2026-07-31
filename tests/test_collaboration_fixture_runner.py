"""协奏 fixture 本机生成器的轻量测试；不会加载或渲染真实音源。"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest import mock

from tianlai.collaboration_fixtures import (
    build_fixture_documents,
    fixture_ids,
)
from tianlai.space import SpaceConfig


ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = ROOT / "tools" / "生成协奏校准.py"
_SAME_AS_DISK = object()
_OMIT_RESULT_VALUE = object()


def _load_tool():
    name = "tianlai_test_collaboration_fixture_runner"
    spec = importlib.util.spec_from_file_location(name, TOOL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {TOOL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CollaborationFixtureRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = _load_tool()
        self.temporary = tempfile.TemporaryDirectory()
        self.output_root = Path(self.temporary.name) / "协奏校准"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _mock_plan(
        self,
        *,
        duration_seconds: float = 2.5,
        warnings: tuple[str, ...] = (),
    ) -> SimpleNamespace:
        return SimpleNamespace(
            duration_seconds=duration_seconds,
            warnings=warnings,
            to_dict=lambda: {
                "title": "mock plan",
                "duration_seconds": duration_seconds,
            },
        )

    def _write_fake_render(
        self,
        plan,
        directory,
        *,
        report: dict | None = None,
        memory_report=_SAME_AS_DISK,
        sample_rate: int = 8_000,
        frame_count: int = 22_000,
        result_duration=_SAME_AS_DISK,
    ) -> SimpleNamespace:
        disk_report = (
            {"warnings": []}
            if report is None
            else report
        )
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        plan_path = output / self.tool.PERFORMANCE_PLAN_NAME
        plan_path.write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        receipt_path = output / "渲染回执.json"
        receipt_path.write_text(
            json.dumps(
                {
                    "audio_format": {"sample_rate": sample_rate},
                    "mix": {"frame_count": frame_count},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        report_path = output / "协奏诊断.json"
        report_path.write_text(
            json.dumps(disk_report, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        mix_path = output / "合奏.wav"
        mix_path.write_bytes(b"mock-pcm24-wav")
        values = {
            "sample_rate": sample_rate,
            "frame_count": frame_count,
            "receipt_path": str(receipt_path),
            "mix_report_path": str(report_path),
            "mix_path": str(mix_path),
        }
        if result_duration is not _OMIT_RESULT_VALUE:
            values["duration_seconds"] = (
                frame_count / sample_rate
                if result_duration is _SAME_AS_DISK
                else result_duration
            )
        if memory_report is not _OMIT_RESULT_VALUE:
            values["mix_report"] = (
                disk_report
                if memory_report is _SAME_AS_DISK
                else memory_report
            )
        return SimpleNamespace(**values)

    def test_default_atomically_writes_all_input_triplets_only(self) -> None:
        with (
            mock.patch.object(
                self.tool,
                "load_capabilities",
                side_effect=AssertionError("默认模式不得加载音源目录"),
            ),
            mock.patch.object(
                self.tool,
                "build_plan",
                side_effect=AssertionError("默认模式不得构建演奏计划"),
            ),
            mock.patch.object(
                self.tool,
                "render_plan",
                side_effect=AssertionError("默认模式不得渲染"),
            ),
        ):
            manifest = self.tool.generate_calibration(
                output_root=self.output_root,
            )

        identifiers = fixture_ids()
        self.assertEqual(len(identifiers), 12)
        self.assertEqual(manifest["fixture_count"], 12)
        self.assertEqual(manifest["mode"], "inputs")
        self.assertFalse(manifest["acceptance_matrix_written"])
        self.assertFalse(manifest["instrument_manifests_modified"])
        self.assertFalse(
            (self.output_root / self.tool.RENDER_DIRECTORY_NAME).exists()
        )

        for identifier, entry in zip(
            identifiers,
            manifest["fixtures"],
            strict=True,
        ):
            self.assertEqual(entry["fixture_id"], identifier)
            source = build_fixture_documents(identifier)
            directory = (
                self.output_root
                / self.tool.INPUT_DIRECTORY_NAME
                / identifier
            )
            self.assertEqual(
                json.loads(
                    (directory / "score.json").read_text(encoding="utf-8")
                ),
                source["score"],
            )
            self.assertEqual(
                json.loads(
                    (directory / "roster.json").read_text(encoding="utf-8")
                ),
                source["roster"],
            )
            metadata = json.loads(
                (directory / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("score", metadata)
            self.assertNotIn("roster", metadata)
            self.assertEqual(metadata["seed"], source["seed"])
            self.assertEqual(metadata["space"], source["space"])
            self.assertEqual(
                metadata["master_gain_db"],
                source["master_gain_db"],
            )
            self.assertIsNone(metadata["normalize_peak_db"])
            self.assertEqual(entry["targets"], source["targets"])
            self.assertEqual(
                entry["human_questions"],
                source["human_questions"],
            )
            self.assertEqual(
                entry["render_settings"],
                {
                    "seed": source["seed"],
                    "space": source["space"],
                    "master_gain_db": float(source["master_gain_db"]),
                    "normalize_peak_db": None,
                },
            )
            for name, filename in self.tool.INPUT_FILENAMES.items():
                path = directory / filename
                self.assertEqual(
                    entry["inputs"][name]["sha256"],
                    _sha256(path),
                )

        variants = [entry["variant"] for entry in manifest["fixtures"]]
        self.assertEqual(variants, ["typical", "stress"] * 6)
        for offset in range(0, len(manifest["fixtures"]), 2):
            typical, stress = manifest["fixtures"][offset : offset + 2]
            self.assertEqual(typical["family"], stress["family"])

        persisted = json.loads(
            (self.output_root / self.tool.MANIFEST_NAME).read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(persisted, manifest)
        order_text = (
            self.output_root / self.tool.LISTENING_ORDER_NAME
        ).read_text(encoding="utf-8")
        self.assertEqual(
            manifest["listening_order"],
            {
                "path": self.tool.LISTENING_ORDER_NAME,
                "sha256": _sha256(
                    self.output_root / self.tool.LISTENING_ORDER_NAME
                ),
            },
        )
        positions = [order_text.index(identifier) for identifier in identifiers]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(
            list(self.output_root.rglob("*.tmp")),
            [],
        )

    def test_list_has_no_file_system_side_effect(self) -> None:
        capture = io.StringIO()
        with redirect_stdout(capture):
            status = self.tool.main(
                [
                    "--list",
                    "--output-root",
                    str(self.output_root),
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            capture.getvalue().splitlines(),
            list(fixture_ids()),
        )
        self.assertFalse(self.output_root.exists())

    def test_list_invalid_only_is_a_clean_cli_error(self) -> None:
        capture = io.StringIO()
        with (
            redirect_stderr(capture),
            self.assertRaises(SystemExit) as raised,
        ):
            self.tool.main(["--list", "--only", "not-a-fixture"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("未知 --only", capture.getvalue())
        self.assertNotIn("Traceback", capture.getvalue())
        self.assertFalse(self.output_root.exists())

    def test_only_selects_one_exact_fixture(self) -> None:
        identifier = fixture_ids()[3]
        manifest = self.tool.generate_calibration(
            output_root=self.output_root,
            only=identifier,
        )
        self.assertEqual(manifest["fixture_count"], 1)
        self.assertEqual(manifest["fixtures"][0]["fixture_id"], identifier)
        directories = [
            path.name
            for path in (
                self.output_root / self.tool.INPUT_DIRECTORY_NAME
            ).iterdir()
        ]
        self.assertEqual(directories, [identifier])
        with self.assertRaisesRegex(ValueError, "未知 --only"):
            self.tool.generate_calibration(
                output_root=self.output_root,
                only="not-a-fixture",
            )

    def test_atomic_file_replace_failure_preserves_previous_bytes(self) -> None:
        target = self.output_root / "atomic.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"previous")
        with mock.patch.object(
            self.tool.os,
            "replace",
            side_effect=OSError("mock replace failure"),
        ):
            with self.assertRaisesRegex(OSError, "mock replace failure"):
                self.tool._write_bytes_atomic(target, b"replacement")
        self.assertEqual(target.read_bytes(), b"previous")
        self.assertEqual(
            list(target.parent.glob(f".{target.name}.*.tmp")),
            [],
        )

    def test_input_update_invalidates_old_metadata_commit_marker(
        self,
    ) -> None:
        identifier = fixture_ids()[0]
        source = build_fixture_documents(identifier)
        _score, _roster, metadata = self.tool._split_fixture_document(
            identifier,
            source,
        )
        directory = (
            self.output_root
            / self.tool.INPUT_DIRECTORY_NAME
            / identifier
        )
        directory.mkdir(parents=True)
        (directory / "score.json").write_text("old score", encoding="utf-8")
        (directory / "roster.json").write_text(
            "old roster",
            encoding="utf-8",
        )
        marker = directory / "metadata.json"
        marker.write_text("old metadata", encoding="utf-8")
        real_write = self.tool._write_json_atomic

        def fail_on_roster(path, document):
            if Path(path).name == "roster.json":
                raise OSError("mock roster failure")
            return real_write(path, document)

        with mock.patch.object(
            self.tool,
            "_write_json_atomic",
            side_effect=fail_on_roster,
        ):
            with self.assertRaisesRegex(OSError, "mock roster failure"):
                self.tool._write_inputs(
                    self.output_root,
                    identifier,
                    source["score"],
                    source["roster"],
                    metadata,
                )
        self.assertFalse(marker.exists())

    def test_plan_only_uses_fixed_seed_without_calling_renderer(self) -> None:
        identifier = fixture_ids()[0]
        source = build_fixture_documents(identifier)
        plan = SimpleNamespace(
            duration_seconds=3.25,
            warnings=("计划提醒",),
            to_dict=lambda: {
                "title": "mock plan",
                "duration_seconds": 3.25,
            },
        )
        observed: dict[str, object] = {}

        def fake_build(score, roster, settings):
            observed["score"] = score
            observed["roster"] = roster
            observed["settings"] = settings
            return plan

        with (
            mock.patch.object(
                self.tool,
                "load_capabilities",
                return_value={"mock": object()},
            ) as load_capabilities,
            mock.patch.object(
                self.tool,
                "parse_score_document",
                return_value="parsed score",
            ),
            mock.patch.object(
                self.tool,
                "parse_roster_document",
                return_value="parsed roster",
            ),
            mock.patch.object(
                self.tool,
                "build_plan",
                side_effect=fake_build,
            ),
            mock.patch.object(
                self.tool,
                "render_plan",
                side_effect=AssertionError("plan-only 不得渲染"),
            ),
        ):
            manifest = self.tool.generate_calibration(
                output_root=self.output_root,
                only=identifier,
                mode="plan-only",
            )

        load_capabilities.assert_called_once_with(ROOT / "乐器")
        settings = observed["settings"]
        self.assertEqual(settings.seed, source["seed"])
        self.assertEqual(settings.mode, "ensemble")
        entry = manifest["fixtures"][0]
        self.assertEqual(entry["duration_seconds"], 3.25)
        self.assertEqual(
            entry["machine_warnings"]["build_plan"],
            ["计划提醒"],
        )
        self.assertIsNone(entry["render"])
        plan_path = self.output_root / entry["plan"]["path"]
        self.assertTrue(plan_path.is_file())
        self.assertEqual(entry["plan"]["sha256"], _sha256(plan_path))

    def test_render_records_hashes_duration_warnings_and_fixed_settings(
        self,
    ) -> None:
        identifier = fixture_ids()[1]
        source = build_fixture_documents(identifier)
        plan = SimpleNamespace(
            duration_seconds=2.5,
            warnings=("计划提醒",),
            to_dict=lambda: {
                "title": "mock render plan",
                "duration_seconds": 2.5,
            },
        )
        report = {
            "warnings": [
                {
                    "code": "mock-balance-warning",
                    "message": "只供机器分流",
                }
            ]
        }
        observed: dict[str, object] = {}

        def fake_render(received_plan, directory, **kwargs):
            observed["plan"] = received_plan
            observed["directory"] = Path(directory)
            observed["kwargs"] = kwargs
            return self._write_fake_render(
                received_plan,
                directory,
                report=report,
                memory_report=_OMIT_RESULT_VALUE,
                result_duration=_OMIT_RESULT_VALUE,
            )

        with (
            mock.patch.object(
                self.tool,
                "load_capabilities",
                return_value={"mock": object()},
            ),
            mock.patch.object(
                self.tool,
                "parse_score_document",
                return_value="parsed score",
            ),
            mock.patch.object(
                self.tool,
                "parse_roster_document",
                return_value="parsed roster",
            ),
            mock.patch.object(
                self.tool,
                "build_plan",
                return_value=plan,
            ),
            mock.patch.object(
                self.tool,
                "render_plan",
                side_effect=fake_render,
            ),
        ):
            manifest = self.tool.generate_calibration(
                output_root=self.output_root,
                only=identifier,
                mode="render",
            )

        self.assertIs(observed["plan"], plan)
        kwargs = observed["kwargs"]
        self.assertFalse(kwargs["write_stems"])
        self.assertEqual(
            kwargs["master_gain_db"],
            float(source["master_gain_db"]),
        )
        self.assertIsNone(kwargs["normalize_peak_db"])
        self.assertEqual(
            kwargs["space"],
            SpaceConfig.from_dict(source["space"]),
        )
        self.assertIsNone(kwargs["collaboration_mode"])

        entry = manifest["fixtures"][0]
        self.assertEqual(entry["duration_seconds"], 2.75)
        self.assertEqual(
            entry["machine_warnings"]["mix_report"],
            report["warnings"],
        )
        self.assertEqual(entry["targets"], source["targets"])
        self.assertEqual(
            entry["human_questions"],
            source["human_questions"],
        )
        for name in ("receipt", "mix_report", "wav"):
            record = entry["render"][name]
            self.assertEqual(
                record["sha256"],
                _sha256(self.output_root / record["path"]),
            )
        listening_order = (
            self.output_root / self.tool.LISTENING_ORDER_NAME
        ).read_text(encoding="utf-8")
        self.assertIn(entry["render"]["wav"]["path"], listening_order)
        self.assertIn("2.750 s", listening_order)
        self.assertIn("mock-balance-warning", listening_order)
        self.assertNotIn('"machine_warnings"', listening_order)
        self.assertIn("机器提示只负责分流排查", listening_order)

    def test_disk_report_is_authoritative_and_memory_mismatch_is_rejected(
        self,
    ) -> None:
        identifier = fixture_ids()[0]
        plan = self._mock_plan()
        disk_report = {
            "warnings": [{"code": "disk-warning"}],
        }
        memory_report = {
            "warnings": [{"code": "memory-warning"}],
        }

        def fake_render(received_plan, directory, **_kwargs):
            return self._write_fake_render(
                received_plan,
                directory,
                report=disk_report,
                memory_report=memory_report,
            )

        with (
            mock.patch.object(
                self.tool,
                "load_capabilities",
                return_value={},
            ),
            mock.patch.object(
                self.tool,
                "parse_score_document",
                return_value="parsed score",
            ),
            mock.patch.object(
                self.tool,
                "parse_roster_document",
                return_value="parsed roster",
            ),
            mock.patch.object(
                self.tool,
                "build_plan",
                return_value=plan,
            ),
            mock.patch.object(
                self.tool,
                "render_plan",
                side_effect=fake_render,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "内存 mix_report 与磁盘协奏诊断不一致",
            ):
                self.tool.generate_calibration(
                    output_root=self.output_root,
                    only=identifier,
                    mode="render",
                )

        self.assertFalse(
            (self.output_root / self.tool.MANIFEST_NAME).exists()
        )

    def test_duration_mismatch_against_hashed_receipt_is_rejected(
        self,
    ) -> None:
        identifier = fixture_ids()[0]
        plan = self._mock_plan()

        def fake_render(received_plan, directory, **_kwargs):
            return self._write_fake_render(
                received_plan,
                directory,
                result_duration=9.0,
            )

        with (
            mock.patch.object(
                self.tool,
                "load_capabilities",
                return_value={},
            ),
            mock.patch.object(
                self.tool,
                "parse_score_document",
                return_value="parsed score",
            ),
            mock.patch.object(
                self.tool,
                "parse_roster_document",
                return_value="parsed roster",
            ),
            mock.patch.object(
                self.tool,
                "build_plan",
                return_value=plan,
            ),
            mock.patch.object(
                self.tool,
                "render_plan",
                side_effect=fake_render,
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                "duration_seconds 与磁盘回执推导时长不一致",
            ):
                self.tool.generate_calibration(
                    output_root=self.output_root,
                    only=identifier,
                    mode="render",
                )

        self.assertFalse(
            (self.output_root / self.tool.MANIFEST_NAME).exists()
        )

    def test_order_tamper_withdraws_final_marker_and_order(self) -> None:
        identifier = fixture_ids()[0]
        real_write = self.tool._write_json_atomic

        def tamper_during_manifest_write(path, document):
            digest = real_write(path, document)
            if Path(path).name == self.tool.MANIFEST_NAME:
                (
                    self.output_root / self.tool.LISTENING_ORDER_NAME
                ).write_text("tampered\n", encoding="utf-8")
            return digest

        with mock.patch.object(
            self.tool,
            "_write_json_atomic",
            side_effect=tamper_during_manifest_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "发生变化"):
                self.tool.generate_calibration(
                    output_root=self.output_root,
                    only=identifier,
                )

        self.assertFalse(
            (self.output_root / self.tool.MANIFEST_NAME).exists()
        )
        self.assertFalse(
            (self.output_root / self.tool.LISTENING_ORDER_NAME).exists()
        )

    def test_final_manifest_failure_withdraws_new_listening_order(self) -> None:
        identifier = fixture_ids()[0]
        real_write = self.tool._write_json_atomic

        def fail_final_marker(path, document):
            if Path(path).name == self.tool.MANIFEST_NAME:
                real_write(path, document)
                raise OSError("mock final marker failure")
            return real_write(path, document)

        with mock.patch.object(
            self.tool,
            "_write_json_atomic",
            side_effect=fail_final_marker,
        ):
            with self.assertRaisesRegex(OSError, "final marker failure"):
                self.tool.generate_calibration(
                    output_root=self.output_root,
                    only=identifier,
                )

        self.assertFalse(
            (self.output_root / self.tool.MANIFEST_NAME).exists()
        )
        self.assertFalse(
            (self.output_root / self.tool.LISTENING_ORDER_NAME).exists()
        )

    def test_reruns_converge_owned_tree_across_mode_downgrades(self) -> None:
        first, second, third = fixture_ids()[:3]
        self.output_root.mkdir(parents=True)
        user_file = self.output_root / "用户保留.txt"
        user_file.write_text("keep", encoding="utf-8")
        user_directory_file = self.output_root / "用户目录" / "keep.bin"
        user_directory_file.parent.mkdir()
        user_directory_file.write_bytes(b"keep")
        plan = self._mock_plan()

        def fake_render(received_plan, directory, **_kwargs):
            return self._write_fake_render(received_plan, directory)

        with (
            mock.patch.object(
                self.tool,
                "load_capabilities",
                return_value={},
            ),
            mock.patch.object(
                self.tool,
                "parse_score_document",
                return_value="parsed score",
            ),
            mock.patch.object(
                self.tool,
                "parse_roster_document",
                return_value="parsed roster",
            ),
            mock.patch.object(
                self.tool,
                "build_plan",
                return_value=plan,
            ),
            mock.patch.object(
                self.tool,
                "render_plan",
                side_effect=fake_render,
            ),
        ):
            rendered = self.tool.generate_calibration(
                output_root=self.output_root,
                only=first,
                mode="render",
            )
            self.assertEqual(
                {
                    path.name
                    for path in (
                        self.output_root / self.tool.INPUT_DIRECTORY_NAME
                    ).iterdir()
                },
                {first},
            )
            self.assertEqual(
                {
                    path.name
                    for path in (
                        self.output_root / self.tool.RENDER_DIRECTORY_NAME
                    ).iterdir()
                },
                {first},
            )
            self.assertEqual(
                rendered["fixtures"][0]["fixture_id"],
                first,
            )

            planned = self.tool.generate_calibration(
                output_root=self.output_root,
                only=second,
                mode="plan-only",
            )
            self.assertEqual(
                {
                    path.name
                    for path in (
                        self.output_root / self.tool.INPUT_DIRECTORY_NAME
                    ).iterdir()
                },
                {second},
            )
            render_children = list(
                (
                    self.output_root
                    / self.tool.RENDER_DIRECTORY_NAME
                ).iterdir()
            )
            self.assertEqual([path.name for path in render_children], [second])
            self.assertEqual(
                {path.name for path in render_children[0].iterdir()},
                {self.tool.PERFORMANCE_PLAN_NAME},
            )
            self.assertEqual(
                planned["fixtures"][0]["fixture_id"],
                second,
            )

            inputs = self.tool.generate_calibration(
                output_root=self.output_root,
                only=third,
                mode="inputs",
            )

        self.assertEqual(
            {
                path.name
                for path in (
                    self.output_root / self.tool.INPUT_DIRECTORY_NAME
                ).iterdir()
            },
            {third},
        )
        self.assertFalse(
            (self.output_root / self.tool.RENDER_DIRECTORY_NAME).exists()
        )
        self.assertEqual(inputs["fixtures"][0]["fixture_id"], third)
        self.assertEqual(user_file.read_text(encoding="utf-8"), "keep")
        self.assertEqual(user_directory_file.read_bytes(), b"keep")

    def test_cleanup_rejects_non_owned_or_non_directory_targets(self) -> None:
        self.output_root.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "非生成器目录"):
            self.tool._validated_owned_directory(
                self.output_root,
                "../outside",
            )

        input_path = self.output_root / self.tool.INPUT_DIRECTORY_NAME
        input_path.write_bytes(b"user-owned collision")
        marker = self.output_root / self.tool.MANIFEST_NAME
        marker.write_text('{"stale":true}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不是目录"):
            self.tool.generate_calibration(
                output_root=self.output_root,
                only=fixture_ids()[0],
            )
        self.assertFalse(marker.exists())
        self.assertEqual(input_path.read_bytes(), b"user-owned collision")

    def test_render_failure_does_not_publish_root_manifest(self) -> None:
        identifier = fixture_ids()[0]
        self.output_root.mkdir(parents=True)
        (self.output_root / self.tool.MANIFEST_NAME).write_text(
            '{"stale":true}\n',
            encoding="utf-8",
        )
        (self.output_root / self.tool.LISTENING_ORDER_NAME).write_text(
            "旧顺序\n",
            encoding="utf-8",
        )
        plan = SimpleNamespace(
            duration_seconds=1.0,
            warnings=(),
            to_dict=lambda: {"duration_seconds": 1.0},
        )
        with (
            mock.patch.object(
                self.tool,
                "load_capabilities",
                return_value={},
            ),
            mock.patch.object(
                self.tool,
                "parse_score_document",
                return_value="parsed score",
            ),
            mock.patch.object(
                self.tool,
                "parse_roster_document",
                return_value="parsed roster",
            ),
            mock.patch.object(
                self.tool,
                "build_plan",
                return_value=plan,
            ),
            mock.patch.object(
                self.tool,
                "render_plan",
                side_effect=RuntimeError("mock render failure"),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "mock render failure"):
                self.tool.generate_calibration(
                    output_root=self.output_root,
                    only=identifier,
                    mode="render",
                )

        self.assertFalse(
            (self.output_root / self.tool.MANIFEST_NAME).exists()
        )
        self.assertFalse(
            (self.output_root / self.tool.LISTENING_ORDER_NAME).exists()
        )
        self.assertFalse(
            any(
                path.name.endswith("matrix.json")
                for path in self.output_root.rglob("*")
            )
        )


if __name__ == "__main__":
    unittest.main()
