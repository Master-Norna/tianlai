from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import patch

import pytest

from tianlai import ensemble as ensemble_module
from tianlai import stem_worker as stem_worker_module
from tianlai import worker_slots as worker_slots_module
from tianlai.adaptive_parallelism import AdaptiveParallelismAdvisor
from tianlai.adaptive_runtime import AdaptiveRenderSession
from tianlai.ensemble import render_plan
from tianlai.roster import CollaborationSettings
from tianlai.stem_worker import StemWorkerError


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "乐器" / "电子乐器" / "温暖铺底" / "乐器.json"


def _write_local_implementation_manifest(root: Path) -> Path:
    """Create a real third-party-style local factory for barrier tests."""

    directory = root / "local-instrument"
    directory.mkdir()
    implementation = directory / "instrument_impl.py"
    implementation.write_text(
        """from tianlai.synthesizer import SynthesizerInstrument


def create(*, manifest, sample_rate, base_directory):
    del base_directory
    return SynthesizerInstrument.from_manifest(manifest, sample_rate)
""",
        encoding="utf-8",
    )
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["implementation"] = implementation.name
    manifest_path = directory / "乐器.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest_path


@pytest.fixture(autouse=True)
def isolated_managed_worker_slots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot_directory = tmp_path / "managed-worker-slots"
    monkeypatch.setattr(
        worker_slots_module,
        "default_worker_slot_directory",
        lambda: slot_directory,
    )


class _ParallelPlan:
    def __init__(
        self,
        *,
        duration_seconds: float = 0.05,
        part_count: int = 3,
        local_implementation_indices: frozenset[int] = frozenset(),
        local_implementation_manifest: Path | None = None,
    ) -> None:
        if local_implementation_indices and local_implementation_manifest is None:
            raise ValueError("local implementation indices need a test manifest")
        self.sample_rate = 8_000
        self.duration_seconds = duration_seconds
        self.collaboration = CollaborationSettings()
        parts = []
        midi_notes = (60, 67, 72, 76, 79)
        for index in range(part_count):
            midi_note = midi_notes[index % len(midi_notes)]
            executor_id = f"oscillator-{index}"
            uses_local_implementation = (
                index in local_implementation_indices
            )
            capability = SimpleNamespace(
                manifest_path=str(
                    local_implementation_manifest
                    if uses_local_implementation
                    else MANIFEST
                ),
                relative_path=(
                    "测试工具/本地实现"
                    if uses_local_implementation
                    else "电子乐器/温暖铺底"
                ),
                quality_tier="formal",
                collaboration_review_status="untested",
                license_status="approved",
            )
            executor = SimpleNamespace(
                executor_id=executor_id,
                part_id=f"part-{index}",
                capability=capability,
                override_map={},
                gain_db=-12.0,
                pan=(-0.4 + index * 0.4),
                seat=SimpleNamespace(distance_m=3.0 + index),
            )
            performance = {
                "sample_rate": self.sample_rate,
                "channels": 2,
                "duration_seconds": duration_seconds,
                "tail_seconds": 0.0,
                "events": [
                    {
                        "type": "note_on",
                        "time": 0.0,
                        "note_id": index + 1,
                        "midi_note": midi_note,
                        "velocity": 0.35,
                    },
                    {
                        "type": "note_off",
                        "time": min(0.02, duration_seconds),
                        "note_id": index + 1,
                    },
                ],
            }
            parts.append(
                SimpleNamespace(
                    executor=executor,
                    performance=performance,
                    gain_envelope=(),
                )
            )
        self.parts = tuple(parts)

    def to_dict(self) -> dict:
        return {
            "title": "managed parallel byte identity",
            "sample_rate": self.sample_rate,
            "duration_seconds": self.duration_seconds,
            "parts": [
                {
                    "executor_id": part.executor.executor_id,
                    "part_id": part.executor.part_id,
                    "instrument": part.executor.capability.relative_path,
                    "gain_db": part.executor.gain_db,
                    "pan": part.executor.pan,
                    "performance": part.performance,
                }
                for part in self.parts
            ],
        }


def _artifact_bytes(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _real_managed_worker_count(*, minimum: int = 2) -> int:
    """Use the host's real managed-worker ceiling for subprocess tests."""

    worker_count = ensemble_module.automatic_worker_capacity()
    if worker_count < minimum:
        pytest.skip(
            f"real managed-worker test needs at least {minimum} workers"
        )
    return worker_count


def test_managed_parallel_render_is_publicly_byte_identical_to_serial() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(part_count=worker_count)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        serial_directory = root / "serial"
        parallel_directory = root / "并行 输出"
        with patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=1,
        ):
            serial = render_plan(plan, serial_directory)
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as starts,
        ):
            parallel = render_plan(plan, parallel_directory)

        assert starts.call_count == worker_count
        assert _artifact_bytes(serial_directory) == _artifact_bytes(
            parallel_directory
        )
        assert [stem.peak_voices for stem in serial.stems] == [
            stem.peak_voices for stem in parallel.stems
        ]


def test_session_warm_render_is_publicly_byte_identical_to_serial() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(part_count=worker_count * 2)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        with patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=1,
        ):
            render_plan(plan, root / "serial")
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as starts,
        ):
            render_plan(plan, root / "warm")

        assert starts.call_count == worker_count * 2
        assert _artifact_bytes(root / "serial") == _artifact_bytes(
            root / "warm"
        )


