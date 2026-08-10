from __future__ import annotations

import copy
import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

from jsonschema import Draft202012Validator

from tianlai.dedicated_fx import DedicatedFxInstrument
from tianlai.dedicated_sfz import DedicatedSfzInstrument


ROOT = Path(__file__).resolve().parents[1]


def _write_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(struct.pack("<h", 8_192) * 64)


def _write_fixture(directory: Path) -> dict[str, object]:
    asset_root = directory / "专用 音源"
    _write_wav(asset_root / "attack.wav")
    _write_wav(asset_root / "release.wav")
    for name in ("normal", "accent"):
        (asset_root / f"{name}.sfz").write_text(
            "<region> sample=attack.wav key=60\n"
            "<region> sample=release.wav key=60 trigger=release\n",
            encoding="utf-8",
        )
    return {
        "type": "dedicated_sfz",
        "asset_root": "专用 音源",
        "articulations": {
            "normal": "normal.sfz",
            "accent": {"sfz": "accent.sfz"},
        },
        "default_articulation": "normal",
        "pitch_mode": "pitched",
        "note_min": 60,
        "note_max": 60,
    }


def _qualities(
    instrument: DedicatedSfzInstrument,
    articulation: str,
) -> set[str]:
    runtime = instrument.articulations[articulation]
    return {
        layer.engine.resampling_quality
        for layer in (*runtime.attack_layers, *runtime.release_layers)
    }


def _component_hash(
    instrument: DedicatedSfzInstrument,
    articulation: str,
) -> str:
    engine = instrument.articulations[articulation].attack_layers[0].engine
    contract = engine.runtime_variant_contract()
    return str(contract["expected_component_sha256s"][0])


class DedicatedResamplingContractTests(unittest.TestCase):
    def test_default_remains_linear_and_manifest_value_reaches_every_layer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _write_fixture(directory)

            legacy = DedicatedSfzInstrument(8_000, manifest, str(directory))
            self.assertEqual(_qualities(legacy, "normal"), {"linear"})
            self.assertEqual(_qualities(legacy, "accent"), {"linear"})

            upgraded = DedicatedSfzInstrument(
                8_000,
                {**manifest, "resampling_quality": "bandlimited"},
                str(directory),
            )
            self.assertEqual(_qualities(upgraded, "normal"), {"bandlimited"})
            self.assertEqual(_qualities(upgraded, "accent"), {"bandlimited"})

    def test_single_sfz_shorthand_receives_manifest_quality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _write_fixture(directory)
            manifest.pop("articulations")
            manifest["sfz"] = "normal.sfz"
            manifest["default_articulation"] = "normal"
            manifest["resampling_quality"] = "bandlimited"

            instrument = DedicatedSfzInstrument(8_000, manifest, str(directory))

            self.assertEqual(_qualities(instrument, "normal"), {"bandlimited"})

    def test_invalid_manifest_value_fails_before_playback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _write_fixture(directory)
            with self.assertRaisesRegex(
                ValueError,
                "manifest resampling_quality",
            ):
                DedicatedSfzInstrument(
                    8_000,
                    {**manifest, "resampling_quality": "cubic"},
                    str(directory),
                )

    def test_quality_is_bound_to_sample_runtime_component_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _write_fixture(directory)
            linear = DedicatedSfzInstrument(8_000, manifest, str(directory))
            bandlimited = DedicatedSfzInstrument(
                8_000,
                {**manifest, "resampling_quality": "bandlimited"},
                str(directory),
            )

            self.assertNotEqual(
                _component_hash(linear, "normal"),
                _component_hash(bandlimited, "normal"),
            )

    def test_dedicated_fx_propagates_the_same_core_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            manifest = _write_fixture(directory)
            manifest.update(
                {
                    "type": "dedicated_fx",
                    "resampling_quality": "bandlimited",
                    "effects": [{"type": "lowpass", "cutoff_hz": 2_000}],
                }
            )

            instrument = DedicatedFxInstrument(8_000, manifest, str(directory))

            self.assertEqual(
                _qualities(instrument.core, "normal"),
                {"bandlimited"},
            )
            self.assertEqual(
                _qualities(instrument.core, "accent"),
                {"bandlimited"},
            )


class DedicatedResamplingSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "instrument.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        cls.validator = Draft202012Validator(schema)
        manifests = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((ROOT / "乐器").rglob("乐器.json"))
        ]
        cls.fixtures = {
            kind: next(
                copy.deepcopy(manifest)
                for manifest in manifests
                if manifest.get("type") == kind
            )
            for kind in ("dedicated_sfz", "dedicated_fx")
        }

    def _errors(self, manifest: dict[str, object]) -> list[object]:
        return list(self.validator.iter_errors(manifest))

    def test_manifest_quality_is_supported_by_sfz_and_fx(self) -> None:
        for kind, original in self.fixtures.items():
            for quality in ("linear", "bandlimited"):
                with self.subTest(kind=kind, quality=quality):
                    fixture = copy.deepcopy(original)
                    fixture["resampling_quality"] = quality
                    self.assertFalse(self._errors(fixture))
            invalid = copy.deepcopy(original)
            invalid["resampling_quality"] = "cubic"
            self.assertTrue(self._errors(invalid))

    def test_articulation_cannot_silently_override_quality(self) -> None:
        fixture = copy.deepcopy(self.fixtures["dedicated_sfz"])
        articulation = str(fixture["default_articulation"])
        raw_spec = fixture["articulations"][articulation]
        sfz = raw_spec if isinstance(raw_spec, str) else raw_spec["sfz"]
        fixture["articulations"][articulation] = {
            "sfz": sfz,
            "resampling_quality": "linear",
        }

        self.assertTrue(self._errors(fixture))

if __name__ == "__main__":
    unittest.main()
