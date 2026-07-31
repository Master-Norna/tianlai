from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .sampler import SampleInstrument
from .sfz import note_number
from .tuning import EqualTemperament
from .vpo_strings import parse_vpo_sfz


@dataclass(frozen=True, slots=True)
class PercussionArticulation:
    sfz_name: str
    source_key_min: float | None = None
    source_key_max: float | None = None
    key_shift: float = 0.0
    fixed_source_key: float | None = None
    one_shot: bool = True
    use_embedded_loops: bool = False
    release_override_seconds: float | None = None
    release_trigger: bool = False
    choke: tuple[str, ...] = ()
    offset_overrides: tuple[tuple[str, int], ...] = ()
    keep_sequence_position: int | None = None


@dataclass(frozen=True, slots=True)
class PercussionProfile:
    pitched: bool
    default_articulation: str
    articulations: dict[str, PercussionArticulation]
    source_directory: str = "Percussion"


PERCUSSION_PROFILES: dict[str, PercussionProfile] = {
    "triangle": PercussionProfile(
        pitched=False,
        default_articulation="open",
        articulations={
            "open": PercussionArticulation(
                "misc.sfz", 81, 81, fixed_source_key=81, choke=("roll",)
            ),
            "muted": PercussionArticulation(
                "misc.sfz",
                80,
                80,
                fixed_source_key=80,
                choke=("open",),
            ),
            "roll": PercussionArticulation(
                "misc.sfz",
                79,
                79,
                fixed_source_key=79,
                one_shot=False,
            ),
        },
    ),
    "timpani": PercussionProfile(
        pitched=True,
        default_articulation="hit",
        articulations={
            "hit": PercussionArticulation("timpani-hit.sfz", one_shot=True),
            "roll": PercussionArticulation(
                "timpani-roll.sfz",
                one_shot=False,
                use_embedded_loops=True,
                release_trigger=True,
            ),
        },
    ),
    "snare": PercussionProfile(
        pitched=False,
        default_articulation="hit",
        articulations={
            "left": PercussionArticulation("snare.sfz", 48, 48, fixed_source_key=48),
            "alternating": PercussionArticulation(
                "snare.sfz", 49, 49, fixed_source_key=49
            ),
            "hit": PercussionArticulation("snare.sfz", 54, 54, fixed_source_key=54),
            "right": PercussionArticulation("snare.sfz", 50, 50, fixed_source_key=50),
            "kit2_left": PercussionArticulation(
                "snare.sfz", 53, 53, fixed_source_key=53
            ),
            "kit2_right": PercussionArticulation(
                "snare.sfz", 55, 55, fixed_source_key=55
            ),
            "tap": PercussionArticulation("snare.sfz", 56, 56, fixed_source_key=56),
            "roll": PercussionArticulation(
                "snare.sfz", 57, 57, fixed_source_key=57, one_shot=False
            ),
            "roll_looped": PercussionArticulation(
                "snare.sfz",
                52,
                52,
                fixed_source_key=52,
                one_shot=False,
                use_embedded_loops=True,
            ),
        },
    ),
    "xylophone": PercussionProfile(
        pitched=True,
        default_articulation="hit",
        articulations={
            # VPO deliberately maps xylophone at written pitch.  Tianlai's
            # instrument layer accepts concert/sounding pitch, one octave up.
            "hit": PercussionArticulation("xylophone.sfz", key_shift=12.0),
        },
    ),
    "woodblock": PercussionProfile(
        pitched=False,
        default_articulation="high",
        articulations={
            "low": PercussionArticulation("misc.sfz", 76, 76, fixed_source_key=76),
            "high": PercussionArticulation("misc.sfz", 77, 77, fixed_source_key=77),
        },
    ),
    "bass_drum": PercussionProfile(
        pitched=False,
        default_articulation="drum_2",
        articulations={
            "drum_1": PercussionArticulation(
                "bassdrum.sfz", 36, 36, fixed_source_key=36
            ),
            "drum_2": PercussionArticulation(
                "bassdrum.sfz", 38, 38, fixed_source_key=38
            ),
        },
    ),
    "cymbals": PercussionProfile(
        pitched=False,
        default_articulation="crash",
        articulations={
            "roll_soft": PercussionArticulation(
                "cymbals.sfz", 60, 60, fixed_source_key=60, one_shot=False
            ),
            "piatti": PercussionArticulation(
                "cymbals.sfz", 61, 61, fixed_source_key=61
            ),
            "roll_alt": PercussionArticulation(
                "cymbals.sfz", 62, 62, fixed_source_key=62, one_shot=False
            ),
            "piatti_high": PercussionArticulation(
                "cymbals.sfz", 63, 63, fixed_source_key=63
            ),
            "crescendo_short": PercussionArticulation(
                "cymbals.sfz", 65, 65, fixed_source_key=65
            ),
            "crash": PercussionArticulation(
                "cymbals.sfz", 66, 66, fixed_source_key=66
            ),
            "crescendo_medium": PercussionArticulation(
                "cymbals.sfz", 67, 67, fixed_source_key=67
            ),
            "suspended_hit": PercussionArticulation(
                "cymbals.sfz", 68, 68, fixed_source_key=68
            ),
            "crescendo_long": PercussionArticulation(
                "cymbals.sfz", 69, 69, fixed_source_key=69
            ),
            "suspended_high": PercussionArticulation(
                "cymbals.sfz", 70, 70, fixed_source_key=70
            ),
        },
    ),
    "tubular_bells": PercussionProfile(
        pitched=True,
        default_articulation="open",
        articulations={
            # A3 (57) is the upstream silent damper keyswitch, not a bell.
            "open": PercussionArticulation(
                "tubular-bells.sfz",
                60,
                79,
                one_shot=False,
                use_embedded_loops=True,
            ),
            "damped": PercussionArticulation(
                "tubular-bells.sfz",
                60,
                79,
                one_shot=False,
                use_embedded_loops=True,
                release_override_seconds=0.12,
            ),
        },
    ),
    "vcsl_tubular_bells_2": PercussionProfile(
        pitched=True,
        default_articulation="open",
        articulations={
            "open": PercussionArticulation(
                "Tubular Bells 2.sfz",
                60,
                79,
                one_shot=False,
                offset_overrides=(
                    (
                        "Idiophones/Struck Idiophones/Tubular Bells 2/"
                        "TB_hit_B4_v2_1.wav",
                        0,
                    ),
                    (
                        "Idiophones/Struck Idiophones/Tubular Bells 2/"
                        "TB_hit_C5_v4_1.wav",
                        0,
                    ),
                ),
            ),
            "damped": PercussionArticulation(
                "Tubular Bells 2.sfz",
                60,
                79,
                one_shot=False,
                release_override_seconds=0.12,
                offset_overrides=(
                    (
                        "Idiophones/Struck Idiophones/Tubular Bells 2/"
                        "TB_hit_B4_v2_1.wav",
                        0,
                    ),
                    (
                        "Idiophones/Struck Idiophones/Tubular Bells 2/"
                        "TB_hit_C5_v4_1.wav",
                        0,
                    ),
                ),
            ),
        },
        source_directory="Idiophones/Struck Idiophones",
    ),
    "glockenspiel": PercussionProfile(
        pitched=True,
        default_articulation="hit",
        articulations={"hit": PercussionArticulation("glockenspiel.sfz")},
    ),
    "vibraphone": PercussionProfile(
        pitched=True,
        default_articulation="damped",
        articulations={
            "damped": PercussionArticulation(
                "vibraphone-auto-damp.sfz",
                53,
                89,
                one_shot=False,
                keep_sequence_position=1,
            ),
            # E2 (40) in the open patch is a silent damper keyswitch.
            "open": PercussionArticulation(
                "vibraphone-open.sfz",
                53,
                89,
                one_shot=True,
                keep_sequence_position=1,
            ),
        },
    ),
}


