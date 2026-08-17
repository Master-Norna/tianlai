from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tianlai import ensemble as ensemble_module
from tianlai import render_parallelism as parallelism_module
from tianlai.adaptive_runtime import AdaptiveRenderSession
from tianlai.resource_limits import ProjectLimits


def _policy_plan(part_count: int = 4) -> SimpleNamespace:
    duration = 60.0
    sample_rate = 48_000
    return SimpleNamespace(
        duration_seconds=duration,
        sample_rate=sample_rate,
        parts=tuple(
            SimpleNamespace(
                performance={
                    "sample_rate": sample_rate,
                    "duration_seconds": duration,
                    "events": [
                        {
                            "type": "note_on",
                            "time": 0.0,
                            "note_id": index,
                            "velocity": 0.8,
                        },
                        {
                            "type": "note_off",
                            "time": duration - 1.0,
                            "note_id": index,
                        },
                    ],
                }
            )
            for index in range(part_count)
        ),
    )


def _resources(part_count: int, *, safe: bool = True) -> SimpleNamespace:
    reserve = 256 * 1024 * 1024
    return SimpleNamespace(
        workers_safe=safe,
        managed_worker_safe_by_part=(True,) * part_count,
        worker_reserve_bytes_by_part=(reserve,) * part_count,
        sample_backed_by_part=(False,) * part_count,
    )


class _RecommendationSession:
    def __init__(self, worker_limit: int, allow_short: bool = False) -> None:
        self.recommendation = SimpleNamespace(
            worker_limit=worker_limit,
            allow_short_workload=allow_short,
        )
        self.decisions: list[object] = []

    def recommend(self, decision, workloads, *, managed_execution):
        assert managed_execution == "managed_cold"
        assert len(workloads) == len(decision_to_parts(decision))
        self.decisions.append(decision)
        return self.recommendation


def decision_to_parts(decision: object) -> range:
    return range(int(getattr(decision, "part_count")))


def test_recommendation_can_reduce_workers_but_cannot_bypass_hard_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = _policy_plan()
    workloads = tuple(object() for _part in plan.parts)
    limits = ProjectLimits(max_audio_memory_bytes=4 * 1024**3)
    monkeypatch.setattr(
        ensemble_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=64 * 1024**3),
    )
    monkeypatch.setattr(
        ensemble_module, "_parallel_runtime_is_pristine", lambda: True
    )
    monkeypatch.setattr(
        ensemble_module, "current_source_tree_matches", lambda: True
    )
    monkeypatch.setattr(
        ensemble_module, "automatic_worker_capacity", lambda: 4
    )
    monkeypatch.setattr(
        parallelism_module, "_runtime_cpu_count", lambda: 16
    )
    monkeypatch.setattr(
        parallelism_module.platform, "system", lambda: "Linux"
    )
    monkeypatch.setattr(
        ProjectLimits,
        "from_environment",
        classmethod(lambda cls: limits),
    )

    baseline = ensemble_module._automatic_stem_worker_count(
        plan,
        scratch_directory=tmp_path,
        hall_tail_seconds=0.0,
        _resources=_resources(len(plan.parts)),
    )
    assert baseline == 4

    downshift = _RecommendationSession(2)
    selected = ensemble_module._automatic_stem_worker_count(
        plan,
        scratch_directory=tmp_path,
        hall_tail_seconds=0.0,
        _resources=_resources(len(plan.parts)),
        adaptive_session=downshift,
        adaptive_workloads=workloads,
    )
    assert selected == 2
    assert downshift.decisions[0].worker_count == 4

    malicious_admission = _RecommendationSession(4, allow_short=True)
    hard_gated = ensemble_module._automatic_stem_worker_count(
        plan,
        scratch_directory=tmp_path,
        hall_tail_seconds=0.0,
        _resources=_resources(len(plan.parts), safe=False),
        adaptive_session=malicious_admission,
        adaptive_workloads=workloads,
    )
    assert hard_gated == 1
    assert malicious_admission.decisions[0].reason == "workers_ineligible"


