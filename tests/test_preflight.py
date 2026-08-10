from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import unittest

from tianlai.capability import load_capabilities
from tianlai.conductor import ExpressionSettings, build_plan
from tianlai.preflight import (
    enforce_roster_availability,
    roster_availability_problems,
)
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document


ROOT = Path(__file__).resolve().parents[1]


class RosterAvailabilityPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")
        cls.quarantined_capabilities = dict(cls.capabilities)
        violin_path = "管弦乐/弦乐组/小提琴"
        cls.quarantined_capabilities[violin_path] = replace(
            cls.capabilities[violin_path],
            license_status="quarantined",
        )

    def _roster(self, instrument: str, *, quarantined: bool = False):
        capabilities = (
            self.quarantined_capabilities
            if quarantined
            else self.capabilities
        )
        return parse_roster_document(
            {"assignments": [{"part": "part", "instrument": instrument}]},
            capabilities,
        )

    def test_quarantine_is_hard_even_when_trusted_quality_filter_is_open(self) -> None:
        roster = self._roster(
            "管弦乐/弦乐组/小提琴",
            quarantined=True,
        )
        problems = roster_availability_problems(
            roster,
            trusted_only=True,
            trusted_instruments={"管弦乐/弦乐组/小提琴"},
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("许可证据已隔离", problems[0])
        self.assertIn("不能放开", problems[0])
        with self.assertRaisesRegex(ValueError, "许可证据已隔离"):
            enforce_roster_availability(roster)

    def test_grandfathered_instrument_remains_available(self) -> None:
        roster = self._roster("管弦乐/弦乐组/低音提琴")
        self.assertEqual(roster_availability_problems(roster), ())
        enforce_roster_availability(roster)

    def test_cc0_ganjo_replacement_is_publicly_available(self) -> None:
        roster = self._roster("世界乐器/班卓琴")
        self.assertEqual(
            roster_availability_problems(
                roster,
                trusted_only=False,
                trusted_instruments=(),
            ),
            (),
        )
        enforce_roster_availability(roster)

    def test_trusted_allowlist_is_an_optional_quality_filter(self) -> None:
        roster = self._roster("键盘乐器/击弦古钢琴")
        self.assertEqual(
            roster_availability_problems(
                roster,
                trusted_only=False,
                trusted_instruments=(),
            ),
            (),
        )
        problems = roster_availability_problems(
            roster,
            trusted_only=True,
            trusted_instruments=(),
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("不在当前调用方提供的允许乐器集合", problems[0])
        self.assertNotIn("trusted_only=false", problems[0])

    def test_requested_trusted_policy_without_allowlist_fails_closed(
        self,
    ) -> None:
        roster = self._roster("管弦乐/弦乐组/小提琴")
        problems = roster_availability_problems(
            roster,
            trusted_only=True,
            trusted_instruments=None,
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("fail-closed", problems[0])

    def test_local_soundfont_backend_never_enters_public_collaboration(
        self,
    ) -> None:
        path = "键盘乐器/击弦古钢琴"
        local_capabilities = dict(self.capabilities)
        local_capabilities[path] = replace(
            self.capabilities[path],
            implementation_type="soundfont",
            license_status="approved",
        )
        roster = parse_roster_document(
            {"assignments": [{"part": "part", "instrument": path}]},
            local_capabilities,
        )

        problems = roster_availability_problems(
            roster,
            trusted_only=False,
            trusted_instruments={path},
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("仅限显式本机兼容/测试", problems[0])
        self.assertIn("public/trusted", problems[0])

    def test_core_plan_rejects_quarantined_instrument(self) -> None:
        score = parse_score_document(
            {
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
                        "id": "part",
                        "notes": [
                            {
                                "bar": 1,
                                "beat": 1,
                                "duration_beats": 1,
                                "pitch": "G3",
                            }
                        ],
                    }
                ],
            }
        )
        roster = self._roster(
            "管弦乐/弦乐组/小提琴",
            quarantined=True,
        )
        with self.assertRaisesRegex(ValueError, "许可证据已隔离"):
            build_plan(
                score,
                roster,
                ExpressionSettings.from_dict({"mode": "strict"}),
            )

    def test_duplicate_kit_use_reports_one_canonical_instrument(self) -> None:
        roster = parse_roster_document(
            {
                "assignments": [
                    {
                        "part": "drums",
                        "kit": {
                            "C2": "管弦乐/弦乐组/小提琴",
                            "D2": "管弦乐/弦乐组/小提琴",
                        },
                    }
                ]
            },
            self.quarantined_capabilities,
        )
        problems = roster_availability_problems(roster)
        self.assertEqual(len(problems), 1)


if __name__ == "__main__":
    unittest.main()
