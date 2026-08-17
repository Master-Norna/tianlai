from __future__ import annotations

import errno
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from typing import Any, Iterator

import numpy as np
import pytest

from tianlai import adaptive_runtime as adaptive_runtime_module
from tianlai import collaboration_report as collaboration_report_module
from tianlai import ensemble as ensemble_module
from tianlai import space as space_module
from tianlai import stem_cache as stem_cache_module
from tianlai import stem_worker as stem_worker_module
from tianlai import worker_slots as worker_slots_module
from tianlai.ensemble import render_plan
from tianlai.roster import CollaborationSettings
from tianlai.space import SpaceConfig
from tianlai.stem_cache import StemCache, VerifiedStemSource
from tianlai.stem_worker import StemWorkerResult
from tianlai.worker_slots import WorkerSlotPool


_LONG_FRAME_COUNT = 65_536 + 257
_MANIFEST_SHA256 = "b" * 64


@pytest.fixture(autouse=True)
def _isolated_verified_snapshot_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    managed_slot_directory = tmp_path / "managed-worker-slots"
    monkeypatch.setattr(
        worker_slots_module,
        "default_worker_slot_directory",
        lambda: managed_slot_directory,
    )
    pool = WorkerSlotPool(tmp_path / "verified-snapshot-slots")
    monkeypatch.setattr(
        stem_cache_module,
        "_verified_snapshot_pool_factory",
        lambda: pool,
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


class _PipelinePlan:
    def __init__(
        self,
        root: Path,
        *,
        frame_count: int,
        part_count: int,
        gain_envelopes: tuple[tuple[Any, ...], ...] | None = None,
    ) -> None:
        self.sample_rate = 8_000
        self.duration_seconds = frame_count / self.sample_rate
        self.audio_by_executor: dict[str, np.ndarray] = {}
        parts: list[Any] = []
        if gain_envelopes is None:
            gain_envelopes = ((),) * part_count
        for index in range(part_count):
            executor_id = f"stream-part-{index}"
            manifest = root / f"instrument-{index}.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(
                json.dumps(
                    {
                        "name": f"Streaming instrument {index}",
                        "upstream": "project test fixture",
                        "creator": "Tianlai tests",
                        "origin": "https://example.invalid/streaming",
                        "license": "CC0-1.0",
                        "license_status": "approved",
                        "provenance_kind": "project_authored_dsp",
                        "implementation_license": "Apache-2.0",
                        "external_audio_assets": [],
                        "audio_asset_license": "not_applicable",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            capability = SimpleNamespace(
                manifest_path=str(manifest),
                relative_path=f"tests/streaming-{index}",
                quality_tier="formal",
                collaboration_review_status="untested",
                license_status="approved",
            )
            executor = SimpleNamespace(
                executor_id=executor_id,
                part_id=f"part-{index}",
                capability=capability,
                override_map={},
                gain_db=-9.0 - index,
                pan=(-0.35 if index % 2 == 0 else 0.45),
                seat=SimpleNamespace(distance_m=2.5 + index),
                role=None,
            )
            performance = {
                "sample_rate": self.sample_rate,
                "channels": 2,
                "duration_seconds": self.duration_seconds,
                "tail_seconds": 0.0,
                "events": [],
            }
            parts.append(
                SimpleNamespace(
                    executor=executor,
                    performance=performance,
                    gain_envelope=gain_envelopes[index],
                )
            )

            time = np.arange(frame_count, dtype=np.float64) / self.sample_rate
            phase = 0.31 * index
            left = (
                0.075 * np.sin(2.0 * np.pi * (173.0 + 19.0 * index) * time + phase)
                + 0.012 * np.cos(2.0 * np.pi * 37.0 * time)
            )
            right = (
                0.068 * np.sin(2.0 * np.pi * (211.0 + 23.0 * index) * time - phase)
                - 0.009 * np.cos(2.0 * np.pi * 29.0 * time)
            )
            self.audio_by_executor[executor_id] = np.array(
                np.column_stack((left, right)),
                dtype="<f4",
                order="C",
                copy=True,
            )
        self.parts = tuple(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": "streaming pipeline integration",
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "parts": [
                {
                    "executor_id": part.executor.executor_id,
                    "part_id": part.executor.part_id,
                    "instrument": part.executor.capability.relative_path,
                    "gain_db": part.executor.gain_db,
                    "pan": part.executor.pan,
                    "performance": part.performance,
                    "gain_envelope": [
                        {
                            "time_seconds": point.time_seconds,
                            "offset_db": point.offset_db,
                        }
                        for point in part.gain_envelope
                    ],
                }
                for part in self.parts
            ],
        }


class _FakeSpace:
    def to_dict(self) -> dict[str, Any]:
        return {"name": "streaming-test-hall", "wet_db": -20.0}

    def effective_filter_frequencies(
        self,
        sample_rate: int,
    ) -> tuple[float, float]:
        return 100.0, min(3_000.0, sample_rate * 0.49)

    def tail_seconds(self, sample_rate: int) -> float:
        del sample_rate
        return 0.025

    def send_scale(self, distance_m: float) -> float:
        return 0.3 + distance_m * 0.01


class _EventDrivenFrameInstrument:
    """Custom frame backend whose output changes at scheduled events."""

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.sample_index = 0
        self.active_notes: set[int] = set()
        self.control_value = 0.5
        self.events: list[tuple[int, str, tuple[tuple[str, Any], ...]]] = []
        self.closed = False

    def handle_event(self, event: Any, tuning: Any) -> None:
        del tuning
        self.events.append(
            (
                int(event.sample),
                str(event.type),
                tuple(sorted(event.payload.items())),
            )
        )
        if event.type == "note_on":
            self.active_notes.add(int(event.payload["note_id"]))
        elif event.type == "note_off":
            self.active_notes.remove(int(event.payload["note_id"]))
        elif event.type == "control":
            self.control_value = float(event.payload["value"])

    @property
    def active_voice_count(self) -> int:
        return len(self.active_notes)

    def render_frame(self) -> tuple[float, float]:
        # Keep every value comfortably finite/in range while making the event
        # state and the absolute frame index observable in the exact f32 bytes.
        saw = float((self.sample_index % 257) - 128) * 0.00001
        voices = float(len(self.active_notes)) * 0.0125
        control = self.control_value * 0.003
        self.sample_index += 1
        return saw + voices + control, -saw + voices - control

    def close(self) -> None:
        self.closed = True


class _ArrayFrameInstrument:
    """Expose one known f32 stereo array through the custom frame contract."""

    def __init__(self, audio: np.ndarray) -> None:
        self.audio = np.asarray(audio, dtype="<f4", order="C")
        self.index = 0
        self.closed = False

    def handle_event(self, event: Any, tuning: Any) -> None:
        del event, tuning
        raise AssertionError("the array fixture declares no events")

    @property
    def active_voice_count(self) -> int:
        return 2

    def render_frame(self) -> tuple[float, float]:
        frame = self.audio[self.index]
        self.index += 1
        return float(frame[0]), float(frame[1])

    def close(self) -> None:
        self.closed = True


class _FakeStemSource:
    """Small protocol-faithful source used to keep managed tests in-process."""

    def __init__(
        self,
        index: int,
        audio: np.ndarray,
        events: list[tuple[Any, ...]],
        *,
        forbid_materialise: bool,
    ) -> None:
        self.index = index
        self._audio = np.array(audio, dtype="<f4", order="C", copy=True)
        self._events = events
        self._forbid_materialise = forbid_materialise
        self._closed = False
        self._consumed = False
        self._verified = False
        self.materialise_calls = 0

    @property
    def frame_count(self) -> int:
        return int(self._audio.shape[0])

    @property
    def shape(self) -> tuple[int, int]:
        return (self.frame_count, 2)

    @property
    def audio_sha256(self) -> str:
        return _sha256_bytes(self._audio.tobytes(order="C"))

    @property
    def closed(self) -> bool:
        return self._closed

    def iter_blocks(self, block_frames: int = 65_536) -> Iterator[np.ndarray]:
        if self._closed:
            raise ValueError("fake stem source is closed")
        if self._consumed:
            raise ValueError("fake stem source is single-consumer")
        if not 1 <= block_frames <= 65_536:
            raise ValueError("invalid block size")
        self._consumed = True
        for start in range(0, self.frame_count, block_frames):
            stop = min(self.frame_count, start + block_frames)
            block = self._audio[start:stop].view()
            block.setflags(write=False)
            self._events.append(("source-block", self.index, start, stop))
            yield block
        self._verified = True
        self._events.append(("source-verified", self.index))

    def materialise(self) -> np.ndarray:
        self.materialise_calls += 1
        self._events.append(("source-materialise", self.index))
        if self._forbid_materialise:
            raise AssertionError("manual managed stem was materialised")
        blocks = list(self.iter_blocks())
        if not blocks:
            return np.empty((0, 2), dtype="<f4")
        return np.array(
            np.concatenate(blocks, axis=0),
            dtype="<f4",
            order="C",
            copy=True,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._events.append(("source-close", self.index, self._verified))
        self._events.append(("lease-release", self.index, self._verified))


class _RecordingTransaction:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self.events = events
        self.digest = hashlib.sha256()
        self.frame_count = 0
        self.committed = False
        self.aborted = False

    def append(self, block: np.ndarray) -> None:
        self.events.append(("cache-append", int(block.shape[0])))
        self.digest.update(memoryview(block).cast("B"))
        self.frame_count += int(block.shape[0])

    def finish(self, frame_count: int, audio_sha256: str) -> Any:
        self.events.append(("cache-finish",))
        assert not self.aborted
        assert self.frame_count == frame_count
        assert self.digest.hexdigest() == audio_sha256
        self.committed = True
        return SimpleNamespace(status="stored")

    def abort(self) -> None:
        if self.committed or self.aborted:
            return
        self.aborted = True
        self.events.append(("cache-abort",))


class _TransactionProxy:
    def __init__(
        self,
        inner: Any,
        index: int,
        events: list[tuple[Any, ...]],
    ) -> None:
        self._inner = inner
        self._index = index
        self._events = events

    def append(self, block: np.ndarray) -> None:
        self._events.append(
            ("cache-append", self._index, int(block.shape[0]))
        )
        self._inner.append(block)

    def finish(self, frame_count: int, audio_sha256: str) -> Any:
        self._events.append(("cache-finish", self._index))
        return self._inner.finish(frame_count, audio_sha256)

    def abort(self) -> None:
        self._events.append(("cache-abort", self._index))
        self._inner.abort()


class _FakeWarmWorker:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self._events = events

    def release_task(self, task: object, *, success: bool) -> None:
        del task
        self._events.append(("warm-release-task", success))


class _FakeSlot:
    def __init__(self, events: list[tuple[Any, ...]]) -> None:
        self._events = events
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._events.append(("slot-close",))


@pytest.fixture
def isolated_pipeline_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        adaptive_runtime_module,
        "_get_process_advisor",
        lambda: None,
    )
    monkeypatch.setattr(
        ensemble_module,
        "current_source_tree_matches",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        ensemble_module,
        "compute_runtime_fingerprint",
        lambda *args, **kwargs: {
            "runtime": "stable-streaming-test",
            "runtime_asset_graph": {"file_count": 0},
        },
    )
    monkeypatch.setattr(
        ensemble_module,
        "retire_idle_stem_workers",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_retire_managed_stem_worker_session",
        lambda *args, **kwargs: None,
    )


def _parallelism(plan: _PipelinePlan, workers: int) -> Any:
    hashes = tuple(
        _sha256_file(Path(part.executor.capability.manifest_path))
        for part in plan.parts
    )
    count_by_part = (
        (workers,) * len(plan.parts)
        if workers > 1
        else (1,) * len(plan.parts)
    )
    return ensemble_module._AutomaticStemParallelism(
        workers,
        count_by_part,
        hashes,
        (1_048_576,) * len(plan.parts),
        (False,) * len(plan.parts),
        ("",) * len(plan.parts),
        (0,) * len(plan.parts),
    )


def _select_workers(
    monkeypatch: pytest.MonkeyPatch,
    workers: int,
) -> None:
    monkeypatch.setattr(
        ensemble_module,
        "_automatic_stem_parallelism",
        lambda plan, **kwargs: _parallelism(plan, workers),
    )
    if workers > 1:
        monkeypatch.setattr(
            ensemble_module,
            "_automatic_worker_slot_context",
            lambda *args, **kwargs: SimpleNamespace(owner_id="a" * 32),
        )


def _fake_render(plan: _PipelinePlan):
    def render(part: Any, sample_rate: int) -> tuple[np.ndarray, int, str]:
        assert sample_rate == plan.sample_rate
        return (
            plan.audio_by_executor[part.executor.executor_id].copy(),
            2,
            _sha256_file(Path(part.executor.capability.manifest_path)),
        )

    return render


def _install_fake_managed_batch(
    monkeypatch: pytest.MonkeyPatch,
    plan: _PipelinePlan,
    events: list[tuple[Any, ...]],
    *,
    forbid_materialise: bool,
) -> tuple[list[_FakeStemSource], list[tuple[int, ...]]]:
    created: list[_FakeStemSource] = []
    batches: list[tuple[int, ...]] = []

    def batch(jobs: tuple[Any, ...], **kwargs: Any) -> Iterator[Any]:
        del kwargs
        batches.append(tuple(job.index for job in jobs))
        for job in jobs:
            part = plan.parts[job.index]
            source = _FakeStemSource(
                job.index,
                plan.audio_by_executor[part.executor.executor_id],
                events,
                forbid_materialise=forbid_materialise,
            )
            created.append(source)
            yield (
                job.index,
                source,
                2,
                _sha256_file(Path(part.executor.capability.manifest_path)),
            )

    monkeypatch.setattr(ensemble_module, "_iter_managed_stem_batch", batch)
    return created, batches


def _public_artifacts(result: Any) -> dict[str, bytes]:
    paths: dict[str, Path] = {
        "mix": Path(result.mix_path),
        "receipt": Path(result.receipt_path),
        "license-json": Path(result.license_sidecar_path),
        "license-text": Path(result.attribution_path),
        "plan": Path(result.plan_path),
        "post-render-check": Path(result.post_render_check_path),
    }
    for index, stem in enumerate(result.stems):
        assert stem.path is not None
        paths[f"stem-{index}"] = Path(stem.path)
    if result.mix_report_path is not None:
        paths["mix-report"] = Path(result.mix_report_path)
    return {name: path.read_bytes() for name, path in paths.items()}


def _fake_hall_renderer(events: list[tuple[Any, ...]]):
    def render(
        left: np.ndarray,
        right: np.ndarray,
        sample_rate: int,
        space: Any,
    ) -> tuple[np.ndarray, np.ndarray]:
        del space
        events.append(("hall-render", sample_rate, len(left)))
        left64 = np.asarray(left, dtype=np.float64)
        right64 = np.asarray(right, dtype=np.float64)
        return (
            left64 * 0.11 + right64 * 0.015,
            right64 * 0.11 + left64 * 0.015,
        )

    return render


def _detached_worker_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audio: np.ndarray,
    events: list[tuple[Any, ...]],
    *,
    lease_kind: str,
) -> tuple[Any, Any]:
    payload = np.asarray(audio, dtype="<f4", order="C").tobytes(order="C")
    prefix = b"worker-prefix"
    suffix = b"worker-suffix"
    handle = tempfile.TemporaryFile(mode="w+b", dir=tmp_path)
    handle.write(prefix)
    handle.write(payload)
    handle.write(suffix)
    handle.flush()

    worker: Any | None = None
    task: Any | None = None
    slot: Any | None = None
    if lease_kind == "warm":
        worker = _FakeWarmWorker(events)
        task = object()

        def return_worker(actual_worker: Any, actual_task: Any) -> None:
            assert actual_worker is worker
            assert actual_task is task
            events.append(("warm-return",))

        def discard_worker(actual_worker: Any, *, force: bool) -> None:
            assert actual_worker is worker
            assert force is True
            events.append(("warm-discard",))

        monkeypatch.setattr(
            stem_worker_module,
            "_return_warm_worker",
            return_worker,
        )
        monkeypatch.setattr(
            stem_worker_module,
            "_discard_warm_worker",
            discard_worker,
        )
    elif lease_kind == "slot":
        slot = _FakeSlot(events)
    else:
        raise AssertionError(f"unknown lease kind: {lease_kind}")

    result = StemWorkerResult(
        index=0,
        executor_id="worker-source",
        sample_rate=8_000,
        frame_count=int(audio.shape[0]),
        peak_voices=2,
        manifest_sha256=_MANIFEST_SHA256,
        audio_sha256=_sha256_bytes(payload),
        audio_file=handle,
        audio_offset=len(prefix),
        byte_count=len(payload),
        _worker_slot=slot,
        _warm_worker=worker,
        _warm_task=task,
        _warm_used=lease_kind == "warm",
    )
    source = result.detach_source(
        completion_callback=lambda succeeded: events.append(
            ("completion", succeeded)
        )
    )
    return source, handle


def test_serial_source_renderer_is_exact_across_block_boundary_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    sample_rate = 8_000
    frame_count = 65_536 + 257
    manifest_path = tmp_path / "event-instrument.json"
    manifest_path.write_text(
        json.dumps({"name": "event-driven source A/B"}, sort_keys=True),
        encoding="utf-8",
    )
    performance = {
        "sample_rate": sample_rate,
        "channels": 2,
        "duration_seconds": frame_count / sample_rate,
        "tail_seconds": 0.0,
        "events": [
            {
                "time": 0.0,
                "type": "note_on",
                "note_id": 1,
                "midi_note": 60,
                "velocity": 0.7,
            },
            {
                "time": 65_534 / sample_rate,
                "type": "note_on",
                "note_id": 2,
                "midi_note": 67,
                "velocity": 0.6,
            },
            {
                "time": 65_535 / sample_rate,
                "type": "control",
                "name": "expression",
                "value": 0.25,
            },
            {
                "time": 65_536 / sample_rate,
                "type": "note_off",
                "note_id": 1,
            },
            {
                "time": 65_537 / sample_rate,
                "type": "note_off",
                "note_id": 2,
            },
        ],
    }
    part = SimpleNamespace(
        executor=SimpleNamespace(
            executor_id="event-source-ab",
            capability=SimpleNamespace(manifest_path=str(manifest_path)),
            override_map={},
        ),
        performance=performance,
    )
    instruments: list[_EventDrivenFrameInstrument] = []

    def create_instrument(
        manifest: dict[str, Any],
        actual_sample_rate: int,
        *,
        base_directory: str,
    ) -> _EventDrivenFrameInstrument:
        assert manifest == {"name": "event-driven source A/B"}
        assert actual_sample_rate == sample_rate
        assert Path(base_directory) == manifest_path.parent
        instrument = _EventDrivenFrameInstrument(actual_sample_rate)
        instruments.append(instrument)
        return instrument

    monkeypatch.setattr(
        ensemble_module,
        "create_instrument",
        create_instrument,
    )

    buffered, buffered_peak, buffered_manifest = (
        ensemble_module._ORIGINAL_RENDER_PART(part, sample_rate)
    )
    source, source_peak, source_manifest = ensemble_module._render_part_source(
        part,
        sample_rate,
        scratch_directory=tmp_path,
    )

    assert isinstance(source, ensemble_module.StemBlockSource)
    assert source.shape == (frame_count, 2)
    assert not source.closed
    blocks = tuple(source.iter_blocks(65_536))
    assert [int(block.shape[0]) for block in blocks] == [65_536, 257]
    assert all(not block.flags.writeable for block in blocks)
    streamed = np.concatenate(blocks, axis=0)
    expected_payload = np.asarray(
        buffered,
        dtype="<f4",
        order="C",
    ).tobytes(order="C")
    assert streamed.tobytes(order="C") == expected_payload
    assert source.audio_sha256 == _sha256_bytes(expected_payload)
    source.close()

    expected_manifest = _sha256_file(manifest_path)
    assert buffered_peak == source_peak == 2
    assert buffered_manifest == source_manifest == expected_manifest
    assert len(instruments) == 2
    assert instruments[0].events == instruments[1].events
    assert [event[0] for event in instruments[0].events] == [
        0,
        65_534,
        65_535,
        65_536,
        65_537,
    ]
    assert all(instrument.sample_index == frame_count for instrument in instruments)
    assert all(instrument.closed for instrument in instruments)
    assert source.closed


def test_forced_serial_source_cache_miss_tees_then_reuses_publicly_identical_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=8_193,
        part_count=1,
    )
    _select_workers(monkeypatch, 1)
    assert ensemble_module._render_part is ensemble_module._ORIGINAL_RENDER_PART
    monkeypatch.setattr(ensemble_module, "_DIRECT_SERIAL_STEM_LOAD_BYTES", 0)
    monkeypatch.setattr(ensemble_module, "_DIRECT_STEM_CACHE_LOAD_BYTES", 0)
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 50),
    )

    instruments: list[_ArrayFrameInstrument] = []
    audio = plan.audio_by_executor["stream-part-0"]

    def create_instrument(
        manifest: dict[str, Any],
        sample_rate: int,
        *,
        base_directory: str,
    ) -> _ArrayFrameInstrument:
        del manifest, base_directory
        assert sample_rate == plan.sample_rate
        instrument = _ArrayFrameInstrument(audio)
        instruments.append(instrument)
        return instrument

    monkeypatch.setattr(
        ensemble_module,
        "create_instrument",
        create_instrument,
    )

    source_calls: list[str] = []
    original_source_renderer = ensemble_module._render_part_source

    def tracked_source_renderer(
        part: Any,
        sample_rate: int,
        *,
        scratch_directory: Path,
    ) -> Any:
        rendered = original_source_renderer(
            part,
            sample_rate,
            scratch_directory=scratch_directory,
        )
        assert isinstance(rendered[0], ensemble_module.StemBlockSource)
        source_calls.append(part.executor.executor_id)
        return rendered

    monkeypatch.setattr(
        ensemble_module,
        "_render_part_source",
        tracked_source_renderer,
    )

    cache_events: list[tuple[Any, ...]] = []
    original_begin = StemCache.begin_streaming_store

    def recording_begin(
        cache: StemCache,
        key: str,
        **kwargs: Any,
    ) -> _TransactionProxy:
        cache_events.append(("cache-begin", key))
        return _TransactionProxy(
            original_begin(cache, key, **kwargs),
            0,
            cache_events,
        )

    monkeypatch.setattr(StemCache, "begin_streaming_store", recording_begin)
    cache_directory = tmp_path / "cache"
    cold = render_plan(
        plan,
        tmp_path / "cold-source",
        stem_cache_directory=cache_directory,
    )
    hot = render_plan(
        plan,
        tmp_path / "hot-source",
        stem_cache_directory=cache_directory,
    )

    assert source_calls == ["stream-part-0"]
    assert len(instruments) == 1
    assert instruments[0].index == audio.shape[0]
    assert instruments[0].closed
    assert [event[0] for event in cache_events].count("cache-begin") == 1
    assert sum(
        int(event[2])
        for event in cache_events
        if event[0] == "cache-append"
    ) == audio.shape[0]
    assert ("cache-finish", 0) in cache_events
    assert cold.stem_cache["misses"] == 1
    assert cold.stem_cache["writes"] == 1
    assert cold.stem_cache["write_failures"] == 0
    assert hot.stem_cache["hits"] == 1
    assert hot.stem_cache["misses"] == 0
    assert _public_artifacts(hot) == _public_artifacts(cold)


