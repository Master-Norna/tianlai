[简体中文](README.md) | **English**

# Marimba (`formal`)

Dedicated VCSL marimba multisample with soft/med/loud three-velocity crossfades. This directory is the dedicated implementation of ORP-11 in the 98-item manifest. Its rendering engine reuses `tianlai/dedicated_sfz.py`; there is no silent fallback to a general-purpose SoundFont.

## Source and licensing

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py)

## Mapping and articulation

- `hit`: `Idiophones/Struck Idiophones/Marimba.sfz`

Default articulation: `hit`; pitch_mode: `pitched`.

## Range

F2(41) - C#7(97)

## Tuning

Harmonic FFT diagnostics cover 30 root samples. The measured median is -0.144 c; the median residual after the upstream mapping is -0.144 c, and the maximum residual is 142.046 c (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/马林巴_奏法.events.json`; render duration 10.15 s, peak 0.419954, RMS 0.026419, clipping 0; WAV SHA-256 `aa87f4fd…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

There is one round robin. Upstream was generated with `--notuning`, so calibration is diagnostic only; there are no roll samples. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
