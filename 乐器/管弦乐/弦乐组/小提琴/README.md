# 小提琴

基于 Virtual Playing Orchestra 3.3 的第一独奏小提琴，直接读取上游 SFZ 区域映射和 WAV 内嵌循环点。

## 当前能力

- 上游 SFZ 映射键区覆盖 G3–A7（MIDI 55–105），但 SOLO 与 SEC 的持续音
  最高原生根采样都只到 B♭6（MIDI 94）；
- 30 个持续音根采样拥有独立的实测音准表，不盲从上游的粗粒度偏移；
- `sustain`、`slow_sustain`、`staccato`、`pizzicato`、`tremolo`、`accent` 六种奏法；
- 断奏保留上游的两组 Round Robin，且选择可重现；
- 持续音和颤弓使用 WAV `smpl` 内嵌循环，不会在采样末尾突然中断；
- `expression` 控制值在 0–1 之间，内部平滑过渡；
- 采样按需解码，不在启动时读入整套交响乐音源；
- 采样点精确事件调度和确定性渲染。

## 核心与扩展音域

`range_profiles` 把“还能按上游映射出声”和“当前音源可作为高仿候选”分开：

- MIDI 55–94 是默认 SOLO + `sustain` 的当前高仿候选核心区；
- MIDI 95–105 是物理/映射扩展区，全部由同一枚 B♭6 采样逐步升调，A7 已达到
  `+11` 半音；兼容模式仍可用于明确需要极端音区的压力测试，但不能据此宣称
  高仿；
- SEC、其他奏法或改变 `release_seconds` 后不会借用 SOLO + `sustain` 的结论，
  在各自完成取证前保持未审核。

这份范围目前是 `contract_candidate`，不是人工批准结论。普通作品常用区不会因为
扩展区降级而失效。

## 事件用法

奏法事件会影响之后开始的音符，已经发声的音符保持原奏法。

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 69, "velocity": 0.8 }
{ "time": 1.0, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

## 尚未冒充“100% 还原”的部分

- `expression` 当前连续改变响度，但这套独奏采样没有可供连续交叉渐变的多层力度音色；
- 录音本身已含颤音，尚不能独立控制颤音的延迟、速率和深度；
- `accent` 是断奏瞬态与持续音的确定性分层；
- 连奏目前依靠较快起音和重叠音符，尚无真实换弓/换弦过渡采样；
- 上游 SFZ 的微小随机音高、响度和延迟尚未启用；目前 Round Robin 调度可重现，而这两组断奏映射实际引用了同一组波形；
- 核心采样器仍是线性重采样，后续会换成带限重采样。
- B♭6 以上缺少新的原生持续根采样；在补齐合法音源或完成更高质量重映射前，
  MIDI 95–105 只保留为明确标注的扩展风险区。

## 音准验证

实测持续音 A4 渲染结果约为 `440.013 Hz / +0.051 cents`。如果更换了上游采样，可重新生成校准表：

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/小提琴/校准音准.py
```

单音验证谱例为 `examples/小提琴_A4_音准.events.json`。

音源与授权见 [来源.md](来源.md)。

## 获取音源

音源资产不进入项目代码版本控制。新环境运行：

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/管弦乐/弦乐组/小提琴/获取音源.ps1
```

脚本使用断点续传，并把波形与 SFZ 合并到根目录 `音源/VirtualPlayingOrchestra`，以便后续木管、铜管和其他弦乐器复用，无需重复下载。

## 渲染示例

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/小提琴/乐器.json `
  --events examples/小提琴_奏法.events.json `
  --output output/小提琴_奏法.wav
```
