# 铜管合奏

基于 Virtual Playing Orchestra 3.3 `all-brass-SEC` 的四声部铜管合奏 `formal`。它同时调度大号、圆号组、长号组和小号组，并复现 SFZ 的键位交叉渐变；不会把整套合奏错误地当成随机单样本，也不会静默回落到 GM SoundFont。

- 实际/输入音域：MIDI `26–84`，D1–C6；合奏入口统一使用实音；
- 层次：低音到高音依次按 VPO 映射在 D1–D2、B1–F3、E2–C5、F#3–C6 间作等功率近似交叉渐变；重叠区会真正叠加声部；
- 奏法：`normal`/`sustain`、`slow_sustain`、`staccato`、`accent`；
- 连续控制：`expression`、`breath`、9 档起音 `modulation` 和 `sustain_pedal`；
- 音准：76 个持续音根样本逐一 FFT 校准；持续/短音合计冻结 88 个去重 WAV；
- 释放：SFZ 循环和包络，无独立释音样本。

当前绑定版本的单音色试听已通过，因此为 `formal`。四个子库的力度层数不一致，当前在各自上游交叉渐变中点作确定性离散层；SFZ 的 EQ/LFO/滤波、随机微扰、编制平衡与协奏盲听仍待更细验收。

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\铜管合奏\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\铜管合奏\核验资源.py
```

试听事件位于 `examples/铜管合奏_奏法.events.json`；混合许可和资源冻结见 `来源.md`、`资源核验.json`。
