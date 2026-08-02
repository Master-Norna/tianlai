[中文](README.md) | [English](README.en.md)

# 合唱铺底

当前默认实现是确定性 `choir_pad`（引擎 `1.0.0`，`formal`）。三声部带限锯齿/正弦激励三个稳定状态变量带通共振峰（约 690、1170、2680 Hz），模拟元音腔体，再经低通与轻微合唱漂移整形；它不是把普通铺底换个名称。

- 校准音域：MIDI 36–104，硬边界。
- 控制：力度、`expression`、`modulation`、`sustain_pedal`；调制会同时加深颤音和共振峰前级的滤波运动。
- 可复现性：固定种子 `275438921`。
- 试听谱例：`examples/合唱铺底_程序合成.events.json`。
- 状态：机器回归通过后仍须人工判断元音自然度，试听待审。

应急回退映射为 GeneralUser GS bank `0` / program `91`，只在调用方显式选用 SoundFont 时生效，不自动降级。

