from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import replace
from pathlib import Path
import pickle
import queue
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import numpy as np
import pytest

from tianlai import ensemble as ensemble_module
from tianlai import stem_worker as stem_worker_module
from tianlai.instrument import Instrument
from tianlai.stem_worker import (
    StemRenderJob,
    StemWorkerError,
    collect_stem_worker,
    managed_subprocess_workers_available,
    terminate_stem_worker,
    try_start_stem_worker,
)
from tianlai.worker_slots import WorkerResourceClaim, WorkerSlotPool


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_MANIFEST = ROOT / "乐器" / "世界乐器" / "卡林巴" / "乐器.json"
DSP_MANIFEST = ROOT / "乐器" / "电子乐器" / "温暖铺底" / "乐器.json"
BIANZHONG_MANIFEST = ROOT / "乐器" / "世界乐器" / "编钟" / "乐器.json"


_PERFORMANCE = {
    "sample_rate": 8_000,
    "channels": 2,
    "tail_seconds": 0.0,
    "duration_seconds": 0.001,
    "events": [],
}

_SAMPLE_PERFORMANCE = {
    "sample_rate": 8_000,
    "channels": 2,
    "tail_seconds": 0.0,
    "duration_seconds": 0.05,
    "events": [
        {
            "type": "note_on",
            "time": 0.0,
            "note_id": 1,
            "midi_note": 60,
            "velocity": 0.4,
        },
        {"type": "note_off", "time": 0.02, "note_id": 1},
    ],
}

_BIANZHONG_PERFORMANCE = {
    "sample_rate": 8_000,
    "channels": 2,
    "tail_seconds": 0.0,
    "duration_seconds": 0.05,
    "events": [
        {
            "type": "note_on",
            "time": 0.0,
            "note_id": 1,
            "midi_note": 60,
            "velocity": 0.4,
        },
        {"type": "note_off", "time": 0.02, "note_id": 1},
    ],
}

_LONG_SILENT_PERFORMANCE = {
    "sample_rate": 8_000,
    "channels": 2,
    "tail_seconds": 0.0,
    "duration_seconds": 2.0,
    "events": [],
}


class _CustomFrameInstrument(Instrument):
    def __init__(self, sample_rate: int) -> None:
        super().__init__(sample_rate)
        self.frames = 0

    def handle_event(self, event: object, tuning: object) -> None:
        del event, tuning

    def render_frame(self) -> tuple[float, float]:
        self.frames += 1
        return 0.125, -0.25

    @property
    def active_voice_count(self) -> int:
        return 0


def _write_loud_instrument(
    directory: Path,
    *,
    fail: bool = False,
    memory_error: bool = False,
    truncate: bool = False,
    hang: bool = False,
    lifecycle_path: Path | None = None,
) -> Path:
    implementation = directory / "instrument_impl.py"
    body = """
import os
from tianlai.instrument import Instrument

class LoudInstrument(Instrument):
    def __init__(self, sample_rate, lifecycle_path=None):
        super().__init__(sample_rate)
        self.frames = 0
        self.lifecycle_path = lifecycle_path
        if lifecycle_path:
            with open(lifecycle_path, "a", encoding="utf-8") as target:
                target.write("create\\n")

    def handle_event(self, event, tuning):
        pass

    def render_frame(self):
        if self.frames == 0:
            print("custom Python stdout before private marker", flush=True)
            os.write(1, b"native stdout before private marker\\n")
        self.frames += 1
        return 0.25, -0.125

    @property
    def active_voice_count(self):
        return 0

    def close(self):
        print("custom close stdout before private marker", flush=True)
        if self.lifecycle_path:
            with open(self.lifecycle_path, "a", encoding="utf-8") as target:
                target.write("close\\n")

def create(*, manifest, sample_rate, base_directory):
    print("custom factory stdout before private marker", flush=True)
    return LoudInstrument(sample_rate, manifest.get("lifecycle_path"))
"""
    if memory_error:
        body = body.replace(
            "return 0.25, -0.125",
            "raise MemoryError('deliberate worker exhaustion')",
        )
    elif fail:
        body = body.replace(
            "return 0.25, -0.125",
            "raise RuntimeError('deliberate worker failure')",
        )
    elif truncate:
        body = body.replace("return 0.25, -0.125", "os._exit(0)")
    elif hang:
        body = body.replace(
            "return 0.25, -0.125",
            "__import__('time').sleep(30); return 0.25, -0.125",
        )
    implementation.write_text(body, encoding="utf-8")
    manifest = directory / "instrument.json"
    manifest_document = {
        "name": "loud-test-instrument",
        "implementation": implementation.name,
    }
    if lifecycle_path is not None:
        manifest_document["lifecycle_path"] = str(lifecycle_path)
    manifest.write_text(
        json.dumps(manifest_document),
        encoding="utf-8",
    )
    return manifest


@pytest.fixture
def isolated_warm_pool() -> None:
    stem_worker_module._shutdown_warm_pool()
    yield
    stem_worker_module._shutdown_warm_pool()


