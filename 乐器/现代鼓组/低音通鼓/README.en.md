[中文](README.md) | [English](README.en.md)

# Low Tom (formal)

VCSL low tom with six keys covering drumsticks, soft mallets, rimshots, and rolls. This directory is the dedicated implementation of SAM-23 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

## Source and License

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 hashes and statistics are in [`资源核验.json`](资源核验.json); use [`核验资源.py`](核验资源.py) to recompute them

## Mapping and Articulations

- `hit`: `Membranophones/Struck Membranophones/Tom 2.sfz`

The default articulation is `hit`; `pitch_mode` is `ignore`.

## Key Map

| MIDI key | Content |
| --- | --- |
| 60 | rimFLS mixed rimshot, 2RR |
| 61 | rimS rimshot, 2 velocity layers × 2RR |
| 62 | HitM soft mallet, 3 velocity layers × 2RR |
| 63 | RollM soft-mallet roll, 2 velocity layers |
| 64 | HitS drumstick, 3 velocity layers × 2RR |
| 65 | RollS drumstick roll, 2 velocity layers |

## Range

C4(60) - F4(65)

## Tuning

The low tom is an unpitched membranophone. Keys 60-65 select rimshot, mallet-hit, and roll variants; no 12-tone equal-temperament calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/低音通鼓_奏法.events.json`;
render length 7.00 s, peak 0.420025,
RMS 0.016846, clipping 0;
WAV SHA-256 `0a2c48e5a0e3…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

The rolls are fixed-duration recordings, and the instrument is not chromatically pitched. This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
