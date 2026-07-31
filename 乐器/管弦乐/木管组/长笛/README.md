# 长笛

基于 Virtual Playing Orchestra 3.3 / VSCO 2 Community Edition 的独奏长笛，使用单声部气息状态机调度真实录音采样。

## 当前能力

- 10 个持续音根采样覆盖 C4–D7（MIDI 60–98）；
- 10 个持续采样均使用 WAV 内嵌循环和独立实测音准；
- `sustain`、`slow_sustain`、`legato`、`staccato`、`accent` 五种奏法；
- 持续音重叠时自动切换为 8 ms 连奏起音，旧音使用 55 ms 交叉淡出；
- 新音会再次缩短已在收尾的旧音，避免 0.7 秒长尾造成假复音；
- 10 个吐音采样；上游 G4/G♯4 的两个重叠区域按 SFZ 语义同时分层发声；
- 重音按上游映射叠加吐音与持续音，持续层保留每个根音 40–120 ms 的独立延迟；
- 重音在延迟期间被松开或新音中断时，延迟层不会事后冒出；
- `expression` 和 `breath` 均为 0–1 的平滑连续控制；
- 采样按需解码、采样点精确调度和确定性渲染。

## 演奏事件

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.8 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 69, "velocity": 0.76 }
{ "time": 1.2, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

谱面可用约 100–200 ms 的音符重叠表达连奏；即使当前奏法仍为 `sustain`，重叠的新音也会自动改用内部 `legato` 发声。

## 尚未冒充“100% 还原”的部分

- `breath` 目前是独立的气息响度包络，不是真实的气流噪声或多层音色渐变；
- 没有独立呼吸/收气尾音采样，目前使用原采样的 0.7 秒释放近似；
- 颤音已录在持续采样中，无法独立去除或调整速率/深度；
- `legato` 是短起音与交叉淡化近似，上游没有真实连奏过渡采样；
- 上游 SFZ 的三组高通近似 EQ 尚未实现；
- 上游微小随机音高、响度和延迟默认关闭，以优先保证音准与可重现性；
- 核心采样器仍使用线性重采样。

## 音准验证

实测持续音 A4 渲染结果约为 `440.015 Hz / +0.061 cents`。

可重新生成 10 个根采样的校准表：

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/长笛/校准音准.py
```

单音验证谱例为 `examples/长笛_A4_音准.events.json`。

## 音源与渲染

授权与来源见 [来源.md](来源.md)。如果根目录 `音源/VirtualPlayingOrchestra` 已存在，无需重复下载；否则运行：

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/管弦乐/木管组/长笛/获取音源.ps1
```

渲染综合示例：

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/长笛/乐器.json `
  --events examples/长笛_奏法.events.json `
  --output output/长笛_奏法.wav
```
