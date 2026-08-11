"""旋律通鼓:VCSL 双通鼓采样按实测膜面基频重根音,映射为半音打击。

GM 旋律通鼓(Melodic Toms)是"按音阶排布的通鼓"。本实现诚实地使用
两只真实通鼓(VCSL Tom 1 高音 / Tom 2 低音)的鼓棒击采样:

1. ``校准音准.py`` 用 FFT 在 55-350 Hz 膜面基频带内测出两鼓每个采样的
   实际基频,把中位数写进 ``音准校准.json`` 作为该鼓的根音;
2. 加载时按根音把两鼓各自映射到相邻半音区间,谱面音高经重采样移调,
   力度 2 层 × 2 RR 保持上游区间;
3. 根音来自测量而非伪造,因此"每个键的音高准确"由构造保证,并在
   校准文件里留下测量证据。

manifest 契约(``type: "melodic_toms"``):``asset_root``、``drums``:
{名: {"samples": [{"sample", "lovel", "hivel", "seq"}], "span": [lo, hi]}}、
``pitch_calibration``(必须已生成)、常规 gain / velocity_exponent /
release_seconds / note_min / note_max。
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from .audio import audio_file_info
from .events import PerformanceEvent
from ._event_free_blocks import audited_event_free_blocks
from .instrument import Instrument, StereoFrame
from .sampler import SampleInstrument
from .tuning import EqualTemperament


_FUNDAMENTAL_LOW_HZ = 55.0
_FUNDAMENTAL_HIGH_HZ = 350.0


def measure_membrane_fundamental(path: str | Path) -> float:
    """FFT peak inside the tom fundamental band, over the early decay."""

    import numpy as np
    import soundfile as sf

    audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
    mono = audio[:, 0].astype("float64", copy=False)
    start = round(0.01 * sample_rate)
    segment = mono[start : start + min(len(mono) - start, 1 << 16)]
    if len(segment) < 4096:
        raise ValueError(f"sample too short for fundamental analysis: {path}")
    segment = segment - np.mean(segment)
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    frequencies = np.fft.rfftfreq(len(segment), 1.0 / sample_rate)
    mask = (frequencies >= _FUNDAMENTAL_LOW_HZ) & (frequencies <= _FUNDAMENTAL_HIGH_HZ)
    bins = np.flatnonzero(mask)
    peak = int(bins[np.argmax(spectrum[mask])])
    delta = 0.0
    if 0 < peak < len(spectrum) - 1:
        left, center, right = np.log(spectrum[peak - 1 : peak + 2] + 1e-20)
        denominator = left - 2.0 * center + right
        if denominator != 0.0:
            delta = float(0.5 * (left - right) / denominator)
    return (peak + delta) * sample_rate / len(segment)


def _load_calibration(manifest: dict[str, Any], manifest_dir: Path) -> dict[str, Any]:
    calibration_path = manifest_dir / str(manifest.get("pitch_calibration", "音准校准.json"))
    if not calibration_path.is_file():
        raise ValueError(
            f"melodic_toms requires a generated pitch calibration: {calibration_path}"
        )
    return json.loads(calibration_path.read_text(encoding="utf-8"))


@audited_event_free_blocks(silence_safe=False)
class MelodicTomsInstrument(Instrument):
    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        base = Path(base_directory).resolve()
        asset_root = (base / str(manifest["asset_root"])).resolve()
        if not asset_root.is_dir():
            raise ValueError(f"melodic_toms asset_root does not exist: {asset_root}")
        calibration = _load_calibration(manifest, base)
        drums = manifest.get("drums")
        if not isinstance(drums, dict) or not drums:
            raise ValueError("melodic_toms manifest requires drums")
        regions: list[dict[str, Any]] = []
        for name, spec in drums.items():
            drum_calibration = calibration.get("drums", {}).get(str(name))
            if drum_calibration is None:
                raise ValueError(f"pitch calibration is missing drum {name!r}")
            root_midi = float(drum_calibration["root_midi"])
            span = spec.get("span")
            if not isinstance(span, list) or len(span) != 2:
                raise ValueError(f"drum {name!r} requires span [lo, hi]")
            for item in spec["samples"]:
                path = (asset_root / str(item["sample"])).resolve()
                path.relative_to(asset_root)
                regions.append(
                    {
                        "sample": str(path),
                        "root_midi": root_midi,
                        "key_min": int(span[0]),
                        "key_max": int(span[1]),
                        "velocity_min": float(item.get("lovel", 0)) / 127.0,
                        "velocity_max": float(item.get("hivel", 127)) / 127.0,
                        "loop_mode": "one_shot",
                        "round_robin_length": int(item.get("seq_length", 1)),
                        "round_robin_position": int(item.get("seq", 1)),
                        "stable_key": f"melodic_toms:{name}:{item['sample']}",
                    }
                )
        self._engine = SampleInstrument.from_manifest(
            {
                "regions": regions,
                "gain": float(manifest.get("gain", 1.0)),
                "velocity_exponent": float(manifest.get("velocity_exponent", 0.72)),
                "release_seconds": float(manifest.get("release_seconds", 0.5)),
            },
            sample_rate,
            base_directory=str(asset_root),
        )
        self.note_min = int(manifest["note_min"])
        self.note_max = int(manifest["note_max"])

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            midi = event.payload.get("midi_note")
            if midi is not None and not self.note_min <= float(midi) <= self.note_max:
                raise ValueError(
                    f"melodic_toms note {float(midi):g} is outside declared range "
                    f"{self.note_min}..{self.note_max}"
                )
        self._engine.handle_event(event, tuning)

    def render_frame(self) -> StereoFrame:
        return self._engine.render_frame()

    @property
    def active_voice_count(self) -> int:
        return self._engine.active_voice_count


def create_melodic_toms(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> MelodicTomsInstrument:
    return MelodicTomsInstrument(sample_rate, manifest, base_directory)


def generate_melodic_toms_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Measure every hit sample's membrane fundamental and freeze drum roots."""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    drums_report: dict[str, Any] = {}
    for name, spec in manifest["drums"].items():
        measurements: dict[str, dict[str, float]] = {}
        midis: list[float] = []
        for item in spec["samples"]:
            path = (asset_root / str(item["sample"])).resolve()
            fundamental = measure_membrane_fundamental(path)
            midi = 69.0 + 12.0 * math.log2(fundamental / 440.0)
            midis.append(midi)
            measurements[str(item["sample"])] = {
                "fundamental_hz": round(fundamental, 6),
                "fundamental_midi": round(midi, 6),
            }
        root = statistics.median(midis)
        drums_report[str(name)] = {
            "root_midi": round(root, 6),
            "root_hz": round(440.0 * 2.0 ** ((root - 69.0) / 12.0), 6),
            "spread_cents": round(100.0 * (max(midis) - min(midis)), 6),
            "samples": measurements,
        }
    document = {
        "applicable": True,
        "method": (
            "FFT peak inside the 55-350 Hz membrane fundamental band over the early "
            "decay; drum root = median of its hit samples; playback transposes from "
            "these measured roots, so scored pitches are accurate by construction"
        ),
        "reference_a4_hz": 440.0,
        "drums": drums_report,
        "generated_at": _datetime.date.today().isoformat(),
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return document


def generate_melodic_toms_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    sample_lines: list[str] = []
    sample_bytes = 0
    formats: dict[str, int] = {}
    drum_summaries: dict[str, Any] = {}
    for name, spec in manifest["drums"].items():
        hashes: dict[str, str] = {}
        for item in spec["samples"]:
            path = (asset_root / str(item["sample"])).resolve()
            relative = path.relative_to(asset_root).as_posix()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            hashes[relative] = digest
            sample_lines.append(f"{digest}  {relative}\n")
            sample_bytes += path.stat().st_size
            sample_rate, _, channels = audio_file_info(path)
            key = f"{path.suffix.lower()}:{sample_rate}Hz:{channels}ch"
            formats[key] = formats.get(key, 0) + 1
        drum_summaries[str(name)] = {
            "span": spec["span"],
            "sample_count": len(spec["samples"]),
            "sample_sha256": hashes,
        }
    evidence: dict[str, str] = {}
    for relative in manifest.get("evidence_files", []):
        path = asset_root / str(relative)
        if not path.is_file():
            raise ValueError(f"licence/evidence file is missing: {path}")
        evidence[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {
        "upstream": str(manifest["upstream"]),
        "origin": str(manifest["origin"]),
        "upstream_version": str(manifest["upstream_version"]),
        "license": str(manifest["license"]),
        "evidence_sha256": evidence,
        "sample_count": len(sample_lines),
        "sample_bytes": sample_bytes,
        "sample_formats": formats,
        "drums": drum_summaries,
        "sample_set_sha256": hashlib.sha256(
            "".join(sorted(sample_lines)).encode("utf-8")
        ).hexdigest(),
        "sample_set_hash_algorithm": (
            "sort unique asset-root-relative UTF-8 paths; concatenate lowercase "
            "'<sha256>  <path>\\n>'; SHA-256 the UTF-8 bytes"
        ),
        "generated_at": _datetime.date.today().isoformat(),
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
