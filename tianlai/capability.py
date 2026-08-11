"""Instrument capability declarations: what each of the 103 can actually do.

The conductor cannot plan a performance without knowing each player's range,
articulation vocabulary and onset behaviour.  Today that knowledge is spread
across manifests and backend modules in several different shapes, so this
module normalises it into one record per instrument.

The guiding rule is the project's existing one: **declare, do not guess**.
Every capability carries ``articulation_source`` naming where the vocabulary
came from, so a reviewer can tell a manifest-declared list from one recovered
out of a backend constant.  Where nothing can be established, the vocabulary
is empty and the playability gate refuses anything but the default — it never
silently substitutes an articulation the instrument does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any


# 后端模块里的奏法常量。写成显式表而不是反射搜索,是为了让"某个奏法名从哪来"
# 这个问题永远有确定答案;后端改了名字这里会立刻失配报错,而不是悄悄查不到。
_BACKEND_ARTICULATIONS: dict[str, tuple[str, str]] = {
    "violin": ("tianlai.violin", "_PUBLIC_ARTICULATIONS"),
    "cello": ("tianlai.cello", "_PUBLIC_ARTICULATIONS"),
    "flute": ("tianlai.flute", "_PUBLIC_ARTICULATIONS"),
    "vpo_solo_string": ("tianlai.vpo_strings", "_PUBLIC_ARTICULATIONS"),
    "vpo_brass": ("tianlai.vpo_brass", "_PUBLIC_ARTICULATIONS"),
    "vpo_woodwind": ("tianlai.vpo_woodwinds", "_PUBLIC_ARTICULATIONS"),
    "vpo_harp": ("tianlai.vpo_strings", "_HARP_ARTICULATIONS"),
    "vpo_mixed_choir": (
        "tianlai.vpo_specials",
        "_MIXED_CHOIR_ARTICULATIONS",
    ),
    "mtg_solo_sax": ("tianlai.mtg_sax", "_SUPPORTED_ARTICULATIONS"),
}

# Some shared backends expose a different vocabulary for each manifest profile.
# Keep the lookup declarative so a renamed backend table fails loudly instead of
# silently reducing every instrument to its default articulation.
_PROFILE_BACKEND_ARTICULATIONS: dict[str, tuple[str, str, str]] = {
    "vpo_percussion": (
        "tianlai.vpo_percussion",
        "PERCUSSION_PROFILES",
        "profile",
    ),
}

# 这些后端本就没有固定音高,逐音符排谱时按打击/音效处理。
_UNPITCHED_TYPES = frozenset(("procedural_sfx", "reversed_cymbal"))
DEFAULT_ARTICULATION_SENTINEL = "__default__"
DEFAULT_ONSET_OVERLAP_POLICY = "conservative"
ONSET_OVERLAP_POLICIES = frozenset(
    (
        DEFAULT_ONSET_OVERLAP_POLICY,
        "polyphonic_independent",
        "monophonic_connected",
    )
)
RANGE_VALIDATION_MODES = frozenset(("compatibility", "strict_hq"))
COLLABORATION_REVIEW_STATUSES = frozenset(
    ("untested", "in_progress", "passed", "failed")
)
_LOCAL_ARTICULATION_MODULE_LOCK = threading.RLock()
RANGE_PROFILE_QUALITY_STATUSES = frozenset(
    ("pending", "contract_candidate", "rejected")
)


def _normalise_runtime_configuration_scalar(value: Any, *, field: str) -> Any:
    """Return one stable, hashable scalar used by an exact profile selector."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{field} must be a finite JSON scalar")
        return number
    raise ValueError(f"{field} must be a string, number, boolean or null")


def _ranges_cover(
    ranges: tuple[tuple[float, float], ...],
    midi: float,
) -> bool:
    return any(low <= midi <= high for low, high in ranges)


def _ranges_are_contained(
    inner: tuple[tuple[float, float], ...],
    outer: tuple[tuple[float, float], ...],
) -> bool:
    return all(
        any(outer_low <= low and high <= outer_high for outer_low, outer_high in outer)
        for low, high in inner
    )


def _validate_range_tuple(
    ranges: tuple[tuple[float, float], ...],
    *,
    field: str,
    allow_empty: bool,
) -> None:
    if not ranges and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    previous_high: float | None = None
    for index, span in enumerate(ranges):
        if not isinstance(span, tuple) or len(span) != 2:
            raise ValueError(f"{field}[{index}] must be a (minimum, maximum) pair")
        low, high = span
        if (
            isinstance(low, bool)
            or isinstance(high, bool)
            or not isinstance(low, (int, float))
            or not isinstance(high, (int, float))
        ):
            raise ValueError(f"{field}[{index}] notes must be numbers")
        low = float(low)
        high = float(high)
        if (
            not math.isfinite(low)
            or not math.isfinite(high)
            or not 0.0 <= low <= high <= 127.0
        ):
            raise ValueError(
                f"{field}[{index}] must satisfy 0 <= minimum <= maximum <= 127"
            )
        if previous_high is not None and low <= previous_high:
            raise ValueError(
                f"{field} must be ordered, non-overlapping inclusive spans"
            )
        previous_high = high


