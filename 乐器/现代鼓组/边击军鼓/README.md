# 边击军鼓(formal)

VCSL 现代军鼓 cross-stick 边击专用入口。本目录是 98 项清单 SAM-30 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Membranophones/Struck Membranophones/Snare Drum, Modern 2.sfz`

默认奏法 `hit`;pitch_mode `fixed`。

## 键位映射

| MIDI 键 | 内容 |
| --- | --- |
| 62 | cross-stick 边击 2RR |

## 音域

见键位映射

## 调音

边击军鼓为膜鸣打击,任意谱面音高都触发 cross-stick 采样,不做音高校准(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/边击军鼓_奏法.events.json`;
渲染 5.50 s,峰值 0.420014,
RMS 0.011457,削波 0;
WAV SHA-256 `1f90d31b1a5b…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

上游仅 1 力度×2RR;完整军鼓击/滚奏见管弦小军鼓入口。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
