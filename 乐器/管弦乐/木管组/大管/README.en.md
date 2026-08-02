[简体中文](README.md) | **English**

# Bassoon

This `formal` solo bassoon is based on Virtual Playing Orchestra Standard 3.3 / Wave 3.2 and directly reads SOLO SFZ/WAV resources from the SSO and Iowa sublibraries. It never falls back silently to a GM SoundFont.

## Current capabilities

- Sounding and written range are both B♭1–E♭5 (MIDI 34–75), with concert-pitch event input.
- Sustains use 13 SSO root samples with embedded loops. Staccato attacks use 66 Iowa regions with 2 discrete velocity layers (33 notes per layer).
- Four articulations: `sustain`, `slow_sustain`, `staccato`, and `accent`. Following the upstream mapping, accent sounds the Iowa short attack and SSO sustain layer simultaneously.
- All 79 deduplicated WAV files are measured and calibrated, with a median deviation of `+7.360 cents` and a maximum absolute deviation in the original samples of `26.585 cents`.
- Smoothed `expression` and `breath` controls; solo note changes use a short cross-release.
- Resource verification, calibration, tests, and the fixed audition render cover Windows paths containing Chinese characters and spaces.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/大管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/大管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/大管/乐器.json `
  --events examples/大管_奏法.events.json `
  --output output/大管_奏法_candidate.wav
```

## Capabilities not implied by single-timbre `formal` status

- Sustains have only one recorded velocity layer. The two Iowa layers are shortened by upstream envelopes and are still not dedicated staccato recordings.
- There is no Round Robin, genuine legato transition, breath, key noise, or independent release; `breath` currently controls only smoothed loudness.
- SFZ random variation and some filtering/EQ are disabled; the current implementation is deterministic and uses linear resampling.
- Machine audition has passed; human A/B comparison and blinded listening still require review.

See [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [试听核验.json](试听核验.json) for evidence.
