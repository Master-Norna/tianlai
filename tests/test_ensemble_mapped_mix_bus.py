from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import tianlai.ensemble as ensemble_module
from tianlai import worker_slots as slots_module
from tianlai.capability import read_capability
from tianlai.conductor import ExpressionSettings, PerformancePlan, PlanPart
from tianlai.render_lock import capture_plain_directory
from tianlai.roster import CollaborationSettings, Executor, Seat
from tianlai.space import SpaceConfig
from tianlai.worker_slots import WorkerSlotError, WorkerSlotPool


MIB = 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


def _empty_plan(*, frames: int = 800, sample_rate: int = 8_000) -> PerformancePlan:
    return PerformancePlan(
        title="mapped dry mix bus",
        sample_rate=sample_rate,
        duration_seconds=frames / sample_rate,
        expression=ExpressionSettings(),
        roster_name="mapped-bus-test",
        parts=(),
    )


def _real_warm_pad_plan(*, frames: int, sample_rate: int = 48_000) -> PerformancePlan:
    catalog = ROOT / "乐器"
    capability = read_capability(
        catalog / "电子乐器" / "温暖铺底" / "乐器.json",
        root=catalog,
    )
    duration = frames / sample_rate
    executor = Executor(
        executor_id="hall-benchmark-warm-pad",
        part_id="tone",
        capability=capability,
        gain_db=-12.0,
        pan=0.0,
        seat=Seat(azimuth_deg=0.0, distance_m=3.0),
        transpose=0,
        articulation_map=(),
        kit_pitch=None,
    )
    part = PlanPart(
        executor=executor,
        performance={
            "sample_rate": sample_rate,
            "channels": 2,
            "duration_seconds": duration,
            "tail_seconds": 0.0,
            "events": [
                {
                    "time": 0.0,
                    "type": "note_on",
                    "note_id": 1,
                    "midi_note": 60,
                    "velocity": 0.35,
                },
                {
                    "time": 0.05,
                    "type": "note_off",
                    "note_id": 1,
                },
            ],
        },
        trace=(),
        gain_envelope=(),
    )
    return PerformancePlan(
        title="manual hall mapped bus benchmark",
        sample_rate=sample_rate,
        duration_seconds=duration,
        expression=ExpressionSettings(),
        roster_name="hall-benchmark",
        parts=(part,),
        collaboration=CollaborationSettings(mode="manual"),
    )


def _bind_private_pool(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
) -> None:
    real_pool = WorkerSlotPool
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: real_pool(directory),
    )


def _tree_payloads(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_mapped_and_ram_generations_are_byte_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    plan = _empty_plan()

    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
        1 << 60,
    )
    ensemble_module.render_plan(plan, tmp_path / "ram", write_stems=False)

    observed_mapped = False
    real_try = ensemble_module._try_mapped_dry_mix_bus

    def observe_transport(*args: Any, **kwargs: Any):
        nonlocal observed_mapped
        transport = real_try(*args, **kwargs)
        observed_mapped = transport is not None
        return transport

    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
        1,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_try_mapped_dry_mix_bus",
        observe_transport,
    )
    ensemble_module.render_plan(plan, tmp_path / "mapped", write_stems=False)

    assert observed_mapped
    assert _tree_payloads(tmp_path / "mapped") == _tree_payloads(
        tmp_path / "ram"
    )


def test_hall_layout_and_aggregate_claim_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan(frames=12_345)
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    total_frames, mix_bytes, send_bytes, scratch_bytes = (
        ensemble_module._mapped_hall_mix_buses_layout(plan, space)
    )
    expected_tail = int(np.ceil(space.tail_seconds(plan.sample_rate) * plan.sample_rate))
    assert total_frames == 12_345 + expected_tail
    assert mix_bytes == total_frames * 16
    assert send_bytes == total_frames * 8
    assert scratch_bytes == total_frames * 24

    volume_id = ensemble_module.scratch_volume_identity(stage)
    lease = _FakeLease(
        stage,
        scratch_bytes=scratch_bytes,
        volume_id=volume_id,
    )
    pool = _FakePool(lease)
    monkeypatch.setattr(ensemble_module, "WorkerSlotPool", lambda: pool)
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )

    transport = ensemble_module._try_mapped_hall_mix_buses(
        plan,
        capture_plain_directory(stage),
        space=space,
        collaboration_mode=None,
    )
    assert transport is not None
    assert pool.claim.scratch_bytes == scratch_bytes
    assert transport.bus.shape == (total_frames, 2)
    assert transport.bus.dtype == np.dtype(np.float64)
    assert transport.bus.flags.c_contiguous and transport.bus.flags.writeable
    assert transport.send_bus.shape == (total_frames, 2)
    assert transport.send_bus.dtype == np.dtype(np.float32)
    assert transport.send_bus.flags.c_contiguous
    assert transport.send_bus.flags.writeable
    assert transport.bus._mmap is not transport.send_bus._mmap
    assert not np.any(transport.bus.view(np.uint8))
    assert not np.any(transport.send_bus.view(np.uint8))
    transport.close()
    assert lease.closed


