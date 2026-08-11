from __future__ import annotations

from dataclasses import dataclass
import multiprocessing
import os
from pathlib import Path
import threading

import pytest

import tianlai.adaptive_parallelism as adaptive_module

from tianlai.adaptive_parallelism import (
    AdaptiveParallelismAdvisor,
    AdaptiveWorkload,
    default_adaptive_state_directory,
    make_adaptive_backend_key,
)
from tianlai.render_parallelism import (
    derive_parallelism_work_frames,
    select_render_parallelism,
)
from tianlai.resource_limits import ProjectLimits


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@dataclass(frozen=True)
class _Part:
    performance: dict[str, object]


@dataclass(frozen=True)
class _Plan:
    duration_seconds: float
    sample_rate: int
    parts: tuple[_Part, ...]


def _plan(count: int, duration: float) -> _Plan:
    return _Plan(
        duration,
        48_000,
        tuple(
            _Part(
                {
                    "duration_seconds": duration,
                    "sample_rate": 48_000,
                }
            )
            for _ in range(count)
        ),
    )


def _decision(count: int, duration: float, **changes: object):
    arguments: dict[str, object] = {
        "limits": ProjectLimits(max_audio_memory_bytes=4 * 1024**3),
        "workers_safe": True,
        "cpu_count": 16,
        "platform_system": "Linux",
        "scratch_available_bytes": 64 * 1024**3,
    }
    arguments.update(changes)
    return select_render_parallelism(_plan(count, duration), **arguments)


def _advisor(root: Path, clock: _Clock, *, cpu_count: int = 16):
    return AdaptiveParallelismAdvisor(
        state_directory=root,
        clock=clock,
        platform_system="Linux",
        platform_machine="test-machine",
        cpu_count=cpu_count,
    )


def _record(
    advisor: AdaptiveParallelismAdvisor,
    clock: _Clock,
    *,
    backend: str,
    work: int,
    execution: str,
    workers: int,
    elapsed: float,
    succeeded: bool = True,
    cancelled: bool = False,
    cache_hit: bool = False,
) -> bool:
    token = advisor.begin_task(
        backend_key=backend,
        work_frames=work,
        execution=execution,
        concurrent_workers=workers,
    )
    assert token is not None
    clock.advance(elapsed)
    return advisor.finish_task(
        token,
        succeeded=succeeded,
        cancelled=cancelled,
        cache_hit=cache_hit,
    )


def _teach_routes(
    advisor: AdaptiveParallelismAdvisor,
    clock: _Clock,
    backend: str,
    *,
    workers: int,
    serial_duration,
    managed_duration,
    managed_execution: str = "managed_cold",
) -> None:
    for work in (40_000, 60_000, 80_000, 100_000, 130_000, 160_000):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=serial_duration(work),
        )
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution=managed_execution,
            workers=workers,
            elapsed=managed_duration(work),
        )


def test_cold_start_keeps_the_existing_static_decision(tmp_path: Path) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    decision = _decision(4, 1.0)

    recommendation = advisor.recommend(
        decision,
        tuple(AdaptiveWorkload("oscillator:v1", 48_000) for _ in range(4)),
    )

    assert decision.reason == "short_workload"
    assert recommendation.worker_limit is None
    assert not recommendation.allow_short_workload


def test_automatic_route_without_models_does_not_explore(
    tmp_path: Path,
) -> None:
    advisor = _advisor(tmp_path / "state", _Clock())
    decision = _decision(4, 3.0)

    recommendation = advisor.recommend(
        decision,
        tuple(AdaptiveWorkload("unseen-backend", 144_000) for _ in range(4)),
    )

    assert decision.reason == "automatic"
    assert recommendation.worker_limit is None
    assert recommendation.reason == "insufficient_evidence"


