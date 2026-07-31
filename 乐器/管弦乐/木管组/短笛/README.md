# 短笛

基于 Virtual Playing Orchestra Standard 3.3 / Wave 3.2 的独奏短笛 `formal`，直接读取 SOLO SFZ/WAV，不会静默回落到 GM SoundFont。

## 音域与移调约定

- 采样**实音域** D5–C9（MIDI 74–108）；
- 记谱音域 D4–C8（MIDI 62–96）；
- 短笛实际发声比记谱高八度：`实音 = 记谱音 + 12 半音`；
- 基础乐器层的 `midi_note` 一律接收实音，所以记谱 D4 应由协作层转换成实音 D5（74），不会在两个层次重复移调。

## 当前能力

- 10 个持续根采样、1 个实录力度层，全部保留内嵌循环；
- `sustain`、`slow_sustain`、`staccato`、`accent` 四种奏法；accent 拆分并同时触发上游短攻击和持续层；
- 10 个根采样全部实测校准，中位偏差 `+6.821 cents`，原采样最大绝对偏差 `19.521 cents`；
- `expression`、`breath` 平滑控制，独奏换音短交叉释放；
- Windows 中文/空格路径通过资源、音准、测试与固定试听回归。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/短笛/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/短笛/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/短笛/乐器.json `
  --events examples/短笛_奏法.events.json `
  --output output/短笛_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 单一录制力度、无 Round Robin；断奏由持续样本偏移与短包络制作；
- 没有真连奏、换气、按键或独立释音，`breath` 尚未改变音色；
- SFZ 随机微扰与部分 EQ 未进入确定性子集，当前使用线性重采样；
- 极高音域虽由上游 SFZ 映射覆盖，仍需人工重点审听混叠和音色拉伸；人工 A/B 待审。

证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [试听核验.json](试听核验.json)。
