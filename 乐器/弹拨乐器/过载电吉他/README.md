[中文](README.md) | [English](README.en.md)

# 过载电吉他(formal)

Karoryfer Emilyguitar DI + 确定性软削波过载链。本目录是 98 项清单 SAM-17 的专用实现,采样核心复用
`tianlai/dedicated_sfz.py`,效果链由 `tianlai/dedicated_fx.py` 逐帧执行,
不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:Karoryfer Lecolds (D. Smolken): Emilyguitar
- 版本:v1.001,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与信号链

- `normal`:`emily_clean.sfz`

确定性信号链:110 Hz 高通 → 软削波(pre 6.5 / post 0.55)→ 5.2 kHz 低通,模拟轻过载音箱前级。全部参数固化在 `乐器.json` 的
`effects` 数组,无随机源,同一输入必得同一输出。

## 音域

D2(38) - D6(86)

## 调音

251 个根采样谐波 FFT 诊断;实测中位 +1.155 c,上游映射后残差中位 +1.155 c,最大残差 178.938 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/过载电吉他_奏法.events.json`;
渲染 10.15 s,峰值 0.420014,
RMS 0.095231,削波 0;
WAV SHA-256 `aa776c72…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

过载由确定性波形整形获得,非过载音箱实录;和弦互调符合真实过载物理。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
