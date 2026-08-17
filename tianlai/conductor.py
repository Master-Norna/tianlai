"""Conductor: turns a notated score into a fully resolved performance plan.

A score is an incomplete specification.  It says ``mf`` but not how loud that
is here; it says two notes fall on the same beat but not that a double bass
must be pulled earlier than a flute for them to *sound* together.  Filling
those gaps is exactly what a conductor and an experienced section do, and
leaving it to the person writing the score would make the platform unusable.

The deviations from the printed page are resolved in three tiers, and it is
worth being precise about which is which, because they behave differently as
the instrument layer improves:

1. **Structural** — derived from the music itself: metric accent, phrase
   shape, melodic contour, articulation marks.  Fully deterministic; the same
   score always yields the same result.  This tier is *interpretation*, not
   defect compensation, so it never becomes obsolete.
2. **Physical** — derived from the instruments: an instrument with a slow
   onset must start earlier for its attack to land on the beat.  Also fully
   deterministic.  This tier applies even in strict mode, because it makes
   the notated simultaneity actually audible rather than overriding it.
3. **Residual randomness** — the small part that is genuinely arbitrary,
   because players are not machines.  Seeded and therefore reproducible.  It
   is the smallest of the three, and the one that should shrink as
   articulation coverage improves.

A common misconception is that "humanisation" *is* tier 3.  It is not: tiers
1 and 2 carry most of the effect and neither uses randomness at all.  Adding
only tier 3 produces a MIDI file with a tremor, not an ensemble.

Randomness is drawn from stable hashes rather than from one running stream.
Velocity variation is keyed per note, while timing variation is keyed per
``(executor, written onset)`` so the tones of one chord cannot be split into
an accidental arpeggio.  Legacy scores retain their original array-index
identity for per-note draws; explicit score-v1 documents use their stable
event IDs, so inserting or moving another note cannot shift an existing v1
note.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import hashlib
import json
import math
import struct
from typing import Any

from .canonical_json import canonical_json_bytes, canonical_json_sha256
from .capability import (
    RANGE_VALIDATION_MODES,
    InstrumentCapability,
    RangeProfileEvaluation,
)
from .preflight import enforce_roster_availability
from .realization import (
    RealizationDocument,
    parse_realization_document,
    realization_note_time_bounds,
)
from .realization_compile import (
    CompiledControlEvent,
    active_note_overrides,
    compile_control_lanes,
    require_release_velocity_support,
    resolve_numeric_override,
)
from .resource_limits import (
    PlanDocumentBudgetTracker,
    ProjectLimits,
    ResourceLimitError,
    performance_event_limit,
    validate_performance_document_resource_limits,
)
from .roster import (
    CollaborationSettings,
    Executor,
    Roster,
    check_roster_covers_score,
)
from .score import ScoreDocument, ScoreNote, parse_score_document, pitch_name
from .score_time import validate_score_time_coordinates


# 力度记号到基准力度。步长大致均匀(约 0.11~0.13),让相邻记号在采样力度层上
# 落到不同的层,而不是挤在同一层里听不出区别。
_DYNAMIC_VELOCITY = {
    "ppp": 0.12,
    "pp": 0.22,
    "p": 0.33,
    "mp": 0.45,
    "mf": 0.58,
    "f": 0.71,
    "ff": 0.84,
    "fff": 0.96,
}


def velocity_for_dynamic(mark: str) -> float:
    """Return the conductor's canonical base velocity for one score mark."""

    try:
        return _DYNAMIC_VELOCITY[mark]
    except KeyError as exc:
        raise ValueError(f"unknown dynamic mark: {mark!r}") from exc


# 奏法记号对时值和力度的影响。这些是记号本身的含义,与具体乐器无关;
# 乐器那侧的翻译由编制表的奏法词典负责。
_ARTICULATION_SHAPE = {
    "staccato": (0.50, 0.02),
    "staccatissimo": (0.35, 0.03),
    "tenuto": (1.00, 0.03),
    "accent": (0.92, 0.12),
    "marcato": (0.80, 0.15),
    "legato": (1.02, -0.01),
    "portato": (0.75, 0.00),
}
_DEFAULT_SHAPE = (0.95, 0.0)

_STRUCTURAL_MODES = frozenset(("strict", "ensemble"))

# 动态压缩收拢到的锚点。取在偏上位置,使强奏基本不动、弱奏被抬起——目的是补
# 齐乐器之间动态范围的差异,而不是把整条力度线压平。
_DYNAMIC_ANCHOR = 0.78

def _articulation_for_duration(
    executor: Executor,
    source_articulation: str | None,
    seconds: float,
    played_midi: float | None = None,
) -> tuple[str, str] | None:
    """Apply one instrument-declared short-note articulation contract."""

    capability = executor.capability
    for rule in capability.duration_articulation_rules:
        if source_articulation != rule.source_articulation:
            continue
        if seconds >= rule.below_seconds:
            continue
        target = rule.target_articulation
        if played_midi is not None and not capability.covers(
            played_midi,
            target,
        ):
            continue
        return (
            target,
            f"规则 {rule.rule_id}: {seconds * 1000:.0f}ms "
            f"{source_articulation} → {target}",
        )
    return None


@dataclass(frozen=True, slots=True)
class ExpressionSettings:
    """Expression tiers plus an independent range-contract enforcement mode."""

    mode: str = "ensemble"
    structural: bool = True
    physical: bool = True
    range_mode: str = "compatibility"
    humanize_depth: float = 1.0
    timing_ms: float = 8.0
    velocity_spread: float = 0.03
    seed: int = 0

    @classmethod
    def from_dict(cls, raw: object) -> "ExpressionSettings":
        if raw is None:
            return cls()
        if isinstance(raw, str):
            raw = {"mode": raw}
        if not isinstance(raw, dict):
            raise ValueError("expression must be a string or an object")
        mode = str(raw.get("mode", "ensemble"))
        if mode not in _STRUCTURAL_MODES:
            raise ValueError(
                f"unknown expression mode {mode!r}; expected strict or ensemble"
            )
        range_mode = str(raw.get("range_mode", "compatibility"))
        if range_mode not in RANGE_VALIDATION_MODES:
            supported = ", ".join(sorted(RANGE_VALIDATION_MODES))
            raise ValueError(
                f"unknown expression range_mode {range_mode!r}; "
                f"expected {supported}"
            )
        strict = mode == "strict"
        humanize = raw.get("humanize")
        if humanize is None:
            humanize = {}
        if not isinstance(humanize, dict):
            raise ValueError("expression.humanize must be an object")
        depth = float(humanize.get("depth", 0.0 if strict else 1.0))
        if not 0.0 <= depth <= 4.0:
            raise ValueError("expression.humanize.depth must be between 0 and 4")
        settings = cls(
            mode=mode,
            structural=bool(raw.get("structural", not strict)),
            physical=bool(raw.get("physical", True)),
            range_mode=range_mode,
            humanize_depth=depth,
            timing_ms=float(humanize.get("timing_ms", 8.0)),
            velocity_spread=float(humanize.get("velocity", 0.03)),
            seed=int(humanize.get("seed", 0)),
        )
        if settings.timing_ms < 0.0 or settings.velocity_spread < 0.0:
            raise ValueError("expression.humanize values must not be negative")
        return settings

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "structural": self.structural,
            "physical": self.physical,
            "range_mode": self.range_mode,
            "humanize": {
                "depth": self.humanize_depth,
                "timing_ms": self.timing_ms,
                "velocity": self.velocity_spread,
                "seed": self.seed,
            },
        }


@dataclass(frozen=True, slots=True)
class ResolvedNote:
    """A score note flattened onto the quarter-note timeline."""

    index: int
    start_quarter: float
    duration_quarters: float
    midi: float
    dynamic: str
    articulation: str | None
    bar: int
    beat: float
    velocity: float | None = None
    source_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class GainEnvelopePoint:
    """One roster gain-automation point compiled onto the audio timeline."""

    bar: int
    beat: float
    time_seconds: float
    offset_db: float

    def to_dict(self, base_gain_db: float) -> dict[str, float | int]:
        return {
            "bar": self.bar,
            "beat": self.beat,
            "time_seconds": round(self.time_seconds, 9),
            "offset_db": self.offset_db,
            "effective_gain_db": base_gain_db + self.offset_db,
        }


@dataclass(frozen=True, slots=True)
class PlanPart:
    executor: Executor
    performance: dict[str, Any]
    trace: tuple[dict[str, Any], ...]
    gain_envelope: tuple[GainEnvelopePoint, ...] = ()
    control_trace: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        data = self.executor.to_dict()
        data["performance"] = self.performance
        data["trace"] = list(self.trace)
        if self.gain_envelope:
            data["gain_envelope"] = [
                point.to_dict(self.executor.gain_db)
                for point in self.gain_envelope
            ]
        if self.control_trace:
            data["control_trace"] = list(self.control_trace)
        return data


