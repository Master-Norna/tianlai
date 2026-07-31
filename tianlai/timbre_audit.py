"""Deterministic machine triage for a timbre acceptance matrix.

The functions in this module deliberately do *not* decide whether an
instrument sounds authentic.  They measure exact direct-output audio, verify
matrix coverage and rank discontinuities for later human review.  A report
produced here can block approval, but can never approve a timbre or a range.
"""

from __future__ import annotations

from collections import defaultdict
import math
from numbers import Real
from typing import Any, Iterable, Sequence

import numpy as np

from .analysis import analyze_signal_wide_pitch
from .canonical_json import canonical_json_bytes
from .runtime_variants import stable_variant_sha256


CLAIM = "machine_anomaly_triage_only_never_timbre_approval"
SIGNAL_STAGE = "instrument_direct_output_no_space"
PCM24_LSB = 1.0 / 8_388_608.0

DEFAULT_TRIAGE_THRESHOLDS = {
    # These thresholds only prioritize listening.  They are intentionally
    # reported with the result and are not high-fidelity acceptance limits.
    "adjacent_loudness_step_db": 10.0,
    "adjacent_spectral_centroid_octaves": 0.85,
    "adjacent_spectral_rolloff_octaves": 0.85,
    "adjacent_flatness_step": 0.30,
    "velocity_loudness_reversal_db": 1.5,
    "velocity_spectral_centroid_octaves": 0.85,
    "variant_loudness_deviation_db": 8.0,
    "variant_spectral_centroid_octaves": 0.90,
}

_COORDINATE_KEYS = {
    "runtime_configuration_sha256",
    "final_articulation",
    "midi_note",
    "velocity",
    "variant_lane_sha256",
    "variant_bundle_sha256",
}
_OBSERVATION_KEYS = {
    "coordinate",
    "metrics",
    "performance_sha256",
    "wav_sha256",
    "selection_receipt_sha256",
    "variant_coverage_status",
    "variant_coverage_proof_sha256",
    "runtime_fingerprint_sha256",
    "source_facts",
}
_METRIC_KEYS = {
    "analysis_version",
    "sample_rate_hz",
    "frame_count",
    "note_on_frame",
    "note_off_frame",
    "peak",
    "peak_dbfs",
    "rms",
    "loudness_proxy_dbfs",
    "crest_factor_db",
    "left_rms",
    "right_rms",
    "pre_roll_rms",
    "tail_rms",
    "dc_offset_left",
    "dc_offset_right",
    "clipping_sample_count",
    "spectral_centroid_hz",
    "spectral_rolloff_85_hz",
    "spectral_bandwidth_hz",
    "spectral_flatness",
    "stereo_correlation",
    "stereo_width_ratio",
    "peak_after_note_on_seconds",
    "active_tail_seconds",
    "pitch",
    "machine_blockers",
}
_SOURCE_FACT_KEYS = {
    "source_mode",
    "selection_scope",
    "selected_root_midis",
    "transposition_semitones",
    "maximum_absolute_transposition_semitones",
}


class TimbreAuditError(ValueError):
    """The matrix or one of its observations is not auditable."""


def _canonical_copy(value: Any) -> Any:
    """Return canonical JSON data while rejecting non-finite values."""

    import json

    try:
        return json.loads(canonical_json_bytes(value).decode("utf-8"))
    except (TypeError, ValueError) as error:
        raise TimbreAuditError(
            f"timbre audit data is not canonical JSON: {error}"
        ) from error


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TimbreAuditError(f"{label} must be a lowercase SHA-256")
    return value


def _db(value: float) -> float:
    return 20.0 * math.log10(max(value, 1.0e-15))


def _octave_distance(left: float, right: float) -> float:
    if left <= 0.0 or right <= 0.0:
        return math.inf
    return abs(math.log2(right / left))


def enumerate_integer_notes(
    ranges: Iterable[Sequence[float]],
) -> tuple[int, ...]:
    """Expand ordered disjoint MIDI spans without filling their holes."""

    notes: list[int] = []
    previous_high = -math.inf
    found = False
    for index, raw_span in enumerate(ranges):
        if len(raw_span) != 2:
            raise TimbreAuditError(
                f"ranges[{index}] must be a [minimum, maximum] pair"
            )
        if any(isinstance(item, bool) or not isinstance(item, Real) for item in raw_span):
            raise TimbreAuditError(f"ranges[{index}] notes must be numbers")
        low, high = float(raw_span[0]), float(raw_span[1])
        if not math.isfinite(low) or not math.isfinite(high):
            raise TimbreAuditError(f"ranges[{index}] notes must be finite")
        if not 0.0 <= low <= high <= 127.0:
            raise TimbreAuditError(
                f"ranges[{index}] must satisfy 0 <= minimum <= maximum <= 127"
            )
        if low <= previous_high:
            raise TimbreAuditError(
                "ranges must be ordered and non-overlapping"
            )
        integer_low, integer_high = math.ceil(low), math.floor(high)
        if integer_low > integer_high:
            raise TimbreAuditError(
                f"ranges[{index}] contains no integer MIDI note"
            )
        notes.extend(range(integer_low, integer_high + 1))
        previous_high = high
        found = True
    if not found:
        raise TimbreAuditError("at least one range is required")
    return tuple(notes)