def test_controlled_exploration_learns_three_then_two_from_zero(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "resident-process-exploration"
    work = 144_000
    for _ in range(6):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=1.0,
        )
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=4,
            elapsed=0.5,
        )
    decision = _decision(12, 3.0)
    workloads = tuple(AdaptiveWorkload(backend, work) for _ in range(12))

    for workers, elapsed in ((3, 0.15), (2, 0.08)):
        for _ in range(6):
            recommendation = advisor.recommend(decision, workloads)
            assert recommendation.worker_limit == workers
            assert recommendation.reason == "controlled_exploration"
            assert _record(
                advisor,
                clock,
                backend=backend,
                work=work,
                execution="managed_cold",
                workers=workers,
                elapsed=elapsed,
            )

    recommendation = advisor.recommend(decision, workloads)

    assert recommendation.worker_limit == 2
    assert recommendation.reason == "learned_benefit"
    for workers in (2, 3):
        assert advisor.observation_count(
            backend_key=backend,
            execution="managed_cold",
            concurrent_workers=workers,
        ) == 6


@pytest.mark.parametrize("outcome", ("failed", "cancelled", "cache"))
def test_unsuccessful_exploration_never_retries_in_process_or_on_reload(
    tmp_path: Path,
    outcome: str,
) -> None:
    state = tmp_path / outcome
    clock = _Clock()
    advisor = _advisor(state, clock)
    backend = f"unsuccessful-exploration-{outcome}"
    work = 144_000
    for _ in range(6):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=1.0,
        )
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=4,
            elapsed=0.5,
        )
    decision = _decision(12, 3.0)
    workloads = tuple(AdaptiveWorkload(backend, work) for _ in range(12))
    trial = advisor.recommend(decision, workloads)
    assert trial.worker_limit == 3
    assert trial.reason == "controlled_exploration"

    if outcome == "cache":
        assert advisor.begin_task(
            backend_key=backend,
            work_frames=work,
            execution="managed_cold",
            concurrent_workers=3,
            cache_hit=True,
        ) is None
    else:
        assert not _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=3,
            elapsed=0.1,
            succeeded=outcome != "failed",
            cancelled=outcome == "cancelled",
        )

    for _ in range(3):
        fallback = advisor.recommend(decision, workloads)
        assert fallback.worker_limit == 4
        assert fallback.reason == "learned_benefit"
    assert advisor.observation_count(
        backend_key=backend,
        execution="managed_cold",
        concurrent_workers=3,
    ) == 0
    assert advisor.flush()

    # Persisted performance evidence is reusable, but willingness to launch
    # an unknown route is deliberately not inherited by a fresh one-shot CLI.
    reloaded = _advisor(state, _Clock())
    after_reload = reloaded.recommend(decision, workloads)
    assert after_reload.worker_limit == 4
    assert after_reload.reason == "learned_benefit"


def test_unprofitable_exploration_stops_before_the_next_lower_width(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "unprofitable-downward-width"
    work = 144_000
    for _ in range(6):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=1.0,
        )
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=4,
            elapsed=0.5,
        )
    decision = _decision(12, 3.0)
    workloads = tuple(AdaptiveWorkload(backend, work) for _ in range(12))
    for _ in range(6):
        trial = advisor.recommend(decision, workloads)
        assert trial.worker_limit == 3
        assert trial.reason == "controlled_exploration"
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=3,
            elapsed=0.48,
        )

    fallback = advisor.recommend(decision, workloads)

    assert fallback.worker_limit == 4
    assert fallback.reason == "learned_benefit"
    assert advisor.observation_count(
        backend_key=backend,
        execution="managed_cold",
        concurrent_workers=2,
    ) == 0


def test_repeated_heterogeneous_plan_builds_a_complete_recommendation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    clock = _Clock()
    advisor = _advisor(state, clock)
    work = 144_000
    backends = tuple(f"heterogeneous-backend-{index}" for index in range(4))
    for _ in range(6):
        for index, backend in enumerate(backends):
            assert _record(
                advisor,
                clock,
                backend=backend,
                work=work,
                execution="serial",
                workers=1,
                elapsed=1.0 + index * 0.05,
            )
            # Later positions deliberately have larger conservative elapsed
            # timings, as they do after earlier ordered protocol validation.
            assert _record(
                advisor,
                clock,
                backend=backend,
                work=work,
                execution="managed_cold",
                workers=4,
                elapsed=0.12 + index * 0.03,
            )
    assert advisor.flush()

    reloaded = _advisor(state, _Clock())
    recommendation = reloaded.recommend(
        _decision(4, 3.0),
        tuple(AdaptiveWorkload(backend, work) for backend in backends),
    )

    assert recommendation.worker_limit == 4
    assert recommendation.reason == "learned_benefit"


