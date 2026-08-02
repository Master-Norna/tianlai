[中文](README.md) | [English](README.en.md)

# 合唱电钢琴（formal）

Greg Sullivan Yamaha CP80 实录核心加确定性立体声 chorus。采样核心复用
`tianlai/dedicated_sfz.py`，效果链由 `tianlai/dedicated_fx.py` 逐帧执行，
不含通用 SoundFont 静默回退。

## 来源与许可

- 上游：Greg Sullivan E-Pianos / Yamaha CP80
- 固定提交：`8c3e581acda3594b553948ff0222d4f84a698376`
- 许可：CC-BY-3.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json)，复算脚本为 [`核验资源.py`](核验资源.py)

## 获取资源

本入口与电钢琴共享
[`../获取GregSullivan电钢琴音源.ps1`](../获取GregSullivan电钢琴音源.ps1)。
安装器固定上游提交并校验许可证、README、SFZ、81 个 FLAC 及其聚合
SHA-256；已有 `音源/GregSullivan.E-Pianos` 时只验证，不覆盖。

## 映射与信号链

- `normal`：`CP80/CP80.sfz`

确定性信号链:0.9 Hz / 4.5 ms 双声道 LFO 合唱,右声道相位偏移 90°,干湿各半。全部参数固化在 `乐器.json` 的
`effects` 数组,无随机源,同一输入必得同一输出。

## 音域

A0（21）— C8（108）

## 调音

根采样与端到端宽频音准结果见 [`音准校准.json`](音准校准.json)。

## 试听

固定事件为 `examples/合唱电钢琴_奏法.events.json`；试听指标由
[`核验试听.py`](核验试听.py) 复算。

## 已知限制

chorus 为确定性 DSP；核心与电钢琴入口共享 CP80 四力度资源。CP80 不等同于
Rhodes/Wurlitzer。根采样 D#1—B7 覆盖 A0—C8，最低音最大下移 6 个半音；
高音端保留真实 CP80 的拉伸调律（根采样最大约 +44 c）。当前没有独立机械
键噪层，当前绑定版本已通过单音色试听；协奏与完整能力覆盖仍未测试。
