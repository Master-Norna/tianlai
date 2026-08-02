[简体中文](README.md) | **English**

# Piccolo

This `formal` solo piccolo is based on Virtual Playing Orchestra Standard 3.3 / Wave 3.2 and directly reads SOLO SFZ/WAV resources. It never falls back silently to a GM SoundFont.

## Range and transposition convention

- **Sounding sample range**: D5–C9 (MIDI 74–108).
- Written range: D4–C8 (MIDI 62–96).
- A piccolo sounds one octave above written pitch: `实音 = 记谱音 + 12 半音` (sounding pitch equals written pitch plus twelve semitones).
- `midi_note` at the base instrument layer always accepts sounding pitch. Therefore, written D4 must be converted by the collaboration layer to sounding D5 (74), with no duplicate transposition across the two layers.

## Current capabilities

- 10 sustain root samples and 1 recorded velocity layer, all preserving embedded loops.
- Four articulations: `sustain`, `slow_sustain`, `staccato`, and `accent`; accent splits and triggers the upstream short attack and sustain layers simultaneously.
- All 10 root samples are measured and calibrated, with a median deviation of `+6.821 cents` and a maximum absolute deviation in the original samples of `19.521 cents`.
- Smoothed `expression` and `breath` controls; solo note changes use a short cross-release.
- Resource, tuning, test, and fixed-audition regressions cover Windows paths containing Chinese characters and spaces.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/短笛/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/短笛/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/短笛/乐器.json `
  --events examples/短笛_奏法.events.json `
  --output output/短笛_奏法_candidate.wav
```

## Capabilities not implied by single-timbre `formal` status

- There is one recorded velocity layer and no Round Robin; staccato is created from sustain samples with offsets and a short envelope.
- There is no genuine legato, breath, key noise, or independent release, and `breath` does not yet alter timbre.
- SFZ random variation and some EQ are not included in the deterministic subset; resampling is linear.
- Although the extreme high register is covered by the upstream SFZ mapping, aliasing and timbral stretching still need focused human listening; human A/B review remains pending.

See [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [试听核验.json](试听核验.json) for evidence.
