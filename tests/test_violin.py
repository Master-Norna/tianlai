import json
from pathlib import Path
import unittest

import pytest

from tianlai.capability import read_capability
from tianlai.events import PerformanceEvent
from tianlai.instrument import create_instrument
from tianlai.sfz import regions_to_manifest
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "乐器" / "管弦乐" / "弦乐组" / "小提琴" / "乐器.json"
WAVE_ROOT = ROOT / "音源" / "VirtualPlayingOrchestra" / "Virtual-Playing-Orchestra3" / "libs"
pytestmark = pytest.mark.external_assets


@unittest.skipUnless(WAVE_ROOT.is_dir(), "Virtual Playing Orchestra wave files are not installed")
class ViolinInstrumentTests(unittest.TestCase):
    def create_violin(self, variant: str | None = None, **overrides):
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        if variant is not None:
            manifest["sample_variant"] = variant
        manifest.update(overrides)
        return create_instrument(manifest, 48000, base_directory=str(MANIFEST.parent))

    def test_default_variant_is_solo(self) -> None:
        self.assertEqual(self.create_violin().sample_variant, "SOLO")

    def test_section_variant_loads_a_different_sample_library(self) -> None:
        """SEC 变体必须真的换成 SSO 声部采样,而不是照旧加载独奏采样。

        这是"一把小提琴两种编制"的核心:同一入口、同一套奏法,换的是采样库。
        """

        solo = self.create_violin("SOLO")
        section = self.create_violin("SEC")
        # 采样奏法两边齐全;accent 的实现方式按变体不同(见 accent 专项测试)。
        sampled = {"sustain", "slow_sustain", "staccato", "pizzicato", "tremolo"}
        self.assertTrue(sampled <= set(solo.engines))
        self.assertTrue(sampled <= set(section.engines))
        solo_samples = {region.path for region in solo.engines["sustain"].regions}
        section_samples = {region.path for region in section.engines["sustain"].regions}
        self.assertTrue(solo_samples.isdisjoint(section_samples))
        self.assertTrue(any("NoBudgetOrch" in str(path) for path in solo_samples))
        # SSO 的声部采样路径含空格("1st Violins"),正是 SFZ 解析必须支持的情形。
        self.assertTrue(any("SSO" in str(path) for path in section_samples))

    def test_unknown_variant_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "sample_variant"):
            self.create_violin("SECTION")

    def test_sustained_engines_never_load_staccato_samples(self) -> None:
        """持续音引擎里绝不能混入断奏采样。

        上游 SEC 的 normal-mod-wheel 是"断奏 + 持续"由 mod wheel(CC1)交叉淡入的
        复合 patch。本采样器不读 CC1,若把它当持续映射就会两层同时出声:断奏音头
        压在持续音上(音色发浑),两层各自的 tune 不同还会产生拍频(听感像跑调)。
        曾经因此让 SEC 变体整体劣化,故在此钉死。
        """

        for variant in ("SOLO", "SEC"):
            violin = self.create_violin(variant)
            for engine_name in ("sustain", "slow_sustain", "accent_sustain"):
                engine = violin.engines.get(engine_name)
                self.assertIsNotNone(
                    engine,
                    f"{variant} 缺少独立的 {engine_name} 持续层",
                )
                names = [Path(region.path).name.lower() for region in engine.regions]
                staccato = [name for name in names if "-stc" in name or "stac" in name]
                self.assertEqual(
                    staccato, [],
                    f"{variant} 的 {engine_name} 引擎混入了断奏采样: {staccato[:3]}",
                )

    def test_manifest_release_overrides_sfz_sustain_regions(self) -> None:
        """作品级 release 必须落到实际 voice，不能被 SFZ 的 1.6/1.9 秒盖掉。"""

        for variant in ("SOLO", "SEC"):
            violin = self.create_violin(
                variant,
                release_seconds=0.11,
            )
            sustained = ("sustain", "slow_sustain", "tremolo")
            for engine_name in sustained:
                self.assertTrue(
                    all(
                        region.release_seconds == 0.11
                        for region in violin.engines[engine_name].regions
                    ),
                    f"{variant} {engine_name} 没有落实 manifest release",
                )
            self.assertTrue(
                all(
                    region.release_seconds == 0.11
                    for region in violin.engines["accent_sustain"].regions
                )
            )

    def test_synthetic_accent_attack_is_bounded_in_time(self) -> None:
        """SOLO accent 的 staccato 瞬态不能作为 one-shot 自由跑完整段采样。"""

        violin = self.create_violin("SOLO")
        tuning = EqualTemperament(440.0)
        violin.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}),
            tuning,
        )
        violin.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 1, "midi_note": 69.0, "velocity": 0.8},
            ),
            tuning,
        )
        self.assertEqual(violin.engines["staccato"].active_voice_count, 1)
        for _ in range(round(0.4 * 48_000)):
            violin.render_frame()
        self.assertEqual(violin.engines["staccato"].active_voice_count, 0)
        self.assertEqual(violin.engines["accent_sustain"].active_voice_count, 1)

    def test_both_variants_sound_across_the_range(self) -> None:
        """两个变体在音域内都要真出声——曾经 SEC 因路径含空格而静默失败。"""

        import numpy as np

        tuning = EqualTemperament(440.0)
        for variant in ("SOLO", "SEC"):
            for midi in (55.0, 69.0, 88.0):
                violin = self.create_violin(variant)
                violin.handle_event(
                    PerformanceEvent(0, 0, "note_on",
                                     {"note_id": 1, "midi_note": midi, "velocity": 0.7}),
                    tuning,
                )
                block = np.array(
                    [sum(violin.render_frame()) * 0.5 for _ in range(int(48000 * 0.3))]
                )
                self.assertGreater(
                    float(np.max(np.abs(block))), 1e-3,
                    f"{variant} 变体在 MIDI {midi:g} 上没有出声",
                )

    def test_section_accent_splits_upstream_attack_and_sustain_layers(self) -> None:
        """SEC accent 每音都必须同时播放即时 RR 音头和延迟持续体。

        上游 SFZ 是复合 patch,不是三个可互换的普通 region。如果把两枚 RR 音头
        和延迟 200ms 的持续体交给一个 SampleInstrument,它会每音只挑其中之一；
        64ms 短音一旦挑中持续体,还没走完 delay 就会被 note_off 静音。
        """

        section = self.create_violin("SEC", release_seconds=0.26)
        self.assertNotIn("accent", section.engines)
        self.assertIn("accent_attack", section.engines)
        self.assertIn("accent_sustain", section.engines)

        attack_names = [
            Path(region.path).name.lower()
            for region in section.engines["accent_attack"].regions
        ]
        sustain_names = [
            Path(region.path).name.lower()
            for region in section.engines["accent_sustain"].regions
        ]
        self.assertTrue(attack_names)
        self.assertTrue(sustain_names)
        self.assertTrue(all("-stc-rr" in name for name in attack_names))
        self.assertEqual(
            {
                marker
                for marker in ("rr1", "rr2")
                if any(marker in name for name in attack_names)
            },
            {"rr1", "rr2"},
        )
        self.assertTrue(all("-sus-" in name for name in sustain_names))
        self.assertTrue(set(attack_names).isdisjoint(sustain_names))

        tuning = EqualTemperament(440.0)
        section.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}),
            tuning,
        )
        section.handle_event(
            PerformanceEvent(
                0,
                1,
                "note_on",
                {"note_id": 1, "midi_note": 69.0, "velocity": 0.8},
            ),
            tuning,
        )

        self.assertEqual(section.engines["accent_attack"].active_voice_count, 1)
        self.assertEqual(section.engines["accent_sustain"].active_voice_count, 1)
        attack_voice = next(iter(section.engines["accent_attack"].voices.values()))
        sustain_voice = next(iter(section.engines["accent_sustain"].voices.values()))
        self.assertEqual(attack_voice.delay_samples, 0)
        self.assertEqual(sustain_voice.delay_samples, round(0.20 * 48_000))
        self.assertEqual(sustain_voice.release_samples, round(0.26 * 48_000))

    def test_section_accent_round_robin_only_rotates_attack_samples(self) -> None:
        """RR1/RR2 只轮换音头；同一个持续采样必须伴随每一次 accent。"""

        section = self.create_violin("SEC")
        tuning = EqualTemperament(440.0)
        section.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}),
            tuning,
        )

        attack_names = []
        sustain_names = []
        for note_id in range(1, 5):
            old_attack_ids = set(section.engines["accent_attack"].voices)
            old_sustain_ids = set(section.engines["accent_sustain"].voices)
            section.handle_event(
                PerformanceEvent(
                    0,
                    note_id,
                    "note_on",
                    {"note_id": note_id, "midi_note": 69.0, "velocity": 0.8},
                ),
                tuning,
            )
            new_attack_id = (
                set(section.engines["accent_attack"].voices) - old_attack_ids
            ).pop()
            new_sustain_id = (
                set(section.engines["accent_sustain"].voices) - old_sustain_ids
            ).pop()
            attack_names.append(
                section.engines["accent_attack"]
                .voices[new_attack_id]
                .region.path.name.lower()
            )
            sustain_names.append(
                section.engines["accent_sustain"].voices[
                    new_sustain_id
                ].region.path.name.lower()
            )

        self.assertEqual(
            [
                "rr1" if "rr1" in name else "rr2" if "rr2" in name else "invalid"
                for name in attack_names
            ],
            ["rr1", "rr2", "rr1", "rr2"],
        )
        self.assertTrue(all("-stc-" in name for name in attack_names))
        self.assertEqual(len(set(sustain_names)), 1)
        self.assertTrue(all("-sus-" in name for name in sustain_names))

    def test_every_section_accent_has_audible_attack_in_a_64ms_gate(self) -> None:
        """重复短音不能每逢第三次选中延迟持续层而整个起音窗静默。"""

        import numpy as np

        section = self.create_violin("SEC", release_seconds=0.26)
        tuning = EqualTemperament(440.0)
        section.handle_event(
            PerformanceEvent(0, 0, "articulation", {"name": "accent"}),
            tuning,
        )

        peaks = []
        for note_id in range(1, 5):
            section.handle_event(
                PerformanceEvent(
                    0,
                    note_id,
                    "note_on",
                    {"note_id": note_id, "midi_note": 69.0, "velocity": 0.8},
                ),
                tuning,
            )
            block = np.array(
                [
                    0.5 * sum(section.render_frame())
                    for _ in range(round(0.064 * 48_000))
                ]
            )
            peaks.append(float(np.max(np.abs(block))))
            section.handle_event(
                PerformanceEvent(
                    0,
                    100 + note_id,
                    "note_off",
                    {"note_id": note_id},
                ),
                tuning,
            )
            for _ in range(round(0.40 * 48_000)):
                section.render_frame()
            self.assertEqual(section.active_voice_count, 0)

        self.assertTrue(
            all(peak > 1e-3 for peak in peaks),
            f"SEC accent 的 64ms 起音窗出现静音: {peaks}",
        )

    def test_section_accent_attack_gate_and_sustain_release_are_independent(self) -> None:
        """音头按短门限退出，持续体按作品传入的 release 完整收音。"""

        tuning = EqualTemperament(440.0)
        for release_seconds in (0.26, 0.28):
            with self.subTest(release_seconds=release_seconds):
                section = self.create_violin(
                    "SEC",
                    release_seconds=release_seconds,
                )
                section.handle_event(
                    PerformanceEvent(0, 0, "articulation", {"name": "accent"}),
                    tuning,
                )
                section.handle_event(
                    PerformanceEvent(
                        0,
                        1,
                        "note_on",
                        {"note_id": 1, "midi_note": 69.0, "velocity": 0.8},
                    ),
                    tuning,
                )
                sustain_voice = next(
                    iter(section.engines["accent_sustain"].voices.values())
                )
                self.assertEqual(
                    sustain_voice.release_samples,
                    round(release_seconds * 48_000),
                )
                self.assertNotEqual(
                    sustain_voice.release_samples,
                    round(1.9 * 48_000),
                )

                for _ in range(round(0.17 * 48_000)):
                    section.render_frame()
                self.assertEqual(
                    section.engines["accent_attack"].active_voice_count,
                    1,
                )
                for _ in range(round(0.14 * 48_000)):
                    section.render_frame()
                self.assertEqual(
                    section.engines["accent_attack"].active_voice_count,
                    0,
                )
                self.assertEqual(
                    section.engines["accent_sustain"].active_voice_count,
                    1,
                )

                section.handle_event(
                    PerformanceEvent(
                        0,
                        2,
                        "note_off",
                        {"note_id": 1},
                    ),
                    tuning,
                )
                for _ in range(round(release_seconds * 0.5 * 48_000)):
                    section.render_frame()
                self.assertEqual(
                    section.engines["accent_sustain"].active_voice_count,
                    1,
                )
                for _ in range(
                    round((release_seconds * 0.6 + 0.02) * 48_000)
                ):
                    section.render_frame()
                self.assertEqual(
                    section.engines["accent_sustain"].active_voice_count,
                    0,
                )

    def test_all_sampled_articulations_are_loaded(self) -> None:
        violin = self.create_violin()
        # accent_sustain 是内部引擎:accent 用它(从采样中段起播、快速起音)取代
        # 慢起弓 sustain 作为 body,消除“staccato 先响、sustain 后升”的第二次起音。
        self.assertEqual(
            set(violin.engines),
            {"sustain", "slow_sustain", "staccato", "pizzicato", "tremolo", "accent_sustain"},
        )
        self.assertTrue(any(region.loop_start is not None for region in violin.engines["sustain"].regions))

    def test_accent_body_starts_far_faster_than_slow_bowed_sustain(self) -> None:
        """accent 的 body 引擎从采样中段起播,起音必须远快于慢起弓 sustain。"""

        import numpy as np
        from tianlai.events import PerformanceEvent
        from tianlai.tuning import EqualTemperament

        def onset_ms(articulation: str) -> float:
            violin = self.create_violin()
            tuning = EqualTemperament(440.0)
            violin.handle_event(
                PerformanceEvent(0, 0, "articulation", {"name": articulation}), tuning
            )
            violin.handle_event(
                PerformanceEvent(0, 1, "note_on",
                                 {"note_id": 1, "midi_note": 69.0, "velocity": 0.7}),
                tuning,
            )
            buf = np.array([0.5 * sum(violin.render_frame()) for _ in range(48000)])
            env = np.abs(buf)
            peak = env.max()
            return float(np.argmax(env >= peak * 0.9)) / 48000 * 1000

        # A4 上 accent 应在百毫秒内到位,而慢起弓 sustain 要数百毫秒。
        self.assertLess(onset_ms("accent"), 200.0)
        self.assertGreater(onset_ms("sustain"), 400.0)

    def test_sustained_a4_uses_measured_pitch_calibration(self) -> None:
        violin = self.create_violin()
        calibration = json.loads(
            (MANIFEST.parent / "音准校准.json").read_text(encoding="utf-8")
        )["samples"]
        expected = next(
            item["measured_hz"]
            for path, item in calibration.items()
            if path.endswith("/Vibrato/4_A-PB.wav")
        )
        region = next(
            item
            for item in violin.engines["sustain"].regions
            if item.path.name == "4_A-PB.wav"
        )
        self.assertAlmostEqual(region.root_pitch_hz, expected, places=5)

    def test_native_root_core_and_repitched_extension_are_not_conflated(self) -> None:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        sfz_root = (
            MANIFEST.parent / str(manifest["asset_root"]) / "Strings"
        ).resolve()
        for variant in ("SOLO", "SEC"):
            with self.subTest(sample_variant=variant):
                regions = regions_to_manifest(
                    sfz_root
                    / f"1st-violin-{variant}-normal-mod-wheel.sfz",
                    use_embedded_loops=True,
                )
                self.assertLessEqual(
                    max(float(region["root_midi"]) for region in regions),
                    94.0,
                )
                self.assertEqual(
                    max(float(region["key_max"]) for region in regions),
                    105.0,
                )

        capability = read_capability(
            MANIFEST,
            root=ROOT / "乐器",
            defer_onset_evidence=True,
        )
        core = capability.evaluate_range_profile(94.0, "sustain")
        self.assertEqual(core.status, "contract_candidate_unverified")
        self.assertTrue(core.high_quality_covered)
        self.assertFalse(core.extended_covered)

        extension = capability.evaluate_range_profile(95.0, "sustain")
        self.assertEqual(extension.status, "outside_candidate_high_quality")
        self.assertFalse(extension.high_quality_covered)
        self.assertTrue(extension.extended_covered)

        different_variant = capability.evaluate_range_profile(
            90.0,
            "sustain",
            overrides={"sample_variant": "SEC"},
        )
        self.assertEqual(different_variant.status, "profile_not_found")

    def test_unknown_articulation_is_rejected(self) -> None:
        violin = self.create_violin()
        with self.assertRaisesRegex(ValueError, "unsupported violin articulation"):
            violin.handle_event(
                PerformanceEvent(0, 0, "articulation", {"name": "col_legno"}),
                EqualTemperament(),
            )

    def test_sampled_range_is_enforced(self) -> None:
        violin = self.create_violin()
        with self.assertRaisesRegex(ValueError, "outside the sampled"):
            violin.handle_event(
                PerformanceEvent(
                    0,
                    0,
                    "note_on",
                    {"note_id": 1, "midi_note": 54, "velocity": 0.8},
                ),
                EqualTemperament(),
            )


if __name__ == "__main__":
    unittest.main()
