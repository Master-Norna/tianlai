[中文](README.md) | [English](README.en.md)

# Clavichord (formal)

This entry uses recordings of a Johann Gottlob Horn clavichord in the
collection of Germany's Staatliches Institut für Musikforschung (SIMPK), built
around 1793. It replaces the previous VCSL TX81Z Clavisynth electronic
approximation. The rendering engine continues to reuse
`tianlai/dedicated_sfz.py` and never silently falls back to General MIDI.

## Installation

```powershell
.\乐器\键盘乐器\击弦古钢琴\获取音源.ps1
```

The installer pins the original SIMPK 1.0 archive and first verifies its size,
SHA-256, license, original `clavichord.dspreset`, and every WAV file. It then
generates the project SFZ in a temporary directory. An existing target is only
reverified, never overwritten or merged. Large resources are installed only
under the root `音源/` directory and are absent from the lightweight release.

## Source and license

- Original author/required attribution: Staatliches Institut für Musikforschung (SIMPK).
- Physical instrument: Johann Gottlob Horn clavichord, Dresden, circa 1793.
- Upstream version: `SIMPK_03_Clavichord` 1.0.
- License: CC BY 4.0.
- Project changes: deterministically convert the upstream DecentSampler mapping to SFZ; remap the labels `rootNote=40–102`, which are one octave high, to measured sounding pitches MIDI 28–90; and write per-sample fine tuning from measurements. The original WAV files receive no octave shift, denoising, compression, or waveform rewrite.

See [`来源.md`](来源.en.md) and
[`SIMPK来源证据.json`](SIMPK来源证据.json) for formal provenance, archive
digests, and content-deduplication evidence. The installed per-file frozen
results are in [`资源核验.json`](资源核验.json).

## Range, velocity, and alternates

- The upstream preset labels roots E2–F#7 (MIDI 40–102), but pitch verification shows that the recordings sound one octave lower. The project preserves their native pitch and defines the formal playable range as E1–F#6 (MIDI 28–90).
- All 63 chromatic keys have independent root samples, with no cross-key stretching.
- The archive has 756 mapped WAV paths, but decoded PCM contains only 252 unique items: `2 timbres × 63 keys × 2 RR = 252`.
- The 3 upstream velocity files in every “timbre × key × RR” group have byte-identical PCM, so there is only **1 genuinely recorded velocity**. The converter retains 3 upstream velocity mapping zones for compatibility and deterministic routing, but they must not be called 3 genuine velocity layers.
- Input velocity can still change volume through gain response, but it does not select a different recorded timbral dynamic.
- Every key and timbre has 2 Round Robin variants. PCM deduplication found no reuse across RR, keys, or timbres.
- No loops are fabricated. Natural decay and upstream sample boundaries are retained, and note-off uses a 4-second release.

## Articulations

- `normal`: the upstream `lupe` group, the default primary timbre.
- `resonance`: the upstream `reso` group, an independent recorded set with a resonance-oriented character.

The two groups do not trigger as layers; the collaboration layer switches them
explicitly with an `articulation` event.

## Current level

Machine gates must pass for resource provenance, licensing, keys, genuine
velocity count, RR, Hash, automated tuning, and Chinese Windows paths. The
historical instrument's own mechanical noise and timbral differences are
preserved rather than erased by “denoising.” The currently bound version has
passed single-instrument listening review and is therefore marked `formal`.
Complete-resource blind listening and ensemble use still await review, and the
project cannot claim 100% reproduction.
