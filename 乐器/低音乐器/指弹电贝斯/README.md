# 指弹电贝斯(formal)

FreePats Yamaha RBX 指弹电贝斯专用多采样。本目录是 98 项清单 SAM-11 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:FreePats project: Yamaha RBX 指弹电贝斯 (Andrea Biasior)
- 版本:2019-09-30 (main @ 8dcb7ea9116f),许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `normal`:`FingerBassYR 20190930.sfz`

默认奏法 `normal`;pitch_mode `pitched`。

## 音域

E1(28) - A2(45)

## 调音

12 个根采样谐波 FFT 诊断;实测中位 +2.833 c,上游映射后残差中位 +2.833 c,最大残差 8.387 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/指弹电贝斯_奏法.events.json`;
渲染 10.15 s,峰值 0.419986,
RMS 0.087321,削波 0;
WAV SHA-256 `76c2408a…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

上游仅采样到 A2(45),高把位缺失;单力度实录;无滑音/闷音技巧采样。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
