# 钟琴

VPO 3.3 / VSCO2-CE 专用多采样 `formal`，实际与记谱音域 F5–C8（MIDI 77–108）。

- 6 个根采样覆盖完整音域；
- 6/6 已生成 FFT 音准校准并用于播放倍率；
- one-shot 保留长尾，note-off 不截断；
- 上游 ±12 cents 音高、±1.5 dB 响度和 12 ms 延迟随机改成稳定哈希微扰；
- 支持 A4 改变与小数 MIDI 音高。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/钟琴/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/钟琴/乐器.json --events examples/钟琴_奏法.events.json --output output/钟琴_奏法.wav
```

当前绑定版本的单音色试听已通过，因此为 `formal`；仍只有一个录音力度，没有 RR、槌头或制音采样，宽音区移调、线性重采样与协奏盲听仍待复查。
