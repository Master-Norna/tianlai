[简体中文](README.md) | **English**

# Orchestral Bass Drum

Dedicated VPO 3.3 multisample `formal` entry with no fixed pitch.

- `drum_1`: two discrete SSO velocity layers.
- `drum_2`: two discrete VSCO2-CE velocity layers × 2 round robins.
- Full one-shot tails are not truncated by ordinary `note_off` events.
- Random pitch/amp/delay terms are replaced with stable-seed variation, so repeat renders are byte-for-byte identical.
- The tuning report explicitly records N/A.

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/管弦大鼓/乐器.json --events examples/管弦大鼓_奏法.events.json --output output/管弦大鼓_奏法.wav
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. The upstream velocity crossfade from 54–104 is currently still a discrete hard switch; multiple mallets, damped strikes, and independent spatial positions are absent, and extended/blinded ensemble audition still requires review.
