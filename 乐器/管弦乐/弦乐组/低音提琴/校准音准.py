"""实测 VPO 低音提琴持续音原始采样的基频。"""

from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.analysis import analyze_file_pitch
from tianlai.vpo_strings import vpo_regions_to_manifest


HERE = Path(__file__).resolve().parent
ASSET_ROOT = (
    HERE / "../../../../音源/VirtualPlayingOrchestra/Virtual-Playing-Orchestra3"
).resolve()
SFZ = ASSET_ROOT / "Strings/bass-SOLO-sustain.sfz"
OUTPUT = HERE / "音准校准.json"


def main() -> None:
    if not SFZ.is_file():
        raise SystemExit(f"未找到音源：{SFZ}")
    samples: dict[str, dict[str, float]] = {}
    for region in vpo_regions_to_manifest(SFZ, use_embedded_loops=False):
        path = Path(region["sample"])
        relative = path.relative_to(ASSET_ROOT).as_posix()
        if relative in samples:
            continue
        root_midi = float(region["root_midi"])
        expected_hz = 440.0 * (2.0 ** ((root_midi - 69.0) / 12.0))
        measurement = analyze_file_pitch(
            path,
            expected_hz,
            start_seconds=0.2,
            maximum_frames=131_072,
            search_cents=180.0,
        )
        samples[relative] = {
            "root_midi": root_midi,
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(measurement.detune_cents, 6),
        }
    detunes = [item["detune_cents"] for item in samples.values()]
    document = {
        "description": "FFT measurement of raw sustained double-bass samples; A4=440 Hz",
        "source_sfz": "Strings/bass-SOLO-sustain.sfz",
        "summary": {
            "sample_count": len(samples),
            "median_detune_cents": round(statistics.median(detunes), 6),
            "maximum_absolute_detune_cents": round(max(map(abs, detunes)), 6),
        },
        "samples": samples,
    }
    OUTPUT.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"已校准 {len(samples)} 个低音提琴持续音采样：{OUTPUT}")


if __name__ == "__main__":
    main()
