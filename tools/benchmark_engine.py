"""Opt-in portable engine benchmark; no external assets or CI timing gate.

Run from the source root: python -m tools.benchmark_engine
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import statistics
import tempfile
import time
import wave

import numpy as np

from tianlai.audio import write_wav_pcm24_blocks
from tianlai.events import parse_performance_document
from tianlai.post_render_check import analyze_rendered_wav
from tianlai.renderer import render_document_blocks
from tianlai.sampler import SampleInstrument
from tianlai.stem_cache import source_tree_digest


_RATE = 48000


def _measure(operation, repeats: int) -> dict:
    durations = []
    fingerprints = []
    for _ in range(repeats):
        start = time.perf_counter()
        fingerprint = operation()
        durations.append(time.perf_counter() - start)
        fingerprints.append(fingerprint)
    if len(set(fingerprints)) != 1:
        raise RuntimeError("benchmark produced different evidence between runs")
    return {
        "seconds": durations,
        "median_seconds": statistics.median(durations),
        "evidence_sha256": fingerprints[0],
    }


def run(seconds: int, repeats: int) -> dict:
    frames = seconds * _RATE
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        # Deterministic local mono PCM fixture. No licensed sample resources.
        phase = np.arange(frames, dtype=np.float64) * (2 * np.pi * 440 / _RATE)
        pcm = np.rint(np.sin(phase) * 10000).astype("<i2")
        with wave.open(str(directory / "sample.wav"), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(_RATE)
            stream.writeframes(pcm.tobytes())
        document = parse_performance_document({
            "sample_rate": _RATE,
            "duration_seconds": seconds,
            "tail_seconds": 0,
            "events": [
                {"time": 0, "type": "note_on", "note_id": 1,
                 "pitch_hz": 660, "velocity": 0.6},
                {"time": seconds - 0.1, "type": "note_off", "note_id": 1},
            ],
        })
        wav = directory / "render.wav"

        def render() -> str:
            instrument = SampleInstrument.from_manifest({
                "regions": [{"sample": "sample.wav", "root_pitch_hz": 440}],
                "resampling_quality": "bandlimited",
            }, _RATE, base_directory=str(directory))
            blocks, _peak = render_document_blocks(instrument, document)
            written = write_wav_pcm24_blocks(wav, blocks, _RATE)
            if written != frames:
                raise RuntimeError("unexpected rendered frame count")
            with wav.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()

        def analyze() -> str:
            report = analyze_rendered_wav(wav, "render.wav", _RATE, frames, True)
            # Canonicalize the returned report; the WAV has a stable label.
            return hashlib.sha256(json.dumps(
                report, sort_keys=True, ensure_ascii=False, allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")).hexdigest()

        # Warm up imports, kernel tables and the analysis path separately.
        render()
        analyze()
        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "numpy": np.__version__,
            "sample_rate": _RATE,
            "frames": frames,
            "repeats": repeats,
            "bandlimited_sample_render_and_pcm24": _measure(render, repeats),
            "post_render_check": _measure(analyze, repeats),
            "source_tree_digest": _measure(source_tree_digest, repeats),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=5)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 120 or not 1 <= args.repeats <= 10:
        parser.error("seconds must be 1–120 and repeats 1–10")
    print(json.dumps(run(args.seconds, args.repeats), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
