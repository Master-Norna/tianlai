[中文](README.md) | [English](README.en.md)

# 尼龙弦吉他(formal)

FreePats 西班牙古典吉他(尼龙弦)专用多采样。本目录是 98 项清单 SAM-14 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:FreePats project: Spanish Classical Guitar
- 版本:2019-06-18,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `normal`:`SpanishClassicalGuitar-20190618.sfz`

默认奏法 `normal`;pitch_mode `pitched`。

## 音域

E2(40) - B5(83)

## 调音

48 个根采样谐波 FFT 诊断;实测中位 +4.843 c,上游映射后残差中位 +4.843 c,最大残差 16.455 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/尼龙弦吉他_奏法.events.json`;
渲染 10.15 s,峰值 0.420040,
RMS 0.064383,削波 0;
WAV SHA-256 `538e1fa4d22c…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

上游把采样区外扩到 29-88,本入口收紧到 E2-B5 实际指板。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
