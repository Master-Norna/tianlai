"""复算长笛实际加载采样的 SHA-256 与许可证据。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.instrument_audit import generate_sampled_resource_verification


def main() -> None:
    here = Path(__file__).resolve().parent
    report = generate_sampled_resource_verification(here / "乐器.json",
        license_note="混合公开许可:SSO Sampling Plus 1.0、No Budget Orchestra/Mattias CC BY-SA 4.0、VSCO2-CE CC0 等,见 Documentation/license.htm",
        upstream="Virtual Playing Orchestra 3(Standard 3.3 / Wave 3.2)",
        origin="http://virtualplaying.com",
        upstream_version="Standard 3.3 / Wave 3.2",
        evidence_files=('Documentation/license.htm',),
    )
    print("完成:", report.get("sample_count", report.get("engine_sha256", "记录已写出")))


if __name__ == "__main__":
    main()