def test_long_serial_cache_stream_is_byte_identical_with_gain_pan_hall_and_stems(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    envelope = (
        SimpleNamespace(time_seconds=0.0, offset_db=-3.0),
        SimpleNamespace(time_seconds=3.2, offset_db=1.75),
        SimpleNamespace(time_seconds=7.8, offset_db=-1.25),
    )
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=_LONG_FRAME_COUNT,
        part_count=1,
        gain_envelopes=(envelope,),
    )
    _select_workers(monkeypatch, 1)
    render_calls: list[str] = []
    render = _fake_render(plan)

    def tracked_render(part: Any, sample_rate: int) -> Any:
        render_calls.append(part.executor.executor_id)
        return render(part, sample_rate)

    monkeypatch.setattr(ensemble_module, "_render_part", tracked_render)
    hall_events: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        space_module,
        "render_reverb_stereo",
        _fake_hall_renderer(hall_events),
    )
    cache = tmp_path / "cache"
    cold = render_plan(
        plan,
        tmp_path / "cold",
        space=_FakeSpace(),
        stem_cache_directory=cache,
    )

    opened: list[VerifiedStemSource] = []
    original_open = StemCache.open_verified

    def tracking_open(cache_object: StemCache, key: str, **kwargs: Any) -> Any:
        lookup = original_open(cache_object, key, **kwargs)
        if lookup.source is not None:
            opened.append(lookup.source)
        return lookup

    monkeypatch.setattr(StemCache, "open_verified", tracking_open)
    monkeypatch.setattr(ensemble_module, "_DIRECT_STEM_CACHE_LOAD_BYTES", 0)
    monkeypatch.setattr(
        VerifiedStemSource,
        "materialise",
        lambda self: (_ for _ in ()).throw(
            AssertionError("long manual cache hit was materialised")
        ),
    )
    hot = render_plan(
        plan,
        tmp_path / "hot",
        space=_FakeSpace(),
        stem_cache_directory=cache,
    )

    assert render_calls == ["stream-part-0"]
    assert cold.stem_cache["misses"] == 1
    assert cold.stem_cache["writes"] == 1
    assert hot.stem_cache["hits"] == 1
    assert opened and all(source.closed for source in opened)
    assert len(hall_events) == 2
    assert _public_artifacts(hot) == _public_artifacts(cold)


