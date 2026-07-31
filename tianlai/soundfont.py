from __future__ import annotations

from dataclasses import dataclass
import ctypes
import importlib
import math
import os
from pathlib import Path
from typing import Any
import warnings

import numpy as np

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .tuning import EqualTemperament


_DLL_DIRECTORY_HANDLES: list[Any] = []
_PRELOADED_DLLS: list[Any] = []
_PREPARED_DLL_DIRECTORIES: set[Path] = set()


class SoundFontRuntimeError(ValueError):
    """A SoundFont backend error whose ``__cause__`` retains the native failure."""


class LocalCompatibilitySoundFontWarning(UserWarning):
    """A known legacy bank is being used outside Tianlai's public pipeline."""


_LOCAL_COMPATIBILITY_NOTICES = {
    "generaluser-gs.sf2": (
        "GeneralUser GS is enabled for explicit local compatibility/testing only. "
        "Its upstream documentation acknowledges that the provenance of some "
        "samples could not be established with complete certainty, so Tianlai "
        "does not approve audio rendered from this bank for its public/trusted "
        "release path."
    ),
    "timgm6mb.sf2": (
        "TimGM6mb is enabled for explicit local compatibility/testing only. "
        "Its GPL-2.0 distribution terms contain no explicit rendered-audio output "
        "exception, so Tianlai does not approve audio rendered from this bank for "
        "its public/trusted release path."
    ),
}


def local_compatibility_soundfont_notice(path: str | Path) -> str | None:
    """Return the precise local-only notice for a known legacy SoundFont."""

    return _LOCAL_COMPATIBILITY_NOTICES.get(Path(path).name.casefold())


@dataclass(slots=True)
class _Voice:
    channel: int
    midi_note: int
    release_frames: int | None = None
    one_shot_frames: int | None = None


