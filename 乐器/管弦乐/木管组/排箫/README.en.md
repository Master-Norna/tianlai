[简体中文](README.md) | **English**

# Pan Flute (`formal`, deterministic modeling)

Closed-pipe pan-flute model dominated by odd harmonics, with strong edge-blown breath noise and deterministic micro-variation on every note. It is implemented in `tianlai/modeled_instruments.py` (profile `pan_flute`, engine 1.1.0) with explicit seed 41040. The same event sequence always produces the same output; there is no silent fallback to a general-purpose SoundFont.

## Reason for modeling (honest disclosure)

No genuine sample set with a clear public license was found, so deterministic physical modeling currently fills the gap. Genuine samples remain a future fidelity upgrade.

## Source and licensing

Project-developed deterministic DSP. The engine source-file SHA-256 and all parameters are available in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py).

## Range

C4(60) - G6(91)

## Tuning

Self-calibration: 3 probe notes were rendered and measured by FFT, with a maximum error of 0.047 cents (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/排箫_奏法.events.json`; render duration 11.89 s, peak 0.419988, RMS 0.166960, clipping 0; WAV SHA-256 `d392e33c…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

The deterministic air-column model is not a recording; there is no double tonguing or glissando technique. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
