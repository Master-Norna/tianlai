# 定音鼓

严格 `CC0-1.0` 的 VCSL `1.2.2-RC` 专用多采样 `formal`。输入是实际
发声音高；不同奏法执行各自真实覆盖范围：

- `hit`：`Timpani 2 - Scale`，MIDI 38–59（D2–B3）。54 个 PCM24
  立体声 WAV、9 个实录音高组、3 个实录力度层、每层真实 RR2；
- `roll`：`Timpani 1 - Roll`，MIDI 41–55（F2–G3）。10 个 PCM16
  立体声 WAV、5 个实录音高组、2 个实录力度层、无 RR；
- 滚奏是 15.7–29.5 秒的自然有限录音，不含 WAV loop，也不会被宣称为
  无限滚奏；
- 单击保留上游力度交叉淡化和短起始 offset；40 个非零 offset 最多只移除
  对应样本峰值的 2.9%，没有一项达到 5%；
- 没有合成的音高、响度或延迟随机，没有把重复触发冒充实录 RR；
- `hit` 和 `roll` 来自同一 CC0 库的两套不同录音，可能存在音色接缝，已在
  机器报告中明确保留。

定音鼓是强非谐波乐器。本项目按冻结的 SFZ `pitch_keycenter + tune`
播放；低频谱模态只用于诊断，绝不把单个 FFT 峰写成虚假的自动音高校正。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/比较VCSL候选.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/定音鼓/乐器.json --events examples/定音鼓_奏法.events.json --output output/定音鼓_奏法_candidate.wav
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/定音鼓/核验试听.py
```

当前仍是 `formal`：逐样本 Hash、削波、尾音、offset、映射和端到端测试
已经自动核验；两套录音的音色衔接与完整音乐语境仍标记为协奏/长时语境待审。
