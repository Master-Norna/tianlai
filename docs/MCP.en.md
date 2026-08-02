[简体中文](MCP.md) | **English**

# Tianlai MCP interface

Tianlai `0.6.0rc1` exposes editable, reproducible music projects to AI agents
through stdio MCP. An agent does not receive an opaque “one sentence to audio”
button. It receives fine-grained tools for reading contracts, choosing
instruments, importing a score, confirming instrumentation, running preflight,
rendering immutable candidates, locating by time, editing locally, rendering
again, and A/B comparison.

Tianlai validates contracts, executes instruments, writes stems and ensembles,
binds hashes, and provides objective diagnostics. Melody, structure,
orchestration intent, trade-offs, and publication decisions remain with the
creator. Neither machine metrics nor a language model replaces human listening.

## Installation and client configuration

Windows source-package users should first run:

```cmd
安装运行环境.cmd
检查运行环境.cmd
```

The installer includes the optional MCP dependencies. For a manually created
development environment, run:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[mcp]"
.\.venv\Scripts\python.exe -m tianlai.mcp_server
```

The second command starts the stdio server. Example client configuration:

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "C:\\path\\to\\tianlai\\.venv\\Scripts\\python.exe",
      "args": ["-m", "tianlai.mcp_server"],
      "cwd": "C:\\path\\to\\tianlai"
    }
  }
}
```

The repository also provides a copyable
[`.mcp.json.example`](../.mcp.json.example). Copy it to `.mcp.json` and replace
both placeholder paths with the source-package directory. A real `.mcp.json`
contains local absolute paths and is therefore excluded from Git and releases
by default.

For a client running in Linux or WSL, use the absolute Linux path to the
project's `.venv/bin/python`. See the
[Linux / WSL quick start](Linux快速开始.en.md) for complete setup, configuration,
and support scope. Never share a `.venv` between Windows and Linux.

On macOS, an MCP client likewise uses the real absolute path to the project's
`.venv/bin/python`. The interpreter architecture must match the native Apple
Silicon or Intel host. See the [macOS quick start](macOS快速开始.en.md) for setup
and configuration. Never reuse a virtual environment across operating systems
or CPU architectures.

`command` must point to the project-local `.venv`, and `cwd` must be Tianlai's
runtime root. The server does not put an audio stream into an MCP text response.
A successful render returns local paths to the candidate directory, WAV,
receipt, and reports.

## Local MCP input roots

File-based import tools do not gain arbitrary read access to the whole
computer. The default policy is:

- Relative paths resolve from the Tianlai runtime root, not whatever directory
  the client process happens to use.
- The Tianlai runtime root and its existing `乐谱/`, `examples/`, and output
  directories are allowed by default.
- An input must already exist and be an ordinary file.
- A path is normalized before containment checks; `..` or a symbolic link
  cannot escape an allowed root.
- On Windows, additional roots may be added to `TIANLAI_INPUT_ROOTS`, separated
  with semicolons. They extend rather than replace the default roots.

An additional root does not join the relative-path search order. Relative paths
always resolve from the Tianlai runtime root. Pass an absolute path for a file
inside an additional root.

Example:

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "C:\\path\\to\\tianlai\\.venv\\Scripts\\python.exe",
      "args": ["-m", "tianlai.mcp_server"],
      "cwd": "C:\\path\\to\\tianlai",
      "env": {
        "TIANLAI_INPUT_ROOTS": "D:\\Scores;E:\\SharedMusicXML"
      }
    }
  }
}
```

Add only score directories that are intentionally exposed to the agent.
Structured `score`, `roster`, patch, and query parameters are passed directly
as MCP objects and need no additional disk root.

Rendered-candidate location and comparison have a narrower boundary: they may
read only the current runtime's `output/mcp/` candidate tree and cannot use a
candidate parameter to inspect an arbitrary input root.

## The current 15 tools

The server registers exactly these 15 tools:

| Tool | Writes audio/project files | Purpose |
| --- | --- | --- |
| `score_and_roster_format` | No | Returns current score/roster contracts, rules, and a minimal example. |
| `list_instruments` | No | Returns palettes, articulations, ranges, pitch modes, quality, and license status. |
| `import_midi` | No | Compatibility entry point: reads local MIDI and returns a score and non-executable draft. |
| `import_musicxml` | No | Compatibility entry point: reads MusicXML/XML/MXL and returns a score and report. |
| `import_score_project` | No | Recommended entry point: unified import returning a hash-bound three-document project bundle. |
| `confirm_roster` | No | Promotes a draft to a formal roster using creator-supplied per-part assignments. |
| `upgrade_score` | No | Upgrades a legacy score to score v1 with stable `event_id` values. |
| `get_score_slice` | No | Reads a bounded fragment by part, event, or bar with its baseline hash. |
| `patch_score` | No | Atomically applies event patches bound to a hash and old values and returns a new score. |
| `compare_score_versions` | No | Compares two scores by stable event identity. |
| `validate_project` | No | Compiles and validates score, roster, and performance settings without instantiating instruments. |
| `locate` | No | Recompiles the current project and maps a time window to planned events. |
| `locate_rendered_candidate` | No | Locates an actually heard timestamp from a saved candidate's receipt and plan. |
| `compare_rendered_candidates` | No | Compares the score, roster, configuration, plan, and mix identity bound to two candidates. |
| `render` | **Yes** | Renders a new candidate directory, ensemble, optional stems, receipt, and attribution sidecars. |

“No” means the tool writes neither audio nor project files; import and candidate
inspection still read authorized local files. `patch_score` returns a new
in-memory object, and the client decides where to save it. Only `render` creates
formal candidate artifacts.

## Recommended agent loop

The CLI documentation uses these names:

```text
project-import → roster-promote → project-render → candidate-locate
    → score-slice → score-patch → project-render → candidate-compare → A/B
