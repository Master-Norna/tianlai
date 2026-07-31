# 开放踩镲(formal)

VCSL 踩镲开放侧:开放击与开-闭击。本目录是 98 项清单 SAM-26 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Idiophones/Struck Idiophones/Hi-Hat Cymbal.sfz`

默认奏法 `hit`;pitch_mode `ignore`。

## 键位映射

| MIDI 键 | 内容 |
| --- | --- |
| 45 | 开后闭合击 1 变体 |
| 46 | 开放击 2RR |

## 音域

A2(45) - A#2(46)

## 调音

踩镲为金属体鸣打击,键 45-46 选择开-闭击与开放击,不做音高校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/开放踩镲_奏法.events.json`;
渲染 4.75 s,峰值 0.419970,
RMS 0.029829,削波 0;
WAV SHA-256 `0ade9782…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

与闭合踩镲共用同一副镲片采样库的不同键位。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
