from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from tianlai import collaboration_report as report_module
from tianlai import ensemble as ensemble_module
from tianlai import worker_slots as slots_module
from tianlai.collaboration_report import CollaborationReportBuilder
from tianlai.roster import (
    BalanceRelation,
    CollaborationAnalysis,
    CollaborationSettings,
    PartGroup,
)
from tianlai.stem_source import StemBlockSource
from tianlai.worker_slots import WorkerSlotError, WorkerSlotPool


ROOT = Path(__file__).resolve().parents[1]
SAMPLE_RATE = 8_000


def _settings(*, relation: bool = False) -> CollaborationSettings:
    return CollaborationSettings(
        mode="analyze",
        analysis=CollaborationAnalysis(
            window_ms=200.0,
            hop_ms=100.0,
            gate_dbfs=-60.0,
        ),
        balance_relations=(
            (BalanceRelation("pad", "lead", -6.0, 0.25, 4.0),)
            if relation
            else ()
        ),
        declared=True,
    )


def _group_settings() -> CollaborationSettings:
    return CollaborationSettings(
        mode="analyze",
        analysis=CollaborationAnalysis(
            window_ms=200.0,
            hop_ms=100.0,
            gate_dbfs=-60.0,
        ),
        part_groups=(PartGroup("pads", ("pad-a", "pad-b")),),
        balance_relations=(
            BalanceRelation("pads", "lead", -6.0, 0.25, 4.0),
        ),
        declared=True,
    )


def _executor(executor_id: str, part_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        executor_id=executor_id,
        part_id=part_id,
        capability=SimpleNamespace(relative_path=f"test/{executor_id}"),
        gain_db=-3.0,
        pan=0.0,
        role=None,
    )


def _audio(frame_count: int, amplitude: float = 0.1) -> np.ndarray:
    time = np.arange(frame_count, dtype=np.float64) / SAMPLE_RATE
    mono = amplitude * np.sin(2.0 * np.pi * 440.0 * time)
    return np.column_stack((mono, mono)).astype(np.float32)


def _traceback_holds_mapping(
    error: BaseException,
    mapping: np.memmap,
) -> bool:
    traceback = error.__traceback__
    while traceback is not None:
        if any(
            candidate is mapping
            for candidate in traceback.tb_frame.f_locals.values()
        ):
            return True
        traceback = traceback.tb_next
    return False


class _RecordingLease:
    def __init__(
        self,
        scratch_directory: Path,
        *,
        scratch_bytes: int,
        volume_id: str,
        events: list[str],
        close_error: BaseException | None = None,
    ) -> None:
        self.scratch_directory = scratch_directory.resolve()
        self.claim = SimpleNamespace(
            scratch_bytes=scratch_bytes,
            scratch_volume_id=volume_id,
        )
        self.events = events
        self.close_error = close_error
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.events.append("lease-close")
        if self.close_error is not None:
            raise self.close_error


class _RecordingPool:
    def __init__(
        self,
        scratch_directory: Path,
        events: list[str],
        *,
        deny: bool = False,
        close_error: BaseException | None = None,
    ) -> None:
        self.scratch_directory = scratch_directory
        self.events = events
        self.deny = deny
        self.close_error = close_error
        self.claims: list[Any] = []
        self.leases: list[_RecordingLease] = []

    def reserve_session_scratch(self, claim: Any) -> _RecordingLease | None:
        self.events.append("reserve")
        self.claims.append(claim)
        if self.deny:
            return None
        lease = _RecordingLease(
            self.scratch_directory,
            scratch_bytes=claim.scratch_bytes,
            volume_id=report_module.scratch_volume_identity(
                self.scratch_directory
            ),
            events=self.events,
            close_error=self.close_error,
        )
        self.leases.append(lease)
        return lease


def _serial_part(root: Path, frame_count: int) -> SimpleNamespace:
    manifest = root / "instrument.json"
    manifest.write_text(json.dumps({"name": "scratch test"}), encoding="utf-8")
    return SimpleNamespace(
        executor=SimpleNamespace(
            executor_id="serial-scratch",
            capability=SimpleNamespace(manifest_path=str(manifest)),
            override_map={},
        ),
        performance={
            "sample_rate": SAMPLE_RATE,
            "channels": 2,
            "duration_seconds": frame_count / SAMPLE_RATE,
            "tail_seconds": 0.0,
            "events": [],
        },
    )


