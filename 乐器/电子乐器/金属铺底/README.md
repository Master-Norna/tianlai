# 金属铺底

默认 `metallic_pad`（引擎 `1.0.0`，`formal`）采用非整数比 `√2` 调频、环形调制和调制器二次侧带，产生钟状非谐波谱；五声部微失谐和慢 ADSR 将瞬态金属谱扩展成铺底。

- 校准音域：MIDI 30–104，硬边界。
- 控制：力度、`expression`、`modulation`、`sustain_pedal`。
- 固定种子：`2449489742`。
- 试听谱例：`examples/金属铺底_程序合成.events.json`。
- 状态：协奏/长时语境待审。

显式应急回退：GeneralUser GS bank `0` / program `93`；默认实现无 SoundFont 依赖。

