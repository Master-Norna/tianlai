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
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Any

from .authoring_json import AuthoringJsonLimits, strict_json_loads
from .plain_file import read_plain_file_bytes


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

# Performance ``control`` events currently target one instantiated instrument,
# hence every audited runtime control is part-wide.  Keep ``per_note`` in the
# public vocabulary so score/tooling code can reject it deliberately today and
# adopt it later without inventing a second scope spelling.
CONTROL_SCOPES = frozenset(("part", "per_note"))
CONTROL_KINDS = frozenset(("discrete", "continuous"))
CONTROL_INTERPOLATIONS = frozenset(("step", "linear"))
CONTROL_FIDELITIES = frozenset(("native", "adapted"))
CONTROL_SEMANTIC_FIDELITIES = frozenset(("native", "approximated"))
SEMANTIC_POLICIES = frozenset(("exact", "approximate"))
CONTROL_APPLICATIONS = frozenset(
    ("active_voice_continuous", "note_on_latched", "release_gate")
)
_RUNTIME_CONTROL_SCOPE = "part"
_MAX_CAPABILITY_JSON_BYTES = 16 * 1024 * 1024
_PITCH_MODES = frozenset(("pitched", "fixed", "ignore"))

# This is an intentionally explicit audit table, not a guess based on common
# MIDI CC names.  A name appears only when the corresponding backend's
# ``handle_event`` consumes it (or, for dedicated_fx, forwards it unchanged to
# the audited DedicatedSfzInstrument).  The mapping is kept next to the public
# capability contract so backend/control drift is reviewable in one diff.
_BACKEND_CONTROLS: dict[str, tuple[tuple[str, ...], str]] = {
    "cello": (
        ("expression",),
        "tianlai.cello.CelloInstrument.handle_event",
    ),
    "dedicated_fx": (
        ("expression", "modulation", "sustain_pedal"),
        "tianlai.dedicated_fx.DedicatedFxInstrument.handle_event",
    ),
    "dedicated_sfz": (
        ("expression", "modulation", "sustain_pedal"),
        "tianlai.dedicated_sfz.DedicatedSfzInstrument.handle_event",
    ),
    "flute": (
        ("breath", "expression"),
        "tianlai.flute.FluteInstrument.handle_event",
    ),
    "modeled_bianzhong": (
        ("expression", "modulation"),
        "tianlai.bianzhong.BianzhongInstrument.handle_event",
    ),
    "modeled_instrument": (
        ("expression", "modulation"),
        "tianlai.modeled_instruments.ModeledInstrument.handle_event",
    ),
    "mtg_solo_sax": (
        ("breath", "expression", "modulation", "noise", "sustain_pedal"),
        "tianlai.mtg_sax.MtgSoloSaxInstrument.handle_event",
    ),
    "oscillator": (
        ("sustain_pedal",),
        "tianlai.oscillator.OscillatorInstrument.handle_event",
    ),
    "piano": (
        ("sustain_pedal", "una_corda"),
        "tianlai.piano.PianoInstrument.handle_event",
    ),
    "procedural_sfx": (
        ("distance", "expression", "modulation", "sustain_pedal"),
        "tianlai.procedural_sfx.ProceduralSfxInstrument.handle_event",
    ),
    "soundfont": (
        (
            "breath",
            "expression",
            "modulation",
            "pan",
            "sustain_pedal",
            "volume",
        ),
        "tianlai.soundfont.SoundFontInstrument.handle_event",
    ),
    "synthesizer": (
        ("expression", "modulation", "sustain_pedal"),
        "tianlai.synthesizer.SynthesizerInstrument.handle_event",
    ),
    "violin": (
        ("expression",),
        "tianlai.violin.ViolinInstrument.handle_event",
    ),
    "vpo_brass": (
        ("breath", "expression", "modulation", "sustain_pedal"),
        "tianlai.vpo_brass.VpoBrassInstrument.handle_event",
    ),
    "vpo_celesta": (
        ("expression", "sustain_pedal"),
        "tianlai.vpo_specials.VpoCelestaInstrument.handle_event",
    ),
    "vpo_cowbell": (
        ("expression",),
        "tianlai.vpo_specials.VpoCowbellInstrument.handle_event",
    ),
    "vpo_harp": (
        ("expression", "sustain_pedal"),
        "tianlai.vpo_strings.VpoHarpInstrument.handle_event",
    ),
    "vpo_mixed_choir": (
        ("breath", "expression", "modulation", "sustain_pedal"),
        "tianlai.vpo_specials.VpoMixedChoirInstrument.handle_event",
    ),
    "vpo_orchestral_hit": (
        ("expression",),
        "tianlai.vpo_specials.VpoOrchestralHitInstrument.handle_event",
    ),
    "vpo_percussion": (
        ("expression", "sustain_pedal"),
        "tianlai.vpo_percussion.VpoPercussionInstrument.handle_event",
    ),
    "vpo_solo_string": (
        ("expression", "sustain_pedal"),
        "tianlai.vpo_strings.VpoSoloStringInstrument.handle_event",
    ),
    "vpo_string_section": (
        ("expression", "sustain_pedal"),
        "tianlai.vpo_strings.VpoStringSectionInstrument.handle_event",
    ),
    "vpo_woodwind": (
        ("breath", "expression"),
        "tianlai.vpo_woodwinds.VpoSoloWoodwindInstrument.handle_event",
    ),
    "vsco2_viola_section": (
        ("expression", "sustain_pedal"),
        "tianlai.vsco2_viola.Vsco2ViolaSectionInstrument.handle_event",
    ),
}

_DISCRETE_CONTROLS = frozenset(("sustain_pedal",))

# These implementations either expose a protocol-native damper contract or
# model an instrument which physically has a damper/sustain pedal.  Everything
# else in the audit table implements only a generic delayed-note-off gate and
# must request approximation consent.
_NATIVE_SUSTAIN_TYPES = frozenset(
    ("oscillator", "piano", "soundfont", "synthesizer", "vpo_celesta")
)
_NATIVE_DEDICATED_SUSTAIN_PROFILES = frozenset(
    (
        "greg_sullivan_cp80_dedicated_multisample_bandlimited",
        "greg_sullivan_cp80_dedicated_multisample_fx_chain_bandlimited",
        "vcsl_vibraphone_strict_cc0_two_mallets_bowed",
    )
)
_NATIVE_VPO_PERCUSSION_SUSTAIN_PROFILES = frozenset(
    ("tubular_bells", "vcsl_tubular_bells_2", "vibraphone")
)
_GAIN_ONLY_BREATH_TYPES = frozenset(
    ("flute", "vpo_brass", "vpo_mixed_choir", "vpo_woodwind")
)

_BACKEND_RELEASE_VELOCITY: dict[str, str] = {
    "mtg_solo_sax": "tianlai.mtg_sax.MtgSoloSaxInstrument.handle_event",
}


# Note-on velocity is a per-note, note-on-latched parameter rather than a
# runtime ``control`` lane.  Keep a separate audit table so a backend cannot
# acquire a velocity claim merely because the performance protocol accepts a
# float.  Every entry below was traced through the named ``handle_event`` path
# to an audible consumer.  Missing entries remain undeclared and fail closed.
NOTE_VELOCITY_FIDELITIES = frozenset(("native", "adapted", "ignored"))
NOTE_VELOCITY_SEMANTIC_FIDELITIES = frozenset(
    ("native", "approximated", "ignored")
)
NOTE_VELOCITY_ZERO_BEHAVIORS = frozenset(
    ("silent", "minimum_nonzero", "audible_baseline")
)

# Score v2 needs a different answer from the legacy ``pitched`` flag.  The
# flag only says whether catalogue range checks are meaningful; it does not
# prove how a note-on pitch reaches the DSP.  These vocabularies describe that
# separately, in the same way note-on velocity is kept separate from controls.
NOTE_PITCH_MODES = frozenset(("continuous", "quantized", "fixed", "selector"))
NOTE_PITCH_FIDELITIES = frozenset(("native", "adapted", "ignored"))
NOTE_PITCH_SEMANTIC_FIDELITIES = frozenset(
    ("native", "approximated", "ignored")
)
NOTE_PITCH_PROTOCOL_INPUTS = frozenset(
    ("midi_note", "pitch_hz", "midi_note_or_pitch_hz")
)
NOTE_PITCH_VALUE_UNITS = frozenset(
    ("midi_note_at_a4_440", "event_midi_note")
)
NOTE_PITCH_APPLICATION = "note_on_latched"
_SELECTOR_ROUNDINGS = frozenset(("nearest_lower_tie", "python_nearest_even"))

ARTICULATION_EXECUTION_APPLICATION = "note_on_latched"
ARTICULATION_EXECUTION_FIDELITIES = frozenset(("native", "ignored"))
ARTICULATION_EXECUTION_SEMANTIC_FIDELITIES = frozenset(
    ("native", "approximated", "ignored")
)
_BACKEND_NOTE_VELOCITY: dict[str, str] = {
    "cello": "tianlai.cello.CelloInstrument.handle_event",
    "dedicated_fx": (
        "tianlai.dedicated_fx.DedicatedFxInstrument.handle_event -> "
        "tianlai.dedicated_sfz.DedicatedSfzInstrument._playback_payload"
    ),
    "dedicated_sfz": (
        "tianlai.dedicated_sfz.DedicatedSfzInstrument._playback_payload"
    ),
    "flute": "tianlai.flute.FluteInstrument.handle_event",
    "melodic_toms": "tianlai.melodic_toms.MelodicTomsInstrument.handle_event",
    "modeled_bianzhong": "tianlai.bianzhong.BianzhongInstrument.handle_event",
    "modeled_instrument": (
        "tianlai.modeled_instruments.ModeledInstrument.handle_event"
    ),
    "mtg_solo_sax": "tianlai.mtg_sax.MtgSoloSaxInstrument.handle_event",
    "oscillator": "tianlai.oscillator.OscillatorInstrument.handle_event",
    "piano": "tianlai.piano.PianoInstrument.handle_event",
    "procedural_sfx": (
        "tianlai.procedural_sfx.ProceduralSfxInstrument.handle_event"
    ),
    "reversed_cymbal": (
        "tianlai.reversed_cymbal.ReversedCymbalInstrument.handle_event"
    ),
    "soundfont": "tianlai.soundfont.SoundFontInstrument.handle_event",
    "sample": "tianlai.sampler.SampleInstrument.handle_event",
    "synthesizer": "tianlai.synthesizer.SynthesizerInstrument.handle_event",
    "violin": "tianlai.violin.ViolinInstrument.handle_event",
    "vpo_brass": "tianlai.vpo_brass.VpoBrassInstrument.handle_event",
    "vpo_celesta": (
        "tianlai.vpo_specials.VpoCelestaInstrument.handle_event"
    ),
    "vpo_cowbell": (
        "tianlai.vpo_specials.VpoCowbellInstrument.handle_event"
    ),
    "vpo_harp": "tianlai.vpo_strings.VpoHarpInstrument.handle_event",
    "vpo_mixed_choir": (
        "tianlai.vpo_specials.VpoMixedChoirInstrument.handle_event"
    ),
    "vpo_orchestral_hit": (
        "tianlai.vpo_specials.VpoOrchestralHitInstrument.handle_event"
    ),
    "vpo_percussion": (
        "tianlai.vpo_percussion.VpoPercussionInstrument.handle_event"
    ),
    "vpo_solo_string": (
        "tianlai.vpo_strings.VpoSoloStringInstrument.handle_event"
    ),
    "vpo_string_section": (
        "tianlai.vpo_strings.VpoStringSectionInstrument.handle_event"
    ),
    "vpo_woodwind": (
        "tianlai.vpo_woodwinds.VpoSoloWoodwindInstrument.handle_event"
    ),
    "vsco2_viola_section": (
        "tianlai.vsco2_viola.Vsco2ViolaSectionInstrument.handle_event"
    ),
}

