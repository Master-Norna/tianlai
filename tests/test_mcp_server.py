"""MCP 服务的单元测试:重点守护鼓组 kit 声部的预检(曾把 kit 误判成"乐器 None")。

MCP 是可选组件,未安装 ``mcp`` 时整体跳过。测试用合成编制,不依赖 gitignore 的
乐谱文件;涉及目录校验时只用仓库内确实存在的乐器路径。
"""
from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch


_HAS_MCP = importlib.util.find_spec("mcp") is not None


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class AssignmentInstrumentsTest(unittest.TestCase):
    def setUp(self):
        from tianlai import mcp_server
        self.m = mcp_server

    def test_normal_assignment(self):
        a = {"executor_id": "1_钢琴", "instrument": "键盘乐器/钢琴"}
        self.assertEqual(self.m._assignment_instruments(a), ["键盘乐器/钢琴"])

    def test_kit_assignment_without_top_level_instrument(self):
        # 这正是曾经的 bug:kit 声部没有顶层 instrument,预检读到 None 就误判
        a = {"executor_id": "10_钹", "kit": {"C#3": "管弦乐/打击乐组/管弦钹"}}
        self.assertEqual(self.m._assignment_instruments(a), ["管弦乐/打击乐组/管弦钹"])

    def test_kit_with_object_values(self):
        a = {"kit": {"C2": {"instrument": "现代鼓组/底鼓"}, "D2": "现代鼓组/军鼓通鼓"}}
        got = sorted(self.m._assignment_instruments(a))
        self.assertEqual(got, ["现代鼓组/军鼓通鼓", "现代鼓组/底鼓"])

    def test_empty_assignment_yields_nothing(self):
        self.assertEqual(self.m._assignment_instruments({"executor_id": "x"}), [])


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class RosterProblemsTest(unittest.TestCase):
    def setUp(self):
        from tianlai import mcp_server
        self.m = mcp_server

    def _install_synthetic_quarantined_violin(self) -> None:
        previous = self.m._caps_cache
        capabilities = dict(self.m._caps())
        path = "管弦乐/弦乐组/小提琴"
        capabilities[path] = replace(
            capabilities[path],
            license_status="quarantined",
        )
        self.m._caps_cache = capabilities
        self.addCleanup(setattr, self.m, "_caps_cache", previous)

    def test_kit_roster_passes_existence_check(self):
        # kit 声部涉及真实存在的打击乐,预检(不限白名单)应通过——修复的核心
        roster = {"assignments": [
            {"executor_id": "1_钢琴", "instrument": "键盘乐器/钢琴"},
            {"executor_id": "10_钹", "kit": {"C#3": "管弦乐/打击乐组/管弦钹"}},
        ]}
        self.assertEqual(self.m._roster_instrument_problems(roster, trusted_only=False), [])

    def test_missing_instrument_flagged(self):
        roster = {"assignments": [{"executor_id": "x", "instrument": "不存在/乐器"}]}
        problems = self.m._roster_instrument_problems(roster, trusted_only=False)
        self.assertTrue(problems and "不存在/乐器" in problems[0])

    def test_assignment_without_instrument_or_kit_flagged(self):
        roster = {"assignments": [{"executor_id": "空壳"}]}
        problems = self.m._roster_instrument_problems(roster, trusted_only=False)
        self.assertTrue(problems and "空壳" in problems[0])

    def test_quarantined_license_cannot_be_opened_by_quality_override(self):
        self._install_synthetic_quarantined_violin()
        roster = {"assignments": [
            {"executor_id": "小提琴", "instrument": "管弦乐/弦乐组/小提琴"},
        ]}
        problems = self.m._roster_instrument_problems(
            roster,
            trusted_only=False,
        )
        self.assertTrue(problems)
        self.assertIn("许可证据已隔离", problems[0])
        self.assertIn("不能放开", problems[0])

    def test_untrusted_but_non_quarantined_can_still_be_explicit(self):
        roster = {"assignments": [
            {"executor_id": "击弦古钢琴", "instrument": "键盘乐器/击弦古钢琴"},
        ]}
        self.assertEqual(
            self.m._roster_instrument_problems(roster, trusted_only=False),
            [],
        )

    def test_cc0_ganjo_replacement_can_be_selected_explicitly(self):
        roster = {
            "assignments": [
                {
                    "executor_id": "班卓琴",
                    "instrument": "世界乐器/班卓琴",
                }
            ]
        }
        self.assertEqual(
            self.m._roster_instrument_problems(
                roster,
                trusted_only=False,
            ),
            [],
        )

    def test_soundfont_local_compatibility_cannot_be_opened(self):
        previous = self.m._caps_cache
        capabilities = dict(self.m._caps())
        path = "键盘乐器/击弦古钢琴"
        capabilities[path] = replace(
            capabilities[path],
            implementation_type="soundfont",
            license_status="approved",
        )
        self.m._caps_cache = capabilities
        self.addCleanup(setattr, self.m, "_caps_cache", previous)
        roster = {
            "assignments": [
                {"executor_id": "local", "instrument": path},
            ]
        }

        problems = self.m._roster_instrument_problems(
            roster,
            trusted_only=False,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("仅限显式本机兼容/测试", problems[0])

    def test_non_formal_reference_oscillator_never_enters_public_scope(self):
        roster = {
            "assignments": [
                {
                    "executor_id": "reference",
                    "instrument": "测试工具/参考振荡器",
                }
            ]
        }

        problems = self.m._roster_instrument_problems(
            roster,
            instrument_scope="formal",
        )

        self.assertEqual(len(problems), 1)
        self.assertIn("不是 MCP 公开 formal", problems[0])

    def test_unique_short_name_uses_the_same_resolution_as_core(self):
        roster = {
            "assignments": [
                {"part": "piano", "executor_id": "钢琴", "instrument": "钢琴"}
            ]
        }
        self.assertEqual(
            self.m._roster_instrument_problems(roster, trusted_only=True),
            [],
        )

    def test_quarantined_short_name_resolves_before_policy_check(self):
        self._install_synthetic_quarantined_violin()
        roster = {
            "assignments": [
                {
                    "part": "violin",
                    "executor_id": "小提琴",
                    "instrument": "小提琴",
                }
            ]
        }
        problems = self.m._roster_instrument_problems(
            roster,
            trusted_only=False,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("许可证据已隔离", problems[0])

    def test_published_format_example_uses_real_trusted_paths(self):
        example = self.m.score_and_roster_format()
        self.assertEqual(
            self.m._roster_instrument_problems(
                example["example_roster"],
                trusted_only=True,
            ),
            [],
        )

    def test_published_mapped_articulation_and_kit_examples_validate(self):
        examples = self.m.score_and_roster_format()

        direct = self.m.validate_project(
            examples["example_score"],
            examples["example_roster"],
            hall=False,
            write_stems=False,
            collaboration_mode="manual",
            use_stem_cache=False,
        )
        kit = self.m.validate_project(
            examples["example_kit_score"],
            examples["example_kit_roster"],
            hall=False,
            write_stems=False,
            collaboration_mode="manual",
            use_stem_cache=False,
        )

        self.assertTrue(direct["ok"], direct.get("issues"))
        self.assertTrue(kit["ok"], kit.get("issues"))
        for result in (direct, kit):
            self.assertTrue(result["self_check"]["can_proceed"])
            self.assertEqual(result["self_check"]["blocking_count"], 0)
            self.assertTrue(result["project_review"]["continuation_allowed"])
            self.assertEqual(result["project_review"]["blocking_count"], 0)
            self.assertTrue(
                all(issue["severity"] == "error" for issue in result["issues"])
            )


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class InstrumentPaletteTest(unittest.TestCase):
    def setUp(self):
        from tianlai import mcp_server
        self.m = mcp_server

    def test_missing_allowlist_fails_closed_for_default_palette(self):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            with patch.object(self.m, "ALLOWLIST_FILE", missing):
                result = self.m.list_instruments(trusted_only=True)

        self.assertFalse(result["ok"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(
            result["issues"][0]["code"],
            "instrument.scope_invalid",
        )

    def test_missing_allowlist_does_not_block_explicit_untrusted_palette(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.json"
            with patch.object(self.m, "ALLOWLIST_FILE", missing):
                result = self.m.list_instruments(trusted_only=False)

        self.assertTrue(result["ok"])
        self.assertEqual(result["catalog_count"], 103)
        self.assertEqual(result["matched_count"], 103)
        self.assertEqual(result["count"], 32)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["curation_state"], "unavailable")
        self.assertIsNone(result["curated_count"])
        self.assertTrue(
            all(item["curated"] is None for item in result["instruments"])
        )

    def test_articulation_range_overrides_are_machine_readable(self):
        palette = self.m.list_instruments(
            trusted_only=False,
            detail_level="full",
            limit=128,
        )
        by_path = {
            item["instrument"]: item for item in palette["instruments"]
        }

        self.assertEqual(palette["count"], 103)
        self.assertEqual(
            by_path["世界乐器/班卓琴"]["license_status"],
            "approved",
        )

        bianzhong = by_path["世界乐器/编钟"]
        self.assertEqual(
            (bianzhong["note_min"], bianzhong["note_max"]),
            (36.0, 98.0),
        )
        self.assertEqual(
            set(bianzhong["articulations"]),
            {"zhenggu", "cegu"},
        )
        self.assertEqual(bianzhong["default_articulation"], "zhenggu")
        self.assertIn("implementation_type", bianzhong)
        self.assertEqual(bianzhong["pitch_mode"], "pitched")
        self.assertFalse(bianzhong["ignores_pitch"])

        timpani = by_path["管弦乐/打击乐组/定音鼓"]
        self.assertEqual(timpani["playable_ranges"], [])
        self.assertEqual(
            timpani["articulation_playable_ranges"],
            {
                "hit": [[38.0, 59.0]],
                "roll": [[41.0, 55.0]],
            },
        )
        self.assertEqual(
            timpani["articulation_range_contracts"]["roll"],
            {
                "midi_ranges": [[41.0, 55.0]],
                "note_ranges": ["F2~G3"],
                "source": "articulation_override",
            },
        )

        bagpipe = by_path["世界乐器/风笛"]
        self.assertEqual(
            bagpipe["articulation_range_contracts"]["chanter"],
            {
                "midi_ranges": [[64.0, 81.0]],
                "note_ranges": ["E4~A5"],
                "source": "articulation_override",
            },
        )
        self.assertEqual(
            bianzhong["articulation_range_contracts"]["zhenggu"],
            {
                "midi_ranges": [[36.0, 98.0]],
                "note_ranges": ["C2~D7"],
                "source": "instrument_note_bounds",
            },
        )

        violin = by_path["管弦乐/弦乐组/小提琴"]
        self.assertEqual(
            violin["duration_articulation_rules"],
            [
                {
                    "rule_id": "violin_short_neutral_bow_v1",
                    "source_articulation": "sustain",
                    "target_articulation": "accent",
                    "below_seconds": 1.2,
                }
            ],
        )
        self.assertEqual(
            bianzhong["duration_articulation_rules"],
            [],
        )

        kick = by_path["现代鼓组/底鼓"]
        self.assertEqual(kick["pitch_mode"], "fixed")
        self.assertEqual(kick["fixed_midi_note"], 60.0)
        self.assertEqual(kick["fixed_note"], "C4")
        self.assertTrue(kick["ignores_pitch"])

        vibraphone = by_path["管弦乐/打击乐组/颤音琴"]
        self.assertEqual(vibraphone["playable_ranges"], [])
        self.assertEqual(
            vibraphone["articulation_playable_ranges"]["bowed"],
            [[57.0, 89.0]],
        )
        self.assertEqual(
            vibraphone["articulation_playable_ranges"]["open"],
            [[53.0, 89.0]],
        )
        self.assertIn("未列奏法继承全局分段", palette["range_semantics"])
        self.assertIn("note_min/note_max", palette["range_semantics"])
        self.assertIn("range_profiles", palette["range_semantics"])
        self.assertIn(
            "articulation_range_contracts", palette["agent_writing_rule"]
        )
        self.assertIn("总包络", palette["agent_writing_rule"])
        self.assertIn("range_contract_status", timpani)
        self.assertIn("range_profiles", timpani)
        self.assertEqual(timpani["quality_tier"], "formal")
        self.assertEqual(
            timpani["collaboration_review_status"], "untested"
        )
        self.assertIn("formal=单音色独立测试通过", palette["note"])
        self.assertIn("fixed", palette["pitch_mode_semantics"])

    def test_default_palette_restores_strings_clarinet_and_variant_hint(self):
        palette = self.m.list_instruments(limit=128)
        by_path = {
            item["instrument"]: item for item in palette["instruments"]
        }
        restored = {
            "管弦乐/弦乐组/小提琴",
            "管弦乐/弦乐组/中提琴",
            "管弦乐/弦乐组/大提琴",
            "管弦乐/弦乐组/弦乐合奏",
            "管弦乐/木管组/单簧管",
        }

        self.assertEqual(palette["instrument_scope"], "formal")
        self.assertEqual(palette["count"], 103)
        self.assertEqual(
            sum(item["curated"] is True for item in palette["instruments"]),
            25,
        )
        self.assertTrue(restored.issubset(by_path))
        hint = by_path["管弦乐/弦乐组/小提琴"]["variant_hint"]
        self.assertIn("SOLO", hint)
        self.assertIn("SEC", hint)
        self.assertIn("sample_variant", hint)
        self.assertIsNone(by_path["键盘乐器/钢琴"]["variant_hint"])

    def test_default_discovery_page_is_bounded_for_mcp_context(self):
        palette = self.m.list_instruments()

        self.assertEqual(palette["detail_level"], "summary")
        self.assertEqual(palette["curation_state"], "available")
        self.assertEqual(palette["curated_count"], 25)
        self.assertEqual(palette["catalog_count"], 103)
        self.assertEqual(palette["matched_count"], 103)
        self.assertEqual(palette["count"], 32)
        self.assertEqual(palette["next_offset"], 32)
        self.assertTrue(palette["has_more"])
        self.assertLess(
            len(json.dumps(palette, ensure_ascii=False).encode("utf-8")),
            24 * 1024,
        )

    def test_public_scope_rejections_never_suggest_legacy_false_bypass(self):
        published = self.m.score_and_roster_format()
        score = published["example_score"]
        roster = json.loads(json.dumps(published["example_roster"]))
        roster["assignments"][0]["instrument"] = "测试工具/参考振荡器"

        results = (
            self.m.validate_project(score, roster),
            self.m.check_project_readiness(score, roster),
            self.m.locate(score, roster, at_seconds=0.0),
            self.m.render(score, roster),
        )
        for result in results:
            messages = [
                str(issue.get("message", ""))
                for issue in result.get("issues", [])
            ]
            messages.extend(str(item) for item in result.get("offenders", []))
            self.assertTrue(messages, result)
            self.assertNotIn("trusted_only=false", " ".join(messages))

    def test_explicit_curated_scope_retains_the_25_item_palette(self):
        palette = self.m.list_instruments(instrument_scope="curated")

        self.assertTrue(palette["ok"])
        self.assertEqual(palette["instrument_scope"], "curated")
        self.assertEqual(palette["count"], 25)
        self.assertTrue(
            all(item["curated"] is True for item in palette["instruments"])
        )

    def test_legacy_scope_aliases_remain_exact(self):
        self.assertEqual(
            self.m.list_instruments(trusted_only=False)["instrument_scope"],
            "formal",
        )
        self.assertEqual(
            self.m.list_instruments(trusted_only=True)["instrument_scope"],
            "curated",
        )
        conflict = self.m.list_instruments(
            trusted_only=True,
            instrument_scope="formal",
        )
        self.assertFalse(conflict["ok"])
        self.assertEqual(
            conflict["issues"][0]["code"],
            "instrument.scope_invalid",
        )

    def test_scope_conflicts_share_one_public_error_code_across_tools(self):
        arguments = {
            "trusted_only": True,
            "instrument_scope": "formal",
        }
        results = {
            "list": self.m.list_instruments(**arguments),
            "import": self.m.import_score_project("unused.mid", **arguments),
            "confirm": self.m.confirm_roster({}, {}, [], **arguments),
            "validate": self.m.validate_project({}, {}, **arguments),
            "readiness": self.m.check_project_readiness({}, {}, **arguments),
            "locate": self.m.locate({}, {}, at_seconds=0.0, **arguments),
            "render": self.m.render({}, {}, **arguments),
        }

        for tool, result in results.items():
            with self.subTest(tool=tool):
                code = result.get("code")
                if code is None:
                    code = result["issues"][0]["code"]
                self.assertEqual(code, "instrument.scope_invalid")

    def test_full_catalog_supports_bounded_summary_discovery(self):
        first = self.m.list_instruments(
            detail_level="summary",
            limit=10,
        )
        second = self.m.list_instruments(
            detail_level="summary",
            offset=10,
            limit=10,
        )

        self.assertEqual(first["catalog_count"], 103)
        self.assertEqual(first["matched_count"], 103)
        self.assertEqual(first["count"], 10)
        self.assertEqual(first["next_offset"], 10)
        self.assertTrue(first["has_more"])
        self.assertTrue(
            {
                item["instrument"] for item in first["instruments"]
            }.isdisjoint(
                item["instrument"] for item in second["instruments"]
            )
        )
        self.assertTrue(
            all(
                "range_profiles" not in item
                for item in first["instruments"]
            )
        )

    def test_catalog_filters_categories_routing_and_exact_detail(self):
        effects = self.m.list_instruments(
            category="环境与拟音",
            routing_class="effect",
            detail_level="summary",
        )
        percussion = self.m.list_instruments(
            routing_class="percussion",
            detail_level="summary",
        )
        clavichord = self.m.list_instruments(
            query="键盘乐器/击弦古钢琴",
            detail_level="full",
        )

        self.assertEqual(effects["matched_count"], 8)
        self.assertTrue(
            all(
                item["routing_class"] == "effect"
                for item in effects["instruments"]
            )
        )
        self.assertEqual(percussion["matched_count"], 27)
        self.assertEqual(
            sum(item["pitched"] for item in percussion["instruments"]),
            9,
        )
        self.assertEqual(clavichord["count"], 1)
        self.assertIn(
            "articulation_range_contracts",
            clavichord["instruments"][0],
        )

    def test_catalog_rejects_invalid_pagination(self):
        result = self.m.list_instruments(limit=0)

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["issues"][0]["code"],
            "instrument_catalog.query_invalid",
        )

    def test_default_palette_accepts_piano_and_violin_roster(self):
        roster = {
            "assignments": [
                {
                    "part": "piano",
                    "executor_id": "钢琴",
                    "instrument": "键盘乐器/钢琴",
                },
                {
                    "part": "violin",
                    "executor_id": "小提琴",
                    "instrument": "管弦乐/弦乐组/小提琴",
                },
            ]
        }
        self.assertEqual(
            self.m._roster_instrument_problems(roster, trusted_only=True),
            [],
        )

    def test_collaboration_status_is_contextual_without_generic_warning(self):
        palette = self.m.list_instruments(trusted_only=False)
        self.assertGreater(palette["count"], 0)
        self.assertTrue(
            all(
                item["collaboration_review_status"] == "untested"
                for item in palette["instruments"]
            )
        )

        sitar = self.m._caps()["世界乐器/西塔琴"]
        cello = self.m._caps()["管弦乐/弦乐组/大提琴"]
        roster = SimpleNamespace(
            executors=(
                SimpleNamespace(
                    capability=sitar,
                    role=SimpleNamespace(prominence="foreground"),
                ),
                SimpleNamespace(
                    capability=cello,
                    role=SimpleNamespace(prominence="background"),
                ),
            )
        )
        warnings = self.m._collaboration_warnings(roster)
        self.assertTrue(any("西塔琴" in item for item in warnings))
        self.assertTrue(any("大提琴" in item for item in warnings))
        self.assertFalse(any("单音色" in item for item in warnings))
        single = SimpleNamespace(
            executors=(SimpleNamespace(capability=sitar, role=None),)
        )
        self.assertEqual(self.m._collaboration_warnings(single), [])

    def test_soundfont_local_compatibility_is_never_listed(self):
        piano = self.m._caps()["键盘乐器/击弦古钢琴"]
        local = replace(
            piano,
            relative_path="本机兼容/SoundFont",
            name="SoundFont",
            implementation_type="soundfont",
            license_status="approved",
        )
        previous = self.m._caps_cache
        try:
            self.m._caps_cache = {local.relative_path: local}
            palette = self.m.list_instruments(trusted_only=False)
        finally:
            self.m._caps_cache = previous

        self.assertEqual(palette["count"], 0)
        self.assertEqual(palette["instruments"], [])

    def test_declared_range_profiles_are_exposed_without_bypassing_license(self):
        violin = self.m._caps()["管弦乐/弦乐组/小提琴"]
        visible = replace(
            violin,
            relative_path="测试/范围合同",
            name="测试范围合同",
            license_status="approved",
        )
        previous = self.m._caps_cache
        try:
            self.m._caps_cache = {visible.relative_path: visible}
            item = self.m.list_instruments(
                trusted_only=False,
                detail_level="full",
            )["instruments"][0]
        finally:
            self.m._caps_cache = previous

        self.assertEqual(item["range_contract_status"], "declared_profiles")
        self.assertEqual(len(item["range_profiles"]), 1)
        profile = item["range_profiles"][0]
        self.assertEqual(
            profile["render_quality"]["current_high_quality_render_ranges"],
            [[55.0, 94.0]],
        )
        self.assertEqual(
            profile["physical"]["extended_ranges"],
            [[95.0, 105.0]],
        )


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class RangeDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        from tianlai import mcp_server
        self.m = mcp_server

    def test_summary_counts_all_contracts_and_limits_attention_examples(self):
        safe = {
            "status": "contract_candidate_unverified",
            "profile_id": "core",
            "legacy_covered": True,
        }
        risky = {
            "status": "outside_candidate_high_quality",
            "profile_id": "core",
            "legacy_covered": True,
        }
        traces = [
            {
                "小节": 1,
                "拍": float(index + 1),
                "音": f"N{index}",
                "推导": {"音域合同": risky if index < 18 else safe},
            }
            for index in range(20)
        ]
        plan = SimpleNamespace(
            expression=SimpleNamespace(range_mode="compatibility"),
            parts=(
                SimpleNamespace(
                    executor=SimpleNamespace(executor_id="violin"),
                    trace=tuple(traces),
                ),
            ),
        )

        summary = self.m._range_diagnostic_summary(plan)

        self.assertEqual(summary["attention_count"], 18)
        self.assertEqual(len(summary["attention_examples"]), 16)
        self.assertEqual(
            summary["status_counts"],
            {
                "contract_candidate_unverified": 2,
                "outside_candidate_high_quality": 18,
            },
        )
        self.assertEqual(
            summary["attention_examples"][0]["executor"],
            "violin",
        )
        self.assertEqual(
            summary["by_executor"]["violin"]["status_counts"],
            {
                "contract_candidate_unverified": 2,
                "outside_candidate_high_quality": 18,
            },
        )
        self.assertEqual(
            summary["by_executor"]["violin"]["attention_count"],
            18,
        )
        self.assertEqual(
            len(
                summary["by_executor"]["violin"][
                    "attention_examples"
                ]
            ),
            8,
        )
        self.assertTrue(
            summary["by_executor"]["violin"][
                "attention_examples_truncated"
            ]
        )

    def test_range_summary_keeps_each_executor_visible(self):
        risky = {
            "status": "outside_candidate_high_quality",
            "profile_id": "core",
            "legacy_covered": True,
        }

        def part(executor_id, count):
            return SimpleNamespace(
                executor=SimpleNamespace(executor_id=executor_id),
                trace=tuple(
                    {
                        "小节": 1,
                        "拍": float(index + 1),
                        "音": f"N{index}",
                        "推导": {"音域合同": risky},
                    }
                    for index in range(count)
                ),
            )

        plan = SimpleNamespace(
            expression=SimpleNamespace(range_mode="compatibility"),
            parts=(part("first", 20), part("second", 2)),
        )

        summary = self.m._range_diagnostic_summary(plan)

        self.assertEqual(len(summary["attention_examples"]), 16)
        self.assertEqual(
            summary["by_executor"]["first"]["attention_count"],
            20,
        )
        self.assertEqual(
            summary["by_executor"]["second"]["attention_count"],
            2,
        )
        self.assertEqual(
            len(
                summary["by_executor"]["second"][
                    "attention_examples"
                ]
            ),
            2,
        )

    def test_render_rejects_unknown_range_mode_before_writing_audio(self):
        published = self.m.score_and_roster_format()
        result = self.m.render(
            published["example_score"],
            published["example_roster"],
            title="invalid-range-mode",
            range_mode="not-a-mode",
        )
        self.assertIn("error", result)
        self.assertIn("range_mode", result["error"])


@unittest.skipUnless(_HAS_MCP, "未安装 mcp,可选组件跳过")
class RenderAttributionReturnTest(unittest.TestCase):
    def setUp(self):
        from tianlai import mcp_server
        self.m = mcp_server

    def test_success_returns_machine_and_human_license_sidecars(self):
        fake_plan = SimpleNamespace(to_dict=lambda: {"title": "test"})
        fake_result = SimpleNamespace(
            receipt_path="render/渲染回执.json",
            post_render_check_path="render/渲染后自检.json",
            post_render_check={
                "format": "tianlai.post_render_check",
                "summary": {"status": "clear", "blocking_count": 0},
            },
            post_render_check_summary={
                "status": "clear",
                "blocking_count": 0,
            },
            license_sidecar_path="render/许可与署名.json",
            attribution_path="render/许可与署名.txt",
            duration_seconds=1.25,
            mix_peak=0.5,
            normalize_gain_db=0.0,
            stems=(),
            stem_cache={
                "requested": True,
                "active": True,
                "hits": 1,
                "misses": 0,
            },
        )
        temporary_root: Path | None = None
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(self.m, "OUTPUT_DIR", Path(temporary)),
            patch.object(
                self.m,
                "_roster_instrument_problems",
                return_value=[],
            ),
            patch.object(
                self.m,
                "_resolve_mcp_instrument_scope",
                return_value=("formal", {"测试/乐器"}),
            ),
            patch.object(self.m, "_caps", return_value={}),
            patch.object(
                self.m,
                "parse_score_document",
                return_value=SimpleNamespace(),
            ),
            patch.object(
                self.m,
                "validate_score_resource_limits",
            ),
            patch.object(
                self.m,
                "parse_roster_document",
                return_value=SimpleNamespace(),
            ),
            patch.object(self.m, "build_plan", return_value=fake_plan),
            patch.object(
                self.m,
                "validate_render_request_resource_limits",
                return_value={"status": "passed"},
            ),
            patch.object(
                self.m,
                "render_plan",
                return_value=fake_result,
            ) as render_plan,
            patch.object(
                self.m,
                "_collaboration_warnings",
                return_value=[],
            ),
            patch.object(
                self.m,
                "_range_diagnostic_summary",
                return_value={},
            ),
        ):
            temporary_root = Path(temporary)
            result = self.m.render(
                {"title": "test"},
                {"assignments": []},
                title="sidecar",
                hall=False,
            )
            render_plan.side_effect = RuntimeError(
                "渲染后自检未通过: render.expected_activity_silent"
            )
            failed = self.m.render(
                {"title": "test"},
                {"assignments": []},
                title="sidecar-failure",
                hall=False,
            )

        self.assertEqual(
            result["license_sidecar"],
            "render/许可与署名.json",
        )
        self.assertEqual(
            result["attribution_notice"],
            "render/许可与署名.txt",
        )
        self.assertEqual(
            result["post_render_check_path"],
            fake_result.post_render_check_path,
        )
        self.assertEqual(
            result["post_render_check"],
            fake_result.post_render_check,
        )
        self.assertEqual(
            result["post_render_check_summary"],
            fake_result.post_render_check_summary,
        )
        self.assertEqual(result["stem_cache"], fake_result.stem_cache)
        self.assertIsNotNone(temporary_root)
        assert temporary_root is not None
        self.assertEqual(
            render_plan.call_args.kwargs["stem_cache_directory"],
            temporary_root.parent / ".tianlai-cache" / "stems",
        )
        self.assertEqual(
            render_plan.call_args.kwargs["analysis_cache_directory"],
            temporary_root.parent / ".tianlai-cache" / "analysis",
        )
        self.assertFalse(
            render_plan.call_args.kwargs["refresh_stem_cache"]
        )
        self.assertTrue(
            result["resolved_render_options"]["use_stem_cache"]
        )
        self.assertEqual(failed["kind"], "tianlai.render_result")
        self.assertEqual(failed["schema_version"], 2)
        self.assertIs(failed["ok"], False)
        self.assertIn("render.expected_activity_silent", failed["error"])
        self.assertTrue(result["project_review"]["continuation_allowed"])
        self.assertEqual(result["project_review"]["blocking_count"], 0)
        self.assertEqual(
            result["project_review"]["binding"][
                "performance_plan_sha256"
            ],
            self.m.canonical_json_sha256(fake_plan.to_dict()),
        )


if __name__ == "__main__":
    unittest.main()
