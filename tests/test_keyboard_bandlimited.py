from __future__ import annotations

import json
from pathlib import Path
import unittest

import pytest

from tianlai.canonical_json import canonical_json_file_sha256
from tianlai.instrument import create_instrument


ROOT = Path(__file__).resolve().parents[1]
KEYBOARD_ROOT = ROOT / "乐器" / "键盘乐器"
TARGETS = {
    "电钢琴": "examples/电钢琴_奏法.events.json",
    "合唱电钢琴": "examples/合唱电钢琴_奏法.events.json",
    "羽管键琴": "examples/羽管键琴_奏法.events.json",
}


def _load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError(f"JSON root is not an object: {path}")
    return document


def _sample_core(instrument: object) -> object:
    return getattr(instrument, "core", instrument)


class KeyboardBandlimitedPortableContractTests(unittest.TestCase):
    def test_target_manifests_explicitly_enable_bandlimited_replay(self) -> None:
        for name in TARGETS:
            with self.subTest(instrument=name):
                manifest = _load_json(KEYBOARD_ROOT / name / "乐器.json")
                self.assertEqual(
                    manifest.get("resampling_quality"),
                    "bandlimited",
                )
                self.assertIn(
                    "bandlimited",
                    str(manifest.get("upgrade_status", "")),
                )

        for name in ("电钢琴", "合唱电钢琴"):
            manifest = _load_json(KEYBOARD_ROOT / name / "乐器.json")
            attribution = str(manifest.get("attribution", ""))
            self.assertIn(
                "https://creativecommons.org/licenses/by/3.0/",
                attribution,
            )
            self.assertIn("remain unmodified", attribution)

    def test_specialized_scripts_cannot_overwrite_full_range_evidence(
        self,
    ) -> None:
        for name in TARGETS:
            with self.subTest(instrument=name):
                script = (
                    KEYBOARD_ROOT / name / "核验试听.py"
                ).read_text(encoding="utf-8")
                self.assertIn(
                    'report_path = here / "表现力试听核验.json"',
                    script,
                )
                self.assertNotIn(
                    'output_path=here / "试听核验.json"',
                    script,
                )

    def test_full_range_and_expressive_reports_bind_current_inputs(self) -> None:
        for name, expected_events in TARGETS.items():
            directory = KEYBOARD_ROOT / name
            manifest_path = directory / "乐器.json"
            reports = {
                "full_range": _load_json(directory / "试听核验.json"),
                "expressive": _load_json(
                    directory / "表现力试听核验.json"
                ),
            }
            with self.subTest(instrument=name):
                self.assertNotEqual(
                    reports["full_range"]["events"],
                    reports["expressive"]["events"],
                )
                self.assertEqual(
                    reports["expressive"]["events"],
                    expected_events,
                )
            for role, report in reports.items():
                with self.subTest(instrument=name, evidence=role):
                    events_path = ROOT / str(report["events"])
                    self.assertEqual(
                        report["manifest_canonical_sha256"],
                        canonical_json_file_sha256(manifest_path),
                    )
                    self.assertEqual(
                        report["events_canonical_sha256"],
                        canonical_json_file_sha256(events_path),
                    )
                    self.assertEqual(report["sample_rate"], 48_000)
                    self.assertEqual(report["channels"], 2)
                    self.assertEqual(report["subtype"], "PCM_24")
                    self.assertEqual(report["clipped_samples"], 0)
                    self.assertGreater(float(report["rms"]), 0.001)

    @pytest.mark.external_assets
    def test_every_real_attack_and_release_layer_uses_bandlimited_replay(
        self,
    ) -> None:
        for name in TARGETS:
            manifest_path = KEYBOARD_ROOT / name / "乐器.json"
            manifest = _load_json(manifest_path)
            asset_root = (
                manifest_path.parent / str(manifest["asset_root"])
            ).resolve()
            if not asset_root.is_dir():
                self.skipTest(f"resource is not installed: {asset_root}")
            instrument = create_instrument(
                manifest,
                48_000,
                base_directory=str(manifest_path.parent),
            )
            core = _sample_core(instrument)
            try:
                for articulation, runtime in core.articulations.items():
                    layers = (*runtime.attack_layers, *runtime.release_layers)
                    self.assertTrue(layers)
                    with self.subTest(
                        instrument=name,
                        articulation=articulation,
                    ):
                        self.assertEqual(
                            {
                                layer.engine.resampling_quality
                                for layer in layers
                            },
                            {"bandlimited"},
                        )
            finally:
                close = getattr(instrument, "close", None)
                if callable(close):
                    close()


if __name__ == "__main__":
    unittest.main()
