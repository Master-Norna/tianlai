from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from tianlai.authoring_json import AuthoringJsonError
from tianlai.capability import (
    ArticulationExecutionCapability,
    ControlCapability,
    InstrumentCapability,
    NotePitchCapability,
    NoteVelocityCapability,
    load_capabilities,
    read_capability,
)
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


class ContinuousControlCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    @classmethod
    def one(cls, implementation_type: str) -> InstrumentCapability:
        matches = [
            capability
            for capability in cls.capabilities.values()
            if capability.implementation_type == implementation_type
        ]
        if len(matches) != 1:
            raise AssertionError(
                f"expected one {implementation_type}, found {len(matches)}"
            )
        return matches[0]

    def test_catalog_controls_are_part_wide_step_only_and_audited(self) -> None:
        declared = 0
        for capability in self.capabilities.values():
            document = capability.to_dict()
            self.assertEqual(len(document["controls"]), len(capability.controls))
            for control in capability.controls:
                with self.subTest(
                    instrument=capability.relative_path,
                    control=control.name,
                ):
                    declared += 1
                    self.assertEqual(control.scope, "part")
                    self.assertEqual(control.interpolations, ("step",))
                    self.assertIn(
                        control.application,
                        {
                            "active_voice_continuous",
                            "note_on_latched",
                            "release_gate",
                        },
                    )
                    self.assertIn(
                        control.semantic_fidelity,
                        {"native", "approximated"},
                    )
                    if control.semantic_fidelity == "native":
                        self.assertIsNone(control.approximation_reason)
                    else:
                        self.assertTrue(control.approximation_reason)
                    self.assertTrue(control.source.startswith("backend:tianlai."))
                    self.assertTrue(capability.supports_control(control.name))
                    self.assertIs(
                        capability.require_control(control.name),
                        control,
                    )
        self.assertGreater(declared, 100)

    def test_representative_backend_control_sets_match_consumers(self) -> None:
        expected = {
            "cello": {"expression"},
            "dedicated_fx": {"expression", "modulation", "sustain_pedal"},
            "dedicated_sfz": {"expression", "modulation", "sustain_pedal"},
            "flute": {"breath", "expression"},
            "modeled_bianzhong": {"expression", "modulation"},
            "mtg_solo_sax": {
                "breath",
                "expression",
                "modulation",
                "noise",
                "sustain_pedal",
            },
            "oscillator": {"sustain_pedal"},
            "piano": {"sustain_pedal", "una_corda"},
            "synthesizer": {"expression", "modulation", "sustain_pedal"},
            "violin": {"expression"},
            "vpo_brass": {
                "breath",
                "expression",
                "modulation",
                "sustain_pedal",
            },
            "vpo_celesta": {"expression", "sustain_pedal"},
            "vpo_cowbell": {"expression"},
            "vpo_harp": {"expression", "sustain_pedal"},
            "vpo_mixed_choir": {
                "breath",
                "expression",
                "modulation",
                "sustain_pedal",
            },
            "vpo_orchestral_hit": {"expression"},
            "vpo_solo_string": {"expression", "sustain_pedal"},
            "vpo_string_section": {"expression", "sustain_pedal"},
            "vpo_woodwind": {"breath", "expression"},
            "vsco2_viola_section": {"expression", "sustain_pedal"},
            "melodic_toms": set(),
            "reversed_cymbal": set(),
        }
        for implementation_type, names in expected.items():
            matches = [
                capability
                for capability in self.capabilities.values()
                if capability.implementation_type == implementation_type
            ]
            self.assertTrue(matches, implementation_type)
            for capability in matches:
                with self.subTest(
                    implementation_type=implementation_type,
                    instrument=capability.relative_path,
                ):
                    self.assertEqual(
                        {control.name for control in capability.controls},
                        names,
                    )

    def test_defaults_resolution_and_discrete_values_are_honest(self) -> None:
        piano = self.one("piano")
        pedal = piano.require_control("sustain_pedal")
        self.assertEqual(pedal.kind, "discrete")
        self.assertEqual(pedal.fidelity, "adapted")
        self.assertEqual(pedal.steps, 2)
        self.assertEqual(pedal.allowed_values, (0.0, 1.0))
        self.assertEqual(pedal.default_value, 0.0)
        self.assertEqual(pedal.application, "release_gate")
        self.assertFalse(piano.supports_control("sustain_pedal", value=0.4))
        self.assertEqual(pedal.adapt_value(0.4), 0.0)
        self.assertEqual(pedal.adapt_value(0.5), 1.0)

        flute = self.one("flute")
        for name in ("expression", "breath"):
            control = flute.require_control(name)
            self.assertEqual(control.kind, "continuous")
            self.assertEqual(control.fidelity, "native")
            self.assertIsNone(control.steps)
            self.assertEqual(control.default_value, 1.0)
            self.assertEqual(control.application, "active_voice_continuous")

        dedicated = next(
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "dedicated_sfz"
        )
        self.assertEqual(
            dedicated.require_control("modulation").default_value,
            1.0,
        )

        procedural = next(
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "procedural_sfx"
        )
        self.assertEqual(
            procedural.require_control("modulation").default_value,
            0.5,
        )
        self.assertEqual(
            procedural.require_control("distance").default_value,
            0.2,
        )

        saxophone = next(
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "mtg_solo_sax"
        )
        self.assertEqual(
            saxophone.require_control("noise").default_value,
            0.22,
        )

        brass = next(
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "vpo_brass"
        )
        modulation = brass.require_control("modulation")
        self.assertEqual(modulation.fidelity, "adapted")
        self.assertEqual(modulation.steps, 9)
        self.assertEqual(modulation.application, "note_on_latched")
        self.assertIn("modulation_attack_bins", modulation.source)

        choir = self.one("vpo_mixed_choir")
        self.assertEqual(
            choir.require_control("modulation").application,
            "note_on_latched",
        )
        self.assertEqual(
            piano.require_control("una_corda").application,
            "note_on_latched",
        )
        una_corda = piano.require_control("una_corda")
        self.assertEqual(una_corda.semantic_fidelity, "approximated")
        approximation_reason = una_corda.approximation_reason or ""
        self.assertIn("velocity and brightness", approximation_reason)
        self.assertIn("mechanical model", approximation_reason)
        self.assertEqual(
            una_corda.to_dict()["semantic_fidelity"],
            "approximated",
        )
        approximated = [
            (capability.implementation_type, control.name)
            for capability in self.capabilities.values()
            for control in capability.controls
            if control.semantic_fidelity == "approximated"
        ]
        self.assertIn(("piano", "una_corda"), approximated)

    def test_latched_modulation_declares_articulation_applicability(self) -> None:
        choir = self.one("vpo_mixed_choir")
        choir_modulation = choir.require_control("modulation")
        self.assertEqual(choir_modulation.applicable_articulations, ("normal",))
        self.assertEqual(
            choir_modulation.to_dict()["applicable_articulations"],
            ["normal"],
        )
        self.assertTrue(
            choir.supports_control("modulation", articulation="normal")
        )
        self.assertFalse(
            choir.supports_control("modulation", articulation="sustain")
        )

        brass_capabilities = [
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "vpo_brass"
        ]
        self.assertTrue(brass_capabilities)
        expected = ("accent", "normal", "staccato", "sustain")
        for capability in brass_capabilities:
            with self.subTest(instrument=capability.relative_path):
                modulation = capability.require_control("modulation")
                self.assertEqual(modulation.applicable_articulations, expected)
                for articulation in expected:
                    self.assertTrue(
                        capability.supports_control(
                            "modulation",
                            articulation=articulation,
                        )
                    )
                self.assertFalse(
                    capability.supports_control(
                        "modulation",
                        articulation="slow_sustain",
                    )
                )

        percussion_expected = {
            "triangle": ("roll",),
            "snare": ("roll", "roll_looped"),
            "cymbals": ("roll_alt", "roll_soft"),
            "vcsl_tubular_bells_2": ("damped", "open"),
        }
        observed: set[str] = set()
        for capability in self.capabilities.values():
            if (
                capability.implementation_type != "vpo_percussion"
                or not capability.supports_control("sustain_pedal")
            ):
                continue
            manifest = json.loads(
                Path(capability.manifest_path).read_text(encoding="utf-8")
            )
            profile = str(manifest["profile"])
            observed.add(profile)
            pedal = capability.require_control("sustain_pedal")
            self.assertEqual(
                pedal.applicable_articulations,
                percussion_expected[profile],
            )
        self.assertEqual(observed, set(percussion_expected))

    def test_sustain_semantics_separate_native_damper_from_release_gate(
        self,
    ) -> None:
        native_types = {
            "oscillator",
            "piano",
            "synthesizer",
            "vpo_celesta",
        }
        native_dedicated_profiles = {
            "greg_sullivan_cp80_dedicated_multisample_bandlimited",
            "greg_sullivan_cp80_dedicated_multisample_fx_chain_bandlimited",
            "vcsl_vibraphone_strict_cc0_two_mallets_bowed",
        }
        checked = 0
        for capability in self.capabilities.values():
            if not capability.supports_control("sustain_pedal"):
                continue
            checked += 1
            manifest = json.loads(
                Path(capability.manifest_path).read_text(encoding="utf-8")
            )
            expected_native = (
                capability.implementation_type in native_types
                or manifest.get("upgrade_status") in native_dedicated_profiles
                or (
                    capability.implementation_type == "vpo_percussion"
                    and manifest.get("profile")
                    in {
                        "tubular_bells",
                        "vcsl_tubular_bells_2",
                        "vibraphone",
                    }
                )
            )
            pedal = capability.require_control("sustain_pedal")
            with self.subTest(instrument=capability.relative_path):
                self.assertEqual(
                    pedal.semantic_fidelity,
                    "native" if expected_native else "approximated",
                )
                self.assertIs(
                    capability.require_control(
                        "sustain_pedal",
                        semantic_policy="approximate",
                    ),
                    pedal,
                )
                if expected_native:
                    self.assertIsNone(pedal.approximation_reason)
                    self.assertIs(
                        capability.require_control(
                            "sustain_pedal",
                            semantic_policy="exact",
                        ),
                        pedal,
                    )
                else:
                    self.assertIn("release gate", pedal.approximation_reason or "")
                    with self.assertRaisesRegex(
                        ValueError,
                        "semantic_policy='approximate'",
                    ):
                        capability.require_control(
                            "sustain_pedal",
                            semantic_policy="exact",
                        )
        self.assertGreater(checked, 50)

    def test_breath_semantics_distinguish_gain_proxy_from_air_noise(self) -> None:
        gain_only_types = {
            "flute",
            "vpo_brass",
            "vpo_mixed_choir",
            "vpo_woodwind",
        }
        observed_types: set[str] = set()
        for capability in self.capabilities.values():
            if not capability.supports_control("breath"):
                continue
            observed_types.add(capability.implementation_type)
            breath = capability.require_control("breath")
            with self.subTest(instrument=capability.relative_path):
                if capability.implementation_type in gain_only_types:
                    self.assertEqual(breath.semantic_fidelity, "approximated")
                    reason = breath.approximation_reason or ""
                    self.assertIn("playback-gain multiplier", reason)
                    self.assertIn("airflow noise", reason)
                    self.assertIn("smoothed_gain_proxy", breath.source)
                    with self.assertRaisesRegex(
                        ValueError,
                        "semantic_policy='approximate'",
                    ):
                        capability.require_control(
                            "breath",
                            semantic_policy="exact",
                        )
                    self.assertIs(
                        capability.require_control(
                            "breath",
                            semantic_policy="approximate",
                        ),
                        breath,
                    )
                else:
                    self.assertEqual(
                        capability.implementation_type,
                        "mtg_solo_sax",
                    )
                    self.assertEqual(breath.semantic_fidelity, "native")
                    self.assertIsNone(breath.approximation_reason)
                    self.assertIn("recorded_breath_noise_mix", breath.source)
                    self.assertIs(
                        capability.require_control(
                            "breath",
                            semantic_policy="exact",
                        ),
                        breath,
                    )
        self.assertEqual(observed_types, gain_only_types | {"mtg_solo_sax"})

    def test_release_velocity_is_declared_only_when_backend_reads_it(self) -> None:
        supporting = [
            capability
            for capability in self.capabilities.values()
            if capability.supports_release_velocity
        ]
        self.assertTrue(supporting)
        self.assertEqual(
            {capability.implementation_type for capability in supporting},
            {"mtg_solo_sax"},
        )
        for capability in self.capabilities.values():
            document = capability.to_dict()
            self.assertEqual(
                document["supports_release_velocity"],
                capability.implementation_type == "mtg_solo_sax",
            )
            if capability.implementation_type == "mtg_solo_sax":
                self.assertIn(
                    "MtgSoloSaxInstrument",
                    capability.release_velocity_source or "",
                )
            else:
                self.assertIsNone(capability.release_velocity_source)

    def test_manifest_json_is_strict_and_default_cannot_expand_backend_vocab(
        self,
    ) -> None:
        for payload, code in (
            (b'{"name":"a","name":"b","type":"oscillator"}',
             "duplicate_object_member"),
            (b'{"name":"a","type":"oscillator","note_min":NaN}',
             "non_finite_number"),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                directory = root / "strict"
                directory.mkdir()
                manifest_path = directory / "instrument.json"
                manifest_path.write_bytes(payload)
                with self.assertRaises(AuthoringJsonError) as caught:
                    read_capability(manifest_path, root=root)
                self.assertEqual(caught.exception.code, code)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "drift"
            directory.mkdir()
            manifest_path = directory / "instrument.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "drift",
                        "type": "violin",
                        "default_articulation": "not-a-backend-articulation",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "audited backend"):
                read_capability(manifest_path, root=root)

    def test_instrument_capability_validates_foundational_identity_fields(
        self,
    ) -> None:
        arguments = dict(
            name="test",
            relative_path="test/instrument",
            manifest_path="test/instrument/instrument.json",
            implementation_type="oscillator",
            pitched=True,
            note_min=0.0,
            note_max=127.0,
            articulations=("sustain",),
            default_articulation="sustain",
            articulation_source="test",
            onset_seconds=None,
            quality_tier=None,
        )
        for replacement, message in (
            ({"pitched": 1}, "pitched must be boolean"),
            ({"note_min": math.nan}, "finite number"),
            ({"note_min": 90.0, "note_max": 20.0}, "note range"),
            ({"pitch_mode": "unknown"}, "pitch_mode"),
            ({"pitch_mode": "fixed"}, "requires fixed_midi_note"),
            ({"fixed_midi_note": 60.0}, "requires fixed pitch_mode"),
            ({"default_articulation": "missing"}, "audited articulation"),
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaisesRegex(ValueError, message):
                    InstrumentCapability(**{**arguments, **replacement})

    def test_soundfont_contract_exposes_midi_cc_quantization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "soundfont"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "soundfont",
                        "type": "soundfont",
                        "pan": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)

        self.assertEqual(
            {control.name for control in capability.controls},
            {
                "breath",
                "expression",
                "modulation",
                "pan",
                "sustain_pedal",
                "volume",
            },
        )
        pan = capability.require_control("pan")
        self.assertAlmostEqual(pan.default_value, 64.0 / 127.0)
        self.assertEqual(pan.fidelity, "adapted")
        self.assertEqual(pan.steps, 128)
        self.assertFalse(capability.supports_control("pan", value=0.5))
        adapted = pan.adapt_value(0.5)
        self.assertAlmostEqual(adapted, 64.0 / 127.0)
        self.assertTrue(capability.supports_control("pan", value=adapted))
        self.assertFalse(
            capability.supports_control("pan", value=adapted + 1.0e-12)
        )

        expression = capability.require_control("expression")
        self.assertEqual(expression.quantization_exponent, 1.25)
        adapted_expression = expression.adapt_value(0.5)
        self.assertTrue(
            capability.supports_control(
                "expression",
                value=adapted_expression,
            )
        )
        self.assertEqual(
            expression.require_value(adapted_expression),
            adapted_expression,
        )
        with self.assertRaisesRegex(ValueError, "adapt explicitly"):
            expression.require_value(adapted_expression + 1.0e-12)
        soundfont_breath = capability.require_control("breath")
        self.assertEqual(soundfont_breath.semantic_fidelity, "native")
        self.assertIsNone(soundfont_breath.approximation_reason)

    def test_preflight_rejects_unknown_scope_kind_interpolation_and_value(
        self,
    ) -> None:
        piano = self.one("piano")
        self.assertFalse(piano.supports_control("expression"))
        self.assertFalse(piano.supports_control("una_corda", scope="per_note"))
        self.assertFalse(piano.supports_control("una_corda", kind="discrete"))
        self.assertFalse(
            piano.supports_control("una_corda", interpolation="linear")
        )
        self.assertFalse(piano.supports_control("una_corda", value=float("nan")))
        self.assertFalse(piano.supports_control("una_corda", value=1.01))
        with self.assertRaisesRegex(ValueError, "does not support control"):
            piano.require_control("expression")
        with self.assertRaisesRegex(ValueError, "scope 'per_note'"):
            piano.require_control("una_corda", scope="per_note")

    def test_profiles_do_not_claim_an_unobservable_sustain_pedal(self) -> None:
        modeled = [
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "modeled_instrument"
        ]
        expected_modeled = {
            "shamisen": {"expression"},
            "koto": {"expression"},
            "sitar": {"expression"},
            "shakuhachi": {"expression", "modulation"},
            "pan_flute": {"expression", "modulation"},
            "suona": {"expression", "modulation"},
            "taiko": {"expression"},
            "steelpan": {"expression"},
            "music_box": {"expression"},
            "synth_drum": {"expression"},
        }
        actual_profiles: set[str] = set()
        for capability in modeled:
            manifest = json.loads(
                Path(capability.manifest_path).read_text(encoding="utf-8")
            )
            profile = str(manifest["profile"])
            actual_profiles.add(profile)
            names = {control.name for control in capability.controls}
            self.assertEqual(names, expected_modeled[profile])
        self.assertEqual(actual_profiles, set(expected_modeled))

        percussion = [
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "vpo_percussion"
        ]
        self.assertTrue(
            any(item.supports_control("sustain_pedal") for item in percussion)
        )
        self.assertTrue(
            any(not item.supports_control("sustain_pedal") for item in percussion)
        )
        for capability in percussion:
            self.assertIn(
                {control.name for control in capability.controls},
                (
                    {"expression"},
                    {"expression", "sustain_pedal"},
                ),
            )

        for profile, expected in (("gunshot", False), ("ocean", True)):
            with self.subTest(profile=profile):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    directory = root / profile
                    directory.mkdir()
                    manifest_path = directory / "乐器.json"
                    manifest_path.write_text(
                        json.dumps(
                            {
                                "name": profile,
                                "type": "procedural_sfx",
                                "profile": profile,
                            }
                        ),
                        encoding="utf-8",
                    )
                    capability = read_capability(manifest_path, root=root)
                self.assertEqual(
                    capability.supports_control("sustain_pedal"),
                    expected,
                )
                expected_names = {"distance", "expression", "modulation"}
                if expected:
                    expected_names.add("sustain_pedal")
                self.assertEqual(
                    {control.name for control in capability.controls},
                    expected_names,
                )

    def test_manifest_controls_cannot_overclaim_the_audited_backend(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "modeled"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "modeled",
                        "type": "modeled_bianzhong",
                        "supported_controls": ["expression", "reverb"],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "supported_controls"):
                read_capability(manifest_path, root=root)

    def test_local_factory_does_not_inherit_builtin_runtime_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "local"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "local",
                        "type": "mtg_solo_sax",
                        "implementation": "custom.py",
                        "allowed_articulations": ["normal"],
                        "default_articulation": "normal",
                    }
                ),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)

        self.assertEqual(capability.controls, ())
        self.assertFalse(capability.supports_release_velocity)
        self.assertIsNone(capability.release_velocity_source)
        self.assertFalse(capability.supports_note_velocity)
        self.assertIsNone(capability.note_velocity)
        self.assertIsNone(capability.to_dict()["note_velocity"])
        self.assertFalse(capability.supports_note_pitch)
        self.assertIsNone(capability.note_pitch)
        self.assertIsNone(capability.to_dict()["note_pitch"])
        self.assertFalse(capability.supports_articulation_execution)
        self.assertIsNone(capability.articulation_execution)
        self.assertIsNone(capability.to_dict()["articulation_execution"])

    def test_legacy_instrument_capability_constructor_defaults_to_no_controls(
        self,
    ) -> None:
        capability = InstrumentCapability(
            "legacy",
            "legacy",
            "legacy.json",
            "legacy",
            True,
            0.0,
            127.0,
            (),
            None,
            "none",
            None,
            None,
        )
        self.assertEqual(capability.controls, ())
        self.assertEqual(capability.to_dict()["controls"], [])
        self.assertFalse(capability.supports_release_velocity)
        self.assertIsNone(capability.release_velocity_source)
        self.assertFalse(capability.supports_note_velocity)
        self.assertIsNone(capability.note_velocity)
        self.assertIsNone(capability.to_dict()["note_velocity"])
        self.assertFalse(capability.supports_note_pitch)
        self.assertIsNone(capability.note_pitch)
        self.assertIsNone(capability.to_dict()["note_pitch"])
        self.assertFalse(capability.supports_articulation_execution)
        self.assertIsNone(capability.articulation_execution)
        self.assertIsNone(capability.to_dict()["articulation_execution"])

    def test_control_record_rejects_unimplemented_per_note_or_linear_claims(
        self,
    ) -> None:
        arguments = {
            "name": "expression",
            "scope": "part",
            "kind": "continuous",
            "minimum": 0.0,
            "maximum": 1.0,
            "default_value": 1.0,
            "interpolations": ("step",),
            "application": "active_voice_continuous",
            "fidelity": "native",
            "semantic_fidelity": "native",
            "approximation_reason": None,
            "steps": None,
            "quantization_exponent": None,
            "allowed_values": None,
            "source": "backend:test.handle_event",
        }
        with self.assertRaisesRegex(ValueError, "per-note"):
            ControlCapability(**{**arguments, "scope": "per_note"})
        with self.assertRaisesRegex(ValueError, "only supports step"):
            ControlCapability(
                **{**arguments, "interpolations": ("step", "linear")}
            )
        with self.assertRaisesRegex(ValueError, "approximation_reason"):
            ControlCapability(
                **{
                    **arguments,
                    "semantic_fidelity": "approximated",
                    "approximation_reason": None,
                }
            )
        with self.assertRaisesRegex(ValueError, "must not declare"):
            ControlCapability(
                **{
                    **arguments,
                    "approximation_reason": "not allowed for native",
                }
            )
        with self.assertRaisesRegex(ValueError, "unique and sorted"):
            ControlCapability(
                **{
                    **arguments,
                    "applicable_articulations": ("sustain", "normal"),
                }
            )
        restricted = ControlCapability(
            **{
                **arguments,
                "applicable_articulations": ("legato",),
            }
        )
        with self.assertRaisesRegex(ValueError, "unknown"):
            InstrumentCapability(
                "invalid",
                "invalid",
                "invalid.json",
                "legacy",
                True,
                0.0,
                127.0,
                ("normal",),
                "normal",
                "test",
                None,
                None,
                controls=(restricted,),
            )


class NoteVelocityCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_catalog_declares_numeric_and_semantic_velocity_fidelity(self) -> None:
        adapted_types = {"dedicated_fx", "dedicated_sfz"}
        approximated_types = {
            "cello",
            "flute",
            "oscillator",
            "procedural_sfx",
            "reversed_cymbal",
            "violin",
            "vpo_brass",
            "vpo_harp",
            "vpo_mixed_choir",
            "vpo_orchestral_hit",
            "vpo_percussion",
            "vpo_solo_string",
            "vpo_string_section",
            "vpo_woodwind",
            "vsco2_viola_section",
        }
        observed_types: set[str] = set()
        for capability in self.capabilities.values():
            velocity = capability.note_velocity
            with self.subTest(instrument=capability.relative_path):
                self.assertIsNotNone(velocity)
                assert velocity is not None
                observed_types.add(capability.implementation_type)
                self.assertTrue(capability.supports_note_velocity)
                self.assertEqual(
                    velocity.fidelity,
                    (
                        "adapted"
                        if capability.implementation_type in adapted_types
                        else "native"
                    ),
                )
                self.assertEqual(
                    velocity.semantic_fidelity,
                    (
                        "approximated"
                        if capability.implementation_type in approximated_types
                        else "native"
                    ),
                )
                if velocity.semantic_fidelity == "approximated":
                    self.assertTrue(velocity.approximation_reason)
                else:
                    self.assertIsNone(velocity.approximation_reason)
                self.assertTrue(velocity.source.startswith("backend:tianlai."))
                document = capability.to_dict()["note_velocity"]
                assert document is not None
                self.assertEqual(document["fidelity"], velocity.fidelity)
                self.assertEqual(document["zero_behavior"], velocity.zero_behavior)
        self.assertEqual(
            observed_types,
            {
                "cello",
                "dedicated_fx",
                "dedicated_sfz",
                "flute",
                "melodic_toms",
                "modeled_bianzhong",
                "modeled_instrument",
                "mtg_solo_sax",
                "oscillator",
                "piano",
                "procedural_sfx",
                "reversed_cymbal",
                "synthesizer",
                "violin",
                "vpo_brass",
                "vpo_celesta",
                "vpo_cowbell",
                "vpo_harp",
                "vpo_mixed_choir",
                "vpo_orchestral_hit",
                "vpo_percussion",
                "vpo_solo_string",
                "vpo_string_section",
                "vpo_woodwind",
                "vsco2_viola_section",
            },
        )

    def test_dedicated_sfz_grid_requires_or_records_explicit_adaptation(
        self,
    ) -> None:
        capability = next(
            item
            for item in self.capabilities.values()
            if item.implementation_type == "dedicated_sfz"
        )
        velocity = capability.note_velocity
        assert velocity is not None
        self.assertEqual(velocity.steps, 128)
        self.assertEqual(velocity.quantization_exponent, 1.0)
        self.assertEqual(velocity.quantization_output_range, (0, 127))
        exact = 64.0 / 127.0
        self.assertAlmostEqual(
            capability.require_note_velocity(exact).resolved_value,
            exact,
        )
        with self.assertRaisesRegex(ValueError, "adapt explicitly"):
            capability.require_note_velocity(exact + 1.0e-10)
        with self.assertRaisesRegex(ValueError, "adapt explicitly"):
            capability.require_note_velocity(exact + 1.0e-12)
        with self.assertRaisesRegex(ValueError, "adapt explicitly"):
            capability.require_note_velocity(0.5)
        resolution = capability.adapt_note_velocity(0.5)
        self.assertEqual(resolution.requested_value, 0.5)
        self.assertAlmostEqual(resolution.resolved_value, exact)
        self.assertTrue(resolution.adapted)
        self.assertEqual(
            capability.require_note_velocity(
                resolution.resolved_value
            ).resolved_value,
            resolution.resolved_value,
        )
        self.assertEqual(resolution.fidelity, "adapted")
        self.assertIn("round(value*127)", resolution.source)

    def test_soundfont_velocity_exposes_exponent_and_midi_noteon_floor(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "soundfont"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "soundfont",
                        "type": "soundfont",
                        "velocity_exponent": 0.72,
                    }
                ),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)

        velocity = capability.note_velocity
        assert velocity is not None
        self.assertEqual(velocity.fidelity, "adapted")
        self.assertEqual(velocity.steps, 127)
        self.assertEqual(velocity.quantization_exponent, 0.72)
        self.assertEqual(velocity.quantization_output_range, (1, 127))
        self.assertEqual(velocity.zero_behavior, "minimum_nonzero")
        with self.assertRaisesRegex(ValueError, "not exactly representable"):
            capability.require_note_velocity(0.0)
        minimum = (1.0 / 127.0) ** (1.0 / 0.72)
        zero_resolution = capability.adapt_note_velocity(0.0)
        self.assertAlmostEqual(zero_resolution.resolved_value, minimum)
        self.assertEqual(zero_resolution.requested_value, 0.0)
        self.assertTrue(zero_resolution.adapted)
        self.assertAlmostEqual(
            capability.require_note_velocity(minimum).resolved_value,
            minimum,
        )
        requested = 0.5
        expected_index = round(requested**0.72 * 127.0)
        expected = (expected_index / 127.0) ** (1.0 / 0.72)
        self.assertAlmostEqual(
            capability.adapt_note_velocity(requested).resolved_value,
            expected,
        )

    def test_native_float_is_preserved_but_semantics_need_consent(self) -> None:
        native = next(
            item
            for item in self.capabilities.values()
            if item.implementation_type == "synthesizer"
        )
        requested = math.nextafter(0.37123456789, 1.0)
        resolution = native.require_note_velocity(
            requested,
            semantic_policy="exact",
        )
        self.assertEqual(resolution.requested_value, requested)
        self.assertEqual(resolution.resolved_value, requested)
        self.assertFalse(resolution.adapted)

        oscillator = next(
            item
            for item in self.capabilities.values()
            if item.implementation_type == "oscillator"
        )
        with self.assertRaisesRegex(ValueError, "semantic_policy='approximate'"):
            oscillator.require_note_velocity(0.5, semantic_policy="exact")
        self.assertEqual(
            oscillator.require_note_velocity(
                0.5,
                semantic_policy="approximate",
            ).resolved_value,
            0.5,
        )

    def test_modeled_zero_behavior_is_profile_specific(self) -> None:
        expected_silent = {"koto", "shamisen", "sitar"}
        observed: set[str] = set()
        for capability in self.capabilities.values():
            if capability.implementation_type != "modeled_instrument":
                continue
            manifest = json.loads(
                Path(capability.manifest_path).read_text(encoding="utf-8")
            )
            profile = str(manifest["profile"])
            observed.add(profile)
            assert capability.note_velocity is not None
            self.assertEqual(
                capability.note_velocity.zero_behavior,
                "silent" if profile in expected_silent else "audible_baseline",
            )
        self.assertEqual(
            observed,
            {
                "koto",
                "music_box",
                "pan_flute",
                "shakuhachi",
                "shamisen",
                "sitar",
                "steelpan",
                "suona",
                "synth_drum",
                "taiko",
            },
        )

    def test_generic_sample_is_numeric_native_but_semantically_unverified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "sample"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps({"name": "sample", "type": "sample"}),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)
        assert capability.note_velocity is not None
        self.assertEqual(capability.note_velocity.fidelity, "native")
        self.assertEqual(
            capability.note_velocity.semantic_fidelity,
            "approximated",
        )

    def test_note_velocity_record_rejects_false_grid_or_zero_claims(self) -> None:
        with self.assertRaisesRegex(ValueError, "minimum_nonzero"):
            NoteVelocityCapability(
                fidelity="native",
                semantic_fidelity="native",
                approximation_reason=None,
                steps=None,
                quantization_exponent=None,
                quantization_output_range=None,
                source="backend:test",
                zero_behavior="minimum_nonzero",
            )
        with self.assertRaisesRegex(ValueError, "requires at least two"):
            NoteVelocityCapability(
                fidelity="adapted",
                semantic_fidelity="native",
                approximation_reason=None,
                steps=None,
                quantization_exponent=1.0,
                quantization_output_range=(0, 127),
                source="backend:test",
            )


class NotePitchCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_catalog_pitch_modes_come_from_audited_execution_paths(self) -> None:
        formal = [
            capability
            for capability in self.capabilities.values()
            if capability.quality_tier == "formal"
        ]
        self.assertEqual(
            {
                mode: sum(
                    (
                        capability.note_pitch.mode
                        if capability.note_pitch is not None
                        else None
                    )
                    == mode
                    for capability in formal
                )
                for mode in ("continuous", "selector", "fixed", None)
            },
            {"continuous": 77, "selector": 10, "fixed": 3, None: 13},
        )
        for capability in formal:
            with self.subTest(instrument=capability.relative_path):
                pitch = capability.note_pitch
                self.assertEqual(capability.supports_note_pitch, pitch is not None)
                self.assertEqual(
                    capability.to_dict()["note_pitch"],
                    None if pitch is None else pitch.to_dict(),
                )
                if pitch is not None:
                    self.assertEqual(pitch.application, "note_on_latched")
                    self.assertTrue(pitch.source.startswith("backend:tianlai."))

        for capability in formal:
            if capability.implementation_type == "procedural_sfx":
                self.assertIsNone(capability.note_pitch)
            if (
                capability.implementation_type == "vpo_percussion"
                and not capability.pitched
            ):
                self.assertIsNone(capability.note_pitch)

    def test_continuous_pitch_is_not_inferred_from_range_or_covers(self) -> None:
        synthesizer = next(
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "synthesizer"
        )
        assert synthesizer.note_pitch is not None
        self.assertEqual(synthesizer.note_pitch.mode, "continuous")
        self.assertEqual(synthesizer.note_pitch.fidelity, "native")
        self.assertEqual(synthesizer.note_pitch.semantic_fidelity, "native")
        requested = math.nextafter(60.123456789, 61.0)
        resolution = synthesizer.require_note_pitch(
            requested,
            semantic_policy="exact",
        )
        self.assertEqual(resolution.requested_value, requested)
        self.assertEqual(resolution.resolved_value, requested)
        self.assertFalse(resolution.adapted)

        unpitched = self.capabilities["环境与拟音/掌声"]
        self.assertTrue(unpitched.covers(60.0))
        self.assertIsNone(unpitched.note_pitch)
        with self.assertRaisesRegex(ValueError, "does not declare"):
            unpitched.require_note_pitch(60.0)

    def test_soundfont_key_and_bend_grid_requires_explicit_adaptation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "soundfont"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "soundfont",
                        "type": "soundfont",
                        "pitch_bend_range_semitones": 2.0,
                    }
                ),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)

        pitch = capability.note_pitch
        assert pitch is not None
        self.assertEqual(pitch.mode, "quantized")
        self.assertEqual(pitch.fidelity, "adapted")
        self.assertEqual(pitch.semantic_fidelity, "native")
        self.assertEqual(pitch.quantization_steps_per_direction, 8192)
        self.assertEqual(pitch.pitch_bend_range_semitones, 2.0)
        exact = 60.0 + 1.0 / 4096.0
        self.assertEqual(
            capability.require_note_pitch(exact).resolved_value,
            exact,
        )
        with self.assertRaisesRegex(ValueError, "adapt explicitly"):
            capability.require_note_pitch(exact + 1.0e-12)
        adapted = capability.adapt_note_pitch(60.0003)
        self.assertEqual(adapted.requested_value, 60.0003)
        self.assertEqual(adapted.resolved_value, exact)
        self.assertTrue(adapted.adapted)
        self.assertEqual(
            capability.require_note_pitch(adapted.resolved_value).resolved_value,
            adapted.resolved_value,
        )
        self.assertIn("14-bit", adapted.numeric_approximation_reason or "")

        # With non-power-of-two bend ranges a first decode can cross the
        # nearest-key boundary.  The adapter must return a stable preimage,
        # not a tolerance-accepted value that encodes differently next time.
        pitch_1_5 = NotePitchCapability(
            application="note_on_latched",
            protocol_input="pitch_hz",
            value_unit="midi_note_at_a4_440",
            mode="quantized",
            fidelity="adapted",
            semantic_fidelity="native",
            numeric_approximation_reason="grid",
            semantic_approximation_reason=None,
            source="backend:test",
            quantization_steps_per_direction=8192,
            pitch_bend_range_semitones=1.5,
        )
        boundary = pitch_1_5.adapt_value(80.49999712201549)
        self.assertEqual(pitch_1_5.require_value(boundary), boundary)

    def test_soundfont_non_cent_bend_range_has_no_v2_pitch_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "soundfont"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "soundfont",
                        "type": "soundfont",
                        "pitch_bend_range_semitones": 2.375,
                    }
                ),
                encoding="utf-8",
            )
            capability = read_capability(manifest_path, root=root)

        # Runtime RPN sensitivity is cent-quantized while _key_and_bend uses
        # the raw float.  Until both are represented, fail closed.
        self.assertIsNone(capability.note_pitch)
        self.assertFalse(capability.supports_note_pitch)

    def test_fixed_and_selector_pitch_need_semantic_consent(self) -> None:
        fixed = self.capabilities["现代鼓组/底鼓"]
        assert fixed.note_pitch is not None
        self.assertEqual(fixed.note_pitch.mode, "fixed")
        self.assertEqual(fixed.note_pitch.fidelity, "ignored")
        with self.assertRaisesRegex(ValueError, "semantic_policy='approximate'"):
            fixed.adapt_note_pitch(72.0, semantic_policy="exact")
        fixed_resolution = fixed.adapt_note_pitch(
            72.0,
            semantic_policy="approximate",
        )
        self.assertEqual(
            fixed_resolution.resolved_value,
            fixed.fixed_midi_note,
        )
        self.assertTrue(fixed_resolution.adapted)

        selector = self.capabilities["现代鼓组/高音通鼓"]
        assert selector.note_pitch is not None
        self.assertEqual(selector.note_pitch.mode, "selector")
        self.assertEqual(selector.note_pitch.fidelity, "native")
        self.assertEqual(
            selector.require_note_pitch(
                60.125,
                semantic_policy="approximate",
            ).resolved_value,
            60.125,
        )
        with self.assertRaisesRegex(ValueError, "semantic_policy='approximate'"):
            selector.require_note_pitch(60.125, semantic_policy="exact")

    def test_profile_specific_selector_rounding_matches_runtime(self) -> None:
        reversed_cymbal = self.capabilities["管弦乐/打击乐组/反向镲"]
        assert reversed_cymbal.note_pitch is not None
        self.assertEqual(reversed_cymbal.note_pitch.allowed_values, (60.0, 61.0, 62.0))
        self.assertEqual(
            reversed_cymbal.adapt_note_pitch(
                60.5,
                semantic_policy="approximate",
            ).resolved_value,
            60.0,
        )
        with self.assertRaisesRegex(ValueError, "adapt explicitly"):
            reversed_cymbal.require_note_pitch(
                60.5,
                semantic_policy="approximate",
            )

        taiko = self.capabilities["管弦乐/打击乐组/太鼓"]
        assert taiko.note_pitch is not None
        self.assertEqual(taiko.note_pitch.allowed_values, (60.0, 61.0, 62.0))
        self.assertEqual(
            taiko.adapt_note_pitch(
                61.5,
                semantic_policy="approximate",
            ).resolved_value,
            62.0,
        )

    def test_reversed_cymbal_selector_keys_match_runtime_integer_parser(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            directory = root / "reversed"
            directory.mkdir()
            manifest_path = directory / "乐器.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "name": "fractional selector",
                        "type": "reversed_cymbal",
                        "variants": {"60.5": {"sample": "unused.wav"}},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "integer strings"):
                read_capability(manifest_path, root=root)

    def test_composite_orchestral_hit_separates_numeric_and_semantic_fidelity(
        self,
    ) -> None:
        hit = next(
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "vpo_orchestral_hit"
        )
        assert hit.note_pitch is not None
        self.assertEqual(hit.note_pitch.fidelity, "native")
        self.assertEqual(hit.note_pitch.semantic_fidelity, "approximated")
        self.assertIsNone(hit.note_pitch.numeric_approximation_reason)
        self.assertIn(
            "fixed",
            hit.note_pitch.semantic_approximation_reason or "",
        )
        with self.assertRaisesRegex(ValueError, "semantic_policy='approximate'"):
            hit.require_note_pitch(60.0, semantic_policy="exact")

    def test_pitch_record_rejects_cross_axis_overclaims(self) -> None:
        common = {
            "application": "note_on_latched",
            "protocol_input": "pitch_hz",
            "value_unit": "midi_note_at_a4_440",
            "source": "backend:test",
        }
        with self.assertRaisesRegex(ValueError, "continuous.*native"):
            NotePitchCapability(
                **common,
                mode="continuous",
                fidelity="adapted",
                semantic_fidelity="native",
                numeric_approximation_reason="grid",
                semantic_approximation_reason=None,
            )
        with self.assertRaisesRegex(
            ValueError,
            "selector.*ignored semantic",
        ):
            NotePitchCapability(
                **common,
                mode="selector",
                fidelity="native",
                semantic_fidelity="native",
                numeric_approximation_reason=None,
                semantic_approximation_reason=None,
            )
        with self.assertRaisesRegex(ValueError, "fixed_midi_note"):
            NotePitchCapability(
                **common,
                mode="fixed",
                fidelity="ignored",
                semantic_fidelity="ignored",
                numeric_approximation_reason="ignored",
                semantic_approximation_reason="ignored",
            )


class ArticulationExecutionCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def test_runtime_latching_is_stronger_than_vocabulary_membership(self) -> None:
        flute = self.capabilities["管弦乐/木管组/长笛"]
        self.assertTrue(flute.supports("staccato"))
        self.assertTrue(flute.supports_articulation_execution)
        resolution = flute.require_articulation_execution(
            "staccato",
            semantic_policy="exact",
        )
        self.assertEqual(resolution.requested_value, "staccato")
        self.assertEqual(resolution.resolved_value, "staccato")
        self.assertFalse(resolution.adapted)
        self.assertEqual(resolution.application, "note_on_latched")
        self.assertIn("FluteInstrument.handle_event", resolution.source)

        for implementation_type in ("vpo_cowbell", "vpo_orchestral_hit"):
            capability = next(
                item
                for item in self.capabilities.values()
                if item.implementation_type == implementation_type
            )
            self.assertTrue(capability.supports("hit"))
            self.assertFalse(capability.supports_articulation_execution)
            self.assertIsNone(capability.articulation_execution)
            with self.assertRaisesRegex(ValueError, "note-on-latched"):
                capability.require_articulation_execution("hit")

    def test_explicit_mapping_records_both_values_and_requires_consent(self) -> None:
        saxophone = next(
            capability
            for capability in self.capabilities.values()
            if capability.implementation_type == "mtg_solo_sax"
        )
        with self.assertRaisesRegex(ValueError, "semantic_policy='approximate'"):
            saxophone.adapt_articulation_execution(
                "tenuto",
                "sustain",
                mapping_source="roster:part.articulation_map",
                semantic_policy="exact",
            )
        resolution = saxophone.adapt_articulation_execution(
            "tenuto",
            "sustain",
            mapping_source="roster:part.articulation_map",
            semantic_policy="approximate",
        )
        self.assertEqual(resolution.requested_value, "tenuto")
        self.assertEqual(resolution.resolved_value, "sustain")
        self.assertTrue(resolution.adapted)
        self.assertEqual(resolution.semantic_fidelity, "approximated")
        self.assertEqual(
            resolution.mapping_source,
            "roster:part.articulation_map",
        )
        with self.assertRaisesRegex(ValueError, "mapping_source"):
            saxophone.adapt_articulation_execution(
                "tenuto",
                "sustain",
                mapping_source="",
                semantic_policy="approximate",
            )

    def test_soundfont_requires_a_real_program_route_not_a_default_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            default_directory = root / "default"
            default_directory.mkdir()
            default_path = default_directory / "乐器.json"
            default_path.write_text(
                json.dumps(
                    {
                        "name": "default only",
                        "type": "soundfont",
                        "default_articulation": "sustain",
                    }
                ),
                encoding="utf-8",
            )
            default_capability = read_capability(default_path, root=root)
            self.assertTrue(default_capability.supports("sustain"))
            self.assertIsNone(default_capability.articulation_execution)

            routed_directory = root / "routed"
            routed_directory.mkdir()
            routed_path = routed_directory / "乐器.json"
            routed_path.write_text(
                json.dumps(
                    {
                        "name": "routed",
                        "type": "soundfont",
                        "articulations": {
                            "staccato": {},
                            "sustain": {},
                        },
                        "default_articulation": "sustain",
                        "articulation_programs": {
                            "staccato": 1,
                            "sustain": 0,
                        },
                    }
                ),
                encoding="utf-8",
            )
            routed_capability = read_capability(routed_path, root=root)

        self.assertIsNotNone(routed_capability.articulation_execution)
        self.assertEqual(
            routed_capability.require_articulation_execution(
                "staccato",
                semantic_policy="exact",
            ).resolved_value,
            "staccato",
        )

    def test_articulation_execution_record_rejects_unordered_or_false_claims(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "unique, sorted"):
            ArticulationExecutionCapability(
                articulations=("sustain", "staccato"),
                application="note_on_latched",
                fidelity="native",
                semantic_fidelity="native",
                approximation_reason=None,
                source="backend:test",
            )
        with self.assertRaisesRegex(ValueError, "ignored.*semantics"):
            ArticulationExecutionCapability(
                articulations=("sustain",),
                application="note_on_latched",
                fidelity="ignored",
                semantic_fidelity="native",
                approximation_reason=None,
                source="backend:test",
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
                    pattern = (
                        "non_finite_number"
                        if any(
                            isinstance(item, list)
                            and any(value == float("inf") for value in item)
                            for item in playable_ranges
                        )
                        else "playable_ranges"
                    )
                    with self.assertRaisesRegex(ValueError, pattern):
                        read_capability(manifest_path, root=root)


if __name__ == "__main__":
    unittest.main()