def test_hall_mapping_is_byte_exact_and_closed_before_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    plan = _empty_plan(frames=1_024)
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)

    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1 << 60,
    )
    ensemble_module.render_plan(
        plan,
        tmp_path / "ram",
        write_stems=False,
        space=space,
    )

    observed: dict[str, Any] = {}
    real_try = ensemble_module._try_mapped_hall_mix_buses
    real_verify = ensemble_module._verify_render_generation

    def observe_transport(*args: Any, **kwargs: Any):
        transport = real_try(*args, **kwargs)
        observed["transport"] = transport
        return transport

    def verify_without_private_files(staging: Path) -> None:
        transport = observed["transport"]
        assert transport is not None and transport._closed
        assert not list(staging.glob(".tianlai-hall-*-bus.*.tmp"))
        real_verify(staging)

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
    monkeypatch.setattr(
        ensemble_module,
        "_verify_render_generation",
        verify_without_private_files,
    )
    ensemble_module.render_plan(
        plan,
        tmp_path / "mapped",
        write_stems=False,
        space=space,
    )

    assert _tree_payloads(tmp_path / "mapped") == _tree_payloads(
        tmp_path / "ram"
    )


def test_real_nonzero_hall_render_is_byte_exact_and_uses_reverb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    plan = _real_warm_pad_plan(frames=1_024, sample_rate=8_000)
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    from tianlai import space as space_module

    real_reverb = space_module.render_reverb_stereo
    reverb_calls = 0

    def observe_reverb(*args: Any, **kwargs: Any):
        nonlocal reverb_calls
        reverb_calls += 1
        return real_reverb(*args, **kwargs)

    monkeypatch.setattr(space_module, "render_reverb_stereo", observe_reverb)
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1 << 60,
    )
    ensemble_module.render_plan(
        plan,
        tmp_path / "ram",
        write_stems=False,
        space=space,
    )

    observed_transport = False
    real_try = ensemble_module._try_mapped_hall_mix_buses

    def observe_transport(*args: Any, **kwargs: Any):
        nonlocal observed_transport
        transport = real_try(*args, **kwargs)
        observed_transport = transport is not None
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
    ensemble_module.render_plan(
        plan,
        tmp_path / "mapped",
        write_stems=False,
        space=space,
    )

    assert observed_transport
    assert reverb_calls == 2
    assert _tree_payloads(tmp_path / "mapped") == _tree_payloads(
        tmp_path / "ram"
    )


def test_generation_rejects_invalid_mapped_hall_bus_shapes_and_dtypes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _empty_plan(frames=64)
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    total_frames = ensemble_module._mapped_hall_mix_buses_layout(plan, space)[0]
    mix = np.zeros((total_frames, 2), dtype=np.float64)
    send = np.zeros((total_frames, 2), dtype=np.float32)
    monkeypatch.setattr(
        ensemble_module,
        "_iter_raw_stems_in_plan_order",
        lambda *_args, **_kwargs: (_ for _ in ()),
    )

    with pytest.raises(ValueError, match="hall send bus"):
        ensemble_module._render_plan_generation(
            plan,
            tmp_path / "bad-send-dtype",
            write_stems=False,
            space=space,
            _dry_mix_bus=mix,
            _hall_send_bus=send.astype(np.float64),
        )
    with pytest.raises(ValueError, match="exact render length"):
        ensemble_module._render_plan_generation(
            plan,
            tmp_path / "bad-mix-shape",
            write_stems=False,
            space=space,
            _dry_mix_bus=mix[:-1],
            _hall_send_bus=send,
        )


def test_custom_space_never_enters_early_hall_optimizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()

    class CustomSpace:
        def tail_seconds(self, _sample_rate: int) -> float:
            raise AssertionError("custom space was called before generation")

    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: (_ for _ in ()).throw(AssertionError("pool was constructed")),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    assert (
        ensemble_module._try_mapped_hall_mix_buses(
            _empty_plan(),
            capture_plain_directory(stage),
            space=CustomSpace(),
            collaboration_mode=None,
        )
        is None
    )


def test_low_space_lease_falls_back_before_creating_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    plan = _empty_plan(frames=1_024)
    _frames, bus_bytes = ensemble_module._mapped_dry_mix_bus_layout(plan)
    free = slots_module._SCRATCH_FREE_RESERVE_BYTES + bus_bytes - 1
    monkeypatch.setattr(
        slots_module.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(free + 1, 1, free),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
        1,
    )

    transport = ensemble_module._try_mapped_dry_mix_bus(
        plan,
        capture_plain_directory(stage),
        space=None,
        collaboration_mode=None,
    )

    assert transport is None
    assert list(stage.iterdir()) == []


