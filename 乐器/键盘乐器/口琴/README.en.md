[中文](README.md) | [English](README.en.md)

# Harmonica (formal)

VCSL Hohner Super 64 chromatic harmonica with normal, vibrato, and accent
articulations. This directory is the dedicated implementation for SAM-44 in
the 98-item inventory. It reuses `tianlai/dedicated_sfz.py` as its rendering
engine and has no silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulations

- `sustain`: `Aerophones/Free Aerophones/Harmonica-Hohner-Super64 - Normal.sfz`
- `vibrato`: `Aerophones/Free Aerophones/Harmonica-Hohner-Super64 - Vib.sfz`
- `accent`: `Aerophones/Free Aerophones/Harmonica-Hohner-Super64 - Accented.sfz`

The default articulation is `sustain`; `pitch_mode` is `pitched`.

## Range

C3(48) - C#7(97)

## Tuning

Harmonic FFT diagnostics cover 39 root samples. The measured median is
-0.396 c; the median residual after the upstream mapping is -0.318 c, and the
maximum residual is 4.897 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/口琴_奏法.events.json`;
render duration 14.25 s, peak 0.420026,
RMS 0.085292, clipping 0;
WAV SHA-256 `00af08fdb444…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

There is one recorded velocity layer, and the distinction between draw and
blow reeds is not modeled. The currently bound version has passed
single-instrument listening review and is marked `formal`; ensemble use, the
complete articulation set, and real repertoire remain untested.
