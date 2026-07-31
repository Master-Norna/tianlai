# 钢弦吉他(formal)

FreePats FS Seagull 钢弦原声吉他专用多采样。本目录是 98 项清单 SAM-18 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:FreePats project: FS Seagull Steel String Guitar (FlameStudios 采样)
- 版本:2020-05-21,许可:GPL-3.0-or-later WITH FlameStudios sampling exception
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `normal`:`FSS-SteelStringGuitar-20200521.sfz`

默认奏法 `normal`;pitch_mode `pitched`。

## 音域

E2(40) - B5(83)

## 调音

59 个根采样谐波 FFT 诊断;实测中位 +7.008 c,上游映射后残差中位 +7.008 c,最大残差 19.872 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/钢弦吉他_奏法.events.json`;
渲染 10.15 s,峰值 0.419960,
RMS 0.073099,削波 0;
WAV SHA-256 `a745d424…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

上游对采样区做了外扩,本入口收紧到 E2-B5 实际指板;无扫弦/泛音奏法。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
