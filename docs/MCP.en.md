[简体中文](MCP.md) | **English**

# Tianlai MCP interface

Tianlai `0.8.0rc1` exposes editable, reproducible music projects to AI agents
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
.\.venv\Scripts\python.exe -m tianlai.mcp_entry
```

The second command starts the stdio server. Example client configuration:

```json
{
  "mcpServers": {
    "tianlai": {
      "command": "C:\\path\\to\\tianlai\\.venv\\Scripts\\python.exe",
      "args": ["-m", "tianlai.mcp_entry"],
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
      "args": ["-m", "tianlai.mcp_entry"],
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

Persistent authoring and workflow tools use an even narrower boundary. Clients
pass a lowercase ASCII `project_key`, which maps below the fixed
`output/mcp/authoring-projects/` namespace. They accept no project, candidate,
or output paths and return no local paths.

## The current 44 tools

The server registers exactly these 44 tools; the original 27 names and
parameters remain compatible:

| Tool | Writes audio/project files | Purpose |
| --- | --- | --- |
| `score_and_roster_format` | No | Returns current score/roster contracts, rules, and a minimal example. |
| `list_instruments` | No | Searches the formal palette by scope and returns routing classes, articulations, ranges, and pitch modes. |
| `diagnose_runtime` | No | Returns a bounded, path-redacted diagnosis of the runtime, platform, layout, resource summary, and optional capabilities. |
| `plan_resource_restore` | No | Plans licenses, size, and local state by instrument, resource family, or group without downloading or installing. |
| `import_midi` | No | Compatibility entry point: reads local MIDI and returns a score and non-executable draft. |
| `import_musicxml` | No | Compatibility entry point: reads MusicXML/XML/MXL and returns a score and report. |
| `import_score_project` | No | Recommended entry point: unified import returning a hash-bound three-document project bundle. |
| `confirm_roster` | No | Promotes a draft to a formal roster using creator-supplied per-part assignments. |
| `upgrade_score` | No | Upgrades a legacy score to score v1 with stable `event_id` values. |
| `get_score_slice` | No | Reads a bounded fragment by part, event, or bar with its baseline hash. |
| `patch_score` | No | Atomically applies event patches bound to a hash and old values and returns a new score. |
| `compare_score_versions` | No | Compares two scores by stable event identity. |
| `validate_project` | No | Compiles and validates score, roster, and performance settings without instantiating instruments. |
| `check_project_readiness` | No | Checks the project contract and resource references for instruments actually used by the roster without decoding or probing audio. |
| `locate` | No | Recompiles the current project and maps a time window to planned events. |
| `locate_rendered_candidate` | No | Locates an actually heard timestamp from a saved candidate's receipt and plan. |
| `compare_rendered_candidates` | No | Compares the score, roster, configuration, plan, and mix identity bound to two candidates. |
| `render` | **Yes** | Renders a new candidate directory, ensemble, optional stems, receipt, and attribution sidecars. |
| `create_authoring_project` | **Yes** | Creates an instrument-neutral persistent project in the dedicated namespace. |
| `open_authoring_project` | No | Opens current or historical immutable project metadata. |
| `get_authoring_snapshot` | No | Returns one three-document snapshot and bounded readiness without paths. |
| `save_authoring_project` | **Yes** | CAS-saves complete documents as a new immutable revision. |
| `check_authoring_readiness` | No | Checks hard contracts while leaving advisory review nonblocking. |
| `render_authoring_revision` | **Yes** | Renders exactly the named immutable revision; the raw result is unmanaged. |
| `inspect_authoring_candidate` | No | Verifies candidate/project bindings and separately reports authorization, recording, and acceptance. |
| `locate_authoring_candidate` | No | Maps rendered seconds to events using project and candidate IDs only. |
| `compare_authoring_candidates` | No | Compares two verified candidates inside one project. |
| `creative_workflow_guide` | No | Returns modes, honesty boundary, charter template, phases, evidence, decisions, and constitution metadata. |
| `get_music_constitution_clauses` | No | Verifies the full local constitution hash and returns at most 12 explicit Chinese or English clauses. |
| `create_creative_workflow` | **Yes** | Creates an off/audit/iterate workflow with stdio final authority frozen to agent. |
| `open_creative_workflow` | No | Opens a verified current or historical workflow revision without implicit full-history traversal. |
| `verify_creative_workflow_history` | No | Explicitly verifies the bounded immutable parent chain back to genesis. |
| `activate_creative_workflow` | **Yes** | Freezes a work charter and optional small official/custom clause set. |
| `record_workflow_review` | **Yes** | Records an agent phase review; MCP cannot claim human or trusted-validator identity. |
| `record_workflow_evidence` | **Yes** | Records nonblocking promise conflicts or aesthetic risks without automatic edits. |
| `record_verified_workflow_hard_failure` | **Yes** | Reruns trusted readiness and records only an exactly reproduced blocking issue. |
| `register_workflow_exception` | **Yes** | Registers an evidenced exception with cost/recovery; hard failures are never exceptable. |
| `render_workflow_candidate` | **Yes** | Performs reserve → managed render → candidate verification → workflow record with no caller auth/path. |
| `attach_workflow_candidate_for_audit` | **Yes** | Attaches an existing candidate by ID for audit; it remains unmanaged. |
| `decide_workflow_iteration` | **Yes** | Accepts/revises/recommends/preserves/stops under frozen agent authority. |
| `record_workflow_authoring_revision` | **Yes** | Binds a separately CAS-saved authoring revision as the next iteration. |
| `rollback_creative_workflow` | **Yes** | Selects an earlier immutable anchor without overwriting or deleting. |
| `cancel_workflow_render` | **Yes** | Cancels the sole current reservation without deleting candidates. |
| `stop_creative_workflow` | **Yes** | Stops under frozen agent authority without fabricating creator approval. |

“No” means the tool writes neither audio nor project files; import and candidate
inspection still read authorized local files. `diagnose_runtime` and
`check_project_readiness` are strictly passive: they do not load external
native libraries, start `tar`, `bsdtar`, or any external program, or create
temporary files. On macOS x86_64, the only additional operation is a read-only
in-process `sysctlbyname` identity query; it starts no process, writes nothing,
and uses no network. Writability of the actual MCP target `output/mcp` is only a no-write
estimate based on the target or parent directory's metadata and permissions, not an actual write
verification. `plan_resource_restore` performs no networking, downloading,
extraction, or installation. `patch_score` returns a new in-memory object, and
the client decides where to save it. `render`, `render_authoring_revision`, and
`render_workflow_candidate` write audio. Authoring and workflow writes remain
inside the dedicated project root and publish immutable CAS revisions.

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
validate_project → check_project_readiness
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

The persistent v0.7 authoring loop is path-free:

```text
create_authoring_project → get_authoring_snapshot
  → edit complete documents → save_authoring_project(expected_revision=...)
  → check_authoring_readiness → render_authoring_revision
  → inspect/locate/compare_authoring_candidates
```

The optional governance loop sits above it:

```text
creative_workflow_guide → optional get_music_constitution_clauses
  → create_creative_workflow → activate_creative_workflow
  → review intent/symbolic_structure/orchestration_performance
  → evidence → render_workflow_candidate → review render_report
  → optional exception → decide_workflow_iteration
  → if revise: CAS-save authoring documents
       → record_workflow_authoring_revision → next iteration
```

`render_workflow_candidate` derives its authorization from the sole current
immutable reservation, rechecks it before expensive work, binds it into both
receipt and manifest, and then records the verified candidate. It accepts no
caller authorization or path. Candidate inspection distinguishes
`workflow_authorized`, `workflow_recorded`, and `workflow_accepted`; a
syntactically valid self-assertion cannot make a candidate managed.

Generic evidence cannot create hard failures and generic review/evidence
provenance is fixed to `agent`. Only the dedicated trusted-readiness tool can
record an exact blocking issue. Promise conflicts and aesthetic risks do not
automatically gate or edit. The stdio boundary has no trusted human identity
channel, so its workflows freeze `final_authority=agent` and expose no authority
switch. See [Creative Workflow](创作工作流.en.md) for the full state and recovery
contract.

Byte-exact, fixed-hash Chinese and English constitution sources ship as wheel
package data and are tested against the copies in the music-creation notes. Clause
lookup therefore does not depend on a repository `docs/` directory being present at
runtime.

The two paths are not the same permission or file wrapper. CLI import writes
three files and offers a loss policy; MCP import returns an in-memory bundle.
CLI candidates default to `output/候选/`; MCP candidates go to `output/mcp/`.
MCP orchestration, preflight, location, and render tools default to
`instrument_scope="formal"`, while the CLI primarily confirms the palette
during `roster-promote`. Each entry point retains its own path, permission, and
roster contracts.

A new session can begin with `diagnose_runtime(check_level="quick")`. It is a
quick runtime diagnosis, not an audio probe. The complete project-resource
check is described after `validate_project` below.

### 1. Read current contracts and palette

At the start of every session, call:

```text
score_and_roster_format()
list_instruments()
```

With no arguments, `list_instruments` uses `instrument_scope="formal"`,
`detail_level="summary"`, and `limit=32`, returning the first page of the
current 103 formally callable sound entries. `catalog_count=103`, `has_more`,
and `next_offset` describe the complete catalog and following page. Pass
`instrument_scope="curated"` explicitly for the 25-entry creator-curated
palette. Each formal item also carries a `curated` marker for membership in
that subset. Existing clients may continue to use the `trusted_only`
compatibility parameter: `true` maps to `curated`, and `false` maps to `formal`.
New calls should use `instrument_scope` directly.

Top-level `curation_state` reports whether curated markers were loaded. A
normal release tree returns `available`, with `curated_count=25`. The formal
catalog remains independently searchable; when an environment returns
`curation_state="unavailable"`, an item's `curated` value is `null` to represent
the currently unloaded marker.

[`可信乐器.json`](../可信乐器.json) is the versioned source for the curated
subset. Clients and agent prompts should read `curation_state`, `curated_count`,
and each item's `curated` value instead of hard-coding the count; 25 describes
the current release tree.

The formal scope currently has three `routing_class` groups:

| `routing_class` | Current count | Orchestration use |
| --- | ---: | --- |
| `instrument` | 68 | Conventional melodic, harmonic, bass, and texture parts. |
| `percussion` | 27 | Modern drums and orchestral percussion, including pitched percussion such as timpani and glockenspiel. |
| `effect` | 8 | Environmental, Foley, and designed effect events. |

Successful results also include top-level `routing_class_semantics`, providing
machine-readable definitions for choosing an ordinary assignment, a per-key
`kit`, or an ambience/effect part.

The full palette can be filtered and paged on the server:

| Parameter | Purpose |
| --- | --- |
| `query` | Case-insensitive text search across instrument paths, display names, implementation types, and articulation names. |
| `category` | Filters by the first path component, for example `管弦乐`. |
| `routing_class` | Selects `instrument`, `percussion`, or `effect`. |
| `articulation` | Returns entries that support the requested articulation. |
| `pitch_mode` | Selects `pitched`, `ignore`, `fixed`, or `unspecified`. |
| `detail_level` | `summary` is the compact default for discovery and paging; `full` returns complete range, articulation, and capability contracts. |
| `offset` / `limit` | Zero-based paging; `limit` is 1–256 and defaults to 32. |

For example, search for an orchestral melodic instrument with a sustain
articulation:

```text
list_instruments(
  instrument_scope="formal",
  category="管弦乐",
  routing_class="instrument",
  articulation="sustain",
  pitch_mode="pitched",
  query="长笛",
  detail_level="summary",
  offset=0,
  limit=16
)
```

In the result, `catalog_count` is the size of the selected scope,
`matched_count` is the number of filtered matches, and `count` is the current
page size. When `has_more=true`, continue from `next_offset`. After choosing an
entry, lock onto its path with `query` and request the full contract:

```text
list_instruments(
  instrument_scope="formal",
  query="管弦乐/木管组/长笛",
  detail_level="full",
  offset=0,
  limit=1
)
```

In the full entry, write notes from
`articulation_range_contracts[articulation].midi_ranges`. This field has already
resolved articulation inheritance; the top-level `range` remains the overall
instrument-range view.

`pitch_mode="pitched"` selects or transposes by score pitch. `ignore` selects a
sample or variant through a legal native key; an assignment or kit-entry
`transpose` can align a different score key with it. `fixed` routes a score note
directly to the entry's declared `fixed_midi_note`.

### 2. Unified import

```text
import_score_project(
  source_path="乐谱/曲目/某曲/MusicXML/某曲.mxl",
  instrument_scope="formal",
  candidate_limit=8
)
```

The successful `bundle` contains:

- score v1;
- a persistable `import_report`;
- an `executable=false` `roster_draft`;
- SHA-256 values binding source file and score;
- a bounded number of non-executable routing suggestions for each part.

Suggestions, track names, Program Change, CC7, CC11, and track order are
preserved as import information in the report and draft. `confirm_roster`
writes the formal routing. Legacy `import_midi` and `import_musicxml` remain for
compatibility; new projects should use unified import so MIDI and MusicXML share
one project-import contract.

MCP unified import returns an in-memory bundle for client-selected persistence;
the CLI entry point separately provides `loss-policy`. After reviewing warnings
and the report, the client can persist the complete three-document bundle. Keep
the import report with the score and draft: it records how source semantics such
as repeats, grace notes, pedal, pitch bend, lyrics, layout, and vendor controls
are represented by the current score contract.

### 3. Confirm the roster explicitly

```text
confirm_roster(
  score=...,
  roster_draft=...,
  assignments=[
    {"part": "Piano", "instrument": "键盘乐器/钢琴"},
    {
      "part": "Violin",
      "instrument": "管弦乐/弦乐组/小提琴",
      "articulation_map": {"arco": "sustain"}
    }
  ],
  instrument_scope="formal"
)
```

Submit an `instrument` for an ordinary part and a per-key `kit` for percussion.
The tool first revalidates the draft's bound score hash, then requires every
score part exactly once and checks instrument existence, license status, and
the selected scope. An `articulation_map` key is a score articulation marking,
and its value is an articulation declared by the target instrument in
`list_instruments`. The example maps the score's `arco` marking to the violin's
`sustain` articulation.

A percussion part can expand into several executors within one assignment. The
following mapping is aligned with the current formal capability contracts for
kick, rimshot snare, and closed hi-hat:

```json
{
  "part": "Drums",
  "kit": {
    "C2": "现代鼓组/底鼓",
    "D2": "现代鼓组/边击军鼓",
    "A1": {"instrument": "现代鼓组/闭合踩镲", "transpose": 9}
  }
}
```

Notes at `C2`, `D2`, and `A1` in the score select the corresponding kit pieces.
The kick and snare use `fixed` routing to their respective `fixed_midi_note`
values; the closed hi-hat aligns `A1 + 9` with its legal native selection key
`F#2`. Each kit value can be either an instrument path or an
`{instrument, transpose}` object.

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
  instrument_scope="formal"
)
```

It shares formal rendering's structural, time-coordinate, license, trusted,
routing, range, and performance-plan checks without opening WAV/SFZ files.
`resources.level="catalog_only"` and `ready_to_render=null` mean physical audio
resources were not checked. When `render_profile` is omitted, validation and
rendering resolve the same versioned default. But preflighting an omitted
profile does not prove that a different custom profile will pass later.

#### Graded project review

`validate_project`, `check_project_readiness`, and `render` all return
`project_review`. It keeps two results independent: whether the request satisfies
hard execution contracts, and whether a renderable creative choice deserves a
focused listening review.

- Hard contracts for structure, time coordinates, licensing, instrument scope,
  explicit routing, actual playability, and resource budgets continue to appear
  in `issues` and determine `ok`, `status`, and renderability.
- Range profiles, onset compensation, automatic-articulation coverage,
  collaboration coverage, and same-source unison candidates enter the read-only
  `project_review`. They carry evidence and review options without changing the
  hard gate.
- `compatibility` remains the default range mode. It preserves extended
  registers, edge timbres, and experimental writing inside the declared hard
  playable range while reporting useful evidence. `strict_hq` remains an
  explicit creator-selected gate for strict high-quality profiles.

A typical response has this shape:

```json
{
  "project_review": {
    "$schema": "https://tianlai.local/schemas/project-review.schema.json",
    "kind": "tianlai.project_review",
    "schema_version": 1,
    "status": "review_recommended",
    "review_recommended": true,
    "continuation_allowed": true,
    "blocking_count": 0,
    "review_count": 2,
    "advisory_count": 1,
    "binding": {
      "score_sha256": "...",
      "roster_sha256": "...",
      "performance_plan_sha256": "..."
    },
    "items": [
      {
        "id": "selfcheck-0123456789abcdef0123",
        "level": "warning",
        "decision": "review",
        "blocking": false,
        "code": "range.outside_current_hq_candidate",
        "scope": {"executor_id": "violin", "part_id": "Violin"},
        "evidence": {"affected_note_count": 3},
        "suggestions": ["Keep the current writing and audition the timbre first."],
        "automatic_change": false
      }
    ]
  }
}
```

Each item ID is derived from its stable code, scope, and evidence. `binding`
ties the report to the current score, roster, and performance-plan hashes.
`level=warning` prioritizes a listening review, while `level=info` supplies
coverage or context; both have `blocking=false` and need no `force` or `ignore`
parameter. Clients can use `scope` to locate a part, executor, event, or
instrument and present `evidence` with several `suggestions` to the creator.
Every item fixes `automatic_change=false`, so the review itself leaves the
score, roster, performance plan, and audio unchanged.
The complete machine contract is `schemas/project-review.schema.json`, allowing
a future UI to validate and present the report without guessing field meaning.

All three entry points use the same semantics. Preflight exposes the review
before rendering, readiness adds physical-reference state beside it, and a
successful render returns the review bound to the actual candidate inputs. The
hard-contract channel remains `issues`, so clients can adopt `project_review`
incrementally. CLI `project-render` JSON uses the same
`project_review` key. `ensemble`, including `--plan-only`, also writes
`创作自检.json` in the output directory so a non-MCP workflow can review the
same graded result.

#### Runtime and project-resource self-check

Use this bounded sequence to add actual resource readiness:

```text
diagnose_runtime(check_level="quick")
validate_project(score=..., roster=..., render_profile=...)
check_project_readiness(
  score=...,
  roster=...,
  render_profile=...,
  instrument_scope="formal",
  verify_references=true
)
```

`diagnose_runtime` with `quick` checks explicit manifest references;
`references` additionally expands sample references in dedicated SFZ files and
is therefore slower. Both levels are strictly passive: they do not load external
native libraries, start `tar`, `bsdtar`, or any external program, create temporary
files, access the network, download or install anything, decode audio, or
perform a playback probe. The read-only in-process `sysctlbyname` identity query
on macOS x86_64 is a passive platform check, not an active capability probe. The actual MCP target `output/mcp` has a `writable_estimate` that is only a
passive estimate based on filesystem metadata and permissions;
`probe_performed=false` means that no actual write probe ran.

`check_project_readiness` repeats the same project preflight and checks the
instruments actually referenced by the current roster.
`ready_for_render_attempt=true` summarizes that contract preflight, resource
references, platform assessment, and output-location evaluation are ready.
`render` then performs instrument construction, audio processing, and candidate
writing.

On macOS x86_64, MCP diagnosis verifies the current process's Rosetta status.
Only verified native Intel makes the platform and render environment ready;
confirmed translation or unavailable identity information fails closed, so
readiness does not authorize the client to continue to `render`. The protocol
itself cannot force a client that bypasses readiness to honor that governance
decision.

When resources are missing, pass `restore_plan_handoff.instrument_ids`
unchanged to:

```text
plan_resource_restore(
  instrument_ids=readiness.restore_plan_handoff.instrument_ids
)
```

The restore plan contains deduplicated resource families, local state,
estimated download/install sizes, and license obligations. It performs no
network access, download, extraction, installation, or persistent write. The
user still reviews licenses and size and explicitly performs restoration
locally, then runs `check_project_readiness` again.

These three diagnosis/planning tools return only safe relative instrument or
resource-family identities, statuses, counts, and stable issue codes. Their
outputs redact usernames, local absolute paths, environment values, download
URLs, and native-loader error details. If operator-level paths are needed, the
user runs `tianlai-doctor` locally rather than placing that report directly in
the agent context.

To prevent an agent from losing hall, stem, or cache parameters while copying,
`validate_project` returns values that can be passed directly to `render`:

```json
{
  "render_handoff": {
    "render_profile": {"kind": "tianlai.render_profile", "...": "..."},
    "expected_render_profile_sha256": "64 lowercase hexadecimal characters",
    "instrument_scope": "formal"
  }
}
```

Pass all three fields to formal rendering. If the profile changes in between,
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
  instrument_scope=validation.render_handoff.instrument_scope
)
```

With no explicit override, the versioned default render profile is used. A
successful response includes `candidate_id`, `candidate_directory`, `mix_wav`,
plan, receipt, attribution sidecars, optional stems, range diagnostics, mix
report, `project_review`, and cache telemetry.

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
- `渲染后自检.json`, indirectly bound by a v3 receipt;
- `渲染回执.json` and its hash.

Once used for location, comparison, or listening, a candidate is an immutable
snapshot. Do not edit these files in place or copy new audio into an old
candidate and call it the same generation. Normal iteration creates a new
`candidate_id` and sets `parent_candidate_id`.

A successful `render` result also returns the post-render self-check path, full
report, and bounded `summary`. Hard contracts have already been verified before
candidate publication. A `warning` is risk evidence to review with stems and
actual listening, not authority for an agent to change gain, filter audio,
repair phase, or trim a tail automatically. See
[Post-render self-check](渲染后自检.en.md) for the measurements and decision
boundary.

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

`suggest` generates bounded relative-gain drafts for relationships
explicitly declared by the roster. They are always `executable=false`,
`audio_modified=false`, and `creator_review_required`. An agent first locates
the candidate fragment; the creator then decides whether to adjust gain,
register, dynamics, duration, or instrumentation. The report retains analysis
windows, relationships, and creator-review state so machine metrics can be
matched to actual listening.

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
accelerates the second mix; the first cold render executes the complete
instrument chain. Every successful render also writes `缓存遥测.json`, whose
`total/accounted/unaccounted` values close over hits, misses, and bypasses. The
candidate manifest binds that file by SHA-256; altering telemetry alone makes
candidate loading fail. `use_stem_cache=false` disables both iterative cache
layers. To prove across machines that an entire candidate was not replaced,
also retain its candidate-manifest hash or use a signed release record.

## Usage and publication guidance

- MCP renders reproducible offline candidates, stems, receipts, and diagnostic
  reports.
- MIDI/MusicXML imports include a report so the creator can confirm the musical
  semantics represented in the score.
- Run `validate_project`, `check_project_readiness`, and `render` in sequence so
  contracts, resources, and the formal candidate share one scope and render
  profile.
- At each step, read the hash-bound `project_review`: resolve hard contract gates
  first, then use stable IDs, scopes, and evidence to audition non-blocking
  review items.
- Instrument routing, articulation mapping, and project edits use explicit
  inputs, making them reviewable and repeatable.
- Candidate location maps actually heard time back to planned events for local
  editing and A/B iterations.
- Diagnostic metrics provide objective context for melody, harmony,
  orchestration, and mix decisions; the creator makes the final listening
  judgment.
- After the first long-form render, the stem cache can accelerate subsequent
  remixes.
- Before publication, combine human listening with checks of input rights,
  sound-source licenses, attribution, and output rights.

The equivalent CLI loop is documented in
[From score to second render](从乐谱到第二次渲染.en.md). See
[Current status](当前状态.en.md) for the live implementation state.
