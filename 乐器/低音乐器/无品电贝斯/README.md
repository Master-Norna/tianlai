[中文](README.md) | [English](README.en.md)

# 无品电贝斯(formal)

Karoryfer Ergo 电立式无品贝斯,拨奏为主、弓奏备选。本目录是 98 项清单 SAM-12 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:Karoryfer Samples: Ergo(电立式无品贝斯,含超低音降调采样)
- 版本:master @ c3232f03608e,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `pizzicato`:`ergo_pizz.sfz`
- `arco`:`ergo_arco.sfz`

默认奏法 `pizzicato`;pitch_mode `pitched`。

## 音域

D1(26) - A3(57)

## 调音

333 个根采样谐波 FFT 诊断;实测中位 -3.547 c,上游映射后残差中位 -3.547 c,最大残差 65.686 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/无品电贝斯_奏法.events.json`;
渲染 12.20 s,峰值 0.420011,
RMS 0.027591,削波 0;
WAV SHA-256 `9bb1c254…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

电立式无品贝斯代表无品电贝斯音色;非卧式无品贝斯吉他实录。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
