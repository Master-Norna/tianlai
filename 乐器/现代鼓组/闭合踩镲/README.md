# 闭合踩镲(formal)

VCSL 踩镲闭合侧:闭合击/半开击/踏板闭合。本目录是 98 项清单 SAM-31 的专用实现,渲染引擎复用
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
| 42 | 闭合击 4 力度层×2RR |
| 43 | 半开击 2RR |
| 44 | 踏板闭合 2RR |

## 音域

F#2(42) - G#2(44)

## 调音

踩镲为金属体鸣打击,键 42-44 选择闭合击/半开击/踏板闭合,不做音高校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/闭合踩镲_奏法.events.json`;
渲染 6.25 s,峰值 0.420002,
RMS 0.016632,削波 0;
WAV SHA-256 `facf65c9…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

与开放踩镲共用同一副镲片采样库的不同键位。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
