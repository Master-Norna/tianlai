[中文](README.md) | [English](README.en.md)

# Violin · folk performance style (formal)

This entry is a folk performance style for violin, not a different acoustic
instrument. It reuses VPO's `2nd-violin-SOLO` remapping of No Budget Orchestra
solo-violin material, then applies traceable articulation envelopes for more
agile fiddle bowing. It reuses `tianlai/dedicated_sfz.py` as its rendering
engine and has no silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: Virtual Playing Orchestra 3 (Standard 3.3 / Wave 3.2)
- Version: Standard 3.3 / Wave 3.2; license: mixed public licenses including SSO Sampling Plus, No Budget Orchestra/Mattias CC-BY-SA, and VSCO2 CC0; see Documentation/license.htm
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Mapping and articulations

- `fiddle`: uses the same upstream SFZ as `sustain`, but explicitly applies a `20 ms` attack and `120 ms` release. It is the new default and suits fast, clean folk phrases.
- `sustain`: the original slow lyrical articulation, retaining the upstream `300 ms` attack and `1.6 s` tail. Existing scores that explicitly specify `sustain` remain unchanged.
- `staccato`: `Strings/2nd-violin-SOLO-staccato.sfz`
- `pizzicato`: `Strings/2nd-violin-SOLO-pizzicato.sfz`
- `accent`: `Strings/2nd-violin-SOLO-accent.sfz`

The default articulation is `fiddle`; `pitch_mode` is `pitched`. The envelope
overrides live in the version-controlled manifest and do not alter the
upstream SFZ/WAV files. Select `sustain` explicitly when the former slow,
lyrical character is required. The manifest also declares
`articulation_auto_default: false`: when the collaboration layer omits an
articulation, `fiddle` remains selected instead of a short-note heuristic
automatically switching to `accent`. If an instrumentation table explicitly
overrides this policy, the final value is still recorded in the execution plan.

## Range

G3(55) - G6(91)

## Tuning

Harmonic FFT diagnostics cover 92 root samples. The measured median is
+2.554 c; the median residual after the upstream mapping is +3.114 c, and the
maximum residual is 43.331 c (see [`音准校准.json`](音准校准.json)).

## Listening check

The current fixed full-range event sequence covers MIDI 55–91. Duration, peak,
RMS, clipping, and WAV Hash are recorded in
[`试听核验.json`](试听核验.json); recompute them with
[`核验试听.py`](核验试听.py). `human_review=pending` only means that a
separate extended blind-listening result has not been recorded; the ensemble
status remains `untested`.

## Known limitations

The folk style comes from the articulation set and phrasing. There is no
separately recorded fiddle source and no folk-specific pitch slides, sustained
double stops, or bow-change transition samples. The faster envelope improves
performance behavior but does not turn the underlying recording into a
different violin. The currently bound version has passed single-instrument
listening review and is marked `formal`; ensemble use, the complete
articulation set, and real repertoire remain untested.
