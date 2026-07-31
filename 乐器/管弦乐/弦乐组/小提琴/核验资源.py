"""复算小提琴实际加载采样的 SHA-256 与许可证据。

这件乐器有两种编制变体(SOLO 独奏 / SEC 声部齐奏),各自加载**不同的采样库**,
所以证据必须两套都冻结:只冻结默认的 SOLO 会让 SEC 那套无证可查。这里对每个
变体各构造一次实例、收集它真实加载的采样,合并成一份带 variants 的报告。
"""

from pathlib import Path
import json
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.instrument_audit import generate_sampled_resource_verification

_LICENSE = (
    "混合公开许可:SSO Sampling Plus 1.0(SEC 声部采样)、"
    "No Budget Orchestra/Mattias CC BY-SA 4.0(SOLO 独奏采样)、VSCO2-CE CC0 等,"
    "见 Documentation/license.htm"
)
_UPSTREAM = "Virtual Playing Orchestra 3(Standard 3.3 / Wave 3.2)"


def _report_for(here: Path, variant: str, temp_dir: Path) -> dict:
    """按指定变体构造实例并冻结其采样;不覆盖正式的 资源核验.json。"""

    manifest = json.loads((here / "乐器.json").read_text(encoding="utf-8"))
    manifest["sample_variant"] = variant
    # 临时清单必须与 乐器.py/音源 保持同一相对位置,故写在乐器目录内再删除。
    temp_manifest = here / f".variant-{variant}.json"
    temp_manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        return generate_sampled_resource_verification(
            temp_manifest,
            output_path=temp_dir / f"{variant}.json",
            license_note=_LICENSE,
            upstream=_UPSTREAM,
            origin="http://virtualplaying.com",
            upstream_version="Standard 3.3 / Wave 3.2",
            evidence_files=("Documentation/license.htm",),
        )
    finally:
        temp_manifest.unlink(missing_ok=True)


def main() -> None:
    here = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as raw_temp:
        temp_dir = Path(raw_temp)
        variants = {name: _report_for(here, name, temp_dir) for name in ("SOLO", "SEC")}

    shared = variants["SOLO"]
    merged = {
        "upstream": shared.get("upstream"),
        "origin": shared.get("origin"),
        "upstream_version": shared.get("upstream_version"),
        "license": shared.get("license"),
        "evidence_sha256": shared.get("evidence_sha256"),
        "说明": (
            "一件乐器两种编制变体,各用不同采样库:SOLO=No Budget Orchestra 独奏,"
            "SEC=SSO 第一小提琴声部。下面按变体分别冻结其真实加载的采样。"
        ),
        "variants": {
            name: {
                key: report[key]
                for key in (
                    "sample_count",
                    "sample_bytes",
                    "sample_formats",
                    "sample_enumeration",
                    "sample_set_sha256",
                    "sample_set_hash_algorithm",
                )
                if key in report
            }
            for name, report in variants.items()
        },
        "generated_at": shared.get("generated_at"),
    }
    (here / "资源核验.json").write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for name, report in variants.items():
        print(f"{name}: {report.get('sample_count')} 个采样")


if __name__ == "__main__":
    main()
