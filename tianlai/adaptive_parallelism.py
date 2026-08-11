"""Private, zero-configuration learning for stem-worker parallelism.

The static policy in :mod:`tianlai.render_parallelism` remains the authority
for backend eligibility and CPU, memory, and scratch safety.  This module only
learns whether a resource-safe worker window is likely to save time on the
current computer.  Missing, stale, noisy, or malformed evidence means "keep
the static decision".

Timings are captured with an internal monotonic clock.  Callers cannot submit
an elapsed duration, and failed, cancelled, or cache-served tasks are always
discarded.  The small optional state file is private to the current user,
bounded, checksum-protected, atomically replaced, and guarded by the same
cross-process OS-lock primitive used for render publication.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import stat
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from .plain_file import read_plain_file_bytes
from .render_lock import (
    RenderLockError,
    acquire_render_lock,
    capture_plain_directory,
    revalidate_plain_directory,
)


_STATE_FORMAT = "tianlai.adaptive_parallelism"
_STATE_VERSION = 1
_MAX_STATE_BYTES = 64 * 1024
_MAX_BACKENDS = 32
_MAX_SAMPLES_PER_ROUTE = 8
_MAX_GENERATION = 2**63 - 1
_MAX_WORK_FRAMES = 1_000_000_000_000
_MIN_ELAPSED_MICROSECONDS = 100
_MAX_ELAPSED_MICROSECONDS = 24 * 60 * 60 * 1_000_000
_MAX_BACKEND_KEY_BYTES = 512
_MAX_OVERRIDE_DOCUMENT_BYTES = 16 * 1024
_MAX_WORKERS = 4
_MANAGED_EXECUTIONS = frozenset({"managed_cold", "managed_warm"})
_MIN_MODEL_SAMPLES = 6
_MIN_DISTINCT_WORK_VALUES = 3
_MIN_WORK_SPAN_RATIO = 1.75
_MAX_RELATIVE_MODEL_ERROR = 0.25
_PREDICTION_RELATIVE_MARGIN = 0.15
_PREDICTION_ERROR_MULTIPLIER = 2.5
_MIN_PREDICTION_MARGIN_SECONDS = 0.005
_MIN_RELATIVE_SAVING = 0.20
_MIN_ABSOLUTE_SAVING_SECONDS = 0.075
_BACKEND_ID_HEX_LENGTH = 64
_MAX_LIVE_TIMINGS = 256
_MAX_PENDING_OBSERVATIONS = 256
_MAX_EXPLORATION_KEYS = 64
_MAX_LOCAL_ADOPTED_KEYS = 256


class AdaptiveParallelismError(RuntimeError):
    """Adaptive state is unavailable or unsafe; rendering must continue."""


@dataclass(frozen=True, slots=True)
class AdaptiveWorkload:
    """One trusted backend identity and its bounded render-work estimate."""

    backend_key: str
    work_frames: int


@dataclass(frozen=True, slots=True)
class AdaptiveParallelismRecommendation:
    """An optional benefit limit to feed back into the static policy.

    ``worker_limit=None`` means that learning is uncertain and the unchanged
    static decision should be used.  This is deliberately different from a
    learned limit of one worker.
    """

    worker_limit: int | None
    allow_short_workload: bool
    reason: str


@dataclass(frozen=True, slots=True)
class AdaptiveTimingToken:
    """Opaque live timing owned by one advisor instance."""

    _advisor_nonce: str
    _ticket_id: int


@dataclass(frozen=True, slots=True)
class AdaptiveCompletedTimingToken:
    """Opaque elapsed timing awaiting authoritative result adoption."""

    _advisor_nonce: str
    _ticket_id: int


@dataclass(frozen=True, slots=True)
class _LiveTiming:
    backend_id: str
    work_frames: int
    route: str | None
    started_at: float


@dataclass(frozen=True, slots=True)
class _CompletedTiming:
    timing: _LiveTiming
    elapsed_microseconds: int


@dataclass(frozen=True, slots=True)
class _PendingObservation:
    sequence: int
    timing: _LiveTiming
    elapsed_microseconds: int


@dataclass(slots=True)
class _ExplorationProgress:
    """Process-local guard for one bounded downward experiment chain."""

    pending_width: int | None = None
    pending_observation_count: int = 0
    attempts_by_width: dict[int, int] = field(default_factory=dict)
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class _TimingModel:
    minimum_work: int
    maximum_work: int
    intercept_seconds: float
    seconds_per_frame: float
    root_mean_square_error: float

    def bounds(self, work_frames: int) -> tuple[float, float] | None:
        # Do not extrapolate a learned line far outside its observed range.
        if (
            work_frames * 2 < self.minimum_work
            or work_frames > self.maximum_work * 2
        ):
            return None
        estimate = max(
            _MIN_PREDICTION_MARGIN_SECONDS,
            self.intercept_seconds + self.seconds_per_frame * work_frames,
        )
        uncertainty = max(
            _MIN_PREDICTION_MARGIN_SECONDS,
            _PREDICTION_ERROR_MULTIPLIER
            * self.root_mean_square_error,
            _PREDICTION_RELATIVE_MARGIN * estimate,
        )
        return max(0.0, estimate - uncertainty), estimate + uncertainty


def _duplicate_safe_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate adaptive-state key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid adaptive-state number: {value}")


def _positive_int(
    value: object,
    *,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > maximum
    ):
        raise ValueError("adaptive-state integer is outside its bound")
    return value


def _backend_id(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value:
        return None
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeError:
        return None
    if len(encoded) > _MAX_BACKEND_KEY_BYTES:
        return None
    return hashlib.sha256(encoded).hexdigest()


def make_adaptive_backend_key(
    *,
    manifest_sha256: str,
    engine_sha256: str,
    overrides_json: bytes,
    sample_backed: bool,
) -> str | None:
    """Bind timings to one engine, manifest, and effective backend variant.

    ``overrides_json`` should be the same canonical private document sent to
    a managed worker.  It is parsed and re-canonicalised here so harmless byte
    spelling differences cannot fragment the profile.  The returned digest
    contains no project path, instrument name, or override value.
    """

    for digest in (manifest_sha256, engine_sha256):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return None
    if type(sample_backed) is not bool:
        return None
    if (
        type(overrides_json) is not bytes
        or not 2 <= len(overrides_json) <= _MAX_OVERRIDE_DOCUMENT_BYTES
    ):
        return None
    try:
        overrides = json.loads(
            overrides_json.decode("utf-8"),
            object_pairs_hook=_duplicate_safe_object,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(overrides, dict):
            return None
        canonical_overrides = _canonical_payload(overrides)
    except (
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ):
        return None
    identity = b"\x00".join(
        (
            b"tianlai-adaptive-backend-v1",
            manifest_sha256.encode("ascii"),
            engine_sha256.encode("ascii"),
            b"sample" if sample_backed else b"dsp",
            canonical_overrides,
        )
    )
    return hashlib.sha256(identity).hexdigest()


def _work_frames(value: object) -> int | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > _MAX_WORK_FRAMES
    ):
        return None
    return value


def _route(execution: object, concurrent_workers: object) -> str | None:
    if (
        execution == "serial"
        and type(concurrent_workers) is int
        and concurrent_workers == 1
    ):
        return "serial"
    if (
        isinstance(execution, str)
        and execution in _MANAGED_EXECUTIONS
        and type(concurrent_workers) is int
        and 2 <= concurrent_workers <= _MAX_WORKERS
    ):
        return f"{execution}:{concurrent_workers}"
    return None


def _machine_identity(
    *,
    platform_system: str,
    platform_machine: str,
    cpu_count: int,
) -> str:
    document = {
        "cpu_count": cpu_count,
        "implementation": platform.python_implementation(),
        "machine": platform_machine,
        "processor": platform.processor(),
        "python_cache_tag": sys.implementation.cache_tag or "",
        "system": platform_system,
    }
    payload = json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def default_adaptive_state_directory(
    *,
    platform_system: str | None = None,
    environ: Mapping[str, str] | None = None,
    home_directory: Path | None = None,
) -> Path:
    """Return stable per-user storage without exposing a configuration knob."""

    system = platform.system() if platform_system is None else platform_system
    environment = os.environ if environ is None else environ
    if system == "Windows":
        raw = environment.get("LOCALAPPDATA")
        if not raw:
            raise AdaptiveParallelismError(
                "per-user Windows adaptive storage is unavailable"
            )
        base = Path(raw)
        if not base.is_absolute():
            raise AdaptiveParallelismError(
                "per-user Windows adaptive storage is not absolute"
            )
        return base / "Tianlai" / "adaptive-parallelism"
    if system == "Darwin":
        home = Path.home() if home_directory is None else home_directory
        if not home.is_absolute():
            raise AdaptiveParallelismError("per-user home is not absolute")
        return home / "Library" / "Application Support" / "Tianlai" / (
            "adaptive-parallelism"
        )
    if system == "Linux":
        raw_state = environment.get("XDG_STATE_HOME")
        if raw_state:
            base = Path(raw_state)
            if not base.is_absolute():
                raise AdaptiveParallelismError(
                    "per-user XDG state storage is not absolute"
                )
        else:
            home = Path.home() if home_directory is None else home_directory
            if not home.is_absolute():
                raise AdaptiveParallelismError("per-user home is not absolute")
            base = home / ".local" / "state"
        return base / "tianlai" / "adaptive-parallelism"
    raise AdaptiveParallelismError("unsupported adaptive-state platform")


def _ensure_private_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise AdaptiveParallelismError("adaptive-state path is not absolute")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        lexical = os.lstat(path)
        resolved = path.resolve(strict=True)
        current = os.lstat(resolved)
    except OSError as exc:
        raise AdaptiveParallelismError(
            "adaptive-state directory is unavailable"
        ) from exc
    is_reparse = bool(
        getattr(lexical, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )
    if (
        not stat.S_ISDIR(lexical.st_mode)
        or stat.S_ISLNK(lexical.st_mode)
        or is_reparse
        or not os.path.samestat(lexical, current)
    ):
        raise AdaptiveParallelismError(
            "adaptive-state directory is not a plain directory"
        )
    if os.name != "nt":
        if current.st_uid != os.geteuid():
            raise AdaptiveParallelismError(
                "adaptive-state directory has a different owner"
            )
        if current.st_mode & 0o077:
            try:
                os.chmod(resolved, 0o700)
            except OSError as exc:
                raise AdaptiveParallelismError(
                    "adaptive-state directory cannot be made private"
                ) from exc
            if os.lstat(resolved).st_mode & 0o077:
                raise AdaptiveParallelismError(
                    "adaptive-state directory is not private"
                )
    return resolved


def _empty_state(machine: str) -> dict[str, Any]:
    return {
        "format": _STATE_FORMAT,
        "version": _STATE_VERSION,
        "machine": machine,
        "generation": 0,
        "backends": {},
    }


def _canonical_payload(document: dict[str, Any]) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _encode_state(document: dict[str, Any]) -> bytes:
    payload = _canonical_payload(document)
    wrapper = {
        "payload": document,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    encoded = _canonical_payload(wrapper) + b"\n"
    if len(encoded) > _MAX_STATE_BYTES:
        raise AdaptiveParallelismError("adaptive state exceeds its byte bound")
    return encoded


def _validate_samples(value: object) -> list[list[int]]:
    if not isinstance(value, list) or len(value) > _MAX_SAMPLES_PER_ROUTE:
        raise ValueError("adaptive sample list is invalid")
    samples: list[list[int]] = []
    for raw in value:
        if not isinstance(raw, list) or len(raw) != 2:
            raise ValueError("adaptive sample is invalid")
        work = _positive_int(raw[0], maximum=_MAX_WORK_FRAMES)
        elapsed = _positive_int(
            raw[1], maximum=_MAX_ELAPSED_MICROSECONDS
        )
        if elapsed < _MIN_ELAPSED_MICROSECONDS:
            raise ValueError("adaptive elapsed time is too small")
        samples.append([work, elapsed])
    return samples


def _validate_state(value: object, machine: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "format", "version", "machine", "generation", "backends"
    }:
        raise ValueError("adaptive state fields are invalid")
    if (
        value["format"] != _STATE_FORMAT
        or value["version"] != _STATE_VERSION
        or value["machine"] != machine
    ):
        raise ValueError("adaptive state identity is invalid")
    generation = value["generation"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or generation > _MAX_GENERATION
    ):
        raise ValueError("adaptive generation is invalid")
    raw_backends = value["backends"]
    if not isinstance(raw_backends, dict) or len(raw_backends) > _MAX_BACKENDS:
        raise ValueError("adaptive backend map is invalid")
    backends: dict[str, Any] = {}
    for backend, raw_backend in raw_backends.items():
        if (
            not isinstance(backend, str)
            or len(backend) != _BACKEND_ID_HEX_LENGTH
            or any(character not in "0123456789abcdef" for character in backend)
            or not isinstance(raw_backend, dict)
            or set(raw_backend) != {"touch", "routes"}
        ):
            raise ValueError("adaptive backend entry is invalid")
        touch = raw_backend["touch"]
        if (
            isinstance(touch, bool)
            or not isinstance(touch, int)
            or touch < 0
            or touch > generation
        ):
            raise ValueError("adaptive backend touch is invalid")
        raw_routes = raw_backend["routes"]
        if not isinstance(raw_routes, dict) or len(raw_routes) > 7:
            raise ValueError("adaptive route map is invalid")
        routes: dict[str, list[list[int]]] = {}
        for route_name, samples in raw_routes.items():
            if route_name not in {
                "serial",
                "managed_cold:2", "managed_cold:3", "managed_cold:4",
                "managed_warm:2", "managed_warm:3", "managed_warm:4",
            }:
                raise ValueError("adaptive route is invalid")
            routes[route_name] = _validate_samples(samples)
        backends[backend] = {"touch": touch, "routes": routes}
    return {
        "format": _STATE_FORMAT,
        "version": _STATE_VERSION,
        "machine": machine,
        "generation": generation,
        "backends": backends,
    }


def _decode_state(payload: bytes, machine: str) -> dict[str, Any]:
    wrapper = json.loads(
        payload.decode("ascii"),
        object_pairs_hook=_duplicate_safe_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(wrapper, dict) or set(wrapper) != {"payload", "sha256"}:
        raise ValueError("adaptive state wrapper is invalid")
    digest = wrapper["sha256"]
    document = wrapper["payload"]
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or hashlib.sha256(_canonical_payload(document)).hexdigest() != digest
    ):
        raise ValueError("adaptive state checksum is invalid")
    return _validate_state(document, machine)


def _fit_model(samples: Sequence[Sequence[int]]) -> _TimingModel | None:
    if len(samples) < _MIN_MODEL_SAMPLES:
        return None
    work = [int(sample[0]) for sample in samples]
    elapsed = [int(sample[1]) / 1_000_000.0 for sample in samples]
    distinct = set(work)
    minimum = min(work)
    maximum = max(work)
    if (
        len(distinct) < _MIN_DISTINCT_WORK_VALUES
        or maximum < minimum * _MIN_WORK_SPAN_RATIO
    ):
        return None
    mean_x = math.fsum(work) / len(work)
    mean_y = math.fsum(elapsed) / len(elapsed)
    variance_x = math.fsum((value - mean_x) ** 2 for value in work)
    if not math.isfinite(variance_x) or variance_x <= 0.0:
        return None
    covariance = math.fsum(
        (x - mean_x) * (y - mean_y)
        for x, y in zip(work, elapsed, strict=True)
    )
    slope = covariance / variance_x
    if not math.isfinite(slope) or slope <= 0.0:
        return None
    intercept = max(0.0, mean_y - slope * mean_x)
    predicted = [intercept + slope * value for value in work]
    if any(not math.isfinite(value) or value <= 0.0 for value in predicted):
        return None
    rmse = math.sqrt(
        math.fsum(
            (observed - expected) ** 2
            for observed, expected in zip(elapsed, predicted, strict=True)
        )
        / len(work)
    )
    if (
        not math.isfinite(rmse)
        or rmse / max(_MIN_PREDICTION_MARGIN_SECONDS, mean_y)
        > _MAX_RELATIVE_MODEL_ERROR
    ):
        return None
    return _TimingModel(minimum, maximum, intercept, slope, rmse)


def _timing_bounds(
    samples: Sequence[Sequence[int]],
    work_frames: int,
) -> tuple[float, float] | None:
    """Return conservative bounds from a range model or an exact-work model.

    The existing linear model remains the only way to predict across work
    values.  Repeated renders of one unchanged project can nevertheless build
    useful evidence: when at least six samples have the *exact* requested
    work value, a stable mean/RMSE model may describe only that value.  It is
    never used for a neighbouring workload or extrapolation.
    """

    model = _fit_model(samples)
    if model is not None:
        bounds = model.bounds(work_frames)
        if bounds is not None:
            return bounds

    exact_elapsed: list[float] = []
    for sample in samples:
        try:
            sample_work = sample[0]
            elapsed = int(sample[1]) / 1_000_000.0
        except (IndexError, TypeError, ValueError, OverflowError):
            return None
        if sample_work != work_frames:
            continue
        if not math.isfinite(elapsed) or elapsed <= 0.0:
            return None
        exact_elapsed.append(elapsed)
    if len(exact_elapsed) < _MIN_MODEL_SAMPLES:
        return None

    mean = math.fsum(exact_elapsed) / len(exact_elapsed)
    rmse = math.sqrt(
        math.fsum((observed - mean) ** 2 for observed in exact_elapsed)
        / len(exact_elapsed)
    )
    if (
        not math.isfinite(mean)
        or mean <= 0.0
        or not math.isfinite(rmse)
        or rmse / max(_MIN_PREDICTION_MARGIN_SECONDS, mean)
        > _MAX_RELATIVE_MODEL_ERROR
    ):
        return None
    estimate = max(_MIN_PREDICTION_MARGIN_SECONDS, mean)
    uncertainty = max(
        _MIN_PREDICTION_MARGIN_SECONDS,
        _PREDICTION_ERROR_MULTIPLIER * rmse,
        _PREDICTION_RELATIVE_MARGIN * estimate,
    )
    return max(0.0, estimate - uncertainty), estimate + uncertainty


def _parallel_wall_time(
    durations: Sequence[float],
    workers: int,
) -> float:
    # The execution layer preserves plan order and fills a bounded sliding
    # window, so consecutive groups are the conservative prediction shape.
    return math.fsum(
        max(durations[index : index + workers])
        for index in range(0, len(durations), workers)
    )


def _meaningfully_faster(candidate: float, baseline: float) -> bool:
    """Require the same conservative margin used for serial/managed choice."""

    return (
        baseline - candidate >= _MIN_ABSOLUTE_SAVING_SECONDS
        and candidate <= baseline * (1.0 - _MIN_RELATIVE_SAVING)
    )


class AdaptiveParallelismAdvisor:
    """Learn conservative per-backend benefit without changing render output."""

    def __init__(
        self,
        *,
        state_directory: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        platform_system: str | None = None,
        platform_machine: str | None = None,
        cpu_count: int | None = None,
    ) -> None:
        self._clock = clock
        self._system = platform.system() if platform_system is None else (
            platform_system
        )
        self._machine_name = (
            platform.machine() if platform_machine is None else platform_machine
        )
        raw_cpu_count = os.cpu_count() if cpu_count is None else cpu_count
        self._cpu_count = (
            raw_cpu_count
            if isinstance(raw_cpu_count, int)
            and not isinstance(raw_cpu_count, bool)
            and raw_cpu_count > 0
            else 1
        )
        self._machine = _machine_identity(
            platform_system=self._system,
            platform_machine=self._machine_name,
            cpu_count=self._cpu_count,
        )
        self._nonce = secrets.token_hex(16)
        self._owner_pid = os.getpid()
        self._next_ticket = 1
        self._next_pending_sequence = 1
        self._tickets: dict[int, _LiveTiming] = {}
        self._completed: dict[int, _CompletedTiming] = {}
        self._pending: list[_PendingObservation] = []
        self._locally_adopted: dict[tuple[str, int, str], None] = {}
        # Exploration is intentionally process-local.  Persisted timing
        # samples may eventually prove a route, but an abandoned or bad
        # experiment cannot create a durable retry loop.
        self._exploration: dict[str, _ExplorationProgress] = {}
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._volatile_state = _empty_state(self._machine)
        self._persistence_disabled = False
        self._state_directory: Path | None = None
        try:
            requested = (
                default_adaptive_state_directory(platform_system=self._system)
                if state_directory is None
                else Path(state_directory)
            )
            self._state_directory = _ensure_private_directory(requested)
        except (AdaptiveParallelismError, OSError, RuntimeError, ValueError):
            # Learning is optional.  Never make a render depend on home/state
            # directory availability or platform-specific locking support.
            self._state_directory = None

    @property
    def persistence_available(self) -> bool:
        if os.getpid() != self._owner_pid:
            return False
        with self._lock:
            return (
                self._state_directory is not None
                and not self._persistence_disabled
            )

    @property
    def persistence_disabled(self) -> bool:
        if os.getpid() != self._owner_pid:
            return True
        with self._lock:
            return self._persistence_disabled

    @property
    def pending_observation_count(self) -> int:
        if os.getpid() != self._owner_pid:
            return 0
        with self._lock:
            return len(self._pending)

    @property
    def state_path(self) -> Path | None:
        if self._state_directory is None:
            return None
        return self._state_directory / f"timings-{self._machine[:16]}-v1.json"

    def begin_task(
        self,
        *,
        backend_key: str,
        work_frames: int,
        execution: str,
        concurrent_workers: int,
        cache_hit: bool = False,
    ) -> AdaptiveTimingToken | None:
        """Start one trusted monotonic timing, or decline malformed/cache work."""

        if os.getpid() != self._owner_pid:
            return None
        backend = _backend_id(backend_key)
        work = _work_frames(work_frames)
        route_name = _route(execution, concurrent_workers)
        if (
            cache_hit is not False
            or backend is None
            or work is None
            or route_name is None
        ):
            return None
        try:
            started = float(self._clock())
        except (TypeError, ValueError, OverflowError, OSError, RuntimeError):
            return None
        if not math.isfinite(started):
            return None
        with self._lock:
            if (
                len(self._tickets) + len(self._completed)
                >= _MAX_LIVE_TIMINGS
            ):
                return None
            ticket = self._next_ticket
            self._next_ticket += 1
            self._tickets[ticket] = _LiveTiming(
                backend, work, route_name, started
            )
        return AdaptiveTimingToken(self._nonce, ticket)

    def begin_managed_task(
        self,
        *,
        backend_key: str,
        work_frames: int,
        cache_hit: bool = False,
    ) -> AdaptiveTimingToken | None:
        """Start a managed timing before cold/warm routing is known.

        Process startup is part of a cold task's cost, so the timer must begin
        before checkout/spawn.  ``freeze_task`` binds the actual route and
        actual batch width after the handle has reported whether it reused a
        session worker.
        """

        if os.getpid() != self._owner_pid:
            return None
        backend = _backend_id(backend_key)
        work = _work_frames(work_frames)
        if cache_hit is not False or backend is None or work is None:
            return None
        try:
            started = float(self._clock())
        except (TypeError, ValueError, OverflowError, OSError, RuntimeError):
            return None
        if not math.isfinite(started):
            return None
        with self._lock:
            if (
                len(self._tickets) + len(self._completed)
                >= _MAX_LIVE_TIMINGS
            ):
                return None
            ticket = self._next_ticket
            self._next_ticket += 1
            self._tickets[ticket] = _LiveTiming(
                backend, work, None, started
            )
        return AdaptiveTimingToken(self._nonce, ticket)

    def freeze_task(
        self,
        token: AdaptiveTimingToken | None,
        *,
        execution: str | None = None,
        concurrent_workers: int | None = None,
    ) -> AdaptiveCompletedTimingToken | None:
        """Freeze elapsed time after collection and before source consumption.

        Managed workers can finish rendering well before their ordered stem is
        mixed.  Freezing after collection conservatively includes the parent
        protocol and complete raw-audio SHA validation, while keeping later
        cache I/O, ordered source consumption and downstream mixing out of the
        model.  The caller commits this observation only if that exact source
        is fully verified and adopted.
        """

        if (
            os.getpid() != self._owner_pid
            or
            not isinstance(token, AdaptiveTimingToken)
            or token._advisor_nonce != self._nonce
        ):
            return None
        with self._lock:
            live = self._tickets.pop(token._ticket_id, None)
        if live is None:
            return None
        route_name = live.route
        if route_name is None:
            route_name = _route(execution, concurrent_workers)
            if route_name is None or not route_name.startswith("managed_"):
                return None
            live = _LiveTiming(
                live.backend_id,
                live.work_frames,
                route_name,
                live.started_at,
            )
        elif execution is not None or concurrent_workers is not None:
            # A route already bound by begin_task cannot be retagged later.
            return None
        try:
            finished = float(self._clock())
        except (TypeError, ValueError, OverflowError, OSError, RuntimeError):
            return None
        elapsed = finished - live.started_at
        if not math.isfinite(elapsed) or elapsed <= 0.0:
            return None
        elapsed_us = round(elapsed * 1_000_000)
        if not (
            _MIN_ELAPSED_MICROSECONDS
            <= elapsed_us
            <= _MAX_ELAPSED_MICROSECONDS
        ):
            return None
        with self._lock:
            if (
                len(self._tickets) + len(self._completed)
                >= _MAX_LIVE_TIMINGS
            ):
                return None
            self._completed[token._ticket_id] = _CompletedTiming(
                live, elapsed_us
            )
        return AdaptiveCompletedTimingToken(
            self._nonce, token._ticket_id
        )

    def commit_task(
        self,
        token: AdaptiveCompletedTimingToken | None,
        *,
        succeeded: bool,
        cancelled: bool = False,
        cache_hit: bool = False,
    ) -> bool:
        """Adopt one frozen timing only after its authoritative result."""

        if (
            os.getpid() != self._owner_pid
            or
            not isinstance(token, AdaptiveCompletedTimingToken)
            or token._advisor_nonce != self._nonce
        ):
            return False
        with self._lock:
            completed = self._completed.pop(token._ticket_id, None)
        if completed is None:
            return False
        if (
            succeeded is not True
            or cancelled is not False
            or cache_hit is not False
        ):
            return False
        return self._record_volatile(
            completed.timing, completed.elapsed_microseconds
        )

    def finish_task(
        self,
        token: AdaptiveTimingToken | None,
        *,
        succeeded: bool,
        cancelled: bool = False,
        cache_hit: bool = False,
    ) -> bool:
        """Freeze and immediately adopt one synchronous task timing."""

        completed = self.freeze_task(token)
        return self.commit_task(
            completed,
            succeeded=succeeded,
            cancelled=cancelled,
            cache_hit=cache_hit,
        )

    def discard_task(
        self,
        token: AdaptiveTimingToken | AdaptiveCompletedTimingToken | None,
    ) -> None:
        """Consume a timing without teaching the model."""

        if (
            os.getpid() != self._owner_pid
            or
            not isinstance(
                token,
                (AdaptiveTimingToken, AdaptiveCompletedTimingToken),
            )
            or token._advisor_nonce != self._nonce
        ):
            return
        with self._lock:
            if isinstance(token, AdaptiveTimingToken):
                self._tickets.pop(token._ticket_id, None)
            elif isinstance(token, AdaptiveCompletedTimingToken):
                self._completed.pop(token._ticket_id, None)

    def _load_persisted(self) -> dict[str, Any] | None:
        path = self.state_path
        if path is None:
            return None
        try:
            _identity, payload = read_plain_file_bytes(
                path, maximum_bytes=_MAX_STATE_BYTES
            )
            return _decode_state(payload, self._machine)
        except (
            FileNotFoundError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
        ):
            return None

    def _best_state(self) -> dict[str, Any]:
        persisted = (
            None if self._persistence_disabled else self._load_persisted()
        )
        with self._lock:
            volatile = copy.deepcopy(self._volatile_state)
        if persisted is None:
            return volatile
        if persisted["generation"] >= volatile["generation"]:
            return persisted
        return volatile

    def _append_sample_in_place(
        self,
        current: dict[str, Any],
        live: _LiveTiming,
        elapsed_us: int,
    ) -> dict[str, Any]:
        generation = current["generation"] + 1
        if generation > _MAX_GENERATION:
            current.clear()
            current.update(_empty_state(self._machine))
            generation = 1
        backends = current["backends"]
        backend = backends.setdefault(
            live.backend_id, {"touch": generation, "routes": {}}
        )
        backend["touch"] = generation
        samples = backend["routes"].setdefault(live.route, [])
        samples.append([live.work_frames, elapsed_us])
        del samples[:-_MAX_SAMPLES_PER_ROUTE]
        current["generation"] = generation
        if len(backends) > _MAX_BACKENDS:
            victim = min(
                backends,
                key=lambda key: (backends[key]["touch"], key),
            )
            if victim != live.backend_id:
                del backends[victim]
            else:
                alternatives = [key for key in backends if key != victim]
                if alternatives:
                    del backends[
                        min(
                            alternatives,
                            key=lambda key: (backends[key]["touch"], key),
                        )
                    ]
        return current

    def _record_volatile(self, live: _LiveTiming, elapsed_us: int) -> bool:
        with self._lock:
            should_persist = (
                self._state_directory is not None
                and not self._persistence_disabled
            )
            if should_persist and (
                len(self._pending) >= _MAX_PENDING_OBSERVATIONS
            ):
                # Learning is optional.  Do not let a caller that never
                # reaches the stem-phase flush grow either queue or model.
                return False
            self._append_sample_in_place(
                self._volatile_state, live, elapsed_us
            )
            if live.route is not None:
                local_key = (
                    live.backend_id,
                    live.work_frames,
                    live.route,
                )
                self._locally_adopted[local_key] = None
                if len(self._locally_adopted) > _MAX_LOCAL_ADOPTED_KEYS:
                    del self._locally_adopted[
                        next(iter(self._locally_adopted))
                    ]
            if should_persist:
                sequence = self._next_pending_sequence
                self._next_pending_sequence += 1
                self._pending.append(
                    _PendingObservation(sequence, live, elapsed_us)
                )
        return True

    def flush(self) -> bool:
        """Persist one bounded snapshot after a complete stem phase.

        Concurrent successful finishes remain queued for a later flush.  A
        filesystem, lock, or verification failure keeps the valid volatile
        model and pending observations but disables further persistence in
        this process.  Rendering never waits for retries and never depends on
        this return value.
        """

        if os.getpid() != self._owner_pid:
            return False
        with self._flush_lock:
            return self._flush_locked()

    def _flush_locked(self) -> bool:
        """Perform one locally serialized cross-process state merge."""

        with self._lock:
            if not self._pending:
                return True
            if self._persistence_disabled or self._state_directory is None:
                return False
            snapshot = tuple(self._pending)
        path = self.state_path
        assert path is not None
        try:
            assert self._state_directory is not None
            parent_identity = capture_plain_directory(self._state_directory)
            with acquire_render_lock(
                path,
                parent_identity=parent_identity,
                existing_target_kind="file",
            ):
                persisted = self._load_persisted()
                merged_base = (
                    persisted
                    if persisted is not None
                    else _empty_state(self._machine)
                )
                for pending in snapshot:
                    self._append_sample_in_place(
                        merged_base,
                        pending.timing,
                        pending.elapsed_microseconds,
                    )
                payload = _encode_state(merged_base)
                revalidate_plain_directory(parent_identity)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".timings-{self._machine[:16]}-v1.",
                    suffix=".tmp",
                    dir=self._state_directory,
                )
                try:
                    with os.fdopen(descriptor, "wb") as handle:
                        handle.write(payload)
                        handle.flush()
                        os.fsync(handle.fileno())
                except BaseException:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    raise
                temporary = Path(temporary_name)
                try:
                    revalidate_plain_directory(parent_identity)
                    os.replace(temporary, path)
                    if os.name != "nt":
                        directory_fd = os.open(
                            self._state_directory,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                        )
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
                    _identity, verified = read_plain_file_bytes(
                        path, maximum_bytes=_MAX_STATE_BYTES
                    )
                    if verified != payload:
                        raise AdaptiveParallelismError(
                            "adaptive-state atomic write was not stable"
                        )
                except BaseException:
                    # Never unlink this pathname after closing its descriptor:
                    # another same-user actor could have replaced the name.
                    # The random file is private and bounded; disabling this
                    # advisor is safer than an identity-unbound cleanup.
                    raise
            snapshot_sequences = {
                pending.sequence for pending in snapshot
            }
            with self._lock:
                self._pending = [
                    pending
                    for pending in self._pending
                    if pending.sequence not in snapshot_sequences
                ]
                rebuilt = merged_base
                for pending in self._pending:
                    self._append_sample_in_place(
                        rebuilt,
                        pending.timing,
                        pending.elapsed_microseconds,
                    )
                self._volatile_state = rebuilt
            return True
        except MemoryError:
            with self._lock:
                self._persistence_disabled = True
            raise
        except (
            AdaptiveParallelismError,
            RenderLockError,
            FileExistsError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            OverflowError,
        ):
            with self._lock:
                self._persistence_disabled = True
            return False

    def observation_count(
        self,
        *,
        backend_key: str,
        execution: str,
        concurrent_workers: int,
    ) -> int:
        """Return a bounded diagnostic count for tests and engine telemetry."""

        if os.getpid() != self._owner_pid:
            return 0
        backend = _backend_id(backend_key)
        route_name = _route(execution, concurrent_workers)
        if backend is None or route_name is None:
            return 0
        entry = self._best_state()["backends"].get(backend)
        if not isinstance(entry, dict):
            return 0
        samples = entry["routes"].get(route_name, ())
        return len(samples)

    @staticmethod
    def _relevant_route_observation_count(
        state: Mapping[str, Any],
        normalized: Sequence[tuple[str, int]],
        route_name: str,
    ) -> int:
        """Count exact-work evidence without multiplying duplicate parts."""

        total = 0
        for backend, work in dict.fromkeys(normalized):
            entry = state["backends"].get(backend)
            if not isinstance(entry, dict):
                continue
            for sample in entry["routes"].get(route_name, ()):
                if sample[0] == work:
                    total += 1
        return total

    @staticmethod
    def _route_is_safely_under_sampled(
        state: Mapping[str, Any],
        normalized: Sequence[tuple[str, int]],
        route_name: str,
    ) -> bool:
        """Permit exploration only for absence, never noisy/stale evidence."""

        needs_sample = False
        for backend, work in dict.fromkeys(normalized):
            entry = state["backends"].get(backend)
            samples = (
                ()
                if not isinstance(entry, dict)
                else entry["routes"].get(route_name, ())
            )
            if _timing_bounds(samples, work) is not None:
                continue
            # Six unusable observations are evidence of noise, incompatible
            # work ranges, or a persistently bad route.  Trying that route
            # again would amplify stale state rather than explore an unknown.
            if len(samples) >= _MIN_MODEL_SAMPLES:
                return False
            needs_sample = True
        return needs_sample

    def _recommend_downward_exploration(
        self,
        *,
        state: Mapping[str, Any],
        normalized: Sequence[tuple[str, int]],
        managed_execution: str,
        static_workers: int,
        route_walls: Mapping[int, float | None],
    ) -> AdaptiveParallelismRecommendation | None:
        """Offer at most one process-local trial along a proven down-chain.

        The static route must already have a stable profitable model before
        this helper is called.  Each narrower width is tried only after the
        immediately wider width is proven, for at most six successful trials,
        and its completed stable model must beat that wider route by the
        normal conservative benefit margin.  A trial which produces no
        adopted observation leaves the chain parked on the static route; a
        noisy or unprofitable result blocks it for the rest of this process.
        """

        key_payload = json.dumps(
            (managed_execution, static_workers, tuple(normalized)),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
        key = hashlib.sha256(key_payload).hexdigest()

        with self._lock:
            static_route = f"{managed_execution}:{static_workers}"
            if not any(
                (backend, work, static_route) in self._locally_adopted
                for backend, work in dict.fromkeys(normalized)
            ):
                # A fresh one-shot CLI may use persisted models, but it does
                # not immediately inherit another process's willingness to
                # experiment.  One successful static-route observation in
                # this advisor lifetime is the zero-configuration anchor.
                return None
            progress = self._exploration.get(key)
            if progress is None:
                if len(self._exploration) >= _MAX_EXPLORATION_KEYS:
                    # Never evict a failure tombstone and accidentally retry
                    # it later in a very long-lived host process.  Reaching
                    # the global bound simply disables new experiments.
                    return None
                progress = _ExplorationProgress()
                self._exploration[key] = progress
            if progress.blocked:
                return None

            pending_width = progress.pending_width
            if pending_width is not None:
                route_name = f"{managed_execution}:{pending_width}"
                current_count = self._relevant_route_observation_count(
                    state, normalized, route_name
                )
                if current_count <= progress.pending_observation_count:
                    # The trial may still be running, or it may have failed,
                    # been cancelled, or hit cache.  In every case, do not
                    # issue another trial.  A later adopted sample can still
                    # unlock evaluation without creating repeated slowdown.
                    return None
                previous_wall = route_walls.get(pending_width + 1)
                pending_wall = route_walls.get(pending_width)
                progress.pending_width = None
                if previous_wall is None:
                    progress.blocked = True
                    return None
                if pending_wall is not None and not _meaningfully_faster(
                    pending_wall, previous_wall
                ):
                    progress.blocked = True
                    return None
                if (
                    pending_wall is None
                    and not self._route_is_safely_under_sampled(
                        state, normalized, route_name
                    )
                ):
                    progress.blocked = True
                    return None

            previous_wall = route_walls.get(static_workers)
            if previous_wall is None:
                progress.blocked = True
                return None
            for workers in range(static_workers - 1, 1, -1):
                wall = route_walls.get(workers)
                if wall is not None:
                    if not _meaningfully_faster(wall, previous_wall):
                        progress.blocked = True
                        return None
                    previous_wall = wall
                    continue
                route_name = f"{managed_execution}:{workers}"
                if not self._route_is_safely_under_sampled(
                    state, normalized, route_name
                ):
                    progress.blocked = True
                    return None
                attempts = progress.attempts_by_width.get(workers, 0)
                if attempts >= _MIN_MODEL_SAMPLES:
                    progress.blocked = True
                    return None
                progress.attempts_by_width[workers] = attempts + 1
                progress.pending_width = workers
                progress.pending_observation_count = (
                    self._relevant_route_observation_count(
                        state, normalized, route_name
                    )
                )
                return AdaptiveParallelismRecommendation(
                    workers,
                    False,
                    "controlled_exploration",
                )
        return None

    def recommend(
        self,
        decision: Any,
        workloads: Sequence[AdaptiveWorkload],
        *,
        managed_execution: str = "managed_cold",
    ) -> AdaptiveParallelismRecommendation:
        """Recommend a benefit limit while treating static safety as final.

        Cold and warm worker timings are never mixed.  A caller planning a run
        with a cold first batch and warm later batches should keep the default
        cold route: that intentionally underclaims the warm-session benefit.
        ``managed_warm`` is suitable only when checkout is already known to be
        warm before the decision.
        """

        unchanged = AdaptiveParallelismRecommendation(
            None, False, "insufficient_evidence"
        )
        if os.getpid() != self._owner_pid:
            return unchanged
        if managed_execution not in _MANAGED_EXECUTIONS:
            return unchanged
        try:
            static_reason = decision.reason
            static_workers = decision.worker_count
            part_count = decision.part_count
            hard_limit = min(
                _MAX_WORKERS,
                part_count,
                decision.cpu_worker_limit,
                decision.memory_worker_limit,
                decision.scratch_worker_limit,
            )
        except (AttributeError, TypeError, ValueError, OverflowError):
            return unchanged
        if (
            static_reason not in {"automatic", "short_workload"}
            or hard_limit < 2
            or len(workloads) != part_count
        ):
            return AdaptiveParallelismRecommendation(
                None, False, "static_safety_gate"
            )
        normalized: list[tuple[str, int]] = []
        for workload in workloads:
            if not isinstance(workload, AdaptiveWorkload):
                return unchanged
            backend = _backend_id(workload.backend_key)
            work = _work_frames(workload.work_frames)
            if backend is None or work is None:
                return unchanged
            normalized.append((backend, work))

        state = self._best_state()
        serial_lower: list[float] = []
        serial_bounds: dict[tuple[str, int], tuple[float, float] | None] = {}
        for backend, work in normalized:
            entry = state["backends"].get(backend)
            if entry is None:
                return unchanged
            cache_key = (backend, work)
            if cache_key not in serial_bounds:
                serial_bounds[cache_key] = _timing_bounds(
                    entry["routes"].get("serial", ()), work
                )
            bounds = serial_bounds[cache_key]
            if bounds is None:
                return unchanged
            serial_lower.append(bounds[0])
        conservative_serial = math.fsum(serial_lower)
        if conservative_serial <= _MIN_ABSOLUTE_SAVING_SECONDS:
            return unchanged

        candidates: list[tuple[float, int]] = []
        route_walls: dict[int, float | None] = {}
        for workers in range(2, hard_limit + 1):
            route_name = f"{managed_execution}:{workers}"
            route_bounds: dict[
                tuple[str, int], tuple[float, float] | None
            ] = {}
            upper: list[float] = []
            for backend, work in normalized:
                entry = state["backends"].get(backend)
                assert entry is not None
                cache_key = (backend, work)
                if cache_key not in route_bounds:
                    route_bounds[cache_key] = _timing_bounds(
                        entry["routes"].get(route_name, ()), work
                    )
                bounds = route_bounds[cache_key]
                if bounds is None:
                    upper = []
                    break
                upper.append(bounds[1])
            if len(upper) != len(normalized):
                route_walls[workers] = None
                continue
            wall = _parallel_wall_time(upper, workers)
            route_walls[workers] = wall
            if _meaningfully_faster(wall, conservative_serial):
                candidates.append((wall, workers))

        static_wall = (
            route_walls.get(static_workers)
            if type(static_workers) is int
            else None
        )
        static_is_beneficial = (
            static_wall is not None
            and _meaningfully_faster(static_wall, conservative_serial)
        )
        if (
            static_reason == "automatic"
            and type(static_workers) is int
            and 3 <= static_workers <= hard_limit
            and static_is_beneficial
        ):
            exploration = self._recommend_downward_exploration(
                state=state,
                normalized=normalized,
                managed_execution=managed_execution,
                static_workers=static_workers,
                route_walls=route_walls,
            )
            if exploration is not None:
                return exploration

        if candidates:
            _wall, selected = min(candidates, key=lambda value: (value[0], -value[1]))
            return AdaptiveParallelismRecommendation(
                selected,
                static_reason == "short_workload",
                "learned_benefit",
            )

        # Downgrade only when the static route itself has a complete, stable
        # model *at this workload* and that evidence fails the conservative
        # benefit test.  A model fitted only to much smaller/larger jobs must
        # not silently turn an out-of-range project into a serial render.
        if (
            static_reason == "automatic"
            and type(static_workers) is int
            and 2 <= static_workers <= hard_limit
            and static_wall is not None
        ):
            if static_is_beneficial:
                return AdaptiveParallelismRecommendation(
                    static_workers,
                    False,
                    "learned_benefit",
                )
            return AdaptiveParallelismRecommendation(
                1, False, "learned_serial_benefit"
            )
        return unchanged


__all__ = [
    "AdaptiveParallelismAdvisor",
    "AdaptiveParallelismError",
    "AdaptiveParallelismRecommendation",
    "AdaptiveCompletedTimingToken",
    "AdaptiveTimingToken",
    "AdaptiveWorkload",
    "default_adaptive_state_directory",
    "make_adaptive_backend_key",
]
