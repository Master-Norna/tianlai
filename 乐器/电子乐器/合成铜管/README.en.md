[中文](README.md) | [English](README.en.md)

# Synth Brass

The default `synth_brass` (engine `1.0.0`, `formal`) uses four voices of band-limited saw/pulse waves as a reed-like excitation, adding a very short pitch transient, a velocity-sensitive filter envelope, and stronger soft saturation to create the “blown” attack of synth brass.

- Calibrated range: MIDI 36–96, with hard boundaries.
- Controls: velocity, `expression`, `modulation`, and sustain pedal.
- Fixed seed: `2236067977`.
- Audition score: `examples/合成铜管_程序合成.events.json`.
- Status: ensemble use and long-duration context remain pending; this patch does not claim to replace real brass samples.

Explicit emergency fallback: GeneralUser GS bank `0` / program `62`; no silent degradation occurs.
