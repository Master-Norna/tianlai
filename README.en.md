[简体中文](README.md) | **English**

# Tianlai

> **Human hands shape the order; every sound speaks in its own voice.**

Tianlai is an Apache-2.0 open-source, local-first workspace for music rendering
and iteration. It turns editable scores—MIDI, MusicXML, or scores written by a
person or an AI—into stems and ensemble WAV files. When you hear a problem, you
can locate the exact notes at that moment, change one thing, and render a new
version that can be compared with the first.

It is not a black box that accepts one sentence and returns an indivisible
audio file. Tianlai keeps the score, instrumentation, performance parameters,
stems, receipts, and version relationships so that the creator can always
answer:

- Which instrument and notes were sounding at this second?
- Which score, parameters, and sound sources produced this version?
- What did the person or AI change, and exactly how does version two differ?
- Can the result be recomputed and verified against the same resource versions?

```text
One idea / an existing score
          ↓
MIDI / MusicXML / editable score
          ↓
Explicitly choose an instrument for every part → first sound
          ↓
Locate a problem by time → a person or AI edits specific notes
          ↓
Second render → machine-readable differences + human A/B listening
```

Tianlai validates contracts, executes instruments, preserves evidence, and
recomputes results. Melody, structure, orchestration intent, and whether the
music actually sounds good remain the creator's decisions. An AI agent can
join the same loop through MCP, but that does not grant it authority to choose
instruments, edit a score, or replace human aesthetic judgment automatically.

The repository includes a complete example that needs no external samples:
[minimal-loop MusicXML](examples/最小闭环.musicxml) →
[import, edit, and second-render tutorial](docs/从乐谱到第二次渲染.en.md). It
directly exercises import, explicit instrumentation, first sound, location,
patching, second render, and comparison without downloading several gigabytes
of sound sources first.

> Current release candidate: `0.6.0rc1`
>
> **Distribution boundary:** the formal product is the lightweight source ZIP
> published by the project. If PyPI sdists or wheels are published later,
> `tianlai-audio` provides only the reusable Python engine. It does not contain
> the complete instrument tree, schemas, Windows installers, or large sound
> sources and cannot replace the formal source release.

## Choose an entry point

