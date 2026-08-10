"""复算手风琴的上游 SFZ、采样、许可、Hash 与高音边界证据。"""

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.dedicated_candidates import (
    dedicated_manifest_sources,
    generate_dedicated_resource_verification,
)
from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)


def _selected_region(regions: list[dict], midi_note: int) -> dict:
    matches = [
        region
        for region in regions
        if float(region["key_min"]) <= midi_note <= float(region["key_max"])
    ]
    if len(matches) != 1:
        raise ValueError(
            f"MIDI {midi_note} 应恰好命中一个攻击区，实际命中 {len(matches)} 个"
        )
    return matches[0]


def main() -> None:
    here = Path(__file__).resolve().parent
    manifest_path = here / "乐器.json"
    destination = here / "资源核验.json"
    temporary = here / "资源核验.临时.json"
    try:
        report = generate_dedicated_resource_verification(
            manifest_path,
            output_path=temporary,
        )
        inventory = dedicated_manifest_sources(manifest_path)
        manifest = inventory["manifest"]
        attacks = inventory["articulations"]["sustain"]["attack_regions"]
        roots = sorted({int(region["root_midi"]) for region in attacks})
        if roots != [47, 50, 54, 55, 57, 59, 60, 62, 64, 66, 67, 69, 71, 72, 74, 76, 79]:
            raise ValueError(f"上游 17 根音集合发生变化：{roots}")
        source_low, source_high = roots[0], roots[-1]
        core_low, core_high = int(manifest["note_min"]), source_high
        extension_low, extension_high = core_high + 1, int(manifest["note_max"])
        if (core_low, core_high) != (50, 79):
            raise ValueError("核心运行音域应为 MIDI 50-79")
        if extension_low != core_high + 1 or extension_high != int(manifest["note_max"]):
            raise ValueError("有限扩展必须紧接核心音域并截止于 note_max")
        if (extension_low, extension_high) != (80, 82):
            raise ValueError("有限扩展应严格限制为 MIDI 80-82")

        note_mapping: dict[str, dict[str, object]] = {}
        for midi_note in range(core_low, extension_high + 1):
            region = _selected_region(attacks, midi_note)
            root = int(region["root_midi"])
            note_mapping[str(midi_note)] = {
                "root_midi": root,
                "transposition_semitones": midi_note - root,
                "sample": Path(region["sample"])
                .relative_to(inventory["asset_root"])
                .as_posix(),
                "tier": "core" if midi_note <= core_high else "bounded_extension",
            }

        maximum_core_upward = max(
            int(item["transposition_semitones"])
            for item in note_mapping.values()
            if item["tier"] == "core"
        )
        maximum_extension_upward = max(
            int(item["transposition_semitones"])
            for item in note_mapping.values()
            if item["tier"] == "bounded_extension"
        )
        if maximum_extension_upward != 3:
            raise ValueError("有限扩展的实际最大移调应为 3 个半音")
        if maximum_extension_upward > maximum_core_upward:
            raise ValueError("有限扩展超过了核心音域已有的最大上移量")
        if max(float(region["key_max"]) for region in attacks) != 127.0:
            raise ValueError("上游最高区域不再延伸至 MIDI 127，需要重新审查")

        report.update(
            {
                "schema_version": 2,
                "status": "passed",
                "hash_algorithm": HASH_ALGORITHM,
                "canonicalization": CANONICALIZATION,
                "manifest_canonical_sha256": canonical_json_file_sha256(
                    manifest_path
                ),
                "runtime_range_policy": {
                    "source_recorded_attack_roots_midi": roots,
                    "source_recorded_attack_root_range": [source_low, source_high],
                    "core_playable_range": [core_low, core_high],
                    "bounded_extension_range": [extension_low, extension_high],
                    "legacy_rejected_range": [83, 91],
                    "upstream_raw_high_key": 127,
                    "runtime_high_key": extension_high,
                    "maximum_core_upward_transposition_semitones": maximum_core_upward,
                    "maximum_extension_upward_transposition_semitones": (
                        maximum_extension_upward
                    ),
                    "extension_not_wider_than_existing_core_zone": True,
                    "note_mapping": note_mapping,
                },
                "failures": [],
            }
        )
        final_temporary = destination.with_suffix(".json.tmp")
        final_temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        final_temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(
        f"已核验 {report['sample_count']} 个手风琴资源；"
        f"运行音域 MIDI {core_low}-{extension_high}：{destination}"
    )


if __name__ == "__main__":
    main()
