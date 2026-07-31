# 圆号

基于 Virtual Playing Orchestra 3.3 的独奏圆号 `formal`。默认直接读取 VPO 的独立 WAV 多采样，不会静默回落到 GM SoundFont。

- 实际发声音域：MIDI `35–77`，B1–F5；越界立即报错，不拉伸冒充；
- 记谱语义：事件统一输入实音。F 调圆号谱面音比实音高纯五度，`concert = written - 7`；对应记谱范围 F#2–C6；
- 奏法：`normal`/`sustain`、`slow_sustain`、`staccato`、`accent`；
- 连续控制：`expression`、`breath` 平滑控制响度；`modulation` 以 9 档控制后续长音的起音长度；`sustain_pedal` 保留已松键长音；
- 力度：VPO 持续/断奏两层，当前在上游交叉渐变中点作确定性离散分层；
- 音准：39 个持续音根采样逐一 FFT 测量，以 A4=440 Hz 记录；渲染仍服从演奏文档的 A4 基准和小数 MIDI 音高；
- 释放：使用 SFZ 包络与内嵌循环，无独立释音样本。

当前绑定版本的单音色试听已通过，因此为 `formal`。上游 SFZ 的随机音高、随机
音量和随机延迟没有启用，以保证重复渲染逐字节一致；当前不包含上游 EQ/LFO、
真正的连续力度交叉渐变或协奏盲听结论，不能据此宣称 100% 复刻。

复现校准与资源核验：

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\圆号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\圆号\核验资源.py
```

试听事件位于 `examples/圆号_奏法.events.json`。来源、许可边界和固定 Hash 分别见 `来源.md`、`资源核验.json`。
