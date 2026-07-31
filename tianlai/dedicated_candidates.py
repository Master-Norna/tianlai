"""通用 dedicated_sfz 入口审计工具（模块名为历史兼容名）。

为直接采用上游 SFZ 映射的乐器(VCSL、FreePats、Karoryfer 等
有明确公开许可的库)生成三类机器可读证据:

- ``资源核验.json``:上游、版本、许可、逐文件 SHA-256 与样本统计;
- ``音准校准.json``:有音高乐器的根采样 FFT 诊断校准,或对无固定音高
  素材写明不适用理由,拒绝伪造十二平均律校准;
- ``试听核验.json``:固定事件渲染的峰值 / RMS / 削波 / Hash 报告。

与 ``mtg_sax`` / ``vpo_*`` 模块的报告字段保持一致,便于清单复查。
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
from typing import Any

from .audio import audio_file_info, wav_loop_points
from .canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from .dedicated_sfz import dedicated_regions_to_manifest, parse_dedicated_sfz
from .sfz import note_number


_REQUIRED_PROVENANCE_KEYS = ("upstream", "origin", "upstream_version", "license")


def _articulation_specs(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = manifest.get("articulations")
    if raw is None:
        if "sfz" not in manifest:
            raise ValueError("dedicated_sfz manifest requires sfz or articulations")
        name = str(manifest.get("default_articulation", "default"))
        raw = {name: {"sfz": manifest["sfz"]}}
    specs: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            specs[str(name)] = {"sfz": value}
        elif isinstance(value, dict):
            if "sfz" not in value:
                raise ValueError(f"articulation {name!r} requires sfz")
            specs[str(name)] = value
        else:
            raise ValueError(f"articulation {name!r} must be a path or object")
    return specs


def dedicated_manifest_sources(
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Parse every articulation SFZ referenced by a dedicated_sfz manifest."""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    if str(manifest.get("type", "")) not in ("dedicated_sfz", "dedicated_fx"):
        raise ValueError(
            f"manifest type must be dedicated_sfz or dedicated_fx: {source_manifest}"
        )
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    if not asset_root.is_dir():
        raise ValueError(f"asset_root does not exist: {asset_root}")

    fixed_midi = manifest.get("fixed_midi_note")
    articulations: dict[str, dict[str, Any]] = {}
    source_files: set[Path] = set()
    for name, spec in _articulation_specs(manifest).items():
        sfz_path = (asset_root / str(spec["sfz"])).resolve()
        document = parse_dedicated_sfz(sfz_path, asset_root=asset_root)
        source_files.update(document.source_files)
        raw_keyswitch = spec.get("keyswitch_select", manifest.get("keyswitch_select"))
        keyswitch = note_number(raw_keyswitch) if raw_keyswitch is not None else None
        attack, _ = dedicated_regions_to_manifest(
            sfz_path,
            asset_root=asset_root,
            trigger="attack",
            stable_prefix="audit",
            root_midi_fallback=(
                float(fixed_midi) if fixed_midi is not None else None
            ),
            keyswitch_select=keyswitch,
        )
        release, _ = dedicated_regions_to_manifest(
            sfz_path,
            asset_root=asset_root,
            trigger="release",
            stable_prefix="audit:release",
            root_midi_fallback=(
                float(fixed_midi) if fixed_midi is not None else None
            ),
            keyswitch_select=keyswitch,
        )
        articulations[name] = {
            "sfz": sfz_path,
            "attack_regions": attack,
            "release_regions": release,
        }
    return {
        "manifest": manifest,
        "manifest_path": source_manifest,
        "asset_root": asset_root,
        "articulations": articulations,
        "source_files": sorted(source_files),
    }


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _region_has_effective_loop(
    region: dict[str, Any],
    frame_counts: dict[Path, int],
) -> bool:
    """Return whether a region has an active, valid loop boundary pair.

    ``dedicated_regions_to_manifest`` enables embedded-loop lookup for sustain
    regions by default.  That flag and ``loop_mode`` are policies, not proof
    that the referenced sample actually contains loop points.  Resource
    verification therefore resolves the explicit or embedded boundaries and
    checks them against the decoded sample length before counting the region.
    """

    if region.get("loop_mode") not in ("loop_sustain", "loop_continuous"):
        return False

    sample_path = Path(region["sample"])
    frame_count = frame_counts.get(sample_path)
    if frame_count is None:
        _, frame_count, _ = audio_file_info(sample_path)
        frame_counts[sample_path] = frame_count

    has_start = "loop_start" in region
    has_end = "loop_end" in region
    if has_start or has_end:
        if not (has_start and has_end):
            return False
        start = region["loop_start"]
        end = region["loop_end"]
    elif bool(region.get("use_embedded_loop", False)):
        embedded = wav_loop_points(sample_path)
        if embedded is None:
            return False
        start, end = embedded
    else:
        return False

    return (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and 0 <= start < end <= frame_count
    )


