# 拨奏弦乐

这是 VPO `all-strings-SEC-pizzicato.sfz` 的专用拨奏入口，不是把弦乐合奏换名，也不接受持续或颤弓奏法。

- 默认且唯一奏法为 `pizzicato`；实音/记谱范围 C1–A7（MIDI 24–105）；
- 63 个真实拨弦区域：低音提琴 12、大提琴 24、中提琴 13、小提琴 14；
- 四声部使用与上游一致的音区交叉淡化；短音按原 WAV 自然尾音播放；
- 63 个本奏法根采样单独校准，中位偏差 `4.707 cents`，最大原始偏差 `108.394 cents`；约 100 cents 的低音 RR2 文件由上游 `transpose=-1` 修正，本实现以实测根音等效还原；
- 支持 A4、小数音高、力度、expression 和确定性播放。

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/拨奏弦乐/乐器.json `
  --events examples/拨奏弦乐_奏法.events.json `
  --output output/拨奏弦乐_奏法_candidate.wav
```

## 单音色 formal 的已知限制

- 上游映射没有真正的 `seq_position` 拨奏 RR；文件名中的 RR1/RR2 在低音区被交替分配给相邻根音，不能虚报为每音轮替；
- 大提琴部分包含低力度叠层，当前采样器在重叠区离散选择，尚未实现 SFZ 同时叠加；
- 没有左手制音、Bartók pizzicato、指板/琴桥位置、独立释弦或真实声部人数控制；
- 当前绑定版本的单音色试听已通过，因此为 `formal`；线性重采样、扩展能力与协奏盲听仍待审。

资源证据见 [来源.md](来源.md)、[资源核验.json](资源核验.json) 和 [试听核验.json](试听核验.json)。
