[简体中文](README.md) | **English**

# Glockenspiel

Dedicated VPO 3.3 / VSCO2-CE multisample `formal` entry with sounding and written range F5–C8 (MIDI 77–108).

- 6 root samples cover the complete range.
- FFT tuning calibration has been generated for all 6/6 samples and is used in playback ratios.
- One-shots preserve long tails and are not truncated by `note_off`.
- Upstream random variation of ±12 cents pitch, ±1.5 dB loudness, and 12 ms delay is replaced by stable hash-based variation.
- Supports changed A4 tuning and fractional MIDI pitch.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/钟琴/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/钟琴/乐器.json --events examples/钟琴_奏法.events.json --output output/钟琴_奏法.wav
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. There is still only one recorded velocity layer, with no round robin, alternate mallet, or damping sample. Wide-range transposition, linear resampling, and blinded ensemble audition still require review.
