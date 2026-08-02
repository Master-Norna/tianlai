[简体中文](Windows最小启动.md) | **English**

# Windows minimal start

`安装运行环境.cmd` in the source-release root is the smallest first-use entry
point. Double-click it or run it from Command Prompt:

```cmd
安装运行环境.cmd
```

It uses `ExecutionPolicy Bypass` only inside that one PowerShell process and
does not permanently change the policy for the current user or the system. The
script:

1. checks for 64-bit CPython 3.11–3.14;
2. creates or reuses the project-local `.venv`;
3. installs the core and optional MCP dependencies in editable mode;
4. runs `python -m tianlai.doctor` environment diagnostics;
5. uses the bundled reference oscillator to generate
   `output\首次出声\参考振荡器.wav`.

This process does not install FluidSynth or download a large sound source. A
missing external sample library appears as `missing` in diagnostics but does
not prevent the reference oscillator from producing the first sound. The first
run still needs network access to install the core Python and MCP dependencies.

Diagnostic `ready` means runtime directories, implementation files, physical
license evidence, and manifest/SFZ references are resolvable. To remain light,
the check does not reread and hash several gigabytes of audio at every start.
Sound-source installers verify a pinned archive digest or a pinned commit plus
the complete-tree digest. See
[Windows installation and inspection](Windows安装与巡检.en.md) for plans,
licenses, space requirements, and restoration of large samples.

To rebuild the environment and run diagnostics without generating a test WAV:

```cmd
安装运行环境.cmd -SkipSmoke
```

For a machine-readable result after environment installation:

```powershell
.\.venv\Scripts\python.exe -m tianlai.doctor --json
```

To require every formal instrument resource and return nonzero when one is
missing:

```powershell
.\.venv\Scripts\python.exe -m tianlai.doctor --json --require-all-resources
```

SoundFont support is an explicit local-compatibility feature, not a minimal
core dependency. Install that backend separately only when needed:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-soundfont.txt
```
