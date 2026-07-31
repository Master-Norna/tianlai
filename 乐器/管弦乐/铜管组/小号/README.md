# 小号

基于 Virtual Playing Orchestra 3.3 的独奏小号 `formal`，直接使用 Iowa/VPO 的两力度层独奏采样，不会静默回落到 GM SoundFont。

- 实际发声音域：MIDI `54–84`，F#3–C6；
- 记谱语义：事件输入实音。B♭小号谱面音比实音高大二度，`concert = written - 2`；对应记谱范围 G#3–D6；
- 奏法：`normal`/`sustain`、`slow_sustain`、`staccato`、`accent`；
- 连续控制：`expression`、`breath`、9 档起音 `modulation` 和 `sustain_pedal`；
- 力度：54 个持续根采样构成两层；断奏由相同素材的上游短音包络候选实现；
- 音准：54 个持续音根样本逐一校准，支持任意 A4 和小数 MIDI 音高；
- 释放：使用 SFZ 包络与内嵌循环，无独立释音样本。

当前不完整复刻上游 EQ、LFO、随机微扰和连续交叉渐变。单音色状态为
`formal`，协奏状态为 `untested`。

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\小号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\小号\核验资源.py
```

试听事件位于 `examples/小号_奏法.events.json`；资源冻结见 `资源核验.json`。
