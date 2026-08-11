"""逐 SOLO/SEC 复算低音提琴的 VPO 资源、许可与 Hash 证据。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_strings import generate_string_resource_audit
from tools.update_decoded_sample_evidence import (
    _ALGORITHM,
    _ALGORITHM_FIELD,
    _DECODED_FIELD,
    _SAMPLE_BYTES_ALGORITHM_FIELD,
    _SAMPLE_SET_ALGORITHM,
    _SAMPLE_SET_ALGORITHM_FIELD,
    _VARIANT_BOUNDS_FIELD,
    _VARIANT_DECODED_ALGORITHM,
    _VARIANT_SAMPLE_BYTES_ALGORITHM,
    _trusted_runtime_variant_evidence,
)


def _report_for(
    here: Path,
    manifest: dict[str, Any],
    variant: str,
    temporary: Path,
) -> dict[str, Any]:
    variant_manifest = dict(manifest)
    variant_manifest["sample_variant"] = variant
    temporary_manifest = here / f".variant-{variant}.json"
    temporary_manifest.write_text(
        json.dumps(variant_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        return generate_string_resource_audit(
            temporary_manifest,
            temporary / f"{variant}.json",
            license_files=tuple(str(item) for item in manifest["evidence_files"]),
        )
    finally:
        temporary_manifest.unlink(missing_ok=True)


def main() -> None:
    here = Path(__file__).resolve().parent
    manifest_path = here / "乐器.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="tianlai-bass-variant-audit-") as raw:
        temporary = Path(raw)
        reports = {
            variant: _report_for(here, manifest, variant, temporary)
            for variant in ("SOLO", "SEC")
        }

    evidence = _trusted_runtime_variant_evidence(manifest_path, manifest)
    for variant, report in reports.items():
        expected = evidence[variant]
        comparisons = {
            "sample_count": report.get("sample_count"),
            "sample_bytes": report.get("sample_bytes"),
            "sample_set_sha256": report.get("sample_set_sha256"),
            "source_sfz_sha256": report.get("source_sfz_sha256"),
        }
        if any(comparisons[key] != expected[key] for key in comparisons):
            raise ValueError(
                f"{variant} formal resource audit disagrees with runtime inventory"
            )

    managed_bounds = {
        variant: {
            "sample_count": int(item["sample_count"]),
            "sample_bytes": int(item["sample_bytes"]),
            _DECODED_FIELD: int(item[_DECODED_FIELD]),
        }
        for variant, item in evidence.items()
    }
    shared = reports["SOLO"]
    merged = {
        "upstream": shared["upstream"],
        "sfz_version": shared["sfz_version"],
        "wave_version": shared["wave_version"],
        "license_file_sha256": shared["license_file_sha256"],
        "version_evidence_sha256": shared["version_evidence_sha256"],
        "sample_bytes": max(item["sample_bytes"] for item in evidence.values()),
        _SAMPLE_BYTES_ALGORITHM_FIELD: _VARIANT_SAMPLE_BYTES_ALGORITHM,
        _DECODED_FIELD: max(item[_DECODED_FIELD] for item in evidence.values()),
        _ALGORITHM_FIELD: _VARIANT_DECODED_ALGORITHM,
        _VARIANT_BOUNDS_FIELD: managed_bounds,
        "variants": {
            variant: {
                "source_sfz_sha256": item["source_sfz_sha256"],
                "sample_count": int(item["sample_count"]),
                "sample_bytes": int(item["sample_bytes"]),
                _DECODED_FIELD: int(item[_DECODED_FIELD]),
                _ALGORITHM_FIELD: _ALGORITHM,
                "sample_set_sha256": item["sample_set_sha256"],
                _SAMPLE_SET_ALGORITHM_FIELD: _SAMPLE_SET_ALGORITHM,
            }
            for variant, item in evidence.items()
        },
    }
    (here / "资源核验.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    for variant, item in evidence.items():
        print(f"{variant}: {item['sample_count']} 个采样")


if __name__ == "__main__":
    main()
