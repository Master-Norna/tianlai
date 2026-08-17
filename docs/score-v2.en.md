[简体中文](score-v2.md) | **English**

# Score v2: exact, portable work semantics

Score v2 is not an attempt to invent a more verbose MIDI, nor does it copy the
MusicXML layout tree unchanged into the executor. It addresses a more
fundamental problem: separating the musical facts that a creator wants to
preserve from what one performance, one sound source, or one interchange format
can express.

The internal collaboration contract has four layers:

1. **score**: what the work is—notes, exact score time, written and sounding
   pitch, tuning, relations, and form;
2. **realization**: how the creator asks it to be performed, and whether numeric
   quantization or semantic approximation is acceptable;
3. **roster / capability**: what the current executor can actually do, at what
   resolution, and which operations are only approximations;
4. **Performance Plan**: the time, values, adaptations, and evidence ultimately
   selected for this run.

“The executor can do it” is not the same as “the creator is willing to let it
do that.” Authorization for exact/adapt and exact/approximate therefore belongs
to realization. Neither capability nor an importer may guess it for the
creator.

## Exact time

Score v2 represents logical time with normalized rational numbers:

```json
{"numerator": 1, "denominator": 7}
```

A position uses a stable `measure_id` and `offset_quarters`; a duration uses
`duration_quarters`. Numerators and denominators are bounded before their
greatest common divisor is computed, and denominators must be positive. After
parsing, sign placement and reduced form are unique. Septuplets, nested ratios,
and very short displacements are not merged by six-decimal rounding or legacy
floating-point tolerances before reaching the conductor layer.

The first meter and tempo events must both cover offset zero of the first
measure. Later meter events may occur only at barlines, while tempo may still
change within a measure. The end of an intermediate measure can be written only
as offset zero of the following measure, so two coordinates cannot represent
the same instant. The final endpoint of the whole timeline may still be used as
the end of a phrase. A logical note may cross measures but may not extend beyond
the timeline. `actual_duration_quarters` is separate from the time signature,
so a pickup measure does not require a fabricated meter.

Rational time describes work time only. Conversion to seconds and integer
sample positions happens at the execution boundary and records requested,
resolved, and fidelity values together with the creator's time policy.

## Pitch and tuning

Each note stores all of the following:

- `written_pitch`: step, exact alter, octave, and optional accidental spelling;
- `sounding_pitch`: the exact sounding pitch under the work's tuning;
- top-level tuning: a stable `tuning_id`, a supported tuning system, and a
  reference frequency.

This allows a B-flat clarinet's written C and sounding B-flat to coexist, while
also retaining both the spelling and sounding pitch of a half-sharp. If MIDI
export can represent that pitch only with an integer note or an approximate
pitch bend, the loss belongs to the export policy; it must not rewrite the
score.

## Stable identities and relations

Entities such as measure, meter, tempo, part, note, tie, and phrase have stable
IDs within the document. A tie is not an ambiguous Boolean on a note. It is a
relation that explicitly references two events, and it requires the same part,
the same exact sounding pitch, strict temporal contiguity, and at most one edge
at each end. Because those references remove the inference ambiguity of the old
format, a tie explicitly authored in v2 may cross staff or voice. A phrase must
declare its `part_id`; its owning part cannot be guessed from a score-wide
position. Future slur, tuplet, ornament, pedal, glissando, and repeat contracts
follow the same rule: relations are separate from entities, and every reference
must exist and be unique.

One notated event may eventually expand into several sounding occurrences, so
an execution plan uses three separate identities:

- `source_event_id`: the notated event in the work;
- `occurrence_id`: one occurrence after form expansion;
- `note_id`: one note-on / note-off pair inside an executor.

Until the occurrence contract is connected, any form or ornament that would
produce a one-to-many expansion must fail closed. It cannot be parsed and then
ignored.

## Two hashes, two purposes

`canonical_json_sha256` is the exact **document-revision identity**. It ignores
whitespace and object-key order, but still distinguishes `1` from `1.0`, an
omitted default from an explicit default, and v1 from v2. Realizations,
candidates, atomic patches, and authoring revisions continue to bind this
identity; apparent equivalence cannot bypass an old-version check.

