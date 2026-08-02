from __future__ import annotations

from dataclasses import dataclass
import ctypes
from ctypes import util as ctypes_util
import importlib
import math
import os
from pathlib import Path
import platform
import sys
import threading
import tomllib
from typing import Any
import warnings

import numpy as np

from .events import PerformanceEvent, event_pitch_hz
from .instrument import Instrument, StereoFrame
from .tuning import EqualTemperament


_DLL_DIRECTORY_HANDLES: list[Any] = []
_PRELOADED_DLLS: list[Any] = []
_PREPARED_DLL_DIRECTORIES: set[Path] = set()
_PREPARED_FLUIDSYNTH_LIBRARIES: dict[Path, Path] = {}
_FLUIDSYNTH_IMPORT_LOCK = threading.RLock()


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
            fluidsynth = _import_fluidsynth_backend(runtime_directory)
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


def _is_windows_runtime() -> bool:
    return os.name == "nt"


def _is_macos_runtime() -> bool:
    return sys.platform == "darwin"


def _is_tianlai_runtime_root(directory: Path) -> bool:
    """Recognise a Tianlai root without trusting an arbitrary ancestor.

    Public/runtime trees have the catalogue and trust allow-list.  A source
    checkout may be recognised independently by its package and PEP 621
    project identity, which keeps development and engine-only test layouts
    usable without letting an unrelated ``音源`` directory take precedence.
    """

    if (directory / "乐器").is_dir() and (directory / "可信乐器.json").is_file():
        return True
    pyproject = directory / "pyproject.toml"
    if not pyproject.is_file() or not (directory / "tianlai" / "__init__.py").is_file():
        return False
    try:
        document = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = document.get("project", {})
        name = project.get("name") if isinstance(project, dict) else None
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return False
    return isinstance(name, str) and name.casefold() == "tianlai-audio"


def _find_tianlai_runtime_root(base_directory: str | Path) -> Path | None:
    base = Path(base_directory).expanduser().resolve()
    if base.is_file():
        base = base.parent
    return next(
        (
            candidate
            for candidate in (base, *base.parents)
            if _is_tianlai_runtime_root(candidate)
        ),
        None,
    )


def _macos_homebrew_prefixes(machine: str | None = None) -> tuple[Path, ...]:
    """Return Homebrew prefixes in current-process architecture order."""

    process_machine = (
        (machine or platform.machine()).strip().casefold().replace("-", "_")
    )
    if process_machine in {"x86_64", "amd64", "x64"}:
        conventional = ("/usr/local", "/opt/homebrew")
    else:
        # arm64/aarch64 is the normal non-Intel case.  Keeping the same safe
        # default for an unknown Darwin architecture avoids preferring an
        # Intel-only installation accidentally.
        conventional = ("/opt/homebrew", "/usr/local")

    values = (os.environ.get("HOMEBREW_PREFIX"), *conventional)
    prefixes: list[Path] = []
    seen: set[Path] = set()
    for value in values:
        if not value:
            continue
        prefix = Path(value).expanduser().resolve()
        if prefix in seen:
            continue
        seen.add(prefix)
        prefixes.append(prefix)
    return tuple(prefixes)


def _native_fluidsynth_libraries(directory: Path) -> tuple[Path, ...]:
    if _is_windows_runtime():
        suffix = ".dll"
        preferred_names = ("libfluidsynth-3.dll", "fluidsynth-3.dll")
    elif _is_macos_runtime():
        suffix = ".dylib"
        preferred_names = ("libfluidsynth.dylib", "libfluidsynth.3.dylib")
    else:
        return ()

    preferred_rank = {
        name: index for index, name in enumerate(preferred_names)
    }
    libraries = (
        path
        for path in directory.iterdir()
        if path.is_file()
        and "fluidsynth" in path.name.casefold()
        and path.name.casefold().endswith(suffix)
    )
    return tuple(
        sorted(
            libraries,
            key=lambda path: (
                preferred_rank.get(path.name.casefold(), len(preferred_rank)),
                path.name.casefold(),
            ),
        )
    )


def _find_project_fluidsynth_directory(base_directory: str | Path) -> Path | None:
    if not (_is_windows_runtime() or _is_macos_runtime()):
        return None

    candidates: list[Path] = []
    runtime_root = _find_tianlai_runtime_root(base_directory)
    if runtime_root is not None:
        runtime = runtime_root / "音源" / "通用" / "fluidsynth"
        candidates.extend((runtime / "bin", runtime / "lib", runtime))

    # An environment override is a fallback.  A checked-in/project-local
    # runtime intentionally wins so unrelated system installations cannot
    # silently change renders.
    override = os.environ.get("TIANLAI_FLUIDSYNTH_DIR")
    if override:
        override_path = Path(override).expanduser().resolve()
        candidates.extend((override_path / "bin", override_path / "lib", override_path))

    if _is_macos_runtime():
        # A Homebrew-configured shell exports HOMEBREW_PREFIX.  The two
        # conventional prefixes also cover GUI/MCP launches that do not inherit
        # shell initialisation: /opt/homebrew on Apple Silicon and /usr/local on
        # Intel.  An explicit Tianlai override still wins over both.
        for prefix in _macos_homebrew_prefixes():
            candidates.extend(
                (
                    prefix / "opt" / "fluid-synth" / "lib",
                    prefix / "lib",
                )
            )

    for directory in candidates:
        if directory.is_dir() and _native_fluidsynth_libraries(directory):
            return directory.resolve()
    return None


