[中文](README.md) | [English](README.en.md)

# Choir Pad

The default implementation is deterministic `choir_pad` (engine `1.0.0`, `formal`). Three voices of band-limited saw/sine excitation feed three stable state-variable band-pass formants (approximately 690, 1170, and 2680 Hz) to model a vowel cavity, followed by low-pass shaping and subtle chorus drift. It is not an ordinary pad under a different name.

- Calibrated range: MIDI 36–104, with hard boundaries.
- Controls: velocity, `expression`, `modulation`, and `sustain_pedal`; modulation increases both vibrato and pre-formant filter motion.
- Reproducibility: fixed seed `275438921`.
- Audition score: `examples/合唱铺底_程序合成.events.json`.
- Status: after machine regression passes, vowel naturalness still requires human judgment; the audition remains pending.

The emergency fallback mapping is GeneralUser GS bank `0` / program `91`. It is active only when the caller explicitly selects a SoundFont and never degrades automatically.
