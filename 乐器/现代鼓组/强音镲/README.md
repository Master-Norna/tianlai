# 强音镲(formal)

VCSL 悬吊镲 2:5 力度层槌击+三档渐强滚奏作强音镲。本目录是 98 项清单 SAM-27 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Idiophones/Struck Idiophones/Suspended Cymbal 2.sfz`

默认奏法 `hit`;pitch_mode `ignore`。

## 键位映射

| MIDI 键 | 内容 |
| --- | --- |
| 63 | 2.5s 渐强滚奏 |
| 64 | 4s 渐强滚奏 |
| 65 | 7s 渐强滚奏 |
| 66 | 槌击 5 力度层(pp-fff) |

## 音域

D#4(63) - F#4(66)

## 调音

悬吊镲为金属体鸣打击,键 63-66 选择渐强滚奏与 5 力度层槌击,不做音高校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/强音镲_奏法.events.json`;
渲染 5.50 s,峰值 0.419998,
RMS 0.056748,削波 0;
WAV SHA-256 `365cb0a5…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

悬吊镲槌击非鼓棒 crash;渐强为定长实录。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