def test_real_two_batch_run_records_cold_then_warm_samples() -> None:
    worker_count = _real_managed_worker_count()
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        advisor = AdaptiveParallelismAdvisor(
            state_directory=root / "adaptive-state"
        )
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble.AdaptiveRenderSession",
                side_effect=lambda: AdaptiveRenderSession(advisor=advisor),
            ),
        ):
            render_plan(
                _ParallelPlan(part_count=worker_count * 2),
                root / "two-batch-routes",
            )

        state_path = advisor.state_path
        assert state_path is not None
        payload = json.loads(state_path.read_text(encoding="ascii"))["payload"]
        route_counts: dict[str, int] = {}
        for entry in payload["backends"].values():
            for route, samples in entry["routes"].items():
                route_counts[route] = route_counts.get(route, 0) + len(samples)

        assert route_counts == {
            f"managed_cold:{worker_count}": worker_count,
            f"managed_warm:{worker_count}": worker_count,
        }


def test_global_slot_denial_falls_back_before_any_worker_spawn() -> None:
    plan = _ParallelPlan()
    denied_pool = SimpleNamespace(reserve_exact=lambda claims: None)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        with patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=1,
        ):
            render_plan(plan, root / "serial")
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=3,
            ),
            patch(
                "tianlai.ensemble.WorkerSlotPool",
                return_value=denied_pool,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                side_effect=AssertionError(
                    "an all-or-none denial must precede every spawn"
                ),
            ) as starts,
        ):
            render_plan(plan, root / "denied")

        assert starts.call_count == 0
        assert _artifact_bytes(root / "serial") == _artifact_bytes(
            root / "denied"
        )


def test_global_slot_memory_error_is_not_hidden_by_serial_fallback() -> None:
    plan = _ParallelPlan()

    class MemoryPressurePool:
        def reserve_exact(self, claims: object) -> object:
            del claims
            raise MemoryError("injected global admission pressure")

    with tempfile.TemporaryDirectory() as temporary_directory:
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=3,
            ),
            patch(
                "tianlai.ensemble.WorkerSlotPool",
                return_value=MemoryPressurePool(),
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=AssertionError(
                    "MemoryError must not retry in process"
                ),
            ),
        ):
            with pytest.raises(
                MemoryError,
                match="injected global admission pressure",
            ):
                render_plan(plan, Path(temporary_directory) / "pressure")


def test_idle_workers_retire_before_mix_phase() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(part_count=worker_count * 2)
    observed_at_mix: list[int] = []
    with tempfile.TemporaryDirectory() as temporary_directory:
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as starts,
            patch(
                "tianlai.ensemble.retire_idle_stem_workers",
                wraps=ensemble_module.retire_idle_stem_workers,
            ) as retire_idle,
        ):

            def observe_phase(
                phase: str,
                completed: int,
                total: int,
            ) -> None:
                del completed, total
                if phase == "mix":
                    observed_at_mix.append(retire_idle.call_count)

            render_plan(
                plan,
                Path(temporary_directory) / "phase-boundary",
                _progress_callback=observe_phase,
            )

        # The first batch removes unrelated idle RSS; the render-session
        # wrapper then retires its own persistent children before mix.
        assert starts.call_count == worker_count * 2
        assert retire_idle.call_count >= 2
        assert observed_at_mix
        assert all(count >= 2 for count in observed_at_mix)
        assert not stem_worker_module._WARM_IDLE
        assert not stem_worker_module._WARM_ALL


def test_globally_coordinated_known_run_reuses_session_workers() -> None:
    worker_count = _real_managed_worker_count()
    original_start = ensemble_module._try_start_stem_worker
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        observations: dict[int, list[tuple[bool, bool, int]]] = {}
        for part_count in (worker_count, worker_count * 2):
            calls: list[tuple[bool, bool, int]] = []

            def tracked_start(*args: object, **kwargs: object) -> object:
                allow_start = kwargs.get("allow_warm_start")
                assert type(allow_start) is bool
                handle = original_start(*args, **kwargs)
                assert handle is not None
                calls.append(
                    (
                        allow_start,
                        handle._warm_worker is not None,
                        handle.process.pid,
                    )
                )
                return handle

            with (
                patch(
                    "tianlai.ensemble._automatic_stem_worker_count",
                    return_value=worker_count,
                ),
                patch(
                    "tianlai.ensemble._try_start_stem_worker",
                    side_effect=tracked_start,
                ),
            ):
                render_plan(
                    _ParallelPlan(part_count=part_count),
                    root / f"route-{part_count}",
                )
            observations[part_count] = calls

        one_batch = observations[worker_count]
        # A single final batch cannot amortise startup and remains one-shot.
        assert [allow for allow, _warm, _pid in one_batch] == (
            [False] * worker_count
        )
        assert not any(warm for _allow, warm, _pid in one_batch)

        two_batches = observations[worker_count * 2]
        assert [allow for allow, _warm, _pid in two_batches] == (
            [True] * worker_count + [False] * worker_count
        )
        assert all(warm for _allow, warm, _pid in two_batches)
        first_pids = {
            pid
            for _allow, _warm, pid in two_batches[:worker_count]
        }
        second_pids = {
            pid
            for _allow, _warm, pid in two_batches[worker_count:]
        }
        assert len(first_pids) == worker_count
        assert len(second_pids) == worker_count
        assert second_pids == first_pids
        assert not stem_worker_module._WARM_ALL


