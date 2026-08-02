[中文](README.md) | [English](README.en.md)

# Choir aahs

A `formal` mixed-choir sustain based on Virtual Playing Orchestra 3.3 /
Sonatina Symphonic Orchestra. The implementation reads real Chorus WAV/SFZ
resources and no longer substitutes GM Choir Aahs.

## Current capabilities

- Recorded range G2–C6 (MIDI 43–84): male voices cover G2–F♯4 and female voices cover G4–C6.
- The `normal` and `sustain` SFZ mappings share 37 per-pitch WAV files; all 37 WAV files use embedded loops.
- The upstream continuous relationship between velocity and attack time is preserved. In the `normal` articulation, `modulation` (CC1 semantics) can lengthen the attack by a further 0–1 seconds.
- The per-part envelope of `0.84 s` hold, `22 s` decay, and `70%` sustain is preserved.
- Supports the A4 reference, fractional MIDI/Hz pitch, velocity, `expression`, `breath`, and sustain pedal.
- The 37 root samples are calibrated with a harmonically constrained FFT: median deviation `-2.609262 cents`, maximum raw deviation `26.900799 cents`.
- Automated tests cover Chinese/space-containing paths, explicit missing-resource errors, and deterministic rendering.

## Naming boundary

The upstream metadata says only `Choir/Chorus sustain` and provides no
verifiable vowel label. This directory retains the instrumentation-table name
“Choir aahs.” The current samples can serve perceptually as an Ah-like sustained
bed, but the project does not claim that they use a strictly uniform `/ɑː/`
pronunciation until every sample has been reviewed manually.

## Usage

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/人声乐器/合唱啊声/乐器.json `
  --events examples/合唱啊声_奏法.events.json `
  --output output/合唱啊声_奏法_candidate.wav
```

## Capabilities not implied by single-instrument `formal` status

- There is only one recorded dynamic, with no Round Robin, consonants, breaths, separate vowels, lyrics, or legato phonemes.
- Male and female voices switch adjacently near G4 rather than overlapping with a crossfade.
- The currently bound version has passed single-instrument listening review and is therefore `formal`; vowel identity, complete capability coverage, and blind ensemble listening still await review.

See [来源.md](来源.en.md) and [资源核验.json](资源核验.json) for the resource
version, license, and aggregate Hash.
