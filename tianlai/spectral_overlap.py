"""Deterministic spectral-overlap triage for two rendered stereo stems.

Only windows active in both stems are measured.  Channel energy is calculated
independently, never from ``L + R``, so a valid antiphase stereo signal cannot
disappear.  The overlap coefficient compares normalized six-band energy
distributions with ``sum(min(first_band, second_band))``.

This is a transparent engineering diagnostic.  It is not a psychoacoustic
masking model, LUFS, or an implementation of ITU-R BS.1770.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Sequence

import numpy as np

from ._window_batches import (
    accumulated_stereo_power_spectrum,
    selected_window_peak,
    window_rms,
)
from .mix_analysis import FREQUENCY_BANDS_HZ, MixAnalysisConfig


SPECTRAL_OVERLAP_FORMAT = "tianlai.spectral_overlap"
SPECTRAL_OVERLAP_VERSION = 1
_AUDIO_CHUNK_FRAMES = 262_144


def _serialized_float(
    value: float | None,
    digits: int,
) -> float | None:
    if value is None:
        return None
    rounded = round(float(value), digits)
    return 0.0 if rounded == 0.0 else rounded


@dataclass(frozen=True, slots=True)
class SpectralOverlapAnalysis:
    """Stable, JSON-compatible measurements for one pair of stereo stems."""

    sample_rate_hz: int
    frame_count: int
    config: MixAnalysisConfig
    shared_active_window_count: int
    window_count: int
    overlap_ratio: float
    first_band_energy_ratios: tuple[tuple[str, float | None], ...]
    second_band_energy_ratios: tuple[tuple[str, float | None], ...]
    band_first_minus_second_db: tuple[tuple[str, float | None], ...]
    spectral_overlap_coefficient: float | None

    def to_dict(self) -> dict[str, Any]:
        """Return fields in a fixed order with finite numbers or ``None``."""

        return {
            "format": SPECTRAL_OVERLAP_FORMAT,
            "version": SPECTRAL_OVERLAP_VERSION,
            "metric": "shared_active_six_band_energy_overlap",
            "sample_rate_hz": self.sample_rate_hz,
            "frame_count": self.frame_count,
            "gate": self.config.to_dict(),
            "shared_active_window_count": self.shared_active_window_count,
            "window_count": self.window_count,
            "overlap_ratio": _serialized_float(self.overlap_ratio, 9),
            "first_band_energy_ratios": {
                name: _serialized_float(value, 9)
                for name, value in self.first_band_energy_ratios
            },
            "second_band_energy_ratios": {
                name: _serialized_float(value, 9)
                for name, value in self.second_band_energy_ratios
            },
            "band_first_minus_second_db": {
                name: _serialized_float(value, 6)
                for name, value in self.band_first_minus_second_db
            },
            "spectral_overlap_coefficient": (
                _serialized_float(
                    self.spectral_overlap_coefficient,
                    9,
                )
            ),
            "audio_modified": False,
            "notice": (
                "该确定性指标只用于频带重叠排查；它不是心理声学 masking、"
                "LUFS 或 BS.1770 响度。"
            ),
        }


def _stereo_audio(
    frames: Sequence[Sequence[float]] | np.ndarray,
) -> np.ndarray:
    raw = np.asarray(frames)
    if np.iscomplexobj(raw):
        raise ValueError("frames must contain real samples")
    try:
        audio = (
            raw
            if raw.dtype.kind == "f"
            else np.asarray(frames, dtype=np.float64)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("frames must contain real numeric samples") from exc
    if audio.ndim != 2 or audio.shape[1] != 2:
        raise ValueError("frames must have shape (frame_count, 2)")
    if audio.shape[0] < 1:
        raise ValueError("frames must contain at least one stereo frame")
    for start in range(0, int(audio.shape[0]), _AUDIO_CHUNK_FRAMES):
        if not np.all(
            np.isfinite(audio[start : start + _AUDIO_CHUNK_FRAMES])
        ):
            raise ValueError("frames contain non-finite samples")
    return audio


def _active_mask(
    audio: np.ndarray,
    starts: tuple[int, ...],
    window_frames: int,
    config: MixAnalysisConfig,
) -> np.ndarray:
    levels = window_rms(audio, starts, window_frames)
    loudest = float(np.max(levels))
    if loudest <= 0.0:
        return np.zeros(len(starts), dtype=bool)
    absolute = 10.0 ** (config.absolute_gate_dbfs / 20.0)
    relative = loudest * 10.0 ** (config.relative_gate_db / 20.0)
    threshold = max(absolute, relative)
    return (levels >= threshold) & (levels > 0.0)


def _shared_band_energies(
    audio: np.ndarray,
    starts: tuple[int, ...],
    shared_indices: np.ndarray,
    window_frames: int,
    sample_rate: int,
) -> tuple[tuple[float, ...], float]:
    scale = selected_window_peak(
        audio,
        starts,
        shared_indices,
        window_frames,
    )
    if scale <= 0.0:
        return tuple(0.0 for _item in FREQUENCY_BANDS_HZ), 0.0

    accumulated = accumulated_stereo_power_spectrum(
        audio,
        starts,
        shared_indices,
        window_frames,
        scale,
    )
    accumulated /= float(shared_indices.size)
    accumulated[0] = 0.0
    if not np.all(np.isfinite(accumulated)):
        raise ValueError("spectral energy exceeds the finite analysis range")

    frequencies = np.fft.rfftfreq(window_frames, 1.0 / sample_rate)
    energies: list[float] = []
    for _name, low, high in FREQUENCY_BANDS_HZ:
        if high is None:
            mask = frequencies >= low
        else:
            mask = (frequencies >= low) & (frequencies < high)
        energies.append(float(np.sum(accumulated[mask])))
    if not all(math.isfinite(value) and value >= 0.0 for value in energies):
        raise ValueError("band energy exceeds the finite analysis range")
    return tuple(energies), scale


def _band_ratios(
    energies: tuple[float, ...],
) -> tuple[tuple[str, float | None], ...]:
    total = math.fsum(energies)
    if total <= 0.0:
        return tuple(
            (name, None)
            for name, _low, _high in FREQUENCY_BANDS_HZ
        )
    return tuple(
        (name, energy / total)
        for (name, _low, _high), energy in zip(
            FREQUENCY_BANDS_HZ,
            energies,
            strict=True,
        )
    )


def _band_level_differences(
    first_energies: tuple[float, ...],
    second_energies: tuple[float, ...],
    first_scale: float,
    second_scale: float,
) -> tuple[tuple[str, float | None], ...]:
    rows: list[tuple[str, float | None]] = []
    for (name, _low, _high), first, second in zip(
        FREQUENCY_BANDS_HZ,
        first_energies,
        second_energies,
        strict=True,
    ):
        if (
            first <= 0.0
            or second <= 0.0
            or first_scale <= 0.0
            or second_scale <= 0.0
        ):
            rows.append((name, None))
            continue
        difference = (
            10.0 * (math.log10(first) - math.log10(second))
            + 20.0
            * (math.log10(first_scale) - math.log10(second_scale))
        )
        if not math.isfinite(difference):
            raise ValueError("band level difference is not finite")
        rows.append((name, difference))
    return tuple(rows)


def _overlap_coefficient(
    first_ratios: tuple[tuple[str, float | None], ...],
    second_ratios: tuple[tuple[str, float | None], ...],
) -> float | None:
    first_values = tuple(value for _name, value in first_ratios)
    second_values = tuple(value for _name, value in second_ratios)
    if any(value is None for value in (*first_values, *second_values)):
        return None
    coefficient = math.fsum(
        min(float(first), float(second))
        for first, second in zip(
            first_values,
            second_values,
            strict=True,
        )
    )
    return max(0.0, min(1.0, coefficient))


def analyze_spectral_overlap(
    first_frames: Sequence[Sequence[float]] | np.ndarray,
    second_frames: Sequence[Sequence[float]] | np.ndarray,
    sample_rate: int,
    config: MixAnalysisConfig | None = None,
) -> SpectralOverlapAnalysis:
    """Measure six-band overlap only where both stems pass their own gates.

    Per-band ``first_minus_second_db`` values compare absolute band power in
    the shared windows.  The 0..1 coefficient instead compares normalized
    spectral shapes: 0 means disjoint six-band distributions and 1 means the
    same distribution, regardless of overall level.
    """

    if config is None:
        config = MixAnalysisConfig()
    elif not isinstance(config, MixAnalysisConfig):
        raise ValueError("config must be a MixAnalysisConfig")
    window_frames, hop_frames = config.frame_lengths(sample_rate)
    sample_rate = int(sample_rate)
    first = _stereo_audio(first_frames)
    second = _stereo_audio(second_frames)
    if first.shape[0] != second.shape[0]:
        raise ValueError("stems must have the same frame count")

    starts = tuple(range(0, first.shape[0], hop_frames))
    first_active = _active_mask(
        first,
        starts,
        window_frames,
        config,
    )
    second_active = _active_mask(
        second,
        starts,
        window_frames,
        config,
    )
    shared_indices = np.flatnonzero(first_active & second_active)
    shared_count = int(shared_indices.size)
    window_count = len(starts)
    null_bands = tuple(
        (name, None)
        for name, _low, _high in FREQUENCY_BANDS_HZ
    )
    if shared_count == 0:
        return SpectralOverlapAnalysis(
            sample_rate_hz=sample_rate,
            frame_count=int(first.shape[0]),
            config=config,
            shared_active_window_count=0,
            window_count=window_count,
            overlap_ratio=0.0,
            first_band_energy_ratios=null_bands,
            second_band_energy_ratios=null_bands,
            band_first_minus_second_db=null_bands,
            spectral_overlap_coefficient=None,
        )

    first_energies, first_scale = _shared_band_energies(
        first,
        starts,
        shared_indices,
        window_frames,
        sample_rate,
    )
    second_energies, second_scale = _shared_band_energies(
        second,
        starts,
        shared_indices,
        window_frames,
        sample_rate,
    )
    first_ratios = _band_ratios(first_energies)
    second_ratios = _band_ratios(second_energies)
    differences = _band_level_differences(
        first_energies,
        second_energies,
        first_scale,
        second_scale,
    )
    return SpectralOverlapAnalysis(
        sample_rate_hz=sample_rate,
        frame_count=int(first.shape[0]),
        config=config,
        shared_active_window_count=shared_count,
        window_count=window_count,
        overlap_ratio=shared_count / window_count,
        first_band_energy_ratios=first_ratios,
        second_band_energy_ratios=second_ratios,
        band_first_minus_second_db=differences,
        spectral_overlap_coefficient=_overlap_coefficient(
            first_ratios,
            second_ratios,
        ),
    )


__all__ = (
    "SPECTRAL_OVERLAP_FORMAT",
    "SPECTRAL_OVERLAP_VERSION",
    "SpectralOverlapAnalysis",
    "analyze_spectral_overlap",
)