def test_three_batch_warm_run_reserves_global_slots_only_once() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(part_count=worker_count * 3)
    actual_pool = worker_slots_module.WorkerSlotPool()
    reservations: list[tuple[object, ...]] = []

    class CountingPool:
        def reserve_exact(self, claims: object) -> object:
            requested = tuple(claims)
            reservations.append(requested)
            return actual_pool.reserve_exact(requested)

    pids: list[int] = []
    original_start = ensemble_module._try_start_stem_worker

    def tracked_start(*args: object, **kwargs: object) -> object:
        handle = original_start(*args, **kwargs)
        assert handle is not None
        pids.append(handle.process.pid)
        return handle

    with tempfile.TemporaryDirectory() as temporary_directory:
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble.WorkerSlotPool",
                return_value=CountingPool(),
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                side_effect=tracked_start,
            ),
        ):
            render_plan(
                plan,
                Path(temporary_directory) / "three-warm-batches",
            )

    assert len(reservations) == 1
    assert len(reservations[0]) == worker_count
    assert len(pids) == worker_count * 3
    assert (
        set(pids[:worker_count])
        == set(pids[worker_count : worker_count * 2])
        == set(pids[worker_count * 2 :])
    )
    assert not stem_worker_module._WARM_ALL


def test_oversized_warm_ceiling_retries_two_exact_one_shot_batches() -> None:
    mebibyte = 1024 * 1024
    exact_worker_memory = (
        100 * mebibyte,
        100 * mebibyte,
        400 * mebibyte,
        100 * mebibyte,
    )
    memory_budget = 700 * mebibyte
    coordinator_memory = 100 * mebibyte
    plan = _ParallelPlan(part_count=4)
    manifest_hash = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    parallelism = ensemble_module._AutomaticStemParallelism(
        worker_count=2,
        worker_count_by_part=(2, 2, 2, 2),
        manifest_sha256_by_part=(manifest_hash,) * 4,
        worker_reserve_bytes_by_part=exact_worker_memory,
        sample_backed_by_part=(False,) * 4,
    )
    serial_parallelism = ensemble_module._AutomaticStemParallelism(
        worker_count=1,
        worker_count_by_part=(1,) * 4,
        manifest_sha256_by_part=(manifest_hash,) * 4,
    )
    reservations: list[tuple[object, ...]] = []
    starts: list[tuple[bool, bool, bool]] = []
    actual_pool = worker_slots_module.WorkerSlotPool()

    class BudgetPool:
        def reserve_exact(self, claims: object) -> object:
            requested = tuple(claims)
            reservations.append(requested)
            return actual_pool.reserve_exact(requested)

    def slot_context(*args: object, **kwargs: object) -> object:
        del args
        scratch_directory = kwargs["scratch_directory"]
        assert isinstance(scratch_directory, Path)
        return ensemble_module._ManagedWorkerSlotContext(
            pool=BudgetPool(),
            owner_id="c" * 32,
            owner_cpu_capacity=2,
            worker_memory_bytes_by_part=exact_worker_memory,
            coordinator_memory_bytes=coordinator_memory,
            memory_budget_bytes=memory_budget,
            scratch_directory=scratch_directory.resolve(strict=True),
        )

    original_start = ensemble_module._try_start_stem_worker

    def tracked_start(*args: object, **kwargs: object) -> object:
        handle = original_start(*args, **kwargs)
        assert handle is not None
        starts.append(
            (
                bool(kwargs["allow_warm_start"]),
                bool(kwargs["allow_warm_reuse"]),
                handle._warm_worker is not None,
            )
        )
        return handle

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        with patch(
            "tianlai.ensemble._automatic_stem_parallelism",
            return_value=serial_parallelism,
        ):
            render_plan(plan, root / "serial")
        with (
            patch(
                "tianlai.ensemble._automatic_stem_parallelism",
                return_value=parallelism,
            ),
            patch(
                "tianlai.ensemble._automatic_worker_slot_context",
                side_effect=slot_context,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                side_effect=tracked_start,
            ),
        ):
            render_plan(plan, root / "exact-one-shot")

        assert _artifact_bytes(root / "serial") == _artifact_bytes(
            root / "exact-one-shot"
        )

    assert [
        [claim.worker_memory_bytes for claim in requested]
        for requested in reservations
    ] == [
        [400 * mebibyte, 400 * mebibyte],
        [100 * mebibyte, 100 * mebibyte],
        [400 * mebibyte, 100 * mebibyte],
    ]
    assert starts == [(False, False, False)] * 4
    assert not stem_worker_module._WARM_ALL


