# 爵士电吉他(formal)

Karoryfer Emilyguitar 平卷弦 DI + 确定性爵士音色滤波链。本目录是 98 项清单 SAM-16 的专用实现,采样核心复用
`tianlai/dedicated_sfz.py`,效果链由 `tianlai/dedicated_fx.py` 逐帧执行,
不含通用 SoundFont 静默回退。

## 来源与许可

- 上游:Karoryfer Lecolds (D. Smolken): Emilyguitar
- 版本:v1.001,许可:CC0-1.0
- 逐文件 SHA-256 与统计见 [`资源核验.json`](资源核验.json),复算脚本 [`核验资源.py`](核验资源.py)

## 映射与信号链

- `normal`:`emily_basic.sfz`

确定性信号链:90 Hz 一阶高通 + 2.4 kHz 一阶低通,模拟琴颈拾音器爵士音色;含释放闷音采样。全部参数固化在 `乐器.json` 的
`effects` 数组,无随机源,同一输入必得同一输出。

## 音域

D2(38) - D6(86)

## 调音

251 个根采样谐波 FFT 诊断;实测中位 +1.155 c,上游映射后残差中位 +1.155 c,最大残差 178.938 c(详见 [`音准校准.json`](音准校准.json))

## 试听

固定事件:`examples/爵士电吉他_奏法.events.json`;
渲染 10.15 s,峰值 0.419989,
RMS 0.047795,削波 0;
WAV SHA-256 `7b0343f50f80…`。复算脚本 [`核验试听.py`](核验试听.py)。

## 已知限制

DI 平卷弦经暗色滤波近似爵士箱琴音色,非空心琴体实录;释放闷音采样保留。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
