# 弱音小号(formal)

VPO 独奏小号采样 + 确定性直管弱音器滤波建模。本目录是 98 项清单 VPO-14 的专用实现,采样核心复用
`tianlai/dedicated_sfz.py`,效果链由 `tianlai/dedicated_fx.py` 逐帧执行,
不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:Virtual Playing Orchestra 3(Standard 3.3 / Wave 3.2)
- 版本:Standard 3.3 / Wave 3.2,许可:混合公开许可:SSO Sampling Plus、No Budget Orchestra/Mattias CC-BY-SA、VSCO2 CC0 等,见 Documentation/license.htm
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与信号链

- `sustain`:`Brass/trumpet-SOLO-sustain.sfz`
- `staccato`:`Brass/trumpet-SOLO-staccato.sfz`
- `accent`:`Brass/trumpet-SOLO-accent.sfz`

确定性信号链:520 Hz 高通 → 1.65 kHz Q2.2 +9 dB 谐振峰 → 4.2 kHz 低通,近似直管弱音器的鼻音共振传递特性。全部参数固化在 `乐器.json` 的
`effects` 数组,无随机源,同一输入必得同一输出。

## 音域

F#3(54) - A#5(82)

## 调音

54 个根采样谐波 FFT 诊断;实测中位 -1.668 c,上游映射后残差中位 -1.668 c,最大残差 2.222 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/弱音小号_奏法.events.json`;
渲染 14.25 s,峰值 0.420029,
RMS 0.079816,削波 0;
WAV SHA-256 `593f60f4…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

弱音音色为确定性滤波建模,非真实弱音小号实录;真实弱音采样仍是后续保真升级方向。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