class _FakeLease:
    def __init__(
        self,
        directory: Path,
        *,
        scratch_bytes: int,
        volume_id: str,
        events: list[str] | None = None,
    ) -> None:
        self.scratch_directory = directory
        self.claim = SimpleNamespace(
            scratch_bytes=scratch_bytes,
            scratch_volume_id=volume_id,
        )
        self.events = events
        self.closed = False

    def close(self) -> None:
        if self.events is not None:
            self.events.append("lease")
        self.closed = True


class _FakePool:
    def __init__(self, lease: _FakeLease | None) -> None:
        self.lease = lease
        self.claim = None

    def reserve_session_scratch(self, claim):
        self.claim = claim
        return self.lease


@pytest.mark.parametrize(
    ("transport_kind", "error_kind"),
    (
        ("dry", "memory"),
        ("dry", "base"),
        ("hall", "memory"),
        ("hall", "base"),
    ),
)
def test_transport_constructor_failure_closes_resources_and_preserves_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transport_kind: str,
    error_kind: str,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan()
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    volume_id = ensemble_module.scratch_volume_identity(stage)
    if transport_kind == "dry":
        scratch_bytes = ensemble_module._mapped_dry_mix_bus_layout(plan)[1]
    else:
        scratch_bytes = ensemble_module._mapped_hall_mix_buses_layout(
            plan,
            space,
        )[3]

    class CleanupFailingLease(_FakeLease):
        def close(self) -> None:
            super().close()
            raise RuntimeError("injected lease cleanup failure")

    lease = CleanupFailingLease(
        stage,
        scratch_bytes=scratch_bytes,
        volume_id=volume_id,
    )
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: _FakePool(lease),
    )
    primary: BaseException = (
        MemoryError(f"{transport_kind} constructor memory failure")
        if error_kind == "memory"
        else LookupError(f"{transport_kind} constructor base failure")
    )
    captured: dict[str, Any] = {}

    if transport_kind == "dry":
        monkeypatch.setattr(
            ensemble_module,
            "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
            1,
        )

        def fail_factory(
            bus: Any,
            mapping: Any,
            temporary: Any,
            actual_lease: Any,
        ) -> Any:
            captured.update(
                bus=bus,
                mappings=(mapping,),
                handles=(temporary,),
                lease=actual_lease,
            )
            raise primary

        monkeypatch.setattr(
            ensemble_module,
            "_mapped_dry_mix_bus_transport_factory",
            fail_factory,
        )
        operation = lambda: ensemble_module._try_mapped_dry_mix_bus(
            plan,
            capture_plain_directory(stage),
            space=None,
            collaboration_mode=None,
        )
    else:
        monkeypatch.setattr(
            ensemble_module,
            "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
            1,
        )

        def fail_factory(
            bus: Any,
            send_bus: Any,
            mix_mapping: Any,
            send_mapping: Any,
            mix_temporary: Any,
            send_temporary: Any,
            actual_lease: Any,
        ) -> Any:
            captured.update(
                bus=bus,
                send_bus=send_bus,
                mappings=(mix_mapping, send_mapping),
                handles=(mix_temporary, send_temporary),
                lease=actual_lease,
            )
            raise primary

        monkeypatch.setattr(
            ensemble_module,
            "_mapped_hall_mix_buses_transport_factory",
            fail_factory,
        )
        operation = lambda: ensemble_module._try_mapped_hall_mix_buses(
            plan,
            capture_plain_directory(stage),
            space=space,
            collaboration_mode=None,
        )

    with pytest.raises(type(primary)) as caught:
        operation()

    assert caught.value is primary
    assert all(mapping.closed for mapping in captured["mappings"])
    assert all(handle.closed for handle in captured["handles"])
    assert captured["lease"] is lease and lease.closed
    assert any(
        "injected lease cleanup failure" in note
        for note in getattr(caught.value, "__notes__", ())
    )
    traceback = caught.value.__traceback__
    while traceback is not None and traceback.tb_frame.f_code.co_name != "fail_factory":
        traceback = traceback.tb_next
    assert traceback is not None
    # The primary traceback still owns the factory frame and its mapped-array
    # arguments.  Explicit close must therefore have succeeded independently
    # of reference counting or garbage collection.
    assert traceback.tb_frame.f_locals["bus"] is captured["bus"]
    assert not list(stage.iterdir())


