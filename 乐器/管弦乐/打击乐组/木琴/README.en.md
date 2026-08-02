[简体中文](README.md) | **English**

# Xylophone

Dedicated VPO 3.3 / No Budget Orchestra multisample `formal` entry.

- API input is always concert pitch C4–C8 (MIDI 60–108).
- Traditional notation is one octave lower, C3–C7; the collaboration layer must add 12 semitones.
- 15 root regions × 2 round robins, totaling 30 WAV files. The unwritten default SFZ `seq_position` is correctly interpreted as RR1.
- All 30 samples have FFT calibration, with support for changed A4 tuning and fractional MIDI pitch.
- Short notes play as complete WAV one-shots and are not truncated by `note_off`.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/打击乐组/木琴/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render --instrument 乐器/管弦乐/打击乐组/木琴/乐器.json --events examples/木琴_奏法.events.json --output output/木琴_奏法.wav
```

The single-timbre audition for the currently bound version has passed, so its status is `formal`. There is still only one recorded velocity layer, with no alternative mallets, rolls, or damping samples. High/low boundaries and higher-order resampling remain for extended/ensemble acceptance testing.
