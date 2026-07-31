# 颤弓弦乐

这是 VPO `all-strings-SEC-tremolo.sfz` 的专用颤弓入口；默认且唯一奏法是 `tremolo`，不会用普通持续音或合成器替代。

- 实音/记谱范围 C1–A7（MIDI 24–105）；
- 116 个映射区域、96 个去重 WAV；中提琴与小提琴分别保留两个同时发声的真实来源层；
- 大提琴及中高音的 90 个区域读取 WAV 内嵌循环；26 个低音提琴区域按有限录音自然结束；
- 96 个颤弓根采样单独做谐波约束校准，中位偏差 `-0.622 cents`，最大原始偏差 `51.890 cents`；
- 支持 A4、小数 MIDI/Hz 音高、力度、expression、持续踏板释放和确定性渲染。

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/颤弓弦乐/乐器.json `
  --events examples/颤弓弦乐_奏法.events.json `
  --output output/颤弓弦乐_奏法_candidate.wav
```

## 单音色 formal 的已知限制

- 上游颤弓没有序列 RR 与多力度层，力度以确定性振幅响应实现；
- 低音提琴录音没有循环，超长音需要未来制作经人工审听的无缝循环；
- 原映射随机微调、EQ 和所有包络调制尚未完全复现；没有 tremolo 速率控制、近桥/指板、独立释弦；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；线性重采样、扩展能力与协奏盲听仍待审。

资源证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [试听核验.json](试听核验.json)。
