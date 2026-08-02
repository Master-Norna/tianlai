[中文](README.md) | [English](README.en.md)

# Jazz electric guitar (formal)

Karoryfer Emilyguitar flatwound DI through a deterministic jazz-tone filter
chain. This directory is the dedicated implementation for SAM-16 in the
98-item inventory. The sample core reuses `tianlai/dedicated_sfz.py`, while
`tianlai/dedicated_fx.py` runs the effect chain frame by frame. There is no
silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: Karoryfer Lecolds (D. Smolken): Emilyguitar
- Version: v1.001, license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and signal chain

- `normal`: `emily_basic.sfz`

Deterministic signal chain: first-order 90 Hz high-pass + first-order 2.4 kHz
low-pass, modeling a neck-pickup jazz tone; release-mute samples are included.
Every parameter is frozen in the `effects` array of `乐器.json`. There is no
random source, so identical input always produces identical output.

## Range

D2(38) - D6(86)

## Tuning

Harmonic FFT diagnostics cover 251 root samples. The measured median is
+1.155 c; the median residual after the upstream mapping is +1.155 c, and the
maximum residual is 178.938 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/爵士电吉他_奏法.events.json`;
render duration 10.15 s, peak 0.419989,
RMS 0.047795, clipping 0;
WAV SHA-256 `7b0343f50f80…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

The dark filtering of a flatwound DI approximates a jazz archtop timbre; this
is not a recording of a hollow body. Release-mute samples are retained. The
currently bound version has passed single-instrument listening review and is
marked `formal`; ensemble use, the complete articulation set, and real
repertoire remain untested.