def generate_dedicated_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Hash every SFZ source, referenced sample and licence evidence file."""

    inventory = dedicated_manifest_sources(manifest_path)
    manifest = inventory["manifest"]
    source_manifest: Path = inventory["manifest_path"]
    asset_root: Path = inventory["asset_root"]
    for key in _REQUIRED_PROVENANCE_KEYS:
        if not str(manifest.get(key, "")).strip():
            raise ValueError(f"manifest must record provenance field {key!r}: {source_manifest}")

    all_regions: list[dict[str, Any]] = []
    articulation_summary: dict[str, dict[str, int]] = {}
    loop_frame_counts: dict[Path, int] = {}
    for name, data in inventory["articulations"].items():
        regions = list(data["attack_regions"]) + list(data["release_regions"])
        all_regions.extend(regions)
        articulation_summary[name] = {
            "attack_regions": len(data["attack_regions"]),
            "release_regions": len(data["release_regions"]),
            "round_robin_regions": sum(
                1 for item in data["attack_regions"] if "round_robin_length" in item
            ),
            "velocity_bounded_regions": sum(
                1
                for item in data["attack_regions"]
                if float(item.get("velocity_min", 0.0)) > 0.0
                or float(item.get("velocity_max", 1.0)) < 1.0
            ),
            "random_variant_regions": sum(
                1
                for item in data["attack_regions"]
                if item.get("_dedicated_has_random_range")
            ),
            "looped_regions": sum(
                1
                for item in regions
                if _region_has_effective_loop(item, loop_frame_counts)
            ),
        }

    sample_paths = sorted(
        {Path(item["sample"]) for item in all_regions},
        key=lambda item: _relative(item, asset_root),
    )
    if not sample_paths:
        raise ValueError(f"no samples referenced by manifest: {source_manifest}")
    sample_lines: list[str] = []
    sample_bytes = 0
    formats: dict[str, int] = {}
    for sample_path in sample_paths:
        digest = hashlib.sha256(sample_path.read_bytes()).hexdigest()
        sample_lines.append(f"{digest}  {_relative(sample_path, asset_root)}\n")
        sample_bytes += sample_path.stat().st_size
        sample_rate, _, channels = audio_file_info(sample_path)
        key = f"{sample_path.suffix.lower()}:{sample_rate}Hz:{channels}ch"
        formats[key] = formats.get(key, 0) + 1

    source_hashes = {
        _relative(path, asset_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inventory["source_files"]
    }
    evidence_hashes: dict[str, str] = {}
    for relative in manifest.get("evidence_files", []):
        path = asset_root / str(relative)
        if not path.is_file():
            raise ValueError(f"licence/evidence file is missing: {path}")
        evidence_hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not evidence_hashes:
        raise ValueError(
            f"manifest must list evidence_files (licence/readme) to freeze: {source_manifest}"
        )

    report: dict[str, Any] = {
        "upstream": str(manifest["upstream"]),
        "origin": str(manifest["origin"]),
        "upstream_version": str(manifest["upstream_version"]),
        "license": str(manifest["license"]),
        "source_file_count": len(source_hashes),
        "source_file_sha256": source_hashes,
        "evidence_sha256": evidence_hashes,
        "sample_count": len(sample_paths),
        "sample_bytes": sample_bytes,
        "sample_formats": formats,
        "articulations": articulation_summary,
        "region_count": len(all_regions),
        "sample_set_sha256": hashlib.sha256(
            "".join(sample_lines).encode("utf-8")
        ).hexdigest(),
        "sample_set_hash_algorithm": (
            "sort unique asset-root-relative UTF-8 paths; concatenate lowercase "
            "'<sha256>  <path>\\n>'; SHA-256 the UTF-8 bytes"
        ),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("resource_verification", "资源核验.json"))
    )
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def generate_dedicated_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Measure unique pitched root samples, or record an explicit N/A reason."""

    inventory = dedicated_manifest_sources(manifest_path)
    manifest = inventory["manifest"]
    source_manifest: Path = inventory["manifest_path"]
    asset_root: Path = inventory["asset_root"]
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / str(manifest.get("pitch_calibration", "音准校准.json"))
    )

    pitch_mode = str(manifest.get("pitch_mode", "pitched")).lower()
    if pitch_mode != "pitched":
        reason = str(manifest.get("calibration_not_applicable_reason", "")).strip()
        if not reason:
            raise ValueError(
                "non-pitched manifests must record calibration_not_applicable_reason: "
                f"{source_manifest}"
            )
        document: dict[str, Any] = {
            "applicable": False,
            "pitch_mode": pitch_mode,
            "reason": reason,
            "samples": {},
        }
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return document

    from .analysis import analyze_file_harmonic_pitch

    reference_a4 = float(manifest.get("reference_a4_hz", 440.0))
    start_seconds = float(manifest.get("calibration_start_seconds", 0.08))
    search_cents = float(manifest.get("calibration_search_cents", 180.0))
    harmonic_count = int(manifest.get("calibration_harmonic_count", 10))

    claims: dict[Path, list[tuple[float, float]]] = {}
    for data in inventory["articulations"].values():
        for region in data["attack_regions"]:
            sample = Path(region["sample"])
            root = float(region["root_midi"])
            claim = float(region.get("measured_tuning_cents", 0.0))
            claims.setdefault(sample, []).append((round(root, 6), round(claim, 6)))
    # Octave-stretched mappings may reuse one sample under several root/tune
    # claims; calibrate against the most frequent claim (ties: smallest |tune|)
    # and record how many distinct claims exist.
    targets: dict[Path, tuple[float, float, int]] = {}
    for sample, pairs in claims.items():
        counts: dict[tuple[float, float], int] = {}
        for pair in pairs:
            counts[pair] = counts.get(pair, 0) + 1
        best = min(counts, key=lambda pair: (-counts[pair], abs(pair[1]), pair[0]))
        targets[sample] = (best[0], best[1], len(counts))

    samples: dict[str, dict[str, float]] = {}
    residuals: list[float] = []
    raw_detunes: list[float] = []
    for sample, (root, claim, claim_variants) in sorted(
        targets.items(), key=lambda item: item[0].as_posix()
    ):
        root_hz = reference_a4 * (2.0 ** ((root - 69.0) / 12.0))
        expected_hz = root_hz * (2.0 ** (claim / 1200.0))
        measurement = analyze_file_harmonic_pitch(
            sample,
            expected_hz,
            start_seconds=start_seconds,
            maximum_frames=131_072,
            search_cents=search_cents,
            harmonic_count=harmonic_count,
        )
        raw_detune = 1200.0 * math.log2(measurement.measured_hz / root_hz)
        residual = 1200.0 * math.log2(measurement.measured_hz / expected_hz)
        record: dict[str, float] = {
            "root_midi": root,
            "upstream_claimed_detune_cents": round(claim, 6),
            "measured_hz": round(measurement.measured_hz, 6),
            "measured_detune_cents": round(raw_detune, 6),
            "residual_after_upstream_map_cents": round(residual, 6),
        }
        if claim_variants > 1:
            record["distinct_upstream_claims"] = claim_variants
        samples[_relative(sample, asset_root)] = record
        residuals.append(residual)
        raw_detunes.append(raw_detune)

    apply_to_runtime = bool(manifest.get("apply_pitch_calibration", False))
    document = {
        "applicable": True,
        "pitch_mode": "pitched",
        "reference_a4_hz": reference_a4,
        "playback_calibration": (
            "per-sample measured_detune_cents applied at runtime"
            if apply_to_runtime
            else "upstream SFZ pitch_keycenter plus tune opcodes"
        ),
        "measurement_role": (
            (
                "formal runtime correction; each current attack sample must "
                "have one finite measured_detune_cents value"
            )
            if apply_to_runtime
            else (
                "diagnostic; playback follows the audited upstream map, and "
                "the residual column records how far each source sample "
                "deviates after that map"
            )
        ),
        "applied_to_runtime": apply_to_runtime,
        "measurement_algorithm": (
            "harmonic-constrained FFT of the raw source audio near the mapped pitch, "
            f"start={start_seconds:g}s, search +/-{search_cents:g} cents"
        ),
        "summary": {
            "sample_count": len(samples),
            "median_measured_detune_cents": round(statistics.median(raw_detunes), 6),
            "maximum_absolute_measured_detune_cents": round(
                max(map(abs, raw_detunes)), 6
            ),
            "median_residual_cents": round(statistics.median(residuals), 6),
            "maximum_absolute_residual_cents": round(max(map(abs, residuals)), 6),
        },
        "samples": samples,
    }
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_dedicated_audition_verification(
    manifest_path: str | Path,
    events_path: str | Path,
    wav_path: str | Path,
    *,
    output_path: str | Path,
    coverage: list[str],
) -> dict[str, Any]:
    """Render one fixed audition and retain reproducible evidence.

    The WAV is a disposable build product.  Its metrics and hash remain in the
    tracked report, while callers may delete the audio after listening.
    """

    import numpy as np
    import soundfile as sf

    from .renderer import render_to_wav

    manifest_path = Path(manifest_path).resolve()
    events_path = Path(events_path).resolve()
    wav_path = Path(wav_path).resolve()
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    result = render_to_wav(manifest_path, events_path, wav_path)
    wav_info = sf.info(str(wav_path))
    audio, sample_rate = sf.read(str(wav_path), dtype="float32", always_2d=True)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = (
        float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
        if audio.size
        else 0.0
    )
    clipped = int(np.count_nonzero(np.abs(audio) >= 1.0))

    workspace_root = manifest_path.parent
    while workspace_root != workspace_root.parent and not (
        workspace_root / "pyproject.toml"
    ).is_file():
        workspace_root = workspace_root.parent
    try:
        wav_label = wav_path.relative_to(workspace_root).as_posix()
    except ValueError:
        wav_label = str(wav_path)
    try:
        events_label = events_path.relative_to(workspace_root).as_posix()
    except ValueError:
        events_label = str(events_path)

    report: dict[str, Any] = {
        "status": "machine_pass_human_pending",
        "rendered_at": _datetime.date.today().isoformat(),
        "platform": f"{platform.system()} Chinese-path workspace",
        "sample_rate": int(sample_rate),
        "channels": int(audio.shape[1]),
        "subtype": str(wav_info.subtype),
        "frame_count": int(audio.shape[0]),
        "duration_seconds": round(audio.shape[0] / sample_rate, 6),
        "peak_active_voices": int(result.peak_active_voices),
        "peak": round(peak, 6),
        "rms": round(rms, 6),
        "clipped_samples": clipped,
        "wav": wav_label,
        "wav_persistence": "temporary",
        "wav_sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
        "hash_algorithm": HASH_ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "manifest_canonical_sha256": canonical_json_file_sha256(manifest_path),
        "events": events_label,
        "events_canonical_sha256": canonical_json_file_sha256(events_path),
        "audition_profile": "fixed-example",
        "audition_protocol": "instrument-fixed-example-v1",
        "coverage": coverage,
        "human_review": "pending",
    }
    report_path = Path(output_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report
