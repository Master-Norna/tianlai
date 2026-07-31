"""第一波 46 项 fallback→candidate 升级的守护测试。

覆盖:总账 98 项全部脱离 fallback;46 个新升级条目的清单、审计工件与
来源字段完备;新引擎(dedicated_fx / reversed_cymbal / melodic_toms /
modeled_instrument)的确定性、音域约束与关键行为;以及 dedicated_sfz
本波修复(力度网格、单边随机区间、行中 include、静态键切换)之上的
真实资源渲染冒烟。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
import unittest

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.events import PerformanceEvent, parse_performance_document
from tianlai.instrument import create_instrument
from tianlai.modeled_instruments import ENGINE_VERSION
from tianlai.quality import load_upgrade_progress
from tianlai.renderer import render_document
from tianlai.tuning import EqualTemperament


INSTRUMENT_ROOT = ROOT / "乐器"

WAVE_IDS = {
    "VPO-14", "ORP-11",
    "SAM-01", "SAM-02", "SAM-03", "SAM-04", "SAM-05", "SAM-06", "SAM-07",
    "SAM-08", "SAM-10", "SAM-11", "SAM-12", "SAM-13", "SAM-14", "SAM-15",
    "SAM-16", "SAM-17", "SAM-18", "SAM-23", "SAM-24", "SAM-25", "SAM-26",
    "SAM-27", "SAM-28", "SAM-30", "SAM-31", "SAM-32", "SAM-33", "SAM-34",
    "SAM-35", "SAM-36", "SAM-37", "SAM-38", "SAM-39", "SAM-40", "SAM-41",
    "SAM-42", "SAM-43", "SAM-44", "SAM-45", "SAM-46", "SAM-47", "SAM-48",
    "SAM-49", "SAM-51",
}

SAMPLE_BACKED_TYPES = {
    "dedicated_sfz", "dedicated_fx", "reversed_cymbal", "melodic_toms"
}


def _progress():
    return load_upgrade_progress(INSTRUMENT_ROOT)


def _wave_entries():
    return [entry for entry in _progress().entries if entry.upgrade_id in WAVE_IDS]


def _render(manifest: dict, base_directory: Path, events: list[dict], *, tail=0.4):
    document = parse_performance_document(
        {
            "sample_rate": 48000,
            "channels": 2,
            "tail_seconds": tail,
            "events": events,
        }
    )
    instrument = create_instrument(manifest, 48000, base_directory=str(base_directory))
    stream, _ = render_document(instrument, document)
    return [(float(left), float(right)) for left, right in stream]


def _require_manifest_assets(manifest_path: Path, manifest: dict) -> Path:
    asset_root = (
        manifest_path.parent / str(manifest.get("asset_root", ""))
    ).resolve()
    if not asset_root.exists():
        raise unittest.SkipTest(
            f"external audio resource is not installed: {asset_root}"
        )
    return asset_root


class LedgerTests(unittest.TestCase):
    def test_wave_covers_exactly_the_46_previous_fallback_entries(self) -> None:
        self.assertEqual(len(WAVE_IDS), 46)

    def test_no_entry_remains_fallback_and_all_are_standalone_formal(self) -> None:
        progress = _progress()
        self.assertEqual(progress.counts["fallback"], 0)
        self.assertEqual(progress.counts["candidate"], 0)
        self.assertEqual(progress.counts["formal"], 98)
        self.assertEqual(progress.collaboration_counts["untested"], 98)
        self.assertEqual(progress.collaboration_counts["passed"], 0)
        for entry in _wave_entries():
            self.assertEqual(
                entry.quality_tier, "formal", f"{entry.upgrade_id} 不是单音色 formal"
            )
            self.assertEqual(entry.collaboration_review_status, "untested")
            self.assertNotEqual(
                entry.implementation_type, "soundfont",
                f"{entry.upgrade_id} 仍是通用 SoundFont",
            )

    def test_every_wave_entry_ships_audit_artifacts(self) -> None:
        for entry in _wave_entries():
            directory = Path(entry.manifest_path).parent
            for artifact in ("资源核验.json", "音准校准.json", "试听核验.json",
                             "README.md", "来源.md", "核验资源.py", "核验试听.py"):
                self.assertTrue(
                    (directory / artifact).is_file(),
                    f"{entry.upgrade_id} 缺少 {artifact}",
                )
            manifest = json.loads(
                Path(entry.manifest_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest.get("fallback_policy"), "explicit_only_no_silent_gm",
                f"{entry.upgrade_id} 缺少显式回退策略",
            )

    @pytest.mark.external_assets
    def test_sample_backed_entries_freeze_provenance_and_source_hashes(self) -> None:
        checked = 0
        for entry in _wave_entries():
            manifest = json.loads(
                Path(entry.manifest_path).read_text(encoding="utf-8")
            )
            if manifest["type"] not in SAMPLE_BACKED_TYPES:
                continue
            directory = Path(entry.manifest_path).parent
            for key in ("upstream", "origin", "upstream_version", "license"):
                self.assertTrue(
                    str(manifest.get(key, "")).strip(),
                    f"{entry.upgrade_id} 缺少来源字段 {key}",
                )
            report = json.loads(
                (directory / "资源核验.json").read_text(encoding="utf-8")
            )
            self.assertTrue(report.get("evidence_sha256"), entry.upgrade_id)
            self.assertTrue(report.get("sample_set_sha256"), entry.upgrade_id)
            asset_root = (directory / str(manifest["asset_root"])).resolve()
            sources = {
                relative: (asset_root / relative, expected)
                for relative, expected in report.get(
                    "source_file_sha256", {}
                ).items()
            }
            if not sources:
                continue
            existing = {
                relative
                for relative, (path, _expected) in sources.items()
                if path.is_file()
            }
            if not existing:
                continue
            missing = sorted(set(sources) - existing)
            self.assertEqual(
                missing,
                [],
                f"{entry.upgrade_id} 音源为部分安装：{missing}",
            )
            checked += 1
            for relative, (path, expected) in sources.items():
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                self.assertEqual(
                    actual, expected,
                    f"{entry.upgrade_id}: {relative} 的 SHA-256 与冻结值不符",
                )
        if checked == 0:
            self.skipTest("未安装任何本测试覆盖的第三方音频资源")

    def test_audition_reports_are_clip_free_and_hash_locked(self) -> None:
        for entry in _wave_entries():
            directory = Path(entry.manifest_path).parent
            report = json.loads(
                (directory / "试听核验.json").read_text(encoding="utf-8")
            )
            self.assertEqual(report["clipped_samples"], 0, entry.upgrade_id)
            self.assertGreater(report["peak"], 0.05, entry.upgrade_id)
            self.assertEqual(len(report["wav_sha256"]), 64, entry.upgrade_id)
            self.assertIn(report["human_review"], {"pending", "passed"}, entry.upgrade_id)


class ModeledInstrumentTests(unittest.TestCase):
    def _manifest(self, profile: str, **overrides):
        manifest = {
            "type": "modeled_instrument",
            "engine_version": ENGINE_VERSION,
            "profile": profile,
            "quality_tier": "candidate",
            "seed": 4242,
            "note_min": 48,
            "note_max": 84,
            "gain": 0.4,
        }
        manifest.update(overrides)
        return manifest

    def test_same_seed_is_bit_deterministic_and_different_seed_differs(self) -> None:
        events = [
            {"time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 60, "velocity": 0.8},
            {"time": 0.3, "type": "note_off", "note_id": 1},
        ]
        first = _render(self._manifest("koto"), ROOT, events, tail=0.2)
        second = _render(self._manifest("koto"), ROOT, events, tail=0.2)
        self.assertEqual(first, second)
        other = _render(self._manifest("shamisen"), ROOT, events, tail=0.2)
        self.assertNotEqual(first, other)
        peak = max(max(abs(left), abs(right)) for left, right in first)
        self.assertGreater(peak, 0.01)
        self.assertLess(peak, 1.0)

    def test_note_range_is_enforced(self) -> None:
        instrument = create_instrument(
            self._manifest("steelpan"), 48000, base_directory=str(ROOT)
        )
        with self.assertRaisesRegex(ValueError, "outside declared range"):
            instrument.handle_event(
                PerformanceEvent(
                    0, 0, "note_on", {"note_id": 1, "midi_note": 24, "velocity": 0.8}
                ),
                EqualTemperament(),
            )

    def test_keymap_profile_rejects_unmapped_keys(self) -> None:
        instrument = create_instrument(
            self._manifest("taiko", note_min=59, note_max=62),
            48000,
            base_directory=str(ROOT),
        )
        with self.assertRaisesRegex(ValueError, "has no key"):
            instrument.handle_event(
                PerformanceEvent(
                    0, 0, "note_on", {"note_id": 1, "midi_note": 59, "velocity": 0.8}
                ),
                EqualTemperament(),
            )

    def test_unknown_profile_fails_fast(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown modeled_instrument profile"):
            create_instrument(
                self._manifest("theremin"), 48000, base_directory=str(ROOT)
            )

    def test_missing_or_wrong_engine_version_fails_fast(self) -> None:
        cases = {
            "missing": self._manifest("koto"),
            "wrong": self._manifest("koto", engine_version="0.0.0"),
        }
        cases["missing"].pop("engine_version")
        for case, manifest in cases.items():
            with self.subTest(case=case), self.assertRaisesRegex(
                ValueError,
                "engine_version .* does not match runtime",
            ):
                create_instrument(
                    manifest,
                    48000,
                    base_directory=str(ROOT),
                )


class DedicatedFxTests(unittest.TestCase):
    @pytest.mark.external_assets
    def test_muted_trumpet_chain_is_deterministic_and_bounded(self) -> None:
        manifest_path = INSTRUMENT_ROOT / "管弦乐" / "铜管组" / "弱音小号" / "乐器.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _require_manifest_assets(manifest_path, manifest)
        events = [
            {"time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 66, "velocity": 0.75},
            {"time": 0.5, "type": "note_off", "note_id": 1},
        ]
        first = _render(manifest, manifest_path.parent, events, tail=0.3)
        second = _render(manifest, manifest_path.parent, events, tail=0.3)
        self.assertEqual(first, second)
        peak = max(max(abs(left), abs(right)) for left, right in first)
        self.assertGreater(peak, 0.02)
        self.assertLess(peak, 1.0)

    def test_effects_array_is_required(self) -> None:
        manifest_path = INSTRUMENT_ROOT / "键盘乐器" / "合唱电钢琴" / "乐器.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        stripped = {
            key: value for key, value in manifest.items() if key != "effects"
        }
        with self.assertRaisesRegex(ValueError, "non-empty effects array"):
            create_instrument(
                stripped, 48000, base_directory=str(manifest_path.parent)
            )


class ReversedCymbalTests(unittest.TestCase):
    @pytest.mark.external_assets
    def test_reversed_playback_swells_and_respects_range(self) -> None:
        manifest_path = (
            INSTRUMENT_ROOT / "管弦乐" / "打击乐组" / "反向镲" / "乐器.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _require_manifest_assets(manifest_path, manifest)
        # 键 60 的源采样倒放后是约 15.8 s 的长上升沿;渲染前 6 s,
        # 后段(4.5-5.5 s)必须显著响于前段(0.5-1.5 s)。
        events = [
            {"time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 60, "velocity": 0.9},
            {"time": 6.0, "type": "note_off", "note_id": 1},
        ]
        frames = _render(manifest, manifest_path.parent, events, tail=0.1)
        early = max(abs(left) for left, _ in frames[24000:72000])
        late = max(abs(left) for left, _ in frames[216000:264000])
        self.assertGreater(late, early * 1.5, "倒放应当呈上升沿")

        instrument = create_instrument(
            manifest, 48000, base_directory=str(manifest_path.parent)
        )
        with self.assertRaisesRegex(ValueError, "outside declared range"):
            instrument.handle_event(
                PerformanceEvent(
                    0, 0, "note_on", {"note_id": 9, "midi_note": 40, "velocity": 0.5}
                ),
                EqualTemperament(),
            )


class MelodicTomsTests(unittest.TestCase):
    @pytest.mark.external_assets
    def test_roots_come_from_frozen_measured_calibration(self) -> None:
        manifest_path = (
            INSTRUMENT_ROOT / "管弦乐" / "打击乐组" / "旋律通鼓" / "乐器.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _require_manifest_assets(manifest_path, manifest)
        calibration = json.loads(
            (manifest_path.parent / "音准校准.json").read_text(encoding="utf-8")
        )
        self.assertTrue(calibration["applicable"])
        for drum in ("low", "high"):
            self.assertIn(drum, calibration["drums"])
            self.assertGreater(calibration["drums"][drum]["root_midi"], 30.0)
            self.assertLess(calibration["drums"][drum]["root_midi"], 70.0)
        events = [
            {"time": 0.0, "type": "note_on", "note_id": 1,
             "midi_note": manifest["note_min"], "velocity": 0.7},
            {"time": 0.3, "type": "note_off", "note_id": 1},
            {"time": 0.45, "type": "note_on", "note_id": 2,
             "midi_note": manifest["note_max"], "velocity": 0.7},
            {"time": 0.75, "type": "note_off", "note_id": 2},
        ]
        frames = _render(manifest, manifest_path.parent, events, tail=0.5)
        peak = max(max(abs(left), abs(right)) for left, right in frames)
        self.assertGreater(peak, 0.02)
        self.assertLess(peak, 1.0)


@pytest.mark.external_assets
class RealAssetSmokeTests(unittest.TestCase):
    """少量真实资源端到端渲染,守住关键引擎修复不回退。"""

    def _load(self, *parts: str):
        manifest_path = INSTRUMENT_ROOT.joinpath(*parts) / "乐器.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        _require_manifest_assets(manifest_path, manifest)
        return (
            manifest,
            manifest_path.parent,
        )

    def test_vcsl_marimba_renders_velocity_crossfade_layers(self) -> None:
        manifest, base = self._load("管弦乐", "打击乐组", "马林巴")
        events = []
        for index, velocity in enumerate((0.2, 0.6, 0.95)):
            events.append({"time": index * 0.3, "type": "note_on",
                           "note_id": index + 1, "midi_note": 60, "velocity": velocity})
            events.append({"time": index * 0.3 + 0.25, "type": "note_off",
                           "note_id": index + 1})
        frames = _render(manifest, base, events, tail=0.6)
        peak = max(max(abs(left), abs(right)) for left, right in frames)
        self.assertGreater(peak, 0.05)
        self.assertLess(peak, 1.0)

    def test_emily_guitar_boundary_velocity_hits_no_gap(self) -> None:
        # 0.32×127 = 40.64 落在上游力度层 40|41 的缝里;
        # 力度网格对齐修复后必须仍能发声而不是抛错。
        manifest, base = self._load("弹拨乐器", "清音电吉他")
        events = [
            {"time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 62,
             "velocity": 0.32},
            {"time": 0.4, "type": "note_off", "note_id": 1},
        ]
        frames = _render(manifest, base, events, tail=0.3)
        peak = max(max(abs(left), abs(right)) for left, right in frames)
        self.assertGreater(peak, 0.01)

    def test_ganjo_b3_level_correction_matches_adjacent_notes(self) -> None:
        manifest, base = self._load("世界乐器", "班卓琴")
        self.assertEqual(
            manifest["sample_gain_db_overrides"],
            [
                {
                    "sample": "Common/Banjo_Common - B3.wav",
                    "gain_db": 3.2,
                }
            ],
        )

        levels = {}
        peaks = {}
        for note in (46, 47, 48):
            frames = _render(
                manifest,
                base,
                [
                    {
                        "time": 0.0,
                        "type": "note_on",
                        "note_id": 1,
                        "midi_note": note,
                        "velocity": 0.72,
                    },
                    {"time": 0.48, "type": "note_off", "note_id": 1},
                ],
                tail=0.0,
            )
            powers = [
                (left * left + right * right) * 0.5
                for left, right in frames[: round(0.48 * 48_000)]
            ]
            levels[note] = math.sqrt(sum(powers) / len(powers))
            peaks[note] = max(
                max(abs(left), abs(right))
                for left, right in frames
            )

        neighbour_db = 10.0 * math.log10(levels[46] * levels[48])
        corrected_db = 20.0 * math.log10(levels[47])
        self.assertLess(abs(corrected_db - neighbour_db), 1.0)
        self.assertTrue(all(0.01 < peak < 1.0 for peak in peaks.values()))

    def test_ganjo_g_sharp_uses_only_consistent_three_sample_round_robin(
        self,
    ) -> None:
        manifest, base = self._load("世界乐器", "班卓琴")
        self.assertEqual(
            manifest["sample_region_exclusions"],
            [
                "Common/Banjo_Common - G#4.wav",
                "Common/Banjo_Common - G#4_5.wav",
                "Common/Banjo_Common - G#4_6.wav",
            ],
        )

        instrument = create_instrument(
            manifest,
            48_000,
            base_directory=str(base),
        )
        matching = [
            region
            for engine in instrument.articulations["normal"].attacks
            for region in engine.regions
            if region.key_min <= 56 <= region.key_max
        ]
        self.assertEqual(
            [region.path.name for region in matching],
            [
                "Banjo_Common - G#4_2.wav",
                "Banjo_Common - G#4_3.wav",
                "Banjo_Common - G#4_4.wav",
            ],
        )
        self.assertEqual(
            [region.round_robin_position for region in matching],
            [1, 2, 3],
        )
        self.assertEqual(
            {region.round_robin_length for region in matching},
            {3},
        )


if __name__ == "__main__":
    unittest.main()