def _velocity_limits(values: dict[str, str]) -> tuple[float, float]:
    low = max(0.0, (float(values.get("lovel", 0.0)) - 0.5) / 127.0)
    high = min(1.0, (float(values.get("hivel", 127.0)) + 0.5) / 127.0)
    if "xfin_lovel" in values and "xfin_hivel" in values:
        midpoint = (float(values["xfin_lovel"]) + float(values["xfin_hivel"])) / 2.0
        low = max(low, (math.floor(midpoint) + 0.5) / 127.0)
    if "xfout_lovel" in values and "xfout_hivel" in values:
        midpoint = (float(values["xfout_lovel"]) + float(values["xfout_hivel"])) / 2.0
        high = min(high, (math.floor(midpoint) + 0.5) / 127.0)
    return low, high


def _region_key_limits(values: dict[str, str]) -> tuple[float, float, float]:
    root_value = values.get("pitch_keycenter", values.get("key"))
    if root_value is None:
        raise ValueError("VPO percussion region has no pitch_keycenter or key")
    root = note_number(root_value)
    low = note_number(values.get("lokey", values.get("key", root_value)))
    high = note_number(values.get("hikey", values.get("key", root_value)))
    return low, high, root


def vpo_percussion_regions(
    sfz_path: str | Path,
    *,
    source_key_min: float | None = None,
    source_key_max: float | None = None,
    key_shift: float = 0.0,
    use_embedded_loops: bool = False,
    trigger: str = "attack",
    keep_sequence_position: int | None = None,
) -> list[dict[str, Any]]:
    """Convert the attack regions of one VPO percussion mapping.

    Unlike the generic SFZ reader, this keeps paths containing spaces, VPO's
    velocity splits, transpose/tune opcodes and the written-to-sounding key
    shifts required by xylophone.  Explicit sequence regions normally remain
    duplicate candidates and are consumed by SampleInstrument's deterministic
    round robin.  ``keep_sequence_position`` can deliberately retain only one
    upstream sequence when the other positions are not real alternate takes.
    """

    path = Path(sfz_path).resolve()
    converted: list[dict[str, Any]] = []
    for index, values in enumerate(parse_vpo_sfz(path)):
        if values.get("trigger", "attack").lower() != trigger.lower():
            continue
        if keep_sequence_position is not None and "seq_length" in values:
            position = int(values.get("seq_position", 1))
            if position != keep_sequence_position:
                continue
        sample_name = values.get("sample")
        if not sample_name:
            continue
        key_min, key_max, root = _region_key_limits(values)
        if source_key_min is not None and key_max < source_key_min:
            continue
        if source_key_max is not None and key_min > source_key_max:
            continue
        if source_key_min is not None:
            key_min = max(key_min, source_key_min)
        if source_key_max is not None:
            key_max = min(key_max, source_key_max)
        sample_path = (path.parent / sample_name.replace("\\", "/")).resolve()
        transpose = float(values.get("transpose", 0.0))
        velocity_min, velocity_max = _velocity_limits(values)
        pan = min(1.0, max(-1.0, float(values.get("pan", 0.0)) / 100.0))
        item: dict[str, Any] = {
            "sample": str(sample_path),
            "stable_key": sample_name.replace("\\", "/"),
            "root_midi": root + key_shift - transpose,
            "measured_tuning_cents": -float(values.get("tune", 0.0)),
            "key_min": key_min + key_shift,
            "key_max": key_max + key_shift,
            "velocity_min": velocity_min,
            "velocity_max": velocity_max,
            "gain_db": float(values.get("volume", 0.0)),
            "pan": pan,
            "stereo_width": min(
                2.0, max(0.0, float(values.get("width", 100.0)) / 100.0)
            ),
            "pitch_random_cents": float(values.get("pitch_random", 0.0)),
            "amplitude_random_db": float(values.get("amp_random", 0.0)),
            "delay_random_seconds": float(values.get("delay_random", 0.0)),
            "delay_seconds": float(values.get("delay", 0.0)),
            "attack_seconds": float(values.get("ampeg_attack", 0.0)),
            "release_seconds": float(values.get("ampeg_release", 0.25)),
            "offset_frames": int(float(values.get("offset", 0.0))),
        }
        if "seq_length" in values and keep_sequence_position is None:
            item["round_robin_length"] = int(values["seq_length"])
            # SFZ defaults sequence_position to the first position.  VPO's
            # xylophone relies on that default for every RR1 region.
            item["round_robin_position"] = int(values.get("seq_position", 1))
        if use_embedded_loops:
            item["use_embedded_loop"] = True
            # SFZ uses an embedded WAV loop continuously when loop_mode is
            # omitted.  Carry the mode explicitly so the release stage is not
            # silently changed to sampler.py's historical loop_sustain mode.
            item["loop_mode"] = values.get("loop_mode", "loop_continuous").lower()
        converted.append(item)
    if not converted:
        selection = f" keys {source_key_min}..{source_key_max}" if source_key_min is not None else ""
        raise ValueError(f"VPO percussion SFZ has no attack regions{selection}: {path}")
    return converted


