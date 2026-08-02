[中文](README.md) | [English](README.en.md)

# Helicopter

A `formal` rotorcraft model: four-blade main-rotor impulses, rotor-disc
subharmonics, twin-engine harmonics, and low-pass turbulence form a sustained
sound field.

- `modulation` controls rotor/engine speed, `expression` and velocity control energy, and `distance` models listening distance.
- `note_off` shuts down with a 1.4-second decay; a fixed seed makes the turbulence reproducible.
- The currently bound version has passed single-instrument/single-scene listening review and is therefore `formal`. It is not yet bound to a specific model, blade count, tail rotor, Doppler trajectory, or real cabin/exterior impulse response; ensemble and extended-duration contexts still await review.