def _strongest_window(
    audio: np.ndarray,
    sample_rate: int,
) -> np.ndarray:
    """Select a deterministic high-energy window for spectral descriptors."""

    frame_count = audio.shape[0]
    target = min(frame_count, max(2_048, round(sample_rate * 0.20)))
    if frame_count <= target:
        return audio
    power = np.mean(np.square(audio), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
    hop = max(1, target // 8)
    starts = np.arange(0, frame_count - target + 1, hop, dtype=np.int64)
    energies = cumulative[starts + target] - cumulative[starts]
    start = int(starts[int(np.argmax(energies))])
    return audio[start : start + target]


def analyze_timbre_audio(
    frames: Sequence[Sequence[float]] | np.ndarray,
    sample_rate: int,
    note_on_frame: int,
    note_off_frame: int,
    *,
    expected_hz: float | None = None,
    pre_quantization_clipping_sample_count: int | None = None,
) -> dict[str, Any]:
    """Measure one exact stereo matrix cell.

    Metrics are descriptive.  ``machine_blockers`` contains only objective
    integrity failures such as silence, clipping, pre-roll leakage or an
    unambiguous octave mapping error.
    """

    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or not 8_000 <= sample_rate <= 384_000
    ):
        raise TimbreAuditError(
            "sample_rate must be an integer between 8000 and 384000"
        )
    audio = np.asarray(frames, dtype=np.float64)
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise TimbreAuditError("frames must have shape (frame_count, 2)")
    if audio.shape[0] < 4:
        raise TimbreAuditError("audio is too short")
    if not np.all(np.isfinite(audio)):
        raise TimbreAuditError("audio contains non-finite samples")
    if (
        isinstance(note_on_frame, bool)
        or isinstance(note_off_frame, bool)
        or not isinstance(note_on_frame, int)
        or not isinstance(note_off_frame, int)
        or not 0 <= note_on_frame < note_off_frame <= audio.shape[0]
    ):
        raise TimbreAuditError(
            "note_on_frame/note_off_frame are outside the audio"
        )
    if expected_hz is not None and (
        isinstance(expected_hz, bool)
        or not isinstance(expected_hz, Real)
        or not math.isfinite(float(expected_hz))
        or float(expected_hz) <= 0.0
    ):
        raise TimbreAuditError("expected_hz must be finite and positive")

    if pre_quantization_clipping_sample_count is None:
        clipping_samples = int(np.count_nonzero(np.abs(audio) >= 1.0))
    elif (
        isinstance(pre_quantization_clipping_sample_count, bool)
        or not isinstance(pre_quantization_clipping_sample_count, int)
        or pre_quantization_clipping_sample_count < 0
    ):
        raise TimbreAuditError(
            "pre_quantization_clipping_sample_count must be non-negative"
        )
    else:
        clipping_samples = pre_quantization_clipping_sample_count

    pre_roll = audio[:note_on_frame]
    note = audio[note_on_frame:note_off_frame]
    tail = audio[note_off_frame:]
    channel_rms = np.sqrt(np.mean(np.square(note), axis=0))
    rms = float(np.sqrt(np.mean(np.square(note))))
    peak = float(np.max(np.abs(audio)))
    pre_roll_rms = (
        float(np.sqrt(np.mean(np.square(pre_roll))))
        if pre_roll.size
        else 0.0
    )
    tail_rms = (
        float(np.sqrt(np.mean(np.square(tail)))) if tail.size else 0.0
    )
    dc = np.mean(note, axis=0)

    blockers: list[str] = []
    signal_floor = max(PCM24_LSB * 8.0, pre_roll_rms * 4.0)
    if rms <= signal_floor:
        blockers.append("silent_or_insufficient_signal_to_noise")
    if clipping_samples:
        blockers.append("pre_quantization_clipping")
    if pre_roll_rms > max(PCM24_LSB * 4.0, rms * 0.01):
        blockers.append("pre_roll_leak")
    if float(np.max(np.abs(dc))) > max(0.01, rms * 0.25):
        blockers.append("excessive_dc_offset")

    body = _strongest_window(note, sample_rate)
    body = body - np.mean(body, axis=0, keepdims=True)
    window = np.hanning(body.shape[0])[:, None]
    spectra = np.abs(np.fft.rfft(body * window, axis=0))
    magnitude = np.sqrt(np.mean(np.square(spectra), axis=1))
    frequencies = np.fft.rfftfreq(body.shape[0], 1.0 / sample_rate)
    magnitude[0] = 0.0
    magnitude_sum = float(np.sum(magnitude))
    if magnitude_sum <= 1.0e-20:
        centroid = 0.0
        rolloff = 0.0
        bandwidth = 0.0
        flatness = 0.0
    else:
        centroid = float(np.sum(frequencies * magnitude) / magnitude_sum)
        variance = float(
            np.sum(np.square(frequencies - centroid) * magnitude)
            / magnitude_sum
        )
        bandwidth = math.sqrt(max(0.0, variance))
        power = np.square(magnitude)
        cumulative_power = np.cumsum(power)
        target_power = 0.85 * float(cumulative_power[-1])
        rolloff_index = min(
            len(frequencies) - 1,
            int(np.searchsorted(cumulative_power, target_power)),
        )
        rolloff = float(frequencies[rolloff_index])
        positive = magnitude[1:]
        flatness = float(
            math.exp(float(np.mean(np.log(positive + 1.0e-20))))
            / max(float(np.mean(positive)), 1.0e-20)
        )

    left = note[:, 0] - float(np.mean(note[:, 0]))
    right = note[:, 1] - float(np.mean(note[:, 1]))
    denominator = math.sqrt(
        float(np.dot(left, left)) * float(np.dot(right, right))
    )
    correlation = (
        float(np.dot(left, right) / denominator)
        if denominator > 1.0e-20
        else 1.0
    )
    mid_rms = float(np.sqrt(np.mean(np.square((left + right) * 0.5))))
    side_rms = float(np.sqrt(np.mean(np.square((left - right) * 0.5))))
    stereo_width_ratio = side_rms / max(mid_rms, 1.0e-15)

    frame_power = np.mean(np.square(note), axis=1)
    peak_frame = int(np.argmax(frame_power))
    tail_threshold = max(PCM24_LSB * 8.0, rms * 0.001)
    active_tail = np.flatnonzero(
        np.sqrt(np.mean(np.square(tail), axis=1)) >= tail_threshold
    ) if tail.size else np.empty(0, dtype=np.int64)
    tail_active_seconds = (
        float((int(active_tail[-1]) + 1) / sample_rate)
        if active_tail.size
        else 0.0
    )

    pitch: dict[str, Any] | None = None
    if expected_hz is not None and note.shape[0] >= 4_096:
        # Pick the stronger channel instead of averaging stereo, because a
        # valid antiphase recording must not disappear in the pitch check.
        pitch_channel = note[:, int(np.argmax(channel_rms))]
        maximum_frames = min(len(pitch_channel), 65_536)
        assessment = analyze_signal_wide_pitch(
            pitch_channel,
            sample_rate,
            float(expected_hz),
            start_seconds=0.0,
            maximum_frames=maximum_frames,
            search_cents=1_800.0,
        )
        pitch = {
            "status": assessment.status,
            "expected_hz": assessment.expected_hz,
            "measured_hz": assessment.measured_hz,
            "detune_cents": assessment.detune_cents,
            "confidence": assessment.confidence,
            "periodicity": assessment.periodicity,
            "harmonic_peak_coverage": assessment.harmonic_peak_coverage,
            "nearest_octave_error": assessment.nearest_octave_error,
            "reason": assessment.reason,
        }
        if assessment.nearest_octave_error not in (None, 0):
            blockers.append("octave_mapping_error")

    return _canonical_copy(
        {
            "analysis_version": 1,
            "sample_rate_hz": sample_rate,
            "frame_count": int(audio.shape[0]),
            "note_on_frame": note_on_frame,
            "note_off_frame": note_off_frame,
            "peak": peak,
            "peak_dbfs": _db(peak),
            "rms": rms,
            "loudness_proxy_dbfs": _db(rms),
            "crest_factor_db": _db(peak / max(rms, 1.0e-15)),
            "left_rms": float(channel_rms[0]),
            "right_rms": float(channel_rms[1]),
            "pre_roll_rms": pre_roll_rms,
            "tail_rms": tail_rms,
            "dc_offset_left": float(dc[0]),
            "dc_offset_right": float(dc[1]),
            "clipping_sample_count": clipping_samples,
            "spectral_centroid_hz": centroid,
            "spectral_rolloff_85_hz": rolloff,
            "spectral_bandwidth_hz": bandwidth,
            "spectral_flatness": flatness,
            "stereo_correlation": max(-1.0, min(1.0, correlation)),
            "stereo_width_ratio": stereo_width_ratio,
            "peak_after_note_on_seconds": peak_frame / sample_rate,
            "active_tail_seconds": tail_active_seconds,
            "pitch": pitch,
            "machine_blockers": sorted(set(blockers)),
        }
    )


def validate_coordinate(value: Any) -> dict[str, Any]:
    coordinate = _canonical_copy(value)
    if not isinstance(coordinate, dict) or set(coordinate) != _COORDINATE_KEYS:
        raise TimbreAuditError(
            "matrix coordinate has missing or unknown fields"
        )
    _require_sha256(
        coordinate["runtime_configuration_sha256"],
        "runtime_configuration_sha256",
    )
    if (
        not isinstance(coordinate["final_articulation"], str)
        or not coordinate["final_articulation"]
    ):
        raise TimbreAuditError("final_articulation must be non-empty")
    midi_note = coordinate["midi_note"]
    if midi_note is not None and (
        isinstance(midi_note, bool)
        or not isinstance(midi_note, int)
        or not 0 <= midi_note <= 127
    ):
        raise TimbreAuditError("midi_note must be null or an integer 0..127")
    velocity = coordinate["velocity"]
    if (
        isinstance(velocity, bool)
        or not isinstance(velocity, int)
        or not 1 <= velocity <= 127
    ):
        raise TimbreAuditError("velocity must be an integer 1..127")
    _require_sha256(
        coordinate["variant_lane_sha256"],
        "variant_lane_sha256",
    )
    _require_sha256(
        coordinate["variant_bundle_sha256"],
        "variant_bundle_sha256",
    )
    return coordinate


def coordinate_sha256(coordinate: Any) -> str:
    return stable_variant_sha256(
        "timbre-matrix-coordinate-v1",
        validate_coordinate(coordinate),
    )


def _comparison(
    kind: str,
    left_id: str,
    right_id: str,
    reasons: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not reasons:
        return None
    return {
        "kind": kind,
        "left_coordinate_sha256": left_id,
        "right_coordinate_sha256": right_id,
        "priority": "listen",
        "reasons": reasons,
    }


def _metric(metrics: dict[str, Any], name: str) -> float:
    value = metrics.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
    ):
        raise TimbreAuditError(f"metrics.{name} must be finite")
    return float(value)


def _validate_metrics(value: Any) -> dict[str, Any]:
    metrics = _canonical_copy(value)
    if not isinstance(metrics, dict) or set(metrics) != _METRIC_KEYS:
        raise TimbreAuditError(
            "metrics has missing or unknown analysis fields"
        )
    if metrics["analysis_version"] != 1:
        raise TimbreAuditError("metrics analysis_version is unsupported")
    integer_fields = {
        "sample_rate_hz",
        "frame_count",
        "note_on_frame",
        "note_off_frame",
        "clipping_sample_count",
    }
    for name in integer_fields:
        item = metrics[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise TimbreAuditError(f"metrics.{name} must be non-negative integer")
    if not 8_000 <= metrics["sample_rate_hz"] <= 384_000:
        raise TimbreAuditError("metrics.sample_rate_hz is outside the supported range")
    if not (
        0
        <= metrics["note_on_frame"]
        < metrics["note_off_frame"]
        <= metrics["frame_count"]
    ):
        raise TimbreAuditError("metrics note frames are inconsistent")
    for name in _METRIC_KEYS - integer_fields - {
        "analysis_version",
        "pitch",
        "machine_blockers",
    }:
        _metric(metrics, name)
    blockers = metrics["machine_blockers"]
    if (
        not isinstance(blockers, list)
        or any(not isinstance(item, str) or not item for item in blockers)
        or blockers != sorted(set(blockers))
    ):
        raise TimbreAuditError(
            "metrics.machine_blockers must be sorted unique strings"
        )
    pitch = metrics["pitch"]
    if pitch is not None:
        pitch_keys = {
            "status",
            "expected_hz",
            "measured_hz",
            "detune_cents",
            "confidence",
            "periodicity",
            "harmonic_peak_coverage",
            "nearest_octave_error",
            "reason",
        }
        if not isinstance(pitch, dict) or set(pitch) != pitch_keys:
            raise TimbreAuditError("metrics.pitch has missing or unknown fields")
        if pitch["status"] not in {"clear_pitch", "no_clear_pitch"}:
            raise TimbreAuditError("metrics.pitch status is invalid")
        if not isinstance(pitch["reason"], str) or not pitch["reason"]:
            raise TimbreAuditError("metrics.pitch reason must be non-empty")
        for name in (
            "expected_hz",
            "confidence",
            "periodicity",
            "harmonic_peak_coverage",
        ):
            item = pitch[name]
            if (
                isinstance(item, bool)
                or not isinstance(item, Real)
                or not math.isfinite(float(item))
            ):
                raise TimbreAuditError(f"metrics.pitch.{name} must be finite")
        for name in ("measured_hz", "detune_cents"):
            item = pitch[name]
            if item is not None and (
                isinstance(item, bool)
                or not isinstance(item, Real)
                or not math.isfinite(float(item))
            ):
                raise TimbreAuditError(
                    f"metrics.pitch.{name} must be null or finite"
                )
        octave = pitch["nearest_octave_error"]
        if octave is not None and (
            isinstance(octave, bool) or not isinstance(octave, int)
        ):
            raise TimbreAuditError(
                "metrics.pitch.nearest_octave_error must be null or integer"
            )
    return metrics


def _validate_source_facts(value: Any) -> dict[str, Any]:
    facts = _canonical_copy(value)
    if not isinstance(facts, dict) or set(facts) != _SOURCE_FACT_KEYS:
        raise TimbreAuditError(
            "source_facts has missing or unknown fields"
        )
    mode = facts["source_mode"]
    expected_scopes = {
        "sampled": "actual_audible_bundle",
        "procedural": "deterministic_program_topology",
        "not_applicable": "no_pitched_sample_roots",
        "unavailable": "unverified",
    }
    if mode not in expected_scopes:
        raise TimbreAuditError("source_facts.source_mode is invalid")
    if facts["selection_scope"] != expected_scopes[mode]:
        raise TimbreAuditError(
            "source_facts selection_scope does not match source_mode"
        )
    roots = facts["selected_root_midis"]
    transpositions = facts["transposition_semitones"]
    if not isinstance(roots, list) or not isinstance(transpositions, list):
        raise TimbreAuditError(
            "source_facts root and transposition fields must be arrays"
        )
    for label, values in (
        ("selected_root_midis", roots),
        ("transposition_semitones", transpositions),
    ):
        for item in values:
            if (
                isinstance(item, bool)
                or not isinstance(item, Real)
                or not math.isfinite(float(item))
            ):
                raise TimbreAuditError(
                    f"source_facts.{label} must contain finite numbers"
                )
    maximum = facts["maximum_absolute_transposition_semitones"]
    if mode == "sampled":
        if not roots or len(roots) != len(transpositions):
            raise TimbreAuditError(
                "sampled source_facts requires paired roots and transpositions"
            )
        expected_maximum = max(abs(float(item)) for item in transpositions)
        if (
            isinstance(maximum, bool)
            or not isinstance(maximum, Real)
            or not math.isfinite(float(maximum))
            or not math.isclose(
                float(maximum),
                expected_maximum,
                rel_tol=0.0,
                abs_tol=1.0e-9,
            )
        ):
            raise TimbreAuditError(
                "source_facts maximum transposition is inconsistent"
            )
    elif roots or transpositions or maximum is not None:
        raise TimbreAuditError(
            f"{mode} source_facts must not invent sample roots"
        )
    return facts


def build_machine_timbre_matrix_report(
    expected_coordinates: Iterable[dict[str, Any]],
    observations: Iterable[dict[str, Any]],
    *,
    triage_thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Verify matrix coverage and rank objective anomalies.

    Every observation must already bind its exact performance, PCM WAV,
    runtime selection receipt and runtime fingerprint.  Missing cells and
    objective signal failures block the batch before human review.
    """

    thresholds = dict(DEFAULT_TRIAGE_THRESHOLDS)
    if triage_thresholds is not None:
        unknown = set(triage_thresholds) - set(thresholds)
        if unknown:
            raise TimbreAuditError(
                "unknown triage thresholds: " + ", ".join(sorted(unknown))
            )
        for name, raw in triage_thresholds.items():
            if (
                isinstance(raw, bool)
                or not isinstance(raw, Real)
                or not math.isfinite(float(raw))
                or float(raw) < 0.0
            ):
                raise TimbreAuditError(
                    f"triage threshold {name} must be finite and non-negative"
                )
            thresholds[name] = float(raw)

    expected_by_id: dict[str, dict[str, Any]] = {}
    for raw in expected_coordinates:
        coordinate = validate_coordinate(raw)
        identifier = coordinate_sha256(coordinate)
        if identifier in expected_by_id:
            raise TimbreAuditError("expected matrix repeats a coordinate")
        expected_by_id[identifier] = coordinate
    if not expected_by_id:
        raise TimbreAuditError("expected matrix must not be empty")

    observed_by_id: dict[str, dict[str, Any]] = {}
    for raw in observations:
        observation = _canonical_copy(raw)
        if not isinstance(observation, dict) or set(observation) != _OBSERVATION_KEYS:
            raise TimbreAuditError(
                "matrix observation has missing or unknown fields"
            )
        coordinate = validate_coordinate(observation["coordinate"])
        identifier = coordinate_sha256(coordinate)
        if identifier in observed_by_id:
            raise TimbreAuditError("observations repeat a coordinate")
        for field in (
            "performance_sha256",
            "wav_sha256",
            "selection_receipt_sha256",
            "runtime_fingerprint_sha256",
        ):
            _require_sha256(observation[field], field)
        variant_coverage_status = observation["variant_coverage_status"]
        if variant_coverage_status not in {
            "all_runtime_variants",
            "runtime_default_only",
            "unexhausted",
        }:
            raise TimbreAuditError(
                "variant_coverage_status is invalid"
            )
        variant_proof = observation["variant_coverage_proof_sha256"]
        if variant_coverage_status == "all_runtime_variants":
            _require_sha256(
                variant_proof,
                "variant_coverage_proof_sha256",
            )
        elif variant_proof is not None:
            raise TimbreAuditError(
                "incomplete variant coverage cannot carry an approval proof hash"
            )
        source_facts = _validate_source_facts(observation["source_facts"])
        if source_facts["source_mode"] == "sampled":
            if coordinate["midi_note"] is None:
                raise TimbreAuditError(
                    "sampled source_facts requires a pitched matrix coordinate"
                )
            for root, transposition in zip(
                source_facts["selected_root_midis"],
                source_facts["transposition_semitones"],
            ):
                expected_transposition = float(coordinate["midi_note"]) - float(root)
                if not math.isclose(
                    float(transposition),
                    expected_transposition,
                    rel_tol=0.0,
                    abs_tol=1.0e-6,
                ):
                    raise TimbreAuditError(
                        "source_facts transposition does not match target minus root"
                    )
        metrics = _validate_metrics(observation["metrics"])
        observed_by_id[identifier] = {
            **observation,
            "coordinate": coordinate,
            "metrics": metrics,
            "source_facts": source_facts,
            "coordinate_sha256": identifier,
        }

    missing = sorted(set(expected_by_id) - set(observed_by_id))
    unexpected = sorted(set(observed_by_id) - set(expected_by_id))
    blockers: list[dict[str, Any]] = []
    if missing:
        blockers.append(
            {"kind": "missing_matrix_cells", "coordinate_sha256s": missing}
        )
    if unexpected:
        blockers.append(
            {
                "kind": "unexpected_matrix_cells",
                "coordinate_sha256s": unexpected,
            }
        )
    for identifier, observation in sorted(observed_by_id.items()):
        cell_blockers = set(
            observation["metrics"]["machine_blockers"]
        )
        if observation["source_facts"]["source_mode"] == "unavailable":
            cell_blockers.add("source_selection_facts_unavailable")
        if observation["variant_coverage_status"] != "all_runtime_variants":
            cell_blockers.add(
                "runtime_variant_coverage_incomplete:"
                + observation["variant_coverage_status"]
            )
        if cell_blockers:
            blockers.append(
                {
                    "kind": "cell_machine_blockers",
                    "coordinate_sha256": identifier,
                    "reasons": sorted(cell_blockers),
                }
            )

    comparisons: list[dict[str, Any]] = []

    # Adjacent-pitch continuity: only compare exactly the same configuration,
    # final articulation, velocity and stable variant lane.  A lane is the
    # same selector role (for example RR1) across notes; the exact audible
    # bundle is expected to change at sample-root boundaries.
    pitch_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for observation in observed_by_id.values():
        coordinate = observation["coordinate"]
        if coordinate["midi_note"] is None:
            continue
        pitch_groups[
            (
                coordinate["runtime_configuration_sha256"],
                coordinate["final_articulation"],
                coordinate["velocity"],
                coordinate["variant_lane_sha256"],
            )
        ].append(observation)
    for group in pitch_groups.values():
        group.sort(key=lambda item: item["coordinate"]["midi_note"])
        for left, right in zip(group, group[1:]):
            if (
                right["coordinate"]["midi_note"]
                - left["coordinate"]["midi_note"]
                != 1
            ):
                continue
            left_metrics, right_metrics = left["metrics"], right["metrics"]
            reasons: list[dict[str, Any]] = []
            loudness_step = abs(
                _metric(right_metrics, "loudness_proxy_dbfs")
                - _metric(left_metrics, "loudness_proxy_dbfs")
            )
            if loudness_step > thresholds["adjacent_loudness_step_db"]:
                reasons.append(
                    {"metric": "loudness_step_db", "value": loudness_step}
                )
            centroid_step = _octave_distance(
                _metric(left_metrics, "spectral_centroid_hz"),
                _metric(right_metrics, "spectral_centroid_hz"),
            )
            if centroid_step > thresholds[
                "adjacent_spectral_centroid_octaves"
            ]:
                reasons.append(
                    {
                        "metric": "spectral_centroid_step_octaves",
                        "value": centroid_step,
                    }
                )
            rolloff_step = _octave_distance(
                _metric(left_metrics, "spectral_rolloff_85_hz"),
                _metric(right_metrics, "spectral_rolloff_85_hz"),
            )
            if rolloff_step > thresholds[
                "adjacent_spectral_rolloff_octaves"
            ]:
                reasons.append(
                    {
                        "metric": "spectral_rolloff_step_octaves",
                        "value": rolloff_step,
                    }
                )
            flatness_step = abs(
                _metric(right_metrics, "spectral_flatness")
                - _metric(left_metrics, "spectral_flatness")
            )
            if flatness_step > thresholds["adjacent_flatness_step"]:
                reasons.append(
                    {
                        "metric": "spectral_flatness_step",
                        "value": flatness_step,
                    }
                )
            result = _comparison(
                "adjacent_pitch_continuity",
                left["coordinate_sha256"],
                right["coordinate_sha256"],
                reasons,
            )
            if result is not None:
                comparisons.append(result)
            if (
                left["coordinate"]["variant_bundle_sha256"]
                != right["coordinate"]["variant_bundle_sha256"]
            ):
                comparisons.append(
                    {
                        "kind": "source_mapping_boundary",
                        "left_coordinate_sha256": left[
                            "coordinate_sha256"
                        ],
                        "right_coordinate_sha256": right[
                            "coordinate_sha256"
                        ],
                        "priority": "listen",
                        "reasons": [
                            {
                                "metric": "audible_variant_bundle_changed",
                                "value": 1.0,
                            }
                        ],
                    }
                )

    # Velocity response: compare successive tested velocities at one exact
    # pitch and actual bundle.  A louder input becoming materially quieter is
    # an anomaly, not automatically an authenticity failure.
    velocity_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for observation in observed_by_id.values():
        coordinate = observation["coordinate"]
        velocity_groups[
            (
                coordinate["runtime_configuration_sha256"],
                coordinate["final_articulation"],
                coordinate["midi_note"],
                coordinate["variant_lane_sha256"],
            )
        ].append(observation)
    for group in velocity_groups.values():
        group.sort(key=lambda item: item["coordinate"]["velocity"])
        for left, right in zip(group, group[1:]):
            reasons = []
            reversal = (
                _metric(left["metrics"], "loudness_proxy_dbfs")
                - _metric(right["metrics"], "loudness_proxy_dbfs")
            )
            if reversal > thresholds["velocity_loudness_reversal_db"]:
                reasons.append(
                    {
                        "metric": "velocity_loudness_reversal_db",
                        "value": reversal,
                    }
                )
            centroid_step = _octave_distance(
                _metric(left["metrics"], "spectral_centroid_hz"),
                _metric(right["metrics"], "spectral_centroid_hz"),
            )
            if centroid_step > thresholds[
                "velocity_spectral_centroid_octaves"
            ]:
                reasons.append(
                    {
                        "metric": "velocity_centroid_step_octaves",
                        "value": centroid_step,
                    }
                )
            result = _comparison(
                "velocity_response",
                left["coordinate_sha256"],
                right["coordinate_sha256"],
                reasons,
            )
            if result is not None:
                comparisons.append(result)

    # Variant consistency: compare every audible bundle against its condition
    # median.  This does not assume the variants should be identical.
    variant_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for observation in observed_by_id.values():
        coordinate = observation["coordinate"]
        variant_groups[
            (
                coordinate["runtime_configuration_sha256"],
                coordinate["final_articulation"],
                coordinate["midi_note"],
                coordinate["velocity"],
            )
        ].append(observation)
    for group in variant_groups.values():
        if len(group) < 3:
            continue
        loudness_values = np.asarray(
            [_metric(item["metrics"], "loudness_proxy_dbfs") for item in group]
        )
        centroid_values = np.asarray(
            [_metric(item["metrics"], "spectral_centroid_hz") for item in group]
        )
        median_loudness = float(np.median(loudness_values))
        positive_centroids = centroid_values[centroid_values > 0.0]
        median_centroid = (
            float(np.median(positive_centroids))
            if positive_centroids.size
            else 0.0
        )
        anchor = min(group, key=lambda item: item["coordinate_sha256"])
        for item in group:
            reasons = []
            loudness_deviation = abs(
                _metric(item["metrics"], "loudness_proxy_dbfs")
                - median_loudness
            )
            if loudness_deviation > thresholds[
                "variant_loudness_deviation_db"
            ]:
                reasons.append(
                    {
                        "metric": "variant_loudness_deviation_db",
                        "value": loudness_deviation,
                    }
                )
            centroid_deviation = _octave_distance(
                median_centroid,
                _metric(item["metrics"], "spectral_centroid_hz"),
            )
            if centroid_deviation > thresholds[
                "variant_spectral_centroid_octaves"
            ]:
                reasons.append(
                    {
                        "metric": "variant_centroid_deviation_octaves",
                        "value": centroid_deviation,
                    }
                )
            result = _comparison(
                "variant_consistency",
                anchor["coordinate_sha256"],
                item["coordinate_sha256"],
                reasons,
            )
            if result is not None:
                comparisons.append(result)

    payload = {
        "schema_version": 1,
        "kind": "machine_timbre_matrix_report",
        "claim": CLAIM,
        "automatic_approval": False,
        "signal_stage": SIGNAL_STAGE,
        "matrix_plan_sha256": stable_variant_sha256(
            "timbre-matrix-plan-v1",
            {
                "signal_stage": SIGNAL_STAGE,
                "coordinates": [
                    {
                        "coordinate_sha256": identifier,
                        "coordinate": expected_by_id[identifier],
                    }
                    for identifier in sorted(expected_by_id)
                ],
            },
        ),
        "expected_coordinates": [
            {
                "coordinate_sha256": identifier,
                "coordinate": expected_by_id[identifier],
            }
            for identifier in sorted(expected_by_id)
        ],
        "coverage": {
            "expected_cell_count": len(expected_by_id),
            "observed_cell_count": len(observed_by_id),
            "complete": not missing and not unexpected,
            "missing_coordinate_sha256s": missing,
            "unexpected_coordinate_sha256s": unexpected,
        },
        "triage_thresholds": thresholds,
        "threshold_semantics": (
            "review_priority_only_not_authenticity_acceptance_limits"
        ),
        "cells": [
            observed_by_id[identifier]
            for identifier in sorted(observed_by_id)
        ],
        "machine_blockers": blockers,
        "anomaly_candidates": sorted(
            comparisons,
            key=lambda item: (
                item["kind"],
                item["left_coordinate_sha256"],
                item["right_coordinate_sha256"],
            ),
        ),
        "disposition": (
            "blocked_before_human_review"
            if blockers
            else "machine_complete_human_review_required"
        ),
    }
    return {
        **_canonical_copy(payload),
        "report_sha256": stable_variant_sha256(
            "machine-timbre-matrix-report-v1",
            payload,
        ),
    }


def validate_machine_timbre_matrix_report(value: Any) -> dict[str, Any]:
    """Validate the immutable claims and self-hash of a machine report."""

    report = _canonical_copy(value)
    required = {
        "schema_version",
        "kind",
        "claim",
        "automatic_approval",
        "signal_stage",
        "matrix_plan_sha256",
        "expected_coordinates",
        "coverage",
        "triage_thresholds",
        "threshold_semantics",
        "cells",
        "machine_blockers",
        "anomaly_candidates",
        "disposition",
        "report_sha256",
    }
    if not isinstance(report, dict) or set(report) != required:
        raise TimbreAuditError("machine timbre report has unknown or missing fields")
    if report["schema_version"] != 1:
        raise TimbreAuditError("unsupported machine timbre report version")
    if report["kind"] != "machine_timbre_matrix_report":
        raise TimbreAuditError("invalid machine timbre report kind")
    if report["claim"] != CLAIM or report["automatic_approval"] is not False:
        raise TimbreAuditError("machine timbre report must never claim approval")
    if report["signal_stage"] != SIGNAL_STAGE:
        raise TimbreAuditError("machine timbre report has the wrong signal stage")
    _require_sha256(report["matrix_plan_sha256"], "matrix_plan_sha256")
    _require_sha256(report["report_sha256"], "report_sha256")
    if not isinstance(report["expected_coordinates"], list):
        raise TimbreAuditError("expected_coordinates must be an array")
    expected_coordinates: list[dict[str, Any]] = []
    for index, record in enumerate(report["expected_coordinates"]):
        if (
            not isinstance(record, dict)
            or set(record) != {"coordinate_sha256", "coordinate"}
        ):
            raise TimbreAuditError(
                f"expected_coordinates[{index}] is invalid"
            )
        coordinate = validate_coordinate(record["coordinate"])
        if record["coordinate_sha256"] != coordinate_sha256(coordinate):
            raise TimbreAuditError(
                f"expected_coordinates[{index}] hash is invalid"
            )
        expected_coordinates.append(coordinate)
    if not isinstance(report["cells"], list):
        raise TimbreAuditError("cells must be an array")
    observations: list[dict[str, Any]] = []
    for index, cell in enumerate(report["cells"]):
        if not isinstance(cell, dict):
            raise TimbreAuditError(f"cells[{index}] must be an object")
        raw = dict(cell)
        identifier = raw.pop("coordinate_sha256", None)
        if identifier != coordinate_sha256(raw.get("coordinate")):
            raise TimbreAuditError(f"cells[{index}] coordinate hash is invalid")
        observations.append(raw)

    rebuilt = build_machine_timbre_matrix_report(
        expected_coordinates,
        observations,
        triage_thresholds=report["triage_thresholds"],
    )
    if report != rebuilt:
        expected_sha256 = stable_variant_sha256(
            "machine-timbre-matrix-report-v1",
            {key: item for key, item in report.items() if key != "report_sha256"},
        )
        if report["report_sha256"] != expected_sha256:
            raise TimbreAuditError("machine timbre report self-hash is invalid")
        raise TimbreAuditError(
            "machine timbre report does not recompute from its bound matrix"
        )
    if report["report_sha256"] != rebuilt["report_sha256"]:
        raise TimbreAuditError("machine timbre report self-hash is invalid")
    return report


__all__ = [
    "CLAIM",
    "DEFAULT_TRIAGE_THRESHOLDS",
    "SIGNAL_STAGE",
    "TimbreAuditError",
    "analyze_timbre_audio",
    "build_machine_timbre_matrix_report",
    "coordinate_sha256",
    "enumerate_integer_notes",
    "validate_coordinate",
    "validate_machine_timbre_matrix_report",
]
