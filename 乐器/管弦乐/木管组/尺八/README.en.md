[简体中文](README.md) | **English**

# Shakuhachi (`formal`, deterministic modeling)

Air-column shakuhachi model with breath noise, a scooped attack, and `modulation` vibrato. It is implemented in `tianlai/modeled_instruments.py` (profile `shakuhachi`, engine 1.1.0) with explicit seed 41039. The same event sequence always produces the same output; there is no silent fallback to a general-purpose SoundFont.

## Reason for modeling (honest disclosure)

No genuine sample set with a clear public license was found, so deterministic physical modeling currently fills the gap. Genuine samples remain a future fidelity upgrade.

## Source and licensing

Project-developed deterministic DSP. The engine source-file SHA-256 and all parameters are available in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py).

## Range

D4(62) - F6(89)

## Tuning

Self-calibration: 3 probe notes were rendered and measured by FFT, with a maximum error of 0.031 cents (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/尺八_奏法.events.json`; render duration 11.89 s, peak 0.420030, RMS 0.164644, clipping 0; WAV SHA-256 `dcd72e73…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

The deterministic air-column model is not a recording; there are no meri/kari fingering timbre changes or flutter tonguing. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