class _FrozenCall:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    def resolve(self, succeeded: bool) -> None:
        self.events.append(("resolve", succeeded))


class _SerialSession:
    def __init__(self) -> None:
        self.events: list[object] = []

    def begin_serial(self, *, backend_key: str, work_frames: int) -> object:
        self.events.append(("begin", backend_key, work_frames))
        return "serial-observation"

    def freeze_serial(self, observation: object) -> _FrozenCall:
        self.events.append(("freeze", observation))
        return _FrozenCall(self.events)


def test_cache_hit_is_not_timed_but_real_serial_render_is() -> None:
    session = _SerialSession()
    summary = ensemble_module._new_stem_cache_summary(
        refresh_requested=False,
        total=1,
    )
    identity = SimpleNamespace(
        key="cache-key",
        manifest_sha256="a" * 64,
        frame_count=16,
    )
    lookup = SimpleNamespace(
        hit=True,
        status="hit",
        record=SimpleNamespace(metadata={"peak_voices": 3}),
        source="verified-cache-source",
        audio=None,
    )
    with (
        patch(
            "tianlai.ensemble.current_source_tree_matches",
            return_value=True,
        ),
        patch(
            "tianlai.ensemble._raw_stem_cache_identity",
            return_value=identity,
        ),
        patch(
            "tianlai.ensemble._load_stem_cache_for_render",
            return_value=lookup,
        ),
        patch(
            "tianlai.ensemble._cache_lookup_matches",
            return_value=True,
        ),
    ):
        cached = ensemble_module._render_part_cached(
            object(),
            48_000,
            cache=object(),
            snapshot_directory=Path.cwd(),
            stream_cache_hits=True,
            refresh=False,
            runtime_fingerprints={},
            summary=summary,
            adaptive_session=session,
            adaptive_backend_key="backend",
            adaptive_work_frames=48_000,
        )
    assert cached == ("verified-cache-source", 3, "a" * 64)
    assert session.events == []

    rendered_value = ("fresh-audio", 7, "b" * 64)
    with patch(
        "tianlai.ensemble._render_part", return_value=rendered_value
    ):
        rendered = ensemble_module._render_part_adaptively(
            object(),
            48_000,
            adaptive_session=session,
            adaptive_backend_key="backend",
            adaptive_work_frames=96_000,
        )
    assert rendered == rendered_value
    assert session.events == [
        ("begin", "backend", 96_000),
        ("freeze", "serial-observation"),
        ("resolve", True),
    ]


class _PhaseSession:
    def __init__(self) -> None:
        self.complete_calls = 0
        self.cancel_calls = 0

    def complete(self) -> None:
        self.complete_calls += 1

    def cancel(self) -> None:
        self.cancel_calls += 1


def test_raw_phase_completes_once_on_success_and_cancels_on_failure(
    tmp_path: Path,
) -> None:
    successful = _PhaseSession()

    def successful_body(*args: object, **kwargs: object):
        del args, kwargs
        yield "raw-stem"

    with (
        patch(
            "tianlai.ensemble.AdaptiveRenderSession",
            return_value=successful,
        ),
        patch(
            "tianlai.ensemble._iter_raw_stems_in_plan_order_body",
            side_effect=successful_body,
        ),
    ):
        values = tuple(
            ensemble_module._iter_raw_stems_in_plan_order(
                object(),
                scratch_directory=tmp_path,
                hall_tail_seconds=0.0,
                cache=None,
                stream_cache_hits=True,
                refresh=False,
                runtime_fingerprints={},
                summary=None,
            )
        )
    assert values == ("raw-stem",)
    assert successful.complete_calls == 1
    assert successful.cancel_calls == 0

    failed = _PhaseSession()

    def failing_body(*args: object, **kwargs: object):
        del args, kwargs
        raise RuntimeError("raw stem failed")
        yield  # pragma: no cover

    with (
        patch(
            "tianlai.ensemble.AdaptiveRenderSession",
            return_value=failed,
        ),
        patch(
            "tianlai.ensemble._iter_raw_stems_in_plan_order_body",
            side_effect=failing_body,
        ),
        pytest.raises(RuntimeError, match="raw stem failed"),
    ):
        tuple(
            ensemble_module._iter_raw_stems_in_plan_order(
                object(),
                scratch_directory=tmp_path,
                hall_tail_seconds=0.0,
                cache=None,
                stream_cache_hits=True,
                refresh=False,
                runtime_fingerprints={},
                summary=None,
            )
        )
    assert failed.complete_calls == 0
    assert failed.cancel_calls == 1


