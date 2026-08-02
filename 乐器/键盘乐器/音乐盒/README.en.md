[中文](README.md) | [English](README.en.md)

# Music box (formal, deterministic model)

A music-box steel-comb model with inharmonic partials (1/3.42/8.93 times), fast
decay, and light mechanical contact. The implementation is in
`tianlai/modeled_instruments.py` (profile `music_box`, engine 1.1.0), with the
explicit seed 41051. The same event sequence always produces the same output;
there is no silent fallback to a general-purpose SoundFont.

## Modeling rationale (honest disclosure)

The sound-production mechanism is suitable for modeling; comparison against
real samples remains a future fidelity upgrade.

## Source and license

This is project-developed deterministic DSP. The engine source SHA-256 and all
parameters are recorded in [`资源核验.json`](资源核验.json); use
[`核验资源.py`](核验资源.py) to recompute them.

## Range

C5(72) - G7(103)

## Tuning

Self-test calibration renders 3 probe notes and measures them by FFT. The
maximum error is 0.024 cents (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/音乐盒_奏法.events.json`;
render duration 11.89 s, peak 0.415550,
RMS 0.102922, clipping 0;
WAV SHA-256 `8c66c99e…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

This is a deterministic model, not a recording, and it has no looping noise
from a spring-driven transport. The currently bound version has passed
single-instrument listening review and is marked `formal`; ensemble use, the
complete articulation set, and real repertoire remain untested.
