**简体中文** | [English](README.en.md)

# 陶笛(formal)

VCSL 标准陶笛,直吹与颤音双奏法。本目录是 98 项清单 SAM-42 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `sustain`:`Aerophones/Edge-blown Aerophones/Ocarina, Typical - Sus.sfz`
- `vibrato`:`Aerophones/Edge-blown Aerophones/Ocarina, Typical - SusVib.sfz`

默认奏法 `sustain`;pitch_mode `pitched`。

## 音域

A4(69) - D6(86)

## 调音

21 个根采样谐波 FFT 诊断;实测中位 +6.518 c,上游映射后残差中位 +4.086 c,最大残差 29.261 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/陶笛_奏法.events.json`;
渲染 12.20 s,峰值 0.420017,
RMS 0.109476,削波 0;
WAV SHA-256 `57bcdd83…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

单力度;音域窄为乐器本身特性。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
