[中文](README.md) | [English](README.en.md)

# Sweep Pad

The default `sweep_pad` (engine `1.0.0`, `formal`) feeds six voices of band-limited saw, sine, and fixed-seed broadband noise into a highly resonant TPT low-pass filter. A deep filter LFO at approximately 0.087 Hz sweeps across multiple octaves, creating genuine long-period motion rather than merely renaming a static timbre.

- Calibrated range: MIDI 24–108, with hard boundaries.
- Controls: `modulation` noticeably increases sweep and vibrato depth; velocity, `expression`, and pedal are also supported.
- Fixed seed: `3678794411`.
- Audition score: `examples/扫频铺底_程序合成.events.json`.
- Status: the long-period motion and resonance peaks still require human listening review.

Explicit emergency fallback: GeneralUser GS bank `0` / program `95`; it never activates silently.
