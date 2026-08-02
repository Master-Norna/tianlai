[中文](README.md) | [English](README.en.md)

# Clean electric guitar (formal)

Karoryfer Emilyguitar flatwound DI clean electric guitar, with 4 velocity
layers × 3 RR. This directory is the dedicated implementation for SAM-15 in
the 98-item inventory. It reuses `tianlai/dedicated_sfz.py` as its rendering
engine and has no silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: Karoryfer Lecolds (D. Smolken): Emilyguitar
- Version: v1.001, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulation

- `normal`: `emily_clean.sfz`

The default articulation is `normal`; `pitch_mode` is `pitched`.

## Range

D2(38) - D6(86)

## Tuning

Harmonic FFT diagnostics cover 251 root samples. The measured median is
+1.155 c; the median residual after the upstream mapping is +1.155 c, and the
maximum residual is 178.938 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/清音电吉他_奏法.events.json`;
render duration 10.15 s, peak 0.420014,
RMS 0.040990, clipping 0;
WAV SHA-256 `60964dc7…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

This is a direct-injection recording with no cabinet. Noise keys (90+) are not
part of the playable range, and the low strings include samples tuned down to
Db. The currently bound version has passed single-instrument listening review
and is marked `formal`; ensemble use, the complete articulation set, and real
repertoire remain untested.
