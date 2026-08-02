[简体中文](README.md) | **English**

# Muted Trumpet (`formal`)

VPO solo-trumpet samples with deterministic straight-mute filter modeling. This directory is the dedicated implementation of VPO-14 in the 98-item manifest. The sampled core reuses `tianlai/dedicated_sfz.py`, while the effects chain is processed frame by frame by `tianlai/dedicated_fx.py`. There is no silent fallback to a general-purpose SoundFont.

## Source and licensing

- Upstream: Virtual Playing Orchestra 3 (Standard 3.3 / Wave 3.2)
- Version: Standard 3.3 / Wave 3.2. License: mixed open licenses, including SSO Sampling Plus, No Budget Orchestra/Mattias CC-BY-SA, and VSCO2 CC0; see Documentation/license.htm
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); reproduce them with [`核验资源.py`](核验资源.py)

## Mapping and signal chain

- `sustain`: `Brass/trumpet-SOLO-sustain.sfz`
- `staccato`: `Brass/trumpet-SOLO-staccato.sfz`
- `accent`: `Brass/trumpet-SOLO-accent.sfz`

Deterministic signal chain: 520 Hz high-pass → 1.65 kHz Q2.2 +9 dB resonance peak → 4.2 kHz low-pass, approximating the nasal resonant transfer characteristic of a straight mute. All parameters are pinned in the `effects` array in `乐器.json`. There is no random source, so identical input always produces identical output.

## Range

F#3(54) - A#5(82)

## Tuning

Harmonic FFT diagnostics cover 54 root samples. The measured median is -1.668 c; the median residual after the upstream mapping is -1.668 c, and the maximum residual is 2.222 c (see [`音准校准.json`](音准校准.json)).

## Audition

Fixed events: `examples/弱音小号_奏法.events.json`; render duration 14.25 s, peak 0.420029, RMS 0.079816, clipping 0; WAV SHA-256 `593f60f4…`. Reproduce with [`核验试听.py`](核验试听.py).

## Known limitations

The muted timbre is deterministic filter modeling rather than a recording of a real muted trumpet. Genuine muted-trumpet samples remain a future fidelity upgrade. The currently bound version has passed single-timbre audition and is marked `formal`; ensemble use, the complete articulation set, and real repertoire remain untested.
