# 拍手(formal)

VCSL 拍手:群体 6RR 与单人 4 力度层。本目录是 98 项清单 SAM-28 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Idiophones/Struck Idiophones/Claps.sfz`

默认奏法 `hit`;pitch_mode `ignore`。

## 键位映射

| MIDI 键 | 内容 |
| --- | --- |
| 60 | 群体拍手 6RR |
| 61 | 单人拍手 4 力度层 |

## 音域

C4(60) - C#4(61)

## 调音

拍手为人体拍击,键 60 群体 6RR、键 61 单人 4 力度层,不做音高校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/拍手_奏法.events.json`;
渲染 6.25 s,峰值 0.420011,
RMS 0.010186,削波 0;
WAV SHA-256 `9c63e6a68ea1…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

群体拍手为小群实录,非观众掌声(掌声另见 SFX-02)。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
