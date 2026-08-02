**简体中文** | [English](README.en.md)

# 小军鼓

VPO 3.3 专用多采样 `formal`，无固定音高，公开击法与真实 SFZ 键位分离。

- `left` / `alternating` / `right`：SSO 左手、双 RR 自动交替、右手；
- `hit`：VSCO2-CE 两力度 × 2 RR 默认击打；
- `kit2_left` / `kit2_right`：第二套鼓的固定左右分支；
- `tap`：两力度 × 2 RR；
- `roll_looped`：SSO WAV 内嵌连续循环；`roll`：VSCO2 有限实录滚奏；
- 两个滚奏映射的 `width=0` 已在声像前按 mid/side 折叠；
- SFZ 随机项改为跨目录可重复的稳定微扰。

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/小军鼓/乐器.json --events examples/小军鼓_奏法.events.json --output output/小军鼓_奏法.wav
```

当前绑定版本的单音色试听已通过，因此为 `formal`；54–104 的原始力度交叉渐变当前仍在中点离散切层，没有边击、刷奏、军鼓响弦开关或多麦位，扩展/协奏盲听仍待审。
