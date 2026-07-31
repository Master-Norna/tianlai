from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class PitchMeasurement:
    measured_hz: float
    expected_hz: float
    detune_cents: float


@dataclass(frozen=True, slots=True)
class WidePitchAssessment:
    """Result of an octave-aware pitch check.

    ``status`` deliberately distinguishes an out-of-tune pitched signal from
    audio that has no defensible monophonic fundamental.  Callers must not turn
    ``no_clear_pitch`` into a pitch failure: cymbals, noise and inharmonic
    percussion belong in that state rather than being assigned an arbitrary
    FFT peak.
    """

    status: Literal["clear_pitch", "no_clear_pitch"]
    expected_hz: float
    measured_hz: float | None
    detune_cents: float | None
    confidence: float
    periodicity: float
    harmonic_peak_coverage: float
    reason: str

    @property
    def clear_pitch(self) -> bool:
        return self.status == "clear_pitch"

    def within_tolerance(self, tolerance_cents: float) -> bool:
        """Return true only for a clear pitch inside the supplied tolerance."""

        if tolerance_cents < 0.0 or not math.isfinite(tolerance_cents):
            raise ValueError("tolerance_cents must be finite and non-negative")
        return (
            self.clear_pitch
            and self.detune_cents is not None
            and abs(self.detune_cents) <= tolerance_cents
        )

    @property
    def nearest_octave_error(self) -> int | None:
        """Signed octave displacement, but only when it is unambiguous."""

        if self.detune_cents is None:
            return None
        octaves = round(self.detune_cents / 1200.0)
        if octaves != 0 and abs(self.detune_cents - 1200.0 * octaves) <= 80.0:
            return octaves
        return 0


