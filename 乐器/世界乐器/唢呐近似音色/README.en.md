[中文](README.md) | [English](README.en.md)

# Suona-like timbre (formal, deterministic model)

A bright double-reed model with a rich harmonic spectrum and nasal formants at
1.25k/3.15k; this entry is explicitly positioned as an approximate timbre. The
implementation is in `tianlai/modeled_instruments.py` (profile `suona`, engine
1.1.0), with the explicit seed 41003. The same event sequence always produces
the same output; there is no silent fallback to a general-purpose SoundFont.

## Modeling rationale (honest disclosure)

No real samples with an unambiguous public license were found, so a
deterministic physical model currently fills this role. Real samples remain a
future fidelity upgrade.

## Source and license

This is project-developed deterministic DSP. The engine source SHA-256 and all
parameters are recorded in [`资源核验.json`](资源核验.json); use
[`核验资源.py`](核验资源.py) to recompute them.

## Range

E4(64) - E6(88)

## Tuning

Self-test calibration renders 3 probe notes and measures them by FFT. The
maximum error is 0.060 cents (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/唢呐近似音色_奏法.events.json`;
render duration 11.89 s, peak 0.419999,
RMS 0.130017, clipping 0;
WAV SHA-256 `3e8df082…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

This is an approximate-timbre entry. It has no reed pitch slides or circular
breathing; real suona samples remain a future fidelity upgrade. The currently
bound version has passed single-instrument listening review and is marked
`formal`; ensemble use, the complete articulation set, and real repertoire
remain untested.
