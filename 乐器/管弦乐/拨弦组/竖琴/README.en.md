[简体中文](README.md) | **English**

# Harp

This `formal` concert harp is based on **VCSL Concert Harp**. The current implementation loads 45 CC0 stereo pluck recordings covering 23 recorded root notes. The backend type is still named `vpo_harp` only as a historical compatibility name for the existing event protocol; it does not mean that VPO samples are still in use.

## Current semantics

- Input consistently uses concert pitch. Runtime is strictly confined to the E1–F♯7 (MIDI 28–102) range covered by the VCSL mapping and does not invent unsampled physical endpoints.
- Of the 23 recorded roots, the lowest E1 has one recording, while each of the other 22 roots has two recordings at different velocities. This means 45 recordings with at most two layers per root, not “three velocity layers” or round robin.
- The velocity crossfades in the VCSL SFZ are currently represented by deterministic switching at their midpoints. Exhaustive checking of integer MIDI velocities 0–127 across the complete range confirms that exactly one region is selected each time, but the current sampler does not yet implement continuous crossfading.
- `open` and `sustain` are aliases for a naturally open string, using the full original tail and a 30-second key-release envelope. `dampened` reuses the same pluck recordings with a 350 ms engineering envelope to approximate palm damping.
- `sustain_pedal` is a collaboration-layer abstraction for “hold/unified damping”: while it is down, note-off is deferred; releasing it releases strings awaiting damping. It is **not** the seven real pitch pedals of a harp.
- Tuning was measured per file for all 45 recordings. Runtime uses each recording's measured frequency as its root pitch instead of treating the coarse-tuning value from the upstream SFZ as final calibration.
- Of the 42 nonzero upstream attack offsets, 41 reasonable silence trims are retained. Only the 3744-frame offset for `KSHarp_D4_f1.wav` is overridden because it removes the main pluck attack. The original VCSL SFZ is not modified.
- Engineering gain is `0.38`, retaining at least 6 dB of single-note headroom after joint verification against sample peaks and SFZ `volume`.
- Supports A4 tuning, fractional MIDI/Hz pitch, velocity, expression, natural WAV tails, and deterministic rendering.
- The selected VCSL version, SFZ, 45 WAV files, and license evidence are pinned by SHA-256; the license is CC0 1.0.

```powershell
.\.venv\Scripts\python.exe -m tianlai render `
  --instrument 乐器/管弦乐/拨弦组/竖琴/乐器.json `
  --events examples/竖琴_奏法.events.json `
  --output output/竖琴_奏法.wav
```

## Known limitations of this single-timbre `formal` entry

- It does not automatically solve seven-pedal enharmonic spelling, double-pedal changes, or pedal glissandi, and it does not prevent enharmonically named notes that cannot coexist under a real pedal configuration. A future collaboration layer should check these constraints.
- There are no genuine upward/downward glissando recordings, fingernail sounds, soundboard strikes, bisbigliando, harmonics, or independent damping samples.
- `dampened` reuses the same plucked WAV files with a shorter envelope to approximate palm damping.
- There is no round robin, so repeated notes do not automatically alternate samples. Velocity crossfades currently switch at the midpoint, and pitch shifting still uses linear resampling.
- The single-timbre audition for the currently bound version has passed, so its status is `formal`; comparison with real performers and blinded ensemble audition still require review, and it is not described as “100% reproduction.”

See [来源.en.md](来源.en.md), [资源核验.json](资源核验.json), [音准校准.json](音准校准.json), and [试听核验.json](试听核验.json) for resource evidence, per-file hashes, and range/tail/clipping/offset verification.
