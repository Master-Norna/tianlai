"""VSCO2-CE 中提琴声部的受信内置、可审计采样映射。

这里只有本地 ``libs/VSCO2-CE/Strings/Viola Section`` 子树中的两种真实
奏法。文件名里的 ``v2`` 是唯一保留的录制力度，不是第二个可切换力度层；
只有 ``spic`` 文件中的 ``rr1/rr2`` 才是两个真实轮替。
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, Mapping


SOURCE_SUBDIRECTORY = Path("libs/VSCO2-CE/Strings/Viola Section")
LICENSE_EVIDENCE = (
    "Documentation/license.htm",
    "libs/VSCO2-CE/LICENSE.txt",
)


@dataclass(frozen=True, slots=True)
class SourceSample:
    articulation: str
    relative_path: str
    root_midi: int
    key_min: int
    key_max: int
    recorded_velocity: str
    round_robin_position: int | None = None
    round_robin_length: int | None = None
    gain_db: float = 0.0
    pan: float = 0.0
    offset_frames: int = 0


# The source filenames use the original library's octave naming convention,
# one octave below this project's MIDI/C4=60 convention.  Numeric roots and
# zones below are the explicit VPO mappings, not guesses parsed at run time.
_SUSTAIN_LAYOUT = (
    ("D2", 50, 48, 50, 6.50, 0.00, 0),
    ("E2", 52, 51, 53, 9.50, 0.00, 0),
    ("G2", 55, 54, 56, 6.50, 0.00, 0),
    ("B2", 59, 57, 60, 5.50, 0.00, 0),
    ("D3", 62, 61, 63, 7.50, 0.00, 0),
    ("F3", 65, 64, 66, 7.50, 0.00, 0),
    ("A3", 69, 67, 70, 5.50, 0.00, 0),
    ("C4", 72, 71, 73, 6.50, 0.00, 0),
    ("E4", 76, 74, 77, 5.50, 0.07, 0),
    ("G4", 79, 78, 80, 8.50, 0.00, 0),
    ("B4", 83, 81, 84, 10.50, 0.00, 2_198),
    ("D5", 86, 85, 93, 8.50, 0.00, 0),
)

_SPICCATO_LAYOUT = (
    ("C2", 48, 48, 51),
    ("E2", 52, 52, 53),
    ("G2", 55, 54, 56),
    ("B2", 59, 57, 60),
    ("D3", 62, 61, 63),
    ("F3", 65, 64, 66),
    ("A3", 69, 67, 70),
    ("C4", 72, 71, 73),
    ("E4", 76, 74, 77),
    ("G4", 79, 78, 80),
    ("B4", 83, 81, 84),
    ("D5", 86, 85, 93),
)


SUSTAIN_SAMPLES = tuple(
    SourceSample(
        articulation="sustain",
        relative_path=(
            "libs/VSCO2-CE/Strings/Viola Section/susvib/"
            f"ViolaEns_susvib_{name}_v2_1-PB-loop.wav"
        ),
        root_midi=root,
        key_min=key_min,
        key_max=key_max,
        recorded_velocity="v2 (single retained recorded tier)",
        gain_db=gain_db,
        pan=pan,
        offset_frames=offset,
    )
    for name, root, key_min, key_max, gain_db, pan, offset in _SUSTAIN_LAYOUT
)

SPICCATO_SAMPLES = tuple(
    SourceSample(
        articulation="spiccato",
        relative_path=(
            "libs/VSCO2-CE/Strings/Viola Section/spic/"
            f"Violas_spic_{name}_v2_rr{round_robin}-PB.wav"
        ),
        root_midi=root,
        key_min=key_min,
        key_max=key_max,
        recorded_velocity="v2 (single retained recorded tier)",
        round_robin_position=round_robin,
        round_robin_length=2,
        pan=(-0.10 if name == "C2" and round_robin == 2 else 0.0),
    )
    for name, root, key_min, key_max in _SPICCATO_LAYOUT
    for round_robin in (1, 2)
)

ALL_SAMPLES = SUSTAIN_SAMPLES + SPICCATO_SAMPLES


def expected_sample_paths() -> tuple[str, ...]:
    return tuple(sorted(item.relative_path for item in ALL_SAMPLES))


def sample_by_path() -> dict[str, SourceSample]:
    return {item.relative_path: item for item in ALL_SAMPLES}


def _detune_for(
    calibration: Mapping[str, Any],
    sample: SourceSample,
) -> float:
    record = calibration.get(sample.relative_path)
    if not isinstance(record, Mapping):
        raise ValueError(
            f"音准表缺少 VSCO2 中提琴采样：{sample.relative_path}"
        )
    if int(record.get("root_midi", -1)) != sample.root_midi:
        raise ValueError(
            f"音准表根音与映射不一致：{sample.relative_path}"
        )
    value = float(record.get("measured_detune_cents", math.nan))
    if not math.isfinite(value) or abs(value) > 50.0:
        raise ValueError(
            f"音准表含不安全校正值：{sample.relative_path}={value!r}"
        )
    return value


def build_region_sets(
    asset_root: Path,
    calibration: Mapping[str, Any],
    manifest: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Build the two truthful region sets consumed by ``SampleInstrument``."""

    sustain_attack = float(manifest.get("sustain_attack_seconds", 0.04))
    sustain_release = float(manifest.get("sustain_release_seconds", 0.8))
    spiccato_attack = float(manifest.get("spiccato_attack_seconds", 0.003))
    region_sets: dict[str, list[dict[str, Any]]] = {
        "sustain": [],
        "spiccato": [],
    }
    for sample in ALL_SAMPLES:
        path = (asset_root / Path(sample.relative_path)).resolve()
        region: dict[str, Any] = {
            "sample": str(path),
            "root_midi": sample.root_midi,
            "measured_tuning_cents": _detune_for(calibration, sample),
            "key_min": sample.key_min,
            "key_max": sample.key_max,
            # `v2` is the only retained source tier.  Velocity changes playback
            # amplitude continuously but never selects a fictitious recording.
            "velocity_min": 0.0,
            "velocity_max": 1.0,
            "gain_db": sample.gain_db,
            "pan": sample.pan,
            "offset_frames": sample.offset_frames,
            "stable_key": sample.relative_path,
        }
        if sample.articulation == "sustain":
            region.update(
                {
                    "attack_seconds": sustain_attack,
                    "release_seconds": sustain_release,
                    "loop_mode": "loop_sustain",
                    "use_embedded_loop": True,
                }
            )
        else:
            region.update(
                {
                    "attack_seconds": spiccato_attack,
                    "loop_mode": "one_shot",
                    "round_robin_position": sample.round_robin_position,
                    "round_robin_length": sample.round_robin_length,
                }
            )
        region_sets[sample.articulation].append(region)
    return region_sets
