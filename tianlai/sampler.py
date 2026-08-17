from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import math
from pathlib import Path
from typing import Any
import zlib

import numpy as np

from .audio import audio_file_info, read_audio_float, wav_loop_points
from ._event_free_blocks import audited_event_free_blocks
from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .runtime_variants import (
    current_runtime_variant_capture,
    stable_variant_sha256,
)
from .tuning import EqualTemperament


_BANDLIMITED_PHASE_COUNT = 1024
_BANDLIMITED_FIRST_OFFSET = -7
_BANDLIMITED_TAP_COUNT = 16
_BANDLIMITED_CUTOFF_STEPS = 128
_MONO_PAN_POWER = math.sqrt(2.0)


def _bandlimited_cutoff_index(increment: float) -> int:
    """Return a conservative, cacheable Nyquist cutoff for one voice.

    ``increment`` is measured in source frames per output frame.  Values above
    one downsample the source, so their low-pass cutoff must contract before
    samples are skipped.  Rounding down never admits more bandwidth than the
    exact ratio would allow.
    """

    if increment > _BANDLIMITED_CUTOFF_STEPS:
        raise ValueError(
            "bandlimited sample playback increment exceeds the supported "
            f"maximum of {_BANDLIMITED_CUTOFF_STEPS}: {increment:.9g}"
        )
    cutoff = min(1.0, 1.0 / increment)
    return max(
        1,
        min(
            _BANDLIMITED_CUTOFF_STEPS,
            math.floor(cutoff * _BANDLIMITED_CUTOFF_STEPS),
        ),
    )


@lru_cache(maxsize=32)
def _bandlimited_kernel_table(cutoff_index: int) -> np.ndarray:
    """Build a deterministic 16-tap, 1024-phase Lanczos-sinc table."""

    cutoff = cutoff_index / _BANDLIMITED_CUTOFF_STEPS
    offsets = np.arange(
        _BANDLIMITED_FIRST_OFFSET,
        _BANDLIMITED_FIRST_OFFSET + _BANDLIMITED_TAP_COUNT,
        dtype=np.float64,
    )
    phases = (
        np.arange(_BANDLIMITED_PHASE_COUNT, dtype=np.float64)
        / _BANDLIMITED_PHASE_COUNT
    )
    distances = offsets[np.newaxis, :] - phases[:, np.newaxis]
    radius = _BANDLIMITED_TAP_COUNT / 2.0
    weights = cutoff * np.sinc(cutoff * distances) * np.sinc(distances / radius)
    weights /= np.sum(weights, axis=1, keepdims=True)
    weights.setflags(write=False)
    return weights


@dataclass(slots=True)
class _SampleData:
    path: Path
    sample_rate: int
    frame_count: int
    channels: int
    frames: Any | None = None

    def load(self) -> Any:
        if self.frames is None:
            decoded_rate, self.frames = read_audio_float(self.path)
            if decoded_rate != self.sample_rate:
                raise ValueError(f"sample rate changed while loading: {self.path}")
        return self.frames


@dataclass(frozen=True, slots=True)
class _Region:
    path: Path
    root_pitch_hz: float
    velocity_min: float
    velocity_max: float
    key_min: float | None
    key_max: float | None
    gain: float
    pan: float
    delay_seconds: float
    attack_seconds: float
    decay_seconds: float
    sustain_level: float
    release_seconds: float | None
    offset_frames: int
    sample_end: int | None
    loop_start: int | None
    loop_end: int | None
    loop_mode: str
    stereo_width: float
    stable_key: str
    native_playback_ratio: float
    pitch_random_cents: float
    amplitude_random_db: float
    delay_random_seconds: float
    round_robin_position: int | None
    round_robin_length: int | None
    random_min: float
    random_max: float
    sample: _SampleData


@dataclass(slots=True)
class _SampleVoice:
    region: _Region
    position: float
    increment: float
    amplitude: float
    delay_samples: int
    attack_samples: int
    attack_remaining: int
    decay_samples: int
    decay_remaining: int
    sustain_level: float
    release_samples: int
    envelope: float = 1.0
    released: bool = False
    pending_release: bool = False
    release_step: float = 0.0
    looped: bool = False
    resampler_table: Any | None = None
    resampler_cutoff_index: int | None = None
    resampler_validated_increment: float | None = None
    resampler_native_increment: bool = False
    resampler_validated_cutoff_index: int | None = None
    mono_pan_cosine: float = 1.0
    mono_pan_sine: float = 1.0


