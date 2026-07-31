# 合唱啊声

基于 Virtual Playing Orchestra 3.3 / Sonatina Symphonic Orchestra 的男女混声持续音 `formal`。实现读取真实 Chorus WAV/SFZ，不再使用 GM Choir Aahs 代替。

## 当前能力

- 实音范围 G2–C6（MIDI 43–84）：男声覆盖 G2–F♯4，女声覆盖 G4–C6；
- `normal` 与 `sustain` 两套 SFZ 映射，共用 37 个逐音高 WAV；37 个 WAV 都使用嵌入循环；
- 保留上游力度到起音时间的连续关系；`normal` 奏法中 `modulation`（CC1 语义）可再将起音延长 0–1 秒；
- 保留 `0.84 s` hold、`22 s` decay 和 `70%` sustain 的逐声部包络；
- 支持 A4 基准、小数 MIDI/Hz 音高、力度、`expression`、`breath` 和延音踏板；
- 37 个根采样经过谐波约束 FFT 校准：中位偏差 `-2.609262 cents`，最大原始偏差 `26.900799 cents`；
- 中文/空格路径、资源缺失显式报错、确定性渲染均纳入自动测试。

## 名称边界

上游元数据只明确写了 `Choir/Chorus sustain`，没有提供可核验的元音标注。本目录沿用编制表中的“合唱啊声”，当前样本听感可作为 Ah 类持续铺底，但在人工逐样本确认前不声称它是严格统一发音的 `/ɑː/`。

## 使用

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/人声乐器/合唱啊声/乐器.json `
  --events examples/合唱啊声_奏法.events.json `
  --output output/合唱啊声_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 只有一套实录动态，没有 Round Robin、辅音、换气、单独元音、歌词或连音音素；
- 男/女声在 G4 附近相邻切换而非重叠交叉淡化；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；元音身份、完整能力与协奏盲听仍待审。

资源版本、许可与聚合 Hash 见 [来源.md](来源.md) 和 [资源核验.json](资源核验.json)。
