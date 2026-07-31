"""冻结钢片琴 candidate 实际采用的 VPO 资源。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_specials import generate_special_resource_audit


HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    result = generate_special_resource_audit(
        HERE / "乐器.json",
        HERE / "资源核验.json",
        license_files=(
            "Documentation/license.htm",
            "libs/stamperadam/samples/celesta/_readme_and_license.txt",
        ),
    )
    print(f"已核验 {result['sample_count']} 个钢片琴去重采样")