def test_stable_backend_timings_can_promote_only_the_short_workload_gate(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "trusted-oscillator:manifest-sha"
    _teach_routes(
        advisor,
        clock,
        backend,
        workers=4,
        serial_duration=lambda work: 0.01 + work / 40_000,
        managed_duration=lambda work: 0.12 + work / 100_000,
    )
    static = _decision(4, 1.0)
    workloads = tuple(AdaptiveWorkload(backend, 48_000) for _ in range(4))

    recommendation = advisor.recommend(static, workloads)
    adjusted = _decision(
        4,
        1.0,
        adaptive_worker_limit=recommendation.worker_limit,
        adaptive_short_workload=recommendation.allow_short_workload,
    )

    assert recommendation.worker_limit == 4
    assert recommendation.allow_short_workload
    assert adjusted.worker_count == 4
    assert adjusted.reason == "adaptive"


def test_repeated_exact_workload_can_learn_without_cross_work_samples(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "repeated-project-backend"
    work = 48_000
    for serial_elapsed, managed_elapsed in (
        (1.00, 0.20),
        (1.01, 0.19),
        (0.99, 0.21),
        (1.02, 0.20),
        (0.98, 0.19),
        (1.00, 0.21),
    ):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=serial_elapsed,
        )
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=4,
            elapsed=managed_elapsed,
        )

    recommendation = advisor.recommend(
        _decision(4, 1.0),
        tuple(AdaptiveWorkload(backend, work) for _ in range(4)),
    )

    assert recommendation.worker_limit == 4
    assert recommendation.allow_short_workload
    assert recommendation.reason == "learned_benefit"


def test_eight_repeated_exact_samples_do_not_erase_a_learned_route(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "iterated-project-backend"
    _teach_routes(
        advisor,
        clock,
        backend,
        workers=4,
        serial_duration=lambda work: 0.01 + work / 40_000,
        managed_duration=lambda work: 0.12 + work / 100_000,
    )
    work = 48_000
    for index in range(8):
        jitter = (index % 3 - 1) * 0.005
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=1.21 + jitter,
        )
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=4,
            elapsed=0.60 + jitter,
        )

    recommendation = advisor.recommend(
        _decision(4, 1.0),
        tuple(AdaptiveWorkload(backend, work) for _ in range(4)),
    )

    assert advisor.observation_count(
        backend_key=backend,
        execution="serial",
        concurrent_workers=1,
    ) == 8
    assert recommendation.worker_limit == 4
    assert recommendation.allow_short_workload


def test_exact_workload_model_is_never_used_for_a_neighbouring_work_value(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "exact-only-backend"
    trained_work = 48_000
    for _ in range(6):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=trained_work,
            execution="serial",
            workers=1,
            elapsed=1.0,
        )
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=trained_work,
            execution="managed_cold",
            workers=4,
            elapsed=0.2,
        )

    recommendation = advisor.recommend(
        _decision(4, 1.0),
        tuple(
            AdaptiveWorkload(backend, trained_work + 1)
            for _ in range(4)
        ),
    )

    assert recommendation.worker_limit is None
    assert recommendation.reason == "insufficient_evidence"


def test_warm_and_cold_worker_timings_are_never_mixed(tmp_path: Path) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "warm-reusable-oscillator"
    _teach_routes(
        advisor,
        clock,
        backend,
        workers=4,
        serial_duration=lambda work: 0.01 + work / 40_000,
        managed_duration=lambda work: 0.01 + work / 120_000,
        managed_execution="managed_warm",
    )
    decision = _decision(4, 1.0)
    workloads = tuple(AdaptiveWorkload(backend, 48_000) for _ in range(4))

    cold = advisor.recommend(decision, workloads)
    warm = advisor.recommend(
        decision, workloads, managed_execution="managed_warm"
    )

    assert cold.worker_limit is None
    assert warm.worker_limit == 4
    assert warm.allow_short_workload


