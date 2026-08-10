from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from tianlai.capability import load_capabilities
from tianlai.trust import TrustPolicyError, load_trusted_instruments


ROOT = Path(__file__).resolve().parents[1]


class TrustedInstrumentPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = load_capabilities(ROOT / "乐器")

    def _load(self, instrument: str, capabilities=None):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "trusted.json"
            path.write_text(
                json.dumps({"trusted": [instrument]}, ensure_ascii=False),
                encoding="utf-8",
            )
            return load_trusted_instruments(
                path,
                self.capabilities if capabilities is None else capabilities,
            )

    def test_formal_approved_entry_is_accepted(self) -> None:
        instrument = "世界乐器/班卓琴"
        self.assertEqual(self._load(instrument), {instrument})

    def test_non_formal_test_tool_is_rejected(self) -> None:
        with self.assertRaisesRegex(TrustPolicyError, "不是 formal"):
            self._load("测试工具/参考振荡器")

    def test_unknown_license_status_is_rejected(self) -> None:
        instrument = "世界乐器/班卓琴"
        capabilities = dict(self.capabilities)
        capabilities[instrument] = replace(
            capabilities[instrument],
            license_status=None,
        )
        with self.assertRaisesRegex(TrustPolicyError, "许可证状态"):
            self._load(instrument, capabilities)

    def test_quarantined_entry_is_rejected(self) -> None:
        instrument = "世界乐器/班卓琴"
        capabilities = dict(self.capabilities)
        capabilities[instrument] = replace(
            capabilities[instrument],
            license_status="quarantined",
        )
        with self.assertRaisesRegex(TrustPolicyError, "许可证据已隔离"):
            self._load(instrument, capabilities)

    def test_soundfont_entry_is_rejected(self) -> None:
        instrument = "世界乐器/班卓琴"
        capabilities = dict(self.capabilities)
        capabilities[instrument] = replace(
            capabilities[instrument],
            implementation_type="soundfont",
        )
        with self.assertRaisesRegex(TrustPolicyError, "SoundFont"):
            self._load(instrument, capabilities)


if __name__ == "__main__":
    unittest.main()
