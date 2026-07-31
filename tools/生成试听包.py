"""从 103 件专用实现中渲染一份 18 件代表性试听集。

这个工具替换了早期的 `生成通用乐器试听包.py`。那一版走 fluidsynth 通用
SoundFont,按 `manifest["program"]` 选音色;而专用实现的乐器清单里根本没有
`program` 键,于是每件乐器都取到默认值 0——GM program 0 正是 Acoustic Grand
Piano。结果是整包 18 件全部由同一架钢琴冒充,包括底鼓和海浪。

这一版直接调用与 103 段试音相同的专用渲染路径,每件乐器用自己的实现、自己的
音源、自己的谱例出声。它只用于跨族群快速展示和抽查,不覆盖全部 103 件,也不
包含问卷、批次证据或听审回答,不能代替 ``tools/人工听审.py`` 的人工听审流程。

**不做响度归一化**:各乐器的增益是逐件校准过的,拉平会抹掉这份校准,也就
听不出真实的声部平衡。这里保留真实电平,只把峰值打印出来。

用法:

    .\\.venv\\Scripts\\python.exe tools\\生成试听包.py
    .\\.venv\\Scripts\\python.exe tools\\生成试听包.py --output output\\我的代表试听

为避免误删源码、作品或整个输出目录,``--output`` 只接受项目 ``output/`` 下的
非根子目录。目标子目录已存在时会整体覆盖。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tianlai.canonical_json import (
    CANONICALIZATION,
    HASH_ALGORITHM,
    canonical_json_file_sha256,
)
from tianlai.renderer import render_to_wav


INSTRUMENT_ROOT = ROOT / "乐器"
EXAMPLES = ROOT / "examples"
DEFAULT_OUTPUT = Path("output") / "代表性试听集"

# 覆盖各个族群的代表性选曲;沿用旧包的名单,便于与之前的版本直接对比。
DEMO_NAMES = [
    "中提琴",
    "单簧管",
    "双簧管",
    "圆号",
    "竖琴",
    "定音鼓",
    "马林巴",
    "管风琴",
    "电钢琴",
    "失真电吉他",
    "原声贝斯",
    "中音萨克斯",
    "日本筝",
    "西塔琴",
    "合成器铺底",
    "合成器主音",
    "底鼓",
    "海浪",
]


def _is_reparse_point(metadata: object) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _reject_redirecting_ancestors(path: Path) -> None:
    """Reject links/junctions without confusing Windows 8.3 aliases for one."""

    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ValueError(
                f"无法安全检查代表性试听集输出路径：{current}"
            ) from exc
        else:
            if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
                raise ValueError(
                    "代表性试听集输出路径不能经过链接或联接点重定向："
                    f"{path}"
                )
        parent = current.parent
        if parent == current:
            return
        current = parent


def resolve_output_directory(
    requested: str | Path,
    *,
    project_root: Path | None = None,
) -> Path:
    """Resolve and validate one disposable demo-collection directory.

    The returned path is guaranteed to be a strict descendant of the project's
    resolved ``output`` directory. Resolving before the containment check also
    prevents ``..`` components or an existing link from escaping ``output``.
    """

    root = (ROOT if project_root is None else project_root).resolve()
    declared_output_root = root / "output"
    output_root = declared_output_root.resolve()
    if output_root != declared_output_root:
        raise ValueError(
            "项目 output/ 不能是指向其他位置的链接或联接点："
            f"{declared_output_root}"
        )

    raw = Path(requested)
    requested_path = raw if raw.is_absolute() else root / raw
    _reject_redirecting_ancestors(requested_path)
    candidate = requested_path.resolve(strict=False)

    if candidate == output_root or not candidate.is_relative_to(output_root):
        raise ValueError(
            "代表性试听集输出必须是项目 output/ 下的非根子目录，"
            f"不能使用：{candidate}"
        )
    return candidate


def prepare_output_directory(
    requested: str | Path,
    *,
    project_root: Path | None = None,
) -> Path:
    """Validate, then replace only the requested demo-collection directory."""

    out = resolve_output_directory(requested, project_root=project_root)
    if out.exists():
        # Narrow the destructive-operation race: fail closed if an ancestor
        # was replaced with a link or junction after the initial validation.
        _reject_redirecting_ancestors(out)
        if not out.is_dir():
            raise ValueError(f"代表性试听集输出已存在但不是目录：{out}")
        shutil.rmtree(out)
    out.mkdir(parents=True)
    return out


def find_instrument(name: str) -> Path:
    for path in INSTRUMENT_ROOT.rglob("乐器.json"):
        if path.parent.name == name:
            return path.parent
    raise KeyError(f"找不到试听乐器:{name}")


def events_for(directory: Path) -> Path:
    """按试听报告的路径/哈希查谱例，兼容协议子目录与旧根级谱例。"""

    audition = directory / "试听核验.json"
    if audition.is_file():
        report = json.loads(audition.read_text(encoding="utf-8"))
        canonical_fields = (
            "hash_algorithm",
            "canonicalization",
            "events_canonical_sha256",
        )
        has_canonical = any(field in report for field in canonical_fields)
        label = report.get("events")
        declared_path: Path | None = None
        if isinstance(label, str) and label.strip():
            declared = Path(label)
            declared_path = (
                declared if declared.is_absolute() else ROOT / declared
            ).resolve()
        examples_root = EXAMPLES.resolve()

        if has_canonical:
            if not all(field in report for field in canonical_fields):
                raise ValueError("试听报告的规范化 events 身份字段不完整")
            if report["hash_algorithm"] != HASH_ALGORITHM:
                raise ValueError("试听报告 hash_algorithm 不受支持")
            if report["canonicalization"] != CANONICALIZATION:
                raise ValueError("试听报告 canonicalization 不受支持")
            if (
                declared_path is None
                or not declared_path.is_relative_to(examples_root)
                or not declared_path.is_file()
            ):
                raise ValueError("试听报告声明的 events 不存在或越出 examples")
            if (
                canonical_json_file_sha256(declared_path)
                != report["events_canonical_sha256"]
            ):
                raise ValueError("试听报告的 events_canonical_sha256 已过期")
            return declared_path

        # Archived reports used exact source bytes.  Keep this read-only
        # fallback, but every newly generated report and batch uses the
        # canonical JSON identity above.
        recorded = str(report.get("events_sha256", ""))
        if recorded:
            if (
                declared_path is not None
                and declared_path.is_relative_to(examples_root)
                and declared_path.is_file()
                and hashlib.sha256(declared_path.read_bytes()).hexdigest()
                == recorded
            ):
                return declared_path
            for path in sorted(EXAMPLES.rglob("*.events.json")):
                if hashlib.sha256(path.read_bytes()).hexdigest() == recorded:
                    return path
    matches = sorted(EXAMPLES.rglob(f"{directory.name}_*.events.json"))
    if not matches:
        raise ValueError(f"找不到 {directory.name} 的谱例")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "从 103 件专用实现中选取 18 件生成代表性试听集；"
            "它是快速演示，不是人工听审包"
        )
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=(
            "输出到项目 output/ 下的非根子目录；默认 output/代表性试听集，"
            "既有目标子目录会被覆盖"
        ),
    )
    args = parser.parse_args()

    try:
        out = prepare_output_directory(args.output)
    except ValueError as exc:
        parser.error(str(exc))

    import numpy as np
    import soundfile as sf

    montage: list["np.ndarray"] = []
    playlist: list[str] = []
    sample_rate = 48_000

    for index, name in enumerate(DEMO_NAMES, 1):
        directory = find_instrument(name)
        manifest = json.loads((directory / "乐器.json").read_text(encoding="utf-8"))
        events = events_for(directory)
        filename = f"{index:02d}_{name}.wav"
        render_to_wav(directory / "乐器.json", events, out / filename)

        audio, rate = sf.read(str(out / filename), dtype="float64", always_2d=True)
        sample_rate = int(rate)
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        montage.append(audio)
        montage.append(np.zeros((round(sample_rate * 0.35), 2), dtype=np.float64))
        relative = directory.relative_to(INSTRUMENT_ROOT).as_posix()
        playlist.append(
            f"{index:02d}. {name} — {relative} [{manifest.get('type', '')}] "
            f"峰值 {peak:.4f}  谱例 {events.name}"
        )
        print(f"{filename}  peak {peak:.4f}  ← {manifest.get('name', name)}")

    all_audio = np.concatenate(montage, axis=0)
    sf.write(out / "00_全部试听集.wav", all_audio, sample_rate, subtype="PCM_24")
    (out / "试听顺序.txt").write_text(
        "天籁 18 件代表性试听集 — 全部由各乐器自己的专用实现渲染,"
        "未做响度归一化。\n"
        "用途:跨族群快速展示和抽查;不覆盖全部 103 件,不等于人工听审或验收。\n"
        "生成命令:.\\.venv\\Scripts\\python.exe tools\\生成试听包.py\n\n"
        + "\n".join(playlist)
        + "\n",
        encoding="utf-8",
    )
    print(f"\n已写出 {len(DEMO_NAMES)} 段 + 合集,目录:{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