@pytest.mark.parametrize("failure_mode", ("missing", "stale"))
def test_incomplete_warm_only_batch_retires_session_and_retries_exact(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    events: list[tuple[object, ...]] = []

    class Source:
        def __init__(self, index: int) -> None:
            self.index = index
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Result:
        peak_voices = 1
        manifest_sha256 = "d" * 64
        _warm_used = False

        def __init__(self, index: int) -> None:
            self.index = index
            self.source = Source(index)

        def detach_source(self) -> Source:
            return self.source

        def close(self) -> None:
            self.source.close()

    class Reservation:
        def __init__(self) -> None:
            self.index = 0

        def take(self) -> object:
            slot = SimpleNamespace(index=self.index, close=lambda: None)
            self.index += 1
            return slot

        def close(self) -> None:
            events.append(("reservation-close",))

    class Pool:
        def reserve_exact(self, claims: object) -> Reservation:
            requested = tuple(claims)
            events.append(
                (
                    "reserve",
                    tuple(
                        claim.worker_memory_bytes for claim in requested
                    ),
                )
            )
            return Reservation()

    context = ensemble_module._ManagedWorkerSlotContext(
        pool=Pool(),
        owner_id="e" * 32,
        owner_cpu_capacity=2,
        worker_memory_bytes_by_part=(100, 100, 400, 100),
        coordinator_memory_bytes=100,
        memory_budget_bytes=700,
        scratch_directory=tmp_path,
    )
    binding = stem_worker_module._ManagedWarmBinding(
        owner_id=context.owner_id,
        scratch_directory=tmp_path,
        scratch_volume_id="test-volume",
        worker_memory_ceiling_bytes=400,
        coordinator_memory_bytes=100,
        memory_budget_bytes=700,
        scratch_ceiling_bytes=64,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in (2, 3)
    )
    call_count = 0

    def start(job: object, **kwargs: object) -> object:
        nonlocal call_count
        call_count += 1
        events.append(
            (
                "start",
                job.index,
                kwargs["allow_warm_reuse"],
                kwargs["managed_warm_binding"] is not None,
            )
        )
        if call_count == 1:
            return "warm-2"
        if call_count == 2:
            if failure_mode == "stale":
                raise StemWorkerError("stale warm worker")
            return None
        return f"cold-{job.index}"

    def collect(handle: object) -> Result:
        return Result(int(str(handle).rsplit("-", 1)[1]))

    disabled: list[bool] = []
    terminated: list[object] = []
    with (
        patch(
            "tianlai.ensemble._try_start_stem_worker",
            side_effect=start,
        ),
        patch(
            "tianlai.ensemble.collect_stem_worker",
            side_effect=collect,
        ),
        patch(
            "tianlai.ensemble.terminate_stem_worker",
            side_effect=terminated.append,
        ),
        patch(
            "tianlai.ensemble._retire_managed_stem_worker_session",
        ) as retire_session,
    ):
        batch = ensemble_module._iter_managed_stem_batch(
            jobs,
            scratch_directory=tmp_path,
            allow_warm_start=False,
            slot_context=context,
            warm_binding=binding,
            disable_warm_for_run=lambda: disabled.append(True),
        )
        first = next(batch)
        first[1].close()
        second = next(batch)
        second[1].close()
        with pytest.raises(StopIteration):
            next(batch)

    assert disabled == [True]
    assert terminated == ["warm-2"]
    retire_session.assert_called_once_with(context.owner_id, force=True)
    assert ("reserve", (400, 100)) in events
    assert [event for event in events if event[0] == "start"] == [
        ("start", 2, True, True),
        ("start", 3, True, True),
        ("start", 2, False, False),
        ("start", 3, False, False),
    ]


def test_managed_batch_collects_every_result_before_first_source_is_yielded(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Source:
        def __init__(self, index: int) -> None:
            self.index = index
            self.closed = False

        def close(self) -> None:
            events.append(f"close-{self.index}")
            self.closed = True

    class Result:
        peak_voices = 1
        manifest_sha256 = "a" * 64

        def __init__(self, index: int) -> None:
            self.index = index
            self.source = Source(index)

        def detach_source(self) -> Source:
            events.append(f"detach-{self.index}")
            return self.source

        def close(self) -> None:
            events.append(f"result-close-{self.index}")

    class Reservation:
        def __init__(self) -> None:
            self.index = 0

        def take(self) -> object:
            slot = SimpleNamespace(index=self.index, close=lambda: None)
            self.index += 1
            return slot

        def close(self) -> None:
            events.append("reservation-close")

    reservation = Reservation()
    context = ensemble_module._ManagedWorkerSlotContext(
        pool=SimpleNamespace(reserve_exact=lambda claims: reservation),
        owner_id="1" * 32,
        owner_cpu_capacity=1,
        worker_memory_bytes_by_part=(64, 64, 64),
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_directory=tmp_path,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in range(3)
    )

    def collect(handle: object) -> Result:
        index = int(handle)
        events.append(f"collect-{index}")
        return Result(index)

    with (
        patch("tianlai.ensemble.retire_idle_stem_workers"),
        patch(
            "tianlai.ensemble._try_start_stem_worker",
            side_effect=(0, 1, 2),
        ),
        patch(
            "tianlai.ensemble.collect_stem_worker",
            side_effect=collect,
        ),
        patch("tianlai.ensemble.terminate_stem_worker"),
    ):
        batch = ensemble_module._iter_managed_stem_batch(
            jobs,
            scratch_directory=tmp_path,
            allow_warm_start=False,
            slot_context=context,
        )
        first = next(batch)
        assert events == [
            "collect-0",
            "detach-0",
            "collect-1",
            "detach-1",
            "collect-2",
            "detach-2",
        ]
        first[1].close()
        second = next(batch)
        second[1].close()
        third = next(batch)
        third[1].close()
        with pytest.raises(StopIteration):
            next(batch)

    assert events[-1] == "reservation-close"


def test_managed_batch_late_collect_failure_abandons_all_detached_sources(
    tmp_path: Path,
) -> None:
    class Source:
        closed = False

        def close(self) -> None:
            self.closed = True

    source = Source()

    class Result:
        index = 0
        peak_voices = 1
        manifest_sha256 = "b" * 64

        def detach_source(self) -> Source:
            return source

        def close(self) -> None:
            raise AssertionError("a detached result must close through source")

    class Reservation:
        def take(self) -> object:
            return SimpleNamespace(close=lambda: None)

        def close(self) -> None:
            pass

    context = ensemble_module._ManagedWorkerSlotContext(
        pool=SimpleNamespace(reserve_exact=lambda claims: Reservation()),
        owner_id="2" * 32,
        owner_cpu_capacity=1,
        worker_memory_bytes_by_part=(64, 64, 64),
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_directory=tmp_path,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in range(3)
    )
    terminated: list[object] = []

    with (
        patch("tianlai.ensemble.retire_idle_stem_workers"),
        patch(
            "tianlai.ensemble._try_start_stem_worker",
            side_effect=("zero", "one", "two"),
        ),
        patch(
            "tianlai.ensemble.collect_stem_worker",
            side_effect=(
                Result(),
                StemWorkerError("late collect failure"),
            ),
        ),
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
        )
        with pytest.raises(
            ensemble_module._ManagedStemBatchFailure,
            match="late collect failure",
        ) as captured:
            next(batch)

    assert captured.value.position == 0
    assert source.closed
    assert terminated == ["one", "two"]


def test_managed_batch_slot_cleanup_never_replaces_start_memory_error(
    tmp_path: Path,
) -> None:
    class Slot:
        def close(self) -> None:
            raise OSError("slot cleanup failure")

    class Reservation:
        def take(self) -> Slot:
            return Slot()

        def close(self) -> None:
            pass

    context = ensemble_module._ManagedWorkerSlotContext(
        pool=SimpleNamespace(reserve_exact=lambda claims: Reservation()),
        owner_id="3" * 32,
        owner_cpu_capacity=1,
        worker_memory_bytes_by_part=(64, 64),
        coordinator_memory_bytes=64,
        memory_budget_bytes=1_024,
        scratch_directory=tmp_path,
    )
    jobs = tuple(
        SimpleNamespace(index=index, frame_count=8) for index in range(2)
    )

    with (
        patch("tianlai.ensemble.retire_idle_stem_workers"),
        patch(
            "tianlai.ensemble._try_start_stem_worker",
            side_effect=MemoryError("primary start pressure"),
        ),
        pytest.warns(RuntimeWarning, match="slot cleanup"),
        pytest.raises(MemoryError, match="primary start pressure"),
    ):
        next(
            ensemble_module._iter_managed_stem_batch(
                jobs,
                scratch_directory=tmp_path,
                allow_warm_start=False,
                slot_context=context,
            )
        )


def test_primary_raw_iterator_error_survives_session_retirement_failure() -> None:
    owner_id = "8" * 32

    def failing_body(*args: object, **kwargs: object):
        del args
        holder = kwargs["slot_context_holder"]
        holder.append(SimpleNamespace(owner_id=owner_id))
        raise RuntimeError("primary raw iterator failure")
        yield  # pragma: no cover

    with (
        patch(
            "tianlai.ensemble._iter_raw_stems_in_plan_order_body",
            side_effect=failing_body,
        ),
        patch(
            "tianlai.ensemble._retire_managed_stem_worker_session",
            side_effect=OSError("retirement cleanup failure"),
        ),
        pytest.warns(RuntimeWarning, match="session cleanup"),
        pytest.raises(RuntimeError, match="primary raw iterator failure"),
    ):
        tuple(
            ensemble_module._iter_raw_stems_in_plan_order(
                object(),
                scratch_directory=Path.cwd(),
                hall_tail_seconds=0.0,
                cache=None,
                stream_cache_hits=True,
                refresh=False,
                runtime_fingerprints={},
                summary=None,
            )
        )


def test_primary_render_error_survives_all_raw_cleanup_failures() -> None:
    class FailingRawStems:
        def __iter__(self) -> "FailingRawStems":
            return self

        def __next__(self) -> object:
            raise RuntimeError("primary render consumer failure")

        def close(self) -> None:
            raise OSError("raw iterator close failure")

    with tempfile.TemporaryDirectory() as temporary_directory:
        with (
            patch(
                "tianlai.ensemble._iter_raw_stems_in_plan_order",
                return_value=FailingRawStems(),
            ),
            patch(
                "tianlai.ensemble.retire_idle_stem_workers",
                side_effect=OSError("idle retirement failure"),
            ),
            pytest.warns(RuntimeWarning, match="raw stem phase cleanup"),
            pytest.raises(
                RuntimeError,
                match="primary render consumer failure",
            ),
        ):
            render_plan(
                _ParallelPlan(part_count=1),
                Path(temporary_directory) / "primary-error",
            )


def test_parallel_worker_count_boundary_retires_larger_warm_run() -> None:
    larger_worker_count = _real_managed_worker_count(minimum=3)
    smaller_worker_count = 2
    boundary_index = larger_worker_count * 2
    plan = _ParallelPlan(
        part_count=boundary_index + smaller_worker_count
    )
    manifest_hash = hashlib.sha256(MANIFEST.read_bytes()).hexdigest()
    parallelism = ensemble_module._AutomaticStemParallelism(
        worker_count=larger_worker_count,
        worker_count_by_part=(
            (larger_worker_count,) * boundary_index
            + (smaller_worker_count,) * smaller_worker_count
        ),
        manifest_sha256_by_part=(manifest_hash,) * len(plan.parts),
    )
    original_start = ensemble_module._try_start_stem_worker
    first_run_processes: dict[int, object] = {}
    observations: list[tuple[int, bool, bool]] = []
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)

        def tracked_start(*args: object, **kwargs: object) -> object:
            job = args[0]
            assert isinstance(job, stem_worker_module.StemRenderJob)
            if job.index == boundary_index:
                assert first_run_processes
                assert all(
                    process.poll() is not None
                    for process in first_run_processes.values()
                )
                assert not stem_worker_module._WARM_IDLE
                assert not stem_worker_module._WARM_ALL
            handle = original_start(*args, **kwargs)
            assert handle is not None
            if job.index < boundary_index:
                first_run_processes[handle.process.pid] = handle.process
            observations.append(
                (
                    job.index,
                    bool(kwargs["allow_warm_start"]),
                    handle._warm_worker is not None,
                )
            )
            return handle

        with (
            patch(
                "tianlai.ensemble._automatic_stem_parallelism",
                return_value=parallelism,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                side_effect=tracked_start,
            ),
        ):
            render_plan(plan, root / "worker-count-boundary")

    assert [
        allow
        for _index, allow, _warm in observations[:boundary_index]
    ] == (
        [True] * larger_worker_count
        + [False] * larger_worker_count
    )
    assert all(
        warm
        for _index, _allow, warm in observations[:boundary_index]
    )
    assert observations[boundary_index:] == [
        (index, False, False)
        for index in range(
            boundary_index,
            boundary_index + smaller_worker_count,
        )
    ]


def test_renderer_module_monkeypatch_forces_in_process_rendering() -> None:
    plan = _ParallelPlan(part_count=4)
    with tempfile.TemporaryDirectory() as temporary_directory:
        with (
            patch.object(
                ensemble_module._renderer_module,
                "render_document_blocks",
                side_effect=AssertionError(
                    "module-global patch must not move into a child"
                ),
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                side_effect=AssertionError(
                    "non-pristine renderer state must force serial rendering"
                ),
            ) as starts,
        ):
            render_plan(
                plan,
                Path(temporary_directory) / "module-patched",
            )
        assert starts.call_count == 0


def test_serial_cache_barrier_retires_prior_idle_worker_before_lookup() -> None:
    stem_worker_module._shutdown_warm_pool()
    plan = _ParallelPlan(part_count=1)
    part = plan.parts[0]
    job = stem_worker_module.StemRenderJob.create(
        index=0,
        executor_id=part.executor.executor_id,
        manifest_path=part.executor.capability.manifest_path,
        sample_rate=plan.sample_rate,
        performance=part.performance,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        try:
            handle = stem_worker_module._try_start_stem_worker(
                job,
                scratch_directory=temporary_directory,
                allow_warm_start=True,
                allow_warm_reuse=True,
            )
            assert handle is not None and handle._warm_worker is not None
            worker = handle._warm_worker
            with stem_worker_module.collect_stem_worker(handle):
                pass
            assert worker.process.poll() is None

            def verified_hit(*args: object, **kwargs: object) -> object:
                assert worker.process.poll() is not None
                assert not stem_worker_module._WARM_IDLE
                assert not stem_worker_module._WARM_ALL
                return ("cached-audio", 0, "a" * 64)

            with patch(
                "tianlai.ensemble._render_part_cached",
                side_effect=verified_hit,
            ):
                rendered = ensemble_module._serial_raw_stem(
                    part,
                    plan.sample_rate,
                    cache=object(),
                    snapshot_directory=Path(temporary_directory),
                    refresh=False,
                    runtime_fingerprints={},
                    summary={},
                )
            assert rendered == ("cached-audio", 0, "a" * 64)
        finally:
            stem_worker_module._shutdown_warm_pool()


def test_mixed_plan_parallelizes_safe_runs_around_local_implementation() -> None:
    worker_count = _real_managed_worker_count()
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        local_manifest = _write_local_implementation_manifest(root)
        plan = _ParallelPlan(
            part_count=5,
            local_implementation_indices=frozenset({2}),
            local_implementation_manifest=local_manifest,
        )
        serial_directory = root / "mixed-serial"
        automatic_directory = root / "mixed-automatic"
        with patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=1,
        ):
            render_plan(plan, serial_directory)
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as starts,
        ):
            render_plan(plan, automatic_directory)

        # Both safe pairs use workers.  The local implementation between
        # them stays in-process without disabling the later safe pair.
        assert starts.call_count == 4
        assert _artifact_bytes(serial_directory) == _artifact_bytes(
            automatic_directory
        )


