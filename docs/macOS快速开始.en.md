[简体中文](macOS快速开始.md) | **English**

# macOS quick start

This page is for people using Tianlai's CLI, MCP, or a custom agent on Apple
Silicon or Intel Macs. The bundled `bootstrap_macos.sh` creates an isolated
Python environment, runs diagnostics, and produces a first WAV with the
reference oscillator. It does not automatically download several gigabytes of
third-party sound sources or install FluidSynth system-wide.

> **Current acceptance status:** on macOS 15, the configured portable gate covers the full
> CPython 3.11–3.14 matrix on native `arm64` and `x86_64`; macOS 26 revalidates
> CPython 3.14 on both architectures. The tag gate also makes both OS generations
> and both architectures verify the same candidate ZIP. Every job extracts into
> a path containing Unicode and spaces before onboarding, tests, and first sound.
> Once merged, the actual GitHub Actions result remains authoritative. A portable
> pass does not constitute exhaustive real-machine acceptance of every large
> third-party resource.

## Support boundary

| Layer | macOS support | Meaning |
| --- | --- | --- |
| Source and portable self-checks | Apple Silicon `arm64`, Intel `x86_64`, and 64-bit CPython 3.11–3.14 | Python architecture must match the current native host |
| Minimal CLI and MCP path | Environment creation, doctor, reference-oscillator first sound, stdio MCP, and portable tests | Writes WAV offline and needs no system audio device |
| 29 project-authored programmatic instruments | Require no third-party audio assets and are directly usable | Other sound entries need separate resource restoration |
| 74 external resources | `resource_restore plan` resolves all 15 resource families through the cross-platform Python entry point; Mac gates require a verified bsdtar / libarchive | Confirm downloads, upstream availability, license conditions, space, and large resource payloads locally; CI does not download those resources |
| FluidSynth / SoundFont | Optional compatibility layer; discovers and loads a system/Homebrew library or an explicit `.dylib` path | Doctor reports the native layer available only after the library loads and exposes the required API; this is not a core first-sound dependency, and formal entries do not silently fall back to it |

macOS support does not mean every third-party source for all 103 entries has
completed real-sample rendering on every kind of Mac. Before using an external
resource, rely on the local `tianlai-doctor` result and its retained physical
license evidence.

## 1. Prepare native Python

You need 64-bit CPython 3.11–3.14 whose architecture matches `uname -m`. Use an
[official python.org installer](https://www.python.org/downloads/macos/) or
Homebrew:

```bash
brew install python@3.13
```

Inspect the host and interpreter first:

```bash
uname -s
uname -m
/usr/sbin/sysctl -in sysctl.proc_translated 2>/dev/null || true
python3 -c 'import platform, struct; print(platform.system(), platform.machine(), struct.calcsize("P") * 8)'
```

`uname -s` must be `Darwin`. Common architectures are `arm64` on Apple Silicon
and `x86_64` on Intel; `sysctl.proc_translated` must not print `1`. Do not reuse
an x86_64/Rosetta virtual environment in a native arm64 project, or the reverse.
Before creating `.venv`, the bootstrap rejects Rosetta translation and any
unsupported version, implementation, bitness, operating system, or architecture.

## 2. Create the environment and produce the first sound

Extract or check out the source into an ordinary user-writable directory, then
run:

```bash
cd "/Users/alice/Projects/Tianlai"
bash ./bootstrap_macos.sh
```

The script:

1. finds a native 64-bit CPython 3.11–3.14;
2. creates or reuses the project's own `.venv`;
3. installs the Tianlai core and optional MCP dependencies;
4. runs `python -m tianlai.doctor --start <source root>`;
5. generates `output/首次出声/参考振荡器.wav` with the reference oscillator and
   validates its WAV metadata.

The first Python-package installation needs network access. The script does not
use `sudo`, modify shell configuration, install system FluidSynth, or download
large sound sources.

To select an interpreter:

```bash
bash ./bootstrap_macos.sh --python /opt/homebrew/bin/python3.13
```

Intel Homebrew normally uses `/usr/local`; do not copy an Apple Silicon
`/opt/homebrew` path mechanically. Use the actual result of
`command -v python3.13`.

To install and diagnose without producing a test WAV:

```bash
bash ./bootstrap_macos.sh --skip-smoke
```

To see every option:

```bash
bash ./bootstrap_macos.sh --help
```

## 3. Run the portable gate

During first setup, install development dependencies and run the complete
sample-free portable suite with:

```bash
bash ./bootstrap_macos.sh --portable-tests
```

With an existing environment:

```bash
"$PWD/.venv/bin/python" -m pip install -e ".[dev,mcp]"
"$PWD/.venv/bin/python" -m pytest -q \
  -m "not external_assets and not listening"
```

`external_assets` needs actual third-party sound sources; `listening` needs
frozen audition material. They are separate acceptance layers. Passing portable
tests does not prove that every sample, articulation, or work passed listening
review.

## 4. CLI and MCP

Use the project virtual environment for the CLI:

```bash
"$PWD/.venv/bin/python" -m tianlai --help
"$PWD/.venv/bin/python" -m tianlai.doctor --start "$PWD" --quick
```

Example macOS MCP client configuration:

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "/Users/alice/Projects/Tianlai/.venv/bin/python",
      "args": ["-m", "tianlai.mcp_entry"],
      "cwd": "/Users/alice/Projects/Tianlai",
      "env": {
        "TIANLAI_INPUT_ROOTS": "/Users/alice/Music/Scores:/Volumes/Shared/Scores"
      }
    }
  }
}
```

`command` and `cwd` must be real absolute paths; JSON does not expand `~`,
`$HOME`, or shell variables. Like Linux, macOS separates multiple
`TIANLAI_INPUT_ROOTS` with a colon. Add only directories that are intentionally
exposed to the agent. See the [MCP interface](MCP.en.md) for complete tools and
permission boundaries.

## 5. Restore external sound sources

Read the no-download plan first:

```bash
"$PWD/.venv/bin/python" -m tianlai.resource_restore \
  --home "$PWD" plan
