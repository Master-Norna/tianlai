[中文](README.md) | [English](README.en.md)

# Nylon-string guitar (formal)

Dedicated multisamples of the FreePats Spanish classical guitar (nylon
strings). This directory is the dedicated implementation for SAM-14 in the
98-item inventory. It reuses `tianlai/dedicated_sfz.py` as its rendering engine
and has no silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: FreePats project: Spanish Classical Guitar
- Version: 2019-06-18, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulation

- `normal`: `SpanishClassicalGuitar-20190618.sfz`

The default articulation is `normal`; `pitch_mode` is `pitched`.

## Range

E2(40) - B5(83)

## Tuning

Harmonic FFT diagnostics cover 48 root samples. The measured median is
+4.843 c; the median residual after the upstream mapping is +4.843 c, and the
maximum residual is 16.455 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/尼龙弦吉他_奏法.events.json`;
render duration 10.15 s, peak 0.420040,
RMS 0.064383, clipping 0;
WAV SHA-256 `538e1fa4d22c…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

Upstream extends the sample zones to 29-88; this entry narrows them to the
physical E2-B5 fingerboard. The currently bound version has passed
single-instrument listening review and is marked `formal`; ensemble use, the
complete articulation set, and real repertoire remain untested.
