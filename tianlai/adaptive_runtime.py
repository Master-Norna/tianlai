"""Render-scoped orchestration for private adaptive parallelism.

The advisor owns the bounded timing model and its optional persistence.  This
module owns the more important render transaction: a timing is not taught to
the model merely because a backend process returned.  Its source must first
be consumed and verified, and every accepted timing is committed only after
the complete stem phase succeeds.

This is deliberately an internal, zero-configuration API.  Ordinary learning
or state failures disable the optional observation without affecting audio.
``MemoryError`` remains authoritative because continuing after an allocation
failure is not a safe fail-soft policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import threading
from typing import Any, Sequence

from .adaptive_parallelism import (
    AdaptiveParallelismAdvisor,
    AdaptiveWorkload,
)


_MAX_SESSION_OBSERVATIONS = 256
_MAX_MANAGED_WIDTH = 4
_DEFAULT_ADVISOR = object()
_UNINITIALIZED_ADVISOR = object()


_process_advisor_lock = threading.Lock()
_process_advisor_pid = os.getpid()
_process_advisor: object = _UNINITIALIZED_ADVISOR


def _reset_process_advisor_after_fork() -> None:
    """Drop inherited advisor state and, crucially, inherited thread locks."""

    global _process_advisor_lock
    global _process_advisor_pid
    global _process_advisor

    # Assign the fresh lock first.  A lock inherited while held by a vanished
    # thread must never be acquired in the child.
    _process_advisor_lock = threading.Lock()
    _process_advisor_pid = os.getpid()
    _process_advisor = _UNINITIALIZED_ADVISOR


def _register_at_fork() -> bool:
    register = getattr(os, "register_at_fork", None)
    if not callable(register):
        return False
    try:
        register(after_in_child=_reset_process_advisor_after_fork)
    except MemoryError:
        raise
    except Exception:
        # Platforms without a usable fork hook still have the PID check in
        # ``_get_process_advisor``.  Learning remains optional.
        return False
    return True


_AT_FORK_REGISTERED = _register_at_fork()


def _get_process_advisor() -> AdaptiveParallelismAdvisor | None:
    """Return one lazily constructed advisor for the current process.

    A failed ordinary construction is cached as disabled for this PID.  This
    prevents every stem from repeatedly touching an unavailable state
    directory.  A child process always gets a fresh lock and instance.
    """

    global _process_advisor_lock
    global _process_advisor_pid
    global _process_advisor

    current_pid = os.getpid()
    if current_pid != _process_advisor_pid:
        # ``register_at_fork`` is the normal route.  This also covers unusual
        # embedders that manufacture a child without invoking the hook.  Only
        # the forking thread survives, so replacing these references is safe.
        _reset_process_advisor_after_fork()

    if _process_advisor is not _UNINITIALIZED_ADVISOR:
        value = _process_advisor
        return value if isinstance(value, AdaptiveParallelismAdvisor) else None

    with _process_advisor_lock:
        if _process_advisor is _UNINITIALIZED_ADVISOR:
            try:
                _process_advisor = AdaptiveParallelismAdvisor()
            except MemoryError:
                raise
            except Exception:
                _process_advisor = None
        value = _process_advisor
    return value if isinstance(value, AdaptiveParallelismAdvisor) else None


@dataclass(frozen=True, slots=True)
class AdaptiveSessionResult:
    """Small internal diagnostic returned when a render session is closed."""

    committed_observations: int
    discarded_observations: int
    flush_attempted: bool
    flush_succeeded: bool
    disabled: bool


@dataclass(frozen=True, slots=True)
class _LiveObservation:
    session_nonce: object
    identifier: int
    kind: str


@dataclass(slots=True)
class _FrozenState:
    advisor_token: object
    resolved: bool | None = None


class AdaptiveFrozenObservation:
    """One-shot source-verification gate for a frozen backend timing.

    Pass ``observation.resolve`` into the source completion callback chain.
    ``True`` only marks the timing eligible; the surrounding render session
    still has to complete before the advisor sees it.
    """

    __slots__ = ("_identifier", "_session", "_session_nonce")

    def __init__(
        self,
        session: AdaptiveRenderSession,
        session_nonce: object,
        identifier: int,
    ) -> None:
        self._session = session
        self._session_nonce = session_nonce
        self._identifier = identifier

    def resolve(self, succeeded: bool) -> bool:
        """Resolve once from the authoritative source completion callback."""

        return self._session._resolve_frozen(
            self._session_nonce,
            self._identifier,
            succeeded,
        )


class AdaptiveRenderSession:
    """Transactionally collect timings for one render's stem phase.

    Normal callers omit ``advisor`` and receive the lazy process advisor.
    Tests and private embedders may inject an advisor-like object without
    touching the real per-user state directory.
    """

    __slots__ = (
        "_advisor",
        "_closed",
        "_discarded_before_close",
        "_frozen",
        "_live",
        "_lock",
        "_next_identifier",
        "_owner_pid",
        "_result",
        "_session_nonce",
    )

    def __init__(self, *, advisor: object = _DEFAULT_ADVISOR) -> None:
        if advisor is _DEFAULT_ADVISOR:
            advisor = _get_process_advisor()
        self._advisor = advisor
        self._owner_pid = os.getpid()
        self._session_nonce = object()
        self._lock = threading.Lock()
        self._next_identifier = 1
        self._live: dict[int, tuple[str, object]] = {}
        self._frozen: dict[int, _FrozenState] = {}
        self._discarded_before_close = 0
        self._closed = False
        self._result: AdaptiveSessionResult | None = None

    @property
    def enabled(self) -> bool:
        """Whether this process owns a usable advisor for the session."""

        return os.getpid() == self._owner_pid and self._advisor is not None

    @property
    def observation_count(self) -> int:
        """Return the bounded number of live and frozen observations."""

        if os.getpid() != self._owner_pid:
            return 0
        with self._lock:
            return len(self._live) + len(self._frozen)

    def __enter__(self) -> AdaptiveRenderSession:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> bool:
        del exception, traceback
        if exception_type is None:
            self.complete()
        else:
            self.cancel()
        return False

    def recommend(
        self,
        decision: Any,
        workloads: Sequence[AdaptiveWorkload],
        *,
        managed_execution: str = "managed_cold",
    ) -> object | None:
        """Pass through a recommendation, or return ``None`` fail-soft."""

        if os.getpid() != self._owner_pid:
            return None
        with self._lock:
            advisor = self._advisor
            if self._closed or advisor is None:
                return None
            try:
                return advisor.recommend(
                    decision,
                    workloads,
                    managed_execution=managed_execution,
                )
            except MemoryError:
                raise
            except Exception:
                return None

    def begin_serial(
        self,
        *,
        backend_key: str,
        work_frames: int,
    ) -> _LiveObservation | None:
        """Begin immediately before an uncached in-process stem render."""

        return self._begin(
            kind="serial",
            backend_key=backend_key,
            work_frames=work_frames,
        )

    def begin_managed(
        self,
        *,
        backend_key: str,
        work_frames: int,
    ) -> _LiveObservation | None:
        """Begin before checkout/spawn, while cold versus warm is unknown."""

        return self._begin(
            kind="managed",
            backend_key=backend_key,
            work_frames=work_frames,
        )

    def _begin(
        self,
        *,
        kind: str,
        backend_key: str,
        work_frames: int,
    ) -> _LiveObservation | None:
        if os.getpid() != self._owner_pid:
            return None
        with self._lock:
            advisor = self._advisor
            if (
                self._closed
                or advisor is None
                or len(self._live) + len(self._frozen)
                >= _MAX_SESSION_OBSERVATIONS
            ):
                return None
            try:
                if kind == "serial":
                    advisor_token = advisor.begin_task(
                        backend_key=backend_key,
                        work_frames=work_frames,
                        execution="serial",
                        concurrent_workers=1,
                        cache_hit=False,
                    )
                else:
                    advisor_token = advisor.begin_managed_task(
                        backend_key=backend_key,
                        work_frames=work_frames,
                        cache_hit=False,
                    )
            except MemoryError:
                raise
            except Exception:
                return None
            if advisor_token is None:
                return None
            identifier = self._next_identifier
            self._next_identifier += 1
            self._live[identifier] = (kind, advisor_token)
            return _LiveObservation(
                self._session_nonce,
                identifier,
                kind,
            )

    def freeze_serial(
        self,
        observation: _LiveObservation | None,
    ) -> AdaptiveFrozenObservation | None:
        """Freeze immediately after the in-process backend returns."""

        return self._freeze(observation, expected_kind="serial")

    def freeze_managed(
        self,
        observation: _LiveObservation | None,
        *,
        warm_used: bool,
        concurrent_workers: int,
    ) -> AdaptiveFrozenObservation | None:
        """Freeze at collect using the actual route and active batch width.

        A managed job collected alone is intentionally discarded.  There is
        no parallel route to compare when the actual width is one.  Ordered
        collection can conservatively overestimate later positions, but never
        mixes downstream source consumption into the timing.
        """

        valid_width = (
            type(concurrent_workers) is int
            and 2 <= concurrent_workers <= _MAX_MANAGED_WIDTH
        )
        if type(warm_used) is not bool or not valid_width:
            self._discard_live(observation, expected_kind="managed")
            return None
        execution = "managed_warm" if warm_used else "managed_cold"
        return self._freeze(
            observation,
            expected_kind="managed",
            execution=execution,
            concurrent_workers=concurrent_workers,
        )

    def discard_serial(self, observation: _LiveObservation | None) -> bool:
        """Discard one unfinished in-process observation fail-soft."""

        return self._discard_live(observation, expected_kind="serial")

    def discard_managed(self, observation: _LiveObservation | None) -> bool:
        """Discard one unfinished managed-worker observation fail-soft."""

        return self._discard_live(observation, expected_kind="managed")

    def _freeze(
        self,
        observation: _LiveObservation | None,
        *,
        expected_kind: str,
        execution: str | None = None,
        concurrent_workers: int | None = None,
    ) -> AdaptiveFrozenObservation | None:
        if os.getpid() != self._owner_pid:
            return None
        with self._lock:
            if (
                self._closed
                or not isinstance(observation, _LiveObservation)
                or observation.session_nonce is not self._session_nonce
            ):
                return None
            current = self._live.pop(observation.identifier, None)
            if current is None:
                return None
            kind, advisor_token = current
            advisor = self._advisor
            if kind != expected_kind or advisor is None:
                self._discard_token_locked(advisor_token)
                self._discarded_before_close += 1
                return None
            try:
                if expected_kind == "serial":
                    completed = advisor.freeze_task(advisor_token)
                else:
                    completed = advisor.freeze_task(
                        advisor_token,
                        execution=execution,
                        concurrent_workers=concurrent_workers,
                    )
            except MemoryError:
                self._discard_token_suppressing_errors(
                    advisor, advisor_token
                )
                self._discarded_before_close += 1
                raise
            except Exception:
                self._discard_token_locked(advisor_token)
                self._discarded_before_close += 1
                return None
            if completed is None:
                self._discard_token_locked(advisor_token)
                self._discarded_before_close += 1
                return None
            self._frozen[observation.identifier] = _FrozenState(completed)
            return AdaptiveFrozenObservation(
                self,
                self._session_nonce,
                observation.identifier,
            )

    def _discard_live(
        self,
        observation: _LiveObservation | None,
        *,
        expected_kind: str,
    ) -> bool:
        if os.getpid() != self._owner_pid:
            return False
        with self._lock:
            if (
                self._closed
                or not isinstance(observation, _LiveObservation)
                or observation.session_nonce is not self._session_nonce
            ):
                return False
            current = self._live.pop(observation.identifier, None)
            if current is None:
                return False
            kind, advisor_token = current
            if kind != expected_kind:
                # It still belongs to this session; consume it fail-closed.
                pass
            self._discard_token_locked(advisor_token)
            self._discarded_before_close += 1
            return True

    def _resolve_frozen(
        self,
        session_nonce: object,
        identifier: int,
        succeeded: bool,
    ) -> bool:
        if (
            os.getpid() != self._owner_pid
            or session_nonce is not self._session_nonce
        ):
            return False
        with self._lock:
            if self._closed:
                return False
            state = self._frozen.get(identifier)
            if state is None or state.resolved is not None:
                return False
            if succeeded is True:
                state.resolved = True
                return True
            self._frozen.pop(identifier, None)
            self._discard_token_locked(state.advisor_token)
            self._discarded_before_close += 1
            return True

    def complete(self) -> AdaptiveSessionResult:
        """Commit verified observations and flush exactly once."""

        return self._finalise(succeeded=True)

    def cancel(self) -> AdaptiveSessionResult:
        """Discard every observation and never flush this session."""

        return self._finalise(succeeded=False)

    def _finalise(self, *, succeeded: bool) -> AdaptiveSessionResult:
        if os.getpid() != self._owner_pid:
            return AdaptiveSessionResult(0, 0, False, False, True)
        with self._lock:
            if self._result is not None:
                return self._result

            self._closed = True
            advisor = self._advisor
            live_tokens = [token for _kind, token in self._live.values()]
            frozen_states = list(self._frozen.values())
            self._live.clear()
            self._frozen.clear()

            committed = 0
            discarded = self._discarded_before_close
            flush_attempted = False
            flush_succeeded = False
            remaining = live_tokens + [
                state.advisor_token for state in frozen_states
            ]
            try:
                if advisor is not None:
                    for token in live_tokens:
                        self._discard_token_locked(token)
                        discarded += 1
                        remaining.remove(token)

                    for state in frozen_states:
                        token = state.advisor_token
                        if succeeded and state.resolved is True:
                            try:
                                accepted = advisor.commit_task(
                                    token,
                                    succeeded=True,
                                    cancelled=False,
                                    cache_hit=False,
                                )
                            except MemoryError:
                                raise
                            except Exception:
                                self._discard_token_locked(token)
                                accepted = False
                            if accepted is True:
                                committed += 1
                            else:
                                discarded += 1
                        else:
                            self._discard_token_locked(token)
                            discarded += 1
                        remaining.remove(token)

                    if succeeded:
                        flush_attempted = True
                        try:
                            flush_succeeded = advisor.flush() is True
                        except MemoryError:
                            raise
                        except Exception:
                            flush_succeeded = False
                else:
                    discarded += len(remaining)
                    remaining.clear()
            except MemoryError:
                if advisor is not None:
                    for token in remaining:
                        self._discard_token_suppressing_errors(advisor, token)
                self._result = AdaptiveSessionResult(
                    committed,
                    discarded + len(remaining),
                    flush_attempted,
                    flush_succeeded,
                    advisor is None,
                )
                raise

            self._result = AdaptiveSessionResult(
                committed,
                discarded,
                flush_attempted,
                flush_succeeded,
                advisor is None,
            )
            return self._result

    def _discard_token_locked(self, token: object) -> None:
        advisor = self._advisor
        if advisor is None:
            return
        try:
            advisor.discard_task(token)
        except MemoryError:
            raise
        except Exception:
            pass

    @staticmethod
    def _discard_token_suppressing_errors(
        advisor: object,
        token: object,
    ) -> None:
        # Used only while preserving an already active MemoryError.
        try:
            advisor.discard_task(token)
        except BaseException:
            pass


__all__ = [
    "AdaptiveFrozenObservation",
    "AdaptiveRenderSession",
    "AdaptiveSessionResult",
]
