# 合成器主音

默认 `synth_lead`（引擎 `1.0.0`，`formal`）把 PolyBLEP 脉冲、锯齿与二倍频相位调制正弦混合，经高谐振滤波和轻驱动得到前置主音。快速起音、短释音与较深可控颤音适合旋律，而非铺底参数改名。

- 校准音域：MIDI 36–108，支持浮点 MIDI 和直接 Hz，但严格执行边界。
- 控制：力度、`expression`、`modulation`、`sustain_pedal`。
- 固定种子：`2718281828`。
- 试听谱例：`examples/合成器主音_程序合成.events.json`。
- 状态：协奏/长时语境待审。

显式应急回退：GeneralUser GS bank `0` / program `81`；默认不加载通用音源。

