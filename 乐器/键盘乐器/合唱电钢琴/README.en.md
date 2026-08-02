[中文](README.md) | [English](README.en.md)

# Chorused electric piano (formal)

The recorded core is Greg Sullivan's Yamaha CP80, followed by deterministic
stereo chorus. The sample core reuses `tianlai/dedicated_sfz.py`, while
`tianlai/dedicated_fx.py` runs the effect chain frame by frame. There is no
silent fallback to a general-purpose SoundFont.

## Source and license

- Upstream: Greg Sullivan E-Pianos / Yamaha CP80
- Pinned commit: `8c3e581acda3594b553948ff0222d4f84a698376`
- License: CC-BY-3.0
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

## Range

A0 (21) — C8 (108)

## Tuning

See [`音准校准.json`](音准校准.json) for root-sample and end-to-end
wide-frequency tuning results.

## Listening check

Fixed events are in `examples/合唱电钢琴_奏法.events.json`; recompute the
listening metrics with [`核验试听.py`](核验试听.py).

## Known limitations

The chorus is deterministic DSP, and the core shares the CP80 four-velocity
resource with the electric-piano entry. A CP80 is not a Rhodes or Wurlitzer.
Root samples from D#1—B7 cover A0—C8, with the lowest note transposed downward
by at most 6 semitones. The upper register retains the real CP80's stretch
tuning (root samples reach approximately +44 c). There is currently no
independent mechanical key-noise layer. The currently bound version has passed
single-instrument listening review; ensemble use and complete capability
coverage remain untested.