def _try_start_warm_stem_worker(
    job: StemRenderJob,
    *,
    scratch_directory: str | Path,
    allow_warm_start: bool = True,
) -> stem_worker_module.StemWorkerHandle | None:
    return stem_worker_module._try_start_stem_worker(
        job,
        scratch_directory=scratch_directory,
        allow_warm_start=allow_warm_start,
        allow_warm_reuse=True,
    )


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_managed_worker_is_pickle_safe_and_ignores_custom_stdout() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root)
        job = StemRenderJob.create(
            index=3,
            executor_id="loud",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        assert pickle.loads(pickle.dumps(job)) == job

        handle = try_start_stem_worker(job, scratch_directory=root)
        assert handle is not None
        with collect_stem_worker(handle) as result:
            audio = result.load_audio()
            assert result.index == 3
            assert result.executor_id == "loud"
            assert result.frame_count == 8
            assert result.byte_count == 64
            assert result.manifest_sha256 == hashlib.sha256(
                manifest.read_bytes()
            ).hexdigest()
            assert result.audio_sha256 == hashlib.sha256(
                audio.astype("<f4", copy=False).tobytes()
            ).hexdigest()
            np.testing.assert_array_equal(
                audio,
                np.tile(
                    np.asarray([[0.25, -0.125]], dtype=np.float32),
                    (8, 1),
                ),
            )


def test_detached_result_has_single_source_owner_and_completion_gate() -> None:
    audio = np.asarray(
        [[0.125, -0.25], [0.5, -0.75]],
        dtype="<f4",
    )
    payload = audio.tobytes(order="C")
    raw = tempfile.TemporaryFile(mode="w+b")
    raw.write(payload)
    raw.flush()
    result = stem_worker_module.StemWorkerResult(
        index=0,
        executor_id="detached-owner",
        sample_rate=8_000,
        frame_count=2,
        peak_voices=1,
        manifest_sha256="1" * 64,
        audio_sha256=hashlib.sha256(payload).hexdigest(),
        audio_file=raw,
        audio_offset=0,
        byte_count=len(payload),
    )
    completions: list[bool] = []

    source = result.detach_source(
        completion_callback=completions.append
    )
    with pytest.raises(ValueError, match="closed"):
        result.load_audio()
    np.testing.assert_array_equal(source.materialise(), audio)
    source.close()

    assert completions == [True]
    assert raw.closed


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_reserved_worker_slot_lives_until_one_shot_result_close() -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="globally-admitted-one-shot",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "slots")

        def claim(owner_id: str) -> WorkerResourceClaim:
            return WorkerResourceClaim(
                owner_id=owner_id,
                owner_cpu_capacity=4,
                worker_memory_bytes=16 * 1024 * 1024,
                coordinator_memory_bytes=16 * 1024 * 1024,
                memory_budget_bytes=1024 * 1024 * 1024,
                scratch_bytes=job.frame_count * 2 * 4,
                scratch_directory=root,
            )

        reservation = pool.reserve_exact((claim("1" * 32),))
        assert reservation is not None
        slot = reservation.take()
        reservation.close()
        handle = stem_worker_module._try_start_stem_worker(
            job,
            scratch_directory=root,
            allow_warm_start=False,
            allow_warm_reuse=False,
            reserved_slot=slot,
        )
        assert handle is not None
        result = collect_stem_worker(handle)
        try:
            # The child has exited, but its anonymous raw result still owns
            # the parent reservation and prevents a four-slot over-admission.
            assert pool.reserve_exact(
                tuple(claim("2" * 32) for _ in range(4))
            ) is None
            audio = result.load_audio()
            assert audio.shape == (job.frame_count, 2)
        finally:
            result.close()

        released = pool.reserve_exact(
            tuple(claim("3" * 32) for _ in range(4))
        )
        assert released is not None
        released.close()


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_session_warm_slot_requeues_only_after_result_close(
    isolated_warm_pool: None,
) -> None:
    first_job = StemRenderJob.create(
        index=0,
        executor_id="session-warm-first",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    second_job = replace(
        first_job,
        index=1,
        executor_id="session-warm-second",
    )
    owner_id = "4" * 32
    worker_memory = 16 * 1024 * 1024
    coordinator_memory = 16 * 1024 * 1024
    memory_budget = 1024 * 1024 * 1024
    scratch_bytes = first_job.frame_count * 2 * 4
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory).resolve()
        pool = WorkerSlotPool(root / "slots")
        binding = stem_worker_module._ManagedWarmBinding(
            owner_id=owner_id,
            scratch_directory=root,
            scratch_volume_id=(
                stem_worker_module.scratch_volume_identity(root)
            ),
            worker_memory_ceiling_bytes=worker_memory,
            coordinator_memory_bytes=coordinator_memory,
            memory_budget_bytes=memory_budget,
            scratch_ceiling_bytes=scratch_bytes,
        )

        def claim(claim_owner: str = owner_id) -> WorkerResourceClaim:
            return WorkerResourceClaim(
                owner_id=claim_owner,
                owner_cpu_capacity=4,
                worker_memory_bytes=worker_memory,
                coordinator_memory_bytes=coordinator_memory,
                memory_budget_bytes=memory_budget,
                scratch_bytes=scratch_bytes,
                scratch_directory=root,
            )

        reservation = pool.reserve_exact((claim(),))
        assert reservation is not None
        first = stem_worker_module._try_start_stem_worker(
            first_job,
            scratch_directory=root,
            allow_warm_start=True,
            allow_warm_reuse=True,
            reserved_slot=reservation.take(),
            managed_warm_binding=binding,
            managed_worker_memory_bytes=worker_memory,
        )
        reservation.close()
        assert first is not None and first._warm_worker is not None
        process = first.process
        worker = first._warm_worker
        first_result = collect_stem_worker(first)
        assert first_result._elapsed_ns > 0
        assert not first_result._warm_used
        assert process.poll() is None
        assert not stem_worker_module._WARM_IDLE

        # The response is complete, but the old result scratch still owns the
        # task and its global ceiling.  It cannot be checked out early.
        with pytest.raises(
            StemWorkerError,
            match="unavailable for this session",
        ):
            stem_worker_module._try_start_stem_worker(
                second_job,
                scratch_directory=root,
                allow_warm_start=False,
                allow_warm_reuse=True,
                managed_warm_binding=binding,
                managed_worker_memory_bytes=worker_memory,
            )
        assert pool.reserve_exact(tuple(claim("5" * 32) for _ in range(4))) is None

        first_result.close()
        assert stem_worker_module._WARM_IDLE == [worker]
        second = stem_worker_module._try_start_stem_worker(
            second_job,
            scratch_directory=root,
            allow_warm_start=False,
            allow_warm_reuse=True,
            managed_warm_binding=binding,
            managed_worker_memory_bytes=worker_memory,
        )
        assert second is not None
        assert second.process is process
        second_result = collect_stem_worker(second)
        assert second_result._warm_used
        assert second_result._elapsed_ns > 0
        source = second_result.detach_source()
        blocks = tuple(source.iter_blocks())
        assert sum(block.shape[0] for block in blocks) == second_job.frame_count
        assert not stem_worker_module._WARM_IDLE
        source.close()
        assert stem_worker_module._WARM_IDLE == [worker]

        with pytest.raises(
            StemWorkerError,
            match="unavailable for this session",
        ):
            stem_worker_module._try_start_stem_worker(
                replace(
                    second_job,
                    index=2,
                    executor_id="foreign-session",
                ),
                scratch_directory=root,
                allow_warm_start=False,
                allow_warm_reuse=True,
                managed_warm_binding=replace(
                    binding,
                    owner_id="7" * 32,
                ),
                managed_worker_memory_bytes=worker_memory,
            )
        with pytest.raises(
            ValueError,
            match="exceeds its admitted ceiling",
        ):
            stem_worker_module._try_start_stem_worker(
                replace(
                    second_job,
                    index=2,
                    executor_id="over-ceiling",
                ),
                scratch_directory=root,
                allow_warm_start=False,
                allow_warm_reuse=True,
                managed_warm_binding=binding,
                managed_worker_memory_bytes=worker_memory + 1,
            )
        with pytest.raises(ValueError, match="scratch volume changed"):
            stem_worker_module._try_start_stem_worker(
                replace(
                    second_job,
                    index=2,
                    executor_id="foreign-volume",
                ),
                scratch_directory=root,
                allow_warm_start=False,
                allow_warm_reuse=True,
                managed_warm_binding=replace(
                    binding,
                    scratch_volume_id="forged-volume",
                ),
                managed_worker_memory_bytes=worker_memory,
            )
        assert stem_worker_module._WARM_IDLE == [worker]
        assert process.poll() is None

        third = stem_worker_module._try_start_stem_worker(
            replace(
                second_job,
                index=2,
                executor_id="session-warm-abandoned",
            ),
            scratch_directory=root,
            allow_warm_start=False,
            allow_warm_reuse=True,
            managed_warm_binding=binding,
            managed_worker_memory_bytes=worker_memory,
        )
        assert third is not None and third.process is process
        abandoned = collect_stem_worker(third).detach_source()
        abandoned.close()
        assert process.poll() is not None
        assert not stem_worker_module._WARM_IDLE

        stem_worker_module._retire_managed_stem_worker_session(owner_id)
        released = pool.reserve_exact(
            tuple(claim("6" * 32) for _ in range(4))
        )
        assert released is not None
        released.close()


