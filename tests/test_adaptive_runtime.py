from __future__ import annotations

from dataclasses import dataclass

import pytest

import tianlai.adaptive_runtime as runtime_module
from tianlai.adaptive_runtime import AdaptiveRenderSession


@dataclass(frozen=True)
class _Token:
    number: int
    stage: str


class _FakeAdvisor:
    def __init__(self) -> None:
        self.next_token = 1
        self.begun: list[tuple[str, int, str | None, int | None]] = []
        self.frozen: list[tuple[_Token, str | None, int | None]] = []
        self.committed: list[_Token] = []
        self.discarded: list[_Token] = []
        self.flush_calls = 0
        self.recommendation = object()
        self.raise_from: dict[str, BaseException] = {}

    def _raise(self, name: str) -> None:
        exception = self.raise_from.get(name)
        if exception is not None:
            raise exception

    def _token(self, stage: str) -> _Token:
        token = _Token(self.next_token, stage)
        self.next_token += 1
        return token

    def recommend(self, decision, workloads, *, managed_execution):
        self._raise("recommend")
        return self.recommendation

    def begin_task(
        self,
        *,
        backend_key,
        work_frames,
        execution,
        concurrent_workers,
        cache_hit,
    ):
        assert cache_hit is False
        self._raise("begin")
        self.begun.append(
            (backend_key, work_frames, execution, concurrent_workers)
        )
        return self._token("live")

    def begin_managed_task(
        self,
        *,
        backend_key,
        work_frames,
        cache_hit,
    ):
        assert cache_hit is False
        self._raise("begin_managed")
        self.begun.append((backend_key, work_frames, None, None))
        return self._token("live")

    def freeze_task(
        self,
        token,
        *,
        execution=None,
        concurrent_workers=None,
    ):
        self._raise("freeze")
        self.frozen.append((token, execution, concurrent_workers))
        return _Token(token.number, "frozen")

    def commit_task(
        self,
        token,
        *,
        succeeded,
        cancelled,
        cache_hit,
    ):
        assert succeeded is True
        assert cancelled is False
        assert cache_hit is False
        self._raise("commit")
        self.committed.append(token)
        return True

    def discard_task(self, token) -> None:
        self._raise("discard")
        self.discarded.append(token)

    def flush(self) -> bool:
        self._raise("flush")
        self.flush_calls += 1
        return True


def _serial(session: AdaptiveRenderSession):
    live = session.begin_serial(backend_key="backend", work_frames=12_000)
    assert live is not None
    frozen = session.freeze_serial(live)
    assert frozen is not None
    return frozen


def test_success_commits_after_source_and_flushes_exactly_once() -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    frozen = _serial(session)

    assert advisor.committed == []
    assert frozen.resolve(True) is True
    assert advisor.committed == []

    result = session.complete()
    assert result.committed_observations == 1
    assert result.discarded_observations == 0
    assert result.flush_attempted is True
    assert result.flush_succeeded is True
    assert advisor.flush_calls == 1
    assert len(advisor.committed) == 1

    assert session.complete() is result
    assert session.cancel() is result
    assert advisor.flush_calls == 1
    assert frozen.resolve(True) is False


def test_cancel_discards_everything_without_commit_or_flush() -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    serial = _serial(session)
    assert serial.resolve(True) is True
    managed_live = session.begin_managed(
        backend_key="managed", work_frames=24_000
    )
    assert managed_live is not None
    managed = session.freeze_managed(
        managed_live,
        warm_used=False,
        concurrent_workers=2,
    )
    assert managed is not None
    assert managed.resolve(True) is True

    result = session.cancel()
    assert result.committed_observations == 0
    assert result.discarded_observations == 2
    assert advisor.committed == []
    assert len(advisor.discarded) == 2
    assert advisor.flush_calls == 0


def test_source_false_discards_before_successful_phase_close() -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    frozen = _serial(session)

    assert frozen.resolve(False) is True
    assert frozen.resolve(False) is False
    result = session.complete()

    assert len(advisor.discarded) == 1
    assert advisor.committed == []
    assert result.discarded_observations == 1
    assert advisor.flush_calls == 1


def test_managed_freeze_uses_actual_route_and_batch_width() -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)

    cold_live = session.begin_managed(
        backend_key="cold", work_frames=10_000
    )
    warm_live = session.begin_managed(
        backend_key="warm", work_frames=20_000
    )
    lone_live = session.begin_managed(
        backend_key="lone", work_frames=30_000
    )
    assert cold_live is not None
    assert warm_live is not None
    assert lone_live is not None

    cold = session.freeze_managed(
        cold_live, warm_used=False, concurrent_workers=3
    )
    warm = session.freeze_managed(
        warm_live, warm_used=True, concurrent_workers=2
    )
    lone = session.freeze_managed(
        lone_live, warm_used=True, concurrent_workers=1
    )

    assert cold is not None
    assert warm is not None
    assert lone is None
    assert advisor.frozen == [
        (_Token(1, "live"), "managed_cold", 3),
        (_Token(2, "live"), "managed_warm", 2),
    ]
    assert advisor.discarded == [_Token(3, "live")]


