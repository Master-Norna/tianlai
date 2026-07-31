"""实测持续音原始采样的基频，生成小提琴音准校准表。"""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.analysis import analyze_file_pitch
from tianlai.sfz import regions_to_manifest


HERE = Path(__file__).resolve().parent
ASSET_ROOT = (HERE / "../../../../音源/VirtualPlayingOrchestra/Virtual-Playing-Orchestra3").resolve()
SFZ = ASSET_ROOT / "Strings/1st-violin-SOLO-normal-mod-wheel.sfz"
OUTPUT = HERE / "音准校准.json"


def main() -> None:
    if not SFZ.is_file():
        raise SystemExit(f"未找到音源：{SFZ}")
    samples: dict[str, dict[str, float]] = {}
    for region in regions_to_manifest(SFZ, use_embedded_loops=False):
        path = Path(region["sample"])
        root_midi = float(region["root_midi"])
        expected_hz = 440.0 * (2.0 ** ((root_midi - 69.0) / 12.0))
        measurement = analyze_file_pitch(
            path,
            expected_hz,
            start_seconds=0.15,
            maximum_frames=131_072,
            search_cents=120.0,
        )
        relative = path.relative_to(ASSET_ROOT).as_posix()
        samples[relative] = {
            "root_midi": root_midi,
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(measurement.detune_cents, 6),
        }
    document = {
        "description": "FFT measurement of raw sustained violin samples; A4=440 Hz",
        "samples": samples,
    }
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已校准 {len(samples)} 个持续音采样：{OUTPUT}")


if __name__ == "__main__":
    main()
