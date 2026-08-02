[中文](README.md) | [English](README.en.md)

# Kick Drum (formal)

VCSL concert bass drum, using a close-microphone mix with 4 velocity layers × 2RR as a modern kick drum. This directory is the dedicated implementation of SAM-25 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

## Source and License

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 hashes and statistics are in [`资源核验.json`](资源核验.json); use [`核验资源.py`](核验资源.py) to recompute them

## Mapping and Articulations

- `hit`: `Membranophones/Struck Membranophones/Bass Drum 1.sfz`

The default articulation is `hit`; `pitch_mode` is `fixed`.

## Key Map

| MIDI key | Content |
| --- | --- |
| 60 | Close-miked bass-drum hit, 4 velocity layers × 2RR |

## Range

See the key map.

## Tuning

The kick is an unpitched membranophone. Any notated pitch triggers the same drum, and no 12-tone equal-temperament calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/底鼓_奏法.events.json`;
render length 5.50 s, peak 0.419928,
RMS 0.035118, clipping 0;
WAV SHA-256 `095d759f…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

The source is a close-miked concert bass drum, with no choice of beater or striking surface. This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