class SoundFontInstrument(Instrument):
    """Headless SoundFont instrument backed by FluidSynth.

    Notes use separate MIDI channels.  This is intentional: pitch bend is a
    channel message, so independent channels are required for simultaneous
    microtonal notes and for non-440 Hz concert pitch.
    """

    def __init__(self, sample_rate: int, manifest: dict[str, Any], base_directory: str) -> None:
        super().__init__(sample_rate)
        try:
            runtime_directory = prepare_fluidsynth_runtime(base_directory)
            fluidsynth = importlib.import_module("fluidsynth")
        except Exception as exc:  # pragma: no cover - exact exception is platform dependent
            location = (
                f" Project-local runtime: {runtime_directory}."
                if "runtime_directory" in locals() and runtime_directory is not None
                else ""
            )
            raise SoundFontRuntimeError(
                "SoundFont instruments require pyfluidsynth 1.4.0 and the FluidSynth "
                f"native library.{location} Original error: {exc}"
            ) from exc

        self._fluidsynth = fluidsynth
        self.soundfont_candidates = _soundfont_candidates(manifest, base_directory)
        if not self.soundfont_candidates:
            raise ValueError(
                "No explicitly selected SoundFont was found. Set the manifest "
                "'soundfont' field to an .sf2/.sf3 path (including @common/name.sf2), "
                "or set TIANLAI_SOUNDFONT. Tianlai never auto-selects a common bank."
            )

        self.gain = _positive_float(manifest.get("gain", 0.55), "soundfont gain")
        channel_count = int(manifest.get("channel_count", 32))
        if not 1 <= channel_count <= 256:
            raise ValueError("channel_count must be between 1 and 256")

        self.bank = int(manifest.get("bank", 0))
        self.program = int(manifest.get("program", 0))
        self.percussion = bool(manifest.get("percussion", False))
        self.fixed_midi_note = (
            int(manifest["fixed_midi_note"]) if "fixed_midi_note" in manifest else None
        )
        if self.fixed_midi_note is not None and not 0 <= self.fixed_midi_note <= 127:
            raise ValueError("fixed_midi_note must be between 0 and 127")

        self.note_min = _finite_float(manifest.get("note_min", 0.0), "note_min")
        self.note_max = _finite_float(manifest.get("note_max", 127.0), "note_max")
        if self.note_min > self.note_max:
            raise ValueError("note_min must not exceed note_max")

        self.release_frames = max(
            1,
            round(
                _nonnegative_float(manifest.get("release_seconds", 0.45), "release_seconds")
                * sample_rate
            ),
        )
        self.one_shot_frames = max(
            1,
            round(
                _positive_float(manifest.get("one_shot_seconds", 2.0), "one_shot_seconds")
                * sample_rate
            ),
        )
        self.velocity_exponent = _positive_float(
            manifest.get("velocity_exponent", 0.72), "velocity_exponent"
        )
        self.output_trim = _positive_float(manifest.get("output_trim", 1.0), "output_trim")
        self.pitch_bend_range = _positive_float(
            manifest.get("pitch_bend_range_semitones", 2.0),
            "pitch_bend_range_semitones",
        )
        if self.pitch_bend_range > 127.99:
            raise ValueError("pitch_bend_range_semitones must not exceed 127.99")

        self.articulation_programs = _parse_articulation_programs(manifest)
        self.articulation = str(manifest.get("default_articulation", "sustain"))
        if self.articulation_programs and self.articulation not in self.articulation_programs:
            raise ValueError(f"unknown default_articulation: {self.articulation!r}")

        pan = _finite_float(manifest.get("pan", 0.0), "pan")
        if not -1.0 <= pan <= 1.0:
            raise ValueError("pan must be between -1 and 1")
        self._controls: dict[int, int] = {
            1: 0,  # modulation
            2: 127,  # breath
            7: 127,  # volume
            10: int(round((pan + 1.0) * 63.5)),
            11: 127,  # expression
            64: 0,  # sustain pedal
        }
        self.channels = tuple(range(channel_count))
        self._free_channels = list(reversed(self.channels))
        self._voices: dict[int, _Voice] = {}
        self._pedal_held: list[_Voice] = []
        self._releasing: list[_Voice] = []
        self._one_shots: list[_Voice] = []

        synth: Any | None = None
        try:
            synth = fluidsynth.Synth(
                gain=min(10.0, self.gain),
                samplerate=sample_rate,
                # FluidSynth 2.x rejects synth.midi-channels values below 16
                # even when Tianlai intentionally exposes fewer independent
                # pitch-bend channels.  Keep the public pool at channel_count,
                # but create the native synth with its documented minimum so
                # real Windows runs do not emit a misleading settings error.
                channels=max(16, channel_count),
                **{
                    "synth.reverb.active": int(bool(manifest.get("reverb", True))),
                    "synth.chorus.active": int(bool(manifest.get("chorus", False))),
                    "synth.threadsafe-api": 0,
                },
            )
            self.synth = synth
            self.sfid, self.soundfont_path = self._load_first_usable_soundfont()
            local_only_notice = local_compatibility_soundfont_notice(
                self.soundfont_path
            )
            if local_only_notice is not None:
                warnings.warn(
                    local_only_notice,
                    LocalCompatibilitySoundFontWarning,
                    stacklevel=2,
                )
            for channel in self.channels:
                self._configure_channel(channel)
        except Exception:
            if synth is not None:
                try:
                    synth.delete()
                finally:
                    self.synth = None
            raise

    def _load_first_usable_soundfont(self) -> tuple[int, Path]:
        failures: list[tuple[Path, Exception]] = []
        for path in self.soundfont_candidates:
            try:
                sfid = int(self.synth.sfload(str(path)))
                if sfid < 0:
                    raise ValueError(f"sfload returned failure status {sfid}")
            except Exception as exc:
                failures.append((path, exc))
                continue
            return sfid, path

        details = "; ".join(f"{path}: {error}" for path, error in failures)
        grouped_cause = ExceptionGroup(
            "all SoundFont candidates failed",
            [error for _, error in failures],
        )
        failure = SoundFontRuntimeError(
            f"FluidSynth could not load any SoundFont candidate. {details}"
        )
        # ExceptionGroup retains the native exception from the one explicit
        # candidate without flattening it into a generic loader failure.
        raise failure from grouped_cause

    def _current_patch(self) -> tuple[int, int]:
        return self.articulation_programs.get(self.articulation, (self.bank, self.program))

    def _configure_channel(self, channel: int) -> None:
        bank, program = self._current_patch()
        target_bank = 128 if self.percussion else bank
        result = self.synth.program_select(channel, self.sfid, target_bank, program)
        if result not in (None, 0):
            raise ValueError(
                "FluidSynth program_select failed for "
                f"channel={channel}, sfid={self.sfid}, bank={target_bank}, program={program} "
                f"(status={result})"
            )
        self._set_pitch_bend_sensitivity(channel)
        self.synth.pitch_bend(channel, 0)
        for controller, value in self._controls.items():
            self.synth.cc(channel, controller, value)

    def _set_pitch_bend_sensitivity(self, channel: int) -> None:
        # MIDI RPN 0,0 is Pitch Bend Sensitivity.  Explicitly setting it avoids
        # relying on a SoundFont/player default (commonly, but not always, +/-2).
        semitones = int(math.floor(self.pitch_bend_range))
        cents = int(round((self.pitch_bend_range - semitones) * 100.0))
        if cents == 100:
            semitones += 1
            cents = 0
        for controller, value in (
            (101, 0),
            (100, 0),
            (6, semitones),
            (38, cents),
            (101, 127),
            (100, 127),
        ):
            self.synth.cc(channel, controller, value)

    def _allocate_channel(self) -> int:
        if self._free_channels:
            channel = self._free_channels.pop()
            self._configure_channel(channel)
            return channel

        # A pedal-held channel must never be reset or reused: that would cut
        # off its sustained note and apply a new patch/bend to the old sound.
        if self._releasing:
            stolen = self._releasing.pop(0)
            self._silence_channel(stolen.channel)
            self._configure_channel(stolen.channel)
            return stolen.channel
        if self._one_shots:
            stolen = self._one_shots.pop(0)
            self._silence_channel(stolen.channel)
            self._configure_channel(stolen.channel)
            return stolen.channel
        if self._voices:
            oldest_note_id = next(iter(self._voices))
            stolen = self._voices.pop(oldest_note_id)
            self._silence_channel(stolen.channel)
            self._configure_channel(stolen.channel)
            return stolen.channel
        raise ValueError(
            "all SoundFont channels are held by the sustain pedal; release the pedal "
            "or increase channel_count"
        )

    def _silence_channel(self, channel: int) -> None:
        all_sounds_off = getattr(self.synth, "all_sounds_off", None)
        if callable(all_sounds_off):
            all_sounds_off(channel)
        else:  # MIDI CC 120: All Sound Off
            self.synth.cc(channel, 120, 0)
        self.synth.pitch_bend(channel, 0)

    def _release_voice(self, voice: _Voice) -> None:
        self.synth.noteoff(voice.channel, voice.midi_note)
        if self._pedal_is_down:
            voice.release_frames = None
            self._pedal_held.append(voice)
        else:
            voice.release_frames = self.release_frames
            self._releasing.append(voice)

    @property
    def _pedal_is_down(self) -> bool:
        return self._controls[64] >= 64

    def _requested_and_synth_pitch(
        self, event: PerformanceEvent, tuning: EqualTemperament
    ) -> tuple[float, float]:
        if self.fixed_midi_note is not None:
            note = float(self.fixed_midi_note)
            return note, note

        target_hz = event_pitch_hz(event, tuning)
        if "midi_note" in event.payload:
            requested_note = float(event.payload["midi_note"])
        else:
            # Explicit Hz values have no score-space MIDI number.  Use the
            # SoundFont's conventional A4=440 coordinate for range checking.
            requested_note = 69.0 + 12.0 * math.log2(target_hz / 440.0)
        if not self.note_min <= requested_note <= self.note_max:
            raise ValueError(
                f"note {requested_note:.3f} is outside sampled range "
                f"{self.note_min:g}-{self.note_max:g}"
            )

        # SoundFonts are conventionally mapped to MIDI A4=440.  Convert the
        # requested physical frequency, including EqualTemperament.a4_hz, to
        # that coordinate before selecting the key and bend.
        synth_pitch = 69.0 + 12.0 * math.log2(target_hz / 440.0)
        return requested_note, synth_pitch

    def _key_and_bend(self, synth_pitch: float) -> tuple[int, int]:
        midi_note = max(0, min(127, int(math.floor(synth_pitch + 0.5))))
        bend_semitones = synth_pitch - midi_note
        if abs(bend_semitones) > self.pitch_bend_range + 1e-12:
            raise ValueError(
                f"pitch {synth_pitch:.3f} needs a {bend_semitones:+.3f}-semitone bend, "
                f"outside configured +/-{self.pitch_bend_range:g}"
            )
        bend_value = int(round((bend_semitones / self.pitch_bend_range) * 8192.0))
        return midi_note, max(-8192, min(8191, bend_value))

    def handle_event(self, event: PerformanceEvent, tuning: EqualTemperament) -> None:
        if event.type == "articulation":
            name = str(event.payload["name"])
            if self.articulation_programs and name not in self.articulation_programs:
                choices = ", ".join(sorted(self.articulation_programs))
                raise ValueError(f"unsupported articulation {name!r}; choose from {choices}")
            self.articulation = name
            return

        if event.type == "control":
            name = str(event.payload["name"])
            value = float(event.payload["value"])
            mapping = {
                "modulation": 1,
                "breath": 2,
                "volume": 7,
                "pan": 10,
                "expression": 11,
                "sustain_pedal": 64,
            }
            controller = mapping.get(name)
            if controller is None:
                return
            if name == "expression":
                midi_value = int(round((value**1.25) * 127.0))
            else:
                midi_value = int(round(value * 127.0))
            old_pedal_state = self._pedal_is_down
            self._controls[controller] = max(0, min(127, midi_value))
            for channel in self.channels:
                self.synth.cc(channel, controller, self._controls[controller])
            if controller == 64 and old_pedal_state and not self._pedal_is_down:
                for voice in self._pedal_held:
                    voice.release_frames = self.release_frames
                    self._releasing.append(voice)
                self._pedal_held.clear()
            return

        if event.type == "note_on":
            note_id = int(event.payload["note_id"])
            if note_id in self._voices:
                raise ValueError(f"note_id {note_id} is already active")
            _, synth_pitch = self._requested_and_synth_pitch(event, tuning)
            midi_note, bend_value = self._key_and_bend(synth_pitch)
            channel = self._allocate_channel()
            self.synth.pitch_bend(channel, bend_value)
            velocity = float(event.payload["velocity"])
            velocity_midi = max(
                1, min(127, int(round((velocity**self.velocity_exponent) * 127.0)))
            )
            result = self.synth.noteon(channel, midi_note, velocity_midi)
            if result not in (None, 0):
                self._free_channels.append(channel)
                raise ValueError(
                    f"FluidSynth noteon failed for channel={channel}, note={midi_note} "
                    f"(status={result})"
                )
            voice = _Voice(channel=channel, midi_note=midi_note)
            if self.fixed_midi_note is not None:
                voice.one_shot_frames = self.one_shot_frames
                self._one_shots.append(voice)
            else:
                self._voices[note_id] = voice
            return

        if event.type == "note_off":
            note_id = int(event.payload["note_id"])
            voice = self._voices.pop(note_id, None)
            if voice is not None:
                self._release_voice(voice)

    def _advance_lifetimes(self) -> None:
        next_releasing: list[_Voice] = []
        for voice in self._releasing:
            assert voice.release_frames is not None
            voice.release_frames -= 1
            if voice.release_frames <= 0:
                self._silence_channel(voice.channel)
                self._free_channels.append(voice.channel)
            else:
                next_releasing.append(voice)
        self._releasing = next_releasing

        expired_one_shots: list[_Voice] = []
        next_one_shots: list[_Voice] = []
        for voice in self._one_shots:
            assert voice.one_shot_frames is not None
            voice.one_shot_frames -= 1
            if voice.one_shot_frames <= 0:
                expired_one_shots.append(voice)
            else:
                next_one_shots.append(voice)
        self._one_shots = next_one_shots
        for voice in expired_one_shots:
            self._release_voice(voice)

    def render_frame(self) -> StereoFrame:
        self._advance_lifetimes()
        samples = np.asarray(self.synth.get_samples(1), dtype=np.float64)
        if samples.size < 2:
            return 0.0, 0.0
        scale = self.output_trim / 32768.0
        return float(samples[0] * scale), float(samples[1] * scale)

    @property
    def active_voice_count(self) -> int:
        return (
            len(self._voices)
            + len(self._pedal_held)
            + len(self._releasing)
            + len(self._one_shots)
        )

    def close(self) -> None:
        synth = getattr(self, "synth", None)
        if synth is not None:
            self.synth = None
            synth.delete()

    def __del__(self) -> None:  # pragma: no cover - interpreter shutdown
        try:
            self.close()
        except Exception:
            pass

    @classmethod
    def from_manifest(
        cls, manifest: dict[str, Any], sample_rate: int, *, base_directory: str
    ) -> "SoundFontInstrument":
        return cls(sample_rate, manifest, base_directory)


