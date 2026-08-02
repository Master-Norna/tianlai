[简体中文](README.md) | **English**

# Steelpan (`formal`, deterministic modeling)

Steelpan modal model with beating between detuned harmonic pairs (±4 cents), a ping attack, and bright overtones. It is implemented in `tianlai/modeled_instruments.py` (profile `steelpan`, engine 1.1.0) with explicit seed 41037. The same event sequence always produces the same output; there is no silent fallback to a general-purpose SoundFont.

## Reason for modeling (honest disclosure)

The sound-production mechanism is well suited to modeling; comparison against genuine samples remains a future fidelity upgrade.

## Source and licensing

Project-developed deterministic DSP. The engine source-file SHA-256 and all parameters are available in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py).

## Range

A3(57) - F6(89)

## Tuning

Self-calibration: 3 probe notes were rendered and measured by FFT, with a maximum error of 0.043 cents (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/钢鼓_奏法.events.json`; render duration 11.89 s, peak 0.365621, RMS 0.068626, clipping 0; WAV SHA-256 `03c2b189…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

The deterministic modal model is not a recording; it models a single pan and has no ensemble-width model for a steelband. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