def test_serial_raw_rejects_performance_sample_rate_mismatch(
    tmp_path: Path,
) -> None:
    part = _serial_part(tmp_path, 17)

    with pytest.raises(
        ValueError,
        match="声部 'serial-scratch' 的采样率与总谱不一致",
    ):
        ensemble_module._render_part_source(
            part,
            SAMPLE_RATE + 1,
            scratch_directory=tmp_path,
        )


def test_serial_raw_lease_precedes_instrument_and_follows_owned_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = _audio(257)
    events: list[str] = []
    pool = _RecordingPool(tmp_path, events)
    monkeypatch.setattr(
        ensemble_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )

    instrument = SimpleNamespace(close=lambda: events.append("instrument-close"))

    def create_instrument(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        events.append("instrument-create")
        return instrument

    def render_blocks(*args: Any, **kwargs: Any) -> tuple[Any, tuple[int]]:
        del args, kwargs
        events.append("render")
        return iter((frames,)), (3,)

    monkeypatch.setattr(ensemble_module, "create_instrument", create_instrument)
    monkeypatch.setattr(
        ensemble_module,
        "_prefer_frame_stream_path",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        ensemble_module,
        "render_document_blocks",
        render_blocks,
    )

    source, peak, _manifest_sha256 = ensemble_module._render_part_source(
        _serial_part(tmp_path, len(frames)),
        SAMPLE_RATE,
        scratch_directory=tmp_path,
    )

    assert isinstance(source, StemBlockSource)
    assert peak == 3
    assert pool.claims[0].scratch_bytes == frames.nbytes
    assert events[:3] == ["reserve", "instrument-create", "render"]
    assert pool.leases and not pool.leases[0].closed
    np.testing.assert_array_equal(source.materialise(), frames)
    assert not pool.leases[0].closed
    source.close()
    assert pool.leases[0].closed
    assert events[-1] == "lease-close"


@pytest.mark.parametrize("failure", ("denied", "oserror"))
def test_serial_raw_admission_failure_falls_back_before_instrument_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    events: list[str] = []
    if failure == "denied":
        pool = _RecordingPool(tmp_path, events, deny=True)
        factory = lambda: pool
    else:
        def factory() -> Any:
            events.append("reserve")
            raise OSError("injected ledger failure")

    monkeypatch.setattr(
        ensemble_module,
        "_session_scratch_pool_factory",
        factory,
    )
    expected = _audio(17)

    def buffered_render(*args: Any, **kwargs: Any) -> tuple[Any, int, str]:
        del args, kwargs
        events.append("buffered-render")
        return expected, 2, "a" * 64

    monkeypatch.setattr(ensemble_module, "_render_part", buffered_render)
    rendered = ensemble_module._render_part_source(
        _serial_part(tmp_path, len(expected)),
        SAMPLE_RATE,
        scratch_directory=tmp_path,
    )
    assert rendered[0] is expected
    assert events == ["reserve", "buffered-render"]


def test_serial_raw_wrong_lease_identity_fails_closed_before_render(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    events: list[str] = []
    pool = _RecordingPool(other, events)
    monkeypatch.setattr(
        ensemble_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("identity failure reached rendering")
        ),
    )

    with pytest.raises(WorkerSlotError, match="wrong directory"):
        ensemble_module._render_part_source(
            _serial_part(tmp_path, 17),
            SAMPLE_RATE,
            scratch_directory=tmp_path,
        )
    assert pool.leases[0].closed


def test_consumer_memory_errors_are_never_downgraded_to_ram_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_pool() -> Any:
        raise MemoryError("injected scratch admission memory error")

    monkeypatch.setattr(
        ensemble_module,
        "_session_scratch_pool_factory",
        fail_pool,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_part",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("MemoryError reached buffered rendering")
        ),
    )
    with pytest.raises(MemoryError, match="admission memory error"):
        ensemble_module._render_part_source(
            _serial_part(tmp_path, 17),
            SAMPLE_RATE,
            scratch_directory=tmp_path,
        )

    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        fail_pool,
    )
    builder = CollaborationReportBuilder(
        _settings(),
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=17,
    )
    with pytest.raises(MemoryError, match="admission memory error"):
        builder._ensure_scratch_admission(17)
    builder.close()