def analyze_signal_wide_pitch(
    audio: object,
    sample_rate: int,
    expected_hz: float,
    *,
    start_seconds: float = 0.08,
    maximum_frames: int = 32_768,
    search_cents: float = 1_800.0,
    harmonic_count: int = 12,
    cents_step: float = 0.5,
) -> WidePitchAssessment:
    """Measure monophonic audio without hiding octave mapping mistakes.

    This is the acceptance-gate estimator, not a replacement for the narrow
    raw-sample calibration functions below.  It searches a deliberately wide
    interval and combines normalized autocorrelation with the extent to which
    the strongest spectral peaks form one harmonic series.  That combination
    handles a weak or absent fundamental while rejecting broadband and
    inharmonic sounds instead of inventing a pitch for them.
    """

    if sample_rate < 8_000:
        raise ValueError("sample_rate must be at least 8000")
    if not math.isfinite(expected_hz) or expected_hz <= 0.0:
        raise ValueError("expected_hz must be finite and positive")
    if not math.isfinite(start_seconds) or start_seconds < 0.0:
        raise ValueError("start_seconds must be finite and non-negative")
    if maximum_frames < 4_096:
        raise ValueError("maximum_frames must be at least 4096")
    if not math.isfinite(search_cents) or search_cents < 1_200.0:
        raise ValueError("search_cents must be finite and at least 1200")
    if harmonic_count < 3:
        raise ValueError("harmonic_count must be at least 3")
    if not math.isfinite(cents_step) or not 0.1 <= cents_step <= 5.0:
        raise ValueError("cents_step must be between 0.1 and 5.0")

    import numpy as np

    samples = np.asarray(audio, dtype="float64")
    if samples.ndim == 2:
        if samples.shape[1] < 1:
            raise ValueError("audio has no channels")
        samples = np.mean(samples, axis=1)
    elif samples.ndim != 1:
        raise ValueError("audio must be a mono vector or a frames-by-channels array")
    if not np.all(np.isfinite(samples)):
        raise ValueError("audio contains non-finite samples")

    start = round(start_seconds * sample_rate)
    available = len(samples) - start
    frame_count = min(maximum_frames, available)
    if frame_count < 4_096:
        raise ValueError("audio sample is too short for reliable pitch analysis")
    segment = samples[start : start + frame_count].copy()
    segment -= float(np.mean(segment))
    rms = float(np.sqrt(np.mean(segment * segment)))
    if rms <= 1e-8:
        return WidePitchAssessment(
            "no_clear_pitch",
            expected_hz,
            None,
            None,
            0.0,
            0.0,
            0.0,
            "signal is silent or below the analysis floor",
        )
    segment /= rms

    nyquist = sample_rate * 0.5
    ratio = 2.0 ** (search_cents / 1200.0)
    lower_hz = max(20.0, expected_hz / ratio)
    upper_hz = min(expected_hz * ratio, nyquist * 0.94)
    if lower_hz >= upper_hz:
        raise ValueError("wide pitch search interval is outside the audio bandwidth")

    grid_count = round(2.0 * search_cents / cents_step) + 1
    cents = np.linspace(-search_cents, search_cents, grid_count)
    candidates = expected_hz * np.power(2.0, cents / 1200.0)
    usable = (candidates >= lower_hz) & (candidates <= upper_hz)
    cents = cents[usable]
    candidates = candidates[usable]
    if len(candidates) < 3:
        raise ValueError("wide pitch search interval has too few candidates")

    # Normalized autocorrelation supplies the time-domain evidence.  Per-lag
    # normalization prevents longer periods winning merely because fewer
    # samples overlap.
    fft_size = 1 << (2 * frame_count - 1).bit_length()
    transformed = np.fft.rfft(segment, fft_size)
    correlation = np.fft.irfft(transformed * np.conjugate(transformed), fft_size)
    squared_prefix = np.concatenate(([0.0], np.cumsum(segment * segment)))
    minimum_lag = max(1, int(math.floor(sample_rate / upper_hz)) - 1)
    maximum_lag = min(
        frame_count - 2, int(math.ceil(sample_rate / lower_hz)) + 1
    )
    lags = np.arange(minimum_lag, maximum_lag + 1)
    left_energy = squared_prefix[frame_count - lags]
    right_energy = squared_prefix[frame_count] - squared_prefix[lags]
    denominator = np.sqrt(np.maximum(left_energy * right_energy, 1e-30))
    normalized_correlation = correlation[lags] / denominator
    candidate_lags = sample_rate / candidates
    periodicities = np.interp(
        candidate_lags,
        lags.astype("float64"),
        normalized_correlation,
        left=-1.0,
        right=-1.0,
    )
    positive_periodicities = np.maximum(periodicities, 0.0)

    # A Hann FFT supplies frequency-domain evidence.  Using a set of prominent
    # peaks rather than only the largest bin is what resolves a dominant second
    # harmonic and most missing-fundamental cases.
    windowed = segment * np.hanning(frame_count)
    spectrum = np.abs(np.fft.rfft(windowed))
    frequencies = np.fft.rfftfreq(frame_count, 1.0 / sample_rate)
    powers = spectrum * spectrum
    peak_mask = np.zeros_like(spectrum, dtype=bool)
    peak_mask[1:-1] = (
        (spectrum[1:-1] > spectrum[:-2])
        & (spectrum[1:-1] >= spectrum[2:])
        & (frequencies[1:-1] >= lower_hz * 0.75)
    )
    peak_indices = np.flatnonzero(peak_mask)
    if len(peak_indices) == 0:
        return WidePitchAssessment(
            "no_clear_pitch",
            expected_hz,
            None,
            None,
            0.0,
            float(np.max(positive_periodicities)),
            0.0,
            "signal has no usable spectral peaks",
        )

    # Retain enough peaks to represent a complex musical tone, but do not let
    # thousands of tiny noise maxima overwhelm its partials.
    strongest = peak_indices[np.argsort(powers[peak_indices])[-64:]]
    peak_floor = float(np.max(powers[strongest])) * 1e-6
    strongest = strongest[powers[strongest] >= peak_floor]
    peak_frequencies = frequencies[strongest]
    # Use magnitude rather than power for harmonic-voting weights.  A bowed or
    # plucked string can put most energy in its second partial while retaining
    # a quieter fundamental and odd partials that establish the true period.
    # Power weights square that imbalance and can erase the octave-resolving
    # evidence even when those peaks are 10--20 dB below the dominant one.
    peak_weights = spectrum[strongest]
    peak_weights /= float(np.sum(peak_weights))

    frequency_ratios = peak_frequencies[None, :] / candidates[:, None]
    harmonic_orders = np.rint(frequency_ratios)
    valid_harmonics = (
        (harmonic_orders >= 1.0)
        & (harmonic_orders <= float(harmonic_count))
    )
    safe_orders = np.maximum(harmonic_orders, 1.0)
    cents_from_harmonic = 1200.0 * np.log2(
        np.maximum(frequency_ratios / safe_orders, 1e-12)
    )
    # Low strings have stretched upper partials: a quiet fundamental and odd
    # partials can sit roughly 20--35 cents away from an ideal harmonic grid.
    # A 24-cent width retains that octave-resolving evidence.  The independent
    # autocorrelation and coverage thresholds below still keep broadband and
    # genuinely inharmonic percussion out of the pitched result.
    closeness = np.exp(-0.5 * np.square(cents_from_harmonic / 24.0))
    closeness *= valid_harmonics
    coverages = closeness @ peak_weights
    ordered_coverages = (
        closeness / np.sqrt(safe_orders)
    ) @ peak_weights
    fundamental_strength = np.interp(candidates, frequencies, spectrum)
    fundamental_strength /= max(float(np.max(fundamental_strength)), 1e-20)

    scores = (
        coverages * (0.62 + 0.38 * positive_periodicities)
        + 0.32 * ordered_coverages
        + 0.06 * fundamental_strength
    )
    best = int(np.argmax(scores))
    refined_cents = float(cents[best])
    if 0 < best < len(scores) - 1:
        left, center, right = scores[best - 1 : best + 2]
        curvature = left - 2.0 * center + right
        if curvature != 0.0:
            refined_cents += (
                float(0.5 * (left - right) / curvature)
                * float(cents[1] - cents[0])
            )
    measured_hz = expected_hz * (2.0 ** (refined_cents / 1200.0))
    periodicity = float(positive_periodicities[best])
    coverage = float(coverages[best])
    confidence = max(0.0, min(1.0, 0.48 * periodicity + 0.52 * coverage))

    # Both domains must contribute.  The alternative high-coverage branch is
    # for naturally decaying samples whose envelopes reduce autocorrelation.
    clear = (
        coverage >= 0.60
        and periodicity >= 0.30
        or coverage >= 0.76
        and periodicity >= 0.15
    )
    if not clear:
        return WidePitchAssessment(
            "no_clear_pitch",
            expected_hz,
            None,
            None,
            confidence,
            periodicity,
            coverage,
            "periodicity and harmonic-series evidence are insufficient",
        )
    return WidePitchAssessment(
        "clear_pitch",
        expected_hz,
        measured_hz,
        refined_cents,
        confidence,
        periodicity,
        coverage,
        "clear periodic harmonic series",
    )