def test_fake_managed_no_cache_never_materialises_and_matches_serial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=65_536 + 31,
        part_count=2,
    )
    _select_workers(monkeypatch, 1)
    monkeypatch.setattr(ensemble_module, "_render_part", _fake_render(plan))
    serial = render_plan(plan, tmp_path / "serial")

    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=True,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("fake managed render unexpectedly fell back")
        ),
    )
    managed = render_plan(plan, tmp_path / "managed")

    assert batches == [(0, 1)]
    assert len(sources) == 2
    assert all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert _public_artifacts(managed) == _public_artifacts(serial)


@pytest.mark.parametrize("lease_kind", ("warm", "slot"))
def test_verified_source_releases_worker_or_slot_before_cache_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lease_kind: str,
) -> None:
    audio = np.linspace(-0.2, 0.2, 129 * 2, dtype=np.float32).reshape(129, 2)
    events: list[tuple[Any, ...]] = []
    source, handle = _detached_worker_source(
        tmp_path,
        monkeypatch,
        audio,
        events,
        lease_kind=lease_kind,
    )
    transaction = _RecordingTransaction(events)
    wrapped = ensemble_module._StreamedRawStem(
        source,
        transaction=transaction,
        finish_cache=lambda: transaction.finish(
            source.frame_count,
            source.audio_sha256,
        ),
    )
    bus = np.zeros((source.frame_count, 2), dtype=np.float64)
    write_evidence: list[Any] = []

    ensemble_module._consume_streamed_raw_stem(
        wrapped,
        sample_rate=8_000,
        base_gain_db=-3.0,
        gain_envelope=(),
        bus=bus,
        send_bus=None,
        total_frames=source.frame_count,
        left_gain=0.8,
        right_gain=1.0,
        send_scale=None,
        stem_target=tmp_path / f"{lease_kind}.wav",
        stem_evidence_sink=write_evidence.append,
    )

    lease_event = ("warm-return",) if lease_kind == "warm" else ("slot-close",)
    assert events.index(lease_event) < events.index(("completion", True))
    assert events.index(("completion", True)) < events.index(("cache-finish",))
    assert transaction.committed
    assert not transaction.aborted
    assert len(write_evidence) == 1
    assert write_evidence[0] is not None
    assert source.closed
    assert handle.closed


