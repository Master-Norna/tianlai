from __future__ import annotations

from pathlib import Path
import tomllib
import unittest

import tianlai


class PackageVersionTests(unittest.TestCase):
    def test_runtime_and_package_metadata_match(self) -> None:
        project = Path(__file__).resolve().parents[1]
        metadata = tomllib.loads(
            (project / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(tianlai.__version__, metadata["project"]["version"])
        self.assertEqual(tianlai.__version__, "0.7.0rc1")


if __name__ == "__main__":
    unittest.main()