class _ManagedSource:
    def __init__(
        self,
        index: int,
        completion_callback,
        events: list[object],
    ) -> None:
        self.index = index
        self.closed = False
        self._completion_callback = completion_callback
        self._events = events

    def finish(self) -> None:
        assert not self.closed
        self.closed = True
        self._events.append(("source-close", self.index, True))
        if self._completion_callback is not None:
            self._completion_callback(True)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._events.append(("source-close", self.index, False))
        if self._completion_callback is not None:
            self._completion_callback(False)


class _ManagedFrozen:
    def __init__(self, index: int, events: list[object]) -> None:
        self.index = index
        self.events = events

    def resolve(self, succeeded: bool) -> None:
        self.events.append(("resolve", self.index, succeeded))


class _ManagedSession:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.next_index = 0

    def begin_managed(self, *, backend_key: str, work_frames: int) -> int:
        index = self.next_index
        self.next_index += 1
        self.events.append(("begin", index, backend_key, work_frames))
        return index

    def freeze_managed(
        self,
        observation: int,
        *,
        warm_used: bool,
        concurrent_workers: int,
    ) -> _ManagedFrozen:
        self.events.append(
            ("freeze", observation, warm_used, concurrent_workers)
        )
        return _ManagedFrozen(observation, self.events)

    def discard_managed(self, observation: int) -> None:
        self.events.append(("discard", observation))


