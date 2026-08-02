[中文](README.md) | [English](README.en.md)

# Closed Hi-Hat (formal)

Closed side of the VCSL hi-hat: closed hit, half-open hit, and pedal close. This directory is the dedicated implementation of SAM-31 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

## Source and License

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 hashes and statistics are in [`资源核验.json`](资源核验.json); use [`核验资源.py`](核验资源.py) to recompute them

## Mapping and Articulations

- `hit`: `Idiophones/Struck Idiophones/Hi-Hat Cymbal.sfz`

The default articulation is `hit`; `pitch_mode` is `ignore`.

## Key Map

| MIDI key | Content |
| --- | --- |
| 42 | Closed hit, 4 velocity layers × 2RR |
| 43 | Half-open hit, 2RR |
| 44 | Pedal close, 2RR |

## Range

F#2(42) - G#2(44)

## Tuning

The hi-hat is an unpitched metallic idiophone. Keys 42-44 select the closed hit, half-open hit, and pedal close; no pitch calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/闭合踩镲_奏法.events.json`;
render length 6.25 s, peak 0.420002,
RMS 0.016632, clipping 0;
WAV SHA-256 `facf65c9…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

This entry uses different keys from the same cymbal sample library as the open hi-hat. This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