def test_collaboration_claim_is_exact_and_lease_closes_after_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = _audio(257)
    events: list[str] = []
    pool = _RecordingPool(tmp_path, events)
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    builder = CollaborationReportBuilder(
        _settings(),
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=len(frames),
    )
    transaction = builder._begin_stem_transaction(
        _executor("texture", "texture"),
        frame_count=len(frames),
    )
    transaction.append(frames)
    mapping = transaction._audio
    view = transaction.finish_view()

    assert pool.claims[0].scratch_bytes == frames.nbytes
    assert pool.leases and not pool.leases[0].closed
    assert mapping is not None and not mapping._mmap.closed
    view.close()
    assert mapping._mmap.closed
    assert not pool.leases[0].closed
    builder.close()
    assert pool.leases[0].closed
    assert events[-1] == "lease-close"


def test_collaboration_allows_only_one_current_transaction_or_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = _audio(257)
    pool = _RecordingPool(tmp_path, [])
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    builder = CollaborationReportBuilder(
        _settings(),
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=len(frames),
    )
    first = builder._begin_stem_transaction(
        _executor("first", "first"),
        frame_count=len(frames),
    )
    with pytest.raises(RuntimeError, match="already has a current"):
        builder._begin_stem_transaction(
            _executor("second", "second"),
            frame_count=len(frames),
        )
    with pytest.raises(RuntimeError, match="unfinished current owner"):
        builder.add_stem(_executor("array", "array"), frames)

    first.append(frames)
    view = first.finish_view()
    with pytest.raises(RuntimeError, match="already has a current"):
        builder._begin_stem_transaction(
            _executor("second", "second"),
            frame_count=len(frames),
        )
    view.close()
    second = builder._begin_stem_transaction(
        _executor("second", "second"),
        frame_count=len(frames),
    )
    second.close()
    builder.close()


def test_collaboration_claim_counts_unique_parts_groups_and_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_count = 257
    settings = CollaborationSettings(
        mode="analyze",
        analysis=CollaborationAnalysis(
            window_ms=200.0,
            hop_ms=100.0,
            gate_dbfs=-60.0,
        ),
        part_groups=(
            PartGroup("pads", ("pad-a", "pad-b")),
            PartGroup("leads", ("lead-a", "lead-b")),
        ),
        balance_relations=(
            BalanceRelation("pads", "leads", -6.0, 0.25, 4.0),
        ),
        declared=True,
    )
    pool = _RecordingPool(tmp_path, [])
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    builder = CollaborationReportBuilder(
        settings,
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=frame_count,
    )
    assert builder._ensure_scratch_admission(frame_count)
    # Four retained relation parts plus two cached part-group endpoints.  The
    # endpoint-build peak exceeds the ingestion peak of four parts plus one
    # current transaction, so six exact float32-stereo timelines are claimed.
    assert pool.claims[0].scratch_bytes == frame_count * 8 * 6
    builder.close()


def test_collaboration_admission_fallback_is_report_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = 2_000
    pad = _audio(frames, 0.1)
    lead = _audio(frames, 0.2)

    admitted_root = tmp_path / "admitted"
    admitted_root.mkdir()
    admitted_pool = _RecordingPool(admitted_root, [])
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: admitted_pool,
    )
    admitted = CollaborationReportBuilder(
        _settings(relation=True),
        SAMPLE_RATE,
        scratch_parent=admitted_root,
        dry_frame_count=frames,
    )
    admitted.add_stem(_executor("pad", "pad"), pad)
    admitted.add_stem(_executor("lead", "lead"), lead)
    expected = admitted.build()

    denied_root = tmp_path / "denied"
    denied_root.mkdir()
    denied_pool = _RecordingPool(denied_root, [], deny=True)
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: denied_pool,
    )
    denied = CollaborationReportBuilder(
        _settings(relation=True),
        SAMPLE_RATE,
        scratch_parent=denied_root,
        dry_frame_count=frames,
    )
    denied.add_stem(_executor("pad", "pad"), pad)
    denied.add_stem(_executor("lead", "lead"), lead)
    assert denied.build() == expected

    # Two unique retained relation parts plus the possible current transaction.
    assert admitted_pool.claims[0].scratch_bytes == frames * 8 * 3


