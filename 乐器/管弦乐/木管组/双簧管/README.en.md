[简体中文](README.md) | **English**

# Oboe

This `formal` solo oboe is based on Virtual Playing Orchestra Standard 3.3 / Wave 3.2 and directly reads SOLO SFZ and WAV resources. It never falls back silently to a GM SoundFont.

## Current capabilities

- Sounding and written range are both B♭3–A6 (MIDI 58–93), with concert-pitch event input.
- 9 sustain root samples and 1 recorded velocity layer; all 9 WAV files retain embedded loops.
- Four articulations: `sustain`, `slow_sustain`, `staccato`, and `accent`. Accent triggers the upstream short-note attack and sustain component as layers.
- All 9 root samples are measured and calibrated, with a median deviation of `+10.679 cents` and a maximum absolute deviation in the original samples of `23.929 cents`; correction is applied per sample during playback.
- Smoothed `expression` and `breath` controls; solo note changes use a short cross-release.
- Loading and fixed audition rendering cover Windows paths containing Chinese characters and spaces, with reproducible results.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/双簧管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/双簧管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/双簧管/乐器.json `
  --events examples/双簧管_奏法.events.json `
  --output output/双簧管_奏法_candidate.wav
```

## Capabilities not implied by single-timbre `formal` status

- There is only one recorded velocity layer and no independent Round Robin; velocity and breath cannot yet change timbre continuously.
- Staccato is made upstream from sustain samples using offsets and a short envelope. There is no genuine legato, breath, key noise, or release sample.
- SFZ EQ and random variation are not part of the current deterministic subset; resampling is linear.
- Machine audition has passed; human A/B comparison and blinded listening still require review.

The event format matches the clarinet; `midi_note` is concert pitch. See [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [试听核验.json](试听核验.json) for evidence.