def analyze_file_wide_pitch(
    path: str | Path,
    expected_hz: float,
    **kwargs: object,
) -> WidePitchAssessment:
    """Read an audio file and run :func:`analyze_signal_wide_pitch`."""

    import soundfile as sf

    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    return analyze_signal_wide_pitch(
        audio,
        sample_rate,
        expected_hz,
        **kwargs,
    )


def analyze_instrument_pitch(
    manifest_path: str | Path,
    midi_note: float,
    *,
    sample_rate: int = 24_000,
    duration_seconds: float = 0.72,
    velocity: float = 0.72,
    articulation: str | None = None,
    **analysis_kwargs: object,
) -> WidePitchAssessment:
    """Render one real instrument note and apply the wide pitch gate.

    The helper intentionally enters through the public manifest/factory/event
    path.  It therefore catches sample-root and transposition errors that a raw
    source-file calibration cannot see.
    """

    if not math.isfinite(midi_note):
        raise ValueError("midi_note must be finite")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0.0:
        raise ValueError("duration_seconds must be finite and positive")
    if not math.isfinite(velocity) or not 0.0 <= velocity <= 1.0:
        raise ValueError("velocity must be between 0 and 1")
    if articulation is not None and not articulation.strip():
        raise ValueError("articulation must not be empty")

    import numpy as np

    from .events import PerformanceEvent
    from .instrument import create_instrument
    from .renderer import load_json_object
    from .tuning import EqualTemperament

    path = Path(manifest_path).resolve()
    manifest = load_json_object(path)
    if str(manifest.get("pitch_mode", "pitched")).lower() != "pitched":
        raise ValueError("wide pitch gate only accepts pitch_mode='pitched'")
    if "note_min" in manifest and midi_note < float(manifest["note_min"]):
        raise ValueError("midi_note is outside manifest note_min")
    if "note_max" in manifest and midi_note > float(manifest["note_max"]):
        raise ValueError("midi_note is outside manifest note_max")

    tuning = EqualTemperament(440.0)
    instrument = create_instrument(
        manifest,
        sample_rate,
        base_directory=str(path.parent),
    )
    try:
        note_sequence = 0
        if articulation is not None:
            instrument.handle_event(
                PerformanceEvent(
                    sample=0,
                    sequence=0,
                    type="articulation",
                    payload={"name": articulation},
                ),
                tuning,
            )
            note_sequence = 1
        instrument.handle_event(
            PerformanceEvent(
                sample=0,
                sequence=note_sequence,
                type="note_on",
                payload={
                    "note_id": 1,
                    "midi_note": float(midi_note),
                    "velocity": velocity,
                },
            ),
            tuning,
        )
        frame_count = max(4_096, round(duration_seconds * sample_rate))
        frames = np.empty((frame_count, 2), dtype="float64")
        for index in range(frame_count):
            frames[index] = instrument.render_frame()
        return analyze_signal_wide_pitch(
            frames,
            sample_rate,
            tuning.note_to_hz(midi_note),
            **analysis_kwargs,
        )
    finally:
        close = getattr(instrument, "close", None)
        if callable(close):
            close()


