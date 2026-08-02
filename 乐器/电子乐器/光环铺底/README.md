[中文](README.md) | [English](README.en.md)

# 光环铺底

当前默认实现是天籁确定性程序合成器 `halo_pad`（引擎 `1.0.0`，质量级别 `formal`）。七声部宽立体声正弦群经过低速滤波 LFO；二、三次泛音和轻微相位调制形成高空“光环”，慢起音与长释音用于持续和声。

- 校准音域：MIDI 30–108；超界事件会明确报错。
- 演奏响应：力度控制起始能量，`expression` 控制连续音量，`modulation` 加深颤音与滤波运动，支持 `sustain_pedal`。
- 可复现性：固定种子 `1742049361`；相同清单、事件、采样率与引擎版本产生逐样本一致输出。
- 试听谱例：`examples/光环铺底_程序合成.events.json`。
- 当前绑定版本的单音色试听已通过，因此为 `formal`；长时运动与混音场景审查仍待完成。

通用 SoundFont 只保留为显式应急回退：GeneralUser GS，bank `0`、program `94`。需要回退时应另行选择 SoundFont 清单；本清单不会静默降级。