def _profile_source_root(
    asset_root: str | Path,
    profile: PercussionProfile,
) -> Path:
    root = Path(asset_root).resolve()
    source_root = (root / profile.source_directory).resolve()
    try:
        source_root.relative_to(root)
    except ValueError as error:
        raise ValueError(
            f"percussion source directory escapes asset root: "
            f"{profile.source_directory!r}"
        ) from error
    return source_root


def _apply_articulation_overrides(
    regions: list[dict[str, Any]],
    *,
    asset_root: str | Path,
    spec: PercussionArticulation,
) -> None:
    """Apply explicit project-layer fixes without changing upstream SFZ files."""

    root = Path(asset_root).resolve()
    offset_overrides = dict(spec.offset_overrides)
    if len(offset_overrides) != len(spec.offset_overrides):
        raise ValueError("percussion offset override paths must be unique")
    seen: set[str] = set()
    for region in regions:
        path = Path(region["sample"]).resolve()
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError as error:
            raise ValueError(f"percussion sample escapes asset root: {path}") from error
        if relative in offset_overrides:
            offset = int(offset_overrides[relative])
            if offset < 0:
                raise ValueError(f"percussion offset override must be non-negative: {relative}")
            region["offset_frames"] = offset
            seen.add(relative)
        if spec.release_override_seconds is not None:
            region["release_seconds"] = spec.release_override_seconds
    missing = set(offset_overrides) - seen
    if missing:
        paths = ", ".join(sorted(missing))
        raise ValueError(f"percussion offset overrides did not match SFZ regions: {paths}")


def _with_internal_note(
    event: PerformanceEvent,
    note_id: int,
    *,
    midi_note: float | None = None,
) -> PerformanceEvent:
    payload = dict(event.payload)
    payload["note_id"] = note_id
    if midi_note is not None:
        payload.pop("pitch_hz", None)
        payload["midi_note"] = midi_note
    return PerformanceEvent(event.sample, event.sequence, event.type, payload)


