# 钢片琴

基于 Virtual Playing Orchestra 3.3 的真实 Celesta 多采样 `formal`。该实现读取专用 `Keys/celesta.sfz` 与 WAV，不再使用 GM 钟琴或通用 SoundFont 代替。

## 当前能力

- 输入按实音处理，范围 C4–C8（MIDI 60–108）；常规钢片琴谱低记八度，清单同时记录书写范围 C3–C7；
- 20 个去重 WAV、21 个 SFZ 区域；软层 11 区域、硬层 10 区域；
- 按上游映射重现软层 `0–95` 淡出和硬层 `63–127` 淡入，重叠区使用等功率交叉淡化；
- 支持 A4 基准、小数 MIDI/Hz 音高、力度、`expression` 平滑与延音踏板；
- 20 个根采样经过谐波约束 FFT 校准：中位偏差 `4.6005 cents`，最大原始偏差 `40.698668 cents`；
- 中文/空格路径、资源缺失显式报错、确定性渲染均纳入自动测试。

## 使用

```powershell
.\.venv\Scripts\python.exe 乐器/键盘乐器/钢片琴/校准音准.py

.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/键盘乐器/钢片琴/乐器.json `
  --events examples/钢片琴_奏法.events.json `
  --output output/钢片琴_奏法_candidate.wav
```

## 单音色 formal 不代表的能力

- 每个音高只有软/硬两层，C4 低力度还复用硬层 WAV；没有独立 Round Robin；
- 上游包虽带机械噪声文件，但此 SFZ 没有踏板、键噪、释键、共鸣或半踏板建模；
- 采样没有循环，长音按真实录音自然衰减；重采样仍为线性；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；人工盲听与扩展/协奏维度仍待审。

资源版本、许可与聚合 Hash 见 [来源.md](来源.md) 和 [资源核验.json](资源核验.json)。
