[简体中文](Linux快速开始.md) | **English**

# Linux / WSL quick start

This page is for developers using the CLI, MCP, or a custom agent in a Linux
environment. The shortest path installs only the Python runtime and MCP
dependencies and produces a first WAV with a bundled programmatic instrument.
It does not automatically download several gigabytes of third-party sound
sources.

## Support boundary

Keep these support layers distinct:

| Layer | Support | Meaning |
| --- | --- | --- |
| Source and portable self-checks | Ubuntu 22.04+ x86_64, WSL2 x86_64, and 64-bit CPython 3.11–3.14 | Does not mean every third-party sample is installed |
| Minimal CLI and MCP path | Environment creation, diagnostics, first sound with a programmatic instrument, and the stdio MCP editing loop | The package has no Windows-host-to-WSL forwarding bridge |
| 29 project-authored programmatic instruments | Require no third-party audio assets and are directly usable | Other sound entries need separate resource restoration |
| 74 external resources | Diagnostics expose a cross-platform Python restorer; `plan` resolves all 15 resource families | Download size, upstream availability, license conditions, and system unpacking dependencies vary; CI does not download large resources |

Windows 10/11 x64 remains the complete reference platform for `1.0.0`.
Linux covers core programmatic instruments, portable self-checks, CLI, and MCP;
large third-party resource coverage is not identical to Windows.

WSL users should preferably extract or check out the source into the Linux
filesystem, for example `/home/alice/src/tianlai`. Do not share one `.venv`
between Windows and Linux; their interpreters, launchers, and binary
dependencies are incompatible. Running under a Windows-mounted path such as
`/mnt/c` or `/mnt/d` also makes many-small-file I/O slower in most setups.

## 1. Prepare system dependencies

On Linux, Tianlai requires an x86_64 host and 64-bit CPython 3.11–3.14. The
Python 3.12 included with Ubuntu 24.04 x86_64 is supported. If the
distribution's `python3` is still 3.10, install a supported interpreter and
pass its absolute path to the bootstrap script.

An unsupported operating system, architecture, or interpreter is rejected
before environment creation. Do not replace the distribution's system Python
for Tianlai; install a parallel
version, use a version manager, or use a newer distribution.

On Ubuntu 24.04, or a Debian-family distribution whose default `python3` is
already supported, install:

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  libsndfile1 libarchive-tools ca-certificates
python3 --version
```

`libsndfile1` is SoundFile's system-level fallback for reading and writing
audio. Minimal programmatic instruments need neither FluidSynth nor a system
audio device; Tianlai writes WAV files, and the user chooses a player.
`libarchive-tools` provides `bsdtar`, which safely preflights and extracts the
bagpipe, Spanish guitar, and accordion 7z resource families in the unified
manifest. GNU tar is not a substitute.

## 2. Create the environment and produce the first sound

Enter the source-package root and run:

```bash
cd /home/alice/src/tianlai
bash ./bootstrap_linux.sh
```

The script:

1. selects a supported 64-bit Python;
2. creates the project's own `.venv`;
3. installs the Tianlai core and optional MCP dependencies;
4. runs environment diagnostics;
5. uses the sample-free reference oscillator to generate
   `output/首次出声/参考振荡器.wav`.

To specify an interpreter:

```bash
bash ./bootstrap_linux.sh --python /usr/bin/python3.12
```

To inspect installation and first sound separately:

```bash
bash ./bootstrap_linux.sh --skip-smoke
mkdir -p "$PWD/output/首次出声"
"$PWD/.venv/bin/python" -m tianlai render \
  --instrument "$PWD/乐器/测试工具/参考振荡器/乐器.json" \
  --events "$PWD/examples/c_major.events.json" \
  --output "$PWD/output/首次出声/参考振荡器.wav"
```

This render passes through event parsing, the programmatic instrument, audio
writing, and atomic publication. It proves that the current Python environment
can run Tianlai and produce a valid WAV. It does not prove that large sample
sets such as VPO, VCSL, or FreePats are available.

Recheck actual local resource state at any time:

```bash
"$PWD/.venv/bin/python" -m tianlai.doctor --start "$PWD"
```

Diagnostics report an absent external source as `missing`. On Linux, all 74
external resources map to the cross-platform Python restoration entry point.
This does not mean the large archives have already been downloaded, and it
does not replace the user's review of upstream licenses and network
availability. Absence does not prevent the reference oscillator from sounding.
A partially present resource, hash mismatch, or broken manifest reference must
be repaired and cannot be skipped as an ordinary absence.

## 3. Verify the portable contract

On first installation, ask the script to install development dependencies and
run the portable tests as well:

```bash
bash ./bootstrap_linux.sh --portable-tests
```

With an existing environment, run them directly:

```bash
"$PWD/.venv/bin/python" -m pip install -e ".[dev,mcp]"
"$PWD/.venv/bin/python" -m pytest -q \
  -m "not external_assets and not listening"
