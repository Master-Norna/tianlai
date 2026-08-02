**简体中文** | [English](README.en.md)

# 木琴

VPO 3.3 / No Budget Orchestra 专用多采样 `formal`。

- API 输入始终是实音 C4–C8（MIDI 60–108）；
- 传统乐谱写低一个八度 C3–C7，协作层需加 12 半音；
- 15 个根音区 × 2 RR，共 30 个 WAV；默认未写的 SFZ `seq_position` 被正确视为 RR1；
- 30 个采样均有 FFT 校准，支持 A4 改变和小数 MIDI 音高；
- 短音按完整 WAV one-shot 播放，note-off 不截尾。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/木琴/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/木琴/乐器.json --events examples/木琴_奏法.events.json --output output/木琴_奏法.wav
```

当前绑定版本的单音色试听已通过，因此为 `formal`；仍只有单一录音力度，没有不同槌头、滚奏或制音采样，高低边界与更高阶重采样留待扩展/协奏验收。
