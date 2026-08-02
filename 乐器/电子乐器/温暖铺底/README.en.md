[中文](README.md) | [English](README.en.md)

# Warm Pad

The default `warm_pad` (engine `1.0.0`, `formal`) is built primarily from sine waves, mixed with a small amount of band-limited saw and second harmonic, then passed through gentle saturation, a low-cutoff filter, and five narrowly detuned voices for a rounded body. Its filter moves less than the sweep pad, and it contains fewer harmonics than the broad pad.

- Calibrated range: MIDI 24–108, with hard boundaries.
- Controls: velocity, smoothed `expression`, `modulation`, and `sustain_pedal`.
- Fixed seed: `3141592653`.
- Audition score: `examples/温暖铺底_程序合成.events.json`.
- Status: ensemble use and long-duration context remain pending.

Explicit emergency fallback: GeneralUser GS bank `0` / program `89`; the default manifest does not load a SoundFont.
