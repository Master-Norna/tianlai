# 低音提琴

基于 Virtual Playing Orchestra 3.3 的独奏低音提琴 `formal`，直接读取 VPO SFZ 和独立 WAV 多采样，不会静默回落到 GM SoundFont。

## 当前能力

- 采样实际发声音域 C1–G4（MIDI 24–67），低音提琴记谱高八度为 C2–G5；
- 12 个持续音区域，全部保留 WAV 内嵌循环；
- `sustain`、`slow_sustain`、`staccato`、`pizzicato`、`accent` 五种奏法；
- 22 个断奏区域保留两组确定性 Round Robin，21 个独立拨弦区域；
- `accent` 依照上游映射叠加弓头攻击和延迟 `120 ms` 的持续层；
- 12 个持续根采样已生成独立音准表，中位偏差 `+0.210 cents`，最大绝对偏差 `2.701 cents`；
- MIDI 39–40 使用的 E1 根采样在固定力度孤立渲染中比相邻区低约
  `6–7 dB`；清单通过资产相对路径记录 `+6.25 dB` 的可复现配平，并把该规则
  显式限定为 `SOLO` 变体，不修改上游 SFZ 或 WAV；合法 `SEC` 变体不会误用
  SOLO 路径，同变体内写错路径仍拒绝加载；
- `expression` 平滑控制响度，`sustain_pedal` 在持续音松开后延迟释放；
- VPO 未加引号的 `Solo Contrabass` 空格路径已用专用解析器兼容 Windows 中文目录。

## 奏法事件

```json
{ "time": 0.0, "type": "articulation", "name": "slow_sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.7 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 36, "velocity": 0.8 }
{ "time": 1.5, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

## 音准与渲染

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/低音提琴/校准音准.py

.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/低音提琴/乐器.json `
  --events examples/低音提琴_奏法.events.json `
  --output output/低音提琴_奏法.wav
```

## 单音色 formal 不代表的能力

- VPO 没有 `bass-SOLO-tremolo.sfz`；现有的 `bass-SEC-tremolo.sfz` 是声部合奏，本实现不用它冒充独奏颤弓；
- 持续音只有一个实录力度层，`expression` 只能平滑改变响度，不能产生真实持续音色渐变；
- 原映射的随机微调音高、响度和延迟未启用，以保持渲染确定性；
- 没有独立释弦采样、真实连奏过渡、换弓/换弦、泛音或近马采样；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；线性重采样与协奏维度仍待更细验收。

资源冻结与授权见 [来源.md](来源.md) 和 [资源核验.json](资源核验.json)。