def test_idle_workers_retire_before_in_process_serial_barrier() -> None:
    worker_count = _real_managed_worker_count()
    original_create = ensemble_module.create_instrument
    serial_create_observations: list[int] = []
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        local_manifest = _write_local_implementation_manifest(root)
        plan = _ParallelPlan(
            part_count=3,
            local_implementation_indices=frozenset({2}),
            local_implementation_manifest=local_manifest,
        )
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as starts,
            patch(
                "tianlai.ensemble.retire_idle_stem_workers",
                wraps=ensemble_module.retire_idle_stem_workers,
            ) as retire_idle,
        ):

            def checked_create(*args: object, **kwargs: object) -> object:
                serial_create_observations.append(retire_idle.call_count)
                assert not stem_worker_module._WARM_IDLE
                return original_create(*args, **kwargs)

            with patch(
                "tianlai.ensemble.create_instrument",
                side_effect=checked_create,
            ):
                render_plan(
                    plan,
                    Path(temporary_directory) / "serial-barrier",
                )

    assert starts.call_count == 2
    assert serial_create_observations
    assert all(count >= 1 for count in serial_create_observations)


def test_heavy_worker_is_a_serial_barrier_not_a_global_disable() -> None:
    def estimate(single_part_plan: object) -> SimpleNamespace:
        part = single_part_plan.parts[0]
        index = int(part.executor.part_id.rsplit("-", 1)[1])
        reserve = (
            2 * 1024**3 if index == 2 else 256 * 1024**2
        )
        return SimpleNamespace(
            workers_safe=True,
            worker_reserve_bytes_by_part=(reserve,),
            sample_backed_by_part=(index == 2,),
            managed_worker_safe_by_part=(True,),
            manifest_sha256_by_part=("a" * 64,),
        )

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        local_manifest = _write_local_implementation_manifest(root)
        plan = _ParallelPlan(
            part_count=5,
            local_implementation_indices=frozenset({2}),
            local_implementation_manifest=local_manifest,
        )
        with (
            patch(
                "tianlai.ensemble.derive_worker_resource_estimate",
                side_effect=estimate,
            ),
            patch(
                "tianlai.ensemble.ProjectLimits.from_environment",
                return_value=SimpleNamespace(
                    max_audio_memory_bytes=2 * 1024**3,
                ),
            ),
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=2,
            ) as decisions,
        ):
            parallelism = ensemble_module._automatic_stem_parallelism(
                plan,
                scratch_directory=root,
                hall_tail_seconds=0.0,
            )

    assert parallelism.worker_count_by_part == (2, 2, 1, 2, 2)
    assert parallelism.worker_reserve_bytes_by_part == (
        256 * 1024**2,
        256 * 1024**2,
        2 * 1024**3,
        256 * 1024**2,
        256 * 1024**2,
    )
    assert parallelism.sample_backed_by_part == (
        False,
        False,
        True,
        False,
        False,
    )
    assert decisions.call_count == 2


