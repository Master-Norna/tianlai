[中文](README.md) | [English](README.en.md)

# Synth Bass

The default `synth_bass` (engine `1.0.0`, `formal`) combines a PolyBLEP saw, variable-pulse-width square wave, and fundamental sine before soft saturation and a highly resonant low-pass filter. A fast, exponentially decaying filter envelope supplies the bass “bite,” while a short ADSR suits rhythmic lines.

- Calibrated range: MIDI 24–72, with hard boundaries.
- Controls: velocity drives level, `expression` provides continuous control, `modulation` adds subtle pitch and filter motion, and the sustain pedal is supported.
- Reproducibility: two voices with fixed seed `3187682451`.
- Audition score: `examples/合成器低音_程序合成.events.json`.
- Status: low-frequency translation across different speakers, ensemble use, and long-duration context remain pending.

Explicit emergency fallback: GeneralUser GS bank `0` / program `38`; the default manifest does not load a SoundFont.
