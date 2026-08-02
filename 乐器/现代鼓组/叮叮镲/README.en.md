[中文](README.md) | [English](README.en.md)

# Ride Cymbal (formal)

VCSL Suspended Cymbal 1: bell and stick-tip hits used as a ride cymbal. This directory is the dedicated implementation of SAM-24 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

## Source and License

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 hashes and statistics are in [`资源核验.json`](资源核验.json); use [`核验资源.py`](核验资源.py) to recompute them

## Mapping and Articulations

- `hit`: `Idiophones/Struck Idiophones/Suspended Cymbal 1.sfz`

The default articulation is `hit`; `pitch_mode` is `ignore`.

## Key Map

| MIDI key | Content |
| --- | --- |
| 69 | Bell hit, 3 velocity layers |
| 70 | Stick-tip hit, 3 velocity layers |
| 71 | Roll, 3 velocity layers |

## Range

A4(69) - B4(71)

## Tuning

The suspended cymbal is an unpitched metallic idiophone. Keys 69-71 select bell, stick-tip, and roll articulations; no pitch calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/叮叮镲_奏法.events.json`;
render length 6.25 s, peak 0.419998,
RMS 0.014457, clipping 0;
WAV SHA-256 `257a5f7e12b3…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

A suspended cymbal stands in for a ride; a dedicated ride-cymbal source is planned as a future replacement. This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