@dataclass(frozen=True, slots=True)
class PerformanceAdvisory:
    """Non-blocking evidence retained outside the hashed performance plan."""

    code: str
    level: str
    basis: str
    confidence: str
    scope: dict[str, Any]
    message: str
    evidence: dict[str, Any]
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PerformancePlan:
    """The conductor's output: auditable, diffable, and directly renderable."""

    title: str
    sample_rate: int
    duration_seconds: float
    expression: ExpressionSettings
    roster_name: str
    parts: tuple[PlanPart, ...]
    collaboration: CollaborationSettings = field(
        default_factory=CollaborationSettings
    )
    warnings: tuple[str, ...] = field(default=())
    realization: dict[str, Any] | None = None
    # Review metadata deliberately stays outside ``to_dict`` so improving a
    # diagnosis never changes the performance-plan hash or rendered audio.
    advisories: tuple[PerformanceAdvisory, ...] = field(
        default=(),
        compare=False,
        repr=False,
    )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "title": self.title,
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "roster": self.roster_name,
            "expression": self.expression.to_dict(),
            "warnings": list(self.warnings),
            "parts": [part.to_dict() for part in self.parts],
        }
        if self.collaboration.declared:
            data["collaboration"] = self.collaboration.to_dict()
        if self.realization is not None:
            data["realization"] = dict(self.realization)
        return data


def _unit_random(
    seed: int,
    executor_id: str,
    note_identity: int | str,
    channel: str,
) -> float:
    """A stable ``[-1, 1)`` draw keyed by note identity, not by stream order.

    Using a running PRNG would make every note's jitter depend on how many
    notes preceded it, so inserting one note at the top of a part would
    silently rewrite the whole performance.  Hashing the identity instead
    keeps every other note byte-identical.
    """

    material = f"{seed}:{executor_id}:{note_identity}:{channel}".encode("utf-8")
    digest = hashlib.blake2b(material, digest_size=8).digest()
    value = struct.unpack("<Q", digest)[0] / float(1 << 64)
    return value * 2.0 - 1.0


def _merge_ties(notes: tuple[ScoreNote, ...], score: ScoreDocument) -> list[ResolvedNote]:
    resolved: list[ResolvedNote] = []
    # MusicXML voice numbers are local to a staff.  Keeping both coordinates
    # in the tie key prevents a continuation in one piano voice from
    # consuming a simultaneous note of the same pitch in another voice.
    pending: dict[tuple[float, int | None, str | None], int] = {}
    for note in notes:
        entry = score.tempo_map.entry_at_bar(note.bar)
        start = score.tempo_map.quarter_at(note.bar, note.beat)
        duration = note.duration_beats * entry.quarters_per_beat
        tie_key = (note.midi, note.staff, note.voice)
        held = pending.pop(tie_key, None)
        if held is not None:
            previous = resolved[held]
            if math.isclose(
                previous.start_quarter + previous.duration_quarters, start, abs_tol=1e-6
            ):
                resolved[held] = ResolvedNote(
                    index=previous.index,
                    start_quarter=previous.start_quarter,
                    duration_quarters=previous.duration_quarters + duration,
                    midi=previous.midi,
                    dynamic=previous.dynamic,
                    articulation=previous.articulation,
                    bar=previous.bar,
                    beat=previous.beat,
                    velocity=previous.velocity,
                    source_event_id=previous.source_event_id,
                )
                if note.tie:
                    pending[tie_key] = held
                continue
        resolved.append(
            ResolvedNote(
                index=note.index,
                start_quarter=start,
                duration_quarters=duration,
                midi=note.midi,
                dynamic=note.dynamic or "",
                articulation=note.articulation,
                bar=note.bar,
                beat=note.beat,
                velocity=note.velocity,
                source_event_id=note.source_event_id,
            )
        )
        if note.tie:
            pending[tie_key] = len(resolved) - 1
    return resolved


def _onset_groups(
    notes: list[ResolvedNote], start: int = 0, end: int | None = None
) -> list[tuple[int, int]]:
    """Return inclusive index spans for notes that begin together.

    A chord is one musical onset, not several consecutive melodic positions.
    Keeping this grouping explicit prevents pitch-sorted chord tones from
    receiving different phrase timing merely because they occupy different
    positions in the flattened note list.
    """

    if not notes:
        return []
    stop = len(notes) - 1 if end is None else end
    if start < 0 or stop < start or stop >= len(notes):
        raise ValueError("invalid onset-group bounds")
    groups: list[tuple[int, int]] = []
    group_start = start
    onset = notes[start].start_quarter
    for index in range(start + 1, stop + 1):
        if not math.isclose(
            notes[index].start_quarter, onset, rel_tol=0.0, abs_tol=1e-9
        ):
            groups.append((group_start, index - 1))
            group_start = index
            onset = notes[index].start_quarter
    groups.append((group_start, stop))
    return groups


def _infer_phrases(notes: list[ResolvedNote]) -> list[tuple[int, int]]:
    """Split a part into phrases at rests, the way a player breathes.

    An explicit phrase mark in the score always wins; this only runs when the
    score is silent about phrasing, which is the common case for imported
    MIDI.  A gap of at least one quarter note ends a phrase, and no phrase is
    allowed to run past 32 quarters so that long held passages still breathe.
    """

    if not notes:
        return []
    groups = _onset_groups(notes)
    phrases: list[tuple[int, int]] = []
    start_group = 0
    sounding_until = max(
        notes[index].start_quarter + notes[index].duration_quarters
        for index in range(groups[0][0], groups[0][1] + 1)
    )
    for group_index in range(1, len(groups)):
        group_start, group_end = groups[group_index]
        onset = notes[group_start].start_quarter
        gap = onset - sounding_until
        span = onset - notes[groups[start_group][0]].start_quarter
        if gap >= 1.0 or span > 32.0:
            phrases.append((groups[start_group][0], groups[group_index - 1][1]))
            start_group = group_index
            sounding_until = onset
        sounding_until = max(
            sounding_until,
            max(
                notes[index].start_quarter + notes[index].duration_quarters
                for index in range(group_start, group_end + 1)
            ),
        )
    phrases.append((groups[start_group][0], groups[-1][1]))
    return phrases


def _metric_accent(note: ResolvedNote, score: ScoreDocument) -> tuple[float, str]:
    """Strong beats carry more weight; off-beats less.  Zero randomness."""

    entry = score.tempo_map.entry_at_bar(note.bar)
    beat = note.beat
    if not math.isclose(beat, round(beat), abs_tol=1e-6):
        return -0.030, "弱位"
    position = int(round(beat))
    if position == 1:
        return 0.060, "小节强拍"
    half = entry.beats_per_bar / 2 + 1
    if entry.beats_per_bar % 2 == 0 and position == int(half):
        return 0.030, "次强拍"
    return 0.0, "普通拍"


def _phrase_shape(
    position: float, is_last: bool
) -> tuple[float, float, str]:
    """Arch the dynamics across a phrase and relax its final note.

    ``position`` runs 0→1 across the phrase.  The arch peaks slightly past the
    middle, which is how phrases are normally shaped; the closing note is
    both softer and a little late, which is what "breathing" sounds like.
    """

    arch = math.sin(math.pi * min(1.0, max(0.0, position))) * 0.045
    if is_last:
        return arch - 0.050, 0.035, "句尾收"
    if position < 0.12:
        return arch + 0.010, 0.0, "句首推"
    return arch, 0.0, "句中"


def _contour(note: ResolvedNote, low: float, high: float) -> tuple[float, str]:
    if high - low < 1.0:
        return 0.0, "音区平"
    ratio = (note.midi - low) / (high - low)
    return (ratio - 0.5) * 0.055, f"音区 {ratio:.0%}"


def _articulation_shape(marking: str | None) -> tuple[float, float]:
    if marking is None:
        return _DEFAULT_SHAPE
    return _ARTICULATION_SHAPE.get(marking, _DEFAULT_SHAPE)


def _check_playable(
    executor: Executor,
    note: ResolvedNote,
    capability: InstrumentCapability,
    articulation: str | None = None,
) -> None:
    midi = note.midi + executor.transpose
    if capability.note_min is None or capability.note_max is None:
        if capability.pitched and not capability.ignores_pitch:
            raise ValueError(
                f"{capability.name} 未声明音域(note_min/note_max),"
                "无法校验可演奏性;请先在乐器清单里声明实测音域"
            )
        return
    if not capability.covers(midi, articulation):
        specific_ranges = next(
            (
                ranges
                for name, ranges in capability.articulation_playable_ranges
                if name == articulation
            ),
            None,
        )
        declared_ranges = capability.ranges_for(articulation)
        if specific_ranges is not None:
            declared_range = "、".join(
                f"{pitch_name(low)}~{pitch_name(high)}"
                for low, high in specific_ranges
            )
            range_label = (
                f"奏法 {articulation!r} 的可演奏分段 {declared_range}"
            )
        elif capability.playable_ranges:
            declared_range = "、".join(
                f"{pitch_name(low)}~{pitch_name(high)}"
                for low, high in declared_ranges
            )
            range_label = f"可演奏分段 {declared_range}"
        else:
            range_label = (
                f"音域 {pitch_name(capability.note_min)}"
                f"~{pitch_name(capability.note_max)}"
            )
        raise ValueError(
            f"声部 {executor.part_id!r} 第 {note.bar} 小节第 {note.beat:g} 拍的 "
            f"{pitch_name(midi)} 超出 {capability.name} 的{range_label};"
            "请改写该音、换乐器,或在编制表里设置 transpose"
        )


