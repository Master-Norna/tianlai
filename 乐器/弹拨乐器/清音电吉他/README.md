[中文](README.md) | [English](README.en.md)

# 清音电吉他(formal)

Karoryfer Emilyguitar 平卷弦 DI 清音电吉他,4 力度层×3 RR。本目录是 98 项清单 SAM-15 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:Karoryfer Lecolds (D. Smolken): Emilyguitar
- 版本:v1.001,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `normal`:`emily_clean.sfz`

默认奏法 `normal`;pitch_mode `pitched`。

## 音域

D2(38) - D6(86)

## 调音

251 个根采样谐波 FFT 诊断;实测中位 +1.155 c,上游映射后残差中位 +1.155 c,最大残差 178.938 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/清音电吉他_奏法.events.json`;
渲染 10.15 s,峰值 0.420014,
RMS 0.040990,削波 0;
WAV SHA-256 `60964dc7…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

DI 直录无箱体;噪声键(90+)不纳入音域;低音弦含降 Db 调弦采样。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