def test_collaboration_cleanup_preserves_mapping_error_and_still_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = _audio(257)
    events: list[str] = []
    pool = _RecordingPool(
        tmp_path,
        events,
        close_error=OSError("injected lease cleanup failure"),
    )
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    builder = CollaborationReportBuilder(
        _settings(),
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=len(frames),
    )
    transaction = builder._begin_stem_transaction(
        _executor("texture", "texture"),
        frame_count=len(frames),
    )
    transaction.append(frames)
    real_close = report_module._close_registered_scratch_resources

    def close_then_fail(*args: Any, **kwargs: Any) -> list[BaseException]:
        errors = real_close(*args, **kwargs)
        events.append("mapping-close")
        return [
            *errors,
            OSError("injected mapping cleanup failure"),
        ]

    monkeypatch.setattr(
        report_module,
        "_close_registered_scratch_resources",
        close_then_fail,
    )
    with pytest.raises(OSError, match="mapping cleanup failure"):
        builder.close()
    assert events[-2:] == ["mapping-close", "lease-close"]


def test_registered_collaboration_cleanup_deduplicates_and_closes_all_mappings_first(
) -> None:
    events: list[str] = []

    class Mapping:
        def __init__(self, name: str) -> None:
            self.name = name
            self._mmap = self

        def flush(self) -> None:
            events.append(f"{self.name}-flush")

        def close(self) -> None:
            events.append(f"{self.name}-mapping-close")

    class Handle:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            events.append(f"{self.name}-handle-close")

    first = Mapping("first")
    second = Mapping("second")
    first_handle = Handle("first")
    second_handle = Handle("second")
    errors = report_module._close_registered_scratch_resources(
        (
            (first, first_handle),
            (first, first_handle),
            (second, first_handle),
            (second, second_handle),
        ),
        flush=True,
    )

    assert errors == []
    assert events.count("first-mapping-close") == 1
    assert events.count("second-mapping-close") == 1
    assert events.count("first-handle-close") == 1
    assert events.count("second-handle-close") == 1
    first_handle_index = min(
        index
        for index, event in enumerate(events)
        if event.endswith("handle-close")
    )
    assert all(
        index < first_handle_index
        for index, event in enumerate(events)
        if event.endswith("mapping-close")
    )


def test_collaboration_missing_group_member_closes_traceback_mapping_before_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = _audio(257)
    events: list[str] = []
    pool = _RecordingPool(
        tmp_path,
        events,
        close_error=OSError("injected lease cleanup failure"),
    )
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    builder = CollaborationReportBuilder(
        _group_settings(),
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=len(frames),
    )
    captured_mappings: list[np.memmap] = []
    real_scratch_memmap = builder._scratch_memmap

    def recording_scratch_memmap(shape: tuple[int, ...]) -> np.memmap:
        mapping = real_scratch_memmap(shape)
        captured_mappings.append(mapping)
        return mapping

    monkeypatch.setattr(builder, "_scratch_memmap", recording_scratch_memmap)
    real_close = report_module._close_registered_scratch_resources

    def close_with_secondary_error(
        *args: Any,
        **kwargs: Any,
    ) -> None:
        errors = real_close(*args, **kwargs)
        assert errors == []
        events.append("resources-close")
        raise OSError("injected resource cleanup failure")

    monkeypatch.setattr(
        report_module,
        "_close_registered_scratch_resources",
        close_with_secondary_error,
    )
    builder.add_stem(_executor("pad-a", "pad-a"), frames)
    builder.add_stem(_executor("lead", "lead"), frames)

    with pytest.raises(ValueError, match="pad-b") as raised:
        builder.build()

    combined = captured_mappings[-1]
    assert _traceback_holds_mapping(raised.value, combined)
    assert all(mapping._mmap.closed for mapping in captured_mappings)
    assert pool.leases[0].closed
    assert events[-2:] == ["resources-close", "lease-close"]
    assert builder._scratch_resources == {}
    assert list(tmp_path.iterdir()) == []


