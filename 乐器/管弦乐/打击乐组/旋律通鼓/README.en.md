[简体中文](README.md) | **English**

# Melodic Toms (`formal`)

GM Melodic Toms are toms arranged chromatically. This implementation uses stick-hit samples from two genuine VCSL toms, each with 3 velocity layers × 2 round robins. It first measures the drumhead fundamental of each tom by FFT and uses it as the root pitch, then maps the low tom to C2(36)–F#2(42) and the high tom to G2(43)–C#3(49), transposing by resampling to obtain the chromatic score pitches. Implementation: `tianlai/melodic_toms.py`; there is no general-purpose SoundFont fallback.

## Source and licensing

- Upstream: sgossner/VCSL (Versilian Community Sample Library)
- Version: 1.2.2-RC; license: CC0-1.0
- Per-file SHA-256 values for all 12 samples are in [`资源核验.json`](资源核验.json)

## Tuning

Measured roots: low tom 41.27 (approximately F2), high tom 44.25 (approximately G#2). See [`音准校准.json`](音准校准.json) for the dispersion of sample fundamentals within each drum. Roots are measured, and constructed score pitches are accurate.

## Velocity and round robin

Each drum has 3 velocity layers (0-65 / 66-99 / 100-127) × 2 round robins, retaining the upstream ranges.

## Audition

Fixed events: `examples/旋律通鼓_奏法.events.json`; render duration 7.31 s, peak 0.420013, RMS 0.024074, clipping 0; WAV SHA-256 `368f0c7301cf…`.

## Known limitations

There are only two genuine drums; keys far from either root rely on resampled transposition, and timbral realism decreases with distance. There are no soft-mallet or rimshot layers. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
