# 管弦钹

VPO 3.3 专用多采样 `formal`，无固定音高，共 15 个去重 WAV。

公开奏法：`roll_soft`、`piatti`、`roll_alt`、`piatti_high`、`crescendo_short`、`crash`、`crescendo_medium`、`suspended_hit`、`crescendo_long`、`suspended_high`。强击与吊钹击包含两力度 × 2 RR；滚奏可提前释放，预录渐强与撞击按 one-shot 保留长尾。

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/管弦钹/乐器.json --events examples/管弦钹_奏法.events.json --output output/管弦钹_奏法.wav
```

当前绑定版本的单音色试听已通过，因此为 `formal`；预录渐强长度仍不可随乐谱无损伸缩，也没有真实手闷 choke、槌头/边缘位置或多麦位，上游 EQ/空间处理与协奏盲听仍待完成。
