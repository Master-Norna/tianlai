import json
from pathlib import Path
import struct
import tempfile
import unittest
import wave

import pytest

from tianlai.dedicated_candidates import (
    _region_has_effective_loop,
    dedicated_manifest_sources,
    generate_dedicated_resource_verification,
)


ROOT = Path(__file__).resolve().parents[1]
INSTRUMENT_ROOT = ROOT / "乐器"


def write_test_wav(
    path: Path,
    *,
    frame_count: int = 64,
    embedded_loop: tuple[int, int] | None = None,
) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        output.writeframes(struct.pack("<h", 1_000) * frame_count)
    if embedded_loop is None:
        return

    start, inclusive_end = embedded_loop
    payload = bytearray(60)
    struct.pack_into("<I", payload, 28, 1)
    struct.pack_into(
        "<IIIIII", payload, 36, 0, 0, start, inclusive_end, 0, 0
    )
    with path.open("ab") as output:
        output.write(b"smpl")
        output.write(struct.pack("<I", len(payload)))
        output.write(payload)
    with path.open("r+b") as output:
        output.seek(0, 2)
        riff_size = output.tell() - 8
        output.seek(4)
        output.write(struct.pack("<I", riff_size))


class DedicatedResourceVerificationTests(unittest.TestCase):
    def test_loop_count_requires_active_valid_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            write_test_wav(root / "plain.wav")
            write_test_wav(root / "explicit.wav")
            write_test_wav(root / "embedded.wav", embedded_loop=(8, 31))
            write_test_wav(root / "one-shot.wav", embedded_loop=(8, 31))
            write_test_wav(root / "invalid-explicit.wav")
            write_test_wav(root / "invalid-embedded.wav", embedded_loop=(48, 95))
            (root / "LICENSE").write_text("test licence\n", encoding="utf-8")
            (root / "instrument.sfz").write_text(
                "<region> sample=plain.wav key=60 loop_mode=loop_sustain\n"
                "<region> sample=explicit.wav key=61 loop_mode=loop_sustain "
                "loop_start=8 loop_end=31\n"
                "<region> sample=embedded.wav key=62 loop_mode=loop_sustain\n"
                "<region> sample=one-shot.wav key=63 loop_mode=one_shot\n"
                "<region> sample=invalid-explicit.wav key=64 "
                "loop_mode=loop_sustain loop_start=48 loop_end=95\n"
                "<region> sample=invalid-embedded.wav key=65 "
                "loop_mode=loop_continuous\n",
                encoding="utf-8",
            )
            manifest_path = root / "instrument.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "type": "dedicated_sfz",
                        "asset_root": ".",
                        "sfz": "instrument.sfz",
                        "upstream": "test fixture",
                        "origin": "local test",
                        "upstream_version": "1",
                        "license": "CC0-1.0",
                        "evidence_files": ["LICENSE"],
                    }
                ),
                encoding="utf-8",
            )

            report = generate_dedicated_resource_verification(
                manifest_path,
                output_path=root / "report.json",
            )

            # Only the explicit in-range pair and the valid WAV smpl pair are
            # effective loops.  A sustain policy alone, one-shot playback, and
            # explicit/embedded points beyond the sample are not loop evidence.
            self.assertEqual(
                report["articulations"]["default"]["looped_regions"],
                2,
            )

    @pytest.mark.external_assets
    def test_all_installed_dedicated_loop_reports_match_current_parser(self) -> None:
        """Prevent frozen reports from drifting behind loop-validation semantics."""

        checked: list[str] = []
        for manifest_path in sorted(INSTRUMENT_ROOT.rglob("乐器.json")):
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("type") not in {"dedicated_sfz", "dedicated_fx"}:
                continue

            asset_root = (
                manifest_path.parent / str(manifest.get("asset_root", ""))
            ).resolve()
            report_path = manifest_path.parent / str(
                manifest.get("resource_verification", "资源核验.json")
            )
            self.assertTrue(
                report_path.is_file(),
                f"{manifest_path.parent.relative_to(INSTRUMENT_ROOT)} 缺少资源核验报告",
            )
            report = json.loads(report_path.read_text(encoding="utf-8"))
            source_paths = [
                asset_root / str(relative)
                for relative in report.get("source_file_sha256", {})
            ]
            if source_paths:
                existing = [path.is_file() for path in source_paths]
                if not any(existing):
                    # 发布包可以有意排除大型音源；这种环境没有数据可供复算。
                    continue
                missing = [
                    str(path)
                    for path, present in zip(source_paths, existing)
                    if not present
                ]
                self.assertEqual(
                    missing,
                    [],
                    "dedicated 资源为部分安装",
                )
            elif not asset_root.is_dir():
                continue

            inventory = dedicated_manifest_sources(manifest_path)
            reported = {
                str(name): values.get("looped_regions")
                for name, values in report.get("articulations", {}).items()
            }

            frame_counts: dict[Path, int] = {}
            actual: dict[str, int] = {}
            for name, data in inventory["articulations"].items():
                regions = list(data["attack_regions"]) + list(
                    data["release_regions"]
                )
                actual[str(name)] = sum(
                    1
                    for region in regions
                    if _region_has_effective_loop(region, frame_counts)
                )

            relative = manifest_path.parent.relative_to(INSTRUMENT_ROOT).as_posix()
            with self.subTest(instrument=relative):
                self.assertEqual(
                    reported,
                    actual,
                    f"{relative} 的冻结 looped_regions 已落后于当前解析结果",
                )
            checked.append(relative)

        if not checked:
            self.skipTest("未安装任何 dedicated_sfz/dedicated_fx 音源")


if __name__ == "__main__":
    unittest.main()