def test_five_part_run_balances_as_three_plus_two_workers() -> None:
    plan = _ParallelPlan(part_count=5)
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=4,
        ) as decisions,
    ):
        parallelism = ensemble_module._automatic_stem_parallelism(
            plan,
            scratch_directory=Path(temporary_directory),
            hall_tail_seconds=0.0,
        )

    assert parallelism.worker_count_by_part == (3, 3, 3, 2, 2)
    assert decisions.call_count == 3


def test_repeated_identical_instruments_share_one_resource_probe() -> None:
    plan = _ParallelPlan(part_count=5)
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        patch(
            "tianlai.ensemble.derive_worker_resource_estimate",
            wraps=ensemble_module.derive_worker_resource_estimate,
        ) as probes,
        patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=1,
        ),
    ):
        ensemble_module._automatic_stem_parallelism(
            plan,
            scratch_directory=Path(temporary_directory),
            hall_tail_seconds=0.0,
        )

    assert probes.call_count == 1


def test_managed_worker_failure_transparently_reuses_serial_renderer() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(part_count=worker_count)
    original_create = ensemble_module.create_instrument
    fallback_create_observations: list[int] = []
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        expected_directory = root / "expected"
        fallback_directory = root / "fallback"
        with patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=1,
        ):
            render_plan(plan, expected_directory)
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble.collect_stem_worker",
                side_effect=StemWorkerError("injected protocol failure"),
            ) as collects,
            patch(
                "tianlai.ensemble.retire_idle_stem_workers",
                wraps=ensemble_module.retire_idle_stem_workers,
            ) as retire_idle,
            patch(
                "tianlai.ensemble.create_instrument",
                side_effect=lambda *args, **kwargs: (
                    fallback_create_observations.append(
                        retire_idle.call_count
                    ),
                    original_create(*args, **kwargs),
                )[1],
            ),
        ):
            render_plan(plan, fallback_directory)

        assert collects.call_count == 1
        assert fallback_create_observations
        assert all(count >= 1 for count in fallback_create_observations)
        assert _artifact_bytes(expected_directory) == _artifact_bytes(
            fallback_directory
        )


