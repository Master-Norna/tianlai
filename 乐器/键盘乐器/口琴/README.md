# 口琴(formal)

VCSL Hohner Super 64 半音阶口琴,普通/颤音/重音三奏法。本目录是 98 项清单 SAM-44 的专用实现,渲染引擎复用
`tianlai/dedicated_sfz.py`,不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与奏法

- `sustain`:`Aerophones/Free Aerophones/Harmonica-Hohner-Super64 - Normal.sfz`
- `vibrato`:`Aerophones/Free Aerophones/Harmonica-Hohner-Super64 - Vib.sfz`
- `accent`:`Aerophones/Free Aerophones/Harmonica-Hohner-Super64 - Accented.sfz`

默认奏法 `sustain`;pitch_mode `pitched`。

## 音域

C3(48) - C#7(97)

## 调音

39 个根采样谐波 FFT 诊断;实测中位 -0.396 c,上游映射后残差中位 -0.318 c,最大残差 4.897 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/口琴_奏法.events.json`;
渲染 14.25 s,峰值 0.420026,
RMS 0.085292,削波 0;
WAV SHA-256 `00af08fdb444…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

单力度实录;吸/吹簧片差异未建模。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
