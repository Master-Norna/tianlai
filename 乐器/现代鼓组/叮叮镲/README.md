# 叮叮镲(formal)

VCSL 悬吊镲 1:镲帽与棒尖击作叮叮镲(ride)。本目录是 98 项清单 SAM-24 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Idiophones/Struck Idiophones/Suspended Cymbal 1.sfz`

默认奏法 `hit`;pitch_mode `ignore`。

## 键位映射

| MIDI 键 | 内容 |
| --- | --- |
| 69 | 镲帽 bell 击 3 力度层 |
| 70 | 棒尖 stick 击 3 力度层 |
| 71 | 滚奏 3 力度层 |

## 音域

A4(69) - B4(71)

## 调音

悬吊镲为金属体鸣打击,键 69-71 选择镲帽/棒尖/滚奏,不做音高校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/叮叮镲_奏法.events.json`;
渲染 6.25 s,峰值 0.419998,
RMS 0.014457,削波 0;
WAV SHA-256 `257a5f7e12b3…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

悬吊镲代演 ride;专用 ride 镲片素材待后续替换。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