Score v2 also provides a domain-separated, versioned render-projection hash. It
proves only that two documents have the same projection under one version of
the execution semantics. It can support migration receipts, caching, or
diagnostics, but does not claim that all notation or layout semantics in the two
scores are identical.

## Extensions and unknown semantics

An extension declares a namespace, version, impact category, `required` flag,
and payload. An unknown extension that is explicitly optional and inaudible may
round-trip unchanged. An unknown required or audible extension must block
processing. The system can therefore admit “I cannot execute this yet” instead
of silently treating data it does not understand as absent.

Per-item limits do not replace a whole-document budget. The implementation also
applies aggregate limits to notes, articulations, meter groups, relations, and
the node count, UTF-8 byte count, and canonical-JSON byte count of all extension
payloads. Common denominators and cumulative positions in the measure timeline
also have bit-length budgets, preventing many mutually prime denominators from
expanding a seemingly small JSON document into enormous integers. The public
Schema defines representable structural boundaries; the semantic parser
continues to fail closed on cross-array totals, references, and exact-time
relations.

Typed `to_dict()` output is the normalized representation: explicit empty
optional arrays are normalized to omission. The source document hash held by
`ScoreSourceSnapshot` still preserves the revision difference between “an empty
array was written” and “the field was absent”; typed normalization does not
pretend they were the same authoring revision.

## v1 compatibility and migration

Score v1 continues to use its existing float bar/beat contract. Fixed golden
vectors protect its existing canonical Performance Plan hashes. v2 must not
silently change v1 chord, tie, humanization, or plan bytes by modifying the old
parser branch.

The implemented v1-to-v2 API produces an explicit, indivisible bundle while
preserving part and event IDs. The bundle contains four artifacts: the v2
`score`; `render_settings`, which holds the `sample_rate` and `tail_seconds`
removed from the score; `performance_facts`, currently limited to per-note
velocity and bound to the new score hash; and a `receipt`. The receipt binds the
source document hash, target document hash, target render-projection hash,
render-settings hash, and performance-facts hash. The bundle revalidates every
cross-binding before each serialization, and the receipt itself has a separate
domain-separated hash.

v1 has no provenance that can reliably distinguish score timing from imported
performance timing. Migration therefore does not guess that bar, beat, and
duration belong in realization; they remain score coordinates. Only per-note
velocity is separated as a performance fact. When numeric pitch lacks a written
spelling, migration derives a deterministic sharp-biased spelling. A v1 meter
numerator becomes a single meter group, and omitted v1 defaults are materialized
explicitly. All three decisions are recorded as structured issues in the
receipt instead of being presented as original author input.

Numeric conversion starts from the decimal text of the **parsed v1 value** and
converts it to an exact rational without a lossy `limit_denominator` call. If
the reduced exact representation exceeds the denominator limit of `1,000,000`
or the JSON safe-integer bound, migration fails closed with a location and error
code; it never approximates silently. This migration API does not currently
accept or rebind a realization. Producing a realization for the new score still
requires a separate, verifiable migration contract.

The command-line entry point reads an explicit score v1 and atomically writes
the complete bundle containing the score, render settings, performance facts,
and receipt:

```console
tianlai migrate-score-v2 --score scores/work.score.json --output output/work.score-v2-migration.json
```

This command does not render v2 and does not replace the source score in place.
The score document Schema is `schemas/score-v2.schema.json`; the migration
bundle Schema is `schemas/score-v2-migration.schema.json`. The latter's external
`$ref` ships in the formal source release together with the former.

The first implementation exposes only the linear core that can be validated
and round-tripped completely. MusicXML/MIDI import records explicit loss for
tuplets, pedals, breaths, orphan ties, form, and other semantics that do not yet
have a v2 mapping. The default reject policy no longer misreports those cases as
lossless.

