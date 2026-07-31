from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# pyfluidsynth 在 import 时就会寻找 DLL，顺序不能颠倒。
from tianlai.soundfont import (
    local_compatibility_soundfont_notice,
    prepare_fluidsynth_runtime,
)

prepare_fluidsynth_runtime(str(ROOT))


def _catalog() -> list[tuple[Path, dict]]:
    result: list[tuple[Path, dict]] = []
    for path in sorted((ROOT / "乐器").rglob("乐器.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("type") == "soundfont":
            result.append((path, data))
    return result


def _test_notes(manifest: dict) -> list[int]:
    if "fixed_midi_note" in manifest:
        return [max(0, min(127, int(manifest["fixed_midi_note"])))]
    low = max(0, min(127, int(round(float(manifest.get("note_min", 48))))))
    high = max(low, min(127, int(round(float(manifest.get("note_max", 84))))))
    if high - low < 5:
        return sorted(set([low, (low + high) // 2, high]))
    margin = max(1, min(5, (high - low) // 6))
    return sorted(set([low + margin, (low + high) // 2, high - margin]))


def _render_probe(synth, channel: int, midi: int, sample_rate: int) -> dict:
    synth.cc(channel, 7, 127)
    synth.cc(channel, 11, 127)
    synth.noteon(channel, midi, 108)
    raw = np.asarray(synth.get_samples(round(sample_rate * 0.58)), dtype=np.float64)
    synth.noteoff(channel, midi)
    synth.cc(channel, 120, 0)
    if raw.size % 2:
        raw = raw[:-1]
    audio = raw / 32768.0
    finite = bool(np.isfinite(audio).all())
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    rms = float(np.sqrt(np.mean(audio * audio))) if audio.size else 0.0
    return {
        "midi_note": midi,
        "finite": finite,
        "peak": peak,
        "rms": rms,
        "silent": peak <= 1e-6 or rms <= 1e-8,
    }


def _shared_scan(catalog: list[tuple[Path, dict]], soundfont: Path, sample_rate: int) -> list[dict]:
    import fluidsynth  # type: ignore

    synth = fluidsynth.Synth(
        gain=0.72,
        samplerate=sample_rate,
        channels=16,
        **{
            "synth.reverb.active": 1,
            "synth.chorus.active": 1,
            "synth.threadsafe-api": 0,
        },
    )
    sfid = int(synth.sfload(str(soundfont)))
    if sfid < 0:
        raise RuntimeError(f"FluidSynth 无法载入：{soundfont}")
    results: list[dict] = []
    try:
        for index, (path, manifest) in enumerate(catalog, 1):
            name = path.parent.name
            percussion = bool(manifest.get("percussion", False))
            channel = 9 if percussion else 0
            bank = 128 if percussion else int(manifest.get("bank", 0))
            program = int(manifest.get("program", 0))
            synth.system_reset()
            status_code = int(synth.program_select(channel, sfid, bank, program))
            synth.pitch_bend(channel, 0)
            probes = [_render_probe(synth, channel, midi, sample_rate) for midi in _test_notes(manifest)]
            finite = all(probe["finite"] for probe in probes)
            audible = all(not probe["silent"] for probe in probes)
            peak = max((probe["peak"] for probe in probes), default=0.0)
            rms = min((probe["rms"] for probe in probes), default=0.0)
            status = "pass" if status_code == 0 and finite and audible else "fail"
            result = {
                "name": name,
                "manifest": str(path.relative_to(ROOT)),
                "type": manifest.get("type"),
                "program": program,
                "bank": bank,
                "channel": channel,
                "sample_rate": sample_rate,
                "notes": probes,
                "finite": finite,
                "peak": peak,
                "minimum_probe_rms": rms,
                "status": status,
            }
            if status_code != 0:
                result["error"] = f"program_select={status_code}"
            elif not finite:
                result["error"] = "输出包含 NaN 或无穷值"
            elif not audible:
                missing = [str(probe["midi_note"]) for probe in probes if probe["silent"]]
                result["error"] = "静音或映射缺失，MIDI：" + ", ".join(missing)
            results.append(result)
            print(f"[{index:03d}/{len(catalog):03d}] {name}: {status} peak={peak:.5f}", flush=True)
    finally:
        synth.delete()
    return results


def _isolated_scan(catalog: list[tuple[Path, dict]], soundfont: Path) -> list[dict]:
    env = os.environ.copy()
    env["TIANLAI_SOUNDFONT"] = str(soundfont)
    helper = ROOT / "tools" / "_巡检单件乐器.py"
    results: list[dict] = []
    for index, (path, _) in enumerate(catalog, 1):
        completed = subprocess.run(
            [sys.executable, str(helper), str(path), "--sample-rate", "16000", "--seconds", "0.55"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=45,
        )
        try:
            result = json.loads(completed.stdout.strip().splitlines()[-1])
        except Exception:
            result = {
                "name": path.parent.name,
                "manifest": str(path.relative_to(ROOT)),
                "status": "fail",
            }
        if completed.returncode != 0:
            result["status"] = "fail"
            result["error"] = (
                completed.stderr.strip()
                or completed.stdout.strip()
                or f"exit {completed.returncode}"
            )
        results.append(result)
        print(f"[{index:03d}/{len(catalog):03d}] {path.parent.name}: {result['status']}", flush=True)
    return results


def _locate_soundfont(explicit: str) -> Path:
    if explicit:
        selected = Path(explicit).expanduser()
        source = "--soundfont"
    else:
        env = os.environ.get("TIANLAI_SOUNDFONT")
        if not env:
            raise SystemExit(
                "SoundFont 巡检是显式本机兼容/测试入口；必须传入 "
                "--soundfont <路径> 或明确设置 TIANLAI_SOUNDFONT，"
                "不会自动选择 GeneralUser GS、TimGM 或系统 GM 音源。"
            )
        selected = Path(env).expanduser()
        source = "TIANLAI_SOUNDFONT"
    resolved = selected.resolve()
    if not resolved.is_file() or resolved.suffix.lower() not in {".sf2", ".sf3"}:
        raise SystemExit(f"{source} 指定的 SoundFont 不存在或不是 .sf2/.sf3：{resolved}")
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "显式本机兼容/测试：逐件实渲染用户指定的 SoundFont 乐器并输出报告"
        )
    )
    parser.add_argument("--json-output", default="新增乐器验证报告.json")
    parser.add_argument("--markdown-output", default="新增乐器验证报告.md")
    parser.add_argument("--soundfont", default="")
    parser.add_argument("--sample-rate", type=int, default=24000)
    parser.add_argument("--expected-count", type=int, default=98)
    parser.add_argument("--isolated", action="store_true", help="每件乐器单独进程做事件接口深度巡检（较慢）")
    args = parser.parse_args()

    soundfont = _locate_soundfont(args.soundfont)
    notice = local_compatibility_soundfont_notice(soundfont)
    if notice is not None:
        print(f"warning: {notice}", file=sys.stderr)
    catalog = _catalog()
    count_matches = len(catalog) == args.expected_count
    if not catalog:
        raise SystemExit("未发现 type=soundfont 的乐器清单。")

    results = (
        _isolated_scan(catalog, soundfont)
        if args.isolated
        else _shared_scan(catalog, soundfont, args.sample_rate)
    )
    passed = sum(result.get("status") == "pass" for result in results)
    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": "isolated" if args.isolated else "shared-bank-range-scan",
        "soundfont": str(soundfont),
        "soundfont_sha256": _sha256(soundfont),
        "expected_count": args.expected_count,
        "count_matches": count_matches,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }
    (ROOT / args.json_output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# 新增乐器实渲染验证报告",
        "",
        f"- 验证模式：`{report['mode']}`",
        f"- 目录数量：**{len(results)} / 期望 {args.expected_count}**",
        f"- 通过：**{passed}**",
        f"- 失败：**{len(results) - passed}**",
        f"- 测试音源：`{soundfont}`",
        f"- 音源 SHA-256：`{report['soundfont_sha256']}`",
        "",
        "> 共享扫描会在每件旋律乐器音域的低、中、高位置实际生成 PCM；固定音高打击乐检查指定键。",
        "",
        "| 乐器 | Bank/Program | 探测音 | Peak | 最低 RMS | 状态 |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for result in results:
        peak = result.get("peak", 0.0)
        rms = result.get("minimum_probe_rms", result.get("rms", 0.0))
        notes = ",".join(str(item["midi_note"]) for item in result.get("notes", [])) or str(
            result.get("midi_note", "-")
        )
        status = (
            "通过"
            if result.get("status") == "pass"
            else f"失败：{result.get('error', '未知错误')}"
        )
        lines.append(
            f"| {result.get('name')} | {result.get('bank', '-')}/{result.get('program', '-')} "
            f"| {notes} | {peak:.6f} | {rms:.6f} | {status} |"
        )
    (ROOT / args.markdown_output).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if count_matches and passed == len(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
