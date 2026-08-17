from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

from tianlai import worker_slots as slots_module
from tianlai.worker_slots import (
    SessionScratchClaim,
    WorkerResourceClaim,
    WorkerSlotPool,
)


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
MIB = 1024 * 1024


def _worker_claim(
    scratch: Path,
    *,
    scratch_bytes: int,
) -> WorkerResourceClaim:
    return WorkerResourceClaim(
        owner_id=uuid.uuid4().hex,
        owner_cpu_capacity=4,
        worker_memory_bytes=256 * MIB,
        coordinator_memory_bytes=64 * MIB,
        memory_budget_bytes=2 * 1024 * MIB,
        scratch_bytes=scratch_bytes,
        scratch_directory=scratch,
    )


def _child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT) + os.pathsep + environment.get(
        "PYTHONPATH", ""
    )
    return environment


def _fixed_scratch_budget(
    monkeypatch: pytest.MonkeyPatch,
    *,
    usable: int,
    volume: str = "volume-a",
) -> None:
    free = usable + slots_module._SCRATCH_FREE_RESERVE_BYTES
    monkeypatch.setattr(
        slots_module.shutil,
        "disk_usage",
        lambda path: shutil._ntuple_diskusage(free + 10_000, 10_000, free),
    )
    monkeypatch.setattr(
        slots_module,
        "scratch_volume_identity",
        lambda path: volume,
    )


def _release_worker_reservation(reservation: object) -> None:
    assert reservation is not None
    slot = reservation.take()
    slot.close()
    reservation.close()


def test_session_lease_layout_lifetime_and_worker_capacity_are_independent() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        session_directory = pool.directory / "session-scratch"
        assert list(session_directory.iterdir()) == []

        lease = pool.reserve_session_scratch(SessionScratchClaim(1, root))
        assert lease is not None
        # The first allocator probe creates fixed lock names for all slots;
        # metadata exists only for the selected generation.
        session_files = {path.name for path in session_directory.iterdir()}
        assert len(session_files) == slots_module._SESSION_SLOT_COUNT + 1
        assert not lease.closed
        assert lease.scratch_directory == root.resolve()
        assert lease.claim.scratch_bytes == 1

        reservation = pool.reserve_exact(
            tuple(_worker_claim(root, scratch_bytes=1) for _ in range(4))
        )
        assert reservation is not None
        workers = [reservation.take() for _ in range(4)]
        for worker in workers:
            worker.close()
        reservation.close()

        index = lease.index
        lease.close()
        assert lease.closed
        replacement = pool.reserve_session_scratch(SessionScratchClaim(1, root))
        assert replacement is not None
        assert replacement.index == index
        replacement.close()


def test_session_and_worker_claims_aggregate_in_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        _fixed_scratch_budget(monkeypatch, usable=1_000)

        session = pool.reserve_session_scratch(SessionScratchClaim(600, root))
        assert session is not None
        assert pool.reserve_session_scratch(SessionScratchClaim(401, root)) is None
        assert pool.reserve_exact(
            (_worker_claim(root, scratch_bytes=401),)
        ) is None
        exact_worker = pool.reserve_exact(
            (_worker_claim(root, scratch_bytes=400),)
        )
        _release_worker_reservation(exact_worker)
        session.close()

        worker = pool.reserve_exact((_worker_claim(root, scratch_bytes=600),))
        assert worker is not None
        held_worker = worker.take()
        assert pool.reserve_session_scratch(SessionScratchClaim(401, root)) is None
        exact_session = pool.reserve_session_scratch(
            SessionScratchClaim(400, root)
        )
        assert exact_session is not None
        exact_session.close()
        held_worker.close()
        worker.close()


