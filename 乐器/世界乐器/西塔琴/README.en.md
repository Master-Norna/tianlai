[中文](README.md) | [English](README.en.md)

# Sitar (formal, deterministic model)

An extended Karplus–Strong sitar model with jawari bridge buzz and four
sympathetic strings. The implementation is in
`tianlai/modeled_instruments.py` (profile `sitar`, engine 1.1.0), with the
explicit seed 41007. The same event sequence always produces the same output;
there is no silent fallback to a general-purpose SoundFont.

## Modeling rationale (honest disclosure)

No real samples with an unambiguous public license were found, so a
deterministic physical model currently fills this role. Real samples remain a
future fidelity upgrade.

## Source and license

This is project-developed deterministic DSP. The engine source SHA-256 and all
parameters are recorded in [`资源核验.json`](资源核验.json); use
[`核验资源.py`](核验资源.py) to recompute them.

## Range

G2(43) - C6(84)

## Tuning

Self-test calibration renders 3 probe notes and measures them by FFT. The
maximum error is 0.145 cents (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/西塔琴_奏法.events.json`;
render duration 11.89 s, peak 0.093410,
RMS 0.003783, clipping 0;
WAV SHA-256 `1e267278…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

This is a deterministic string model, not a recording. The sympathetic strings
use simplified fixed ratios, and there is no meend pitch-slide articulation.
The currently bound version has passed single-instrument listening review and
is marked `formal`; ensemble use, the complete articulation set, and real
repertoire remain untested.
