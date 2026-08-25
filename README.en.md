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

> Current release: `1.1.0`
>
> **Distribution boundary:** the formal product is the lightweight source ZIP
> published by the project. If PyPI sdists or wheels are published later,
> `tianlai-audio` provides only the reusable Python engine. It does not contain
> the complete instrument tree, schemas, Windows installers, or large sound
> sources and cannot replace the formal source release.

## Choose an entry point

| Environment | Shortest entry point | Current boundary |
| --- | --- | --- |
| Windows 10/11 x64 | [Windows in three steps](#windows-in-three-steps) | Complete reference platform for `1.1.0` |
| Linux / WSL x86_64 | [Linux / WSL quick start](docs/Linux快速开始.en.md) | Bash, programmatic instruments, and MCP are available; the success path and real-sample coverage are validated in separate layers |
| macOS Apple Silicon / Intel | [macOS quick start](docs/macOS快速开始.en.md) | Native 64-bit CPython 3.11–3.14; clean-source-ZIP portable CI is included, while real samples are accepted separately |

From the source root, Linux / WSL users can start with:

```bash
bash ./bootstrap_linux.sh
```

On Linux x86_64 with a supported 64-bit CPython 3.11–3.14 interpreter, this
creates a Linux `.venv`, installs the core and MCP dependencies, runs
environment diagnostics, and produces a first WAV without external samples.
Do not share a `.venv` between Windows and WSL. See the
[Linux / WSL quick start](docs/Linux快速开始.en.md) for the support boundary,
MCP stdio configuration, and external-sample installation and restoration.

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
for `1.1.0`. Run the following `cmd` blocks from Command Prompt (`cmd.exe`)
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

For `1.1.0`, use this main workflow instead of invoking older import and
ensemble commands separately and assembling their artifacts by hand:

| Stage | CLI | Result |
| --- | --- | --- |
| Unified import | `project-import` | score v1, import report, and a non-executable roster draft |
| Explicit instrumentation | `roster-promote` | a formal roster with exactly one explicit route per part |
| First execution | `project-render` | candidate 1 in a unique directory, with audio, stems, receipt, and binding manifest |
| Generation verification | `candidate-verify` | proves that the descriptor-bound bytes read as one closed, self-consistent generation |
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
`instrument`; percussion parts must explicitly provide a `kit`. The default MCP
`formal` scope covers all 103 formal sound entries. Each result also carries a
`curated` marker for the 25 creator-curated entries, and callers may explicitly
select that smaller `curated` scope.

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

New ordinary MCP candidates are organized as
`output/mcp/<sanitized title without an identity hash>/<candidate_id>/`. The
`work_id` in `候选.json` remains a hash-bound identity and normally differs from
that clean parent directory; `candidate_id` and the candidate-directory name do
not change. Authoring and workflow internals remain separately managed in the
dedicated `output/mcp/authoring-projects/` namespace.

To verify a saved candidate independently, run
`天籁.cmd candidate-verify --candidate "candidate-directory"`. After copying or
extracting, the final directory must still be the manifest-bound `candidate_id`;
its parent may be the new sanitized title or the legacy `work_id`. The command
rejects extra files, directories, links or reparse points, hard links, and
detected drift in bound artifacts. A report written with `--output`
must stay outside the candidate directory. `integrity_verified=true` proves
only that the descriptor-bound bytes form a closed, self-consistent local
generation; it does not prove authorship, provenance, content quality, or that
an uncooperative writer cannot change the live directory after verification.

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

For the finer-grained work model, see [Score v2: exact, portable work
semantics](docs/score-v2.en.md). The foundation now supports exact rational time,
separate written and sounding pitch, stable explicit relations, trusted
`ScoreSourceSnapshot` loading, and explicit v1-to-v2 migration. The first formal
slice now has a separate `project-render-v2` entry point. It reads a direct
Score-v2 document, a formal roster, and an execution profile, renders PCM24
under an active runtime lease, and publishes Candidate v3. Its four required
arguments are `--score`, `--roster`, `--execution-profile`, and `--sample-rate`:

```console
天籁.cmd project-render-v2 ^
  --score "scores\work.score-v2.json" ^
  --roster "scores\work.roster.json" ^
  --execution-profile "scores\work.execution-profile.json" ^
  --sample-rate 48000
```

This entry point currently requires the source-workspace layout, one executor,
the built-in oscillator, an explicit declaration of zero external audio
assets, and `tail=0`. Migration bundles, performance facts, realization,
sampled backends, custom implementations, lazy assets, and multiple executors
fail closed; a migration bundle cannot masquerade as the direct `--score`
input. The existing `project-render`, Score-v1, and Candidate-v1/v2 behavior is
unchanged. Candidate v3 belongs only to this restricted direct-v2 formal path.
See the Score-v2 document above for its runtime-generation, PCM24, and candidate
closure boundaries.

The explicit migration command writes a complete bundle and never rewrites the
source score in place:

```console
tianlai migrate-score-v2 --score scores/work.score.json --output output/work.score-v2-migration.json
```

For performance detail beyond notation, an optional `realization v1` document
is bound to the exact score hash. It can carry per-note timing, gate, velocity,
release velocity, and sparse semantically named expression, breath, or pedal
lanes. Omitting it preserves the previous plan byte-for-byte. When present, the
creator separately chooses whether numeric quantization, semantic approximation,
and sample-grid timing adaptation are acceptable; backend capability never
silently grants that consent. `ensemble` and `project-render` accept it through
`--realization`, and candidates bind the source document to the resolved plan
evidence. See [Realization v1](docs/realization-v1.en.md).

MIDI and MusicXML can both contain semantics that the current score cannot
represent losslessly. Unified import defaults to `--loss-policy reject`. To
accept degradation, choose `warn` or `allow` explicitly and keep the generated
`import-report.json`. Exported MIDI is an exchange copy with a loss report, not
a lossless inverse of the score.

## AI and MCP

The MCP server currently exposes 50 tools. Existing diagnosis, import, roster,
score editing, preflight, location, comparison, and rendering remain compatible.
The path-isolated persistent authoring projects and optional creative workflow
introduced in v0.7 provide work charters, phased review, trusted hard failures,
exceptions, managed rendering, revision, rollback, and history audit. MCP also
offers a separate stateless external-constitution lookup that may be consulted
after the charter is formed. `1.0.0` adds
Claim Lifecycle v1—content-ID recomputation, score-referent verification,
per-claim decision dispositions, and frozen terminal open claims—passage-level
necessity derivations, charter settlement (acceptance must settle every charter
promise), whole-work fork declarations (a branch is always one complete piece;
the current record is a sparse alternative declaration, not epoch/LCA lineage),
and an acceptance gate that freezes only the point-in-time recheck of recorded
hard failures. That gate is neither current readiness nor aesthetic proof. Read
compatibility now has five policy tiers: base legacy, explicit Claim Lifecycle,
acceptance-gated, the tier with
`charter_settlement_profile=affirmative-promise-ledger-v1`, and the latest tier
with
`composition_governance_profile=whole-work-derivation-and-bounded-amendment-v1`.
The first four remain read-compatible. An ordinary older workflow continues to
upgrade within the pre-governance policy until a composition map is explicitly
recorded; it cannot downgrade. Workflows newly created through this MCP version
enable the latest tier by default; pass `composition_governance=false` only for an
explicit legacy-flow opt-out. Test a model's unassisted baseline without connecting
the MCP server. New records remain
optional and sparse: once the promise is fulfilled, identity is stable, and
material alternatives are closed, the workflow should stop rather than iterate
for iteration's sake. `budget_exhausted` is valid only when a positive frozen
budget, the fork cap, or the history ceiling is actually reached; other
termination reasons remain final-authority declarations, not machine proofs.

The work charter comes first and, together with the work's own material, remains
the root of the workflow. An external music constitution is only a stateless,
optional source of ideas that may be consulted afterward. Without it, the
composition map, question-complete reviews, derivations, evidence, acceptance,
and continuation remain fully available. Clauses bind none of generation,
review, acceptance, or continuation. Any existing binding in an older workflow
preserves only immutable provenance about what was referenced then; it is neither
admitted into current judgment nor allowed to block continued work. v0.1 is
retired, so its text is not looked up or mapped to v0.2.
Legacy clients should remove `constitution` / `active_clauses` from activation,
or pass `null` and `null`/an empty array respectively; consult the stateless
current-v0.2 getter separately after forming the charter when useful.

> Upgrade compatibility note: Opening a `0.9.x` authoring project read-only
> under `1.0.0` does not rewrite it; opening it or saving identical documents
> does not trigger migration. The first content-changing save adds
> `save_sequence` / `current_save_event_sha256` to `tianlai-project.json`,
> `first_save_sequence` / `parent_revision` to the new `revision.json`, and
> creates `.tianlai/save-events/`. This is a one-way causal-provenance upgrade:
> `1.0.0` reads older projects, but `0.9.x` cannot reopen the project after that
> save. Copy the complete project directory before the first changed save if
> downgrade access matters.

The recommended MCP chain maps to the CLI conceptually, while file transport, output
roots, and default instrument scopes differ:

```text
diagnose_runtime(check_level="quick")
        → import_score_project → confirm_roster → validate_project
        → check_project_readiness
        → render(**render_handoff)
        → locate_rendered_candidate → get_score_slice → patch_score
        → validate_project → check_project_readiness
        → render(parent_candidate_id=..., **render_handoff)
        → compare_rendered_candidates
```

The latest “Xiangyin” governance first builds a whole-work composition map for
the current piece, then uses a read-only whole-work mirror to generate questions
that must be answered rather than checked off. Key derivations bind charter
claims, map nodes, and those answers. If iteration evidence genuinely requires a
charter change, the workflow must preflight and acknowledge the exact
reconstruction cost before editing the score, then append one entry to a linear
amendment ledger. The amendment takes effect next iteration, which rebuilds the
map and repeats whole-work review. The map consumes no historical works,
preference examples, or winner rationales; formal candidates remain complete
pieces rather than fragment products. Machine checks establish facts and
bindings only—they neither replace human listening nor prove that the music
sounds good. The optional loop may still record a sparse fork once several
complete candidates genuinely exist, and acceptance supplies
`charter_settlement` covering every affirmative charter promise. With no real
branch or new information, it adds neither step. Position review keeps two paths
open: material that grows from existing material and relationships should trace
those consequences honestly; material with no such lineage may still be accepted
when it is globally necessary to the complete work. Never invent causality merely
to preserve a detail. If neither path holds, silence, muting, or deletion is also
a complete outcome.

`render`, `render_authoring_revision`, and `render_workflow_candidate` write
audio. Other write tools publish immutable state/document revisions only inside
the dedicated project root. File-based import follows the MCP input-root policy; attaching a
client does not grant arbitrary access to the whole computer. See the
[MCP interface](docs/MCP.en.md) for the complete 50-tool table, input-root
configuration, and candidate rules, and [Creative Workflow](docs/创作工作流.en.md)
for the honesty boundary, state machine, and disconnect recovery. Runtime and project-readiness checks
passively summarize contract, resource, platform, and output-location
assessments; formal `render` performs actual instantiation, audio processing,
and candidate writes. On macOS x86_64, a read-only in-process `sysctlbyname`
check verifies Rosetta status without starting an external program or writing a
file. Only verified native Intel makes readiness authorize a render attempt;
translation or an unverifiable identity keeps readiness blocked. Missing
resources can be passed to `plan_resource_restore` for a path-redacted
restoration plan.
`validate_project`, `check_project_readiness`, and `render` also return the same
graded `project_review`. Hard contract findings remain explicit gates in
`issues`; renderable range, onset, articulation, and orchestration candidates
carry stable IDs, scopes, evidence, and several review options for the creator
to consider while listening. The report is bound to the current score, roster,
and performance-plan hashes and never edits the score or audio automatically.
The `render_handoff` returned by preflight
contains both the complete profile and its canonical hash, allowing formal
rendering to reject an accidental profile substitution before candidate
creation. The same document defines the cache, remix, and immutable-candidate
boundaries.

## Current capabilities

- There are 103 registered sound entries. Each bound version has completed an
  isolated single-instrument, single-timbre listening check and is marked
  `quality_tier=formal`. All 103 are available in the default MCP `formal`
  scope, remain available for individual audition, and can be discovered by
  category, routing class, articulation, pitch mode, or name.
- Development has exercised orchestration, dynamics, space, and actual rendering
  across many ensemble test works. New combinations can continue through the
  `manual`, `analyze`, and `suggest` workflow with creator and community feedback.
- `manual`, `analyze`, and `suggest` separate analysis from revision. `suggest`
  produces a bounded, reviewable diagnostic draft that a creator can carry into
  the next candidate.
- Offline rendering produces 24-bit WAV audio, optional stems, a shared hall,
  and complete receipts. `1.0.0` automatically chooses serial execution or
  up to four managed workers from CPU, memory, scratch-space, work, and verified
  resource evidence, and passes long stems through bounded streaming blocks.
  It adds no render-profile option, falls back to the complete serial path when
  required worker-safety or resource evidence is insufficient, and preserves
  one formal audio-byte contract.
- Stem and content-addressed analysis caches accelerate later remixes, with
  closed telemetry and its hash retained by the candidate. Local adaptive
  scheduling learns only from successful tasks that were fully verified and
  committed, never from failures or cache hits.
- Every formal standalone or ensemble render streams its final PCM back from
  disk and produces a
  Hash-bound [`渲染后自检.json`](docs/渲染后自检.en.md). Damage, format
  mismatches, and exact silence when sound is explicitly expected are hard
  errors; True Peak/LUFS, DC, phase, channel, and tail risks remain creator
  review findings and never modify audio or impose one aesthetic.
- `strict_hq` applies each instrument's declared range-evidence contract to a
  reproducible high-quality candidate range. The default `compatibility` mode
  preserves extended registers and experimental timbres inside the hard
  playable contract and presents supporting evidence through `project_review`.
- Graded self-checks reserve blocking decisions for hard contracts such as
  structure, licensing, routing, resources, safety budgets, and actual
  playability. Creative-context findings remain non-blocking and provide
  stable, hash-bound review identities suitable for a future UI.
- True Peak, LUFS, peak, RMS, spectrum, phase, range, and difference reports
  provide objective coordinates for debugging and A/B review; the creator
  combines them with actual listening for the final choice.

See [Current status](docs/当前状态.en.md) for live status and technical details.

## Creative reference

[Tianlai Music Constitution v0.2](docs/音乐创作参考笔记/天籁音乐宪法-v0.2.en.md)
is an included, non-normative creative guide for human creators and AI agents.
It protects material that emerges before a verbal reason while asking major
structural choices to bear proportionate consequences. It is not law, a rules
engine, or mandatory project policy; declining it causes no project penalty or
feature restriction. Its text is CC BY 4.0, but music created with its guidance
does not thereby become CC BY, and Tianlai software remains Apache-2.0.

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
evidence must still fail. Portable tests verify machine contracts;
`external_assets` and `listening` add real-resource and frozen-listening
acceptance respectively.

Start with the [documentation map](docs/README.en.md), and see
[From score to second render](docs/从乐谱到第二次渲染.en.md) for the complete
score-iteration contract. Before contributing code or reproducible listening
feedback, read [Contributing](CONTRIBUTING.en.md). Security boundaries and
private-reporting guidance are in the [Security policy](SECURITY.en.md).
