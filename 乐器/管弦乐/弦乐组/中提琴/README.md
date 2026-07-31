# 中提琴

当前实现是 **VSCO2-CE 中提琴声部** `formal`，不是独奏中提琴。运行时严格限制在 `libs/VSCO2-CE/Strings/Viola Section` 的纯 CC0 子树，不再读取 No Budget Orchestra、SSO、Mattias Westlund 或 GM fallback。

## 已实现的真实结构

- `sustain`：12 个 `susvib` 根采样，MIDI 50–86；每根只有一个保留的 `v2` 录制层和一个 take，使用 WAV 内嵌循环。
- `spiccato`：12 个采样根、每根真实 RR1/RR2，共 24 个短音 WAV；每根同样只有一个 `v2` 录制力度。
- 36 个采样都是 44.1 kHz 双声道 WAV；33 个 PCM16、3 个 PCM24；没有削波或静音文件。
- 演奏映射为 MIDI 48–93（C3–A6）。持续音最低 C3 由 D3 根降 2 半音；MIDI 85–93 共用 D6 根，其中 A6 是上延映射，不是独立根采样。
- `velocity` 只连续控制响度，不会虚构第二个音色力度层。
- 36 个采样全部使用三窗口实测音准；运行时按每文件结果反向校正。

## 事件

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 60, "velocity": 0.8 }
{ "time": 1.5, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

短弓只接受准确名称：

```json
{ "time": 2.0, "type": "articulation", "name": "spiccato" }
```

`staccato`、`pizzicato`、`accent`、`tremolo` 和 `slow_sustain` 都不会被悄悄近似。

## 复算

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/中提琴/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/中提琴/校准音准.py
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/中提琴/核验试听.py
```

固定试听输出到 `output/中提琴_VSCO2_CC0_candidate.wav`，覆盖低音、真实根、上延音域、持续循环、两个短弓 RR、响度响应和 expression。

## 单音色 formal 的已知边界

- 没有真实多力度、非振音持续音、连奏过渡、独立释音、拨奏、重音或颤弓。
- `susvib` 是声部振音，不能当作独奏或无振音中提琴。
- 持续 WAV 的循环接缝最大样本差约 `0.0603`；已纳入资源门和长音试听，但当前采样器没有循环交叉淡化。
- 顶部七个半音依赖 D6 根的移调，音色精度低于有独立根采样的区域。
- 当前绑定版本的单音色试听已通过，因此为 `formal`；线性重采样、扩展音域与协奏盲听仍待更细验收。

许可与逐文件证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [音准校准.json](音准校准.json)。
