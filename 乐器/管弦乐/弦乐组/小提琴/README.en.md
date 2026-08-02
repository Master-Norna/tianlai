[简体中文](README.md) | **English**

# Violin

Based on the first solo violin from Virtual Playing Orchestra 3.3, this entry reads the upstream SFZ region mappings and embedded WAV loop points directly.

## Current capabilities

- The upstream SFZ key mapping covers G3–A7 (MIDI 55–105), but the highest native sustain root sample for both SOLO and SEC is only B♭6 (MIDI 94).
- 30 sustain root samples have individual measured tuning tables rather than blindly following the upstream coarse offsets.
- Six articulations: `sustain`, `slow_sustain`, `staccato`, `pizzicato`, `tremolo`, and `accent`.
- Staccato preserves the two upstream Round Robins with reproducible selection.
- Sustains and tremolo use embedded WAV `smpl` loops and do not stop abruptly at the end of a sample.
- `expression` ranges from 0 to 1 with internal smoothing.
- Samples are decoded on demand; the complete orchestral library is not loaded at startup.
- Sample-accurate event scheduling and deterministic rendering.

## Core and extended ranges

`range_profiles` distinguishes “the upstream mapping can still sound here” from “the current source qualifies as a high-fidelity candidate”:

- MIDI 55–94 is the current high-fidelity candidate core for the default SOLO + `sustain` configuration.
- MIDI 95–105 is a physical/mapping extension. Every note progressively transposes the same B♭6 sample upward, reaching `+11` semitones at A7. Compatibility mode remains available for stress tests that explicitly need the extreme register, but this does not justify a high-fidelity claim.
- SEC, other articulations, or a changed `release_seconds` value do not inherit the SOLO + `sustain` conclusion and remain unaudited until evidence is completed for each configuration.

This range is currently a `contract_candidate`, not a human-approved conclusion. The ordinary working range is not invalidated by the lower status of the extension.

## Event usage

Articulation events affect notes that begin afterward. Notes already sounding retain their original articulation.

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 69, "velocity": 0.8 }
{ "time": 1.0, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

## What is not being represented as “100% reproduction”

- `expression` currently changes loudness continuously, but this solo sample set has no multilayer velocity timbres for continuous crossfading.
- Vibrato is already present in the recordings, and its delay, rate, and depth cannot yet be controlled independently.
- `accent` is deterministic layering of a staccato transient and a sustain.
- Legato currently relies on faster attacks and overlapping notes; there are no genuine bow-change or string-change transition samples.
- The upstream SFZ's small random pitch, loudness, and delay variations are not enabled. Round Robin scheduling is reproducible, while these two staccato mappings actually reference the same set of waveforms.
- The core sampler still uses linear resampling and will later be replaced with band-limited resampling.
- There are no new native sustain roots above B♭6. Until a lawful source is added or a higher-quality remapping is accepted, MIDI 95–105 remains an explicitly labeled extended-risk range.

## Tuning verification

The measured sustained A4 render is approximately `440.013 Hz / +0.051 cents`. If the upstream samples are replaced, regenerate the calibration table with:

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/小提琴/校准音准.py
```

The single-note verification score is `examples/小提琴_A4_音准.events.json`.

See [来源.en.md](来源.en.md) for the sample source and licensing.

## Obtaining the samples

Sample assets are not placed under project source control. In a new environment, run:

```powershell
powershell -ExecutionPolicy Bypass -File 乐器/管弦乐/弦乐组/小提琴/获取音源.ps1
```

The script supports resumable downloads and merges the wave files and SFZ files into the root-level `音源/VirtualPlayingOrchestra` directory so that woodwinds, brass, and the other strings can reuse them without another download.

## Rendering example

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/小提琴/乐器.json `
  --events examples/小提琴_奏法.events.json `
  --output output/小提琴_奏法.wav
```