def test_hall_low_space_falls_back_before_creating_either_mapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan(frames=1_024)
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    _total, _mix, _send, scratch_bytes = (
        ensemble_module._mapped_hall_mix_buses_layout(plan, space)
    )
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    free = slots_module._SCRATCH_FREE_RESERVE_BYTES + scratch_bytes - 1
    monkeypatch.setattr(
        slots_module.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(free + 1, 1, free),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    created = False

    def reject_create(**_kwargs: Any):
        nonlocal created
        created = True
        raise AssertionError("mapping creation followed denied admission")

    monkeypatch.setattr(ensemble_module.tempfile, "TemporaryFile", reject_create)
    assert (
        ensemble_module._try_mapped_hall_mix_buses(
            plan,
            capture_plain_directory(stage),
            space=space,
            collaboration_mode=None,
        )
        is None
    )
    assert not created


def test_hall_second_file_oserror_closes_first_file_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan()
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    _total, _mix, _send, scratch_bytes = (
        ensemble_module._mapped_hall_mix_buses_layout(plan, space)
    )
    lease = _FakeLease(
        stage,
        scratch_bytes=scratch_bytes,
        volume_id=ensemble_module.scratch_volume_identity(stage),
    )
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: _FakePool(lease),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    real_temporary_file = tempfile.TemporaryFile
    first_handle: Any | None = None
    calls = 0

    def fail_second_create(**kwargs: Any):
        nonlocal calls, first_handle
        calls += 1
        if calls == 2:
            raise PermissionError(errno.EACCES, "ordinary second create failure")
        first_handle = real_temporary_file(**kwargs)
        return first_handle

    monkeypatch.setattr(
        ensemble_module.tempfile,
        "TemporaryFile",
        fail_second_create,
    )
    assert (
        ensemble_module._try_mapped_hall_mix_buses(
            plan,
            capture_plain_directory(stage),
            space=space,
            collaboration_mode=None,
        )
        is None
    )
    assert first_handle is not None and first_handle.closed
    assert lease.closed


def test_hall_second_file_memory_error_is_hard_and_releases_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan()
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    _total, _mix, _send, scratch_bytes = (
        ensemble_module._mapped_hall_mix_buses_layout(plan, space)
    )
    lease = _FakeLease(
        stage,
        scratch_bytes=scratch_bytes,
        volume_id=ensemble_module.scratch_volume_identity(stage),
    )
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: _FakePool(lease),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    real_temporary_file = tempfile.TemporaryFile
    first_handle: Any | None = None
    calls = 0

    def fail_second_create(**kwargs: Any):
        nonlocal calls, first_handle
        calls += 1
        if calls == 2:
            raise MemoryError("mapped hall send")
        first_handle = real_temporary_file(**kwargs)
        return first_handle

    monkeypatch.setattr(
        ensemble_module.tempfile,
        "TemporaryFile",
        fail_second_create,
    )
    with pytest.raises(MemoryError, match="mapped hall send"):
        ensemble_module._try_mapped_hall_mix_buses(
            plan,
            capture_plain_directory(stage),
            space=space,
            collaboration_mode=None,
        )
    assert first_handle is not None and first_handle.closed
    assert lease.closed


def test_hall_second_file_truncate_enospc_falls_back_and_releases_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan()
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    _total, _mix, _send, scratch_bytes = (
        ensemble_module._mapped_hall_mix_buses_layout(plan, space)
    )
    lease = _FakeLease(
        stage,
        scratch_bytes=scratch_bytes,
        volume_id=ensemble_module.scratch_volume_identity(stage),
    )
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: _FakePool(lease),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    real_temporary_file = tempfile.TemporaryFile
    handles: list[Any] = []

    class FailingTruncateHandle:
        def __init__(self, handle: Any) -> None:
            self.handle = handle

        def truncate(self, _size: int) -> None:
            raise OSError(errno.ENOSPC, "injected full scratch volume")

        def close(self) -> None:
            self.handle.close()

    def create(**kwargs: Any) -> Any:
        handle = real_temporary_file(**kwargs)
        handles.append(handle)
        return handle if len(handles) == 1 else FailingTruncateHandle(handle)

    monkeypatch.setattr(ensemble_module.tempfile, "TemporaryFile", create)
    assert (
        ensemble_module._try_mapped_hall_mix_buses(
            plan,
            capture_plain_directory(stage),
            space=space,
            collaboration_mode=None,
        )
        is None
    )
    assert len(handles) == 2 and all(handle.closed for handle in handles)
    assert lease.closed


def test_hall_wrong_lease_directory_identity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    replacement = tmp_path / "other-stage"
    stage.mkdir()
    replacement.mkdir()
    plan = _empty_plan()
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    _total, _mix, _send, scratch_bytes = (
        ensemble_module._mapped_hall_mix_buses_layout(plan, space)
    )
    lease = _FakeLease(
        replacement,
        scratch_bytes=scratch_bytes,
        volume_id=ensemble_module.scratch_volume_identity(stage),
    )
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: _FakePool(lease),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    with pytest.raises(WorkerSlotError, match="directory identity changed"):
        ensemble_module._try_mapped_hall_mix_buses(
            plan,
            capture_plain_directory(stage),
            space=space,
            collaboration_mode=None,
        )
    assert lease.closed


def test_ordinary_mapping_creation_oserror_falls_back_to_ram(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan()
    _frames, bus_bytes = ensemble_module._mapped_dry_mix_bus_layout(plan)
    volume_id = ensemble_module.scratch_volume_identity(stage)
    lease = _FakeLease(
        stage,
        scratch_bytes=bus_bytes,
        volume_id=volume_id,
    )
    pool = _FakePool(lease)
    monkeypatch.setattr(ensemble_module, "WorkerSlotPool", lambda: pool)
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
        1,
    )

    def fail_create(**_kwargs):
        raise PermissionError(errno.EACCES, "ordinary create failure")

    monkeypatch.setattr(ensemble_module.tempfile, "TemporaryFile", fail_create)

    assert (
        ensemble_module._try_mapped_dry_mix_bus(
            plan,
            capture_plain_directory(stage),
            space=None,
            collaboration_mode="manual",
        )
        is None
    )
    assert pool.claim.scratch_bytes == bus_bytes
    assert lease.closed


def test_memory_error_is_hard_and_releases_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    plan = _empty_plan()
    _frames, bus_bytes = ensemble_module._mapped_dry_mix_bus_layout(plan)
    lease = _FakeLease(
        stage,
        scratch_bytes=bus_bytes,
        volume_id=ensemble_module.scratch_volume_identity(stage),
    )
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: _FakePool(lease),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
        1,
    )
    monkeypatch.setattr(
        ensemble_module.tempfile,
        "TemporaryFile",
        lambda **_kwargs: (_ for _ in ()).throw(MemoryError("mapped bus")),
    )

    with pytest.raises(MemoryError, match="mapped bus"):
        ensemble_module._try_mapped_dry_mix_bus(
            plan,
            capture_plain_directory(stage),
            space=None,
            collaboration_mode=None,
        )
    assert lease.closed


