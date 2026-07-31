"""按 8′ / 4′ 实际音栓音高测量羽管键琴根采样。"""

from __future__ import annotations

import json
import math
from pathlib import Path
import statistics
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.analysis import analyze_file_harmonic_pitch
from tianlai.dedicated_candidates import dedicated_manifest_sources


FOUR_FOOT_SAMPLE_PART = "/Sustains/High/"


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _summary(records: list[dict[str, float | str]]) -> dict[str, float | int]:
    detunes = [float(item["measured_detune_cents"]) for item in records]
    residuals = [
        float(item["residual_after_upstream_map_cents"]) for item in records
    ]
    return {
        "sample_count": len(records),
        "median_measured_detune_cents": round(statistics.median(detunes), 6),
        "maximum_absolute_measured_detune_cents": round(
            max(map(abs, detunes)), 6
        ),
        "median_residual_cents": round(statistics.median(residuals), 6),
        "maximum_absolute_residual_cents": round(max(map(abs, residuals)), 6),
    }


def generate_harpsichord_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, object]:
    """Generate a register-aware calibration for the Flemish harpsichord.

    VCSL maps its ``High`` samples one octave below their physical pitch so
    they implement a real 4′ stop.  Treating ``pitch_keycenter`` as the
    sounding pitch makes a narrow FFT search lock onto an unrelated lower
    partial.  This audit instead measures Low/8′ at the keyboard root and
    High/4′ at keyboard root + 12 semitones.
    """

    inventory = dedicated_manifest_sources(manifest_path)
    manifest = inventory["manifest"]
    source_manifest: Path = inventory["manifest_path"]
    asset_root: Path = inventory["asset_root"]
    reference_a4 = float(manifest.get("reference_a4_hz", 440.0))
    start_seconds = float(manifest.get("calibration_start_seconds", 0.08))
    search_cents = float(manifest.get("calibration_search_cents", 180.0))
    harmonic_count = int(manifest.get("calibration_harmonic_count", 10))
    gate_articulation = str(manifest.get("calibration_articulation", ""))
    if gate_articulation != "eight_foot":
        raise ValueError(
            "harpsichord calibration_articulation must be 'eight_foot' so "
            "a monophonic gate does not mistake the full 4-foot partial for "
            "an octave mapping error"
        )

    claims: dict[Path, list[tuple[float, float]]] = {}
    for data in inventory["articulations"].values():
        for region in data["attack_regions"]:
            sample = Path(region["sample"])
            pair = (
                round(float(region["root_midi"]), 6),
                round(float(region.get("measured_tuning_cents", 0.0)), 6),
            )
            claims.setdefault(sample, []).append(pair)

    samples: dict[str, dict[str, float | str | int]] = {}
    by_register: dict[str, list[dict[str, float | str]]] = {
        "eight_foot": [],
        "four_foot": [],
    }
    for sample, pairs in sorted(claims.items(), key=lambda item: item[0].as_posix()):
        distinct_claims = sorted(set(pairs))
        if len(distinct_claims) != 1:
            raise ValueError(
                f"harpsichord sample has conflicting root/tune claims: {sample}"
            )
        root, claim = distinct_claims[0]
        relative = _relative(sample, asset_root)
        if FOUR_FOOT_SAMPLE_PART in f"/{relative}":
            register = "four_foot"
            register_offset_cents = 1200.0
        elif "/Sustains/Low/" in f"/{relative}":
            register = "eight_foot"
            register_offset_cents = 0.0
        else:
            raise ValueError(f"unknown harpsichord register sample: {relative}")

        keyboard_root_hz = reference_a4 * (2.0 ** ((root - 69.0) / 12.0))
        nominal_sounding_hz = keyboard_root_hz * (
            2.0 ** (register_offset_cents / 1200.0)
        )
        mapped_sounding_hz = nominal_sounding_hz * (
            2.0 ** (claim / 1200.0)
        )
        measurement = analyze_file_harmonic_pitch(
            sample,
            mapped_sounding_hz,
            start_seconds=start_seconds,
            maximum_frames=131_072,
            search_cents=search_cents,
            harmonic_count=harmonic_count,
        )
        measured_detune = 1200.0 * math.log2(
            measurement.measured_hz / nominal_sounding_hz
        )
        residual = 1200.0 * math.log2(
            measurement.measured_hz / mapped_sounding_hz
        )
        record: dict[str, float | str | int] = {
            "register": register,
            "root_midi": root,
            "sounding_root_midi": root + register_offset_cents / 100.0,
            "intentional_register_offset_cents": register_offset_cents,
            "upstream_claimed_detune_cents": claim,
            "measured_hz": round(measurement.measured_hz, 6),
            "measured_detune_cents": round(measured_detune, 6),
            "residual_after_upstream_map_cents": round(residual, 6),
            "mapping_reference_count": len(pairs),
        }
        samples[relative] = record
        by_register[register].append(record)

    all_records = [
        item for records in by_register.values() for item in records
    ]
    document: dict[str, object] = {
        "applicable": True,
        "pitch_mode": "pitched",
        "reference_a4_hz": reference_a4,
        "playback_calibration": (
            "upstream SFZ pitch_keycenter/tune mapping; High samples retain the "
            "intentional +1200-cent 4-foot sounding register"
        ),
        "measurement_role": (
            "diagnostic only; measured_detune_cents is relative to each stop's "
            "physical sounding target, not merely the keyboard key number"
        ),
        "measurement_algorithm": (
            "register-aware harmonic-constrained FFT of raw source audio, "
            f"start={start_seconds:g}s, search +/-{search_cents:g} cents"
        ),
        "automated_monophonic_gate_articulation": gate_articulation,
        "legacy_report_correction": (
            "the former 135.404-cent maximum was a false narrow-window peak "
            "caused by analyzing 4-foot samples at the keyboard octave"
        ),
        "registers": {
            "eight_foot": {
                "keyboard_range": "F1-C6 (MIDI 29-84)",
                "sounding_range": "F1-C6 (MIDI 29-84)",
                "intentional_register_offset_cents": 0.0,
                **_summary(by_register["eight_foot"]),
            },
            "four_foot": {
                "keyboard_range": "F1-C6 (MIDI 29-84)",
                "sounding_range": "F2-C7 (MIDI 41-96)",
                "intentional_register_offset_cents": 1200.0,
                **_summary(by_register["four_foot"]),
            },
        },
        "summary": _summary(all_records),
        "samples": samples,
    }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent
        / str(manifest.get("pitch_calibration", "音准校准.json"))
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_harpsichord_pitch_calibration(here / "乐器.json")
    summary = report["summary"]
    assert isinstance(summary, dict)
    print(
        f"已按 8′/4′ 实音校准 {summary['sample_count']} 个根采样,"
        f"残差中位 {summary['median_residual_cents']:+.3f} c,"
        f"最大 {summary['maximum_absolute_residual_cents']:.3f} c"
    )


if __name__ == "__main__":
    main()
