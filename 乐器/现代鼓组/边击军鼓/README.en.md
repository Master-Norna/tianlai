[中文](README.md) | [English](README.en.md)

# Snare Cross-Stick (formal)

Dedicated VCSL modern-snare cross-stick entry. This directory is the dedicated implementation of SAM-30 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

## Source and License

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 hashes and statistics are in [`资源核验.json`](资源核验.json); use [`核验资源.py`](核验资源.py) to recompute them

## Mapping and Articulations

- `hit`: `Membranophones/Struck Membranophones/Snare Drum, Modern 2.sfz`

The default articulation is `hit`; `pitch_mode` is `fixed`.

## Key Map

| MIDI key | Content |
| --- | --- |
| 62 | Cross-stick rim hit, 2RR |

## Range

See the key map.

## Tuning

The snare cross-stick is an unpitched membranophone. Any notated pitch triggers the cross-stick sample, and no pitch calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/边击军鼓_奏法.events.json`;
render length 5.50 s, peak 0.420014,
RMS 0.011457, clipping 0;
WAV SHA-256 `1f90d31b1a5b…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

The upstream resource provides only 1 velocity layer × 2RR. See the orchestral snare entry for full snare hits and rolls. This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
