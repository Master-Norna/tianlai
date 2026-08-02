[中文](README.md) | [English](README.en.md)

# 上低音萨克斯

这是基于 `sfzinstruments/MTG.SoloSax` 的专用采样 `formal`，默认读取真实 FLAC 与上游 SFZ，不会在资源缺失时静默换成 GM SoundFont。

## 当前能力

- 事件音高采用实音：采样实音域 C2–A4（MIDI 36–69）；E♭ 上低音萨克斯记谱音域 A3–F#6（57–90），换算为 `实音 = 记谱音 - 21 半音`。
- 34 个半音键位、3 个真实力度层、每音 3 个确定性 RR 映射，共 306 个有音高区域；RR2/RR3 按上游设计使用相邻键真实录音移调，并非每个键独立录制 3 次。
- 100 个去重有音高 FLAC 全部读取 `riff/smpl` 循环；另接入 90 个呼吸噪声和 40 个按键噪声录音。
- 播放音准严格采用上游 `key/pitch_keycenter + tune`。100 个文件也逐个在循环段做 FFT 诊断：相对上游调音中位数 `+2.217 cents`，最大绝对差 `25.626 cents`；自然颤音明显，因此诊断残差不反向覆盖作者的 tune 表。
- `expression`、`breath` 为平滑响度控制；`modulation` 按上游最大 50 cents、约 5 Hz、2 秒淡入实现确定性颤音；`noise` 控制真实噪声层。
- 支持 `note_off` 与 `sustain_pedal`；中文及空格 Windows 路径可加载，渲染可逐字节复现。

## 事件约定

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.8 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.75 }
{ "time": 0.0, "type": "control", "name": "modulation", "value": 0.4 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 52, "velocity": 0.8 }
{ "time": 1.2, "type": "note_off", "note_id": 1 }
```

`midi_note` 始终是实音。协作层以后可读取 `written_to_sounding_semitones=-21`，把记谱音转换成实音。

```powershell
.\.venv\Scripts\python.exe 乐器/现代管乐/上低音萨克斯/核验资源.py
.\.venv\Scripts\python.exe 乐器/现代管乐/上低音萨克斯/校准音准.py
.\.venv\Scripts\python.exe 乐器/现代管乐/上低音萨克斯/核验试听.py
```

## 单音色 formal 不代表的能力

- `legato` 使用上游同一持续音采样的 20,000 帧偏移、50 ms 起音和短交叉释放，只是可靠可复现的伪连奏，不是录制的音程过渡。
- 力度层为离散选择，没有连续音色交叉渐变；呼吸/表情目前不做连续滤波或谱形变。
- 噪声池使用全部真实录音，但将上游“4 组序列 + 组内随机”展平为确定性循环；默认在 note-on/note-off 轻量触发，这是天籁适配行为。
- 当前绑定版本的单音色试听已通过，因此为 `formal`；原始素材仍是单声道，没有多麦克风、房间位或独立释音层，人工 A/B 与协奏盲听仍待审。

来源与可复算证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json)、[音准校准.json](音准校准.json) 和 [试听核验.json](试听核验.json)。
