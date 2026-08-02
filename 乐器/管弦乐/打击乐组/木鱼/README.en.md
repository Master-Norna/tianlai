[简体中文](README.md) | **English**

# Woodblocks

Dedicated VPO 3.3 / SSO sample `formal` entry. This is unpitched percussion: `low` and `high` represent a relatively low and high pair of woodblocks and are not mapped to equal-tempered notes.

- 1 recorded WAV file for each of the high and low blocks.
- Complete one-shot tails.
- Supports velocity and smoothed `expression`.
- The tuning report explicitly records N/A and does not invent cents values.

```powershell
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/木鱼/乐器.json --events examples/木鱼_奏法.events.json --output output/木鱼_奏法.wav
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. Each woodblock still has only one sample and there is no round robin, alternate mallet, roll, or multi-size extension. Extended/blinded ensemble audition still requires review.