@pytest.mark.parametrize(
    ("failure_site", "lease_kind"),
    (("wav", "warm"), ("mix", "slot")),
)
def test_stream_failure_aborts_cache_and_retires_worker_or_releases_slot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_site: str,
    lease_kind: str,
) -> None:
    audio = np.linspace(-0.15, 0.15, 257 * 2, dtype=np.float32).reshape(257, 2)
    events: list[tuple[Any, ...]] = []
    source, handle = _detached_worker_source(
        tmp_path,
        monkeypatch,
        audio,
        events,
        lease_kind=lease_kind,
    )
    transaction = _RecordingTransaction(events)
    wrapped = ensemble_module._StreamedRawStem(
        source,
        transaction=transaction,
        finish_cache=lambda: transaction.finish(
            source.frame_count,
            source.audio_sha256,
        ),
    )
    stem_target: Path | None = None
    if failure_site == "wav":
        stem_target = tmp_path / "failed-stem.wav"

        def failing_writer(
            path: Path,
            blocks: Iterator[np.ndarray],
            sample_rate: int,
        ) -> int:
            del path, sample_rate
            next(iter(blocks))
            raise OSError("injected WAV failure")

        monkeypatch.setattr(
            ensemble_module,
            "write_wav_pcm24_blocks",
            failing_writer,
        )
    else:
        monkeypatch.setattr(
            ensemble_module,
            "_accumulate_stem",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected mix failure")
            ),
        )

    with pytest.raises(OSError, match=f"injected {failure_site.upper() if failure_site == 'wav' else failure_site} failure"):
        ensemble_module._consume_streamed_raw_stem(
            wrapped,
            sample_rate=8_000,
            base_gain_db=0.0,
            gain_envelope=(),
            bus=np.zeros((source.frame_count, 2), dtype=np.float64),
            send_bus=None,
            total_frames=source.frame_count,
            left_gain=1.0,
            right_gain=1.0,
            send_scale=None,
            stem_target=stem_target,
        )

    assert transaction.aborted
    assert not transaction.committed
    assert ("cache-finish",) not in events
    assert ("completion", False) in events
    if lease_kind == "warm":
        assert ("warm-discard",) in events
        assert ("warm-release-task", False) in events
    else:
        assert ("slot-close",) in events
    assert source.closed
    assert handle.closed


