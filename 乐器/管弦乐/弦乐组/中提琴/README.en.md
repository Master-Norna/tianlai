[简体中文](README.md) | **English**

# Viola

The current implementation is a `formal` **VSCO2-CE viola section**, not a solo viola. At runtime it is strictly confined to the pure-CC0 subtree `libs/VSCO2-CE/Strings/Viola Section` and no longer reads No Budget Orchestra, SSO, Mattias Westlund, or a GM fallback.

## Implemented source structure

- `sustain`: 12 `susvib` root samples spanning MIDI 50–86. Each root retains only one recorded `v2` layer and one take, using the embedded WAV loop.
- `spiccato`: 12 sampled roots with genuine RR1/RR2 for each, totaling 24 short-note WAV files. Each root likewise has only one recorded `v2` velocity.
- All 36 samples are 44.1 kHz stereo WAV files: 33 PCM16 and 3 PCM24. There are no clipped or silent files.
- The playable mapping is MIDI 48–93 (C3–A6). The lowest sustain C3 transposes the D3 root down 2 semitones. MIDI 85–93 share the D6 root; A6 is an upward extension, not an independent root sample.
- `velocity` controls loudness continuously and does not invent a second timbral velocity layer.
- All 36 samples use measured tuning from three analysis windows, and runtime applies the inverse correction for each file.

## Events

```json
{ "time": 0.0, "type": "articulation", "name": "sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 60, "velocity": 0.8 }
{ "time": 1.5, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

The short-bow articulation accepts only its exact name:

```json
{ "time": 2.0, "type": "articulation", "name": "spiccato" }
```

`staccato`, `pizzicato`, `accent`, `tremolo`, and `slow_sustain` are never approximated silently.

## Reproduction

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/中提琴/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/中提琴/校准音准.py
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/中提琴/核验试听.py
```

The fixed audition renders to `output/中提琴_VSCO2_CC0_candidate.wav` and covers the low register, genuine roots, upper extension, sustain loops, both short-bow round robins, velocity response, and expression.

## Known boundaries of single-timbre `formal` status

- There are no genuine multiple velocities, non-vibrato sustains, legato transitions, independent releases, pizzicato, accents, or tremolo.
- `susvib` is a vibrato section sound and must not be represented as a solo or non-vibrato viola.
- The maximum sample difference at sustain-loop seams is approximately `0.0603`. This is covered by the resource gate and long-note audition, but the current sampler has no loop crossfade.
- The top seven semitones depend on transposition of the D6 root, so timbral accuracy is lower than in ranges with independent root samples.
- The single-timbre audition for the currently bound version has passed, so its status is `formal`; linear resampling, extended range, and blinded ensemble audition still require more detailed acceptance testing.

See [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [音准校准.json](音准校准.json) for licenses and per-file evidence.
