"""Freeze decoded sample-memory bounds for managed stem workers.

This is a release-maintenance tool, not a render option.  It constructs each
built-in catalogue instrument without a local Python implementation, walks
the exact sample inventory retained by its runtime, and records the worst-case
float32 stereo payload in the existing resource-verification document.

Run without ``--write`` to check the current catalogue.  ``--write`` performs
the one-time mechanical sidecar update; normal users never run this tool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.audio import audio_file_info  # noqa: E402
from tianlai.instrument import create_instrument  # noqa: E402
from tianlai.instrument_audit import collect_loaded_samples  # noqa: E402
from tianlai.runtime_layout import discover_runtime_layout  # noqa: E402


_MANIFEST_NAME = "乐器.json"
_DEFAULT_RESOURCE_REPORT = "资源核验.json"
_DECODED_FIELD = "decoded_float32_stereo_bytes"
_ALGORITHM_FIELD = "decoded_float32_stereo_algorithm"
_SAMPLE_BYTES_ALGORITHM_FIELD = "sample_bytes_upper_bound_algorithm"
_VARIANT_BOUNDS_FIELD = "managed_runtime_variant_bounds"
_VARIANT_REPORTS_FIELD = "variants"
_SAMPLE_SET_ALGORITHM_FIELD = "sample_set_hash_algorithm"
_ALGORITHM = (
    "sum unique runtime sample frame_count * 2 output channels * 4-byte "
    "float32; mono sources are expanded to stereo by read_audio_float"
)
_TRUSTED_RUNTIME_VARIANT_TYPES = frozenset({"violin", "vpo_solo_string"})
_TRUSTED_RUNTIME_VARIANTS = ("SOLO", "SEC")
_VARIANT_SAMPLE_BYTES_ALGORITHM = (
    "max over trusted runtime variants {SOLO, SEC} of sum unique runtime "
    "sample file byte sizes; variants are mutually exclusive at run time"
)
_VARIANT_DECODED_ALGORITHM = (
    "max over trusted runtime variants {SOLO, SEC} of sum unique runtime "
    "sample frame_count * 2 output channels * 4-byte float32; mono sources "
    "are expanded to stereo by read_audio_float; variants are mutually "
    "exclusive at run time"
)
_SAMPLE_SET_ALGORITHM = (
    "sort unique asset-root-relative UTF-8 paths; concatenate lowercase "
    "'<sha256>  <path>\\n>'; SHA-256 the UTF-8 bytes"
)


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return value


def _runtime_sample_paths(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> tuple[Path, ...]:
    instrument = create_instrument(
        manifest,
        48_000,
        base_directory=str(manifest_path.parent),
    )
    try:
        paths = tuple(collect_loaded_samples(instrument))
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()

    if not paths and manifest.get("type") == "reversed_cymbal":
        asset_root = (
            manifest_path.parent / str(manifest["asset_root"])
        ).resolve(strict=True)
        variants = manifest.get("variants")
        if not isinstance(variants, dict):
            raise ValueError("reversed cymbal variants must be an object")
        paths = tuple(
            sorted(
                {
                    (
                        asset_root
                        / str(spec["sample"])
                    ).resolve(strict=True)
                    for spec in variants.values()
                    if isinstance(spec, dict) and "sample" in spec
                },
                key=lambda path: path.as_posix(),
            )
        )
    if not paths:
        raise ValueError("constructed sample instrument exposed no samples")
    return paths


def _decoded_float32_stereo_bytes(paths: tuple[Path, ...]) -> int:
    total = 0
    for path in paths:
        _sample_rate, frame_count, channels = audio_file_info(path)
        if channels not in (1, 2):
            raise ValueError(
                f"sample has unsupported channel count {channels}: {path}"
            )
        total += frame_count * 2 * 4
    if total <= 0:
        raise ValueError("decoded sample inventory is empty")
    return total


def _trusted_variant_source_sfz_paths(
    manifest_path: Path,
    manifest: dict[str, Any],
    variant: str,
) -> tuple[Path, tuple[Path, ...]]:
    asset_root = (
        manifest_path.parent / str(manifest["asset_root"])
    ).resolve(strict=True)
    string_root = asset_root / "Strings"
    instrument_type = manifest.get("type")
    if instrument_type == "violin":
        from tianlai.violin import _SFZ_BY_VARIANT

        names = tuple(dict.fromkeys(_SFZ_BY_VARIANT[variant].values()))
        paths = tuple(string_root / name for name in names)
    elif instrument_type == "vpo_solo_string":
        prefix = str(manifest["sfz_prefix"])
        paths = tuple(
            string_root / f"{prefix}-{variant}-{articulation}.sfz"
            for articulation in ("sustain", "staccato", "pizzicato", "accent")
        )
    else:
        raise ValueError(
            f"manifest type has no trusted runtime variants: {instrument_type}"
        )
    return asset_root, tuple(path.resolve(strict=True) for path in paths)


def _trusted_runtime_variant_evidence(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    instrument_type = manifest.get("type")
    if instrument_type not in _TRUSTED_RUNTIME_VARIANT_TYPES:
        raise ValueError(
            f"manifest type has no trusted runtime variants: {instrument_type}"
        )
    evidence: dict[str, dict[str, Any]] = {}
    for variant in _TRUSTED_RUNTIME_VARIANTS:
        variant_manifest = dict(manifest)
        variant_manifest["sample_variant"] = variant
        paths = _runtime_sample_paths(manifest_path, variant_manifest)
        asset_root, sfz_paths = _trusted_variant_source_sfz_paths(
            manifest_path,
            manifest,
            variant,
        )
        sample_bytes = sum(path.stat().st_size for path in paths)
        decoded_bytes = _decoded_float32_stereo_bytes(paths)
        if sample_bytes <= 0:
            raise ValueError(f"runtime variant has no sample bytes: {variant}")
        sample_hashes: dict[str, str] = {}
        for path in paths:
            relative = path.relative_to(asset_root).as_posix()
            sample_hashes[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        hash_lines = (
            f"{sample_hashes[relative]}  {relative}\n"
            for relative in sorted(sample_hashes)
        )
        evidence[variant] = {
            "sample_count": len(paths),
            "sample_bytes": sample_bytes,
            _DECODED_FIELD: decoded_bytes,
            "source_sfz_sha256": {
                path.relative_to(asset_root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sfz_paths
            },
            "sample_set_sha256": hashlib.sha256(
                "".join(hash_lines).encode("utf-8")
            ).hexdigest(),
            _SAMPLE_SET_ALGORITHM_FIELD: _SAMPLE_SET_ALGORITHM,
        }
    return evidence


def _trusted_runtime_variant_bounds(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> dict[str, dict[str, int]]:
    evidence = _trusted_runtime_variant_evidence(manifest_path, manifest)
    return {
        variant: {
            "sample_count": int(item["sample_count"]),
            "sample_bytes": int(item["sample_bytes"]),
            _DECODED_FIELD: int(item[_DECODED_FIELD]),
        }
        for variant, item in evidence.items()
    }


def _with_decoded_evidence(
    report: dict[str, Any],
    decoded_bytes: int,
) -> dict[str, Any]:
    updated: dict[str, Any] = {}
    inserted = False
    for key, value in report.items():
        updated[key] = value
        if key == "sample_bytes":
            updated[_DECODED_FIELD] = decoded_bytes
            updated[_ALGORITHM_FIELD] = _ALGORITHM
            inserted = True
    if not inserted:
        raise ValueError("resource report has no sample_bytes field")
    return updated


def _with_variant_evidence(
    report: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    bounds = {
        variant: {
            "sample_count": int(item["sample_count"]),
            "sample_bytes": int(item["sample_bytes"]),
            _DECODED_FIELD: int(item[_DECODED_FIELD]),
        }
        for variant, item in evidence.items()
    }
    sample_bytes = max(item["sample_bytes"] for item in bounds.values())
    decoded_bytes = max(item[_DECODED_FIELD] for item in bounds.values())
    replacement = {
        "sample_bytes": sample_bytes,
        _SAMPLE_BYTES_ALGORITHM_FIELD: _VARIANT_SAMPLE_BYTES_ALGORITHM,
        _DECODED_FIELD: decoded_bytes,
        _ALGORITHM_FIELD: _VARIANT_DECODED_ALGORITHM,
        _VARIANT_BOUNDS_FIELD: bounds,
    }
    replaced_fields = frozenset(replacement)
    current_variants = report.get(_VARIANT_REPORTS_FIELD)
    if not isinstance(current_variants, dict):
        current_variants = {}
    variant_reports: dict[str, dict[str, Any]] = {}
    for variant, expected in evidence.items():
        current = current_variants.get(variant)
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(expected)
        variant_reports[variant] = merged
    updated: dict[str, Any] = {}
    inserted = False
    variants_inserted = False
    for key, value in report.items():
        if key == _VARIANT_REPORTS_FIELD:
            updated[key] = variant_reports
            variants_inserted = True
            continue
        if key in replaced_fields:
            if not inserted:
                updated.update(replacement)
                inserted = True
            continue
        updated[key] = value
        if not inserted and key == "evidence_sha256":
            updated.update(replacement)
            inserted = True
    if not inserted:
        updated.update(replacement)
    if not variants_inserted:
        updated[_VARIANT_REPORTS_FIELD] = variant_reports
    return updated


def _variant_reports_match(
    report: dict[str, Any],
    evidence: dict[str, dict[str, Any]],
) -> bool:
    variants = report.get(_VARIANT_REPORTS_FIELD)
    if not isinstance(variants, dict) or set(variants) != set(evidence):
        return False
    for variant, expected in evidence.items():
        actual = variants.get(variant)
        if not isinstance(actual, dict):
            return False
        if any(actual.get(key) != value for key, value in expected.items()):
            return False
    return True


def update_catalogue(catalog: Path, *, write: bool) -> tuple[int, int]:
    checked = 0
    stale = 0
    for manifest_path in sorted(
        catalog.rglob(_MANIFEST_NAME),
        key=lambda path: path.relative_to(catalog).as_posix(),
    ):
        manifest = _load_object(manifest_path)
        # Local factories are deliberately ineligible for managed workers;
        # do not create evidence that could be mistaken for such authority.
        if manifest.get("implementation") is not None:
            continue
        report_name = manifest.get(
            "resource_verification",
            _DEFAULT_RESOURCE_REPORT,
        )
        if not isinstance(report_name, str) or not report_name:
            continue
        report_path = manifest_path.with_name(report_name)
        if not report_path.is_file():
            continue
        report = _load_object(report_path)
        if manifest.get("type") in _TRUSTED_RUNTIME_VARIANT_TYPES:
            variant_evidence = _trusted_runtime_variant_evidence(
                manifest_path,
                manifest,
            )
            bounds = {
                variant: {
                    "sample_count": int(item["sample_count"]),
                    "sample_bytes": int(item["sample_bytes"]),
                    _DECODED_FIELD: int(item[_DECODED_FIELD]),
                }
                for variant, item in variant_evidence.items()
            }
            sample_bytes = max(
                item["sample_bytes"] for item in bounds.values()
            )
            decoded_bytes = max(
                item[_DECODED_FIELD] for item in bounds.values()
            )
            checked += 1
            if (
                report.get("sample_bytes") == sample_bytes
                and report.get(_SAMPLE_BYTES_ALGORITHM_FIELD)
                == _VARIANT_SAMPLE_BYTES_ALGORITHM
                and report.get(_DECODED_FIELD) == decoded_bytes
                and report.get(_ALGORITHM_FIELD) == _VARIANT_DECODED_ALGORITHM
                and report.get(_VARIANT_BOUNDS_FIELD) == bounds
                and _variant_reports_match(report, variant_evidence)
            ):
                continue
            stale += 1
            if write:
                updated = _with_variant_evidence(report, variant_evidence)
                report_path.write_text(
                    json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            else:
                print(
                    f"stale: {report_path.relative_to(catalog).as_posix()} "
                    f"expected sample_bytes={sample_bytes} "
                    f"{_DECODED_FIELD}={decoded_bytes} over "
                    f"{', '.join(_TRUSTED_RUNTIME_VARIANTS)}"
                )
            continue
        sample_bytes = report.get("sample_bytes")
        if (
            isinstance(sample_bytes, bool)
            or not isinstance(sample_bytes, int)
            or sample_bytes <= 0
        ):
            continue

        paths = _runtime_sample_paths(manifest_path, manifest)
        decoded_bytes = _decoded_float32_stereo_bytes(paths)
        checked += 1
        if (
            report.get(_DECODED_FIELD) == decoded_bytes
            and report.get(_ALGORITHM_FIELD) == _ALGORITHM
        ):
            continue
        stale += 1
        if write:
            updated = _with_decoded_evidence(report, decoded_bytes)
            report_path.write_text(
                json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            print(
                f"stale: {report_path.relative_to(catalog).as_posix()} "
                f"expected {_DECODED_FIELD}={decoded_bytes}"
            )
    return checked, stale


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--catalog", type=Path)
    arguments = parser.parse_args()
    catalog = (
        arguments.catalog.resolve(strict=True)
        if arguments.catalog is not None
        else discover_runtime_layout(require_catalog=True).catalog
    )
    checked, stale = update_catalogue(catalog, write=arguments.write)
    action = "updated" if arguments.write else "stale"
    print(f"checked={checked} {action}={stale}")
    return 0 if arguments.write or stale == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
