"""冻结并严格核验纯 CC0 VSCO2-CE 中提琴声部子集。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any

import numpy as np
import soundfile as sf


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from tianlai.audio import wav_loop_points  # noqa: E402
from tianlai.canonical_json import (  # noqa: E402
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.instrument import create_instrument  # noqa: E402
from tianlai.instrument_audit import collect_loaded_samples  # noqa: E402
from VSCO2中提琴映射 import (  # noqa: E402
    ALL_SAMPLES,
    LICENSE_EVIDENCE,
    SOURCE_SUBDIRECTORY,
    expected_sample_paths,
    sample_by_path,
)


OUTPUT = HERE / "资源核验.json"
ACTIVE_THRESHOLD = 1e-4
SPICCATO_TAIL_RMS_MAXIMUM = 1e-4
SPICCATO_END_DISCONTINUITY_MAXIMUM = 1e-3
SUSTAIN_LOOP_SEAM_MAXIMUM = 0.07
MAPPING_EVIDENCE = (
    "Strings/viola-SEC-sustain.sfz",
    "Strings/viola-SEC-staccato.sfz",
)


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _first_last_active(audio: np.ndarray) -> tuple[int, int]:
    envelope = np.max(np.abs(audio), axis=1)
    indices = np.flatnonzero(envelope > ACTIVE_THRESHOLD)
    if len(indices) == 0:
        return len(audio), -1
    return int(indices[0]), int(indices[-1])


def verify() -> dict[str, Any]:
    manifest_path = HERE / "乐器.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    asset_root = (HERE / str(manifest["asset_root"])).resolve()
    source_root = (asset_root / SOURCE_SUBDIRECTORY).resolve()
    failures: list[dict[str, Any]] = []

    if (
        manifest.get("license_status") != "approved"
        or manifest.get("license") != "CC0-1.0"
    ):
        failures.append({"code": "manifest_not_pure_cc0_approved"})
    if not source_root.is_dir():
        failures.append(
            {
                "code": "missing_source_subtree",
                "path": str(source_root),
            }
        )

    expected = set(expected_sample_paths())
    on_disk = {
        path.relative_to(asset_root).as_posix()
        for path in source_root.rglob("*.wav")
    }
    if on_disk != expected:
        failures.append(
            {
                "code": "source_path_set_mismatch",
                "missing": sorted(expected - on_disk),
                "extra": sorted(on_disk - expected),
            }
        )

    instrument = create_instrument(
        manifest,
        48_000,
        base_directory=str(HERE),
    )
    loaded_paths = collect_loaded_samples(instrument)
    loaded: set[str] = set()
    for path in loaded_paths:
        try:
            path.relative_to(source_root)
        except ValueError:
            failures.append(
                {
                    "code": "runtime_sample_outside_cc0_subtree",
                    "path": str(path),
                }
            )
            continue
        loaded.add(path.relative_to(asset_root).as_posix())
    if loaded != expected:
        failures.append(
            {
                "code": "runtime_path_set_mismatch",
                "missing": sorted(expected - loaded),
                "extra": sorted(loaded - expected),
            }
        )

    evidence_sha256: dict[str, str] = {}
    for relative in LICENSE_EVIDENCE:
        path = asset_root / Path(relative)
        if not path.is_file():
            failures.append(
                {
                    "code": "missing_license_evidence",
                    "path": relative,
                }
            )
            continue
        evidence_sha256[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    mapping_evidence_sha256: dict[str, str] = {}
    for relative in MAPPING_EVIDENCE:
        path = asset_root / Path(relative)
        if not path.is_file():
            failures.append(
                {
                    "code": "missing_mapping_evidence",
                    "path": relative,
                }
            )
            continue
        mapping_evidence_sha256[relative] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()

    mapping = sample_by_path()
    records: dict[str, dict[str, Any]] = {}
    set_hash_lines: list[str] = []
    content_hashes: set[str] = set()
    format_counts: dict[str, int] = {}
    total_bytes = 0
    maximum_spiccato_tail_rms = 0.0
    maximum_spiccato_end_discontinuity = 0.0
    maximum_sustain_loop_seam = 0.0

    for relative in sorted(expected):
        path = asset_root / Path(relative)
        if not path.is_file():
            continue
        source = mapping[relative]
        info = sf.info(str(path))
        audio, sample_rate = sf.read(
            str(path),
            dtype="float64",
            always_2d=True,
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        content_hashes.add(digest)
        set_hash_lines.append(f"{digest}  {relative}\n")
        total_bytes += path.stat().st_size
        format_key = f"{info.format}:{info.subtype}:{info.samplerate}Hz:{info.channels}ch"
        format_counts[format_key] = format_counts.get(format_key, 0) + 1

        if (
            info.format != "WAV"
            or info.samplerate != 44_100
            or info.channels != 2
            or info.subtype not in {"PCM_16", "PCM_24"}
        ):
            failures.append(
                {
                    "code": "unexpected_audio_format",
                    "sample": relative,
                    "actual": format_key,
                }
            )
        if source.articulation == "spiccato" and info.subtype != "PCM_16":
            failures.append(
                {
                    "code": "spiccato_subtype_changed",
                    "sample": relative,
                    "actual": info.subtype,
                }
            )

        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        clipped = int(np.count_nonzero(np.abs(audio) >= 1.0))
        first_active, last_active = _first_last_active(audio)
        if peak <= ACTIVE_THRESHOLD:
            failures.append({"code": "silent_sample", "sample": relative})
        if clipped:
            failures.append(
                {
                    "code": "clipped_source",
                    "sample": relative,
                    "clipped_values": clipped,
                }
            )

        tail_frames = max(1, min(info.frames, round(0.02 * sample_rate)))
        tail = audio[-tail_frames:]
        tail_peak = float(np.max(np.abs(tail)))
        tail_rms = float(math.sqrt(np.mean(tail * tail)))
        end_discontinuity = float(
            np.max(np.abs(audio[-1] - audio[-2]))
        )
        loop = wav_loop_points(path)
        loop_seam = None
        loop_seam_relative_to_rms = None

        if source.articulation == "spiccato":
            maximum_spiccato_tail_rms = max(
                maximum_spiccato_tail_rms,
                tail_rms,
            )
            maximum_spiccato_end_discontinuity = max(
                maximum_spiccato_end_discontinuity,
                end_discontinuity,
            )
            if loop is not None:
                failures.append(
                    {
                        "code": "spiccato_must_not_loop",
                        "sample": relative,
                    }
                )
            if tail_rms > SPICCATO_TAIL_RMS_MAXIMUM:
                failures.append(
                    {
                        "code": "spiccato_tail_not_quiet",
                        "sample": relative,
                        "tail_rms": tail_rms,
                    }
                )
            if end_discontinuity > SPICCATO_END_DISCONTINUITY_MAXIMUM:
                failures.append(
                    {
                        "code": "spiccato_end_discontinuity",
                        "sample": relative,
                        "difference": end_discontinuity,
                    }
                )
        else:
            if loop is None:
                failures.append(
                    {
                        "code": "sustain_missing_loop",
                        "sample": relative,
                    }
                )
            else:
                loop_start, loop_end = loop
                if not 0 <= loop_start < loop_end <= info.frames:
                    failures.append(
                        {
                            "code": "invalid_sustain_loop",
                            "sample": relative,
                            "loop": [loop_start, loop_end],
                        }
                    )
                else:
                    loop_seam = float(
                        np.max(
                            np.abs(
                                audio[loop_end - 1]
                                - audio[loop_start]
                            )
                        )
                    )
                    neighborhood = np.concatenate(
                        (
                            audio[
                                max(0, loop_start - tail_frames) :
                                min(info.frames, loop_start + tail_frames)
                            ],
                            audio[max(0, loop_end - tail_frames) : loop_end],
                        )
                    )
                    neighborhood_rms = float(
                        math.sqrt(np.mean(neighborhood * neighborhood))
                    )
                    loop_seam_relative_to_rms = (
                        loop_seam / max(neighborhood_rms, 1e-12)
                    )
                    maximum_sustain_loop_seam = max(
                        maximum_sustain_loop_seam,
                        loop_seam,
                    )
                    if loop_seam > SUSTAIN_LOOP_SEAM_MAXIMUM:
                        failures.append(
                            {
                                "code": "sustain_loop_seam_too_large",
                                "sample": relative,
                                "difference": loop_seam,
                            }
                        )

        records[relative] = {
            "sha256": digest,
            "bytes": path.stat().st_size,
            "articulation": source.articulation,
            "root_midi": source.root_midi,
            "key_range": [source.key_min, source.key_max],
            "recorded_velocity": source.recorded_velocity,
            "round_robin_position": source.round_robin_position,
            "format": info.format,
            "subtype": info.subtype,
            "sample_rate": info.samplerate,
            "channels": info.channels,
            "frames": info.frames,
            "duration_seconds": round(info.duration, 6),
            "peak": round(peak, 7),
            "clipped_values": clipped,
            "activity_threshold": ACTIVE_THRESHOLD,
            "leading_quiet_frames": first_active,
            "trailing_quiet_frames": (
                info.frames - 1 - last_active
                if last_active >= 0
                else info.frames
            ),
            "tail_20ms_peak": round(tail_peak, 7),
            "tail_20ms_rms": round(tail_rms, 8),
            "final_frame_discontinuity": round(end_discontinuity, 7),
            "embedded_loop": list(loop) if loop is not None else None,
            "loop_seam_difference": (
                round(loop_seam, 7) if loop_seam is not None else None
            ),
            "loop_seam_relative_to_neighborhood_rms": (
                round(loop_seam_relative_to_rms, 6)
                if loop_seam_relative_to_rms is not None
                else None
            ),
        }

    if len(content_hashes) != 36:
        failures.append(
            {
                "code": "duplicate_or_missing_source_audio",
                "expected_unique_sha256": 36,
                "actual_unique_sha256": len(content_hashes),
            }
        )

    spiccato_rr_pairs: dict[str, dict[str, Any]] = {}
    for root_midi in sorted(
        {item.root_midi for item in ALL_SAMPLES if item.articulation == "spiccato"}
    ):
        pair = [
            item
            for item in ALL_SAMPLES
            if item.articulation == "spiccato"
            and item.root_midi == root_midi
        ]
        hashes = [
            records[item.relative_path]["sha256"]
            for item in pair
            if item.relative_path in records
        ]
        distinct = len(pair) == 2 and len(set(hashes)) == 2
        spiccato_rr_pairs[str(root_midi)] = {
            "positions": sorted(
                item.round_robin_position for item in pair
            ),
            "distinct_audio_sha256": distinct,
        }
        if not distinct:
            failures.append(
                {
                    "code": "spiccato_rr_pair_not_distinct",
                    "root_midi": root_midi,
                }
            )

    format_expectation = {
        "WAV:PCM_16:44100Hz:2ch": 33,
        "WAV:PCM_24:44100Hz:2ch": 3,
    }
    if format_counts != format_expectation:
        failures.append(
            {
                "code": "format_distribution_changed",
                "expected": format_expectation,
                "actual": format_counts,
            }
        )

    document: dict[str, Any] = {
        "schema_version": 2,
        "status": "passed" if not failures else "failed",
        "upstream": manifest["upstream"],
        "origin": manifest["origin"],
        "distribution_origin": manifest["distribution_origin"],
        "upstream_version": manifest["upstream_version"],
        "license": "CC0-1.0",
        "license_status": "approved",
        "license_scope": (
            "all 36 runtime samples are confined to "
            "libs/VSCO2-CE/Strings/Viola Section"
        ),
        "attribution_required": False,
        "provenance_retained_despite_cc0": True,
        "evidence_sha256": evidence_sha256,
        "mapping_evidence_sha256": mapping_evidence_sha256,
        "mapping_evidence_role": (
            "numeric roots, key zones, gain, pan and B4 offset only; runtime "
            "does not parse these mixed-library SFZ files"
        ),
        "hash_algorithm": HASH_ALGORITHM,
        "canonicalization": CANONICALIZATION,
        "manifest_canonical_sha256": canonical_json_file_sha256(
            manifest_path
        ),
        "mapping_sha256": hashlib.sha256(
            (HERE / "VSCO2中提琴映射.py").read_bytes()
        ).hexdigest(),
        "implementation_sha256": hashlib.sha256(
            (HERE / "乐器.py").read_bytes()
        ).hexdigest(),
        "sample_count": len(records),
        "unique_audio_sha256_count": len(content_hashes),
        "sample_bytes": total_bytes,
        "sample_formats": {
            name: format_counts[name] for name in sorted(format_counts)
        },
        "sample_set_sha256": hashlib.sha256(
            "".join(set_hash_lines).encode("utf-8")
        ).hexdigest(),
        "sample_set_hash_algorithm": (
            "sort exact VPO-root-relative UTF-8 paths; concatenate lowercase "
            "'<sha256>  <path>\\n>'; SHA-256 the UTF-8 bytes"
        ),
        "structure": {
            "articulation_sample_counts": {
                "sustain": 12,
                "spiccato": 24,
            },
            "recorded_velocity_layers": {
                "sustain": 1,
                "spiccato": 1,
            },
            "recorded_round_robins": {
                "sustain": 1,
                "spiccato": 2,
            },
            "sustain_recorded_roots_midi": sorted(
                {
                    item.root_midi
                    for item in ALL_SAMPLES
                    if item.articulation == "sustain"
                }
            ),
            "spiccato_recorded_roots_midi": sorted(
                {
                    item.root_midi
                    for item in ALL_SAMPLES
                    if item.articulation == "spiccato"
                }
            ),
            "playable_key_range_midi": [48, 93],
            "spiccato_rr_pairs": spiccato_rr_pairs,
            "velocity_interpretation": (
                "every filename is v2; velocity is runtime amplitude only and "
                "does not select another recorded tier"
            ),
        },
        "signal_summary": {
            "clipped_value_count": sum(
                int(item["clipped_values"]) for item in records.values()
            ),
            "maximum_spiccato_tail_20ms_rms": round(
                maximum_spiccato_tail_rms,
                8,
            ),
            "spiccato_tail_20ms_rms_limit": (
                SPICCATO_TAIL_RMS_MAXIMUM
            ),
            "maximum_spiccato_final_frame_discontinuity": round(
                maximum_spiccato_end_discontinuity,
                7,
            ),
            "spiccato_final_frame_discontinuity_limit": (
                SPICCATO_END_DISCONTINUITY_MAXIMUM
            ),
            "maximum_sustain_loop_seam_difference": round(
                maximum_sustain_loop_seam,
                7,
            ),
            "sustain_loop_seam_review_limit": (
                SUSTAIN_LOOP_SEAM_MAXIMUM
            ),
            "sustain_tail_interpretation": (
                "susvib files end inside an active embedded loop, so their "
                "non-silent EOF is intentional; loop seam is audited instead"
            ),
        },
        "samples": records,
        "failures": failures,
    }
    _write_json_atomic(OUTPUT, document)
    if failures:
        preview = "; ".join(
            f"{item['code']}:{item.get('sample', item.get('root_midi', ''))}"
            for item in failures[:8]
        )
        raise RuntimeError(
            f"VSCO2 中提琴资源核验失败，共 {len(failures)} 项：{preview}"
        )
    return document


def main() -> None:
    report = verify()
    print(
        "VSCO2 中提琴资源通过："
        f"{report['sample_count']} WAV，"
        f"{report['unique_audio_sha256_count']} 个独立 SHA-256，"
        f"许可 {report['license']}"
    )


if __name__ == "__main__":
    main()
