"""Verify one profile projects matching resource, conductor and engine inputs."""

from tianlai.conductor import ExpressionSettings
from tianlai.render_profile import parse_render_profile, profile_with_overrides


def test_profile_projections_preserve_disabled_options_and_defaults():
    for profile in (
        parse_render_profile(),
        profile_with_overrides(
            parse_render_profile({"normalize_peak_db": None}),
            seed=17, expression="strict", range_mode="strict_hq",
            space=False, write_stems=False, use_stem_cache=False,
            collaboration_mode="manual", master_gain_db=-4,
            refresh_stem_cache=True,
        ),
    ):
        engine = profile.render_options()
        preflight = profile.preflight_options()
        assert preflight == {
            "write_stems": engine["write_stems"],
            "space": engine["space"],
            "collaboration_mode": engine["collaboration_mode"],
            "stem_cache_enabled": profile.use_stem_cache,
        }
        assert engine == {
            "write_stems": profile.write_stems,
            "space": profile.space,
            "collaboration_mode": profile.collaboration_mode,
            "master_gain_db": profile.master_gain_db,
            "normalize_peak_db": profile.normalize_peak_db,
            "refresh_stem_cache": profile.refresh_stem_cache,
        }
        expected = ExpressionSettings.from_dict({
            "mode": profile.expression,
            "range_mode": profile.range_mode,
            "humanize": {"seed": profile.seed},
        })
        assert ExpressionSettings.from_dict(profile.expression_options()) == expected
        engine["write_stems"] = not profile.write_stems
        assert profile.render_options()["write_stems"] == profile.write_stems
