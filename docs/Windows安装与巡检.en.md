[简体中文](Windows安装与巡检.md) | **English**

# Windows installation and inspection

Windows 10/11 x64 with Windows PowerShell 5.1 is the complete reference
environment. Tianlai uses a project-local virtual environment; FluidSynth and
Python dependencies do not need to be installed into system directories. Linux
and WSL users should read the [Linux / WSL quick start](Linux快速开始.en.md);
Mac users should read the [macOS quick start](macOS快速开始.en.md).

## Prepare Python

Tianlai requires 64-bit CPython 3.11–3.14. If Python is not installed, obtain a
64-bit build from the
[official Python downloads for Windows](https://www.python.org/downloads/windows/)
and make sure `py.exe` or `python.exe` can be called from a command line.

An unsupported implementation, version, or 32-bit interpreter is rejected
before environment creation, so it does not leave behind a misleading partial
`.venv`.

## Install the minimal runtime

From the source-package root, run:

```cmd
安装运行环境.cmd
```

This entry point:

1. creates or reuses the project-local `.venv`;
2. installs the core and MCP dependencies;
3. runs environment diagnostics;
4. uses the reference oscillator to generate
   `output\首次出声\参考振荡器.wav`.

The reference oscillator needs no third-party samples, so several gigabytes of
sound sources do not have to be downloaded first. To install and inspect the
environment without generating a WAV, use:

```cmd
安装运行环境.cmd -SkipSmoke
```

The installer does not change the system's persistent PowerShell execution
policy.

## Inspect current state

```cmd
检查运行环境.cmd
```

The report separately shows whether code and directories are usable and whether
each external resource is `ready`, `missing`, or `invalid`. A complete resource
that has never been installed may be normally `missing`. A resource that is
present but incomplete, has a mismatched digest, or lacks physical license
evidence is reported as an error.

For a concise report:

```cmd
检查运行环境.cmd --quick
```

Resource state is always determined by the current local inspection. The source
package contains no large third-party samples.

## Restore external sound sources as needed

The project has 103 formal sound entries. Twenty-nine project-authored DSP,
synthesis, or procedural-sound entries need no third-party audio assets; the
other 74 depend on external public resources. Start with a read-only plan that
shows licenses, download size, disk space, and local state:

```cmd
安装可恢复音源.cmd -PlanOnly
```

After reviewing the plan, run:

```cmd
安装可恢复音源.cmd
```

The script displays the complete plan and starts downloading and installing
only after the user enters exactly `INSTALL`. All 74 external-resource entries
are covered by the unified manifest in 15 resource families. Old per-resource
PowerShell installers remain available for compatibility and diagnostics, but
the root flow does not dispatch to them a second time.

To install only a required subset:

```cmd
安装可恢复音源.cmd -ResourceFamily vcsl
安装可恢复音源.cmd -ResourceGroup freepats
```

Use `-PlanOnly` for valid IDs, required space, and the resources missing on this
computer. The complete unified manifest downloads about 7.17 GiB and occupies
about 9.86 GiB after installation and derivation. Reserve at least 24 GiB for
archive caches, same-volume staging, and atomic publication. Use the local plan
for the actual remaining amount.

`-LegacyOnly` now installs only the optional project-local FluidSynth
compatibility runtime; it does not process the 74 external sample resources.
Core first sound and formal dedicated instruments do not fall back to it.

## Download and installation safety

Downloads first go to `音源\下载缓存\<archive>.part` and support resume. If a
server does not support ranges or returns an unusable range, the restorer
restarts that controlled temporary download at most once. A second failure
preserves the `.part` and reports an error. When a selected temporary download
is known to be safe to restart from zero, use:

```cmd
安装可恢复音源.cmd -ResourceFamily vcsl -RestartDownload
```

This switch removes only the controlled `.part` for the selected resource
family. It does not delete a verified cache or formal sound-source tree.

Archives are verified using a fixed SHA-256 or a fixed commit plus the digest
of the complete extracted tree. The restorer builds in a same-volume temporary
directory and publishes atomically only after every check passes. An existing
matching directory is verified only; a mismatching directory is never merged
or overwritten. Concurrent installation uses a process lock and no-clobber
publication and does not require changing the Windows long-path registry
setting.

## SoundFont compatibility

GeneralUser GS and TimGM are not part of the default or trusted instrument
path. Run the following explicitly only for old-project compatibility or
SoundFont backend testing:

```powershell
.\安装通用音源.ps1 -InstallLocalCompatibilitySoundFonts
```

The GeneralUser GS upstream notice acknowledges that the provenance of some
samples cannot be established completely. TimGM's GPL-2.0 terms do not state a
clear rendered-audio output exception. Both are limited to local compatibility
and testing and are never selected silently when a dedicated resource is
missing.

## License boundary

> This English section is an informational translation. Original upstream
> license texts and retained physical evidence control third-party resources.

The restorer downloads from public upstream sources to the user's computer;
Tianlai neither mirrors nor repackages third-party samples. License admission,
timbre quality, and default trusted curation are independent states. Changing
ordinary discovery options never releases a quarantined resource.

Mixed-license resources such as VPO are installed locally from a pinned
official distribution. Tianlai may not redistribute their original samples.
See the [sound-source license policy](音源许可政策.en.md) and
[VPO license and installation guide](VPO音源许可与安装说明.en.md).

Before publishing music, inspect `许可与署名.json/.txt` next to the rendered
result and also confirm the rights conditions for the input work and
third-party resources.

## Troubleshooting

### No supported Python found

Confirm that 64-bit CPython 3.11–3.14 is installed, open a new Command Prompt,
and run:

```cmd
py -0p
```

### The environment came from Linux / macOS or is damaged

Windows, Linux / WSL, and macOS cannot share a `.venv`. Move the inapplicable
virtual environment aside and rerun `安装运行环境.cmd`.

### An instrument cannot render

Run `检查运行环境.cmd` first. If the resource is `missing`, use
`安装可恢复音源.cmd -PlanOnly` to find its resource family. If it is `invalid`, do
not substitute another timbre; repair the incomplete installation according to
the report.

### A download was interrupted

Rerun the original command to resume its controlled `.part`. Use
`-RestartDownload` only when the download must be restarted from zero.
