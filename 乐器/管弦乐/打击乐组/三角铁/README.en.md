[简体中文](README.md) | **English**

# Triangle

Dedicated VPO 3.3 multisample `formal` entry with no silent GM fallback. A triangle has no fixed pitch; any valid note event only triggers the current playing technique.

- `open`: 2 discrete velocity layers, each with 2 round robins.
- `muted`: muted strike.
- `roll`: an 8-second recorded roll that can fade early on `note_off`.
- Reproduces upstream `off_by` exactly: an open strike stops a roll, a muted strike stops an open tail, and a roll does not stop other sounds in the opposite direction.
- `pitch_random`/`amp_random` use the event number and resource-relative path to generate stable variation, reproducible across processes and directory moves.

Run:

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/三角铁/乐器.json --events examples/三角铁_奏法.events.json --output output/三角铁_奏法.wav
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. There is still only one finite roll recording, no continuous velocity crossfade, and no independent spatial position. Extended capabilities and blinded ensemble audition still require review.