def _finite_float(value: object, field: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _positive_float(value: object, field: str) -> float:
    result = _finite_float(value, field)
    if result <= 0.0:
        raise ValueError(f"{field} must be positive")
    return result


def _nonnegative_float(value: object, field: str) -> float:
    result = _finite_float(value, field)
    if result < 0.0:
        raise ValueError(f"{field} must not be negative")
    return result


def _ancestors(base_directory: str | Path) -> tuple[Path, ...]:
    base = Path(base_directory).expanduser().resolve()
    return (base, *base.parents)


def _find_project_fluidsynth_directory(base_directory: str | Path) -> Path | None:
    candidates: list[Path] = []
    for parent in _ancestors(base_directory):
        runtime = parent / "音源" / "通用" / "fluidsynth"
        candidates.extend((runtime / "bin", runtime / "lib", runtime))

    # An environment override is a fallback.  A checked-in/project-local
    # runtime intentionally wins so unrelated system installations cannot
    # silently change renders.
    override = os.environ.get("TIANLAI_FLUIDSYNTH_DIR")
    if override:
        override_path = Path(override).expanduser().resolve()
        candidates.extend((override_path / "bin", override_path / "lib", override_path))

    for directory in candidates:
        if directory.is_dir() and any(directory.glob("*fluidsynth*.dll")):
            return directory
    return None


def prepare_fluidsynth_runtime(base_directory: str | Path) -> Path | None:
    """Put the project-local FluidSynth DLL ahead of system search paths."""

    if os.name != "nt":
        return None
    directory = _find_project_fluidsynth_directory(base_directory)
    if directory is None:
        return None
    directory = directory.resolve()
    if directory in _PREPARED_DLL_DIRECTORIES:
        return directory

    current_path = os.environ.get("PATH", "")
    path_parts = [part for part in current_path.split(os.pathsep) if part]
    path_parts = [part for part in path_parts if Path(part).resolve() != directory]
    os.environ["PATH"] = os.pathsep.join((str(directory), *path_parts))

    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is not None:
        # Keep this handle alive.  Closing or collecting it removes the DLL
        # search directory on current CPython/Windows.
        _DLL_DIRECTORY_HANDLES.append(add_dll_directory(str(directory)))

    dll_paths = sorted(
        directory.glob("*fluidsynth*.dll"),
        key=lambda path: (
            path.name.lower() != "libfluidsynth-3.dll",
            path.name.lower(),
        ),
    )
    if dll_paths:
        # Load by absolute path before importing pyfluidsynth.  This both gives
        # the local runtime priority and makes native loader errors the direct
        # cause of the user-facing SoundFontRuntimeError.
        _PRELOADED_DLLS.append(ctypes.WinDLL(str(dll_paths[0])))
    _PREPARED_DLL_DIRECTORIES.add(directory)
    return directory


# Compatibility for the first expansion draft and any private callers made
# before the runtime helper became part of the installer-facing API.
_prepare_fluidsynth_runtime = prepare_fluidsynth_runtime


def _parse_articulation_programs(manifest: dict[str, Any]) -> dict[str, tuple[int, int]]:
    raw = manifest.get("articulation_programs", {})
    if not isinstance(raw, dict):
        raise ValueError("articulation_programs must be an object")
    result: dict[str, tuple[int, int]] = {}
    for name, patch in raw.items():
        if isinstance(patch, int):
            result[str(name)] = (int(manifest.get("bank", 0)), patch)
        elif isinstance(patch, dict):
            result[str(name)] = (
                int(patch.get("bank", manifest.get("bank", 0))),
                int(patch["program"]),
            )
        else:
            raise ValueError(f"invalid articulation patch for {name!r}")
    return result


def _soundfont_candidates(
    manifest: dict[str, Any], base_directory: str | Path
) -> tuple[Path, ...]:
    # Both supported selectors are explicit local choices.  An environment
    # override wins completely: if it is missing or broken, failing closed is
    # safer than silently changing the rendered timbre or licence boundary.
    env_path = os.environ.get("TIANLAI_SOUNDFONT")
    if env_path:
        selected = Path(env_path).expanduser().resolve()
        return (
            (selected,)
            if selected.is_file() and selected.suffix.lower() in {".sf2", ".sf3"}
            else ()
        )

    explicit = manifest.get("soundfont")
    if not explicit:
        return ()

    value = str(explicit)
    if value.startswith("@common/"):
        common_name = value.removeprefix("@common/")
        selected = next(
            (
                parent / "音源" / "通用" / common_name
                for parent in _ancestors(base_directory)
                if (parent / "音源" / "通用" / common_name).is_file()
            ),
            None,
        )
        if selected is None:
            return ()
    else:
        selected = (Path(base_directory) / value).expanduser()

    resolved = selected.resolve()
    if not resolved.is_file() or resolved.suffix.lower() not in {".sf2", ".sf3"}:
        return ()
    return (resolved,)


def _resolve_soundfont(manifest: dict[str, Any], base_directory: str | Path) -> Path | None:
    """Return the preferred existing candidate without attempting native loading."""

    return next(iter(_soundfont_candidates(manifest, base_directory)), None)


def create(
    *, manifest: dict[str, Any], sample_rate: int, base_directory: str
) -> Instrument:
    return SoundFontInstrument(sample_rate, manifest, base_directory)
