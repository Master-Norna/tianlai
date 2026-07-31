"""冻结分层管弦重击 candidate 实际采用的 VPO 资源。"""

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
            "libs/VSCO2-CE/LICENSE.txt",
            "libs/NoBudgetOrch/CelloSect/license.txt",
            "libs/Mattias-Westlund/ViolaSect/readme.txt",
            "libs/Mattias-Westlund/Horns/license.txt",
            "libs/NoBudgetOrch2/Trumpet/license.txt",
        ),
    )
    print(f"已核验 {result['sample_count']} 个管弦重击去重采样")