@dataclass(frozen=True, slots=True)
class _NoteRoute:
    articulation: str
    internal_note_id: int
    releasable: bool
    midi_note: float | None = None
    velocity: float = 1.0


class VpoPercussionInstrument(Instrument):
    """Dedicated deterministic adapter for mapped orchestral percussion."""

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        self.instrument_name = str(manifest["instrument_name"])
        profile_name = str(manifest["profile"])
        try:
            self.profile = PERCUSSION_PROFILES[profile_name]
        except KeyError as error:
            choices = ", ".join(sorted(PERCUSSION_PROFILES))
            raise ValueError(
                f"unknown percussion profile {profile_name!r}; choose from {choices}"
            ) from error

        self.note_min = float(manifest.get("note_min", 0.0))
        self.note_max = float(manifest.get("note_max", 127.0))
        if not 0.0 <= self.note_min <= self.note_max <= 127.0:
            raise ValueError("VPO percussion note range must satisfy 0 <= min <= max <= 127")

        asset_root = (Path(base_directory) / str(manifest["asset_root"])).resolve()
        percussion_root = _profile_source_root(asset_root, self.profile)
        if not percussion_root.is_dir():
            raise ValueError(
                f"{self.instrument_name} 打击乐音源目录不存在：{percussion_root}。"
                "请按来源.md 安装并核对冻结版本。"
            )

        calibration: dict[str, Any] = {}
        calibration_name = manifest.get("pitch_calibration")
        if calibration_name:
            calibration_path = Path(base_directory) / str(calibration_name)
            if calibration_path.is_file():
                document = json.loads(calibration_path.read_text(encoding="utf-8"))
                calibration = document.get("samples", {})
                if not isinstance(calibration, dict):
                    raise ValueError("percussion pitch calibration samples must be an object")

        shared_cache: dict[Path, Any] = {}
        gain = float(manifest.get("gain", 0.35))
        velocity_exponent = float(manifest.get("velocity_exponent", 0.72))
        release_seconds = float(manifest.get("release_seconds", 0.4))
        articulation_gain = manifest.get("articulation_gain", {})
        if not isinstance(articulation_gain, dict):
            raise ValueError("articulation_gain must be an object")
        self.engines: dict[str, SampleInstrument] = {}
        self.release_engines: dict[str, SampleInstrument] = {}
        for name, spec in self.profile.articulations.items():
            sfz_path = percussion_root / spec.sfz_name
            if not sfz_path.is_file():
                raise ValueError(f"{self.instrument_name} 奏法映射不存在：{sfz_path}")
            regions = vpo_percussion_regions(
                sfz_path,
                source_key_min=spec.source_key_min,
                source_key_max=spec.source_key_max,
                key_shift=spec.key_shift,
                use_embedded_loops=spec.use_embedded_loops,
                keep_sequence_position=spec.keep_sequence_position,
            )
            _apply_articulation_overrides(
                regions,
                asset_root=asset_root,
                spec=spec,
            )
            for region in regions:
                try:
                    relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                except ValueError:
                    continue
                measured = calibration.get(relative)
                if isinstance(measured, dict) and isinstance(
                    measured.get("detune_cents"), (int, float)
                ):
                    region["measured_tuning_cents"] = float(measured["detune_cents"])
            self.engines[name] = SampleInstrument.from_manifest(
                {
                    "regions": regions,
                    "reference_a4_hz": 440.0,
                    "gain": gain * float(articulation_gain.get(name, 1.0)),
                    "velocity_exponent": velocity_exponent,
                    "release_seconds": release_seconds,
                },
                sample_rate,
                base_directory=base_directory,
                sample_cache=shared_cache,
            )
            if spec.release_trigger:
                release_regions = vpo_percussion_regions(
                    sfz_path,
                    source_key_min=spec.source_key_min,
                    source_key_max=spec.source_key_max,
                    key_shift=spec.key_shift,
                    trigger="release",
                    keep_sequence_position=spec.keep_sequence_position,
                )
                for region in release_regions:
                    try:
                        relative = Path(region["sample"]).relative_to(asset_root).as_posix()
                    except ValueError:
                        continue
                    measured = calibration.get(relative)
                    if isinstance(measured, dict) and isinstance(
                        measured.get("detune_cents"), (int, float)
                    ):
                        region["measured_tuning_cents"] = float(
                            measured["detune_cents"]
                        )
                self.release_engines[name] = SampleInstrument.from_manifest(
                    {
                        "regions": release_regions,
                        "reference_a4_hz": 440.0,
                        "gain": gain
                        * float(articulation_gain.get(name, 1.0))
                        * float(manifest.get("release_trigger_gain", 0.62)),
                        "velocity_exponent": velocity_exponent,
                        "release_seconds": release_seconds,
                    },
                    sample_rate,
                    base_directory=base_directory,
                    sample_cache=shared_cache,
                )

        default_articulation = str(
            manifest.get("default_articulation", self.profile.default_articulation)
        )
        if default_articulation not in self.engines:
            raise ValueError(
                f"unsupported default {self.instrument_name} articulation "
                f"{default_articulation!r}"
            )
        self.articulation = default_articulation
        self.routes: dict[int, _NoteRoute] = {}
        self._internal_note_id = int(manifest.get("auxiliary_note_id_base", 1_700_000_000))
        self.expression = 1.0
        self.expression_target = 1.0
        smoothing_seconds = max(
            0.001, float(manifest.get("expression_smoothing_seconds", 0.012))
        )
        self._expression_coefficient = 1.0 - math.exp(
            -1.0 / (smoothing_seconds * sample_rate)
        )
        self._choke_seconds = max(0.001, float(manifest.get("choke_seconds", 0.035)))

    def _next_internal_id(self) -> int:
        self._internal_note_id += 1
        return self._internal_note_id

    def _event_note(self, event: PerformanceEvent, tuning: EqualTemperament) -> float:
        if "midi_note" in event.payload:
            return float(event.payload["midi_note"])
        return 69.0 + 12.0 * math.log2(event_pitch_hz(event, tuning) / tuning.a4_hz)

    def _choke(self, names: tuple[str, ...]) -> None:
        for name in names:
            engine = self.engines.get(name)
            if engine is None:
                continue
            for note_id in tuple(engine.voices):
                engine.release_note(note_id, release_seconds=self._choke_seconds)

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if name not in self.engines:
                choices = ", ".join(sorted(self.engines))
                raise ValueError(
                    f"unsupported {self.instrument_name} articulation {name!r}; "
                    f"choose from {choices}"
                )
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload["name"])
            if name == "expression":
                self.expression_target = float(event.payload["value"]) ** 1.25
            elif name == "sustain_pedal":
                for articulation, engine in self.engines.items():
                    if not self.profile.articulations[articulation].one_shot:
                        engine.handle_event(event, tuning)
            return

        if event.type == "note_on":
            if self.profile.pitched:
                note = self._event_note(event, tuning)
                if not self.note_min <= note <= self.note_max:
                    raise ValueError(
                        f"{self.instrument_name} note {note:.3f} is outside sampled "
                        f"range {self.note_min:g}..{self.note_max:g}"
                    )
            spec = self.profile.articulations[self.articulation]
            self._choke(spec.choke)
            internal_id = self._next_internal_id()
            mapped_note = (
                spec.fixed_source_key + spec.key_shift
                if spec.fixed_source_key is not None
                else None
            )
            routed = _with_internal_note(event, internal_id, midi_note=mapped_note)
            engine_tuning = tuning if self.profile.pitched else EqualTemperament(440.0)
            self.engines[self.articulation].handle_event(routed, engine_tuning)
            self.routes[int(event.payload["note_id"])] = _NoteRoute(
                self.articulation,
                internal_id,
                not spec.one_shot,
                (
                    self._event_note(event, tuning)
                    if self.profile.pitched
                    else mapped_note
                ),
                float(event.payload["velocity"]),
            )
            return

        if event.type == "note_off":
            route = self.routes.pop(int(event.payload["note_id"]), None)
            if route is None or not route.releasable:
                return
            release_engine = self.release_engines.get(route.articulation)
            if release_engine is not None and route.midi_note is not None:
                release_event = PerformanceEvent(
                    event.sample,
                    event.sequence,
                    "note_on",
                    {
                        "note_id": self._next_internal_id(),
                        "midi_note": route.midi_note,
                        # These are SFZ release-trigger samples: their
                        # velocity follows the corresponding note-on, not
                        # the optional MIDI note-off velocity.
                        "velocity": route.velocity,
                    },
                )
                release_engine.handle_event(release_event, tuning)
            routed = _with_internal_note(event, route.internal_note_id)
            engine_tuning = tuning if self.profile.pitched else EqualTemperament(440.0)
            self.engines[route.articulation].handle_event(routed, engine_tuning)

    def render_frame(self) -> StereoFrame:
        self.expression += (
            self.expression_target - self.expression
        ) * self._expression_coefficient
        left = 0.0
        right = 0.0
        for engine in self.engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        for engine in self.release_engines.values():
            engine_left, engine_right = engine.render_frame()
            left += engine_left
            right += engine_right
        return left * self.expression, right * self.expression

    @property
    def active_voice_count(self) -> int:
        return sum(
            engine.active_voice_count
            for engine in (*self.engines.values(), *self.release_engines.values())
        )


