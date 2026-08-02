[简体中文](README.md) | **English**

# Score directory

`乐谱/` stores a user's own notation sources, Tianlai score revisions, patches,
and formal instrumentation. It is a creative workspace, not a public examples
directory, and is excluded from Git and the lightweight source release by
default.

Tianlai treats score v1 as the authoritative editable intermediate form. MIDI
and MusicXML are source or interchange formats; a candidate WAV is one execution
snapshot. Neither should replace the score revision history in the work's
directory.

Run the `cmd` blocks below from Command Prompt (`cmd.exe`) in the project root.
The multiline continuation character is `^`.

## One directory per work

Recommended structure:

```text
乐谱/
└─ 曲目/
   └─ 某曲/
      ├─ MIDI/
      │  └─ 某曲.mid
      ├─ MusicXML/
      │  └─ 某曲.mxl
      ├─ 导入-01/
      │  ├─ 某曲.score.json
      │  ├─ 某曲.import-report.json
      │  └─ 某曲.roster-draft.json
      ├─ 某曲.roster.json
      ├─ 某曲.rev02.score.json
      ├─ 某曲.rev02.patch-result.json
      ├─ patches/
      │  └─ patch-01.json
      ├─ 某曲.render-profile.json       # optional
      ├─ 某曲_作曲生成器.py              # optional
      └─ 来源说明.md                     # record work/score rights when needed
```

Do not create an empty directory for a source format that is not used.
`导入-01/` is one non-overwriting import generation. When changing the loss
policy or source file, use `导入-02/`; do not mix a new result into the old
three-document package.

Candidates do not belong here:

- CLI `project-render` writes to `output/候选/<work ID>/<candidate ID>/` by
  default.
- MCP `render` writes to `output/mcp/<work ID>/<candidate ID>/`.
- A finished result approved for publication may be copied or re-exported to
  `output/作品/`. Do not move or rewrite the original candidate evidence.

A candidate directory is bound by its receipt and `候选.json` and must be used
as an immutable snapshot.

## Recommended workflow

### 1. Unified import

```cmd
天籁.cmd project-import ^
  --input "乐谱\曲目\某曲\MusicXML\某曲.mxl" ^
  --output "乐谱\曲目\某曲\导入-01"
```

For MIDI, change `--input` to a `.mid` or `.midi` file. The default loss policy
is `reject`: import publication fails when source semantics cannot be
represented. If the creator accepts degradation, use a new output directory,
pass `--loss-policy warn` explicitly, and retain and read the complete
`import-report.json`.

Each successful import produces:

- score v1;
- an import report recording source format, degradation, and hashes;
- a roster draft with `executable=false`;
- a bounded number of routing suggestions.

These three files are bound to each other. Do not replace only one, and do not
treat a roster draft as formal instrumentation.

### 2. Explicit instrumentation

```cmd
天籁.cmd roster-promote ^
  --score "乐谱\曲目\某曲\导入-01\某曲.score.json" ^
  --draft "乐谱\曲目\某曲\导入-01\某曲.roster-draft.json" ^
  --assign "Piano=键盘乐器/钢琴" ^
  --assign "Violin=管弦乐/弦乐组/小提琴" ^
  --output "乐谱\曲目\某曲\某曲.roster.json"
```

Choose an `instrument` explicitly for an ordinary part; submit per-key `kit`
routing for percussion through assignments JSON. The left side of `--assign`
must be `score.parts[].id` / draft `assignment.part`, not the display name
`parts[].name`.

Track names, Program Change, CC7/CC11, track order, and routing hints do not
gain execution authority automatically. The creator should also decide part
roles, gains, automation, seats, groups, and relative balance explicitly.

### 3. First candidate

```cmd
天籁.cmd project-render ^
  --score "乐谱\曲目\某曲\导入-01\某曲.score.json" ^
  --roster "乐谱\曲目\某曲\某曲.roster.json" ^
  --title "某曲"
```

Record the returned `candidate_id`. Do not edit the candidate's `score.json`,
`roster.json`, WAV files, or receipt; they describe an execution that already
happened.

### 4. Map time back to the score

```cmd
天籁.cmd candidate-locate ^
  --candidate "output\候选\作品ID\候选ID" ^
  --at 34.2 ^
  --output "output\诊断\某曲-34.2秒.json"
```

The result reports events active at that time, recently ended, and about to
enter, together with their `event_id`, part, instrument, bar, and beat. Recent
events are only candidates for release or hall tails and still need stem and
listening evidence.

Use `score-slice` to read a bounded fragment and its `score_sha256`, then write
a new revision with `score-patch`. A patch binds the baseline hash, event IDs,
and optional old values; any conflict rejects the entire patch.