def test_managed_streaming_cache_miss_finishes_in_order_then_hits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=8_193,
        part_count=2,
    )
    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=True,
    )
    original_begin = StemCache.begin_streaming_store
    transaction_count = 0

    def recording_begin(
        cache: StemCache,
        key: str,
        **kwargs: Any,
    ) -> _TransactionProxy:
        nonlocal transaction_count
        index = transaction_count
        transaction_count += 1
        return _TransactionProxy(
            original_begin(cache, key, **kwargs),
            index,
            events,
        )

    monkeypatch.setattr(StemCache, "begin_streaming_store", recording_begin)
    cache_directory = tmp_path / "cache"
    cold = render_plan(
        plan,
        tmp_path / "cold",
        stem_cache_directory=cache_directory,
    )
    cold_batch_count = len(batches)
    cold_source_count = len(sources)
    hot = render_plan(
        plan,
        tmp_path / "hot",
        stem_cache_directory=cache_directory,
    )

    assert cold.stem_cache["misses"] == 2
    assert cold.stem_cache["writes"] == 2
    assert cold.stem_cache["write_failures"] == 0
    assert hot.stem_cache["hits"] == 2
    assert hot.stem_cache["misses"] == 0
    assert len(batches) == cold_batch_count == 1
    assert len(sources) == cold_source_count == 2
    for index in range(2):
        assert events.index(("source-verified", index)) < events.index(
            ("lease-release", index, True)
        )
        assert events.index(("lease-release", index, True)) < events.index(
            ("cache-finish", index)
        )
    assert all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert _public_artifacts(hot) == _public_artifacts(cold)