@audited_event_free_blocks(silence_safe=True)
class SampleInstrument(Instrument):
    """Deterministic velocity-layer sample instrument with sustain support."""

    def __init__(
        self,
        sample_rate: int,
        regions: tuple[_Region, ...],
        *,
        release_seconds: float,
        velocity_exponent: float,
        gain: float,
        attack_seconds: float,
        resampling_quality: str = "linear",
        runtime_component: str | None = None,
    ) -> None:
        super().__init__(sample_rate)
        if not regions:
            raise ValueError("sample instrument requires at least one region")
        self.regions = regions
        self.release_samples = max(1, round(release_seconds * sample_rate))
        self.velocity_exponent = velocity_exponent
        self.gain = gain
        self.attack_seconds = attack_seconds
        if resampling_quality not in {"linear", "bandlimited"}:
            raise ValueError(
                "sample instrument resampling_quality must be 'linear' or "
                "'bandlimited'"
            )
        self.resampling_quality = resampling_quality
        self.sustain_pedal = 0.0
        self.voices: dict[int, _SampleVoice] = {}
        self._round_robin_counters: dict[tuple[int, int], int] = {}
        self._runtime_component_hint = runtime_component
        self._runtime_variant_component_sha256: str | None = None
        self._runtime_variant_choice_records: dict[
            int, dict[str, Any]
        ] | None = None

    @classmethod
    def from_manifest(
        cls,
        data: dict[str, Any],
        sample_rate: int,
        *,
        base_directory: str,
        sample_cache: dict[Path, _SampleData] | None = None,
    ) -> "SampleInstrument":
        base = Path(base_directory)
        reference_a4 = float(data.get("reference_a4_hz", 440.0))
        manifest_tuning = EqualTemperament(reference_a4)
        cache = sample_cache if sample_cache is not None else {}
        regions: list[_Region] = []
        raw_regions = data.get("regions")
        if not isinstance(raw_regions, list):
            raise ValueError("sample instrument regions must be an array")

        for index, raw in enumerate(raw_regions):
            if not isinstance(raw, dict):
                raise ValueError(f"regions[{index}] must be an object")
            path = (base / str(raw["sample"])).resolve()
            if not path.is_file():
                raise ValueError(f"sample file does not exist: {path}")
            if path not in cache:
                source_rate, frame_count, channels = audio_file_info(path)
                cache[path] = _SampleData(
                    path,
                    source_rate,
                    frame_count,
                    channels,
                )
            if "root_pitch_hz" in raw:
                root_pitch_hz = float(raw["root_pitch_hz"])
            elif "root_midi" in raw:
                cents = float(raw.get("measured_tuning_cents", 0.0))
                root_pitch_hz = manifest_tuning.note_to_hz(float(raw["root_midi"])) * (
                    2.0 ** (cents / 1200.0)
                )
            else:
                raise ValueError(f"regions[{index}] requires root_pitch_hz or root_midi")
            velocity_min = float(raw.get("velocity_min", 0.0))
            velocity_max = float(raw.get("velocity_max", 1.0))
            if not 0.0 <= velocity_min <= velocity_max <= 1.0:
                raise ValueError(f"regions[{index}] has an invalid velocity range")
            regions.append(
                _Region(
                    path=path,
                    root_pitch_hz=root_pitch_hz,
                    velocity_min=velocity_min,
                    velocity_max=velocity_max,
                    key_min=(float(raw["key_min"]) if "key_min" in raw else None),
                    key_max=(float(raw["key_max"]) if "key_max" in raw else None),
                    gain=10.0 ** (float(raw.get("gain_db", 0.0)) / 20.0),
                    pan=min(1.0, max(-1.0, float(raw.get("pan", 0.0)))),
                    delay_seconds=float(raw.get("delay_seconds", 0.0)),
                    attack_seconds=float(raw.get("attack_seconds", data.get("attack_seconds", 0.0))),
                    decay_seconds=float(raw.get("decay_seconds", 0.0)),
                    sustain_level=float(raw.get("sustain_level", 1.0)),
                    release_seconds=(
                        float(raw["release_seconds"])
                        if "release_seconds" in raw
                        else None
                    ),
                    offset_frames=int(raw.get("offset_frames", 0)),
                    sample_end=(
                        int(raw["sample_end"]) if "sample_end" in raw else None
                    ),
                    loop_start=(int(raw["loop_start"]) if "loop_start" in raw else None),
                    loop_end=(int(raw["loop_end"]) if "loop_end" in raw else None),
                    loop_mode=str(raw.get("loop_mode", "loop_sustain")),
                    stereo_width=float(raw.get("stereo_width", 1.0)),
                    stable_key=str(raw.get("stable_key", raw["sample"])).replace(
                        "\\", "/"
                    ),
                    native_playback_ratio=float(raw.get("native_playback_ratio", 1.0)),
                    pitch_random_cents=max(
                        0.0, float(raw.get("pitch_random_cents", 0.0))
                    ),
                    amplitude_random_db=max(
                        0.0, float(raw.get("amplitude_random_db", 0.0))
                    ),
                    delay_random_seconds=max(
                        0.0, float(raw.get("delay_random_seconds", 0.0))
                    ),
                    round_robin_position=(
                        int(raw["round_robin_position"])
                        if "round_robin_position" in raw
                        else None
                    ),
                    round_robin_length=(
                        int(raw["round_robin_length"])
                        if "round_robin_length" in raw
                        else None
                    ),
                    random_min=float(raw.get("random_min", 0.0)),
                    random_max=float(raw.get("random_max", 1.0)),
                    sample=cache[path],
                )
            )
            region = regions[-1]
            if (region.key_min is None) != (region.key_max is None):
                raise ValueError(f"regions[{index}] must define both key_min and key_max")
            if region.key_min is not None and region.key_min > region.key_max:
                raise ValueError(f"regions[{index}] has an invalid key range")
            if not 0 <= region.offset_frames < region.sample.frame_count:
                raise ValueError(f"regions[{index}] has an invalid sample offset")
            if region.sample_end is not None and not (
                region.offset_frames < region.sample_end <= region.sample.frame_count
            ):
                raise ValueError(f"regions[{index}] has an invalid sample end")
            if region.delay_seconds < 0.0:
                raise ValueError(f"regions[{index}] has a negative delay")
            if region.attack_seconds < 0.0 or region.decay_seconds < 0.0:
                raise ValueError(f"regions[{index}] has a negative envelope time")
            if not 0.0 <= region.sustain_level <= 1.0:
                raise ValueError(f"regions[{index}] has an invalid sustain level")
            if (region.loop_start is None) != (region.loop_end is None):
                raise ValueError(f"regions[{index}] must define both loop_start and loop_end")
            if region.loop_start is None and bool(raw.get("use_embedded_loop", False)):
                embedded_loop = wav_loop_points(path)
                if embedded_loop is not None:
                    object.__setattr__(region, "loop_start", embedded_loop[0])
                    object.__setattr__(region, "loop_end", embedded_loop[1])
            if region.loop_start is not None and not (
                0
                <= region.loop_start
                < region.loop_end
                <= (
                    region.sample_end
                    if region.sample_end is not None
                    else region.sample.frame_count
                )
            ):
                raise ValueError(f"regions[{index}] has an invalid loop range")
            if region.loop_mode not in {
                "no_loop",
                "one_shot",
                "loop_continuous",
                "loop_sustain",
            }:
                raise ValueError(f"regions[{index}] has an invalid loop mode")
            if not 0.0 <= region.stereo_width <= 2.0:
                raise ValueError(f"regions[{index}] has an invalid stereo width")
            if (
                not math.isfinite(region.native_playback_ratio)
                or region.native_playback_ratio <= 0.0
            ):
                raise ValueError(
                    f"regions[{index}] has an invalid native playback ratio"
                )
            if (region.round_robin_position is None) != (
                region.round_robin_length is None
            ):
                raise ValueError(
                    f"regions[{index}] must define both round-robin position and length"
                )
            if region.round_robin_position is not None and not (
                1 <= region.round_robin_position <= region.round_robin_length
            ):
                raise ValueError(f"regions[{index}] has an invalid round-robin position")
            if not 0.0 <= region.random_min <= region.random_max <= 1.0:
                raise ValueError(f"regions[{index}] has an invalid random range")

        return cls(
            sample_rate,
            tuple(regions),
            release_seconds=float(data.get("release_seconds", 0.25)),
            velocity_exponent=float(data.get("velocity_exponent", 1.0)),
            gain=float(data.get("gain", 1.0)),
            attack_seconds=float(data.get("attack_seconds", 0.0)),
            resampling_quality=str(data.get("resampling_quality", "linear")),
            runtime_component=(
                str(data["runtime_component"])
                if "runtime_component" in data
                else None
            ),
        )

    @staticmethod
    def _region_score(
        region: _Region,
        *,
        pitch_hz: float,
        velocity: float,
        target_midi: float,
    ) -> tuple[float, float, float]:
        velocity_distance = (
            0.0
            if region.velocity_min <= velocity <= region.velocity_max
            else min(
                abs(velocity - region.velocity_min),
                abs(velocity - region.velocity_max),
            )
        )
        pitch_distance = abs(math.log2(pitch_hz / region.root_pitch_hz))
        if region.key_min is None or region.key_max is None:
            key_distance = pitch_distance * 12.0
        elif region.key_min <= target_midi <= region.key_max:
            key_distance = 0.0
            pitch_distance = 0.0
        else:
            key_distance = min(
                abs(target_midi - region.key_min),
                abs(target_midi - region.key_max),
            )
        return velocity_distance, key_distance, pitch_distance

    def _selection_candidates(
        self,
        pitch_hz: float,
        velocity: float,
        *,
        target_midi: float,
        random_value: float,
    ) -> list[_Region]:
        eligible = [
            region
            for region in self.regions
            if region.random_min <= random_value <= region.random_max
        ]
        if not eligible:
            return []
        scores = {
            id(region): self._region_score(
                region,
                pitch_hz=pitch_hz,
                velocity=velocity,
                target_midi=target_midi,
            )
            for region in eligible
        }
        best_score = min(scores.values())
        candidates = [
            region for region in eligible if scores[id(region)] == best_score
        ]
        # SFZ-backed candidates carry the source sequence position so an RR2
        # sample whose filename sorts before RR1 still starts on RR1. Plain
        # sample manifests retain the historical path-stable ordering.
        candidates.sort(
            key=lambda region: (
                region.round_robin_position
                if region.round_robin_position is not None
                else 0,
                str(region.path),
            )
        )
        return candidates

    @staticmethod
    def _portable_sample_identity(region: _Region) -> str:
        stable_key = region.stable_key.replace("\\", "/")
        looks_absolute = stable_key.startswith("/") or (
            len(stable_key) >= 3
            and stable_key[1] == ":"
            and stable_key[2] == "/"
        )
        # Absolute source paths are machine-local. Their basename plus the
        # region's catalog position/parameters (bound below) is portable and
        # the receipt publishes only a hash of this token.
        return region.path.name if looks_absolute else stable_key

    def _ensure_runtime_variant_identity(self) -> None:
        if self._runtime_variant_choice_records is not None:
            return

        records: dict[int, dict[str, Any]] = {}
        ordered_choice_hashes: list[str] = []
        for index, region in enumerate(self.regions):
            sample_identity_sha256 = stable_variant_sha256(
                "sample-source-identity-v1",
                {
                    "portable_key": self._portable_sample_identity(region),
                    "sample_rate": region.sample.sample_rate,
                    "frame_count": region.sample.frame_count,
                    "channels": region.sample.channels,
                },
            )
            choice_payload = {
                "catalog_position": index,
                "sample_identity_sha256": sample_identity_sha256,
                "selection": {
                    "root_pitch_hz": region.root_pitch_hz,
                    "velocity_min": region.velocity_min,
                    "velocity_max": region.velocity_max,
                    "key_min": region.key_min,
                    "key_max": region.key_max,
                    "round_robin_position": region.round_robin_position,
                    "round_robin_length": region.round_robin_length,
                    "random_min": region.random_min,
                    "random_max": region.random_max,
                },
                "render": {
                    "gain": region.gain,
                    "pan": region.pan,
                    "delay_seconds": region.delay_seconds,
                    "attack_seconds": region.attack_seconds,
                    "decay_seconds": region.decay_seconds,
                    "sustain_level": region.sustain_level,
                    "release_seconds": region.release_seconds,
                    "offset_frames": region.offset_frames,
                    "sample_end": region.sample_end,
                    "loop_start": region.loop_start,
                    "loop_end": region.loop_end,
                    "loop_mode": region.loop_mode,
                    "stereo_width": region.stereo_width,
                    "native_playback_ratio": region.native_playback_ratio,
                    "pitch_random_cents": region.pitch_random_cents,
                    "amplitude_random_db": region.amplitude_random_db,
                    "delay_random_seconds": region.delay_random_seconds,
                },
            }
            choice_sha256 = stable_variant_sha256(
                "sample-region-choice-v1",
                choice_payload,
            )
            record = {
                "choice_sha256": choice_sha256,
                "catalog_position": index,
                "random_min": region.random_min,
                "random_max": region.random_max,
                "round_robin_position": region.round_robin_position,
                "round_robin_length": region.round_robin_length,
                "jitter": {
                    "pitch_random_cents": region.pitch_random_cents,
                    "amplitude_random_db": region.amplitude_random_db,
                    "delay_random_seconds": region.delay_random_seconds,
                },
            }
            records[id(region)] = record
            ordered_choice_hashes.append(choice_sha256)

        component_hint_sha256 = (
            stable_variant_sha256(
                "sample-component-hint-v1",
                self._runtime_component_hint,
            )
            if self._runtime_component_hint is not None
            else None
        )
        self._runtime_variant_component_sha256 = stable_variant_sha256(
            "sample-instrument-component-v1",
            {
                "selection_algorithm": "sample-select-region-partition-v1",
                "choice_sha256s": ordered_choice_hashes,
                "component_hint_sha256": component_hint_sha256,
                "sample_rate": self.sample_rate,
                "release_samples": self.release_samples,
                "velocity_exponent": self.velocity_exponent,
                "gain": self.gain,
                "attack_seconds": self.attack_seconds,
                "resampling_quality": self.resampling_quality,
                "resampling_algorithm": (
                    "lanczos-sinc-16tap-1024phase-v1"
                    if self.resampling_quality == "bandlimited"
                    else "linear-v1"
                ),
            },
        )
        self._runtime_variant_choice_records = records

    def _runtime_variant_choice_sha256(self, region: _Region) -> str:
        self._ensure_runtime_variant_identity()
        assert self._runtime_variant_choice_records is not None
        return self._runtime_variant_choice_records[id(region)][
            "choice_sha256"
        ]

    def runtime_variant_contract(self) -> dict[str, Any]:
        """Declare the one selector component consumed by this exact backend."""

        self._ensure_runtime_variant_identity()
        assert self._runtime_variant_component_sha256 is not None
        return {
            "schema_version": 1,
            "kind": "top_level_runtime_variant_contract",
            "backend": "builtin_sample_instrument",
            "audio_selection_model": "sample_region_selector_v1",
            "capture_completeness": (
                "all_audio_selection_delegated_to_runtime_variant_capture"
            ),
            "expected_component_sha256s": [
                self._runtime_variant_component_sha256
            ],
            "expected_selection_count": 1,
        }

    def _runtime_variant_partition(
        self,
        *,
        kind: str,
        pitch_hz: float,
        velocity: float,
        target_midi: float,
        random_value: float,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> dict[str, Any]:
        candidates = self._selection_candidates(
            pitch_hz,
            velocity,
            target_midi=target_midi,
            random_value=random_value,
        )
        partition: dict[str, Any] = {
            "kind": kind,
            "probe_value": random_value,
            "status": "choices" if candidates else "gap",
            "choice_sha256s": [
                self._runtime_variant_choice_sha256(region)
                for region in candidates
            ],
        }
        if kind == "point":
            partition["value"] = random_value
        else:
            partition["minimum"] = minimum
            partition["maximum"] = maximum
        return partition

    def _runtime_variant_catalog(
        self,
        *,
        pitch_hz: float,
        velocity: float,
        target_midi: float,
    ) -> dict[str, Any]:
        self._ensure_runtime_variant_identity()
        assert self._runtime_variant_component_sha256 is not None
        assert self._runtime_variant_choice_records is not None

        boundaries = sorted(
            {
                0.0,
                1.0,
                *(
                    boundary
                    for region in self.regions
                    for boundary in (region.random_min, region.random_max)
                ),
            }
        )
        partitions: list[dict[str, Any]] = []
        for index, boundary in enumerate(boundaries):
            partitions.append(
                self._runtime_variant_partition(
                    kind="point",
                    pitch_hz=pitch_hz,
                    velocity=velocity,
                    target_midi=target_midi,
                    random_value=boundary,
                )
            )
            if index + 1 >= len(boundaries):
                continue
            following = boundaries[index + 1]
            if following <= boundary:
                continue
            probe = boundary + (following - boundary) / 2.0
            if not boundary < probe < following:
                probe = math.nextafter(boundary, following)
            if not boundary < probe < following:
                # No representable float exists inside this mathematical
                # interval, so there is no runtime selector value to cover.
                continue
            partitions.append(
                self._runtime_variant_partition(
                    kind="open_interval",
                    pitch_hz=pitch_hz,
                    velocity=velocity,
                    target_midi=target_midi,
                    random_value=probe,
                    minimum=boundary,
                    maximum=following,
                )
            )

        reachable = {
            choice_sha256
            for partition in partitions
            for choice_sha256 in partition["choice_sha256s"]
        }
        choices = sorted(
            (
                record
                for record in self._runtime_variant_choice_records.values()
                if record["choice_sha256"] in reachable
            ),
            key=lambda record: record["choice_sha256"],
        )
        unexhausted_domains: list[dict[str, Any]] = []
        region_by_choice = {
            record["choice_sha256"]: region
            for region in self.regions
            for record in (
                self._runtime_variant_choice_records[id(region)],
            )
        }
        for choice_sha256 in sorted(reachable):
            region = region_by_choice[choice_sha256]
            if region.pitch_random_cents > 0.0:
                unexhausted_domains.append(
                    {
                        "domain": "pitch_jitter_cents",
                        "choice_sha256": choice_sha256,
                        "minimum": -region.pitch_random_cents,
                        "maximum": region.pitch_random_cents,
                        "exhaustive": False,
                    }
                )
            if region.amplitude_random_db > 0.0:
                unexhausted_domains.append(
                    {
                        "domain": "amplitude_jitter_db",
                        "choice_sha256": choice_sha256,
                        "minimum": -region.amplitude_random_db,
                        "maximum": region.amplitude_random_db,
                        "exhaustive": False,
                    }
                )
            if region.delay_random_seconds > 0.0:
                unexhausted_domains.append(
                    {
                        "domain": "delay_jitter_seconds",
                        "choice_sha256": choice_sha256,
                        "minimum": 0.0,
                        "maximum": region.delay_random_seconds,
                        "exhaustive": False,
                    }
                )

        normalized_pitch_hz = 0.0 if pitch_hz == 0.0 else pitch_hz
        normalized_target_midi = (
            0.0 if target_midi == 0.0 else target_midi
        )
        normalized_velocity = 0.0 if velocity == 0.0 else velocity
        condition = {
            "pitch_hz": normalized_pitch_hz,
            "target_midi": normalized_target_midi,
            "velocity": normalized_velocity,
            "pitch_bucket": round(target_midi),
            "velocity_bucket": round(velocity * 127.0),
        }
        condition_sha256 = stable_variant_sha256(
            "sample-region-condition-v1",
            condition,
        )
        has_gaps = any(
            partition["status"] == "gap" for partition in partitions
        )
        deterministic_single = (
            not has_gaps
            and len(reachable) == 1
            and all(
                len(partition["choice_sha256s"]) == 1
                for partition in partitions
            )
            and not unexhausted_domains
        )
        return {
            "algorithm": "sample-select-region-partition-v1",
            "claim": "choice_directory_only_not_variant_certification",
            "component_sha256": self._runtime_variant_component_sha256,
            "condition": condition,
            "condition_sha256": condition_sha256,
            "selector_domain": {
                "name": "_sample_random_value",
                "minimum": 0.0,
                "maximum": 1.0,
                "bounds": "closed",
            },
            "partitions": partitions,
            "has_selector_gaps": has_gaps,
            "choices": choices,
            "unexhausted_domains": unexhausted_domains,
            "deterministic_single": deterministic_single,
        }

    def _select_region(
        self,
        pitch_hz: float,
        velocity: float,
        *,
        target_midi: float | None = None,
        random_value: float = 0.5,
    ) -> _Region:
        if target_midi is None:
            target_midi = 69.0 + 12.0 * math.log2(pitch_hz / 440.0)
        candidates = self._selection_candidates(
            pitch_hz,
            velocity,
            target_midi=target_midi,
            random_value=random_value,
        )
        if not candidates:
            raise ValueError(
                f"no sample region matches deterministic random value {random_value:.6f}"
            )
        pitch_bucket = round(target_midi)
        velocity_bucket = round(velocity * 127.0)
        key = (pitch_bucket, velocity_bucket)
        counter = self._round_robin_counters.get(key, 0)
        self._round_robin_counters[key] = counter + 1
        selected_index = counter % len(candidates)
        selected = candidates[selected_index]

        capture = current_runtime_variant_capture()
        if capture is not None:
            capture.record_selection(
                catalog=self._runtime_variant_catalog(
                    pitch_hz=pitch_hz,
                    velocity=velocity,
                    target_midi=target_midi,
                ),
                choice_sha256=self._runtime_variant_choice_sha256(selected),
                actual_selector={
                    "random_value": random_value,
                    "round_robin_counter_before": counter,
                    "candidate_count": len(candidates),
                    "candidate_index": selected_index,
                },
            )
        return selected

    def _begin_release(self, voice: _SampleVoice) -> None:
        if not voice.released:
            voice.released = True
            voice.pending_release = False
            voice.release_step = max(voice.envelope, 1.0 / voice.release_samples) / voice.release_samples

    def release_note(self, note_id: int, *, release_seconds: float | None = None) -> None:
        """Release one voice, optionally overriding its release time for legato crossfades."""

        voice = self.voices.get(note_id)
        if voice is None:
            return
        if release_seconds is not None:
            if release_seconds < 0.0:
                raise ValueError("release_seconds must not be negative")
            voice.release_samples = max(1, round(release_seconds * self.sample_rate))
        if voice.released:
            voice.release_step = max(
                voice.envelope,
                1.0 / voice.release_samples,
            ) / voice.release_samples
        else:
            self._begin_release(voice)

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "note_on":
            note_id = int(event.payload["note_id"])
            velocity = float(event.payload["velocity"])
            pitch_hz = event_pitch_hz(event, tuning)
            random_value = float(
                event.payload.get(
                    "_sample_random_value",
                    (
                        (
                            (note_id * 0x9E3779B1)
                            ^ (event.sequence * 0x85EBCA77)
                        )
                        & 0xFFFFFFFF
                    )
                    / 0xFFFFFFFF,
                )
            )
            if not 0.0 <= random_value <= 1.0:
                raise ValueError("sample random selector must be between 0 and 1")
            region = self._select_region(
                pitch_hz,
                velocity,
                target_midi=(
                    float(event.payload["midi_note"])
                    if "midi_note" in event.payload
                    else None
                ),
                random_value=random_value,
            )
            region.sample.load()
            # VPO asks for small per-hit variations.  Python's randomized
            # hash cannot be used here because renders must reproduce across
            # processes, so derive three stable pseudo-random values from the
            # event identity and normalized sample path.
            seed = (
                (note_id * 0x9E3779B1)
                ^ (event.sequence * 0x85EBCA77)
                ^ zlib.crc32(region.stable_key.encode("utf-8"))
            ) & 0xFFFFFFFF

            def stable_unit(salt: int) -> float:
                value = (seed ^ salt) & 0xFFFFFFFF
                value ^= value >> 16
                value = (value * 0x7FEB352D) & 0xFFFFFFFF
                value ^= value >> 15
                value = (value * 0x846CA68B) & 0xFFFFFFFF
                value ^= value >> 16
                return value / 0xFFFFFFFF

            pitch_jitter = (stable_unit(0x243F6A88) * 2.0 - 1.0) * (
                region.pitch_random_cents
            )
            amplitude_jitter = (stable_unit(0xB7E15162) * 2.0 - 1.0) * (
                region.amplitude_random_db
            )
            delay_jitter = stable_unit(0xDEADBEEF) * region.delay_random_seconds
            # Dedicated sample libraries sometimes use the played key only
            # to select a region (for example a mapped drum kit) while the
            # selected recording must retain its native pitch.  Keep this an
            # internal adapter flag rather than a public performance-event
            # field so ordinary sample manifests preserve their historical
            # pitched behaviour.
            ignore_input_pitch = bool(event.payload.get("_sample_ignore_pitch", False))
            attack_samples = max(0, round(region.attack_seconds * self.sample_rate))
            decay_samples = max(0, round(region.decay_seconds * self.sample_rate))
            if attack_samples > 0:
                initial_envelope = 0.0
            elif decay_samples > 0:
                initial_envelope = 1.0
            else:
                initial_envelope = region.sustain_level
            increment = (
                (
                    region.native_playback_ratio
                    if ignore_input_pitch
                    else pitch_hz / region.root_pitch_hz
                )
                * (2.0 ** (pitch_jitter / 1200.0))
                * (region.sample.sample_rate / self.sample_rate)
            )
            if not math.isfinite(increment) or increment <= 0.0:
                raise ValueError("sample playback increment must be finite and positive")
            resampler_table = None
            resampler_cutoff_index = None
            resampler_validated_increment = None
            resampler_native_increment = False
            resampler_validated_cutoff_index = None
            if self.resampling_quality == "bandlimited":
                resampler_validated_increment = increment
                resampler_native_increment = math.isclose(
                    increment,
                    1.0,
                    rel_tol=0.0,
                    abs_tol=1e-12,
                )
                if not resampler_native_increment:
                    resampler_cutoff_index = _bandlimited_cutoff_index(
                        increment
                    )
                    resampler_validated_cutoff_index = (
                        resampler_cutoff_index
                    )
                    resampler_table = _bandlimited_kernel_table(
                        resampler_cutoff_index
                    )
            self.voices[note_id] = _SampleVoice(
                region=region,
                position=float(region.offset_frames),
                increment=increment,
                amplitude=self.gain
                * region.gain
                * (10.0 ** (amplitude_jitter / 20.0))
                * (velocity**self.velocity_exponent),
                delay_samples=max(
                    0,
                    round(
                        (region.delay_seconds + delay_jitter) * self.sample_rate
                    ),
                ),
                attack_samples=attack_samples,
                attack_remaining=attack_samples,
                decay_samples=decay_samples,
                decay_remaining=decay_samples,
                sustain_level=region.sustain_level,
                release_samples=(
                    max(1, round(region.release_seconds * self.sample_rate))
                    if region.release_seconds is not None
                    else self.release_samples
                ),
                envelope=initial_envelope,
                resampler_table=resampler_table,
                resampler_cutoff_index=resampler_cutoff_index,
                resampler_validated_increment=(
                    resampler_validated_increment
                ),
                resampler_native_increment=resampler_native_increment,
                resampler_validated_cutoff_index=(
                    resampler_validated_cutoff_index
                ),
                mono_pan_cosine=math.cos(
                    (region.pan + 1.0) * math.pi / 4.0
                ),
                mono_pan_sine=math.sin(
                    (region.pan + 1.0) * math.pi / 4.0
                ),
            )
        elif event.type == "note_off":
            voice = self.voices.get(int(event.payload["note_id"]))
            if voice is not None:
                if voice.region.loop_mode == "one_shot":
                    return
                if self.sustain_pedal >= 0.5:
                    voice.pending_release = True
                else:
                    self._begin_release(voice)
        elif event.type == "control" and event.payload["name"] == "sustain_pedal":
            previous = self.sustain_pedal
            self.sustain_pedal = float(event.payload["value"])
            if previous >= 0.5 and self.sustain_pedal < 0.5:
                for voice in self.voices.values():
                    if voice.pending_release:
                        self._begin_release(voice)

    @staticmethod
    def _mapped_filter_index(
        voice: _SampleVoice,
        sample_index: int,
        *,
        playback_end: int,
        loop_active: bool,
        loop_start: int | None,
        loop_end: int | None,
    ) -> int:
        if loop_start is not None and loop_end is not None:
            if (loop_active and sample_index >= loop_end) or (
                voice.looped and sample_index < loop_start
            ):
                return loop_start + (
                    (sample_index - loop_start) % (loop_end - loop_start)
                )
        return min(
            playback_end - 1,
            max(voice.region.offset_frames, sample_index),
        )

    @classmethod
    def _bandlimited_frame(
        cls,
        voice: _SampleVoice,
        frames: Any,
        *,
        playback_end: int,
        loop_active: bool,
        loop_start: int | None,
        loop_end: int | None,
    ) -> tuple[float, float]:
        table = voice.resampler_table
        assert table is not None
        index = math.floor(voice.position)
        fraction = voice.position - index
        phase_index = min(
            _BANDLIMITED_PHASE_COUNT - 1,
            int(fraction * _BANDLIMITED_PHASE_COUNT),
        )
        weights = table[phase_index]
        first_index = index + _BANDLIMITED_FIRST_OFFSET
        stop_index = first_index + _BANDLIMITED_TAP_COUNT
        # Once a voice has crossed the loop boundary, taps immediately to the
        # left of ``loop_start`` belong to the loop tail that was actually
        # heard.  Keep that history when a sustain loop is released; otherwise
        # the first release frame would suddenly read pre-loop attack data.
        lower_contiguous = (
            loop_start
            if voice.looped and loop_start is not None
            else voice.region.offset_frames
        )
        upper_contiguous = (
            loop_end
            if loop_active and loop_end is not None
            else playback_end
        )
        if (
            isinstance(frames, np.ndarray)
            and first_index >= lower_contiguous
            and stop_index <= upper_contiguous
        ):
            block = frames[first_index:stop_index]
            return (
                float(np.dot(block[:, 0], weights)),
                float(np.dot(block[:, 1], weights)),
            )

        source_left = 0.0
        source_right = 0.0
        for tap, weight in enumerate(weights):
            mapped = cls._mapped_filter_index(
                voice,
                first_index + tap,
                playback_end=playback_end,
                loop_active=loop_active,
                loop_start=loop_start,
                loop_end=loop_end,
            )
            frame = frames[mapped]
            source_left += float(frame[0]) * float(weight)
            source_right += float(frame[1]) * float(weight)
        return source_left, source_right

    def _refresh_resampler_table(self, voice: _SampleVoice) -> None:
        """Track pitch modulation without using a stale anti-alias cutoff."""

        if not math.isfinite(voice.increment) or voice.increment <= 0.0:
            raise ValueError("sample playback increment must be finite and positive")
        if self.resampling_quality != "bandlimited":
            voice.resampler_cutoff_index = None
            voice.resampler_table = None
            return

        if voice.increment != voice.resampler_validated_increment:
            native_increment = math.isclose(
                voice.increment,
                1.0,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            validated_cutoff_index = (
                None
                if native_increment
                else _bandlimited_cutoff_index(voice.increment)
            )
            # Publish the cached derivation only after every operation above
            # succeeds.  An unsupported or otherwise invalid runtime change
            # therefore raises on every attempted frame exactly as before.
            voice.resampler_validated_increment = voice.increment
            voice.resampler_native_increment = native_increment
            voice.resampler_validated_cutoff_index = (
                validated_cutoff_index
            )

        native_rate_at_integer_position = (
            voice.resampler_native_increment
            and math.isclose(
                voice.position,
                round(voice.position),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )
        if native_rate_at_integer_position:
            voice.resampler_cutoff_index = None
            voice.resampler_table = None
            return

        cutoff_index = voice.resampler_validated_cutoff_index
        if cutoff_index is None:
            # A native-rate increment can move away from an integer position
            # after runtime position or pitch mutation.  The established path
            # then uses the full-band kernel until it becomes integer again.
            cutoff_index = _bandlimited_cutoff_index(voice.increment)
            voice.resampler_validated_cutoff_index = cutoff_index
        if cutoff_index != voice.resampler_cutoff_index:
            voice.resampler_cutoff_index = cutoff_index
            voice.resampler_table = _bandlimited_kernel_table(cutoff_index)

    def render_frame(self) -> StereoFrame:
        left = 0.0
        right = 0.0
        finished: list[int] = []
        for note_id, voice in self.voices.items():
            if voice.delay_samples > 0:
                if voice.released:
                    finished.append(note_id)
                    continue
                voice.delay_samples -= 1
                continue
            frames = voice.region.sample.frames
            assert frames is not None
            index = int(voice.position)
            playback_end = voice.region.sample_end or len(frames)
            if index >= playback_end:
                finished.append(note_id)
                continue
            loop_start = voice.region.loop_start
            loop_end = voice.region.loop_end
            loop_active = (
                loop_start is not None
                and loop_end is not None
                and voice.region.loop_mode in {"loop_sustain", "loop_continuous"}
                and (
                    not voice.released
                    or voice.region.loop_mode == "loop_continuous"
                )
            )
            self._refresh_resampler_table(voice)
            if voice.resampler_table is not None:
                source_left, source_right = self._bandlimited_frame(
                    voice,
                    frames,
                    playback_end=playback_end,
                    loop_active=loop_active,
                    loop_start=loop_start,
                    loop_end=loop_end,
                )
            else:
                fraction = voice.position - index
                first = frames[index]
                if loop_active and index + 1 >= loop_end:
                    assert loop_start is not None
                    second = frames[loop_start]
                elif index + 1 >= playback_end:
                    # ``sample_end`` is an exclusive upper boundary internally.
                    # Holding the final included frame for interpolation preserves
                    # SFZ's inclusive ``end`` sample instead of dropping it.
                    second = first
                else:
                    second = frames[index + 1]
                source_left = float(first[0]) + (
                    float(second[0]) - float(first[0])
                ) * fraction
                source_right = float(first[1]) + (
                    float(second[1]) - float(first[1])
                ) * fraction
            if voice.region.stereo_width != 1.0:
                middle = (source_left + source_right) * 0.5
                side = (
                    (source_left - source_right)
                    * 0.5
                    * voice.region.stereo_width
                )
                source_left = middle + side
                source_right = middle - side

            if voice.released:
                voice.envelope = max(0.0, voice.envelope - voice.release_step)
                if voice.envelope <= 0.0:
                    finished.append(note_id)
                    continue
            elif voice.attack_samples > 0 and (
                voice.attack_remaining > 0
                or (
                    voice.attack_remaining == 0
                    and voice.envelope <= 0.0
                    and voice.decay_remaining == voice.decay_samples
                )
            ):
                # Some higher-level instruments intentionally replace the
                # SFZ attack after note-on.  Detect that before the first
                # frame so their existing public behaviour is preserved.
                if (
                    voice.envelope <= 0.0
                    and voice.decay_remaining == voice.decay_samples
                    and voice.attack_remaining != voice.attack_samples
                ):
                    voice.attack_remaining = voice.attack_samples
                voice.envelope = min(1.0, voice.envelope + 1.0 / voice.attack_samples)
                voice.attack_remaining -= 1
                if voice.attack_remaining <= 0:
                    voice.attack_remaining = -1
                    voice.envelope = 1.0
                    if voice.decay_samples == 0:
                        voice.envelope = voice.sustain_level
            elif voice.decay_remaining > 0:
                voice.envelope = max(
                    voice.sustain_level,
                    voice.envelope
                    - (1.0 - voice.sustain_level) / voice.decay_samples,
                )
                voice.decay_remaining -= 1
                if voice.decay_remaining <= 0:
                    voice.envelope = voice.sustain_level
            if not voice.released and voice.envelope <= 0.0:
                finished.append(note_id)
                continue

            amplitude = voice.amplitude * voice.envelope
            if voice.region.sample.channels == 1:
                left += (
                    source_left
                    * amplitude
                    * voice.mono_pan_cosine
                    * _MONO_PAN_POWER
                )
                right += (
                    source_left
                    * amplitude
                    * voice.mono_pan_sine
                    * _MONO_PAN_POWER
                )
            elif voice.region.pan >= 0.0:
                left += source_left * amplitude * (1.0 - voice.region.pan)
                right += source_right * amplitude
            else:
                left += source_left * amplitude
                right += source_right * amplitude * (1.0 + voice.region.pan)
            voice.position += voice.increment
            if loop_active and voice.position >= loop_end:
                voice.position = loop_start + ((voice.position - loop_start) % (loop_end - loop_start))
                voice.looped = True

        for note_id in finished:
            del self.voices[note_id]
        return left, right

    @property
    def active_voice_count(self) -> int:
        return len(self.voices)