def test_wrong_lease_directory_identity_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    replacement = tmp_path / "other-stage"
    stage.mkdir()
    replacement.mkdir()
    plan = _empty_plan()
    _frames, bus_bytes = ensemble_module._mapped_dry_mix_bus_layout(plan)
    volume_id = ensemble_module.scratch_volume_identity(stage)
    lease = _FakeLease(
        replacement,
        scratch_bytes=bus_bytes,
        volume_id=volume_id,
    )
    monkeypatch.setattr(
        ensemble_module,
        "WorkerSlotPool",
        lambda: _FakePool(lease),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
        1,
    )

    with pytest.raises(WorkerSlotError, match="directory identity changed"):
        ensemble_module._try_mapped_dry_mix_bus(
            plan,
            capture_plain_directory(stage),
            space=None,
            collaboration_mode=None,
        )
    assert lease.closed


def test_cleanup_order_continues_and_preserves_first_error() -> None:
    events: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            events.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} close")

    first = ensemble_module._close_mapped_dry_mix_bus_resources(
        Resource("mapping", fail=True),
        Resource("handle", fail=True),
        Resource("lease"),
    )

    assert events == ["mapping", "handle", "lease"]
    assert isinstance(first, RuntimeError)
    assert str(first) == "mapping close"


def test_hall_cleanup_closes_all_mappings_then_handles_then_lease() -> None:
    events: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            events.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} close")

    first = ensemble_module._close_mapped_hall_mix_bus_resources(
        Resource("mix-mapping", fail=True),
        Resource("send-mapping", fail=True),
        Resource("mix-handle", fail=True),
        Resource("send-handle", fail=True),
        Resource("lease", fail=True),
    )

    assert events == [
        "mix-mapping",
        "send-mapping",
        "mix-handle",
        "send-handle",
        "lease",
    ]
    assert isinstance(first, RuntimeError)
    assert str(first) == "mix-mapping close"


def test_hall_generation_error_survives_mapping_cleanup_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fail: bool = False) -> None:
            self.name = name
            self.fail = fail

        def close(self) -> None:
            events.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} close")

    transport = ensemble_module._MappedHallMixBusesTransport(
        object(),
        object(),
        Resource("mix-mapping", fail=True),
        Resource("send-mapping"),
        Resource("mix-handle"),
        Resource("send-handle"),
        Resource("lease"),
    )
    monkeypatch.setattr(
        ensemble_module,
        "_try_mapped_hall_mix_buses",
        lambda *_args, **_kwargs: transport,
    )
    monkeypatch.setattr(
        ensemble_module,
        "_render_plan_generation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            LookupError("primary generation failure")
        ),
    )

    with pytest.warns(RuntimeWarning, match="cleanup failed"):
        with pytest.raises(LookupError, match="primary generation failure"):
            ensemble_module._render_plan_locked(
                object(),
                tmp_path / "published",
                space=object(),
            )
    assert events == [
        "mix-mapping",
        "send-mapping",
        "mix-handle",
        "send-handle",
        "lease",
    ]