```

The MCP concepts map to:

```text
import_score_project
    ↓
confirm_roster
    ↓
render
    ↓
locate_rendered_candidate
    ↓
get_score_slice → patch_score
    ↓
render(parent_candidate_id=...)
    ↓
compare_rendered_candidates → creator listens to the A/B
```

The two paths are not the same permission or file wrapper. CLI import writes
three files and offers a loss policy; MCP import returns an in-memory bundle.
CLI candidates default to `output/候选/`; MCP candidates go to `output/mcp/`.
MCP `render` defaults to `trusted_only=true`, while the CLI primarily confirms
the palette during `roster-promote`. Do not apply one entry point's path or
permission defaults to the other.

### 1. Read current contracts and palette

At the start of every session, call:

```text
score_and_roster_format()
list_instruments(trusted_only=true, pitched_only=false)
```

Do not rely on an old prompt's memory of fields or instruments.
`trusted_only=true` is the default curated scope. `false` admits only
non-quarantined formal single-timbre entries; it does not bypass license
quarantine or expose local-compatibility SoundFonts.

The only versioned source of truth for the trusted palette is
[`可信乐器.json`](../可信乐器.json). Documentation and agent prompts should not
hard-code its count; read the current set with `list_instruments` in every
session.

Before writing notes, prefer
`articulation_range_contracts[articulation].midi_ranges`. The top-level range
is only an instrument envelope and may contain articulation gaps. `strict_hq`
is a fail-closed evidence gate; it does not automatically improve a sound source
or resampling quality.

### 2. Unified import

```text
import_score_project(
  source_path="乐谱/曲目/某曲/MusicXML/某曲.mxl",
  trusted_only=true,
  candidate_limit=8
)
```

The successful `bundle` contains:

- score v1;
- a persistable `import_report`;
- an `executable=false` `roster_draft`;
- SHA-256 values binding source file and score;
- a bounded number of non-executable routing suggestions for each part.

Suggestions, track names, Program Change, CC7, CC11, and track order never
become formal routing automatically. Legacy `import_midi` and
`import_musicxml` remain for compatibility. New projects should use unified
import so MIDI and MusicXML share one audit boundary.

MCP unified import returns an in-memory bundle only. It does not choose the
CLI's `loss-policy` or write files for the client. The client should inspect
warnings and the report, then either reject the result or persist the complete
three-document bundle. Keep and read the import report with the score and draft.
Semantics omitted from the score—such as repeats, grace notes, pedal, pitch
bend, lyrics, layout, or vendor controls—cannot reappear magically during a
later render.

### 3. Confirm the roster explicitly

```text
confirm_roster(
  score=...,
  roster_draft=...,
  assignments=[
    {"part": "Piano", "instrument": "键盘乐器/钢琴"},
    {"part": "Violin", "instrument": "管弦乐/弦乐组/小提琴"}
  ],
  trusted_only=true
)
```

Submit an `instrument` for an ordinary part and a per-key `kit` for percussion.
The tool first revalidates the draft's bound score hash, then requires every
score part exactly once and checks instrument existence, quarantine, and the
trusted policy. It never fills a missing assignment from routing hints.

The creator should also state roles, foreground/background, static gain,
automation, seats, groups, and relative balance. Words such as “solo” or “left
hand” in an instrument or track name confer no authority.

### 4. Preflight and first render

Before an expensive render, call:

```text
validate_project(
  score=...,
  roster=...,
  render_profile={
    "kind": "tianlai.render_profile",
    "schema_version": 1,
    "name": "preview-v1",
    "write_stems": true
  },
  trusted_only=true
)
```

It shares formal rendering's structural, time-coordinate, license, trusted,
routing, range, and performance-plan checks without opening WAV/SFZ files.
`resources.level="catalog_only"` and `ready_to_render=null` mean physical audio
resources were not checked. When `render_profile` is omitted, validation and
rendering resolve the same versioned default. But preflighting an omitted
profile does not prove that a different custom profile will pass later.

To prevent an agent from losing hall, stem, or cache parameters while copying,
`validate_project` returns values that can be passed directly to `render`:

```json
{
  "render_handoff": {
    "render_profile": {"kind": "tianlai.render_profile", "...": "..."},
    "expected_render_profile_sha256": "64 lowercase hexadecimal characters"
  }
}
```

Pass both fields to formal rendering. If the profile changes in between,
`render` returns `render_profile.preflight_mismatch` before creating a candidate
directory. This hash prevents accidental local workflow substitution; it does
not replace a candidate manifest or release signature.

`render_preflight` reports the current request's `passed` state, shared-hall
tail, stems, effective collaboration mode, cache switches, memory/main-output
estimates, and each budget gate. A shared hall substantially increases full-
length work arrays. Because stems are rendered and written one by one,
`write_stems` increases disk output estimates without multiplying peak memory
by track count. Relationship audio for `analyze/suggest` uses temporary memmaps,
and FFT runs in bounded-window batches. A failed preflight returns the same
report with its error; a red gate never authorizes rendering to continue.

Then create the candidate:

```text
render(
  score=...,
  roster=...,
  title="某曲",
  render_profile=validation.render_handoff.render_profile,
  expected_render_profile_sha256=(
    validation.render_handoff.expected_render_profile_sha256
  ),
  trusted_only=true
)
```

With no explicit override, the versioned default render profile is used. A
successful response includes `candidate_id`, `candidate_directory`, `mix_wav`,
plan, receipt, attribution sidecars, optional stems, range diagnostics, mix
report, and cache telemetry.

### 5. Locate the actual candidate

When a problem is heard at `34.2` seconds, prefer:

```text
locate_rendered_candidate(
  candidate_directory="作品ID/候选ID",
  at_seconds=34.2
)
```

It validates hashes for the candidate's score, roster, render profile,
performance plan, and receipt, then reports:

- events currently gated on;
- recently ended events that may still contribute sample release or hall tail;
- events about to enter;
- `event_id`, part, instrument, pitch, bar, and beat.

`locate` recompiles the current score/roster passed at call time and is suitable
for checking a plan that has not been rendered. When listening to a saved
candidate, do not use a subsequently modified project to guess what it
contained.

The tail list is a bounded set of candidates, not sample-by-sample causal proof.
Use stems and human listening as well.

### 6. Edit with conflict protection

Read a small fragment first:

```text
get_score_slice(
  score=...,
  query={
    "kind": "tianlai.score_slice_query",
    "schema_version": 1,
    "part_ids": ["Violin"],
    "bar_range": {"start": 12, "end": 14},
    "max_notes": 128
  }
)
```

Put the returned `score_sha256` into the patch:

```text
patch_score(
  score=...,
  patch={
    "kind": "tianlai.score_patch",
    "schema_version": 1,
    "base_score_sha256": "...",
    "operations": [
      {
        "op": "update_note",
        "event_id": "violin-0042",
        "expect": {"pitch": "B5"},
        "changes": {"pitch": "A5"}
      }
    ]
  }
)
```

A hash or `expect` mismatch rejects the entire batch rather than applying half.
Preserve `event_id` when modifying an existing note. The engine allocates IDs
for new notes deterministically. Save the returned score as a new revision; do
not overwrite `score.json` inside a candidate.

### 7. Second candidate and A/B

Run `validate_project` again with the new score, then:

```text
render(
  score=new_revision,
  roster=original_or_new_roster,
  title="某曲",
  parent_candidate_id="candidate-first-candidate-ID"
)
```

Finally call:

```text
compare_rendered_candidates(
  before_candidate_directory="作品ID/候选1",
  after_candidate_directory="作品ID/候选2"
)
```

Comparison separates score, roster, render profile, performance plan, and
receipt identity. It explains what changed, not which version sounds better.
The creator must still directly A/B both `合奏.wav` files.

## Candidate immutability

MCP rendering defaults to:

```text
output/mcp/<sanitized work ID>/<unique candidate ID>/
```

Each directory writes `候选.json` last, binding:

- `score.json`;
- `roster.json`;
- `render-profile.json`;
- the performance-plan hash;
- `渲染回执.json` and its hash.

Once used for location, comparison, or listening, a candidate is an immutable
snapshot. Do not edit these files in place or copy new audio into an old
candidate and call it the same generation. Normal iteration creates a new
`candidate_id` and sets `parent_candidate_id`.

`render(overwrite=false)` rejects a same-named directory by default. Controlled
replacement requires an explicit `output_id`, `overwrite=true`, and the
existing `expected_receipt_sha256` together. It is a repair tool, not normal
version management.

## Score v1, `staff`, and `voice`

Every note has a score-wide unique and stable `event_id`. These optional fields
identify notation structure:

```json
{
  "event_id": "piano-rh-0042",
  "bar": 12,
  "beat": 1,
  "duration_beats": 2,
  "pitch": "C5",
  "tie": true,
  "staff": 1,
  "voice": "1"
}
```

- `staff` is a positive integer staff identity.
- `voice` is a non-empty string scoped by `staff`.
- Neither is a roster part or affects instrument choice.
- A simple score may omit both.
- MusicXML import preserves them so ties at the same pitch but on different
  staves/voices are not merged after flattening.

When using `patch_score` on a MusicXML-derived score, preserve existing
`staff/voice` unless the internal voice structure is intentionally changing.
Assign the correct identity to a new polyphonic tie as well.

## Collaboration analysis and mix authority

A roster or render profile supports:

| Mode | Diagnostics | Non-executable suggestions | Changes audio | Writes project changes |
| --- | --- | --- | --- | --- |
| `manual` | No | No | No | No |
| `analyze` | Yes | No | No | No |
| `suggest` | Yes | Yes | No | No |

`suggest` generates bounded relative-gain drafts only for relationships
explicitly declared by the roster. They are always `executable=false`,
`audio_modified=false`, and `creator_review_required`. An agent first locates
the candidate fragment; the creator then decides whether to change gain,
register, dynamics, duration, instrumentation, or nothing at all. No warning
does not mean a work passed complete ensemble validation.

The current strict report contract is `mix_report.version=2`, with
`temporal_balance.version=2`. A client must stop automatic interpretation and
upgrade when it encounters an unknown version.

## Stem cache

The default cache stores local float32 raw stems before assignment gain. Cache
identity binds part performance content, sample rate, effective instrument
parameters, actual sound-source bytes, DSP source, and relevant dependencies.

A change only to gain, automation, pan, seats, hall, master, normalization,
diagnostic mode, or public stem writing can be remixed. A change to notes,
articulations, actual sound sources, instrument parameters, or DSP invalidates
the affected stem. In `analyze` / `suggest`, the same switch also enables a
separate content-addressed analysis cache. It binds post-gain float32 content,
relationship declarations, analysis parameters, and algorithm source. Seat,
pan, hall, or master changes can hit; a track's gain or audio change invalidates
that track's metrics and related relationships, while a declaration change
invalidates relationship metrics only.

- `use_stem_cache=false`: disable caching.
- `refresh_stem_cache=true`: force raw-stem recomputation. The analysis cache
  still revalidates the newly produced audio content and is reused only if the
  content is identical.
- `write_stems=false`: do not write public PCM24 stems; this does not disable
  internal caches.

A corrupt cache falls back safely to ordinary rendering. Caching mainly
accelerates the second mix and cannot eliminate instrument execution on the
first cold render. Every successful render also writes `缓存遥测.json`, whose
`total/accounted/unaccounted` values close over hits, misses, and bypasses. The
candidate manifest binds that file by SHA-256; altering telemetry alone makes
candidate loading fail. `use_stem_cache=false` disables both iterative cache
layers. To prove across machines that an entire candidate was not replaced,
also retain its candidate-manifest hash or use a signed release record.

## Interface and quality boundaries

- MCP provides offline rendering only, not a real-time software instrument.
- MIDI/MusicXML import does not promise to preserve every source-format
  semantic; read the report.
- `validate_project` does not prove that external samples are installed.
- `validate_project.render_preflight` proves only that the current render
  profile passes static resource budgets. Formal `render` repeats the same gate
  before taking ownership of candidate output.
- Every formal route and project modification requires explicit input; no name
  or statistic confers authority.
- Release/hall contributions from candidate location are candidate evidence,
  not sample-by-sample causal analysis.
- Machine diagnostics cannot judge melody, harmony, orchestration, or
  aesthetics.
- A first render of a long work may take minutes and substantial peak memory;
  caches optimize later remixes only.
- `quality_tier=formal` does not mean every range, dynamic, articulation, and
  instrument combination has been accepted.
- Human listening and checks of input rights, sound-source licenses,
  attribution, and output rights remain required before final publication.

The equivalent CLI loop is documented in
[From score to second render](从乐谱到第二次渲染.en.md). See
[Current status](当前状态.en.md) for the live implementation state.
