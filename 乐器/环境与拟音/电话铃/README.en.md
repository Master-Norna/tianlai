[中文](README.md) | [English](README.en.md)

# Telephone bell

A `formal` electromechanical two-bell telephone model: 20 Hz striker impulses
excite two metal bells at approximately 820/1040 Hz, using a repeating cadence
of 2 seconds ringing and 4 seconds silent.

- `modulation` changes the detuning between the two bells within a small range, velocity/`expression` control loudness, and `distance` controls listening distance.
- `note_off` stops the mechanism and lets the metallic tail fade; a fixed seed guarantees determinism.
- The currently bound version has passed single-instrument/single-scene listening review and is therefore `formal`. It currently targets only a generic old electromechanical bell and does not cover country-specific ring cadences, electronic ringtones, or recordings of particular telephone enclosures.