def percussion_source_regions(
    asset_root: str | Path,
    profile_name: str,
) -> dict[str, list[dict[str, Any]]]:
    """Return the exact source regions selected by one public profile."""

    root = Path(asset_root).resolve()
    try:
        profile = PERCUSSION_PROFILES[profile_name]
    except KeyError as error:
        choices = ", ".join(sorted(PERCUSSION_PROFILES))
        raise ValueError(
            f"unknown percussion profile {profile_name!r}; choose from {choices}"
        ) from error
    source_root = _profile_source_root(root, profile)
    result: dict[str, list[dict[str, Any]]] = {}
    for name, spec in profile.articulations.items():
        result[name] = vpo_percussion_regions(
            source_root / spec.sfz_name,
            source_key_min=spec.source_key_min,
            source_key_max=spec.source_key_max,
            key_shift=spec.key_shift,
            use_embedded_loops=spec.use_embedded_loops,
            keep_sequence_position=spec.keep_sequence_position,
        )
        _apply_articulation_overrides(
            result[name],
            asset_root=root,
            spec=spec,
        )
        if spec.release_trigger:
            result[f"{name}__release"] = vpo_percussion_regions(
                source_root / spec.sfz_name,
                source_key_min=spec.source_key_min,
                source_key_max=spec.source_key_max,
                key_shift=spec.key_shift,
                trigger="release",
                keep_sequence_position=spec.keep_sequence_position,
            )
    return result


