[中文](README.md) | [English](README.en.md)

# 温暖铺底

默认 `warm_pad`（引擎 `1.0.0`，`formal`）以正弦为主体，混合少量带限锯齿和二次泛音，经温和饱和、低截止滤波与五声部窄失谐获得圆润主体。滤波移动小于扫频铺底，泛音也少于宽铺底。

- 校准音域：MIDI 24–108，硬边界。
- 控制：力度、平滑 `expression`、`modulation`、`sustain_pedal`。
- 固定种子：`3141592653`。
- 试听谱例：`examples/温暖铺底_程序合成.events.json`。
- 状态：协奏/长时语境待审。

显式应急回退：GeneralUser GS bank `0` / program `89`；默认清单不加载 SoundFont。