@dataclass(frozen=True, slots=True)
class OnsetEvidenceRef:
    """Immutable provenance for one approved perceptual-onset document."""

    path: str
    sha256: str
    runtime_fingerprint: str
    review_lead: str
    candidate_sha256: str | None = None
    review_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.path or not self.review_lead:
            raise ValueError("approved onset evidence path and review lead are required")
        for label, value in (
            ("sha256", self.sha256),
            ("runtime_fingerprint", self.runtime_fingerprint),
            ("candidate_sha256", self.candidate_sha256),
            ("review_sha256", self.review_sha256),
        ):
            if value is not None and (
                len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(
                    f"approved onset evidence {label} must be lowercase SHA-256"
                )

    def to_dict(self) -> dict[str, str]:
        data = {
            "path": self.path,
            "sha256": self.sha256,
            "runtime_fingerprint": self.runtime_fingerprint,
            "review_lead": self.review_lead,
        }
        if self.candidate_sha256 is not None:
            data["candidate_sha256"] = self.candidate_sha256
        if self.review_sha256 is not None:
            data["review_sha256"] = self.review_sha256
        return data


@dataclass(frozen=True, slots=True)
class ArticulationOnset:
    """A human-approved delay for one final backend articulation.

    Frames are kept as the source of truth.  Converting an annotation to a
    rounded decimal and back would otherwise make the compensation depend on
    which JSON writer happened to touch the evidence last.
    """

    articulation: str
    frames: int
    sample_rate_hz: int
    context: str
    anchor: str
    evidence: OnsetEvidenceRef

    def __post_init__(self) -> None:
        if not self.articulation:
            raise ValueError("approved onset articulation must not be empty")
        if (
            isinstance(self.frames, bool)
            or not isinstance(self.frames, int)
            or self.frames < 0
        ):
            raise ValueError("approved onset frames must be a non-negative integer")
        if (
            isinstance(self.sample_rate_hz, bool)
            or not isinstance(self.sample_rate_hz, int)
            or self.sample_rate_hz <= 0
        ):
            raise ValueError("approved onset sample_rate_hz must be a positive integer")
        if self.context != "isolated_attack":
            raise ValueError(
                "only context-independent isolated_attack onset evidence is supported"
            )
        if self.anchor != "performance_note_on_output_frame":
            raise ValueError("unsupported approved onset alignment anchor")

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate_hz

    def to_dict(self) -> dict[str, Any]:
        return {
            "articulation": self.articulation,
            "frames": self.frames,
            "sample_rate_hz": self.sample_rate_hz,
            "seconds": self.seconds,
            "context": self.context,
            "anchor": self.anchor,
            "evidence": self.evidence.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DurationArticulationRule:
    """An explicit opt-in rule for choosing a neutral short-note layer.

    Merely exposing an articulation named ``accent`` is not evidence that it
    is a suitable replacement for every short unmarked note.  These rules are
    therefore declared per instrument and identify both the source and target
    articulation plus the effective gate threshold.
    """

    rule_id: str
    source_articulation: str
    target_articulation: str
    below_seconds: float

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("duration articulation rule_id must not be empty")
        if not self.source_articulation.strip():
            raise ValueError(
                "duration articulation source_articulation must not be empty"
            )
        if not self.target_articulation.strip():
            raise ValueError(
                "duration articulation target_articulation must not be empty"
            )
        if self.source_articulation == self.target_articulation:
            raise ValueError(
                "duration articulation source and target must differ"
            )
        if (
            not math.isfinite(self.below_seconds)
            or self.below_seconds <= 0.0
        ):
            raise ValueError(
                "duration articulation below_seconds must be positive and finite"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "source_articulation": self.source_articulation,
            "target_articulation": self.target_articulation,
            "below_seconds": self.below_seconds,
        }


@dataclass(frozen=True, slots=True)
class RangeProfile:
    """Four-layer range contract for one exact runtime configuration/articulation."""

    profile_id: str
    runtime_configuration: tuple[tuple[str, Any], ...]
    final_articulation: str
    hard_playable_ranges: tuple[tuple[float, float], ...]
    idiomatic_ranges: tuple[tuple[float, float], ...] | None
    extended_ranges: tuple[tuple[float, float], ...] | None
    current_high_quality_render_ranges: (
        tuple[tuple[float, float], ...] | None
    )
    quality_status: str

    def __post_init__(self) -> None:
        if not self.profile_id or not self.final_articulation:
            raise ValueError(
                "range profile id and final articulation must not be empty"
            )
        keys = [key for key, _value in self.runtime_configuration]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError(
                "range profile runtime configuration keys must be unique and sorted"
            )
        for key, value in self.runtime_configuration:
            if not key:
                raise ValueError(
                    "range profile runtime configuration keys must not be empty"
                )
            _normalise_runtime_configuration_scalar(
                value,
                field=f"range profile runtime configuration[{key!r}]",
            )

        _validate_range_tuple(
            self.hard_playable_ranges,
            field="hard_playable_ranges",
            allow_empty=False,
        )
        for field, ranges in (
            ("idiomatic_ranges", self.idiomatic_ranges),
            ("extended_ranges", self.extended_ranges),
            (
                "current_high_quality_render_ranges",
                self.current_high_quality_render_ranges,
            ),
        ):
            if ranges is None:
                continue
            _validate_range_tuple(ranges, field=field, allow_empty=True)
            if not _ranges_are_contained(ranges, self.hard_playable_ranges):
                raise ValueError(
                    f"{field} must be contained in hard_playable_ranges"
                )

        if self.quality_status not in RANGE_PROFILE_QUALITY_STATUSES:
            supported = ", ".join(sorted(RANGE_PROFILE_QUALITY_STATUSES))
            raise ValueError(
                f"range profile quality status must be one of {supported}"
            )
        if self.quality_status == "pending":
            if self.current_high_quality_render_ranges is not None:
                raise ValueError(
                    "pending range profile must keep high-quality ranges null"
                )
        elif self.quality_status == "contract_candidate":
            if not self.current_high_quality_render_ranges:
                raise ValueError(
                    "contract-candidate range profile requires non-empty "
                    "proposed high-quality ranges"
                )
        elif self.current_high_quality_render_ranges != ():
            raise ValueError(
                "rejected range profile must declare an empty high-quality range"
            )

    @property
    def selector_key(self) -> tuple[
        tuple[tuple[str, Any], ...],
        str,
    ]:
        return self.runtime_configuration, self.final_articulation

    def to_dict(self) -> dict[str, Any]:
        def optional_ranges(
            ranges: tuple[tuple[float, float], ...] | None,
        ) -> list[list[float]] | None:
            if ranges is None:
                return None
            return [list(span) for span in ranges]

        return {
            "profile_id": self.profile_id,
            "selector": {
                "resolved_runtime_configuration": dict(
                    self.runtime_configuration
                ),
                "final_articulation": self.final_articulation,
            },
            "physical": {
                "hard_playable_ranges": [
                    list(span) for span in self.hard_playable_ranges
                ],
                "idiomatic_ranges": optional_ranges(self.idiomatic_ranges),
                "extended_ranges": optional_ranges(self.extended_ranges),
            },
            "render_quality": {
                "current_high_quality_render_ranges": optional_ranges(
                    self.current_high_quality_render_ranges
                ),
                "status": self.quality_status,
                "approval_evidence": None,
            },
        }


@dataclass(frozen=True, slots=True)
class RangeProfileEvaluation:
    """Structured per-note result; compatibility may report without enforcing."""

    mode: str
    status: str
    applicable: bool
    verified: bool
    midi_note: float
    final_articulation: str
    runtime_configuration: tuple[tuple[str, Any], ...]
    legacy_covered: bool
    profile: RangeProfile | None
    hard_covered: bool | None = None
    idiomatic_covered: bool | None = None
    extended_covered: bool | None = None
    high_quality_covered: bool | None = None

    def __post_init__(self) -> None:
        if self.mode not in RANGE_VALIDATION_MODES:
            supported = ", ".join(sorted(RANGE_VALIDATION_MODES))
            raise ValueError(
                f"range validation mode must be one of {supported}"
            )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "mode": self.mode,
            "status": self.status,
            "applicable": self.applicable,
            "verified": self.verified,
            "midi_note": self.midi_note,
            "final_articulation": self.final_articulation,
            "resolved_runtime_configuration": dict(
                self.runtime_configuration
            ),
            "legacy_covered": self.legacy_covered,
            "profile_id": (
                self.profile.profile_id if self.profile is not None else None
            ),
            "quality_status": (
                self.profile.quality_status
                if self.profile is not None
                else None
            ),
            "approval_evidence_status": "protocol_unavailable",
            "coverage": {
                "hard_playable": self.hard_covered,
                "idiomatic": self.idiomatic_covered,
                "extended": self.extended_covered,
                "current_high_quality": self.high_quality_covered,
            },
        }
        if self.profile is not None:
            result["declared_ranges"] = {
                "hard_playable": [
                    list(span)
                    for span in self.profile.hard_playable_ranges
                ],
                "idiomatic": (
                    None
                    if self.profile.idiomatic_ranges is None
                    else [
                        list(span)
                        for span in self.profile.idiomatic_ranges
                    ]
                ),
                "extended": (
                    None
                    if self.profile.extended_ranges is None
                    else [
                        list(span)
                        for span in self.profile.extended_ranges
                    ]
                ),
                "current_high_quality": (
                    None
                    if self.profile.current_high_quality_render_ranges is None
                    else [
                        list(span)
                        for span in (
                            self.profile.current_high_quality_render_ranges
                        )
                    ]
                ),
            }
        return result


@dataclass(frozen=True, slots=True)
class InstrumentCapability:
    """One instrument's machine-readable contract with the conductor."""

    name: str
    relative_path: str
    manifest_path: str
    implementation_type: str
    pitched: bool
    note_min: float | None
    note_max: float | None
    articulations: tuple[str, ...]
    default_articulation: str | None
    articulation_source: str
    onset_seconds: float | None
    quality_tier: str | None
    license_status: str | None = None
    collaboration_review_status: str | None = None
    pitch_mode: str | None = None
    fixed_midi_note: float | None = None
    playable_ranges: tuple[tuple[float, float], ...] = ()
    articulation_playable_ranges: tuple[
        tuple[str, tuple[tuple[float, float], ...]], ...
    ] = ()
    range_profiles: tuple[RangeProfile, ...] = ()
    range_base_runtime_configuration: tuple[tuple[str, Any], ...] = ()
    articulation_onsets: tuple[ArticulationOnset, ...] = ()
    onset_evidence_path: str | None = None
    onset_project_root: str | None = None
    onset_overlap_policy: str = DEFAULT_ONSET_OVERLAP_POLICY
    articulation_auto_default: bool = True
    duration_articulation_rules: tuple[DurationArticulationRule, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.collaboration_review_status is not None
            and self.collaboration_review_status
            not in COLLABORATION_REVIEW_STATUSES
        ):
            supported = ", ".join(sorted(COLLABORATION_REVIEW_STATUSES))
            raise ValueError(
                "collaboration_review_status must be one of "
                f"{supported}; got {self.collaboration_review_status!r}"
            )
        if not isinstance(self.articulation_auto_default, bool):
            raise ValueError("articulation_auto_default must be boolean")
        rule_ids: set[str] = set()
        rule_keys: set[tuple[str, float]] = set()
        for rule in self.duration_articulation_rules:
            if rule.rule_id in rule_ids:
                raise ValueError(
                    f"duplicate duration articulation rule_id {rule.rule_id!r}"
                )
            key = (rule.source_articulation, rule.below_seconds)
            if key in rule_keys:
                raise ValueError(
                    "duplicate duration articulation source/threshold rule"
                )
            if rule.source_articulation not in self.articulations:
                raise ValueError(
                    "duration articulation source is not a declared "
                    f"articulation: {rule.source_articulation!r}"
                )
            if rule.target_articulation not in self.articulations:
                raise ValueError(
                    "duration articulation target is not a declared "
                    f"articulation: {rule.target_articulation!r}"
                )
            rule_ids.add(rule.rule_id)
            rule_keys.add(key)
        if self.onset_overlap_policy not in ONSET_OVERLAP_POLICIES:
            supported = ", ".join(sorted(ONSET_OVERLAP_POLICIES))
            raise ValueError(
                "onset_overlap_policy must be one of "
                f"{supported}; got {self.onset_overlap_policy!r}"
            )
        if not self.range_profiles:
            if self.range_base_runtime_configuration:
                raise ValueError(
                    "range base runtime configuration requires declared profiles"
                )
            return
        expected_keys = tuple(
            key for key, _value in self.range_profiles[0].runtime_configuration
        )
        base_keys = tuple(
            key for key, _value in self.range_base_runtime_configuration
        )
        if base_keys != expected_keys:
            raise ValueError(
                "range base runtime configuration keys must exactly match "
                "the profile selector keys"
            )
        profile_ids: set[str] = set()
        selectors: set[tuple[tuple[tuple[str, Any], ...], str]] = set()
        for profile in self.range_profiles:
            keys = tuple(
                key for key, _value in profile.runtime_configuration
            )
            if keys != expected_keys:
                raise ValueError(
                    "all range profiles must use the same resolved runtime "
                    "configuration keys"
                )
            if profile.profile_id in profile_ids:
                raise ValueError(
                    f"duplicate range profile id {profile.profile_id!r}"
                )
            if profile.selector_key in selectors:
                raise ValueError(
                    "duplicate range profile runtime configuration/final "
                    "articulation selector"
                )
            profile_ids.add(profile.profile_id)
            selectors.add(profile.selector_key)

    @property
    def ignores_pitch(self) -> bool:
        """A ``fixed`` instrument plays the same sound whatever note arrives."""

        return self.pitch_mode == "fixed"

    @property
    def routing_class(self) -> str:
        """Return the explicit catalogue-level routing family.

        This uses the repository's stable top-level taxonomy rather than
        guessing from an instrument name or from ``pitched``.  In particular,
        tuned percussion still belongs in percussion-kit discovery, while
        environmental effects never crowd out drum candidates.
        """

        parts = self.relative_path.split("/")
        if parts[0] == "环境与拟音":
            return "effect"
        if parts[0] == "现代鼓组" or parts[:2] == ["管弦乐", "打击乐组"]:
            return "percussion"
        return "instrument"

    def ranges_for(
        self, articulation: str | None = None
    ) -> tuple[tuple[float, float], ...]:
        """Return the effective inclusive spans for one backend articulation.

        An articulation-specific declaration overrides the instrument-wide
        spans.  Missing declarations inherit the global segmented range, then
        the backwards-compatible ``note_min`` / ``note_max`` envelope.
        """

        if articulation is not None:
            for name, ranges in self.articulation_playable_ranges:
                if name == articulation:
                    return ranges
        if self.playable_ranges:
            return self.playable_ranges
        if self.note_min is not None and self.note_max is not None:
            return ((self.note_min, self.note_max),)
        return ()

    def covers(self, midi: float, articulation: str | None = None) -> bool:
        if self.ignores_pitch:
            return True
        # ``ignore`` means "select an existing sample by key, but do not
        # transpose that sample".  It is therefore non-pitched in musical
        # semantics while still requiring the incoming key to stay inside the
        # backend's declared selector range.  Other unpitched entries without
        # that explicit contract retain their legacy all-key behaviour.
        if not self.pitched and self.pitch_mode != "ignore":
            return True
        if self.note_min is not None and midi < self.note_min:
            return False
        if self.note_max is not None and midi > self.note_max:
            return False
        ranges = self.ranges_for(articulation)
        if ranges:
            return any(low <= midi <= high for low, high in ranges)
        return True

    def supports(self, articulation: str) -> bool:
        if articulation == self.default_articulation:
            return True
        return articulation in self.articulations

    def resolved_range_configuration(
        self,
        overrides: dict[str, Any] | None = None,
    ) -> tuple[tuple[str, Any], ...]:
        """Resolve the base manifest values plus every roster sound override.

        An override absent from the profile selector key set is intentionally
        retained.  Exact matching then fails instead of reusing evidence from
        a different runtime configuration.
        """

        resolved = dict(self.range_base_runtime_configuration)
        for raw_key, raw_value in (overrides or {}).items():
            key = str(raw_key)
            resolved[key] = _normalise_runtime_configuration_scalar(
                raw_value,
                field=f"runtime override[{key!r}]",
            )
        return tuple(sorted(resolved.items()))

    def range_profile_for(
        self,
        articulation: str | None,
        *,
        overrides: dict[str, Any] | None = None,
    ) -> RangeProfile | None:
        """Return only an exact configuration and final-articulation match."""

        lookup = (
            DEFAULT_ARTICULATION_SENTINEL
            if articulation is None
            else articulation
        )
        configuration = self.resolved_range_configuration(overrides)
        for profile in self.range_profiles:
            if (
                profile.runtime_configuration == configuration
                and profile.final_articulation == lookup
            ):
                return profile
        return None

    def evaluate_range_profile(
        self,
        midi: float,
        articulation: str | None,
        *,
        overrides: dict[str, Any] | None = None,
        mode: str = "compatibility",
    ) -> RangeProfileEvaluation:
        """Evaluate all four layers without silently borrowing another profile."""

        if mode not in RANGE_VALIDATION_MODES:
            supported = ", ".join(sorted(RANGE_VALIDATION_MODES))
            raise ValueError(
                f"range validation mode must be one of {supported}"
            )
        if isinstance(midi, bool) or not isinstance(midi, (int, float)):
            raise ValueError("range evaluation MIDI note must be a number")
        midi_note = float(midi)
        if not math.isfinite(midi_note):
            raise ValueError("range evaluation MIDI note must be finite")
        lookup = (
            DEFAULT_ARTICULATION_SENTINEL
            if articulation is None
            else articulation
        )
        configuration = self.resolved_range_configuration(overrides)
        legacy_covered = self.covers(midi_note, articulation)

        if not self.pitched:
            return RangeProfileEvaluation(
                mode=mode,
                status="not_applicable_unpitched",
                applicable=False,
                verified=False,
                midi_note=midi_note,
                final_articulation=lookup,
                runtime_configuration=configuration,
                legacy_covered=legacy_covered,
                profile=None,
            )
        if not self.range_profiles:
            return RangeProfileEvaluation(
                mode=mode,
                status="manifest_unmigrated",
                applicable=True,
                verified=False,
                midi_note=midi_note,
                final_articulation=lookup,
                runtime_configuration=configuration,
                legacy_covered=legacy_covered,
                profile=None,
            )
        profile = self.range_profile_for(
            articulation,
            overrides=overrides,
        )
        if profile is None:
            return RangeProfileEvaluation(
                mode=mode,
                status="profile_not_found",
                applicable=True,
                verified=False,
                midi_note=midi_note,
                final_articulation=lookup,
                runtime_configuration=configuration,
                legacy_covered=legacy_covered,
                profile=None,
            )

        hard_covered = _ranges_cover(
            profile.hard_playable_ranges,
            midi_note,
        )
        idiomatic_covered = (
            None
            if profile.idiomatic_ranges is None
            else _ranges_cover(profile.idiomatic_ranges, midi_note)
        )
        extended_covered = (
            None
            if profile.extended_ranges is None
            else _ranges_cover(profile.extended_ranges, midi_note)
        )
        high_quality_covered = (
            None
            if profile.current_high_quality_render_ranges is None
            else _ranges_cover(
                profile.current_high_quality_render_ranges,
                midi_note,
            )
        )
        if not hard_covered:
            status = "outside_hard_playable_range"
            verified = False
        elif profile.quality_status == "pending":
            status = "quality_pending"
            verified = False
        elif profile.quality_status == "rejected":
            status = "quality_rejected"
            verified = False
        elif not high_quality_covered:
            status = "outside_candidate_high_quality"
            verified = False
        else:
            # A manifest profile is only a contract candidate.  Until the
            # machine matrix proves compound runtime-variant coverage and a
            # strict human-summary protocol exists, no manifest field can
            # self-authorise strict HQ rendering.
            status = "contract_candidate_unverified"
            verified = False
        return RangeProfileEvaluation(
            mode=mode,
            status=status,
            applicable=True,
            verified=verified,
            midi_note=midi_note,
            final_articulation=lookup,
            runtime_configuration=configuration,
            legacy_covered=legacy_covered,
            profile=profile,
            hard_covered=hard_covered,
            idiomatic_covered=idiomatic_covered,
            extended_covered=extended_covered,
            high_quality_covered=high_quality_covered,
        )

    def onset_for(
        self,
        articulation: str | None,
        *,
        context: str,
        records: tuple[ArticulationOnset, ...] | None = None,
    ) -> ArticulationOnset | None:
        """Return only an exact, approved final-articulation/context match."""

        available = (
            self.resolve_articulation_onsets()
            if records is None
            else records
        )
        lookup = (
            DEFAULT_ARTICULATION_SENTINEL
            if articulation is None
            else articulation
        )
        for onset in available:
            if onset.articulation == lookup and onset.context == context:
                return onset
        return None

    def resolve_articulation_onsets(self) -> tuple[ArticulationOnset, ...]:
        """Validate deferred evidence for this instrument, if one exists.

        Catalogue discovery stays fast and cannot be taken down by an unused
        instrument's stale evidence.  A roster that actually selects that
        instrument still fails closed before its first performance event.
        """

        if self.articulation_onsets or self.onset_evidence_path is None:
            return self.articulation_onsets
        if self.onset_project_root is None:
            raise ValueError(
                f"{self.name} has deferred onset evidence without a project root"
            )
        manifest_path = Path(self.manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError(
                f"instrument manifest root must be an object: {manifest_path}"
            )
        return _read_articulation_onsets(
            manifest_path.parent,
            manifest_path,
            manifest,
            self.articulations,
            catalogue_root=Path(self.onset_project_root) / "乐器",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "implementation_type": self.implementation_type,
            "routing_class": self.routing_class,
            "pitched": self.pitched,
            "note_min": self.note_min,
            "note_max": self.note_max,
            "articulations": list(self.articulations),
            "default_articulation": self.default_articulation,
            "articulation_source": self.articulation_source,
            "articulation_auto_default": self.articulation_auto_default,
            "duration_articulation_rules": [
                rule.to_dict() for rule in self.duration_articulation_rules
            ],
            "onset_seconds": self.onset_seconds,
            "quality_tier": self.quality_tier,
            "collaboration_review_status": self.collaboration_review_status,
            "license_status": self.license_status,
            "pitch_mode": self.pitch_mode,
            "fixed_midi_note": self.fixed_midi_note,
            "playable_ranges": [list(span) for span in self.playable_ranges],
            "articulation_playable_ranges": {
                name: [list(span) for span in ranges]
                for name, ranges in self.articulation_playable_ranges
            },
            "range_contract_status": (
                "declared_profiles"
                if self.range_profiles
                else "unmigrated"
            ),
            "range_base_runtime_configuration": dict(
                self.range_base_runtime_configuration
            ),
            "range_profiles": [
                profile.to_dict() for profile in self.range_profiles
            ],
            "articulation_onsets": [
                onset.to_dict() for onset in self.articulation_onsets
            ],
            "onset_overlap_policy": self.onset_overlap_policy,
            "onset_evidence_status": (
                "approved"
                if self.articulation_onsets
                else "deferred"
                if self.onset_evidence_path is not None
                else "absent"
            ),
        }


def _load_backend_articulations(
    instrument_type: str, manifest: dict[str, Any]
) -> tuple[tuple[str, ...], str] | None:
    target = _BACKEND_ARTICULATIONS.get(instrument_type)
    import importlib

    if target is not None:
        module_name, constant_name = target
        module = importlib.import_module(module_name)
        if not hasattr(module, constant_name):
            raise ValueError(
                f"backend {module_name} no longer defines {constant_name}; "
                "the capability table in tianlai/capability.py is out of date"
            )
        names = tuple(sorted(str(item) for item in getattr(module, constant_name)))
        return names, f"backend:{module_name}.{constant_name}"

    profile_target = _PROFILE_BACKEND_ARTICULATIONS.get(instrument_type)
    if profile_target is None:
        return None
    module_name, table_name, profile_field = profile_target
    module = importlib.import_module(module_name)
    if not hasattr(module, table_name):
        raise ValueError(
            f"backend {module_name} no longer defines {table_name}; "
            "the capability table in tianlai/capability.py is out of date"
        )
    if profile_field not in manifest:
        raise ValueError(
            f"{instrument_type} manifest does not declare {profile_field!r}, "
            "so its articulation vocabulary cannot be established"
        )
    profile_name = str(manifest[profile_field])
    profiles = getattr(module, table_name)
    try:
        profile = profiles[profile_name]
    except (KeyError, TypeError) as error:
        raise ValueError(
            f"backend {module_name}.{table_name} has no profile {profile_name!r}"
        ) from error
    names = tuple(sorted(str(item) for item in profile.articulations))
    return names, f"backend:{module_name}.{table_name}[{profile_name}]"


def _parse_playable_ranges(
    raw_ranges: Any,
    *,
    field: str,
) -> tuple[tuple[float, float], ...]:
    if not isinstance(raw_ranges, list) or not raw_ranges:
        raise ValueError(f"{field} must be a non-empty array")

    ranges: list[tuple[float, float]] = []
    previous_high: float | None = None
    for index, raw_span in enumerate(raw_ranges):
        if not isinstance(raw_span, list) or len(raw_span) != 2:
            raise ValueError(
                f"{field}[{index}] must be a [note_min, note_max] pair"
            )
        if any(isinstance(value, bool) for value in raw_span):
            raise ValueError(f"{field}[{index}] notes must be numbers")
        try:
            low, high = (float(raw_span[0]), float(raw_span[1]))
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field}[{index}] notes must be numbers") from error
        if not math.isfinite(low) or not math.isfinite(high):
            raise ValueError(f"{field}[{index}] notes must be finite")
        if not 0.0 <= low <= high <= 127.0:
            raise ValueError(
                f"{field}[{index}] must satisfy 0 <= min <= max <= 127"
            )
        if previous_high is not None and low <= previous_high:
            raise ValueError(
                f"{field} must be ordered, non-overlapping inclusive spans"
            )
        ranges.append((low, high))
        previous_high = high
    return tuple(ranges)


def _read_playable_ranges(
    manifest: dict[str, Any],
) -> tuple[tuple[float, float], ...]:
    """Validate optional disjoint playable spans in sounding MIDI notes."""

    raw_ranges = manifest.get("playable_ranges")
    if raw_ranges is None:
        return ()
    return _parse_playable_ranges(raw_ranges, field="playable_ranges")


def _read_articulation_playable_ranges(
    manifest: dict[str, Any],
    articulations: tuple[str, ...],
) -> tuple[tuple[str, tuple[tuple[float, float], ...]], ...]:
    """Merge generic and SFZ-local per-articulation range declarations.

    Dedicated SFZ manifests already keep the range beside each SFZ entry.
    ``articulation_playable_ranges`` is the backend-neutral spelling for local
    Python, VPO and future implementations.  If both spellings are present,
    they must agree exactly so declaration order can never change behaviour.
    """

    combined: dict[str, tuple[tuple[float, float], ...]] = {}
    raw_generic = manifest.get("articulation_playable_ranges")
    if raw_generic is not None:
        if not isinstance(raw_generic, dict) or not raw_generic:
            raise ValueError(
                "articulation_playable_ranges must be a non-empty object"
            )
        for raw_name, raw_ranges in raw_generic.items():
            name = str(raw_name)
            combined[name] = _parse_playable_ranges(
                raw_ranges,
                field=f"articulation_playable_ranges[{name!r}]",
            )

    raw_articulations = manifest.get("articulations")
    if isinstance(raw_articulations, dict):
        for raw_name, specification in raw_articulations.items():
            if not isinstance(specification, dict):
                continue
            raw_ranges = specification.get("playable_ranges")
            if raw_ranges is None:
                continue
            name = str(raw_name)
            ranges = _parse_playable_ranges(
                raw_ranges,
                field=f"articulations[{name!r}].playable_ranges",
            )
            previous = combined.get(name)
            if previous is not None and previous != ranges:
                raise ValueError(
                    f"articulation {name!r} has conflicting playable range "
                    "declarations"
                )
            combined[name] = ranges

    known = set(articulations)
    unknown = sorted(name for name in combined if name not in known)
    if unknown:
        names = ", ".join(repr(name) for name in unknown)
        raise ValueError(
            "articulation_playable_ranges names undeclared articulations: "
            f"{names}"
        )
    return tuple(sorted(combined.items()))


def _validate_articulation_playable_ranges(
    articulation_ranges: tuple[
        tuple[str, tuple[tuple[float, float], ...]], ...
    ],
    *,
    note_min: float | None,
    note_max: float | None,
    playable_ranges: tuple[tuple[float, float], ...],
) -> None:
    for name, ranges in articulation_ranges:
        for low, high in ranges:
            if note_min is not None and low < note_min:
                raise ValueError(
                    f"articulation {name!r} playable ranges extend below "
                    "declared note_min"
                )
            if note_max is not None and high > note_max:
                raise ValueError(
                    f"articulation {name!r} playable ranges extend above "
                    "declared note_max"
                )
            if playable_ranges and not any(
                global_low <= low and high <= global_high
                for global_low, global_high in playable_ranges
            ):
                raise ValueError(
                    f"articulation {name!r} playable range [{low:g}, {high:g}] "
                    "is not contained in one global playable_ranges span"
                )


def _require_range_contract_keys(
    value: Any,
    *,
    field: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    keys = {str(key) for key in value}
    missing = sorted(required - keys)
    unknown = sorted(keys - required - optional)
    if missing:
        raise ValueError(
            f"{field} is missing required fields: {', '.join(missing)}"
        )
    if unknown:
        raise ValueError(
            f"{field} contains unknown fields: {', '.join(unknown)}"
        )
    return value


def _parse_optional_range_profile_ranges(
    raw_ranges: Any,
    *,
    field: str,
) -> tuple[tuple[float, float], ...] | None:
    if raw_ranges is None:
        return None
    if not isinstance(raw_ranges, list):
        raise ValueError(f"{field} must be null or an array")
    if not raw_ranges:
        return ()
    return _parse_playable_ranges(raw_ranges, field=field)


def _read_range_profiles(
    manifest: dict[str, Any],
    articulations: tuple[str, ...],
    default_articulation: str | None,
    *,
    note_min: float | None,
    note_max: float | None,
    playable_ranges: tuple[tuple[float, float], ...],
    articulation_playable_ranges: tuple[
        tuple[str, tuple[tuple[float, float], ...]], ...
    ],
) -> tuple[
    tuple[RangeProfile, ...],
    tuple[tuple[str, Any], ...],
]:
    """Parse an exact, non-wildcard four-layer range contract."""

    raw_contract = manifest.get("range_profiles")
    if raw_contract is None:
        return (), ()
    contract = _require_range_contract_keys(
        raw_contract,
        field="range_profiles",
        required=frozenset(
            ("schema_version", "pitch_unit", "profiles", "fallback_policy")
        ),
        optional=frozenset(("unknown_value_semantics",)),
    )
    if contract["schema_version"] != 1:
        raise ValueError("range_profiles.schema_version must be 1")
    if contract["pitch_unit"] != "concert_midi_note":
        raise ValueError(
            "range_profiles.pitch_unit must be concert_midi_note"
        )
    if contract["fallback_policy"] != (
        "reject_unknown_configuration_or_final_articulation"
    ):
        raise ValueError(
            "range_profiles fallback policy must reject unknown "
            "configuration or final articulation"
        )
    semantics = contract.get(
        "unknown_value_semantics",
        "null_means_unreviewed",
    )
    if semantics != "null_means_unreviewed":
        raise ValueError(
            "range_profiles unknown values must use null_means_unreviewed"
        )
    raw_profiles = contract["profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("range_profiles.profiles must be a non-empty array")

    known_articulations = set(articulations)
    if not known_articulations and default_articulation is None:
        known_articulations.add(DEFAULT_ARTICULATION_SENTINEL)
    legacy_articulation_ranges = dict(articulation_playable_ranges)
    global_legacy_ranges = playable_ranges
    if (
        not global_legacy_ranges
        and note_min is not None
        and note_max is not None
    ):
        global_legacy_ranges = ((note_min, note_max),)

    profiles: list[RangeProfile] = []
    expected_configuration_keys: tuple[str, ...] | None = None
    profile_ids: set[str] = set()
    selectors: set[tuple[tuple[tuple[str, Any], ...], str]] = set()
    for index, raw_profile in enumerate(raw_profiles):
        field = f"range_profiles.profiles[{index}]"
        profile_data = _require_range_contract_keys(
            raw_profile,
            field=field,
            required=frozenset(
                ("profile_id", "selector", "physical", "render_quality")
            ),
        )
        profile_id = profile_data["profile_id"]
        if not isinstance(profile_id, str) or not profile_id:
            raise ValueError(f"{field}.profile_id must be a non-empty string")
        if profile_id in profile_ids:
            raise ValueError(f"duplicate range profile id {profile_id!r}")

        selector = _require_range_contract_keys(
            profile_data["selector"],
            field=f"{field}.selector",
            required=frozenset(
                ("resolved_runtime_configuration", "final_articulation")
            ),
        )
        raw_configuration = selector["resolved_runtime_configuration"]
        if not isinstance(raw_configuration, dict):
            raise ValueError(
                f"{field}.selector.resolved_runtime_configuration "
                "must be an object"
            )
        configuration: list[tuple[str, Any]] = []
        for raw_key, raw_value in sorted(
            raw_configuration.items(),
            key=lambda item: str(item[0]),
        ):
            key = str(raw_key)
            if not key:
                raise ValueError(
                    f"{field} runtime configuration keys must not be empty"
                )
            configuration.append(
                (
                    key,
                    _normalise_runtime_configuration_scalar(
                        raw_value,
                        field=(
                            f"{field}.selector."
                            f"resolved_runtime_configuration[{key!r}]"
                        ),
                    ),
                )
            )
        configuration_tuple = tuple(configuration)
        configuration_keys = tuple(key for key, _value in configuration_tuple)
        if expected_configuration_keys is None:
            expected_configuration_keys = configuration_keys
        elif configuration_keys != expected_configuration_keys:
            raise ValueError(
                "all range profiles must use the same resolved runtime "
                "configuration keys"
            )

        final_articulation = selector["final_articulation"]
        if not isinstance(final_articulation, str) or not final_articulation:
            raise ValueError(
                f"{field}.selector.final_articulation must be non-empty"
            )
        if final_articulation not in known_articulations:
            raise ValueError(
                f"{field} selects undeclared final articulation "
                f"{final_articulation!r}"
            )

        physical = _require_range_contract_keys(
            profile_data["physical"],
            field=f"{field}.physical",
            required=frozenset(
                (
                    "hard_playable_ranges",
                    "idiomatic_ranges",
                    "extended_ranges",
                )
            ),
        )
        hard_ranges = _parse_playable_ranges(
            physical["hard_playable_ranges"],
            field=f"{field}.physical.hard_playable_ranges",
        )
        legacy_ranges = legacy_articulation_ranges.get(
            final_articulation,
            global_legacy_ranges,
        )
        if not legacy_ranges:
            raise ValueError(
                f"{field} cannot declare a hard range before the legacy "
                "playable range exists"
            )
        if not _ranges_are_contained(hard_ranges, legacy_ranges):
            raise ValueError(
                f"{field}.physical.hard_playable_ranges must stay inside "
                "the existing articulation/global playable ranges"
            )
        idiomatic_ranges = _parse_optional_range_profile_ranges(
            physical["idiomatic_ranges"],
            field=f"{field}.physical.idiomatic_ranges",
        )
        extended_ranges = _parse_optional_range_profile_ranges(
            physical["extended_ranges"],
            field=f"{field}.physical.extended_ranges",
        )

        render_quality = _require_range_contract_keys(
            profile_data["render_quality"],
            field=f"{field}.render_quality",
            required=frozenset(
                (
                    "current_high_quality_render_ranges",
                    "status",
                    "approval_evidence",
                )
            ),
        )
        high_quality_ranges = _parse_optional_range_profile_ranges(
            render_quality["current_high_quality_render_ranges"],
            field=(
                f"{field}.render_quality."
                "current_high_quality_render_ranges"
            ),
        )
        quality_status = render_quality["status"]
        if (
            not isinstance(quality_status, str)
            or quality_status not in RANGE_PROFILE_QUALITY_STATUSES
        ):
            supported = ", ".join(sorted(RANGE_PROFILE_QUALITY_STATUSES))
            raise ValueError(
                f"{field}.render_quality.status must be one of {supported}"
            )
        if render_quality["approval_evidence"] is not None:
            raise ValueError(
                f"{field}.render_quality.approval_evidence must remain null; "
                "the compound-variant and human-summary approval protocol "
                "is not implemented"
            )

        profile = RangeProfile(
            profile_id=profile_id,
            runtime_configuration=configuration_tuple,
            final_articulation=final_articulation,
            hard_playable_ranges=hard_ranges,
            idiomatic_ranges=idiomatic_ranges,
            extended_ranges=extended_ranges,
            current_high_quality_render_ranges=high_quality_ranges,
            quality_status=quality_status,
        )
        if profile.selector_key in selectors:
            raise ValueError(
                f"{field} duplicates a runtime configuration/final "
                "articulation selector"
            )
        profile_ids.add(profile_id)
        selectors.add(profile.selector_key)
        profiles.append(profile)

    keys = expected_configuration_keys or ()
    base_configuration = tuple(
        (
            key,
            _normalise_runtime_configuration_scalar(
                manifest.get(key),
                field=f"manifest runtime configuration[{key!r}]",
            ),
        )
        for key in keys
    )
    return tuple(profiles), base_configuration


def _load_local_articulations(
    directory: Path, manifest: dict[str, Any]
) -> tuple[str, ...] | None:
    """Read a legacy local factory's articulation set from ``乐器.py``.

    Trusted built-in backends belong in ``_BACKEND_ARTICULATIONS`` above.  This
    fallback remains for compatible third-party or historical local factories.
    """

    implementation = manifest.get("implementation")
    if implementation is None:
        return None
    path = (directory / str(implementation)).resolve()
    if not path.is_file():
        return None
    import hashlib
    import importlib.util
    import sys

    suffix = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    name = f"tianlai_capability_probe_{suffix}"
    # ``sys.modules`` exposes a module before its loader has finished.  Two
    # concurrent readiness/render calls could therefore let one caller see a
    # half-initialised instrument backend and silently fall back to the
    # manifest's default articulation.  Duration rules would then appear to
    # target an undeclared articulation.  Serialise this small, one-time local
    # probe and remove failed imports so later calls never trust partial state.
    with _LOCAL_ARTICULATION_MODULE_LOCK:
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                return None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                if sys.modules.get(name) is module:
                    del sys.modules[name]
                raise
        names = getattr(module, "_PUBLIC_ARTICULATIONS", None)
    if not names:
        return None
    return tuple(sorted(str(item) for item in names))


def _read_onset(directory: Path, manifest: dict[str, Any]) -> float | None:
    """Reject the former unreviewed scalar inlet.

    The field stays on ``InstrumentCapability`` for source compatibility with
    callers that construct records themselves, but the catalogue loader never
    imports it and the conductor never schedules from it.
    """

    if "onset_seconds" in manifest:
        raise ValueError(
            f"{directory / '乐器.json'} uses legacy onset_seconds; "
            "generate and manually approve per-articulation 发音延迟.json instead"
        )
    return None


def _read_onset_overlap_policy(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> str:
    policy = manifest.get(
        "onset_overlap_policy",
        DEFAULT_ONSET_OVERLAP_POLICY,
    )
    if not isinstance(policy, str) or policy not in ONSET_OVERLAP_POLICIES:
        supported = ", ".join(sorted(ONSET_OVERLAP_POLICIES))
        raise ValueError(
            f"{manifest_path} onset_overlap_policy must be one of {supported}"
        )
    return policy


def _read_duration_articulation_rules(
    manifest_path: Path,
    manifest: dict[str, Any],
    articulations: tuple[str, ...],
    default_articulation: str | None,
) -> tuple[DurationArticulationRule, ...]:
    raw_rules = manifest.get("duration_articulation_rules")
    if raw_rules is None:
        return ()
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError(
            f"{manifest_path} duration_articulation_rules must be a non-empty list"
        )
    rules: list[DurationArticulationRule] = []
    allowed_fields = {
        "rule_id",
        "source_articulation",
        "target_articulation",
        "below_seconds",
    }
    for index, raw in enumerate(raw_rules):
        label = f"duration_articulation_rules[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{manifest_path} {label} must be an object")
        unknown = sorted(str(key) for key in raw if key not in allowed_fields)
        if unknown:
            raise ValueError(
                f"{manifest_path} {label} contains unknown fields: "
                f"{', '.join(unknown)}"
            )
        missing = sorted(allowed_fields - set(raw))
        if missing:
            raise ValueError(
                f"{manifest_path} {label} is missing: {', '.join(missing)}"
            )
        below = raw["below_seconds"]
        if isinstance(below, bool):
            raise ValueError(
                f"{manifest_path} {label}.below_seconds must be a number"
            )
        rule = DurationArticulationRule(
            rule_id=str(raw["rule_id"]),
            source_articulation=str(raw["source_articulation"]),
            target_articulation=str(raw["target_articulation"]),
            below_seconds=float(below),
        )
        if rule.source_articulation != default_articulation:
            raise ValueError(
                f"{manifest_path} {label}.source_articulation must equal "
                "the instrument default_articulation"
            )
        if rule.source_articulation not in articulations:
            raise ValueError(
                f"{manifest_path} {label} source articulation is undeclared"
            )
        if rule.target_articulation not in articulations:
            raise ValueError(
                f"{manifest_path} {label} target articulation is undeclared"
            )
        rules.append(rule)
    # Smaller thresholds are more specific and win when a future instrument
    # declares more than one short-note layer.
    return tuple(
        sorted(
            rules,
            key=lambda item: (
                item.below_seconds,
                item.rule_id,
            ),
        )
    )


def _project_root_for(directory: Path, catalogue_root: Path) -> Path:
    for candidate in (directory, *directory.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "tianlai"
        ).is_dir():
            return candidate.resolve()
    return catalogue_root.resolve().parent


def _read_articulation_onsets(
    directory: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    articulations: tuple[str, ...],
    *,
    catalogue_root: Path,
) -> tuple[ArticulationOnset, ...]:
    """Load only a complete, current, human-approved evidence chain."""

    evidence_path = directory / "发音延迟.json"
    if not evidence_path.is_file():
        return ()
    if str(manifest.get("type", "")) == "reversed_cymbal":
        raise ValueError(
            f"{evidence_path} cannot define attack onset for anticipatory audio"
        )

    from .onset_evidence import (
        canonical_json_bytes,
        load_approved_onset_evidence,
        sha256_file,
    )

    project_root = _project_root_for(directory, catalogue_root)
    approved = load_approved_onset_evidence(
        evidence_path,
        project_root=project_root,
        manifest_path=manifest_path,
        verify_source_chain=False,
    )
    try:
        evidence_label = evidence_path.resolve().relative_to(project_root).as_posix()
    except ValueError as error:
        raise ValueError(
            f"approved onset evidence must stay inside project root: {evidence_path}"
        ) from error

    lead = approved["review_lead"]
    sources = approved["sources"]
    fingerprint_hash = hashlib.sha256(
        canonical_json_bytes(approved["runtime_fingerprint"])
    ).hexdigest()
    evidence = OnsetEvidenceRef(
        path=evidence_label,
        sha256=sha256_file(evidence_path),
        runtime_fingerprint=fingerprint_hash,
        review_lead=str(lead["reviewer_id"]),
        candidate_sha256=str(sources["candidate_sha256"]),
        review_sha256=str(sources["review_sha256"]),
    )

    known = set(articulations)
    if not known and manifest.get("default_articulation") is None:
        known.add(DEFAULT_ARTICULATION_SENTINEL)
    records: list[ArticulationOnset] = []
    for name, raw in sorted(approved["articulations"].items()):
        if name not in known:
            raise ValueError(
                f"{evidence_path} approves undeclared articulation {name!r}"
            )
        if name.casefold().startswith("crescendo_"):
            raise ValueError(
                f"{evidence_path} cannot treat anticipatory articulation "
                f"{name!r} as an attack"
            )
        records.append(
            ArticulationOnset(
                articulation=name,
                frames=int(raw["frames"]),
                sample_rate_hz=int(raw["sample_rate_hz"]),
                context=str(approved["context"]),
                anchor=str(approved["anchor"]),
                evidence=evidence,
            )
        )
    return tuple(records)


def _is_pitched(directory: Path, manifest: dict[str, Any]) -> bool:
    if str(manifest.get("type", "")) in _UNPITCHED_TYPES:
        return False
    calibration = directory / "音准校准.json"
    if calibration.is_file():
        data = json.loads(calibration.read_text(encoding="utf-8"))
        if data.get("applicable") is False:
            return False
    return True


def read_capability(
    manifest_path: str | Path,
    *,
    root: str | Path,
    defer_onset_evidence: bool = False,
) -> InstrumentCapability:
    """Build the capability record for one instrument manifest."""

    path = Path(manifest_path).resolve()
    base = Path(root).resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError(f"instrument manifest root must be an object: {path}")
    directory = path.parent
    instrument_type = str(manifest.get("type", ""))

    raw_articulations = manifest.get("articulations")
    allowed = manifest.get("allowed_articulations")
    if isinstance(raw_articulations, dict) and raw_articulations:
        articulations = tuple(sorted(str(name) for name in raw_articulations))
        source = "manifest.articulations"
    elif isinstance(allowed, list) and allowed:
        articulations = tuple(sorted(str(name) for name in allowed))
        source = "manifest.allowed_articulations"
    else:
        recovered = _load_backend_articulations(instrument_type, manifest)
        local = (
            None
            if recovered is not None
            else _load_local_articulations(directory, manifest)
        )
        if recovered is not None:
            articulations, source = recovered
        elif local is not None:
            articulations = local
            source = f"local:{manifest['implementation']}"
        elif manifest.get("default_articulation") is not None:
            articulations = (str(manifest["default_articulation"]),)
            source = "manifest.default_articulation"
        else:
            articulations = ()
            source = "none"

    default_articulation = manifest.get("default_articulation")
    if default_articulation is not None:
        default_articulation = str(default_articulation)
        if articulations and default_articulation not in articulations:
            articulations = tuple(sorted({*articulations, default_articulation}))
    articulation_auto_default = manifest.get("articulation_auto_default", True)
    if not isinstance(articulation_auto_default, bool):
        raise ValueError(
            f"articulation_auto_default must be boolean: {path}"
        )
    duration_articulation_rules = _read_duration_articulation_rules(
        path,
        manifest,
        articulations,
        default_articulation,
    )

    playable_ranges = _read_playable_ranges(manifest)
    articulation_playable_ranges = _read_articulation_playable_ranges(
        manifest,
        articulations,
    )
    license_status = manifest.get("license_status")
    if license_status is not None:
        license_status = str(license_status)
        if license_status not in {"approved", "grandfathered", "quarantined"}:
            raise ValueError(
                f"invalid license_status {license_status!r}: {path}"
            )
    collaboration_review_status = manifest.get(
        "collaboration_review_status"
    )
    if collaboration_review_status is not None:
        collaboration_review_status = str(collaboration_review_status)
        if (
            collaboration_review_status
            not in COLLABORATION_REVIEW_STATUSES
        ):
            supported = ", ".join(
                sorted(COLLABORATION_REVIEW_STATUSES)
            )
            raise ValueError(
                "invalid collaboration_review_status "
                f"{collaboration_review_status!r}; expected one of "
                f"{supported}: {path}"
            )
    note_min = float(manifest["note_min"]) if "note_min" in manifest else None
    note_max = float(manifest["note_max"]) if "note_max" in manifest else None
    if playable_ranges:
        first_low = playable_ranges[0][0]
        last_high = playable_ranges[-1][1]
        if note_min is None:
            note_min = first_low
        elif first_low < note_min:
            raise ValueError("playable_ranges extend below declared note_min")
        if note_max is None:
            note_max = last_high
        elif last_high > note_max:
            raise ValueError("playable_ranges extend above declared note_max")
    _validate_articulation_playable_ranges(
        articulation_playable_ranges,
        note_min=note_min,
        note_max=note_max,
        playable_ranges=playable_ranges,
    )
    range_profiles, range_base_runtime_configuration = _read_range_profiles(
        manifest,
        articulations,
        default_articulation,
        note_min=note_min,
        note_max=note_max,
        playable_ranges=playable_ranges,
        articulation_playable_ranges=articulation_playable_ranges,
    )
    onset_seconds = _read_onset(directory, manifest)
    onset_overlap_policy = _read_onset_overlap_policy(path, manifest)
    evidence_path = directory / "发音延迟.json"
    project_root = _project_root_for(directory, base)
    articulation_onsets = (
        ()
        if defer_onset_evidence and evidence_path.is_file()
        else _read_articulation_onsets(
            directory,
            path,
            manifest,
            articulations,
            catalogue_root=base,
        )
    )

    return InstrumentCapability(
        name=str(manifest.get("name", directory.name)),
        relative_path=directory.relative_to(base).as_posix(),
        manifest_path=str(path),
        implementation_type=instrument_type,
        pitched=_is_pitched(directory, manifest),
        note_min=note_min,
        note_max=note_max,
        articulations=articulations,
        default_articulation=default_articulation,
        articulation_source=source,
        onset_seconds=onset_seconds,
        quality_tier=(
            str(manifest["quality_tier"]) if "quality_tier" in manifest else None
        ),
        license_status=license_status,
        collaboration_review_status=collaboration_review_status,
        pitch_mode=(str(manifest["pitch_mode"]) if "pitch_mode" in manifest else None),
        fixed_midi_note=(
            float(manifest["fixed_midi_note"]) if "fixed_midi_note" in manifest else None
        ),
        playable_ranges=playable_ranges,
        articulation_playable_ranges=articulation_playable_ranges,
        range_profiles=range_profiles,
        range_base_runtime_configuration=(
            range_base_runtime_configuration
        ),
        articulation_onsets=articulation_onsets,
        onset_evidence_path=(
            str(evidence_path.resolve()) if evidence_path.is_file() else None
        ),
        onset_project_root=(
            str(project_root) if evidence_path.is_file() else None
        ),
        onset_overlap_policy=onset_overlap_policy,
        articulation_auto_default=articulation_auto_default,
        duration_articulation_rules=duration_articulation_rules,
    )


def load_capabilities(
    root: str | Path,
    *,
    defer_onset_evidence: bool = True,
) -> dict[str, InstrumentCapability]:
    """Map every instrument's catalogue-relative path to its capability."""

    base = Path(root).resolve()
    if not base.is_dir():
        raise ValueError(f"instrument catalog does not exist: {base}")
    capabilities: dict[str, InstrumentCapability] = {}
    for path in sorted(base.rglob("乐器.json")):
        capability = read_capability(
            path,
            root=base,
            defer_onset_evidence=defer_onset_evidence,
        )
        capabilities[capability.relative_path] = capability
    return capabilities


def resolve_capability(
    capabilities: dict[str, InstrumentCapability], reference: str
) -> InstrumentCapability:
    """Look an instrument up by catalogue path, or by unique trailing name."""

    normalised = reference.strip().strip("/")
    if normalised in capabilities:
        return capabilities[normalised]
    matches = [
        capability
        for key, capability in capabilities.items()
        if key.rsplit("/", 1)[-1] == normalised
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"编制表引用了不存在的乐器: {reference!r}")
    options = ", ".join(sorted(match.relative_path for match in matches))
    raise ValueError(f"乐器引用 {reference!r} 不唯一,请写完整路径;候选: {options}")
