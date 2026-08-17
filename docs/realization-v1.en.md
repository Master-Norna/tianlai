[Simplified Chinese](realization-v1.md) | **English**

# Realization v1: optional performance implementation layer

`tianlai.realization` is a sparse, optional layer between a score and the final
Performance JSON. The score continues to describe musical semantics;
realization describes only per-note offsets and continuous controls explicitly
requested by a creator or captured performance. It does not store instrument
keyswitches, MIDI CC numbers, mix faders, spatial parameters, or rendered
events.

The current version is integrated with the conductor, CLI, and candidate
evidence chain. The parser validates score binding and references. The compiler
then checks capability, quantization, semantic approximation, and resource
budgets against the actual roster. Any degradation that was not explicitly
authorized or cannot leave resolved evidence must stop the operation. Omitting
a realization, or supplying one with `note_overrides=[]` and
`control_lanes=[]`, remains a strict no-op.

## Minimal document

```json
{
  "kind": "tianlai.realization",
  "schema_version": 1,
  "score_sha256": "64-character lowercase SHA-256",
  "defaults_profile": "tianlai.realization-defaults-v1",
  "mode": "interpreted",
  "note_overrides": [],
  "control_lanes": []
}
```

`defaults_profile` fixes the interpretation of every omitted value and cannot
be `latest`. `mode` may be:

- `interpreted`: explicit data comes from the creator's performance
  interpretation;
- `captured`: explicit data comes from captured or imported performance
  evidence.

The mode itself does not change any omitted parameter or silently lock a
number. Whether a captured value must be preserved is still declared by
`strategy=lock` on that field, so an empty captured document is also a no-op.

## Per-note overrides

Each entry addresses a stable score-v1 `event_id`:

```json
{
  "event_id": "event-000042",
  "timing_offset_ms": {
    "strategy": "add", "value": -12.0,
    "value_policy": "adapt", "semantic_policy": "exact"
  },
  "gate_ratio": {
    "strategy": "scale", "value": 0.84,
    "value_policy": "adapt", "semantic_policy": "exact"
  },
  "velocity": {
    "strategy": "lock", "value": 0.61,
    "value_policy": "adapt", "semantic_policy": "approximate"
  },
  "release_velocity": {
    "strategy": "replace", "value": 0.22,
    "value_policy": "exact", "semantic_policy": "exact"
  }
}
```

The five merge strategies have fixed meanings:

- `auto`: carries no `value`, inherits the automatic interpretation, and is a
  no-op for this parameter;
- `add`: adds `value` to the automatic interpretation;
- `scale`: multiplies the automatic interpretation by `value`;
- `replace`: replaces the automatic interpretation with `value` during the
  realization merge; declared musical policies may still process it later;
- `lock`: produces the same numeric result as replace but forbids later musical
  automation from rewriting it. Measured onset compensation, safety checks,
  and capability validation still run after lock and enter the trace. Lock
  fixes the requested value and musical automation in the Performance Plan; it
  does not promise physical acoustic continuity beyond the sample grid or
  backend resolution. The actually quantized value and its fidelity must enter
  resolved evidence.

Every per-note parameter other than `auto` must declare two independent creator
decisions:

- `value_policy=exact|adapt`: require the requested value to be directly
  executable, or explicitly permit snapping to the executor or sample grid.
  For timing and gate it constrains the final event time; for velocity it
  constrains the backend's actual dynamic resolution;
- `semantic_policy=exact|approximate`: require native musical semantics, or
  explicitly accept the capability's stated approximation and reason.

Neither policy substitutes for the other. A backend might accept arbitrary
floating-point velocity while only approximating “key speed” as amplitude.
Conversely, SoundFont velocity semantics may be valid while the value must
still land on a finite MIDI grid. The resolved trace retains the authored
value, final executed value, numeric fidelity, semantic fidelity, and source.

Field units and document-level limits are:

