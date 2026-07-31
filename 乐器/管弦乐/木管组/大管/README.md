# 大管

基于 Virtual Playing Orchestra Standard 3.3 / Wave 3.2 的独奏大管 `formal`，直接读取 SSO 与 Iowa 子库的 SOLO SFZ/WAV，不会静默回落到 GM SoundFont。

## 当前能力

- 实际发声与记谱音域均为 B♭1–E♭5（MIDI 34–75），事件输入实音；
- 持续音使用 13 个 SSO 根采样和内嵌循环；断奏攻击使用 66 个 Iowa 区域、2 个离散力度层（每层 33 个音）；
- `sustain`、`slow_sustain`、`staccato`、`accent` 四种奏法；accent 按上游映射把 Iowa 短攻击与 SSO 持续层同时发声；
- 79 个去重 WAV 全部实测校准，中位偏差 `+7.360 cents`，原采样最大绝对偏差 `26.585 cents`；
- `expression`、`breath` 平滑控制，独奏换音执行短交叉释放；
- Windows 中文/空格路径通过资源核验、校准、测试和固定试听渲染。

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/大管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/大管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/大管/乐器.json `
  --events examples/大管_奏法.events.json `
  --output output/大管_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 持续音只有一个录制力度层；两层 Iowa 素材在上游通过包络制作短音，仍非专门录制的断奏；
- 没有 Round Robin、真连奏过渡、换气、按键或独立释音；`breath` 目前只平滑控制响度；
- SFZ 随机微扰和部分滤波/EQ 未启用，当前为确定性线性重采样；
- 机器试听通过，人工 A/B 与盲听仍待审。

证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [试听核验.json](试听核验.json)。