def test_session_and_worker_claims_use_distinct_volume_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        first = root / "first"
        second = root / "second"
        first.mkdir()
        second.mkdir()
        pool = WorkerSlotPool(root / "pool")
        free = 1_000 + slots_module._SCRATCH_FREE_RESERVE_BYTES
        monkeypatch.setattr(
            slots_module.shutil,
            "disk_usage",
            lambda path: shutil._ntuple_diskusage(free + 10_000, 10_000, free),
        )
        monkeypatch.setattr(
            slots_module,
            "scratch_volume_identity",
            lambda path: (
                "volume-a" if os.path.samefile(path, first) else "volume-b"
            ),
        )

        session = pool.reserve_session_scratch(SessionScratchClaim(800, first))
        assert session is not None
        worker = pool.reserve_exact((_worker_claim(second, scratch_bytes=800),))
        _release_worker_reservation(worker)
        session.close()


def test_active_corrupt_session_ledger_blocks_both_admission_paths() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        paths = pool._session_paths(0)
        lock = slots_module._FileLock(paths.lease, blocking=False)
        try:
            paths.metadata.write_bytes(b"corrupt")
            assert pool.reserve_session_scratch(SessionScratchClaim(1, root)) is None
            assert pool.reserve_exact(
                (_worker_claim(root, scratch_bytes=1),)
            ) is None
        finally:
            lock.close()

        # Once the authoritative lock is free, its stale bytes are not a live
        # claim and the next generation may safely overwrite them.
        recovered = pool.reserve_session_scratch(SessionScratchClaim(1, root))
        assert recovered is not None
        recovered.close()


def test_checksum_valid_malformed_active_session_ledger_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        lease = pool.reserve_session_scratch(SessionScratchClaim(1, root))
        assert lease is not None
        metadata = pool._session_paths(lease.index).metadata
        document = slots_module._read_metadata(metadata)
        document["claim"]["scratch_volume_id"] = ["not-a-string"]
        slots_module._write_metadata(metadata, document)
        try:
            assert pool.reserve_session_scratch(SessionScratchClaim(1, root)) is None
            assert pool.reserve_exact(
                (_worker_claim(root, scratch_bytes=1),)
            ) is None
        finally:
            lease.close()


def test_active_corrupt_worker_ledger_blocks_session_admission() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        paths = pool._paths(0)
        lock = slots_module._FileLock(paths.reservation, blocking=False)
        try:
            paths.metadata.write_bytes(b"corrupt")
            assert pool.reserve_session_scratch(SessionScratchClaim(1, root)) is None
        finally:
            lock.close()


def test_session_lease_subtracts_fixed_reserve_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        _fixed_scratch_budget(monkeypatch, usable=99)
        assert pool.reserve_session_scratch(SessionScratchClaim(100, root)) is None
        exact = pool.reserve_session_scratch(SessionScratchClaim(99, root))
        assert exact is not None
        exact.close()


def test_session_directory_replacement_and_identity_change_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        scratch = root / "scratch"
        scratch.mkdir()
        pool = WorkerSlotPool(root / "pool")
        claim = SessionScratchClaim(1, scratch)
        scratch.rmdir()
        scratch.write_text("replacement", encoding="ascii")
        assert pool.reserve_session_scratch(claim) is None

        scratch.unlink()
        scratch.mkdir()
        identities = iter(("volume-a", "volume-b"))
        monkeypatch.setattr(
            slots_module,
            "scratch_volume_identity",
            lambda path: next(identities),
        )
        assert pool.reserve_session_scratch(SessionScratchClaim(1, scratch)) is None


def test_session_reservation_is_non_blocking_inside_one_process() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        with slots_module._LOCAL_ALLOCATOR_LOCK:
            started = time.monotonic()
            assert pool.reserve_session_scratch(SessionScratchClaim(1, root)) is None
            assert time.monotonic() - started < 0.5


