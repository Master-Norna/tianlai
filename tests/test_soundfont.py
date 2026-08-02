from __future__ import annotations

from contextlib import nullcontext
import importlib.util
import math
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np
import pytest

import tianlai.soundfont as soundfont_module
from tianlai.events import PerformanceEvent
from tianlai.soundfont import (
    LocalCompatibilitySoundFontWarning,
    SoundFontInstrument,
    SoundFontRuntimeError,
    _find_project_fluidsynth_directory,
    _find_tianlai_runtime_root,
    _macos_homebrew_prefixes,
    _resolve_soundfont,
    local_compatibility_soundfont_notice,
    prepare_fluidsynth_runtime,
)
from tianlai.tuning import EqualTemperament


ROOT = Path(__file__).resolve().parents[1]


def _find_installed_soundfont() -> Path | None:
    env_path = os.environ.get("TIANLAI_SOUNDFONT")
    candidates = [Path(env_path).expanduser()] if env_path else []
    candidates.extend(
        ROOT / "音源" / "通用" / name
        for name in ("GeneralUser-GS.sf2", "TimGM6mb.sf2")
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


INSTALLED_SOUNDFONT = _find_installed_soundfont()


class _FakeSynth:
    instances: list["_FakeSynth"] = []
    program_select_status = 0
    sfload_outcomes: dict[str, int | Exception] = {}

    def __init__(self, **settings: object) -> None:
        self.settings = settings
        self.program_calls: list[tuple[int, int, int, int]] = []
        self.cc_calls: list[tuple[int, int, int]] = []
        self.pitch_bends: list[tuple[int, int]] = []
        self.noteons: list[tuple[int, int, int]] = []
        self.noteoffs: list[tuple[int, int]] = []
        self.sfload_calls: list[Path] = []
        self.bends: dict[int, int] = {}
        self.active: dict[int, tuple[int, int]] = {}
        self.deleted = False
        type(self).instances.append(self)

    def sfload(self, path: str) -> int:
        self.loaded_path = path
        soundfont_path = Path(path)
        self.sfload_calls.append(soundfont_path)
        outcome = type(self).sfload_outcomes.get(soundfont_path.name, 7)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def program_select(self, channel: int, sfid: int, bank: int, program: int) -> int:
        self.program_calls.append((channel, sfid, bank, program))
        return type(self).program_select_status

    def cc(self, channel: int, controller: int, value: int) -> int:
        self.cc_calls.append((channel, controller, value))
        return 0

    def pitch_bend(self, channel: int, value: int) -> int:
        self.pitch_bends.append((channel, value))
        self.bends[channel] = value
        return 0

    def noteon(self, channel: int, note: int, velocity: int) -> int:
        self.noteons.append((channel, note, velocity))
        self.active[channel] = (note, velocity)
        return 0

    def noteoff(self, channel: int, note: int) -> int:
        self.noteoffs.append((channel, note))
        self.active.pop(channel, None)
        return 0

    def all_sounds_off(self, channel: int) -> int:
        self.active.pop(channel, None)
        return 0

    def get_samples(self, frame_count: int) -> np.ndarray:
        value = sum(
            note * 37 + velocity * 11 + self.bends.get(channel, 0)
            for channel, (note, velocity) in sorted(self.active.items())
        )
        value = max(-30_000, min(30_000, value))
        return np.tile(np.asarray([value, -value], dtype=np.int16), frame_count)

    def delete(self) -> None:
        self.deleted = True


def _fake_module() -> types.ModuleType:
    module = types.ModuleType("fluidsynth")
    module.Synth = _FakeSynth  # type: ignore[attr-defined]
    return module


def _mark_tianlai_runtime_root(root: Path) -> None:
    (root / "乐器").mkdir(parents=True, exist_ok=True)
    (root / "可信乐器.json").write_text(
        '{"trusted": []}\n',
        encoding="utf-8",
    )


class SoundFontInstrumentTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeSynth.instances.clear()
        _FakeSynth.program_select_status = 0
        _FakeSynth.sfload_outcomes = {}
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.soundfont = Path(self.temporary_directory.name) / "Test.sf2"
        self.soundfont.write_bytes(b"test soundfont placeholder")

    def _create(self, **overrides: object) -> SoundFontInstrument:
        manifest: dict[str, object] = {
            "name": "test",
            "type": "soundfont",
            "soundfont": str(self.soundfont),
            "bank": 0,
            "program": 40,
            "channel_count": 4,
            "release_seconds": 0.002,
            "reverb": False,
            "chorus": False,
        }
        manifest.update(overrides)
        with patch.dict(sys.modules, {"fluidsynth": _fake_module()}):
            instrument = SoundFontInstrument(1_000, manifest, self.temporary_directory.name)
        self.addCleanup(instrument.close)
        return instrument

    @staticmethod
    def _note_on(note_id: int, midi_note: float) -> PerformanceEvent:
        return PerformanceEvent(
            0,
            note_id,
            "note_on",
            {"note_id": note_id, "midi_note": midi_note, "velocity": 0.8},
        )

    @staticmethod
    def _note_off(note_id: int) -> PerformanceEvent:
        return PerformanceEvent(
            0,
            note_id,
            "note_off",
            {"note_id": note_id, "release_velocity": 0.5},
        )

    @staticmethod
    def _pedal(value: float) -> PerformanceEvent:
        return PerformanceEvent(
            0,
            0,
            "control",
            {"name": "sustain_pedal", "value": value},
        )

    def test_midi_note_uses_equal_temperament_a4(self) -> None:
        instrument = self._create(channel_count=1)
        instrument.handle_event(self._note_on(1, 69.0), EqualTemperament(442.0))
        synth = _FakeSynth.instances[-1]
        pitch_delta = 12.0 * math.log2(442.0 / 440.0)
        expected_bend = round((pitch_delta / 2.0) * 8192.0)
        self.assertEqual(synth.noteons[-1][1], 69)
        self.assertEqual(synth.pitch_bends[-1], (0, expected_bend))

    def test_fractional_polyphony_uses_independent_channels_and_explicit_range(self) -> None:
        instrument = self._create(channel_count=2, pitch_bend_range_semitones=2.0)
        tuning = EqualTemperament()
        instrument.handle_event(self._note_on(1, 69.0), tuning)
        instrument.handle_event(self._note_on(2, 69.35), tuning)
        synth = _FakeSynth.instances[-1]
        self.assertEqual(synth.settings["channels"], 16)
        self.assertEqual([call[0] for call in synth.noteons[-2:]], [0, 1])
        self.assertEqual(synth.pitch_bends[-1], (1, round((0.35 / 2.0) * 8192.0)))
        expected_rpn = [(101, 0), (100, 0), (6, 2), (38, 0), (101, 127), (100, 127)]
        channel_zero_rpn = [
            (controller, value)
            for channel, controller, value in synth.cc_calls
            if channel == 0 and controller in {101, 100, 6, 38}
        ]
        self.assertEqual(channel_zero_rpn[-6:], expected_rpn)

    def test_pedal_held_voice_is_not_released_or_reused(self) -> None:
        instrument = self._create(channel_count=1, release_seconds=0.002)
        tuning = EqualTemperament()
        instrument.handle_event(self._note_on(1, 60.0), tuning)
        instrument.handle_event(self._pedal(1.0), tuning)
        instrument.handle_event(self._note_off(1), tuning)
        for _ in range(10):
            instrument.render_frame()
        self.assertEqual(instrument.active_voice_count, 1)
        with self.assertRaisesRegex(ValueError, "held by the sustain pedal"):
            instrument.handle_event(self._note_on(2, 64.0), tuning)

        instrument.handle_event(self._pedal(0.0), tuning)
        instrument.render_frame()
        self.assertEqual(instrument.active_voice_count, 1)
        instrument.render_frame()
        self.assertEqual(instrument.active_voice_count, 0)
        instrument.handle_event(self._note_on(3, 67.0), tuning)
        self.assertEqual(_FakeSynth.instances[-1].noteons[-1][0], 0)

    def test_program_select_failure_is_reported(self) -> None:
        _FakeSynth.program_select_status = -1
        with patch.dict(sys.modules, {"fluidsynth": _fake_module()}):
            with self.assertRaisesRegex(ValueError, "program_select failed"):
                SoundFontInstrument(
                    1_000,
                    {
                        "name": "broken patch",
                        "type": "soundfont",
                        "soundfont": str(self.soundfont),
                        "program": 127,
                        "channel_count": 1,
                    },
                    self.temporary_directory.name,
                )
        self.assertTrue(_FakeSynth.instances[-1].deleted)

    def test_backend_import_error_retains_root_cause(self) -> None:
        cause = OSError("native FluidSynth DLL could not be loaded")
        with patch("tianlai.soundfont.importlib.import_module", side_effect=cause):
            with self.assertRaises(SoundFontRuntimeError) as raised:
                SoundFontInstrument(
                    1_000,
                    {
                        "name": "missing runtime",
                        "type": "soundfont",
                        "soundfont": str(self.soundfont),
                        "program": 0,
                    },
                    self.temporary_directory.name,
                )
        self.assertIs(raised.exception.__cause__, cause)
        self.assertIn("native FluidSynth DLL", str(raised.exception))

    def test_fake_backend_render_is_deterministic(self) -> None:
        tuning = EqualTemperament(441.0)
        outputs: list[list[tuple[float, float]]] = []
        for _ in range(2):
            instrument = self._create(channel_count=2, reverb=False, chorus=False)
            instrument.handle_event(self._note_on(1, 60.25), tuning)
            instrument.handle_event(self._note_on(2, 67.0), tuning)
            outputs.append([instrument.render_frame() for _ in range(32)])
            instrument.close()
        self.assertEqual(outputs[0], outputs[1])

    def test_soundfont_selection_is_explicit_and_environment_override_wins(self) -> None:
        project = Path(self.temporary_directory.name) / "project"
        _mark_tianlai_runtime_root(project)
        manifest_directory = project / "乐器" / "管弦乐" / "弦乐组" / "中提琴"
        common = project / "音源" / "通用"
        manifest_directory.mkdir(parents=True)
        common.mkdir(parents=True)
        compact = common / "TimGM6mb.sf2"
        compact.write_bytes(b"compact")
        self.assertIsNone(
            _resolve_soundfont(
                {"soundfont": "@common/GeneralUser-GS.sf2"}, manifest_directory
            )
        )
        self.assertEqual(
            _resolve_soundfont(
                {"soundfont": "@common/TimGM6mb.sf2"}, manifest_directory
            ),
            compact.resolve(),
        )
        self.assertIsNone(_resolve_soundfont({}, manifest_directory))

        external = Path(self.temporary_directory.name) / "External.sf3"
        external.write_bytes(b"external")
        with patch.dict(os.environ, {"TIANLAI_SOUNDFONT": str(external)}):
            self.assertEqual(_resolve_soundfont({}, manifest_directory), external.resolve())

    def test_broken_explicit_bank_never_falls_back_to_another_common_bank(self) -> None:
        project = Path(self.temporary_directory.name) / "fallback-project"
        _mark_tianlai_runtime_root(project)
        manifest_directory = project / "乐器" / "管弦乐" / "弦乐组" / "中提琴"
        common = project / "音源" / "通用"
        manifest_directory.mkdir(parents=True)
        common.mkdir(parents=True)
        general_user = common / "GeneralUser-GS.sf2"
        tim_gm = common / "TimGM6mb.sf2"
        general_user.write_bytes(b"broken")
        tim_gm.write_bytes(b"usable")
        _FakeSynth.sfload_outcomes = {
            general_user.name: OSError("corrupt SoundFont table"),
            tim_gm.name: 11,
        }
        with patch.dict(sys.modules, {"fluidsynth": _fake_module()}):
            with self.assertRaises(SoundFontRuntimeError):
                SoundFontInstrument(
                    1_000,
                    {
                        "name": "explicit local compatibility",
                        "type": "soundfont",
                        "soundfont": "@common/GeneralUser-GS.sf2",
                        "program": 40,
                        "channel_count": 1,
                    },
                    str(manifest_directory),
                )
        synth = _FakeSynth.instances[-1]
        self.assertEqual(synth.sfload_calls, [general_user.resolve()])
        self.assertTrue(synth.deleted)

    def test_explicit_soundfont_load_failure_retains_native_cause(self) -> None:
        project = Path(self.temporary_directory.name) / "failed-project"
        _mark_tianlai_runtime_root(project)
        manifest_directory = project / "乐器" / "世界乐器" / "卡林巴"
        common = project / "音源" / "通用"
        manifest_directory.mkdir(parents=True)
        common.mkdir(parents=True)
        general_user = common / "GeneralUser-GS.sf2"
        tim_gm = common / "TimGM6mb.sf2"
        general_user.write_bytes(b"broken one")
        tim_gm.write_bytes(b"broken two")
        _FakeSynth.sfload_outcomes = {
            general_user.name: OSError("bad GeneralUser RIFF"),
            tim_gm.name: -1,
        }
        with patch.dict(sys.modules, {"fluidsynth": _fake_module()}):
            with self.assertRaises(SoundFontRuntimeError) as raised:
                SoundFontInstrument(
                    1_000,
                    {
                        "name": "all failed",
                        "type": "soundfont",
                        "soundfont": "@common/GeneralUser-GS.sf2",
                        "program": 0,
                        "channel_count": 1,
                    },
                    str(manifest_directory),
                )
        message = str(raised.exception)
        self.assertIn("GeneralUser-GS.sf2", message)
        self.assertIn("bad GeneralUser RIFF", message)
        self.assertNotIn("TimGM6mb.sf2", message)
        self.assertIsInstance(raised.exception.__cause__, ExceptionGroup)
        self.assertEqual(len(raised.exception.__cause__.exceptions), 1)

    def test_known_legacy_bank_emits_precise_local_only_warning(self) -> None:
        self.soundfont = (
            Path(self.temporary_directory.name) / "GeneralUser-GS.sf2"
        )
        self.soundfont.write_bytes(b"local compatibility")
        with self.assertWarnsRegex(
            LocalCompatibilitySoundFontWarning,
            "explicit local compatibility/testing only",
        ):
            instrument = self._create()
        self.assertEqual(instrument.soundfont_path, self.soundfont.resolve())

    def test_known_legacy_banks_have_distinct_licence_notices(self) -> None:
        general_user = local_compatibility_soundfont_notice(
            "GeneralUser-GS.sf2"
        )
        timgm = local_compatibility_soundfont_notice("TimGM6mb.sf2")

        assert general_user is not None
        assert timgm is not None
        self.assertIn("provenance", general_user)
        self.assertIn("GPL-2.0", timgm)
        self.assertIn("no explicit rendered-audio output exception", timgm)

    def test_project_local_dll_directory_wins_over_environment_override(self) -> None:
        project = Path(self.temporary_directory.name) / "project"
        _mark_tianlai_runtime_root(project)
        manifest_directory = project / "乐器" / "键盘乐器" / "电钢琴"
        local_bin = project / "音源" / "通用" / "fluidsynth" / "bin"
        override = Path(self.temporary_directory.name) / "external-runtime"
        manifest_directory.mkdir(parents=True)
        local_bin.mkdir(parents=True)
        override.mkdir()
        (local_bin / "libfluidsynth-3.dll").write_bytes(b"local placeholder")
        (override / "libfluidsynth-3.dll").write_bytes(b"external placeholder")
        with (
            patch("tianlai.soundfont._is_windows_runtime", return_value=True),
            patch("tianlai.soundfont._is_macos_runtime", return_value=False),
            patch.dict(os.environ, {"TIANLAI_FLUIDSYNTH_DIR": str(override)}),
        ):
            self.assertEqual(
                _find_project_fluidsynth_directory(manifest_directory),
                local_bin.resolve(),
            )

    def test_project_local_macos_dylib_wins_over_environment_override(self) -> None:
        project = Path(self.temporary_directory.name) / "mac project"
        _mark_tianlai_runtime_root(project)
        manifest_directory = project / "乐器" / "键盘乐器" / "电钢琴"
        local_lib = project / "音源" / "通用" / "fluidsynth" / "lib"
        override = Path(self.temporary_directory.name) / "external mac runtime"
        manifest_directory.mkdir(parents=True)
        local_lib.mkdir(parents=True)
        override.mkdir()
        (local_lib / "libfluidsynth.3.dylib").write_bytes(b"local placeholder")
        (override / "libfluidsynth.dylib").write_bytes(b"external placeholder")

        with (
            patch("tianlai.soundfont._is_windows_runtime", return_value=False),
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch.dict(os.environ, {"TIANLAI_FLUIDSYNTH_DIR": str(override)}),
        ):
            self.assertEqual(
                _find_project_fluidsynth_directory(manifest_directory),
                local_lib.resolve(),
            )

    def test_unidentified_ancestor_cannot_supply_runtime_or_common_bank(self) -> None:
        ancestor = Path(self.temporary_directory.name) / "unrelated workspace"
        manifest_directory = ancestor / "nested" / "instrument"
        local_runtime = ancestor / "音源" / "通用" / "fluidsynth" / "lib"
        common = ancestor / "音源" / "通用"
        override = Path(self.temporary_directory.name) / "explicit runtime"
        manifest_directory.mkdir(parents=True)
        local_runtime.mkdir(parents=True)
        override.mkdir()
        (local_runtime / "libfluidsynth.3.dylib").write_bytes(b"untrusted")
        (common / "Ancestor.sf2").write_bytes(b"untrusted")
        (override / "libfluidsynth.dylib").write_bytes(b"explicit")

        with (
            patch("tianlai.soundfont._is_windows_runtime", return_value=False),
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch.dict(
                os.environ,
                {
                    "TIANLAI_FLUIDSYNTH_DIR": str(override),
                    "HOMEBREW_PREFIX": "",
                },
            ),
        ):
            self.assertIsNone(_find_tianlai_runtime_root(manifest_directory))
            self.assertEqual(
                _find_project_fluidsynth_directory(manifest_directory),
                override.resolve(),
            )
            self.assertIsNone(
                _resolve_soundfont(
                    {"soundfont": "@common/Ancestor.sf2"},
                    manifest_directory,
                )
            )

    def test_tianlai_pyproject_is_a_source_runtime_boundary(self) -> None:
        project = Path(self.temporary_directory.name) / "source checkout"
        manifest_directory = project / "scratch" / "instrument"
        runtime = project / "音源" / "通用" / "fluidsynth" / "lib"
        (project / "tianlai").mkdir(parents=True)
        (project / "tianlai" / "__init__.py").write_text("", encoding="utf-8")
        (project / "pyproject.toml").write_text(
            '[project]\nname = "tianlai-audio"\n',
            encoding="utf-8",
        )
        manifest_directory.mkdir(parents=True)
        runtime.mkdir(parents=True)
        (runtime / "libfluidsynth.dylib").write_bytes(b"source runtime")

        with (
            patch("tianlai.soundfont._is_windows_runtime", return_value=False),
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch.dict(
                os.environ,
                {"TIANLAI_FLUIDSYNTH_DIR": "", "HOMEBREW_PREFIX": ""},
            ),
        ):
            self.assertEqual(
                _find_tianlai_runtime_root(manifest_directory),
                project.resolve(),
            )
            self.assertEqual(
                _find_project_fluidsynth_directory(manifest_directory),
                runtime.resolve(),
            )

    def test_macos_homebrew_prefix_order_follows_process_architecture(self) -> None:
        with patch.dict(os.environ, {"HOMEBREW_PREFIX": ""}):
            arm = _macos_homebrew_prefixes("arm64")
            intel = _macos_homebrew_prefixes("x86_64")

        self.assertEqual(
            arm[:2],
            (Path("/opt/homebrew").resolve(), Path("/usr/local").resolve()),
        )
        self.assertEqual(
            intel[:2],
            (Path("/usr/local").resolve(), Path("/opt/homebrew").resolve()),
        )

    def test_explicit_homebrew_prefix_precedes_architecture_defaults(self) -> None:
        explicit = Path(self.temporary_directory.name) / "custom brew"
        with patch.dict(os.environ, {"HOMEBREW_PREFIX": str(explicit)}):
            prefixes = _macos_homebrew_prefixes("x86_64")

        self.assertEqual(prefixes[0], explicit.resolve())
        self.assertEqual(
            prefixes[1:3],
            (Path("/usr/local").resolve(), Path("/opt/homebrew").resolve()),
        )

    def test_macos_homebrew_prefix_is_discovered_without_manual_override(
        self,
    ) -> None:
        project = Path(self.temporary_directory.name) / "mac project"
        manifest_directory = project / "乐器" / "键盘乐器" / "电钢琴"
        homebrew = Path(self.temporary_directory.name) / "homebrew prefix"
        formula_lib = homebrew / "opt" / "fluid-synth" / "lib"
        manifest_directory.mkdir(parents=True)
        formula_lib.mkdir(parents=True)
        (formula_lib / "libfluidsynth.3.dylib").write_bytes(b"placeholder")

        with (
            patch("tianlai.soundfont._is_windows_runtime", return_value=False),
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch.dict(
                os.environ,
                {
                    "HOMEBREW_PREFIX": str(homebrew),
                    "TIANLAI_FLUIDSYNTH_DIR": "",
                },
            ),
        ):
            self.assertEqual(
                _find_project_fluidsynth_directory(manifest_directory),
                formula_lib.resolve(),
            )

    def test_macos_runtime_preloads_preferred_dylib_once_without_changing_path(
        self,
    ) -> None:
        project = Path(self.temporary_directory.name) / "mac preload"
        _mark_tianlai_runtime_root(project)
        manifest_directory = project / "乐器"
        local_lib = project / "音源" / "通用" / "fluidsynth" / "lib"
        manifest_directory.mkdir(parents=True, exist_ok=True)
        local_lib.mkdir(parents=True)
        preferred = local_lib / "libfluidsynth.dylib"
        preferred.write_bytes(b"unversioned placeholder")
        (local_lib / "libfluidsynth.3.dylib").write_bytes(b"versioned placeholder")
        native_handle = object()

        with (
            patch("tianlai.soundfont._is_windows_runtime", return_value=False),
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch("tianlai.soundfont.ctypes.CDLL", return_value=native_handle) as load,
            patch("tianlai.soundfont._PRELOADED_DLLS", new=[]) as handles,
            patch("tianlai.soundfont._PREPARED_DLL_DIRECTORIES", new=set()),
            patch("tianlai.soundfont._PREPARED_FLUIDSYNTH_LIBRARIES", new={}),
            patch.dict(os.environ, {"PATH": "unchanged"}),
        ):
            first = prepare_fluidsynth_runtime(manifest_directory)
            second = prepare_fluidsynth_runtime(manifest_directory)
            self.assertEqual(os.environ["PATH"], "unchanged")

        self.assertEqual(first, local_lib.resolve())
        self.assertEqual(second, first)
        load.assert_called_once_with(
            str(preferred.resolve()),
            mode=getattr(soundfont_module.ctypes, "RTLD_GLOBAL", 0),
        )
        self.assertEqual(handles, [native_handle])

    def test_macos_backend_import_uses_selected_dylib_then_restores_lookup(
        self,
    ) -> None:
        runtime = Path(self.temporary_directory.name) / "mac import" / "lib"
        runtime.mkdir(parents=True)
        library = runtime / "libfluidsynth.dylib"
        library.write_bytes(b"placeholder")
        backend = _fake_module()
        backend._fl = types.SimpleNamespace(_name=str(library.resolve()))

        def import_backend(name: str) -> types.ModuleType:
            self.assertEqual(name, "fluidsynth")
            self.assertEqual(
                soundfont_module.ctypes_util.find_library("fluidsynth"),
                str(library),
            )
            self.assertEqual(
                soundfont_module.ctypes_util.find_library("unrelated"),
                "system:unrelated",
            )
            return backend

        with (
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch.dict(sys.modules) as modules,
            patch(
                "tianlai.soundfont._PREPARED_FLUIDSYNTH_LIBRARIES",
                new={runtime.resolve(): library.resolve()},
            ),
            patch(
                "tianlai.soundfont.ctypes_util.find_library",
                side_effect=lambda name: f"system:{name}",
            ) as system_lookup,
            patch(
                "tianlai.soundfont.importlib.import_module",
                side_effect=import_backend,
            ) as importer,
        ):
            modules.pop("fluidsynth", None)
            selected = soundfont_module._import_fluidsynth_backend(runtime)
            self.assertIs(
                soundfont_module.ctypes_util.find_library,
                system_lookup,
            )

        self.assertIs(selected, backend)
        importer.assert_called_once_with("fluidsynth")

    def test_preimported_macos_backend_must_match_selected_dylib(self) -> None:
        runtime = Path(self.temporary_directory.name) / "selected" / "lib"
        runtime.mkdir(parents=True)
        selected = runtime / "libfluidsynth.dylib"
        selected.write_bytes(b"selected")
        other = Path(self.temporary_directory.name) / "other" / "libfluidsynth.dylib"
        other.parent.mkdir()
        other.write_bytes(b"other")
        backend = _fake_module()
        backend._fl = types.SimpleNamespace(_name=str(other.resolve()))

        with (
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch.dict(sys.modules, {"fluidsynth": backend}),
            patch(
                "tianlai.soundfont._PREPARED_FLUIDSYNTH_LIBRARIES",
                new={runtime.resolve(): selected.resolve()},
            ),
            self.assertRaisesRegex(
                SoundFontRuntimeError,
                "already bound to a different native library",
            ),
        ):
            soundfont_module._import_fluidsynth_backend(runtime)

        backend._fl = types.SimpleNamespace(_name=str(selected.resolve()))
        with (
            patch("tianlai.soundfont._is_macos_runtime", return_value=True),
            patch.dict(sys.modules, {"fluidsynth": backend}),
            patch(
                "tianlai.soundfont._PREPARED_FLUIDSYNTH_LIBRARIES",
                new={runtime.resolve(): selected.resolve()},
            ),
        ):
            self.assertIs(
                soundfont_module._import_fluidsynth_backend(runtime),
                backend,
            )

    @unittest.skipUnless(
        INSTALLED_SOUNDFONT is not None,
        "set TIANLAI_SOUNDFONT or install GeneralUser-GS/TimGM in project 音源/通用",
    )
    @pytest.mark.external_assets
    def test_installed_soundfont_is_not_implicitly_discovered(self) -> None:
        self.assertIsNone(_resolve_soundfont({}, ROOT / "乐器"))
        assert INSTALLED_SOUNDFONT is not None
        self.assertEqual(
            _resolve_soundfont(
                {"soundfont": str(INSTALLED_SOUNDFONT)},
                ROOT / "乐器",
            ),
            INSTALLED_SOUNDFONT,
        )
        # Merely locating the Python binding must not be part of path discovery;
        # native loading is covered separately by integration/installer checks.
        importlib.util.find_spec("fluidsynth")

    @unittest.skipUnless(
        INSTALLED_SOUNDFONT is not None
        and importlib.util.find_spec("fluidsynth") is not None,
        "project/env SoundFont and pyfluidsynth are required for integration rendering",
    )
    @pytest.mark.external_assets
    @pytest.mark.listening
    def test_installed_backend_render_is_deterministic(self) -> None:
        assert INSTALLED_SOUNDFONT is not None
        outputs: list[np.ndarray] = []
        local_notice = local_compatibility_soundfont_notice(INSTALLED_SOUNDFONT)
        warning_context = (
            pytest.warns(
                LocalCompatibilitySoundFontWarning,
                match="explicit local compatibility/testing only",
            )
            if local_notice is not None
            else nullcontext()
        )
        with warning_context as caught:
            for _ in range(2):
                instrument = SoundFontInstrument(
                    24_000,
                    {
                        "name": "determinism integration",
                        "type": "soundfont",
                        "soundfont": str(INSTALLED_SOUNDFONT),
                        "program": 40,
                        "channel_count": 1,
                        "reverb": False,
                        "chorus": False,
                    },
                    str(ROOT),
                )
                try:
                    instrument.handle_event(self._note_on(1, 69.0), EqualTemperament())
                    outputs.append(
                        np.asarray([instrument.render_frame() for _ in range(2_048)])
                    )
                finally:
                    instrument.close()
        if local_notice is not None:
            assert caught is not None
            self.assertEqual([str(item.message) for item in caught], [local_notice] * 2)
        self.assertTrue(np.array_equal(outputs[0], outputs[1]))
        self.assertGreater(float(np.max(np.abs(outputs[0]))), 1e-6)

    @unittest.skipUnless(
        INSTALLED_SOUNDFONT is not None
        and importlib.util.find_spec("fluidsynth") is not None,
        "project/env SoundFont and pyfluidsynth are required for integration tuning",
    )
    @pytest.mark.external_assets
    @pytest.mark.listening
    def test_installed_backend_applies_non_440_a4_to_real_audio(self) -> None:
        """Verify the native renderer, not only the MIDI-call mock, shifts A4."""

        assert INSTALLED_SOUNDFONT is not None
        sample_rate = 24_000

        def measured_frequency(a4_hz: float) -> float:
            instrument = SoundFontInstrument(
                sample_rate,
                {
                    "name": "concert-pitch integration",
                    "type": "soundfont",
                    "soundfont": str(INSTALLED_SOUNDFONT),
                    # The whistle patch has a stable, nearly monophonic
                    # sustained spectrum and is safer for this relative-
                    # frequency check than a decaying or chorused patch.
                    "program": 78,
                    "channel_count": 1,
                    "reverb": False,
                    "chorus": False,
                },
                str(ROOT),
            )
            try:
                instrument.handle_event(self._note_on(1, 69.0), EqualTemperament(a4_hz))
                mono = np.asarray(
                    [
                        sum(instrument.render_frame()) * 0.5
                        for _ in range(sample_rate * 3)
                    ],
                    dtype=np.float64,
                )
            finally:
                instrument.close()
            segment = mono[2_400:]
            segment -= np.mean(segment)
            windowed = segment * np.hanning(len(segment))
            spectrum = np.abs(np.fft.rfft(windowed))
            frequencies = np.fft.rfftfreq(len(segment), 1.0 / sample_rate)
            candidates = np.flatnonzero((frequencies >= 420.0) & (frequencies <= 465.0))
            peak = int(candidates[np.argmax(spectrum[candidates])])
            left, center, right = np.log(spectrum[peak - 1 : peak + 2] + 1e-20)
            denominator = left - 2.0 * center + right
            offset = 0.0 if denominator == 0.0 else 0.5 * (left - right) / denominator
            return float((peak + offset) * sample_rate / len(segment))

        local_notice = local_compatibility_soundfont_notice(INSTALLED_SOUNDFONT)
        warning_context = (
            pytest.warns(
                LocalCompatibilitySoundFontWarning,
                match="explicit local compatibility/testing only",
            )
            if local_notice is not None
            else nullcontext()
        )
        with warning_context as caught:
            measured_440 = measured_frequency(440.0)
            measured_442 = measured_frequency(442.0)
        if local_notice is not None:
            assert caught is not None
            self.assertEqual([str(item.message) for item in caught], [local_notice] * 2)
        self.assertAlmostEqual(
            measured_442 / measured_440,
            442.0 / 440.0,
            delta=0.00035,
            msg=f"measured_440={measured_440:.6f}, measured_442={measured_442:.6f}",
        )


if __name__ == "__main__":
    unittest.main()
