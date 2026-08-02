[中文](README.md) | [English](README.en.md)

# Synth Strings

The default `synth_strings` (engine `1.0.0`, `formal`) uses a seven-voice band-limited saw/pulse string ensemble, extremely low-level fixed-seed bow noise, a slower attack, and ensemble vibrato. Key-tracked filtering suppresses harshness in the upper register while retaining the sustained brightness of synthesized strings.

- Calibrated range: MIDI 36–100, with hard boundaries.
- Controls: velocity, smoothed `expression`, `modulation`, and `sustain_pedal`.
- Fixed seed: `1618033988`.
- Audition score: `examples/合成弦乐_程序合成.events.json`.
- Status: ensemble use and long-duration context remain pending; this is explicitly a synth-string patch and does not impersonate real string samples.

Explicit emergency fallback: GeneralUser GS bank `0` / program `50`.
