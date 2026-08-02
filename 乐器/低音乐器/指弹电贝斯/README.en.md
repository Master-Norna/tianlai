[中文](README.md) | [English](README.en.md)

# Fingered electric bass (formal)

Dedicated multisamples of the FreePats Yamaha RBX fingered electric bass. This
directory is the dedicated implementation for SAM-11 in the 98-item inventory.
It reuses `tianlai/dedicated_sfz.py` as its rendering engine and has no silent
fallback to a general-purpose SoundFont.

## Source and license

- Upstream: FreePats project: Yamaha RBX fingered electric bass (Andrea Biasior)
- Version: 2019-09-30 (main @ 8dcb7ea9116f), license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulation

- `normal`: `FingerBassYR 20190930.sfz`

The default articulation is `normal`; `pitch_mode` is `pitched`.

## Range

E1(28) - A2(45)

## Tuning

Harmonic FFT diagnostics cover 12 root samples. The measured median is
+2.833 c; the median residual after the upstream mapping is +2.833 c, and the
maximum residual is 8.387 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/指弹电贝斯_奏法.events.json`;
render duration 10.15 s, peak 0.419986,
RMS 0.087321, clipping 0;
WAV SHA-256 `76c2408a…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

Upstream samples only through A2(45), so the upper positions are absent. There
is one recorded velocity layer and no slide or muted-technique samples. The
currently bound version has passed single-instrument listening review and is
marked `formal`; ensemble use, the complete articulation set, and real
repertoire remain untested.
