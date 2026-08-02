[中文](README.md) | [English](README.en.md)

# 管风琴(formal)

VCSL 管风琴,大/小音栓组双层。本目录是 98 项清单 SAM-48 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `loud`:`Aerophones/Edge-blown Aerophones/Pipe Organ - Loud.sfz`
- `quiet`:`Aerophones/Edge-blown Aerophones/Pipe Organ - Quiet.sfz`

默认奏法 `loud`;pitch_mode `pitched`。

## 音域

C2(36) - C#7(97)

## 调音

42 个根采样谐波 FFT 诊断;实测中位 +0.202 c,上游映射后残差中位 +0.272 c,最大残差 19.188 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/管风琴_奏法.events.json`;
渲染 12.20 s,峰值 0.420021,
RMS 0.085175,削波 0;
WAV SHA-256 `983ef0f0…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

脚键盘专用采样暂未接入;无音栓混合控制。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
