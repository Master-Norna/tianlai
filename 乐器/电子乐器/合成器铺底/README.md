[中文](README.md) | [English](README.en.md)

# 合成器铺底

默认 `broad_pad`（引擎 `1.0.0`，`formal`）使用六声部带限超锯齿、基频正弦和二次泛音，宽立体声分布后进入带键位跟踪的缓慢低通。它强调宽度和和弦密度，与光环、温暖、扫频铺底使用不同源拓扑与包络。

- 校准音域：MIDI 24–108，硬边界。
- 控制：力度、平滑 `expression`、`modulation`、`sustain_pedal`。
- 固定种子：`1414213562`。
- 试听谱例：`examples/合成器铺底_程序合成.events.json`。
- 状态：协奏/长时语境待审。

显式 SoundFont 回退为 GeneralUser GS bank `0` / program `88`；不会静默替换当前实现。

