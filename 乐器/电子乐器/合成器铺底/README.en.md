[中文](README.md) | [English](README.en.md)

# Synth Pad

The default `broad_pad` (engine `1.0.0`, `formal`) uses six voices of band-limited supersaw, fundamental sine, and second harmonic, spread across a wide stereo field and then passed through a slow key-tracked low-pass filter. It emphasizes width and chord density, using a source topology and envelope distinct from the halo, warm, and sweep pads.

- Calibrated range: MIDI 24–108, with hard boundaries.
- Controls: velocity, smoothed `expression`, `modulation`, and `sustain_pedal`.
- Fixed seed: `1414213562`.
- Audition score: `examples/合成器铺底_程序合成.events.json`.
- Status: ensemble use and long-duration context remain pending.

The explicit SoundFont fallback is GeneralUser GS bank `0` / program `88`; it never silently replaces the current implementation.
