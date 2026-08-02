[中文](README.md) | [English](README.en.md)

# Open Hi-Hat (formal)

Open side of the VCSL hi-hat: open hits and open-to-closed hits. This directory is the dedicated implementation of SAM-26 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

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
| 45 | Open-to-closed hit, 1 variant |
| 46 | Open hit, 2RR |

## Range

A2(45) - A#2(46)

## Tuning

The hi-hat is an unpitched metallic idiophone. Keys 45-46 select the open-to-closed hit and open hit; no pitch calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/开放踩镲_奏法.events.json`;
render length 4.75 s, peak 0.419970,
RMS 0.029829, clipping 0;
WAV SHA-256 `0ade9782…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

This entry uses different keys from the same cymbal sample library as the closed hi-hat. This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