def test_real_hall_transport_closes_when_traceback_retains_bus_views(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    captured: dict[str, Any] = {}

    def fail_generation(_plan: Any, _staging: Path, **kwargs: Any) -> Any:
        # These slices remain reachable from the exception traceback until the
        # assertion context exits.  Resource retirement must not depend on GC.
        mix_view = kwargs["_dry_mix_bus"][:, 0]
        send_view = kwargs["_hall_send_bus"][:, 1]
        captured["mix_mapping"] = kwargs["_dry_mix_bus"]._mmap
        captured["send_mapping"] = kwargs["_hall_send_bus"]._mmap
        assert mix_view.shape == send_view.shape
        raise LookupError("generation retained mapped hall views")

    monkeypatch.setattr(
        ensemble_module,
        "_render_plan_generation",
        fail_generation,
    )
    with pytest.raises(LookupError, match="retained mapped hall views"):
        ensemble_module._render_plan_locked(
            _empty_plan(),
            tmp_path / "published",
            space=SpaceConfig(room_size=0.0, predelay_ms=0.0),
        )

    assert captured["mix_mapping"].closed
    assert captured["send_mapping"].closed
    assert not list(tmp_path.rglob(".tianlai-hall-*-bus.*.tmp"))


def test_transport_is_closed_before_generation_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, Any] = {}

    class FakeTransport:
        def __init__(self, marker: Path) -> None:
            self.bus = object()
            self.marker = marker
            self.closed = False
            marker.write_bytes(b"visible temporary")

        def close(self) -> None:
            self.closed = True
            self.marker.unlink()

    def acquire(_plan, identity, **_kwargs):
        transport = FakeTransport(
            identity.path / ".tianlai-dry-mix-bus.visible.tmp"
        )
        observed["transport"] = transport
        return transport

    def generate(_plan, _staging, **kwargs):
        assert kwargs["_dry_mix_bus"] is observed["transport"].bus
        return object()

    def verify(staging: Path) -> None:
        transport = observed["transport"]
        assert transport.closed
        assert list(staging.glob(".tianlai-dry-mix-bus.*.tmp")) == []
        raise RuntimeError("verification checkpoint")

    monkeypatch.setattr(ensemble_module, "_try_mapped_dry_mix_bus", acquire)
    monkeypatch.setattr(ensemble_module, "_render_plan_generation", generate)
    monkeypatch.setattr(ensemble_module, "_verify_render_generation", verify)

    with pytest.raises(RuntimeError, match="verification checkpoint"):
        ensemble_module._render_plan_locked(object(), tmp_path / "published")


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-release contract")
def test_real_windows_mapping_releases_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES",
        1,
    )
    transport = ensemble_module._try_mapped_dry_mix_bus(
        _empty_plan(),
        capture_plain_directory(stage),
        space=None,
        collaboration_mode=None,
    )

    assert transport is not None
    assert list(stage.glob(".tianlai-dry-mix-bus.*.tmp"))
    transport.close()
    assert list(stage.iterdir()) == []
    stage.rmdir()
    assert not stage.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows hall-handle release contract")
def test_real_windows_hall_mappings_release_both_files_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    _bind_private_pool(monkeypatch, tmp_path / "ledger")
    monkeypatch.setattr(
        ensemble_module,
        "_MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES",
        1,
    )
    transport = ensemble_module._try_mapped_hall_mix_buses(
        _empty_plan(),
        capture_plain_directory(stage),
        space=SpaceConfig(room_size=0.0, predelay_ms=0.0),
        collaboration_mode=None,
    )

    assert transport is not None
    assert len(list(stage.glob(".tianlai-hall-*-bus.*.tmp"))) == 2
    transport.close()
    assert list(stage.iterdir()) == []
    stage.rmdir()
    assert not stage.exists()


def _windows_peak_memory() -> dict[str, int]:
    import ctypes
    from ctypes import wintypes

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ProcessMemoryCountersEx),
        wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    if not psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    ):
        raise ctypes.WinError()
    return {
        "peak_private_bytes": int(counters.PeakPagefileUsage),
        "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
    }


