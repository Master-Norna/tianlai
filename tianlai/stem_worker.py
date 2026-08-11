"""Private, managed subprocess runtime for rendering raw ensemble stems.

This module is deliberately not a user-facing command.  The parent process
starts ``sys.executable -m tianlai.stem_worker`` and sends either one private
JSON job or a sequence of length-framed JSON jobs on stdin.  Trusted catalogue
jobs may reuse an idle child, while every job still revalidates its immutable
inputs and constructs and closes a fresh instrument.  Using a managed module
subprocess, rather than ``multiprocessing``'s ``spawn`` start method, avoids
re-executing an embedding application's ``__main__`` module on Windows.

The child never publishes a WAV, cache entry, receipt, or other user artifact.
It renders little-endian float32 stereo samples directly into the parent's
anonymous ``TemporaryFile`` between unpredictable per-job markers.  Standard
output and error are redirected before custom instrument code runs, so native
or local-factory diagnostics cannot corrupt the protocol or grow scratch
without a bound.
"""

from __future__ import annotations

import atexit
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, BinaryIO, Callable
import uuid

from .canonical_json import canonical_json_bytes
from .events import parse_performance_document
from .instrument import create_instrument
from .plain_file import read_plain_file_bytes
from .render_parallelism import (
    _is_trusted_managed_worker_manifest,
    automatic_worker_capacity,
)
from .renderer import (
    _prefer_frame_stream_path,
    render_document,
    render_document_blocks,
)
from .stem_cache import (
    PROCESS_SOURCE_TREE_SHA256,
    current_source_tree_matches,
)
from .worker_slots import ReservedWorkerSlot, scratch_volume_identity


_PROTOCOL_FORMAT = "tianlai.private_stem_worker"
_PROTOCOL_VERSION = 1
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_BEGIN_PREFIX = b"\x00TIANLAI-STEM-BEGIN-v1:"
_END_PREFIX = b"\x00TIANLAI-STEM-END-v1:"
_MARKER_SUFFIX = b"\r\n"
_MAX_METADATA_BYTES = 16_384
_COPY_CHUNK_BYTES = 1024 * 1024
_RENDER_CHUNK_FRAMES = 65_536
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_REQUEST_BYTES = 64 * 1024 * 1024
_MAX_PROTOCOL_PREAMBLE_BYTES = 64 * 1024
_MAX_DIAGNOSTIC_BYTES = 8192
_WARM_IDLE_SECONDS = 20.0
_WARM_PARENT_REUSE_SECONDS = 15.0
_WARM_MAX_JOBS = 64
_RESOURCE_EXHAUSTED_EXIT_CODE = 73
_RESOURCE_EXHAUSTED_DIAGNOSTIC = b"MemoryError: worker resource exhausted"
_ALLOWED_OVERRIDE_FIELDS = frozenset(
    {"release_seconds", "release_tail_gain", "sample_variant"}
)
_WARM_REUSABLE_BUILTIN_TYPES = frozenset(
    {
        "modeled_instrument",
        "modeled_bianzhong",
        "oscillator",
        "procedural_sfx",
        "synthesizer",
    }
)
_GLOBAL_CAPACITY = automatic_worker_capacity()
_GLOBAL_PERMITS = threading.BoundedSemaphore(_GLOBAL_CAPACITY)
_PROCESS_GLOBALS_PID = os.getpid()


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _duplicate_safe_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _decode_json_object(
    payload: bytes | bytearray,
    label: str,
) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=_reject_json_constant,
            object_pairs_hook=_duplicate_safe_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be strict UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return value


def _load_json_object(payload: bytes, label: str) -> dict[str, Any]:
    if type(payload) is not bytes:
        raise TypeError(f"{label} must be bytes")
    return _decode_json_object(payload, label)


def _load_json_buffer(
    payload: bytes | bytearray,
    label: str,
) -> dict[str, Any]:
    if type(payload) not in (bytes, bytearray):
        raise TypeError(f"{label} must be a byte buffer")
    return _decode_json_object(payload, label)


