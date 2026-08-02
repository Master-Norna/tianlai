[中文](README.md) | [English](README.en.md)

# 合成器低音

默认 `synth_bass`（引擎 `1.0.0`，`formal`）组合 PolyBLEP 锯齿、可变脉宽方波和基频正弦，经软饱和与高谐振低通。快速、按指数衰减的滤波包络形成低音“咬字”，短 ADSR 适合节奏线。

- 校准音域：MIDI 24–72，硬边界。
- 控制：力度驱动音量，`expression` 连续控制，`modulation` 增加轻微音高/滤波运动，支持延音踏板。
- 可复现性：双声部固定种子 `3187682451`。
- 试听谱例：`examples/合成器低音_程序合成.events.json`。
- 状态：低频翻译与不同扬声器协奏/长时语境待审。

显式应急回退：GeneralUser GS bank `0` / program `38`；默认清单不加载 SoundFont。

