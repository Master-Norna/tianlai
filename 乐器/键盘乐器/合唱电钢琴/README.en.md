[中文](README.md) | [English](README.en.md)

# Chorused electric piano (formal)

The recorded core is Greg Sullivan's Yamaha CP80, followed by deterministic
stereo chorus. The sample core reuses `tianlai/dedicated_sfz.py`, while
`tianlai/dedicated_fx.py` runs the effect chain frame by frame. There is no
silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: Greg Sullivan E-Pianos / Yamaha CP80
- Pinned commit: `8c3e581acda3594b553948ff0222d4f84a698376`
- License: [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/);
  original recordings by Greg Sullivan, with SFZ/FLAC mapping by kinwie; see
  [`来源.md`](来源.en.md) for attribution and license evidence
- Per-file SHA-256 values and statistics are in [`资源核验.json`](资源核验.json); recompute them with [`核验资源.py`](核验资源.py)

## Obtaining the resources

This entry shares
[`../获取GregSullivan电钢琴音源.ps1`](../获取GregSullivan电钢琴音源.ps1)
with the electric-piano entry. The installer pins the upstream commit and
checks the license, README, SFZ, all 81 FLAC files, and their aggregate
SHA-256. If `音源/GregSullivan.E-Pianos` already exists, it verifies the
directory without overwriting it.

## Mapping and signal chain

- `normal`: `CP80/CP80.sfz`

Deterministic signal chain: 0.9 Hz / 4.5 ms stereo LFO chorus, with the right
channel shifted by 90° and a 50/50 dry/wet mix. Every parameter is frozen in
the `effects` array of `乐器.json`. There is no random source, so identical
input always produces identical output.

The shared source set contains 81 mono FLAC files at 44.1 kHz. The sample core
first renders 48 kHz stereo with explicit
`resampling_quality=bandlimited`, then enters the fixed chorus. Both the
band-limited algorithm choice and original channel count are part of runtime
identity; upstream files are not modified.

## Range

A0 (21) — C8 (108)

## Tuning

See [`音准校准.json`](音准校准.json) for root-sample and end-to-end
wide-frequency tuning results.

## Listening check

[`试听核验.json`](试听核验.json) is the full 88-key ascending stress scan and can
be rebuilt with `tools/生成全部试音.py --only 键盘乐器/合唱电钢琴`.
[`表现力试听核验.json`](表现力试听核验.json) uses
`examples/合唱电钢琴_奏法.events.json` to cover all four velocity levels,
short/long notes, and deterministic stereo chorus; recompute it with
[`核验试听.py`](核验试听.py). The two reports do not overwrite one another.

## Known limitations

The chorus is deterministic DSP, and the core shares the CP80 four-velocity
resource with the electric-piano entry. A CP80 is not a Rhodes or Wurlitzer.
Root samples from D#1—B7 cover A0—C8, with the lowest note transposed downward
by at most 6 semitones. The upper register retains the real CP80's stretch
tuning (root samples reach approximately +44 c). There is currently no
independent mechanical key-noise layer. Machine evidence and automated tests
pass, while work-level human listening review of the currently bound version
is still pending; ensemble use and complete capability coverage remain
untested.
