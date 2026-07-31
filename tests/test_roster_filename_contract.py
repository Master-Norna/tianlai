from __future__ import annotations

import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator

from tianlai.capability import InstrumentCapability
from tianlai.roster import parse_roster_document


ROOT = Path(__file__).resolve().parents[1]
CAPABILITY = InstrumentCapability(
    name="测试乐器",
    relative_path="测试乐器",
    manifest_path="测试乐器/乐器.json",
    implementation_type="oscillator",
    pitched=True,
    note_min=0.0,
    note_max=127.0,
    articulations=("sustain",),
    default_articulation="sustain",
    articulation_source="test",
    onset_seconds=None,
    quality_tier="candidate",
    license_status="approved",
)
CAPABILITIES = {"测试乐器": CAPABILITY}


def _instrument(part: str, executor_id: str) -> dict[str, str]:
    return {
        "part": part,
        "executor_id": executor_id,
        "instrument": "测试乐器",
    }


def _parse(assignments: list[dict]) -> object:
    return parse_roster_document(
        {"assignments": assignments},
        CAPABILITIES,
    )


class PortableExecutorFilenameTests(unittest.TestCase):
    def test_ascii_case_collision_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "violin.*Violin.*Unicode NFC.*大小写",
        ):
            _parse(
                [
                    _instrument("upper", "Violin"),
                    _instrument("lower", "violin"),
                ]
            )

    def test_unicode_normalization_and_fullwidth_case_collisions_are_rejected(
        self,
    ) -> None:
        cases = (
            ("Café", "Cafe\u0301"),
            ("Ｖｉｏｌｉｎ", "ｖｉｏｌｉｎ"),
            ("バイオリン", "ハ\u3099イオリン"),
        )
        for first, second in cases:
            with self.subTest(first=first, second=second):
                with self.assertRaisesRegex(ValueError, "Unicode NFC.*大小写"):
                    _parse(
                        [
                            _instrument("part-a", first),
                            _instrument("part-b", second),
                        ]
                    )

    def test_every_windows_reserved_device_basename_is_rejected(self) -> None:
        names = [
            "CON",
            "prn.txt",
            "Aux.WAV",
            "nul.json",
            *(f"COM{number}.wav" for number in range(1, 10)),
            *(f"lpt{number}" for number in range(1, 10)),
        ]
        for name in names:
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    ValueError,
                    "Windows 保留设备名",
                ):
                    _parse([_instrument("part", name)])

    def test_trailing_dot_or_space_is_rejected_instead_of_trimmed(self) -> None:
        for name in ("violin.", "violin "):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "不能以.*结尾"):
                    _parse([_instrument("part", name)])

    def test_existing_forbidden_characters_remain_rejected(self) -> None:
        for name in ("violin/name", "violin\x1f"):
            with self.subTest(name=repr(name)):
                with self.assertRaisesRegex(
                    ValueError,
                    "不能用作文件名的字符",
                ):
                    _parse([_instrument("part", name)])

    def test_kit_expansion_uses_the_same_portable_collision_key(self) -> None:
        cases = (
            [
                _instrument("melody", "DRUMS.C2"),
                {
                    "part": "drums",
                    "kit": {"C2": "测试乐器"},
                },
            ],
            [
                {
                    "part": "Drums",
                    "kit": {"C2": "测试乐器"},
                },
                {
                    "part": "drums",
                    "kit": {"C2": "测试乐器"},
                },
            ],
        )
        for assignments in cases:
            with self.subTest(assignments=assignments):
                with self.assertRaisesRegex(
                    ValueError,
                    "drums.C2.*Unicode NFC",
                ):
                    _parse(assignments)

    def test_legal_chinese_executor_ids_are_preserved(self) -> None:
        roster = _parse(
            [
                _instrument("主旋律", "第一小提琴·独奏"),
                {
                    "part": "鼓组",
                    "kit": {"C2": "测试乐器"},
                },
            ]
        )
        self.assertEqual(
            [executor.executor_id for executor in roster.executors],
            ["第一小提琴·独奏", "鼓组.C2"],
        )

    def test_schema_documents_the_runtime_filename_contract(self) -> None:
        schema = json.loads(
            (ROOT / "schemas" / "roster.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(schema)
        assignment = schema["properties"]["assignments"]["items"]
        part = assignment["properties"]["part"]
        executor_id = assignment["properties"]["executor_id"]
        self.assertEqual(part["pattern"], executor_id["pattern"])
        self.assertIn("Unicode NFC + casefold", executor_id["description"])
        self.assertIn("Windows 保留设备名", executor_id["description"])

        validator = Draft202012Validator(schema)
        for name in ("violin.", "violin ", "violin/name", "violin\x1f"):
            with self.subTest(name=repr(name)):
                document = {
                    "assignments": [_instrument("part", name)],
                }
                self.assertTrue(list(validator.iter_errors(document)))


if __name__ == "__main__":
    unittest.main()
