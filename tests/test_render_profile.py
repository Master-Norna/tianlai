from __future__ import annotations

import math
import unittest

from tianlai.render_profile import (
    RenderProfile,
    parse_render_profile,
    profile_with_overrides,
)


class RenderProfileTests(unittest.TestCase):
    def test_default_preview_profile_is_explicit_and_round_trips(self) -> None:
        profile = RenderProfile()
        document = profile.to_dict()
        self.assertEqual(document["name"], "preview-v1")
        self.assertEqual(document["normalize_peak_db"], -1.0)
        self.assertTrue(document["space"]["enabled"])
        self.assertEqual(parse_render_profile(document), profile)

    def test_dry_unnormalized_profile_is_creator_selectable(self) -> None:
        profile = parse_render_profile(
            {
                "kind": "tianlai.render_profile",
                "schema_version": 1,
                "name": "dry",
                "normalize_peak_db": None,
                "space": {"enabled": False},
            }
        )
        self.assertIsNone(profile.normalize_peak_db)
        self.assertIsNone(profile.space)

    def test_unknown_and_nonfinite_values_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            parse_render_profile({"hidden_compressor": True})
        with self.assertRaisesRegex(ValueError, "未知的 space 参数"):
            parse_render_profile(
                {
                    "space": {
                        "enabled": False,
                        "config": {"hidden_reverb_mode": "surprise"},
                    }
                }
            )
        with self.assertRaisesRegex(ValueError, "finite"):
            parse_render_profile({"master_gain_db": math.inf})

    def test_non_none_overrides_revalidate_profile(self) -> None:
        profile = profile_with_overrides(
            RenderProfile(),
            expression="strict",
            write_stems=False,
            space=False,
        )
        self.assertEqual(profile.expression, "strict")
        self.assertFalse(profile.write_stems)
        self.assertIsNone(profile.space)


if __name__ == "__main__":
    unittest.main()