def prepare_fluidsynth_runtime(base_directory: str | Path) -> Path | None:
    """Prepare the selected FluidSynth DLL or dylib for the Python binding."""

    is_windows = _is_windows_runtime()
    is_macos = _is_macos_runtime()
    if not (is_windows or is_macos):
        return None
    directory = _find_project_fluidsynth_directory(base_directory)
    if directory is None:
        return None
    directory = directory.resolve()
    with _FLUIDSYNTH_IMPORT_LOCK:
        # Native loading and the process-wide Windows search-path update must be
        # performed once even when several render workers start concurrently.
        if directory in _PREPARED_DLL_DIRECTORIES:
            return directory

        libraries = _native_fluidsynth_libraries(directory)
        if not libraries:
            return None
        library = libraries[0].resolve()

        if is_windows:
            current_path = os.environ.get("PATH", "")
            path_parts = [part for part in current_path.split(os.pathsep) if part]
            path_parts = [
                part for part in path_parts if Path(part).resolve() != directory
            ]
            os.environ["PATH"] = os.pathsep.join((str(directory), *path_parts))

            add_dll_directory = getattr(os, "add_dll_directory", None)
            if add_dll_directory is not None:
                # Keep this handle alive. Closing or collecting it removes the
                # DLL search directory on current CPython/Windows.
                _DLL_DIRECTORY_HANDLES.append(add_dll_directory(str(directory)))

            native_library = ctypes.WinDLL(str(library))
        else:
            # Use an absolute path and global visibility so Homebrew and
            # project-local builds can resolve their native dependency graph
            # before pyfluidsynth imports its ctypes declarations.
            native_library = ctypes.CDLL(
                str(library),
                mode=getattr(ctypes, "RTLD_GLOBAL", 0),
            )

        # Keep the native handle alive for every Synth that uses the binding.
        _PRELOADED_DLLS.append(native_library)
        _PREPARED_FLUIDSYNTH_LIBRARIES[directory] = library
        _PREPARED_DLL_DIRECTORIES.add(directory)
        return directory


def _import_fluidsynth_backend(runtime_directory: Path | None) -> Any:
    """Import pyfluidsynth, binding a selected macOS dylib deterministically."""

    if not _is_macos_runtime() or runtime_directory is None:
        return importlib.import_module("fluidsynth")

    with _FLUIDSYNTH_IMPORT_LOCK:
        directory = runtime_directory.resolve()
        library = _PREPARED_FLUIDSYNTH_LIBRARIES.get(directory)
        if library is None:
            return importlib.import_module("fluidsynth")

        existing = sys.modules.get("fluidsynth")
        if existing is not None:
            _require_backend_native_library(existing, library)
            return existing

        # pyfluidsynth 1.4.0 calls ctypes.util.find_library during import.  A
        # dylib loaded by absolute path does not make that lookup discover a
        # private directory, so temporarily answer its FluidSynth probes with
        # the already verified path.  Restore the process-global function
        # before returning, including when the import fails.
        library_names = frozenset(
            {
                "fluidsynth",
                "fluidsynth-3",
                "libfluidsynth",
                "libfluidsynth-3",
                "libfluidsynth-2",
                "libfluidsynth-1",
            }
        )
        original_find_library = ctypes_util.find_library

        def find_library(name: str) -> str | None:
            if str(name).casefold() in library_names:
                return str(library)
            return original_find_library(name)

        ctypes_util.find_library = find_library
        try:
            backend = importlib.import_module("fluidsynth")
            _require_backend_native_library(backend, library)
            return backend
        finally:
            ctypes_util.find_library = original_find_library


def _require_backend_native_library(backend: Any, expected: Path) -> None:
    """Fail closed when an imported macOS binding uses another native dylib."""

    handle = getattr(backend, "_fl", None)
    raw_name = getattr(handle, "_name", None)
    if not isinstance(raw_name, (str, bytes, os.PathLike)):
        raise SoundFontRuntimeError(
            "the imported pyfluidsynth backend does not expose its native "
            "library identity; refusing to bypass the selected macOS runtime"
        )
    try:
        actual = Path(os.fsdecode(raw_name)).expanduser().resolve()
        selected = expected.expanduser().resolve()
    except (OSError, TypeError, ValueError) as exc:
        raise SoundFontRuntimeError(
            "the imported pyfluidsynth backend reported an invalid native "
            f"library path: {raw_name!r}"
        ) from exc
    if os.path.normcase(str(actual)) != os.path.normcase(str(selected)):
        raise SoundFontRuntimeError(
            "pyfluidsynth was already bound to a different native library; "
            f"selected={selected}, imported={actual}. Start a fresh Tianlai "
            "process after changing the FluidSynth runtime."
        )


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
        runtime_root = _find_tianlai_runtime_root(base_directory)
        selected = (
            runtime_root / "音源" / "通用" / common_name
            if runtime_root is not None
            else None
        )
        if selected is None or not selected.is_file():
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
