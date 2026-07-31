# 手风琴（bounded-range formal）

真实音源为 FreePats 的 Hohner 按钮手风琴（Button Accordion HN）。它包含
17 个循环攻击采样和与之配对的 17 个真实释放采样，运行时复用
`tianlai/dedicated_sfz.py`，没有通用 SoundFont 或合成音色静默回退。

## 来源与许可

- 上游：FreePats project: Button Accordion HN
- 官方页面：<https://freepats.zenvoid.org/Organ/accordion.html>
- 发布版本：2024-03-29
- 许可：CC0-1.0
- 本地证据：`LICENSE.txt`、`README.txt`
- 逐文件 SHA-256 和格式统计：[`资源核验.json`](资源核验.json)

## 音域策略

SFZ 的 17 个攻击根音为 MIDI 47–79；本乐器正式使用 D3–G5
（MIDI 50–79）作为核心音域。最高真实根音是 G5（MIDI 79）。

为了保留紧邻的三个半音，G#5–A#5（MIDI 80–82）作为有明确标记的有限
扩展区，最多从 G5 上移 3 个半音。这并不超过上游映射在低音区已经使用的
最大 +3 半音键区。原先 G6（MIDI 91）需要把同一个 G5 根音上移整整
12 个半音，会同步抬高瞬态、噪声与共振峰；现在 MIDI 83–91 会被运行时
明确拒绝。

这套分级表示的是：

- MIDI 50–79：核心音域，位于采用的真实攻击根音跨度内；
- MIDI 80–82：有限扩展，仍然是移调样本，不是假称的逐键采样；
- MIDI 83–91：不再支持，等待许可合格的更高真实根音。

当前采用资源的选择标准、许可和高音边界见 [`来源.md`](来源.md)。

## 奏法与动态

- `sustain`：循环持续采样，并在 note-off 时触发配对释放采样。
- 只有一个录音力度层；velocity 和 expression 只改变播放响度，不能冒充
  真实风箱压力或音色动态层。

## 音准

17 个根采样使用谐波约束 FFT 做诊断，播放仍采用经核验的上游
`pitch_keycenter` 与 `tune`。手风琴的复簧拍频会影响单窗频率估计，因此
报告保留实测残差，但不会擅自把某一根簧片的峰值当作整件乐器的唯一音高。
详见 [`音准校准.json`](音准校准.json)。

## 复算

```powershell
.\.venv\Scripts\python.exe 乐器\键盘乐器\手风琴\核验资源.py
.\.venv\Scripts\python.exe 乐器\键盘乐器\手风琴\校准音准.py
.\.venv\Scripts\python.exe 乐器\键盘乐器\手风琴\核验试听.py
.\.venv\Scripts\python.exe -m unittest tests.test_accordion_range -v
```

试听固定覆盖最低音、核心中音、最高真实根音和有限扩展顶端，结果见
[`试听核验.json`](试听核验.json)。

## 已知限制

- FreePats 当前发布没有 MIDI 80 以上的真实攻击根音。
- 单力度、单奏法、无真实风箱压力层和 round robin。
- MIDI 80–82 仍有轻微上移调音色变化。
- 当前绑定版本的单音色试听已通过，因此为 `formal`；人工盲听复查与扩展/协奏维度仍待审。
