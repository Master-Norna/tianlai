[中文](README.md) | [English](README.en.md)

# Celesta

A `formal` real Celesta multisample based on Virtual Playing Orchestra 3.3.
The implementation reads the dedicated `Keys/celesta.sfz` and WAV files and no
longer substitutes a GM glockenspiel or general-purpose SoundFont.

## Current capabilities

- Input is treated as sounding pitch over C4–C8 (MIDI 60–108). Conventional celesta notation sounds one octave higher than written, so the manifest also records the written range C3–C7.
- 20 deduplicated WAV files and 21 SFZ regions: 11 soft-layer regions and 10 hard-layer regions.
- Reproduces the upstream soft-layer `0–95` fade-out and hard-layer `63–127` fade-in, using an equal-power crossfade in the overlap.
- Supports the A4 reference, fractional MIDI/Hz pitch, velocity, smooth `expression`, and sustain pedal.
- The 20 root samples are calibrated with a harmonically constrained FFT: median deviation `4.6005 cents`, maximum raw deviation `40.698668 cents`.
- Automated tests cover Chinese/space-containing paths, explicit missing-resource errors, and deterministic rendering.

## Usage

```powershell
.\.venv\Scripts\python.exe 乐器/键盘乐器/钢片琴/校准音准.py

.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/键盘乐器/钢片琴/乐器.json `
  --events examples/钢片琴_奏法.events.json `
  --output output/钢片琴_奏法_candidate.wav
```

## Capabilities not implied by single-instrument `formal` status

- Each pitch has only soft/hard layers, and low-velocity C4 also reuses a hard-layer WAV; there is no independent Round Robin.
- Although the upstream package includes mechanical-noise files, this SFZ models no pedal noise, key noise, key release, resonance, or half-pedaling.
- Samples are not looped; long notes decay naturally according to the real recording. Resampling is still linear.
- The currently bound version has passed single-instrument listening review and is therefore `formal`; manual blind listening and extended/ensemble dimensions still await review.

See [来源.md](来源.en.md) and [资源核验.json](资源核验.json) for the resource
version, license, and aggregate Hash.
