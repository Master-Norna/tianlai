[中文](README.md) | [English](README.en.md)

# Applause

A `formal` crowd-applause model: it deterministically schedules many short,
stereo-scattered handclap micropulses over low-level crowd bed noise. It is not
a loop of one GM applause sample.

- `modulation` continuously controls clap density, velocity and `expression` control overall loudness, and `distance` controls listening distance.
- `note_on` starts the crowd; `note_off` lets the density fade naturally, and the sustain pedal can delay the ending.
- A fixed seed makes the same event document reproducible; changing the seed produces a different crowd timeline.
- The currently bound version has passed single-instrument/single-scene listening review and is therefore `formal`. Real venue early reflections, individual hand shapes, cheering layers, and ensemble context still await review.