def _enforce_strict_hq_range(
    executor: Executor,
    note: ResolvedNote,
    evaluation: RangeProfileEvaluation,
) -> None:
    """Fail closed unless the exact applicable profile approves this note."""

    if not evaluation.applicable or evaluation.verified:
        return
    configuration = (
        ", ".join(
            f"{key}={value!r}"
            for key, value in evaluation.runtime_configuration
        )
        or "(empty)"
    )
    profile = (
        evaluation.profile.profile_id
        if evaluation.profile is not None
        else "(none)"
    )
    raise ValueError(
        f"声部 {executor.part_id!r} 第 {note.bar} 小节第 {note.beat:g} 拍的 "
        f"{pitch_name(evaluation.midi_note)} 未通过 strict_hq 音域合同: "
        f"status={evaluation.status}, profile={profile}, "
        f"final_articulation={evaluation.final_articulation!r}, "
        f"resolved_runtime_configuration={configuration}; "
        "缺失、待审、已拒绝、配置不匹配或超出当前高质量范围都不会回退到 "
        "legacy 音域"
    )


def _compile_gain_envelope(
    executor: Executor, score: ScoreDocument
) -> tuple[GainEnvelopePoint, ...]:
    """Resolve bar/beat mix rides through the score's complete tempo map."""

    compiled: list[GainEnvelopePoint] = []
    for point in executor.gain_automation:
        meter = score.tempo_map.entry_at_bar(point.bar)
        # Beat numbering starts at 1, so a 4/4 bar occupies [1, 5).  Accepting
        # beat 5 would create two spellings for the following downbeat and make
        # diffs/automation ordering needlessly ambiguous.
        if point.beat >= meter.beats_per_bar + 1.0:
            raise ValueError(
                f"声部 {executor.part_id!r} 的 gain_automation 在第 {point.bar} "
                f"小节写了第 {point.beat:g} 拍，但该小节是 "
                f"{meter.beats_per_bar}/{meter.beat_unit}"
            )
        time_seconds = score.tempo_map.seconds_at(point.bar, point.beat)
        if compiled and time_seconds <= compiled[-1].time_seconds:
            raise ValueError(
                f"声部 {executor.part_id!r} 的 gain_automation 编译后时间不递增"
            )
        compiled.append(
            GainEnvelopePoint(
                bar=point.bar,
                beat=point.beat,
                time_seconds=time_seconds,
                offset_db=point.offset_db,
            )
        )
    return tuple(compiled)


def _onset_context_for_overlap(policy: str, *, overlaps_earlier: bool) -> str:
    """Classify one written onset using only its declared instrument policy."""

    if not overlaps_earlier or policy == "polyphonic_independent":
        return "isolated_attack"
    if policy in {"conservative", "monophonic_connected"}:
        return "connected_transition"
    raise ValueError(f"unsupported onset overlap policy: {policy!r}")


def _merge_compiled_control_events(
    performance_events: list[dict[str, Any]],
    controls: tuple[CompiledControlEvent, ...],
    *,
    sample_rate: int,
    logical_note_onsets: tuple[tuple[float, float], ...] = (),
    logical_note_releases: tuple[tuple[float, float], ...] = (),
    control_event_limit: int | None = None,
    plan_budget: PlanDocumentBudgetTracker | None = None,
) -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...], float]:
    """Merge controls with sample-accurate, scoped note-boundary state.

    Authored controls establish the ordinary clock-time state.  A latched or
    release-gate backend may additionally need the value that was in force at
    the note's *logical* boundary after humanisation or gate processing moved
    its physical event.  Such a correction is a one-event transaction: apply
    the logical value immediately before the note event, then restore the
    ordinary clock-time value immediately afterwards.  Without the restore, a
    delayed release could turn the pedal back on permanently.
    """

    if not controls:
        latest = max(
            (float(item["time"]) for item in performance_events),
            default=0.0,
        )
        return performance_events, (), latest
    rows: list[tuple[int, int, int, dict[str, Any]]] = []
    for index, item in enumerate(performance_events):
        sample = max(0, round(float(item["time"]) * sample_rate))
        normalized = dict(item)
        normalized["time"] = round(sample / sample_rate, 9)
        rows.append((sample, 2, index, normalized))
    trace: list[dict[str, Any]] = []
    for index, control in enumerate(controls):
        sample = max(0, round(control.time_seconds * sample_rate))
        resolved_time = sample / sample_rate
        time_adapted = not math.isclose(
            control.time_seconds,
            resolved_time,
            rel_tol=0.0,
            abs_tol=1.0e-12,
        )
        if time_adapted and control.time_policy == "exact":
            raise ValueError(
                f"realization control lane {control.lane_id!r} requested "
                f"exact time {control.time_seconds:.12g}s, but the "
                f"{sample_rate} Hz grid resolves it to {resolved_time:.12g}s"
            )
        event = control.performance_event()
        event["time"] = round(resolved_time, 9)
        rows.append((sample, 0, index, event))
        evidence = control.trace_entry()
        evidence["resolved_sample"] = sample
        evidence["resolved_time_seconds"] = round(resolved_time, 9)
        evidence["time_adapted"] = time_adapted
        trace.append(evidence)
    sampled_groups: dict[
        tuple[str, str], list[CompiledControlEvent]
    ] = {}
    for control in controls:
        if control.application in {"note_on_latched", "release_gate"}:
            sampled_groups.setdefault(
                (control.lane_id, control.name),
                [],
            ).append(control)
    insertion_specs: list[
        tuple[
            int,
            CompiledControlEvent,
            CompiledControlEvent,
            str,
        ]
    ] = []
    for (_lane_id, _name), lane_events in sorted(sampled_groups.items()):
        lane_events.sort(key=lambda item: item.time_seconds)
        logical_times = [item.time_seconds for item in lane_events]
        application = lane_events[0].application
        queries = (
            logical_note_onsets
            if application == "note_on_latched"
            else logical_note_releases
        )
        desired_by_sample: dict[int, CompiledControlEvent] = {}
        for logical_time, scheduled_time in queries:
            position = bisect_right(logical_times, logical_time + 1.0e-12) - 1
            if position < 0:
                raise ValueError(
                    f"sampled control lane {lane_events[0].lane_id!r} has no "
                    f"value at a routed note {application} boundary"
                )
            selected = lane_events[position]
            sample = max(0, round(scheduled_time * sample_rate))
            previous = desired_by_sample.get(sample)
            if previous is not None and not math.isclose(
                previous.value,
                selected.value,
                rel_tol=0.0,
                abs_tol=1.0e-12,
            ):
                raise ValueError(
                    f"sampled control lane {selected.lane_id!r} requires "
                    "different values for note boundaries on the same sample"
                )
            desired_by_sample[sample] = selected

        # Resolve the ordinary clock-time state at each physical boundary.
        # Any temporary logical-time correction is restored after the note
        # event, so it cannot leak into a later note or leave a release gate
        # permanently active.  A constant lane therefore stays O(points), not
        # O(notes), because its logical and physical values are identical.
        base_by_sample: dict[int, CompiledControlEvent] = {}
        for item in lane_events:
            base_by_sample[
                max(0, round(item.time_seconds * sample_rate))
            ] = item
        base_samples = sorted(base_by_sample)
        for sample, selected in sorted(desired_by_sample.items()):
            base_position = bisect_right(base_samples, sample) - 1
            if base_position < 0:
                raise ValueError(
                    f"sampled control lane {selected.lane_id!r} has no "
                    "clock-time state at a routed note boundary"
                )
            ordinary = base_by_sample[base_samples[base_position]]
            if not math.isclose(
                ordinary.value,
                selected.value,
                rel_tol=0.0,
                abs_tol=1.0e-12,
            ):
                insertion_specs.append(
                    (sample, selected, ordinary, application)
                )

    if (
        control_event_limit is not None
        and len(controls) + 2 * len(insertion_specs) > control_event_limit
    ):
        materialized_count = len(controls) + 2 * len(insertion_specs)
        raise ResourceLimitError(
            "realization.too_many_control_events",
            "realization scheduling would materialize "
            f"{materialized_count} control events, "
            f"exceeding limit {control_event_limit}",
            actual=materialized_count,
            limit=control_event_limit,
        )
    sampled_index = len(controls)
    for sample, selected, ordinary, application in insertion_specs:
        resolved_time = sample / sample_rate
        event = selected.performance_event()
        event["time"] = round(resolved_time, 9)
        if plan_budget is not None:
            plan_budget.charge_fragment(event, framing_bytes=32)
        rows.append((sample, 1, sampled_index, event))
        sampled_index += 1
        evidence = selected.trace_entry()
        evidence.update(
            {
                "materialized_for_note_boundary": application,
                "logical_control_time_seconds": round(
                    selected.time_seconds,
                    9,
                ),
                "resolved_sample": sample,
                "resolved_time_seconds": round(resolved_time, 9),
            }
        )
        if plan_budget is not None:
            plan_budget.charge_fragment(evidence, framing_bytes=64)
        trace.append(evidence)
        restore = ordinary.performance_event()
        restore["time"] = round(resolved_time, 9)
        if plan_budget is not None:
            plan_budget.charge_fragment(restore, framing_bytes=32)
        rows.append((sample, 3, sampled_index, restore))
        sampled_index += 1
        restore_evidence = ordinary.trace_entry()
        restore_evidence.update(
            {
                "restored_after_note_boundary": application,
                "resolved_sample": sample,
                "resolved_time_seconds": round(resolved_time, 9),
            }
        )
        if plan_budget is not None:
            plan_budget.charge_fragment(
                restore_evidence,
                framing_bytes=64,
            )
        trace.append(restore_evidence)
    rows.sort(key=lambda item: (item[0], item[1], item[2]))
    merged = [item[3] for item in rows]
    return merged, tuple(trace), max(item[0] for item in rows) / sample_rate


