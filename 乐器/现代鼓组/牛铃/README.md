# 牛铃

基于 Virtual Playing Orchestra / VSCO2 Community Edition 的真实独立 Cowbell `formal`。它使用 `misc.sfz` 中 MIDI 56 的四个 Cowbell WAV，不再走 General MIDI 鼓组，也不会误用相邻的 Agogo。

## 当前能力

- 2 个力度层 × 2 个 Round Robin，共 4 个真实牛铃 WAV；
- 软层在力度 54–104 淡出、硬层在同一区间淡入，Round Robin 对两个层全局同步；
- 保留上游 `amp_random=1 dB`，但随机值由事件确定性派生，可重复渲染；
- 固定一击自然衰减，谱面 `note_off` 不会截断金属尾音；支持 `expression`；
- MIDI 56 仅是 SFZ 触发键。牛铃属于无固定音高打击，不伪造十二平均律音高校准。

## 使用

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/现代鼓组/牛铃/乐器.json `
  --events examples/牛铃_奏法.events.json `
  --output output/牛铃_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 只有一种 Cowbell 与一种击法，没有开/闷、边击、阻尼、多个尺寸或可控音高；
- 上游未提供 choke/group 关系；当前按录音完整 one-shot 播放；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；扩展能力与协奏盲听仍待审。

资源版本、许可与聚合 Hash 见 [来源.md](来源.md) 和 [资源核验.json](资源核验.json)。
