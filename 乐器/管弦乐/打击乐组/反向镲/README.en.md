[简体中文](README.md) | **English**

# Reverse Cymbal (`formal`)

Deterministic reversal of genuine VCSL suspended-cymbal samples. A reverse cymbal is, by nature, a reversed cymbal recording. At load time this implementation reverses every sample in the verified source file in time (`tianlai/reversed_cymbal.py`) without introducing any random source or general-purpose SoundFont fallback.

## Source and licensing

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 values for source samples and license evidence are in [`资源核验.json`](资源核验.json)

## Variant mapping

| MIDI key | Source sample | Rise |
| --- | --- | --- |
| 60 | susCymb2_hit_fff1 (bright) | 15.81 s |
| 61 | susCymb1_hit_fff1 (dark) | 12.95 s |
| 62 | susCymb2_roll_fff1 (long roll swell) | 20.91 s |

Reversed rise lengths: key 60: 15.81s, key 61: 12.95s, key 62: 20.91s. `note_off` triggers a 12 ms click-prevention fade to an abrupt stop. Releasing early produces a partial swell; holding the note plays the complete rise and then stops naturally and abruptly.

## Tuning

There is no fixed pitch; keys select variants only. See the not-applicable statement in [`音准校准.json`](音准校准.json).

## Audition

Fixed events: `examples/反向镲_奏法.events.json`; render duration 24.80 s, peak 0.419996, RMS 0.047638, clipping 0; WAV SHA-256 `de8402b3…`.

## Known limitations

The variant count is limited to 3. Reversal always uses the complete recording and has no mid-file starting offset. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