Score v2 currently has an independent typed model, a Draft 2020-12 Schema, a
trusted source snapshot, a versioned render projection, and an isolated
exact-time compiler. The time compiler accepts only a trusted snapshot,
integrates tempo with exact rationals, and produces requested/resolved sample
evidence with `nearest-ties-to-even` at 8,000–384,000 Hz. It is a time
foundation with a default 32 MiB output budget, not a renderable
PerformancePlan, and it is not connected to the legacy float-based conductor.
There is deliberately no shortcut that first collapses v2 to floats. The
formal entry point compiles only the restricted subset whose articulation,
dynamics, relations, creator consent, capabilities, and runtime generation are
all closed into performance transport; execution never silently falls back to
legacy tolerances.

Above the time layer, `score_v2_plan` now provides the first sealed plan
foundation. It requires an explicit `sample_time_policy` (`exact` or `adapt`)
and a rational dynamic-to-velocity profile. Semantically consistent explicit
tie chains become one occurrence while retaining their source events,
relations, written pitch, sounding pitch, and sample-adaptation evidence. The
contract remains `not-render-authority`: phrases, extensions, multiple
articulations, and unresolved dynamics fail closed, and the plan is not yet
bound to a roster, instrument capabilities, or semantic-approximation consent,
so it cannot be sent directly to the renderer.

## Execution consent: separating capability from willingness

`score_v2_execution_profile` is an explicit creator-consent document. It is
neither an instrument-capability declaration nor a renderable plan. It records
separate numeric and semantic policies for sample-time adaptation, note
velocity, tuning, pitch, range, and articulation. Tuning has its own consent
axis rather than borrowing the per-note pitch policy. `exact` disallows alteration,
`adapt` authorizes only traceable numeric adaptation, and `approximate`
authorizes only a disclosed semantic approximation; none substitutes for
another.

The dynamic map uses canonical rationals. Tuning, note velocity, pitch, and
articulation must satisfy both the executor's measured capability and this
consent profile. Capability answers what the backend can do; the execution
profile answers what the creator is willing to accept. A later adapter may use
only their intersection and must retain requested/resolved/fidelity evidence.
The v1 phrase policy is deliberately limited to `reject`, so no unversioned
phrase-shaping interpretation is introduced silently.

Its Schema is `schemas/score-v2-execution-profile.schema.json`. The document
has its own canonical JSON hash and resource budget, but remains consent only.
It does not promote `score_v2_plan` to render authority until roster and
manifest generations, pitch/articulation capability, and runtime fingerprints
are all bound.

`score_v2_capability_source` additionally freezes the raw manifest bytes and
file identity selected by the roster, their canonical hashes and capability
projections, and each executor's effective-manifest hashes after overrides.
Custom implementations are explicitly blocked. Runtime sample fingerprints
remain `not_captured`, so this layer proves one ordinary, race-checked
capability generation without presenting uncaptured runtime assets as facts.

`score_v2_capability_adapter` then takes only the intersection of the
score/plan, execution profile, and those capability facts. Its first safe
subset requires one executor per part and rejects kits, transpose, duration
scaling, dynamic compression, automatic articulation, gain automation, and
runtime overrides. Every occurrence retains requested/resolved/fidelity
evidence for tuning, pitch, velocity, range, and articulation. The contract is
still `score-v2-capability-adapter-v1-not-render-authority`: it has not bound a
runtime fingerprint and does not emit renderer performance events.

`score_v2_runtime_source` next binds each executor's effective manifest,
legacy runtime fingerprint, Python render closure, runtime dependencies, and
aggregate asset graph. The legacy API can only recompute those sources
sequentially; it cannot freeze unrelated executor files as one atomic
generation. Per-asset descriptors, lazy-asset generations, onset evidence,
and the factory instance are also not captured yet. The artifact therefore
remains `not-render-authority`: it proves the sources observed and rechecked
during this pass, not the final generation consumed by a render transaction.

