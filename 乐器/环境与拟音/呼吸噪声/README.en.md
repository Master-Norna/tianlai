[中文](README.md) | [English](README.en.md)

# Breathing noise

`formal` programmatic model: independent left/right turbulent noise is shaped
by breathing bandwidth and vocal-tract resonance, then modulated by a slow
breathing pulse. It no longer loads a placeholder GM timbre.

- `note_on` starts the airflow, and `note_off` applies an exhalation-like fade; velocity controls the initial airflow.
- `expression` controls level, `modulation` changes airway resonance, `distance` controls distance attenuation, and the sustain pedal is supported.
- A fixed seed makes repeated renders sample-identical; this unpitched effect intentionally ignores MIDI pitch.
- The currently bound version has passed single-instrument/single-scene listening review and is therefore `formal`. It does not currently model real human inhalation/exhalation phases, switching between mouth and nose, sex, or close-miked recordings; the ensemble context remains `untested`.