```

This is the test contract for a clean source package. `external_assets` needs
actual third-party sound sources and `listening` needs frozen audition
material. Neither belongs to portable failures, and neither should be presented
as covered by the portable suite.

## 4. Connect MCP / an agent

The following configuration applies when the client itself runs on Linux or in
a WSL Remote environment. `command` and `cwd` must be real absolute Linux
paths; JSON does not expand `~`, `$HOME`, or shell variables.

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "/home/alice/src/tianlai/.venv/bin/python",
      "args": ["-m", "tianlai.mcp_entry"],
      "cwd": "/home/alice/src/tianlai",
      "env": {
        "TIANLAI_INPUT_ROOTS": "/home/alice/scores:/mnt/d/shared-scores"
      }
    }
  }
}
```

Linux separates multiple `TIANLAI_INPUT_ROOTS` with a colon; Windows uses a
semicolon. Add only score directories that are intentionally exposed to the
agent. Relative input paths still resolve from the Tianlai root in `cwd`.

Before configuring a client, verify that the entry point and dependencies can
be imported:

```bash
"$PWD/.venv/bin/python" -c \
  "import tianlai.mcp_server; print('Tianlai MCP import: OK')"
```

The actual service uses stdio. After the client starts, call these first:

```text
score_and_roster_format()
list_instruments()
```

Then follow this loop:

```text
import_score_project → confirm_roster → validate_project
    → check_project_readiness → render(**render_handoff)
    → locate_rendered_candidate → get_score_slice → patch_score
    → validate_project → check_project_readiness
    → render(parent_candidate_id=..., **render_handoff)
    → compare_rendered_candidates
```

An MCP client running on the Windows host cannot execute
`/home/.../.venv/bin/python` directly. Run the client inside WSL/Remote or use
the Windows `.venv\Scripts\python.exe` entry point. The source package does not
include a Windows-host-to-WSL forwarding bridge.

See the [MCP interface](MCP.en.md) for all tools, permissions, and
immutable-candidate rules.

## 5. Import your own MIDI / MusicXML

The CLI uses the same Linux path conventions:

```bash
"$PWD/.venv/bin/python" -m tianlai project-import \
  --input "/home/alice/scores/demo.musicxml" \
  --output "$PWD/乐谱/曲目/demo/导入-01"
```

Import produces only a score, report, and roster draft with
`executable=false`; a MIDI Program Change never gains formal instrument routing
authority automatically. Continue with
[From score to second render](从乐谱到第二次渲染.en.md) to confirm
instrumentation explicitly, render a candidate, locate by time, and produce a
second version.

## 6. Large sound sources

Read the restoration plan first:

```bash
"$PWD/.venv/bin/python" -m tianlai.resource_restore \
  --home "$PWD" plan
```

This command downloads nothing. It displays the 15 families and 74 entries in
the unified manifest. After checking licenses, download size, and disk space,
use the same module's `install` subcommand according to the plan. The 7z
resources need `bsdtar` (`libarchive-tools` on Ubuntu); a missing dependency is
reported before download.

The restorer never silently substitutes another timbre for a missing or
mismatched resource. For the complete Windows reference workflow, see
[Windows installation and inspection](Windows安装与巡检.en.md).

## Troubleshooting

### `No supported 64-bit Python 3.11-3.14 was found`

The current `python3` is unsupported or is not 64-bit. Install a supported
interpreter and pass it explicitly:

```bash
bash ./bootstrap_linux.sh --python /absolute/path/to/python3.12
```

### `could not create .venv`

On Debian / Ubuntu, the matching `venv` package is usually missing. Install
`python3-venv` or `python3.12-venv` for the interpreter version, remove the
incomplete `.venv`, and retry.

### Diagnostics say `.venv` is a Windows environment

The current directory contains a Windows virtual environment. Do not reuse it
across platforms. Use a separate source tree in WSL's Linux filesystem or move
the old environment aside before creating the Linux `.venv`.

### `soundfile` / `libsndfile` fails to load

Install the system library and recheck:

```bash
sudo apt install -y libsndfile1
"$PWD/.venv/bin/python" -c "import soundfile; print(soundfile.__version__)"
```

### The MCP client says the server is disconnected

Verify in order:

1. `command` is the absolute path to Linux `.venv/bin/python`.
2. `cwd` is the source root containing `pyproject.toml`, `乐器/`, and
   `可信乐器.json`.
3. `args` is `["-m", "tianlai.mcp_entry"]`.
4. The client process really runs in Linux/WSL, not on the Windows host.
5. Importing `tianlai.mcp_server` directly has no dependency error.

A stdio service has no ordinary interactive interface while it waits for the
client handshake. Do not mistake the absence of a terminal prompt for a hung
service.

### Chinese paths or `/mnt/c` are slow

Tianlai supports UTF-8 project paths, but a Windows filesystem mounted by WSL
is usually slower than the Linux filesystem for many small files. Keep the
source, `.venv`, and frequently written output in the WSL Linux filesystem. If
a third-party MCP client cannot handle Chinese paths, use a short ASCII absolute
path; Tianlai's data contracts do not need to change.
