# 管弦大鼓

VPO 3.3 专用多采样 `formal`，无固定音高。

- `drum_1`：SSO 两个离散力度层；
- `drum_2`：VSCO2-CE 两个离散力度层 × 2 RR；
- one-shot 完整长尾，不被常规 note-off 截断；
- pitch/amp/delay 随机项改为稳定种子微扰，重复渲染逐字节一致；
- 音准报告明确为 N/A。

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/管弦大鼓/乐器.json --events examples/管弦大鼓_奏法.events.json --output output/管弦大鼓_奏法.wav
```

当前绑定版本的单音色试听已通过，因此为 `formal`；上游 54–104 力度交叉淡化目前仍是离散硬切，缺少多槌头、闷音与独立空间位，扩展/协奏盲听仍待审。
