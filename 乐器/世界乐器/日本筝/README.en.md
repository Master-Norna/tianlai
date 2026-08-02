[中文](README.md) | [English](README.en.md)

# Japanese koto (formal, deterministic model)

An extended Karplus–Strong koto model with a clean pluck, paulownia-body
resonance, and medium-long sustain. The implementation is in
`tianlai/modeled_instruments.py` (profile `koto`, engine 1.1.0), with the
explicit seed 41004. The same event sequence always produces the same output;
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

D3(50) - E6(88)

## Tuning

Self-test calibration renders 3 probe notes and measures them by FFT. The
maximum error is 0.019 cents (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/日本筝_奏法.events.json`;
render duration 11.89 s, peak 0.257969,
RMS 0.047801, clipping 0;
WAV SHA-256 `eb1f9907…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

This is a deterministic string model, not a recording, and it has no dedicated
articulation for sweeping with koto picks or pitch-bending by pressing a
string. The currently bound version has passed single-instrument listening
review and is marked `formal`; ensemble use, the complete articulation set,
and real repertoire remain untested.
