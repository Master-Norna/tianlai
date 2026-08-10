from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from tianlai.instrument_audit import generate_sampled_pitch_calibration


class _Region:
    __slots__ = ("path", "root_pitch_hz")

    def __init__(self, path: Path, root_pitch_hz: float) -> None:
        self.path = path
        self.root_pitch_hz = root_pitch_hz


class _Instrument:
    __slots__ = ("regions",)

    def __init__(self, regions: list[_Region]) -> None:
        self.regions = regions


class SampledPitchCalibrationTests(unittest.TestCase):
    def _noncanonical_root(self, temporary: str) -> Path:
        """Return one physical directory through a portable lexical alias."""

        base = Path(temporary)
        (base / "detour").mkdir()
        (base / "fixture").mkdir()
        root = base / "detour" / ".." / "fixture"
        self.assertNotEqual(root, root.resolve())
        return root

    def _fixture(
        self,
        root: Path,
        relative_samples: list[str],
        *,
        include_globs: list[str] | None,
    ) -> tuple[Path, _Instrument]:
        asset_root = root / "assets"
        asset_root.mkdir()
        regions: list[_Region] = []
        for index, relative in enumerate(relative_samples):
            sample = asset_root.joinpath(*relative.split("/"))
            sample.parent.mkdir(parents=True, exist_ok=True)
            sample.touch()
            regions.append(_Region(sample, 220.0 * (index + 1)))

        manifest = {
            "name": "pitch calibration fixture",
            "type": "fixture",
            "asset_root": "assets",
            "pitch_calibration": "pitch.json",
        }
        if include_globs is not None:
            manifest["pitch_calibration_include_globs"] = include_globs
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return manifest_path, _Instrument(regions)

    def test_explicit_globs_filter_before_analysis_and_report_exclusions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._noncanonical_root(temporary)
            globs = ["Pitched/[A-Z]4.wav", "Pitched/**/C5.wav"]
            manifest_path, instrument = self._fixture(
                root,
                [
                    "Pitched/A4.wav",
                    "Pitched/nested/C5.wav",
                    "Pitched/b4.wav",
                    "Mechanical/key-release.wav",
                ],
                include_globs=globs,
            )
            analysed: list[str] = []
            canonical_asset_root = (root / "assets").resolve()

            def analyse(path: Path, root_hz: float, **_: object) -> object:
                relative = path.relative_to(canonical_asset_root).as_posix()
                analysed.append(relative)
                if relative.endswith("C5.wav"):
                    raise ValueError("fixture is intentionally too short")
                return SimpleNamespace(measured_hz=root_hz)

            with (
                mock.patch(
                    "tianlai.instrument_audit.create_instrument",
                    return_value=instrument,
                ),
                mock.patch(
                    "tianlai.analysis.analyze_file_harmonic_pitch",
                    side_effect=analyse,
                ),
            ):
                report = generate_sampled_pitch_calibration(manifest_path)

            self.assertEqual(
                analysed,
                ["Pitched/A4.wav", "Pitched/nested/C5.wav"],
            )
            self.assertEqual(report["pitch_calibration_include_globs"], globs)
            self.assertEqual(report["summary"]["sample_count"], 2)
            self.assertEqual(report["summary"]["measured_count"], 1)
            self.assertEqual(report["summary"]["skipped_count"], 1)
            self.assertEqual(report["summary"]["excluded_count"], 2)
            self.assertEqual(
                report["excluded_samples"],
                ["Mechanical/key-release.wav", "Pitched/b4.wav"],
            )
            self.assertEqual(report["skipped_samples"], ["Pitched/nested/C5.wav"])
            self.assertEqual(list(report["samples"]), ["Pitched/A4.wav"])

    def test_absent_globs_preserve_the_historical_report_shape_and_scope(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = self._noncanonical_root(temporary)
            manifest_path, instrument = self._fixture(
                root,
                ["Pitched/A4.wav", "Mechanical/key-release.wav"],
                include_globs=None,
            )
            analysed: list[str] = []
            canonical_asset_root = (root / "assets").resolve()

            def analyse(path: Path, root_hz: float, **_: object) -> object:
                analysed.append(path.relative_to(canonical_asset_root).as_posix())
                return SimpleNamespace(measured_hz=root_hz)

            with (
                mock.patch(
                    "tianlai.instrument_audit.create_instrument",
                    return_value=instrument,
                ),
                mock.patch(
                    "tianlai.analysis.analyze_file_harmonic_pitch",
                    side_effect=analyse,
                ),
            ):
                report = generate_sampled_pitch_calibration(manifest_path)

            self.assertEqual(
                analysed,
                ["Mechanical/key-release.wav", "Pitched/A4.wav"],
            )
            self.assertEqual(report["summary"]["sample_count"], 2)
            self.assertNotIn("excluded_count", report["summary"])
            self.assertNotIn("pitch_calibration_include_globs", report)
            self.assertNotIn("excluded_samples", report)

    def test_all_unanalysable_samples_fail_with_a_domain_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, instrument = self._fixture(
                root,
                ["Pitched/A4.wav", "Pitched/C5.wav"],
                include_globs=None,
            )

            with (
                mock.patch(
                    "tianlai.instrument_audit.create_instrument",
                    return_value=instrument,
                ),
                mock.patch(
                    "tianlai.analysis.analyze_file_harmonic_pitch",
                    side_effect=ValueError("fixture is intentionally too short"),
                ),
                self.assertRaisesRegex(
                    ValueError,
                    "音准校准未获得任何可靠测量.*2 个无法分析",
                ),
            ):
                generate_sampled_pitch_calibration(manifest_path)

            self.assertFalse((root / "pitch.json").exists())

    def test_explicit_globs_must_match_at_least_one_loaded_pitched_sample(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, instrument = self._fixture(
                root,
                ["Mechanical/key-release.wav"],
                include_globs=["Pitched/*.wav"],
            )
            with (
                mock.patch(
                    "tianlai.instrument_audit.create_instrument",
                    return_value=instrument,
                ),
                mock.patch(
                    "tianlai.analysis.analyze_file_harmonic_pitch"
                ) as analyse,
                self.assertRaisesRegex(
                    ValueError,
                    "未匹配任何已加载的有音高根采样",
                ),
            ):
                generate_sampled_pitch_calibration(manifest_path)

            analyse.assert_not_called()
            self.assertFalse((root / "pitch.json").exists())


if __name__ == "__main__":
    unittest.main()