```cmd
天籁.cmd score-patch ^
  --score "乐谱\曲目\某曲\导入-01\某曲.score.json" ^
  --patch "乐谱\曲目\某曲\patches\patch-01.json" ^
  --output "乐谱\曲目\某曲\某曲.rev02.score.json" ^
  --result-output "乐谱\曲目\某曲\某曲.rev02.patch-result.json"
```

### 5. Second candidate and A/B

```cmd
天籁.cmd project-render ^
  --score "乐谱\曲目\某曲\某曲.rev02.score.json" ^
  --roster "乐谱\曲目\某曲\某曲.roster.json" ^
  --title "某曲" ^
  --parent-candidate "candidate-第一版ID"
```

```cmd
天籁.cmd candidate-compare ^
  --before "output\候选\作品ID\候选1" ^
  --after "output\候选\作品ID\候选2" ^
  --output "output\诊断\某曲-候选1-候选2.json"
```

Machine comparison explains what changed in score, roster, profile, plan, and
mix identity. You should still directly A/B the two `合奏.wav` files.

## Score v1 editing rules

Every note must have a score-wide unique and stable `event_id`:

- Preserve the original ID when moving a note or changing pitch, velocity,
  articulation, or duration.
- Allocate a new ID only for a newly added note.
- A deleted and re-added note is a new event; do not reuse the old ID to mimic
  continuous identity.
- Stable IDs keep candidate location, patch conflicts, and version comparison
  reproducible across many iterations.

Notes also support optional `staff` and `voice` fields:

```json
{
  "event_id": "piano-0042",
  "bar": 8,
  "beat": 1,
  "duration_beats": 2,
  "pitch": "C5",
  "tie": true,
  "staff": 1,
  "voice": "1"
}
```

Simple programmatic scores may omit both fields. MusicXML import preserves
them because a MusicXML voice is scoped by staff. After flattening into one
Tianlai part, ties still need to distinguish notation voices and staves.
`staff/voice` is not a roster part and does not choose an instrument. Preserve
it while editing a MusicXML-derived score unless the internal notation
structure is intentionally changing.

## Generators and the single source of truth

A programmatic work must identify its creative source of truth:

- If the generator is authoritative, edit `<title>_作曲生成器.py` and regenerate;
  do not accumulate manual edits in JSON that the generator will overwrite.
- If the score is authoritative, maintain score revisions directly and stop
  running an old generator that overwrites them unconditionally.
- A roster describes routing, roles, gains, automation, seats, and execution
  parameters; do not mix those concerns into the score.
- Store the render profile separately so that a score edit can be distinguished
  from an execution-only or mix-only change.

Changing only gain, pan, seats, hall, master, or normalization usually allows
the original stem cache to be remixed. Changing notes, articulations, sound
sources, effective instrument parameters, or DSP re-executes affected stems.

## Import and export boundaries

MusicXML can currently preserve notes, chords, time signatures, tempo,
dynamics, common articulations, ties, concert pitch after transposition, mapped
percussion, and the `staff/voice` identity needed for polyphonic ties. Repeats,
grace notes, lyrics, layout, and other semantics that cannot be represented
losslessly are recorded in the import report.

MIDI preserves per-note velocity, tempo changes, and auditable track evidence,
but does not automatically map Program Change to a dedicated instrument or
guess dB values from CC7/CC11. Pedal, pitch bend, aftertouch, and device
messages are not guaranteed to enter score losslessly.

Use `export-midi` to produce an editable copy with a loss report when returning
to notation software. MIDI does not preserve stable event identity, every
articulation, microtones, phrases, or dedicated-instrument semantics and is not
a lossless inverse of score.

## Version control and rights

This directory is not committed by default. In addition to carrying a work,
MIDI and MusicXML may contain protected arrangements, editions, data entry, or
exports. “Available online” does not mean that a file may be copied, modified,
rendered, or redistributed.

For public demonstrations, prefer original works, clearly open-licensed
material, or material for which both the composition and the specific score
encoding are in the public domain. A composition entering the public domain
does not automatically release a modern edition, arrangement, or downloaded
MIDI/MusicXML file.

Tianlai does not acquire copyright in an input file or output music by importing
or rendering it, and it does not remove third-party sound-source terms. Before
publication, confirm that the work, the specific score version, and any data
entry or arrangement permit the intended use. See
[`OUTPUT_RIGHTS.en.md`](../OUTPUT_RIGHTS.en.md) for the complete position.

For complete commands, patch examples, and failure diagnosis, see
[From score to second render](../docs/从乐谱到第二次渲染.en.md).
