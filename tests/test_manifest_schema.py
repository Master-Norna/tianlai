from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "schemas" / "instrument.schema.json"
INSTRUMENT_ROOT = ROOT / "乐器"


class InstrumentManifestSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)
        cls.manifest_paths = sorted(INSTRUMENT_ROOT.rglob("乐器.json"))
        cls.manifests = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in cls.manifest_paths
        ]

    def _dedicated_fixture(self) -> dict[str, Any]:
        for manifest in self.manifests:
            if (
                manifest.get("type") == "dedicated_sfz"
                and "note_min" in manifest
                and "note_max" in manifest
            ):
                return copy.deepcopy(manifest)
        self.fail("没有可用于负例的 dedicated_sfz 清单")

    def _vpo_solo_string_fixture(self) -> dict[str, Any]:
        for manifest in self.manifests:
            if manifest.get("type") == "vpo_solo_string":
                return copy.deepcopy(manifest)
        self.fail("没有可用于负例的 vpo_solo_string 清单")

    def _fixture_by_type(self, instrument_type: str) -> dict[str, Any]:
        for manifest in self.manifests:
            if manifest.get("type") == instrument_type:
                return copy.deepcopy(manifest)
        self.fail(f"没有 {instrument_type} 清单")

    def assert_manifest_invalid(self, manifest: dict[str, Any]) -> None:
        errors = list(self.validator.iter_errors(manifest))
        self.assertTrue(errors, "故意损坏的清单不应通过 schema")

    def test_all_instrument_manifests_validate_with_draft_2020_12(self) -> None:
        self.assertGreaterEqual(
            len(self.manifest_paths),
            104,
            "全量契约门禁意外少于当前 104 份乐器清单",
        )
        failures: list[str] = []
        for path, manifest in zip(self.manifest_paths, self.manifests, strict=True):
            for error in self.validator.iter_errors(manifest):
                location = "/".join(str(item) for item in error.absolute_path) or "<root>"
                relative_path = path.relative_to(ROOT)
                failures.append(f"{relative_path}:{location}: {error.message}")

        self.assertFalse(failures, "\n" + "\n".join(failures))

    def test_every_live_backend_type_has_a_schema_branch(self) -> None:
        live_types = {str(manifest["type"]) for manifest in self.manifests}
        schema_types = {
            str(branch["properties"]["type"]["const"])
            for branch in self.schema["oneOf"]
        }
        self.assertTrue(live_types <= schema_types, sorted(live_types - schema_types))

    def test_project_authored_dsp_provenance_is_required_and_exact(self) -> None:
        target_types = {
            "modeled_instrument",
            "modeled_bianzhong",
            "synthesizer",
            "procedural_sfx",
        }
        for instrument_type in target_types:
            branch = next(
                item
                for item in self.schema["oneOf"]
                if item["properties"]["type"]["const"] == instrument_type
            )
            references = {
                item.get("$ref")
                for item in branch["allOf"]
                if isinstance(item, dict)
            }
            self.assertIn(
                "#/$defs/projectAuthoredDspProvenance",
                references,
                instrument_type,
            )

            fixture = self._fixture_by_type(instrument_type)
            for field, invalid in (
                ("provenance_kind", "third_party_sample"),
                ("implementation_license", "GPL-3.0-only"),
                ("external_audio_assets", ["sample.wav"]),
                ("audio_asset_license", "CC0-1.0"),
                ("license_status", "grandfathered"),
            ):
                with self.subTest(
                    instrument_type=instrument_type,
                    field=field,
                ):
                    broken = copy.deepcopy(fixture)
                    broken[field] = invalid
                    self.assert_manifest_invalid(broken)

    def test_invalid_midi_range_is_rejected(self) -> None:
        manifest = self._dedicated_fixture()
        manifest["note_min"] = -1
        self.assert_manifest_invalid(manifest)

    def test_invalid_articulation_object_is_rejected(self) -> None:
        manifest = self._dedicated_fixture()
        manifest["articulations"] = {
            "normal": {
                "keyswitch_select": "c2",
            }
        }
        manifest["default_articulation"] = "normal"
        self.assert_manifest_invalid(manifest)

    def test_generic_articulation_range_map_is_schema_supported(self) -> None:
        manifest = self._dedicated_fixture()
        articulation = str(manifest["default_articulation"])
        manifest["articulation_playable_ranges"] = {
            articulation: [
                [manifest["note_min"], manifest["note_max"]],
            ]
        }
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

    def test_explicit_articulation_attack_override_is_schema_supported(self) -> None:
        manifest = self._dedicated_fixture()
        articulation = str(manifest["default_articulation"])
        specification = manifest["articulations"][articulation]
        sfz = (
            specification
            if isinstance(specification, str)
            else specification["sfz"]
        )
        manifest["articulations"][articulation] = {
            "sfz": sfz,
            "attack_override_seconds": 0.02,
        }
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

        manifest["articulations"][articulation][
            "attack_override_seconds"
        ] = -0.01
        self.assert_manifest_invalid(manifest)

    def test_variant_scoped_sample_gain_override_is_schema_supported(self) -> None:
        manifest = self._vpo_solo_string_fixture()
        manifest["sample_gain_db_overrides"] = [
            {
                "sample_variant": "SOLO",
                "sample": "libs/example.wav",
                "gain_db": 1.25,
            }
        ]
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

        manifest["sample_gain_db_overrides"][0]["sample_variant"] = "ALL"
        self.assert_manifest_invalid(manifest)

    def test_dedicated_sample_gain_overrides_are_schema_supported_and_bounded(
        self,
    ) -> None:
        manifest = self._dedicated_fixture()
        manifest["sample_gain_db_overrides"] = [
            {
                "sample": "Common/Banjo_Common - B3.wav",
                "gain_db": 3.2,
            }
        ]
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

        for invalid_path in (
            "/absolute.wav",
            "../escape.wav",
            "samples/../escape.wav",
            "./tone.wav",
            "samples//tone.wav",
            "samples\\tone.wav",
            "C:/tone.wav",
        ):
            with self.subTest(invalid_path=invalid_path):
                broken = copy.deepcopy(manifest)
                broken["sample_gain_db_overrides"][0]["sample"] = invalid_path
                self.assert_manifest_invalid(broken)

        for invalid_gain in (-24.01, 24.01):
            with self.subTest(invalid_gain=invalid_gain):
                broken = copy.deepcopy(manifest)
                broken["sample_gain_db_overrides"][0]["gain_db"] = invalid_gain
                self.assert_manifest_invalid(broken)

        duplicate = copy.deepcopy(manifest)
        duplicate["sample_gain_db_overrides"].append(
            copy.deepcopy(duplicate["sample_gain_db_overrides"][0])
        )
        self.assert_manifest_invalid(duplicate)

        unknown = copy.deepcopy(manifest)
        unknown["sample_gain_db_overrides"][0]["note"] = 47
        self.assert_manifest_invalid(unknown)

    def test_dedicated_sample_region_exclusions_are_schema_supported_and_safe(
        self,
    ) -> None:
        manifest = self._dedicated_fixture()
        manifest["sample_region_exclusions"] = [
            "Common/Banjo_Common - G#4.wav",
            "Common/Banjo_Common - G#4_5.wav",
            "Common/Banjo_Common - G#4_6.wav",
        ]
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

        for invalid_path in (
            "",
            "/absolute.wav",
            "../escape.wav",
            "samples/../escape.wav",
            "./tone.wav",
            "samples//tone.wav",
            "samples\\tone.wav",
            "C:/tone.wav",
        ):
            with self.subTest(invalid_path=invalid_path):
                broken = copy.deepcopy(manifest)
                broken["sample_region_exclusions"][0] = invalid_path
                self.assert_manifest_invalid(broken)

        empty = copy.deepcopy(manifest)
        empty["sample_region_exclusions"] = []
        self.assert_manifest_invalid(empty)

        duplicate = copy.deepcopy(manifest)
        duplicate["sample_region_exclusions"].append(
            duplicate["sample_region_exclusions"][0]
        )
        self.assert_manifest_invalid(duplicate)

        non_string = copy.deepcopy(manifest)
        non_string["sample_region_exclusions"][0] = None
        self.assert_manifest_invalid(non_string)

    def test_articulation_auto_default_requires_a_boolean(self) -> None:
        manifest = self._dedicated_fixture()
        manifest["articulation_auto_default"] = False
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

        manifest["articulation_auto_default"] = "false"
        self.assert_manifest_invalid(manifest)

    def test_duration_articulation_rule_shape_is_schema_checked(self) -> None:
        manifest = self._dedicated_fixture()
        articulation = str(manifest["default_articulation"])
        manifest["duration_articulation_rules"] = [
            {
                "rule_id": "short-layer-v1",
                "source_articulation": articulation,
                "target_articulation": articulation + "-fast",
                "below_seconds": 0.8,
            }
        ]
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

        for bad_value in (0, -1, True, "0.8"):
            with self.subTest(bad_value=bad_value):
                broken = copy.deepcopy(manifest)
                broken["duration_articulation_rules"][0][
                    "below_seconds"
                ] = bad_value
                self.assert_manifest_invalid(broken)

        missing = copy.deepcopy(manifest)
        del missing["duration_articulation_rules"][0]["rule_id"]
        self.assert_manifest_invalid(missing)

        unknown = copy.deepcopy(manifest)
        unknown["duration_articulation_rules"][0]["guess"] = True
        self.assert_manifest_invalid(unknown)

    def test_four_layer_range_contract_candidate_is_schema_supported(self) -> None:
        manifest = self._dedicated_fixture()
        articulation = str(manifest["default_articulation"])
        span = [[manifest["note_min"], manifest["note_max"]]]
        manifest["range_profiles"] = {
            "schema_version": 1,
            "pitch_unit": "concert_midi_note",
            "unknown_value_semantics": "null_means_unreviewed",
            "fallback_policy": (
                "reject_unknown_configuration_or_final_articulation"
            ),
            "profiles": [
                {
                    "profile_id": "candidate",
                    "selector": {
                        "resolved_runtime_configuration": {},
                        "final_articulation": articulation,
                    },
                    "physical": {
                        "hard_playable_ranges": span,
                        "idiomatic_ranges": span,
                        "extended_ranges": [],
                    },
                    "render_quality": {
                        "current_high_quality_render_ranges": span,
                        "status": "contract_candidate",
                        "approval_evidence": None,
                    },
                }
            ],
        }
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

    def test_range_contract_cannot_self_report_approved(self) -> None:
        manifest = self._dedicated_fixture()
        articulation = str(manifest["default_articulation"])
        span = [[manifest["note_min"], manifest["note_max"]]]
        manifest["range_profiles"] = {
            "schema_version": 1,
            "pitch_unit": "concert_midi_note",
            "fallback_policy": (
                "reject_unknown_configuration_or_final_articulation"
            ),
            "profiles": [
                {
                    "profile_id": "forged",
                    "selector": {
                        "resolved_runtime_configuration": {},
                        "final_articulation": articulation,
                    },
                    "physical": {
                        "hard_playable_ranges": span,
                        "idiomatic_ranges": span,
                        "extended_ranges": [],
                    },
                    "render_quality": {
                        "current_high_quality_render_ranges": span,
                        "status": "approved",
                        "approval_evidence": {
                            "path": "fake.json",
                            "sha256": "0" * 64,
                        },
                    },
                }
            ],
        }
        self.assert_manifest_invalid(manifest)

    def test_onset_overlap_policy_accepts_only_declared_contract_values(self) -> None:
        manifest = self._dedicated_fixture()
        manifest["onset_overlap_policy"] = "polyphonic_independent"
        errors = list(self.validator.iter_errors(manifest))
        self.assertFalse(errors, [error.message for error in errors])

        manifest["onset_overlap_policy"] = "guess_from_instrument_name"
        self.assert_manifest_invalid(manifest)

    def test_invalid_provenance_is_rejected(self) -> None:
        manifest = self._dedicated_fixture()
        manifest["license"] = ""
        manifest["evidence_files"] = []
        self.assert_manifest_invalid(manifest)


if __name__ == "__main__":
    unittest.main()