def _validate_control_articulation_applicability(
    compiled_controls: dict[str, tuple[CompiledControlEvent, ...]],
    executors: dict[str, Executor],
    resolved_articulations: dict[str, set[str | None]],
) -> None:
    """Prove each part-wide lane applies to every note it will encounter.

    Some backends expose a control only for selected sample articulations (for
    example a percussion roll, but not a one-shot hit).  Capability discovery
    alone is therefore insufficient: the conductor must check the *final*
    roster-mapped/automatic articulation selected for every routed note.
    """

    for executor_id, controls in compiled_controls.items():
        executor = executors[executor_id]
        signatures = {
            (
                control.lane_id,
                control.name,
                control.interpolation,
                control.semantic_policy,
                "per_note" if control.voice is not None else "part",
            )
            for control in controls
        }
        for lane_id, name, interpolation, semantic_policy, scope in sorted(
            signatures
        ):
            declared = executor.capability.require_control(
                name,
                scope=scope,
                interpolation=interpolation,
                semantic_policy=semantic_policy,
            )
            for articulation in sorted(
                resolved_articulations.get(executor_id, ()),
                key=lambda value: "" if value is None else value,
            ):
                if articulation is None:
                    if declared.applicable_articulations is not None:
                        raise ValueError(
                            f"realization control lane {lane_id!r} cannot "
                            f"prove articulation applicability for executor "
                            f"{executor_id!r} with no named articulation"
                        )
                    continue
                try:
                    executor.capability.require_control(
                        name,
                        scope=scope,
                        interpolation=interpolation,
                        articulation=articulation,
                        semantic_policy=semantic_policy,
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"realization control lane {lane_id!r} cannot apply "
                        f"to executor {executor_id!r} articulation "
                        f"{articulation!r}: {exc}"
                    ) from exc


def _enforce_sample_value_policy(
    override: Any,
    requested_seconds: float,
    resolved_seconds: float,
    *,
    path: str,
) -> dict[str, Any] | None:
    if override is None or override.is_noop:
        return None
    adapted = not math.isclose(
        requested_seconds,
        resolved_seconds,
        rel_tol=0.0,
        abs_tol=1.0e-12,
    )
    if adapted and override.value_policy == "exact":
        raise ValueError(
            f"{path} requested exact sample timing, but {requested_seconds:.12g}s "
            f"resolves to {resolved_seconds:.12g}s"
        )
    return {
        "value_policy": override.value_policy,
        "requested_seconds": requested_seconds,
        "resolved_seconds": resolved_seconds,
        "adapted": adapted,
    }


