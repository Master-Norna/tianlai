[中文](README.md) | [English](README.en.md)

# Shamisen (formal, deterministic model)

An extended Karplus–Strong shamisen model with a skin resonator, sawari bridge
buzz, and bachi attack noise. The implementation is in
`tianlai/modeled_instruments.py` (profile `shamisen`, engine 1.1.0), with the
explicit seed 41001. The same event sequence always produces the same output;
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

B2(47) - B5(83)

## Tuning

Self-test calibration renders 3 probe notes and measures them by FFT. The
maximum error is 0.234 cents (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/三味线_奏法.events.json`;
render duration 11.89 s, peak 0.279871,
RMS 0.011788, clipping 0;
WAV SHA-256 `10c16f55…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

This is a deterministic string model, not a recording, and it has no dedicated
kake, oshi, or pitch-slide articulations. The currently bound version has
passed single-instrument listening review and is marked `formal`; ensemble use,
the complete articulation set, and real repertoire remain untested.
