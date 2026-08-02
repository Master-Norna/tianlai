[中文](README.md) | [English](README.en.md)

# Halo Pad

The default implementation is Tianlai's deterministic procedural synthesizer `halo_pad` (engine `1.0.0`, quality level `formal`). A wide stereo cluster of seven sine voices passes through a slow filter LFO; second and third harmonics plus subtle phase modulation create an elevated “halo,” while a slow attack and long release support sustained harmony.

- Calibrated range: MIDI 30–108; out-of-range events produce an explicit error.
- Performance response: velocity controls initial energy, `expression` controls continuous volume, `modulation` deepens vibrato and filter motion, and `sustain_pedal` is supported.
- Reproducibility: fixed seed `1742049361`; identical manifests, events, sample rates, and engine versions produce sample-identical output.
- Audition score: `examples/光环铺底_程序合成.events.json`.
- The pinned version has passed a single-timbre audition and is therefore `formal`; long-duration motion and use in a mix still require review.

A general-purpose SoundFont is retained only as an explicit emergency fallback: GeneralUser GS, bank `0`, program `94`. Select a separate SoundFont manifest when fallback is needed; this manifest never degrades silently.
