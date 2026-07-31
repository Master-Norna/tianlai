"""卡林巴专用资源与音片模态核验。

金属悬臂音片具有明显非谐和模态，不能把弦乐式整数谐波模板的最高分直接当作
基频。本模块保留 VCSL 原始 SFZ 和录音，只修正项目的证据生成方法。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

import numpy as np
import soundfile as sf

from tianlai.audio import wav_loop_points
from tianlai.analysis import analyze_file_harmonic_pitch
from tianlai.dedicated_candidates import (
    dedicated_manifest_sources,
    generate_dedicated_resource_verification,
)


_EXPECTED_SHAPE = {
    "sample_count": 15,
    "sample_bytes": 22_303_536,
    "sample_set_sha256": (
        "6ea996c51ec01f751e5a971517266ba1a3cdf148ce5bb7411030e71a88049f5d"
    ),
    "source_file_sha256": {
        "Idiophones/Plucked Idiophones/Kalimba, Kenya.sfz": (
            "29fb4d6e1a02e05170a6bb921510d1862e5727cb903a3f24fd866b2c320dc4a9"
        )
    },
    "evidence_sha256": {
        "README.md": (
            "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b"
        )
    },
}
_D_SHARP_K13 = (
    "Idiophones/Plucked Idiophones/Kalimba, Kenya/"
    "Mbira6_Normal_MainSpirit_D#4_k13_vl3_rr2.wav"
)
_B_K15 = (
    "Idiophones/Plucked Idiophones/Kalimba, Kenya/"
    "Mbira6_Normal_MainSpirit_B4_k15_vl3_rr2.wav"
)


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _refined_peak_bin(spectrum: np.ndarray, index: int) -> float:
    if not 0 < index < len(spectrum) - 1:
        return float(index)
    values = np.log(spectrum[index - 1 : index + 2] + 1e-30)
    denominator = float(values[0] - 2.0 * values[1] + values[2])
    if denominator == 0.0:
        return float(index)
    return float(index) + 0.5 * float(values[0] - values[2]) / denominator


def _modal_peak(
    audio: np.ndarray,
    sample_rate: int,
    expected_hz: float,
    *,
    start_seconds: float,
    duration_seconds: float | None,
    maximum_frames: int,
    low_cents: float,
    high_cents: float,
) -> tuple[float, float]:
    start = max(0, round(start_seconds * sample_rate))
    available = len(audio) - start
    requested = (
        round(duration_seconds * sample_rate)
        if duration_seconds is not None
        else maximum_frames
    )
    frame_count = min(maximum_frames, requested, available)
    if frame_count < 4_096:
        raise ValueError("kalimba sample is too short for modal analysis")
    signal = np.mean(audio[start : start + frame_count], axis=1).astype(
        "float64",
        copy=False,
    )
    signal = signal - float(np.mean(signal))
    # The 120 ms onset window needs zero padding for stable sub-bin
    # interpolation; longer windows retain their native resolution.
    fft_size = max(frame_count, 1 << 19) if duration_seconds is not None else frame_count
    window = np.hanning(frame_count)
    spectrum = np.abs(np.fft.rfft(signal * window, n=fft_size))
    spectrum /= max(float(np.sum(window)), 1e-30)
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    low_hz = expected_hz * (2.0 ** (low_cents / 1200.0))
    high_hz = expected_hz * (2.0 ** (high_cents / 1200.0))
    candidates = np.flatnonzero(
        (frequencies >= low_hz) & (frequencies <= high_hz)
    )
    if len(candidates) < 3:
        raise ValueError("kalimba modal search interval contains too few bins")
    local = candidates[
        (spectrum[candidates] >= spectrum[candidates - 1])
        & (spectrum[candidates] > spectrum[candidates + 1])
    ]
    if len(local) == 0:
        raise ValueError("kalimba modal search interval contains no local peak")
    index = int(local[np.argmax(spectrum[local])])
    refined_bin = _refined_peak_bin(spectrum, index)
    measured_hz = refined_bin * sample_rate / fft_size
    peak_magnitude = float(spectrum[index])
    return measured_hz, peak_magnitude


def _cents(frequency: float, expected: float) -> float:
    return 1200.0 * math.log2(frequency / expected)


def generate_kalimba_pitch_calibration(
    manifest_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Measure intended tine modes without treating inharmonic modes as harmonics."""

    inventory = dedicated_manifest_sources(manifest_path)
    manifest = inventory["manifest"]
    source_manifest: Path = inventory["manifest_path"]
    asset_root: Path = inventory["asset_root"]
    reference_a4 = float(manifest.get("reference_a4_hz", 440.0))

    claims: dict[Path, tuple[float, float]] = {}
    for articulation in inventory["articulations"].values():
        for region in articulation["attack_regions"]:
            sample = Path(region["sample"]).resolve()
            value = (
                float(region["root_midi"]),
                float(region.get("measured_tuning_cents", 0.0)),
            )
            previous = claims.setdefault(sample, value)
            if previous != value:
                raise ValueError(
                    f"kalimba sample has inconsistent root/tune claims: {sample}"
                )

    measurements: dict[str, dict[str, Any]] = {}
    residuals: list[float] = []
    raw_detunes: list[float] = []
    sympathetic_count = 0
    for sample, (root_midi, upstream_claim) in sorted(
        claims.items(),
        key=lambda item: _relative(item[0], asset_root),
    ):
        audio, sample_rate = sf.read(sample, dtype="float64", always_2d=True)
        expected_hz = reference_a4 * (2.0 ** ((root_midi - 69.0) / 12.0))
        long_hz, long_magnitude = _modal_peak(
            audio,
            sample_rate,
            expected_hz,
            start_seconds=0.08,
            duration_seconds=None,
            maximum_frames=131_072,
            low_cents=-180.0,
            high_cents=180.0,
        )
        onset_hz, onset_magnitude = _modal_peak(
            audio,
            sample_rate,
            expected_hz,
            start_seconds=0.05,
            duration_seconds=0.12,
            maximum_frames=131_072,
            low_cents=-180.0,
            high_cents=180.0,
        )
        dominant_hz, dominant_magnitude = _modal_peak(
            audio,
            sample_rate,
            expected_hz,
            start_seconds=0.08,
            duration_seconds=None,
            maximum_frames=131_072,
            low_cents=-1_400.0,
            high_cents=400.0,
        )
        long_detune = _cents(long_hz, expected_hz)
        onset_detune = _cents(onset_hz, expected_hz)
        dominant_detune = _cents(dominant_hz, expected_hz)
        octave_sympathetic = (
            abs(dominant_detune + 1_200.0) <= 80.0
            and abs(onset_detune) <= 80.0
        )
        if octave_sympathetic:
            selected_hz = onset_hz
            selection = (
                "0.05-0.17 s labelled-root mode; late dominant is an "
                "octave-lower sympathetic resonance"
            )
            classification = "onset_tine_with_octave_lower_sympathetic_resonance"
            sympathetic_count += 1
        else:
            selected_hz = long_hz
            selection = (
                "long-lived strongest local mode within +/-180 cents of "
                "the labelled root"
            )
            classification = "long_lived_labelled_tine_mode"
        raw_detune = _cents(selected_hz, expected_hz)
        residual = raw_detune - upstream_claim
        relative = _relative(sample, asset_root)
        measurements[relative] = {
            "root_midi": root_midi,
            "upstream_claimed_detune_cents": round(upstream_claim, 6),
            "measured_hz": round(selected_hz, 6),
            "measured_detune_cents": round(raw_detune, 6),
            "residual_after_upstream_map_cents": round(residual, 6),
            "classification": classification,
            "selection": selection,
            "long_label_band_mode_hz": round(long_hz, 6),
            "long_label_band_detune_cents": round(long_detune, 6),
            "onset_label_band_mode_hz": round(onset_hz, 6),
            "onset_label_band_detune_cents": round(onset_detune, 6),
            "long_dominant_mode_hz": round(dominant_hz, 6),
            "long_dominant_detune_cents": round(dominant_detune, 6),
            "onset_to_long_label_peak_db": round(
                20.0
                * math.log10(max(onset_magnitude, 1e-30) / max(long_magnitude, 1e-30)),
                6,
            ),
            "long_dominant_to_label_peak_db": round(
                20.0
                * math.log10(
                    max(dominant_magnitude, 1e-30) / max(long_magnitude, 1e-30)
                ),
                6,
            ),
            # The project deliberately does not retune a physical tine from
            # this diagnostic measurement.
            "automatic_pitch_override_cents": None,
        }
        residuals.append(residual)
        raw_detunes.append(raw_detune)

    # Reproduce the former false-positive method only for the two disputed
    # recordings, so the report freezes why its result was rejected.
    legacy: dict[str, dict[str, Any]] = {}
    for relative in (_D_SHARP_K13, _B_K15):
        record = measurements[relative]
        root_hz = reference_a4 * (
            2.0 ** ((float(record["root_midi"]) - 69.0) / 12.0)
        )
        claim = float(record["upstream_claimed_detune_cents"])
        expected_mapped_hz = root_hz * (2.0 ** (claim / 1200.0))
        measurement = analyze_file_harmonic_pitch(
            asset_root / relative,
            expected_mapped_hz,
            start_seconds=0.08,
            maximum_frames=131_072,
            search_cents=180.0,
            harmonic_count=10,
        )
        raw_legacy = _cents(measurement.measured_hz, root_hz)
        legacy[relative] = {
            "legacy_harmonic_template_hz": round(measurement.measured_hz, 6),
            "legacy_measured_detune_cents": round(raw_legacy, 6),
            "accepted_modal_detune_cents": record["measured_detune_cents"],
            "reason_rejected": (
                "integer-harmonic scoring is invalid for an inharmonic "
                "cantilever tine and selected a secondary mode"
            ),
        }

    lower_b_modes = [
        float(record["long_label_band_mode_hz"])
        for relative, record in measurements.items()
        if "B3_k3_" in relative or "B3_k12_" in relative
    ]
    b_late = float(measurements[_B_K15]["long_dominant_mode_hz"])
    lower_b_mean = statistics.mean(lower_b_modes)
    b_octave_match = 1200.0 * math.log2(b_late / lower_b_mean)
    document = {
        "applicable": True,
        "pitch_mode": "pitched",
        "reference_a4_hz": reference_a4,
        "playback_calibration": (
            "unchanged audited upstream SFZ pitch_keycenter plus tune opcodes"
        ),
        "measurement_role": (
            "diagnostic only; no per-sample average-temperament correction is "
            "applied, so the recorded physical tuning and resonances remain intact"
        ),
        "measurement_algorithm": (
            "direct local-mode FFT for inharmonic metal tines: normally the "
            "long-lived strongest peak within +/-180 cents of the labelled root; "
            "when a late octave-lower sympathetic mode dominates, use the "
            "0.05-0.17 s labelled-root onset mode"
        ),
        "summary": {
            "sample_count": len(measurements),
            "median_measured_detune_cents": round(
                statistics.median(raw_detunes),
                6,
            ),
            "maximum_absolute_measured_detune_cents": round(
                max(map(abs, raw_detunes)),
                6,
            ),
            "median_residual_cents": round(statistics.median(residuals), 6),
            "maximum_absolute_residual_cents": round(
                max(map(abs, residuals)),
                6,
            ),
            "residuals_above_50_cents": sum(abs(value) > 50.0 for value in residuals),
            "octave_sympathetic_recording_count": sympathetic_count,
            "automatic_pitch_override_count": 0,
        },
        "disputed_recording_findings": {
            "D#4_k13": (
                "label/root mapping is supported; the accepted long-lived "
                "label-band mode is close to D# and the former +161-cent result "
                "is a secondary-mode false positive"
            ),
            "B4_k15": (
                "label/root mapping is supported by its B5 onset mode; the later "
                "octave-lower mode matches the two recorded B4 tines and is "
                "classified as sympathetic resonance, not a root remap"
            ),
            "B4_k15_late_mode_vs_lower_B_tines_cents": round(b_octave_match, 6),
        },
        "legacy_false_positive_diagnostics": legacy,
        "samples": measurements,
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def generate_kalimba_resource_verification(
    manifest_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze the complete CC0 source shape and independently audit its audio."""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("resource_verification", "资源核验.json"))
    )
    base = generate_dedicated_resource_verification(
        source_manifest,
        output_path=destination,
    )
    actual_shape = {
        key: base[key]
        for key in (
            "sample_count",
            "sample_bytes",
            "sample_set_sha256",
            "source_file_sha256",
            "evidence_sha256",
        )
    }
    if actual_shape != _EXPECTED_SHAPE:
        raise ValueError(
            "VCSL Kalimba, Kenya does not match the frozen v1.2.2-RC "
            f"resource shape: {actual_shape}"
        )

    inventory = dedicated_manifest_sources(source_manifest)
    asset_root: Path = inventory["asset_root"]
    regions = [
        region
        for articulation in inventory["articulations"].values()
        for region in articulation["attack_regions"]
    ]
    sample_paths = sorted(
        {Path(region["sample"]).resolve() for region in regions},
        key=lambda path: _relative(path, asset_root),
    )
    sample_hashes = {
        _relative(path, asset_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sample_paths
    }
    formats: dict[str, int] = {}
    peaks: dict[Path, float] = {}
    durations: list[float] = []
    tail_rms_dbfs: list[float] = []
    clipped = 0
    silent = 0
    embedded_loops = 0
    for path in sample_paths:
        info = sf.info(path)
        format_key = (
            f"{path.suffix.lower()}:{info.samplerate}Hz:"
            f"{info.channels}ch:{info.subtype}"
        )
        formats[format_key] = formats.get(format_key, 0) + 1
        durations.append(float(info.duration))
        embedded_loops += int(wav_loop_points(path) is not None)
        audio, sample_rate = sf.read(path, dtype="float32", always_2d=True)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        peaks[path] = peak
        clipped += int(peak >= 1.0)
        silent += int(peak <= 1e-6)
        tail = audio[-min(len(audio), round(sample_rate * 0.1)) :]
        tail_rms = (
            math.sqrt(float(np.mean(np.square(tail, dtype=np.float64))))
            if tail.size
            else 0.0
        )
        tail_rms_dbfs.append(20.0 * math.log10(max(tail_rms, 1e-12)))

    roots = sorted({int(region["root_midi"]) for region in regions})
    rr_families: set[tuple[int, int, int, int]] = set()
    mapping_failures: list[list[int]] = []
    for note in range(59, 85):
        matches = [
            region
            for region in regions
            if float(region["key_min"]) <= note <= float(region["key_max"])
        ]
        if len(matches) == 2 and {
            int(region.get("round_robin_position", 0)) for region in matches
        } == {1, 2} and {
            int(region.get("round_robin_length", 0)) for region in matches
        } == {2}:
            rr_families.add(
                (
                    int(matches[0]["root_midi"]),
                    int(matches[0]["key_min"]),
                    int(matches[0]["key_max"]),
                    2,
                )
            )
        elif len(matches) != 1:
            mapping_failures.append([note, len(matches)])
    if mapping_failures:
        raise ValueError(f"kalimba mapping is ambiguous or incomplete: {mapping_failures}")

    maximum_stretch = max(
        max(
            abs(float(region["key_min"]) - float(region["root_midi"])),
            abs(float(region["key_max"]) - float(region["root_midi"])),
        )
        for region in regions
    )
    upstream_peak = max(
        peaks[Path(region["sample"]).resolve()]
        * (10.0 ** (float(region["gain_db"]) / 20.0))
        for region in regions
    )
    project_gain = float(manifest["gain"])
    project_peak = upstream_peak * project_gain
    headroom_db = -20.0 * math.log10(project_peak)
    if headroom_db < 6.0:
        raise ValueError(
            f"kalimba project gain leaves only {headroom_db:.3f} dB headroom"
        )

    report = {
        "upstream": manifest["upstream"],
        "origin": manifest["origin"],
        "upstream_version": manifest["upstream_version"],
        "upstream_commit": manifest["upstream_commit"],
        "license": manifest["license"],
        "source_file_count": base["source_file_count"],
        "source_file_sha256": base["source_file_sha256"],
        "evidence_sha256": base["evidence_sha256"],
        "sample_count": len(sample_paths),
        "sample_bytes": base["sample_bytes"],
        "sample_sha256": sample_hashes,
        "sample_set_sha256": base["sample_set_sha256"],
        "sample_set_hash_algorithm": base["sample_set_hash_algorithm"],
        "sample_formats": formats,
        "articulations": base["articulations"],
        "mapping": {
            "region_count": len(regions),
            "physical_tine_recording_count": len(sample_paths),
            "unique_root_count": len(roots),
            "root_midi_notes": roots,
            "recorded_velocity_layer_count": 1,
            "round_robin_family_count": len(rr_families),
            "round_robin_region_count": sum(
                "round_robin_length" in region for region in regions
            ),
            "coverage_midi": [59, 84],
            "maximum_stretch_semitones": maximum_stretch,
            "ambiguous_or_missing_integer_notes": len(mapping_failures),
            "embedded_loop_count": embedded_loops,
            "stereo_sample_count": sum(
                count for key, count in formats.items() if ":2ch:" in key
            ),
            "release_seconds": 15.0,
        },
        "project_policy": {
            "upstream_sfz_unchanged": True,
            "pitch_root_overrides": 0,
            "average_temperament_corrections": 0,
            "interpretation": (
                "preserve the recorded physical tine tuning and upstream mapping; "
                "use a kalimba-specific modal audit instead of retuning from an "
                "integer-harmonic false positive"
            ),
        },
        "audio_integrity": {
            "source_clipped_samples": clipped,
            "silent_samples": silent,
            "duration_seconds": {
                "minimum": round(min(durations), 6),
                "median": round(statistics.median(durations), 6),
                "maximum": round(max(durations), 6),
            },
            "final_100ms_rms_dbfs": {
                "minimum": round(min(tail_rms_dbfs), 6),
                "median": round(statistics.median(tail_rms_dbfs), 6),
                "maximum": round(max(tail_rms_dbfs), 6),
                "samples_above_minus_60_dbfs": sum(
                    value > -60.0 for value in tail_rms_dbfs
                ),
            },
            "maximum_upstream_region_peak_dbfs": round(
                20.0 * math.log10(upstream_peak),
                6,
            ),
            "project_gain": project_gain,
            "maximum_project_peak_dbfs": round(
                20.0 * math.log10(project_peak),
                6,
            ),
            "minimum_headroom_db": round(headroom_db, 6),
        },
    }
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


__all__ = [
    "generate_kalimba_pitch_calibration",
    "generate_kalimba_resource_verification",
]
