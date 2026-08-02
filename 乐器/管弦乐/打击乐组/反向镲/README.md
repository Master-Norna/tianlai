**简体中文** | [English](README.en.md)

# 反向镲(formal)

VCSL 悬吊镲真实采样的确定性倒放。反向镲本质就是镲片录音倒放,本实现
在加载时对已核验的源文件做逐样本时间反转(`tianlai/reversed_cymbal.py`),
不引入任何随机源或通用 SoundFont 回退。

## 来源与许可

- 上游:sgossner/VCSL (Versilian Community Sample Library)
- 版本:1.2.2-RC,许可:CC0-1.0
- 源采样与许可证据逐文件 SHA-256 见 [`资源核验.json`](资源核验.json)

## 变体映射

| MIDI 键 | 源采样 | 上升沿 |
| --- | --- | --- |
| 60 | susCymb2_hit_fff1(亮) | 15.81 s |
| 61 | susCymb1_hit_fff1(暗) | 12.95 s |
| 62 | susCymb2_roll_fff1(滚奏长涌) | 20.91 s |

倒放上升沿长度:键 60:15.81s, 键 61:12.95s, 键 62:20.91s。note_off 触发 12 ms 防爆音骤停淡出,
提前松键即得"半截涌起"效果;不松键播完整上升沿后自然骤停。

## 调音

无固定音高;键位仅选择变体,见 [`音准校准.json`](音准校准.json) 的不适用声明。

## 试听

固定事件:`examples/反向镲_奏法.events.json`;渲染 24.80 s,
峰值 0.419996,RMS 0.047638,削波 0;
WAV SHA-256 `de8402b3…`。

## 已知限制

变体数量有限(3 种);倒放为全长反转,无中途起播偏移。当前绑定版本已通过单音色试听并标为 formal；协奏、完整奏法与实际曲目仍未测试。
