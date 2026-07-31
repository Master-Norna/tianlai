"""Deterministic, offline construction of derived instrument samples.

The renderer never performs these operations at note-on time.  Instrument-local
recipes freeze source hashes, phase alignment, spectral shaping and steady-state
level targets; this module builds PCM files and SFZ maps under the ignored
``音源/派生`` tree.  Original third-party resources are read-only inputs.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from .resource_restore import (
    _mkdir_path,
    _path_exists,
    _path_is_plain_file,
    _replace_path,
    _resolve_path,
    _unlink_path,
    _windows_extended_path,
)


_RECIPE_SCHEMA_VERSION = 1
_ALGORITHM_VERSION = "tianlai-derived-samples-v1"


def sha256_file(path: str | Path) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    with open(_windows_extended_path(source), "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _workspace_root(recipe_path: Path) -> Path:
    current = _resolve_path(recipe_path).parent
    while current != current.parent:
        if _path_is_plain_file(current / "pyproject.toml"):
            return current
        current = current.parent
    raise ValueError(f"cannot locate workspace root above recipe: {recipe_path}")


def _safe_workspace_path(root: Path, relative: object, *, kind: str) -> Path:
    raw = str(relative)
    if not raw or Path(raw).is_absolute():
        raise ValueError(f"{kind} must be a non-empty workspace-relative path")
    candidate = _resolve_path(root / raw)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{kind} escapes the workspace: {raw}") from error
    return candidate


def _safe_output_path(root: Path, relative: object, *, kind: str) -> Path:
    raw = str(relative)
    if not raw or Path(raw).is_absolute():
        raise ValueError(f"{kind} must be a non-empty output-relative path")
    candidate = _resolve_path(root / raw)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{kind} escapes the derived output root: {raw}") from error
    return candidate


def _verify_sha256(path: Path, expected: object, *, kind: str) -> str:
    wanted = str(expected).lower()
    if len(wanted) != 64 or any(character not in "0123456789abcdef" for character in wanted):
        raise ValueError(f"{kind} has an invalid expected SHA-256")
    actual = sha256_file(path)
    if actual != wanted:
        raise ValueError(
            f"{kind} SHA-256 mismatch for {path}: expected {wanted}, got {actual}"
        )
    return actual


def _shift_channel(channel: np.ndarray, frames: int) -> np.ndarray:
    """Shift without changing length; positive values delay the channel."""

    if frames == 0:
        return channel.copy()
    shifted = np.zeros_like(channel)
    if frames > 0:
        if frames < len(channel):
            shifted[frames:] = channel[:-frames]
    elif -frames < len(channel):
        shifted[:frames] = channel[-frames:]
    return shifted


def _center_channels(audio: np.ndarray, job: dict[str, Any]) -> np.ndarray:
    mode = str(job.get("channel_mode", "preserve"))
    if mode == "preserve":
        return audio.copy()
    if mode == "mono":
        return np.mean(audio, axis=1, keepdims=True)
    if mode != "stereo_phase_aligned_dual_mono":
        raise ValueError(f"unsupported derived-sample channel_mode: {mode!r}")
    if audio.shape[1] != 2:
        raise ValueError(
            "stereo_phase_aligned_dual_mono requires exactly two source channels"
        )
    polarity = int(job.get("right_polarity", 1))
    if polarity not in (-1, 1):
        raise ValueError("right_polarity must be -1 or 1")
    shift = int(job.get("right_shift_frames", 0))
    if abs(shift) > 128:
        raise ValueError("right_shift_frames exceeds the audited +/-128-frame limit")
    right = _shift_channel(audio[:, 1], shift) * polarity
    mono = 0.5 * (audio[:, 0] + right)
    return np.column_stack((mono, mono))


def _spectral_shape(
    audio: np.ndarray,
    sample_rate: int,
    transitions: list[dict[str, Any]],
) -> np.ndarray:
    """Apply a smooth zero-phase magnitude curve with reflected edge padding."""

    if not transitions:
        return audio
    nyquist = sample_rate / 2.0
    padding = min(4096, max(256, len(audio) // 16))
    padded = np.pad(audio, ((padding, padding), (0, 0)), mode="reflect")
    fft_size = 1 << (len(padded) - 1).bit_length()
    frequencies = np.fft.rfftfreq(fft_size, 1.0 / sample_rate)
    response_db = np.zeros_like(frequencies)
    for transition in transitions:
        start = float(transition["start_hz"])
        end = float(transition["end_hz"])
        gain_db = float(transition["gain_db"])
        if not 20.0 <= start < end <= nyquist:
            raise ValueError(
                f"invalid spectral transition {start:g}..{end:g} Hz "
                f"for {sample_rate} Hz audio"
            )
        progress = np.clip((frequencies - start) / (end - start), 0.0, 1.0)
        smooth = 0.5 - 0.5 * np.cos(np.pi * progress)
        response_db += gain_db * smooth
    response = 10.0 ** (response_db / 20.0)
    shaped = np.empty_like(padded)
    for channel in range(padded.shape[1]):
        spectrum = np.fft.rfft(padded[:, channel], n=fft_size)
        filtered = np.fft.irfft(spectrum * response, n=fft_size)
        shaped[:, channel] = filtered[: len(padded)]
    return shaped[padding:-padding]


def _steady_slice(
    audio: np.ndarray,
    sample_rate: int,
    window: dict[str, Any],
) -> np.ndarray:
    offset = int(window.get("offset_frames", 0))
    start = offset + int(round(float(window["start_seconds"]) * sample_rate))
    frames = int(round(float(window["duration_seconds"]) * sample_rate))
    end = start + frames
    if offset < 0 or start < 0 or frames <= 0 or end > len(audio):
        raise ValueError(
            f"steady window {start}:{end} falls outside {len(audio)} frames"
        )
    return audio[start:end]


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def _process_audio_job(
    source: Path,
    destination: Path,
    job: dict[str, Any],
) -> dict[str, Any]:
    decoded, sample_rate = sf.read(
        _windows_extended_path(source),
        dtype="float64",
        always_2d=True,
    )
    expected_rate = int(job["sample_rate"])
    if sample_rate != expected_rate:
        raise ValueError(
            f"source sample rate mismatch for {source}: "
            f"expected {expected_rate}, got {sample_rate}"
        )
    expected_channels = int(job["source_channels"])
    if decoded.shape[1] != expected_channels:
        raise ValueError(
            f"source channel mismatch for {source}: "
            f"expected {expected_channels}, got {decoded.shape[1]}"
        )
    if not np.all(np.isfinite(decoded)):
        raise ValueError(f"source contains non-finite samples: {source}")

    processed = _center_channels(decoded, job)
    processed -= np.mean(processed, axis=0, keepdims=True)
    spectral = list(job.get("spectral_transitions", []))
    processed = _spectral_shape(processed, sample_rate, spectral)
    processed -= np.mean(processed, axis=0, keepdims=True)

    window = dict(job["steady_window"])
    before_rms = _rms(_steady_slice(processed, sample_rate, window))
    if before_rms <= 1.0e-9:
        raise ValueError(f"steady window is effectively silent: {source}")
    target_dbfs = float(job["target_rms_dbfs"])
    target_rms = 10.0 ** (target_dbfs / 20.0)
    gain = target_rms / before_rms
    processed *= gain
    after_rms = _rms(_steady_slice(processed, sample_rate, window))
    peak = float(np.max(np.abs(processed))) if processed.size else 0.0
    peak_limit_dbfs = float(job.get("peak_limit_dbfs", -0.05))
    peak_limit = 10.0 ** (peak_limit_dbfs / 20.0)
    if peak > peak_limit:
        raise ValueError(
            f"derived sample would exceed its peak limit for {source}: "
            f"{20.0 * math.log10(peak):.3f} dBFS > {peak_limit_dbfs:.3f} dBFS"
        )

    _mkdir_path(destination.parent, parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    sf.write(
        _windows_extended_path(temporary),
        processed,
        sample_rate,
        format="WAV",
        subtype="PCM_24",
    )
    _replace_path(temporary, destination)
    return {
        "source_sha256": str(job["sha256"]).lower(),
        "output_sha256": sha256_file(destination),
        "sample_rate": sample_rate,
        "source_channels": int(decoded.shape[1]),
        "output_channels": int(processed.shape[1]),
        "frame_count": int(processed.shape[0]),
        "channel_mode": str(job.get("channel_mode", "preserve")),
        "right_shift_frames": int(job.get("right_shift_frames", 0)),
        "right_polarity": int(job.get("right_polarity", 1)),
        "steady_window": window,
        "steady_rms_dbfs": round(20.0 * math.log10(after_rms), 6),
        "applied_gain_db": round(20.0 * math.log10(gain), 6),
        "peak_dbfs": round(20.0 * math.log10(max(peak, 1.0e-30)), 6),
        "spectral_transitions": spectral,
        "format": "WAV:PCM_24",
    }


def _atomic_write_bytes(destination: Path, payload: bytes) -> None:
    _mkdir_path(destination.parent, parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    with open(_windows_extended_path(temporary), "wb") as stream:
        stream.write(payload)
    _replace_path(temporary, destination)


def _atomic_write_json(destination: Path, payload: dict[str, Any]) -> None:
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(destination, encoded)


def build_derived_resources(
    recipe_path: str | Path,
    *,
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build one frozen derived-resource recipe and return its receipt."""

    recipe_source = _resolve_path(recipe_path)
    with open(_windows_extended_path(recipe_source), encoding="utf-8") as stream:
        recipe = json.load(stream)
    if recipe.get("schema_version") != _RECIPE_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported derived-resource recipe schema: "
            f"{recipe.get('schema_version')!r}"
        )
    workspace = _workspace_root(recipe_source)
    canonical_output = _safe_workspace_path(
        workspace,
        recipe["canonical_output_root"],
        kind="canonical_output_root",
    )
    destination_root = (
        _resolve_path(output_root) if output_root is not None else canonical_output
    )
    for source_job in recipe.get("verified_inputs", []):
        source = _safe_workspace_path(
            workspace,
            source_job["path"],
            kind="verified input",
        )
        if not _path_is_plain_file(source):
            raise ValueError(f"verified input is missing: {source}")
        _verify_sha256(
            source,
            source_job["sha256"],
            kind="verified input",
        )

    _mkdir_path(destination_root, parents=True, exist_ok=True)
    receipt_path = _safe_output_path(
        destination_root,
        recipe.get("receipt", "处理说明.json"),
        kind="receipt",
    )
    if _path_exists(receipt_path):
        _unlink_path(receipt_path)

    audio_records: dict[str, dict[str, Any]] = {}
    for job in recipe.get("audio_jobs", []):
        source = _safe_workspace_path(workspace, job["source"], kind="audio source")
        if not _path_is_plain_file(source):
            raise ValueError(f"audio source is missing: {source}")
        _verify_sha256(source, job["sha256"], kind="audio source")
        destination = _safe_output_path(
            destination_root,
            job["output"],
            kind="audio output",
        )
        if destination == source:
            raise ValueError("derived audio output must not overwrite its source")
        record = _process_audio_job(source, destination, job)
        record["source"] = str(job["source"]).replace("\\", "/")
        record["output"] = str(job["output"]).replace("\\", "/")
        audio_records[record["output"]] = record

    text_records: dict[str, dict[str, str]] = {}
    for item in recipe.get("text_outputs", []):
        template = _resolve_path(recipe_source.parent / str(item["template"]))
        try:
            template.relative_to(recipe_source.parent)
        except ValueError as error:
            raise ValueError(
                f"text template escapes recipe directory: {item['template']}"
            ) from error
        if not _path_is_plain_file(template):
            raise ValueError(f"text template is missing: {template}")
        destination = _safe_output_path(
            destination_root,
            item["output"],
            kind="text output",
        )
        with open(_windows_extended_path(template), "rb") as stream:
            payload = stream.read()
        _atomic_write_bytes(destination, payload)
        label = str(item["output"]).replace("\\", "/")
        text_records[label] = {
            "template": str(item["template"]).replace("\\", "/"),
            "template_sha256": hashlib.sha256(payload).hexdigest(),
            "output_sha256": sha256_file(destination),
        }

    verified_inputs = {
        str(item["path"]).replace("\\", "/"): str(item["sha256"]).lower()
        for item in recipe.get("verified_inputs", [])
    }
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "algorithm_version": _ALGORITHM_VERSION,
        "recipe_id": str(recipe["recipe_id"]),
        "recipe_sha256": sha256_file(recipe_source),
        "canonical_output_root": str(recipe["canonical_output_root"]).replace(
            "\\", "/"
        ),
        "original_resources_modified": False,
        "runtime_processing": False,
        "license_inheritance": recipe["license_inheritance"],
        "verified_inputs": verified_inputs,
        "audio_outputs": audio_records,
        "text_outputs": text_records,
    }
    _atomic_write_json(receipt_path, receipt)
    return receipt


__all__ = [
    "build_derived_resources",
    "sha256_file",
]
