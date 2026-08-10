import hashlib
import json
from contextlib import nullcontext
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


def _assert_only_recoverable_private_files(
    test: unittest.TestCase,
    directory: Path,
) -> None:
    hidden = [path for path in directory.iterdir() if path.name.startswith(".")]
    test.assertTrue(hidden)
    for path in hidden:
        test.assertTrue(path.is_file())
        test.assertTrue(
            ".cleanup-preserved-" in path.name
            or ".rollback-preserved-" in path.name,
            path.name,
        )


class RendererTests(unittest.TestCase):
    def test_transaction_cleanup_preserves_a_racing_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            staged = directory / ".render-stage.tmp"
            staged.write_bytes(b"original staged bytes")
            parked = directory / "parked-original"
            real_rename = os.rename
            raced = False

            def swap_then_rename(source, destination):
                nonlocal raced
                if not raced and Path(source) == staged:
                    raced = True
                    real_rename(staged, parked)
                    staged.write_bytes(b"replacement must survive")
                return real_rename(source, destination)

            with patch(
                "tianlai.renderer.os.rename",
                side_effect=swap_then_rename,
            ):
                preserved = renderer_module._preserve_transaction_file(
                    staged,
                    label="cleanup",
                )

            self.assertTrue(raced)
            self.assertEqual(parked.read_bytes(), b"original staged bytes")
            self.assertIsNotNone(preserved)
            assert preserved is not None
            self.assertEqual(preserved.read_bytes(), b"replacement must survive")
            self.assertFalse(staged.exists())

    def test_rollback_does_not_clobber_a_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "published.json"
            backup = directory / ".published.json.tianlai-backup"
            target.write_bytes(b"new transaction bytes")
            backup.write_bytes(b"old durable bytes")
            concurrent = b"concurrent writer must survive"
            real_link = os.link
            raced = False

            def race_before_no_clobber_install(source, destination, **kwargs):
                nonlocal raced
                if not raced and Path(destination) == target:
                    raced = True
                    target.write_bytes(concurrent)
                return real_link(source, destination, **kwargs)

            with patch(
                "tianlai.renderer.os.link",
                side_effect=race_before_no_clobber_install,
            ):
                with self.assertRaises(FileExistsError):
                    renderer_module._restore_published_file(backup, target)

            self.assertTrue(raced)
            self.assertEqual(target.read_bytes(), concurrent)
            self.assertEqual(backup.read_bytes(), b"old durable bytes")
            preserved = list(directory.glob(".*.rollback-preserved-*"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(
                preserved[0].read_bytes(),
                b"new transaction bytes",
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
            self.assertEqual(
                sorted(
                    path.name
                    for path in Path(temporary_directory).iterdir()
                    if path.name.startswith(".")
                ),
                [],
            )

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
            _assert_only_recoverable_private_files(self, directory)

    def test_mutated_staged_post_render_check_preserves_old_quartet(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target = directory / "transaction.wav"
            performance = _write_tiny_performance(directory)
            previous = _seed_old_quartet(target)

            def tamper(path, _report):
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
            _assert_only_recoverable_private_files(self, directory)

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
            _assert_only_recoverable_private_files(self, directory)

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
            _assert_only_recoverable_private_files(self, directory)

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
                            "tianlai.renderer.tempfile.mkstemp"
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
                        "tianlai.renderer.tempfile.mkstemp"
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
                    _assert_only_recoverable_private_files(self, directory)

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
                            if destination == failed_target and not failure_injected:
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
                            json_path,
                            text_path,
                            check_path,
                            audio_path,
                        ]
                        failed_index = expected_order.index(failed_target)
                        self.assertEqual(
                            publication_attempts,
                            expected_order[: failed_index + 1],
                        )
                        for path in _render_quartet(target):
                            if path in previous:
                                self.assertEqual(path.read_bytes(), previous[path])
                            else:
                                self.assertFalse(path.exists())
                        _assert_only_recoverable_private_files(self, directory)

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
                    side_effect=PermissionError("lock also blocks restore"),
                ) as restore_replace,
            ):
                with self.assertRaisesRegex(PermissionError, "persistently locked"):
                    render_to_wav_atomic(
                        ROOT / "乐器/测试工具/参考振荡器/乐器.json",
                        performance,
                        target,
                    )

            publish_replace.assert_called_once()
            self.assertEqual(publish_replace.call_args.args[1], json_path)
            restore_replace.assert_not_called()
            for path, payload in previous.items():
                self.assertEqual(path.read_bytes(), payload)
            _assert_only_recoverable_private_files(self, directory)


if __name__ == "__main__":
    unittest.main()