def _run_benchmark_child(mode: str, directory: Path) -> None:
    # 128 MiB is large enough to separate anonymous private commit from a
    # file-backed mapping while remaining practical for an opt-in CI run.
    bus_bytes = 128 * MIB
    frames = bus_bytes // 16
    started = time.perf_counter()
    if mode == "ram":
        bus = np.zeros((frames, 2), dtype=np.float64)
        bus += 0.125
        checksum = float(np.sum(bus[::4096]))
    elif mode == "mapped":
        stage = directory / "scratch"
        stage.mkdir()
        real_pool = WorkerSlotPool
        ensemble_module.WorkerSlotPool = lambda: real_pool(directory / "ledger")
        ensemble_module._MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES = 1
        plan = _empty_plan(frames=frames, sample_rate=48_000)
        transport = ensemble_module._try_mapped_dry_mix_bus(
            plan,
            capture_plain_directory(stage),
            space=None,
            collaboration_mode=None,
        )
        if transport is None:
            raise RuntimeError("mapped benchmark admission was unavailable")
        try:
            bus = transport.bus
            bus += 0.125
            checksum = float(np.sum(bus[::4096]))
        finally:
            transport.close()
    else:
        raise ValueError(mode)
    payload = {
        "mode": mode,
        "bus_bytes": bus_bytes,
        "checksum": checksum,
        "seconds": time.perf_counter() - started,
        **_windows_peak_memory(),
    }
    print(json.dumps(payload, sort_keys=True))


def _run_public_render_benchmark_child(mode: str, directory: Path) -> None:
    bus_bytes = 128 * MIB
    frames = bus_bytes // 16
    if mode == "mapped":
        real_pool = WorkerSlotPool
        ensemble_module.WorkerSlotPool = lambda: real_pool(directory / "ledger")
        ensemble_module._MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES = 1
    elif mode == "ram":
        ensemble_module._MAPPED_DRY_MIX_BUS_THRESHOLD_BYTES = 1 << 60
    else:
        raise ValueError(mode)

    output = directory / "rendered"
    started = time.perf_counter()
    ensemble_module.render_plan(
        _empty_plan(frames=frames, sample_rate=48_000),
        output,
        write_stems=False,
    )
    seconds = time.perf_counter() - started
    artifacts = {
        relative: hashlib.sha256(payload).hexdigest()
        for relative, payload in _tree_payloads(output).items()
    }
    private_temporary_entries = [
        path.name
        for path in directory.rglob("*")
        if path.name.startswith(".tianlai-dry-mix-bus.")
    ]
    print(
        json.dumps(
            {
                "mode": mode,
                "bus_bytes": bus_bytes,
                "seconds": seconds,
                "artifacts": artifacts,
                "private_temporary_entries": private_temporary_entries,
                **_windows_peak_memory(),
            },
            sort_keys=True,
        )
    )


def _run_hall_public_render_benchmark_child(mode: str, directory: Path) -> None:
    target_bus_bytes = 128 * MIB
    sample_rate = 48_000
    space = SpaceConfig(room_size=0.0, predelay_ms=0.0)
    tail_frames = int(np.ceil(space.tail_seconds(sample_rate) * sample_rate))
    total_frames = int(np.ceil(target_bus_bytes / 24))
    dry_frames = max(1, total_frames - tail_frames)
    plan = _real_warm_pad_plan(frames=dry_frames, sample_rate=sample_rate)
    layout = ensemble_module._mapped_hall_mix_buses_layout(plan, space)
    bus_bytes = layout[3]
    real_pool = WorkerSlotPool
    ensemble_module.WorkerSlotPool = lambda: real_pool(directory / "ledger")
    if mode == "mapped":
        ensemble_module._MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES = 1
    elif mode == "ram":
        ensemble_module._MAPPED_HALL_MIX_BUSES_THRESHOLD_BYTES = 1 << 60
    else:
        raise ValueError(mode)

    output = directory / "rendered"
    started = time.perf_counter()
    transport_hits = 0
    real_try = ensemble_module._try_mapped_hall_mix_buses

    def observe_transport(*args: Any, **kwargs: Any):
        nonlocal transport_hits
        transport = real_try(*args, **kwargs)
        if transport is not None:
            transport_hits += 1
        return transport

    ensemble_module._try_mapped_hall_mix_buses = observe_transport
    ensemble_module.render_plan(
        plan,
        output,
        write_stems=False,
        space=space,
    )
    seconds = time.perf_counter() - started
    artifacts = {
        relative: hashlib.sha256(payload).hexdigest()
        for relative, payload in _tree_payloads(output).items()
    }
    private_temporary_entries = [
        path.name
        for path in directory.rglob("*")
        if path.name.startswith(".tianlai-hall-")
    ]
    print(
        json.dumps(
            {
                "mode": mode,
                "bus_bytes": bus_bytes,
                "seconds": seconds,
                "artifacts": artifacts,
                "private_temporary_entries": private_temporary_entries,
                "transport_hits": transport_hits,
                **_windows_peak_memory(),
            },
            sort_keys=True,
        )
    )


