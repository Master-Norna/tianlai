# 大提琴

基于 Virtual Playing Orchestra 3.3 的独奏大提琴，直接读取上游 SFZ 区域、WAV 内嵌循环和独立释弦采样。

## 当前能力

- 真实录音采样覆盖 C2–A5（MIDI 36–81）；
- 9 个持续音根采样均有内嵌循环和独立实测音准；
- `sustain`、`slow_sustain`、`staccato`、`pizzicato`、`accent` 五种奏法；
- 48 个断奏区域保留两组真实 Round Robin 弓向变化；
- 21 个拨弦区域；
- 10 个释弦尾音区域，松开持续音时按音域独立触发；
- `expression` 在 0–1 之间连续控制响度，并使用平滑过渡；
- 采样按需解码、采样点精确调度、确定性渲染。

## 奏法事件

```json
{ "time": 0.0, "type": "articulation", "name": "slow_sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.7 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 48, "velocity": 0.8 }
{ "time": 1.5, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

奏法事件只影响之后开始的音符，已经发声的音符保持原奏法。

## 音准验证

实测持续音 A3 渲染结果约为 `220.028 Hz / +0.220 cents`。如果更换了上游采样，可重新生成校准表：

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/大提琴/校准音准.py
```

## 尚未冒充“100% 还原”的部分

- `expression` 当前连续改变响度，但这套独奏采样没有多层动态音色可供交叉渐变；
- 录音本身含颤音，尚不能独立改变颤音速率和深度；
- 连奏使用快起音、重叠音符和释弦尾音近似，尚无真实换弦/换弓过渡采样；
- 当前音源没有独奏大提琴颤弓、泛音、近马/近指板等专用采样；
- 上游的微小随机音高、响度与延迟尚未启用；
- 核心采样器仍使用线性重采样。

音源与授权见 [来源.md](来源.md)。

## 获取音源

如果根目录 `音源/VirtualPlayingOrchestra` 已由小提琴下载，无需重复执行。新环境可运行：

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/管弦乐/弦乐组/大提琴/获取音源.ps1
```

## 渲染示例

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/大提琴/乐器.json `
  --events examples/大提琴_奏法.events.json `
  --output output/大提琴_奏法.wav
```
