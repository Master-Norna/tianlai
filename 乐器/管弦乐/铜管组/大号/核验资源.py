"""冻结本 candidate 实际采用的大号资源 Hash。"""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.vpo_brass import generate_resource_audit


HERE = Path(__file__).resolve().parent


if __name__ == "__main__":
    result = generate_resource_audit(
        HERE / "乐器.json",
        HERE / "资源核验.json",
        license_files=("Documentation/license.htm",),
    )
    print(f"已核验 {result['sample_count']} 个大号采样")