def test_keyboard_interrupt_is_not_converted_to_a_serial_retry() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(
        duration_seconds=0.2,
        part_count=worker_count,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        output = Path(temporary_directory) / "interrupted"
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble.collect_stem_worker",
                side_effect=KeyboardInterrupt,
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=AssertionError(
                    "cancellation must not enter the serial fallback"
                ),
            ) as serial_render,
        ):
            try:
                render_plan(plan, output)
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError("KeyboardInterrupt was not propagated")

        assert serial_render.call_count == 0


def test_worker_start_memory_error_is_not_retried_in_process() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(
        duration_seconds=0.2,
        part_count=worker_count,
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        output = Path(temporary_directory) / "memory-pressure"
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                side_effect=MemoryError("injected worker pressure"),
            ),
            patch(
                "tianlai.ensemble._render_part",
                side_effect=AssertionError(
                    "memory exhaustion must not enter the serial fallback"
                ),
            ) as serial_render,
        ):
            try:
                render_plan(plan, output)
            except MemoryError as exc:
                assert str(exc) == "injected worker pressure"
            else:
                raise AssertionError("MemoryError was not propagated")

        assert serial_render.call_count == 0


def test_cold_parallel_cache_and_hot_cache_keep_ordered_telemetry() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(part_count=worker_count)
    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        cache = root / "cache"
        serial_cache = root / "serial-cache"
        with patch(
            "tianlai.ensemble._automatic_stem_worker_count",
            return_value=1,
        ):
            serial_cold = render_plan(
                plan,
                root / "serial-cold",
                stem_cache_directory=serial_cache,
            )
        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as starts,
        ):
            cold = render_plan(
                plan,
                root / "cold",
                stem_cache_directory=cache,
            )
        assert starts.call_count == worker_count
        assert cold.stem_cache is not None
        assert serial_cold.stem_cache == cold.stem_cache
        assert cold.stem_cache["misses"] == worker_count
        assert cold.stem_cache["writes"] == worker_count
        assert _artifact_bytes(root / "serial-cold") == _artifact_bytes(
            root / "cold"
        )

        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                side_effect=AssertionError("hot cache must not start a worker"),
            ),
        ):
            hot = render_plan(
                plan,
                root / "hot",
                stem_cache_directory=cache,
            )
        assert hot.stem_cache is not None
        assert hot.stem_cache["hits"] == worker_count
        assert hot.stem_cache["misses"] == 0
        assert hot.stem_cache["writes"] == 0
        cold_artifacts = _artifact_bytes(root / "cold")
        hot_artifacts = _artifact_bytes(root / "hot")
        # Runtime cache telemetry must differ; every published musical and
        # provenance artifact remains byte-identical.
        cold_artifacts.pop("缓存遥测.json")
        hot_artifacts.pop("缓存遥测.json")
        assert hot_artifacts == cold_artifacts

        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as refresh_starts,
        ):
            refreshed = render_plan(
                plan,
                root / "refreshed",
                stem_cache_directory=cache,
                refresh_stem_cache=True,
            )
        assert refresh_starts.call_count == worker_count
        assert refreshed.stem_cache is not None
        assert refreshed.stem_cache["hits"] == 0
        assert refreshed.stem_cache["misses"] == worker_count
        assert refreshed.stem_cache["write_skips"] == worker_count