def test_managed_batch_freezes_all_routes_before_yield_and_source_resolves(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    session = _ManagedSession(events)

    class _Result:
        peak_voices = 1
        manifest_sha256 = "c" * 64

        def __init__(self, index: int, warm_used: bool) -> None:
            self.index = index
            self._warm_used = warm_used

        def detach_source(self, *, completion_callback=None):
            events.append(("detach", self.index))
            return _ManagedSource(
                self.index,
                completion_callback,
                events,
            )

        def close(self) -> None:
            events.append(("result-close", self.index))

    class _Reservation:
        def __init__(self) -> None:
            self.index = 0

        def take(self) -> object:
            slot = SimpleNamespace(index=self.index, close=lambda: None)
            self.index += 1
            return slot

        def close(self) -> None:
            events.append("reservation-close")

    reservation = _Reservation()
    slot_context = ensemble_module._ManagedWorkerSlotContext(
        pool=SimpleNamespace(reserve_exact=lambda claims: reservation),
        owner_id="1" * 32,
        owner_cpu_capacity=2,
        worker_memory_bytes_by_part=(64, 64),
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_directory=tmp_path,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in range(2)
    )

    def start(job, **kwargs):
        del kwargs
        events.append(("start", job.index))
        return job.index

    def collect(handle: int) -> _Result:
        events.append(("collect", handle))
        return _Result(handle, warm_used=handle == 1)

    with (
        patch("tianlai.ensemble.retire_idle_stem_workers"),
        patch("tianlai.ensemble._try_start_stem_worker", side_effect=start),
        patch("tianlai.ensemble.collect_stem_worker", side_effect=collect),
        patch("tianlai.ensemble.terminate_stem_worker"),
    ):
        batch = ensemble_module._iter_managed_stem_batch(
            jobs,
            scratch_directory=tmp_path,
            allow_warm_start=False,
            slot_context=slot_context,
            adaptive_session=session,
            adaptive_backend_key_by_part=("backend-0", "backend-1"),
            adaptive_work_frames_by_part=(10_000, 20_000),
        )
        first = next(batch)
        # Every heterogeneous route owns the same batch timing boundary: no
        # child may start (or check out a warm worker) until all observations
        # have begun.  The worker starts are deliberately separate events so
        # a staggered start loop cannot silently move later timing origins.
        assert events[:4] == [
            ("begin", 0, "backend-0", 10_000),
            ("begin", 1, "backend-1", 20_000),
            ("start", 0),
            ("start", 1),
        ]
        # Collection and conservative route binding for the entire batch
        # happen before ordered downstream source consumption.
        assert [event for event in events if event[0] == "collect"] == [
            ("collect", 0),
            ("collect", 1),
        ]
        assert [event for event in events if event[0] == "freeze"] == [
            ("freeze", 0, False, 2),
            ("freeze", 1, True, 2),
        ]
        assert events[4:10] == [
            ("collect", 0),
            ("freeze", 0, False, 2),
            ("detach", 0),
            ("collect", 1),
            ("freeze", 1, True, 2),
            ("detach", 1),
        ]
        assert not any(event[0] == "resolve" for event in events)

        first[1].finish()
        second = next(batch)
        second[1].close()
        with pytest.raises(StopIteration):
            next(batch)

    assert ("resolve", 0, True) in events
    assert ("resolve", 1, False) in events
    assert not any(event[0] == "discard" for event in events)


def test_managed_batch_start_failure_discards_every_prestarted_observation(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    session = _ManagedSession(events)

    class _Reservation:
        def take(self) -> object:
            return SimpleNamespace(close=lambda: None)

        def close(self) -> None:
            events.append("reservation-close")

    context = ensemble_module._ManagedWorkerSlotContext(
        pool=SimpleNamespace(reserve_exact=lambda claims: _Reservation()),
        owner_id="2" * 32,
        owner_cpu_capacity=3,
        worker_memory_bytes_by_part=(64, 64, 64),
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_directory=tmp_path,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in range(3)
    )
    terminated: list[object] = []

    def start(job, **kwargs):
        del kwargs
        events.append(("start", job.index))
        if job.index == 1:
            raise ensemble_module.StemWorkerError("injected start failure")
        return job.index

    with (
        patch("tianlai.ensemble.retire_idle_stem_workers"),
        patch("tianlai.ensemble._try_start_stem_worker", side_effect=start),
        patch(
            "tianlai.ensemble.terminate_stem_worker",
            side_effect=terminated.append,
        ),
    ):
        batch = ensemble_module._iter_managed_stem_batch(
            jobs,
            scratch_directory=tmp_path,
            allow_warm_start=False,
            slot_context=context,
            adaptive_session=session,
            adaptive_backend_key_by_part=(
                "backend-0",
                "backend-1",
                "backend-2",
            ),
            adaptive_work_frames_by_part=(10_000, 20_000, 30_000),
        )
        with pytest.raises(
            ensemble_module._ManagedStemBatchFailure,
            match="injected start failure",
        ):
            next(batch)

    assert events[:5] == [
        ("begin", 0, "backend-0", 10_000),
        ("begin", 1, "backend-1", 20_000),
        ("begin", 2, "backend-2", 30_000),
        ("start", 0),
        ("start", 1),
    ]
    assert sorted(
        event for event in events if event[0] == "discard"
    ) == [
        ("discard", 0),
        ("discard", 1),
        ("discard", 2),
    ]
    assert not any(event[0] in {"freeze", "resolve"} for event in events)
    assert terminated == [0]


def test_managed_warm_retry_discards_first_batch_and_restarts_one_cold_clock(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    session = _ManagedSession(events)

    class _Result:
        peak_voices = 1
        manifest_sha256 = "e" * 64
        _warm_used = False

        def __init__(self, index: int) -> None:
            self.index = index

        def detach_source(self, *, completion_callback=None):
            events.append(("detach", self.index))
            return _ManagedSource(
                self.index,
                completion_callback,
                events,
            )

        def close(self) -> None:
            events.append(("result-close", self.index))

    class _Reservation:
        def take(self) -> object:
            return SimpleNamespace(close=lambda: None)

        def close(self) -> None:
            events.append("reservation-close")

    context = ensemble_module._ManagedWorkerSlotContext(
        pool=SimpleNamespace(reserve_exact=lambda claims: _Reservation()),
        owner_id="4" * 32,
        owner_cpu_capacity=2,
        worker_memory_bytes_by_part=(64, 64),
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_directory=tmp_path,
    )
    binding = ensemble_module._ManagedWarmBinding(
        owner_id=context.owner_id,
        scratch_directory=tmp_path,
        scratch_volume_id="test-volume",
        worker_memory_ceiling_bytes=64,
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_ceiling_bytes=64,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in range(2)
    )
    start_calls = 0

    def start(job, **kwargs):
        nonlocal start_calls
        start_calls += 1
        route = "warm" if kwargs["managed_warm_binding"] else "cold"
        events.append(("start", route, job.index))
        if start_calls == 2:
            raise ensemble_module.StemWorkerError("stale warm sibling")
        return f"{route}-{job.index}"

    def collect(handle: str) -> _Result:
        index = int(handle.rsplit("-", 1)[1])
        events.append(("collect", index))
        return _Result(index)

    with (
        patch("tianlai.ensemble.retire_idle_stem_workers"),
        patch("tianlai.ensemble._try_start_stem_worker", side_effect=start),
        patch("tianlai.ensemble.collect_stem_worker", side_effect=collect),
        patch("tianlai.ensemble.terminate_stem_worker"),
        patch("tianlai.ensemble._retire_managed_stem_worker_session"),
    ):
        batch = ensemble_module._iter_managed_stem_batch(
            jobs,
            scratch_directory=tmp_path,
            allow_warm_start=False,
            slot_context=context,
            warm_binding=binding,
            adaptive_session=session,
            adaptive_backend_key_by_part=("backend-0", "backend-1"),
            adaptive_work_frames_by_part=(10_000, 20_000),
        )
        first = next(batch)
        first[1].finish()
        second = next(batch)
        second[1].finish()
        with pytest.raises(StopIteration):
            next(batch)

    assert events[:4] == [
        ("begin", 0, "backend-0", 10_000),
        ("begin", 1, "backend-1", 20_000),
        ("start", "warm", 0),
        ("start", "warm", 1),
    ]
    assert ("discard", 0) in events
    assert ("discard", 1) in events
    second_begin = events.index(("begin", 2, "backend-0", 10_000))
    assert events[second_begin : second_begin + 4] == [
        ("begin", 2, "backend-0", 10_000),
        ("begin", 3, "backend-1", 20_000),
        ("start", "cold", 0),
        ("start", "cold", 1),
    ]
    assert ("freeze", 2, False, 2) in events
    assert ("freeze", 3, False, 2) in events
    assert ("resolve", 2, True) in events
    assert ("resolve", 3, True) in events


def test_managed_batch_late_validation_failure_never_adopts_a_timing(
    tmp_path: Path,
) -> None:
    events: list[object] = []
    session = _ManagedSession(events)

    class _Result:
        index = 0
        peak_voices = 1
        manifest_sha256 = "d" * 64
        _warm_used = False

        def __init__(self) -> None:
            self.source: _ManagedSource | None = None

        def detach_source(self, *, completion_callback=None):
            events.append(("detach", self.index))
            self.source = _ManagedSource(
                self.index,
                completion_callback,
                events,
            )
            return self.source

        def close(self) -> None:
            events.append(("result-close", self.index))

    class _Reservation:
        def take(self) -> object:
            return SimpleNamespace(close=lambda: None)

        def close(self) -> None:
            events.append("reservation-close")

    context = ensemble_module._ManagedWorkerSlotContext(
        pool=SimpleNamespace(reserve_exact=lambda claims: _Reservation()),
        owner_id="3" * 32,
        owner_cpu_capacity=3,
        worker_memory_bytes_by_part=(64, 64, 64),
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_directory=tmp_path,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in range(3)
    )

    def collect(handle: int) -> _Result:
        events.append(("collect", handle))
        if handle == 1:
            raise ensemble_module.StemWorkerError(
                "injected ordered validation failure"
            )
        return _Result()

    with (
        patch("tianlai.ensemble.retire_idle_stem_workers"),
        patch(
            "tianlai.ensemble._try_start_stem_worker",
            side_effect=(0, 1, 2),
        ),
        patch("tianlai.ensemble.collect_stem_worker", side_effect=collect),
        patch("tianlai.ensemble.terminate_stem_worker"),
    ):
        batch = ensemble_module._iter_managed_stem_batch(
            jobs,
            scratch_directory=tmp_path,
            allow_warm_start=False,
            slot_context=context,
            adaptive_session=session,
            adaptive_backend_key_by_part=(
                "backend-0",
                "backend-1",
                "backend-2",
            ),
            adaptive_work_frames_by_part=(10_000, 20_000, 30_000),
        )
        with pytest.raises(
            ensemble_module._ManagedStemBatchFailure,
            match="injected ordered validation failure",
        ):
            next(batch)

    assert [event for event in events if event[0] == "collect"] == [
        ("collect", 0),
        ("collect", 1),
    ]
    assert ("freeze", 0, False, 3) in events
    assert ("resolve", 0, False) in events
    assert ("discard", 1) in events
    assert ("discard", 2) in events
    assert not any(
        event[0] == "resolve" and event[2] is True for event in events
    )


class _FaultingAdvisor:
    def __init__(self, operation: str) -> None:
        self.operation = operation
        self.flush_calls = 0

    def begin_task(self, **kwargs):
        del kwargs
        if self.operation == "begin":
            raise OSError("optional timing begin failed")
        return "live"

    def freeze_task(self, token, **kwargs):
        del token, kwargs
        if self.operation == "freeze":
            raise OSError("optional timing freeze failed")
        return "frozen"

    def discard_task(self, token) -> None:
        del token

    def commit_task(self, token, **kwargs) -> bool:
        del token, kwargs
        return True

    def flush(self) -> bool:
        self.flush_calls += 1
        if self.operation == "flush":
            raise OSError("optional timing flush failed")
        return True


@pytest.mark.parametrize("operation", ["begin", "freeze"])
def test_ordinary_advisor_timing_error_does_not_change_serial_render(
    operation: str,
) -> None:
    expected = ("audio", 2, "d" * 64)
    session = AdaptiveRenderSession(advisor=_FaultingAdvisor(operation))
    with patch("tianlai.ensemble._render_part", return_value=expected):
        actual = ensemble_module._render_part_adaptively(
            object(),
            48_000,
            adaptive_session=session,
            adaptive_backend_key="backend",
            adaptive_work_frames=20_000,
        )
    assert actual == expected
    session.complete()


def test_ordinary_advisor_flush_error_does_not_break_raw_phase(
    tmp_path: Path,
) -> None:
    advisor = _FaultingAdvisor("flush")
    session = AdaptiveRenderSession(advisor=advisor)

    def body(*args: object, **kwargs: object):
        del args, kwargs
        yield "raw-stem"

    with (
        patch(
            "tianlai.ensemble.AdaptiveRenderSession",
            return_value=session,
        ),
        patch(
            "tianlai.ensemble._iter_raw_stems_in_plan_order_body",
            side_effect=body,
        ),
    ):
        assert tuple(
            ensemble_module._iter_raw_stems_in_plan_order(
                object(),
                scratch_directory=tmp_path,
                hall_tail_seconds=0.0,
                cache=None,
                stream_cache_hits=True,
                refresh=False,
                runtime_fingerprints={},
                summary=None,
            )
        ) == ("raw-stem",)
    assert advisor.flush_calls == 1
