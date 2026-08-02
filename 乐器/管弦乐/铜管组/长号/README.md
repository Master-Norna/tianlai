**简体中文** | [English](README.en.md)

# 长号

基于 Virtual Playing Orchestra 3.3 的独奏长号 `formal`，直接使用 Iowa/VPO 多采样，不会静默回落到 GM SoundFont。

- 实际/管弦乐记谱音域：MIDI `40–77`，E2–F5，按实音输入；
- 奏法：`normal`/`sustain`、`slow_sustain`、`staccato`、`accent`；
- 连续控制：`expression`、`breath`、9 档起音 `modulation` 和 `sustain_pedal`；
- 力度：持续与短音均采用 VPO 两层映射；
- 音准：20 个持续音根样本逐一校准，支持任意 A4 和小数 MIDI 音高；
- 释放：使用 SFZ 包络与内嵌循环，无独立释音样本。

当前绑定版本的单音色试听已通过，因此为 `formal`。此入口不自动模拟滑管连续滑音；上游 LFO、EQ、随机微扰和连续力度交叉渐变也尚未完整复刻，滑音协议与协奏盲听仍待审。

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\长号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\长号\核验资源.py
```

试听事件位于 `examples/长号_奏法.events.json`；资源冻结见 `资源核验.json`。
