[简体中文](README.md) | **English**

# Clarinet

This `formal` solo clarinet is based on Virtual Playing Orchestra Standard 3.3 / Wave 3.2. By default it reads SOLO SFZ and WAV multisamples directly and never falls back silently to a GM SoundFont.

## Current capabilities

- Events use **concert pitch**. The sampled sounding range is D3–B♭6 (MIDI 50–94), while the written range for a B♭ clarinet is E3–C7 (52–96), with `实音 = 记谱音 - 2 半音` (sounding pitch equals written pitch minus two semitones).
- 26 sustain regions and 2 recorded velocity layers, currently split deterministically at normalized velocity `0.622`.
- Four articulations: `sustain`, `slow_sustain`, `staccato`, and `accent`. Following the upstream SFZ, accent triggers a short-note attack layer and a sustain layer simultaneously.
- All 26 WAV files use embedded loops; staccato is constructed by the upstream mapping from sustain samples with short envelopes.
- All 26 root samples are measured and calibrated, with a median deviation of `-0.035 cents` and a maximum absolute deviation in the original samples of `0.320 cents`.
- Both `expression` and `breath` are smoothed controls; the solo state machine applies a short cross-release on note changes.
- Windows paths containing Chinese characters and spaces load directly, samples decode on demand, and renders are byte-for-byte reproducible.

## Event conventions

```json
{ "time": 0.0, "type": "articulation", "name": "slow_sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.72 }
{ "time": 0.0, "type": "control", "name": "breath", "value": 0.65 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 69, "velocity": 0.8 }
{ "time": 1.4, "type": "note_off", "note_id": 1 }
```

`midi_note` is always concert pitch. A future collaboration layer will read `written_to_sounding_semitones=-2` and convert written pitch to sounding pitch.

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/单簧管/核验资源.py
.\.venv\Scripts\python.exe 乐器/管弦乐/木管组/单簧管/校准音准.py
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/木管组/单簧管/乐器.json `
  --events examples/单簧管_奏法.events.json `
  --output output/单簧管_奏法_candidate.wav
```

## Capabilities not implied by single-timbre `formal` status

- The two upstream velocity layers originally crossfade continuously; the current sampler selects them discretely at the midpoint. There is no independent Round Robin.
- Staccato is envelope shaping of sustain samples, not a dedicated staccato recording. There is no genuine legato transition, breath, key noise, or independent release.
- `expression` / `breath` currently control smoothed loudness and do not yet provide continuous timbral morphing.
- SFZ random pitch, loudness, and delay are not used in order to preserve determinism; resampling remains linear.
- The single-timbre audition for the currently bound version has passed, so its status is `formal`; human A/B comparison, extended capabilities, and blinded ensemble audition still require review.

See [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [试听核验.json](试听核验.json) for the frozen resources and mixed licensing.
