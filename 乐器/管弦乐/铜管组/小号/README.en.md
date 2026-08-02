[简体中文](README.md) | **English**

# Trumpet

This `formal` solo trumpet is based on Virtual Playing Orchestra 3.3 and directly uses the two-velocity-layer Iowa/VPO solo samples. It never falls back silently to a GM SoundFont.

- Actual sounding range: MIDI `54–84`, F#3–C6.
- Notation semantics: events use concert pitch. Written pitch for a B♭ trumpet is a major second above concert pitch, `concert = written - 2`; the corresponding written range is G#3–D6.
- Articulations: `normal`/`sustain`, `slow_sustain`, `staccato`, and `accent`.
- Continuous controls: `expression`, `breath`, 9-step attack `modulation`, and `sustain_pedal`.
- Dynamics: 54 sustain root samples form two layers; staccato is implemented by the upstream short-envelope candidate over the same material.
- Tuning: all 54 sustain root samples are calibrated individually, with support for arbitrary A4 tuning and fractional MIDI pitch.
- Release: SFZ envelopes and embedded loops, with no independent release samples.

The upstream EQ, LFO, random variation, and continuous crossfade are not currently reproduced in full. The single-timbre status is `formal`; the ensemble status is `untested`.

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\小号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\小号\核验资源.py
```

Audition events are in `examples/小号_奏法.events.json`; the frozen resource record is in `资源核验.json`.