def generate_percussion_pitch_calibration(
    manifest_path: str | Path,
    *,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate a reproducible pitch report for one percussion profile.

    Unpitched profiles explicitly produce an N/A report.  Tubular bells are
    intentionally mapping-only: their prominent partials are inharmonic, so a
    monophonic FFT peak is not written as a fictitious fundamental correction.
    """

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    profile_name = str(manifest["profile"])
    profile = PERCUSSION_PROFILES[profile_name]
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / "音准校准.json"
    )
    if not profile.pitched:
        document: dict[str, Any] = {
            "applicable": False,
            "profile": profile_name,
            "reason": "无固定音高打击；输入音高只选择击法，不对样本做十二平均律校正。",
            "a4_hz": None,
            "samples": {},
        }
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return document

    region_sets = percussion_source_regions(asset_root, profile_name)
    roots: dict[Path, float] = {}
    for regions in region_sets.values():
        for region in regions:
            path = Path(region["sample"])
            root_midi = float(region["root_midi"])
            previous = roots.setdefault(path, root_midi)
            if not math.isclose(previous, root_midi, abs_tol=1e-9):
                raise ValueError(
                    f"one VPO percussion sample is mapped to inconsistent roots: {path}"
                )

    if profile_name in {"tubular_bells", "vcsl_tubular_bells_2"}:
        root_notes = sorted({root_midi for root_midi in roots.values()})
        samples = {
            path.relative_to(asset_root).as_posix(): {
                "root_midi": root_midi,
                "expected_equal_temperament_hz": round(
                    440.0 * (2.0 ** ((root_midi - 69.0) / 12.0)), 6
                ),
                "automatic_cents_correction": None,
                "reason": "管钟为强非谐波瞬态；单 FFT 峰不等于听觉基音。",
            }
            for path, root_midi in sorted(
                roots.items(), key=lambda item: item[0].as_posix()
            )
        }
        document = {
            "applicable": True,
            "profile": profile_name,
            "method": "SFZ root mapping audit; automatic FFT correction disabled",
            "a4_hz": 440.0,
            "summary": {
                "sample_count": len(samples),
                "unique_root_count": len(root_notes),
                "root_midi_notes": root_notes,
                "recorded_velocity_layer_count": 2,
                "round_robin_count": 0,
                "automatic_correction_count": 0,
                "human_spectral_review": "pending",
            },
            "samples": samples,
        }
        destination.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return document

    from .analysis import analyze_file_pitch

    samples: dict[str, dict[str, float]] = {}
    for path, root_midi in sorted(roots.items(), key=lambda item: item[0].as_posix()):
        expected_hz = 440.0 * (2.0 ** ((root_midi - 69.0) / 12.0))
        measurement = analyze_file_pitch(
            path,
            expected_hz,
            start_seconds=float(manifest.get("calibration_start_seconds", 0.08)),
            maximum_frames=131_072,
            search_cents=float(manifest.get("calibration_search_cents", 240.0)),
        )
        samples[path.relative_to(asset_root).as_posix()] = {
            "root_midi": root_midi,
            "measured_hz": round(measurement.measured_hz, 6),
            "detune_cents": round(measurement.detune_cents, 6),
        }
    detunes = [item["detune_cents"] for item in samples.values()]
    document = {
        "applicable": True,
        "profile": profile_name,
        "method": "windowed FFT near the SFZ root; raw source WAV; A4=440 Hz",
        "summary": {
            "sample_count": len(samples),
            "median_detune_cents": round(statistics.median(detunes), 6),
            "maximum_absolute_detune_cents": round(max(map(abs, detunes)), 6),
        },
        "samples": samples,
    }
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def generate_percussion_resource_verification(
    manifest_path: str | Path,
    *,
    license_files: tuple[str, ...],
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Freeze selected SFZ/WAV, licence and upstream version evidence hashes."""

    source_manifest = Path(manifest_path).resolve()
    manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    profile_name = str(manifest["profile"])
    profile = PERCUSSION_PROFILES[profile_name]
    asset_root = (source_manifest.parent / str(manifest["asset_root"])).resolve()
    region_sets = percussion_source_regions(asset_root, profile_name)
    source_root = _profile_source_root(asset_root, profile)
    sfz_paths = sorted(
        {
            source_root / spec.sfz_name
            for spec in profile.articulations.values()
        },
        key=lambda path: path.as_posix(),
    )
    sample_paths = sorted(
        {
            Path(region["sample"])
            for regions in region_sets.values()
            for region in regions
        },
        key=lambda path: path.relative_to(asset_root).as_posix(),
    )
    for path in (*sfz_paths, *sample_paths):
        if not path.is_file():
            raise ValueError(f"percussion evidence file is missing: {path}")

    sample_lines: list[str] = []
    sample_hashes: dict[str, str] = {}
    sample_bytes = 0
    for path in sample_paths:
        relative = path.relative_to(asset_root).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        sample_hashes[relative] = digest
        sample_lines.append(f"{digest}  {relative}\n")
        sample_bytes += path.stat().st_size
    sample_set_sha256 = hashlib.sha256(
        "".join(sample_lines).encode("utf-8")
    ).hexdigest()

    def hash_relatives(relatives: tuple[str, ...]) -> dict[str, str]:
        result: dict[str, str] = {}
        for relative in relatives:
            path = asset_root / relative
            if not path.is_file():
                raise ValueError(f"percussion evidence file is missing: {path}")
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    source_sfz_sha256 = {
        path.relative_to(asset_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sfz_paths
    }
    if profile_name == "vcsl_tubular_bells_2":
        expected_shape = {
            "sample_count": 22,
            "sample_bytes": 75_496_208,
            "sample_set_sha256": (
                "f35617d893237a552b722d8471d9f73790cc75a12dcf615a6b80db4dd966a3cc"
            ),
            "source_sfz_sha256": {
                "Idiophones/Struck Idiophones/Tubular Bells 2.sfz": (
                    "9ce2237fe3d23921500c6e537bb03b563b2a29dc7f9ba31c6b72bc49631208c6"
                )
            },
            "evidence_sha256": {
                "README.md": (
                    "e360f24c120c9ad734cc8508695e09a61ddc4cae5a59c6c9af33fe501b6c9a5b"
                )
            },
        }
        evidence_sha256 = hash_relatives(license_files)
        actual_shape = {
            "sample_count": len(sample_paths),
            "sample_bytes": sample_bytes,
            "sample_set_sha256": sample_set_sha256,
            "source_sfz_sha256": source_sfz_sha256,
            "evidence_sha256": evidence_sha256,
        }
        if actual_shape != expected_shape:
            raise ValueError(
                "VCSL Tubular Bells 2 does not match the frozen "
                f"v1.2.2-RC resource shape: {actual_shape}"
            )

        import soundfile as sf

        formats: dict[str, int] = {}
        durations: list[float] = []
        sample_peaks: dict[Path, float] = {}
        clipped_samples = 0
        silent_samples = 0
        for path in sample_paths:
            info = sf.info(path)
            format_key = (
                f"{path.suffix.lower()}:{info.samplerate}Hz:"
                f"{info.channels}ch:{info.subtype}"
            )
            formats[format_key] = formats.get(format_key, 0) + 1
            durations.append(float(info.duration))
            audio, _sample_rate = sf.read(
                path,
                dtype="float32",
                always_2d=True,
            )
            peak = float(abs(audio).max()) if audio.size else 0.0
            sample_peaks[path] = peak
            clipped_samples += int(peak >= 1.0)
            silent_samples += int(peak <= 1e-6)

        open_regions = region_sets["open"]
        upstream_peak = max(
            sample_peaks[Path(region["sample"])]
            * (10.0 ** (float(region["gain_db"]) / 20.0))
            for region in open_regions
        )
        project_gain = float(manifest.get("gain", 1.0))
        project_peak = upstream_peak * project_gain
        headroom_db = (
            -20.0 * math.log10(project_peak)
            if project_peak > 0.0
            else float("inf")
        )
        if headroom_db < 6.0:
            raise ValueError(
                f"VCSL Tubular Bells 2 project gain leaves only "
                f"{headroom_db:.3f} dB headroom"
            )

        roots = sorted({int(region["root_midi"]) for region in open_regions})
        velocity_bands = sorted(
            {
                (
                    (
                        0
                        if float(region["velocity_min"]) <= 0.0
                        else round(float(region["velocity_min"]) * 127.0 + 0.5)
                    ),
                    (
                        127
                        if float(region["velocity_max"]) >= 1.0
                        else round(float(region["velocity_max"]) * 127.0 - 0.5)
                    ),
                )
                for region in open_regions
            }
        )
        coverage_min = min(int(region["key_min"]) for region in open_regions)
        coverage_max = max(int(region["key_max"]) for region in open_regions)
        maximum_stretch = max(
            max(
                abs(float(region["key_min"]) - float(region["root_midi"])),
                abs(float(region["key_max"]) - float(region["root_midi"])),
            )
            for region in open_regions
        )
        round_robin_regions = sum(
            "round_robin_length" in region for region in open_regions
        )
        looped_regions = sum(
            bool(region.get("use_embedded_loop")) for region in open_regions
        )
        stereo_regions = sum(
            math.isclose(float(region["stereo_width"]), 1.0)
            for region in open_regions
        )
        document = {
            "upstream": manifest["upstream"],
            "origin": manifest["origin"],
            "upstream_version": manifest["upstream_version"],
            "upstream_commit": manifest.get("upstream_commit"),
            "license": manifest["license"],
            "profile": profile_name,
            "source_sfz_sha256": source_sfz_sha256,
            "evidence_sha256": evidence_sha256,
            "sample_count": len(sample_paths),
            "sample_bytes": sample_bytes,
            "sample_sha256": sample_hashes,
            "sample_set_sha256": sample_set_sha256,
            "sample_set_algorithm": (
                "Sort unique VCSL-relative UTF-8 paths; for each write "
                "'<lowercase file sha256>  <path>\\n'; SHA-256 the "
                "concatenated UTF-8 bytes."
            ),
            "sample_formats": formats,
            "mapping": {
                "unique_root_count": len(roots),
                "root_midi_notes": roots,
                "recorded_velocity_layer_count": len(velocity_bands),
                "velocity_bands": [list(band) for band in velocity_bands],
                "round_robin_count": 0,
                "round_robin_regions": round_robin_regions,
                "coverage_midi": [coverage_min, coverage_max],
                "maximum_stretch_semitones": maximum_stretch,
                "embedded_loop_count": looped_regions,
                "stereo_regions": stereo_regions,
                "open_region_count": len(open_regions),
                "damped_region_count": len(region_sets["damped"]),
                "damped_source": "same recordings; project 120 ms release envelope",
            },
            "project_overrides": {
                "upstream_sfz_unchanged": True,
                "offset_frames": {
                    (
                        "Idiophones/Struck Idiophones/Tubular Bells 2/"
                        "TB_hit_B4_v2_1.wav"
                    ): {"upstream": 1026, "project": 0},
                    (
                        "Idiophones/Struck Idiophones/Tubular Bells 2/"
                        "TB_hit_C5_v4_1.wav"
                    ): {"upstream": 2727, "project": 0},
                },
            },
            "audio_integrity": {
                "source_clipped_samples": clipped_samples,
                "silent_samples": silent_samples,
                "duration_seconds": {
                    "minimum": round(min(durations), 6),
                    "median": round(statistics.median(durations), 6),
                    "maximum": round(max(durations), 6),
                },
                "maximum_upstream_region_peak_dbfs": round(
                    20.0 * math.log10(upstream_peak),
                    6,
                ),
                "project_gain": project_gain,
                "maximum_project_peak_dbfs": round(
                    20.0 * math.log10(project_peak),
                    6,
                ),
                "minimum_headroom_db": round(headroom_db, 6),
            },
        }
    else:
        document = {
            "upstream": "Virtual Playing Orchestra",
            "sfz_version": "Standard Orchestra 3.3 (2026-06-27)",
            "wave_version": "Wave Files 3.2 (2026-06-27)",
            "profile": profile_name,
            "source_sfz_sha256": source_sfz_sha256,
            "sample_count": len(sample_paths),
            "sample_bytes": sample_bytes,
            "sample_set_sha256": sample_set_sha256,
            "sample_set_algorithm": (
                "Sort unique VPO-relative UTF-8 paths; for each write "
                "'<lowercase file sha256>  <path>\\n'; SHA-256 the "
                "concatenated UTF-8 bytes."
            ),
            "license_file_sha256": hash_relatives(license_files),
            "version_evidence_sha256": hash_relatives(
                (
                    "Documentation/change-log-Standard-Orchestra.txt",
                    "Documentation/change-log-Wave-Files.txt",
                )
            ),
        }
        if profile_name == "vibraphone":
            document["mapping"] = {
                "damped_runtime_regions": len(region_sets["damped"]),
                "open_runtime_regions": len(region_sets["open"]),
                "unique_recordings": len(sample_paths),
                "recorded_velocity_layers": 1,
                "real_round_robin_count": 0,
                "project_selection": "retain upstream seq_position=1 only",
                "excluded_upstream_mapping": (
                    "seq_position=2 cross-maps neighboring roots and is not "
                    "a same-note alternate recording"
                ),
                "upstream_sfz_unchanged": True,
            }
    destination = (
        Path(output_path).resolve()
        if output_path is not None
        else source_manifest.parent / "资源核验.json"
    )
    destination.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return document


def create_vpo_percussion(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return VpoPercussionInstrument(sample_rate, manifest, base_directory)


__all__ = [
    "PERCUSSION_PROFILES",
    "PercussionArticulation",
    "PercussionProfile",
    "VpoPercussionInstrument",
    "create_vpo_percussion",
    "generate_percussion_pitch_calibration",
    "generate_percussion_resource_verification",
    "percussion_source_regions",
    "vpo_percussion_regions",
]