| Environment | Shortest entry point | Current boundary |
| --- | --- | --- |
| Windows 10/11 x64 | [Windows in three steps](#windows-in-three-steps) | Complete reference platform for `0.6.0rc1` |
| Linux / WSL | [Linux / WSL quick start](docs/Linux快速开始.en.md) | Bash, programmatic instruments, and MCP are available; the success path and real-sample coverage are validated in separate layers |
| macOS Apple Silicon / Intel | [macOS quick start](docs/macOS快速开始.en.md) | Native 64-bit CPython 3.11–3.14; clean-source-ZIP portable CI is included, while real samples are accepted separately |

From the source root, Linux / WSL users can start with:

```bash
bash ./bootstrap_linux.sh
```

With a supported 64-bit CPython 3.11–3.14 interpreter, this creates a Linux
`.venv`, installs the core and MCP dependencies, runs environment diagnostics,
and produces a first WAV without external samples. Do not share a `.venv`
between Windows and WSL. See the
[Linux / WSL quick start](docs/Linux快速开始.en.md) for the support boundary,
MCP stdio configuration, and external-sample limitations.

From the source root, macOS users can start with:

```bash
bash ./bootstrap_macos.sh
```

The entry point supports native Apple Silicon `arm64` and Intel `x86_64`. It
creates an architecture-local `.venv`, runs diagnostics, and produces a first
WAV with the reference oscillator. Never share a virtual environment across
operating systems or CPU architectures. All 74 external-resource entries are
now covered by 15 families in the cross-platform Python restorer. Users still
download the large third-party archives locally under their upstream licenses,
and every installation must pass complete integrity verification. See the
[macOS quick start](docs/macOS快速开始.en.md) for the full boundary.

## Windows in three steps

Windows 10/11 x64 with 64-bit CPython 3.11–3.14 is the reference environment
for `0.6.0rc1`. Run the following `cmd` blocks from Command Prompt (`cmd.exe`)
in the source-release root. The multiline continuation character is `^`.

1. Create the project's own virtual environment and skip the automatic smoke
   test for now:

   ```cmd
   安装运行环境.cmd -SkipSmoke
   ```

2. Inspect the actual state of the code, directories, trusted catalog, and
   local resources:

   ```cmd
   检查运行环境.cmd
   ```

3. Use the bundled example and reference oscillator to confirm that a WAV is
   actually written:

   ```cmd
   天籁.cmd render ^
     --instrument "乐器\测试工具\参考振荡器\乐器.json" ^
     --events "examples\c_major.events.json" ^
     --output "output\首次出声\参考振荡器.wav"
   ```

Without `-SkipSmoke`, the first step automatically runs the environment check
and the first-sound render from step three, which makes it suitable for a
double-click start. Installation does not download several gigabytes of sound
sources. When samples are needed, run `安装可恢复音源.cmd -PlanOnly` to inspect
licenses, size, and local state before running `安装可恢复音源.cmd`. Twenty-nine
project-authored entries need no third-party audio assets; the remaining 74
all have a root-level restoration path. The result still has to be verified
with `检查运行环境.cmd` after installation. A missing resource fails explicitly
instead of silently substituting a generic GM sound.

See [Windows minimal start](docs/Windows最小启动.en.md) and
[Windows installation and inspection](docs/Windows安装与巡检.en.md) for more
detail. The complete import-to-second-render walkthrough is
[From score to second render](docs/从乐谱到第二次渲染.en.md).

The repository also includes `examples/最小闭环.musicxml`, a complete workflow
input that needs no external samples, together with its bound render profile,
slice query, and patch. It is both a quick self-check and a safe way to learn
the import → instrumentation → candidate → location → edit → comparison path
before using your own score.

## Recommended creative loop

For `0.6.0rc1`, use this main workflow instead of invoking older import and
ensemble commands separately and assembling their artifacts by hand:

| Stage | CLI | Result |
| --- | --- | --- |
| Unified import | `project-import` | score v1, import report, and a non-executable roster draft |
| Explicit instrumentation | `roster-promote` | a formal roster with exactly one explicit route per part |
| First execution | `project-render` | candidate 1 in a unique directory, with audio, stems, receipt, and binding manifest |
| Listening-based location | `candidate-locate` | maps an actual candidate time back to events, bar, beat, and executor |
| Bounded read | `score-slice` | a local score fragment bound to the score hash |
| Atomic edit | `score-patch` | a new score revision; any conflict rejects the entire patch |
| Second execution | `project-render --parent-candidate ...` | candidate 2 with its parent relationship preserved |
| A/B review | `candidate-compare` + human listening | the machine explains what changed; a person decides which version is better |

Once your own score is ready, the unified-import template is:

```cmd
天籁.cmd project-import ^
  --input "乐谱\曲目\某曲\MusicXML\某曲.mxl" ^
  --output "乐谱\曲目\某曲\导入-01"
```

The paths are placeholders and must be replaced with real files. The generated
`roster-draft.json` is explicitly `executable=false`. Track names, Program
Change, CC7, CC11, and routing suggestions do not gain formal performance
authority automatically. Ordinary parts must explicitly select an
`instrument`; percussion parts must explicitly provide a `kit`. The default
trusted palette is a curation boundary, not a license exemption. Quarantined
resources and local-compatibility-only SoundFonts do not enter the public path
merely because a palette is enabled.

Each `project-render` creates a new unique candidate directory by default and
writes `候选.json` last, binding the score, roster, render profile, performance
plan, and render receipt. Treat candidates as immutable snapshots: do not edit
their JSON or WAV files in place. A normal revision creates a new candidate and
records `parent-candidate`. Controlled replacement is only for an explicit
repair of the same target and must also provide the old receipt hash. During
preparation, the engine freezes the old candidate-manifest hash, then recursively
revalidates the plan, ensemble, stems, and attribution sidecars before and after
the directory exchange. Concurrent changes or an incomplete generation are
never published as a visible candidate.

## Editable intermediate representation

Score v1 is Tianlai's authoritative editable score, not a disposable import
cache. Every note has a score-wide unique and stable `event_id`. Preserve that
ID when moving a note or changing its pitch, velocity, or duration; allocate a
new ID only for a new note. `score-patch` checks both the baseline hash and an
optional previous `expect` value, so stale changes are not silently overwritten
during multi-person or human–AI collaboration.

Notes can also carry optional `staff` and `voice` fields. Simple programmatic
scores may omit them. MusicXML import preserves both identities so ties at the
same pitch but on different staves or voices are not merged after flattening
into one internal part. These fields are not roster parts and do not route
instruments. Preserve them along with `event_id` when editing a MusicXML-derived
score unless the notation structure is intentionally changing.

MIDI and MusicXML can both contain semantics that the current score cannot
represent losslessly. Unified import defaults to `--loss-policy reject`. To
accept degradation, choose `warn` or `allow` explicitly and keep the generated
`import-report.json`. Exported MIDI is an exchange copy with a loss report, not
a lossless inverse of the score.

## AI and MCP

The MCP server currently exposes 15 tools covering contract reading, instrument
discovery, unified import, explicit roster confirmation, score slicing,
patching and comparison, preflight, current-plan location, rendered-candidate
location and comparison, and formal rendering. The recommended MCP chain maps
to the CLI conceptually, while file transport, output roots, and default trusted
gates differ:

```text
import_score_project → confirm_roster → validate_project
        → render(**render_handoff)
        → locate_rendered_candidate → get_score_slice → patch_score
        → validate_project → render(parent_candidate_id=..., **render_handoff)
        → compare_rendered_candidates
```

Only `render` writes audio. Other tools return in-memory objects or read existing
candidates. File-based import follows the MCP input-root policy; attaching a
client does not grant arbitrary access to the whole computer. See the
[MCP interface](docs/MCP.en.md) for the complete 15-tool table, input-root
configuration, and candidate rules. The `render_handoff` returned by preflight
contains both the complete profile and its canonical hash, allowing formal
rendering to reject an accidental profile substitution before candidate
creation. The same document defines the cache, remix, and immutable-candidate
boundaries.

## Current capabilities and boundaries

- There are 103 registered sound entries. Each bound version has completed an
  isolated single-instrument, single-timbre listening check and is marked
  `quality_tier=formal`. This does not mean that every register, dynamic,
  articulation, runtime variant, or expert-level assessment has been covered.
- Formal multi-instrument validation of orchestration, dynamics, space, and
  complete works is still not systematic, so `collaboration_review_status` is
  maintained separately from single-timbre quality.
- `manual`, `analyze`, and `suggest` never edit a score or change audio
  automatically. `suggest` produces only a bounded, non-executable diagnostic
  draft.
- Rendering is offline, not a real-time software instrument. A first cold
  render still executes each track; long, dense works and shared halls can
  require substantial time and peak memory. Stem and content-addressed analysis
  caches mainly accelerate later remixes. Cached renders retain closed
  telemetry, and candidate renders bind its hash in the candidate manifest,
  but a cache does not eliminate the cost of the first performance. With
  `write_stems=true`, a warm remix still rewrites public stems and computes the
  hall and final mix; “all cache hits” does not mean zero I/O.
- Sample playback still has room for resampling improvements. Schemas and tests
  cannot guarantee source quality, usable range, or sound in every combination.
  `strict_hq` is a fail-closed evidence gate, not an audio enhancement switch.
- Peak, RMS, spectrum, phase, range, and difference reports are diagnostic
  instruments. They do not decide whether a melody, arrangement, or work is
  successful. Final candidates still require human A/B listening.

See [Current status](docs/当前状态.en.md) for live details and finer limitations.

## Creative reference

[Tianlai Music Constitution v0.1](docs/音乐创作参考笔记/天籁音乐宪法-v0.1.en.md)
is an included, non-normative creative guide for human creators and AI agents,
not law, a rules engine, or mandatory project policy. Declining to follow it
causes no project penalty or feature restriction. Its text is CC BY 4.0, but
music created with its guidance does not thereby become CC BY, and Tianlai
software remains Apache-2.0.

## Licenses, output, and attribution

Project-authored code, DSP, CLI/MCP, schemas, tests, and configuration are
licensed under [Apache-2.0](LICENSE). Original project attribution is in
[NOTICE](NOTICE), and rules for use of the project name and identity are in
[Trademarks](TRADEMARKS.en.md).

Third-party sound sources, input works, MIDI/MusicXML encodings, and final WAV
files do not automatically become Apache-2.0 material because they passed
through Tianlai. The project also does not acquire copyright in a user's music
merely by rendering it. Before publishing a result, check the generated
`许可与署名.json/.txt`, the rights in the input work, and the applicable upstream
resource terms. See [Output rights](OUTPUT_RIGHTS.en.md) and the
[Sound-source license policy](docs/音源许可政策.en.md) for the complete boundary.

## Directory map

| Directory | Responsibility |
| --- | --- |
| `tianlai/` | Import, scores, conducting, rendering, candidates, diagnostics, and MCP core |
| `乐器/` | Programs, manifests, and verification artifacts for 103 sound entries |
| `schemas/` | Stable JSON contracts |
| `examples/` | Committable and reproducible example inputs |
| `乐谱/` | Local user scores, score revisions, and rosters; ignored by Git by default |
| `音源/` | Large runtime resources and download caches; excluded from the lightweight source package |
| `output/` | Candidates, works, diagnostics, caches, and audition artifacts |
| `docs/` | Installation, usage, interfaces, capability boundaries, and licensing guidance |

Chinese directory and file names are intended for people. Python packages,
JSON fields, commands, and stable identifiers remain in English.

## Development and verification

Contributors should install development dependencies and run pytest:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,mcp]"
.\.venv\Scripts\python.exe -m pytest -q -m "not external_assets and not listening"
```

This is the portable contract that a clean source package must pass. It does
not require large third-party sound sources. `external_assets` tests that need
real samples and `listening` tests that need a frozen audition environment are
separate acceptance layers. A wholly absent resource is skipped, but a present
yet incomplete resource, a hash mismatch, or mismatched physical license
evidence must still fail. Passing tests proves the corresponding machine
contracts; it does not mean every timbre and work has passed human listening
review.

Start with the [documentation map](docs/README.en.md), and see
[From score to second render](docs/从乐谱到第二次渲染.en.md) for the complete
score-iteration contract. Before contributing code or reproducible listening
feedback, read [Contributing](CONTRIBUTING.en.md). Security boundaries and
private-reporting guidance are in the [Security policy](SECURITY.en.md).
