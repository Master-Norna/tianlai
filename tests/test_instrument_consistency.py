"""全部正式声音入口的结构一致性守护（当前 103 件）。

98 项清单入口、4 件既有专用乐器、以及后续合奏听感驱动新增的乐器**同级**:
都必须是单音色 formal、协奏未测试，都必须带同一套审计工件与复算脚本、都不得靠通用
SoundFont 出声。参考振荡器是测试工具,明确排除在声音入口之外,并断言它
确实没有被算进来。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.catalog import discover_instruments
from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
    canonical_json_sha256,
)
from tianlai.quality import load_upgrade_progress


INSTRUMENT_ROOT = ROOT / "乐器"
TEST_TOOL = "测试工具/参考振荡器"
EXAMPLES = ROOT / "examples"
FULL_RANGE_EVENTS = EXAMPLES / "全音域上行"

# 计数边界:98 项清单之外的既有专用乐器,与清单入口同级。
# 原始 4 件(钢琴/小提琴/大提琴/长笛)之外,后续合奏听感驱动新增的乐器也在此列。
LEGACY_FOUR = {
    "键盘乐器/钢琴",
    "管弦乐/弦乐组/小提琴",
    "管弦乐/弦乐组/大提琴",
    "管弦乐/木管组/长笛",
}
POST_LEDGER = {
    "世界乐器/编钟",
}
# 曾经清单外还有两件小提琴(第一小提琴组 / 独奏小提琴_爱荷华),已于 2026-07-25
# 合并回 管弦乐/弦乐组/小提琴:同一件乐器用 sample_variant 选 SOLO(独奏)或
# SEC(声部齐奏),避免"一把小提琴摆成三个入口"。爱荷华那支(仅 sustain 一种
# 奏法、且消声室干声本底暴露)随之退役。
BEYOND_LEDGER = LEGACY_FOUR | POST_LEDGER
SOUND_ENTRY_COUNT = 98 + len(BEYOND_LEDGER)

REQUIRED_ARTIFACTS = (
    "资源核验.json",
    "音准校准.json",
    "试听核验.json",
    "README.md",
    "来源.md",
    "核验资源.py",
    "校准音准.py",
    "核验试听.py",
)


def _sound_entries() -> list[tuple[str, Path]]:
    entries = []
    for entry in discover_instruments(INSTRUMENT_ROOT):
        directory = Path(entry.manifest_path).parent
        relative = directory.relative_to(INSTRUMENT_ROOT).as_posix()
        if relative == TEST_TOOL:
            continue
        entries.append((relative, directory))
    return entries


def _audition_event_candidates(directory: Path) -> list[Path]:
    """Return all hash-addressable audition scores, excluding pitch probes."""

    return [
        path
        for path in sorted(EXAMPLES.rglob(f"{directory.name}_*.events.json"))
        if "_音准" not in path.stem
    ]


class InstrumentConsistencyTests(unittest.TestCase):
    def test_exactly_103_sound_entries_plus_one_test_tool(self) -> None:
        all_entries = {
            Path(entry.manifest_path).parent.relative_to(INSTRUMENT_ROOT).as_posix()
            for entry in discover_instruments(INSTRUMENT_ROOT)
        }
        self.assertIn(TEST_TOOL, all_entries, "参考振荡器应当存在")
        sound = all_entries - {TEST_TOOL}
        self.assertEqual(
            len(sound), SOUND_ENTRY_COUNT,
            f"正式声音入口应为 {SOUND_ENTRY_COUNT} 件,实为 {len(sound)}",
        )

    def test_ledger_98_plus_legacy_4_and_post_ledger_1_partition_the_103(
        self,
    ) -> None:
        ledger = {
            entry.relative_path
            for entry in load_upgrade_progress(INSTRUMENT_ROOT).entries
        }
        self.assertEqual(len(ledger), 98)
        sound = {relative for relative, _ in _sound_entries()}
        self.assertTrue(ledger < sound, "清单 98 项必须都在声音入口内")
        self.assertEqual(
            sound - ledger, BEYOND_LEDGER,
            "98 项之外应当恰好是清单外的既有专用乐器与新增声部乐器",
        )

    def test_all_103_are_standalone_formal_collaboration_untested(self) -> None:
        for relative, directory in _sound_entries():
            manifest = json.loads(
                (directory / "乐器.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest.get("quality_tier"), "formal",
                f"{relative} 未声明单音色 formal",
            )
            self.assertEqual(
                manifest.get("collaboration_review_status"),
                "untested",
                f"{relative} 不应冒充已经过协奏验收",
            )
            self.assertNotIn(
                "manual_review",
                manifest,
                f"{relative} 仍在使用语义混杂的旧 manual_review",
            )
            self.assertNotEqual(
                manifest.get("type"), "soundfont",
                f"{relative} 仍是通用 SoundFont",
            )
            self.assertNotIn("soundfont", manifest, f"{relative} 残留 soundfont 字段")
            self.assertEqual(
                manifest.get("fallback_policy"), "explicit_only_no_silent_gm",
                f"{relative} 未声明显式回退策略",
            )

    def test_all_103_ship_the_same_audit_artifacts(self) -> None:
        for relative, directory in _sound_entries():
            for artifact in REQUIRED_ARTIFACTS:
                self.assertTrue(
                    (directory / artifact).is_file(),
                    f"{relative} 缺少 {artifact}",
                )

    def test_every_audition_report_is_clip_free_and_hash_locked(self) -> None:
        for relative, directory in _sound_entries():
            report = json.loads(
                (directory / "试听核验.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["clipped_samples"], 0, f"{relative} 试听有削波")
            self.assertGreater(report["peak"], 0.0, f"{relative} 试听是静音")
            self.assertLess(report["peak"], 1.0, f"{relative} 试听峰值越界")
            self.assertEqual(report["sample_rate"], 48_000, relative)
            self.assertEqual(report["channels"], 2, relative)
            self.assertEqual(report["subtype"], "PCM_24", relative)
            self.assertEqual(
                report.get("wav_persistence"),
                "temporary",
                f"{relative} 未声明试听 WAV 可删除",
            )
            self.assertEqual(len(report["wav_sha256"]), 64, relative)
            self.assertIn(report["human_review"], {"pending", "passed"}, relative)

    def test_full_range_event_recipes_are_complete_and_hash_locked(self) -> None:
        """试听 WAV 是可删除产物；只冻结可复现谱例与报告中的证据。"""

        event_paths = list(FULL_RANGE_EVENTS.rglob("*.events.json"))
        self.assertEqual(len(event_paths), SOUND_ENTRY_COUNT)
        by_relative = {
            path.relative_to(FULL_RANGE_EVENTS).parent.as_posix()
            + "/"
            + path.name.removesuffix("_全音域上行.events.json"): path
            for path in event_paths
        }
        self.assertEqual(set(by_relative), {
            relative for relative, _directory in _sound_entries()
        })

        for relative, _directory in _sound_entries():
            events_path = by_relative[relative]
            self.assertTrue(events_path.is_file(), relative)

            events = json.loads(events_path.read_text(encoding="utf-8"))
            self.assertEqual(
                events["tuning"],
                {"temperament": "equal", "a4_hz": 440.0},
                relative,
            )
            note_ons = [
                event["midi_note"]
                for event in events["events"]
                if event["type"] == "note_on"
            ]
            self.assertTrue(note_ons, relative)

    def test_audition_manifest_hashes_match_current_manifests(self) -> None:
        mismatches = []
        for relative, directory in _sound_entries():
            report = json.loads(
                (directory / "试听核验.json").read_text(encoding="utf-8")
            )
            actual = canonical_json_file_sha256(directory / "乐器.json")
            if (
                report.get("hash_algorithm") != HASH_ALGORITHM
                or report.get("canonicalization") != CANONICALIZATION
                or report.get("manifest_canonical_sha256") != actual
            ):
                mismatches.append(relative)
        self.assertEqual(
            mismatches,
            [],
            "试听报告未由当前乐器清单渲染：" + "、".join(mismatches),
        )

    def test_resource_report_manifest_bindings_use_canonical_json(self) -> None:
        """结构化资源证据不得重新绑定到平台相关的 JSON 原始字节。"""

        legacy = []
        mismatches = []
        canonical_reports = []
        for relative, directory in _sound_entries():
            report = json.loads(
                (directory / "资源核验.json").read_text(encoding="utf-8")
            )
            if "manifest_sha256" in report:
                legacy.append(relative)
            if "manifest_canonical_sha256" not in report:
                continue
            canonical_reports.append(relative)
            actual = canonical_json_file_sha256(directory / "乐器.json")
            if (
                report.get("schema_version") != 2
                or report.get("hash_algorithm") != HASH_ALGORITHM
                or report.get("canonicalization") != CANONICALIZATION
                or report.get("manifest_canonical_sha256") != actual
            ):
                mismatches.append(relative)

        self.assertEqual(
            legacy,
            [],
            "资源报告仍使用平台相关 manifest_sha256：" + "、".join(legacy),
        )
        self.assertEqual(
            set(canonical_reports),
            {"键盘乐器/手风琴", "管弦乐/弦乐组/中提琴"},
        )
        self.assertEqual(
            mismatches,
            [],
            "资源报告的规范化清单身份已过期：" + "、".join(mismatches),
        )

    def test_license_metadata_only_migrations_are_reconstructable(self) -> None:
        """纯元数据迁移可复原；重渲染后的旧记录必须留在历史证据中。"""

        current_migrations = []
        archived_migrations = []

        def validate_record(record: dict, relative: str) -> None:
            self.assertEqual(
                record.get("status"),
                "license_metadata_only_no_audio_change",
                relative,
            )
            self.assertRegex(
                str(record.get("migrated_at", "")),
                r"^\d{4}-\d{2}-\d{2}$",
                relative,
            )
            self.assertEqual(
                record.get("changed_fields"),
                ["creator", "attribution"],
                relative,
            )
            self.assertIs(record.get("audio_rerendered"), False, relative)
            self.assertEqual(
                record.get("reason"),
                "Only creator and attribution metadata changed; runtime and "
                "render parameters are unchanged.",
                relative,
            )
            recorded_previous_hash = record.get(
                "previous_manifest_canonical_sha256"
            )
            self.assertIsInstance(recorded_previous_hash, str, relative)
            self.assertRegex(recorded_previous_hash, r"^[0-9a-f]{64}$", relative)

        def archived_records(value: object) -> list[dict]:
            records = []
            if isinstance(value, dict):
                record = value.get("license_metadata_migration")
                if isinstance(record, dict):
                    records.append(record)
                for nested in value.values():
                    records.extend(archived_records(nested))
            elif isinstance(value, list):
                for nested in value:
                    records.extend(archived_records(nested))
            return records

        for relative, directory in _sound_entries():
            manifest = json.loads(
                (directory / "乐器.json").read_text(encoding="utf-8")
            )
            report = json.loads(
                (directory / "试听核验.json").read_text(encoding="utf-8")
            )
            record = report.get("license_metadata_migration")

            if record is not None:
                self.assertIsInstance(record, dict, relative)
                current_migrations.append(relative)
                validate_record(record, relative)
                previous = dict(manifest)
                # The factory-dispatch migration happened after the older
                # license-only migration.  Rewind that later identity step
                # first so the license record is reconstructed against the
                # manifest version it actually followed.
                factory_migration = report.get("factory_dispatch_migration")
                if isinstance(factory_migration, dict):
                    self.assertEqual(
                        factory_migration.get("changed_fields"),
                        ["implementation"],
                        relative,
                    )
                    previous["implementation"] = "乐器.py"
                previous.pop("creator")
                previous.pop("attribution")
                expected_previous_hash = canonical_json_sha256(previous)
                self.assertEqual(
                    record["previous_manifest_canonical_sha256"],
                    expected_previous_hash,
                    f"{relative} 的旧清单身份不能由当前清单移除许可字段后复原",
                )

            history = archived_records(report.get("previous_protocol_evidence"))
            self.assertLessEqual(len(history), 1, relative)
            if record is not None or history:
                self.assertTrue(
                    str(manifest.get("creator", "")).strip(), relative
                )
                self.assertTrue(
                    str(manifest.get("attribution", "")).strip(), relative
                )
            if history:
                self.assertIsNone(record, f"{relative} 不得同时声明当前与历史迁移")
                archived_migrations.append(relative)
                validate_record(history[0], relative)

        self.assertEqual(
            len(current_migrations),
            26,
            "仍未重渲染的纯许可元数据迁移应恰有 26 件",
        )
        self.assertEqual(
            len(archived_migrations),
            14,
            "本轮音频重渲染应恰好归档 14 件旧许可元数据迁移",
        )
        self.assertEqual(
            len(current_migrations) + len(archived_migrations),
            40,
            "原 40 件许可署名迁移记录必须全部保留且不重复",
        )

    def test_audition_event_hashes_match_current_examples(self) -> None:
        missing_examples = []
        mismatches = []
        for relative, directory in _sound_entries():
            candidates = _audition_event_candidates(directory)
            if not candidates:
                missing_examples.append(relative)
                continue
            report = json.loads(
                (directory / "试听核验.json").read_text(encoding="utf-8")
            )
            current_hashes = {
                canonical_json_file_sha256(path) for path in candidates
            }
            if (
                report.get("hash_algorithm") != HASH_ALGORITHM
                or report.get("canonicalization") != CANONICALIZATION
                or report.get("events_canonical_sha256") not in current_hashes
            ):
                mismatches.append(relative)
        self.assertEqual(
            missing_examples,
            [],
            "找不到当前固定试听谱例：" + "、".join(missing_examples),
        )
        self.assertEqual(
            mismatches,
            [],
            "试听报告未由当前固定谱例渲染：" + "、".join(mismatches),
        )

    def test_pitch_calibration_is_either_measured_or_explicitly_not_applicable(
        self,
    ) -> None:
        for relative, directory in _sound_entries():
            document = json.loads(
                (directory / "音准校准.json").read_text(encoding="utf-8")
            )
            # 两种合法形态:声明不适用并给出理由,或给出实测 summary。
            # 早期批次没有 applicable 字段,但只要有 summary 就同样是证据。
            if document.get("applicable") is False:
                self.assertTrue(
                    str(document.get("reason", "")).strip(),
                    f"{relative} 声明音准不适用却没写理由",
                )
                continue
            # 各批次的实测数据段命名不同(汇总/逐采样/逐鼓/逐探测音),
            # 只要求其中至少有一段非空,不强求统一字段名。
            evidence = [
                document.get(key)
                for key in ("summary", "samples", "drums", "probes")
            ]
            self.assertTrue(
                any(isinstance(item, dict) and item for item in evidence),
                f"{relative} 既未声明不适用,也没有任何实测数据",
            )

    def test_provenance_is_recorded_in_manifest_or_resource_verification(self) -> None:
        for relative, directory in _sound_entries():
            manifest = json.loads(
                (directory / "乐器.json").read_text(encoding="utf-8")
            )
            resource = json.loads(
                (directory / "资源核验.json").read_text(encoding="utf-8")
            )
            merged = {**resource, **manifest}
            has_upstream = bool(str(merged.get("upstream", "")).strip())
            # 自研 DSP 入口没有上游音源,改以引擎哈希与许可状态留证。
            has_engine = bool(str(resource.get("engine_sha256", "")).strip())
            self.assertTrue(
                has_upstream or has_engine,
                f"{relative} 既无上游来源也无引擎哈希",
            )
            if has_engine and not has_upstream:
                self.assertTrue(
                    str(resource.get("license_status", "")).strip(),
                    f"{relative} 自研入口缺少许可状态",
                )
            else:
                # 许可可以写成文字声明,也可以是冻结的许可文件哈希;
                # 两者都是可追溯证据,只是批次格式不同。
                license_text = str(merged.get("license", "")).strip()
                license_hashes = (
                    merged.get("license_file_sha256")
                    or merged.get("evidence_sha256")
                    or {}
                )
                self.assertTrue(
                    license_text or license_hashes,
                    f"{relative} 既无许可声明也无许可文件哈希",
                )


if __name__ == "__main__":
    unittest.main()
