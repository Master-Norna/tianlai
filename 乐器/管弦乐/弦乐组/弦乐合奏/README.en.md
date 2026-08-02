[简体中文](README.md) | **English**

# String Ensemble

This `formal` four-section string ensemble is based on Virtual Playing Orchestra 3.3. It reads the genuine WAV/SFZ resources from `all-strings-SEC-*` directly and never falls back silently to GM.

## Current capabilities

- Concert-pitch input and notation; sample mappings cover C1–A7 (MIDI 24–105).
- Renders four genuine sections—double bass, cello, viola, and violin—and applies the upstream equal-power crossfades across C2–B2, C3–F3, G3–C6, and the other transition ranges.
- Five genuine SFZ articulation sets: `sustain`, `staccato`, `pizzicato`, `tremolo`, and `accent`.
- 64 sustain regions with loops; staccato and accent bow attacks preserve two deterministic Round Robins.
- Independent sample libraries in viola sustains and tremolo, and in violin tremolo, are treated as simultaneous layers rather than incorrectly interpreted as round robins.
- The 64 sustain root samples use harmonically constrained FFT calibration, with a median deviation of `-0.306 cents` and a maximum raw deviation of `22.954 cents`.
- Supports A4 tuning, fractional MIDI/Hz pitch, velocity, smoothed `expression`, and sustain-pedal release.
- Automated tests cover Windows paths containing Chinese characters and spaces, explicit missing-resource errors, and deterministic rendering.

## Usage

```powershell
.\.venv\Scripts\python.exe 乐器/管弦乐/弦乐组/弦乐合奏/校准音准.py

.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/弦乐合奏/乐器.json `
  --events examples/弦乐合奏_奏法.events.json `
  --output output/弦乐合奏_奏法_candidate.wav
```

## Capabilities not implied by single-timbre `formal` status

- Sustains, pizzicato, and most tremolo have only one recorded velocity layer. Velocity currently follows an amplitude curve, while the overlap between the two staccato-cello layers is selected discretely.
- Random pitch, loudness, and delay from the original SFZ are disabled for reproducibility. There are currently no independent releases, genuine legato transitions, bow changes, sul ponticello, or harmonics.
- The 26 double-bass tremolo WAV files have no loop metadata and end at their natural recorded length; the other 90 tremolo regions use embedded loops.
- SFZ EQ and continuous velocity-to-attack/release modulation have not yet been modeled completely, and resampling remains linear.
- The single-timbre audition for the currently bound version has passed, so its status is `formal`; extended capabilities, orchestration balance, and blinded ensemble audition still require review.

See [来源.en.md](来源.en.md) and [资源核验.json](资源核验.json) for resource versions, licenses, and aggregate hashes.
