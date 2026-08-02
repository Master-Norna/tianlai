[中文](README.md) | [English](README.en.md)

# Metallic Pad

The default `metallic_pad` (engine `1.0.0`, `formal`) uses frequency modulation at the non-integer ratio `√2`, ring modulation, and second-order modulator sidebands to create a bell-like inharmonic spectrum. Five-voice micro-detuning and a slow ADSR extend the transient metallic spectrum into a pad.

- Calibrated range: MIDI 30–104, with hard boundaries.
- Controls: velocity, `expression`, `modulation`, and `sustain_pedal`.
- Fixed seed: `2449489742`.
- Audition score: `examples/金属铺底_程序合成.events.json`.
- Status: ensemble use and long-duration context remain pending.

Explicit emergency fallback: GeneralUser GS bank `0` / program `93`; the default implementation has no SoundFont dependency.
