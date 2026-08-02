[简体中文](README.md) | **English**

# Tuba

This `formal` solo tuba is based on Virtual Playing Orchestra 3.3. It uses dedicated SSO/VPO WAV multisamples and never falls back silently to a GM SoundFont.

- Actual sounding range: MIDI `26–62`, D1–D4.
- Notation semantics: orchestral bass-clef input is at concert pitch, `concert = written`. Transposing notation that may be used in brass bands is not handled automatically by this entry point.
- Articulations: `normal`/`sustain`, `slow_sustain`, `staccato`, and `accent`.
- Continuous controls: `expression`, `breath`, 9-step attack `modulation`, and `sustain_pedal`.
- Samples: 9 looped sustain root samples and 12 short-note samples; short notes release at deterministic thresholds.
- Tuning: all 9 sustain root samples are calibrated individually, with support for document-level A4 tuning and fractional MIDI pitch.
- Release: SFZ envelopes/loops, with no independent release samples.

The single-timbre audition for the currently bound version has passed, so its status is `formal`. VPO filtering, random variation, and the complete envelope have not yet been reproduced in full; continuous dynamics and blinded ensemble audition still require more detailed acceptance testing.

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\大号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\大号\核验资源.py
```

Audition events are in `examples/大号_奏法.events.json`; the frozen resource record is in `资源核验.json`.