# This table was traced from note-on input to the audible pitch consumer.  It
# deliberately excludes procedural_sfx and other implementations which merely
# accept the common event shape.  Profile-specific selector/fixed cases are
# handled in ``_read_note_pitch`` below and cannot inherit a continuous claim
# from this table.
_BACKEND_NOTE_PITCH: dict[str, str] = {
    "cello": (
        "tianlai.cello.CelloInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "dedicated_fx": (
        "tianlai.dedicated_fx.DedicatedFxInstrument.handle_event -> "
        "tianlai.dedicated_sfz.DedicatedSfzInstrument._playback_payload -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "dedicated_sfz": (
        "tianlai.dedicated_sfz.DedicatedSfzInstrument._playback_payload -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "flute": (
        "tianlai.flute.FluteInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "melodic_toms": (
        "tianlai.melodic_toms.MelodicTomsInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "modeled_bianzhong": "tianlai.bianzhong.BianzhongInstrument.handle_event",
    "modeled_instrument": (
        "tianlai.modeled_instruments.ModeledInstrument.handle_event"
    ),
    "mtg_solo_sax": (
        "tianlai.mtg_sax.MtgSoloSaxInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "oscillator": "tianlai.oscillator.OscillatorInstrument.handle_event",
    "piano": (
        "tianlai.piano.PianoInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "sample": "tianlai.sampler.SampleInstrument.handle_event",
    "soundfont": "tianlai.soundfont.SoundFontInstrument.handle_event",
    "synthesizer": (
        "tianlai.synthesizer.SynthesizerInstrument.handle_event"
    ),
    "violin": (
        "tianlai.violin.ViolinInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_brass": (
        "tianlai.vpo_brass.VpoBrassInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_celesta": (
        "tianlai.vpo_specials.VpoCelestaInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_harp": (
        "tianlai.vpo_strings.VpoHarpInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_mixed_choir": (
        "tianlai.vpo_specials.VpoMixedChoirInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_orchestral_hit": (
        "tianlai.vpo_specials.VpoOrchestralHitInstrument.handle_event"
    ),
    "vpo_percussion": (
        "tianlai.vpo_percussion.VpoPercussionInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_solo_string": (
        "tianlai.vpo_strings.VpoSoloStringInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_string_section": (
        "tianlai.vpo_strings.VpoStringSectionInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vpo_woodwind": (
        "tianlai.vpo_woodwinds.VpoSoloWoodwindInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
    "vsco2_viola_section": (
        "tianlai.vsco2_viola.Vsco2ViolaSectionInstrument.handle_event -> "
        "tianlai.sampler.SampleInstrument.handle_event"
    ),
}

_NOTE_PITCH_SEMANTIC_APPROXIMATIONS: dict[str, str] = {
    "vpo_orchestral_hit": (
        "the requested pitch tunes the tonal string and brass layers, while "
        "the bass-drum and cymbal layers remain fixed; the composite event "
        "therefore has no single native sounding-pitch meaning"
    ),
}

# A vocabulary is not execution evidence.  Every entry below was checked for
# all three links: an articulation event is consumed, it updates runtime state,
# and the following note-on reads that state when choosing an audible route.
# Static one-articulation validators (currently VPO cowbell/orchestral hit) are
# intentionally absent because they do not latch any runtime state.
_BACKEND_ARTICULATION_EXECUTION: dict[str, str] = {
    "cello": "tianlai.cello.CelloInstrument.handle_event",
    "dedicated_fx": (
        "tianlai.dedicated_fx.DedicatedFxInstrument.handle_event -> "
        "tianlai.dedicated_sfz.DedicatedSfzInstrument.handle_event"
    ),
    "dedicated_sfz": (
        "tianlai.dedicated_sfz.DedicatedSfzInstrument.handle_event"
    ),
    "flute": "tianlai.flute.FluteInstrument.handle_event",
    "modeled_bianzhong": "tianlai.bianzhong.BianzhongInstrument.handle_event",
    "mtg_solo_sax": "tianlai.mtg_sax.MtgSoloSaxInstrument.handle_event",
    "soundfont": "tianlai.soundfont.SoundFontInstrument.handle_event",
    "violin": "tianlai.violin.ViolinInstrument.handle_event",
    "vpo_brass": "tianlai.vpo_brass.VpoBrassInstrument.handle_event",
    "vpo_harp": "tianlai.vpo_strings.VpoHarpInstrument.handle_event",
    "vpo_mixed_choir": (
        "tianlai.vpo_specials.VpoMixedChoirInstrument.handle_event"
    ),
    "vpo_percussion": (
        "tianlai.vpo_percussion.VpoPercussionInstrument.handle_event"
    ),
    "vpo_solo_string": (
        "tianlai.vpo_strings.VpoSoloStringInstrument.handle_event"
    ),
    "vpo_string_section": (
        "tianlai.vpo_strings.VpoStringSectionInstrument.handle_event"
    ),
    "vpo_woodwind": (
        "tianlai.vpo_woodwinds.VpoSoloWoodwindInstrument.handle_event"
    ),
    "vsco2_viola_section": (
        "tianlai.vsco2_viola.Vsco2ViolaSectionInstrument.handle_event"
    ),
}

# These backends audibly consume every finite float, but their authored
# velocity meaning is deliberately narrower than a physical strike/blow model.
# Keep that semantic limitation orthogonal to numeric resolution.
_NOTE_VELOCITY_APPROXIMATIONS: dict[str, str] = {
    "cello": (
        "the retained sample material does not provide an audited set of "
        "recorded dynamic layers; velocity continuously scales playback gain"
    ),
    "flute": (
        "the retained sample material does not provide an audited set of "
        "recorded dynamic layers; velocity continuously scales playback gain"
    ),
    "oscillator": (
        "velocity only scales oscillator amplitude through velocity_exponent; "
        "it does not model velocity-dependent timbre or recorded dynamic layers"
    ),
    "procedural_sfx": (
        "velocity is an intensity gain and deterministic noise seed, not a "
        "profile-specific physical gesture model"
    ),
    "reversed_cymbal": (
        "velocity only scales the selected reversed sample amplitude; it does "
        "not select or synthesize a velocity-dependent strike timbre"
    ),
    "sample": (
        "the generic sampler preserves continuous playback gain, but velocity "
        "timbre and layer semantics depend on an unaudited manifest"
    ),
    "violin": (
        "the retained sample material does not provide an audited set of "
        "recorded dynamic layers; velocity continuously scales playback gain"
    ),
    "vpo_brass": (
        "the current VPO importer collapses authored velocity crossfades to "
        "midpoint-selected sample layers while preserving continuous gain"
    ),
    "vpo_harp": (
        "the current VPO importer collapses authored velocity crossfades to "
        "midpoint-selected sample layers while preserving continuous gain"
    ),
    "vpo_mixed_choir": (
        "velocity continuously affects gain and attack, but the retained choir "
        "material does not expose an audited physical dynamic-layer response"
    ),
    "vpo_orchestral_hit": (
        "the composite orchestral-hit backend mixes crossfaded components with "
        "a hard-switched cymbal layer, so one native velocity meaning cannot be "
        "claimed for the whole event"
    ),
    "vpo_percussion": (
        "the current VPO importer collapses authored velocity crossfades to "
        "midpoint-selected sample layers while preserving continuous gain"
    ),
    "vpo_solo_string": (
        "the current VPO importer collapses authored velocity crossfades to "
        "midpoint-selected sample layers while preserving continuous gain"
    ),
    "vpo_string_section": (
        "the current VPO importer collapses authored velocity crossfades to "
        "midpoint-selected sample layers while preserving continuous gain"
    ),
    "vpo_woodwind": (
        "the current VPO importer collapses authored velocity crossfades to "
        "midpoint-selected sample layers while preserving continuous gain"
    ),
    "vsco2_viola_section": (
        "the retained VSCO2 viola material has one recorded velocity tier per "
        "articulation; velocity continuously scales amplitude but cannot select "
        "a different recorded dynamic"
    ),
}


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
class ControlCapability:
    """One explicitly audited score-to-runtime control contract.

    ``kind`` describes the value semantics, independently of interpolation.
    The current performance protocol delivers timestamped point events, so the
    audited contracts expose only ``step`` even where a backend smooths its
    target internally.  Discrete controls still use a numeric payload, but
    list their exactly representable values.  Per-note controls are reserved
    in the vocabulary and rejected until the runtime can route them without
    affecting a whole instrument instance.  ``application`` distinguishes a
    value that keeps affecting active voices from one sampled only at note-on
    or used solely as a release gate.
    """

    name: str
    scope: str
    kind: str
    minimum: float
    maximum: float
    default_value: float
    interpolations: tuple[str, ...]
    application: str
    fidelity: str
    semantic_fidelity: str
    approximation_reason: str | None
    steps: int | None
    quantization_exponent: float | None
    allowed_values: tuple[float, ...] | None
    source: str
    applicable_articulations: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("control name must be a non-empty string")
        if self.name != self.name.strip():
            raise ValueError("control name must not contain surrounding whitespace")
        if self.scope not in CONTROL_SCOPES:
            supported = ", ".join(sorted(CONTROL_SCOPES))
            raise ValueError(
                f"control scope must be one of {supported}; got {self.scope!r}"
            )
        if self.scope != _RUNTIME_CONTROL_SCOPE:
            raise ValueError(
                "per-note control capability is not implemented by the runtime"
            )
        if self.kind not in CONTROL_KINDS:
            supported = ", ".join(sorted(CONTROL_KINDS))
            raise ValueError(
                f"control kind must be one of {supported}; got {self.kind!r}"
            )
        for field, value in (
            ("minimum", self.minimum),
            ("maximum", self.maximum),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"control {field} must be a finite number")
        minimum = float(self.minimum)
        maximum = float(self.maximum)
        if minimum >= maximum:
            raise ValueError("control minimum must be less than maximum")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)
        if (
            isinstance(self.default_value, bool)
            or not isinstance(self.default_value, (int, float))
            or not math.isfinite(float(self.default_value))
        ):
            raise ValueError("control default_value must be a finite number")
        default_value = float(self.default_value)
        if not minimum <= default_value <= maximum:
            raise ValueError("control default_value must be inside its declared range")
        object.__setattr__(self, "default_value", default_value)

        if not isinstance(self.interpolations, tuple) or not self.interpolations:
            raise ValueError("control interpolations must be a non-empty tuple")
        if len(set(self.interpolations)) != len(self.interpolations):
            raise ValueError("control interpolations must not contain duplicates")
        unsupported = set(self.interpolations) - CONTROL_INTERPOLATIONS
        if unsupported:
            supported = ", ".join(sorted(CONTROL_INTERPOLATIONS))
            raise ValueError(
                "control interpolations must be chosen from "
                f"{supported}; got {sorted(unsupported)!r}"
            )
        if "step" not in self.interpolations:
            raise ValueError("every control must allow step interpolation")
        if self.interpolations != ("step",):
            raise ValueError(
                "the current runtime only supports step control interpolation"
            )
        if self.application not in CONTROL_APPLICATIONS:
            supported = ", ".join(sorted(CONTROL_APPLICATIONS))
            raise ValueError(
                "control application must be one of "
                f"{supported}; got {self.application!r}"
            )
        if self.fidelity not in CONTROL_FIDELITIES:
            supported = ", ".join(sorted(CONTROL_FIDELITIES))
            raise ValueError(
                f"control fidelity must be one of {supported}; got {self.fidelity!r}"
            )
        if self.semantic_fidelity not in CONTROL_SEMANTIC_FIDELITIES:
            supported = ", ".join(sorted(CONTROL_SEMANTIC_FIDELITIES))
            raise ValueError(
                "control semantic_fidelity must be one of "
                f"{supported}; got {self.semantic_fidelity!r}"
            )
        if self.semantic_fidelity == "approximated":
            if (
                not isinstance(self.approximation_reason, str)
                or not self.approximation_reason.strip()
            ):
                raise ValueError(
                    "approximated control semantics require approximation_reason"
                )
        elif self.approximation_reason is not None:
            raise ValueError(
                "native control semantics must not declare approximation_reason"
            )
        if self.steps is not None and (
            isinstance(self.steps, bool)
            or not isinstance(self.steps, int)
            or self.steps < 2
        ):
            raise ValueError("control steps must be null or an integer of at least 2")
        if self.fidelity == "adapted" and self.steps is None:
            raise ValueError("adapted control fidelity requires a finite step count")
        if self.fidelity == "native" and self.steps is not None:
            raise ValueError("native control fidelity must not declare quantized steps")
        if self.quantization_exponent is not None:
            if (
                isinstance(self.quantization_exponent, bool)
                or not isinstance(self.quantization_exponent, (int, float))
                or not math.isfinite(float(self.quantization_exponent))
                or float(self.quantization_exponent) <= 0.0
            ):
                raise ValueError(
                    "control quantization_exponent must be null or positive and finite"
                )
            object.__setattr__(
                self,
                "quantization_exponent",
                float(self.quantization_exponent),
            )
        if self.kind == "continuous" and self.fidelity == "adapted":
            if self.quantization_exponent is None:
                raise ValueError(
                    "adapted continuous controls require quantization_exponent"
                )
        elif self.quantization_exponent is not None:
            raise ValueError(
                "only adapted continuous controls may declare quantization_exponent"
            )
        if self.allowed_values is not None:
            if (
                not isinstance(self.allowed_values, tuple)
                or not self.allowed_values
            ):
                raise ValueError(
                    "control allowed_values must be null or a non-empty tuple"
                )
            normalised_values: list[float] = []
            for value in self.allowed_values:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise ValueError("control allowed_values must be finite numbers")
                number = float(value)
                if not minimum <= number <= maximum:
                    raise ValueError(
                        "control allowed_values must be inside its declared range"
                    )
                normalised_values.append(number)
            if normalised_values != sorted(set(normalised_values)):
                raise ValueError(
                    "control allowed_values must be unique and increasing"
                )
            object.__setattr__(self, "allowed_values", tuple(normalised_values))
            if self.kind != "discrete":
                raise ValueError(
                    "only discrete controls may declare explicit allowed_values"
                )
            if self.steps != len(normalised_values):
                raise ValueError(
                    "control steps must equal the number of allowed_values"
                )
            if default_value not in normalised_values:
                raise ValueError(
                    "discrete control default_value must be an allowed value"
                )
        elif self.kind == "discrete":
            raise ValueError("discrete controls must declare allowed_values")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("control source must be a non-empty string")
        if self.applicable_articulations is not None:
            if (
                not isinstance(self.applicable_articulations, tuple)
                or not self.applicable_articulations
            ):
                raise ValueError(
                    "control applicable_articulations must be null or a "
                    "non-empty tuple"
                )
            if any(
                not isinstance(item, str)
                or not item.strip()
                or item != item.strip()
                for item in self.applicable_articulations
            ):
                raise ValueError(
                    "control applicable_articulations must contain non-empty "
                    "names without surrounding whitespace"
                )
            if self.applicable_articulations != tuple(
                sorted(set(self.applicable_articulations))
            ):
                raise ValueError(
                    "control applicable_articulations must be unique and sorted"
                )
        # Sparse lanes inherit this value before their first point.  It must
        # therefore be exactly realizable, not merely inside the source range.
        self.require_value(self.default_value)

    def require_semantic_policy(self, policy: str) -> None:
        """Require explicit consent before using semantic approximation."""

        if policy not in SEMANTIC_POLICIES:
            choices = ", ".join(sorted(SEMANTIC_POLICIES))
            raise ValueError(
                f"control semantic policy must be one of {choices}; got {policy!r}"
            )
        if self.semantic_fidelity == "approximated" and policy != "approximate":
            raise ValueError(
                f"control {self.name!r} requires semantic_policy='approximate': "
                f"{self.approximation_reason}"
            )

    def require_value(self, value: Any) -> float:
        """Return a finite in-range value, or fail closed for preflight."""

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"control {self.name!r} value must be a finite number")
        number = float(value)
        if not self.minimum <= number <= self.maximum:
            raise ValueError(
                f"control {self.name!r} value must be between "
                f"{self.minimum:g} and {self.maximum:g}; got {number:g}"
            )
        if self.allowed_values is not None:
            if number not in self.allowed_values:
                choices = ", ".join(f"{item:g}" for item in self.allowed_values)
                raise ValueError(
                    f"control {self.name!r} value {number:g} is not exactly "
                    f"representable; choose from {choices} or adapt explicitly"
                )
        elif self.steps is not None:
            # ``exact`` means the float is already the precise preimage that
            # the audited backend mapping will consume.  A tolerance around
            # an integer grid point would accept authored values which the
            # backend nevertheless changes.  Reconstruct through the same
            # mapping as ``adapt_value`` and require exact float identity;
            # values returned by ``adapt_value`` are therefore guaranteed to
            # pass this gate on a later validation pass.
            if self.adapt_value(number) != number:
                raise ValueError(
                    f"control {self.name!r} value {number:g} is not exactly "
                    f"representable at {self.steps}-step fidelity; adapt explicitly"
                )
        return number

    def adapt_value(self, value: Any) -> float:
        """Explicitly quantize an in-range value to this runtime contract."""

        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"control {self.name!r} value must be a finite number")
        number = float(value)
        if not self.minimum <= number <= self.maximum:
            raise ValueError(
                f"control {self.name!r} value must be between "
                f"{self.minimum:g} and {self.maximum:g}; got {number:g}"
            )
        if self.allowed_values is not None:
            return min(
                self.allowed_values,
                key=lambda candidate: (abs(candidate - number), -candidate),
            )
        if self.steps is not None:
            assert self.quantization_exponent is not None
            normalised = (
                (number - self.minimum) / (self.maximum - self.minimum)
            )
            position = normalised**self.quantization_exponent * (self.steps - 1)
            # The audited quantized backends use Python's nearest-even round.
            index = round(position)
            adapted_normalised = (
                index / (self.steps - 1)
            ) ** (1.0 / self.quantization_exponent)
            return self.minimum + (
                (self.maximum - self.minimum) * adapted_normalised
            )
        return number

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "kind": self.kind,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "default_value": self.default_value,
            "interpolations": list(self.interpolations),
            "application": self.application,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "approximation_reason": self.approximation_reason,
            "steps": self.steps,
            "quantization_exponent": self.quantization_exponent,
            "allowed_values": (
                None
                if self.allowed_values is None
                else list(self.allowed_values)
            ),
            "source": self.source,
            "applicable_articulations": (
                None
                if self.applicable_articulations is None
                else list(self.applicable_articulations)
            ),
        }


@dataclass(frozen=True, slots=True)
class NoteVelocityResolution:
    """One requested note-on velocity resolved against an audited backend."""

    requested_value: float
    resolved_value: float
    adapted: bool
    fidelity: str
    semantic_fidelity: str
    approximation_reason: str | None
    steps: int | None
    quantization_exponent: float | None
    quantization_output_range: tuple[int, int] | None
    zero_behavior: str
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_value": self.requested_value,
            "resolved_value": self.resolved_value,
            "adapted": self.adapted,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "approximation_reason": self.approximation_reason,
            "steps": self.steps,
            "quantization_exponent": self.quantization_exponent,
            "quantization_output_range": (
                None
                if self.quantization_output_range is None
                else list(self.quantization_output_range)
            ),
            "zero_behavior": self.zero_behavior,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class NoteVelocityCapability:
    """Numeric and semantic fidelity of one backend's note-on velocity.

    ``native`` means every protocol float in ``[0, 1]`` reaches an audible
    continuous consumer without an explicit input grid.  ``adapted`` means the
    backend snaps to a finite output grid; ``quantization_output_range`` names
    the inclusive integer range and ``quantization_exponent`` names the power
    applied before rounding.  ``ignored`` is reserved for an audited backend
    which accepts but cannot audibly observe velocity.

    Semantic fidelity is independent: amplitude-only synthesis can preserve
    every input float while still approximating a performer's physical
    velocity gesture.
    """

    fidelity: str
    semantic_fidelity: str
    approximation_reason: str | None
    steps: int | None
    quantization_exponent: float | None
    quantization_output_range: tuple[int, int] | None
    source: str
    minimum: float = 0.0
    maximum: float = 1.0
    zero_behavior: str = "silent"

    def __post_init__(self) -> None:
        if self.fidelity not in NOTE_VELOCITY_FIDELITIES:
            choices = ", ".join(sorted(NOTE_VELOCITY_FIDELITIES))
            raise ValueError(
                f"note velocity fidelity must be one of {choices}; "
                f"got {self.fidelity!r}"
            )
        if self.semantic_fidelity not in NOTE_VELOCITY_SEMANTIC_FIDELITIES:
            choices = ", ".join(sorted(NOTE_VELOCITY_SEMANTIC_FIDELITIES))
            raise ValueError(
                "note velocity semantic_fidelity must be one of "
                f"{choices}; got {self.semantic_fidelity!r}"
            )
        for field, value, expected in (
            ("minimum", self.minimum, 0.0),
            ("maximum", self.maximum, 1.0),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not math.isclose(
                    float(value),
                    expected,
                    rel_tol=0.0,
                    abs_tol=0.0,
                )
            ):
                raise ValueError(
                    f"note velocity {field} must be the protocol bound {expected:g}"
                )
            object.__setattr__(self, field, expected)
        if self.fidelity == "ignored":
            if self.semantic_fidelity != "ignored":
                raise ValueError(
                    "ignored note velocity requires ignored semantic_fidelity"
                )
        elif self.semantic_fidelity == "ignored":
            raise ValueError(
                "ignored semantic_fidelity requires ignored numeric fidelity"
            )
        if self.semantic_fidelity in {"approximated", "ignored"}:
            if (
                not isinstance(self.approximation_reason, str)
                or not self.approximation_reason.strip()
            ):
                raise ValueError(
                    "non-native note velocity semantics require "
                    "approximation_reason"
                )
        elif self.approximation_reason is not None:
            raise ValueError(
                "native note velocity semantics must not declare "
                "approximation_reason"
            )
        if self.zero_behavior not in NOTE_VELOCITY_ZERO_BEHAVIORS:
            choices = ", ".join(sorted(NOTE_VELOCITY_ZERO_BEHAVIORS))
            raise ValueError(
                "note velocity zero_behavior must be one of "
                f"{choices}; got {self.zero_behavior!r}"
            )
        if (
            self.zero_behavior == "minimum_nonzero"
            and self.fidelity != "adapted"
        ):
            raise ValueError(
                "minimum_nonzero note velocity requires adapted fidelity"
            )

        if self.fidelity == "adapted":
            if (
                isinstance(self.steps, bool)
                or not isinstance(self.steps, int)
                or self.steps < 2
            ):
                raise ValueError(
                    "adapted note velocity requires at least two output steps"
                )
            if (
                isinstance(self.quantization_exponent, bool)
                or not isinstance(self.quantization_exponent, (int, float))
                or not math.isfinite(float(self.quantization_exponent))
                or float(self.quantization_exponent) <= 0.0
            ):
                raise ValueError(
                    "adapted note velocity requires a positive finite "
                    "quantization_exponent"
                )
            object.__setattr__(
                self,
                "quantization_exponent",
                float(self.quantization_exponent),
            )
            output_range = self.quantization_output_range
            if (
                not isinstance(output_range, tuple)
                or len(output_range) != 2
                or any(
                    isinstance(item, bool) or not isinstance(item, int)
                    for item in output_range
                )
            ):
                raise ValueError(
                    "adapted note velocity requires an integer "
                    "quantization_output_range"
                )
            low, high = output_range
            if low < 0 or high <= 0 or low > high:
                raise ValueError(
                    "note velocity quantization_output_range must be a "
                    "non-negative increasing range with a positive maximum"
                )
            if high - low + 1 != self.steps:
                raise ValueError(
                    "note velocity steps must equal the inclusive "
                    "quantization_output_range size"
                )
            if self.zero_behavior == "minimum_nonzero" and low == 0:
                raise ValueError(
                    "minimum_nonzero note velocity requires a positive "
                    "quantization output minimum"
                )
        elif any(
            value is not None
            for value in (
                self.steps,
                self.quantization_exponent,
                self.quantization_output_range,
            )
        ):
            raise ValueError(
                "only adapted note velocity may declare a quantization grid"
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("note velocity source must be a non-empty string")

    def require_semantic_policy(self, policy: str) -> None:
        """Require explicit consent before using semantic approximation."""

        if policy not in SEMANTIC_POLICIES:
            choices = ", ".join(sorted(SEMANTIC_POLICIES))
            raise ValueError(
                "note velocity semantic policy must be one of "
                f"{choices}; got {policy!r}"
            )
        if self.semantic_fidelity == "approximated" and policy != "approximate":
            raise ValueError(
                "note velocity requires semantic_policy='approximate': "
                f"{self.approximation_reason}"
            )

    def _checked_value(self, value: Any) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("note velocity must be a finite number")
        number = float(value)
        if not self.minimum <= number <= self.maximum:
            raise ValueError("note velocity must be between 0 and 1")
        if self.fidelity == "ignored":
            raise ValueError(
                "note velocity is accepted by the protocol but ignored by "
                "this backend"
            )
        return number

    def _quantized_index(self, number: float) -> tuple[float, int]:
        assert self.quantization_exponent is not None
        assert self.quantization_output_range is not None
        low, high = self.quantization_output_range
        position = number**self.quantization_exponent * high
        return position, max(low, min(high, round(position)))

    def require_value(self, value: Any) -> float:
        """Require an exactly representable velocity and return it unchanged."""

        number = self._checked_value(value)
        if self.fidelity == "adapted" and self.adapt_value(number) != number:
            assert self.steps is not None
            raise ValueError(
                f"note velocity {number:g} is not exactly representable "
                f"at {self.steps}-step fidelity; adapt explicitly"
            )
        return number

    def adapt_value(self, value: Any) -> float:
        """Resolve a velocity onto the backend's exact input preimage grid."""

        number = self._checked_value(value)
        if self.fidelity != "adapted":
            return number
        _position, index = self._quantized_index(number)
        assert self.quantization_exponent is not None
        assert self.quantization_output_range is not None
        high = self.quantization_output_range[1]
        return (index / high) ** (1.0 / self.quantization_exponent)

    def resolve(self, value: Any, *, adapt: bool) -> NoteVelocityResolution:
        requested = self._checked_value(value)
        resolved = self.adapt_value(requested) if adapt else self.require_value(
            requested
        )
        return NoteVelocityResolution(
            requested_value=requested,
            resolved_value=resolved,
            adapted=requested != resolved,
            fidelity=self.fidelity,
            semantic_fidelity=self.semantic_fidelity,
            approximation_reason=self.approximation_reason,
            steps=self.steps,
            quantization_exponent=self.quantization_exponent,
            quantization_output_range=self.quantization_output_range,
            zero_behavior=self.zero_behavior,
            source=self.source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": "per_note",
            "kind": "continuous",
            "application": "note_on_latched",
            "minimum": self.minimum,
            "maximum": self.maximum,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "approximation_reason": self.approximation_reason,
            "steps": self.steps,
            "quantization_exponent": self.quantization_exponent,
            "quantization_output_range": (
                None
                if self.quantization_output_range is None
                else list(self.quantization_output_range)
            ),
            "zero_behavior": self.zero_behavior,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class NotePitchResolution:
    """One score pitch resolved against an audited note-on pitch path.

    Values use ``value_unit`` from the capability.  Continuous/quantized
    sounding pitch uses the physical A4=440 MIDI coordinate; fixed and
    selector contracts use the incoming event's MIDI coordinate because those
    numbers are control tokens rather than an acoustic-frequency assertion.
    """

    requested_value: float
    resolved_value: float
    adapted: bool
    application: str
    protocol_input: str
    value_unit: str
    mode: str
    fidelity: str
    semantic_fidelity: str
    numeric_approximation_reason: str | None
    semantic_approximation_reason: str | None
    quantization_steps_per_direction: int | None
    pitch_bend_range_semitones: float | None
    allowed_values: tuple[float, ...] | None
    fixed_midi_note: float | None
    selector_rounding: str | None
    source: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_value": self.requested_value,
            "resolved_value": self.resolved_value,
            "adapted": self.adapted,
            "application": self.application,
            "protocol_input": self.protocol_input,
            "value_unit": self.value_unit,
            "mode": self.mode,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "numeric_approximation_reason": self.numeric_approximation_reason,
            "semantic_approximation_reason": self.semantic_approximation_reason,
            "quantization_steps_per_direction": (
                self.quantization_steps_per_direction
            ),
            "pitch_bend_range_semitones": self.pitch_bend_range_semitones,
            "allowed_values": (
                None if self.allowed_values is None else list(self.allowed_values)
            ),
            "fixed_midi_note": self.fixed_midi_note,
            "selector_rounding": self.selector_rounding,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class NotePitchCapability:
    """Audited note-on pitch transport and musical meaning.

    Numeric fidelity is deliberately independent from semantic fidelity.  A
    selector can preserve its input float perfectly while using it only to
    choose a sample; a SoundFont can interpret pitch musically but snap it to
    its key-plus-bend grid.  Neither case may masquerade as continuous native
    sounding pitch.
    """

    application: str
    protocol_input: str
    value_unit: str
    mode: str
    fidelity: str
    semantic_fidelity: str
    numeric_approximation_reason: str | None
    semantic_approximation_reason: str | None
    source: str
    quantization_steps_per_direction: int | None = None
    pitch_bend_range_semitones: float | None = None
    allowed_values: tuple[float, ...] | None = None
    fixed_midi_note: float | None = None
    selector_rounding: str | None = None

    def __post_init__(self) -> None:
        if self.application != NOTE_PITCH_APPLICATION:
            raise ValueError(
                "note pitch application must be 'note_on_latched'"
            )
        if self.protocol_input not in NOTE_PITCH_PROTOCOL_INPUTS:
            choices = ", ".join(sorted(NOTE_PITCH_PROTOCOL_INPUTS))
            raise ValueError(
                f"note pitch protocol_input must be one of {choices}; "
                f"got {self.protocol_input!r}"
            )
        if self.value_unit not in NOTE_PITCH_VALUE_UNITS:
            choices = ", ".join(sorted(NOTE_PITCH_VALUE_UNITS))
            raise ValueError(
                f"note pitch value_unit must be one of {choices}; "
                f"got {self.value_unit!r}"
            )
        if self.mode not in NOTE_PITCH_MODES:
            choices = ", ".join(sorted(NOTE_PITCH_MODES))
            raise ValueError(
                f"note pitch mode must be one of {choices}; got {self.mode!r}"
            )
        if self.fidelity not in NOTE_PITCH_FIDELITIES:
            choices = ", ".join(sorted(NOTE_PITCH_FIDELITIES))
            raise ValueError(
                f"note pitch fidelity must be one of {choices}; "
                f"got {self.fidelity!r}"
            )
        if self.semantic_fidelity not in NOTE_PITCH_SEMANTIC_FIDELITIES:
            choices = ", ".join(sorted(NOTE_PITCH_SEMANTIC_FIDELITIES))
            raise ValueError(
                "note pitch semantic_fidelity must be one of "
                f"{choices}; got {self.semantic_fidelity!r}"
            )
        if self.fidelity == "native":
            if self.numeric_approximation_reason is not None:
                raise ValueError(
                    "native note pitch fidelity must not declare a numeric "
                    "approximation reason"
                )
        elif (
            not isinstance(self.numeric_approximation_reason, str)
            or not self.numeric_approximation_reason.strip()
        ):
            raise ValueError(
                "adapted or ignored note pitch fidelity requires a numeric "
                "approximation reason"
            )
        if self.semantic_fidelity == "native":
            if self.semantic_approximation_reason is not None:
                raise ValueError(
                    "native note pitch semantics must not declare a semantic "
                    "approximation reason"
                )
        elif (
            not isinstance(self.semantic_approximation_reason, str)
            or not self.semantic_approximation_reason.strip()
        ):
            raise ValueError(
                "approximated or ignored note pitch semantics require a "
                "semantic approximation reason"
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("note pitch source must be a non-empty string")

        if self.allowed_values is not None:
            if not isinstance(self.allowed_values, tuple) or not self.allowed_values:
                raise ValueError(
                    "note pitch allowed_values must be null or a non-empty tuple"
                )
            normalised_allowed: list[float] = []
            for value in self.allowed_values:
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))
                ):
                    raise ValueError(
                        "note pitch allowed_values must contain finite numbers"
                    )
                normalised_allowed.append(float(value))
            allowed = tuple(normalised_allowed)
            if allowed != tuple(sorted(set(allowed))):
                raise ValueError(
                    "note pitch allowed_values must be unique and sorted"
                )
            object.__setattr__(self, "allowed_values", allowed)
        if self.fixed_midi_note is not None:
            if (
                isinstance(self.fixed_midi_note, bool)
                or not isinstance(self.fixed_midi_note, (int, float))
                or not math.isfinite(float(self.fixed_midi_note))
            ):
                raise ValueError("note pitch fixed_midi_note must be finite")
            object.__setattr__(
                self,
                "fixed_midi_note",
                float(self.fixed_midi_note),
            )
        if self.selector_rounding is not None and (
            self.selector_rounding not in _SELECTOR_ROUNDINGS
        ):
            choices = ", ".join(sorted(_SELECTOR_ROUNDINGS))
            raise ValueError(
                "note pitch selector_rounding must be one of "
                f"{choices} or null"
            )

        has_bend_grid = any(
            value is not None
            for value in (
                self.quantization_steps_per_direction,
                self.pitch_bend_range_semitones,
            )
        )
        if has_bend_grid:
            if (
                isinstance(self.quantization_steps_per_direction, bool)
                or not isinstance(self.quantization_steps_per_direction, int)
                or self.quantization_steps_per_direction < 2
            ):
                raise ValueError(
                    "note pitch bend quantization requires at least two steps "
                    "per direction"
                )
            if (
                isinstance(self.pitch_bend_range_semitones, bool)
                or not isinstance(self.pitch_bend_range_semitones, (int, float))
                or not math.isfinite(float(self.pitch_bend_range_semitones))
                or float(self.pitch_bend_range_semitones) <= 0.0
            ):
                raise ValueError(
                    "note pitch bend range must be positive and finite"
                )
            object.__setattr__(
                self,
                "pitch_bend_range_semitones",
                float(self.pitch_bend_range_semitones),
            )

        if self.mode == "continuous":
            if self.fidelity != "native":
                raise ValueError("continuous note pitch requires native fidelity")
            if has_bend_grid or self.allowed_values is not None:
                raise ValueError(
                    "continuous note pitch must not declare a quantization grid"
                )
            if self.fixed_midi_note is not None or self.selector_rounding is not None:
                raise ValueError(
                    "continuous note pitch must not declare fixed/selector fields"
                )
        elif self.mode == "quantized":
            if self.fidelity != "adapted" or not has_bend_grid:
                raise ValueError(
                    "quantized note pitch requires adapted fidelity and a bend grid"
                )
            if self.allowed_values is not None or self.fixed_midi_note is not None:
                raise ValueError(
                    "quantized note pitch must not declare selector/fixed values"
                )
            if self.selector_rounding is not None:
                raise ValueError(
                    "quantized note pitch must not declare selector_rounding"
                )
        elif self.mode == "fixed":
            if self.fidelity != "ignored" or self.semantic_fidelity != "ignored":
                raise ValueError(
                    "fixed note pitch requires ignored numeric and semantic fidelity"
                )
            if self.fixed_midi_note is None:
                raise ValueError("fixed note pitch requires fixed_midi_note")
            if has_bend_grid or self.allowed_values is not None:
                raise ValueError("fixed note pitch must not declare a grid")
            if self.selector_rounding is not None:
                raise ValueError(
                    "fixed note pitch must not declare selector_rounding"
                )
        else:
            if self.semantic_fidelity != "ignored":
                raise ValueError(
                    "selector note pitch requires ignored semantic fidelity"
                )
            if self.fixed_midi_note is not None:
                raise ValueError(
                    "selector note pitch must not declare fixed_midi_note"
                )
            if self.allowed_values is not None:
                if self.fidelity != "adapted" or self.selector_rounding is None:
                    raise ValueError(
                        "selector allowed_values require adapted fidelity and "
                        "selector_rounding"
                    )
                if has_bend_grid:
                    raise ValueError(
                        "selector allowed_values and bend quantization are "
                        "mutually exclusive"
                    )
            elif self.selector_rounding is not None:
                raise ValueError(
                    "selector_rounding requires selector allowed_values"
                )
            elif self.fidelity == "adapted" and not has_bend_grid:
                raise ValueError(
                    "adapted selector note pitch requires an audited grid"
                )

    def require_semantic_policy(self, policy: str) -> None:
        if policy not in SEMANTIC_POLICIES:
            choices = ", ".join(sorted(SEMANTIC_POLICIES))
            raise ValueError(
                f"note pitch semantic policy must be one of {choices}; "
                f"got {policy!r}"
            )
        if self.semantic_fidelity != "native" and policy != "approximate":
            raise ValueError(
                "note pitch requires semantic_policy='approximate': "
                f"{self.semantic_approximation_reason}"
            )

    @staticmethod
    def _checked_value(value: Any) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("note pitch must be a finite number")
        return float(value)

    def _bend_once(self, number: float) -> float:
        assert self.quantization_steps_per_direction is not None
        assert self.pitch_bend_range_semitones is not None
        key = max(0, min(127, int(math.floor(number + 0.5))))
        offset = number - key
        if abs(offset) > self.pitch_bend_range_semitones + 1e-12:
            raise ValueError(
                f"note pitch {number:g} needs a {offset:+g}-semitone bend, "
                "outside the audited pitch-bend range"
            )
        steps = self.quantization_steps_per_direction
        bend = round(offset / self.pitch_bend_range_semitones * steps)
        bend = max(-steps, min(steps - 1, bend))
        return key + bend / steps * self.pitch_bend_range_semitones

    def _bend_adapted_value(self, number: float) -> float:
        """Return the nearest stable key-plus-bend input preimage.

        Quantizing a value just below a half-semitone boundary can round the
        decoded bend just above that boundary.  Feeding that decoded value
        back then selects the neighbouring key.  Searching the tiny local
        key/bend neighbourhood avoids tolerance-based false exactness and
        guarantees that every returned adaptation passes ``require_value``.
        """

        assert self.quantization_steps_per_direction is not None
        assert self.pitch_bend_range_semitones is not None
        steps = self.quantization_steps_per_direction
        bend_range = self.pitch_bend_range_semitones
        base_key = max(0, min(127, int(math.floor(number + 0.5))))
        candidates: set[float] = set()
        for key in range(max(0, base_key - 2), min(127, base_key + 2) + 1):
            position = (number - key) / bend_range * steps
            centre = max(-steps, min(steps - 1, round(position)))
            for bend in range(
                max(-steps, centre - 2),
                min(steps - 1, centre + 2) + 1,
            ):
                candidate = key + bend / steps * bend_range
                if self._bend_once(candidate) == candidate:
                    candidates.add(candidate)
        if not candidates:
            raise ValueError(
                f"note pitch {number:g} has no stable value on the audited "
                "key-plus-bend grid"
            )
        return min(
            candidates,
            key=lambda candidate: (abs(candidate - number), candidate),
        )

    def _selector_adapted_value(self, number: float) -> float:
        assert self.allowed_values is not None
        assert self.selector_rounding is not None
        if self.selector_rounding == "nearest_lower_tie":
            return min(
                self.allowed_values,
                key=lambda value: (abs(value - number), value),
            )
        selected = float(round(number))
        if selected not in self.allowed_values:
            choices = ", ".join(f"{value:g}" for value in self.allowed_values)
            raise ValueError(
                f"note pitch {number:g} selects unavailable key {selected:g}; "
                f"choose from {choices}"
            )
        return selected

    def adapt_value(self, value: Any) -> float:
        """Resolve one pitch onto the backend's actual input/output grid."""

        number = self._checked_value(value)
        if self.mode == "fixed":
            assert self.fixed_midi_note is not None
            return self.fixed_midi_note
        if self.allowed_values is not None:
            return self._selector_adapted_value(number)
        if self.quantization_steps_per_direction is not None:
            return self._bend_adapted_value(number)
        return number

    def require_value(self, value: Any) -> float:
        """Require exact representation without a tolerance-based shortcut."""

        number = self._checked_value(value)
        if self.adapt_value(number) != number:
            raise ValueError(
                f"note pitch {number:g} is not exactly representable by "
                f"{self.mode} fidelity; adapt explicitly"
            )
        return number

    def resolve(
        self,
        value: Any,
        *,
        adapt: bool,
        semantic_policy: str | None = None,
    ) -> NotePitchResolution:
        requested = self._checked_value(value)
        if semantic_policy is not None:
            self.require_semantic_policy(semantic_policy)
        resolved = self.adapt_value(requested) if adapt else self.require_value(
            requested
        )
        return NotePitchResolution(
            requested_value=requested,
            resolved_value=resolved,
            adapted=requested != resolved,
            application=self.application,
            protocol_input=self.protocol_input,
            value_unit=self.value_unit,
            mode=self.mode,
            fidelity=self.fidelity,
            semantic_fidelity=self.semantic_fidelity,
            numeric_approximation_reason=self.numeric_approximation_reason,
            semantic_approximation_reason=self.semantic_approximation_reason,
            quantization_steps_per_direction=(
                self.quantization_steps_per_direction
            ),
            pitch_bend_range_semitones=self.pitch_bend_range_semitones,
            allowed_values=self.allowed_values,
            fixed_midi_note=self.fixed_midi_note,
            selector_rounding=self.selector_rounding,
            source=self.source,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": "per_note",
            "application": self.application,
            "protocol_input": self.protocol_input,
            "value_unit": self.value_unit,
            "mode": self.mode,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "numeric_approximation_reason": self.numeric_approximation_reason,
            "semantic_approximation_reason": self.semantic_approximation_reason,
            "quantization_steps_per_direction": (
                self.quantization_steps_per_direction
            ),
            "pitch_bend_range_semitones": self.pitch_bend_range_semitones,
            "allowed_values": (
                None if self.allowed_values is None else list(self.allowed_values)
            ),
            "fixed_midi_note": self.fixed_midi_note,
            "selector_rounding": self.selector_rounding,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class ArticulationExecutionResolution:
    """Requested and final articulation with backend and mapping evidence."""

    requested_value: str
    resolved_value: str
    adapted: bool
    application: str
    fidelity: str
    semantic_fidelity: str
    approximation_reason: str | None
    source: str
    mapping_source: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_value": self.requested_value,
            "resolved_value": self.resolved_value,
            "adapted": self.adapted,
            "application": self.application,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "approximation_reason": self.approximation_reason,
            "source": self.source,
            "mapping_source": self.mapping_source,
        }


@dataclass(frozen=True, slots=True)
class ArticulationExecutionCapability:
    """Articulations proven to affect the following runtime note-on.

    This contract is intentionally independent from
    :attr:`InstrumentCapability.articulations`, which remains a vocabulary and
    discovery surface.  Only an audited event -> latched state -> note-on route
    may create this stronger capability.
    """

    articulations: tuple[str, ...]
    application: str
    fidelity: str
    semantic_fidelity: str
    approximation_reason: str | None
    source: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.articulations, tuple)
            or not self.articulations
            or any(
                not isinstance(value, str) or not value.strip()
                for value in self.articulations
            )
            or self.articulations != tuple(sorted(set(self.articulations)))
        ):
            raise ValueError(
                "articulation execution vocabulary must be a non-empty, "
                "unique, sorted tuple of strings"
            )
        if self.application != ARTICULATION_EXECUTION_APPLICATION:
            raise ValueError(
                "articulation execution application must be 'note_on_latched'"
            )
        if self.fidelity not in ARTICULATION_EXECUTION_FIDELITIES:
            choices = ", ".join(sorted(ARTICULATION_EXECUTION_FIDELITIES))
            raise ValueError(
                "articulation execution fidelity must be one of "
                f"{choices}; got {self.fidelity!r}"
            )
        if (
            self.semantic_fidelity
            not in ARTICULATION_EXECUTION_SEMANTIC_FIDELITIES
        ):
            choices = ", ".join(
                sorted(ARTICULATION_EXECUTION_SEMANTIC_FIDELITIES)
            )
            raise ValueError(
                "articulation execution semantic_fidelity must be one of "
                f"{choices}; got {self.semantic_fidelity!r}"
            )
        if self.fidelity == "ignored" and self.semantic_fidelity != "ignored":
            raise ValueError(
                "ignored articulation execution requires ignored semantics"
            )
        if self.semantic_fidelity == "native":
            if self.approximation_reason is not None:
                raise ValueError(
                    "native articulation semantics must not declare an "
                    "approximation reason"
                )
        elif (
            not isinstance(self.approximation_reason, str)
            or not self.approximation_reason.strip()
        ):
            raise ValueError(
                "non-native articulation semantics require an approximation "
                "reason"
            )
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError(
                "articulation execution source must be a non-empty string"
            )

    @staticmethod
    def _checked_name(value: Any, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value

    def _require_target(self, value: Any) -> str:
        name = self._checked_name(value, field="articulation")
        if name not in self.articulations:
            choices = ", ".join(self.articulations)
            raise ValueError(
                f"articulation {name!r} has no audited runtime execution; "
                f"choose from {choices}"
            )
        if self.fidelity == "ignored":
            raise ValueError(
                f"articulation {name!r} is accepted but ignored by the backend"
            )
        return name

    @staticmethod
    def _require_semantic_policy(
        semantic_fidelity: str,
        approximation_reason: str | None,
        policy: str,
    ) -> None:
        if policy not in SEMANTIC_POLICIES:
            choices = ", ".join(sorted(SEMANTIC_POLICIES))
            raise ValueError(
                "articulation semantic policy must be one of "
                f"{choices}; got {policy!r}"
            )
        if semantic_fidelity != "native" and policy != "approximate":
            raise ValueError(
                "articulation requires semantic_policy='approximate': "
                f"{approximation_reason}"
            )

    def require_value(self, value: Any) -> str:
        return self._require_target(value)

    def adapt_value(
        self,
        requested_value: Any,
        resolved_value: Any,
        *,
        mapping_source: str,
    ) -> str:
        requested = self._checked_name(
            requested_value,
            field="requested articulation",
        )
        resolved = self._require_target(resolved_value)
        if requested != resolved and (
            not isinstance(mapping_source, str) or not mapping_source.strip()
        ):
            raise ValueError(
                "adapted articulation requires a non-empty mapping_source"
            )
        return resolved

    def resolve(
        self,
        requested_value: Any,
        *,
        adapt: bool,
        resolved_value: Any | None = None,
        mapping_source: str | None = None,
        semantic_policy: str | None = None,
    ) -> ArticulationExecutionResolution:
        requested = self._checked_name(
            requested_value,
            field="requested articulation",
        )
        if adapt:
            if resolved_value is None:
                raise ValueError(
                    "adapted articulation requires an explicit resolved_value"
                )
            resolved = self.adapt_value(
                requested,
                resolved_value,
                mapping_source="" if mapping_source is None else mapping_source,
            )
        else:
            if resolved_value is not None and resolved_value != requested:
                raise ValueError(
                    "exact articulation resolution cannot change the value"
                )
            if mapping_source is not None:
                raise ValueError(
                    "exact articulation resolution must not declare mapping_source"
                )
            resolved = self.require_value(requested)
        changed = requested != resolved
        resolution_semantics = (
            "approximated" if changed else self.semantic_fidelity
        )
        resolution_reason = (
            "an explicit external mapping replaces the requested articulation "
            "with a different audited backend articulation"
            if changed
            else self.approximation_reason
        )
        if semantic_policy is not None:
            self._require_semantic_policy(
                resolution_semantics,
                resolution_reason,
                semantic_policy,
            )
        return ArticulationExecutionResolution(
            requested_value=requested,
            resolved_value=resolved,
            adapted=changed,
            application=self.application,
            fidelity=self.fidelity,
            semantic_fidelity=resolution_semantics,
            approximation_reason=resolution_reason,
            source=self.source,
            mapping_source=mapping_source if changed else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": "per_note",
            "articulations": list(self.articulations),
            "application": self.application,
            "fidelity": self.fidelity,
            "semantic_fidelity": self.semantic_fidelity,
            "approximation_reason": self.approximation_reason,
            "source": self.source,
        }


_CONTROL_VALUE_UNSET = object()


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
    controls: tuple[ControlCapability, ...] = ()
    supports_release_velocity: bool = False
    release_velocity_source: str | None = None
    note_velocity: NoteVelocityCapability | None = None
    note_pitch: NotePitchCapability | None = None
    articulation_execution: ArticulationExecutionCapability | None = None

    def __post_init__(self) -> None:
        for label, value in (
            ("name", self.name),
            ("relative_path", self.relative_path),
            ("manifest_path", self.manifest_path),
            ("implementation_type", self.implementation_type),
            ("articulation_source", self.articulation_source),
        ):
            if type(value) is not str or not value.strip():
                raise ValueError(f"instrument {label} must be a non-empty string")
        if type(self.pitched) is not bool:
            raise ValueError("instrument pitched must be boolean")
        for label, value in (
            ("note_min", self.note_min),
            ("note_max", self.note_max),
            ("fixed_midi_note", self.fixed_midi_note),
            ("onset_seconds", self.onset_seconds),
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise ValueError(f"instrument {label} must be a finite number or null")
        if self.note_min is not None and self.note_max is not None:
            if not 0.0 <= float(self.note_min) <= float(self.note_max) <= 127.0:
                raise ValueError(
                    "instrument note range must satisfy 0 <= note_min <= "
                    "note_max <= 127"
                )
        if self.fixed_midi_note is not None and not 0.0 <= float(
            self.fixed_midi_note
        ) <= 127.0:
            raise ValueError("instrument fixed_midi_note must be between 0 and 127")
        if self.onset_seconds is not None and float(self.onset_seconds) < 0.0:
            raise ValueError("instrument onset_seconds must not be negative")
        if self.pitch_mode is not None and self.pitch_mode not in _PITCH_MODES:
            choices = ", ".join(sorted(_PITCH_MODES))
            raise ValueError(
                f"instrument pitch_mode must be one of {choices} or null"
            )
        if self.pitch_mode == "fixed" and self.fixed_midi_note is None:
            raise ValueError("fixed pitch_mode requires fixed_midi_note")
        if self.pitch_mode != "fixed" and self.fixed_midi_note is not None:
            raise ValueError("fixed_midi_note requires fixed pitch_mode")
        if type(self.articulations) is not tuple or any(
            type(item) is not str or not item.strip()
            for item in self.articulations
        ):
            raise ValueError(
                "instrument articulations must be a tuple of non-empty strings"
            )
        if len(self.articulations) != len(set(self.articulations)):
            raise ValueError("instrument articulations must be unique")
        if self.default_articulation is not None:
            if (
                type(self.default_articulation) is not str
                or not self.default_articulation.strip()
            ):
                raise ValueError(
                    "instrument default_articulation must be a non-empty string or null"
                )
            if self.default_articulation not in self.articulations:
                raise ValueError(
                    "instrument default_articulation must be present in the "
                    "audited articulation vocabulary"
                )
        if self.quality_tier is not None and (
            type(self.quality_tier) is not str or not self.quality_tier.strip()
        ):
            raise ValueError("instrument quality_tier must be a non-empty string or null")
        _validate_range_tuple(
            self.playable_ranges,
            field="playable_ranges",
            allow_empty=True,
        )
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
        if not isinstance(self.controls, tuple):
            raise ValueError("instrument controls must be a tuple")
        if not isinstance(self.supports_release_velocity, bool):
            raise ValueError("supports_release_velocity must be boolean")
        if self.supports_release_velocity:
            if (
                not isinstance(self.release_velocity_source, str)
                or not self.release_velocity_source.strip()
            ):
                raise ValueError(
                    "release_velocity support requires an audited source"
                )
        elif self.release_velocity_source is not None:
            raise ValueError(
                "release_velocity_source requires supports_release_velocity"
            )
        if self.note_velocity is not None and not isinstance(
            self.note_velocity,
            NoteVelocityCapability,
        ):
            raise ValueError(
                "instrument note_velocity must be a NoteVelocityCapability or null"
            )
        if self.note_pitch is not None and not isinstance(
            self.note_pitch,
            NotePitchCapability,
        ):
            raise ValueError(
                "instrument note_pitch must be a NotePitchCapability or null"
            )
        if self.articulation_execution is not None:
            if not isinstance(
                self.articulation_execution,
                ArticulationExecutionCapability,
            ):
                raise ValueError(
                    "instrument articulation_execution must be an "
                    "ArticulationExecutionCapability or null"
                )
            unknown_execution = set(
                self.articulation_execution.articulations
            ) - set(self.articulations)
            if unknown_execution:
                raise ValueError(
                    "articulation execution names must belong to the audited "
                    f"instrument vocabulary; unknown {sorted(unknown_execution)!r}"
                )
        control_keys: set[tuple[str, str]] = set()
        for control in self.controls:
            if not isinstance(control, ControlCapability):
                raise ValueError(
                    "instrument controls must contain ControlCapability records"
                )
            key = (control.scope, control.name)
            if key in control_keys:
                raise ValueError(
                    "duplicate instrument control capability "
                    f"{control.name!r} in scope {control.scope!r}"
                )
            control_keys.add(key)
            if control.applicable_articulations is not None:
                declared_articulations = set(self.articulations)
                if self.default_articulation is not None:
                    declared_articulations.add(self.default_articulation)
                unknown = set(control.applicable_articulations) - declared_articulations
                if unknown:
                    raise ValueError(
                        f"control {control.name!r} applicable_articulations "
                        "must be declared by the instrument; unknown "
                        f"{sorted(unknown)!r}"
                    )
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
        return articulation in self.articulations

    def control_for(
        self,
        name: str,
        *,
        scope: str = _RUNTIME_CONTROL_SCOPE,
    ) -> ControlCapability | None:
        """Return an exact declared control; never infer MIDI conventions."""

        for control in self.controls:
            if control.name == name and control.scope == scope:
                return control
        return None

    def require_control(
        self,
        name: str,
        *,
        scope: str = _RUNTIME_CONTROL_SCOPE,
        kind: str | None = None,
        interpolation: str | None = None,
        articulation: str | None = None,
        semantic_policy: str | None = None,
        value: Any = _CONTROL_VALUE_UNSET,
    ) -> ControlCapability:
        """Preflight one proposed control use and fail closed if unsupported."""

        if scope not in CONTROL_SCOPES:
            supported = ", ".join(sorted(CONTROL_SCOPES))
            raise ValueError(
                f"control scope must be one of {supported}; got {scope!r}"
            )
        control = self.control_for(name, scope=scope)
        if control is None:
            choices = ", ".join(
                item.name for item in self.controls if item.scope == scope
            )
            suffix = f"; choose from {choices}" if choices else ""
            raise ValueError(
                f"{self.name} does not support control {name!r} "
                f"in scope {scope!r}{suffix}"
            )
        if kind is not None and kind != control.kind:
            raise ValueError(
                f"control {name!r} is {control.kind}, not {kind}"
            )
        if (
            interpolation is not None
            and interpolation not in control.interpolations
        ):
            choices = ", ".join(control.interpolations)
            raise ValueError(
                f"control {name!r} does not allow {interpolation!r} "
                f"interpolation; choose from {choices}"
            )
        if articulation is not None:
            if not self.supports(articulation):
                raise ValueError(
                    f"{self.name} does not declare articulation {articulation!r}"
                )
            if (
                control.applicable_articulations is not None
                and articulation not in control.applicable_articulations
            ):
                choices = ", ".join(control.applicable_articulations)
                raise ValueError(
                    f"control {name!r} is not applicable to articulation "
                    f"{articulation!r}; choose from {choices}"
                )
        if semantic_policy is not None:
            control.require_semantic_policy(semantic_policy)
        if value is not _CONTROL_VALUE_UNSET:
            control.require_value(value)
        return control

    def supports_control(
        self,
        name: str,
        *,
        scope: str = _RUNTIME_CONTROL_SCOPE,
        kind: str | None = None,
        interpolation: str | None = None,
        articulation: str | None = None,
        semantic_policy: str | None = None,
        value: Any = _CONTROL_VALUE_UNSET,
    ) -> bool:
        """Boolean counterpart to :meth:`require_control` for discovery."""

        try:
            self.require_control(
                name,
                scope=scope,
                kind=kind,
                interpolation=interpolation,
                articulation=articulation,
                semantic_policy=semantic_policy,
                value=value,
            )
        except ValueError:
            return False
        return True

    @property
    def supports_note_velocity(self) -> bool:
        """Whether note-on velocity has an audited audible implementation."""

        return (
            self.note_velocity is not None
            and self.note_velocity.fidelity != "ignored"
        )

    def require_note_velocity(
        self,
        value: Any,
        *,
        semantic_policy: str | None = None,
    ) -> NoteVelocityResolution:
        """Require exact backend representation and retain audit evidence."""

        if self.note_velocity is None:
            raise ValueError(
                f"{self.name} does not declare an audited note-on velocity "
                "implementation"
            )
        if semantic_policy is not None:
            self.note_velocity.require_semantic_policy(semantic_policy)
        return self.note_velocity.resolve(value, adapt=False)

    def adapt_note_velocity(
        self,
        value: Any,
        *,
        semantic_policy: str | None = None,
    ) -> NoteVelocityResolution:
        """Explicitly adapt velocity to the backend grid with audit evidence."""

        if self.note_velocity is None:
            raise ValueError(
                f"{self.name} does not declare an audited note-on velocity "
                "implementation"
            )
        if semantic_policy is not None:
            self.note_velocity.require_semantic_policy(semantic_policy)
        return self.note_velocity.resolve(value, adapt=True)

    @property
    def supports_note_pitch(self) -> bool:
        """Whether note-on pitch has an audited backend execution path."""

        return self.note_pitch is not None

    def require_note_pitch(
        self,
        value: Any,
        *,
        semantic_policy: str | None = None,
    ) -> NotePitchResolution:
        """Require exact backend pitch representation and retain evidence."""

        if self.note_pitch is None:
            raise ValueError(
                f"{self.name} does not declare an audited note-on pitch "
                "implementation"
            )
        return self.note_pitch.resolve(
            value,
            adapt=False,
            semantic_policy=semantic_policy,
        )

    def adapt_note_pitch(
        self,
        value: Any,
        *,
        semantic_policy: str | None = None,
    ) -> NotePitchResolution:
        """Explicitly adapt pitch to the backend contract with evidence."""

        if self.note_pitch is None:
            raise ValueError(
                f"{self.name} does not declare an audited note-on pitch "
                "implementation"
            )
        return self.note_pitch.resolve(
            value,
            adapt=True,
            semantic_policy=semantic_policy,
        )

    @property
    def supports_articulation_execution(self) -> bool:
        """Whether runtime note-on articulation selection is proven."""

        return self.articulation_execution is not None

    def require_articulation_execution(
        self,
        articulation: Any,
        *,
        semantic_policy: str | None = None,
    ) -> ArticulationExecutionResolution:
        """Require one direct, note-on-latched articulation execution."""

        if self.articulation_execution is None:
            raise ValueError(
                f"{self.name} does not declare audited note-on-latched "
                "articulation execution"
            )
        return self.articulation_execution.resolve(
            articulation,
            adapt=False,
            semantic_policy=semantic_policy,
        )

    def adapt_articulation_execution(
        self,
        requested_articulation: Any,
        resolved_articulation: Any,
        *,
        mapping_source: str,
        semantic_policy: str | None = None,
    ) -> ArticulationExecutionResolution:
        """Validate an explicit external mapping to an executable target."""

        if self.articulation_execution is None:
            raise ValueError(
                f"{self.name} does not declare audited note-on-latched "
                "articulation execution"
            )
        return self.articulation_execution.resolve(
            requested_articulation,
            adapt=True,
            resolved_value=resolved_articulation,
            mapping_source=mapping_source,
            semantic_policy=semantic_policy,
        )

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
        manifest_path, manifest = _read_capability_json_object(manifest_path)
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
            "controls": [control.to_dict() for control in self.controls],
            "supports_release_velocity": self.supports_release_velocity,
            "release_velocity_source": self.release_velocity_source,
            "note_velocity": (
                None
                if self.note_velocity is None
                else self.note_velocity.to_dict()
            ),
            "note_pitch": (
                None if self.note_pitch is None else self.note_pitch.to_dict()
            ),
            "articulation_execution": (
                None
                if self.articulation_execution is None
                else self.articulation_execution.to_dict()
            ),
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


def _continuous_note_pitch(
    instrument_type: str,
    handler_source: str,
) -> NotePitchCapability:
    semantic_reason = _NOTE_PITCH_SEMANTIC_APPROXIMATIONS.get(
        instrument_type
    )
    return NotePitchCapability(
        application=NOTE_PITCH_APPLICATION,
        protocol_input="pitch_hz",
        value_unit="midi_note_at_a4_440",
        mode="continuous",
        fidelity="native",
        semantic_fidelity=(
            "approximated" if semantic_reason is not None else "native"
        ),
        numeric_approximation_reason=None,
        semantic_approximation_reason=semantic_reason,
        source=f"backend:{handler_source}",
    )


def _fixed_note_pitch(
    *,
    fixed_midi_note: float,
    source: str,
    reason: str,
) -> NotePitchCapability:
    return NotePitchCapability(
        application=NOTE_PITCH_APPLICATION,
        protocol_input="midi_note_or_pitch_hz",
        value_unit="event_midi_note",
        mode="fixed",
        fidelity="ignored",
        semantic_fidelity="ignored",
        numeric_approximation_reason=reason,
        semantic_approximation_reason=reason,
        source=source,
        fixed_midi_note=fixed_midi_note,
    )


def _selector_note_pitch(
    *,
    source: str,
    reason: str,
    fidelity: str,
    allowed_values: tuple[float, ...] | None = None,
    selector_rounding: str | None = None,
) -> NotePitchCapability:
    return NotePitchCapability(
        application=NOTE_PITCH_APPLICATION,
        protocol_input="midi_note_or_pitch_hz",
        value_unit="event_midi_note",
        mode="selector",
        fidelity=fidelity,
        semantic_fidelity="ignored",
        numeric_approximation_reason=(
            None
            if fidelity == "native"
            else "the backend snaps the incoming key to its audited selector grid"
        ),
        semantic_approximation_reason=reason,
        source=source,
        allowed_values=allowed_values,
        selector_rounding=selector_rounding,
    )


def _read_note_pitch(
    instrument_type: str,
    manifest: dict[str, Any],
    *,
    pitched: bool,
) -> NotePitchCapability | None:
    """Return pitch facts proven by the built-in runtime implementation."""

    # A local factory replaces the entire built-in dispatch.  A matching
    # ``type`` string is not evidence that the returned class preserves any of
    # the built-in pitch path.
    if manifest.get("implementation") is not None:
        return None

    handler_source = _BACKEND_NOTE_PITCH.get(instrument_type)
    if instrument_type in {"dedicated_sfz", "dedicated_fx"}:
        raw_mode = manifest.get("pitch_mode", "pitched")
        if type(raw_mode) is not str or raw_mode not in _PITCH_MODES:
            choices = ", ".join(sorted(_PITCH_MODES))
            raise ValueError(
                f"dedicated pitch_mode must be one of {choices}"
            )
        assert handler_source is not None
        source = f"backend:{handler_source}"
        if raw_mode == "fixed":
            fixed = _manifest_finite_number(manifest, "fixed_midi_note")
            if fixed is None:
                raise ValueError("fixed pitch_mode requires fixed_midi_note")
            reason = (
                "the dedicated backend discards the requested pitch and "
                "replaces it with manifest.fixed_midi_note"
            )
            return _fixed_note_pitch(
                fixed_midi_note=fixed,
                source=source + ";manifest.fixed_midi_note",
                reason=reason,
            )
        if raw_mode == "ignore":
            return _selector_note_pitch(
                source=source + ";backend:_sample_ignore_pitch",
                reason=(
                    "the incoming key selects an SFZ region, but the selected "
                    "sample retains native playback pitch"
                ),
                fidelity="native",
            )
        return _continuous_note_pitch(instrument_type, handler_source)

    if instrument_type == "modeled_instrument":
        profile_name = manifest.get("profile")
        if type(profile_name) is not str:
            raise ValueError("modeled instrument profile must be a string")
        from tianlai.modeled_instruments import PROFILES

        profile = PROFILES.get(profile_name)
        if profile is None:
            raise ValueError(
                f"unknown modeled instrument profile {profile_name!r} "
                "while reading note pitch"
            )
        if profile.get("pitch_mode") == "keymap":
            raw_keymap = profile.get("keymap")
            if not isinstance(raw_keymap, dict) or not raw_keymap:
                raise ValueError(
                    "modeled keymap profile lacks an audited key map"
                )
            allowed = tuple(sorted(float(key) for key in raw_keymap))
            assert handler_source is not None
            return _selector_note_pitch(
                source=(
                    f"backend:{handler_source};"
                    f"tianlai.modeled_instruments.PROFILES[{profile_name}]"
                ),
                reason=(
                    "the rounded incoming key selects a modeled strike/voice; "
                    "it is not interpreted as twelve-tone sounding pitch"
                ),
                fidelity="adapted",
                allowed_values=allowed,
                selector_rounding="python_nearest_even",
            )

    if instrument_type == "reversed_cymbal":
        raw_variants = manifest.get("variants")
        if not isinstance(raw_variants, dict) or not raw_variants:
            raise ValueError(
                "reversed cymbal requires a non-empty variants selector map"
            )
        allowed_values: list[float] = []
        for raw_key in raw_variants:
            if not isinstance(raw_key, str) or not raw_key.strip():
                raise ValueError(
                    "reversed cymbal selector keys must be integer strings"
                )
            try:
                runtime_key = int(raw_key)
                value = float(runtime_key)
            except (OverflowError, ValueError) as error:
                raise ValueError(
                    "reversed cymbal selector keys must be integer strings"
                ) from error
            if not math.isfinite(value):
                raise ValueError(
                    "reversed cymbal selector keys must be finite integers"
                )
            allowed_values.append(value)
        return _selector_note_pitch(
            source=(
                "backend:tianlai.reversed_cymbal."
                "ReversedCymbalInstrument._variant_for;manifest.variants"
            ),
            reason=(
                "the incoming key chooses the nearest reversed-cymbal sample "
                "variant and does not control its acoustic pitch"
            ),
            fidelity="adapted",
            allowed_values=tuple(sorted(allowed_values)),
            selector_rounding="nearest_lower_tie",
        )

    if instrument_type == "vpo_cowbell":
        reason = (
            "the cowbell backend discards the requested pitch and always "
            "routes note-on to its audited fixed source key 56"
        )
        return _fixed_note_pitch(
            fixed_midi_note=56.0,
            source=(
                "backend:tianlai.vpo_specials."
                "VpoCowbellInstrument.handle_event;constant:midi_note=56"
            ),
            reason=reason,
        )

    if instrument_type == "soundfont":
        assert handler_source is not None
        fixed = _manifest_finite_number(manifest, "fixed_midi_note")
        if fixed is not None:
            reason = (
                "the SoundFont backend discards the requested pitch and uses "
                "manifest.fixed_midi_note"
            )
            return _fixed_note_pitch(
                fixed_midi_note=fixed,
                source=(
                    f"backend:{handler_source};manifest.fixed_midi_note"
                ),
                reason=reason,
            )
        raw_range = manifest.get("pitch_bend_range_semitones", 2.0)
        if (
            isinstance(raw_range, bool)
            or not isinstance(raw_range, (int, float))
            or not math.isfinite(float(raw_range))
            or not 0.0 < float(raw_range) <= 127.99
        ):
            raise ValueError(
                "pitch_bend_range_semitones must be positive, finite and at "
                "most 127.99"
            )
        pitch_bend_range = float(raw_range)
        decimal_pitch_bend_range = Decimal(str(pitch_bend_range))
        if (
            decimal_pitch_bend_range * 100
            != (decimal_pitch_bend_range * 100).to_integral_value()
        ):
            # FluidSynth receives the RPN sensitivity in whole semitones and
            # cents, while _key_and_bend divides by the unrounded manifest
            # float.  Until both numbers are represented in the public
            # contract, that configuration has no truthful v2 pitch adapter.
            return None
        percussion = manifest.get("percussion", False)
        if not isinstance(percussion, bool):
            raise ValueError("soundfont percussion must be boolean")
        numeric_reason = (
            "SoundFont pitch is snapped to a nearest MIDI key plus one signed "
            "14-bit pitch-bend value"
        )
        semantic_reason = (
            "the incoming SoundFont key selects a percussion timbre; key "
            "number is not a chromatic sounding-pitch promise"
            if percussion
            else None
        )
        return NotePitchCapability(
            application=NOTE_PITCH_APPLICATION,
            protocol_input="pitch_hz",
            value_unit="midi_note_at_a4_440",
            mode="selector" if percussion else "quantized",
            fidelity="adapted",
            semantic_fidelity="ignored" if percussion else "native",
            numeric_approximation_reason=numeric_reason,
            semantic_approximation_reason=semantic_reason,
            source=(
                f"backend:{handler_source};"
                "mapping:nearest_key_plus_signed_14_bit_bend;"
                "manifest.pitch_bend_range_semitones"
            ),
            quantization_steps_per_direction=8192,
            pitch_bend_range_semitones=pitch_bend_range,
        )

    if not pitched or handler_source is None:
        return None
    return _continuous_note_pitch(instrument_type, handler_source)


def _read_articulation_execution(
    instrument_type: str,
    manifest: dict[str, Any],
    articulations: tuple[str, ...],
) -> ArticulationExecutionCapability | None:
    """Return only vocabulary members proven to affect the next note-on."""

    if manifest.get("implementation") is not None or not articulations:
        return None
    handler_source = _BACKEND_ARTICULATION_EXECUTION.get(instrument_type)
    if handler_source is None:
        return None
    if instrument_type == "soundfont":
        programs = manifest.get("articulation_programs")
        if programs is None:
            return None
        if not isinstance(programs, dict) or not programs:
            raise ValueError(
                "soundfont articulation_programs must be a non-empty object"
            )
        program_names = tuple(sorted(programs))
        if any(
            not isinstance(name, str) or not name.strip()
            for name in program_names
        ):
            raise ValueError(
                "soundfont articulation_programs names must be non-empty strings"
            )
        for name, patch in programs.items():
            if isinstance(patch, bool):
                raise ValueError(
                    f"soundfont articulation program {name!r} must be an integer "
                    "or an object with an integer program"
                )
            if isinstance(patch, int):
                continue
            if not isinstance(patch, dict):
                raise ValueError(
                    f"soundfont articulation program {name!r} must be an integer "
                    "or an object with an integer program"
                )
            program = patch.get("program")
            bank = patch.get("bank", manifest.get("bank", 0))
            if (
                isinstance(program, bool)
                or not isinstance(program, int)
                or isinstance(bank, bool)
                or not isinstance(bank, int)
            ):
                raise ValueError(
                    f"soundfont articulation program {name!r} must use integer "
                    "bank/program values"
                )
        if program_names != articulations:
            raise ValueError(
                "soundfont articulation vocabulary must exactly match "
                "articulation_programs for runtime execution"
            )
    return ArticulationExecutionCapability(
        articulations=articulations,
        application=ARTICULATION_EXECUTION_APPLICATION,
        fidelity="native",
        semantic_fidelity="native",
        approximation_reason=None,
        source=f"backend:{handler_source}",
    )


def _read_note_velocity(
    instrument_type: str,
    manifest: dict[str, Any],
) -> NoteVelocityCapability | None:
    """Return only a note-on velocity contract proven by a backend audit."""

    # A local create() factory replaces the built-in dispatch completely.  Its
    # chosen class may share the manifest's ``type`` spelling without sharing
    # any of that built-in backend's velocity semantics.
    if manifest.get("implementation") is not None:
        return None
    handler_source = _BACKEND_NOTE_VELOCITY.get(instrument_type)
    if handler_source is None:
        return None

    fidelity = "native"
    steps: int | None = None
    quantization_exponent: float | None = None
    output_range: tuple[int, int] | None = None
    zero_behavior = "silent"
    source = f"backend:{handler_source}"
    if instrument_type in {"dedicated_sfz", "dedicated_fx"}:
        # _playback_payload performs this exact snap before region selection
        # and before SampleInstrument applies its continuous amplitude curve.
        fidelity = "adapted"
        steps = 128
        quantization_exponent = 1.0
        output_range = (0, 127)
        source += ";mapping:round(value*127)/127"
    elif instrument_type == "soundfont":
        # FluidSynth note-on is a MIDI byte.  Zero is deliberately promoted to
        # one (MIDI zero means note-off), hence there are 127 audible levels,
        # not the 128 levels exposed by a generic 7-bit CC.
        raw_exponent = manifest.get("velocity_exponent", 0.72)
        if isinstance(raw_exponent, bool):
            raise ValueError("velocity_exponent must be a positive finite number")
        exponent = float(raw_exponent)
        if not math.isfinite(exponent) or exponent <= 0.0:
            raise ValueError("velocity_exponent must be a positive finite number")
        fidelity = "adapted"
        steps = 127
        quantization_exponent = exponent
        output_range = (1, 127)
        zero_behavior = "minimum_nonzero"
        source += ";manifest.velocity_exponent;mapping:max(1,round(value**exponent*127))"
    elif instrument_type == "modeled_instrument":
        from tianlai.modeled_instruments import PROFILES

        profile_name = str(manifest.get("profile", ""))
        profile = PROFILES.get(profile_name)
        if profile is None:
            raise ValueError(
                f"unknown modeled instrument profile {profile_name!r} "
                "while reading note velocity"
            )
        if str(profile.get("voice", "")) != "plucked_string":
            zero_behavior = "audible_baseline"
        source += f";manifest.profile:{profile_name}"

    approximation_reason = _NOTE_VELOCITY_APPROXIMATIONS.get(instrument_type)
    return NoteVelocityCapability(
        fidelity=fidelity,
        semantic_fidelity=(
            "approximated" if approximation_reason is not None else "native"
        ),
        approximation_reason=approximation_reason,
        steps=steps,
        quantization_exponent=quantization_exponent,
        quantization_output_range=output_range,
        zero_behavior=zero_behavior,
        source=source,
    )


def _sustain_semantic_contract(
    instrument_type: str,
    manifest: dict[str, Any],
) -> tuple[str, str | None, str]:
    """Classify CC64-style release holding separately from physical pedals."""

    if instrument_type in _NATIVE_SUSTAIN_TYPES:
        return "native", None, ";semantic:native_damper_or_protocol"
    if (
        instrument_type in {"dedicated_sfz", "dedicated_fx"}
        and str(manifest.get("upgrade_status", ""))
        in _NATIVE_DEDICATED_SUSTAIN_PROFILES
    ):
        return "native", None, ";semantic:audited_native_damper"
    if (
        instrument_type == "vpo_percussion"
        and str(manifest.get("profile", ""))
        in _NATIVE_VPO_PERCUSSION_SUSTAIN_PROFILES
    ):
        return "native", None, ";semantic:audited_native_damper"
    if instrument_type == "vpo_harp":
        return (
            "approximated",
            "the backend implements a generic release gate as a score-level "
            "release-hold abstraction; "
            "it is not the concert harp's seven pitch pedals",
            ";semantic:generic_release_gate",
        )
    return (
        "approximated",
        "the backend only delays note-off through a generic release gate; "
        "this instrument/profile has no audited native damper or sustain-pedal "
        "mechanism",
        ";semantic:generic_release_gate",
    )


def _read_controls(
    instrument_type: str,
    manifest: dict[str, Any],
) -> tuple[ControlCapability, ...]:
    """Load only controls backed by the explicit runtime audit table."""

    declared = manifest.get("supported_controls")
    if manifest.get("implementation") is not None:
        if declared is not None:
            raise ValueError(
                "supported_controls cannot reuse a built-in audit when a local "
                "implementation replaces that backend"
            )
        return ()
    target = _BACKEND_CONTROLS.get(instrument_type)
    declared_names: tuple[str, ...] | None = None
    if declared is not None:
        if (
            not isinstance(declared, list)
            or any(not isinstance(item, str) or not item for item in declared)
            or len(declared) != len(set(declared))
        ):
            raise ValueError(
                "supported_controls must be an array of unique non-empty strings"
            )
        if target is None:
            raise ValueError(
                "supported_controls has no audited backend mapping for "
                f"instrument type {instrument_type!r}"
            )
        declared_names = tuple(sorted(declared))

    if target is None:
        return ()
    names, handler_source = target
    if tuple(sorted(set(names))) != names:
        raise ValueError(
            f"backend control audit for {instrument_type!r} must be unique and sorted"
        )

    # Some shared implementations accept a pedal event even when a particular
    # manifest profile contains only self-terminating voices.  That is not an
    # observable instrument capability, so remove it at profile resolution.
    observable_names = list(names)
    if instrument_type == "modeled_instrument" and "modulation" in names:
        from tianlai.modeled_instruments import PROFILES

        profile_name = str(manifest.get("profile", ""))
        profile = PROFILES.get(profile_name)
        if profile is None:
            raise ValueError(
                f"unknown modeled instrument profile {profile_name!r} "
                "while reading controls"
            )
        if str(profile.get("voice", "")) not in {"blown_pipe", "double_reed"}:
            observable_names.remove("modulation")
    if instrument_type == "vpo_percussion" and "sustain_pedal" in names:
        from tianlai.vpo_percussion import PERCUSSION_PROFILES

        profile_name = str(manifest.get("profile", ""))
        profile = PERCUSSION_PROFILES.get(profile_name)
        if profile is None:
            raise ValueError(
                f"unknown percussion profile {profile_name!r} while reading controls"
            )
        if all(spec.one_shot for spec in profile.articulations.values()):
            observable_names.remove("sustain_pedal")
    if instrument_type == "procedural_sfx" and "sustain_pedal" in names:
        from tianlai.procedural_sfx import SFX_PROFILES

        profile_name = str(manifest.get("profile", ""))
        profile = SFX_PROFILES.get(profile_name)
        if profile is None:
            raise ValueError(
                f"unknown procedural SFX profile {profile_name!r} while reading controls"
            )
        parameters = manifest.get("parameters", {})
        if not isinstance(parameters, dict):
            raise ValueError("procedural SFX parameters must be an object")
        one_shot_seconds = parameters.get(
            "one_shot_seconds",
            profile.one_shot_seconds,
        )
        if one_shot_seconds is not None:
            if isinstance(one_shot_seconds, bool):
                raise ValueError("procedural SFX one_shot_seconds must be positive")
            one_shot_seconds = float(one_shot_seconds)
            if not math.isfinite(one_shot_seconds) or one_shot_seconds <= 0.0:
                raise ValueError("procedural SFX one_shot_seconds must be positive")
            observable_names.remove("sustain_pedal")
    if declared_names is not None and declared_names != tuple(observable_names):
        raise ValueError(
            "supported_controls does not exactly match the observable audited "
            f"backend contract for {instrument_type!r}; expected "
            f"{observable_names!r}"
        )

    records: list[ControlCapability] = []
    for name in observable_names:
        kind = "discrete" if name in _DISCRETE_CONTROLS else "continuous"
        fidelity = "native"
        semantic_fidelity = "native"
        approximation_reason: str | None = None
        steps: int | None = None
        quantization_exponent: float | None = None
        allowed_values: tuple[float, ...] | None = None
        applicable_articulations: tuple[str, ...] | None = None
        source = f"backend:{handler_source}"
        if kind == "discrete":
            application = "release_gate"
        elif (
            (instrument_type == "piano" and name == "una_corda")
            or (
                instrument_type in {"vpo_brass", "vpo_mixed_choir"}
                and name == "modulation"
            )
        ):
            application = "note_on_latched"
        else:
            application = "active_voice_continuous"
        if instrument_type == "piano" and name == "una_corda":
            semantic_fidelity = "approximated"
            approximation_reason = (
                "the current piano backend only approximates una corda by "
                "reducing note-on velocity and brightness; it has no dedicated "
                "soft-pedal samples or una-corda mechanical model"
            )
        elif name == "sustain_pedal":
            (
                semantic_fidelity,
                approximation_reason,
                semantic_source,
            ) = _sustain_semantic_contract(instrument_type, manifest)
            source += semantic_source
        elif name == "breath" and instrument_type in _GAIN_ONLY_BREATH_TYPES:
            semantic_fidelity = "approximated"
            approximation_reason = (
                "the backend reduces breath to a smoothed playback-gain "
                "multiplier; it does not change airflow noise, sample timbre, "
                "or articulation"
            )
            source += ";semantic:smoothed_gain_proxy"
        elif name == "breath" and instrument_type == "mtg_solo_sax":
            # MTG keeps recorded breath transients and gives breath a distinct
            # influence over their noise/pitched balance, rather than merely
            # aliasing expression gain.
            source += ";semantic:pitched_gain_and_recorded_breath_noise_mix"

        if instrument_type == "vpo_mixed_choir" and name == "modulation":
            applicable_articulations = ("normal",)
        elif instrument_type == "vpo_brass" and name == "modulation":
            applicable_articulations = (
                "accent",
                "normal",
                "staccato",
                "sustain",
            )
        elif instrument_type == "vpo_percussion" and name == "sustain_pedal":
            from tianlai.vpo_percussion import PERCUSSION_PROFILES

            profile_name = str(manifest.get("profile", ""))
            profile = PERCUSSION_PROFILES[profile_name]
            applicable_articulations = tuple(
                sorted(
                    articulation
                    for articulation, spec in profile.articulations.items()
                    if not spec.one_shot
                )
            )

        if name in {"expression", "breath", "volume"}:
            default_value = 1.0
        else:
            default_value = 0.0
        if (
            instrument_type in {"dedicated_sfz", "dedicated_fx"}
            and name == "modulation"
        ):
            # In this backend modulation is a multiplicative gain control; its
            # neutral/default value is therefore one, not MIDI CC1's usual zero.
            default_value = 1.0
        elif instrument_type == "procedural_sfx":
            if name == "modulation":
                default_value = 0.5
            elif name == "distance":
                default_value = 0.2
        elif instrument_type == "mtg_solo_sax" and name == "noise":
            raw_default = manifest.get("noise_default", 0.22)
            if isinstance(raw_default, bool):
                raise ValueError("noise_default must be a finite number")
            noise_default = float(raw_default)
            if not math.isfinite(noise_default):
                raise ValueError("noise_default must be a finite number")
            default_value = min(1.0, max(0.0, noise_default))
        elif instrument_type == "soundfont" and name == "pan":
            raw_pan = manifest.get("pan", 0.0)
            if isinstance(raw_pan, bool):
                raise ValueError("pan must be a finite number")
            pan = float(raw_pan)
            if not math.isfinite(pan) or not -1.0 <= pan <= 1.0:
                raise ValueError("pan must be between -1 and 1")
            default_value = round((pan + 1.0) * 63.5) / 127.0

        if kind == "discrete":
            # Current pedal consumers threshold at 0.5.  Advertising the whole
            # float interval as exact would falsely promise half-pedal support.
            fidelity = "adapted"
            steps = 2
            allowed_values = (0.0, 1.0)
        elif instrument_type == "soundfont":
            # SoundFontInstrument rounds unit floats to a MIDI CC byte.
            fidelity = "adapted"
            steps = 128
            quantization_exponent = 1.25 if name == "expression" else 1.0
        elif instrument_type == "vpo_brass" and name == "modulation":
            raw_steps = manifest.get("modulation_attack_bins", 9)
            if isinstance(raw_steps, bool):
                raise ValueError("modulation_attack_bins must be an integer")
            steps = int(raw_steps)
            if not 2 <= steps <= 33:
                raise ValueError(
                    "modulation_attack_bins must be between 2 and 33"
                )
            fidelity = "adapted"
            quantization_exponent = 1.0
            source += ";manifest.modulation_attack_bins"

        records.append(
            ControlCapability(
                name=name,
                scope=_RUNTIME_CONTROL_SCOPE,
                kind=kind,
                minimum=0.0,
                maximum=1.0,
                default_value=default_value,
                interpolations=("step",),
                application=application,
                fidelity=fidelity,
                semantic_fidelity=semantic_fidelity,
                approximation_reason=approximation_reason,
                steps=steps,
                quantization_exponent=quantization_exponent,
                allowed_values=allowed_values,
                source=source,
                applicable_articulations=applicable_articulations,
            )
        )
    return tuple(records)


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


def _read_capability_json_object(
    path: str | Path,
) -> tuple[Path, dict[str, Any]]:
    """Capture one bounded, strict, descriptor-bound capability document."""

    identity, payload = read_plain_file_bytes(
        path,
        maximum_bytes=_MAX_CAPABILITY_JSON_BYTES,
    )
    document = strict_json_loads(
        payload,
        limits=AuthoringJsonLimits(
            max_document_bytes=_MAX_CAPABILITY_JSON_BYTES,
        ),
        require_object=True,
        require_js_safe_integers=True,
    )
    if type(document) is not dict:
        raise ValueError("instrument capability JSON root must be an object")
    return identity.path, document


def _manifest_finite_number(
    manifest: dict[str, Any],
    name: str,
) -> float | None:
    if name not in manifest:
        return None
    value = manifest[name]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"instrument manifest {name} must be a finite number")
    return float(value)


def _is_pitched(directory: Path, manifest: dict[str, Any]) -> bool:
    if str(manifest.get("type", "")) in _UNPITCHED_TYPES:
        return False
    calibration = directory / "音准校准.json"
    if calibration.is_file():
        _calibration_path, data = _read_capability_json_object(calibration)
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

    path, manifest = _read_capability_json_object(manifest_path)
    base = Path(root).resolve()
    directory = path.parent
    instrument_type = manifest.get("type", "")
    if type(instrument_type) is not str or not instrument_type.strip():
        raise ValueError("instrument manifest type must be a non-empty string")

    raw_articulations = manifest.get("articulations")
    allowed = manifest.get("allowed_articulations")
    if isinstance(raw_articulations, dict) and raw_articulations:
        if any(not name for name in raw_articulations):
            raise ValueError("manifest articulation names must not be empty")
        articulations = tuple(sorted(raw_articulations))
        source = "manifest.articulations"
    elif isinstance(allowed, list) and allowed:
        if any(type(name) is not str or not name.strip() for name in allowed):
            raise ValueError(
                "manifest allowed_articulations must contain non-empty strings"
            )
        articulations = tuple(sorted(allowed))
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
            default_only = manifest["default_articulation"]
            if type(default_only) is not str or not default_only.strip():
                raise ValueError(
                    "manifest default_articulation must be a non-empty string"
                )
            articulations = (default_only,)
            source = "manifest.default_articulation"
        else:
            articulations = ()
            source = "none"

    default_articulation = manifest.get("default_articulation")
    if default_articulation is not None:
        if type(default_articulation) is not str or not default_articulation.strip():
            raise ValueError(
                "manifest default_articulation must be a non-empty string"
            )
        if default_articulation not in articulations:
            raise ValueError(
                "manifest default_articulation is absent from the audited "
                "backend articulation vocabulary"
            )
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
    pitched = _is_pitched(directory, manifest)
    controls = _read_controls(instrument_type, manifest)
    note_velocity = _read_note_velocity(instrument_type, manifest)
    note_pitch = _read_note_pitch(
        instrument_type,
        manifest,
        pitched=pitched,
    )
    articulation_execution = _read_articulation_execution(
        instrument_type,
        manifest,
        articulations,
    )
    release_velocity_handler = (
        None
        if manifest.get("implementation") is not None
        else _BACKEND_RELEASE_VELOCITY.get(instrument_type)
    )
    release_velocity_source = (
        None
        if release_velocity_handler is None
        else f"backend:{release_velocity_handler}"
    )

    playable_ranges = _read_playable_ranges(manifest)
    articulation_playable_ranges = _read_articulation_playable_ranges(
        manifest,
        articulations,
    )
    license_status = manifest.get("license_status")
    if license_status is not None:
        if type(license_status) is not str:
            raise ValueError(f"license_status must be a string: {path}")
        if license_status not in {"approved", "grandfathered", "quarantined"}:
            raise ValueError(
                f"invalid license_status {license_status!r}: {path}"
            )
    collaboration_review_status = manifest.get(
        "collaboration_review_status"
    )
    if collaboration_review_status is not None:
        if type(collaboration_review_status) is not str:
            raise ValueError(
                f"collaboration_review_status must be a string: {path}"
            )
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
    note_min = _manifest_finite_number(manifest, "note_min")
    note_max = _manifest_finite_number(manifest, "note_max")
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

    name = manifest.get("name", directory.name)
    if type(name) is not str or not name.strip():
        raise ValueError("instrument manifest name must be a non-empty string")
    quality_tier = manifest.get("quality_tier")
    if quality_tier is not None and (
        type(quality_tier) is not str or not quality_tier.strip()
    ):
        raise ValueError("instrument manifest quality_tier must be a string")
    pitch_mode = manifest.get("pitch_mode")
    if pitch_mode is not None and type(pitch_mode) is not str:
        raise ValueError("instrument manifest pitch_mode must be a string")
    fixed_midi_note = _manifest_finite_number(manifest, "fixed_midi_note")

    return InstrumentCapability(
        name=name,
        relative_path=directory.relative_to(base).as_posix(),
        manifest_path=str(path),
        implementation_type=instrument_type,
        pitched=pitched,
        note_min=note_min,
        note_max=note_max,
        articulations=articulations,
        default_articulation=default_articulation,
        articulation_source=source,
        onset_seconds=onset_seconds,
        quality_tier=(
            quality_tier
        ),
        license_status=license_status,
        collaboration_review_status=collaboration_review_status,
        pitch_mode=pitch_mode,
        fixed_midi_note=fixed_midi_note,
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
        controls=controls,
        supports_release_velocity=release_velocity_source is not None,
        release_velocity_source=release_velocity_source,
        note_velocity=note_velocity,
        note_pitch=note_pitch,
        articulation_execution=articulation_execution,
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
