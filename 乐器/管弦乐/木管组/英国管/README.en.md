[简体中文](README.md) | **English**

# English Horn

This `formal` solo English horn is based on Virtual Playing Orchestra Standard 3.3 / Wave 3.2 and directly reads SOLO SFZ/WAV resources. It never falls back silently to a GM SoundFont.

## Current capabilities

- Sampled sounding range: E3–B♭5 (MIDI 52–82). The written range for an English horn in F is B3–F6 (59–89), with `实音 = 记谱音 - 7 半音` (sounding pitch equals written pitch minus seven semitones).
- `midi_note` always uses concert pitch; written-pitch transposition is performed exactly once by a future collaboration layer.
- 9 sustain root samples and 1 recorded velocity layer, all retaining embedded loops.
- Four articulations: `sustain`, `slow_sustain`, `staccato`, and `accent`; accent triggers the upstream short attack and sustain component as layers.
- All 9 root samples are measured and calibrated, with a median deviation of `+0.923 cents` and a maximum absolute deviation in the original samples of `7.701 cents`.
- Smoothed `expression` and `breath` controls; solo note changes use a short cross-release.
- Resource, tuning, test, and fixed-audition regressions cover Windows paths containing Chinese characters and spaces.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/英国管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/英国管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/英国管/乐器.json `
  --events examples/英国管_奏法.events.json `
  --output output/英国管_奏法_candidate.wav
```

## Capabilities not implied by single-timbre `formal` status

- There is one recorded velocity layer and no Round Robin; staccato is made from sustain-sample offsets and a short envelope.
- There is no genuine legato, breath, key noise, or independent release; `breath` currently controls only smoothed loudness.
- SFZ EQ and random variation are not part of the current deterministic subset; resampling is linear.
- Machine audition has passed; human A/B comparison and blinded listening still require review.

See [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [试听核验.json](试听核验.json) for evidence.