`score_v2_performance` writes the already-authorized and resolved pitch,
velocity, and articulation values into the legacy performance protocol. A
sidecar binds every event to its occurrence, role, sequence, and authoritative
integer sample. After canonical JSON round-trip, the legacy parser is run
again and every transported float time must resolve to that same sample.
Events sharing a sample remain ordered by exact work time; at one exact
instant note-off precedes each occurrence's adjacent articulation and
note-on. Events at `frame_count` are retained but marked
`pending_v2_renderer`: correct execution dispatches them after producing
exactly N frames and must not render a hidden extra frame. The first contract
also uses `tail_seconds=0`, so it does not claim a preserved natural release
tail. This bundle is verifiable transport evidence, not render authority for
publishable audio.
The first contract also does not reinterpret one note whose endpoints round to
the same sample as a “zero-sample pulse.” The plan layer rejects it explicitly
with `plan.zero_sample_duration` until such an audible meaning is defined.

## First formal render: runtime authority and Candidate v3

`project-render-v2` is the first formal CLI for a direct Score-v2 document. It
does not first downgrade v2 to v1 and does not implicitly unpack a v1-to-v2
migration bundle. The caller must explicitly supply the score, formal roster,
creator execution profile, and sample rate:

```console
tianlai project-render-v2 \
  --score scores/work.score-v2.json \
  --roster scores/work.roster.json \
  --execution-profile scores/work.execution-profile.json \
  --sample-rate 48000
```

Each of the three JSON inputs is read through a descriptor and retains its file
identity and hash for repeated checks across compilation, rendering, and
publication. The contract does not present those three sequential captures as
one cross-file atomic snapshot or claim resistance to a malicious ABA writer.
The first slice also requires a **source-workspace layout**: the instrument
directory's parent is the project root, and its `tianlai/` directory must be the
package actually loaded by the process. `--root` may select the instrument
directory inside that workspace, but cannot splice in a different code tree.

The fixed compilation chain binds the score, roster, execution profile,
`score_v2_plan`, capability source/plan, runtime source, and performance bundle.
Only then does it acquire an active, non-transferable, single-use runtime
lease. The lease fixes the raw and effective manifests, factory generation,
and the mapping from loaded Python modules to held source-file descriptors, and
keeps checkpointing them inside the transaction. Candidate metadata is still
created while that lease context is active; leaving the context performs one
final full source checkpoint. Any checkpoint failure prevents the candidate
directory from becoming a visible generation. The saved acquisition and
consumption JSON documents are historical evidence only; they cannot recreate
runtime authority in another process or render.

The renderer consumes that same lease and performance transport, writes exactly
`frame_count == N` frames, and only then dispatches events at `sample == N` in
sidecar order. It does not invent a hidden release frame. The float64 stereo
stream is incrementally quantized to a stereo PCM24 WAV through an
identity-bound private descriptor, sealed, and installed without replacement
as `合奏.wav`. The receipt binds the float-stream hash, PCM24 file hash and
size, WAV header captured through the descriptor, runtime manifest, lease
acquisition/consumption, performance bundle, and `渲染后自检.json`. “These
samples were once computed” therefore cannot substitute for “these are the
PCM24 bytes in this candidate.”

The formal artifact uses **Candidate v3** (`version: 3` in `候选.json`). Its
fixed file set closes over the direct-v2 score, roster, execution profile, all
compiled evidence, runtime-generation evidence, PCM24 audio, post-render
check, and formal receipt. It retains the candidate publication transaction's
closed-world verification and final directory exchange. `candidate-verify`
dispatches by version and can verify this generation. Success proves only that
the locally descriptor-observed bytes are self-consistent with their bindings;
it does not prove authorship or provenance, or promise that the live directory
cannot be changed after the command returns.

This formal slice intentionally promises one complete but narrow scope: one
part and one executor, the built-in oscillator, both manifest and runtime asset
graph explicitly declaring **zero external audio assets**, stereo PCM24, no
stems/space/normalization, and `tail=0`. Migration wrappers, `render_settings`,
performance facts, realization, sampled backends, custom factories,
lazy/external assets, kits, and multiple executors still fail closed. A natural
release tail is not yet promised. Existing `project-render`, Score-v1 plans,
and Candidate-v1/v2 verification keep their previous behavior and never switch
to this v2 pipeline implicitly.
