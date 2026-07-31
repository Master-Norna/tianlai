"""Strict-CC0 VCSL timpani selection and reproducible quality evidence.

The selected instrument deliberately combines two *real* VCSL recordings:

* ``Timpani 2 - Scale`` supplies the hit articulation because it has wider
  pitch coverage, 24-bit samples, three recorded velocity layers and RR2.
* ``Timpani 1 - Roll`` supplies finite natural rolls because the second set
  contains no roll recordings.

Nothing in this module manufactures velocity layers, round robins, pitch
randomisation or loops.  Timpani spectra are strongly inharmonic, so the
spectral measurements below are diagnostics only and are never written back
as automatic tuning corrections.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any

from .audio import wav_loop_points
from .dedicated_sfz import (
    DedicatedSfzRegionMetadata,
    dedicated_regions_to_manifest,
    parse_dedicated_sfz,
)


UPSTREAM_VERSION = "1.2.2-RC"
UPSTREAM_COMMIT = "b6e6ac82d22248edee98a0bde185eb9ef6d439ad"
LICENSE_EVIDENCE = "README.md"
LICENSE_EVIDENCE_SHA256 = (
    "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b"
)

TIMPANI_DIRECTORY = "Membranophones/Struck Membranophones"
CANDIDATE_SFZ: dict[str, str] = {
    "timpani_1_hit": f"{TIMPANI_DIRECTORY}/Timpani 1 - Hit.sfz",
    "timpani_1_roll": f"{TIMPANI_DIRECTORY}/Timpani 1 - Roll.sfz",
    "timpani_2_scale": f"{TIMPANI_DIRECTORY}/Timpani 2 - Scale.sfz",
}
SELECTED_SFZ: dict[str, str] = {
    "hit": CANDIDATE_SFZ["timpani_2_scale"],
    "roll": CANDIDATE_SFZ["timpani_1_roll"],
}
FROZEN_SELECTED_SHAPE: dict[str, Any] = {
    "sample_count": 64,
    "sample_bytes": 132_588_162,
    "sample_set_sha256": (
        "d323d3c2a7587be4948bf831b4956c6e0d6fec73aceb4255a425226be8c1803a"
    ),
    "source_sfz_sha256": {
        CANDIDATE_SFZ["timpani_1_roll"]: (
            "3ec91e9eee1d8614889380b1a5581fdc5191baaf7598eac6d3c038811dd5c1e5"
        ),
        CANDIDATE_SFZ["timpani_2_scale"]: (
            "2e1953d4c3c79c187d312f4f93a7249f3024dbc5b4b69b8190c9d68925fb5ab4"
        ),
    },
    "evidence_sha256": {
        LICENSE_EVIDENCE: LICENSE_EVIDENCE_SHA256,
    },
}

_PITCH_GROUP = re.compile(r"^(Timpani\d+[A-Z]?)_", re.IGNORECASE)
_VELOCITY_LABEL = re.compile(r"_v(\d+)_", re.IGNORECASE)


def _load_manifest(manifest_path: str | Path) -> tuple[Path, dict[str, Any], Path]:
    path = Path(manifest_path).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("type") != "dedicated_sfz":
        raise ValueError(f"VCSL timpani must use dedicated_sfz: {path}")
    asset_root = (path.parent / str(manifest["asset_root"])).resolve()
    if not asset_root.is_dir():
        raise ValueError(f"VCSL asset root does not exist: {asset_root}")
    if manifest.get("upstream_version") != UPSTREAM_VERSION:
        raise ValueError("VCSL timpani upstream_version does not match frozen release")
    if manifest.get("upstream_commit") != UPSTREAM_COMMIT:
        raise ValueError("VCSL timpani upstream_commit does not match frozen release")
    if manifest.get("license") != "CC0-1.0":
        raise ValueError("VCSL timpani must remain strict CC0-1.0")
    if manifest.get("license_status") != "approved":
        raise ValueError("VCSL timpani CC0 resource must have approved status")
    return path, manifest, asset_root


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _candidate_regions(
    asset_root: Path,
    sfz_relative: str,
) -> tuple[
    list[dict[str, Any]],
    dict[str, DedicatedSfzRegionMetadata],
    tuple[Path, ...],
]:
    sfz_path = (asset_root / sfz_relative).resolve()
    try:
        sfz_path.relative_to(asset_root)
    except ValueError as error:
        raise ValueError(f"candidate SFZ escapes VCSL root: {sfz_relative}") from error
    document = parse_dedicated_sfz(sfz_path, asset_root=asset_root)
    regions, metadata = dedicated_regions_to_manifest(
        sfz_path,
        asset_root=asset_root,
        trigger="attack",
        use_embedded_loops=False,
        stable_prefix=sfz_relative,
    )
    if not regions:
        raise ValueError(f"VCSL timpani candidate has no attack regions: {sfz_path}")
    return regions, metadata, document.source_files


def _velocity_family(
    region: dict[str, Any],
    metadata: DedicatedSfzRegionMetadata,
) -> tuple[Any, ...]:
    return (
        float(region["key_min"]),
        float(region["key_max"]),
        float(region["velocity_min"]),
        float(region["velocity_max"]),
        metadata.velocity_fade_in,
        metadata.velocity_fade_out,
    )


def _low_mode(
    audio: Any,
    sample_rate: int,
    expected_hz: float,
    *,
    start_seconds: float,
) -> tuple[float, float]:
    """Return the strongest low mode near the mapped raw pitch.

    A timpani's strongest partial is not a harmonic fundamental.  Restricting
    the search to +/-300 cents around the mapped low mode makes the diagnostic
    repeatable while keeping it explicitly unsuitable for automatic tuning.
    """

    import numpy as np

    mono = audio.mean(axis=1, dtype="float64")
    start = max(0, round(start_seconds * sample_rate))
    frame_count = min(131_072, len(mono) - start)
    if frame_count < 4096:
        raise ValueError("timpani sample is too short for low-mode diagnostics")
    segment = mono[start : start + frame_count]
    segment = segment - float(np.mean(segment))
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(frame_count)))
    frequencies = np.fft.rfftfreq(frame_count, 1.0 / sample_rate)
    ratio = 2.0 ** (300.0 / 1200.0)
    candidates = np.flatnonzero(
        (frequencies >= expected_hz / ratio)
        & (frequencies <= expected_hz * ratio)
    )
    if len(candidates) == 0:
        raise ValueError("timpani low-mode search range contains no FFT bins")
    peak_index = int(candidates[int(np.argmax(spectrum[candidates]))])
    delta = 0.0
    if 0 < peak_index < len(spectrum) - 1:
        left, center, right = np.log(
            spectrum[peak_index - 1 : peak_index + 2] + 1.0e-20
        )
        denominator = left - 2.0 * center + right
        if denominator != 0.0:
            delta = float(0.5 * (left - right) / denominator)
    measured_hz = (peak_index + delta) * sample_rate / frame_count
    residual_cents = 1200.0 * math.log2(measured_hz / expected_hz)
    return measured_hz, residual_cents


def _inspect_candidate(
    asset_root: Path,
    candidate_name: str,
    sfz_relative: str,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    import numpy as np
    import soundfile as sf

    regions, metadata, source_files = _candidate_regions(asset_root, sfz_relative)
    sample_paths = sorted(
        {Path(region["sample"]).resolve() for region in regions},
        key=lambda path: _relative(path, asset_root),
    )
    region_by_sample: dict[Path, dict[str, Any]] = {}
    for region in regions:
        sample = Path(region["sample"]).resolve()
        if sample in region_by_sample:
            raise ValueError(
                f"one VCSL timpani candidate maps a sample more than once: {sample}"
            )
        region_by_sample[sample] = region

    formats: dict[str, int] = {}
    durations: list[float] = []
    peaks: dict[Path, float] = {}
    tail_rms: list[float] = []
    endpoint_peaks: list[float] = []
    onset_seconds: list[float] = []
    low_modes: dict[Path, tuple[float, float]] = {}
    clipped = 0
    silent = 0
    embedded_loops = 0
    offset_ratios: list[float] = []
    sample_records: dict[str, dict[str, Any]] = {}
    is_roll = candidate_name == "timpani_1_roll"

    for path in sample_paths:
        region = region_by_sample[path]
        info = sf.info(path)
        format_key = (
            f"{path.suffix.lower()}:{info.samplerate}Hz:"
            f"{info.channels}ch:{info.subtype}"
        )
        formats[format_key] = formats.get(format_key, 0) + 1
        audio, sample_rate = sf.read(
            path,
            dtype="float32",
            always_2d=True,
        )
        absolute = np.abs(audio)
        peak = float(absolute.max()) if audio.size else 0.0
        peaks[path] = peak
        durations.append(float(info.duration))
        clipped += int(peak >= 1.0)
        silent += int(peak <= 1.0e-6)
        embedded_loops += int(wav_loop_points(path) is not None)

        tail_frames = min(len(audio), round(0.25 * sample_rate))
        tail = audio[-tail_frames:] if tail_frames else audio
        tail_value = (
            float(np.sqrt(np.mean(np.square(tail, dtype="float64"))))
            if tail.size
            else 0.0
        )
        tail_rms.append(tail_value)
        endpoint = (
            float(np.max(np.abs(audio[-1])))
            if len(audio)
            else 0.0
        )
        endpoint_peaks.append(endpoint)
        threshold = max(1.0e-6, peak * 1.0e-4)
        audible_frames = np.flatnonzero(np.max(absolute, axis=1) > threshold)
        onset = (
            float(audible_frames[0] / sample_rate)
            if len(audible_frames)
            else float(info.duration)
        )
        onset_seconds.append(onset)

        offset = int(region.get("offset_frames", 0))
        discarded_peak = (
            float(absolute[:offset].max())
            if offset > 0 and absolute[:offset].size
            else 0.0
        )
        discarded_ratio = discarded_peak / peak if peak > 0.0 else 0.0
        offset_ratios.append(discarded_ratio)

        root_midi = float(region["root_midi"])
        upstream_claim = float(region.get("measured_tuning_cents", 0.0))
        expected_raw_hz = 440.0 * (2.0 ** ((root_midi - 69.0) / 12.0))
        expected_raw_hz *= 2.0 ** (upstream_claim / 1200.0)
        measured_hz, residual = _low_mode(
            audio,
            sample_rate,
            expected_raw_hz,
            start_seconds=0.5 if is_roll else 0.12,
        )
        low_modes[path] = (measured_hz, residual)

        sample_records[_relative(path, asset_root)] = {
            "format": format_key,
            "duration_seconds": round(float(info.duration), 6),
            "peak_dbfs": (
                round(20.0 * math.log10(peak), 6)
                if peak > 0.0
                else None
            ),
            "tail_250ms_rms_dbfs": (
                round(20.0 * math.log10(tail_value), 6)
                if tail_value > 0.0
                else None
            ),
            "endpoint_peak": round(endpoint, 9),
            "onset_seconds": round(onset, 6),
            "offset_frames": offset,
            "discarded_peak_ratio": round(discarded_ratio, 9),
            "embedded_loop": wav_loop_points(path) is not None,
            "root_midi": root_midi,
            "sfz_tune_cents": round(-upstream_claim, 6),
            "mapped_raw_low_mode_hz": round(expected_raw_hz, 6),
            "observed_low_mode_hz": round(measured_hz, 6),
            "low_mode_residual_cents": round(residual, 6),
            "automatic_tuning_correction_cents": None,
        }

    rr_families: dict[tuple[Any, ...], set[int]] = defaultdict(set)
    rr_lengths: set[int] = set()
    for region in regions:
        item = metadata[region["stable_key"]]
        length = region.get("round_robin_length")
        position = region.get("round_robin_position")
        if length is not None and position is not None:
            rr_lengths.add(int(length))
            rr_families[_velocity_family(region, item)].add(int(position))
    true_rr = (
        min(rr_lengths)
        if rr_lengths
        and len(rr_lengths) == 1
        and all(positions == set(range(1, next(iter(rr_lengths)) + 1))
                for positions in rr_families.values())
        else 0
    )

    rr_low_mode_differences: list[float] = []
    paths_by_family: dict[tuple[Any, ...], list[Path]] = defaultdict(list)
    for region in regions:
        item = metadata[region["stable_key"]]
        paths_by_family[_velocity_family(region, item)].append(
            Path(region["sample"]).resolve()
        )
    for paths in paths_by_family.values():
        if len(paths) != 2:
            continue
        # Compare residuals *after* each RR member's own audited SFZ root/tune
        # mapping.  Raw frequencies are not comparable when the upstream map
        # intentionally gives two takes different tuning corrections.
        first = low_modes[paths[0]][1]
        second = low_modes[paths[1]][1]
        rr_low_mode_differences.append(abs(first - second))

    physical_groups = sorted(
        {
            match.group(1)
            for path in sample_paths
            if (match := _PITCH_GROUP.match(path.name)) is not None
        }
    )
    velocity_labels = sorted(
        {
            int(match.group(1))
            for path in sample_paths
            if (match := _VELOCITY_LABEL.search(path.name)) is not None
        }
    )
    velocity_layers_by_zone: dict[
        tuple[float, float],
        set[tuple[Any, ...]],
    ] = defaultdict(set)
    for region in regions:
        item = metadata[region["stable_key"]]
        velocity_layers_by_zone[
            (float(region["key_min"]), float(region["key_max"]))
        ].add(
            (
                float(region["velocity_min"]),
                float(region["velocity_max"]),
                item.velocity_fade_in,
                item.velocity_fade_out,
            )
        )
    recorded_velocity_layer_count = max(
        map(len, velocity_layers_by_zone.values())
    )
    velocity_fades = sum(
        metadata[region["stable_key"]].velocity_fade_in is not None
        or metadata[region["stable_key"]].velocity_fade_out is not None
        for region in regions
    )
    mapped_peak = max(
        peaks[Path(region["sample"]).resolve()]
        * (10.0 ** (float(region.get("gain_db", 0.0)) / 20.0))
        for region in regions
    )
    residuals = [item[1] for item in low_modes.values()]
    summary: dict[str, Any] = {
        "sfz": sfz_relative,
        "source_file_sha256": {
            _relative(path, asset_root): _hash(path)
            for path in sorted(source_files, key=lambda item: item.as_posix())
        },
        "region_count": len(regions),
        "unique_sample_count": len(sample_paths),
        "coverage_midi": [
            int(min(float(region["key_min"]) for region in regions)),
            int(max(float(region["key_max"]) for region in regions)),
        ],
        "physical_recorded_pitch_group_count": len(physical_groups),
        "physical_recorded_pitch_groups": physical_groups,
        "mapped_root_count": len(
            {float(region["root_midi"]) for region in regions}
        ),
        "mapped_root_midi": sorted(
            {float(region["root_midi"]) for region in regions}
        ),
        "recorded_velocity_layer_count": recorded_velocity_layer_count,
        "source_filename_velocity_labels": velocity_labels,
        "true_round_robin_count": true_rr,
        "round_robin_family_count": len(rr_families),
        "velocity_crossfade_region_count": velocity_fades,
        "sample_formats": formats,
        "source_clipped_sample_count": clipped,
        "silent_sample_count": silent,
        "duration_seconds": {
            "minimum": round(min(durations), 6),
            "median": round(statistics.median(durations), 6),
            "maximum": round(max(durations), 6),
        },
        "onset_seconds": {
            "minimum": round(min(onset_seconds), 6),
            "median": round(statistics.median(onset_seconds), 6),
            "maximum": round(max(onset_seconds), 6),
        },
        "tail_250ms_rms": {
            "maximum": round(max(tail_rms), 9),
            "maximum_dbfs": (
                round(20.0 * math.log10(max(tail_rms)), 6)
                if max(tail_rms) > 0.0
                else None
            ),
            "maximum_endpoint_peak": round(max(endpoint_peaks), 9),
        },
        "offsets": {
            "nonzero_region_count": sum(
                int(region.get("offset_frames", 0)) > 0 for region in regions
            ),
            "maximum_frames": max(
                int(region.get("offset_frames", 0)) for region in regions
            ),
            "maximum_discarded_peak_ratio": round(max(offset_ratios), 9),
            "discarded_peak_ratio_over_1_percent_count": sum(
                ratio > 0.01 for ratio in offset_ratios
            ),
            "discarded_peak_ratio_over_5_percent_count": sum(
                ratio > 0.05 for ratio in offset_ratios
            ),
        },
        "embedded_loop_sample_count": embedded_loops,
        "looped_regions": embedded_loops,
        "maximum_upstream_mapped_peak": round(mapped_peak, 9),
        "maximum_upstream_mapped_peak_dbfs": round(
            20.0 * math.log10(mapped_peak),
            6,
        ),
        "inharmonic_pitch_diagnostic": {
            "method": (
                "strongest low spectral mode within +/-300 cents of the "
                "SFZ-mapped raw center; diagnostic only"
            ),
            "median_residual_cents": round(statistics.median(residuals), 6),
            "maximum_absolute_residual_cents": round(
                max(map(abs, residuals)),
                6,
            ),
            "automatic_correction_count": 0,
            "rr_pair_low_mode_difference_cents": {
                "pair_count": len(rr_low_mode_differences),
                "median": (
                    round(statistics.median(rr_low_mode_differences), 6)
                    if rr_low_mode_differences
                    else None
                ),
                "maximum": (
                    round(max(rr_low_mode_differences), 6)
                    if rr_low_mode_differences
                    else None
                ),
            },
        },
    }
    return summary, sample_records


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def generate_vcsl_timpani_candidate_comparison(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    source_manifest, _manifest, asset_root = _load_manifest(manifest_path)
    candidates: dict[str, dict[str, Any]] = {}
    for name, relative in CANDIDATE_SFZ.items():
        summary, _samples = _inspect_candidate(asset_root, name, relative)
        candidates[name] = summary

    document = {
        "scope": "VCSL v1.2.2-RC local timpani A/B machine comparison",
        "license_gate": {
            "license": "CC0-1.0",
            "evidence": LICENSE_EVIDENCE,
            "evidence_sha256": LICENSE_EVIDENCE_SHA256,
            "passed": True,
        },
        "candidates": candidates,
        "decision": {
            "hit": "timpani_2_scale",
            "hit_reasons": [
                "MIDI 38-59 coverage versus 41-55",
                "9 physical recorded pitch groups versus 5",
                "54 unique PCM24 samples versus 30 PCM16 samples",
                "3 real recorded velocity layers and true RR2 are preserved",
                "40 short upstream offsets remove at most 2.9% of each sample peak; none remove 5%",
            ],
            "roll": "timpani_1_roll",
            "roll_reasons": [
                "only reviewed candidate containing actual roll performances",
                "5 physical pitch groups and 2 real velocity layers",
                "10 finite natural 15.7-29.5 second recordings",
            ],
            "declared_limitations": [
                "hit and roll come from different VCSL recording sets and may have a timbral seam",
                "roll has no recorded round robin",
                "roll has no loop and is not advertised as infinite",
                "roll range is MIDI 41-55 while hit range is MIDI 38-59",
                "inharmonic low-mode diagnostics never become automatic tuning corrections",
            ],
            "forbidden_synthetic_claims": {
                "pitch_random_cents": 0,
                "amplitude_random_db": 0,
                "delay_random_seconds": 0,
                "manufactured_velocity_layers": 0,
                "manufactured_round_robins": 0,
                "manufactured_loops": 0,
            },
        },
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / "VCSL候选比较.json"
    )
    _write_json(destination, document)
    return document


def _selected_inventory(
    manifest: dict[str, Any],
    asset_root: Path,
) -> tuple[
    dict[str, tuple[list[dict[str, Any]], dict[str, DedicatedSfzRegionMetadata]]],
    list[Path],
    list[Path],
]:
    raw_articulations = manifest.get("articulations")
    if not isinstance(raw_articulations, dict):
        raise ValueError("VCSL timpani manifest articulations must be an object")
    actual_paths: dict[str, str] = {}
    for name, raw in raw_articulations.items():
        if not isinstance(raw, dict) or "sfz" not in raw:
            raise ValueError(f"VCSL timpani articulation {name!r} must be an object")
        actual_paths[str(name)] = str(raw["sfz"])
    if actual_paths != SELECTED_SFZ:
        raise ValueError(
            f"VCSL timpani selected SFZ paths changed: {actual_paths!r}"
        )

    region_sets: dict[
        str, tuple[list[dict[str, Any]], dict[str, DedicatedSfzRegionMetadata]]
    ] = {}
    source_files: set[Path] = set()
    samples: set[Path] = set()
    for name, relative in SELECTED_SFZ.items():
        regions, metadata, sources = _candidate_regions(asset_root, relative)
        region_sets[name] = (regions, metadata)
        source_files.update(path.resolve() for path in sources)
        samples.update(Path(region["sample"]).resolve() for region in regions)
    return (
        region_sets,
        sorted(source_files, key=lambda path: _relative(path, asset_root)),
        sorted(samples, key=lambda path: _relative(path, asset_root)),
    )


def generate_vcsl_timpani_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    source_manifest, manifest, asset_root = _load_manifest(manifest_path)
    region_sets, source_files, sample_paths = _selected_inventory(
        manifest,
        asset_root,
    )
    source_hashes = {
        _relative(path, asset_root): _hash(path) for path in source_files
    }
    evidence_hashes = {
        LICENSE_EVIDENCE: _hash(asset_root / LICENSE_EVIDENCE)
    }
    sample_hashes: dict[str, str] = {}
    sample_lines: list[str] = []
    for path in sample_paths:
        relative = _relative(path, asset_root)
        digest = _hash(path)
        sample_hashes[relative] = digest
        sample_lines.append(f"{digest}  {relative}\n")
    actual_shape = {
        "sample_count": len(sample_paths),
        "sample_bytes": sum(path.stat().st_size for path in sample_paths),
        "sample_set_sha256": hashlib.sha256(
            "".join(sample_lines).encode("utf-8")
        ).hexdigest(),
        "source_sfz_sha256": source_hashes,
        "evidence_sha256": evidence_hashes,
    }
    if actual_shape != FROZEN_SELECTED_SHAPE:
        raise ValueError(
            "VCSL timpani does not match the frozen v1.2.2-RC resource shape: "
            f"{actual_shape}"
        )

    selected_summaries: dict[str, dict[str, Any]] = {}
    selected_records: dict[str, dict[str, Any]] = {}
    for name, relative in SELECTED_SFZ.items():
        candidate_name = (
            "timpani_2_scale" if name == "hit" else "timpani_1_roll"
        )
        summary, records = _inspect_candidate(
            asset_root,
            candidate_name,
            relative,
        )
        selected_summaries[name] = summary
        selected_records.update(records)

    project_gain = float(manifest.get("gain", 1.0))
    articulation_gain = manifest.get("articulation_gain", {})
    if not isinstance(articulation_gain, dict):
        raise ValueError("VCSL timpani articulation_gain must be an object")
    headroom_by_articulation: dict[str, float] = {}
    for name, summary in selected_summaries.items():
        peak = float(summary["maximum_upstream_mapped_peak"])
        peak *= project_gain * float(articulation_gain.get(name, 1.0))
        headroom_by_articulation[name] = (
            -20.0 * math.log10(peak) if peak > 0.0 else float("inf")
        )
    if min(headroom_by_articulation.values()) < 6.0:
        raise ValueError(
            "VCSL timpani project gain leaves less than 6 dB single-voice headroom"
        )

    document = {
        "upstream": manifest["upstream"],
        "origin": manifest["origin"],
        "upstream_version": manifest["upstream_version"],
        "upstream_commit": manifest["upstream_commit"],
        "license": manifest["license"],
        "license_status": manifest["license_status"],
        "source_sfz_sha256": source_hashes,
        "evidence_sha256": evidence_hashes,
        "sample_count": actual_shape["sample_count"],
        "sample_bytes": actual_shape["sample_bytes"],
        "sample_sha256": sample_hashes,
        "sample_set_sha256": actual_shape["sample_set_sha256"],
        "sample_set_algorithm": (
            "Sort unique VCSL-relative UTF-8 paths; for each write "
            "'<lowercase file sha256>  <path>\\n'; SHA-256 the concatenated "
            "UTF-8 bytes."
        ),
        "articulations": selected_summaries,
        "audio_integrity": {
            "source_clipped_sample_count": sum(
                int(record["peak_dbfs"] is not None and record["peak_dbfs"] >= 0.0)
                for record in selected_records.values()
            ),
            "silent_sample_count": sum(
                int(record["peak_dbfs"] is None)
                for record in selected_records.values()
            ),
            "minimum_single_voice_headroom_db": round(
                min(headroom_by_articulation.values()),
                6,
            ),
            "single_voice_headroom_db": {
                name: round(value, 6)
                for name, value in headroom_by_articulation.items()
            },
        },
        "runtime_truth": {
            "hit": {
                "source": "Timpani 2 - Scale",
                "range_midi": [38, 59],
                "recorded_velocity_layers": 3,
                "recorded_round_robin": 2,
                "upstream_velocity_crossfades_preserved": True,
            },
            "roll": {
                "source": "Timpani 1 - Roll",
                "range_midi": [41, 55],
                "recorded_velocity_layers": 2,
                "recorded_round_robin": 0,
                "finite_natural_recording": True,
                "loop": False,
            },
            "synthetic_pitch_or_amplitude_variation": False,
            "upstream_sfz_and_wav_unchanged": True,
            "automatic_inharmonic_pitch_correction": False,
        },
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / "资源核验.json"
    )
    _write_json(destination, document)
    return document


def generate_vcsl_timpani_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    source_manifest, manifest, asset_root = _load_manifest(manifest_path)
    _region_sets, _source_files, _sample_paths = _selected_inventory(
        manifest,
        asset_root,
    )
    samples: dict[str, dict[str, Any]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for articulation, relative in SELECTED_SFZ.items():
        candidate_name = (
            "timpani_2_scale"
            if articulation == "hit"
            else "timpani_1_roll"
        )
        summary, records = _inspect_candidate(
            asset_root,
            candidate_name,
            relative,
        )
        summaries[articulation] = {
            "sample_count": summary["unique_sample_count"],
            "physical_recorded_pitch_group_count": (
                summary["physical_recorded_pitch_group_count"]
            ),
            "mapped_root_midi": summary["mapped_root_midi"],
            "median_low_mode_residual_cents": summary[
                "inharmonic_pitch_diagnostic"
            ]["median_residual_cents"],
            "maximum_absolute_low_mode_residual_cents": summary[
                "inharmonic_pitch_diagnostic"
            ]["maximum_absolute_residual_cents"],
        }
        for path, record in records.items():
            samples[path] = {
                "articulation": articulation,
                "root_midi": record["root_midi"],
                "sfz_tune_cents": record["sfz_tune_cents"],
                "mapped_raw_low_mode_hz": record["mapped_raw_low_mode_hz"],
                "observed_low_mode_hz": record["observed_low_mode_hz"],
                "low_mode_residual_cents": record["low_mode_residual_cents"],
                "automatic_tuning_correction_cents": None,
                "reason": (
                    "timpani modes are inharmonic; the audited upstream SFZ "
                    "root/tune mapping is retained without deriving a fake "
                    "fundamental correction from one spectral peak"
                ),
            }

    document = {
        "applicable": True,
        "pitch_semantics": "sounding_pitch_inharmonic",
        "reference_a4_hz": 440.0,
        "playback_calibration": "audited upstream SFZ pitch_keycenter plus tune",
        "measurement_role": (
            "diagnostic only; no measured low spectral mode is applied as an "
            "automatic correction"
        ),
        "measurement_algorithm": (
            "strongest low spectral mode within +/-300 cents of the mapped raw "
            "center; hit starts at 120 ms, roll at 500 ms, maximum 131072 frames"
        ),
        "summary": {
            "sample_count": len(samples),
            "automatic_correction_count": 0,
            "human_spectral_review": "pending",
            "articulations": summaries,
        },
        "samples": dict(sorted(samples.items())),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / "音准校准.json"
    )
    _write_json(destination, document)
    return document


__all__ = [
    "CANDIDATE_SFZ",
    "FROZEN_SELECTED_SHAPE",
    "SELECTED_SFZ",
    "generate_vcsl_timpani_candidate_comparison",
    "generate_vcsl_timpani_pitch_calibration",
    "generate_vcsl_timpani_resource_verification",
]