def test_adaptive_short_admission_never_bypasses_hard_resource_gates() -> None:
    for changes, reason in (
        ({"workers_safe": False}, "workers_ineligible"),
        ({"cpu_count": 1}, "single_cpu"),
        ({"scratch_available_bytes": 0}, "scratch_budget"),
        (
            {"limits": ProjectLimits(max_audio_memory_bytes=1)},
            "memory_budget",
        ),
    ):
        decision = _decision(
            4,
            1.0,
            adaptive_worker_limit=4,
            adaptive_short_workload=True,
            **changes,
        )
        assert decision.worker_count == 1
        assert decision.reason == reason


def test_success_clock_is_trusted_and_other_outcomes_never_pollute(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "sample:v1"

    assert _record(
        advisor,
        clock,
        backend=backend,
        work=80_000,
        execution="serial",
        workers=1,
        elapsed=1.0,
    )
    assert not _record(
        advisor,
        clock,
        backend=backend,
        work=90_000,
        execution="serial",
        workers=1,
        elapsed=1.0,
        succeeded=False,
    )
    assert not _record(
        advisor,
        clock,
        backend=backend,
        work=100_000,
        execution="serial",
        workers=1,
        elapsed=1.0,
        cancelled=True,
    )
    assert not _record(
        advisor,
        clock,
        backend=backend,
        work=110_000,
        execution="serial",
        workers=1,
        elapsed=1.0,
        cache_hit=True,
    )
    assert advisor.begin_task(
        backend_key=backend,
        work_frames=120_000,
        execution="serial",
        concurrent_workers=1,
        cache_hit=True,
    ) is None
    assert advisor.begin_task(
        backend_key=backend,
        work_frames=120_000,
        execution=[],  # type: ignore[arg-type]
        concurrent_workers=True,
    ) is None

    assert advisor.observation_count(
        backend_key=backend,
        execution="serial",
        concurrent_workers=1,
    ) == 1


def test_frozen_worker_elapsed_excludes_later_source_consumption(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    token = advisor.begin_task(
        backend_key="two-phase-worker",
        work_frames=80_000,
        execution="managed_cold",
        concurrent_workers=2,
    )
    assert token is not None
    clock.advance(0.25)
    completed = advisor.freeze_task(token)
    assert completed is not None
    # Ordered gain/WAV/mix consumption must not inflate backend render time.
    clock.advance(20.0)
    assert advisor.commit_task(completed, succeeded=True)
    assert advisor.flush()

    path = advisor.state_path
    assert path is not None
    wrapper = adaptive_module.json.loads(path.read_text(encoding="ascii"))
    backend_id = adaptive_module._backend_id("two-phase-worker")
    assert backend_id is not None
    samples = wrapper["payload"]["backends"][backend_id]["routes"][
        "managed_cold:2"
    ]
    assert samples == [[80_000, 250_000]]


def test_managed_route_is_bound_after_checkout_without_losing_startup(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    token = advisor.begin_managed_task(
        backend_key="late-bound-route",
        work_frames=90_000,
    )
    assert token is not None
    clock.advance(0.4)
    completed = advisor.freeze_task(
        token,
        execution="managed_warm",
        concurrent_workers=3,
    )
    assert completed is not None
    assert advisor.commit_task(completed, succeeded=True)

    assert advisor.observation_count(
        backend_key="late-bound-route",
        execution="managed_warm",
        concurrent_workers=3,
    ) == 1
    assert advisor.observation_count(
        backend_key="late-bound-route",
        execution="managed_cold",
        concurrent_workers=3,
    ) == 0


def test_pending_managed_route_requires_actual_batch_width(
    tmp_path: Path,
) -> None:
    advisor = _advisor(tmp_path / "state", _Clock())
    for workers in (1, 5):
        token = advisor.begin_managed_task(
            backend_key=f"invalid-width-{workers}",
            work_frames=90_000,
        )
        assert token is not None
        assert advisor.freeze_task(
            token,
            execution="managed_cold",
            concurrent_workers=workers,
        ) is None


def test_abandoned_frozen_worker_timing_is_discarded(tmp_path: Path) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    token = advisor.begin_task(
        backend_key="abandoned-worker",
        work_frames=80_000,
        execution="managed_cold",
        concurrent_workers=2,
    )
    assert token is not None
    clock.advance(0.5)
    completed = advisor.freeze_task(token)
    assert completed is not None

    advisor.discard_task(completed)

    assert advisor.observation_count(
        backend_key="abandoned-worker",
        execution="managed_cold",
        concurrent_workers=2,
    ) == 0


@pytest.mark.skipif(
    os.name == "nt" or "fork" not in multiprocessing.get_all_start_methods(),
    reason="requires POSIX fork inheritance",
)
def test_fork_child_cannot_commit_or_flush_parent_advisor(tmp_path: Path) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    token = advisor.begin_task(
        backend_key="fork-owned-advisor",
        work_frames=80_000,
        execution="serial",
        concurrent_workers=1,
    )
    assert token is not None
    clock.advance(0.5)
    completed = advisor.freeze_task(token)
    assert completed is not None
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)

    def child_attempt() -> None:
        send.send(
            (
                advisor.commit_task(completed, succeeded=True),
                advisor.flush(),
                advisor.begin_managed_task(
                    backend_key="fork-child",
                    work_frames=40_000,
                )
                is None,
            )
        )
        send.close()

    child = context.Process(target=child_attempt)
    child.start()
    send.close()
    assert receive.recv() == (False, False, True)
    child.join(timeout=5)
    assert child.exitcode == 0

    assert advisor.commit_task(completed, succeeded=True)
    assert advisor.flush()
    assert advisor.observation_count(
        backend_key="fork-owned-advisor",
        execution="serial",
        concurrent_workers=1,
    ) == 1


def test_complete_stable_model_can_reduce_an_unprofitable_static_window(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "very-fast-in-process-backend"
    _teach_routes(
        advisor,
        clock,
        backend,
        workers=4,
        serial_duration=lambda work: 0.01 + work / 120_000,
        managed_duration=lambda work: 4.0 + work / 120_000,
    )
    static = _decision(4, 3.0)
    workloads = tuple(AdaptiveWorkload(backend, 144_000) for _ in range(4))

    recommendation = advisor.recommend(static, workloads)

    assert static.reason == "automatic"
    assert recommendation.worker_limit == 1
    assert recommendation.reason == "learned_serial_benefit"


def test_noisy_or_out_of_range_evidence_keeps_static_policy(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "unstable-native-backend"
    works = (40_000, 60_000, 80_000, 100_000, 130_000, 160_000)
    for index, work in enumerate(works):
        _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=(0.1 if index % 2 else 4.0) + work / 100_000,
        )
        _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=4,
            elapsed=(0.1 if index % 2 else 5.0) + work / 100_000,
        )

    recommendation = advisor.recommend(
        _decision(4, 3.0),
        tuple(AdaptiveWorkload(backend, 144_000) for _ in range(4)),
    )

    assert recommendation.worker_limit is None


def test_out_of_range_managed_model_cannot_demote_a_large_static_job(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "small-only-managed-history"
    for work in (40_000, 60_000, 80_000, 100_000, 130_000, 160_000):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="serial",
            workers=1,
            elapsed=0.2 + work / 80_000,
        )
    for work in (1_000, 1_200, 1_400, 1_600, 1_800, 2_000):
        assert _record(
            advisor,
            clock,
            backend=backend,
            work=work,
            execution="managed_cold",
            workers=4,
            elapsed=0.5 + work / 100_000,
        )

    recommendation = advisor.recommend(
        _decision(4, 3.0),
        tuple(AdaptiveWorkload(backend, 144_000) for _ in range(4)),
    )

    assert recommendation.worker_limit is None
    assert recommendation.reason == "insufficient_evidence"


def test_state_is_cross_instance_merged_and_machine_scoped(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first_clock = _Clock()
    first = _advisor(state, first_clock, cpu_count=16)
    assert _record(
        first,
        first_clock,
        backend="backend-a",
        work=40_000,
        execution="serial",
        workers=1,
        elapsed=1.0,
    )
    assert first.flush()
    second_clock = _Clock()
    second = _advisor(state, second_clock, cpu_count=16)
    assert _record(
        second,
        second_clock,
        backend="backend-b",
        work=50_000,
        execution="serial",
        workers=1,
        elapsed=1.0,
    )
    assert second.flush()
    reopened = _advisor(state, _Clock(), cpu_count=16)
    different_machine = _advisor(state, _Clock(), cpu_count=8)

    assert reopened.observation_count(
        backend_key="backend-a", execution="serial", concurrent_workers=1
    ) == 1
    assert reopened.observation_count(
        backend_key="backend-b", execution="serial", concurrent_workers=1
    ) == 1
    assert different_machine.observation_count(
        backend_key="backend-a", execution="serial", concurrent_workers=1
    ) == 0


def test_persisted_state_is_small_private_and_does_not_store_backend_names(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    backend = "private-project-backend-name"
    assert _record(
        advisor,
        clock,
        backend=backend,
        work=40_000,
        execution="serial",
        workers=1,
        elapsed=1.0,
    )
    assert advisor.flush()
    path = advisor.state_path
    assert path is not None
    payload = path.read_bytes()

    assert len(payload) < 64 * 1024
    assert backend.encode("utf-8") not in payload
    if os.name != "nt":
        assert path.parent.stat().st_mode & 0o077 == 0


def test_many_finishes_are_volatile_until_one_bounded_flush(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    path = advisor.state_path
    assert path is not None
    real_replace = adaptive_module.os.replace
    replacements: list[tuple[object, object]] = []

    def track_replace(source, destination) -> None:
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(adaptive_module.os, "replace", track_replace)
    for index in range(20):
        assert _record(
            advisor,
            clock,
            backend="short-stem-backend",
            work=40_000 + index * 1_000,
            execution="serial",
            workers=1,
            elapsed=0.1 + index / 1_000,
        )

    assert not path.exists()
    assert replacements == []
    assert advisor.pending_observation_count == 20
    assert advisor.flush()
    assert len(replacements) == 1
    assert advisor.pending_observation_count == 0
    assert advisor.flush()
    assert len(replacements) == 1


def test_finish_during_flush_remains_pending_for_the_next_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    assert _record(
        advisor,
        clock,
        backend="concurrent-backend",
        work=40_000,
        execution="serial",
        workers=1,
        elapsed=0.5,
    )
    real_replace = adaptive_module.os.replace
    replace_entered = threading.Event()
    allow_replace = threading.Event()

    def paused_replace(source, destination) -> None:
        replace_entered.set()
        assert allow_replace.wait(timeout=5)
        real_replace(source, destination)

    monkeypatch.setattr(adaptive_module.os, "replace", paused_replace)
    result: list[bool] = []
    flush_thread = threading.Thread(target=lambda: result.append(advisor.flush()))
    flush_thread.start()
    assert replace_entered.wait(timeout=5)
    assert _record(
        advisor,
        clock,
        backend="concurrent-backend",
        work=50_000,
        execution="serial",
        workers=1,
        elapsed=0.6,
    )
    allow_replace.set()
    flush_thread.join(timeout=5)

    assert not flush_thread.is_alive()
    assert result == [True]
    assert advisor.pending_observation_count == 1
    after_first = _advisor(tmp_path / "state", _Clock())
    assert after_first.observation_count(
        backend_key="concurrent-backend",
        execution="serial",
        concurrent_workers=1,
    ) == 1
    assert advisor.flush()
    after_second = _advisor(tmp_path / "state", _Clock())
    assert after_second.observation_count(
        backend_key="concurrent-backend",
        execution="serial",
        concurrent_workers=1,
    ) == 2


def test_failed_atomic_publish_keeps_pending_and_never_unlinks_by_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    clock = _Clock()
    state = tmp_path / "state"
    advisor = _advisor(state, clock)
    assert _record(
        advisor,
        clock,
        backend="publish-failure-backend",
        work=40_000,
        execution="serial",
        workers=1,
        elapsed=0.5,
    )
    replace_calls = 0

    def fail_replace(source, destination) -> None:
        nonlocal replace_calls
        replace_calls += 1
        raise OSError("simulated atomic publish failure")

    monkeypatch.setattr(adaptive_module.os, "replace", fail_replace)

    assert not advisor.flush()
    retained = tuple(state.glob(".timings-*-v1.*.tmp"))
    assert len(retained) == 1
    assert retained[0].is_file()
    assert advisor.pending_observation_count == 1
    assert advisor.persistence_disabled
    assert not advisor.persistence_available
    assert not advisor.flush()
    assert replace_calls == 1
    assert tuple(state.glob(".timings-*-v1.*.tmp")) == retained


def test_corrupt_or_oversized_state_fails_soft_to_cold_start(
    tmp_path: Path,
) -> None:
    clock = _Clock()
    advisor = _advisor(tmp_path / "state", clock)
    path = advisor.state_path
    assert path is not None
    path.write_bytes(b"not-json")
    reopened = _advisor(tmp_path / "state", _Clock())

    recommendation = reopened.recommend(
        _decision(4, 1.0),
        tuple(AdaptiveWorkload("backend", 48_000) for _ in range(4)),
    )

    assert recommendation.worker_limit is None
    path.write_bytes(b"x" * (64 * 1024 + 1))
    assert reopened.observation_count(
        backend_key="backend", execution="serial", concurrent_workers=1
    ) == 0


def test_default_state_locations_require_absolute_private_user_roots(
    tmp_path: Path,
) -> None:
    assert default_adaptive_state_directory(
        platform_system="Windows",
        environ={"LOCALAPPDATA": str(tmp_path)},
    ) == tmp_path / "Tianlai" / "adaptive-parallelism"
    assert default_adaptive_state_directory(
        platform_system="Linux",
        environ={},
        home_directory=tmp_path,
    ) == tmp_path / ".local" / "state" / "tianlai" / (
        "adaptive-parallelism"
    )


def test_backend_key_binds_engine_manifest_variant_and_backend_kind() -> None:
    manifest = "a" * 64
    engine = "b" * 64
    first = make_adaptive_backend_key(
        manifest_sha256=manifest,
        engine_sha256=engine,
        overrides_json=b'{"sample_variant":"close","release_seconds":1}',
        sample_backed=True,
    )
    equivalent = make_adaptive_backend_key(
        manifest_sha256=manifest,
        engine_sha256=engine,
        overrides_json=b'{ "release_seconds": 1, "sample_variant": "close" }',
        sample_backed=True,
    )
    dsp = make_adaptive_backend_key(
        manifest_sha256=manifest,
        engine_sha256=engine,
        overrides_json=b'{"sample_variant":"close","release_seconds":1}',
        sample_backed=False,
    )

    assert first is not None
    assert first == equivalent
    assert first != dsp
    assert make_adaptive_backend_key(
        manifest_sha256="invalid",
        engine_sha256=engine,
        overrides_json=b"{}",
        sample_backed=True,
    ) is None


def test_work_frame_interface_preserves_backend_plan_order() -> None:
    plan = _Plan(
        2.0,
        48_000,
        (
            _Part({"duration_seconds": 2.0, "sample_rate": 48_000}),
            _Part(
                {
                    "duration_seconds": 2.0,
                    "sample_rate": 48_000,
                    "events": [
                        {
                            "type": "note_on",
                            "time": 0.0,
                            "note_id": 1,
                            "velocity": 0.8,
                        },
                        {"type": "note_off", "time": 2.0, "note_id": 1},
                    ],
                }
            ),
        ),
    )

    work = derive_parallelism_work_frames(
        plan, sample_backed_by_part=(False, True)
    )

    assert work is not None
    assert work[0] == 96_000
    assert work[1] > work[0]