def build_plan(
    score: ScoreDocument,
    roster: Roster,
    expression: ExpressionSettings | None = None,
    realization: RealizationDocument | None = None,
    *,
    score_document: dict[str, Any] | None = None,
    realization_document: dict[str, Any] | None = None,
) -> PerformancePlan:
    """Resolve score, roster and optional realization into renderable plans.

    A realization is bound to the canonical source score rather than merely to
    overlapping event IDs.  Callers must therefore provide the raw
    ``score_document`` whenever they provide one.  Re-parsing both documents at
    this trust boundary also prevents manually constructed frozen dataclasses
    from bypassing the public validation contract.
    """

    settings = expression or ExpressionSettings()
    realization_canonical_sha256: str | None = None
    if realization is not None:
        if not isinstance(realization, RealizationDocument):
            raise TypeError("realization must be a RealizationDocument")
        if score_document is None:
            raise ValueError(
                "build_plan requires score_document when realization is provided"
            )
        source_realization_input = (
            realization.to_dict()
            if realization_document is None
            else realization_document
        )
        if not isinstance(source_realization_input, dict):
            raise TypeError("realization_document must be an object")
        try:
            frozen_score_document = json.loads(
                canonical_json_bytes(score_document)
            )
            source_realization = json.loads(
                canonical_json_bytes(source_realization_input)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "score_document and realization_document must be finite "
                "portable JSON"
            ) from exc
        trusted_score = parse_score_document(frozen_score_document)
        if trusted_score != score:
            raise ValueError("score does not match score_document")
        parsed_realization = parse_realization_document(
            source_realization,
            score_document=frozen_score_document,
            score=trusted_score,
        )
        if parsed_realization != realization:
            raise ValueError(
                "realization does not match realization_document"
            )
        realization = parsed_realization
        score = trusted_score
        realization_canonical_sha256 = canonical_json_sha256(
            source_realization
        )
    elif realization_document is not None:
        raise ValueError(
            "realization_document requires a parsed realization"
        )
    elif score_document is not None and not isinstance(score_document, dict):
        raise TypeError("score_document must be an object")
    active_realization = (
        realization
        if realization is not None and not realization.is_noop
        else None
    )
    project_limits = ProjectLimits.from_environment()
    plan_budget = PlanDocumentBudgetTracker(project_limits)
    plan_budget.charge_fragment(
        {
            "title": score.title,
            "sample_rate": score.sample_rate,
            "roster": roster.name,
            "expression": settings.to_dict(),
            "collaboration": (
                roster.collaboration.to_dict()
                if roster.collaboration.declared
                else None
            ),
        },
        framing_bytes=1024,
    )
    # A bar uses a half-open beat interval: in 4/4, beat 5 is the next
    # downbeat and must be written as the following bar's beat 1.  Enforce the
    # same coordinate contract for every CLI/MCP/batch caller before any
    # performance choices are made.
    validate_score_time_coordinates(score)
    # 许可隔离是协作核心的硬边界，不能只靠 MCP 或某个 CLI 入口代劳。
    # 所有批量工具最终都经过 build_plan，因此在这里统一拒绝 quarantined。
    enforce_roster_availability(roster)
    check_roster_covers_score(roster, score)

    note_realizations = active_note_overrides(active_realization)
    unapplied_note_realizations = set(note_realizations)
    realization_limits = (
        project_limits if active_realization is not None else None
    )
    if realization_limits is not None:
        for part_id, note_index, start_time, end_time in (
            realization_note_time_bounds(score)
        ):
            effective_end = end_time + score.tail_seconds
            if effective_end > realization_limits.max_plan_seconds:
                raise ResourceLimitError(
                    "realization.note_time_too_late",
                    f"score part {part_id!r} note {note_index} reaches "
                    f"{effective_end:g}s, exceeding plan limit "
                    f"{realization_limits.max_plan_seconds}s; raise "
                    "TIANLAI_MAX_PLAN_SECONDS deliberately if trusted",
                    actual=effective_end,
                    limit=realization_limits.max_plan_seconds,
                )
            for label, seconds in (
                ("start", start_time),
                ("end", end_time),
            ):
                frame = seconds * score.sample_rate
                if not math.isfinite(frame):
                    raise ValueError(
                        f"score part {part_id!r} note {note_index} {label} "
                        "exceeds the finite sample timeline"
                    )
    compiled_controls = compile_control_lanes(
        active_realization,
        score,
        roster,
        event_limit=(
            performance_event_limit(realization_limits)
            if realization_limits is not None
            else None
        ),
        max_time_seconds=(
            realization_limits.max_plan_seconds
            if realization_limits is not None
            else None
        ),
        control_trace_event_limit=(
            max(1, project_limits.max_plan_json_bytes // 1024)
            if realization_limits is not None
            else None
        ),
        plan_budget=plan_budget,
    )

    resolved_onsets: dict[str, tuple[Any, ...]] = {}
    onset_configuration_mismatches: dict[str, tuple[str, ...]] = {}
    if settings.physical:
        by_manifest: dict[str, tuple[Any, ...]] = {}
        for executor in roster.executors:
            affecting_overrides = tuple(
                name
                for name in ("sample_variant",)
                if name in executor.override_map
            )
            if affecting_overrides and (
                executor.capability.articulation_onsets
                or executor.capability.onset_evidence_path is not None
            ):
                # Approved evidence is bound to the base manifest.  The audio
                # renderer applies roster overrides later, so reusing base
                # evidence for another sample family would be an unreviewed
                # cross-configuration guess.
                resolved_onsets[executor.executor_id] = ()
                onset_configuration_mismatches[executor.executor_id] = (
                    affecting_overrides
                )
                continue
            key = executor.capability.manifest_path
            records = by_manifest.get(key)
            if records is None:
                records = executor.capability.resolve_articulation_onsets()
                by_manifest[key] = records
            resolved_onsets[executor.executor_id] = records

    buckets: dict[str, list[dict[str, Any]]] = {
        executor.executor_id: [] for executor in roster.executors
    }
    traces: dict[str, list[dict[str, Any]]] = {
        executor.executor_id: [] for executor in roster.executors
    }
    logical_note_onsets: dict[str, list[tuple[float, float]]] = {
        executor.executor_id: [] for executor in roster.executors
    }
    logical_note_releases: dict[str, list[tuple[float, float]]] = {
        executor.executor_id: [] for executor in roster.executors
    }
    resolved_articulations: dict[str, set[str | None]] = {
        executor.executor_id: set() for executor in roster.executors
    }
    by_id = {executor.executor_id: executor for executor in roster.executors}
    warnings: list[str] = []
    advisories: list[PerformanceAdvisory] = []
    executor_note_counts = {
        executor.executor_id: 0 for executor in roster.executors
    }
    automatic_articulation_counts = {
        executor.executor_id: 0 for executor in roster.executors
    }
    last_time = 0.0
    # Connection context is decided on the written timeline, before expression
    # timing or onset compensation.  The capability declaration decides whether
    # an overlap means a monophonic transition or another independent attack;
    # instrument names and families are never used as heuristics.
    logical_span_state: dict[str, dict[str, float | str | None]] = {
        executor.executor_id: {
            "group_start": None,
            "earlier_max_end": float("-inf"),
            "group_max_end": float("-inf"),
            "context": "isolated_attack",
        }
        for executor in roster.executors
    }

    dropped = set(roster.dropped_parts)
    for part in score.parts:
        if part.id in dropped:
            # 已在编制表里显式声明有意删除,连音符一起跳过。
            continue
        notes = _merge_ties(part.notes, score)
        if not notes:
            continue
        if part.phrases:
            bounds: list[tuple[int, int]] = []
            for phrase in part.phrases:
                start_q = score.tempo_map.quarter_at(phrase.start_bar, phrase.start_beat)
                end_q = score.tempo_map.quarter_at(phrase.end_bar, phrase.end_beat)
                indices = [
                    index
                    for index, note in enumerate(notes)
                    if start_q - 1e-6 <= note.start_quarter <= end_q + 1e-6
                ]
                if indices:
                    bounds.append((indices[0], indices[-1]))
            phrases = bounds or _infer_phrases(notes)
        else:
            phrases = _infer_phrases(notes)

        # 乐句位置按“同起音组”而不是扁平音符序号计算。同一和弦的各音必须
        # 一起处于句首/句中/句尾；音高排序只用于稳定输出，不能把末和弦的
        # 最高音单独推迟成一个假装的句尾。旋律音区 contour 仍按逐音计算。
        phrase_of: dict[int, tuple[int, int, float, bool]] = {}
        for span in phrases:
            groups = _onset_groups(notes, span[0], span[1])
            denominator = max(1, len(groups) - 1)
            for group_position, group in enumerate(groups):
                position = group_position / denominator
                is_last = group_position == len(groups) - 1
                for index in range(group[0], group[1] + 1):
                    phrase_of[index] = (span[0], span[1], position, is_last)

        for order, note in enumerate(notes):
            executor = roster.route(part.id, note.midi)
            capability = executor.capability
            note_realization = (
                note_realizations.get(note.source_event_id)
                if note.source_event_id is not None
                else None
            )
            if note_realization is not None:
                unapplied_note_realizations.discard(note_realization.event_id)
                release_override = note_realization.release_velocity
                if (
                    release_override is not None
                    and not release_override.is_noop
                ):
                    require_release_velocity_support(
                        capability,
                        event_id=note_realization.event_id,
                    )
            executor_note_counts[executor.executor_id] += 1
            span_state = logical_span_state[executor.executor_id]
            group_start = span_state["group_start"]
            if group_start is None or not math.isclose(
                float(group_start),
                note.start_quarter,
                abs_tol=1e-9,
            ):
                earlier_max_end = max(
                    float(span_state["earlier_max_end"]),
                    float(span_state["group_max_end"]),
                )
                span_state["earlier_max_end"] = earlier_max_end
                span_state["group_start"] = note.start_quarter
                span_state["group_max_end"] = float("-inf")
                span_state["context"] = _onset_context_for_overlap(
                    capability.onset_overlap_policy,
                    overlaps_earlier=(
                        earlier_max_end > note.start_quarter + 1e-9
                    ),
                )
            onset_context = str(span_state["context"])
            span_state["group_max_end"] = max(
                float(span_state["group_max_end"]),
                note.start_quarter + note.duration_quarters,
            )
            marking = note.articulation or part.default_articulation
            articulation, articulation_reason = executor.mapped_articulation(marking)
            if marking is None and settings.structural and executor.articulation_auto:
                # 用确定性的实际门控时值判断：包括编制表 duration_scale，
                # 但不含残差抖动。是否可替换以及阈值完全来自乐器能力合同，
                # 不能再凭 ``accent`` 等名字猜音乐语义。
                notated = score.tempo_map.seconds_at_quarter(
                    note.start_quarter + note.duration_quarters
                ) - score.tempo_map.seconds_at_quarter(note.start_quarter)
                notated_shape, _velocity_shape = _articulation_shape(marking)
                effective_gate = max(
                    0.02,
                    notated * notated_shape * executor.duration_scale,
                )
                chosen = _articulation_for_duration(
                    executor,
                    articulation,
                    effective_gate,
                    note.midi + executor.transpose,
                )
                if chosen is not None:
                    articulation, articulation_reason = chosen
                    automatic_articulation_counts[executor.executor_id] += 1
            _check_playable(executor, note, capability, articulation)
            resolved_articulations[executor.executor_id].add(articulation)
            played_midi = note.midi + executor.transpose
            if capability.ignores_pitch and capability.fixed_midi_note is not None:
                played_midi = capability.fixed_midi_note
            range_evaluation = capability.evaluate_range_profile(
                played_midi,
                articulation,
                overrides=executor.override_map,
                mode=settings.range_mode,
            )
            if settings.range_mode == "strict_hq":
                _enforce_strict_hq_range(
                    executor,
                    note,
                    range_evaluation,
                )

            derivation: dict[str, Any] = {
                "音域合同": range_evaluation.to_dict(),
            }
            if note.velocity is not None:
                # 导入的演奏自带逐音力度,直接采用;力度记号的查表值会丢掉
                # 演奏者真正做出的层次。
                velocity = note.velocity
                derivation["力度记号"] = f"演奏自带 {velocity:.3f}"
            else:
                dynamic = note.dynamic or part.default_dynamic
                velocity = _DYNAMIC_VELOCITY[dynamic]
                derivation["力度记号"] = f"{dynamic} → {velocity:.3f}"

            duration_scale, articulation_velocity = _articulation_shape(marking)
            timing_offset = 0.0

            if settings.structural:
                accent, accent_reason = _metric_accent(note, score)
                velocity += accent
                derivation["节拍重音"] = f"{accent_reason} {accent:+.3f}"

                phrase = phrase_of.get(order)
                if phrase is not None:
                    span_start, span_end, position, is_last = phrase
                    low = min(notes[i].midi for i in range(span_start, span_end + 1))
                    high = max(notes[i].midi for i in range(span_start, span_end + 1))
                    shape, delay_quarters, shape_reason = _phrase_shape(
                        position, is_last
                    )
                    velocity += shape
                    timing_offset += delay_quarters
                    derivation["乐句"] = f"{shape_reason} {shape:+.3f}"
                    contour, contour_reason = _contour(note, low, high)
                    velocity += contour
                    derivation["旋律走向"] = f"{contour_reason} {contour:+.3f}"

                velocity += articulation_velocity
                if articulation_velocity:
                    derivation["奏法记号"] = (
                        f"{marking} {articulation_velocity:+.3f}"
                    )

            notated_start_seconds = score.tempo_map.seconds_at_quarter(
                note.start_quarter
            )
            start_seconds = score.tempo_map.seconds_at_quarter(
                note.start_quarter + timing_offset
            )
            notated_end_seconds = score.tempo_map.seconds_at_quarter(
                note.start_quarter + note.duration_quarters
            )
            notated_duration_seconds = (
                notated_end_seconds - notated_start_seconds
            )
            sounding = max(
                0.02,
                (notated_end_seconds - start_seconds)
                * duration_scale
                * executor.duration_scale,
            )

            if settings.humanize_depth > 0.0:
                humanize_identity: int | str = (
                    note.source_event_id
                    if note.source_event_id is not None
                    else note.index
                )
                # Residual timing belongs to one physical gesture, not to
                # each pitch in that gesture.  ``span_state["group_start"]``
                # is shared by every note routed to this executor at the
                # same written onset (using the same tolerance as the onset
                # overlap classifier above), so a piano chord or section
                # divisi remains simultaneous instead of turning into a
                # random 16 ms-wide flam.
                onset_identity = (
                    f"onset:{float(span_state['group_start']).hex()}"
                )
                jitter = (
                    _unit_random(
                        settings.seed,
                        executor.executor_id,
                        onset_identity,
                        "t",
                    )
                    * settings.timing_ms
                    / 1000.0
                    * settings.humanize_depth
                )
                velocity_jitter = (
                    _unit_random(
                        settings.seed,
                        executor.executor_id,
                        humanize_identity,
                        "v",
                    )
                    * settings.velocity_spread
                    * settings.humanize_depth
                )
                start_seconds += jitter
                velocity += velocity_jitter
                derivation["残差随机"] = (
                    f"时值 {jitter * 1000.0:+.1f}ms / 力度 {velocity_jitter:+.3f}"
                )

            realization_evidence: dict[str, Any] = {}
            if note_realization is not None:
                timing_resolution = resolve_numeric_override(
                    "timing_offset_ms",
                    (start_seconds - notated_start_seconds) * 1000.0,
                    note_realization.timing_offset_ms,
                    path=(
                        "realization.note_overrides"
                        f"[{note_realization.event_id!r}].timing_offset_ms"
                    ),
                )
                if timing_resolution.evidence is not None:
                    assert timing_resolution.value is not None
                    start_seconds = notated_start_seconds + (
                        timing_resolution.value / 1000.0
                    )
                    realization_evidence["timing_offset_ms"] = (
                        timing_resolution.evidence
                    )

                automatic_gate_ratio = sounding / notated_duration_seconds
                gate_resolution = resolve_numeric_override(
                    "gate_ratio",
                    automatic_gate_ratio,
                    note_realization.gate_ratio,
                    path=(
                        "realization.note_overrides"
                        f"[{note_realization.event_id!r}].gate_ratio"
                    ),
                )
                if gate_resolution.evidence is not None:
                    assert gate_resolution.value is not None
                    sounding = notated_duration_seconds * gate_resolution.value
                    realization_evidence["gate_ratio"] = gate_resolution.evidence

            requested_logical_start_seconds = start_seconds
            if start_seconds < 0.0:
                # Timeline zero is a hard boundary for both expression jitter
                # and physical compensation.  Clamp before freezing note_off;
                # otherwise a negative opening jitter shortens the note and
                # makes compensation audit report a negative applied delay.
                derivation["时间边界"] = (
                    f"补偿前起点 {start_seconds:.6f}s 截断到 0"
                )
                start_seconds = 0.0

            boundary_resolved_start_seconds = start_seconds
            logical_gate_start_seconds = start_seconds
            requested_gate_seconds = sounding

            # 发音补偿只提前音头,不提前音尾。业界实践与这里的道理一致:补偿
            # 要解决的是"音头没落在拍上",而不是"这个音该早点结束";两端一起
            # 前移会连时值一起改掉,时值是谱面已经写明的东西。
            requested_release_seconds = (
                logical_gate_start_seconds + requested_gate_seconds
            )
            release_at = requested_release_seconds

            onset = (
                capability.onset_for(
                    articulation,
                    context=onset_context,
                    records=resolved_onsets[executor.executor_id],
                )
                if settings.physical
                else None
            )
            if onset is not None:
                compensation = onset.seconds
                logical_start = start_seconds
                shifted = start_seconds - compensation
                if shifted < 0.0:
                    warning = (
                        f"{capability.name} 的发音补偿把第 {note.bar} 小节的音推到了"
                        "负时间,已截断到 0;可给总谱开头留一个空小节"
                    )
                    plan_budget.charge_fragment(warning, framing_bytes=8)
                    warnings.append(warning)
                    scope: dict[str, Any] = {
                        "executor_id": executor.executor_id,
                        "part_id": executor.part_id,
                        "bar": note.bar,
                        "beat": note.beat,
                    }
                    if note.source_event_id is not None:
                        scope["event_id"] = note.source_event_id
                    advisories.append(
                        PerformanceAdvisory(
                            code="onset.compensation_clipped_at_zero",
                            level="warning",
                            basis="measurement",
                            confidence="high",
                            scope=scope,
                            message=warning,
                            evidence={
                                "instrument": capability.relative_path,
                                "requested_delay_seconds": round(
                                    compensation,
                                    9,
                                ),
                                "logical_start_seconds": round(
                                    logical_start,
                                    9,
                                ),
                                "clipped_delay_seconds": round(
                                    -shifted,
                                    9,
                                ),
                            },
                            suggestions=(
                                "在总谱开头预留空拍或空小节后重新自检。",
                                "若截断后的起音正是创作意图，可保留并试听确认。",
                            ),
                        )
                    )
                    shifted = 0.0
                derivation["发音补偿"] = f"提前 {compensation * 1000.0:.0f}ms"
                applied = logical_start - shifted
                derivation["发音补偿审计"] = {
                    "status": "applied",
                    "context": onset_context,
                    "onset_overlap_policy": capability.onset_overlap_policy,
                    "final_articulation": onset.articulation,
                    "anchor": onset.anchor,
                    "logical_start_seconds": round(logical_start, 9),
                    "scheduled_start_seconds": round(shifted, 9),
                    "release_seconds": round(release_at, 9),
                    "requested_delay_seconds": round(compensation, 9),
                    "applied_delay_seconds": round(applied, 9),
                    "clipped_delay_seconds": round(
                        max(0.0, compensation - applied), 9
                    ),
                    "evidence": onset.evidence.to_dict(),
                }
                start_seconds = shifted
            elif (
                settings.physical
                and executor.executor_id in onset_configuration_mismatches
            ):
                derivation["发音补偿审计"] = {
                    "status": "not_applied_runtime_configuration_mismatch",
                    "context": onset_context,
                    "onset_overlap_policy": capability.onset_overlap_policy,
                    "final_articulation": (
                        articulation
                        if articulation is not None
                        else "__default__"
                    ),
                    "logical_start_seconds": round(start_seconds, 9),
                    "scheduled_start_seconds": round(start_seconds, 9),
                    "release_seconds": round(release_at, 9),
                    "requested_delay_seconds": 0.0,
                    "applied_delay_seconds": 0.0,
                    "clipped_delay_seconds": 0.0,
                    "onset_affecting_overrides": list(
                        onset_configuration_mismatches[executor.executor_id]
                    ),
                }
            elif settings.physical and onset_context != "isolated_attack":
                # An isolated attack annotation says nothing about a connected
                # transition.  Keep that distinction visible when relevant,
                # but never turn it into a guessed delay.
                isolated = capability.onset_for(
                    articulation,
                    context="isolated_attack",
                    records=resolved_onsets[executor.executor_id],
                )
                if isolated is not None:
                    derivation["发音补偿审计"] = {
                        "status": "not_applied_unapproved_context",
                        "context": onset_context,
                        "onset_overlap_policy": capability.onset_overlap_policy,
                        "final_articulation": isolated.articulation,
                        "logical_start_seconds": round(start_seconds, 9),
                        "scheduled_start_seconds": round(start_seconds, 9),
                        "release_seconds": round(release_at, 9),
                        "requested_delay_seconds": 0.0,
                        "applied_delay_seconds": 0.0,
                        "clipped_delay_seconds": 0.0,
                        "available_evidence": isolated.evidence.to_dict(),
                    }

            if executor.dynamic_compression > 0.0:
                # 实测钢琴 v=0.2→0.9 跨越 32 dB,弦乐只有约 10 dB。弱奏段落
                # 钢琴因此被弦乐压住,强奏段落才浮出来,整首曲子的平衡是漂移
                # 的。把力度往上端收拢可以抬起弱奏而几乎不动强奏,这正是真
                # 指挥在做的补偿。
                before = velocity
                velocity = _DYNAMIC_ANCHOR + (velocity - _DYNAMIC_ANCHOR) * (
                    1.0 - executor.dynamic_compression
                )
                derivation["动态压缩"] = f"{before:.3f} → {velocity:.3f}"

            start_seconds = max(0.0, start_seconds)
            velocity = min(1.0, max(0.01, velocity))
            release_velocity: float | None = None
            if note_realization is not None:
                velocity_resolution = resolve_numeric_override(
                    "velocity",
                    velocity,
                    note_realization.velocity,
                    path=(
                        "realization.note_overrides"
                        f"[{note_realization.event_id!r}].velocity"
                    ),
                )
                if velocity_resolution.evidence is not None:
                    assert velocity_resolution.value is not None
                    velocity_override = note_realization.velocity
                    assert velocity_override is not None
                    if velocity_override.value_policy == "exact":
                        backend_velocity = capability.require_note_velocity(
                            velocity_resolution.value
                        )
                    elif velocity_override.value_policy == "adapt":
                        backend_velocity = capability.adapt_note_velocity(
                            velocity_resolution.value
                        )
                    else:
                        raise AssertionError(
                            "unsupported note velocity value policy"
                        )
                    if (
                        backend_velocity.semantic_fidelity
                        in {"approximated", "ignored"}
                        and velocity_override.semantic_policy
                        != "approximate"
                    ):
                        raise ValueError(
                            "realization note "
                            f"{note_realization.event_id!r} requires "
                            "semantic_policy='approximate' for note velocity "
                            f"on {capability.name}: "
                            f"{backend_velocity.approximation_reason}"
                        )
                    velocity = backend_velocity.resolved_value
                    velocity_evidence = dict(
                        velocity_resolution.evidence
                    )
                    velocity_evidence["execution_resolution"] = (
                        backend_velocity.to_dict()
                    )
                    realization_evidence["velocity"] = velocity_evidence
                release_resolution = resolve_numeric_override(
                    "release_velocity",
                    None,
                    note_realization.release_velocity,
                    path=(
                        "realization.note_overrides"
                        f"[{note_realization.event_id!r}].release_velocity"
                    ),
                )
                release_velocity = release_resolution.value
                if release_resolution.evidence is not None:
                    release_evidence = dict(release_resolution.evidence)
                    release_evidence["capability_source"] = (
                        capability.release_velocity_source
                    )
                    release_evidence.update(
                        {
                            "requested_value": release_resolution.value,
                            "execution_resolved_value": (
                                release_resolution.value
                            ),
                            "fidelity": "native",
                            "semantic_fidelity": "native",
                            "adapted": False,
                        }
                    )
                    realization_evidence["release_velocity"] = release_evidence

            if realization_evidence:
                scheduled_start_before_grid_seconds = start_seconds
                requested_start_frame = start_seconds * score.sample_rate
                if not math.isfinite(requested_start_frame):
                    raise ResourceLimitError(
                        "plan.note_time_not_finite",
                        "resolved note onset exceeds the finite sample "
                        "timeline",
                    )
                start_sample = max(0, round(requested_start_frame))
                start_seconds = start_sample / score.sample_rate
                scheduler_evidence: dict[str, Any] = {
                    "sample_rate": score.sample_rate,
                    "requested_start_seconds": (
                        scheduled_start_before_grid_seconds
                    ),
                    "resolved_start_sample": start_sample,
                    "resolved_start_seconds": start_seconds,
                }
                timing_override = (
                    note_realization.timing_offset_ms
                    if note_realization is not None
                    else None
                )
                if timing_override is not None and not timing_override.is_noop:
                    boundary_adapted = not math.isclose(
                        requested_logical_start_seconds,
                        boundary_resolved_start_seconds,
                        rel_tol=0.0,
                        abs_tol=1.0e-12,
                    )
                    grid_adapted = not math.isclose(
                        scheduled_start_before_grid_seconds,
                        start_seconds,
                        rel_tol=0.0,
                        abs_tol=1.0e-12,
                    )
                    if (
                        boundary_adapted or grid_adapted
                    ) and timing_override.value_policy == "exact":
                        raise ValueError(
                            "realization.note_overrides"
                            f"[{note.source_event_id!r}].timing_offset_ms "
                            "requested exact sample timing, but the timeline "
                            "boundary or sample grid requires adaptation"
                        )
                    scheduler_evidence["timing_resolution"] = {
                        "value_policy": timing_override.value_policy,
                        "requested_seconds": requested_logical_start_seconds,
                        "boundary_resolved_seconds": (
                            boundary_resolved_start_seconds
                        ),
                        "scheduled_before_grid_seconds": (
                            scheduled_start_before_grid_seconds
                        ),
                        "resolved_seconds": start_seconds,
                        "boundary_adapted": boundary_adapted,
                        "grid_adapted": grid_adapted,
                        "adapted": boundary_adapted or grid_adapted,
                    }
                realization_evidence["scheduler"] = scheduler_evidence

            events = buckets[executor.executor_id]
            logical_note_onsets[executor.executor_id].append(
                (notated_start_seconds, start_seconds)
            )
            articulation_event = (
                {"type": "articulation", "name": articulation}
                if articulation
                else None
            )
            if articulation_event is not None:
                plan_budget.charge_fragment(
                    {
                        "time": round(start_seconds, 9),
                        **articulation_event,
                    },
                    framing_bytes=32,
                )
            events.append(
                {
                    "time": start_seconds,
                    "kind": 0,
                    "event": articulation_event,
                }
            )
            note_on_event: dict[str, Any] = {
                "type": "note_on",
                "midi_note": played_midi,
                "velocity": velocity,
            }
            if note.source_event_id is not None:
                note_on_event["source_event_id"] = note.source_event_id
            plan_budget.charge_fragment(
                {
                    "time": round(start_seconds, 9),
                    **note_on_event,
                    "note_id": 0,
                },
                framing_bytes=32,
            )
            events.append(
                {"time": start_seconds, "kind": 1, "event": note_on_event}
            )
            # 音尾用补偿前的时刻,保证谱面时值不被发音补偿一起挪走。
            release_at = max(release_at, start_seconds + 0.02)
            # A release-gate lane is sampled at the interpreted key-up, not at
            # the score's unshaped note end.  Gate ratio, articulation length
            # and timing realization all define when that musical release
            # occurs; onset compensation and sample-grid freezing are the
            # later physical scheduling stages.
            logical_release_seconds = requested_release_seconds
            if realization_evidence:
                requested_release_frame = release_at * score.sample_rate
                if not math.isfinite(requested_release_frame):
                    raise ResourceLimitError(
                        "plan.note_time_not_finite",
                        "resolved note release exceeds the finite sample "
                        "timeline",
                    )
                release_sample = max(
                    round(requested_release_frame),
                    round(start_seconds * score.sample_rate) + 1,
                )
                release_at = release_sample / score.sample_rate
                logical_gate_start_sample = max(
                    0,
                    round(
                        logical_gate_start_seconds * score.sample_rate
                    ),
                )
                resolved_logical_gate_start = (
                    logical_gate_start_sample / score.sample_rate
                )
                resolved_gate_seconds = max(
                    0.0,
                    release_at - resolved_logical_gate_start,
                )
                gate_grid = _enforce_sample_value_policy(
                    (
                        note_realization.gate_ratio
                        if note_realization is not None
                        else None
                    ),
                    requested_gate_seconds,
                    resolved_gate_seconds,
                    path=(
                        "realization.note_overrides"
                        f"[{note.source_event_id!r}].gate_ratio"
                    ),
                )
                realization_evidence["scheduler"].update(
                    {
                        "requested_release_seconds": requested_release_seconds,
                        "resolved_release_sample": release_sample,
                        "resolved_release_seconds": release_at,
                        "resolved_logical_gate_start_sample": (
                            logical_gate_start_sample
                        ),
                        "resolved_logical_gate_start_seconds": (
                            resolved_logical_gate_start
                        ),
                        "requested_gate_seconds": requested_gate_seconds,
                        "resolved_gate_seconds": resolved_gate_seconds,
                        "effective_gate_ratio": (
                            resolved_gate_seconds
                            / notated_duration_seconds
                        ),
                    }
                )
                if gate_grid is not None:
                    realization_evidence["scheduler"][
                        "gate_resolution"
                    ] = gate_grid
                derivation["realization"] = realization_evidence
            effective_note_end = release_at + score.tail_seconds
            if (
                not math.isfinite(effective_note_end)
                or effective_note_end > project_limits.max_plan_seconds
            ):
                raise ResourceLimitError(
                    "plan.note_time_too_late",
                    f"resolved score part {executor.part_id!r} note "
                    f"{(note.source_event_id if note.source_event_id is not None else order)!r} reaches "
                    f"{effective_note_end:g}s including tail, exceeding "
                    f"plan limit {project_limits.max_plan_seconds}s; raise "
                    "TIANLAI_MAX_PLAN_SECONDS deliberately if trusted",
                    actual=effective_note_end,
                    limit=project_limits.max_plan_seconds,
                )
            note_off_event: dict[str, Any] = {"type": "note_off"}
            logical_note_releases[executor.executor_id].append(
                (logical_release_seconds, release_at)
            )
            if release_velocity is not None:
                note_off_event["release_velocity"] = release_velocity
            if note.source_event_id is not None:
                note_off_event["source_event_id"] = note.source_event_id
            plan_budget.charge_fragment(
                {
                    "time": round(release_at, 9),
                    **note_off_event,
                    "note_id": 0,
                },
                framing_bytes=32,
            )
            events.append(
                {"time": release_at, "kind": 2, "event": note_off_event}
            )
            last_time = max(last_time, release_at)

            derivation["奏法"] = articulation_reason
            trace_entry: dict[str, Any] = {
                "小节": note.bar,
                "拍": note.beat,
                "音": pitch_name(played_midi),
                "时间": round(start_seconds, 6),
                "时长": round(release_at - start_seconds, 6),
                "力度": round(velocity, 4),
                "奏法": articulation,
                "推导": derivation,
            }
            if note.source_event_id is not None:
                trace_entry["source_event_id"] = note.source_event_id
            plan_budget.charge_fragment(trace_entry, framing_bytes=32)
            traces[executor.executor_id].append(trace_entry)

    if unapplied_note_realizations:
        unresolved = ", ".join(sorted(unapplied_note_realizations))
        raise ValueError(
            "realization note overrides were not routed into the performance "
            f"plan (the score part may be dropped): {unresolved}"
        )

    for executor_id, automatic_count in automatic_articulation_counts.items():
        total = executor_note_counts[executor_id]
        if total < 8 or automatic_count / total < 0.8:
            continue
        executor = by_id[executor_id]
        warning = (
            f"{executor.executor_id}({executor.capability.name}) 的显式时值奏法合同"
            f"覆盖 {automatic_count}/{total} 个音符"
            f"({automatic_count / total:.1%});这是诊断提示，请确认该轨确实需要"
            "近全程自动换奏法，创作者可在 roster 设 articulation_auto=false"
        )
        plan_budget.charge_fragment(warning, framing_bytes=8)
        warnings.append(warning)
        advisories.append(
            PerformanceAdvisory(
                code="articulation.auto_dominant",
                level="warning",
                basis="measurement",
                confidence="high",
                scope={
                    "executor_id": executor.executor_id,
                    "part_id": executor.part_id,
                    "instrument": executor.capability.relative_path,
                },
                message=(
                    "时值奏法合同覆盖了该执行器的大部分音符；这可能是预期的"
                    "演奏设计，也可能与原谱的奏法意图不一致。"
                ),
                evidence={
                    "automatic_articulation_count": automatic_count,
                    "note_count": total,
                    "ratio": round(automatic_count / total, 6),
                },
                suggestions=(
                    "保留当前设置并试听自动奏法是否符合乐句意图。",
                    "在 roster 设置 articulation_auto=false 做一次 A/B。",
                    "在关键音符上显式写入奏法。",
                ),
            )
        )

    part_event_documents: dict[str, list[dict[str, Any]]] = {}
    part_control_traces: dict[str, tuple[dict[str, Any], ...]] = {}
    latest_performance_time = last_time
    _validate_control_articulation_applicability(
        compiled_controls,
        by_id,
        resolved_articulations,
    )
    for executor in roster.executors:
        note_events = _pair_note_ids(buckets[executor.executor_id])
        performance_events, control_trace, control_last_time = (
            _merge_compiled_control_events(
                note_events,
                compiled_controls.get(executor.executor_id, ()),
                sample_rate=score.sample_rate,
                logical_note_onsets=tuple(
                    logical_note_onsets[executor.executor_id]
                ),
                logical_note_releases=tuple(
                    logical_note_releases[executor.executor_id]
                ),
                control_event_limit=(
                    max(
                        1,
                        realization_limits.max_score_json_bytes // 512,
                    )
                    if realization_limits is not None
                    else None
                ),
                plan_budget=plan_budget,
            )
        )
        part_event_documents[executor.executor_id] = performance_events
        part_control_traces[executor.executor_id] = control_trace
        latest_performance_time = max(
            latest_performance_time,
            control_last_time,
        )

    duration = latest_performance_time + score.tail_seconds
    if (
        not math.isfinite(duration)
        or duration > project_limits.max_plan_seconds
    ):
        raise ResourceLimitError(
            "plan.duration_too_long",
            f"resolved performance plan reaches {duration:g}s including "
            f"tail, exceeding plan limit {project_limits.max_plan_seconds}s; "
            "raise TIANLAI_MAX_PLAN_SECONDS deliberately if trusted",
            actual=duration,
            limit=project_limits.max_plan_seconds,
        )
    if realization_limits is not None:
        aggregate_event_count = sum(
            len(events) for events in part_event_documents.values()
        )
        aggregate_event_limit = performance_event_limit(
            realization_limits
        )
        if aggregate_event_count > aggregate_event_limit:
            raise ResourceLimitError(
                "realization.too_many_compiled_events",
                "compiled performance plan contains "
                f"{aggregate_event_count} events, exceeding aggregate limit "
                f"{aggregate_event_limit}; raise TIANLAI_MAX_NOTES "
                "deliberately if this project is trusted",
                actual=aggregate_event_count,
                limit=aggregate_event_limit,
            )
    parts: list[PlanPart] = []
    for executor in roster.executors:
        performance_document = {
            "sample_rate": score.sample_rate,
            "channels": 2,
            "duration_seconds": round(duration, 6),
            "tuning": score.tuning,
            "events": part_event_documents[executor.executor_id],
        }
        if realization_limits is not None:
            validate_performance_document_resource_limits(
                performance_document,
                realization_limits,
            )
        gain_envelope = _compile_gain_envelope(executor, score)
        plan_budget.charge_fragment(
            executor.to_dict(),
            framing_bytes=128,
        )
        plan_budget.charge_fragment(
            {
                "sample_rate": score.sample_rate,
                "channels": 2,
                "duration_seconds": round(duration, 6),
                "tuning": score.tuning,
            },
            framing_bytes=128,
        )
        for point in gain_envelope:
            plan_budget.charge_fragment(
                point.to_dict(executor.gain_db),
                framing_bytes=16,
            )
        parts.append(
            PlanPart(
                executor=executor,
                performance=performance_document,
                trace=tuple(traces[executor.executor_id]),
                gain_envelope=gain_envelope,
                control_trace=part_control_traces[executor.executor_id],
            )
        )

    realization_binding: dict[str, Any] | None = None
    if active_realization is not None:
        realization_binding = {
            "kind": active_realization.kind,
            "schema_version": active_realization.schema_version,
            "score_sha256": active_realization.score_sha256,
            "canonical_sha256": realization_canonical_sha256,
            "defaults_profile": active_realization.defaults_profile,
            "mode": active_realization.mode,
        }
        plan_budget.charge_fragment(
            realization_binding,
            framing_bytes=32,
        )

    plan = PerformancePlan(
        title=score.title,
        sample_rate=score.sample_rate,
        duration_seconds=duration,
        expression=settings,
        roster_name=roster.name,
        parts=tuple(parts),
        collaboration=roster.collaboration,
        warnings=tuple(dict.fromkeys(warnings)),
        realization=realization_binding,
        advisories=tuple(advisories),
    )
    plan_budget.validate_final(plan.to_dict())
    return plan


def _pair_note_ids(raw_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assign note ids so each note_off closes exactly its own note_on.

    Events were emitted as ordered triples, so the n-th note_on and the n-th
    note_off in emission order belong together even after the list is sorted
    by time.  Pairing by emission index avoids the classic bug where two
    overlapping notes of different lengths swap their releases.
    """

    def ordering_key(index: int) -> tuple[float, int, int, int]:
        item = raw_events[index]
        kind = item["kind"]
        if kind == 0:
            # Articulation and the immediately following note_on are one
            # atomic attack.  Sorting every articulation before every note at
            # the same time makes a mixed-articulation chord play all notes
            # with the final chord tone's articulation.
            return item["time"], 0, index, 0
        if kind == 1:
            return item["time"], 0, index - 1, 1
        # Preserve the established attack-before-release ordering at an equal
        # timestamp while retaining each note's independent id.
        return item["time"], 1, index, 0

    ordering = sorted(range(len(raw_events)), key=ordering_key)
    identifiers: dict[int, int] = {}
    next_id = 1
    for index, item in enumerate(raw_events):
        payload = item["event"]
        if payload is not None and payload["type"] == "note_on":
            identifiers[index] = next_id
            # note_off 紧随其后一位发出,直接沿用同一个编号。
            identifiers[index + 1] = next_id
            next_id += 1

    events: list[dict[str, Any]] = []
    current_articulation: str | None = None
    for index in ordering:
        item = raw_events[index]
        payload = item["event"]
        if payload is None:
            continue
        if payload["type"] == "articulation":
            # 每个音都发一次奏法事件会让计划文件难读,也让后端做无谓的切换;
            # 只在真正改变时发,语义完全等价。
            if payload["name"] == current_articulation:
                continue
            current_articulation = payload["name"]
        entry: dict[str, Any] = {"time": round(item["time"], 9), **payload}
        if payload["type"] in ("note_on", "note_off"):
            entry["note_id"] = identifiers[index]
        events.append(entry)
    return events