```

macOS resolves all 15 families and 74 entries in the unified manifest. Check
licenses, download size, disk space, and local dependencies, then use the same
module's `install` subcommand according to the plan. Large third-party sound
sources are neither included in the source package nor downloaded in ordinary
CI; a missing or mismatched source is never silently substituted.

Some unified resources use 7z. The restorer accepts only `bsdtar` capability
and never treats GNU tar as equivalent. macOS system `tar` can be used when it
actually identifies as libarchive/bsdtar. If detection fails, install Homebrew
libarchive and add its `bin` to the current terminal temporarily:

```bash
brew install libarchive
export PATH="$(brew --prefix libarchive)/bin:$PATH"
bsdtar --version
```

The source package carries no large samples. Rerun doctor after installation.
A partial installation, hash mismatch, or missing physical license evidence
must be repaired and cannot be skipped as an ordinary `missing` resource.

## 6. Optional FluidSynth / SoundFont

Core programmatic instruments and first sound do not need FluidSynth. Install
the optional SoundFont compatibility backend separately only when required:

```bash
brew install fluid-synth
"$PWD/.venv/bin/python" -m pip install -r requirements-soundfont.txt
```

The runtime discovers a system or Homebrew FluidSynth. It can also preload a
`.dylib` from the project-local directory or a directory named by
`TIANLAI_FLUIDSYNTH_DIR`, binding the actual library by canonical absolute path.
Never place an untrusted dynamic library in a search directory. GeneralUser GS,
TimGM, and user-provided SoundFonts remain explicit local compatibility/test
material and never enter default public/trusted routing.

## Troubleshooting

### `Rosetta translation is active`

The current terminal is translated by Rosetta. Leave it, confirm in Finder that
Terminal or iTerm is not configured to "Open using Rosetta," and open a native
terminal. Do not reuse a `.venv` created under translation; use a fresh source
directory or move the old `.venv` outside the project before rerunning the
bootstrap.

### `No supported native 64-bit Python 3.11-3.14 was found`

Install a supported native CPython and pass its actual path:

```bash
bash ./bootstrap_macos.sh --python "$(command -v python3.13)"
```

### Interpreter architecture does not match the host

Compare:

```bash
uname -m
"/path/to/python" -c 'import platform; print(platform.machine())'
```

Both results must match. Leave a Rosetta terminal or choose a matching Python,
then use a fresh source directory or move the inapplicable `.venv` aside and
rebuild it.

### `.venv` came from Windows / Linux or is incomplete

A virtual environment cannot be shared across operating systems,
architectures, or source snapshots. Do not overwrite its interpreter. Use a
separate checkout or move the old `.venv` aside before rerunning the bootstrap.

### `soundfile` or first-WAV validation fails

First check that Python and dependencies do not mix architectures:

```bash
"$PWD/.venv/bin/python" -m pip check
"$PWD/.venv/bin/python" -c 'import platform, soundfile; print(platform.machine(), soundfile.__version__)'
```

If custom dynamic libraries are involved, remove related `DYLD_*` overrides
temporarily and recreate a clean `.venv`.

### The MCP client cannot connect

Confirm that `command` is the absolute `.venv/bin/python` path, `cwd` is the
source root, and `args` is `["-m", "tianlai.mcp_entry"]`. A stdio service has no
ordinary interactive prompt while waiting for a client handshake; a silent
terminal does not mean the server is hung.

## License and publication boundary

> This English section is an informational translation. Original upstream
> license texts and retained physical evidence control third-party resources.

macOS compatibility changes no license. Project code remains Apache-2.0;
third-party sound sources, input works, and output music retain their own rights
status. The restorer installs only from frozen public upstream sources onto the
user's computer and does not authorize mirroring or repackaging samples. Before
publishing music, inspect `许可与署名.json/.txt`, rights in the input work, and
upstream terms. See the [sound-source license policy](音源许可政策.en.md) and
[output-rights statement](../OUTPUT_RIGHTS.en.md).
