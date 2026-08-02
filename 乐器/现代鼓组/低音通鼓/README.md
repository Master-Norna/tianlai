[中文](README.md) | [English](README.en.md)

# 低音通鼓(formal)

VCSL 低音通鼓,鼓棒/软槌/边击/滚奏 6 键位。本目录是 98 项清单 SAM-23 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Membranophones/Struck Membranophones/Tom 2.sfz`

默认奏法 `hit`;pitch_mode `ignore`。

## 键位映射

| MIDI 键 | 内容 |
| --- | --- |
| 60 | rimFLS 混合边击 2RR |
| 61 | rimS 边击 2 力度×2RR |
| 62 | HitM 软槌 3 力度×2RR |
| 63 | RollM 软槌滚奏 2 力度 |
| 64 | HitS 鼓棒 3 力度×2RR |
| 65 | RollS 鼓棒滚奏 2 力度 |

## 音域

C4(60) - F4(65)

## 调音

低音通鼓为膜鸣打击,键 60-65 分别选择边击/槌击/滚奏变体,不做十二平均律校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/低音通鼓_奏法.events.json`;
渲染 7.00 s,峰值 0.420025,
RMS 0.016846,削波 0;
WAV SHA-256 `0a2c48e5a0e3…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

滚奏为定长实录;非半音乐器。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
