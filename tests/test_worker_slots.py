from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import pytest

from tianlai import worker_slots as slots_module
from tianlai.worker_slots import (
    ChildSlotSpec,
    WorkerResourceClaim,
    WorkerSlotPool,
)


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
MIB = 1024 * 1024


def _claim(
    scratch: Path,
    *,
    owner: str | None = None,
    capacity: int = 4,
    worker_memory: int = 256 * MIB,
    coordinator_memory: int = 64 * MIB,
    memory_budget: int = 2 * 1024 * MIB,
    scratch_bytes: int = 8,
) -> WorkerResourceClaim:
    return WorkerResourceClaim(
        owner_id=owner or uuid.uuid4().hex,
        owner_cpu_capacity=capacity,
        worker_memory_bytes=worker_memory,
        coordinator_memory_bytes=coordinator_memory,
        memory_budget_bytes=memory_budget,
        scratch_bytes=scratch_bytes,
        scratch_directory=scratch,
    )


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    return environment


def test_pool_creates_permanent_split_lock_layout_and_reuses_it() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        expected = {pool.directory / "allocator.lock"}
        for index in range(4):
            slot = pool.directory / f"slot-{index}"
            expected.update(
                {
                    slot / "reservation.lock",
                    slot / "active.lock",
                    slot / "metadata",
                }
            )
        assert all(path.is_file() for path in expected)

        reservation = pool.reserve_exact((_claim(root),))
        assert reservation is not None
        slot = reservation.take()
        slot.close()
        reservation.close()
        assert all(path.is_file() for path in expected)
        assert WorkerSlotPool(pool.directory).directory == pool.directory


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime path only")
def test_windows_relative_runtime_path_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", "relative-runtime")
    with pytest.raises(slots_module.WorkerSlotError):
        slots_module.default_worker_slot_directory()


def test_batch_is_exact_and_owner_coordinator_is_counted_once() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        owner = uuid.uuid4().hex
        # 2 * 100 worker + one 100 coordinator == the 300 MiB budget.
        claims = tuple(
            _claim(
                root,
                owner=owner,
                capacity=2,
                worker_memory=100 * MIB,
                coordinator_memory=100 * MIB,
                memory_budget=300 * MIB,
            )
            for _ in range(2)
        )
        reservation = pool.reserve_exact(claims)
        assert reservation is not None
        slots = [reservation.take(), reservation.take()]
        # The owner's effective capacity is global: no second CLI gets one of
        # the two physical files left over.
        assert pool.reserve_exact((_claim(root),)) is None
        for slot in slots:
            slot.close()
        reservation.close()

        # Different owners pay both coordinator claims: 400 > 300 MiB.
        different = (
            _claim(
                root,
                owner=uuid.uuid4().hex,
                worker_memory=100 * MIB,
                coordinator_memory=100 * MIB,
                memory_budget=300 * MIB,
            ),
            _claim(
                root,
                owner=uuid.uuid4().hex,
                worker_memory=100 * MIB,
                coordinator_memory=100 * MIB,
                memory_budget=300 * MIB,
            ),
        )
        assert pool.reserve_exact(different) is None


def test_same_volume_scratch_aggregates_and_distinct_volumes_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first = root / "first"
        second = root / "second"
        first.mkdir()
        second.mkdir()
        pool = WorkerSlotPool(root / "pool")
        usable = 1000

        monkeypatch.setattr(
            slots_module.shutil,
            "disk_usage",
            lambda path: shutil._ntuple_diskusage(
                10_000, 9_000 - slots_module._SCRATCH_FREE_RESERVE_BYTES,
                usable + slots_module._SCRATCH_FREE_RESERVE_BYTES,
            ),
        )
        monkeypatch.setattr(
            slots_module,
            "scratch_volume_identity",
            lambda path: "same-volume",
        )
        same = (
            _claim(first, scratch_bytes=600),
            _claim(second, scratch_bytes=600),
        )
        assert pool.reserve_exact(same) is None

        monkeypatch.setattr(
            slots_module,
            "scratch_volume_identity",
            # Production resolves each existing scratch directory before
            # asking for its volume identity.  Compare filesystem identity so
            # a real Windows 8.3 TEMP spelling does not make both test paths
            # fall into the synthetic ``volume-b`` bucket.
            lambda path: (
                "volume-a" if os.path.samefile(path, first) else "volume-b"
            ),
        )
        separate = pool.reserve_exact(same)
        assert separate is not None
        held = [separate.take(), separate.take()]
        for slot in held:
            slot.close()
        separate.close()


