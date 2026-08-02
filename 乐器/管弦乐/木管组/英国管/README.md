**简体中文** | [English](README.en.md)

# 英国管

基于 Virtual Playing Orchestra Standard 3.3 / Wave 3.2 的独奏英国管 `formal`，直接读取 SOLO SFZ/WAV，不会静默回落到 GM SoundFont。

## 当前能力

- 采样实音域 E3–B♭5（MIDI 52–82）；F 调英国管记谱音域 B3–F6（59–89），`实音 = 记谱音 - 7 半音`；
- `midi_note` 一律输入实音，记谱移调只由未来协作层做一次；
- 9 个持续根采样、1 个实录力度层，全部保留内嵌循环；
- `sustain`、`slow_sustain`、`staccato`、`accent` 四种奏法，accent 分层触发上游短攻击与持续组件；
- 9 个根采样全部实测校准，中位偏差 `+0.923 cents`，原采样最大绝对偏差 `7.701 cents`；
- `expression`、`breath` 平滑控制，独奏换音短交叉释放；
- Windows 中文/空格路径通过资源、音准、测试与固定试听回归。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/英国管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/英国管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/英国管/乐器.json `
  --events examples/英国管_奏法.events.json `
  --output output/英国管_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 单一录制力度、无 Round Robin；断奏由持续样本偏移与短包络制作；
- 没有真连奏、换气、按键或独立释音，`breath` 目前只平滑控制响度；
- SFZ 的 EQ 与随机微扰未进入当前确定性子集，使用线性重采样；
- 机器试听通过，人工 A/B 与盲听仍待审。

证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [试听核验.json](试听核验.json)。
