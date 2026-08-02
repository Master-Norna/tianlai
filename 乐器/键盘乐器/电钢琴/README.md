[中文](README.md) | [English](README.en.md)

# 电钢琴（formal）

本入口采用 Greg Sullivan 实录的 Yamaha CP80 Electric Grand Piano，而不是旧版
TX81Z FM 占位音色。渲染引擎复用 `tianlai/dedicated_sfz.py`，不含通用
SoundFont 静默回退。

## 来源与许可

- 上游：Greg Sullivan E-Pianos / Yamaha CP80
- 固定提交：`8c3e581acda3594b553948ff0222d4f84a698376`
- 许可：CC-BY-3.0，署名与许可证据见 [`来源.md`](来源.md)
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json)，复算脚本为 [`核验资源.py`](核验资源.py)

## 获取资源

电钢琴与合唱电钢琴共用一个安全安装器：
[`../获取GregSullivan电钢琴音源.ps1`](../获取GregSullivan电钢琴音源.ps1)。
它固定到上述提交，安装到 `音源/GregSullivan.E-Pianos`，并核验许可证、
上游 README、SFZ 与 81 个 FLAC 的聚合 SHA-256。目标目录已存在时只校验，
不会覆盖或合并现有内容。

## 映射与奏法

- `normal`：`CP80/CP80.sfz`

四档真实力度层 PP / MP / F / FF；默认奏法 `normal`，`pitch_mode=pitched`。

## 音域

A0（21）— C8（108）

## 调音

根采样校准和端到端宽频音准结果以 [`音准校准.json`](音准校准.json) 为准。
校准除微调外必须识别 ±1200 cents 的根音映射错误。

## 试听

固定事件为 `examples/电钢琴_奏法.events.json`；指标和 WAV Hash 由
[`核验试听.py`](核验试听.py) 复算。当前说明不承诺额外的细分奏法矩阵或
专家听审结论。

## 已知限制

CP80 与 Rhodes/Wurlitzer 的结构和音色并不相同，本入口应明确理解为 Yamaha
电声大钢琴。上游根采样从 D#1 到 B7，SFZ 将其覆盖到 CP80 的 A0—C8；
最低 A0 相对首个根采样下移 6 个半音，这是本资源最大的音色拉伸，不能误写成
逐键采样。最高根采样约有 +44 c 的 CP80 拉伸调律，属于真实弦列特征，因此
本入口不满足“全音域 ≤10 c”的可信白名单硬门槛。上游未提供独立机械键噪和
释放采样；长音依赖完整录音自然衰减。
