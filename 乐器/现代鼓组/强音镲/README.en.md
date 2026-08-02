[中文](README.md) | [English](README.en.md)

# Crash Cymbal (formal)

VCSL Suspended Cymbal 2: 5 velocity layers of mallet hits plus three crescendo-roll durations, used as a crash cymbal. This directory is the dedicated implementation of SAM-27 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

## Source and License

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 hashes and statistics are in [`资源核验.json`](资源核验.json); use [`核验资源.py`](核验资源.py) to recompute them

## Mapping and Articulations

- `hit`: `Idiophones/Struck Idiophones/Suspended Cymbal 2.sfz`

The default articulation is `hit`; `pitch_mode` is `ignore`.

## Key Map

| MIDI key | Content |
| --- | --- |
| 63 | 2.5s crescendo roll |
| 64 | 4s crescendo roll |
| 65 | 7s crescendo roll |
| 66 | Mallet hit, 5 velocity layers (pp-fff) |

## Range

D#4(63) - F#4(66)

## Tuning

The suspended cymbal is an unpitched metallic idiophone. Keys 63-66 select crescendo rolls and the 5-layer mallet hit; no pitch calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/强音镲_奏法.events.json`;
render length 5.50 s, peak 0.419998,
RMS 0.056748, clipping 0;
WAV SHA-256 `365cb0a5…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

The crash sound is a suspended-cymbal mallet hit rather than a drumstick hit, and the crescendos are fixed-duration recordings. This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
