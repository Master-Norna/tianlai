[中文](README.md) | [English](README.en.md)

# Gunshot

A `formal` one-shot impact model: a muzzle-pressure impulse, broadband blast,
low-frequency muzzle resonance, mechanical tail, and three groups of discrete
reflections combine into a natural decay.

- `note_on` triggers the complete 2.4-second model; an early `note_off` does not truncate the one-shot impact.
- Velocity/`expression` control energy and `distance` controls distance attenuation; a fixed seed makes noise and reflections fully reproducible.
- MIDI pitch does not alter the gunshot, preventing this unpitched impact from being treated as a melodic instrument.
- The currently bound version has passed single-instrument/single-scene listening review and is therefore `formal`. This is a generic acoustic model and does not represent a specific firearm, caliber, suppressor, or real space; identity and ensemble context still await review.
