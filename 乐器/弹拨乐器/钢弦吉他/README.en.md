[中文](README.md) | [English](README.en.md)

# Steel-string guitar (formal)

Dedicated multisamples of the FreePats FS Seagull steel-string acoustic guitar.
This directory is the dedicated implementation for SAM-18 in the 98-item
inventory. It reuses `tianlai/dedicated_sfz.py` as its rendering engine and has
no silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: FreePats project: FS Seagull Steel String Guitar (FlameStudios samples)
- Version: 2020-05-21, license: GPL-3.0-or-later WITH FlameStudios sampling exception
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulation

- `normal`: `FSS-SteelStringGuitar-20200521.sfz`

The default articulation is `normal`; `pitch_mode` is `pitched`.

## Range

E2(40) - B5(83)

## Tuning

Harmonic FFT diagnostics cover 59 root samples. The measured median is
+7.008 c; the median residual after the upstream mapping is +7.008 c, and the
maximum residual is 19.872 c (see [`音准校准.json`](音准校准.json)).

## Listening check

Fixed events: `examples/钢弦吉他_奏法.events.json`;
render duration 10.15 s, peak 0.419960,
RMS 0.073099, clipping 0;
WAV SHA-256 `a745d424…`. Recompute with [`核验试听.py`](核验试听.py).

## Known limitations

Upstream extends the sample zones; this entry narrows them to the physical
E2-B5 fingerboard. There are no strumming or harmonic articulations. The
currently bound version has passed single-instrument listening review and is
marked `formal`; ensemble use, the complete articulation set, and real
repertoire remain untested.
