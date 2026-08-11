from __future__ import annotations

from dataclasses import dataclass
import unittest

from tianlai.render_parallelism import (
    automatic_worker_capacity,
    select_render_parallelism,
)
from tianlai.resource_limits import ProjectLimits


@dataclass(frozen=True)
class _Part:
    performance: object


@dataclass(frozen=True)
class _Plan:
    duration_seconds: object
    sample_rate: object
    parts: tuple[_Part, ...]


def _part(
    duration: object = 10.0,
    sample_rate: object = 48_000,
    *,
    events: list[dict[str, object]] | None = None,
) -> _Part:
    performance: dict[str, object] = {
        "duration_seconds": duration,
        "sample_rate": sample_rate,
    }
    if events is not None:
        performance["events"] = events
    return _Part(performance)


def _active_part(duration: float, sample_rate: int) -> _Part:
    return _part(
        duration,
        sample_rate,
        events=[
            {
                "type": "note_on",
                "time": 0.0,
                "note_id": 1,
                "velocity": 0.8,
            },
            {
                "type": "note_off",
                "time": duration,
                "note_id": 1,
            },
        ],
    )


def _plan(
    count: int,
    *,
    duration: object = 10.0,
    sample_rate: object = 48_000,
    parts: tuple[_Part, ...] | None = None,
) -> _Plan:
    return _Plan(
        duration,
        sample_rate,
        (
            parts
            if parts is not None
            else tuple(_part(duration, sample_rate) for _ in range(count))
        ),
    )


def _select(plan: _Plan, **changes: object):
    arguments: dict[str, object] = {
        "limits": ProjectLimits(max_audio_memory_bytes=2 * 1024**3),
        "workers_safe": True,
        "cpu_count": 16,
        "platform_system": "Linux",
        "scratch_available_bytes": 64 * 1024**3,
    }
    arguments.update(changes)
    return select_render_parallelism(plan, **arguments)