def analyze_file_pitch(
    path: str | Path,
    expected_hz: float,
    *,
    start_seconds: float = 0.15,
    maximum_frames: int = 131_072,
    search_cents: float = 180.0,
) -> PitchMeasurement:
    """Estimate a pitched sample near an expected frequency using a windowed FFT."""

    if not math.isfinite(expected_hz) or expected_hz <= 0.0:
        raise ValueError("expected_hz must be finite and positive")
    import numpy as np
    import soundfile as sf

    try:
        audio, sample_rate = sf.read(
            str(path),
            dtype="float32",
            always_2d=True,
        )
    except (sf.SoundFileError, OSError) as exc:
        raise ValueError(f"无法读取音频文件 {path}: {exc}") from exc
    mono = audio[:, 0]
    start = max(0, round(start_seconds * sample_rate))
    frame_count = min(maximum_frames, len(mono) - start)
    if frame_count < 4096:
        raise ValueError("audio sample is too short for reliable pitch analysis")
    segment = mono[start : start + frame_count].astype("float64", copy=False)
    segment = segment - np.mean(segment)
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(frame_count)))
    frequencies = np.fft.rfftfreq(frame_count, 1.0 / sample_rate)
    ratio = 2.0 ** (search_cents / 1200.0)
    mask = (frequencies >= expected_hz / ratio) & (frequencies <= expected_hz * ratio)
    candidates = np.flatnonzero(mask)
    if len(candidates) == 0:
        raise ValueError("pitch search range contains no FFT bins")
    peak_index = int(candidates[np.argmax(spectrum[mask])])

    delta = 0.0
    if 0 < peak_index < len(spectrum) - 1:
        left, center, right = np.log(spectrum[peak_index - 1 : peak_index + 2] + 1e-20)
        denominator = left - 2.0 * center + right
        if denominator != 0.0:
            delta = float(0.5 * (left - right) / denominator)
    measured_hz = (peak_index + delta) * sample_rate / frame_count
    detune_cents = 1200.0 * math.log2(measured_hz / expected_hz)
    return PitchMeasurement(measured_hz, expected_hz, detune_cents)


def analyze_file_harmonic_pitch(
    path: str | Path,
    expected_hz: float,
    *,
    start_seconds: float = 0.1,
    maximum_frames: int = 131_072,
    search_cents: float = 180.0,
    harmonic_count: int = 10,
) -> PitchMeasurement:
    """Estimate a missing-fundamental sample by matching several harmonics.

    Bowed tremolo and low pizzicato recordings often carry more energy in the
    second or third harmonic than at the fundamental.  Selecting the largest
    FFT bin near the expected fundamental can therefore lock to bow/noise
    sidebands.  This constrained estimator searches only near the declared
    SFZ root and sums normalized energy at integer harmonics, retaining a
    deterministic sub-cent result without an octave ambiguity.
    """

    if expected_hz <= 0.0:
        raise ValueError("expected_hz must be positive")
    if harmonic_count < 1:
        raise ValueError("harmonic_count must be positive")
    import numpy as np
    import soundfile as sf

    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = audio[:, 0]
    start = max(0, round(start_seconds * sample_rate))
    frame_count = min(maximum_frames, len(mono) - start)
    if frame_count < 4096:
        raise ValueError("audio sample is too short for reliable pitch analysis")
    segment = mono[start : start + frame_count].astype("float64", copy=False)
    segment = segment - np.mean(segment)
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(frame_count)))
    frequencies = np.fft.rfftfreq(frame_count, 1.0 / sample_rate)

    # A 0.05-cent grid is finer than the uncertainty of these recordings.
    steps = max(1201, round(search_cents * 40.0) + 1)
    cents = np.linspace(-search_cents, search_cents, steps)
    candidates = expected_hz * np.power(2.0, cents / 1200.0)
    score = np.zeros_like(candidates)
    used = 0
    for harmonic in range(1, harmonic_count + 1):
        query = candidates * harmonic
        if query[-1] >= frequencies[-1]:
            break
        energy = np.interp(query, frequencies, spectrum)
        local_max = float(np.max(energy))
        if local_max <= 1e-20:
            continue
        # Per-harmonic normalization lets a real but weak fundamental vote
        # alongside a dominant upper partial; sqrt weighting still favours
        # lower harmonics and reduces inharmonic high-partial bias.
        score += (energy / local_max) / math.sqrt(harmonic)
        used += 1
    if used == 0:
        raise ValueError("pitch search range contains no usable harmonics")
    peak = int(np.argmax(score))
    refined_cents = float(cents[peak])
    if 0 < peak < len(score) - 1:
        left, center, right = score[peak - 1 : peak + 2]
        denominator = left - 2.0 * center + right
        if denominator != 0.0:
            delta = float(0.5 * (left - right) / denominator)
            refined_cents += delta * float(cents[1] - cents[0])
    measured_hz = expected_hz * (2.0 ** (refined_cents / 1200.0))
    return PitchMeasurement(measured_hz, expected_hz, refined_cents)
