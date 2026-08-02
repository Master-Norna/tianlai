[简体中文](README.md) | **English**

# Pizzicato Strings

This is the dedicated pizzicato entry point for the VPO `all-strings-SEC-pizzicato.sfz`. It is not a renamed string ensemble, and it does not accept sustain or tremolo articulations.

- The default and only articulation is `pizzicato`; sounding/written range: C1–A7 (MIDI 24–105).
- 63 genuine plucked-string regions: 12 double bass, 24 cello, 13 viola, and 14 violin.
- The four sections use the same range crossfades as the upstream mapping; short notes play through the natural tails of the original WAV files.
- All 63 root samples for this articulation were calibrated individually, with a median deviation of `4.707 cents` and a maximum raw deviation of `108.394 cents`. The low-register RR2 files that are approximately 100 cents off are corrected upstream with `transpose=-1`; this implementation reproduces the equivalent correction from the measured root pitches.
- Supports A4 tuning, fractional pitch, velocity, expression, and deterministic playback.

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/弦乐组/拨奏弦乐/乐器.json `
  --events examples/拨奏弦乐_奏法.events.json `
  --output output/拨奏弦乐_奏法_candidate.wav
```

## Known limitations of this single-timbre `formal` entry

- The upstream mapping has no true `seq_position` pizzicato round robin. In the low register, files named RR1/RR2 are alternately assigned to adjacent root notes, so they must not be represented as per-note alternation.
- The cello portion contains overlapping low-velocity layers. The current sampler selects one layer discretely in the overlap rather than mixing both SFZ layers simultaneously.
- There is no left-hand damping, Bartók pizzicato, fingerboard/bridge position control, independent release sample, or real section-size control.
- The single-timbre audition for the currently bound version has passed, so its status is `formal`; linear resampling, extended capabilities, and blinded ensemble audition still require review.

Resource evidence is available in [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), and [试听核验.json](试听核验.json).