def test_busy_slot_with_corrupt_ledger_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        paths = pool._paths(0)
        lock = slots_module._FileLock(paths.reservation, blocking=False)
        try:
            paths.metadata.write_bytes(b"corrupt")
            assert pool.reserve_exact((_claim(root),)) is None
        finally:
            lock.close()
        # A fully free slot ignores stale metadata and overwrites it safely.
        recovered = pool.reserve_exact((_claim(root),))
        assert recovered is not None
        slot = recovered.take()
        slot.close()
        recovered.close()


@pytest.mark.parametrize(
    ("field", "malformed"),
    [("owner_id", 123), ("scratch_volume_id", ["volume"])],
)
def test_checksum_valid_ledger_rejects_non_string_identity_fields(
    field: str,
    malformed: object,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        reservation = pool.reserve_exact((_claim(root),))
        assert reservation is not None
        slot = reservation.take()
        metadata_path = pool._paths(slot.index).metadata
        document = slots_module._read_metadata(metadata_path)
        document["claim"][field] = malformed
        slots_module._write_metadata(metadata_path, document)
        try:
            assert pool.reserve_exact((_claim(root),)) is None
        finally:
            slot.close()
            reservation.close()


def test_checksum_valid_ledger_rejects_trailing_bytes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        reservation = pool.reserve_exact((_claim(root),))
        assert reservation is not None
        slot = reservation.take()
        metadata_path = pool._paths(slot.index).metadata
        with metadata_path.open("ab") as target:
            target.write(b"trailing")
        try:
            assert pool.reserve_exact((_claim(root),)) is None
        finally:
            slot.close()
            reservation.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows per-user fallback only")
def test_windows_pool_has_no_shared_temp_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    with pytest.raises(slots_module.WorkerSlotError):
        slots_module.default_worker_slot_directory()


@pytest.mark.skipif(os.name != "nt", reason="Windows volume GUID only")
def test_windows_volume_identity_fails_closed_without_guid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    class _Kernel32WithoutGuid:
        @staticmethod
        def GetVolumePathNameW(path: object, target: object, size: int) -> int:
            del path, size
            target.value = "C:\\"
            return 1

        @staticmethod
        def GetVolumeNameForVolumeMountPointW(
            path: object, target: object, size: int
        ) -> int:
            del path, target, size
            return 0

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *args, **kwargs: _Kernel32WithoutGuid(),
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        with pytest.raises(slots_module.WorkerSlotError):
            slots_module.scratch_volume_identity(Path(temporary_directory))


def test_failed_multislot_publication_releases_every_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        original = slots_module._write_metadata
        writes = 0

        def fail_second(path: Path, document: dict[str, object]) -> None:
            nonlocal writes
            writes += 1
            if writes == 2:
                raise OSError("deliberate metadata publication failure")
            original(path, document)

        monkeypatch.setattr(slots_module, "_write_metadata", fail_second)
        assert pool.reserve_exact((_claim(root), _claim(root))) is None
        monkeypatch.setattr(slots_module, "_write_metadata", original)
        recovered = pool.reserve_exact(tuple(_claim(root) for _ in range(4)))
        assert recovered is not None
        held = [recovered.take() for _ in range(4)]
        for slot in held:
            slot.close()
        recovered.close()


def test_scratch_path_directory_to_file_race_falls_back_to_no_slots() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        scratch = root / "scratch"
        scratch.mkdir()
        claim = _claim(scratch)
        scratch.rmdir()
        scratch.write_text("replaced", encoding="ascii")
        pool = WorkerSlotPool(root / "pool")
        assert pool.reserve_exact((claim,)) is None


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork is unavailable")
def test_after_fork_rebuilds_thread_locks_held_by_vanished_threads() -> None:
    import select

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        claim = _claim(root, capacity=1)
        locks_held = threading.Event()
        release_locks = threading.Event()

        def hold_parent_thread_locks() -> None:
            with slots_module._LOCAL_ALLOCATOR_LOCK:
                with slots_module._LIVE_LOCKS_LOCK:
                    locks_held.set()
                    release_locks.wait(timeout=10)

        holder = threading.Thread(target=hold_parent_thread_locks)
        holder.start()
        assert locks_held.wait(timeout=5)
        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                reservation = pool.reserve_exact((claim,))
                if reservation is None:
                    os.write(write_fd, b"0")
                else:
                    slot = reservation.take()
                    slot.close()
                    reservation.close()
                    os.write(write_fd, b"1")
            except BaseException:
                os.write(write_fd, b"E")
            finally:
                os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        try:
            readable, _, _ = select.select([read_fd], [], [], 5.0)
            assert readable, "fork child inherited a permanently locked mutex"
            assert os.read(read_fd, 1) == b"1"
            waited_pid, status = os.waitpid(child_pid, 0)
            assert waited_pid == child_pid
            assert os.waitstatus_to_exitcode(status) == 0
            child_pid = 0
        finally:
            os.close(read_fd)
            if child_pid:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            release_locks.set()
            holder.join(timeout=5)
        assert not holder.is_alive()


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="real OS locking is unsupported on this platform",
)
def test_late_child_rejects_reused_slot_generation() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        old_reservation = pool.reserve_exact((_claim(root),))
        assert old_reservation is not None
        old_slot = old_reservation.take()
        old_spec = old_slot.child_spec
        old_slot.close()
        old_reservation.close()

        new_reservation = pool.reserve_exact((_claim(root),))
        assert new_reservation is not None
        new_slot = new_reservation.take()
        assert new_slot.index == old_slot.index
        assert new_slot.token != old_slot.token
        code = r"""
import json, sys
from pathlib import Path
from tianlai.worker_slots import ChildSlotSpec, claim_reserved_worker_slot
raw = json.loads(sys.argv[1])
spec = ChildSlotSpec(Path(raw['directory']), raw['slot'], raw['token'], raw['parent'])
try:
    claim_reserved_worker_slot(spec)
except Exception:
    raise SystemExit(19)
raise SystemExit(0)
"""
        payload = json.dumps(
            {
                "directory": str(old_spec.directory),
                "slot": old_spec.slot_index,
                "token": old_spec.token,
                "parent": old_spec.parent_pid,
            }
        )
        late = subprocess.run(
            [PYTHON, "-c", code, payload],
            cwd=ROOT,
            env=_child_environment(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert late.returncode == 19, late.stderr

        # The rejected old generation released active.lock, so the current
        # child can still acknowledge the durable new token.
        new_spec = new_slot.child_spec
        current_payload = json.dumps(
            {
                "directory": str(new_spec.directory),
                "slot": new_spec.slot_index,
                "token": new_spec.token,
                "parent": new_spec.parent_pid,
            }
        )
        current = subprocess.run(
            [PYTHON, "-c", code, current_payload],
            cwd=ROOT,
            env=_child_environment(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert current.returncode == 0, current.stderr
        new_slot.close()
        new_reservation.close()


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="real OS locking is unsupported on this platform",
)
def test_child_active_lock_and_parent_reservation_overlap_lifetimes() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        reservation = pool.reserve_exact((_claim(root),))
        assert reservation is not None
        slot = reservation.take()
        spec = slot.child_spec
        code = r"""
import json, sys, time
from pathlib import Path
from tianlai.worker_slots import ChildSlotSpec, claim_reserved_worker_slot
raw = json.loads(sys.argv[1])
spec = ChildSlotSpec(Path(raw['directory']), raw['slot'], raw['token'], raw['parent'])
active = claim_reserved_worker_slot(spec)
print('READY', flush=True)
sys.stdin.buffer.read()
active.close()
"""
        payload = json.dumps(
            {
                "directory": str(spec.directory),
                "slot": spec.slot_index,
                "token": spec.token,
                "parent": spec.parent_pid,
            }
        )
        child = subprocess.Popen(
            [PYTHON, "-c", code, payload],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env=_child_environment(),
        )
        try:
            slot.wait_for_active(child)
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "READY"
            # Occupy the other three slots so the assertion is about the
            # overlapping slot, not merely spare physical capacity.
            others = pool.reserve_exact(tuple(_claim(root) for _ in range(3)))
            assert others is not None
            held_others = [others.take() for _ in range(3)]
            assert pool.reserve_exact((_claim(root),)) is None

            assert child.stdin is not None
            child.stdin.close()
            assert child.wait(timeout=5) == 0
            # Child is gone, but parent reservation deliberately covers the
            # still-owned result/scratch lifetime.
            assert pool.reserve_exact((_claim(root),)) is None
            slot.close()
            replacement = pool.reserve_exact((_claim(root),))
            assert replacement is not None
            replacement_slot = replacement.take()
            replacement_slot.close()
            replacement.close()
            for item in held_others:
                item.close()
            others.close()
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            slot.close()
            reservation.close()


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="real OS locking is unsupported on this platform",
)
def test_parent_crash_leaves_child_counted_until_child_exits() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool_path = root / "pool"
        ready_path = root / "ready.json"
        child_code = r"""
import json, sys, time
from pathlib import Path
from tianlai.worker_slots import ChildSlotSpec, claim_reserved_worker_slot
raw = json.loads(sys.argv[1])
active = claim_reserved_worker_slot(ChildSlotSpec(Path(raw['directory']), raw['slot'], raw['token'], raw['parent']))
Path(raw['active']).write_text('active', encoding='ascii')
time.sleep(1.5)
active.close()
"""
        parent_code = r"""
import json, os, subprocess, sys, time, uuid
from pathlib import Path
from tianlai.worker_slots import WorkerResourceClaim, WorkerSlotPool
pool_path, scratch, ready, child_code = map(Path, sys.argv[1:5])
pool = WorkerSlotPool(pool_path)
claim = WorkerResourceClaim(uuid.uuid4().hex, 1, 268435456, 67108864, 2147483648, 8, scratch)
reservation = pool.reserve_exact((claim,))
slot = reservation.take()
spec = slot.child_spec
active_path = scratch / 'child-active'
payload = json.dumps({'directory': str(spec.directory), 'slot': spec.slot_index, 'token': spec.token, 'parent': spec.parent_pid, 'active': str(active_path)})
child = subprocess.Popen([sys.executable, '-c', child_code.read_text(encoding='utf-8'), payload], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
slot.wait_for_active(child)
ready.write_text(json.dumps({'child_pid': child.pid, 'active': str(active_path)}), encoding='utf-8')
os._exit(23)
"""
        child_script = root / "child_code.py.txt"
        child_script.write_text(child_code, encoding="utf-8")
        parent = subprocess.Popen(
            [
                PYTHON,
                "-c",
                parent_code,
                str(pool_path),
                str(root),
                str(ready_path),
                str(child_script),
            ],
            cwd=ROOT,
            env=_child_environment(),
        )
        deadline = time.monotonic() + 10
        while not ready_path.exists() and time.monotonic() < deadline:
            if parent.poll() is not None and parent.returncode != 23:
                break
            time.sleep(0.02)
        assert ready_path.is_file()
        assert parent.wait(timeout=5) == 23
        details = json.loads(ready_path.read_text(encoding="utf-8"))
        assert Path(details["active"]).is_file()

        pool = WorkerSlotPool(pool_path)
        # The owner capacity is one, so the orphan child's active.lock keeps
        # every competing batch on the serial path after parent os._exit.
        assert pool.reserve_exact((_claim(root, capacity=1),)) is None
        time.sleep(1.8)
        recovered = pool.reserve_exact((_claim(root, capacity=1),))
        assert recovered is not None
        recovered_slot = recovered.take()
        recovered_slot.close()
        recovered.close()


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="real OS locking is unsupported on this platform",
)
def test_two_processes_never_split_one_exact_four_worker_batch() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = root / "pool"
        go = root / "go"
        code = r"""
import json, sys, time, uuid
from pathlib import Path
from tianlai.worker_slots import WorkerResourceClaim, WorkerSlotPool
pool_path, scratch, go = map(Path, sys.argv[1:4])
while not go.exists(): time.sleep(0.005)
owner = uuid.uuid4().hex
claims = tuple(WorkerResourceClaim(owner, 4, 268435456, 67108864, 2147483648, 8, scratch) for _ in range(4))
reservation = WorkerSlotPool(pool_path).reserve_exact(claims)
print('1' if reservation is not None else '0', flush=True)
if reservation is not None:
    slots = [reservation.take() for _ in range(4)]
    time.sleep(0.5)
    for slot in slots: slot.close()
    reservation.close()
"""
        processes = [
            subprocess.Popen(
                [PYTHON, "-c", code, str(pool), str(root), str(go)],
                cwd=ROOT,
                env=_child_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        go.write_text("go", encoding="ascii")
        try:
            results = [process.communicate(timeout=10) for process in processes]
            outputs = []
            for process, (stdout, stderr) in zip(
                processes, results, strict=True
            ):
                assert process.returncode == 0, stderr
                outputs.append(stdout.strip())
            assert sorted(outputs) == ["0", "1"]
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
