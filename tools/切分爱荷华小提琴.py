# -*- coding: utf-8 -*-
"""把爱荷华 MIS 小提琴 arco 的多音 AIFF 切成单音 WAV(逐帧测音、中位定名)。

爱荷华每个文件是一条音阶,音间有静音,但录音里有的音连得紧、静音不清晰,
"按段数归位"会错位。这里改为**直接给每段定音**,不依赖段数:

1. 按能量切出发声段;
2. 每段做**逐帧自相关测音**,取稳定段(中间 60%)的**中位数**——揉音 ±20~30
   音分围绕真音高摆动,中位数正好落在真音高上,对揉音鲁棒;
3. 中位音高就近归到半音;同一半音有多段时取最长;
4. 用文件名标注的音域**校验**是否有缺音,而不是用来定名。

输出:notes/<音名>.wav(单声道,峰值归一 -3 dBFS)+ notes/清单.json。
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
import numpy as np
import soundfile as sf

from tianlai.runtime_layout import discover_runtime_layout


NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
OFFSET = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# 消声室干声把弓毛噪声/本底噪声暴露无遗,归一放大 ~17 dB 后成为"沙哑"的高频嘶声
# (稳态 >8kHz 能量占比 13%)。小提琴在本音域(≤C6)的有用谐波到约 8 kHz,故在
# 9 kHz 做温和低通:嘶声从 13% 压到 4%,亮度基本不动。零相位(前后各滤一次)避免
# 起音被群延迟涂抹。
_LOWPASS_HZ = 9000.0


def _biquad_lowpass_coeffs(fc: float, sr: int, q: float = 0.707):
    import math
    w0 = 2 * math.pi * fc / sr
    alpha = math.sin(w0) / (2 * q)
    cosw = math.cos(w0)
    a0 = 1 + alpha
    b0 = (1 - cosw) / 2 / a0
    b1 = (1 - cosw) / a0
    b2 = (1 - cosw) / 2 / a0
    a1 = -2 * cosw / a0
    a2 = (1 - alpha) / a0
    return b0, b1, b2, a1, a2


def _biquad(x, coeffs):
    b0, b1, b2, a1, a2 = coeffs
    y = np.zeros_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for i in range(len(x)):
        yi = b0 * x[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, x[i]
        y2, y1 = y1, yi
        y[i] = yi
    return y


def lowpass_zero_phase(x, fc: float, sr: int):
    """零相位 4 阶巴特沃斯低通:前向滤两次、反向滤两次,群延迟抵消。"""
    coeffs = _biquad_lowpass_coeffs(fc, sr)
    y = _biquad(_biquad(x, coeffs), coeffs)
    y = _biquad(_biquad(y[::-1], coeffs), coeffs)[::-1]
    return y


def note_name(midi: int) -> str:
    return f"{NOTE_NAMES[midi % 12]}{midi // 12 - 1}"


def parse_note(token: str) -> int:
    m = re.match(r"([A-G])(b|#)?(\d)", token)
    letter, acc, octv = m.group(1), m.group(2), int(m.group(3))
    return (octv + 1) * 12 + OFFSET[letter] + (1 if acc == "#" else -1 if acc == "b" else 0)


def parse_range(filename: str) -> list[int]:
    part = filename.split(".")[-2]
    notes = re.findall(r"[A-G][b#]?\d", part)
    if len(notes) == 1:
        return [parse_note(notes[0])]
    return list(range(parse_note(notes[0]), parse_note(notes[1]) + 1))


def voiced_segments(m: np.ndarray, sr: int):
    w = int(0.02 * sr)
    env = np.array([np.sqrt((m[i:i+w]**2).mean()) for i in range(0, len(m)-w, w)])
    thr = env.max() * 0.05
    voiced = env > thr
    raw, i = [], 0
    while i < len(voiced):
        if voiced[i]:
            j = i
            while j < len(voiced) and voiced[j]:
                j += 1
            if (j - i) * w / sr > 0.25:
                raw.append((i * w, j * w))
            i = j
        else:
            i += 1
    return raw


def frame_hz(frame: np.ndarray, sr: int, lo=180.0, hi=1500.0) -> float:
    """单帧自相关测基频。"""
    x = frame - frame.mean()
    if np.sqrt((x**2).mean()) < 1e-4:
        return 0.0
    corr = np.correlate(x, x, mode="full")[len(x) - 1:]
    lag_lo, lag_hi = int(sr / hi), int(sr / lo)
    if lag_hi >= len(corr):
        return 0.0
    seg = corr[lag_lo:lag_hi]
    if len(seg) == 0 or corr[0] <= 0:
        return 0.0
    peak = lag_lo + int(np.argmax(seg))
    # 抛物线插值精修峰位
    if 0 < peak < len(corr) - 1:
        a, b, c = corr[peak-1], corr[peak], corr[peak+1]
        denom = a - 2*b + c
        if denom != 0:
            peak = peak + 0.5 * (a - c) / denom
    return sr / peak if peak > 0 else 0.0


def pitch_plateaus(seg: np.ndarray, sr: int, start: int):
    """在一个发声区里找所有"音高恒定的最长平台",每个平台=一个干净单音。

    连奏处相邻音会渗进同一发声区,若整段取中位数,音高落在两半音之间。改为
    逐帧测音、就近归半音,找连续同半音的长游程(>0.4s)作为音芯。过渡段的半音
    在快速变化,不会形成长平台,自动被排除;一个区里两个音各自成一个平台。

    返回 [(绝对起点, 绝对终点, midi, 音分偏差), ...]。
    """
    fw = int(0.046 * sr)
    hop = fw // 2
    frames = []  # (帧中心偏移, midi_float or None)
    for i in range(0, len(seg) - fw, hop):
        f = frame_hz(seg[i:i+fw], sr)
        if f > 0:
            frames.append((i + fw // 2, 69 + 12 * np.log2(f / 440.0)))
        else:
            frames.append((i + fw // 2, None))
    # 找连续同半音游程
    plateaus = []
    i = 0
    while i < len(frames):
        if frames[i][1] is None:
            i += 1
            continue
        semi = int(round(frames[i][1]))
        j = i
        cents_run = []
        while j < len(frames) and frames[j][1] is not None and int(round(frames[j][1])) == semi:
            cents_run.append((frames[j][1] - semi) * 100)
            j += 1
        dur = (frames[j-1][0] - frames[i][0]) / sr if j > i else 0
        if dur > 0.4:
            s = start + frames[i][0]
            e = start + frames[j-1][0]
            plateaus.append((s, e, semi, round(float(np.median(cents_run)), 1)))
        i = max(j, i + 1)
    return plateaus


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    default_source = (
        discover_runtime_layout().resources
        / "UIowaMIS"
        / "violin"
    )
    parser = argparse.ArgumentParser(
        description="把 Iowa MIS 小提琴音阶 AIFF 切分为单音 WAV",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=default_source,
        help="源 AIFF 目录；默认 TIANLAI_RESOURCE_DIR/UIowaMIS/violin",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="输出目录；默认 <source>/notes",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    source = args.source.expanduser().resolve()
    output = (
        args.output.expanduser().resolve()
        if args.output is not None
        else source / "notes"
    )
    if not source.is_dir():
        raise FileNotFoundError(f"找不到 Iowa MIS 小提琴源目录: {source}")
    output.mkdir(parents=True, exist_ok=True)
    picked: dict[int, dict] = {}
    covered_labels: set[int] = set()
    for aiff in sorted(source.glob("*.aiff")):
        covered_labels |= set(parse_range(aiff.name))
        m, sr = sf.read(str(aiff), dtype="float64", always_2d=True)
        m = m.mean(axis=1)
        for start, end in voiced_segments(m, sr):
            seg = m[start:end]
            for s, e, midi, cents in pitch_plateaus(seg, sr, start):
                if abs(cents) > 40:
                    continue
                # 平台向两侧各留一点,保住起音与自然收尾
                pad = int(0.05 * sr)
                a2 = max(0, s - pad)
                b2 = min(len(m), e + int(0.25 * sr))
                core = m[a2:b2]
                dur = (b2 - a2) / sr
                prev = picked.get(midi)
                if prev is None or dur > prev["dur"]:
                    picked[midi] = {"seg": core.copy(), "sr": sr, "dur": dur,
                                    "cents": cents, "src": aiff.name}
    manifest = {}
    for midi in sorted(picked):
        d = picked[midi]
        seg = d["seg"]
        # 先低通去嘶,再峰值归一(归一才不会把已压低的噪声重新顶上来)。
        seg = lowpass_zero_phase(seg, _LOWPASS_HZ, d["sr"])
        peak = np.max(np.abs(seg))
        if peak > 0:
            seg = seg * (10 ** (-3 / 20) / peak)
        name = note_name(midi)
        sf.write(
            str(output / f"{name}.wav"),
            seg.astype(np.float32),
            d["sr"],
            subtype="PCM_24",
        )
        manifest[name] = {"midi": midi, "measured_cents": d["cents"],
                          "duration_s": round(d["dur"], 2), "source": d["src"]}
    if not picked:
        raise ValueError(f"源目录没有切出任何可用音符: {source}")
    (output / "清单.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lo, hi = min(picked), max(picked)
    print(f"切出 {len(manifest)} 个单音:{note_name(lo)}~{note_name(hi)}")
    missing = sorted(covered_labels - set(picked))
    if missing:
        print("标签覆盖但未切出的音:", ", ".join(note_name(x) for x in missing))
    off = [(n, v["measured_cents"]) for n, v in manifest.items() if abs(v["measured_cents"]) > 25]
    print("偏差 >25 音分:", ", ".join(f"{n}{c:+.0f}" for n, c in off) if off else "无(全部 ≤25,可作校准记录)")


if __name__ == "__main__":
    main()
