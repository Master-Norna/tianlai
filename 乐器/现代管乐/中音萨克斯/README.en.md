[中文](README.md) | [English](README.en.md)

# Alto saxophone

This is a dedicated sample-based `formal` instrument built on
`sfzinstruments/MTG.SoloSax`. By default it reads real FLAC files and upstream
SFZ mappings and never silently substitutes a GM SoundFont when resources are
missing.

## Current capabilities

- Events use sounding pitch: the sampled sounding range is Db3–A5 (MIDI 49–81). The written range of the E♭ alto saxophone is Bb3–F#6 (58–90), converted by `sounding pitch = written pitch - 9 semitones`.
- 33 chromatic keys, 2 genuine velocity layers, and 3 deterministic RR mappings per note produce 198 pitched regions. By upstream design, RR2/RR3 transpose real recordings from adjacent keys; not every key was independently recorded 3 times.
- All 66 deduplicated pitched FLAC files read their `riff/smpl` loops; 68 breath-noise and 36 key-noise recordings are also connected.
- Playback tuning follows the upstream `key/pitch_keycenter + tune` exactly. Per-file FFT diagnostics over the loops of all 66 files have a median relative residual of `+0.754 cents` and a maximum absolute difference of `4.277 cents`.
- `expression` and `breath` are smooth loudness controls. `modulation` implements deterministic vibrato according to upstream settings: up to 50 cents, about 5 Hz, with a 2-second fade-in. `noise` controls the real noise layer.
- Supports `note_off` and `sustain_pedal`. Chinese and space-containing Windows paths load correctly, and rendering is byte-for-byte reproducible.

## Event convention

```json
{ "time": 0.0, "type": "articulation", "name": "legato" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.8 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.75 }
{ "time": 0.0, "type": "control", "name": "modulation", "value": 0.4 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 65, "velocity": 0.8 }
{ "time": 1.2, "type": "note_off", "note_id": 1 }
```

`midi_note` is always sounding pitch. The collaboration layer can later read
`written_to_sounding_semitones=-9` to convert written pitch to sounding pitch.

```powershell
.\.venv\Scripts\python.exe 乐器/现代管乐/中音萨克斯/核验资源.py
.\.venv\Scripts\python.exe 乐器/现代管乐/中音萨克斯/校准音准.py
.\.venv\Scripts\python.exe 乐器/现代管乐/中音萨克斯/核验试听.py
```

## Capabilities not implied by single-instrument `formal` status

- `legato` uses a 20,000-frame offset into the same upstream sustain sample, a 50 ms attack, and a short cross-release. It is pseudo-legato, not a recorded interval transition.
- Velocity layers are selected discretely, without a continuous timbral crossfade. `breath`/`expression` currently apply no continuous filtering or spectral transformation.
- The noise pool uses every real recording but flattens the upstream “4 sequential groups + random selection within each group” into a deterministic cycle and triggers it lightly on note-on/note-off.
- The currently bound version has passed single-instrument listening review and is therefore `formal`. The original material remains mono, with no multiple microphones, room positions, or separate release layer; manual A/B and blind ensemble listening still await review.

For provenance and reproducible evidence, see [来源.md](来源.en.md),
[资源核验.json](资源核验.json), [音准校准.json](音准校准.json), and
[试听核验.json](试听核验.json).
