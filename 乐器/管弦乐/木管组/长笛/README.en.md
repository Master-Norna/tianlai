[简体中文](README.md) | **English**

# Flute

This solo flute is based on Virtual Playing Orchestra 3.3 / VSCO 2 Community Edition and uses a monophonic breath-state machine to schedule genuine recorded samples.

## Current capabilities

- 10 sustain root samples cover C4–D7 (MIDI 60–98).
- All 10 sustain samples use embedded WAV loops and independently measured tuning.
- Five articulations: `sustain`, `slow_sustain`, `legato`, `staccato`, and `accent`.
- When sustained notes overlap, the new note automatically switches to an 8 ms legato attack and the old note crossfades out over 55 ms.
- A new note shortens any old note already in its release, preventing the 0.7-second tail from creating false polyphony.
- 10 tongued samples; following SFZ semantics, the two overlapping upstream regions for G4/G♯4 sound simultaneously as layers.
- Following the upstream mapping, accent layers a tongued sample with a sustain, retaining an independent 40–120 ms delay for each sustain root.
- If an accent is released or interrupted by a new note during that delay, the delayed layer does not appear afterward.
- `expression` and `breath` are both smoothed continuous controls from 0 to 1.
- On-demand sample decoding, sample-accurate scheduling, and deterministic rendering.

## Performance events

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.8 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 69, "velocity": 0.76 }
{ "time": 1.2, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

A score can express legato with approximately 100–200 ms of note overlap. Even if the current articulation remains `sustain`, an overlapping new note automatically sounds with the internal `legato` articulation.

## What is not being represented as “100% reproduction”

- `breath` is currently an independent breath-loudness envelope, not genuine airflow noise or multilayer timbral morphing.
- There are no independent inhalation/release-tail samples; the current implementation approximates them with the original sample's 0.7-second release.
- Vibrato is recorded into the sustain samples and cannot be removed or have its rate/depth adjusted independently.
- `legato` is an approximation using short attacks and crossfades; upstream has no genuine legato-transition samples.
- The three groups of high-pass-like EQ in the upstream SFZ have not yet been implemented.
- Small random pitch, loudness, and delay from the upstream SFZ are disabled by default in favor of tuning accuracy and reproducibility.
- The core sampler still uses linear resampling.

## Tuning verification

The measured sustained A4 render is approximately `440.015 Hz / +0.061 cents`.

Regenerate the calibration table for all 10 root samples with:

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/长笛/校准音准.py
```

The single-note verification score is `examples/长笛_A4_音准.events.json`.

## Samples and rendering

See [来源.en.md](来源.en.md) for licensing and provenance. If the root-level `音源/VirtualPlayingOrchestra` directory already exists, there is no need to download it again; otherwise run:

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/管弦乐/木管组/长笛/获取音源.ps1
```

Render the comprehensive example with:

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/长笛/乐器.json `
  --events examples/长笛_奏法.events.json `
  --output output/长笛_奏法.wav
```
