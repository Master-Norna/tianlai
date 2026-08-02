[简体中文](README.md) | **English**

# Agogo Bells (`formal`)

VCSL agogo bells with velocity-layered high and low bells. This directory is the dedicated implementation of SAM-38 in the 98-item manifest. Its rendering engine reuses `tianlai/dedicated_sfz.py`; there is no silent fallback to a general-purpose SoundFont.

## Source and licensing

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py)

## Mapping and articulation

- `hit`: `Idiophones/Struck Idiophones/Agogo Bells.sfz`

Default articulation: `hit`; pitch_mode: `ignore`.

## Key mapping

| MIDI key | Content |
| --- | --- |
| 60 | High bell, 3 velocity layers |
| 61 | Low bell, 2 velocity layers |

## Range

C4(60) - C#4(61)

## Tuning

Agogo bells are a pair of relatively high and low metal bells: key 60 selects the high bell and key 61 the low bell. No false absolute-pitch calibration is claimed (see the not-applicable statement in [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/阿哥哥铃_奏法.events.json`; render duration 7.00 s, peak 0.420004, RMS 0.019293, clipping 0; WAV SHA-256 `0e3cd966…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

There is no round robin, and the bells form a relative-pitch pair. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