def test_failed_session_metadata_publication_releases_every_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        original = slots_module._write_metadata

        def fail(path: Path, document: dict[str, object]) -> None:
            del path, document
            raise OSError("deliberate session metadata failure")

        monkeypatch.setattr(slots_module, "_write_metadata", fail)
        assert pool.reserve_session_scratch(SessionScratchClaim(1, root)) is None
        monkeypatch.setattr(slots_module, "_write_metadata", original)

        recovered = pool.reserve_session_scratch(SessionScratchClaim(1, root))
        assert recovered is not None
        recovered.close()
        worker = pool.reserve_exact((_worker_claim(root, scratch_bytes=1),))
        _release_worker_reservation(worker)


def test_memory_error_is_not_downgraded_to_optional_lease_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")

        def fail(path: Path) -> object:
            del path
            raise MemoryError("deliberate")

        monkeypatch.setattr(slots_module, "_scratch_volume_facts", fail)
        with pytest.raises(MemoryError, match="deliberate"):
            pool.reserve_session_scratch(SessionScratchClaim(1, root))


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork is unavailable")
def test_fork_child_cannot_release_or_reuse_parent_session_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import select

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool = WorkerSlotPool(root / "pool")
        _fixed_scratch_budget(monkeypatch, usable=1_000)
        lease = pool.reserve_session_scratch(SessionScratchClaim(600, root))
        assert lease is not None

        read_fd, write_fd = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(read_fd)
            try:
                lease.close()
                competing = pool.reserve_session_scratch(
                    SessionScratchClaim(500, root)
                )
                os.write(write_fd, b"0" if competing is None else b"1")
                if competing is not None:
                    competing.close()
            except BaseException:
                os.write(write_fd, b"E")
            finally:
                os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        try:
            readable, _, _ = select.select([read_fd], [], [], 5.0)
            assert readable
            assert os.read(read_fd, 1) == b"0"
            waited, status = os.waitpid(child_pid, 0)
            assert waited == child_pid
            assert os.waitstatus_to_exitcode(status) == 0
            child_pid = 0
        finally:
            os.close(read_fd)
            if child_pid:
                os.kill(child_pid, 9)
                os.waitpid(child_pid, 0)
        assert pool.reserve_session_scratch(SessionScratchClaim(500, root)) is None
        lease.close()
        recovered = pool.reserve_session_scratch(SessionScratchClaim(500, root))
        assert recovered is not None
        recovered.close()


