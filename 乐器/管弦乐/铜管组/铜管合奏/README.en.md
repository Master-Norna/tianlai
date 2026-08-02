[简体中文](README.md) | **English**

# Brass Ensemble

This `formal` four-section brass ensemble is based on Virtual Playing Orchestra 3.3 `all-brass-SEC`. It schedules tuba, horn section, trombone section, and trumpet section together and reproduces the SFZ key-range crossfades. It neither misinterprets the complete ensemble as a random single sample nor falls back silently to a GM SoundFont.

- Actual/input range: MIDI `26–84`, D1–C6; the ensemble entry point consistently uses concert pitch.
- Layering: from low to high, the VPO mapping applies approximate equal-power crossfades across D1–D2, B1–F3, E2–C5, and F#3–C6; overlapping sections genuinely sound together.
- Articulations: `normal`/`sustain`, `slow_sustain`, `staccato`, and `accent`.
- Continuous controls: `expression`, `breath`, 9-step attack `modulation`, and `sustain_pedal`.
- Tuning: all 76 sustain root samples are calibrated individually by FFT; 88 deduplicated WAV files are pinned across sustained and short notes.
- Release: SFZ loops and envelopes, with no independent release samples.

The single-timbre audition for the currently bound version has passed, so its status is `formal`. The four sublibraries have different numbers of velocity layers; each is currently selected deterministically at the midpoint of its upstream crossfade. SFZ EQ/LFO/filtering, random variation, orchestration balance, and blinded ensemble audition still require more detailed acceptance testing.

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\铜管合奏\校准音准.py
.\.venv\Scripts\python.exe .\乐器\管弦乐\铜管组\铜管合奏\核验资源.py
```

Audition events are in `examples/铜管合奏_奏法.events.json`. The original source page is `来源.md`; see [来源.en.md](来源.en.md) and `资源核验.json` for mixed licensing and the frozen resource record.
