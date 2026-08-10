from __future__ import annotations

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tianlai.capability import load_capabilities, read_capability
from tianlai.conductor import ResolvedNote, _check_playable


ROOT = Path(__file__).resolve().parents[1]


class BackendArticulationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_mixed_choir_exposes_both_backend_articulations(self) -> None:
        capability = self.capabilities["人声乐器/合唱啊声"]
        self.assertEqual(capability.articulations, ("normal", "sustain"))
        self.assertTrue(capability.supports("normal"))
        self.assertTrue(capability.supports("sustain"))
        self.assertIn("tianlai.vpo_specials", capability.articulation_source)

    def test_vpo_percussion_profiles_expose_their_real_vocabularies(self) -> None:
        expected = {
            "三角铁": {"muted", "open", "roll"},
            "定音鼓": {"hit", "roll"},
            "小军鼓": {
                "alternating",
                "hit",
                "kit2_left",
                "kit2_right",
                "left",
                "right",
                "roll",
                "roll_looped",
                "tap",
            },
            "木琴": {"hit"},
            "木鱼": {"high", "low"},
            "管弦大鼓": {"drum_1", "drum_2"},
            "管弦钹": {
                "crash",
                "crescendo_long",
                "crescendo_medium",
                "crescendo_short",
                "piatti",
                "piatti_high",
                "roll_alt",
                "roll_soft",
                "suspended_high",
                "suspended_hit",
            },
            "管钟": {"damped", "open"},
            "钟琴": {"hit"},
            "颤音琴": {
                "bowed",
                "damped",
                "hard_damped",
                "hard_open",
                "open",
            },
        }
        for name, articulations in expected.items():
            with self.subTest(instrument=name):
                capability = self.capabilities[f"管弦乐/打击乐组/{name}"]
                self.assertEqual(set(capability.articulations), articulations)
                if name in {"定音鼓", "颤音琴"}:
                    self.assertEqual(
                        capability.articulation_source,
                        "manifest.articulations",
                    )
                else:
                    self.assertIn(
                        "tianlai.vpo_percussion.PERCUSSION_PROFILES",
                        capability.articulation_source,
                    )
                for articulation in articulations:
                    self.assertTrue(capability.supports(articulation))

    def test_license_status_is_part_of_the_capability_contract(self) -> None:
        grandfathered = self.capabilities["管弦乐/弦乐组/小提琴"]
        tubular_bells = self.capabilities["管弦乐/打击乐组/管钟"]
        approved = self.capabilities["键盘乐器/钢琴"]
        project_authored = self.capabilities["电子乐器/合成器主音"]

        self.assertEqual(grandfathered.license_status, "grandfathered")
        self.assertEqual(
            grandfathered.to_dict()["license_status"],
            "grandfathered",
        )
        self.assertEqual(tubular_bells.license_status, "approved")
        self.assertEqual(approved.license_status, "approved")
        self.assertEqual(project_authored.license_status, "approved")
        self.assertEqual(
            project_authored.to_dict()["license_status"],
            "approved",
        )

    def test_formal_and_collaboration_are_independent_capability_axes(self) -> None:
        sound_capabilities = [
            capability
            for capability in self.capabilities.values()
            if capability.quality_tier is not None
        ]
        self.assertEqual(len(sound_capabilities), 103)
        for capability in sound_capabilities:
            with self.subTest(instrument=capability.relative_path):
                self.assertEqual(capability.quality_tier, "formal")
                self.assertEqual(
                    capability.collaboration_review_status,
                    "untested",
                )
                document = capability.to_dict()
                self.assertEqual(
                    document["collaboration_review_status"],
                    "untested",
                )

    def test_catalog_routing_classes_cover_every_formal_entry(self) -> None:
        formal = [
            capability
            for capability in self.capabilities.values()
            if capability.quality_tier == "formal"
        ]
        counts = {
            routing_class: sum(
                capability.routing_class == routing_class
                for capability in formal
            )
            for routing_class in ("instrument", "percussion", "effect")
        }

        self.assertEqual(
            counts,
            {"instrument": 68, "percussion": 27, "effect": 8},
        )
        self.assertEqual(
            sum(
                capability.routing_class == "percussion"
                and capability.pitched
                for capability in formal
            ),
            9,
        )


class PlayableRangeContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_ignore_pitch_checks_selector_keys_while_fixed_accepts_any_key(
        self,
    ) -> None:
        high_tom = self.capabilities["现代鼓组/高音通鼓"]
        kick = self.capabilities["现代鼓组/底鼓"]

        self.assertEqual(high_tom.pitch_mode, "ignore")
        self.assertTrue(high_tom.covers(60.0))
        self.assertTrue(high_tom.covers(65.0))
        self.assertFalse(high_tom.covers(59.0))
        self.assertFalse(high_tom.covers(66.0))
        self.assertEqual(kick.pitch_mode, "fixed")
        self.assertTrue(kick.covers(0.0))
        self.assertTrue(kick.covers(127.0))

    def test_bagpipe_chanter_and_independent_drones_are_not_one_scale(self) -> None:
        capability = self.capabilities["世界乐器/风笛"]
        self.assertEqual(capability.note_min, 43.0)
        self.assertEqual(capability.note_max, 81.0)
        self.assertEqual(
            capability.playable_ranges,
            ((43.0, 43.0), (55.0, 55.0), (64.0, 81.0)),
        )
        for note in (43.0, 55.0, 64.0, 81.0):
            with self.subTest(note=note):
                self.assertTrue(capability.covers(note))
        for note in (42.0, 44.0, 54.0, 56.0, 63.0, 82.0):
            with self.subTest(note=note):
                self.assertFalse(capability.covers(note))
        self.assertEqual(
            capability.to_dict()["playable_ranges"],
            [[43.0, 43.0], [55.0, 55.0], [64.0, 81.0]],
        )
        self.assertEqual(
            capability.ranges_for("chanter"),
            ((64.0, 81.0),),
        )
        self.assertEqual(
            capability.ranges_for("drone_low"),
            ((43.0, 43.0),),
        )
        self.assertEqual(
            capability.ranges_for("drone_high"),
            ((55.0, 55.0),),
        )

    def test_articulation_specific_ranges_match_runtime_manifests(self) -> None:
        timpani = self.capabilities["管弦乐/打击乐组/定音鼓"]
        vibraphone = self.capabilities["管弦乐/打击乐组/颤音琴"]

        self.assertEqual(
            dict(timpani.articulation_playable_ranges),
            {
                "hit": ((38.0, 59.0),),
                "roll": ((41.0, 55.0),),
            },
        )
        self.assertEqual(
            dict(vibraphone.articulation_playable_ranges),
            {
                "bowed": ((57.0, 89.0),),
                "damped": ((53.0, 89.0),),
                "hard_damped": ((53.0, 89.0),),
                "hard_open": ((53.0, 89.0),),
                "open": ((53.0, 89.0),),
            },
        )

        for note in (38.0, 59.0):
            self.assertTrue(timpani.covers(note, "hit"))
            self.assertFalse(timpani.covers(note, "roll"))
        for note in (41.0, 55.0):
            self.assertTrue(timpani.covers(note, "roll"))
        for note in (40.0, 56.0):
            self.assertFalse(timpani.covers(note, "roll"))

        self.assertTrue(vibraphone.covers(53.0, "open"))
        self.assertFalse(vibraphone.covers(53.0, "bowed"))
        self.assertFalse(vibraphone.covers(56.0, "bowed"))
        for note in (57.0, 89.0):
            self.assertTrue(vibraphone.covers(note, "bowed"))
        self.assertFalse(vibraphone.covers(90.0, "bowed"))

        self.assertEqual(
            timpani.to_dict()["articulation_playable_ranges"]["roll"],
            [[41.0, 55.0]],
        )

    def test_conductor_reports_segmented_ranges_when_rejecting_the_gap(self) -> None:
        capability = self.capabilities["世界乐器/风笛"]
        executor = SimpleNamespace(part_id="bagpipe", transpose=0)
        note = ResolvedNote(
            index=0,
            start_quarter=0.0,
            duration_quarters=1.0,
            midi=60.0,
            dynamic="mf",
            articulation=None,
            bar=2,
            beat=3.0,
        )
        with self.assertRaisesRegex(
            ValueError,
            "可演奏分段 G2~G2、G3~G3、E4~A5",
        ):
            _check_playable(executor, note, capability)

    def test_outer_bounds_are_derived_for_a_segmented_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "segmented"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "segmented",
                        "type": "oscillator",
                        "playable_ranges": [[10, 20], [30.5, 40]],
                    }
                ),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)
            self.assertEqual(capability.note_min, 10.0)
            self.assertEqual(capability.note_max, 40.0)
            self.assertTrue(capability.covers(30.5))
            self.assertFalse(capability.covers(25.0))

    def test_generic_articulation_ranges_support_non_sfz_backends(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "generic"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "generic",
                        "type": "oscillator",
                        "note_min": 40,
                        "note_max": 80,
                        "allowed_articulations": ["sustain", "accent"],
                        "default_articulation": "sustain",
                        "articulation_playable_ranges": {
                            "accent": [[50, 70]],
                        },
                    }
                ),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)

        self.assertEqual(
            capability.ranges_for("accent"),
            ((50.0, 70.0),),
        )
        self.assertEqual(
            capability.ranges_for("sustain"),
            ((40.0, 80.0),),
        )
        self.assertFalse(capability.covers(49.0, "accent"))
        self.assertTrue(capability.covers(49.0, "sustain"))

    def test_duration_articulation_rules_are_explicit_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "duration-rule"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest = {
                "name": "duration rule",
                "type": "oscillator",
                "note_min": 40,
                "note_max": 80,
                "allowed_articulations": ["sustain", "accent"],
                "default_articulation": "sustain",
                "duration_articulation_rules": [
                    {
                        "rule_id": "neutral-short-v1",
                        "source_articulation": "sustain",
                        "target_articulation": "accent",
                        "below_seconds": 0.75,
                    }
                ],
            }
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)
            self.assertEqual(len(capability.duration_articulation_rules), 1)
            self.assertEqual(
                capability.duration_articulation_rules[0].target_articulation,
                "accent",
            )
            self.assertEqual(
                capability.to_dict()["duration_articulation_rules"][0][
                    "rule_id"
                ],
                "neutral-short-v1",
            )

            for index, bad_rule in enumerate(
                (
                    {
                        "rule_id": "bad-source",
                        "source_articulation": "accent",
                        "target_articulation": "sustain",
                        "below_seconds": 0.75,
                    },
                    {
                        "rule_id": "bad-target",
                        "source_articulation": "sustain",
                        "target_articulation": "missing",
                        "below_seconds": 0.75,
                    },
                    {
                        "rule_id": "bad-threshold",
                        "source_articulation": "sustain",
                        "target_articulation": "accent",
                        "below_seconds": 0,
                    },
                )
            ):
                with self.subTest(index=index):
                    broken = dict(manifest)
                    broken["duration_articulation_rules"] = [bad_rule]
                    manifest_path.write_text(
                        json.dumps(broken),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "duration articulation|duration_articulation",
                    ):
                        read_capability(manifest_path, root=root)

    def test_onset_overlap_policy_defaults_conservatively_and_loads_explicitly(
        self,
    ) -> None:
        for declared, expected in (
            (None, "conservative"),
            ("polyphonic_independent", "polyphonic_independent"),
            ("monophonic_connected", "monophonic_connected"),
        ):
            with self.subTest(declared=declared):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    directory = root / "policy"
                    directory.mkdir()
                    manifest = {
                        "name": "policy",
                        "type": "oscillator",
                        "note_min": 40,
                        "note_max": 80,
                    }
                    if declared is not None:
                        manifest["onset_overlap_policy"] = declared
                    manifest_path = directory / "乐器.json"
                    manifest_path.write_text(
                        json.dumps(manifest),
                        encoding="utf-8",
                    )

                    capability = read_capability(manifest_path, root=root)

                self.assertEqual(capability.onset_overlap_policy, expected)
                self.assertEqual(
                    capability.to_dict()["onset_overlap_policy"],
                    expected,
                )

    def test_invalid_onset_overlap_policy_is_rejected(self) -> None:
        for index, policy in enumerate(("guess_from_name", 1, None)):
            with self.subTest(policy=policy):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    directory = root / f"invalid-policy-{index}"
                    directory.mkdir()
                    manifest_path = directory / "乐器.json"
                    manifest_path.write_text(
                        json.dumps(
                            {
                                "name": "invalid policy",
                                "type": "oscillator",
                                "note_min": 40,
                                "note_max": 80,
                                "onset_overlap_policy": policy,
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "onset_overlap_policy",
                    ):
                        read_capability(manifest_path, root=root)

    def test_articulation_ranges_reject_unknown_conflicting_or_unbounded_data(
        self,
    ) -> None:
        invalid_manifests = (
            {
                "allowed_articulations": ["sustain"],
                "articulation_playable_ranges": {"unknown": [[40, 50]]},
            },
            {
                "allowed_articulations": ["sustain"],
                "articulation_playable_ranges": {"sustain": [[39, 50]]},
            },
            {
                "allowed_articulations": ["sustain"],
                "playable_ranges": [[40, 50], [60, 70]],
                "articulation_playable_ranges": {"sustain": [[45, 65]]},
            },
            {
                "articulations": {
                    "sustain": {
                        "sfz": "sustain.sfz",
                        "playable_ranges": [[40, 60]],
                    }
                },
                "default_articulation": "sustain",
                "articulation_playable_ranges": {"sustain": [[41, 60]]},
            },
        )
        for index, addition in enumerate(invalid_manifests):
            with self.subTest(index=index):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    directory = root / f"invalid-articulation-{index}"
                    directory.mkdir()
                    manifest = {
                        "name": "invalid",
                        "type": "oscillator",
                        "note_min": 40,
                        "note_max": 80,
                        **addition,
                    }
                    manifest_path = directory / "乐器.json"
                    manifest_path.write_text(
                        json.dumps(manifest),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        ValueError,
                        "articulation|playable",
                    ):
                        read_capability(manifest_path, root=root)

    def test_malformed_or_overlapping_ranges_are_rejected(self) -> None:
        invalid_ranges = (
            [],
            [[20, 10]],
            [[10, 20], [20, 30]],
            [[10, 20], [5, 8]],
            [[10, float("inf")]],
            [[True, 20]],
        )
        for index, playable_ranges in enumerate(invalid_ranges):
            with self.subTest(playable_ranges=playable_ranges):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    directory = root / f"invalid-{index}"
                    directory.mkdir()
                    manifest_path = directory / "乐器.json"
                    manifest_path.write_text(
                        json.dumps(
                            {
                                "name": "invalid",
                                "type": "oscillator",
                                "playable_ranges": playable_ranges,
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "playable_ranges"):
                        read_capability(manifest_path, root=root)


if __name__ == "__main__":
    unittest.main()
