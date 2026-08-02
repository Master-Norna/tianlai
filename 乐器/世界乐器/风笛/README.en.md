[中文](README.md) | [English](README.en.md)

# Bagpipe (formal)

This entry uses the recorded G-tuned bagpipe from FreePats 2022-12-04. The
upstream keyboard mapping stretches two sustained drones across two octaves
and places them with the chanter on one continuous keyboard. That layout suits
a general-purpose sampler, but it makes the full-range ascending listening
check sound as though the instrument abruptly changes at about 8 and 16
seconds.

The default entry now plays and scans only the `chanter`. The two drones are
separate articulations that retain their original G2/G3 pitches; they no longer
masquerade as one chromatically transposable melodic range.

## Sound source and license

- Upstream: FreePats Scottish Great Highland Bagpipe, version 2022-12-04
- License: CC0-1.0
- Original files remain in `音源/FreePats/Bagpipe`
- Derived files are written to `音源/派生/风笛-v1`, inheriting CC0-1.0
- Hashes for the original and derived SFZ files, samples, and license evidence are in `资源核验.json`

## Offline derivation

```powershell
.\.venv\Scripts\python.exe .\乐器\世界乐器\风笛\预处理音源.py
```

The script reads `预处理参数.json`, verifies the original SHA-256 values, and
generates:

- `chanter.sfz`: only the MIDI 64–81 chanter
- `drone-low.sfz`: triggers only the original low drone at MIDI 43
- `drone-high.sfz`: triggers only the original high drone at MIDI 55
- two groups of offline-derived WAV files for F4 and G4
- an auditable `处理说明.json`

The steady-state RMS of the derived F4 and G4 samples is normalized to
−13.3 dBFS, and the SFZ applies the same 2 dB compensation to all three. The
two genuine G4 round-robin samples receive separate gentle high-shelf filters,
preventing an excessive brightness jump across the F4→G4 seam. Sample rate,
frame count, loop points, and upstream tune remain unchanged.

## Articulations and ranges

- `chanter`: default, E4–A5 (MIDI 64–81)
- `drone_low`: G2 (MIDI 43)
- `drone_high`: G3 (MIDI 55)

A complete bagpipe phrase should start the two drones separately, switch back
to `chanter` for the melody, and keep the drones sounding across phrases.
Switching articulation does not stop sounds that are already held. This
produces one instrument comprising two sustained drones and a chanter, rather
than playing the three components consecutively as a chromatic scale.

## Known limitations

The chanter still has one velocity layer, and only most root notes have two RR
samples. The derived matching repairs only the confirmed F4/G4 seam and does
not use aggressive equalization to conceal other recording differences. The
new full-range and combined-phrase listening checks still require manual
review.
