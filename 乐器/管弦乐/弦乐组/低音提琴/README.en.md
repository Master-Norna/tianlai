[简体中文](README.md) | **English**

# Double Bass

This `formal` solo double bass is based on Virtual Playing Orchestra 3.3. It reads VPO SFZ files and independent WAV multisamples directly and never falls back silently to a GM SoundFont.

## Current capabilities

- The samples actually sound from C1–G4 (MIDI 24–67); double-bass notation is one octave higher, C2–G5.
- 12 sustain regions, all preserving embedded WAV loops.
- Five articulations: `sustain`, `slow_sustain`, `staccato`, `pizzicato`, and `accent`.
- 22 staccato regions preserve two deterministic Round Robins, plus 21 independent pizzicato regions.
- Following the upstream mapping, `accent` layers a bow attack over a sustain layer delayed by `120 ms`.
- The 12 sustain root samples have individual tuning tables, with a median deviation of `+0.210 cents` and a maximum absolute deviation of `2.701 cents`.
- At a fixed velocity in isolated renders, the E1 root sample used for MIDI 39–40 is approximately `6–7 dB` quieter than adjacent regions. The manifest records a reproducible `+6.25 dB` correction by asset-relative path and explicitly limits it to the `SOLO` variant, without altering the upstream SFZ or WAV. Valid `SEC` variants do not incorrectly receive the SOLO rule, while an incorrect path within the same variant is still rejected.
- `expression` smoothly controls loudness, while `sustain_pedal` delays release after a sustained note is released.
- A dedicated parser supports VPO's unquoted `Solo Contrabass` path containing a space under Windows directories with Chinese characters.

## Articulation events

```json
{ "time": 0.0, "type": "articulation", "name": "slow_sustain" }
{ "time": 0.0, "type": "control", "name": "expression", "value": 0.7 }
{ "time": 0.0, "type": "note_on", "note_id": 1, "midi_note": 36, "velocity": 0.8 }
{ "time": 1.5, "type": "note_off", "note_id": 1, "release_velocity": 0.5 }
```

## Tuning and rendering

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/低音提琴/校准音准.py

.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/低音提琴/乐器.json `
  --events examples/低音提琴_奏法.events.json `
  --output output/低音提琴_奏法.wav
```

## Capabilities not implied by single-timbre `formal` status

- VPO has no `bass-SOLO-tremolo.sfz`. The existing `bass-SEC-tremolo.sfz` is a section ensemble, and this implementation does not misrepresent it as solo tremolo.
- Sustains have only one recorded velocity layer. `expression` can change loudness smoothly but cannot produce a genuine sustained-timbre transition.
- Random pitch, loudness, and delay adjustments from the original mapping are disabled to preserve deterministic rendering.
- There are no independent release samples, genuine legato transitions, bow/string changes, harmonics, or sul ponticello samples.
- The single-timbre audition for the currently bound version has passed, so its status is `formal`; linear resampling and ensemble dimensions still require more detailed acceptance testing.

See [来源.en.md](来源.en.md) and [资源核验.json](资源核验.json) for the frozen resources and licensing.