def _validated_overrides(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("stem worker overrides must be an object")
    if any(type(key) is not str for key in value):
        raise ValueError("stem worker override keys must be strings")
    unknown = set(value) - _ALLOWED_OVERRIDE_FIELDS
    if unknown:
        raise ValueError("stem worker overrides contain structural fields")
    if "release_seconds" in value:
        release = value["release_seconds"]
        if (
            isinstance(release, bool)
            or not isinstance(release, (int, float))
            or not math.isfinite(float(release))
            or float(release) < 0.0
        ):
            raise ValueError("stem worker release_seconds override is invalid")
    if "release_tail_gain" in value:
        tail_gain = value["release_tail_gain"]
        if (
            isinstance(tail_gain, bool)
            or not isinstance(tail_gain, (int, float))
            or not math.isfinite(float(tail_gain))
            or not 0.0 <= float(tail_gain) <= 1.0
        ):
            raise ValueError("stem worker release_tail_gain override is invalid")
    if "sample_variant" in value:
        variant = value["sample_variant"]
        if (
            not isinstance(variant, str)
            or not variant
            or len(variant) > 256
            or "\x00" in variant
        ):
            raise ValueError("stem worker sample_variant override is invalid")
    return dict(value)


@dataclass(frozen=True, slots=True)
class StemRenderJob:
    """One pickle-safe raw-stem request containing only primitive values."""

    index: int
    executor_id: str
    manifest_path: str
    expected_manifest_sha256: str
    sample_rate: int
    frame_count: int
    performance_json: bytes
    overrides_json: bytes

    @classmethod
    def create(
        cls,
        *,
        index: int,
        executor_id: str,
        manifest_path: str | Path,
        sample_rate: int,
        performance: dict[str, Any],
        overrides: dict[str, Any] | None = None,
        expected_manifest_sha256: str | None = None,
    ) -> "StemRenderJob":
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError("stem worker index must be a non-negative integer")
        if not isinstance(executor_id, str) or not executor_id or "\x00" in executor_id:
            raise ValueError("stem worker executor_id must be a non-empty string")
        if len(executor_id) > 1024:
            raise ValueError("stem worker executor_id is too long")
        if (
            isinstance(sample_rate, bool)
            or not isinstance(sample_rate, int)
            or sample_rate <= 0
        ):
            raise ValueError("stem worker sample_rate must be positive")
        manifest_identity, manifest_payload = read_plain_file_bytes(
            manifest_path,
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
        if expected_manifest_sha256 is not None and (
            not isinstance(expected_manifest_sha256, str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                expected_manifest_sha256,
            )
            is None
            or expected_manifest_sha256 != manifest_sha256
        ):
            raise ValueError(
                "stem worker manifest changed after parallel authorisation"
            )
        performance_json = canonical_json_bytes(performance)
        overrides_json = canonical_json_bytes(
            _validated_overrides(overrides or {})
        )
        document = parse_performance_document(
            _load_json_object(performance_json, "performance_json")
        )
        if document.sample_rate != sample_rate:
            raise ValueError("stem worker performance sample rate does not match")
        return cls(
            index=index,
            executor_id=executor_id,
            manifest_path=str(manifest_identity.path),
            expected_manifest_sha256=manifest_sha256,
            sample_rate=sample_rate,
            frame_count=document.total_samples,
            performance_json=performance_json,
            overrides_json=overrides_json,
        )

    def protocol_document(self, *, token: str) -> dict[str, Any]:
        _validate_job(self)
        _validate_token(token)
        return {
            "format": _PROTOCOL_FORMAT,
            "version": _PROTOCOL_VERSION,
            "token": token,
            "producer_source_tree_sha256": PROCESS_SOURCE_TREE_SHA256,
            "job": {
                "index": self.index,
                "executor_id": self.executor_id,
                "manifest_path": self.manifest_path,
                "expected_manifest_sha256": (
                    self.expected_manifest_sha256
                ),
                "sample_rate": self.sample_rate,
                "frame_count": self.frame_count,
                "performance": _load_json_object(
                    self.performance_json, "performance_json"
                ),
                "overrides": _load_json_object(
                    self.overrides_json, "overrides_json"
                ),
            },
        }


@dataclass(slots=True)
class StemWorkerHandle:
    """Parent-owned process and anonymous files for one active job."""

    job: StemRenderJob
    token: str
    process: subprocess.Popen[bytes]
    stdout_file: BinaryIO
    stderr_file: BinaryIO
    _permit_held: bool = True
    _finished: bool = False
    _owner_pid: int = field(default_factory=os.getpid)
    _warm_worker: _WarmWorker | None = None
    _warm_task: _WarmTask | None = None
    _worker_slot: ReservedWorkerSlot | None = None
    _started_ns: int = field(default_factory=time.monotonic_ns)
    _warm_used: bool = False


@dataclass(frozen=True, slots=True)
class _ManagedWarmBinding:
    """One render session's immutable persistent-worker resource ceiling."""

    owner_id: str
    scratch_directory: Path
    scratch_volume_id: str
    worker_memory_ceiling_bytes: int
    coordinator_memory_bytes: int
    memory_budget_bytes: int
    scratch_ceiling_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str) or _TOKEN_RE.fullmatch(
            self.owner_id
        ) is None:
            raise ValueError("managed warm owner must be 32 lowercase hex characters")
        if not isinstance(self.scratch_directory, Path):
            raise TypeError("managed warm scratch directory must be a Path")
        if (
            not isinstance(self.scratch_volume_id, str)
            or not self.scratch_volume_id
            or len(self.scratch_volume_id) > 1024
        ):
            raise ValueError("managed warm scratch volume identity is invalid")
        for value, label in (
            (self.worker_memory_ceiling_bytes, "worker memory ceiling"),
            (self.coordinator_memory_bytes, "coordinator memory claim"),
            (self.memory_budget_bytes, "memory budget"),
            (self.scratch_ceiling_bytes, "scratch ceiling"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"managed warm {label} must be positive")
        if (
            self.worker_memory_ceiling_bytes + self.coordinator_memory_bytes
            > self.memory_budget_bytes
        ):
            raise ValueError("managed warm memory ceiling exceeds its budget")


@dataclass(slots=True)
class StemWorkerResult:
    """Validated bounded metadata plus an anonymous raw-audio file."""

    index: int
    executor_id: str
    sample_rate: int
    frame_count: int
    peak_voices: int
    manifest_sha256: str
    audio_sha256: str
    audio_file: BinaryIO
    audio_offset: int
    byte_count: int
    _worker_slot: ReservedWorkerSlot | None = None
    _warm_worker: _WarmWorker | None = None
    _warm_task: _WarmTask | None = None
    _owner_pid: int = field(default_factory=os.getpid)
    _closed: bool = False
    _elapsed_ns: int = 0
    _warm_used: bool = False

    def load_audio(self) -> Any:
        """Load this stem as ``(frames, 2)`` float32 after validation."""

        if self._closed:
            raise ValueError("stem worker result is closed")
        import numpy as np

        self.audio_file.seek(self.audio_offset)
        audio = np.fromfile(
            self.audio_file,
            dtype="<f4",
            count=self.frame_count * 2,
        )
        if audio.size != self.frame_count * 2:
            raise RuntimeError("validated stem audio became truncated")
        return audio.reshape(self.frame_count, 2)

    def detach_source(
        self,
        *,
        completion_callback: Callable[[bool], None] | None = None,
    ) -> Any:
        """Transfer this result and its leases to a verified block source."""

        if self._closed:
            raise ValueError("stem worker result is closed")
        if completion_callback is not None and not callable(
            completion_callback
        ):
            raise TypeError("completion_callback must be callable")
        from .stem_source import OwnedStemSource

        worker = self._warm_worker
        task = self._warm_task
        slot = self._worker_slot

        def complete(success: bool) -> None:
            first_error: BaseException | None = None
            adopted = success
            if worker is not None and task is not None:
                try:
                    if success:
                        _return_warm_worker(worker, task)
                    else:
                        _discard_warm_worker(worker, force=True)
                        worker.release_task(task, success=False)
                except BaseException as exc:
                    first_error = exc
                    adopted = False
            if slot is not None:
                try:
                    slot.close()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
                    adopted = False
            if completion_callback is not None:
                try:
                    completion_callback(adopted)
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error

        # OwnedStemSource documents that constructor failure retains caller
        # ownership.  Mutate this result only after every descriptor-bound
        # precondition has succeeded.
        source = OwnedStemSource(
            self.audio_file,
            audio_offset=self.audio_offset,
            frame_count=self.frame_count,
            expected_sha256=self.audio_sha256,
            completion_callback=complete,
        )
        self._closed = True
        self._warm_worker = None
        self._warm_task = None
        self._worker_slot = None
        return source

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        audio_closed = False
        first_error: BaseException | None = None
        try:
            self.audio_file.close()
            audio_closed = True
        except BaseException as exc:
            first_error = exc
        worker = self._warm_worker
        task = self._warm_task
        self._warm_worker = None
        self._warm_task = None
        if worker is not None and task is not None:
            try:
                if self._owner_pid == os.getpid():
                    if audio_closed:
                        _return_warm_worker(worker, task)
                    else:
                        _discard_warm_worker(worker, force=True)
                        worker.release_task(task, success=False)
                # A fork child owns only its duplicate scratch fd.  The
                # parent still owns the reusable process and task.
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        slot = self._worker_slot
        self._worker_slot = None
        if slot is not None:
            try:
                slot.close()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "StemWorkerResult":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class StemWorkerError(RuntimeError):
    """A managed stem child failed or returned an invalid private protocol."""


@dataclass(slots=True)
class _WarmTask:
    """One active request owned by exactly one warm worker."""

    job: StemRenderJob
    token: str
    stdout_file: BinaryIO
    stderr_file: BinaryIO
    done: threading.Event = field(default_factory=threading.Event)
    response_complete: bool = False
    error: BaseException | None = None


def _read_pipe_exact(source: BinaryIO, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining:
        chunk = source.read(remaining)
        if not chunk:
            raise EOFError("warm stem worker protocol is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _copy_pipe_exact(
    source: BinaryIO,
    target: BinaryIO,
    byte_count: int,
) -> None:
    remaining = byte_count
    while remaining:
        chunk = source.read(min(_COPY_CHUNK_BYTES, remaining))
        if not chunk:
            raise EOFError("warm stem worker audio is truncated")
        target.write(chunk)
        remaining -= len(chunk)


def _copy_warm_response(source: BinaryIO, task: _WarmTask) -> None:
    """Continuously drain one response into this task's private scratch file."""

    begin = _BEGIN_PREFIX + task.token.encode("ascii") + _MARKER_SUFFIX
    window = bytearray()
    preamble_bytes = 0
    while True:
        value = source.read(1)
        if not value:
            raise EOFError("warm stem worker begin marker is missing")
        preamble_bytes += 1
        window.extend(value)
        if len(window) > len(begin):
            del window[0]
        if bytes(window) == begin:
            break
        if preamble_bytes > _MAX_PROTOCOL_PREAMBLE_BYTES + len(begin):
            raise StemWorkerError("warm stem worker protocol preamble is too large")

    target = task.stdout_file
    target.seek(0)
    target.truncate()
    target.write(begin)
    _copy_pipe_exact(
        source,
        target,
        task.job.frame_count * 2 * 4,
    )
    metadata_length_raw = _read_pipe_exact(source, 4)
    target.write(metadata_length_raw)
    metadata_length = struct.unpack(">I", metadata_length_raw)[0]
    if metadata_length == 0 or metadata_length > _MAX_METADATA_BYTES:
        raise StemWorkerError("warm stem worker metadata length is invalid")
    _copy_pipe_exact(source, target, metadata_length)
    end = _END_PREFIX + task.token.encode("ascii") + _MARKER_SUFFIX
    actual_end = _read_pipe_exact(source, len(end))
    target.write(actual_end)
    target.flush()
    if actual_end != end:
        raise StemWorkerError("warm stem worker end marker is invalid")


def _write_stream_all(
    target: BinaryIO,
    payload: bytes | bytearray | memoryview,
) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = target.write(remaining)
        if written is None or written <= 0:
            raise OSError("warm stem worker request write made no progress")
        remaining = remaining[written:]


def _write_framed_request(target: BinaryIO, protocol: bytes) -> None:
    """Write a request without allocating a second protocol-sized buffer."""

    _write_stream_all(target, struct.pack(">I", len(protocol)))
    _write_stream_all(target, protocol)
    target.flush()


class _WarmWorker:
    """One reusable trusted child with continuous stdout/stderr drainers."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        managed_binding: _ManagedWarmBinding | None = None,
        reserved_slot: ReservedWorkerSlot | None = None,
    ) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ValueError("warm worker requires all standard pipes")
        if (managed_binding is None) != (reserved_slot is None):
            raise ValueError(
                "managed warm workers require both a binding and reservation"
            )
        self.process = process
        self.managed_binding = managed_binding
        self._reserved_slot = reserved_slot
        self._condition = threading.Condition()
        self._active: _WarmTask | None = None
        self._response_pending = False
        self._retired = False
        self._diagnostic_tail = bytearray()
        self._completed_jobs = 0
        self._idle_since: float | None = None
        self._stdout_thread = threading.Thread(
            target=self._stdout_loop,
            name=f"tianlai-stem-stdout-{process.pid}",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name=f"tianlai-stem-stderr-{process.pid}",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @property
    def reusable(self) -> bool:
        with self._condition:
            return (
                not self._retired
                and self._active is None
                and self._completed_jobs < _WARM_MAX_JOBS
                and (
                    self._idle_since is None
                    or time.monotonic() - self._idle_since
                    < _WARM_PARENT_REUSE_SECONDS
                )
                and self.process.poll() is None
            )

    def assign(self, task: _WarmTask, protocol: bytes) -> bool:
        if not protocol or len(protocol) > _MAX_REQUEST_BYTES:
            raise ValueError("warm stem worker request size is invalid")
        with self._condition:
            if (
                self._retired
                or self._active is not None
                or self._completed_jobs >= _WARM_MAX_JOBS
                or (
                    self._idle_since is not None
                    and time.monotonic() - self._idle_since
                    >= _WARM_PARENT_REUSE_SECONDS
                )
                or self.process.poll() is not None
            ):
                return False
            self._active = task
            self._response_pending = True
            self._idle_since = None
            self._diagnostic_tail.clear()
            self._condition.notify_all()

        try:
            assert self.process.stdin is not None
            _write_framed_request(self.process.stdin, protocol)
        except BaseException as exc:
            self._fail_active(task, exc)
            return False
        return True

    def release_reserved_slot(self) -> None:
        """Close the parent ledger lease at most once across drainer races."""

        with self._condition:
            slot = self._reserved_slot
            self._reserved_slot = None
        if slot is None:
            return
        try:
            slot.close()
        except BaseException:
            with self._condition:
                if self._reserved_slot is None:
                    self._reserved_slot = slot
            raise

    def _copy_diagnostic_to_task(self, task: _WarmTask) -> None:
        try:
            with self._condition:
                diagnostic = bytes(self._diagnostic_tail)
            task.stderr_file.seek(0)
            task.stderr_file.truncate()
            task.stderr_file.write(diagnostic)
            task.stderr_file.flush()
        except BaseException:
            pass

    def _append_diagnostic(self, chunk: bytes) -> None:
        with self._condition:
            self._diagnostic_tail.extend(chunk)
            overflow = len(self._diagnostic_tail) - _MAX_DIAGNOSTIC_BYTES
            if overflow > 0:
                del self._diagnostic_tail[:overflow]

    def _fail_active(self, task: _WarmTask, error: BaseException) -> None:
        with self._condition:
            if self._active is task:
                task.error = error
                self._response_pending = False
                self._retired = True
                task.done.set()
                self._condition.notify_all()

    def _stdout_loop(self) -> None:
        assert self.process.stdout is not None
        while True:
            with self._condition:
                while not self._retired and not self._response_pending:
                    self._condition.wait()
                if self._retired:
                    return
                task = self._active
            assert task is not None
            try:
                _copy_warm_response(self.process.stdout, task)
            except BaseException as exc:
                self._fail_active(task, exc)
                try:
                    self._copy_diagnostic_to_task(task)
                except BaseException:
                    pass
                return
            try:
                self._copy_diagnostic_to_task(task)
            except BaseException:
                pass
            with self._condition:
                if (
                    self._active is task
                    and not self._retired
                    and task.error is None
                ):
                    task.response_complete = True
                    self._response_pending = False
                    task.done.set()
                    self._condition.notify_all()
            del task

    def _stderr_loop(self) -> None:
        assert self.process.stderr is not None
        try:
            while True:
                chunk = self.process.stderr.read(4096)
                if not chunk:
                    break
                self._append_diagnostic(chunk)
        except BaseException as exc:
            with self._condition:
                task = self._active
                self._retired = True
                if task is not None and not task.done.is_set():
                    task.error = exc
                    self._response_pending = False
                    task.done.set()
                self._condition.notify_all()
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            except (OSError, ValueError):
                pass
            _terminate_process(self.process)
            if task is None:
                _forget_exited_warm_worker(self)
            return
        try:
            self.process.wait()
        except (OSError, ValueError):
            pass
        with self._condition:
            exited_while_idle = self._active is None
            if exited_while_idle:
                self._retired = True
                self._condition.notify_all()
        if exited_while_idle:
            _forget_exited_warm_worker(self)

    def release_task(self, task: _WarmTask, *, success: bool) -> bool:
        """Forget task scratch before the child can become idle."""

        with self._condition:
            if self._active is not task:
                return False
            self._active = None
            self._response_pending = False
            if success:
                self._completed_jobs += 1
                self._idle_since = time.monotonic()
            self._condition.notify_all()
            return (
                success
                and not self._retired
                and self._completed_jobs < _WARM_MAX_JOBS
                and self.process.poll() is None
            )

    def retire(self, *, force: bool) -> None:
        with self._condition:
            self._retired = True
            task = self._active
            self._condition.notify_all()

        def close_input() -> None:
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            except (OSError, ValueError):
                pass

        if force:
            close_input()
            _terminate_process(self.process)
        elif self.process.poll() is None:
            if task is None:
                # An idle persistent child is blocked waiting for the next
                # framed request.  EOF is its normal prompt shutdown signal;
                # waiting before closing stdin would add 250 ms per worker at
                # every render phase boundary.
                close_input()
            try:
                self.process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                close_input()
                try:
                    self.process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    _terminate_process(self.process)
        else:
            self.process.wait()
        close_input()
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not threading.current_thread():
                # The process has been reaped, so both pipes are at EOF.  Do
                # not close task scratch until its drain thread has actually
                # released the file object (especially on Windows).
                thread.join()
        if task is not None:
            try:
                self._copy_diagnostic_to_task(task)
            except BaseException:
                pass
            if not task.done.is_set():
                task.error = StemWorkerError("warm stem worker was retired")
                task.done.set()
        self.release_reserved_slot()


_WARM_POOL_LOCK = threading.Lock()
_PROCESS_ADMISSION_LOCK = threading.Lock()
_WARM_IDLE: list[_WarmWorker] = []
_WARM_ALL: set[_WarmWorker] = set()
_WARM_QUARANTINED: set[_WarmWorker] = set()
_IDLE_RETIRE_BARRIER = 0


def _forget_exited_warm_worker(worker: _WarmWorker) -> None:
    """Drop a self-expired idle child without retaining dead pool entries."""

    worker.release_reserved_slot()
    with _WARM_POOL_LOCK:
        _WARM_IDLE[:] = [
            candidate
            for candidate in _WARM_IDLE
            if candidate is not worker
        ]
        _WARM_ALL.discard(worker)
        _WARM_QUARANTINED.discard(worker)


def _close_inherited_worker_streams(worker: _WarmWorker) -> None:
    for stream in (
        worker.process.stdin,
        worker.process.stdout,
        worker.process.stderr,
    ):
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass


def _reset_process_globals_after_fork() -> None:
    global _GLOBAL_PERMITS
    global _IDLE_RETIRE_BARRIER
    global _PROCESS_ADMISSION_LOCK
    global _PROCESS_GLOBALS_PID
    global _WARM_ALL
    global _WARM_IDLE
    global _WARM_POOL_LOCK
    global _WARM_QUARANTINED

    inherited = tuple(_WARM_ALL)
    for worker in inherited:
        _close_inherited_worker_streams(worker)
    _GLOBAL_PERMITS = threading.BoundedSemaphore(_GLOBAL_CAPACITY)
    _PROCESS_ADMISSION_LOCK = threading.Lock()
    _WARM_POOL_LOCK = threading.Lock()
    _WARM_IDLE = []
    _WARM_ALL = set()
    _WARM_QUARANTINED = set()
    _IDLE_RETIRE_BARRIER = 0
    _PROCESS_GLOBALS_PID = os.getpid()


def _ensure_process_globals() -> None:
    if _PROCESS_GLOBALS_PID != os.getpid():
        _reset_process_globals_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_globals_after_fork)


def _pool_eligible_job(job: StemRenderJob) -> bool:
    try:
        identity, payload = read_plain_file_bytes(
            job.manifest_path,
            maximum_bytes=_MAX_MANIFEST_BYTES,
        )
        if hashlib.sha256(payload).hexdigest() != job.expected_manifest_sha256:
            return False
        manifest = _load_json_object(payload, "instrument manifest")
        instrument_type = manifest.get("type")
        if (
            type(instrument_type) is not str
            or instrument_type not in _WARM_REUSABLE_BUILTIN_TYPES
            or "implementation" in manifest
        ):
            return False
        raw_asset_root = manifest.get("asset_root")
        if "asset_root" in manifest and (
            type(raw_asset_root) is not str or raw_asset_root.strip()
        ):
            return False
        explicitly_asset_free = (
            manifest.get("runtime_asset_policy")
            == "no_external_audio_assets"
            or (
                manifest.get("provenance_kind")
                == "project_authored_dsp"
                and manifest.get("external_audio_assets") == []
            )
        )
        if not explicitly_asset_free:
            return False
        return _is_trusted_managed_worker_manifest(identity.path, manifest)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _warm_worker_reuse_available(
    managed_binding: _ManagedWarmBinding | None = None,
) -> bool:
    """Cheap hint used to skip speculative manifest work for a final batch."""

    with _WARM_POOL_LOCK:
        return any(
            worker.managed_binding == managed_binding
            for worker in _WARM_IDLE
        )


def _start_warm_worker(
    *,
    managed_binding: _ManagedWarmBinding | None = None,
    reserved_slot: ReservedWorkerSlot | None = None,
) -> _WarmWorker:
    if (managed_binding is None) != (reserved_slot is None):
        raise ValueError(
            "managed warm worker startup requires an exact reservation"
        )
    command = [sys.executable, "-m", "tianlai.stem_worker", "--persistent"]
    if reserved_slot is not None:
        spec = reserved_slot.child_spec
        command = [
            sys.executable,
            os.fspath(
                Path(__file__).resolve().with_name(
                    "_stem_worker_bootstrap.py"
                )
            ),
            os.fspath(spec.directory),
            str(spec.slot_index),
            spec.token,
            str(spec.parent_pid),
            "--persistent",
        ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent),
        bufsize=0,
    )
    try:
        if reserved_slot is not None:
            # The standard-library bootstrap holds active.lock before the
            # persistent runtime imports NumPy or the instrument engine.
            reserved_slot.wait_for_active(process)
        return _WarmWorker(
            process,
            managed_binding=managed_binding,
            reserved_slot=reserved_slot,
        )
    except BaseException:
        _terminate_process(process)
        if reserved_slot is not None:
            reserved_slot.close()
        raise


def _checkout_warm_worker(
    *,
    allow_start: bool = True,
    managed_binding: _ManagedWarmBinding | None = None,
    reserved_slot: ReservedWorkerSlot | None = None,
) -> _WarmWorker | None:
    if reserved_slot is not None and managed_binding is None:
        raise ValueError("reserved warm worker requires a managed binding")
    with _PROCESS_ADMISSION_LOCK:
        _raise_if_warm_worker_quarantined()
        stale: list[_WarmWorker] = []
        selected: _WarmWorker | None = None
        with _WARM_POOL_LOCK:
            reusable: list[_WarmWorker] = []
            for worker in _WARM_IDLE:
                if (
                    worker.managed_binding == managed_binding
                    and worker.reusable
                    and reserved_slot is None
                ):
                    reusable.append(worker)
                elif (
                    worker.managed_binding == managed_binding
                    and not worker.reusable
                ):
                    stale.append(worker)
                else:
                    reusable.append(worker)
            if reusable and reserved_slot is None:
                for index in range(len(reusable) - 1, -1, -1):
                    candidate = reusable[index]
                    if candidate.managed_binding == managed_binding:
                        selected = reusable.pop(index)
                        break
            _WARM_IDLE[:] = reusable
        try:
            for worker in stale:
                _discard_warm_worker(worker, force=False)
        except BaseException:
            if selected is not None:
                requeued = False
                with _WARM_POOL_LOCK:
                    if selected in _WARM_ALL and selected.reusable:
                        _WARM_IDLE.append(selected)
                        requeued = True
                if not requeued:
                    try:
                        _discard_warm_worker(selected, force=True)
                    except BaseException:
                        pass
            raise
        if selected is not None:
            return selected
        if not allow_start:
            return None
        if managed_binding is not None and reserved_slot is None:
            # A session may reuse only the globally admitted children whose
            # immutable ledgers were reserved at the start of its known run.
            return None
        worker = _start_warm_worker(
            managed_binding=managed_binding,
            reserved_slot=reserved_slot,
        )
        with _WARM_POOL_LOCK:
            _WARM_ALL.add(worker)
        return worker


def _discard_warm_worker(worker: _WarmWorker, *, force: bool) -> None:
    with _WARM_POOL_LOCK:
        _WARM_IDLE[:] = [
            candidate
            for candidate in _WARM_IDLE
            if candidate is not worker
        ]
    errors: list[BaseException] = []
    try:
        worker.retire(force=force)
    except BaseException as exc:
        errors.append(exc)
    if worker.process.poll() is None:
        # A graceful retirement can fail before closing stdin, and even a
        # force attempt can be interrupted by an unexpected runtime error.
        # Make one final best-effort force pass before deciding whether this
        # process must quarantine all later child admission.
        try:
            worker.retire(force=True)
        except BaseException as exc:
            errors.append(exc)
    process_exited = worker.process.poll() is not None
    if process_exited:
        try:
            worker.release_reserved_slot()
        except BaseException as exc:
            errors.append(exc)
    with _WARM_POOL_LOCK:
        if process_exited:
            _WARM_ALL.discard(worker)
            _WARM_QUARANTINED.discard(worker)
        else:
            _WARM_ALL.add(worker)
            _WARM_QUARANTINED.add(worker)
    if not process_exited:
        error = StemWorkerError(
            "warm stem worker could not be retired; child admission is "
            "quarantined"
        )
        if errors:
            raise error from errors[-1]
        raise error


def _raise_if_warm_worker_quarantined() -> None:
    with _WARM_POOL_LOCK:
        dead = tuple(
            worker
            for worker in _WARM_QUARANTINED
            if worker.process.poll() is not None
        )
        for worker in dead:
            _WARM_QUARANTINED.discard(worker)
            _WARM_ALL.discard(worker)
        quarantined = bool(_WARM_QUARANTINED)
    if quarantined:
        raise StemWorkerError(
            "warm stem worker retirement is incomplete; refusing to start "
            "another child"
        )


def _begin_idle_stem_worker_retirement() -> tuple[_WarmWorker, ...]:
    global _IDLE_RETIRE_BARRIER

    with _WARM_POOL_LOCK:
        _IDLE_RETIRE_BARRIER += 1
        workers = tuple(_WARM_IDLE)
        _WARM_IDLE.clear()
    return workers


def _finish_idle_stem_worker_retirement() -> None:
    global _IDLE_RETIRE_BARRIER

    with _WARM_POOL_LOCK:
        _IDLE_RETIRE_BARRIER -= 1


def _retire_detached_stem_workers(
    workers: tuple[_WarmWorker, ...],
) -> None:
    first_error: BaseException | None = None
    for worker in workers:
        try:
            _discard_warm_worker(worker, force=False)
        except BaseException as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def _retire_idle_stem_workers_under_admission() -> None:
    """Retire the current idle set while new child admission is blocked."""

    workers = _begin_idle_stem_worker_retirement()
    try:
        _retire_detached_stem_workers(workers)
        _raise_if_warm_worker_quarantined()
    finally:
        _finish_idle_stem_worker_retirement()


def retire_idle_stem_workers() -> None:
    """Retire reusable idle children without touching active render jobs."""

    _ensure_process_globals()
    with _PROCESS_ADMISSION_LOCK:
        _retire_idle_stem_workers_under_admission()


def _retire_managed_stem_worker_session(
    owner_id: str,
    *,
    force: bool = True,
) -> None:
    """Force-retire every child owned by one private render session."""

    if not isinstance(owner_id, str) or _TOKEN_RE.fullmatch(owner_id) is None:
        raise ValueError("managed warm owner must be 32 lowercase hex characters")
    if type(force) is not bool:
        raise TypeError("managed warm retirement mode must be a bool")
    _ensure_process_globals()
    with _PROCESS_ADMISSION_LOCK:
        with _WARM_POOL_LOCK:
            workers = tuple(
                worker
                for worker in _WARM_ALL
                if worker.managed_binding is not None
                and worker.managed_binding.owner_id == owner_id
            )
        first_error: BaseException | None = None
        for worker in workers:
            try:
                # On cancellation a completed response may intentionally
                # remain attached to its result scratch.  Terminating the
                # now-idle child is safe; the anonymous result file remains
                # parent-owned until its consumer closes it.
                _discard_warm_worker(worker, force=force)
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error


def _return_warm_worker(worker: _WarmWorker, task: _WarmTask) -> None:
    reusable = worker.release_task(task, success=True)
    if not reusable:
        _discard_warm_worker(worker, force=False)
        return
    with _WARM_POOL_LOCK:
        if (
            _IDLE_RETIRE_BARRIER == 0
            and worker.reusable
            and worker in _WARM_ALL
        ):
            _WARM_IDLE.append(worker)
            return
    _discard_warm_worker(worker, force=False)


def _shutdown_warm_pool() -> None:
    if _PROCESS_GLOBALS_PID != os.getpid():
        return
    with _PROCESS_ADMISSION_LOCK:
        workers = _begin_idle_stem_worker_retirement()
        with _WARM_POOL_LOCK:
            workers = tuple(dict.fromkeys((*workers, *_WARM_ALL)))
        try:
            for worker in workers:
                try:
                    _discard_warm_worker(worker, force=True)
                except BaseException:
                    # A still-live process remains registered and quarantined,
                    # so a later shutdown call can retry it without admitting
                    # another child in the meantime.
                    pass
        finally:
            _finish_idle_stem_worker_retirement()


atexit.register(_shutdown_warm_pool)


def managed_subprocess_workers_available() -> bool:
    """Whether this interpreter can safely launch the managed module child."""

    if getattr(sys, "frozen", False):
        return False
    executable = Path(sys.executable)
    return executable.is_absolute() and executable.is_file()


def _validate_token(token: str) -> None:
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise ValueError("stem worker token must be 32 lowercase hex characters")


def _validate_job(job: StemRenderJob) -> None:
    if type(job) is not StemRenderJob:
        raise TypeError("stem worker input must be an exact StemRenderJob")
    if isinstance(job.index, bool) or not isinstance(job.index, int) or job.index < 0:
        raise ValueError("stem worker index must be a non-negative integer")
    if not isinstance(job.executor_id, str) or not job.executor_id:
        raise ValueError("stem worker executor_id must be non-empty")
    if len(job.executor_id) > 1024 or "\x00" in job.executor_id:
        raise ValueError("stem worker executor_id is invalid")
    if (
        isinstance(job.sample_rate, bool)
        or not isinstance(job.sample_rate, int)
        or job.sample_rate <= 0
    ):
        raise ValueError("stem worker sample_rate must be positive")
    if (
        isinstance(job.frame_count, bool)
        or not isinstance(job.frame_count, int)
        or job.frame_count < 0
    ):
        raise ValueError("stem worker frame_count must be non-negative")
    manifest = Path(job.manifest_path)
    if not manifest.is_absolute():
        raise ValueError("stem worker manifest must be an absolute regular file")
    if (
        not isinstance(job.expected_manifest_sha256, str)
        or re.fullmatch(
            r"[0-9a-f]{64}",
            job.expected_manifest_sha256,
        )
        is None
    ):
        raise ValueError("stem worker expected manifest SHA-256 is invalid")
    manifest_identity, manifest_payload = read_plain_file_bytes(
        manifest,
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    if (
        manifest_identity.path != manifest
        or hashlib.sha256(manifest_payload).hexdigest()
        != job.expected_manifest_sha256
    ):
        raise ValueError("stem worker manifest identity changed")
    performance = _load_json_object(job.performance_json, "performance_json")
    overrides = _validated_overrides(
        _load_json_object(job.overrides_json, "overrides_json")
    )
    document = parse_performance_document(performance)
    if document.sample_rate != job.sample_rate:
        raise ValueError("stem worker performance sample rate does not match")
    if document.total_samples != job.frame_count:
        raise ValueError("stem worker frame count does not match performance")
    # Force overrides through strict canonical JSON validation too.
    canonical_json_bytes(overrides)


def _validate_managed_warm_request(
    job: StemRenderJob,
    *,
    scratch_directory: str | Path,
    binding: _ManagedWarmBinding,
    worker_memory_bytes: int,
    reserved_slot: ReservedWorkerSlot | None,
) -> None:
    if type(binding) is not _ManagedWarmBinding:
        raise TypeError("managed warm binding must be exact")
    if (
        isinstance(worker_memory_bytes, bool)
        or not isinstance(worker_memory_bytes, int)
        or worker_memory_bytes <= 0
    ):
        raise ValueError("managed warm worker memory claim must be positive")
    scratch = Path(scratch_directory).resolve(strict=True)
    if scratch != binding.scratch_directory:
        raise ValueError("managed warm scratch directory changed")
    if scratch_volume_identity(scratch) != binding.scratch_volume_id:
        raise ValueError("managed warm scratch volume changed")
    scratch_bytes = job.frame_count * 2 * 4
    if (
        worker_memory_bytes > binding.worker_memory_ceiling_bytes
        or scratch_bytes > binding.scratch_ceiling_bytes
    ):
        raise ValueError("managed warm request exceeds its admitted ceiling")
    if not current_source_tree_matches(PROCESS_SOURCE_TREE_SHA256):
        raise StemWorkerError("managed warm source identity changed")
    if not _pool_eligible_job(job):
        raise StemWorkerError("managed warm manifest identity changed")
    if reserved_slot is None:
        return
    claim = reserved_slot.claim
    if (
        claim.owner_id != binding.owner_id
        or claim.worker_memory_bytes
        != binding.worker_memory_ceiling_bytes
        or claim.coordinator_memory_bytes
        != binding.coordinator_memory_bytes
        or claim.memory_budget_bytes != binding.memory_budget_bytes
        or claim.scratch_bytes != binding.scratch_ceiling_bytes
        or claim.scratch_volume_id != binding.scratch_volume_id
    ):
        raise ValueError("managed warm reservation does not match its binding")


def _try_start_stem_worker(
    job: StemRenderJob,
    *,
    scratch_directory: str | Path,
    allow_warm_start: bool,
    allow_warm_reuse: bool,
    reserved_slot: ReservedWorkerSlot | None = None,
    managed_warm_binding: _ManagedWarmBinding | None = None,
    managed_worker_memory_bytes: int | None = None,
) -> StemWorkerHandle | None:
    """Internal start path with a coordinator-only warm admission hint.

    These flags are not user settings.  A globally coordinated ensemble may
    attach an immutable session binding and reservation ceiling; unbound
    internal callers retain the legacy private pool, while the public
    compatibility wrapper remains strictly one-shot.
    """

    started_ns = time.monotonic_ns()
    if reserved_slot is not None and type(reserved_slot) is not ReservedWorkerSlot:
        raise TypeError("reserved worker slot must be exact")
    try:
        _ensure_process_globals()
        if type(allow_warm_start) is not bool:
            raise TypeError("warm worker start hint must be a bool")
        if type(allow_warm_reuse) is not bool:
            raise TypeError("warm worker reuse hint must be a bool")
        if allow_warm_start and not allow_warm_reuse:
            raise ValueError("starting a warm worker requires warm reuse")
        if managed_warm_binding is None:
            if managed_worker_memory_bytes is not None:
                raise ValueError("managed worker memory requires a warm binding")
            if reserved_slot is not None and (
                allow_warm_start or allow_warm_reuse
            ):
                raise ValueError("reserved worker slots require one-shot workers")
        else:
            if not allow_warm_reuse:
                raise ValueError("managed warm binding requires warm reuse")
            if managed_worker_memory_bytes is None:
                raise ValueError("managed warm worker memory claim is missing")
        _validate_job(job)
        if managed_warm_binding is not None:
            _validate_managed_warm_request(
                job,
                scratch_directory=scratch_directory,
                binding=managed_warm_binding,
                worker_memory_bytes=managed_worker_memory_bytes,
                reserved_slot=reserved_slot,
            )
    except BaseException:
        if reserved_slot is not None:
            reserved_slot.close()
        raise
    if not managed_subprocess_workers_available():
        if reserved_slot is not None:
            reserved_slot.close()
        return None
    if not _GLOBAL_PERMITS.acquire(blocking=False):
        if reserved_slot is not None:
            reserved_slot.close()
        return None
    token = uuid.uuid4().hex
    stdout_file: BinaryIO | None = None
    stderr_file: BinaryIO | None = None
    try:
        scratch = Path(scratch_directory).resolve(strict=True)
        if not scratch.is_dir():
            raise ValueError("stem worker scratch directory must be a directory")
        protocol = canonical_json_bytes(job.protocol_document(token=token))
        stdout_file = tempfile.TemporaryFile(mode="w+b", dir=scratch)
        stderr_file = tempfile.TemporaryFile(mode="w+b", dir=scratch)
        warm_candidate_available = (
            allow_warm_start
            or (
                allow_warm_reuse
                and _warm_worker_reuse_available(managed_warm_binding)
            )
        )
        if (
            len(protocol) <= _MAX_REQUEST_BYTES
            and warm_candidate_available
            and _pool_eligible_job(job)
        ):
            last_error: BaseException | None = None
            warm_attempts_exhausted = True
            for _ in range(1 if reserved_slot is not None else 2):
                worker = _checkout_warm_worker(
                    allow_start=allow_warm_start,
                    managed_binding=managed_warm_binding,
                    reserved_slot=reserved_slot,
                )
                if worker is None:
                    warm_attempts_exhausted = False
                    break
                task = _WarmTask(
                    job=job,
                    token=token,
                    stdout_file=stdout_file,
                    stderr_file=stderr_file,
                )
                if worker.assign(task, protocol):
                    return StemWorkerHandle(
                        job=job,
                        token=token,
                        process=worker.process,
                        stdout_file=stdout_file,
                        stderr_file=stderr_file,
                        _warm_worker=worker,
                        _warm_task=task,
                        _started_ns=started_ns,
                        _warm_used=(
                            managed_warm_binding is not None
                            and reserved_slot is None
                        ),
                    )
                last_error = task.error
                _discard_warm_worker(worker, force=True)
            if warm_attempts_exhausted:
                raise StemWorkerError(
                    "warm stem worker could not accept a request"
                    + (
                        f": {type(last_error).__name__}"
                        if last_error is not None
                        else ""
                    )
                )
        if managed_warm_binding is not None:
            # A session-bound request cannot silently escape its immutable
            # global claim by falling through to an unreserved one-shot child.
            raise StemWorkerError(
                "managed warm worker is unavailable for this session"
            )
        with _PROCESS_ADMISSION_LOCK:
            # Keep returning warm workers behind the retirement barrier until
            # the replacement one-shot child physically exists.  A concurrent
            # collect therefore retires its worker instead of making it idle
            # in the narrow gap between retirement and Popen.
            idle_workers = _begin_idle_stem_worker_retirement()
            try:
                _retire_detached_stem_workers(idle_workers)
                _raise_if_warm_worker_quarantined()
                command = [sys.executable, "-m", "tianlai.stem_worker"]
                if reserved_slot is not None:
                    spec = reserved_slot.child_spec
                    command = [
                        sys.executable,
                        os.fspath(
                            Path(__file__).resolve().with_name(
                                "_stem_worker_bootstrap.py"
                            )
                        ),
                        os.fspath(spec.directory),
                        str(spec.slot_index),
                        spec.token,
                        str(spec.parent_pid),
                    ]
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=str(Path(__file__).resolve().parent.parent),
                )
            finally:
                _finish_idle_stem_worker_retirement()
        assert process.stdin is not None
        if reserved_slot is not None:
            try:
                # The lightweight child owns active.lock before it imports
                # Tianlai or reads the potentially large request frame.
                reserved_slot.wait_for_active(process)
            except BaseException:
                _terminate_process(process)
                raise
        try:
            process.stdin.write(protocol)
            process.stdin.close()
        except BaseException:
            _terminate_process(process)
            raise
        return StemWorkerHandle(
            job=job,
            token=token,
            process=process,
            stdout_file=stdout_file,
            stderr_file=stderr_file,
            _worker_slot=reserved_slot,
            _started_ns=started_ns,
        )
    except BaseException:
        if stdout_file is not None:
            stdout_file.close()
        if stderr_file is not None:
            stderr_file.close()
        if reserved_slot is not None:
            reserved_slot.close()
        _GLOBAL_PERMITS.release()
        raise


def try_start_stem_worker(
    job: StemRenderJob,
    *,
    scratch_directory: str | Path,
) -> StemWorkerHandle | None:
    """Start one managed one-shot child using the established public lifecycle.

    The public one-shot API remains unchanged; only Tianlai's internal batch
    coordinator uses the private admission hint.
    """

    return _try_start_stem_worker(
        job,
        scratch_directory=scratch_directory,
        allow_warm_start=False,
        allow_warm_reuse=False,
    )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
    except OSError:
        # The child may have exited between poll and terminate.  If it did
        # not, make one best-effort hard-stop before the mandatory wait.
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as exc:
            raise StemWorkerError(
                "managed stem worker did not stop after kill"
            ) from exc


def terminate_stem_worker(handle: StemWorkerHandle) -> None:
    """Terminate an active child and release all anonymous resources."""

    if handle._owner_pid != os.getpid():
        # A handle inherited through fork does not own the parent's worker.
        # Close only this process's duplicate scratch descriptors; signalling
        # the recorded PID could otherwise kill the parent's healthy child.
        handle.stdout_file.close()
        handle.stderr_file.close()
        handle._finished = True
        handle._permit_held = False
        handle._warm_worker = None
        handle._warm_task = None
        slot = handle._worker_slot
        handle._worker_slot = None
        if slot is not None:
            slot.close()
        return
    if handle._finished:
        slot = handle._worker_slot
        handle._worker_slot = None
        if slot is not None:
            slot.close()
        return
    try:
        if handle._warm_worker is not None:
            worker = handle._warm_worker
            task = handle._warm_task
            _discard_warm_worker(worker, force=True)
            if task is not None:
                worker.release_task(task, success=False)
        else:
            _terminate_process(handle.process)
    finally:
        handle.stdout_file.close()
        handle.stderr_file.close()
        handle._finished = handle.process.poll() is not None
        handle._warm_worker = None
        handle._warm_task = None
        if handle._finished and handle._permit_held:
            _GLOBAL_PERMITS.release()
            handle._permit_held = False
        if handle._finished:
            slot = handle._worker_slot
            handle._worker_slot = None
            if slot is not None:
                slot.close()


def _tail_diagnostic(source: BinaryIO, limit: int = 8192) -> str:
    source.seek(0, os.SEEK_END)
    size = source.tell()
    source.seek(max(0, size - limit))
    return source.read(limit).decode("utf-8", errors="replace").strip()


def _find_marker(source: BinaryIO, marker: bytes) -> int:
    source.seek(0)
    overlap = len(marker) - 1
    position = 0
    previous = b""
    while True:
        chunk = source.read(_COPY_CHUNK_BYTES)
        if not chunk:
            return -1
        combined = previous + chunk
        offset = combined.find(marker)
        if offset >= 0:
            return position - len(previous) + offset
        previous = combined[-overlap:] if overlap else b""
        position += len(chunk)


def _strict_metadata(raw: bytes, job: StemRenderJob, token: str) -> dict[str, Any]:
    metadata = _load_json_object(raw, "stem worker metadata")
    expected_keys = {
        "format",
        "version",
        "token",
        "index",
        "executor_id",
        "sample_rate",
        "frame_count",
        "byte_count",
        "peak_voices",
        "manifest_sha256",
        "audio_sha256",
        "producer_source_tree_sha256",
    }
    if set(metadata) != expected_keys:
        raise StemWorkerError("stem worker metadata has an invalid shape")
    if (
        metadata["format"] != _PROTOCOL_FORMAT
        or metadata["version"] != _PROTOCOL_VERSION
        or metadata["token"] != token
        or metadata["index"] != job.index
        or metadata["executor_id"] != job.executor_id
        or metadata["sample_rate"] != job.sample_rate
        or metadata["frame_count"] != job.frame_count
        or metadata["byte_count"] != job.frame_count * 2 * 4
        or metadata["manifest_sha256"]
        != job.expected_manifest_sha256
        or metadata["producer_source_tree_sha256"]
        != PROCESS_SOURCE_TREE_SHA256
    ):
        raise StemWorkerError("stem worker metadata does not match its request")
    peak_voices = metadata["peak_voices"]
    if (
        isinstance(peak_voices, bool)
        or not isinstance(peak_voices, int)
        or peak_voices < 0
    ):
        raise StemWorkerError("stem worker peak_voices is invalid")
    for key in ("manifest_sha256", "audio_sha256"):
        value = metadata[key]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise StemWorkerError(f"stem worker {key} is invalid")
    return metadata


def _raise_worker_failure(
    process: subprocess.Popen[bytes],
    diagnostic_file: BinaryIO,
    *,
    protocol_error: BaseException | None = None,
) -> None:
    return_code = process.wait()
    diagnostic = _tail_diagnostic(diagnostic_file)
    windows_status = return_code & 0xFFFFFFFF
    if (
        return_code == _RESOURCE_EXHAUSTED_EXIT_CODE
        or return_code == -getattr(signal, "SIGKILL", 9)
        or windows_status in {0xC0000017, 0xC000009A}
    ):
        raise MemoryError(
            "managed stem worker exhausted host resources"
            + (f": {diagnostic}" if diagnostic else "")
        )
    if return_code != 0:
        raise StemWorkerError(
            f"stem worker exited with code {return_code}"
            + (f": {diagnostic}" if diagnostic else "")
        )
    detail = ""
    if protocol_error is not None:
        detail = f": {type(protocol_error).__name__}: {protocol_error}"
    raise StemWorkerError("stem worker returned an invalid protocol" + detail)


def _parse_worker_result(handle: StemWorkerHandle) -> StemWorkerResult:
    begin = _BEGIN_PREFIX + handle.token.encode("ascii") + _MARKER_SUFFIX
    end = _END_PREFIX + handle.token.encode("ascii") + _MARKER_SUFFIX
    begin_offset = _find_marker(handle.stdout_file, begin)
    if begin_offset < 0:
        raise StemWorkerError("stem worker begin marker is missing")
    audio_offset = begin_offset + len(begin)
    byte_count = handle.job.frame_count * 2 * 4
    handle.stdout_file.seek(audio_offset + byte_count)
    length_raw = handle.stdout_file.read(4)
    if len(length_raw) != 4:
        raise StemWorkerError("stem worker metadata length is truncated")
    metadata_length = struct.unpack(">I", length_raw)[0]
    if metadata_length == 0 or metadata_length > _MAX_METADATA_BYTES:
        raise StemWorkerError("stem worker metadata length is invalid")
    metadata_raw = handle.stdout_file.read(metadata_length)
    if len(metadata_raw) != metadata_length:
        raise StemWorkerError("stem worker metadata is truncated")
    metadata = _strict_metadata(metadata_raw, handle.job, handle.token)
    handle.stdout_file.seek(audio_offset)
    remaining = byte_count
    digest = hashlib.sha256()
    while remaining:
        chunk = handle.stdout_file.read(min(_COPY_CHUNK_BYTES, remaining))
        if not chunk:
            raise StemWorkerError("stem worker audio is truncated")
        digest.update(chunk)
        remaining -= len(chunk)
    if digest.hexdigest() != metadata["audio_sha256"]:
        raise StemWorkerError("stem worker audio SHA-256 does not match")
    handle.stdout_file.seek(audio_offset + byte_count + 4 + metadata_length)
    if handle.stdout_file.read(len(end)) != end:
        raise StemWorkerError("stem worker end marker is missing")
    return StemWorkerResult(
        index=handle.job.index,
        executor_id=handle.job.executor_id,
        sample_rate=handle.job.sample_rate,
        frame_count=handle.job.frame_count,
        peak_voices=int(metadata["peak_voices"]),
        manifest_sha256=str(metadata["manifest_sha256"]),
        audio_sha256=str(metadata["audio_sha256"]),
        audio_file=handle.stdout_file,
        audio_offset=audio_offset,
        byte_count=byte_count,
    )


def collect_stem_worker(handle: StemWorkerHandle) -> StemWorkerResult:
    """Wait for and fully validate one child result, retaining only raw audio."""

    if handle._owner_pid != os.getpid():
        terminate_stem_worker(handle)
        raise StemWorkerError("stem worker handle belongs to another process")
    if handle._finished:
        raise StemWorkerError("stem worker handle has already been consumed")
    try:
        worker = handle._warm_worker
        task = handle._warm_task
        if worker is not None:
            assert task is not None
            task.done.wait()
            if not task.response_complete:
                _discard_warm_worker(worker, force=False)
                worker.release_task(task, success=False)
                try:
                    worker._copy_diagnostic_to_task(task)
                except BaseException:
                    pass
                if isinstance(task.error, MemoryError):
                    raise MemoryError(
                        "managed stem worker exhausted host resources"
                    ) from task.error
                _raise_worker_failure(
                    handle.process,
                    handle.stderr_file,
                    protocol_error=task.error,
                )
        else:
            return_code = handle.process.wait()
            if return_code != 0:
                _raise_worker_failure(handle.process, handle.stderr_file)

        result = _parse_worker_result(handle)
        if worker is not None:
            assert task is not None
            if worker.managed_binding is None:
                _return_warm_worker(worker, task)
            else:
                # The managed reservation accounts for both the persistent
                # process and its exact raw result scratch.  Do not make the
                # process available to the next batch until the old result's
                # consumer has closed that scratch lease.
                result._warm_worker = worker
                result._warm_task = task
        handle.stderr_file.close()
        handle._finished = True
        handle._warm_worker = None
        handle._warm_task = None
        if handle._permit_held:
            _GLOBAL_PERMITS.release()
            handle._permit_held = False
        result._elapsed_ns = max(1, time.monotonic_ns() - handle._started_ns)
        result._warm_used = handle._warm_used
        result._worker_slot = handle._worker_slot
        handle._worker_slot = None
        return result
    except BaseException:
        terminate_stem_worker(handle)
        raise


def _write_all(
    descriptor: int,
    payload: bytes | bytearray | memoryview,
) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("stem worker protocol write made no progress")
        view = view[written:]


def _render_child(
    document: dict[str, Any],
    protocol_descriptor: int,
    *,
    verify_live_source: bool = False,
) -> None:
    if verify_live_source and not current_source_tree_matches(
        PROCESS_SOURCE_TREE_SHA256
    ):
        raise RuntimeError("stem worker source tree changed after process start")
    if set(document) != {
        "format",
        "version",
        "token",
        "producer_source_tree_sha256",
        "job",
    }:
        raise ValueError("stem worker request has an invalid shape")
    if (
        document["format"] != _PROTOCOL_FORMAT
        or document["version"] != _PROTOCOL_VERSION
        or document["producer_source_tree_sha256"]
        != PROCESS_SOURCE_TREE_SHA256
    ):
        raise ValueError("stem worker protocol is unsupported")
    token = document["token"]
    _validate_token(token)
    raw_job = document["job"]
    if not isinstance(raw_job, dict) or set(raw_job) != {
        "index",
        "executor_id",
        "manifest_path",
        "expected_manifest_sha256",
        "sample_rate",
        "frame_count",
        "performance",
        "overrides",
    }:
        raise ValueError("stem worker job has an invalid shape")
    job = StemRenderJob(
        index=raw_job["index"],
        executor_id=raw_job["executor_id"],
        manifest_path=raw_job["manifest_path"],
        expected_manifest_sha256=raw_job[
            "expected_manifest_sha256"
        ],
        sample_rate=raw_job["sample_rate"],
        frame_count=raw_job["frame_count"],
        performance_json=canonical_json_bytes(raw_job["performance"]),
        overrides_json=canonical_json_bytes(raw_job["overrides"]),
    )
    _validate_job(job)
    manifest_identity, manifest_bytes = read_plain_file_bytes(
        job.manifest_path,
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    manifest_path = manifest_identity.path
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest_sha256 != job.expected_manifest_sha256:
        raise ValueError("stem worker manifest changed before construction")
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("instrument manifest must be a JSON object")
    overrides = _validated_overrides(
        _load_json_object(job.overrides_json, "overrides_json")
    )
    if overrides:
        manifest = {**manifest, **overrides}
    performance = _load_json_object(job.performance_json, "performance_json")
    performance_document = parse_performance_document(performance)
    instrument = create_instrument(
        manifest,
        job.sample_rate,
        base_directory=str(manifest_path.parent),
    )
    try:
        import numpy as np

        begin = _BEGIN_PREFIX + token.encode("ascii") + _MARKER_SUFFIX
        end = _END_PREFIX + token.encode("ascii") + _MARKER_SUFFIX
        _write_all(protocol_descriptor, begin)
        audio_sha256 = hashlib.sha256()
        byte_count = 0
        if _prefer_frame_stream_path(
            instrument,
            performance_document,
        ):
            frames, peak = render_document(instrument, performance_document)

            def scalars(count: int) -> Any:
                for _ in range(count):
                    left, right = next(frames)
                    yield left
                    yield right

            for start in range(0, job.frame_count, _RENDER_CHUNK_FRAMES):
                count = min(_RENDER_CHUNK_FRAMES, job.frame_count - start)
                chunk = np.fromiter(
                    scalars(count),
                    dtype="<f4",
                    count=count * 2,
                )
                payload = memoryview(chunk).cast("B")
                _write_all(protocol_descriptor, payload)
                audio_sha256.update(payload)
                byte_count += len(payload)
            try:
                next(frames)
            except StopIteration:
                pass
            else:
                raise RuntimeError("stem renderer produced excess frames")
        else:
            blocks, peak = render_document_blocks(
                instrument,
                performance_document,
                maximum_block_frames=_RENDER_CHUNK_FRAMES,
                sample_dtype="float32",
            )
            for block in blocks:
                chunk = np.asarray(block, dtype="<f4", order="C")
                if chunk.ndim != 2 or chunk.shape[1:] != (2,):
                    raise RuntimeError("stem renderer produced an invalid block")
                payload = memoryview(chunk).cast("B")
                _write_all(protocol_descriptor, payload)
                audio_sha256.update(payload)
                byte_count += len(payload)
        if byte_count != job.frame_count * 2 * 4:
            raise RuntimeError("stem renderer produced an invalid byte count")

        close = getattr(instrument, "close", None)
        instrument = None
        if callable(close):
            close()

        metadata = canonical_json_bytes(
            {
                "format": _PROTOCOL_FORMAT,
                "version": _PROTOCOL_VERSION,
                "token": token,
                "index": job.index,
                "executor_id": job.executor_id,
                "sample_rate": job.sample_rate,
                "frame_count": job.frame_count,
                "byte_count": byte_count,
                "peak_voices": int(peak[0]),
                "manifest_sha256": manifest_sha256,
                "audio_sha256": audio_sha256.hexdigest(),
                "producer_source_tree_sha256": PROCESS_SOURCE_TREE_SHA256,
            }
        )
        if len(metadata) > _MAX_METADATA_BYTES:
            raise RuntimeError("stem worker metadata exceeds its bound")
        _write_all(protocol_descriptor, struct.pack(">I", len(metadata)))
        _write_all(protocol_descriptor, metadata)
        _write_all(protocol_descriptor, end)
    finally:
        if instrument is not None:
            close = getattr(instrument, "close", None)
            if callable(close):
                close()


def _persistent_input_reader(
    commands: queue.Queue[bytes | bytearray | BaseException],
) -> None:
    """Read framed commands and terminate promptly when the parent vanishes."""

    try:
        while True:
            header = bytearray()
            while len(header) < 4:
                chunk = os.read(0, 4 - len(header))
                if not chunk:
                    # stdin is a private parent-liveness channel.  An EOF
                    # must stop an active render too, rather than leaving an
                    # orphan.
                    os._exit(0 if not header else 1)
                header.extend(chunk)
            length = struct.unpack(">I", header)[0]
            if length == 0 or length > _MAX_REQUEST_BYTES:
                commands.put(
                    ValueError("warm stem worker request length is invalid")
                )
                return
            payload = bytearray()
            while len(payload) < length:
                chunk = os.read(
                    0,
                    min(_COPY_CHUNK_BYTES, length - len(payload)),
                )
                if not chunk:
                    os._exit(1)
                payload.extend(chunk)
            commands.put(payload)
            del payload
            del chunk
    except (MemoryError, OSError, ValueError) as exc:
        try:
            commands.put_nowait(exc)
        except BaseException:
            os._exit(
                _RESOURCE_EXHAUSTED_EXIT_CODE
                if isinstance(exc, MemoryError)
                else 1
            )


def _persistent_child_loop(
    protocol_descriptor: int,
    *,
    commands: queue.Queue[bytes | bytearray | BaseException] | None = None,
    idle_seconds: float = _WARM_IDLE_SECONDS,
    start_reader: bool = True,
) -> int:
    if commands is None:
        commands = queue.Queue(maxsize=1)
    if start_reader:
        reader = threading.Thread(
            target=_persistent_input_reader,
            args=(commands,),
            name="tianlai-stem-stdin",
            daemon=True,
        )
        reader.start()
    completed = 0
    while completed < _WARM_MAX_JOBS:
        try:
            request = commands.get(timeout=idle_seconds)
        except queue.Empty:
            return 0
        if isinstance(request, BaseException):
            raise request
        document = _load_json_buffer(request, "stem worker request")
        # JSON decoding has produced the authoritative task object; release
        # the potentially 64 MiB framed byte buffer before instrument/render
        # allocations begin, rather than merely before the next idle wait.
        del request
        _render_child(
            document,
            protocol_descriptor,
            verify_live_source=True,
        )
        # The warm process owns no task payload while idle.  In particular,
        # do not retain a large event document or its framed UTF-8 bytes past
        # the response boundary.
        del document
        completed += 1
    return 0


def main() -> int:
    protocol_descriptor = -1
    diagnostic_descriptor = -1
    try:
        # Preserve the inherited protocol/diagnostic streams, then
        # sink fd 1 and fd 2 before any custom/native instrument is loaded.
        # Top-level module-import output precedes this point and remains
        # bounded to Tianlai's own trusted imports.
        protocol_descriptor = os.dup(1)
        diagnostic_descriptor = os.dup(2)
        null_descriptor = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_descriptor, 1)
            os.dup2(null_descriptor, 2)
        finally:
            os.close(null_descriptor)
        if sys.argv[1:] == ["--persistent"]:
            return _persistent_child_loop(protocol_descriptor)
        if sys.argv[1:]:
            raise ValueError("stem worker command line is invalid")
        raw = sys.stdin.buffer.read()
        document = _load_json_object(raw, "stem worker request")
        _render_child(document, protocol_descriptor)
        return 0
    except MemoryError:
        target = diagnostic_descriptor if diagnostic_descriptor >= 0 else 2
        try:
            os.write(target, _RESOURCE_EXHAUSTED_DIAGNOSTIC)
        except OSError:
            pass
        return _RESOURCE_EXHAUSTED_EXIT_CODE
    except BaseException as exc:
        message = f"{type(exc).__name__}: {exc}"
        target = diagnostic_descriptor if diagnostic_descriptor >= 0 else 2
        os.write(
            target,
            message.encode("utf-8", errors="replace")[-8192:],
        )
        return 1
    finally:
        if protocol_descriptor >= 0:
            os.close(protocol_descriptor)
        if diagnostic_descriptor >= 0:
            os.close(diagnostic_descriptor)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "StemRenderJob",
    "StemWorkerError",
    "StemWorkerHandle",
    "StemWorkerResult",
    "collect_stem_worker",
    "managed_subprocess_workers_available",
    "terminate_stem_worker",
    "try_start_stem_worker",
]