| Field | add | scale | replace / lock |
|---|---:|---:|---:|
| `timing_offset_ms` | -60000..60000 ms | 0..16 | -60000..60000 ms |
| `gate_ratio` | -16..16 ratio | (0..16] | (0..16] |
| `velocity` | -1..1 | 0..16 | 0..1 |
| `release_velocity` | -1..1 | 0..16 | 0..1 |

A valid add or scale operand does not guarantee that the merged result is
valid. The compiler checks final timing, gate, and normalized values and fails
on an out-of-range result; it never silently clamps. In particular, the score
currently has no `release_velocity` baseline. If add or scale is used and
neither defaults nor capability supplies a resolvable baseline, compilation
must fail closed. Replace and lock do not require an inherited baseline. In the
current implementation, only the MTG solo sax capability that explicitly reads
release velocity may declare support. Backends such as piano and dedicated SFZ
that ignore this value must reject compilation instead of pretending that the
field took effect.

The current performance pipeline merges a score tie chain into one sustained
sounding event and retains only the head `event_id`. Realization v1 therefore
allows overrides only on the tie-chain head. A document that addresses a
continuation `event_id` is rejected during reference validation and reports the
usable head ID. This prevents detail from appearing to be accepted and then
being silently discarded during rendering. Controlling individual chain
segments in the future requires explicit intra-chain semantics first; a
continuation cannot be treated as an independent onset.

## Sparse control lanes

Supported controls are `expression`, `sustain_pedal`, `una_corda`, and `breath`,
all in the range 0..1. Generic `modulation` is not part of realization v1:
existing backends variously interpret it as gain, vibrato, an attack latch,
spectral change, or SoundFont CC1, so it has no stable musical meaning. A future
contract should split it into explicit intents such as `vibrato_depth`,
`attack_shape`, and `timbre`, then map those through capability adapters. A lane
may target a whole part or add a `voice` restriction:

```json
{
  "lane_id": "piano-pedal",
  "target": { "part_id": "Piano", "voice": "upper" },
  "control": "sustain_pedal",
  "interpolation": "step",
  "time_policy": "exact",
  "value_policy": "exact",
  "semantic_policy": "exact",
  "points": [
    { "bar": 12, "beat": 1.5, "value": 1.0 },
    { "bar": 13, "beat": 1.0, "value": 0.0 }
  ]
}
```

Points must be strictly increasing by `(bar, beat)`, but the first point need
not be at 1:1:

- before the first point, use the target executor capability's explicitly
  declared `default_value`; the realization layer never hard-codes a supposed
  universal neutral value;
- if capability does not declare a pre-first-point default, compilation must
  stop;
- `step` switches at each explicit point and holds until the next point;
- `linear` interpolates only between adjacent explicit points; it holds the
  capability default before the first point and the last value after the final
  point;
- the resolved Performance Plan must materialize the actual pre-first-point
  value, the source of interpolation results, and capability evidence.

Each lane must declare `value_policy`:

- `exact`: the target capability must implement the requested value unchanged;
  for example, a control with only 128 levels cannot claim arbitrary
  floating-point precision, and quantization blocks the operation;
- `adapt`: the creator explicitly authorizes the capability's `adapt_value`
  rule to select the nearest executable value; the resolved plan must retain
  `requested`, `resolved`, and `fidelity`, not just the adapted number.

Control-point positions are governed independently by `time_policy`:

- `exact`: the time converted from bar/beat must land exactly on the target
  sample grid;
- `adapt`: authorize snapping to the nearest sample, while the trace records
  logical time, sample index, and final seconds.

Thus, “can the control value be represented exactly?” and “can the control
occur at the exact time?” never collapse into one decision.

Each lane must also declare `semantic_policy` independently:

- `exact`: capability must natively implement the musical meaning of the
  control;
- `approximate`: the creator explicitly accepts the semantic approximation
  declared by capability; the resolved plan must record
  `semantic_fidelity=approximated` and the concrete `reason`.

This is orthogonal to `value_policy`: a backend may accept the numeric value
exactly while only approximating its musical meaning, or support the meaning
natively while requiring numeric quantization. Capability answers whether the
executor can do it; the two authored policies separately answer whether the
creator accepts adaptation or approximation.