class RenderParallelismPolicyTests(unittest.TestCase):
    def test_process_wide_capacity_uses_the_same_conservative_cpu_rule(self) -> None:
        self.assertEqual(automatic_worker_capacity(1), 1)
        self.assertEqual(automatic_worker_capacity(2), 2)
        self.assertEqual(automatic_worker_capacity(4), 3)
        self.assertEqual(automatic_worker_capacity(64), 4)

    def test_automatic_policy_is_capped_and_requires_no_profile_option(self) -> None:
        decision = _select(_plan(12))

        self.assertEqual(decision.worker_count, 4)
        self.assertTrue(decision.parallel)
        self.assertEqual(decision.reason, "automatic")
        self.assertEqual(decision.part_count, 12)
        self.assertEqual(decision.cpu_worker_limit, 4)
        self.assertLessEqual(
            decision.selected_peak_bytes,
            decision.memory_budget_bytes,
        )
        self.assertEqual(decision.to_dict()["worker_count"], 4)

    def test_cpu_policy_reserves_capacity_for_the_coordinator(self) -> None:
        two_cpu = _select(_plan(8), cpu_count=2)
        three_cpu = _select(_plan(8), cpu_count=3)
        four_cpu = _select(_plan(8), cpu_count=4)

        self.assertEqual(two_cpu.worker_count, 2)
        self.assertEqual(three_cpu.worker_count, 2)
        self.assertEqual(four_cpu.worker_count, 3)

    def test_single_cpu_and_single_part_preserve_serial_rendering(self) -> None:
        single_cpu = _select(_plan(4), cpu_count=1)
        single_part = _select(_plan(1))

        self.assertEqual(single_cpu.worker_count, 1)
        self.assertEqual(single_cpu.reason, "single_cpu")
        self.assertEqual(single_part.worker_count, 1)
        self.assertEqual(single_part.reason, "single_part")

    def test_platform_and_worker_eligibility_fail_closed(self) -> None:
        cases = (
            ({"platform_system": "Plan9"}, "unsupported_platform"),
            ({"workers_safe": False}, "workers_ineligible"),
        )

        for changes, reason in cases:
            with self.subTest(reason=reason):
                decision = _select(_plan(4), **changes)
                self.assertEqual(decision.worker_count, 1)
                self.assertEqual(decision.reason, reason)

    def test_memory_budget_uses_bus_and_each_concurrent_worker(self) -> None:
        plan = _plan(4, duration=60.0)
        roomy = _select(plan)
        two_worker_peak = _select(plan, cpu_count=2).selected_peak_bytes

        constrained = _select(
            plan,
            limits=ProjectLimits(
                max_audio_memory_bytes=two_worker_peak,
            ),
        )

        self.assertEqual(roomy.worker_count, 4)
        self.assertEqual(constrained.worker_count, 2)
        self.assertEqual(constrained.memory_worker_limit, 2)
        self.assertEqual(constrained.selected_peak_bytes, two_worker_peak)

    def test_parallel_peak_keeps_one_parent_stem_after_streamed_cache_check(
        self,
    ) -> None:
        plan = _plan(2, duration=60.0)
        decision = _select(plan, cpu_count=2)
        stem_bytes = round(60.0 * 48_000) * 2 * 4
        worker_runtime_bytes = 256 * 1024**2
        worker_chunk_bytes = 65_536 * 2 * 4

        self.assertEqual(decision.worker_count, 2)
        self.assertEqual(
            decision.selected_peak_bytes,
            decision.coordinator_bytes
            + stem_bytes
            + 2 * worker_runtime_bytes
            + 2 * worker_chunk_bytes,
        )

    def test_memory_budget_falls_back_to_serial_without_rejecting_render(self) -> None:
        decision = _select(
            _plan(4, duration=60.0),
            limits=ProjectLimits(max_audio_memory_bytes=1),
        )

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "memory_budget")

    def test_hall_send_bus_reduces_available_parallel_memory(self) -> None:
        plan = _plan(3, duration=60.0)
        dry = _select(plan, cpu_count=3)
        hall = _select(plan, cpu_count=3, hall_tail_seconds=8.0)

        self.assertGreater(hall.coordinator_bytes, dry.coordinator_bytes)
        self.assertGreater(hall.selected_peak_bytes, dry.selected_peak_bytes)

    def test_per_part_duration_drives_stem_memory_estimate(self) -> None:
        plan = _plan(
            0,
            duration=60.0,
            parts=(_part(60.0), _part(30.0), _part(10.0)),
        )
        decision = _select(plan, cpu_count=2)

        self.assertEqual(
            decision.largest_stem_bytes,
            round(60.0 * 48_000) * 8,
        )

    def test_missing_or_mismatched_performance_duration_uses_full_plan(self) -> None:
        plan = _plan(
            0,
            duration=25.0,
            parts=(
                _Part({}),
                _part(1.0, sample_rate=44_100),
            ),
        )
        decision = _select(plan, cpu_count=2)

        self.assertEqual(
            decision.largest_stem_bytes,
            round(25.0 * 48_000) * 8,
        )
        self.assertEqual(decision.worker_count, 2)

    def test_worker_safety_requires_an_explicit_internal_opt_in(self) -> None:
        decision = select_render_parallelism(
            _plan(4),
            limits=ProjectLimits(max_audio_memory_bytes=2 * 1024**3),
            cpu_count=8,
            platform_system="Linux",
        )

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "workers_ineligible")

    def test_short_workload_stays_serial_to_avoid_process_overhead(self) -> None:
        too_short = _select(_plan(4, duration=1.0))
        worthwhile = _select(_plan(2, duration=5.0))

        self.assertEqual(too_short.worker_count, 1)
        self.assertEqual(too_short.reason, "short_workload")
        self.assertEqual(worthwhile.worker_count, 2)

    def test_work_threshold_uses_sample_frames_and_active_notes(self) -> None:
        short_48k = _plan(
            0,
            duration=2.0,
            sample_rate=48_000,
            parts=tuple(_active_part(2.0, 48_000) for _ in range(3)),
        )
        useful_48k = _plan(
            0,
            duration=3.0,
            sample_rate=48_000,
            parts=tuple(_active_part(3.0, 48_000) for _ in range(3)),
        )
        useful_8k = _plan(
            0,
            duration=18.0,
            sample_rate=8_000,
            parts=tuple(_active_part(18.0, 8_000) for _ in range(3)),
        )

        short = _select(short_48k)
        high_rate = _select(useful_48k)
        low_rate = _select(useful_8k)

        self.assertEqual(short.worker_count, 1)
        self.assertEqual(short.reason, "short_workload")
        self.assertEqual(high_rate.worker_count, 3)
        self.assertEqual(low_rate.worker_count, 3)
        self.assertEqual(high_rate.total_work_frames, 432_000)
        self.assertEqual(low_rate.total_work_frames, 432_000)

    def test_two_part_run_scales_the_total_work_threshold(self) -> None:
        short = _plan(
            0,
            duration=2.0,
            sample_rate=48_000,
            parts=tuple(_active_part(2.0, 48_000) for _ in range(2)),
        )
        useful = _plan(
            0,
            duration=3.0,
            sample_rate=48_000,
            parts=tuple(_active_part(3.0, 48_000) for _ in range(2)),
        )

        short_decision = _select(short)
        useful_decision = _select(useful)

        self.assertEqual(short_decision.worker_count, 1)
        self.assertEqual(short_decision.reason, "short_workload")
        self.assertEqual(useful_decision.worker_count, 2)
        self.assertEqual(useful_decision.total_work_frames, 288_000)

    def test_sample_playback_uses_a_calibrated_active_voice_cost(self) -> None:
        one_second = _plan(
            0,
            duration=1.0,
            sample_rate=48_000,
            parts=tuple(_active_part(1.0, 48_000) for _ in range(3)),
        )

        dsp = _select(one_second)
        sampled = _select(
            one_second,
            sample_backed_by_part=(True, True, True),
        )

        self.assertEqual(dsp.worker_count, 1)
        self.assertEqual(dsp.reason, "short_workload")
        self.assertEqual(sampled.worker_count, 3)
        self.assertEqual(sampled.sample_backed_part_count, 3)
        self.assertEqual(sampled.longest_work_frames, 156_000)
        self.assertEqual(sampled.total_work_frames, 468_000)

    def test_long_but_mostly_silent_preview_stays_serial(self) -> None:
        sparse = _plan(
            0,
            duration=20.0,
            sample_rate=8_000,
            parts=tuple(
                _part(
                    20.0,
                    8_000,
                    events=[
                        {
                            "type": "note_on",
                            "time": 0.0,
                            "note_id": 1,
                            "velocity": 0.8,
                        },
                        {
                            "type": "note_off",
                            "time": 0.02,
                            "note_id": 1,
                        },
                    ],
                )
                for _ in range(3)
            ),
        )

        decision = _select(sparse)

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "short_workload")
        self.assertLess(decision.total_work_frames, 432_000)

    def test_scratch_space_limits_the_sliding_worker_window(self) -> None:
        plan = _plan(4, duration=60.0)
        stem_bytes = round(60.0 * 48_000) * 8
        reserve = 512 * 1024**2
        decision = _select(
            plan,
            scratch_available_bytes=reserve + 2 * stem_bytes,
        )

        self.assertEqual(decision.worker_count, 2)
        self.assertEqual(decision.scratch_worker_limit, 2)
        self.assertEqual(decision.selected_scratch_bytes, 2 * stem_bytes)

    def test_unknown_scratch_space_fails_closed_without_a_user_setting(self) -> None:
        decision = _select(_plan(4), scratch_available_bytes=None)

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "scratch_budget")
        self.assertIsNone(decision.scratch_available_bytes)

    def test_insufficient_scratch_space_preserves_serial_functionality(self) -> None:
        decision = _select(
            _plan(4),
            scratch_available_bytes=0,
        )

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "scratch_budget")
        self.assertEqual(decision.selected_scratch_bytes, 0)

    def test_known_worker_footprints_reduce_memory_parallelism(self) -> None:
        plan = _plan(4, duration=60.0)
        mib = 1024**2
        decision = _select(
            plan,
            limits=ProjectLimits(max_audio_memory_bytes=2 * 1024**3),
            worker_reserve_bytes_by_part=(
                850 * mib,
                850 * mib,
                256 * mib,
                256 * mib,
            ),
        )

        self.assertEqual(decision.worker_count, 2)
        self.assertEqual(decision.memory_worker_limit, 2)
        self.assertEqual(
            decision.largest_worker_reserve_bytes,
            850 * mib,
        )

    def test_one_known_heavy_worker_keeps_the_complete_serial_path(self) -> None:
        gib = 1024**3
        decision = _select(
            _plan(3),
            limits=ProjectLimits(max_audio_memory_bytes=2 * gib),
            worker_reserve_bytes_by_part=(gib + 1, 0, 0),
        )

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "heavy_worker")

    def test_malformed_worker_footprints_fail_closed(self) -> None:
        decision = _select(
            _plan(3),
            worker_reserve_bytes_by_part=(1, 2),
        )

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "invalid_plan_facts")

    def test_malformed_backend_facts_fail_closed(self) -> None:
        for flags in ((True, False), (True, 1, False)):
            with self.subTest(flags=flags):
                decision = _select(
                    _plan(3),
                    sample_backed_by_part=flags,
                )

                self.assertEqual(decision.worker_count, 1)
                self.assertEqual(decision.reason, "invalid_plan_facts")

    def test_invalid_cpu_fact_fails_closed_to_one(self) -> None:
        decision = _select(_plan(4), cpu_count=0)

        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "single_cpu")

    def test_invalid_plan_or_hall_facts_fail_closed_to_serial(self) -> None:
        invalid_plan = _select(_plan(4, sample_rate=True))
        invalid_hall = _select(_plan(4), hall_tail_seconds=float("nan"))

        self.assertEqual(invalid_plan.worker_count, 1)
        self.assertEqual(invalid_plan.reason, "invalid_plan_facts")
        self.assertEqual(invalid_hall.worker_count, 1)
        self.assertEqual(invalid_hall.reason, "invalid_plan_facts")

    def test_hall_tail_uses_the_renderers_ceil_frame_contract(self) -> None:
        plan = _plan(2, duration=0.0, sample_rate=8_000)
        dry = _select(plan, hall_tail_seconds=0.0)
        with_fractional_tail = _select(
            plan,
            hall_tail_seconds=0.00001,
        )

        # Enabling the hall adds the float32 send for both dry and tail
        # frames, while the fractional tail adds one float64 bus frame.
        self.assertEqual(
            with_fractional_tail.coordinator_bytes - dry.coordinator_bytes,
            32,
        )


if __name__ == "__main__":
    unittest.main()
