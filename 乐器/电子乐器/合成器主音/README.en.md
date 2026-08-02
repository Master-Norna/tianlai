[中文](README.md) | [English](README.en.md)

# Synth Lead

The default `synth_lead` (engine `1.0.0`, `formal`) blends PolyBLEP pulse and saw waves with a phase-modulated sine at twice the fundamental, then applies highly resonant filtering and light drive for a forward lead tone. Its fast attack, short release, and deeper controllable vibrato suit melodies; this is not a pad with renamed parameters.

- Calibrated range: MIDI 36–108; floating-point MIDI and direct Hz input are supported, but boundaries are enforced strictly.
- Controls: velocity, `expression`, `modulation`, and `sustain_pedal`.
- Fixed seed: `2718281828`.
- Audition score: `examples/合成器主音_程序合成.events.json`.
- Status: ensemble use and long-duration context remain pending.

Explicit emergency fallback: GeneralUser GS bank `0` / program `81`; the default does not load a general-purpose sound source.
