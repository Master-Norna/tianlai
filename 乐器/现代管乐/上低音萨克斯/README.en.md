[中文](README.md) | [English](README.en.md)

# Baritone saxophone

This is a dedicated sample-based `formal` instrument built on
`sfzinstruments/MTG.SoloSax`. By default it reads real FLAC files and upstream
SFZ mappings and never silently substitutes a GM SoundFont when resources are
missing.

## Current capabilities

- Events use sounding pitch: the sampled sounding range is C2–A4 (MIDI 36–69). The written range of the E♭ baritone saxophone is A3–F#6 (57–90), converted by `sounding pitch = written pitch - 21 semitones`.
- 34 chromatic keys, 3 genuine velocity layers, and 3 deterministic RR mappings per note produce 306 pitched regions. By upstream design, RR2/RR3 transpose real recordings from adjacent keys; not every key was independently recorded 3 times.
- All 100 deduplicated pitched FLAC files read their `riff/smpl` loops; 90 breath-noise and 40 key-noise recordings are also connected.
- Playback tuning follows the upstream `key/pitch_keycenter + tune` exactly. Each of the 100 files also receives an FFT diagnostic over its loop: the median residual relative to upstream tuning is `+2.217 cents`, and the maximum absolute difference is `25.626 cents`. Natural vibrato is pronounced, so diagnostic residuals do not overwrite the author's tune table.
- `expression` and `breath` are smooth loudness controls. `modulation` implements deterministic vibrato according to upstream settings: up to 50 cents, about 5 Hz, with a 2-second fade-in. `noise` controls the real noise layer.
- Supports `note_off` and `sustain_pedal`. Chinese and space-containing Windows paths load correctly, and rendering is byte-for-byte reproducible.

## Event convention

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.8 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.75 }
{ "time": 0.0, "type": "control", "name": "modulation", "value": 0.4 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 52, "velocity": 0.8 }
{ "time": 1.2, "type": "note_off", "note_id": 1 }
```

`midi_note` is always sounding pitch. The collaboration layer can later read
`written_to_sounding_semitones=-21` to convert written pitch to sounding pitch.

```powershell
.\.venv\Scripts\python.exe 乐器/现代管乐/上低音萨克斯/核验资源.py
.\.venv\Scripts\python.exe 乐器/现代管乐/上低音萨克斯/校准音准.py
.\.venv\Scripts\python.exe 乐器/现代管乐/上低音萨克斯/核验试听.py
```

## Capabilities not implied by single-instrument `formal` status

- `legato` uses a 20,000-frame offset into the same upstream sustain sample, a 50 ms attack, and a short cross-release. It is reliable, reproducible pseudo-legato, not a recorded interval transition.
- Velocity layers are selected discretely, without a continuous timbral crossfade. `breath`/`expression` currently apply no continuous filtering or spectral transformation.
- The noise pool uses every real recording but flattens the upstream “4 sequential groups + random selection within each group” into a deterministic cycle. Lightweight triggering on note-on/note-off is Tianlai adapter behavior.
- The currently bound version has passed single-instrument listening review and is therefore `formal`. The original material remains mono, with no multiple microphones, room positions, or separate release layer; manual A/B and blind ensemble listening still await review.

For provenance and reproducible evidence, see [来源.md](来源.en.md),
[资源核验.json](资源核验.json), [音准校准.json](音准校准.json), and
[试听核验.json](试听核验.json).
