[简体中文](VPO音源许可与安装说明.md) | **English**

# VPO sound-source license and installation guide

> **Translation notice:** This English page is an informational translation of
> the project-authored [Chinese source text](VPO音源许可与安装说明.md). It summarizes
> a frozen installation path and does not replace VPO's own license files or
> the terms of its component sources. Upstream texts control.

This guide covers the 31 sound entries that share one official VPO 3.2 Wave +
3.3 Standard installation tree. Thirty entries use VPO's mixed-license content
and are `grandfathered`; one viola entry reads only the VSCO2-CE CC0 subtree and
is independently `approved`. This page records the installation method, pinned
archives, complete-tree digest, and both licence boundaries.

## Conclusion

All 30 mixed-license entries are `grandfathered`, not `approved`. They may be
installed and rendered on a user's computer from the frozen official
distribution. Their status is not a general allowlist for adding new sources
or redistributing samples. The viola entry is outside these 30 exceptions
because its runtime scope is confined to a verified VSCO2-CE CC0 subtree.

Using the official library to render music and redistributing the sample
library are different activities:

- Tianlai's source package does not carry, host, or mirror VPO SFZ/WAV files.
- The installer retrieves complete Wave 3.2 and Standard 3.3 archives through
  VPO's official download path.
- Both official ZIPs are merged unchanged on the local computer as upstream
  instructs; Tianlai does not generate or rewrite the sound-source tree.
- VPO's aggregate notice expresses an intention to permit music made with the
  library, including commercial music, but it is not one blanket waiver from
  every underlying rightsholder. The formal terms of the components actually
  used still control.

The 30 entries may therefore render within this frozen installation boundary,
but this page does not promise unconditional permission for every commercial
use, advertising use, or output form.
If Tianlai later hosts, splits, converts, or republishes samples, this conclusion
immediately stops applying and the complete per-source and derivation license
chain must be reviewed again.

## Installation-chain evidence

The official instructions require the Wave Files and Standard Orchestra scripts
to be extracted into the same `Virtual-Playing-Orchestra3/` directory:

- [Official Virtual Playing Orchestra project and license page](https://virtualplaying.com/virtual-playing-orchestra/comment-page-1/)
- The official Wave 3.2 redirect ultimately points to
  `archive.org/download/virtual-playing-orchestra-3-2-wave-files/`
- Standard 3.3 uses VPO's official `virtualplaying.com/go/` download entry point

Pinned archives:

| Archive | Bytes | SHA-256 |
|---|---:|---|
| `Virtual-Playing-Orchestra3-2-wave-files.zip` | 616,114,842 | `CA8F1E0B56EEDE35314994646E5F1F307EC349616C967FBECF627C43AA646E90` |
| `Virtual-Playing-Orchestra3-3-standard-scripts.zip` | 544,010 | `F0F2BF0E42D2A39C5F49401ADDCFFA840FD8F5525670F5945BF5093A5442BDA5` |

After extracting Wave first and Standard second as instructed:

- 1,922 files, totaling 724,695,982 bytes;
- every file digest must match the frozen tree;
- complete-tree digest:
  `B06390C70D9D701481BC6DB0CF13B6ED6F3EF6B660DAC9A51034B9BE368DF317`.

Complete-tree digest algorithm: recursively enumerate ordinary files under
`Virtual-Playing-Orchestra3/`; normalize relative-path separators to `/`; sort
ascending by case-sensitive Unicode ordinal; write
`<lowercase file SHA-256><two spaces><relative path>\n` for each file; then hash
the resulting BOM-free UTF-8 record stream with SHA-256.

Root [`安装VPO音源.ps1`](../安装VPO音源.ps1) pins and verifies both archives,
merges and validates the complete tree in a temporary directory, then atomically
replaces the formal directory. The VPO acquisition scripts in three instrument
directories remain compatibility entry points that dispatch to this unified
installer.

## Mapping of the 30 `grandfathered` entries

| Entry group | Tianlai entries | Official VPO mapping family |
|---|---|---|
| World / voice / electronic / modern drums | Folk fiddle, choir aah, orchestral hit, cowbell | `Strings/2nd-violin-SOLO-*`, VPO choir, orchestral-hit, and cowbell configurations |
| Strings | Double bass, cello, violin, string ensemble, pizzicato strings, tremolo strings | `Strings/*-SOLO-*`, `1st-violin-SEC-*`, `all-strings-SEC-*` |
| Woodwinds | Bassoon, clarinet, piccolo, oboe, English horn, flute | `Woodwinds/{bassoon,clarinet,piccolo,oboe,english-horn,flute}-SOLO-*` |
| Brass | Tuba, muted trumpet, brass ensemble, trumpet, horn, trombone | `Brass/*-SOLO-*` and VPO sections; muted trumpet adds project-authored dynamic filtering to a trumpet SOLO mapping |
| Percussion | Orchestral cymbals, orchestral bass drum, xylophone, woodblock, triangle, snare, glockenspiel | VPO `cymbals`, `bass_drum`, `xylophone`, `woodblock`, `triangle`, `snare`, and `glockenspiel` configurations |
| Keyboard | Celesta | VPO `celesta` configuration |

The 31st entry is `管弦乐/弦乐组/中提琴`. Although it shares the installation
tree, its runtime `sample_subtree` is confined to
`libs/VSCO2-CE/Strings/Viola Section`. Its evidence is
`libs/VSCO2-CE/LICENSE.txt` plus VPO's aggregate declaration that VSCO2-CE is
CC0, so it remains `approved / CC0-1.0` and is not one of the 30 mixed-license
exceptions.

Each entry's runtime sample set and SFZ hashes remain frozen by its own
`资源核验.json`. This mapping is not a license inference from an instrument name;
it matches the actual VPO type/configuration in 30 manifests to the unified
official tree. VPO contains samples that upstream contributors such as Paul
Battersby and No Budget Orchestra already looped, mixed, or organized. They are
not untouched WAV files from the first recorder, but they are unchanged release
files from Tianlai's direct upstream, VPO.

## License boundary

VPO's aggregate notice lists Sonatina Sampling Plus 1.0, Mattias Westlund
CC BY-SA 3.0, No Budget Orchestra CC BY-SA 4.0, VSCO2/stamperadam CC0, the
University of Iowa use statement, and other sources. It is not a single licence
for the complete tree and does not replace the formal terms retained with each
sublibrary.

The following boundaries therefore remain important:

- VPO's author says that the intended policy is to allow music made with the
  library, including commercial music, without output attribution. That is an
  aggregate statement of intent, not evidence that every underlying author
  expressly waived their formal terms.
- The [Creative Commons Sampling Plus 1.0 legal code](https://creativecommons.org/licenses/sampling%2B/1.0/legalcode.en)
  requires a good-faith, partial, and highly transformative derivative, carries
  attribution and licence-notice conditions, and excludes advertising or
  promotional uses for other products or services. Promotion of the derivative
  work itself or its author is the stated exception.
- CC BY-SA 3.0 and 4.0 components carry their own attribution and ShareAlike
  conditions. Whether and how a particular output shares or adapts those
  materials depends on the components actually used and the use in question;
  it cannot be inferred once for the whole family from the VPO name.
- Modifying, repackaging, or redistributing SFZ/WAV material requires
  source-by-source compliance and retention of the evidence. VPO's aggregate
  notice also asks that a derived sample library remain free or personal-use
  only.

Therefore:

- The 30 mixed-license instruments may render locally within the frozen
  boundary. Publication still requires a component- and use-specific check;
  Tianlai does not promise that every commercial or advertising use is free of
  additional conditions.
- Ordinary finished music and a reusable sample package are different
  distributions, but that distinction does not override an underlying licence.
- Until clear per-source waivers are available, published music must retain the
  generated `许可与署名` sidecar. It supplies conservative unified credit for
  VPO, Paul Battersby, and the component sources named by the aggregate notice.
  The sidecar assists compliance; it is not a new permission or legal ruling.
- VPO's `Documentation/license.htm` must remain with the local sound source.
- Tianlai source packages, releases, and Git do not carry these resources.
- Do not publish dry notes, stems, or chromatic full-range auditions that could
  be reused as sample material.
- `grandfathered` is not CC0 and cannot lower the admission threshold for a new
  sound source.
- The 31st viola entry is governed only by its verified VSCO2-CE CC0 subtree;
  that CC0 conclusion is never extended to other VPO directories.