Capability also declares how a control takes effect: continuously, as a
note-on latch, or as a release gate. The latter two sample lane state at the
interpreted logical note boundary. If humanization, gate, or sample snapping
moves the physical event across a control point, the conductor temporarily
materializes the required state immediately before that note-on or note-off,
then restores the normal lane state after note events at the same sample. This
ensures that the note receives its control without leaking the temporary pedal
state into later music. If a control applies only to some articulations, the
compiler validates every final articulation actually routed through that lane.
A part containing an inapplicable one-shot or articulation must fail instead of
emitting an inaudible fake control.

`voice` currently means every note in the part whose `note.voice` matches
exactly, even when the same voice name appears on multiple MusicXML staves. v1
first preserves and strictly validates that intent. Current execution
capabilities are still primarily part-scoped; if the target instrument does not
support voice scope, execution must be rejected rather than silently widening
the control to the whole part.

Every `lane_id` must be unique within a document, and only one lane may exist
for a given `(part_id, voice, control)`. This avoids ambiguous precedence
between curves.

## Binding and API

Parser entry point:

```python
parse_realization_document(
    data,
    score_document=raw_score_json,
    score=parsed_score,
    expected_score_sha256=canonical_score_sha256,
)
```

`score_document` establishes binding evidence. Internally, the parser computes
its SHA-256 under the project's canonical JSON rules, parses that raw document,
and then verifies the realization's `score_sha256`. Both the hash and parse come
from the same canonical snapshot, so a caller cannot mutate a dictionary later
to create A content with a B hash. `score` is an optional parsed copy; when both
are supplied, it must equal the internally parsed result exactly. Supplying
only `score` is forbidden because another revision with overlapping event IDs
cannot prove that it is the source text bound by the realization.

`expected_score_sha256` is an optional additional caller assertion. When used
with `score_document`, it must also match the internally computed value. An
expected hash alone proves only that two strings match; it cannot prove that an
in-memory `ScoreDocument` came from that hash. Structural tools may omit every
context parameter. After binding, the parser checks that:

- the score is v1 and carries stable event IDs;
- every note override references a real event;
- a tie continuation is not independently overridden and the tie-chain head is
  referenced instead;
- every lane references a real part that contains notes;
- a voice target matches at least one note;
- every control point lies within the bar/beat range allowed by the score's
  meter.

Binding validation also resolves every control point to finite logical
quarters and seconds. An oversized coordinate that cannot be converted is
stopped as a structured `ValueError` instead of leaking an `OverflowError` into
the conductor. Per-entry realization limits protect the input, while the
compile stage also combines score-note expansion, note-on/off and articulation
events, control-point expansion, and cross-executor fan-out. It charges the
unified Performance event and resource budget before materialization; limits
from individual layers cannot simply be added and execution allowed to
continue.

The returned `RealizationDocument` and every child are frozen dataclasses, and
arrays become tuples. Public constructors also revalidate finite values,
bounds, enums, point order, nested types, duplicate IDs, and resource limits,
so bypassing the parser cannot construct a fake “validated” object. `to_dict()`
returns a fresh JSON object detached from internal state every time.
`empty_realization(score_sha256)` creates the canonical empty no-op document.

The JSON Schema is `schemas/realization.schema.json`. Schema validation checks
document shape; cross-document constraints on events, parts, voices, meter, and
hashes belong to the parser API above.

Resource limits and precision authorization remain separate concerns:
`exact` / `adapt` decides whether quantization is acceptable and never bypasses
runtime budgets. The default maximum canonical final Performance Plan is 32
MiB, and maximum total duration including tail is 7,200 seconds. Construction
charges expanded events and trace incrementally before retaining them, then
performs an exact whole-document check. Trusted non-candidate batch work may
explicitly raise `TIANLAI_MAX_PLAN_MIB` or `TIANLAI_MAX_PLAN_SECONDS`; candidate
publication retains its fixed 32 MiB integrity boundary so the system cannot
publish a candidate that it is unable to verify again.
