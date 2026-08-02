[简体中文](README.md) | **English**

# Tremolo Strings

This is the dedicated tremolo entry point for VPO `all-strings-SEC-tremolo.sfz`. Its default and only articulation is `tremolo`; it does not substitute ordinary sustains or a synthesizer.

- Sounding/written range: C1–A7 (MIDI 24–105).
- 116 mapped regions and 96 deduplicated WAV files. The viola and violin each retain two genuine source layers that sound simultaneously.
- The 90 cello and upper-register regions read embedded WAV loops. The 26 low double-bass regions end naturally with their finite recordings.
- All 96 tremolo root samples were individually calibrated under harmonic constraints, with a median deviation of `-0.622 cents` and a maximum raw deviation of `51.890 cents`.
- Supports A4 tuning, fractional MIDI/Hz pitch, velocity, expression, sustain-pedal release, and deterministic rendering.

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/颤弓弦乐/乐器.json `
  --events examples/颤弓弦乐_奏法.events.json `
  --output output/颤弓弦乐_奏法_candidate.wav
```

## Known limitations of this single-timbre `formal` entry

- The upstream tremolo mapping has no sequential round robin or multiple velocity layers; velocity is implemented as a deterministic amplitude response.
- The double-bass recordings have no loops. Very long notes will require hand-auditioned seamless loops in a future revision.
- The original mapping's random variation, EQ, and all envelope modulation have not yet been reproduced completely. There is no tremolo-rate control, sul ponticello/sul tasto position, or independent release sample.
- The single-timbre audition for the currently bound version has passed, so its status is `formal`; linear resampling, extended capabilities, and blinded ensemble audition still require review.

Resource evidence is available in [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [试听核验.json](试听核验.json).
