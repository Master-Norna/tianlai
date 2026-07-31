"""离线构建竖笛派生采样；不覆盖 VCSL 原始文件。"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.derived_samples import build_derived_resources


HERE = Path(__file__).resolve().parent
RECIPE = HERE / "预处理参数.json"


def build(*, output_root: str | Path | None = None) -> dict[str, object]:
    return build_derived_resources(RECIPE, output_root=output_root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        help="测试用输出目录；省略时写入音源/派生/竖笛-v1",
    )
    arguments = parser.parse_args()
    receipt = build(output_root=arguments.output)
    print(
        f"已构建 {len(receipt['audio_outputs'])} 个竖笛派生采样；"
        "原始 VCSL 资源未修改。"
    )


if __name__ == "__main__":
    main()