def test_recommend_is_a_fail_soft_passthrough() -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    assert session.recommend("decision", (), managed_execution="managed_warm") is (
        advisor.recommendation
    )

    advisor.raise_from["recommend"] = OSError("state unavailable")
    assert session.recommend("decision", ()) is None

    advisor.raise_from["recommend"] = MemoryError()
    with pytest.raises(MemoryError):
        session.recommend("decision", ())


@pytest.mark.parametrize("operation", ["begin", "freeze", "flush"])
def test_ordinary_advisor_failures_never_break_render(operation: str) -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    advisor.raise_from[operation] = OSError("optional learning unavailable")

    live = session.begin_serial(backend_key="backend", work_frames=10_000)
    if operation == "begin":
        assert live is None
    else:
        assert live is not None
        frozen = session.freeze_serial(live)
        if operation == "freeze":
            assert frozen is None
        else:
            assert frozen is not None
            assert frozen.resolve(True) is True

    result = session.complete()
    assert result.flush_attempted is True
    if operation == "flush":
        assert result.flush_succeeded is False


@pytest.mark.parametrize("operation", ["begin", "freeze", "flush"])
def test_memory_errors_remain_authoritative(operation: str) -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    advisor.raise_from[operation] = MemoryError()

    if operation == "begin":
        with pytest.raises(MemoryError):
            session.begin_serial(backend_key="backend", work_frames=10_000)
        return

    live = session.begin_serial(backend_key="backend", work_frames=10_000)
    assert live is not None
    if operation == "freeze":
        with pytest.raises(MemoryError):
            session.freeze_serial(live)
        return

    frozen = session.freeze_serial(live)
    assert frozen is not None
    assert frozen.resolve(True) is True
    with pytest.raises(MemoryError):
        session.complete()


def test_session_observations_are_bounded() -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    accepted = [
        session.begin_serial(backend_key="backend", work_frames=index + 1)
        for index in range(runtime_module._MAX_SESSION_OBSERVATIONS + 20)
    ]
    assert sum(value is not None for value in accepted) == (
        runtime_module._MAX_SESSION_OBSERVATIONS
    )
    assert session.observation_count == runtime_module._MAX_SESSION_OBSERVATIONS


def test_lazy_global_advisor_is_pid_scoped_and_fork_reset_rebuilds_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_FakeAdvisor] = []

    class _ConstructedAdvisor(_FakeAdvisor):
        def __init__(self) -> None:
            super().__init__()
            created.append(self)

    monkeypatch.setattr(
        runtime_module, "AdaptiveParallelismAdvisor", _ConstructedAdvisor
    )
    runtime_module._reset_process_advisor_after_fork()
    old_lock = runtime_module._process_advisor_lock

    first = AdaptiveRenderSession()
    second = AdaptiveRenderSession()
    assert first.enabled is True
    assert second.enabled is True
    assert len(created) == 1

    runtime_module._reset_process_advisor_after_fork()
    assert runtime_module._process_advisor_lock is not old_lock
    third = AdaptiveRenderSession()
    assert third.enabled is True
    assert len(created) == 2


def test_lazy_creation_failure_is_cached_as_disabled_without_real_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    class _BrokenAdvisor:
        def __init__(self) -> None:
            nonlocal attempts
            attempts += 1
            raise OSError("no state directory")

    monkeypatch.setattr(
        runtime_module, "AdaptiveParallelismAdvisor", _BrokenAdvisor
    )
    runtime_module._reset_process_advisor_after_fork()
    assert AdaptiveRenderSession().enabled is False
    assert AdaptiveRenderSession().enabled is False
    assert attempts == 1


def test_lazy_creation_memory_error_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BrokenAdvisor:
        def __init__(self) -> None:
            raise MemoryError()

    monkeypatch.setattr(
        runtime_module, "AdaptiveParallelismAdvisor", _BrokenAdvisor
    )
    runtime_module._reset_process_advisor_after_fork()
    with pytest.raises(MemoryError):
        AdaptiveRenderSession()


def test_inherited_session_is_disabled_without_touching_its_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    advisor = _FakeAdvisor()
    session = AdaptiveRenderSession(advisor=advisor)
    owner_pid = runtime_module.os.getpid()
    monkeypatch.setattr(runtime_module.os, "getpid", lambda: owner_pid + 1)

    assert session.enabled is False
    assert session.observation_count == 0
    assert session.begin_serial(backend_key="backend", work_frames=1) is None
    result = session.cancel()
    assert result.disabled is True
    assert advisor.begun == []