@pytest.mark.skipif(
    sys.platform not in {"win32", "linux", "darwin"},
    reason="real OS locking is unsupported on this platform",
)
def test_session_lease_is_released_by_process_crash() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool_path = root / "pool"
        pool = WorkerSlotPool(pool_path)
        usable = max(
            1,
            shutil.disk_usage(root).free
            - slots_module._SCRATCH_FREE_RESERVE_BYTES,
        )
        claim_bytes = max(1, usable * 3 // 5)
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
        child = subprocess.Popen(
            [PYTHON, "-c", code, str(pool_path), str(root), str(claim_bytes)],
            cwd=ROOT,
            env=_child_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "READY"
            assert pool.reserve_session_scratch(
                SessionScratchClaim(claim_bytes, root)
            ) is None
            assert child.stdin is not None
            child.stdin.write("x")
            child.stdin.flush()
            assert child.wait(timeout=10) == 23
            recovered = pool.reserve_session_scratch(
                SessionScratchClaim(claim_bytes, root)
            )
            assert recovered is not None
            recovered.close()
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="POSIX canonical runtime only")
def test_posix_runtime_environment_cannot_split_session_scratch_ledger() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        usable = max(
            1,
            shutil.disk_usage(root).free
            - slots_module._SCRATCH_FREE_RESERVE_BYTES,
        )
        claim_bytes = max(1, usable * 3 // 5)
        go = root / "go"
        environments: list[dict[str, str]] = []
        for index in range(2):
            runtime = root / f"runtime-{index}"
            temporary = root / f"temporary-{index}"
            runtime.mkdir(mode=0o700)
            temporary.mkdir(mode=0o700)
            environment = _child_environment()
            environment.update(
                {
                    "XDG_RUNTIME_DIR": str(runtime),
                    "TMPDIR": str(temporary),
                    "TEMP": str(temporary),
                    "TMP": str(temporary),
                }
            )
            environments.append(environment)

        code = r"""
import json, sys, time
from pathlib import Path
from tianlai.worker_slots import SessionScratchClaim, WorkerSlotPool
scratch, go, amount = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
while not go.exists(): time.sleep(0.005)
pool = WorkerSlotPool()
lease = pool.reserve_session_scratch(SessionScratchClaim(amount, scratch))
print(json.dumps({'pool': str(pool.directory), 'accepted': lease is not None}), flush=True)
if lease is not None: time.sleep(0.5)
"""
        children = [
            subprocess.Popen(
                [PYTHON, "-c", code, str(root), str(go), str(claim_bytes)],
                cwd=ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for environment in environments
        ]
        go.write_text("go", encoding="ascii")
        try:
            results = [child.communicate(timeout=10) for child in children]
            documents = []
            for child, (stdout, stderr) in zip(children, results, strict=True):
                assert child.returncode == 0, stderr
                documents.append(json.loads(stdout))
            assert len({item["pool"] for item in documents}) == 1
            assert sorted(item["accepted"] for item in documents) == [False, True]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="real Windows locking only")
def test_windows_two_processes_never_oversubscribe_one_volume() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool_path = root / "pool"
        WorkerSlotPool(pool_path)
        usable = max(
            1,
            shutil.disk_usage(root).free
            - slots_module._SCRATCH_FREE_RESERVE_BYTES,
        )
        claim_bytes = max(1, usable * 3 // 5)
        go = root / "go"
        code = r"""
import sys, time
from pathlib import Path
from tianlai.worker_slots import SessionScratchClaim, WorkerSlotPool
pool, scratch, go, amount = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), int(sys.argv[4])
while not go.exists(): time.sleep(0.005)
lease = WorkerSlotPool(pool).reserve_session_scratch(SessionScratchClaim(amount, scratch))
print('1' if lease is not None else '0', flush=True)
if lease is not None: time.sleep(0.5)
"""
        children = [
            subprocess.Popen(
                [
                    PYTHON,
                    "-c",
                    code,
                    str(pool_path),
                    str(root),
                    str(go),
                    str(claim_bytes),
                ],
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
            results = [child.communicate(timeout=10) for child in children]
            outputs: list[str] = []
            for child, (stdout, stderr) in zip(children, results, strict=True):
                assert child.returncode == 0, stderr
                outputs.append(stdout.strip())
            assert sorted(outputs) == ["0", "1"]
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)


@pytest.mark.skipif(os.name != "nt", reason="real Windows locking only")
def test_windows_session_claim_blocks_cross_process_worker_scratch() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        pool_path = root / "pool"
        pool = WorkerSlotPool(pool_path)
        usable = max(
            1,
            shutil.disk_usage(root).free
            - slots_module._SCRATCH_FREE_RESERVE_BYTES,
        )
        claim_bytes = max(1, usable * 3 // 5)
        code = r"""
import sys
from pathlib import Path
from tianlai.worker_slots import SessionScratchClaim, WorkerSlotPool
pool, scratch, amount = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
lease = WorkerSlotPool(pool).reserve_session_scratch(SessionScratchClaim(amount, scratch))
if lease is None: raise SystemExit(17)
print('READY', flush=True)
sys.stdin.buffer.read()
"""
        child = subprocess.Popen(
            [PYTHON, "-c", code, str(pool_path), str(root), str(claim_bytes)],
            cwd=ROOT,
            env=_child_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "READY"
            assert pool.reserve_exact(
                (_worker_claim(root, scratch_bytes=claim_bytes),)
            ) is None

            assert child.stdin is not None
            child.stdin.close()
            assert child.wait(timeout=10) == 0
            recovered = pool.reserve_exact(
                (_worker_claim(root, scratch_bytes=claim_bytes),)
            )
            _release_worker_reservation(recovered)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
