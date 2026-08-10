[中文](README.md) | [English](README.en.md)

# 中音萨克斯

这是基于 `sfzinstruments/MTG.SoloSax` 的专用采样 `formal`，默认读取真实 FLAC 与上游 SFZ，不会在资源缺失时静默换成 GM SoundFont。

## 当前能力

- 事件音高采用实音：采样实音域 Db3–A5（MIDI 49–81）；E♭ 中音萨克斯记谱音域 Bb3–F#6（58–90），换算为 `实音 = 记谱音 - 9 半音`。
- 33 个半音键位、2 个真实力度层、每音 3 个确定性 RR 映射，共 198 个有音高区域；RR2/RR3 按上游设计使用相邻键真实录音移调，并非每个键独立录制 3 次。
- 66 个去重有音高 FLAC 全部读取 `riff/smpl` 循环；另接入 68 个呼吸噪声和 36 个按键噪声录音。
- 播放音准严格采用上游 `key/pitch_keycenter + tune`。66 个文件逐个循环段 FFT 诊断的相对残差中位数为 `+0.754 cents`、最大绝对差 `4.277 cents`。
- 有音高与噪声引擎统一配置带限重采样：1:1 整数位置精确直读，其余步进使用 16 tap / 1024 相位 sinc；当有效播放步进大于 1（如升调或输出采样率低于源采样率）时，会在抽取前收窄带宽，减少混叠与高频粗糙感。
- `expression`、`breath` 为平滑响度控制；`modulation` 按上游最大 50 cents、约 5 Hz、2 秒淡入实现确定性颤音；`noise` 控制真实噪声层。
- 支持 `note_off` 与 `sustain_pedal`；中文及空格 Windows 路径可加载，渲染可逐字节复现。

## 事件约定

```json
{ "time": 0.0, "type": "articulation", "name": "legato" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.8 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.75 }
{ "time": 0.0, "type": "control", "name": "modulation", "value": 0.4 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 65, "velocity": 0.8 }
{ "time": 1.2, "type": "note_off", "note_id": 1 }
```

`midi_note` 始终是实音。协作层以后可读取 `written_to_sounding_semitones=-9`，把记谱音转换成实音。

```powershell
.\.venv\Scripts\python.exe 乐器/现代管乐/中音萨克斯/核验资源.py
.\.venv\Scripts\python.exe 乐器/现代管乐/中音萨克斯/校准音准.py
.\.venv\Scripts\python.exe 乐器/现代管乐/中音萨克斯/核验试听.py
```

## 单音色 formal 不代表的能力

- `legato` 使用上游同一持续音采样的 20,000 帧偏移、50 ms 起音和短交叉释放，只是伪连奏，不是录制的音程过渡。
- 力度层为离散选择，没有连续音色交叉渐变；呼吸/表情目前不做连续滤波或谱形变。
- 噪声池使用全部真实录音，并将上游“4 组序列 + 组内随机”展平为确定性循环；呼吸噪声仅在乐句起点触发，叠接 `legato` 不会重复吸气，按键噪声随 `note_off` 触发。
- 当前绑定版本已通过单音色机器门并重建固定试听，因此为 `formal`；原始单声道素材、映射参数与运行时选择均保持可追溯和逐字节可复现。

来源与可复算证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json)、[音准校准.json](音准校准.json) 和 [试听核验.json](试听核验.json)。
