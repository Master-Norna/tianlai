"""冻结竖琴 candidate 实际采用的 VCSL Concert Harp CC0 资源。"""

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
        license_files=("README.md",),
    )
    print(f"已核验 {result['sample_count']} 个竖琴采样")
