[中文](README.md) | [English](README.en.md)

# Pipe organ (formal)

VCSL pipe organ with loud and quiet stop-group layers. This directory is the
dedicated implementation for SAM-48 in the 98-item inventory. It reuses
`tianlai/dedicated_sfz.py` as its rendering engine and has no silent fallback
to a general-purpose SoundFont.

## Source and license

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulations

- `loud`: `Aerophones/Edge-blown Aerophones/Pipe Organ - Loud.sfz`
- `quiet`: `Aerophones/Edge-blown Aerophones/Pipe Organ - Quiet.sfz`

The default articulation is `loud`; `pitch_mode` is `pitched`.

## Range

C2(36) - C#7(97)

## Tuning

Harmonic FFT diagnostics cover 42 root samples. The measured median is
+0.202 c; the median residual after the upstream mapping is +0.272 c, and the
maximum residual is 19.188 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/管风琴_奏法.events.json`;
render duration 12.20 s, peak 0.420021,
RMS 0.085175, clipping 0;
WAV SHA-256 `983ef0f0…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

Dedicated pedalboard samples are not yet connected, and there is no stop-mix
control. The currently bound version has passed single-instrument listening
review and is marked `formal`; ensemble use, the complete articulation set,
and real repertoire remain untested.
