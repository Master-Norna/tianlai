[中文](README.md) | [English](README.en.md)

# Electric piano (formal)

This entry uses Greg Sullivan's recorded Yamaha CP80 Electric Grand Piano
rather than the former TX81Z FM placeholder. It reuses
`tianlai/dedicated_sfz.py` as its rendering engine and has no silent fallback
to a general-purpose SoundFont.

## Source and license

- Upstream: Greg Sullivan E-Pianos / Yamaha CP80
- Pinned commit: `8c3e581acda3594b553948ff0222d4f84a698376`
- License: CC-BY-3.0; see [`来源.md`](来源.en.md) for attribution and license evidence
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Obtaining the resources

The electric piano and chorused electric piano share one safe installer:
[`../获取GregSullivan电钢琴音源.ps1`](../获取GregSullivan电钢琴音源.ps1).
It pins the commit above, installs into `音源/GregSullivan.E-Pianos`, and
checks the license, upstream README, SFZ, and aggregate SHA-256 of 81 FLAC
files. If the target directory already exists, it only verifies it and never
overwrites or merges existing content.

## Mapping and articulation

- `normal`: `CP80/CP80.sfz`

Four genuine velocity levels, PP / MP / F / FF. The default articulation is
`normal`; `pitch_mode=pitched`.

## Range

A0 (21) — C8 (108)

## Tuning

Root-sample calibration and end-to-end wide-frequency tuning results are
authoritative in [`音准校准.json`](音准校准.json). In addition to fine tuning,
calibration must detect root-note mapping errors of ±1200 cents.

## Listening check

Fixed events are in `examples/电钢琴_奏法.events.json`; metrics and the WAV Hash
are recomputed by [`核验试听.py`](核验试听.py). This document makes no promise of
an additional fine-grained articulation matrix or expert listening conclusion.

## Known limitations

A CP80 differs structurally and sonically from a Rhodes or Wurlitzer; this
entry should be understood explicitly as a Yamaha electric grand piano.
Upstream root samples run from D#1 to B7, and the SFZ extends them across the
CP80's A0—C8 range. The lowest A0 is shifted down 6 semitones from the first
root sample, the largest timbral stretch in this resource, and must not be
described as per-key sampling. The highest root sample has approximately +44 c
of real CP80 stretch tuning, so this entry does not satisfy a trusted-list hard
gate of “≤10 c across the entire range.” Upstream provides no independent
mechanical key-noise or release samples; long notes depend on the full
recording's natural decay.
