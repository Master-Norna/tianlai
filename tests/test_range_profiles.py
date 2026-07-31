from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from collections.abc import Callable

from tianlai.capability import InstrumentCapability, read_capability
from tianlai.conductor import ExpressionSettings, build_plan
from tianlai.roster import parse_roster_document
from tianlai.score import parse_score_document


def _profile(
    profile_id: str,
    variant: str,
    articulation: str,
    *,
    hard: list[list[float]],
    idiomatic: list[list[float]] | None,
    extended: list[list[float]] | None,
    high_quality: list[list[float]] | None,
    status: str,
) -> dict:
    return {
        "profile_id": profile_id,
        "selector": {
            "resolved_runtime_configuration": {
                "sample_variant": variant,
            },
            "final_articulation": articulation,
        },
        "physical": {
            "hard_playable_ranges": hard,
            "idiomatic_ranges": idiomatic,
            "extended_ranges": extended,
        },
        "render_quality": {
            "current_high_quality_render_ranges": high_quality,
            "status": status,
            "approval_evidence": None,
        },
    }


def _manifest() -> dict:
    return {
        "name": "profiled",
        "type": "oscillator",
        "note_min": 40,
        "note_max": 80,
        "playable_ranges": [[40, 80]],
        "allowed_articulations": ["sustain", "staccato"],
        "default_articulation": "sustain",
        "sample_variant": "SOLO",
        "range_profiles": {
            "schema_version": 1,
            "pitch_unit": "concert_midi_note",
            "unknown_value_semantics": "null_means_unreviewed",
            "fallback_policy": (
                "reject_unknown_configuration_or_final_articulation"
            ),
            "profiles": [
                _profile(
                    "solo-sustain",
                    "SOLO",
                    "sustain",
                    hard=[[40, 50], [60, 80]],
                    idiomatic=[[45, 50], [60, 68]],
                    extended=[[40, 44], [69, 80]],
                    high_quality=[[45, 50], [60, 68]],
                    status="contract_candidate",
                ),
                _profile(
                    "sec-sustain",
                    "SEC",
                    "sustain",
                    hard=[[40, 80]],
                    idiomatic=None,
                    extended=None,
                    high_quality=[[40, 80]],
                    status="contract_candidate",
                ),
                _profile(
                    "solo-staccato",
                    "SOLO",
                    "staccato",
                    hard=[[40, 70]],
                    idiomatic=[[45, 65]],
                    extended=[[40, 44], [66, 70]],
                    high_quality=None,
                    status="pending",
                ),
                _profile(
                    "sec-staccato",
                    "SEC",
                    "staccato",
                    hard=[[40, 70]],
                    idiomatic=[[45, 65]],
                    extended=[[40, 44], [66, 70]],
                    high_quality=[],
                    status="rejected",
                ),
            ],
        },
    }


class _TemporaryCapability:
    def __init__(self, manifest: dict) -> None:
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        directory = self.root / "profiled"
        directory.mkdir()
        self.manifest_path = directory / "乐器.json"
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            self.capability = read_capability(
                self.manifest_path,
                root=self.root,
            )
        except BaseException:
            self._temporary_directory.cleanup()
            raise

    def close(self) -> None:
        self._temporary_directory.cleanup()


class RangeProfileParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _TemporaryCapability(_manifest())
        self.capability = self.fixture.capability

    def tearDown(self) -> None:
        self.fixture.close()

    def test_exact_resolved_configuration_selects_the_profile(self) -> None:
        base = self.capability.range_profile_for("sustain")
        section = self.capability.range_profile_for(
            "sustain",
            overrides={"sample_variant": "SEC"},
        )
        self.assertEqual(base.profile_id, "solo-sustain")
        self.assertEqual(section.profile_id, "sec-sustain")

        # An extra sound-affecting override is part of the resolved
        # configuration.  It cannot silently borrow the base profile.
        self.assertIsNone(
            self.capability.range_profile_for(
                "sustain",
                overrides={"release_seconds": 0.2},
            )
        )

    def test_non_contiguous_ranges_and_all_four_layers_are_reported(self) -> None:
        inside = self.capability.evaluate_range_profile(45, "sustain")
        self.assertEqual(inside.status, "contract_candidate_unverified")
        self.assertFalse(inside.verified)
        self.assertTrue(inside.hard_covered)
        self.assertTrue(inside.idiomatic_covered)
        self.assertFalse(inside.extended_covered)
        self.assertTrue(inside.high_quality_covered)

        gap = self.capability.evaluate_range_profile(55, "sustain")
        self.assertEqual(gap.status, "outside_hard_playable_range")
        self.assertFalse(gap.hard_covered)
        self.assertFalse(gap.high_quality_covered)

        extended = self.capability.evaluate_range_profile(75, "sustain")
        self.assertEqual(extended.status, "outside_candidate_high_quality")
        self.assertTrue(extended.hard_covered)
        self.assertFalse(extended.idiomatic_covered)
        self.assertTrue(extended.extended_covered)
        self.assertFalse(extended.high_quality_covered)

    def test_pending_and_unknown_exact_selectors_fail_closed(self) -> None:
        pending = self.capability.evaluate_range_profile(55, "staccato")
        self.assertEqual(pending.status, "quality_pending")
        self.assertFalse(pending.verified)

        unknown = self.capability.evaluate_range_profile(
            55,
            "sustain",
            overrides={"release_seconds": 0.2},
            mode="strict_hq",
        )
        self.assertEqual(unknown.status, "profile_not_found")
        self.assertFalse(unknown.verified)
        self.assertEqual(
            dict(unknown.runtime_configuration),
            {"release_seconds": 0.2, "sample_variant": "SOLO"},
        )

    def test_every_current_pitched_status_is_unverified_in_strict_hq(
        self,
    ) -> None:
        cases = (
            ("sustain", None, "contract_candidate_unverified"),
            ("staccato", None, "quality_pending"),
            ("staccato", {"sample_variant": "SEC"}, "quality_rejected"),
        )
        for articulation, overrides, status in cases:
            with self.subTest(status=status):
                evaluation = self.capability.evaluate_range_profile(
                    55 if articulation == "staccato" else 45,
                    articulation,
                    overrides=overrides,
                    mode="strict_hq",
                )
                self.assertEqual(evaluation.status, status)
                self.assertTrue(evaluation.applicable)
                self.assertFalse(evaluation.verified)

    def test_declared_profiles_are_candidates_not_claimed_as_verified(self) -> None:
        payload = self.capability.to_dict()
        self.assertEqual(payload["range_contract_status"], "declared_profiles")
        self.assertEqual(
            payload["range_profiles"][0]["render_quality"]["status"],
            "contract_candidate",
        )
        self.assertIsNone(
            payload["range_profiles"][0]["render_quality"][
                "approval_evidence"
            ]
        )

    def test_forged_approved_status_and_evidence_are_rejected(self) -> None:
        for mutation, message in (
            (
                lambda profile: profile["render_quality"].update(
                    {"status": "approved"}
                ),
                "status must be one of",
            ),
            (
                lambda profile: profile["render_quality"].update(
                    {
                        "approval_evidence": {
                            "path": "fake.json",
                            "sha256": "0" * 64,
                        }
                    }
                ),
                "approval_evidence must remain null",
            ),
        ):
            with self.subTest(message=message):
                manifest = _manifest()
                mutation(manifest["range_profiles"]["profiles"][0])
                with self.assertRaisesRegex(ValueError, message):
                    fixture = _TemporaryCapability(manifest)
                    fixture.close()

    def test_malformed_or_cross_contract_profiles_are_rejected(self) -> None:
        cases: list[tuple[str, Callable[[dict], None]]] = [
            (
                "same resolved runtime configuration keys",
                lambda manifest: manifest["range_profiles"]["profiles"][1][
                    "selector"
                ]["resolved_runtime_configuration"].update(
                    {"release_seconds": 0.2}
                ),
            ),
            (
                "undeclared final articulation",
                lambda manifest: manifest["range_profiles"]["profiles"][0][
                    "selector"
                ].update({"final_articulation": "invented"}),
            ),
            (
                "existing articulation/global playable ranges",
                lambda manifest: manifest["range_profiles"]["profiles"][0][
                    "physical"
                ].update({"hard_playable_ranges": [[20, 80]]}),
            ),
            (
                "ordered, non-overlapping",
                lambda manifest: manifest["range_profiles"]["profiles"][0][
                    "physical"
                ].update({"hard_playable_ranges": [[40, 60], [50, 80]]}),
            ),
            (
                "contained in hard_playable_ranges",
                lambda manifest: manifest["range_profiles"]["profiles"][0][
                    "render_quality"
                ].update(
                    {"current_high_quality_render_ranges": [[40, 55]]}
                ),
            ),
        ]
        for message, mutation in cases:
            with self.subTest(message=message):
                manifest = _manifest()
                mutation(manifest)
                with self.assertRaisesRegex(ValueError, message):
                    fixture = _TemporaryCapability(manifest)
                    fixture.close()


class RangeProfileConductorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = _TemporaryCapability(_manifest())

    def tearDown(self) -> None:
        self.fixture.close()

    def _plan(
        self,
        capability: InstrumentCapability,
        *,
        pitch: float = 45,
        articulation: str | None = None,
        articulation_map: dict[str, str] | None = None,
        overrides: dict | None = None,
        range_mode: str = "compatibility",
    ):
        note = {
            "bar": 1,
            "beat": 1,
            "duration_beats": 1,
            "pitch": pitch,
        }
        if articulation is not None:
            note["articulation"] = articulation
        score = parse_score_document(
            {
                "title": "range contract",
                "tempo_map": [
                    {
                        "bar": 1,
                        "bpm": 120,
                        "beats_per_bar": 4,
                        "beat_unit": 4,
                    }
                ],
                "parts": [{"id": "part", "notes": [note]}],
            }
        )
        assignment = {
            "part": "part",
            "instrument": capability.relative_path,
            "articulation_auto": False,
        }
        if articulation_map is not None:
            assignment["articulation_map"] = articulation_map
        if overrides is not None:
            assignment["overrides"] = overrides
        roster = parse_roster_document(
            {"assignments": [assignment]},
            {capability.relative_path: capability},
        )
        return build_plan(
            score,
            roster,
            ExpressionSettings.from_dict({"range_mode": range_mode}),
        )

    def _range_trace(self, plan) -> dict:
        return plan.parts[0].trace[0]["推导"]["音域合同"]

    def test_compatibility_preserves_legacy_rendering_and_adds_diagnostics(
        self,
    ) -> None:
        # MIDI 55 is accepted by the legacy 40..80 envelope, but is in the
        # deliberate gap of the candidate hard range.
        plan = self._plan(self.fixture.capability, pitch=55)
        diagnostic = self._range_trace(plan)
        self.assertEqual(diagnostic["mode"], "compatibility")
        self.assertEqual(diagnostic["status"], "outside_hard_playable_range")
        self.assertTrue(diagnostic["legacy_covered"])
        self.assertFalse(diagnostic["verified"])
        self.assertEqual(len(plan.parts[0].performance["events"]), 3)

    def test_unmigrated_manifest_is_visible_but_compatible_by_default(
        self,
    ) -> None:
        manifest = _manifest()
        del manifest["range_profiles"]
        fixture = _TemporaryCapability(manifest)
        self.addCleanup(fixture.close)

        plan = self._plan(fixture.capability)
        diagnostic = self._range_trace(plan)
        self.assertEqual(diagnostic["status"], "manifest_unmigrated")
        self.assertFalse(diagnostic["verified"])
        self.assertEqual(
            fixture.capability.to_dict()["range_contract_status"],
            "unmigrated",
        )

    def test_strict_hq_rejects_candidates_and_unmigrated_manifests(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "strict_hq.*contract_candidate_unverified",
        ):
            self._plan(
                self.fixture.capability,
                pitch=45,
                range_mode="strict_hq",
            )

        manifest = _manifest()
        del manifest["range_profiles"]
        fixture = _TemporaryCapability(manifest)
        self.addCleanup(fixture.close)
        with self.assertRaisesRegex(ValueError, "strict_hq.*manifest_unmigrated"):
            self._plan(
                fixture.capability,
                pitch=45,
                range_mode="strict_hq",
            )

    def test_final_backend_articulation_selects_the_profile(self) -> None:
        plan = self._plan(
            self.fixture.capability,
            pitch=55,
            articulation="swell",
            articulation_map={"swell": "staccato"},
        )
        diagnostic = self._range_trace(plan)
        self.assertEqual(diagnostic["final_articulation"], "staccato")
        self.assertEqual(diagnostic["profile_id"], "solo-staccato")
        self.assertEqual(diagnostic["status"], "quality_pending")

    def test_roster_override_selects_the_final_runtime_profile(self) -> None:
        plan = self._plan(
            self.fixture.capability,
            pitch=55,
            overrides={"sample_variant": "SEC"},
        )
        diagnostic = self._range_trace(plan)
        self.assertEqual(
            diagnostic["resolved_runtime_configuration"],
            {"sample_variant": "SEC"},
        )
        self.assertEqual(diagnostic["profile_id"], "sec-sustain")
        self.assertEqual(diagnostic["status"], "contract_candidate_unverified")

    def test_range_mode_is_independent_and_validated(self) -> None:
        settings = ExpressionSettings.from_dict(
            {"mode": "strict", "range_mode": "strict_hq"}
        )
        self.assertEqual(settings.mode, "strict")
        self.assertEqual(settings.range_mode, "strict_hq")
        self.assertEqual(settings.to_dict()["range_mode"], "strict_hq")
        with self.assertRaisesRegex(ValueError, "range_mode"):
            ExpressionSettings.from_dict({"range_mode": "guess"})


if __name__ == "__main__":
    unittest.main()