def test_analysis_managed_cache_miss_materialises_without_losing_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=4_097,
        part_count=2,
    )
    _select_workers(monkeypatch, 1)
    monkeypatch.setattr(ensemble_module, "_render_part", _fake_render(plan))
    serial = render_plan(
        plan,
        tmp_path / "analysis-serial",
        collaboration_mode="analyze",
    )

    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=True,
    )
    materialised_frames: list[int] = []
    original_materialise = ensemble_module._StreamedRawStem.materialise

    def tracking_materialise(source: Any) -> np.ndarray:
        materialised_frames.append(source.frame_count)
        return original_materialise(source)

    monkeypatch.setattr(
        ensemble_module._StreamedRawStem,
        "materialise",
        tracking_materialise,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("managed analysis unexpectedly fell back")
        ),
    )
    managed = render_plan(
        plan,
        tmp_path / "analysis-managed",
        collaboration_mode="analyze",
        stem_cache_directory=tmp_path / "analysis-stem-cache",
    )

    assert batches == [(0, 1)]
    assert materialised_frames == [
        plan.audio_by_executor[part.executor.executor_id].shape[0]
        for part in plan.parts
    ]
    assert all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert managed.stem_cache["misses"] == 2
    assert managed.stem_cache["writes"] == 2
    assert managed.mix_report == serial.mix_report
    assert managed.mix_report is not None
    receipt = json.loads(Path(managed.receipt_path).read_text(encoding="utf-8"))
    assert receipt["mix_report"]["sha256"] == _sha256_file(
        Path(managed.mix_report_path)
    )
    assert _public_artifacts(managed) == _public_artifacts(serial)


def test_cache_append_write_error_keeps_audio_identical_and_reports_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=12_289,
        part_count=2,
    )
    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=True,
    )
    baseline = render_plan(plan, tmp_path / "baseline")
    baseline_source_count = len(sources)

    def fail_cache_write(target: Any, payload: Any) -> None:
        del target, payload
        raise OSError("injected disk full")

    monkeypatch.setattr(
        stem_cache_module,
        "_write_stream_payload",
        fail_cache_write,
    )
    cache_directory = tmp_path / "failed-cache"
    failed = render_plan(
        plan,
        tmp_path / "cache-write-failure",
        stem_cache_directory=cache_directory,
    )

    assert len(batches) == 2
    assert len(sources) == baseline_source_count * 2
    assert all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert failed.stem_cache["misses"] == 2
    assert failed.stem_cache["writes"] == 0
    assert failed.stem_cache["write_failures"] == 2
    assert failed.stem_cache["reason_counts"]["store_write_error"] == 2
    assert not list(cache_directory.rglob("*.json"))
    assert not list(cache_directory.rglob("*.f32le"))
    assert _public_artifacts(failed) == _public_artifacts(baseline)


def test_forced_analysis_transaction_is_publicly_exact_and_ordered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    envelopes = (
        (
            SimpleNamespace(time_seconds=0.0, offset_db=-2.5),
            SimpleNamespace(time_seconds=4.0, offset_db=1.25),
            SimpleNamespace(time_seconds=8.0, offset_db=-0.75),
        ),
        (
            SimpleNamespace(time_seconds=0.0, offset_db=0.5),
            SimpleNamespace(time_seconds=3.0, offset_db=-1.75),
            SimpleNamespace(time_seconds=8.0, offset_db=1.0),
        ),
    )
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=_LONG_FRAME_COUNT,
        part_count=2,
        gain_envelopes=envelopes,
    )
    hall_events: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        space_module,
        "render_reverb_stereo",
        _fake_hall_renderer(hall_events),
    )
    _select_workers(monkeypatch, 1)
    monkeypatch.setattr(ensemble_module, "_render_part", _fake_render(plan))
    baseline = render_plan(
        plan,
        tmp_path / "analysis-array",
        collaboration_mode="analyze",
        space=_FakeSpace(),
    )

    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=True,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("transaction analysis unexpectedly rerendered")
        ),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_DIRECT_ANALYSIS_STEM_LOAD_BYTES",
        0,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_DIRECT_STEM_CACHE_LOAD_BYTES",
        0,
    )
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 50),
    )

    original_begin = StemCache.begin_streaming_store
    cache_index = 0

    def recording_begin(cache: StemCache, key: str, **kwargs: Any) -> Any:
        nonlocal cache_index
        index = cache_index
        cache_index += 1
        return _TransactionProxy(
            original_begin(cache, key, **kwargs),
            index,
            events,
        )

    monkeypatch.setattr(StemCache, "begin_streaming_store", recording_begin)

    original_validate_peak = ensemble_module._validate_stem_peak

    def recording_validate_peak(executor: Any, peak: float) -> None:
        events.append(("peak", executor.executor_id))
        original_validate_peak(executor, peak)

    monkeypatch.setattr(
        ensemble_module,
        "_validate_stem_peak",
        recording_validate_peak,
    )
    original_finish_view = (
        collaboration_report_module._StemAnalysisTransaction.finish_view
    )

    def recording_finish_view(transaction: Any) -> Any:
        executor_id = transaction._executor.executor_id
        events.append(("diagnostic-start", executor_id))
        view = original_finish_view(transaction)
        events.append(("diagnostic-finish", executor_id))
        return view

    monkeypatch.setattr(
        collaboration_report_module._StemAnalysisTransaction,
        "finish_view",
        recording_finish_view,
    )
    original_writer = ensemble_module.write_wav_pcm24
    write_index = 0

    def recording_writer(path: Path, audio: Any, sample_rate: int) -> int:
        nonlocal write_index
        label = (
            plan.parts[write_index].executor.executor_id
            if write_index < len(plan.parts)
            else "mix"
        )
        write_index += 1
        events.append(("wav", label))
        return original_writer(path, audio, sample_rate)

    monkeypatch.setattr(ensemble_module, "write_wav_pcm24", recording_writer)
    original_accumulate = ensemble_module._accumulate_stem
    mix_index = 0

    def recording_accumulate(*args: Any, **kwargs: Any) -> None:
        nonlocal mix_index
        executor_id = plan.parts[mix_index].executor.executor_id
        mix_index += 1
        events.append(("mix", executor_id))
        original_accumulate(*args, **kwargs)

    monkeypatch.setattr(
        ensemble_module,
        "_accumulate_stem",
        recording_accumulate,
    )

    transaction_result = render_plan(
        plan,
        tmp_path / "analysis-transaction",
        collaboration_mode="analyze",
        space=_FakeSpace(),
        stem_cache_directory=tmp_path / "analysis-cache",
    )

    assert batches == [(0, 1)]
    assert len(sources) == 2
    assert all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert transaction_result.mix_report == baseline.mix_report
    assert _public_artifacts(transaction_result) == _public_artifacts(baseline)
    for index, part in enumerate(plan.parts):
        executor_id = part.executor.executor_id
        ordered = (
            ("source-verified", index),
            ("source-close", index, True),
            ("lease-release", index, True),
            ("cache-finish", index),
            ("peak", executor_id),
            ("diagnostic-start", executor_id),
            ("diagnostic-finish", executor_id),
            ("wav", executor_id),
            ("mix", executor_id),
        )
        positions = [events.index(event) for event in ordered]
        assert positions == sorted(positions)

    events.clear()
    write_index = 0
    mix_index = 0
    hot = render_plan(
        plan,
        tmp_path / "analysis-transaction-hot",
        collaboration_mode="analyze",
        space=_FakeSpace(),
        stem_cache_directory=tmp_path / "analysis-cache",
    )
    assert batches == [(0, 1)]
    assert len(sources) == 2
    assert hot.stem_cache["hits"] == 2
    assert hot.stem_cache["misses"] == 0
    assert hot.mix_report == baseline.mix_report
    assert _public_artifacts(hot) == _public_artifacts(baseline)
    assert len(hall_events) == 3

    # A long verified hit may be unable to reserve a bounded snapshot on the
    # staging volume.  Analysis must retain the historic verified in-memory
    # hit in that case, rather than reporting a miss and rendering again.
    original_load = StemCache.load
    original_open_verified = StemCache.open_verified
    load_limits: list[Any] = []
    open_statuses: list[str] = []

    def recording_load(cache: StemCache, key: str, **kwargs: Any) -> Any:
        load_limits.append(kwargs.get("maximum_audio_bytes"))
        return original_load(cache, key, **kwargs)

    def recording_open_verified(
        cache: StemCache,
        key: str,
        **kwargs: Any,
    ) -> Any:
        lookup = original_open_verified(cache, key, **kwargs)
        open_statuses.append(lookup.status)
        return lookup

    monkeypatch.setattr(StemCache, "load", recording_load)
    monkeypatch.setattr(StemCache, "open_verified", recording_open_verified)
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=0),
    )
    events.clear()
    write_index = 0
    mix_index = 0
    low_disk_hot = render_plan(
        plan,
        tmp_path / "analysis-transaction-hot-low-disk",
        collaboration_mode="analyze",
        space=_FakeSpace(),
        stem_cache_directory=tmp_path / "analysis-cache",
    )
    assert open_statuses == ["unavailable", "unavailable"]
    assert load_limits.count(0) == 2
    assert load_limits.count(None) == 2
    assert batches == [(0, 1)]
    assert len(sources) == 2
    assert all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert low_disk_hot.stem_cache["hits"] == 2
    assert low_disk_hot.stem_cache["misses"] == 0
    assert low_disk_hot.mix_report == baseline.mix_report
    assert _public_artifacts(low_disk_hot) == _public_artifacts(baseline)
    assert len(hall_events) == 4


