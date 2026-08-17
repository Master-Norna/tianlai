import hashlib
import json
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import tianlai.renderer as renderer_module
from tianlai.instrument import Instrument
from tianlai.post_render_check import POST_RENDER_CHECK_NAME
from tianlai.renderer import render_to_wav, render_to_wav_atomic
from tianlai.resource_limits import ResourceLimitError


ROOT = Path(__file__).resolve().parents[1]


class _ConstantInstrument(Instrument):
    def __init__(
        self,
        frame: tuple[float, float],
        sample_rate: int = 48_000,
    ) -> None:
        super().__init__(sample_rate)
        self.frame = frame

    def handle_event(self, event, tuning) -> None:
        return None

    def render_frame(self) -> tuple[float, float]:
        return self.frame

    @property
    def active_voice_count(self) -> int:
        return 0


class _OrderedFailureInstrument(Instrument):
    """Custom frame stream whose first semantic error must remain observable."""

    def __init__(self, sample_rate: int = 8_000) -> None:
        super().__init__(sample_rate)
        self._frame_index = 0

    def handle_event(self, event, tuning) -> None:
        del event, tuning

    def render_frame(self):
        frames = ((1.1, 0.0), ("bad", 0.0))
        if self._frame_index < len(frames):
            frame = frames[self._frame_index]
        else:
            frame = (0.0, 0.0)
        self._frame_index += 1
        return frame

    @property
    def active_voice_count(self) -> int:
        return 0


def _render_quartet(target: Path) -> tuple[Path, Path, Path, Path]:
    return (
        target,
        target.with_name(f"{target.name}.许可与署名.json"),
        target.with_name(f"{target.name}.许可与署名.txt"),
        Path(f"{target}.{POST_RENDER_CHECK_NAME}"),
    )


def _seed_old_quartet(target: Path) -> dict[Path, bytes]:
    audio_path, json_path, text_path, check_path = _render_quartet(target)
    previous = {
        audio_path: b"previous verified render",
        json_path: b'{"state":"previous verified sidecar"}\n',
        text_path: "此前已核验的署名\n".encode("utf-8"),
        check_path: b'{"state":"previous verified post-render check"}\n',
    }
    for path, payload in previous.items():
        path.write_bytes(payload)
    return previous


