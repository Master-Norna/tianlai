[中文](README.md) | [English](README.en.md)

# Fretless electric bass (formal)

Karoryfer Ergo electric upright fretless bass, primarily pizzicato with arco
as an alternative. This directory is the dedicated implementation for SAM-12
in the 98-item inventory. It reuses `tianlai/dedicated_sfz.py` as its rendering
engine and has no silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: Karoryfer Samples: Ergo (electric upright fretless bass, including down-tuned sub-bass samples)
- Version: master @ c3232f03608e, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulations

- `pizzicato`: `ergo_pizz.sfz`
- `arco`: `ergo_arco.sfz`

The default articulation is `pizzicato`; `pitch_mode` is `pitched`.

## Range

D1(26) - A3(57)

## Tuning

Harmonic FFT diagnostics cover 333 root samples. The measured median is
-3.547 c; the median residual after the upstream mapping is -3.547 c, and the
maximum residual is 65.686 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/无品电贝斯_奏法.events.json`;
render duration 12.20 s, peak 0.420011,
RMS 0.027591, clipping 0;
WAV SHA-256 `9bb1c254…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

An electric upright fretless bass represents the fretless-electric-bass
timbre; this is not a recording of a horizontal fretless bass guitar. The
currently bound version has passed single-instrument listening review and is
marked `formal`; ensemble use, the complete articulation set, and real
repertoire remain untested.
