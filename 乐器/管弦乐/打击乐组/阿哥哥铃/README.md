**简体中文** | [English](README.en.md)

# 阿哥哥铃(formal)

VCSL 阿哥哥铃,高/低双铃力度分层。本目录是 98 项清单 SAM-38 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Idiophones/Struck Idiophones/Agogo Bells.sfz`

默认奏法 `hit`;pitch_mode `ignore`。

## 键位映射

| MIDI 键 | 内容 |
| --- | --- |
| 60 | 高铃 3 力度层 |
| 61 | 低铃 2 力度层 |

## 音域

C4(60) - C#4(61)

## 调音

阿哥哥铃为一对相对高低音金属铃,键 60 高铃、键 61 低铃,不伪造绝对音高校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/阿哥哥铃_奏法.events.json`;
渲染 7.00 s,峰值 0.420004,
RMS 0.019293,削波 0;
WAV SHA-256 `0e3cd966…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

无 RR;铃为一对相对音高。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
