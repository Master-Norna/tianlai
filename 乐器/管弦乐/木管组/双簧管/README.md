**简体中文** | [English](README.en.md)

# 双簧管

基于 Virtual Playing Orchestra Standard 3.3 / Wave 3.2 的独奏双簧管 `formal`，直接读取 SOLO SFZ 与 WAV，不会静默回落到 GM SoundFont。

## 当前能力

- 实际发声与记谱音域均为 B♭3–A6（MIDI 58–93），事件输入实音；
- 9 个持续根采样、1 个实录力度层，9 个 WAV 均保留内嵌循环；
- `sustain`、`slow_sustain`、`staccato`、`accent` 四种奏法；accent 分层触发上游短音攻击与持续组件；
- 9 个根采样全部实测校准，中位偏差 `+10.679 cents`，原采样最大绝对偏差 `23.929 cents`；播放时逐样本修正；
- `expression`、`breath` 平滑控制，独奏换音执行短交叉释放；
- Windows 中文/空格路径通过加载与固定试听渲染，结果可重复。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/双簧管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/双簧管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/双簧管/乐器.json `
  --events examples/双簧管_奏法.events.json `
  --output output/双簧管_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 只有一个录制力度层、没有独立 Round Robin；力度与呼吸尚不能连续改变音色；
- 断奏是上游用持续采样偏移和短包络制作的奏法，没有真连奏、换气、按键或释音采样；
- SFZ 的 EQ 与随机微扰尚未进入当前确定性子集；使用线性重采样；
- 机器试听通过，人工 A/B 与盲听仍待审。

事件格式与单簧管相同，`midi_note` 为实音。证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [试听核验.json](试听核验.json)。
