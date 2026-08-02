[中文](README.md) | [English](README.en.md)

# Harpsichord (formal)

VCSL Flemish harpsichord with 8′, 4′, and combined `full` registrations. This
directory is the dedicated implementation for SAM-49 in the 98-item inventory.
It reuses `tianlai/dedicated_sfz.py` as its rendering engine. Missing resources
produce an immediate error; there is no silent fallback to a general-purpose
SoundFont.

## Source and license

- Upstream: sgossner/VCSL (Versilian Community Sample Library).
- SFZ release: `v1.2.2-RC`, corresponding to commit `b6e6ac82d22248edee98a0bde185eb9ef6d439ad`.
- License: CC0-1.0; the frozen upstream `README.md` explicitly declares the complete resource CC0.
- Per-file SHA-256, format, and region statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py).

## Mappings, articulations, and actual ranges

| Articulation | SFZ | Key / score input | Actual sound |
| --- | --- | --- | --- |
| `eight_foot` | `Harpsichord, Flemish - 8'.sfz` | F1–C6 (29–84) | F1–C6, 8′ at unison |
| `four_foot` | `Harpsichord, Flemish - 4'.sfz` | F1–C6 (29–84) | F2–C7, 1 octave above the key |
| `full` | `Harpsichord, Flemish - Full.sfz` | F1–C6 (29–84) | Each key triggers the 8′ unison and the 4′ upper octave together |

The default articulation is `full`; `pitch_mode` is `pitched`. All three
upstream SFZ files continuously cover all 56 keys. The 8′ registration uses 28
discrete sample roots, the 4′ uses 26, and `full` layers both mappings on each
key. Every sustain sample set has corresponding genuine key-release samples.

The manifest also declares `calibration_articulation: eight_foot`: generic
per-key tuning checks should use one unison registration, preventing the real
4′ upper-octave component in `full` from overpowering the 8′ fundamental and
being misreported as an octave mapping error. The dedicated calibration report
still checks all 8′ and 4′ root samples separately.

## Tuning and resource quality

The 54 sustained root samples were remeasured against each registration's
**actual sounding pitch**. The 8′ target is the key pitch; the 4′ target is
1200 cents above the key. The overall median residual is -1.227 c and the
maximum absolute residual is 8.293 c. The old 135.404 c result did not indicate
a bad E5: a narrow-window FFT had evaluated a 4′ sample in the keyboard's
original octave and locked onto an unrelated low-frequency peak. See
[`音准校准.json`](音准校准.json) for per-sample and per-registration results.

The adopted resources are 44.1 kHz, 24-bit, stereo WAV. Sustain samples retain
2.658–17.413 seconds of natural decay, with no fabricated WAV/SFZ loops;
release samples last 0.636–1.869 seconds. There are 108 unique WAV files. Across
the three articulations, the mappings reference 108 sustain regions and 108
key-release regions.

## Listening check

Fixed events are in `examples/羽管键琴_奏法.events.json` and cover low, middle,
and high keys, long and short notes, all three registrations, and key releases.
Machine-verification metrics and current WAV/manifest/events Hash values are in
[`试听核验.json`](试听核验.json); recompute them with
[`核验试听.py`](核验试听.py).

## Known limitations

- The original resource has one recorded velocity and one variant, with no timbral velocity layers or RR. Velocity currently controls gain only and must not be described as real soft/medium/loud samples.
- The lute stop is not connected, and there is no independent 8′/4′ registration balance control.
- In `full`, the 4′ component can dominate the compound spectrum on some keys. This is part of the dual-registration timbre and must not make a single-pitch algorithm report the whole key one octave high.
- This pass completed machine tuning, full-range mapping, release, Hash, and clipping verification. Manual blind listening remains `pending`, which blocks only the corresponding strict capability claims; the currently bound single-instrument status remains `formal`, and ensemble use is untested.