@pytest.mark.external_assets
@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_builtin_sample_worker_matches_serial_float32_stem() -> None:
    executor = SimpleNamespace(
        executor_id="sample-worker",
        capability=SimpleNamespace(manifest_path=str(SAMPLE_MANIFEST)),
        override_map={},
    )
    part = SimpleNamespace(
        executor=executor,
        performance=_SAMPLE_PERFORMANCE,
    )
    serial_audio, serial_peak, serial_manifest = (
        ensemble_module._render_part(part, 8_000)
    )
    job = StemRenderJob.create(
        index=0,
        executor_id=executor.executor_id,
        manifest_path=SAMPLE_MANIFEST,
        sample_rate=8_000,
        performance=_SAMPLE_PERFORMANCE,
    )

    with tempfile.TemporaryDirectory() as temporary_directory:
        handle = try_start_stem_worker(
            job,
            scratch_directory=Path(temporary_directory),
        )
        assert handle is not None
        assert handle._warm_worker is None
        with collect_stem_worker(handle) as result:
            np.testing.assert_array_equal(result.load_audio(), serial_audio)
            assert result.peak_voices == serial_peak
            assert result.manifest_sha256 == serial_manifest


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_asset_free_builtin_reuses_pid_and_matches_serial_float32_stem(
    isolated_warm_pool: None,
) -> None:
    executor = SimpleNamespace(
        executor_id="dsp-worker",
        capability=SimpleNamespace(manifest_path=str(DSP_MANIFEST)),
        override_map={},
    )
    part = SimpleNamespace(executor=executor, performance=_SAMPLE_PERFORMANCE)
    serial_audio, serial_peak, serial_manifest = (
        ensemble_module._render_part(part, 8_000)
    )
    job = StemRenderJob.create(
        index=0,
        executor_id=executor.executor_id,
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_SAMPLE_PERFORMANCE,
    )
    assert stem_worker_module._pool_eligible_job(job)

    with tempfile.TemporaryDirectory() as temporary_directory:
        first = _try_start_warm_stem_worker(
            job,
            scratch_directory=temporary_directory,
        )
        assert first is not None and first._warm_worker is not None
        first_pid = first.process.pid
        with collect_stem_worker(first) as result:
            np.testing.assert_array_equal(result.load_audio(), serial_audio)
            assert result.peak_voices == serial_peak
            assert result.manifest_sha256 == serial_manifest

        repeated = _try_start_warm_stem_worker(
            replace(job, index=1, executor_id="dsp-worker-repeated"),
            scratch_directory=temporary_directory,
        )
        assert repeated is not None and repeated._warm_worker is not None
        assert repeated.process.pid == first_pid
        with collect_stem_worker(repeated) as result:
            np.testing.assert_array_equal(result.load_audio(), serial_audio)
            assert result.peak_voices == serial_peak
            assert result.manifest_sha256 == serial_manifest


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_bianzhong_keeps_public_one_shot_and_reuses_warm_worker_byte_exactly(
    isolated_warm_pool: None,
) -> None:
    executor = SimpleNamespace(
        executor_id="bianzhong-worker",
        capability=SimpleNamespace(manifest_path=str(BIANZHONG_MANIFEST)),
        override_map={},
    )
    part = SimpleNamespace(
        executor=executor,
        performance=_BIANZHONG_PERFORMANCE,
    )
    serial_audio, serial_peak, serial_manifest = ensemble_module._render_part(
        part,
        8_000,
    )
    job = StemRenderJob.create(
        index=0,
        executor_id=executor.executor_id,
        manifest_path=BIANZHONG_MANIFEST,
        sample_rate=8_000,
        performance=_BIANZHONG_PERFORMANCE,
    )
    assert stem_worker_module._pool_eligible_job(job)

    with tempfile.TemporaryDirectory() as temporary_directory:
        one_shot = try_start_stem_worker(
            job,
            scratch_directory=temporary_directory,
        )
        assert one_shot is not None
        assert one_shot._warm_worker is None
        with collect_stem_worker(one_shot) as result:
            np.testing.assert_array_equal(result.load_audio(), serial_audio)
            assert result.peak_voices == serial_peak
            assert result.manifest_sha256 == serial_manifest

        first = _try_start_warm_stem_worker(
            replace(job, index=1, executor_id="bianzhong-warm-first"),
            scratch_directory=temporary_directory,
        )
        assert first is not None and first._warm_worker is not None
        first_pid = first.process.pid
        with collect_stem_worker(first) as result:
            np.testing.assert_array_equal(result.load_audio(), serial_audio)
            assert result.peak_voices == serial_peak
            assert result.manifest_sha256 == serial_manifest

        repeated = _try_start_warm_stem_worker(
            replace(job, index=2, executor_id="bianzhong-warm-repeated"),
            scratch_directory=temporary_directory,
        )
        assert repeated is not None and repeated._warm_worker is not None
        assert repeated.process.pid == first_pid
        with collect_stem_worker(repeated) as result:
            np.testing.assert_array_equal(result.load_audio(), serial_audio)
            assert result.peak_voices == serial_peak
            assert result.manifest_sha256 == serial_manifest


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_failed_worker_releases_anonymous_files_and_global_permit() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        failing_manifest = _write_loud_instrument(root, fail=True)
        failing = StemRenderJob.create(
            index=0,
            executor_id="failure",
            manifest_path=failing_manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        handle = try_start_stem_worker(failing, scratch_directory=root)
        assert handle is not None
        with pytest.raises(StemWorkerError, match="deliberate worker failure"):
            collect_stem_worker(handle)

        # Replacing the implementation makes the same private directory usable
        # immediately; no named partial output survives the failed child.
        working_manifest = _write_loud_instrument(root, fail=False)
        working = StemRenderJob.create(
            index=1,
            executor_id="recovered",
            manifest_path=working_manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        recovered_handle = try_start_stem_worker(
            working,
            scratch_directory=root,
        )
        assert recovered_handle is not None
        with collect_stem_worker(recovered_handle) as result:
            assert result.frame_count == 8
        assert not any(
            path.suffix in {".f32", ".f32le", ".tmp"}
            for path in root.iterdir()
        )


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_worker_memory_exhaustion_is_not_reported_as_retryable_failure() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root, memory_error=True)
        job = StemRenderJob.create(
            index=0,
            executor_id="memory-error",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        handle = try_start_stem_worker(job, scratch_directory=root)
        assert handle is not None

        with pytest.raises(MemoryError, match="exhausted host resources"):
            collect_stem_worker(handle)


def test_manifest_change_after_job_binding_is_rejected_before_start() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root)
        job = StemRenderJob.create(
            index=0,
            executor_id="bound-manifest",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        manifest.write_text(
            json.dumps({"name": "replacement", "type": "oscillator"}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="identity changed"):
            try_start_stem_worker(job, scratch_directory=root)


def test_structural_overrides_are_rejected_in_parent_and_forged_job() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root)
        with pytest.raises(ValueError, match="structural fields"):
            StemRenderJob.create(
                index=0,
                executor_id="bad-override",
                manifest_path=manifest,
                sample_rate=8_000,
                performance=_PERFORMANCE,
                overrides={"implementation": "evil.py"},
            )

        valid = StemRenderJob.create(
            index=0,
            executor_id="forged-override",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        forged = replace(
            valid,
            overrides_json=b'{"implementation":"evil.py"}',
        )
        with pytest.raises(ValueError, match="structural fields"):
            try_start_stem_worker(forged, scratch_directory=root)


@pytest.mark.parametrize(
    ("extra_manifest", "expected"),
    [
        ({"runtime_asset_policy": "no_external_audio_assets"}, True),
        (
            {
                "provenance_kind": "project_authored_dsp",
                "external_audio_assets": [],
                "asset_root": "",
            },
            True,
        ),
        ({}, False),
        (
            {
                "runtime_asset_policy": "no_external_audio_assets",
                "asset_root": "../../../audio",
            },
            False,
        ),
        (
            {
                "runtime_asset_policy": "no_external_audio_assets",
                "implementation": None,
            },
            False,
        ),
        (
            {
                "runtime_asset_policy": "no_external_audio_assets",
                "type": "sample",
            },
            False,
        ),
    ],
)
def test_warm_pool_requires_explicit_asset_free_builtin_evidence(
    monkeypatch: pytest.MonkeyPatch,
    extra_manifest: dict[str, object],
    expected: bool,
) -> None:
    monkeypatch.setattr(
        stem_worker_module,
        "_is_trusted_managed_worker_manifest",
        lambda path, manifest: True,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = root / "instrument.json"
        document: dict[str, object] = {
            "name": "pool-policy",
            "type": "synthesizer",
            **extra_manifest,
        }
        manifest.write_text(json.dumps(document), encoding="utf-8")
        job = StemRenderJob.create(
            index=0,
            executor_id="pool-policy",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        assert stem_worker_module._pool_eligible_job(job) is expected


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_success_exit_with_truncated_protocol_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root, truncate=True)
        job = StemRenderJob.create(
            index=0,
            executor_id="truncated",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        handle = try_start_stem_worker(job, scratch_directory=root)
        assert handle is not None
        with pytest.raises(StemWorkerError, match="truncated"):
            collect_stem_worker(handle)


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_terminate_kills_child_and_releases_permit() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        hanging_manifest = _write_loud_instrument(root, hang=True)
        hanging = StemRenderJob.create(
            index=0,
            executor_id="hanging",
            manifest_path=hanging_manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        handle = try_start_stem_worker(hanging, scratch_directory=root)
        assert handle is not None
        terminate_stem_worker(handle)
        assert handle.process.poll() is not None

        working_manifest = _write_loud_instrument(root)
        working = StemRenderJob.create(
            index=1,
            executor_id="after-terminate",
            manifest_path=working_manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        recovered = try_start_stem_worker(working, scratch_directory=root)
        assert recovered is not None
        with collect_stem_worker(recovered) as result:
            assert result.frame_count == 8


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_warm_worker_reuses_pid_without_retaining_task_scratch(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    monkeypatch.setattr(stem_worker_module, "_pool_eligible_job", lambda job: True)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        lifecycle = root / "lifecycle.txt"
        manifest = _write_loud_instrument(
            root,
            lifecycle_path=lifecycle,
        )

        scratch_a = root / "scratch-a"
        scratch_a.mkdir()
        first_job = StemRenderJob.create(
            index=0,
            executor_id="warm-a",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_LONG_SILENT_PERFORMANCE,
        )
        first = _try_start_warm_stem_worker(
            first_job,
            scratch_directory=scratch_a,
        )
        assert first is not None and first._warm_worker is not None
        first_pid = first.process.pid
        with collect_stem_worker(first) as result:
            first_audio = result.load_audio().copy()
        # Windows refuses this removal if a worker retained either anonymous
        # task file after collection.
        scratch_a.rmdir()

        scratch_b = root / "scratch-b"
        scratch_b.mkdir()
        second_job = StemRenderJob.create(
            index=1,
            executor_id="warm-b",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_LONG_SILENT_PERFORMANCE,
        )
        second = _try_start_warm_stem_worker(
            second_job,
            scratch_directory=scratch_b,
        )
        assert second is not None and second._warm_worker is not None
        assert second.process.pid == first_pid
        with collect_stem_worker(second) as result:
            second_audio = result.load_audio().copy()
        scratch_b.rmdir()

        np.testing.assert_array_equal(second_audio, first_audio)
        assert first_audio.nbytes > 64 * 1024
        assert lifecycle.read_text(encoding="utf-8").splitlines() == [
            "create",
            "close",
            "create",
            "close",
        ]


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_warm_worker_retires_on_manifest_drift_and_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root)
        job = StemRenderJob.create(
            index=0,
            executor_id="manifest-drift",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        drifted = False

        def drift_after_parent_validation(candidate: StemRenderJob) -> bool:
            nonlocal drifted
            if not drifted:
                drifted = True
                manifest.write_text(
                    json.dumps({"name": "drifted", "type": "oscillator"}),
                    encoding="utf-8",
                )
            return True

        monkeypatch.setattr(
            stem_worker_module,
            "_pool_eligible_job",
            drift_after_parent_validation,
        )
        handle = _try_start_warm_stem_worker(job, scratch_directory=root)
        assert handle is not None
        failed_pid = handle.process.pid
        with pytest.raises(StemWorkerError, match="manifest identity changed"):
            collect_stem_worker(handle)
        assert handle.process.poll() is not None

        _write_loud_instrument(root)
        recovered_job = StemRenderJob.create(
            index=1,
            executor_id="after-manifest-drift",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        recovered = _try_start_warm_stem_worker(
            recovered_job,
            scratch_directory=root,
        )
        assert recovered is not None
        assert recovered.process.pid != failed_pid
        with collect_stem_worker(recovered) as result:
            assert result.frame_count == 8


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_warm_worker_retires_on_source_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    monkeypatch.setattr(stem_worker_module, "_pool_eligible_job", lambda job: True)
    original = StemRenderJob.protocol_document

    def forged_source(
        self: StemRenderJob,
        *,
        token: str,
    ) -> dict[str, object]:
        document = original(self, token=token)
        document["producer_source_tree_sha256"] = "0" * 64
        return document

    monkeypatch.setattr(StemRenderJob, "protocol_document", forged_source)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root)
        job = StemRenderJob.create(
            index=0,
            executor_id="source-drift",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        handle = _try_start_warm_stem_worker(job, scratch_directory=root)
        assert handle is not None
        with pytest.raises(StemWorkerError, match="protocol is unsupported"):
            collect_stem_worker(handle)
        assert handle.process.poll() is not None


def test_each_child_task_rehashes_live_source_tree_before_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        stem_worker_module,
        "current_source_tree_matches",
        lambda digest: False,
    )
    with pytest.raises(RuntimeError, match="source tree changed"):
        stem_worker_module._render_child(
            {},
            -1,
            verify_live_source=True,
        )


def test_ensemble_and_worker_consumers_keep_custom_instrument_on_frame_stream(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    def reject_blocks(*args: object, **kwargs: object) -> object:
        raise AssertionError("custom instrument must not enter block transport")

    executor = SimpleNamespace(
        executor_id="custom-frame-consumer",
        capability=SimpleNamespace(manifest_path=str(DSP_MANIFEST)),
        override_map={},
    )
    part = SimpleNamespace(executor=executor, performance=_PERFORMANCE)
    monkeypatch.setattr(
        ensemble_module,
        "create_instrument",
        lambda *args, **kwargs: _CustomFrameInstrument(8_000),
    )
    monkeypatch.setattr(
        ensemble_module,
        "render_document_blocks",
        reject_blocks,
    )
    audio, peak, _manifest_hash = ensemble_module._render_part(part, 8_000)
    assert peak == 0
    np.testing.assert_array_equal(
        audio,
        np.tile(np.asarray([[0.125, -0.25]], dtype=np.float32), (8, 1)),
    )

    job = StemRenderJob.create(
        index=0,
        executor_id=executor.executor_id,
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    monkeypatch.setattr(
        stem_worker_module,
        "create_instrument",
        lambda *args, **kwargs: _CustomFrameInstrument(8_000),
    )
    monkeypatch.setattr(
        stem_worker_module,
        "render_document_blocks",
        reject_blocks,
    )
    with tempfile.TemporaryFile(mode="w+b") as protocol:
        stem_worker_module._render_child(
            job.protocol_document(token="1" * 32),
            protocol.fileno(),
        )
        protocol.seek(0)
        assert protocol.read().startswith(
            stem_worker_module._BEGIN_PREFIX + b"1" * 32
        )


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_warm_worker_oom_retires_process_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    monkeypatch.setattr(stem_worker_module, "_pool_eligible_job", lambda job: True)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root, memory_error=True)
        job = StemRenderJob.create(
            index=0,
            executor_id="warm-oom",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        handle = _try_start_warm_stem_worker(job, scratch_directory=root)
        assert handle is not None
        failed_pid = handle.process.pid
        with pytest.raises(MemoryError, match="exhausted host resources"):
            collect_stem_worker(handle)
        assert handle.process.poll() is not None

        _write_loud_instrument(root)
        recovered_job = StemRenderJob.create(
            index=1,
            executor_id="after-warm-oom",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        recovered = _try_start_warm_stem_worker(
            recovered_job,
            scratch_directory=root,
        )
        assert recovered is not None
        assert recovered.process.pid != failed_pid
        with collect_stem_worker(recovered) as result:
            assert result.frame_count == 8


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_warm_cancel_kills_then_waits_and_does_not_leak_permit(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    monkeypatch.setattr(stem_worker_module, "_pool_eligible_job", lambda job: True)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root, hang=True)
        job = StemRenderJob.create(
            index=0,
            executor_id="warm-cancel",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        handle = _try_start_warm_stem_worker(job, scratch_directory=root)
        assert handle is not None
        cancelled_pid = handle.process.pid
        terminate_stem_worker(handle)
        assert handle.process.poll() is not None

        _write_loud_instrument(root)
        recovered_job = StemRenderJob.create(
            index=1,
            executor_id="after-warm-cancel",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        recovered = _try_start_warm_stem_worker(
            recovered_job,
            scratch_directory=root,
        )
        assert recovered is not None
        assert recovered.process.pid != cancelled_pid
        with collect_stem_worker(recovered) as result:
            assert result.frame_count == 8


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_two_active_warm_jobs_use_distinct_workers_and_idle_holds_no_permit(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    if stem_worker_module._GLOBAL_CAPACITY < 2:
        pytest.skip("host policy allows only one managed worker")
    monkeypatch.setattr(stem_worker_module, "_pool_eligible_job", lambda job: True)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root)
        first_job = StemRenderJob.create(
            index=0,
            executor_id="concurrent-a",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_LONG_SILENT_PERFORMANCE,
        )
        second_job = StemRenderJob.create(
            index=1,
            executor_id="concurrent-b",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_LONG_SILENT_PERFORMANCE,
        )
        first = _try_start_warm_stem_worker(
            first_job,
            scratch_directory=root,
        )
        second = _try_start_warm_stem_worker(
            second_job,
            scratch_directory=root,
        )
        assert first is not None and second is not None
        assert first.process.pid != second.process.pid
        processes = {first.process, second.process}
        pids = {first.process.pid, second.process.pid}
        with collect_stem_worker(first):
            pass
        with collect_stem_worker(second):
            pass

        acquired = 0
        try:
            for _ in range(stem_worker_module._GLOBAL_CAPACITY):
                if stem_worker_module._GLOBAL_PERMITS.acquire(blocking=False):
                    acquired += 1
            assert acquired == stem_worker_module._GLOBAL_CAPACITY
        finally:
            for _ in range(acquired):
                stem_worker_module._GLOBAL_PERMITS.release()

        third_job = StemRenderJob.create(
            index=2,
            executor_id="concurrent-c",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        third = _try_start_warm_stem_worker(
            third_job,
            scratch_directory=root,
        )
        assert third is not None
        assert third.process.pid in pids
        with collect_stem_worker(third):
            pass
        stem_worker_module._shutdown_warm_pool()
        assert not stem_worker_module._WARM_ALL
        assert not stem_worker_module._WARM_IDLE
        assert all(process.poll() is not None for process in processes)


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_warm_worker_rotates_after_bounded_job_count(
    isolated_warm_pool: None,
) -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="bounded-rotation",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        worker_objects: list[object] = []
        for index in range(stem_worker_module._WARM_MAX_JOBS + 1):
            handle = _try_start_warm_stem_worker(
                replace(job, index=index, executor_id=f"rotation-{index}"),
                scratch_directory=temporary_directory,
            )
            assert handle is not None and handle._warm_worker is not None
            worker_objects.append(handle._warm_worker)
            with collect_stem_worker(handle):
                pass
        assert all(
            worker is worker_objects[0]
            for worker in worker_objects[: stem_worker_module._WARM_MAX_JOBS]
        )
        assert worker_objects[-1] is not worker_objects[0]


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_persistent_child_exits_promptly_on_parent_eof() -> None:
    process = subprocess.Popen(
        [sys.executable, "-m", "tianlai.stem_worker", "--persistent"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(ROOT),
    )
    assert process.stdin is not None
    process.stdin.close()
    assert process.wait(timeout=3.0) == 0


def test_persistent_child_idle_timeout_is_internal_and_bounded() -> None:
    commands: queue.Queue[bytes | BaseException] = queue.Queue()
    started = time.monotonic()
    assert (
        stem_worker_module._persistent_child_loop(
            -1,
            commands=commands,
            idle_seconds=0.01,
            start_reader=False,
        )
        == 0
    )
    assert time.monotonic() - started < 1.0


def test_framed_warm_request_does_not_copy_the_protocol_buffer() -> None:
    class RecordingStream:
        def __init__(self) -> None:
            self.writes: list[memoryview] = []
            self.flushes = 0

        def write(self, payload: memoryview) -> int:
            self.writes.append(payload)
            return len(payload)

        def flush(self) -> None:
            self.flushes += 1

    stream = RecordingStream()
    protocol = b"request" * (1024 * 1024)
    stem_worker_module._write_framed_request(stream, protocol)

    assert [len(payload) for payload in stream.writes] == [4, len(protocol)]
    assert stream.writes[1].obj is protocol
    assert stream.flushes == 1


def test_persistent_reader_queues_mutable_buffer_without_full_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = b'{"request":true}'
    chunks = iter(
        (
            len(protocol).to_bytes(4, "big"),
            protocol,
            b"\x00\x00\x00\x00",
        )
    )
    monkeypatch.setattr(stem_worker_module.os, "read", lambda fd, size: next(chunks))
    commands: queue.Queue[bytes | bytearray | BaseException] = queue.Queue()

    stem_worker_module._persistent_input_reader(commands)

    request = commands.get_nowait()
    assert type(request) is bytearray
    assert request == protocol
    assert isinstance(commands.get_nowait(), ValueError)


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_diagnostic_copy_failure_cannot_block_successful_collection(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="diagnostic-copy-fault",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        with monkeypatch.context() as patcher:
            patcher.setattr(
                stem_worker_module._WarmWorker,
                "_copy_diagnostic_to_task",
                lambda self, task: (_ for _ in ()).throw(
                    MemoryError("injected diagnostic copy failure")
                ),
            )
            handle = _try_start_warm_stem_worker(
                job,
                scratch_directory=temporary_directory,
            )
            assert handle is not None and handle._warm_task is not None
            assert handle._warm_task.done.wait(timeout=5.0)
            with collect_stem_worker(handle) as result:
                assert result.frame_count == 8

        recovered = _try_start_warm_stem_worker(
            replace(job, index=1, executor_id="after-diagnostic-copy-fault"),
            scratch_directory=temporary_directory,
        )
        assert recovered is not None
        with collect_stem_worker(recovered) as result:
            assert result.frame_count == 8


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_diagnostic_drain_failure_completes_task_and_reclaims_worker(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        manifest = _write_loud_instrument(root, fail=True)
        failing = StemRenderJob.create(
            index=0,
            executor_id="diagnostic-drain-fault",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        with monkeypatch.context() as patcher:
            patcher.setattr(
                stem_worker_module,
                "_pool_eligible_job",
                lambda job: True,
            )
            patcher.setattr(
                stem_worker_module._WarmWorker,
                "_append_diagnostic",
                lambda self, chunk: (_ for _ in ()).throw(
                    MemoryError("injected diagnostic append failure")
                ),
            )
            handle = _try_start_warm_stem_worker(
                failing,
                scratch_directory=root,
            )
            assert handle is not None and handle._warm_task is not None
            failed_process = handle.process
            assert handle._warm_task.done.wait(timeout=5.0)
            with pytest.raises((MemoryError, StemWorkerError)):
                collect_stem_worker(handle)
            assert failed_process.poll() is not None

        _write_loud_instrument(root)
        recovered = StemRenderJob.create(
            index=1,
            executor_id="after-diagnostic-drain-fault",
            manifest_path=manifest,
            sample_rate=8_000,
            performance=_PERFORMANCE,
        )
        monkeypatch.setattr(
            stem_worker_module,
            "_pool_eligible_job",
            lambda job: True,
        )
        handle = _try_start_warm_stem_worker(
            recovered,
            scratch_directory=root,
        )
        assert handle is not None
        with collect_stem_worker(handle) as result:
            assert result.frame_count == 8


@pytest.mark.external_assets
@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_nonpool_admission_retires_full_idle_pool_before_spawn_and_rebuilds(
    isolated_warm_pool: None,
) -> None:
    warm_job = StemRenderJob.create(
        index=0,
        executor_id="capacity-warm",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    sample_job = StemRenderJob.create(
        index=100,
        executor_id="capacity-sample",
        manifest_path=SAMPLE_MANIFEST,
        sample_rate=8_000,
        performance=_SAMPLE_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        handles = [
            _try_start_warm_stem_worker(
                replace(
                    warm_job,
                    index=index,
                    executor_id=f"capacity-warm-{index}",
                ),
                scratch_directory=temporary_directory,
            )
            for index in range(stem_worker_module._GLOBAL_CAPACITY)
        ]
        assert all(handle is not None for handle in handles)
        warm_workers = tuple(
            handle._warm_worker
            for handle in handles
            if handle is not None
        )
        warm_processes = tuple(
            handle.process for handle in handles if handle is not None
        )
        assert len(set(warm_processes)) == stem_worker_module._GLOBAL_CAPACITY
        for handle in handles:
            assert handle is not None
            with collect_stem_worker(handle):
                pass
        assert len(stem_worker_module._WARM_IDLE) == len(warm_processes)

        sample = try_start_stem_worker(
            sample_job,
            scratch_directory=temporary_directory,
        )
        assert sample is not None and sample._warm_worker is None
        assert not stem_worker_module._WARM_IDLE
        assert not stem_worker_module._WARM_ALL
        assert all(process.poll() is not None for process in warm_processes)
        live_children = sum(
            process.poll() is None for process in (*warm_processes, sample.process)
        )
        assert live_children <= stem_worker_module._GLOBAL_CAPACITY
        with collect_stem_worker(sample):
            pass

        rebuilt = _try_start_warm_stem_worker(
            replace(warm_job, index=101, executor_id="capacity-rebuilt"),
            scratch_directory=temporary_directory,
        )
        assert rebuilt is not None and rebuilt._warm_worker is not None
        assert rebuilt._warm_worker not in warm_workers
        with collect_stem_worker(rebuilt):
            pass


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_multiworker_idle_retirement_is_prompt_and_clears_processes(
    isolated_warm_pool: None,
) -> None:
    if stem_worker_module._GLOBAL_CAPACITY < 2:
        pytest.skip("latency regression requires multiple idle workers")
    job = StemRenderJob.create(
        index=0,
        executor_id="prompt-retirement",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        handles = [
            _try_start_warm_stem_worker(
                replace(job, index=index, executor_id=f"prompt-{index}"),
                scratch_directory=temporary_directory,
            )
            for index in range(stem_worker_module._GLOBAL_CAPACITY)
        ]
        assert all(handle is not None for handle in handles)
        processes = tuple(
            handle.process for handle in handles if handle is not None
        )
        for handle in handles:
            assert handle is not None
            with collect_stem_worker(handle):
                pass

        started = time.monotonic()
        stem_worker_module.retire_idle_stem_workers()
        elapsed = time.monotonic() - started

        assert elapsed < 0.18 * len(processes) + 0.2
        assert all(process.poll() is not None for process in processes)
        assert not stem_worker_module._WARM_IDLE
        assert not stem_worker_module._WARM_ALL
        assert not stem_worker_module._WARM_QUARANTINED


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_graceful_retire_error_forces_worker_and_does_not_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="force-after-graceful-error",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        handle = _try_start_warm_stem_worker(
            job,
            scratch_directory=temporary_directory,
        )
        assert handle is not None and handle._warm_worker is not None
        worker = handle._warm_worker
        with collect_stem_worker(handle):
            pass
        original_retire = worker.retire
        calls: list[bool] = []

        def fail_graceful(*, force: bool) -> None:
            calls.append(force)
            if not force:
                raise RuntimeError("injected graceful retire failure")
            original_retire(force=True)

        monkeypatch.setattr(worker, "retire", fail_graceful)
        stem_worker_module.retire_idle_stem_workers()

        assert calls == [False, True]
        assert worker.process.poll() is not None
        assert worker not in stem_worker_module._WARM_ALL
        assert worker not in stem_worker_module._WARM_QUARANTINED


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_dead_worker_retire_error_is_removed_from_registry(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="dead-retire-error",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        handle = _try_start_warm_stem_worker(
            job,
            scratch_directory=temporary_directory,
        )
        assert handle is not None and handle._warm_worker is not None
        worker = handle._warm_worker
        with collect_stem_worker(handle):
            pass
        original_retire = worker.retire

        def retire_then_raise(*, force: bool) -> None:
            original_retire(force=True)
            raise RuntimeError("injected post-reap failure")

        monkeypatch.setattr(worker, "retire", retire_then_raise)
        stem_worker_module.retire_idle_stem_workers()

        assert worker.process.poll() is not None
        assert worker not in stem_worker_module._WARM_ALL
        assert worker not in stem_worker_module._WARM_QUARANTINED


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_live_retire_failure_quarantines_all_later_child_admission(
    monkeypatch: pytest.MonkeyPatch,
    isolated_warm_pool: None,
) -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="live-retire-error",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        handle = _try_start_warm_stem_worker(
            job,
            scratch_directory=temporary_directory,
        )
        assert handle is not None and handle._warm_worker is not None
        worker = handle._warm_worker
        with collect_stem_worker(handle):
            pass
        original_retire = worker.retire

        try:
            with monkeypatch.context() as patcher:
                calls: list[bool] = []

                def never_retire(*, force: bool) -> None:
                    calls.append(force)
                    raise RuntimeError("injected live retire failure")

                patcher.setattr(worker, "retire", never_retire)
                with pytest.raises(StemWorkerError, match="quarantined"):
                    stem_worker_module.retire_idle_stem_workers()
                assert calls == [False, True]
                assert worker.process.poll() is None
                assert worker in stem_worker_module._WARM_QUARANTINED

                original_popen = stem_worker_module.subprocess.Popen
                popen_calls = 0

                def track_popen(*args: object, **kwargs: object) -> object:
                    nonlocal popen_calls
                    popen_calls += 1
                    return original_popen(*args, **kwargs)

                patcher.setattr(
                    stem_worker_module.subprocess,
                    "Popen",
                    track_popen,
                )
                with pytest.raises(StemWorkerError, match="refusing to start"):
                    try_start_stem_worker(
                        replace(job, index=1, executor_id="blocked-by-quarantine"),
                        scratch_directory=temporary_directory,
                    )
                assert popen_calls == 0
        finally:
            stem_worker_module._shutdown_warm_pool()

        assert worker.process.poll() is not None
        assert not stem_worker_module._WARM_ALL
        assert not stem_worker_module._WARM_QUARANTINED


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_private_final_batch_hint_reuses_idle_without_starting_new_warm_worker(
    isolated_warm_pool: None,
) -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="warm-start-hint",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        single_batch = stem_worker_module._try_start_stem_worker(
            job,
            scratch_directory=temporary_directory,
            allow_warm_start=False,
            allow_warm_reuse=True,
        )
        assert single_batch is not None
        assert single_batch._warm_worker is None
        with collect_stem_worker(single_batch):
            pass

        starter = _try_start_warm_stem_worker(
            replace(job, index=1, executor_id="warm-start-allowed"),
            scratch_directory=temporary_directory,
        )
        assert starter is not None and starter._warm_worker is not None
        worker = starter._warm_worker
        pid = starter.process.pid
        with collect_stem_worker(starter):
            pass

        reuse_only = stem_worker_module._try_start_stem_worker(
            replace(job, index=2, executor_id="warm-reuse-only"),
            scratch_directory=temporary_directory,
            allow_warm_start=False,
            allow_warm_reuse=True,
        )
        assert reuse_only is not None
        assert reuse_only._warm_worker is worker
        assert reuse_only.process.pid == pid
        with collect_stem_worker(reuse_only):
            pass


def test_public_start_signature_has_no_warm_pool_setting() -> None:
    parameters = inspect.signature(try_start_stem_worker).parameters
    assert tuple(parameters) == ("job", "scratch_directory")
    assert parameters["job"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert parameters["scratch_directory"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_public_start_remains_one_shot_even_when_warm_worker_is_idle(
    isolated_warm_pool: None,
) -> None:
    job = StemRenderJob.create(
        index=0,
        executor_id="public-one-shot-lifecycle",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        warm = _try_start_warm_stem_worker(
            job,
            scratch_directory=temporary_directory,
        )
        assert warm is not None and warm._warm_worker is not None
        warm_process = warm.process
        with collect_stem_worker(warm):
            pass
        assert warm_process.poll() is None

        public = try_start_stem_worker(
            replace(job, index=1, executor_id="public-one-shot-after-idle"),
            scratch_directory=temporary_directory,
        )
        assert public is not None and public._warm_worker is None
        assert warm_process.poll() is not None
        public_process = public.process
        with collect_stem_worker(public):
            pass
        assert public_process.poll() is not None
        assert not stem_worker_module._WARM_IDLE
        assert not stem_worker_module._WARM_ALL


@pytest.mark.parametrize("stale_is_newer", [True, False])
@pytest.mark.skipif(
    not managed_subprocess_workers_available(),
    reason="managed module subprocess is unavailable",
)
def test_checkout_retires_stale_workers_on_either_side_of_live_selection(
    stale_is_newer: bool,
    isolated_warm_pool: None,
) -> None:
    if stem_worker_module._GLOBAL_CAPACITY < 2:
        pytest.skip("stale-before-live checkout requires two workers")
    job = StemRenderJob.create(
        index=0,
        executor_id="stale-checkout",
        manifest_path=DSP_MANIFEST,
        sample_rate=8_000,
        performance=_PERFORMANCE,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        first = _try_start_warm_stem_worker(
            job,
            scratch_directory=temporary_directory,
        )
        second = _try_start_warm_stem_worker(
            replace(job, index=1, executor_id="stale-checkout-newer"),
            scratch_directory=temporary_directory,
        )
        assert first is not None and first._warm_worker is not None
        assert second is not None and second._warm_worker is not None
        older = first._warm_worker
        newer = second._warm_worker
        with collect_stem_worker(first):
            pass
        with collect_stem_worker(second):
            pass
        assert stem_worker_module._WARM_IDLE == [older, newer]
        stale = newer if stale_is_newer else older
        expected = older if stale_is_newer else newer
        with stale._condition:
            stale._retired = True
            stale._condition.notify_all()

        selected = stem_worker_module._checkout_warm_worker(
            allow_start=False,
        )
        try:
            assert selected is expected
            assert stale.process.poll() is not None
            assert stale not in stem_worker_module._WARM_ALL
            assert stale not in stem_worker_module._WARM_IDLE
        finally:
            if selected is not None:
                stem_worker_module._discard_warm_worker(
                    selected,
                    force=True,
                )