def _write_tiny_performance(directory: Path) -> Path:
    path = directory / "tiny.events.json"
    path.write_text(
        json.dumps(
            {
                "sample_rate": 8_000,
                "channels": 2,
                "tail_seconds": 0.0,
                "duration_seconds": 0.001,
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_tiny_active_performance(directory: Path) -> Path:
    path = directory / "tiny-active.events.json"
    path.write_text(
        json.dumps(
            {
                "sample_rate": 8_000,
                "channels": 2,
                "tail_seconds": 0.0,
                "duration_seconds": 0.001,
                "events": [
                    {
                        "time": 0.0,
                        "type": "note_on",
                        "note_id": 1,
                        "midi_note": 69,
                        "velocity": 0.5,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _assert_only_expected_private_files(
    test: unittest.TestCase,
    directory: Path,
    *,
    allow_rollbacks: bool = False,
) -> None:
    hidden = [path for path in directory.iterdir() if path.name.startswith(".")]
    test.assertTrue(hidden)
    for path in hidden:
        test.assertTrue(path.is_file())
        is_lock = (
            path.name.startswith(".tianlai-render-")
            and path.name.endswith(".lock")
        )
        is_rollback = ".rollback-preserved-" in path.name
        test.assertTrue(is_lock or (allow_rollbacks and is_rollback), path.name)


class RendererTests(unittest.TestCase):
    def test_custom_frame_stream_preserves_first_render_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "ordered-error.wav"
            performance = _write_tiny_performance(directory)

            with patch(
                "tianlai.renderer.create_instrument",
                return_value=_OrderedFailureInstrument(),
            ):
                with self.assertRaisesRegex(ValueError, "第 0 帧"):
                    render_to_wav(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        performance,
                        target,
                    )

            self.assertFalse(target.exists())
            self.assertEqual(
                list(directory.glob(f".{target.name}.*.tianlai-part")),
                [],
            )

    def test_atomic_render_requires_a_wav_output_name(self) -> None:
        with self.assertRaisesRegex(ValueError, r"\.wav"):
            render_to_wav_atomic(
                "instrument.json",
                "performance.json",
                "render.许可与署名.json",
            )

    def test_atomic_render_holds_the_output_file_lock(self) -> None:
        expected = renderer_module.RenderResult(
            sample_rate=8_000,
            frame_count=8,
            duration_seconds=0.001,
            peak_active_voices=0,
        )
        inputs = SimpleNamespace(
            manifest=SimpleNamespace(
                path=Path.cwd() / "instrument.json",
            ),
            performance=SimpleNamespace(
                path=Path.cwd() / "performance.json",
            ),
        )
        with (
            patch("tianlai.renderer.acquire_render_lock") as acquire,
            patch(
                "tianlai.renderer._capture_single_render_inputs",
                return_value=inputs,
            ),
            patch(
                "tianlai.renderer._render_to_wav_atomic_locked",
                return_value=expected,
            ) as render_locked,
        ):
            observed = render_to_wav_atomic(
                "instrument.json",
                "performance.json",
                "render.wav",
            )

        self.assertIs(observed, expected)
        acquire.assert_called_once_with(
            Path.cwd() / "render.wav",
            existing_target_kind="file",
        )
        render_locked.assert_called_once_with(
            Path.cwd() / "instrument.json",
            Path.cwd() / "performance.json",
            Path.cwd() / "render.wav",
            _inputs=inputs,
        )

    def test_duration_budget_fails_before_output_lock_or_staging(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            performance = directory / "too-long.events.json"
            performance.write_text(
                json.dumps(
                    {
                        "sample_rate": 8_000,
                        "channels": 2,
                        "tail_seconds": 0.0,
                        "duration_seconds": 7_201.0,
                        "events": [],
                    }
                ),
                encoding="utf-8",
            )
            target = directory / "not-created" / "render.wav"
            manifest = (
                ROOT
                / "乐器"
                / "测试工具"
                / "参考振荡器"
                / "乐器.json"
            )

            with patch("tianlai.renderer.acquire_render_lock") as acquire:
                with self.assertRaises(ResourceLimitError) as raised:
                    render_to_wav_atomic(manifest, performance, target)

            self.assertEqual(raised.exception.code, "render.duration_too_long")
            acquire.assert_not_called()
            self.assertFalse(target.parent.exists())

    def test_event_budget_fails_before_output_lock_or_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            performance = directory / "too-many.events.json"
            performance.write_text(
                json.dumps(
                    {
                        "sample_rate": 8_000,
                        "channels": 2,
                        "tail_seconds": 0.0,
                        "duration_seconds": 0.001,
                        "events": [
                            {
                                "time": 0.0,
                                "type": "control",
                                "name": f"control-{index}",
                                "value": 0.5,
                            }
                            for index in range(5)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            target = directory / "not-created" / "render.wav"
            manifest = (
                ROOT
                / "乐器"
                / "测试工具"
                / "参考振荡器"
                / "乐器.json"
            )

            with (
                patch.dict(os.environ, {"TIANLAI_MAX_NOTES": "1"}),
                patch("tianlai.renderer.acquire_render_lock") as acquire,
                patch(
                    "tianlai.renderer.parse_performance_document"
                ) as parse_performance,
            ):
                with self.assertRaises(ResourceLimitError) as raised:
                    render_to_wav_atomic(manifest, performance, target)

            self.assertEqual(
                raised.exception.code,
                "performance.too_many_events",
            )
            acquire.assert_not_called()
            parse_performance.assert_not_called()
            self.assertFalse(target.parent.exists())

    def test_input_byte_limit_fails_before_output_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "instrument.json"
            manifest.write_text('{"name":"bounded"}', encoding="utf-8")
            performance = directory / "oversized.events.json"
            performance.write_text(
                '{"events":[],"padding":"' + ("x" * 128) + '"}',
                encoding="utf-8",
            )
            target = directory / "not-created" / "render.wav"

            with (
                patch(
                    "tianlai.renderer.ProjectLimits.from_environment",
                    return_value=renderer_module.ProjectLimits(
                        max_score_json_bytes=64,
                    ),
                ),
                patch("tianlai.renderer.acquire_render_lock") as acquire,
            ):
                with self.assertRaisesRegex(ValueError, "演奏事件文档"):
                    render_to_wav_atomic(manifest, performance, target)

            acquire.assert_not_called()
            self.assertFalse(target.parent.exists())

    def test_duplicate_manifest_member_fails_before_output_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "instrument.json"
            manifest.write_text(
                '{"name":"first","name":"second"}',
                encoding="utf-8",
            )
            performance = _write_tiny_performance(directory)
            target = directory / "not-created" / "render.wav"

            with patch("tianlai.renderer.acquire_render_lock") as acquire:
                with self.assertRaisesRegex(ValueError, "严格且有界"):
                    render_to_wav_atomic(manifest, performance, target)

            acquire.assert_not_called()
            self.assertFalse(target.parent.exists())

    def test_duplicate_performance_member_fails_before_output_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "instrument.json"
            manifest.write_text('{"name":"bounded"}', encoding="utf-8")
            performance = directory / "duplicate.events.json"
            performance.write_text(
                '{"sample_rate":8000,"sample_rate":16000,"events":[]}',
                encoding="utf-8",
            )
            target = directory / "not-created" / "render.wav"

            with patch("tianlai.renderer.acquire_render_lock") as acquire:
                with self.assertRaisesRegex(ValueError, "严格且有界"):
                    render_to_wav_atomic(manifest, performance, target)

            acquire.assert_not_called()
            self.assertFalse(target.parent.exists())

    def test_input_replacement_while_waiting_for_lock_fails_before_staging(
        self,
    ) -> None:
        for replaced_input in ("manifest", "performance"):
            with self.subTest(replaced_input=replaced_input):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    manifest = directory / "instrument.json"
                    manifest.write_text(
                        '{"name":"captured"}',
                        encoding="utf-8",
                    )
                    performance = _write_tiny_performance(directory)
                    target = directory / "render.wav"
                    victim = (
                        manifest
                        if replaced_input == "manifest"
                        else performance
                    )

                    @contextmanager
                    def replacing_lock(_path, *, existing_target_kind):
                        self.assertEqual(existing_target_kind, "file")
                        replacement = victim.with_name(
                            f"{victim.name}.replacement"
                        )
                        replacement.write_bytes(victim.read_bytes())
                        os.replace(replacement, victim)
                        yield

                    with (
                        patch(
                            "tianlai.renderer.acquire_render_lock",
                            replacing_lock,
                        ),
                        patch(
                            "tianlai.renderer._reserve_private_file"
                        ) as reserve,
                        self.assertRaises(OSError),
                    ):
                        render_to_wav_atomic(
                            manifest,
                            performance,
                            target,
                        )

                    reserve.assert_not_called()
                    self.assertFalse(target.exists())

    def test_render_and_sidecar_share_one_manifest_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            manifest = directory / "instrument.json"
            original_manifest = {"name": "snapshot-original"}
            manifest.write_text(
                json.dumps(original_manifest),
                encoding="utf-8",
            )
            original_sha256 = hashlib.sha256(manifest.read_bytes()).hexdigest()
            performance = _write_tiny_performance(directory)
            target = directory / "snapshot.wav"
            validated: list[dict[str, object]] = []
            constructed: list[dict[str, object]] = []

            def validate(value: dict[str, object]) -> None:
                validated.append(dict(value))
                value["name"] = "policy-callback-mutation"

            def create(value, sample_rate, *, base_directory):
                del base_directory
                constructed.append(dict(value))
                manifest.write_text(
                    json.dumps({"name": "racing-replacement"}),
                    encoding="utf-8",
                )
                return _ConstantInstrument((0.0, 0.0), sample_rate)

            with patch(
                "tianlai.renderer.create_instrument",
                side_effect=create,
            ):
                result = render_to_wav_atomic(
                    manifest,
                    performance,
                    target,
                    _manifest_validator=validate,
                )

            sidecar = json.loads(
                Path(result.license_sidecar_path).read_text(encoding="utf-8")
            )
            self.assertEqual(validated, [original_manifest])
            self.assertEqual(constructed, [original_manifest])
            self.assertEqual(
                sidecar["instruments"][0]["instrument"],
                "snapshot-original",
            )
            self.assertEqual(
                sidecar["instruments"][0]["manifest"]["sha256"],
                original_sha256,
            )

    def test_manifest_with_utf8_bom_renders_and_writes_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            source_manifest = (
                ROOT
                / "乐器"
                / "测试工具"
                / "参考振荡器"
                / "乐器.json"
            )
            manifest = directory / "bom-instrument.json"
            manifest.write_bytes(b"\xef\xbb\xbf" + source_manifest.read_bytes())
            performance = _write_tiny_performance(directory)
            target = directory / "bom.wav"

            result = render_to_wav_atomic(manifest, performance, target)

            self.assertEqual(result.frame_count, 8)
            sidecar = json.loads(
                Path(result.license_sidecar_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                sidecar["instruments"][0]["instrument"],
                "精确音高参考振荡器",
            )

    def test_transaction_cleanup_preserves_a_racing_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            claim = renderer_module._reserve_private_file(
                directory,
                prefix=".render-stage.",
                suffix=".tmp",
            )
            staged = claim.path
            original = b"original staged bytes"
            staged.write_bytes(original)
            sealed = renderer_module._seal_private_file_claim(
                claim,
                expected_sha256=hashlib.sha256(original).hexdigest(),
            )
            parked = directory / "parked-original"
            real_rename_noreplace = renderer_module._rename_noreplace
            raced = False

            def swap_then_rename(source, destination):
                nonlocal raced
                if not raced and Path(source) == staged:
                    raced = True
                    real_rename_noreplace(staged, parked)
                    staged.write_bytes(b"replacement must survive")
                return real_rename_noreplace(source, destination)

            with patch(
                "tianlai.renderer._rename_noreplace",
                side_effect=swap_then_rename,
            ), self.assertRaisesRegex(
                OSError,
                "changed while being preserved",
            ):
                renderer_module._preserve_published_generation(
                    sealed,
                    staged,
                )

            self.assertTrue(raced)
            self.assertEqual(parked.read_bytes(), original)
            self.assertEqual(staged.read_bytes(), b"replacement must survive")

    def test_render_cleanup_preserves_and_warns_for_a_racing_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "cleanup-race.wav"
            performance = _write_tiny_performance(directory)
            parked_owned = directory / "parked-owned-stage"
            sentinel = b"racing stage replacement must survive"
            raced_path: Path | None = None

            def replace_stage_then_fail(json_path, _text_path, **_kwargs):
                nonlocal raced_path
                raced_path = Path(json_path)
                os.replace(raced_path, parked_owned)
                raced_path.write_bytes(sentinel)
                raise RuntimeError("injected sidecar race")

            with (
                patch(
                    "tianlai.renderer.create_instrument",
                    return_value=_ConstantInstrument((0.01, -0.01), 8_000),
                ),
                patch(
                    "tianlai.renderer.write_license_sidecars",
                    side_effect=replace_stage_then_fail,
                ),
                self.assertWarnsRegex(RuntimeWarning, "replacement preserved"),
                self.assertRaisesRegex(RuntimeError, "injected sidecar race"),
            ):
                render_to_wav_atomic(
                    ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                    performance,
                    target,
                )

            self.assertIsNotNone(raced_path)
            assert raced_path is not None
            self.assertFalse(raced_path.exists())
            self.assertEqual(parked_owned.read_bytes(), b"")
            preserved = list(directory.glob(".*.retired.*"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(preserved[0].read_bytes(), sentinel)
            for path in _render_quartet(target):
                self.assertFalse(path.exists())

    def test_rollback_does_not_clobber_a_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "published.json"
            backup_claim = renderer_module._reserve_private_file(
                directory,
                prefix=".published.json.",
                suffix=".tianlai-backup",
            )
            old_payload = b"old durable bytes"
            backup_claim.path.write_bytes(old_payload)
            backup = renderer_module._seal_private_file_claim(
                backup_claim,
                expected_sha256=hashlib.sha256(old_payload).hexdigest(),
            )
            concurrent = b"concurrent writer must survive"
            real_rename = renderer_module._rename_noreplace
            raced = False

            def race_before_no_clobber_install(source, destination):
                nonlocal raced
                if not raced and Path(destination) == target:
                    raced = True
                    target.write_bytes(concurrent)
                return real_rename(source, destination)

            with patch(
                "tianlai.renderer._rename_noreplace",
                side_effect=race_before_no_clobber_install,
            ):
                with self.assertRaises(FileExistsError):
                    renderer_module._restore_published_file(backup, target)

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), concurrent)
            self.assertEqual(backup.claim.path.read_bytes(), old_payload)
            self.assertIsNone(
                renderer_module._retire_private_file(backup.claim)
            )

    def test_render_is_deterministic_and_pcm24(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            first = Path(temporary_directory) / "first.wav"
            second = Path(temporary_directory) / "second.wav"
            result = render_to_wav(
                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                ROOT / "examples/c_major.events.json",
                first,
            )
            render_to_wav(
                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                ROOT / "examples/c_major.events.json",
                second,
            )
            self.assertEqual(hashlib.sha256(first.read_bytes()).digest(), hashlib.sha256(second.read_bytes()).digest())
            self.assertEqual(result.sample_rate, 48000)
            self.assertEqual(result.peak_active_voices, 4)
            self.assertEqual(
                result.license_sidecar_path,
                str(first.with_name(f"{first.name}.许可与署名.json")),
            )
            self.assertEqual(
                result.attribution_path,
                str(first.with_name(f"{first.name}.许可与署名.txt")),
            )
            self.assertEqual(
                result.post_render_check_path,
                str(Path(f"{first}.{POST_RENDER_CHECK_NAME}")),
            )
            self.assertIsNotNone(result.post_render_check)
            self.assertIsNotNone(result.post_render_check_summary)
            self.assertEqual(
                json.loads(
                    Path(result.post_render_check_path).read_text(
                        encoding="utf-8"
                    )
                ),
                result.post_render_check,
            )
            assert result.post_render_check is not None
            self.assertEqual(
                result.post_render_check["artifact"],
                {
                    "path": first.name,
                    "sha256": hashlib.sha256(first.read_bytes()).hexdigest(),
                    "size_bytes": first.stat().st_size,
                },
            )
            self.assertEqual(
                result.post_render_check["audio_format"],
                {
                    "container": "WAV",
                    "encoding": "PCM",
                    "bits_per_sample": 24,
                    "channels": 2,
                    "sample_rate": 48_000,
                    "frame_count": result.frame_count,
                },
            )
            self.assertIs(
                result.post_render_check["summary"]["can_proceed"],
                True,
            )
            sidecar = json.loads(
                Path(result.license_sidecar_path).read_text(encoding="utf-8")
            )
            self.assertEqual(
                sidecar["scope"]["rule"],
                "actual_render_inputs_only",
            )
            self.assertEqual(sidecar["scope"]["instrument_count"], 1)
            self.assertEqual(
                sidecar["instruments"][0]["manifest"]["path"],
                "测试工具/参考振荡器/乐器.json",
            )
            self.assertIsNone(sidecar["instruments"][0]["creator"])
            self.assertEqual(
                sidecar["audio_artifacts"][0]["sha256"],
                hashlib.sha256(first.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                sidecar["audio_artifacts"][0]["path"],
                first.name,
            )
            with wave.open(str(first), "rb") as audio:
                self.assertEqual(audio.getnchannels(), 2)
                self.assertEqual(audio.getsampwidth(), 3)
                self.assertEqual(audio.getframerate(), 48000)
                self.assertEqual(audio.getnframes(), result.frame_count)
            _assert_only_expected_private_files(
                self,
                Path(temporary_directory),
            )

    def test_successful_overwrite_retires_every_old_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "overwrite.wav"
            performance = _write_tiny_performance(directory)
            previous = _seed_old_quartet(target)

            with patch(
                "tianlai.renderer.create_instrument",
                return_value=_ConstantInstrument((0.01, -0.01), 8_000),
            ):
                render_to_wav_atomic(
                    ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                    performance,
                    target,
                )

            for path, old_payload in previous.items():
                self.assertTrue(path.is_file())
                self.assertNotEqual(path.read_bytes(), old_payload)
            _assert_only_expected_private_files(self, directory)
            self.assertEqual(list(directory.glob(".*.tianlai-backup*")), [])
            self.assertEqual(list(directory.glob(".*.retired.*")), [])

    def test_post_render_check_reads_staged_pcm_and_binds_unicode_final_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory) / "Unicode 路径"
            directory.mkdir()
            target = directory / "天籁 试听.wav"
            observed: dict[str, object] = {}
            real_analyze = renderer_module.analyze_rendered_wav

            def recording_analyze(path, **kwargs):
                staged = Path(path)
                observed["staged"] = staged
                observed["audio_bytes"] = staged.read_bytes()
                observed["kwargs"] = dict(kwargs)
                return real_analyze(path, **kwargs)

            with patch(
                "tianlai.renderer.analyze_rendered_wav",
                side_effect=recording_analyze,
            ):
                result = render_to_wav_atomic(
                    ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                    ROOT / "examples/c_major.events.json",
                    target,
                )

            staged = observed["staged"]
            self.assertIsInstance(staged, Path)
            assert isinstance(staged, Path)
            self.assertNotEqual(staged, target)
            self.assertFalse(staged.exists())
            self.assertEqual(target.read_bytes(), observed["audio_bytes"])
            kwargs = observed["kwargs"]
            self.assertIsInstance(kwargs, dict)
            assert isinstance(kwargs, dict)
            self.assertEqual(kwargs["artifact_path"], target.name)
            self.assertEqual(kwargs["expected_sample_rate"], 48_000)
            self.assertEqual(kwargs["expected_frame_count"], result.frame_count)
            self.assertIs(kwargs["expected_activity"], True)
            self.assertRegex(str(kwargs["plan_sha256"]), r"^[0-9a-f]{64}$")
            self.assertEqual(
                Path(result.post_render_check_path),
                Path(f"{target}.{POST_RENDER_CHECK_NAME}"),
            )

    def test_post_render_check_is_byte_deterministic_for_same_final_name(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first" / "result.wav"
            second = root / "second" / "result.wav"
            performance = _write_tiny_performance(root)
            render_to_wav_atomic(
                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                performance,
                first,
            )
            render_to_wav_atomic(
                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                performance,
                second,
            )

            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(
                Path(f"{first}.{POST_RENDER_CHECK_NAME}").read_bytes(),
                Path(f"{second}.{POST_RENDER_CHECK_NAME}").read_bytes(),
            )

    def test_post_render_check_write_failure_preserves_old_quartet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "transaction.wav"
            performance = _write_tiny_performance(directory)
            previous = _seed_old_quartet(target)

            with (
                patch(
                    "tianlai.renderer.create_instrument",
                    return_value=_ConstantInstrument((0.01, -0.01), 8_000),
                ),
                patch(
                    "tianlai.renderer.write_post_render_check",
                    side_effect=RuntimeError("injected post-render write failure"),
                ),
                patch(
                    "tianlai.renderer._replace_published_file",
                    wraps=renderer_module._replace_published_file,
                ) as publish_replace,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected post-render write failure",
                ):
                    render_to_wav_atomic(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        performance,
                        target,
                    )

            publish_replace.assert_not_called()
            for path, payload in previous.items():
                self.assertEqual(path.read_bytes(), payload)
            _assert_only_expected_private_files(self, directory)

    def test_mutated_staged_post_render_check_preserves_old_quartet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "transaction.wav"
            performance = _write_tiny_performance(directory)
            previous = _seed_old_quartet(target)

            def tamper(path, _report, **_kwargs):
                Path(path).write_text("{}\n", encoding="utf-8")

            with (
                patch(
                    "tianlai.renderer.create_instrument",
                    return_value=_ConstantInstrument((0.01, -0.01), 8_000),
                ),
                patch(
                    "tianlai.renderer.write_post_render_check",
                    side_effect=tamper,
                ),
                patch(
                    "tianlai.renderer._replace_published_file",
                    wraps=renderer_module._replace_published_file,
                ) as publish_replace,
            ):
                with self.assertRaisesRegex(ValueError, "自检报告.*不一致"):
                    render_to_wav_atomic(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        performance,
                        target,
                    )

            publish_replace.assert_not_called()
            for path, payload in previous.items():
                self.assertEqual(path.read_bytes(), payload)
            _assert_only_expected_private_files(self, directory)

    def test_misbound_report_hash_is_rejected_before_write_or_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "transaction.wav"
            performance = _write_tiny_performance(directory)
            previous = _seed_old_quartet(target)
            real_analyze = renderer_module.analyze_rendered_wav

            def return_misbound_report(path, **kwargs):
                report = real_analyze(path, **kwargs)
                report["artifact"]["sha256"] = "0" * 64
                return report

            with (
                patch(
                    "tianlai.renderer.create_instrument",
                    return_value=_ConstantInstrument((0.01, -0.01), 8_000),
                ),
                patch(
                    "tianlai.renderer.analyze_rendered_wav",
                    side_effect=return_misbound_report,
                ),
                patch(
                    "tianlai.renderer.write_post_render_check",
                    wraps=renderer_module.write_post_render_check,
                ) as report_writer,
                patch(
                    "tianlai.renderer._replace_published_file",
                    wraps=renderer_module._replace_published_file,
                ) as publish_replace,
            ):
                with self.assertRaisesRegex(RuntimeError, "没有绑定.*WAV"):
                    render_to_wav_atomic(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        performance,
                        target,
                    )

            report_writer.assert_not_called()
            publish_replace.assert_not_called()
            for path, payload in previous.items():
                self.assertEqual(path.read_bytes(), payload)
            _assert_only_expected_private_files(self, directory)

    def test_expected_activity_digital_silence_blocks_before_publication(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "transaction.wav"
            performance = _write_tiny_active_performance(directory)
            previous = _seed_old_quartet(target)

            with (
                patch(
                    "tianlai.renderer.create_instrument",
                    return_value=_ConstantInstrument((0.0, 0.0), 8_000),
                ),
                patch(
                    "tianlai.renderer.write_post_render_check",
                    wraps=renderer_module.write_post_render_check,
                ) as report_writer,
                patch(
                    "tianlai.renderer._replace_published_file",
                    wraps=renderer_module._replace_published_file,
                ) as publish_replace,
            ):
                with self.assertRaisesRegex(RuntimeError, "自检未通过"):
                    render_to_wav_atomic(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        performance,
                        target,
                    )

            report_writer.assert_not_called()
            publish_replace.assert_not_called()
            for path, payload in previous.items():
                self.assertEqual(path.read_bytes(), payload)
            _assert_only_expected_private_files(self, directory)

    def test_clipping_does_not_replace_an_existing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "existing.wav"
            original = b"previous verified render"
            target.write_bytes(original)

            with patch(
                "tianlai.renderer.create_instrument",
                return_value=_ConstantInstrument((1.000_001, 0.0)),
            ):
                with self.assertRaisesRegex(ValueError, "过载"):
                    render_to_wav(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        ROOT / "examples/c_major.events.json",
                        target,
                    )

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(
                list(directory.glob(f".{target.name}.*.tianlai-part")),
                [],
            )
            self.assertFalse(
                target.with_name(f"{target.name}.许可与署名.json").exists()
            )
            self.assertFalse(
                target.with_name(f"{target.name}.许可与署名.txt").exists()
            )
            self.assertFalse(
                Path(f"{target}.{POST_RENDER_CHECK_NAME}").exists()
            )

    def test_non_finite_frames_leave_no_output_or_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            for label, frame in (
                ("nan", (float("nan"), 0.0)),
                ("positive-infinity", (0.0, float("inf"))),
                ("negative-infinity", (float("-inf"), 0.0)),
            ):
                with self.subTest(label=label):
                    target = directory / f"{label}.wav"
                    with patch(
                        "tianlai.renderer.create_instrument",
                        return_value=_ConstantInstrument(frame),
                    ):
                        with self.assertRaisesRegex(ValueError, "非有限样本"):
                            render_to_wav_atomic(
                                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                                ROOT / "examples/c_major.events.json",
                                target,
                            )

                    self.assertFalse(target.exists())
                    self.assertEqual(
                        list(directory.glob(f".{target.name}.*.tianlai-part")),
                        [],
                    )

    def test_each_output_target_rejects_live_and_dangling_symlinks_before_staging(
        self,
    ) -> None:
        for member_index, member_name in enumerate(
            ("wav", "json", "text", "post-render-check")
        ):
            for dangling in (False, True):
                with self.subTest(member=member_name, dangling=dangling):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        directory = Path(temporary_directory)
                        target = directory / "transaction.wav"
                        performance = _write_tiny_performance(directory)
                        member = _render_quartet(target)[member_index]
                        link_target = directory / (
                            "missing.bin" if dangling else "existing.bin"
                        )
                        if not dangling:
                            link_target.write_bytes(b"must remain untouched")
                        link_created = True
                        try:
                            member.symlink_to(link_target)
                        except (NotImplementedError, OSError):
                            # Some Windows hosts lack symlink privileges.  Keep
                            # the validator covered there by making only this
                            # output path report the same lstat mode as both a
                            # live and a dangling symlink would expose.
                            link_created = False

                        original_lstat = Path.lstat

                        def lstat_with_synthetic_link(path: Path):
                            if not link_created and path == member:
                                return SimpleNamespace(st_mode=stat.S_IFLNK)
                            return original_lstat(path)

                        lstat_context = (
                            nullcontext()
                            if link_created
                            else patch.object(
                                Path,
                                "lstat",
                                lstat_with_synthetic_link,
                            )
                        )

                        with lstat_context, patch(
                            "tianlai.atomic_publish.tempfile.mkstemp"
                        ) as reserve_staging:
                            with self.assertRaisesRegex(ValueError, "符号链接"):
                                render_to_wav_atomic(
                                    ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                                    performance,
                                    target,
                                )

                        reserve_staging.assert_not_called()
                        if link_created:
                            self.assertTrue(member.is_symlink())
                        else:
                            self.assertFalse(member.exists())
                        if not dangling:
                            self.assertEqual(
                                link_target.read_bytes(),
                                b"must remain untouched",
                            )

    def test_each_output_target_rejects_non_regular_files_before_staging(
        self,
    ) -> None:
        for member_index, member_name in enumerate(
            ("wav", "json", "text", "post-render-check")
        ):
            with self.subTest(member=member_name):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    target = directory / "transaction.wav"
                    performance = _write_tiny_performance(directory)
                    member = _render_quartet(target)[member_index]
                    member.mkdir()

                    with patch(
                        "tianlai.atomic_publish.tempfile.mkstemp"
                    ) as reserve_staging:
                        with self.assertRaisesRegex(ValueError, "非普通文件"):
                            render_to_wav_atomic(
                                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                                performance,
                                target,
                            )

                    reserve_staging.assert_not_called()
                    self.assertTrue(member.is_dir())

    def test_sidecar_build_failure_preserves_the_previous_quartet_or_none(self) -> None:
        for existing in (False, True):
            with self.subTest(existing=existing):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    directory = Path(temporary_directory)
                    target = directory / "transaction.wav"
                    performance = _write_tiny_performance(directory)
                    previous = _seed_old_quartet(target) if existing else {}

                    with (
                        patch(
                            "tianlai.renderer.create_instrument",
                            return_value=_ConstantInstrument(
                                (0.01, -0.01),
                                8_000,
                            ),
                        ),
                        patch(
                            "tianlai.renderer.write_license_sidecars",
                            side_effect=RuntimeError("injected sidecar build failure"),
                        ),
                        patch(
                            "tianlai.renderer._replace_published_file",
                            wraps=renderer_module._replace_published_file,
                        ) as publish_replace,
                    ):
                        with self.assertRaisesRegex(
                            RuntimeError,
                            "injected sidecar build failure",
                        ):
                            render_to_wav_atomic(
                                ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                                performance,
                                target,
                            )

                    publish_replace.assert_not_called()
                    for path in _render_quartet(target):
                        if path in previous:
                            self.assertEqual(path.read_bytes(), previous[path])
                        else:
                            self.assertFalse(path.exists())
                    _assert_only_expected_private_files(self, directory)

    def test_each_publication_failure_rolls_back_the_whole_quartet(self) -> None:
        for existing in (False, True):
            for failed_member in ("json", "text", "post-render-check", "wav"):
                with self.subTest(
                    existing=existing,
                    failed_member=failed_member,
                ):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        directory = Path(temporary_directory)
                        target = directory / "transaction.wav"
                        performance = _write_tiny_performance(directory)
                        previous = _seed_old_quartet(target) if existing else {}
                        (
                            audio_path,
                            json_path,
                            text_path,
                            check_path,
                        ) = _render_quartet(target)
                        failed_target = {
                            "json": json_path,
                            "text": text_path,
                            "post-render-check": check_path,
                            "wav": audio_path,
                        }[failed_member]
                        original_replace = renderer_module._replace_published_file
                        failure_injected = False
                        publication_attempts: list[Path] = []

                        def fail_one_publication(
                            staged: Path,
                            destination: Path,
                        ) -> None:
                            nonlocal failure_injected
                            publication_attempts.append(destination)
                            if (
                                destination
                                == failed_target.resolve(strict=False)
                                and not failure_injected
                            ):
                                failure_injected = True
                                raise OSError(
                                    f"injected {failed_member} publication failure"
                                )
                            original_replace(staged, destination)

                        with (
                            patch(
                                "tianlai.renderer.create_instrument",
                                return_value=_ConstantInstrument(
                                    (0.01, -0.01),
                                    8_000,
                                ),
                            ),
                            patch(
                                "tianlai.renderer._replace_published_file",
                                side_effect=fail_one_publication,
                            ),
                        ):
                            with self.assertRaisesRegex(
                                OSError,
                                f"injected {failed_member} publication failure",
                            ):
                                render_to_wav_atomic(
                                    ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                                    performance,
                                    target,
                                )

                        self.assertTrue(failure_injected)
                        expected_order = [
                            path.resolve(strict=False)
                            for path in (
                                json_path,
                                text_path,
                                check_path,
                                audio_path,
                            )
                        ]
                        failed_index = expected_order.index(
                            failed_target.resolve(strict=False)
                        )
                        self.assertEqual(
                            publication_attempts,
                            expected_order[: failed_index + 1],
                        )
                        for path in _render_quartet(target):
                            if path in previous:
                                self.assertEqual(path.read_bytes(), previous[path])
                            else:
                                self.assertFalse(path.exists())
                        _assert_only_expected_private_files(
                            self,
                            directory,
                            allow_rollbacks=True,
                        )

    def test_first_persistent_publish_lock_does_not_touch_original_quartet(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "transaction.wav"
            performance = _write_tiny_performance(directory)
            previous = _seed_old_quartet(target)
            _, json_path, _, _ = _render_quartet(target)

            with (
                patch(
                    "tianlai.renderer.create_instrument",
                    return_value=_ConstantInstrument((0.01, -0.01), 8_000),
                ),
                patch(
                    "tianlai.renderer._replace_published_file",
                    side_effect=PermissionError("persistently locked"),
                ) as publish_replace,
                patch(
                    "tianlai.renderer._restore_published_file",
                    wraps=renderer_module._restore_published_file,
                ) as restore_replace,
            ):
                with self.assertRaisesRegex(PermissionError, "persistently locked"):
                    render_to_wav_atomic(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        performance,
                        target,
                    )

            publish_replace.assert_called_once()
            self.assertEqual(
                publish_replace.call_args.args[1],
                json_path.resolve(strict=False),
            )
            restore_replace.assert_called_once()
            for path, payload in previous.items():
                self.assertEqual(path.read_bytes(), payload)
            _assert_only_expected_private_files(self, directory)


if __name__ == "__main__":
    unittest.main()
