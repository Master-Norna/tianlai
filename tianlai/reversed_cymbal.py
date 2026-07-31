"""反向镲:审计过的真实镲片采样确定性倒放。

反向镲(reverse cymbal)本质就是"镲片录音倒放"。本模块把 VCSL 悬吊镲
的指定击/滚奏采样在内存中逐样本倒序,note_on 后以文件原速(经采样率
换算)播放完整上升沿,结束处按乐器特征骤停,只留极短防爆音淡出。
没有随机源:同一事件序列必得同一输出;不含通用 SoundFont 回退。

manifest 契约(``type: "reversed_cymbal"``):

- ``asset_root``:采样库根(相对 manifest);
- ``variants``:{MIDI 键: {"sample": 相对路径, "gain_db": 可选}} —— 每个
  键一个倒放变体,键仅做变体选择,不改变播放速度;
- ``note_min`` / ``note_max``、``gain``、``velocity_exponent``、
  ``stop_fade_seconds``(骤停淡出,默认 0.012 s)。
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .audio import audio_file_info, read_audio_float
from .events import PerformanceEvent
from .instrument import Instrument, StereoFrame
from .tuning import EqualTemperament


class _Voice:
    __slots__ = ("frames", "position", "step", "amplitude", "fade_step", "fade")

    def __init__(self, frames: Any, step: float, amplitude: float, fade_samples: int) -> None:
        self.frames = frames
        self.position = 0.0
        self.step = step
        self.amplitude = amplitude
        self.fade_step = 1.0 / max(1, fade_samples)
        self.fade = -1.0  # <0 表示尚未进入骤停淡出


class ReversedCymbalInstrument(Instrument):
    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        base = Path(base_directory).resolve()
        asset_root = (base / str(manifest["asset_root"])).resolve()
        if not asset_root.is_dir():
            raise ValueError(f"reversed_cymbal asset_root does not exist: {asset_root}")
        self.asset_root = asset_root
        raw_variants = manifest.get("variants")
        if not isinstance(raw_variants, dict) or not raw_variants:
            raise ValueError("reversed_cymbal manifest requires variants")
        self.variants: dict[int, dict[str, Any]] = {}
        for key, spec in raw_variants.items():
            midi = int(key)
            sample_path = (asset_root / str(spec["sample"])).resolve()
            sample_path.relative_to(asset_root)
            if not sample_path.is_file():
                raise ValueError(f"reversed_cymbal sample does not exist: {sample_path}")
            file_rate, frames = read_audio_float(sample_path)
            try:
                reversed_frames = frames[::-1].copy()
            except (TypeError, AttributeError):
                reversed_frames = tuple(reversed(frames))
            self.variants[midi] = {
                "path": sample_path,
                "frames": reversed_frames,
                "step": file_rate / float(sample_rate),
                "gain": 10.0 ** (float(spec.get("gain_db", 0.0)) / 20.0),
            }
        keys = sorted(self.variants)
        self.note_min = int(manifest.get("note_min", keys[0]))
        self.note_max = int(manifest.get("note_max", keys[-1]))
        self.gain = float(manifest.get("gain", 1.0))
        self.velocity_exponent = float(manifest.get("velocity_exponent", 0.72))
        self.stop_fade_samples = max(
            1, round(float(manifest.get("stop_fade_seconds", 0.012)) * sample_rate)
        )
        self._voices: dict[int, _Voice] = {}

    def _variant_for(self, midi: float) -> dict[str, Any]:
        keys = sorted(self.variants)
        best = min(keys, key=lambda key: (abs(key - midi), key))
        return self.variants[best]

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        del tuning
        if event.type == "note_on":
            note_id = int(event.payload["note_id"])
            if note_id in self._voices:
                raise ValueError(f"reversed_cymbal note_id {note_id} is already active")
            midi = float(event.payload.get("midi_note", self.note_min))
            if not self.note_min <= midi <= self.note_max:
                raise ValueError(
                    f"reversed_cymbal note {midi:g} is outside declared range "
                    f"{self.note_min}..{self.note_max}"
                )
            velocity = min(1.0, max(0.0, float(event.payload.get("velocity", 0.8))))
            variant = self._variant_for(midi)
            amplitude = self.gain * variant["gain"] * (velocity ** self.velocity_exponent)
            self._voices[note_id] = _Voice(
                variant["frames"], variant["step"], amplitude, self.stop_fade_samples
            )
        elif event.type == "note_off":
            note_id = int(event.payload["note_id"])
            voice = self._voices.get(note_id)
            if voice is not None and voice.fade < 0.0:
                voice.fade = 1.0
        elif event.type == "control":
            return

    def render_frame(self) -> StereoFrame:
        left = right = 0.0
        finished: list[int] = []
        for note_id, voice in self._voices.items():
            index = int(voice.position)
            if index + 1 >= len(voice.frames):
                finished.append(note_id)
                continue
            fraction = voice.position - index
            first = voice.frames[index]
            second = voice.frames[index + 1]
            sample_left = float(first[0]) + (float(second[0]) - float(first[0])) * fraction
            sample_right = float(first[1]) + (float(second[1]) - float(first[1])) * fraction
            amplitude = voice.amplitude
            if voice.fade >= 0.0:
                amplitude *= voice.fade
                voice.fade -= voice.fade_step
                if voice.fade <= 0.0:
                    finished.append(note_id)
            left += sample_left * amplitude
            right += sample_right * amplitude
            voice.position += voice.step
        for note_id in finished:
            self._voices.pop(note_id, None)
        return left, right

    @property
    def active_voice_count(self) -> int:
        return len(self._voices)


def create_reversed_cymbal(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> ReversedCymbalInstrument:
    return ReversedCymbalInstrument(sample_rate, manifest, base_directory)


def generate_reversed_cymbal_resource_verification(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Hash the reversed source samples plus licence evidence."""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    sample_lines: list[str] = []
    sample_bytes = 0
    formats: dict[str, int] = {}
    variant_report: dict[str, dict[str, Any]] = {}
    for key in sorted(manifest["variants"], key=int):
        spec = manifest["variants"][key]
        path = (asset_root / str(spec["sample"])).resolve()
        relative = path.relative_to(asset_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sample_lines.append(f"{digest}  {relative}\n")
        sample_bytes += path.stat().st_size
        sample_rate, frame_count, channels = audio_file_info(path)
        formats_key = f"{path.suffix.lower()}:{sample_rate}Hz:{channels}ch"
        formats[formats_key] = formats.get(formats_key, 0) + 1
        variant_report[key] = {
            "sample": relative,
            "sha256": digest,
            "swell_seconds": round(frame_count / sample_rate, 6),
            "transform": "deterministic full time reversal in memory",
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
        "transform": (
            "each source sample is reversed sample-exactly at load time; no other "
            "processing, no random source"
        ),
        "evidence_sha256": evidence,
        "sample_count": len(sample_lines),
        "sample_bytes": sample_bytes,
        "sample_formats": formats,
        "variants": variant_report,
        "sample_set_sha256": hashlib.sha256(
            "".join(sample_lines).encode("utf-8")
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


def generate_reversed_cymbal_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    reason = str(manifest.get("calibration_not_applicable_reason", "")).strip()
    if not reason:
        raise ValueError(
            f"reversed_cymbal must record calibration_not_applicable_reason: {source_manifest}"
        )
    document = {
        "applicable": False,
        "pitch_mode": "variant_select",
        "reason": reason,
        "samples": {},
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
