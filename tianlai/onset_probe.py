r"""Machine probes for articulation-specific perceptual onset candidates.

This module deliberately stops at *candidate evidence*.  It renders isolated
attacks through the same public performance/factory/renderer path used by the
engine, measures their direct stereo output, and emits a hash-bound report for
later human review.  It never writes ``发音延迟.json`` and it never approves its
own measurements.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import copy
import hashlib
import json
import math
from numbers import Real
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Iterable, Sequence
import wave

import numpy as np

from .audio import read_wav_float, write_wav_pcm24
from .capability import (
    DEFAULT_ARTICULATION_SENTINEL,
    InstrumentCapability,
    read_capability,
)
from .events import PerformanceDocument, parse_performance_document
from .instrument import create_instrument
from .onset_evidence import (
    ANCHOR,
    APPROVABLE_VARIANT_COVERAGE,
    CANDIDATE_SCHEMA,
    CONTEXT,
    VARIANT_COVERAGE,
    canonical_sha256,
    compute_runtime_fingerprint,
    validate_candidate_report,
)
from .renderer import render_document
from .runtime_variants import (
    RuntimeVariantNotCertifiable,
    capture_runtime_variants,
    certify_runtime_variant_observation,
    dedicated_sfz_finite_rr_variation_period,
    onset_sampled_condition,
    onset_sampled_condition_id,
    prewarm_dedicated_sfz_variation_slot,
)


DEFAULT_VELOCITIES = (32, 80, 120)
REPORT_FILENAME = "逐奏法发音探针候选.json"
ALGORITHM_NAME = "tianlai_stereo_power_onset_v1"
WINDOW_MS = 5.0
HOP_MS = 1.0
THRESHOLD_POLICY = (
    "max(pre_roll_p95_rms*4,pre_roll_median_rms*8,"
    "post_peak_rms*0.01,pcm24_lsb*8); "
    "pre_roll_leak=max(pcm24_lsb*4,post_peak_rms*0.001)"
)
SIGNAL_STAGE = "instrument_direct_output_no_space"
_FORBIDDEN_ARTICULATION_PREFIX = "crescendo_"


def _require_integer(
    value: Any,
    label: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} must be at most {maximum}")
    return value


def _require_finite_number(value: Any, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{label} must be a finite number")
    return float(value)


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    """One isolated render/measurement observation.

    A fresh instrument instance is created for every instance of this record.
    ``velocity`` uses the conventional MIDI integer scale so reports remain
    easy to compare with scores and sample-layer declarations.
    """

    manifest_path: Path
    output_directory: Path
    articulation: str | None
    midi_note: int
    velocity: int = 80
    repeat_index: int = 0
    variation_slot: int = 0
    sample_rate: int = 48_000
    pre_roll_seconds: float = 1.0
    note_seconds: float = 4.0
    tail_seconds: float = 0.5

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_path", Path(self.manifest_path).resolve())
        object.__setattr__(
            self, "output_directory", Path(self.output_directory).resolve()
        )
        if self.articulation is not None and not self.articulation.strip():
            raise ValueError("articulation must be non-empty or None")
        _require_integer(self.midi_note, "midi_note", minimum=0, maximum=127)
        _require_integer(self.velocity, "velocity", minimum=1, maximum=127)
        _require_integer(self.repeat_index, "repeat_index", minimum=0)
        _require_integer(self.variation_slot, "variation_slot", minimum=0)
        _require_integer(
            self.sample_rate,
            "sample_rate",
            minimum=8_000,
            maximum=384_000,
        )
        for field, value, allow_zero in (
            ("pre_roll_seconds", self.pre_roll_seconds, False),
            ("note_seconds", self.note_seconds, False),
            ("tail_seconds", self.tail_seconds, True),
        ):
            numeric_value = _require_finite_number(value, field)
            if numeric_value < 0.0 or (
                not allow_zero and numeric_value == 0.0
            ):
                qualifier = "non-negative" if allow_zero else "positive"
                raise ValueError(f"{field} must be finite and {qualifier}")
        if round(self.pre_roll_seconds * self.sample_rate) < 1:
            raise ValueError("pre_roll_seconds must contain at least one frame")
        if round(self.note_seconds * self.sample_rate) < 1:
            raise ValueError("note_seconds must contain at least one frame")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def select_probe_notes(
    ranges: Iterable[tuple[float, float] | Sequence[float]],
) -> tuple[int, ...]:
    """Select low/middle/high legal integer notes from every disjoint span.

    Selection happens inside each span, so a midpoint can never accidentally
    land in a range hole.  Duplicates from one- and two-note spans are removed
    while preserving declaration order.
    """

    selected: list[int] = []
    seen: set[int] = set()
    previous_high = -math.inf
    found = False
    for index, raw_span in enumerate(ranges):
        if len(raw_span) != 2:
            raise ValueError(f"ranges[{index}] must be a [low, high] pair")
        low = _require_finite_number(
            raw_span[0],
            f"ranges[{index}].low",
        )
        high = _require_finite_number(
            raw_span[1],
            f"ranges[{index}].high",
        )
        if not 0.0 <= low <= high <= 127.0:
            raise ValueError(f"ranges[{index}] is outside the MIDI range")
        if low <= previous_high:
            raise ValueError("ranges must be ordered and non-overlapping")
        previous_high = high
        integer_low = math.ceil(low)
        integer_high = math.floor(high)
        if integer_low > integer_high:
            raise ValueError(f"ranges[{index}] contains no legal integer MIDI note")
        middle = (integer_low + integer_high) // 2
        for note in (integer_low, middle, integer_high):
            if note not in seen:
                seen.add(note)
                selected.append(note)
        found = True
    if not found:
        raise ValueError("at least one playable range is required")
    return tuple(selected)


# A descriptive alias used by callers that want the range semantics in the
# function name.
select_range_probe_notes = select_probe_notes


def _crossing_frame(
    coordinates: np.ndarray,
    values: np.ndarray,
    threshold: float,
    note_on_frame: int,
) -> int | None:
    indexes = np.flatnonzero(values >= threshold)
    if indexes.size == 0:
        return None
    relative = float(coordinates[int(indexes[0])]) - note_on_frame
    return max(0, round(relative))


def analyze_stereo_onset(
    frames: Sequence[Sequence[float]] | np.ndarray,
    sample_rate: int,
    note_on_frame: int,
    *,
    note_off_frame: int,
    pre_quantization_clipping_sample_count: int | None = None,
    window_ms: float = WINDOW_MS,
    hop_ms: float = HOP_MS,
) -> dict[str, Any]:
    """Measure onset from stereo power, preserving antiphase information.

    RMS is calculated from ``(left² + right²) / 2``.  Summing channels first
    would erase a perfectly valid antiphase stereo signal and is explicitly
    avoided here.
    """

    sample_rate = _require_integer(
        sample_rate,
        "sample_rate",
        minimum=8_000,
        maximum=384_000,
    )
    note_on_frame = _require_integer(
        note_on_frame,
        "note_on_frame",
        minimum=0,
    )
    note_off_frame = _require_integer(
        note_off_frame,
        "note_off_frame",
        minimum=0,
    )
    window_ms = _require_finite_number(window_ms, "window_ms")
    hop_ms = _require_finite_number(hop_ms, "hop_ms")
    if window_ms <= 0.0:
        raise ValueError("window_ms must be positive")
    if hop_ms <= 0.0:
        raise ValueError("hop_ms must be positive")

    audio = np.asarray(frames, dtype=np.float64)
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise ValueError("frames must have shape (frame_count, 2)")
    if audio.shape[0] < 2:
        raise ValueError("frames are too short")
    if not np.all(np.isfinite(audio)):
        raise ValueError("frames must contain only finite samples")
    if not 0 <= note_on_frame < audio.shape[0]:
        raise ValueError("note_on_frame is outside the rendered audio")
    if not note_on_frame < note_off_frame <= audio.shape[0]:
        raise ValueError(
            "note_off_frame must be after note_on_frame and inside the audio"
        )
    if pre_quantization_clipping_sample_count is None:
        clipping_sample_count = int(np.count_nonzero(np.abs(audio) >= 1.0))
    elif (
        isinstance(pre_quantization_clipping_sample_count, bool)
        or not isinstance(pre_quantization_clipping_sample_count, int)
        or pre_quantization_clipping_sample_count < 0
    ):
        raise ValueError(
            "pre_quantization_clipping_sample_count must be a non-negative integer"
        )
    else:
        clipping_sample_count = pre_quantization_clipping_sample_count
    clipped = clipping_sample_count > 0

    window_frames = max(1, round(window_ms * sample_rate / 1000.0))
    hop_frames = max(1, round(hop_ms * sample_rate / 1000.0))
    if audio.shape[0] < window_frames:
        raise ValueError("frames are shorter than one RMS window")

    power = np.mean(np.square(audio), axis=1)
    cumulative = np.concatenate(([0.0], np.cumsum(power, dtype=np.float64)))
    starts = np.arange(
        0, audio.shape[0] - window_frames + 1, hop_frames, dtype=np.int64
    )
    ends = starts + window_frames
    # A centered RMS timestamp avoids the old systematic +5 ms error caused by
    # labeling a complete window with its start.  Half-frame centres are kept
    # as floats until the final report is rounded to an integer frame.
    centers = starts.astype(np.float64) + (window_frames - 1) / 2.0
    rms = np.sqrt(
        np.maximum(
            0.0,
            (cumulative[starts + window_frames] - cumulative[starts])
            / window_frames,
        )
    )

    pre_mask = ends <= note_on_frame
    pre_rms = rms[pre_mask]
    pcm24_lsb = 1.0 / 8_388_608.0
    if pre_rms.size:
        noise_floor = float(np.median(pre_rms))
        noise_p95 = float(np.percentile(pre_rms, 95.0))
        pre_roll_peak = float(np.max(pre_rms))
    else:
        noise_floor = 0.0
        noise_p95 = 0.0
        pre_roll_peak = 0.0

    # Only complete attack windows are eligible.  In particular, release and
    # tail energy after note_off can never become an onset candidate.
    post_mask = (centers >= note_on_frame) & (ends <= note_off_frame)
    post_centers = centers[post_mask]
    post_rms = rms[post_mask]

    if post_rms.size == 0:
        leak_threshold = 4.0 * pcm24_lsb
        pre_roll_leak = pre_roll_peak > leak_threshold
        reasons = ["no complete note_on-to-note_off RMS window"]
        if pre_roll_leak:
            reasons.append("pre-roll contains direct-output energy")
        if clipped:
            reasons.append("pre-quantization output clipped")
        return {
            "status": "unresolved",
            "candidate_onset_frame": None,
            "t10_frame": None,
            "t50_frame": None,
            "t90_frame": None,
            "peak_frame": None,
            "snr_db": None,
            "pre_roll_leak": pre_roll_leak,
            "clipped": clipped,
            "reason": "; ".join(reasons),
            "noise_floor_rms": noise_floor,
            "threshold_rms": None,
            "peak_rms": None,
            "clipping_sample_count": clipping_sample_count,
            "pre_roll_peak_rms": pre_roll_peak,
        }

    peak_index = int(np.argmax(post_rms))
    peak_rms = float(post_rms[peak_index])
    adaptive_noise_threshold = max(
        noise_p95 * 4.0,
        noise_floor * 8.0,
        pcm24_lsb * 8.0,
    )
    threshold = max(adaptive_noise_threshold, peak_rms * 0.01)
    noise_reference = max(noise_floor, noise_p95, pcm24_lsb)
    snr_db = (
        20.0 * math.log10(peak_rms / noise_reference)
        if peak_rms > 0.0
        else None
    )
    # Leakage is a separate validity gate, not an adaptive estimate derived
    # from the leakage itself.  A few PCM24 LSBs protect against quantization
    # dust, while the post-signal-relative term scales for very loud probes.
    leak_threshold = max(pcm24_lsb * 4.0, peak_rms * 0.001)
    pre_roll_leak = pre_roll_peak > leak_threshold
    audible = peak_rms > adaptive_noise_threshold

    if audible:
        onset_frame = _crossing_frame(
            post_centers, post_rms, threshold, note_on_frame
        )
        t10 = _crossing_frame(
            post_centers, post_rms, peak_rms * 0.10, note_on_frame
        )
        t50 = _crossing_frame(
            post_centers, post_rms, peak_rms * 0.50, note_on_frame
        )
        t90 = _crossing_frame(
            post_centers, post_rms, peak_rms * 0.90, note_on_frame
        )
        peak_frame = max(
            0, round(float(post_centers[peak_index]) - note_on_frame)
        )
    else:
        onset_frame = t10 = t50 = t90 = peak_frame = None

    attack_frames = note_off_frame - note_on_frame
    end_guard_frames = max(
        window_frames,
        min(round(0.050 * sample_rate), max(1, attack_frames // 10)),
    )
    peak_near_analysis_end = (
        audible
        and note_off_frame - float(post_centers[peak_index])
        <= end_guard_frames
    )
    blockers: list[str] = []
    if not audible:
        blockers.append(
            "note_on-to-note_off power does not clear the adaptive noise threshold"
        )
    if pre_roll_leak:
        blockers.append("pre-roll contains direct-output energy")
    if clipped:
        blockers.append("pre-quantization output clipped")
    if peak_near_analysis_end:
        blockers.append(
            "power peak is too close to the attack analysis end; "
            "render a longer note"
        )
    if onset_frame is None and audible:
        blockers.append("adaptive onset threshold has no crossing")

    status = "unresolved" if blockers else "proposed"
    reason = "; ".join(blockers) if blockers else None
    candidate_onset_frame = None if blockers else onset_frame

    return {
        "status": status,
        "candidate_onset_frame": candidate_onset_frame,
        "t10_frame": t10,
        "t50_frame": t50,
        "t90_frame": t90,
        "peak_frame": peak_frame,
        "snr_db": snr_db,
        "pre_roll_leak": pre_roll_leak,
        "clipped": clipped,
        "reason": reason,
        "noise_floor_rms": noise_floor,
        "threshold_rms": threshold,
        "peak_rms": peak_rms,
        "clipping_sample_count": clipping_sample_count,
        "pre_roll_peak_rms": pre_roll_peak,
    }


analyze_onset = analyze_stereo_onset


def _project_root(path: Path) -> Path:
    for candidate in (path.parent, *path.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "tianlai"
        ).is_dir():
            return candidate.resolve()
    raise ValueError(
        "manifest must be inside a project root containing pyproject.toml and tianlai/"
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError(f"path must stay inside project root {root}: {path}") from error


def _safe_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return cleaned[:48] or "default"


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    path.write_text(encoded, encoding="utf-8")


def _performance_data(spec: ProbeSpec) -> dict[str, Any]:
    note_on_time = spec.pre_roll_seconds
    note_off_time = note_on_time + spec.note_seconds
    events: list[dict[str, Any]] = []
    if spec.articulation is not None:
        events.append(
            {
                "time": 0.0,
                "type": "articulation",
                "name": spec.articulation,
            }
        )
    events.extend(
        (
            {
                "time": note_on_time,
                "type": "note_on",
                "note_id": 1,
                "midi_note": spec.midi_note,
                "velocity": spec.velocity / 127.0,
            },
            {
                "time": note_off_time,
                "type": "note_off",
                "note_id": 1,
                "release_velocity": 0.5,
            },
        )
    )
    return {
        "sample_rate": spec.sample_rate,
        "channels": 2,
        "duration_seconds": note_off_time + spec.tail_seconds,
        "tail_seconds": spec.tail_seconds,
        "events": events,
    }


def _single_event_frame(document: PerformanceDocument, event_type: str) -> int:
    matches = [
        event.sample for event in document.events if event.type == event_type
    ]
    if len(matches) != 1:
        raise ValueError(
            f"probe performance must contain exactly one {event_type}"
        )
    return matches[0]


def _render_one(
    spec: ProbeSpec,
    *,
    manifest: dict[str, Any],
    performance_path: Path,
    wav_path: Path,
) -> tuple[dict[str, Any], int, str, dict[str, Any], dict[str, Any] | None]:
    performance_data = _performance_data(spec)
    _write_json(performance_path, performance_data)
    document = parse_performance_document(performance_data)
    note_on_frame = _single_event_frame(document, "note_on")
    note_off_frame = _single_event_frame(document, "note_off")

    final_articulation = (
        spec.articulation
        if spec.articulation is not None
        else DEFAULT_ARTICULATION_SENTINEL
    )
    condition_id = onset_sampled_condition_id(
        final_articulation=final_articulation,
        midi_note=spec.midi_note,
        velocity=spec.velocity,
        sample_rate_hz=spec.sample_rate,
    )
    sampled_condition = onset_sampled_condition(
        final_articulation=final_articulation,
        midi_note=spec.midi_note,
        velocity=spec.velocity,
        sample_rate_hz=spec.sample_rate,
    )
    instrument = create_instrument(
        copy.deepcopy(manifest),
        spec.sample_rate,
        base_directory=str(spec.manifest_path.parent),
    )
    try:
        if spec.variation_slot:
            prewarm_dedicated_sfz_variation_slot(
                instrument=instrument,
                manifest=manifest,
                sampled_condition=sampled_condition,
                variation_slot=spec.variation_slot,
            )
        stream, _peak_voice_count = render_document(instrument, document)
        iterator = iter(stream)
        frames: list[tuple[float, float]] = []
        attack_capture = None
        # Runtime-variant onset evidence has one explicit selection phase:
        # the generator step that dispatches note_on and renders its first
        # output frame.  Attack-layer selectors are captured there; SFZ
        # release-trigger selectors at note_off remain outside this receipt.
        # Candidate replay uses the same event boundary.
        for frame_index in range(document.total_samples):
            if frame_index == note_on_frame:
                with capture_runtime_variants() as capture:
                    frames.append(next(iterator))
                attack_capture = capture
            else:
                frames.append(next(iterator))
        rendered = np.asarray(frames, dtype=np.float64)
        if attack_capture is None:
            raise RuntimeError(
                "renderer never reached the certified note_on capture frame"
            )
        selection_receipt = attack_capture.receipt()
        try:
            variant_catalog_proof = certify_runtime_variant_observation(
                instrument=instrument,
                manifest=manifest,
                selection_receipt=selection_receipt,
                condition_id=condition_id,
                sampled_condition=sampled_condition,
                variation_slot=spec.variation_slot,
            )
        except RuntimeVariantNotCertifiable:
            # A capture-only receipt remains useful audit evidence, but it can
            # never authorize all_runtime_variants by itself.
            variant_catalog_proof = None
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()

    if rendered.shape != (document.total_samples, 2):
        raise RuntimeError("renderer returned an invalid direct stereo frame stream")
    if not np.all(np.isfinite(rendered)):
        raise RuntimeError("renderer returned non-finite direct stereo samples")
    pre_quantization_clipping_sample_count = int(
        np.count_nonzero(np.abs(rendered) >= 1.0)
    )
    written = write_wav_pcm24(wav_path, rendered, spec.sample_rate)
    if written != document.total_samples:
        raise RuntimeError("PCM24 writer returned an unexpected frame count")
    with wave.open(str(wav_path), "rb") as source:
        if (
            source.getnchannels() != 2
            or source.getsampwidth() != 3
            or source.getframerate() != spec.sample_rate
            or source.getnframes() != document.total_samples
        ):
            raise RuntimeError("probe WAV is not stereo PCM24 at the requested rate")

    decoded_rate, decoded_frames = read_wav_float(wav_path)
    if decoded_rate != spec.sample_rate:
        raise RuntimeError("decoded probe WAV changed sample rate")
    quantized = np.asarray(decoded_frames, dtype=np.float64)
    if quantized.shape != rendered.shape:
        raise RuntimeError("decoded probe WAV changed frame count or channels")
    analysis = analyze_stereo_onset(
        quantized,
        spec.sample_rate,
        note_on_frame,
        note_off_frame=note_off_frame,
        pre_quantization_clipping_sample_count=(
            pre_quantization_clipping_sample_count
        ),
    )
    return (
        analysis,
        note_on_frame,
        condition_id,
        selection_receipt,
        variant_catalog_proof,
    )


def _effective_articulations(
    capability: InstrumentCapability,
    requested: Sequence[str] | None,
) -> tuple[tuple[str | None, str], ...]:
    available = capability.articulations
    if requested:
        normalized: list[str] = []
        for raw in requested:
            name = raw.strip()
            if not name:
                raise ValueError("requested articulation must not be empty")
            if name.lower().startswith(_FORBIDDEN_ARTICULATION_PREFIX):
                raise ValueError(f"forbidden anticipatory articulation: {name}")
            if name not in available and name != capability.default_articulation:
                raise ValueError(f"unsupported articulation: {name}")
            if name not in normalized:
                normalized.append(name)
        return tuple((name, name) for name in normalized)

    eligible = tuple(
        name
        for name in available
        if not name.lower().startswith(_FORBIDDEN_ARTICULATION_PREFIX)
    )
    if eligible:
        return tuple((name, name) for name in eligible)
    if available:
        raise ValueError("instrument has no onset-safe articulation to probe")
    label = capability.default_articulation or DEFAULT_ARTICULATION_SENTINEL
    if label.lower().startswith(_FORBIDDEN_ARTICULATION_PREFIX):
        raise ValueError(f"forbidden anticipatory articulation: {label}")
    # No vocabulary means no articulation event.  The report uses an explicit
    # sentinel so absence cannot be confused with a backend articulation named
    # "default".
    return ((None, DEFAULT_ARTICULATION_SENTINEL),)


def _notes_for(
    capability: InstrumentCapability,
    articulation: str | None,
) -> tuple[int, ...]:
    if capability.fixed_midi_note is not None and capability.ignores_pitch:
        note = round(capability.fixed_midi_note)
        if not 0 <= note <= 127:
            raise ValueError("fixed_midi_note is outside the MIDI range")
        return (note,)
    ranges = capability.ranges_for(articulation)
    if ranges:
        return select_probe_notes(ranges)
    if capability.fixed_midi_note is not None:
        note = round(capability.fixed_midi_note)
        if not 0 <= note <= 127:
            raise ValueError("fixed_midi_note is outside the MIDI range")
        return (note,)
    # Some unpitched/effect backends intentionally ignore the incoming note.
    if not capability.pitched or capability.ignores_pitch:
        return (60,)
    raise ValueError("instrument has no declared playable range for onset probing")


def _validate_probe_manifest(manifest: dict[str, Any]) -> None:
    if str(manifest.get("type", "")).strip().lower() == "reversed_cymbal":
        raise ValueError("reversed_cymbal is anticipatory and cannot be onset-probed")
    if str(manifest.get("license_status", "")).strip().lower() == "quarantined":
        raise ValueError(
            "license_status=quarantined instruments cannot produce onset evidence"
        )


def _expand_probe_specs(
    manifest_path: Path,
    output_directory: Path,
    *,
    manifest: dict[str, Any],
    capability: InstrumentCapability,
    articulations: Sequence[str] | None = None,
    repeat: int = 1,
    sample_rate: int = 48_000,
    pre_roll_seconds: float = 1.0,
    note_seconds: float = 4.0,
    tail_seconds: float = 0.5,
    velocities: Sequence[int] = DEFAULT_VELOCITIES,
) -> tuple[ProbeSpec, ...]:
    repeat = _require_integer(repeat, "repeat", minimum=1)
    normalized_velocities = tuple(velocities)
    if not normalized_velocities:
        raise ValueError("velocities must not be empty")
    if any(
        isinstance(value, bool) or not isinstance(value, int)
        for value in normalized_velocities
    ):
        raise ValueError("velocities must contain only integers")
    if len(set(normalized_velocities)) != len(normalized_velocities):
        raise ValueError("velocities must not contain duplicates")

    _validate_probe_manifest(manifest)
    if capability.license_status == "quarantined":
        raise ValueError(
            "quarantined instrument capability cannot produce onset evidence"
        )

    root = _project_root(manifest_path)
    _relative(output_directory, root)
    articulation_pairs = _effective_articulations(capability, articulations)

    specs: list[ProbeSpec] = []
    for articulation, _label in articulation_pairs:
        for midi_note in _notes_for(capability, articulation):
            for velocity in normalized_velocities:
                for repeat_index in range(repeat):
                    specs.append(
                        ProbeSpec(
                            manifest_path=manifest_path,
                            output_directory=output_directory,
                            articulation=articulation,
                            midi_note=midi_note,
                            velocity=velocity,
                            repeat_index=repeat_index,
                            sample_rate=sample_rate,
                            pre_roll_seconds=pre_roll_seconds,
                            note_seconds=note_seconds,
                            tail_seconds=tail_seconds,
                        )
                    )
    return tuple(specs)


def _expand_natural_finite_rr_slots(
    specs: Sequence[ProbeSpec],
    *,
    manifest: dict[str, Any],
) -> tuple[ProbeSpec, ...]:
    """Expand exact Dedicated SFZ conditions into their bounded RR cycle."""

    if manifest.get("type") != "dedicated_sfz":
        return tuple(specs)
    periods: dict[str, int] = {}
    expanded: list[ProbeSpec] = []
    for spec in specs:
        final_articulation = (
            spec.articulation
            if spec.articulation is not None
            else DEFAULT_ARTICULATION_SENTINEL
        )
        sampled_condition = onset_sampled_condition(
            final_articulation=final_articulation,
            midi_note=spec.midi_note,
            velocity=spec.velocity,
            sample_rate_hz=spec.sample_rate,
        )
        condition_id = onset_sampled_condition_id(
            final_articulation=final_articulation,
            midi_note=spec.midi_note,
            velocity=spec.velocity,
            sample_rate_hz=spec.sample_rate,
        )
        period = periods.get(condition_id)
        if period is None:
            planning_instrument = create_instrument(
                copy.deepcopy(manifest),
                spec.sample_rate,
                base_directory=str(spec.manifest_path.parent),
            )
            try:
                try:
                    period = dedicated_sfz_finite_rr_variation_period(
                        instrument=planning_instrument,
                        manifest=manifest,
                        sampled_condition=sampled_condition,
                    )
                except RuntimeVariantNotCertifiable:
                    period = 1
            finally:
                close = getattr(planning_instrument, "close", None)
                if callable(close):
                    close()
            periods[condition_id] = period
        expanded.extend(
            replace(spec, variation_slot=slot)
            for slot in range(period)
        )
    return tuple(expanded)


def build_probe_specs(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    articulations: Sequence[str] | None = None,
    repeat: int = 1,
    sample_rate: int = 48_000,
    pre_roll_seconds: float = 1.0,
    note_seconds: float = 4.0,
    tail_seconds: float = 0.5,
    velocities: Sequence[int] = DEFAULT_VELOCITIES,
) -> tuple[ProbeSpec, ...]:
    """Expand one fixed manifest snapshot into deterministic observations."""

    manifest_path = Path(manifest_path).resolve()
    output_directory = Path(output_directory).resolve()
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except OSError as error:
        raise ValueError(
            f"instrument manifest does not exist: {manifest_path}"
        ) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid instrument manifest: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise ValueError("instrument manifest root must be an object")
    _validate_probe_manifest(manifest)
    capability = read_capability(manifest_path, root=manifest_path.parent)
    if manifest_path.read_bytes() != manifest_bytes:
        raise RuntimeError("instrument manifest changed while building probe specs")
    return _expand_probe_specs(
        manifest_path,
        output_directory,
        manifest=manifest,
        capability=capability,
        articulations=articulations,
        repeat=repeat,
        sample_rate=sample_rate,
        pre_roll_seconds=pre_roll_seconds,
        note_seconds=note_seconds,
        tail_seconds=tail_seconds,
        velocities=velocities,
    )


def _publish_new_directory(staging: Path, output: Path) -> None:
    if output.exists():
        raise FileExistsError(
            f"refusing to replace an existing probe batch directory: {output}"
        )
    os.replace(staging, output)


def _assert_runtime_snapshot(
    *,
    project_root: Path,
    manifest_path: Path,
    initial_manifest_bytes: bytes,
    initial_runtime_fingerprint: dict[str, Any],
    sample_rate_hz: int,
) -> None:
    try:
        current_manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        raise RuntimeError(
            "instrument manifest disappeared during onset probing"
        ) from error
    if current_manifest_bytes != initial_manifest_bytes:
        raise RuntimeError("instrument manifest changed during onset probing")
    current_fingerprint = compute_runtime_fingerprint(
        project_root,
        manifest_path,
        sample_rate_hz=sample_rate_hz,
    )
    if current_fingerprint != initial_runtime_fingerprint:
        raise RuntimeError(
            "instrument runtime fingerprint changed during onset probing"
        )


def run_probe_batch(
    manifest_path: str | Path,
    output_directory: str | Path,
    *,
    articulations: Sequence[str] | None = None,
    repeat: int = 1,
    sample_rate: int = 48_000,
    pre_roll_seconds: float = 1.0,
    note_seconds: float = 4.0,
    tail_seconds: float = 0.5,
    velocities: Sequence[int] = DEFAULT_VELOCITIES,
) -> dict[str, Any]:
    """Render, analyze, hash, then atomically publish one complete batch."""

    manifest_path = Path(manifest_path).resolve()
    output_directory = Path(output_directory).resolve()
    try:
        initial_manifest_bytes = manifest_path.read_bytes()
    except OSError as error:
        raise ValueError(
            f"instrument manifest does not exist: {manifest_path}"
        ) from error
    try:
        manifest = json.loads(initial_manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"invalid instrument manifest: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise ValueError("instrument manifest root must be an object")
    _validate_probe_manifest(manifest)
    initial_manifest_sha256 = hashlib.sha256(initial_manifest_bytes).hexdigest()

    project_root = _project_root(manifest_path)
    relative_manifest = _relative(manifest_path, project_root)
    _relative(output_directory, project_root)
    runtime_fingerprint = compute_runtime_fingerprint(
        project_root,
        manifest_path,
        sample_rate_hz=sample_rate,
    )
    if (
        runtime_fingerprint.get("manifest", {}).get("sha256")
        != initial_manifest_sha256
    ):
        raise RuntimeError("manifest changed while its runtime snapshot was created")

    capability = read_capability(manifest_path, root=manifest_path.parent)
    if manifest_path.read_bytes() != initial_manifest_bytes:
        raise RuntimeError("instrument manifest changed while building probe specs")
    specs = _expand_probe_specs(
        manifest_path,
        output_directory,
        manifest=manifest,
        capability=capability,
        articulations=articulations,
        repeat=repeat,
        sample_rate=sample_rate,
        pre_roll_seconds=pre_roll_seconds,
        note_seconds=note_seconds,
        tail_seconds=tail_seconds,
        velocities=velocities,
    )
    specs = _expand_natural_finite_rr_slots(
        specs,
        manifest=manifest,
    )
    if not specs:
        raise ValueError("probe batch expanded to no observations")
    if output_directory.exists():
        raise FileExistsError(
            f"refusing to replace an existing probe batch directory: "
            f"{output_directory}"
        )

    _assert_runtime_snapshot(
        project_root=project_root,
        manifest_path=manifest_path,
        initial_manifest_bytes=initial_manifest_bytes,
        initial_runtime_fingerprint=runtime_fingerprint,
        sample_rate_hz=sample_rate,
    )
    algorithm_sha256 = sha256_file(Path(__file__))
    output_directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_directory.name}.staging-",
            dir=output_directory.parent,
        )
    )
    observations: list[dict[str, Any]] = []
    staged_artifacts: list[tuple[Path, Path]] = []
    try:
        for index, spec in enumerate(specs, start=1):
            final_articulation = (
                spec.articulation
                if spec.articulation is not None
                else DEFAULT_ARTICULATION_SENTINEL
            )
            stem = (
                f"probe-{index:04d}_{_safe_component(final_articulation)}"
                f"_m{spec.midi_note:03d}_v{spec.velocity:03d}"
                f"_r{spec.repeat_index + 1:02d}"
                f"_s{spec.variation_slot + 1:02d}"
            )
            staged_performance = staging / "performance" / f"{stem}.json"
            staged_wav = staging / "wav" / f"{stem}.wav"
            (
                analysis,
                note_on_frame,
                condition_id,
                selection_receipt,
                variant_catalog_proof,
            ) = _render_one(
                spec,
                manifest=manifest,
                performance_path=staged_performance,
                wav_path=staged_wav,
            )
            final_performance = output_directory / "performance" / f"{stem}.json"
            final_wav = output_directory / "wav" / f"{stem}.wav"
            staged_artifacts.append((staged_performance, staged_wav))
            observations.append(
                {
                    "observation_id": stem,
                    "final_articulation": final_articulation,
                    "midi_note": spec.midi_note,
                    "velocity": spec.velocity,
                    "performance_path": _relative(
                        final_performance, project_root
                    ),
                    "performance_sha256": sha256_file(staged_performance),
                    "wav_path": _relative(final_wav, project_root),
                    "wav_sha256": sha256_file(staged_wav),
                    "note_on_frame": note_on_frame,
                    "condition_id": condition_id,
                    "variation_slot": spec.variation_slot,
                    "variant_catalog_proof": variant_catalog_proof,
                    "selection_receipt": selection_receipt,
                    "analysis": analysis,
                }
            )

        _assert_runtime_snapshot(
            project_root=project_root,
            manifest_path=manifest_path,
            initial_manifest_bytes=initial_manifest_bytes,
            initial_runtime_fingerprint=runtime_fingerprint,
            sample_rate_hz=sample_rate,
        )
        sampled_condition_ids = sorted(
            {
                observation["condition_id"]
                for observation in observations
            }
        )
        variant_coverage = (
            APPROVABLE_VARIANT_COVERAGE
            if all(
                observation["variant_catalog_proof"] is not None
                for observation in observations
            )
            else VARIANT_COVERAGE
        )
        report: dict[str, Any] = {
            "$schema": CANDIDATE_SCHEMA,
            "schema_version": 1,
            "kind": "onset_candidate_report",
            "candidate_sha256": "",
            "automatic_approval": False,
            "created_at": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "instrument": {
                "manifest_path": relative_manifest,
                "manifest_sha256": initial_manifest_sha256,
            },
            "runtime_fingerprint": runtime_fingerprint,
            "protocol": {
                "anchor": ANCHOR,
                "context": CONTEXT,
                "variant_coverage": variant_coverage,
                "condition_coverage": {
                    "kind": "sampled_conditions",
                    "condition_id_algorithm": (
                        "onset-isolated-sampled-condition-v1"
                    ),
                    "unique_condition_count": len(sampled_condition_ids),
                    "condition_ids": sampled_condition_ids,
                },
                "signal_stage": SIGNAL_STAGE,
                "pre_roll_frames": round(pre_roll_seconds * sample_rate),
                "sample_rate_hz": sample_rate,
                "algorithm_sha256": algorithm_sha256,
                "window_ms": WINDOW_MS,
                "hop_ms": HOP_MS,
                "threshold_policy": THRESHOLD_POLICY,
            },
            "observations": observations,
        }
        report["candidate_sha256"] = canonical_sha256(
            report, omit="candidate_sha256"
        )

        # The public report refers to the not-yet-published final paths.  Make
        # an otherwise identical validation copy bound to staging artifacts so
        # the strict evidence validator can inspect the exact PCM24/performance
        # bytes before the atomic directory rename.
        validation_report = copy.deepcopy(report)
        for observation, (performance_path, wav_path) in zip(
            validation_report["observations"],
            staged_artifacts,
            strict=True,
        ):
            observation["performance_path"] = _relative(
                performance_path, project_root
            )
            observation["wav_path"] = _relative(wav_path, project_root)
        validation_report["candidate_sha256"] = canonical_sha256(
            validation_report,
            omit="candidate_sha256",
        )
        validate_candidate_report(
            validation_report,
            project_root=project_root,
            verify_current=True,
            verify_artifacts=True,
        )

        _write_json(staging / REPORT_FILENAME, report)
        # Close the render/validation/report-write TOCTOU window immediately
        # before publication.  This rehashes both the exact manifest bytes and
        # the complete runtime graph.
        _assert_runtime_snapshot(
            project_root=project_root,
            manifest_path=manifest_path,
            initial_manifest_bytes=initial_manifest_bytes,
            initial_runtime_fingerprint=runtime_fingerprint,
            sample_rate_hz=sample_rate,
        )
        _publish_new_directory(staging, output_directory)
        return report
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


__all__ = [
    "ALGORITHM_NAME",
    "DEFAULT_VELOCITIES",
    "HOP_MS",
    "ProbeSpec",
    "REPORT_FILENAME",
    "WINDOW_MS",
    "analyze_onset",
    "analyze_stereo_onset",
    "build_probe_specs",
    "canonical_sha256",
    "run_probe_batch",
    "select_probe_notes",
    "select_range_probe_notes",
    "sha256_file",
]
