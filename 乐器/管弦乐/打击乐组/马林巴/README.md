**简体中文** | [English](README.en.md)

# 马林巴(formal)

VCSL 马林巴专用多采样(soft/med/loud 三力度交叉渐变)。本目录是 98 项清单 ORP-11 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `hit`:`Idiophones/Struck Idiophones/Marimba.sfz`

默认奏法 `hit`;pitch_mode `pitched`。

## 音域

F2(41) - C#7(97)

## 调音

30 个根采样谐波 FFT 诊断;实测中位 -0.144 c,上游映射后残差中位 -0.144 c,最大残差 142.046 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/马林巴_奏法.events.json`;
渲染 10.15 s,峰值 0.419954,
RMS 0.026419,削波 0;
WAV SHA-256 `aa87f4fd…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

单 RR;上游 --notuning 生成,校准仅诊断;无滚奏采样。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
