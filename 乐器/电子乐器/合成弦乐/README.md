# 合成弦乐

默认 `synth_strings`（引擎 `1.0.0`，`formal`）使用七声部带限锯齿/脉冲弦群、极低幅固定种子弓噪声、较慢起音和合奏颤音。键位跟踪滤波抑制高音刺耳，同时保留合成弦乐的持续亮度。

- 校准音域：MIDI 36–100，硬边界。
- 控制：力度、平滑 `expression`、`modulation`、`sustain_pedal`。
- 固定种子：`1618033988`。
- 试听谱例：`examples/合成弦乐_程序合成.events.json`。
- 状态：协奏/长时语境待审；它是明确的合成弦乐，不冒充真实弦乐采样。

显式应急回退：GeneralUser GS bank `0` / program `50`。

