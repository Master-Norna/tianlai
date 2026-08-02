**简体中文** | [English](README.en.md)

# 木鱼

VPO 3.3 / SSO 专用采样 `formal`。这是无固定音高打击，`low` 与 `high` 表示两块相对高低木鱼，不映射为十二平均律音符。

- 高低各 1 个实录 WAV；
- one-shot 完整尾音；
- 支持力度、平滑 `expression`；
- 音准报告明确为 N/A，不虚构 cents。

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/木鱼/乐器.json --events examples/木鱼_奏法.events.json --output output/木鱼_奏法.wav
```

当前绑定版本的单音色试听已通过，因此为 `formal`；每个木鱼仍只有一个样本且无 RR、槌头、滚奏或多尺寸扩展，扩展/协奏盲听仍待审。
