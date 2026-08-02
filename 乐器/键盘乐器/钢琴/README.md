[中文](README.md) | [English](README.en.md)

# 钢琴

基于 Salamander Grand Piano V3 的 Yamaha C5 三角钢琴。

## 当前能力

- 30 个采样根音覆盖 A0–C8；
- 保留上游 SFZ 的 16 个非均匀力度层；
- 按需解码 FLAC，不在启动时载入整套音源；
- 88 键独立释键机械声；
- 两层琴弦释放共鸣；
- 延音踏板状态和两组踏板升降机械声；
- 无制音器高音区使用更长的释放时间；
- 柔音踏板的首版近似响应；
- 采样点精确调度和确定性 Round Robin。

## 当前限制

- 半踏板目前连续接收数值，但制音切换仍以阈值近似；
- 交感共鸣使用上游释放采样，不是全局琴弦耦合模型；
- 柔音踏板尚无独立采样层；
- 当前使用线性重采样，不提供带限重采样；
- 尚未实现重新踩踏、中央踏板和音板物理模型。

授权及音源来源见 [来源.md](来源.md)。

## 音准验证

实际测量中层力度 A4：

```powershell
.\.venv\Scripts\python.exe -m tianlai analyze-pitch `
  --audio 音源/钢琴/SalamanderGrandPiano/Samples/A4v8.flac `
  --expected-hz 440
```

当前结果约为 `440.2334 Hz / +0.918 cents`，专项测试要求误差小于 2 音分。

## 获取音源

音源资产不进入项目代码版本控制。新环境运行：

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/键盘乐器/钢琴/获取音源.ps1
```

安装器固定官方仓库提交
`3382bf9496bba2486f5ab0de55a264d1dfc38404`，并在临时目录核验许可证、
README、主 SFZ、上游全部 641 个 FLAC 及 668 文件完整树；验证通过后才原子安装。已有
目录若完全一致只复核后退出，若不一致则先构建新树，切换失败会恢复旧目录。完整
摘要见 [`来源.md`](来源.md)。

## 渲染示例

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/键盘乐器/钢琴/乐器.json `
  --events examples/钢琴_C大调.events.json `
  --output output/钢琴_C大调.wav
```

## 质量状态与核验

本入口为 `formal`。核验材料：

- [`资源核验.json`](资源核验.json)：构造实例后遍历**实际加载**的 618 个采样并逐文件
  求 SHA-256，复算脚本 [`核验资源.py`](核验资源.py)；
- [`音准校准.json`](音准校准.json)：逐根采样谐波 FFT 诊断，复算脚本
  [`校准音准.py`](校准音准.py)；
- [`试听核验.json`](试听核验.json)：固定谱例渲染的峰值/RMS/削波/WAV 哈希，复算脚本
  [`核验试听.py`](核验试听.py)。

## 采样映射说明

### C8 组采样

上游 `C8v*.flac` 实测约为 C#8。`乐器.py` 的 `_ROOT_TUNING_CENTS` 将该组根
采样声明为 `+100 音分`，使引擎按真实录音音高映射 C8 键；其他根采样不做这项
修正，以保留钢琴原有的伸展调律。完整测量见 [`音准校准.json`](音准校准.json)。

### 不是缺陷：高音区 +27～+38 音分

F7–B7 一带测得约 +27～+38 音分，属于钢琴的 Railsback 伸展调律：高音区偏高、
低音区偏低。它不是平均律误差，渲染不会把这一区域强制拉平。

### 其他

- 释键噪（`rel*.flac`）与踏板噪（`pedal*.flac`）为无调性层，音准校准对它们的
  测量会撞到搜索边界，已在 `音准校准.json` 中标记为 `unreliable` 并排除出统计。