def test_parallel_progress_cancellation_reaps_remaining_workers() -> None:
    worker_count = _real_managed_worker_count()
    plan = _ParallelPlan(
        duration_seconds=0.2,
        part_count=worker_count,
    )
    detached_sources: list[object] = []
    original_detach = stem_worker_module.StemWorkerResult.detach_source

    def track_detached_source(
        result: object,
        *args: object,
        **kwargs: object,
    ) -> object:
        source = original_detach(result, *args, **kwargs)
        detached_sources.append(source)
        return source

    with tempfile.TemporaryDirectory() as temporary_directory:
        root = Path(temporary_directory)
        output = root / "cancelled"

        def cancel_after_first(
            phase: str,
            completed: int,
            total: int,
        ) -> None:
            del total
            if phase == "render_parts" and completed == 1:
                raise RuntimeError("cancel parallel render")

        with (
            patch(
                "tianlai.ensemble._automatic_stem_worker_count",
                return_value=worker_count,
            ),
            patch(
                "tianlai.ensemble.terminate_stem_worker",
                wraps=ensemble_module.terminate_stem_worker,
            ) as terminate,
            patch(
                "tianlai.ensemble._try_start_stem_worker",
                wraps=ensemble_module._try_start_stem_worker,
            ) as starts,
            patch(
                "tianlai.ensemble.retire_idle_stem_workers",
                wraps=ensemble_module.retire_idle_stem_workers,
            ) as retire_idle,
            patch.object(
                stem_worker_module.StemWorkerResult,
                "detach_source",
                track_detached_source,
            ),
        ):
            try:
                render_plan(
                    plan,
                    output,
                    _progress_callback=cancel_after_first,
                )
            except RuntimeError as exc:
                assert str(exc) == "cancel parallel render"
            else:
                raise AssertionError("parallel cancellation was not raised")

        assert starts.call_count == worker_count
        # The batch is now collected atomically before its first source is
        # exposed.  Cancellation therefore closes the remaining owned
        # sources (and their leases) instead of terminating already
        # reaped one-shot children.
        assert terminate.call_count == 0
        assert len(detached_sources) == worker_count
        assert all(source.closed for source in detached_sources)
        assert retire_idle.call_count >= 2
        assert not (output / "渲染回执.json").exists()
