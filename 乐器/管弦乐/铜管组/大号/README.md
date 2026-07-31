# 大号

基于 Virtual Playing Orchestra 3.3 的独奏大号 `formal`，使用专用 SSO/VPO WAV 多采样，不会静默回落到 GM SoundFont。

- 实际发声音域：MIDI `26–62`，D1–D4；
- 记谱语义：管弦乐低音谱表按实音输入，`concert = written`；铜管乐队中可能出现的移调记谱不在此入口自动处理；
- 奏法：`normal`/`sustain`、`slow_sustain`、`staccato`、`accent`；
- 连续控制：`expression`、`breath`、9 档起音 `modulation` 和 `sustain_pedal`；
- 采样：9 个循环持续音根样本、12 个短音样本；短音按确定性门限释音；
- 音准：9 个持续音根样本逐一校准，支持文档级 A4 和小数 MIDI 音高；
- 释放：SFZ 包络/循环，无独立释音样本。

当前绑定版本的单音色试听已通过，因此为 `formal`。VPO 的滤波、随机微扰和完整包络尚未全部复刻；连续力度与协奏盲听仍待更细验收。

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\大号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\大号\核验资源.py
```

试听事件位于 `examples/大号_奏法.events.json`；资源冻结见 `资源核验.json`。
