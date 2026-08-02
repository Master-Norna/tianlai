[中文](README.md) | [English](README.en.md)

# Ocean waves

A `formal` ocean-wave field model: the left and right channels use swell
envelopes with different periods to modulate low-frequency water body and
high-frequency foam noise separately. It can continue indefinitely with no
sample-loop seam.

- `note_on` starts the wave field and `note_off` applies a long fade; velocity/`expression` control wave intensity and `distance` controls listening distance.
- A fixed seed and continuous state guarantee deterministic offline rendering; this unpitched effect ignores MIDI pitch.
- The currently bound version has passed single-instrument/single-scene listening review and is therefore `formal`. It does not yet distinguish rocky shores, beaches, wind force, or tides and has no real multichannel coastal recording; long ensemble and extended-duration contexts still await review.
