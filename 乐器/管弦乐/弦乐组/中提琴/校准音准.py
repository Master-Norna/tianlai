"""严格测量 VSCO2-CE 中提琴声部 36 个原始映射采样。"""

from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any

import soundfile as sf


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tianlai.analysis import analyze_file_harmonic_pitch  # noqa: E402
from VSCO2中提琴映射 import ALL_SAMPLES, expected_sample_paths  # noqa: E402


OUTPUT = HERE / "音准校准.json"
FAILURE_OUTPUT = HERE / "音准校准.失败.json"


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _expected_hz(root_midi: int) -> float:
    return 440.0 * 2.0 ** ((root_midi - 69.0) / 12.0)


def _measurement_windows(articulation: str) -> tuple[float, ...]:
    if articulation == "sustain":
        return (0.2, 0.8, 1.4)
    return (0.02, 0.06, 0.12)


def calibrate() -> dict[str, Any]:
    manifest = json.loads((HERE / "乐器.json").read_text(encoding="utf-8"))
    asset_root = (HERE / str(manifest["asset_root"])).resolve()
    failures: list[dict[str, Any]] = []
    samples: dict[str, dict[str, Any]] = {}
    medians: list[float] = []

    actual_paths = tuple(
        sorted(
            path.relative_to(asset_root).as_posix()
            for path in (
                asset_root / "libs/VSCO2-CE/Strings/Viola Section"
            ).rglob("*.wav")
        )
    )
    expected_paths = expected_sample_paths()
    if actual_paths != expected_paths:
        failures.append(
            {
                "code": "source_path_set_mismatch",
                "missing": sorted(set(expected_paths) - set(actual_paths)),
                "extra": sorted(set(actual_paths) - set(expected_paths)),
            }
        )

    for source in sorted(ALL_SAMPLES, key=lambda item: item.relative_path):
        path = (asset_root / Path(source.relative_path)).resolve()
        if not path.is_file():
            failures.append(
                {
                    "code": "missing_sample",
                    "sample": source.relative_path,
                }
            )
            continue
        info = sf.info(str(path))
        expected_hz = _expected_hz(source.root_midi)
        harmonic_count = max(3, min(10, int(5_000.0 / expected_hz)))
        windows: list[dict[str, Any]] = []
        detunes: list[float] = []
        for logical_start in _measurement_windows(source.articulation):
            # Runtime begins B4 sustain at its audited upstream offset.  Pitch
            # evidence starts the same number of frames later.
            file_start = logical_start + source.offset_frames / info.samplerate
            available = info.frames - round(file_start * info.samplerate)
            frame_cap = 65_536 if source.articulation == "sustain" else 32_768
            maximum_frames = min(frame_cap, available)
            if maximum_frames < 4_096:
                windows.append(
                    {
                        "logical_start_seconds": logical_start,
                        "status": "insufficient_audio",
                        "available_frames": available,
                    }
                )
                continue
            try:
                result = analyze_file_harmonic_pitch(
                    path,
                    expected_hz,
                    start_seconds=file_start,
                    maximum_frames=maximum_frames,
                    search_cents=180.0,
                    harmonic_count=harmonic_count,
                )
            except (OSError, RuntimeError, ValueError) as error:
                windows.append(
                    {
                        "logical_start_seconds": logical_start,
                        "status": "analysis_error",
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )
                continue
            detune = float(result.detune_cents)
            detunes.append(detune)
            windows.append(
                {
                    "logical_start_seconds": logical_start,
                    "file_start_seconds": round(file_start, 6),
                    "analysis_frames": maximum_frames,
                    "harmonic_count": harmonic_count,
                    "measured_hz": round(float(result.measured_hz), 6),
                    "detune_cents": round(detune, 6),
                    "status": "accepted",
                }
            )

        record: dict[str, Any] = {
            "articulation": source.articulation,
            "root_midi": source.root_midi,
            "expected_hz": round(expected_hz, 6),
            "recorded_velocity": source.recorded_velocity,
            "round_robin_position": source.round_robin_position,
            "windows": windows,
        }
        if len(detunes) != len(_measurement_windows(source.articulation)):
            record["status"] = "rejected_incomplete_measurement"
            failures.append(
                {
                    "code": "incomplete_pitch_measurement",
                    "sample": source.relative_path,
                    "accepted_windows": len(detunes),
                }
            )
            samples[source.relative_path] = record
            continue

        median_detune = float(statistics.median(detunes))
        window_range = max(detunes) - min(detunes)
        maximum_range = 30.0 if source.articulation == "sustain" else 12.0
        record.update(
            {
                "measured_hz": round(
                    expected_hz * 2.0 ** (median_detune / 1200.0),
                    6,
                ),
                "measured_detune_cents": round(median_detune, 6),
                "window_range_cents": round(window_range, 6),
                "maximum_allowed_window_range_cents": maximum_range,
            }
        )
        if abs(median_detune) > 30.0:
            record["status"] = "rejected_unsafe_detune"
            failures.append(
                {
                    "code": "unsafe_detune",
                    "sample": source.relative_path,
                    "measured_detune_cents": round(median_detune, 6),
                }
            )
        elif window_range > maximum_range:
            record["status"] = "rejected_unstable_windows"
            failures.append(
                {
                    "code": "unstable_pitch_windows",
                    "sample": source.relative_path,
                    "window_range_cents": round(window_range, 6),
                    "maximum": maximum_range,
                }
            )
        else:
            record["status"] = "accepted"
            medians.append(median_detune)
        samples[source.relative_path] = record

    rr_pair_spreads: dict[str, float] = {}
    for root in sorted({item.root_midi for item in ALL_SAMPLES}):
        pair = [
            item
            for item in ALL_SAMPLES
            if item.articulation == "spiccato" and item.root_midi == root
        ]
        values = [
            float(samples[item.relative_path]["measured_detune_cents"])
            for item in pair
            if samples.get(item.relative_path, {}).get("status") == "accepted"
        ]
        if len(pair) != 2 or len(values) != 2:
            continue
        spread = abs(values[0] - values[1])
        rr_pair_spreads[str(root)] = round(spread, 6)
        if spread > 15.0:
            failures.append(
                {
                    "code": "spiccato_rr_pitch_disagreement",
                    "root_midi": root,
                    "spread_cents": round(spread, 6),
                    "maximum": 15.0,
                }
            )

    passed = not failures and len(medians) == 36
    document: dict[str, Any] = {
        "schema_version": 1,
        "status": "passed" if passed else "failed",
        "applicable": True,
        "reference_a4_hz": 440.0,
        "source_subtree": "libs/VSCO2-CE/Strings/Viola Section",
        "measurement_algorithm": (
            "three post-attack harmonic-constrained FFT windows per raw mapped "
            "sample; median becomes the runtime source-root detune"
        ),
        "calibration_semantics": (
            "SampleInstrument treats expected_hz * 2^(measured_detune/1200) "
            "as the recording's actual root, so playback applies the inverse "
            "correction without modifying the source WAV"
        ),
        "source_filename_octave_semantics": (
            "VSCO/VPO filename octave is one below project MIDI/C4=60 naming; "
            "roots are frozen explicitly by the mapping module"
        ),
        "summary": {
            "declared_sample_count": 36,
            "accepted_sample_count": len(medians),
            "rejected_sample_count": 36 - len(medians),
            "sustain_sample_count": 12,
            "spiccato_sample_count": 24,
            "recorded_velocity_layers": {
                "sustain": 1,
                "spiccato": 1,
            },
            "recorded_round_robins": {
                "sustain": 1,
                "spiccato": 2,
            },
            "median_measured_detune_cents": (
                round(statistics.median(medians), 6) if medians else None
            ),
            "maximum_absolute_measured_detune_cents": (
                round(max(map(abs, medians)), 6) if medians else None
            ),
            "maximum_window_range_cents": (
                round(
                    max(
                        float(item["window_range_cents"])
                        for item in samples.values()
                        if "window_range_cents" in item
                    ),
                    6,
                )
                if samples
                else None
            ),
            "maximum_spiccato_rr_pair_spread_cents": (
                max(rr_pair_spreads.values()) if rr_pair_spreads else None
            ),
        },
        "spiccato_rr_pair_spread_cents": rr_pair_spreads,
        "samples": {
            path: samples[path] for path in sorted(samples)
        },
        "failures": failures,
    }
    if passed:
        _write_json_atomic(OUTPUT, document)
    else:
        _write_json_atomic(FAILURE_OUTPUT, document)
        preview = "; ".join(
            f"{item['code']}:{item.get('sample', item.get('root_midi', ''))}"
            for item in failures[:8]
        )
        raise RuntimeError(
            f"VSCO2 中提琴音准校准失败，共 {len(failures)} 项：{preview}；"
            f"诊断见 {FAILURE_OUTPUT}"
        )
    return document


def main() -> None:
    report = calibrate()
    summary = report["summary"]
    print(
        "VSCO2 中提琴音准通过："
        f"{summary['accepted_sample_count']}/"
        f"{summary['declared_sample_count']}，"
        f"最大绝对偏差 {summary['maximum_absolute_measured_detune_cents']:.3f} cents"
    )


if __name__ == "__main__":
    main()
