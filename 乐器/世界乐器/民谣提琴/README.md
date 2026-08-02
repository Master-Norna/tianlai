[中文](README.md) | [English](README.en.md)

# 小提琴·民谣演奏风格(formal)

本入口是“小提琴的民谣演奏风格”，不是另一种声学实体。它复用 VPO
`2nd-violin-SOLO` 对 No Budget Orchestra 独奏小提琴素材的重新映射，再由可追踪
的奏法包络提供更敏捷的 fiddle 弓法。渲染引擎复用
`tianlai/dedicated_sfz.py`，不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:Virtual Playing Orchestra 3(Standard 3.3 / Wave 3.2)
- 版本:Standard 3.3 / Wave 3.2,许可:混合公开许可:SSO Sampling Plus、No Budget Orchestra/Mattias CC-BY-SA、VSCO2 CC0 等,见 Documentation/license.htm
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `fiddle`：与 `sustain` 使用同一上游 SFZ，但显式采用 `20 ms` 起音和
  `120 ms` 释音；它是新的默认奏法，适合快速、干净的民谣乐句；
- `sustain`：原始慢抒情奏法，保留上游 `300 ms` 起音和 `1.6 s` 尾音，旧乐谱
  显式写 `sustain` 时声音不变；
- `staccato`:`Strings/2nd-violin-SOLO-staccato.sfz`
- `pizzicato`:`Strings/2nd-violin-SOLO-pizzicato.sfz`
- `accent`:`Strings/2nd-violin-SOLO-accent.sfz`

默认奏法 `fiddle`;pitch_mode `pitched`。包络覆盖写在受版本控制的清单中，不改
上游 SFZ/WAV；需要旧版慢抒情质感时应显式选择 `sustain`。清单同时声明
`articulation_auto_default: false`：协作层省略奏法时保留 `fiddle`，不会再因短音
启发式自动换成 `accent`；编制表若显式覆盖该策略，最终值仍会写入执行计划。

## 音域

G3(55) - G6(91)

## 调音

92 个根采样谐波 FFT 诊断;实测中位 +2.554 c,上游映射后残差中位 +3.114 c,最大残差 43.331 c(详见 [`音准校准.json`](音准校准.json))

## 试听

当前全音域固定事件覆盖 MIDI 55–91；时长、峰值、RMS、削波和 WAV Hash 见
[`试听核验.json`](试听核验.json)，复算脚本见 [`核验试听.py`](核验试听.py)。
其中 `human_review=pending` 只表示没有单独记录扩展盲听结果，协奏状态仍为
`untested`。

## 已知限制

民谣风格靠奏法组与乐句表达；当前没有独立录制的 fiddle 音源，也没有民谣特有
滑音、双音持续或换弓过渡采样。快速包络改善演奏行为但不会把底层录音变成另一把
琴。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