@pytest.mark.skipif(
    os.name != "nt" or os.environ.get("TIANLAI_RUN_MAPPED_BUS_BENCHMARK") != "1",
    reason="opt-in 128 MiB Windows memory/time benchmark",
)
def test_mapped_bus_128mib_memory_time_benchmark(tmp_path: Path) -> None:
    results: dict[str, dict[str, Any]] = {}
    for mode in ("ram", "mapped"):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--benchmark-child",
                mode,
                str(tmp_path / mode),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        results[mode] = json.loads(completed.stdout.strip().splitlines()[-1])

    print(json.dumps(results, sort_keys=True))
    assert results["mapped"]["checksum"] == results["ram"]["checksum"]
    assert results["mapped"]["bus_bytes"] >= 128 * MIB
    assert results["mapped"]["peak_private_bytes"] + 64 * MIB < (
        results["ram"]["peak_private_bytes"]
    )
    assert results["mapped"]["seconds"] <= results["ram"]["seconds"] * 5 + 1


@pytest.mark.skipif(
    os.name != "nt"
    or os.environ.get("TIANLAI_RUN_MAPPED_PUBLIC_BENCHMARK") != "1",
    reason="opt-in 128 MiB alternating public-render benchmark",
)
def test_mapped_bus_128mib_public_render_ab_benchmark(tmp_path: Path) -> None:
    sequence: list[dict[str, Any]] = []
    for index, mode in enumerate(("ram", "mapped") * 3):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--public-render-benchmark-child",
                mode,
                str(tmp_path / f"{index}-{mode}"),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        sequence.append(json.loads(completed.stdout.strip().splitlines()[-1]))

    print(json.dumps(sequence, sort_keys=True))
    assert all(item["bus_bytes"] >= 128 * MIB for item in sequence)
    assert all(not item["private_temporary_entries"] for item in sequence)
    assert all(item["artifacts"] == sequence[0]["artifacts"] for item in sequence)
    assert all(
        item["transport_hits"] == (1 if item["mode"] == "mapped" else 0)
        for item in sequence
    )
    ram = [item for item in sequence if item["mode"] == "ram"]
    mapped = [item for item in sequence if item["mode"] == "mapped"]
    ram_seconds = statistics.median(item["seconds"] for item in ram)
    mapped_seconds = statistics.median(item["seconds"] for item in mapped)
    ram_private = statistics.median(
        item["peak_private_bytes"] for item in ram
    )
    mapped_private = statistics.median(
        item["peak_private_bytes"] for item in mapped
    )
    assert mapped_seconds <= ram_seconds * 1.05
    assert mapped_private + 64 * MIB < ram_private


@pytest.mark.skipif(
    os.name != "nt"
    or os.environ.get("TIANLAI_RUN_MAPPED_HALL_PUBLIC_BENCHMARK") != "1",
    reason="opt-in 128 MiB alternating public hall-render benchmark",
)
def test_mapped_hall_buses_128mib_public_render_ab_benchmark(
    tmp_path: Path,
) -> None:
    sequence: list[dict[str, Any]] = []
    for index, mode in enumerate(("ram", "mapped") * 3):
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--hall-public-render-benchmark-child",
                mode,
                str(tmp_path / f"{index}-{mode}"),
            ],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        sequence.append(json.loads(completed.stdout.strip().splitlines()[-1]))

    print(json.dumps(sequence, sort_keys=True))
    assert all(item["bus_bytes"] >= 128 * MIB for item in sequence)
    assert all(not item["private_temporary_entries"] for item in sequence)
    assert all(item["artifacts"] == sequence[0]["artifacts"] for item in sequence)
    ram = [item for item in sequence if item["mode"] == "ram"]
    mapped = [item for item in sequence if item["mode"] == "mapped"]
    ram_seconds = statistics.median(item["seconds"] for item in ram)
    mapped_seconds = statistics.median(item["seconds"] for item in mapped)
    ram_private = statistics.median(
        item["peak_private_bytes"] for item in ram
    )
    mapped_private = statistics.median(
        item["peak_private_bytes"] for item in mapped
    )
    assert mapped_seconds <= ram_seconds * 1.05
    assert mapped_private + 64 * MIB < ram_private


def _benchmark_main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(2)
    benchmark_directory = Path(sys.argv[3])
    benchmark_directory.mkdir(parents=True)
    if sys.argv[1] == "--benchmark-child":
        _run_benchmark_child(sys.argv[2], benchmark_directory)
    elif sys.argv[1] == "--public-render-benchmark-child":
        _run_public_render_benchmark_child(sys.argv[2], benchmark_directory)
    elif sys.argv[1] == "--hall-public-render-benchmark-child":
        _run_hall_public_render_benchmark_child(
            sys.argv[2],
            benchmark_directory,
        )
    else:
        raise SystemExit(2)


if __name__ == "__main__":
    _benchmark_main()
