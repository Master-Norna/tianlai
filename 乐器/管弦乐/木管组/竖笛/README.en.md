[简体中文](README.md) | **English**

# Recorder (`formal`)

This entry point uses recordings of a Baroque soprano recorder from VCSL 1.2.2-RC. The default `sustain` no longer reads the original studio stereo files directly; it reads offline-derived versions with unified phase, stereo image, and steady-state loudness. `staccato` continues to use the original VCSL staccato mapping. Runtime performs no audio preprocessing and has no silent fallback to a general-purpose SoundFont.

## Sample source and licensing

- Upstream: sgossner/VCSL, version 1.2.2-RC.
- Original license: CC0-1.0.
- The 13 derived sustain WAV files are likewise used under CC0-1.0.
- Original files remain under `音源/VCSL`; the script writes only to `音源/派生/竖笛-v1`.
- See `资源核验.json` for hashes of the originals, derived SFZ, samples, and license evidence.

## Offline derivation

```powershell
.\.venv\Scripts\python.exe .\乐器\管弦乐\木管组\竖笛\预处理音源.py
```

The processing recipe is pinned in `预处理参数.json`, and the algorithm is implemented in `tianlai/derived_samples.py`. The script verifies every original SHA-256, then generates PCM24 WAV files, `sustain.sfz`, and `处理说明.json`; any mismatched input hash fails explicitly.

The following processing is applied to the 13 sustain root samples:

1. Apply the integer-frame offset and polarity correction specified by the recipe to the left and right channels.
2. Mix to dual mono, eliminating wandering stereo images and phase cancellation between root samples.
3. Normalize to −20 dBFS RMS using the 0.75–3.75 second steady-state window after the attack.
4. Apply only smooth, zero-phase, gentle high-frequency shelving and noise-band roll-off to E5, F♯5, A♯5, and C6.
5. Preserve sample rate, frame count, original SFZ offset, and pitch mapping.

## Articulations and range

- `sustain`: derived sustain sound, the default articulation.
- `staccato`: independent original VCSL staccato.
- Sounding range: C5–D7 (MIDI 72–98).

## Known limitations

The current resources have only one genuine velocity layer, no sustain round robin, and no loops. The derived processing fixes obvious phase, stereo-image, loudness, and high-register noise discontinuities; it does not misrepresent a single-layer source as a complete multilayer source. The new full-range audition still requires a separate follow-up review after human verification.
