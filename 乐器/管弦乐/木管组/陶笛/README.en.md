[简体中文](README.md) | **English**

# Ocarina (`formal`)

VCSL typical ocarina with straight and vibrato articulations. This directory is the dedicated implementation of SAM-42 in the 98-item manifest. Its rendering engine reuses `tianlai/dedicated_sfz.py`; there is no silent fallback to a general-purpose SoundFont.

## Source and licensing

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py)

## Mappings and articulations

- `sustain`: `Aerophones/Edge-blown Aerophones/Ocarina, Typical - Sus.sfz`
- `vibrato`: `Aerophones/Edge-blown Aerophones/Ocarina, Typical - SusVib.sfz`

Default articulation: `sustain`; pitch_mode: `pitched`.

## Range

A4(69) - D6(86)

## Tuning

Harmonic FFT diagnostics cover 21 root samples. The measured median is +6.518 c; the median residual after the upstream mapping is +4.086 c, and the maximum residual is 29.261 c (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/陶笛_奏法.events.json`; render duration 12.20 s, peak 0.420017, RMS 0.109476, clipping 0; WAV SHA-256 `57bcdd83…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

There is a single velocity layer; the narrow range is a characteristic of the instrument itself. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
