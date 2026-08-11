"""Conservative, zero-configuration stem-render parallelism policy.

The renderer owns the concurrency mechanism; this module only decides how
many independent stem workers may run.  Keeping the policy separate makes two
important contracts explicit:

* parallelism is an implementation detail, never another render-profile knob;
* uncertainty or an unsafe process context preserves the complete serial path.

The memory estimate models the phase in which managed subprocesses stream
private float32 stems to scratch files while the coordinator owns the mix
buses.  Hall processing happens only after the workers have closed, so its
larger existing preflight estimate is still enforced independently by
:mod:`tianlai.resource_limits`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import platform
from typing import Any

from .plain_file import read_plain_file_bytes
from .resource_limits import ProjectLimits
from .runtime_layout import RuntimeLayoutError, discover_runtime_layout


# Four workers already cover the useful part of the speed-up on typical local
# computers while avoiding an unbounded multiplication of Python runtimes,
# native synthesizers and decoded sample assets.  This is deliberately an
# engine policy, not an environment variable or user-facing render option.
_MAX_AUTOMATIC_WORKERS = 4

# Every worker owns a Python/NumPy runtime and may load instrument metadata or
# native state that is not represented by its bounded float32 output chunk.
# The resource-evidence probe raises this floor for known sample inventories;
# the policy then reduces concurrency or selects the serial renderer.
_PER_WORKER_RESERVE_BYTES = 256 * 1024 * 1024
_COORDINATOR_RESERVE_BYTES = 64 * 1024 * 1024
_WORKER_CHUNK_FRAMES = 65_536

# Starting clean interpreters and importing the audio runtime has a real fixed
# cost.  A seconds-only threshold is wrong across the supported 8--384 kHz
# range: three sparse 8 kHz previews can contain less work than one short
# 48 kHz part.  The per-part floor corresponds to one continuously active
# 48 kHz stem of roughly three seconds; the total floor scales for a two-part
# run and caps at three parts, where startup is fully amortised.  Inactive
# frames retain a quarter-weight because every backend still advances its
# event loop while silent.
_MIN_PARALLEL_LONGEST_WORK_FRAMES = 144_000
_MIN_PARALLEL_TOTAL_THRESHOLD_PARTS = 3
_INACTIVE_FRAME_WORK_NUMERATOR = 1
_INACTIVE_FRAME_WORK_DENOMINATOR = 4
_DSP_ACTIVE_FRAME_WORK_NUMERATOR = 3
_SAMPLE_ACTIVE_FRAME_WORK_NUMERATOR = 12
_RELEASE_WORK_SECONDS = 0.5

# Scratch is temporary but must not consume the volume's last useful space.
# The execution layer supplies current free bytes for its private scratch
# volume; this fixed reserve is intentionally unavailable to the workers.
_SCRATCH_FREE_RESERVE_BYTES = 512 * 1024 * 1024

_FLOAT32_STEREO_BYTES_PER_FRAME = 2 * 4
_FLOAT64_STEREO_BYTES_PER_FRAME = 2 * 8
_SUPPORTED_SYSTEMS = frozenset({"Windows", "Linux", "Darwin"})

# Worker eligibility is decided from tiny, checked metadata rather than by
# constructing an instrument in the coordinator.  Bounding both inputs keeps
# a malformed third-party manifest from turning this optional optimisation
# probe into an unbounded read.  A rejected probe always retains the complete
# serial renderer.
_MAX_WORKER_RESOURCE_DOCUMENT_BYTES = 16 * 1024 * 1024
_DEFAULT_RESOURCE_VERIFICATION_NAME = "资源核验.json"
_BUILTIN_MANAGED_WORKER_TYPES = frozenset(
    {
        "dedicated_fx",
        "dedicated_sfz",
        "melodic_toms",
        "modeled_bianzhong",
        "modeled_instrument",
        "mtg_solo_sax",
        "oscillator",
        "procedural_sfx",
        "piano",
        "reversed_cymbal",
        "sample",
        "soundfont",
        "synthesizer",
        "cello",
        "flute",
        "violin",
        "vpo_brass",
        "vpo_celesta",
        "vpo_cowbell",
        "vpo_harp",
        "vpo_mixed_choir",
        "vpo_orchestral_hit",
        "vpo_percussion",
        "vpo_solo_string",
        "vpo_string_section",
        "vpo_woodwind",
        "vsco2_viola_section",
    }
)
_ALLOWED_MANAGED_OVERRIDE_FIELDS = frozenset(
    {"release_seconds", "release_tail_gain", "sample_variant"}
)


@dataclass(frozen=True, slots=True)
class WorkerResourceEstimate:
    """Per-part worker memory weights plus a fail-closed safety verdict.

    ``worker_reserve_bytes_by_part`` always has one entry per plan part, even
    when ``workers_safe`` is false.  The execution layer can therefore record
    useful diagnostics without ever interpreting a missing/invalid resource
    report as permission to use subprocess workers.
    """

    workers_safe: bool
    reason: str
    worker_reserve_bytes_by_part: tuple[int, ...]
    sample_backed_by_part: tuple[bool, ...]
    managed_worker_safe_by_part: tuple[bool, ...]
    manifest_sha256_by_part: tuple[str, ...]


def _duplicate_safe_object(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        document[key] = value
    return document


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _load_bounded_json_object(
    path: Path,
) -> tuple[Path, dict[str, Any], str]:
    """Read one small regular JSON document for an eligibility decision."""

    identity, payload = read_plain_file_bytes(
        path,
        maximum_bytes=_MAX_WORKER_RESOURCE_DOCUMENT_BYTES,
    )
    if len(payload) < 2:
        raise ValueError("resource document size is outside the safe range")
    document = json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_duplicate_safe_object,
        parse_constant=_reject_json_constant,
    )
    if not isinstance(document, dict):
        raise ValueError("resource document root must be an object")
    return (
        identity.path,
        document,
        hashlib.sha256(payload).hexdigest(),
    )


def _manifest_path_from_part(part: Any) -> Path:
    raw_path = part.executor.capability.manifest_path
    if not isinstance(raw_path, (str, os.PathLike)):
        raise ValueError("manifest path must be path-like")
    return Path(raw_path)


def _is_trusted_managed_worker_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> bool:
    """Limit subprocess execution to this installed catalogue's code.

    A third-party manifest can still render through the unchanged in-process
    path.  Merely claiming to be asset-free is not enough to prove that a
    local factory is process-independent or safe to execute concurrently.
    """

    try:
        catalog_root = discover_runtime_layout(
            require_catalog=True
        ).catalog.resolve(strict=True)
        relative = manifest_path.relative_to(catalog_root)
    except (OSError, RuntimeLayoutError, ValueError):
        return False
    if manifest_path.name != "乐器.json" or len(relative.parts) < 2:
        return False

    implementation = manifest.get("implementation")
    if implementation is not None:
        # Local Python factories are intentionally kept on the established
        # in-process path.  Catalogue location and a self-declared manifest
        # flag cannot prove process independence or concurrent safety, and
        # importlib would otherwise reopen mutable source after verification.
        return False
    return type(manifest.get("type")) is str and (
        manifest["type"] in _BUILTIN_MANAGED_WORKER_TYPES
    )


def _managed_worker_overrides_safe(part: Any) -> bool:
    try:
        overrides = part.executor.override_map
    except AttributeError:
        return False
    if not isinstance(overrides, dict):
        return False
    if any(type(key) is not str for key in overrides):
        return False
    if set(overrides) - _ALLOWED_MANAGED_OVERRIDE_FIELDS:
        return False
    release = overrides.get("release_seconds")
    if "release_seconds" in overrides and (
        isinstance(release, bool)
        or not isinstance(release, (int, float))
        or not math.isfinite(float(release))
        or float(release) < 0.0
    ):
        return False
    tail_gain = overrides.get("release_tail_gain")
    if "release_tail_gain" in overrides and (
        isinstance(tail_gain, bool)
        or not isinstance(tail_gain, (int, float))
        or not math.isfinite(float(tail_gain))
        or not 0.0 <= float(tail_gain) <= 1.0
    ):
        return False
    variant = overrides.get("sample_variant")
    if "sample_variant" in overrides and (
        not isinstance(variant, str)
        or not variant
        or len(variant) > 256
        or "\x00" in variant
    ):
        return False
    return True


def _resource_verification_path(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> Path:
    raw_name = manifest.get(
        "resource_verification",
        _DEFAULT_RESOURCE_VERIFICATION_NAME,
    )
    if not isinstance(raw_name, str) or not raw_name:
        raise ValueError("resource verification name must be a string")
    # Evidence for worker memory must be an immediate sibling.  In particular,
    # do not let an untrusted manifest redirect this optional probe elsewhere.
    if (
        raw_name in {".", ".."}
        or "/" in raw_name
        or "\\" in raw_name
        or "\x00" in raw_name
        or Path(raw_name).name != raw_name
    ):
        raise ValueError("resource verification must be a sibling filename")
    return manifest_path.with_name(raw_name)


def _is_explicit_asset_free_dsp(manifest: dict[str, Any]) -> bool:
    asset_root = manifest.get("asset_root")
    if asset_root is not None and asset_root != "":
        return False
    external_assets = manifest.get("external_audio_assets")
    declared_project_dsp = (
        manifest.get("provenance_kind") == "project_authored_dsp"
        and isinstance(external_assets, list)
        and not external_assets
    )
    declared_asset_free_runtime = (
        manifest.get("runtime_asset_policy")
        == "no_external_audio_assets"
    )
    # An explicit non-empty declaration always wins over an asset-free label.
    return (
        declared_project_dsp or declared_asset_free_runtime
    ) and not (
        isinstance(external_assets, list) and bool(external_assets)
    )


def derive_worker_resource_estimate(plan: Any) -> WorkerResourceEstimate:
    """Derive automatic worker reserves from immutable manifest evidence.

    Sample-backed instruments are eligible only when their manifest's sibling
    resource-verification document supplies both a positive ``sample_bytes``
    inventory and ``decoded_float32_stereo_bytes`` upper bound.  The latter is
    added to, rather than substituted for, the Python/native runtime reserve;
    compressed file bytes are never treated as a RAM bound.  Explicitly
    asset-free procedural/modelled DSP receives the fixed reserve.  Unknown
    manifests or missing/malformed evidence select the serial path.

    This is an internal probe, not a render option.  It deliberately catches
    filesystem and JSON failures and reports a stable reason instead of
    removing render functionality.
    """

    try:
        parts = tuple(plan.parts)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return WorkerResourceEstimate(
            False,
            "invalid_plan_parts",
            (),
            (),
            (),
            (),
        )

    reserves = [_PER_WORKER_RESERVE_BYTES] * len(parts)
    sample_backed = [False] * len(parts)
    managed_worker_safe = [False] * len(parts)
    manifest_sha256 = [""] * len(parts)
    for index, part in enumerate(parts):
        try:
            requested_manifest_path = _manifest_path_from_part(part)
            (
                manifest_path,
                manifest,
                manifest_sha256[index],
            ) = _load_bounded_json_object(
                requested_manifest_path
            )
            managed_worker_safe[index] = (
                _is_trusted_managed_worker_manifest(
                    manifest_path,
                    manifest,
                )
                and _managed_worker_overrides_safe(part)
            )
            evidence_path = _resource_verification_path(
                manifest_path,
                manifest,
            )

            evidence: dict[str, Any] | None = None
            try:
                (
                    _evidence_path,
                    evidence,
                    _evidence_sha256,
                ) = _load_bounded_json_object(
                    evidence_path
                )
            except FileNotFoundError:
                pass
            has_sample_bytes = (
                evidence is not None and "sample_bytes" in evidence
            )
            sample_bytes = (
                None if evidence is None else evidence.get("sample_bytes")
            )
            if has_sample_bytes:
                if (
                    isinstance(sample_bytes, bool)
                    or not isinstance(sample_bytes, int)
                    or sample_bytes <= 0
                ):
                    raise ValueError("invalid sample_bytes evidence")
                if (
                    evidence is None
                    or "decoded_float32_stereo_bytes" not in evidence
                ):
                    return WorkerResourceEstimate(
                        False,
                        (
                            f"part_{index}_decoded_sample_"
                            "evidence_missing"
                        ),
                        tuple(reserves),
                        tuple(sample_backed),
                        tuple(managed_worker_safe),
                        tuple(manifest_sha256),
                    )
                decoded_bytes = evidence.get(
                    "decoded_float32_stereo_bytes"
                )
                if (
                    isinstance(decoded_bytes, bool)
                    or not isinstance(decoded_bytes, int)
                    or decoded_bytes <= 0
                ):
                    raise ValueError("invalid decoded sample byte evidence")
                reserves[index] = _PER_WORKER_RESERVE_BYTES + decoded_bytes
                sample_backed[index] = True
                continue

            if _is_explicit_asset_free_dsp(manifest):
                reserves[index] = _PER_WORKER_RESERVE_BYTES
                continue
        except (
            AttributeError,
            TypeError,
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            RuntimeError,
            OverflowError,
        ):
            return WorkerResourceEstimate(
                False,
                f"part_{index}_resource_evidence_invalid",
                tuple(reserves),
                tuple(sample_backed),
                tuple(managed_worker_safe),
                tuple(manifest_sha256),
            )
        return WorkerResourceEstimate(
            False,
            f"part_{index}_sample_evidence_missing",
            tuple(reserves),
            tuple(sample_backed),
            tuple(managed_worker_safe),
            tuple(manifest_sha256),
        )

    return WorkerResourceEstimate(
        True,
        "verified",
        tuple(reserves),
        tuple(sample_backed),
        tuple(managed_worker_safe),
        tuple(manifest_sha256),
    )


@dataclass(frozen=True, slots=True)
class RenderParallelismDecision:
    """Auditable internal result of the automatic worker policy."""

    worker_count: int
    reason: str
    part_count: int
    cpu_count: int
    cpu_worker_limit: int
    memory_worker_limit: int
    memory_budget_bytes: int
    coordinator_bytes: int
    selected_peak_bytes: int
    largest_stem_bytes: int
    total_work_frames: int
    longest_work_frames: int
    sample_backed_part_count: int
    scratch_worker_limit: int
    scratch_available_bytes: int | None
    selected_scratch_bytes: int
    largest_worker_reserve_bytes: int

    @property
    def parallel(self) -> bool:
        return self.worker_count > 1

    def to_dict(self) -> dict[str, int | str | bool | None]:
        """Return diagnostics without exposing a mutable policy surface."""

        return {
            "worker_count": self.worker_count,
            "parallel": self.parallel,
            "reason": self.reason,
            "part_count": self.part_count,
            "cpu_count": self.cpu_count,
            "cpu_worker_limit": self.cpu_worker_limit,
            "memory_worker_limit": self.memory_worker_limit,
            "memory_budget_bytes": self.memory_budget_bytes,
            "coordinator_bytes": self.coordinator_bytes,
            "selected_peak_bytes": self.selected_peak_bytes,
            "largest_stem_bytes": self.largest_stem_bytes,
            "total_work_frames": self.total_work_frames,
            "longest_work_frames": self.longest_work_frames,
            "sample_backed_part_count": self.sample_backed_part_count,
            "scratch_worker_limit": self.scratch_worker_limit,
            "scratch_available_bytes": self.scratch_available_bytes,
            "selected_scratch_bytes": self.selected_scratch_bytes,
            "largest_worker_reserve_bytes": (
                self.largest_worker_reserve_bytes
            ),
        }


def _runtime_cpu_count() -> int:
    """Best-effort count constrained by process affinity when available."""

    process_cpu_count = getattr(os, "process_cpu_count", None)
    if callable(process_cpu_count):
        try:
            count = process_cpu_count()
        except (OSError, TypeError, ValueError):
            pass
        else:
            if (
                isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            ):
                return count
    affinity = getattr(os, "sched_getaffinity", None)
    if callable(affinity):
        try:
            count = len(affinity(0))
        except (OSError, TypeError, ValueError):
            pass
        else:
            if count > 0:
                return count
    count = os.cpu_count()
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count
    return 1


def _positive_cpu_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 1


def _cpu_worker_limit(cpu_count: int) -> int:
    if cpu_count <= 1:
        return 1
    if cpu_count == 2:
        return 2
    # Leave one logical processor for mixing, cache I/O and the host process.
    return min(_MAX_AUTOMATIC_WORKERS, cpu_count - 1)


def automatic_worker_capacity(cpu_count: int | None = None) -> int:
    """Return the process-wide managed-worker permit capacity.

    The execution layer uses the same CPU rule for its non-blocking global
    permit pool, preventing independent renders in one long-lived service
    from each assuming they own the whole computer.
    """

    effective_cpu_count = _positive_cpu_count(
        _runtime_cpu_count() if cpu_count is None else cpu_count
    )
    return _cpu_worker_limit(effective_cpu_count)


def _safe_frame_count(duration: object, sample_rate: int) -> int | None:
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
    ):
        return None
    try:
        seconds = float(duration)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0.0:
        return None
    return max(1, round(seconds * sample_rate))


def _safe_tail_frame_count(duration: object, sample_rate: int) -> int | None:
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
    ):
        return None
    try:
        seconds = float(duration)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0.0:
        return None
    return max(0, math.ceil(seconds * sample_rate))


def _stem_sizes(plan: Any, dry_frame_count: int) -> tuple[int, ...]:
    """Estimate every exact float32 stem, falling back conservatively."""

    sizes: list[int] = []
    sample_rate = int(plan.sample_rate)
    for part in plan.parts:
        performance = getattr(part, "performance", None)
        frame_count: int | None = None
        if isinstance(performance, dict):
            performance_rate = performance.get("sample_rate", sample_rate)
            if (
                isinstance(performance_rate, int)
                and not isinstance(performance_rate, bool)
                and performance_rate == sample_rate
            ):
                frame_count = _safe_frame_count(
                    performance.get("duration_seconds"),
                    sample_rate,
                )
        if frame_count is None:
            # A validated plan normally has the exact duration above.  If a
            # custom caller omits it, assuming a full-plan stem is safer than
            # opportunistically increasing concurrency.
            frame_count = dry_frame_count
        sizes.append(frame_count * _FLOAT32_STEREO_BYTES_PER_FRAME)
    return tuple(sorted(sizes, reverse=True))


def _part_render_work_frames(
    part: Any,
    *,
    frame_count: int,
    sample_rate: int,
    sample_backed: bool,
) -> int:
    """Estimate backend work from active voices without changing audio.

    Valid conductor output supplies an ordered event array.  Unknown custom
    plan shapes fall back to full-frame work so this optional policy never
    becomes a new validation dependency.  Exact resource and output bounds
    continue to use the complete stem length rather than this cost hint.
    """

    performance = getattr(part, "performance", None)
    if not isinstance(performance, dict) or "events" not in performance:
        return frame_count
    raw_events = performance.get("events")
    if not isinstance(raw_events, list):
        return frame_count

    events: list[tuple[int, int, str, object, float]] = []
    try:
        for sequence, raw in enumerate(raw_events):
            if not isinstance(raw, dict):
                return frame_count
            event_type = raw.get("type")
            if event_type not in {"note_on", "note_off"}:
                continue
            raw_time = raw.get("time", 0.0)
            if isinstance(raw_time, bool) or not isinstance(
                raw_time,
                (int, float),
            ):
                return frame_count
            seconds = float(raw_time)
            if not math.isfinite(seconds) or seconds < 0.0:
                return frame_count
            sample = round(seconds * sample_rate)
            if sample < 0 or sample > frame_count:
                return frame_count
            note_id = raw.get("note_id")
            velocity = raw.get("velocity", 0.8)
            if event_type == "note_on":
                if isinstance(velocity, bool) or not isinstance(
                    velocity,
                    (int, float),
                ):
                    return frame_count
                velocity = float(velocity)
                if not math.isfinite(velocity):
                    return frame_count
            else:
                velocity = 0.0
            events.append(
                (sample, sequence, str(event_type), note_id, velocity)
            )
    except (OverflowError, TypeError, ValueError):
        return frame_count

    events.sort(key=lambda event: (event[0], event[1]))
    active_notes: set[object] = set()
    cursor = 0
    active_voice_frames = 0
    release_frames = 0
    release_length = round(_RELEASE_WORK_SECONDS * sample_rate)
    for sample, _sequence, event_type, note_id, velocity in events:
        active_voice_frames += (sample - cursor) * len(active_notes)
        cursor = sample
        if event_type == "note_on":
            if velocity > 0.0:
                try:
                    active_notes.add(note_id)
                except TypeError:
                    return frame_count
        else:
            try:
                was_active = note_id in active_notes
            except TypeError:
                return frame_count
            if was_active:
                active_notes.remove(note_id)
                release_frames += min(
                    release_length,
                    frame_count - sample,
                )
    active_voice_frames += (frame_count - cursor) * len(active_notes)
    active_voice_frames += release_frames

    base_work = math.ceil(
        frame_count
        * _INACTIVE_FRAME_WORK_NUMERATOR
        / _INACTIVE_FRAME_WORK_DENOMINATOR
    )
    active_work_numerator = (
        _SAMPLE_ACTIVE_FRAME_WORK_NUMERATOR
        if sample_backed
        else _DSP_ACTIVE_FRAME_WORK_NUMERATOR
    )
    active_work = math.ceil(
        active_voice_frames
        * active_work_numerator
        / _INACTIVE_FRAME_WORK_DENOMINATOR
    )
    return max(1, base_work + active_work)


def _stem_render_work_frames_by_part(
    plan: Any,
    dry_frame_count: int,
    sample_backed_by_part: tuple[bool, ...],
) -> tuple[int, ...]:
    sample_rate = int(plan.sample_rate)
    work: list[int] = []
    for part, sample_backed in zip(
        plan.parts,
        sample_backed_by_part,
        strict=True,
    ):
        performance = getattr(part, "performance", None)
        frame_count: int | None = None
        if isinstance(performance, dict):
            performance_rate = performance.get("sample_rate", sample_rate)
            if (
                isinstance(performance_rate, int)
                and not isinstance(performance_rate, bool)
                and performance_rate == sample_rate
            ):
                frame_count = _safe_frame_count(
                    performance.get("duration_seconds"),
                    sample_rate,
                )
        if frame_count is None:
            frame_count = dry_frame_count
        work.append(
            _part_render_work_frames(
                part,
                frame_count=frame_count,
                sample_rate=sample_rate,
                sample_backed=sample_backed,
            )
        )
    return tuple(work)


def _stem_render_work_frames(
    plan: Any,
    dry_frame_count: int,
    sample_backed_by_part: tuple[bool, ...],
) -> tuple[int, ...]:
    return tuple(
        sorted(
            _stem_render_work_frames_by_part(
                plan,
                dry_frame_count,
                sample_backed_by_part,
            ),
            reverse=True,
        )
    )


def derive_parallelism_work_frames(
    plan: Any,
    *,
    sample_backed_by_part: Sequence[bool],
) -> tuple[int, ...] | None:
    """Return stable per-part work units for adaptive timing.

    The tuple preserves plan order so each value stays bound to the manifest
    that rendered it.  These are benefit-model units only; exact output and
    resource bounds continue to use complete stem frames.  Invalid custom
    plan facts return ``None`` and therefore leave the static policy intact.
    """

    try:
        parts = tuple(plan.parts)
        raw_sample_rate = plan.sample_rate
        if (
            isinstance(raw_sample_rate, bool)
            or not isinstance(raw_sample_rate, int)
            or raw_sample_rate <= 0
        ):
            return None
        flags = _sample_backend_flags(
            len(parts), sample_backed_by_part
        )
        dry_frames = _safe_frame_count(
            plan.duration_seconds, raw_sample_rate
        )
        if flags is None or dry_frames is None:
            return None
        return _stem_render_work_frames_by_part(plan, dry_frames, flags)
    except (
        AttributeError,
        TypeError,
        ValueError,
        OverflowError,
        RuntimeError,
    ):
        return None


def _parallel_phase_peak_bytes(
    coordinator_bytes: int,
    worker_count: int,
    parent_stem_bytes: int,
    descending_worker_reserves: tuple[int, ...],
) -> int:
    # One array is consumed in plan order.  Cache refresh/conflict checks scan
    # existing entries in fixed-size blocks, so they no longer require a
    # second track-sized ndarray alongside the freshly rendered stem.
    return (
        coordinator_bytes
        + parent_stem_bytes
        + sum(descending_worker_reserves[:worker_count])
        + worker_count
        * (
            _WORKER_CHUNK_FRAMES
            * _FLOAT32_STEREO_BYTES_PER_FRAME
        )
    )


def _scratch_bytes(
    descending_stem_bytes: tuple[int, ...],
    worker_count: int,
) -> int:
    return sum(descending_stem_bytes[:worker_count])


def _worker_reserves(
    part_count: int,
    supplied: Sequence[int] | None,
) -> tuple[int, ...] | None:
    if supplied is None:
        return (_PER_WORKER_RESERVE_BYTES,) * part_count
    if len(supplied) != part_count:
        return None
    reserves: list[int] = []
    for value in supplied:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            return None
        reserves.append(max(_PER_WORKER_RESERVE_BYTES, value))
    return tuple(sorted(reserves, reverse=True))


def _sample_backend_flags(
    part_count: int,
    supplied: Sequence[bool] | None,
) -> tuple[bool, ...] | None:
    if supplied is None:
        return (False,) * part_count
    try:
        if len(supplied) != part_count:
            return None
        flags = tuple(supplied)
    except (TypeError, ValueError, OverflowError):
        return None
    if len(flags) != part_count or any(type(value) is not bool for value in flags):
        return None
    return flags


def _serial_decision(
    *,
    reason: str,
    part_count: int,
    cpu_count: int,
    cpu_limit: int,
    memory_limit: int,
    memory_budget: int,
    coordinator_bytes: int,
    stem_sizes: tuple[int, ...],
    total_work_frames: int,
    longest_work_frames: int,
    sample_backed_part_count: int,
    scratch_limit: int,
    scratch_available: int | None,
    worker_reserves: tuple[int, ...],
) -> RenderParallelismDecision:
    # A one-worker decision means the established in-process serial renderer,
    # not a managed subprocess.  It holds one complete float32 stem and uses
    # no private raw scratch file.
    selected_peak = (
        coordinator_bytes + stem_sizes[0]
        if stem_sizes
        else coordinator_bytes
    )
    return RenderParallelismDecision(
        worker_count=1,
        reason=reason,
        part_count=part_count,
        cpu_count=cpu_count,
        cpu_worker_limit=cpu_limit,
        memory_worker_limit=memory_limit,
        memory_budget_bytes=memory_budget,
        coordinator_bytes=coordinator_bytes,
        selected_peak_bytes=selected_peak,
        largest_stem_bytes=stem_sizes[0] if stem_sizes else 0,
        total_work_frames=total_work_frames,
        longest_work_frames=longest_work_frames,
        sample_backed_part_count=sample_backed_part_count,
        scratch_worker_limit=scratch_limit,
        scratch_available_bytes=scratch_available,
        selected_scratch_bytes=0,
        largest_worker_reserve_bytes=(
            worker_reserves[0] if worker_reserves else 0
        ),
    )


def select_render_parallelism(
    plan: Any,
    *,
    hall_tail_seconds: float = 0.0,
    limits: ProjectLimits | None = None,
    workers_safe: bool = False,
    cpu_count: int | None = None,
    platform_system: str | None = None,
    scratch_available_bytes: int | None = None,
    worker_reserve_bytes_by_part: Sequence[int] | None = None,
    sample_backed_by_part: Sequence[bool] | None = None,
    adaptive_worker_limit: int | None = None,
    adaptive_short_workload: bool = False,
) -> RenderParallelismDecision:
    """Choose ``1..N`` stem workers without creating a user setting.

    ``workers_safe`` is the execution layer's final eligibility signal.  It
    must be false when a worker job cannot reconstruct an instrument from
    immutable inputs or when known native/sample-memory requirements exceed
    the reserve modeled here.  A false signal never removes functionality: it
    selects the existing serial renderer.

    Optional host facts exist for deterministic tests and embedding.  Normal
    callers omit CPU/platform facts and receive automatic detection.  The
    managed-subprocess execution layer must explicitly opt in with
    ``workers_safe=True`` after checking its interpreter/backend contract.  It
    must also pass the free bytes on the private scratch volume; omitting that
    fact selects the established serial path rather than inventing a
    user-facing setting or risking an unbounded temporary allocation.
    ``worker_reserve_bytes_by_part`` lets that layer account for known native
    or decoded-sample footprints.  Values are clamped to the fixed runtime
    reserve.  ``sample_backed_by_part`` lets the same verified evidence apply
    a higher active-voice cost to sample playback than to asset-free DSP.
    Both facts are internal and malformed values fail closed to serial
    rendering.  ``adaptive_worker_limit`` is an optional private benefit cap
    learned from successful timings on this computer.  It can only reduce a
    normal automatic window, or (with ``adaptive_short_workload=True``) admit
    a workload rejected solely by the conservative fixed benefit threshold.
    It can never bypass backend eligibility, CPU, memory, scratch, or heavy
    worker safety gates, and it remains capped by the four-worker engine rule.
    """

    limits = limits or ProjectLimits.from_environment()
    parts = getattr(plan, "parts", ())
    plan_facts_valid = True
    try:
        part_count = len(parts)
        raw_sample_rate = plan.sample_rate
        if (
            isinstance(raw_sample_rate, bool)
            or not isinstance(raw_sample_rate, int)
            or raw_sample_rate <= 0
        ):
            raise ValueError("invalid sample rate")
        sample_rate = raw_sample_rate
    except (AttributeError, TypeError, ValueError, OverflowError):
        part_count = 0
        sample_rate = 1
        plan_facts_valid = False

    effective_cpu_count = _positive_cpu_count(
        _runtime_cpu_count() if cpu_count is None else cpu_count
    )
    cpu_limit = automatic_worker_capacity(effective_cpu_count)
    memory_budget = max(1, int(limits.max_audio_memory_bytes))

    dry_frames = _safe_frame_count(
        getattr(plan, "duration_seconds", None),
        sample_rate,
    )
    if dry_frames is None:
        dry_frames = 1
        plan_facts_valid = False
    tail_frames = _safe_tail_frame_count(hall_tail_seconds, sample_rate)
    if tail_frames is None:
        tail_frames = 0
        plan_facts_valid = False
    total_frames = dry_frames + tail_frames
    coordinator_bytes = (
        _COORDINATOR_RESERVE_BYTES
        + total_frames * _FLOAT64_STEREO_BYTES_PER_FRAME
        + (
            total_frames * _FLOAT32_STEREO_BYTES_PER_FRAME
            if tail_frames > 0
            else 0
        )
    )
    stem_sizes = (
        _stem_sizes(plan, dry_frames)
        if part_count > 0 and sample_rate > 0
        else ()
    )
    worker_reserves = _worker_reserves(
        part_count,
        worker_reserve_bytes_by_part,
    )
    if worker_reserves is None:
        worker_reserves = ()
        plan_facts_valid = False
    sample_backend_flags = _sample_backend_flags(
        part_count,
        sample_backed_by_part,
    )
    if sample_backend_flags is None:
        sample_backend_flags = (False,) * part_count
        plan_facts_valid = False
    stem_work_frames = (
        _stem_render_work_frames(
            plan,
            dry_frames,
            sample_backend_flags,
        )
        if part_count > 0 and sample_rate > 0
        else ()
    )

    cpu_candidate_limit = min(
        max(1, part_count),
        cpu_limit,
        _MAX_AUTOMATIC_WORKERS,
    )
    memory_candidate_limit = min(
        max(1, part_count),
        _MAX_AUTOMATIC_WORKERS,
    )
    memory_limit = 1
    for candidate in range(2, memory_candidate_limit + 1):
        peak = _parallel_phase_peak_bytes(
            coordinator_bytes,
            candidate,
            stem_sizes[0],
            worker_reserves,
        )
        if peak > memory_budget:
            break
        memory_limit = candidate

    normalized_scratch_available: int | None
    if scratch_available_bytes is None:
        normalized_scratch_available = None
        # The managed worker adds large temporary files that the serial route
        # does not need.  If the execution layer cannot inspect the scratch
        # volume, retain functionality through the established serial path.
        scratch_limit = 1
    elif (
        isinstance(scratch_available_bytes, int)
        and not isinstance(scratch_available_bytes, bool)
        and scratch_available_bytes >= 0
    ):
        normalized_scratch_available = scratch_available_bytes
        usable_scratch = max(
            0,
            scratch_available_bytes - _SCRATCH_FREE_RESERVE_BYTES,
        )
        scratch_limit = 1
        for candidate in range(2, memory_candidate_limit + 1):
            if _scratch_bytes(stem_sizes, candidate) > usable_scratch:
                break
            scratch_limit = candidate
    else:
        normalized_scratch_available = 0
        scratch_limit = 1

    system = platform.system() if platform_system is None else platform_system
    total_work_frames = sum(stem_work_frames)
    longest_work_frames = (
        stem_work_frames[0] if stem_work_frames else 0
    )
    sample_backed_part_count = sum(sample_backend_flags)
    required_total_work_frames = (
        _MIN_PARALLEL_LONGEST_WORK_FRAMES
        * min(_MIN_PARALLEL_TOTAL_THRESHOLD_PARTS, max(1, part_count))
    )

    normalized_adaptive_limit: int | None = None
    if (
        isinstance(adaptive_worker_limit, int)
        and not isinstance(adaptive_worker_limit, bool)
        and 1 <= adaptive_worker_limit <= _MAX_AUTOMATIC_WORKERS
    ):
        normalized_adaptive_limit = adaptive_worker_limit

    serial_reason: str | None = None
    if not plan_facts_valid:
        serial_reason = "invalid_plan_facts"
    elif workers_safe is not True:
        serial_reason = "workers_ineligible"
    elif system not in _SUPPORTED_SYSTEMS:
        serial_reason = "unsupported_platform"
    elif part_count < 2:
        serial_reason = "single_part"
    elif cpu_limit < 2:
        serial_reason = "single_cpu"
    elif memory_limit < 2:
        serial_reason = "memory_budget"
    elif (
        worker_reserves
        and worker_reserves[0] > memory_budget // 2
    ):
        serial_reason = "heavy_worker"
    elif scratch_limit < 2:
        serial_reason = "scratch_budget"
    else:
        short_workload = (
            total_work_frames < required_total_work_frames
            or longest_work_frames < _MIN_PARALLEL_LONGEST_WORK_FRAMES
        )
        adaptive_short_admission = (
            short_workload
            and adaptive_short_workload is True
            and normalized_adaptive_limit is not None
            and normalized_adaptive_limit >= 2
        )
        if short_workload and not adaptive_short_admission:
            serial_reason = "short_workload"

    if serial_reason is not None:
        return _serial_decision(
            reason=serial_reason,
            part_count=part_count,
            cpu_count=effective_cpu_count,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            memory_budget=memory_budget,
            coordinator_bytes=coordinator_bytes,
            stem_sizes=stem_sizes,
            total_work_frames=total_work_frames,
            longest_work_frames=longest_work_frames,
            sample_backed_part_count=sample_backed_part_count,
            scratch_limit=scratch_limit,
            scratch_available=normalized_scratch_available,
            worker_reserves=worker_reserves,
        )

    selected = min(cpu_candidate_limit, memory_limit, scratch_limit)
    reason = "automatic"
    if normalized_adaptive_limit is not None:
        selected = min(selected, normalized_adaptive_limit)
        if selected < 2:
            return _serial_decision(
                reason="adaptive_serial",
                part_count=part_count,
                cpu_count=effective_cpu_count,
                cpu_limit=cpu_limit,
                memory_limit=memory_limit,
                memory_budget=memory_budget,
                coordinator_bytes=coordinator_bytes,
                stem_sizes=stem_sizes,
                total_work_frames=total_work_frames,
                longest_work_frames=longest_work_frames,
                sample_backed_part_count=sample_backed_part_count,
                scratch_limit=scratch_limit,
                scratch_available=normalized_scratch_available,
                worker_reserves=worker_reserves,
            )
        if selected != min(cpu_candidate_limit, memory_limit, scratch_limit) or (
            adaptive_short_workload is True
        ):
            reason = "adaptive"
    return RenderParallelismDecision(
        worker_count=selected,
        reason=reason,
        part_count=part_count,
        cpu_count=effective_cpu_count,
        cpu_worker_limit=cpu_limit,
        memory_worker_limit=memory_limit,
        memory_budget_bytes=memory_budget,
        coordinator_bytes=coordinator_bytes,
        selected_peak_bytes=_parallel_phase_peak_bytes(
            coordinator_bytes,
            selected,
            stem_sizes[0],
            worker_reserves,
        ),
        largest_stem_bytes=stem_sizes[0],
        total_work_frames=total_work_frames,
        longest_work_frames=longest_work_frames,
        sample_backed_part_count=sample_backed_part_count,
        scratch_worker_limit=scratch_limit,
        scratch_available_bytes=normalized_scratch_available,
        selected_scratch_bytes=_scratch_bytes(stem_sizes, selected),
        largest_worker_reserve_bytes=worker_reserves[0],
    )


__all__ = [
    "RenderParallelismDecision",
    "WorkerResourceEstimate",
    "automatic_worker_capacity",
    "derive_worker_resource_estimate",
    "derive_parallelism_work_frames",
    "select_render_parallelism",
]
