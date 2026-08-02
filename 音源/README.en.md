[简体中文](README.md) | **English**

# Sound-source directory

This directory stores large samples, SFZ files, SoundFonts, local runtime
libraries, download caches, derived resources, and installation receipts. The
lightweight source release keeps only this README. Actual resources are not
committed to Git or distributed in the source ZIP.

Instrument directories retain relative resource paths, frozen versions,
hashes, mappings, behavior code, and verification information. A missing or
mismatched resource makes its dedicated entry fail explicitly; Tianlai never
silently substitutes a generic GM sound.

## Coverage

The project registers 103 formal sound entries:

| Type | Count | Restoration method |
| --- | ---: | --- |
| Project-authored DSP, synthesis, modeling, and sound effects | 29 | No third-party audio assets required |
| External resources covered by the unified restoration manifest | 74 | 15 resource families in `resource_restore_manifest.json` |

All 74 external-resource entries therefore have a root-level installation
path. “Coverage” means that a user can download, verify, and install a frozen
public upstream resource on their own computer. It does not mean Tianlai has
permission to mirror or repackage the original samples.

## Recommended entry point

After installing the minimal Python environment, inspect a read-only plan:

```cmd
安装可恢复音源.cmd -PlanOnly
```

With no arguments, the installer first displays licenses, existing state,
download size, and disk estimates. It processes all 74 external resources only
after the user enters `INSTALL`:

```cmd
安装可恢复音源.cmd
```

Select unified-manifest resources as needed:

```cmd
安装可恢复音源.cmd -ResourceFamily vcsl
安装可恢复音源.cmd -ResourceGroup freepats
```

Install only the optional project-local FluidSynth compatibility runtime and
do not process external sample resources:

```cmd
安装可恢复音源.cmd -LegacyOnly
```

The unified manifest currently freezes 15 resource families and covers all 74
external-resource entries, including VCSL, FreePats, Karoryfer, Emilyguitar,
MTG Solo Sax, VPO, Greg Sullivan E-Pianos, Salamander, ganjo, and SIMPK. The
complete manifest downloads about 7.17 GiB and occupies about 9.86 GiB after
installation and derivation; reserve at least 24 GiB. Use the local `-PlanOnly`
result for the actual remaining amount.

## Installation safety

The unified restorer enforces these contracts:

- URLs point only to public upstream sources.
- Every resource uses either a fixed archive SHA-256 or a fixed commit plus a
  full extracted-tree SHA-256.
- Downloads go to `下载缓存/<file>.part`, with a maximum-size limit and resume
  support.
- Every archive passes frozen integrity and member-path checks first. ZIP and
  tar.xz archives also have declared-size limits before extraction; a 7z archive
  must have a fixed SHA-256. After extraction, all formats reject links and
  reparse points and verify the complete tree.
- The resource is built in a unique staging directory on the target volume and
  atomically renamed only after the whole tree passes.
- An existing matching tree is verified only. A mismatching destination is not
  merged or overwritten.
- Installation receipts are written to `.tianlai/receipts/`.

If the server rejects a resume range or an existing `.part` is corrupt, the
restorer retries from zero at most once. A further failure preserves evidence
and prints an explicit command. When one selected resource family may safely be
downloaded again, use:

```cmd
安装可恢复音源.cmd -ResourceFamily vcsl -RestartDownload
```

This switch removes only the controlled `.part` belonging to that manifest
archive. It does not delete a verified cache or formal sound-source tree.

## Directory responsibilities

Typical layout:

```text
音源/
├─ 下载缓存/              # deletable and re-downloadable; includes partial .part files
├─ 派生/                  # deterministically derived from an upstream tree and frozen parameters
├─ .tianlai/receipts/     # installation and complete-tree verification receipts
├─ VCSL/
├─ FreePats/
├─ Karoryfer/
├─ Emilyguitar/
├─ MTG-Solo-Sax/
├─ VirtualPlayingOrchestra/
├─ 钢琴/
└─ 通用/
```

Do not place the entire `音源/` directory in a source release. When migrating
local resources, preserve their licenses and receipts. Recheck actual state
after migration with `检查运行环境.cmd`.

## Compatibility dedicated installers

These pinned PowerShell installers remain for per-resource compatibility,
diagnostics, and historical verification. The root restoration flow treats the
unified Python manifest as authoritative and no longer dispatches to these
sample installers a second time:

- Salamander Grand Piano: fixed official commit; verifies the license, README,
  main SFZ, 641 FLAC files, and complete 668-file tree.
- Yamaha CP80: fixed Greg Sullivan E-Pianos commit; verifies 81 FLAC files and
  complete evidence.
- SIMPK 1793 clavichord: fixed approximately 1.5 GB archive, 756 upstream WAV
  files, and tuning map.
- itsclipping ganjo v1.000: CC0, fixed commit, and complete 66-file tree.
- Virtual Playing Orchestra: fixed official Wave 3.2 and Standard 3.3 archives;
  verifies the merged 1,922-file tree.
- FluidSynth: project-local Windows x64 runtime; no system installation is
  required.

They may also be run individually through PowerShell scripts in the respective
instrument directories. A matching tree is verified only; any mismatch is
never merged in place.

## SoundFont compatibility boundary

GeneralUser GS and TimGM are not part of the default, public/trusted, or
103-entry dedicated-instrument path. Install them explicitly only for an old
private project's compatibility or SoundFont backend testing:

```powershell
.\安装通用音源.ps1 -InstallLocalCompatibilitySoundFonts
```

The GeneralUser GS upstream notice acknowledges that the provenance of some
samples cannot be established completely. TimGM's GPL-2.0 terms do not state a
clear music-output exception. Both are limited to local compatibility/testing,
do not authorize publication of a Tianlai work on that basis, and never
silently fall back to each other. Users are likewise responsible for checking
the license of any free or paid SoundFont they connect themselves.

## License boundary

> This English section is an informational translation. Upstream license texts
> and the physical evidence retained with each installed resource control the
> use of third-party material.

Project code is Apache-2.0, while third-party samples and tools remain governed
by their own upstream licenses. Installers preserve licenses, attribution, and
change evidence. Before publishing audio, inspect the `许可与署名.json/.txt`
generated for that candidate.

The steel-string guitar is installed unchanged from the official FreePats
package and is neither mirrored nor extracted by Tianlai. MTG Solo Sax is
CC-BY-4.0 and requires attribution in public output. `grandfathered` resources
such as VPO may be restored locally only within their frozen official-package
boundary; this does not authorize redistribution of samples. See the
[sound-source license policy](../docs/音源许可政策.en.md),
[VPO license and installation guide](../docs/VPO音源许可与安装说明.en.md), and
[`OUTPUT_RIGHTS.en.md`](../OUTPUT_RIGHTS.en.md).