@pytest.mark.parametrize("fallback_site", ("disk", "create"))
def test_analysis_transaction_preconsume_failure_materialises_without_rerender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
    fallback_site: str,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / f"plan-{fallback_site}",
        frame_count=_LONG_FRAME_COUNT,
        part_count=2,
    )
    _select_workers(monkeypatch, 1)
    monkeypatch.setattr(ensemble_module, "_render_part", _fake_render(plan))
    baseline = render_plan(
        plan,
        tmp_path / f"fallback-{fallback_site}-array",
        collaboration_mode="analyze",
    )

    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=False,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("preconsume fallback unexpectedly rerendered")
        ),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_DIRECT_ANALYSIS_STEM_LOAD_BYTES",
        0,
    )
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(
            free=0 if fallback_site == "disk" else 1 << 50
        ),
    )
    if fallback_site == "create":
        monkeypatch.setattr(
            collaboration_report_module.CollaborationReportBuilder,
            "_begin_stem_transaction",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected analysis scratch creation failure")
            ),
        )

    fallback = render_plan(
        plan,
        tmp_path / f"fallback-{fallback_site}-managed",
        collaboration_mode="analyze",
    )

    assert batches == [(0, 1)]
    assert len(sources) == 2
    assert all(source.materialise_calls == 1 for source in sources)
    assert all(source.closed for source in sources)
    assert _public_artifacts(fallback) == _public_artifacts(baseline)


@pytest.mark.parametrize("failed_check", (1, 2))
def test_analysis_transaction_staging_identity_failure_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
    failed_check: int,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / f"identity-plan-{failed_check}",
        frame_count=_LONG_FRAME_COUNT,
        part_count=2,
    )
    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=True,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_DIRECT_ANALYSIS_STEM_LOAD_BYTES",
        0,
    )
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 50),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("identity failure unexpectedly rerendered")
        ),
    )
    real_revalidate = ensemble_module.revalidate_plain_directory
    staging_checks = 0

    def fail_staging_revalidation(identity: Any) -> Path:
        nonlocal staging_checks
        if ".render-stage." in identity.path.name:
            staging_checks += 1
            if staging_checks == failed_check:
                raise OSError(
                    errno.ESTALE,
                    "injected staging identity replacement",
                )
        return real_revalidate(identity)

    monkeypatch.setattr(
        ensemble_module,
        "revalidate_plain_directory",
        fail_staging_revalidation,
    )

    with pytest.raises(OSError) as caught:
        render_plan(
            plan,
            tmp_path / f"identity-failed-{failed_check}",
            collaboration_mode="analyze",
        )

    assert caught.value.errno == errno.ESTALE
    assert staging_checks >= failed_check
    assert batches == [(0, 1)]
    assert sources and all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)


def test_analysis_transaction_memory_error_propagates_and_closes_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / "memory-error-plan",
        frame_count=_LONG_FRAME_COUNT,
        part_count=2,
    )
    _select_workers(monkeypatch, 2)
    events: list[tuple[Any, ...]] = []
    sources, _batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        events,
        forbid_materialise=True,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_DIRECT_ANALYSIS_STEM_LOAD_BYTES",
        0,
    )
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 50),
    )
    monkeypatch.setattr(
        collaboration_report_module.CollaborationReportBuilder,
        "_begin_stem_transaction",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            MemoryError("injected analysis allocation pressure")
        ),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("MemoryError unexpectedly rerendered")
        ),
    )

    with pytest.raises(MemoryError, match="injected analysis allocation"):
        render_plan(
            plan,
            tmp_path / "memory-error-output",
            collaboration_mode="analyze",
        )

    assert sources and all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert not list(
        tmp_path.rglob(".collaboration-analysis.*.f32")
    )


