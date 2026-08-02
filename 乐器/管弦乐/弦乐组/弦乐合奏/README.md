**简体中文** | [English](README.en.md)

# 弦乐合奏

基于 Virtual Playing Orchestra 3.3 的四声部弦乐合奏 `formal`。实现直接读取 `all-strings-SEC-*` 的真实 WAV/SFZ，不会静默回落到 GM。

## 当前能力

- 实音输入、实音记谱，采样映射覆盖 C1–A7（MIDI 24–105）；
- 分别渲染低音提琴、大提琴、中提琴、小提琴四个真实声部，并执行上游 C2–B2、C3–F3、G3–C6 等功率交叉淡化；
- `sustain`、`staccato`、`pizzicato`、`tremolo`、`accent` 五套真实 SFZ 奏法；
- 64 个持续音区域和循环，断奏/重音弓头保留两组确定性 Round Robin；
- 中提琴持续音和颤弓、小提琴颤弓中的独立采样库作为同时发声层，而非错误地当作 RR；
- 64 个持续根采样使用谐波约束 FFT 校准，中位偏差 `-0.306 cents`，最大原始偏差 `22.954 cents`；
- 支持 A4 基准、小数 MIDI/Hz 音高、力度、`expression` 平滑和持续音踏板释音；
- Windows 中文/空格路径、资源缺失显式报错、确定性渲染均有自动测试。

## 使用

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/弦乐合奏/校准音准.py

.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/弦乐合奏/乐器.json `
  --events examples/弦乐合奏_奏法.events.json `
  --output output/弦乐合奏_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 持续、拨奏和大多数颤弓只有一个实录力度层；力度目前以振幅曲线响应，断奏大提琴的两层交叉区被离散选择；
- 原 SFZ 的随机音高、响度、延迟被禁用以保证可重复；当前没有独立释弦、真实连奏过渡、换弓、近马、泛音；
- 26 个低音提琴颤弓 WAV 没有循环元数据，会按录音自然长度结束；其余 90 个颤弓区域使用嵌入循环；
- SFZ EQ、速度到 attack/release 的连续调制尚未完整建模，重采样仍为线性；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；扩展能力、编制平衡与协奏盲听仍待审。

资源版本、许可与聚合 Hash 见 [来源.md](来源.md) 和 [资源核验.json](资源核验.json)。
