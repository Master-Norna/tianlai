[简体中文](README.md) | **English**

# Trombone

This `formal` solo trombone is based on Virtual Playing Orchestra 3.3 and directly uses Iowa/VPO multisamples. It never falls back silently to a GM SoundFont.

- Actual/orchestral written range: MIDI `40–77`, E2–F5, with concert-pitch input.
- Articulations: `normal`/`sustain`, `slow_sustain`, `staccato`, and `accent`.
- Continuous controls: `expression`, `breath`, 9-step attack `modulation`, and `sustain_pedal`.
- Dynamics: VPO's two-layer mappings are used for both sustained and short notes.
- Tuning: all 20 sustain root samples are calibrated individually, with support for arbitrary A4 tuning and fractional MIDI pitch.
- Release: SFZ envelopes and embedded loops, with no independent release samples.

The single-timbre audition for the currently bound version has passed, so its status is `formal`. This entry point does not automatically model continuous slide glissando. The upstream LFO, EQ, random variation, and continuous velocity crossfade are also not yet reproduced in full; the glissando protocol and blinded ensemble audition still require review.

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\长号\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\长号\核验资源.py
```

Audition events are in `examples/长号_奏法.events.json`; the frozen resource record is in `资源核验.json`.
