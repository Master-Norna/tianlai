**简体中文** | [English](README.en.md)

# 三角铁

VPO 3.3 专用多采样 `formal`，不静默回退 GM。三角铁无固定音高；任意合法 note 事件只负责触发当前击法。

- `open`：2 个离散力度层、每层 2 RR；
- `muted`：闷击；
- `roll`：8 秒实录滚奏，可按 note-off 提前淡出；
- 精确复现上游 `off_by`：开放击制止滚奏，闷击制止开放余响，滚奏不反向制止其他声音；
- `pitch_random`/`amp_random` 由事件编号和资源相对路径生成稳定微扰，跨进程、换目录仍可重复。

运行：

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/三角铁/乐器.json --events examples/三角铁_奏法.events.json --output output/三角铁_奏法.wav
```

当前绑定版本的单音色试听已通过，因此为 `formal`；滚奏仍只有一个有限录音，没有连续力度交叉渐变或独立空间位，扩展能力与协奏盲听仍待审。
