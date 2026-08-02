[中文](README.md) | [English](README.en.md)

# Hand Claps (formal)

VCSL hand claps: group claps with 6RR and solo claps with 4 velocity layers. This directory is the dedicated implementation of SAM-28 in the 98-item inventory. It reuses the `tianlai/dedicated_sfz.py` rendering engine and does not silently fall back to a general-purpose SoundFont.

## Source and License

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 hashes and statistics are in [`资源核验.json`](资源核验.json); use [`核验资源.py`](核验资源.py) to recompute them

## Mapping and Articulations

- `hit`: `Idiophones/Struck Idiophones/Claps.sfz`

The default articulation is `hit`; `pitch_mode` is `ignore`.

## Key Map

| MIDI key | Content |
| --- | --- |
| 60 | Group claps, 6RR |
| 61 | Solo clap, 4 velocity layers |

## Range

C4(60) - C#4(61)

## Tuning

These claps are human-body percussion. Key 60 selects the group claps with 6RR, and key 61 selects the solo clap with 4 velocity layers; no pitch calibration is applied (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/拍手_奏法.events.json`;
render length 6.25 s, peak 0.420011,
RMS 0.010186, clipping 0;
WAV SHA-256 `9c63e6a68ea1…`. Recompute with [`核验试听.py`](核验试听.py).

## Known Limitations

The group recording is a small group, not audience applause (see SFX-02 for applause). This pinned version has passed a single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and use in actual pieces remain untested.