def test_mapped_hall_buses_are_exact_for_array_and_streamed_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / "plan",
        frame_count=_LONG_FRAME_COUNT,
        part_count=2,
    )
    # The production gate accepts only the concrete immutable plan class.  This
    # focused pipeline fixture has the same renderer contract; bind that type
    # locally so the test can exercise both array and owned-source consumers.
    monkeypatch.setattr(ensemble_module, "PerformancePlan", _PipelinePlan)
    real_pool = WorkerSlotPool
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: real_pool(tmp_path / "hall-bus-slots"),
    )
    hall_events: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        space_module,
        "render_reverb_stereo",
        _fake_hall_renderer(hall_events),
    )
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)

    _select_workers(monkeypatch, 1)
    monkeypatch.setattr(ensemble_module, "_render_part", _fake_render(plan))
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1 << 60,
    )
    baseline = render_plan(
        plan,
        tmp_path / "array-ram",
        space=space,
    )

    observed: list[Any] = []
    real_try = ensemble_module._try_mapped_hall_mix_buses

    def observe_transport(*args: Any, **kwargs: Any) -> Any:
        transport = real_try(*args, **kwargs)
        observed.append(transport)
        return transport

    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_try_mapped_hall_mix_buses",
        observe_transport,
    )
    mapped_array = render_plan(
        plan,
        tmp_path / "array-mapped",
        space=space,
    )

    _select_workers(monkeypatch, 2)
    stream_events: list[tuple[Any, ...]] = []
    sources, batches = _install_fake_managed_batch(
        monkeypatch,
        plan,
        stream_events,
        forbid_materialise=True,
    )
    mapped_stream = render_plan(
        plan,
        tmp_path / "stream-mapped",
        space=space,
    )

    expected = _public_artifacts(baseline)
    assert _public_artifacts(mapped_array) == expected
    assert _public_artifacts(mapped_stream) == expected
    assert len(hall_events) == 3
    assert all(event[0] == "hall-render" for event in hall_events)
    assert len(observed) == 2 and all(item is not None for item in observed)
    assert all(item._closed for item in observed)
    assert batches == [(0, 1)]
    assert sources and all(source.closed for source in sources)
    assert all(source.materialise_calls == 0 for source in sources)
    assert not list(tmp_path.rglob(".tianlai-hall-*-bus.*.tmp"))


def test_streamed_analysis_post_gain_nonfinite_preserves_peak_error(
    tmp_path: Path,
) -> None:
    executor = SimpleNamespace(
        executor_id="post-gain-nonfinite",
        gain_db=12.0,
    )
    audio = np.full((17, 2), np.finfo(np.float32).max, dtype="<f4")
    events: list[tuple[Any, ...]] = []
    source = _FakeStemSource(
        0,
        audio,
        events,
        forbid_materialise=True,
    )
    builder = collaboration_report_module.CollaborationReportBuilder(
        CollaborationSettings(mode="analyze"),
        8_000,
        scratch_parent=tmp_path,
    )
    transaction = builder._begin_stem_transaction(
        executor,
        frame_count=len(audio),
    )
    try:
        with pytest.raises(ValueError) as expected:
            ensemble_module._validate_stem_peak(executor, float("inf"))
        with np.errstate(over="ignore", invalid="ignore"):
            with pytest.raises(ValueError) as actual:
                ensemble_module._consume_streamed_analysis_stem(
                    source,
                    transaction,
                    sample_rate=8_000,
                    executor=executor,
                    gain_envelope=(),
                )
        assert str(actual.value) == str(expected.value)
        assert source.closed
        assert transaction.closed
        assert ("source-verified", 0) in events
        assert events.index(("source-verified", 0)) < events.index(
            ("source-close", 0, True)
        )
    finally:
        builder.close()


@pytest.mark.parametrize(
    "failure_site",
    ("append", "diagnostic", "wav", "mix"),
)
def test_analysis_transaction_failure_closes_source_cache_worker_and_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_pipeline_runtime: None,
    failure_site: str,
) -> None:
    del isolated_pipeline_runtime
    plan = _PipelinePlan(
        tmp_path / f"failure-plan-{failure_site}",
        frame_count=_LONG_FRAME_COUNT,
        part_count=1,
    )
    events: list[tuple[Any, ...]] = []
    source, handle = _detached_worker_source(
        tmp_path,
        monkeypatch,
        plan.audio_by_executor["stream-part-0"],
        events,
        lease_kind="slot",
    )
    cache_transaction = _RecordingTransaction(events)
    wrapped = ensemble_module._StreamedRawStem(
        source,
        transaction=cache_transaction,
        finish_cache=lambda: cache_transaction.finish(
            source.frame_count,
            source.audio_sha256,
        ),
    )

    def raw_stems(*args: Any, **kwargs: Any) -> Iterator[Any]:
        del args, kwargs
        try:
            yield (
                0,
                plan.parts[0],
                wrapped,
                2,
                _sha256_file(
                    Path(plan.parts[0].executor.capability.manifest_path)
                ),
            )
        finally:
            if not wrapped.closed:
                wrapped.close()

    monkeypatch.setattr(
        ensemble_module,
        "_iter_raw_stems_in_plan_order",
        raw_stems,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_DIRECT_ANALYSIS_STEM_LOAD_BYTES",
        0,
    )
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda path: SimpleNamespace(free=1 << 50),
    )

    released: list[tuple[Any, Any]] = []
    original_close_scratch = (
        collaboration_report_module._close_private_stem_scratch
    )

    def recording_close_scratch(
        audio: Any,
        scratch: Any,
        *,
        flush: bool,
    ) -> None:
        try:
            original_close_scratch(audio, scratch, flush=flush)
        finally:
            released.append((audio, scratch))

    monkeypatch.setattr(
        collaboration_report_module,
        "_close_private_stem_scratch",
        recording_close_scratch,
    )
    if failure_site == "append":
        monkeypatch.setattr(
            collaboration_report_module._StemAnalysisTransaction,
            "append",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected append failure")
            ),
        )
    elif failure_site == "diagnostic":
        monkeypatch.setattr(
            collaboration_report_module.CollaborationReportBuilder,
            "_add_stem",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected diagnostic failure")
            ),
        )
    elif failure_site == "wav":
        monkeypatch.setattr(
            ensemble_module,
            "write_wav_pcm24",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected wav failure")
            ),
        )
    else:
        monkeypatch.setattr(
            ensemble_module,
            "_accumulate_stem",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                OSError("injected mix failure")
            ),
        )

    with pytest.raises(OSError, match=f"injected {failure_site} failure"):
        render_plan(
            plan,
            tmp_path / f"failed-{failure_site}",
            collaboration_mode="analyze",
        )

    assert wrapped.closed
    assert source.closed
    assert handle.closed
    assert ("slot-close",) in events
    assert released
    assert all(audio._mmap.closed for audio, _scratch in released)
    assert all(scratch.closed for _audio, scratch in released)
    if failure_site == "append":
        assert cache_transaction.aborted
        assert not cache_transaction.committed
        assert ("completion", False) in events
    else:
        assert cache_transaction.committed
        assert not cache_transaction.aborted
        assert ("completion", True) in events
        assert events.index(("completion", True)) < events.index(
            ("cache-finish",)
        )
    assert not list(
        tmp_path.rglob(".collaboration-analysis.*.f32")
    )
