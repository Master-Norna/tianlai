"""冻结颤弓弦乐 candidate 实际采用的 VPO 资源。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_strings import generate_string_resource_audit


HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    result = generate_string_resource_audit(
        HERE / "乐器.json",
        HERE / "资源核验.json",
        license_files=(
            "Documentation/license.htm",
            "libs/NoBudgetOrch2/Cello/CelloSect/license.txt",
            "libs/NoBudgetOrch2/Violin/SoloViolin/license.txt",
            "libs/NoBudgetOrch2/Violin/ViolinSect/license.txt",
            "libs/Other/readme-pb.txt",
        ),
    )
    print(f"已核验 {result['sample_count']} 个颤弓弦乐去重采样")
