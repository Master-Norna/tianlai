[简体中文](README.md) | **English**

# Synth Drum (`formal`, deterministic modeling)

Analog-drum-machine-style synth drum: an exponentially pitch-decaying sine wave, click transient, and short noise burst, with the score pitch setting the fundamental. It is implemented in `tianlai/modeled_instruments.py` (profile `synth_drum`, engine 1.1.0) with explicit seed 41034. The same event sequence always produces the same output; there is no silent fallback to a general-purpose SoundFont.

## Reason for modeling (honest disclosure)

This timbre is programmatically synthesized by definition, so modeling is the appropriate approach.

## Source and licensing

Project-developed deterministic DSP. The engine source-file SHA-256 and all parameters are available in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py).

## Range

C2(36) - C6(84)

## Tuning

Self-calibration: 3 probe notes were rendered and measured by FFT, with a maximum error of 0.366 cents (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/合成鼓_奏法.events.json`; render duration 11.89 s, peak 0.419980, RMS 0.066693, clipping 0; WAV SHA-256 `bc487479…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

This follows GM Synth Drum semantics and is not a circuit simulation of any particular drum machine. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
