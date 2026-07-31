# 单簧管

基于 Virtual Playing Orchestra Standard 3.3 / Wave 3.2 的独奏单簧管 `formal`。默认直接读取 SOLO SFZ 与 WAV 多采样，不会静默回落到 GM SoundFont。

## 当前能力

- 事件使用**实音**：采样实音域 D3–B♭6（MIDI 50–94），B♭ 单簧管记谱音域 E3–C7（52–96），换算为 `实音 = 记谱音 - 2 半音`；
- 26 个持续音区域、2 个实录力度层，当前在归一化力度 `0.622` 处分为确定性的离散层；
- `sustain`、`slow_sustain`、`staccato`、`accent` 四种奏法；accent 按上游 SFZ 同时触发短音攻击层和持续层；
- 26 个 WAV 均使用内嵌循环；断奏按上游映射由持续样本的短包络构成；
- 26 个根采样全部实测校准，中位偏差 `-0.035 cents`，原采样最大绝对偏差 `0.320 cents`；
- `expression` 与 `breath` 均有平滑控制，独奏状态机在换音时执行短交叉释放；
- 中文与空格 Windows 路径可直接加载，采样按需解码，渲染逐字节可重复。

## 事件约定

```json
{ "time": 0.0, "type": "articulation", "name": "slow_sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.65 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 69, "velocity": 0.8 }
{ "time": 1.4, "type": "note_off", "note_id": 1 }
```

`midi_note` 始终是实音；未来协作层读取 `written_to_sounding_semitones=-2` 后负责把记谱音转换为实音。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/单簧管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/单簧管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/单簧管/乐器.json `
  --events examples/单簧管_奏法.events.json `
  --output output/单簧管_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 上游两个力度层原本连续交叉渐变，当前采样器按中点离散选择；没有独立 Round Robin；
- 断奏是持续采样的包络塑形，不是独立断奏录音；没有真连奏过渡、换气、按键噪声或独立释音；
- `expression` / `breath` 当前控制平滑响度，尚未做连续音色变形；
- 为保证确定性，未采用 SFZ 的随机音高、响度和延迟；当前仍使用线性重采样；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；人工 A/B、扩展能力与协奏盲听仍待审。

资源冻结与混合授权见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [试听核验.json](试听核验.json)。
