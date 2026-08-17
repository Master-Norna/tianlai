from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_bytes,
    canonical_json_file_sha256,
    canonical_json_sha256,
)


class CanonicalJsonTests(unittest.TestCase):
    def test_contract_identifiers_are_explicit(self) -> None:
        self.assertEqual(HASH_ALGORITHM, "SHA-256")
        self.assertEqual(CANONICALIZATION, "tianlai-json-v1")

    def test_layout_key_order_and_line_endings_do_not_change_identity(self) -> None:
        first = {"中文": [1, 2], "nested": {"b": False, "a": None}}
        second = {"nested": {"a": None, "b": False}, "中文": [1, 2]}
        self.assertEqual(
            canonical_json_sha256(first),
            canonical_json_sha256(second),
        )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lf = root / "lf.json"
            crlf = root / "crlf.json"
            text = json.dumps(first, ensure_ascii=False, indent=2)
            lf.write_bytes((text + "\n").encode("utf-8"))
            crlf.write_bytes(
                (text.replace("\n", "\r\n") + "\r\n").encode("utf-8")
            )
            self.assertEqual(
                canonical_json_file_sha256(lf),
                canonical_json_file_sha256(crlf),
            )

    def test_canonical_bytes_are_compact_utf8_with_sorted_keys(self) -> None:
        self.assertEqual(
            canonical_json_bytes({"z": 1, "啊": 2}),
            '{"z":1,"啊":2}'.encode("utf-8"),
        )

    def test_identity_is_document_exact_not_cross_schema_semantics(self) -> None:
        self.assertNotEqual(
            canonical_json_sha256({"value": 1}),
            canonical_json_sha256({"value": 1.0}),
        )
        self.assertNotEqual(
            canonical_json_sha256({}),
            canonical_json_sha256({"defaulted": False}),
        )

    def test_nonfinite_numbers_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json_sha256({"bad": float("nan")})

    def test_file_identity_rejects_duplicate_object_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.json"
            path.write_text('{"same":1,"same":2}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                canonical_json_file_sha256(path)


if __name__ == "__main__":
    unittest.main()
