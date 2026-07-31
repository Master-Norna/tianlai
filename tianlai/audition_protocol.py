"""Deterministic full-range audition scores.

The listening sweep is deliberately simpler than a musical excerpt: normally
use the default articulation and play every *declared legal integer MIDI key*
once in ascending order.  It is a compact mapping/polyphony stress scan, not
an isolated timbre verdict: note-on/note-off lifetimes do not overlap, but a
backend's audio release may continue into later chromatic notes.  A declared
hole between playable spans remains silent and is reported as a gap; the
protocol never fills it by guessing.  Long-release and mapped-percussion
exceptions are explicit in each plan.

Unpitched instruments follow the same rule when they expose a trigger-key
range.  Backends that intentionally ignore incoming pitch must instead expose
a fixed key, a backend-owned fixed source key, or match one of the explicit
single-trigger exceptions below.  This keeps a missing range from silently
turning into an arbitrary C4.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any

from .capability import InstrumentCapability, read_capability


PROTOCOL_ID = "full-range-chromatic-ascending-v1"
ISOLATED_PROTOCOL_ID = "full-range-chromatic-isolated-v1"
SAMPLE_RATE = 48_000
CHANNELS = 2
A4_HZ = 440.0
VELOCITY = 0.72
NOTE_DURATION_SECONDS = 0.48
INTER_NOTE_GAP_SECONDS = 0.12
START_SECONDS = 0.25
TAIL_SECONDS = 1.5

_SLOW_PAD_PATCHES = frozenset(
    {
        "halo_pad",
        "choir_pad",
        "broad_pad",
        "metallic_pad",
        "sweep_pad",
        "warm_pad",
    }
)

_SCAN_ARTICULATION_OVERRIDES: dict[tuple[str, str | None], tuple[str, str]] = {
    (
        "vpo_harp",
        None,
    ): (
        "dampened",
        "竖琴全音域扫描使用 dampened，避免 30 秒开放尾音叠成音簇",
    ),
    (
        "vpo_percussion",
        "vcsl_tubular_bells_2",
    ): (
        "damped",
        "管钟全音域扫描使用 damped，避免 30 秒开放尾音叠成音簇",
    ),
}

# ``procedural_sfx`` consumes note lifetime, velocity and controls but
# intentionally ignores pitch.  MIDI 60 is therefore a protocol control token,
# not an asserted acoustic pitch or an inferred playable range.
_SINGLE_TRIGGER_BACKEND_EXCEPTIONS: dict[str, tuple[int, str]] = {
    "procedural_sfx": (
        60,
        "procedural_sfx 后端明确忽略音高；MIDI 60 仅作单次生命周期触发键",
    ),
}


@dataclass(frozen=True, slots=True)
class AuditionStrike:
    """One note/trigger in the serial audition schedule."""

    midi_key: int
    articulation: str | None
    duration_seconds: float = NOTE_DURATION_SECONDS
    gap_seconds: float = INTER_NOTE_GAP_SECONDS


@dataclass(frozen=True, slots=True)
class FullRangeAudition:
    """One validated full-range sweep and its machine-readable provenance."""

    instrument: str
    articulation: str | None
    pitch_semantics: str
    range_source: str
    declared_ranges: tuple[tuple[int, int], ...]
    gaps: tuple[tuple[int, int], ...]
    sequence: tuple[AuditionStrike, ...]
    tail_seconds: float
    exception: str | None
    document: dict[str, Any]

    @property
    def keys(self) -> tuple[int, ...]:
        return tuple(strike.midi_key for strike in self.sequence)

    @property
    def unique_keys(self) -> tuple[int, ...]:
        return tuple(sorted(set(self.keys)))

    @property
    def coverage(self) -> list[str]:
        spans = "、".join(
            f"MIDI {low}" if low == high else f"MIDI {low}-{high}"
            for low, high in self.declared_ranges
        )
        duration_values = {
            round(strike.duration_seconds, 6) for strike in self.sequence
        }
        gap_values = {
            round(strike.gap_seconds, 6) for strike in self.sequence
        }
        timing = (
            f"固定力度 {VELOCITY:g}、时值 {NOTE_DURATION_SECONDS:g}s、"
            f"音间隔 {INTER_NOTE_GAP_SECONDS:g}s；note 事件不重叠，"
            "后端声音释音可能跨入后续音（压力扫描）"
        )
        if (
            duration_values != {NOTE_DURATION_SECONDS}
            or gap_values != {INTER_NOTE_GAP_SECONDS}
            or self.tail_seconds != TAIL_SECONDS
        ):
            timing = (
                f"固定力度 {VELOCITY:g}、常规时值 "
                f"{NOTE_DURATION_SECONDS:g}s、音间隔 "
                f"{INTER_NOTE_GAP_SECONDS:g}s；本乐器按协议使用独立 "
                "gate/gap/tail；note 事件不重叠，是否隔离声音尾部"
                "以本乐器协议例外说明为准"
            )
        result = [
            (
                f"全声明合法键升序：{spans}（{len(self.unique_keys)} 键，"
                f"{len(self.sequence)} 次触发）"
            ),
            timing,
        ]
        if self.gaps:
            holes = "、".join(
                f"MIDI {low}" if low == high else f"MIDI {low}-{high}"
                for low, high in self.gaps
            )
            result.append(f"声明音域空洞未触发：{holes}")
        if self.exception is not None:
            result.append(f"协议例外：{self.exception}")
        return result

    def metadata(self) -> dict[str, Any]:
        """Return the protocol-specific part of a batch manifest entry."""

        return {
            "instrument": self.instrument,
            "articulation": self.articulation,
            "pitch_semantics": self.pitch_semantics,
            "range_source": self.range_source,
            "declared_ranges": [list(span) for span in self.declared_ranges],
            "gaps": [list(span) for span in self.gaps],
            "key_count": len(self.unique_keys),
            "event_count": len(self.sequence),
            "keys": list(self.keys),
            "articulations": [
                strike.articulation for strike in self.sequence
            ],
            "gate_seconds": [
                strike.duration_seconds for strike in self.sequence
            ],
            "gap_seconds": [
                strike.gap_seconds for strike in self.sequence
            ],
            "tail_seconds": self.tail_seconds,
            "exception": self.exception,
        }


def _integer_ranges(
    ranges: tuple[tuple[float, float], ...],
    *,
    field: str,
) -> tuple[tuple[int, int], ...]:
    if not ranges:
        raise ValueError(f"{field} 没有声明任何合法键位")
    converted: list[tuple[int, int]] = []
    for index, (raw_low, raw_high) in enumerate(ranges):
        low = float(raw_low)
        high = float(raw_high)
        if not math.isfinite(low) or not math.isfinite(high):
            raise ValueError(f"{field}[{index}] 边界必须是有限数")
        if not low.is_integer() or not high.is_integer():
            raise ValueError(
                f"{field}[{index}]={low:g}..{high:g} 不是整数半音边界；"
                "全音域试听拒绝静默取整"
            )
        low_key = int(low)
        high_key = int(high)
        if not 0 <= low_key <= high_key <= 127:
            raise ValueError(
                f"{field}[{index}] 必须满足 0 <= low <= high <= 127"
            )
        converted.append((low_key, high_key))
    return tuple(converted)


def _gaps(ranges: tuple[tuple[int, int], ...]) -> tuple[tuple[int, int], ...]:
    result: list[tuple[int, int]] = []
    for previous, current in zip(ranges, ranges[1:]):
        low = previous[1] + 1
        high = current[0] - 1
        if low <= high:
            result.append((low, high))
    return tuple(result)


def _ranges_from_keys(keys: tuple[int, ...]) -> tuple[tuple[float, float], ...]:
    unique = sorted(set(keys))
    if not unique:
        return ()
    result: list[tuple[float, float]] = []
    low = high = unique[0]
    for key in unique[1:]:
        if key == high + 1:
            high = key
            continue
        result.append((float(low), float(high)))
        low = high = key
    result.append((float(low), float(high)))
    return tuple(result)


def _extended_unpitched_duration(articulation: str) -> float:
    if "crescendo_long" in articulation:
        return 8.0
    if "crescendo_medium" in articulation:
        return 5.0
    if "crescendo_short" in articulation:
        return 3.0
    if "roll" in articulation:
        return 2.4
    return NOTE_DURATION_SECONDS


def _positive_timing(value: object, *, field: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{field} 必须是有限正数")
    return number


def _parameters(manifest: dict[str, Any], *, field: str) -> dict[str, Any]:
    raw = manifest.get("parameters", {})
    if not isinstance(raw, dict):
        raise ValueError(f"{field}.parameters 必须是 object")
    return raw


def _procedural_sfx_timing(
    manifest: dict[str, Any],
    articulation: str | None,
    midi_key: int,
) -> tuple[tuple[AuditionStrike, ...], float, str]:
    from .procedural_sfx import SFX_PROFILES

    profile_name = str(manifest.get("profile", ""))
    try:
        base = SFX_PROFILES[profile_name]
    except KeyError as error:
        choices = "、".join(sorted(SFX_PROFILES))
        raise ValueError(
            f"procedural_sfx 未知 profile {profile_name!r}；可选：{choices}"
        ) from error
    parameters = _parameters(manifest, field="procedural_sfx")
    attack = _positive_timing(
        parameters.get("attack_seconds", base.attack_seconds),
        field=f"procedural_sfx {profile_name} attack_seconds",
    )
    release = _positive_timing(
        parameters.get("release_seconds", base.release_seconds),
        field=f"procedural_sfx {profile_name} release_seconds",
    )
    raw_one_shot = parameters.get(
        "one_shot_seconds",
        base.one_shot_seconds,
    )
    one_shot = (
        None
        if raw_one_shot is None
        else _positive_timing(
            raw_one_shot,
            field=f"procedural_sfx {profile_name} one_shot_seconds",
        )
    )
    if one_shot is not None:
        gate = round(one_shot, 6)
        lifecycle = f"one-shot 完整持有 {one_shot:g}s"
    else:
        gate = round(attack + 1.2, 6)
        lifecycle = f"持有 attack {attack:g}s + 稳态 1.2s"
    tail = round(max(TAIL_SECONDS, release + 0.25), 6)
    exception = (
        f"procedural_sfx/{profile_name} {lifecycle}；"
        f"release {release:g}s，tail {tail:g}s"
    )
    return (
        (AuditionStrike(midi_key, articulation, gate),),
        tail,
        exception,
    )


def _slow_pad_timing(
    manifest: dict[str, Any],
    articulation: str | None,
    keys: tuple[int, ...],
) -> tuple[tuple[AuditionStrike, ...], float, str] | None:
    if str(manifest.get("type", "")) != "synthesizer":
        return None
    patch = str(manifest.get("patch", ""))
    if patch not in _SLOW_PAD_PATCHES:
        return None
    from .synthesizer import PATCH_PROFILES

    try:
        base = PATCH_PROFILES[patch]
    except KeyError as error:
        raise ValueError(f"慢铺底 patch 不存在：{patch!r}") from error
    parameters = _parameters(manifest, field=f"synthesizer/{patch}")
    attack = _positive_timing(
        parameters.get("attack_seconds", base.attack_seconds),
        field=f"synthesizer/{patch} attack_seconds",
    )
    release = _positive_timing(
        parameters.get("release_seconds", base.release_seconds),
        field=f"synthesizer/{patch} release_seconds",
    )
    gate = round(attack + 0.3, 6)
    gap = round(release + 0.12, 6)
    tail = round(max(TAIL_SECONDS, release + 0.12), 6)
    sequence = tuple(
        AuditionStrike(key, articulation, gate, gap) for key in keys
    )
    exception = (
        f"慢铺底 {patch} 仍逐半音；每音 gate=attack {attack:g}+0.3="
        f"{gate:g}s，gap=release {release:g}+0.12={gap:g}s，"
        f"tail={tail:g}s，以隔离相邻音"
    )
    return sequence, tail, exception


def _vpo_unpitched_sequence(
    manifest: dict[str, Any],
    capability: InstrumentCapability,
) -> tuple[AuditionStrike, ...] | None:
    """Resolve every unpitched VPO articulation/source key in key order."""

    if str(manifest.get("type", "")) != "vpo_percussion":
        return None
    from .vpo_percussion import PERCUSSION_PROFILES

    profile_name = str(manifest.get("profile", ""))
    try:
        profile = PERCUSSION_PROFILES[profile_name]
    except KeyError as error:
        raise ValueError(
            f"vpo_percussion 未知 profile {profile_name!r}"
        ) from error
    if profile.pitched:
        return None
    sequence: list[AuditionStrike] = []
    for articulation, specification in profile.articulations.items():
        key = specification.fixed_source_key
        if key is None or not float(key).is_integer():
            raise ValueError(
                f"vpo_percussion {profile_name!r}/{articulation!r} "
                "没有整数 fixed_source_key"
            )
        sequence.append(
            AuditionStrike(
                int(key),
                articulation,
                _extended_unpitched_duration(articulation),
            )
        )
    sequence.sort(key=lambda strike: (strike.midi_key, strike.articulation or ""))
    return tuple(sequence)


def _unpitched_ranges(
    manifest: dict[str, Any],
    capability: InstrumentCapability,
) -> tuple[
    tuple[tuple[float, float], ...],
    str,
    str | None,
    tuple[AuditionStrike, ...] | None,
]:
    declared = capability.ranges_for(capability.default_articulation)
    if declared:
        articulation = capability.default_articulation
        source = (
            f"capability.articulation_ranges:{articulation}"
            if articulation is not None
            else "capability.global_ranges"
        )
        return (
            declared,
            source,
            None,
            None,
        )

    if capability.fixed_midi_note is not None:
        key = float(capability.fixed_midi_note)
        sequence = tuple(
            AuditionStrike(int(key), capability.default_articulation)
            for _ in range(4)
        )
        return (
            ((key, key),),
            "manifest.fixed_midi_note",
            "固定打击键重复 4 次，以覆盖轮替/重复触发稳定性",
            sequence,
        )

    fixed_source = manifest.get("fixed_source_midi_note")
    if fixed_source is not None:
        key = float(fixed_source)
        sequence = tuple(
            AuditionStrike(int(key), capability.default_articulation)
            for _ in range(4)
        )
        return (
            ((key, key),),
            "manifest.fixed_source_midi_note",
            "固定牛铃源键重复 4 次，以覆盖真实轮替层",
            sequence,
        )

    vpo_sequence = _vpo_unpitched_sequence(manifest, capability)
    if vpo_sequence is not None:
        vpo_keys = tuple(strike.midi_key for strike in vpo_sequence)
        return (
            _ranges_from_keys(vpo_keys),
            "vpo_percussion.all_articulation_fixed_source_keys",
            (
                "VPO 无音高打击按全部奏法的固定源键升序；"
                "roll/crescendo 使用延长时值"
            ),
            vpo_sequence,
        )

    instrument_type = str(manifest.get("type", ""))
    explicit = _SINGLE_TRIGGER_BACKEND_EXCEPTIONS.get(instrument_type)
    if explicit is not None:
        key, reason = explicit
        return (
            ((float(key), float(key)),),
            f"explicit_backend_exception:{instrument_type}",
            reason,
            None,
        )

    raise ValueError(
        "无固定音高乐器既未声明 playable range/fixed key，也没有可证明的"
        f"后端单触发例外：{capability.relative_path}"
    )


def _scan_articulation(
    manifest: dict[str, Any],
    capability: InstrumentCapability,
) -> tuple[str | None, str | None]:
    key = (
        str(manifest.get("type", "")),
        (
            str(manifest["profile"])
            if manifest.get("profile") is not None
            else None
        ),
    )
    override = _SCAN_ARTICULATION_OVERRIDES.get(key)
    if override is None:
        # Type-wide rules use ``None`` as their profile selector.
        override = _SCAN_ARTICULATION_OVERRIDES.get((key[0], None))
    if override is None:
        return capability.default_articulation, None
    articulation, reason = override
    if not capability.supports(articulation):
        raise ValueError(
            f"{capability.relative_path} 的扫描奏法例外 {articulation!r} "
            "不在能力合同中"
        )
    return articulation, reason


def _combine_exception(current: str | None, addition: str) -> str:
    return addition if current is None else f"{current}；{addition}"


def _reversed_cymbal_sequence(
    manifest: dict[str, Any],
    manifest_path: Path,
    keys: tuple[int, ...],
) -> tuple[AuditionStrike, ...]:
    report_name = str(manifest.get("resource_verification", "资源核验.json"))
    report_path = manifest_path.parent / report_name
    if not report_path.is_file():
        raise FileNotFoundError(
            f"反向镲全长试听需要资源报告中的 swell_seconds：{report_path}"
        )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    variants = report.get("variants")
    if not isinstance(variants, dict):
        raise ValueError(f"{report_path} 缺少 variants")
    sequence: list[AuditionStrike] = []
    for key in keys:
        raw = variants.get(str(key))
        if not isinstance(raw, dict):
            raise ValueError(f"{report_path} 缺少反向镲键 {key} 的资源记录")
        seconds = float(raw.get("swell_seconds", 0.0))
        if not math.isfinite(seconds) or seconds <= 0.0:
            raise ValueError(
                f"{report_path} 键 {key} 的 swell_seconds 必须是正数"
            )
        sequence.append(AuditionStrike(key, None, seconds))
    return tuple(sequence)


def _event_document(
    sequence: tuple[AuditionStrike, ...],
    *,
    tail_seconds: float,
) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    start = START_SECONDS
    current_articulation: str | None = None
    for index, strike in enumerate(sequence):
        articulation = strike.articulation
        if articulation is not None and articulation != current_articulation:
            events.append(
                {
                    "time": round(start, 6),
                    "type": "articulation",
                    "name": articulation,
                }
            )
            current_articulation = articulation
        note_id = index + 1
        stop = start + strike.duration_seconds
        events.extend(
            (
                {
                    "time": round(start, 6),
                    "type": "note_on",
                    "note_id": note_id,
                    "midi_note": strike.midi_key,
                    "velocity": VELOCITY,
                },
                {
                    "time": round(stop, 6),
                    "type": "note_off",
                    "note_id": note_id,
                    "release_velocity": 0.5,
                },
            )
        )
        start = stop + strike.gap_seconds
    return {
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "tail_seconds": tail_seconds,
        "tuning": {
            "temperament": "equal",
            "a4_hz": A4_HZ,
        },
        "events": events,
    }


def isolate_full_range_audition(
    plan: FullRangeAudition,
    *,
    gate_seconds: float,
    release_seconds: float,
    silence_seconds: float = 0.18,
) -> FullRangeAudition:
    """Return a timbre-review sweep whose release tails cannot cross notes.

    The regular ascending sweep intentionally acts as a compact stress test.
    Its event notes do not overlap, but an instrument's *audio* release can
    extend into later notes.  That is useful for finding polyphony pressure,
    yet it can disguise a healthy source as echo or turn adjacent chromatic
    notes into a beating cluster.

    This helper keeps the exact key/articulation coverage while giving every
    strike a minimum sounding gate and then reserving the declared release
    plus a short silence before the next onset.  ``release_seconds`` must be a
    measured/backend value chosen by the caller; guessing it from a manifest
    is unsafe because SFZ regions can override the manifest envelope.
    """

    gate = _positive_timing(gate_seconds, field="isolated gate_seconds")
    release = _positive_timing(
        release_seconds,
        field="isolated release_seconds",
    )
    silence = _positive_timing(
        silence_seconds,
        field="isolated silence_seconds",
    )
    isolated_gap = round(release + silence, 6)
    sequence = tuple(
        AuditionStrike(
            strike.midi_key,
            strike.articulation,
            max(strike.duration_seconds, gate),
            max(strike.gap_seconds, isolated_gap),
        )
        for strike in plan.sequence
    )
    tail_seconds = round(
        max(plan.tail_seconds, release + silence),
        6,
    )
    exception = _combine_exception(
        plan.exception,
        (
            "隔离音色复验："
            f"gate≥{gate:g}s，note_off 后为 release {release:g}s "
            f"+ 静音余量 {silence:g}s；相邻音的声音尾部不得交叠"
        ),
    )
    return FullRangeAudition(
        instrument=plan.instrument,
        articulation=plan.articulation,
        pitch_semantics=plan.pitch_semantics,
        range_source=plan.range_source,
        declared_ranges=plan.declared_ranges,
        gaps=plan.gaps,
        sequence=sequence,
        tail_seconds=tail_seconds,
        exception=exception,
        document=_event_document(
            sequence,
            tail_seconds=tail_seconds,
        ),
    )


def restrict_full_range_audition(
    plan: FullRangeAudition,
    *,
    ranges: tuple[tuple[int, int], ...],
    reason: str,
) -> FullRangeAudition:
    """Return a truthful subset sweep without mutating the source contract.

    Some instruments deliberately retain compatibility/extended mappings that
    are not part of the current high-fidelity review range.  A repair audition
    must not exercise those keys and then imply that they were approved, nor
    should it erase the compatibility range from the production manifest.
    This helper records the exact reviewed subset in both metadata and events.
    """

    selected_ranges = _integer_ranges(
        tuple((float(low), float(high)) for low, high in ranges),
        field="review ranges",
    )
    selected_keys = tuple(
        key
        for low, high in selected_ranges
        for key in range(low, high + 1)
    )
    if len(selected_keys) != len(set(selected_keys)):
        raise ValueError("review ranges overlap")
    if tuple(sorted(selected_ranges)) != selected_ranges:
        raise ValueError("review ranges must be declared in ascending order")
    available = set(plan.unique_keys)
    missing = sorted(set(selected_keys) - available)
    if missing:
        raise ValueError(
            "review ranges include keys outside the source audition: "
            + ", ".join(str(key) for key in missing)
        )
    selected = set(selected_keys)
    sequence = tuple(
        strike for strike in plan.sequence if strike.midi_key in selected
    )
    if not sequence:
        raise ValueError("review ranges select no audition strikes")
    explanation = str(reason).strip()
    if not explanation:
        raise ValueError("review range reason must not be empty")
    exception = _combine_exception(
        plan.exception,
        f"本次只复验声明子集：{explanation}",
    )
    return FullRangeAudition(
        instrument=plan.instrument,
        articulation=plan.articulation,
        pitch_semantics=plan.pitch_semantics,
        range_source=f"{plan.range_source};explicit_review_subset",
        declared_ranges=selected_ranges,
        gaps=_gaps(selected_ranges),
        sequence=sequence,
        tail_seconds=plan.tail_seconds,
        exception=exception,
        document=_event_document(
            sequence,
            tail_seconds=plan.tail_seconds,
        ),
    )


def build_full_range_audition(
    manifest_path: str | Path,
    *,
    instrument_root: str | Path,
) -> FullRangeAudition:
    """Build a fail-closed chromatic sweep from one instrument contract."""

    source = Path(manifest_path).resolve()
    root = Path(instrument_root).resolve()
    manifest = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"乐器清单根节点必须是 object：{source}")
    capability = read_capability(
        source,
        root=root,
        defer_onset_evidence=True,
    )

    sequence_override: tuple[AuditionStrike, ...] | None = None
    if capability.pitched:
        articulation, exception = _scan_articulation(manifest, capability)
        raw_ranges = capability.ranges_for(articulation)
        range_source = (
            f"capability.articulation_ranges:{articulation}"
            if articulation is not None
            else "capability.global_ranges"
        )
        pitch_semantics = "pitched_chromatic"
    else:
        articulation = capability.default_articulation
        (
            raw_ranges,
            range_source,
            exception,
            sequence_override,
        ) = _unpitched_ranges(
            manifest,
            capability,
        )
        pitch_semantics = (
            "unpitched_trigger_map"
            if len(raw_ranges) > 1
            or any(float(low) != float(high) for low, high in raw_ranges)
            else "unpitched_single_trigger"
        )

    ranges = _integer_ranges(
        raw_ranges,
        field=f"{capability.relative_path} 的合法键位",
    )
    keys = tuple(
        key
        for low, high in ranges
        for key in range(low, high + 1)
    )
    if len(keys) != len(set(keys)):
        raise ValueError(
            f"{capability.relative_path} 的合法键位区间重叠，无法生成唯一升序"
        )
    if tuple(sorted(keys)) != keys:
        raise ValueError(
            f"{capability.relative_path} 的合法键位没有严格升序声明"
        )

    tail_seconds = TAIL_SECONDS
    sequence = sequence_override or tuple(
        AuditionStrike(key, articulation) for key in keys
    )
    instrument_type = str(manifest.get("type", ""))
    if instrument_type == "procedural_sfx":
        if len(keys) != 1:
            raise ValueError(
                f"{capability.relative_path} 的 procedural_sfx 必须只有一个触发键"
            )
        sequence, tail_seconds, timing_exception = _procedural_sfx_timing(
            manifest,
            articulation,
            keys[0],
        )
        exception = _combine_exception(exception, timing_exception)
    elif instrument_type == "reversed_cymbal":
        sequence = _reversed_cymbal_sequence(manifest, source, keys)
        exception = _combine_exception(
            exception,
            (
                "反向镲每个变体按资源核验 swell_seconds 完整持有，"
                "不使用通用 0.48s gate"
            ),
        )
    elif instrument_type == "modeled_bianzhong":
        # The highest modeled bell still needs about 5.7 seconds after the
        # standard note-off to reach its deterministic natural end.  A 6 s
        # document tail prevents the last audible sample from being cut at EOF.
        tail_seconds = 6.0
        exception = _combine_exception(
            exception,
            "编钟使用 6s 文件尾，确保最后一枚钟自然落静而不是在 EOF 截断",
        )

    slow_pad = _slow_pad_timing(manifest, articulation, keys)
    if slow_pad is not None:
        sequence, tail_seconds, timing_exception = slow_pad
        exception = _combine_exception(exception, timing_exception)

    articulations = {
        strike.articulation
        for strike in sequence
        if strike.articulation is not None
    }
    if len(articulations) > 1:
        articulation = None

    sequence_unique = tuple(sorted(set(strike.midi_key for strike in sequence)))
    if sequence_unique != keys:
        # Fixed RR entries intentionally repeat one declared key.
        if not (
            len(keys) == 1
            and sequence_unique == keys
            and len(sequence) == 4
        ):
            raise ValueError(
                f"{capability.relative_path} 的事件键与声明合法键集合不一致"
            )

    return FullRangeAudition(
        instrument=capability.relative_path,
        articulation=articulation,
        pitch_semantics=pitch_semantics,
        range_source=range_source,
        declared_ranges=ranges,
        gaps=_gaps(ranges),
        sequence=sequence,
        tail_seconds=tail_seconds,
        exception=exception,
        document=_event_document(
            sequence,
            tail_seconds=tail_seconds,
        ),
    )


__all__ = [
    "A4_HZ",
    "AuditionStrike",
    "CHANNELS",
    "FullRangeAudition",
    "INTER_NOTE_GAP_SECONDS",
    "ISOLATED_PROTOCOL_ID",
    "NOTE_DURATION_SECONDS",
    "PROTOCOL_ID",
    "SAMPLE_RATE",
    "START_SECONDS",
    "TAIL_SECONDS",
    "VELOCITY",
    "build_full_range_audition",
    "isolate_full_range_audition",
    "restrict_full_range_audition",
]