def test_collaboration_group_shape_failure_closes_traceback_mapping_before_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = _audio(257)
    events: list[str] = []
    pool = _RecordingPool(tmp_path, events)
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    builder = CollaborationReportBuilder(
        _group_settings(),
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=len(frames),
    )
    captured_mappings: list[np.memmap] = []
    real_scratch_memmap = builder._scratch_memmap

    def recording_scratch_memmap(shape: tuple[int, ...]) -> np.memmap:
        mapping = real_scratch_memmap(shape)
        captured_mappings.append(mapping)
        return mapping

    monkeypatch.setattr(builder, "_scratch_memmap", recording_scratch_memmap)
    builder.add_stem(_executor("pad-a", "pad-a"), frames)
    builder.add_stem(_executor("lead", "lead"), frames)
    builder._part_buffers["pad-b"] = _audio(129)

    with pytest.raises(ValueError, match="different lengths") as raised:
        builder.build()

    combined = captured_mappings[-1]
    assert _traceback_holds_mapping(raised.value, combined)
    assert all(mapping._mmap.closed for mapping in captured_mappings)
    assert pool.leases[0].closed
    assert events[-1] == "lease-close"
    assert list(tmp_path.iterdir()) == []


def test_collaboration_group_copy_failure_closes_traceback_mapping_before_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = _audio(257)
    events: list[str] = []
    pool = _RecordingPool(tmp_path, events)
    monkeypatch.setattr(
        report_module,
        "_session_scratch_pool_factory",
        lambda: pool,
    )
    builder = CollaborationReportBuilder(
        _group_settings(),
        SAMPLE_RATE,
        scratch_parent=tmp_path,
        dry_frame_count=len(frames),
    )
    builder.add_stem(_executor("pad-a", "pad-a"), frames)
    builder.add_stem(_executor("pad-b", "pad-b"), frames)
    builder.add_stem(_executor("lead", "lead"), frames)
    retained_mappings = list(builder._part_buffers.values())

    captured_mappings: list[np.memmap] = []
    failing_mapping_ids: set[int] = set()
    real_scratch_memmap = builder._scratch_memmap

    def recording_scratch_memmap(shape: tuple[int, ...]) -> np.memmap:
        mapping = real_scratch_memmap(shape)
        captured_mappings.append(mapping)
        failing_mapping_ids.add(id(mapping))
        return mapping

    monkeypatch.setattr(builder, "_scratch_memmap", recording_scratch_memmap)
    real_setitem = np.memmap.__setitem__

    def failing_setitem(
        mapping: np.memmap,
        key: Any,
        value: Any,
    ) -> None:
        if id(mapping) in failing_mapping_ids:
            raise MemoryError("injected group copy failure")
        real_setitem(mapping, key, value)

    monkeypatch.setattr(np.memmap, "__setitem__", failing_setitem)

    with pytest.raises(MemoryError, match="group copy failure") as raised:
        builder.build()

    combined = captured_mappings[-1]
    assert _traceback_holds_mapping(raised.value, combined)
    assert combined._mmap.closed
    assert all(mapping._mmap.closed for mapping in retained_mappings)
    assert pool.leases[0].closed
    assert events[-1] == "lease-close"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="real OS locking is unsupported on this platform",
)
def test_collaboration_claim_cannot_oversubscribe_live_process_and_recovers_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool_path = tmp_path / "pool"
    pool = WorkerSlotPool(pool_path)
    usable = max(
        1,
        shutil.disk_usage(tmp_path).free
        - slots_module._SCRATCH_FREE_RESERVE_BYTES,
    )
    held_bytes = max(1, usable * 3 // 5)
    code = r"""
import os, sys
from pathlib import Path
from tianlai.worker_slots import SessionScratchClaim, WorkerSlotPool
pool, scratch, amount = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
lease = WorkerSlotPool(pool).reserve_session_scratch(SessionScratchClaim(amount, scratch))
if lease is None: raise SystemExit(17)
print('READY', flush=True)
sys.stdin.buffer.read(1)
os._exit(23)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            code,
            str(pool_path),
            str(tmp_path),
            str(held_bytes),
        ],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        monkeypatch.setattr(
            report_module,
            "_session_scratch_pool_factory",
            lambda: pool,
        )
        dry_frames = max(1, (held_bytes + 7) // 8)
        denied = CollaborationReportBuilder(
            _settings(),
            SAMPLE_RATE,
            scratch_parent=tmp_path,
            dry_frame_count=dry_frames,
        )
        assert not denied._ensure_scratch_admission(dry_frames)
        denied.close()

        assert child.stdin is not None
        child.stdin.write("x")
        child.stdin.flush()
        assert child.wait(timeout=10) == 23

        recovered = CollaborationReportBuilder(
            _settings(),
            SAMPLE_RATE,
            scratch_parent=tmp_path,
            dry_frame_count=dry_frames,
        )
        assert recovered._ensure_scratch_admission(dry_frames)
        recovered.close()
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
