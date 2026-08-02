**简体中文** | [English](README.en.md)

# 管钟

当前 `formal` 固定使用 VCSL `Tubular Bells 2`，实音与记谱音域均为
C4–G5（MIDI 60–79）。这是单一、可核验的 CC0 音源，不再引用原来的
VPO/NBO 混合许可采样。

清单里的 `type: vpo_percussion` 是复用共享打击乐 SFZ 适配器的历史接口名，
不代表本乐器仍使用 VPO 音源；实际来源完全由 VCSL 路径和冻结 Hash 锁定。

## 真实素材与映射

- 22 个 PCM16 / 44.1 kHz / 双声道 WAV；
- 11 个真实根音：60、62、64、65、67、69、71、72、74、76、77；
- 每个根音有 2 个真实录音力度层：MIDI velocity 0–83 与 84–127；
- 相邻音插值覆盖 C4–G5，最大伸展 2 个半音；
- 0 个 round robin；文件名末尾的 `_1` / `_2` 是上游选定的 take，
  SFZ 没有顺序轮换操作码，因此不把它们虚报成 RR；
- 0 个循环。`open` 播放原始自然长尾，并保留双声道空间信息。

`damped` 不是独立录制的制音奏法：它复用同一批录音，只在项目层对
note-off 或踏板松开施加 120 ms 去点击释放包络。它可以用于编曲控制，
但不能宣称具有真实制音器噪声或独立制音采样。

## 项目层修正

上游 `Tubular Bells 2.sfz` 中两个非零起点会切入清晰的敲击起音：

- `TB_hit_B4_v2_1.wav`: 1026 帧；
- `TB_hit_C5_v4_1.wav`: 2727 帧。

本项目在加载映射后把这两个 `offset` 明确覆盖为 0；VCSL 原文件没有被
修改。上游区域增益最高会把原始峰值推到约 +3.046 dBFS，因此乐器总增益
固定为 0.35，静态最坏情况仍保留约 6.07 dB 余量。

## 音准口径

SFZ 的 `pitch_keycenter` 已经包含生成注释中的 `--transpose 12` 结果，
不能再次整体升高八度。管钟频谱是强非谐波结构，单个最大 FFT 峰并不等于
听觉基音；自动逐样本 cents 修正继续禁用，只核验根音、音域、八度和映射。
人工频谱与听觉定音仍为 `pending`。

## 复核

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/管钟/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/管钟/校准音准.py
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/管钟/核验试听.py
.\.venv\Scripts\python.exe -m unittest tests.test_vcsl_tubular_bells
```

当前绑定版本的单音色试听已通过，因此为 `formal`。仍存在的真实限制是只有 11 个根音和 2 个力度层、没有 RR，
`damped` 是包络模拟，并且仍需人工盲听与非谐波定音复核。
