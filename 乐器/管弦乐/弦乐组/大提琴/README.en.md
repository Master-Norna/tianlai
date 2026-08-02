[简体中文](README.md) | **English**

# Cello

Based on the solo cello from Virtual Playing Orchestra 3.3, this entry reads the upstream SFZ regions, embedded WAV loops, and independent release samples directly.

## Current capabilities

- Genuine recorded samples cover C2–A5 (MIDI 36–81).
- All 9 sustain root samples have embedded loops and independently measured tuning.
- Five articulations: `sustain`, `slow_sustain`, `staccato`, `pizzicato`, and `accent`.
- 48 staccato regions preserve two genuine Round Robin bow-direction variations.
- 21 pizzicato regions.
- 10 release-tail regions, triggered independently by register when a sustained note is released.
- `expression` continuously controls loudness from 0 to 1 with smoothed transitions.
- On-demand sample decoding, sample-accurate scheduling, and deterministic rendering.

## Articulation events

```json
{ "time": 0.0, "type": "articulation", "name": "slow_sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.7 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 48, "velocity": 0.8 }
{ "time": 1.5, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

An articulation event affects only notes that begin afterward. Notes already sounding retain their original articulation.

## Tuning verification

The measured sustained A3 render is approximately `220.028 Hz / +0.220 cents`. If the upstream samples are replaced, regenerate the calibration table with:

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/大提琴/校准音准.py
```

## What is not being represented as “100% reproduction”

- `expression` currently changes loudness continuously, but this solo sample set has no multilayer dynamic timbres to crossfade.
- Vibrato is present in the recordings and its rate and depth cannot yet be controlled independently.
- Legato is approximated with fast attacks, overlapping notes, and release tails; there are no genuine string-change or bow-change transition samples.
- The current source has no dedicated solo-cello tremolo, harmonics, sul ponticello, or sul tasto samples.
- The upstream mapping's small random pitch, loudness, and delay variations are not enabled.
- The core sampler still uses linear resampling.

See [来源.en.md](来源.en.md) for the sample source and licensing.

## Obtaining the samples

If the root-level `音源/VirtualPlayingOrchestra` directory has already been downloaded for the violin, there is no need to run this again. In a new environment, run:

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/管弦乐/弦乐组/大提琴/获取音源.ps1
```

## Rendering example

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/大提琴/乐器.json `
  --events examples/大提琴_奏法.events.json `
  --output output/大提琴_奏法.wav
```
